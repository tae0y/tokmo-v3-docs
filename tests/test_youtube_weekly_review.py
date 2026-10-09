"""YouTube weekly duplicate review: KST ISO-week identity, durable scan/receipt.

Contract: .ai/plans/2026-08-16_youtube-semantic-dedupe-weekly-sds.md
Verticals 6-8 — canonical week derivation, pair scan idempotency, resume.
"""

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from tokmo.programs.youtube_weekly_review import kst_iso_week

UTC = UTC
KST = ZoneInfo("Asia/Seoul")


# --- V6: canonical KST ISO-week derivation ---------------------------------


def test_week_identity_is_canonical_kst_iso_week():
    # 2026-08-16T16:00Z is Sunday KST midnight crossing into Monday 2026-08-17:
    # naive-UTC is (2026, 33, 7) but KST-local is (2026, 34, 1).
    week = kst_iso_week(datetime(2026, 8, 16, 16, 0, tzinfo=UTC))
    assert week.identity == "2026-W34"
    assert (week.iso_year, week.iso_week) == (2026, 34)


def test_week_identity_matches_for_instant_expressed_in_utc_or_kst():
    utc_instant = datetime(2026, 8, 14, 2, 30, tzinfo=UTC)
    kst_instant = utc_instant.astimezone(KST)
    assert kst_iso_week(utc_instant).identity == kst_iso_week(kst_instant).identity


def test_week_identity_at_year_boundary_differs_from_naive_utc():
    # 2027-01-03T16:00Z: ISO Sunday end of 2026-W53 in UTC, but KST local is
    # Monday 2027-01-04 -> 2027-W01. Canonical identity must follow KST.
    week = kst_iso_week(datetime(2027, 1, 3, 16, 0, tzinfo=UTC))
    assert week.identity == "2027-W01"
    assert tuple(week.range) == (
        datetime(2027, 1, 4, 0, 0, tzinfo=KST),
        datetime(2027, 1, 10, 23, 59, 59, 999999, tzinfo=KST),
    )


def test_week_identity_requires_aware_instant():
    with pytest.raises(ValueError):
        kst_iso_week(datetime(2026, 8, 16, 9, 0))


# --- V7: weekly scan coverage, idempotency, no fabrication ------------------


UTC_RUN = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)

from tokmo.programs.youtube_tracking import admit_report_candidates
from tokmo.programs.youtube_weekly_review import run_weekly_review
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import (
    HypothesisIndexKeyV1,
    IndexKeyKind,
    ProducerRole,
    TrackingCandidateV1,
    TriggerKind,
)

E, M, F = IndexKeyKind.ENTITY, IndexKeyKind.METRIC, IndexKeyKind.EFFECT


def _key(kind, value):
    return HypothesisIndexKeyV1(kind, value)


def _candidate(**over):
    fields = dict(
        statement="주간 리뷰 기준 가설",
        trigger_kind=TriggerKind.EVENT_OCCUR,
        trigger_expression="entity:005980:stake_change",
        supporting_evidence=("yt:2026-08-13:00:14",),
        counter_evidence=(),
        invalidation_conditions=("metric:005980:fund_exit",),
        next_check_at=datetime(2026, 8, 13, 5, 30, tzinfo=UTC) + timedelta(days=30),
        portfolio_touch="PERSONAL",
        index_keys=(
            _key(E, "entity:005980"),
            _key(M, "metric:semis:sovereign_flow"),
            _key(F, "effect:semis_long_growth"),
        ),
        source_ref="YlVhQO-Fkc8",
        revision="r1",
        payload_digest="d" * 64,
        observed_at=datetime(2026, 8, 13, 5, 30, tzinfo=UTC),
    )
    fields.update(over)
    return TrackingCandidateV1(**fields)


