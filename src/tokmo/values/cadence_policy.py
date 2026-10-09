"""Central versioned tick/cadence policy (SDS §7.1, G-11).

This module is the single declaration of every tick admission limit and of the
calendar retry schedule. Admission code, SQL gates, manifests, reports, and
tests import or receive these objects; they never duplicate the numeric
values. Changing a limit means changing this declaration and rerunning the
contract tests.
"""

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

from tokmo.values.material_pack import canonical_encode


@dataclass(frozen=True)
class TickRequestLimitsV1:
    total_wire_starts: int = 27
    token_starts: int = 1
    calendar_starts: int = 2
    holdings_starts: int = 8
    quote_starts: int = 4
    trades_starts: int = 12
    daily_candle_starts: int = 0
    market_daily_candle_starts: int = 0
    quant_daily_candle_starts: int = 0
    membership_starts: int = 0
    # Conditional-order SDS v0.4 §4.4: Pulse-lane-only budget. Every 300s
    # cadence policy keeps the structural default 0 (budget unchanged), and
    # the field is deliberately absent from policy_snapshot so every pinned
    # snapshot byte and digest stays stable.
    conditional_order_starts: int = 0

    def __post_init__(self):
        values = (
            self.total_wire_starts,
            self.token_starts,
            self.calendar_starts,
            self.holdings_starts,
            self.quote_starts,
            self.trades_starts,
            self.daily_candle_starts,
            self.market_daily_candle_starts,
            self.quant_daily_candle_starts,
            self.membership_starts,
            self.conditional_order_starts,
        )
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("tick request limits must be non-negative ints")
        group_sum = (
            self.token_starts
            + self.calendar_starts
            + self.holdings_starts
            + self.quote_starts
            + self.trades_starts
            + self.daily_candle_starts
            + self.membership_starts
            + self.conditional_order_starts
        )
        if group_sum > self.total_wire_starts:
            raise ValueError("group limits exceed total wire starts")
        if (
            self.market_daily_candle_starts + self.quant_daily_candle_starts
            != self.daily_candle_starts
        ):
            raise ValueError("daily candle lanes must equal daily candle starts")


TICK_REQUEST_LIMITS_V1 = TickRequestLimitsV1()


@dataclass(frozen=True)
class CadencePolicyV1:
    policy_version: str = "cadence-300s-v1"
    cadence_seconds: int = 300
    max_start_lag_seconds: int = 60
    child_timeout_seconds: int = 270
    termination_margin_seconds: int = 30
    request_limits: TickRequestLimitsV1 = TICK_REQUEST_LIMITS_V1

    def __post_init__(self):
        if type(self.policy_version) is not str or not self.policy_version:
            raise ValueError("invalid policy version")
        durations = (
            self.cadence_seconds,
            self.max_start_lag_seconds,
            self.child_timeout_seconds,
            self.termination_margin_seconds,
        )
        if any(type(value) is not int or value < 0 for value in durations):
            raise ValueError("cadence durations must be non-negative ints")
        if self.cadence_seconds <= 0:
            raise ValueError("cadence must be positive")
        if (
            self.child_timeout_seconds + self.termination_margin_seconds
            > self.cadence_seconds
        ):
            raise ValueError("child timeout plus termination margin exceeds cadence")
        if not isinstance(self.request_limits, TickRequestLimitsV1):
            raise ValueError("invalid request limits")


CADENCE_POLICY_V1 = CadencePolicyV1()
CADENCE_CORE_SHADOW_POLICY_V1 = CadencePolicyV1(
    policy_version="cadence-300s-core-shadow-v1",
    request_limits=TickRequestLimitsV1(
        total_wire_starts=44,
        daily_candle_starts=17,
        market_daily_candle_starts=17,
        quant_daily_candle_starts=0,
    ),
)
# Market Regime portfolio drain (portfolio-expansion Plan §4.1): V1 plus a
# daily-candle budget of exactly 3 per tick, total 27 + 3. Every other group
# and child ceiling is identical to V1 by construction (dataclass defaults).
CADENCE_MARKET_PORTFOLIO_POLICY_V1 = CadencePolicyV1(
    policy_version="cadence-300s-market-portfolio-v1",
    request_limits=TickRequestLimitsV1(
        total_wire_starts=30,
        daily_candle_starts=3,
        market_daily_candle_starts=3,
        quant_daily_candle_starts=0,
    ),
)

