"""Immutable order command vocabulary; no transport, storage or strategy."""

from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class OrderOperation(str, Enum):
    CREATE = "CREATE"
    MODIFY = "MODIFY"
    CANCEL = "CANCEL"
    CONDITIONAL_CREATE = "CONDITIONAL_CREATE"
    CONDITIONAL_MODIFY = "CONDITIONAL_MODIFY"
    CONDITIONAL_CANCEL = "CONDITIONAL_CANCEL"


@dataclass(frozen=True)
class RegularOrder:
    side: str
    order_type: str
    quantity: str | None = field(default=None, repr=False)
    price: str | None = field(default=None, repr=False)
    client_order_id: str | None = field(default=None, repr=False)
    time_in_force: str = "DAY"
    order_amount: str | None = field(default=None, repr=False)
    confirm_high_value: bool = False


@dataclass(frozen=True)
class ReplaceOrder:
    order_type: str
    quantity: str | None = field(default=None, repr=False)
    price: str | None = field(default=None, repr=False)
    confirm_high_value: bool = False


@dataclass(frozen=True)
class CancelOrder:
    pass


@dataclass(frozen=True)
class Condition:
    side: str
    trigger_price: str = field(repr=False)
    order_price: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class ConditionalOrder:
    kind: str
    quantity: str = field(repr=False)
    order_type: str
    expire_date: date
    first: Condition = field(repr=False)
    second: Condition | None = field(default=None, repr=False)
    client_order_id: str | None = field(default=None, repr=False)
    confirm_high_value: bool = False


@dataclass(frozen=True)
class OrderCommand:
    operation: OrderOperation
    market: str
    symbol: str = field(repr=False)
    payload: RegularOrder | ReplaceOrder | CancelOrder | ConditionalOrder = field(
        repr=False
    )
    target_id: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class OrderWireRequest:
    command: OrderCommand = field(repr=False)
    token: str = field(repr=False)
    account_ref: str = field(repr=False)


@dataclass(frozen=True)
class OrderWireResponse:
    status: int
    body: bytes = field(repr=False)


@dataclass(frozen=True)
class OrderCommandResult:
    """ACCEPTED acknowledges the command, never certifies a fill/cancellation."""

    status: str
    code: str
    order_id: str | None = field(default=None, repr=False)
    previous_order_id: str | None = field(default=None, repr=False)
    request_id: str | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.status not in {"ACCEPTED", "REJECTED", "UNKNOWN", "NOT_SENT"}:
            raise ValueError("order_result_status")


class OrderTransportDisabled(RuntimeError):
    """The transport refused before creating a network request."""
