"""Behavioral and accounting checks for the new investment decisions path."""

import json
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.facts.toss_reads import build_read_request, parse_read_response
from tokmo.facts.toss_reads_live import _wire_url
from tokmo.programs.quant_decision_analysis import (
    AnalysisValidationError,
    analysis_display_values,
    build_report,
    make_packet,
    render_report,
    run_analysis,
    validate_analysis,
)
from tokmo.programs.quant_decision_calculations import (
    features,
    portfolio_totals,
    relationships,
    size_options,
)
from tokmo.programs.quant_decision_inputs import playbook_evidence
from tokmo.values.quant_decisions import parse_snapshot


def sample_input():
    now = datetime(2026, 9, 7, 1, tzinfo=UTC)

    def stock(symbol, market, start, step, name):
        day = datetime(2026, 1, 1, 21, tzinfo=UTC)
        bars = []
        n = 0
        while len(bars) < 150:
            day += timedelta(days=1)
            if day.weekday() >= 5:
                continue
            p = D(start) + D(step) * n + D(str((n % 7) - 3)) / 10
            n += 1
            bars.append(
                {
                    "date": day.date().isoformat(),
                    "available_at": day.isoformat(),
                    "open": str(p),
                    "high": str(p * D("1.02")),
                    "low": str(p * D(".98")),
                    "close": str(p),
                    "volume": "10000",
                    "adjusted": True,
                }
            )
        return {
            "symbol": symbol,
            "market": market,
            "currency": "KRW" if market == "KR" else "USD",
            "name": name,
            "kind": "stock",
            "price": bars[-1]["close"],
            "price_at": now.isoformat(),
            "bars": bars,
            "sector": "technology",
            "evidence": [
                {
                    "id": "business-1",
                    "kind": "business",
                    "text": "계약 확대 공시와 이익 증가가 확인된 가상 기업입니다.",
                    "source": "합성 검증 자료",
                    "available_at": now.isoformat(),
                }
            ],
        }

    a = stock("000001", "KR", "10000", "10", "가상 성장기업")
    b = stock("SAMPLE", "US", "100", "1", "가상 미국기업")
    return {
        "schema_version": 1,
        "as_of": now.isoformat(),
        "synthetic": True,
        "instruments": [a, b],
        "positions": [
            {
                "account": "personal-a",
                "scope": "personal",
                "key": "KR:000001",
                "quantity": "100",
                "average_cost": "7000",
                "sellable": "100",
                "observed_at": now.isoformat(),
            }
        ],
        "cash": [
            {
                "account": "personal-a",
                "scope": "personal",
                "currency": "KRW",
                "available": "2000000",
                "total_cash": "2000000",
                "observed_at": now.isoformat(),
                "source": "합성 계좌",
            },
            {
                "account": "personal-a",
                "scope": "personal",
                "currency": "USD",
                "available": "1000",
                "total_cash": "1000",
                "observed_at": now.isoformat(),
                "source": "합성 계좌",
            },
        ],
        "fx": {
            "base_currency": "USD",
            "quote_currency": "KRW",
            "rate": "1400",
            "observed_at": now.isoformat(),
            "source": "합성 환율",
        },
    }


def response(packet, action="HOLD", option=None):
    return {
        "action": action,
        "option_id": None if option is None else option["id"],
        "summary": "사업 가설을 유지하면서 계좌 부담을 점검합니다.",
        "reasons": [
            {
                "text": "가격 상승과 사업 근거를 함께 비교했습니다.",
                "sources": ["business-1", "features"],
            }
        ],
        "counterevidence": "최근 추세가 꺾이면 추가 하락할 수 있습니다.",
        "change_conditions": ["다음 실적에서 기존 성장 근거가 유지되는지 확인합니다."],
        "thesis_status": "supported",
    }


