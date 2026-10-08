import json
import os
from datetime import datetime, timezone

import pytest

from tokmo.records.news_episode_store import (NewsEpisodeSchemaError,
    NewsEpisodeStore,NewsEpisodeStoreError)
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID, NewsClassifierRouteError,
    read_classifier_model, validate_classifier_route,
)
from tokmo.values.news_event import EpisodeState, NewsEpisodeTransitionError
from tokmo.programs.news_investigation import run_news_investigation

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)
HEX = "a" * 64


def test_private_strict_full_schema_digest_and_transition_audit(tmp_path):
    path = tmp_path / "news.sqlite"
    store = NewsEpisodeStore(path)
    expected = {"schema_meta", "discovery_runs", "discovery_candidates", "episodes",
                "episode_entities", "episode_evidence", "episode_transitions",
                "scenario_nodes", "alert_revisions", "alert_reactions",
                "meta_feedback_proposals", "morning_hold_queue", "classifier_calls",
                "classifier_outages"}
    table_list = {row[1]: row[5] for row in store.connection.execute("PRAGMA table_list")}
    assert expected <= table_list.keys()
    assert all(table_list[name] == 1 for name in expected)
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    assert store.connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert store.connection.execute("PRAGMA busy_timeout").fetchone()[0] == 250

    episode = store.upsert("transition-fixture", (), NOW)
    store.transition(episode.episode_id, EpisodeState.INVESTIGATING, EpisodeState.ACTIVE, NOW)
    assert store.connection.execute("SELECT state FROM episodes WHERE episode_id=?", (episode.episode_id,)).fetchone() == ("ACTIVE",)
    assert store.connection.execute("SELECT count(*) FROM episode_transitions WHERE episode_id=?", (episode.episode_id,)).fetchone() == (1,)
    with pytest.raises(NewsEpisodeTransitionError):
        store.transition(episode.episode_id, EpisodeState.ACTIVE, EpisodeState.DISCOVERED, NOW)
    store.close()
    NewsEpisodeStore(path).close()

    import sqlite3
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET ddl_sha256=?", ("0" * 64,))
    connection.commit(); connection.close()
    with pytest.raises(NewsEpisodeSchemaError):
        NewsEpisodeStore(path)


def test_classifier_config_route_lifecycle_replay_and_outage_recurrence(tmp_path):
    catalog = {DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "openai-codex", "approved-model": "provider-x"}
    route = validate_classifier_route(DEFAULT_NEWS_CLASSIFIER_MODEL_ID, catalog)
    assert (route.provider, route.model_id) == ("openai-codex", DEFAULT_NEWS_CLASSIFIER_MODEL_ID)
    config = tmp_path / "model.json"
    config.write_text(json.dumps({"model_id": "approved-model"}))
    assert read_classifier_model(config, approved=catalog) == "approved-model"
    for bad in ("GPT-5.3-Codex-Spark", " gpt-5.3-codex-spark", "latest", "unknown"):
        config.write_text(json.dumps({"model_id": bad}))
        with pytest.raises(NewsClassifierRouteError):
            read_classifier_model(config, approved=catalog)

    store = NewsEpisodeStore(tmp_path / "calls.sqlite")
    call = {"call_id": "ncl-1", "classification_identity": HEX, "attempt_no": 1,
            "purpose": "EVENT_CLASS", "episode_id": store.upsert("call-episode", (), NOW).episode_id,
            "provider": "openai-codex", "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
            "model_contract_digest": HEX, "prompt_policy_digest": HEX,
            "input_evidence_digest": HEX, "output_schema_id": "news-classifier-output-v1"}
    assert store.plan_classifier_call(call, now=NOW).value == "CREATED"
    store.start_classifier_call("ncl-1", now=NOW)
    store.settle_classifier_call("ncl-1", "OK", '{"event_class":"ORDINARY"}', now=NOW)
    assert store.find_settled_classification(HEX)["call_id"] == "ncl-1"
    assert store.plan_classifier_call(call, now=NOW).value == "DUPLICATE"

    first = store.record_outage("approved-model", "QUOTA_EXCEEDED", NOW)
    store.clear_open_outages("ncl-1", now=NOW)
    second = store.record_outage("approved-model", "QUOTA_EXCEEDED", NOW.replace(second=1))
    store.clear_open_outages("ncl-1", now=NOW.replace(second=2))
    assert first.outage_id != second.outage_id
    assert store.connection.execute("SELECT count(*) FROM classifier_outages WHERE status='CLEARED'").fetchone() == (2,)


def test_classifier_execution_provenance_replay_and_no_fallback(tmp_path):
    store=NewsEpisodeStore(tmp_path/"run.sqlite"); episode=store.upsert("run",(),NOW)
    calls=[]
    def classifier(request):
        calls.append(request.model_id)
        return json.dumps({"provider":request.provider,"model_id":request.model_id,
                           "event_class":"ORDINARY","citations":["ev1"]}).encode()
    kwargs=dict(store=store,episode_id=episode.episode_id,purpose="EVENT_CLASS",
        evidence={"ev1":"public news"},prompt_policy_digest=HEX,model_contract_digest=HEX,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID:"openai-codex"},classifier_call=classifier,now=NOW)
    first=run_news_investigation(**kwargs); second=run_news_investigation(**kwargs)
    assert first==second and calls==[DEFAULT_NEWS_CLASSIFIER_MODEL_ID]
    assert store.find_settled_classification(first["classification_identity"])["provider"]=="openai-codex"

def test_locked_and_corrupt_store_raise_typed_errors(tmp_path):
    import sqlite3
    path=tmp_path/"locked.sqlite";owner=NewsEpisodeStore(path);owner.close()
    blocker=sqlite3.connect(path,timeout=.1)
    blocker.execute("PRAGMA journal_mode=DELETE");blocker.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(NewsEpisodeStoreError):NewsEpisodeStore(path)
    finally:
        blocker.rollback();blocker.close()
    corrupt=tmp_path/"corrupt.sqlite";corrupt.write_bytes(b"not-a-sqlite-database")
    with pytest.raises((NewsEpisodeStoreError,NewsEpisodeSchemaError)):NewsEpisodeStore(corrupt)

def test_chmod_failure_closes_connection_and_raises_typed_store_error(tmp_path,monkeypatch):
    import sqlite3
    import tokmo.records.news_episode_store as module
    path=tmp_path/"chmod.sqlite";connections=[]
    real_connect=module.sqlite3.connect;real_chmod=module.os.chmod
    def connect(*args,**kwargs):
        connection=real_connect(*args,**kwargs);connections.append(connection);return connection
    def chmod(target,mode):
        if target==path:raise OSError("permission denied")
        return real_chmod(target,mode)
    monkeypatch.setattr(module.sqlite3,"connect",connect)
    monkeypatch.setattr(module.os,"chmod",chmod)
    with pytest.raises(NewsEpisodeStoreError,match="store initialization failed"):
        NewsEpisodeStore(path)
    assert len(connections)==1
    with pytest.raises(sqlite3.ProgrammingError,match="closed"):
        connections[0].execute("SELECT 1")
