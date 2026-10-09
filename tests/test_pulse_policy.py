"""Pulse 60s policy/identity contracts (60s Pulse Plan/SDS §7, §12, §26)."""

from datetime import UTC, datetime

import pytest

from tokmo.values.cadence_policy import (
    CADENCE_OPERATIONS,
    CADENCE_POLICY_REGISTRY,
    CadenceTickV1,
    cadence_tick_v1,
    policy_snapshot,
)
from tokmo.values.pulse_policy import (
    PULSE_OPERATIONS,
    PULSE_POLICY_REGISTRY,
    PULSE_POLICY_V1,
    PULSE_REQUEST_LIMITS_V1,
    PULSE_TRADES_CHILD_LIMITS_V1,
    PulseTickV1,
    pulse_tick_v1,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


SLOT = _utc("2026-08-10T00:05:00Z")


def test_pulse_policy_arithmetic_is_exactly_the_plan_envelope():
    # §7.1: 40 + 20 <= 60, lag 15.
    assert PULSE_POLICY_V1.policy_version == "pulse-60s-v1"
    assert PULSE_POLICY_V1.cadence_seconds == 60
    assert PULSE_POLICY_V1.max_start_lag_seconds == 15
    assert PULSE_POLICY_V1.child_timeout_seconds == 40
    assert PULSE_POLICY_V1.termination_margin_seconds == 20
    assert (
        PULSE_POLICY_V1.child_timeout_seconds
        + PULSE_POLICY_V1.termination_margin_seconds
        == PULSE_POLICY_V1.cadence_seconds
    )


def test_pulse_request_limits_group_sum_is_exactly_seven():
    # §12.2 + conditional SDS §4.4: token1 + quotes2 + trades3 +
    # conditional1 = total 7; every other group is zero.
    limits = PULSE_REQUEST_LIMITS_V1
    assert limits.total_wire_starts == 7
    assert limits.token_starts == 1
    assert limits.quote_starts == 2
    assert limits.trades_starts == 3
    assert limits.conditional_order_starts == 1
    assert limits.calendar_starts == 0
    assert limits.holdings_starts == 0
    assert limits.daily_candle_starts == 0
    assert (
        limits.token_starts
        + limits.calendar_starts
        + limits.holdings_starts
        + limits.quote_starts
        + limits.trades_starts
        + limits.daily_candle_starts
        + limits.conditional_order_starts
        == limits.total_wire_starts
    )


def test_pulse_trades_children_fit_inside_the_trades_group():
    # §12.2 mutation guard: OrdersPage2 + OrderDetail1 <= trades3.
    child = PULSE_TRADES_CHILD_LIMITS_V1
    assert child.orders_page_starts == 2
    assert child.order_detail_starts == 1
    assert (
        child.orders_page_starts + child.order_detail_starts
        <= PULSE_REQUEST_LIMITS_V1.trades_starts
    )
    # The pinned snapshot must stay feasible with these exact children.
    snapshot = policy_snapshot(PULSE_POLICY_V1, child)
    assert snapshot["request_limits"]["total_wire_starts"] == 7
    # the frozen snapshot format deliberately omits the pulse-only
    # conditional budget so every pinned 300s digest stays stable
    assert "conditional_order_starts" not in snapshot["request_limits"]
    assert snapshot["trades_child_limits"] == {
        "orders_page_starts": 2,
        "order_detail_starts": 1,
    }


def test_pulse_expressible_operations_are_the_closed_five():
    # §12.1 + conditional SDS §4.4: TOKEN, PRICE_BATCH, ORDERS_PAGE,
    # ORDER_DETAIL, CONDITIONAL_ORDERS only, and every one of them is a
    # known cadence operation string (no invented names).
    assert PULSE_OPERATIONS == (
        "TOKEN",
        "PRICE_BATCH",
        "ORDERS_PAGE",
        "ORDER_DETAIL",
        "CONDITIONAL_ORDERS",
    )
    for operation in PULSE_OPERATIONS:
        assert operation in CADENCE_OPERATIONS
    for forbidden in (
        "CALENDAR_DAY_KR",
        "CALENDAR_DAY_US",
        "ACCOUNTS",
        "ACCOUNT_HOLDINGS",
        "STOCK_CANDLES_DAILY",
    ):
        assert forbidden not in PULSE_OPERATIONS


def test_pulse_registry_is_closed_and_disjoint_from_cadence_registry():
    assert set(PULSE_POLICY_REGISTRY) == {
        "pulse-60s-v1",
        "pulse-60s-quant-market-close-v1",
    }
    assert not set(PULSE_POLICY_REGISTRY) & set(CADENCE_POLICY_REGISTRY)


def test_pulse_tick_aligns_to_the_60s_grid():
    tick = pulse_tick_v1("pulse-60s-v1", SLOT)
    assert tick.tick_id == int(SLOT.timestamp()) // 60
    assert tick.scheduled_for_utc == SLOT
    with pytest.raises(ValueError):
        pulse_tick_v1("pulse-60s-v1", _utc("2026-08-10T00:05:30Z"))
    with pytest.raises(ValueError):
        pulse_tick_v1("pulse-60s-v1", datetime(2026, 8, 10, 0, 5))  # naive rejected


def test_same_wall_clock_pulse_and_cadence_ticks_have_distinct_identity():
    # §7.2: same wall instant, different (policy_version, tick_id).
    shared = _utc("2026-08-10T01:00:00Z")
    pulse = pulse_tick_v1("pulse-60s-v1", shared)
    cadence = cadence_tick_v1("cadence-300s-market-portfolio-v1", shared)
    assert pulse.policy_version != cadence.policy_version
    assert pulse.tick_id != cadence.tick_id
    assert pulse.tick_id == cadence.tick_id * 5


def test_cadence_registry_structurally_rejects_the_pulse_policy():
    # §7.1: the pulse version can never form a CadenceTickV1.
    with pytest.raises(ValueError):
        cadence_tick_v1("pulse-60s-v1", SLOT)
    with pytest.raises(ValueError):
        CadenceTickV1("pulse-60s-v1", SLOT, int(SLOT.timestamp()) // 60)


def test_pulse_identity_structurally_rejects_cadence_policies():
    slot300 = _utc("2026-08-10T00:05:00Z")
    for version in CADENCE_POLICY_REGISTRY:
        with pytest.raises(ValueError):
            pulse_tick_v1(version, slot300)
        with pytest.raises(ValueError):
            PulseTickV1(version, slot300, int(slot300.timestamp()) // 60)


def test_pulse_tick_id_must_derive_from_slot():
    epoch = int(SLOT.timestamp())
    with pytest.raises(ValueError):
        PulseTickV1("pulse-60s-v1", SLOT, epoch // 60 + 1)
    with pytest.raises(ValueError):
        PulseTickV1("pulse-60s-v1", SLOT.replace(tzinfo=None), epoch // 60)


def test_pulse_policy_registry_rejects_unknown_versions():
    with pytest.raises(ValueError):
        pulse_tick_v1("pulse-60s-v99", SLOT)


def test_rolling_five_minute_ceiling_stays_below_five_x():
    # §12.3 no-5x: 5 pulse slots per 300s window, ≤7 each → ≤35, and the
    # documented steady-state combined ceiling 300s(≤14) + pulse(≤35) ≤ 49
    # (was 44 before the conditional SDS §4.4 +1-per-tick lane).
    per_window_pulse = 5 * PULSE_REQUEST_LIMITS_V1.total_wire_starts
    assert per_window_pulse == 35
    assert per_window_pulse < 150  # the forbidden naive 5x replication
    cadence_after_cutover = 30 - 4 - 12  # total30 − quotes4 − trades12
    assert cadence_after_cutover + per_window_pulse == 49


def test_utc_aware_non_utc_offset_slot_is_rejected():
    from datetime import timedelta
    from datetime import timezone as tz

    seoul = tz(timedelta(hours=9))
    with pytest.raises(ValueError):
        pulse_tick_v1("pulse-60s-v1", SLOT.astimezone(seoul))
    assert (
        pulse_tick_v1("pulse-60s-v1", SLOT.astimezone(UTC)).tick_id
        == int(SLOT.timestamp()) // 60
    )
