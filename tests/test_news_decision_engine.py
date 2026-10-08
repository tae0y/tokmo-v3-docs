from datetime import timedelta
from functools import partial
import json
from pathlib import Path
import sqlite3

import pytest

from tokmo.programs.news_decision_engine import NewsDecisionEngine, RECORDED_TIME
from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries, queue_health_notice
from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole

NOW = timestamp("2026-09-01T23:00:00+09:00")
# Recorded-time mode: model calls use the tick's now, as the replay tool does.
Engine = partial(NewsDecisionEngine, clock=RECORDED_TIME)


def observation(key="a", at=NOW, kind="NEWS"):
    return dict(id=key, observed_at=at.isoformat(), source_kind=kind,
                title="시장 충격", text="검증 자료", publisher="검증 출처", url="https://example.invalid/" + key)


def decision(ids=("a",), **overrides):
    return dict(title="시장 충격", facts="전쟁 발생 보도", interpretation="유가 상승과 위험자산 변동성 확대 가능성",
        why_now="새로운 시장 전체 사건", next_check="공식 발표 및 유가", importance="HIGH",
        urgency="IMMEDIATE", confirmation="REPORTED", change="NEW", evidence_ids=list(ids), open=True, notify=True) | overrides


def followup(ids, **overrides):
    """S3 response contract: with a delivered or pending body in view, an UPDATE states its material change."""
    return decision(ids) | {"change": "UPDATE", "material_change": {"prior_claim": "이전 전달", "new_fact": "추가 보도로 달라진 사실",
                                                                  "support_ids": [ids[-1]], "why_not_covered": "이전 본문에 없음"}} | overrides


def screen(request):
    return {"candidates": [{"evidence_ids": [row["id"] for row in request["evidence"]],
                            "event_id": request["events"][0]["event_id"] if request["events"] else None,
                            "comparison_event_ids": [], "event_reason": "합성 연속 사건에 부착; 목록이 없으면 첫 관측"}], "ignored": []}


def test_failure_and_budget_do_not_silently_ignore_or_retry(tmp_path):
    calls = []
    def failing(request):
        calls.append(request)
        raise TimeoutError("provider timeout")
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, failing, failing)
    assert engine.tick([observation()], NOW)["failures"]
    assert engine.tick([], NOW + timedelta(minutes=1))["input_states"] == {"ERROR": 1}
    assert len(calls) == 1
    store.retry_errors()
    assert engine.tick([], NOW + timedelta(minutes=2))["calls"] == 1
    engine = Engine(store, screen, lambda request: decision(), token_budget=0)
    store.retry_errors()
    result = engine.tick([], NOW + timedelta(minutes=3))
    # The budget bounds analysis only: the screen still classifies, the analysis fails visibly.
    assert result["calls"] == 1 and result["failures"][0]["stage"] == "analyze" and "budget" in result["failures"][0]["reason"]
    store.close()


