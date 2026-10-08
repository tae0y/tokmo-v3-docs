"""G3 trade-dialogue Telegram UI (confirmed output contract + inline buttons).

The user-confirmed question contract, the recommendation slot, the
Decimal ROUND_HALF_UP money format, the KST times without suffix, the
inline-keyboard button rows, and the callback-driven preview/append flows.
"""

import json
import sqlite3
from datetime import datetime

import pytest

from tokmo.programs.execution_detection import NewTradeExecutionV1
from tokmo.programs.execution_dialogue import (
    EVALUATION_POLICY_VERSION,
    PROMPT_VERSION,
    build_trade_question_message,
    consume_inbound,
    run_execution_dialogue,
)
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.values.trade_event import (
    TradeEventEvaluationV1,
    TradeEventTimesV1,
    conditional_alert_id,
    dialogue_dedupe_key,
    trade_event_id,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


NOW = _utc("2026-08-19T14:40:00Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24


def _times(filled="2026-08-19T14:39:00Z", discovered="2026-08-19T14:40:00Z"):
    return TradeEventTimesV1(
        filled_at=_utc(filled), provider_observed_at=None,
        discovered_at=_utc(discovered), stored_at=_utc(discovered),
        notified_at=None,
    )


def _event(order_id="ord-googl-1", symbol="GOOGL", side="SELL",
           quantity="1.04094", unit_price="343.7378", amount="357.8226",
           currency="USD", market="US", times=None, scope=TOSS_SCOPE):
    event_id = trade_event_id("toss-orders-v1", scope, order_id)
    return NewTradeExecutionV1(
        event_id, scope, order_id, symbol, side, currency, quantity,
        unit_price, amount, market, times or _times(),
    )


def _store(tmp_path):
    return ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3",
        clock=lambda: NOW,
    )


# --- confirmed question message contract -----------------------------------


def test_question_message_matches_confirmed_contract_no_recommendation():
    message = build_trade_question_message(
        _event(), evaluation=None, playbook_root=None)
    assert message == (
        "GOOGL\n매도 체결 확인\n"
        "1.04094주  $343.74 = $357.82\n"
        "체결 2026-08-19 23:39 / 확인 2026-08-19 23:40\n"
        "추천 문구\n"
        "관련 정보가 부족해 추천문구 없음\n"
        "이 메시지에 바로 답장하면 매매 사유를 저장해요. (추가입력 버튼 없이 가능)"
    )


def test_question_message_shows_recommendation_as_bullet_line():
    evaluation = TradeEventEvaluationV1(
        _event().trade_event_id, EVALUATION_POLICY_VERSION,
        "목표가 도달로 일부 익절", "partial", True, True)
    message = build_trade_question_message(
        _event(), evaluation=evaluation, playbook_root=None)
    assert "추천 문구\n- 목표가 도달로 일부 익절\n" in message
    assert "관련 정보가 부족해 추천문구 없음" not in message


def test_question_message_money_rounds_half_up_not_bankers():
    # 343.745 -> 343.75 under ROUND_HALF_UP (banker's would give 343.74)
    message = build_trade_question_message(
        _event(unit_price="343.745", amount="357.745"),
        evaluation=None, playbook_root=None)
    assert "$343.75 = $357.75" in message


def test_question_message_pads_money_to_two_decimals():
    message = build_trade_question_message(
        _event(quantity="3", unit_price="90.5", amount="271.5"),
        evaluation=None, playbook_root=None)
    # quantity keeps the ledger's raw precision: no trailing zeros added
    assert "3주  $90.50 = $271.50" in message
    assert "3.0주" not in message


def test_question_message_buy_side_and_kst_conversion():
    message = build_trade_question_message(
        _event(side="BUY", times=_times(
            filled="2026-08-19T00:01:00Z", discovered="2026-08-19T15:30:00Z")),
        evaluation=None, playbook_root=None)
    assert message.startswith("GOOGL\n매수 체결 확인\n")
    assert "체결 2026-08-19 09:01 / 확인 2026-08-20 00:30" in message
    assert "KST" not in message


def test_question_message_masks_scope_paths_and_refs(tmp_path):
    message = build_trade_question_message(
        _event(), evaluation=None, playbook_root=tmp_path / "playbooks")
    assert TOSS_SCOPE not in message
    assert "toss-acct" not in message
    assert "/Users/" not in message
    assert str(tmp_path) not in message


# --- recommendation persistence ---------------------------------------------


def test_store_question_recommendation_roundtrip_first_write_wins(tmp_path):
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    assert store.read_question_recommendation(event_id) is None
    assert store.record_question_recommendation(
        event_id, "목표가 도달로 일부 익절".encode()) == "created"
    assert store.record_question_recommendation(
        event_id, "다른 문구".encode()) == "duplicate"
    assert store.read_question_recommendation(event_id) \
        == "목표가 도달로 일부 익절".encode()


def test_pipeline_records_recommendation_bytes(tmp_path):
    store = _store(tmp_path)
    playbooks = tmp_path / "playbooks"
    (playbooks / "personal" / "US").mkdir(parents=True)
    (playbooks / "personal" / "US" / "GOOGL.md").write_text(
        "# GOOGL\n\n## 3층 — 매매이력\n", encoding="utf-8")

    def evaluator(payload):
        return TradeEventEvaluationV1(
            payload["trade_event_id"], EVALUATION_POLICY_VERSION,
            "목표가 도달로 일부 익절", "partial", True, True)

    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: {"last_price": "343.74"},
        playbook_root=playbooks, ai_evaluator=evaluator, sender=None,
        activation_enabled=False, send_eligible_after=NOW,
        clock=lambda: NOW, now=NOW)
    assert store.read_question_recommendation(_event().trade_event_id) \
        == "목표가 도달로 일부 익절".encode()


