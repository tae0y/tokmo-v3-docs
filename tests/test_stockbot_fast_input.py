"""User-facing fast input, durability and coexistence with the minute Pulse."""

import importlib.util
import json
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from test_execution_dialogue import (
    NOW,
    _event,
    _playbook_root,
    _Sender,
    _sent_question,
    _store,
    _validator,
)

from tokmo.facts.telegram_dialogue_live import fetch_dialogue_updates
from tokmo.programs.execution_dialogue import consume_inbound


def setup(tmp_path, missing=False):
    store = _store(tmp_path)
    sender = _Sender()
    _sent_question(store, sender)
    books = _playbook_root(tmp_path, with_symbol=not missing)

    def consume(updates, **overrides):
        options = dict(
            dialogue_store=store,
            updates_provider=lambda offset: updates,
            playbook_root=books,
            playbook_validator=_validator(),
            sender=sender,
            activation_enabled=True,
            send_eligible_after=NOW,
            clock=lambda: NOW,
            now=NOW,
        )
        options.update(overrides)
        return consume_inbound(**options)

    return store, sender, books, consume


def reply(
    update=500, message=600, text="전쟁 수혜로 추매. 단기 상승 시 일부 매도 가능"
):
    return {
        "update_id": update,
        "message_id": message,
        "reply_to_message_id": 101,
        "text": text,
    }


def test_direct_natural_reply_creates_missing_file_without_button_or_ai(tmp_path):
    store, sender, books, consume = setup(tmp_path, missing=True)
    raw = "  이란전쟁 우크라이나전쟁 수혜주라고 생각해서 추매함\n단, 단기 상승 시 매도 가능  "
    result = consume([reply(text=raw)])
    assert result.appends == result.receipts_enqueued == 1
    assert result.input_prompts_enqueued == result.previews_enqueued == 0
    text = (books / "personal/US/SPCX.md").read_text()
    assert raw in text and text.count("USER_RAW_START") == 1
    assert "USER_THESIS_START" not in text  # No invented general investment thesis.
    assert _validator()(books) == []
    assert "저장했어요" in sender.sent[-1]
    assert store.read_pending_append(_event().trade_event_id) is None


def test_duplicate_reply_and_correction_keep_distinct_original_text(tmp_path):
    store, sender, books, consume = setup(tmp_path)
    assert consume([reply(text="첫 이유")]).appends == 1
    assert consume([reply(text="첫 이유")]).appends == 0
    assert (
        consume([reply(update=501, message=601, text="정정: 분할 매수임")]).appends == 1
    )
    text = (books / "personal/US/SPCX.md").read_text()
    assert text.count("USER_RAW_START") == 2
    assert "첫 이유" in text and "정정: 분할 매수임" in text
    assert "supersedes_entry_id" in text


def test_new_file_validation_failure_rolls_back_and_reports_immediately(tmp_path):
    store, sender, books, consume = setup(tmp_path, missing=True)
    target = books / "personal/US/SPCX.md"
    result = consume(
        [reply()], playbook_validator=lambda root: ["bad"] if target.exists() else []
    )
    assert result.appends == 0 and result.append_failures == 1
    assert result.failure_reasons == ("validator_failed_after",)
    assert not target.exists()
    pending = store.read_pending_append(_event().trade_event_id)
    assert pending["payload"] == reply()["text"].encode()
    assert "다시 입력하지 않아도" in sender.sent[-1]
    assert "저장했어요" not in sender.sent[-1]
    # Fast polling never burns the minute worker's retry budget.
    consume([], retry_pending=False)
    assert store.read_pending_append(_event().trade_event_id)["attempts"] == 1


def test_two_pollers_serialize_and_send_exactly_one_receipt(tmp_path):
    store, sender, books, consume = setup(tmp_path, missing=True)
    entered = threading.Event()
    release = threading.Event()
    second_entered = threading.Event()

    def first_provider(offset):
        entered.set()
        assert release.wait(3)
        return [reply()]

    def second_provider(offset):
        second_entered.set()
        assert offset == 501
        return []

    with ThreadPoolExecutor(2) as pool:
        first = pool.submit(consume, [], updates_provider=first_provider)
        assert entered.wait(2)
        second = pool.submit(consume, [], updates_provider=second_provider)
        assert not second_entered.wait(0.05)
        release.set()
        assert first.result(3).appends == 1
        assert second.result(3).appends == 0
    assert sum("저장했어요" in msg for msg in sender.sent) == 1


def test_physical_ledger_id_is_used_and_changed_trade_is_refused(tmp_path):
    store, sender, books, consume = setup(tmp_path, missing=True)
    event = store.read_event(_event().trade_event_id)
    db = store.path.with_name("trade_ledger.sqlite3")
    with sqlite3.connect(db) as c:
        c.execute(
            "CREATE TABLE trade_ledger_events(event_id,source,account_scope,source_record_key,instrument_raw,transaction_type_raw,quantity,unit_price,amount,currency_raw)"
        )
        c.execute(
            "INSERT INTO trade_ledger_events VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "pulse-actual-row",
                event["source"],
                event["account_scope"],
                event["source_record_key"],
                event["symbol"],
                event["side"],
                event["quantity"],
                event["unit_price"],
                event["amount"],
                event["currency"],
            ),
        )
    assert consume([reply()]).appends == 1
    text = (books / "personal/US/SPCX.md").read_text()
    assert "`source_ref`: `trade_ledger_events#pulse-actual-row`" in text
    assert "`v3_trade_event_id`: `pulse-actual-row`" in text
    with sqlite3.connect(db) as c:
        c.execute("UPDATE trade_ledger_events SET quantity='99'")
    rejected = consume([reply(update=501, message=601, text="또 다른 사유")])
    assert rejected.appends == 0 and rejected.failure_reasons == (
        "ledger_match_failed",
    )
    assert (books / "personal/US/SPCX.md").read_text() == text


def test_fast_runner_processes_reply_without_full_pulse_and_respects_gate(tmp_path):
    store, sender, books, consume = setup(tmp_path, missing=True)
    path = Path(__file__).parents[1] / "tools/run_stockbot_inbox.py"
    spec = importlib.util.spec_from_file_location("fast_inbox_test", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gate = tmp_path / "runtime/dialogue_activation.json"
    gate.parent.mkdir()
    gate.write_text(
        json.dumps({"enabled": True, "cutoff": NOW.isoformat(), "approved_by": "woong"})
    )
    started = time.monotonic()
    result = module.run_once(
        root=tmp_path,
        playbook_root=books,
        validator=_validator(),
        clock=lambda: NOW,
        updates_provider=lambda offset: [reply()],
        sender=sender,
        callback_ack=lambda x: None,
    )
    assert result["appends"] == 1
    assert time.monotonic() - started < 2  # No minute scheduler or model work.
    gate.write_text('{"enabled": false}')
    assert (
        module.run_once(
            root=tmp_path,
            playbook_root=books,
            validator=_validator(),
            clock=lambda: NOW,
            updates_provider=lambda offset: pytest.fail("gate is off"),
            sender=sender,
            callback_ack=None,
        )["status"]
        == "disabled"
    )


def test_transport_fast_path_uses_small_batch_and_configured_timeout():
    calls = []

    def opener(url, timeout):
        calls.append((url, timeout))
        return b'{"ok":true,"result":[]}'

    assert (
        fetch_dialogue_updates(
            offset=8,
            bot_token="fixture",
            chat_id="123",
            opener=opener,
            limit=1,
            timeout=3,
        )
        == ()
    )
    assert "limit=1&" in calls[0][0] and calls[0][1] == 3