def test_daily_budget_counts_and_gates_analysis_stages_only(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    assert store.begin_call("s", "screen", {}, NOW, 10**9, 0) is None
    for stage in ("analyze_first", "analyze"):
        with pytest.raises(ValueError, match="budget"):
            store.begin_call(stage, stage, {}, NOW, 1, 0)
    assert store.begin_call("f", "analyze_first", {}, NOW, 30, 100) is None
    with pytest.raises(ValueError, match="budget"):
        store.begin_call("a", "analyze", {}, NOW, 71, 100)  # first-pass tokens count against the same allowance
    store.close()


def test_first_pass_settles_silence_and_the_primary_confirms_alerts(tmp_path):
    primary_calls = []
    def primary(request):
        primary_calls.append(request)
        return decision(importance="MEDIUM", urgency="NORMAL", notify=False)
    store = NewsDecisionStore(tmp_path / "silence.db")
    engine = Engine(store, screen, primary, first_pass=lambda request: decision(importance="MEDIUM", urgency="NORMAL", notify=False))
    report = engine.tick([observation()], NOW)
    assert [c["stage"] for c in report["model_calls"]] == ["screen", "analyze_first"]
    assert not primary_calls and report["first_pass_settled"] == 1 and not report["alerts"]
    assert {row[0] for row in store.connection.execute("SELECT stage FROM news_decision_calls")} == {"screen", "analyze_first"}
    store.close()
    # A first-pass HIGH/IMMEDIATE alert is a nomination over the identical request; the primary judgment stands.
    store = NewsDecisionStore(tmp_path / "alert.db")
    engine = Engine(store, screen, primary, first_pass=lambda request: decision())
    report = engine.tick([observation()], NOW)
    assert [c["stage"] for c in report["model_calls"]] == ["screen", "analyze_first", "analyze"]
    assert report["confirmations"] == [dict(reason="IMMEDIATE", evidence_ids=["a"])]
    assert primary_calls[0]["evidence"][0]["id"] == "a" and primary_calls[0]["task"] == "analyze"
    assert not report["alerts"] and report["decisions"][0]["notify"] is False
    store.close()


def test_confirmation_switch_and_evening_headline_exemption(tmp_path):
    store = NewsDecisionStore(tmp_path / "off.db")
    engine = Engine(store, screen, lambda request: pytest.fail("primary must stay idle when confirmation is off"),
                    first_pass=lambda request: decision(), confirm_enabled=False)
    report = engine.tick([observation()], NOW)
    assert [c["stage"] for c in report["model_calls"]] == ["screen", "analyze_first"] and len(report["alerts"]) == 1
    store.close()
    store = NewsDecisionStore(tmp_path / "digest.db")
    engine = Engine(store, screen, lambda request: pytest.fail("a MEDIUM evening headline settles on the first pass"),
                    first_pass=lambda request: decision(importance="MEDIUM", urgency="NORMAL", portfolio_note=""), headline_digest_hour=18)
    report = engine.tick([observation()], NOW)
    assert [c["stage"] for c in report["model_calls"]] == ["screen", "analyze_first"]
    assert report["decisions"][0]["notify"] is True and store.events()[0]["held_kind"] == "EVENING_HEADLINES"
    store.close()
    store = NewsDecisionStore(tmp_path / "personal.db")
    seen = []
    def primary(request):
        seen.append(request)
        return decision(importance="MEDIUM", urgency="NORMAL", portfolio_note="보유 종목과 직접 연결")
    engine = Engine(store, screen, primary, first_pass=lambda request: decision(importance="MEDIUM", urgency="NORMAL", portfolio_note="보유 종목과 직접 연결"))
    report = engine.tick([observation()], NOW)
    assert report["confirmations"][0]["reason"] == "NOTIFY" and len(seen) == 1
    store.close()


def test_stale_periodic_rechecks_are_retired_without_a_model_call(tmp_path):
    from tokmo.records.news_decision_store import kst
    silent = lambda request: decision(importance="MEDIUM", urgency="NORMAL", notify=False)
    store = NewsDecisionStore(tmp_path / "stale.db")
    Engine(store, screen, silent).tick([observation()], NOW)
    event = store.events()[0]
    due = timestamp(event["next_check_at"])  # open MEDIUM: 24h recheck
    report = Engine(store, screen, lambda request: pytest.fail("a stale recheck must not call the model")).tick([], due + timedelta(hours=73))
    assert report["retired_stale_rechecks"] == [event["id"]] and report["calls"] == 0 and not report["failures"]
    event = store.events()[0]
    assert event["next_check_at"] is None and event["review_error"] is None
    assert event["retired_review_failures"][0]["reason"] == "STALE_RECHECK"
    store.close()
    # A recheck due less than 72h ago still runs normally.
    store = NewsDecisionStore(tmp_path / "recent.db")
    engine = Engine(store, screen, silent)
    engine.tick([observation()], NOW)
    due = timestamp(store.events()[0]["next_check_at"])
    report = engine.tick([], due + timedelta(hours=1))
    assert report["calls"] == 1 and "retired_stale_rechecks" not in report
    store.close()
    # A failed recheck (outage error) older than 72h is retired with the error kept in the record; a held proposal is not touched.
    store = NewsDecisionStore(tmp_path / "failed.db")
    engine = Engine(store, screen, silent)
    engine.tick([observation()], NOW)
    event = store.events()[0]
    event["review_error"] = "subscription inference failed: EXECUTOR_UNKNOWN (exit 1)"
    store.save_event(event)
    report = engine.tick([], timestamp(event["next_check_at"]) + timedelta(hours=73))
    assert report["retired_stale_rechecks"] == [event["id"]] and store.events()[0]["retired_review_failures"][0]["error"].startswith("subscription")
    held = dict(store.events()[0], held_until=kst(NOW + timedelta(hours=8)), next_check_at=kst(NOW), review_error=None)
    store.save_event(held)
    report = engine.tick([], NOW + timedelta(days=5))
    assert "retired_stale_rechecks" not in report and report["calls"] == 1  # the held release still runs its own reassessment
    store.close()


def test_analyze_request_and_stored_snapshot_carry_the_compact_context(tmp_path):
    from copy import deepcopy
    from tokmo.records.news_decision_store import kst
    seen = []
    def analyze(request):
        seen.append(request)
        return decision(importance="MEDIUM", urgency="NORMAL", notify=False)
    context = dict(holdings=[dict(key="KR:000660", name="SK하이닉스", quantity=10)], instruments=[], exposures=[],
        etf_context=[dict(symbol="0162Z0", market="KR", holding_status="CONFIRMED_IN_SNAPSHOT",
            nav=dict(usable_for_price_decision=False, note="괴리율 설명"),
            components=dict(coverage="COMPLETE_DISCLOSURE", note="공시 기준 추정", asset_weights_pct=dict(stock="50.19", bond="46.43"),
                rows=[dict(asset_kind="stock", code="KR7000660001", key="KR:000660", name="SK하이닉스", weight_pct="25.15"),
                      dict(asset_kind="bond", code="KR103501GF30", key="ISIN:KR103501GF30", name="국고채", weight_pct="16.71")]))],
        investment_followup=dict(status="OK", choices=[dict(id="choice-1", keys=["KR:000660"])], delivered_reviews=[
            dict(id="r1", choice_id="choice-1", delivered_at="2026-08-30T10:00:00+09:00", comparison="옛 비교", summary="옛", new_facts=[dict(sources=["s1"], text="옛 사실")]),
            dict(id="r2", choice_id="choice-1", delivered_at="2026-08-31T10:00:00+09:00", comparison="새 비교", summary="새",
                 new_facts=[dict(sources=["s2"], text="새 사실"), dict(sources=["s3"], text="둘째"), dict(sources=["s4"], text="셋째")])]))
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, screen, analyze, context_mode="incremental", context_reader=lambda at: dict(deepcopy(context), observed_at=kst(at)))
    engine.tick([observation()], NOW)
    sent = seen[0]["context"]
    assert sent["etf_context"][0]["components"] == dict(coverage="COMPLETE_DISCLOSURE", asset_weights_pct=dict(stock="50.19", bond="46.43"),
        rows=[dict(key="KR:000660", code="KR7000660001", name="SK하이닉스", weight_pct="25.15")], non_stock_rows=1)
    assert sent["etf_context"][0]["nav"] == dict(usable_for_price_decision=False)
    assert sent["investment_followup"]["delivered_reviews"] == [dict(id="r2", choice_id="choice-1", delivered_at="2026-08-31T10:00:00+09:00",
        comparison="새 비교", summary="새", new_facts=["새 사실", "둘째"])]
    assert sent["holdings"][0]["key"] == "KR:000660" and sent["investment_followup"]["choices"] == context["investment_followup"]["choices"]
    assert store.events()[0]["context_snapshot"]["etf_context"] == sent["etf_context"]
    store.close()


