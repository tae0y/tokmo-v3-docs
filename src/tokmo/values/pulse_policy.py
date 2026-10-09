"""Pulse 60-second lane policy and tick identity (60s Pulse Plan/SDS §7/§12).

The Pulse lane reuses the reviewed ``CadencePolicyV1``/``TickRequestLimitsV1``
value types but registers its policy in a separate closed registry, exactly
like the targeted one-shot precedent: ``CadenceTickV1``/``cadence_tick_v1``
structurally reject the pulse version and ``PulseTickV1`` structurally
rejects every 300s policy, so the two recurring pipelines can never consume
each other's identity.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from types import MappingProxyType

from tokmo.values.cadence_policy import (
    CADENCE_OPERATIONS,
    CADENCE_POLICY_REGISTRY,
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)

# Core-only §12.2 + conditional-order SDS v0.4 §4.4: token1 + quotes2 + trades3 +
# conditional1 = total 7; the operations Pulse cannot express
# (calendar/holdings/candles) have a structural zero budget.
PULSE_REQUEST_LIMITS_V1 = TickRequestLimitsV1(
    total_wire_starts=7,
    token_starts=1,
    calendar_starts=0,
    holdings_starts=0,
    quote_starts=2,
    trades_starts=3,
    daily_candle_starts=0,
    conditional_order_starts=1,
)

# Approved 2026-09-28 input capacity: protect core maxima independently.
PULSE_QUANT_INPUT_STARTS = 4
PULSE_OPPORTUNITY_INPUT_STARTS = 6
PULSE_ACTIVE_WIRE_STARTS = (
    PULSE_REQUEST_LIMITS_V1.total_wire_starts
    + PULSE_QUANT_INPUT_STARTS
    + PULSE_OPPORTUNITY_INPUT_STARTS
)

# §12.2: OrdersPage 2 + OrderDetail 1 inside the trades group of 3.
PULSE_TRADES_CHILD_LIMITS_V1 = TradesChildLimitsV1(
    orders_page_starts=2,
    order_detail_starts=1,
)

PULSE_POLICY_V1 = CadencePolicyV1(
    policy_version="pulse-60s-v1",
    cadence_seconds=60,
    max_start_lag_seconds=15,
    child_timeout_seconds=40,
    termination_margin_seconds=20,
    request_limits=PULSE_REQUEST_LIMITS_V1,
)

# Optional G0 declaration. Composition remains pinned to PULSE_POLICY_V1 and
# the runner defaults to None, so merge alone starts no provider operation.
PULSE_QUANT_MARKET_CLOSE_POLICY_V1 = CadencePolicyV1(
    policy_version="pulse-60s-quant-market-close-v1",
    cadence_seconds=60,
    max_start_lag_seconds=10,
    child_timeout_seconds=30,
    termination_margin_seconds=10,
    request_limits=TickRequestLimitsV1(
        total_wire_starts=24,
        token_starts=1,
        calendar_starts=0,
        holdings_starts=0,
        quote_starts=0,
        trades_starts=0,
        daily_candle_starts=16,
        quant_daily_candle_starts=16,
        membership_starts=7,
        conditional_order_starts=0,
    ),
)

# Pulse 전용 폐쇄 registry (§7.1). CADENCE_POLICY_REGISTRY에는 절대 넣지
# 않는다: 그래야 run_production_cadence_once / cadence_tick_v1 이 pulse
# policy를 구조적으로 거부하고, Pulse runner는 300초 policy를 거부한다.
PULSE_POLICY_REGISTRY = MappingProxyType(
    {
        PULSE_POLICY_V1.policy_version: PULSE_POLICY_V1,
        PULSE_QUANT_MARKET_CLOSE_POLICY_V1.policy_version: PULSE_QUANT_MARKET_CLOSE_POLICY_V1,
    }
)

# §12.1: the only provider operations a Pulse tick can express. Everything
# else is not admission-denied but unrepresentable in the Pulse code path.
# CONDITIONAL_ORDERS joins as the fast-alert scan (conditional SDS §4.4).
PULSE_OPERATIONS = (
    "TOKEN",
    "PRICE_BATCH",
    "ORDERS_PAGE",
    "ORDER_DETAIL",
    "CONDITIONAL_ORDERS",
)

if set(PULSE_POLICY_REGISTRY) & set(CADENCE_POLICY_REGISTRY):
    raise RuntimeError("pulse policy leaked into the cadence registry")
if any(operation not in CADENCE_OPERATIONS for operation in PULSE_OPERATIONS):
    raise RuntimeError("pulse operation outside the closed cadence enum")
if (
    PULSE_TRADES_CHILD_LIMITS_V1.orders_page_starts
    + PULSE_TRADES_CHILD_LIMITS_V1.order_detail_starts
    > PULSE_REQUEST_LIMITS_V1.trades_starts
):
    raise RuntimeError("pulse trades children exceed the trades group")


@dataclass(frozen=True)
class PulseTickV1:
    """Canonical Pulse tick identity: 60s-grid slot bound to the registry.

    Same construction contract as ``CadenceTickV1`` — aware whole-second
    UTC slot aligned to the registered policy cadence with
    ``tick_id == epoch // cadence_seconds`` — but resolved against
    ``PULSE_POLICY_REGISTRY`` only, so the identities are mutually
    unassignable at the type level.
    """

    policy_version: str
    scheduled_for_utc: datetime
    tick_id: int

    def __post_init__(self):
        policy = PULSE_POLICY_REGISTRY.get(self.policy_version)
        if policy is None:
            raise ValueError("unknown pulse policy version")
        slot = self.scheduled_for_utc
        if (
            not isinstance(slot, datetime)
            or slot.tzinfo is None
            or slot.utcoffset() != timedelta(0)
        ):
            raise ValueError("scheduled_for_utc must be aware UTC")
        if slot.microsecond != 0:
            raise ValueError("scheduled_for_utc must be a whole second")
        epoch = int(slot.timestamp())
        if epoch % policy.cadence_seconds != 0:
            raise ValueError("scheduled slot is not aligned to the policy cadence")
        if (
            type(self.tick_id) is not int
            or self.tick_id != epoch // policy.cadence_seconds
        ):
            raise ValueError("tick_id must derive from policy and scheduled slot")

    @property
    def start_utc(self) -> datetime:
        return self.scheduled_for_utc


def pulse_tick_v1(policy_version: str, scheduled_for_utc: datetime) -> PulseTickV1:
    """Derive the canonical Pulse tick identity for one policy/slot pair."""
    policy = PULSE_POLICY_REGISTRY.get(policy_version)
    if policy is None:
        raise ValueError("unknown pulse policy version")
    if (
        not isinstance(scheduled_for_utc, datetime)
        or scheduled_for_utc.tzinfo is None
        or scheduled_for_utc.utcoffset() != timedelta(0)
        or scheduled_for_utc.microsecond != 0
    ):
        raise ValueError("scheduled_for_utc must be aware UTC")
    epoch = int(scheduled_for_utc.timestamp())
    if epoch % policy.cadence_seconds != 0:
        raise ValueError("scheduled slot is not aligned to the policy cadence")
    return PulseTickV1(
        policy_version,
        scheduled_for_utc,
        epoch // policy.cadence_seconds,
    )
