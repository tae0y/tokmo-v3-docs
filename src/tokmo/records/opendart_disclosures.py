"""Private SQLite evidence store dedicated to OpenDART bulk disclosure runs."""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from tokmo.records.private_sqlite import (
    connect_private,
    prepare_private_path,
    private_creation,
)


class UnsafeStore(OSError):
    pass


@dataclass(frozen=True)
class RunReceipt:
    run_id: str
    kind: str
    begin_de: str
    end_de: str
    status: str
    planned_pages: int | None
    fetched_pages: int
    new_count: int
    duplicate_count: int
    conflict_count: int
    revision_count: int


SCHEMA = """
CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
 bgn_de TEXT NOT NULL, end_de TEXT NOT NULL, planned_pages INTEGER,
 fetched_pages INTEGER NOT NULL DEFAULT 0, first_total_count INTEGER,
 first_total_page INTEGER, status TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
 duplicate_count INTEGER NOT NULL DEFAULT 0, conflict_count INTEGER NOT NULL DEFAULT 0,
 revision_count INTEGER NOT NULL DEFAULT 0,
 started_at TEXT NOT NULL, ended_at TEXT);
CREATE TABLE IF NOT EXISTS attempts(attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
 page_no INTEGER NOT NULL, started_at TEXT NOT NULL, outcome TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS pilot_invocations(
 ordinal INTEGER PRIMARY KEY CHECK(ordinal BETWEEN 1 AND 5),
 run_id TEXT NOT NULL UNIQUE, claimed_at TEXT NOT NULL,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE TABLE IF NOT EXISTS disclosures(rcept_no TEXT PRIMARY KEY, corp_code TEXT NOT NULL,
 corp_name TEXT NOT NULL, stock_code TEXT, corp_cls TEXT NOT NULL, report_nm TEXT NOT NULL,
 flr_nm TEXT NOT NULL, rcept_dt TEXT NOT NULL, rm TEXT, canonical_json TEXT NOT NULL,
 payload_hash TEXT NOT NULL, first_observed_at TEXT NOT NULL, first_run_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS conflicts(conflict_id INTEGER PRIMARY KEY, rcept_no TEXT NOT NULL,
 stored_json TEXT NOT NULL, observed_json TEXT NOT NULL, observed_at TEXT NOT NULL,
 run_id TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS dart_revisions(
 revision_id INTEGER PRIMARY KEY AUTOINCREMENT,
 rcept_no TEXT NOT NULL,
 previous_rm TEXT,
 observed_rm TEXT,
 previous_payload_hash TEXT NOT NULL,
 observed_payload_hash TEXT NOT NULL,
 observed_at TEXT NOT NULL,
 run_id TEXT NOT NULL,
 evidence_fingerprint TEXT NOT NULL UNIQUE,
 previous_canonical_json TEXT,
 observed_canonical_json TEXT,
 changed_fields TEXT,
 FOREIGN KEY(run_id) REFERENCES runs(run_id));
CREATE INDEX IF NOT EXISTS idx_dart_revisions_rcept_no ON dart_revisions(rcept_no);
"""

# First calendar day the corpus actually covers; the lower bound of the normal
# migrate seed. Older days enter only through the explicit backfill-seed
# command, so unapproved backward expansion is structurally impossible.
CORPUS_FLOOR = date(2026, 7, 1)

# day_partitions is created by the explicit `migrate` command only — never as
# a side effect of opening the store — so a paused-then-resumed scheduler
# cannot alter the production schema on its own. Single statement: it runs
# inside the migrate transaction via execute(), not executescript().
PARTITION_SCHEMA = """
CREATE TABLE IF NOT EXISTS day_partitions(
 part_date TEXT PRIMARY KEY,
 status TEXT NOT NULL DEFAULT 'open'
   CHECK(status IN ('open','in_progress','complete','failed')),
 next_page INTEGER NOT NULL DEFAULT 1,
 plan_total_page INTEGER,
 plan_total_count INTEGER,
 last_run_id TEXT,
 updated_at TEXT NOT NULL)
"""


def _day_texts(begin: date, end: date):
    day = begin
    while day <= end:
        yield day.strftime("%Y%m%d")
        day += timedelta(days=1)


def _parse_day(value: str) -> date:
    return date(int(value[:4]), int(value[4:6]), int(value[6:]))


