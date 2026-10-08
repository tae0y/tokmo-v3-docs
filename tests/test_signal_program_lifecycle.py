"""G1-A RED→GREEN lifecycle tests (SDS §8.1 LC-01..LC-12).

Exercises ProgramLifecycleSpecV1, ProgramLifecycleStateV1,
evaluate_lifecycle_status, and lifecycle-reason semantics defined in
SDS §3.1–3.7.
"""

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from tokmo.values.signal_program_lifecycle import (
    LifecycleReasonCode,
    ProgramKindV1,
    ProgramLifecycleSpecV1,
    ProgramLifecycleStateV1,
    ProgramLifecycleStatusV1,
    evaluate_lifecycle_status,
)

# --------------------------------------------------------------------------
# Shared canonical datetimes
# --------------------------------------------------------------------------

KST = timezone(timedelta(hours=9))
ACTIVATED = datetime(2026, 1, 1, 9, 0, tzinfo=KST)
REVIEW_DUE_90 = ACTIVATED + timedelta(days=90)


def _digest(text: str = "canonical-spec-text") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _core_spec(**overrides) -> ProgramLifecycleSpecV1:
    defaults = dict(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=ACTIVATED,
        review_due_at=REVIEW_DUE_90,
        expires_at=None,
        review_cadence_days=90,
        spec_digest=_digest(),
    )
    defaults.update(overrides)
    return ProgramLifecycleSpecV1(**defaults)


def _hypothesis_spec(**overrides) -> ProgramLifecycleSpecV1:
    activated = overrides.pop("activated_at", ACTIVATED)
    review = overrides.pop("review_due_at", ACTIVATED + timedelta(days=45))
    expires = overrides.pop("expires_at", ACTIVATED + timedelta(days=120))
    defaults = dict(
        program_id="AI-CAPEX-001",
        version="1.0.0",
        kind=ProgramKindV1.HYPOTHESIS_SENSOR,
        activated_at=activated,
        review_due_at=review,
        expires_at=expires,
        review_cadence_days=45,
        spec_digest=_digest("hypothesis"),
    )
    defaults.update(overrides)
    return ProgramLifecycleSpecV1(**defaults)


# ==========================================================================
# LC-01: CORE spec with expires_at != None → ValueError
# ==========================================================================

def test_lc01_core_with_expires_at_rejects():
    with pytest.raises(ValueError):
        _core_spec(expires_at=ACTIVATED + timedelta(days=365))


# ==========================================================================
# LC-02: HYPOTHESIS spec without expires_at → ValueError
# ==========================================================================

def test_lc02_hypothesis_without_expires_at_rejects():
    with pytest.raises(ValueError):
        ProgramLifecycleSpecV1(
            program_id="AI-CAPEX-001",
            version="1.0.0",
            kind=ProgramKindV1.HYPOTHESIS_SENSOR,
            activated_at=ACTIVATED,
            review_due_at=ACTIVATED + timedelta(days=45),
            expires_at=None,
            review_cadence_days=45,
            spec_digest=_digest("h"),
        )


# ==========================================================================
# LC-03: HYPOTHESIS activated >= review_due → ValueError
# ==========================================================================

def test_lc03_hypothesis_activated_gte_review_due_rejects():
    with pytest.raises(ValueError):
        _hypothesis_spec(
            activated_at=ACTIVATED,
            review_due_at=ACTIVATED,           # equal → fail
            expires_at=ACTIVATED + timedelta(days=10),
        )


# ==========================================================================
# LC-04: HYPOTHESIS review_due >= expires_at → ValueError
# ==========================================================================

def test_lc04_hypothesis_review_gte_expires_rejects():
    with pytest.raises(ValueError):
        _hypothesis_spec(
            activated_at=ACTIVATED,
            review_due_at=ACTIVATED + timedelta(days=10),
            expires_at=ACTIVATED + timedelta(days=10),  # equal → fail
        )


# ==========================================================================
# LC-05: Naive activated_at → ValueError
# ==========================================================================

def test_lc05_naive_activated_at_rejects():
    with pytest.raises(ValueError):
        ProgramLifecycleSpecV1(
            program_id="AI-MACRO-001",
            version="1.0.0",
            kind=ProgramKindV1.CORE_ANALYZER,
            activated_at=datetime(2026, 1, 1, 9, 0),  # naive
            review_due_at=datetime(2026, 4, 1, 9, 0),  # naive
            expires_at=None,
            review_cadence_days=90,
            spec_digest=_digest(),
        )


# ==========================================================================
# LC-06: as_of < activated_at → SHADOW
# ==========================================================================

