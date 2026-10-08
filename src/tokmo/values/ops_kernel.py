"""Closed, immutable values for the Slice D operations kernel."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import hashlib
import re
from types import MappingProxyType
from typing import Mapping
from urllib.parse import urlsplit

from tokmo.values.material_pack import JobKind, canonical_encode
from tokmo.values.session_gate import Market


class OpsKernelError(Exception):
    """Base class whose messages are deliberately private-value free."""


class OpsKernelCorruptError(OpsKernelError):
    pass


class OpsKernelLockedError(OpsKernelError):
    pass


class OpsKernelMigrationRequiredError(OpsKernelCorruptError):
    """Exact supported legacy schema seen by ordinary construction.

    Plan/SDS 2026-08-30 §4: ordinary constructors never migrate; an exact
    legacy database raises this typed subclass so unaware callers keep their
    fail-closed OpsKernelCorruptError handling while migration tooling can
    distinguish "migration required" from corruption.
    """
    pass


class OpsKernelConflictError(OpsKernelError):
    pass


class OpsKernelTransitionError(OpsKernelError):
    pass


class OpsKernelRoleError(OpsKernelError):
    pass


class OpsKernelBudgetExceeded(OpsKernelError):
    pass


class OpsKernelCooldownActive(OpsKernelError):
    pass


class CadenceBindingError(OpsKernelConflictError):
    """Missing/unknown versioned tick binding, operation, or native identity."""


class CadencePolicyMismatchError(OpsKernelConflictError):
    """Presented policy does not match the tick's pinned snapshot."""


class CadenceDuplicateError(OpsKernelConflictError):
    """Same durable identity already consumed; duplicate performs wire zero."""


class CadenceBudgetExceeded(OpsKernelBudgetExceeded):
    """Total/group/child STARTED ceiling reached for this tick."""


class CadenceTransitionError(OpsKernelTransitionError):
    """Row exists but is not in a startable/settleable state."""


class CadenceCalendarClosedError(CadenceTransitionError):
    """Calendar identity already succeeded; later retries perform wire zero."""


class CadenceCalendarExhaustedError(CadenceTransitionError):
    """All six calendar attempt ordinals consumed for this market-local date."""


class CadenceCalendarBackoffError(CadenceTransitionError):
    """The previous failed attempt's backoff deadline has not passed."""


@dataclass(frozen=True)
class CalendarStartReceiptV1:
    """Typed STARTED receipt of one calendar wire attempt (SDS §4, §7.3).

    Issued only by the operations kernel inside the admission transaction.
    Calendar fetch/refresh and the cache commit must carry and validate this
    exact identity; without it the provider call and cache mutation are zero.
    """

    policy_version: str
    scheduled_for_utc: datetime
    market: Market
    market_local_date: date
    attempt_ordinal: int
    native_identity: str
    state: str

    def __post_init__(self):
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("invalid policy version")
        _aware(self.scheduled_for_utc, "scheduled_for_utc")
        if not isinstance(self.market, Market):
            raise ValueError("unknown market")
        if (type(self.market_local_date) is not date
                or isinstance(self.market_local_date, datetime)):
            raise ValueError("market local date must be a date")
        if type(self.attempt_ordinal) is not int or not 0 <= self.attempt_ordinal <= 5:
            raise ValueError("invalid attempt ordinal")
        expected = (
            f"calendar:{self.market.value}:"
            f"{self.market_local_date.isoformat()}:{self.attempt_ordinal}"
        )
        if self.native_identity != expected:
            raise ValueError("native identity does not match receipt identity")
        if self.state != "STARTED":
            raise ValueError("calendar receipt must be STARTED")


class ProducerRole(str, Enum):
    COLLECTOR = "COLLECTOR"
    ANALYSIS = "ANALYSIS"
    HERMES = "HERMES"
    FEEDBACK = "FEEDBACK"


class ReplayResult(str, Enum):
    CREATED = "CREATED"
    DUPLICATE = "DUPLICATE"


class ManifestState(str, Enum):
    OPEN = "OPEN"
    SEALED = "SEALED"
    FAILED = "FAILED"


class CandidateAssignment(str, Enum):
    CRITICAL = "CRITICAL"
    FULL_REVIEW = "FULL_REVIEW"
    SAMPLED_AUDIT = "SAMPLED_AUDIT"
    EXPLICITLY_DEFERRED = "EXPLICITLY_DEFERRED"
    TERMINAL = "TERMINAL"


class CandidateState(str, Enum):
    PENDING = "PENDING"
    DISPOSITIONED = "DISPOSITIONED"
    DEFERRED = "DEFERRED"
    TERMINAL = "TERMINAL"


class DispositionAction(str, Enum):
    SEND = "SEND"
    DEFER = "DEFER"
    MERGE = "MERGE"
    SKIP = "SKIP"
    ESCALATE = "ESCALATE"


class AlertOrigin(str, Enum):
    DISPOSITION = "DISPOSITION"
    INCIDENT = "INCIDENT"
    NEWS_EVENT = "NEWS_EVENT"
    NEWS_OPS = "NEWS_OPS"
    QUANT_MARKET_CLOSE = "QUANT_MARKET_CLOSE"


class AlertChannel(str, Enum):
    SHADOW = "SHADOW"
    TELEGRAM = "TELEGRAM"


class AlertStatus(str, Enum):
    PENDING = "PENDING"
    ATTEMPTING = "ATTEMPTING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


class AlertResolution(str, Enum):
    UNRESOLVED = "UNRESOLVED"
    CONFIRMED_SENT = "CONFIRMED_SENT"
    REQUEUED = "REQUEUED"
    FAILED_ACKNOWLEDGED = "FAILED_ACKNOWLEDGED"


class IncidentState(str, Enum):
    OPEN = "OPEN"
    RESOLVED = "RESOLVED"


class FeedbackEventKind(str, Enum):
    CREATE = "CREATE"
    EDIT = "EDIT"
    DELETE = "DELETE"


class DeferredTerminalState(str, Enum):
    DISPOSITIONED = "DISPOSITIONED"
    TERMINAL = "TERMINAL"
    AUDITED = "AUDITED"
    ESCALATED = "ESCALATED"


class MaterialKindV1(str, Enum):
    HOLDINGS_CHANGE = "HOLDINGS_CHANGE"
    QUOTE_STATE = "QUOTE_STATE"
    COMPLETED_EXECUTION = "COMPLETED_EXECUTION"
    OPERATIONAL_INCIDENT = "OPERATIONAL_INCIDENT"


# --- YouTube long-term hypothesis tracking (2026-08-16 wiring) -------------

