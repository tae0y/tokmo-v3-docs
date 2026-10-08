"""Toss order types: execution, order, paginated, conditional."""

from dataclasses import dataclass, field
from datetime import date, datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "OrderExecution", "Order", "PaginatedOrderResponse",
    "ConditionalOrderCondition", "ConditionalOrderDetailResponse",
    "PaginatedConditionalOrderResponse",
]


@dataclass(frozen=True)
class OrderExecution:
    filled_quantity: str = field(repr=False)
    average_filled_price: str | None = field(default=None, repr=False)
    filled_amount: str | None = field(default=None, repr=False)
    commission: str | None = field(default=None, repr=False)
    tax: str | None = field(default=None, repr=False)
    filled_at: datetime | None = field(default=None, repr=False)
    settlement_date: date | None = field(default=None, repr=False)


@dataclass(frozen=True)
class Order:
    order_id: str = field(repr=False)
    symbol: str = field(repr=False)
    side: str
    order_type: str
    time_in_force: str
    status: str
    quantity: str = field(repr=False)
    currency: str
    ordered_at: datetime
    execution: OrderExecution = field(repr=False)
    price: str | None | Missing = field(default=MISSING, repr=False)
    order_amount: str | None | Missing = field(default=MISSING, repr=False)
    canceled_at: datetime | None | Missing = field(default=MISSING, repr=False)


@dataclass(frozen=True)
class PaginatedOrderResponse:
    orders: tuple[Order, ...] = field(repr=False)
    next_cursor: str | None = field(default=None, repr=False)
    has_next: bool = False


@dataclass(frozen=True)
class ConditionalOrderCondition:
    type: str
    status: str
    order_side: str | None | Missing = field(default=MISSING, repr=False)
    trigger_price: str | None | Missing = field(default=MISSING, repr=False)
    target_profit_rate: str | None | Missing = field(default=MISSING, repr=False)
    order_price: str | None | Missing = field(default=MISSING, repr=False)
    triggered_order_id: str | None | Missing = field(default=MISSING, repr=False)


@dataclass(frozen=True)
class ConditionalOrderDetailResponse:
    conditional_order_id: str = field(repr=False)
    type: str
    status: str
    symbol: str = field(repr=False)
    market: str
    quantity: str = field(repr=False)
    order_type: str
    first: ConditionalOrderCondition = field(repr=False)
    created_at: datetime
    expire_date: date | None | Missing = MISSING
    second: ConditionalOrderCondition | None | Missing = MISSING


@dataclass(frozen=True)
class PaginatedConditionalOrderResponse:
    conditional_orders: tuple[ConditionalOrderDetailResponse, ...] = field(repr=False)
    has_next: bool = False
    next_cursor: str | None | Missing = field(default=MISSING, repr=False)
