#!/usr/bin/env python3
"""On-demand FRED→Macro production assembly. Live use requires an explicit gate.

G2 Delta SDS §3-§8. This tool is the only place in the delta that owns a
network boundary, a provider URL literal and a private production root. It
assembles reviewed parts it does not modify: the routine query adapter, the
Macro due policy, the routine query cache, and the pure BLS calendar parser.

Three subcommands:

``preflight``  offline only. Opens a private cache under an explicitly named
               root, checks the credential by metadata alone, proves the
               dual-authority join against a pinned ICS fixture, and asserts
               that it left no schedule sidecar in the production root.
``canary``     one bounded invocation. Refuses to run without the explicit
               execute flag. At most 3 authenticated FRED starts and 1 public
               BLS start; zero of both when a valid schedule sidecar reloads.
``readback``   offline read of what a previous invocation settled.

Nothing here registers a scheduler, sends a message, writes an order or calls
a model, and nothing re-fetches on a timer: the schedule sidecar is a reload
cache read on demand, never a refresh loop.
"""

import argparse
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import pwd
import stat
import sys
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tokmo.facts.bls_release_calendar import (
    BLS_TZID, IANA_ZONE, MAX_ICS_BYTES, BlsCalendarRejection,
    parse_bls_calendar_v1, select_release_instant_v1,
)
from tokmo.facts.fred_live import (
    FredLiveTransport, build_live_opener, load_fred_api_key,
)
from tokmo.facts.fred_query import (
    FredQueryAdapterV1, FredScheduleAdapterV1, TerminalMonthGuardedPortV1,
    observation_limit,
)
from tokmo.fred_query_composition import (
    FredStartBudget, BudgetedFredTransportV1,
)
from tokmo.programs.macro_fred_inputs import (
    run_macro_fred_enrichment_v1,
    derive_query_window_v1, previous_calendar_month, last_day_of_month,
    WINDOW_MONTHS_BEFORE_TERMINAL,
)
from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.records.fred_query_cache import DATABASE_NAME, FredQueryCacheV1
from tokmo.records.fred_schedule_sidecar import (
    LoadedScheduleV1, MAX_SIDECAR_BYTES, SIDECAR_NAME, SIDECAR_SCHEMA,
    SIDECAR_SCHEMA_VERSION, ScheduleSidecarInvalid,
    build_sidecar_payload_v1, encode_sidecar_payload_v1,
    format_instant_v1, load_schedule_sidecar_v1, parse_instant_v1,
    write_schedule_sidecar_v1,
)
from tokmo.records.private_sqlite import prepare_private_path
from tokmo.values.fred_query import (
    SCHEDULE_DOMAIN, SERIES_PAYEMS, SERIES_REGISTRY, FredCoverageV1,
    FredDueStateV1, FredQueryFailureV1, FredReleaseScheduleV1,
    FredSeriesQueryV1, build_fred_release_schedule_v1, canonical_json,
    domain_digest, release_slot_for,
)

# -- declared boundaries ----------------------------------------------------

# Source B, public and unauthenticated. This literal exists only here: the
# parser in ``src`` receives bytes and never names a host.
BLS_SOURCE_URL = "https://www.bls.gov/schedule/news_release/bls.ics"
BLS_HOST = "www.bls.gov"
BLS_RESOURCE_PATH = "/schedule/news_release/bls.ics"
BLS_SOURCE_ID = "BLS_ICS_V1"
BLS_SUMMARY = "Employment Situation"
BLS_UA_ENV_KEY = "BLS_USER_AGENT"
JOIN_RULE = ("SUMMARY_EXACT+TZID_ALLOWLIST+DST_CROSSCHECK+"
             "LOCAL_DATE_EQUAL+SINGLETON")
GRACE_SECONDS = 900

# Per-invocation ceilings (§4.8). Cumulative purpose accounting is recorded
# in the closure record, not here.
FRED_MAX_STARTS = 3
BLS_MAX_STARTS = 1
FRED_PURPOSE = "FRED_MACRO_ONDEMAND_ROUTINE_V1"
BLS_PURPOSE = "BLS_RELEASE_SCHEDULE_VERIFICATION_V1"

TIMEOUT_SECONDS = 10.0
MAX_CREDENTIAL_BYTES = 4096

EXECUTE_FLAG = "--fred-ondemand-canary-execute"

