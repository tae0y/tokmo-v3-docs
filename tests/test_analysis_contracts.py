from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
import hashlib
import json

import pytest

from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import (
    AuditCompletionV1 as KernelAuditCompletionV1,
    CandidateAssignment,
    DispositionAction,
    MaterialKindV1 as KernelMaterialKindV1,
    DispositionV1,
)
from tokmo.values.analysis_contracts import (
    ALLOWED_REASONS_V1,
    AuditCompletionV1,
    AnalysisRunState,
    CandidateEnvelopeV1,
    ChangeBasis,
    ChangeKind,
    EvidenceRefV1,
    FreshnessState,
    GlmMateriality,
    GlmRelevance,
    GlmUrgency,
    MaterialKindV1,
    ModelCallStatus,
    ProviderExit,
    SolProposedAction,
    SolVerdict,
    TrustClass,
    ValidatorRejection,
    HermesDispositionInputV1,
    validate_disposition,
    candidate_id_for,
    evidence_id_for,
    parse_glm_batch,
    parse_sol_batch,
    subject_id_for,
)

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)
HEX64 = "a" * 64

QUOTE_NUMERIC_FIELDS = {
    "numeric_state": "DATA_INCOMPLETE", "trigger_kind": "FIRST_OBSERVATION",
    "trigger_reference_price": None, "trigger_horizon": "NONE",
    "price_delta_ratio_5m": None, "price_delta_ratio_15m": None,
    "price_delta_ratio_30m": None, "velocity_per_min": None,
    "acceleration_per_min2": None, "reversal": False,
    "persistent_drift_ticks": 0, "money_impact": None,
}


def test_closed_enums_and_reason_table_are_exact():
    assert {x.value for x in MaterialKindV1} == {
        "HOLDINGS_CHANGE", "QUOTE_STATE", "COMPLETED_EXECUTION", "OPERATIONAL_INCIDENT"
    }
    assert len(ProviderExit) == 10
    assert len(ModelCallStatus) == 11
    assert len(ValidatorRejection) == 4
    assert len(AnalysisRunState) == 6
    assert set(ALLOWED_REASONS_V1) == set(DispositionAction)
    assert ALLOWED_REASONS_V1[DispositionAction.SEND] == {"evidence_verified_send"}


def test_audit_completion_v1_is_frozen_exact_and_validated():
    assert AuditCompletionV1 is KernelAuditCompletionV1
    assert MaterialKindV1 is KernelMaterialKindV1
    receipt = AuditCompletionV1(
        MaterialKindV1.HOLDINGS_CHANGE, "subject-1", "2026-07-30", 5,
    )
    assert tuple(receipt.__dict__) == (
        "kind", "subject_id", "session_date", "audited_count",
    )
    with pytest.raises(FrozenInstanceError):
        receipt.audited_count = 4
    invalid = (
        ("kind", "HOLDINGS_CHANGE"),
        ("subject_id", "private subject"),
        ("session_date", "2026-7-30"),
        ("session_date", "2026-02-30"),
        ("audited_count", 0),
        ("audited_count", 6),
        ("audited_count", True),
    )
    for name, value in invalid:
        with pytest.raises(ValueError):
            AuditCompletionV1(**{**receipt.__dict__, name: value})


def test_identity_hashes_are_byte_exact_and_tick_scope_is_correct():
    kind = MaterialKindV1.HOLDINGS_CHANGE
    key = ["KR", "toss-acct-" + "1" * 24, "005930"]
    expected_subject = hashlib.sha256(canonical_encode({
        "v": 1, "domain": "tokmo-v3:subject", "kind": kind.value, "key": key,
    })).hexdigest()[:16]
    assert subject_id_for(kind, key) == expected_subject
    assert candidate_id_for(10, kind, expected_subject) != candidate_id_for(11, kind, expected_subject)
    assert evidence_id_for(kind, "source", "record", HEX64) == hashlib.sha256(
        canonical_encode({
            "v": 1, "domain": "tokmo-v3:evidence", "kind": kind.value,
            "source": "source", "record": "record", "content_hash": HEX64,
        })
    ).hexdigest()[:16]


