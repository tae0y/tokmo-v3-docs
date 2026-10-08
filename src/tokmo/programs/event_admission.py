"""Deterministic EVENT admission policy registry and evaluator.

This is the single localized owner of admission criteria and threshold
values. No threshold literal may remain in cadence, composition, storage,
provider adapter, or analyzer code. The evaluator is pure: it imports
only value/protocol modules and the closed criterion contracts.

§6.4: ``EventAdmissionPolicyV1`` contains schema version, policy version,
enabled ordered criterion IDs, and exact typed threshold values. Use
checked-in immutable configuration for v1.
"""

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from types import MappingProxyType
from typing import Mapping

from tokmo.values.material_pack import canonical_encode
from tokmo.values.analysis_admission import (
    AdmissionDecisionV1,
    CriterionIdV1,
    CriterionReasonV1,
    CriterionResultV1,
    MaterialDeltaV1,
)
from tokmo.values.analysis_contracts import (
    FreshnessState,
    MaterialKindV1,
    NumericStateV1,
    QuoteTriggerKindV1,
)


class EventAdmissionPolicyConfigError(ValueError):
    """Raised when a policy configuration is malformed or invalid."""


# Bounded threshold keys. Ratio thresholds must be canonical decimals in
# [0, 1]. Integer-count thresholds are non-negative finite decimals.
_RATIO_KEYS = frozenset({
    "quote_move_threshold_ratio",
    "quote_cumulative_threshold_ratio",
    "quote_move_noise_floor_ratio",
    "quote_watch_threshold_ratio",
})
_COUNT_KEYS = frozenset({
    "quote_drift_tick_threshold",
})
_THRESHOLD_KEYS = _RATIO_KEYS | _COUNT_KEYS


@dataclass(frozen=True)
class EventAdmissionPolicyV1:
    """Versioned, validated, immutable EVENT admission policy.

    The policy digest is SHA-256 of domain-separated canonical JSON
    containing schema, version, ordered criteria, and thresholds.
    """

    schema_version: int
    policy_version: str
    enabled_criteria: tuple[CriterionIdV1, ...]
    thresholds: Mapping[str, str] = field(
        default_factory=lambda: MappingProxyType({}), repr=False,
    )

    def __post_init__(self):
        if self.schema_version != 1:
            raise EventAdmissionPolicyConfigError("invalid schema_version")
        if type(self.policy_version) is not str or not self.policy_version:
            raise EventAdmissionPolicyConfigError("invalid policy_version")
        if type(self.enabled_criteria) is not tuple or not self.enabled_criteria:
            raise EventAdmissionPolicyConfigError("at least one criterion required")
        # Unique criteria
        seen: set[CriterionIdV1] = set()
        for cid in self.enabled_criteria:
            if not isinstance(cid, CriterionIdV1):
                raise EventAdmissionPolicyConfigError("invalid criterion id")
            if cid in seen:
                raise EventAdmissionPolicyConfigError("duplicate criterion id")
            seen.add(cid)
        # Validate thresholds
        validated: dict[str, str] = {}
        for key, raw in self.thresholds.items():
            if key not in _THRESHOLD_KEYS:
                raise EventAdmissionPolicyConfigError(f"unknown threshold key: {key}")
            try:
                value = Decimal(raw)
            except (InvalidOperation, TypeError) as error:
                raise EventAdmissionPolicyConfigError(
                    f"non-canonical threshold: {key}"
                ) from error
            if not value.is_finite():
                raise EventAdmissionPolicyConfigError(
                    f"non-finite threshold: {key}"
                )
            if key in _RATIO_KEYS:
                if value < 0 or value > 1:
                    raise EventAdmissionPolicyConfigError(
                        f"threshold out of range [0,1]: {key}"
                    )
            elif key in _COUNT_KEYS:
                if value < 0 or value != value.to_integral_value():
                    raise EventAdmissionPolicyConfigError(
                        f"count threshold must be non-negative integer: {key}"
                    )
            validated[key] = raw
        object.__setattr__(
            self, "thresholds", MappingProxyType(dict(validated)),
        )
        object.__setattr__(
            self, "enabled_criteria", tuple(self.enabled_criteria),
        )

    def thresholds_canonical(self) -> dict[str, str]:
        """Canonical thresholds for digest computation."""
        return dict(sorted(self.thresholds.items()))

    @property
    def policy_digest(self) -> str:
        raw = canonical_encode({
            "domain": "tokmo-v3:event-admission-policy:v1",
            "schema_version": self.schema_version,
            "policy_version": self.policy_version,
            "criteria": [c.value for c in self.enabled_criteria],
            "thresholds": self.thresholds_canonical(),
        })
        import hashlib
        return hashlib.sha256(raw).hexdigest()

    def _ordinal_of(self, criterion_id: CriterionIdV1) -> int:
        """Registry ordinal of a criterion (position in enabled_criteria)."""
        for index, cid in enumerate(self.enabled_criteria):
            if cid is criterion_id:
                return index
        return len(self.enabled_criteria)


