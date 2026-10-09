"""Semantic pre-admission, durable relations/review queue, manual-merge history.

Contract: .ai/plans/2026-08-16_youtube-semantic-dedupe-weekly-sds.md
Verticals 1-5, 10 — paraphrase sighting, distinct+relation, merge review,
replay idempotency, manual merge preserving history, archived replay.
"""

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.youtube_tracking import (
    admit_report_candidates,
    parse_tracking_candidates,
    provenance_from_archive_meta,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import (
    HypothesisIndexKeyV1,
    IndexKeyKind,
    ProducerRole,
    TrackingCandidateV1,
    TriggerKind,
)

NOW = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
NEXT_DAY = NOW + timedelta(days=1)
FIXTURES = Path(__file__).parent / "fixtures" / "youtube"

E, M, F, S = (
    IndexKeyKind.ENTITY,
    IndexKeyKind.METRIC,
    IndexKeyKind.EFFECT,
    IndexKeyKind.SOURCE_EVENT,
)


def key(kind, value):
    return HypothesisIndexKeyV1(kind, value)


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
            key(E, "entity:005980"),
            key(M, "metric:semis:sovereign_flow"),
            key(F, "effect:semis_long_growth"),
        ),
        source_ref="YlVhQO-Fkc8",
        revision="2026-08-13T04:21:51+00:00:d2a78950e33b",
        payload_digest="d" * 64,
        observed_at=NOW,
    )
    fields.update(over)
    return TrackingCandidateV1(**fields)


