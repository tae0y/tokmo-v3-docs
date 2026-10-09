"""Dependency-free typed values shared by Tokmo V3 leaves."""

# =========================================================================
# Constants — defined directly in __init__.py (all layers use these)
# =========================================================================

# Contract v0.2 §3: the closed reason set for typed unavailability.
UNAVAILABLE_REASONS = frozenset(
    {
        "config_missing",
        "config_invalid",
        "config_load_error",
        "no_published_data",
        "transport_error",
        "rate_limited",
        "contract_mismatch",
        "parse_error",
    }
)

# Contract v0.2 §3: decisions are save/skip only, never send.
DECISION_ACTIONS = frozenset({"SAVE", "SKIP"})

# Contract v0.2 §5: WTI observation identity and units. Defined here so that
# both facts and records can validate against them without importing each other.
WTI_SOURCE = "eia_wti_rwtc"
WTI_SERIES = "RWTC"
WTI_UNITS = "$/BBL"

# SEC Tracer 1a mini-contract: submissions filing observation identity.
SEC_SOURCE = "sec_submissions"

# OpenDART Tracer 1a mini-contract: disclosure list observation identity.
OPENDART_SOURCE = "opendart_disclosure"
KRX_SOURCE = "krx_kospi_daily_trade"

# Toss current-holdings tracer: account-current snapshot observation identity.
TOSS_SOURCE = "toss-holdings-v1"

# Toss current-price fact boundary identity.
TOSS_PRICE_SOURCE = "toss-current-price-v1"


# =========================================================================
# canonical_decimal — defined directly in __init__.py
# =========================================================================

from decimal import Decimal


def canonical_decimal(value: Decimal) -> str:
    """Deterministic canonical serialization of a finite Decimal.

    Independent of the input's scale or exponent form: trailing zeros are
    dropped and scientific notation is expanded, so numerically equal inputs
    (``78.420``, ``78.42``, ``7.842E+1``) all serialize to ``"78.42"``.
    """
    if not value.is_finite():
        raise ValueError("decimal_not_finite")
    if value.is_zero():
        return "0"

    sign, raw_digits, exponent = value.as_tuple()
    digits = list(raw_digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1

    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        plain = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            plain = coefficient[:point] + "." + coefficient[point:]
        else:
            plain = "0." + ("0" * -point) + coefficient
    return ("-" if sign else "") + plain


# =========================================================================
# Re-export from domain files
# =========================================================================

# --- Primitives ---
from tokmo.values._primitives import (  # noqa: F401
    MISSING,
    Missing,
    Opt,
    RawStr,
    _AwareDateTime,
    _Decimal30,
    _DecimalNoMax,
    _MissingType,
    _RawEnum,
    _StrictDate,
)

# --- Filings ---
from tokmo.values.filings import (  # noqa: F401
    OpenDartDisclosureFact,
    SecFilingFact,
)

# --- Holdings ---
from tokmo.values.holdings import (  # noqa: F401
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    HoldingsSnapshotHeader,
    TossAccountHoldingsFact,
    TossHoldingFact,
)
from tokmo.values.krx import KrxDailyTradeFact  # noqa: F401

# --- Market observations ---
from tokmo.values.market_observations import (  # noqa: F401
    TossCurrentPriceFact,
    WtiFact,
    YieldCurveFact,
)

# --- Results ---
from tokmo.values.results import (  # noqa: F401
    DecisionRecord,
    SaveResult,
    SaveStatus,
    UnavailableFact,
)

# --- Toss: indicators ---
from tokmo.values.toss_indicators import (  # noqa: F401
    MarketIndicatorCandle,
    MarketIndicatorCandlePageResponse,
    MarketIndicatorPrice,
)

# --- Toss: investor trading ---
from tokmo.values.toss_investor_trading import (  # noqa: F401
    _INSTITUTION_BREAKDOWN_KEYS,
    InstitutionTradingBreakdown,
    InvestorTradingAmount,
    InvestorTradingRecord,
    InvestorTradingResponse,
)

# --- Toss: market calendar ---
from tokmo.values.toss_market_calendar import (  # noqa: F401
    CalendarSession,
    IntegratedHour,
    KrMarketCalendarResponse,
    KrMarketDay,
    UsMarketCalendarResponse,
    UsMarketDay,
)

# --- Toss: market data ---
from tokmo.values.toss_market_data import (  # noqa: F401
    Candle,
    CandlePageResponse,
    OrderbookEntry,
    OrderbookResponse,
    PriceLimitResponse,
    Trade,
)

# --- Toss: orders ---
from tokmo.values.toss_orders import (  # noqa: F401
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
    Order,
    OrderExecution,
    PaginatedConditionalOrderResponse,
    PaginatedOrderResponse,
)

# --- Toss: rankings ---
from tokmo.values.toss_rankings import (  # noqa: F401
    RankingItem,
    RankingPrice,
    RankingResponse,
)

# --- Toss: reference ---
from tokmo.values.toss_reference import (  # noqa: F401
    ExchangeRateResponse,
    KrMarketDetail,
    StockInfo,
    StockWarning,
)

# --- Trade ledger (existing, unchanged) ---
from tokmo.values.trade_ledger import (  # noqa: F401
    CLOSED_REASON_TOKENS,
    EVENT_KINDS,
    TRADE_LEDGER_ACCOUNT_SCOPE,
    TRADE_LEDGER_PAYLOAD_VERSION,
    TRADE_LEDGER_SCHEMA_VERSION,
    TRADE_LEDGER_SOURCE,
    ImportAuditMeta,
    TradeBatchSaveReceipt,
    TradeLedgerEvent,
)
