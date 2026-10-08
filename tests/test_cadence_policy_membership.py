"""W1 cadence declaration for the inactive Quant recurring lane."""

from dataclasses import fields
from datetime import date, datetime, timezone

import pytest


def test_quant_policy_budget_is_exactly_saturated_and_registered_in_w3():
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_REGISTRY,
        CADENCE_QUANT_SHADOW_POLICY_V1,
        GROUP_LIMIT_FIELDS,
        TickRequestLimitsV1,
    )

    limits = CADENCE_QUANT_SHADOW_POLICY_V1.request_limits
    assert CADENCE_QUANT_SHADOW_POLICY_V1.policy_version == (
        "cadence-300s-market-quant4-shadow-v1"
    )
    assert limits.total_wire_starts == 45
    assert limits.membership_starts == 7
    assert (limits.daily_candle_starts, limits.market_daily_candle_starts,
            limits.quant_daily_candle_starts) == (11, 3, 8)
    assert sum(getattr(limits, name) for name in GROUP_LIMIT_FIELDS.values()) == 45
    assert (CADENCE_POLICY_REGISTRY[CADENCE_QUANT_SHADOW_POLICY_V1.policy_version]
            is CADENCE_QUANT_SHADOW_POLICY_V1)

    with pytest.raises(ValueError, match="group limits exceed total wire starts"):
        TickRequestLimitsV1(
            total_wire_starts=44,
            daily_candle_starts=11,
            market_daily_candle_starts=3,
            quant_daily_candle_starts=8,
            membership_starts=7,
        )


def test_membership_group_snapshot_operations_and_lane_ownership_are_closed():
    from tokmo.values.cadence_policy import (
        CADENCE_OPERATIONS,
        CADENCE_POLICY_REGISTRY,
        GROUP_LIMIT_FIELDS,
        TRADES_CHILD_LIMITS_V1,
        TickRequestLimitsV1,
        policy_snapshot,
        validate_cadence_operation,
    )
    from tokmo.values.operation_lane_registry import (
        OPERATION_LANE_REGISTRY_V1,
        OperationLane,
    )

    expected = {
        "MEMBERSHIP_NASDAQ_LISTED": "membership:nasdaqlisted:2026-08-14",
        "MEMBERSHIP_OTHER_LISTED": "membership:otherlisted:2026-08-14",
        "MEMBERSHIP_SEC_REGISTRY": "membership:registry:2026-08-14",
        "MEMBERSHIP_SEC_ISSUER": "membership:issuer:2026-08-14:AAPL",
    }
    assert GROUP_LIMIT_FIELDS["MEMBERSHIP"] == "membership_starts"
    assert GROUP_LIMIT_FIELDS["MEMBERSHIP"] in {
        item.name for item in fields(TickRequestLimitsV1)
    }
    for operation, identity in expected.items():
        assert CADENCE_OPERATIONS[operation] == "MEMBERSHIP"
        assert validate_cadence_operation(operation, identity) == "MEMBERSHIP"
        entry = OPERATION_LANE_REGISTRY_V1.entries[operation]
        assert entry.recurring_owner is OperationLane.CADENCE_300S
        assert entry.fallback_owner is None
        assert entry.cutover_epoch_utc is None
    for bad in (
        "membership:nasdaqlisted:2026-02-30",
        "membership:issuer:2026-08-14:aapl",
        "membership:issuer:AAPL",
    ):
        operation = (
            "MEMBERSHIP_SEC_ISSUER" if "issuer" in bad
            else "MEMBERSHIP_NASDAQ_LISTED"
        )
        with pytest.raises(ValueError, match="non-canonical native identity"):
            validate_cadence_operation(operation, bad)
    for policy in CADENCE_POLICY_REGISTRY.values():
        snapshot = policy_snapshot(policy, TRADES_CHILD_LIMITS_V1)
        expected_membership = 7 if policy.policy_version in (
            "cadence-300s-market-quant4-shadow-v1",
            "cadence-300s-quant-profiles-shadow-v1") else 0
        assert snapshot["request_limits"]["membership_starts"] == expected_membership


def test_quant_stages_are_exactly_positioned_and_constructible():
    from tokmo.values.production_cadence import (
        CADENCE_STAGE_ORDER,
        CadenceLifecycleEventV1,
        StageStatusV1,
    )

    assert len(CADENCE_STAGE_ORDER) == 19
    assert CADENCE_STAGE_ORDER[4:7] == (
        "market_regime_shadow", "quant_membership", "mask",
    )
    assert CADENCE_STAGE_ORDER[-2:] == ("quant_shadow", "sink")
    assert StageStatusV1("quant_membership", "skipped").stage == "quant_membership"
    assert StageStatusV1("quant_shadow", "skipped").stage == "quant_shadow"
    event = CadenceLifecycleEventV1(
        "stage_finished", "cadence-300s-market-portfolio-v1", 1,
        datetime(2026, 8, 14, tzinfo=timezone.utc),
        stage="quant_shadow", status="skipped", reason="policy_not_pinned",
    )
    assert event.stage == "quant_shadow"


def _quant_executor(tmp_path):
    from tokmo.programs.cadence_wire import CadenceWireExecutorV1
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.cadence_policy import (
        CADENCE_QUANT_SHADOW_POLICY_V1,
        TradesChildLimitsV1,
    )
    from tokmo.values.operation_lane_registry import (
        OPERATION_LANE_REGISTRY_V1,
        OperationLane,
    )
    from tokmo.values.ops_kernel import ProducerRole

    slot = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    store = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")
    return store, CadenceWireExecutorV1(
        ops_kernel=store,
        role=ProducerRole.COLLECTOR,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=slot,
        clock=lambda: slot,
        child_limits=TradesChildLimitsV1(),
        lane_registry=OPERATION_LANE_REGISTRY_V1,
        lane=OperationLane.CADENCE_300S,
    )


def test_membership_eighth_and_duplicate_are_rejected_before_callback(tmp_path):
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.values.ops_kernel import CadenceBudgetExceeded, CadenceDuplicateError

    _store, executor = _quant_executor(tmp_path)
    plan = membership_operation_plan(("A", "B", "C", "D", "E"), date(2026, 8, 14))
    for operation, identity in plan[:7]:
        executor.execute(operation, identity, lambda claim: None)
    callbacks = []
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute(*plan[7], lambda claim: callbacks.append(claim))
    with pytest.raises(CadenceDuplicateError):
        executor.execute(*plan[3], lambda claim: callbacks.append(claim))
    assert callbacks == []


def test_shared_candle_ceiling_rejects_twelfth_before_callback(tmp_path):
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    _store, executor = _quant_executor(tmp_path / "shared")
    callbacks = []
    for symbol in ("A", "B", "C"):
        executor.execute(
            "STOCK_CANDLES_DAILY",
            f"candles:mr:US:{symbol}:2026-08-14:1",
            lambda claim: None,
        )
    for index in range(8):
        symbol = "DEFG"[index // 2]
        executor.execute(
            "STOCK_CANDLES_DAILY",
            f"candles:qt:US:{symbol}:2026-08-14:{index % 2 + 1}",
            lambda claim: None,
        )
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute(
            "STOCK_CANDLES_DAILY",
            "candles:qt:US:H:2026-08-14:1",
            lambda claim: callbacks.append(claim),
        )
    assert callbacks == []
