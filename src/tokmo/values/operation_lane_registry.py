"""Closed provider-operation lane ownership registry (60s Pulse Plan §8).

Exactly one recurring lane owns each governed provider operation at any
epoch. The registry is a reviewed, versioned code constant — never a runtime
toggle: cutover happens only through a constant change plus re-review, with
the boundary aligned to the 300s grid so no five-minute window ever has two
owners of the same operation. TOKEN stays outside governance: each lane
acquires it lazily per tick and never persists a shared token.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

from tokmo.values.cadence_policy import CADENCE_OPERATIONS

# Every governed operation: the closed cadence enum minus lane-local TOKEN.
LANE_GOVERNED_OPERATIONS = frozenset(CADENCE_OPERATIONS) - {"TOKEN"}

_CUTOVER_GRID_SECONDS = 300


class OperationLane(str, Enum):
    PULSE_60S = "PULSE_60S"
    CADENCE_300S = "CADENCE_300S"


class OperationOwnershipError(ValueError):
    """A lane tried to run a provider operation it does not own."""


@dataclass(frozen=True)
class OperationLaneV1:
    operation: str
    recurring_owner: OperationLane
    fallback_owner: "OperationLane | None"
    cutover_epoch_utc: "int | None"

    def __post_init__(self):
        if self.operation not in LANE_GOVERNED_OPERATIONS:
            raise ValueError("operation is not lane-governed")
        if not isinstance(self.recurring_owner, OperationLane):
            raise ValueError("invalid recurring owner")
        if self.fallback_owner is not None and not isinstance(
            self.fallback_owner, OperationLane
        ):
            raise ValueError("invalid fallback owner")
        if self.cutover_epoch_utc is not None:
            if (
                type(self.cutover_epoch_utc) is not int
                or self.cutover_epoch_utc < 0
                or self.cutover_epoch_utc % _CUTOVER_GRID_SECONDS != 0
            ):
                raise ValueError("cutover epoch must align to the 300s grid")
            if self.fallback_owner is None:
                raise ValueError("cutover requires an explicit fallback owner")
            if self.fallback_owner is self.recurring_owner:
                raise ValueError("cutover fallback must differ from the owner")


@dataclass(frozen=True)
class OperationLaneRegistryV1:
    entries: Mapping[str, OperationLaneV1]

    def __post_init__(self):
        if not isinstance(self.entries, (dict, MappingProxyType)):
            raise ValueError("invalid lane registry entries")
        if set(self.entries) != LANE_GOVERNED_OPERATIONS:
            raise ValueError("registry must cover exactly the governed operations")
        for operation, entry in self.entries.items():
            if not isinstance(entry, OperationLaneV1) or entry.operation != operation:
                raise ValueError("registry key must match its entry operation")
        object.__setattr__(self, "entries", MappingProxyType(dict(self.entries)))


def active_owner(entry: OperationLaneV1, *, epoch: int) -> OperationLane:
    """The single lane that owns this operation at the given UTC epoch."""
    if not isinstance(entry, OperationLaneV1):
        raise ValueError("invalid lane entry")
    if type(epoch) is not int:
        raise ValueError("epoch must be an int")
    if entry.cutover_epoch_utc is None or epoch >= entry.cutover_epoch_utc:
        return entry.recurring_owner
    return entry.fallback_owner


def validate_lane_startup(
    registry: OperationLaneRegistryV1,
    *,
    lane: OperationLane,
    provider_operations,
    epoch: int,
) -> None:
    """Startup fail-closed ownership check (§8.2/§12.4).

    A runner presents the provider operations its configuration would start
    wires for. Any operation the lane does not own at this epoch — or any
    unknown operation — raises before provider I/O. TOKEN is lane-local and
    always permitted.
    """
    if not isinstance(registry, OperationLaneRegistryV1):
        raise OperationOwnershipError("invalid lane registry")
    if not isinstance(lane, OperationLane):
        raise OperationOwnershipError("invalid lane")
    for operation in provider_operations:
        if operation == "TOKEN":
            continue
        entry = registry.entries.get(operation)
        if entry is None:
            raise OperationOwnershipError(f"operation has no lane owner: {operation}")
        if active_owner(entry, epoch=epoch) is not lane:
            raise OperationOwnershipError(
                f"lane is not the active owner of {operation}"
            )


# User-approved G3A operational cutover after token serialization and cadence
# execution-consumer material-pack validation were repaired.
G3A_CUTOVER_EPOCH_UTC = 1786443300  # 2026-08-11T10:15:00Z
_PULSE_PROVIDER_OPERATIONS = frozenset(
    {
        "PRICE_BATCH",
        "ORDERS_PAGE",
        "ORDER_DETAIL",
    }
)
# Conditional-order SDS v0.4 §4.4: born Pulse-owned — the 300s lane never
# ran a conditional scan, so there is no cutover boundary and no fallback.
_PULSE_ONLY_OPERATIONS = frozenset({"CONDITIONAL_ORDERS"})


def _production_entry(operation: str) -> OperationLaneV1:
    if operation in _PULSE_ONLY_OPERATIONS:
        return OperationLaneV1(operation, OperationLane.PULSE_60S, None, None)
    return OperationLaneV1(
        operation,
        (
            OperationLane.PULSE_60S
            if operation in _PULSE_PROVIDER_OPERATIONS
            else OperationLane.CADENCE_300S
        ),
        (
            OperationLane.CADENCE_300S
            if operation in _PULSE_PROVIDER_OPERATIONS
            else None
        ),
        (G3A_CUTOVER_EPOCH_UTC if operation in _PULSE_PROVIDER_OPERATIONS else None),
    )


OPERATION_LANE_REGISTRY_V1 = OperationLaneRegistryV1(
    entries={
        operation: _production_entry(operation)
        for operation in sorted(LANE_GOVERNED_OPERATIONS)
    }
)
