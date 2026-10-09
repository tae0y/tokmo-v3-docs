"""Private SQLite ledger for the bulk-triggered KR fundamentals enrichment.

This store is an operational request/idempotency and evidence ledger
(fundamentals SDS §6): trigger settlement, write-ahead provider-start
accounting, and sanitized official rows kept as provenance-bearing evidence.
It is explicitly NOT a canonical fundamental-materials authority; canonical
KR materials, when unpaused, belong to the existing fundamental-material
store only.

The file is created solely by the explicit ``enrich-init`` operator command,
never as a side effect of a tick. Until it exists and ``enabled=1``, every
natural tick is inactive with provider starts 0 and zero writes.
"""

import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path

from tokmo.records.private_sqlite import (
    connect_private,
    prepare_private_path,
    private_creation,
)

SCHEMA_VERSION = "1"
DEFAULT_CUMULATIVE_START_CAP = 10

STATUS_IN_ATTEMPT = "IN_ATTEMPT"
STATUS_SUCCESS = "SUCCESS"
STATUS_NO_DATA = "NO_DATA"
STATUS_FAILED_RETRYABLE = "FAILED_RETRYABLE"
STATUS_FAILED_EXHAUSTED = "FAILED_EXHAUSTED"
STATUS_FAILED_ROW_BOUND = "FAILED_ROW_BOUND"
STATUS_SKIPPED_YEAR_UNPARSEABLE = "SKIPPED_YEAR_UNPARSEABLE"
STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR = "SKIPPED_NONSTANDARD_FISCAL_YEAR"

# SDS §6 (M-2) normative closed sets. "Terminal" always means exactly the
# first set: once a report identity rests there, provider starts are 0
# forever. NO_DATA and FAILED_RETRYABLE remain re-attempt eligible under the
# §5 attempt/spacing bounds; IN_ATTEMPT is transient and reconciled (§7).
TERMINAL_STATUSES = frozenset(
    {
        STATUS_SUCCESS,
        STATUS_FAILED_EXHAUSTED,
        STATUS_FAILED_ROW_BOUND,
        STATUS_SKIPPED_YEAR_UNPARSEABLE,
        STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR,
    }
)
NON_TERMINAL_STATUSES = frozenset(
    {
        STATUS_NO_DATA,
        STATUS_FAILED_RETRYABLE,
        STATUS_IN_ATTEMPT,
    }
)
REPORT_STATUSES = TERMINAL_STATUSES | NON_TERMINAL_STATUSES

RESERVED_OUTCOME = "RESERVED"
STALE_CRASH_OUTCOME = "stale_crash"
# Closed start-settlement vocabulary: the pinned transport failure codes,
# the body dispositions, and exact provider status stops.
START_OUTCOMES = frozenset(
    {
        "success",
        "schema_drift",
        "request_identity_disagreement",
        STALE_CRASH_OUTCOME,
        "timeout",
        "connection_failure",
        "protocol_failure",
        "auth_rejection",
        "http_429",
        "redirect",
        "bad_request",
        "http_5xx",
        "body_too_large",
        "unsupported_content_encoding",
    }
)
_PROVIDER_STOP_PATTERN = re.compile(r"provider_\d{3}")

TICK_EVENT_OUTCOMES = frozenset(
    {
        "processed",
        "refused_inputs_unavailable",
        "refused_cap_reached",
        "deferred_deadline",
    }
)

# Schema preconditions (SDS §6 m-6):
# - bsns_year is TEXT holding the exact 4-digit string of the pinned request
#   grammar; it is never an INTEGER.
# - stock_code NOT NULL is safe only because the §3.3 candidate scan filters
#   stock_code IS NOT NULL on the bulk store (whose own column is nullable);
#   that filter is a stated precondition of this table.
SCHEMA = """
CREATE TABLE IF NOT EXISTS meta(
 key TEXT PRIMARY KEY,
 value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS enrichments(
 rcept_no TEXT PRIMARY KEY,
 corp_code TEXT NOT NULL,
 stock_code TEXT NOT NULL, -- guarded by the §3.3 stock_code IS NOT NULL scan
 corp_name TEXT NOT NULL,
 bsns_year TEXT NOT NULL,  -- exact 4-digit request string, never INTEGER
 provenance TEXT NOT NULL,
 status TEXT NOT NULL,
 attempts INTEGER NOT NULL,
 last_attempt_at TEXT,
 last_outcome TEXT,
 updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS provider_starts(
 start_id INTEGER PRIMARY KEY,
 rcept_no TEXT NOT NULL,
 endpoint TEXT NOT NULL,
 request_identity TEXT NOT NULL,
 reserved_at TEXT NOT NULL,
 outcome TEXT NOT NULL,
 body_sha256 TEXT);
CREATE TABLE IF NOT EXISTS fundamental_payload_rows(
 rcept_no TEXT NOT NULL,
 endpoint TEXT NOT NULL,
 ordinal INTEGER NOT NULL,
 row_json TEXT NOT NULL,
 PRIMARY KEY(rcept_no, endpoint, ordinal));
CREATE TABLE IF NOT EXISTS tick_events(
 event_id INTEGER PRIMARY KEY,
 at TEXT NOT NULL,
 outcome TEXT NOT NULL,
 reason TEXT,
 reports_processed INTEGER NOT NULL,
 starts INTEGER NOT NULL);
"""