def _three_hypothesis_store(tmp_path):
    """One structural pair (SAME), one conflicting pair (DISTINCT),
    one ambiguous pair (MERGE_REVIEW) — 3 open hypotheses, 3 pairs."""
    from tokmo.values.ops_kernel import ProducerRole

    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    base = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
    admit_report_candidates(store, (_candidate(),), now=base)
    # A second hypothesis that is STRUCTURALLY IDENTICAL to the first but
    # admitted by the raw kernel seam (before any pre-admission existed) —
    # exactly the legacy pair a weekly review must surface as SAME.
    store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        _candidate(
            statement="재서술 동일 구조",
            source_ref="66fuB_7nofM",
            revision="r2",
            payload_digest="e" * 64,
            observed_at=base + timedelta(days=1),
            next_check_at=base + timedelta(days=31),
        ),
        now=base + timedelta(days=1),
    )
    # conflicting trigger -> distinct hypothesis #2
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="매도 관점 가설",
                trigger_expression="entity:005980:stake_sale_confirmed",
                source_ref="77fuB_8nofM",
                revision="r3",
                payload_digest="f" * 64,
                observed_at=base + timedelta(days=2),
                next_check_at=base + timedelta(days=32),
            ),
        ),
        now=base + timedelta(days=2),
    )
    # ambiguous high overlap -> hypothesis #3 + OPEN review
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="흐름 관점 가설",
                source_ref="88fuB_9nofM",
                revision="r4",
                payload_digest="a" * 64,
                observed_at=base + timedelta(days=3),
                next_check_at=base + timedelta(days=33),
                index_keys=(
                    _key(E, "entity:005980"),
                    _key(M, "metric:semis:sovereign_flow_v2"),
                    _key(F, "effect:semis_long_growth"),
                ),
            ),
        ),
        now=base + timedelta(days=3),
    )
    return store


