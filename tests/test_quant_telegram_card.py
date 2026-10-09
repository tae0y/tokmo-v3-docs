"""Narrow TDD for the Quant Telegram decision-card vertical (contract 2026-09-03,
cycle-2 presentation): recipient/gate, one-attempt cap/dedupe/UNKNOWN,
deterministic rendering with the approved line breaks, the no-order boundary,
and the exact two-market split."""

import importlib.util
import json
import unicodedata
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.programs.quant_telegram_card import (
    MESSAGE_CHAR_CAP,
    CardContextV1,
    ConditionalContextV1,
    ConditionalOrderContextV1,
    HoldingContextV1,
    MarketMessageV1,
    PlaybookSynthesisV1,
    QuoteContextV1,
    WeightContextV1,
    extract_playbook_layers,
    render_decision_cards,
    render_market_message,
    run_quant_telegram_card_canary,
    telegram_pre_message,
)
from tokmo.records.quant_telegram_card_receipts import (
    QuantTelegramCardReceiptStore,
    read_card_activation,
    write_card_activation,
)
from tokmo.values.instrument_display import display_or_symbol
from tokmo.values.quant_discovery import (
    DecisionCardV1,
    FactorOrientation,
    FactorStatus,
    FactorValueV1,
    QuantMarketProfileV1,
    QuantProductKind,
    QuantRunIdentityV1,
    QuantRunReportV1,
    ReferenceEntryZoneV1,
)

NOW = datetime(2026, 9, 3, 1, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))
TOOL = Path(__file__).parents[1] / "tools" / "run_quant_telegram_card.py"
SRC = Path(__file__).parents[1] / "src" / "tokmo"
_LOWER = {"realized_vol_63", "downside_vol_63"}


def _tool():
    spec = importlib.util.spec_from_file_location("run_quant_telegram_card", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _card(symbol, market, name, values, zone=None):
    factors = tuple(
        FactorValueV1(
            key,
            None if value is None else Decimal(value),
            FactorOrientation.LOWER_IS_BETTER
            if key in _LOWER
            else FactorOrientation.HIGHER_IS_BETTER,
            FactorStatus.INSUFFICIENT if value is None else FactorStatus.AVAILABLE,
            200,
            2,
        )
        for key, value in values.items()
    )
    return DecisionCardV1(
        symbol, QuantMarketProfileV1(market), name, factors, (), zone, ()
    )


def _report(market, cards, digest_seed="a"):
    identity = QuantRunIdentityV1(
        QuantProductKind.MARKET_DISCOVERY,
        QuantMarketProfileV1(market),
        date(2026, 9, 2),
        None,
        "quant-profile-v1",
        None,
        "quant-report-v1",
        digest_seed * 64,
    )
    return QuantRunReportV1(identity, tuple(cards), (), (("RANKINGS", 1),))


VALUES_HOT = {
    "ret_5": "0.182",
    "ret_20": "0.447",
    "ret_120": "1.65",
    "close_to_sma_20": "0.10",
    "close_to_sma_60": "0.20",
    "close_to_sma_120": "0.40",
    "rsi_14": "84.3",
    "realized_vol_63": "0.965",
    "max_drawdown": "-0.214",
    "risk_adjusted_momentum": "1.7",
}
VALUES_CALM = {
    "ret_5": "0.01",
    "ret_20": "0.02",
    "ret_120": "0.5",
    "close_to_sma_20": "0.01",
    "close_to_sma_60": "0.02",
    "close_to_sma_120": "0.05",
    "rsi_14": "55.0",
    "realized_vol_63": "0.3",
    "max_drawdown": "-0.1",
    "risk_adjusted_momentum": "0.6",
}
ZONE = ReferenceEntryZoneV1(
    Decimal(2250000),
    Decimal(2450000),
    "TREND_PULLBACK",
    ("SMA20", "SMA60", "ATR14", "support20"),
    Decimal(2150000),
    1,
    Decimal(2900000),
    Decimal("-0.224"),
    50,
    "단일 가격·거래량 setup",
    "close <= x",
)
SYNTHESIS = PlaybookSynthesisV1("개인계좌", True, True, 3, "BUY", "HOLD")
WEIGHT = WeightContextV1(
    Decimal("0.6"), "개인계좌 KR 주식", None, None, "USD/KRW 환율 근거 없음"
)
ACTIVE = ConditionalContextV1(
    "ACTIVE",
    None,
    (
        ConditionalOrderContextV1(
            "개인계좌",
            "SINGLE",
            "LIMIT",
            "2",
            "STOP_LOSS",
            "WATCHING",
            Decimal(2300000),
            Decimal(2290000),
            "2026-09-30",
        ),
    ),
)


def _kr_report():
    return _report(
        "KR",
        (
            _card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),
            _card("000002", "KR", "평온산업", VALUES_CALM),
        ),
    )


