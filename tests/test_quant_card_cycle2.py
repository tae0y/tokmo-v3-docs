"""Cycle-2 content repair of the Quant decision card (user feedback on provider ids
81/82): raw playbook text/paths never reach Telegram, holdings weights over exact
labelled denominators (staleness, FX), the bounded OPEN conditional-order read
(one page per mapped account, limit 100, retry 0, write 0, never inferred),
playbook agreement/conflict, and formatting stability."""

import dataclasses
import json
import unicodedata
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_quant_telegram_card as card_tests

from tokmo.programs.portfolio_calculations import (
    FX_MAX_AGE_SECONDS_V1,
    fx_content_hash_v1,
)
from tokmo.programs.quant_card_conditional import (
    CONDITIONAL_PAGE_LIMIT,
    ConditionalScanV1,
    ScopeConditionalScanV1,
    collect_open_conditional_orders,
    conditional_context_for,
)
from tokmo.programs.quant_telegram_card import (
    MESSAGE_CHAR_CAP,
    CardContextV1,
    ConditionalContextV1,
    HoldingContextV1,
    PlaybookSynthesisV1,
    PositionV1,
    QuoteContextV1,
    classify_stance,
    compute_weights,
    entry_zone_condition_checks,
    fx_usd_krw_from_evidence,
    playbook_agreement,
    render_market_message,
    synthesize_playbook,
)
from tokmo.records.account_native_refs import AccountNativeRefStoreV1
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.values import MISSING
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.portfolio_calculations import FxEvidenceEnvelopeV1
from tokmo.values.toss_orders import (
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
)

NOW = card_tests.NOW
KST = card_tests.KST
TOSS_SCOPE = "toss-acct-" + "a" * 24
TOSS_SCOPE_2 = "toss-acct-" + "b" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24
_card, _report, VALUES_HOT, VALUES_CALM, ZONE = (
    card_tests._card,
    card_tests._report,
    card_tests.VALUES_HOT,
    card_tests.VALUES_CALM,
    card_tests.ZONE,
)


def _position(
    symbol, market, qty, price, *, scope=TOSS_SCOPE, label="개인계좌", reason=None
):
    currency = "KRW" if market == "KR" else "USD"
    return PositionV1(
        scope,
        label,
        market,
        symbol,
        Decimal(qty),
        None if price is None else Decimal(price),
        currency,
        reason,
    )


# --- weights ------------------------------------------------------------------------


def test_market_weight_uses_exact_same_account_market_denominator_and_fails_closed_on_missing_quote():
    positions = (
        _position("000001", "KR", "4", "3000000"),
        _position("000002", "KR", "10", "200000"),
        _position("NVDA", "US", "3", "100"),
    )
    weights = compute_weights(
        positions, fx_usd_krw=None, fx_reason="USD/KRW 환율 근거 없음"
    )
    kr = weights[(TOSS_SCOPE, "KR", "000001")]
    assert (
        kr.market_weight == Decimal(12000000) / Decimal(14000000)
        and kr.market_label == "개인계좌 KR 주식"
    )
    assert kr.total_weight is None and kr.total_reason == "USD/KRW 환율 근거 없음"
    us = weights[(TOSS_SCOPE, "US", "NVDA")]
    assert us.market_weight == Decimal(1) and us.market_label == "개인계좌 US 주식"
    # a missing quote anywhere in the same account+market kills that denominator, never invents
    stale = (
        _position("000001", "KR", "4", "3000000"),
        _position("000002", "KR", "10", None, reason="시세 관측시각 오래됨"),
    )
    weights = compute_weights(stale, fx_usd_krw=None, fx_reason=None)
    assert weights[(TOSS_SCOPE, "KR", "000001")].market_weight is None
    assert (
        weights[(TOSS_SCOPE, "KR", "000001")].market_reason
        == "같은 시장 보유 1종목 시세 미확인"
    )
    assert weights[(TOSS_SCOPE, "KR", "000002")].market_reason == "시세 관측시각 오래됨"
    assert (
        weights[(TOSS_SCOPE, "KR", "000001")].total_reason
        == "계좌 보유 종목 일부 시세 미확인"
    )
    # accounts are separate denominators even when both are personal Toss accounts
    two = (
        _position("000001", "KR", "1", "100"),
        _position("000001", "KR", "3", "100", scope=TOSS_SCOPE_2),
    )
    weights = compute_weights(two, fx_usd_krw=None, fx_reason=None)
    assert (
        weights[(TOSS_SCOPE, "KR", "000001")].market_weight
        == 1
        == weights[(TOSS_SCOPE_2, "KR", "000001")].market_weight
    )
    assert (
        weights[(TOSS_SCOPE, "KR", "000001")].total_weight == 1
    )  # KRW-only account needs no FX


def _fx(
    rate="1350.5",
    *,
    observed=NOW - timedelta(seconds=60),
    source="toss-exchange-rate-v1",
):
    base = FxEvidenceEnvelopeV1(
        1,
        source,
        "USD",
        "KRW",
        rate,
        observed,
        observed,
        observed,
        observed + timedelta(seconds=FX_MAX_AGE_SECONDS_V1),
        FX_MAX_AGE_SECONDS_V1,
        NOW,
        "0" * 64,
        "x",
    )
    return dataclasses.replace(base, content_hash=fx_content_hash_v1(base))


def test_total_weight_needs_valid_canonical_fx_and_never_invents_a_rate():
    positions = (
        _position("000001", "KR", "1", "1350500"),
        _position("NVDA", "US", "10", "100"),
    )
    assert fx_usd_krw_from_evidence(None, now=NOW) == (None, "USD/KRW 환율 근거 없음")
    rate, reason = fx_usd_krw_from_evidence(_fx(), now=NOW)
    assert rate == Decimal("1350.5") and reason is None
    weights = compute_weights(positions, fx_usd_krw=rate, fx_reason=reason)
    assert weights[(TOSS_SCOPE, "KR", "000001")].total_weight == Decimal("0.5")
    assert weights[(TOSS_SCOPE, "US", "NVDA")].total_weight == Decimal("0.5")
    for bad, expected in (
        (
            _fx(observed=NOW - timedelta(seconds=FX_MAX_AGE_SECONDS_V1 + 1)),
            "환율 근거 유효기간 경과",
        ),
        (_fx(source="other"), "환율 근거 계약 불일치"),
        (_fx(rate="0"), "환율 근거 계약 불일치"),
        (dataclasses.replace(_fx(), content_hash="f" * 64), "환율 근거 해시 불일치"),
        ({"rate": "1350"}, "환율 근거 형식 불일치"),
    ):
        assert fx_usd_krw_from_evidence(bad, now=NOW) == (None, expected)
    weights = compute_weights(
        positions, fx_usd_krw=None, fx_reason="환율 근거 유효기간 경과"
    )
    assert weights[(TOSS_SCOPE, "US", "NVDA")].total_weight is None
    assert weights[(TOSS_SCOPE, "US", "NVDA")].total_reason == "환율 근거 유효기간 경과"


# --- playbook synthesis: structure only, never text -------------------------------------

PLAYBOOK = (
    "---\ninstrument: x\n---\n<!-- HERMES_CURRENT_START -->\n## 1층\n> preamble\n"
    "SECRET-THESIS-MARKER 헤르 판단: 장기 보유 유지, 조정 시 추가 매수 검토\n<!-- HERMES_CURRENT_END -->\n"
    "<!-- USER_THESIS_START -->\n## 2층\n> 안내\n사용자 원문 RAW-USER-LINE 계속 보유 유지\n<!-- USER_THESIS_END -->\n"
    '## 3층 — 매매이력\n<a id="entry-1"></a>\n- `trade_summary`: `SELL 2 @ 100`\n'
    '<a id="entry-0"></a>\n- `trade_summary`: `BUY 4 @ 90`\n'
)


def test_weight_quote_gate_names_null_versus_stale_provider_timestamp_and_never_uses_fetch_time():
    """Production RCA 2026-09-04: personal KR holding 442580 carried price_reason
    '시세 관측시각 오래됨' so the KR denominator was incomplete. The gate judges the
    provider observation time only; a null provider timestamp and an old one are
    distinct, evidence-bearing labels; the fail-closed rule is unchanged (no price
    is ever invented, the limit stays WEIGHT_QUOTE_MAX_AGE)."""
    tool = card_tests._tool()
    fresh = QuoteContextV1(
        Decimal(96645), "KRW", NOW - timedelta(days=6, hours=23), "최근 시세 스토어"
    )
    assert tool._weight_quote(fresh, None, "KR", NOW) == (Decimal(96645), None)
    at_limit = QuoteContextV1(
        Decimal(96645), "KRW", NOW - tool.WEIGHT_QUOTE_MAX_AGE, "최근 시세 스토어"
    )
    assert tool._weight_quote(at_limit, None, "KR", NOW) == (Decimal(96645), None)
    stale = QuoteContextV1(
        Decimal(96645), "KRW", NOW - timedelta(days=7, seconds=1), "최근 시세 스토어"
    )
    assert tool._weight_quote(stale, None, "KR", NOW) == (
        None,
        "시세 관측시각 오래됨 (관측시각 2026-08-27 · 기준 7일)",
    )
    null_ts = QuoteContextV1(Decimal(96645), "KRW", None, "최근 시세 스토어")
    assert tool._weight_quote(null_ts, None, "KR", NOW) == (
        None,
        "시세 관측시각 없음 (제공자 timestamp null)",
    )
    wrong_ccy = QuoteContextV1(Decimal(96645), "USD", NOW, "최근 시세 스토어")
    assert tool._weight_quote(wrong_ccy, None, "KR", NOW) == (None, "시세 통화 불일치")
    assert tool._weight_quote(None, "최근 시세 스토어에 종목 없음", "KR", NOW) == (
        None,
        "최근 시세 스토어에 종목 없음",
    )
    # the reason flows unchanged into both weights and the denominator of the sibling fails closed
    positions = (
        _position("000660", "KR", "4", "1641000"),
        _position(
            "442580",
            "KR",
            "1",
            None,
            reason=tool._weight_quote(null_ts, None, "KR", NOW)[1],
        ),
    )
    weights = compute_weights(positions, fx_usd_krw=None, fx_reason=None)
    assert (
        weights[(TOSS_SCOPE, "KR", "442580")].market_reason
        == "시세 관측시각 없음 (제공자 timestamp null)"
    )
    assert (
        weights[(TOSS_SCOPE, "KR", "442580")].total_reason
        == "시세 관측시각 없음 (제공자 timestamp null)"
    )
    assert weights[(TOSS_SCOPE, "KR", "000660")].market_weight is None
    assert (
        weights[(TOSS_SCOPE, "KR", "000660")].market_reason
        == "같은 시장 보유 1종목 시세 미확인"
    )


def test_controller_stale_and_null_provider_timestamps_reach_the_card_with_distinct_labels(
    tmp_path, capsys
):
    tool = card_tests._tool()
    root = tmp_path / "reports"
    card_tests._store_report(
        root,
        "KR",
        (
            _card("000660", "KR", "SK하이닉스", VALUES_CALM),
            _card("442580", "KR", "글로벌HBM", VALUES_CALM),
            _card("000002", "KR", "평온산업", VALUES_CALM),
        ),
        "a",
    )
    holdings_path = tmp_path / "current_holdings.json"
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                TOSS_SCOPE,
                1,
                (
                    HoldingItemV1("000660", "4", "1000000", "KRW", "KR", "market"),
                    HoldingItemV1("442580", "1", "90000", "KRW", "KR", "market"),
                    HoldingItemV1("000002", "10", "50000", "KRW", "KR", "market"),
                ),
            ),
        ),
        True,
        NOW,
        {TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, None, 1, None)},
    )
    CurrentHoldingsStore(holdings_path, clock=lambda: NOW).replace(
        state, identity_fingerprints={TOSS_SCOPE: "f" * 64}
    )
    quotes_path = tmp_path / "latest_quotes.sqlite3"
    receipt = LatestQuoteStore(quotes_path, clock=lambda: NOW).save_batch(
        (
            _fact("000660", "1641000", "KRW", NOW - timedelta(hours=1)),
            _fact(
                "442580", "96645", "KRW", NOW - timedelta(days=30)
            ),  # provider observation too old
            _fact("000002", "60000", "KRW", None),
        )
    )  # provider timestamp null
    assert receipt.status != "FAILED"
    paths = dict(
        holdings_path=holdings_path,
        quotes_path=quotes_path,
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "refs.sqlite3",
    )
    code = tool.main(
        ["preview", "--report-root", str(root), "--text"], clock=lambda: NOW, **paths
    )
    captured = capsys.readouterr().out
    assert code == tool.EXIT_OK
    text = captured.split("===== KR part 1/1 =====", 1)[1]
    out = json.loads(captured.strip().split("\n")[-1])
    assert out["reports"]["evidence"]["holdings"] == {
        "available": True,
        "reason": None,
        "positions": 3,
        "priced": 1,
    }
    hynix = text.split("• SK하이닉스(000660)", 1)[1].split("• 글로벌HBM(442580)", 1)[0]
    assert "비중 : 국내 산출 불가\n같은 시장 보유 2종목 시세 미확인\n" in hynix
    hbm = text.split("• 글로벌HBM(442580)", 1)[1].split("• 평온산업(000002)", 1)[0]
    # the exact RCA reason is kept verbatim; it is only broken at its own " · " into phone-width lines
    assert (
        "비중 : 국내 산출 불가\n시세 관측시각 오래됨 (관측시각 2026-08-04\n기준 7일)\n"
        in hbm
    )
    calm = text.split("• 평온산업(000002)", 1)[1].split("• SK하이닉스(000660)", 1)[
        0
    ]  # cards sort by symbol
    assert "비중 : 국내 산출 불가\n시세 관측시각 없음 (제공자 timestamp null)\n" in calm
    assert [
        "442580:비중",
        "시세 관측시각 오래됨 (관측시각 2026-08-04 · 기준 7일)",
    ] in out["messages"][0]["unavailable_sections"]
    for card_text in (hynix, hbm, calm):
        assert (
            card_tests._max_cells(
                card_text.split("보유 현황", 1)[1]
                .split("=====", 1)[0]
                .split("\n{", 1)[0]
            )
            <= 44
        )
    # the account-total line is omitted from the card; its exact reason survives in the receipt only
    assert "계좌 전체 비중" not in text.split("\n{", 1)[0] and text.count(
        "비중 : 국내 산출 불가"
    ) == 3 == text.count("비중 : ")
    unavailable = out["messages"][0]["unavailable_sections"]
    assert ["000660:계좌 전체 비중", "계좌 보유 종목 일부 시세 미확인"] in unavailable
    assert [
        "442580:계좌 전체 비중",
        "시세 관측시각 오래됨 (관측시각 2026-08-04 · 기준 7일)",
    ] in unavailable
    assert "96645" not in hynix  # nothing invented for the neighbour


# --- entry zone unavailable detail ------------------------------------------------

