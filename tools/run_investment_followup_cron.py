#!/usr/bin/env python3
"""Optional finite task for the existing Hermes scheduler.

Separate invocation keeps NEWS's first warning independent of Quant latency.
Errors stay in health.json; handled degradation is not a cron process failure.
Health changes are printed once during 08:00-22:00. No new daemon.
"""

import argparse
import contextlib
import fcntl
import io
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path
from zoneinfo import ZoneInfo

from run_investment_followup import main as run

from tokmo.programs.followup_health import enrich_errors, issue_key, issue_text
from tokmo.programs.news_decision_config import delivery_window_open
from tokmo.programs.news_decision_engine import night
from tokmo.values.quant_decisions import instant

HEALTH_SETTLE = timedelta(minutes=10)


def notified_issues(notification):
    if notification.get("status") != "DEGRADED":
        return set()
    if isinstance(notification.get("issue_keys"), list):
        return set(notification["issue_keys"])
    try:
        _, issues = json.loads(notification["signature"])
        return {issue_key(json.loads(issue)) for issue in issues}
    except (KeyError, ValueError, TypeError):
        return set()


def health_notice(health, previous, config, now):
    """Settle transient health changes; never delay market/analysis alerts."""
    notification = previous.get("notification") or {}
    health["notification"] = notification
    monitor = dict(previous.get("monitor") or {})
    observed = dict(monitor.get("observed") or {})
    health["monitor"] = monitor
    grouped = {}
    for error in health["errors"]:
        key = issue_key(error)
        # A plan placeholder and its actual validation failure are one problem.
        if (
            key not in grouped
            or error.get("cause")
            or grouped[key].get("error") == "REVIEW_PENDING_RETRY"
        ):
            grouped[key] = error
        observed.setdefault(key, now.isoformat())
    monitor["observed"] = observed
    seen = notified_issues(notification)
    local = now.astimezone(ZoneInfo("Asia/Seoul"))
    if health["status"] == "OK":
        if notification.get("status") != "DEGRADED":
            monitor.clear()
            return None  # No recovery notice for an unannounced blip.
        since = monitor.setdefault("healthy_since", now.isoformat())
        if now - instant(since) < HEALTH_SETTLE:
            health["notice_suppressed"] = "confirming_recovery"
            return None
        text = "[퀸트 점검] 복구 확인\n" + local.strftime("%m-%d %H:%M")
        subjects = notification.get("subjects", [])
        if subjects:
            text += "\n대상: " + " · ".join(subjects[:3])
        text += (
            "\n보류·오류 0건 상태가 10분간 유지됐어. 기존 정기 점검을 계속하고 있어."
        )
        new_notification = {
            "status": "OK",
            "issue_keys": [],
            "subjects": [],
            "emitted_at": now.isoformat(),
            "incident_since": since,
        }
    else:
        monitor.pop("healthy_since", None)
        eligible = {
            k: e
            for k, e in grouped.items()
            if not (
                e.get("stage") == "price_checkpoint"
                and str(e.get("error", "")).startswith(("QUOTE_", "CALENDAR_"))
                or e.get("retry_at")
            )
            or now - instant(observed[k]) >= HEALTH_SETTLE
        }
        fresh = set(eligible) - seen
        if not fresh:
            return None
        first = min(instant(observed[k]) for k in eligible)
        text = "[퀸트 점검] 확인 필요\n" + local.strftime("%m-%d %H:%M")
        text += " · 최초 확인 " + first.astimezone(ZoneInfo("Asia/Seoul")).strftime(
            "%m-%d %H:%M"
        )
        subjects = []
        for key, error in list(eligible.items())[:3]:
            subject, reason, impact, retry = issue_text(error)
            subjects.append(subject)
            text += (
                "\n\n"
                + subject
                + "\n원인: "
                + reason
                + "\n영향: "
                + impact
                + "\n조치: "
                + retry
            )
            kind = {
                "SCHEDULED": "요청한 예약 분석",
                "NEWS_FOLLOWUP": "새 뉴스에 따른 후속 분석",
                "PERIODIC": "정기 후속 분석",
                "FOLLOWUP": "선택 후속 분석",
                "PRICE_CHECKPOINT": "가격 조건 분석",
            }.get(error.get("review_kind"))
            if kind:
                text += "\n점검 종류: " + kind
            if error.get("failed_at"):
                text += "\n분석 시작: " + instant(error["failed_at"]).astimezone(
                    ZoneInfo("Asia/Seoul")
                ).strftime("%m-%d %H:%M")
            if error.get("scheduled_at"):
                text += "\n예약 시각: " + instant(error["scheduled_at"]).astimezone(
                    ZoneInfo("Asia/Seoul")
                ).strftime("%m-%d %H:%M")
            elif error.get("previous_schedule_completed"):
                text += (
                    "\n이전 "
                    + instant(error["previous_schedule_completed"])
                    .astimezone(ZoneInfo("Asia/Seoul"))
                    .strftime("%H:%M")
                    + " 예약 분석은 이미 완료됐어."
                )
        if len(eligible) > 3:
            text += "\n\n그 외 " + str(len(eligible) - 3) + "건은 상태 기록에 남겼어."
        text += "\n\n같은 미해결 문제는 반복 통지하지 않아."
        new_notification = {
            "status": "DEGRADED",
            "issue_keys": sorted(seen | set(eligible)),
            "subjects": list(
                dict.fromkeys(notification.get("subjects", []) + subjects)
            ),
            "emitted_at": now.isoformat(),
            "incident_since": first.isoformat(),
        }
    if not delivery_window_open(config, now):
        health["notice_suppressed"] = "delivery_window_expired"
        return None
    if night(now):
        health["notice_suppressed"] = "quiet_hours"
        return None
    health["notification"] = new_notification
    if health["status"] == "OK":
        monitor.clear()
    return text


