import json
from copy import deepcopy
from datetime import timedelta

import pytest
from test_news_decision_engine import NOW, observation

from tokmo.records.news_decision_store import NewsDecisionStore, kst


def pair(store, **changes):
    original = observation("source:1", NOW)
    original.update(
        published_at=kst(NOW),
        first_seen_at=kst(NOW),
        source_id="publisher",
        time_interpretation="EXPLICIT",
    )
    duplicate = dict(
        deepcopy(original), id="source:2", observed_at=kst(NOW + timedelta(minutes=1))
    )
    duplicate.update(changes)
    store.ingest([original, duplicate], NOW + timedelta(hours=1))
    store.mark(["source:1"], "DONE")
    store.mark(["source:2"], "ERROR", "QUOTA_EXCEEDED")
    return original, duplicate


def test_exact_completed_revision_closes_failure_and_keeps_provenance_across_restart(
    tmp_path,
):
    path = tmp_path / "state.db"
    store = NewsDecisionStore(path)
    original, duplicate = pair(store)
    before = store.observation("source:2")
    assert len(store.duplicate_error_candidates(NOW + timedelta(hours=1))) == 1
    assert store.errors() == [("source:2", "QUOTA_EXCEEDED")]  # dry run
    result = store.close_duplicate_errors(NOW + timedelta(hours=1))
    assert result["closed"] == 1 and result["model_calls"] == 0
    assert store.observation("source:2") == before
    assert store.counts() == {"DONE": 1, "CLOSED": 1}
    receipt = store.connection.execute(
        "SELECT reason,details FROM news_decision_input_closures"
    ).fetchone()
    assert receipt[0] == "DUPLICATE_OF_PROCESSED"
    assert json.loads(receipt[1])["matched_input_id"] == "source:1"
    assert (
        store.connection.execute(
            "SELECT error FROM news_decision_inputs WHERE id='source:2'"
        ).fetchone()[0]
        == "QUOTA_EXCEEDED"
    )
    store.close()
    store = NewsDecisionStore(path)
    try:
        assert store.close_duplicate_errors(NOW + timedelta(hours=2))["closed"] == 0
        store.retry_errors()
        store.ingest([duplicate], NOW + timedelta(hours=2))
        assert not store.pending() and store.counts() == {"DONE": 1, "CLOSED": 1}
    finally:
        store.close()


@pytest.mark.parametrize(
    "change",
    [
        {"text": "확정 아님. 조건부 최대 10억 달러"},
        {"published_at": kst(NOW + timedelta(seconds=1))},
        {"publisher": "다른 출처"},
        {"source_id": "other"},
        {"source_kind": "DISCLOSURE"},
        {"first_seen_at": kst(NOW + timedelta(seconds=1))},
        {"time_interpretation": "ASSUMED_KST"},
        {"new_unrecognized_field": "revised"},
        {"url": ""},
        {"text": ""},
    ],
)
def test_changed_source_or_unknown_field_does_not_close(tmp_path, change):
    store = NewsDecisionStore(tmp_path / "state.db")
    try:
        pair(store, **change)
        assert store.close_duplicate_errors(NOW + timedelta(hours=1))["closed"] == 0
        assert len(store.errors()) == 1
    finally:
        store.close()


def test_unprocessed_and_failed_matches_never_prove_completion(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    try:
        pair(store)
        for state in ("READY", "ERROR", "CLOSED"):
            store.mark(["source:1"], state)
            assert not store.duplicate_error_candidates(NOW + timedelta(hours=1))
        store.mark(["source:1"], "DONE")
        assert not store.duplicate_error_candidates(NOW)  # future failed observation
    finally:
        store.close()


def test_receipt_failure_rolls_back_closure(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    try:
        pair(store)
        store.connection.execute(
            "CREATE TRIGGER reject_closure BEFORE INSERT ON news_decision_input_closures BEGIN SELECT RAISE(ABORT,'disk failure'); END"
        )
        with pytest.raises(Exception, match="disk failure"):
            store.close_duplicate_errors(NOW + timedelta(hours=1))
        assert len(store.errors()) == 1
        assert (
            store.connection.execute(
                "SELECT count(*) FROM news_decision_input_closures"
            ).fetchone()[0]
            == 0
        )
    finally:
        store.close()


def test_stale_error_inputs_close_after_72h_without_reanalysis(tmp_path):
    store = NewsDecisionStore(tmp_path / "stale.db")
    store.ingest(
        [observation("old", NOW), observation("fresh", NOW + timedelta(hours=70))],
        NOW + timedelta(hours=71),
    )
    store.mark(["old", "fresh"], "ERROR", "material_change required")
    assert (
        store.close_stale_errors(NOW + timedelta(hours=71))["closed"] == 0
    )  # nothing 72h old yet
    result = store.close_stale_errors(NOW + timedelta(hours=73))
    assert result == dict(
        closed=1, reason="STALE_ERROR", input_ids=["old"], model_calls=0
    )
    assert store.counts() == {"CLOSED": 1, "ERROR": 1}
    receipt = store.connection.execute(
        "SELECT reason,details FROM news_decision_input_closures WHERE input_id='old'"
    ).fetchone()
    assert receipt[0] == "STALE_ERROR" and "material_change required" in receipt[1]
    assert (
        store.observation("old") is not None
    )  # input and error text remain as evidence
    assert (
        store.close_stale_errors(NOW + timedelta(hours=73))["closed"] == 0
    )  # idempotent
    store.close()
