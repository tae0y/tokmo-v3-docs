#!/usr/bin/env python3
"""Conversation/cron entry point. No orders, auth issuance, or bot polling."""

from __future__ import annotations

import argparse
import fcntl
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from quant_decision_models import QuantModel, canonical_model, model_profiles

from tokmo.programs.investment_followup import (
    deliver_pending,
    record_text,
    render_followup,
)
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.programs.quant_decision_inputs import load_local_snapshot
from tokmo.records.investment_followup import InvestmentFollowupStore
from tokmo.values.quant_decisions import instant, parse_snapshot


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "command",
        choices=[
            "record",
            "list",
            "tick",
            "pending",
            "deliver",
            "resolve-delivery",
            "register-checkpoint",
            "cancel-checkpoint",
            "register-plan",
            "cancel-plan",
            "sync-plans",
        ],
    )
    p.add_argument(
        "--state",
        required=True,
        type=Path,
        help="선택/모델 호출/후속 기록 DB. 시험은 임시 폴더 지정",
    )
    p.add_argument(
        "--data-root",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "var/data",
    )
    p.add_argument(
        "--project-root", type=Path, default=Path(__file__).resolve().parents[1]
    )
    p.add_argument("--snapshot", type=Path)
    p.add_argument("--supplement", action="append", default=[])
    p.add_argument(
        "--pension-baseline",
        type=Path,
        help="명시한 역산 보유 기준으로 분석. 운영 잔고는 변경하지 않음",
    )
    p.add_argument(
        "--pension-analysis",
        type=Path,
        help="명시적 과거 비교용 연금 분석 manifest. 현재 보유 분석과 구분",
    )
    p.add_argument(
        "--text-file", type=Path, help="현재 사용자의 실제 발언 원문 UTF-8 파일"
    )
    p.add_argument("--source-id", help="대화/메시지 고유 식별자")
    p.add_argument(
        "--checkpoint-file", type=Path, help="사용자 승인된 typed 체크포인트 JSON"
    )
    p.add_argument("--plan-file", type=Path, help="대화에서 요청한 계획/예약 JSON")
    p.add_argument("--choice-id", help="취소할 체크포인트 선택 id")
    p.add_argument("--reference", action="append", default=[])
    p.add_argument("--model", default="glm-5.3")
    p.add_argument("--screen-model", default="glm-5.3")
    p.add_argument(
        "--first-pass-model",
        default=None,
        help="뉴스/주기 리뷰를 먼저 판정할 저비용 모델. 지정하면 MATERIAL·REVIEW 등일 때만 --model이 재판정",
    )
    p.add_argument(
        "--analysis-reasoning-effort",
        default="medium",
        choices=("low", "medium", "high", "xhigh", "max"),
        help="Codex 최종 분석의 추론 강도",
    )
    p.add_argument("--model-env", type=Path, default=Path.home() / ".hermes/.env")
    p.add_argument("--at", help="시험 판단 시각. 명시하면 실발송 금지")
    p.add_argument("--max-reviews", type=int, default=4)
    p.add_argument("--max-events", type=int, default=12)
    p.add_argument(
        "--retry-attempt",
        default="1",
        help="실패 이유를 확인한 뒤에만 새 재시도 식별자 지정",
    )
    p.add_argument("--output", type=Path, help="비공개 결과 JSON 파일")
    p.add_argument(
        "--send", action="store_true", help="톡모그룹에 실제 발송. 운영 적용 때만 사용"
    )
    p.add_argument(
        "--delivery-until", help="이 시각부터 발송 중지. 시간대가 있는 ISO 시각"
    )
    p.add_argument("--review-id")
    p.add_argument("--resolution", choices=["sent", "requeue", "cancel"])
    p.add_argument("--reason", help="복구 판단의 확인 근거")
    p.add_argument(
        "--receipt", help="확인한 실제 Telegram message id 또는 그룹 receipt"
    )
    p.add_argument("--delivered-at", help="실제 전달 시각. --resolution sent에 필요")
    p.add_argument(
        "--confirmed-not-sent",
        action="store_true",
        help="전달 기록을 조사해 미전달을 확인한 경우만 지정",
    )
    a = p.parse_args(argv)
    if a.send and a.at:
        p.error("시험 시각으로 실발송할 수 없습니다")
    if a.send and a.snapshot:
        p.error("파일로 지정한 시험 스냅샷으로 실발송할 수 없습니다")
    if a.command == "deliver" and not a.send:
        p.error("deliver에는 명시적인 --send가 필요합니다")
    if a.send and a.command not in {"tick", "deliver"}:
        p.error("이 명령은 발송하지 않습니다")
    if a.command == "record" and (not a.text_file or not a.source_id):
        p.error("record에는 --text-file과 --source-id가 필요합니다")
    if a.command == "register-checkpoint" and (
        not a.checkpoint_file or not a.source_id
    ):
        p.error("--checkpoint-file과 --source-id가 필요합니다")
    if a.command == "cancel-checkpoint" and (not a.choice_id or not a.reason):
        p.error("--choice-id와 --reason이 필요합니다")
    if a.command == "register-plan" and (not a.plan_file or not a.source_id):
        p.error("--plan-file과 --source-id가 필요합니다")
    if a.command == "cancel-plan" and (not a.choice_id or not a.reason):
        p.error("--choice-id와 --reason이 필요합니다")
    if a.command == "resolve-delivery" and not (
        a.review_id and a.resolution and a.reason
    ):
        p.error("복구할 --review-id, --resolution, --reason이 필요합니다")
    at = instant(a.at) if a.at else datetime.now(UTC)
    a.state.parent.mkdir(parents=True, exist_ok=True)
    with a.state.with_suffix(a.state.suffix + ".lock").open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        return execute(a, at)


