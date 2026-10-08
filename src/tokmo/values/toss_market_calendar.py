"""Toss market calendar types: KR/US sessions and days."""

from dataclasses import dataclass, field
from datetime import date, datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "CalendarSession", "IntegratedHour", "KrMarketDay",
    "KrMarketCalendarResponse", "UsMarketDay", "UsMarketCalendarResponse",
]


@dataclass(frozen=True)
class CalendarSession:
    start_time: datetime
    end_time: datetime
    single_price_auction_start_time: datetime | None | Missing = MISSING
    single_price_auction_end_time: datetime | None | Missing = MISSING


@dataclass(frozen=True)
class IntegratedHour:
    pre_market: CalendarSession | None | Missing = MISSING
    regular_market: CalendarSession | None | Missing = MISSING
    after_market: CalendarSession | None | Missing = MISSING


@dataclass(frozen=True)
class KrMarketDay:
    date: date
    integrated: IntegratedHour | None | Missing = MISSING


@dataclass(frozen=True)
class KrMarketCalendarResponse:
    today: KrMarketDay
    previous_business_day: KrMarketDay
    next_business_day: KrMarketDay


@dataclass(frozen=True)
class UsMarketDay:
    date: date
    day_market: CalendarSession | None | Missing = MISSING
    pre_market: CalendarSession | None | Missing = MISSING
    regular_market: CalendarSession | None | Missing = MISSING
    after_market: CalendarSession | None | Missing = MISSING


@dataclass(frozen=True)
class UsMarketCalendarResponse:
    today: UsMarketDay
    previous_business_day: UsMarketDay
    next_business_day: UsMarketDay
