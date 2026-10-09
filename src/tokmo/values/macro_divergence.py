"""Macro divergence types, formulas, and evidence pack (SDS §5–6).

Pure value objects: component enum, state enum, window summary, evidence
pack, and the canonical digest algorithm. No I/O, no wall-clock.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any

from tokmo.values import canonical_decimal
from tokmo.values.macro_observations import MacroCoverageState, MacroSourceRefV1

__all__ = [
    # Constants (§5.1)
    "LOOKBACK_MAX",
    "LOOKBACK_MIN",
    "COMMON_WINDOW_MIN",
    "OUTFLOW_WINDOW",
    "EXHAUSTION_PRIOR_WINDOW",
    "EXHAUSTION_MIN_INVESTOR",
    "FX_RETURN_WINDOW",
    "EXHAUSTION_IMPROVEMENT_RATIO",
    "HIGH_PERCENTILE_THRESHOLD",
    "FOREIGN_OUTFLOW_STRESS_THRESHOLD",
    "EXHAUSTION_FX_RETURN_THRESHOLD",
    "STALENESS_CEILING_CALENDAR_DAYS",
    "FORMULA_VERSION",
    "PACK_BYTE_CAP",
    # Enums
    "MacroDivergenceComponent",
    "MacroDivergenceState",
    "ConfidenceCeiling",
    # Data types
    "MacroDivergenceComponentV1",
    "MacroWindowSummaryV1",
    "MacroSymbolContactV1",
    "MacroDivergencePackV1",
    # Functions
    "compute_pack_digest",
    "percentile_rank",
    "map_confidence_ceiling",
]


# ==========================================================================
# §5.1  Constants (frozen at SDS level)
# ==========================================================================

LOOKBACK_MAX = 100
LOOKBACK_MIN = 60
COMMON_WINDOW_MIN = 60
OUTFLOW_WINDOW = 20
EXHAUSTION_PRIOR_WINDOW = 20
EXHAUSTION_MIN_INVESTOR = OUTFLOW_WINDOW + EXHAUSTION_PRIOR_WINDOW  # = 40
FX_RETURN_WINDOW = 8

EXHAUSTION_IMPROVEMENT_RATIO = Decimal("0.5")
HIGH_PERCENTILE_THRESHOLD = Decimal("0.90")
FOREIGN_OUTFLOW_STRESS_THRESHOLD = Decimal(-20000000000000)
EXHAUSTION_FX_RETURN_THRESHOLD = Decimal("-0.03")
STALENESS_CEILING_CALENDAR_DAYS = 4
FORMULA_VERSION = "macro-divergence-v1"
PACK_BYTE_CAP = 32768


# ==========================================================================
# §6.2  MacroDivergenceState
# ==========================================================================


class MacroDivergenceState(Enum):
    INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
    NO_MATERIAL_DIVERGENCE = "NO_MATERIAL_DIVERGENCE"
    INDEX_HIGH_FX_PROXY_HIGH = "INDEX_HIGH_FX_PROXY_HIGH"
    FOREIGN_OUTFLOW_CONFIRMATION = "FOREIGN_OUTFLOW_CONFIRMATION"
    OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL = "OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL"


# ==========================================================================
# §6.1  MacroDivergenceComponent
# ==========================================================================


class MacroDivergenceComponent(Enum):
    INDEX_HIGH_FX_PROXY_HIGH = "INDEX_HIGH_FX_PROXY_HIGH"
    FOREIGN_OUTFLOW_CONFIRMATION = "FOREIGN_OUTFLOW_CONFIRMATION"
    OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL = "OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL"


class ConfidenceCeiling(Enum):
    DIRECT_FLOW_DATA = "DIRECT_FLOW_DATA"
    PROXY_ROLL_SENSITIVE = "PROXY_ROLL_SENSITIVE"
    NOT_APPLICABLE = "NOT_APPLICABLE"


# ==========================================================================
# §5.2  Percentile rank (max-rank, inclusive <=)
# ==========================================================================


def percentile_rank(window: list[Decimal], current: Decimal) -> Decimal:
    """Empirical percentile using max-rank: count(close <= current) / len.

    Ties are inclusive: all values equal to current contribute.
    """
    if not window:
        raise ValueError("percentile_rank: empty window")
    count_le = sum(1 for w in window if w <= current)
    return Decimal(count_le) / Decimal(len(window))


# ==========================================================================
# §5.7  Confidence ceiling mapping
# ==========================================================================

_CONFIDENCE_CEILING: dict[str, ConfidenceCeiling] = {
    MacroDivergenceState.INDEX_HIGH_FX_PROXY_HIGH.value: ConfidenceCeiling.PROXY_ROLL_SENSITIVE,
    MacroDivergenceState.FOREIGN_OUTFLOW_CONFIRMATION.value: ConfidenceCeiling.DIRECT_FLOW_DATA,
    MacroDivergenceState.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL.value: ConfidenceCeiling.PROXY_ROLL_SENSITIVE,
}


def map_confidence_ceiling(primary_state: str) -> ConfidenceCeiling:
    return _CONFIDENCE_CEILING.get(primary_state, ConfidenceCeiling.NOT_APPLICABLE)


# ==========================================================================
# §6.1  MacroDivergenceComponentV1
# ==========================================================================

_DETAILS_MAX_KEYS = 20
_DETAILS_VALUE_MAX_BYTES = 200
_REASON_MAX_BYTES = 1000


@dataclass(frozen=True)
class MacroDivergenceComponentV1:
    component: MacroDivergenceComponent
    fired: bool
    reason: str
    details: dict[str, str] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.reason, str):
            raise ValueError("reason must be str")
        if len(self.reason.encode("utf-8")) > _REASON_MAX_BYTES:
            raise ValueError("reason exceeds 1000 bytes")
        if not isinstance(self.details, dict):
            raise ValueError("details must be dict")
        if len(self.details) > _DETAILS_MAX_KEYS:
            raise ValueError("details exceeds 20 keys")
        for k, v in self.details.items():
            if not isinstance(k, str) or not isinstance(v, str):
                raise ValueError("details keys/values must be str")
            if len((k + v).encode("utf-8")) > _DETAILS_VALUE_MAX_BYTES:
                raise ValueError(f"details entry exceeds 200 bytes: {k}")


# ==========================================================================
# §6.3  MacroWindowSummaryV1
# ==========================================================================


@dataclass(frozen=True)
class MacroWindowSummaryV1:
    as_of: str
    latest_common_session: str
    common_window_count: int
    kospi_latest_close: str
    proxy_latest_close: str
    kospi_percentile: str | None
    proxy_percentile: str | None
    foreign_rolling_flow_20: str | None
    foreign_prior_flow_20: str | None
    fx_return_8: str | None


# ==========================================================================
# §6.4  MacroSymbolContactV1
# ==========================================================================


@dataclass(frozen=True)
class MacroSymbolContactV1:
    symbol: str
    role: str
    latest_session: str
    latest_close: str | None


# ==========================================================================
# §6.6  Canonical digest algorithm
# ==========================================================================


def _serialize_for_digest(obj: Any) -> Any:
    """Recursively convert dataclass/enum/Decimal/date to canonical forms.

    Per §6.6 pseudocode: json.dumps(..., default=...) calls this hook only
    for non-JSON-native types. Bool/int/str/float pass through as JSON
    primitives. The §6.1 "details values are strings" invariant is enforced
    at MacroDivergenceComponentV1 construction time, so by the time the
    serializer sees a details dict its values are already str.
    """
    if obj is None:
        return None
    if isinstance(obj, Enum):
        return obj.value
    if isinstance(obj, Decimal):
        return canonical_decimal(obj)
    if isinstance(obj, datetime):
        utc = obj.astimezone(UTC)
        return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond:06d}Z"
    if isinstance(obj, date):
        return obj.isoformat()
    if is_dataclass(obj) and not isinstance(obj, type):
        return {
            f.name: _serialize_for_digest(getattr(obj, f.name)) for f in fields(obj)
        }
    if isinstance(obj, dict):
        return {k: _serialize_for_digest(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_serialize_for_digest(item) for item in obj]
    # str, int, bool, float pass through as JSON primitives (§6.6 pseudocode).
    return obj


def compute_pack_digest(pack_fields: dict) -> str:
    """SHA-256 over canonical JSON serialization (§6.6).

    Per §6.1 aggregate cap: if the canonical form exceeds PACK_BYTE_CAP
    (32768 bytes), evidence/counter_evidence items are tail-dropped
    deterministically until under cap before hashing. Well-formed packs
    are well under 32 KB; this is a safety bound.
    """
    serialized = _serialize_for_digest(pack_fields)

    # §6.1: truncate canonical form if it exceeds 32 KB.
    truncated = _apply_byte_cap(serialized)

    canonical = json.dumps(
        truncated,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _apply_byte_cap(fields_dict: dict) -> dict:
    """Deterministically tail-drop evidence/counter_evidence if over 32 KB.

    Per §6.1: "the pack_digest is computed over the truncated canonical form
    (evidence/counter_evidence items dropped from the end until under cap)."
    """
    import copy

    result = copy.deepcopy(fields_dict)
    for _ in range(100):  # safety bound on iterations
        candidate = json.dumps(
            result,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
            default=str,
        )
        if len(candidate.encode("utf-8")) <= PACK_BYTE_CAP:
            return result
        # Tail-drop counter_evidence first, then evidence
        if result.get("counter_evidence") and len(result["counter_evidence"]) > 0:
            result["counter_evidence"] = result["counter_evidence"][:-1]
        elif result.get("evidence") and len(result["evidence"]) > 0:
            result["evidence"] = result["evidence"][:-1]
        else:
            return result  # nothing left to drop
    return result


# ==========================================================================
# §6.5  MacroDivergencePackV1
# ==========================================================================

_EVIDENCE_MAX_ITEMS = 20
_COUNTER_EVIDENCE_MAX_ITEMS = 10
_LIMITATIONS_MAX_ITEMS = 10
_ITEM_MAX_BYTES = 500

_PACK_DEFAULT_LIMITATIONS = (
    "261240 is KODEX 미국달러선물 ETF, not spot USD/KRW. "
    "Futures roll premium may cause ±1% systematic bias vs spot.",
    "Contango roll dates may produce discontinuous price drops unrelated to "
    "spot FX movement (review FP2 scenario).",
    "100-session lookback window; pre-window regime unobserved.",
    "Single regime sample (2026-03~2026-08); thresholds are not proven optimal.",
)


def _validate_str_tuple(value: tuple, max_items: int, field_name: str) -> None:
    if not isinstance(value, tuple):
        raise ValueError(f"{field_name} must be tuple")
    if len(value) > max_items:
        raise ValueError(f"{field_name} exceeds {max_items} items")
    for item in value:
        if not isinstance(item, str):
            raise ValueError(f"{field_name} items must be str")
        if len(item.encode("utf-8")) > _ITEM_MAX_BYTES:
            raise ValueError(f"{field_name} item exceeds {_ITEM_MAX_BYTES} bytes")


@dataclass(frozen=True)
class MacroDivergencePackV1:
    program_id: str
    program_version: str
    program_kind: str
    formula_version: str
    spec_digest: str = field(repr=False)
    as_of: str
    lifecycle_status: str
    lifecycle_reason: str
    lifecycle_review_due_at: str
    coverage_state: MacroCoverageState | None
    coverage_reason: str
    components: tuple[MacroDivergenceComponentV1, ...] = field(repr=False)
    primary_state: str
    window_summary: MacroWindowSummaryV1
    symbol_contacts: tuple[MacroSymbolContactV1, ...] = field(repr=False)
    evidence: tuple[str, ...] = field(default_factory=tuple, repr=False)
    counter_evidence: tuple[str, ...] = field(default_factory=tuple, repr=False)
    confidence_ceiling: str = ConfidenceCeiling.NOT_APPLICABLE.value
    limitations: tuple[str, ...] = field(
        default_factory=lambda: _PACK_DEFAULT_LIMITATIONS, repr=False
    )
    source_refs: tuple[MacroSourceRefV1, ...] = field(default=(), repr=False)
    pack_digest: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        # Validate closed component set: exactly 3 in canonical order
        if not isinstance(self.components, tuple) or len(self.components) != 3:
            raise ValueError("components must have exactly 3 items")
        expected_order = (
            MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
            MacroDivergenceComponent.FOREIGN_OUTFLOW_CONFIRMATION,
            MacroDivergenceComponent.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL,
        )
        for i, expected in enumerate(expected_order):
            if self.components[i].component is not expected:
                raise ValueError(
                    f"component[{i}] must be {expected.value}, "
                    f"got {self.components[i].component.value}"
                )

        # coverage_state / lifecycle_reason cross-field invariant (§3.7)
        blocking_reasons = {"NOT_YET_ACTIVE", "DORMANT", "RETIRED", "EXPIRED"}
        eval_reasons = {"OK", "REVIEW_DUE"}
        if self.coverage_state is None:
            if self.lifecycle_reason not in blocking_reasons:
                raise ValueError(
                    "coverage_state is None but lifecycle_reason "
                    f"{self.lifecycle_reason!r} is not a blocking reason"
                )
        else:
            if not isinstance(self.coverage_state, MacroCoverageState):
                raise ValueError("coverage_state must be MacroCoverageState or None")
            if self.lifecycle_reason not in eval_reasons:
                raise ValueError(
                    f"coverage_state is non-None but lifecycle_reason "
                    f"{self.lifecycle_reason!r} is not OK/REVIEW_DUE"
                )

        _validate_str_tuple(self.evidence, _EVIDENCE_MAX_ITEMS, "evidence")
        _validate_str_tuple(
            self.counter_evidence, _COUNTER_EVIDENCE_MAX_ITEMS, "counter_evidence"
        )
        _validate_str_tuple(self.limitations, _LIMITATIONS_MAX_ITEMS, "limitations")
