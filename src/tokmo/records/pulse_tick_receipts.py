"""Durable Pulse tick receipts and the Pulse wire ledger (Plan §7.3/§11).

Physically separate from ``cadence_tick_receipts`` so the 300s receipt
semantics are never polluted: one STRICT row per
``(policy_version, scheduled_for_utc)`` Pulse slot with terminal states
SAVED/PARTIAL/FAILED/MISSED/OVERLAP/SKIPPED_CLOSED (plus STARTED in flight
and UNKNOWN after crash reconcile), a MISSED/DUPLICATE/OVERLAP event log,
and a per-tick wire-attempt ledger that carries the crash-boundary
STARTED→SETTLED/UNKNOWN semantics and the rolling five-minute telemetry.
No secret, account scope, symbol, or raw exception text is stored here.
"""

from datetime import datetime
import os
from pathlib import Path
import sqlite3
import stat
from typing import Mapping, Sequence

from tokmo.values.cadence_policy import validate_cadence_operation
from tokmo.values.material_pack import canonical_decode, canonical_encode
from tokmo.values.ops_kernel import iso, parse_iso
from tokmo.values.pulse_policy import PULSE_OPERATIONS

PULSE_TICK_STATUS_TOKENS = frozenset({
    "STARTED", "SAVED", "PARTIAL", "FAILED", "MISSED", "OVERLAP",
    "SKIPPED_CLOSED", "UNKNOWN",
})
PULSE_TERMINAL_STATUS_TOKENS = PULSE_TICK_STATUS_TOKENS - {"STARTED"}
PULSE_EVENT_KIND_TOKENS = frozenset({"MISSED", "DUPLICATE", "OVERLAP"})

