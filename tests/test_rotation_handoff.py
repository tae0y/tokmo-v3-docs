"""v0.4 handoff: the runner consumes the analysis file, never analyses, never substitutes."""

import json
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
from test_rotation_daily import group, order, setup, verdict
from test_rotation_policy import policy

from tokmo.programs.order_execution import OrderExecutionClient
from tokmo.programs.rotation_daily import run_from_decision_file
from tokmo.values.rotation_decision_file import (
    DecisionFileError,
    parse_decision_file,
    today_approval_matches,
)

NOW = datetime(2026, 9, 16, 1, 5, 20, tzinfo=UTC)  # 10:05:20 KST
DEADLINE = datetime(2026, 9, 16, 1, 40, tzinfo=UTC)  # 10:40:00 KST
TODAY = date(2026, 9, 16)


def document(status="COMPLETE", action="ENTER", buy="380000", sell="400000", **changes):
    decision = {
        "symbol": "005380",
        "action": action,
        "buy_price": buy if action == "ENTER" else None,
        "sell_price": sell if action != "HOLD" else None,
        "expires_on": TODAY.isoformat() if action == "ENTER" else None,
        "quantity": "1",
        "total_amount": (buy if action == "ENTER" else sell)
        if action != "HOLD"
        else None,
        "reason": "분석 근거",
        "evidence_ids": ["quant:report:1"],
    }
    data = {
        "schema": "rotation-decision-file-v1",
        "status": status,
        "experiment_id": "test-005380",
        "account_scope": "personal-test",
        "trade_date": TODAY.isoformat(),
        "analysis_id": "astra-20260916-1",
        "producer": "astra",
        "started_at": (NOW - timedelta(minutes=3)).isoformat(),
        "produced_at": (NOW - timedelta(minutes=1)).isoformat(),
        "data_as_of": (NOW - timedelta(minutes=2)).isoformat(),
        "valid_until": DEADLINE.isoformat(),
        "decision": decision if status in ("COMPLETE", "WAITING_APPROVAL") else None,
        "band_proposal": None,
        "analysis_summary": "요약",
        "failure_reason": None,
    }
    data.update(changes)
    return data


class Clock:
    """Sleeping advances the same clock the runner samples."""

    def __init__(self, at):
        self.at = at
        self.slept = 0

    def __call__(self):
        self.at += timedelta(seconds=1)
        return self.at

    def sleep(self, seconds):
        self.slept += seconds
        self.at += timedelta(seconds=seconds)


def parse(data, **kw):
    return parse_decision_file(
        json.dumps(data, ensure_ascii=False),
        policy=policy(),
        now=NOW,
        trade_date=TODAY,
        **kw,
    )


def test_valid_complete_file_parses_with_band_flag():
    parsed = parse(document())
    assert (
        parsed.status,
        parsed.producer,
        parsed.in_band,
        parsed.decision["buy_price"],
    ) == ("COMPLETE", "astra", True, "380000")
    assert (
        parse(document(buy="345000", sell="470000")).in_band is False
    )  # buy under the band floor
    assert (
        parse(document(buy="380000", sell="385000")).in_band is False
    )  # +1.3% under the 2% minimum
    assert (
        parse(document(action="EXIT", sell="405000")).in_band is None
    )  # no fill price known yet
    assert (
        parse(
            document(action="EXIT", sell="405000"), cost_basis=Decimal(385000)
        ).in_band
        is True
    )
    assert (
        parse(
            document(action="EXIT", sell="390000"), cost_basis=Decimal(385000)
        ).in_band
        is False
    )  # floor 393,000
    assert parse(document(action="HOLD")).in_band is True
    assert (
        parse(document(status="RUNNING")).decision is None
        and parse(document(status="FAILED", failure_reason="x")).status == "FAILED"
    )


