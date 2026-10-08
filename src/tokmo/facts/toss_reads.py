"""Closed 20-member Toss read operation identity, request builders, validators,
closed query outcome, and one-request orchestration.

The host, path, method, and query keys are never caller arguments. The
operation is chosen from a closed enum, and the immutable spec table maps
each member to its literal path, rate group, account rule, and parser.

No persistence, composition, campaign runner, scheduler, retry, alert,
semantic calculation, or production wiring beyond facts query functions.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import Enum
import re

from tokmo.facts.toss import valid_token, valid_account_seq
from tokmo.facts.toss_read_parsers import parse_envelope, _FAIL, _WRONG
from tokmo.values import MISSING

__all__ = [
    "TossReadOperation",
    "TossReadSpec",
    "SPEC_TABLE",
    "TossReadRequest",
    "TossReadOutcome",
    "REQUEST_TIMEOUT_SECONDS",
    "build_read_request",
    "parse_read_response",
    "query_read",
    "valid_iso_date",
    "valid_account_seq_int64",
]

REQUEST_TIMEOUT_SECONDS = 10.0


# =========================================================================
# Closed operation enum
# =========================================================================

class TossReadOperation(Enum):
    BUYING_POWER = "BUYING_POWER"
    SELLABLE_QUANTITY = "SELLABLE_QUANTITY"
    COMMISSIONS = "COMMISSIONS"
    ORDERBOOK = "ORDERBOOK"
    TRADES = "TRADES"
    PRICE_LIMITS = "PRICE_LIMITS"
    STOCK_CANDLES = "STOCK_CANDLES"
    STOCKS = "STOCKS"
    STOCK_WARNINGS = "STOCK_WARNINGS"
    STOCK_INVESTOR_TRADING = "STOCK_INVESTOR_TRADING"
    EXCHANGE_RATE = "EXCHANGE_RATE"
    KR_MARKET_CALENDAR = "KR_MARKET_CALENDAR"
    US_MARKET_CALENDAR = "US_MARKET_CALENDAR"
    RANKINGS = "RANKINGS"
    INDICATOR_PRICES = "INDICATOR_PRICES"
    INDICATOR_CANDLES = "INDICATOR_CANDLES"
    INDICATOR_INVESTOR_TRADING = "INDICATOR_INVESTOR_TRADING"
    ORDERS = "ORDERS"
    ORDER_DETAIL = "ORDER_DETAIL"
    CONDITIONAL_ORDERS = "CONDITIONAL_ORDERS"
    CONDITIONAL_ORDER_DETAIL = "CONDITIONAL_ORDER_DETAIL"


def resolve_conditional_order_side(
        order_type: str, leg_index: int,
        leg_order_side: object, legacy_order_side: object = None,
) -> str | None:
    """Resolve one conditional-order leg's provider-defined direction.

    Explicit leg ``orderSide`` wins.  The legacy top-level field is accepted
    only for SINGLE.  Otherwise the provider's documented grouped-order
    contract supplies OCO=SELL+SELL and OTO=BUY+SELL.  No price or holdings
    inference is performed.
    """
    if order_type not in {"SINGLE", "OCO", "OTO"} or leg_index not in {0, 1}:
        raise ValueError("conditional_order_side_contract")
    if order_type == "SINGLE" and leg_index != 0:
        raise ValueError("conditional_order_side_contract")
    if leg_order_side is MISSING:
        leg_order_side = None
    if legacy_order_side is MISSING:
        legacy_order_side = None
    if leg_order_side is not None:
        if not isinstance(leg_order_side, str) or leg_order_side not in {"BUY", "SELL"}:
            raise ValueError("conditional_order_side_contract")
        return leg_order_side
    if not isinstance(legacy_order_side, str) or legacy_order_side not in {"BUY", "SELL"}:
        legacy_order_side = None
    if order_type == "SINGLE":
        return legacy_order_side
    if order_type == "OCO":
        return "SELL"
    return "BUY" if leg_index == 0 else "SELL"



# =========================================================================
# Immutable spec table
# =========================================================================

@dataclass(frozen=True)
class TossReadSpec:
    operation: TossReadOperation
    path: str
    rate_group: str
    account_rule: str  # "FORBIDDEN" | "required"


SPEC_TABLE: dict[TossReadOperation, TossReadSpec] = {
    TossReadOperation.STOCK_INVESTOR_TRADING: TossReadSpec(
        TossReadOperation.STOCK_INVESTOR_TRADING, "/api/v1/stocks/{symbol}/investor-trading",
        "STOCK_TRADING_TREND", "FORBIDDEN"),
    TossReadOperation.BUYING_POWER: TossReadSpec(
        TossReadOperation.BUYING_POWER, "/api/v1/buying-power", "ORDER_INFO", "required"),
    TossReadOperation.SELLABLE_QUANTITY: TossReadSpec(
        TossReadOperation.SELLABLE_QUANTITY, "/api/v1/sellable-quantity", "ORDER_INFO", "required"),
    TossReadOperation.COMMISSIONS: TossReadSpec(
        TossReadOperation.COMMISSIONS, "/api/v1/commissions", "ORDER_INFO", "required"),
    TossReadOperation.ORDERBOOK: TossReadSpec(
        TossReadOperation.ORDERBOOK, "/api/v1/orderbook",
        "MARKET_DATA", "FORBIDDEN"),
    TossReadOperation.TRADES: TossReadSpec(
        TossReadOperation.TRADES, "/api/v1/trades",
        "MARKET_DATA", "FORBIDDEN"),
    TossReadOperation.PRICE_LIMITS: TossReadSpec(
        TossReadOperation.PRICE_LIMITS, "/api/v1/price-limits",
        "MARKET_DATA", "FORBIDDEN"),
    TossReadOperation.STOCK_CANDLES: TossReadSpec(
        TossReadOperation.STOCK_CANDLES, "/api/v1/candles",
        "MARKET_DATA_CHART", "FORBIDDEN"),
    TossReadOperation.STOCKS: TossReadSpec(
        TossReadOperation.STOCKS, "/api/v1/stocks",
        "STOCK", "FORBIDDEN"),
    TossReadOperation.STOCK_WARNINGS: TossReadSpec(
        TossReadOperation.STOCK_WARNINGS, "/api/v1/stocks/{symbol}/warnings",
        "STOCK", "FORBIDDEN"),
    TossReadOperation.EXCHANGE_RATE: TossReadSpec(
        TossReadOperation.EXCHANGE_RATE, "/api/v1/exchange-rate",
        "MARKET_INFO", "FORBIDDEN"),
    TossReadOperation.KR_MARKET_CALENDAR: TossReadSpec(
        TossReadOperation.KR_MARKET_CALENDAR, "/api/v1/market-calendar/KR",
        "MARKET_INFO", "FORBIDDEN"),
    TossReadOperation.US_MARKET_CALENDAR: TossReadSpec(
        TossReadOperation.US_MARKET_CALENDAR, "/api/v1/market-calendar/US",
        "MARKET_INFO", "FORBIDDEN"),
    TossReadOperation.RANKINGS: TossReadSpec(
        TossReadOperation.RANKINGS, "/api/v1/rankings",
        "RANKING", "FORBIDDEN"),
    TossReadOperation.INDICATOR_PRICES: TossReadSpec(
        TossReadOperation.INDICATOR_PRICES, "/api/v1/market-indicators/prices",
        "MARKET_INDICATOR", "FORBIDDEN"),
    TossReadOperation.INDICATOR_CANDLES: TossReadSpec(
        TossReadOperation.INDICATOR_CANDLES,
        "/api/v1/market-indicators/{symbol}/candles",
        "MARKET_INDICATOR_CHART", "FORBIDDEN"),
    TossReadOperation.INDICATOR_INVESTOR_TRADING: TossReadSpec(
        TossReadOperation.INDICATOR_INVESTOR_TRADING,
        "/api/v1/market-indicators/{symbol}/investor-trading",
        "MARKET_INDICATOR", "FORBIDDEN"),
    TossReadOperation.ORDERS: TossReadSpec(
        TossReadOperation.ORDERS, "/api/v1/orders",
        "ORDER_HISTORY", "required"),
    TossReadOperation.ORDER_DETAIL: TossReadSpec(
        TossReadOperation.ORDER_DETAIL, "/api/v1/orders/{orderId}",
        "ORDER_HISTORY", "required"),
    TossReadOperation.CONDITIONAL_ORDERS: TossReadSpec(
        TossReadOperation.CONDITIONAL_ORDERS, "/api/v1/conditional-orders",
        "CONDITIONAL_ORDER_HISTORY", "required"),
    TossReadOperation.CONDITIONAL_ORDER_DETAIL: TossReadSpec(
        TossReadOperation.CONDITIONAL_ORDER_DETAIL,
        "/api/v1/conditional-orders/{conditionalOrderId}",
        "CONDITIONAL_ORDER_HISTORY", "required"),
}


# =========================================================================
# Validators
# =========================================================================

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_BATCH_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.\-]+$")
_INDICATOR_SYMBOL_RE = re.compile(r"^[A-Za-z0-9_]+$")
_CURSOR_RE = re.compile(r"^[A-Za-z0-9_\-]+$")

_CANDLE_INTERVALS = frozenset({"1m", "1d"})
_INVESTOR_INTERVALS = frozenset({"1d", "1w", "1mo", "1y"})
_RANKING_TYPES = frozenset({
    "MARKET_TRADING_AMOUNT", "MARKET_TRADING_VOLUME",
    "TOP_GAINERS", "TOP_LOSERS",
    "TOSS_SECURITIES_TRADING_AMOUNT", "TOSS_SECURITIES_TRADING_VOLUME",
})
_RANKING_DURATIONS = frozenset({
    "realtime", "1d", "1w", "1mo", "3mo", "6mo", "1y",
})
_TOP_RANKING_TYPES = frozenset({"TOP_GAINERS", "TOP_LOSERS"})
_CURRENCIES = frozenset({"KRW", "USD"})
_MARKET_COUNTRIES = frozenset({"KR", "US"})
_INDICATOR_CATALOG = frozenset({
    "KOSPI", "KOSDAQ", "KR_BOND_2Y", "KR_BOND_3Y",
    "KR_BOND_5Y", "KR_BOND_10Y", "KR_BOND_20Y", "KR_BOND_30Y",
})
_INVESTOR_SYMBOLS = frozenset({"KOSPI", "KOSDAQ"})
_ORDER_STATUSES = frozenset({"OPEN", "CLOSED"})
_BOND_PREFIX = "KR_BOND_"


def _valid_single_symbol(value) -> bool:
    return (isinstance(value, str) and _SYMBOL_RE.fullmatch(value) is not None)


def _valid_batch_symbols(value) -> tuple[str, ...] | None:
    if not isinstance(value, tuple):
        return None
    if not 1 <= len(value) <= 200:
        return None
    if len(set(value)) != len(value):
        return None
    for sym in value:
        if not isinstance(sym, str) or not sym or _BATCH_SYMBOL_RE.fullmatch(sym) is None:
            return None
    return value


def _valid_indicator_symbols(value) -> tuple[str, ...] | None:
    if not isinstance(value, tuple):
        return None
    if not 1 <= len(value) <= 200:
        return None
    if len(set(value)) != len(value):
        return None
    for sym in value:
        if sym not in _INDICATOR_CATALOG:
            return None
    return value


def _valid_int(value, lo: int, hi: int) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if lo <= value <= hi:
        return value
    return None


def _valid_aware_datetime(value) -> datetime | None:
    if isinstance(value, datetime) and value.tzinfo is not None and value.utcoffset() is not None:
        return value
    return None


def _valid_date(value) -> date | None:
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    if isinstance(value, datetime):
        return value.date() if value.tzinfo is not None else None
    return None


_ISO_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def valid_iso_date(value) -> bool:
    """Strict ISO ``YYYY-MM-DD`` calendar date; wire-safe by construction."""
    if not isinstance(value, str) or _ISO_DATE_RE.fullmatch(value) is None:
        return False
    try:
        date.fromisoformat(value)
    except ValueError:
        return False
    return True


_INT64_MIN = -(2 ** 63)
_INT64_MAX = 2 ** 63 - 1
_INT64_RE = re.compile(r"^-?\d{1,19}$")


def valid_account_seq_int64(value) -> bool:
    """Canonical signed OpenAPI int64 decimal text, full range.

    No plus sign, exponent, whitespace, or non-canonical leading form.
    """
    if not isinstance(value, str) or _INT64_RE.fullmatch(value) is None:
        return False
    parsed = int(value)
    if str(parsed) != value:
        return False
    return _INT64_MIN <= parsed <= _INT64_MAX


def _valid_opaque_id(value) -> str | None:
    if not isinstance(value, str):
        return None
    if not value or len(value) > 4096:
        return None
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        return None
    if "/" in value:
        return None
    return value


# =========================================================================
# Request type
# =========================================================================

@dataclass(frozen=True)
class TossReadRequest:
    """One of the closed read operations.

    All sensitive fields carry ``repr=False``: token, account_seq, and the
    query parameters that may carry IDs/cursors/symbols.
    """

    operation: TossReadOperation
    timeout_seconds: float
    token: str = field(repr=False)
    account_seq: str | None = field(repr=False, default=None)
    # Query/path parameters — all repr=False to avoid leaking symbols/IDs.
    symbol: str = field(repr=False, default="")
    symbols: tuple[str, ...] = field(repr=False, default=())
    interval: str = field(repr=False, default="")
    count: int | None = field(repr=False, default=None)
    before: datetime | None = field(repr=False, default=None)
    adjusted: bool | None = field(repr=False, default=None)
    date: str = field(repr=False, default="")
    order_id: str = field(repr=False, default="")
    conditional_order_id: str = field(repr=False, default="")
    status: str = field(repr=False, default="")
    cursor: str = field(repr=False, default="")
    limit: int | None = field(repr=False, default=None)
    from_date: str = field(repr=False, default="")
    to_date: str = field(repr=False, default="")
    # Exchange rate
    base_currency: str = field(repr=False, default="")
    quote_currency: str = field(repr=False, default="")
    date_time: datetime | None = field(repr=False, default=None)
    # Rankings
    ranking_type: str = field(repr=False, default="")
    market_country: str = field(repr=False, default="")
    duration: str = field(repr=False, default="")
    exclude_investment_caution: bool | None = field(repr=False, default=None)
    until: str = field(repr=False, default="")
    currency: str = field(repr=False, default="")


# =========================================================================
# Request builder
# =========================================================================

def build_read_request(
        operation: TossReadOperation, *, token: str,
        account_seq: str | None = None,
        **kwargs) -> TossReadRequest:
    """Build a validated request for one of the 20 operations.

    Raises ``ValueError`` on invalid parameters. Raises ``TypeError`` if a
    boolean-only parameter receives a non-bool.
    """
    if not isinstance(operation, TossReadOperation):
        raise ValueError("invalid_operation")
    if not valid_token(token):
        raise ValueError("invalid_token")

    spec = SPEC_TABLE[operation]
    if spec.account_rule == "required":
        if not valid_account_seq(account_seq):
            raise ValueError("account_seq_required")
        if not valid_account_seq_int64(account_seq):
            raise ValueError("account_seq_not_int64")
    else:
        if account_seq is not None:
            raise ValueError("account_seq_forbidden")

    op = operation.value
    params: dict = {}

    if op == "BUYING_POWER":
        params["currency"] = _require_enum(kwargs.pop("currency", None), {"KRW", "USD"}, "currency")
        _reject_extra(kwargs)

    elif op == "SELLABLE_QUANTITY":
        _require_symbol(kwargs, params)
        _reject_extra(kwargs)

    elif op == "COMMISSIONS":
        _reject_extra(kwargs)

    elif op == "ORDERBOOK":
        _require_symbol(kwargs, params)
        _reject_extra(kwargs)

    elif op == "TRADES":
        _require_symbol(kwargs, params)
        _optional_count(kwargs, params, 1, 50)
        _reject_extra(kwargs)

    elif op == "PRICE_LIMITS":
        _require_symbol(kwargs, params)
        _reject_extra(kwargs)

    elif op == "STOCK_CANDLES":
        _require_symbol(kwargs, params)
        _require_interval(kwargs, params)
        _optional_count(kwargs, params, 1, 200)
        _optional_before(kwargs, params)
        _optional_adjusted(kwargs, params)
        _reject_extra(kwargs)

    elif op == "STOCKS":
        _require_batch_symbols(kwargs, params)
        _reject_extra(kwargs)

    elif op == "STOCK_INVESTOR_TRADING":
        _require_path_symbol(kwargs, params)
        if re.fullmatch(r"[0-9A-Z]{6}", params["symbol"]) is None:
            raise ValueError("invalid_kr_symbol")
        _optional_count(kwargs, params, 1, 100)
        _optional_until(kwargs, params)
        _reject_extra(kwargs)

    elif op == "STOCK_WARNINGS":
        _require_path_symbol(kwargs, params)
        _reject_extra(kwargs)

    elif op == "EXCHANGE_RATE":
        _optional_datetime(kwargs, params)
        _require_currencies(kwargs, params)
        _reject_extra(kwargs)

    elif op == "KR_MARKET_CALENDAR":
        _optional_date(kwargs, params)
        _reject_extra(kwargs)

    elif op == "US_MARKET_CALENDAR":
        _optional_date(kwargs, params)
        _reject_extra(kwargs)

    elif op == "RANKINGS":
        _require_ranking_type(kwargs, params)
        _require_market_country(kwargs, params)
        _require_duration(kwargs, params)
        _optional_bool(kwargs, params, "excludeInvestmentCaution",
                       "exclude_investment_caution")
        _optional_count(kwargs, params, 1, 100)
        _reject_extra(kwargs)

    elif op == "INDICATOR_PRICES":
        _require_indicator_symbols(kwargs, params)
        _reject_extra(kwargs)

    elif op == "INDICATOR_CANDLES":
        _require_indicator_path_symbol(kwargs, params)
        _require_interval(kwargs, params)
        _reject_bond_minute(params, kwargs)
        _optional_count(kwargs, params, 1, 200)
        _optional_before(kwargs, params)
        _reject_extra(kwargs)

    elif op == "INDICATOR_INVESTOR_TRADING":
        _require_investor_symbol(kwargs, params)
        _require_investor_interval(kwargs, params)
        _optional_count(kwargs, params, 1, 100)
        _optional_until(kwargs, params)
        _reject_extra(kwargs)

    elif op == "ORDERS":
        params["status"] = _require_enum(
            kwargs.pop("status"), _ORDER_STATUSES, "status")
        _optional_symbol(kwargs, params)
        _optional_named_date(kwargs, params, "from", "from_date")
        _optional_named_date(kwargs, params, "to", "to_date")
        _optional_cursor_unbounded(kwargs, params)
        _optional_limit(kwargs, params, 1, 100)
        _reject_extra(kwargs)

    elif op == "ORDER_DETAIL":
        params["order_id"] = _require_opaque_id_kwarg(kwargs, "order_id")
        _reject_extra(kwargs)

    elif op == "CONDITIONAL_ORDERS":
        params["status"] = _require_enum(
            kwargs.pop("status"), _ORDER_STATUSES, "status")
        _optional_symbol(kwargs, params)
        _optional_conditional_cursor(kwargs, params)
        _optional_limit(kwargs, params, 1, 100)
        _reject_extra(kwargs)

    elif op == "CONDITIONAL_ORDER_DETAIL":
        params["conditional_order_id"] = _require_opaque_id_kwarg(
            kwargs, "conditional_order_id")
        _reject_extra(kwargs)

    else:
        raise ValueError("invalid_operation")

    return TossReadRequest(
        operation=operation,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=token,
        account_seq=account_seq,
        **params,
    )


# =========================================================================
# Per-operation validator helpers
# =========================================================================

def _reject_extra(kwargs):
    if kwargs:
        raise ValueError(f"unexpected_parameters:{sorted(kwargs)}")


def _require_symbol(kwargs, params):
    sym = kwargs.pop("symbol")
    if not _valid_single_symbol(sym):
        raise ValueError("invalid_symbol")
    params["symbol"] = sym


def _require_path_symbol(kwargs, params):
    sym = kwargs.pop("symbol")
    if not _valid_single_symbol(sym):
        raise ValueError("invalid_symbol")
    params["symbol"] = sym


def _optional_symbol(kwargs, params):
    if "symbol" in kwargs:
        sym = kwargs.pop("symbol")
        if sym is not None:
            if not _valid_single_symbol(sym):
                raise ValueError("invalid_symbol")
            params["symbol"] = sym


def _require_batch_symbols(kwargs, params):
    syms = kwargs.pop("symbols")
    validated = _valid_batch_symbols(syms)
    if validated is None:
        raise ValueError("invalid_symbols")
    params["symbols"] = validated


def _require_indicator_symbols(kwargs, params):
    syms = kwargs.pop("symbols")
    validated = _valid_indicator_symbols(syms)
    if validated is None:
        raise ValueError("invalid_symbols")
    params["symbols"] = validated


def _require_indicator_path_symbol(kwargs, params):
    sym = kwargs.pop("symbol")
    if not isinstance(sym, str) or sym not in _INDICATOR_CATALOG:
        raise ValueError("invalid_symbol")
    params["symbol"] = sym


def _require_investor_symbol(kwargs, params):
    sym = kwargs.pop("symbol")
    if not isinstance(sym, str) or sym not in _INVESTOR_SYMBOLS:
        raise ValueError("invalid_symbol")
    params["symbol"] = sym


def _require_interval(kwargs, params):
    iv = kwargs.pop("interval")
    if iv not in _CANDLE_INTERVALS:
        raise ValueError("invalid_interval")
    params["interval"] = iv


def _require_investor_interval(kwargs, params):
    iv = kwargs.pop("interval")
    if iv not in _INVESTOR_INTERVALS:
        raise ValueError("invalid_interval")
    params["interval"] = iv


def _reject_bond_minute(params, kwargs):
    """Bond symbols only support ``1d``, not ``1m``."""
    sym = params.get("symbol", "")
    iv = params.get("interval", "")
    if sym.startswith(_BOND_PREFIX) and iv == "1m":
        raise ValueError("bond_minute_not_supported")


def _optional_count(kwargs, params, lo, hi):
    if "count" in kwargs:
        c = kwargs.pop("count")
        if _valid_int(c, lo, hi) is None:
            raise ValueError("invalid_count")
        params["count"] = c


def _optional_limit(kwargs, params, lo, hi):
    if "limit" in kwargs:
        c = kwargs.pop("limit")
        if _valid_int(c, lo, hi) is None:
            raise ValueError("invalid_limit")
        params["limit"] = c


def _optional_before(kwargs, params):
    if "before" in kwargs:
        b = kwargs.pop("before")
        if b is not None:
            dt = _valid_aware_datetime(b)
            if dt is None:
                raise ValueError("invalid_before")
            params["before"] = dt


def _optional_adjusted(kwargs, params):
    if "adjusted" in kwargs:
        a = kwargs.pop("adjusted")
        if a is not None:
            if not isinstance(a, bool):
                raise TypeError("adjusted_must_be_bool")
            params["adjusted"] = a


def _optional_bool(kwargs, params, key, dest):
    if key in kwargs:
        v = kwargs.pop(key)
        if v is not None:
            if not isinstance(v, bool):
                raise TypeError(f"{key}_must_be_bool")
            params[dest] = v


def _optional_date(kwargs, params):
    if "date" in kwargs:
        d = kwargs.pop("date")
        if d is not None:
            if not valid_iso_date(d):
                raise ValueError("invalid_date")
            params["date"] = d


def _optional_named_date(kwargs, params, key, dest):
    if key in kwargs:
        d = kwargs.pop(key)
        if d is not None:
            if not valid_iso_date(d):
                raise ValueError(f"invalid_{key}")
            params[dest] = d


def _optional_until(kwargs, params):
    if "until" in kwargs:
        d = kwargs.pop("until")
        if d is not None:
            if not valid_iso_date(d):
                raise ValueError("invalid_until")
            params["until"] = d


def _optional_datetime(kwargs, params):
    if "dateTime" in kwargs:
        dt = kwargs.pop("dateTime")
        if dt is not None:
            validated = _valid_aware_datetime(dt)
            if validated is None:
                raise ValueError("invalid_dateTime")
            params["date_time"] = validated


def _require_currencies(kwargs, params):
    base = kwargs.pop("baseCurrency")
    quote = kwargs.pop("quoteCurrency")
    if base not in _CURRENCIES:
        raise ValueError("invalid_baseCurrency")
    if quote not in _CURRENCIES:
        raise ValueError("invalid_quoteCurrency")
    params["base_currency"] = base
    params["quote_currency"] = quote


def _require_ranking_type(kwargs, params):
    rt = kwargs.pop("type")
    if rt not in _RANKING_TYPES:
        raise ValueError("invalid_type")
    params["ranking_type"] = rt


def _require_market_country(kwargs, params):
    mc = kwargs.pop("marketCountry")
    if mc not in _MARKET_COUNTRIES:
        raise ValueError("invalid_marketCountry")
    params["market_country"] = mc


def _require_duration(kwargs, params):
    dur = kwargs.pop("duration")
    if dur not in _RANKING_DURATIONS:
        raise ValueError("invalid_duration")
    # TOP_GAINERS/TOP_LOSERS + realtime rejected before I/O
    rt = kwargs.get("type") or kwargs.get("ranking_type")
    # ranking_type may already be popped into params; check params too
    rt = params.get("ranking_type", rt)
    if rt in _TOP_RANKING_TYPES and dur == "realtime":
        raise ValueError("top_ranking_realtime_not_supported")
    params["duration"] = dur


def _require_enum(value, valid_set, name):
    if value not in valid_set:
        raise ValueError(f"invalid_{name}")
    return value


def _optional_cursor_unbounded(kwargs, params):
    if "cursor" in kwargs:
        c = kwargs.pop("cursor")
        if c is not None:
            if not isinstance(c, str) or not c:
                raise ValueError("invalid_cursor")
            params["cursor"] = c


def _optional_conditional_cursor(kwargs, params):
    if "cursor" in kwargs:
        c = kwargs.pop("cursor")
        if c is not None:
            if not isinstance(c, str) or _CURSOR_RE.fullmatch(c) is None:
                raise ValueError("invalid_cursor")
            params["cursor"] = c


def _require_opaque_id_kwarg(kwargs, key):
    val = kwargs.pop(key)
    validated = _valid_opaque_id(val)
    if validated is None:
        raise ValueError(f"invalid_{key}")
    return validated


# =========================================================================
# Query outcome
# =========================================================================

@dataclass(frozen=True)
class TossReadOutcome:
    """Closed outcome: ``success`` with a ``repr=False`` value, or a closed
    failure code with no value.
    """

    code: str
    value: object = field(repr=False, default=None)
    rate_limit_remaining: int | None = None
    retry_after: int | None = None


# =========================================================================
# Response parsing (called by query_read after transport returns body)
# =========================================================================

def parse_read_response(*, req: TossReadRequest, body: bytes,
                        fetched_at: datetime) -> TossReadOutcome:
    """Parse the transport body into a typed value or closed failure."""
    op = req.operation.value
    parsed = parse_envelope(
        body, op,
        requested_symbols=req.symbols if req.symbols else None,
        status=req.status if req.status else None,
    )
    if parsed is _FAIL:
        return TossReadOutcome("parse_error")
    if parsed is _WRONG:
        return TossReadOutcome("contract_mismatch")
    return TossReadOutcome("success", value=parsed)


# =========================================================================
# One-request orchestration
# =========================================================================

def query_read(
        request: TossReadRequest, *,
        transport: Callable,
        clock: Callable[[], datetime]) -> TossReadOutcome:
    """Start exactly one transport attempt, then parse atomically.

    No retry, no auto-pagination, no loop. One call to ``transport``,
    one parse, one outcome.
    """
    reply = transport(request)
    if hasattr(reply, "body"):
        parsed = parse_read_response(
            req=request, body=reply.body, fetched_at=clock())
        return TossReadOutcome(
            parsed.code,
            parsed.value,
            rate_limit_remaining=getattr(reply, "rate_limit_remaining", None),
        )
    return TossReadOutcome(
        getattr(reply, "code", "protocol_failure"),
        retry_after=getattr(reply, "retry_after", None),
    )