SK_HYNIX_0904 = {
    "ret_5": "0.02",
    "ret_20": "-0.03",
    "ret_120": "0.9",
    "close_to_sma_20": "-0.0196",
    "close_to_sma_60": "-0.1965",
    "close_to_sma_120": "0.1",
    "rsi_14": "50.58",
    "realized_vol_63": "0.4",
    "max_drawdown": "-0.2",
    "risk_adjusted_momentum": "0.8",
}


def test_entry_zone_condition_checks_reproduce_the_trend_pullback_contract_from_card_factors():
    """Production RCA 2026-09-04: SK Hynix entry_zone=None with close_to_sma20=-1.96%,
    close_to_sma60=-19.65%, RSI=50.58 — close>SMA20 and SMA20>SMA60 fail, RSI passes."""
    checks = entry_zone_condition_checks(
        _card("000660", "KR", "SK하이닉스", SK_HYNIX_0904)
    )
    assert checks == (
        ("종가>SMA20", False, "20일선 대비 -2.0%"),
        ("SMA20>SMA60", False, "60일선 대비 -19.7% vs 20일선 대비 -2.0%"),
        ("RSI14 45~65", True, "RSI14 50.6"),
    )
    # overheated uptrend: both SMA conditions hold (close/SMA60 above close/SMA20 ⇔ SMA20 > SMA60), RSI fails
    hot = entry_zone_condition_checks(_card("000001", "KR", "테스트전자", VALUES_HOT))
    assert [(label, passed) for label, passed, _ in hot] == [
        ("종가>SMA20", True),
        ("SMA20>SMA60", True),
        ("RSI14 45~65", False),
    ]
    # below SMA20 but SMA20 still above SMA60 (close_to_sma_60 < close_to_sma_20 fails, > passes)
    dip = entry_zone_condition_checks(
        _card(
            "000001",
            "KR",
            "x",
            {**VALUES_CALM, "close_to_sma_20": "-0.01", "close_to_sma_60": "0.03"},
        )
    )
    assert [(label, passed) for label, passed, _ in dip] == [
        ("종가>SMA20", False),
        ("SMA20>SMA60", True),
        ("RSI14 45~65", True),
    ]
    # boundary RSI values are inclusive, exactly as build_reference_entry_zone
    for rsi, passed in (("45", True), ("65", True), ("44.99", False), ("65.01", False)):
        checks = entry_zone_condition_checks(
            _card("000001", "KR", "x", {**VALUES_CALM, "rsi_14": rsi})
        )
        assert checks[2][1] is passed, rsi
    # an unavailable factor is 판정 불가, never guessed
    missing = entry_zone_condition_checks(
        _card("000001", "KR", "x", {**VALUES_CALM, "close_to_sma_60": None})
    )
    assert missing[0][1] is True and missing[1] == (
        "SMA20>SMA60",
        None,
        "20일선/60일선 대비 지표 없음",
    )


@pytest.mark.parametrize(
    "values,required,absent",
    [
        (
            SK_HYNIX_0904,
            ("현재 추세눌림 조건은 충족하지 않습니다.", "-2.0%", "-19.7%"),
            ("자료가 부족",),
        ),
        (
            VALUES_CALM,
            ("기본 지표 조건은 충족하지만", "이유는", "확인되지 않습니다."),
            ("미성립", "시계열 미확보"),
        ),
        (
            {"ret_120": "0.1"},
            ("추세눌림 판단 자료가 부족합니다.", "20일선 대비 지표 없음"),
            ("조건은 충족하지",),
        ),
        (
            {**VALUES_CALM, "close_to_sma_20": "-0.02", "close_to_sma_60": None},
            (
                "현재 추세눌림 조건은 충족하지 않습니다.",
                "추세눌림 판단 자료가 부족합니다.",
                "-2.0%",
            ),
            ("지표 연결됨",),
        ),
    ],
)
def test_zone_unavailable_lines_state_failed_conditions_with_actual_values_and_never_a_zone(
    values, required, absent
):
    card = _card("000660", "KR", "SYNTHETIC", values)
    text = render_market_message(
        _report("KR", (card,)), (CardContextV1(card.symbol),), report_sha256="d" * 64
    ).parts[0]
    for phrase in required:
        assert phrase in text.replace("\n", " ")
    assert all(phrase not in text for phrase in absent)
    assert "매수 확인 신호는 아직 계산하지 않습니다." in text.replace("\n", " ")
    assert "2026-09-02" in text and card_tests._max_cells(text) <= 44


def test_playbook_synthesis_carries_structure_and_stance_but_no_text():
    synthesis = synthesize_playbook(PLAYBOOK, scope_label="개인계좌")
    assert synthesis == PlaybookSynthesisV1("개인계좌", True, True, 2, "SELL", "HOLD")
    for value in dataclasses.astuple(synthesis):
        assert (
            "SECRET" not in str(value)
            and "RAW-USER" not in str(value)
            and ".md" not in str(value)
        )
    empty = synthesize_playbook(
        "---\n---\n<!-- HERMES_CURRENT_START -->\n> only preamble\n<!-- HERMES_CURRENT_END -->\n",
        scope_label="개인계좌",
    )
    assert empty == PlaybookSynthesisV1("개인계좌", False, False, 0, None, None)
    assert classify_stance("추가 매수 후 비중 확대") == "ACCUMULATE"
    assert classify_stance("전량 매도 정리") == "REDUCE"
    assert (
        classify_stance("추가 매수 또는 전량 매도") is None
    )  # tie → unclassified, never guessed


def test_playbook_agreement_matrix_and_rendered_synthesis_has_no_raw_text_or_path():
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "ACCUMULATE"), "down"
        )[0]
        == "상충"
    )
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "REDUCE"), "up"
        )[0]
        == "상충"
    )
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "HOLD"), "down"
        )[0]
        == "주의"
    )
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "REDUCE"), "down"
        )[0]
        == "일치"
    )
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", None), "up"
        )[0]
        == "대조 불가"
    )
    assert (
        playbook_agreement(
            PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "HOLD"), "mixed"
        )[0]
        == "대조 보류"
    )
    assert playbook_agreement(None, "up") == (
        "해당 없음",
        "플레이북 없음",
        "플레이북 없이 Quant 근거만으로 판단",
    )
    synthesis = synthesize_playbook(PLAYBOOK, scope_label="개인계좌")
    down = dict(VALUES_HOT, ret_120="-0.3", close_to_sma_120="-0.1")
    context = CardContextV1(
        "000001",
        None,
        "x",
        (),
        True,
        False,
        synthesis,
        None,
        ConditionalContextV1("NONE"),
    )
    text = render_market_message(
        _report("KR", (_card("000001", "KR", "테스트전자", down),)),
        (context,),
        report_sha256="d" * 64,
    ).parts[0]
    assert "플레이북\n" not in text and "손절 규칙" not in text
    for leak in (
        "SECRET-THESIS-MARKER",
        "RAW-USER-LINE",
        "장기 보유 유지, 조정",
        "personal/KR",
        ".md",
        "trade_summary",
        "있음",
        "플레이북 - ",
    ):
        assert leak not in text
    # absence is printed per layer; presence never is
    partial = PlaybookSynthesisV1("개인계좌", False, False, 0, None, None)
    text = render_market_message(
        _report("KR", (_card("000001", "KR", "테스트전자", down),)),
        (dataclasses.replace(context, playbook=partial),),
        report_sha256="d" * 64,
    ).parts[0]
    assert "플레이북\n" not in text and "없음" not in text.split("어떻게 대응하나")[0]


# --- conditional orders: bounded read, never inferred -----------------------------------


def _holdings(scopes=(TOSS_SCOPE,), *, symbol="000001"):
    accounts = tuple(
        AccountHoldingsV1(
            scope,
            index,
            (HoldingItemV1(symbol, "4", "1900000", "KRW", "KR", "market"),),
        )
        for index, scope in enumerate(scopes, 1)
    )
    return CurrentHoldingsV1(
        1,
        accounts,
        True,
        NOW,
        {
            scope: AccountScopeIdentityV1(scope, None, index, None)
            for index, scope in enumerate(scopes, 1)
        },
    )


class _Refs:
    def __init__(self, mapping):
        self.mapping = mapping

    def scopes(self):
        return frozenset(self.mapping)

    def reveal(self, scope):
        return self.mapping.get(scope)


def _leg(
    status="WATCHING",
    trigger="2300000",
    price="2290000",
    kind="STOP_LOSS",
    side=MISSING,
):
    return ConditionalOrderCondition(
        type=kind,
        status=status,
        trigger_price=trigger,
        target_profit_rate=None,
        order_price=price,
        triggered_order_id=None,
        order_side=side,
    )


def _order(
    symbol="000001",
    market="KR",
    *,
    first=None,
    second=None,
    order_kind="SINGLE",
    status="WATCHING",
    expire=date(2026, 9, 30),
    coid="c-1",
):
    # ``status`` is the provider's top-level ORDER status (WATCHING/PAUSED/ORDERING/ORDERED |
    # COMPLETED/EXPIRED); the request-side group filter "OPEN" is never an order status
    return ConditionalOrderDetailResponse(
        conditional_order_id=coid,
        type=order_kind,
        status=status,
        symbol=symbol,
        market=market,
        quantity="2",
        order_type="LIMIT",
        first=first or _leg(),
        created_at=NOW,
        expire_date=expire,
        second=second,
    )


def test_conditional_side_uses_leg_field_and_documented_group_semantics():
    def with_top_side(order, side):
        return SimpleNamespace(
            **{
                field.name: getattr(order, field.name)
                for field in dataclasses.fields(order)
            },
            side=side,
        )

    orders = (
        _order(coid="single-sell", first=_leg(side="SELL")),
        _order(coid="single-buy", first=_leg(side="BUY")),
        _order(coid="single-unknown", first=_leg()),
        with_top_side(
            _order(
                coid="oco",
                order_kind="OCO",
                first=_leg(trigger="300"),
                second=_leg(trigger="200"),
            ),
            "BUY",
        ),
        with_top_side(
            _order(
                coid="oto",
                order_kind="OTO",
                first=_leg(trigger="100"),
                second=_leg(trigger="200"),
            ),
            "BUY",
        ),
        _order(
            coid="oto-explicit",
            order_kind="OTO",
            first=_leg(side="SELL"),
            second=_leg(side="BUY"),
        ),
    )
    scan = ConditionalScanV1(
        (ScopeConditionalScanV1(TOSS_SCOPE, "ok", None, orders),), 1
    )
    context = conditional_context_for(
        scan, market="KR", symbol="000001", scope_label=LABEL
    )
    assert [order.side for order in context.orders] == [
        "SELL",
        "BUY",
        None,
        "SELL",
        "SELL",
        "BUY",
        "SELL",
        "SELL",
        "BUY",
    ]

    malformed = _order(coid="bad-oco", order_kind="OCO", second=None)
    result = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch({TOSS_SCOPE: _page((malformed,))}),
    )
    assert result.scopes[0].status == "unavailable"
    assert result.scopes[0].reason == "order_detail_incomplete"


def test_single_with_second_leg_marks_scope_unavailable():
    malformed = _order(
        coid="bad-single-second",
        order_kind="SINGLE",
        first=_leg(side="BUY"),
        second=_leg(side="SELL"),
    )
    result = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch({TOSS_SCOPE: _page((malformed,))}),
    )

    assert result.scopes[0].status == "unavailable"
    assert result.scopes[0].reason == "order_detail_incomplete"


def test_quant_uses_the_shared_facts_side_resolver():
    from tokmo.facts import toss_reads as facts_reads
    from tokmo.programs import quant_card_conditional as quant_conditional

    assert (
        quant_conditional.resolve_conditional_order_side
        is facts_reads.resolve_conditional_order_side
    )


class _Fetch:
    def __init__(self, pages):
        self.pages = dict(pages)
        self.requests = []

    def __call__(self, *, account_scope, native_ref, status, cursor, limit):
        self.requests.append(
            {
                "scope": account_scope,
                "ref": native_ref,
                "status": status,
                "cursor": cursor,
                "limit": limit,
            }
        )
        page = self.pages[account_scope]
        if isinstance(page, Exception):
            raise page
        return page


def _page(orders=(), has_next=False, code="success"):
    from tokmo.programs.conditional_detection import PulseConditionalPageOutcomeV1

    return PulseConditionalPageOutcomeV1(
        code, tuple(orders), "cur" if has_next else None, has_next
    )


LABEL = lambda scope: "개인계좌"


def test_conditional_read_is_one_open_page_per_mapped_account_limit_100_retry_0_write_0():
    fetch = _Fetch({TOSS_SCOPE: _page((_order(),)), TOSS_SCOPE_2: RuntimeError("boom")})
    scan = collect_open_conditional_orders(
        holdings=_holdings((TOSS_SCOPE, TOSS_SCOPE_2, PENSION_SCOPE)),
        refs=_Refs({TOSS_SCOPE: "ref-1", TOSS_SCOPE_2: "ref-2"}),
        fetch=fetch,
    )
    assert (
        scan.provider_calls == 2 and len(fetch.requests) == 2
    )  # retry 0: the failing scope was called once
    assert all(
        r["status"] == "OPEN"
        and r["cursor"] is None
        and r["limit"] == CONDITIONAL_PAGE_LIMIT == 100
        for r in fetch.requests
    )
    assert [r["scope"] for r in fetch.requests] == [TOSS_SCOPE, TOSS_SCOPE_2]
    assert dict(scan.writes) == {
        "order": 0,
        "account": 0,
        "ledger": 0,
        "provider_payload_persisted": 0,
    }
    by_scope = {item.account_scope: item for item in scan.scopes}
    assert (
        by_scope[TOSS_SCOPE].status == "ok"
        and by_scope[TOSS_SCOPE_2].reason == "provider_failure"
    )
    assert (
        PENSION_SCOPE not in by_scope
    )  # pension/excel scopes are not Toss conditional-order accounts
    context = conditional_context_for(
        scan, market="KR", symbol="000001", scope_label=LABEL
    )
    assert context.status == "ACTIVE" and len(context.orders) == 1
    assert (
        context.orders[0].trigger_price == Decimal(2300000)
        and context.orders[0].expire_date == "2026-09-30"
    )
    assert context.reason == "일부 계좌 확인 불가: 개인계좌 제공자 호출 실패"
    # a different symbol: not NONE while any account is unread
    other = conditional_context_for(
        scan, market="KR", symbol="000002", scope_label=LABEL
    )
    assert other.status == "UNAVAILABLE" and other.reason == "개인계좌 제공자 호출 실패"


