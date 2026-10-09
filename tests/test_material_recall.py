"""Strict-TDD RED suite: material delta → reverse-index recall → shadow."""

from datetime import UTC, datetime, timedelta

from tokmo.programs.material_recall import (
    index_keys_from_delta,
    recall_for_deltas,
    record_would_have_sent_shadow,
)
from tokmo.programs.youtube_tracking import (
    admit_report_candidates,
    parse_tracking_candidates,
    provenance_from_archive_meta,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_admission import MaterialDeltaV1
from tokmo.values.analysis_contracts import (
    ChangeBasis,
    EvidenceRefV1,
    FreshnessState,
    MaterialKindV1,
    TrustClass,
)

NOW = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)
BINDING = "b" * 64

CANDIDATE_BLOCK = """=== TRACKING_CANDIDATE BEGIN ===
STATEMENT: 국부펀드 삼성전자 장기 보유 지속은 반도체 수요 회복 가설의 반증 가능한 신호다
TRIGGER: THRESHOLD_PERSIST | metric:005980:net_flow_10d >= 0 for 10 sessions
SUPPORT: yt:2026-08-13:00:14
COUNTER: metric:005980:net_foreign_outflow_10d
INVALIDATE: disclosure:005980:fund_exit
NEXT_CHECK: 2026-09-13T00:00:00Z
TOUCH: PERSONAL
KEYS: entity:005980, metric:005980:net_flow_10d, effect:state_change
=== TRACKING_CANDIDATE END ==="""


