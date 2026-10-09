"""Toss market indicator types: price, candle, candle page."""

from dataclasses import dataclass, field
from datetime import datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "MarketIndicatorCandle",
    "MarketIndicatorCandlePageResponse",
    "MarketIndicatorPrice",
]


@dataclass(frozen=True)
class MarketIndicatorPrice:
    symbol: str
    last_price: str = field(repr=False)
    timestamp: datetime | None | Missing = MISSING


@dataclass(frozen=True)
class MarketIndicatorCandle:
    timestamp: datetime
    open_price: str = field(repr=False)
    high_price: str = field(repr=False)
    low_price: str = field(repr=False)
    close_price: str = field(repr=False)
    volume: str = field(repr=False)


@dataclass(frozen=True)
class MarketIndicatorCandlePageResponse:
    candles: tuple[MarketIndicatorCandle, ...] = field(repr=False)
    next_before: str | None | Missing = field(default=MISSING, repr=False)