# One body shared by bootstrap and the one-time CHECK-widening rebuild so
# the two can never diverge. CONDITIONAL_ORDERS joined the closed pulse
# operation set with the conditional-order SDS v0.4 §4.4 registration.
_WIRE_ATTEMPTS_BODY = """(
  policy_version TEXT NOT NULL,
  tick_id INTEGER NOT NULL CHECK(tick_id >= 0),
  seq INTEGER NOT NULL CHECK(seq >= 0),
  operation TEXT NOT NULL CHECK(operation IN
    ('TOKEN','PRICE_BATCH','ORDERS_PAGE','ORDER_DETAIL',
     'CONDITIONAL_ORDERS')),
  native_identity TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('STARTED','SETTLED','UNKNOWN')),
  started_at TEXT NOT NULL,
  settled_at TEXT,
  PRIMARY KEY(policy_version, tick_id, seq)
) STRICT"""

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS pulse_tick_receipts(
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  tick_id INTEGER NOT NULL CHECK(tick_id >= 0),
  status TEXT NOT NULL CHECK(status IN
    ('STARTED','SAVED','PARTIAL','FAILED','MISSED','OVERLAP',
     'SKIPPED_CLOSED','UNKNOWN')),
  reason TEXT,
  observed_start TEXT NOT NULL,
  started_at TEXT NOT NULL,
  finished_at TEXT,
  lag_seconds REAL NOT NULL CHECK(lag_seconds >= 0.0),
  max_lag_seconds INTEGER NOT NULL CHECK(max_lag_seconds >= 0),
  stages_json TEXT,
  wire_counts_json TEXT,
  PRIMARY KEY(policy_version, scheduled_for_utc)
) STRICT;
CREATE TABLE IF NOT EXISTS pulse_tick_events(
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  seq INTEGER NOT NULL CHECK(seq >= 0),
  kind TEXT NOT NULL CHECK(kind IN ('MISSED','DUPLICATE','OVERLAP')),
  observed_at TEXT NOT NULL,
  PRIMARY KEY(policy_version, scheduled_for_utc, seq)
) STRICT;
CREATE TABLE IF NOT EXISTS pulse_wire_attempts{_WIRE_ATTEMPTS_BODY};
"""


class PulseTickReceiptStoreError(ValueError):
    pass


def _aware(value: datetime, name: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise PulseTickReceiptStoreError(f"{name} must be an aware datetime")


def _json(value) -> str:
    return canonical_encode(value).decode()


def _decode(value):
    if value is None:
        return None
    return canonical_decode(value.encode(), max_bytes=256 * 1024)


class PulseTickReceiptStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self, *, opportunity=False) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                value = os.lstat(self.path)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise PulseTickReceiptStoreError("unsafe receipt store")
                os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            self._widen_wire_operations(connection, opportunity=opportunity)
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _widen_wire_operations(connection: sqlite3.Connection, *, opportunity=False) -> None:
        """One-time additive rebuild for pre-conditional databases.

        ``CREATE TABLE IF NOT EXISTS`` cannot widen the operation CHECK of
        an existing ``pulse_wire_attempts``, so a database created before
        CONDITIONAL_ORDERS joined the closed set is rebuilt once — copy,
        drop, rename — inside a single transaction (SQLite DDL is
        transactional, so a crash leaves either the old or the new table,
        never neither). Rows are preserved verbatim; identical columns.
        """
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' "
            "AND name='pulse_wire_attempts'",
        ).fetchone()
        if row is None or ("CONDITIONAL_ORDERS" in row["sql"] and (not opportunity or "OPPORTUNITY_READ" in row["sql"] and "QUANT_READ" in row["sql"])):
            return
        body = _WIRE_ATTEMPTS_BODY
        if opportunity:
            body = body.replace("'CONDITIONAL_ORDERS'", "'CONDITIONAL_ORDERS','OPPORTUNITY_READ','QUANT_READ'")
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                f"CREATE TABLE pulse_wire_attempts_widened{body}")
            connection.execute(
                "INSERT INTO pulse_wire_attempts_widened "
                "SELECT * FROM pulse_wire_attempts")
            connection.execute("DROP TABLE pulse_wire_attempts")
            connection.execute(
                "ALTER TABLE pulse_wire_attempts_widened "
                "RENAME TO pulse_wire_attempts")
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    @staticmethod
    def _slot(policy_version: str, scheduled_for_utc: datetime) -> tuple[str, str]:
        if type(policy_version) is not str or not policy_version:
            raise PulseTickReceiptStoreError("invalid policy version")
        _aware(scheduled_for_utc, "scheduled_for_utc")
        return policy_version, iso(scheduled_for_utc)

    def begin_tick(self, policy_version: str, scheduled_for_utc: datetime,
                   tick_id: int, *, observed_start: datetime,
                   lag_seconds: float, max_lag_seconds: int) -> str:
        """Insert the slot's STARTED row; an existing row means duplicate."""
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        _aware(observed_start, "observed_start")
        if type(tick_id) is not int or tick_id < 0:
            raise PulseTickReceiptStoreError("invalid tick id")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT status FROM pulse_tick_receipts "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (version, slot_iso),
            ).fetchone()
            if existing is not None:
                self._append_event(connection, version, slot_iso, "DUPLICATE")
                return "duplicate"
            connection.execute(
                "INSERT INTO pulse_tick_receipts("
                "policy_version,scheduled_for_utc,tick_id,status,reason,"
                "observed_start,started_at,finished_at,lag_seconds,"
                "max_lag_seconds,stages_json,wire_counts_json) "
                "VALUES(?,?,?,'STARTED',NULL,?,?,NULL,?,?,NULL,NULL)",
                (version, slot_iso, tick_id, iso(observed_start),
                 iso(self._clock()), float(lag_seconds), int(max_lag_seconds)),
            )
            return "created"

    def _record_terminal_slot(self, status, reason, policy_version,
                              scheduled_for_utc, tick_id, observed_start,
                              lag_seconds, max_lag_seconds) -> None:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        _aware(observed_start, "observed_start")
        now = iso(self._clock())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO pulse_tick_receipts("
                "policy_version,scheduled_for_utc,tick_id,status,reason,"
                "observed_start,started_at,finished_at,lag_seconds,"
                "max_lag_seconds,stages_json,wire_counts_json) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,NULL,NULL)",
                (version, slot_iso, tick_id, status, reason,
                 iso(observed_start), now, now, float(lag_seconds),
                 int(max_lag_seconds)),
            )
            self._append_event(connection, version, slot_iso, status)

    def record_missed(self, policy_version: str, scheduled_for_utc: datetime,
                      tick_id: int, *, observed_start: datetime,
                      lag_seconds: float, max_lag_seconds: int) -> None:
        """Lag excess is terminal MISSED; the slot is never caught up (§11)."""
        self._record_terminal_slot(
            "MISSED", "excess_start_lag", policy_version, scheduled_for_utc,
            tick_id, observed_start, lag_seconds, max_lag_seconds,
        )

    def record_overlap(self, policy_version: str, scheduled_for_utc: datetime,
                       tick_id: int, *, observed_start: datetime,
                       lag_seconds: float, max_lag_seconds: int) -> None:
        """Lease contention terminal: provider I/O zero for this slot."""
        self._record_terminal_slot(
            "OVERLAP", "lease_held", policy_version, scheduled_for_utc,
            tick_id, observed_start, lag_seconds, max_lag_seconds,
        )

    def _append_event(self, connection, version, slot_iso, kind) -> None:
        if kind not in PULSE_EVENT_KIND_TOKENS:
            return
        seq = connection.execute(
            "SELECT COALESCE(MAX(seq)+1,0) FROM pulse_tick_events "
            "WHERE policy_version=? AND scheduled_for_utc=?",
            (version, slot_iso),
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO pulse_tick_events VALUES(?,?,?,?,?)",
            (version, slot_iso, seq, kind, iso(self._clock())),
        )

    def finish_tick(self, policy_version: str, scheduled_for_utc: datetime, *,
                    status: str, reason: "str | None",
                    stages: Sequence[Mapping],
                    wire_counts: Mapping,
                    finished_at: datetime) -> None:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        if status not in ("SAVED", "PARTIAL", "FAILED", "SKIPPED_CLOSED"):
            raise PulseTickReceiptStoreError("invalid terminal tick status")
        _aware(finished_at, "finished_at")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM pulse_tick_receipts "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (version, slot_iso),
            ).fetchone()
            if row is None or row["status"] != "STARTED":
                raise PulseTickReceiptStoreError(
                    "terminal receipt requires the slot's STARTED row")
            connection.execute(
                "UPDATE pulse_tick_receipts SET status=?,reason=?,"
                "finished_at=?,stages_json=?,wire_counts_json=? "
                "WHERE policy_version=? AND scheduled_for_utc=?",
                (status, reason, iso(finished_at),
                 _json([dict(stage) for stage in stages]),
                 _json(dict(wire_counts)), version, slot_iso),
            )

    def reconcile_stale(self, *, now: datetime,
                        exclude: tuple[str, datetime]) -> tuple[dict, ...]:
        """Settle crashed STARTED slots and their wires to UNKNOWN (§11)."""
        _aware(now, "now")
        exclude_version, exclude_iso = self._slot(*exclude)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT policy_version,scheduled_for_utc,tick_id FROM "
                "pulse_tick_receipts WHERE status='STARTED' "
                "AND NOT (policy_version=? AND scheduled_for_utc=?) "
                "ORDER BY policy_version,scheduled_for_utc",
                (exclude_version, exclude_iso),
            ).fetchall()
            reconciled = []
            for row in rows:
                connection.execute(
                    "UPDATE pulse_tick_receipts SET status='UNKNOWN',"
                    "reason='reconciled_stale_started',finished_at=? "
                    "WHERE policy_version=? AND scheduled_for_utc=?",
                    (iso(now), row["policy_version"], row["scheduled_for_utc"]),
                )
                connection.execute(
                    "UPDATE pulse_wire_attempts SET status='UNKNOWN',"
                    "settled_at=? WHERE policy_version IN (?,?) AND tick_id=? "
                    "AND status='STARTED'",
                    (iso(now), row["policy_version"], row["policy_version"]+':inputs-v1', row["tick_id"]),
                )
                reconciled.append({
                    "policy_version": row["policy_version"],
                    "scheduled_for_utc": parse_iso(row["scheduled_for_utc"]),
                    "tick_id": row["tick_id"],
                })
            return tuple(reconciled)

    # --- wire ledger -----------------------------------------------------

    def start_wire(self, policy_version: str, tick_id: int, operation: str,
                   native_identity: str) -> int:
        """Durably record one wire start before provider I/O; returns seq."""
        if operation not in PULSE_OPERATIONS and operation not in ("OPPORTUNITY_READ", "QUANT_READ"):
            raise PulseTickReceiptStoreError("operation is not pulse-expressible")
        if operation in ('OPPORTUNITY_READ', 'QUANT_READ'):
            if len(native_identity) != 64 or any(c not in '0123456789abcdef' for c in native_identity):
                raise PulseTickReceiptStoreError('public capture identity must be a request hash')
        else:
            validate_cadence_operation(operation, native_identity)
        if type(policy_version) is not str or not policy_version:
            raise PulseTickReceiptStoreError("invalid policy version")
        if type(tick_id) is not int or tick_id < 0:
            raise PulseTickReceiptStoreError("invalid tick id")
        optional = operation in ('OPPORTUNITY_READ', 'QUANT_READ')
        base_version = policy_version
        if optional:
            policy_version += ':inputs-v1'
        with self._connect(opportunity=optional) as connection:
            seq = connection.execute(
                "SELECT COALESCE(MAX(seq)+1,0) FROM pulse_wire_attempts "
                "WHERE policy_version IN (?,?) AND tick_id=?",
                (base_version, base_version+':inputs-v1', tick_id),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO pulse_wire_attempts VALUES(?,?,?,?,?, "
                "'STARTED',?,NULL)",
                (policy_version, tick_id, seq, operation, native_identity,
                 iso(self._clock())),
            )
            return seq

    def settle_wire(self, policy_version: str, tick_id: int, seq: int) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM pulse_wire_attempts WHERE "
                "policy_version IN (?,?) AND tick_id=? AND seq=?",
                (policy_version, policy_version+':inputs-v1', tick_id, seq),
            ).fetchone()
            if row is None or row["status"] != "STARTED":
                raise PulseTickReceiptStoreError(
                    "settle requires the wire's STARTED row")
            connection.execute(
                "UPDATE pulse_wire_attempts SET status='SETTLED',settled_at=? "
                "WHERE policy_version IN (?,?) AND tick_id=? AND seq=?",
                (iso(self._clock()), policy_version, policy_version+':inputs-v1', tick_id, seq),
            )

    def wire_counts(self, policy_version: str, tick_id: int) -> dict:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT operation, COUNT(*) FROM pulse_wire_attempts "
                "WHERE policy_version IN (?,?) AND tick_id=? GROUP BY operation",
                (policy_version, policy_version+':inputs-v1', tick_id),
            ).fetchall()
        counts = {operation: 0 for operation in PULSE_OPERATIONS}
        for operation, count in rows:
            counts[operation] = count
        counts["total"] = sum(counts.values())
        return counts

    def read_wires(self, policy_version: str, tick_id: int) -> tuple[dict, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT seq, operation, native_identity, status FROM "
                "pulse_wire_attempts WHERE policy_version IN (?,?) AND tick_id=? "
                "ORDER BY seq",
                (policy_version, policy_version+':inputs-v1', tick_id),
            ).fetchall()
        return tuple({
            "seq": row["seq"], "operation": row["operation"],
            "native_identity": row["native_identity"], "status": row["status"],
        } for row in rows)

    def rolling_wire_starts(self, policy_version: str, *,
                            now_epoch: int, window_seconds: int = 300, core_only=False) -> int:
        """Total wire starts whose slot lies in [now−window, now) (§12.3)."""
        if type(now_epoch) is not int or type(window_seconds) is not int \
                or window_seconds <= 0:
            raise PulseTickReceiptStoreError("invalid rolling window")
        low_tick = (now_epoch - window_seconds) // 60 + 1
        high_tick = now_epoch // 60
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM pulse_wire_attempts WHERE "
                "policy_version IN (?,?) AND tick_id BETWEEN ? AND ?",
                (policy_version, policy_version if core_only else policy_version+':inputs-v1', low_tick, high_tick),
            ).fetchone()
        return row[0]

    # --- reads -----------------------------------------------------------

    def read_tick(self, policy_version: str,
                  scheduled_for_utc: datetime) -> "dict | None":
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pulse_tick_receipts "
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
            "stages": _decode(row["stages_json"]),
            "wire_counts": _decode(row["wire_counts_json"]),
        }

    def read_events(self, policy_version: str,
                    scheduled_for_utc: datetime) -> tuple[dict, ...]:
        version, slot_iso = self._slot(policy_version, scheduled_for_utc)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pulse_tick_events "
                "WHERE policy_version=? AND scheduled_for_utc=? ORDER BY seq",
                (version, slot_iso),
            ).fetchall()
        return tuple({
            "seq": row["seq"], "kind": row["kind"],
            "observed_at": parse_iso(row["observed_at"]),
        } for row in rows)