# W3 registration only. The policy is name-resolvable in the closed registry,
# while tools/run_tokmo_cadence.py keeps the Market policy active until the
# separately approved W4 activation pin.
CADENCE_QUANT_SHADOW_POLICY_V1 = CadencePolicyV1(
    policy_version="cadence-300s-market-quant4-shadow-v1",
    request_limits=TickRequestLimitsV1(
        total_wire_starts=45,
        daily_candle_starts=11,
        market_daily_candle_starts=3,
        quant_daily_candle_starts=8,
        membership_starts=7,
    ),
)

# KR/US market-profiles shared orchestrator (Plan/SDS 2026-08-22 §7).
# Registered for the separate G5 activation gate only; never pinned by this
# slice. Ceilings cover the worse single profile per tick (one DUE profile
# executes per tick): membership 7 = max(US 3+4, KR 1), quant candles
# 10 = max(KR 2*5, US 2*4).
CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1 = CadencePolicyV1(
    policy_version="cadence-300s-quant-profiles-shadow-v1",
    request_limits=TickRequestLimitsV1(
        total_wire_starts=47,
        daily_candle_starts=13,
        market_daily_candle_starts=3,
        quant_daily_candle_starts=10,
        membership_starts=7,
    ),
)

# Closed registry of activatable cadence policies (SDS §7.1/§7.3). A new
# policy version (e.g. a 60-second cadence) is added here through review,
# never invented at a call site.
CADENCE_POLICY_REGISTRY = MappingProxyType(
    {
        CADENCE_POLICY_V1.policy_version: CADENCE_POLICY_V1,
        CADENCE_CORE_SHADOW_POLICY_V1.policy_version: CADENCE_CORE_SHADOW_POLICY_V1,
        CADENCE_MARKET_PORTFOLIO_POLICY_V1.policy_version: CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_QUANT_SHADOW_POLICY_V1.policy_version: CADENCE_QUANT_SHADOW_POLICY_V1,
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1.policy_version: CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    }
)


@dataclass(frozen=True)
class CadenceTickV1:
    """The one canonical versioned cadence tick identity (SDS §2.2, B1).

    ``tick_id`` is fully derived from ``(policy_version, scheduled_for_utc)``:
    the slot must be the canonical aware-UTC representation, whole-second,
    exactly aligned to the registered policy's cadence, and
    ``tick_id == epoch_seconds // cadence_seconds``. A caller can therefore
    never choose a colliding tick identity: same policy + same tick_id
    implies the same scheduled slot.
    """

    policy_version: str
    scheduled_for_utc: datetime
    tick_id: int

    def __post_init__(self):
        policy = CADENCE_POLICY_REGISTRY.get(self.policy_version)
        if policy is None:
            raise ValueError("unknown cadence policy version")
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


def cadence_tick_v1(policy_version: str, scheduled_for_utc: datetime) -> CadenceTickV1:
    """Derive the canonical tick identity for one policy/slot pair."""
    policy = CADENCE_POLICY_REGISTRY.get(policy_version)
    if policy is None:
        raise ValueError("unknown cadence policy version")
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
    return CadenceTickV1(
        policy_version,
        scheduled_for_utc,
        epoch // policy.cadence_seconds,
    )


@dataclass(frozen=True)
class TradesChildLimitsV1:
    """Orders-page and order-detail children of the trades group (SDS §7.2)."""

    orders_page_starts: int = 10
    order_detail_starts: int = 2

    def __post_init__(self):
        values = (self.orders_page_starts, self.order_detail_starts)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("trades child limits must be non-negative ints")


