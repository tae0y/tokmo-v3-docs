"""Typed KRX KOSPI daily trading observation."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal


@dataclass(frozen=True)
class KrxDailyTradeFact:
    source: str
    base_date: date
    issue_code: str
    issue_name: str
    market_name: str
    sector_type_name: str
    close_price: Decimal
    change_from_previous: Decimal
    fluctuation_rate: Decimal
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    accumulated_volume: int
    accumulated_value: int
    market_cap: int
    listed_shares: int
    observed_at: datetime

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.issue_code}:{self.base_date.isoformat()}"
