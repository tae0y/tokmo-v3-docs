"""Private durable cache for the routine FRED query seam.

Delta SDS §6: this adapter stores only passed typed values. It never calls
Facts, never reaches a provider and is completely separate from the R1-R4
campaign material store — no schema or write is added there.

``query_attempt`` is append-only settlement history keyed by
``(query_identity, due_slot, attempt_seq)``. A claim runs in one
``BEGIN IMMEDIATE`` transaction that first reconciles expired claims, then
refuses to start while any ``STARTED`` row is active for that slot, then
allocates ``COALESCE(MAX(attempt_seq), 0) + 1``. Reconciliation converts an
expired ``STARTED`` row to ``FAILED``/``LOCAL_STATE_UNKNOWN_START`` and
never deletes it: the transport may already have started before the crash.
"""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation
import hashlib
import json
import os
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (
    acquire_immediate_transaction, assert_no_sidecars_after_close,
    assert_private_sidecars_while_open, connect_private, prepare_private_path,
    private_creation,
)
from tokmo.values.fred_query import (
    LOCAL_STATE_UNKNOWN_START, STARTED_LEASE, FredCompletenessV1,
    FredObservationV1, FredQueryCacheCorruption, FredQueryFailureCodeV1,
    FredSeriesFactV1, FredValueStateV1, build_fred_series_fact_v1,
    canonical_json,
)

DATABASE_NAME = "fred-query-cache.sqlite3"
FACT_SCHEMA = "tokmo.fred-query.fact.r1"
USER_VERSION = 1

DDL = """
CREATE TABLE query_success(
  query_identity TEXT PRIMARY KEY,
  series_id TEXT NOT NULL,
  revision_identity TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  fact_json BLOB NOT NULL,
  fact_digest TEXT NOT NULL,
  due_slot TEXT NOT NULL
);
CREATE TABLE query_attempt(
  query_identity TEXT NOT NULL,
  due_slot TEXT NOT NULL,
  attempt_seq INTEGER NOT NULL CHECK(attempt_seq >= 1),
  state TEXT NOT NULL CHECK(state IN ('STARTED','SUCCEEDED','FAILED')),
  started_at TEXT NOT NULL,
  finished_at TEXT,
  failure_code TEXT,
  PRIMARY KEY(query_identity, due_slot, attempt_seq)
);
PRAGMA user_version=1;
"""

_EXPECTED_COLUMNS = {
    "query_success": ("query_identity", "series_id", "revision_identity",
                      "fetched_at", "fact_json", "fact_digest", "due_slot"),
    "query_attempt": ("query_identity", "due_slot", "attempt_seq", "state",
                      "started_at", "finished_at", "failure_code"),
}


@dataclass(frozen=True)
class FredCachedSuccessV1:
    query_identity: str
    series_id: str
    revision_identity: str
    fetched_at: datetime
    due_slot: str
    fact: FredSeriesFactV1


@dataclass(frozen=True)
class FredAttemptStateV1:
    highest_attempt_seq: int
    active_started_at: datetime | None
    last_failure_at: datetime | None
    last_failure_code: str | None


@dataclass(frozen=True)
class FredClaimV1:
    query_identity: str
    due_slot: str
    attempt_seq: int
    started_at: datetime


def _instant(value: object) -> str:
    if (type(value) is not datetime or value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset().total_seconds() != 0):
        raise ValueError("aware_utc_required")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _moment(value: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, ValueError) as error:
        raise FredQueryCacheCorruption("invalid_stored_instant") from error


def _require_identity(value: object) -> str:
    if (type(value) is not str or len(value) != 64
            or any(character not in "0123456789abcdef" for character in value)):
        raise ValueError("invalid_query_identity")
    return value


def _require_slot(value: object) -> str:
    if type(value) is not str or not 1 <= len(value) <= 120:
        raise ValueError("invalid_due_slot")
    return value


def encode_fact(fact: FredSeriesFactV1) -> bytes:
    if not isinstance(fact, FredSeriesFactV1):
        raise ValueError("fact_required")
    return canonical_json({
        "schema": FACT_SCHEMA,
        "query_identity": fact.query_identity,
        "series_id": fact.series_id,
        "fetched_at": fact.fetched_at,
        "release_id": fact.release_id,
        "release_date": fact.release_date,
        "vintage_date": fact.vintage_date,
        "revision_identity": fact.revision_identity,
        "completeness": fact.completeness,
        "observations": [item.normalized() for item in fact.observations],
    })


