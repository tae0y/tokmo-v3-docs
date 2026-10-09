import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.programs.news_topic_discovery import (
    discover_topics,
    persist_discovery_result,
)
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.news_discovery import NewsDocument
from tokmo.values.news_event import Evidence, NewsClaimStatusV1

NOW = datetime(2026, 8, 21, tzinfo=UTC)


def test_discovery_and_episode_evidence_tables_are_real_producer_sots(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    docs = tuple(
        NewsDocument(
            f"d{i}",
            "novelburst semiconductor",
            f"story-{i}",
            f"source-{i}",
            observed_at=NOW.isoformat(),
            publisher_name=("연합뉴스", "JTBC", "BBC")[i % 3],
        )
        for i in range(3)
    )
    result = discover_topics(docs, holdings=set())
    run_id = persist_discovery_result(
        store, result, window_from=NOW, window_to=NOW, documents=docs, now=NOW
    )
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_runs WHERE run_id=?", (run_id,)
    ).fetchone() == (1,)
    assert (
        store.connection.execute(
            "SELECT count(*) FROM discovery_candidates WHERE run_id=?", (run_id,)
        ).fetchone()[0]
        >= 1
    )
    # I2: identical corpus persists under the next slot without a PK clash,
    # and candidate evidence is the versioned observed_at-preserving shape.
    next_slot = NOW + timedelta(seconds=1800)
    second = persist_discovery_result(
        store,
        result,
        window_from=NOW,
        window_to=next_slot,
        documents=docs,
        now=next_slot,
    )
    assert second != run_id
    assert store.connection.execute(
        "SELECT count(*) FROM discovery_runs"
    ).fetchone() == (2,)
    payload = json.loads(
        store.connection.execute(
            "SELECT evidence_story_keys_json FROM discovery_candidates WHERE run_id=?",
            (run_id,),
        ).fetchone()[0]
    )
    # v:2 (SDS 2026-09-01 W3) keeps the v:1-compatible fields alongside the
    # per-doc provenance list.
    assert payload["v"] == 2
    assert payload["document_ids"] and payload["latest_observed_at"] == NOW.isoformat()
    assert isinstance(payload["docs"], list) and payload["docs"]
    episode = store.upsert(
        "canonical-evidence",
        (
            Evidence("ev-a", NewsClaimStatusV1.CONFIRMED_FACT.value, "g1"),
            Evidence("ev-b", NewsClaimStatusV1.CORRECTED.value, "g1"),
        ),
        NOW,
        entities=("ent:novelburst",),
    )
    assert store.connection.execute(
        "SELECT count(*) FROM episode_evidence WHERE episode_id=?",
        (episode.episode_id,),
    ).fetchone() == (2,)
    assert store.connection.execute(
        "SELECT count(*) FROM episode_entities WHERE episode_id=?",
        (episode.episode_id,),
    ).fetchone() == (1,)


def test_sds_check_constraints_reject_invalid_tokens(tmp_path):
    store = NewsEpisodeStore(tmp_path / "news.sqlite")
    c = store.connection
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "bad",
                "k",
                None,
                None,
                "t",
                "BAD_DOMAIN",
                "BOGUS",
                "NOPE",
                0,
                1,
                NOW.isoformat(),
                None,
                None,
                None,
                None,
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO classifier_calls(call_id,classification_identity,attempt_no,purpose,episode_id,provider,model_id,model_contract_digest,prompt_policy_digest,input_evidence_digest,output_schema_id,state,exit,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "c",
                "a" * 64,
                1,
                "EVENT_CLASS",
                None,
                "p",
                "m",
                "a" * 64,
                "a" * 64,
                "a" * 64,
                "news-classifier-output-v1",
                "DEFERRED",
                "SCHEMA_INVALID",
                NOW.isoformat(),
            ),
        )
    episode = store.upsert("valid-parent", (), NOW)
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO scenario_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "node",
                episode.episode_id,
                None,
                0,
                "FIXED_THREE",
                "title",
                "[]",
                "[]",
                "",
                0,
                "PENDING",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO meta_feedback_proposals VALUES(?,?,?,?,?,?)",
            (
                "proposal",
                "context",
                "[]",
                "AUTO_MUTATED",
                NOW.isoformat(),
                NOW.isoformat(),
            ),
        )
    with pytest.raises(sqlite3.IntegrityError):
        c.execute(
            "INSERT INTO classifier_calls(call_id,classification_identity,attempt_no,purpose,episode_id,provider,model_id,model_contract_digest,prompt_policy_digest,input_evidence_digest,output_schema_id,state,requested_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "bad-route",
                "b" * 64,
                1,
                "EVENT_CLASS",
                episode.episode_id,
                "Provider Upper",
                "latest model",
                "b" * 64,
                "b" * 64,
                "b" * 64,
                "news-classifier-output-v1",
                "PLANNED",
                NOW.isoformat(),
            ),
        )
