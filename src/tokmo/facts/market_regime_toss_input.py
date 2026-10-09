"""Pure Toss daily-candle boundary and normalization for market-regime shadow."""

import hashlib
import json
import re
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from tokmo.values.market_regime_core import (
    AdjustmentMode,
    NormalizedDailyBarV1,
    NormalizedDailySeriesV1,
)
from tokmo.values.request_parity import REQUEST_OPERATION_SPECS
from tokmo.values.session_gate import MARKET_TIMEZONE, Market

_SYMBOL = re.compile(r"[A-Z0-9._-]{1,32}\Z", re.ASCII)


def completed_session_boundary(market: Market, session_date: date) -> datetime:
    if not isinstance(market, Market) or type(session_date) is not date:
        raise ValueError("invalid completed session")
    return datetime.combine(
        session_date,
        time(23, 59, 59),
        ZoneInfo(MARKET_TIMEZONE[market]),
    )


def candle_native_identity(
    lane: str, market: Market, symbol: str, session_date: date, page_index: int
) -> str:
    symbol = symbol.upper() if isinstance(symbol, str) else ""
    if (
        lane not in ("mr", "qt")
        or not isinstance(market, Market)
        or _SYMBOL.fullmatch(symbol) is None
        or type(session_date) is not date
        or page_index not in (1, 2)
        or (lane == "mr" and page_index != 1)
    ):
        raise ValueError("invalid candle identity")
    return f"candles:{lane}:{market.value}:{symbol}:{session_date.isoformat()}:{page_index}"


def candle_request_identity(
    *,
    lane: str,
    market: Market,
    symbol: str,
    completed_session: date,
    adjusted: bool,
    page_index: int = 1,
) -> str:
    boundary = completed_session_boundary(market, completed_session)
    payload = (
        REQUEST_OPERATION_SPECS["STOCK_CANDLES_DAILY"][1],
        lane,
        market.value,
        symbol.upper(),
        "1d",
        200,
        adjusted,
        boundary.isoformat(),
        page_index,
    )
    return hashlib.sha256(
        json.dumps(payload, separators=(",", ":")).encode()
    ).hexdigest()


def _value(row, *names):
    for name in names:
        if isinstance(row, dict) and name in row:
            return row[name]
        if hasattr(row, name):
            return getattr(row, name)
    raise ValueError("malformed candle")


def normalize_daily_candles(
    rows,
    *,
    market: Market,
    symbol: str,
    completed_session: date,
    adjustment_mode: AdjustmentMode,
) -> NormalizedDailySeriesV1:
    rows = tuple(rows)
    if not 1 <= len(rows) <= 200:
        raise ValueError("candle count")
    zone = ZoneInfo(MARKET_TIMEZONE[market])
    stamps = []
    for row in rows:
        stamp = _value(row, "timestamp")
        if (
            not isinstance(stamp, datetime)
            or stamp.tzinfo is None
            or stamp.utcoffset() is None
        ):
            raise ValueError("naive candle timestamp")
        stamps.append(stamp)
    if any(left <= right for left, right in zip(stamps, stamps[1:])):
        raise ValueError("candles must be strict descending")
    currencies = {_value(row, "currency") for row in rows}
    if len(currencies) != 1:
        raise ValueError("mixed currency")
    bars = []
    for row in rows:
        stamp = _value(row, "timestamp").astimezone(zone)
        if stamp.date() > completed_session:
            continue
        bars.append(
            NormalizedDailyBarV1(
                stamp.date(),
                stamp,
                Decimal(_value(row, "open", "open_price")),
                Decimal(_value(row, "high", "high_price")),
                Decimal(_value(row, "low", "low_price")),
                Decimal(_value(row, "close", "close_price")),
                Decimal(_value(row, "volume")),
            )
        )
    bars.reverse()
    if len(bars) != 200:
        raise ValueError("completed candle count must be 200")
    return NormalizedDailySeriesV1(
        symbol.upper(),
        next(iter(currencies)),
        adjustment_mode,
        MARKET_TIMEZONE[market],
        completed_session,
        tuple(bars),
    )
