"""Independent per-market calendar refresh with typed results (SDS §4, §8).

Each market owns its own local date, staleness decision, fetch attempt, and
result. One market's failure closes only that market; only shared-cache
corruption (untrustworthy market separation) closes both, with zero fetches.

Every stale-market fetch requires a typed OpsKernel STARTED calendar receipt
(M5): without a receipt whose market/local-date identity matches, the provider
is not called and the cache is not mutated. The receipt is preserved through
``CalendarFetchResult`` into the cache commit and the returned result.

The dataclass alone is never authority (B1): immediately before the provider
call, the receipt's one-time claim is consumed in the authoritative OpsKernel
against the exact planned/STARTED cadence rows; the cache commit consumes a
second one-time kernel claim. Forged, terminal, mismatched, or already
claimed receipts perform provider wire zero and cache mutation zero.
"""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from typing import Callable, Mapping

from tokmo.records.calendar_cache import (
    CalendarCacheConflictError,
    CalendarCacheCorruptError,
    CalendarCacheStore,
    calendar_market_digest,
)
from tokmo.values.calendar_refresh import (
    CalendarBoundaryError,
    CalendarDateError,
    CalendarFetchResult,
    CalendarIdentityError,
    market_local_date,
)
from tokmo.values.ops_kernel import CalendarStartReceiptV1, OpsKernelError
from tokmo.values.session_gate import Market


class MarketRefreshState(str, Enum):
    SUCCESS = "success"
    PROVIDER_FAILED = "provider_failed"
    INVALID_IDENTITY = "invalid_identity"
    INVALID_DATE = "invalid_date"
    INVALID_BOUNDARY = "invalid_boundary"
    CACHE_UPDATE_FAILED = "cache_update_failed"
    CACHE_CONFLICT = "cache_conflict"


@dataclass(frozen=True)
class MarketRefreshResult:
    market: Market
    requested_local_date: date
    state: MarketRefreshState
    refreshed: bool
    receipt: CalendarStartReceiptV1 | None = None
    # True when a CAS conflict was resolved by re-reading the store and
    # rejudging against the racing writer's valid current entry (M6).
    rejudged: bool = False

    def __post_init__(self):
        if self.refreshed and self.state is not MarketRefreshState.SUCCESS:
            raise ValueError("only a successful refresh commits")
        if self.refreshed and self.receipt is None:
            raise ValueError("a committed refresh requires its STARTED receipt")
        if self.rejudged and (self.state is not MarketRefreshState.SUCCESS or self.refreshed):
            raise ValueError("a rejudged result is an uncommitted current success")


def _valid_receipt(receipt, market, local_date) -> bool:
    return (
        type(receipt) is CalendarStartReceiptV1
        and receipt.market is market
        and receipt.market_local_date == local_date
        and receipt.state == "STARTED"
    )


def _refresh_one(store, kernel, fetcher, market, local_date, entry, receipt,
                 now) -> MarketRefreshResult:
    def result(state, refreshed=False, bound_receipt=None, rejudged=False):
        return MarketRefreshResult(
            market, local_date, state, refreshed, bound_receipt, rejudged,
        )

    if entry is not None and entry.trade_date == local_date.isoformat():
        return result(MarketRefreshState.SUCCESS)
    if not _valid_receipt(receipt, market, local_date):
        # Missing/mismatched/non-STARTED receipt: provider call 0, mutation 0.
        return result(MarketRefreshState.INVALID_IDENTITY)
    try:
        # One-time DB-backed claim against the exact STARTED kernel rows;
        # a forged/terminal/reused receipt fails here with provider wire 0.
        kernel.claim_calendar_provider(receipt, now=now)
    except OpsKernelError:
        return result(MarketRefreshState.INVALID_IDENTITY)
    expected_digest = calendar_market_digest(entry)
    try:
        day = fetcher(market=market, local_trade_date=local_date, receipt=receipt)
    except Exception:
        return result(MarketRefreshState.PROVIDER_FAILED, bound_receipt=receipt)
    # G2.5: calendar_fetch now returns the full CalendarResponse to preserve
    # next_business_day. Extract .today for the cache commit.
    if hasattr(day, "today") and hasattr(day, "next_business_day"):
        day = day.today
    try:
        fetch = CalendarFetchResult(market, local_date, day, receipt)
    except CalendarIdentityError:
        return result(MarketRefreshState.INVALID_IDENTITY, bound_receipt=receipt)
    except CalendarDateError:
        return result(MarketRefreshState.INVALID_DATE, bound_receipt=receipt)
    except CalendarBoundaryError:
        return result(MarketRefreshState.INVALID_BOUNDARY, bound_receipt=receipt)
    try:
        store.replace_market(market, expected_digest, fetch, authority=kernel)
    except CalendarCacheConflictError:
        # CAS conflict: never overwrite and never call the provider again.
        # Re-read the store; a racing writer's valid entry for the SAME
        # market-local date is a usable current result, rejudged.
        try:
            reread = store.read()
        except CalendarCacheCorruptError:
            return result(MarketRefreshState.CACHE_UPDATE_FAILED, bound_receipt=receipt)
        current = None if reread is None else (
            reread.kr if market is Market.KR else reread.us
        )
        if current is not None and current.trade_date == local_date.isoformat():
            return result(
                MarketRefreshState.SUCCESS, bound_receipt=receipt, rejudged=True,
            )
        return result(MarketRefreshState.CACHE_CONFLICT, bound_receipt=receipt)
    except (CalendarCacheCorruptError, OpsKernelError, OSError, ValueError):
        return result(MarketRefreshState.CACHE_UPDATE_FAILED, bound_receipt=receipt)
    return result(MarketRefreshState.SUCCESS, refreshed=True, bound_receipt=receipt)


def refresh_markets(
    *, store: CalendarCacheStore, fetchers: Mapping[Market, Callable],
    now: datetime, receipts: Mapping[Market, CalendarStartReceiptV1 | None],
    kernel,
) -> tuple[MarketRefreshResult, ...]:
    """Refresh both markets; ``kernel`` is the authoritative OpsKernel whose
    durable claims — not the receipt objects — authorize provider and cache."""
    local_dates = {market: market_local_date(market, now) for market in Market}
    try:
        cache = store.read()
    except CalendarCacheCorruptError:
        return tuple(
            MarketRefreshResult(
                market, local_dates[market],
                MarketRefreshState.CACHE_UPDATE_FAILED, False,
            ) for market in Market
        )
    return tuple(
        _refresh_one(
            store, kernel, fetchers[market], market, local_dates[market],
            None if cache is None else (cache.kr if market is Market.KR else cache.us),
            receipts.get(market), now,
        ) for market in Market
    )
