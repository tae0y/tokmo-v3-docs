#!/usr/bin/env python3
"""Unattended rehearsal of the rotation cycle against scratch state dirs.

One-off cron entries call ``auto`` through the day; the stage is chosen by
the Seoul clock and recorded so a rerun never repeats a stage. Every order
goes to a scratch journal, never the production one; the only production
write is ``live.flag`` at the end of the day, and only when every scratch
order is verifiably closed. Refuses to touch the production state dir for
anything else.

Stages (all Asia/Seoul):
  A  Mon 09:00-09:15  cancel the standing canary OTO, reset the scratch
                      journal, re-register it live (expires today)
  B1 Mon 09:20-09:30  model-inclusive dry run with production band values
  B2 Mon 09:30-09:45  second model dry run (fresh decision)
  C  Mon 10:20-11:30  plain limit BUY at the band floor, live, unfilled
  D  Mon 17:30-19:00  every scratch order closed? -> create live.flag
  E  Tue 09:00-09:30  dry settlement of every scratch (EXPIRED/CANCELED)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.composition import build_rotation_daily_wiring
from tokmo.values.order_commands import (
    CancelOrder,
    OrderCommand,
    OrderOperation,
    RegularOrder,
)
from tokmo.values.order_execution import (
    ExecutionPermit,
    authorize_order,
    command_digest,
)
from tokmo.values.rotation_policy import load_policies

KST = ZoneInfo("Asia/Seoul")
STATE_ROOT = (
    PROJECT / "var" / "state"
)  # relocated from ~/.hermes/state/tokmo-v3 2026-09-24
PRODUCTION = STATE_ROOT / "hyundai-rotation"
REHEARSAL = STATE_ROOT / "hyundai-rotation-rehearsal"
ROOT = Path(__file__).resolve().parents[1] / "var" / "data"
TOOL = PROJECT / "tools" / "run_hyundai_rotation_daily.py"
PYTHON = PROJECT / ".venv" / "bin" / "python"
SCRATCH = {
    "canary": STATE_ROOT / "hyundai-rotation-canary",
    "model": STATE_ROOT / "hyundai-rotation-canary-model",
    "buynow": STATE_ROOT / "hyundai-rotation-canary-buynow",
    "exit": STATE_ROOT / "hyundai-rotation-canary-exit",
}
MONDAY = "2026-09-14"
TUESDAY = "2026-09-15"
# Rehearsal-only tolerance: the provider's word for an unfilled DAY order after
# the close is learned on Tuesday (stage E); production settlement stays strict.
TERMINAL_REGULAR = {"FILLED", "CANCELED", "REJECTED", "REPLACED", "EXPIRED"}
TERMINAL_CONDITIONAL = {"COMPLETED", "EXPIRED", "CANCELED"}
STAGES = (  # name, day, start, end (KST minutes)
    ("A", MONDAY, (9, 0), (9, 15)),
    ("B1", MONDAY, (9, 20), (9, 30)),
    ("B2", MONDAY, (9, 30), (9, 45)),
    ("C", MONDAY, (10, 20), (11, 30)),
    ("D", MONDAY, (17, 30), (19, 0)),
    ("E", TUESDAY, (9, 0), (9, 30)),
)


def now_utc():
    return datetime.now(UTC)


def emit(payload):
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True
    )


def guard_scratch(state: Path):
    if state.resolve() == PRODUCTION.resolve():
        raise SystemExit("refusing to rehearse against the production state dir")
    if not state.is_dir():
        raise SystemExit(f"scratch state dir missing: {state}")


def run_tool(state: Path, *, trade_date: str, live=False, no_model=True):
    args = [
        str(PYTHON),
        str(TOOL),
        "--state-dir",
        str(state),
        "run",
        "--market",
        "KR",
        "--force-trade-date",
        trade_date,
    ]
    if live:
        args.append("--live")
    if no_model:
        args.append("--no-model")
    completed = subprocess.run(args, capture_output=True, text=True, timeout=120)
    lines = [line for line in completed.stdout.splitlines() if line.startswith("{")]
    outcome = json.loads(lines[-1]) if lines else {"status": "NO_OUTPUT"}
    outcome["_rc"] = completed.returncode
    if completed.returncode and completed.stderr.strip():
        outcome["_stderr"] = completed.stderr.strip()[-400:]
    return outcome


def wiring_for(state: Path, *, live: bool):
    (policy,) = load_policies((state / "experiments.json").read_text(encoding="utf-8"))
    wiring = build_rotation_daily_wiring(
        project_root=PROJECT,
        root=ROOT,
        account_scope=policy.account_scope,
        journal_path=state / "orders.sqlite3",
        clock=now_utc,
        live_enabled=live,
    )
    return policy, wiring


def open_rows(journal, policy):
    return journal.open_commands(
        mode="live", account_scope=policy.account_scope, symbol=policy.symbol
    )


def cancel_open_conditionals(state: Path):
    """Cancel the scratch journal's standing conditional groups; report each result."""
    guard_scratch(state)
    policy, wiring = wiring_for(state, live=True)
    results = []
    for row in open_rows(wiring.journal, policy):
        if (
            row["operation"] != "CONDITIONAL_CREATE"
            or row["status"] != "ACCEPTED"
            or not row["order_id"]
        ):
            continue
        command = OrderCommand(
            OrderOperation.CONDITIONAL_CANCEL,
            policy.market,
            policy.symbol,
            CancelOrder(),
            target_id=row["order_id"],
        )
        now = now_utc()
        permit = ExecutionPermit(
            policy.account_scope,
            command_digest(command),
            "live",
            now + timedelta(seconds=15),
            policy.plan_version,
            now,
        )
        result = wiring.client.submit(
            command, request_key="cancel-" + row["order_id"], permit=permit
        )
        detail = wiring.reads.conditional_detail(row["order_id"])
        results.append(
            {
                "order_id": row["order_id"],
                "cancel_status": result.status,
                "cancel_code": result.code,
                "group_status_after": getattr(detail, "status", None),
            }
        )
    return results


