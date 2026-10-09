"""Stage 4 B: bounded harness lifecycle channel (dry-run default, optional
Telegram stock-bot sender).

The 48-hour live shadow may emit only typed harness lifecycle events —
START once, FATAL/STALLED durably deduplicated per signature with a
cooldown, FINISH once — never a stock signal or analysis alert. The default
sink is a local dry-run file with no network; the optional Telegram sender
uses stdlib HTTPS only, reads its token/chat id exclusively from the
environment, and never exposes the token through repr, describe, logs, or
state.
"""

import json
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.observability.shadow_lifecycle import (
    DryRunLifecycleSink,
    HarnessLifecycleEventV1,
    LifecycleDeduperV1,
    LifecycleTransportError,
    TelegramStockBotSender,
    build_lifecycle_sink,
)

UTC = UTC
NOW = datetime(2026, 8, 2, 6, 30, tzinfo=UTC)


def event(kind, *, signature=None, detail=None, at=NOW):
    return HarnessLifecycleEventV1(
        kind=kind,
        label="tokmo-v3-48h-shadow",
        at=at,
        signature=signature,
        detail=detail,
    )


# -- typed events -----------------------------------------------------------


def test_event_kinds_are_closed_and_fatal_requires_signature():
    assert event("START").kind == "START"
    assert event("FATAL", signature="invoke_RuntimeError").signature
    with pytest.raises(ValueError):
        event("STOCK_ALERT")
    with pytest.raises(ValueError):
        event("FATAL")  # dedup signature is mandatory
    with pytest.raises(ValueError):
        event("STALLED")


def test_event_detail_is_token_shaped_never_free_text():
    assert event("FINISH", detail="completed_576_ticks").detail
    with pytest.raises(ValueError):
        event("FINISH", detail="bearer secret-value leaked")
    with pytest.raises(ValueError):
        event("START", detail="x" * 200)


def test_event_time_must_be_aware():
    with pytest.raises(ValueError):
        HarnessLifecycleEventV1(
            kind="START",
            label="x",
            at=datetime(2026, 8, 2),
        )


# -- durable dedup ----------------------------------------------------------


def test_start_and_finish_admit_exactly_once_durably(tmp_path):
    path = tmp_path / "dedup.json"
    clock = lambda: NOW
    deduper = LifecycleDeduperV1(path, clock=clock)
    assert deduper.admit(event("START")) is True
    assert deduper.admit(event("START")) is False
    assert deduper.admit(event("FINISH")) is True
    assert deduper.admit(event("FINISH")) is False
    # durability across process restart
    fresh = LifecycleDeduperV1(path, clock=clock)
    assert fresh.admit(event("START")) is False
    assert fresh.admit(event("FINISH")) is False
    assert oct(path.stat().st_mode & 0o777) == "0o600"


def test_fatal_same_signature_dedups_within_cooldown(tmp_path):
    moments = {"now": NOW}
    deduper = LifecycleDeduperV1(
        tmp_path / "dedup.json",
        clock=lambda: moments["now"],
        cooldown_seconds=3600,
    )
    fatal = lambda: event("FATAL", signature="invoke_RuntimeError", at=moments["now"])
    assert deduper.admit(fatal()) is True
    moments["now"] = NOW + timedelta(seconds=600)
    assert deduper.admit(fatal()) is False  # same signature inside cooldown
    other = event("FATAL", signature="tick_overrun", at=moments["now"])
    assert deduper.admit(other) is True  # distinct signature admitted
    moments["now"] = NOW + timedelta(seconds=3601)
    assert deduper.admit(fatal()) is True  # cooldown elapsed: at most one per
    # durable: a fresh instance still honours the last send times
    fresh = LifecycleDeduperV1(
        tmp_path / "dedup.json",
        clock=lambda: moments["now"],
        cooldown_seconds=3600,
    )
    assert fresh.admit(fatal()) is False


# -- dry-run sink (default: local, no network) ------------------------------


def test_dry_run_sink_appends_json_lines_locally(tmp_path):
    path = tmp_path / "lifecycle.jsonl"
    sink = DryRunLifecycleSink(path)
    sink(event("START"))
    sink(event("FATAL", signature="tick_overrun", detail="lag_301s"))
    lines = [json.loads(line) for line in path.read_text().splitlines()]
    assert [line["kind"] for line in lines] == ["START", "FATAL"]
    assert lines[1]["signature"] == "tick_overrun"
    assert oct(path.stat().st_mode & 0o777) == "0o600"


# -- optional Telegram stock-bot sender -------------------------------------


def test_telegram_sender_reads_env_only_and_uses_stdlib_https(tmp_path):
    sent = []

    def opener(url, data, timeout):
        sent.append({"url": url, "data": data, "timeout": timeout})

    sender = TelegramStockBotSender(
        environ={
            "TELEGRAM_STOCK_BOT_TOKEN": "123:secret-token-value",
            "TELEGRAM_STOCK_BOT_CHAT_ID": "424242",
        },
        opener=opener,
    )
    sender(event("START", detail="campaign_begin"))
    assert len(sent) == 1
    assert sent[0]["url"].startswith("https://api.telegram.org/bot")
    assert sent[0]["url"].endswith("/sendMessage")
    body = sent[0]["data"].decode()
    assert "chat_id=424242" in body
    assert "START" in body
    assert "campaign_begin" in body
    # the token never leaks through repr or describe
    assert "secret-token-value" not in repr(sender)
    assert "secret-token-value" not in sender.describe()
    assert sender.describe() == "telegram-stock-bot:configured"


def test_telegram_sender_refuses_without_env_credentials():
    with pytest.raises(LifecycleTransportError):
        TelegramStockBotSender(environ={}, opener=lambda *a, **k: None)
    with pytest.raises(LifecycleTransportError):
        TelegramStockBotSender(
            environ={"TELEGRAM_STOCK_BOT_TOKEN": "123:x"},
            opener=lambda *a, **k: None,
        )


def test_telegram_send_failure_never_raises_into_the_harness():
    def opener(url, data, timeout):
        raise OSError("network down")

    sender = TelegramStockBotSender(
        environ={
            "TELEGRAM_STOCK_BOT_TOKEN": "123:x",
            "TELEGRAM_STOCK_BOT_CHAT_ID": "1",
        },
        opener=opener,
    )
    sender(event("FATAL", signature="sig"))  # must not raise


# -- composed sink pipeline -------------------------------------------------


def test_build_lifecycle_sink_default_is_dry_run_with_dedup(tmp_path):
    sink = build_lifecycle_sink(
        "dry-run",
        state_dir=tmp_path,
        clock=lambda: NOW,
        environ={},
    )
    sink(event("START"))
    sink(event("START"))  # deduplicated
    lines = (tmp_path / "lifecycle_log.jsonl").read_text().splitlines()
    assert len(lines) == 1


def test_build_lifecycle_sink_telegram_requires_env():
    with pytest.raises(LifecycleTransportError):
        build_lifecycle_sink(
            "telegram",
            state_dir=None,
            clock=lambda: NOW,
            environ={},
        )
    with pytest.raises(ValueError):
        build_lifecycle_sink(
            "loudspeaker",
            state_dir=None,
            clock=lambda: NOW,
            environ={},
        )
