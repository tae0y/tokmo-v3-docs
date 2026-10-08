"""Failure-path hardening for the trade-ledger store.

Covers the failure-audit contract (one sanitized audit row per rejected
batch, with the ``audit_write_failed`` fallback when that write itself
fails), linked-supersession rules for a synthetic future source, stored-row
hash reconstruction and corruption detection, bounded locking, forced
commit failure, and privacy of every receipt/exception surface. Synthetic
fixtures and private temporary paths only.
"""

import itertools
import sqlite3
import stat
import time
from datetime import date, datetime, timedelta, timezone

import pytest

import tokmo.records.trade_ledger_store as trade_ledger_store
from tokmo.records.trade_ledger_preflight import SOURCE_CONTRACTS, SourceContract
from tokmo.records.trade_ledger_privacy import (
    StoreCorruptError,
    StoreLockedError,
    StoreReadError,
)
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent


NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc)
SOURCE = "retirement_excel_v1"
SCOPE = "retirement_pension_1"
_ID_COUNTER = itertools.count(1)

FUTURE = SourceContract(
    source="fixture_future_source_1",
    contract_version=1,
    account_scopes=frozenset({"fixture_scope_1"}),
    key_pattern=r"fix-[a-z0-9]{4}",
    sequential_full_history=False,
    allows_linked_supersession=True,
)


def audit(sha="ab" * 32):
    return ImportAuditMeta(file_sha256=sha, importer_version="fixture-importer-1")


def ledger_event(number, **overrides):
    values = dict(
        source=SOURCE,
        account_scope=SCOPE,
        source_record_key=f"row-{number:06d}",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 1, 1) + timedelta(days=number % 7),
        transaction_type_raw="ETF매수",
        instrument_raw=f"Fixture ETF {number % 4}",
        quantity=str(1 + number % 5),
        unit_price=str(1000 + number),
        amount=str((1 + number % 5) * (1000 + number)),
        medium_raw="FixtureApp",
    )
    values.update(overrides)
    return TradeLedgerEvent(**values)


def future_event(key, *, kind="EXECUTION", supersedes=None, amount="10"):
    return TradeLedgerEvent(
        source=FUTURE.source,
        account_scope="fixture_scope_1",
        source_record_key=key,
        event_kind=kind,
        supersedes_source_record_key=supersedes,
        trade_date=date(2026, 2, 2),
        transaction_type_raw="FIXTURE-KIND",
        instrument_raw="Fixture Linked Instrument",
        quantity="1",
        unit_price="10",
        amount=amount,
        medium_raw="FixtureFeed",
        currency_raw="FIX",
        settlement_date=date(2026, 2, 4),
        fee="0.5",
        tax="0",
    )


def make_store(path, *, clock=None, contracts=None):
    return TradeLedgerStore(
        path,
        clock=clock or (lambda: NOW),
        id_factory=lambda: f"fixture-id-{next(_ID_COUNTER):08d}",
        source_contracts=contracts,
    )


def future_store(path):
    return make_store(
        path, contracts={**SOURCE_CONTRACTS, FUTURE.source: FUTURE}
    )


def counts(receipt):
    return (
        receipt.submitted_count,
        receipt.new_candidate_count,
        receipt.inserted_count,
        receipt.duplicate_count,
        receipt.conflict_count,
        receipt.invalid_count,
    )


def event_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT source_record_key, quantity, unit_price, amount "
            "FROM trade_ledger_events ORDER BY source_record_key"
        ).fetchall()


def batch_rows(path):
    with sqlite3.connect(path) as connection:
        return connection.execute(
            "SELECT status, reason, submitted_count, new_candidate_count, "
            "inserted_count, duplicate_count, conflict_count, invalid_count "
            "FROM trade_ledger_import_batches ORDER BY import_batch_id"
        ).fetchall()


class FailingConnection:
    """Wraps a real connection; fails statements matching ``marker`` once
    ``arm_after`` (if any) has been seen."""

    def __init__(self, connection, marker, arm_after=None):
        self.connection = connection
        self.marker = marker
        self.arm_after = arm_after
        self.armed = arm_after is None

    def execute(self, sql, parameters=()):
        if self.armed and self.marker in sql:
            raise sqlite3.OperationalError("fixture injected failure")
        if self.arm_after is not None and self.arm_after in sql:
            self.armed = True
        return self.connection.execute(sql, parameters)

    def executemany(self, sql, parameters):
        return self.connection.executemany(sql, parameters)

    def close(self):
        self.connection.close()


