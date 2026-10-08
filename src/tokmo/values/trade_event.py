"""Trade-event identity, state machine, and time contract (Plan §17.3/§20).

A trade event is one newly INSERTED completed execution. Its identity is the
canonical hash of the ledger's own uniqueness key, so event identity and
ledger identity are 1:1 and replay can never mint a second event for the
same execution. All derived dialogue identities hash from it.
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import hashlib
from types import MappingProxyType

from tokmo.values.material_pack import canonical_encode

# §17.3: filled_at vs discovered_at gaps at or above this are labelled
# late-arrival in every user-facing message. The cause is never narrated.
LATE_ARRIVAL_THRESHOLD_SECONDS = 3600


class TradeEventState(str, Enum):
    DETECTED = "DETECTED"
    EVALUATED = "EVALUATED"
    ENQUEUED = "ENQUEUED"
    SENT = "SENT"
    SUPPRESSED = "SUPPRESSED"
    ANSWERED = "ANSWERED"
    EXPIRED = "EXPIRED"


_TRANSITIONS = MappingProxyType({
    TradeEventState.DETECTED: frozenset({TradeEventState.EVALUATED}),
    TradeEventState.EVALUATED: frozenset({
        TradeEventState.ENQUEUED, TradeEventState.SUPPRESSED,
    }),
    TradeEventState.ENQUEUED: frozenset({
        TradeEventState.SENT, TradeEventState.SUPPRESSED,
        TradeEventState.EXPIRED,
    }),
    TradeEventState.SENT: frozenset({
        TradeEventState.ANSWERED, TradeEventState.EXPIRED,
    }),
    TradeEventState.SUPPRESSED: frozenset(),
    TradeEventState.ANSWERED: frozenset(),
    TradeEventState.EXPIRED: frozenset(),
})


class TradeEventTransitionError(ValueError):
    pass


def validate_transition(current: TradeEventState, target: TradeEventState) -> None:
    if not isinstance(current, TradeEventState) or not isinstance(
            target, TradeEventState):
        raise TradeEventTransitionError("unknown trade event state")
    if target not in _TRANSITIONS[current]:
        raise TradeEventTransitionError(
            f"illegal transition {current.value}->{target.value}")


def _hash(parts) -> str:
    return hashlib.sha256(canonical_encode(list(parts))).hexdigest()


def trade_event_id(source: str, account_scope: str,
                   source_record_key: str) -> str:
    """Canonical hash of the ledger UNIQUE(source, scope, record key)."""
    for value in (source, account_scope, source_record_key):
        if type(value) is not str or not value:
            raise ValueError("invalid trade event identity component")
    return _hash(["trade-event-v1", source, account_scope, source_record_key])


def evaluation_request_id(event_id: str, evaluation_policy_version: str) -> str:
    return _hash(["evaluation-v1", event_id, evaluation_policy_version])


def dialogue_dedupe_key(event_id: str, prompt_kind: str,
                        prompt_version: str) -> str:
    if prompt_kind not in PROMPT_KINDS:
        raise ValueError("unknown prompt kind")
    return _hash(["dialogue-v1", event_id, prompt_kind, prompt_version])


def dialogue_instance_dedupe_key(event_id: str, prompt_kind: str,
                                 prompt_version: str, instance: str) -> str:
    """Per-instance outbox identity for repeatable prompts (button UI).

    A preview or input prompt can legitimately recur for one event (수정 →
    재입력 → 새 preview), so its identity carries the triggering provider
    update as ``instance``: a replay of the same update maps to the same
    row (no double send) while a genuinely new attempt mints a new row.
    """
    if prompt_kind not in PROMPT_KINDS:
        raise ValueError("unknown prompt kind")
    if type(instance) is not str or not instance:
        raise ValueError("invalid dialogue instance marker")
    return _hash(["dialogue-instance-v1", event_id, prompt_kind,
                  prompt_version, instance])


def reason_append_id(event_id: str, inbound_message_id: int) -> str:
    if type(inbound_message_id) is not int:
        raise ValueError("invalid inbound message id")
    return _hash(["reason-append-v1", event_id, str(inbound_message_id)])


def conditional_alert_id(conditional_order_id: str, leg_index: int,
                         transition_seq: str) -> str:
    """Per-leg-trigger alert identity (conditional SDS v0.4 §4.1).

    ``transition_seq`` is the transition's resulting status token: a leg
    transitions into a given status at most once, so the sequence marker
    and the UNIQUE(order, leg, new_status) restart no-op stay aligned and
    the id is deterministic across restarts and re-observations.
    """
    if type(conditional_order_id) is not str or not conditional_order_id:
        raise ValueError("invalid conditional order id")
    if type(leg_index) is not int or leg_index not in (0, 1):
        raise ValueError("invalid conditional leg index")
    if type(transition_seq) is not str or not transition_seq:
        raise ValueError("invalid conditional transition seq")
    return _hash(["conditional-alert-v1", conditional_order_id,
                  str(leg_index), transition_seq])


# "conditional_fill_question" is the conditional SDS v0.4 §4.2 NAMED change
# site: the closed prompt-kind set widens here and nowhere else (the store's
# enqueue validation reads this same constant).
#
# The receipt kinds are the 2026-08-21 UX contract. "trade_reason_preview" is
# retained for old in-flight bubbles only: nothing enqueues a new preview.
PROMPT_KINDS = frozenset({
    "trade_reason_question", "trade_reason_reconfirm", "trade_reason_preview",
    "trade_reason_input_prompt", "conditional_fill_question",
    "trade_reason_receipt", "trade_reason_pending_receipt",
})

EVALUATION_CLASSIFICATIONS = frozenset({
    "match", "partial", "mismatch", "insufficient_context",
})


def _aware(value) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


@dataclass(frozen=True)
class TradeEventTimesV1:
    """§17.3 five separated instants; absent provider times stay None."""

    filled_at: datetime
    provider_observed_at: "datetime | None"
    discovered_at: datetime
    stored_at: datetime
    notified_at: "datetime | None"

    def __post_init__(self):
        if not _aware(self.filled_at) or not _aware(self.discovered_at) \
                or not _aware(self.stored_at):
            raise ValueError("event instants must be aware datetimes")
        if self.provider_observed_at is not None and not _aware(
                self.provider_observed_at):
            raise ValueError("provider_observed_at must be aware or None")
        if self.notified_at is not None and not _aware(self.notified_at):
            raise ValueError("notified_at must be aware or None")

    @property
    def late_arrival(self) -> bool:
        delta = self.discovered_at - self.filled_at
        return delta.total_seconds() >= LATE_ARRIVAL_THRESHOLD_SECONDS


@dataclass(frozen=True)
class TradeEventEvaluationV1:
    """Deterministic/AI evaluation transport (§19).

    Prose is the primary product; the bounded classification is internal
    transport only and never becomes user-facing canonical policy. The
    inferred-reason field is structurally null: no AI output can carry a
    guess about why the user traded.
    """

    trade_event_id: str
    evaluation_policy_version: str
    prose: str
    classification: "str | None"
    material_sufficient: bool
    ai_consulted: bool
    inferred_trade_reason: None = None

    def __post_init__(self):
        if type(self.trade_event_id) is not str or len(self.trade_event_id) != 64:
            raise ValueError("invalid trade event id")
        if type(self.evaluation_policy_version) is not str \
                or not self.evaluation_policy_version:
            raise ValueError("invalid evaluation policy version")
        if type(self.prose) is not str or not self.prose:
            raise ValueError("evaluation prose is the primary product")
        if self.classification is not None \
                and self.classification not in EVALUATION_CLASSIFICATIONS:
            raise ValueError("unknown evaluation classification")
        if type(self.material_sufficient) is not bool \
                or type(self.ai_consulted) is not bool:
            raise ValueError("invalid evaluation flags")
        if self.inferred_trade_reason is not None:
            raise ValueError("trade reasons are never inferred or stored by AI")
        if self.ai_consulted and not self.material_sufficient:
            raise ValueError("AI consultation requires sufficient material")