def cancel_open_regular(state: Path):
    """Cancel the scratch journal's still-open plain orders (frees reserved cash)."""
    guard_scratch(state)
    policy, wiring = wiring_for(state, live=True)
    results = []
    for row in open_rows(wiring.journal, policy):
        if (
            row["operation"] != "CREATE"
            or row["status"] != "ACCEPTED"
            or not row["order_id"]
        ):
            continue
        before = wiring.reads.order_detail(row["order_id"])
        if getattr(before, "status", None) in TERMINAL_REGULAR:
            results.append(
                {
                    "order_id": row["order_id"],
                    "cancel_status": "not_needed",
                    "order_status_before": before.status,
                }
            )
            continue
        command = OrderCommand(
            OrderOperation.CANCEL,
            policy.market,
            policy.symbol,
            CancelOrder(),
            target_id=row["order_id"],
        )
        now = now_utc()
        permit = ExecutionPermit(
            policy.account_scope,
            command_digest(command),
            "live",
            now + timedelta(seconds=15),
            policy.plan_version,
            now,
        )
        result = wiring.client.submit(
            command, request_key="cancel-" + row["order_id"][:40], permit=permit
        )
        after = wiring.reads.order_detail(row["order_id"])
        results.append(
            {
                "order_id": row["order_id"],
                "order_status_before": getattr(before, "status", None),
                "cancel_status": result.status,
                "cancel_code": result.code,
                "order_status_after": getattr(after, "status", None),
            }
        )
    return results


def reset_journal(state: Path):
    guard_scratch(state)
    (state / "orders.sqlite3").unlink(missing_ok=True)


def regular_buy(state: Path, *, price: str):
    """Plain DAY limit buy through the real client and risk gate (no model)."""
    guard_scratch(state)
    policy, wiring = wiring_for(state, live=True)
    journal = wiring.journal
    journal.initialize_experiment(
        policy.experiment_id, mode="live", envelope=policy.envelope()
    )
    total = wiring.reads.holdings_quantity(policy.symbol)
    if total is None:
        return {"status": "HALT", "reason": "holdings_unavailable"}
    snapshot = journal.experiment_snapshot(
        policy.experiment_id,
        mode="live",
        total_quantity=total,
        pending_buy=Decimal(0),
        pending_sell=Decimal(0),
        fee_buffer=policy.fee_buffer,
        observed_at=now_utc(),
    )
    day = now_utc().astimezone(KST).strftime("%Y%m%d")
    command = OrderCommand(
        OrderOperation.CREATE,
        policy.market,
        policy.symbol,
        RegularOrder(
            "BUY",
            "LIMIT",
            quantity=str(policy.quantity),
            price=price,
            client_order_id="reh-buy-" + day,
        ),
    )
    permit = authorize_order(
        command, policy.envelope(), snapshot, now=now_utc(), mode="live"
    )
    result = wiring.client.submit(
        command, request_key="rehearsal-buy-" + day, permit=permit
    )
    detail = wiring.reads.order_detail(result.order_id) if result.order_id else None
    return {
        "status": result.status,
        "code": result.code,
        "order_id": result.order_id,
        "order_status_after": getattr(detail, "status", None),
        "price": price,
    }


