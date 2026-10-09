"""Schedule sidecar read/write for the FRED on-demand query seam.

This module is shared between ``tools/fred_ondemand_canary.py`` (which discovers
and writes the sidecar) and the Macro caller (which reads it read-only).  It
was moved verbatim from the tool so that both callers share one definition of
the sidecar format, one digest vocabulary and one rejection vocabulary.

Layer contract: this module imports only the routine query vocabulary
(``tokmo.values.fred_query``), two records primitives, and the stdlib.
It reaches no Facts, Programs, Runtime, live transport, home directory,
process environment, or wall clock.  ``now`` is injected, exactly as before.
"""

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.records.private_sqlite import prepare_private_path
from tokmo.values.fred_query import (
    SCHEDULE_DOMAIN,
    SERIES_REGISTRY,
    FredReleaseScheduleV1,
    build_fred_release_schedule_v1,
    canonical_json,
    domain_digest,
    release_slot_for,
)

MAX_SIDECAR_BYTES = 65_536
SIDECAR_NAME = "schedule-payems-v1.json"
SIDECAR_SCHEMA = "tokmo.fred-ondemand.schedule.r2"
SIDECAR_SCHEMA_VERSION = 1


class ScheduleSidecarInvalid(ValueError):
    """A stored schedule that cannot be trusted; never an automatic refetch."""


def format_instant_v1(value: datetime) -> str:
    return (
        value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")
    )


def parse_instant_v1(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ScheduleSidecarInvalid("invalid_stored_instant")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ScheduleSidecarInvalid("invalid_stored_instant") from error


def build_sidecar_payload_v1(
    *,
    schedule: FredReleaseScheduleV1,
    release_id: int,
    release_date: date,
    official_release_at: datetime,
    provenance: dict,
) -> dict:
    body = {
        "schema": SIDECAR_SCHEMA,
        "schema_version": SIDECAR_SCHEMA_VERSION,
        "series_id": schedule.series_id,
        "release_id": release_id,
        "release_date": release_date.isoformat(),
        "official_release_at": format_instant_v1(official_release_at),
        "release_slot": schedule.release_slot,
        "valid_until": format_instant_v1(schedule.valid_until),
        "provenance": provenance,
        "provenance_digest": schedule.provenance_digest,
    }
    return dict(body, payload_digest=hashlib.sha256(canonical_json(body)).hexdigest())


def encode_sidecar_payload_v1(payload: dict) -> bytes:
    return canonical_json(payload)


def write_schedule_sidecar_v1(path: Path, payload: dict) -> None:
    """Atomic, private, canonical. A crash leaves the old file or the new one."""
    write_atomic_json(
        path, encode_sidecar_payload_v1(payload), max_bytes=MAX_SIDECAR_BYTES
    )


@dataclass(frozen=True)
class LoadedScheduleV1:
    schedule: FredReleaseScheduleV1
    release_id: int
    release_date: date
    official_release_at: datetime
    provenance: dict
    expired: bool


def load_schedule_sidecar_v1(path: Path, *, now: datetime) -> LoadedScheduleV1 | None:
    """Return the stored schedule, or ``None`` when absent. Never guesses.

    Every check is fail-closed and raises ``ScheduleSidecarInvalid``, which
    the caller must treat as a STOP with zero external starts: an automatic
    refetch here would hide the corruption behind a fresh-looking success.
    """
    if not os.path.lexists(path):
        return None
    safety = prepare_private_path(path)
    if safety.unsafe:
        raise ScheduleSidecarInvalid(safety.reason)
    try:
        raw = read_atomic_json(path, max_bytes=MAX_SIDECAR_BYTES)
    except (OSError, ValueError) as error:
        raise ScheduleSidecarInvalid("unreadable_sidecar") from error
    try:
        payload = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ScheduleSidecarInvalid("invalid_sidecar_json") from error
    if type(payload) is not dict:
        raise ScheduleSidecarInvalid("invalid_sidecar_json")
    if (
        payload.get("schema") != SIDECAR_SCHEMA
        or payload.get("schema_version") != SIDECAR_SCHEMA_VERSION
        or type(payload.get("schema_version")) is not int
    ):
        raise ScheduleSidecarInvalid("sidecar_schema_mismatch")
    try:
        canonical = canonical_json(payload)
    except ValueError as error:
        raise ScheduleSidecarInvalid("noncanonical_sidecar") from error
    if canonical != raw:
        raise ScheduleSidecarInvalid("noncanonical_sidecar")
    stored_digest = payload.get("payload_digest")
    body = {key: value for key, value in payload.items() if key != "payload_digest"}
    if stored_digest != hashlib.sha256(canonical_json(body)).hexdigest():
        raise ScheduleSidecarInvalid("sidecar_payload_digest_mismatch")
    provenance = payload.get("provenance")
    if type(provenance) is not dict:
        raise ScheduleSidecarInvalid("invalid_sidecar_provenance")
    if payload.get("provenance_digest") != domain_digest(SCHEDULE_DOMAIN, provenance):
        raise ScheduleSidecarInvalid("sidecar_provenance_digest_mismatch")
    series_id = payload.get("series_id")
    if type(series_id) is not str or series_id not in SERIES_REGISTRY:
        raise ScheduleSidecarInvalid("unregistered_series")
    release_id = payload.get("release_id")
    if type(release_id) is not int or type(release_id) is bool or release_id <= 0:
        raise ScheduleSidecarInvalid("invalid_release_id")
    try:
        release_date = date.fromisoformat(payload.get("release_date"))
    except (TypeError, ValueError) as error:
        raise ScheduleSidecarInvalid("invalid_release_date") from error
    if payload.get("release_slot") != release_slot_for(series_id, release_date):
        raise ScheduleSidecarInvalid("release_slot_mismatch")
    official_release_at = parse_instant_v1(payload.get("official_release_at"))
    try:
        schedule = build_fred_release_schedule_v1(
            series_id=series_id,
            release_id=release_id,
            release_date=release_date,
            official_release_at=official_release_at,
            schedule_provenance=provenance,
        )
    except ValueError as error:
        raise ScheduleSidecarInvalid("schedule_reconstruction_failure") from error
    if (
        schedule.release_slot != payload["release_slot"]
        or schedule.provenance_digest != payload["provenance_digest"]
        or format_instant_v1(schedule.valid_until) != payload.get("valid_until")
    ):
        raise ScheduleSidecarInvalid("schedule_reconstruction_mismatch")
    return LoadedScheduleV1(
        schedule,
        release_id,
        release_date,
        official_release_at,
        provenance,
        now > schedule.valid_until,
    )
