"""Closed, immutable Slice E analysis contracts and bounded model parsers."""

from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from enum import Enum
import hashlib
import re
from types import MappingProxyType
from typing import Mapping

from tokmo.values import canonical_decimal
from tokmo.values.material_pack import canonical_decode, canonical_encode
from tokmo.values.ops_kernel import (
    AuditCompletionV1,
    CandidateAssignment,
    DispositionAction,
    DispositionV1,
    MaterialKindV1,
    require_token,
)

MODEL_REQUEST_MAX_BYTES = 262_144
MODEL_RESPONSE_MAX_BYTES = 1_048_576
PARSER_MAX_DEPTH = 16
PARSER_MAX_STRING = 16_384
PARSER_MAX_LIST = 2_048
_HASH64 = re.compile(r"^[0-9a-f]{64}$")
_ACCOUNT_SCOPE = re.compile(r"^toss-acct-")


class AnalysisContractError(Exception):
    pass


class TrustClass(str, Enum):
    OFFICIAL_PROVIDER = "OFFICIAL_PROVIDER"
    DERIVED = "DERIVED"
    USER_TEXT = "USER_TEXT"


class FreshnessState(str, Enum):
    FRESH = "FRESH"
    STALE = "STALE"


class GlmRelevance(str, Enum):
    PORTFOLIO_DIRECT = "PORTFOLIO_DIRECT"
    PORTFOLIO_INDIRECT = "PORTFOLIO_INDIRECT"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class GlmMateriality(str, Enum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    UNKNOWN = "UNKNOWN"


class GlmUrgency(str, Enum):
    NOW = "NOW"
    TODAY = "TODAY"
    NONE = "NONE"
    UNKNOWN = "UNKNOWN"


class SolVerdict(str, Enum):
    ALERT_WORTHY = "ALERT_WORTHY"
    NOTEWORTHY = "NOTEWORTHY"
    NO_ACTION = "NO_ACTION"
    UNKNOWN = "UNKNOWN"


class SolProposedAction(str, Enum):
    ALERT = "ALERT"
    WATCH = "WATCH"
    IGNORE = "IGNORE"


class ProviderExit(str, Enum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    TRUNCATED = "TRUNCATED"
    OVERSIZE = "OVERSIZE"
    EMPTY_SUCCESS = "EMPTY_SUCCESS"
    PARSE_FAIL = "PARSE_FAIL"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAIL = "AUTH_FAIL"
    TRANSPORT_FAIL = "TRANSPORT_FAIL"


class ModelCallStatus(str, Enum):
    OK = "OK"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"
    TRUNCATED = "TRUNCATED"
    OVERSIZE = "OVERSIZE"
    EMPTY_SUCCESS = "EMPTY_SUCCESS"
    PARSE_FAIL = "PARSE_FAIL"
    RATE_LIMITED = "RATE_LIMITED"
    AUTH_FAIL = "AUTH_FAIL"
    TRANSPORT_FAIL = "TRANSPORT_FAIL"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


class ChangeKind(str, Enum):
    NEW_POSITION = "NEW_POSITION"
    POSITION_CLOSED = "POSITION_CLOSED"
    QUANTITY_CHANGED = "QUANTITY_CHANGED"
    AVG_PRICE_CHANGED = "AVG_PRICE_CHANGED"
    CURRENT_FACT = "CURRENT_FACT"


class ChangeBasis(str, Enum):
    BASELINE_DELTA = "BASELINE_DELTA"
    CURRENT_FACT_ONLY = "CURRENT_FACT_ONLY"


class IncidentTransition(str, Enum):
    FIRST = "FIRST"
    IMPACT_CHANGED = "IMPACT_CHANGED"
    RESOLVED = "RESOLVED"


class NumericStateV1(str, Enum):
    DATA_INCOMPLETE = "DATA_INCOMPLETE"
    NORMAL = "NORMAL"
    WATCH = "WATCH"
    MATERIAL_MOVE = "MATERIAL_MOVE"
    REVERSAL = "REVERSAL"
    PERSISTENT_DRIFT = "PERSISTENT_DRIFT"


class QuoteTriggerKindV1(str, Enum):
    FIRST_OBSERVATION = "FIRST_OBSERVATION"
    REVERSAL = "REVERSAL"
    PERSISTENT_DRIFT = "PERSISTENT_DRIFT"
    CUMULATIVE_DRIFT = "CUMULATIVE_DRIFT"
    THRESHOLD_MOVE = "THRESHOLD_MOVE"
    STATE_TRANSITION = "STATE_TRANSITION"


class TriggerHorizonV1(str, Enum):
    NONE = "NONE"
    TICK = "TICK"
    STREAK = "STREAK"
    HORIZON_15M = "HORIZON_15M"
    HORIZON_30M = "HORIZON_30M"


QUOTE_WATCH_THRESHOLD_RATIO = Decimal("0.002")


class AnalysisKind(str, Enum):
    """Trigger-agnostic analysis request kind.

    EVENT is the only kind produced by the deterministic event admission
    registry in this wave. HEALTH_COVERAGE is reserved for future
    AI-HC-001 and accepted by the closed parser/contract round-trip, but
    no producer, scheduler, counter, or watchdog is implemented here.
    """

    EVENT = "EVENT"
    HEALTH_COVERAGE = "HEALTH_COVERAGE"


class AnalysisRequestState(str, Enum):
    """Durable analysis request lifecycle state (§6.6, §7 Atomic APIs)."""

    PENDING = "PENDING"
    ANALYZING = "ANALYZING"
    SUCCEEDED = "SUCCEEDED"
    DEFERRED = "DEFERRED"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class TriggerKindV1(str, Enum):
    """Closed trigger-kind token for analysis requests (§6.6).

    Only DETERMINISTIC_EVENT is produced by the event admission registry
    in this wave. Future producers require a separate migration.
    """

    DETERMINISTIC_EVENT = "DETERMINISTIC_EVENT"


class ValidatorRejection(str, Enum):
    EVIDENCE_STALE = "EVIDENCE_STALE"
    EVIDENCE_EMPTY = "EVIDENCE_EMPTY"
    MERGE_PRIOR_MISMATCH = "MERGE_PRIOR_MISMATCH"
    CRITICAL_SKIP_OR_MERGE = "CRITICAL_SKIP_OR_MERGE"


class AnalysisRunState(str, Enum):
    COMPLETED = "COMPLETED"
    LOCK_CONTENDED = "LOCK_CONTENDED"
    LOCK_UNAVAILABLE = "LOCK_UNAVAILABLE"
    CAPACITY_BLOCKED = "CAPACITY_BLOCKED"
    DRIFT_CONFLICT = "DRIFT_CONFLICT"
    TERMINAL_MANIFEST = "TERMINAL_MANIFEST"


@dataclass(frozen=True)
class AnalysisRunOutcomeV1:
    state: AnalysisRunState
    invocation_id: str | None
    tick_id: int
    candidate_count: int
    disposition_count: int

    def __post_init__(self):
        if not isinstance(self.state, AnalysisRunState):
            raise ValueError("invalid analysis run state")
        if self.invocation_id is not None and not re.fullmatch(
            r"[0-9a-f]{32}", self.invocation_id
        ):
            raise ValueError("invalid invocation id")
        if any(type(value) is not int or value < 0 for value in (
            self.tick_id, self.candidate_count, self.disposition_count,
        )):
            raise ValueError("invalid analysis run counts")


ALLOWED_REASONS_V1 = MappingProxyType({
    DispositionAction.SEND: frozenset({"evidence_verified_send"}),
    DispositionAction.DEFER: frozenset({
        "market_closed_defer", "model_unavailable_defer", "capacity_defer",
        "glm_failure_defer", "sol_failure_defer", "next_session_defer",
        "snapshot_incomplete_defer", "evidence_stale_defer",
        "proposal_invalid_defer", "carryover_defer",
    }),
    DispositionAction.MERGE: frozenset({"same_subject_session_merge"}),
    DispositionAction.SKIP: frozenset({"glm_screened_none", "subject_unchanged"}),
    DispositionAction.ESCALATE: frozenset({
        "critical_model_failure", "judgment_unavailable", "stop_signal",
        "capacity_exceeded", "quarantine_conflict", "evidence_unverifiable",
    }),
})
CRITICAL_DEFER_REASONS_V1 = frozenset({
    "market_closed_defer", "model_unavailable_defer", "sol_failure_defer",
})
VALIDATOR_FALLBACK_REASONS_V1 = frozenset({
    "evidence_stale_defer", "proposal_invalid_defer", "carryover_defer",
    "evidence_unverifiable",
})
CRITICAL_CONFLICT_KINDS_V1 = frozenset({
    "execution-identity-conflict", "execution-quarantine",
    "trades-quarantine-observed",
})
CRITICAL_STOP_KINDS_V1 = frozenset({
    "holdings-identity-collision", "holdings_material_unavailable",
    "quotes_material_unavailable", "trades_material_unavailable",
    "kr_holdings_unavailable", "us_holdings_unavailable",
})
CRITICAL_MISSING_KINDS_V1 = frozenset({
    "kr_holdings_missing", "us_holdings_missing",
})
CRITICAL_INCIDENT_KINDS_V1 = (
    CRITICAL_CONFLICT_KINDS_V1
    | CRITICAL_STOP_KINDS_V1
    | CRITICAL_MISSING_KINDS_V1
)
NON_CRITICAL_INCIDENT_KINDS_V1 = frozenset({
    "kr_holdings_stale", "us_holdings_stale", "market-classification-unknown",
    "execution-incomplete", "execution-market-pending",
    "trades-incomplete-observed", "holdings-snapshot-incomplete",
    "analysis-capacity-exceeded", "audit-capacity-exceeded",
    "glm-failure-streak", "model-failure-both", "model-call-unknown",
    "model-rate-limited", "model-auth-unavailable",
    "validator-proposal-rejected", "manifest-material-drift",
    "unknown-incident-kind",
})
KNOWN_INCIDENT_KINDS_V1 = (
    CRITICAL_INCIDENT_KINDS_V1 | NON_CRITICAL_INCIDENT_KINDS_V1
)
VALIDATOR_REJECTION_FALLBACK_V1 = MappingProxyType({
    (ValidatorRejection.CRITICAL_SKIP_OR_MERGE, True):
        (DispositionAction.ESCALATE, "judgment_unavailable"),
    (ValidatorRejection.EVIDENCE_EMPTY, True):
        (DispositionAction.ESCALATE, "evidence_unverifiable"),
    (ValidatorRejection.EVIDENCE_EMPTY, False):
        (DispositionAction.DEFER, "evidence_stale_defer"),
    (ValidatorRejection.EVIDENCE_STALE, True):
        (DispositionAction.ESCALATE, "evidence_unverifiable"),
    (ValidatorRejection.EVIDENCE_STALE, False):
        (DispositionAction.DEFER, "evidence_stale_defer"),
    (ValidatorRejection.MERGE_PRIOR_MISMATCH, False):
        (DispositionAction.DEFER, "proposal_invalid_defer"),
})


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be aware")


def _exact(value: Mapping, keys: frozenset[str], name: str) -> None:
    if not isinstance(value, Mapping) or set(value) != keys:
        raise ValueError(f"{name} must have exact keys")


def _digest(value) -> str:
    return hashlib.sha256(canonical_encode(value)).hexdigest()


def subject_id_for(kind: MaterialKindV1, key) -> str:
    if not isinstance(kind, MaterialKindV1):
        raise ValueError("invalid material kind")
    return _digest({"v": 1, "domain": "tokmo-v3:subject", "kind": kind.value, "key": key})[:16]


def candidate_id_for(tick_id: int, kind: MaterialKindV1, subject_id: str) -> str:
    if type(tick_id) is not int or tick_id < 0:
        raise ValueError("invalid tick")
    require_token(subject_id, "subject_id")
    return _digest({
        "v": 1, "domain": "tokmo-v3:candidate", "tick_id": tick_id,
        "kind": kind.value, "subject_id": subject_id,
    })[:16]


def evidence_id_for(kind: MaterialKindV1, source: str, record: str, content_hash: str) -> str:
    if not _HASH64.fullmatch(content_hash):
        raise ValueError("invalid content hash")
    return _digest({
        "v": 1, "domain": "tokmo-v3:evidence", "kind": kind.value,
        "source": source, "record": record, "content_hash": content_hash,
    })[:16]


@dataclass(frozen=True)
class EvidenceRefV1:
    evidence_id: str
    kind: MaterialKindV1
    source: str
    source_record_key: str
    observed_at: datetime
    fetched_at: datetime
    stored_at: datetime
    acquired_at: datetime
    content_hash: str
    schema_version: int
    parser_version: str
    trust_class: TrustClass
    freshness_state: FreshnessState
    expires_at: datetime

    def __post_init__(self):
        require_token(self.evidence_id, "evidence_id")
        if not isinstance(self.kind, MaterialKindV1):
            raise ValueError("invalid evidence kind")
        if (type(self.source) is not str or type(self.source_record_key) is not str
                or _ACCOUNT_SCOPE.search(self.source) or _ACCOUNT_SCOPE.search(self.source_record_key)):
            raise ValueError("private identity forbidden")
        for value, name in (
            (self.observed_at, "observed_at"), (self.fetched_at, "fetched_at"),
            (self.stored_at, "stored_at"), (self.acquired_at, "acquired_at"),
            (self.expires_at, "expires_at"),
        ):
            _aware(value, name)
        if not _HASH64.fullmatch(self.content_hash):
            raise ValueError("invalid content hash")
        if type(self.schema_version) is not int or self.schema_version < 1:
            raise ValueError("invalid schema version")
        require_token(self.parser_version, "parser_version")
        if not isinstance(self.trust_class, TrustClass) or not isinstance(self.freshness_state, FreshnessState):
            raise ValueError("invalid evidence enum")


FIELD_KEYS = MappingProxyType({
    MaterialKindV1.HOLDINGS_CHANGE: frozenset({
        "market", "account_type_label", "portfolio_relation", "ordinal", "symbol", "change_kind",
        "baseline_tick_id", "current_quantity", "current_avg_price",
        "baseline_quantity", "baseline_avg_price", "quantity_delta_ratio", "currency",
    }),
    MaterialKindV1.QUOTE_STATE: frozenset({
        "market", "symbol", "portfolio_relation", "last_price", "currency_raw", "provider_timestamp",
        "fetched_at", "change_basis", "baseline_price", "price_delta_ratio",
        "numeric_state", "trigger_kind", "trigger_reference_price",
        "trigger_horizon", "price_delta_ratio_5m", "price_delta_ratio_15m",
        "price_delta_ratio_30m", "velocity_per_min", "acceleration_per_min2",
        "reversal", "persistent_drift_ticks", "money_impact",
    }),
    MaterialKindV1.COMPLETED_EXECUTION: frozenset({
        "market", "symbol", "source_record_key", "trade_date", "transaction_type_raw",
        "quantity", "unit_price", "amount", "currency_raw", "filled_at",
    }),
    MaterialKindV1.OPERATIONAL_INCIDENT: frozenset({
        "incident_kind", "incident_subject", "transition", "count", "impact_ref",
    }),
})


@dataclass(frozen=True)
class CandidateDataSegmentV1:
    candidate_id: str
    payload: Mapping[str, object]

    def __post_init__(self):
        require_token(self.candidate_id, "candidate_id")
        if not isinstance(self.payload, Mapping):
            raise ValueError("payload must be mapping")
        object.__setattr__(self, "payload", MappingProxyType(dict(self.payload)))
        canonical_encode(self.payload)


@dataclass(frozen=True)
class CandidateEnvelopeV1:
    candidate_id: str
    subject_id: str
    kind: MaterialKindV1
    market: str
    tick_id: int
    assignment: CandidateAssignment
    evidence_refs: tuple[EvidenceRefV1, ...]
    fields: Mapping[str, object]
    created_at: datetime

    def __post_init__(self):
        require_token(self.candidate_id, "candidate_id")
        require_token(self.subject_id, "subject_id")
        if not isinstance(self.kind, MaterialKindV1) or self.market not in {"KR", "US", "GLOBAL"}:
            raise ValueError("invalid candidate domain")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid candidate tick")
        if not isinstance(self.assignment, CandidateAssignment):
            raise ValueError("invalid assignment")
        if type(self.evidence_refs) is not tuple or not self.evidence_refs or any(
            not isinstance(item, EvidenceRefV1) for item in self.evidence_refs
        ):
            raise ValueError("invalid evidence refs")
        _exact(self.fields, FIELD_KEYS[self.kind], "candidate fields")
        if "account_scope" in self.fields:
            raise ValueError("private identity forbidden")
        object.__setattr__(self, "fields", MappingProxyType(dict(self.fields)))
        _aware(self.created_at, "created_at")


@dataclass(frozen=True)
class GlmAnnotationV1:
    candidate_id: str
    relevance: GlmRelevance
    materiality: GlmMateriality
    urgency: GlmUrgency
    evidence_unknowns: tuple[str, ...]
    summary: str


@dataclass(frozen=True)
class GlmBatchV1:
    schema_version: int
    annotations: tuple[GlmAnnotationV1, ...]
    exit: ProviderExit


@dataclass(frozen=True)
class SolAnalysisV1:
    candidate_id: str
    verdict: SolVerdict
    proposed_action: SolProposedAction
    rationale: str
    cited_evidence_ids: tuple[str, ...]
    confidence: str
    disagreements_with_glm: tuple[str, ...]


@dataclass(frozen=True)
class SolBatchV1:
    schema_version: int
    analyses: tuple[SolAnalysisV1, ...]
    exit: ProviderExit


@dataclass(frozen=True)
class UntrustedTextV1:
    text: str
    provenance: str
    trust_class: TrustClass
    instruction_authority: str = "NONE"

    def __post_init__(self):
        if type(self.text) is not str or len(self.text) > 8192 or _has_controls(self.text, True):
            raise ValueError("invalid untrusted text")
        require_token(self.provenance, "provenance")
        if not isinstance(self.trust_class, TrustClass) or self.instruction_authority != "NONE":
            raise ValueError("invalid untrusted text metadata")


@dataclass(frozen=True)
class HermesDispositionInputV1:
    candidate: CandidateEnvelopeV1
    glm_status: ModelCallStatus
    sol_status: ModelCallStatus
    glm_annotation: GlmAnnotationV1 | None
    sol_analysis_present: bool
    sol_cited_evidence_ids: tuple[str, ...]
    evidence_freshness: Mapping[str, FreshnessState]
    session_key: tuple[str, str]
    prior_subject_disposition: DispositionV1 | None
    activation_telegram: bool


def validate_disposition(
    value: HermesDispositionInputV1, proposal: DispositionV1
) -> DispositionV1 | ValidatorRejection:
    """Apply the §8.1 hard gates in their normative deterministic order."""
    if not isinstance(value, HermesDispositionInputV1) or not isinstance(
        proposal, DispositionV1
    ):
        raise AnalysisContractError("invalid validator input")
    if proposal.candidate_id != value.candidate.candidate_id or (
        proposal.tick_id, proposal.subject_id
    ) != (value.candidate.tick_id, value.candidate.subject_id):
        raise AnalysisContractError("validator identity invariant")
    if proposal.reason_code not in ALLOWED_REASONS_V1.get(proposal.action, ()):
        raise AnalysisContractError("validator reason invariant")
    critical = value.candidate.assignment is CandidateAssignment.CRITICAL
    if critical and proposal.action in {
        DispositionAction.SKIP, DispositionAction.MERGE
    }:
        return ValidatorRejection.CRITICAL_SKIP_OR_MERGE
    if critical and proposal.action is DispositionAction.DEFER and (
        proposal.reason_code not in CRITICAL_DEFER_REASONS_V1
    ):
        raise AnalysisContractError("validator critical defer invariant")
    if proposal.action is DispositionAction.SEND:
        if value.sol_status is not ModelCallStatus.OK or not value.sol_analysis_present:
            raise AnalysisContractError("validator sol invariant")
        if not proposal.cited_evidence_ids:
            return ValidatorRejection.EVIDENCE_EMPTY
        manifest_ids = {item.evidence_id for item in value.candidate.evidence_refs}
        if not set(proposal.cited_evidence_ids) <= manifest_ids:
            raise AnalysisContractError("validator evidence invariant")
        if any(
            value.evidence_freshness.get(item) is not FreshnessState.FRESH
            for item in proposal.cited_evidence_ids
        ):
            return ValidatorRejection.EVIDENCE_STALE
    if proposal.action is DispositionAction.MERGE:
        prior = value.prior_subject_disposition
        if prior is None or prior.subject_id != value.candidate.subject_id or (
            prior.action not in {DispositionAction.SEND, DispositionAction.ESCALATE}
        ):
            return ValidatorRejection.MERGE_PRIOR_MISMATCH
    if proposal.action is DispositionAction.SKIP and (
        proposal.reason_code == "glm_screened_none"
        and value.glm_status is not ModelCallStatus.OK
    ):
        raise AnalysisContractError("validator glm invariant")
    return proposal


def _has_controls(value: str, allow_lines: bool) -> bool:
    return any((ord(char) < 32 and not (allow_lines and char in "\n\t")) or ord(char) == 127
               for char in value)


def _bounded(value, depth=0):
    if depth > PARSER_MAX_DEPTH:
        raise ValueError("parser depth")
    if type(value) is str:
        if len(value) > PARSER_MAX_STRING or _has_controls(value, True):
            raise ValueError("parser string")
    elif type(value) is list:
        if len(value) > PARSER_MAX_LIST:
            raise ValueError("parser list")
        for item in value:
            _bounded(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            _bounded(key, depth + 1)
            _bounded(item, depth + 1)


def parse_model_json(raw: bytes, max_bytes: int):
    value = canonical_decode(raw, max_bytes)
    _bounded(value)
    return value


def _strings(value, name: str, limit: int | None = None) -> tuple[str, ...]:
    if type(value) is not list or any(type(item) is not str or _has_controls(item, True) for item in value):
        raise ValueError(f"invalid {name}")
    if limit is not None and len(value) > limit:
        raise ValueError(f"invalid {name}")
    return tuple(value)


def parse_glm_batch(raw: bytes, candidate_ids: tuple[str, ...]) -> GlmBatchV1:
    try:
        value = parse_model_json(raw, MODEL_RESPONSE_MAX_BYTES)
        _exact(value, frozenset({"schema_version", "annotations"}), "glm batch")
        if value["schema_version"] != 1 or type(value["annotations"]) is not list:
            raise ValueError("invalid glm batch")
        annotations = []
        for row in value["annotations"]:
            _exact(row, frozenset({
                "candidate_id", "relevance", "materiality", "urgency",
                "evidence_unknowns", "summary",
            }), "glm annotation")
            if type(row["summary"]) is not str or len(row["summary"]) > 1000:
                raise ValueError("invalid summary")
            annotations.append(GlmAnnotationV1(
                candidate_id=row["candidate_id"], relevance=GlmRelevance(row["relevance"]),
                materiality=GlmMateriality(row["materiality"]), urgency=GlmUrgency(row["urgency"]),
                evidence_unknowns=_strings(row["evidence_unknowns"], "evidence unknowns"),
                summary=row["summary"],
            ))
        actual = [item.candidate_id for item in annotations]
        if len(actual) != len(set(actual)) or set(actual) != set(candidate_ids):
            raise ValueError("candidate coverage")
        return GlmBatchV1(1, tuple(annotations), ProviderExit.OK)
    except (TypeError, ValueError, KeyError, RecursionError):
        return GlmBatchV1(1, (), ProviderExit.PARSE_FAIL)


def _confidence(value) -> str:
    if type(value) is not str:
        raise ValueError("invalid confidence")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:
        raise ValueError("invalid confidence") from error
    if not parsed.is_finite() or parsed < 0 or parsed > 1 or canonical_decimal(parsed) != value:
        raise ValueError("invalid confidence")
    if max(0, -parsed.as_tuple().exponent) > 4:
        raise ValueError("invalid confidence")
    return value


def parse_sol_batch(raw: bytes, candidate_ids: tuple[str, ...],
                    evidence_ids: frozenset[str]) -> SolBatchV1:
    try:
        value = parse_model_json(raw, MODEL_RESPONSE_MAX_BYTES)
        _exact(value, frozenset({"schema_version", "analyses"}), "sol batch")
        if value["schema_version"] != 1 or type(value["analyses"]) is not list:
            raise ValueError("invalid sol batch")
        analyses = []
        for row in value["analyses"]:
            _exact(row, frozenset({
                "candidate_id", "verdict", "proposed_action", "rationale",
                "cited_evidence_ids", "confidence", "disagreements_with_glm",
            }), "sol analysis")
            if type(row["rationale"]) is not str or len(row["rationale"]) > 2000:
                raise ValueError("invalid rationale")
            citations = _strings(row["cited_evidence_ids"], "citations", 64)
            if len(citations) != len(set(citations)) or not set(citations) <= evidence_ids:
                raise ValueError("citation coverage")
            analyses.append(SolAnalysisV1(
                candidate_id=row["candidate_id"], verdict=SolVerdict(row["verdict"]),
                proposed_action=SolProposedAction(row["proposed_action"]),
                rationale=row["rationale"], cited_evidence_ids=citations,
                confidence=_confidence(row["confidence"]),
                disagreements_with_glm=_strings(row["disagreements_with_glm"], "disagreements"),
            ))
        actual = [item.candidate_id for item in analyses]
        if len(actual) != len(set(actual)) or set(actual) != set(candidate_ids):
            raise ValueError("candidate coverage")
        return SolBatchV1(1, tuple(analyses), ProviderExit.OK)
    except (TypeError, ValueError, KeyError, RecursionError):
        return SolBatchV1(1, (), ProviderExit.PARSE_FAIL)


@dataclass(frozen=True)
class ExpectedAnalysisCoverageV1:
    """Immutable per-symbol coverage contract admitted with the request."""

    market: str
    symbol: str
    evidence_id: str
    material_tick_id: int
    material_observed_at: datetime

    def __post_init__(self):
        if self.market not in ("KR", "US"):
            raise ValueError("invalid expected coverage market")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("invalid expected coverage symbol")
        require_token(self.evidence_id, "expected coverage evidence_id")
        if type(self.material_tick_id) is not int or self.material_tick_id < 0:
            raise ValueError("invalid expected coverage material tick")
        _aware(self.material_observed_at, "material_observed_at")

    @property
    def canonical_payload(self):
        return {
            "market": self.market,
            "symbol": self.symbol,
            "evidence_id": self.evidence_id,
            "material_tick_id": self.material_tick_id,
            "material_observed_at": self.material_observed_at.astimezone(
                timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        }


@dataclass(frozen=True)
class AnalysisRequestV1:
    """Trigger-agnostic durable analysis request (§6.6).

    Bound one-to-one to its candidate manifest row via
    ``UNIQUE(tick_id,candidate_id)``. Identity is a domain-separated
    digest that never equals or reuses ``candidate_id``.
    """

    request_id: str
    analysis_kind: AnalysisKind
    trigger_kind: TriggerKindV1
    tick_id: int
    candidate_id: str
    delta_id: str
    subject_id: str
    material_binding_digest: str
    admission_policy_version: str
    admission_policy_digest: str
    admission_reason_codes: tuple[str, ...]
    requested_at: datetime
    expected_coverage: tuple[ExpectedAnalysisCoverageV1, ...] = ()

    def __post_init__(self):
        require_token(self.request_id, "request_id")
        if not isinstance(self.analysis_kind, AnalysisKind):
            raise ValueError("invalid analysis kind")
        if not isinstance(self.trigger_kind, TriggerKindV1):
            raise ValueError("invalid trigger kind")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid tick_id")
        require_token(self.candidate_id, "candidate_id")
        require_token(self.delta_id, "delta_id")
        require_token(self.subject_id, "subject_id")
        if len(self.material_binding_digest) != 64:
            raise ValueError("invalid material_binding_digest")
        require_token(self.admission_policy_version, "admission_policy_version")
        if len(self.admission_policy_digest) != 64:
            raise ValueError("invalid admission_policy_digest")
        if (type(self.admission_reason_codes) is not tuple
                or not self.admission_reason_codes
                or any(type(item) is not str or not item
                       for item in self.admission_reason_codes)):
            raise ValueError("invalid admission reason codes")
        _aware(self.requested_at, "requested_at")
        if type(self.expected_coverage) is not tuple or any(
            not isinstance(row, ExpectedAnalysisCoverageV1)
            for row in self.expected_coverage
        ):
            raise ValueError("invalid expected coverage")
        identities = tuple((row.market, row.symbol) for row in self.expected_coverage)
        if identities != tuple(sorted(set(identities))):
            raise ValueError("expected coverage must be unique and sorted")

    @staticmethod
    def compute_request_id(*, analysis_kind, tick_id, candidate_id,
                           material_binding_digest, admission_policy_digest):
        """Domain-separated request identity (§6.6).

        ``sha256(canonical_encode({domain, analysis_kind, tick_id,
        candidate_id, material_binding_digest,
        admission_policy_digest}))[:32]``
        """
        if isinstance(analysis_kind, AnalysisKind):
            kind = analysis_kind.value
        elif type(analysis_kind) is str:
            kind = analysis_kind
        else:
            raise ValueError("invalid analysis kind")
        payload = {
            "v": 1,
            "domain": "tokmo-v3:analysis-request:v1",
            "analysis_kind": kind,
            "tick_id": tick_id,
            "candidate_id": candidate_id,
            "material_binding_digest": material_binding_digest,
            "admission_policy_digest": admission_policy_digest,
        }
        return _digest(payload)[:32]

    @property
    def request_digest(self) -> str:
        """Deterministic payload digest for replay/duplicate detection."""
        return _digest({
            "v": 1,
            "domain": "tokmo-v3:analysis-request-payload:v1",
            "request_id": self.request_id,
            "analysis_kind": self.analysis_kind.value,
            "trigger_kind": self.trigger_kind.value,
            "tick_id": self.tick_id,
            "candidate_id": self.candidate_id,
            "delta_id": self.delta_id,
            "subject_id": self.subject_id,
            "material_binding_digest": self.material_binding_digest,
            "admission_policy_version": self.admission_policy_version,
            "admission_policy_digest": self.admission_policy_digest,
            "admission_reason_codes": list(self.admission_reason_codes),
            "requested_at": self.requested_at.astimezone(timezone.utc).strftime(
                "%Y-%m-%dT%H:%M:%S.%fZ"
            ),
            "expected_coverage": [
                row.canonical_payload for row in self.expected_coverage
            ],
        })


@dataclass(frozen=True)
class AnalysisResultV1:
    """Typed facade over a validated analysis result (§6.7).

    This is not a second judgment system; it records the accepted
    terminal outcome of the common analyzer for one request.
    """

    result_id: str
    request_id: str
    analysis_kind: AnalysisKind
    status: str  # SUCCEEDED | DEFERRED | FAILED | UNKNOWN
    decision_id: str | None
    artifact_digest: str | None
    analyzed_at: datetime

    def __post_init__(self):
        require_token(self.result_id, "result_id")
        require_token(self.request_id, "request_id")
        if not isinstance(self.analysis_kind, AnalysisKind):
            raise ValueError("invalid analysis kind")
        if self.status not in ("SUCCEEDED", "DEFERRED", "FAILED", "UNKNOWN"):
            raise ValueError("invalid result status")
        if self.decision_id is not None:
            require_token(self.decision_id, "decision_id")
        if self.artifact_digest is not None and len(self.artifact_digest) != 64:
            raise ValueError("invalid artifact digest")
        _aware(self.analyzed_at, "analyzed_at")

    @staticmethod
    def compute_result_id(*, request_id, decision_id):
        payload = {
            "v": 1,
            "domain": "tokmo-v3:analysis-result:v1",
            "request_id": request_id,
            "decision_id": decision_id,
        }
        return hashlib.sha256(canonical_encode(payload)).hexdigest()[:32]


@dataclass(frozen=True)
class AnalysisCoverageV1:
    """Per-symbol successful analysis coverage (§6.7).

    Appended only when all of: request durable, provider result OK,
    parser succeeds, validator accepts terminal conclusion, all cited
    evidence fresh/unexpired, disposition/result transaction commits.
    Contains no quantity, price, account label, rationale, or prompt.
    """

    analysis_kind: AnalysisKind
    market: str
    symbol: str
    request_id: str
    result_id: str
    subject_id: str
    material_tick_id: int
    material_observed_at: datetime
    analyzed_at: datetime
    policy_digest: str
    evidence_id: str

    def __post_init__(self):
        if not isinstance(self.analysis_kind, AnalysisKind):
            raise ValueError("invalid coverage analysis kind")
        if self.market not in ("KR", "US"):
            raise ValueError("invalid coverage market")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("invalid coverage symbol")
        require_token(self.request_id, "request_id")
        require_token(self.result_id, "result_id")
        require_token(self.subject_id, "subject_id")
        if type(self.material_tick_id) is not int or self.material_tick_id < 0:
            raise ValueError("invalid material tick id")
        _aware(self.material_observed_at, "material_observed_at")
        _aware(self.analyzed_at, "analyzed_at")
        if len(self.policy_digest) != 64:
            raise ValueError("invalid policy digest")
        require_token(self.evidence_id, "coverage evidence_id")
