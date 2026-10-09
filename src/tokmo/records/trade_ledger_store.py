"""Append-only private SQLite trade-ledger store (official storage contract).

One ``append_batch`` call classifies every submitted member exactly once as
new/duplicate/conflict/invalid under the store-side source contract, then
either inserts all NEW rows plus one audit row in a single ``BEGIN
IMMEDIATE`` transaction or rolls back with zero event mutation. Exact
duplicates are success, not errors; any payload drift under an existing
identity fails the whole batch closed. No import path updates or deletes
prior rows. Receipts and exceptions expose counts, closed reason tokens,
and opaque IDs only.

The durability/permission recipe deliberately mirrors the latest-quote
store by comparison, without extracting a shared framework.
"""

import dataclasses
import hashlib
import json
import os
import re
import sqlite3
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, date, datetime
from pathlib import Path

from tokmo.records.trade_ledger_canonical import (
    canonical_payload_bytes,
    is_canonical_decimal_string,
    payload_hash_hex,
)
from tokmo.records.trade_ledger_preflight import (
    SOURCE_CONTRACTS,
    SourceContract,
    registered_contract,
    sequence_preflight_ok,
)
from tokmo.records.trade_ledger_privacy import (
    StoreCorruptError,
    StoreLockedError,
    StoreReadError,
)
from tokmo.values.trade_ledger import (
    EVENT_KINDS,
    TRADE_LEDGER_PAYLOAD_VERSION,
    TRADE_LEDGER_SCHEMA_VERSION,
    ImportAuditMeta,
    LedgerStoredRow,
    TradeBatchSaveReceipt,
    TradeLedgerEvent,
)

MIN_SQLITE_VERSION = (3, 37, 0)
BUSY_TIMEOUT_MS = 1000

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_UMASK_LOCK = threading.Lock()

_CREATE_EVENTS = """
CREATE TABLE trade_ledger_events (
    event_id                     TEXT PRIMARY KEY,
    source                       TEXT NOT NULL,
    account_scope                TEXT NOT NULL,
    source_record_key            TEXT NOT NULL,
    event_kind                   TEXT NOT NULL,
    supersedes_source_record_key TEXT,
    trade_date                   TEXT NOT NULL,
    transaction_type_raw         TEXT NOT NULL,
    instrument_raw               TEXT NOT NULL,
    quantity                     TEXT NOT NULL,
    unit_price                   TEXT NOT NULL,
    amount                       TEXT NOT NULL,
    medium_raw                   TEXT NOT NULL,
    currency_raw                 TEXT,
    settlement_date              TEXT,
    fee                          TEXT,
    tax                          TEXT,
    payload_version              INTEGER NOT NULL,
    payload_hash                 TEXT NOT NULL,
    stored_at                    TEXT NOT NULL,
    schema_version               INTEGER NOT NULL,
    UNIQUE(source, account_scope, source_record_key),
    CHECK (event_kind IN ('EXECUTION', 'CORRECTION')),
    CHECK (schema_version = 1),
    CHECK (payload_version = 1)
) STRICT
"""
_CREATE_INDEX = """
CREATE INDEX trade_ledger_scope_order
ON trade_ledger_events(source, account_scope, trade_date, source_record_key)
"""
_CREATE_BATCHES = """
CREATE TABLE trade_ledger_import_batches (
    import_batch_id   TEXT PRIMARY KEY,
    source            TEXT NOT NULL,
    account_scope     TEXT NOT NULL,
    file_sha256       TEXT,
    importer_version  TEXT NOT NULL,
    submitted_count       INTEGER NOT NULL,
    new_candidate_count   INTEGER NOT NULL,
    inserted_count        INTEGER NOT NULL,
    duplicate_count       INTEGER NOT NULL,
    conflict_count        INTEGER NOT NULL,
    invalid_count         INTEGER NOT NULL,
    status            TEXT NOT NULL,
    reason            TEXT NOT NULL,
    stored_at         TEXT NOT NULL
) STRICT
"""