def test_conditional_absence_is_only_claimed_after_a_complete_read():
    complete = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch({TOSS_SCOPE: _page(())}),
    )
    assert conditional_context_for(
        complete, market="KR", symbol="000001", scope_label=LABEL
    ) == ConditionalContextV1("NONE")
    truncated = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch({TOSS_SCOPE: _page((), has_next=True)}),
    )
    assert (
        conditional_context_for(
            truncated, market="KR", symbol="000001", scope_label=LABEL
        ).reason
        == "개인계좌 목록 초과(hasNext)"
    )
    failed = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch({TOSS_SCOPE: _page(code="auth_failed")}),
    )
    assert (
        conditional_context_for(
            failed, market="KR", symbol="000001", scope_label=LABEL
        ).reason
        == "개인계좌 제공자 응답 실패(auth_failed)"
    )
    fetch = _Fetch({})
    unmapped = collect_open_conditional_orders(
        holdings=_holdings(), refs=_Refs({}), fetch=fetch
    )
    assert fetch.requests == [] and unmapped.provider_calls == 0
    assert (
        conditional_context_for(
            unmapped, market="KR", symbol="000001", scope_label=LABEL
        ).reason
        == "개인계좌 계좌 미매핑"
    )
    no_holdings = collect_open_conditional_orders(
        holdings=None, refs=_Refs({}), fetch=fetch
    )
    assert (
        conditional_context_for(
            no_holdings, market="KR", symbol="000001", scope_label=LABEL
        ).status
        == "UNAVAILABLE"
    )
    closed_only = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch(
            {
                TOSS_SCOPE: _page(
                    (_order(status="COMPLETED"), _order(status="EXPIRED", coid="c-2"))
                )
            }
        ),
    )
    assert (
        conditional_context_for(
            closed_only, market="KR", symbol="000001", scope_label=LABEL
        ).status
        == "NONE"
    )


def test_two_sk_hynix_watching_orders_are_both_active_and_closed_statuses_are_excluded():
    """Production RCA 2026-09-04: status=OPEN page carried two KR/000660 orders, each
    status=WATCHING · SINGLE · LIMIT; both were discarded by comparing the order status
    against the request group filter "OPEN". Every OPEN-group status is active; the
    CLOSED-group statuses never are; the request filter itself stays "OPEN"."""
    hynix = (
        _order("000660", first=_leg(trigger="1500000", price="1490000"), coid="h-1"),
        _order(
            "000660",
            first=_leg(trigger="1800000", price="1790000", kind="TAKE_PROFIT"),
            coid="h-2",
        ),
    )
    fetch = _Fetch(
        {
            TOSS_SCOPE: _page(
                hynix
                + (
                    _order(
                        "000660",
                        status="COMPLETED",
                        first=_leg(trigger="1200000"),
                        coid="h-done",
                    ),
                    _order(
                        "000660",
                        status="EXPIRED",
                        first=_leg(trigger="1100000"),
                        coid="h-expired",
                    ),
                    _order("005930", status="WATCHING", coid="s-1"),
                )
            )
        }
    )
    scan = collect_open_conditional_orders(
        holdings=_holdings(), refs=_Refs({TOSS_SCOPE: "ref-1"}), fetch=fetch
    )
    assert (
        scan.provider_calls == 1 and fetch.requests[0]["status"] == "OPEN"
    )  # group filter unchanged
    assert (
        fetch.requests[0]["cursor"] is None
        and fetch.requests[0]["limit"] == CONDITIONAL_PAGE_LIMIT
    )
    assert dict(scan.writes) == {
        "order": 0,
        "account": 0,
        "ledger": 0,
        "provider_payload_persisted": 0,
    }
    context = conditional_context_for(
        scan, market="KR", symbol="000660", scope_label=LABEL
    )
    assert (
        context.status == "ACTIVE"
        and context.reason is None
        and len(context.orders) == 2
    )
    assert [(o.leg_type, o.leg_status, o.trigger_price) for o in context.orders] == [
        ("STOP_LOSS", "WATCHING", Decimal(1500000)),
        ("TAKE_PROFIT", "WATCHING", Decimal(1800000)),
    ]
    assert all(
        o.order_kind == "SINGLE" and o.order_type == "LIMIT" and o.side is None
        for o in context.orders
    )
    # every OPEN-group status is active, one at a time
    for status in ("PAUSED", "ORDERING", "ORDERED"):
        one = collect_open_conditional_orders(
            holdings=_holdings(),
            refs=_Refs({TOSS_SCOPE: "ref-1"}),
            fetch=_Fetch({TOSS_SCOPE: _page((_order("000660", status=status),))}),
        )
        assert (
            conditional_context_for(
                one, market="KR", symbol="000660", scope_label=LABEL
            ).status
            == "ACTIVE"
        ), status
    # the closed statuses alone → NONE after a complete read, never ACTIVE
    for status in ("COMPLETED", "EXPIRED"):
        closed = collect_open_conditional_orders(
            holdings=_holdings(),
            refs=_Refs({TOSS_SCOPE: "ref-1"}),
            fetch=_Fetch({TOSS_SCOPE: _page((_order("000660", status=status),))}),
        )
        assert (
            conditional_context_for(
                closed, market="KR", symbol="000660", scope_label=LABEL
            ).status
            == "NONE"
        ), status
    # a status outside the provider vocabulary (including the group filter word itself or a
    # leg-only state) is neither active nor absent: the scope fails closed as unread
    for status in ("OPEN", "CANCELED", "HOLDING", "TRIGGERED"):
        drift = collect_open_conditional_orders(
            holdings=_holdings(),
            refs=_Refs({TOSS_SCOPE: "ref-1"}),
            fetch=_Fetch({TOSS_SCOPE: _page((_order("000660", status=status),))}),
        )
        assert drift.scopes[0].reason == "order_status_unknown", status
        result = conditional_context_for(
            drift, market="KR", symbol="000660", scope_label=LABEL
        )
        assert (
            result.status == "UNAVAILABLE"
            and result.reason == "개인계좌 주문 상태 어휘 미인식"
        )
        assert status not in result.reason


def test_two_active_orders_render_as_two_entries_for_the_same_symbol():
    quote = QuoteContextV1(
        Decimal(1641000),
        "KRW",
        datetime(2026, 9, 4, 9, 39, tzinfo=KST),
        "최근 시세 스토어",
    )
    scan = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch(
            {
                TOSS_SCOPE: _page(
                    (
                        _order(
                            "000660",
                            first=_leg(trigger="1500000", price="1490000"),
                            coid="h-1",
                        ),
                        _order(
                            "000660",
                            first=_leg(trigger="1800000", price="1790000"),
                            coid="h-2",
                        ),
                    )
                )
            }
        ),
    )
    context = conditional_context_for(
        scan, market="KR", symbol="000660", scope_label=LABEL
    )
    card_context = CardContextV1(
        "000660", quote, None, (), True, False, None, None, context
    )
    message = render_market_message(
        _report("KR", (_card("000660", "KR", "SK하이닉스", VALUES_CALM),)),
        (card_context,),
        report_sha256="d" * 64,
    )
    text = message.parts[0]
    assert "조건주문 현황" not in text and "거래구분 : 확인 불가" not in text


def test_conditional_rendering_is_compact_and_never_derives_side():
    quote = QuoteContextV1(
        Decimal(3000000),
        "KRW",
        datetime(2026, 9, 3, 10, 0, tzinfo=KST),
        "최근 시세 스토어",
    )
    scan = collect_open_conditional_orders(
        holdings=_holdings(),
        refs=_Refs({TOSS_SCOPE: "ref-1"}),
        fetch=_Fetch(
            {
                TOSS_SCOPE: _page(
                    (
                        _order(first=_leg(trigger="2500000", price=None), expire=None),
                        _order(
                            first=_leg(trigger="2000000", kind="TRAILING_STOP"),
                            coid="c-2",
                        ),
                    )
                )
            }
        ),
    )
    context = conditional_context_for(
        scan, market="KR", symbol="000001", scope_label=LABEL
    )
    card_context = CardContextV1(
        "000001", quote, None, (), True, False, None, None, context
    )
    message = render_market_message(
        _report("KR", (_card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),)),
        (card_context,),
        report_sha256="d" * 64,
    )
    text = message.parts[0]
    assert "조건주문 현황" not in text and "거래구분 : 확인 불가" not in text


def test_pinned_get_excerpt_omits_side_and_legacy_single_top_level_side_is_compatible():
    """The pinned GET excerpt omits side; retain exact top-level side only for
    legacy SINGLE provider objects. Group semantics are tested separately."""
    fixture = (
        Path(__file__).parent / "fixtures" / "toss_remaining_reads_openapi_excerpt.json"
    )
    schemas = json.loads(fixture.read_text(encoding="utf-8"))["components"]["schemas"]
    keys = set(schemas["ConditionalOrderDetailResponse"]["properties"]) | set(
        schemas["ConditionalOrderCondition"]["properties"]
    )
    assert keys == {
        "conditionalOrderId",
        "type",
        "status",
        "symbol",
        "market",
        "quantity",
        "orderType",
        "expireDate",
        "first",
        "second",
        "createdAt",
        "triggerPrice",
        "targetProfitRate",
        "orderPrice",
        "triggeredOrderId",
    }
    assert not any(
        hint in key.lower() for key in keys for hint in ("side", "buy", "sell")
    )
    assert "side" not in {
        field.name for field in dataclasses.fields(ConditionalOrderDetailResponse)
    }

    def sided(value):
        base = _order()
        return SimpleNamespace(
            **{
                field.name: getattr(base, field.name)
                for field in dataclasses.fields(base)
            },
            side=value,
        )

    for value, expected in (
        ("SELL", "SELL"),
        ("BUY", "BUY"),
        ("sell", None),
        ("LONG", None),
        (None, None),
    ):
        scan = collect_open_conditional_orders(
            holdings=_holdings(),
            refs=_Refs({TOSS_SCOPE: "ref-1"}),
            fetch=_Fetch({TOSS_SCOPE: _page((sided(value),))}),
        )
        context = conditional_context_for(
            scan, market="KR", symbol="000001", scope_label=LABEL
        )
        assert context.status == "ACTIVE" and context.orders[0].side == expected, value


# --- formatting stability -------------------------------------------------------------


def test_formatting_is_stable_single_spaced_and_under_cap_for_full_context_cards():
    contexts = card_tests._kr_contexts()
    report = card_tests._kr_report()
    renders = {
        render_market_message(report, contexts, report_sha256="d" * 64)
        for _ in range(3)
    }
    assert len(renders) == 1
    text = renders.pop().parts[0]
    assert "\t" not in text
    assert len(text.encode("utf-16-le")) // 2 <= MESSAGE_CHAR_CAP
    lines = text.split("\n")
    index = lines.index("수익률")
    assert lines[index + 1] == "```"
    labels, values = lines[index + 2 : index + 4]
    assert lines[index + 4] == "```"

    def centers(line):
        result, visual_cursor, raw_cursor = [], 0, 0
        for token in line.split():
            start = line.index(token, raw_cursor)
            visual_cursor += sum(
                2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1
                for c in line[raw_cursor:start]
            )
            width = sum(
                2 if unicodedata.east_asian_width(c) in {"W", "F"} else 1 for c in token
            )
            result.append((token, visual_cursor + width / 2))
            raw_cursor = start + len(token)
            visual_cursor += width
        return result

    assert [token for token in labels.split() if token == "/"] == ["/", "/"]
    assert [token for token in values.split() if token == "/"] == ["/", "/"]
    label_cells = [item for item in centers(labels) if item[0] != "/"]
    value_cells = [item for item in centers(values) if item[0] != "/"]
    assert [item[0] for item in label_cells] == ["5일", "20일", "120일"]
    assert [item[0] for item in value_cells] == ["+18.2%", "+44.7%", "+165.0%"]
    # each label is centred over its value; odd/even widths allow at most a half cell
    assert all(
        abs(label[1] - value[1]) <= 0.5
        for label, value in zip(label_cells, value_cells)
    )
    slash_labels = [item for item in centers(labels) if item[0] == "/"]
    slash_values = [item for item in centers(values) if item[0] == "/"]
    assert [item[1] for item in slash_labels] == [
        item[1] for item in slash_values
    ]  # separators line up


# --- end to end through the controller: canonical stores, injected read seam -------------


def _fact(symbol, price, currency, timestamp):
    return TossCurrentPriceFact(
        source="toss-current-price-v1",
        symbol=symbol,
        last_price=price,
        currency_raw=currency,
        timestamp=timestamp,
        fetched_at=NOW,
    )


def test_controller_wires_weights_synthesis_and_conditional_read_with_evidence(
    tmp_path, capsys
):
    tool = card_tests._tool()
    root = tmp_path / "reports"
    card_tests._store_report(
        root,
        "KR",
        (
            _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
            _card("000002", "KR", "평온산업", VALUES_CALM),
        ),
        "a",
    )
    holdings_path = tmp_path / "current_holdings.json"
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                TOSS_SCOPE,
                1,
                (
                    HoldingItemV1("000001", "4", "1900000", "KRW", "KR", "market"),
                    HoldingItemV1("000002", "10", "50000", "KRW", "KR", "market"),
                    HoldingItemV1("NVDA", "3", "100", "USD", "US", "market"),
                ),
            ),
        ),
        True,
        NOW,
        {TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, None, 1, None)},
    )
    CurrentHoldingsStore(holdings_path, clock=lambda: NOW).replace(
        state, identity_fingerprints={TOSS_SCOPE: "f" * 64}
    )
    quotes_path = tmp_path / "latest_quotes.sqlite3"
    receipt = LatestQuoteStore(quotes_path, clock=lambda: NOW).save_batch(
        (
            _fact("000001", "3000000", "KRW", NOW - timedelta(hours=1)),
            _fact(
                "000002", "60000", "KRW", NOW - timedelta(days=30)
            ),  # stale: no weight may use it
            _fact("NVDA", "120", "USD", NOW - timedelta(hours=1)),
        )
    )
    assert receipt.status != "FAILED"
    refs_path = tmp_path / "account_native_refs.sqlite3"
    AccountNativeRefStoreV1(refs_path, clock=lambda: NOW).replace_refs(
        {TOSS_SCOPE: "native-secret-ref"}, receipt_binding="rb-1"
    )
    playbook = tmp_path / "playbooks" / "personal" / "KR" / "000001.md"
    playbook.parent.mkdir(parents=True)
    playbook.write_text(PLAYBOOK, encoding="utf-8")
    fetch = _Fetch({TOSS_SCOPE: _page((_order(),))})
    paths = dict(
        holdings_path=holdings_path,
        quotes_path=quotes_path,
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=refs_path,
    )
    code = tool.main(
        ["preview", "--report-root", str(root), "--text"],
        clock=lambda: NOW,
        conditional_fetch=fetch,
        **paths,
    )
    captured = capsys.readouterr().out
    out = json.loads(captured.strip().split("\n")[-1])
    assert code == tool.EXIT_OK
    text = captured.split("===== KR part 1/1 =====", 1)[1]
    assert fetch.requests == [
        {
            "scope": TOSS_SCOPE,
            "ref": "native-secret-ref",
            "status": "OPEN",
            "cursor": None,
            "limit": 100,
        }
    ]
    evidence = out["reports"]["evidence"]
    assert evidence["conditional"] == {
        "performed": True,
        "provider_calls": 1,
        "page_limit": 100,
        "retry": 0,
        "scopes": {"개인계좌#1": {"status": "ok", "reason": None, "open_orders": 1}},
        "writes": {
            "order": 0,
            "account": 0,
            "ledger": 0,
            "provider_payload_persisted": 0,
        },
    }
    assert evidence["holdings"] == {
        "available": True,
        "reason": None,
        "positions": 3,
        "priced": 2,
    }
    assert evidence["fx"] == {
        "rate_available": False,
        "reason": "USD/KRW 환율 근거 없음",
    }
    # weights: the KR denominator is incomplete because 000002's quote is stale → honest unavailable
    block = text.split("• 테스트전자(000001)", 1)[1].split("• 평온산업(000002)", 1)[0]
    assert (
        "보유 : 4주 / 평단 : 1,900,000원\n비중 : 국내 산출 불가\n같은 시장 보유 1종목 시세 미확인\n"
        in block
    )
    assert ["000001:계좌 전체 비중", "계좌 보유 종목 일부 시세 미확인"] in out[
        "messages"
    ][0]["unavailable_sections"]
    assert "조건주문 현황" not in block
    assert block.index("어떻게 대응하나") < block.index("보유 현황")
    assert "플레이북\n" not in block
    assert (
        text.startswith("\n📊 퀀트 KR\n2026-09-02\n카드 2종목\n\n")
        and "가격 관측 " in text
        and "최근 시세 스토어" not in text
    )
    block2 = text.split("• 평온산업(000002)", 1)[1]
    assert (
        "보유 : 10주 / 평단 : 50,000원\n비중 : 국내 산출 불가\n시세 관측시각 오래됨"
        in block2
    )
    assert "조건주문 현황" not in block2
    for leak in (
        "native-secret-ref",
        "SECRET-THESIS-MARKER",
        "RAW-USER-LINE",
        "personal/KR/000001.md",
        "c-1",
        TOSS_SCOPE,
    ):
        assert leak not in text and leak not in json.dumps(out)
    assert "계좌 전체 비중" not in text.split("\n{", 1)[0]
    assert (
        "%" not in block.split("보유 현황", 1)[1].split("\n\n", 1)[0]
    )  # no weight claimed that was not computed
    # fresh quote for 000002 → both KR weights compute over the (personal KR stock) denominator
    LatestQuoteStore(quotes_path, clock=lambda: NOW).save_batch(
        (_fact("000002", "60000", "KRW", NOW - timedelta(hours=1)),)
    )
    code = tool.main(
        ["preview", "--report-root", str(root), "--text"],
        clock=lambda: NOW,
        conditional_fetch=fetch,
        **paths,
    )
    text = capsys.readouterr().out
    assert (
        "비중 : 국내 95.2%\n" in text and "비중 : 국내 4.8%\n" in text
    )  # 12,000,000 / 12,600,000
    assert (
        "계좌 전체 비중" not in text.split("\n{", 1)[0] and "계좌 주식 합산" not in text
    )  # no FX: total omitted, never invented
    # without an injected seam and without --conditional-live the read is not performed and says so
    code = tool.main(
        ["preview", "--report-root", str(root), "--text"], clock=lambda: NOW, **paths
    )
    text = capsys.readouterr().out
    assert "조건주문 현황" not in text and code == tool.EXIT_OK