class HypothesisState(str, Enum):
    CANDIDATE = "CANDIDATE"
    WATCHING = "WATCHING"
    RESOLVED = "RESOLVED"
    INVALIDATED = "INVALIDATED"
    EXPIRED = "EXPIRED"
    ARCHIVED = "ARCHIVED"


_HYPOTHESIS_TERMINAL_STATES = frozenset({
    HypothesisState.RESOLVED, HypothesisState.INVALIDATED,
    HypothesisState.EXPIRED, HypothesisState.ARCHIVED,
})

_HYPOTHESIS_TRANSITIONS = {
    HypothesisState.CANDIDATE: frozenset({
        HypothesisState.WATCHING, HypothesisState.ARCHIVED,
    }),
    HypothesisState.WATCHING: frozenset({
        HypothesisState.RESOLVED, HypothesisState.INVALIDATED,
        HypothesisState.EXPIRED, HypothesisState.ARCHIVED,
    }),
}


class TriggerKind(str, Enum):
    THRESHOLD_PERSIST = "THRESHOLD_PERSIST"
    EVENT_OCCUR = "EVENT_OCCUR"
    CALENDAR_CHECKPOINT = "CALENDAR_CHECKPOINT"


class IndexKeyKind(str, Enum):
    METRIC = "METRIC"
    ENTITY = "ENTITY"
    EFFECT = "EFFECT"
    SOURCE_EVENT = "SOURCE_EVENT"


class HypothesisMergeOutcome(str, Enum):
    MERGED = "MERGED"
    CONFLICT = "CONFLICT"
    DUPLICATE = "DUPLICATE"


_INDEX_KEY_PREFIXES = {
    IndexKeyKind.METRIC: "metric:",
    IndexKeyKind.ENTITY: "entity:",
    IndexKeyKind.EFFECT: "effect:",
    IndexKeyKind.SOURCE_EVENT: "source-event:",
}

_PORTFOLIO_TOUCH = frozenset({"PERSONAL", "PENSION", "PORTFOLIO_ALL", "NONE"})
_HYPOTHESIS_ORIGIN = "youtube:12man"
_HYPOTHESIS_ID_RE = re.compile(r"^hypothesis:[0-9a-z]{24}$")
_UNARTICULATED = "THESIS_UNARTICULATED"
# Private-path/identity markers block actual secrets and absolute host paths.
# The ordinary word 'secret' alone is NOT blocked (legitimate vocabulary);
# key-shaped tokens (sk-…, ghp_…, AKIA…) are blocked by shape below.
_FORBIDDEN_STATEMENT_MARKERS = (
    "/users/", "~/.local", "tokmo", "/hermes",
)
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
)


class HypothesisIndexKeyV1:
    """One deterministic reverse-index key bound to a hypothesis."""

    __slots__ = ("kind", "value")

    def __init__(self, kind: IndexKeyKind, value: str):
        if not isinstance(kind, IndexKeyKind):
            raise ValueError("invalid index key kind")
        if type(value) is not str:
            raise ValueError("invalid index key value")
        try:
            require_token(value, "index key value")
        except ValueError:
            raise ValueError("invalid index key value")
        prefix = _INDEX_KEY_PREFIXES[kind]
        if not value.startswith(prefix) or value.startswith(prefix + prefix):
            raise ValueError("index key value does not match its kind prefix")
        self.kind = kind
        self.value = value

    def __eq__(self, other):
        return (
            isinstance(other, HypothesisIndexKeyV1)
            and self.kind is other.kind and self.value == other.value
        )

    def __hash__(self):
        return hash((self.kind, self.value))

    def __repr__(self):
        return f"HypothesisIndexKeyV1({self.kind.value},{self.value!r})"


def canonical_index_keys(keys) -> tuple:
    """Dedup + deterministic ordering for an iterable of index keys."""
    seen = {}
    for key in keys:
        if not isinstance(key, HypothesisIndexKeyV1):
            raise ValueError("invalid index key")
        seen[(key.kind.value, key.value)] = key
    return tuple(
        seen[pair] for pair in sorted(seen, key=lambda pair: (pair[0], pair[1]))
    )


def _require_statement(value: str) -> str:
    """Validate and return the canonical (stripped) statement."""
    if type(value) is not str:
        raise ValueError("invalid statement")
    stripped = value.strip()
    if not stripped or len(stripped) > 300:
        raise ValueError("invalid statement")
    if stripped == _UNARTICULATED:
        # User intent gap: never auto-articulated by the system.
        raise ValueError("statement must not be THESIS_UNARTICULATED")
    lowered = stripped.lower()
    for marker in _FORBIDDEN_STATEMENT_MARKERS:
        if marker in lowered:
            raise ValueError("statement contains forbidden marker")
    for shape in _SECRET_SHAPES:
        if shape.search(stripped):
            raise ValueError("statement contains secret-shaped token")
    return stripped


def _require_token_tuple(value, name, *, minimum, maximum) -> None:
    if type(value) is not tuple or not minimum <= len(value) <= maximum:
        raise ValueError(f"invalid {name}")
    for item in value:
        if type(item) is not str:
            raise ValueError(f"invalid {name}")
        try:
            require_token(item, name)
        except ValueError:
            raise ValueError(f"invalid {name}")


_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_FAILURE_REASONS = frozenset({
    "provider_failure", "parser_failure", "budget_exceeded",
    "material_unavailable", "policy_failure", "internal_failure",
})
_RESOLUTION_REASONS = frozenset({
    "provider_failure", "provider_failed", "provider_timeout", "operator_checked",
    "operator_confirmed", "operator_requeue", "checked",
    "settle_failed_after_delivery", "canary_cap_consumed",
    "redaction_rejected",
})


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    _aware(value, "datetime")
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value: str | None) -> datetime | None:
    if value is None:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    _aware(parsed, "datetime")
    return parsed


def created_on_or_after(raw, cutoff: datetime) -> bool:
    """Delivery-selector cutoff eligibility (incident 2026-08-22): a stored
    created_at is eligible only when it parses to an aware instant at or
    after the activation cutoff. The comparison is semantic (Z and explicit
    offsets compare by instant, never by text); a missing, malformed, or
    naive timestamp fails closed to ineligible."""
    _aware(cutoff, "cutoff")
    if type(raw) is not str:
        return False
    try:
        created = parse_iso(raw)
    except ValueError:
        return False
    return created >= cutoff


def require_token(value: str, name: str) -> None:
    if type(value) is not str or not _TOKEN.fullmatch(value):
        raise ValueError(f"invalid {name}")