def decode_fact(blob: object) -> FredSeriesFactV1:
    """Rebuild a stored fact, re-validating its canonical bytes and identity."""
    if type(blob) is not bytes:
        raise FredQueryCacheCorruption("invalid_stored_fact")
    try:
        payload = json.loads(blob)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise FredQueryCacheCorruption("invalid_stored_fact") from error
    if type(payload) is not dict or payload.get("schema") != FACT_SCHEMA:
        raise FredQueryCacheCorruption("invalid_stored_fact")
    try:
        observations = tuple(
            FredObservationV1(
                date.fromisoformat(item["observation_date"]),
                None if item["value"] is None else Decimal(item["value"]),
                FredValueStateV1(item["value_state"]),
                date.fromisoformat(item["realtime_start"]),
                date.fromisoformat(item["realtime_end"]))
            for item in payload["observations"])
        fact = build_fred_series_fact_v1(
            query_identity=payload["query_identity"],
            series_id=payload["series_id"],
            observations=observations,
            fetched_at=_stored_instant(payload["fetched_at"]),
            release_id=payload["release_id"],
            release_date=(None if payload["release_date"] is None
                          else date.fromisoformat(payload["release_date"])),
            vintage_date=(None if payload["vintage_date"] is None
                          else date.fromisoformat(payload["vintage_date"])),
            completeness=FredCompletenessV1(payload["completeness"]))
    except (KeyError, TypeError, ValueError, InvalidOperation) as error:
        raise FredQueryCacheCorruption("invalid_stored_fact") from error
    if fact.revision_identity != payload["revision_identity"]:
        raise FredQueryCacheCorruption("invalid_stored_fact")
    if encode_fact(fact) != blob:
        raise FredQueryCacheCorruption("noncanonical_stored_fact")
    return fact


def _stored_instant(value: object) -> datetime:
    if type(value) is not str or not value.endswith("Z"):
        raise ValueError("aware_utc_required")
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(
        timezone.utc)