def _kr_contexts():
    quote = QuoteContextV1(
        Decimal(3000000),
        "KRW",
        datetime(2026, 9, 3, 10, 0, tzinfo=KST),
        "최근 시세 스토어",
    )
    return (
        CardContextV1(
            "000001",
            quote,
            None,
            (HoldingContextV1("개인계좌", "4", "1,900,000원", WEIGHT),),
            True,
            False,
            SYNTHESIS,
            None,
            ACTIVE,
        ),
        CardContextV1(
            "000002",
            None,
            "최근 시세 스토어에 종목 없음",
            (),
            True,
            True,
            None,
            "플레이북 문서 없음",
            ConditionalContextV1("NONE"),
        ),
    )


# --- deterministic rendering with the approved line breaks ------------------


def _max_cells(text):
    """Widest line in display cells (CJK = 2), the phone-width unit of the contract."""
    return max(
        sum(
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            for char in line
        )
        for line in text.split("\n")
    )


def test_render_is_deterministic_and_keeps_approved_line_breaks():
    first = render_market_message(_kr_report(), _kr_contexts(), report_sha256="d" * 64)
    assert first == render_market_message(
        _kr_report(), _kr_contexts(), report_sha256="d" * 64
    )
    text = first.parts[0]
    assert text.startswith("📊 퀀트 KR\n2026-09-02\n카드 2종목")
    assert "투자판단\n규칙 기반 · AI 분석 미연결" in text
    assert "보유 종목" in text and "관심종목" in text
    assert "기록 가격 3,000,000원\n가격 관측 2026-09-03 10:00 KST" in text
    assert "2,250,000원~2,450,000원" in text and "2,150,000원" in text
    assert "과거/참고 조건" in text and "유효 1완료 세션" in text
    assert "연환산 63일 변동성 96.5%" in text and "과거 최대낙폭 -21.4%" in text
    assert "보유 : 4주 / 평단 : 1,900,000원\n비중 : 국내 60.0%" in text
    assert "비중 기준: 각 보유계좌의 국내 주식 평가액" in text
    assert text.count("이번 리포트의 비교 대상 2종목") == 2
    assert text.count("```") == 4 and _max_cells(text) <= 44
    for gone in (
        "미보유",
        "관심종목 아님",
        "최상위",
        "핵심 실적 가설",
        "익절·비중상한",
        "조건주문 현황",
        "플레이북\n",
    ):
        assert gone not in text
    assert (
        "000001:계좌 전체 비중",
        "USD/KRW 환율 근거 없음",
    ) in first.unavailable_sections


def test_render_labels_unavailable_sections_instead_of_inventing():
    message = render_market_message(
        _kr_report(), _kr_contexts(), report_sha256="d" * 64
    )
    block = message.parts[0].split("• 평온산업(000002)", 1)[1]
    assert "기록 가격 없음" in block and "관심종목" in block
    assert (
        "구간이 없는 이유는" in block.replace("\n", " ")
        and "매수 확인 신호는 아직" in block
    )
    assert all(
        word not in block
        for word in ("미보유", "보유 현황", "조건주문 현황", "플레이북\n")
    )
    assert (
        "000002:진입구간",
        "report entry_zone is null",
    ) in message.unavailable_sections
    assert ("000002:플레이북", "플레이북 문서 없음") in message.unavailable_sections
    assert (
        "000002:현재가",
        "최근 시세 스토어에 종목 없음",
    ) in message.unavailable_sections