def test_holdings_section_is_personal_only_and_weight_label_follows_market(
    tmp_path, capsys
):
    """Contract 2026-09-04: Quant holdings are the personal account. A retirement
    (pension Excel) position in the same instrument never mixes into the card, and
    the weight line reads 국내/미국 with no denominator prose."""
    tool = card_tests._tool()
    root = tmp_path / "reports"
    card_tests._store_report(
        root, "KR", (_card("000001", "KR", "테스트전자", VALUES_CALM),), "a"
    )
    card_tests._store_report(
        root, "US", (_card("NVDA", "US", "엔비디아", VALUES_CALM),), "b"
    )
    holdings_path = tmp_path / "current_holdings.json"
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                TOSS_SCOPE,
                1,
                (
                    HoldingItemV1("000001", "4", "1250000", "KRW", "KR", "market"),
                    HoldingItemV1("NVDA", "3", "100", "USD", "US", "market"),
                ),
            ),
            AccountHoldingsV1(
                PENSION_SCOPE,
                2,
                (HoldingItemV1("000001", "10", "900000", "KRW", "KR", "market"),),
            ),
        ),
        True,
        NOW,
        {
            TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, None, 1, None),
            PENSION_SCOPE: AccountScopeIdentityV1(PENSION_SCOPE, None, 2, None),
        },
    )
    CurrentHoldingsStore(holdings_path, clock=lambda: NOW).replace(
        state, identity_fingerprints={TOSS_SCOPE: "f" * 64, PENSION_SCOPE: "e" * 64}
    )
    quotes_path = tmp_path / "latest_quotes.sqlite3"
    LatestQuoteStore(quotes_path, clock=lambda: NOW).save_batch(
        (
            _fact("000001", "1500000", "KRW", NOW - timedelta(hours=1)),
            _fact("NVDA", "120", "USD", NOW - timedelta(hours=1)),
        )
    )
    paths = dict(
        holdings_path=holdings_path,
        quotes_path=quotes_path,
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "refs.sqlite3",
    )
    code = tool.main(
        ["preview", "--report-root", str(root), "--text"], clock=lambda: NOW, **paths
    )
    out = capsys.readouterr().out
    assert code == tool.EXIT_OK
    kr = out.split("===== KR part 1/1 =====", 1)[1].split("===== US part 1/1 =====", 1)[
        0
    ]
    us = out.split("===== US part 1/1 =====", 1)[1].split("\n{", 1)[0]
    assert "보유 현황\n보유 : 4주 / 평단 : 1,250,000원\n비중 : 국내 100.0%\n" in kr
    assert "보유 현황\n보유 : 3주 / 평단 : $100.00\n비중 : 미국 100.0%\n" in us
    for leak in ("10주", "900,000", "퇴직연금", "계좌 전체 비중", "계좌 주식 합산"):
        assert leak not in kr and leak not in us, leak
    assert (
        json.loads(out.strip().split("\n")[-1])["reports"]["evidence"]["holdings"][
            "positions"
        ]
        == 3
    )


def test_total_weight_names_the_concrete_fx_evidence_defect_never_a_generic_unwired_label(
    tmp_path, capsys
):
    """Cross-market total weight needs valid Toss USD/KRW evidence; when it is
    missing or invalid the card states THAT reason (never '환산 집계 미연결')."""
    tool = card_tests._tool()
    root = tmp_path / "reports"
    card_tests._store_report(
        root, "KR", (_card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),), "a"
    )
    holdings_path = tmp_path / "current_holdings.json"
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                TOSS_SCOPE,
                1,
                (
                    HoldingItemV1("000001", "4", "1900000", "KRW", "KR", "market"),
                    HoldingItemV1("NVDA", "3", "100", "USD", "US", "market"),
                ),
            ),
        ),
        True,
        NOW,
        {TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, None, 1, None)},
    )
    CurrentHoldingsStore(holdings_path, clock=lambda: NOW).replace(
        state, identity_fingerprints={TOSS_SCOPE: "f" * 64}
    )
    quotes_path = tmp_path / "latest_quotes.sqlite3"
    LatestQuoteStore(quotes_path, clock=lambda: NOW).save_batch(
        (
            _fact("000001", "3000000", "KRW", NOW - timedelta(hours=1)),
            _fact("NVDA", "120", "USD", NOW - timedelta(hours=1)),
        )
    )
    paths = dict(
        holdings_path=holdings_path,
        quotes_path=quotes_path,
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "refs.sqlite3",
    )

    def run(fx_evidence):
        code = tool.main(
            ["preview", "--report-root", str(root), "--text"],
            clock=lambda: NOW,
            fx_evidence=fx_evidence,
            **paths,
        )
        out = capsys.readouterr().out
        assert code == tool.EXIT_OK
        payload = json.loads(out.strip().split("\n")[-1])
        return (
            out,
            payload["reports"]["evidence"]["fx"],
            payload["messages"][0]["unavailable_sections"],
        )

    for fx_evidence, reason in (
        (None, "USD/KRW 환율 근거 없음"),
        (dataclasses.replace(_fx(), content_hash="f" * 64), "환율 근거 해시 불일치"),
        (
            _fx(observed=NOW - timedelta(seconds=FX_MAX_AGE_SECONDS_V1 + 1)),
            "환율 근거 유효기간 경과",
        ),
        (_fx(source="other"), "환율 근거 계약 불일치"),
        ({"rate": "1350"}, "환율 근거 형식 불일치"),
    ):
        text, evidence, unavailable = run(fx_evidence)
        assert "보유 : 4주 / 평단 : 1,900,000원\n비중 : 국내 100.0%" in text
        # the total line is omitted from the card (never invented); the concrete reason stays in the receipt
        assert ["000001:계좌 전체 비중", reason] in unavailable, reason
        assert (
            "계좌 전체 비중" not in text.split("\n{", 1)[0]
            and "계좌 주식 합산" not in text
        )
        assert evidence == {"rate_available": False, "reason": reason}
        assert (
            "환산 집계 미연결" not in text
            and "평가액 미연결" not in text
            and "미연결" not in text.split("투자판단")[0]
        )
    text, evidence, unavailable = run(_fx())
    assert evidence == {"rate_available": True, "reason": None}
    # even with valid FX the KR+US stock ratio (96.1%) is NOT an account total and is never rendered;
    # the card stays market-only and the receipt records nothing as unavailable for it
    assert (
        "비중 : 국내 100.0%" in text
        and "96.1" not in text
        and "계좌 주식 합산" not in text
    )
    assert not any(key == "000001:계좌 전체 비중" for key, _ in unavailable)


# --- contextual decision analysis (SDS 2026-09-03 v0.1): evidence pack, seams, validation ----

from tokmo.programs.quant_contextual_analysis import (
    MAX_FETCHES,
    MAX_QUERIES,
    AnalysisValidationError,
    ContextItemV1,
    build_evidence_pack,
    gather_external_evidence,
    run_contextual_analysis,
    validate_analysis,
)
from tokmo.programs.quant_telegram_card import (
    ANALYSIS_LABEL,
    ConditionalOrderContextV1,
    WeightContextV1,
    compute_standings,
    render_decision_cards,
)
from tokmo.values.quant_discovery import ReferenceEntryZoneV1

_QUOTE = lambda price: QuoteContextV1(
    Decimal(price), "KRW", datetime(2026, 9, 3, 10, 0, tzinfo=KST), "최근 시세 스토어"
)


def _order_ctx(
    trigger, price=None, *, expire="2026-09-30", scope="개인계좌", side="BUY"
):
    return ConditionalOrderContextV1(
        scope,
        "SINGLE",
        "LIMIT",
        "2",
        "STOP_LOSS",
        "WATCHING",
        Decimal(trigger),
        None if price is None else Decimal(price),
        expire,
        side,
    )


def _evidence(pack, item_id):
    return next(item for item in pack["evidence"] if item["id"] == item_id)


def _ids(pack):
    return {item["id"] for item in pack["evidence"]} | {
        gap["id"] for gap in pack["gaps"]
    }


def _statement(text, *ids):
    return {"statement": text, "evidence_ids": list(ids)}


def _good_output(pack):
    """A minimal valid reply that connects real pack ids; adapts to ACTIVE/NONE."""
    ids = _ids(pack)
    has_orders = "order:1" in ids
    anchor = next(
        item for item in ("rel:price_zone", "zone", "f:ret_120") if item in ids
    )
    assessment = (
        {
            "verdict": "조정 검토",
            "statement": "발동가는 관찰구간 안이지만 현재가보다 한참 아래라 체결 가능성은 멀음",
            "evidence_ids": ["order:1", "f:ret_120"],
        }
        if has_orders
        else None
    )
    return {
        "symbol": pack["symbol"],
        "summary": "중장기 상승과 단기 과열이 동시에 있어 추격보다 눌림 확인이 우선",
        "evidence_relations": [
            _statement(
                "120일 수익률은 강하나 RSI14 과열이 이를 상쇄", "f:ret_120", "f:rsi_14"
            )
        ],
        "conditional_order_assessment": assessment,
        "alternatives": [
            _statement("유지: 현재 구간과 조건을 그대로 두고 확인 신호 대기", anchor)
        ],
        "counterevidence": [
            _statement(
                "63일 변동성이 매우 높아 되돌림 폭이 클 수 있음", "f:realized_vol_63"
            )
        ],
        "assumptions": [],
        "falsifiers": [
            _statement(
                "무효화 기준 하향 이탈 시 상승 가설 기각",
                "zone" if "zone" in ids else "f:ret_120",
            )
        ],
        "confidence": {
            "level": "낮음",
            "statement": "최근 사건·공시 근거가 없어 확신도 제한",
            "evidence_ids": ["gap:news_disclosure"],
        },
        "change_conditions": [
            _statement(
                "관찰구간 재산출 또는 로컬 공시 근거 연결 시 재판단",
                "f:ret_120",
                "gap:news_disclosure",
            )
        ],
    }


def test_evidence_pack_relations_are_decimal_exact_and_hold_for_other_numbers():
    """Same relational contract for different symbols/prices/zones: no keyword or symbol rule."""
    other_zone = ReferenceEntryZoneV1(
        Decimal(1250000),
        Decimal(1350000),
        "TREND_PULLBACK",
        ("SMA20",),
        Decimal(1200000),
        1,
        Decimal(1380000),
        Decimal(0),
        50,
        "x",
        "y",
    )
    cases = (
        ("000001", "3000000", ZONE, "2300000", "2290000"),
        ("000042", "1400000", other_zone, "1300000", None),
    )
    for symbol, price, zone, trigger, order_price in cases:
        card = _card(symbol, "KR", f"종목{symbol}", VALUES_HOT, zone)
        context = CardContextV1(
            symbol,
            _QUOTE(price),
            None,
            (),
            True,
            False,
            None,
            None,
            ConditionalContextV1("ACTIVE", None, (_order_ctx(trigger, order_price),)),
        )
        pack = build_evidence_pack(card, context, completed_session_date="2026-09-02")
        price_d, trigger_d = Decimal(price), Decimal(trigger)
        relation = _evidence(pack, "rel:order:1")
        assert Decimal(relation["trigger_vs_price_pct"]) == (
            (trigger_d / price_d - 1) * 100
        ).quantize(Decimal("0.01"))
        assert Decimal(relation["trigger_minus_price"]) == trigger_d - price_d
        assert relation["zone"]["position"] == "inside"
        assert Decimal(relation["zone"]["to_lower_pct"]) == (
            (trigger_d / zone.lower - 1) * 100
        ).quantize(Decimal("0.01"))
        assert Decimal(_evidence(pack, "rel:price_zone")["to_upper_pct"]) == (
            (price_d / zone.upper - 1) * 100
        ).quantize(Decimal("0.01"))
        assert _evidence(pack, "rel:price_zone")["position"] == "above_upper"
        assert (
            _evidence(pack, "order:1")["side"] == "buy"
            and _evidence(pack, "order:1")["trigger_price"] == trigger
        )
        assert (relation["order_vs_trigger_pct"] is None) == (order_price is None)
        assert pack["symbol"] == symbol and json.dumps(
            pack
        )  # JSON-safe: Decimals are strings
    # trigger below invalidation / above upper: relation labels follow the numbers only
    card = _card("000007", "KR", "종목7", VALUES_HOT, ZONE)
    orders = (_order_ctx("2000000"), _order_ctx("2500000"))
    pack = build_evidence_pack(
        card,
        CardContextV1(
            "000007",
            _QUOTE("2400000"),
            None,
            (),
            True,
            False,
            None,
            None,
            ConditionalContextV1("ACTIVE", None, orders),
        ),
        completed_session_date="2026-09-02",
    )
    assert _evidence(pack, "rel:order:1")["zone"]["position"] == "below_invalidation"
    assert _evidence(pack, "rel:order:2")["zone"]["position"] == "above_upper"
    assert _evidence(pack, "rel:price_zone")["position"] == "inside"
    # no quote and no zone: relations are honest nulls and become gaps, never estimates
    bare = build_evidence_pack(
        _card("000008", "KR", "종목8", VALUES_CALM),
        CardContextV1(
            "000008",
            None,
            "시세 없음",
            (),
            True,
            False,
            None,
            None,
            ConditionalContextV1("ACTIVE", None, (_order_ctx("100"),)),
        ),
        completed_session_date="2026-09-02",
    )
    assert _evidence(bare, "rel:order:1") == {
        "id": "rel:order:1",
        "kind": "relation",
        "label": "조건주문1↔현재가·구간",
        "trigger_vs_price_pct": None,
        "trigger_minus_price": None,
        "zone": None,
        "order_vs_trigger_pct": None,
    }
    assert {"gap:quote", "gap:zone", "gap:playbook"} <= {
        gap["id"] for gap in bare["gaps"]
    }


