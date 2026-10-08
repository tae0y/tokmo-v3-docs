"""Mandatory production cadence wire executor (Stage 3, SDS §7.3).

Every actual provider wire start in the production cadence — TOKEN,
CALENDAR_DAY_KR/US, ACCOUNTS, ACCOUNT_HOLDINGS, PRICE_BATCH, ORDERS_PAGE,
ORDER_DETAIL — is durably PLANNED and atomically STARTED in the operations
kernel under the exact pinned versioned-tick policy *before* the callback
transport runs, and settles exactly once as SUCCEEDED/FAILED/UNKNOWN. An
unknown operation, non-canonical native identity, duplicate identity, policy
mismatch, or total/group/child over-limit fails before the callback: callback
zero, mutation zero.

No provider callable can be reached without a current claim: ``WireClaimV1``
is constructible only inside :class:`CadenceWireExecutorV1`, lives exactly for
the duration of one admitted callback, and every dependency provider validates
it with :func:`require_started_claim` before touching any transport. Calendar
wires keep their existing kernel retry/receipt authority
(``start_calendar_attempt``/``claim_calendar_provider``); this module is the
admission boundary for every other operation, including TOKEN — a bearer
hidden inside an adapter cannot bypass it because the in-memory bearer itself
is acquired through :class:`LazyWireTokenV1` as an admitted TOKEN wire.
"""

from datetime import datetime
from typing import Callable

from tokmo.values.cadence_policy import (
    CadencePolicyV1,
    TradesChildLimitsV1,
    validate_cadence_operation,
)
from tokmo.values.operation_lane_registry import (
    OperationLane,
    OperationLaneRegistryV1,
    OperationOwnershipError,
    active_owner,
)


class WireOutcomeUnknownError(RuntimeError):
    """The callback cannot prove whether the provider wire happened; the
    started row settles UNKNOWN and is never restarted."""


# Module-private issuance guard: WireClaimV1.__post_init__ admits construction
# only while the executor holds this token in the guard cell. Any other
# construction site — production or test — fails with PermissionError before
# a claim object exists (same sealing pattern as the Stage 2 legacy
# capability).
_ISSUER_CAPABILITY = object()
_ISSUER_GUARD: dict = {}


class WireClaimV1:
    """One live, executor-issued proof of a currently STARTED wire row.

    The claim is valid only between the atomic STARTED transition and the
    settlement of the same row; the executor retires it in all outcomes, so a
    stored claim can never authorize a second provider call.
    """

    __slots__ = (
        "policy_version", "scheduled_for_utc", "operation", "operation_group",
        "native_identity", "_live",
    )

    def __init__(self, policy_version: str, scheduled_for_utc: datetime,
                 operation: str, operation_group: str, native_identity: str):
        if _ISSUER_GUARD.get("token") is not _ISSUER_CAPABILITY:
            raise PermissionError("wire claims are executor-issued only")
        self.policy_version = policy_version
        self.scheduled_for_utc = scheduled_for_utc
        self.operation = operation
        self.operation_group = operation_group
        self.native_identity = native_identity
        self._live = True

    def __repr__(self):
        return (
            f"WireClaimV1(operation={self.operation!r}, "
            f"native_identity={self.native_identity!r}, live={self._live})"
        )


def require_started_claim(claim, operation: str) -> WireClaimV1:
    """Fail closed unless ``claim`` is a live executor-issued claim for
    exactly ``operation``. Every read-only dependency provider calls this
    before any transport work, so an unreceipted callback is impossible."""
    if type(claim) is not WireClaimV1:
        raise PermissionError("provider requires an executor-issued wire claim")
    if not claim._live:
        raise PermissionError("wire claim is no longer live")
    if claim.operation != operation:
        raise PermissionError("wire claim operation mismatch")
    return claim