def test_merged_snapshot_has_native_prices_and_one_exchange_rate():
    s = parse_snapshot(sample_input())
    t = portfolio_totals(s)["personal:KRW"]
    assert t["total"] == s.instruments[0].price * 100 + D(2000000) + D(1400000)
    opts = size_options(s, s.instruments[1], "personal-a", "personal")
    assert opts and all(o["currency"] == "USD" for o in opts)
    for o in opts:
        assert o["cash_after"] >= 0
        assert (
            o["weight_after"]
            == o["holding_after"] * s.instruments[1].price * 1400 / o["portfolio_after"]
        )


def test_missing_fx_preserves_independent_market_calculations():
    d = sample_input()
    d["fx"] = None
    s = parse_snapshot(d)
    assert set(portfolio_totals(s)) >= {"personal:KRW", "personal:USD"}
    opts = size_options(s, s.instruments[1], "personal-a", "personal")
    assert opts and all(o["portfolio_currency"] == "USD" for o in opts)


def test_quote_loss_is_not_silently_complete_account():
    d = sample_input()
    d["instruments"][0]["price"] = None
    s = parse_snapshot(d)
    assert not portfolio_totals(s)["personal:KRW"]["complete"]
    assert size_options(s, s.instruments[0], "personal-a", "personal") == []


def test_unconfirmed_sellable_does_not_force_hold_but_never_invents_trade_size():
    d = sample_input()
    d["positions"][0]["sellable"] = None
    s = parse_snapshot(d)
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    raw = response(p, "TRIM")
    assert validate_analysis(raw, p)["action"] == "TRIM"
    r = build_report(s, recorded={"personal-a:KR:000001": raw}, new_limit=0)
    assert r["results"][0]["selected_option"] is None
    assert "거래 주수는 미확정" in render_report(r)
    raw["option_id"] = "invented"
    with pytest.raises(ValueError):
        validate_analysis(raw, p)


def test_price_proposals_respect_kr_stock_and_etf_ticks():
    from dataclasses import replace

    from tokmo.programs.quant_decision_calculations import round_price

    i = parse_snapshot(sample_input()).instruments[0]
    assert round_price(i, D(393333)) == D(393000)
    assert round_price(i, D(1790999), up=True) == D(1791000)
    assert round_price(replace(i, kind="etf"), D(110823)) == D(110820)


def test_unknown_single_order_direction_is_explicit_and_capture_time_preserved():
    d = sample_input()
    d["positions"][0]["sellable_at"] = "2026-09-07T00:50:00+00:00"
    d["orders"] = [
        {
            "account": "personal-a",
            "key": "KR:000001",
            "kind": "conditional",
            "type": "SINGLE",
            "legs": [{"side": None, "price": "15000"}],
            "relationship": "OCO 설명이 잘못 붙었던 과거 입력",
        }
    ]
    s = parse_snapshot(d)
    packet = make_packet(s, s.instruments[0], "personal-a", "personal")
    assert packet["orders"][0]["relationship"] == "단일 조건주문"
    assert "방향 미확인" in packet["orders"][0]["direction_status"]
    assert packet["holdings"][0]["sellable_at"] == "2026-09-07T00:50:00+00:00"
    d["positions"][0]["sellable_at"] = "2099-01-01T00:00:00+00:00"
    with pytest.raises(ValueError, match="future sellable"):
        parse_snapshot(d)


def test_buying_power_is_not_total_cash_or_double_reserved():
    d = sample_input()
    for c in d["cash"]:
        c.pop("total_cash")
    d["orders"] = [
        {
            "account": "personal-a",
            "key": "KR:000001",
            "side": "BUY",
            "reserved_cash": "1900000",
        }
    ]
    s = parse_snapshot(d)
    assert not portfolio_totals(s)["personal:KRW"]["complete"]
    opts = size_options(s, s.instruments[0], "personal-a", "personal")
    assert all(o["cash_before"] == D(2000000) for o in opts)


