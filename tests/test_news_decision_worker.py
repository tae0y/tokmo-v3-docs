from datetime import timedelta
import fcntl
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

from tokmo.programs.news_decision_engine import RECORDED_TIME
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
from tokmo.programs.news_group_runner import GroupSourceResult, run_news_group
from tokmo.programs.news_poll_manifest import NewsPollManifest
from tokmo.programs.news_poll_policy import NewsGroup
from test_news_decision_engine import Engine, NOW, decision, observation, screen
from tokmo.records.ops_kernel import OpsKernelStore

TOOLS = Path(__file__).resolve().parents[1] / "tools"
sys.path.insert(0, str(TOOLS))
spec = importlib.util.spec_from_file_location("decision_worker", TOOLS / "run_news_decision_worker.py")
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)


@pytest.mark.parametrize('failure', ['invalid', 'deferred'])
def test_morning_group_is_queued_before_unrelated_backlog_and_survives_interruption(tmp_path, monkeypatch, failure):
    from tokmo.programs.news_model_wait import ModelDeferred
    from tokmo.programs.news_source_buttons import MARKER
    release = timestamp('2026-09-02T08:00:00+09:00')
    store = NewsDecisionStore(tmp_path / 'stores/news_episodes.sqlite3')
    split = lambda req: dict(candidates=[dict(evidence_ids=[r['id']]) for r in req['evidence']], ignored=[])
    output = lambda ids: decision(ids, importance='MEDIUM', urgency='NORMAL', confirmation='OFFICIAL')
    seed = Engine(store, split, lambda req: output([r['id'] for r in req['evidence']]))
    seeded = seed.tick([observation(k, kind='DISCLOSURE') for k in ('a', 'b', 'c')], NOW)
    assert not seeded['failures'], seeded
    held = sorted(store.events(), key=lambda e: e['id'])
    failed_id = held[-1]['id']
    # Ordinary due work and a new batch must follow the morning group.
    for index in range(12):
        store.save_event(dict(held[0], id='backlog-' + str(index), held_until=None, next_check_at=release.isoformat()))
    store.ingest([observation('new', release)], release)
    store.close()
    monkeypatch.setattr('tokmo.kis_composition.refresh_kis_etf_inputs', lambda **kw: {})
    monkeypatch.setattr('tokmo.programs.news_portfolio_context.read_news_portfolio', lambda *a, **kw: {})
    monkeypatch.setattr('tokmo.programs.pulse_production_deps.read_news_gate',
                        lambda root: SimpleNamespace(delivery=SimpleNamespace(cutoff=NOW)))
    monkeypatch.setattr('tokmo.programs.news_alert_delivery.live_delivery_authorized', lambda *a: (True, 'fixture'))
    ops = OpsKernelStore(tmp_path / 'ops.db', stable_phase_ids=frozenset())
    seen, queued_ids = [], []
    def analyze(req):
        previous = req['events'][0]
        seen.append(previous['id'])
        if previous['id'] == failed_id:
            if failure == 'deferred':
                raise ModelDeferred(dict(category='TRANSIENT', retry_at=None))
            raise ValueError('fixture_invalid_review')
        return output([r['id'] for r in req['evidence']]) | dict(change='NONE', title='오전 최신 재검토')
    class Interrupted(BaseException):
        pass
    def interrupt(req):
        check = NewsDecisionStore(tmp_path / 'stores/news_episodes.sqlite3')
        try:
            successful = [e for e in check.events() if e['id'] in seen[:2]]
            assert len(successful) == 2 and all(e['last_delivery'] == 'QUEUED' for e in successful)
            refs = {ref for e in successful for ids in e['delivery_refs'].values() for ref in ids}
            queued_ids.extend(sorted(refs))
            rows = [ops.read_outbox(ref) for ref in refs]
            assert len(rows) == 1 and '오전 최신 재검토' in rows[0].body_evidence[0]
            assert any(text.startswith(MARKER) for text in rows[0].body_evidence)
            bad = next(e for e in check.events() if e['id'] == failed_id)
            assert bad['held_until'] and (failure == 'deferred' or bad['review_error'] == 'news_recheck_invalid')
        finally:
            check.close()
        raise Interrupted()
    config = dict(enabled=True, cutoff=NOW.isoformat(), delivery_enabled=True, max_batches=1)
    try:
        with pytest.raises(Interrupted):
            result = worker.run_once(tmp_path, config, now=release, screen=interrupt, analyze=analyze, clock=RECORDED_TIME, ops=ops)
            pytest.fail(str(result['ticks'][0]['failures']))
        assert list(dict.fromkeys(seen)) == [e['id'] for e in held]
        # Restart before any provider recovery: queued constituents are not sent again.
        with pytest.raises(Interrupted):
            result = worker.run_once(tmp_path, config, now=release + timedelta(minutes=1), screen=interrupt,
                                     analyze=analyze, clock=RECORDED_TIME, ops=ops)
            pytest.fail(str(result['ticks'][0]['failures']))
        assert queued_ids[0] == queued_ids[1]
    finally:
        ops.close()


