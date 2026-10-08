from datetime import datetime, timedelta, timezone

from tokmo.programs.news_event_admission import (NewsAdmissionRejected,record_early_factual_revision,
    record_correction_revision,AdmissionInput,admit_news_event,run_news_event_admission)
from tokmo.values.news_payload import validate_news_payload
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
import pytest
from tokmo.records.news_episode_store import NewsEpisodeStore

NOW=datetime(2026,8,21,tzinfo=timezone.utc)

def test_same_cause_followups_merge_parent_but_independent_core_remain_distinct(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite")
    parent=store.upsert_with_cause("root-event",(),NOW,cause_key="cause-a")
    follow=store.upsert_with_cause("followup-event",(),NOW,cause_key="cause-a")
    other=store.upsert_with_cause("independent-core",(),NOW,cause_key="cause-b")
    assert follow.episode_id==parent.episode_id and other.episode_id!=parent.episode_id
    store.mute_episode(parent.episode_id,"explicit",now=NOW)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    draft={"episode_id":parent.episode_id,"revision_no":1,"revision_kind":"INITIAL","body_text":"muted",
        "portfolio_context_digest":"b"*64,"confidence":50,"material_change_kind":"SCALE_CHANGE",
        "material_change_observed_at":NOW,"links":('https://example.com',)}
    with pytest.raises(NewsAdmissionRejected):run_news_event_admission(store=store,ops_kernel=ops,
        role=ProducerRole.HERMES,draft=draft,activation_enabled=False,now=NOW)

def test_early_factual_and_correction_are_durable_immutable_revisions(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("early",(),NOW)
    early=record_early_factual_revision(store,episode.episode_id,source_id="bbc",
        unknowns=("official",),checkpoint="independent",body="잠정 미확인 속보",
        portfolio_context_digest="b"*64,links=('https://example.com',),confidence=40,now=NOW)
    correction=record_correction_revision(store,episode.episode_id,body="공식 정정",
        portfolio_context_digest="b"*64,links=('https://example.com/correction',),confidence=90,now=NOW+timedelta(minutes=5))
    assert early and correction and early!=correction
    assert store.connection.execute("SELECT state,provisional FROM episodes WHERE episode_id=?",(episode.episode_id,)).fetchone()==("DISCONFIRMED",0)
    assert tuple(r[0] for r in store.connection.execute("SELECT revision_kind FROM alert_revisions ORDER BY revision_no"))==("EARLY_FACTUAL","CORRECTION")

def test_meta_feedback_requires_three_recent_negative_and_honors_cooldown(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");episode=store.upsert("meta",(),NOW)
    reaction_ids=[]
    for revision in range(1,4):
        rid=store.admit_alert_revision({"episode_id":episode.episode_id,"revision_no":revision,
            "revision_kind":"INITIAL" if revision==1 else "MATERIAL_UPDATE","body_text":f"body {revision}",
            "portfolio_context_digest":"b"*64,"confidence":50,"material_change_kind":"SCALE_CHANGE",
            "material_change_observed_at":NOW,"links":('https://example.com',)},now=NOW)
        event={**store.revision_by_callback(rid[:32]),"provider_update_id":revision,"provider_message_id":str(revision),"reaction":"NOT_HELPFUL"}
        store.append_reaction(event,now=NOW);reaction_ids.append(store.connection.execute("SELECT reaction_event_id FROM alert_reactions WHERE provider_update_id=?",(revision,)).fetchone()[0])
    assert store.propose_meta_feedback("context",tuple(reaction_ids),now=NOW)=="CREATED"
    assert store.propose_meta_feedback("context",tuple(reaction_ids),now=NOW)=="COOLDOWN"
    assert store.propose_meta_feedback("weak",tuple(reaction_ids[:2]),now=NOW)=="INSUFFICIENT"

def test_systemic_realert_after_mute_renders_acknowledgement_and_new_identity(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite");muted=store.upsert("muted-risk",(),NOW)
    store.mute_episode(muted.episode_id,"explicit",now=NOW)
    systemic=store.upsert("systemic-new-risk",(),NOW)
    admitted=admit_news_event(AdmissionInput(systemic.episode_id,1,"CORE_BREAKING","SYSTEMIC_TRANSMISSION",
        NOW,NOW,False,False,parent_episode_id=muted.episode_id))
    assert systemic.episode_id!=muted.episode_id
    assert "mute" in admitted["body"] and "신규 정체성 근거" in admitted["body"]
    sections={str(index):admitted["body"] if index==1 else f"section {index}" for index in range(1,11)}
    assert validate_news_payload(sections=sections,links=("https://example.com",),
        callback_data="n1:u:"+"a"*32,revision_kind="INITIAL",provisional=False)
    ops=OpsKernelStore(tmp_path/"ops"/"kernel.sqlite",stable_phase_ids=frozenset())
    run_news_event_admission(store=store,ops_kernel=ops,role=ProducerRole.HERMES,draft={
        "episode_id":systemic.episode_id,"revision_no":1,"revision_kind":"INITIAL",
        "body_text":admitted["body"],"portfolio_context_digest":"b"*64,"confidence":60,
        "material_change_kind":"SYSTEMIC_TRANSMISSION","material_change_observed_at":NOW,
        "links":("https://example.com",)},activation_enabled=False,now=NOW)
    assert store.connection.execute("SELECT body_text FROM alert_revisions WHERE episode_id=?",(systemic.episode_id,)).fetchone()==(admitted["body"],)