def test_conditional_side_groups_cap_two_each_and_hidden_count_once():
    """Side comes only from the provider-side field on the context (None today, see
    test_quant_card_cycle2): BUY ≤2, SELL ≤2, unknown ≤2 in input order per group, the
    hidden remainder summarised once; equal trigger/order price is one combined line.
    Compact shape: index, prices, 거래구분, 만료일 — no scope label, leg type/status,
    price gap or zone relation."""

    def order(tag, trigger, price, side, order_type="LIMIT"):
        return ConditionalOrderContextV1(
            "개인계좌",
            "SINGLE",
            order_type,
            "2",
            tag,
            "WATCHING",
            None if trigger is None else Decimal(trigger),
            None if price is None else Decimal(price),
            "2026-09-30",
            side,
        )

    orders = (
        order("B1", "2300000", "2300000", "BUY"),
        order("S1", "2600000", None, "SELL", "MARKET"),
        order("B2", "2200000", "2190000", "BUY"),
        order("U1", "2100000", "2090000", None),
        order("B3", "2000000", "1990000", "BUY"),
        order("S2", "2700000", "2690000", "SELL"),
        order("S3", "2800000", "2790000", "SELL"),
        order("U2", None, None, None, "MARKET"),
    )
    context = CardContextV1(
        "000001",
        None,
        "x",
        (),
        True,
        False,
        None,
        None,
        ConditionalContextV1("ACTIVE", None, orders),
    )
    text = render_market_message(
        _report("KR", (_card("000001", "KR", "테스트전자", VALUES_HOT, ZONE),)),
        (context,),
        report_sha256="d" * 64,
    ).parts[0]
    assert "조건주문 현황" not in text and "거래구분" not in text
    # Legacy helper grouping stays unchanged for historical rendering.
    from tokmo.programs.quant_telegram_card import grouped_conditional_orders

    selected, hidden = grouped_conditional_orders(orders)
    assert len(selected) == 6 and hidden == 2
    assert [order.side for order in selected] == [
        "BUY",
        "BUY",
        "SELL",
        "SELL",
        None,
        None,
    ]
    with pytest.raises(ValueError):
        ConditionalOrderContextV1(
            "개인계좌",
            "SINGLE",
            "LIMIT",
            "2",
            "X",
            "WATCHING",
            None,
            None,
            None,
            "LONG",
        )


def test_render_privacy_guard_rejects_private_body():
    with pytest.raises(ValueError, match="privacy"):
        render_market_message(
            _report("KR", (_card("000001", "KR", "credential-x", VALUES_HOT),)),
            (),
            report_sha256="d" * 64,
        )


def test_render_splits_only_at_card_boundaries_when_over_cap():
    cards = tuple(
        _card(f"00000{i}", "KR", f"종목{i}", VALUES_HOT, ZONE) for i in range(1, 9)
    )
    contexts = tuple(
        CardContextV1(
            card.symbol,
            None,
            "x",
            (HoldingContextV1("개인계좌", "4", "1,900,000원", WEIGHT),),
            True,
            False,
            SYNTHESIS,
            None,
            ACTIVE,
        )
        for card in cards
    )
    message = render_market_message(
        _report("KR", cards), contexts, report_sha256="d" * 64
    )
    assert len(message.parts) >= 2
    header = "📊 퀀트 KR\n2026-09-02\n카드 8종목\n\n"
    for part in message.parts:
        assert part.startswith(header)
        assert len(part.encode("utf-16-le")) // 2 <= MESSAGE_CHAR_CAP
    joined = "\n".join(message.parts)
    for card in cards:
        assert joined.count("• " + display_or_symbol("KR", card.symbol, card.name)) == 1


def test_playbook_layer_extraction_reads_both_markers_or_none():
    text = (
        "---\n---\n<!-- HERMES_CURRENT_START -->\n## 1층\n헤르 판단\n<!-- HERMES_CURRENT_END -->\n"
        "<!-- USER_THESIS_START -->\n## 2층\n> 안내\n- `source`: x\n사용자 원문\n<!-- USER_THESIS_END -->\n"
    )
    thesis, hermes = extract_playbook_layers(text)
    assert "사용자 원문" in thesis and "헤르 판단" in hermes
    assert extract_playbook_layers("no markers") == (None, None)


# --- monospace return table: fences → one Telegram ``pre`` entity at the send boundary ----


