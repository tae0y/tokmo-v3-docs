"""Small conversation plans on the existing choice journal and worker.

No extra scheduler, model at intake, trade, or transport. Markdown is a
rebuildable view of the same source, not a second interpretation.
"""

from __future__ import annotations

import re
import tempfile
from operator import ge, gt, le, lt
from pathlib import Path

from tokmo.programs.investment_followup import review_choice, statement
from tokmo.records.investment_followup import digest, encode, plan_state, validated_plan
from tokmo.values.followup_failures import (
    failed_attempt,
    failure_notice,
    retry_attempt,
    retry_due,
)
from tokmo.values.quant_decisions import instant, number


def register_plan(store, supplied, *, source_id, at, project_root, references=()):
    if not isinstance(supplied, dict) or set(supplied) != {
        "raw_text",
        "interpretation",
        "plan",
    }:
        raise ValueError("사용자 원문, 선택 해석, plan이 필요합니다")
    text = statement(supplied["raw_text"], limit=16000)
    i = dict(supplied["interpretation"])
    plan = validated_plan(supplied["plan"])
    if i.get("scope") not in {"personal", "retirement"}:
        raise ValueError("계좌 범위를 확인해 주세요")
    if i.get("action") not in {"HOLD", "TRIM", "EXIT", "BUY", "ADD", "CASH", "WATCH"}:
        raise ValueError("행동을 확인해 주세요")
    if i.get("phase") not in {"INTENT", "DECIDED", "REPORTED_EXECUTED"}:
        raise ValueError("의향/결정/체결 진술을 구분해 주세요")
    if plan["kind"] == "ANALYSIS" and (i["action"], i["phase"]) != ("WATCH", "INTENT"):
        raise ValueError("분석 요청을 매매 채택이나 체결로 저장할 수 없습니다")
    for field in ("account", "summary"):
        statement(i.get(field))
    keys = i.get("keys")
    if (
        not isinstance(keys, list)
        or not keys
        or len(keys) != len(set(keys))
        or any(
            not isinstance(k, str)
            or not re.fullmatch(r"(KR|US):[A-Za-z0-9][A-Za-z0-9.-]{0,23}", k)
            or ".." in k
            for k in keys
        )
    ):
        raise ValueError("공식 식별자로 조회한 종목 목록이 필요합니다")
    for field in ("reason_quotes", "condition_quotes"):
        if (
            not isinstance(i.get(field), list)
            or field == "reason_quotes"
            and not i[field]
        ):
            raise ValueError("원문 근거가 필요합니다")
        for quote in i[field]:
            if statement(quote) not in text:
                raise ValueError("사용자 원문에 없는 인용입니다")
    if (
        not isinstance(i.get("suggested_checks"), list)
        or len(i["suggested_checks"]) > 5
    ):
        raise ValueError("점검 질문은 다섯 가지 이하입니다")
    for check in i["suggested_checks"]:
        statement(check)
    if plan["price"] and plan["price"]["key"] not in keys:
        raise ValueError("가격 감시 종목이 계획 대상에 없습니다")
    for request in plan.get("analysis_requests", []):
        if request["key"] not in keys or any(
            request[k] != i[k] for k in ("account", "scope")
        ):
            raise ValueError("분석 가정과 계획의 대상이 다릅니다")
        expected_currency = {"KR": "KRW", "US": "USD"}[request["key"].split(":")[0]]
        if request.get("currency", expected_currency) != expected_currency:
            raise ValueError("분석 가정 통화 오류")
        if (
            request["key"].startswith("KR:")
            and request.get("quantity") is not None
            and number(request["quantity"]) % 1
        ):
            raise ValueError("국내 종목 가정 수량은 정수입니다")
    choice = store.record_choice(
        raw_text=text,
        interpretation=i,
        source_id=source_id,
        at=at,
        references=references,
        plan=plan,
    )
    try:
        paths = sync_playbooks(store, project_root, at=at)
    except OSError as error:
        return {
            "choice": choice,
            "status": "PLAYBOOK_PENDING",
            "error": type(error).__name__,
        }
    state = plan_state(store.connection, choice, at=at)
    return {
        "choice": choice,
        "status": "CANCELLED" if state.get("cancelled_at") else "REGISTERED",
        "playbooks": paths,
        "plan_state": state,
    }


