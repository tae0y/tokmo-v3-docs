"""The policy bounds the model: buys inside the approved band, sells at least the minimum return over the actual fill."""

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.values.rotation_policy import (
    RotationPolicy,
    align_price,
    apply_band_changes,
    bound_decision,
    load_policies,
)

NOW = datetime(2026, 9, 11, 1, 0, 20, tzinfo=UTC)
TODAY = date(2026, 9, 11)
EXAMPLE = (
    Path(__file__).resolve().parents[1]
    / "ops"
    / "hyundai-rotation"
    / "experiments.example.json"
)
BASIS = Decimal(372500)  # the first live fill


def policy(**changes):
    base = RotationPolicy(
        "test-005380",
        "personal-test",
        "005380",
        "KR",
        Decimal(1),
        Decimal(450000),
        Decimal(7),
        Decimal(1500),
        Decimal(380000),
        Decimal(350000),
        Decimal(392000),
        Decimal(3),
        Decimal(2),
        NOW + timedelta(days=100),
        "test-v1",
    )
    return replace(base, **changes)


def answer(**changes):
    data = dict(
        symbol="005380",
        action="ENTER",
        buy_price="385000",
        sell_price="402000",
        expires_on="2026-12-24",
        reason="최근 저가 매수세 근거",
        evidence_ids=["quote"],
    )
    data.update(changes)
    return json.dumps(data, ensure_ascii=False)


def decide(raw, *, holding=False, pol=None, cost_basis=None):
    decided, note, _ = bound_decision(
        raw,
        policy=pol or policy(),
        today=TODAY,
        holding=holding,
        fallback_evidence_id="policy",
        cost_basis=cost_basis,
    )
    return json.loads(decided), note


def propose(raw, *, holding=False, cost_basis=None):
    return bound_decision(
        raw,
        policy=policy(),
        today=TODAY,
        holding=holding,
        fallback_evidence_id="policy",
        cost_basis=cost_basis,
    )[2]


def test_example_policy_file_loads_and_binds_the_envelope():
    (loaded,) = load_policies(EXAMPLE.read_text(encoding="utf-8"))
    envelope = loaded.envelope()
    assert (
        envelope.symbol,
        envelope.market,
        envelope.max_quantity,
        envelope.protected_quantity,
    ) == ("005380", "KR", Decimal(1), Decimal(7))
    assert envelope.valid_until == datetime.fromisoformat("2027-01-01T00:00:00+09:00")
    assert (
        "account_scope" not in loaded.document()
        and loaded.document()["sell_min_pct"] == "2"
    )


def test_sell_floor_and_target_follow_the_actual_fill_on_the_tick():
    # v0.6 worked example: 372,500 × 1.02 = 379,950 -> 380,000; × 1.03 = 383,675 -> 384,000.
    assert policy().sell_floor_for(BASIS) == Decimal(380000)
    assert policy().sell_target_for(BASIS) == Decimal(384000)
    assert policy(sell_min_pct=Decimal("2.5")).sell_floor_for(BASIS) == Decimal(382000)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["experiments"][0].update(buy_ceiling="370000"),
        lambda d: d["experiments"][0].update(sell_min_pct="4"),
        lambda d: d["experiments"][0].update(sell_min_pct="0"),
        lambda d: d["experiments"][0].pop("sell_target_pct"),
        lambda d: d["experiments"][0].update(min_spread="8000"),
        lambda d: d["experiments"][0].update(quantity="1.5"),
        lambda d: d["experiments"][0].update(valid_until="2027-01-01T00:00:00"),
        lambda d: d.update(schema="rotation-policies-v2"),
        lambda d: d["experiments"].append(dict(d["experiments"][0])),
    ],
)
def test_invalid_policy_files_are_refused(mutate):
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    mutate(document)
    with pytest.raises(ValueError):
        load_policies(json.dumps(document))


def test_duplicate_keys_and_nan_are_refused():
    with pytest.raises(ValueError):
        load_policies('{"schema":"rotation-policies-v1","schema":"x","experiments":[]}')
    document = json.loads(EXAMPLE.read_text(encoding="utf-8"))
    with pytest.raises(ValueError):
        load_policies(json.dumps(document).replace('"1500"', "NaN"))


def test_krx_ticks_round_buys_down_and_sells_up():
    assert align_price(Decimal(385123), "KR", "BUY") == Decimal(385000)
    assert align_price(Decimal(402001), "KR", "SELL") == Decimal(402500)
    assert align_price(Decimal(123456), "KR", "BUY") == Decimal(123400)
    assert align_price(Decimal(512345), "KR", "SELL") == Decimal(513000)


def test_model_prices_inside_the_rules_pass_with_tick_alignment_and_today_expiry():
    decision, note = decide(answer(buy_price="385123", sell_price="402001"))
    assert note == "model"
    assert (decision["action"], decision["buy_price"], decision["sell_price"]) == (
        "ENTER",
        "385000",
        "402500",
    )
    assert decision["expires_on"] == "2026-09-11"
    assert decision["reason"] == "최근 저가 매수세 근거" and decision[
        "evidence_ids"
    ] == ["quote"]


def test_buy_is_clamped_to_the_band_and_a_high_sell_is_not_capped():
    decision, note = decide(answer(buy_price="300000", sell_price="480000"))
    assert note == "model_price_clamped"
    assert (decision["buy_price"], decision["sell_price"]) == ("350000", "480000")


