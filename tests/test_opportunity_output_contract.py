"""Authored synthetic responses following packet guidance, not LLM conformance."""

from copy import deepcopy

import pytest
from test_opportunity_rotation import (
    at,
    bar,
    decision,
    discovery,
    market,
    paper,
    proposal,
    session,
)

from tokmo.programs.opportunity_rotation import build_packet


@pytest.mark.parametrize(
    "action", ["ENTER", "ADD", "EXIT", "SWITCH", "WATCH", "HOLD", "CANCEL"]
)
def test_explained_action_shapes_pass_production_decision(tmp_path, action):
    p = paper(tmp_path)
    held = action in ("ADD", "EXIT", "SWITCH", "HOLD")
    if held or action == "CANCEL":
        p.propose([proposal(time="09:00:00")], session())
        p.advance(market([bar("09:00:00")] if held else [], time="09:01:00"))
    d = discovery()
    d["candidates"].append(dict(deepcopy(d["candidates"][0]), symbol="222222"))
    pkt = build_packet(d, p.state())
    contract = pkt["decision_contract"]
    row = dict(symbol="111111", action=action, reason="합성 검토")
    if action in ("ENTER", "ADD", "EXIT", "SWITCH"):
        buy = action in ("ENTER", "ADD")
        pr = proposal(
            "next",
            side="BUY" if buy else "SELL",
            target="WEAK" if buy else "NONE",
            weak_reason="합성 약세 근거" if buy else None,
        )
        pr.pop("proposed_at")
        if buy:
            pr["protection"] = dict(target_price=10400, stop_price="9400")
        else:
            pr.update(trigger="BELOW", assumed_exit_price=11000)
        row["proposal"] = pr
    if action == "SWITCH":
        row.update(replacement_symbol="222222", replacement_reason="합성 비교")
    if action == "CANCEL":
        row["proposal_id"] = "b1"
    assert set(row) == set(contract["action_fields"][action])
    # The second, unheld candidate need not have an assessment.
    p.decide(pkt, decision(pkt, assessments=[row]), at("09:01:00"))
    assert p.state()["decisions"]["daily1"]["assessments"] == [row]
    if action in ("EXIT", "SWITCH"):
        assert p.state()["proposals"]["next"]["side"] == "SELL"
    if action == "CANCEL":
        assert p.state()["proposals"]["b1"]["status"] == "CANCELLED"


@pytest.mark.parametrize(
    "field,value",
    [
        ("trigger", "LIMIT"),
        ("target", "+3%"),
        ("assumed_exit_price", "10300"),
        ("assumed_exit_price", "10000"),
        ("quantity", True),
        ("quantity", 1.0),
        ("quantity", "1"),
        ("price", 10000.0),
        ("price", "10000.5"),
        ("price", "NaN"),
        ("price", "0"),
        ("weak_reason", "unexpected"),
        ("target", "WEAK"),
        ("protection", None),
        ("protection", dict(target_price="10300", stop_price="10000")),
        ("review_at", at("09:01:00")),
        ("review_at", "2026-09-28T14:00:00"),
        ("proposed_at", at("09:00:00")),
    ],
)
def test_documented_malformed_proposals_still_fail(tmp_path, field, value):
    p = paper(tmp_path)
    pkt = build_packet(discovery(), p.state())
    choice = decision(pkt)
    choice["assessments"][0]["proposal"][field] = value
    before = p.state()
    with pytest.raises(ValueError):
        p.decide(pkt, choice, at("09:01:00"))
    assert p.state() == before


@pytest.mark.parametrize("action", ["WATCH", "HOLD"])
@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_move", "합성 기대"),
        ("invalidation", "합성 무효 조건"),
        ("review_at", at("14:00:00")),
    ],
)
def test_nontrading_extra_fields_still_fail(tmp_path, action, field, value):
    p = paper(tmp_path)
    if action == "HOLD":
        p.propose([proposal(time="09:00:00")], session())
        p.advance(market([bar("09:00:00")], time="09:01:00"))
    pkt = build_packet(discovery(), p.state())
    row = dict(symbol="111111", action=action, reason="합성 대기", **{field: value})
    with pytest.raises(ValueError, match="fields"):
        p.decide(pkt, decision(pkt, assessments=[row]), at("09:01:00"))
