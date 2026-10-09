"""Pure, immutable event-admission value contracts.

These types are derived operational evidence, not canonical facts.
They carry no account identity, credentials, filesystem paths, or raw
provider body. They are intentionally trigger-agnostic: a future
AI-ADMIT-001 admission evaluator may emit the same
:class:`AdmissionDecisionV1` without changing these contracts.
"""

import hashlib
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from tokmo.values.analysis_contracts import (
    ChangeBasis,
    EvidenceRefV1,
    FreshnessState,
    MaterialKindV1,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import require_token


class CriterionIdV1(str, Enum):
    """Closed stable criterion identifier set.

    The ordinal in this enum defines registry evaluation order
    (§6.5: deterministic registry order, never discovery order).
    """

    COMPLETED_EXECUTION = "completed_execution"
    OPERATIONAL_INCIDENT = "operational_incident"
    HOLDINGS_CHANGE = "holdings_change"
    QUOTE_THRESHOLD_MOVE = "quote_threshold_move"
    QUOTE_REVERSAL = "quote_reversal"
    QUOTE_PERSISTENT_DRIFT = "quote_persistent_drift"
    QUOTE_CUMULATIVE_DRIFT = "quote_cumulative_drift"
    QUOTE_STATE_TRANSITION = "quote_state_transition"
    QUOTE_FIRST_OBSERVATION = "quote_first_observation"


class CriterionReasonV1(str, Enum):
    """Closed stable machine reason codes.

    Reason codes are criterion-specific, stable, and do not embed
    values, symbols, thresholds, or prose.
    """

    EVENT_COMPLETED_EXECUTION = "event_completed_execution"
    EVENT_OPERATIONAL_INCIDENT = "event_operational_incident"
    EVENT_HOLDINGS_NEW_POSITION = "event_holdings_new_position"
    EVENT_HOLDINGS_QUANTITY_CHANGED = "event_holdings_quantity_changed"
    EVENT_HOLDINGS_AVG_PRICE_CHANGED = "event_holdings_avg_price_changed"
    EVENT_HOLDINGS_POSITION_CLOSED = "event_holdings_position_closed"
    EVENT_QUOTE_THRESHOLD_MOVE = "event_quote_threshold_move"
    EVENT_QUOTE_REVERSAL = "event_quote_reversal"
    EVENT_QUOTE_PERSISTENT_DRIFT = "event_quote_persistent_drift"
    EVENT_QUOTE_CUMULATIVE_DRIFT = "event_quote_cumulative_drift"
    EVENT_QUOTE_STATE_TRANSITION = "event_quote_state_transition"
    EVENT_QUOTE_FIRST_OBSERVATION = "event_quote_first_observation"
    EVENT_NO_CRITERION_MATCHED = "event_no_criterion_matched"
    EVENT_REQUIRED_MATERIAL_STALE = "event_required_material_stale"


_AWARE_DATETIME = None


def _aware(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be aware")


@dataclass(frozen=True)
class CriterionResultV1:
    """One criterion evaluation result for one delta."""

    criterion_id: CriterionIdV1
    matched: bool
    reason_code: CriterionReasonV1
    observed_value: str | None = None
    threshold_value: str | None = None

    def __post_init__(self):
        if not isinstance(self.criterion_id, CriterionIdV1):
            raise ValueError("invalid criterion id")
        if type(self.matched) is not bool:
            raise ValueError("invalid matched flag")
        if not isinstance(self.reason_code, CriterionReasonV1):
            raise ValueError("invalid reason code")


@dataclass(frozen=True)
class AdmissionDecisionV1:
    """Admission decision for one delta: ADMIT or REJECT.

    All matched results are preserved in registry order so multi-fire
    evidence is reproducible (§6.5).
    """

    delta_id: str
    decision: str  # "ADMIT" | "REJECT"
    matched_results: tuple[CriterionResultV1, ...]
    policy_version: str
    policy_digest: str
    evaluated_at: datetime

    def __post_init__(self):
        require_token(self.delta_id, "delta_id")
        if self.decision not in ("ADMIT", "REJECT"):
            raise ValueError("invalid admission decision")
        if type(self.matched_results) is not tuple or any(
            not isinstance(r, CriterionResultV1) for r in self.matched_results
        ):
            raise ValueError("invalid criterion results")
        require_token(self.policy_version, "policy_version")
        if len(self.policy_digest) != 64:
            raise ValueError("invalid policy digest")
        _aware(self.evaluated_at, "evaluated_at")

    @property
    def admission_reason_codes(self) -> tuple[str, ...]:
        """Ordered non-empty tuple of matched reason codes (ADMIT only)."""
        return tuple(r.reason_code.value for r in self.matched_results if r.matched)


@dataclass(frozen=True)
class MaterialDeltaV1:
    """A deterministic, typed material delta produced from the Slice D barrier.

    A delta is derived evidence, not a canonical fact. It may not contain
    ``account_scope``, raw account identity, credentials, filesystem paths,
    or raw provider body.

    Identity: ``sha256(canonical_encode({domain, tick_id, material_kind,
    subject_id, material_binding_digest, normalized current/baseline fields}))[:32]``
    """

    subject_id: str
    material_kind: MaterialKindV1
    market: str
    symbol: str | None
    tick_id: int
    material_binding_digest: str
    evidence_refs: tuple[EvidenceRefV1, ...]
    current_fields: Mapping[str, object]
    baseline_fields: Mapping[str, object] | None
    change_basis: ChangeBasis
    freshness_state: FreshnessState
    created_at: datetime
    delta_id: str = ""

    def __post_init__(self):
        require_token(self.subject_id, "subject_id")
        if not isinstance(self.material_kind, MaterialKindV1):
            raise ValueError("invalid material kind")
        if self.market not in ("KR", "US", "GLOBAL"):
            raise ValueError("invalid market")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid tick_id")
        if len(self.material_binding_digest) != 64:
            raise ValueError("invalid material_binding_digest")
        if (
            type(self.evidence_refs) is not tuple
            or not self.evidence_refs
            or any(not isinstance(r, EvidenceRefV1) for r in self.evidence_refs)
        ):
            raise ValueError("invalid evidence_refs")
        if not isinstance(self.current_fields, Mapping):
            raise ValueError("invalid current_fields")
        if self.baseline_fields is not None and not isinstance(
            self.baseline_fields, Mapping
        ):
            raise ValueError("invalid baseline_fields")
        if not isinstance(self.change_basis, ChangeBasis):
            raise ValueError("invalid change_basis")
        if not isinstance(self.freshness_state, FreshnessState):
            raise ValueError("invalid freshness_state")
        _aware(self.created_at, "created_at")
        # Freeze mappings
        object.__setattr__(
            self,
            "current_fields",
            MappingProxyType(dict(self.current_fields)),
        )
        if self.baseline_fields is not None:
            object.__setattr__(
                self,
                "baseline_fields",
                MappingProxyType(dict(self.baseline_fields)),
            )
        # Privacy: no account identity may appear
        for mapping in (self.current_fields, self.baseline_fields or {}):
            if "account_scope" in mapping:
                raise ValueError("private identity forbidden")
        # Compute deterministic delta_id
        computed = self._compute_delta_id()
        if self.delta_id and self.delta_id != computed:
            raise ValueError("delta_id mismatch")
        object.__setattr__(self, "delta_id", computed)

    def _compute_delta_id(self) -> str:
        payload = {
            "v": 1,
            "domain": "tokmo-v3:material-delta:v1",
            "tick_id": self.tick_id,
            "material_kind": self.material_kind.value,
            "subject_id": self.subject_id,
            "material_binding_digest": self.material_binding_digest,
            "current_fields": dict(self.current_fields),
            "baseline_fields": (
                dict(self.baseline_fields) if self.baseline_fields is not None else None
            ),
        }
        return hashlib.sha256(canonical_encode(payload)).hexdigest()[:32]