def test_interrupted_morning_reassessment_rejoins_saved_success_into_one_digest(tmp_path):
    store = NewsDecisionStore(tmp_path/'morning.db')
    engine = Engine(store, lambda req: dict(candidates=[dict(evidence_ids=[r['id']]) for r in req['evidence']], ignored=[]),
        lambda req: decision([r['id'] for r in req['evidence']], importance='MEDIUM', urgency='NORMAL'))
    release = timestamp('2026-09-02T08:00:00+09:00')
    class Interrupted(BaseException):
        pass
    seen = []
    def review(req):
        seen.append(req['events'][0]['id'])
        if len(seen) == 2: raise Interrupted()
        return decision([r['id'] for r in req['evidence']], importance='MEDIUM', urgency='NORMAL', change='NONE')
    try:
        engine.tick([observation(k) for k in ('a', 'b', 'c')], NOW)
        engine.analyze = review
        with pytest.raises(Interrupted): engine.tick([], release)
        assert len(engine.pending_alerts()[0]['event_ids']) == 1
        engine = Engine(store, screen, lambda req: decision([r['id'] for r in req['evidence']],
            importance='MEDIUM', urgency='NORMAL', change='NONE'))
        engine.tick([], release + timedelta(minutes=1))
        pending = engine.pending_alerts()
        assert len(pending) == 1 and len(pending[0]['event_ids']) == 3
        assert len(set(pending[0]['constituent_ids'])) == 3
    finally:
        store.close()


def test_disabled_worker_creates_nothing_and_missing_sources_are_visible(tmp_path):
    never = lambda request: (_ for _ in ()).throw(AssertionError("model called"))
    now = timestamp("2026-09-02T09:00:00+09:00")
    assert worker.run_once(tmp_path, {"enabled": False}, now=now, screen=never, analyze=never, clock=RECORDED_TIME)["status"] == "DISABLED"
    assert not list(tmp_path.iterdir())
    result = worker.run_once(tmp_path, {"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00"}, now=now, screen=never, analyze=never, clock=RECORDED_TIME)
    assert result["status"] == "DEGRADED"
    assert all(row["status"] == "UNAVAILABLE" for row in result["sources"].values())
    assert not (tmp_path / "news/external_news_v4.sqlite3").exists()


T = timestamp("2026-09-07T09:06:00+09:00")


def enable(root):
    (root / "news_decision.json").write_text(json.dumps({"enabled": True, "cutoff": "2026-09-01T09:00:00+09:00"}))


def run(root, capsys, minutes, *extra, **kw):
    code = worker.main(["--root", str(root), "--now", (T + timedelta(minutes=minutes)).isoformat(), *extra], **kw)
    return code, json.loads(capsys.readouterr().out.strip().splitlines()[-1])


def test_same_unresolved_incident_does_not_repeat_across_hours_or_restarts(tmp_path, capsys):
    enable(tmp_path)
    code, first = run(tmp_path, capsys, 0)
    assert code == 2 and first["status"] == "DEGRADED" and "최초" in first["notice"] and "disclosures, news" in first["notice"]
    assert "notice" not in run(tmp_path, capsys, 1)[1] and "notice" not in run(tmp_path, capsys, 59)[1]
    assert "notice" not in run(tmp_path, capsys, 60)[1]
    assert "notice" not in run(tmp_path, capsys, 61)[1]
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert latest["status"] == "DEGRADED" and latest["health_notice"]["next_due_at"] is None
    # NEWS-owned times are stored in KST text; instants compare equal to the UTC-normalized T.
    assert latest["health_notice"]["since"] == "2026-09-07T09:06:00+09:00" and latest["started_at"].endswith("+09:00")
    assert latest["health"]["unavailable_sources"] == ["disclosures", "news", "sec"]