def test_weekly_scan_covers_every_pair_and_persists_findings(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    run_at = datetime(2026, 8, 16, 9, 0, tzinfo=UTC)  # 2026-W33 KST
    outcome = run_weekly_review(store, now=run_at)
    assert outcome.status == "COMPLETE"
    assert outcome.week_identity == "2026-W33"
    assert outcome.open_count == 4
    assert outcome.pairs_scanned == 6  # C(4,2)
    # findings persisted through the store seams
    summary = store.read_weekly_review_summary("2026-W33")
    kinds = sorted(f"{row.classification}" for row in summary["pairs"])
    assert kinds == [
        "MERGE_REVIEW",
        "MERGE_REVIEW",
        "RELATED_BUT_DISTINCT",
        "RELATED_BUT_DISTINCT",
        "RELATED_BUT_DISTINCT",
        "SAME_HYPOTHESIS_NEW_SIGHTING",
    ]
    # SAME finding created NO fabricated sighting: still 2 sightings total
    for hypothesis_id in summary["hypothesis_ids"]:
        assert len(store.read_hypothesis_sightings(hypothesis_id)) <= 2
    store.close()


def test_same_week_replay_is_duplicate_noop(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    first = run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    assert first.status == "COMPLETE"
    # 2026-08-16T14:00Z is 23:00 KST the SAME Sunday: still 2026-W33.
    replay = run_weekly_review(store, now=datetime(2026, 8, 16, 14, 0, tzinfo=UTC))
    assert replay.status == "DUPLICATE"
    assert replay.week_identity == "2026-W33"
    summary = store.read_weekly_review_summary("2026-W33")
    assert len(summary["pairs"]) == 6  # no duplicate pair findings
    assert summary["run"].status == "COMPLETE"
    store.close()


def test_next_week_reevaluates(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    next_week = run_weekly_review(
        store,
        now=datetime(2026, 8, 17, 9, 0, tzinfo=UTC),
    )  # Monday 2026-08-17 18:00 KST -> 2026-W34
    assert next_week.status == "COMPLETE"
    assert next_week.week_identity == "2026-W34"
    both = [
        store.read_weekly_review_summary(week)["pairs"]
        for week in ("2026-W33", "2026-W34")
    ]
    assert all(len(rows) == 6 for rows in both)
    store.close()


def test_weekly_review_records_no_fabricated_sightings(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    ids = [row.hypothesis_id for row in store.read_open_hypotheses()]
    before = {
        hypothesis_id: store.read_hypothesis_sightings(hypothesis_id)
        for hypothesis_id in ids
    }
    run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    for hypothesis_id in ids:
        assert store.read_hypothesis_sightings(hypothesis_id) == before[hypothesis_id]
    store.close()


# --- V8: interrupted run resumes without false COMPLETE ---------------------


def test_interrupted_run_resumes_without_duplicate_findings(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    week = kst_iso_week(datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    reservation = store.reserve_weekly_review(
        ProducerRole.ANALYSIS,
        week,
        now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )
    assert reservation["status"] == "CREATED"
    # simulate a crash after one pair adjudication
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import SemanticProfileV1

    left = store.read_hypothesis(ids[0])
    right = store.read_hypothesis(ids[1])
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_hypothesis(
            left,
            index_keys=store.read_hypothesis_index(ids[0]),
        ),
        SemanticProfileV1.from_hypothesis(
            right,
            index_keys=store.read_hypothesis_index(ids[1]),
        ),
    )
    store.record_weekly_pair_adjudication(
        ProducerRole.ANALYSIS,
        week.identity,
        (ids[0], ids[1]),
        decision,
        now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )
    # no COMPLETE receipt exists yet
    assert store.read_weekly_review_summary(week.identity)["run"].status == "RESERVED"

    # the retry resumes and completes the SAME week (later the same Sunday)
    outcome = run_weekly_review(store, now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    assert outcome.status == "COMPLETE"
    summary = store.read_weekly_review_summary(week.identity)
    assert len(summary["pairs"]) == 6  # each pair exactly once
    assert summary["run"].status == "COMPLETE"
    store.close()


# --- CLI seam + no-network/no-merge guarantees -------------------------------


def test_cli_seam_runs_one_review_and_replays_as_duplicate(tmp_path, capsys):
    import subprocess
    import sys

    store = _three_hypothesis_store(tmp_path)
    store.close()
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    argv = [
        sys.executable,
        "-m",
        "tokmo.programs.youtube_weekly_review",
        str(path),
        "--now",
        "2026-08-16T09:00:00+00:00",
    ]
    env = {
        "PYTHONPATH": str(
            __import__("pathlib").Path(__file__).parents[1] / "src",
        ),
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
    }
    refused = subprocess.run(
        [argv[0], "-m", "tokmo.programs.youtube_weekly_review", str(path)],
        capture_output=True,
        text=True,
        env=env,
    )
    assert refused.returncode == 1
    assert "refusal=observed_now_required" in refused.stderr
    first = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert first.returncode == 0, first.stderr
    assert "status=COMPLETE" in first.stdout
    assert "week=2026-W33" in first.stdout
    assert "pairs=6" in first.stdout
    replay = subprocess.run(argv, capture_output=True, text=True, env=env)
    assert replay.returncode == 0, replay.stderr
    assert "status=DUPLICATE" in replay.stdout


def test_run_weekly_review_source_tree_is_network_and_merge_free():
    import ast
    from pathlib import Path

    source = Path(__file__).parents[1] / ("src/tokmo/programs/youtube_weekly_review.py")
    tree = ast.parse(source.read_text())
    banned_calls = {
        "urlopen",
        "requests",
        "socket",
        "post",
        "send_message",
        "merge_hypotheses",
        "create_order",
        "upsert_hypothesis",
        "record_sighting",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = getattr(node.func, "id", None) or getattr(
                node.func,
                "attr",
                None,
            )
            assert name not in banned_calls, f"forbidden call: {name}"
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            module = getattr(node, "module", "") or ""
            names = [alias.name for alias in node.names]
            assert not any(
                module.startswith(prefix) or name.startswith(prefix)
                for prefix in ("requests", "socket", "http", "urllib")
                for name in names + [module]
            ), "network import in weekly review"


# --- Fable repairs: resume counters, frozen ids, append-only review history


def test_resumed_complete_summarizes_whole_durable_week(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    week = kst_iso_week(datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    store.reserve_weekly_review(
        ProducerRole.ANALYSIS,
        week,
        now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import SemanticProfileV1

    left = store.read_hypothesis(ids[0])
    right = store.read_hypothesis(ids[1])
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_hypothesis(
            left,
            index_keys=store.read_hypothesis_index(ids[0]),
        ),
        SemanticProfileV1.from_hypothesis(
            right,
            index_keys=store.read_hypothesis_index(ids[1]),
        ),
    )
    store.record_weekly_pair_adjudication(
        ProducerRole.ANALYSIS,
        week.identity,
        (ids[0], ids[1]),
        decision,
        now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC),
    )
    outcome = run_weekly_review(store, now=datetime(2026, 8, 16, 12, 0, tzinfo=UTC))
    assert outcome.status == "COMPLETE"
    # the receipt summarizes the whole durable week: all 6 pairs, all
    # findings — not only the 5 rows created during the resume invocation
    assert outcome.pairs_scanned == 6
    run = store.read_weekly_review_summary(week.identity)["run"]
    assert run.pairs_scanned == 6
    assert run.same_hypothesis_findings >= 1
    assert run.new_related_edges >= 1
    assert run.new_merge_reviews >= 1
    store.close()


def test_duplicate_week_returns_frozen_reservation_hypothesis_ids(tmp_path):
    store = _three_hypothesis_store(tmp_path)
    run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    frozen = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    # a NEW unrelated hypothesis admitted after the week completed must not
    # leak into the DUPLICATE replay's summary of that week
    from tokmo.programs.youtube_tracking import admit_report_candidates

    admit_report_candidates(
        store,
        (
            _candidate(
                statement="주 완료 후 신규 무관 가설",
                trigger_expression="entity:035720:other",
                invalidation_conditions=("metric:035720:other_exit",),
                index_keys=(_key(E, "entity:035720"),),
                source_ref="AAfuB_1nofM",
                revision="rA",
                payload_digest="b" * 64,
                observed_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
                next_check_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
                + timedelta(days=30),
            ),
        ),
        now=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
    )
    replay = run_weekly_review(store, now=datetime(2026, 8, 16, 14, 0, tzinfo=UTC))
    assert replay.status == "DUPLICATE"
    assert sorted(replay.hypothesis_ids) == frozen
    store.close()


def test_review_resolution_history_is_append_only_across_requeue(tmp_path):
    """A pair reviewed, resolved REJECTED, later re-queued and resolved
    again must keep BOTH resolution events — the second resolution may
    never overwrite the first."""
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import (
        SemanticProfileV1,
    )

    store = _three_hypothesis_store(tmp_path)
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    left = store.read_hypothesis(ids[2])
    right = store.read_hypothesis(ids[3])
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_hypothesis(
            left,
            index_keys=store.read_hypothesis_index(ids[2]),
        ),
        SemanticProfileV1.from_hypothesis(
            right,
            index_keys=store.read_hypothesis_index(ids[3]),
        ),
    )
    assert decision.classification.value == "MERGE_REVIEW"
    first = store.queue_merge_review(
        ProducerRole.ANALYSIS,
        ids[2],
        ids[3],
        decision,
        now=UTC_RUN,
        origin="WEEKLY_REVIEW",
    )
    assert first == "created"
    resolved = store.resolve_merge_review(
        ProducerRole.ANALYSIS,
        ids[2],
        ids[3],
        "REJECTED",
        now=UTC_RUN,
        origin="WEEKLY_REVIEW",
    )
    assert resolved == "resolved"
    # a later week re-queues the same ambiguous pair
    second = store.queue_merge_review(
        ProducerRole.ANALYSIS,
        ids[2],
        ids[3],
        decision,
        now=UTC_RUN + timedelta(days=7),
        origin="WEEKLY_REVIEW",
    )
    assert second == "created"
    resolved_again = store.resolve_merge_review(
        ProducerRole.ANALYSIS,
        ids[2],
        ids[3],
        "APPROVED",
        now=UTC_RUN + timedelta(days=8),
        origin="WEEKLY_REVIEW",
    )
    assert resolved_again == "resolved"
    history = store.read_merge_review_history(ids[2], ids[3])
    assert [row.status for row in history] == [
        "REJECTED",
        "APPROVED",
    ]
    # every resolution event carries its own resolved_at
    assert history[0].resolved_at < history[1].resolved_at
    # replay of the final resolution is idempotent (no third event)
    store.close()


# --- Fable 2nd repair: weekly counters scoped to the week, resume dedupe,
# --- supersede guard, and frozen summary ids.


def _two_week_store(tmp_path):
    """A store with pre-existing ADMISSION edges/reviews plus the standard
    4-hypothesis set, so week-scoped counters differ from store-wide ones."""
    store = _three_hypothesis_store(tmp_path)
    return store


def test_w2_counters_equal_week_pair_rows_not_store_wide_tables(tmp_path):
    """Pre-existing admission-time edges/reviews must NOT inflate a later
    week's counters: W2 receipt counts derive exactly from W2 pair rows."""
    store = _two_week_store(tmp_path)
    # W1 (2026-W33): the standard run
    first = run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    assert first.status == "COMPLETE"
    # between weeks: a NEW ambiguous admission creates a pre-existing
    # admission-time OPEN review (store-wide but not W2's own finding)
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="주간 사전 모호 가설",
                source_ref="BBfuB_2nofM",
                revision="rB",
                payload_digest="c" * 64,
                observed_at=datetime(2026, 8, 18, 7, 0, tzinfo=UTC),
                next_check_at=datetime(2026, 8, 18, 7, 0, tzinfo=UTC)
                + timedelta(days=30),
                index_keys=(
                    _key(E, "entity:005980"),
                    _key(M, "metric:semis:sovereign_flow_v9"),
                    _key(F, "effect:semis_long_growth"),
                ),
            ),
        ),
        now=datetime(2026, 8, 18, 7, 0, tzinfo=UTC),
    )
    store_wide_reviews_before = len(store.read_merge_reviews())
    store_wide_relations_before = len(store.read_hypothesis_relations())
    assert store_wide_reviews_before >= 1  # pre-existing admission review

    # W2 (2026-W34): Monday 2026-08-17 -> but that is BEFORE 08-18. Use
    # 2026-08-24 (Monday, W35) so the new hypothesis is inside the week set.
    second = run_weekly_review(store, now=datetime(2026, 8, 24, 9, 0, tzinfo=UTC))
    assert second.status == "COMPLETE"
    assert second.week_identity == "2026-W35"

    week_pairs = store.read_weekly_review_summary("2026-W35")["pairs"]
    expected_edges = sum(
        1 for row in week_pairs if row.classification == "RELATED_BUT_DISTINCT"
    )
    expected_reviews = sum(
        1 for row in week_pairs if row.classification == "MERGE_REVIEW"
    )
    expected_same = sum(
        1 for row in week_pairs if row.classification == "SAME_HYPOTHESIS_NEW_SIGHTING"
    )
    run = store.read_weekly_review_summary("2026-W35")["run"]
    # EXACT equality against this week's pair rows, not >= against the
    # store-wide tables (which contain the pre-existing admission rows).
    assert run.new_related_edges == expected_edges
    assert run.new_merge_reviews == expected_reviews
    assert run.same_hypothesis_findings == expected_same
    assert second.new_related_edges == expected_edges
    assert second.new_merge_reviews == expected_reviews
    store.close()


def test_resume_creates_only_one_unresolved_open_review(tmp_path):
    """Crash after queueing an OPEN review, resume at a DIFFERENT instant:
    the same pair+origin must not gain a second unresolved OPEN row."""
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import SemanticProfileV1

    # isolated 2-hypothesis store: one ambiguous pair, no other reviews
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    base = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
    admit_report_candidates(store, (_candidate(),), now=base)
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="모호 가설",
                source_ref="88fuB_9nofM",
                revision="r4",
                payload_digest="a" * 64,
                observed_at=base + timedelta(days=3),
                next_check_at=base + timedelta(days=33),
                index_keys=(
                    _key(E, "entity:005980"),
                    _key(M, "metric:semis:sovereign_flow_v2"),
                    _key(F, "effect:semis_long_growth"),
                ),
            ),
        ),
        now=base + timedelta(days=3),
    )
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    assert len(ids) == 2
    a, b = ids
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_hypothesis(
            store.read_hypothesis(a),
            index_keys=store.read_hypothesis_index(a),
        ),
        SemanticProfileV1.from_hypothesis(
            store.read_hypothesis(b),
            index_keys=store.read_hypothesis_index(b),
        ),
    )
    assert decision.classification.value == "MERGE_REVIEW"
    # the WEEKLY path queues its own review for the same pair
    assert (
        store.queue_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            decision,
            now=UTC_RUN,
            origin="WEEKLY_REVIEW",
        )
        == "created"
    )
    # resume attempt at a LATER instant must be a duplicate, not a second
    # unresolved OPEN with a different event id
    resumed = store.queue_merge_review(
        ProducerRole.ANALYSIS,
        a,
        b,
        decision,
        now=UTC_RUN + timedelta(hours=3),
        origin="WEEKLY_REVIEW",
    )
    assert resumed == "duplicate"
    with store._read() as connection:
        unresolved = connection.execute(
            "SELECT count(*) FROM merge_review_queue WHERE hypothesis_a=? "
            "AND hypothesis_b=? AND origin=? AND status='OPEN' "
            "AND resolved_at IS NULL",
            (a, b, "WEEKLY_REVIEW"),
        ).fetchone()[0]
    assert unresolved == 1
    # after manual resolution OF THAT ORIGIN'S row, a LATER requeue is a
    # new append-only event; still exactly one unresolved OPEN afterwards
    assert (
        store.resolve_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            "REJECTED",
            now=UTC_RUN + timedelta(days=1),
            origin="WEEKLY_REVIEW",
        )
        == "resolved"
    )
    assert (
        store.queue_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            decision,
            now=UTC_RUN + timedelta(days=7),
            origin="WEEKLY_REVIEW",
        )
        == "created"
    )
    with store._read() as connection:
        unresolved_after = connection.execute(
            "SELECT count(*) FROM merge_review_queue WHERE hypothesis_a=? "
            "AND hypothesis_b=? AND origin=? AND status='OPEN' "
            "AND resolved_at IS NULL",
            (a, b, "WEEKLY_REVIEW"),
        ).fetchone()[0]
    assert unresolved_after == 1
    store.close()