def test_evidence_pack_isolates_orders_per_symbol_and_reflects_none_partial_unavailable():
    card_a, card_b = (
        _card("000001", "KR", "에이", VALUES_HOT, ZONE),
        _card("000002", "KR", "비", VALUES_CALM),
    )
    context_a = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1(
            "ACTIVE",
            "일부 계좌 확인 불가: 퇴직연금계좌 제공자 호출 실패",
            (_order_ctx("2300000"),),
        ),
    )
    context_b = CardContextV1(
        "000002",
        _QUOTE("50000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("NONE"),
    )
    pack_a = build_evidence_pack(card_a, context_a, completed_session_date="2026-09-02")
    pack_b = build_evidence_pack(card_b, context_b, completed_session_date="2026-09-02")
    assert (
        "order:1" in _ids(pack_a)
        and "order:1" not in _ids(pack_b)
        and "2300000" not in json.dumps(pack_b)
    )
    assert "conditional_status" not in pack_a and not any(
        gap["id"].startswith("gap:conditional") for gap in pack_a["gaps"]
    )
    assert "conditional_status" not in pack_b and not any(
        gap["id"].startswith("gap:conditional") for gap in pack_b["gaps"]
    )
    with pytest.raises(ValueError, match="context_symbol_mismatch"):
        build_evidence_pack(card_a, context_b, completed_session_date="2026-09-02")
    # an output for B citing A's order id is rejected: ids resolve only within B's own pack
    output = _good_output(pack_b)
    output["evidence_relations"] = [
        _statement("비의 조건주문과 구간 관계", "order:1", "f:ret_120")
    ]
    with pytest.raises(AnalysisValidationError, match="unknown_evidence_id"):
        validate_analysis(output, pack_b)
    with pytest.raises(AnalysisValidationError, match="symbol_mismatch"):
        validate_analysis(dict(_good_output(pack_b), symbol="000001"), pack_b)
    # an unread or unavailable conditional read is a gap, never an inferred absence
    for conditional, detail in (
        (None, "조회 미실행"),
        (
            ConditionalContextV1("UNAVAILABLE", "개인계좌 계좌 미매핑"),
            "개인계좌 계좌 미매핑",
        ),
    ):
        pack = build_evidence_pack(
            card_b,
            dataclasses.replace(context_b, conditional=conditional),
            completed_session_date="2026-09-02",
        )
        assert pack == pack_b
    # the model may not invent an assessment for a symbol without orders, nor skip citing real ones
    claim = {
        "verdict": "맞음",
        "statement": "조건이 구간과 맞음",
        "evidence_ids": ["f:ret_120"],
    }
    with pytest.raises(AnalysisValidationError, match="conditional_invented"):
        validate_analysis(
            dict(_good_output(pack_b), conditional_order_assessment=claim), pack_b
        )
    with pytest.raises(AnalysisValidationError, match="conditional_uncited"):
        validate_analysis(
            dict(_good_output(pack_a), conditional_order_assessment=claim), pack_a
        )


@pytest.mark.parametrize("wire", [False, True])
def test_unknown_conditional_side_cannot_be_inferred_from_price_holdings_or_leg_type(
    wire,
):
    def validate_analysis(output, pack):
        if wire:
            encoded = _map_reply(output, qca._citation_map(pack))
            output = analyzer_adapter(lambda request: json.dumps(encoded))(pack)
        return qca.validate_analysis(output, pack)

    card = _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE)
    unknown_context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("ACTIVE", None, (_order_ctx("2300000"),)),
    )
    pack = build_evidence_pack(
        card, unknown_context, completed_session_date="2026-09-02"
    )
    # Unknown legs are excluded before wire IDs are assigned. Invented citations fail exactly.
    unknown_context = dataclasses.replace(
        unknown_context,
        conditional=ConditionalContextV1(
            "ACTIVE", None, (_order_ctx("2300000", side=None),)
        ),
    )
    pack = build_evidence_pack(
        card, unknown_context, completed_session_date="2026-09-02"
    )
    assert "order:1" not in _ids(pack)
    output = _good_output(pack)
    output["assumptions"] = [_statement("조건주문 목적을 해석한다", "order:1")]
    with pytest.raises(AnalysisValidationError, match="unknown_evidence_id"):
        validate_analysis(output, pack)
    assert (
        validate_analysis(_good_output(pack), pack).conditional_order_assessment is None
    )


def test_evidence_pack_reuses_playbook_synthesis_holdings_and_optional_context_without_private_data():
    synthesis = synthesize_playbook(PLAYBOOK, scope_label="개인계좌")
    weight = WeightContextV1(
        Decimal("0.6"), "개인계좌 KR 주식", None, None, "USD/KRW 환율 근거 없음"
    )
    down = dict(VALUES_HOT, ret_120="-0.3", close_to_sma_120="-0.1")
    card = _card("000001", "KR", "테스트전자", down, ZONE)
    context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (HoldingContextV1("개인계좌", "4", "1,900,000원", weight),),
        True,
        True,
        synthesis,
        None,
        ConditionalContextV1("NONE"),
    )
    items = (
        ContextItemV1(
            "news", "9월 1일 실적 가이던스 상향 발표", "2026-09-01", "로컬 NEWS"
        ),
        ContextItemV1(
            "trade_reason",
            "직전 매도는 비중 상한 초과 정리",
            "2026-08-20",
            "플레이북 3층",
        ),
    )
    pack = build_evidence_pack(
        card,
        context,
        completed_session_date="2026-09-02",
        standings=compute_standings((card,)),
        context_items=items,
    )
    playbook = _evidence(pack, "playbook")
    assert (
        playbook["stance"] == "HOLD"
        and playbook["quant_trend"] == "down"
        and playbook["quant_agreement"] == "주의"
    )
    assert playbook["layer3_entries"] == 2 and playbook["latest_trade_side"] == "SELL"
    holding = _evidence(pack, "holding:1")
    assert (
        holding["market_weight_pct"] == "60.00"
        and holding["total_weight_reason"] == "USD/KRW 환율 근거 없음"
    )
    assert (
        _evidence(pack, "ctx:1")["kind"] == "news"
        and _evidence(pack, "ctx:2")["summary"] == "직전 매도는 비중 상한 초과 정리"
    )
    assert _evidence(pack, "standing") == {
        "id": "standing",
        "kind": "standing",
        "label": "상대순위",
        "ordinal": 1,
        "sample": 1,
        "scope": "리포트 카드 한정",
    }
    dumped = json.dumps(pack, ensure_ascii=False)
    for leak in (
        "SECRET-THESIS-MARKER",
        "RAW-USER-LINE",
        ".md",
        "trade_summary",
        TOSS_SCOPE,
        "c-1",
    ):
        assert leak not in dumped
    gap_ids = {gap["id"] for gap in pack["gaps"]}
    assert (
        "gap:news_disclosure" not in gap_ids
    )  # a recent local news item closes the recency gap locally
    assert "gap:playbook_conflict" not in gap_ids
    # stale news (older than 14 days) reopens the recency gap; conflict without any news/disclosure is a gap
    stale = build_evidence_pack(
        card,
        context,
        completed_session_date="2026-09-02",
        context_items=(ContextItemV1("news", "오래된 기사", "2026-08-01"),),
    )
    assert {"gap:news_disclosure", "gap:playbook_conflict"} <= {
        gap["id"] for gap in stale["gaps"]
    }
    for bad in (
        ContextItemV1("rumor", "x"),
        ContextItemV1("news", "token=abc"),
        ContextItemV1("news", "x" * 401),
        "news",
    ):
        with pytest.raises(ValueError):
            build_evidence_pack(
                card, context, completed_session_date="2026-09-02", context_items=(bad,)
            )
    with pytest.raises(ValueError, match="evidence_pack_privacy"):
        build_evidence_pack(
            card,
            dataclasses.replace(
                context, holdings=(HoldingContextV1("account_id 7", "4", "1", weight),)
            ),
            completed_session_date="2026-09-02",
        )


def test_local_evidence_sufficient_means_zero_external_calls_and_gaps_are_bounded_retry_0():
    calls = {"search": [], "fetch": []}

    def search(query):
        calls["search"].append(query)
        if query["gap"] == "gap:one_sided":
            raise RuntimeError("search down")
        return [
            {"url": "https://blog.example/x", "title": "blog", "primary": False},
            {
                "url": "https://dart.example/official",
                "title": "공시",
                "primary": True,
                "published_on": "2026-09-01",
            },
            {"url": "https://ir.example/official2", "title": "IR", "primary": True},
        ]

    def fetch(url):
        calls["fetch"].append(url)
        if url.endswith("official"):
            raise RuntimeError("fetch failed")
        return "발행사 IR: 3분기 가이던스 상향"

    def failing_fetch(url):
        calls["fetch"].append(url)
        raise RuntimeError("fetch failed")

    # sufficient local evidence (recent disclosure, two-sided judgment, no playbook conflict): zero calls
    card = _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE)
    context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("NONE"),
    )
    pack = build_evidence_pack(
        card,
        context,
        completed_session_date="2026-09-02",
        context_items=(
            ContextItemV1("disclosure", "9월 2일 자사주 취득 공시", "2026-09-02"),
        ),
    )
    assert gather_external_evidence(pack, search=search, fetch=fetch)["external"] == {
        "queries": 0,
        "fetches": 0,
        "retries": 0,
        "gaps_closed": [],
        "gaps_open": [],
    }
    assert calls == {"search": [], "fetch": []}
    # gaps present but no seams bound: still zero calls, gaps stay listed
    gapped = build_evidence_pack(card, context, completed_session_date="2026-09-02")
    assert (
        gather_external_evidence(gapped)["external"]["gaps_open"]
        == ["gap:news_disclosure"]
        and calls["search"] == []
    )
    # gaps + seams: one query per gap, only primary candidates fetched, failed fetch not retried, early exit on close
    gathered = gather_external_evidence(gapped, search=search, fetch=fetch)
    assert [q["gap"] for q in calls["search"]] == [
        "gap:news_disclosure"
    ] and "테스트전자 (000001)" in calls["search"][0]["query"]
    assert calls["fetch"] == [
        "https://dart.example/official",
        "https://ir.example/official2",
    ]
    assert gathered["external"] == {
        "queries": 1,
        "fetches": 2,
        "retries": 0,
        "gaps_closed": ["gap:news_disclosure"],
        "gaps_open": [],
    }
    assert (
        _evidence(gathered, "ext:1")["summary"] == "발행사 IR: 3분기 가이던스 상향"
        and _evidence(gathered, "ext:1")["gap"] == "gap:news_disclosure"
    )
    assert "ext:1" not in _ids(gapped)  # input pack untouched
    # three searchable gaps, a failing search, and an always-failing fetch: caps hold, retry 0, gaps reported open
    one_sided = {
        key: (
            "50"
            if key == "rsi_14"
            else "0.1"
            if key == "realized_vol_63"
            else "-0.05"
            if key == "max_drawdown"
            else "0.5"
        )
        for key in VALUES_HOT
    }
    conflict = CardContextV1(
        "000003",
        _QUOTE("100"),
        None,
        (),
        True,
        False,
        PlaybookSynthesisV1("개인계좌", True, True, 0, None, "REDUCE"),
        None,
        ConditionalContextV1("NONE"),
    )
    pack3 = build_evidence_pack(
        _card("000003", "KR", "일방", one_sided, None),
        conflict,
        completed_session_date="2026-09-02",
    )
    assert [gap["id"] for gap in pack3["gaps"]][-3:] == [
        "gap:news_disclosure",
        "gap:playbook_conflict",
        "gap:one_sided",
    ]
    calls["search"].clear()
    calls["fetch"].clear()
    failing = gather_external_evidence(pack3, search=search, fetch=failing_fetch)
    assert (
        len(calls["search"]) == MAX_QUERIES == 3
        and len(calls["fetch"]) == 4 <= MAX_FETCHES
    )
    assert failing["external"] == {
        "queries": 3,
        "fetches": 4,
        "retries": 0,
        "gaps_closed": [],
        "gaps_open": ["gap:news_disclosure", "gap:playbook_conflict", "gap:one_sided"],
    }
    assert not any(item["id"].startswith("ext:") for item in failing["evidence"])
    # fetch cap: nine primary candidates are truncated at 5 fetches in total, and no further query is issued
    many = lambda query: [
        {"url": f"https://ir.example/{index}", "primary": True} for index in range(9)
    ]
    calls["fetch"].clear()
    capped = gather_external_evidence(pack3, search=many, fetch=failing_fetch)
    assert (
        capped["external"]["fetches"] == len(calls["fetch"]) == MAX_FETCHES == 5
        and capped["external"]["queries"] == 1
    )


