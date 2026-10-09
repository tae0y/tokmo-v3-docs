"""YouTube semantic dedupe: structured profile + deterministic decision engine.

Contract: .ai/plans/2026-08-16_youtube-semantic-dedupe-weekly-sds.md
Verticals 1-3 (values/engine level) — profiles derived only from fields
already present on candidates/hypotheses; deterministic conservative
classification with typed reason codes and compared fields.
"""

from datetime import UTC, datetime, timedelta

from tokmo.programs.youtube_semantic import classify_semantic_profiles
from tokmo.values.ops_kernel import (
    HypothesisIndexKeyV1,
    HypothesisState,
    HypothesisV1,
    IndexKeyKind,
    SemanticClassification,
    SemanticProfileV1,
    TrackingCandidateV1,
    TriggerKind,
)

NOW = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)


def key(kind, value):
    return HypothesisIndexKeyV1(kind, value)


ENTITY = IndexKeyKind.ENTITY
METRIC = IndexKeyKind.METRIC
EFFECT = IndexKeyKind.EFFECT
SOURCE = IndexKeyKind.SOURCE_EVENT


def make_candidate(**over):
    fields = dict(
        statement="싱가포르 국부펀드의 삼성전자 지분 보유는 반도체 장기 성장 베팅의 지속 신호다",
        trigger_kind=TriggerKind.EVENT_OCCUR,
        trigger_expression="entity:005980:stake_change OR entity:000660:stake_change",
        supporting_evidence=("yt:2026-08-13:00:14",),
        counter_evidence=(),
        invalidation_conditions=("metric:005980:fund_exit",),
        next_check_at=NOW + timedelta(days=30),
        portfolio_touch="PERSONAL",
        index_keys=(
            key(ENTITY, "entity:005980"),
            key(METRIC, "metric:semis:sovereign_flow"),
            key(EFFECT, "effect:semis_long_growth"),
        ),
        source_ref="YlVhQO-Fkc8",
        revision="2026-08-13T04:21:51+00:00:d2a78950e33b",
        payload_digest="d" * 64,
        observed_at=NOW,
    )
    fields.update(over)
    return TrackingCandidateV1(**fields)


HYPOTHESIS_INDEX_KEYS = (
    key(ENTITY, "entity:005980"),
    key(METRIC, "metric:semis:sovereign_flow"),
    key(EFFECT, "effect:semis_long_growth"),
)


def hyp_profile(hypothesis, index_keys=HYPOTHESIS_INDEX_KEYS):
    return SemanticProfileV1.from_hypothesis(hypothesis, index_keys=index_keys)


def make_hypothesis(**over):
    fields = dict(
        hypothesis_id="hypothesis:" + "a" * 24,
        state=HypothesisState.CANDIDATE,
        statement="다른 날 다른 표현의 동일 구조 가설 서술",
        trigger_kind=TriggerKind.EVENT_OCCUR,
        trigger_expression="entity:005980:stake_change OR entity:000660:stake_change",
        supporting_evidence=("yt:2026-08-13:00:14",),
        counter_evidence=(),
        invalidation_conditions=("metric:005980:fund_exit",),
        next_check_at=NOW + timedelta(days=30),
        portfolio_touch="PERSONAL",
        origin="youtube:12man",
        provenance_first="YlVhQO-Fkc8",
        superseded_by=None,
        created_at=NOW,
        updated_at=NOW,
    )
    fields.update(over)
    return HypothesisV1(**fields)


# --- profile construction ---------------------------------------------------


def test_profile_partitions_keys_and_normalizes_trigger_order():
    profile = SemanticProfileV1.from_candidate(make_candidate())
    assert profile.entities == frozenset({"entity:005980"})
    assert profile.metrics == frozenset({"metric:semis:sovereign_flow"})
    assert profile.effects == frozenset({"effect:semis_long_growth"})
    assert profile.source_events == frozenset()
    # OR-operand order is normalized: reversed paraphrase is the same trigger
    assert profile.trigger_norm == (
        "entity:000660:stake_change OR entity:005980:stake_change"
    )
    assert profile.invalidation == ("metric:005980:fund_exit",)
    assert profile.portfolio_touch == "PERSONAL"


def test_profile_from_hypothesis_matches_candidate_profile_of_same_fields():
    candidate = make_candidate()
    hypothesis = make_hypothesis(
        trigger_kind=candidate.trigger_kind,
        trigger_expression=candidate.trigger_expression,
        invalidation_conditions=candidate.invalidation_conditions,
        next_check_at=candidate.next_check_at,
        portfolio_touch=candidate.portfolio_touch,
        created_at=candidate.observed_at,
        updated_at=candidate.observed_at,
    )
    assert hyp_profile(hypothesis) == SemanticProfileV1.from_candidate(
        candidate,
    )


def test_time_horizon_buckets_are_bounded_and_deterministic():
    base = dict(
        trigger_expression="entity:005980:x",
        index_keys=(key(ENTITY, "entity:005980"),),
    )
    none = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=None,
            **base,
        )
    )
    short = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=NOW + timedelta(days=7),
            **base,
        )
    )
    medium = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=NOW + timedelta(days=30),
            **base,
        )
    )
    long_ = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=NOW + timedelta(days=90),
            **base,
        )
    )
    indefinite = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=NOW + timedelta(days=400),
            **base,
        )
    )
    assert (
        none.horizon,
        short.horizon,
        medium.horizon,
        long_.horizon,
        indefinite.horizon,
    ) == (
        "none",
        "short",
        "medium",
        "long",
        "indefinite",
    )
    # small drift in the anchor cannot flip the bucket
    drifted = SemanticProfileV1.from_candidate(
        make_candidate(
            next_check_at=NOW + timedelta(days=30) + timedelta(minutes=90),
            **base,
        )
    )
    assert drifted.horizon == medium.horizon


