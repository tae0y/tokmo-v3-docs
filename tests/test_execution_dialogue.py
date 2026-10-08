"""Trade-event dialogue pipeline (Plan §19–§22): G3-off default, append gate."""

import importlib.util
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.programs import execution_dialogue
from tokmo.programs.execution_detection import NewTradeExecutionV1
from tokmo.programs.execution_dialogue import (
    EVALUATION_POLICY_VERSION,
    PROMPT_VERSION,
    build_trade_question_message,
    consume_inbound,
    extract_reason_payload,
    run_execution_dialogue,
)
from tokmo.records.execution_dialogue_store import (
    DialogueStoreError,
    ExecutionDialogueStore,
)
from tokmo.values.trade_event import (
    TradeEventEvaluationV1,
    TradeEventState,
    TradeEventTimesV1,
    TradeEventTransitionError,
    dialogue_dedupe_key,
    reason_append_id,
    trade_event_id,
    validate_transition,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


NOW = _utc("2026-08-10T04:00:00Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24


def _times(filled="2026-08-10T03:59:00Z", discovered="2026-08-10T04:00:00Z"):
    return TradeEventTimesV1(
        filled_at=_utc(filled), provider_observed_at=None,
        discovered_at=_utc(discovered), stored_at=_utc(discovered),
        notified_at=None,
    )


def _event(order_id="ord-001", scope=TOSS_SCOPE, symbol="SPCX",
           times=None):
    event_id = trade_event_id("toss-orders-v1", scope, order_id)
    return NewTradeExecutionV1(
        event_id, scope, order_id, symbol, "SELL", "USD", "3", "90.5",
        "271.5", "US", times or _times(),
    )


def _store(tmp_path):
    return ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3",
        clock=lambda: NOW,
    )


def _playbook_root(tmp_path, *, with_symbol=True):
    root = tmp_path / "playbooks"
    (root / "personal" / "US").mkdir(parents=True)
    (root / "personal" / "KR").mkdir(parents=True)
    (root / "retirement" / "KR").mkdir(parents=True)
    (root / "portfolio.md").write_text(
        "# 포트폴리오\n\n<!-- HERMES_CURRENT_START -->\n생각\n"
        "<!-- HERMES_CURRENT_END -->\n"
        "<!-- HERMES_ANCHORS_START -->\n<!-- HERMES_ANCHORS_END -->\n",
        encoding="utf-8")
    (root / "personal" / "_account.md").write_text(
        "# 계좌\n\n<!-- HERMES_CURRENT_START -->\n생각\n"
        "<!-- HERMES_CURRENT_END -->\n"
        "<!-- HERMES_ANCHORS_START -->\n<!-- HERMES_ANCHORS_END -->\n",
        encoding="utf-8")
    if with_symbol:
        (root / "personal" / "US" / "SPCX.md").write_text(
            '# SPCX\n\nplaybook_id: "personal:US:SPCX"\n\n'
            "## 1층 — 헤르의 생각\n\n<!-- HERMES_CURRENT_START -->\n"
            "성장 지속 관찰.\n<!-- HERMES_CURRENT_END -->\n\n"
            "## 2층 — 사용자의 원문\n\n<!-- USER_THESIS_START -->\n"
            "장기 보유.\n<!-- USER_THESIS_END -->\n\n"
            "## 3층 — 매매이력\n",
            encoding="utf-8")
    return root


def _validator():
    spec = importlib.util.spec_from_file_location(
        "validate_playbooks",
        Path(__file__).parents[1] / "scripts" / "playbooks"
        / "validate_playbooks.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.validate_tree


class _Sender:
    def __init__(self, fail=False, unknown=False):
        self.sent = []
        self.markups = []
        self.fail = fail
        self.unknown = unknown
        self._next_id = 100

    def __call__(self, text, reply_markup=None):
        if self.unknown:
            self.sent.append(text)
            raise TimeoutError("settle unknown")
        if self.fail:
            raise RuntimeError("send failed")
        self.sent.append(text)
        self.markups.append(reply_markup)
        self._next_id += 1
        return self._next_id


def _run_dialogue(store, events=(), *, sender=None, activation=False,
                  ai_evaluator=None, quote_reader=None, playbook_root=None):
    return run_execution_dialogue(
        new_events=tuple(events),
        dialogue_store=store,
        quote_reader=quote_reader or (lambda symbol: None),
        playbook_root=playbook_root,
        ai_evaluator=ai_evaluator,
        sender=sender,
        activation_enabled=activation,
        send_eligible_after=NOW,
        clock=lambda: NOW,
        now=NOW,
    )


def test_flush_fail_closed_before_listing_for_invalid_cutoff():
    from tokmo.programs.execution_dialogue import _flush_outbox

    class Store:
        def list_outbox(self, **kwargs):
            raise AssertionError("must not list")

    assert _flush_outbox(
        Store(), lambda text: 1, activation_enabled=True,
        send_eligible_after=None) == 0
    assert _flush_outbox(
        Store(), lambda text: 1, activation_enabled=True,
        send_eligible_after=datetime(2026, 1, 1)) == 0


# --- trade_event value contracts -----------------------------------------


def test_state_machine_transitions_are_closed():
    validate_transition(TradeEventState.DETECTED, TradeEventState.EVALUATED)
    validate_transition(TradeEventState.EVALUATED, TradeEventState.ENQUEUED)
    validate_transition(TradeEventState.ENQUEUED, TradeEventState.SENT)
    validate_transition(TradeEventState.SENT, TradeEventState.ANSWERED)
    for illegal in (
        (TradeEventState.DETECTED, TradeEventState.SENT),
        (TradeEventState.ANSWERED, TradeEventState.SENT),
        (TradeEventState.SUPPRESSED, TradeEventState.ENQUEUED),
        (TradeEventState.SENT, TradeEventState.ENQUEUED),
    ):
        with pytest.raises(TradeEventTransitionError):
            validate_transition(*illegal)


def test_trade_event_id_is_ledger_identity_hash():
    first = trade_event_id("toss-orders-v1", TOSS_SCOPE, "ord-001")
    same = trade_event_id("toss-orders-v1", TOSS_SCOPE, "ord-001")
    other = trade_event_id("toss-orders-v1", TOSS_SCOPE, "ord-002")
    assert first == same and first != other and len(first) == 64


def test_evaluation_reason_field_is_structurally_null():
    with pytest.raises(ValueError):
        TradeEventEvaluationV1(
            "a" * 64, "policy-v1", "prose", None, True, False,
            inferred_trade_reason="사용자가 익절했을 것",
        )
    ok = TradeEventEvaluationV1("a" * 64, "policy-v1", "prose", None,
                                False, False)
    assert ok.inferred_trade_reason is None
    # AI consultation without sufficient material is structurally invalid.
    with pytest.raises(ValueError):
        TradeEventEvaluationV1("a" * 64, "policy-v1", "prose", None,
                               False, True)


# --- dialogue stage -------------------------------------------------------


def test_same_execution_over_many_ticks_yields_one_event_one_outbox(tmp_path):
    store = _store(tmp_path)
    for _ in range(60):
        _run_dialogue(store, [_event()])
    event_id = _event().trade_event_id
    assert store.list_events() == (event_id,)
    key = dialogue_dedupe_key(event_id, "trade_reason_question",
                              PROMPT_VERSION)
    rows = store.list_outbox()
    assert [row["dedupe_key"] for row in rows] == [key]


def test_activation_off_sends_nothing_and_leaves_outbox_pending(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    result = _run_dialogue(store, [_event()], sender=sender, activation=False)
    assert sender.sent == []
    assert result.send_calls == 0
    key = dialogue_dedupe_key(_event().trade_event_id,
                              "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(key)["status"] == "PENDING"
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ENQUEUED


def test_no_ai_call_when_deterministic_material_is_insufficient(tmp_path):
    store = _store(tmp_path)
    ai_calls = []

    def evaluator(payload):
        ai_calls.append(payload)
        raise AssertionError("must not be called")

    result = _run_dialogue(store, [_event()], ai_evaluator=evaluator)
    assert ai_calls == []
    assert result.ai_calls == 0
    key = dialogue_dedupe_key(_event().trade_event_id,
                              "trade_reason_question", PROMPT_VERSION)
    message = store.read_outbox(key)["message_text"]
    assert "관련 정보가 부족해 추천문구 없음" in message
    assert "바로 답장" in message


def test_late_arrival_shows_through_kst_times_only(tmp_path):
    # The confirmed contract carries no extra late-arrival line: the gap
    # is visible from the 체결/확인 KST timestamps themselves.
    store = _store(tmp_path)
    late = _event(times=_times(filled="2026-08-09T13:00:00Z"))
    _run_dialogue(store, [late])
    key = dialogue_dedupe_key(late.trade_event_id, "trade_reason_question",
                              PROMPT_VERSION)
    message = store.read_outbox(key)["message_text"]
    assert "늦게 확인" not in message
    assert "체결 2026-08-09 22:00 / 확인 2026-08-10 13:00" in message


def test_message_masks_scope_ref_token_and_paths(tmp_path):
    playbooks = _playbook_root(tmp_path)
    message = build_trade_question_message(
        _event(),
        evaluation=None,
        playbook_root=playbooks,
    )
    assert TOSS_SCOPE not in message
    assert "toss-acct" not in message
    assert "accountSeq" not in message.replace("accountSeq", "accountSeq") \
        or "accountSeq" not in message
    assert str(playbooks) not in message
    assert "/Users/" not in message
    pension_msg = build_trade_question_message(
        _event(scope=PENSION_SCOPE), evaluation=None, playbook_root=None)
    assert PENSION_SCOPE not in pension_msg
    assert "pension-excel" not in pension_msg


def test_send_settle_failure_is_unknown_and_never_resent(tmp_path):
    store = _store(tmp_path)
    sender = _Sender(unknown=True)
    _run_dialogue(store, [_event()], sender=sender, activation=True)
    key = dialogue_dedupe_key(_event().trade_event_id,
                              "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(key)["status"] == "UNKNOWN"
    assert len(sender.sent) == 1
    # The next tick must not resend the UNKNOWN row.
    _run_dialogue(store, [], sender=sender, activation=True)
    assert len(sender.sent) == 1
    with pytest.raises(TradeEventTransitionError):
        store.mark_outbox(key, "SENT")


def test_successful_send_binds_telegram_message_id(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    result = _run_dialogue(store, [_event()], sender=sender, activation=True)
    assert result.send_calls == 1
    key = dialogue_dedupe_key(_event().trade_event_id,
                              "trade_reason_question", PROMPT_VERSION)
    row = store.read_outbox(key)
    assert row["status"] == "SENT"
    assert row["telegram_message_id"] == 101
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.SENT


def test_ai_evaluator_runs_only_with_sufficient_material(tmp_path):
    store = _store(tmp_path)
    playbooks = _playbook_root(tmp_path)
    calls = []

    def evaluator(payload):
        calls.append(payload)
        return TradeEventEvaluationV1(
            payload["trade_event_id"], EVALUATION_POLICY_VERSION,
            "플레이북과 부분 일치합니다.", "partial", True, True,
        )

    result = _run_dialogue(
        store, [_event()],
        ai_evaluator=evaluator,
        quote_reader=lambda symbol: {"last_price": "91.0",
                                     "observed_at": NOW},
        playbook_root=playbooks,
    )
    assert result.ai_calls == 1
    assert len(calls) == 1
    # AI payload carries no reason field and no account scope.
    assert "account_scope" not in calls[0]
    key = dialogue_dedupe_key(_event().trade_event_id,
                              "trade_reason_question", PROMPT_VERSION)
    message = store.read_outbox(key)["message_text"]
    assert "추천 문구\n- 플레이북과 부분 일치합니다." in message


# --- inbound / append gate ------------------------------------------------


def test_extract_reason_payload_is_byte_exact_after_marker():
    assert extract_reason_payload("추가: 급등해서 익절  ".encode()) \
        == "급등해서 익절  ".encode()
    assert extract_reason_payload("추가:바로붙임".encode()) \
        == "바로붙임".encode()
    assert extract_reason_payload("추가하지 않음".encode()) is None
    assert extract_reason_payload("그냥 텍스트".encode()) is None


def _sent_question(store, sender=None):
    sender = sender or _Sender()
    _run_dialogue(store, [_event()], sender=sender, activation=True)
    return sender


def _consume(store, updates, *, playbook_root=None, validator=None,
             activation=True, sender=None):
    provider_calls = []

    def updates_provider(offset):
        provider_calls.append(offset)
        return tuple(updates)

    result = consume_inbound(
        dialogue_store=store,
        updates_provider=updates_provider,
        playbook_root=playbook_root,
        playbook_validator=validator or (lambda root: []),
        sender=sender,
        activation_enabled=activation,
        send_eligible_after=NOW,
        clock=lambda: NOW,
        now=NOW,
    )
    return result, provider_calls


def test_inbound_disabled_before_g3_polls_nothing(tmp_path):
    store = _store(tmp_path)
    result, calls = _consume(store, [], activation=False)
    assert result.status == "skipped"
    assert calls == []


def test_ignored_unauthorized_update_advances_cursor_without_raw_storage(tmp_path):
    store = _store(tmp_path)
    result, calls = _consume(store, [{"update_id": 7, "ignored": True}])
    assert result.status == "saved"
    assert calls == [0]
    assert store.read_inbound_cursor() == 8
    assert store.read_inbound(7) is None


def test_reply_binding_is_exact_reply_to_message_id(tmp_path):
    store = _store(tmp_path)
    _sent_question(store)  # question message id 101
    playbooks = _playbook_root(tmp_path)
    result, _calls = _consume(
        store,
        [{"update_id": 1, "message_id": 500, "reply_to_message_id": 101,
          "text": "추가: 목표가 도달로 일부 익절"}],
        playbook_root=playbooks, validator=_validator(),
    )
    # 2026-08-21: the explicit opt-in IS the submission — it saves at once
    # and answers with a receipt, never a second confirmation.
    assert result.appends == 1
    assert result.receipts_enqueued == 1
    assert result.previews_enqueued == 0
    assert store.read_pending_append(_event().trade_event_id) is None
    assert "목표가 도달로 일부 익절" in (
        playbooks / "personal" / "US" / "SPCX.md").read_text(encoding="utf-8")


def test_reply_without_reply_to_never_guesses_even_with_one_pending(tmp_path):
    store = _store(tmp_path)
    _sent_question(store)
    result, _calls = _consume(
        store,
        [{"update_id": 2, "message_id": 501, "reply_to_message_id": None,
          "text": "추가: 사유입니다"}],
    )
    assert result.appends == 0
    assert result.previews_enqueued == 0
    assert result.reconfirms_enqueued == 1
    assert store.read_pending_append(_event().trade_event_id) is None


def test_unknown_reply_to_id_reconfirms_without_append(tmp_path):
    store = _store(tmp_path)
    _sent_question(store)
    result, _calls = _consume(
        store,
        [{"update_id": 3, "message_id": 502, "reply_to_message_id": 99999,
          "text": "추가: 사유"}],
    )
    assert result.appends == 0
    assert result.reconfirms_enqueued == 1


def test_immediate_save_flow_appends_byte_for_byte(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    raw_reason = "급등해서 절반 익절, 나머지는 계속 보유  (오타그대로)"
    result, _calls = _consume(
        store,
        [{"update_id": 10, "message_id": 600, "reply_to_message_id": 101,
          "text": f"추가: {raw_reason}"}],
        playbook_root=playbooks, validator=validator, sender=sender,
    )
    # The receipt goes out on the same consume pass (activation on).
    receipt = next(row for row in store.list_outbox()
                   if row["prompt_kind"] == "trade_reason_receipt")
    assert receipt["status"] == "SENT"
    assert receipt["message_text"].endswith(f"---\n{raw_reason}")
    assert result.appends == 1
    document = (playbooks / "personal" / "US" / "SPCX.md").read_text(
        encoding="utf-8")
    assert raw_reason in document
    assert document.count("USER_RAW_START") == 1
    assert f"`v3_trade_event_id`: `{_event().trade_event_id}`" in document
    assert validator(playbooks) == []
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED
    # New entries land at the top of layer 3, right under the heading.
    heading_at = document.index("## 3층 — 매매이력")
    entry_at = document.index("<a id=\"entry-")
    assert heading_at < entry_at


def test_replayed_submission_update_appends_exactly_once(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    submission = {"update_id": 20, "message_id": 700,
                  "reply_to_message_id": 101, "text": "추가: 사유 원문"}
    first, _ = _consume(store, [submission], playbook_root=playbooks,
                        validator=validator, sender=sender)
    replay, _ = _consume(store, [submission], playbook_root=playbooks,
                         validator=validator, sender=sender)
    assert first.appends == 1
    assert replay.appends == 0
    document = (playbooks / "personal" / "US" / "SPCX.md").read_text(
        encoding="utf-8")
    assert document.count("USER_RAW_START") == 1


def test_decline_reply_answers_without_any_write(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    before = (playbooks / "personal" / "US" / "SPCX.md").read_bytes()
    result, _ = _consume(
        store,
        [{"update_id": 30, "message_id": 800, "reply_to_message_id": 101,
          "text": "추가하지 않음"}],
        playbook_root=playbooks, validator=_validator(), sender=sender,
    )
    assert result.appends == 0
    assert (playbooks / "personal" / "US" / "SPCX.md").read_bytes() == before
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED


def test_free_text_reply_is_not_an_explicit_opt_in(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    result, _ = _consume(
        store,
        [{"update_id": 40, "message_id": 900, "reply_to_message_id": 101,
          "text": "그냥 오르길래 팔았어"}],
    )
    assert result.appends == 0
    assert result.previews_enqueued == 0
    assert result.reconfirms_enqueued == 1


def test_update_replay_dedupes_on_provider_update_id(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    update = {"update_id": 50, "message_id": 1000,
              "reply_to_message_id": 101, "text": "추가: 사유"}
    first, _ = _consume(store, [update],
                        playbook_root=_playbook_root(tmp_path),
                        validator=_validator(), sender=sender)
    replay, _ = _consume(store, [update])
    assert first.receipts_enqueued == 1
    assert replay.receipts_enqueued == 0
    assert store.read_inbound_cursor() == 51


def _explicit_flow(store, playbooks, validator, sender, *, update_id,
                   message_id, reason_text):
    """One submission: '추가: …' reply, saved immediately (2026-08-21)."""
    return _consume(store, [{"update_id": update_id,
                             "message_id": message_id,
                             "reply_to_message_id": 101,
                             "text": f"추가: {reason_text}"}],
                    playbook_root=playbooks, validator=validator,
                    sender=sender)


def _retry_sweep(store, playbooks, validator, sender):
    """A later pass with no new input: the bounded pending sweep retries."""
    return _consume(store, [], playbook_root=playbooks, validator=validator,
                    sender=sender)


def test_correction_is_a_new_entry_never_a_rewrite(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    _explicit_flow(store, playbooks, validator, sender,
                   update_id=60, message_id=1100, reason_text="원래 사유")
    document = (playbooks / "personal" / "US" / "SPCX.md").read_text(
        encoding="utf-8")
    assert "원래 사유" in document
    original_id = document.split('<a id="entry-', 1)[1].split('"', 1)[0]
    # A correction arrives as a fresh submission for the same event.
    _explicit_flow(store, playbooks, validator, sender,
                   update_id=64, message_id=1104,
                   reason_text="정정: 사실은 리밸런싱 때문")
    document = (playbooks / "personal" / "US" / "SPCX.md").read_text(
        encoding="utf-8")
    assert "원래 사유" in document  # the original entry is immutable
    assert "리밸런싱 때문" in document
    assert document.count("USER_RAW_START") == 2
    # The correction names what it supersedes and leads layer 3, so the
    # current interpretation is deterministic without touching the original.
    assert f"`supersedes_entry_id`: `{original_id}`" in document
    assert document.index("리밸런싱 때문") < document.index("원래 사유")
    assert _validator()(playbooks) == []


# --- durable exactly-once append (SOL BLOCKER: claim-after-durable-write) --
#
# Contract: the deterministic entry anchor inside the canonical file is the
# recovery truth. The reason_appends claim is recorded only AFTER the bytes
# are atomically replaced, read back, and validator-approved; every failure
# keeps the pending row and a retry converges on exactly one USER_RAW entry.


def _claim_count(store):
    with sqlite3.connect(store.path) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM reason_appends").fetchone()[0]


def _symbol_document(playbooks):
    return playbooks / "personal" / "US" / "SPCX.md"


def _submit(store, playbooks, validator, sender, *, update_id, message_id,
            reason="목표 밴드 도달로 익절"):
    """One submission pass: the '추가:' reply attempts the save at once.

    Every later attempt is a plain sweep pass over the surviving pending
    row, so the inbound_message_id — and therefore the entry anchor — stays
    fixed across retries.
    """
    return _consume(store, [{"update_id": update_id, "message_id": message_id,
                             "reply_to_message_id": 101,
                             "text": f"추가: {reason}"}],
                    playbook_root=playbooks, validator=validator,
                    sender=sender)


def test_validator_after_failure_leaves_zero_claims_and_restores(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    document = _symbol_document(playbooks)

    def rejecting_validator(root):
        return (["rejected"] if "USER_RAW_START"
                in document.read_text(encoding="utf-8") else [])

    before = document.read_bytes()
    result, _ = _submit(store, playbooks, rejecting_validator, sender,
                        update_id=80, message_id=1300)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0  # never a false success
    assert document.read_bytes() == before
    assert _claim_count(store) == 0
    assert store.read_pending_append(_event().trade_event_id) is not None
    # a second attempt against the still-failing validator never duplicates
    result_again, _ = _retry_sweep(store, playbooks, rejecting_validator,
                                   sender)
    assert (result_again.appends, result_again.append_failures) == (0, 1)
    assert document.read_bytes() == before
    assert _claim_count(store) == 0
    # a retry with a healthy validator converges to exactly one entry
    result_retry, _ = _retry_sweep(store, playbooks, _validator(), sender)
    assert result_retry.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1
    assert store.read_pending_append(_event().trade_event_id) is None


def test_crash_before_replace_changes_nothing_and_retry_converges(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    document = _symbol_document(playbooks)
    before = document.read_bytes()

    def crash_before_replace(path, data):
        raise OSError("fault: crash before replace")

    monkeypatch.setattr(execution_dialogue, "_write_playbook_bytes_durable",
                        crash_before_replace)
    result, _ = _submit(store, playbooks, validator, sender,
                        update_id=90, message_id=1400)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0
    assert document.read_bytes() == before
    assert _claim_count(store) == 0
    assert store.read_pending_append(_event().trade_event_id) is not None
    monkeypatch.undo()
    result_retry, _ = _retry_sweep(store, playbooks, validator, sender)
    assert result_retry.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1


def test_crash_after_replace_before_claim_recovers_without_second_entry(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    document = _symbol_document(playbooks)
    real_write = execution_dialogue._write_playbook_bytes_durable

    def crash_after_replace(path, data):
        real_write(path, data)
        raise OSError("fault: crash after durable replace, before claim")

    monkeypatch.setattr(execution_dialogue, "_write_playbook_bytes_durable",
                        crash_after_replace)
    result, _ = _submit(store, playbooks, validator, sender,
                        update_id=100, message_id=1500)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 0
    assert store.read_pending_append(_event().trade_event_id) is not None
    monkeypatch.undo()
    # retry: the anchor already in the file settles the claim, no rewrite
    result_retry, _ = _retry_sweep(store, playbooks, validator, sender)
    assert result_retry.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1
    assert store.read_pending_append(_event().trade_event_id) is None
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED


def test_claim_failure_after_replace_keeps_pending_then_settles_once(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    document = _symbol_document(playbooks)

    def failing_claim(self, append_id, **kwargs):
        raise DialogueStoreError("fault: claim write failed")

    monkeypatch.setattr(ExecutionDialogueStore, "record_reason_append",
                        failing_claim)
    result, _ = _submit(store, playbooks, validator, sender,
                        update_id=110, message_id=1600)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert store.read_pending_append(_event().trade_event_id) is not None
    monkeypatch.undo()
    assert _claim_count(store) == 0
    result_retry, _ = _retry_sweep(store, playbooks, validator, sender)
    assert result_retry.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1


def test_restore_failure_keeps_pending_and_recovery_never_duplicates(
        tmp_path, monkeypatch):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    document = _symbol_document(playbooks)

    def rejecting_validator(root):
        return (["rejected"] if "USER_RAW_START"
                in document.read_text(encoding="utf-8") else [])

    real_write = execution_dialogue._write_playbook_bytes_durable
    calls = {"count": 0}

    def fail_restore_only(path, data):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("fault: restore write failed")
        real_write(path, data)

    monkeypatch.setattr(execution_dialogue, "_write_playbook_bytes_durable",
                        fail_restore_only)
    result, _ = _submit(store, playbooks, rejecting_validator, sender,
                        update_id=120, message_id=1700)
    assert (result.appends, result.append_failures) == (0, 1)
    assert result.receipts_enqueued == 0
    assert _claim_count(store) == 0
    assert store.read_pending_append(_event().trade_event_id) is not None
    monkeypatch.undo()
    # the entry is durably in the file: recovery settles it exactly once
    result_retry, _ = _retry_sweep(store, playbooks, _validator(), sender)
    assert result_retry.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1


def test_persistent_rejection_after_restore_failure_never_settles(
        tmp_path, monkeypatch):
    # SOL reclosure BLOCKER: after validator-after rejection + restore
    # failure the rejected entry is stuck in the canonical file. While the
    # validator still rejects the whole document, no retry may treat the
    # leftover anchor as proof of success: zero claims, pending kept, event
    # never ANSWERED, and the file is never rewritten.
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    document = _symbol_document(playbooks)

    def rejecting_validator(root):
        return (["rejected"] if "USER_RAW_START"
                in document.read_text(encoding="utf-8") else [])

    real_write = execution_dialogue._write_playbook_bytes_durable
    calls = {"count": 0}

    def fail_restore_only(path, data):
        calls["count"] += 1
        if calls["count"] == 2:
            raise OSError("fault: restore write failed")
        real_write(path, data)

    monkeypatch.setattr(execution_dialogue, "_write_playbook_bytes_durable",
                        fail_restore_only)
    result, _ = _submit(store, playbooks, rejecting_validator, sender,
                        update_id=150, message_id=2000)
    assert (result.appends, result.append_failures) == (0, 1)
    assert _claim_count(store) == 0
    monkeypatch.undo()
    stuck = document.read_bytes()

    def raising_validator(root):
        raise RuntimeError("fault: validator crashed")

    for validator in (rejecting_validator, raising_validator,
                      rejecting_validator):
        result_retry, _ = _retry_sweep(store, playbooks, validator, sender)
        assert (result_retry.appends, result_retry.append_failures) == (0, 1)
        assert result_retry.receipts_enqueued == 0
        assert _claim_count(store) == 0
        assert store.read_pending_append(_event().trade_event_id) is not None
        assert store.read_event(_event().trade_event_id)["state"] \
            is not TradeEventState.ANSWERED
        assert document.read_bytes() == stuck
        assert document.read_text(encoding="utf-8").count(
            "USER_RAW_START") == 1
    # Only once the validator approves the whole document again does the
    # already-durable entry settle: exactly one claim, no rewrite.
    result_retry, _ = _retry_sweep(store, playbooks, _validator(), sender)
    assert result_retry.appends == 1
    assert document.read_bytes() == stuck
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1
    assert store.read_pending_append(_event().trade_event_id) is None
    assert store.read_event(_event().trade_event_id)["state"] \
        is TradeEventState.ANSWERED


def test_legacy_claim_residue_is_repaired_into_the_file(tmp_path):
    # Residue of the abolished claim-first ordering: a durable claim exists
    # but the canonical file never received the bytes. The confirm must
    # write the entry instead of settling into permanent false-duplicate
    # data loss.
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    document = _symbol_document(playbooks)
    # The claim lands first, before the submission ever reaches the file.
    append_id = reason_append_id(_event().trade_event_id, 1800)
    assert store.record_reason_append(
        append_id, trade_event_id=_event().trade_event_id,
        inbound_message_id=1800) == "created"
    result, _ = _submit(store, playbooks, validator, sender,
                        update_id=130, message_id=1800)
    assert result.appends == 1
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    assert _claim_count(store) == 1


def test_replay_of_successful_append_settles_without_second_write(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path)
    validator = _validator()
    document = _symbol_document(playbooks)
    _explicit_flow(store, playbooks, validator, sender,
                   update_id=140, message_id=1900, reason_text="사유 원문")
    assert document.read_text(encoding="utf-8").count("USER_RAW_START") == 1
    event = store.read_event(_event().trade_event_id)
    result = execution_dialogue._perform_append(
        dialogue_store=store, event=event,
        payload="다른 payload여도 재기록 금지".encode(),
        inbound_message_id=1900, playbook_root=playbooks,
        playbook_validator=validator, now=NOW)
    assert result == "appended"
    text = document.read_text(encoding="utf-8")
    assert text.count("USER_RAW_START") == 1
    assert "사유 원문" in text
    assert "다른 payload" not in text
    assert _claim_count(store) == 1


def test_missing_playbook_file_is_created_and_reason_is_saved(tmp_path):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    playbooks = _playbook_root(tmp_path, with_symbol=False)
    validator = _validator()
    result, _ = _submit(store, playbooks, validator, sender,
                        update_id=70, message_id=1200, reason="사유")
    assert result.appends == 1
    assert result.append_failures == 0
    assert result.receipts_enqueued == 1
    assert "사유" in (playbooks / "personal" / "US" / "SPCX.md").read_text()
    assert validator(playbooks) == []
    assert store.read_pending_append(_event().trade_event_id) is None
