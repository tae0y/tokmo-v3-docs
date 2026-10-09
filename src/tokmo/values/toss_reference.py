"""Toss reference types: stock info, market detail, warning, exchange rate."""

from dataclasses import dataclass, field
from datetime import date, datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "ExchangeRateResponse",
    "KrMarketDetail",
    "StockInfo",
    "StockWarning",
]


@dataclass(frozen=True)
class KrMarketDetail:
    liquidation_trading: bool
    nxt_supported: bool
    krx_trading_suspended: bool
    nxt_trading_suspended: bool | None | Missing = MISSING


@dataclass(frozen=True)
class StockInfo:
    symbol: str = field(repr=False)
    name: str = field(repr=False)
    english_name: str = field(repr=False)
    isin_code: str = field(repr=False)
    market: str
    security_type: str
    is_common_share: bool
    status: str
    currency: str
    shares_outstanding: str = field(repr=False)
    list_date: date | None | Missing = MISSING
    delist_date: date | None | Missing = MISSING
    leverage_factor: str | None | Missing = field(default=MISSING, repr=False)
    korean_market_detail: KrMarketDetail | None | Missing = MISSING


@dataclass(frozen=True)
class StockWarning:
    warning_type: str
    exchange: str | None | Missing = MISSING
    start_date: date | None | Missing = MISSING
    end_date: date | None | Missing = MISSING


@dataclass(frozen=True)
class ExchangeRateResponse:
    base_currency: str
    quote_currency: str
    rate: str = field(repr=False)
    mid_rate: str = field(repr=False)
    basis_point: str = field(repr=False)
    rate_change_type: str
    valid_from: datetime
    valid_until: datetime
