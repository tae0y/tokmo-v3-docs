"""Toss market data types: orderbook, trade, price-limit, candle."""

from dataclasses import dataclass, field
from datetime import datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "Candle",
    "CandlePageResponse",
    "OrderbookEntry",
    "OrderbookResponse",
    "PriceLimitResponse",
    "Trade",
]


@dataclass(frozen=True)
class OrderbookEntry:
    price: str = field(repr=False)
    volume: str = field(repr=False)


@dataclass(frozen=True)
class OrderbookResponse:
    currency: str
    asks: tuple[OrderbookEntry, ...] = field(repr=False)
    bids: tuple[OrderbookEntry, ...] = field(repr=False)
    timestamp: datetime | None | Missing = MISSING


@dataclass(frozen=True)
class Trade:
    price: str = field(repr=False)
    volume: str = field(repr=False)
    timestamp: datetime
    currency: str


@dataclass(frozen=True)
class PriceLimitResponse:
    timestamp: datetime
    currency: str
    upper_limit_price: str | None | Missing = field(default=MISSING, repr=False)
    lower_limit_price: str | None | Missing = field(default=MISSING, repr=False)


@dataclass(frozen=True)
class Candle:
    timestamp: datetime
    open_price: str = field(repr=False)
    high_price: str = field(repr=False)
    low_price: str = field(repr=False)
    close_price: str = field(repr=False)
    volume: str = field(repr=False)
    currency: str


@dataclass(frozen=True)
class CandlePageResponse:
    candles: tuple[Candle, ...] = field(repr=False)
    next_before: str | None | Missing = field(default=MISSING, repr=False)
