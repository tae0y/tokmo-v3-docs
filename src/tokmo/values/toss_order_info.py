"""Public Order Info GET response values (official OpenAPI 1.2.14)."""
from dataclasses import dataclass, field
from datetime import date


@dataclass(frozen=True)
class BuyingPowerResponse:
    currency: str
    cash_buying_power: str = field(repr=False)


@dataclass(frozen=True)
class SellableQuantityResponse:
    sellable_quantity: str = field(repr=False)


@dataclass(frozen=True)
class Commission:
    market_country: str
    commission_rate: str
    start_date: date | None = None
    end_date: date | None = None
