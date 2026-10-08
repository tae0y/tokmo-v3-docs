"""Typed conversion boundary from closed order pages to ledger events."""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from zoneinfo import ZoneInfo

from tokmo.values.decimal_canon import (
    Decimal30CanonicalizationViolation,
    canonicalize_decimal30,
)
from tokmo.values.session_gate import MARKET_TIMEZONE, Market
from tokmo.values.toss_orders import Order
from tokmo.values.trade_ledger import TradeLedgerEvent


@dataclass(frozen=True)
class ExecutionCompletenessViolation:
    reason: str


@dataclass(frozen=True)
class CompletedExecutionFact:
    account_scope: str
    order_id: str
    symbol: str
    side: str
    currency: str
    filled_quantity: str
    average_filled_price: str
    filled_amount: str
    filled_at: datetime
    commission: str | None
    tax: str | None
    settlement_date: date | None
    classified_market: Market


def is_completed_execution(page_provenance_closed: bool, order: Order) -> bool:
    if page_provenance_closed is not True:
        return False
    try:
        return Decimal(order.execution.filled_quantity) > 0
    except (InvalidOperation, TypeError):
        return False


def to_completed_execution(
    order: Order, *, account_scope: str, classified_market: Market | None,
    page_provenance_closed: bool,
) -> CompletedExecutionFact | ExecutionCompletenessViolation:
    execution = order.execution
    if not is_completed_execution(page_provenance_closed, order):
        return ExecutionCompletenessViolation("not_completed_execution")
    if classified_market is None:
        return ExecutionCompletenessViolation("classified_market_missing")
    if execution.average_filled_price is None:
        return ExecutionCompletenessViolation("average_filled_price_missing")
    if execution.filled_amount is None:
        return ExecutionCompletenessViolation("filled_amount_missing")
    if execution.filled_at is None:
        return ExecutionCompletenessViolation("filled_at_missing")
    if execution.filled_at.tzinfo is None or execution.filled_at.utcoffset() is None:
        return ExecutionCompletenessViolation("filled_at_invalid")
    return CompletedExecutionFact(
        account_scope, order.order_id, order.symbol, order.side, order.currency,
        execution.filled_quantity, execution.average_filled_price,
        execution.filled_amount, execution.filled_at, execution.commission,
        execution.tax, execution.settlement_date, classified_market,
    )


def to_trade_ledger_event(
    fact: CompletedExecutionFact, *, source: str = "toss-orders-v1",
) -> TradeLedgerEvent:
    values = [
        canonicalize_decimal30(value) for value in (
            fact.filled_quantity, fact.average_filled_price, fact.filled_amount,
        )
    ]
    nullable = [
        None if value is None else canonicalize_decimal30(value)
        for value in (fact.commission, fact.tax)
    ]
    if any(isinstance(value, Decimal30CanonicalizationViolation)
           for value in (*values, *nullable)):
        raise ValueError("invalid_execution_decimal")
    return TradeLedgerEvent(
        source=source, account_scope=fact.account_scope,
        source_record_key=fact.order_id, event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=fact.filled_at.astimezone(
            ZoneInfo(MARKET_TIMEZONE[fact.classified_market])
        ).date(),
        transaction_type_raw=fact.side, instrument_raw=fact.symbol,
        quantity=values[0], unit_price=values[1], amount=values[2],
        medium_raw="toss-orders", currency_raw=fact.currency,
        settlement_date=fact.settlement_date, fee=nullable[0], tax=nullable[1],
    )