def test_additional_capital_does_not_change_the_default_or_retirement():
    d = sample_input()
    base = parse_snapshot(d)
    d["extra_capital"] = [
        {
            "account": "personal-a",
            "scope": "personal",
            "currency": "KRW",
            "amount": "3000000",
        }
    ]
    s = parse_snapshot(d)
    assert portfolio_totals(s) == portfolio_totals(base)
    assert (
        portfolio_totals(s, include_extra=True)["personal:KRW"]["total"]
        - portfolio_totals(s)["personal:KRW"]["total"]
        == 3000000
    )
    a = size_options(s, s.instruments[0], "personal-a", "personal", include_extra=True)
    b = size_options(s, s.instruments[0], "personal-a", "personal")
    assert max(o["quantity"] for o in a if o["action"] == "BUY") > max(
        o["quantity"] for o in b if o["action"] == "BUY"
    )


def test_no_pension_buy_without_verified_product_eligibility():
    d = sample_input()
    d["cash"].append(dict(d["cash"][0], account="pension-a", scope="retirement"))
    s = parse_snapshot(d)
    assert size_options(s, s.instruments[0], "pension-a", "retirement") == []


def test_sellable_quantity_not_total_holding_controls_sells():
    d = sample_input()
    d["positions"][0]["sellable"] = "16"
    s = parse_snapshot(d)
    opts = size_options(s, s.instruments[0], "personal-a", "personal")
    assert max(o["quantity"] for o in opts if o["action"] == "SELL") == 16


def test_add_above_old_cost_raises_average_even_after_price_pullback():
    d = sample_input()
    d["instruments"][0]["price"] = "9000"
    s = parse_snapshot(d)
    opts = size_options(s, s.instruments[0], "personal-a", "personal")
    buy = next(
        o for o in opts if o["action"] == "BUY" and o["range_id"] == "participate"
    )
    assert D(7000) < buy["average_cost_after"] < buy["price_upper"]
    sell = next(o for o in opts if o["action"] == "SELL")
    assert sell["average_cost_after"] == sell["average_cost_before"] == D(7000)
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    option = next(o for o in p["options"] if o["action"] == "SELL")
    with pytest.raises(ValueError, match="disposal"):
        validate_analysis(response(p, "EXIT", option), p)


def test_high_rsi_is_not_a_universal_buy_veto():
    s = parse_snapshot(sample_input())
    i = s.instruments[0]
    assert features(i)["rsi_14"] > 65
    assert any(
        o["action"] == "BUY" and o["range_id"] == "participate"
        for o in size_options(s, i, "personal-a", "personal")
    )


def test_cross_market_correlation_uses_completed_weeks():
    s = parse_snapshot(sample_input())
    r = relationships(s, s.instruments[1])[0]
    assert (
        r["interval"] == "weekly"
        and r["samples"] >= 12
        and r["correlation"] is not None
    )
    assert "환율 변동 미포함" in r["basis"]


@pytest.mark.parametrize(
    "case",
    [
        "duplicate_holding",
        "duplicate_cash",
        "future_cash",
        "future_holding",
        "wrong_currency",
    ],
)
def test_reject_ambiguous_money_inputs(case):
    d = sample_input()
    if case == "duplicate_holding":
        d["positions"] *= 2
    if case == "duplicate_cash":
        d["cash"] += d["cash"][:1]
    if case == "future_cash":
        d["cash"][0]["observed_at"] = "2099-01-01T00:00:00Z"
    if case == "future_holding":
        d["positions"][0]["observed_at"] = "2099-01-01T00:00:00Z"
    if case == "nan_fx":
        d["fx"]["rate"] = "NaN"
    if case == "wrong_currency":
        d["instruments"][0]["currency"] = "USD"
    with pytest.raises(ValueError):
        parse_snapshot(d)


def test_invalid_fx_degrades_to_market_results_and_empty_scope_not_zero():
    d = sample_input()
    d["fx"]["rate"] = "NaN"
    s = parse_snapshot(d)
    assert s.fx is None and s.limitations
    assert set(portfolio_totals(s)) == {"personal:KRW", "personal:USD"}