def test_pack_marks_supporting_and_conflicting_evidence_and_external_items_become_citable():
    # HOLD + up trend with a two-sided judgment: agreement 일치, no conflict/one-sided gap
    supportive = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        PlaybookSynthesisV1("개인계좌", True, True, 1, "BUY", "HOLD"),
        None,
        ConditionalContextV1(
            "ACTIVE",
            None,
            (_order_ctx("2300000"), _order_ctx("2600000", scope="퇴직연금계좌")),
        ),
    )
    pack = build_evidence_pack(
        _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
        supportive,
        completed_session_date="2026-09-02",
    )
    assert (
        _evidence(pack, "playbook")["quant_agreement"] == "일치"
        and _evidence(pack, "playbook")["quant_trend"] == "up"
    )
    assert [gap["id"] for gap in pack["gaps"]] == ["gap:news_disclosure"]
    assert (
        _evidence(pack, "order:2")["scope_label"] == "퇴직연금계좌"
        and _evidence(pack, "rel:order:2")["zone"]["position"] == "above_upper"
    )
    assert _evidence(pack, "rel:order:1")["zone"]["position"] == "inside"
    # REDUCE + up trend: 상충 and (without recent primary evidence) a searchable conflict gap
    conflicting = dataclasses.replace(
        supportive,
        playbook=PlaybookSynthesisV1("개인계좌", True, False, 0, None, "REDUCE"),
    )
    pack = build_evidence_pack(
        _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
        conflicting,
        completed_session_date="2026-09-02",
    )
    assert _evidence(pack, "playbook")["quant_agreement"] == "상충"
    assert [gap["id"] for gap in pack["gaps"]] == [
        "gap:news_disclosure",
        "gap:playbook_conflict",
    ]
    # an external primary item gathered for a gap is a first-class, citable evidence id
    gathered = gather_external_evidence(
        pack,
        search=lambda q: [{"url": "https://ir.example/q2", "primary": True}],
        fetch=lambda url: "발행사 IR: 2분기 실적 발표",
    )
    output = _good_output(gathered)
    output["counterevidence"] = [
        _statement("발행사 IR 실적과 플레이북 축소 기조가 갈림", "ext:1", "playbook")
    ]
    validated = validate_analysis(output, gathered)
    assert (
        validated.counterevidence[0]["evidence_ids"] == ["ext:1", "playbook"]
        and validated.labels["ext:1"]["label"] == "외부1(1차 출처)"
    )
    with pytest.raises(AnalysisValidationError, match="unknown_evidence_id"):
        validate_analysis(
            output, pack
        )  # the same claim is unsupported against the un-gathered pack


def test_validation_rejects_unsupported_prose_probability_privacy_and_order_instructions():
    context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("ACTIVE", None, (_order_ctx("2300000", "2290000"),)),
    )
    pack = build_evidence_pack(
        _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
        context,
        completed_session_date="2026-09-02",
    )
    good = validate_analysis(_good_output(pack), pack)
    assert (
        good.symbol == "000001"
        and good.conditional_order_assessment["verdict"] == "조정 검토"
    )
    assert (
        good.labels["rel:order:1"]["label"] == "조건주문1↔현재가·구간"
        and good.labels["gap:news_disclosure"]["label"] == "최근 사건·공시 근거 없음"
    )

    def rejected(code, **changes):
        with pytest.raises(AnalysisValidationError, match=code):
            validate_analysis(dict(_good_output(pack), **changes), pack)

    rejected("required_sections", extra="x")
    missing = _good_output(pack)
    del missing["falsifiers"]
    with pytest.raises(AnalysisValidationError, match="required_sections"):
        validate_analysis(missing, pack)
    rejected("unsupported_claim", counterevidence=[_statement("근거 없는 주장")])
    rejected(
        "unsupported_claim",
        evidence_relations=[_statement("하나의 근거만 인용", "f:rsi_14")],
    )  # a relation needs ≥2
    rejected("unknown_evidence_id", falsifiers=[_statement("없는 근거", "f:ret_999")])
    rejected(
        "unmodelled_probability",
        alternatives=[_statement("발동가 도달 확률 35%", "rel:order:1")],
    )
    rejected(
        "unmodelled_probability",
        alternatives=[_statement("70% 가능성으로 도달", "rel:order:1")],
    )
    rejected("unmodelled_probability", summary="도달 probability 0.4")
    rejected("free_text_number", summary="현재가 1,5790,000원은 높음")
    rejected("free_text_number", summary="수익률 0.7007은 강함")
    qualitative = dict(
        _good_output(pack),
        summary="RSI14가 낮아 추가 하락 확률을 정성적으로 점검해야 함",
    )
    assert validate_analysis(qualitative, pack).summary.startswith("RSI14")
    rejected("privacy", falsifiers=[_statement("account_id 12 기준 손절", "zone")])
    rejected(
        "order_instruction", alternatives=[_statement("지금 매도하세요", "rel:order:1")]
    )
    rejected(
        "order_instruction",
        alternatives=[_statement("조건주문을 취소하세요", "order:1")],
    )
    rejected("statement_text", counterevidence=[_statement("x" * 241, "f:rsi_14")])
    rejected("section_alternatives", alternatives=[])
    rejected("section_falsifiers", falsifiers=[_statement("a", "zone")] * 6)
    rejected(
        "conditional_shape",
        conditional_order_assessment={
            "verdict": "확실",
            "statement": "x",
            "evidence_ids": ["order:1"],
        },
    )
    rejected(
        "confidence_shape",
        confidence={"level": "매우 높음", "statement": "x", "evidence_ids": ["zone"]},
    )
    rejected("statement_shape", confidence={"level": "낮음", "statement": "x"})
    # Qualitative distance labels are allowed; numerical values remain in the
    # deterministic evidence/card and are not retyped by the model.
    fine = dict(
        _good_output(pack),
        alternatives=[
            _statement("발동가는 현재가보다 멀어 가능 범위 밖", "rel:order:1")
        ],
    )
    assert (
        validate_analysis(fine, pack)
        .alternatives[0]["statement"]
        .endswith("가능 범위 밖")
    )
    # no per-symbol/keyword registry, no I/O, no transport in the analysis module (relations only)
    source = (card_tests.SRC / "programs" / "quant_contextual_analysis.py").read_text(
        encoding="utf-8"
    )
    for banned in (
        "000001",
        "NVDA",
        "삼성",
        "테슬라",
        "_STANCE_WORDS",
        "KEYWORDS",
        "sqlite",
        "open(",
        "Path(",
        "urllib",
        "requests",
        "http.client",
        "socket",
        "toss_reads_live",
        "telegram_dialogue_live",
        "ops_kernel",
        "write_text",
    ):
        assert banned not in source, banned


def test_analysis_off_and_model_failure_keep_card_byte_equivalent_and_validated_output_renders():
    report, contexts = card_tests._kr_report(), card_tests._kr_contexts()
    base = render_market_message(report, contexts, report_sha256="d" * 64)
    standings = compute_standings(report.cards)
    packs = {
        card.symbol: build_evidence_pack(
            card, context, completed_session_date="2026-09-02", standings=standings
        )
        for card, context in zip(
            sorted(report.cards, key=lambda item: item.symbol), contexts
        )
    }
    assert run_contextual_analysis(packs["000001"]) == (None, "analysis_off")

    def timeout(pack):
        raise TimeoutError

    assert (
        run_contextual_analysis(packs["000001"], analysis=timeout)[1]
        == "model_failure:TimeoutError"
    )
    assert (
        run_contextual_analysis(packs["000001"], analysis=lambda pack: "not json")[1]
        == "invalid_output:required_sections"
    )
    assert (
        run_contextual_analysis(
            packs["000001"],
            analysis=lambda pack: dict(_good_output(pack), summary="도달 확률 90%"),
        )[1]
        == "invalid_output:unmodelled_probability"
    )
    # OFF, model failure, invalid output → identical bytes; explicit None analyses too
    for analyses in (None, {}, {"000001": None}):
        assert (
            render_market_message(
                report, contexts, report_sha256="d" * 64, analyses=analyses
            )
            == base
        )
    assert render_decision_cards(
        {"KR": (report, "d" * 64)}, {"KR": contexts}
    ) == render_decision_cards({"KR": (report, "d" * 64)}, {"KR": contexts}, {"KR": {}})
    assert (
        "규칙 기반 · AI 분석 미연결" in base.parts[0]
        and ANALYSIS_LABEL not in base.parts[0]
        and "맥락 분석" not in base.grounded_sections
    )
    # validated analysis for one symbol renders only in that card, after 행동 제안, with evidence labels
    validated, reason = run_contextual_analysis(packs["000001"], analysis=_good_output)
    assert reason == "validated"
    message = render_market_message(
        report, contexts, report_sha256="d" * 64, analyses={"000001": validated}
    )
    text = message.parts[0]
    block, other = text.split("• 평온산업(000002)", 1)
    assert (
        "투자판단\n" in block
        and "행동 제안" not in block
        and ANALYSIS_LABEL not in block
    )
    assert (
        block.index("투자판단")
        < block.index("왜 이렇게 보나")
        < block.index("어떻게 대응하나")
    )
    assert "+165.0%" in block.split("왜 이렇게 보나")[0]
    assert "왜 이렇게 보나" not in other
    assert "맥락 분석" in message.grounded_sections
    # Oversize v2 is explicit failure, never an AI-free substitute.
    huge = _good_output(packs["000001"])
    huge["summary"] = "요" * 240
    for section in (
        "alternatives",
        "counterevidence",
        "assumptions",
        "falsifiers",
        "change_conditions",
    ):
        huge[section] = [_statement("가" * 240, "f:ret_120") for _ in range(5)]
    huge["evidence_relations"] = [
        _statement("가" * 240, "f:ret_120", "f:rsi_14") for _ in range(5)
    ]
    oversized = validate_analysis(huge, packs["000001"])
    with pytest.raises(ValueError, match="cap"):
        render_market_message(
            report, contexts, report_sha256="d" * 64, analyses={"000001": oversized}
        )


def test_external_search_budget_is_shared_across_cards():
    calls = {"search": 0, "fetch": 0}
    budget = {"queries": 0, "fetches": 0}
    context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("NONE"),
    )
    pack = build_evidence_pack(
        _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
        context,
        completed_session_date="2026-09-02",
    )

    def search(_query):
        calls["search"] += 1
        return [
            {"url": f"https://ir.example/{calls['search']}/{index}", "primary": True}
            for index in range(5)
        ]

    def fetch(_url):
        calls["fetch"] += 1
        raise RuntimeError("retry zero")

    for _ in range(4):
        gather_external_evidence(pack, search=search, fetch=fetch, budget=budget)
    assert calls == {"search": 1, "fetch": MAX_FETCHES}
    assert budget == {"queries": 1, "fetches": MAX_FETCHES}

    query_calls = {"count": 0}
    query_budget = {"queries": 0, "fetches": 0}

    def empty_search(_query):
        query_calls["count"] += 1
        return []

    for _ in range(4):
        gather_external_evidence(
            pack, search=empty_search, fetch=fetch, budget=query_budget
        )
    assert query_calls["count"] == MAX_QUERIES
    assert query_budget == {"queries": MAX_QUERIES, "fetches": 0}


def test_controller_analysis_seam_is_off_by_default_counts_calls_and_never_writes(
    tmp_path, capsys
):
    tool = card_tests._tool()
    root = tmp_path / "reports"
    card_tests._store_report(
        root,
        "KR",
        (
            _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
            _card("000002", "KR", "평온산업", VALUES_CALM),
        ),
        "a",
    )
    paths = dict(
        holdings_path=tmp_path / "no_holdings.json",
        quotes_path=tmp_path / "no_quotes.sqlite3",
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "no_refs.sqlite3",
    )
    calls = {"model": 0, "search": 0}

    def analysis(pack):
        calls["model"] += 1
        if pack["symbol"] == "000002":
            raise RuntimeError("model down")
        return _good_output(pack)

    def search(query):
        calls["search"] += 1
        return []

    def run(*argv, **kwargs):
        code = tool.main(
            ["preview", "--report-root", str(root), "--text", *argv],
            clock=lambda: NOW,
            **paths,
            **kwargs,
        )
        out = capsys.readouterr().out
        assert code == tool.EXIT_OK
        return out, json.loads(out.strip().split("\n")[-1])

    before = sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
    # default OFF: zero model calls, no analysis section; the flag alone (no callable) is honest about the unbound seam
    off_text, off = run()
    assert (
        off["reports"]["evidence"]["analysis"] == {"enabled": False, "model_calls": 0}
        and calls["model"] == 0
    )
    flag_text, flagged = run("--analysis-preview")
    assert flagged["reports"]["evidence"]["analysis"] == {
        "enabled": False,
        "model_calls": 0,
        "reason": "model_seam_unbound",
    }
    assert (
        calls["model"] == 0
        and flag_text.split("\n{")[0] == off_text.split("\n{")[0]
        and ANALYSIS_LABEL not in off_text
    )
    # injected callable without the flag stays OFF; with the flag it runs once per card and renders only validated cards
    _text, quiet = run(analysis=analysis)
    assert (
        quiet["reports"]["evidence"]["analysis"]["enabled"] is False
        and calls["model"] == 0
    )
    on_text, on = run(
        "--analysis-preview", analysis=analysis, search=search, fetch=lambda url: "x"
    )
    receipt = on["reports"]["evidence"]["analysis"]
    assert receipt["enabled"] is True and receipt["model_calls"] == 2 == calls["model"]
    assert receipt["validated"] == ["KR:000001"] and receipt["rejected"] == {
        "KR:000002": "model_failure:RuntimeError"
    }
    # 000002 (calm, all-positive factors) is one-sided → one more searchable gap; every search returned nothing
    assert receipt["external"] == {
        "queries": 3,
        "fetches": 0,
        "retries": 0,
        "gaps_open": {
            "KR:000001": ["gap:news_disclosure"],
            "KR:000002": ["gap:news_disclosure", "gap:one_sided"],
        },
    }
    assert calls["search"] == 3
    block, other = on_text.split("• 테스트전자(000001)", 1)[1].split(
        "• 평온산업(000002)", 1
    )
    assert (
        "왜 이렇게 보나" in block
        and "왜 이렇게 보나" not in other
        and "규칙 기반 · AI 분석 미연결" in other
    )
    # the failed card is byte-identical to the OFF render of the same card
    assert (
        other.split("\n{")[0]
        == off_text.split("• 평온산업(000002)", 1)[1].split("\n{")[0]
    )
    # no file was created or modified by any run (no order/account/ledger/model-output write)
    assert (
        sorted(str(path.relative_to(tmp_path)) for path in tmp_path.rglob("*"))
        == before
    )
    for leak in ("token", "secret", "/Users/", TOSS_SCOPE):
        assert leak not in block
    assert TOSS_SCOPE not in json.dumps(on) and "native" not in json.dumps(on)


# --- provider-neutral analyzer adapter (nearest unfinished vertical, 2026-09-04) ------------

import logging

from tokmo.programs import quant_contextual_analysis as qca
from tokmo.programs.quant_contextual_analysis import (
    analyzer_adapter,
    build_analyzer_request,
)
from tokmo.records.quant_telegram_card_receipts import (
    write_card_activation,
)


def _pack(symbol="000001", name="테스트전자", values=VALUES_HOT, price="3000000"):
    card = _card(symbol, "KR", name, values, ZONE if values is VALUES_HOT else None)
    return build_evidence_pack(
        card,
        CardContextV1(
            symbol,
            _QUOTE(price),
            None,
            (),
            True,
            False,
            None,
            None,
            ConditionalContextV1("NONE"),
        ),
        completed_session_date="2026-09-02",
    )


