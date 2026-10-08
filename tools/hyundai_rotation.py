"""Fixed Hyundai entry: preview by default, explicit user-run --execute.

The daily producer must atomically publish current-plan.json, including a
current ANALYZING marker when starting new work. No previous-file fallback.
Execution additionally needs fresh execution-context.json and the existing
shared credential supplier on stdin. This module does not create either.
"""
import argparse
from dataclasses import replace
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sys
from zoneinfo import ZoneInfo

from tokmo.composition import build_order_execution_client
from tokmo.facts.toss_order_commands import encode_order_command
from tokmo.values.order_commands import ConditionalOrder, Condition, OrderCommand, OrderOperation, RegularOrder
from tokmo.values.order_execution import RiskEnvelope, RiskSnapshot, authorize_order, aware

KST = ZoneInfo("Asia/Seoul")
DEADLINE = datetime(2027, 1, 1, tzinfo=KST)


def _json(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate_key")
            result[key] = value
        return result
    if len(raw.encode("utf-8")) > 65536:
        raise ValueError("document_size")
    return json.loads(raw, object_pairs_hook=unique)


def _read(path):
    with path.open(encoding="utf-8") as source:
        return source.read(65537)


def _time(value):
    at = datetime.fromisoformat(value)
    if not aware(at):
        raise ValueError("timezone_required")
    return at


def load_plan(raw, *, now):
    data = _json(raw)
    keys = {"schema", "plan_id", "status", "analysis_started_at", "completed_at", "valid_until", "command"}
    if type(data) is not dict or set(data) != keys or data["schema"] != "hyundai-rotation-plan-v1":
        raise ValueError("plan_schema")
    if not isinstance(data["plan_id"], str) or not 1 <= len(data["plan_id"]) <= 128:
        raise ValueError("plan_identity")
    if data["status"] not in {"READY", "HOLD"}:
        raise ValueError("analysis_not_complete")
    started, completed, until = (_time(data[key]) for key in ("analysis_started_at", "completed_at", "valid_until"))
    if not aware(now) or now >= DEADLINE or not started <= completed <= now < until <= DEADLINE:
        raise ValueError("plan_time")
    if not started.astimezone(KST).date() == completed.astimezone(KST).date() == now.astimezone(KST).date():
        raise ValueError("not_today")
    if data["status"] == "HOLD":
        if data["command"] is not None:
            raise ValueError("hold_has_order")
        return data, None, until
    item = dict(data["command"])
    operation = OrderOperation(item["operation"])
    payload = dict(item["payload"])
    if operation == OrderOperation.CREATE:
        payload = RegularOrder(**payload)
    elif operation == OrderOperation.CONDITIONAL_CREATE:
        payload["expire_date"] = date.fromisoformat(payload["expire_date"])
        payload["first"] = Condition(**payload["first"])
        if payload.get("second") is not None:
            payload["second"] = Condition(**payload["second"])
        payload = ConditionalOrder(**payload)
    else:
        raise ValueError("replacement_requires_target_review")
    item.update(operation=operation, payload=payload)
    command = OrderCommand(**item)
    if (command.market, command.symbol, payload.quantity, payload.order_type) != ("KR", "005380", "1", "LIMIT"):
        raise ValueError("hyundai_one_share_only")
    encode_order_command(command)
    return data, command, until


def _context(raw):
    data = _json(raw)
    if type(data) is not dict or set(data) != {"envelope", "snapshot"}:
        raise ValueError("execution_context")
    envelope, snapshot = dict(data["envelope"]), dict(data["snapshot"])
    envelope["valid_until"] = _time(envelope["valid_until"])
    snapshot["observed_at"] = _time(snapshot["observed_at"])
    for name in ("max_quantity", "initial_budget", "protected_quantity"):
        envelope[name] = Decimal(envelope[name])
    for name in ("total_quantity", "experiment_quantity", "pending_buy", "pending_sell", "available_cash", "fee_buffer", "funded_amount"):
        snapshot[name] = Decimal(snapshot[name])
    envelope, snapshot = RiskEnvelope(**envelope), RiskSnapshot(**snapshot)
    if (envelope.symbol, envelope.market, envelope.max_quantity) != ("005380", "KR", Decimal(1)) \
            or envelope.valid_until > DEADLINE:
        raise ValueError("experiment_scope")
    return envelope, snapshot


def main(argv=None, *, clock=None, client_factory=build_order_execution_client):
    parser = argparse.ArgumentParser(description="현대차 1주: 고정 파일로 당일 주문안 확인. 기본은 미리보기.")
    parser.add_argument("--state-dir", type=Path, default=Path(__file__).resolve().parents[1] / "var/state/hyundai-rotation")
    parser.add_argument("--execute", action="store_true", help="사용자가 직접 지정: 실계좌 주문 전송. 기존 공유 인증 JSON을 표준입력으로 전달")
    args = parser.parse_args(argv)
    clock = (lambda: datetime.now(timezone.utc)) if clock is None else clock
    state = args.state_dir.expanduser()
    try:
        plan, command, until = load_plan(_read(state / "current-plan.json"), now=clock())
        if command is None:
            print("오늘의 분석 결과는 관망이야. 주문 없음.")
            return 0
        method, path, body = encode_order_command(command)
        print(json.dumps({"mode": "EXECUTE" if args.execute else "PREVIEW", "symbol": "005380", "quantity": "1",
                          "method": method, "path": path, "request": json.loads(body), "valid_until": until.isoformat()},
                         ensure_ascii=False, indent=2))
        if not args.execute:
            return 0
        envelope, snapshot = _context(_read(state / "execution-context.json"))
        authorize_order(command, envelope, snapshot, now=clock(), mode="live")
        if sys.stdin.isatty():
            raise ValueError("shared_credentials_on_stdin_required")
        credentials = _json(sys.stdin.read(65537))
        if type(credentials) is not dict or set(credentials) != {"token", "account_ref", "account_scope"}:
            raise ValueError("credentials_schema")
        # Re-read after potentially blocking input. A new analysis must not
        # silently replace the exact order displayed above.
        latest, _, _ = load_plan(_read(state / "current-plan.json"), now=clock())
        if latest != plan:
            raise ValueError("plan_changed")
        envelope, snapshot = _context(_read(state / "execution-context.json"))
        if credentials["account_scope"] != envelope.account_scope:
            raise ValueError("credential_account_mismatch")
        permit = authorize_order(command, envelope, snapshot, now=clock(), mode="live")
        permit = replace(permit, valid_until=min(permit.valid_until, until))
        client = client_factory(account_scope=envelope.account_scope, journal_path=state / "orders.sqlite3",
            token_provider=lambda: credentials["token"], account_ref_provider=lambda scope: credentials["account_ref"],
            clock=clock, live_enabled=True)
    except (OSError, ValueError, TypeError, KeyError, ArithmeticError):
        print("주문을 전송하지 않았어. 당일 분석 완료·유효시간·최신 계좌 상태·실행 입력을 확인해 줘.", file=sys.stderr)
        return 2
    try:
        key = "hyundai-" + hashlib.sha256(plan["plan_id"].encode()).hexdigest()
        outcome = client.submit(command, request_key=key, permit=permit)
    except Exception:
        # An exception may follow a successful wire call (e.g. disk failure).
        print("주문 결과를 확정하지 못했어. 토스 주문 내역과 장부를 확인해 줘. 자동 재시도 없음.", file=sys.stderr)
        return 3
    print(json.dumps({"status": outcome.status, "code": outcome.code, "order_id": outcome.order_id}, ensure_ascii=False))
    return 0 if outcome.status == "ACCEPTED" else 3


if __name__ == "__main__":
    raise SystemExit(main())