def scratch_orders_closed():
    """True only when every open scratch reservation is verifiably terminal at the broker."""
    report, clean = [], True
    for name, state in SCRATCH.items():
        if not (state / "orders.sqlite3").exists():
            continue
        policy, wiring = wiring_for(state, live=False)
        for row in open_rows(wiring.journal, policy):
            if row["status"] != "ACCEPTED" or not row["order_id"]:
                clean = False
                report.append(
                    {
                        "scratch": name,
                        "request_key": row["request_key"],
                        "status": row["status"],
                        "closed": False,
                    }
                )
                continue
            if row["operation"].startswith("CONDITIONAL"):
                if row["operation"] == "CONDITIONAL_CANCEL":
                    continue
                detail = wiring.reads.conditional_detail(row["order_id"])
                status, closed = (
                    getattr(detail, "status", None),
                    getattr(detail, "status", None) in TERMINAL_CONDITIONAL,
                )
            else:
                detail = wiring.reads.order_detail(row["order_id"])
                status, closed = (
                    getattr(detail, "status", None),
                    getattr(detail, "status", None) in TERMINAL_REGULAR,
                )
            clean = clean and closed
            report.append(
                {
                    "scratch": name,
                    "order_id": row["order_id"],
                    "status": status,
                    "closed": closed,
                }
            )
    return clean, report


def stage_a():
    canary = SCRATCH["canary"]
    cancelled = cancel_open_conditionals(canary)
    reset_journal(canary)
    registered = run_tool(canary, trade_date=MONDAY, live=True, no_model=True)
    return {"cancelled": cancelled, "re_registered": registered}


def stage_b():
    model = SCRATCH["model"]
    reset_journal(model)
    return {
        "model_dry_run": run_tool(model, trade_date=MONDAY, live=False, no_model=False)
    }


def stage_c():
    buynow = SCRATCH["buynow"]
    reset_journal(buynow)
    (policy,) = load_policies((buynow / "experiments.json").read_text(encoding="utf-8"))
    return {"regular_buy": regular_buy(buynow, price=str(policy.buy_floor))}