@pytest.mark.parametrize("screen_result", [
    {"candidates": [], "ignored": []},
    {"candidates": [{"evidence_ids": ["absent"]}], "ignored": []},
])
def test_screen_cannot_omit_or_double_classify(tmp_path, screen_result):
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, lambda request: screen_result, lambda request: pytest.fail("Must not analyze"))
    result = engine.tick([observation()], NOW)
    assert result["failures"] and not result["alerts"]
    store.close()


def test_official_claim_requires_official_input_and_future_is_rejected(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    output = decision() | {"confirmation": "OFFICIAL"}
    engine = Engine(store, screen, lambda request: output)
    assert engine.tick([observation()], NOW)["failures"]
    with pytest.raises(ValueError, match="future"):
        engine.tick([observation("future", NOW + timedelta(seconds=1))], NOW)
    store.close()


def test_recovery_archives_exact_unqueued_draft_and_rechecks_without_new_news(tmp_path):
    store=NewsDecisionStore(tmp_path/'recovery.db')
    at=timestamp('2026-09-07T10:00:00+09:00')
    engine=Engine(store,screen,lambda req:decision())
    first=engine.tick([observation(at=at)],at)['alerts'][0]
    recovery=at+timedelta(hours=1)
    result=engine.retire_unqueued_proposals([first['id']],at=recovery,reason='reviewed stale draft after delivery failure')
    assert result['retired']==1 and not engine.pending_alerts()
    event=store.events()[0]
    assert event['retired_pending'][0]['body']==first['body']
    assert not event.get('last_sent') and event['evidence']
    assert engine.retire_unqueued_proposals([first['id']],at=recovery,reason='same approved recovery')['retired']==0
    def recheck(req):
        assert req['review_reason']=='DELIVERY_RECOVERY'
        assert not req['comparison']['pending']
        return decision(change='NONE')
    engine.analyze=recheck
    output=engine.tick([],recovery)
    assert len(output['alerts'])==1 and output['alerts'][0]['id']!=first['id']
    assert timestamp(output['alerts'][0]['created_at'])==recovery
    assert 'recovery_review_at' not in store.events()[0]
    store.close()


def test_recovery_cannot_retire_a_queued_draft(tmp_path):
    store=NewsDecisionStore(tmp_path/'queued.db');at=timestamp('2026-09-07T10:00:00+09:00')
    engine=Engine(store,screen,lambda req:decision())
    alert=engine.tick([observation(at=at)],at)['alerts'][0]
    event=store.events()[0];event['delivery_refs']={alert['id']:['existing-outbox']};store.save_event(event)
    with pytest.raises(ValueError,match='receipt investigation'):
        engine.retire_unqueued_proposals([alert['id']],at=at,reason='must not erase uncertain delivery')
    assert engine.pending_alerts()
    store.close()


def test_omitted_article_gets_one_repair_without_losing_valid_candidate(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def partial(request):
        calls.append(request)
        if len(calls) == 1:
            return {"candidates": [{"evidence_ids": ["a"]}], "ignored": []}
        assert [item["id"] for item in request["evidence"]] == ["b"]
        raise TimeoutError("repair unavailable")
    engine = Engine(store, partial, lambda request: decision())
    result = engine.tick([observation(), observation("b")], NOW)
    assert len(calls) == 2 and len(result["alerts"]) == 1
    assert result["input_states"] == {"DONE": 1, "ERROR": 1}
    assert result["failures"][0]["stage"] == "screen_repair"
    store.close()


def test_redundant_ignored_entry_cannot_cancel_important_candidate(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    response = {"candidates": [{"evidence_ids": ["a"]}], "ignored": [{"evidence_id": "a", "reason": "candidate above"}]}
    engine = Engine(store, lambda request: response, lambda request: decision())
    result = engine.tick([observation()], NOW)
    assert len(result["alerts"]) == 1 and not result["failures"]
    assert result["normalizations"]
    store.close()


def test_digest_survives_restart_and_actual_delivery_receipt(tmp_path):
    path = tmp_path / "state.db"
    store = NewsDecisionStore(path)
    output = decision() | {"importance": "MEDIUM", "urgency": "NORMAL", "confirmation": "OFFICIAL"}
    engine = Engine(store, screen, lambda request: output)
    assert not engine.tick([observation(kind="DISCLOSURE")], NOW)["alerts"]
    engine.analyze = lambda request: output | {"change": "NONE"}
    release = timestamp("2026-09-02T08:00:00+09:00")
    report = engine.tick([], release)
    digest = report["alerts"][0]
    assert digest["kind"] == "MORNING_DIGEST"
    store.close()
    store = NewsDecisionStore(path)
    engine = Engine(store, screen, lambda request: pytest.fail("Unexpected reanalysis"))
    assert engine.pending_alerts() == [digest]
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    role = ProducerRole.HERMES
    assert queue_proposals(engine, ops, role, cutoff=NOW)["queued"] == 0
    assert queue_proposals(engine, ops, role, cutoff=NOW, activation_enabled=True, test=True)["queued"] == 1
    assert store.events()[0]["last_delivery"] == "QUEUED"
    bodies = []
    result = run_alert_delivery(ops_kernel=ops, role=role, now=release,
        telegram_send=lambda body, **kw: bodies.append(body) or "fake-123",
        keyboard_resolver=lambda key: None, delivery_binder=lambda *a, **kw: None)
    assert result["sent"] == 1 and bodies[0].startswith("[재현테스트]")
    reconcile_deliveries(engine, ops)
    assert store.events()[0]["last_delivery"] == "SENT"
    assert queue_proposals(engine, ops, role, cutoff=NOW, activation_enabled=True)["queued"] == 0
    assert not engine.tick([], release + timedelta(minutes=1))["alerts"]
    store.close()
    ops.close()


def deliver(ops, engine, *, at):
    """Claim every queued NEWS part and settle it SENT at ``at`` with a Telegram-shaped receipt
    (integer message_id, as the dialogue sender returns); returns {proposal_id: [parts]}."""
    refs = {alert_id: list(parts) for event in engine.store.events() for alert_id, parts in event.get("delivery_refs", {}).items()}
    sent = {}
    while (row := ops.claim_pending_telegram(ProducerRole.HERMES, at)) is not None:
        ops.mark_sent(ProducerRole.HERMES, row.alert_id, at, str(1000 + len(sent)))
        proposal = next(key for key, parts in refs.items() if row.alert_id in parts)
        sent.setdefault(proposal, []).append(row.body_evidence[0])
    return sent


def test_war_followup_uses_actual_send_receipt_and_current_portfolio(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    context = {"holdings": [{"symbol": "005930"}], "observed_at": NOW.isoformat()}
    engine = Engine(store, screen, lambda request: decision(), context_reader=lambda at: context)
    first = engine.tick([observation()], NOW)["alerts"][0]
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    queue_proposals(engine, ops, ProducerRole.HERMES, cutoff=NOW, activation_enabled=True)
    # QUEUED is not a send: a proposal made now is still a first notice.
    engine.analyze = lambda request: followup(("b",), facts="추가 보도")
    queued = engine.tick([observation("b", NOW + timedelta(hours=1))], NOW + timedelta(hours=1))["alerts"][0]
    assert queued["body"].startswith("[중요 뉴스]")
    sent = deliver(ops, engine, at=NOW + timedelta(hours=2))
    reconcile_deliveries(engine, ops)
    assert store.events()[0]["last_sent"]["alert_id"] == first["id"] and store.events()[0]["last_sent"]["body"] == first["body"].replace('\nhttps://example.invalid/a', '')
    context = {"holdings": [], "cash_status": "NOT_PROVIDED", "observed_at": (NOW + timedelta(days=2)).isoformat()}
    def with_current_portfolio(request):
        prior = request["events"][0]
        assert prior["last_sent"]["body"] == first["body"].replace('\nhttps://example.invalid/a', '') and prior["last_sent"]["evidence_ids"] == ["a"]
        assert prior["last_sent"]["delivered_at"] == "2026-09-02T01:00:00+09:00" and prior["last_proposal_state"] == "PENDING"
        assert prior["context_snapshot"]["holdings"]
        assert request["context"]["holdings"] == []
        return followup(("c",), facts="전쟁 장기화, 유가 추가 상승", portfolio_note="현재 보유 주식이 없어 직접 노출은 줄었습니다. 매도 이유는 알 수 없습니다.")
    engine.analyze = with_current_portfolio
    result = engine.tick([observation("c", NOW + timedelta(days=2))], NOW + timedelta(days=2))
    assert len(store.events()) == 1 and sent == {first["id"]: [first["body"].replace('\nhttps://example.invalid/a', '')]} and not result["failures"]
    assert result["alerts"][0]["body"].startswith("[후속]")
    store.close(); ops.close()


def test_last_sent_is_the_latest_complete_actual_send_and_never_regresses(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    role = ProducerRole.HERMES
    engine = Engine(store, screen, lambda request: decision())
    day = timestamp("2026-09-01T10:00:00+09:00")
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    first = engine.tick([observation("a", day)], day)["alerts"][0]
    queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)
    engine.analyze = lambda request: followup(("b",), facts="두 번째 보도")
    second = engine.tick([observation("b", day + timedelta(minutes=1))], day + timedelta(minutes=1))["alerts"][0]
    queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)
    parts = {ref: alert_id for alert_id, refs in store.events()[0]["delivery_refs"].items() for ref in refs}
    claimed = {parts[row.alert_id]: row for row in (ops.claim_pending_telegram(role, day + timedelta(minutes=2)) for _ in range(2))}
    # The newer proposal is delivered first; the older one settles later with an earlier receipt time.
    ops.mark_sent(role, claimed[second["id"]].alert_id, day + timedelta(minutes=3), "2")
    reconcile_deliveries(engine, ops)
    event = store.events()[0]
    assert event["last_sent"]["alert_id"] == second["id"] and event["last_sent"]["delivered_at"] == "2026-09-01T10:03:00+09:00"
    assert event["last_sent"]["outbox_refs"] == store.events()[0]["delivery_refs"][second["id"]] and event["last_delivery"] == "SENT"
    ops.mark_sent(role, claimed[first["id"]].alert_id, day + timedelta(minutes=2), "1")
    writes = []
    store.save_event = lambda value, _save=store.save_event: writes.append(value["id"]) or _save(value)
    reconcile_deliveries(engine, ops)
    assert store.events()[0]["last_sent"]["alert_id"] == second["id"]
    assert not store.events()[0].get("comparison_proposals")  # Older completed body is pruned without regressing last_sent.
    writes.clear()
    reconcile_deliveries(engine, ops)
    assert writes == []  # An unchanged reconciliation is still read-only.
    # A multipart proposal is a baseline only once every part is SENT; an UNKNOWN part is never one.
    engine.analyze = lambda request: followup(("c",), facts="긴 " + "본문" * 2000)
    third = engine.tick([observation("c", day + timedelta(minutes=5))], day + timedelta(minutes=5))["alerts"][0]
    queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)
    refs = store.events()[0]["delivery_refs"][third["id"]]
    assert len(refs) == 2
    ops.mark_sent(role, ops.claim_pending_telegram(role, day + timedelta(minutes=6)).alert_id, day + timedelta(minutes=6), "31")
    reconcile_deliveries(engine, ops)
    assert store.events()[0]["last_sent"]["alert_id"] == second["id"] and store.events()[0]["last_delivery"] == "QUEUED"
    ops.mark_sent(role, ops.claim_pending_telegram(role, day + timedelta(minutes=7)).alert_id, day + timedelta(minutes=7), "32")
    reconcile_deliveries(engine, ops)
    event = store.events()[0]
    assert event["last_sent"]["alert_id"] == third["id"] and event["last_sent"]["body"] == third["body"].replace('\nhttps://example.invalid/c', '') and event["last_sent"]["parts"] == 2
    assert event["last_sent"]["delivered_at"] == "2026-09-01T10:07:00+09:00" and event["last_sent"]["evidence_ids"] == ["c"]
    engine.analyze = lambda request: followup(("d",), facts="네 번째")
    engine.tick([observation("d", day + timedelta(minutes=8))], day + timedelta(minutes=8))
    queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)
    ops.mark_unknown(role, ops.claim_pending_telegram(role, day + timedelta(minutes=9)).alert_id, day + timedelta(minutes=9), "provider_timeout")
    reconcile_deliveries(engine, ops)
    assert store.events()[0]["last_sent"]["alert_id"] == third["id"] and store.events()[0]["last_delivery"] == "UNKNOWN"
    store.close(); ops.close()


@pytest.mark.parametrize('legacy', [False, True])
def test_digest_send_is_recorded_on_every_constituent_event_and_simulated_sends_are_not(tmp_path, legacy):
    store = NewsDecisionStore(tmp_path / "state.db")
    role = ProducerRole.HERMES
    split = lambda request: {"candidates": [{"evidence_ids": [item["id"]]} for item in request["evidence"]], "ignored": []}
    quiet = lambda request: decision((request["evidence"][0]["id"],)) | {"importance": "MEDIUM", "urgency": "NORMAL", "confirmation": "OFFICIAL"}
    engine = Engine(store, split, quiet)
    assert not engine.tick([observation("a", NOW, "DISCLOSURE"), observation("b", NOW, "DISCLOSURE")], NOW)["alerts"]
    engine.analyze = lambda request: quiet(request) | {"change": "NONE"}
    release = timestamp("2026-09-02T08:00:00+09:00")
    digest = engine.tick([], release)["alerts"][0]
    assert digest["kind"] == "MORNING_DIGEST" and len(digest["event_ids"]) == 2
    if legacy:
        digest.pop('evidence_snapshots')
        for event in store.events():
            event['last_alert'].pop('evidence_snapshots', None)
            for alert in event.get('pending_alerts', []):alert.pop('evidence_snapshots', None)
            store.save_event(event)
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    queue_proposals(engine, ops, role, cutoff=NOW, activation_enabled=True, test=True)
    ops.mark_sent(role, ops.claim_pending_telegram(role, release).alert_id, release, "SIMULATED-1")
    reconcile_deliveries(engine, ops)
    assert all(event["last_delivery"] == "SENT" and "last_sent" not in event for event in store.events())
    with sqlite3.connect(tmp_path / "ops.db") as connection:  # fixture: the same message with the group route's receipt form
        connection.execute("UPDATE alert_outbox SET provider_message_id='telegram-group:-1000000000000:9' WHERE alert_id LIKE 'nda-%'")
    reconcile_deliveries(engine, ops)
    for event in store.events():
        assert event["last_sent"]["alert_id"] == digest["id"] and event["last_sent"]["body"] == "[재현테스트]\n" + digest["body"].replace('\nhttps://example.invalid/a', '').replace('\nhttps://example.invalid/b', '')
        assert event["last_sent"]["constituent_ids"] == digest["constituent_ids"]
        if legacy:
            assert event['last_sent']['evidence_snapshot']=='UNVERIFIED'
        else:
            assert event['last_sent']['evidence_ids']==digest['evidence_snapshots'][event['id']]
    store.close(); ops.close()


def test_urgent_publish_queues_only_its_own_proposal(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    role = ProducerRole.HERMES
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    day = timestamp("2026-09-01T10:00:00+09:00")
    seen = []
    def analyze(request):
        key = request["evidence"][0]["id"]
        return decision((key,)) | ({"importance": "MEDIUM", "urgency": "NORMAL"} if key == "ordinary" else {})
    engine = Engine(store, lambda request: {"candidates": [{"evidence_ids": ["ordinary"]}, {"evidence_ids": ["urgent"]}], "ignored": []}, analyze,
                    on_urgent=lambda alert: seen.append(queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True, only=alert["id"])))
    engine.tick([observation("ordinary", day), observation("urgent", day)], day)
    states = {event["evidence"][0]["id"]: event["last_delivery"] for event in store.events()}
    assert seen == [{"queued": 1}] and states == {"ordinary": "PENDING", "urgent": "QUEUED"}
    assert queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)["queued"] == 1
    store.close(); ops.close()