@dataclass(frozen=True)
class AuditCompletionV1:
    kind: MaterialKindV1
    subject_id: str
    session_date: str
    audited_count: int

    def __post_init__(self):
        if not isinstance(self.kind, MaterialKindV1):
            raise ValueError("invalid audit kind")
        require_token(self.subject_id, "subject_id")
        if type(self.session_date) is not str:
            raise ValueError("invalid session date")
        try:
            parsed = date.fromisoformat(self.session_date)
        except ValueError as error:
            raise ValueError("invalid session date") from error
        if parsed.isoformat() != self.session_date:
            raise ValueError("invalid session date")
        if type(self.audited_count) is not int or not 1 <= self.audited_count <= 5:
            raise ValueError("invalid audited count")


def canonical_job_ticks(value: Mapping[JobKind, int | None]) -> Mapping[JobKind, int | None]:
    if not isinstance(value, Mapping):
        raise ValueError("material ticks must be a mapping")
    result = {}
    for job, tick in value.items():
        if not isinstance(job, JobKind) or (tick is not None and (type(tick) is not int or tick < 0)):
            raise ValueError("invalid material tick")
        result[job] = tick
    return MappingProxyType(dict(sorted(result.items(), key=lambda item: item[0].value)))


@dataclass(frozen=True)
class CandidateManifestV1:
    candidate_id: str
    subject_id: str
    kind: str
    evidence_refs: tuple[str, ...]
    assignment: CandidateAssignment
    state: CandidateState
    created_at: datetime

    def __post_init__(self):
        for value, name in ((self.candidate_id, "candidate_id"), (self.subject_id, "subject_id"),
                            (self.kind, "kind")):
            require_token(value, name)
        if (type(self.evidence_refs) is not tuple
                or any(type(item) is not str or not item for item in self.evidence_refs)):
            raise ValueError("invalid evidence refs")
        if not isinstance(self.assignment, CandidateAssignment) or not isinstance(self.state, CandidateState):
            raise ValueError("invalid candidate enum")
        _aware(self.created_at, "created_at")


@dataclass(frozen=True)
class ManifestSummaryV1:
    tick_id: int
    started_at: datetime
    material_ticks: Mapping[JobKind, int | None]
    material_binding_digest: str
    counts: Mapping[str, int]
    total_count: int
    state: ManifestState
    sealed_at: datetime | None
    failure_reason: str | None


@dataclass(frozen=True)
class DispositionV1:
    decision_id: str
    tick_id: int
    candidate_id: str
    subject_id: str
    action: DispositionAction
    reason_code: str
    cited_evidence_ids: tuple[str, ...]
    material_ticks: Mapping[JobKind, int | None]
    glm_status: str
    sol_status: str
    policy_version: str
    decided_at: datetime

    def __post_init__(self):
        for value, name in (
            (self.decision_id, "decision_id"), (self.candidate_id, "candidate_id"),
            (self.subject_id, "subject_id"), (self.reason_code, "reason_code"),
            (self.glm_status, "glm_status"), (self.sol_status, "sol_status"),
            (self.policy_version, "policy_version"),
        ):
            require_token(value, name)
        if type(self.tick_id) is not int or self.tick_id < 0 or not isinstance(self.action, DispositionAction):
            raise ValueError("invalid disposition")
        if type(self.cited_evidence_ids) is not tuple or any(
            type(item) is not str or not item for item in self.cited_evidence_ids
        ):
            raise ValueError("invalid cited evidence")
        object.__setattr__(self, "material_ticks", canonical_job_ticks(self.material_ticks))
        _aware(self.decided_at, "decided_at")


def alert_dedupe_key(origin: AlertOrigin, subject: str, severity: str) -> str:
    raw = "\0".join((origin.value, subject, severity)).encode()
    return hashlib.sha256(raw).hexdigest()[:32]


@dataclass(frozen=True)
class IncidentKeyV1:
    kind: str
    subject: str

    def __post_init__(self):
        require_token(self.kind, "incident kind")
        require_token(self.subject, "incident subject")


@dataclass(frozen=True)
class AlertOutboxV1:
    alert_id: str
    origin: AlertOrigin
    decision_id: str | None
    incident_kind: str | None
    incident_subject: str | None
    requeue_of: str | None
    tick_id: int
    subject_id: str
    dedupe_key: str
    channel: AlertChannel
    severity: str
    body_evidence: tuple[str, ...]
    created_at: datetime
    attempted_at: datetime | None = None
    delivered_at: datetime | None = None
    status: AlertStatus | None = None
    provider_message_id: str | None = None
    resolution: AlertResolution = AlertResolution.UNRESOLVED
    resolved_at: datetime | None = None
    resolution_reason: str | None = None

    def __post_init__(self):
        require_token(self.alert_id, "alert_id")
        require_token(self.subject_id, "subject_id")
        require_token(self.severity, "severity")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid alert tick")
        if self.origin is AlertOrigin.DISPOSITION:
            if self.decision_id is None or self.incident_kind is not None or self.incident_subject is not None:
                raise ValueError("invalid disposition origin")
        elif self.origin is AlertOrigin.INCIDENT:
            if self.decision_id is not None or self.incident_kind is None or self.incident_subject is None:
                raise ValueError("invalid incident origin")
        elif self.origin is AlertOrigin.NEWS_EVENT:
            if self.decision_id is not None or self.incident_kind is not None or self.incident_subject is not None:
                raise ValueError("invalid news event origin")
            if not self.dedupe_key.startswith("news:"):
                raise ValueError("invalid news event dedupe")
        elif self.origin is AlertOrigin.NEWS_OPS:
            if (self.decision_id is not None or self.incident_kind is not None
                    or self.incident_subject is not None or self.severity != "OPS_FAILURE"
                    or self.dedupe_key != "newsops:" + self.subject_id):
                raise ValueError("invalid news ops origin")
        elif self.origin is AlertOrigin.QUANT_MARKET_CLOSE:
            if (self.decision_id is not None or self.incident_kind is not None
                    or self.incident_subject is not None
                    or not self.dedupe_key.startswith("quant-close:")):
                raise ValueError("invalid quant market close origin")
        else:
            raise ValueError("invalid alert origin")
        if self.channel is AlertChannel.SHADOW:
            if any(value is not None for value in (
                self.status, self.attempted_at, self.delivered_at, self.provider_message_id
            )):
                raise ValueError("shadow delivery fields must be null")
        elif self.channel is AlertChannel.TELEGRAM and self.status is None:
            raise ValueError("telegram status required")
        if type(self.body_evidence) is not tuple:
            raise ValueError("invalid body evidence")
        _aware(self.created_at, "created_at")

    @classmethod
    def for_disposition(cls, *, alert_id, decision_id, tick_id, subject_id,
                        channel, severity, body_evidence, created_at):
        return cls(
            alert_id, AlertOrigin.DISPOSITION, decision_id, None, None, None,
            tick_id, subject_id,
            alert_dedupe_key(AlertOrigin.DISPOSITION, subject_id, severity),
            channel, severity, body_evidence, created_at,
            status=AlertStatus.PENDING if channel is AlertChannel.TELEGRAM else None,
        )

    @classmethod
    def for_incident(cls, *, alert_id, key, tick_id, subject_id,
                     severity, body_evidence, created_at):
        identity = f"{key.kind}\0{key.subject}"
        return cls(
            alert_id, AlertOrigin.INCIDENT, None, key.kind, key.subject, None,
            tick_id, subject_id,
            alert_dedupe_key(AlertOrigin.INCIDENT, identity, severity),
            AlertChannel.SHADOW, severity, body_evidence, created_at,
        )

    @classmethod
    def for_quant_market_close(cls, *, alert_id, tick_id, subject_id,
                               body_evidence, created_at):
        return cls(
            alert_id, AlertOrigin.QUANT_MARKET_CLOSE, None, None, None, None,
            tick_id, subject_id, "quant-close:" + subject_id,
            AlertChannel.SHADOW, "QUANT_CLOSE", body_evidence, created_at,
        )


