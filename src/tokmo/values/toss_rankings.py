"""Toss ranking types: price, item, response."""

from dataclasses import dataclass, field
from datetime import datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = ["RankingPrice", "RankingItem", "RankingResponse"]


@dataclass(frozen=True)
class RankingPrice:
    last_price: str = field(repr=False)
    base_price: str = field(repr=False)
    change_rate: str | None | Missing = field(default=MISSING, repr=False)


@dataclass(frozen=True)
class RankingItem:
    rank: int
    symbol: str = field(repr=False)
    currency: str
    price: RankingPrice
    trading_volume: str = field(repr=False)
    trading_amount: str = field(repr=False)


@dataclass(frozen=True)
class RankingResponse:
    rankings: tuple[RankingItem, ...] = field(repr=False)
    ranked_at: datetime | None | Missing = MISSING