def make_store(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")


# --- V2: paraphrase becomes a sighting on the kept hypothesis --------------


def test_paraphrased_cross_date_candidate_is_new_sighting_not_new_hypothesis(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)

    paraphrase = make_candidate(
        statement="싱가포르 국부펀드가 삼성전자를 계속 들고 있다는 점은 반도체 장기 성장 베팅 유지 신호다",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
        supporting_evidence=("yt:2026-08-14:01:30",),
    )
    outcome = admit_report_candidates(store, (paraphrase,), now=NEXT_DAY)

    assert outcome.created == 0  # no second hypothesis
    assert outcome.semantic_sightings == 1
    open_rows = store.read_open_hypotheses()
    assert len(open_rows) == 1
    hypothesis_id = open_rows[0].hypothesis_id
    sightings = store.read_hypothesis_sightings(hypothesis_id)
    assert len(sightings) == 2
    assert sightings[1].source_ref == "66fuB_7nofM"
    decisions = store.read_admission_decisions()
    assert [row.classification for row in decisions] == [
        "NO_RELATION",
        "SAME_HYPOTHESIS_NEW_SIGHTING",
    ]
    store.close()


def test_paraphrase_replay_is_idempotent(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    paraphrase = make_candidate(
        statement="재서술 동일 가설 두번째 관찰",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
    )
    admit_report_candidates(store, (paraphrase,), now=NEXT_DAY)
    replay = admit_report_candidates(store, (paraphrase,), now=NEXT_DAY)
    assert replay.created == 0
    assert replay.duplicates == 1
    assert replay.semantic_sightings == 0
    hypothesis_id = store.read_open_hypotheses()[0].hypothesis_id
    assert len(store.read_hypothesis_sightings(hypothesis_id)) == 2
    assert len(store.read_admission_decisions()) == 2  # audit not duplicated
    store.close()


# --- V3: distinct causal claims stay distinct + durable relation -----------


def test_different_causal_claim_creates_distinct_hypothesis_and_relation(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    different_claim = make_candidate(
        statement="싱가포르 국부펀드의 삼성전자 매도는 반도체 조정 경고 신호다",
        trigger_expression="entity:005980:stake_sale_confirmed",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
    )
    outcome = admit_report_candidates(store, (different_claim,), now=NEXT_DAY)
    assert outcome.created == 1
    assert outcome.related_edges == 1
    open_rows = store.read_open_hypotheses()
    assert len(open_rows) == 2
    relations = store.read_hypothesis_relations()
    assert len(relations) == 1
    relation = relations[0]
    assert relation.relation_kind == "RELATED_BUT_DISTINCT"
    assert relation.origin == "ADMISSION"
    assert relation.reason_code == "material_conflict"
    assert "trigger" in relation.conflicts
    store.close()


def test_ambiguous_high_overlap_queues_open_merge_review(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    ambiguous = make_candidate(
        statement="국부펀드 반도체 지분 흐름의 두번째 관점 가설",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
        index_keys=(
            key(E, "entity:005980"),
            key(M, "metric:semis:sovereign_flow_v2"),
            key(F, "effect:semis_long_growth"),
        ),
    )
    outcome = admit_report_candidates(store, (ambiguous,), now=NEXT_DAY)
    assert outcome.created == 1  # preserved as its own hypothesis
    assert outcome.merge_reviews == 1
    assert outcome.related_edges == 0
    reviews = store.read_merge_reviews()
    assert len(reviews) == 1
    review = reviews[0]
    assert review.status == "OPEN"
    assert review.origin == "ADMISSION"
    assert review.reason_code == "high_overlap_ambiguous"
    # both hypotheses remain open — nothing was merged
    assert len(store.read_open_hypotheses()) == 2
    store.close()


# --- V4: replay idempotency of relations/queue/audit ------------------------


def test_relation_and_queue_replay_are_idempotent(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    different_claim = make_candidate(
        statement="매도 관점 가설",
        trigger_expression="entity:005980:stake_sale_confirmed",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
    )
    ambiguous = make_candidate(
        statement="흐름 두번째 관점 가설",
        source_ref="77fuB_8nofM",
        revision="2026-08-15T04:00:00+00:00:abcdef654321",
        payload_digest="f" * 64,
        observed_at=NEXT_DAY + timedelta(days=1),
        next_check_at=NEXT_DAY + timedelta(days=31),
        index_keys=(
            key(E, "entity:005980"),
            key(M, "metric:semis:sovereign_flow_v2"),
            key(F, "effect:semis_long_growth"),
        ),
    )
    outcome = admit_report_candidates(
        store,
        (different_claim, ambiguous),
        now=NEXT_DAY,
    )
    # conservative closure: ambiguous conflicts with BOTH priors (its
    # trigger differs from different_claim's) so it links to each; only the
    # trigger-compatible original pair queues a merge review.
    assert outcome.related_edges == 2
    assert outcome.merge_reviews == 1
    relations = store.read_hypothesis_relations()
    assert len(relations) == 2
    assert all(row.relation_kind == "RELATED_BUT_DISTINCT" for row in relations)
    assert len(store.read_merge_reviews()) == 1
    assert len(store.read_admission_decisions()) == 4
    # replay both candidates: nothing duplicates
    replay = admit_report_candidates(
        store,
        (different_claim, ambiguous),
        now=NEXT_DAY + timedelta(hours=1),
    )
    assert replay.related_edges == 0
    assert replay.merge_reviews == 0
    assert replay.duplicates == 2
    assert len(store.read_hypothesis_relations()) == 2
    assert len(store.read_merge_reviews()) == 1
    # the first replay appends the EXACT_DUPLICATE audit per candidate...
    assert len(store.read_admission_decisions()) == 6
    assert [row.classification for row in store.read_admission_decisions()].count(
        "EXACT_DUPLICATE"
    ) == 2
    # ...and a second replay appends nothing further (audit idempotent).
    admit_report_candidates(
        store,
        (different_claim, ambiguous),
        now=NEXT_DAY + timedelta(hours=2),
    )
    assert len(store.read_admission_decisions()) == 6
    store.close()


# --- V5: approved manual merge preserves history ----------------------------


def test_manual_merge_preserves_sightings_and_relation_review_history(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    different_claim = make_candidate(
        statement="매도 관점 가설",
        trigger_expression="entity:005980:stake_sale_confirmed",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
    )
    ambiguous = make_candidate(
        statement="흐름 두번째 관점 가설",
        source_ref="77fuB_8nofM",
        revision="2026-08-15T04:00:00+00:00:abcdef654321",
        payload_digest="f" * 64,
        observed_at=NEXT_DAY + timedelta(days=1),
        next_check_at=NEXT_DAY + timedelta(days=31),
        index_keys=(
            key(E, "entity:005980"),
            key(M, "metric:semis:sovereign_flow_v2"),
            key(F, "effect:semis_long_growth"),
        ),
    )
    outcome = admit_report_candidates(
        store,
        (different_claim, ambiguous),
        now=NEXT_DAY,
    )
    assert outcome.related_edges == 2
    assert outcome.merge_reviews == 1
    open_rows = store.read_open_hypotheses()
    kept = open_rows[0].hypothesis_id
    merged = open_rows[2].hypothesis_id  # the pair holding the OPEN review
    merged_sightings_before = store.read_hypothesis_sightings(merged)
    merged_provenance_before = store.read_hypothesis(merged).provenance_first

    from tokmo.values.ops_kernel import HypothesisMergeOutcome

    result = store.merge_hypotheses(
        ProducerRole.ANALYSIS,
        kept,
        merged,
        now=NEXT_DAY + timedelta(days=2),
    )
    assert result is HypothesisMergeOutcome.MERGED

    # sightings/provenance of BOTH hypotheses survive the merge
    assert store.read_hypothesis_sightings(merged) == merged_sightings_before
    assert len(store.read_hypothesis_sightings(kept)) == 1
    archived = store.read_hypothesis(merged)
    assert archived.provenance_first == merged_provenance_before
    assert archived.superseded_by == kept
    # relation history is preserved verbatim
    relations = store.read_hypothesis_relations()
    assert len(relations) == 2
    # the OPEN review involving the merged hypothesis is superseded, not
    # lost: the resolution is a durable event, the queue has nothing
    # actionable left for that pair.
    history = store.read_merge_review_history(kept, merged)
    assert [row.status for row in history] == ["SUPERSEDED"]
    assert history[0].resolved_at is not None
    assert store.read_merge_reviews() == ()
    store.close()


# --- V10: archived fixture replay behavior stays exact ----------------------


def test_archived_fixture_replay_counts_unchanged_with_semantic_admission(tmp_path):
    meta = json.loads((FIXTURES / "2026-08-13_excerpt.meta.json").read_text())
    provenance = provenance_from_archive_meta(meta)
    report = """=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: 싱가포르 테마색 국부펀드의 삼성전자·SK하이닉스 편입은 국내 반도체 장기 성장 가설의 지속 신호다
TRIGGER: EVENT_OCCUR | entity:005980:sovereign_fund_stake_change OR entity:000660:sovereign_fund_stake_change
SUPPORT: yt:2026-08-13:00:14;yt:2026-08-13:00:21
COUNTER: metric:005980:net_foreign_outflow_10d
INVALIDATE: disclosure:005980:fund_exit
NEXT_CHECK: 2026-09-13T00:00:00Z
TOUCH: PERSONAL
KEYS: entity:005980, entity:000660, metric:semis:sovereign_flow
=== TRACKING_CANDIDATE END ==="""
    candidates = parse_tracking_candidates(
        report,
        source_ref=provenance.source_ref,
        revision=provenance.revision,
        observed_at=NOW,
    )
    store = make_store(tmp_path)
    outcome = admit_report_candidates(store, candidates, now=NOW)
    assert (outcome.created, outcome.duplicates, outcome.failures) == (1, 0, 0)
    replay = admit_report_candidates(store, candidates, now=NOW)
    assert (replay.created, replay.duplicates) == (0, 1)
    assert replay.semantic_sightings == 0
    hypothesis_id = store.read_open_hypotheses()[0].hypothesis_id
    assert len(store.read_hypothesis_sightings(hypothesis_id)) == 1
    assert len(store.read_hypothesis_relations()) == 0
    assert len(store.read_merge_reviews()) == 0
    store.close()


# --- Fable repair M1: exact replay must win over structural twins ----------


def test_exact_replay_of_later_structural_twin_is_duplicate_not_misattributed(tmp_path):
    """Raw-store two structural twins (identical profiles, distinct
    statements); an exact replay of the lexicographically later twin must be
    DUPLICATE against that twin — never a semantic sighting on the earlier
    one."""
    from tokmo.values.ops_kernel import ProducerRole

    store = make_store(tmp_path)
    base = make_candidate(statement="aaaa 트윈")
    twin = make_candidate(
        statement="zzzz 트윈",
        source_ref="99fuB_0nofM",
        revision="r9",
        payload_digest="9" * 64,
    )
    _, base_id = store.upsert_hypothesis(ProducerRole.ANALYSIS, base, now=NOW)
    _, twin_id = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        twin,
        now=NOW,
    )
    ids = sorted([base_id, twin_id])
    # sanity: both twins exist and the replayed twin is the later one
    assert len(ids) == 2 and twin_id > base_id
    earlier_sightings = len(store.read_hypothesis_sightings(base_id))
    later_sightings = len(store.read_hypothesis_sightings(twin_id))

    replay = admit_report_candidates(store, (twin,), now=NOW)

    assert replay.duplicates == 1
    assert replay.semantic_sightings == 0
    assert replay.created == 0
    # no sighting growth on either twin
    assert len(store.read_hypothesis_sightings(base_id)) == earlier_sightings
    assert len(store.read_hypothesis_sightings(twin_id)) == later_sightings
    store.close()


# --- Fable repair minor 3: semantic sighting keeps supporting evidence ------


def test_semantic_sighting_preserves_candidate_supporting_evidence(tmp_path):
    store = make_store(tmp_path)
    admit_report_candidates(store, (make_candidate(),), now=NOW)
    paraphrase = make_candidate(
        statement="재서술 가증 증거 보존 확인",
        source_ref="66fuB_7nofM",
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        payload_digest="e" * 64,
        observed_at=NEXT_DAY,
        next_check_at=NEXT_DAY + timedelta(days=30),
        supporting_evidence=("yt:2026-08-14:01:30", "yt:2026-08-14:02:00"),
    )
    outcome = admit_report_candidates(store, (paraphrase,), now=NEXT_DAY)
    assert outcome.semantic_sightings == 1
    hypothesis_id = store.read_open_hypotheses()[0].hypothesis_id
    sightings = store.read_hypothesis_sightings(hypothesis_id)
    assert sightings[-1].evidence_delta == (
        "yt:2026-08-14:01:30",
        "yt:2026-08-14:02:00",
    )
    store.close()
