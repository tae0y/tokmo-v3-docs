"""Fundamental materialization program (G1A).

Assembles official fundamental materials from SEC companyfacts parse
results. This is the orchestration layer between the raw parser and the
material store. It:

1. Calls parse_companyfacts_response to get raw materials.
2. Returns a MaterializationResult carrying materials or unavailable reason.
3. Preserves raw body SHA-256 provenance in each material.

No external API calls — this is pure data assembly from a provided body.

This module also owns the effect-zero immutable deps builder
(``build_fundamental_materialization_deps``).  composition.py exposes a
thin delegating wrapper with a function-local import so that importing
composition.py itself triggers no construction or provider side effects.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from types import MappingProxyType

from tokmo.facts.sec_fundamentals import parse_companyfacts_response
from tokmo.values.fundamental_materials import (
    OfficialFundamentalMaterialV1,
    StatementScope,
)

from tokmo.values.fundamental_materials import AvailableAtResolution
AvailableAtResolver = Callable[[str], AvailableAtResolution]


@dataclass(frozen=True)
class MaterializationResult:
    """Result of fundamental materialization."""

    materials: tuple[OfficialFundamentalMaterialV1, ...] | None
    unavailable_reason: str | None


def materialize_sec_fundamentals(
    body: bytes,
    *,
    cik: str,
    canonical_symbol: str,
    observed_at: datetime,
    available_at_resolver: AvailableAtResolver,
    market: str,
    statement_scope: StatementScope,
    as_of: datetime,
) -> MaterializationResult:
    """Materialize SEC companyfacts into official fundamental materials.

    Parses the companyfacts body, resolves available-at via the injected
    policy seam, and returns materials sorted by fiscal year.

    ``market`` and ``statement_scope`` must be verified by the caller from
    official inputs (e.g. SEC submissions/exchange listing); this function
    does not guess them.

    ``as_of`` is the as-of datetime; the parser enforces
    ``available_at <= as_of`` for every material produced.
    """
    parse_result = parse_companyfacts_response(
        body,
        cik=cik,
        canonical_symbol=canonical_symbol,
        observed_at=observed_at,
        available_at_resolver=available_at_resolver,
        market=market,
        statement_scope=statement_scope,
        as_of=as_of,
    )

    if parse_result.materials is None:
        return MaterializationResult(
            materials=None,
            unavailable_reason=parse_result.unavailable_reason,
        )

    materials = tuple(sorted(parse_result.materials, key=lambda m: m.fiscal_year))

    return MaterializationResult(
        materials=materials,
        unavailable_reason=None,
    )


# ---------------------------------------------------------------------------
# G1A: Effect-zero immutable deps builder.
#
# SDS §12 G1A item 8 authorizes minimal wiring to import the G1A live
# transports.  This builder constructs SECCompanyFactsLiveTransport and
# OpenDartFundamentalsLiveTransport but does NOT start providers, make
# network calls, activate cadence-time I/O, create directories, or create
# SQLite files.  Import-time AND construction-time effects are all zero.
#
# The returned mapping is immutable (MappingProxyType) and carries only the
# store PATH/FACTORY (not a store instance).  The private directory and
# SQLite file are created only inside an explicit materialization
# invocation, never during construction.
#
# composition.py exposes a thin wrapper that delegates here via a
# function-local import so that importing composition itself has zero
# side effects beyond the baseline composition-root import chain.
# ---------------------------------------------------------------------------

Clock = Callable[[], datetime]


def build_fundamental_materialization_deps(
    *,
    home: Path,
    clock: Clock,
    sec_transport_factory: Callable,
    opendart_transport_factory: Callable,
) -> MappingProxyType:
    """Construct G1A fundamental transports without side effects.

    B3 fix: this leaf builder imports NO live transport modules.
    Transport and store factories are *injected* by the composition wrapper,
    which function-locally imports/constructs them. This keeps the live
    transport import boundary confined to composition.py only.

    Returns an immutable mapping of transport instances (from injected
    factories), a store PATH and a store FACTORY (not a store instance),
    and the injected clock.

    Construction-time guarantees (SDS §12 15d):
    - network starts: 0
    - provider starts: 0
    - cadence registration: 0
    - mkdir: 0 (private dir is NOT created here)
    - SQLite/file-write: 0 (store is NOT instantiated here)
    """
    store_path = (
        (home / ".local" / "share" / "tokmo-v3").resolve() / "fundamentals"
        / "fundamental_materials.db"
    )

    def _store_factory():
        from tokmo.records.fundamental_material_store import (
            FundamentalMaterialStore,
        )
        store_path.parent.mkdir(parents=True, exist_ok=True)
        return FundamentalMaterialStore(store_path)

    return MappingProxyType({
        "sec_transport": sec_transport_factory(),
        "opendart_transport": opendart_transport_factory(),
        "store_path": store_path,
        "store_factory": _store_factory,
        "clock": clock,
    })
