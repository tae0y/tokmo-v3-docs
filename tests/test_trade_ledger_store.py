"""Core append/read behavior of the trade-ledger SQLite store.

Synthetic fixtures only, in the observed seven-column shape; private
temporary paths only. Covers STRICT schema creation and permissions,
atomic batch append, exact read-back order, duplicate/tail/prefix
re-imports, fail-closed conflicts, source-contract preflight, the five
worked count examples, and architecture import boundaries.
"""

import ast
import itertools
import sqlite3
import stat
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.trade_ledger import ImportAuditMeta, TradeLedgerEvent


NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc)
SOURCE = "retirement_excel_v1"
SCOPE = "retirement_pension_1"


def audit(sha="ab" * 32, version="fixture-importer-1"):
    return ImportAuditMeta(file_sha256=sha, importer_version=version)


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
        currency_raw=None,
        settlement_date=None,
        fee=None,
        tax=None,
    )
    values.update(overrides)
    return TradeLedgerEvent(**values)


def observed_shape_batch():
    """85 synthetic events mirroring the observed type distribution:
    64 buy-typed, 18 sell-typed, 3 unlinked corrections with raw asterisk
    types."""
    events = []
    for number in range(1, 65):
        events.append(ledger_event(number))
    for number in range(65, 83):
        events.append(ledger_event(number, transaction_type_raw="ETF매도"))
    events.append(
        ledger_event(
            83, event_kind="CORRECTION", transaction_type_raw="매수정정*"
        )
    )
    for number in (84, 85):
        events.append(
            ledger_event(
                number,
                event_kind="CORRECTION",
                transaction_type_raw="매도정정*",
                quantity="-1",
                amount="-1001",
            )
        )
    return tuple(events)


# Shared across every store instance: opaque IDs must never collide across
# process reopens, mirroring the uuid factory injected by a composition root.
_ID_COUNTER = itertools.count(1)


def make_store(path, *, clock=None):
    return TradeLedgerStore(
        path,
        clock=clock or (lambda: NOW),
        id_factory=lambda: f"fixture-id-{next(_ID_COUNTER):08d}",
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


def read_order_key(event):
    return (event.trade_date.isoformat(), event.source_record_key)


# --- schema, permissions, reopen ---------------------------------------------

def test_fresh_store_creates_strict_schema_with_private_permissions(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)

    receipt = store.append_batch(
        (ledger_event(1),), import_audit=audit()
    )

    assert receipt.status == "SAVED"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    with sqlite3.connect(path) as connection:
        table_list = {
            row[1]: row for row in connection.execute("PRAGMA table_list")
        }
        assert table_list["trade_ledger_events"][5] == 1  # STRICT
        assert table_list["trade_ledger_import_batches"][5] == 1
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE sql IS NOT NULL"
            )
        }
    assert names == {
        "trade_ledger_events",
        "trade_ledger_import_batches",
        "trade_ledger_scope_order",
    }

    reopened = make_store(path)
    again = reopened.append_batch((ledger_event(1),), import_audit=audit())
    assert again.status == "SAVED"
    assert counts(again) == (1, 0, 0, 1, 0, 0)


def test_strict_mode_rejects_wrongly_typed_column_values(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"

    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE trade_ledger_events SET schema_version='fixture'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE trade_ledger_events SET event_kind='REVERSAL'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE trade_ledger_events SET schema_version=2"
            )


def test_foreign_schema_is_schema_mismatch_and_untouched(tmp_path):
    private = tmp_path / "private"
    private.mkdir(mode=0o700)
    path = private / "ledger.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute(
            "CREATE TABLE trade_ledger_events (event_id TEXT PRIMARY KEY)"
        )
    path.chmod(0o600)
    before = path.read_bytes()

    receipt = make_store(path).append_batch(
        (ledger_event(1),), import_audit=audit()
    )

    assert receipt.status == "FAILED"
    assert receipt.reason == "schema_mismatch"
    assert receipt.inserted_count == 0
    assert path.read_bytes() == before


# --- core append and read-back -----------------------------------------------