@dataclass(frozen=True)
class IncidentV1:
    key: IncidentKeyV1
    first_seen_at: datetime
    last_seen_at: datetime
    count: int
    state: IncidentState
    impact_ref: str
    last_notified_at: datetime | None


@dataclass(frozen=True)
class IncidentObservationV1:
    incident: IncidentV1
    notification_required: bool


@dataclass(frozen=True)
class DeferredCandidateV1:
    subject_id: str
    market: Market
    evidence_refs: tuple[str, ...]
    material_ticks: Mapping[JobKind, int | None]
    first_seen_tick: int
    last_attempt_tick: int
    next_review_at: datetime
    attempts: int
    terminal_state: str | None
    updated_at: datetime

    def __post_init__(self):
        require_token(self.subject_id, "subject_id")
        if not isinstance(self.market, Market):
            raise ValueError("invalid market")
        if type(self.evidence_refs) is not tuple:
            raise ValueError("invalid evidence refs")
        for item in self.evidence_refs:
            require_token(item, "evidence_ref")
        object.__setattr__(self, "material_ticks", canonical_job_ticks(self.material_ticks))
        if (type(self.first_seen_tick) is not int
                or type(self.last_attempt_tick) is not int
                or not 0 <= self.first_seen_tick <= 2_147_483_647
                or not self.first_seen_tick <= self.last_attempt_tick <= 2_147_483_647):
            raise ValueError("invalid deferred tick")
        if type(self.attempts) is not int or not 1 <= self.attempts <= 8640:
            raise ValueError("invalid attempts")
        if self.terminal_state is not None and self.terminal_state not in {
            item.value for item in DeferredTerminalState
        }:
            raise ValueError("invalid terminal state")
        _aware(self.next_review_at, "next_review_at")
        _aware(self.updated_at, "updated_at")


_DEFER_REASONS = frozenset({
    "market_closed_defer", "model_unavailable_defer", "capacity_defer",
    "glm_failure_defer", "sol_failure_defer", "next_session_defer",
    "snapshot_incomplete_defer", "evidence_stale_defer",
    "proposal_invalid_defer", "carryover_defer",
})


@dataclass(frozen=True)
class DeferredAttemptV1:
    subject_id: str
    market: Market
    first_seen_tick: int
    attempt_tick: int
    candidate_tick: int
    candidate_id: str
    evidence_refs: tuple[str, ...]
    material_ticks: Mapping[JobKind, int | None]
    next_review_at: datetime
    reason_code: str
    assignment: CandidateAssignment
    attempted_at: datetime

    def __post_init__(self):
        require_token(self.subject_id, "subject_id")
        if not isinstance(self.market, Market):
            raise ValueError("invalid market")
        require_token(self.candidate_id, "candidate_id")
        if (type(self.first_seen_tick) is not int
                or type(self.attempt_tick) is not int
                or type(self.candidate_tick) is not int
                or not 0 <= self.first_seen_tick <= self.attempt_tick <= 2_147_483_647
                or not 0 <= self.candidate_tick <= self.attempt_tick):
            raise ValueError("invalid deferred tick")
        if type(self.evidence_refs) is not tuple:
            raise ValueError("invalid evidence refs")
        for item in self.evidence_refs:
            require_token(item, "evidence_ref")
        object.__setattr__(self, "material_ticks", canonical_job_ticks(self.material_ticks))
        if self.reason_code not in _DEFER_REASONS:
            raise ValueError("invalid defer reason")
        if not isinstance(self.assignment, CandidateAssignment):
            raise ValueError("invalid assignment")
        _aware(self.next_review_at, "next_review_at")
        _aware(self.attempted_at, "attempted_at")


@dataclass(frozen=True)
class AuditBacklogV1:
    kind: str
    subject_id: str
    first_eligible_session: str
    last_considered_session: str
    state: str


@dataclass(frozen=True)
class RequestEndpointV1:
    source: str
    method: str
    host: str
    normalized_path_template: str

    def __post_init__(self):
        require_token(self.source, "source")
        method = self.method.upper()
        host = self.host.lower()
        path = re.sub(r"/+", "/", self.normalized_path_template)
        if method not in {"GET", "HEAD"}:
            raise ValueError("invalid method")
        if any(ord(char) < 32 or ord(char) == 127 for char in host + path):
            raise ValueError("control character")
        parsed = urlsplit("//" + host)
        if (not host or parsed.username is not None or parsed.password is not None
                or parsed.hostname != host or "@" in host):
            raise ValueError("invalid host")
        if (not path.startswith("/") or "?" in path or "#" in path
                or any(part == ".." for part in path.split("/"))):
            raise ValueError("invalid path")
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "host", host)
        object.__setattr__(self, "normalized_path_template", path)


@dataclass(frozen=True)
class ReservationCountsV1:
    runtime: int
    development: int | None