_ADAPTER_PACK = _pack()
_CALM_PACK = _pack("000002", "평온산업", VALUES_CALM, "60000")


def _wire_good_output(request):
    from tokmo.programs.quant_finite_canary import (
        STATEMENT_PLACEHOLDER,
        analyzer_prompt_skeleton,
    )

    return json.loads(
        json.dumps(analyzer_prompt_skeleton(request), ensure_ascii=False).replace(
            STATEMENT_PLACEHOLDER, "근거가 서로 지지해 판단이 유지됨"
        )
    )


def _good_transport():
    return lambda request: json.dumps(_wire_good_output(request), ensure_ascii=False)


def test_adapter_builds_fixed_contract_request_from_pack_only():
    request = build_analyzer_request(_ADAPTER_PACK)
    assert set(request) == {
        "schema_version",
        "model_purpose",
        "system",
        "task",
        "evidence_pack",
    }  # deterministic only
    assert (
        request["schema_version"] == 1
        and request["model_purpose"] == "quant-contextual-decision-analysis-v1"
    )
    assert request["task"]["output_contract"]["type"] == "json_object"
    assert _ids(request["evidence_pack"]) == set(
        qca._citation_map(_ADAPTER_PACK).values()
    )
    # no account/order ids, paths, credentials, raw provider payloads, cohort data
    # ("native_response" not the substring "native": required key "alternatives" contains it)
    dumps = json.dumps(request)  # also proves JSON-safety
    for leak in (
        TOSS_SCOPE,
        "/Users/",
        "token",
        "secret",
        "native_response",
        "cohort",
        "orders_all",
        "report",
    ):
        assert leak not in dumps


def test_adapter_request_carries_complete_structural_contract_matching_validator():
    """M1: output_contract tells the transport/model the exact validate_analysis
    contract — one deterministic JSON-safe mapping derived from the module
    constants, not a second validator."""
    contract = build_analyzer_request(_ADAPTER_PACK)["task"]["output_contract"]
    assert contract["required_keys"] == sorted(
        qca.REQUIRED_KEYS
    )  # same constant the validator uses
    assert (
        contract["symbol"] == _ADAPTER_PACK["symbol"]
        and contract["symbol_must_equal_pack"] is True
    )
    for (
        name
    ) in qca.LIST_SECTIONS:  # statement+evidence_ids shape, min/max items, emptiness
        section = contract["sections"][name]
        assert section["statement_keys"] == ["statement", "evidence_ids"]
        assert section["max_items"] == qca.MAX_ITEMS
        assert section["min_items"] == 1
        assert section["may_be_empty"] is (name == "assumptions")
    assessment, confidence = (
        contract["conditional_order_assessment"],
        contract["confidence"],
    )
    assert assessment["statement_keys"] == ["verdict", "statement", "evidence_ids"]
    assert assessment["verdict_enum"] == list(
        qca.CONDITIONAL_VERDICTS
    )  # exact validator enums
    assert confidence["statement_keys"] == ["level", "statement", "evidence_ids"]
    assert confidence["level_enum"] == list(qca.CONFIDENCE_LEVELS)
    citations = contract[
        "citations"
    ]  # ids must come from this pack; relations cite >=2
    assert citations["allowed_ids"] == list(qca._citation_map(_ADAPTER_PACK).values())
    assert citations["must_cite_pack_ids"] is True and citations["unique"] is True
    assert (
        citations["min_per_relation"] == 2
        and contract["statement_text_max_chars"] == qca.MAX_STATEMENT_CHARS
    )
    other = build_analyzer_request(_CALM_PACK)["task"]["output_contract"]
    assert (
        other["symbol"] == "000002"
    )  # derived mapping follows the pack: own ids/symbol
    assert other["citations"]["allowed_ids"] != citations["allowed_ids"]


def test_output_contract_parity_covers_validator_behaviour_and_pack_conditional_switch():
    """M1 parity: the request's output_contract communicates every behaviour
    validate_analysis enforces, and the conditional semantics switch on the
    pack's own order evidence (with-order vs no-order)."""
    hot_context = CardContextV1(
        "000001",
        _QUOTE("3000000"),
        None,
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("ACTIVE", None, (_order_ctx("2300000"),)),
    )
    with_orders = build_evidence_pack(
        _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
        hot_context,
        completed_session_date="2026-09-02",
    )
    no_orders = _pack()  # ConditionalContextV1("NONE")
    for pack in (with_orders, no_orders):
        contract = build_analyzer_request(pack)["task"]["output_contract"]
        assert (
            contract["required_keys"] == sorted(qca.REQUIRED_KEYS)
            and contract["no_extra_keys"] is True
        )
        assert contract["statement_text"] == {
            "non_empty": True,
            "max_chars": qca.MAX_STATEMENT_CHARS,
            "no_numeric_probability": True,
            "no_percent_reach": True,
            "no_digits_except_labels": list(qca._ALLOWED_ANALYSIS_NUMBER_LABELS),
            "no_private_markers": True,
            "no_order_instructions": list(qca._ORDER_INSTRUCTIONS),
        }
        citations = contract["citations"]
        assert (
            citations["allowed_ids"] == list(qca._citation_map(pack).values())
            and citations["elements_are_strings"] is True
            and citations["unique"] is True
            and citations["must_cite_pack_ids"] is True
        )
        assert (
            citations["min_per_statement"] == 1 and citations["min_per_relation"] == 2
        )
        assert contract["conditional_order_assessment"]["verdict_enum"] == list(
            qca.CONDITIONAL_VERDICTS
        )
        assert contract["confidence"]["level_enum"] == list(qca.CONFIDENCE_LEVELS)
    semantics = build_analyzer_request(with_orders)["task"]["output_contract"][
        "conditional_semantics"
    ]
    assert semantics["order_evidence_present"] is True
    assert semantics["must_cite_at_least_one_of"] == [
        qca._citation_map(with_orders)[key] for key in ("order:1",)
    ]
    assert semantics["default"] is None and semantics["allowed_verdicts"] == list(
        qca.CONDITIONAL_VERDICTS
    )
    bare = build_analyzer_request(no_orders)["task"]["output_contract"][
        "conditional_semantics"
    ]
    assert bare == {
        "order_evidence_present": False,
        "must_cite_at_least_one_of": [],
        "default": None,
        "allowed_verdicts": list(qca.CONDITIONAL_VERDICTS),
    }
    assert json.dumps(
        build_analyzer_request(with_orders), ensure_ascii=False
    )  # still JSON-safe


def test_adapter_wraps_injected_transport_exactly_once_and_validates():
    seen = []

    def transport(request):
        seen.append(request)
        return json.dumps(_wire_good_output(request), ensure_ascii=False)

    adapter = analyzer_adapter(transport)
    for count, (pack, symbol) in enumerate(
        ((_ADAPTER_PACK, "000001"), (_CALM_PACK, "000002")), 1
    ):
        result, reason = run_contextual_analysis(pack, analysis=adapter)
        assert reason == "validated" and result is not None and result.symbol == symbol
        assert len(seen) == count  # exactly one transport call per card, never retried


def test_adapter_classifies_each_failure_case_exactly():
    """m1: each malformed path reaches its intended branch, not any-rejection."""

    def reason_of(transport):
        result, reason = run_contextual_analysis(
            _ADAPTER_PACK, analysis=analyzer_adapter(transport)
        )
        assert result is None
        return reason

    def boom(_request):
        raise RuntimeError("timeout")

    def mutate(**fields):  # a good output with one field overridden
        return lambda request: json.dumps(
            dict(_wire_good_output(request), **fields), ensure_ascii=False
        )

    assert reason_of(boom) == "model_failure:RuntimeError"
    assert reason_of(lambda _r: "not json at all") == "model_failure:JSONDecodeError"
    assert (
        reason_of(lambda _r: json.dumps([_good_output(_ADAPTER_PACK)]))
        == "model_failure:ValueError"
    )
    assert (
        reason_of(lambda _r: json.dumps({"unrelated": True}))
        == "invalid_output:required_sections"
    )
    assert reason_of(mutate(symbol="000009")) == "invalid_output:symbol_mismatch"
    assert (
        reason_of(mutate(alternatives=[_statement("근거 없는 인용", "f:ghost")]))
        == "invalid_output:unknown_evidence_id"
    )
    result, reason = run_contextual_analysis(
        _ADAPTER_PACK, analysis=analyzer_adapter(_good_transport())
    )
    assert reason == "validated" and result is not None  # positive control


def test_adapter_never_persists_or_logs_raw_output(caplog):
    raw = json.dumps(_good_output(_ADAPTER_PACK), ensure_ascii=False)
    with caplog.at_level(logging.DEBUG):
        run_contextual_analysis(
            _ADAPTER_PACK, analysis=analyzer_adapter(lambda _r: raw)
        )
    assert raw not in caplog.text and caplog.text == ""


def test_send_path_never_runs_analysis_even_with_everything_injected(tmp_path, capsys):
    tool, root = card_tests._tool(), tmp_path / "reports"
    card_tests._store_report(
        root, "KR", (_card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),), "a"
    )
    gate = tmp_path / "gate.json"
    write_card_activation(
        gate,
        activation_id="quant-card-adapter-01",
        mode="CANARY",
        expires_at=NOW + timedelta(minutes=30),
    )
    env = {"TELEGRAM_STOCK_BOT_TOKEN": "tok", "TELEGRAM_STOCK_BOT_CHAT_ID": "42"}
    calls, sent = {"model": 0}, []

    def transport(request):
        calls["model"] += 1
        return json.dumps(_wire_good_output(request), ensure_ascii=False)

    paths = dict(
        holdings_path=tmp_path / "no_holdings.json",
        quotes_path=tmp_path / "no_quotes.sqlite3",
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "no_refs.sqlite3",
    )
    code = tool.main(
        [
            "send",
            "--activation-id",
            "quant-card-adapter-01",
            "--report-root",
            str(root),
        ],
        environ=env,
        clock=lambda: NOW,
        analysis=analyzer_adapter(transport),
        sender_factory=lambda token, chat: lambda text: sent.append(text) or 656,
        gate_path=gate,
        receipt_root=tmp_path / "receipts",
        **paths,
    )
    out = json.loads(capsys.readouterr().out)
    assert code == tool.EXIT_OK and out["result"]["sent"] >= 1
    # send never enables contextual analysis: receipt shows OFF with zero model starts
    assert out["reports"]["evidence"]["analysis"] == {
        "enabled": False,
        "model_calls": 0,
    }
    assert calls["model"] == 0 and all(ANALYSIS_LABEL not in text for text in sent)


@pytest.mark.parametrize("extra", [0, 105])
def test_wire_request_exact_mapping_and_immutable_fields(extra):
    pack = json.loads(json.dumps(_ADAPTER_PACK))
    pack["evidence"] += [
        {"id": f"custom:{i}", "kind": "factor", "label": "price"} for i in range(extra)
    ]
    gap = pack["gaps"][0]["id"]
    pack["evidence"].append(
        {"id": "ext:1", "kind": "external", "gap": gap, "label": gap}
    )
    pack["external"].update(gaps_open=[gap], gaps_closed=[gap])
    before = json.loads(json.dumps(pack))
    ids = sorted(_ids(pack))
    expected = {
        key: f"E{i:0{max(2, len(str(len(ids))))}d}" for i, key in enumerate(ids, 1)
    }
    assert qca._citation_map(pack) == expected
    request = build_analyzer_request(pack)
    assert request == build_analyzer_request(pack) and pack == before
    wire = request["evidence_pack"]
    for section in ("evidence", "gaps"):
        for original, encoded in zip(pack[section], wire[section]):
            changed = dict(original, id=expected[original["id"]])
            if original.get("kind") == "external":
                changed["gap"] = expected[original["gap"]]
            assert encoded == changed
    assert wire["external"] == dict(
        pack["external"], gaps_open=[expected[gap]], gaps_closed=[expected[gap]]
    )
    wire["evidence"][0]["label"] = "changed"
    assert pack == before


@pytest.mark.parametrize("bad_id", ["price", "gap:news_disclosure", "E01"])
def test_wire_ambiguous_ids_reject_before_transport(bad_id):
    pack = dict(
        _ADAPTER_PACK,
        evidence=_ADAPTER_PACK["evidence"] + [{"id": bad_id, "kind": "factor"}],
    )
    calls = []
    with pytest.raises(ValueError):
        analyzer_adapter(lambda request: calls.append(request))(pack)
    assert calls == []


def _map_reply(output, mapping):
    output = json.loads(json.dumps(output))
    for name in (*qca.LIST_SECTIONS, "conditional_order_assessment", "confidence"):
        entries = output.get(name)
        entries = entries if name in qca.LIST_SECTIONS else [entries]
        if isinstance(entries, list):
            for entry in entries:
                if isinstance(entry, dict) and isinstance(
                    entry.get("evidence_ids"), list
                ):
                    entry["evidence_ids"] = [
                        mapping.get(item, item) if isinstance(item, str) else item
                        for item in entry["evidence_ids"]
                    ]
    return output


@pytest.mark.parametrize("pack", [_ADAPTER_PACK, _CALM_PACK])
def test_wire_roundtrip_all_citation_locations(pack):
    good = _good_output(pack)
    good["assumptions"] = [good["alternatives"][0]]
    wire = _map_reply(good, qca._citation_map(pack))
    adapter = analyzer_adapter(lambda request: json.dumps(wire))
    assert adapter(pack) == good
    assert run_contextual_analysis(pack, analysis=adapter) == (
        validate_analysis(good, pack),
        "validated",
    )


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("alternatives", ["E00", "e01"], "unknown_evidence_id"),
        ("alternatives", ["price"], "unknown_evidence_id"),
        ("alternatives", [" E01"], "unknown_evidence_id"),
        ("alternatives", ["E001"], "unknown_evidence_id"),
        ("change_conditions", ["E01", "EE02"], "unknown_evidence_id"),
        ("alternatives", ["ghost", "ghost"], "unsupported_claim"),
        ("alternatives", ["E01", "E01"], "unsupported_claim"),
        ("alternatives", [None], "unsupported_claim"),
        ("alternatives", "E01", "unsupported_claim"),
        ("evidence_relations", ["E01"], "unsupported_claim"),
        ("confidence", ["ghost"], "unknown_evidence_id"),
        ("conditional_order_assessment", ["ghost"], "unknown_evidence_id"),
    ],
)
def test_wire_rejections_keep_canonical_reasons(field, value, reason):
    pack = dict(
        _ADAPTER_PACK,
        gaps=_ADAPTER_PACK["gaps"] + [{"id": "!unknown:ghost", "label": "欠落"}],
    )
    output = _map_reply(_good_output(pack), qca._citation_map(pack))
    entry = output[field][0] if field in qca.LIST_SECTIONS else output[field]
    if entry is None:
        entry = output[field] = {"verdict": "맞음", "statement": "근거 확인"}
    entry["evidence_ids"] = value
    assert run_contextual_analysis(
        pack, analysis=analyzer_adapter(lambda request: json.dumps(output))
    ) == (None, "invalid_output:" + reason)