def test_live_sender_posts_return_table_as_pre_entity_with_utf16_offsets():
    import urllib.parse

    from tokmo.facts.telegram_dialogue_live import send_dialogue_message

    text = render_market_message(
        _kr_report(), _kr_contexts(), report_sha256="d" * 64
    ).parts[0]
    body, entities = telegram_pre_message(text)
    assert "```" not in body and len(entities) == 2 and "```" in text
    units = body.encode(
        "utf-16-le"
    )  # the 📊 surrogate pair before the table makes code-point offsets wrong

    def block(entity):
        return units[
            entity["offset"] * 2 : (entity["offset"] + entity["length"]) * 2
        ].decode("utf-16-le")

    assert all(entity["type"] == "pre" for entity in entities)
    assert block(entities[0]) == " 5일   /  20일  /  120일\n+18.2% / +44.7% / +165.0%"
    assert block(entities[1]) == " 5일  / 20일  / 120일\n+1.0% / +2.0% / +50.0%"
    assert (
        body.replace("\n", "|").count("|수익률| 5일") == 2
    )  # the fence lines themselves are gone
    calls = []

    def opener(url, form, timeout):
        calls.append(form)
        return b'{"ok": true, "result": {"message_id": 9}}'

    assert (
        send_dialogue_message(
            body_text=body, bot_token="t", chat_id="7", entities=entities, opener=opener
        )
        == 9
    )
    fields = dict(urllib.parse.parse_qsl(calls[0].decode("utf-8")))
    assert fields["text"] == body and json.loads(fields["entities"]) == list(entities)
    assert "parse_mode" not in fields and "reply_markup" not in fields
    send_dialogue_message(body_text="x", bot_token="t", chat_id="7", opener=opener)
    assert (
        b"entities" not in calls[1]
    )  # legacy form stays byte-identical without entities
    # the tool's live sender wires the conversion; fence-free text keeps the legacy form
    tool = _tool()
    seen = []
    sender = tool.pre_entity_sender(
        "tok", "42", send=lambda **kwargs: seen.append(kwargs) or 5
    )
    assert sender(text) == 5
    assert seen[-1] == {
        "body_text": body,
        "bot_token": "tok",
        "chat_id": "42",
        "entities": entities,
        "protect_content": False,
    }
    sender("plain")
    assert seen[-1] == {
        "body_text": "plain",
        "bot_token": "tok",
        "chat_id": "42",
        "entities": None,
        "protect_content": False,
    }
    with pytest.raises(ValueError):
        telegram_pre_message("```\nunterminated")


# --- exact two-market split ---------------------------------------------------


def test_exactly_two_market_messages_with_market_pure_symbols():
    us = _report("US", (_card("NVDA", "US", "엔비디아", VALUES_CALM),), digest_seed="b")
    messages = render_decision_cards(
        {"KR": (_kr_report(), "d" * 64), "US": (us, "e" * 64)},
        {"KR": _kr_contexts(), "US": ()},
    )
    assert tuple(message.market for message in messages) == ("KR", "US")
    assert "NVDA" not in messages[0].parts[0] and "000001" not in messages[1].parts[0]
    assert messages[1].parts[0].startswith("📊 퀀트 US\n2026-09-02\n카드 1종목\n\n")
    assert messages[1].report_sha256 == "e" * 64
    missing = render_decision_cards({"KR": (_kr_report(), "d" * 64)}, {})
    assert missing[1].market == "US" and missing[1].parts[0].startswith(
        "📊 퀀트 US\n리포트 없음"
    )
    with pytest.raises(ValueError):
        render_market_message(
            _report("KR", (_card("NVDA", "US", "x", VALUES_CALM),)),
            (),
            report_sha256="d" * 64,
        )


# --- recipient / gate ---------------------------------------------------------


def test_gate_is_off_unless_exact_unexpired_canary(tmp_path):
    gate = tmp_path / "gate.json"
    assert read_card_activation(gate, now=NOW)["mode"] == "OFF"
    gate.write_text("{bad json")
    assert read_card_activation(gate, now=NOW) == {
        "mode": "OFF",
        "activation_id": None,
        "expires_at": None,
        "reason": "gate_invalid",
    }
    write_card_activation(
        gate,
        activation_id="quant-card-canary-01",
        mode="CANARY",
        expires_at=NOW + timedelta(minutes=30),
    )
    assert (gate.stat().st_mode & 0o777) == 0o600
    active = read_card_activation(gate, now=NOW)
    assert (
        active["mode"] == "CANARY" and active["activation_id"] == "quant-card-canary-01"
    )
    assert (
        read_card_activation(gate, now=NOW + timedelta(minutes=31))["reason"]
        == "gate_expired"
    )
    write_card_activation(
        gate,
        activation_id="quant-card-canary-01",
        mode="OFF",
        expires_at=NOW + timedelta(days=1),
    )
    assert read_card_activation(gate, now=NOW)["reason"] == "gate_off"
    payload = json.loads(gate.read_bytes())
    payload["extra"] = 1
    gate.write_text(json.dumps(payload))
    assert read_card_activation(gate, now=NOW)["reason"] == "gate_shape"
    with pytest.raises(ValueError):
        write_card_activation(
            gate, activation_id="Bad Id", mode="CANARY", expires_at=NOW
        )