@dataclass(frozen=True)
class FeedbackInboundV1:
    feedback_id: str
    provider: str
    provider_update_id: str
    provider_message_ref: str
    sender_ref: str
    thread_ref: str | None
    received_at: datetime
    raw_text: str
    context_ref: str | None
    capture_source: str
    raw_hash: str
    event_kind: FeedbackEventKind

    @classmethod
    def build(cls, feedback_id, provider, provider_update_id, provider_message_ref,
              sender_ref, thread_ref, received_at, raw_text, context_ref,
              capture_source, event_kind):
        if type(raw_text) is not str:
            raise ValueError("raw text must be string")
        digest = hashlib.sha256(canonical_encode({
            "raw_text": raw_text, "context_ref": context_ref,
            "provider_message_ref": provider_message_ref,
        })).hexdigest()
        return cls(
            feedback_id, provider, provider_update_id, provider_message_ref,
            sender_ref, thread_ref, received_at, raw_text, context_ref,
            capture_source, digest, event_kind,
        )

    def __post_init__(self):
        for value, name in (
            (self.feedback_id, "feedback_id"), (self.provider, "provider"),
            (self.provider_update_id, "provider_update_id"),
            (self.provider_message_ref, "provider_message_ref"),
            (self.sender_ref, "sender_ref"), (self.capture_source, "capture_source"),
        ):
            require_token(value, name)
        if not re.fullmatch(r"[0-9a-f]{64}", self.raw_hash):
            raise ValueError("invalid raw hash")
        if not isinstance(self.event_kind, FeedbackEventKind):
            raise ValueError("invalid feedback kind")
        _aware(self.received_at, "received_at")


def validate_failure_reason(reason: str) -> None:
    if reason not in _FAILURE_REASONS:
        raise ValueError("invalid failure reason")


def validate_resolution_reason(reason: str) -> None:
    # NEWS transport diagnostics deliberately contain only a class and an
    # optional HTTP status. Raw exception URLs/bodies may contain credentials.
    if isinstance(reason,str) and re.fullmatch(r'provider_failure:[A-Za-z_][A-Za-z0-9_]{0,47}(?::[1-5][0-9]{2})?',reason):
        return
    if reason not in _RESOLUTION_REASONS:
        raise ValueError("invalid resolution reason")


def hypothesis_identity_digest(statement: str, trigger_kind: TriggerKind,
                               trigger_expression: str) -> str:
    """Deterministic same-hypothesis identity over the tracking core."""
    return hashlib.sha256(canonical_encode({
        "v": 1,
        "domain": "tokmo-v3:youtube-hypothesis-identity:v1",
        "origin": _HYPOTHESIS_ORIGIN,
        "statement": statement.strip(),
        "trigger_kind": trigger_kind.value,
        "trigger_expression": trigger_expression.strip(),
    })).hexdigest()


@dataclass(frozen=True)
class TrackingCandidateV1:
    """Adapter-validated admission input parsed from an untrusted report."""

    statement: str
    trigger_kind: TriggerKind
    trigger_expression: str
    supporting_evidence: tuple
    counter_evidence: tuple
    invalidation_conditions: tuple
    next_check_at: datetime | None
    portfolio_touch: str
    index_keys: tuple
    source_ref: str
    revision: str
    payload_digest: str
    observed_at: datetime

    def __post_init__(self):
        object.__setattr__(
            self, "statement", _require_statement(self.statement),
        )
        if not isinstance(self.trigger_kind, TriggerKind):
            raise ValueError("invalid trigger kind")
        if (type(self.trigger_expression) is not str
                or not self.trigger_expression.strip()
                or len(self.trigger_expression.strip()) > 200):
            raise ValueError("invalid trigger expression")
        _require_token_tuple(self.supporting_evidence, "supporting evidence",
                             minimum=1, maximum=8)
        _require_token_tuple(self.counter_evidence, "counter evidence",
                             minimum=0, maximum=8)
        _require_token_tuple(self.invalidation_conditions,
                             "invalidation conditions", minimum=1, maximum=8)
        if self.next_check_at is not None:
            _aware(self.next_check_at, "next_check_at")
        if self.portfolio_touch not in _PORTFOLIO_TOUCH:
            raise ValueError("invalid portfolio touch")
        object.__setattr__(
            self, "index_keys", canonical_index_keys(self.index_keys),
        )
        if not 1 <= len(self.index_keys) <= 8:
            raise ValueError("invalid index keys")
        require_token(self.source_ref, "source_ref")
        if (type(self.revision) is not str
                or not 1 <= len(self.revision.strip()) <= 128
                or self.revision != self.revision.strip()
                or any(ch in self.revision for ch in " \t\n\r")):
            raise ValueError("invalid revision")
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_digest):
            raise ValueError("invalid payload digest")
        _aware(self.observed_at, "observed_at")

    @property
    def identity_digest(self) -> str:
        return hypothesis_identity_digest(
            self.statement, self.trigger_kind, self.trigger_expression,
        )


@dataclass(frozen=True)
class HypothesisV1:
    """Durable long-term hypothesis admitted from a tracking candidate."""

    hypothesis_id: str
    state: HypothesisState
    statement: str
    trigger_kind: TriggerKind
    trigger_expression: str
    supporting_evidence: tuple
    counter_evidence: tuple
    invalidation_conditions: tuple
    next_check_at: datetime | None
    portfolio_touch: str
    origin: str
    provenance_first: str
    superseded_by: str | None
    created_at: datetime
    updated_at: datetime

    def __post_init__(self):
        if not (type(self.hypothesis_id) is str
                and self.hypothesis_id.startswith("hypothesis:")
                and _HYPOTHESIS_ID_RE.fullmatch(self.hypothesis_id)):
            raise ValueError("invalid hypothesis id")
        if not isinstance(self.state, HypothesisState):
            raise ValueError("invalid hypothesis state")
        object.__setattr__(
            self, "statement", _require_statement(self.statement),
        )
        if not isinstance(self.trigger_kind, TriggerKind):
            raise ValueError("invalid trigger kind")
        if (type(self.trigger_expression) is not str
                or not self.trigger_expression.strip()):
            raise ValueError("invalid trigger expression")
        _require_token_tuple(self.supporting_evidence, "supporting evidence",
                             minimum=1, maximum=8)
        _require_token_tuple(self.counter_evidence, "counter evidence",
                             minimum=0, maximum=8)
        _require_token_tuple(self.invalidation_conditions,
                             "invalidation conditions", minimum=1, maximum=8)
        if self.next_check_at is not None:
            _aware(self.next_check_at, "next_check_at")
        if self.portfolio_touch not in _PORTFOLIO_TOUCH:
            raise ValueError("invalid portfolio touch")
        if self.origin != _HYPOTHESIS_ORIGIN:
            raise ValueError("invalid hypothesis origin")
        require_token(self.provenance_first, "provenance_first")
        if self.superseded_by is not None:
            if not self.superseded_by.startswith("hypothesis:"):
                raise ValueError("invalid superseded_by")
            if self.superseded_by == self.hypothesis_id:
                raise ValueError("hypothesis cannot supersede itself")
        _aware(self.created_at, "created_at")
        _aware(self.updated_at, "updated_at")