def sync_playbooks(store, project_root, *, at):
    """Rebuild only our marked section; preserve thesis, trades and other writers."""
    grouped = {}
    active = {c["id"] for c in store.choices(at=at)}
    for choice in store.choices(at=at, active=False):
        if not choice.get("plan"):
            continue
        i = choice["interpretation"]
        for key in i["keys"]:
            market, symbol = key.split(":")
            path = (
                Path(project_root)
                / "playbooks"
                / i["scope"]
                / market
                / (symbol + ".md")
            )
            grouped.setdefault(path, []).append(choice)
    paths = []
    for path, rows in grouped.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text() if path.exists() else "# " + path.stem + "\n"
        lines = [
            "<!-- USER_PLANS_START -->",
            "## 대화에서 등록한 계획",
            "선택 DB에서 표시한 기록이다. 예약 완료는 체결이나 보유 이유 추적 종료가 아니다.",
        ]
        for c in rows:
            state = plan_state(store.connection, c, at=at)
            status = (
                "취소"
                if state.get("cancelled_at")
                else "현재"
                if c["id"] in active
                else "이전 계획"
            )
            lines += [
                "",
                "### " + c["id"] + " · " + status,
                "기록 시각: " + c["recorded_at"],
                "요약: " + c["interpretation"]["summary"],
                "사용자 원문:",
                *["> " + line for line in c["raw_text"].splitlines()],
                "계획: " + encode(c["plan"]),
                "진행: " + encode(state),
            ]
        lines += ["<!-- USER_PLANS_END -->"]
        section = "\n".join(lines)
        pattern = r"(?m)^<!-- USER_PLANS_START -->.*?^<!-- USER_PLANS_END -->$"
        after = (
            re.sub(pattern, lambda _: section, before, flags=re.DOTALL)
            if re.search(pattern, before, re.DOTALL)
            else before.rstrip() + "\n\n" + section + "\n"
        )
        if after != before:
            with tempfile.NamedTemporaryFile(
                mode="w", dir=path.parent, encoding="utf-8", delete=False
            ) as f:
                temporary = Path(f.name)
                f.write(after)
            try:
                # Another process may maintain the same playbook. Never erase it.
                current = path.read_text() if path.exists() else "# " + path.stem + "\n"
                if current != before:
                    raise OSError("playbook changed during projection")
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
        paths.append(str(path))
    return paths


def cancel_plan(store, choice_id, *, at, reason, project_root):
    c = store.choice(choice_id)
    if not c or not c.get("plan"):
        raise ValueError("취소할 계획이 없습니다")
    statement(reason)
    with store.transaction():
        state = plan_state(store.connection, c, at=at)
        if not state.get("cancelled_at"):
            state.update(cancelled_at=instant(at).isoformat(), cancel_reason=reason)
            store.connection.execute(
                "INSERT INTO investment_watermarks VALUES(?,?) ON CONFLICT(source) DO UPDATE SET value=excluded.value",
                ("plan:" + choice_id, encode(state)),
            )
            store.connection.execute(
                "UPDATE investment_reviews SET delivery_state='CANCELLED' WHERE choice_id=? AND delivery_state IN ('PENDING','RETRY','FAILED')",
                (choice_id,),
            )
    try:
        sync_playbooks(store, project_root, at=at)
    except OSError:
        return {"choice_id": choice_id, "status": "CANCELLED_PLAYBOOK_PENDING"}
    return {"choice_id": choice_id, "status": "CANCELLED"}


