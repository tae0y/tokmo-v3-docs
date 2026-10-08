from datetime import date, datetime, timezone

import pytest

from tokmo.values.completed_execution import (
    CompletedExecutionFact,
    ExecutionCompletenessViolation,
    is_completed_execution,
    to_completed_execution,
    to_trade_ledger_event,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_orders import Order, OrderExecution


def order(status="FILLED", *, qty="1.00", price="185.50", amount="185.50",
          filled_at=datetime(2026, 7, 29, 15, tzinfo=timezone.utc)):
    return Order(
        "order-1", "AAPL", "BUY", "MARKET", "DAY", status, "2", "USD",
        datetime(2026, 7, 28, tzinfo=timezone.utc),
        OrderExecution(qty, price, amount, "0.10", "0.00", filled_at,
                       date(2026, 7, 31)),
        price="999",
    )


@pytest.mark.parametrize(
    "status", ["FILLED", "CANCELED", "REJECTED", "REPLACED", "PARTIAL_FILLED"],
)
def test_closed_group_provenance_accepts_actual_positive_fills(status):
    candidate = order(status, qty="0.25")
    assert is_completed_execution(True, candidate)
    assert not is_completed_execution(False, candidate)


def test_per_order_closed_is_not_provenance_and_zero_is_not_completed():
    assert not is_completed_execution(False, order("CLOSED"))
    assert not is_completed_execution(True, order(qty="0"))


@pytest.mark.parametrize("missing", ["price", "amount", "filled_at"])
def test_six_requirements_never_guess_missing_execution(missing):
    kwargs = {missing: None}
    result = to_completed_execution(
        order(**kwargs), account_scope="toss-acct-" + "a" * 24,
        classified_market=Market.US, page_provenance_closed=True,
    )
    assert isinstance(result, ExecutionCompletenessViolation)


def test_event_mapping_uses_filled_at_timezone_and_canonical_decimals():
    fact = to_completed_execution(
        order("CANCELED", qty="0.2500"),
        account_scope="toss-acct-" + "a" * 24,
        classified_market=Market.US, page_provenance_closed=True,
    )
    assert isinstance(fact, CompletedExecutionFact)
    event = to_trade_ledger_event(fact)
    assert (event.source, event.event_kind, event.medium_raw) == (
        "toss-orders-v1", "EXECUTION", "toss-orders",
    )
    assert event.trade_date == date(2026, 7, 29)
    assert (event.quantity, event.unit_price, event.amount, event.fee, event.tax) == (
        "0.25", "185.5", "185.5", "0.1", "0",
    )
    assert event.supersedes_source_record_key is None