class FredQueryCacheV1:
    """Private routine-query cache under an injected root."""

    def __init__(self, root: Path, *, opened_at: datetime):
        if not isinstance(root, Path):
            raise ValueError("injected_root_required")
        _instant(opened_at)
        self.root = root
        self.path = root / DATABASE_NAME
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise ValueError(safety.reason)
        created = not self.path.exists()
        if created:
            with private_creation():
                descriptor = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL,
                                     0o600)
                os.close(descriptor)
            fsync_directory(root)
        self._initialize(created)
        self._validate_stored_successes()
        self.reconcile_stale_claims(now=opened_at)

    # -- connection helpers -------------------------------------------------

    def _connect(self):
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise ValueError(safety.reason)
        return connect_private(self.path)

    def _initialize(self, created: bool) -> None:
        connection = self._connect()
        try:
            if created:
                connection.executescript(DDL)
            else:
                self._validate_schema(connection)
            assert_private_sidecars_while_open(self.path)
        finally:
            connection.close()
        fsync_directory(self.root)
        if assert_no_sidecars_after_close(self.path):
            raise ValueError("sqlite_sidecar_present")

    def _validate_schema(self, connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != USER_VERSION:
            raise FredQueryCacheCorruption("schema_mismatch")
        for table, columns in _EXPECTED_COLUMNS.items():
            actual = tuple(row[1] for row in connection.execute(
                f"PRAGMA table_info('{table}')"))
            if actual != columns:
                raise FredQueryCacheCorruption("schema_mismatch")

    def _validate_stored_successes(self) -> None:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT query_identity,revision_identity,fact_json,fact_digest "
                "FROM query_success").fetchall()
        finally:
            connection.close()
        for identity, revision, blob, digest in rows:
            fact = decode_fact(blob)
            if (hashlib.sha256(blob).hexdigest() != digest
                    or fact.query_identity != identity
                    or fact.revision_identity != revision):
                raise FredQueryCacheCorruption("stored_fact_digest_mismatch")

    # -- reconciliation -----------------------------------------------------

    def _reconcile(self, connection, now: datetime) -> int:
        cutoff = _instant(now - STARTED_LEASE)
        return connection.execute(
            "UPDATE query_attempt SET state='FAILED',finished_at=?,failure_code=? "
            "WHERE state='STARTED' AND started_at < ?",
            (_instant(now), LOCAL_STATE_UNKNOWN_START, cutoff)).rowcount

    def reconcile_stale_claims(self, *, now: datetime) -> int:
        _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            reconciled = self._reconcile(connection, now)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        if reconciled:
            fsync_directory(self.root)
        return reconciled

    # -- reads --------------------------------------------------------------

    def read_attempt_state(self, query_identity: str, due_slot: str, *,
                           now: datetime) -> FredAttemptStateV1:
        _require_identity(query_identity)
        _require_slot(due_slot)
        _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            self._reconcile(connection, now)
            rows = connection.execute(
                "SELECT attempt_seq,state,started_at,finished_at,failure_code "
                "FROM query_attempt WHERE query_identity=? AND due_slot=? "
                "ORDER BY attempt_seq", (query_identity, due_slot)).fetchall()
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        return _attempt_state(rows)

    def read_success_for_series(self, series_id: str, *,
                                now: datetime) -> FredCachedSuccessV1 | None:
        if type(series_id) is not str or not series_id:
            raise ValueError("invalid_series_id")
        _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            self._reconcile(connection, now)
            row = connection.execute(
                "SELECT query_identity,series_id,revision_identity,fetched_at,"
                "fact_json,fact_digest,due_slot FROM query_success "
                "WHERE series_id=? ORDER BY fetched_at DESC LIMIT 1",
                (series_id,)).fetchone()
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        if row is None:
            return None
        fact = decode_fact(row[4])
        if (hashlib.sha256(row[4]).hexdigest() != row[5]
                or fact.query_identity != row[0]
                or fact.revision_identity != row[2]
                or fact.series_id != row[1]):
            raise FredQueryCacheCorruption("stored_fact_digest_mismatch")
        return FredCachedSuccessV1(row[0], row[1], row[2], _moment(row[3]),
                                   row[6], fact)

    def attempt_rows(self, query_identity: str, due_slot: str) -> tuple:
        _require_identity(query_identity)
        _require_slot(due_slot)
        connection = self._connect()
        try:
            return tuple(connection.execute(
                "SELECT attempt_seq,state,started_at,finished_at,failure_code "
                "FROM query_attempt WHERE query_identity=? AND due_slot=? "
                "ORDER BY attempt_seq", (query_identity, due_slot)).fetchall())
        finally:
            connection.close()

    def count_observation_attempts_v1(self) -> int:
        """Durable observation-attempt rows.

        This is the ``series/observations`` start counter and nothing else.
        Schedule-discovery starts (``series/release``, ``release/dates``) and
        the public BLS calendar start construct no claim and leave no row
        here, so they are NOT counted by this accessor.
        """
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT COUNT(*) FROM query_attempt").fetchone()[0]
        finally:
            connection.close()

    # -- claim and settlement ----------------------------------------------

    def claim(self, query_identity: str, due_slot: str, *,
              now: datetime) -> FredClaimV1 | None:
        _require_identity(query_identity)
        _require_slot(due_slot)
        started = _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            self._reconcile(connection, now)
            active = connection.execute(
                "SELECT count(*) FROM query_attempt WHERE query_identity=? "
                "AND due_slot=? AND state='STARTED'",
                (query_identity, due_slot)).fetchone()[0]
            if active:
                connection.execute("COMMIT")
                return None
            attempt_seq = connection.execute(
                "SELECT COALESCE(MAX(attempt_seq),0)+1 FROM query_attempt "
                "WHERE query_identity=? AND due_slot=?",
                (query_identity, due_slot)).fetchone()[0]
            connection.execute(
                "INSERT INTO query_attempt VALUES(?,?,?,?,?,?,?)",
                (query_identity, due_slot, attempt_seq, "STARTED", started,
                 None, None))
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        fsync_directory(self.root)
        return FredClaimV1(query_identity, due_slot, attempt_seq, now)

    def settle_success(self, claim: FredClaimV1, fact: FredSeriesFactV1, *,
                       now: datetime) -> None:
        self._require_claim(claim)
        if not isinstance(fact, FredSeriesFactV1):
            raise ValueError("fact_required")
        if fact.query_identity != claim.query_identity:
            raise ValueError("settlement_fact_mismatch")
        blob = encode_fact(fact)
        digest = hashlib.sha256(blob).hexdigest()
        finished = _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            self._reconcile(connection, now)
            self._settle_row(connection, claim, "SUCCEEDED", finished, None)
            connection.execute(
                "INSERT INTO query_success VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(query_identity) DO UPDATE SET series_id=excluded.series_id,"
                "revision_identity=excluded.revision_identity,"
                "fetched_at=excluded.fetched_at,fact_json=excluded.fact_json,"
                "fact_digest=excluded.fact_digest,due_slot=excluded.due_slot",
                (fact.query_identity, fact.series_id, fact.revision_identity,
                 _instant(fact.fetched_at), blob, digest, claim.due_slot))
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        fsync_directory(self.root)

    def settle_failure(self, claim: FredClaimV1,
                       failure_code: FredQueryFailureCodeV1, *,
                       now: datetime) -> None:
        self._require_claim(claim)
        if not isinstance(failure_code, FredQueryFailureCodeV1):
            raise ValueError("invalid_failure_code")
        finished = _instant(now)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            self._reconcile(connection, now)
            self._settle_row(connection, claim, "FAILED", finished,
                             failure_code.value)
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        fsync_directory(self.root)

    @staticmethod
    def _require_claim(claim: object) -> None:
        if not isinstance(claim, FredClaimV1):
            raise ValueError("claim_required")

    @staticmethod
    def _settle_row(connection, claim: FredClaimV1, state: str, finished: str,
                    failure_code: str | None) -> None:
        changed = connection.execute(
            "UPDATE query_attempt SET state=?,finished_at=?,failure_code=? "
            "WHERE query_identity=? AND due_slot=? AND attempt_seq=? "
            "AND state='STARTED'",
            (state, finished, failure_code, claim.query_identity,
             claim.due_slot, claim.attempt_seq)).rowcount
        if changed != 1:
            raise ValueError("settlement_conflict")


def _attempt_state(rows) -> FredAttemptStateV1:
    highest = max((row[0] for row in rows), default=0)
    active = None
    failure_at = None
    failure_code = None
    for _seq, state, started_at, finished_at, code in rows:
        if state == "STARTED":
            active = _moment(started_at)
        elif state == "FAILED":
            moment = _moment(finished_at)
            if failure_at is None or moment >= failure_at:
                failure_at, failure_code = moment, code
    return FredAttemptStateV1(highest, active, failure_at, failure_code)
