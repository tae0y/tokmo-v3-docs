from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from tokmo.records.receipt_store import (
    ReceiptCorruptError,
    ReceiptStore,
    with_preserved_last_success,
)
from tokmo.values.material_pack import JobKind
from tokmo.values.receipt_v1 import (
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
)

NOW = datetime(2026, 7, 30, tzinfo=UTC)


def saved_receipt():
    success = LastSuccessV1(1, "packs/quotes/1.json", "a" * 64, NOW, NOW)
    return ReceiptV1(
        1,
        JobKind.QUOTES,
        AttemptV1(1, AttemptStatus.SAVED, "saved", NOW),
        success,
        {"KR": MarketFreshness("KR", "fresh", 1, NOW)},
        None,
    )


def test_receipt_is_typed_frozen_atomic_and_preserves_last_success(tmp_path):
    path = tmp_path / "quotes.json"
    store = ReceiptStore(path, clock=lambda: NOW)
    receipt = saved_receipt()
    store.replace(receipt)
    loaded = store.read()
    assert loaded == receipt
    assert isinstance(loaded.attempt, AttemptV1)
    assert isinstance(loaded.last_success, LastSuccessV1)
    with pytest.raises(FrozenInstanceError):
        loaded.schema_version = 2

    failed = AttemptV1(2, AttemptStatus.FAILED, "failed_parse", NOW)
    preserved = with_preserved_last_success(loaded, failed)
    assert preserved.attempt == failed
    assert preserved.last_success is loaded.last_success
    store.replace(preserved)
    assert store.read() == preserved

    original = path.read_bytes()
    corrupted_bytes = b'{"schema_version":1}'
    path.write_bytes(corrupted_bytes)
    with pytest.raises(ReceiptCorruptError):
        store.read()
    assert path.read_bytes() == corrupted_bytes
    assert path.read_bytes() != original


def test_receipt_reason_tokens_are_closed_and_open_is_rejected():
    with pytest.raises(ValueError):
        AttemptV1(1, AttemptStatus.SKIPPED, "open", NOW)
    with pytest.raises(ValueError):
        AttemptV1(1, AttemptStatus.SKIPPED, "unknown", NOW)
    receipt = saved_receipt()
    with pytest.raises(ValueError):
        ReceiptV1(
            True,
            receipt.job,
            receipt.attempt,
            receipt.last_success,
            dict(receipt.markets),
            receipt.completeness,
        )


def test_required_receipt_datetimes_reject_none_but_market_freshness_allows_it():
    with pytest.raises(ValueError, match="completed_at"):
        AttemptV1(1, AttemptStatus.SAVED, "saved", None)
    with pytest.raises(ValueError, match="captured_at"):
        LastSuccessV1(1, "packs/quotes/1.json", "a" * 64, None, NOW)
    with pytest.raises(ValueError, match="completed_at"):
        LastSuccessV1(1, "packs/quotes/1.json", "a" * 64, NOW, None)
    assert MarketFreshness("KR", "stale", None, None).captured_at is None


def test_receipt_reader_maps_deep_json_recursion_to_typed_corrupt(tmp_path):
    path = tmp_path / "quotes.json"
    raw = b"[" * 10_000 + b"null" + b"]" * 10_000
    path.write_bytes(raw)
    with pytest.raises(ReceiptCorruptError):
        ReceiptStore(path, clock=lambda: NOW).read()
    assert path.read_bytes() == raw


def test_trades_receipt_round_trips_exact_five_field_completeness(tmp_path):
    completeness = CompletenessV1(3, 1, True, 2, 4)
    receipt = ReceiptV1(
        1,
        JobKind.TRADES,
        AttemptV1(9, AttemptStatus.FAILED, "failed_store", NOW),
        None,
        {},
        completeness,
    )
    store = ReceiptStore(tmp_path / "trades.json", clock=lambda: NOW)
    store.replace(receipt)
    loaded = store.read()
    assert loaded == receipt
    assert loaded.completeness == completeness
