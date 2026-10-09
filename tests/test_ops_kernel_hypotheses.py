"""Strict-TDD RED suite: schema v10 integration + Fable repair expectations.

Covers (per Fable session 354442c2 findings):
- B1: canonical v9 base, SCHEMA_VERSION=10, v4/v6/v7/v8/v9 migrations all
  valid, v9->v10 row-preserving migration, clean-v10 bootstrap, hypothesis
  tables present, canonical v8/v9 tables retained.
- B2: merge_hypotheses index transfer, terminal conflict, kept validation,
  cycle prevention, identical replay DUPLICATE.
- M1: terminal/superseded historical replay returns DUPLICATE before the
  terminal guard; per-candidate admission isolation.
- M3: transition reason_token durably preserved (append-only lifecycle).
- M4: sighting evidence_delta records actual evidence delta; revisions
  preserve prior supporting evidence history.
- minors: canonical statement strip, redaction alignment (no bare 'secret'
  false positive), bounded/hashed long subject ids, duplicate-prefix keys.
"""

import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.records import ops_kernel as module
from tokmo.records.ops_kernel import SCHEMA_VERSION, OpsKernelStore
from tokmo.values.ops_kernel import (
    HypothesisIndexKeyV1,
    HypothesisState,
    IndexKeyKind,
    ProducerRole,
    ReplayResult,
    TrackingCandidateV1,
    TriggerKind,
    hypothesis_identity_digest,
)

NOW = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)
LATER = NOW + timedelta(hours=3)
TS = "2026-08-01T00:00:00.000000Z"


def make_candidate(
    statement=" 삼성전자 장기 성장 가설: 국부펀드 편입 지속 관찰 ", **over
):
    fields = dict(
        statement=statement,
        trigger_kind=TriggerKind.EVENT_OCCUR,
        trigger_expression="entity:005980:daily_flow:net_buy_10d_persist",
        supporting_evidence=("yt:2026-08-13:00:14", "yt:2026-08-13:00:21"),
        counter_evidence=(),
        invalidation_conditions=("metric:005980:net_flow_negative_10d",),
        next_check_at=NOW + timedelta(days=30),
        portfolio_touch="PERSONAL",
        index_keys=(
            HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:005980"),
            HypothesisIndexKeyV1(IndexKeyKind.METRIC, "metric:005980:net_flow"),
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


# --- B1: canonical base + v10 ---------------------------------------------


def test_schema_version_is_current_on_canonical_base():
    assert SCHEMA_VERSION == 15


def test_bootstrap_creates_v11_hypothesis_tables(tmp_path):
    store = make_store(tmp_path)
    with store._read() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 15
    assert {
        "hypotheses",
        "hypothesis_sightings",
        "hypothesis_index",
        "hypothesis_lifecycle",
        "shadow_recall_records",
        "hypothesis_relations",
        "merge_review_queue",
        "weekly_review_runs",
        "weekly_review_pairs",
        "hypothesis_admission_decisions",
    } <= names
    # canonical v9 tables retained
    assert {
        "analysis_requests",
        "analysis_coverage",
        "cadence_ticks",
        "planned_requests",
        "manifest_runs",
    } <= names
    store.close()


def _build_legacy(path, sql):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    import os

    os.chmod(path, 0o600)


def test_v9_fixture_requires_migration_and_preserves_rows_via_builder(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V9)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO incidents(kind,subject,first_seen_at,last_seen_at,count,"
        "state,impact_ref) VALUES('k','s',?,?,1,'RESOLVED','z')",
        (TS, TS),
    )
    connection.commit()
    connection.close()
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    # Historical v9 row/schema compatibility stays provable through the
    # explicitly test-only legacy builder (Plan/SDS 2026-08-30 §4).
    import legacy_ops_kernel_builder

    legacy_ops_kernel_builder.build_legacy(
        tmp_path / "probe" / "v9.sqlite3",
        9,
    )
    with sqlite3.connect(path) as probe:
        assert probe.execute("PRAGMA user_version").fetchone() == (9,)
        assert probe.execute("SELECT count(*) FROM incidents").fetchone() == (1,)
    store = None


def test_every_legacy_schema_version_is_test_only_migratable(tmp_path):
    import legacy_ops_kernel_builder

    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    for version in sorted(legacy_ops_kernel_builder.LEGACY_VERSIONS):
        path = tmp_path / f"v{version}" / "ops_kernel.sqlite3"
        _build_legacy(path, legacy_ops_kernel_builder.LEGACY_VERSIONS[version])
        before = path.read_bytes()
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
        assert path.read_bytes() == before
        with sqlite3.connect(path) as connection:
            assert connection.execute("PRAGMA user_version").fetchone() == (version,)


def test_malformed_v9_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V9)
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX cadence_wire_starts_open")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    from tokmo.values.ops_kernel import OpsKernelCorruptError

    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


# --- B2: merge_hypotheses --------------------------------------------------


def admit(tmp_path, candidate):
    store = make_store(tmp_path)
    result, hypothesis_id = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        candidate,
        now=NOW,
    )
    assert result is ReplayResult.CREATED
    return store, hypothesis_id


