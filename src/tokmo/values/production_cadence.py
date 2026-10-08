"""Typed values for the Stage 3 production cadence tick (SDS §2, §6, §9).

One fixed stage order, closed outcome/status vocabularies, typed lifecycle
events for the injected local sink, and the read-only per-wire account
directory carrier. Everything here is token-shaped: no secret, account scope,
symbol, quantity, URL, or raw exception text may pass through these types.
"""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from tokmo.values.current_holdings import AccountScopeIdentityV1

CADENCE_MODE_LIVE_SHADOW = "live_shadow"
CADENCE_MODE_PRODUCTION = "production"

# The fixed one-tick state machine order (SDS Stage 3 A). Snapshot persistence
# and the open projection happen inside the holdings collector but are
# recorded as their own stages so the durable receipt proves each transition.
CADENCE_STAGE_ORDER = (
    "lease", "tick", "calendar", "history", "market_regime_shadow",
    "quant_membership", "mask", "holdings", "snapshot",
    "projection", "quotes", "trades", "barrier", "deltas", "admission",
    "analysis_shadow", "candidates", "quant_shadow", "sink",
)

STAGE_STATUS_TOKENS = frozenset({
    "ok", "skipped", "failed", "blocked", "not_reached",
})


class AnalysisExecutionV1:
    """Marker contract for a bounded fixture-only EVENT execution facade."""

    def __call__(self, *, tick, barrier, store, now):
        raise NotImplementedError


class CadenceOutcome(str, Enum):
    COMPLETE = "COMPLETE"
    SKIPPED_CLOSED = "SKIPPED_CLOSED"
    PARTIAL = "PARTIAL"
    FAILED = "FAILED"
    MISSED = "MISSED"
    DUPLICATE = "DUPLICATE"
    OVERLAP = "OVERLAP"


# CLI exit codes (SDS §9): truthful completion/skip is 0; every duplicate,
# missed, overlap, partial, or failed terminal is 2. Configuration/preflight
# refusals exit 1 before any provider work and never become an outcome.
OUTCOME_EXIT_CODES = MappingProxyType({
    CadenceOutcome.COMPLETE: 0,
    CadenceOutcome.SKIPPED_CLOSED: 0,
    CadenceOutcome.PARTIAL: 2,
    CadenceOutcome.FAILED: 2,
    CadenceOutcome.MISSED: 2,
    CadenceOutcome.DUPLICATE: 2,
    CadenceOutcome.OVERLAP: 2,
})


@dataclass(frozen=True)
class StageStatusV1:
    """One stage's terminal status inside a single cadence tick."""

    stage: str
    status: str
    reason: str | None = None
    latency_ms: int = 0

    def __post_init__(self):
        if self.stage not in CADENCE_STAGE_ORDER:
            raise ValueError("unknown cadence stage")
        if self.status not in STAGE_STATUS_TOKENS:
            raise ValueError("unknown stage status")
        if self.reason is not None and (
                type(self.reason) is not str or not self.reason):
            raise ValueError("invalid stage reason")
        if type(self.latency_ms) is not int or self.latency_ms < 0:
            raise ValueError("invalid stage latency")


LIFECYCLE_EVENT_KINDS = frozenset({
    "tick_started", "stage_finished", "tick_finished",
})


@dataclass(frozen=True)
class CadenceLifecycleEventV1:
    """Typed local lifecycle event (SDS Stage 3 C).

    The only thing a live-shadow tick may emit to its injected sink: fixed
    kind/stage/status tokens plus the versioned tick identity. Stock
    signal/analysis alerts stay disabled and no transport is implied.
    """

    kind: str
    policy_version: str
    tick_id: int
    at: datetime
    stage: str | None = None
    status: str | None = None
    reason: str | None = None

    def __post_init__(self):
        if self.kind not in LIFECYCLE_EVENT_KINDS:
            raise ValueError("unknown lifecycle event kind")
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("invalid policy version")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid tick id")
        if (not isinstance(self.at, datetime) or self.at.tzinfo is None
                or self.at.utcoffset() is None):
            raise ValueError("event time must be aware")
        if self.stage is not None and self.stage not in CADENCE_STAGE_ORDER:
            raise ValueError("unknown lifecycle stage")


def null_lifecycle_sink(event: CadenceLifecycleEventV1) -> None:
    """The default sink: local no-op (no transport, no persistence)."""
    return None


@dataclass(frozen=True)
class CadenceAccountsV1:
    """Read-only account directory from one admitted ACCOUNTS wire.

    ``native_refs`` maps account scope to the provider-native opaque
    reference each later per-account wire needs. Under Plan §17.1(a),
    these refs may be persisted to a receipt-bound private store
    (``AccountNativeRefStoreV1``) via the native ref writer seam; the
    raw ref value is never logged, exposed in exceptions, or shown in
    repr/str (it carries ``repr=False``).
    """

    identities: tuple[AccountScopeIdentityV1, ...]
    native_refs: Mapping[str, str] = field(repr=False)
    fingerprints: Mapping[str, str] = field(repr=False)

    def __post_init__(self):
        if (type(self.identities) is not tuple or not self.identities
                or not all(isinstance(item, AccountScopeIdentityV1)
                           for item in self.identities)):
            raise ValueError("invalid account identities")
        scopes = [item.account_scope for item in self.identities]
        if len(set(scopes)) != len(scopes):
            raise ValueError("duplicate account scope")
        for mapping in (self.native_refs, self.fingerprints):
            if not isinstance(mapping, Mapping):
                raise ValueError("invalid account mapping")
        if set(self.native_refs) != set(scopes):
            raise ValueError("native refs must cover every account scope")
        object.__setattr__(
            self, "native_refs", MappingProxyType(dict(self.native_refs)),
        )
        object.__setattr__(
            self, "fingerprints", MappingProxyType(dict(self.fingerprints)),
        )