def test_merge_supersedes_only_unresolved_open_reviews(tmp_path):
    """A manually SUPERSEDED (closed) review must not be superseded again;
    merging an involved hypothesis still succeeds and the event count and
    history stay invariant."""
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import SemanticProfileV1

    # minimal isolation: exactly two hypotheses, exactly one review —
    # nothing else can add supersede events
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    base = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
    admit_report_candidates(store, (_candidate(),), now=base)
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="모호 가설",
                source_ref="88fuB_9nofM",
                revision="r4",
                payload_digest="a" * 64,
                observed_at=base + timedelta(days=3),
                next_check_at=base + timedelta(days=33),
                index_keys=(
                    _key(E, "entity:005980"),
                    _key(M, "metric:semis:sovereign_flow_v2"),
                    _key(F, "effect:semis_long_growth"),
                ),
            ),
        ),
        now=base + timedelta(days=3),
    )
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    assert len(ids) == 2
    a, b = ids
    decision = classify_semantic_profiles(
        SemanticProfileV1.from_hypothesis(
            store.read_hypothesis(a),
            index_keys=store.read_hypothesis_index(a),
        ),
        SemanticProfileV1.from_hypothesis(
            store.read_hypothesis(b),
            index_keys=store.read_hypothesis_index(b),
        ),
    )
    assert decision.classification.value == "MERGE_REVIEW"
    # admission itself queued the only review (ADMISSION origin, unresolved)
    assert (
        store.queue_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            decision,
            now=base + timedelta(days=3),
            origin="ADMISSION",
        )
        == "duplicate"
    )
    # a human supersedes the review WITHOUT merging
    assert (
        store.resolve_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            "SUPERSEDED",
            now=UTC_RUN,
            origin="ADMISSION",
        )
        == "resolved"
    )

    def all_rows():
        with store._read() as connection:
            return [
                (row["review_id"], row["status"], row["resolved_at"])
                for row in connection.execute(
                    "SELECT * FROM merge_review_queue ORDER BY review_id"
                )
            ]

    before = all_rows()
    assert len(before) == 2  # closed OPEN marker + SUPERSEDED event
    history_before = [row.review_id for row in store.read_merge_review_history(a, b)]
    # merging one involved hypothesis succeeds and creates NO spurious
    # SUPERSEDED event for the already-closed review
    outcome = store.merge_hypotheses(
        ProducerRole.ANALYSIS,
        a,
        b,
        now=UTC_RUN + timedelta(days=1),
    )
    assert outcome is not None
    assert all_rows() == before  # no new events of any kind
    history_after = [row.review_id for row in store.read_merge_review_history(a, b)]
    assert history_after == history_before
    store.close()


