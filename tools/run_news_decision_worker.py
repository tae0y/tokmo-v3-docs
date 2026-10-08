#!/usr/bin/env python3
"""One finite NEWS pass. Deployment/scheduling is separate from this command."""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import sys
import time
import traceback

from tokmo.records.news_decision_inputs import read_news_observations, read_disclosure_observations, read_sec_observations, read_portfolio_context
from news_decision_models import build_models
from tokmo.programs.news_decision_config import read_decision_config, delivery_window_open
from tokmo.programs.news_decision_engine import KST, RECORDED_TIME, NewsDecisionEngine, night, morning
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries, queue_health_notice, delivery_issues, reconsider_failed_proposals
from tokmo.records.news_decision_store import NewsDecisionStore, kst, timestamp

# Engine stages whose failure is also recorded as a durable input ERROR or event review_error.
DURABLE_STAGES = frozenset({"screen", "screen_repair", "analyze", "recheck"})
# Summary contract read by the scheduled wrapper: version, status set and exit code per status.
SUMMARY = "news-decision-worker/1"
CODES = {"OK": 0, "DEGRADED": 2, "EXCEPTION": 1, "DISABLED": 0, "ALREADY_RUNNING": 0, "BEFORE_CUTOFF": 2,
         "RETRY_REJECTED": 2, "REQUEUED": 0}


class Notice(str):
    """Notice text that also carries the summary contract (version/status/code), so the
    scheduled wrapper can validate the payload before it touches the real stdout."""
    def __new__(cls, text, status):
        value = super().__new__(cls, text)
        value.contract = {"summary": SUMMARY, "status": status, "code": CODES[status]}
        return value


def stable_kind(text):
    """Identity form of an error text: ids, hashes and numbers collapse so a moving detail is not a new incident."""
    return re.sub(r"[0-9a-f]{12,}|\d+", "#", str(text or "unknown"))[:120]


def health_summary(store, report, ops=None):
    """Every unresolved issue this pass knows about, durable or tick-local; identity never uses counts or ids."""
    errors, failures = {}, {}
    for row in store.error_details():
        errors.setdefault(row['error'] or "unknown", []).append(row)
    for tick in report.get("ticks", []):
        for failure in tick.get("failures", []):
            failures.setdefault((str(failure.get("stage")), stable_kind(failure.get("reason"))), []).append(str(failure.get("reason")))
    events = store.events()
    # Read-only consistency: a last proposal that is neither queued (delivery_refs) nor still
    # pending has left the trail. Reported only; never resent or deleted here.
    unaccounted = [{"event_id": event["id"], "alert_id": event["last_alert"]["id"], "created_at": event["last_alert"].get("created_at"),
                    "last_delivery": event.get("last_delivery")} for event in events
                   if event.get("last_alert", {}).get("id") and event["last_alert"]["id"] not in event.get("delivery_refs", {})
                   and event["last_alert"]["id"] not in {item["id"] for item in event.get("pending_alerts", [])}]
    return {"input_errors": [{"error": error, "count": len(rows), "ids": [r['id'] for r in rows[:5]],
                             "first_observed_at": rows[0]['observed_at'], "last_observed_at": rows[-1]['observed_at']}
                            for error, rows in sorted(errors.items())],
            "review_errors": sorted({event["review_error"] for event in events if event.get("review_error")}),
            "review_error_events": [{"id": e['id'], "title": e.get('decision',{}).get('title','제목 미확인'),
                                      "error": e['review_error']} for e in events if e.get('review_error')],
            "unavailable_sources": sorted(key for key, row in report.get("sources", {}).items() if row["status"] == "UNAVAILABLE"),
            "tick_failures": [{"stage": stage, "kind": kind, "count": len(reasons), "detail": reasons[0]} for (stage, kind), reasons in sorted(failures.items())],
            "proposal_unaccounted": unaccounted,
            "model_waits": store.controls('model_wait:'),
            "retired_stale_rechecks": sum(len(tick.get('retired_stale_rechecks', [])) for tick in report.get("ticks", [])),
            "closed_stale_errors": (report.get("stale_error_cleanup") or {}).get("closed", 0),
            "deferred_inputs": store.counts().get('WAITING', 0),
            "delivery_unresolved": delivery_issues(store, ops)}


