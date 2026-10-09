"""Request-bound permits and account/position constraints; no I/O."""

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from decimal import Decimal

from tokmo.values.order_commands import (
    ConditionalOrder,
    OrderCommand,
    OrderOperation,
    RegularOrder,
)


def command_document(command):
    if type(command) is not OrderCommand:
        raise ValueError("order_command")
    return json.dumps(
        asdict(command),
        sort_keys=True,
        separators=(",", ":"),
        default=lambda value: value.isoformat(),
        allow_nan=False,
    )


def command_digest(command):
    return hashlib.sha256(command_document(command).encode()).hexdigest()


@dataclass(frozen=True)
class ExecutionPermit:
    account_scope: str = field(repr=False)
    digest: str
    mode: str
    valid_until: datetime
    plan_version: str
    snapshot_at: datetime | None = None


@dataclass(frozen=True)
class RiskEnvelope:
    account_scope: str = field(repr=False)
    symbol: str
    market: str
    max_quantity: Decimal
    initial_budget: Decimal
    protected_quantity: Decimal
    valid_until: datetime
    plan_version: str


@dataclass(frozen=True)
class RiskSnapshot:
    observed_at: datetime
    total_quantity: Decimal
    experiment_quantity: Decimal
    pending_buy: Decimal
    pending_sell: Decimal
    available_cash: Decimal
    fee_buffer: Decimal
    unresolved: bool
    account_scope: str = field(repr=False)
    symbol: str
    market: str
    funded_amount: Decimal | None = None


def aware(value):
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _nonnegative(value):
    return isinstance(value, Decimal) and value.is_finite() and value >= 0


def authorize_order(command, envelope, snapshot, *, now, mode):
    """Authorize a new order using a fresh, reconciled experiment cash book.

    available_cash is experiment cash, not the whole brokerage cash balance.
    Replacements/cancellations require a separately reviewed target-bound permit.
    Provider-side shape/session validation remains the Facts adapter's job.
    """
    if type(envelope) is not RiskEnvelope or type(snapshot) is not RiskSnapshot:
        raise ValueError("risk_inputs")
    if mode not in {"paper", "live"} or not all(
        aware(v) for v in (now, envelope.valid_until, snapshot.observed_at)
    ):
        raise ValueError("risk_clock")
    if (
        not 0 <= (now - snapshot.observed_at).total_seconds() <= 30
        or now >= envelope.valid_until
    ):
        raise ValueError("risk_expired")
    if not envelope.plan_version or not envelope.account_scope:
        raise ValueError("risk_identity")
    if (snapshot.account_scope, snapshot.symbol, snapshot.market) != (
        envelope.account_scope,
        envelope.symbol,
        envelope.market,
    ):
        raise ValueError("snapshot_identity")
    if (command.symbol, command.market) != (envelope.symbol, envelope.market):
        raise ValueError("instrument_outside_envelope")
    numbers = (
        envelope.max_quantity,
        envelope.initial_budget,
        envelope.protected_quantity,
        snapshot.total_quantity,
        snapshot.experiment_quantity,
        snapshot.pending_buy,
        snapshot.pending_sell,
        snapshot.available_cash,
        snapshot.fee_buffer,
        snapshot.funded_amount,
    )
    if (
        not all(_nonnegative(v) for v in numbers)
        or envelope.max_quantity <= 0
        or envelope.initial_budget <= 0
    ):
        raise ValueError("risk_numbers")
    if type(snapshot.unresolved) is not bool or snapshot.unresolved:
        raise ValueError("unresolved_orders")
    if snapshot.funded_amount > envelope.initial_budget:
        raise ValueError("additional_funding")
    if (
        snapshot.total_quantity
        != envelope.protected_quantity + snapshot.experiment_quantity
    ):
        raise ValueError("protected_position_changed")
    if snapshot.experiment_quantity + snapshot.pending_buy > envelope.max_quantity:
        raise ValueError("existing_exposure_exceeded")
    order = command.payload
    if command.operation == OrderOperation.CREATE and type(order) is RegularOrder:
        if order.quantity is None or order.order_amount is not None:
            raise ValueError("quantity_required_for_experiment")
        quantity = Decimal(order.quantity)
        side, price = order.side, order.price
    elif (
        command.operation == OrderOperation.CONDITIONAL_CREATE
        and type(order) is ConditionalOrder
    ):
        quantity = Decimal(order.quantity)
        side, price = order.first.side, order.first.order_price
        # A date is not an intraday cutoff. Until the provider's expiration
        # timezone is normalized, use the latest civil-day boundary (UTC-12).
        expiration_bound = datetime.combine(
            order.expire_date + timedelta(days=1),
            time.min,
            tzinfo=timezone(timedelta(hours=-12)),
        )
        if expiration_bound > envelope.valid_until:
            raise ValueError("condition_beyond_envelope")
        if order.kind == "OTO" and snapshot.pending_sell:
            raise ValueError("oto_sell_conflict")
    else:
        raise ValueError("target_review_required")
    if (
        not _nonnegative(quantity)
        or quantity <= 0
        or quantity != quantity.to_integral_value()
    ):
        raise ValueError("quantity")
    if side == "BUY":
        if (
            quantity + snapshot.experiment_quantity + snapshot.pending_buy
            > envelope.max_quantity
        ):
            raise ValueError("position_limit")
        if order.order_type != "LIMIT" or price is None:
            raise ValueError("bounded_buy_price_required")
        amount = Decimal(price) * quantity
        if not _nonnegative(amount) or amount <= 0:
            raise ValueError("buy_amount")
        if amount + snapshot.fee_buffer > min(
            snapshot.available_cash, envelope.initial_budget
        ):
            raise ValueError("experiment_cash_limit")
    elif side == "SELL":
        if quantity + snapshot.pending_sell > snapshot.experiment_quantity:
            raise ValueError("protected_shares_not_sellable")
    else:
        raise ValueError("side")
    return ExecutionPermit(
        envelope.account_scope,
        command_digest(command),
        mode,
        min(envelope.valid_until, now + timedelta(seconds=15)),
        envelope.plan_version,
        snapshot.observed_at,
    )