def test_lc06_shadow_before_activation():
    spec = _core_spec()
    as_of = ACTIVATED - timedelta(days=1)
    state = evaluate_lifecycle_status(
        spec, as_of=as_of, last_evidence_at=None,
        evidence_count=0, renewal_count=0,
    )
    assert state.status is ProgramLifecycleStatusV1.SHADOW


# ==========================================================================
# LC-07: CORE as_of >= review_due_at → REVIEW_DUE, evaluation continues
# ==========================================================================

def test_lc07_review_due_when_past_review_date():
    spec = _core_spec()
    as_of = REVIEW_DUE_90 + timedelta(days=1)
    state = evaluate_lifecycle_status(
        spec, as_of=as_of, last_evidence_at=as_of - timedelta(days=1),
        evidence_count=5, renewal_count=0,
    )
    assert state.status is ProgramLifecycleStatusV1.REVIEW_DUE


# ==========================================================================
# LC-08: HYPOTHESIS as_of >= expires_at → EXPIRED
# ==========================================================================

def test_lc08_expired_when_past_hypothesis_expiry():
    spec = _hypothesis_spec()
    as_of = ACTIVATED + timedelta(days=121)
    state = evaluate_lifecycle_status(
        spec, as_of=as_of, last_evidence_at=None,
        evidence_count=0, renewal_count=0,
    )
    assert state.status is ProgramLifecycleStatusV1.EXPIRED


# ==========================================================================
# LC-09b: CORE spec + EXPIRED status construction → ValueError
# ==========================================================================

def test_lc09b_core_expired_state_construction_rejects():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=365)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.EXPIRED,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


# ==========================================================================
# LC-09c: same program_id, different version → different spec_digest
# ==========================================================================

def test_lc09c_version_distinctness_produces_different_digest():
    """LC-09c: two specs with same program_id but different version (and thus
    different spec_digest) construct as distinct valid specs. The spec_digest
    field is caller-supplied and validated by ProgramLifecycleSpecV1."""
    spec_v1 = _core_spec(version="1.0.0", spec_digest=_digest("spec-text-v1"))
    spec_v2 = _core_spec(version="2.0.0", spec_digest=_digest("spec-text-v2"))
    assert spec_v1.version != spec_v2.version
    assert spec_v1.spec_digest != spec_v2.spec_digest
    assert spec_v1.program_id == spec_v2.program_id


# ==========================================================================
# LC-10: semantic drift changes spec_digest
# ==========================================================================

def test_lc10_semantic_drift_changes_digest():
    """LC-10: different spec text produces different spec_digest.
    Two specs with different spec_digest values are both valid but distinct."""
    spec_a = _core_spec(spec_digest=_digest("text-a"))
    spec_b = _core_spec(spec_digest=_digest("text-b"))
    assert spec_a.spec_digest != spec_b.spec_digest


# ==========================================================================
# LC-11: review_cadence_days = 0 → ValueError
# ==========================================================================

def test_lc11_review_cadence_zero_rejects():
    with pytest.raises(ValueError):
        _core_spec(review_cadence_days=0)


def test_lc11b_review_cadence_negative_rejects():
    with pytest.raises(ValueError):
        _core_spec(review_cadence_days=-1)


def test_lc11c_review_cadence_over_365_rejects():
    with pytest.raises(ValueError):
        _core_spec(review_cadence_days=366)


# ==========================================================================
# LC-12: CORE AI-MACRO-001 exact spec — activated + 90d = review_due, no expiry
# ==========================================================================

def test_lc12_core_ai_macro_001_exact_spec():
    spec = _core_spec()
    assert spec.expires_at is None
    assert spec.review_due_at == spec.activated_at + timedelta(days=90)
    assert spec.review_cadence_days == 90


# ==========================================================================
# Additional spec validation (§3.3)
# ==========================================================================

def test_program_id_empty_rejects():
    with pytest.raises(ValueError):
        _core_spec(program_id="")


def test_program_id_too_long_rejects():
    with pytest.raises(ValueError):
        _core_spec(program_id="A" * 129)


def test_program_id_bad_chars_rejects():
    with pytest.raises(ValueError):
        _core_spec(program_id="AI MACRO!")


def test_version_bad_format_rejects():
    with pytest.raises(ValueError):
        _core_spec(version="1.0")


def test_spec_digest_wrong_length_rejects():
    with pytest.raises(ValueError):
        _core_spec(spec_digest="abc")


def test_spec_digest_uppercase_rejects():
    with pytest.raises(ValueError):
        _core_spec(spec_digest="A" * 64)