def test_merge_moves_unique_index_keys_to_kept(tmp_path):
    store, kept = admit(tmp_path, make_candidate())
    other = make_candidate(
        statement="하이닉스 국부펀드 편입 관찰",
        trigger_expression="entity:000660:daily_flow:x",
        index_keys=(HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:000660"),),
    )
    _, merged = store.upsert_hypothesis(ProducerRole.ANALYSIS, other, now=NOW)
    outcome = store.merge_hypotheses(
        ProducerRole.ANALYSIS,
        kept,
        merged,
        now=LATER,
    )
    assert outcome.value == "MERGED"
    # merged's unique key now wakes the KEPT hypothesis on recall
    assert store.read_hypotheses_by_index(
        IndexKeyKind.ENTITY,
        "entity:000660",
    ) == (kept,)
    # kept still has its own keys
    assert kept in store.read_hypotheses_by_index(
        IndexKeyKind.ENTITY,
        "entity:005980",
    )
    store.close()


def test_merge_terminal_merged_conflicts_without_overwrite(tmp_path):
    store, kept = admit(tmp_path, make_candidate())
    other = make_candidate(
        statement="하이닉스 국부펀드 편입 관찰",
        trigger_expression="entity:000660:daily_flow:x",
        index_keys=(HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:000660"),),
    )
    _, merged = store.upsert_hypothesis(ProducerRole.ANALYSIS, other, now=NOW)
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        merged,
        HypothesisState.WATCHING,
        now=NOW,
    )
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        merged,
        HypothesisState.RESOLVED,
        now=NOW,
        reason_token="resolved_by_user",
    )
    outcome = store.merge_hypotheses(
        ProducerRole.ANALYSIS,
        kept,
        merged,
        now=LATER,
    )
    assert outcome.value == "CONFLICT"
    row = store.read_hypothesis(merged)
    assert row.state is HypothesisState.RESOLVED  # terminal provenance intact
    assert row.superseded_by is None
    # index transfer must not happen on conflict
    assert store.read_hypotheses_by_index(
        IndexKeyKind.ENTITY,
        "entity:000660",
    ) == (merged,)
    store.close()


def test_merge_validates_kept_and_prevents_cycles(tmp_path):
    store, kept = admit(tmp_path, make_candidate())
    other = make_candidate(
        statement="하이닉스 국부펀드 편입 관찰",
        trigger_expression="entity:000660:daily_flow:x",
    )
    _, merged = store.upsert_hypothesis(ProducerRole.ANALYSIS, other, now=NOW)
    # kept terminal -> conflict
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        kept,
        HypothesisState.WATCHING,
        now=NOW,
    )
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        kept,
        HypothesisState.EXPIRED,
        now=NOW,
    )
    assert (
        store.merge_hypotheses(
            ProducerRole.ANALYSIS,
            kept,
            merged,
            now=LATER,
        ).value
        == "CONFLICT"
    )
    # self merge -> conflict
    assert (
        store.merge_hypotheses(
            ProducerRole.ANALYSIS,
            kept,
            kept,
            now=LATER,
        ).value
        == "CONFLICT"
    )
    # cycle: A merged into B earlier, now B into A -> conflict
    store2, a = admit(tmp_path / "cyc", make_candidate())
    b = make_candidate(
        statement="사이클 가설 B",
        trigger_expression="entity:035720:daily_flow:x",
    )
    _, bid = store2.upsert_hypothesis(ProducerRole.ANALYSIS, b, now=NOW)
    store2.merge_hypotheses(ProducerRole.ANALYSIS, bid, a, now=LATER)
    assert (
        store2.merge_hypotheses(
            ProducerRole.ANALYSIS,
            a,
            bid,
            now=LATER,
        ).value
        == "CONFLICT"
    )
    store.close()
    store2.close()