def test_model_cannot_invent_money_but_can_quote_calculated_shares():
    s = parse_snapshot(sample_input())
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    o = next(o for o in p["options"] if o["action"] == "SELL")
    r = response(p, "TRIM", o)
    r["summary"] = o["quantity"] + "주 매도 검토"
    assert validate_analysis(r, p)["action"] == "TRIM"
    r["summary"] = "99999999주 매도 검토"
    with pytest.raises(ValueError, match="claim"):
        validate_analysis(r, p)


def test_source_numbers_are_allowed_in_their_unit_without_authorizing_new_sizes():
    s = parse_snapshot(sample_input())
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    p["evidence"].append(
        {
            "id": "news-prose",
            "kind": "news",
            "text": "공급사 매출 +52% 증가 보도; 과거 관찰선 97,800원",
        }
    )
    r = response(p)
    r["summary"] = (
        "공급사 매출 +52% 증가 보도와 과거 97,800원 관찰선을 현재 자료와 비교합니다."
    )
    assert validate_analysis(r, p)["action"] == "HOLD"
    r["summary"] = "97800주를 보유합니다."
    with pytest.raises(ValueError, match="claim"):
        validate_analysis(r, p)


def test_decline_magnitude_and_short_source_alias_are_preserved():
    d = sample_input()
    d["instruments"][0]["price"] = "6038.6"
    s = parse_snapshot(d)
    p = make_packet(s, s.instruments[0], "personal-a", "personal")
    r = response(p)
    r["summary"] = "평단보다 13.7% 낮은 가격입니다."
    r["reasons"][0]["sources"] = "E1"
    checked = validate_analysis(r, p)
    assert checked["reasons"][0]["sources"] == ["business-1"]


def choice_input():
    data = sample_input()
    data["user_choices"] = [
        {
            "id": "choice-current",
            "raw_text": "공장 적용이 늘어날 것으로 기대해서 보유한다.",
            "recorded_at": data["as_of"],
            "execution_status": "NOT_VERIFIED",
            "interpretation": {
                "account": "personal-a",
                "scope": "personal",
                "keys": ["KR:000001"],
                "action": "HOLD",
                "phase": "DECIDED",
            },
        }
    ]
    return data