BLS_FAILURE_CODES = frozenset({
    "timeout", "connection_failure", "redirect", "bad_request", "http_429",
    "http_5xx", "body_too_large", "unsupported_content_encoding",
    "protocol_failure",
})


@dataclass(frozen=True)
class OndemandPathsV1:
    cache_root: Path
    sidecar: Path
    credential: Path
    lock: Path


def ondemand_paths() -> OndemandPathsV1:
    """The production identity of this lane; never opened by ``preflight``."""
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    namespace = home / ".local" / "share" / "tokmo-v3" / "fred-ondemand"
    root = namespace / "v1"
    return OndemandPathsV1(root, root / SIDECAR_NAME,
                           Path(__file__).resolve().parents[1] / ".secrets" / "hermes-credentials" / "fred.env",
                           namespace / ".ondemand-v1.lock")


def paths_for_root(root: Path, *, credential: Path) -> OndemandPathsV1:
    """The same identity around an explicitly injected root."""
    if not isinstance(root, Path) or not isinstance(credential, Path):
        raise ValueError("explicit_root_required")
    return OndemandPathsV1(root, root / SIDECAR_NAME, credential,
                           root.parent / ".ondemand-v1.lock")


# -- per-invocation start budgets (§4.8) ------------------------------------


class BlsStartBudget:
    """Public BLS starts; counted separately from the authenticated source."""

    def __init__(self, maximum: int = BLS_MAX_STARTS):
        self.maximum = maximum
        self.starts = 0

    def spend(self) -> None:
        if self.starts >= self.maximum:
            raise ValueError("bls_start_budget_exceeded")
        self.starts += 1


class BudgetedBlsTransportV1:
    def __init__(self, transport, budget: BlsStartBudget):
        self._transport = transport
        self._budget = budget

    def __call__(self):
        self._budget.spend()
        return self._transport()


# -- the public BLS calendar transport --------------------------------------


@dataclass(frozen=True)
class BlsTransportOutcome:
    code: str
    body: bytes | None = None

    def __post_init__(self):
        if self.code not in BLS_FAILURE_CODES | {"success"}:
            raise ValueError("closed_transport_code")
        if (self.code == "success") != (type(self.body) is bytes):
            raise ValueError("transport_body_contract")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_bls_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                       _NoRedirect())


