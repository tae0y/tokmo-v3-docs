"""Injected operations clock and collector scheduling constants."""

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import TypeAlias

from tokmo.values.material_pack import JobKind
from tokmo.values.ops_tick import TickId, tick_id_from_utc


@dataclass(frozen=True)
class TickContext:
    now: datetime
    tick: TickId


def current_tick(clock) -> TickContext:
    now = clock()
    return TickContext(now, tick_id_from_utc(now))


COLLECTOR_OFFSETS = MappingProxyType(
    {
        JobKind.HOLDINGS: 0,
        JobKind.QUOTES: 20,
        JobKind.TRADES: 40,
    }
)
RUN_CEILINGS = MappingProxyType(
    {
        JobKind.HOLDINGS: 8,
        JobKind.QUOTES: 4,
        JobKind.TRADES: 12,
    }
)
ReservationSeal: TypeAlias = Callable[[str, str, str, str], None]