@dataclass(frozen=True)
class HypothesisSightingV1:
    """Append-only observation of a hypothesis from one source revision."""

    hypothesis_id: str
    source_kind: str
    source_ref: str
    revision: str
    payload_digest: str
    evidence_delta: tuple
    observed_at: datetime
    sighting_id: str = ""

    def __post_init__(self):
        if not self.hypothesis_id.startswith("hypothesis:"):
            raise ValueError("invalid hypothesis id")
        if self.source_kind not in {"REPORT", "TRANSCRIPT", "MATERIAL"}:
            raise ValueError("invalid source kind")
        require_token(self.source_ref, "source_ref")
        if (type(self.revision) is not str
                or not 1 <= len(self.revision.strip()) <= 128
                or self.revision != self.revision.strip()
                or any(ch in self.revision for ch in " \t\n\r")):
            raise ValueError("invalid revision")
        if not re.fullmatch(r"[0-9a-f]{64}", self.payload_digest):
            raise ValueError("invalid payload digest")
        _require_token_tuple(self.evidence_delta, "evidence delta",
                             minimum=0, maximum=8)
        _aware(self.observed_at, "observed_at")
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-hypothesis-sighting:v1",
            "hypothesis_id": self.hypothesis_id,
            "source_ref": self.source_ref,
            "revision": self.revision,
            "payload_digest": self.payload_digest,
        })).hexdigest()[:32]
        if self.sighting_id and self.sighting_id != computed:
            raise ValueError("sighting id mismatch")
        object.__setattr__(self, "sighting_id", computed)


@dataclass(frozen=True)
class RecallCandidateV1:
    """Deterministic recall of an open hypothesis against new material."""

    hypothesis_id: str
    statement: str
    state: HypothesisState
    matched_keys: tuple
    match_kind: str
    material_delta_ids: tuple

    def __post_init__(self):
        if not self.hypothesis_id.startswith("hypothesis:"):
            raise ValueError("invalid hypothesis id")
        if type(self.statement) is not str or not self.statement:
            raise ValueError("invalid statement")
        if not isinstance(self.state, HypothesisState):
            raise ValueError("invalid state")
        object.__setattr__(
            self, "matched_keys", canonical_index_keys(self.matched_keys),
        )
        if not self.matched_keys:
            raise ValueError("recall requires at least one matched key")
        if self.match_kind not in {"EXACT", "PARTIAL"}:
            raise ValueError("invalid match kind")
        if type(self.material_delta_ids) is not tuple or not all(
            type(item) is str and item for item in self.material_delta_ids
        ):
            raise ValueError("invalid material delta ids")


@dataclass(frozen=True)
class ShadowRecallRecordV1:
    """Would-have-sent shadow of a recall — delivery is structurally absent."""

    hypothesis_id: str
    material_delta_ids: tuple
    matched_keys: tuple
    match_kind: str
    would_have_sent: bool
    payload_json: str
    recorded_at: datetime
    shadow_id: str = ""

    def __post_init__(self):
        if not self.hypothesis_id.startswith("hypothesis:"):
            raise ValueError("invalid hypothesis id")
        if type(self.material_delta_ids) is not tuple or not all(
            type(item) is str and item for item in self.material_delta_ids
        ):
            raise ValueError("invalid material delta ids")
        object.__setattr__(
            self, "matched_keys", canonical_index_keys(self.matched_keys),
        )
        if not self.matched_keys:
            raise ValueError("shadow record requires matched keys")
        if self.match_kind not in {"EXACT", "PARTIAL"}:
            raise ValueError("invalid match kind")
        # This phase forbids delivery semantics entirely.
        if self.would_have_sent is not False:
            raise ValueError("would_have_sent must be false in this phase")
        if type(self.payload_json) is not str or not self.payload_json:
            raise ValueError("invalid payload json")
        lowered = self.payload_json.lower()
        for marker in ("/users/", "tokmo", ".local", "/hermes"):
            if marker in lowered:
                raise ValueError("payload contains private path marker")
        _aware(self.recorded_at, "recorded_at")
        key_material = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-shadow-recall:v1",
            "hypothesis_id": self.hypothesis_id,
            "material_delta_ids": list(self.material_delta_ids),
            "matched_keys": [
                [key.kind.value, key.value] for key in self.matched_keys
            ],
        })).hexdigest()[:32]
        if self.shadow_id and self.shadow_id != key_material:
            raise ValueError("shadow id mismatch")
        object.__setattr__(self, "shadow_id", key_material)


@dataclass(frozen=True)
class HypothesisLifecycleV1:
    """Append-only lifecycle evidence for one accepted hypothesis transition."""

    hypothesis_id: str
    from_state: HypothesisState
    to_state: HypothesisState
    reason_token: str
    transitioned_at: datetime
    lifecycle_id: str = ""

    def __post_init__(self):
        if not self.hypothesis_id.startswith("hypothesis:"):
            raise ValueError("invalid hypothesis id")
        if not isinstance(self.from_state, HypothesisState):
            raise ValueError("invalid from state")
        if not isinstance(self.to_state, HypothesisState):
            raise ValueError("invalid to state")
        require_token(self.reason_token, "reason_token")
        _aware(self.transitioned_at, "transitioned_at")
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-hypothesis-lifecycle:v1",
            "hypothesis_id": self.hypothesis_id,
            "from_state": self.from_state.value,
            "to_state": self.to_state.value,
            "reason_token": self.reason_token,
        })).hexdigest()[:32]
        if self.lifecycle_id and self.lifecycle_id != computed:
            raise ValueError("lifecycle id mismatch")
        object.__setattr__(self, "lifecycle_id", computed)


_CANDIDATE_FAILURE_KINDS = frozenset({
    "missing_fields", "bad_field", "unarticulated_statement",
    "bad_statement", "bad_trigger", "bad_keys", "duplicate_field",
    "unterminated_block",
})


@dataclass(frozen=True)
class CandidateFailureV1:
    """Durable record of one malformed candidate block (M2).

    The block is quarantined as data — never articulated, never admitted —
    so the user can later decide what the unarticulated thesis meant.
    """

    source_ref: str
    revision: str
    error_kind: str
    block_digest: str
    recorded_at: datetime
    detail_token: str | None = None
    failure_id: str = ""

    def __post_init__(self):
        require_token(self.source_ref, "source_ref")
        if (type(self.revision) is not str
                or not 1 <= len(self.revision.strip()) <= 128
                or self.revision != self.revision.strip()
                or any(ch in self.revision for ch in " \t\n\r")):
            raise ValueError("invalid revision")
        if self.error_kind not in _CANDIDATE_FAILURE_KINDS:
            raise ValueError("invalid failure kind")
        if not re.fullmatch(r"[0-9a-f]{64}", self.block_digest):
            raise ValueError("invalid block digest")
        _aware(self.recorded_at, "recorded_at")
        if self.detail_token is not None:
            require_token(self.detail_token, "detail_token")
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-candidate-failure:v1",
            "source_ref": self.source_ref,
            "revision": self.revision,
            "block_digest": self.block_digest,
            "error_kind": self.error_kind,
        })).hexdigest()[:32]
        if self.failure_id and self.failure_id != computed:
            raise ValueError("failure id mismatch")
        object.__setattr__(self, "failure_id", computed)