@pytest.mark.parametrize(
    "mutate, code",
    [
        (lambda d: d.update(schema="rotation-decision-file-v2"), "schema"),
        (lambda d: d.update(status="DONE"), "status"),
        (lambda d: d.update(experiment_id="other"), "identity"),
        (lambda d: d.update(trade_date="2026-09-15"), "trade_date"),
        (
            lambda d: d.update(produced_at=(NOW + timedelta(minutes=1)).isoformat()),
            "time_order",
        ),
        (
            lambda d: d.update(
                produced_at=(NOW - timedelta(hours=3)).isoformat(),
                started_at=(NOW - timedelta(hours=4)).isoformat(),
                data_as_of=(NOW - timedelta(hours=4)).isoformat(),
            ),
            "stale",
        ),
        (lambda d: d.update(produced_at="2026-09-16T10:04:00"), "time_naive"),
        (lambda d: d["decision"].update(buy_price="380123"), "price_off_tick"),
        (lambda d: d["decision"].update(total_amount="380001"), "total_amount"),
        (lambda d: d["decision"].update(quantity="2"), "decision_quantity"),
        (lambda d: d["decision"].update(evidence_ids=[]), "decision_evidence"),
        (lambda d: d["decision"].update(expires_on="2026-09-17"), "enter_fields"),
        (
            lambda d: d["decision"].update(sell_price="370000", total_amount="380000"),
            "enter_spread",
        ),
        (lambda d: d["decision"].pop("reason"), "decision_shape"),
        (lambda d: d.update(extra="x"), "document_shape"),
        (
            lambda d: d.update(
                band_proposal={"protected_quantity": "8", "reason": "x"}
            ),
            "band_proposal_fields",
        ),
    ],
)
def test_invalid_files_fail_with_a_stable_code(mutate, code):
    data = document()
    mutate(data)
    with pytest.raises(DecisionFileError) as error:
        parse(data)
    assert error.value.code == code


def test_duplicate_keys_and_nan_are_rejected():
    with pytest.raises(DecisionFileError):
        parse_decision_file(
            '{"schema":"a","schema":"b"}', policy=policy(), now=NOW, trade_date=TODAY
        )
    raw = json.dumps(document()).replace('"380000"', "NaN", 1)
    with pytest.raises(DecisionFileError):
        parse_decision_file(raw, policy=policy(), now=NOW, trade_date=TODAY)


def approval(decision, **changes):
    record = {
        "scope": "today",
        "status": "APPROVED",
        "experiment_id": "test-005380",
        "trade_date": TODAY.isoformat(),
        "proposal_id": "p1",
        "revision": 1,
        "approved_at": NOW.isoformat(),
        "decision": {
            k: decision[k] for k in ("action", "buy_price", "sell_price", "quantity")
        },
    }
    record.update(changes)
    return record


def test_today_approval_must_match_the_exact_decision_and_day():
    decision = document(buy="345000", sell="470000")["decision"]
    assert today_approval_matches(
        approval(decision), decision=decision, policy=policy(), trade_date=TODAY
    )
    assert not today_approval_matches(
        approval(decision, scope="persistent"),
        decision=decision,
        policy=policy(),
        trade_date=TODAY,
    )
    assert not today_approval_matches(
        approval(decision, status="PENDING"),
        decision=decision,
        policy=policy(),
        trade_date=TODAY,
    )
    assert not today_approval_matches(
        approval(decision, trade_date="2026-09-15"),
        decision=decision,
        policy=policy(),
        trade_date=TODAY,
    )
    other = dict(decision, sell_price="480000")
    assert not today_approval_matches(
        approval(decision), decision=other, policy=policy(), trade_date=TODAY
    )


def harness(tmp_path, *, files, approvals=(), at=NOW, holdings=7, quote="390000"):
    broker, journal = setup(tmp_path)
    broker.holdings, broker.quote = Decimal(holdings), quote
    broker.session = verdict(trade_date=TODAY.isoformat())
    clock = Clock(at)
    client = OrderExecutionClient(
        account_scope="personal-test",
        mode="live",
        journal=journal,
        token_provider=lambda: "token",
        account_ref_provider=lambda s: "123",
        transport=broker.transport,
        clock=clock,
    )
    queue = list(files)

    def reader():
        # Each poll pops the next scripted file state; the last state persists.
        raw = queue.pop(0) if len(queue) > 1 else queue[0]
        return None if raw is None else json.dumps(raw, ensure_ascii=False)

    outcome = run_from_decision_file(
        policy=policy(),
        journal=journal,
        reads=broker.reads(),
        decision_reader=reader,
        approvals_reader=lambda: list(approvals),
        clock=clock,
        sleeper=clock.sleep,
        client=client,
        deadline=DEADLINE,
    )
    return outcome, broker, journal, clock


