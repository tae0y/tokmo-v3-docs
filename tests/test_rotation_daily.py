"""One instrument, one day: yesterday is settled from broker reads before any model call or order."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from test_rotation_policy import answer, policy

from tokmo.programs.order_execution import OrderExecutionClient
from tokmo.programs.rotation_daily import RotationReads, run_daily
from tokmo.records.calendar_lookahead import PulseSessionVerdictV1
from tokmo.records.order_command_journal import CommandConflict, OrderCommandJournal
from tokmo.values.order_commands import OrderTransportDisabled, OrderWireResponse
from tokmo.values.session_gate import Market
from tokmo.values.toss_orders import (
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
    Order,
    OrderExecution,
)

NOW = datetime(
    2026, 9, 11, 1, 0, 20, tzinfo=UTC
)  # 10:00:20 KST, inside the send window
NEXT = NOW + timedelta(days=1)
EVIDENCE = {"quote": {"as_of": NOW.isoformat(), "last_price": "384000"}}


def verdict(
    *,
    open=True,
    reason="open",
    kind="regularMarket",
    trade_date="2026-09-11",
    market=Market.KR,
):
    if not open:
        return PulseSessionVerdictV1(market, False, reason, None, None, None)
    return PulseSessionVerdictV1(market, True, reason, trade_date, kind, None)


def order(
    order_id, side, status, *, filled="0", price="385000", commission="57", tax="0"
):
    execution = OrderExecution(
        filled,
        price if filled != "0" else None,
        str(Decimal(price) * Decimal(filled)) if filled != "0" else None,
        commission if filled != "0" else None,
        tax if filled != "0" else None,
        NOW + timedelta(hours=3) if filled != "0" else None,
        None,
    )
    return Order(
        order_id,
        "005380",
        side,
        "LIMIT",
        "DAY",
        status,
        "1",
        "KRW",
        NOW,
        execution,
        price=price,
    )


def group(status, *, first_child=None, second_child=None):
    first = ConditionalOrderCondition(
        "STOP",
        "COMPLETED" if first_child else "WATCHING",
        order_side="BUY",
        trigger_price="385000",
        order_price="385000",
        triggered_order_id=first_child,
    )
    second = ConditionalOrderCondition(
        "STOP",
        "COMPLETED" if second_child else "HOLDING",
        order_side="SELL",
        trigger_price="402000",
        order_price="402000",
        triggered_order_id=second_child,
    )
    return ConditionalOrderDetailResponse(
        "grp-1",
        "OTO",
        status,
        "005380",
        "KR",
        "1",
        "LIMIT",
        first,
        NOW,
        expire_date=date(2026, 9, 11),
        second=second,
    )


class Broker:
    """Scriptable read side plus a wire that acknowledges every command."""

    def __init__(self):
        self.holdings = Decimal(7)
        self.groups = {}
        self.orders = {}
        self.session = None  # None = open regular session on the run's KST date
        self.quote = "390000"  # above the fixture answer's buy trigger (385000)
        self.cash = Decimal(450000)
        self.wire_calls = []
        self.read_calls = []
        self.disabled = False

    def _detail(self, table, key):
        self.read_calls.append(key)
        return table.get(key)

    def reads(self):
        return RotationReads(
            order_detail=lambda i: self._detail(self.orders, i),
            conditional_detail=lambda i: self._detail(self.groups, i),
            holdings_quantity=lambda s: self.holdings,
            session_verdict=lambda m, now: (
                self.session
                if self.session is not None
                else verdict(
                    trade_date=(now + timedelta(hours=9)).date().isoformat(),
                    market=Market(m),
                )
            ),
            evidence=lambda p: {
                "quote": {"as_of": NOW.isoformat(), "last_price": self.quote}
            },
            cash_available=lambda currency: self.cash,
        )

    def transport(self, request):
        self.wire_calls.append(request.command)
        if self.disabled:
            raise OrderTransportDisabled("order_transport_disabled")
        key = (
            "conditionalOrderId"
            if request.command.operation.value.startswith("CONDITIONAL")
            else "orderId"
        )
        return OrderWireResponse(
            200,
            json.dumps(
                {"result": {key: "grp-1" if key == "conditionalOrderId" else "ord-1"}}
            ).encode(),
        )


class Ticking:
    """A real clock never stands still: every sample is one second later."""

    def __init__(self, at):
        self.at = at

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at


def setup(tmp_path):
    broker = Broker()
    journal = OrderCommandJournal(tmp_path / "private" / "orders.sqlite3")
    return broker, journal


def run(broker, journal, *, model=None, at=NOW, pol=None):
    clock = Ticking(at)
    client = OrderExecutionClient(
        account_scope="personal-test",
        mode="live",
        journal=journal,
        token_provider=lambda: "token",
        account_ref_provider=lambda s: "123",
        transport=broker.transport,
        clock=clock,
    )
    return run_daily(
        policy=pol or policy(),
        journal=journal,
        reads=broker.reads(),
        model_id="fixture-model",
        model_transport=model or (lambda r: answer()),
        clock=clock,
        client=client,
    )


def test_first_day_registers_an_oto_at_the_models_prices_expiring_today(tmp_path):
    broker, journal = setup(tmp_path)
    outcome = run(
        broker, journal, model=lambda r: answer(buy_price="385123", sell_price="402001")
    )
    assert (
        outcome.status,
        outcome.details["execution_status"],
        outcome.details["order_id"],
    ) == ("SUBMITTED", "ACCEPTED", "grp-1")
    payload = outcome.details["command"]["payload"]
    assert (
        payload["kind"],
        payload["first"]["order_price"],
        payload["second"]["order_price"],
        payload["expire_date"],
    ) == ("OTO", "385000", "402500", "2026-09-11")
    row = journal.open_commands(
        mode="live", account_scope="personal-test", symbol="005380"
    )[0]
    assert (row["status"], row["operation"]) == ("ACCEPTED", "CONDITIONAL_CREATE")
    assert "token" not in journal.path.read_bytes().decode("latin1")


def test_model_failure_still_registers_the_default_band(tmp_path):
    broker, journal = setup(tmp_path)

    def broken(request):
        raise TimeoutError()

    outcome = run(broker, journal, model=broken)
    assert (
        outcome.status == "SUBMITTED"
        and "model:model_unusable_default_band" in outcome.details["notes"]
    )
    assert (
        outcome.details["decision"]["buy_price"],
        outcome.details["decision"]["sell_price"],
    ) == ("380000", "391500")


def test_unresolved_reservation_halts_before_any_model_call_or_wire(tmp_path):
    broker, journal = setup(tmp_path)
    journal.claim(
        request_key="stuck",
        mode="live",
        account_scope="personal-test",
        symbol="005380",
        operation="CONDITIONAL_CREATE",
        target_id=None,
        client_key="k1",
        digest="d",
        command_json="{}",
        plan_version="v",
        now=(NOW - timedelta(days=1)).isoformat(),
    )
    outcome = run(broker, journal, model=lambda r: pytest.fail("model called"))
    assert (outcome.status, outcome.reason) == (
        "HALT",
        "unresolved_command:stuck",
    ) and broker.wire_calls == []


def test_watching_group_means_no_new_order_and_no_model_call(tmp_path):
    broker, journal = setup(tmp_path)
    run(broker, journal)
    broker.groups["grp-1"] = group("WATCHING")
    outcome = run(
        broker,
        journal,
        model=lambda r: pytest.fail("model called"),
        at=NOW + timedelta(minutes=30),
    )
    assert (outcome.status, outcome.reason) == ("SKIP", "standing_order_open") and len(
        broker.wire_calls
    ) == 1


def test_expired_group_with_filled_buy_books_the_lot_and_exits_next_day(tmp_path):
    broker, journal = setup(tmp_path)
    run(broker, journal)
    broker.groups["grp-1"] = group("EXPIRED", first_child="child-buy")
    broker.orders["child-buy"] = order(
        "child-buy", "BUY", "FILLED", filled="1", price="385000"
    )
    broker.holdings = Decimal(8)
    outcome = run(
        broker,
        journal,
        model=lambda r: answer(
            action="EXIT", buy_price=None, sell_price="405000", expires_on=None
        ),
        at=NEXT,
    )
    book = journal.experiment_book("test-005380", mode="live")
    assert (book["quantity"], book["cash"], book["frozen"]) == (
        Decimal(1),
        Decimal(450000 - 385000 - 57),
        False,
    )
    assert (
        journal.get("rotation-" + outcome.details["decision_key"])["operation"]
        == "CREATE"
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["command"]["payload"]["side"] == "SELL"
    )
    assert outcome.details["command"]["payload"]["price"] == "405000"
    assert "fill:BUY:APPLIED" in outcome.details["notes"]
    assert all(
        r["operation"] == "CREATE"
        for r in journal.open_commands(
            mode="live", account_scope="personal-test", symbol="005380"
        )
    )


def test_completed_round_trip_books_both_fills_and_re_enters(tmp_path):
    broker, journal = setup(tmp_path)
    run(broker, journal)
    broker.groups["grp-1"] = group(
        "COMPLETED", first_child="child-buy", second_child="child-sell"
    )
    broker.orders["child-buy"] = order(
        "child-buy", "BUY", "FILLED", filled="1", price="385000"
    )
    broker.orders["child-sell"] = order(
        "child-sell",
        "SELL",
        "FILLED",
        filled="1",
        price="402000",
        commission="60",
        tax="603",
    )
    outcome = run(broker, journal, at=NEXT)
    book = journal.experiment_book("test-005380", mode="live")
    assert (book["quantity"], book["cash"]) == (
        Decimal(0),
        Decimal(450000 - 385057 + 402000 - 663),
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["command"]["payload"]["kind"] == "OTO"
    )


def test_pending_child_order_blocks_the_day_without_reconciling(tmp_path):
    broker, journal = setup(tmp_path)
    run(broker, journal)
    broker.groups["grp-1"] = group("ORDERED", first_child="child-buy")
    broker.orders["child-buy"] = order("child-buy", "BUY", "PENDING")
    outcome = run(broker, journal, model=lambda r: pytest.fail("model called"), at=NEXT)
    assert (outcome.status, outcome.reason) == ("SKIP", "standing_order_open")
    assert (
        journal.open_commands(
            mode="live", account_scope="personal-test", symbol="005380"
        )[0]["lifecycle"]
        == "OPEN"
    )


def test_manual_trade_changing_protected_shares_blocks_without_wire(tmp_path):
    broker, journal = setup(tmp_path)
    broker.holdings = Decimal(10)
    outcome = run(broker, journal)
    assert outcome.status == "BLOCKED" and broker.wire_calls == []


def test_session_gate_runs_before_any_read_token_or_model(tmp_path):
    broker, journal = setup(tmp_path)
    journal.claim(
        request_key="stuck",
        mode="live",
        account_scope="personal-test",
        symbol="005380",
        operation="CONDITIONAL_CREATE",
        target_id=None,
        client_key="k1",
        digest="d",
        command_json="{}",
        plan_version="v",
        now=(NOW - timedelta(days=1)).isoformat(),
    )
    forbidden = lambda r: pytest.fail("model called")
    broker.session = verdict(open=False, reason="closed_between_sessions")
    assert (
        run(broker, journal, model=forbidden).reason
        == "market_closed:closed_between_sessions"
    )
    broker.session = verdict(open=False, reason="lookahead_exhausted")
    assert (
        run(broker, journal, model=forbidden).reason
        == "market_closed:lookahead_exhausted"
    )
    broker.session = verdict(kind="afterMarket")
    assert (
        run(broker, journal, model=forbidden).reason
        == "outside_regular_session:afterMarket"
    )
    outcome = run_daily(
        policy=policy(),
        journal=journal,
        model_id="fixture-model",
        model_transport=forbidden,
        client=None,
        clock=Ticking(NOW),
        reads=RotationReads(
            order_detail=lambda i: pytest.fail("read"),
            conditional_detail=lambda i: pytest.fail("read"),
            holdings_quantity=lambda s: pytest.fail("read"),
            session_verdict=lambda m, now: None,
            evidence=lambda p: pytest.fail("read"),
        ),
    )
    assert (outcome.status, outcome.reason) == ("SKIP", "calendar_unknown")
    # The stuck reservation was never even looked at while the market was closed.
    assert broker.read_calls == [] and broker.wire_calls == []


def test_entry_below_the_trigger_becomes_a_plain_limit_buy_not_an_oto(tmp_path):
    broker, journal = setup(tmp_path)
    broker.quote = "375000"
    outcome = run(
        broker, journal, model=lambda r: answer(buy_price="380000", sell_price="400000")
    )
    payload = outcome.details["command"]["payload"]
    assert outcome.status == "SUBMITTED" and (
        payload["side"],
        payload["price"],
        payload.get("kind"),
    ) == ("BUY", "380000", None)
    assert "entry:limit_buy_trigger_already_met" in outcome.details["notes"]
    row = journal.open_commands(
        mode="live", account_scope="personal-test", symbol="005380"
    )[0]
    assert row["operation"] == "CREATE"
    # Next day the lot is booked from the filled buy and the exit is a plain limit sell.
    broker.orders["ord-1"] = order("ord-1", "BUY", "FILLED", filled="1", price="375000")
    broker.holdings = Decimal(8)
    outcome = run(
        broker,
        journal,
        model=lambda r: answer(
            action="EXIT", buy_price=None, sell_price="400000", expires_on=None
        ),
        at=NEXT,
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["command"]["payload"]["side"] == "SELL"
    )
    assert journal.experiment_book("test-005380", mode="live")["quantity"] == Decimal(1)


def test_a_buy_the_account_cannot_fund_becomes_a_recorded_hold(tmp_path):
    broker, journal = setup(tmp_path)
    broker.cash = Decimal(50)
    outcome = run(broker, journal)
    assert (
        outcome.status == "HOLD"
        and "예수금 부족" in outcome.reason
        and broker.wire_calls == []
    )
    assert (
        outcome.details["decision"]["action"] == "HOLD"
        and "cash:insufficient" in outcome.details["notes"]
    )
    assert outcome.details["cash_available"] == "50"
    # Unknown cash is noted, not blocking: the broker rejects an unfunded child order harmlessly.
    broker.cash = None
    outcome = run(broker, journal, at=NEXT)
    assert outcome.status == "SUBMITTED" and "cash:unknown" in outcome.details["notes"]
    # Selling needs no cash.
    broker.cash = Decimal(50)
    journal.apply_experiment_fill(
        "test-005380",
        mode="live",
        fill_id="f",
        symbol="005380",
        side="BUY",
        quantity=Decimal(1),
        amount=Decimal(385000),
        costs=Decimal(57),
        observed_at=NOW,
    )
    broker.holdings = Decimal(8)
    broker.groups["grp-1"] = group("EXPIRED")
    outcome = run(
        broker,
        journal,
        model=lambda r: answer(
            action="EXIT", buy_price=None, sell_price="400000", expires_on=None
        ),
        at=NEXT + timedelta(days=1),
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["command"]["payload"]["side"] == "SELL"
    )


def test_settlement_books_fills_without_deciding_and_tolerates_a_vanished_group(
    tmp_path,
):
    from tokmo.programs.rotation_daily import settle_open_rows

    broker, journal = setup(tmp_path)
    run(broker, journal)  # day 1: OTO accepted
    broker.groups["grp-1"] = group("EXPIRED", first_child="child-buy")
    broker.orders["child-buy"] = order(
        "child-buy", "BUY", "FILLED", filled="1", price="385000"
    )
    reads = broker.reads()
    result = settle_open_rows(policy=policy(), journal=journal, reads=reads, now=NEXT)
    assert (result["status"], result["open_remaining"], result["book"]["quantity"]) == (
        "OK",
        0,
        "1",
    )
    assert (
        "fill:BUY:APPLIED" in result["notes"] and len(broker.wire_calls) == 1
    )  # settlement never sends
    # A group the provider has dropped (detail unreadable, absent from both lists) closes as CANCELED.
    from dataclasses import replace as dc_replace

    broker2, journal2 = setup(tmp_path / "second")
    run(broker2, journal2)
    reads2 = dc_replace(
        broker2.reads(), conditional_lookup=lambda symbol, order_id: "GONE"
    )
    result = settle_open_rows(policy=policy(), journal=journal2, reads=reads2, now=NEXT)
    assert result["status"] == "OK" and any(
        n.startswith("group_gone:") for n in result["notes"]
    )
    assert (
        journal2.open_commands(
            mode="live", account_scope="personal-test", symbol="005380"
        )
        == []
    )
    # Unreadable lists keep the halt.
    broker3, journal3 = setup(tmp_path / "third")
    run(broker3, journal3)
    assert (
        settle_open_rows(
            policy=policy(), journal=journal3, reads=broker3.reads(), now=NEXT
        )["status"]
        == "HALT"
    )


def test_settlement_is_due_only_outside_and_after_the_regular_session(tmp_path):
    from dataclasses import replace as dc_replace

    from tokmo.programs.rotation_daily import settlement_due

    broker, _ = setup(tmp_path)
    margin = timedelta(minutes=35)
    reads = dc_replace(
        broker.reads(), regular_session_state=lambda m, now: (True, None)
    )
    assert settlement_due(market="KR", now=NOW, reads=reads, margin=margin) == (
        False,
        "regular_session_in_progress",
    )
    reads = dc_replace(
        broker.reads(),
        regular_session_state=lambda m, now: (False, NOW - timedelta(minutes=10)),
    )
    assert settlement_due(market="KR", now=NOW, reads=reads, margin=margin) == (
        False,
        "close_margin_not_elapsed",
    )
    reads = dc_replace(
        broker.reads(),
        regular_session_state=lambda m, now: (False, NOW - timedelta(hours=17)),
    )
    assert settlement_due(market="KR", now=NOW, reads=reads, margin=margin) == (
        True,
        "due",
    )
    reads = dc_replace(
        broker.reads(), regular_session_state=lambda m, now: (False, None)
    )
    assert settlement_due(market="KR", now=NOW, reads=reads, margin=margin) == (
        True,
        "due",
    )


def test_read_failure_after_the_gate_halts_before_the_model(tmp_path):
    broker, journal = setup(tmp_path)
    run(broker, journal)
    outcome = run(broker, journal, model=lambda r: pytest.fail("model called"), at=NEXT)
    assert (outcome.status, outcome.reason) == ("HALT", "conditional_read_failed")


def test_us_instrument_uses_the_providers_trade_date_not_the_seoul_date(tmp_path):
    broker, journal = setup(tmp_path)
    broker.holdings = Decimal(3)
    us = policy(
        market="US",
        symbol="AVAV",
        protected_quantity=Decimal(3),
        initial_budget=Decimal(20),
        fee_buffer=Decimal("0.1"),
        default_buy=Decimal("12.50"),
        buy_floor=Decimal(11),
        buy_ceiling=Decimal("12.9"),
    )
    # 00:42 KST on 09-12 is still the 09-11 New York regular session.
    at = datetime(2026, 9, 11, 15, 42, 20, tzinfo=UTC)
    broker.session = verdict(trade_date="2026-09-11", market=Market.US)
    outcome = run(
        broker,
        journal,
        at=at,
        pol=us,
        model=lambda r: answer(symbol="AVAV", buy_price="12.555", sell_price="13.301"),
    )
    assert outcome.status == "SUBMITTED" and outcome.details["day"] == "2026-09-11"
    payload = outcome.details["command"]["payload"]
    assert (
        payload["first"]["order_price"],
        payload["second"]["order_price"],
        payload["expire_date"],
    ) == ("12.55", "13.31", "2026-09-11")


def test_outside_the_pulse_safe_window_nothing_starts(tmp_path):
    broker, journal = setup(tmp_path)
    outcome = run(
        broker,
        journal,
        model=lambda r: pytest.fail("model called"),
        at=NOW.replace(second=2),
    )
    assert (outcome.status, outcome.reason) == (
        "HALT",
        "outside_send_window",
    ) and broker.wire_calls == []


def test_dry_run_settles_terminal_and_same_day_rerun_is_idempotent(tmp_path):
    broker, journal = setup(tmp_path)
    broker.disabled = True
    first = run(broker, journal)
    assert (
        first.status,
        first.details["execution_status"],
        first.details["execution_code"],
    ) == ("SUBMITTED", "NOT_SENT", "transport_disabled")
    assert (
        journal.open_commands(
            mode="live", account_scope="personal-test", symbol="005380"
        )
        == []
    )
    again = run(
        broker,
        journal,
        model=lambda r: pytest.fail("model called twice"),
        at=NOW + timedelta(minutes=5),
    )
    assert again.status == "EXISTING" and len(broker.wire_calls) == 1


def test_expired_experiment_never_calls_anything(tmp_path):
    broker, journal = setup(tmp_path)
    outcome = run(
        broker,
        journal,
        model=lambda r: pytest.fail("model called"),
        at=NOW + timedelta(days=200),
    )
    assert (outcome.status, outcome.reason) == ("SKIP", "experiment_expired")


def external(fill_id, side, quantity="1", price="382500", at=None):
    return {
        "fill_id": fill_id,
        "side": side,
        "quantity": Decimal(quantity),
        "price": Decimal(price),
        "amount": Decimal(price) * Decimal(quantity),
        "currency": "KRW",
        "filled_at": at or (NOW + timedelta(hours=1)),
        "state": "ANSWERED",
    }


def classified(tmp_path, fills, *, holdings, pol=None, seed=True):
    """A journal that has already traded once, plus a scripted broker fill feed."""
    from tokmo.programs.rotation_daily import classify_external_fills

    broker, journal = setup(tmp_path)
    pol = pol or policy()
    journal.initialize_experiment(
        pol.experiment_id, mode="live", envelope=pol.envelope()
    )
    if seed:
        # The experiment's own first lot: this is what makes account history before
        # it irrelevant, and it is the fill the runner must not re-attribute.
        journal.apply_experiment_fill(
            pol.experiment_id,
            mode="live",
            fill_id="manual-own",
            symbol="005380",
            side="BUY",
            quantity=Decimal(1),
            amount=Decimal(372500),
            costs=Decimal(0),
            observed_at=NOW,
        )
    broker.holdings = Decimal(holdings)
    reads = replace_reads(broker, fills)
    return (
        journal,
        pol,
        classify_external_fills(
            policy=pol, journal=journal, reads=reads, now=NOW + timedelta(days=1)
        ),
    )


def replace_reads(broker, fills):
    from dataclasses import replace as _replace

    return _replace(broker.reads(), external_fills=lambda symbol: fills)


def test_an_undeclared_fill_becomes_a_protected_trade_and_moves_the_rail(tmp_path):
    journal, pol, result = classified(tmp_path, [external("app-1", "BUY")], holdings=9)
    assert (
        result["status"],
        result["protected_before"],
        result["protected_after"],
    ) == ("APPLIED", "7", "8")
    assert [f["fill_id"] for f in result["classified"]] == ["app-1"]
    # The journal now only accepts the moved rail, and the experiment's own lot is untouched.
    journal.initialize_experiment(
        pol.experiment_id,
        mode="live",
        envelope=replace(pol, protected_quantity=Decimal(8)).envelope(),
    )
    with pytest.raises(CommandConflict):
        journal.initialize_experiment(
            pol.experiment_id, mode="live", envelope=pol.envelope()
        )
    assert journal.experiment_book(pol.experiment_id, mode="live")[
        "quantity"
    ] == Decimal(1)
    assert (
        journal.protected_fill(pol.experiment_id, mode="live", fill_id="app-1")["side"]
        == "BUY"
    )


def test_the_same_fill_is_never_counted_twice(tmp_path):
    from tokmo.programs.rotation_daily import classify_external_fills

    journal, pol, first = classified(tmp_path, [external("app-1", "BUY")], holdings=9)
    assert first["status"] == "APPLIED"
    broker, _ = setup(tmp_path / "second")
    broker.holdings = Decimal(9)
    again = classify_external_fills(
        policy=replace(pol, protected_quantity=Decimal(8)),
        journal=journal,
        reads=replace_reads(broker, [external("app-1", "BUY")]),
        now=NOW + timedelta(days=2),
    )
    assert (again["status"], again["reason"]) == ("NONE", "no_unclaimed_fills")


def test_a_declared_rotation_fill_and_the_runners_own_order_are_left_alone(tmp_path):
    # "manual-own" was declared through record-fill; it must stay the experiment's.
    _, _, result = classified(
        tmp_path, [external("own", "BUY", price="372500", at=NOW)], holdings=8
    )
    assert (result["status"], result["classified"]) == ("NONE", [])


def test_a_count_that_disagrees_with_the_account_halts_without_writing(tmp_path):
    journal, pol, result = classified(tmp_path, [external("app-1", "SELL")], holdings=8)
    assert (
        result["status"] == "HALT"
        and result["reason"] == "holdings_disagree_with_classification"
    )
    assert (
        journal.protected_fill(pol.experiment_id, mode="live", fill_id="app-1") is None
    )


def test_history_older_than_the_experiment_is_not_attributed(tmp_path):
    _, _, result = classified(
        tmp_path, [external("ancient", "SELL", at=NOW - timedelta(days=30))], holdings=8
    )
    assert (result["status"], result["classified"]) == ("NONE", [])


def test_a_sell_narrows_the_rail_and_an_unreadable_feed_is_not_an_error(tmp_path):
    _, _, sold = classified(tmp_path, [external("app-2", "SELL")], holdings=7)
    assert (sold["status"], sold["protected_after"]) == ("APPLIED", "6")
    from dataclasses import replace as _replace

    from tokmo.programs.rotation_daily import classify_external_fills

    broker, journal = setup(tmp_path / "broken")
    pol = policy()
    journal.initialize_experiment(
        pol.experiment_id, mode="live", envelope=pol.envelope()
    )

    def boom(symbol):
        raise OSError("store gone")

    quiet = classify_external_fills(
        policy=pol,
        journal=journal,
        reads=_replace(broker.reads(), external_fills=boom),
        now=NOW,
    )
    assert (quiet["status"], quiet["reason"]) == ("SKIP", "fill_feed_unreadable")