# --- failure audit -----------------------------------------------------------

def test_rejected_batch_persists_one_sanitized_failure_audit_row(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = (ledger_event(1), ledger_event(2))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"

    drifted = (stored[0], ledger_event(2, amount="9999"))
    receipt = store.append_batch(drifted, import_audit=audit())

    assert receipt.status == "FAILED"
    assert receipt.reason == "batch_identity_conflict"
    assert batch_rows(path) == [
        ("SAVED", "", 2, 2, 2, 0, 0, 0),
        ("FAILED", "batch_identity_conflict", 2, 0, 0, 1, 1, 0),
    ]
    assert len(event_rows(path)) == 2


def test_preflight_rejection_also_persists_a_failure_audit_row(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(50),), import_audit=audit()
    )
    assert receipt.reason == "source_contract_violation"
    assert batch_rows(path) == [
        ("FAILED", "source_contract_violation", 1, 0, 0, 0, 0, 1),
    ]
    assert event_rows(path) == []


def test_failure_audit_write_failure_degrades_to_audit_write_failed(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = (ledger_event(1), ledger_event(2))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"
    before = event_rows(path)
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return FailingConnection(
            real_connect(*args, **kwargs),
            "INSERT INTO trade_ledger_import_batches",
            arm_after="ROLLBACK",
        )

    monkeypatch.setattr(
        trade_ledger_store.sqlite3, "connect", failing_connect
    )
    drifted = (stored[0], ledger_event(2, amount="9999"))
    receipt = store.append_batch(drifted, import_audit=audit())
    monkeypatch.setattr(trade_ledger_store.sqlite3, "connect", real_connect)

    assert receipt.status == "FAILED"
    assert receipt.reason == "audit_write_failed"
    assert counts(receipt) == (2, 0, 0, 1, 1, 0)
    assert event_rows(path) == before
    # The receipt and the persisted audit can never disagree: no FAILED
    # audit row exists for this batch.
    assert batch_rows(path) == [("SAVED", "", 2, 2, 2, 0, 0, 0)]


def test_store_level_failures_do_not_masquerade_as_audit_failures(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    original = (ledger_event(1), ledger_event(2))
    assert store.append_batch(original, import_audit=audit()).status == "SAVED"
    real_connect = sqlite3.connect

    def failing_connect(*args, **kwargs):
        return FailingConnection(real_connect(*args, **kwargs), "COMMIT")

    monkeypatch.setattr(
        trade_ledger_store.sqlite3, "connect", failing_connect
    )
    receipt = store.append_batch(
        original + (ledger_event(3),), import_audit=audit()
    )
    monkeypatch.setattr(trade_ledger_store.sqlite3, "connect", real_connect)

    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"
    assert receipt.inserted_count == 0
    assert event_rows(path) == [
        (event.source_record_key, event.quantity, event.unit_price,
         event.amount)
        for event in original
    ]
    assert batch_rows(path) == [("SAVED", "", 2, 2, 2, 0, 0, 0)]


# --- linked supersession (synthetic future source) ---------------------------

def test_valid_linked_correction_chain_appends_atomically(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = future_store(path)
    first = (
        future_event("fix-aaaa"),
        future_event("fix-bbbb", kind="CORRECTION", supersedes="fix-aaaa"),
    )
    receipt = store.append_batch(first, import_audit=audit())
    assert receipt.status == "SAVED"
    assert counts(receipt) == (2, 2, 2, 0, 0, 0)

    chained = store.append_batch(
        (future_event(
            "fix-cccc", kind="CORRECTION", supersedes="fix-bbbb"
        ),),
        import_audit=audit(),
    )
    assert chained.status == "SAVED"

    read_back = store.read_scope(
        source=FUTURE.source, account_scope="fixture_scope_1"
    )
    assert len(read_back) == 3
    assert read_back[1].supersedes_source_record_key == "fix-aaaa"
    assert read_back[1].currency_raw == "FIX"
    assert read_back[1].settlement_date == date(2026, 2, 4)
    assert read_back[1].fee == "0.5"
    assert read_back[1].tax == "0"


def test_reimporting_an_identical_linked_correction_is_a_duplicate_noop(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = future_store(path)
    first = (
        future_event("fix-aaaa"),
        future_event("fix-bbbb", kind="CORRECTION", supersedes="fix-aaaa"),
    )
    assert store.append_batch(first, import_audit=audit()).status == "SAVED"

    receipt = store.append_batch(first, import_audit=audit())
    assert receipt.status == "SAVED"
    assert counts(receipt) == (2, 0, 0, 2, 0, 0)


def test_second_supersession_of_a_superseded_target_is_a_conflict(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = future_store(path)
    assert store.append_batch(
        (
            future_event("fix-aaaa"),
            future_event(
                "fix-bbbb", kind="CORRECTION", supersedes="fix-aaaa"
            ),
        ),
        import_audit=audit(),
    ).status == "SAVED"
    before = event_rows(path)

    receipt = store.append_batch(
        (future_event(
            "fix-dddd", kind="CORRECTION", supersedes="fix-aaaa"
        ),),
        import_audit=audit(),
    )

    assert receipt.status == "FAILED"
    assert receipt.reason == "supersession_invalid"
    assert counts(receipt) == (1, 0, 0, 0, 1, 0)
    assert event_rows(path) == before


def test_missing_self_and_later_batch_targets_fail_atomically(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = future_store(path)

    missing = store.append_batch(
        (future_event(
            "fix-aaaa", kind="CORRECTION", supersedes="fix-zzzz"
        ),),
        import_audit=audit(),
    )
    assert (missing.status, missing.reason) == (
        "FAILED", "supersession_invalid"
    )
    assert counts(missing) == (1, 0, 0, 0, 0, 1)

    selfref = store.append_batch(
        (future_event(
            "fix-aaaa", kind="CORRECTION", supersedes="fix-aaaa"
        ),),
        import_audit=audit(),
    )
    assert (selfref.status, selfref.reason) == (
        "FAILED", "supersession_invalid"
    )

    later_target = store.append_batch(
        (
            future_event(
                "fix-aaaa", kind="CORRECTION", supersedes="fix-bbbb"
            ),
            future_event("fix-bbbb"),
        ),
        import_audit=audit(),
    )
    assert (later_target.status, later_target.reason) == (
        "FAILED", "supersession_invalid"
    )
    assert counts(later_target) == (2, 1, 0, 0, 0, 1)
    assert event_rows(path) == []


def test_supersession_never_mutates_the_superseded_row(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = future_store(path)
    original = future_event("fix-aaaa")
    assert store.append_batch(
        (original,), import_audit=audit()
    ).status == "SAVED"
    assert store.append_batch(
        (future_event("fix-bbbb", kind="CORRECTION", supersedes="fix-aaaa"),),
        import_audit=audit(),
    ).status == "SAVED"

    read_back = store.read_scope(
        source=FUTURE.source, account_scope="fixture_scope_1"
    )
    assert original in read_back


# --- stored-row reconstruction and corruption --------------------------------

def test_tampered_payload_hash_is_store_corrupt_on_append_and_read(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = (ledger_event(1), ledger_event(2))
    assert store.append_batch(events, import_audit=audit()).status == "SAVED"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE trade_ledger_events SET payload_hash=? "
            "WHERE source_record_key=?",
            ("00" * 32, "row-000002"),
        )

    receipt = store.append_batch(events, import_audit=audit())
    assert (receipt.status, receipt.reason) == ("FAILED", "store_corrupt")

    with pytest.raises(StoreCorruptError) as captured:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert captured.value.reason == "store_corrupt"


def test_tampered_semantic_column_is_store_corrupt_not_conflict(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = (ledger_event(1), ledger_event(2))
    assert store.append_batch(events, import_audit=audit()).status == "SAVED"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE trade_ledger_events SET amount='4242' "
            "WHERE source_record_key=?",
            ("row-000002",),
        )

    receipt = store.append_batch(events, import_audit=audit())
    assert (receipt.status, receipt.reason) == ("FAILED", "store_corrupt")
    assert receipt.conflict_count == 0

    with pytest.raises(StoreCorruptError):
        store.read_scope(source=SOURCE, account_scope=SCOPE)


def test_semantically_malformed_row_is_store_corrupt_on_read(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE trade_ledger_events SET trade_date='fixture-not-a-date'"
        )

    with pytest.raises(StoreCorruptError) as captured:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert captured.value.reason == "store_corrupt"
    assert captured.value.__cause__ is None


def test_garbage_file_is_store_corrupt_and_preserved(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "ledger.sqlite3"
    original = b"fixture-corrupt-trade-ledger"
    path.write_bytes(original)
    path.chmod(0o600)
    store = make_store(path)

    receipt = store.append_batch((ledger_event(1),), import_audit=audit())
    assert (receipt.status, receipt.reason) == ("FAILED", "store_corrupt")
    assert path.read_bytes() == original

    with pytest.raises(StoreCorruptError):
        store.read_scope(source=SOURCE, account_scope=SCOPE)


# --- locking and environment -------------------------------------------------

def test_lock_is_bounded_and_leaves_the_ledger_unchanged(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    before = event_rows(path)

    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        receipt = store.append_batch(
            (ledger_event(1), ledger_event(2)), import_audit=audit()
        )
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert receipt.reason == "store_locked"
    assert time.monotonic() - started < 4.0
    assert event_rows(path) == before


def test_read_works_under_another_writers_begin_immediate(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    before = path.read_bytes()
    journal = path.with_name(path.name + "-journal")

    blocker = sqlite3.connect(path, isolation_level=None)
    assert blocker.execute("PRAGMA journal_mode=DELETE").fetchone() == (
        "delete",
    )
    blocker.execute("BEGIN IMMEDIATE")
    try:
        read_back = store.read_scope(source=SOURCE, account_scope=SCOPE)
        assert not journal.exists()
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()

    assert len(read_back) == 1
    assert path.read_bytes() == before


def test_old_sqlite_gate_fails_closed_for_append_and_read(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    monkeypatch.setattr(
        trade_ledger_store.sqlite3, "sqlite_version_info", (3, 36, 0)
    )
    receipt = store.append_batch((ledger_event(1),), import_audit=audit())
    assert receipt.reason == "schema_mismatch"
    assert not path.exists()
    with pytest.raises(StoreReadError) as captured:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert captured.value.reason == "schema_mismatch"


def test_unsafe_parent_permissions_fail_before_initialization(tmp_path):
    unsafe = tmp_path / "unsafe"
    unsafe.mkdir(mode=0o755)
    unsafe.chmod(0o755)
    path = unsafe / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(1),), import_audit=audit()
    )
    assert receipt.reason == "store_write_failed"
    assert not path.exists()


def test_blocked_ancestor_path_is_a_closed_write_failure(tmp_path):
    ancestor = tmp_path / "not-a-directory"
    ancestor.write_bytes(b"fixture")
    path = ancestor / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(1),), import_audit=audit()
    )
    assert (receipt.status, receipt.reason) == ("FAILED", "store_write_failed")


def test_rollback_journal_is_private_while_writing(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    connection = sqlite3.connect(path, isolation_level=None)
    journal = path.with_name(path.name + "-journal")
    connection.execute("PRAGMA journal_mode=DELETE")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "UPDATE trade_ledger_import_batches SET stored_at='2026-07-28'"
    )
    assert journal.exists()
    assert stat.S_IMODE(journal.stat().st_mode) == 0o600
    connection.execute("ROLLBACK")
    connection.close()
    assert not journal.exists()


# --- privacy -----------------------------------------------------------------

def test_no_receipt_or_error_surface_leaks_values_paths_or_sql(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = (ledger_event(1), ledger_event(2))
    saved = store.append_batch(events, import_audit=audit())
    drifted = store.append_batch(
        (events[0], ledger_event(2, amount="9999")), import_audit=audit()
    )
    preflight = store.append_batch(
        (ledger_event(9),), import_audit=audit()
    )
    with sqlite3.connect(path) as connection:
        connection.execute(
            "UPDATE trade_ledger_events SET payload_hash=?", ("00" * 32,)
        )
    corrupt = store.append_batch(events, import_audit=audit())
    try:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
        read_error = None
    except StoreCorruptError as error:
        read_error = error

    rendered = "".join((
        repr(saved), str(saved),
        repr(drifted), str(drifted),
        repr(preflight), str(preflight),
        repr(corrupt), str(corrupt),
        repr(read_error), str(read_error),
        repr(store),
    ))
    for private_value in (
        "row-000001", "row-000002", "Fixture", "매수", "1001", "1002",
        "2026-01", str(path), str(tmp_path), "SELECT", "INSERT",
        "sqlite3", "file is not a database", "UNIQUE constraint",
    ):
        assert private_value not in rendered
    assert read_error.__cause__ is None
    assert read_error.args == ("store_corrupt",)


def test_locked_and_read_errors_expose_reason_tokens_only(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    with pytest.raises(StoreReadError) as captured:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert repr(captured.value) == "<StoreReadError reason='store_read_failed'>"

    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    blocker = sqlite3.connect(path, isolation_level=None)
    blocker.execute("PRAGMA locking_mode=EXCLUSIVE")
    blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(StoreLockedError) as locked:
            store.read_scope(source=SOURCE, account_scope=SCOPE)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert locked.value.reason == "store_locked"