def test_complete_in_band_file_is_executed_once_inside_the_window(tmp_path):
    outcome, broker, journal, clock = harness(
        tmp_path, files=[None, document(status="RUNNING"), document()]
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["execution_status"] == "ACCEPTED"
    )
    assert (
        outcome.details["producer"] == "astra"
        and outcome.details["command"]["payload"]["kind"] == "OTO"
    )
    assert (
        len(broker.wire_calls) == 1
        and journal.decision(outcome.details["decision_key"])["status"] == "DECIDED"
    )
    # Same day again: nothing, not even a read.
    outcome2, broker2, journal2, _ = harness(tmp_path / "again", files=[document()])
    assert outcome2.status == "SUBMITTED"
    again = run_from_decision_file(
        policy=policy(),
        journal=journal2,
        reads=broker2.reads(),
        decision_reader=lambda: pytest.fail("read"),
        approvals_reader=list,
        clock=Clock(NOW + timedelta(minutes=5)),
        sleeper=lambda s: None,
        client=None,
        deadline=DEADLINE,
    )
    assert again.status == "EXISTING"


def test_running_until_the_deadline_expires_the_day_without_any_substitute(tmp_path):
    outcome, broker, journal, clock = harness(
        tmp_path, files=[document(status="RUNNING")]
    )
    assert (outcome.status, outcome.reason) == (
        "EXPIRED",
        "analysis_timeout",
    ) and broker.wire_calls == []
    assert (
        journal.decision(outcome.details["decision_key"])["status"] == "EXPIRED"
        and clock.slept >= 30 * 60
    )
    outcome, broker, _, _ = harness(tmp_path / "absent", files=[None])
    assert (outcome.status, outcome.reason) == (
        "EXPIRED",
        "decision_file:absent",
    ) and broker.wire_calls == []


def test_failed_or_invalid_files_end_the_day_at_once(tmp_path):
    outcome, broker, _, clock = harness(
        tmp_path,
        files=[
            document(
                status="FAILED", failure_reason="자료 부족", failure_code="data_missing"
            )
        ],
    )
    assert (outcome.status, outcome.reason) == (
        "FAILED",
        "analysis_failed:data_missing",
    ) and clock.slept == 0
    bad = document()
    bad["decision"]["buy_price"] = "380123"
    outcome, broker, _, _ = harness(tmp_path / "bad", files=[bad])
    assert (
        outcome.status == "FAILED"
        and outcome.reason == "decision_file:invalid:price_off_tick"
        and broker.wire_calls == []
    )


def test_out_of_band_needs_the_runners_own_same_day_approval(tmp_path):
    wide = document(buy="345000", sell="470000")
    outcome, broker, _, _ = harness(tmp_path, files=[wide])
    assert (outcome.status, outcome.reason) == (
        "FAILED",
        "out_of_band_without_today_approval",
    ) and broker.wire_calls == []
    waiting = document(
        status="WAITING_APPROVAL",
        buy="345000",
        sell="470000",
        band_proposal={"buy_floor": "340000", "reason": "34만 지지"},
    )
    outcome, broker, _, clock = harness(tmp_path / "wait", files=[waiting])
    assert (outcome.status, outcome.reason) == (
        "EXPIRED",
        "approval_timeout",
    ) and broker.wire_calls == []
    outcome, broker, _, _ = harness(
        tmp_path / "approved",
        files=[waiting],
        approvals=[approval(waiting["decision"])],
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["approval"]["proposal_id"] == "p1"
    )
    assert outcome.details["command"]["payload"]["first"]["order_price"] == "345000"


def test_inventory_mismatch_and_hold_never_reach_the_broker(tmp_path):
    outcome, broker, _, _ = harness(
        tmp_path, files=[document(action="EXIT", sell="405000")]
    )
    assert (
        outcome.status == "FAILED"
        and outcome.reason.startswith("inventory_mismatch")
        and broker.wire_calls == []
    )
    outcome, broker, journal, _ = harness(
        tmp_path / "hold", files=[document(action="HOLD")]
    )
    assert (outcome.status, outcome.reason) == (
        "HOLD",
        "분석 근거",
    ) and broker.wire_calls == []
    assert journal.decision(outcome.details["decision_key"])["status"] == "HOLD"


