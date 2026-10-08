from dataclasses import replace
import json
import os
from datetime import datetime, timedelta, timezone

import pytest

from tokmo.programs.news_event_admission import (_outbox_from_revision,
    reconcile_news_outbox, run_news_event_admission)
from tokmo.programs.news_alert_delivery import run_alert_delivery
from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.programs.pulse_production_deps import build_pulse_production_deps
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import (AlertChannel, AlertOutboxV1, AlertStatus,
    IncidentKeyV1,ProducerRole)
from tokmo.values.session_gate import Market

NOW=datetime(2026,8,21,tzinfo=timezone.utc)

def test_tx_c_d_e_crash_reconcile_converges_one_canonical_outbox(tmp_path):
    news=NewsEpisodeStore(tmp_path/"news"/"episodes.sqlite")
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    episode=news.upsert("outbox-flow",(),NOW)
    draft={"episode_id":episode.episode_id,"revision_no":1,"revision_kind":"INITIAL",
           "body_text":"ten-section rendered alert","portfolio_context_digest":"b"*64,
           "confidence":70,"material_change_kind":"FIRST_ADMISSION",
           "material_change_observed_at":NOW,"links":('https://example.com',),
           "acknowledges_prior_reaction":False}
    with pytest.raises(RuntimeError):
        run_news_event_admission(store=news,ops_kernel=ops,role=ProducerRole.HERMES,
            draft=draft,activation_enabled=False,now=NOW,inject_crash_at="after_tx_c")
    assert len(news.list_pending_outbox_revisions())==1
    assert reconcile_news_outbox(store=news,ops_kernel=ops,role=ProducerRole.HERMES,
        activation_enabled=False,now=NOW)==1
    assert reconcile_news_outbox(store=news,ops_kernel=ops,role=ProducerRole.HERMES,
        activation_enabled=False,now=NOW)==0
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'").fetchone())==(1,)
    row=news.connection.execute("SELECT delivery_state,ops_alert_id FROM alert_revisions").fetchone()
    assert row[0]=="OUTBOXED" and row[1].startswith("nal-")

def test_no_parallel_in_memory_news_queue_surface():
    import tokmo.programs.news_event_admission as admission
    import tokmo.programs.news_alert_delivery as delivery
    assert not hasattr(admission,"NewsOutboxDecision")
    assert not hasattr(delivery,"deliver_pending")

def _draft(episode_id):
    return {"episode_id":episode_id,"revision_no":1,"revision_kind":"INITIAL",
        "body_text":"body","portfolio_context_digest":"b"*64,"confidence":70,
        "material_change_kind":"FIRST_ADMISSION","material_change_observed_at":NOW,
        "links":("https://example.com",)}

def test_news_worker_never_claims_non_news_telegram_alerts(tmp_path):
    news=NewsEpisodeStore(tmp_path/"news.sqlite")
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    key=IncidentKeyV1("market_unclassified","pension-scope")
    ops.observe_incident(ProducerRole.COLLECTOR,key,"pension",NOW)
    pension=AlertOutboxV1.for_incident(alert_id="pension-alert",key=key,tick_id=1,
        subject_id="pension",severity="HIGH",body_evidence=("pension",),created_at=NOW)
    ops.record_incident_outbox(ProducerRole.HERMES,pension)
    with ops._transaction() as connection:
        connection.execute("UPDATE alert_outbox SET channel='TELEGRAM',status='PENDING' WHERE alert_id='pension-alert'")
    episode=news.upsert("news-origin",(),NOW)
    run_news_event_admission(store=news,ops_kernel=ops,role=ProducerRole.HERMES,
        draft=_draft(episode.episode_id),activation_enabled=True,now=NOW)
    sent=[]
    result=run_alert_delivery(ops_kernel=ops,role=ProducerRole.HERMES,
        telegram_send=lambda body,reply_markup=None:(sent.append(body) or "42"),
        keyboard_resolver=lambda alert_id:None,
        delivery_binder=news.bind_delivery_by_ops_alert,now=NOW)
    assert result["claimed"]==1 and sent==["body"]
    assert ops.read_outbox("pension-alert").status.value=="PENDING"

def test_w1_reconcile_converges_across_different_clocks(tmp_path):
    news=NewsEpisodeStore(tmp_path/"news.sqlite")
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    episode=news.upsert("w1-clock",(),NOW)
    revision_id=news.admit_alert_revision(_draft(episode.episode_id),now=NOW)
    row=news.list_pending_outbox_revisions()[0]
    outbox=_outbox_from_revision(row,activation_enabled=False,now=NOW)
    ops.record_news_alert_outbox(ProducerRole.HERMES,outbox,activation_enabled=False)
    assert reconcile_news_outbox(store=news,ops_kernel=ops,role=ProducerRole.HERMES,
        activation_enabled=False,now=NOW+timedelta(minutes=5))==1
    assert tuple(ops._connect().execute("SELECT count(*) FROM alert_outbox WHERE origin='NEWS_EVENT'").fetchone())==(1,)
    assert news.connection.execute("SELECT delivery_state FROM alert_revisions WHERE alert_revision_id=?",(revision_id,)).fetchone()==("OUTBOXED",)

def _enable_news(root):
    path=root/"news_activation.json"
    path.write_text(json.dumps({"enabled":True,"cutoff":NOW.isoformat(),"approved_by":"woong"}))
    os.chmod(path,0o600)

def test_pulse_tick_leaves_pension_queue_untouched(tmp_path):
    _enable_news(tmp_path)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    key=IncidentKeyV1("market_unclassified","pension-pulse")
    ops.observe_incident(ProducerRole.COLLECTOR,key,"pension",NOW)
    pension=AlertOutboxV1.for_incident(alert_id="pension-pulse",key=key,tick_id=1,
        subject_id="pension",severity="HIGH",body_evidence=("pension",),created_at=NOW)
    ops.record_incident_outbox(ProducerRole.HERMES,pension)
    with ops._transaction() as connection:
        connection.execute("UPDATE alert_outbox SET channel='TELEGRAM',status='PENDING' WHERE alert_id='pension-pulse'")
    deps=build_pulse_production_deps(root=tmp_path,clock=lambda:NOW,
        watchlist_symbols={Market.KR:frozenset(),Market.US:frozenset()},dialogue_cutoff=NOW,
        news_ops_kernel=ops)
    assert deps.news_delivery_runner(NOW)["claimed"]==0
    assert ops.read_outbox("pension-pulse").status is AlertStatus.PENDING

def test_pulse_survives_news_stage_exception(tmp_path):
    deps=build_pulse_production_deps(root=tmp_path,clock=lambda:NOW,
        watchlist_symbols={Market.KR:frozenset(),Market.US:frozenset()},dialogue_cutoff=NOW)
    dialogue_calls=[]
    deps=replace(deps,news_outbox_reconcile=lambda now:(_ for _ in ()).throw(RuntimeError("boom")),
        cadence_boundary_gate=lambda slot:True,playbook_validator=lambda root:(dialogue_calls.append(1) or []))
    result=run_pulse_once(root=tmp_path,policy_version="pulse-60s-v1",
        scheduled_for_utc=NOW,observed_now_utc=NOW,deps=deps)
    assert any(stage["stage"]=="news_outbox" and stage["status"]=="failed" for stage in result.stages)
    assert any(stage["stage"]=="dialogue" for stage in result.stages)
