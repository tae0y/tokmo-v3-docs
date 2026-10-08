"""Frozen output values for the receipt-to-pack analysis barrier."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Mapping

from tokmo.values.material_pack import JobKind
from tokmo.values.session_gate import Market


class MarketReadiness(str, Enum):
    READY = "READY"
    SKIP_CLOSED = "SKIP_CLOSED"
    SKIP_HOLDINGS_MISSING = "SKIP_HOLDINGS_MISSING"
    SKIP_HOLDINGS_STALE = "SKIP_HOLDINGS_STALE"
    UNAVAILABLE = "UNAVAILABLE"


@dataclass(frozen=True)
class VerifiedMaterialV1:
    job: JobKind
    pack_tick_id: int
    captured_at: datetime
    file_sha256: str
    content_hash: str
    payload: Mapping[str, object]
    market_partition: tuple[str, ...] = ()

    def __post_init__(self):
        object.__setattr__(self, "payload", _freeze(self.payload))


@dataclass(frozen=True)
class AnalysisBarrierResultV1:
    tick_id: int
    materials: Mapping[JobKind, VerifiedMaterialV1]
    market_readiness: Mapping[Market, MarketReadiness]
    stale_jobs: Mapping[Market, frozenset[JobKind]]
    material_ticks: Mapping[JobKind, int | None]
    incidents: tuple[str, ...]
    proceed_markets: frozenset[Market]
    # Stage 2 (SDS §6): the exact OpenMarketMaskV1 digest this barrier was
    # assembled under; None only for legacy (pre-mask) assemblies.
    mask_digest: str | None = None

    def __post_init__(self):
        object.__setattr__(self, "materials", MappingProxyType(dict(self.materials)))
        object.__setattr__(
            self, "market_readiness", MappingProxyType(dict(self.market_readiness))
        )
        object.__setattr__(self, "stale_jobs", MappingProxyType(dict(self.stale_jobs)))
        object.__setattr__(self, "material_ticks", MappingProxyType(dict(self.material_ticks)))


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value