def test_entry_at_or_below_the_last_price_is_a_plain_limit_buy(tmp_path):
    outcome, broker, _, _ = harness(tmp_path, files=[document()], quote="375000")
    payload = outcome.details["command"]["payload"]
    assert outcome.status == "SUBMITTED" and (
        payload["side"],
        payload["price"],
        "kind" in payload,
    ) == ("BUY", "380000", False)
    assert "entry:limit_buy_trigger_already_met" in outcome.details["notes"]


def test_presend_settlement_books_yesterdays_fill_then_exits(tmp_path):
    broker, journal = setup(tmp_path)
    from test_rotation_daily import run

    run(broker, journal)  # yesterday's OTO
    broker.groups["grp-1"] = group("EXPIRED", first_child="child-buy")
    broker.orders["child-buy"] = order(
        "child-buy", "BUY", "FILLED", filled="1", price="385000"
    )
    broker.holdings, broker.session = Decimal(8), verdict(trade_date=TODAY.isoformat())
    clock = Clock(NOW)
    client = OrderExecutionClient(
        account_scope="personal-test",
        mode="live",
        journal=journal,
        token_provider=lambda: "token",
        account_ref_provider=lambda s: "123",
        transport=broker.transport,
        clock=clock,
    )
    outcome = run_from_decision_file(
        policy=policy(),
        journal=journal,
        reads=broker.reads(),
        decision_reader=lambda: json.dumps(document(action="EXIT", sell="405000")),
        approvals_reader=list,
        clock=clock,
        sleeper=clock.sleep,
        client=client,
        deadline=DEADLINE,
    )
    assert (
        outcome.status == "SUBMITTED"
        and outcome.details["command"]["payload"]["side"] == "SELL"
    )
    assert (
        "presend:fill:BUY:APPLIED" in outcome.details["notes"]
        and outcome.details["holding"] is True
    )


def two_share(**changes):
    """A policy that accumulates two shares before it exits."""
    return policy(quantity=Decimal(2), initial_budget=Decimal(800000), **changes)


def shares(count, total, **changes):
    """A decision file naming an explicit share count inside the decision."""
    data = document(**changes)
    data["decision"].update(quantity=count, total_amount=total)
    return data


def seeded(tmp_path, *, lots, pol):
    """A journal whose experiment already holds ``lots`` at the given prices."""
    broker, journal = setup(tmp_path)
    journal.initialize_experiment(
        pol.experiment_id, mode="live", envelope=pol.envelope()
    )
    for index, price in enumerate(lots):
        journal.apply_experiment_fill(
            pol.experiment_id,
            mode="live",
            fill_id=f"manual-seed{index}",
            symbol="005380",
            side="BUY",
            quantity=Decimal(1),
            amount=Decimal(price),
            costs=Decimal(0),
            observed_at=NOW - timedelta(days=len(lots) - index),
        )
    return broker, journal


def drive(broker, journal, pol, raw, *, holdings, at=NOW, quote="390000"):
    broker.holdings, broker.quote = Decimal(holdings), quote
    broker.session = verdict(trade_date=TODAY.isoformat())
    clock = Clock(at)
    client = OrderExecutionClient(
        account_scope="personal-test",
        mode="live",
        journal=journal,
        token_provider=lambda: "token",
        account_ref_provider=lambda s: "123",
        transport=broker.transport,
        clock=clock,
    )
    return run_from_decision_file(
        policy=pol,
        journal=journal,
        reads=broker.reads(),
        decision_reader=lambda: json.dumps(raw),
        approvals_reader=list,
        clock=clock,
        sleeper=clock.sleep,
        client=client,
        deadline=DEADLINE,
    )


def test_a_partly_filled_experiment_tops_up_with_a_plain_buy_and_no_sell_leg(tmp_path):
    pol = two_share()
    broker, journal = seeded(tmp_path, lots=["372500"], pol=pol)
    # One of two shares held: the day buys the remainder, and pairing a sell leg
    # with it would round-trip the new share instead of averaging the lot down.
    outcome = drive(
        broker,
        journal,
        pol,
        shares("1", "360000", buy="360000", sell="374000"),
        holdings=8,
    )
    payload = outcome.details["command"]["payload"]
    assert outcome.status == "SUBMITTED" and (
        payload["side"],
        payload["quantity"],
        "kind" in payload,
    ) == ("BUY", "1", False)
    assert "entry:top_up_limit_buy" in outcome.details["notes"]
    assert (outcome.details["held"], outcome.details["remaining"]) == ("1", "1")


