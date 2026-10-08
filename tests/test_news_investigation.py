from datetime import datetime, timezone

import pytest

from tokmo.programs.news_investigation import investigate
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NewsClassifierRouteError,
    read_classifier_model,
    validate_classifier_route,
)
from tokmo.values.news_event import Evidence, EpisodeState, NewsEpisodeTransitionError

NOW = datetime(2026, 8, 21, tzinfo=timezone.utc)


def test_episode_merge_conflict_retraction_and_transition(tmp_path):
    store = NewsEpisodeStore(tmp_path / "episodes.sqlite")
    first = investigate("moderna-trial", (Evidence("e1", "CONFIRMED_FACT", "g1"),), store=store, now=NOW)
    merged = investigate("moderna-trial", (Evidence("e2", "CORRECTED", "g1"),), store=store, now=NOW)
    assert first.episode_id == merged.episode_id
    assert merged.state is EpisodeState.CONFLICTED
    corrected = investigate("moderna-trial", (Evidence("e3", "RETRACTED", "g1"),), store=store, now=NOW)
    assert corrected.revision == 3
    assert store.revisions(corrected.episode_id)[0].revision == 1
    with pytest.raises(NewsEpisodeTransitionError):
        store.transition(corrected.episode_id, EpisodeState.RESOLVED, EpisodeState.DISCOVERED, NOW)


def test_classifier_single_config_exact_route_no_fallback(tmp_path):
    assert read_classifier_model(tmp_path / "missing.json") == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    configured = tmp_path / "news_classifier_model.json"
    configured.write_text('{"model_id":"approved-model"}\n')
    assert read_classifier_model(configured, approved={"approved-model":"provider-x"}) == "approved-model"
    with pytest.raises(NewsClassifierRouteError):
        validate_classifier_route("approved-model", "other-model")
    configured.write_text('{"model_id":"bad"}')
    with pytest.raises(NewsClassifierRouteError):
        read_classifier_model(configured, approved={"approved-model"})


def test_classify_event_guarded_v2_model_label_never_promotes():
    """SDS 2026-09-01 §E.2 m-4: the old ``classifier_event_class ==
    'CORE_BREAKING'`` promotion branch is gone — a model label alone can
    never yield CORE_BREAKING on the v2 judgment path."""
    from tokmo.programs.news_investigation import classify_event_guarded
    assert classify_event_guarded(
        direct_holding=False, official_primary=False, irreversible=False,
        single_source_claim=False,
        classifier_event_class="CORE_BREAKING") == "ORDINARY"
    assert classify_event_guarded(
        direct_holding=True, official_primary=False, irreversible=False,
        single_source_claim=False,
        classifier_event_class="CORE_BREAKING") == "ORDINARY"


def test_classify_event_guarded_deterministic_rules_hold():
    from tokmo.programs.news_investigation import classify_event_guarded
    # direct holding ∧ official primary ∧ irreversible → CORE (W5 revived)
    assert classify_event_guarded(
        direct_holding=True, official_primary=True, irreversible=True,
        single_source_claim=False) == "CORE_BREAKING"
    # systemic ∧ high propagation → CORE
    assert classify_event_guarded(
        direct_holding=False, official_primary=False, irreversible=False,
        single_source_claim=False, systemic_transition=True,
        propagation_high=True) == "CORE_BREAKING"
    # everything else stays ORDINARY; deferral passes through
    assert classify_event_guarded(
        direct_holding=True, official_primary=True, irreversible=False,
        single_source_claim=False) == "ORDINARY"
    assert classify_event_guarded(
        direct_holding=False, official_primary=False, irreversible=False,
        single_source_claim=True) == "ORDINARY"
    assert classify_event_guarded(
        direct_holding=False, official_primary=False, irreversible=False,
        single_source_claim=False,
        classification_status="DEFERRED") == "CLASSIFICATION_DEFERRED"


def test_classifier_outage_dedupes_and_clears_silently(tmp_path):
    store = NewsEpisodeStore(tmp_path / "episodes.sqlite")
    one = store.record_outage("approved-model", "CONFIG_INVALID", NOW)
    two = store.record_outage("approved-model", "CONFIG_INVALID", NOW)
    assert one.outage_id == two.outage_id
    assert store.open_outages() == (one,)
    store.clear_config_invalid_outages(now=NOW)
    assert store.open_outages() == ()