def test_recipient_bound_only_to_stock_bot_env_and_never_printed(tmp_path, capsys):
    tool = _tool()
    assert tool.bind_stock_bot_env({}) is None
    assert tool.bind_stock_bot_env({"TELEGRAM_STOCK_BOT_TOKEN": "t"}) is None
    assert tool.bind_stock_bot_env(
        {"TELEGRAM_STOCK_BOT_TOKEN": "tok-1", "TELEGRAM_STOCK_BOT_CHAT_ID": "42"}
    ) == ("tok-1", "42")
    assert tool.bind_stock_bot_env(
        {"TELEGRAM_STOCK_BOT": "tok-2", "TELEGRAM_CHAT_ID": "43"}
    ) == ("tok-2", "43")
    gate = tmp_path / "gate.json"
    write_card_activation(
        gate,
        activation_id="quant-card-canary-01",
        mode="CANARY",
        expires_at=NOW + timedelta(minutes=30),
    )
    code = tool.main(
        [
            "send",
            "--activation-id",
            "quant-card-canary-01",
            "--report-root",
            str(tmp_path / "none"),
        ],
        environ={},
        clock=lambda: NOW,
        gate_path=gate,
        receipt_root=tmp_path / "receipts",
    )
    assert code == tool.EXIT_REFUSED
    assert json.loads(capsys.readouterr().out)["off"] == "recipient_unbound"
    assert not (tmp_path / "receipts").exists() or not list(
        (tmp_path / "receipts").glob("*.json")
    )


# --- one-attempt cap / dedupe / UNKNOWN -------------------------------------


def _messages():
    return (
        MarketMessageV1("KR", "2026-09-02", "a" * 64, "d" * 64, ("KR body",), (), ()),
        MarketMessageV1("US", "2026-09-02", "b" * 64, "e" * 64, ("US body",), (), ()),
    )


def _activation():
    return {
        "mode": "CANARY",
        "activation_id": "quant-card-canary-01",
        "expires_at": None,
        "reason": None,
    }


def test_reservation_precedes_transport_and_second_run_claims_nothing(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    seen = []

    def send(text):
        receipt = receipts.read("quant-card-canary-01")
        assert receipt is not None and receipt["status"] == "RESERVED"
        seen.append(text)
        return 650 + len(seen)

    result = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert (
        result["off"] is None and result["sent"] == 2 and seen == ["KR body", "US body"]
    )
    receipt = receipts.read("quant-card-canary-01")
    assert receipt["status"] == "SENT" and receipt["attempt_cap"] == 1
    assert [
        (part["key"], part["provider_message_id"]) for part in receipt["parts"]
    ] == [("KR:1", "651"), ("US:1", "652")]
    assert receipt["report_digests"] == {"KR": "d" * 64, "US": "e" * 64}
    again = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert again["off"] == "cap_consumed" and len(seen) == 2
    other = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-02",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert other["off"] == "activation_mismatch" and len(seen) == 2


def test_kr_timeout_is_unknown_us_still_gets_one_attempt_and_rerun_sends_neither(
    tmp_path,
):
    """Live defect 2026-09-03: KR timed out (message actually arrived), US was
    aborted. Markets must settle independently; a later run resends neither."""
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    starts = []

    def send(text):
        starts.append(text)
        if text.startswith("KR"):
            raise TimeoutError  # client timeout after a possible/actual delivery
        return 654

    result = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert result["unknown"] == 1 and result["sent"] == 1 and result["skipped"] == 0
    assert starts == ["KR body", "US body"]
    receipt = receipts.read("quant-card-canary-01")
    assert receipt["status"] == "UNKNOWN"
    assert [
        (part["key"], part["status"], part["provider_message_id"])
        for part in receipt["parts"]
    ] == [("KR:1", "UNKNOWN", None), ("US:1", "SENT", "654")]
    again = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert again["off"] == "cap_consumed" and starts == ["KR body", "US body"]
    activation = dict(_activation(), activation_id="quant-card-canary-02")
    rerun = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id="quant-card-canary-02",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
    )
    assert rerun["off"] == "report_already_attempted" and starts == [
        "KR body",
        "US body",
    ]
    assert rerun["deduped"] == {
        "KR": "quant-card-canary-01:UNKNOWN",
        "US": "quant-card-canary-01:SENT",
    }
    assert receipts.read("quant-card-canary-02") is None
    forced = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id="quant-card-canary-02",
        messages=_messages(),
        telegram_send=send,
        now=NOW,
        override_report_dedupe=True,
    )
    # override may resend a SENT report, but an UNKNOWN part is never resent
    assert forced["deduped"] == {"KR": "quant-card-canary-01:UNKNOWN"} and starts[
        2:
    ] == ["US body"]


LEGACY_LIVE_RECEIPT = {
    "activation_id": "quant-card-canary-20260903-02",
    "attempt_cap": 1,
    "parts": [
        {
            "key": "KR:1",
            "provider_message_id": None,
            "reason": "provider_timeout",
            "settled_at": "2026-09-03T01:14:26.429504+00:00",
            "status": "UNKNOWN",
        },
        {
            "key": "US:1",
            "provider_message_id": None,
            "reason": "aborted_after_failure",
            "settled_at": "2026-09-03T01:14:26.429504+00:00",
            "status": "SKIPPED",
        },
    ],
    "report_digests": {"KR": "d" * 64, "US": "e" * 64},
    "reserved_at": "2026-09-03T01:14:26.429504+00:00",
    "schema_version": 1,
    "status": "UNKNOWN",
}