def run_plan_reviews(
    store,
    choices,
    *,
    data_root,
    snapshot,
    at,
    model_for_plan,
    max_reviews,
    attempt,
    snapshot_loader=None,
):
    from tokmo.programs.investment_price_checkpoint import (
        checkpoint_snapshot,
        read_quote,
    )

    at = instant(at)
    result = {"plans": [], "reviewed": [], "errors": []}
    for c in choices:
        plan = c.get("plan")
        if not plan:
            continue
        state = plan_state(store.connection, c, at=at)
        done = set(state.get("done", []))
        triggers = []
        quote = None
        cp = plan["price"]
        if (
            plan["review_at"]
            and instant(plan["review_at"]) <= at
            and "TIME" not in done
        ):
            triggers.append("TIME")
        if cp and "PRICE" not in done:
            if instant(cp["starts_at"]) <= at < instant(cp["expires_at"]):
                quote_status, quote = read_quote(data_root, cp, at=at)
                state["price_status"] = quote_status
                if quote and {"<": lt, "<=": le, ">": gt, ">=": ge}[cp["direction"]](
                    number(quote["price"]), number(cp["threshold"])
                ):
                    triggers.append("PRICE")
            elif at >= instant(cp["expires_at"]):
                state["price_status"] = "EXPIRED"
        pending_work = state.get("work", {})
        observation = quote
        if pending_work.get("status") in {"RUNNING", "FAILED"}:
            # A fluctuating quote must not create endless new work keys/retries.
            triggers = pending_work["triggers"]
            observation = pending_work.get("observation")
        work_key = (
            pending_work["key"]
            if pending_work.get("status") in {"RUNNING", "FAILED"}
            else digest([c["id"], sorted(triggers), plan])
        )
        if triggers and len(result["reviewed"]) < max_reviews:
            input_key = "plan-" + work_key
            existing = store.review_by_input(input_key)
            failure = state.get("work", {})
            previous_failure = (
                failure.get("failure") if failure.get("attempt") == attempt else None
            )
            retry_ready = previous_failure and retry_due(
                previous_failure, at=at, attempt=attempt
            )
            if (
                not existing
                and failure.get("key") == work_key
                and failure.get("attempt") == attempt
                and not retry_ready
            ):
                result["errors"].append(
                    {
                        "stage": "plan",
                        "choice_id": c["id"],
                        "error": "REVIEW_PENDING_RETRY",
                    }
                )
            else:
                state["work"] = {
                    "key": work_key,
                    "attempt": attempt,
                    "status": "RUNNING",
                    "triggers": triggers,
                    "observation": observation,
                }
                store.set_watermark("plan:" + c["id"], state)
                try:
                    current = (
                        snapshot_loader()
                        if snapshot_loader and not existing
                        else snapshot
                    )
                    review_at = (
                        max(at, current.as_of) if snapshot_loader and current else at
                    )
                    evidence = []
                    if current:
                        evidence = [
                            dict(
                                id=e.id,
                                kind=e.kind,
                                text=e.text,
                                available_at=e.available_at.isoformat(),
                                source=e.source,
                            )
                            for item in current.instruments
                            if item.key in c["interpretation"]["keys"]
                            for e in item.evidence
                            if e.scope is None
                            or e.scope == c["interpretation"]["scope"]
                        ]
                    if observation:
                        evidence.append(
                            dict(
                                id="plan-price",
                                kind="price",
                                text=observation["price"]
                                + " "
                                + observation["currency"],
                                available_at=observation["observed_at"],
                                source=observation["source"],
                            )
                        )
                    review_snapshot = (
                        checkpoint_snapshot(current, cp, quote, at=review_at)
                        if cp
                        else current
                    )
                    # Include raw sources from recent matched events; a scheduled
                    # job must not use a naked price threshold as full analysis.
                    from datetime import timedelta

                    from tokmo.programs.investment_followup_worker import (
                        event_evidence,
                        event_version,
                        read_events,
                    )

                    events, news_status = read_events(
                        Path(data_root) / "stores/news_episodes.sqlite3",
                        at=review_at,
                        since=at - timedelta(days=2),
                    )
                    # Reuse choice-associated event ids found by the normal matcher.
                    known = {
                        r["event_id"]
                        for r in store.watermark("news_context:" + c["id"], [])
                    }
                    related = [e for e in events if e["id"] in known]
                    # First scheduled review has no match history yet. Include
                    # a finite recent market window too, regardless of tickers.
                    selected_events = list(
                        {
                            e["id"]: e
                            for e in [*reversed(related[-4:]), *reversed(events[-6:])]
                        }.values()
                    )
                    source_rows = event_evidence(selected_events, at=review_at)
                    # Avoid reintroducing a whole-news archive into each plan.
                    # Truncation and omitted coverage are explicit model inputs.
                    remaining_chars = 24000
                    news_rows = []
                    for row in source_rows[:12]:
                        text = row["text"][: min(2000, remaining_chars)]
                        news_rows.append(
                            dict(
                                row,
                                text=text,
                                text_truncated=len(text) < len(row["text"]),
                            )
                        )
                        remaining_chars -= len(text)
                    evidence += news_rows
                    evidence = list({e["id"]: e for e in evidence}.values())
                    all_sources = {
                        s["id"]
                        for e in selected_events
                        for s in e["evidence"]
                        if instant(s["observed_at"]) <= review_at
                    }
                    complete_events = {
                        e["id"]: event_version(e)
                        for e in selected_events
                        if all(
                            any(
                                n["id"] == s["id"] and not n["text_truncated"]
                                for n in news_rows
                            )
                            for s in e["evidence"]
                            if instant(s["observed_at"]) <= review_at
                        )
                    }
                    trigger = dict(
                        reason="USER_PLAN",
                        request_key=input_key,
                        triggers=triggers,
                        question=plan["question"],
                        scheduled_at=plan["review_at"],
                        observation=observation,
                        news_source_status=news_status,
                        news_coverage={
                            "events_selected": len(selected_events),
                            "events_available": len(events),
                            "sources_read": len(news_rows),
                            "sources_available": len(all_sources),
                            "truncated": any(r["text_truncated"] for r in news_rows),
                            "complete": len(selected_events) == len(events)
                            and len(news_rows) == len(all_sources)
                            and not any(r["text_truncated"] for r in news_rows),
                        },
                        # Only fully read events may skip the separate matcher review.
                        news_event_versions=complete_events,
                        data_note="사용자가 요청한 재분석이다. 변화가 없어도 결과를 한 번 전달한다. 예약 시각은 분석 시작 기준이다. 자료의 실제 시각과 결측을 밝힌다. 단일 가격은 시가/재회복/거래량/추세 확인이 아니다. 매매 실행이나 승인으로 해석하지 않는다.",
                    )
                    review = existing or review_choice(
                        store,
                        c,
                        evidence,
                        snapshot=review_snapshot,
                        model=model_for_plan(plan),
                        at=review_at,
                        trigger=trigger,
                        attempt=retry_attempt(attempt, previous_failure),
                    )
                    done.update(triggers)
                    state.update(
                        done=sorted(done),
                        last_review_id=review["id"],
                        work={"status": "COMPLETE"},
                    )
                    state.setdefault("completed_at", {}).update(
                        {t: at.isoformat() for t in triggers}
                    )
                    result["reviewed"].append(
                        {
                            "id": review["id"],
                            "choice_id": c["id"],
                            "notify": True,
                            "effect": review["assessment"]["effect"],
                            "reviewed_at": review["reviewed_at"],
                        }
                    )
                except Exception as error:
                    state["work"]["status"] = "FAILED"
                    from tokmo.programs.followup_health import error_info

                    attempt_failure = failed_attempt(
                        error, previous_failure, at=at, attempt=attempt
                    )
                    detail = dict(
                        error_info(error),
                        **failure_notice(attempt_failure),
                        review_kind="SCHEDULED"
                        if "TIME" in triggers
                        else "PRICE_CHECKPOINT",
                    )
                    state["work"].update(
                        detail, error=type(error).__name__, failure=attempt_failure
                    )
                    result["errors"].append(
                        dict(stage="plan", choice_id=c["id"], **detail)
                    )
        store.set_watermark("plan:" + c["id"], state)
        result["plans"].append({"choice_id": c["id"], **state})
    return result
