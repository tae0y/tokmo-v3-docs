"""Durable scheduler tick receipts for the production cadence (SDS §9).

One canonical STRICT SQLite row per ``(policy_version, scheduled_for_utc)``
slot: scheduled/observed/started/finished times, lag against the pinned
policy's maximum, per-stage status/latency, wire counts by operation and
group, the mask digest with open markets and market-local dates, calendar
attempt identities, and the terminal status/reason. MISSED/DUPLICATE/OVERLAP
occurrences land in a separate events log so a preserved terminal receipt is
never rewritten. No secret, account scope, symbol, quantity, URL, or raw
exception text is ever stored here.
"""

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat
from typing import Mapping, Sequence

from tokmo.values.material_pack import canonical_decode, canonical_encode
from tokmo.values.ops_kernel import iso, parse_iso

TICK_STATUS_TOKENS = frozenset({
    "STARTED", "COMPLETE", "SKIPPED_CLOSED", "PARTIAL", "FAILED",
    "MISSED", "UNKNOWN",
})
TERMINAL_STATUS_TOKENS = TICK_STATUS_TOKENS - {"STARTED"}
EVENT_KIND_TOKENS = frozenset({"MISSED", "DUPLICATE", "OVERLAP"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cadence_tick_receipts(
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  tick_id INTEGER NOT NULL CHECK(tick_id >= 0),
  status TEXT NOT NULL CHECK(status IN
    ('STARTED','COMPLETE','SKIPPED_CLOSED','PARTIAL','FAILED','MISSED','UNKNOWN')),
  reason TEXT,
  observed_start TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  lag_seconds REAL NOT NULL CHECK(lag_seconds >= 0.0),
  max_lag_seconds INTEGER NOT NULL CHECK(max_lag_seconds >= 0),
  mask_digest TEXT CHECK(mask_digest IS NULL OR length(mask_digest)=64),
  open_markets_json TEXT,
  local_dates_json TEXT,
  stages_json TEXT,
  wire_counts_json TEXT,
  calendar_attempts_json TEXT,
  PRIMARY KEY(policy_version, scheduled_for_utc)
) STRICT;
CREATE TABLE IF NOT EXISTS cadence_tick_events(
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq >= 0),
  kind TEXT NOT NULL CHECK(kind IN ('MISSED','DUPLICATE','OVERLAP')),
  observed_at TEXT NOT NULL,
  PRIMARY KEY(policy_version, scheduled_for_utc, seq)
) STRICT;
"""


class CadenceTickReceiptStoreError(ValueError):
    pass


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise CadenceTickReceiptStoreError(f"{name} must be an aware datetime")


def _json(value) -> str:
    return canonical_encode(value).decode()


def _decode(value):
    if value is None:
        return None
    return canonical_decode(value.encode(), max_bytes=256 * 1024)


class CadenceTickReceiptStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                value = os.lstat(self.path)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise CadenceTickReceiptStoreError("unsafe receipt store")
                os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            connection.executescript(_SCHEMA)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _slot(policy_version: str, scheduled_for_utc: datetime) -> tuple[str, str]:
        if type(policy_version) is not str or not policy_version:
            raise CadenceTickReceiptStoreError("invalid policy version")
        _aware(scheduled_for_utc, "scheduled_for_utc")
        return policy_version, iso(scheduled_for_utc)

    def begin_tick(self, policy_version: str, scheduled_for_utc: datetime,
                   tick_id: int, *, observed_start: datetime,
                   lag_seconds: float, max_lag_seconds: int) -> str:
        """Insert the slot's STARTED row; an existing row means duplicate."""
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        _aware(observed_start, "observed_start")
        if type(tick_id) is not int or tick_id < 0:
            raise CadenceTickReceiptStoreError("invalid tick id")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM cadence_tick_receipts "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (version, slot_iso),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO cadence_tick_receipts("
                "policy_version,scheduled_for_utc,tick_id,status,reason,"
                "observed_start,started_at,finished_at,lag_seconds,"
                "max_lag_seconds,mask_digest,open_markets_json,"
                "local_dates_json,stages_json,wire_counts_json,"
                "calendar_attempts_json) "
                "VALUES(?,?,?,'STARTED',NULL,?,?,NULL,?,?,NULL,NULL,NULL,"
                "NULL,NULL,NULL)",
                (version, slot_iso, tick_id, iso(observed_start),
                 iso(self._clock()), float(lag_seconds), int(max_lag_seconds)),
            )
            return "created"

    def record_missed(self, policy_version: str, scheduled_for_utc: datetime,
                      tick_id: int, *, observed_start: datetime,
                      lag_seconds: float, max_lag_seconds: int) -> None:
        """A lag-excess slot is terminal MISSED without ever starting."""
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        _aware(observed_start, "observed_start")
        now = iso(self._clock())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO cadence_tick_receipts("
                "policy_version,scheduled_for_utc,tick_id,status,reason,"
                "observed_start,started_at,finished_at,lag_seconds,"
                "max_lag_seconds,mask_digest,open_markets_json,"
                "local_dates_json,stages_json,wire_counts_json,"
                "calendar_attempts_json) "
                "VALUES(?,?,?,'MISSED','excess_start_lag',?,?,?,?,?,NULL,"
                "NULL,NULL,NULL,NULL,NULL)",
                (version, slot_iso, tick_id, iso(observed_start), now, now,
                 float(lag_seconds), int(max_lag_seconds)),
            )
            self._append_event(connection, version, slot_iso, "MISSED")

    def _append_event(self, connection, version, slot_iso, kind) -> None:
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq)+1,0) FROM cadence_tick_events "
            "WHERE policy_version=? AND scheduled_for_utc=?",
            (version, slot_iso),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO cadence_tick_events VALUES(?,?,?,?,?)",
            (version, slot_iso, seq, kind, iso(self._clock())),
        )

    def record_event(self, policy_version: str, scheduled_for_utc: datetime,
                     kind: str) -> None:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        if kind not in EVENT_KIND_TOKENS:
            raise CadenceTickReceiptStoreError("unknown tick event kind")
        with self._connect() as connection:
            self._append_event(connection, version, slot_iso, kind)

    def finish_tick(self, policy_version: str, scheduled_for_utc: datetime, *,
                    status: str, reason: str | None, mask_digest: str | None,
                    open_markets: Sequence[str],
                    local_dates: Mapping[str, str],
                    stages: Sequence[Mapping],
                    wire_counts: Mapping,
                    calendar_attempts: Sequence[str],
                    finished_at: datetime) -> None:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        if status not in TERMINAL_STATUS_TOKENS:
            raise CadenceTickReceiptStoreError("invalid terminal tick status")
        _aware(finished_at, "finished_at")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM cadence_tick_receipts "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (version, slot_iso),
            ).fetchone()
            if row is None or row["status"] != "STARTED":
                raise CadenceTickReceiptStoreError(
                    "terminal receipt requires the slot's STARTED row"
                )
            connection.execute(
                "UPDATE cadence_tick_receipts SET status=?,reason=?,"
                "finished_at=?,mask_digest=?,open_markets_json=?,"
                "local_dates_json=?,stages_json=?,wire_counts_json=?,"
                "calendar_attempts_json=? "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (status, reason, iso(finished_at), mask_digest,
                 _json(list(open_markets)), _json(dict(local_dates)),
                 _json([dict(stage) for stage in stages]),
                 _json(dict(wire_counts)), _json(list(calendar_attempts)),
                 version, slot_iso),
            )

    def reconcile_stale(self, *, now: datetime,
                        exclude: tuple[str, datetime]) -> tuple[dict, ...]:
        """Settle every stale STARTED slot (crashed invocation) to UNKNOWN."""
        _aware(now, "now")
        exclude_version, exclude_iso = self._slot(*exclude)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT policy_version,scheduled_for_utc FROM "
                "cadence_tick_receipts WHERE status='STARTED' "
                "AND NOT (policy_version=? AND scheduled_for_utc=?) "
                "ORDER BY policy_version,scheduled_for_utc",
                (exclude_version, exclude_iso),
            ).fetchall()
            reconciled = []
            for row in rows:
                connection.execute(
                    "UPDATE cadence_tick_receipts SET status='UNKNOWN',"
                    "reason='reconciled_stale_started',finished_at=? "
                    "WHERE policy_version=? AND scheduled_for_utc=?",
                    (iso(now), row["policy_version"], row["scheduled_for_utc"]),
                )
                reconciled.append({
                    "policy_version": row["policy_version"],
                    "scheduled_for_utc": parse_iso(row["scheduled_for_utc"]),
                })
            return tuple(reconciled)

    def read_tick(self, policy_version: str,
                  scheduled_for_utc: datetime) -> dict | None:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM cadence_tick_receipts "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (version, slot_iso),
            ).fetchone()
        if row is None:
            return None
        return {
            "policy_version": row["policy_version"],
            "scheduled_for_utc": parse_iso(row["scheduled_for_utc"]),
            "tick_id": row["tick_id"],
            "status": row["status"],
            "reason": row["reason"],
            "observed_start": parse_iso(row["observed_start"]),
            "started_at": parse_iso(row["started_at"]),
            "finished_at": parse_iso(row["finished_at"]),
            "lag_seconds": row["lag_seconds"],
            "max_lag_seconds": row["max_lag_seconds"],
            "mask_digest": row["mask_digest"],
            "open_markets": _decode(row["open_markets_json"]),
            "local_dates": _decode(row["local_dates_json"]),
            "stages": _decode(row["stages_json"]),
            "wire_counts": _decode(row["wire_counts_json"]),
            "calendar_attempts": _decode(row["calendar_attempts_json"]),
        }

    def read_events(self, policy_version: str,
                    scheduled_for_utc: datetime) -> tuple[dict, ...]:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM cadence_tick_events "
                "WHERE policy_version=? AND scheduled_for_utc=? ORDER BY seq",
                (version, slot_iso),
            ).fetchall()
        return tuple({
            "seq": row["seq"], "kind": row["kind"],
            "observed_at": parse_iso(row["observed_at"]),
        } for row in rows)