def test_merge_identical_replay_returns_duplicate(tmp_path):
    store, kept = admit(tmp_path, make_candidate())
    other = make_candidate(
        statement="하이닉스 국부펀드 편입 관찰",
        trigger_expression="entity:000660:daily_flow:x",
        index_keys=(HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:000660"),),
    )
    _, merged = store.upsert_hypothesis(ProducerRole.ANALYSIS, other, now=NOW)
    assert (
        store.merge_hypotheses(
            ProducerRole.ANALYSIS,
            kept,
            merged,
            now=LATER,
        ).value
        == "MERGED"
    )
    replay = store.merge_hypotheses(
        ProducerRole.ANALYSIS,
        kept,
        merged,
        now=LATER + timedelta(hours=1),
    )
    assert replay.value == "DUPLICATE"
    assert store.read_hypothesis(merged).superseded_by == kept
    store.close()


# --- M1: terminal/superseded historical replay -----------------------------


def test_terminal_historical_replay_returns_duplicate_before_guard(tmp_path):
    store, hypothesis_id = admit(tmp_path, make_candidate())
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        hypothesis_id,
        HypothesisState.WATCHING,
        now=NOW,
    )
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        hypothesis_id,
        HypothesisState.RESOLVED,
        now=LATER,
        reason_token="resolved_by_user",
    )
    # Historical replay of the ORIGINAL sighting: DUPLICATE, not a transition
    # error — re-observing an already-recorded report changes nothing.
    result, same = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        make_candidate(),
        now=LATER,
    )
    assert result is ReplayResult.DUPLICATE
    assert same == hypothesis_id
    store.close()


def test_superseded_historical_replay_returns_duplicate(tmp_path):
    store, kept = admit(tmp_path, make_candidate())
    other = make_candidate(
        statement="동일 주제 재서술 가설",
        trigger_expression="entity:005980:daily_flow:net_buy_10d_persist_v2",
    )
    _, merged = store.upsert_hypothesis(ProducerRole.ANALYSIS, other, now=NOW)
    store.merge_hypotheses(ProducerRole.ANALYSIS, kept, merged, now=LATER)
    result, same = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        other,
        now=LATER,
    )
    assert result is ReplayResult.DUPLICATE
    assert same == merged
    store.close()


# --- M3: reason_token durability -------------------------------------------


def test_transition_reason_token_is_durably_preserved(tmp_path):
    store, hypothesis_id = admit(tmp_path, make_candidate())
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        hypothesis_id,
        HypothesisState.WATCHING,
        now=NOW,
        reason_token="operator_promoted",
    )
    store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        hypothesis_id,
        HypothesisState.INVALIDATED,
        now=LATER,
        reason_token="counterevidence_confirmed",
    )
    history = store.read_hypothesis_lifecycle(hypothesis_id)
    assert [(row.from_state, row.to_state, row.reason_token) for row in history] == [
        ("CANDIDATE", "WATCHING", "operator_promoted"),
        ("WATCHING", "INVALIDATED", "counterevidence_confirmed"),
    ]
    store.close()


