#!/usr/bin/env python3
"""Fast Stock-bot replies, using the same durable dialogue queue as Pulse.

No models or brokerage authentication. Existing Pulse stays as a fallback.
The shared processing lock serializes polling, migration, writes and receipts.
"""

from __future__ import annotations

import argparse
import fcntl
import importlib.util
import json
import os
import sys
import time
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from tokmo.facts.telegram_dialogue_live import (
    answer_dialogue_callback,
    fetch_dialogue_updates,
    send_dialogue_message,
)
from tokmo.programs.execution_dialogue import consume_inbound
from tokmo.programs.news_feedback import build_news_callback_handler
from tokmo.records.dialogue_activation import read_dialogue_activation
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore


def run_once(
    *,
    root,
    playbook_root,
    validator,
    clock,
    updates_provider,
    sender,
    callback_ack,
    news_callback_handler=None,
):
    gate = read_dialogue_activation(root / "runtime/dialogue_activation.json")
    if not gate.enabled:
        return {"status": "disabled"}
    result = consume_inbound(
        dialogue_store=ExecutionDialogueStore(
            root / "stores/execution_dialogue.sqlite3", clock=clock
        ),
        playbook_root=playbook_root,
        playbook_validator=validator,
        updates_provider=updates_provider,
        sender=sender,
        callback_ack=callback_ack,
        news_callback_handler=news_callback_handler,
        activation_enabled=True,
        send_eligible_after=gate.cutoff,
        clock=clock,
        now=clock(),
        retry_pending=False,
        outbox_limit=1,
    )
    return asdict(result)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / "var/data"
    )
    parser.add_argument(
        "--project-root",
        type=Path,
        default=Path.home() / "tokmo/tokmo-v3/projects/tokmo-v3",
    )
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    state = args.root / "ops/stockbot-inbox"
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock = os.open(state / "worker.lock", os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        return 0
    token = os.environ.get("TELEGRAM_STOCK_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_STOCK_BOT_CHAT_ID")
    if not token or not chat_id:
        print("stockbot inbox: missing credentials", file=sys.stderr)
        return 2
    spec = importlib.util.spec_from_file_location(
        "stockbot_playbook_validator",
        args.project_root / "scripts/playbooks/validate_playbooks.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    clock = lambda: datetime.now(UTC)
    news_handler = None
    if (args.root / "stores/news_episodes.sqlite3").exists() and (
        args.root / "ops/ops_kernel.sqlite3"
    ).exists():
        episodes = NewsEpisodeStore(args.root / "stores/news_episodes.sqlite3")
        ops = OpsKernelStore(
            args.root / "ops/ops_kernel.sqlite3", stable_phase_ids=frozenset()
        )
        news_handler = build_news_callback_handler(episodes, ops.read_outbox, now=clock)

    def updates(offset):
        return fetch_dialogue_updates(
            offset=offset, bot_token=token, chat_id=chat_id, timeout=8, limit=1
        )

    def sender(text, reply_markup=None):
        return send_dialogue_message(
            body_text=text,
            bot_token=token,
            chat_id=chat_id,
            reply_markup=reply_markup,
            timeout=10,
        )

    def ack(query):
        return answer_dialogue_callback(
            callback_query_id=query, bot_token=token, timeout=2
        )

    errors = 0
    totals = {"updates_seen": 0, "appends": 0, "send_calls": 0}
    last_activity = None
    last_failure = None
    while True:
        started = time.monotonic()
        try:
            enabled = (
                json.loads((args.root / "runtime/stockbot_inbox.json").read_text()).get(
                    "enabled"
                )
                is True
            )
            result = (
                run_once(
                    root=args.root,
                    playbook_root=args.project_root / "playbooks",
                    validator=module.validate_tree,
                    clock=clock,
                    updates_provider=updates,
                    sender=sender,
                    callback_ack=ack,
                    news_callback_handler=news_handler,
                )
                if enabled
                else {"status": "disabled"}
            )
            errors = 0
        except Exception as exc:
            # Never print exceptions containing URLs/tokens or user's text.
            result = {"status": "error", "error_type": type(exc).__name__}
            errors += 1
        duration = round(time.monotonic() - started, 3)
        for key in totals:
            totals[key] += result.get(key, 0)
        if result.get("updates_seen") or result.get("send_calls"):
            last_activity = {"at": clock().isoformat(), "seconds": duration, **result}
        if result.get("failure_reasons") or result.get("status") == "error":
            last_failure = {"at": clock().isoformat(), **result}
        health = {
            "updated_at": clock().isoformat(),
            "pid": os.getpid(),
            "poll_interval_seconds": 2,
            "cycle_seconds": duration,
            "consecutive_errors": errors,
            "totals": totals,
            "last_activity": last_activity,
            "last_failure": last_failure,
            **result,
        }
        temporary = state / "latest.tmp"
        temporary.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n")
        temporary.chmod(0o600)
        temporary.replace(state / "latest.json")
        if args.once:
            return 0 if result["status"] not in {"error", "degraded"} else 2
        time.sleep(min(60, 2 * 2 ** min(errors, 5)))


if __name__ == "__main__":
    raise SystemExit(main())