TRADES_CHILD_LIMITS_V1 = TradesChildLimitsV1()

# Named operation group -> owning field of TickRequestLimitsV1.
GROUP_LIMIT_FIELDS = MappingProxyType(
    {
        "TOKEN": "token_starts",
        "CALENDAR": "calendar_starts",
        "HOLDINGS": "holdings_starts",
        "QUOTES": "quote_starts",
        "TRADES": "trades_starts",
        "DAILY_CANDLES": "daily_candle_starts",
        "MEMBERSHIP": "membership_starts",
        "CONDITIONAL_ORDER_HISTORY": "conditional_order_starts",
    }
)

# Provider batch cardinality of one PRICE_BATCH wire call (SDS §7.2). The
# quotes group ceiling admits calls; this bounds symbols per admitted call.
QUOTE_BATCH_SYMBOLS_V1 = 200

# Calendar retry schedule (SDS §4): delays after each failed attempt; six
# wire-attempt ordinals (0..5) exist per (market, market_local_date).
CALENDAR_RETRY_DELAY_MINUTES = (5, 15, 30, 60, 120)
CALENDAR_ATTEMPT_MAX = len(CALENDAR_RETRY_DELAY_MINUTES) + 1

# Closed cadence operation enum (SDS §7.2/§7.3): every provider wire start is
# one of these operations, owned by exactly one admission group. No other
# operation string may plan or start a cadence wire row.
CADENCE_OPERATIONS = MappingProxyType(
    {
        "TOKEN": "TOKEN",
        "CALENDAR_DAY_KR": "CALENDAR",
        "CALENDAR_DAY_US": "CALENDAR",
        "ACCOUNTS": "HOLDINGS",
        "ACCOUNT_HOLDINGS": "HOLDINGS",
        "PRICE_BATCH": "QUOTES",
        "ORDERS_PAGE": "TRADES",
        "ORDER_DETAIL": "TRADES",
        "STOCK_CANDLES_DAILY": "DAILY_CANDLES",
        "MEMBERSHIP_NASDAQ_LISTED": "MEMBERSHIP",
        "MEMBERSHIP_OTHER_LISTED": "MEMBERSHIP",
        "MEMBERSHIP_SEC_REGISTRY": "MEMBERSHIP",
        "MEMBERSHIP_SEC_ISSUER": "MEMBERSHIP",
        # KR profile membership: exactly one Toss STOCKS batch per session date
        # (Plan/SDS 2026-08-22 §5.1).
        "MEMBERSHIP_TOSS_STOCKS": "MEMBERSHIP",
        # Conditional-order SDS v0.4 §4.4: Pulse-lane fast-alert scan; the 300s
        # lane never plans it (its pinned snapshots carry a structural zero
        # budget for this group).
        "CONDITIONAL_ORDERS": "CONDITIONAL_ORDER_HISTORY",
    }
)

# Child operations of the trades group -> owning field of TradesChildLimitsV1.
CADENCE_CHILD_LIMIT_FIELDS = MappingProxyType(
    {
        "ORDERS_PAGE": "orders_page_starts",
        "ORDER_DETAIL": "order_detail_starts",
    }
)