def execute(a, at):
    store = InvestmentFollowupStore(a.state)
    try:
        snapshot_error = None
        snapshot_loader = None
        if a.command in {"record", "tick"}:
            model = QuantModel(
                env_file=a.model_env,
                model=a.model,
                max_tokens=6000,
                reasoning_effort=a.analysis_reasoning_effort,
            )
            try:

                def read_snapshot():
                    raw = (
                        json.loads(a.snapshot.read_text())
                        if a.snapshot
                        else load_local_snapshot(
                            a.project_root,
                            a.data_root,
                            as_of=at if a.at else datetime.now(UTC),
                            supplement=a.supplement,
                            pension_baseline=a.pension_baseline,
                            pension_analysis=a.pension_analysis,
                            historical_pension=bool(
                                a.pension_baseline or a.pension_analysis
                            ),
                        )
                    )
                    return parse_snapshot(raw)

                snapshot = read_snapshot()
                # Explicit snapshots/historical comparisons are fixed inputs.
                # Live work rereads saved facts before each separate analysis.
                if not (a.snapshot or a.pension_baseline or a.pension_analysis):
                    snapshot_loader = read_snapshot
            except (ValueError, OSError, KeyError, TypeError) as error:
                if a.command != "tick":
                    raise
                snapshot = None
                snapshot_error = type(error).__name__
        if a.command == "record":
            accounts = {(p.account, p.scope) for p in snapshot.positions} | {
                (c.account, c.scope) for c in snapshot.cash
            }
            accounts |= {
                (c["interpretation"]["account"], c["interpretation"]["scope"])
                for c in store.choices(at=at)
            }
            catalog = {
                "accounts": [
                    {"account": account, "scope": scope}
                    for account, scope in sorted(accounts)
                ],
                "instruments": {i.key: i.name for i in snapshot.instruments},
            }
            result = record_text(
                store,
                text=a.text_file.read_text(),
                source_id=a.source_id,
                at=at,
                catalog=catalog,
                model=model,
                references=a.reference,
                attempt=a.retry_attempt,
            )
        elif a.command == "resolve-delivery":
            if a.resolution == "sent":
                from tokmo.programs.news_decision_outbox import _message_receipt

                if not _message_receipt(a.receipt):
                    raise ValueError("실제 Telegram 발송 확인값 형식이 필요합니다")
            result = store.resolve_delivery(
                a.review_id,
                action=a.resolution,
                reason=a.reason,
                at=at,
                receipt=a.receipt,
                delivered_at=a.delivered_at,
                confirmed_not_sent=a.confirmed_not_sent,
            )
        elif a.command == "register-checkpoint":
            from tokmo.programs.investment_price_checkpoint import register_checkpoint

            choice = register_checkpoint(
                store,
                json.loads(a.checkpoint_file.read_text()),
                source_id=a.source_id,
                at=at,
                references=a.reference,
            )
            result = {
                "choice": choice,
                "checkpoint": store.checkpoint_state(choice, at=at),
            }
        elif a.command == "cancel-checkpoint":
            result = {
                "checkpoint": store.cancel_checkpoint(
                    a.choice_id, at=at, reason=a.reason
                )
            }
        elif a.command == "register-plan":
            from tokmo.programs.investment_plans import register_plan

            supplied = json.loads(a.plan_file.read_text())
            selected = canonical_model(supplied["plan"]["model"])
            if selected not in model_profiles():
                raise ValueError("지원하는 모델을 지정하세요")
            supplied["plan"]["model"] = selected
            if (
                supplied["plan"]["reasoning_effort"]
                != model_profiles()[selected]["reasoning_effort"]
            ):
                raise ValueError("현재 모델 프로필의 추론 강도를 지정하세요")
            result = register_plan(
                store,
                supplied,
                source_id=a.source_id,
                at=at,
                project_root=a.project_root,
                references=a.reference,
            )
            if result["status"] == "PLAYBOOK_PENDING":
                result["errors"] = [{"stage": "playbook", "error": result["error"]}]
        elif a.command == "cancel-plan":
            from tokmo.programs.investment_plans import cancel_plan

            result = cancel_plan(
                store, a.choice_id, at=at, reason=a.reason, project_root=a.project_root
            )
            if result["status"] == "CANCELLED_PLAYBOOK_PENDING":
                result["errors"] = [{"stage": "playbook", "error": "SYNC_PENDING"}]
        elif a.command == "sync-plans":
            from tokmo.programs.investment_plans import sync_playbooks

            result = {"playbooks": sync_playbooks(store, a.project_root, at=at)}
        elif a.command == "list":
            result = {"choices": store.choices(at=at)}
            result["checkpoints"] = [
                store.checkpoint_state(c, at=at)
                for c in result["choices"]
                if "checkpoint" in c
            ]
            from tokmo.programs.investment_plans import plan_state

            result["plans"] = [
                {"choice": c, "state": plan_state(store.connection, c, at=at)}
                for c in store.choices(at=at, active=False)
                if c.get("plan")
            ]
        elif a.command == "pending":
            result = {
                "pending": [
                    {"id": r["id"], "body": render_followup(r)}
                    for r in store.pending(at=at)
                ],
                "delivery_issues": store.delivery_issues(),
            }
        elif a.command == "tick":
            if not a.at:
                at = datetime.now(UTC)

            def model_for_plan(plan):
                return QuantModel(
                    env_file=a.model_env,
                    model=plan["model"],
                    on_demand=True,
                    reasoning_effort=plan["reasoning_effort"],
                    max_calls=3,
                    receipt_dir=a.data_root / "ops/plan-model-calls",
                )

            result = run_tick(
                store,
                news_path=a.data_root / "stores/news_episodes.sqlite3",
                snapshot=snapshot,
                at=at,
                data_root=a.data_root,
                snapshot_error=snapshot_error,
                snapshot_loader=snapshot_loader,
                model=model,
                screen_model=QuantModel(
                    env_file=a.model_env, model=a.screen_model, max_tokens=3000
                ),
                max_events=a.max_events,
                max_reviews=a.max_reviews,
                attempt=a.retry_attempt,
                model_for_plan=model_for_plan,
                first_pass_model=QuantModel(
                    env_file=a.model_env, model=a.first_pass_model, max_tokens=6000
                )
                if a.first_pass_model
                else None,
            )
            if result.get("plans"):
                from tokmo.programs.investment_plans import sync_playbooks

                try:
                    sync_playbooks(store, a.project_root, at=at)
                except OSError:
                    result.setdefault("errors", []).append(
                        {"stage": "playbook", "error": "SYNC_PENDING"}
                    )
        else:
            result = {}
        if a.send:
            from tokmo.facts.telegram_dialogue_live import send_dialogue_message
            from tokmo.programs.news_delivery_route import (
                NewsRouteError,
                build_news_sender,
            )

            # See existing configured TOKMO_GROUP route. No separate credential.
            def no_fallback(*args, **kwargs):
                raise NewsRouteError("톡모그룹 전달 경로가 없습니다")

            routed_sender = build_news_sender(
                a.data_root, no_fallback, send_dialogue_message
            )
            from tokmo.programs.investment_followup import followup_telegram_message

            def sender(body):
                text, buttons = followup_telegram_message(body)
                return routed_sender(text, reply_markup=buttons)

            from tokmo.programs.news_decision_config import delivery_window_open

            window = {"delivery_until": a.delivery_until} if a.delivery_until else {}
            result["delivery"] = deliver_pending(
                store,
                at=lambda: datetime.now(UTC),
                sender=sender,
                can_send=lambda: delivery_window_open(window, datetime.now(UTC)),
            )
            if "checkpoints" in result:
                checked_at = datetime.now(UTC)
                result["checkpoints"] = [
                    store.checkpoint_state(
                        store.choice(row["choice_id"]), at=checked_at
                    )
                    for row in result["checkpoints"]
                    if row.get("status") != "CONFIG_INVALID"
                ]
        if a.command in {"tick", "deliver"}:
            # Keep uncertainty visible after restart, even when no new send was
            # attempted. These rows require receipt investigation, not retries.
            uncertain = store.uncertain_deliveries()
            if uncertain:
                result.setdefault("errors", []).append(
                    {
                        "stage": "delivery",
                        "error": "RECEIPT_UNCONFIRMED",
                        "deliveries": uncertain,
                    }
                )
            failed = [
                r for r in store.delivery_issues() if r["status"] in {"FAILED", "RETRY"}
            ]
            if failed:
                result.setdefault("errors", []).append(
                    {
                        "stage": "delivery",
                        "error": "DELIVERY_UNRESOLVED",
                        "deliveries": failed,
                    }
                )
        if a.output:
            a.output.parent.mkdir(parents=True, exist_ok=True)
            a.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
            a.output.chmod(0o600)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2 if result.get("errors") else 0
    except (ValueError, OSError, KeyError) as error:
        # Expected input failures have a machine-readable boundary, not a traceback.
        # Provider exception strings may contain credentials, so keep only the type.
        result = {
            "errors": [
                {
                    "stage": a.command,
                    "error": type(error).__name__,
                    "detail": "입력/연결 자료를 확인해 주세요",
                }
            ]
        }
        if a.output:
            a.output.parent.mkdir(parents=True, exist_ok=True)
            a.output.write_text(json.dumps(result, ensure_ascii=False) + "\n")
            a.output.chmod(0o600)
        print(json.dumps(result, ensure_ascii=False))
        return 2
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())