def health_notice(report, previous, now):
    """FIRST / kind CHANGE / RECOVERY only; elapsed time alone is not a new notice.

    State travels inside latest.json. Counts and raw details change the message text
    but not the identity. The caller enforces quiet hours before the stdout handoff.
    Only a completed OK pass can prove recovery; a DEGRADED or EXCEPTION pass never does."""
    health = report.get("health", {})
    items = ["input_error:" + stable_kind(row["error"]) for row in health.get("input_errors", [])]
    items += ["review_error:" + stable_kind(text) for text in health.get("review_errors", [])]
    items += ["source_unavailable:" + name for name in health.get("unavailable_sources", [])]
    # A tick failure in a stage that leaves a durable input/review error of the same kind is
    # the same incident; other stages (e.g. urgent_queue) stay their own identity.
    durable = {stable_kind(row["error"]) for row in health.get("input_errors", [])} | {stable_kind(text) for text in health.get("review_errors", [])}
    items += ["tick_failure:" + row["stage"] + ":" + row["kind"] for row in health.get("tick_failures", [])
              if not (row["stage"] in DURABLE_STAGES and row["kind"] in durable)]
    if report.get("status") == "EXCEPTION":
        items.append("exception:" + stable_kind(health.get("exception", "")))
    if health.get("proposal_unaccounted"):
        items.append("proposal_unaccounted")
    items += ['delivery_unresolved:' + status for row in health.get('delivery_unresolved', []) for status in row['statuses']]
    items += ['model_wait:'+r['model']+':'+r['category']+(':'+str(r['manual_required'])) for r in health.get('model_waits', [])]
    items += ['model_switch:'+r['stage']+':'+r['active'] for r in health.get('model_switches', [])]
    items = sorted(set(items))  # identity is the set of kinds; more raw errors of one kind is not a change
    previous = previous if isinstance(previous, dict) else {}
    before = sorted(set(previous.get("items") or []))
    reported = set(previous.get('reported_items', before))
    added = set(items) - reported
    now = timestamp(now)
    if items and not before:
        kind, since = "FIRST", previous.get("since") or kst(now)
    elif items and added:
        kind, since = "CHANGE", previous.get("since") or kst(now)
    elif items:
        since = previous.get("since") or kst(now)
        kind = "FIRST" if not previous.get("notified_at") else None
    elif before and (report.get("status") != "OK" or report.get("backlog")):
        # Unprocessed READY inputs (e.g. a requeued ERROR) cannot prove the incident is over.
        return dict(previous), None
    elif before:
        kind, since = "RECOVERY", previous.get("since")
    else:
        return {"items": [], "since": None, "notified_at": None, "next_due_at": None}, None
    notified = kst(now) if kind else previous.get("notified_at")
    state = {"items": items, "since": since if items else None, "notified_at": notified if items else None,
             "next_due_at": None, "last_kind": kind or previous.get("last_kind"),
             "reported_items": sorted(reported | set(items)) if items else []}
    if kind is None:
        return state, None
    local = lambda value: timestamp(value).astimezone(KST).strftime("%m-%d %H:%M")
    minutes = int((now - timestamp(since)).total_seconds() // 60) if since else 0
    lines = ["[뉴스 판단 점검] " + {"FIRST": "최초 알림", "CHANGE": "문제 종류 변경", "RECOVERY": "복구"}[kind]]
    if kind == "RECOVERY":
        lines.append(f"미해결 문제가 해소되었습니다. 지속 {minutes // 60}시간 {minutes % 60}분 ({local(since)} 이후)." if since else "미해결 문제가 해소되었습니다.")
        return state, "\n".join(lines)
    # A new symptom must not reprint every old backlog item as an ongoing outage.
    selected = set(items) if kind == 'FIRST' else added
    problems = [f"분석 보류 {row['count']}건: " + explain_error(row['error'])
                + (f" · 자료 수집 {local(row['first_observed_at'])}~{local(row['last_observed_at'])}" if row.get('first_observed_at') else '')
                for row in health.get('input_errors',[]) if 'input_error:'+stable_kind(row['error']) in selected]
    problems += ['후속 점검 실패: '+explain_error(text) for text in health.get('review_errors',[])
                 if 'review_error:'+stable_kind(text) in selected]
    problems += [f"{r['model']} 공통 대기 ({r['category']}) · " +
                 ('자동 회복 확인 한도 종료 또는 인증/쿼터 확인 필요. 입력은 보존 중이야.' if r['manual_required']
                  else f"{local(r['retry_at'])} 이후 다음 실제 요청으로 회복 확인 예정. 수집은 계속돼.")
                 for r in health.get('model_waits', []) if any(i.startswith('model_wait:'+r['model']+':') for i in selected)]
    problems += [f"{r['stage']} 모델 전환: {r['preferred']} 대신 {r['active']} 사용 중"
                 + ((f" ({r['category']} · 다음 요청에서 {r['preferred']} 복귀 확인)" if r.get('retry_due')
                     else f" ({r['category']} · {local(r['retry_at'])} 이후 {r['preferred']} 복귀 시도)") if r.get('retry_at') else '')
                 for r in health.get('model_switches', []) if 'model_switch:'+r['stage']+':'+r['active'] in selected]
    if health.get("unavailable_sources"):
        problems.append("읽지 못한 수집 자료: " + ", ".join(health["unavailable_sources"]))
    problems += [f"실행 단계 실패 {row['count']}건 ({row['stage']}: {row['detail'][:120]})" for row in health.get("tick_failures", [])]
    if health.get("proposal_unaccounted"):
        lost = health["proposal_unaccounted"]
        problems.append(f"발송 기록 없는 제안 {len(lost)}건 (사건 {lost[0]['event_id'][:20]} 제안 {lost[0]['alert_id'][-12:]} 생성 {lost[0]['created_at']} 상태 {lost[0]['last_delivery']}; 자동 재발송·삭제 없음)")
    if report.get("status") == "EXCEPTION":
        problems.append("실행 예외: " + str(health.get("exception", ""))[:200])
    if health.get('delivery_unresolved'):
        problems.append(f"발송 확인/복구 대기 {len(health['delivery_unresolved'])}건 (전달 기록 조사 후 개별 복구 가능)")
    lines.append('이번에 추가 확인한 문제:' if kind == 'CHANGE' else '확인한 문제:')
    lines.extend('- '+p for p in problems[:5])
    if len(problems)>5:lines.append(f'- 그 외 {len(problems)-5}종류는 상세 기록에 보관했어.')
    ticks=report.get('ticks',[])
    failures=[f for tick in ticks for f in tick.get('failures',[])]
    calls=sum(t.get('calls',0) for t in ticks)
    if ticks and not failures and not health.get('unavailable_sources') and report.get('status')!='EXCEPTION':
        lines.append(f"\n이번 실행: 새 처리 오류 없음 · 모델 호출 {calls}회 완료" if calls else '\n이번 실행: 새 처리 오류 없음 · 모델 호출 없음')
    elif failures:lines.append(f'\n이번 실행: 처리 실패 {len(failures)}건. 해당 입력·사건 분석이 보류됐어.')
    total=sum(r['count'] for r in health.get('input_errors',[]))
    reviews=len(health.get('review_error_events',[]))
    lines.append(f'남아 있는 미처리: 입력 {total}건 · 사건 재점검 {reviews}건')
    if health.get('retired_stale_rechecks'):
        lines.append(f"재점검 대기 목록에서 {health['retired_stale_rechecks']}건 자동 제외 (해당 사건에 새 기사가 연결되면 재분석됨)")
    if health.get('closed_stale_errors'):
        lines.append(f"묵은 입력 오류 자동 마감 {health['closed_stale_errors']}건 (관측 후 72시간 경과 · 재분석 없음 · 필요하면 --retry-errors)")
    if health.get('deferred_inputs'):
        lines.append(f"분류 완료 후 모델 대기: 입력 {health['deferred_inputs']}건 (개별 오류와 별도)")
    lines.append(f"미처리 내역 최초 확인 {local(since)} · 전체 분석이 그동안 중단됐다는 뜻은 아니야.")
    lines.append("같은 문제는 반복 통지하지 않습니다. 일반 점검 안내는 08시~22시에 전달합니다.")
    lines.append(("기존 개별 오류는 자동 재시도하지 않아. 공통 모델 혼잡만 최대 2회 회복을 확인해." if health.get('model_waits')
                  else "자동 재시도는 하지 않습니다.") + " 중요 뉴스가 없다는 뜻이 아닙니다. 상세: decision-worker/latest.json health")
    return state, "\n".join(lines)


def explain_error(text):
    if 'subscription inference failed:' in text:
        meanings={'QUOTA':'모델 사용 한도에 도달했어.', 'AUTH':'모델 로그인·인증을 확인해야 해.',
                  'REQUEST':'모델 요청 설정이나 입력 크기를 확인해야 해.', 'TRANSIENT':'모델 통신이 일시적으로 실패했어.',
                  'EXECUTOR_UNKNOWN':'모델 실행이 실패했어. 로컬 진단 기록으로 추가 확인이 필요해.'}
        for category,meaning in meanings.items():
            if 'subscription inference failed: '+category in text:return meaning
    if 'QUOTA_EXCEEDED' in text:return '당시 모델 사용 한도로 분석하지 못했어.'
    if 'bounded call budget' in text:return '당시 모델 호출 제한에 걸려 분석을 끝내지 못했어.'
    if 'ungrounded document amount' in text:return '공시 금액과 원문 근거를 연결하는 검증에서 보류됐어.'
    if any(word in text for word in ('invalid_json','explicit ignored reasons')):return '모델 응답 형식을 읽지 못했어.'
    if any(word in text for word in ('invalid ignored input','absent input','absent event','unresolved evidence')):return '모델이 적은 기사·공시 참조가 실제 입력과 맞지 않았어.'
    if 'subscription CLI failed' in text or 'subscription model returned' in text:return '모델 실행이 실패해 분석을 끝내지 못했어.'
    return str(text)[:160]


def run_once(root, config, *, now, screen, analyze, clock, ops=None, health_owner="outbox", document_reader=None, sec_document_reader=None, diagnostic_purpose="unknown", diagnostic_task_id=None, first_pass=None):
    """``now`` is the run snapshot (collection cutoff, due checks); ``clock`` is read before each
    model call, or RECORDED_TIME to pin every call to ``now`` (tests, replay).
    health_owner="scheduled" means the cron wrapper's stdout notice owns this incident,
    so the daily group outbox notice is not queued a second time; direct callers keep it."""
    root = Path(root)
    if not config.get("enabled"):
        return {"status": "DISABLED"}
    cutoff = timestamp(config["cutoff"])
    if now < cutoff:
        return {"status": "BEFORE_CUTOFF"}
    from tokmo.kis_composition import refresh_kis_etf_inputs
    etf_refresh = refresh_kis_etf_inputs(root=root, at=now)
    store = NewsDecisionStore(root / "stores" / "news_episodes.sqlite3")
    from tokmo.programs.news_portfolio_context import read_news_portfolio
    diagnostics = None
    if config.get('usage_diagnostics_enabled') is True:
        from tokmo.programs.news_usage_diagnostics import UsageDiagnostics
        diagnostics = UsageDiagnostics(root/'news'/'decision-worker'/'usage-diagnostics', now=now,
                                       purpose=diagnostic_purpose, task_id=diagnostic_task_id)
        diagnostics.safe('start', Path(__file__).resolve().parents[1], config, screen, analyze)
    # A chain holds work while every member waits and resumes it through ModelWait, so a chain
    # turns the wait machinery on even when the config does not (reported in latest.json).
    chained = any(hasattr(model, 'available') for model in (screen, analyze, first_pass))
    model_wait_enabled = config.get('model_wait_enabled') is True or chained
    engine = NewsDecisionEngine(store, screen, analyze, clock=clock, context_mode=config.get('context_mode','compact'),
        reuse_unchanged_reviews=config.get('reuse_unchanged_reviews') is True,
        scoped_review_reuse=config.get('scoped_review_reuse') is True,
        event_detail_enabled=config.get('event_detail_enabled') is True,
        typed_review_enabled=config.get('typed_review_enabled') is True,
        model_wait_enabled=model_wait_enabled,
        headline_digest_hour=config.get('headline_digest_hour'),
        context_reader=lambda at: read_news_portfolio(root,
            config.get('project_root') or Path(__file__).resolve().parents[1], at=at,
            followup=config.get('followup_context_enabled') is True,
            market_observations=config.get('incremental_market_observations') is True),
        token_budget=config.get("daily_token_budget"), document_reader=document_reader, sec_document_reader=sec_document_reader,
        diagnostics=diagnostics, first_pass=first_pass, confirm_enabled=config.get('confirm_enabled', True) is not False)
    report = {"status": "OK", "sources": {}, "ticks": [], "delivery": {"queued": 0}, "started_at": kst(now)}
    report["etf_refresh"] = etf_refresh
    report["model_wait_enabled"] = model_wait_enabled
    if chained and config.get('model_wait_enabled') is not True:
        report["model_wait_reason"] = "enabled for the configured model chain"
    started = time.monotonic()
    enabled = False
    try:
        if config.get("retry_errors_once"):
            raise ValueError("Use --retry-errors explicitly; do not configure recurring retries")
        for source, path, reader in (
            ("news", root / "news" / "external_news_v4.sqlite3", read_news_observations),
            ("disclosures", root / "opendart-bulk.sqlite3", read_disclosure_observations),
            ("sec", root / "sec-targeted.sqlite3", read_sec_observations)):
            initialized = source == "sec" and store.cursor(source, None) is None
            # Adding a source must not replay its historical filings as a burst of new alerts.
            # Persist its initial boundary even if the source is unavailable: recovery reads everything
            # after this boundary, instead of moving the first boundary forward on every failed run.
            if initialized:
                store.advance(source, kst(now))
            since = store.cursor(source, cutoff.isoformat())
            result = reader(path, since=since, until=now, limit=200)
            report["sources"][source] = {key: value for key, value in result.items() if key != "observations"}
            if initialized:
                report["sources"][source]["initial_cutoff"] = since
            # Commit inputs before cursor: a crash can re-read but cannot skip.
            store.ingest(result["observations"], now)
            if result["status"] != "UNAVAILABLE":
                store.advance(source, result["next_since"])
        # A failed collector revision can have an identical, already processed
        # source version. Retire that failure without replaying the old article.
        report['backlog_cleanup'] = store.close_duplicate_errors(now)
        report['stale_error_cleanup'] = store.close_stale_errors(now)
        if ops is not None:
            reconcile_deliveries(engine, ops)
        delivery_cutoff = cutoff
        if ops is not None and config.get("delivery_enabled"):
            from tokmo.programs.pulse_production_deps import read_news_gate
            from tokmo.programs.news_alert_delivery import live_delivery_authorized
            from tokmo.values.ops_kernel import ProducerRole
            delivery = read_news_gate(root).delivery
            enabled, reason = live_delivery_authorized(store.base, delivery, now)
            delivery_cutoff = max(cutoff, delivery.cutoff) if enabled else cutoff
            report["delivery"]["activation_reason"] = reason
        def publish(only=None):
            if enabled:
                result = queue_proposals(engine, ops, ProducerRole.HERMES, cutoff=delivery_cutoff, activation_enabled=True, only=only)
                report["delivery"]["queued"] += result["queued"]
        # An urgent decision enters the existing sender queue immediately, rather than waiting
        # behind unrelated analyses in this pass; only that proposal, so other events' ordinary
        # proposals keep their PENDING state until the pass's own publish.
        engine.on_urgent = lambda alert: publish(only=alert["id"])
        engine.on_morning = lambda alert: publish(only=alert["id"])
        max_batches = config.get("max_batches", 5)
        if type(max_batches) is not int or not 1 <= max_batches <= 100:
            raise ValueError("max_batches must be 1..100")
        for _ in range(max_batches):
            tick = engine.tick([], now)
            report["ticks"].append(tick)
            if not store.pending() or tick["failures"] or tick.get('model_deferred'):
                break
        publish()
        report["input_states"] = store.counts()
        report["pending_proposals"] = len(engine.pending_alerts())
        if engine.headline_digest_hour is not None:
            held = [event for event in store.events() if event.get('held_kind') == 'EVENING_HEADLINES' and event.get('held_until')]
            report['headline_digest'] = dict(hour=engine.headline_digest_hour, waiting_reassessment=len(held),
                next_due=min((event['held_until'] for event in held), default=None),
                review_errors=sum(bool(event.get('review_error')) for event in held))
        # One health view decides the status, so latest.json and the notice never disagree.
        report["health"] = health_summary(store, report, ops)
        if any(report["health"].values()):
            report["status"] = "DEGRADED"
        if report["input_states"].get("READY") or report["input_states"].get('WAITING'):
            report["backlog"] = report["input_states"].get("READY", 0) + report["input_states"].get('WAITING', 0)
        if enabled and report["status"] == "DEGRADED" and health_owner != "scheduled":
            queue_health_notice(ops, ProducerRole.HERMES, report, now)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        return report
    except Exception as exc:
        if diagnostics:
            diagnostics.safe('emit', 'run_failed', error_type=type(exc).__name__)
            report['status'] = 'EXCEPTION'
        raise
    finally:
        if diagnostics:
            diagnostics.safe('finish', report)
            report['usage_diagnostics'] = diagnostics.status()
        store.close()


def finish(payload):
    """Print the versioned summary line and return its exit code."""
    code = CODES[payload["status"]]
    print(json.dumps(dict(summary=SUMMARY, code=code, **payload), ensure_ascii=False))
    return code


def acquire(stack, path):
    handle = stack.enter_context(Path(path).open("a"))
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return False
    return True


def stdout_emit(text):
    print(text, flush=True)


def build_document_reader():
    """Lazy credential access, same private OpenDART configuration as the existing collection lane."""
    from tokmo.facts.opendart_document_live import OpenDartDocumentReader
    def credential():
        from tokmo.opendart_bulk_composition import _require_private_directory, _require_private_credential
        from tokmo.runtime.config import load_opendart_config
        folder = Path.home() / ".config" / "tokmo-v3"
        _require_private_directory(folder)
        path = folder / "opendart.env"
        _require_private_credential(path)
        return load_opendart_config(path).api_key
    return OpenDartDocumentReader(credential)


def build_sec_document_reader():
    from tokmo.facts.sec_document_live import SecDocumentReader
    def agent():
        from tokmo.opendart_bulk_composition import _require_private_directory, _require_private_credential
        from tokmo.runtime.config import load_sec_config
        folder = Path.home() / ".config" / "tokmo-v3"
        _require_private_directory(folder)
        path = folder / "sec.env"
        _require_private_credential(path)
        return load_sec_config(path).user_agent
    return SecDocumentReader(agent)


def main(argv=None, *, build=build_models, emit=stdout_emit):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--retry-errors", action="store_true", help="Explicit one-time retry, not a scheduler option")
    parser.add_argument("--input-id", action="append", dest="input_ids", help="With --retry-errors: retry only these ERROR inputs")
    parser.add_argument("--diagnostic-purpose", choices=['operation','recovery','replay','verification','unknown'], default=None)
    parser.add_argument("--diagnostic-task-id")
    parser.add_argument("--requeue-only", action="store_true", help="With --retry-errors --input-id: requeue and exit; no model, no pass, no latest.json")
    parser.add_argument('--recover-failed-alert-id', action='append', dest='recover_alert_ids', help='미전달 확인한 FAILED 제안을 새 가치 판단 대상으로 돌리고 종료; 발송/모델 호출 없음')
    parser.add_argument('--recovery-reason', help='발송 기록 조사 결과와 복구 이유')
    parser.add_argument('--resume-model', help='확인한 공통 모델 대기만 해제하고 종료. 모델/발송 호출 없음; --recovery-reason 필요')
    parser.add_argument('--confirmed-not-sent', action='store_true')
    parser.add_argument("--outer-lock", type=Path, help="Scheduler wrapper lock, taken before the worker lock")
    parser.add_argument("--now", help="Clock override for tests only")
    args = parser.parse_args(argv)
    if args.input_ids and not args.retry_errors:
        parser.error("--input-id requires --retry-errors")
    if args.requeue_only and not args.input_ids:
        parser.error("--requeue-only requires --retry-errors --input-id")
    if args.recover_alert_ids and (args.retry_errors or not args.recovery_reason or not args.confirmed_not_sent):
        parser.error('복구는 --recovery-reason --confirmed-not-sent를 지정하고 입력 재시도와 별도로 실행합니다')
    if args.resume_model and (args.retry_errors or args.recover_alert_ids or not args.recovery_reason):
        parser.error('--resume-model은 --recovery-reason과 함께 별도로 실행합니다')
    now = timestamp(args.now) if args.now else datetime.now(timezone.utc)
    # A test override pins every model call to --now; live runs read the wall clock per call.
    clock = RECORDED_TIME if args.now else (lambda: datetime.now(timezone.utc))
    config = read_decision_config(args.root)
    if not config.get("enabled"):
        return finish({"status": "DISABLED"})
    runtime = args.root / "news" / "decision-worker"
    runtime.mkdir(parents=True, exist_ok=True)
    with contextlib.ExitStack() as locks:
        # Existing lock order: scheduler wrapper (outer) first, then this worker (inner).
        for path in ([args.outer_lock] if args.outer_lock else []) + [runtime / "worker.lock"]:
            if not acquire(locks, path):
                return finish({"status": "ALREADY_RUNNING"})
        if args.resume_model:
            from tokmo.programs.news_model_wait import ModelWait
            store = NewsDecisionStore(args.root / 'stores/news_episodes.sqlite3')
            try:
                return finish(dict(status='REQUEUED', **ModelWait(store).reset(args.resume_model, now, args.recovery_reason)))
            except ValueError as exc:
                return finish(dict(status='RETRY_REJECTED', reason=str(exc)))
            finally:
                store.close()
        if args.recover_alert_ids:
            from tokmo.records.ops_kernel import OpsKernelStore
            store = NewsDecisionStore(args.root / 'stores/news_episodes.sqlite3')
            ops = None
            try:
                ops = OpsKernelStore(args.root / 'ops/ops_kernel.sqlite3', stable_phase_ids=frozenset())
                engine = NewsDecisionEngine(store, None, None, clock=RECORDED_TIME)
                result = reconsider_failed_proposals(engine, ops, args.recover_alert_ids, at=now,
                    reason=args.recovery_reason, confirmed_not_sent=args.confirmed_not_sent)
                return finish(dict(status='REQUEUED', **result))
            except ValueError as exc:
                return finish({'status': 'RETRY_REJECTED', 'reason': str(exc)})
            finally:
                store.close()
                if ops is not None:ops.close()
        if args.retry_errors:
            store = NewsDecisionStore(args.root / "stores" / "news_episodes.sqlite3")
            try:
                store.retry_errors(args.input_ids)
                # A targeted retry touches only the named inputs, never event rechecks.
                for event in [] if args.input_ids else store.events():
                    if event.get("review_error"):
                        event["review_error"] = None
                        store.save_event(event)
            except ValueError as exc:
                return finish({"status": "RETRY_REJECTED", "reason": str(exc)})
            finally:
                store.close()
            if args.requeue_only:
                # The next scheduled pass processes READY and reports recovery itself.
                return finish({"status": "REQUEUED", "input_ids": args.input_ids})
        try:
            previous = json.loads((runtime / "latest.json").read_text()).get("health_notice")
        except (OSError, ValueError, AttributeError):
            previous = None
        try:
            # models.json (the registry) names each stage's model or chain; without it the
            # legacy keys in news_decision.json apply unchanged.
            from tokmo.values.model_registry import check_known, known_models, load_registry, news_chains
            chains = news_chains(load_registry(args.root / 'models.json'), config)
            # Unknown ids fail here, before any model call (codex ids: this login's cached catalog).
            check_known(chains, known_models(Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex')))))
            chain_clock = (lambda: now) if args.now else (lambda: datetime.now(timezone.utc))
            # A QUOTA-blocked chain member waits for the provider's known reset (Hermes quota snapshot,
            # refreshed twice a day) plus 5 min; without the snapshot it probes every 6 hours.
            from tokmo.programs.quota_snapshot import quota_reset_reader
            quota_reset = quota_reset_reader(Path(os.environ.get('HERMES_HOME', str(Path.home() / '.hermes'))) / 'cron/state/model-quota-latest.json')
            screen, analyze = build(screen_model=chains['screen'], analysis_model=chains['confirm'],
                timeout=config.get("timeout_seconds", 90),
                analysis_reasoning_effort=config.get('analysis_reasoning_effort', 'medium'),
                diagnostic_root=runtime/'inference-diagnostics', chain_state=runtime/'model-chain.json', clock=chain_clock,
                quota_reset=quota_reset)
            first_pass = None
            if chains['first_pass']:
                from news_decision_models import build_first_pass_model
                first_pass = build_first_pass_model(chains['first_pass'], timeout=config.get("timeout_seconds", 90),
                                                    diagnostic_root=runtime/'inference-diagnostics',
                                                    chain_state=runtime/'model-chain.json', clock=chain_clock,
                                                    quota_reset=quota_reset)
            ops = None
            try:
                if config.get("delivery_enabled"):
                    from tokmo.records.ops_kernel import OpsKernelStore
                    ops = OpsKernelStore(args.root / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset())
                report = run_once(args.root, config, now=now, screen=screen, analyze=analyze, clock=clock, ops=ops, health_owner="scheduled", first_pass=first_pass,
                                  document_reader=None if args.now else build_document_reader(),
                                  sec_document_reader=None if args.now else build_sec_document_reader(),
                                  diagnostic_purpose=args.diagnostic_purpose or ('replay' if args.now else 'recovery' if args.retry_errors else 'operation'),
                                  diagnostic_task_id=args.diagnostic_task_id)
            finally:
                if ops is not None:
                    ops.close()
            chains_used = [m for m in (screen, first_pass, analyze) if hasattr(m, 'members')]
            if chains_used:
                report['model_chain'] = [c.summary() for c in chains_used]
                report['model_chain_notices'] = [line for c in chains_used for line in c.notices]
                if isinstance(report.get('health'), dict):
                    # A chain running on a fallback member is a health item until the preferred model
                    # actually answers again (a retry merely being due is not a recovery).
                    report['health']['model_switches'] = [dict(stage=s['stage'], preferred=s['preferred'], active=s['active'],
                        **(s['waiting'].get(s['preferred']) or {})) for s in report['model_chain'] if s['active'] != s['preferred']]
                    if report['health']['model_switches'] and report['status'] == 'OK':
                        report['status'] = 'DEGRADED'
        except Exception as exc:
            # Bounded like any other unresolved issue; the raw traceback stays
            # in stderr and latest.json rather than being masked.
            traceback.print_exc(file=sys.stderr)
            report = {"status": "EXCEPTION", "started_at": kst(now), "health": {"exception": type(exc).__name__ + ": " + str(exc)[:200]},
                      "traceback": traceback.format_exc()}
        if report["status"] in ("OK", "DEGRADED", "EXCEPTION"):
            state, notice = health_notice(report, previous, now)
            report["health_notice"] = state
            notify_at = now if args.now else datetime.now(timezone.utc)
            report['health_notice_policy'] = 'changes_only_08_22'
            suppressed = 'delivery_window_expired' if not delivery_window_open(config, notify_at) else 'quiet_hours' if night(notify_at) else None
            if suppressed:
                report['notice_suppressed'] = suppressed
            if notice and suppressed:
                # Retain the last handoff baseline. An overnight first/change/recovery
                # must still be considered at 08:00, using the then-current health.
                deferred = dict(previous or {"items": [], "notified_at": None})
                deferred['since'] = deferred.get('since') or state.get('since')
                deferred['next_due_at'] = kst(morning(notify_at)) if suppressed == 'quiet_hours' else None
                report['health_notice'] = deferred
                notice = None
            if notice:
                # notified_at is the stdout handoff time, not a platform receipt.
                # A failed local handoff keeps the previous due so the notice repeats.
                report["notice"] = notice
                try:
                    emit(Notice(notice, report["status"]))
                    report["notice_handoff"] = {"status": "OK", "at": kst(now)}
                except Exception as exc:
                    report["health_notice"] = previous or {"items": [], "since": None, "notified_at": None, "next_due_at": None}
                    report["notice_handoff"] = {"status": "FAILED", "error": type(exc).__name__ + ": " + str(exc)[:200]}
        elif previous:
            report["health_notice"] = previous
        temporary = runtime / "latest.tmp"
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2))
        temporary.replace(runtime / "latest.json")
        return finish({key: report[key] for key in ("status", "input_states", "pending_proposals", "elapsed_seconds", "notice", "notice_handoff") if key in report})


if __name__ == "__main__":
    raise SystemExit(main())