# Evidence columns added 2026-08-07. `previous_rm`/`observed_rm` only ever
# captured the `rm` field by value; once any other field became revisable the
# old value would have survived as a hash alone and been unrecoverable. These
# must stay LAST in the CREATE above so that a freshly created schema and an
# ALTER-migrated one agree on column order.
REVISION_EVIDENCE_COLUMNS = (
    "previous_canonical_json",
    "observed_canonical_json",
    "changed_fields",
)

# Only provider/company and listed-security identifiers remain immutable.
# `rcept_no` is the table key used to find the row; drift cannot occur within
# one comparison. Any drift in these additional identity guards is CONFLICT.
IMMUTABLE_FIELDS = ("corp_code", "stock_code")

# Fields OpenDART is observed to correct after first publication. If only these
# differ, classify REVISION and adopt the provider's newer value.
#
# `rcept_dt` joined 2026-08-07. DART served rcept_dt=20260807 for rcept_no
# 20260806000555 at 2026-08-06T22:30:42Z and rcept_dt=20260806 from
# 2026-08-07T00:30Z; 1,172 stored rows carry the same +1-day shape. Treating it
# as identity stalled the whole lane, because one conflict aborts the run and
# the ascending sort puts new disclosures on the last pages.
# The canonical identity is `rcept_no`, not the date attached to it.
PROVIDER_MUTABLE_FIELDS = (
    "corp_name",
    "corp_cls",
    "report_nm",
    "flr_nm",
    "rcept_dt",
    "rm",
)


def _changed_fields(stored_canonical: str, observed_canonical: str) -> str:
    """Sorted CSV of the field names that differ. Empty string if undecodable."""
    try:
        stored = json.loads(stored_canonical)
        observed = json.loads(observed_canonical)
    except (json.JSONDecodeError, TypeError):
        return ""
    return ",".join(
        sorted(
            name
            for name in set(stored) | set(observed)
            if stored.get(name) != observed.get(name)
        )
    )


def _is_listing_enrichment_transition(stored: dict, observed: dict) -> bool:
    """Allow only the observed unlisted-E to KOSDAQ listing enrichment.

    stock_code remains globally immutable. This predicate is deliberately
    exact so no other stock-code drift can enter the revision path.
    """
    changed = {
        name
        for name in set(stored) | set(observed)
        if stored.get(name) != observed.get(name)
    }
    observed_stock = observed.get("stock_code")
    return (
        changed == {"corp_cls", "stock_code"}
        and stored.get("corp_cls") == "E"
        and observed.get("corp_cls") == "K"
        and stored.get("stock_code") in (None, "")
        and isinstance(observed_stock, str)
        and bool(observed_stock)
    )


def _migrate(connection):
    """Additive, idempotent migration for existing DBs created before
    dart_revisions table and runs.revision_count column existed."""
    # Add runs.revision_count column if missing
    cols = [row[1] for row in connection.execute("pragma table_info(runs)").fetchall()]
    if "revision_count" not in cols:
        connection.execute(
            "ALTER TABLE runs ADD COLUMN revision_count INTEGER NOT NULL DEFAULT 0"
        )
    # Create dart_revisions table if missing
    exists = connection.execute(
        "select count(*) from sqlite_master where type='table' and name='dart_revisions'"
    ).fetchone()[0]
    if not exists:
        connection.execute(
            "CREATE TABLE dart_revisions("
            "revision_id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "rcept_no TEXT NOT NULL, "
            "previous_rm TEXT, "
            "observed_rm TEXT, "
            "previous_payload_hash TEXT NOT NULL, "
            "observed_payload_hash TEXT NOT NULL, "
            "observed_at TEXT NOT NULL, "
            "run_id TEXT NOT NULL, "
            "evidence_fingerprint TEXT NOT NULL UNIQUE, "
            "FOREIGN KEY(run_id) REFERENCES runs(run_id))"
        )
        connection.execute(
            "CREATE INDEX idx_dart_revisions_rcept_no ON dart_revisions(rcept_no)"
        )
    # Add the revision evidence columns to DBs whose dart_revisions predates
    # them. ALTER TABLE appends, and SCHEMA declares them last, so both paths
    # converge on the same column order.
    revision_cols = [
        row[1]
        for row in connection.execute("pragma table_info(dart_revisions)").fetchall()
    ]
    for column in REVISION_EVIDENCE_COLUMNS:
        if column not in revision_cols:
            connection.execute(f"ALTER TABLE dart_revisions ADD COLUMN {column} TEXT")