def test_urgent_news_passes_ordinary_backlog_and_unknown_is_not_retried(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    engine = Engine(store, lambda request: {"candidates": [{"evidence_ids": [item["id"]], "event_id": None, "comparison_event_ids": [], "event_reason": "기존 보통 사건과 별개의 긴급 사건"} for item in request["evidence"]], "ignored": []},
        lambda request: decision((request["evidence"][0]["id"],)))
    day = timestamp("2026-09-01T10:00:00+09:00")
    engine.analyze = lambda request: decision(("ordinary",)) | {"urgency": "NORMAL", "importance": "MEDIUM"}
    engine.tick([observation("ordinary", day)], day)
    engine.analyze = lambda request: decision(("urgent",))
    engine.tick([observation("urgent", day + timedelta(minutes=1))], day + timedelta(minutes=1))
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    role = ProducerRole.HERMES
    queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)
    claimed = ops.claim_pending_telegram(role, day + timedelta(minutes=2))
    assert claimed.severity == "NEWS_IMMEDIATE"
    ops.mark_unknown(role, claimed.alert_id, day + timedelta(minutes=2), "provider_timeout")
    reconcile_deliveries(engine, ops)
    assert queue_proposals(engine, ops, role, cutoff=day, activation_enabled=True)["queued"] == 0
    next_row = ops.claim_pending_telegram(role, day + timedelta(minutes=3))
    assert next_row.severity == "ORDINARY" and next_row.alert_id != claimed.alert_id
    store.close(); ops.close()