FALLBACK_ALERT = (
    Path(__file__).resolve().parents[1] / "ops/cron/fallback/send_system_alert.sh"
)


def send_health_notice(body):
    # Reuse the dedicated bot's existing credential loader; no second token copy.
    import importlib.util

    script = Path.home() / ".hermes/scripts/send_system_alert.py"
    if not script.is_file():
        # No Hermes on this machine: the same stand-in the cron wrappers use (TOKMO_SYSTEM_ALERT_SCRIPT or ops/cron/fallback).
        fallback = os.environ.get("TOKMO_SYSTEM_ALERT_SCRIPT") or str(FALLBACK_ALERT)
        result = subprocess.run(
            [fallback, body], capture_output=True, text=True, timeout=60
        )
        if result.returncode != 0:
            raise ValueError("system alert fallback failed")
        return "fallback"
    spec = importlib.util.spec_from_file_location("tokmo_system_alert", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    token, chat_id = module.load_config()
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message

    receipt = send_dialogue_message(body_text=body, bot_token=token, chat_id=chat_id)
    if receipt is None or not str(receipt).isdigit():
        raise ValueError("system alert receipt missing")
    return str(receipt)


def deliver_health(health, previous, notice, now, sender):
    """Acknowledge only confirmed sends; three bounded attempts, no bot fallback."""
    candidate = health["notification"]
    key = sha256(
        json.dumps(
            [
                candidate["status"],
                candidate.get("issue_keys"),
                candidate.get("incident_since"),
            ],
            sort_keys=True,
        ).encode()
    ).hexdigest()
    prior = previous.get("health_delivery") or {}
    state = dict(prior) if prior.get("key") == key else {"key": key, "attempts": 0}
    if state.get("status") == "SENT":
        return
    if (
        state["attempts"] >= 3
        or state.get("retry_at")
        and instant(state["retry_at"]) > now
    ):
        health["notification"] = previous.get("notification") or {}
        health["health_delivery"] = state
        if candidate["status"] == "OK":
            health["monitor"] = dict(previous.get("monitor") or {})
        return
    state.update(
        attempts=state["attempts"] + 1, at=now.isoformat(), destination="tokmoALERT_BOT"
    )
    try:
        state.update(receipt=sender(notice), status="SENT", retry_at=None)
    except Exception as error:
        state.update(
            status="FAILED",
            error=type(error).__name__,
            retry_at=(now + HEALTH_SETTLE).isoformat()
            if state["attempts"] < 3
            else None,
        )
        health["notification"] = previous.get("notification") or {}
        if candidate["status"] == "OK":
            health["monitor"] = dict(previous.get("monitor") or {})
    health["health_delivery"] = state


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--root", type=Path, default=Path(__file__).resolve().parents[1] / "var/data"
    )
    p.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    a = p.parse_args(argv)
    path = a.root / "investment_followup.json"
    if not path.is_file():
        return 0
    config = json.loads(path.read_text())
    if config.get("enabled") is not True:
        return 0
    folder = a.root / "ops/investment-followup"
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / "worker.lock").open("a+") as lock:
        try:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        supplements = config.get("supplements", [])
        latest = a.root / "ops/quant-inputs/latest.json"
        if latest.is_file() and str(latest) not in supplements:
            supplements = [str(latest), *supplements]
        args = [
            "tick",
            "--data-root",
            str(a.root),
            "--project-root",
            str(a.project_root),
            "--state",
            str(a.root / "private/investment-followup.sqlite3"),
            "--output",
            str(folder / "latest.json"),
            "--model",
            config.get("analysis_model", "glm-5.3"),
            "--screen-model",
            config.get("screen_model", "glm-5.3"),
            "--analysis-reasoning-effort",
            config.get("analysis_reasoning_effort", "medium"),
            "--max-events",
            str(config.get("max_events", 12)),
            "--max-reviews",
            str(config.get("max_reviews", 4)),
            "--retry-attempt",
            str(config.get("retry_attempt", "1")),
        ]
        for supplement in supplements:
            args.extend(["--supplement", str(supplement)])
        if config.get("first_pass_model"):
            args.extend(["--first-pass-model", str(config["first_pass_model"])])
        # Historical pension views require explicit comparison CLI arguments;
        # legacy configuration must not override the saved current inventory.
        if config.get("delivery_enabled") is True:
            args.append("--send")
        if "delivery_until" in config:
            args.extend(["--delivery-until", str(config["delivery_until"])])
        output = io.StringIO()
        try:
            with contextlib.redirect_stdout(output):
                code = run(args)
            result = json.loads(output.getvalue())
        except Exception as error:
            code = 2
            result = {"errors": [{"stage": "worker", "error": type(error).__name__}]}
        now = datetime.now(UTC)
        health = {
            "at": now.isoformat(),
            "status": "DEGRADED" if code or result.get("errors") else "OK",
            "worker_exit_code": code,
            "errors": enrich_errors(a.root, result.get("errors", [])),
            "reviews": len(result.get("reviewed", [])),
        }
        hpath = folder / "health.json"
        previous = json.loads(hpath.read_text()) if hpath.is_file() else {}
        notice = health_notice(health, previous, config, now)
        if notice and config.get("health_destination") == "SYSTEM_ALERT":
            deliver_health(health, previous, notice, now, send_health_notice)
            notice = None
        elif previous.get("health_delivery"):
            health["health_delivery"] = previous["health_delivery"]
        hpath.write_text(json.dumps(health, ensure_ascii=False, indent=2) + "\n")
        hpath.chmod(0o600)
        if notice:
            print(notice)
        # The scheduled invocation completed and persisted its real health.
        # Returning 2 here makes Hermes send its own failure notice every 5 min,
        # bypassing both deduplication and quiet hours. Unhandled process/storage
        # failures still propagate; the direct tick CLI retains its exit status.
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