def _evidence() -> EvidenceRefV1:
    return EvidenceRefV1(
        evidence_id="evidence1", kind=MaterialKindV1.QUOTE_STATE,
        source="toss-current-price-v1", source_record_key="KR:005930",
        observed_at=NOW, fetched_at=NOW, stored_at=NOW, acquired_at=NOW,
        content_hash=HEX64, schema_version=1, parser_version="parser-v1",
        trust_class=TrustClass.OFFICIAL_PROVIDER, freshness_state=FreshnessState.FRESH,
        expires_at=NOW,
    )


def test_envelope_is_frozen_and_exact_fields_are_enforced():
    evidence = _evidence()
    envelope = CandidateEnvelopeV1(
        candidate_id="candidate1", subject_id="subject1", kind=MaterialKindV1.QUOTE_STATE,
        market="KR", tick_id=1, assignment=CandidateAssignment.FULL_REVIEW,
        evidence_refs=(evidence,), fields={
            "market": "KR", "symbol": "005930", "portfolio_relation": "HELD",
            "last_price": "1",
            "currency_raw": "KRW", "provider_timestamp": None,
            "fetched_at": NOW, "change_basis": ChangeBasis.CURRENT_FACT_ONLY.value,
            "baseline_price": None, "price_delta_ratio": None,
            **QUOTE_NUMERIC_FIELDS,
        }, created_at=NOW,
    )
    with pytest.raises(FrozenInstanceError):
        envelope.tick_id = 2
    with pytest.raises(ValueError, match="exact keys"):
        CandidateEnvelopeV1(**{**envelope.__dict__, "fields": {**envelope.fields, "extra": 1}})
    for missing in QUOTE_NUMERIC_FIELDS:
        reduced = {key: value for key, value in envelope.fields.items() if key != missing}
        with pytest.raises(ValueError, match="exact keys"):
            CandidateEnvelopeV1(**{**envelope.__dict__, "fields": reduced})


def test_validator_reachable_rejections_have_exact_priority():
    candidate = CandidateEnvelopeV1(
        candidate_id="candidate1", subject_id="subject1",
        kind=MaterialKindV1.QUOTE_STATE, market="KR", tick_id=1,
        assignment=CandidateAssignment.CRITICAL, evidence_refs=(_evidence(),),
        fields={
            "market": "KR", "symbol": "005930", "portfolio_relation": "HELD",
            "last_price": "1",
            "currency_raw": "KRW", "provider_timestamp": None,
            "fetched_at": NOW, "change_basis": ChangeBasis.CURRENT_FACT_ONLY.value,
            "baseline_price": None, "price_delta_ratio": None,
            **QUOTE_NUMERIC_FIELDS,
        }, created_at=NOW,
    )
    proposal = DispositionV1(
        "decision1", 1, "candidate1", "subject1", DispositionAction.MERGE,
        "same_subject_session_merge", (), {}, "OK", "OK", "policy-v1", NOW,
    )
    value = HermesDispositionInputV1(
        candidate, ModelCallStatus.OK, ModelCallStatus.OK, None, True, (),
        {"evidence1": FreshnessState.FRESH}, ("KR", "2026-07-30"), None, False,
    )
    assert validate_disposition(value, proposal) is ValidatorRejection.CRITICAL_SKIP_OR_MERGE


def test_private_account_scope_is_rejected_without_echo():
    with pytest.raises(ValueError) as error:
        EvidenceRefV1(
            evidence_id="evidence1", kind=MaterialKindV1.QUOTE_STATE,
            source="toss-acct-" + "1" * 24, source_record_key="KR:005930",
            observed_at=NOW, fetched_at=NOW, stored_at=NOW, acquired_at=NOW,
            content_hash=HEX64, schema_version=1, parser_version="parser-v1",
            trust_class=TrustClass.OFFICIAL_PROVIDER, freshness_state=FreshnessState.FRESH,
            expires_at=NOW,
        )
    assert "toss-acct-" not in str(error.value)


def _glm_raw(candidate_id="candidate1"):
    return canonical_encode({"schema_version": 1, "annotations": [{
        "candidate_id": candidate_id, "relevance": GlmRelevance.NONE.value,
        "materiality": GlmMateriality.LOW.value, "urgency": GlmUrgency.NONE.value,
        "evidence_unknowns": [], "summary": "ok",
    }]})