# Canonical native-identity shape per closed operation. Native identity
# supports dedup only; it never replaces the mandatory versioned tick binding.
_CADENCE_IDENTITY_PATTERNS = MappingProxyType(
    {
        "TOKEN": re.compile(r"token:tick0"),
        "CALENDAR_DAY_KR": re.compile(r"calendar:KR:\d{4}-\d{2}-\d{2}:[0-5]"),
        "CALENDAR_DAY_US": re.compile(r"calendar:US:\d{4}-\d{2}-\d{2}:[0-5]"),
        "ACCOUNTS": re.compile(r"accounts:tick0"),
        "ACCOUNT_HOLDINGS": re.compile(r"holdings:[1-5]"),
        "PRICE_BATCH": re.compile(r"quotes:(?:0|[1-9]\d{0,2})"),
        "ORDERS_PAGE": re.compile(r"orders:[0-9]"),
        "ORDER_DETAIL": re.compile(r"detail:[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}"),
        "STOCK_CANDLES_DAILY": re.compile(
            r"candles:(mr|qt):(KR|US):[A-Z0-9._-]{1,32}:\d{4}-\d{2}-\d{2}:[12]"
        ),
        "MEMBERSHIP_NASDAQ_LISTED": re.compile(
            r"membership:nasdaqlisted:\d{4}-\d{2}-\d{2}"
        ),
        "MEMBERSHIP_OTHER_LISTED": re.compile(
            r"membership:otherlisted:\d{4}-\d{2}-\d{2}"
        ),
        "MEMBERSHIP_SEC_REGISTRY": re.compile(r"membership:registry:\d{4}-\d{2}-\d{2}"),
        "MEMBERSHIP_SEC_ISSUER": re.compile(
            r"membership:issuer:\d{4}-\d{2}-\d{2}:[A-Z0-9._-]{1,32}"
        ),
        "MEMBERSHIP_TOSS_STOCKS": re.compile(
            r"membership:toss-stocks:\d{4}-\d{2}-\d{2}"
        ),
        "CONDITIONAL_ORDERS": re.compile(r"conditional:(OPEN|CLOSED):[0-9]"),
    }
)


def validate_cadence_operation(operation, native_identity) -> str:
    """Validate one closed operation/native-identity pair; return its group."""
    if type(operation) is not str or operation not in CADENCE_OPERATIONS:
        raise ValueError("unknown cadence operation")
    pattern = _CADENCE_IDENTITY_PATTERNS[operation]
    if type(native_identity) is not str or not pattern.fullmatch(native_identity):
        raise ValueError("non-canonical native identity")
    if operation in ("CALENDAR_DAY_KR", "CALENDAR_DAY_US"):
        date_text = native_identity.split(":")[2]
        try:
            if date.fromisoformat(date_text).isoformat() != date_text:
                raise ValueError
        except ValueError:
            raise ValueError("non-canonical native identity") from None
    if operation == "STOCK_CANDLES_DAILY":
        parts = native_identity.split(":")
        lane, date_text, page = parts[1], parts[4], parts[5]
        try:
            if date.fromisoformat(date_text).isoformat() != date_text:
                raise ValueError
        except ValueError:
            raise ValueError("non-canonical native identity") from None
        if lane == "mr" and page != "1":
            raise ValueError("non-canonical native identity")
    if operation.startswith("MEMBERSHIP_"):
        date_text = native_identity.split(":")[2]
        try:
            if date.fromisoformat(date_text).isoformat() != date_text:
                raise ValueError
        except ValueError:
            raise ValueError("non-canonical native identity") from None
    return CADENCE_OPERATIONS[operation]


def policy_snapshot(policy: CadencePolicyV1, child_limits: TradesChildLimitsV1) -> dict:
    """Full canonical snapshot pinned by the first transaction of a tick."""
    if not isinstance(policy, CadencePolicyV1):
        raise ValueError("invalid cadence policy")
    if not isinstance(child_limits, TradesChildLimitsV1):
        raise ValueError("invalid trades child limits")
    if (
        child_limits.orders_page_starts + child_limits.order_detail_starts
        > policy.request_limits.trades_starts
    ):
        raise ValueError("trades child limits exceed trades group limit")
    limits = policy.request_limits
    return {
        "policy_version": policy.policy_version,
        "cadence_seconds": policy.cadence_seconds,
        "max_start_lag_seconds": policy.max_start_lag_seconds,
        "child_timeout_seconds": policy.child_timeout_seconds,
        "termination_margin_seconds": policy.termination_margin_seconds,
        "request_limits": {
            "total_wire_starts": limits.total_wire_starts,
            "token_starts": limits.token_starts,
            "calendar_starts": limits.calendar_starts,
            "holdings_starts": limits.holdings_starts,
            "quote_starts": limits.quote_starts,
            "trades_starts": limits.trades_starts,
            "daily_candle_starts": limits.daily_candle_starts,
            "membership_starts": limits.membership_starts,
        },
        "trades_child_limits": {
            "orders_page_starts": child_limits.orders_page_starts,
            "order_detail_starts": child_limits.order_detail_starts,
        },
    }


