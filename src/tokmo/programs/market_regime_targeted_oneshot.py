"""Bounded production one-shot operator seam (G2, delta Plan/SDS v0.2 §15.3).

Admits exactly one explicit-target market-regime analysis against the real
operations kernel under the registered ``cadence-300s-targeted-oneshot-v1``
policy, then returns a sanitized evidence dict. Everything durable is the
canonical operational stores the recurring cadence already owns: the ops
kernel, the regime state store, the read-only calendar cache/session history,
and the same exclusive ``runtime/cadence.lock`` lease. The reviewed G1 seam
``run_market_regime_targeted`` is called unchanged; this module adds no
selection, no retry, no recurring registration, and imports no live
transport — providers arrive injected through ``deps``.
"""

import os
import sqlite3
from contextlib import closing
from datetime import datetime

from tokmo.programs.cadence_wire import CadenceWireExecutorV1, LazyWireTokenV1
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1, OperationLane,
)
from tokmo.programs.market_regime_shadow import run_market_regime_targeted
from tokmo.programs.production_cadence import _acquire_lease
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CADENCE_TARGETED_ONESHOT_POLICY_V1,
    TARGETED_ONESHOT_CHILD_LIMITS_V1,
    targeted_oneshot_slot,
)
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market

# Evidence strings are bounded so an unvalidated target cannot smuggle an
# unbounded payload into the operator's JSON line.
_EVIDENCE_TEXT_MAX = 64


def _text(value):
    return value[:_EVIDENCE_TEXT_MAX] if type(value) is str else None


def _empty_wire_counts() -> dict:
    return {
        "total_started": 0, "group_started": {}, "operation_started": {},
        "state_counts": {},
    }


def _wire_counts(kernel: OpsKernelStore, policy_version: str,
                 slot: datetime) -> dict:
    report = kernel.read_cadence_tick_report(policy_version, slot)
    if report is None:
        return _empty_wire_counts()
    return {
        "total_started": report["total_started"],
        "group_started": dict(report["group_started"]),
        "operation_started": dict(report["operation_started"]),
        "state_counts": dict(report["state_counts"]),
    }


def _state_readback(root, policy_version: str, *, market, symbol,
                    invocation_id) -> dict | None:
    """Latest regime state row for the validated ``(market, symbol)`` under
    the targeted policy version.

    The binding is mandatory: a later one-shot for another symbol under the
    same policy must never surface as this target's evidence. No state
    filter — FAILED/CLAIMED rows return unchanged, with ``failure_code``
    exposed and ``owned_by_this_invocation`` marking whether this invocation
    wrote the row (a mismatch is normal on the reuse path).
    """
    path = root / "ops" / "market_regime_shadow.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT c.state, c.market, c.symbol, c.completed_session_date,"
            " c.failure_code, c.owner_invocation_id,"
            " r.pack_digest, r.normalized_bar_count"
            " FROM market_regime_shadow_claim c"
            " LEFT JOIN market_regime_shadow_result r"
            " ON r.identity_digest = c.identity_digest"
            " WHERE c.policy_version = ? AND c.market = ? AND c.symbol = ?"
            " ORDER BY c.claimed_at DESC, c.rowid DESC LIMIT 1",
            (policy_version, market, symbol),
        ).fetchone()
    if row is None:
        return None
    return {
        "state": row["state"], "market": row["market"],
        "symbol": row["symbol"],
        "completed_session": row["completed_session_date"],
        "failure_code": row["failure_code"],
        "owned_by_this_invocation":
            row["owner_invocation_id"] == invocation_id,
        "pack_digest": row["pack_digest"],
        "normalized_bar_count": row["normalized_bar_count"],
    }


def run_market_regime_targeted_oneshot(*, root, deps, target,
                                       observed_now_utc, invocation_id) -> dict:
    """Admit one targeted analysis under the registered one-shot policy.

    The slot is derived by flooring ``observed_now_utc`` onto the policy
    grid, so the attempt identity ``(policy_version, slot)`` is canonical by
    construction. Lease contention returns without any durable row: the
    attempt is not consumed. Target validation itself belongs to the G1 seam.
    """
    if (not isinstance(observed_now_utc, datetime)
            or observed_now_utc.tzinfo is None
            or observed_now_utc.utcoffset() is None):
        raise ValueError("observed_now_utc must be an aware datetime")
    policy = CADENCE_TARGETED_ONESHOT_POLICY_V1
    slot = targeted_oneshot_slot(policy.policy_version, observed_now_utc)
    evidence = {
        "policy_version": policy.policy_version,
        "scheduled_for_utc": slot.scheduled_for_utc.isoformat(),
        "market": (target.market.value
                   if isinstance(getattr(target, "market", None), Market)
                   else None),
        "symbol": _text(getattr(target, "symbol", None)),
        "requested_by": _text(getattr(target, "requested_by", None)),
        "invocation_id": _text(invocation_id),
    }
    lease = _acquire_lease(root)
    if lease is None:
        return {"status": "skipped", "reason": "overlap_lock_contention",
                **evidence, "wire": _empty_wire_counts(), "state_row": None}
    try:
        kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        # Settle any STARTED row a crashed prior invocation left behind with
        # the same rule the recurring tick applies (§16-3): consumed, UNKNOWN,
        # never restarted.
        kernel.reconcile_cadence(ProducerRole.COLLECTOR, now=deps.clock())
        executor = CadenceWireExecutorV1(
            ops_kernel=kernel, role=ProducerRole.COLLECTOR, policy=policy,
            scheduled_for_utc=slot.scheduled_for_utc, clock=deps.clock,
            child_limits=TARGETED_ONESHOT_CHILD_LIMITS_V1,
            lane_registry=OPERATION_LANE_REGISTRY_V1,
            lane=OperationLane.CADENCE_300S,
        )
        token = LazyWireTokenV1(executor=executor,
                                token_fetch=deps.token_fetch)
        # Read-only view of the cache the recurring cadence maintains: with
        # calendar_starts=0 a stale cache fails closed as session-unavailable.
        cache_store = CalendarCacheStore(
            root / "ops" / "calendar_cache.json", clock=deps.clock,
        )
        status, reason = run_market_regime_targeted(
            root=root, deps=deps, policy=policy, tick=slot, executor=executor,
            token=token, cache_store=cache_store,
            invocation_id=invocation_id, target=target,
        )
        wire = _wire_counts(kernel, policy.policy_version,
                            slot.scheduled_for_utc)
    finally:
        os.close(lease)
    return {"status": status, "reason": reason, **evidence, "wire": wire,
            "state_row": _state_readback(
                root, policy.policy_version, market=evidence["market"],
                symbol=evidence["symbol"], invocation_id=invocation_id)}