def degraded(count, **extra):
    health = {"input_errors": [{"error": "missing analytical text: interpretation", "count": count, "ids": []}], "review_errors": [], "unavailable_sources": []}
    return {"status": "DEGRADED", "health": health | extra}


def test_notice_identity_is_error_kind_and_change_recovery_are_immediate():
    state, notice = worker.health_notice(degraded(1), None, T)
    assert "최초 알림" in notice and "분석 보류 1건" in notice
    state, notice = worker.health_notice(degraded(3), state, T + timedelta(minutes=30))
    assert notice is None and state["next_due_at"] is None
    state, notice = worker.health_notice(degraded(3, unavailable_sources=["news"]), state, T + timedelta(minutes=31))
    assert "문제 종류 변경" in notice and "3건" in notice and "news" in notice and timestamp(state["since"]) == T
    state, notice = worker.health_notice({"status": "EXCEPTION", "health": {"exception": "OperationalError: database is locked"}}, state, T + timedelta(minutes=32))
    assert "문제 종류 변경" in notice and "OperationalError" in notice
    state, notice = worker.health_notice({"status": "OK", "health": {}}, state, T + timedelta(minutes=40))
    assert "복구" in notice and "0시간 40분" in notice and state["items"] == [] and state["next_due_at"] is None
    assert worker.health_notice({"status": "OK", "health": {}}, state, T + timedelta(minutes=41))[1] is None


def test_notice_reports_automatically_retired_rechecks_apart_from_unresolved_ones():
    state, notice = worker.health_notice(degraded(1, retired_stale_rechecks=4, review_error_events=[{"id": "e", "title": "t", "error": "x"}]), None, T)
    assert "사건 재점검 1건" in notice
    assert "재점검 대기 목록에서 4건 자동 제외 (해당 사건에 새 기사가 연결되면 재분석됨)" in notice
    assert "72시간 넘게 새 기사 없음" not in notice
    assert "재점검 대기 목록에서" not in worker.health_notice(degraded(1), None, T)[1]
    state, notice = worker.health_notice(degraded(1, closed_stale_errors=3), None, T)
    assert "묵은 입력 오류 자동 마감 3건" in notice


def test_tick_failures_and_recheck_errors_keep_degraded_and_never_fake_recovery(tmp_path, monkeypatch):
    store = NewsDecisionStore(tmp_path / "stores/news_episodes.sqlite3")
    store.save_event({"id": "e", "review_error": "review failed", "next_check_at": T.isoformat()})
    empty = lambda *a, **kw: {"status": "OK", "observations": [], "next_since": T.isoformat()}
    monkeypatch.setattr(worker, "read_news_observations", empty)
    monkeypatch.setattr(worker, "read_disclosure_observations", empty)
    monkeypatch.setattr(worker, "read_sec_observations", empty)
    report = worker.run_once(tmp_path, {"enabled": True, "cutoff": T.isoformat()}, now=T, screen=None, analyze=None, clock=RECORDED_TIME)
    assert report["status"] == "DEGRADED" and report["health"]["review_errors"] == ["review failed"]
    def failing(reason):
        tick = {"status": "DEGRADED", "sources": {}, "ticks": [{"failures": [{"stage": "urgent_queue", "event_id": "event-1a2b3c4d5e6f", "reason": reason}]}]}
        return dict(tick, health=worker.health_summary(store, tick))
    state, notice = worker.health_notice(failing("timeout after 90s"), None, T)
    assert "최초 알림" in notice and "timeout after 90s" in notice and state["items"] == ["review_error:review failed", "tick_failure:urgent_queue:timeout after #s"]
    assert worker.health_notice(failing("timeout after 91s"), state, T + timedelta(minutes=1))[1] is None
    previous, _ = worker.health_notice({"status": "EXCEPTION", "health": {"exception": "OSError: x"}}, None, T)
    assert "문제 종류 변경" in worker.health_notice(failing("OSError"), previous, T + timedelta(minutes=1))[1]
    unfinished = worker.health_notice({"status": "DEGRADED", "health": {}}, state, T + timedelta(minutes=2))
    assert unfinished == (state, None)
    store.close()


