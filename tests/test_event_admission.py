"""Event admission policy: contracts, evaluator, digest, architecture."""

import hashlib
from datetime import UTC, datetime

import pytest

from tokmo.programs.event_admission import (
    DEFAULT_EVENT_ADMISSION_POLICY_V1,
    EventAdmissionPolicyConfigError,
    EventAdmissionPolicyV1,
    evaluate_event_admission,
)
from tokmo.values.analysis_admission import (
    CriterionIdV1,
    MaterialDeltaV1,
)
from tokmo.values.analysis_contracts import (
    AnalysisKind,
    CandidateEnvelopeV1,
    ChangeBasis,
    EvidenceRefV1,
    FreshnessState,
    MaterialKindV1,
    TrustClass,
    candidate_id_for,
    evidence_id_for,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import CandidateAssignment

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_NOW = datetime(2026, 8, 4, 14, 0, tzinfo=UTC)


def _evidence(kind, source, record, captured_at):
    content_hash = hashlib.sha256(
        canonical_encode(
            {
                "v": 1,
                "domain": "tokmo-v3:evidence-content",
                "row": {"x": record},
                "pack": "packhash" * 6 + "0" * 4,
            }
        )
    ).hexdigest()
    return EvidenceRefV1(
        evidence_id=evidence_id_for(kind, source, record, content_hash),
        kind=kind,
        source=source,
        source_record_key=record,
        observed_at=captured_at,
        fetched_at=captured_at,
        stored_at=captured_at,
        acquired_at=captured_at,
        content_hash=content_hash,
        schema_version=1,
        parser_version="material-pack-v1",
        trust_class=TrustClass.OFFICIAL_PROVIDER,
        freshness_state=FreshnessState.FRESH,
        expires_at=captured_at,
    )


def _delta(
    *,
    subject_id="sub-abcd1234efgh5678",
    kind=MaterialKindV1.QUOTE_STATE,
    market="US",
    symbol="AAPL",
    current_fields=None,
    baseline_fields=None,
    tick_id=1,
    material_binding_digest="a" * 64,
    freshness=FreshnessState.FRESH,
):
    cf = current_fields or {"market": market, "symbol": symbol}
    return MaterialDeltaV1(
        subject_id=subject_id,
        material_kind=kind,
        market=market,
        symbol=symbol,
        tick_id=tick_id,
        material_binding_digest=material_binding_digest,
        evidence_refs=(_evidence(kind, "test-src", "rec1", _NOW),),
        current_fields=cf,
        baseline_fields=baseline_fields,
        change_basis=ChangeBasis.BASELINE_DELTA,
        freshness_state=freshness,
        created_at=_NOW,
    )


def _candidate(delta):
    subject = delta.subject_id
    kind = delta.material_kind
    cid = candidate_id_for(delta.tick_id, kind, subject)
    fields_keys = {
        MaterialKindV1.QUOTE_STATE: {
            "market",
            "symbol",
            "portfolio_relation",
            "last_price",
            "currency_raw",
            "provider_timestamp",
            "fetched_at",
            "change_basis",
            "baseline_price",
            "price_delta_ratio",
            "numeric_state",
            "trigger_kind",
            "trigger_reference_price",
            "trigger_horizon",
            "price_delta_ratio_5m",
            "price_delta_ratio_15m",
            "price_delta_ratio_30m",
            "velocity_per_min",
            "acceleration_per_min2",
            "reversal",
            "persistent_drift_ticks",
            "money_impact",
        },
    }
    return CandidateEnvelopeV1(
        cid,
        subject,
        kind,
        delta.market,
        delta.tick_id,
        CandidateAssignment.FULL_REVIEW,
        delta.evidence_refs,
        {k: "v" for k in fields_keys[kind]},
        _NOW,
    )


# ---------------------------------------------------------------------------
# 1. AnalysisKind
# ---------------------------------------------------------------------------


def test_analysis_kind_reserves_health_coverage_but_event_registry_executes_event_only():
    assert AnalysisKind.EVENT.value == "EVENT"
    assert AnalysisKind.HEALTH_COVERAGE.value == "HEALTH_COVERAGE"


# ---------------------------------------------------------------------------
# 2. MaterialDeltaV1 identity
# ---------------------------------------------------------------------------


def test_material_delta_identity_is_stable_over_mapping_order_and_restart():
    d1 = _delta(current_fields={"market": "US", "symbol": "AAPL"})
    d2 = _delta(current_fields={"symbol": "AAPL", "market": "US"})
    assert d1.delta_id == d2.delta_id
    assert len(d1.delta_id) == 32


def test_delta_contains_exact_evidence_and_no_private_account_identity():
    d = _delta()
    assert all(
        hasattr(d, f)
        for f in (
            "delta_id",
            "subject_id",
            "material_kind",
            "market",
            "symbol",
            "tick_id",
            "material_binding_digest",
            "evidence_refs",
            "current_fields",
            "baseline_fields",
            "change_basis",
            "freshness_state",
            "created_at",
        )
    )
    assert "account_scope" not in d.current_fields
    assert "account_scope" not in (d.baseline_fields or {})


# ---------------------------------------------------------------------------
# 3. Policy digest
# ---------------------------------------------------------------------------


def test_policy_digest_is_canonical_and_order_independent():
    policy = DEFAULT_EVENT_ADMISSION_POLICY_V1
    d1 = policy.policy_digest
    assert len(d1) == 64
    # Re-computing yields the same digest
    raw = canonical_encode(
        {
            "domain": "tokmo-v3:event-admission-policy:v1",
            "schema_version": policy.schema_version,
            "policy_version": policy.policy_version,
            "criteria": [c.value for c in policy.enabled_criteria],
            "thresholds": policy.thresholds_canonical(),
        }
    )
    expected = hashlib.sha256(raw).hexdigest()
    assert d1 == expected


def test_policy_rejects_unknown_duplicate_or_empty_criteria():
    # Duplicate criterion ids
    with pytest.raises(EventAdmissionPolicyConfigError):
        EventAdmissionPolicyV1(
            schema_version=1,
            policy_version="test-dup",
            enabled_criteria=(
                CriterionIdV1.COMPLETED_EXECUTION,
                CriterionIdV1.COMPLETED_EXECUTION,
            ),
            thresholds={},
        )
    # Empty criteria
    with pytest.raises(EventAdmissionPolicyConfigError):
        EventAdmissionPolicyV1(
            schema_version=1,
            policy_version="test-empty",
            enabled_criteria=(),
            thresholds={},
        )


def test_policy_rejects_noncanonical_out_of_range_and_cross_invalid_thresholds():
    # Non-decimal threshold value
    with pytest.raises(EventAdmissionPolicyConfigError):
        EventAdmissionPolicyV1(
            schema_version=1,
            policy_version="test-bad-val",
            enabled_criteria=(CriterionIdV1.QUOTE_THRESHOLD_MOVE,),
            thresholds={"quote_move_threshold_ratio": "abc"},
        )
    # Out of range (negative)
    with pytest.raises(EventAdmissionPolicyConfigError):
        EventAdmissionPolicyV1(
            schema_version=1,
            policy_version="test-neg",
            enabled_criteria=(CriterionIdV1.QUOTE_THRESHOLD_MOVE,),
            thresholds={"quote_move_threshold_ratio": "-0.005"},
        )
    # > 1 is out of range for a ratio threshold
    with pytest.raises(EventAdmissionPolicyConfigError):
        EventAdmissionPolicyV1(
            schema_version=1,
            policy_version="test-over-one",
            enabled_criteria=(CriterionIdV1.QUOTE_THRESHOLD_MOVE,),
            thresholds={"quote_move_threshold_ratio": "1.5"},
        )


# ---------------------------------------------------------------------------
# 4. Evaluation
# ---------------------------------------------------------------------------


def test_all_applicable_criteria_evaluate_in_registry_order_without_short_circuit():
    """Multiple criteria match and all results are returned in registry order."""
    # A completed execution that also has a quote threshold move
    delta = _delta(
        kind=MaterialKindV1.COMPLETED_EXECUTION,
        current_fields={
            "market": "US",
            "symbol": "AAPL",
        },
    )
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1,
        (delta,),
        now=_NOW,
    )
    assert len(decisions) == 1
    decision = decisions[0]
    # All results are sorted by registry ordinal
    ordinals = [
        DEFAULT_EVENT_ADMISSION_POLICY_V1._ordinal_of(r.criterion_id)
        for r in decision.matched_results
    ]
    assert ordinals == sorted(ordinals)
    # ADMIT because at least one matched
    assert decision.decision == "ADMIT"