_EVENT_COLUMNS = (
    "event_id",
    "source",
    "account_scope",
    "source_record_key",
    "event_kind",
    "supersedes_source_record_key",
    "trade_date",
    "transaction_type_raw",
    "instrument_raw",
    "quantity",
    "unit_price",
    "amount",
    "medium_raw",
    "currency_raw",
    "settlement_date",
    "fee",
    "tax",
    "payload_version",
    "payload_hash",
    "stored_at",
    "schema_version",
)
_INSERT_EVENT = (
    f"INSERT INTO trade_ledger_events ({', '.join(_EVENT_COLUMNS)}) "
    f"VALUES ({', '.join('?' for _ in _EVENT_COLUMNS)})"
)
_INSERT_BATCH = (
    "INSERT INTO trade_ledger_import_batches (import_batch_id, source, "
    "account_scope, file_sha256, importer_version, submitted_count, "
    "new_candidate_count, inserted_count, duplicate_count, conflict_count, "
    "invalid_count, status, reason, stored_at) "
    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_SELECT_SEMANTIC = (
    "SELECT source_record_key, event_kind, supersedes_source_record_key, "
    "trade_date, transaction_type_raw, instrument_raw, quantity, unit_price, "
    "amount, medium_raw, currency_raw, settlement_date, fee, tax, "
    "payload_version, payload_hash, schema_version "
    "FROM trade_ledger_events WHERE source=? AND account_scope=?"
)
_SELECT_WINDOW = (
    "SELECT source_record_key, event_kind, supersedes_source_record_key, "
    "trade_date, transaction_type_raw, instrument_raw, quantity, unit_price, "
    "amount, medium_raw, currency_raw, settlement_date, fee, tax, "
    "payload_version, payload_hash, schema_version, stored_at "
    "FROM trade_ledger_events WHERE source=? AND account_scope=? "
    "AND stored_at >= ? AND stored_at < ? "
    "ORDER BY stored_at, source_record_key, event_id"
)


def _normalized_sql(sql: str) -> str:
    return " ".join(sql.split())


_EXPECTED_SCHEMA = {
    "trade_ledger_events": _normalized_sql(_CREATE_EVENTS),
    "trade_ledger_scope_order": _normalized_sql(_CREATE_INDEX),
    "trade_ledger_import_batches": _normalized_sql(_CREATE_BATCHES),
}


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _valid_text(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, str) and len(value) >= 1


def _valid_date(value: object, *, nullable: bool = False) -> bool:
    if value is None:
        return nullable
    return isinstance(value, date) and not isinstance(value, datetime)


def _valid_event(event: object) -> bool:
    return (
        isinstance(event, TradeLedgerEvent)
        and _valid_text(event.source)
        and _valid_text(event.account_scope)
        and _valid_text(event.source_record_key)
        and event.event_kind in EVENT_KINDS
        and (
            event.supersedes_source_record_key is None
            or (
                _valid_text(event.supersedes_source_record_key)
                and event.event_kind == "CORRECTION"
            )
        )
        and _valid_date(event.trade_date)
        and _valid_text(event.transaction_type_raw)
        and _valid_text(event.instrument_raw)
        and is_canonical_decimal_string(event.quantity)
        and is_canonical_decimal_string(event.unit_price)
        and is_canonical_decimal_string(event.amount)
        and _valid_text(event.medium_raw)
        and _valid_text(event.currency_raw, nullable=True)
        and _valid_date(event.settlement_date, nullable=True)
        and (event.fee is None or is_canonical_decimal_string(event.fee))
        and (event.tax is None or is_canonical_decimal_string(event.tax))
    )


def _valid_audit(meta: object) -> bool:
    return (
        isinstance(meta, ImportAuditMeta)
        and (
            meta.file_sha256 is None
            or (
                isinstance(meta.file_sha256, str)
                and _SHA256_RE.fullmatch(meta.file_sha256) is not None
            )
        )
        and isinstance(meta.importer_version, str)
        and len(meta.importer_version) >= 1
    )


def _sqlite_locked(error: sqlite3.Error) -> bool:
    return getattr(error, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


@contextmanager
def _private_creation():
    with _UMASK_LOCK:
        prior = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(prior)


def _event_from_semantic_row(
    row: tuple, *, source: str, account_scope: str
) -> tuple[TradeLedgerEvent, str]:
    """Rebuild the typed event and stored hash from one semantic row."""
    (
        source_record_key,
        event_kind,
        supersedes,
        trade_date_text,
        transaction_type_raw,
        instrument_raw,
        quantity,
        unit_price,
        amount,
        medium_raw,
        currency_raw,
        settlement_text,
        fee,
        tax,
        payload_version,
        payload_hash,
        schema_version,
    ) = row
    if (
        payload_version != TRADE_LEDGER_PAYLOAD_VERSION
        or schema_version != TRADE_LEDGER_SCHEMA_VERSION
    ):
        raise StoreCorruptError("store_corrupt")
    try:
        trade_date = date.fromisoformat(trade_date_text)
        settlement_date = (
            None if settlement_text is None else date.fromisoformat(settlement_text)
        )
        event = TradeLedgerEvent(
            source=source,
            account_scope=account_scope,
            source_record_key=source_record_key,
            event_kind=event_kind,
            supersedes_source_record_key=supersedes,
            trade_date=trade_date,
            transaction_type_raw=transaction_type_raw,
            instrument_raw=instrument_raw,
            quantity=quantity,
            unit_price=unit_price,
            amount=amount,
            medium_raw=medium_raw,
            currency_raw=currency_raw,
            settlement_date=settlement_date,
            fee=fee,
            tax=tax,
        )
    except (TypeError, ValueError):
        raise StoreCorruptError("store_corrupt") from None
    if not _valid_event(event) or not isinstance(payload_hash, str):
        raise StoreCorruptError("store_corrupt")
    return event, payload_hash


def _parse_stored_at(value: object) -> datetime:
    """Parse the stored_at column text into a timezone-aware UTC datetime."""
    if not isinstance(value, str):
        raise StoreCorruptError("store_corrupt")
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        raise StoreCorruptError("store_corrupt") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise StoreCorruptError("store_corrupt")
    return parsed.astimezone(UTC)


def _stored_row_from_window_row(
    row: tuple, *, source: str, account_scope: str
) -> "LedgerStoredRow":
    """Rebuild a LedgerStoredRow (event + stored_at) from one window row."""
    semantic = row[:-1]
    stored_at_text = row[-1]
    event, stored_hash = _event_from_semantic_row(
        semantic,
        source=source,
        account_scope=account_scope,
    )
    payload = canonical_payload_bytes(event)
    if payload_hash_hex(payload) != stored_hash:
        raise StoreCorruptError("store_corrupt")
    stored_at = _parse_stored_at(stored_at_text)
    return LedgerStoredRow(event=event, stored_at=stored_at)


def _stored_rows_from_window_rows(
    rows: tuple[tuple, ...] | list[tuple],
    *,
    source: str,
    account_scope: str,
) -> tuple[LedgerStoredRow, ...]:
    """Decode window rows and fail closed on duplicate semantic keys."""
    result = []
    seen_source_record_keys: set[str] = set()
    for row in rows:
        stored_row = _stored_row_from_window_row(
            row,
            source=source,
            account_scope=account_scope,
        )
        key = stored_row.event.source_record_key
        if key in seen_source_record_keys:
            raise StoreCorruptError("store_corrupt")
        seen_source_record_keys.add(key)
        result.append(stored_row)
    return tuple(result)


class _BatchOutcome:
    """Per-member terminal classification for one submitted tuple."""

    def __init__(self, submitted: int) -> None:
        self.submitted = submitted
        self.classes: dict[int, str] = {}

    def mark(self, index: int, classification: str) -> None:
        self.classes[index] = classification

    def count(self, classification: str) -> int:
        return sum(1 for value in self.classes.values() if value == classification)

    def new_indexes(self) -> list[int]:
        return sorted(index for index, value in self.classes.items() if value == "new")

    def mark_all_remaining(self, classification: str) -> None:
        for index in range(self.submitted):
            self.classes.setdefault(index, classification)


class TradeLedgerStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime],
        id_factory: Callable[[], str],
        source_contracts: dict[str, SourceContract] | None = None,
    ) -> None:
        self._path = path
        self._clock = clock
        self._id_factory = id_factory
        self._contracts = (
            SOURCE_CONTRACTS if source_contracts is None else source_contracts
        )

    # -- connection and schema ------------------------------------------------

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._path, timeout=1.0, isolation_level=None)
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if mode != ("delete",) or synchronous != (2,):
                raise OSError
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare_path(self) -> bool:
        parent = self._path.parent
        if parent.exists():
            if not parent.is_dir():
                return False
            if stat.S_IMODE(parent.stat().st_mode) & 0o077:
                return False
        else:
            with _private_creation():
                parent.mkdir(mode=0o700, parents=True)
            if stat.S_IMODE(parent.stat().st_mode) != 0o700:
                return False
        if self._path.exists() and not self._path.is_file():
            return False
        return True

    def _schema_valid(self, connection: sqlite3.Connection) -> bool:
        found = {
            name: _normalized_sql(sql)
            for name, sql in connection.execute(
                "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }
        if found != _EXPECTED_SCHEMA:
            return False
        tables = {row[1]: row for row in connection.execute("PRAGMA table_list")}
        return (
            tables.get("trade_ledger_events", (None,) * 6)[5] == 1
            and tables.get("trade_ledger_import_batches", (None,) * 6)[5] == 1
        )

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_CREATE_EVENTS)
            connection.execute(_CREATE_INDEX)
            connection.execute(_CREATE_BATCHES)
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    # -- append ---------------------------------------------------------------

    def append_batch(
        self,
        events: tuple[TradeLedgerEvent, ...],
        *,
        import_audit: ImportAuditMeta,
    ) -> TradeBatchSaveReceipt:
        try:
            stored_at = self._clock()
        except BaseException:
            stored_at = datetime.min.replace(tzinfo=UTC)
            return self._all_invalid_receipt("", 0, "invalid_batch", stored_at)
        submitted = len(events) if type(events) is tuple else 0
        try:
            batch_id = self._id_factory()
            if not _valid_text(batch_id):
                raise ValueError("invalid_batch")
        except BaseException:
            return self._all_invalid_receipt("", submitted, "invalid_batch", stored_at)

        def invalid_batch() -> TradeBatchSaveReceipt:
            return self._all_invalid_receipt(
                batch_id, submitted, "invalid_batch", stored_at
            )

        if (
            not _aware(stored_at)
            or type(events) is not tuple
            or len(events) == 0
            or not _valid_audit(import_audit)
        ):
            return invalid_batch()
        stored_at = stored_at.astimezone(UTC)
        typed = [event for event in events if isinstance(event, TradeLedgerEvent)]
        if not typed:
            return invalid_batch()
        try:
            scopes = {(event.source, event.account_scope) for event in typed}
        except TypeError:
            return invalid_batch()
        if len(scopes) != 1:
            return invalid_batch()
        ((source, account_scope),) = scopes
        if not _valid_text(source) or not _valid_text(account_scope):
            return invalid_batch()

        outcome = _BatchOutcome(submitted)
        payloads: dict[int, bytes] = {}
        for index, event in enumerate(events):
            if not _valid_event(event):
                outcome.mark(index, "invalid")
                continue
            try:
                payloads[index] = canonical_payload_bytes(event)
            except ValueError:
                outcome.mark(index, "invalid")

        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            return self._all_invalid_receipt(
                batch_id, submitted, "schema_mismatch", stored_at
            )
        try:
            if not self._prepare_path():
                return self._all_invalid_receipt(
                    batch_id, submitted, "store_write_failed", stored_at
                )
            existed = self._path.exists()
            was_empty = existed and self._path.stat().st_size == 0
            if not existed:
                with _private_creation():
                    connection = self._connect()
            else:
                connection = self._connect()
            try:
                if not existed or was_empty:
                    self._initialize(connection)
                    if (
                        hasattr(os, "getuid")
                        and self._path.stat().st_uid == os.getuid()
                    ):
                        os.chmod(self._path, 0o600)
                return self._append_in_transaction(
                    connection=connection,
                    events=events,
                    payloads=payloads,
                    outcome=outcome,
                    source=source,
                    account_scope=account_scope,
                    import_audit=import_audit,
                    batch_id=batch_id,
                    stored_at=stored_at,
                    verify_schema=existed and not was_empty,
                )
            finally:
                connection.close()
        except StoreCorruptError:
            return self._all_invalid_receipt(
                batch_id, submitted, "store_corrupt", stored_at
            )
        except sqlite3.DatabaseError as error:
            if _sqlite_locked(error):
                reason = "store_locked"
            elif getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_NOTADB:
                reason = "store_corrupt"
            else:
                reason = "store_write_failed"
            return self._all_invalid_receipt(batch_id, submitted, reason, stored_at)
        except (OSError, ValueError):
            return self._all_invalid_receipt(
                batch_id, submitted, "store_write_failed", stored_at
            )

    def _append_in_transaction(
        self,
        *,
        connection: sqlite3.Connection,
        events: tuple,
        payloads: dict[int, bytes],
        outcome: _BatchOutcome,
        source: str,
        account_scope: str,
        import_audit: ImportAuditMeta,
        batch_id: str,
        stored_at: datetime,
        verify_schema: bool,
    ) -> TradeBatchSaveReceipt:
        connection.execute("BEGIN IMMEDIATE")
        try:
            if verify_schema and not self._schema_valid(connection):
                connection.execute("ROLLBACK")
                return self._all_invalid_receipt(
                    batch_id, outcome.submitted, "schema_mismatch", stored_at
                )

            contract = registered_contract(self._contracts, source, account_scope)
            contract_violation = contract is None
            if contract is not None:
                for index in sorted(payloads):
                    event = events[index]
                    key_ok = (
                        contract.key_number(event.source_record_key) is not None
                        if contract.sequential_full_history
                        else contract.key_matches(event.source_record_key)
                    )
                    linked_ok = (
                        event.supersedes_source_record_key is None
                        or contract.allows_linked_supersession
                    )
                    if not key_ok or not linked_ok:
                        contract_violation = True
                remaining_keys = tuple(
                    sorted(
                        {
                            events[index].source_record_key
                            for index in payloads
                            if outcome.classes.get(index) is None
                        }
                    )
                )
                if not contract_violation and not sequence_preflight_ok(
                    contract, remaining_keys
                ):
                    contract_violation = True
            if contract_violation:
                return self._rollback_with_failure_audit(
                    connection,
                    self._all_invalid_receipt(
                        batch_id,
                        outcome.submitted,
                        "source_contract_violation",
                        stored_at,
                    ),
                    source=source,
                    account_scope=account_scope,
                    import_audit=import_audit,
                )

            if source == "retirement_excel_delta_v1":
                from tokmo.records.pension_delta import delta_admission_reason

                reason = (
                    "source_contract_violation"
                    if len(payloads) != len(events)
                    else delta_admission_reason(connection, events)
                )
                if reason is not None:
                    return self._rollback_with_failure_audit(
                        connection,
                        self._all_invalid_receipt(
                            batch_id, outcome.submitted, reason, stored_at
                        ),
                        source=source,
                        account_scope=account_scope,
                        import_audit=import_audit,
                    )

            distinct_keys = tuple(
                sorted({events[index].source_record_key for index in payloads})
            )
            existing = self._load_existing_payloads(
                connection, source, account_scope, distinct_keys
            )

            by_key: dict[str, list[int]] = {}
            for index in sorted(payloads):
                by_key.setdefault(events[index].source_record_key, []).append(index)
            for key, indexes in by_key.items():
                group_payloads = {payloads[index] for index in indexes}
                if len(group_payloads) > 1:
                    for index in indexes:
                        outcome.mark(index, "conflict")
                    continue
                stored = existing.get(key)
                if stored is None:
                    outcome.mark(indexes[0], "new")
                    for index in indexes[1:]:
                        outcome.mark(index, "duplicate")
                elif stored == payloads[indexes[0]]:
                    for index in indexes:
                        outcome.mark(index, "duplicate")
                else:
                    for index in indexes:
                        outcome.mark(index, "conflict")
            outcome.mark_all_remaining("invalid")
            identity_conflict = outcome.count("conflict") > 0

            supersession_violation = False
            if not identity_conflict and contract.allows_linked_supersession:
                supersession_violation = self._apply_supersession_rules(
                    connection,
                    events,
                    payloads,
                    outcome,
                    source=source,
                    account_scope=account_scope,
                )

            failure_reason = None
            if source == "retirement_excel_v1" and outcome.new_indexes():
                from tokmo.records.pension_delta import legacy_overlaps_delta

                if legacy_overlaps_delta(
                    connection, [events[i] for i in outcome.new_indexes()]
                ):
                    failure_reason = "batch_identity_conflict"
            if identity_conflict:
                failure_reason = "batch_identity_conflict"
            elif supersession_violation:
                failure_reason = "supersession_invalid"
            elif outcome.count("invalid") > 0:
                failure_reason = "invalid_batch"
            if failure_reason is not None:
                return self._rollback_with_failure_audit(
                    connection,
                    self._classified_failure(
                        batch_id, outcome, failure_reason, stored_at
                    ),
                    source=source,
                    account_scope=account_scope,
                    import_audit=import_audit,
                )

            new_indexes = outcome.new_indexes()
            for index in new_indexes:
                fields = json.loads(payloads[index].decode("utf-8"))
                event_id = self._id_factory()
                if not _valid_text(event_id):
                    raise ValueError("store_write_failed")
                connection.execute(
                    _INSERT_EVENT,
                    (
                        event_id,
                        fields["source"],
                        fields["account_scope"],
                        fields["source_record_key"],
                        fields["event_kind"],
                        fields["supersedes_source_record_key"],
                        fields["trade_date"],
                        fields["transaction_type_raw"],
                        fields["instrument_raw"],
                        fields["quantity"],
                        fields["unit_price"],
                        fields["amount"],
                        fields["medium_raw"],
                        fields["currency_raw"],
                        fields["settlement_date"],
                        fields["fee"],
                        fields["tax"],
                        TRADE_LEDGER_PAYLOAD_VERSION,
                        payload_hash_hex(payloads[index]),
                        stored_at.isoformat(),
                        TRADE_LEDGER_SCHEMA_VERSION,
                    ),
                )
            receipt = TradeBatchSaveReceipt(
                status="SAVED",
                import_batch_id=batch_id,
                submitted_count=outcome.submitted,
                new_candidate_count=len(new_indexes),
                inserted_count=len(new_indexes),
                duplicate_count=outcome.count("duplicate"),
                conflict_count=0,
                invalid_count=0,
                reason="",
                stored_at=stored_at,
            )
            connection.execute(
                _INSERT_BATCH,
                (
                    batch_id,
                    source,
                    account_scope,
                    import_audit.file_sha256,
                    import_audit.importer_version,
                    receipt.submitted_count,
                    receipt.new_candidate_count,
                    receipt.inserted_count,
                    receipt.duplicate_count,
                    receipt.conflict_count,
                    receipt.invalid_count,
                    "SAVED",
                    "",
                    stored_at.isoformat(),
                ),
            )
            connection.execute("COMMIT")
            return receipt
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def _apply_supersession_rules(
        self,
        connection: sqlite3.Connection,
        events: tuple,
        payloads: dict[int, bytes],
        outcome: _BatchOutcome,
        *,
        source: str,
        account_scope: str,
    ) -> bool:
        """Validate linked corrections for a source that supplies targets.

        Only NEW candidates are validated: a byte-identical re-import of an
        already-stored linked correction stays a duplicate no-op. A target
        must exist in the store or earlier in the same batch and share the
        scope; self-reference and unknown targets invalidate the member; a
        second active supersession of an already-superseded target is a
        conflict unless it supersedes the prior correction in chain. Rows
        are never modified because a correction arrived.
        """
        stored = connection.execute(
            "SELECT source_record_key, supersedes_source_record_key "
            "FROM trade_ledger_events WHERE source=? AND account_scope=?",
            (source, account_scope),
        ).fetchall()
        stored_keys = {row[0] for row in stored}
        superseded = {row[1] for row in stored if row[1] is not None}
        seen_batch_keys: set[str] = set()
        violation = False
        for index in sorted(payloads):
            event = events[index]
            if (
                outcome.classes.get(index) == "new"
                and event.supersedes_source_record_key is not None
            ):
                target = event.supersedes_source_record_key
                if target == event.source_record_key or (
                    target not in stored_keys and target not in seen_batch_keys
                ):
                    outcome.mark(index, "invalid")
                    violation = True
                elif target in superseded:
                    outcome.mark(index, "conflict")
                    violation = True
                else:
                    superseded.add(target)
            if outcome.classes.get(index) in ("new", "duplicate"):
                seen_batch_keys.add(event.source_record_key)
        return violation

    def _rollback_with_failure_audit(
        self,
        connection: sqlite3.Connection,
        receipt: TradeBatchSaveReceipt,
        *,
        source: str,
        account_scope: str,
        import_audit: ImportAuditMeta,
    ) -> TradeBatchSaveReceipt:
        """Roll back a rejected batch, then attempt one sanitized failure
        audit row in its own small transaction.

        If that audit write itself fails, no durable audit is claimed: the
        receipt degrades to ``audit_write_failed`` with zero event mutation
        and no retry loop, so a reason appears on a durable audit row if
        and only if that row committed.
        """
        connection.execute("ROLLBACK")
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    _INSERT_BATCH,
                    (
                        receipt.import_batch_id,
                        source,
                        account_scope,
                        import_audit.file_sha256,
                        import_audit.importer_version,
                        receipt.submitted_count,
                        receipt.new_candidate_count,
                        receipt.inserted_count,
                        receipt.duplicate_count,
                        receipt.conflict_count,
                        receipt.invalid_count,
                        "FAILED",
                        receipt.reason,
                        receipt.stored_at.isoformat(),
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
            return receipt
        except BaseException:
            return dataclasses.replace(receipt, reason="audit_write_failed")

    def _load_existing_payloads(
        self,
        connection: sqlite3.Connection,
        source: str,
        account_scope: str,
        keys: tuple[str, ...],
    ) -> dict[str, bytes]:
        if not keys:
            return {}
        placeholders = ",".join("?" for _ in keys)
        rows = connection.execute(
            f"{_SELECT_SEMANTIC} AND source_record_key IN ({placeholders})",
            (source, account_scope, *keys),
        ).fetchall()
        found: dict[str, bytes] = {}
        for row in rows:
            event, stored_hash = _event_from_semantic_row(
                row, source=source, account_scope=account_scope
            )
            payload = canonical_payload_bytes(event)
            if payload_hash_hex(payload) != stored_hash:
                raise StoreCorruptError("store_corrupt")
            found[event.source_record_key] = payload
        return found

    def _classified_failure(
        self,
        batch_id: str,
        outcome: _BatchOutcome,
        reason: str,
        stored_at: datetime,
    ) -> TradeBatchSaveReceipt:
        return TradeBatchSaveReceipt(
            status="FAILED",
            import_batch_id=batch_id,
            submitted_count=outcome.submitted,
            new_candidate_count=outcome.count("new"),
            inserted_count=0,
            duplicate_count=outcome.count("duplicate"),
            conflict_count=outcome.count("conflict"),
            invalid_count=outcome.count("invalid"),
            reason=reason,
            stored_at=stored_at,
        )

    def _all_invalid_receipt(
        self,
        batch_id: str,
        submitted: int,
        reason: str,
        stored_at: datetime,
    ) -> TradeBatchSaveReceipt:
        """Terminal failure before/outside complete classification: every
        member is reported invalid so the count invariant holds."""
        return TradeBatchSaveReceipt(
            status="FAILED",
            import_batch_id=batch_id,
            submitted_count=submitted,
            new_candidate_count=0,
            inserted_count=0,
            duplicate_count=0,
            conflict_count=0,
            invalid_count=submitted,
            reason=reason,
            stored_at=stored_at,
        )

    # -- read -----------------------------------------------------------------

    def read_scope(
        self, *, source: str, account_scope: str
    ) -> tuple[TradeLedgerEvent, ...]:
        if not _valid_text(source) or not _valid_text(account_scope):
            raise StoreReadError("store_read_failed") from None
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StoreReadError("schema_mismatch") from None
        try:
            if (
                not self._path.exists()
                or not self._path.is_file()
                or self._path.stat().st_size == 0
            ):
                raise StoreReadError("store_read_failed")
            connection = self._connect()
            try:
                if not self._schema_valid(connection):
                    raise StoreReadError("schema_mismatch")
                rows = connection.execute(
                    f"{_SELECT_SEMANTIC} "
                    "ORDER BY trade_date, source_record_key, event_id",
                    (source, account_scope),
                ).fetchall()
            finally:
                connection.close()
            events = []
            seen_keys = set()
            for row in rows:
                event, stored_hash = _event_from_semantic_row(
                    row, source=source, account_scope=account_scope
                )
                payload = canonical_payload_bytes(event)
                if payload_hash_hex(payload) != stored_hash:
                    raise StoreCorruptError("store_corrupt")
                if event.source_record_key in seen_keys:
                    raise StoreCorruptError("store_corrupt")
                seen_keys.add(event.source_record_key)
                events.append(event)
            return tuple(events)
        except (StoreReadError, StoreCorruptError):
            raise
        except sqlite3.DatabaseError as error:
            if _sqlite_locked(error):
                raise StoreLockedError("store_locked") from None
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_NOTADB:
                raise StoreCorruptError("store_corrupt") from None
            raise StoreReadError("store_read_failed") from None
        except (OSError, ValueError):
            raise StoreReadError("store_read_failed") from None

    def read_stored_window(
        self,
        *,
        source: str,
        account_scope: str,
        from_stored_at: datetime,
        to_stored_at: datetime,
    ) -> tuple[LedgerStoredRow, ...]:
        """Read ledger events in a half-open [from_stored_at, to_stored_at)
        window, each paired with its persisted stored_at.

        The half-open interval includes ``from_stored_at`` and excludes
        ``to_stored_at``, preventing duplicate or missing rows across
        adjacent tick boundaries. All datetimes must be timezone-aware.
        Returns rows ordered by stored_at, then source_record_key.

        Raises typed exceptions (StoreReadError/StoreCorruptError/
        StoreLockedError) — never returns an empty list to mask a
        corrupt or unreadable store.
        """
        if not _valid_text(source) or not _valid_text(account_scope):
            raise StoreReadError("store_read_failed") from None
        if not _aware(from_stored_at) or not _aware(to_stored_at):
            raise StoreReadError("store_read_failed") from None
        if from_stored_at > to_stored_at:
            raise StoreReadError("store_read_failed") from None
        from_stored_at = from_stored_at.astimezone(UTC)
        to_stored_at = to_stored_at.astimezone(UTC)
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise StoreReadError("schema_mismatch") from None
        try:
            if (
                not self._path.exists()
                or not self._path.is_file()
                or self._path.stat().st_size == 0
            ):
                raise StoreReadError("store_read_failed")
            connection = self._connect()
            try:
                if not self._schema_valid(connection):
                    raise StoreReadError("schema_mismatch")
                rows = connection.execute(
                    _SELECT_WINDOW,
                    (
                        source,
                        account_scope,
                        from_stored_at.isoformat(),
                        to_stored_at.isoformat(),
                    ),
                ).fetchall()
            finally:
                connection.close()
            return _stored_rows_from_window_rows(
                rows,
                source=source,
                account_scope=account_scope,
            )
        except (StoreReadError, StoreCorruptError):
            raise
        except sqlite3.DatabaseError as error:
            if _sqlite_locked(error):
                raise StoreLockedError("store_locked") from None
            if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_NOTADB:
                raise StoreCorruptError("store_corrupt") from None
            raise StoreReadError("store_read_failed") from None
        except (OSError, ValueError):
            raise StoreReadError("store_read_failed") from None


# -- candidate → canonical promotion ------------------------------------------
#
# Each (source, account_scope) has exactly one authoritative canonical DB
# path. ``os.link`` is the atomic no-replace commit point; the parent
# directory fsync is the promotion completion point. The candidate name
# remains a same-inode staging alias — never a second history — and its
# later cleanup is separate, non-authoritative housekeeping. Errors carry
# closed tokens only; no path ever rides on an exception.


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while chunk := handle.read(65536):
            digest.update(chunk)
    return digest.hexdigest()


def finalize_candidate(candidate_path: Path | str, canonical_path: Path | str) -> None:
    candidate = Path(candidate_path).resolve()
    canonical = Path(canonical_path).resolve()

    if candidate.parent != canonical.parent:
        raise ValueError("directory_mismatch")
    if not candidate.exists():
        raise FileNotFoundError("candidate_missing")
    if canonical.exists():
        raise FileExistsError("canonical_exists")

    connection = sqlite3.connect(str(candidate))
    try:
        result = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        if result[0] != 0 or result[1] != result[2]:
            raise RuntimeError("wal_checkpoint_failed")
    finally:
        connection.close()

    wal_file = candidate.with_name(candidate.name + "-wal")
    shm_file = candidate.with_name(candidate.name + "-shm")
    if wal_file.exists() or shm_file.exists():
        raise RuntimeError("sidecars_present")

    candidate_fd = os.open(str(candidate), os.O_RDWR)
    try:
        try:
            os.fsync(candidate_fd)
        except Exception:
            raise RuntimeError("store_write_failed") from None
    finally:
        os.close(candidate_fd)

    try:
        os.link(str(candidate), str(canonical))
    except Exception:
        raise RuntimeError("promotion_link_failed") from None

    directory_fd = os.open(str(candidate.parent), os.O_RDONLY)
    try:
        try:
            os.fsync(directory_fd)
        except Exception:
            # Both names remain, same inode; durability is unproven until
            # recovery repeats the directory fsync.
            raise RuntimeError("promotion_recovery_required") from None
    finally:
        os.close(directory_fd)


def recover_promotion(candidate_path: Path | str, canonical_path: Path | str) -> None:
    candidate = Path(candidate_path).resolve()
    canonical = Path(canonical_path).resolve()

    if not candidate.exists() or not canonical.exists():
        return

    if candidate.stat().st_ino != canonical.stat().st_ino:
        raise RuntimeError("store_corrupt")
    if _file_sha256(candidate) != _file_sha256(canonical):
        raise RuntimeError("store_corrupt")

    directory_fd = os.open(str(candidate.parent), os.O_RDONLY)
    try:
        try:
            os.fsync(directory_fd)
        except Exception:
            raise RuntimeError("promotion_recovery_required") from None
    finally:
        os.close(directory_fd)


def cleanup_candidate_alias(
    candidate_path: Path | str, canonical_path: Path | str
) -> None:
    candidate = Path(candidate_path).resolve()
    canonical = Path(canonical_path).resolve()

    if not candidate.exists():
        return
    if not canonical.exists() or candidate.stat().st_ino != canonical.stat().st_ino:
        raise RuntimeError("store_corrupt")
    try:
        os.unlink(str(candidate))
    except Exception:
        raise RuntimeError("store_write_failed") from None
