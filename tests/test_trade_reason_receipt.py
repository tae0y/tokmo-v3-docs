"""Trade-reason receipt UX contract (2026-08-21, user-approved).

The second confirmation is abolished. Submitting a reason — by adopting the
recommendation (예), by the 추가입력 prompt, or by a direct ``추가:`` reply —
saves it immediately, subject to the same validator/durability gates, and
answers with one receipt bubble carrying exactly one 수정 button:

    2026-08-21 06:10 / MRNA(모더나)
    플레이북에 매매 사유를 저장했어요.
    ---
    <the user's exact bytes>

A save failure never produces that receipt: the pending bytes survive, the
retry is bounded, and the user is told the truth. 수정 never rewrites or
deletes the prior USER_RAW block — it appends a linked correction entry, and
the latest correction is the current interpretation.
"""

import sqlite3
from datetime import datetime

from tokmo.programs.execution_detection import NewTradeExecutionV1
from tokmo.programs import execution_dialogue
from tokmo.programs.execution_dialogue import (
    EVALUATION_POLICY_VERSION,
    PENDING_APPEND_MAX_ATTEMPTS,
    PROMPT_VERSION,
    RECEIPT_KEYBOARD,
    build_reason_receipt_message,
    consume_inbound,
    run_execution_dialogue,
)
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.values.callback_registry import (
    CB_QUESTION_INPUT,
    CB_QUESTION_YES,
    CB_RECEIPT_EDIT,
)
from tokmo.values.trade_event import (
    TradeEventEvaluationV1,
    TradeEventState,
    TradeEventTimesV1,
    conditional_alert_id,
    dialogue_dedupe_key,
    trade_event_id,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


NOW = _utc("2026-08-20T21:12:00Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
FILLED = "2026-08-20T21:10:00Z"  # 2026-08-21 06:10 KST


def _times(filled=FILLED, discovered="2026-08-20T21:12:00Z"):
    return TradeEventTimesV1(
        filled_at=_utc(filled), provider_observed_at=None,
        discovered_at=_utc(discovered), stored_at=_utc(discovered),
        notified_at=None)


def _event(order_id="ord-mrna-1", symbol="MRNA", side="BUY", quantity="3",
           unit_price="25.5", amount="76.5", currency="USD", market="US",
           times=None, scope=TOSS_SCOPE):
    return NewTradeExecutionV1(
        trade_event_id("toss-orders-v1", scope, order_id), scope, order_id,
        symbol, side, currency, quantity, unit_price, amount, market,
        times or _times())


def _store(tmp_path):
    return ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3", clock=lambda: NOW)


def _playbook_root(tmp_path, *, with_symbol=True, name="모더나",
                   symbol="MRNA", market="US"):
    root = tmp_path / "playbooks"
    (root / "personal" / market).mkdir(parents=True, exist_ok=True)
    if with_symbol:
        (root / "personal" / market / f"{symbol}.md").write_text(
            "---\n"
            "schema_version: 1\n"
            f'playbook_id: "personal:{market}:{symbol}"\n'
            f'instrument_name: "{name}"\n'
            "---\n\n"
            f"# {name}\n\n## 3층 — 매매이력\n",
            encoding="utf-8")
    return root


class _Sender:
    def __init__(self):
        self.calls = []
        self._next_id = 100

    def __call__(self, text, **kwargs):
        self.calls.append((text, kwargs))
        self._next_id += 1
        return self._next_id


def _send_question(store, sender, *, tmp_path, recommendation=None,
                   playbooks=None):
    playbooks = _playbook_root(tmp_path) if playbooks is None else playbooks
    evaluator = None
    if recommendation is not None:
        def evaluator(payload):
            return TradeEventEvaluationV1(
                payload["trade_event_id"], EVALUATION_POLICY_VERSION,
                recommendation, "partial", True, True)
    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: {"last_price": "25.5"},
        playbook_root=playbooks, ai_evaluator=evaluator, sender=sender,
        activation_enabled=True, send_eligible_after=NOW,
        clock=lambda: NOW, now=NOW)
    return playbooks


def _consume(store, updates, *, playbooks=None, sender=None,
             validator=None, callback_ack=None):
    return consume_inbound(
        dialogue_store=store,
        updates_provider=lambda offset: tuple(updates),
        playbook_root=playbooks,
        playbook_validator=validator or (lambda root: []),
        sender=sender, callback_ack=callback_ack,
        activation_enabled=True, send_eligible_after=NOW,
        clock=lambda: NOW, now=NOW)


def _callback(update_id, message_id, data):
    return {"update_id": update_id, "callback": True,
            "callback_query_id": f"cb-{update_id}",
            "message_id": message_id, "data": data}


def _reply(update_id, message_id, reply_to, text):
    return {"update_id": update_id, "message_id": message_id,
            "reply_to_message_id": reply_to, "text": text}


def _document(playbooks, symbol="MRNA", market="US"):
    return playbooks / "personal" / market / f"{symbol}.md"


def _receipts(store, kind="trade_reason_receipt"):
    return [row for row in store.list_outbox() if row["prompt_kind"] == kind]


def _input_prompt_id(store, *, exclude=()):
    rows = [row for row in store.list_outbox()
            if row["prompt_kind"] == "trade_reason_input_prompt"
            and row["telegram_message_id"] not in exclude]
    return rows[-1]["telegram_message_id"]


def _claims(store):
    with sqlite3.connect(store.path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM reason_appends").fetchone()[0]


# --- RED-1: exact receipt bytes and one-button markup ----------------------


def test_receipt_message_matches_the_confirmed_byte_contract():
    assert build_reason_receipt_message(
        when=_utc(FILLED), display="MRNA(모더나)",
        payload="그냥 몰빵  (오타그대로)".encode()) == (
            "2026-08-21 06:10 / MRNA(모더나)\n"
            "플레이북에 매매 사유를 저장했어요.\n"
            "---\n"
            "그냥 몰빵  (오타그대로)")


def test_receipt_keyboard_is_exactly_one_edit_button():
    assert RECEIPT_KEYBOARD == {"inline_keyboard": [[
        {"text": "수정", "callback_data": CB_RECEIPT_EDIT}]]}
    buttons = [button for row in RECEIPT_KEYBOARD["inline_keyboard"]
               for button in row]
    assert len(buttons) == 1
    assert [button["text"] for button in buttons] == ["수정"]
    assert "확인" not in str(RECEIPT_KEYBOARD)
    assert "취소" not in str(RECEIPT_KEYBOARD)


def test_receipt_flush_attaches_only_the_edit_button(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_callback(10, 101, CB_QUESTION_INPUT)],
             playbooks=playbooks, sender=sender)
    prompt_id = _input_prompt_id(store)
    _consume(store, [_reply(11, 1100, prompt_id, "그냥 몰빵")],
             playbooks=playbooks, sender=sender)
    receipt_text, receipt_kwargs = sender.calls[-1]
    assert receipt_kwargs == {"reply_markup": RECEIPT_KEYBOARD}
    assert receipt_text == (
        "2026-08-21 06:10 / MRNA(모더나)\n"
        "플레이북에 매매 사유를 저장했어요.\n"
        "---\n"
        "그냥 몰빵")


# --- RED-2: immediate append, no second confirmation -----------------------


def test_direct_reply_appends_immediately_without_a_preview(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    raw = "급등해서 절반 익절  (오타그대로)"
    result = _consume(store, [_reply(20, 2000, 101, f"추가: {raw}")],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 1
    assert result.receipts_enqueued == 1
    assert result.previews_enqueued == 0
    document = _document(playbooks).read_text(encoding="utf-8")
    assert raw in document
    assert document.count("USER_RAW_START") == 1
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED
    assert store.read_pending_append(_event().trade_event_id) is None
    assert not _receipts(store, "trade_reason_preview")


def test_recommendation_adoption_appends_immediately(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path,
                               recommendation="목표가 도달로 일부 익절")
    result = _consume(store, [_callback(30, 101, CB_QUESTION_YES)],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 1
    assert result.receipts_enqueued == 1
    document = _document(playbooks).read_text(encoding="utf-8")
    assert "목표가 도달로 일부 익절" in document
    receipt = _receipts(store)[0]
    assert receipt["message_text"] == (
        "2026-08-21 06:10 / MRNA(모더나)\n"
        "플레이북에 매매 사유를 저장했어요.\n"
        "---\n"
        "목표가 도달로 일부 익절")


def test_input_prompt_reply_appends_immediately_byte_exact(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    opened = _consume(store, [_callback(40, 101, CB_QUESTION_INPUT)],
                      playbooks=playbooks, sender=sender)
    assert opened.input_prompts_enqueued == 1
    raw = "  앞뒤 공백도 그대로  "
    result = _consume(
        store, [_reply(41, 4100, _input_prompt_id(store), raw)],
        playbooks=playbooks, sender=sender)
    assert result.appends == 1
    assert raw in _document(playbooks).read_text(encoding="utf-8")


def test_none_button_stays_terminal_and_writes_nothing(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    before = _document(playbooks).read_bytes()
    result = _consume(store, [_callback(50, 101, "q1:none")],
                      playbooks=playbooks, sender=sender)
    assert (result.appends, result.receipts_enqueued) == (0, 0)
    assert _document(playbooks).read_bytes() == before
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED


def test_replayed_submission_appends_exactly_once(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    update = _reply(60, 6000, 101, "추가: 사유 원문")
    first = _consume(store, [update], playbooks=playbooks, sender=sender)
    replay = _consume(store, [update], playbooks=playbooks, sender=sender)
    assert (first.appends, replay.appends) == (1, 0)
    assert _document(playbooks).read_text(
        encoding="utf-8").count("USER_RAW_START") == 1
    assert _claims(store) == 1


def test_restart_replay_of_the_whole_pass_is_idempotent(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_reply(70, 7000, 101, "추가: 사유")],
             playbooks=playbooks, sender=sender)
    snapshot = _document(playbooks).read_bytes()
    # a fresh store handle over the same file: the process restarted
    restarted = ExecutionDialogueStore(store.path, clock=lambda: NOW)
    again = _consume(restarted, [], playbooks=playbooks, sender=sender)
    assert again.appends == 0
    assert _document(playbooks).read_bytes() == snapshot
    assert _claims(store) == 1


# --- RED-3: 수정 preserves source history ----------------------------------


def test_edit_appends_a_linked_correction_and_keeps_the_original(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_reply(80, 8000, 101, "추가: 원래 사유")],
             playbooks=playbooks, sender=sender)
    receipt = _receipts(store)[0]
    original_id = _document(playbooks).read_text(
        encoding="utf-8").split('<a id="entry-', 1)[1].split('"', 1)[0]

    edit = _consume(
        store, [_callback(81, receipt["telegram_message_id"],
                          CB_RECEIPT_EDIT)],
        playbooks=playbooks, sender=sender)
    assert edit.input_prompts_enqueued == 1
    assert edit.appends == 0
    corrected = _consume(
        store,
        [_reply(82, 8200, _input_prompt_id(store), "사실은 리밸런싱 때문")],
        playbooks=playbooks, sender=sender)
    assert corrected.appends == 1

    document = _document(playbooks).read_text(encoding="utf-8")
    assert "원래 사유" in document        # never overwritten
    assert "사실은 리밸런싱 때문" in document
    assert document.count("USER_RAW_START") == 2
    assert f"`supersedes_entry_id`: `{original_id}`" in document
    # the latest correction is the current interpretation: it leads layer 3
    assert document.index("사실은 리밸런싱 때문") < document.index("원래 사유")


def test_edit_receipt_carries_the_corrected_bytes(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_reply(90, 9000, 101, "추가: 원래 사유")],
             playbooks=playbooks, sender=sender)
    first_receipt = _receipts(store)[0]
    _consume(store, [_callback(91, first_receipt["telegram_message_id"],
                               CB_RECEIPT_EDIT)],
             playbooks=playbooks, sender=sender)
    _consume(store, [_reply(92, 9200, _input_prompt_id(store), "정정 원문")],
             playbooks=playbooks, sender=sender)
    latest_text, latest_kwargs = sender.calls[-1]
    assert latest_text.endswith("---\n정정 원문")
    assert latest_kwargs == {"reply_markup": RECEIPT_KEYBOARD}
    assert len(_receipts(store)) == 2


def test_edit_never_deletes_the_prior_claim(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_reply(95, 9500, 101, "추가: 첫 원문")],
             playbooks=playbooks, sender=sender)
    receipt = _receipts(store)[0]
    _consume(store, [_callback(96, receipt["telegram_message_id"],
                               CB_RECEIPT_EDIT)],
             playbooks=playbooks, sender=sender)
    _consume(store, [_reply(97, 9700, _input_prompt_id(store), "둘째 원문")],
             playbooks=playbooks, sender=sender)
    assert _claims(store) == 2


# --- RED-4: save failure never shows a success receipt ---------------------


def test_validator_rejection_gives_no_receipt_and_keeps_the_bytes(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    document = _document(playbooks)
    before = document.read_bytes()

    def rejecting(root):
        return (["rejected"] if "USER_RAW_START"
                in document.read_text(encoding="utf-8") else [])

    result = _consume(store, [_reply(100, 10000, 101, "추가: 사유")],
                      playbooks=playbooks, sender=sender,
                      validator=rejecting)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0
    assert _receipts(store) == []
    assert document.read_bytes() == before
    assert _claims(store) == 0
    pending = store.read_pending_append(_event().trade_event_id)
    assert pending["payload"] == "사유".encode()
    assert store.read_event(_event().trade_event_id)["state"] \
        is not TradeEventState.ANSWERED


def test_write_failure_is_retried_and_converges_to_one_entry(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)

    def crash(path, data):
        raise OSError("fault: crash before replace")

    monkeypatch.setattr(execution_dialogue, "_write_playbook_bytes_durable",
                        crash)
    failed = _consume(store, [_reply(110, 11000, 101, "추가: 사유")],
                      playbooks=playbooks, sender=sender)
    assert (failed.appends, failed.receipts_enqueued) == (0, 0)
    monkeypatch.undo()
    # the bounded sweep retries the surviving pending row with no new input
    retried = _consume(store, [], playbooks=playbooks, sender=sender)
    assert retried.appends == 1
    assert retried.receipts_enqueued == 1
    assert _document(playbooks).read_text(
        encoding="utf-8").count("USER_RAW_START") == 1


def test_retry_is_bounded_and_never_loses_the_pending_bytes(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)

    def always_rejects(root):
        return ["rejected"]

    _consume(store, [_reply(120, 12000, 101, "추가: 잃으면 안 되는 원문")],
             playbooks=playbooks, sender=sender, validator=always_rejects)
    for _ in range(PENDING_APPEND_MAX_ATTEMPTS + 3):
        _consume(store, [], playbooks=playbooks, sender=sender,
                 validator=always_rejects)
    pending = store.read_pending_append(_event().trade_event_id)
    assert pending["payload"] == "잃으면 안 되는 원문".encode()
    assert pending["attempts"] == PENDING_APPEND_MAX_ATTEMPTS
    assert _receipts(store) == []
    # exactly one truthful failure notice, never a success receipt
    notices = [row for row in store.list_outbox()
               if row["prompt_kind"] == "trade_reason_reconfirm"]
    assert len(notices) == 1
    assert "저장" in notices[0]["message_text"]


def test_exhausted_retry_resumes_on_a_fresh_submission(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)

    def always_rejects(root):
        return ["rejected"]

    _consume(store, [_reply(130, 13000, 101, "추가: 첫 시도")],
             playbooks=playbooks, sender=sender, validator=always_rejects)
    for _ in range(PENDING_APPEND_MAX_ATTEMPTS + 2):
        _consume(store, [], playbooks=playbooks, sender=sender,
                 validator=always_rejects)
    healthy = _consume(store, [_reply(140, 14000, 101, "추가: 다시 시도")],
                       playbooks=playbooks, sender=sender)
    assert healthy.appends == 1
    assert "다시 시도" in _document(playbooks).read_text(encoding="utf-8")


# --- RED-5: conditional pre-ledger input fabricates nothing ----------------


def _conditional_question(store, sender, playbooks, *, alert_id):
    assert store.record_conditional_alert(
        alert_id, conditional_order_id="cond-1", leg_index=0,
        triggered_order_id="ord-mrna-1", account_scope=TOSS_SCOPE,
        symbol="MRNA", market_class="US", order_type="LIMIT",
        quantity_ordered="3", prev_status="WATCHING",
        new_status="COMPLETED") == "created"
    key = dialogue_dedupe_key(alert_id, "conditional_fill_question",
                              PROMPT_VERSION)
    assert store.enqueue_outbox(
        key, trade_event_id=alert_id,
        prompt_kind="conditional_fill_question",
        prompt_version=PROMPT_VERSION,
        message_text="MRNA(모더나)\nV 조건주문 체결이 감지") == "created"
    # Existing delivered messages remain replyable after submissions are retired.
    store.mark_outbox(key, "SENT",
        telegram_message_id=sender("historical submission"))
    return store.read_outbox(key)["telegram_message_id"]


def test_conditional_input_is_durable_and_truthfully_pending(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _playbook_root(tmp_path)
    alert_id = conditional_alert_id("cond-1", 0, "COMPLETED")
    question_id = _conditional_question(store, sender, playbooks,
                                        alert_id=alert_id)
    _consume(store, [_callback(200, question_id, CB_QUESTION_INPUT)],
             playbooks=playbooks, sender=sender)
    raw = "조건매수 계획에 따른 진입"
    result = _consume(
        store, [_reply(201, 20100, _input_prompt_id(store), raw)],
        playbooks=playbooks, sender=sender)
    # nothing is fabricated: no trade event, no playbook entry, no claim
    assert result.appends == 0
    assert store.read_event(alert_id) is None
    assert "USER_RAW_START" not in _document(playbooks).read_text(
        encoding="utf-8")
    assert _claims(store) == 0
    # but the bytes are durable and the user is told the truth
    assert store.read_pending_append(alert_id)["payload"] == raw.encode()
    pending_receipt = _receipts(store, "trade_reason_pending_receipt")
    assert len(pending_receipt) == 1
    assert pending_receipt[0]["message_text"].endswith(f"---\n{raw}")
    assert "원장" in pending_receipt[0]["message_text"]
    assert "플레이북에 매매 사유를 저장했어요." \
        not in pending_receipt[0]["message_text"]


def test_conditional_pending_receipt_edit_replaces_the_stored_bytes(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _playbook_root(tmp_path)
    alert_id = conditional_alert_id("cond-1", 0, "COMPLETED")
    question_id = _conditional_question(store, sender, playbooks,
                                        alert_id=alert_id)
    _consume(store, [_callback(210, question_id, CB_QUESTION_INPUT)],
             playbooks=playbooks, sender=sender)
    first_prompt = _input_prompt_id(store)
    _consume(store, [_reply(211, 21100, first_prompt, "첫 입력")],
             playbooks=playbooks, sender=sender)
    pending_receipt = _receipts(store, "trade_reason_pending_receipt")[0]
    _consume(store, [_callback(212, pending_receipt["telegram_message_id"],
                               CB_RECEIPT_EDIT)],
             playbooks=playbooks, sender=sender)
    _consume(store, [_reply(213, 21300,
                            _input_prompt_id(store, exclude=(first_prompt,)),
                            "둘째 입력")],
             playbooks=playbooks, sender=sender)
    assert store.read_pending_append(alert_id)["payload"] == "둘째 입력".encode()
    assert _claims(store) == 0


def test_conditional_bytes_append_exactly_once_after_migration(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _playbook_root(tmp_path)
    alert_id = conditional_alert_id("cond-1", 0, "COMPLETED")
    question_id = _conditional_question(store, sender, playbooks,
                                        alert_id=alert_id)
    _consume(store, [_callback(220, question_id, CB_QUESTION_INPUT)],
             playbooks=playbooks, sender=sender)
    raw = "조건매수 계획에 따른 진입"
    _consume(store, [_reply(221, 22100, _input_prompt_id(store), raw)],
             playbooks=playbooks, sender=sender)

    # the regular order becomes canonical: the alert migrates
    run_execution_dialogue(
        new_events=(_event(),), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=playbooks,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    event_id = _event().trade_event_id
    assert store.read_pending_append(event_id) is not None

    settled = _consume(store, [], playbooks=playbooks, sender=sender)
    assert settled.appends == 1
    assert settled.receipts_enqueued == 1
    document = _document(playbooks).read_text(encoding="utf-8")
    assert raw in document
    assert document.count("USER_RAW_START") == 1
    receipt = _receipts(store)[0]
    assert receipt["message_text"] == (
        "2026-08-21 06:10 / MRNA(모더나)\n"
        "플레이북에 매매 사유를 저장했어요.\n"
        "---\n"
        + raw)
    # replayed sweeps never append a second time
    again = _consume(store, [], playbooks=playbooks, sender=sender)
    assert again.appends == 0
    assert _document(playbooks).read_text(
        encoding="utf-8").count("USER_RAW_START") == 1
    assert _claims(store) == 1


# --- RED-6: old in-flight preview buttons stay safe ------------------------


def test_legacy_preview_row_still_confirms_without_a_second_send(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    event_id = _event().trade_event_id
    legacy_key = dialogue_dedupe_key(event_id, "trade_reason_preview", "v1")
    store.enqueue_outbox(
        legacy_key, trade_event_id=event_id,
        prompt_kind="trade_reason_preview", prompt_version="v1",
        message_text="아래 원문을 플레이북 매매이력에 그대로 추가할까요?\n---\n"
                     "구버전 원문")
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=300,
        inbound_message_id=30000, preview_dedupe_key=legacy_key,
        payload="구버전 원문".encode())
    run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=playbooks,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    preview_id = store.read_outbox(legacy_key)["telegram_message_id"]
    result = _consume(store, [_callback(301, preview_id, "p1:ok")],
                      playbooks=playbooks, sender=sender)
    assert result.appends == 1
    assert "구버전 원문" in _document(playbooks).read_text(encoding="utf-8")


def test_legacy_preview_cancel_and_edit_stay_safe(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    event_id = _event().trade_event_id
    legacy_key = dialogue_dedupe_key(event_id, "trade_reason_preview", "v1")
    store.enqueue_outbox(
        legacy_key, trade_event_id=event_id,
        prompt_kind="trade_reason_preview", prompt_version="v1",
        message_text="구버전 미리보기")
    store.create_pending_append(
        trade_event_id=event_id, provider_update_id=310,
        inbound_message_id=31000, preview_dedupe_key=legacy_key,
        payload="구버전 원문".encode())
    run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=playbooks,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)
    preview_id = store.read_outbox(legacy_key)["telegram_message_id"]
    before = _document(playbooks).read_bytes()
    cancelled = _consume(store, [_callback(311, preview_id, "p1:cancel")],
                         playbooks=playbooks, sender=sender)
    assert cancelled.appends == 0
    assert store.read_pending_append(event_id) is None
    assert _document(playbooks).read_bytes() == before
    late = _consume(store, [_callback(312, preview_id, "p1:ok"),
                            _callback(313, preview_id, "p1:edit")],
                    playbooks=playbooks, sender=sender)
    assert late.appends == 0
    assert late.input_prompts_enqueued == 1


def test_unknown_or_stale_callback_data_is_ignored_safely(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    before = _document(playbooks).read_bytes()
    result = _consume(store, [_callback(320, 101, "q1:legacy-unknown"),
                              _callback(321, 999999, CB_RECEIPT_EDIT)],
                      playbooks=playbooks, sender=sender)
    assert (result.appends, result.receipts_enqueued) == (0, 0)
    assert _document(playbooks).read_bytes() == before
    assert store.read_inbound_cursor() == 322


def test_receipt_callback_binds_to_its_own_source_message(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    playbooks = _send_question(store, sender, tmp_path=tmp_path)
    _consume(store, [_reply(330, 33000, 101, "추가: 사유")],
             playbooks=playbooks, sender=sender)
    receipt_id = _receipts(store)[0]["telegram_message_id"]
    # the edit press must name the receipt bubble, not the question bubble
    wrong = _consume(store, [_callback(331, 101, CB_RECEIPT_EDIT)],
                     playbooks=playbooks, sender=sender)
    assert wrong.input_prompts_enqueued == 0
    right = _consume(store, [_callback(332, receipt_id, CB_RECEIPT_EDIT)],
                     playbooks=playbooks, sender=sender)
    assert right.input_prompts_enqueued == 1