# --- YouTube semantic dedupe + weekly review (2026-08-16 wiring) -----------

_SEMANTIC_HORIZONS = frozenset({
    "none", "short", "medium", "long", "indefinite",
})
_SEMANTIC_CONFLICT_FIELDS = frozenset({
    "trigger", "invalidation", "horizon", "touch",
})
# Overlap bands over the union of entity/metric/effect/source-event keys.
_SEMANTIC_HIGH_OVERLAP = 0.5


class SemanticClassification(str, Enum):
    EXACT_DUPLICATE = "EXACT_DUPLICATE"
    SAME_HYPOTHESIS_NEW_SIGHTING = "SAME_HYPOTHESIS_NEW_SIGHTING"
    RELATED_BUT_DISTINCT = "RELATED_BUT_DISTINCT"
    MERGE_REVIEW = "MERGE_REVIEW"
    NO_RELATION = "NO_RELATION"


def _normalize_trigger_expression(expression: str) -> str:
    """Deterministic OR-operand order + whitespace canonicalization."""
    parts = [part.strip() for part in expression.strip().split(" OR ")]
    return " OR ".join(sorted(parts))


def _horizon_bucket(next_check_at, observed_at) -> str:
    """Bucket the raw time horizon (next_check - observation instant).

    Bounded bands keep small drifts (a different observation date, minutes
    of skew) from flipping semantic equality; exact raw datetimes are never
    compared directly.
    """
    if next_check_at is None:
        return "none"
    days = (next_check_at - observed_at).total_seconds() / 86400.0
    if days <= 14:
        return "short"
    if days <= 60:
        return "medium"
    if days <= 180:
        return "long"
    return "indefinite"


@dataclass(frozen=True)
class SemanticProfileV1:
    """Bounded structured profile of one hypothesis/candidate.

    Derived only from fields already present on the tracking candidate or
    hypothesis: no embeddings, aliases, or hidden model state. Equality of
    profiles is structural equality of the semantic core.
    """

    entities: frozenset
    metrics: frozenset
    effects: frozenset
    source_events: frozenset
    trigger_kind: TriggerKind
    trigger_norm: str
    invalidation: tuple
    horizon: str
    portfolio_touch: str

    def __post_init__(self):
        for name in ("entities", "metrics", "effects", "source_events"):
            value = getattr(self, name)
            if type(value) is not frozenset or any(
                type(item) is not str for item in value
            ):
                raise ValueError(f"invalid {name}")
        if not isinstance(self.trigger_kind, TriggerKind):
            raise ValueError("invalid trigger kind")
        if type(self.trigger_norm) is not str or not self.trigger_norm:
            raise ValueError("invalid normalized trigger")
        if type(self.invalidation) is not tuple or any(
            type(item) is not str for item in self.invalidation
        ):
            raise ValueError("invalid invalidation")
        if self.horizon not in _SEMANTIC_HORIZONS:
            raise ValueError("invalid horizon")
        if self.portfolio_touch not in _PORTFOLIO_TOUCH:
            raise ValueError("invalid portfolio touch")

    @classmethod
    def from_candidate(cls, candidate) -> "SemanticProfileV1":
        return cls._build(
            index_keys=candidate.index_keys,
            trigger_kind=candidate.trigger_kind,
            trigger_expression=candidate.trigger_expression,
            invalidation_conditions=candidate.invalidation_conditions,
            next_check_at=candidate.next_check_at,
            observed_at=candidate.observed_at,
            portfolio_touch=candidate.portfolio_touch,
        )

    @classmethod
    def from_hypothesis(cls, hypothesis, *, index_keys=()) -> "SemanticProfileV1":
        return cls._build(
            index_keys=index_keys,
            trigger_kind=hypothesis.trigger_kind,
            trigger_expression=hypothesis.trigger_expression,
            invalidation_conditions=hypothesis.invalidation_conditions,
            next_check_at=hypothesis.next_check_at,
            observed_at=hypothesis.updated_at,
            portfolio_touch=hypothesis.portfolio_touch,
        )

    @classmethod
    def _build(cls, *, index_keys, trigger_kind, trigger_expression,
               invalidation_conditions, next_check_at, observed_at,
               portfolio_touch):
        entities, metrics, effects, source_events = set(), set(), set(), set()
        for item in index_keys:
            if not isinstance(item, HypothesisIndexKeyV1):
                raise ValueError("invalid index key")
            {
                IndexKeyKind.ENTITY: entities,
                IndexKeyKind.METRIC: metrics,
                IndexKeyKind.EFFECT: effects,
                IndexKeyKind.SOURCE_EVENT: source_events,
            }[item.kind].add(item.value)
        return cls(
            entities=frozenset(entities),
            metrics=frozenset(metrics),
            effects=frozenset(effects),
            source_events=frozenset(source_events),
            trigger_kind=trigger_kind,
            trigger_norm=_normalize_trigger_expression(trigger_expression),
            invalidation=tuple(sorted(set(invalidation_conditions))),
            horizon=_horizon_bucket(next_check_at, observed_at),
            portfolio_touch=portfolio_touch,
        )

    def key_overlap(self, other) -> float:
        own = self.entities | self.metrics | self.effects | self.source_events
        theirs = (
            other.entities | other.metrics | other.effects
            | other.source_events
        )
        if not own and not theirs:
            return 0.0
        return len(own & theirs) / len(own | theirs)


@dataclass(frozen=True)
class ClassifierHintV1:
    """Bounded later-classifier seam; cannot override material conflicts."""

    asserts_high_overlap: bool

    def __post_init__(self):
        if type(self.asserts_high_overlap) is not bool:
            raise ValueError("invalid classifier hint")


