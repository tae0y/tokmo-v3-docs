"""Tokmo notices and the human-operated, runner-owned today approval bridge.

The analysis worker calls only make_notifier. resolve_callback is called by
the existing Telegram receiver after it has authenticated the callback sender.
No model invocation or broker call exists in this module.
"""

import json
import re
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

from tokmo.programs.rotation_analysis import KST, policy_digest, window
from tokmo.records.durability import fsync_directory
from tokmo.records.quant_decision_reports import atomic_private
from tokmo.values.quant_decisions import instant
from tokmo.values.rotation_decision_file import parse_decision_file


class ApprovalError(ValueError):
    pass


def callback_token(document):
    # Binds the displayed proposal, not evidence documents or their contents.
    bound = {
        key: document.get(key)
        for key in (
            "analysis_id",
            "experiment_id",
            "account_scope",
            "trade_date",
            "revision",
            "policy_digest",
            "decision",
            "proposal_id",
            "valid_until",
        )
    }
    return sha256(
        json.dumps(
            bound, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()[:24]


def deadline_text(document):
    """HH:MM KST of the owner's approval deadline bound in the document."""
    try:
        return f"{instant(document['valid_until']).astimezone(KST):%H:%M}"
    except Exception:
        return "시각 미기록"


def failure_lines(document):
    """Closed, actionable messages; unknown is not a diagnosis of quota loss."""
    code = document.get("failure_code") or document.get("failure_reason")
    repair = (
        "운영 수리 후 재검증이 필요해. 쿼터 회복을 기다리는 것만으로 해결되지 않아."
    )
    inspect = "비공개 실행 기록을 확인해야 해. 쿼터 대기로 단정하지 않아."
    details = {
        "QUANT_PRODUCT_SCOPE_NOT_APPROVED": (
            "설치 승인 불일치로 AI 호출 전에 차단됐어. 쿼터 소진 오류가 아니야.",
            repair,
        ),
        "QUANT_ROUTE_OR_QUOTA_BLOCKED": (
            "모델 라우팅·쿼터 사전검사에서 차단됐어. 한도 소진으로 단정할 수 없어.",
            "허용 모델과 쿼터 조회 상태·잔여량을 확인해야 해.",
        ),
        "QUANT_CALL_BUDGET_EXHAUSTED": (
            "이번 분석의 호출 횟수 상한에 도달했어. 구독 쿼터와는 별개야.",
            "호출 이력 확인이 필요해. 같은 작업을 자동 재호출하지 않아.",
        ),
        "QUANT_CALLER_OR_MODEL_NOT_ADMITTED": (
            "실행 입구 또는 모델이 승인 범위와 맞지 않아.",
            repair,
        ),
        "QUANT_RECEIPT_UNAVAILABLE": (
            "모델 실행 기록을 안전하게 저장할 수 없어 호출을 차단했어.",
            repair,
        ),
        "QUANT_BOUNDS_NOT_ADMITTED": (
            "모델의 제한 시간·추론 설정이 승인 범위와 맞지 않아.",
            repair,
        ),
        "quant_failed": (
            "유효한 퀀트 분석 결과가 없어 오늘 판단을 완료하지 못했어.",
            inspect,
        ),
        "quant_validation_failed": (
            "AI 분석의 결과 검증을 통과하지 못했어.",
            "가격·수량·근거 참조를 점검하고 수리 후 재검증해야 해.",
        ),
        "quant_model_failed": (
            "모델 실행이 실패했어. 상세 원인은 추가 확인이 필요해.",
            inspect,
        ),
        "model_timeout": ("AI 분석이 제한 시간 안에 끝나지 않았어.", inspect),
        "analysis_timeout": (
            "10:40 분석 마감 시간이 지났어.",
            "오늘 작업은 종료됐고 자동 재개하지 않아.",
        ),
        "approval_timeout": (
            f"승인 마감 {deadline_text(document)}까지 오늘 제안 승인이 완료되지 않았어.",
            "오늘 작업은 종료됐고 자동 재개하지 않아.",
        ),
        "owner_rejected": ("오늘 제안이 거절됐어.", "오늘 회전을 종료해."),
    }
    if not isinstance(code, str):
        code = "unknown"
    reason, action = details.get(
        code, ("원인 미확정: 분석 처리 중 오류가 발생했어.", inspect)
    )
    lines = [
        "원인: " + reason,
        "영향: 오늘 회전 분석을 이용한 주문 인계는 보류돼.",
        "조치: " + action,
    ]
    if code in details:
        lines.append("진단 코드: " + code)
    return lines


def make_notifier(send):
    def notify(document):
        status = document["status"]
        decision = document.get("decision") or {}
        labels = {
            "SKIPPED": "휴장일 미실행",
            "COMPLETE": "분석 완료",
            "WAITING_APPROVAL": "오늘 제안 승인 대기",
            "FAILED": "분석 실패 · 오늘 거래 보류",
            "EXPIRED": "마감 · 오늘 거래 종료",
        }
        lines = [
            f"[톡모 · 현대차 회전 1주] {labels.get(status, status)}",
            document["trade_date"],
        ]
        action = {"ENTER": "매수", "EXIT": "매도", "HOLD": "보류"}.get(
            decision.get("action")
        )
        if action:
            lines.append("판단: " + action)
        for field, label in (("buy_price", "매수가"), ("sell_price", "매도가")):
            if decision.get(field):
                lines.append(f"{label}: {Decimal(decision[field]):,.0f}원")
        if status in {"FAILED", "EXPIRED"}:
            lines.extend(failure_lines(document))
        else:
            lines.append(document.get("analysis_summary", "")[:1400])
        if document.get("report_id"):
            lines.append("퀀트 보고서: " + document["report_id"])
        markup = None
        token = callback_token(document)
        if status == "WAITING_APPROVAL":
            lines.append(
                f"승인 마감 {deadline_text(document)}. 그 전에 승인하면 이 가격을 오늘만 허용해. "
                "마감 5분 전에 한 번 더 알릴게. 미승인·시간 초과면 오늘은 종료해."
            )
            markup = {
                "inline_keyboard": [
                    [
                        {"text": "오늘 제안 승인", "callback_data": "rot1:a:" + token},
                        {
                            "text": "거절 · 오늘 종료",
                            "callback_data": "rot1:r:" + token,
                        },
                    ]
                ]
            }
        chat_id, message_id = _group_receipt(
            send("\n".join(lines), reply_markup=markup)
        )
        return {"message_id": message_id, "chat_id": chat_id, "callback_token": token}

    return notify


def _group_receipt(receipt):
    match = re.fullmatch(r"telegram-group:(-[1-9][0-9]*):([1-9][0-9]*)", str(receipt))
    if not match:
        raise ValueError("tokmo_group_receipt_required")
    return match[1], match[2]


def make_warning(send):
    """Plain reminder 5 minutes before the approval deadline. No buttons: the
    original notice keeps the only approval controls, so the window is not
    restarted or duplicated."""

    def warn(document, closes):
        decision = document.get("decision") or {}
        action = {"ENTER": "매수", "EXIT": "매도"}.get(decision.get("action"), "제안")
        price = (
            decision.get("buy_price")
            if decision.get("action") == "ENTER"
            else decision.get("sell_price")
        )
        lines = [
            "[톡모 · 현대차 회전 1주] 승인 마감 5분 전",
            document["trade_date"],
            f"판단: {action} {Decimal(price):,.0f}원" if price else f"판단: {action}",
            f"{closes.astimezone(KST):%H:%M}까지 승인하지 않으면 오늘 회전은 종료돼. 승인은 위 제안 알림의 버튼으로 해.",
        ]
        chat_id, message_id = _group_receipt(send("\n".join(lines), reply_markup=None))
        return {"message_id": message_id, "chat_id": chat_id}

    return warn


def resolve_callback(
    *, store, proposal_root, policy, user_id, owner_id, chat_id, message_id, data, now
):
    if not owner_id or str(user_id) != str(owner_id):
        raise ApprovalError("unauthorized_owner")
    match = re.fullmatch(r"rot1:([ar]):([a-f0-9]{24})", str(data))
    day, start, _ = window(now)
    if not match or now < start or now >= policy.valid_until:
        raise ApprovalError("expired_or_invalid_callback")
    with store.lock(policy, day) as acquired:
        if not acquired:
            raise ApprovalError("analysis_busy_retry")
        doc = store.read(policy, day)
        delivery = store.delivery_receipt(policy, day) or {}
        receipt = delivery.get("receipt") or {}
        if not doc or delivery.get("status") != "SENT":
            raise ApprovalError("notice_not_found")
        # The approval window is the one the delivered notice displayed.
        if now >= instant(doc["valid_until"]):
            raise ApprovalError("expired_or_invalid_callback")
        if (str(chat_id), str(message_id), match[2]) != (
            receipt.get("chat_id"),
            receipt.get("message_id"),
            receipt.get("callback_token"),
        ):
            raise ApprovalError("callback_message_mismatch")
        if match[2] != callback_token(doc) or doc.get("policy_digest") != policy_digest(
            policy
        ):
            raise ApprovalError("proposal_changed")
        if doc["status"] != "WAITING_APPROVAL":
            return "ALREADY_RESOLVED"
        parsed = parse_decision_file(
            json.dumps(doc), policy=policy, now=now, trade_date=day
        )
        if parsed.decision is None or parsed.decision["action"] == "HOLD":
            raise ApprovalError("proposal_has_no_trade")
        proposal_id = f"{day:%Y%m%d}-today-{policy.experiment_id}"
        if doc.get("proposal_id") != proposal_id:
            raise ApprovalError("proposal_identity")
        root = Path(proposal_root).absolute()
        path = root / (proposal_id + ".json")
        if any(p.is_symlink() for p in (path, *path.parents)):
            raise ApprovalError("proposal_path_unsafe")
        expected = {
            key: doc["decision"][key]
            for key in ("action", "buy_price", "sell_price", "quantity")
        }
        record = (
            json.loads(path.read_text())
            if path.exists()
            else {
                "proposal_id": proposal_id,
                "scope": "today",
                "status": "PENDING",
                "experiment_id": policy.experiment_id,
                "trade_date": day.isoformat(),
                "analysis_id": doc["analysis_id"],
                "revision": doc["revision"],
                "created_at": now.isoformat(),
                "decision": expected,
                "band_proposal": doc.get("band_proposal"),
                "reason": doc["decision"]["reason"],
            }
        )
        binding = (
            record.get("scope"),
            record.get("experiment_id"),
            record.get("trade_date"),
            record.get("analysis_id"),
            record.get("revision"),
            record.get("decision"),
        )
        if binding != (
            "today",
            policy.experiment_id,
            day.isoformat(),
            doc["analysis_id"],
            doc["revision"],
            expected,
        ):
            raise ApprovalError("owner_proposal_mismatch")
        if record.get("policy_digest", doc["policy_digest"]) != doc["policy_digest"]:
            raise ApprovalError("owner_policy_mismatch")
        if record.get("status") != "PENDING":
            raise ApprovalError("owner_already_resolved")
        accepted = match[1] == "a"
        record.update(
            status="APPROVED" if accepted else "REJECTED",
            resolved_at=now.isoformat(),
            approved_by=str(user_id) if accepted else None,
            resolved_by=str(user_id),
            approved_at=now.isoformat() if accepted else None,
            policy_digest=doc["policy_digest"],
            account_scope=policy.account_scope,
            telegram_chat_id=str(chat_id),
            telegram_message_id=str(message_id),
        )
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The owner record is canonical. A crash after this write can be consumed
        # by the runner's existing WAITING_APPROVAL + today approval path.
        atomic_private(path, record)
        fsync_directory(root)
        doc.update(
            status="COMPLETE" if accepted else "FAILED", produced_at=now.isoformat()
        )
        if accepted:
            doc["analysis_summary"] = (
                "사용자가 오늘 제안 가격을 승인했어. " + doc["decision"]["reason"]
            )
        else:
            doc.update(
                failure_code="owner_rejected",
                failure_reason="owner_rejected",
                analysis_summary="사용자가 제안을 거절해 오늘 회전을 종료했어.",
            )
        store.write(policy, day, doc)
        return record["status"]
