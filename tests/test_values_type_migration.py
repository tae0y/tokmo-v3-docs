"""Phase 0 type migration verification: every moved domain type is a frozen dataclass.

Covers all types moved from ``facts/toss_read_models.py`` (30 domain types +
10 primitives) and the 12 types split from ``values/__init__.py``.
"""

import dataclasses

from tokmo.values import (
    # --- primitives (helpers, not dataclasses) ---
    # _MissingType, MISSING, Missing, Opt, RawStr, _Decimal30, _DecimalNoMax,
    # _AwareDateTime, _StrictDate, _RawEnum  — these are NOT dataclasses
    #
    # --- toss_read_models domain types (30) ---
    OrderbookEntry, OrderbookResponse, Trade, PriceLimitResponse,
    Candle, CandlePageResponse,
    KrMarketDetail, StockInfo, StockWarning, ExchangeRateResponse,
    CalendarSession, IntegratedHour, KrMarketDay, KrMarketCalendarResponse,
    UsMarketDay, UsMarketCalendarResponse,
    RankingPrice, RankingItem, RankingResponse,
    MarketIndicatorPrice, MarketIndicatorCandle, MarketIndicatorCandlePageResponse,
    InvestorTradingAmount, InstitutionTradingBreakdown,
    InvestorTradingRecord, InvestorTradingResponse,
    OrderExecution, Order, PaginatedOrderResponse,
    ConditionalOrderCondition, ConditionalOrderDetailResponse,
    PaginatedConditionalOrderResponse,
    # --- __init__ split types (14, incl SaveStatus enum + SaveResult) ---
    TossHoldingFact, TossAccountHoldingsFact, HoldingsSnapshotAccount,
    CompleteHoldingsSnapshot, HoldingsSnapshotHeader,
    WtiFact, YieldCurveFact, TossCurrentPriceFact,
    SecFilingFact, OpenDartDisclosureFact,
    UnavailableFact, SaveResult, DecisionRecord,
)

ALL_MOVED_DATACLASSES = [
    # toss_read_models domain types (30)
    OrderbookEntry, OrderbookResponse, Trade, PriceLimitResponse,
    Candle, CandlePageResponse,
    KrMarketDetail, StockInfo, StockWarning, ExchangeRateResponse,
    CalendarSession, IntegratedHour, KrMarketDay, KrMarketCalendarResponse,
    UsMarketDay, UsMarketCalendarResponse,
    RankingPrice, RankingItem, RankingResponse,
    MarketIndicatorPrice, MarketIndicatorCandle, MarketIndicatorCandlePageResponse,
    InvestorTradingAmount, InstitutionTradingBreakdown,
    InvestorTradingRecord, InvestorTradingResponse,
    OrderExecution, Order, PaginatedOrderResponse,
    ConditionalOrderCondition, ConditionalOrderDetailResponse,
    PaginatedConditionalOrderResponse,
    # __init__ split types (13 dataclasses)
    TossHoldingFact, TossAccountHoldingsFact, HoldingsSnapshotAccount,
    CompleteHoldingsSnapshot, HoldingsSnapshotHeader,
    WtiFact, YieldCurveFact, TossCurrentPriceFact,
    SecFilingFact, OpenDartDisclosureFact,
    UnavailableFact, SaveResult, DecisionRecord,
]


def test_all_moved_types_are_dataclasses():
    """Every moved domain type must be a ``@dataclass``."""
    non_dataclass = [
        cls.__name__ for cls in ALL_MOVED_DATACLASSES
        if not dataclasses.is_dataclass(cls)
    ]
    assert non_dataclass == [], f"not dataclasses: {non_dataclass}"


def test_all_moved_dataclasses_are_frozen():
    """Every moved dataclass must have ``frozen=True``."""
    not_frozen = []
    for cls in ALL_MOVED_DATACLASSES:
        params = getattr(cls, "__dataclass_params__", None)
        if params is None:
            not_frozen.append(f"{cls.__name__} (no __dataclass_params__)")
        elif not params.frozen:
            not_frozen.append(cls.__name__)
    assert not_frozen == [], f"not frozen: {not_frozen}"
