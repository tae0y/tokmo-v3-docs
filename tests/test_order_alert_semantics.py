"""Order submission is not execution; exercise the actual public stages."""

import json
from dataclasses import replace

import pytest
from test_conditional_detection import (
    NOW,
    SLOT_A,
    SLOT_B,
    TOSS_SCOPE,
    _cond_order,
    _condition,
    _ConditionalFetch,
    _detect,
    _regular_event,
    _run_dialogue,
    _store,
)

from tokmo.programs.conditional_detection import build_conditional_fill_question
from tokmo.programs.execution_dialogue import (
    _flush_outbox,
    build_trade_question_message,
)
from tokmo.values.trade_event import TradeEventState, dialogue_dedupe_key


@pytest.mark.parametrize(
    "market,symbol,name,title",
    [
        ("US", "V", "비자", "비자(V)"),
        ("US", "MRNA", "모더나", "모더나(MRNA)"),
        ("KR", "005380", "현대차", "현대차(005380)"),
        ("KR", "0167A0", "확인된 ETF명", "확인된 ETF명(0167A0)"),
    ],
)
def test_submission_title_once_and_no_fill_claim(market, symbol, name, title):
    text = build_conditional_fill_question(
        account_scope=TOSS_SCOPE,
        symbol=symbol,
        market_class=market,
        instrument_name=name,
    )
    assert text.splitlines()[:3] == [
        title,
        "조건에 도달해 주문이 접수됐어요.",
        "체결 여부는 아직 확인되지 않았어요.",
    ]
    assert text.count(title) == 1
    assert "V 조건" not in text
    assert "체결이 감지" not in text
    assert "체결 확인 전까지 임시 보관" in text


@pytest.mark.parametrize("side,expected", [("BUY", "매수"), ("SELL", "매도")])
def test_real_execution_has_distinct_title_and_fill_evidence(tmp_path, side, expected):
    root = tmp_path / "playbooks"
    path = root / "personal/US/V.md"
    path.parent.mkdir(parents=True)
    path.write_text('---\ninstrument_name: "비자"\n---\n')
    event = replace(_regular_event("real", symbol="V"), side=side)
    text = build_trade_question_message(event, evaluation=None, playbook_root=root)
    assert text.splitlines()[:2] == ["비자(V)", expected + " 체결 확인"]
    assert "7주" in text and "$251.00" in text
    assert "체결 " in text and "주문이 접수" not in text


@pytest.mark.parametrize("status", ["ORDERED", "COMPLETED"])
def test_submitted_order_then_real_fill_not_suppressed_or_repeated(tmp_path, status):
    store = _store(tmp_path)
    fetch = _ConditionalFetch(
        [
            [_cond_order(first=_condition("WATCHING"))],
            [_cond_order(first=_condition(status, "real"))],
        ]
    )
    _detect(store, SLOT_A, fetch)
    _detect(store, SLOT_B, fetch)
    assert store.list_outbox(status="PENDING") == ()
    event = _regular_event("real")
    result = _run_dialogue(store, [event])
    assert result.enqueued == 1
    assert store.read_event(event.trade_event_id)["state"] is TradeEventState.ENQUEUED
    assert (
        store.list_conditional_alerts()[0]["migrated_event_id"] == event.trade_event_id
    )
    rows = store.list_outbox(status="PENDING")
    assert len(rows) == 1  # only an actual execution
    assert sum("매도 체결 확인" in row["message_text"] for row in rows) == 1
    assert _run_dialogue(store, [event]).enqueued == 0
    assert len(store.list_outbox(status="PENDING")) == 1


def _capture(root, rows):
    path = root / "ops/quant-inputs/latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"captured_at": NOW.isoformat(), "instruments": rows}))


@pytest.mark.parametrize(
    "rows,title",
    [
        ([{"market": "US", "symbol": "V", "name": "비자"}], "비자(V)"),
        ([{"market": "KR", "symbol": "V", "name": "다른종목"}], "V"),
        (
            [
                {"market": "US", "symbol": "V", "name": "비자"},
                {"market": "US", "symbol": "V", "name": "충돌"},
            ],
            "V",
        ),
        ([{"market": "US", "symbol": "V", "name": "Visa"}], "V"),
    ],
)
def test_capture_identity_for_execution_without_submission_alert(tmp_path, rows, title):
    root = tmp_path / "runtime"
    store_dir = root / "stores"
    store_dir.mkdir(parents=True)
    store = _store(store_dir)
    _capture(root, rows)
    fetch = _ConditionalFetch(
        [
            [_cond_order(symbol="V", first=_condition("WATCHING"))],
            [_cond_order(symbol="V", first=_condition("ORDERED", "real"))],
        ]
    )
    _detect(store, SLOT_A, fetch)
    _detect(store, SLOT_B, fetch)
    assert store.list_outbox(status="PENDING") == ()
    _run_dialogue(store, [_regular_event("real", symbol="V")])
    assert all(
        row["message_text"].splitlines()[0] == title
        for row in store.list_outbox(status="PENDING")
    )


@pytest.mark.parametrize("limit", [None, 1])
def test_pending_legacy_submission_never_sends_or_starves_fill(tmp_path, limit):
    store = _store(tmp_path)
    key = dialogue_dedupe_key("a" * 64, "conditional_fill_question", "v1")
    store.enqueue_outbox(
        key,
        trade_event_id="a" * 64,
        prompt_kind="conditional_fill_question",
        prompt_version="v1",
        message_text="old submission",
    )
    _run_dialogue(store, [_regular_event("real")])
    sent = []

    def sender(text, **kwargs):
        sent.append(text)
        return 100 + len(sent)

    assert (
        _flush_outbox(
            store, sender, activation_enabled=True, send_eligible_after=NOW, limit=limit
        )
        == 1
    )
    assert len(sent) == 1 and "매도 체결 확인" in sent[0]
    assert store.read_outbox(key)["status"] == "PENDING"  # preserve history
    assert (
        _flush_outbox(
            store, sender, activation_enabled=True, send_eligible_after=NOW, limit=limit
        )
        == 0
    )
