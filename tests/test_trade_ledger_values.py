"""Typed value contracts for the trade-ledger batch storage pilot.

All fixture values are synthetic; nothing here is copied from any private
workbook. Assertions follow the closed pilot plan: redacted representations,
closed reason tokens, and the receipt count invariant on every receipt.
"""

from datetime import date, datetime, timezone

import pytest

from tokmo.values.trade_ledger import (
    CLOSED_REASON_TOKENS,
    EVENT_KINDS,
    TRADE_LEDGER_ACCOUNT_SCOPE,
    TRADE_LEDGER_SOURCE,
    ImportAuditMeta,
    TradeBatchSaveReceipt,
    TradeLedgerEvent,
)


NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=timezone.utc)


def event(**overrides):
    values = dict(
        source=TRADE_LEDGER_SOURCE,
        account_scope=TRADE_LEDGER_ACCOUNT_SCOPE,
        source_record_key="row-000001",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 7, 1),
        transaction_type_raw="ETF매수",
        instrument_raw="Fixture ETF Alpha",
        quantity="3",
        unit_price="2500",
        amount="7500",
        medium_raw="FixtureApp",
        currency_raw=None,
        settlement_date=None,
        fee=None,
        tax=None,
    )
    values.update(overrides)
    return TradeLedgerEvent(**values)


def receipt(**overrides):
    values = dict(
        status="SAVED",
        import_batch_id="batch-fixture-1",
        submitted_count=3,
        new_candidate_count=3,
        inserted_count=3,
        duplicate_count=0,
        conflict_count=0,
        invalid_count=0,
        reason="",
        stored_at=NOW,
    )
    values.update(overrides)
    return TradeBatchSaveReceipt(**values)


def test_identity_labels_and_kind_and_reason_vocabularies_are_frozen():
    assert TRADE_LEDGER_SOURCE == "retirement_excel_v1"
    assert TRADE_LEDGER_ACCOUNT_SCOPE == "retirement_pension_1"
    assert EVENT_KINDS == frozenset({"EXECUTION", "CORRECTION"})
    assert CLOSED_REASON_TOKENS == frozenset({
        "invalid_batch",
        "source_contract_violation",
        "batch_identity_conflict",
        "supersession_invalid",
        "schema_mismatch",
        "store_corrupt",
        "store_locked",
        "store_write_failed",
        "store_read_failed",
        "audit_write_failed",
        "promotion_link_failed",
        "promotion_recovery_required",
        "candidate_missing",
        "canonical_exists",
        "wal_checkpoint_failed",
        "sidecars_present",
        "directory_mismatch",
    })


def test_event_is_frozen_lossless_and_value_equal():
    first = event()
    second = event()
    assert first == second
    assert first != event(quantity="4")
    assert first.supersedes_source_record_key is None
    assert first.currency_raw is None
    assert first.settlement_date is None
    assert first.fee is None
    assert first.tax is None
    with pytest.raises(Exception):
        first.quantity = "9"


def test_event_repr_and_str_are_constant_redactions():
    unlinked = event(
        source_record_key="row-000085",
        event_kind="CORRECTION",
        transaction_type_raw="매수정정*",
        quantity="-2",
        unit_price="1250.5",
        amount="-2501",
    )
    for rendered in (repr(unlinked), str(unlinked)):
        assert rendered == "<TradeLedgerEvent REDACTED>"
    for leaked in ("row-000085", "매수정정*", "-2", "1250.5", "2026",
                   "retirement_excel_v1", "retirement_pension_1"):
        assert leaked not in repr(unlinked)
        assert leaked not in str(unlinked)


def test_import_audit_meta_repr_and_str_are_constant_redactions():
    meta = ImportAuditMeta(file_sha256="ab" * 32, importer_version="fixture-importer-9")
    assert repr(meta) == "<ImportAuditMeta REDACTED>"
    assert str(meta) == "<ImportAuditMeta REDACTED>"
    assert "fixture-importer-9" not in repr(meta)
    assert "ab" * 32 not in str(meta)
    assert ImportAuditMeta(file_sha256=None, importer_version="v").file_sha256 is None


def test_receipt_count_invariant_holds_on_every_receipt():
    saved = receipt()
    assert saved.submitted_count == (
        saved.new_candidate_count
        + saved.duplicate_count
        + saved.conflict_count
        + saved.invalid_count
    )
    with pytest.raises(ValueError) as broken:
        receipt(submitted_count=4)
    assert str(broken.value) == "count_invariant_broken"


def test_failed_receipt_requires_zero_inserted_and_saved_requires_exact_inserted():
    failed = receipt(
        status="FAILED",
        inserted_count=0,
        new_candidate_count=1,
        conflict_count=2,
        reason="batch_identity_conflict",
    )
    assert failed.inserted_count == 0
    with pytest.raises(ValueError) as nonzero:
        receipt(
            status="FAILED",
            inserted_count=1,
            new_candidate_count=1,
            duplicate_count=2,
            reason="batch_identity_conflict",
        )
    assert str(nonzero.value) == "failed_receipt_inserted_count_nonzero"
    with pytest.raises(ValueError) as mismatch:
        receipt(inserted_count=1, duplicate_count=1, new_candidate_count=2)
    assert str(mismatch.value) == "saved_receipt_inserted_count_mismatch"


def test_receipt_status_and_reason_are_closed():
    with pytest.raises(ValueError) as status:
        receipt(status="PARTIAL")
    assert str(status.value) == "receipt_status_invalid"
    with pytest.raises(ValueError) as saved_reason:
        receipt(reason="saved")
    assert str(saved_reason.value) == "receipt_reason_invalid"
    with pytest.raises(ValueError) as open_reason:
        receipt(status="FAILED", inserted_count=0, new_candidate_count=0,
                invalid_count=3, reason="because the row was odd")
    assert str(open_reason.value) == "receipt_reason_invalid"


def test_receipt_repr_exposes_counts_and_tokens_but_never_stored_at():
    failed = receipt(
        status="FAILED",
        inserted_count=0,
        new_candidate_count=1,
        conflict_count=2,
        reason="batch_identity_conflict",
    )
    rendered = repr(failed)
    assert rendered == str(failed)
    assert "FAILED" in rendered
    assert "batch_identity_conflict" in rendered
    assert "batch-fixture-1" in rendered
    assert "2026" not in rendered
    assert "01:02:03" not in rendered