def _sol_raw(confidence="0.5", cited=("evidence1",)):
    return canonical_encode({"schema_version": 1, "analyses": [{
        "candidate_id": "candidate1", "verdict": SolVerdict.NO_ACTION.value,
        "proposed_action": SolProposedAction.IGNORE.value, "rationale": "ok",
        "cited_evidence_ids": list(cited), "confidence": confidence,
        "disagreements_with_glm": [],
    }]})


def test_parsers_require_exact_complete_coverage_and_known_evidence():
    assert parse_glm_batch(_glm_raw(), ("candidate1",)).exit is ProviderExit.OK
    assert parse_sol_batch(_sol_raw(), ("candidate1",), frozenset({"evidence1"})).exit is ProviderExit.OK
    for raw in (
        _glm_raw("unknown"),
        canonical_encode({"schema_version": 1, "annotations": []}),
        canonical_encode({"schema_version": 1, "annotations": [
            json.loads(_glm_raw())["annotations"][0],
            json.loads(_glm_raw())["annotations"][0],
        ]}),
    ):
        assert parse_glm_batch(raw, ("candidate1",)).exit is ProviderExit.PARSE_FAIL
    assert parse_sol_batch(_sol_raw(cited=("unknown",)), ("candidate1",),
                           frozenset({"evidence1"})).exit is ProviderExit.PARSE_FAIL


@pytest.mark.parametrize("confidence", ["1.00000", "0.1000", "1.1", "-0.1", "NaN"])
def test_sol_parser_rejects_noncanonical_or_out_of_range_confidence(confidence):
    assert parse_sol_batch(_sol_raw(confidence), ("candidate1",),
                           frozenset({"evidence1"})).exit is ProviderExit.PARSE_FAIL


def test_numeric_enums_are_closed_and_watch_threshold_is_fixed():
    from decimal import Decimal

    from tokmo.values.analysis_contracts import (
        NumericStateV1,
        QuoteTriggerKindV1,
        TriggerHorizonV1,
        QUOTE_WATCH_THRESHOLD_RATIO,
    )

    assert {value.value for value in NumericStateV1} == {
        "DATA_INCOMPLETE", "NORMAL", "WATCH", "MATERIAL_MOVE",
        "REVERSAL", "PERSISTENT_DRIFT",
    }
    assert {value.value for value in QuoteTriggerKindV1} == {
        "FIRST_OBSERVATION", "REVERSAL", "PERSISTENT_DRIFT",
        "CUMULATIVE_DRIFT", "THRESHOLD_MOVE", "STATE_TRANSITION",
    }
    assert {value.value for value in TriggerHorizonV1} == {
        "NONE", "TICK", "STREAK", "HORIZON_15M", "HORIZON_30M",
    }
    assert QUOTE_WATCH_THRESHOLD_RATIO == Decimal("0.002")


def test_quote_state_field_keys_are_exactly_the_22_key_contract():
    from tokmo.values.analysis_contracts import FIELD_KEYS

    assert FIELD_KEYS[MaterialKindV1.QUOTE_STATE] == frozenset({
        "market", "symbol", "portfolio_relation", "last_price", "currency_raw",
        "provider_timestamp", "fetched_at", "change_basis", "baseline_price",
        "price_delta_ratio",
        "numeric_state", "trigger_kind", "trigger_reference_price",
        "trigger_horizon", "price_delta_ratio_5m", "price_delta_ratio_15m",
        "price_delta_ratio_30m", "velocity_per_min", "acceleration_per_min2",
        "reversal", "persistent_drift_ticks", "money_impact",
    })


def test_parser_rejects_float_duplicate_keys_controls_and_bounds_as_whole_batch():
    annotation = json.loads(_glm_raw())["annotations"][0]
    float_raw = json.dumps({"schema_version": 1, "annotations": [{**annotation, "summary": 1.5}]}).encode()
    duplicate_raw = b'{"schema_version":1,"schema_version":1,"annotations":[]}'
    control_raw = canonical_encode({"schema_version": 1, "annotations": [{**annotation, "summary": "\u007f"}]})
    deep = canonical_encode({"schema_version": 1, "annotations": [{**annotation, "evidence_unknowns": [[[[[[[[[[[[[[[[[]]]]]]]]]]]]]]]]]}]})
    for raw in (float_raw, duplicate_raw, control_raw, deep, b'{"schema_version":'):
        batch = parse_glm_batch(raw, ("candidate1",))
        assert batch.exit is ProviderExit.PARSE_FAIL
        assert batch.annotations == ()
