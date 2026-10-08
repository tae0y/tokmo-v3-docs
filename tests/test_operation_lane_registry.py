"""Operation lane ownership registry contracts (Plan §8, §12.4, §26)."""

import pytest

from tokmo.values.cadence_policy import CADENCE_OPERATIONS
from tokmo.values.operation_lane_registry import (
    G3A_CUTOVER_EPOCH_UTC,
    LANE_GOVERNED_OPERATIONS,
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
    OperationOwnershipError,
    active_owner,
    validate_lane_startup,
)


def _entry(operation, owner=OperationLane.CADENCE_300S, fallback=None,
           cutover=None):
    return OperationLaneV1(operation, owner, fallback, cutover)


def _registry(overrides=()):
    entries = {
        operation: _entry(operation)
        for operation in sorted(LANE_GOVERNED_OPERATIONS)
    }
    for entry in overrides:
        entries[entry.operation] = entry
    return OperationLaneRegistryV1(entries=entries)


CUTOVER_EPOCH = 1786500000 - (1786500000 % 300)  # aligned 300s boundary


def _cutover_registry():
    return _registry(overrides=[
        _entry("PRICE_BATCH", OperationLane.PULSE_60S,
               OperationLane.CADENCE_300S, CUTOVER_EPOCH),
        _entry("ORDERS_PAGE", OperationLane.PULSE_60S,
               OperationLane.CADENCE_300S, CUTOVER_EPOCH),
        _entry("ORDER_DETAIL", OperationLane.PULSE_60S,
               OperationLane.CADENCE_300S, CUTOVER_EPOCH),
    ])


def test_governed_operations_are_every_cadence_operation_except_token():
    # §8.1: TOKEN is lane-locally acquired per tick and never lane-owned.
    assert LANE_GOVERNED_OPERATIONS == frozenset(CADENCE_OPERATIONS) - {"TOKEN"}


def test_production_registry_schedules_the_g3a_provider_cutover():
    assert set(OPERATION_LANE_REGISTRY_V1.entries) == LANE_GOVERNED_OPERATIONS
    governed = {"PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"}
    cutovers = {
        OPERATION_LANE_REGISTRY_V1.entries[operation].cutover_epoch_utc
        for operation in governed
    }
    assert len(cutovers) == 1
    cutover = cutovers.pop()
    assert cutover is not None and cutover % 300 == 0
    for operation, entry in OPERATION_LANE_REGISTRY_V1.entries.items():
        if operation in governed:
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is OperationLane.CADENCE_300S
            assert active_owner(entry, epoch=cutover - 1) is OperationLane.CADENCE_300S
            assert active_owner(entry, epoch=cutover) is OperationLane.PULSE_60S
        elif operation == "CONDITIONAL_ORDERS":
            # born Pulse-owned (conditional SDS §4.4): the 300s lane never
            # ran a conditional scan, so no cutover boundary exists.
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is None
            assert entry.cutover_epoch_utc is None
        else:
            assert entry.recurring_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is None


def test_registry_requires_exactly_the_governed_operation_set():
    entries = {
        operation: _entry(operation)
        for operation in sorted(LANE_GOVERNED_OPERATIONS)
    }
    missing = dict(entries)
    del missing["PRICE_BATCH"]
    with pytest.raises(ValueError):
        OperationLaneRegistryV1(entries=missing)
    # TOKEN is lane-local: it cannot even form a lane entry, so a registry
    # can never grow to cover it.
    with pytest.raises(ValueError):
        _entry("TOKEN")


def test_unknown_operation_and_key_mismatch_are_rejected():
    with pytest.raises(ValueError):
        _entry("NOT_AN_OPERATION")
    entries = {
        operation: _entry(operation)
        for operation in sorted(LANE_GOVERNED_OPERATIONS)
    }
    entries["PRICE_BATCH"] = _entry("ORDERS_PAGE")
    with pytest.raises(ValueError):
        OperationLaneRegistryV1(entries=entries)