def load_bls_user_agent(path: Path) -> str:
    """Read a declared-contact User-Agent from a private env file.

    Mirrors the fd-anchored metadata-safe pattern of ``load_fred_api_key``:
    the file must be a regular non-symlink at mode 0600, owner-matched, and
    bounded in size (``MAX_CREDENTIAL_BYTES``, reused because the same
    boundary governs every private credential). The value must contain
    ``@`` to satisfy the BLS Terms requirement that robots identify their
    contact owner — the same rule the SEC declared-contact contract applies
    (``runtime/config.py``).

    The returned value is never logged, persisted, or repr'd.
    """
    try:
        info = os.lstat(path)
    except OSError:
        raise ValueError("credential_unsafe") from None
    uid = os.getuid() if hasattr(os, "getuid") else None
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or uid is None or info.st_uid != uid
            or info.st_size > MAX_CREDENTIAL_BYTES):
        raise ValueError("credential_unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("credential_unsafe")
            raw = os.read(descriptor, MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise ValueError("credential_unsafe") from None
    if len(raw) > MAX_CREDENTIAL_BYTES or not raw.endswith(b"\n") or b"\r" in raw:
        raise ValueError("credential_grammar")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise ValueError("credential_grammar") from None
    assignments = [line for line in lines if line and not line.startswith("#")]
    if len(assignments) != 1:
        raise ValueError("credential_grammar")
    line = assignments[0]
    if "=" not in line:
        raise ValueError("credential_grammar")
    key, _, value = line.partition("=")
    if key.strip() != BLS_UA_ENV_KEY:
        raise ValueError("credential_grammar")
    user_agent = value.strip()
    if not user_agent or "@" not in user_agent:
        raise ValueError("credential_grammar")
    if not all(32 <= ord(ch) <= 126 for ch in user_agent):
        raise ValueError("credential_grammar")
    return user_agent


class BlsIcsTransport:
    """One credential-free public GET; retry 0, redirect 0, bounded body.

    It carries no API key, no cookie and no identifying header beyond a
    declared-contact User-Agent loaded from a private file, and it is a
    separate type from ``FredLiveTransport`` precisely so no authenticated
    transport can ever be repointed at this host.
    """

    __slots__ = ("_opener", "_user_agent")

    def __init__(self, *, opener, user_agent: str):
        if not isinstance(user_agent, str) or "@" not in user_agent:
            raise ValueError("bls_user_agent_no_contact")
        self._opener = opener
        self._user_agent = user_agent

    def __repr__(self) -> str:
        return "BlsIcsTransport(<redacted>)"

    def __call__(self) -> BlsTransportOutcome:
        try:
            wire = urllib.request.Request(
                BLS_SOURCE_URL, data=None, method="GET",
                headers={"User-Agent": self._user_agent,
                         "Accept": "text/calendar",
                         "Accept-Encoding": "identity"})
            parts = urllib.parse.urlsplit(wire.full_url)
            if ((parts.scheme, parts.hostname, parts.port, parts.path,
                 parts.query) != ("https", BLS_HOST, None, BLS_RESOURCE_PATH, "")
                    or wire.method != "GET"):
                return BlsTransportOutcome("protocol_failure")
            with self._opener.open(wire, timeout=TIMEOUT_SECONDS) as response:
                return _read_ics_response(response)
        except Exception as error:
            code = _bls_failure_code(error)
            del error
            return BlsTransportOutcome(code)


def _read_ics_response(response) -> BlsTransportOutcome:
    if getattr(response, "status", None) != 200:
        return BlsTransportOutcome("protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        return BlsTransportOutcome("unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if not 0 <= int(declared) <= MAX_ICS_BYTES:
                return BlsTransportOutcome("body_too_large")
        except (TypeError, ValueError):
            return BlsTransportOutcome("protocol_failure")
    body = response.read(MAX_ICS_BYTES + 1)
    if type(body) is not bytes or len(body) > MAX_ICS_BYTES:
        return BlsTransportOutcome("body_too_large")
    return BlsTransportOutcome("success", body)


def _bls_failure_code(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (301, 302, 303, 307, 308):
            return "redirect"
        if error.code == 429:
            return "http_429"
        if 400 <= error.code < 500:
            return "bad_request"
        if 500 <= error.code < 600:
            return "http_5xx"
        return "protocol_failure"
    if isinstance(error, urllib.error.URLError):
        return ("timeout" if isinstance(getattr(error, "reason", None),
                                        TimeoutError) else "connection_failure")
    if isinstance(error, TimeoutError):
        return "timeout"
    if isinstance(error, OSError):
        return "connection_failure"
    return "protocol_failure"


# -- schedule provenance and the private sidecar (§4.6/§4.9) ----------------


def build_schedule_provenance_v1(*, release_id: int, release_date: date,
                                 official_release_at: datetime,
                                 body_sha256: str, body_bytes: int) -> dict:
    """The exact digested field set; no raw calendar material is retained."""
    return {
        "fred_release_id": release_id,
        "fred_release_date": release_date.isoformat(),
        "bls_source_id": BLS_SOURCE_ID,
        "bls_source_url": BLS_SOURCE_URL,
        "bls_summary": BLS_SUMMARY,
        "bls_tzid": BLS_TZID,
        "bls_resolved_zone": IANA_ZONE,
        "official_release_at_utc": format_instant_v1(official_release_at),
        "bls_body_sha256": body_sha256,
        "bls_body_bytes": body_bytes,
        "grace_seconds": GRACE_SECONDS,
        "join_rule": JOIN_RULE,
    }


# -- the bounded canary ------------------------------------------------------


@dataclass(frozen=True)
class DiscoveryOutcomeV1:
    schedule: FredReleaseScheduleV1 | None
    release_id: int | None
    release_date: date | None
    official_release_at: datetime | None
    provenance: dict | None
    stopped_at: str | None
    failure_code: str | None


def discover_schedule_v1(*, schedule_adapter, bls_transport) -> DiscoveryOutcomeV1:
    """Steps 1-3 of §5 plus the local join. The one failure STOPs the rest."""
    release_id = schedule_adapter.query_series_release_v1(series_id=SERIES_PAYEMS)
    if isinstance(release_id, FredQueryFailureV1):
        return DiscoveryOutcomeV1(None, None, None, None, None, "series_release",
                                  release_id.code.value)
    release_date = schedule_adapter.query_release_dates_v1(release_id=release_id)
    if isinstance(release_date, FredQueryFailureV1):
        return DiscoveryOutcomeV1(None, release_id, None, None, None,
                                  "release_dates", release_date.code.value)
    outcome = bls_transport()
    if getattr(outcome, "code", None) != "success":
        return DiscoveryOutcomeV1(None, release_id, release_date, None, None,
                                  "bls_calendar", getattr(outcome, "code", None))
    try:
        calendar = parse_bls_calendar_v1(getattr(outcome, "body", None))
        official_release_at = select_release_instant_v1(
            calendar, summary=BLS_SUMMARY, local_date=release_date)
        provenance = build_schedule_provenance_v1(
            release_id=release_id, release_date=release_date,
            official_release_at=official_release_at,
            body_sha256=calendar.body_sha256, body_bytes=calendar.body_bytes)
        schedule = build_fred_release_schedule_v1(
            series_id=SERIES_PAYEMS, release_id=release_id,
            release_date=release_date,
            official_release_at=official_release_at,
            schedule_provenance=provenance)
    except (BlsCalendarRejection, ValueError) as error:
        return DiscoveryOutcomeV1(None, release_id, release_date, None, None,
                                  "schedule_join", type(error).__name__)
    return DiscoveryOutcomeV1(schedule, release_id, release_date,
                              official_release_at, provenance, None, None)


def run_canary_v1(*, paths: OndemandPathsV1, fred_transport, bls_transport,
                  api_key_provider, clock, rediscover_schedule: bool) -> dict:
    """One bounded invocation in the §4.6 order: sidecar, then admission."""
    now = _require_aware_utc(clock())
    fred_budget = FredStartBudget(maximum=FRED_MAX_STARTS)
    bls_budget = BlsStartBudget()
    fred_wire = BudgetedFredTransportV1(fred_transport, fred_budget)
    bls_wire = BudgetedBlsTransportV1(bls_transport, bls_budget)

    cache = FredQueryCacheV1(paths.cache_root, opened_at=now)
    schedule_source = "sidecar"
    try:
        loaded = load_schedule_sidecar_v1(paths.sidecar, now=now)
    except ScheduleSidecarInvalid as error:
        if not rediscover_schedule:
            return _result("schedule_sidecar_invalid", fred_budget, bls_budget,
                           schedule_source="invalid", stopped_at="sidecar",
                           failure_code=str(error))
        loaded = None
        schedule_source = "rediscovery"
    if loaded is not None and loaded.expired:
        loaded = None
        schedule_source = "expired_rediscovery"
    if loaded is None:
        if schedule_source == "sidecar":
            schedule_source = "discovery"
        discovery = discover_schedule_v1(
            schedule_adapter=FredScheduleAdapterV1(
                transport=fred_wire, api_key_provider=api_key_provider),
            bls_transport=bls_wire)
        if discovery.schedule is None:
            return _result("schedule_unavailable", fred_budget, bls_budget,
                           schedule_source=schedule_source,
                           stopped_at=discovery.stopped_at,
                           failure_code=discovery.failure_code,
                           release_id=discovery.release_id,
                           release_date=_isoformat(discovery.release_date))
        # Persisted as soon as the join validates and before any observation
        # request, so a later failure cannot force a rediscovery.
        write_schedule_sidecar_v1(paths.sidecar, build_sidecar_payload_v1(
            schedule=discovery.schedule, release_id=discovery.release_id,
            release_date=discovery.release_date,
            official_release_at=discovery.official_release_at,
            provenance=discovery.provenance))
        loaded = LoadedScheduleV1(discovery.schedule, discovery.release_id,
                                  discovery.release_date,
                                  discovery.official_release_at,
                                  discovery.provenance, False)

    query, expected_terminal_month = derive_query_window_v1(
        release_date=loaded.release_date, as_of=now)
    port = TerminalMonthGuardedPortV1(FredQueryAdapterV1(
        transport=fred_wire, api_key_provider=api_key_provider, clock=clock))
    enrichment = run_macro_fred_enrichment_v1(
        query=query, schedule=loaded.schedule, cache=cache, query_port=port,
        clock=clock)
    settled = cache.read_attempt_state(query.query_identity,
                                       loaded.schedule.release_slot, now=now)
    failure_code = (settled.last_failure_code if enrichment.provider_starts
                    and enrichment.coverage is not FredCoverageV1.USED else None)
    if enrichment.coverage is FredCoverageV1.USED:
        code = "enrichment_complete"
    elif failure_code == "NOT_YET_RELEASED":
        code = "schedule_released_data_not_available"
    elif enrichment.due_state is FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE:
        code = "schedule_unavailable"
    else:
        code = "enrichment_omitted"
    return _result(
        code, fred_budget, bls_budget, schedule_source=schedule_source,
        failure_code=failure_code, release_id=loaded.release_id,
        release_date=_isoformat(loaded.release_date),
        release_slot=loaded.schedule.release_slot,
        official_release_at=format_instant_v1(loaded.official_release_at),
        eligible_from=format_instant_v1(loaded.schedule.eligible_from),
        valid_until=format_instant_v1(loaded.schedule.valid_until),
        expected_terminal_month=list(expected_terminal_month),
        observation_starts=enrichment.provider_starts,
        due_state=enrichment.due_state.value,
        coverage=enrichment.coverage.value,
        direction=None if enrichment.direction is None else enrichment.direction.value,
        current_date=_isoformat(enrichment.current_date),
        prior_date=_isoformat(enrichment.prior_date),
        query_identity=enrichment.query_identity,
        revision_identity=enrichment.revision_identity,
        schedule_provenance_digest=loaded.schedule.provenance_digest,
        bls_body_sha256=loaded.provenance["bls_body_sha256"],
        bls_body_bytes=loaded.provenance["bls_body_bytes"])


def _result(code: str, fred_budget: FredStartBudget, bls_budget: BlsStartBudget,
            **fields) -> dict:
    return dict({"code": code, "fred_starts": fred_budget.starts,
                 "bls_starts": bls_budget.starts, "observation_starts": 0,
                 "fred_purpose": FRED_PURPOSE, "bls_purpose": BLS_PURPOSE,
                 "fred_per_invocation_cap": fred_budget.maximum,
                 "bls_per_invocation_cap": bls_budget.maximum}, **fields)


def _isoformat(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _require_aware_utc(value: object) -> datetime:
    if (type(value) is not datetime or value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset().total_seconds() != 0):
        raise ValueError("aware_utc_required")
    return value


# -- offline preflight (§8.2) ------------------------------------------------


def credential_metadata_v1(path: Path) -> dict:
    """Metadata only: mode, ownership and size. The value is never read."""
    try:
        info = os.lstat(path)
    except OSError:
        return {"present": False, "safe": False, "reason": "absent"}
    uid = os.getuid()
    safe = (stat.S_ISREG(info.st_mode) and not stat.S_ISLNK(info.st_mode)
            and stat.S_IMODE(info.st_mode) == 0o600 and info.st_uid == uid
            and 0 < info.st_size <= MAX_CREDENTIAL_BYTES)
    return {"present": True, "safe": safe, "mode": oct(stat.S_IMODE(info.st_mode)),
            "size_bytes": info.st_size, "owner_match": info.st_uid == uid,
            "reason": None if safe else "credential_unsafe"}


def directory_fingerprint_v1(root: Path) -> dict:
    """A stat and content fingerprint used to prove a root was untouched."""
    if not os.path.lexists(root):
        return {"present": False}
    entries = []
    for path in sorted(Path(root).rglob("*")):
        info = os.lstat(path)
        digest = (hashlib.sha256(path.read_bytes()).hexdigest()
                  if stat.S_ISREG(info.st_mode) else None)
        entries.append({"name": path.relative_to(root).as_posix(),
                        "mode": stat.S_IMODE(info.st_mode),
                        "size": info.st_size, "sha256": digest})
    return {"present": True, "entries": entries,
            "digest": hashlib.sha256(canonical_json(entries)).hexdigest()}


def read_pinned_ics_fixture(path: Path) -> bytes:
    """Return the RFC 5545 wire bytes of the pinned calendar fixture.

    The fixture stores its body with canonical LF so the repository text
    carries no trailing-whitespace artefact, but RFC 5545 is a CRLF format and
    the parser's unfolding rule is defined on CRLF. This reader is the single
    place that reconstructs the wire form, so the parser under test always
    receives exactly the bytes a fetched body would carry.
    """
    raw = Path(path).read_bytes()
    begin = b"<!--BLS-ICS-BEGIN-->\n"
    end = b"<!--BLS-ICS-END-->"
    if begin not in raw or end not in raw:
        raise ValueError("invalid_ics_fixture")
    stored = raw[raw.index(begin) + len(begin):raw.index(end)]
    if b"\r" in stored:
        # Canonical LF storage is an invariant, not a preference: a stored CR
        # would make the reconstruction below ambiguous.
        raise ValueError("noncanonical_ics_fixture_storage")
    return stored.replace(b"\n", b"\r\n")


def run_preflight_v1(*, root: Path, credential: Path, campaign_root: Path,
                     production_sidecar: Path, ics_fixture: Path,
                     clock) -> dict:
    """Prove the production path offline. External starts are structurally 0.

    No transport is constructed here at all, and the fixture-derived schedule
    is confined to the injected working root: a sidecar written into the
    production root would let the later live canary skip real discovery.
    """
    now = _require_aware_utc(clock())
    if production_sidecar.parent == root:
        raise ValueError("preflight_root_conflict")
    campaign_before = directory_fingerprint_v1(campaign_root)

    cache = FredQueryCacheV1(root, opened_at=now)
    database = root / DATABASE_NAME
    reopened = FredQueryCacheV1(root, opened_at=now)
    sidecars = [suffix for suffix in ("-journal", "-wal", "-shm")
                if os.path.lexists(Path(f"{database}{suffix}"))]

    body = read_pinned_ics_fixture(ics_fixture)
    calendar = parse_bls_calendar_v1(body)
    release_date = date(2026, 8, 7)
    official_release_at = select_release_instant_v1(
        calendar, summary=BLS_SUMMARY, local_date=release_date)
    provenance = build_schedule_provenance_v1(
        release_id=50, release_date=release_date,
        official_release_at=official_release_at,
        body_sha256=calendar.body_sha256, body_bytes=calendar.body_bytes)
    schedule = build_fred_release_schedule_v1(
        series_id=SERIES_PAYEMS, release_id=50, release_date=release_date,
        official_release_at=official_release_at,
        schedule_provenance=provenance)
    working_sidecar = root / SIDECAR_NAME
    write_schedule_sidecar_v1(working_sidecar, build_sidecar_payload_v1(
        schedule=schedule, release_id=50, release_date=release_date,
        official_release_at=official_release_at, provenance=provenance))
    reloaded = load_schedule_sidecar_v1(working_sidecar, now=now)
    query, expected_terminal_month = derive_query_window_v1(
        release_date=release_date, as_of=now)

    return {
        "code": "preflight_ok",
        "root": str(root),
        "fred_starts": 0,
        "bls_starts": 0,
        "observation_starts": 0,
        "credential_reads": 0,
        "credential": credential_metadata_v1(credential),
        "root_mode": oct(stat.S_IMODE(os.lstat(root).st_mode)),
        "database_mode": oct(stat.S_IMODE(os.lstat(database).st_mode)),
        "sqlite_sidecars": sidecars,
        "reopen_success": (cache.path == reopened.path
                           and reopened.read_success_for_series(
                               SERIES_PAYEMS, now=now) is None),
        "campaign_root_unchanged": (campaign_before
                                    == directory_fingerprint_v1(campaign_root)),
        "production_sidecar": str(production_sidecar),
        "production_sidecar_absent": not os.path.lexists(production_sidecar),
        "bls_body_sha256": calendar.body_sha256,
        "bls_body_bytes": calendar.body_bytes,
        "official_release_at": format_instant_v1(official_release_at),
        "eligible_from": format_instant_v1(schedule.eligible_from),
        "release_slot": schedule.release_slot,
        "schedule_provenance_digest": schedule.provenance_digest,
        "sidecar_reload_matches": (reloaded is not None
                                   and reloaded.schedule == schedule
                                   and not reloaded.expired),
        "sidecar_mode": oct(stat.S_IMODE(os.lstat(working_sidecar).st_mode)),
        "expected_terminal_month": list(expected_terminal_month),
        "observation_start": query.observation_start.isoformat(),
        "observation_end": query.observation_end.isoformat(),
        "observation_limit": observation_limit(query),
    }


def run_readback_v1(*, root: Path, clock) -> dict:
    """Offline read of what a previous invocation settled. Starts stay 0."""
    now = _require_aware_utc(clock())
    cache = FredQueryCacheV1(root, opened_at=now)
    stored = cache.read_success_for_series(SERIES_PAYEMS, now=now)
    sidecar = root / SIDECAR_NAME
    attempts = []
    coverage = None
    success = None
    if stored is not None:
        success = {"query_identity": stored.query_identity,
                   "revision_identity": stored.revision_identity,
                   "due_slot": stored.due_slot,
                   "fetched_at": format_instant_v1(stored.fetched_at),
                   "observations": len(stored.fact.observations),
                   "newest_observation_date":
                       stored.fact.observations[-1].observation_date.isoformat()}
        coverage = FredCoverageV1.USED.value
        attempts = [{"attempt_seq": row[0], "state": row[1],
                     "started_at": row[2], "finished_at": row[3],
                     "failure_code": row[4]}
                    for row in cache.attempt_rows(stored.query_identity,
                                                  stored.due_slot)]
    return {"code": "readback_ok", "root": str(root), "fred_starts": 0,
            "bls_starts": 0, "observation_starts": 0, "coverage": coverage,
            "success": success, "attempts": attempts,
            "observation_attempt_total": cache.count_observation_attempts_v1(),
            "sidecar_present": os.path.lexists(sidecar),
            "sidecar_mode": (oct(stat.S_IMODE(os.lstat(sidecar).st_mode))
                             if os.path.lexists(sidecar) else None)}


# -- CLI ---------------------------------------------------------------------


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("subcommand",
                        choices=("preflight", "canary", "readback"))
    parser.add_argument("--root", default=None)
    parser.add_argument("--credential", default=None)
    parser.add_argument("--bls-ua-credential", default=None)
    parser.add_argument("--campaign-root", default=None)
    parser.add_argument("--production-sidecar", default=None)
    parser.add_argument("--ics-fixture", default=None)
    parser.add_argument(EXECUTE_FLAG, dest="execute", action="store_true")
    parser.add_argument("--rediscover-schedule", action="store_true")
    return parser


def run_cli(argv, *, clock, api_key_loader, bls_ua_loader=load_bls_user_agent) -> int:
    try:
        args = _parser().parse_args(argv)
    except SystemExit:
        return 2
    if args.subcommand == "canary" and not args.execute:
        # D2: the gate is checked before any credential value read, any
        # external request and any production persistence.
        print(json.dumps({"code": "ondemand_forbidden", "fred_starts": 0,
                          "bls_starts": 0, "observation_starts": 0,
                          "credential_reads": 0,
                          "required_flag": EXECUTE_FLAG}, sort_keys=True))
        return 2
    if args.root is None:
        print(json.dumps({"code": "explicit_root_required"}, sort_keys=True))
        return 2
    root = Path(args.root)
    if args.subcommand == "preflight":
        for name in ("credential", "campaign_root", "production_sidecar",
                     "ics_fixture"):
            if getattr(args, name) is None:
                print(json.dumps({"code": "explicit_preflight_paths_required",
                                  "missing": name}, sort_keys=True))
                return 2
        result = run_preflight_v1(
            root=root, credential=Path(args.credential),
            campaign_root=Path(args.campaign_root),
            production_sidecar=Path(args.production_sidecar),
            ics_fixture=Path(args.ics_fixture), clock=clock)
    elif args.subcommand == "readback":
        result = run_readback_v1(root=root, clock=clock)
    else:
        paths = ondemand_paths()
        credential = (Path(args.credential) if args.credential is not None
                      else paths.credential)
        bls_ua_path = (Path(args.bls_ua_credential)
                       if args.bls_ua_credential is not None
                       else credential.parent / "bls_ua.env")
        try:
            bls_user_agent = bls_ua_loader(bls_ua_path)
        except ValueError:
            print(json.dumps({"code": "bls_user_agent_unavailable",
                              "fred_starts": 0, "bls_starts": 0,
                              "observation_starts": 0,
                              "credential_reads": 0}, sort_keys=True))
            return 2
        result = run_canary_v1(
            paths=paths_for_root(root, credential=credential),
            fred_transport=FredLiveTransport(opener=build_live_opener()),
            bls_transport=BlsIcsTransport(opener=build_bls_opener(),
                                          user_agent=bls_user_agent),
            api_key_provider=lambda: api_key_loader(credential), clock=clock,
            rediscover_schedule=args.rediscover_schedule)
    print(json.dumps(result, sort_keys=True))
    return 0 if result.get("code") in ("preflight_ok", "readback_ok",
                                       "enrichment_complete") else 1


def main() -> int:
    return run_cli(sys.argv[1:],
                   clock=lambda: datetime.now(timezone.utc),
                   api_key_loader=load_fred_api_key)


if __name__ == "__main__":
    raise SystemExit(main())