def test_tick_failure_already_durable_as_input_error_is_one_identity():
    error = "screen requires candidates and explicit ignored reasons"
    both = {"status": "DEGRADED", "health": {"input_errors": [{"error": error, "count": 3, "ids": []}], "tick_failures": [{"stage": "screen", "kind": error, "count": 1, "detail": error}]}}
    durable = {"status": "DEGRADED", "health": {"input_errors": [{"error": error, "count": 3, "ids": []}]}}
    state, notice = worker.health_notice(both, None, T)
    assert "최초 알림" in notice and state["items"] == ["input_error:" + error]
    assert worker.health_notice(durable, state, T + timedelta(minutes=1))[1] is None
    other = dict(both, health=dict(both["health"], tick_failures=[{"stage": "urgent_queue", "kind": "OSError", "count": 1, "detail": "OSError"}]))
    assert "문제 종류 변경" in worker.health_notice(other, state, T + timedelta(minutes=2))[1]


def test_unexpected_exception_is_bounded_but_never_hidden(tmp_path, capsys):
    enable(tmp_path)
    (tmp_path / "stores" / "news_episodes.sqlite3").mkdir(parents=True)
    code = worker.main(["--root", str(tmp_path), "--now", T.isoformat()])
    out, err = capsys.readouterr()
    first = json.loads(out.strip().splitlines()[-1])
    assert code == 1 and first["status"] == "EXCEPTION" and "실행 예외: NewsEpisodeStoreError" in first["notice"] and "Traceback" in err
    code, second = run(tmp_path, capsys, 1)
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert code == 1 and "notice" not in second and "Traceback" in latest["traceback"] and timestamp(latest["health_notice"]["since"]) == T


def test_targeted_retry_rejects_wrong_id_and_reprocesses_only_that_input(tmp_path, capsys):
    enable(tmp_path)
    store = NewsDecisionStore(tmp_path / "stores" / "news_episodes.sqlite3")
    store.ingest([{"id": key, "observed_at": T.isoformat(), "title": key, "text": "", "source_kind": "NEWS"} for key in ("x", "y")], T)
    store.mark(["x", "y"], "ERROR", "missing analytical text: interpretation")
    store.begin_call("old", "analyze", {}, T, 1, None)
    store.finish_call("old", error="ValueError")
    store.close()
    code, rejected = run(tmp_path, capsys, 1, "--retry-errors", "--input-id", "x", "--input-id", "nope")
    assert code == 2 and rejected["status"] == "RETRY_REJECTED" and "nope" in rejected["reason"]
    calls = []
    fake = lambda request: calls.append(request) or {"candidates": [], "ignored": [{"evidence_id": "x", "reason": "검증"}]}
    code, retried = run(tmp_path, capsys, 2, "--retry-errors", "--input-id", "x", build=lambda **kw: (fake, fake))
    assert len(calls) == 1 and retried["input_states"] == {"DONE": 1, "ERROR": 1}
    store = NewsDecisionStore(tmp_path / "stores" / "news_episodes.sqlite3")
    assert store.errors() == [("y", "missing analytical text: interpretation")]
    assert store.connection.execute("SELECT status FROM news_decision_calls WHERE id='old'").fetchone() == ("ERROR",)
    store.close()


def test_requeue_only_takes_both_locks_and_touches_nothing_else(tmp_path, capsys):
    enable(tmp_path)
    store = NewsDecisionStore(tmp_path / "stores" / "news_episodes.sqlite3")
    store.ingest([{"id": key, "observed_at": T.isoformat(), "title": key, "text": "", "source_kind": "NEWS"} for key in ("x", "y")], T)
    store.mark(["x", "y"], "ERROR", "missing analytical text: interpretation")
    store.save_event({"id": "e", "review_error": "review failed"})
    store.close()
    outer = tmp_path / "outer.lock"
    never = lambda **kw: pytest.fail("no model in requeue-only")
    with outer.open("a") as held:
        fcntl.flock(held, fcntl.LOCK_EX)
        code, busy = run(tmp_path, capsys, 0, "--retry-errors", "--input-id", "x", "--requeue-only", "--outer-lock", str(outer), build=never)
    assert (code, busy["status"]) == (0, "ALREADY_RUNNING")
    code, done = run(tmp_path, capsys, 0, "--retry-errors", "--input-id", "x", "--requeue-only", "--outer-lock", str(outer), build=never)
    assert (code, done["status"], done["input_ids"]) == (0, "REQUEUED", ["x"])
    assert not (tmp_path / "news/decision-worker/latest.json").exists()
    store = NewsDecisionStore(tmp_path / "stores" / "news_episodes.sqlite3")
    assert store.errors() == [("y", "missing analytical text: interpretation")] and store.events()[0]["review_error"] == "review failed"
    assert store.counts() == {"ERROR": 1, "READY": 1}
    store.close()