def stage_d(*, go_live: bool):
    # Expiry is processed by the provider on its own schedule; cancel whatever
    # scratch group is still watching so the day ends verifiably clean (and
    # Tuesday's settlement exercises the CANCELED path).
    cancelled = {
        name: cancel_open_conditionals(state)
        for name, state in SCRATCH.items()
        if (state / "orders.sqlite3").exists()
    }
    clean, report = scratch_orders_closed()
    created = False
    if clean and go_live:
        flag = PRODUCTION / "live.flag"
        if not flag.exists():
            fd = os.open(flag, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            os.close(fd)
        created = True
    return {
        "all_scratch_orders_closed": clean,
        "orders": report,
        "live_flag_created": created,
        "cancelled": {k: v for k, v in cancelled.items() if v},
    }


def stage_e():
    # First learn what the provider calls yesterday's unfilled DAY order; if it
    # is somehow still open, cancel it so the reserved cash is back before the
    # 10:12 production run.
    regular = (
        cancel_open_regular(SCRATCH["buynow"])
        if (SCRATCH["buynow"] / "orders.sqlite3").exists()
        else []
    )
    settled = {
        name: run_tool(state, trade_date=TUESDAY, live=False, no_model=True)
        for name, state in SCRATCH.items()
        if (state / "orders.sqlite3").exists()
    }
    return {"regular_before_settlement": regular, **settled}


def summarize(stage, result):
    def ok(outcome):
        return (
            outcome.get("status") in {"SUBMITTED", "SKIP", "HOLD", "EXISTING"}
            and outcome.get("_rc", 0) == 0
        )

    lines = [f"[회전매매 리허설 {stage}]"]
    if stage == "A":
        for c in result["cancelled"]:
            lines.append(
                f"카나리 취소 {c['order_id']}: {c['cancel_status']}/{c['cancel_code']} → 그룹 {c['group_status_after']}"
            )
        r = result["re_registered"]
        lines.append(
            f"재등록: {r.get('status')}/{r.get('execution_status')} ({r.get('reason')})"
        )
    elif stage in ("B1", "B2"):
        r = result["model_dry_run"]
        d = r.get("decision") or {}
        lines.append(
            f"모델 드라이런: {r.get('status')}/{r.get('execution_code')} — {d.get('action')} 매수 {d.get('buy_price')} 매도 {d.get('sell_price')}"
        )
        lines.append(f"판단: {d.get('reason')}")
        lines.append(f"노트: {r.get('notes')}")
    elif stage == "C":
        r = result["regular_buy"]
        lines.append(
            f"일반 지정가 매수 {r.get('price')}: {r.get('status')}/{r.get('code')} → 주문상태 {r.get('order_status_after')}"
        )
    elif stage == "D":
        for name, items in result.get("cancelled", {}).items():
            for c in items:
                lines.append(
                    f"{name} 취소 {c['order_id']}: {c['cancel_status']} → {c['group_status_after']}"
                )
        lines.append(
            "스크래치 주문 전부 종료: "
            + ("예" if result["all_scratch_orders_closed"] else "아니오")
        )
        for o in result["orders"]:
            lines.append(
                f"- {o.get('scratch')} {o.get('order_id') or o.get('request_key')}: {o.get('status')}"
            )
        if result["live_flag_created"]:
            lines.append("live.flag 생성 → 화요일 10:12 실전")
        elif (PRODUCTION / "live.flag").exists():
            lines.append(
                "live.flag 이미 있음 → 화요일 10:12 실전 (미종료 주문은 화 09:05 E단계가 정리)"
            )
        else:
            lines.append("live.flag 미생성 (사람 확인 필요)")
    elif stage == "E":
        for c in result.get("regular_before_settlement", []):
            lines.append(
                f"어제 일반매수 상태: {c.get('order_status_before')} → 취소 {c.get('cancel_status')} → {c.get('order_status_after')}"
            )
        for name, r in result.items():
            if name == "regular_before_settlement":
                continue
            lines.append(
                f"{name}: {r.get('status')}/{r.get('reason')} 노트 {r.get('notes')}"
            )
    return "\n".join(lines)


def notify(text):
    token, chat_id = (
        os.environ.get("TELEGRAM_STOCK_BOT_TOKEN"),
        os.environ.get("TELEGRAM_STOCK_BOT_CHAT_ID"),
    )
    if not token or not chat_id:
        return False
    from tokmo.facts.telegram_reminder_live import send_pension_reminder

    try:
        send_pension_reminder(body_text=text, bot_token=token, chat_id=chat_id)
        return True
    except Exception:
        return False


def load_stages():
    path = REHEARSAL / "stages.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def save_stages(done):
    REHEARSAL.mkdir(mode=0o700, exist_ok=True)
    path = REHEARSAL / "stages.json"
    path.write_text(
        json.dumps(done, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )
    os.chmod(path, 0o600)


def pick_stage(now_kst, done):
    day, minutes = now_kst.strftime("%Y-%m-%d"), now_kst.hour * 60 + now_kst.minute
    for name, stage_day, start, end in STAGES:
        if name in done or stage_day != day:
            continue
        if start[0] * 60 + start[1] <= minutes < end[0] * 60 + end[1]:
            return name
    return None


RUNNERS = {"A": stage_a, "B1": stage_b, "B2": stage_b, "C": stage_c, "E": stage_e}


def main(argv=None):
    parser = argparse.ArgumentParser(description="회전매매 무인 리허설")
    sub = parser.add_subparsers(dest="command", required=True)
    auto = sub.add_parser("auto", help="시각으로 단계를 골라 1개 실행")
    auto.add_argument(
        "--go-live-if-clean",
        action="store_true",
        help="D단계에서 스크래치 주문이 모두 종료됐으면 live.flag 생성",
    )
    auto.add_argument("--notify", action="store_true")
    one = sub.add_parser("stage", help="특정 단계 강제 실행 (기록 안 함)")
    one.add_argument("name", choices=[s[0] for s in STAGES])
    one.add_argument("--go-live-if-clean", action="store_true")
    sub.add_parser("status")
    args = parser.parse_args(argv)
    if args.command == "status":
        emit({"done": load_stages(), "closed": scratch_orders_closed()})
        return 0
    if args.command == "stage":
        stage = args.name
    else:
        done = load_stages()
        stage = pick_stage(now_utc().astimezone(KST), done)
        if stage is None:
            emit(
                {
                    "stage": None,
                    "reason": "no_pending_stage_in_window",
                    "done": sorted(done),
                }
            )
            return 0
    try:
        result = (
            stage_d(go_live=args.go_live_if_clean) if stage == "D" else RUNNERS[stage]()
        )
        failed = False
    except Exception as error:
        result, failed = {"error": type(error).__name__ + ": " + str(error)[:300]}, True
    record = {
        "stage": stage,
        "at": now_utc().isoformat(),
        "failed": failed,
        "result": result,
    }
    if args.command == "auto":
        done = load_stages()
        done[stage] = record
        save_stages(done)
    text = (
        summarize(stage, result)
        if not failed
        else f"[회전매매 리허설 {stage}] 실패: {result['error']}"
    )
    notified = notify(text) if getattr(args, "notify", False) else False
    emit({**record, "notified": notified, "text": text})
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