def test_urgent_proposal_is_durable_and_published_before_next_analysis(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    published = []
    def analyze(request):
        key = request["evidence"][0]["id"]
        if key == "b":
            assert published == ["a"]
        return decision((key,))
    def publish(alert):
        assert store.events()
        published.append(alert["evidence_ids"][0])
    engine = Engine(store, lambda request: {"candidates": [{"evidence_ids": ["a"]}, {"evidence_ids": ["b"]}], "ignored": []}, analyze, on_urgent=publish)
    assert not engine.tick([observation(), observation("b")], NOW)["failures"]
    assert published == ["a", "b"]
    store.close()


def malformed(key='interpretation "interpretation'):
    value = decision()
    value[key] = value.pop("interpretation")
    return value


@pytest.mark.parametrize("damaged, reason", [
    (malformed(), "missing analytical text: interpretation"),
    (decision() | {"importance": []}, "invalid importance"),
])
def test_malformed_analysis_gets_one_correction_and_keeps_original_evidence(tmp_path, damaged, reason):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def analyze(request):
        calls.append(request)
        if "correction" not in request:
            return dict(damaged)
        assert request["correction"]["previous_response"] == damaged
        assert request["correction"]["reason"] == reason
        return decision()
    engine = Engine(store, screen, analyze)
    result = engine.tick([observation()], NOW)
    assert len(calls) == 2 and len(result["alerts"]) == 1 and not result["failures"]
    assert result["corrections"] == [{"stage": "analyze", "evidence_ids": ["a"], "reason": reason, "status": "OK"}]
    rows = store.connection.execute("SELECT status,response FROM news_decision_calls WHERE stage='analyze' ORDER BY rowid").fetchall()
    assert [row[0] for row in rows] == ["OK", "OK"] and json.loads(rows[0][1]) == damaged
    assert result["input_states"] == {"DONE": 1}
    store.close()


def test_invalid_correction_keeps_error_without_loop_while_sibling_completes(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def analyze(request):
        calls.append(request)
        key = request["evidence"][0]["id"]
        return decision(("b",)) if key == "b" else malformed()
    two = lambda request: {"candidates": [{"evidence_ids": ["a"]}, {"evidence_ids": ["b"]}], "ignored": []}
    engine = Engine(store, two, analyze)
    result = engine.tick([observation(), observation("b")], NOW)
    assert len(calls) == 3 and result["input_states"] == {"DONE": 1, "ERROR": 1}
    assert result["failures"] == [{"stage": "analyze", "evidence_ids": ["a"], "reason": "missing analytical text: interpretation"}]
    assert result["corrections"][0]["status"] == "FAILED" and len(result["alerts"]) == 1
    assert engine.tick([], NOW + timedelta(minutes=1))["calls"] == 0
    store.close()


def test_transport_failure_is_not_corrected(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def analyze(request):
        calls.append(request)
        raise TimeoutError("provider timeout")
    result = Engine(store, screen, analyze).tick([observation()], NOW)
    assert len(calls) == 1 and "corrections" not in result and result["input_states"] == {"ERROR": 1}
    assert store.connection.execute("SELECT status,error FROM news_decision_calls WHERE stage='analyze'").fetchall() == [("ERROR", "TimeoutError")]
    store.close()


BROKEN_SCREEN = {"candidates": [{"evidence_ids": ["a"], "event_id": None, "title": "ECB"}], "candidates_note": "b는 기존 사건과 겹침"}


def test_screen_missing_ignored_gets_one_correction_covering_every_input(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def screen_model(request):
        calls.append(request)
        if "correction" not in request:
            return dict(BROKEN_SCREEN)
        assert request["correction"]["previous_response"] == BROKEN_SCREEN
        assert request["correction"]["reason"] == "screen requires candidates and explicit ignored reasons"
        assert [item["id"] for item in request["evidence"]] == ["a", "b", "c"]
        return {"candidates": [{"evidence_ids": ["a"]}], "ignored": [{"evidence_id": "b", "reason": "중복"}, {"evidence_id": "c", "reason": "홍보"}]}
    engine = Engine(store, screen_model, lambda request: decision())
    result = engine.tick([observation(), observation("b"), observation("c")], NOW)
    assert len(calls) == 2 and len(result["alerts"]) == 1 and not result["failures"]
    assert result["corrections"] == [{"stage": "screen", "evidence_ids": ["a", "b", "c"], "reason": "screen requires candidates and explicit ignored reasons", "status": "OK"}]
    assert result["input_states"] == {"DONE": 3}
    rows = store.connection.execute("SELECT status,response FROM news_decision_calls WHERE stage='screen' ORDER BY rowid").fetchall()
    assert [row[0] for row in rows] == ["OK", "OK"] and json.loads(rows[0][1]) == BROKEN_SCREEN
    store.close()


@pytest.mark.parametrize("corrected", [
    {"candidates": [{"evidence_ids": ["a"]}], "ignored": [{"evidence_id": "b", "reason": "중복"}]},
    {"candidates": [{"evidence_ids": ["a"]}], "ignored": [{"evidence_id": "b", "reason": "중복"}, {"evidence_id": "zzz", "reason": "없는 입력"}]},
    {"candidates": [{"evidence_ids": ["a"]}, {"evidence_ids": ["a", "c"]}], "ignored": [{"evidence_id": "b", "reason": "중복"}]},
    {"candidates": [{"evidence_ids": ["a"]}], "ignored": [{"evidence_id": "b", "reason": "중복"}, {"evidence_id": "c"}]},
])
def test_invalid_screen_correction_marks_batch_error_without_second_repair(tmp_path, corrected):
    store = NewsDecisionStore(tmp_path / "state.db")
    calls = []
    def screen_model(request):
        calls.append(request)
        return dict(corrected) if "correction" in request else dict(BROKEN_SCREEN)
    engine = Engine(store, screen_model, lambda request: pytest.fail("no analysis for an invalid screen"))
    result = engine.tick([observation(), observation("b"), observation("c")], NOW)
    assert len(calls) == 2 and result["input_states"] == {"ERROR": 3} and result["corrections"][0]["status"] == "FAILED"
    assert result["failures"] == [{"stage": "screen", "reason": result["failures"][0]["reason"]}] and result["failures"][0]["reason"]
    assert engine.tick([], NOW + timedelta(minutes=1))["calls"] == 0
    store.close()


def test_health_notice_respects_quiet_hours_and_does_not_repeat(tmp_path):
    ops = OpsKernelStore(tmp_path / "ops.db", stable_phase_ids=frozenset())
    role = ProducerRole.HERMES
    report = {"input_states": {"ERROR": 2}, "sources": {}}
    queue_health_notice(ops, role, report, NOW)
    assert ops.claim_pending_telegram(role, NOW) is None
    day = timestamp("2026-09-02T08:00:00+09:00")
    queue_health_notice(ops, role, report, day)
    queue_health_notice(ops, role, report, day + timedelta(minutes=1))
    alert = ops.claim_pending_telegram(role, day)
    assert "분석 보류 2건" in alert.body_evidence[0]
    assert ops.claim_pending_telegram(role, day + timedelta(minutes=1)) is None
    ops.close()
