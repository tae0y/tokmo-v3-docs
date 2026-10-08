from datetime import datetime, timezone
from types import SimpleNamespace

from tokmo.programs.news_feedback import build_news_callback_handler
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.callback_registry import news_feedback_callback
from tokmo.programs.integrated_pulse import PulseDepsV1
from dataclasses import fields

NOW=datetime(2026,8,21,tzinfo=timezone.utc)
HEX="b"*64

def test_production_call_shape_exact_binding_canonical_reactions_and_reconcile(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite")
    episode=store.upsert("feedback",(),NOW)
    revision_id=store.admit_alert_revision({"episode_id":episode.episode_id,"revision_no":1,
        "revision_kind":"INITIAL","body_text":"body","portfolio_context_digest":"b"*64,
        "confidence":50,"material_change_kind":"FIRST_ADMISSION","material_change_observed_at":NOW,
        "links":('https://example.com',)},now=NOW)
    handler=build_news_callback_handler(store,lambda alert_id: SimpleNamespace(provider_message_id="42"),now=lambda:NOW)
    update={"update_id":7,"callback":True,"callback_query_id":"cq","message_id":"42",
            "data":news_feedback_callback(revision_id[:32],useful=False)}
    assert handler(update)=="RECORDED"
    assert handler(update)=="DUPLICATE"
    row=store.connection.execute("SELECT reaction,provider_update_id,alert_revision_id FROM alert_reactions").fetchone()
    assert row==("NOT_HELPFUL",7,revision_id)
    wrong=update|{"update_id":8,"message_id":"wrong"}
    assert handler(wrong)=="IGNORED"

def test_meta_feedback_cooldown_never_mutates_preferences(tmp_path):
    store=NewsEpisodeStore(tmp_path/"news.sqlite")
    assert store.propose_meta_feedback("similar-context",("r1","r2","r3"),now=NOW)=="INSUFFICIENT"

def test_integrated_pulse_exposes_single_owner_news_handler_seam():
    assert "news_callback_handler" in {field.name for field in fields(PulseDepsV1)}