def test_pipeline_records_no_recommendation_without_evaluation(tmp_path):
    store = _store(tmp_path)
    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=None, activation_enabled=False,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    assert store.read_question_recommendation(_event().trade_event_id) is None


# --- pending append payload (button flows carry the bytes directly) --------


def test_pending_append_payload_roundtrip(tmp_path):
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_preview",
                              PROMPT_VERSION)
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=7,
        inbound_message_id=700, preview_dedupe_key=key,
        payload="원문 그대로  ".encode())
    pending = store.read_pending_append(event_id)
    assert pending["payload"] == "원문 그대로  ".encode()
    by_preview = store.find_pending_append_by_preview(key)
    assert by_preview["payload"] == "원문 그대로  ".encode()


def test_pending_append_without_payload_stays_none(tmp_path):
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_preview",
                              PROMPT_VERSION)
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=8,
        inbound_message_id=800, preview_dedupe_key=key)
    assert store.read_pending_append(event_id)["payload"] is None


def test_pending_append_payload_column_migrates_legacy_store(tmp_path):
    path = tmp_path / "stores" / "execution_dialogue.sqlite3"
    path.parent.mkdir(parents=True)
    legacy = sqlite3.connect(path)
    legacy.execute(
        "CREATE TABLE pending_appends("
        "trade_event_id TEXT PRIMARY KEY,"
        "provider_update_id INTEGER NOT NULL,"
        "inbound_message_id INTEGER NOT NULL,"
        "preview_dedupe_key TEXT NOT NULL,"
        "created_at TEXT NOT NULL) STRICT")
    legacy.execute(
        "INSERT INTO pending_appends VALUES('e'||substr(hex(zeroblob(32)),2),"
        "1, 10, 'k', '2026-08-18T00:00:00+00:00')")
    legacy.commit()
    legacy.close()
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_preview",
                              PROMPT_VERSION)
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=9,
        inbound_message_id=900, preview_dedupe_key=key, payload=b"bytes")
    assert store.read_pending_append(event_id)["payload"] == b"bytes"
    # The pre-migration row survives with a NULL payload.
    legacy_id = "e" + "0" * 63
    assert store.read_pending_append(legacy_id)["payload"] is None


# --- inline keyboards at send time ------------------------------------------


QUESTION_KEYBOARD = {"inline_keyboard": [[
    {"text": "예", "callback_data": "q1:yes"},
    {"text": "입력안함", "callback_data": "q1:none"},
    {"text": "추가입력", "callback_data": "q1:input"},
]]}
PREVIEW_KEYBOARD = {"inline_keyboard": [[
    {"text": "확인", "callback_data": "p1:ok"},
    {"text": "취소", "callback_data": "p1:cancel"},
    {"text": "수정", "callback_data": "p1:edit"},
]]}