def test_notice_due_advances_only_after_stdout_handoff_succeeds(tmp_path, capsys):
    enable(tmp_path)
    def broken(text):
        raise BrokenPipeError("synthetic stdout failure")
    code, first = run(tmp_path, capsys, 0, emit=broken)
    latest = json.loads((tmp_path / "news/decision-worker/latest.json").read_text())
    assert code == 2 and first["notice_handoff"]["status"] == "FAILED" and "BrokenPipeError" in first["notice_handoff"]["error"]
    assert latest["health_notice"]["notified_at"] is None and "최초 알림" in latest["notice"]
    delivered = []
    code, second = run(tmp_path, capsys, 1, emit=delivered.append)
    assert "최초 알림" in delivered[0] and second["notice_handoff"]["status"] == "OK"
    assert json.loads((tmp_path / "news/decision-worker/latest.json").read_text())["health_notice"]["notified_at"] == "2026-09-07T09:07:00+09:00"


def test_outbox_health_notice_kept_for_direct_callers_but_not_scheduled_owner(tmp_path, monkeypatch):
    class Ops:
        recorded = []
        read_outbox = staticmethod(lambda key: None)
        def record_news_ops_failure_outbox(self, role, outbox, *, activation_enabled):
            self.recorded.append(outbox.alert_id)
    monkeypatch.setattr("tokmo.programs.pulse_production_deps.read_news_gate", lambda root: SimpleNamespace(delivery=SimpleNamespace(cutoff=T)))
    monkeypatch.setattr("tokmo.programs.news_alert_delivery.live_delivery_authorized", lambda ledger, delivery, now: (True, "test"))
    config = {"enabled": True, "cutoff": T.isoformat(), "delivery_enabled": True}
    direct = worker.run_once(tmp_path, config, now=T, screen=None, analyze=None, clock=RECORDED_TIME, ops=Ops())
    assert direct["status"] == "DEGRADED" and len(Ops.recorded) == 1
    scheduled = worker.run_once(tmp_path, config, now=T, screen=None, analyze=None, clock=RECORDED_TIME, ops=Ops(), health_owner="scheduled")
    assert scheduled["status"] == "DEGRADED" and len(Ops.recorded) == 1


def test_unaccounted_proposal_is_health_only_never_resent_or_deleted(tmp_path, monkeypatch):
    store = NewsDecisionStore(tmp_path / "stores/news_episodes.sqlite3")
    lost = {"id": "decision-alert-lost", "created_at": "2026-09-07T03:41:00+00:00", "kind": "NEWS", "evidence_ids": ["x"]}
    held = dict(lost, id="decision-alert-held")
    digest = {"id": "digest-1", "kind": "MORNING_DIGEST", "constituent_ids": ["decision-alert-m"], "event_ids": ["e-digest"], "created_at": "2026-09-07T03:41:00+00:00"}
    for event in ({"id": "e-lost", "last_alert": lost, "last_delivery": "PENDING", "pending_alerts": []},
                  {"id": "e-held", "last_alert": held, "last_delivery": "PENDING", "pending_alerts": [held]},
                  {"id": "e-sent", "last_alert": dict(lost, id="decision-alert-sent"), "last_delivery": "SENT", "delivery_refs": {"decision-alert-sent": ["nda-1"]}},
                  {"id": "e-digest", "last_alert": digest, "last_delivery": "QUEUED", "delivery_refs": {"digest-1": ["nda-2"]}},
                  {"id": "e-none", "decision": {"title": "t"}}):
        store.save_event(dict({"updated_at": T.isoformat(), "decision": {"title": "t", "facts": "f", "open": False}}, **event))
    before = store.events()
    store.close()
    empty = lambda *a, **kw: {"status": "OK", "observations": [], "next_since": T.isoformat()}
    monkeypatch.setattr(worker, "read_news_observations", empty)
    monkeypatch.setattr(worker, "read_disclosure_observations", empty)
    monkeypatch.setattr(worker, "read_sec_observations", empty)
    report = worker.run_once(tmp_path, {"enabled": True, "cutoff": T.isoformat()}, now=T, screen=None, analyze=None, clock=RECORDED_TIME)
    assert report["status"] == "DEGRADED"
    assert report["health"]["proposal_unaccounted"] == [{"event_id": "e-lost", "alert_id": "decision-alert-lost", "created_at": "2026-09-07T03:41:00+00:00", "last_delivery": "PENDING"}]
    state, notice = worker.health_notice(report, None, T)
    assert "발송 기록 없는 제안 1건" in notice and state["items"] == ["proposal_unaccounted"]
    assert worker.health_notice(report, state, T + timedelta(minutes=1))[1] is None
    store = NewsDecisionStore(tmp_path / "stores/news_episodes.sqlite3")
    assert store.events() == before and report["pending_proposals"] == 1
    store.close()