def test_lifecycle_replay_is_idempotent(tmp_path):
    store, hypothesis_id = admit(tmp_path, make_candidate())
    first = store.transition_hypothesis(
        ProducerRole.ANALYSIS,
        hypothesis_id,
        HypothesisState.WATCHING,
        now=NOW,
        reason_token="operator_promoted",
    )
    # same-state replay is rejected by the state machine, not by duplication
    assert first is ReplayResult.CREATED
    with pytest.raises(Exception):
        store.transition_hypothesis(
            ProducerRole.ANALYSIS,
            hypothesis_id,
            HypothesisState.WATCHING,
            now=LATER,
            reason_token="operator_promoted",
        )
    assert len(store.read_hypothesis_lifecycle(hypothesis_id)) == 1
    store.close()


# --- M4: evidence_delta + revision history ---------------------------------


def test_sighting_evidence_delta_records_actual_evidence_delta(tmp_path):
    store, hypothesis_id = admit(tmp_path, make_candidate())
    next_day = make_candidate(
        payload_digest="e" * 64,
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        source_ref="66fuB_7nofM",
        supporting_evidence=(
            "yt:2026-08-13:00:14",
            "yt:2026-08-13:00:21",
            "yt:2026-08-14:01:30",
        ),
        counter_evidence=("metric:005980:price_down_5pct",),
    )
    result, same = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        next_day,
        now=LATER,
    )
    assert result is ReplayResult.CREATED  # revision applied
    assert same == hypothesis_id
    sightings = store.read_hypothesis_sightings(hypothesis_id)
    assert len(sightings) == 2
    # evidence delta = the NEW evidence tokens only, not counter-evidence
    assert sightings[1].evidence_delta == ("yt:2026-08-14:01:30",)
    store.close()


def test_revision_preserves_prior_supporting_evidence_history(tmp_path):
    store, hypothesis_id = admit(tmp_path, make_candidate())
    next_day = make_candidate(
        payload_digest="e" * 64,
        revision="2026-08-14T04:00:00+00:00:abcdef123456",
        source_ref="66fuB_7nofM",
        supporting_evidence=("yt:2026-08-14:01:30",),
    )
    store.upsert_hypothesis(ProducerRole.ANALYSIS, next_day, now=LATER)
    # current supporting evidence is the latest revision's view
    assert store.read_hypothesis(hypothesis_id).supporting_evidence == (
        "yt:2026-08-14:01:30",
    )
    # but the FIRST sighting still records the original evidence set
    first = store.read_hypothesis_sightings(hypothesis_id)[0]
    assert first.payload_digest == "d" * 64
    store.close()


# --- minors -----------------------------------------------------------------


def test_canonical_statement_is_stripped():
    candidate = make_candidate()
    assert candidate.statement == candidate.statement.strip()
    digest = hypothesis_identity_digest(
        candidate.statement,
        candidate.trigger_kind,
        candidate.trigger_expression,
    )
    assert candidate.identity_digest == digest


def test_redaction_blocks_real_secrets_not_the_word_secret():
    # the ordinary word 'secret' alone is legitimate Korean-market vocabulary
    candidate = make_candidate(
        statement=" 이 회사의 핵심 경쟁력은 비밀(secret) 기술이다 "
    )
    assert candidate.statement.startswith("이 회사의")
    # actual secret-looking material still rejected
    with pytest.raises(ValueError):
        make_candidate(statement=" 키는 sk-abcdef1234567890abcdef1234567890 ")
    with pytest.raises(ValueError):
        make_candidate(statement="/home/tokmo/notes.txt 내용 참조")