class _RecordingSender:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        self._next_id += 1
        return self._next_id


def test_question_flush_attaches_single_row_yes_none_input_keyboard(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    assert len(sender.calls) == 1
    _text, kwargs = sender.calls[0]
    assert kwargs == {"reply_markup": QUESTION_KEYBOARD}


def test_preview_flush_attaches_confirm_cancel_edit_keyboard(tmp_path):
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_preview",
                              PROMPT_VERSION)
    store.enqueue_outbox(key, trade_event_id=event_id,
                         prompt_kind="trade_reason_preview",
                         prompt_version=PROMPT_VERSION,
                         message_text="미리보기")
    sender = _RecordingSender()
    run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    assert sender.calls == [("미리보기", {"reply_markup": PREVIEW_KEYBOARD})]


def test_reconfirm_flush_sends_without_markup_for_legacy_senders(tmp_path):
    store = _store(tmp_path)
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_reconfirm",
                              PROMPT_VERSION)
    store.enqueue_outbox(key, trade_event_id=event_id,
                         prompt_kind="trade_reason_reconfirm",
                         prompt_version=PROMPT_VERSION,
                         message_text="재확인")
    legacy_calls = []

    def legacy_sender(text):  # single-arg legacy callable stays valid
        legacy_calls.append(text)
        return 555

    run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=legacy_sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    assert legacy_calls == ["재확인"]
    assert store.read_outbox(key)["status"] == "SENT"


# --- live transport: reply_markup form, callback normalization, ack --------


def test_send_message_form_includes_reply_markup_json():
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message
    import urllib.parse

    calls = []

    def opener(url, body, timeout):
        calls.append((url, body, timeout))
        return b'{"ok": true, "result": {"message_id": 4242}}'

    message_id = send_dialogue_message(
        body_text="질문", bot_token="123456:tok", chat_id="777",
        reply_markup=QUESTION_KEYBOARD, opener=opener)
    assert message_id == 4242
    body = calls[0][1].decode("utf-8")
    fields = dict(urllib.parse.parse_qsl(body))
    assert fields["chat_id"] == "777"
    assert fields["text"] == "질문"
    assert json.loads(fields["reply_markup"]) == QUESTION_KEYBOARD


def test_send_message_without_markup_keeps_legacy_form():
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message

    calls = []

    def opener(url, body, timeout):
        calls.append(body)
        return b'{"ok": true, "result": {"message_id": 1}}'

    send_dialogue_message(body_text="x", bot_token="t", chat_id="7",
                          opener=opener)
    assert b"reply_markup" not in calls[0]


def test_fetch_updates_normalizes_authorized_callback_query():
    from tokmo.facts import telegram_dialogue_live as live

    payload = {"ok": True, "result": [
        {"update_id": 30, "callback_query": {
            "id": "cb-30", "from": {"id": 777},
            "message": {"message_id": 101,
                        "chat": {"id": 777, "type": "private"}},
            "data": "q1:yes"}},
        # foreign user pressing a button in some forwarded copy: ignored
        {"update_id": 31, "callback_query": {
            "id": "cb-31", "from": {"id": 999},
            "message": {"message_id": 101,
                        "chat": {"id": 777, "type": "private"}},
            "data": "q1:yes"}},
        # group-chat callback: ignored
        {"update_id": 32, "callback_query": {
            "id": "cb-32", "from": {"id": 777},
            "message": {"message_id": 101,
                        "chat": {"id": 777, "type": "group"}},
            "data": "q1:yes"}},
        # data missing (game callback etc.): ignored
        {"update_id": 33, "callback_query": {
            "id": "cb-33", "from": {"id": 777},
            "message": {"message_id": 101,
                        "chat": {"id": 777, "type": "private"}}}},
    ]}
    seen = []

    def opener(url, timeout):
        seen.append(url)
        return json.dumps(payload).encode()

    updates = live.fetch_dialogue_updates(
        offset=30, bot_token="123456:tok", chat_id="777", opener=opener)
    assert updates[0] == {"update_id": 30, "callback": True,
                          "callback_query_id": "cb-30", "message_id": 101,
                          "data": "q1:yes"}
    assert updates[1:] == (
        {"update_id": 31, "ignored": True},
        {"update_id": 32, "ignored": True},
        {"update_id": 33, "ignored": True},
    )
    assert "callback_query" in seen[0]