def policy_digest(policy: CadencePolicyV1, child_limits: TradesChildLimitsV1) -> str:
    return hashlib.sha256(
        canonical_encode(policy_snapshot(policy, child_limits))
    ).hexdigest()


# Targeted one-shot lane (G2, delta Plan/SDS v0.2 §15.1): zero trades
# children so the pinned snapshot stays feasible with trades_starts=0.
TARGETED_ONESHOT_CHILD_LIMITS_V1 = TradesChildLimitsV1(
    orders_page_starts=0,
    order_detail_starts=0,
)

CADENCE_TARGETED_ONESHOT_POLICY_V1 = CadencePolicyV1(
    policy_version="cadence-300s-targeted-oneshot-v1",
    request_limits=TickRequestLimitsV1(
        total_wire_starts=2,
        token_starts=1,
        calendar_starts=0,
        holdings_starts=0,
        quote_starts=0,
        trades_starts=0,
        daily_candle_starts=1,
        market_daily_candle_starts=1,
        quant_daily_candle_starts=0,
    ),
)

# One-shot 전용 폐쇄 registry. CADENCE_POLICY_REGISTRY에는 절대 넣지 않는다:
# 그래야 run_production_cadence_once / cadence_tick_v1 / CadenceTickV1 이
# 이 policy_version을 구조적으로 거부하여 recurring 활성화가 불가능하다.
TARGETED_ONESHOT_POLICY_REGISTRY = MappingProxyType(
    {
        CADENCE_TARGETED_ONESHOT_POLICY_V1.policy_version: CADENCE_TARGETED_ONESHOT_POLICY_V1,
    }
)


@dataclass(frozen=True)
class TargetedOneShotSlotV1:
    """One-shot attempt의 canonical slot identity.

    ``scheduled_for_utc`` 는 등록된 targeted policy의 grid에 정렬된
    whole-second aware UTC 여야 하며, 그렇지 않으면 생성 자체가 불가능하다.
    """

    policy_version: str
    scheduled_for_utc: datetime

    def __post_init__(self):
        policy = TARGETED_ONESHOT_POLICY_REGISTRY.get(self.policy_version)
        if policy is None:
            raise ValueError("unknown targeted one-shot policy version")
        slot = self.scheduled_for_utc
        if (
            not isinstance(slot, datetime)
            or slot.tzinfo is None
            or slot.utcoffset() != timedelta(0)
        ):
            raise ValueError("scheduled_for_utc must be aware UTC")
        if slot.microsecond != 0:
            raise ValueError("scheduled_for_utc must be a whole second")
        if int(slot.timestamp()) % policy.cadence_seconds != 0:
            raise ValueError("scheduled slot is not aligned to the policy cadence")


def targeted_oneshot_slot(
    policy_version: str, observed_now_utc: datetime
) -> TargetedOneShotSlotV1:
    """Floor observed now onto the registered targeted policy's grid."""
    policy = TARGETED_ONESHOT_POLICY_REGISTRY.get(policy_version)
    if policy is None:
        raise ValueError("unknown targeted one-shot policy version")
    if (
        not isinstance(observed_now_utc, datetime)
        or observed_now_utc.tzinfo is None
        or observed_now_utc.utcoffset() is None
    ):
        raise ValueError("observed_now_utc must be an aware datetime")
    epoch = int(observed_now_utc.timestamp())
    slot = datetime.fromtimestamp(epoch - epoch % policy.cadence_seconds, tz=UTC)
    return TargetedOneShotSlotV1(policy_version, slot)