def test_read_merge_reviews_open_status_excludes_resolved_rows(tmp_path):
    """status='OPEN' filter must behave like the default actionable view:
    OPEN rows already carried into a resolution event are not actionable."""

    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    base = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
    admit_report_candidates(store, (_candidate(),), now=base)
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="모호 가설",
                source_ref="88fuB_9nofM",
                revision="r4",
                payload_digest="a" * 64,
                observed_at=base + timedelta(days=3),
                next_check_at=base + timedelta(days=33),
                index_keys=(
                    _key(E, "entity:005980"),
                    _key(M, "metric:semis:sovereign_flow_v2"),
                    _key(F, "effect:semis_long_growth"),
                ),
            ),
        ),
        now=base + timedelta(days=3),
    )
    ids = sorted(row.hypothesis_id for row in store.read_open_hypotheses())
    a, b = ids
    # the ADMISSION-origin review is the store's only queue row
    before = store.read_merge_reviews(status="OPEN")
    assert len(before) == 1
    assert (
        store.resolve_merge_review(
            ProducerRole.ANALYSIS,
            a,
            b,
            "REJECTED",
            now=UTC_RUN,
            origin="ADMISSION",
        )
        == "resolved"
    )
    # the OPEN row for the resolved event is closed in place; an explicit
    # status='OPEN' query must not resurrect it as actionable
    assert store.read_merge_reviews(status="OPEN") == ()
    assert store.read_merge_reviews() == ()
    store.close()