class CadenceWireExecutorV1:
    """The one authoritative production wire executor for a single tick.

    Owned by the operations kernel: every ``execute`` runs the kernel's
    atomic pinned-policy admission (plan -> STARTED under total/group/child
    ceilings) in one transaction before the callback, and settles the same
    durable row exactly once afterwards. Admission failure of any kind is
    raised before the callback runs.
    """

    def __init__(self, *, ops_kernel, role, policy: CadencePolicyV1,
                 scheduled_for_utc: datetime, clock: Callable[[], datetime],
                 child_limits: TradesChildLimitsV1,
                 lane_registry: OperationLaneRegistryV1,
                 lane: OperationLane):
        if not isinstance(policy, CadencePolicyV1):
            raise ValueError("executor requires a cadence policy")
        if not isinstance(child_limits, TradesChildLimitsV1):
            raise ValueError("executor requires trades child limits")
        if (not isinstance(scheduled_for_utc, datetime)
                or scheduled_for_utc.tzinfo is None
                or scheduled_for_utc.utcoffset() is None):
            raise ValueError("scheduled_for_utc must be an aware datetime")
        if not callable(clock):
            raise ValueError("executor requires an injected clock")
        if not isinstance(lane_registry, OperationLaneRegistryV1):
            raise ValueError("executor requires a lane registry")
        if not isinstance(lane, OperationLane):
            raise ValueError("executor requires its lane identity")
        self._kernel = ops_kernel
        self._role = role
        self._policy = policy
        self._slot = scheduled_for_utc
        self._clock = clock
        self._child_limits = child_limits
        self._lane_registry = lane_registry
        self._lane = lane
        self._epoch = int(scheduled_for_utc.timestamp())

    @property
    def policy(self) -> CadencePolicyV1:
        return self._policy

    @property
    def scheduled_for_utc(self) -> datetime:
        return self._slot

    def _issue_claim(self, operation, group, native_identity) -> WireClaimV1:
        _ISSUER_GUARD["token"] = _ISSUER_CAPABILITY
        try:
            return WireClaimV1(
                self._policy.policy_version, self._slot, operation, group,
                native_identity,
            )
        finally:
            _ISSUER_GUARD.pop("token", None)

    def _settle(self, native_identity, outcome) -> None:
        self._kernel.settle_cadence_request(
            self._role, policy_version=self._policy.policy_version,
            scheduled_for_utc=self._slot, native_identity=native_identity,
            outcome=outcome, now=self._clock(),
        )

    def execute(self, operation: str, native_identity: str,
                callback: Callable):
        """Admit, run, and settle exactly one provider wire.

        Validation, durable planning, and the atomic STARTED transition all
        precede the callback; any admission error means callback zero. A
        normal return settles SUCCEEDED; :class:`WireOutcomeUnknownError`
        settles UNKNOWN; any other ``Exception`` settles FAILED — each
        re-raised after settlement. A ``BaseException`` that is not an
        ``Exception`` (hard abort) leaves the row STARTED for the next
        invocation's reconcile, never a fabricated settlement.
        """
        if operation != "TOKEN":
            entry = self._lane_registry.entries.get(operation)
            if entry is None:
                raise OperationOwnershipError(
                    f"operation has no lane owner: {operation}")
            if active_owner(entry, epoch=self._epoch) is not self._lane:
                raise OperationOwnershipError(
                    f"executor lane {self._lane.value} is not the active owner "
                    f"of {operation} at epoch {self._epoch}")
        group = validate_cadence_operation(operation, native_identity)
        self._kernel.plan_cadence_requests(
            self._role, self._policy, scheduled_for_utc=self._slot,
            requests=((operation, native_identity),), now=self._clock(),
            child_limits=self._child_limits,
        )
        self._kernel.start_cadence_request(
            self._role, self._policy, scheduled_for_utc=self._slot,
            operation=operation, native_identity=native_identity,
            now=self._clock(), child_limits=self._child_limits,
        )
        claim = self._issue_claim(operation, group, native_identity)
        try:
            value = callback(claim)
        except WireOutcomeUnknownError:
            claim._live = False
            self._settle(native_identity, "UNKNOWN")
            raise
        except Exception:
            claim._live = False
            self._settle(native_identity, "FAILED")
            raise
        except BaseException:
            claim._live = False
            raise
        claim._live = False
        self._settle(native_identity, "SUCCEEDED")
        return value


class LazyWireTokenV1:
    """One-invocation in-memory bearer, acquired lazily as an admitted wire.

    The bearer is fetched through the executor as the tick's TOKEN operation
    on first use and cached only in process memory: it is never persisted,
    serialized, or exposed by ``repr``. A fully cached, both-markets-closed
    tick that needs no metered operation therefore acquires no token.
    """

    def __init__(self, *, executor: CadenceWireExecutorV1,
                 token_fetch: Callable):
        if not isinstance(executor, CadenceWireExecutorV1):
            raise ValueError("lazy token requires the wire executor")
        if not callable(token_fetch):
            raise ValueError("lazy token requires a token fetch callable")
        self._executor = executor
        self._fetch = token_fetch
        self._value: str | None = None

    def bearer(self) -> str:
        if self._value is None:
            value = self._executor.execute("TOKEN", "token:tick0", self._fetch)
            if type(value) is not str or not value:
                raise ValueError("token fetch must return a bearer string")
            self._value = value
        return self._value

    @property
    def acquired(self) -> bool:
        return self._value is not None

    def __repr__(self):
        return f"LazyWireTokenV1(acquired={self._value is not None})"