def evidence(evidence_id="ev-1"):
    return EvidenceRefV1(
        evidence_id=evidence_id,
        kind=MaterialKindV1.QUOTE_STATE,
        source="toss-quotes",
        source_record_key="005980",
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


def delta(
    subject="subject-1",
    *,
    symbol="005980",
    ref=None,
    change_basis=ChangeBasis.BASELINE_DELTA,
):
    return MaterialDeltaV1(
        subject_id=subject,
        material_kind=MaterialKindV1.QUOTE_STATE,
        market="KR",
        symbol=symbol,
        tick_id=4242,
        material_binding_digest=BINDING,
        evidence_refs=(ref if ref is not None else evidence(),),
        current_fields={"last_price_text": "80000"},
        baseline_fields={"last_price_text": "76000"},
        change_basis=change_basis,
        freshness_state=FreshnessState.FRESH,
        created_at=NOW,
    )


# --- T12 deterministic index keys ------------------------------------------


def test_index_keys_from_delta_are_deterministic_and_closed():
    keys = index_keys_from_delta(delta())
    values = [(key.kind.value, key.value) for key in keys]
    assert ("ENTITY", "entity:005980") in values
    assert ("METRIC", "metric:QUOTE_STATE:005980") in values
    assert ("EFFECT", "effect:BASELINE_DELTA") in values
    assert ("SOURCE_EVENT", "source-event:ev-1") in values
    assert index_keys_from_delta(delta()) == keys


# --- T13 recall matching ----------------------------------------------------


def make_store_with_hypothesis(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    meta = {
        "video_id": "YlVhQO-Fkc8",
        "published": "2026-08-13T04:21:51+00:00",
        "transcript_sha256": "c" * 64,
    }
    provenance = provenance_from_archive_meta(meta)
    candidates = parse_tracking_candidates(
        CANDIDATE_BLOCK,
        source_ref=provenance.source_ref,
        revision=provenance.revision,
        observed_at=NOW,
    )
    admit_report_candidates(store, candidates, now=NOW)
    return store


def test_recall_matches_metric_and_entity_keys(tmp_path):
    store = make_store_with_hypothesis(tmp_path)
    recalls = recall_for_deltas(store, (delta(),), now=NOW)
    assert len(recalls) == 1
    recall = recalls[0]
    matched = {(key.kind.value, key.value) for key in recall.matched_keys}
    assert ("ENTITY", "entity:005980") in matched
    assert recall.match_kind in {"EXACT", "PARTIAL"}
    assert recall.material_delta_ids
    store.close()


def test_recall_no_match_returns_empty(tmp_path):
    store = make_store_with_hypothesis(tmp_path)
    other = delta(subject="subject-2", symbol="035720", ref=evidence("ev-9"))
    assert recall_for_deltas(store, (other,), now=NOW) == ()
    store.close()


# --- T14 shadow recording ---------------------------------------------------


def test_shadow_record_written_once_and_replayed_as_duplicate(tmp_path):
    store = make_store_with_hypothesis(tmp_path)
    deltas = (delta(),)
    recalls = recall_for_deltas(store, deltas, now=NOW)
    outcome = record_would_have_sent_shadow(store, recalls, deltas, now=NOW)
    assert outcome.new_records == 1
    replay = record_would_have_sent_shadow(
        store,
        recalls,
        deltas,
        now=NOW + timedelta(hours=5),
    )
    assert replay.new_records == 0
    assert replay.duplicates == 1
    store.close()


def test_shadow_payload_contains_no_paths_or_identifiers(tmp_path):
    store = make_store_with_hypothesis(tmp_path)
    deltas = (delta(),)
    recalls = recall_for_deltas(store, deltas, now=NOW)
    record_would_have_sent_shadow(store, recalls, deltas, now=NOW)
    rows = store.read_shadow_recall()
    assert rows
    for row in rows:
        lowered = row.payload_json.lower()
        assert "/users/" not in lowered
        assert "tokmo" not in lowered
        assert ".local" not in lowered
    store.close()


def test_new_material_creates_only_candidates_never_alerts(tmp_path):
    store = make_store_with_hypothesis(tmp_path)
    recalls = recall_for_deltas(store, (delta(),), now=NOW)
    # This phase forbids any promotion: recall rows carry no action payload.
    for recall in recalls:
        assert not hasattr(recall, "action") or recall.action is None
    store.close()


# --- M5: genuine >96-char identities must stay legal, bounded, deterministic


def test_128_char_subject_with_symbol_none_end_to_end(tmp_path):
    """Positive control reaching the hash/bound branch for real.

    subject_id of 128 legal token chars, symbol=None: the ENTITY/METRIC key
    identities exceed the 96-char bound, so the bounded form must be used,
    stay require_token-legal (no '~'), be deterministic, and the recall
    batch must survive (match via the bounded key).
    """
    store = make_store_with_hypothesis(tmp_path)
    long_subject = "s" * 128
    long_delta = delta(subject=long_subject, symbol=None, ref=evidence("ev-l1"))
    keys = index_keys_from_delta(long_delta)
    for key in keys:
        assert len(key.value) <= 128
        # require_token-legal charset (the old '~' separator raised here)
        from tokmo.values.ops_kernel import HypothesisIndexKeyV1 as _K

        _K(key.kind, key.value)  # must not raise
    # deterministic: same input -> identical bounded identity
    again = index_keys_from_delta(
        delta(subject=long_subject, symbol=None, ref=evidence("ev-l1")),
    )
    assert [k.value for k in keys] == [k.value for k in again]
    # the bounded branch really engaged: identity is shorter than raw subject
    entity = next(k for k in keys if k.kind.value == "ENTITY")
    assert len(entity.value) < len("entity:") + len(long_subject)
    # batch resilience + real match: register a hypothesis under the SAME
    # bounded key namespace via a delta-built key, then recall matches it
    from tokmo.programs.youtube_tracking import (
        admit_report_candidates,
        parse_tracking_candidates,
    )

    block = CANDIDATE_BLOCK.replace(
        "KEYS: entity:005980, metric:005980:net_flow_10d, effect:state_change",
        f"KEYS: {entity.value}",
    )
    parsed = parse_tracking_candidates(
        block,
        source_ref="vid-long",
        revision="rev-long-1",
        observed_at=NOW,
    )
    admit_report_candidates(store, parsed, now=NOW)
    recalls = recall_for_deltas(store, (long_delta,), now=NOW)
    assert len(recalls) == 1
    store.close()


def test_max_length_evidence_id_source_event_is_bounded(tmp_path):
    """128-char evidence_id must yield a legal bounded source-event key."""
    long_ev = "e" * 128
    long_delta = delta(subject="subject-1", symbol="005980", ref=evidence(long_ev))
    keys = index_keys_from_delta(long_delta)
    src = next(k for k in keys if k.kind.value == "SOURCE_EVENT")
    # 'source-event:' (13) + 128 = 141 > 128: must be bounded, not thrown
    assert len(src.value) <= 128
    from tokmo.values.ops_kernel import HypothesisIndexKeyV1 as _K

    _K(src.kind, src.value)  # legal charset
    # deterministic
    again = index_keys_from_delta(
        delta(subject="subject-1", symbol="005980", ref=evidence(long_ev)),
    )
    src_again = next(k for k in again if k.kind.value == "SOURCE_EVENT")
    assert src.value == src_again.value
    # distinct long ids must not collide
    other = index_keys_from_delta(
        delta(subject="subject-1", symbol="005980", ref=evidence("e" * 127 + "f")),
    )
    src_other = next(k for k in other if k.kind.value == "SOURCE_EVENT")
    assert src.value != src_other.value
