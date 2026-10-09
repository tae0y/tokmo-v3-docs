"""Stage 1-F: one central versioned tick/cadence policy declaration (G-11, SDS §7.1)."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime

import pytest

# Immutable W1 bytes measured after adding the inactive membership shape.
# These are deliberately literals, never derived from the post-change policy.
POLICY_SNAPSHOT_JSON_LITERALS = {
    "cadence-300s-v1": '{"cadence_seconds":300,"child_timeout_seconds":270,"max_start_lag_seconds":60,"policy_version":"cadence-300s-v1","request_limits":{"calendar_starts":2,"daily_candle_starts":0,"holdings_starts":8,"membership_starts":0,"quote_starts":4,"token_starts":1,"total_wire_starts":27,"trades_starts":12},"termination_margin_seconds":30,"trades_child_limits":{"order_detail_starts":2,"orders_page_starts":10}}',
    "cadence-300s-core-shadow-v1": '{"cadence_seconds":300,"child_timeout_seconds":270,"max_start_lag_seconds":60,"policy_version":"cadence-300s-core-shadow-v1","request_limits":{"calendar_starts":2,"daily_candle_starts":17,"holdings_starts":8,"membership_starts":0,"quote_starts":4,"token_starts":1,"total_wire_starts":44,"trades_starts":12},"termination_margin_seconds":30,"trades_child_limits":{"order_detail_starts":2,"orders_page_starts":10}}',
    "cadence-300s-market-portfolio-v1": '{"cadence_seconds":300,"child_timeout_seconds":270,"max_start_lag_seconds":60,"policy_version":"cadence-300s-market-portfolio-v1","request_limits":{"calendar_starts":2,"daily_candle_starts":3,"holdings_starts":8,"membership_starts":0,"quote_starts":4,"token_starts":1,"total_wire_starts":30,"trades_starts":12},"termination_margin_seconds":30,"trades_child_limits":{"order_detail_starts":2,"orders_page_starts":10}}',
    "cadence-300s-market-quant4-shadow-v1": '{"cadence_seconds":300,"child_timeout_seconds":270,"max_start_lag_seconds":60,"policy_version":"cadence-300s-market-quant4-shadow-v1","request_limits":{"calendar_starts":2,"daily_candle_starts":11,"holdings_starts":8,"membership_starts":7,"quote_starts":4,"token_starts":1,"total_wire_starts":45,"trades_starts":12},"termination_margin_seconds":30,"trades_child_limits":{"order_detail_starts":2,"orders_page_starts":10}}',
    "cadence-300s-quant-profiles-shadow-v1": '{"cadence_seconds":300,"child_timeout_seconds":270,"max_start_lag_seconds":60,"policy_version":"cadence-300s-quant-profiles-shadow-v1","request_limits":{"calendar_starts":2,"daily_candle_starts":13,"holdings_starts":8,"membership_starts":7,"quote_starts":4,"token_starts":1,"total_wire_starts":47,"trades_starts":12},"termination_margin_seconds":30,"trades_child_limits":{"order_detail_starts":2,"orders_page_starts":10}}',
}
POLICY_DIGEST_LITERALS = {
    "cadence-300s-v1": "571f8aa25368c595949911868733727166e51a9591538c61a05672c9b7910564",
    "cadence-300s-core-shadow-v1": "d81f2b754027ff13126be5294c708a07078fdf7026e1db9e74447e4e0361d571",
    "cadence-300s-market-portfolio-v1": "5093db5a9c75ca5817e56d099b4cc7e98a579cf37c8b0fd3e45e87cd4e849546",
    "cadence-300s-market-quant4-shadow-v1": "de64f5fe5e4e9dc5fe88a34c15d3d5f48ff8c9712c1e56a0ed51852963004d36",
    "cadence-300s-quant-profiles-shadow-v1": "35d73088e7f925d05ba9d2b134d07280874de7123a2017f5c5d119e13195fa1f",
}


def test_production_v1_policy_matches_the_reviewed_sds_declaration():
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_V1,
        TICK_REQUEST_LIMITS_V1,
        TRADES_CHILD_LIMITS_V1,
    )

    assert CADENCE_POLICY_V1.policy_version == "cadence-300s-v1"
    assert CADENCE_POLICY_V1.cadence_seconds == 300
    assert CADENCE_POLICY_V1.max_start_lag_seconds == 60
    assert CADENCE_POLICY_V1.child_timeout_seconds == 270
    assert CADENCE_POLICY_V1.termination_margin_seconds == 30
    assert CADENCE_POLICY_V1.request_limits is TICK_REQUEST_LIMITS_V1
    limits = CADENCE_POLICY_V1.request_limits
    assert (
        limits.total_wire_starts,
        limits.token_starts,
        limits.calendar_starts,
        limits.holdings_starts,
        limits.quote_starts,
        limits.trades_starts,
    ) == (
        27,
        1,
        2,
        8,
        4,
        12,
    )
    assert (
        TRADES_CHILD_LIMITS_V1.orders_page_starts,
        TRADES_CHILD_LIMITS_V1.order_detail_starts,
    ) == (10, 2)


def test_group_sum_must_not_exceed_total_wire_starts():
    from tokmo.values.cadence_policy import TICK_REQUEST_LIMITS_V1

    with pytest.raises(ValueError, match="group limits exceed total wire starts"):
        replace(
            TICK_REQUEST_LIMITS_V1, quote_starts=TICK_REQUEST_LIMITS_V1.quote_starts + 1
        )


def test_g1ca_lane_validation_and_group_sum_non_regression():
    from tokmo.values.cadence_policy import (
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        GROUP_LIMIT_FIELDS,
        TickRequestLimitsV1,
    )

    with pytest.raises(ValueError, match="non-negative ints"):
        TickRequestLimitsV1(market_daily_candle_starts=-1)
    with pytest.raises(ValueError, match="non-negative ints"):
        TickRequestLimitsV1(quant_daily_candle_starts=-1)
    with pytest.raises(ValueError, match="daily candle lanes must equal"):
        TickRequestLimitsV1(
            total_wire_starts=30,
            daily_candle_starts=3,
            market_daily_candle_starts=0,
            quant_daily_candle_starts=0,
        )
    with pytest.raises(ValueError, match="daily candle lanes must equal"):
        TickRequestLimitsV1(
            total_wire_starts=30,
            daily_candle_starts=3,
            market_daily_candle_starts=2,
            quant_daily_candle_starts=0,
        )
    with pytest.raises(ValueError, match="daily candle lanes must equal"):
        TickRequestLimitsV1(
            total_wire_starts=30,
            daily_candle_starts=3,
            market_daily_candle_starts=3,
            quant_daily_candle_starts=1,
        )
    with pytest.raises(ValueError, match="daily candle lanes must equal"):
        TickRequestLimitsV1(market_daily_candle_starts=1)
    limits = CADENCE_MARKET_PORTFOLIO_POLICY_V1.request_limits
    assert sum(getattr(limits, field) for field in GROUP_LIMIT_FIELDS.values()) == 30
    assert "market_daily_candle_starts" not in GROUP_LIMIT_FIELDS.values()
    assert "quant_daily_candle_starts" not in GROUP_LIMIT_FIELDS.values()


def test_g1ca_all_real_policy_lanes_registry_and_pulse_defaults():
    from tokmo.values.cadence_policy import (
        CADENCE_CORE_SHADOW_POLICY_V1,
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_POLICY_REGISTRY,
        CADENCE_POLICY_V1,
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
        CADENCE_QUANT_SHADOW_POLICY_V1,
        CADENCE_TARGETED_ONESHOT_POLICY_V1,
    )
    from tokmo.values.pulse_policy import PULSE_POLICY_V1

    expected = {
        CADENCE_POLICY_V1: (0, 0, 0),
        CADENCE_CORE_SHADOW_POLICY_V1: (17, 17, 0),
        CADENCE_MARKET_PORTFOLIO_POLICY_V1: (3, 3, 0),
        CADENCE_QUANT_SHADOW_POLICY_V1: (11, 3, 8),
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1: (13, 3, 10),
        CADENCE_TARGETED_ONESHOT_POLICY_V1: (1, 1, 0),
        PULSE_POLICY_V1: (0, 0, 0),
    }
    for policy, lanes in expected.items():
        limits = policy.request_limits
        assert (
            limits.daily_candle_starts,
            limits.market_daily_candle_starts,
            limits.quant_daily_candle_starts,
        ) == lanes
    assert tuple(CADENCE_POLICY_REGISTRY) == (
        "cadence-300s-v1",
        "cadence-300s-core-shadow-v1",
        "cadence-300s-market-portfolio-v1",
        "cadence-300s-market-quant4-shadow-v1",
        "cadence-300s-quant-profiles-shadow-v1",
    )
    assert CADENCE_QUANT_SHADOW_POLICY_V1.request_limits.quant_daily_candle_starts == 8
    assert CADENCE_QUANT_SHADOW_POLICY_V1.request_limits.membership_starts == 7
    assert (
        CADENCE_POLICY_REGISTRY[CADENCE_QUANT_SHADOW_POLICY_V1.policy_version]
        is CADENCE_QUANT_SHADOW_POLICY_V1
    )
    from tools.run_tokmo_cadence import ACTIVE_POLICY

    assert ACTIVE_POLICY is CADENCE_MARKET_PORTFOLIO_POLICY_V1
    assert ACTIVE_POLICY.request_limits.quant_daily_candle_starts == 0
    assert ACTIVE_POLICY.request_limits.membership_starts == 0


def test_g1ca_snapshot_digest_bytes_and_tmp_kernel_repin_are_unchanged(tmp_path):
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_REGISTRY,
        TradesChildLimitsV1,
        policy_digest,
        policy_snapshot,
    )
    from tokmo.values.material_pack import canonical_encode
    from tokmo.values.ops_kernel import ProducerRole

    child = TradesChildLimitsV1()
    slot = datetime(2026, 8, 12, 0, 0, tzinfo=UTC)
    now = datetime(2026, 8, 12, 0, 0, 1, tzinfo=UTC)
    for version, policy in CADENCE_POLICY_REGISTRY.items():
        assert (
            canonical_encode(policy_snapshot(policy, child)).decode()
            == (POLICY_SNAPSHOT_JSON_LITERALS[version])
        )
        assert policy_digest(policy, child) == POLICY_DIGEST_LITERALS[version]
        path = tmp_path / f"{version}.sqlite3"
        store = OpsKernelStore(path)
        store.pin_cadence_tick(
            ProducerRole.COLLECTOR,
            policy,
            scheduled_for_utc=slot,
            now=now,
            child_limits=child,
        )
        with closing(sqlite3.connect(path)) as connection, connection:
            row = connection.execute(
                "SELECT snapshot_json,policy_digest FROM cadence_ticks"
            ).fetchone()
        assert row == (
            POLICY_SNAPSHOT_JSON_LITERALS[version],
            POLICY_DIGEST_LITERALS[version],
        )
        store.pin_cadence_tick(
            ProducerRole.COLLECTOR,
            policy,
            scheduled_for_utc=slot,
            now=now,
            child_limits=child,
        )


def test_timeout_plus_margin_must_fit_inside_cadence():
    from tokmo.values.cadence_policy import CADENCE_POLICY_V1

    with pytest.raises(ValueError):
        replace(CADENCE_POLICY_V1, termination_margin_seconds=31)


def test_child_limits_must_fit_inside_trades_group():
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_V1,
        TRADES_CHILD_LIMITS_V1,
        policy_snapshot,
    )

    policy_snapshot(CADENCE_POLICY_V1, TRADES_CHILD_LIMITS_V1)
    with pytest.raises(ValueError):
        policy_snapshot(
            CADENCE_POLICY_V1,
            replace(TRADES_CHILD_LIMITS_V1, orders_page_starts=11),
        )


def test_policy_digest_is_stable_and_sensitive_to_every_limit():
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_V1,
        TRADES_CHILD_LIMITS_V1,
        policy_digest,
        policy_snapshot,
    )

    digest = policy_digest(CADENCE_POLICY_V1, TRADES_CHILD_LIMITS_V1)
    assert digest == policy_digest(CADENCE_POLICY_V1, TRADES_CHILD_LIMITS_V1)
    assert len(digest) == 64

    changed = replace(
        CADENCE_POLICY_V1,
        request_limits=replace(CADENCE_POLICY_V1.request_limits, quote_starts=3),
    )
    assert policy_digest(changed, TRADES_CHILD_LIMITS_V1) != digest

    snapshot = policy_snapshot(CADENCE_POLICY_V1, TRADES_CHILD_LIMITS_V1)
    assert snapshot["policy_version"] == CADENCE_POLICY_V1.policy_version
    assert snapshot["request_limits"]["total_wire_starts"] == (
        CADENCE_POLICY_V1.request_limits.total_wire_starts
    )
    assert snapshot["trades_child_limits"]["orders_page_starts"] == (
        TRADES_CHILD_LIMITS_V1.orders_page_starts
    )


def test_operation_groups_map_onto_named_limit_fields():
    from tokmo.values.cadence_policy import GROUP_LIMIT_FIELDS, TICK_REQUEST_LIMITS_V1

    assert set(GROUP_LIMIT_FIELDS) == {
        "TOKEN",
        "CALENDAR",
        "HOLDINGS",
        "QUOTES",
        "TRADES",
        "DAILY_CANDLES",
        "MEMBERSHIP",
        "CONDITIONAL_ORDER_HISTORY",
    }
    for field_name in GROUP_LIMIT_FIELDS.values():
        assert hasattr(TICK_REQUEST_LIMITS_V1, field_name)


def test_calendar_retry_schedule_is_centrally_declared():
    from tokmo.values.cadence_policy import (
        CALENDAR_ATTEMPT_MAX,
        CALENDAR_RETRY_DELAY_MINUTES,
    )

    assert CALENDAR_RETRY_DELAY_MINUTES == (5, 15, 30, 60, 120)
    assert CALENDAR_ATTEMPT_MAX == len(CALENDAR_RETRY_DELAY_MINUTES) + 1