def test_scheduled_urgent_publish_passes_only_its_proposal(tmp_path, monkeypatch):
    from tokmo.records.ops_kernel import OpsKernelStore
    rows = [{"id": key, "observed_at": T.isoformat(), "title": key, "text": "본문", "source_kind": "NEWS", "publisher": "p", "url": "https://example.invalid/" + key} for key in ("ordinary", "urgent")]
    monkeypatch.setattr(worker, "read_news_observations", lambda *a, **kw: {"status": "OK", "observations": rows, "next_since": T.isoformat()})
    monkeypatch.setattr(worker, "read_disclosure_observations", lambda *a, **kw: {"status": "OK", "observations": [], "next_since": T.isoformat()})
    monkeypatch.setattr(worker, "read_sec_observations", lambda *a, **kw: {"status": "OK", "observations": [], "next_since": T.isoformat()})
    monkeypatch.setattr("tokmo.programs.pulse_production_deps.read_news_gate", lambda root: SimpleNamespace(delivery=SimpleNamespace(cutoff=T)))
    monkeypatch.setattr("tokmo.programs.news_alert_delivery.live_delivery_authorized", lambda ledger, delivery, now: (True, "test"))
    only = []
    real = worker.queue_proposals
    monkeypatch.setattr(worker, "queue_proposals", lambda *args, **kwargs: only.append(kwargs.get("only")) or real(*args, **kwargs))
    def analyze(request):
        key = request["evidence"][0]["id"]
        return dict(title=key, facts=key, interpretation="i", why_now="w", next_check="n", confirmation="REPORTED", change="NEW", evidence_ids=[key], open=True, notify=True,
                    **({"importance": "HIGH", "urgency": "IMMEDIATE"} if key == "urgent" else {"importance": "MEDIUM", "urgency": "NORMAL"}))
    (tmp_path / "ops").mkdir(mode=0o700)
    ops = OpsKernelStore(tmp_path / "ops/ops_kernel.sqlite3", stable_phase_ids=frozenset())
    screen = lambda request: {"candidates": [{"evidence_ids": ["ordinary"]}, {"evidence_ids": ["urgent"]}], "ignored": []}
    report = worker.run_once(tmp_path, {"enabled": True, "cutoff": T.isoformat(), "delivery_enabled": True}, now=T, screen=screen, analyze=analyze, clock=RECORDED_TIME, ops=ops)
    ops.close()
    urgent = next(alert["id"] for tick in report["ticks"] for alert in tick["alerts"] if alert["evidence_ids"] == ["urgent"])
    assert only == [urgent, None] and report["delivery"]["queued"] == 2 and report["status"] == "OK"


def test_fast_poll_is_opt_in_and_only_primary_sources(tmp_path):
    manifest = NewsPollManifest("news16-v1", True, "query", 10, ("NVDA",))
    seen = []
    def execute(invocation, campaign):
        seen.append(invocation.source)
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0)
    now = timestamp("2026-09-01T09:05:00+09:00")
    first = run_news_group(NewsGroup.A, now, manifest, clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute, fast_primary=True)
    second = run_news_group(NewsGroup.A, now, manifest, clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute, fast_primary=True)
    assert first.status == "SUCCEEDED" and second.status == "DUPLICATE_TICK"
    assert seen == ["yna-market"]