def test_current_choice_reference_keeps_exact_id_and_does_not_mutate_inputs():
    snapshot = parse_snapshot(choice_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["reasons"][0]["sources"] = ["choice-current", "E1", "choice-current"]
    originals = deepcopy((packet, raw))
    checked = validate_analysis(raw, packet)
    assert checked["reasons"][0]["sources"] == ["choice-current", "business-1"]
    assert (packet, raw) == originals
    # Previously stored root-level references remain valid.
    raw["reasons"][0]["sources"] = ["current_user_choices"]
    assert validate_analysis(raw, packet)["reasons"][0]["sources"] == [
        "current_user_choices"
    ]


@pytest.mark.parametrize("different", ["account", "scope", "keys", "future", "absent"])
def test_choice_outside_this_analysis_is_not_an_allowed_reference(different):
    data = choice_input()
    if different == "future":
        data["user_choices"][0]["recorded_at"] = "2099-01-01T00:00:00Z"
    elif different == "absent":
        data["user_choices"] = []
    else:
        data["user_choices"][0]["interpretation"][different] = {
            "account": "other-account",
            "scope": "retirement",
            "keys": ["US:SAMPLE"],
        }[different]
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    assert packet["current_user_choices"] == []
    raw = response(packet)
    raw["reasons"][0]["sources"] = ["choice-current"]
    with pytest.raises(ValueError, match="unknown evidence reference.*choice-current"):
        validate_analysis(raw, packet)


def test_saved_packet_cannot_authorize_another_accounts_choice():
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    packet["current_user_choices"] = choice_input()["user_choices"]
    packet["current_user_choices"][0]["interpretation"]["account"] = "other-account"
    raw = response(packet)
    raw["reasons"][0]["sources"] = ["choice-current"]
    with pytest.raises(ValueError, match="unknown evidence reference"):
        validate_analysis(raw, packet)


def test_user_price_quote_preserves_its_unit_and_does_not_promote_ai_suggestions():
    data = choice_input()
    data["user_choices"][0]["raw_text"] = "97,800원에서 기존 선택을 다시 점검해 줘."
    data["user_choices"][0]["interpretation"]["suggested_checks"] = [
        "91,234원에서 재점검"
    ]
    snapshot = parse_snapshot(data)
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["reasons"][0] = {
        "text": "사용자가 97,800원에서 재점검을 선택했다.",
        "sources": ["choice-current"],
    }
    assert validate_analysis(raw, packet)["reasons"][0]["sources"] == ["choice-current"]
    for bad in [
        "97,800주를 보유한다.",
        "97,800달러에서 재점검한다.",
        "91,234원에서 점검하기로 선택했다.",
    ]:
        raw["reasons"][0]["text"] = bad
        with pytest.raises(ValueError, match="claim"):
            validate_analysis(raw, packet)


def test_display_values_keep_ratios_rsi_and_missing_values_distinct():
    packet = {
        "features": {
            "ret_60": "-0.405819",
            "ret_5": "0.031915",
            "ret_20": None,
            "rsi_14": "34.920635",
            "macd_histogram": "-173.61",
            "volume_change_20": "-0.00001",
            "ret_120": "NaN",
        },
        "holdings": [
            {
                "unrealized_return": "-0.263143",
                "weight_in_portfolio": "0.13122",
                "weight_within_equity": None,
            }
        ],
    }
    before = deepcopy(packet)
    display = analysis_display_values(packet)
    assert display == {
        "features.ret_60": "-40.6%",
        "features.ret_5": "3.2%",
        "features.rsi_14": "34.9",
        "features.volume_change_20": "0.0%",
        "holdings[0].unrealized_return": "-26.3%",
        "holdings[0].weight_in_portfolio": "13.1%",
    }
    assert packet == before


@pytest.mark.parametrize("display_present", [False, True])
def test_return_rounding_is_checked_against_raw_value(display_present):
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    packet["features"]["ret_60"] = "-0.405819"
    packet.pop("display_values", None)
    if display_present:
        packet["display_values"] = analysis_display_values(packet)
    raw = response(packet)
    raw["summary"] = "60거래일 수익률은 -40.6%다."
    assert validate_analysis(raw, packet)["summary"] == raw["summary"]
    raw["summary"] = "60거래일 수익률은 -40%다."
    with pytest.raises(
        ValueError, match="summary: unsupported monetary/share claim: -40%"
    ):
        validate_analysis(raw, packet)
    assert raw["summary"] == "60거래일 수익률은 -40%다."


def test_derived_display_metadata_does_not_authorize_extra_money_or_shares():
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    packet["display_values"]["untrusted"] = "987654321"
    for unit in ("원", "달러", "주", "%"):
        raw = response(packet)
        raw["summary"] = f"987654321{unit}로 확인했다."
        with pytest.raises(ValueError, match="claim"):
            validate_analysis(raw, packet)


@pytest.mark.parametrize(
    "phrase",
    [
        "987654321%입니다.",
        "987654321%다.",
        "987654321%라는 수치",
        "987654321원입니다.",
        "987654321달러다.",
        "987654321주입니다.",
    ],
)
def test_korean_copula_does_not_bypass_numeric_validation(phrase):
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["summary"] = phrase
    with pytest.raises(ValueError, match="claim"):
        validate_analysis(raw, packet)


def test_single_correction_receives_all_independent_issues_and_computed_display():
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    packet.pop(
        "display_values"
    )  # Older saved input can be analyzed without rewriting it.
    packet["features"]["ret_60"] = "-0.405819"
    before = deepcopy(packet)
    prompts = []

    def model(prompt):
        prompts.append(prompt)
        supplied = json.JSONDecoder().raw_decode(prompt.split("\nINPUT\n", 1)[1])[0]
        raw = response(supplied)
        if len(prompts) == 1:
            raw["summary"] = "60거래일 수익률은 -40%다."
            raw["reasons"][0]["sources"] = ["missing-choice"]
        else:
            assert "reasons[0].sources: unknown evidence reference" in prompt
            assert "summary: unsupported monetary/share claim: -40%" in prompt
            raw["summary"] = (
                "60거래일 수익률은 "
                + supplied["display_values"]["features.ret_60"]
                + "다."
            )
        return raw

    checked, errors = run_analysis(packet, model)
    assert checked["action"] == "HOLD" and checked["summary"].endswith("-40.6%다.")
    assert len(prompts) == 2 and len(errors) == 1
    assert packet == before


@pytest.mark.parametrize("sources", [None, 1, {}, [None], [[]], [{"id": "business-1"}]])
def test_malformed_sources_report_a_validation_error(sources):
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["reasons"][0]["sources"] = sources
    with pytest.raises(ValueError, match="unknown evidence reference"):
        validate_analysis(raw, packet)


def test_many_issues_still_use_only_two_model_calls_and_bounded_errors():
    snapshot = parse_snapshot(sample_input())
    packet = make_packet(snapshot, snapshot.instruments[0], "personal-a", "personal")
    raw = response(packet)
    raw["reasons"] = [
        {"text": f"{987654321 + i}주를 보유했다.", "sources": [f"absent-{i}"]}
        for i in range(8)
    ]
    with pytest.raises(AnalysisValidationError) as exc:
        validate_analysis(raw, packet)
    assert len(exc.value.issues) == 8 and len(str(exc.value)) <= 2400
    calls = []

    def model(prompt):
        calls.append(prompt)
        return raw

    checked, errors = run_analysis(packet, model)
    assert checked is None and len(calls) == len(errors) == 2


def test_one_share_of_three_is_not_labelled_half():
    d = sample_input()
    d["positions"][0].update(quantity="3", sellable="3")
    s = parse_snapshot(d)
    options = size_options(s, s.instruments[0], "personal-a", "personal")
    sale = next(o for o in options if o["action"] == "SELL" and o["quantity"] == 1)
    assert sale["size_label"] == "일부 조절" and sale["portion_of_holding"] == D(1) / 3


def test_small_participation_can_be_less_than_the_initial_size_menu():
    s = parse_snapshot(sample_input())
    options = size_options(s, s.instruments[0], "personal-a", "personal")
    assert any(o["action"] == "BUY" and o["quantity"] == 1 for o in options)
    assert any(o["action"] == "BUY" and o["quantity"] > 1 for o in options)


def test_requested_symbol_does_not_call_model_for_every_holding_or_candidate():
    s = parse_snapshot(sample_input())
    calls = []

    def model(prompt):
        p = json.JSONDecoder().raw_decode(prompt.split("\nINPUT\n")[1])[0]
        calls.append(p["key"])
        return response(p, "WATCH")

    report = build_report(s, model=model, new_limit=0, only_keys=["US:SAMPLE"])
    assert calls == ["US:SAMPLE"] and len(report["results"]) == 1
    with pytest.raises(ValueError, match="입력이 없습니다"):
        build_report(s, only_keys=["US:UNKNOWN"])


def test_multiple_candidates_do_not_allocate_same_cash():
    d = sample_input()
    d["cash"][0]["available"] = d["cash"][0]["total_cash"] = "50000"
    d["instruments"].append(
        dict(d["instruments"][0], symbol="000002", name="두 번째 후보")
    )
    s = parse_snapshot(d)

    def model(prompt):
        p = json.loads(prompt.split("\nINPUT\n")[1].split("\n이전 응답")[0])
        opts = [o for o in p["options"] if o["action"] == "BUY"]
        return response(
            p,
            ("ADD" if p["holdings"] else "BUY")
            if opts
            else "HOLD"
            if p["holdings"]
            else "WATCH",
            opts[0] if opts else None,
        )

    r = build_report(s, model=model)
    cost = sum(
        -D(x["selected_option"]["cash_delta"])
        for x in r["results"]
        if x["selected_option"]
        and x["selected_option"]["action"] == "BUY"
        and x["selected_option"]["currency"] == "KRW"
    )
    assert cost <= 50000 and all(x["status"] == "ANALYZED" for x in r["results"])


def test_extra_is_not_used_for_report_base_and_empty_portfolio_works():
    d = sample_input()
    d["extra_capital"] = [
        {
            "account": "personal-a",
            "scope": "personal",
            "currency": "KRW",
            "amount": "3000000",
        }
    ]
    s = parse_snapshot(d)
    r = build_report(s)
    assert D(r["portfolio"]["personal:KRW"]["extra"]) == 0
    assert D(r["extra_portfolio"]["personal:KRW"]["extra"]) == 3000000
    d.update(instruments=[], positions=[], cash=[], extra_capital=[])
    assert build_report(parse_snapshot(d))["results"] == []


def test_partial_sale_reduces_measured_portfolio_volatility():
    s = parse_snapshot(sample_input())
    opts = size_options(s, s.instruments[0], "personal-a", "personal")
    sale = next(o for o in opts if o["action"] == "SELL")
    assert sale["risk_before"]["available"] and sale["risk_after"]["available"]
    assert (
        sale["risk_after"]["annualized_volatility"]
        < sale["risk_before"]["annualized_volatility"]
    )


def test_future_information_cannot_enter_replay_packet():
    d = sample_input()
    i = d["instruments"][0]
    i["evidence"].append(
        dict(i["evidence"][0], id="future", available_at="2099-01-01T00:00:00Z")
    )
    i["bars"].append(
        dict(
            i["bars"][-1],
            date="2099-01-01",
            available_at="2099-01-01T00:00:00Z",
            close="999999",
            high="1000000",
        )
    )
    s = parse_snapshot(d)
    assert "future" not in [e.id for e in s.instruments[0].evidence]
    assert len(s.instruments[0].bars) == 150


def test_one_failed_symbol_keeps_valid_results_and_no_unlimited_retry():
    s = parse_snapshot(sample_input())
    calls = []

    def model(prompt):
        packet = json.loads(prompt.split("\nINPUT\n")[1].split("\n이전 응답")[0])
        calls.append(packet["key"])
        if packet["key"] == "US:SAMPLE":
            return "{}"
        return response(packet)

    report = build_report(s, model=model)
    assert [r["status"] for r in report["results"]] == ["ANALYZED", "FAILED"]
    assert calls.count("US:SAMPLE") == 2


def test_playbook_reads_thesis_and_skips_migration_note(tmp_path):
    f = tmp_path / "book.md"
    f.write_text("""<!-- HERMES_CURRENT_START -->
**마이그레이션 메모:** 이전 버전 안내입니다.

신사업 진행을 확인합니다.
<!-- HERMES_CURRENT_END -->
<!-- USER_THESIS_START -->
## 2층
> 원문 안내
아틀라스 공장 도입까지 보유할 생각입니다.
<!-- USER_THESIS_END -->""")
    rows = playbook_evidence(f, as_of=datetime.now(UTC))
    assert any("아틀라스" in r["text"] and r["kind"] == "legacy_thesis" for r in rows)
    assert all("마이그레이션 메모" not in r["text"] for r in rows)


@pytest.mark.parametrize(
    "operation,params,body,field,expected",
    [
        (
            Op.BUYING_POWER,
            {"currency": "KRW"},
            {"currency": "KRW", "cashBuyingPower": "120000"},
            "cash_buying_power",
            "120000",
        ),
        (
            Op.SELLABLE_QUANTITY,
            {"symbol": "005380"},
            {"sellableQuantity": "5.5"},
            "sellable_quantity",
            "5.5",
        ),
    ],
)
def test_order_info_get_wire_and_values(operation, params, body, field, expected):
    request = build_read_request(
        operation, token="test-token", account_seq="123", **params
    )
    assert "/api/v1/" in _wire_url(request)
    result = parse_read_response(
        req=request,
        body=json.dumps({"result": body}).encode(),
        fetched_at=datetime.now(UTC),
    )
    assert result.code == "success"
    assert getattr(result.value, field) == expected