def test_cutover_epoch_must_be_300s_aligned_with_a_distinct_fallback():
    with pytest.raises(ValueError):
        _entry("PRICE_BATCH", OperationLane.PULSE_60S,
               OperationLane.CADENCE_300S, CUTOVER_EPOCH + 60)
    with pytest.raises(ValueError):
        _entry("PRICE_BATCH", OperationLane.PULSE_60S, None, CUTOVER_EPOCH)
    with pytest.raises(ValueError):
        _entry("PRICE_BATCH", OperationLane.PULSE_60S,
               OperationLane.PULSE_60S, CUTOVER_EPOCH)


def test_active_owner_flips_exactly_at_the_cutover_boundary():
    entry = _entry("PRICE_BATCH", OperationLane.PULSE_60S,
                   OperationLane.CADENCE_300S, CUTOVER_EPOCH)
    assert active_owner(entry, epoch=CUTOVER_EPOCH - 1) is OperationLane.CADENCE_300S
    assert active_owner(entry, epoch=CUTOVER_EPOCH) is OperationLane.PULSE_60S
    assert active_owner(entry, epoch=CUTOVER_EPOCH + 299) is OperationLane.PULSE_60S


def test_every_operation_has_exactly_one_active_owner_per_epoch():
    registry = _cutover_registry()
    for epoch in (CUTOVER_EPOCH - 300, CUTOVER_EPOCH, CUTOVER_EPOCH + 300):
        owners = {
            operation: active_owner(entry, epoch=epoch)
            for operation, entry in registry.entries.items()
        }
        assert set(owners) == LANE_GOVERNED_OPERATIONS
        for owner in owners.values():
            assert isinstance(owner, OperationLane)


def test_pulse_startup_refuses_provider_stage_it_does_not_own():
    # §8.2/§12.4: pre-cutover, a pulse runner configured to start PRICE_BATCH
    # provider wires must fail closed with provider I/O zero.
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            OPERATION_LANE_REGISTRY_V1, lane=OperationLane.PULSE_60S,
            provider_operations=("PRICE_BATCH",),
            epoch=G3A_CUTOVER_EPOCH_UTC - 1,
        )


def test_cadence_startup_refuses_provider_quotes_after_cutover():
    registry = _cutover_registry()
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            registry, lane=OperationLane.CADENCE_300S,
            provider_operations=("PRICE_BATCH",), epoch=CUTOVER_EPOCH,
        )
    # Before the boundary the same configuration is legitimate.
    validate_lane_startup(
        registry, lane=OperationLane.CADENCE_300S,
        provider_operations=("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"),
        epoch=CUTOVER_EPOCH - 300,
    )


def test_pulse_startup_accepts_only_owned_operations_after_cutover():
    registry = _cutover_registry()
    validate_lane_startup(
        registry, lane=OperationLane.PULSE_60S,
        provider_operations=("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"),
        epoch=CUTOVER_EPOCH,
    )
    # Owner-enum swap mutation: claiming a 300s-owned operation still fails.
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            registry, lane=OperationLane.PULSE_60S,
            provider_operations=("ACCOUNTS",), epoch=CUTOVER_EPOCH,
        )
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            registry, lane=OperationLane.PULSE_60S,
            provider_operations=("STOCK_CANDLES_DAILY",), epoch=CUTOVER_EPOCH,
        )


def test_token_is_outside_lane_governance_for_both_lanes():
    validate_lane_startup(
        OPERATION_LANE_REGISTRY_V1, lane=OperationLane.PULSE_60S,
        provider_operations=("TOKEN",), epoch=0,
    )
    validate_lane_startup(
        OPERATION_LANE_REGISTRY_V1, lane=OperationLane.CADENCE_300S,
        provider_operations=("TOKEN",), epoch=0,
    )


def test_unknown_provider_operation_fails_startup():
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            OPERATION_LANE_REGISTRY_V1, lane=OperationLane.CADENCE_300S,
            provider_operations=("NOT_AN_OPERATION",), epoch=0,
        )


def test_registry_mapping_is_immutable():
    with pytest.raises(TypeError):
        OPERATION_LANE_REGISTRY_V1.entries["PRICE_BATCH"] = _entry("PRICE_BATCH")
