#!/opt/homebrew/bin/python3.12
"""Bounded NEWS classifier cron; NEWS health goes directly to the Stock bot.

The worker's versioned notice is validated, sent with the existing Stock bot,
and then acknowledged to the worker. Nothing is forwarded on stdout to the
Hermes origin bot. Failure to send leaves the health notice unacknowledged.
The final summary must match the callback. Already-notified or nighttime
health stays silent; its raw detail remains in decision-worker/latest.json.
"""
from __future__ import annotations

import contextlib
import fcntl
import io
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path("/home/tokmo/.hermes/runtime/tokmo-v3/news-shadow-src/projects/tokmo-v3")
ROOT = Path("/home/tokmo/.local/share/tokmo-v3")
RUNTIME = Path("/home/tokmo/.hermes/runtime/tokmo-v3/news-classifier-worker")
STOCK_HEALTH_CREDENTIAL = Path('/home/tokmo/tokmo-v3/.secrets/hermes-credentials/news_decision_stock_telegram.env')
sys.path.insert(0, str(PROJECT / "src"))

SUMMARY = "news-decision-worker/1"
CODES = {"OK": 0, "DEGRADED": 2, "EXCEPTION": 1, "DISABLED": 0, "ALREADY_RUNNING": 0, "BEFORE_CUTOFF": 2}


def atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name("." + path.name + f".{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


def validated_payload(payload) -> dict:
    """Check the notice payload's version/status/code before anything reaches the real stdout."""
    contract = getattr(payload, "contract", None)
    if (not isinstance(payload, str) or not payload.strip() or not isinstance(contract, dict)
            or contract.get("summary") != SUMMARY or contract.get("status") not in CODES
            or contract.get("code") != CODES[contract["status"]]):
        raise RuntimeError(f"NEWS decision notice payload rejected before stdout: {str(payload)[:200]!r} {contract!r}")
    return dict(contract, notice=str(payload))


def validated_summary(raw: str, code, delivered: list) -> dict:
    lines = [line for line in raw.splitlines() if line.strip()]
    try:
        summary = json.loads(lines[-1]) if lines else None
    except ValueError:
        summary = None
    notice = summary.get("notice") if isinstance(summary, dict) else None
    if (not isinstance(summary, dict) or summary.get("summary") != SUMMARY or summary.get("status") not in CODES
            or summary.get("code") != code or CODES[summary["status"]] != code
            or not (notice is None or (isinstance(notice, str) and notice.strip()))):
        raise RuntimeError(f"NEWS decision worker contract mismatch: exit={code!r} output={raw[-600:]!r}")
    handoff = summary.get("notice_handoff")
    if isinstance(handoff, dict) and handoff.get("status") != "OK":
        raise RuntimeError("NEWS decision notice handoff failed: " + json.dumps(handoff, ensure_ascii=False))
    # What the callback actually wrote must be exactly what the final summary claims.
    expected = [dict(summary=SUMMARY, status=summary["status"], code=code, notice=notice)] if notice is not None else []
    if delivered != expected or (notice is not None) != (isinstance(handoff, dict) and handoff.get("status") == "OK"):
        raise RuntimeError(f"NEWS decision notice handoff mismatch: delivered={delivered!r} summary={lines[-1][-600:]!r}")
    return summary


def emit_stock_notice(payload, delivered, sender):
    record = validated_payload(payload)
    receipt = sender(record['notice'])
    delivered.append(record)
    return receipt


def main() -> int:
    RUNTIME.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_path = RUNTIME / "worker.lock"
    with lock_path.open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        from tokmo.programs.news_decision_config import read_decision_config
        if read_decision_config(ROOT).get("enabled"):
            sys.path.insert(0, str(PROJECT / "tools"))
            from run_news_decision_worker import main as decision_main
            delivered = []
            receipts = []

            def emit(payload) -> None:
                from news_health_stock_route import build_stock_health_sender
                from tokmo.facts.telegram_dialogue_live import send_dialogue_message
                sender = build_stock_health_sender(STOCK_HEALTH_CREDENTIAL, send_dialogue_message)
                receipts.append(emit_stock_notice(payload, delivered, sender))

            captured = io.StringIO()
            with contextlib.redirect_stdout(captured):
                code = decision_main(["--root", str(ROOT)], emit=emit)
            validated_summary(captured.getvalue(), code, delivered)
            if receipts:
                atomic_write(RUNTIME / 'stock-health-latest.json', json.dumps({
                    'at':datetime.now(timezone.utc).isoformat(), 'destination':'STOCK_BOT',
                    'provider_message_ids':receipts}, ensure_ascii=False)+'\n')
            return 0
        from tokmo.facts.news_http import zai_chat_once
        from tokmo.programs.news_classifier_worker import main as worker_main
        now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured):
            code = worker_main([
                "--root", str(ROOT), "--now", now,
                "--routing-state", "/home/tokmo/.hermes/MODEL_ROUTING_STATE.md",
                "--quota-latest", "/home/tokmo/.hermes/cron/state/model-quota-latest.json",
                "--timeout", "600", "--max-items", "2",
                "--lease-owner", "news-classifier-worker-cron",
            ], glm_transport=zai_chat_once)
        if code != 0:
            raise RuntimeError(f"NEWS classifier worker exit {code}")
        payload = captured.getvalue().strip() or "{}"
        json.loads(payload)
        atomic_write(RUNTIME / "latest.json", payload + "\n")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