def test_answer_callback_is_bounded_and_never_raises():
    from tokmo.facts.telegram_dialogue_live import answer_dialogue_callback

    calls = []

    def ok_opener(url, body, timeout):
        calls.append((url, body, timeout))
        return b'{"ok": true, "result": true}'

    assert answer_dialogue_callback(
        callback_query_id="cb-1", bot_token="123456:tok",
        opener=ok_opener) is True
    url, body, timeout = calls[0]
    assert url.endswith("/answerCallbackQuery")
    assert b"callback_query_id=cb-1" in body
    assert timeout <= 10.0

    def raising_opener(url, body, timeout):
        raise TimeoutError("socket timeout")

    assert answer_dialogue_callback(
        callback_query_id="cb-1", bot_token="t",
        opener=raising_opener) is False

    def rejected_opener(url, body, timeout):
        return b'{"ok": false, "description": "query is too old"}'

    assert answer_dialogue_callback(
        callback_query_id="cb-1", bot_token="t",
        opener=rejected_opener) is False

    # missing identity: refused locally, opener never called
    assert answer_dialogue_callback(
        callback_query_id="", bot_token="t",
        opener=ok_opener) is False
    assert len(calls) == 1


# --- callback-driven button flows -------------------------------------------


def _playbook_root(tmp_path):
    root = tmp_path / "playbooks"
    (root / "personal" / "US").mkdir(parents=True)
    (root / "personal" / "US" / "GOOGL.md").write_text(
        "# GOOGL\n\n## 3층 — 매매이력\n", encoding="utf-8")
    return root


def _send_question(store, sender, *, tmp_path, with_recommendation=False):
    playbooks = _playbook_root(tmp_path)
    evaluator = None
    if with_recommendation:
        def evaluator(payload):
            return TradeEventEvaluationV1(
                payload["trade_event_id"], EVALUATION_POLICY_VERSION,
                "목표가 도달로 일부 익절", "partial", True, True)
    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: ({"last_price": "343.74"}
                                     if with_recommendation else None),
        playbook_root=playbooks, ai_evaluator=evaluator, sender=sender,
        activation_enabled=True, send_eligible_after=NOW,
        clock=lambda: NOW, now=NOW)
    return playbooks


def _consume(store, updates, *, playbooks=None, sender=None,
             callback_ack=None):
    return consume_inbound(
        dialogue_store=store,
        updates_provider=lambda offset: tuple(updates),
        playbook_root=playbooks,
        playbook_validator=lambda root: [],
        sender=sender,
        callback_ack=callback_ack,
        activation_enabled=True,
        send_eligible_after=NOW,
        clock=lambda: NOW,
        now=NOW,
    )


def _callback(update_id, message_id, data, cq=None):
    return {"update_id": update_id, "callback": True,
            "callback_query_id": cq or f"cb-{update_id}",
            "message_id": message_id, "data": data}


def _receipt_of(store, kind="trade_reason_receipt"):
    rows = [row for row in store.list_outbox()
            if row["prompt_kind"] == kind]
    assert rows
    return rows[-1]