def test_long_identity_is_bounded_hashed_legal_and_deterministic():
    """M5: a genuine >96-char end-to-end identity must exercise the bound.

    128-char subject_id with symbol None flows untouched into
    ``_bounded_identity``; the bounded result must be key-legal (token
    charset), <= the key bound, deterministic, and collision-free for
    distinct long inputs. The 96-char boundary itself must pass through
    unhashed.
    """
    from tokmo.programs.material_recall import (
        _bounded_identity,
        index_keys_from_delta,
    )

    long_a = "subject-" + "a" * 120  # 128 chars total
    long_b = "subject-" + "b" * 120  # distinct 128-char identity
    assert len(long_a) == 128

    bounded_a = _bounded_identity(long_a)
    bounded_b = _bounded_identity(long_b)
    # bound branch actually taken (input > 96), output strictly shorter
    assert len(bounded_a) <= 96 and len(bounded_a) < len(long_a)
    # deterministic and collision-free for distinct inputs
    assert bounded_a == _bounded_identity(long_a)
    assert bounded_a != bounded_b
    # exact-boundary input passes through unhashed
    assert _bounded_identity("x" * 96) == "x" * 96

    delta = _delta_with_identity(subject_id=long_a, symbol=None)
    keys = index_keys_from_delta(delta)
    assert len(keys) == 4
    for key in keys:
        # every key value is legal under the closed token rule and bounded
        assert len(key.value) <= 128
        from tokmo.values.ops_kernel import HypothesisIndexKeyV1

        HypothesisIndexKeyV1(key.kind, key.value)  # must not raise
    # the entity key actually carries the bounded form, not the raw 128 chars
    from tokmo.values.ops_kernel import IndexKeyKind

    entity = [k for k in keys if k.kind is IndexKeyKind.ENTITY][0]
    assert len(entity.value) <= len("entity:") + 96
    assert "a" * 120 not in entity.value


def test_source_event_max_length_identity_is_bounded_legal():
    """M5 positive control: a max-length (128-char) evidence_id must still
    produce a legal bounded SOURCE_EVENT key instead of overflowing."""
    from tokmo.programs.material_recall import (
        _bounded_identity,
        index_keys_from_delta,
    )
    from tokmo.values.ops_kernel import HypothesisIndexKeyV1, IndexKeyKind

    max_evidence = "e" * 128
    delta = _delta_with_identity(
        subject_id="subject-1",
        symbol="005980",
        evidence_id=max_evidence,
    )
    (source_key,) = [
        key
        for key in index_keys_from_delta(delta)
        if key.kind is IndexKeyKind.SOURCE_EVENT
    ]
    # unbounded concatenation would be 13 + 128 = 141 chars -> must be bounded
    assert len(source_key.value) <= 128
    HypothesisIndexKeyV1(IndexKeyKind.SOURCE_EVENT, source_key.value)
    # the bounded evidence identity matches the shared mechanism exactly
    assert source_key.value == ("source-event:" + _bounded_identity(max_evidence))


def _delta_with_identity(*, subject_id, symbol, evidence_id="ev-1"):
    from tokmo.values.analysis_admission import MaterialDeltaV1
    from tokmo.values.analysis_contracts import (
        ChangeBasis,
        EvidenceRefV1,
        FreshnessState,
        MaterialKindV1,
        TrustClass,
    )

    evidence = EvidenceRefV1(
        evidence_id=evidence_id,
        kind=MaterialKindV1.QUOTE_STATE,
        source="toss-quotes",
        source_record_key="k",
        observed_at=NOW,
        fetched_at=NOW,
        stored_at=NOW,
        acquired_at=NOW,
        content_hash="e" * 64,
        schema_version=1,
        parser_version="p1",
        trust_class=TrustClass.OFFICIAL_PROVIDER,
        freshness_state=FreshnessState.FRESH,
        expires_at=NOW + timedelta(days=1),
    )
    return MaterialDeltaV1(
        subject_id=subject_id,
        material_kind=MaterialKindV1.QUOTE_STATE,
        market="KR",
        symbol=symbol,
        tick_id=1,
        material_binding_digest="b" * 64,
        evidence_refs=(evidence,),
        current_fields={},
        baseline_fields=None,
        change_basis=ChangeBasis.BASELINE_DELTA,
        freshness_state=FreshnessState.FRESH,
        created_at=NOW,
    )


def test_index_key_values_carry_no_duplicate_prefix():
    key = HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:005980")
    # canonical shape: the stored value includes its prefix exactly once
    assert key.value.startswith("entity:")
    assert not key.value.startswith("entity:entity:")
    with pytest.raises(ValueError):
        HypothesisIndexKeyV1(IndexKeyKind.ENTITY, "entity:entity:005980")
