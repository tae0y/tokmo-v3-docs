"""Pure dynamic target resolution for future KR OpenDART collection.

This module does not perform provider I/O or persistence. The fixed issuer set
in the EP-1 source-contract tracer is verification evidence, not an operating
universe.
"""

from __future__ import annotations

from dataclasses import dataclass

from tokmo.facts.opendart import OpenDartCorpMapping
from tokmo.values.current_holdings import classify_market
from tokmo.values.holdings import CompleteHoldingsSnapshot
from tokmo.values.session_gate import Market

HOLDING = "HOLDING"
WATCHLIST = "WATCHLIST"
COMBINED = "HOLDING_AND_WATCHLIST"


class OpenDartTargetError(ValueError):
    """Fail-closed dynamic-universe contract violation."""


@dataclass(frozen=True)
class OpenDartTarget:
    symbol: str
    corp_code: str
    provenance: str


@dataclass(frozen=True)
class OpenDartTargetResolution:
    targets: tuple[OpenDartTarget, ...]
    unmapped: tuple[str, ...]


def _kr_holdings(snapshot: CompleteHoldingsSnapshot) -> frozenset[str]:
    if not isinstance(snapshot, CompleteHoldingsSnapshot):
        raise OpenDartTargetError("complete_holdings_required")
    symbols: set[str] = set()
    for account in snapshot.accounts:
        for item in account.holdings:
            if classify_market(item.market_raw, item.market_field) is Market.KR:
                symbols.add(item.symbol)
    return frozenset(symbols)


@dataclass(frozen=True)
class KrRelevanceResolution:
    """Sorted (symbol, provenance) membership of HOLDING ∪ active KR watchlist."""

    members: tuple[tuple[str, str], ...]

    def provenance_for(self, symbol: str) -> str | None:
        for member_symbol, provenance in self.members:
            if member_symbol == symbol:
                return provenance
        return None


def resolve_kr_relevance(
    *,
    snapshots: tuple[CompleteHoldingsSnapshot, ...],
    watchlist_symbols: frozenset[str],
) -> KrRelevanceResolution:
    """Union the KR holdings of every injected complete snapshot with the
    explicit watchlist, keeping per-symbol provenance. Offline-pure."""
    if not isinstance(watchlist_symbols, frozenset) or not all(
        isinstance(symbol, str) and symbol for symbol in watchlist_symbols
    ):
        raise OpenDartTargetError("invalid_watchlist")
    held: set[str] = set()
    for snapshot in snapshots:
        held |= _kr_holdings(snapshot)
    members = []
    for symbol in sorted(held | watchlist_symbols):
        if symbol in held and symbol in watchlist_symbols:
            provenance = COMBINED
        elif symbol in held:
            provenance = HOLDING
        else:
            provenance = WATCHLIST
        members.append((symbol, provenance))
    return KrRelevanceResolution(tuple(members))


def resolve_kr_opendart_targets(
    *,
    snapshot: CompleteHoldingsSnapshot,
    watchlist_symbols: frozenset[str],
    mappings: tuple[OpenDartCorpMapping, ...],
) -> OpenDartTargetResolution:
    """Resolve holdings plus explicit watch intent against official mappings."""
    if not isinstance(watchlist_symbols, frozenset) or not all(
        isinstance(symbol, str) and symbol for symbol in watchlist_symbols
    ):
        raise OpenDartTargetError("invalid_watchlist")
    if not isinstance(mappings, tuple) or not all(
        isinstance(item, OpenDartCorpMapping) for item in mappings
    ):
        raise OpenDartTargetError("invalid_mappings")

    by_symbol: dict[str, OpenDartCorpMapping] = {}
    for item in mappings:
        if item.stock_code in by_symbol:
            raise OpenDartTargetError("duplicate_mapping")
        by_symbol[item.stock_code] = item

    held = _kr_holdings(snapshot)
    universe = held | watchlist_symbols
    targets: list[OpenDartTarget] = []
    unmapped: list[str] = []
    for symbol in sorted(universe):
        mapped = by_symbol.get(symbol)
        if mapped is None:
            unmapped.append(symbol)
            continue
        if symbol in held and symbol in watchlist_symbols:
            provenance = COMBINED
        elif symbol in held:
            provenance = HOLDING
        else:
            provenance = WATCHLIST
        targets.append(
            OpenDartTarget(
                symbol=symbol,
                corp_code=mapped.corp_code,
                provenance=provenance,
            )
        )
    return OpenDartTargetResolution(tuple(targets), tuple(unmapped))