def test_naive_review_due_at_rejects():
    with pytest.raises(ValueError):
        ProgramLifecycleSpecV1(
            program_id="AI-MACRO-001",
            version="1.0.0",
            kind=ProgramKindV1.CORE_ANALYZER,
            activated_at=ACTIVATED,
            review_due_at=datetime(2026, 4, 1, 9, 0),  # naive
            expires_at=None,
            review_cadence_days=90,
            spec_digest=_digest(),
        )


# ==========================================================================
# State invariants (§3.4)
# ==========================================================================

def test_active_state_normal():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    state = evaluate_lifecycle_status(
        spec, as_of=as_of, last_evidence_at=None,
        evidence_count=0, renewal_count=0,
    )
    assert state.status is ProgramLifecycleStatusV1.ACTIVE


def test_core_renewal_count_nonzero_rejects():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.ACTIVE,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=1,
            as_of=as_of,
        )


def test_shadow_state_with_as_of_past_activation_rejects():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=1)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.SHADOW,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


def test_active_state_before_activation_rejects():
    """ACTIVE requires as_of >= activated_at."""
    spec = _core_spec()
    as_of = ACTIVATED - timedelta(days=1)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.ACTIVE,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


def test_dormant_state_constructs_and_is_explicit():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.DORMANT,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    assert state.status is ProgramLifecycleStatusV1.DORMANT


def test_retired_state_constructs_and_is_explicit():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.RETIRED,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    assert state.status is ProgramLifecycleStatusV1.RETIRED


def test_expired_hypothesis_state_consistency():
    """EXPIRED HYPOTHESIS_SENSOR with as_of >= expires_at constructs ok."""
    spec = _hypothesis_spec()
    as_of = ACTIVATED + timedelta(days=121)
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.EXPIRED,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    assert state.status is ProgramLifecycleStatusV1.EXPIRED


def test_expired_hypothesis_before_expiry_rejects():
    """status=EXPIRED but as_of < expires_at → ValueError."""
    spec = _hypothesis_spec()
    as_of = ACTIVATED + timedelta(days=10)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.EXPIRED,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


def test_last_evidence_future_rejects():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.ACTIVE,
            last_evidence_at=as_of + timedelta(days=1),
            evidence_count=1,
            renewal_count=0,
            as_of=as_of,
        )


def test_naive_as_of_state_rejects():
    spec = _core_spec()
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.ACTIVE,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=datetime(2026, 2, 1, 9, 0),  # naive
        )


def test_review_due_active_status_before_review_rejects():
    """status=REVIEW_DUE but as_of < review_due_at → ValueError."""
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.REVIEW_DUE,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


def test_active_status_at_or_after_review_rejects():
    """status=ACTIVE but as_of >= review_due_at → ValueError."""
    spec = _core_spec()
    as_of = REVIEW_DUE_90 + timedelta(days=1)
    with pytest.raises(ValueError):
        ProgramLifecycleStateV1(
            spec=spec,
            status=ProgramLifecycleStatusV1.ACTIVE,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )


# ==========================================================================
# Status enum closed-set check (§3.2)
# ==========================================================================

def test_lifecycle_status_closed_set():
    members = {m.value for m in ProgramLifecycleStatusV1}
    assert members == {
        "SHADOW", "ACTIVE", "REVIEW_DUE", "DORMANT", "RETIRED", "EXPIRED",
    }


def test_program_kind_closed_set():
    members = {m.value for m in ProgramKindV1}
    assert members == {"CORE_ANALYZER", "HYPOTHESIS_SENSOR"}


def test_lifecycle_reason_code_closed_set():
    members = {m.value for m in LifecycleReasonCode}
    assert members == {
        "OK", "NOT_YET_ACTIVE", "REVIEW_DUE", "EXPIRED", "DORMANT", "RETIRED",
    }


# ==========================================================================
# frozen=True immutability
# ==========================================================================

def test_spec_is_frozen():
    spec = _core_spec()
    with pytest.raises(Exception):
        spec.program_id = "mutated"  # type: ignore[misc]


def test_state_is_frozen():
    spec = _core_spec()
    as_of = ACTIVATED + timedelta(days=30)
    state = ProgramLifecycleStateV1(
        spec=spec, status=ProgramLifecycleStatusV1.ACTIVE,
        last_evidence_at=None, evidence_count=0, renewal_count=0, as_of=as_of,
    )
    with pytest.raises(Exception):
        state.status = ProgramLifecycleStatusV1.SHADOW  # type: ignore[misc]