def test_observed_shape_batch_saves_in_one_commit_and_reads_back_exactly(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = observed_shape_batch()

    receipt = store.append_batch(events, import_audit=audit())

    assert receipt.status == "SAVED"
    assert counts(receipt) == (85, 85, 85, 0, 0, 0)
    assert receipt.reason == ""

    read_back = store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert read_back == tuple(sorted(events, key=read_order_key))
    corrections = [
        event for event in read_back if event.event_kind == "CORRECTION"
    ]
    assert len(corrections) == 3
    assert all(
        event.supersedes_source_record_key is None for event in corrections
    )
    assert {event.transaction_type_raw for event in corrections} == {
        "매수정정*", "매도정정*"
    }


def test_nullable_fields_round_trip_as_null_never_defaulted(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"

    (read_back,) = store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert read_back.currency_raw is None
    assert read_back.settlement_date is None
    assert read_back.fee is None
    assert read_back.tax is None
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT currency_raw, settlement_date, fee, tax "
            "FROM trade_ledger_events"
        ).fetchone()
    assert row == (None, None, None, None)


def test_non_positive_values_round_trip_losslessly(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    negative = ledger_event(
        1,
        event_kind="CORRECTION",
        transaction_type_raw="매도정정*",
        quantity="-2",
        unit_price="0",
        amount="-2501.5",
    )

    receipt = store.append_batch((negative,), import_audit=audit())

    assert receipt.status == "SAVED"
    (read_back,) = store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert read_back.quantity == "-2"
    assert read_back.unit_price == "0"
    assert read_back.amount == "-2501.5"


def test_stored_at_is_storage_metadata_from_the_injected_clock(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    receipt = store.append_batch((ledger_event(1),), import_audit=audit())

    assert receipt.stored_at == NOW
    with sqlite3.connect(path) as connection:
        stored = connection.execute(
            "SELECT stored_at, trade_date FROM trade_ledger_events"
        ).fetchone()
    assert stored[0] == NOW.isoformat()
    assert stored[1] == ledger_event(1).trade_date.isoformat()
    assert stored[0] != stored[1]


# --- duplicate / tail / prefix re-imports ------------------------------------

def test_same_export_reimport_is_duplicate_noop(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = observed_shape_batch()
    assert store.append_batch(events, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    receipt = store.append_batch(events, import_audit=audit())

    assert receipt.status == "SAVED"
    assert counts(receipt) == (85, 0, 0, 85, 0, 0)
    assert event_rows(path) == before


def test_renamed_file_reimport_is_duplicate_noop_with_two_audit_rows(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = observed_shape_batch()
    first = store.append_batch(events, import_audit=audit(sha="ab" * 32))
    second = store.append_batch(events, import_audit=audit(sha="cd" * 32))

    assert first.status == "SAVED"
    assert second.status == "SAVED"
    assert counts(second) == (85, 0, 0, 85, 0, 0)
    assert first.import_batch_id != second.import_batch_id
    with sqlite3.connect(path) as connection:
        batches = connection.execute(
            "SELECT import_batch_id, file_sha256, status, inserted_count, "
            "duplicate_count FROM trade_ledger_import_batches "
            "ORDER BY import_batch_id"
        ).fetchall()
        logical = connection.execute(
            "SELECT count(*) FROM trade_ledger_events"
        ).fetchone()
    assert len(batches) == 2
    assert {row[1] for row in batches} == {"ab" * 32, "cd" * 32}
    assert all(row[2] == "SAVED" for row in batches)
    assert [row[3] for row in sorted(batches)] == [85, 0]
    assert logical == (85,)


def test_cumulative_tail_append_commits_prefix_duplicates_and_new_tail(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    prefix = tuple(ledger_event(number) for number in (1, 2, 3))
    assert store.append_batch(prefix, import_audit=audit()).status == "SAVED"

    cumulative = prefix + (ledger_event(4), ledger_event(5))
    receipt = store.append_batch(cumulative, import_audit=audit())

    assert receipt.status == "SAVED"
    assert counts(receipt) == (5, 2, 2, 3, 0, 0)
    assert [row[0] for row in event_rows(path)] == [
        f"row-{number:06d}" for number in (1, 2, 3, 4, 5)
    ]


def test_shorter_exact_prefix_reimport_is_duplicate_noop(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    full = tuple(ledger_event(number) for number in (1, 2, 3, 4, 5))
    assert store.append_batch(full, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    receipt = store.append_batch(full[:3], import_audit=audit())

    assert receipt.status == "SAVED"
    assert counts(receipt) == (3, 0, 0, 3, 0, 0)
    assert event_rows(path) == before


def test_unordered_but_complete_tuple_has_identical_outcome(tmp_path):
    ordered_path = tmp_path / "ordered" / "ledger.sqlite3"
    shuffled_path = tmp_path / "shuffled" / "ledger.sqlite3"
    events = tuple(ledger_event(number) for number in (1, 2, 3, 4, 5))
    shuffled = tuple(events[index] for index in (3, 0, 4, 2, 1))

    ordered_receipt = make_store(ordered_path).append_batch(
        events, import_audit=audit()
    )
    shuffled_receipt = make_store(shuffled_path).append_batch(
        shuffled, import_audit=audit()
    )

    assert ordered_receipt.status == shuffled_receipt.status == "SAVED"
    assert counts(ordered_receipt) == counts(shuffled_receipt) == (
        5, 5, 5, 0, 0, 0
    )
    assert make_store(ordered_path).read_scope(
        source=SOURCE, account_scope=SCOPE
    ) == make_store(shuffled_path).read_scope(
        source=SOURCE, account_scope=SCOPE
    )


# --- fail-closed conflicts ---------------------------------------------------

def test_prefix_drift_edited_value_fails_whole_batch_with_zero_mutation(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = tuple(ledger_event(number) for number in (1, 2, 3))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    drifted = (
        stored[0],
        ledger_event(2, amount="9999"),
        stored[2],
        ledger_event(4),
    )
    receipt = store.append_batch(drifted, import_audit=audit())

    assert receipt.status == "FAILED"
    assert receipt.reason == "batch_identity_conflict"
    assert counts(receipt) == (4, 1, 0, 2, 1, 0)
    assert event_rows(path) == before


def test_deleted_row_sequence_shift_fails_whole_batch(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = tuple(ledger_event(number) for number in (1, 2, 3))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    # Row 2 deleted at the source: old row 3 shifts under key row-000002.
    shifted = (
        stored[0],
        ledger_event(2, unit_price=stored[2].unit_price,
                     amount=stored[2].amount,
                     quantity=stored[2].quantity),
    )
    receipt = store.append_batch(shifted, import_audit=audit())

    assert receipt.status == "FAILED"
    assert receipt.reason == "batch_identity_conflict"
    assert counts(receipt) == (2, 0, 0, 1, 1, 0)
    assert event_rows(path) == before


def test_inserted_and_reordered_rows_fail_whole_batch(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = tuple(ledger_event(number) for number in (1, 2))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    reordered = (
        ledger_event(1, quantity=stored[1].quantity,
                     unit_price=stored[1].unit_price,
                     amount=stored[1].amount),
        ledger_event(2, quantity=stored[0].quantity,
                     unit_price=stored[0].unit_price,
                     amount=stored[0].amount),
    )
    receipt = store.append_batch(reordered, import_audit=audit())

    assert receipt.status == "FAILED"
    assert receipt.reason == "batch_identity_conflict"
    assert counts(receipt) == (2, 0, 0, 0, 2, 0)
    assert event_rows(path) == before


def test_conflicted_import_never_deletes_or_rewrites_rows(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    events = observed_shape_batch()
    assert store.append_batch(events, import_audit=audit()).status == "SAVED"
    before = store.read_scope(source=SOURCE, account_scope=SCOPE)

    drifted = tuple(
        ledger_event(
            number,
            quantity="9",
            unit_price="9",
            amount="81",
        )
        for number in range(1, 86)
    )
    receipt = store.append_batch(drifted, import_audit=audit())

    assert receipt.status == "FAILED"
    assert store.read_scope(source=SOURCE, account_scope=SCOPE) == before


# --- worked count examples (frozen in the closed plan) -----------------------

def test_worked_example_1_three_new_events(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        tuple(ledger_event(number) for number in (1, 2, 3)),
        import_audit=audit(),
    )
    assert receipt.status == "SAVED"
    assert counts(receipt) == (3, 3, 3, 0, 0, 0)


def test_worked_example_2_identical_prefix_plus_tail(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    prefix = tuple(ledger_event(number) for number in (1, 2))
    assert store.append_batch(prefix, import_audit=audit()).status == "SAVED"

    receipt = store.append_batch(
        prefix + (ledger_event(3),), import_audit=audit()
    )
    assert receipt.status == "SAVED"
    assert counts(receipt) == (3, 1, 1, 2, 0, 0)


def test_worked_example_3_same_key_twice_identical_collapses(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(1), ledger_event(1)), import_audit=audit()
    )
    assert receipt.status == "SAVED"
    assert counts(receipt) == (2, 1, 1, 1, 0, 0)
    assert len(event_rows(path)) == 1


def test_worked_example_4_same_key_twice_differing_fails_batch(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (
            ledger_event(1),
            ledger_event(1, amount="8888"),
            ledger_event(2),
        ),
        import_audit=audit(),
    )
    assert receipt.status == "FAILED"
    assert receipt.reason == "batch_identity_conflict"
    assert counts(receipt) == (3, 1, 0, 0, 2, 0)
    assert event_rows(path) == []


def test_worked_example_5_invalid_member_fails_batch_with_no_earlier_row(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (
            ledger_event(1),
            ledger_event(2),
            ledger_event(3, quantity="01"),
        ),
        import_audit=audit(),
    )
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_batch"
    assert counts(receipt) == (3, 2, 0, 0, 0, 1)
    assert event_rows(path) == []


# --- source-contract preflight -----------------------------------------------

def test_fresh_store_batch_not_starting_at_one_fails_closed(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(50),), import_audit=audit()
    )
    assert receipt.status == "FAILED"
    assert receipt.reason == "source_contract_violation"
    assert counts(receipt) == (1, 0, 0, 0, 0, 1)
    assert event_rows(path) == []


def test_gap_and_detached_tail_fail_closed_with_zero_mutation(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    stored = tuple(ledger_event(number) for number in (1, 2, 3))
    assert store.append_batch(stored, import_audit=audit()).status == "SAVED"
    before = event_rows(path)

    detached = store.append_batch(
        (ledger_event(100),), import_audit=audit()
    )
    gapped = store.append_batch(
        stored + (ledger_event(5),), import_audit=audit()
    )

    for receipt in (detached, gapped):
        assert receipt.status == "FAILED"
        assert receipt.reason == "source_contract_violation"
        assert receipt.inserted_count == 0
    assert counts(detached) == (1, 0, 0, 0, 0, 1)
    assert counts(gapped) == (4, 0, 0, 0, 0, 4)
    assert event_rows(path) == before


@pytest.mark.parametrize(
    "bad_key", ["abc", "row-1", "row-0000001", "row-000000", "ROW-000001"]
)
def test_malformed_keys_fail_closed(tmp_path, bad_key):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(1), ledger_event(2, source_record_key=bad_key)),
        import_audit=audit(),
    )
    assert receipt.status == "FAILED"
    assert receipt.reason == "source_contract_violation"
    assert receipt.inserted_count == 0
    assert event_rows(path) == []


def test_unregistered_source_and_scope_fail_closed(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    unregistered_source = make_store(path).append_batch(
        (
            ledger_event(
                1,
                source="fixture_unregistered_source",
                account_scope="fixture_scope",
            ),
        ),
        import_audit=audit(),
    )
    unregistered_scope = make_store(path).append_batch(
        (ledger_event(1, account_scope="fixture_other_scope"),),
        import_audit=audit(),
    )
    for receipt in (unregistered_source, unregistered_scope):
        assert receipt.status == "FAILED"
        assert receipt.reason == "source_contract_violation"
        assert counts(receipt) == (1, 0, 0, 0, 0, 1)
    assert event_rows(path) == []


def test_linked_supersession_is_a_contract_violation_for_the_excel_source(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    before = event_rows(path)

    linked = ledger_event(
        2,
        event_kind="CORRECTION",
        transaction_type_raw="매수정정*",
        supersedes_source_record_key="row-000001",
    )
    receipt = store.append_batch(
        (ledger_event(1), linked), import_audit=audit()
    )

    assert receipt.status == "FAILED"
    assert receipt.reason == "source_contract_violation"
    assert event_rows(path) == before


# --- structural invalid batches (no store touch) -----------------------------

@pytest.mark.parametrize(
    "batch",
    [
        (),
        [1, 2],
        ("not-an-event",),
    ],
)
def test_structurally_invalid_batches_fail_without_creating_a_store(
    tmp_path, batch
):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(batch, import_audit=audit())
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_batch"
    assert receipt.inserted_count == 0
    assert not path.exists()


def test_untyped_member_beside_a_valid_event_fails_with_no_row_persisted(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    receipt = make_store(path).append_batch(
        (ledger_event(1), "not-an-event"), import_audit=audit()
    )
    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_batch"
    assert counts(receipt) == (2, 1, 0, 0, 0, 1)
    assert event_rows(path) == []


def test_mixed_source_scope_batch_fails_without_creating_a_store(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    mixed = (
        ledger_event(1),
        ledger_event(2, account_scope="fixture_other_scope"),
    )
    receipt = make_store(path).append_batch(mixed, import_audit=audit())

    assert receipt.status == "FAILED"
    assert receipt.reason == "invalid_batch"
    assert counts(receipt) == (2, 0, 0, 0, 0, 2)
    assert not path.exists()


def test_naive_clock_and_bad_audit_meta_fail_without_creating_a_store(
    tmp_path,
):
    path = tmp_path / "private" / "ledger.sqlite3"
    naive = TradeLedgerStore(
        path,
        clock=lambda: datetime(2026, 7, 28, 1, 2, 3),
        id_factory=lambda: "fixture-id-1",
    )
    receipt = naive.append_batch((ledger_event(1),), import_audit=audit())
    assert (receipt.status, receipt.reason) == ("FAILED", "invalid_batch")
    assert not path.exists()

    for bad_meta in (
        None,
        audit(sha="XYZ"),
        audit(sha="ab" * 31),
        audit(version=""),
        ImportAuditMeta(file_sha256="ab" * 32, importer_version=7),
    ):
        receipt = make_store(path).append_batch(
            (ledger_event(1),), import_audit=bad_meta
        )
        assert (receipt.status, receipt.reason) == ("FAILED", "invalid_batch")
    assert not path.exists()


@pytest.mark.parametrize(
    "overrides",
    [
        dict(event_kind="REVERSAL"),
        dict(quantity="1.50"),
        dict(quantity="NaN"),
        dict(unit_price="1e3"),
        dict(amount="+5"),
        dict(fee="01"),
        dict(trade_date=datetime(2026, 1, 1, tzinfo=timezone.utc)),
        dict(transaction_type_raw=""),
        dict(instrument_raw=""),
        dict(medium_raw=""),
        dict(currency_raw=""),
        dict(supersedes_source_record_key="row-000001"),
    ],
)
def test_syntactically_invalid_members_fail_the_whole_batch(
    tmp_path, overrides
):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    before = event_rows(path)

    receipt = store.append_batch(
        (ledger_event(1), ledger_event(2, **overrides)),
        import_audit=audit(),
    )

    assert receipt.status == "FAILED"
    assert receipt.inserted_count == 0
    assert receipt.invalid_count >= 1
    assert event_rows(path) == before


# --- read path ---------------------------------------------------------------

def test_read_scope_is_read_only_and_never_creates_a_store(tmp_path):
    from tokmo.records.trade_ledger_privacy import StoreReadError

    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    with pytest.raises(StoreReadError) as captured:
        store.read_scope(source=SOURCE, account_scope=SCOPE)
    assert captured.value.reason == "store_read_failed"
    assert not path.exists()


def test_read_scope_of_an_absent_scope_returns_empty(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    store = make_store(path)
    assert store.append_batch(
        (ledger_event(1),), import_audit=audit()
    ).status == "SAVED"
    assert store.read_scope(
        source=SOURCE, account_scope="fixture_other_scope"
    ) == ()


def test_separate_process_reopen_reads_exact_equality(tmp_path):
    path = tmp_path / "private" / "ledger.sqlite3"
    events = observed_shape_batch()
    assert make_store(path).append_batch(
        events, import_audit=audit()
    ).status == "SAVED"

    fresh = make_store(path)
    assert fresh.read_scope(source=SOURCE, account_scope=SCOPE) == tuple(
        sorted(events, key=read_order_key)
    )


# --- architecture boundaries -------------------------------------------------

def test_trade_ledger_modules_import_no_forbidden_layers():
    root = Path(__file__).parents[1] / "src" / "tokmo"
    modules = {
        "values/trade_ledger.py",
        "records/trade_ledger_store.py",
        "records/trade_ledger_canonical.py",
        "records/trade_ledger_preflight.py",
        "records/trade_ledger_privacy.py",
    }

    def imports(path):
        found = set()
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.Import):
                found.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                found.add(node.module)
        return found

    forbidden_prefixes = (
        "tokmo.facts", "tokmo.programs", "tokmo.runtime",
        "tokmo.composition", "urllib", "http", "socket", "ssl",
    )
    for module in modules:
        for name in imports(root / module):
            assert not name.startswith(forbidden_prefixes), (module, name)
    values_imports = imports(root / "values/trade_ledger.py")
    assert not any(name.startswith("tokmo") for name in values_imports)

    # "excel" appears only inside the frozen identity label; forbid the
    # parser/provider/judgment surfaces instead.
    forbidden_text = (
        "openpyxl", "xlrd", ".xlsx", "holding", "portfolio",
        "alert", "calendar", "scheduler", "telegram",
    )
    for module in modules:
        text = (root / module).read_text().lower()
        for token in forbidden_text:
            assert token not in text, (module, token)
        if module != "records/trade_ledger_preflight.py":
            assert "toss" not in text, (module, "toss")