def test_recovery_of_legacy_skipped_us_sends_only_us_and_never_kr(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    (tmp_path / "receipts" / "quant-card-canary-20260903-02.receipt.json").write_text(
        json.dumps(LEGACY_LIVE_RECEIPT)
    )
    assert receipts.report_attempted("d" * 64) == (
        "quant-card-canary-20260903-02",
        "UNKNOWN",
    )
    assert (
        receipts.report_attempted("e" * 64) is None
    )  # SKIPPED never reached transport
    starts = []
    activation = dict(_activation(), activation_id="quant-card-recover-us-01")
    result = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id="quant-card-recover-us-01",
        messages=_messages(),
        telegram_send=lambda text: starts.append(text) or 655,
        now=NOW,
    )
    assert starts == ["US body"] and result["sent"] == 1 and result["off"] is None
    assert result["deduped"] == {"KR": "quant-card-canary-20260903-02:UNKNOWN"}
    receipt = receipts.read("quant-card-recover-us-01")
    assert receipt["status"] == "SENT" and receipt["report_digests"] == {"US": "e" * 64}
    assert receipt["deduped"] == {"KR": "quant-card-canary-20260903-02:UNKNOWN"}
    assert [part["key"] for part in receipt["parts"]] == ["US:1"]
    legacy = receipts.read("quant-card-canary-20260903-02")
    assert legacy["parts"][0]["status"] == "UNKNOWN" and legacy["status"] == "UNKNOWN"
    for activation_id, override in (
        ("quant-card-recover-us-02", False),
        ("quant-card-recover-us-03", True),
    ):
        again = run_quant_telegram_card_canary(
            activation=dict(_activation(), activation_id=activation_id),
            receipts=receipts,
            activation_id=activation_id,
            messages=_messages(),
            telegram_send=lambda text: starts.append(text) or 1,
            now=NOW,
            override_report_dedupe=override,
        )
        assert again["deduped"]["KR"] == "quant-card-canary-20260903-02:UNKNOWN"
    assert "KR body" not in starts