@dataclass(frozen=True)
class SemanticDecisionV1:
    """Typed decision with reason code and compared fields."""

    classification: SemanticClassification
    reason_code: str
    conflicts: tuple
    overlap: float
    hint_used: bool = False

    def __post_init__(self):
        if not isinstance(self.classification, SemanticClassification):
            raise ValueError("invalid classification")
        if type(self.reason_code) is not str or not self.reason_code:
            raise ValueError("invalid reason code")
        if type(self.conflicts) is not tuple or any(
            item not in _SEMANTIC_CONFLICT_FIELDS for item in self.conflicts
        ):
            raise ValueError("invalid conflict fields")
        if type(self.overlap) is not float or not 0.0 <= self.overlap <= 1.0:
            raise ValueError("invalid overlap")
        if type(self.hint_used) is not bool:
            raise ValueError("invalid hint flag")


_MERGE_REVIEW_STATUSES = frozenset({
    "OPEN", "APPROVED", "REJECTED", "SUPERSEDED",
})
_WEEK_IDENTITY_RE = re.compile(r"^[0-9]{4}-W[0-9]{2}$")


@dataclass(frozen=True)
class HypothesisRelationV1:
    """Directed-store, semantically symmetric distinctness edge."""

    hypothesis_a: str
    hypothesis_b: str
    relation_kind: str
    reason_code: str
    conflicts: tuple
    overlap: float
    origin: str
    created_at: datetime
    relation_id: str = ""

    def __post_init__(self):
        if self.hypothesis_a >= self.hypothesis_b:
            raise ValueError("relation pair must be ordered a<b")
        if self.relation_kind != "RELATED_BUT_DISTINCT":
            raise ValueError("invalid relation kind")
        require_token(self.reason_code, "reason_code")
        if type(self.conflicts) is not tuple or any(
            item not in _SEMANTIC_CONFLICT_FIELDS for item in self.conflicts
        ):
            raise ValueError("invalid conflicts")
        if type(self.overlap) is not float or not 0.0 <= self.overlap <= 1.0:
            raise ValueError("invalid overlap")
        if self.origin not in {"ADMISSION", "WEEKLY_REVIEW"}:
            raise ValueError("invalid origin")
        _aware(self.created_at, "created_at")
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-hypothesis-relation:v1",
            "a": self.hypothesis_a,
            "b": self.hypothesis_b,
            "kind": self.relation_kind,
        })).hexdigest()[:32]
        if self.relation_id and self.relation_id != computed:
            raise ValueError("relation id mismatch")
        object.__setattr__(self, "relation_id", computed)


@dataclass(frozen=True)
class MergeReviewV1:
    """Queue entry for an ambiguous pair awaiting a manual merge decision."""

    hypothesis_a: str
    hypothesis_b: str
    status: str
    reason_code: str
    origin: str
    created_at: datetime
    resolved_at: datetime | None = None
    review_id: str = ""

    def __post_init__(self):
        if self.hypothesis_a >= self.hypothesis_b:
            raise ValueError("review pair must be ordered a<b")
        if self.status not in _MERGE_REVIEW_STATUSES:
            raise ValueError("invalid review status")
        require_token(self.reason_code, "reason_code")
        if self.origin not in {"ADMISSION", "WEEKLY_REVIEW"}:
            raise ValueError("invalid origin")
        _aware(self.created_at, "created_at")
        if self.resolved_at is not None:
            _aware(self.resolved_at, "resolved_at")
        # Fable minor 2: the event identity includes the queueing instant —
        # two resolution events of the SAME status for the same pair (e.g.
        # rejected this month, re-queued and rejected again later) are
        # distinct durable events and may never overwrite each other.
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-merge-review-event:v1",
            "a": self.hypothesis_a,
            "b": self.hypothesis_b,
            "status": self.status,
            "origin": self.origin,
            "queued_at": self.created_at.astimezone(
                timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
        })).hexdigest()[:32]
        if self.review_id and self.review_id != computed:
            raise ValueError("review id mismatch")
        object.__setattr__(self, "review_id", computed)


@dataclass(frozen=True)
class WeeklyReviewRunV1:
    """One week's duplicate-review receipt keyed by KST ISO week."""

    week_identity: str
    status: str
    open_count: int
    pairs_scanned: int
    new_related_edges: int
    new_merge_reviews: int
    same_hypothesis_findings: int
    reserved_at: datetime
    completed_at: datetime | None = None

    def __post_init__(self):
        if not _WEEK_IDENTITY_RE.fullmatch(self.week_identity):
            raise ValueError("invalid week identity")
        if self.status not in {"RESERVED", "COMPLETE"}:
            raise ValueError("invalid run status")
        for name in ("open_count", "pairs_scanned", "new_related_edges",
                     "new_merge_reviews", "same_hypothesis_findings"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 0:
                raise ValueError(f"invalid {name}")
        _aware(self.reserved_at, "reserved_at")
        if self.completed_at is not None:
            _aware(self.completed_at, "completed_at")


@dataclass(frozen=True)
class WeeklyReviewPairV1:
    """Durable per-pair adjudication so same-week replay cannot redo work."""

    week_identity: str
    hypothesis_a: str
    hypothesis_b: str
    classification: str
    reason_code: str
    adjudicated_at: datetime

    def __post_init__(self):
        if not _WEEK_IDENTITY_RE.fullmatch(self.week_identity):
            raise ValueError("invalid week identity")
        if self.hypothesis_a >= self.hypothesis_b:
            raise ValueError("pair must be ordered a<b")
        if self.classification not in {
            item.value for item in SemanticClassification
        }:
            raise ValueError("invalid classification")
        require_token(self.reason_code, "reason_code")
        _aware(self.adjudicated_at, "adjudicated_at")


@dataclass(frozen=True)
class AdmissionDecisionV1:
    """Audit record of one semantic pre-admission comparison."""

    candidate_digest: str
    hypothesis_id: str | None
    matched_hypothesis_id: str | None
    classification: str
    reason_code: str
    conflicts: tuple
    decided_at: datetime
    decision_id: str = ""

    def __post_init__(self):
        if not re.fullmatch(r"[0-9a-f]{64}", self.candidate_digest):
            raise ValueError("invalid candidate digest")
        if self.classification not in {
            item.value for item in SemanticClassification
        }:
            raise ValueError("invalid classification")
        require_token(self.reason_code, "reason_code")
        if type(self.conflicts) is not tuple or any(
            item not in _SEMANTIC_CONFLICT_FIELDS for item in self.conflicts
        ):
            raise ValueError("invalid conflicts")
        _aware(self.decided_at, "decided_at")
        computed = hashlib.sha256(canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-admission-decision:v1",
            "candidate_digest": self.candidate_digest,
            "matched": self.matched_hypothesis_id or "",
            "classification": self.classification,
        })).hexdigest()[:32]
        if self.decision_id and self.decision_id != computed:
            raise ValueError("decision id mismatch")
        object.__setattr__(self, "decision_id", computed)