def test_yes_with_recommendation_saves_immediately_and_receipts(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path,
                               with_recommendation=True)
    result = _consume(store, [_callback(100, 101, "q1:yes")],
                      playbooks=playbooks, sender=sender)
    # 2026-08-21: adoption IS the submission — no second confirmation.
    assert result.appends == 1
    assert result.receipts_enqueued == 1
    assert result.previews_enqueued == 0
    assert store.read_pending_append(_event().trade_event_id) is None
    receipt = _receipt_of(store)
    assert receipt["status"] == "SENT"
    assert receipt["message_text"] == (
        "2026-08-19 23:39 / GOOGL\n"
        "플레이북에 매매 사유를 저장했어요.\n"
        "---\n"
        "목표가 도달로 일부 익절")
    document = (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8")
    assert "목표가 도달로 일부 익절" in document
    assert document.count("USER_RAW_START") == 1
    # The replayed update is deduped outright; a genuinely fresh second
    # press re-affirms the same bytes and converges on the same one entry
    # through the deterministic anchor — never a duplicate.
    _consume(store, [_callback(100, 101, "q1:yes"),
                     _callback(102, 101, "q1:yes")],
             playbooks=playbooks, sender=sender)
    assert (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8").count("USER_RAW_START") == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM reason_appends").fetchone()[0] == 1


def test_yes_without_recommendation_guides_zero_canonical_writes(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    before = (playbooks / "personal" / "US" / "GOOGL.md").read_bytes()
    result = _consume(store, [_callback(110, 101, "q1:yes")],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 0
    assert result.previews_enqueued == 0
    assert result.reconfirms_enqueued == 1
    assert store.read_pending_append(_event().trade_event_id) is None
    assert (playbooks / "personal" / "US" / "GOOGL.md").read_bytes() == before
    guidance = [text for text, _ in sender.calls if "추천문구" in text]
    assert any("추가입력" in text for text in guidance)


def test_none_button_reuses_no_append_close_idempotently(tmp_path):
    from tokmo.values.trade_event import TradeEventState

    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    result = _consume(store, [_callback(120, 101, "q1:none")],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 0
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED
    # replayed update and a second press both close idempotently
    replay = _consume(store, [_callback(120, 101, "q1:none"),
                              _callback(121, 101, "q1:none")],
                      playbooks=playbooks, sender=sender)
    assert replay.status == "saved"
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED


def test_input_button_full_roundtrip_appends_byte_exact(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    result = _consume(store, [_callback(130, 101, "q1:input")],
                      playbooks=playbooks, sender=sender)
    assert result.input_prompts_enqueued == 1
    prompt_text, prompt_kwargs = sender.calls[-1]
    assert "답장" in prompt_text
    assert prompt_kwargs["reply_markup"]["force_reply"] is True  # Open the reply field.
    prompt_rows = [row for row in store.list_outbox()
                   if row["prompt_kind"] == "trade_reason_input_prompt"]
    assert len(prompt_rows) == 1
    prompt_tg_id = prompt_rows[0]["telegram_message_id"]
    raw_reason = "그냥 몰빵  (오타그대로)"
    reply = _consume(
        store,
        [{"update_id": 131, "message_id": 1310,
          "reply_to_message_id": prompt_tg_id, "text": raw_reason}],
        playbooks=playbooks, sender=sender)
    # The reply itself saves: one append, one receipt, nothing pending.
    assert reply.appends == 1
    assert reply.receipts_enqueued == 1
    assert store.read_pending_append(_event().trade_event_id) is None
    receipt = _receipt_of(store)
    assert receipt["message_text"].endswith("---\n" + raw_reason)
    document = (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8")
    assert raw_reason in document
    # a replayed inbound update appends nothing more
    again = _consume(
        store,
        [{"update_id": 131, "message_id": 1310,
          "reply_to_message_id": prompt_tg_id, "text": raw_reason}],
        playbooks=playbooks, sender=sender)
    assert again.appends == 0
    document = (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8")
    assert document.count("USER_RAW_START") == 1


def test_conditional_buttons_preserve_reason_until_ledger_migration(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _playbook_root(tmp_path)
    alert_id = conditional_alert_id("cond-ui-1", 0, "COMPLETED")
    assert store.record_conditional_alert(
        alert_id,
        conditional_order_id="cond-ui-1",
        leg_index=0,
        triggered_order_id="ord-googl-1",
        account_scope=TOSS_SCOPE,
        symbol="GOOGL",
        market_class="US",
        order_type="LIMIT",
        quantity_ordered="1.04094",
        prev_status="WATCHING",
        new_status="COMPLETED",
    ) == "created"
    assert store.record_question_recommendation(
        alert_id, "조건매수 계획 유지".encode()) == "created"
    question_key = dialogue_dedupe_key(
        alert_id, "conditional_fill_question", PROMPT_VERSION)
    assert store.enqueue_outbox(
        question_key,
        trade_event_id=alert_id,
        prompt_kind="conditional_fill_question",
        prompt_version=PROMPT_VERSION,
        message_text="V 조건주문 체결이 감지",
    ) == "created"
    # Seed an already delivered historical message; new submissions do not send.
    store.mark_outbox(question_key, "SENT",
        telegram_message_id=sender("historical submission"))
    question = store.read_outbox(question_key)
    assert question["telegram_message_id"] == 101

    opened = _consume(
        store, [_callback(200, 101, "q1:input")],
        playbooks=playbooks, sender=sender)
    assert opened.input_prompts_enqueued == 1
    input_row = next(row for row in store.list_outbox()
                     if row["prompt_kind"] == "trade_reason_input_prompt")
    raw_reason = "조건매수 계획에 따른 진입"
    replied = _consume(
        store,
        [{"update_id": 201, "message_id": 2010,
          "reply_to_message_id": input_row["telegram_message_id"],
          "text": raw_reason}],
        playbooks=playbooks, sender=sender)
    # Pre-ledger: nothing is fabricated, the bytes are durable, and the
    # receipt says so truthfully.
    assert replied.appends == 0
    assert replied.receipts_enqueued == 1
    assert store.read_pending_append(alert_id)["payload"] == raw_reason.encode()
    pending_receipt = _receipt_of(store, "trade_reason_pending_receipt")
    assert "원장" in pending_receipt["message_text"]
    assert store.read_event(alert_id) is None

    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=playbooks,
        ai_evaluator=None, sender=None, activation_enabled=False,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    event_id = _event().trade_event_id
    rebound = store.read_pending_append(event_id)
    assert rebound is not None
    assert store.read_outbox(
        rebound["preview_dedupe_key"])["trade_event_id"] == event_id
    assert store.read_question_recommendation(event_id) \
        == "조건매수 계획 유지".encode()
    assert store.read_question_recommendation(alert_id) is None

    # After migration the sweep settles the preserved bytes with no further
    # user action, exactly once.
    settled = _consume(store, [], playbooks=playbooks, sender=sender)
    assert settled.appends == 1
    assert settled.receipts_enqueued == 1
    document = (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8")
    assert raw_reason in document
    assert document.count("USER_RAW_START") == 1
    again = _consume(store, [], playbooks=playbooks, sender=sender)
    assert again.appends == 0


def _legacy_preview(store, sender, playbooks, *, payload="구버전 원문",
                    update_id=900, message_id=9000):
    """Seed an in-flight bubble from the abolished preview handshake."""
    event_id = _event().trade_event_id
    key = dialogue_dedupe_key(event_id, "trade_reason_preview",
                              PROMPT_VERSION)
    store.enqueue_outbox(key, trade_event_id=event_id,
                         prompt_kind="trade_reason_preview",
                         prompt_version=PROMPT_VERSION,
                         message_text="아래 원문을 플레이북 매매이력에 그대로 "
                                      "추가할까요?\n---\n" + payload)
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=update_id,
        inbound_message_id=message_id, preview_dedupe_key=key,
        payload=payload.encode())
    run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=playbooks,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    return store.read_outbox(key)


def test_legacy_preview_row_is_never_auto_settled_by_the_sweep(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    preview = _legacy_preview(store, sender, playbooks)
    before = (playbooks / "personal" / "US" / "GOOGL.md").read_bytes()
    # Many bounded sweeps: an old preview waits for its own 확인, always.
    for _ in range(5):
        swept = _consume(store, [], playbooks=playbooks, sender=sender)
        assert swept.appends == 0
    assert (playbooks / "personal" / "US" / "GOOGL.md").read_bytes() == before
    assert store.read_pending_append(
        _event().trade_event_id)["payload"] == "구버전 원문".encode()
    confirmed = _consume(
        store, [_callback(901, preview["telegram_message_id"], "p1:ok")],
        playbooks=playbooks, sender=sender)
    assert confirmed.appends == 1
    assert "구버전 원문" in (
        playbooks / "personal" / "US" / "GOOGL.md").read_text(encoding="utf-8")


def test_preview_cancel_is_terminal_and_replay_safe(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path,
                               with_recommendation=True)
    preview = _legacy_preview(store, sender, playbooks)
    before = (playbooks / "personal" / "US" / "GOOGL.md").read_bytes()
    cancel = _consume(
        store, [_callback(141, preview["telegram_message_id"], "p1:cancel")],
        playbooks=playbooks, sender=sender)
    assert cancel.appends == 0
    assert store.read_pending_append(_event().trade_event_id) is None
    # A late 확인 on the cancelled preview appends nothing (button or text).
    late = _consume(
        store, [_callback(142, preview["telegram_message_id"], "p1:ok"),
                {"update_id": 143, "message_id": 1430,
                 "reply_to_message_id": preview["telegram_message_id"],
                 "text": "확인"}],
        playbooks=playbooks, sender=sender)
    assert late.appends == 0
    assert (playbooks / "personal" / "US" / "GOOGL.md").read_bytes() == before


def test_receipt_edit_appends_a_correction_and_keeps_the_original(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_callback(150, 101, "q1:input")],
             playbooks=playbooks, sender=sender)
    prompt_tg_id = [row for row in store.list_outbox()
                    if row["prompt_kind"] == "trade_reason_input_prompt"
                    ][0]["telegram_message_id"]
    _consume(store, [{"update_id": 151, "message_id": 1510,
                      "reply_to_message_id": prompt_tg_id,
                      "text": "첫번째 입력"}],
             playbooks=playbooks, sender=sender)
    receipt = _receipt_of(store)
    edit = _consume(
        store, [_callback(152, receipt["telegram_message_id"], "r1:edit")],
        playbooks=playbooks, sender=sender)
    # 수정 opens a fresh input; it never touches what is already recorded.
    assert edit.appends == 0
    assert edit.input_prompts_enqueued == 1
    prompts = [row for row in store.list_outbox()
               if row["prompt_kind"] == "trade_reason_input_prompt"]
    assert len(prompts) == 2  # a second, distinct prompt message went out
    new_prompt = next(row for row in prompts
                      if row["telegram_message_id"] != prompt_tg_id)
    corrected = _consume(
        store, [{"update_id": 154, "message_id": 1540,
                 "reply_to_message_id": new_prompt["telegram_message_id"],
                 "text": "두번째 입력"}],
        playbooks=playbooks, sender=sender)
    assert corrected.appends == 1
    document = (playbooks / "personal" / "US" / "GOOGL.md").read_text(
        encoding="utf-8")
    assert "두번째 입력" in document
    assert "첫번째 입력" in document  # source history is never destroyed
    assert document.count("USER_RAW_START") == 2
    assert "`supersedes_entry_id`: `" in document
    assert document.index("두번째 입력") < document.index("첫번째 입력")


def test_callback_ack_is_called_and_failure_never_blocks(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path,
                               with_recommendation=True)
    acked = []
    first = _consume(store, [_callback(160, 101, "q1:yes", cq="cq-yes")],
                     playbooks=playbooks, sender=sender,
                     callback_ack=lambda cq_id: acked.append(cq_id))
    assert acked == ["cq-yes"]
    assert first.appends == 1
    receipt = _receipt_of(store)

    def broken_ack(cq_id):
        raise RuntimeError("ack transport down")

    edited = _consume(
        store, [_callback(161, receipt["telegram_message_id"], "r1:edit")],
        playbooks=playbooks, sender=sender, callback_ack=broken_ack)
    # ack failure never blocks the dispatch and never becomes a write
    assert edited.input_prompts_enqueued == 1
    assert edited.appends == 0
    assert edited.status == "saved"


def test_callback_on_unknown_message_only_advances_cursor(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    outbox_before = len(store.list_outbox())
    result = _consume(store, [_callback(170, 99999, "p1:ok")],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 0
    assert result.previews_enqueued == 0
    assert store.read_inbound_cursor() == 171
    assert len(store.list_outbox()) == outbox_before


def test_callback_data_never_carries_user_text_or_identity(tmp_path):
    store = _store(tmp_path)
    sender = _RecordingSender()
    _send_question(store, sender, tmp_path=tmp_path)
    markups = [kwargs.get("reply_markup") for _, kwargs in sender.calls
               if kwargs.get("reply_markup")]
    assert markups
    for markup in markups:
        for row in markup["inline_keyboard"]:
            for button in row:
                data = button["callback_data"]
                assert len(data) <= 16
                assert TOSS_SCOPE not in data
                assert "/" not in data