def test_operator_ack_never_changes_machine_status(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    (tmp_path / "receipts" / "quant-card-canary-20260903-02.receipt.json").write_text(
        json.dumps(LEGACY_LIVE_RECEIPT)
    )
    receipt = receipts.acknowledge(
        "quant-card-canary-20260903-02",
        "KR:1",
        note="user confirmed KR arrived",
        now=NOW,
    )
    assert receipt["parts"][0]["status"] == "UNKNOWN" and receipt["status"] == "UNKNOWN"
    assert receipt["parts"][0]["operator_ack"] == {
        "note": "user confirmed KR arrived",
        "at": NOW.isoformat(),
    }
    assert receipts.report_attempted("d" * 64) == (
        "quant-card-canary-20260903-02",
        "UNKNOWN",
    )
    with pytest.raises(ValueError):
        receipts.acknowledge("quant-card-canary-20260903-02", "KR:9", note="x", now=NOW)


def _store_report(root, market, cards, seed):
    from tokmo.records.quant_reports import QuantReportStore

    store = QuantReportStore(root)
    report = _report(market, cards, digest_seed=seed)
    digest = store.append_if_absent(report)
    store.finish(
        report.identity, "SUCCEEDED_WITH_CARDS", report.operation_counts, digest
    )
    return digest


def test_cli_recovers_only_us_with_digest_guard_and_full_rerun_sends_neither(
    tmp_path, capsys
):
    tool = _tool()
    tool.build_live_conditional_fetch = lambda **_kwargs: (_ for _ in ()).throw(
        RuntimeError("credential unavailable fixture")
    )
    root = tmp_path / "reports"
    kr_digest = _store_report(
        root, "KR", (_card("000001", "KR", "테스트전자", VALUES_HOT),), "a"
    )
    us_digest = _store_report(
        root, "US", (_card("NVDA", "US", "엔비디아", VALUES_CALM),), "b"
    )
    receipt_root = tmp_path / "receipts"
    receipt_root.mkdir()
    legacy = dict(
        LEGACY_LIVE_RECEIPT, report_digests={"KR": kr_digest, "US": us_digest}
    )
    (receipt_root / "quant-card-canary-20260903-02.receipt.json").write_text(
        json.dumps(legacy)
    )
    gate = tmp_path / "gate.json"
    env = {"TELEGRAM_STOCK_BOT_TOKEN": "tok", "TELEGRAM_STOCK_BOT_CHAT_ID": "42"}
    sent = []
    paths = dict(
        holdings_path=tmp_path / "no_holdings.json",
        quotes_path=tmp_path / "no_quotes.sqlite3",
        playbook_root=tmp_path / "playbooks",
        watchlist_root=tmp_path / "watchlists",
        native_refs_path=tmp_path / "no_refs.sqlite3",
    )

    def factory(token, chat_id):
        assert (token, chat_id) == ("tok", "42")
        return lambda text: sent.append(text) or 656

    def run(activation_id, *extra):
        write_card_activation(
            gate,
            activation_id=activation_id,
            mode="CANARY",
            expires_at=NOW + timedelta(minutes=30),
        )
        code = tool.main(
            [
                "send",
                "--activation-id",
                activation_id,
                "--report-root",
                str(root),
                *extra,
            ],
            environ=env,
            clock=lambda: NOW,
            sender_factory=factory,
            gate_path=gate,
            receipt_root=receipt_root,
            **paths,
        )
        return code, json.loads(capsys.readouterr().out)

    code, out = run(
        "quant-card-recover-us-01",
        "--markets",
        "US",
        "--expect-report-sha256",
        f"US={'f' * 64}",
    )
    assert (
        code == tool.EXIT_REFUSED
        and out["off"] == "report_digest_mismatch:US"
        and sent == []
    )
    code, out = run(
        "quant-card-recover-us-01",
        "--markets",
        "US",
        "--expect-report-sha256",
        f"US={us_digest}",
    )
    assert (
        code == tool.EXIT_OK and out["markets"] == ["US"] and out["result"]["sent"] == 1
    )
    assert (
        len(sent) == 1
        and sent[0].startswith("📊 퀀트 US\n2026-09-02\n카드 1종목\n")
        and "000001" not in sent[0]
    )
    assert "tok" not in json.dumps(out)
    # no Toss credential in the test tree: the live conditional seam fails closed, the send still goes
    assert out["provider_calls"]["seam"] == "unavailable"
    assert "조건주문 현황" not in sent[0]
    code, out = run("quant-card-canary-20260904-01")
    assert (
        code == tool.EXIT_REFUSED
        and out["result"]["off"] == "report_already_attempted"
        and len(sent) == 1
    )
    assert set(out["result"]["deduped"]) == {"KR", "US"}
    assert (
        json.loads(
            (receipt_root / "quant-card-canary-20260903-02.receipt.json").read_text()
        )["parts"][0]["status"]
        == "UNKNOWN"
    )


def test_provider_failure_settles_failed_and_settle_failure_is_unknown(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")

    def reject(text):
        raise RuntimeError("telegram send rejected")

    result = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=reject,
        now=NOW,
    )
    # each market keeps its own one attempt: both are FAILED, none SKIPPED
    assert result["failed"] == 2 and result["skipped"] == 0
    assert receipts.read("quant-card-canary-01")["status"] == "FAILED"

    class Flaky(QuantTelegramCardReceiptStore):
        def settle(self, activation_id, part_key, status, **kwargs):
            if status == "SENT":
                raise OSError("disk")
            return super().settle(activation_id, part_key, status, **kwargs)

    flaky = Flaky(tmp_path / "flaky")
    result = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=flaky,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=lambda text: 700,
        now=NOW,
    )
    assert (
        result["unknown"] == 2
        and flaky.read("quant-card-canary-01")["status"] == "UNKNOWN"
    )
    assert (
        flaky.read("quant-card-canary-01")["parts"][0]["reason"]
        == "settle_failed_after_delivery"
    )
    assert flaky.report_attempted("d" * 64) == ("quant-card-canary-01", "UNKNOWN")


def test_multi_part_market_aborts_only_its_own_remaining_parts(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    messages = (
        MarketMessageV1(
            "KR", "2026-09-02", "a" * 64, "d" * 64, ("KR one", "KR two"), (), ()
        ),
        MarketMessageV1("US", "2026-09-02", "b" * 64, "e" * 64, ("US one",), (), ()),
    )
    starts = []

    def send(text):
        starts.append(text)
        if text == "KR one":
            raise TimeoutError
        return 1

    result = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=messages,
        telegram_send=send,
        now=NOW,
    )
    assert starts == ["KR one", "US one"]
    assert result["parts"] == [
        ("KR:1", "UNKNOWN"),
        ("KR:2", "SKIPPED"),
        ("US:1", "SENT"),
    ]