def test_a_full_two_share_lot_exits_as_one_sell_at_the_averaged_floor(tmp_path):
    pol = two_share()
    broker, journal = seeded(tmp_path, lots=["372500", "360000"], pol=pol)
    basis = journal.experiment_lot_cost(pol.experiment_id, mode="live")
    assert basis == Decimal(366250) and pol.sell_floor_for(basis) == Decimal(374000)
    outcome = drive(
        broker,
        journal,
        pol,
        shares("2", "748000", action="EXIT", sell="374000"),
        holdings=9,
    )
    payload = outcome.details["command"]["payload"]
    assert outcome.status == "SUBMITTED" and (payload["side"], payload["quantity"]) == (
        "SELL",
        "2",
    )
    assert outcome.details["sell_floor"] == "374000"


def test_a_file_naming_the_wrong_share_count_never_reaches_the_broker(tmp_path):
    pol = two_share()
    broker, journal = seeded(tmp_path, lots=["372500"], pol=pol)
    # Two shares of capacity but one already held: a two-share buy would breach
    # the position limit, so it is refused before any wire call.
    outcome = drive(
        broker,
        journal,
        pol,
        shares("2", "720000", buy="360000", sell="374000"),
        holdings=8,
    )
    assert (
        outcome.status == "FAILED"
        and outcome.reason == "quantity_mismatch:file=2,expected=1"
    )
    assert broker.wire_calls == []


def test_the_decision_file_accepts_a_partial_count_but_still_checks_the_total(tmp_path):
    pol = two_share()
    parsed = parse_decision_file(
        json.dumps(shares("1", "360000", buy="360000", sell="374000")),
        policy=pol,
        now=NOW,
        trade_date=TODAY,
    )
    assert parsed.decision["quantity"] == "1"
    for bad in (
        shares("1", "720000", buy="360000", sell="374000"),
        shares("3", "1080000", buy="360000", sell="374000"),
        shares("0", "0", buy="360000", sell="374000"),
    ):
        with pytest.raises(DecisionFileError):
            parse_decision_file(json.dumps(bad), policy=pol, now=NOW, trade_date=TODAY)


def test_the_model_fallback_path_refuses_a_multi_share_policy(tmp_path):
    from test_rotation_daily import run

    broker, journal = setup(tmp_path)
    outcome = run(broker, journal, pol=two_share())
    assert (outcome.status, outcome.reason) == (
        "HALT",
        "model_path_supports_one_share_only",
    )
    assert broker.wire_calls == [] and broker.read_calls == []


def test_confirmed_skip_is_nontradable_terminal_even_with_an_owner_approval(tmp_path):
    doc = document(status="SKIPPED", analysis_summary="휴장일 미실행")
    assert parse(doc).decision is None
    outcome, broker, journal, clock = harness(
        tmp_path, files=[doc], approvals=[approval(document()["decision"])]
    )
    assert (outcome.status, outcome.reason) == (
        "SKIP",
        "analysis_skipped:market_holiday",
    )
    assert clock.slept == 0 and broker.wire_calls == []
    again = run_from_decision_file(
        policy=policy(),
        journal=journal,
        reads=broker.reads(),
        decision_reader=lambda: pytest.fail("reread"),
        approvals_reader=lambda: pytest.fail("approval"),
        clock=Clock(NOW),
        sleeper=lambda _: pytest.fail("sleep"),
        client=None,
        deadline=DEADLINE,
    )
    assert again.status == "EXISTING"


@pytest.mark.parametrize(
    "field,value",
    [
        ("decision", document()["decision"]),
        ("band_proposal", {"buy_floor": "350000", "reason": "x"}),
        ("proposal_id", "owner"),
    ],
)
def test_skip_cannot_carry_a_trade_or_approval(field, value):
    with pytest.raises(DecisionFileError, match="skipped_payload"):
        parse(document(status="SKIPPED", **{field: value}))