# The default v1 policy preserves current Slice E semantics rather than
# narrowing them: all existing trigger kinds are admitted.
DEFAULT_EVENT_ADMISSION_POLICY_V1 = EventAdmissionPolicyV1(
    schema_version=1,
    policy_version="event-admission-v1",
    enabled_criteria=(
        CriterionIdV1.COMPLETED_EXECUTION,
        CriterionIdV1.OPERATIONAL_INCIDENT,
        CriterionIdV1.HOLDINGS_CHANGE,
        CriterionIdV1.QUOTE_THRESHOLD_MOVE,
        CriterionIdV1.QUOTE_REVERSAL,
        CriterionIdV1.QUOTE_PERSISTENT_DRIFT,
        CriterionIdV1.QUOTE_CUMULATIVE_DRIFT,
        CriterionIdV1.QUOTE_STATE_TRANSITION,
        CriterionIdV1.QUOTE_FIRST_OBSERVATION,
    ),
    thresholds={
        "quote_move_threshold_ratio": "0.005",
        "quote_cumulative_threshold_ratio": "0.01",
        "quote_drift_tick_threshold": "3",
        "quote_move_noise_floor_ratio": "0.001",
        "quote_watch_threshold_ratio": "0.002",
    },
)


# Closed incident-kind registries from analysis_contracts (imported indirectly
# to avoid a circular dependency on the criterion module).
_CRITICAL_INCIDENT_KINDS = frozenset({
    "execution-identity-conflict", "execution-quarantine",
    "trades-quarantine-observed",
    "holdings-identity-collision", "holdings_material_unavailable",
    "quotes_material_unavailable", "trades_material_unavailable",
    "kr_holdings_unavailable", "us_holdings_unavailable",
    "kr_holdings_missing", "us_holdings_missing",
})


def _dec(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError):
        return None