class FundamentalsStoreError(RuntimeError):
    """Closed-reason enrichment-store contract violation."""


class CapReachedError(FundamentalsStoreError):
    """Write-ahead refusal: the cumulative provider-start cap is spent."""


def _valid_start_outcome(outcome: str) -> bool:
    return outcome in START_OUTCOMES or bool(_PROVIDER_STOP_PATTERN.fullmatch(outcome))


class OpenDartFundamentalsStore:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path, self.connection = path, connection

    @classmethod
    def init_create(cls, path: Path, *, now: datetime):
        """Create the store exactly once; the explicit operator entry point."""
        if os.path.lexists(path):
            raise FundamentalsStoreError("store_exists")
        safety = prepare_private_path(path)
        if safety.unsafe:
            raise FundamentalsStoreError(safety.reason)
        connection = None
        try:
            with private_creation():
                connection = connect_private(path)
            os.chmod(path, 0o600)
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            connection.executemany(
                "insert into meta(key,value) values(?,?)",
                [
                    ("schema_version", SCHEMA_VERSION),
                    ("activation_epoch", now.isoformat()),
                    ("cumulative_start_cap", str(DEFAULT_CUMULATIVE_START_CAP)),
                    ("enabled", "1"),
                    ("created_at", now.isoformat()),
                ],
            )
            connection.execute("COMMIT")
            return cls(path, connection)
        except (OSError, sqlite3.Error) as error:
            if connection is not None:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                connection.close()
            raise FundamentalsStoreError("store_create_failed") from error

    @classmethod
    def open(cls, path: Path):
        return cls._open(path, mode="rw")

    @classmethod
    def open_readonly(cls, path: Path):
        return cls._open(path, mode="ro")

    @classmethod
    def _open(cls, path: Path, *, mode: str):
        if not os.path.lexists(path):
            raise FundamentalsStoreError("store_absent")
        safety = prepare_private_path(path)
        if safety.unsafe:
            raise FundamentalsStoreError(safety.reason)
        try:
            connection = connect_private(path, mode=mode)
        except (OSError, sqlite3.Error) as error:
            raise FundamentalsStoreError("store_open_failed") from error
        store = cls(path, connection)
        if store.meta("schema_version") != SCHEMA_VERSION:
            connection.close()
            raise FundamentalsStoreError("schema_version_disagreement")
        return store

    def close(self):
        self.connection.close()

    # -- meta --------------------------------------------------------------

    def meta(self, key: str) -> str | None:
        row = self.connection.execute(
            "select value from meta where key=?", (key,)
        ).fetchone()
        return None if row is None else row[0]

    def set_meta(self, key: str, value: str) -> None:
        self.connection.execute(
            "insert into meta(key,value) values(?,?) "
            "on conflict(key) do update set value=excluded.value",
            (key, value),
        )

    # -- accounting reads --------------------------------------------------

    def cumulative_starts(self) -> int:
        """Every reservation ever made counts, RESERVED and stale included."""
        return self.connection.execute(
            "select count(*) from provider_starts"
        ).fetchone()[0]

    def report(self, rcept_no: str) -> dict | None:
        row = self.connection.execute(
            "select rcept_no,corp_code,stock_code,corp_name,bsns_year,"
            "provenance,status,attempts,last_attempt_at,last_outcome,"
            "updated_at from enrichments where rcept_no=?",
            (rcept_no,),
        ).fetchone()
        if row is None:
            return None
        names = (
            "rcept_no",
            "corp_code",
            "stock_code",
            "corp_name",
            "bsns_year",
            "provenance",
            "status",
            "attempts",
            "last_attempt_at",
            "last_outcome",
            "updated_at",
        )
        return dict(zip(names, row))

    def is_settled(self, rcept_no: str) -> bool:
        row = self.report(rcept_no)
        return row is not None and row["status"] in TERMINAL_STATUSES

    # -- write paths -------------------------------------------------------

    def _cap_guard(self) -> None:
        cap = int(self.meta("cumulative_start_cap") or "0")
        if self.cumulative_starts() >= cap:
            raise CapReachedError("cap_reached")

    def _reserve(
        self, rcept_no: str, endpoint: str, request_identity: str, now: datetime
    ) -> int:
        self._cap_guard()
        cursor = self.connection.execute(
            "insert into provider_starts"
            "(rcept_no,endpoint,request_identity,reserved_at,outcome) "
            "values(?,?,?,?,?)",
            (rcept_no, endpoint, request_identity, now.isoformat(), RESERVED_OUTCOME),
        )
        return cursor.lastrowid

    def begin_attempt(
        self,
        *,
        rcept_no: str,
        corp_code: str,
        stock_code: str,
        corp_name: str,
        bsns_year: str,
        provenance: str,
        endpoint: str,
        request_identity: str,
        now: datetime,
    ) -> int:
        """SDS §6 Txn A: IN_ATTEMPT upsert plus the first RESERVED start,
        committed together before any wire I/O. A cap refusal rolls the whole
        transaction back, so a refused candidate leaves no report row (m-2)."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "select status from enrichments where rcept_no=?", (rcept_no,)
            ).fetchone()
            if existing is not None and existing[0] in TERMINAL_STATUSES:
                raise FundamentalsStoreError("report_settled")
            self.connection.execute(
                "insert into enrichments(rcept_no,corp_code,stock_code,"
                "corp_name,bsns_year,provenance,status,attempts,"
                "last_attempt_at,last_outcome,updated_at) "
                "values(?,?,?,?,?,?,?,1,?,NULL,?) "
                "on conflict(rcept_no) do update set status=excluded.status,"
                "attempts=attempts+1,last_attempt_at=excluded.last_attempt_at,"
                "updated_at=excluded.updated_at",
                (
                    rcept_no,
                    corp_code,
                    stock_code,
                    corp_name,
                    bsns_year,
                    provenance,
                    STATUS_IN_ATTEMPT,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            start_id = self._reserve(rcept_no, endpoint, request_identity, now)
            self.connection.execute("COMMIT")
            return start_id
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def settle_start(
        self,
        start_id: int,
        outcome: str,
        *,
        body_sha256: str | None = None,
        reserve_next: tuple[str, str, str] | None = None,
        now: datetime | None = None,
    ) -> int | None:
        """SDS §6 Txn B/C: settle one RESERVED start; optionally reserve the
        fallback start in the same transaction. A cap refusal on the fallback
        still commits the settlement, then raises."""
        if not _valid_start_outcome(outcome):
            raise FundamentalsStoreError("unknown_start_outcome")
        if reserve_next is not None and now is None:
            raise FundamentalsStoreError("reservation_time_required")
        self.connection.execute("BEGIN IMMEDIATE")
        cap_refusal = None
        next_id = None
        try:
            row = self.connection.execute(
                "select outcome from provider_starts where start_id=?", (start_id,)
            ).fetchone()
            if row is None or row[0] != RESERVED_OUTCOME:
                raise FundamentalsStoreError("reservation_disagreement")
            self.connection.execute(
                "update provider_starts set outcome=?,body_sha256=? where start_id=?",
                (outcome, body_sha256, start_id),
            )
            if reserve_next is not None:
                try:
                    next_id = self._reserve(*reserve_next, now)
                except CapReachedError as error:
                    cap_refusal = error
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise
        if cap_refusal is not None:
            raise cap_refusal
        return next_id

    def insert_payload_rows(self, rcept_no: str, endpoint: str, payload_rows) -> None:
        """Sanitized rows for one settled report. Participates in the caller's
        transaction; the (rcept_no, endpoint, ordinal) primary key makes an
        accidental duplicate commit refuse loudly (§7)."""
        for ordinal, row_json in enumerate(payload_rows, start=1):
            self.connection.execute(
                "insert into fundamental_payload_rows"
                "(rcept_no,endpoint,ordinal,row_json) values(?,?,?,?)",
                (rcept_no, endpoint, ordinal, row_json),
            )

    def settle_report_terminal(
        self,
        *,
        rcept_no: str,
        status: str,
        last_outcome: str,
        now: datetime,
        corp_code: str | None = None,
        stock_code: str | None = None,
        corp_name: str | None = None,
        bsns_year: str | None = None,
        provenance: str | None = None,
        endpoint: str | None = None,
        payload_rows=(),
    ) -> None:
        """Settle a report out of the attempt path into a resting status.

        SUCCESS commits its payload rows in the same transaction (§6 final
        txn). Statuses in TERMINAL_STATUSES are permanent; NO_DATA and
        FAILED_RETRYABLE rest here only until their §5 re-attempt bound.
        Absent rows (skip classes settled without any attempt) are inserted
        with attempts=0.
        """
        if status not in REPORT_STATUSES or status == STATUS_IN_ATTEMPT:
            raise FundamentalsStoreError("unknown_report_status")
        if status == STATUS_SUCCESS:
            if not payload_rows or endpoint is None:
                raise FundamentalsStoreError("payload_rows_required")
        elif payload_rows:
            raise FundamentalsStoreError("payload_requires_success")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            existing = self.connection.execute(
                "select status from enrichments where rcept_no=?", (rcept_no,)
            ).fetchone()
            if existing is not None and existing[0] in TERMINAL_STATUSES:
                raise FundamentalsStoreError("report_settled")
            if existing is None:
                fields = (corp_code, stock_code, corp_name, bsns_year, provenance)
                if any(value is None for value in fields):
                    raise FundamentalsStoreError("report_fields_required")
                self.connection.execute(
                    "insert into enrichments(rcept_no,corp_code,stock_code,"
                    "corp_name,bsns_year,provenance,status,attempts,"
                    "last_attempt_at,last_outcome,updated_at) "
                    "values(?,?,?,?,?,?,?,0,NULL,?,?)",
                    (rcept_no, *fields, status, last_outcome, now.isoformat()),
                )
            else:
                self.connection.execute(
                    "update enrichments set status=?,last_outcome=?,"
                    "updated_at=? where rcept_no=?",
                    (status, last_outcome, now.isoformat(), rcept_no),
                )
            if status == STATUS_SUCCESS:
                self.insert_payload_rows(rcept_no, endpoint, payload_rows)
            self.connection.execute("COMMIT")
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def reconcile_stale(self, *, now: datetime, stale_before: datetime) -> dict:
        """SDS §7 crash reconcile: RESERVED starts older than the threshold
        settle as stale_crash (still counted toward the cumulative cap) and
        stale IN_ATTEMPT reports become FAILED_RETRYABLE."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            stale_starts = self.connection.execute(
                "update provider_starts set outcome=? "
                "where outcome=? and reserved_at<?",
                (STALE_CRASH_OUTCOME, RESERVED_OUTCOME, stale_before.isoformat()),
            ).rowcount
            stale_reports = self.connection.execute(
                "update enrichments set status=?,last_outcome=?,updated_at=? "
                "where status=? and last_attempt_at<?",
                (
                    STATUS_FAILED_RETRYABLE,
                    STALE_CRASH_OUTCOME,
                    now.isoformat(),
                    STATUS_IN_ATTEMPT,
                    stale_before.isoformat(),
                ),
            ).rowcount
            self.connection.execute("COMMIT")
            return {"stale_starts": stale_starts, "stale_reports": stale_reports}
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    # -- durable tick evidence and summaries -------------------------------

    def append_tick_event(
        self,
        *,
        at: datetime,
        outcome: str,
        reason: str | None,
        reports_processed: int,
        starts: int,
    ) -> None:
        if outcome not in TICK_EVENT_OUTCOMES:
            raise FundamentalsStoreError("unknown_tick_outcome")
        self.connection.execute(
            "insert into tick_events(at,outcome,reason,reports_processed,"
            "starts) values(?,?,?,?,?)",
            (at.isoformat(), outcome, reason, reports_processed, starts),
        )

    def status_summary(self) -> dict:
        meta = dict(
            self.connection.execute(
                "select key,value from meta order by key"
            ).fetchall()
        )
        reports = dict(
            self.connection.execute(
                "select status,count(*) from enrichments "
                "group by status order by status"
            ).fetchall()
        )
        events = [
            {
                "at": at,
                "outcome": outcome,
                "reason": reason,
                "reports_processed": reports_processed,
                "starts": starts,
            }
            for at, outcome, reason, reports_processed, starts in self.connection.execute(
                "select at,outcome,reason,reports_processed,starts "
                "from tick_events order by event_id desc limit 5"
            ).fetchall()
        ]
        return {
            "meta": meta,
            "reports": reports,
            "cumulative_starts": self.cumulative_starts(),
            "tick_events": events,
        }

    def latest_success_rows(self, corp_code: str, bsns_year: str) -> tuple[str, ...]:
        """Interim evidence read (§6): payload rows of the maximum-rcept_no
        SUCCESS enrichment for the pair. Not a canonical material authority."""
        latest = self.connection.execute(
            "select rcept_no from enrichments where corp_code=? and "
            "bsns_year=? and status=? order by rcept_no desc limit 1",
            (corp_code, bsns_year, STATUS_SUCCESS),
        ).fetchone()
        if latest is None:
            return ()
        return tuple(
            row[0]
            for row in self.connection.execute(
                "select row_json from fundamental_payload_rows where rcept_no=? "
                "order by endpoint,ordinal",
                (latest[0],),
            ).fetchall()
        )
