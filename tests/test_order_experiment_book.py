"""An experiment never refills its cash from the rest of the account."""
from dataclasses import replace
from decimal import Decimal
import stat

import pytest

from test_order_commands import buy, NOW
from test_order_execution import journal, limits
from tokmo.records.order_command_journal import CommandConflict, OrderCommandJournal
from tokmo.values.order_execution import authorize_order


def book_snapshot(store, *, total="7", mode="paper"):
    return store.experiment_snapshot("hyundai-test", mode=mode, total_quantity=Decimal(total),
                                     pending_buy=Decimal(0), pending_sell=Decimal(0),
                                     fee_buffer=Decimal(1000), observed_at=NOW)


def fill(store, *, key, side, amount, quantity="1", costs="500"):
    return store.apply_experiment_fill("hyundai-test", mode="paper", fill_id=key,
                                       symbol="005380", side=side, quantity=Decimal(quantity),
                                       amount=Decimal(amount), costs=Decimal(costs), observed_at=NOW)


def test_costs_losses_and_restart_cannot_reset_budget(tmp_path):
    store = journal(tmp_path)
    store.initialize_experiment("hyundai-test", mode="paper", envelope=limits())
    assert fill(store, key="buy-1", side="BUY", amount="380000") == "APPLIED"
    assert book_snapshot(store, total="8").available_cash == Decimal("19500")
    assert fill(store, key="sell-1", side="SELL", amount="370000") == "APPLIED"
    reopened = OrderCommandJournal(store.path)
    reopened.initialize_experiment("hyundai-test", mode="paper", envelope=limits())
    snap = book_snapshot(reopened)
    assert snap.available_cash == Decimal("389000")
    assert snap.funded_amount == Decimal("400000") and snap.experiment_quantity == 0
    # This price fits the initial allocation but exceeds the remaining cash.
    expensive = replace(buy(), payload=replace(buy().payload, price="390000"))
    with pytest.raises(ValueError, match="experiment_cash_limit"):
        authorize_order(expensive, limits(), snap, now=NOW, mode="paper")
    with pytest.raises(CommandConflict):
        reopened.initialize_experiment("hyundai-test", mode="paper", envelope=replace(limits(), initial_budget=Decimal("500000")))


def test_each_broker_execution_is_applied_once(tmp_path):
    store = journal(tmp_path)
    store.initialize_experiment("hyundai-test", mode="paper", envelope=limits())
    fill(store, key="buy-1", side="BUY", amount="380000")
    assert fill(store, key="buy-1", side="BUY", amount="380000") == "DUPLICATE"
    assert book_snapshot(store, total="8").experiment_quantity == 1
    with pytest.raises(CommandConflict): fill(store, key="buy-1", side="BUY", amount="370000")


@pytest.mark.parametrize("side,amount,quantity", [("BUY", "410000", "1"), ("BUY", "380000", "2"), ("SELL", "380000", "1")])
def test_unexpected_real_fills_are_preserved_but_freeze_future_orders(tmp_path, side, amount, quantity):
    store = journal(tmp_path)
    store.initialize_experiment("hyundai-test", mode="paper", envelope=limits())
    assert fill(store, key="unexpected", side=side, amount=amount, quantity=quantity) == "FROZEN"
    snap = book_snapshot(store)
    assert snap.unresolved is True
    with pytest.raises(ValueError): authorize_order(buy(), limits(), snap, now=NOW, mode="paper")


def test_paper_fills_do_not_touch_live_cash_and_storage_is_private(tmp_path):
    store = journal(tmp_path)
    for mode in ("paper", "live"):
        store.initialize_experiment("hyundai-test", mode=mode, envelope=limits())
    fill(store, key="buy-1", side="BUY", amount="380000")
    assert book_snapshot(store, mode="live").available_cash == Decimal("400000")
    assert stat.S_IMODE(store.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(store.path.parent.stat().st_mode) == 0o700


@pytest.mark.parametrize("changes", [dict(initial_budget=Decimal("NaN")), dict(max_quantity=Decimal("0.5")),
                                    dict(protected_quantity=Decimal("-1")), dict(account_scope="")])
def test_invalid_experiment_cannot_be_initialized(tmp_path, changes):
    with pytest.raises(ValueError):
        journal(tmp_path).initialize_experiment("hyundai-test", mode="paper", envelope=replace(limits(), **changes))