def _evaluate_criterion(
    delta: MaterialDeltaV1, criterion_id: CriterionIdV1,
    thresholds: Mapping[str, str],
) -> CriterionResultV1:
    """Evaluate one criterion against one delta.

    Returns a CriterionResultV1 with matched=True/False and a stable
    reason code. Does not short-circuit.
    """
    cf = delta.current_fields
    kind = delta.material_kind

    if criterion_id is CriterionIdV1.COMPLETED_EXECUTION:
        matched = kind is MaterialKindV1.COMPLETED_EXECUTION
        reason = (
            CriterionReasonV1.EVENT_COMPLETED_EXECUTION if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED
        )
        return CriterionResultV1(criterion_id, matched, reason)

    if criterion_id is CriterionIdV1.OPERATIONAL_INCIDENT:
        if kind is MaterialKindV1.OPERATIONAL_INCIDENT:
            incident_kind = cf.get("incident_kind", "")
            # Admit critical/changed operational incidents; resolved
            # transitions are still operational evidence.
            matched = True
            reason = CriterionReasonV1.EVENT_OPERATIONAL_INCIDENT
        else:
            matched = False
            reason = CriterionReasonV1.EVENT_NO_CRITERION_MATCHED
        return CriterionResultV1(criterion_id, matched, reason)

    if criterion_id is CriterionIdV1.HOLDINGS_CHANGE:
        if kind is MaterialKindV1.HOLDINGS_CHANGE:
            change = cf.get("change_kind", "")
            reason_map = {
                "NEW_POSITION": CriterionReasonV1.EVENT_HOLDINGS_NEW_POSITION,
                "QUANTITY_CHANGED": CriterionReasonV1.EVENT_HOLDINGS_QUANTITY_CHANGED,
                "AVG_PRICE_CHANGED": CriterionReasonV1.EVENT_HOLDINGS_AVG_PRICE_CHANGED,
                "POSITION_CLOSED": CriterionReasonV1.EVENT_HOLDINGS_POSITION_CLOSED,
            }
            reason = reason_map.get(change, CriterionReasonV1.EVENT_NO_CRITERION_MATCHED)
            matched = change in reason_map
        else:
            matched = False
            reason = CriterionReasonV1.EVENT_NO_CRITERION_MATCHED
        return CriterionResultV1(criterion_id, matched, reason)

    # Quote criteria
    if kind is not MaterialKindV1.QUOTE_STATE:
        return CriterionResultV1(
            criterion_id, False, CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    move_threshold = _dec(thresholds.get("quote_move_threshold_ratio"))
    cumulative_threshold = _dec(thresholds.get("quote_cumulative_threshold_ratio"))
    drift_tick_threshold_raw = thresholds.get("quote_drift_tick_threshold", "3")
    try:
        drift_tick_threshold = int(drift_tick_threshold_raw)
    except (ValueError, TypeError):
        drift_tick_threshold = 3

    trigger = cf.get("trigger_kind", "")
    numeric_state = cf.get("numeric_state", "")

    if criterion_id is CriterionIdV1.QUOTE_THRESHOLD_MOVE:
        ratio = _dec(cf.get("price_delta_ratio"))
        matched = (
            trigger == QuoteTriggerKindV1.THRESHOLD_MOVE.value
            or (ratio is not None and move_threshold is not None
                and abs(ratio) >= move_threshold)
        )
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_THRESHOLD_MOVE if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
            observed_value=(str(ratio) if ratio is not None else None),
            threshold_value=(str(move_threshold) if move_threshold is not None else None),
        )

    if criterion_id is CriterionIdV1.QUOTE_REVERSAL:
        reversal = cf.get("reversal", False)
        matched = bool(reversal) or trigger == QuoteTriggerKindV1.REVERSAL.value
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_REVERSAL if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    if criterion_id is CriterionIdV1.QUOTE_PERSISTENT_DRIFT:
        streak = cf.get("persistent_drift_ticks", 0)
        try:
            streak_val = int(streak)
        except (ValueError, TypeError):
            streak_val = 0
        matched = abs(streak_val) >= drift_tick_threshold or (
            trigger == QuoteTriggerKindV1.PERSISTENT_DRIFT.value
        )
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_PERSISTENT_DRIFT if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    if criterion_id is CriterionIdV1.QUOTE_CUMULATIVE_DRIFT:
        matched = trigger == QuoteTriggerKindV1.CUMULATIVE_DRIFT.value
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_CUMULATIVE_DRIFT if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    if criterion_id is CriterionIdV1.QUOTE_STATE_TRANSITION:
        matched = trigger == QuoteTriggerKindV1.STATE_TRANSITION.value
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_STATE_TRANSITION if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    if criterion_id is CriterionIdV1.QUOTE_FIRST_OBSERVATION:
        matched = trigger == QuoteTriggerKindV1.FIRST_OBSERVATION.value
        return CriterionResultV1(
            criterion_id, matched,
            CriterionReasonV1.EVENT_QUOTE_FIRST_OBSERVATION if matched
            else CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
        )

    # Unknown criterion: never admit
    return CriterionResultV1(
        criterion_id, False, CriterionReasonV1.EVENT_NO_CRITERION_MATCHED,
    )


def evaluate_event_admission(
    policy: EventAdmissionPolicyV1,
    deltas: tuple[MaterialDeltaV1, ...],
    *,
    now: datetime,
) -> tuple[AdmissionDecisionV1, ...]:
    """Evaluate every enabled criterion applicable to each delta.

    Evaluates all criteria (no short-circuit). Results are sorted by the
    registry's explicit ordinal. Decision is ADMIT if any result matched
    and required freshness is healthy; otherwise REJECT.

    §6.5 and §8: do not admit financial EVENT analysis whose triggering
    evidence is stale, while preserving an operational incident path.
    """
    evaluated_at = now
    decisions: list[AdmissionDecisionV1] = []
    for delta in deltas:
        results: list[CriterionResultV1] = []
        for criterion_id in policy.enabled_criteria:
            result = _evaluate_criterion(delta, criterion_id, policy.thresholds)
            results.append(result)
        # Sort by registry ordinal (stable)
        results.sort(key=lambda r: policy._ordinal_of(r.criterion_id))
        any_matched = any(r.matched for r in results)
        # Freshness gate: stale required material cannot support a healthy
        # financial conclusion. Operational incidents bypass this gate.
        stale = delta.freshness_state is FreshnessState.STALE
        is_operational = delta.material_kind is MaterialKindV1.OPERATIONAL_INCIDENT
        if any_matched and (not stale or is_operational):
            decision = "ADMIT"
        elif any_matched and stale and not is_operational:
            # Replace all reason codes with the fail-closed stale reason
            results = [CriterionResultV1(
                CriterionIdV1.COMPLETED_EXECUTION,  # placeholder criterion
                False,
                CriterionReasonV1.EVENT_REQUIRED_MATERIAL_STALE,
            )]
            decision = "REJECT"
        else:
            decision = "REJECT"
        decisions.append(AdmissionDecisionV1(
            delta_id=delta.delta_id,
            decision=decision,
            matched_results=tuple(results),
            policy_version=policy.policy_version,
            policy_digest=policy.policy_digest,
            evaluated_at=evaluated_at,
        ))
    return tuple(decisions)