@pytest.mark.parametrize(
    "field,value,reason",
    [
        ("alternatives", None, "section_alternatives"),
        ("alternatives", [None], "statement_shape"),
        ("confidence", None, "confidence_shape"),
        ("conditional_order_assessment", [], "conditional_shape"),
    ],
)
def test_wire_malformed_sections_reach_validator(field, value, reason):
    output = _map_reply(_good_output(_ADAPTER_PACK), qca._citation_map(_ADAPTER_PACK))
    output[field] = value
    assert run_contextual_analysis(
        _ADAPTER_PACK, analysis=analyzer_adapter(lambda request: json.dumps(output))
    ) == (None, "invalid_output:" + reason)


# Grounded actionable v2: synthetic inputs only, through the existing adapter.
def _actionable_output(pack):
    out = _good_output(pack)
    out.update(
        summary="장기 상승을 근거로 보유 유지를 검토하되 새 매수는 관찰구간과 비교합니다.",
        conditional_order_assessment=None,
    )
    out["evidence_relations"] = [
        _statement(
            "장기 수익률과 이동평균 대비 위치가 양수라 상승 추세가 유지됩니다.",
            "f:ret_120",
            "f:close_to_sma_120",
        )
    ]
    out["alternatives"] = [
        _statement(
            "보유 중이라면 추세가 이어지는 동안 유지를 검토합니다.", "f:ret_120"
        ),
        _statement(
            "가격 변동을 감당하기 어렵다면 비중 축소를 검토합니다.", "f:realized_vol_63"
        ),
    ]
    out["counterevidence"] = [
        _statement(
            "가격 변동성이 있어 상승 추세만으로 손실 위험이 사라지지는 않습니다.",
            "f:realized_vol_63",
        )
    ]
    out["change_conditions"] = [
        _statement(
            "이후 완료 세션에서 장기 수익률이 양수가 아니면 상승에 근거한 유지 판단을 재검토합니다.",
            "f:ret_120",
        )
    ]
    out["falsifiers"] = list(out["change_conditions"])
    out["confidence"] = {
        "level": "보통",
        **_statement(
            "장기 추세와 변동성을 함께 확인합니다.", "f:ret_120", "f:realized_vol_63"
        ),
    }
    return out


def _actionable_analysis(pack):
    output = _actionable_output(pack)
    wire = _map_reply(output, qca._citation_map(pack))
    return analyzer_adapter(lambda request: json.dumps(wire, ensure_ascii=False))(pack)


@pytest.mark.parametrize(
    "market,price,expected", [("KR", "1234.5", "1,235원"), ("US", "123.455", "$123.46")]
)
def test_actionable_display_fidelity(market, price, expected):
    card = _card(
        "NVDA" if market == "US" else "000001",
        market,
        "SYNTHETIC",
        {
            **VALUES_CALM,
            "ret_20": "-0.01235",
            "ret_5": "0",
            "macd_histogram": "-1.5",
            "downside_vol_63": "0.25",
            "price_position_200": "0.75",
            "volume_change_20": "0.01",
        },
    )
    pack = build_evidence_pack(
        card,
        CardContextV1(
            card.symbol,
            QuoteContextV1(
                Decimal(price), market == "KR" and "KRW" or "USD", None, "fixture"
            ),
        ),
        completed_session_date="2026-09-02",
    )
    result = validate_analysis(_actionable_analysis(pack), pack)
    assert result.labels["price"]["display"] == expected
    assert result.labels["price"]["asof"] == "가격 관측시각 미기록"
    assert result.labels["f:ret_20"]["display"] == "-1.2%"
    assert result.labels["f:ret_5"]["display"] == "0.0%"
    assert result.labels["f:rsi_14"]["display"] == "55.0점"
    assert result.labels["f:ret_120"]["asof"] == "2026-09-02"
    assert result.labels["f:downside_vol_63"]["display"] == "25.0%"
    assert result.labels["f:price_position_200"]["display"] == "75.0%"
    assert result.labels["f:volume_change_20"]["display"] == "+1.0%"
    assert result.labels["f:macd_histogram"]["display"] == (
        "-2원" if market == "KR" else "$-1.50"
    )


def test_actionable_unknown_orders_are_analytically_and_visibly_absent():
    card = _card("000001", "KR", "SYNTHETIC", VALUES_CALM)
    base = CardContextV1(card.symbol, _QUOTE("3000000"))
    unknown = dataclasses.replace(
        base,
        conditional=ConditionalContextV1(
            "ACTIVE", "UNKNOWN PRIVATE DIAGNOSTIC", (_order_ctx("2300000", side=None),)
        ),
    )
    packs = [
        build_evidence_pack(card, ctx, completed_session_date="2026-09-02")
        for ctx in (base, unknown)
    ]
    assert build_analyzer_request(packs[0]) == build_analyzer_request(packs[1])
    texts = [
        render_market_message(
            _report("KR", (card,)), (ctx,), report_sha256="a" * 64
        ).parts
        for ctx in (base, unknown)
    ]
    assert texts[0] == texts[1]
    assert "미보유" not in texts[0][0] and "관심종목 아님" not in texts[0][0]
    assert "최상위" not in texts[0][0] and "가격 관측" in texts[0][0]
    zoned = render_market_message(
        _report("KR", (dataclasses.replace(card, entry_zone=ZONE),)),
        (base,),
        report_sha256="a" * 64,
    ).parts[0]
    assert "핵심 실적 가설" not in zoned and "익절·비중상한" not in zoned
    known = dataclasses.replace(
        base,
        conditional=ConditionalContextV1(
            "ACTIVE", None, (dataclasses.replace(_order_ctx("2300000"), side="BUY"),)
        ),
    )
    pack = build_evidence_pack(card, known, completed_session_date="2026-09-02")
    assert (
        validate_analysis(_actionable_output(pack), pack).conditional_order_assessment
        is None
    )
    assessment = {
        **_statement(
            "확인된 매수 발동가와 장기 상승 근거를 함께 검토합니다.",
            "order:1",
            "f:ret_120",
        ),
        "verdict": "맞음",
    }
    output = {**_actionable_output(pack), "conditional_order_assessment": assessment}
    checked = validate_analysis(output, pack)
    assert "2,300,000원" in checked.labels["order:1"]["display"]
    assert checked.labels["rel:order:1"]["display"] != ""


def test_actionable_reversal_requires_observed_factor_or_actual_bound():
    pack = _pack()
    out = _actionable_output(pack)
    out["change_conditions"] = [
        _statement("흐름이 바뀌면 재검토합니다.", "price", "gap:news_disclosure")
    ]
    with pytest.raises(AnalysisValidationError, match="observable_reversal"):
        validate_analysis(out, pack)


@pytest.mark.parametrize(
    "position,price,word",
    [
        ("above_upper", "3000000", "추격"),
        ("inside", "2300000", "구간 안"),
        ("below_invalidation", "2000000", "무효화"),
        ("missing", "2300000", "이유는"),
    ],
)
def test_actionable_whole_card_and_two_card_market(position, price, word):
    from tokmo.programs.quant_telegram_card import _card_block, compute_standings

    values = {
        **VALUES_CALM,
        "ret_60": "0.1",
        "macd_histogram": "50",
        "downside_vol_63": "0.2",
        "price_position_200": "0.7",
        "volume_change_20": "0.05",
        "trading_amount_change_20": "0.07",
    }
    cards = tuple(
        _card(
            symbol,
            "KR",
            "SYNTHETIC " + symbol,
            values,
            None if position == "missing" else ZONE,
        )
        for symbol in ("000001", "000002")
    )
    contexts = tuple(
        dataclasses.replace(
            card_tests._kr_contexts()[0],
            symbol=card.symbol,
            quote=_QUOTE(price),
            watchlisted=True,
            report_verified=True,
            quote_age_valid=position != "below_invalidation",
            conditional=ConditionalContextV1(
                "ACTIVE",
                None,
                (_order_ctx("2300000", "2290000"), _order_ctx("2100000", side=None)),
            ),
            quote_validity_reason="시세 관측시각 오래됨 (관측시각 2026-08-20 · 기준 7일)"
            if position == "below_invalidation"
            else None,
        )
        for card in cards
    )
    analyses = {}
    for card, ctx in zip(cards, contexts):
        pack = build_evidence_pack(
            card,
            ctx,
            completed_session_date="2026-09-02",
            context_items=(
                ContextItemV1(
                    "news",
                    "합성 발행사의 사업 진행 소식",
                    "2026-09-02",
                    "SYNTHETIC NEWS",
                ),
                ContextItemV1(
                    "disclosure",
                    "합성 발행사의 공시 제목",
                    "2026-09-02",
                    "SYNTHETIC 공시",
                ),
                ContextItemV1(
                    "user_plan",
                    "합성 투자계획은 추세 유지 시 보유 검토",
                    "2026-09-02",
                    "SYNTHETIC 계획",
                ),
                ContextItemV1(
                    "trade_reason",
                    "합성 매매사유는 장기 추세 관찰",
                    "2026-09-02",
                    "SYNTHETIC 기록",
                ),
            ),
        )
        from tokmo.programs.quant_finite_canary import checked_prompt

        assert checked_prompt(pack)
        output = _actionable_analysis(pack)
        if position == "inside":
            output["conditional_order_assessment"] = {
                **_statement(
                    "확인된 매수 발동가가 관찰구간 안이므로 구간 기준과 일치합니다.",
                    "order:1",
                    "zone",
                ),
                "verdict": "맞음",
            }
        analysis = validate_analysis(output, pack)
        analyses[card.symbol] = analysis
        text = _card_block(card, ctx, compute_standings(cards), set(), [], analysis)
        titles = [
            "투자판단",
            "왜 이렇게 보나",
            "어떻게 대응하나",
            "판단을 바꿀 때",
            "지표 요약",
            "보유 현황",
        ]
        assert [text.index(title) for title in titles] == sorted(
            text.index(title) for title in titles
        )
        assert "+50.0%" in text.split("왜 이렇게 보나")[0]
        assert word in text and "가격 관측" in text and "2026-09-02" in text
        if position == "inside":
            assert "최신 저장 리포트 확인" in text and "가격 관측 7일 이내" in text
        elif position == "below_invalidation":
            assert "시세 관측시각 오래됨" in text and "과거/참고 조건" in text
        assert "4주" in text and "1,900,000원" in text and "60.0%" in text
        assert (
            "행동 제안" not in text
            and "최상위" not in text
            and "조건주문 현황" not in text
        )
        assert card_tests._max_cells(text) <= 44
        assert len(text.encode("utf-16-le")) // 2 <= 2000
        if position != "missing":
            action = text.split("어떻게 대응하나")[1].split("판단을 바꿀 때")[0]
            assert all(
                value in action
                for value in (
                    "2,250,000원",
                    "2,450,000원",
                    "2,150,000원",
                    "유효 1완료 세션",
                )
            )
    message = render_market_message(
        _report("KR", cards), contexts, report_sha256="a" * 64, analyses=analyses
    )
    assert (
        len(message.parts) == 1
        and len(message.parts[0].encode("utf-16-le")) // 2 <= 4096
    )
    huge = dataclasses.replace(
        analyses["000001"],
        alternatives=tuple([_statement("가나다 " * 50, "f:ret_120")] * 5),
        counterevidence=tuple([_statement("가나다 " * 50, "f:realized_vol_63")] * 5),
        summary="가나다 " * 200,
    )
    with pytest.raises(ValueError, match="cap"):
        render_market_message(
            _report("KR", cards),
            contexts,
            report_sha256="a" * 64,
            analyses={**analyses, "000001": huge},
        )


@pytest.mark.parametrize(
    "valid,quote_price,expected",
    [(False, "80", "위입니다"), (True, "80", "아래입니다"), (False, None, "위입니다")],
)
def test_actionable_quote_age_selects_actual_session_basis(
    valid, quote_price, expected
):
    from tokmo.programs.quant_telegram_card import (
        _card_block,
        _zone_action_lines,
        compute_standings,
    )

    zone = dataclasses.replace(
        ZONE,
        lower=Decimal(95),
        upper=Decimal(105),
        invalidation=Decimal(90),
        latest_close=Decimal(110),
    )
    card = _card("NVDA", "US", "SYNTHETIC", VALUES_CALM, zone)
    quote = (
        None
        if quote_price is None
        else QuoteContextV1(
            Decimal(quote_price),
            "USD",
            NOW if valid else NOW - timedelta(days=14),
            "SYNTHETIC",
        )
    )
    ctx = CardContextV1(
        "NVDA",
        quote=quote,
        quote_age_valid=valid,
        quote_validity_reason=None if valid else "시세 관측시각 오래됨",
    )
    action = _zone_action_lines(card, ctx, "2026-09-02")[0]
    assert expected in action
    assert ("기록 가격은" if valid else "기준 세션 종가는") in action
    if not valid:
        assert "$95.00~$105.00" in action and "추격을 보류" in action
    pack = build_evidence_pack(card, ctx, completed_session_date="2026-09-02")
    if quote is not None:
        price = next(e for e in pack["evidence"] if e["id"] == "price")
        assert price["value"] == "80"
        assert price["age_valid"] is valid
        if not valid:
            assert price["validity_reason"] == ctx.quote_validity_reason
            assert "참고 전용" in price["label"]
        else:
            assert price["label"] == "기록 가격"
    assert (
        next(e for e in pack["evidence"] if e["id"] == "session_close")["value"]
        == "110"
    )
    analysis = validate_analysis(_actionable_analysis(pack), pack)
    rendered = _card_block(card, ctx, compute_standings((card,)), set(), [], analysis)
    assert "기준 세션 종가 $110.00" in rendered
    if quote is not None:
        assert "기록 가격 $80.00" in rendered
    if not valid:
        assert "기준 세션 종가는" in rendered and "추격을 보류" in rendered
        assert "기록 가격은 무효화" not in rendered


def test_actionable_ground_dedupe_kst_and_weekly_missing_date():
    from tokmo.programs.quant_telegram_card import _card_block, compute_standings

    card = _card("000001", "KR", "SYNTHETIC", VALUES_CALM, ZONE)
    ctx = dataclasses.replace(card_tests._kr_contexts()[0], quote=_QUOTE("2300000"))
    pack = build_evidence_pack(card, ctx, completed_session_date="2026-09-02")
    analysis = validate_analysis(_actionable_analysis(pack), pack)
    text = _card_block(card, ctx, compute_standings((card,)), set(), [], analysis)
    reasons = text.split("왜 이렇게 보나")[1].split("어떻게 대응하나")[0]
    assert "120일 수익률" not in reasons
    assert "가격 관측 2026-09-03 10:00 KST" in text
    assert not any(line.strip() == "2026-09-02" for line in text.splitlines())
    report = _report("KR", (card,))
    weekly = dataclasses.replace(
        report,
        identity=dataclasses.replace(
            report.identity, completed_session_date=None, completed_iso_week="2026-W36"
        ),
    )
    rendered = render_market_message(weekly, (ctx,), report_sha256="a" * 64).parts[0]
    assert "None" not in rendered and "세션 미기록" in rendered
