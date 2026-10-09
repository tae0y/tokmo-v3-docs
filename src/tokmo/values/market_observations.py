"""Market observation types: WTI, yield curve, Toss current price.

Extracted from values/__init__.py as three non-contiguous ranges:
  - YieldCurveFact (originally line 72-86)
  - WtiFact (originally line 88-104)
  - TossCurrentPriceFact (originally line 256-270)
"""

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal

__all__ = ["TossCurrentPriceFact", "WtiFact", "YieldCurveFact"]


@dataclass(frozen=True)
class YieldCurveFact:
    source: str
    market: str
    rate_date: date
    two_year_percent: Decimal | None
    ten_year_percent: Decimal | None
    observed_at: datetime

    @property
    def spread_percentage_points(self) -> Decimal | None:
        if self.two_year_percent is None or self.ten_year_percent is None:
            return None
        return self.ten_year_percent - self.two_year_percent


@dataclass(frozen=True)
class WtiFact:
    source: str
    series: str
    period: date
    value: Decimal
    units: str
    observed_at: datetime

    @property
    def usd_per_barrel(self) -> Decimal:
        return self.value

    @property
    def source_ref(self) -> str:
        return f"{self.source}:{self.series}:{self.period.isoformat()}"


@dataclass(frozen=True)
class TossCurrentPriceFact:
    """One unclassified provider current-price observation.

    The symbol and provider values are excluded from generated representations
    so price-bearing data cannot leak through routine exception/report paths.
    """

    source: str
    symbol: str = field(repr=False)
    last_price: str = field(repr=False)
    currency_raw: str = field(repr=False)
    timestamp: datetime | None
    fetched_at: datetime