# --- deterministic classification -------------------------------------------


def test_complete_structural_equality_is_same_hypothesis_new_sighting():
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(make_candidate()),
        hyp_profile(make_hypothesis()),
    )
    assert decision.classification is (
        SemanticClassification.SAME_HYPOTHESIS_NEW_SIGHTING
    )
    assert decision.reason_code == "structural_equality_new_source"
    assert decision.conflicts == ()


def test_canonical_identity_flag_short_circuits_to_exact_duplicate():
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(make_candidate()),
        hyp_profile(make_hypothesis()),
        same_canonical_identity=True,
    )
    assert decision.classification is SemanticClassification.EXACT_DUPLICATE
    assert decision.reason_code == "canonical_identity_replay"


def test_material_trigger_conflict_preserves_distinct_with_reason():
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                trigger_expression="entity:005980:other_signal",
                index_keys=(
                    key(ENTITY, "entity:005980"),
                    key(METRIC, "metric:semis:sovereign_flow"),
                    key(EFFECT, "effect:semis_long_growth"),
                ),
            )
        ),
        hyp_profile(make_hypothesis()),
    )
    assert decision.classification is SemanticClassification.RELATED_BUT_DISTINCT
    assert decision.reason_code == "material_conflict"
    assert "trigger" in decision.conflicts


def test_invalidation_or_horizon_or_touch_conflicts_are_material():
    base_profile = SemanticProfileV1.from_candidate(make_candidate())
    for over in (
        {"invalidation_conditions": ("metric:005980:other_exit",)},
        {"next_check_at": NOW + timedelta(days=200)},
        {"portfolio_touch": "PENSION"},
    ):
        decision = classify_semantic_profiles(
            SemanticProfileV1.from_candidate(make_candidate(**over)),
            base_profile,
        )
        assert decision.classification is SemanticClassification.RELATED_BUT_DISTINCT
        assert decision.reason_code == "material_conflict"


def test_high_overlap_without_conflict_queues_merge_review():
    # metric sets differ by one element; everything material is equal and the
    # Jaccard overlap of entity/metric/effect keys is >= 1/2
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                index_keys=(
                    key(ENTITY, "entity:005980"),
                    key(METRIC, "metric:semis:sovereign_flow_v2"),
                    key(EFFECT, "effect:semis_long_growth"),
                ),
            )
        ),
        hyp_profile(make_hypothesis()),
    )
    assert decision.classification is SemanticClassification.MERGE_REVIEW
    assert decision.reason_code == "high_overlap_ambiguous"


def test_partial_overlap_without_conflict_is_related_but_distinct():
    # 1 shared key of a 7-key union (~0.14): below the high-overlap band,
    # above zero, no material conflict -> preserved as distinct.
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                index_keys=(
                    key(ENTITY, "entity:005980"),
                    key(ENTITY, "entity:000660"),
                    key(ENTITY, "entity:035720"),
                    key(ENTITY, "entity:068270"),
                    key(ENTITY, "entity:207940"),
                ),
            )
        ),
        hyp_profile(make_hypothesis()),
    )
    assert decision.classification is SemanticClassification.RELATED_BUT_DISTINCT
    assert decision.reason_code == "partial_overlap_distinct"


def test_no_meaningful_overlap_creates_with_no_relation():
    # Disjoint key sets but NO material conflict (same trigger/invalidation/
    # horizon/touch): zero overlap only, so the pair is simply unrelated.
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                index_keys=(key(ENTITY, "entity:035720"),),
            )
        ),
        hyp_profile(make_hypothesis()),
    )
    assert decision.classification is SemanticClassification.NO_RELATION


def test_classifier_hint_cannot_override_material_conflict_guard():
    from tokmo.values.ops_kernel import ClassifierHintV1

    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                trigger_expression="entity:005980:other_signal",
                index_keys=(
                    key(ENTITY, "entity:005980"),
                    key(METRIC, "metric:semis:sovereign_flow"),
                    key(EFFECT, "effect:semis_long_growth"),
                ),
            )
        ),
        hyp_profile(make_hypothesis()),
        hint=ClassifierHintV1(asserts_high_overlap=True),
    )
    assert decision.classification is SemanticClassification.RELATED_BUT_DISTINCT
    assert decision.reason_code == "material_conflict"


def test_classifier_hint_may_only_escalate_no_relation_to_merge_review():
    from tokmo.values.ops_kernel import ClassifierHintV1

    decision = classify_semantic_profiles(
        SemanticProfileV1.from_candidate(
            make_candidate(
                index_keys=(key(ENTITY, "entity:035720"),),
            )
        ),
        hyp_profile(make_hypothesis()),
        hint=ClassifierHintV1(asserts_high_overlap=True),
    )
    assert decision.classification is SemanticClassification.MERGE_REVIEW
    assert decision.reason_code == "classifier_overlap_hint"