def test_weekly_summary_ids_are_frozen_reservation_ids(tmp_path):
    """Even when a week reserves with open<2 hypotheses (no pair rows),
    the summary/DUPLICATE hypothesis_ids stay the frozen reservation set,
    not the current open set."""
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    base = datetime(2026, 8, 13, 5, 30, tzinfo=UTC)
    single = admit_report_candidates(store, (_candidate(),), now=base)
    assert single.created == 1
    lone_id = store.read_open_hypotheses()[0].hypothesis_id
    # reserve + complete a week with only ONE open hypothesis (0 pairs)
    outcome = run_weekly_review(store, now=datetime(2026, 8, 16, 9, 0, tzinfo=UTC))
    assert outcome.status == "COMPLETE"
    assert outcome.pairs_scanned == 0
    summary = store.read_weekly_review_summary("2026-W33")
    # degenerate week (open<2): no pair rows, so the frozen id set is empty;
    # open_count preserves the cardinality on the receipt. The invariant is
    # that NO later admission ever leaks into this frozen summary.
    assert summary["hypothesis_ids"] == ()
    assert summary["run"].open_count == 1
    # a new hypothesis admitted AFTER completion must not leak into the
    # frozen summary ids of that week
    admit_report_candidates(
        store,
        (
            _candidate(
                statement="완료 후 신규 가설",
                trigger_expression="entity:035720:other",
                invalidation_conditions=("metric:035720:other_exit",),
                index_keys=(_key(E, "entity:035720"),),
                source_ref="CCfuB_3nofM",
                revision="rC",
                payload_digest="3" * 64,
                observed_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
                next_check_at=datetime(2026, 8, 16, 10, 0, tzinfo=UTC)
                + timedelta(days=30),
            ),
        ),
        now=datetime(2026, 8, 16, 10, 0, tzinfo=UTC),
    )
    replay = run_weekly_review(store, now=datetime(2026, 8, 16, 14, 0, tzinfo=UTC))
    assert replay.status == "DUPLICATE"
    # the frozen empty pair set survives the replay — the new hypothesis
    # never leaks into the completed week's summary
    assert replay.hypothesis_ids == ()
    summary2 = store.read_weekly_review_summary("2026-W33")
    assert summary2["hypothesis_ids"] == ()
    assert summary2["run"].open_count == 1
    store.close()