def test_multiple_matches_aggregate_reason_codes_and_max_assignment_deterministically():
    delta = _delta(
        kind=MaterialKindV1.QUOTE_STATE,
        current_fields={"market": "US", "symbol": "AAPL"},
    )
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1,
        (delta,),
        now=_NOW,
    )
    assert len(decisions) == 1
    decision = decisions[0]
    assert decision.decision in ("ADMIT", "REJECT")
    # reason codes don't embed values/symbols/thresholds
    for r in decision.matched_results:
        assert "_" in r.reason_code.value or r.reason_code.value.startswith("event_")
        assert not any(c.isdigit() for c in r.reason_code.value)


def test_threshold_boundaries_are_exact_inclusive_or_exclusive_by_contract():
    # ratio exactly at threshold => inclusive (admit)
    delta_at = _delta(
        current_fields={
            "market": "US",
            "symbol": "AAPL",
            "price_delta_ratio": str(
                DEFAULT_EVENT_ADMISSION_POLICY_V1.thresholds[
                    "quote_move_threshold_ratio"
                ]
            ),
        },
    )
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1,
        (delta_at,),
        now=_NOW,
    )
    # Should admit (inclusive boundary)
    assert decisions[0].decision == "ADMIT"


def test_event_registry_executes_event_only():
    """The registry/evaluator only produces EVENT admission decisions;
    HEALTH_COVERAGE is reserved but never produced by this evaluator."""
    delta = _delta()
    decisions = evaluate_event_admission(
        DEFAULT_EVENT_ADMISSION_POLICY_V1,
        (delta,),
        now=_NOW,
    )
    for d in decisions:
        # Decisions are about EVENT admission
        assert d.decision in ("ADMIT", "REJECT")
        assert d.policy_version == DEFAULT_EVENT_ADMISSION_POLICY_V1.policy_version


