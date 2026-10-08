"""Store one already-produced current-price query outcome."""

from dataclasses import dataclass

from tokmo.facts.toss_prices import TossPriceQueryOutcome
from tokmo.records.latest_quotes import LatestQuoteStore


@dataclass(frozen=True)
class CurrentPriceStorageRunResult:
    code: str
    queried: int
    inserted: int
    updated: int


def store_current_price_outcome(
    outcome: TossPriceQueryOutcome, *, store: LatestQuoteStore
) -> CurrentPriceStorageRunResult:
    if not isinstance(outcome, TossPriceQueryOutcome):
        return CurrentPriceStorageRunResult("protocol_failure", 0, 0, 0)
    if outcome.code != "success":
        return CurrentPriceStorageRunResult(
            f"query_{outcome.code}", 0, 0, 0
        )
    receipt = store.save_batch(outcome.facts)
    if receipt.status == "SAVED":
        return CurrentPriceStorageRunResult(
            "saved",
            len(outcome.facts),
            receipt.inserted_count,
            receipt.updated_count,
        )
    return CurrentPriceStorageRunResult(
        f"store_{receipt.reason}", len(outcome.facts), 0, 0
    )