class DisclosureStore:
    def __init__(self, path: Path, connection: sqlite3.Connection):
        self.path, self.connection = path, connection

    @classmethod
    def open(cls, path: Path):
        safety = prepare_private_path(path)
        if safety.unsafe:
            raise UnsafeStore(safety.reason)
        connection = None
        try:
            with private_creation():
                connection = connect_private(path)
            os.chmod(path, 0o600)
            connection.executescript(SCHEMA)
            connection.execute("BEGIN IMMEDIATE")
            _migrate(connection)
            connection.execute("COMMIT")
            return cls(path, connection)
        except (OSError, sqlite3.Error) as error:
            if connection is not None and connection.in_transaction:
                connection.execute("ROLLBACK")
            raise UnsafeStore("store_open_failed") from error

    @classmethod
    def open_readonly(cls, path: Path):
        """Open with mode=ro for status/integrity — zero write side effects."""
        if not path.is_file():
            raise UnsafeStore("store_absent")
        safety = prepare_private_path(path)
        if safety.unsafe:
            raise UnsafeStore(safety.reason)
        try:
            return cls(path, connect_private(path, mode="ro"))
        except (OSError, sqlite3.Error) as error:
            raise UnsafeStore("store_open_failed") from error

    def close(self):
        self.connection.close()

    # -- day partitions ----------------------------------------------------

    def partitions_migrated(self) -> bool:
        return bool(
            self.connection.execute(
                "select 1 from sqlite_master where type='table' "
                "and name='day_partitions'"
            ).fetchone()
        )

    def _seed_days(self, begin: date, end: date, now: datetime) -> int:
        before = self.connection.total_changes
        self.connection.executemany(
            "insert or ignore into day_partitions"
            "(part_date,status,next_page,updated_at) values(?,'open',1,?)",
            [(text, now.isoformat()) for text in _day_texts(begin, end)],
        )
        return self.connection.total_changes - before

    def migrate_partitions(self, today: date, now: datetime) -> int:
        """Explicit additive migration: create the table and seed
        [CORPUS_FLOOR..today] as open. Idempotent."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(PARTITION_SCHEMA)
            seeded = self._seed_days(CORPUS_FLOOR, today, now)
            self.connection.execute("COMMIT")
            return seeded
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def seed_forward(self, today: date, floor: date, now: datetime) -> int:
        """Fill every missing day between the newest seeded row and today so
        days elapsed while the scheduler was down can never silently vanish."""
        latest = self.connection.execute(
            "select max(part_date) from day_partitions"
        ).fetchone()[0]
        if latest is None:
            return 0
        begin = max(_parse_day(latest) + timedelta(days=1), floor)
        if begin > today:
            return 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            seeded = self._seed_days(begin, today, now)
            self.connection.execute("COMMIT")
            return seeded
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def backfill_seed(self, floor: date, now: datetime) -> int:
        """Explicit 18-month backfill entry: seed [floor..earliest) as open.
        Collection then runs through the ordinary backlog lane. Idempotent."""
        if not self.partitions_migrated():
            raise UnsafeStore("migration_required")
        earliest = self.connection.execute(
            "select min(part_date) from day_partitions"
        ).fetchone()[0]
        if earliest is None:
            raise UnsafeStore("migration_required")
        end = _parse_day(earliest) - timedelta(days=1)
        if floor > end:
            return 0
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            seeded = self._seed_days(floor, end, now)
            self.connection.execute("COMMIT")
            return seeded
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def partition(self, part_date: str):
        return self.connection.execute(
            "select status,next_page,plan_total_page,plan_total_count "
            "from day_partitions where part_date=?",
            (part_date,),
        ).fetchone()

    def set_partition(
        self,
        part_date: str,
        *,
        status: str,
        next_page: int,
        plan,
        run_id: str,
        now: datetime,
    ):
        pages, count = plan if plan is not None else (None, None)
        self.connection.execute(
            "update day_partitions set status=?,next_page=?,plan_total_page=?,"
            "plan_total_count=?,last_run_id=?,updated_at=? where part_date=?",
            (status, next_page, pages, count, run_id, now.isoformat(), part_date),
        )

    def next_backlog(self, today: str, floor: str, exclude: tuple):
        marks = ",".join("?" for _ in exclude)
        exclusion = f" and part_date not in ({marks})" if exclude else ""
        row = self.connection.execute(
            "select part_date from day_partitions where part_date<? "
            "and part_date>=? and status in ('open','failed')"
            + exclusion
            + " order by (status='failed'), part_date limit 1",
            (today, floor, *exclude),
        ).fetchone()
        return row[0] if row else None

    def recover_stale(self, now: datetime, stale_before: datetime):
        """Settle claimed runs older than the stale threshold and reopen
        in_progress partitions whose claimed run is gone. Cursor survives."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            self.connection.execute(
                "update runs set status='failed', ended_at=? "
                "where status='claimed' and started_at<?",
                (now.isoformat(), stale_before.isoformat()),
            )
            self.connection.execute(
                "update day_partitions set status='open', updated_at=? "
                "where status='in_progress' and not exists("
                "select 1 from runs where runs.run_id=day_partitions.last_run_id "
                "and runs.status='claimed')",
                (now.isoformat(),),
            )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise

    def prune(self, floor: date, now: datetime) -> dict:
        """Delete rows strictly below the retention floor. Double guard on
        disclosures: the mutable rcept_dt alone never prunes a row, and any
        rcept_no with recorded conflict evidence is kept unconditionally.
        runs/pilot_invocations/conflicts are permanent."""
        if not self.partitions_migrated():
            raise UnsafeStore("migration_required")
        floor_text = floor.strftime("%Y%m%d")
        floor_iso = floor.strftime("%Y-%m-%d")
        doomed = (
            "select rcept_no from disclosures where rcept_dt<? "
            "and substr(rcept_no,1,8)<? "
            "and rcept_no not in (select rcept_no from conflicts)"
        )
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            revisions = self.connection.execute(
                f"delete from dart_revisions where rcept_no in ({doomed})",
                (floor_text, floor_text),
            ).rowcount
            disclosures = self.connection.execute(
                f"delete from disclosures where rcept_no in ({doomed})",
                (floor_text, floor_text),
            ).rowcount
            attempts = self.connection.execute(
                "delete from attempts where run_id in (select run_id from runs "
                "where ended_at is not null and substr(ended_at,1,10)<?)",
                (floor_iso,),
            ).rowcount
            partitions = self.connection.execute(
                "delete from day_partitions where part_date<?", (floor_text,)
            ).rowcount
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        violations = self.connection.execute("pragma foreign_key_check").fetchall()
        return {
            "disclosures": disclosures,
            "revisions": revisions,
            "attempts": attempts,
            "partitions": partitions,
            "foreign_key_violations": len(violations),
            "integrity": self.integrity(),
        }

    def claim(self, kind: str, bgn_de: str, end_de: str, now: datetime) -> RunReceipt:
        return self._claim(kind, bgn_de, end_de, now, pilot=False)

    def claim_pilot_incremental(
        self, bgn_de: str, end_de: str, now: datetime
    ) -> RunReceipt | None:
        return self._claim("incremental", bgn_de, end_de, now, pilot=True)

    def _claim(
        self, kind: str, bgn_de: str, end_de: str, now: datetime, *, pilot: bool
    ) -> RunReceipt | None:
        run_id = uuid4().hex
        if kind not in {"preflight", "backfill", "incremental", "partition"}:
            raise ValueError("run_kind")
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            if pilot:
                used = self.connection.execute(
                    "select count(*) from pilot_invocations"
                ).fetchone()[0]
                if used >= 5:
                    self.connection.execute("ROLLBACK")
                    return None
                ordinal = used + 1
            active = self.connection.execute(
                "select 1 from runs where status='claimed' limit 1"
            ).fetchone()
            status = "skipped_overlap" if active else "claimed"
            self.connection.execute(
                "insert into runs(run_id,kind,bgn_de,end_de,status,started_at,ended_at) values(?,?,?,?,?,?,?)",
                (
                    run_id,
                    kind,
                    bgn_de,
                    end_de,
                    status,
                    now.isoformat(),
                    now.isoformat() if active else None,
                ),
            )
            if pilot:
                self.connection.execute(
                    "insert into pilot_invocations(ordinal,run_id,claimed_at) values(?,?,?)",
                    (ordinal, run_id, now.isoformat()),
                )
            self.connection.execute("COMMIT")
        except BaseException:
            self.connection.execute("ROLLBACK")
            raise
        return self.receipt(run_id)

    def start_attempt(self, run_id: str, page_no: int, now: datetime) -> int:
        cursor = self.connection.execute(
            "insert into attempts(run_id,page_no,started_at,outcome) values(?,?,?,'started')",
            (run_id, page_no, now.isoformat()),
        )
        return cursor.lastrowid

    def close_attempt(self, attempt_id: int, outcome: str):
        self.connection.execute(
            "update attempts set outcome=? where attempt_id=?", (outcome, attempt_id)
        )

    def set_plan(self, run_id: str, pages: int, count: int):
        self.connection.execute(
            "update runs set planned_pages=?,first_total_count=?,first_total_page=? where run_id=?",
            (pages, count, pages, run_id),
        )

    def page_fetched(self, run_id: str):
        self.connection.execute(
            "update runs set fetched_pages=fetched_pages+1 where run_id=?", (run_id,)
        )

    def observe(self, run, disclosure, now: datetime) -> str:
        """Observe one disclosure atomically, including evidence and counters."""
        run_id = run.run_id if hasattr(run, "run_id") else run
        canonical, digest = disclosure.canonical_json(), disclosure.payload_hash()
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            stored = self.connection.execute(
                "select canonical_json,payload_hash,rm from disclosures where rcept_no=?",
                (disclosure.rcept_no,),
            ).fetchone()
            if stored is None:
                self.connection.execute(
                    "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        disclosure.rcept_no,
                        disclosure.corp_code,
                        disclosure.corp_name,
                        disclosure.stock_code,
                        disclosure.corp_cls,
                        disclosure.report_nm,
                        disclosure.flr_nm,
                        disclosure.rcept_dt,
                        disclosure.rm,
                        canonical,
                        digest,
                        now.isoformat(),
                        run_id,
                    ),
                )
                result, column = "new", "new_count"
            elif stored[1] == digest and stored[0] == canonical:
                result, column = "duplicate", "duplicate_count"
            else:
                stored_canonical, stored_hash, stored_rm = (
                    stored[0],
                    stored[1],
                    stored[2],
                )
                if self._is_revision_only(stored_canonical, canonical):
                    self._record_revision(
                        disclosure,
                        stored_canonical,
                        stored_hash,
                        stored_rm,
                        canonical,
                        digest,
                        now,
                        run_id,
                    )
                    result, column = "revision", "revision_count"
                else:
                    self.connection.execute(
                        "insert into conflicts(rcept_no,stored_json,observed_json,observed_at,run_id) values(?,?,?,?,?)",
                        (
                            disclosure.rcept_no,
                            stored_canonical,
                            canonical,
                            now.isoformat(),
                            run_id,
                        ),
                    )
                    result, column = "conflict", "conflict_count"
            self.connection.execute(
                f"update runs set {column}={column}+1 where run_id=?", (run_id,)
            )
            self.connection.execute("COMMIT")
            return result
        except BaseException:
            if self.connection.in_transaction:
                self.connection.execute("ROLLBACK")
            raise

    def _is_revision_only(self, stored_canonical: str, observed_canonical: str) -> bool:
        """Return True if only provider-mutable fields differ."""
        try:
            stored = json.loads(stored_canonical)
            observed = json.loads(observed_canonical)
        except (json.JSONDecodeError, TypeError):
            return False
        # Narrow exception before generic immutable-field classification:
        # an external/unlisted corporation has become KOSDAQ-listed.
        if _is_listing_enrichment_transition(stored, observed):
            return True
        for field in IMMUTABLE_FIELDS:
            if stored.get(field) != observed.get(field):
                return False
        # All immutable fields match — check that at least one mutable field differs
        for field in PROVIDER_MUTABLE_FIELDS:
            if stored.get(field) != observed.get(field):
                return True
        return False

    def _record_revision(
        self,
        disclosure,
        stored_canonical,
        stored_hash,
        stored_rm,
        observed_canonical,
        observed_hash,
        now: datetime,
        run_id: str,
    ):
        """Append immutable revision evidence and update canonical latest payload
        to observed. Atomic with the caller's transaction."""
        previous_rm = stored_rm
        observed_rm = disclosure.rm
        observed_at = now.isoformat()
        occurrence = self.connection.execute(
            "select count(*) from dart_revisions where rcept_no=? "
            "and previous_payload_hash=? and observed_payload_hash=?",
            (disclosure.rcept_no, stored_hash, observed_hash),
        ).fetchone()[0]
        fingerprint = hashlib.sha256(
            f"{disclosure.rcept_no}:{stored_hash}:{observed_hash}:{occurrence}".encode()
        ).hexdigest()
        self.connection.execute(
            "INSERT INTO dart_revisions"
            "(rcept_no,previous_rm,observed_rm,previous_payload_hash,"
            "observed_payload_hash,observed_at,run_id,evidence_fingerprint,"
            "previous_canonical_json,observed_canonical_json,changed_fields) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                disclosure.rcept_no,
                previous_rm,
                observed_rm,
                stored_hash,
                observed_hash,
                observed_at,
                run_id,
                fingerprint,
                stored_canonical,
                observed_canonical,
                _changed_fields(stored_canonical, observed_canonical),
            ),
        )
        # Update canonical latest payload to observed. Every provider-mutable
        # field also has its own denormalised column on `disclosures`; leaving
        # one behind would let that column drift from canonical_json. The column
        # list is the module constant, never caller input.
        assignments = ",".join(f"{name}=?" for name in PROVIDER_MUTABLE_FIELDS)
        mutated = tuple(getattr(disclosure, name) for name in PROVIDER_MUTABLE_FIELDS)
        stored = json.loads(stored_canonical)
        observed = json.loads(observed_canonical)
        listing_enrichment = _is_listing_enrichment_transition(stored, observed)
        stock_assignment = ",stock_code=?" if listing_enrichment else ""
        stock_value = (disclosure.stock_code,) if listing_enrichment else ()
        self.connection.execute(
            f"UPDATE disclosures SET canonical_json=?,payload_hash=?,{assignments}"
            f"{stock_assignment} "
            "WHERE rcept_no=?",
            (
                observed_canonical,
                observed_hash,
                *mutated,
                *stock_value,
                disclosure.rcept_no,
            ),
        )

    def finish(self, run_id: str, status: str, now: datetime):
        self.connection.execute(
            "update runs set status=?,ended_at=? where run_id=? and status='claimed'",
            (status, now.isoformat(), run_id),
        )
        return self.receipt(run_id)

    def receipt(self, run_id: str) -> RunReceipt:
        row = self.connection.execute(
            "select run_id,kind,bgn_de,end_de,status,planned_pages,fetched_pages,new_count,duplicate_count,conflict_count,revision_count from runs where run_id=?",
            (run_id,),
        ).fetchone()
        return RunReceipt(*row)

    def completed_preflight(self, run_id: str):
        return self.connection.execute(
            "select planned_pages,bgn_de,end_de,first_total_count from runs where run_id=? and kind='preflight' and status='complete'",
            (run_id,),
        ).fetchone()

    def latest_complete(self, kinds):
        marks = ",".join("?" for _ in kinds)
        return self.connection.execute(
            f"select run_id,bgn_de,end_de from runs where status='complete' "
            f"and kind in ({marks}) order by ended_at desc,rowid desc limit 1",
            tuple(kinds),
        ).fetchone()

    def recent_first_observed(self, since_iso: str, limit: int):
        """Read-only enrichment candidate scan (fundamentals SDS §3.3).

        Listed issuers only: rows whose stock_code is NULL can never map to a
        fundamentals request, and downstream stores that declare stock_code
        NOT NULL rely on exactly this filter.
        """
        return self.connection.execute(
            "select rcept_no,corp_code,corp_name,stock_code,report_nm,"
            "rcept_dt,first_observed_at from disclosures "
            "where stock_code is not null and first_observed_at>=? "
            "order by first_observed_at,rcept_no limit ?",
            (since_iso, limit),
        ).fetchall()

    def integrity(self):
        return self.connection.execute("pragma integrity_check").fetchone()[0]
