import runpy
from datetime import datetime, timedelta
from decimal import Decimal as D
from pathlib import Path

import pytest
from test_quant_decisions import response, sample_input

from tokmo.programs.quant_decision_analysis import build_report, make_packet
from tokmo.records.quant_decision_reports import (
    DecisionDeliveryQueue,
    DecisionReportStore,
    meaningful_changes,
)
from tokmo.values.quant_decisions import parse_snapshot

replay_module = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "tools/replay_quant_decisions.py")
)
replay = replay_module["replay"]


def case_fixture():
    data = sample_input()
    data["instruments"] = data["instruments"][:1]
    data["cash"] = data["cash"][:1]
    data["fx"] = None
    # Round synthetic KR quote to actual whole-won presentation.
    data["instruments"][0]["price"] = "11490"
    s = parse_snapshot(data)
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    option = next(
        o for o in p["options"] if o["action"] == "SELL" and D(o["quantity"]) == 25
    )
    identity = "personal-a:KR:000001"
    later = (s.as_of + timedelta(days=1)).isoformat()
    steps = [
        {
            "at": s.as_of.isoformat(),
            "responses": {identity: response(p, "TRIM", option)},
            "expected": {"KR:000001": ["TRIM"]},
            "executions": [
                {
                    "key": "KR:000001",
                    "price": "11000",
                    "at": (s.as_of + timedelta(hours=1)).isoformat(),
                }
            ],
        },
        {
            "at": later,
            "instruments": [
                {
                    "market": "KR",
                    "symbol": "000001",
                    "price": "10000",
                    "price_at": later,
                }
            ],
            "responses": {identity: response(p, "HOLD")},
            "expected": {"KR:000001": ["HOLD"]},
        },
    ]
    return data, {
        "name": "partial-sale-before-fall",
        "classification": "synthetic",
        "steps": steps,
        "new_limit": 0,
    }


def test_replay_uses_next_fill_price_costs_and_same_initial_assets(tmp_path):
    data, case = case_fixture()
    r = replay(case, base=data, output=tmp_path)
    m = r["metrics"]["personal:KRW"]
    assert r["passed"] and r["actual_sends"] == 0 and len(r["fills"]) == 1
    # 25 shares at 11,000, not signal 11,490; fee 825. Remaining 75 at 10,000.
    assert D(m["proposed"]["final_equity"]) == D(3024175)
    assert D(m["buy_hold"]["final_equity"]) == D(3000000)
    assert D(m["proposed"]["realized_net"]) == D(99175)
    assert D(m["proposed"]["unrealized"]) == D(225000)
    assert D(m["proposed"]["costs"]) == 825
    assert D(m["proposed"]["max_drawdown"]) > D(m["buy_hold"]["max_drawdown"])
    assert (
        data["positions"][0]["quantity"] == "100"
        and data["cash"][0]["available"] == "2000000"
    )


def test_losing_relative_result_is_preserved(tmp_path):
    data, case = case_fixture()
    case["steps"][1]["instruments"][0]["price"] = "16000"
    r = replay(case, base=data, output=tmp_path)
    m = r["metrics"]["personal:KRW"]
    assert r["passed"]
    assert D(m["proposed"]["final_equity"]) < D(m["buy_hold"]["final_equity"])
    assert D(m["upside_participation"]) < 1


def test_deposit_does_not_hide_price_drawdown(tmp_path):
    data, case = case_fixture()
    case["steps"][1]["deposits"] = [
        {"account": "personal-a", "currency": "KRW", "amount": "1000000"}
    ]
    r = replay(case, base=data, output=tmp_path)
    assert D(r["metrics"]["personal:KRW"]["buy_hold"]["max_drawdown"]) < 0
    assert D(r["metrics"]["personal:KRW"]["proposed"]["final_equity"]) == D(4024175)


@pytest.mark.parametrize(
    "kind", ["same_time_fill", "last_step_fill", "cash_reset", "future_bar"]
)
def test_replay_rejects_impossible_history(tmp_path, kind):
    data, case = case_fixture()
    if kind == "same_time_fill":
        case["steps"][0]["executions"][0]["at"] = case["steps"][0]["at"]
    if kind == "last_step_fill":
        case["steps"][1]["executions"] = case["steps"][0]["executions"]
    if kind == "cash_reset":
        case["steps"][1]["cash"] = data["cash"]
    if kind == "future_bar":
        data["instruments"][0]["bars"][0]["date"] = "2099-01-01"
    with pytest.raises(ValueError):
        replay(case, base=data, output=tmp_path)


def test_calculation_without_opinion_is_not_passing_model_replay(tmp_path):
    data, case = case_fixture()
    for step in case["steps"]:
        step.pop("responses")
        step.pop("expected")
    r = replay(case, base=data, output=tmp_path)
    assert not r["passed"] and all(s["unanalysed_rows"] for s in r["steps"])


def decision_report():
    data, case = case_fixture()
    return build_report(
        parse_snapshot(data), recorded=case["steps"][0]["responses"], new_limit=0
    )


def test_delivery_failure_restart_and_ack_cursor(tmp_path):
    r = decision_report()
    now = datetime.fromisoformat(r["as_of"])
    q = DecisionDeliveryQueue(tmp_path)
    assert q.stage(r, now=now)["disposition"] == "READY"
    # Failed/unknown transport does not acknowledge; restart offers the same ID.
    q = DecisionDeliveryQueue(tmp_path)
    assert q.ready(now=now)["report_id"] == r["report_id"]
    with pytest.raises(ValueError):
        q.acknowledge(r["report_id"], ["personal-a:KR:000001"], receipt="", now=now)
    q.acknowledge(r["report_id"], ["personal-a:KR:000001"], receipt="fake:123", now=now)
    assert q.ready(now=now) is None and q.stage(r, now=now)["disposition"] == "SILENT"


def test_night_pending_morning_requires_fresh_analysis_and_on_demand_immediate(
    tmp_path,
):
    r = decision_report()
    night = datetime.fromisoformat("2026-09-07T23:00:00+09:00")
    q = DecisionDeliveryQueue(tmp_path)
    assert q.stage(r, now=night)["disposition"] == "PENDING_MORNING"
    assert q.ready(now=night + timedelta(hours=9))["disposition"] == "REFRESH_REQUIRED"
    assert (
        meaningful_changes(r, None, on_demand=True, now=night)["disposition"]
        == "RESPOND"
    )


def test_reusable_report_id_is_safe_and_same_report_save_idempotent(tmp_path):
    r = decision_report()
    s = DecisionReportStore(tmp_path)
    s.save(r)
    s.save(r)
    assert s.read(r["report_id"]) == r
    with pytest.raises(ValueError):
        s.read("../../private")