def test_a_sell_under_the_minimum_return_is_raised_to_the_floor():
    decision, note = decide(
        answer(buy_price="380000", sell_price="383000")
    )  # +0.8% < 2%
    assert (
        note == "model_sell_below_minimum_raised" and decision["sell_price"] == "388000"
    )  # 380,000 × 1.02 = 387,600 -> 388,000
    decision, note = decide(
        answer(action="EXIT", buy_price=None, sell_price="379500", expires_on=None),
        holding=True,
        cost_basis=BASIS,
    )
    assert (
        note == "model_sell_below_minimum_raised" and decision["sell_price"] == "380000"
    )


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "not json",
        '{"action":"ENTER"}',
        answer(reason=""),
        answer(evidence_ids=[]),
        answer(buy_price="abc"),
    ],
)
def test_unusable_answers_collapse_to_the_default_band(raw):
    decision, note = decide(raw)
    assert note.endswith("default_band")
    # default buy 380,000 and the 3% target over it: 391,400 -> 391,500 on the tick
    assert (decision["action"], decision["buy_price"], decision["sell_price"]) == (
        "ENTER",
        "380000",
        "391500",
    )
    assert (
        decision["evidence_ids"] == ["policy"]
        and decision["expires_on"] == "2026-09-11"
    )


def test_hold_is_the_models_to_keep():
    decision, note = decide(
        answer(action="HOLD", buy_price=None, sell_price=None, expires_on=None)
    )
    assert (
        note == "model"
        and decision["action"] == "HOLD"
        and decision["sell_price"] is None
    )


def test_inventory_state_decides_enter_versus_exit_and_the_exit_floor_uses_the_fill():
    exit_decision, note = decide(
        answer(action="EXIT", buy_price=None, sell_price="405000", expires_on=None),
        holding=True,
        cost_basis=BASIS,
    )
    assert note == "model" and (
        exit_decision["action"],
        exit_decision["sell_price"],
        exit_decision["buy_price"],
    ) == ("EXIT", "405000", None)
    wrong, note = decide(answer(), holding=True, cost_basis=BASIS)
    assert note == "model_action_mismatch_default_band" and (
        wrong["action"],
        wrong["sell_price"],
    ) == ("EXIT", "384000")
    wrong, note = decide(
        answer(action="EXIT", buy_price=None, sell_price="405000", expires_on=None),
        holding=False,
    )
    assert note == "model_action_mismatch_default_band" and wrong["action"] == "ENTER"


def test_a_held_lot_without_a_known_fill_price_holds_instead_of_guessing():
    decision, note = decide(
        answer(action="EXIT", buy_price=None, sell_price="405000", expires_on=None),
        holding=True,
        cost_basis=None,
    )
    assert (
        note == "cost_basis_unknown"
        and decision["action"] == "HOLD"
        and "체결가" in decision["reason"]
    )


def test_stray_section_citations_are_trimmed_not_fatal():
    decided, note, _ = bound_decision(
        answer(evidence_ids=["quote", "experiment", "limits"]),
        policy=policy(),
        today=TODAY,
        holding=False,
        fallback_evidence_id="policy",
        evidence_ids={"quote", "policy"},
    )
    assert note == "model_citations_trimmed" and json.loads(decided)[
        "evidence_ids"
    ] == ["quote"]
    decided, note, _ = bound_decision(
        answer(evidence_ids=["experiment"]),
        policy=policy(),
        today=TODAY,
        holding=False,
        fallback_evidence_id="policy",
        evidence_ids={"quote", "policy"},
    )
    assert (
        json.loads(decided)["evidence_ids"] == ["policy"]
        and json.loads(decided)["buy_price"] == "385000"
    )


def test_inside_the_rules_there_is_no_proposal():
    assert propose(answer()) is None
    assert (
        propose(answer(action="HOLD", buy_price=None, sell_price=None, expires_on=None))
        is None
    )
    assert propose("garbage") is None


def test_clamped_buys_and_sub_minimum_sells_propose_the_implied_edge():
    proposal = propose(answer(buy_price="300000"))
    assert (
        proposal["changes"] == {"buy_floor": "294000"}
        and proposal["reason"] == "최근 저가 매수세 근거"
    )
    proposal = propose(
        answer(action="EXIT", buy_price=None, sell_price="378000", expires_on=None),
        holding=True,
        cost_basis=BASIS,
    )
    assert proposal["changes"] == {"sell_min_pct": "1.4"}  # 378,000 / 372,500 = +1.47%
    assert (
        propose(
            answer(action="EXIT", buy_price=None, sell_price="373000", expires_on=None),
            holding=True,
            cost_basis=BASIS,
        )
        is None
    )


def test_models_explicit_band_proposal_wins_when_it_validates():
    explicit = dict(
        band_proposal={
            "sell_min_pct": "1.5",
            "sell_target_pct": "2.5",
            "reason": "상승 여력 제한",
        }
    )
    proposal = propose(answer(**explicit))
    assert (
        proposal["changes"] == {"sell_min_pct": "1.5", "sell_target_pct": "2.5"}
        and proposal["reason"] == "상승 여력 제한"
    )
    # A minimum above the target would leave the policy invalid.
    assert (
        propose(answer(band_proposal={"sell_min_pct": "5", "reason": "반쪽 제안"}))
        is None
    )
    invalid = dict(band_proposal={"sell_floor": "100000", "reason": "폐지된 필드"})
    assert propose(answer(**invalid)) is None
    assert propose(answer(buy_price="300000", **invalid))["changes"] == {
        "buy_floor": "294000"
    }
    with pytest.raises(ValueError):
        apply_band_changes(policy(), {"protected_quantity": "10"})


def test_decision_day_must_be_a_calendar_date():
    with pytest.raises(ValueError):
        bound_decision(
            answer(),
            policy=policy(),
            today=NOW,
            holding=False,
            fallback_evidence_id="policy",
        )