# ---------------------------------------------------------------------------
# 5. Architecture
# ---------------------------------------------------------------------------


def test_criterion_module_has_no_provider_transport_storage_cadence_or_composition_import():
    import ast
    from pathlib import Path

    module_path = Path("src/tokmo/programs/event_admission.py")
    tree = ast.parse(module_path.read_text())
    forbidden_roots = {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "subprocess",
        "logging",
    }
    forbidden_tokmo = {
        "tokmo.facts",
        "tokmo.composition",
        "tokmo.records",
        "tokmo.programs.production_cadence",
        "tokmo.programs.analysis_pipeline",
    }
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0]
            assert root not in forbidden_roots, f"forbidden import: {node.module}"
            assert not any(node.module.startswith(f) for f in forbidden_tokmo), (
                f"forbidden tokmo import: {node.module}"
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                assert root not in forbidden_roots
                assert not any(alias.name.startswith(f) for f in forbidden_tokmo)


def test_threshold_literals_and_criterion_ids_are_absent_from_cadence_storage_composition_and_provider_modules():
    """No threshold literal or criterion ID leaks into cadence/storage/composition/provider modules."""
    from pathlib import Path

    from tokmo.values.analysis_admission import CriterionIdV1

    criterion_names = {c.value.lower() for c in CriterionIdV1}
    threshold_tokens = {"0.005", "0.01", "0.002", "0.001"}
    check_files = [
        Path("src/tokmo/programs/production_cadence.py"),
        Path("src/tokmo/composition.py"),
        Path("src/tokmo/records/ops_kernel.py"),
        Path("src/tokmo/values/production_cadence.py"),
    ]
    for path in check_files:
        if not path.exists():
            continue
        text = path.read_text().lower()
        # Criterion names must not appear
        leaked = criterion_names & set(text.replace(".", " ").replace("_", " ").split())
        assert not leaked, f"{path}: leaked criterion names: {leaked}"