def test_same_report_digest_is_refused_across_activation_ids(tmp_path):
    receipts = QuantTelegramCardReceiptStore(tmp_path / "receipts")
    first = run_quant_telegram_card_canary(
        activation=_activation(),
        receipts=receipts,
        activation_id="quant-card-canary-01",
        messages=_messages(),
        telegram_send=lambda text: 1,
        now=NOW,
    )
    assert first["sent"] == 2
    activation = dict(_activation(), activation_id="quant-card-canary-02")
    second = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id="quant-card-canary-02",
        messages=_messages(),
        telegram_send=lambda text: 2,
        now=NOW,
    )
    assert second["off"] == "report_already_attempted"
    assert second["deduped"] == {
        "KR": "quant-card-canary-01:SENT",
        "US": "quant-card-canary-01:SENT",
    }
    assert receipts.read("quant-card-canary-02") is None
    forced = run_quant_telegram_card_canary(
        activation=activation,
        receipts=receipts,
        activation_id="quant-card-canary-02",
        messages=_messages(),
        telegram_send=lambda text: 2,
        now=NOW,
        override_report_dedupe=True,
    )
    assert forced["sent"] == 2
    off = run_quant_telegram_card_canary(
        activation={"mode": "OFF", "activation_id": None, "reason": "gate_absent"},
        receipts=receipts,
        activation_id="quant-card-canary-03",
        messages=_messages(),
        telegram_send=lambda text: 3,
        now=NOW,
    )
    assert off["off"] == "gate_absent" and receipts.read("quant-card-canary-03") is None


# --- no-order boundary --------------------------------------------------------


def test_no_order_or_account_write_path_and_boundary_line():
    sources = [
        SRC / "programs" / "quant_telegram_card.py",
        SRC / "programs" / "quant_card_conditional.py",
        SRC / "records" / "quant_telegram_card_receipts.py",
        SRC / "records" / "quant_report_locator.py",
    ]
    banned = (
        "/api/v1",
        "/oauth2",
        "toss_live",
        "toss_reads_live",
        "toss_auth_live",
        "conditional-orders",
        "orders/",
        "record_quant_market_close_outbox",
        "ops_kernel",
        "telegram_dialogue_live",
    )
    for path in sources:
        text = path.read_text(encoding="utf-8")
        for item in banned:
            assert item not in text, (path.name, item)
    tool_text = TOOL.read_text(encoding="utf-8")
    for item in (
        "/api/v1",
        "/oauth2",
        "toss_live",
        "conditional-orders",
        "orders/",
        "ops_kernel",
        "record_quant_market_close_outbox",
    ):
        assert item not in tool_text, item
    # the ONLY provider path in the controller is the bounded OPEN conditional read seam
    seam = tool_text.split("def build_live_conditional_fetch", 1)[1].split("\ndef ", 1)[
        0
    ]
    for adapter in (
        "toss_reads_live",
        "toss_auth_live",
        "TossReadOperation.CONDITIONAL_ORDERS",
    ):
        assert tool_text.count(adapter) == seam.count(adapter) == 1, adapter
    assert (
        seam.count("query_read(") == 1
        and "while " not in seam
        and "for attempt" not in seam
    )  # retry 0
    # the visible header is exactly market / session date / card count: no digest, label or disclaimer
    for message in render_decision_cards(
        {"KR": (_kr_report(), "d" * 64)}, {"KR": _kr_contexts()}
    ):
        for part in message.parts:
            expected = (
                ["📊 퀀트 KR", "2026-09-02", "카드 2종목"]
                if message.market == "KR"
                else ["📊 퀀트 US", "리포트 없음 — 검증된 완료 세션 리포트가 없습니다"]
            )
            assert part.split("\n\n", 1)[0].split("\n") == expected
            assert (
                "dddddddddddd" not in part
                and "canary" not in part
                and "※ 주문" not in part
            )


def test_actionable_quant_sender_serializes_false_and_other_callers_omit():
    from urllib.parse import parse_qs

    from tokmo.facts.telegram_dialogue_live import send_dialogue_message

    bodies = []

    def opener(url, body, timeout):
        bodies.append(parse_qs(body.decode()))
        return b'{"ok":true,"result":{"message_id":7}}'

    sender = _tool().pre_entity_sender(
        "fixture",
        "42",
        send=lambda **kwargs: send_dialogue_message(**kwargs, opener=opener),
    )
    assert sender("SYNTHETIC") == 7
    assert bodies[-1]["protect_content"] == ["false"]
    send_dialogue_message(
        body_text="SYNTHETIC", bot_token="fixture", chat_id="42", opener=opener
    )
    assert "protect_content" not in bodies[-1]
