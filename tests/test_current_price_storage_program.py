from datetime import UTC, datetime

from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.programs.current_price_storage import (
    CurrentPriceStorageRunResult,
    store_current_price_outcome,
)
from tokmo.records.latest_quotes import LatestQuoteSaveReceipt
from tokmo.values import TossCurrentPriceFact

NOW = datetime(2026, 7, 28, 1, 2, 3, tzinfo=UTC)
FACT = TossCurrentPriceFact(
    source="fixture-source",
    symbol="FixtureA",
    last_price="12.5",
    currency_raw="XTS",
    timestamp=None,
    fetched_at=NOW,
)


class StubStore:
    def __init__(self, receipt):
        self.receipt = receipt
        self.calls = 0

    def save_batch(self, facts):
        self.calls += 1
        return self.receipt


def test_query_failure_and_protocol_failure_make_zero_store_calls():
    store = StubStore(LatestQuoteSaveReceipt("SAVED", 1, 0, "saved", NOW))

    failed = store_current_price_outcome(
        TossPriceQueryOutcome("rate_limited"), store=store
    )
    protocol = store_current_price_outcome(object(), store=store)

    assert failed == CurrentPriceStorageRunResult("query_rate_limited", 0, 0, 0)
    assert protocol == CurrentPriceStorageRunResult("protocol_failure", 0, 0, 0)
    assert store.calls == 0


def test_success_stores_exactly_once_and_store_failure_is_not_retried():
    success_store = StubStore(LatestQuoteSaveReceipt("SAVED", 1, 0, "saved", NOW))
    saved = store_current_price_outcome(
        TossPriceQueryOutcome("success", (FACT,)), store=success_store
    )
    assert saved == CurrentPriceStorageRunResult("saved", 1, 1, 0)
    assert success_store.calls == 1

    failed_store = StubStore(
        LatestQuoteSaveReceipt("FAILED", 0, 0, "store_locked", NOW)
    )
    failed = store_current_price_outcome(
        TossPriceQueryOutcome("success", (FACT,)), store=failed_store
    )
    assert failed == CurrentPriceStorageRunResult("store_store_locked", 1, 0, 0)
    assert failed_store.calls == 1

    rendered = repr(saved) + repr(failed)
    for private_value in ("FixtureA", "12.5", "XTS"):
        assert private_value not in rendered
