"""Dynamic personalized NEWS universe resolution and routing (SDS §4).

Pure resolution over injected inputs: the ambient edge (tools) supplies the
holdings projection, watchlist identities, snapshot display names, and the
previously persisted universe. Persistence is a single bounded atomic JSON
artifact; this module never opens SQLite or any transport.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path

from tokmo.programs.quotes_sync import pension_only_symbols
from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.news_universe import (
    FINNHUB_SYMBOL_CAP,
    FINNHUB_SYMBOL_RE,
    KR_SYMBOL_RE,
    NAVER_QUERY_MAX_CHARS,
    NewsUniverseEntryV1,
    NewsUniverseV1,
    PersistedUniverseV1,
    STALE_UNIVERSE_MAX_HOURS,
    UNIVERSE_MAX_BYTES,
    UNIVERSE_SCHEMA_VERSION,
    UNKNOWN_MARKET,
    UniverseProvenance,
    UniverseRouting,
    US_SYMBOL_RE,
)
from tokmo.values.session_gate import Market

_HELD_PROVENANCES = (UniverseProvenance.HOLDING, UniverseProvenance.BOTH)
_WATCHED_PROVENANCES = (UniverseProvenance.WATCHLIST, UniverseProvenance.BOTH)
# D10 overflow priority: BOTH > HOLDING > WATCHLIST, then lexicographic.
_PRIORITY_RANK = {
    UniverseProvenance.BOTH: 0,
    UniverseProvenance.HOLDING: 1,
    UniverseProvenance.WATCHLIST: 2,
}


@dataclass(frozen=True)
class _Candidate:
    market: str
    symbol: str
    name: str | None
    provenance: UniverseProvenance


def _priority(candidate) -> tuple[int, str]:
    return (_PRIORITY_RANK[candidate.provenance], candidate.symbol)


def _held_keys(holdings: CurrentHoldingsV1) -> set[tuple[str, str]]:
    excluded = pension_only_symbols(holdings)
    held: set[tuple[str, str]] = set()
    for account in holdings.accounts:
        for item in account.holdings:
            if item.symbol in excluded:
                continue
            market = classify_market(item.market_raw, item.market_field)
            key = ((market.value, item.symbol) if market is not None
                   else (UNKNOWN_MARKET, item.symbol))
            held.add(key)
    classified = {symbol for market, symbol in held if market != UNKNOWN_MARKET}
    return {key for key in held
            if key[0] != UNKNOWN_MARKET or key[1] not in classified}


def _route(candidates, *, expired: bool) -> tuple[NewsUniverseEntryV1, ...]:
    routings: dict[tuple[str, str], UniverseRouting] = {}
    us_pool: list[_Candidate] = []
    kr_pool: list[_Candidate] = []
    for candidate in candidates:
        key = (candidate.market, candidate.symbol)
        if candidate.market == UNKNOWN_MARKET:
            routings[key] = UniverseRouting.UNROUTABLE_MARKET
        elif candidate.market == "US":
            if (US_SYMBOL_RE.fullmatch(candidate.symbol) is None
                    or FINNHUB_SYMBOL_RE.fullmatch(candidate.symbol) is None):
                routings[key] = UniverseRouting.UNROUTABLE_MARKET
            elif expired and candidate.provenance is UniverseProvenance.HOLDING:
                routings[key] = UniverseRouting.OVERFLOW
            else:
                us_pool.append(candidate)
        else:
            if KR_SYMBOL_RE.fullmatch(candidate.symbol) is None:
                routings[key] = UniverseRouting.UNROUTABLE_MARKET
            elif candidate.name is None:
                routings[key] = UniverseRouting.UNROUTABLE_NO_NAME
            elif expired and candidate.provenance is UniverseProvenance.HOLDING:
                routings[key] = UniverseRouting.OVERFLOW
            else:
                kr_pool.append(candidate)
    for index, candidate in enumerate(sorted(us_pool, key=_priority)):
        routings[(candidate.market, candidate.symbol)] = (
            UniverseRouting.FINNHUB_COMPANY if index < FINNHUB_SYMBOL_CAP
            else UniverseRouting.OVERFLOW)
    query_length = 0
    fits = True
    for candidate in sorted(kr_pool, key=_priority):
        projected = (len(candidate.name) if query_length == 0
                     else query_length + 4 + len(candidate.name))
        if fits and projected <= NAVER_QUERY_MAX_CHARS:
            query_length = projected
            routing = UniverseRouting.NAVER_QUERY
        else:
            fits = False
            routing = UniverseRouting.OVERFLOW
        routings[(candidate.market, candidate.symbol)] = routing
    entries = tuple(
        NewsUniverseEntryV1(candidate.market, candidate.symbol, candidate.name,
                            candidate.provenance,
                            routings[(candidate.market, candidate.symbol)])
        for candidate in sorted(candidates,
                                key=lambda item: (item.market, item.symbol)))
    return entries


def resolve_news_universe(*, holdings, watchlist_identities, snapshot_names,
                          previous, now) -> NewsUniverseV1 | None:
    """Resolve the personal universe for one tick (SDS §4.2/§4.5).

    ``holdings``/``watchlist_identities`` are ``None`` on read failure; with
    no holdings and no previous universe the caller must fall back to the
    static manifest, signalled by ``None`` (never an empty universe).
    """
    if holdings is None and previous is None:
        return None
    previous_names: dict[tuple[str, str], str] = {}
    if previous is not None:
        previous_names = {
            (entry.market, entry.symbol): entry.name
            for entry in previous.entries if entry.name is not None}

    if watchlist_identities is not None:
        watch = {
            (market.value, symbol): name
            for market, pairs in watchlist_identities.items()
            for symbol, name in pairs}
        watchlist_ref = "watchlists:{}/{}".format(
            len(watchlist_identities.get(Market.KR, ())),
            len(watchlist_identities.get(Market.US, ())))
    elif previous is not None:
        watch = {
            (entry.market, entry.symbol): entry.name
            for entry in previous.entries
            if entry.provenance in _WATCHED_PROVENANCES}
        watchlist_ref = "stale"
    else:
        watch = {}
        watchlist_ref = "stale"

    expired = False
    if holdings is not None:
        held = _held_keys(holdings)
        stale = watchlist_identities is None
        resolved_at = now
        holdings_ref = f"current_holdings:{holdings.captured_at.isoformat()}"
    else:
        held = {(entry.market, entry.symbol) for entry in previous.entries
                if entry.provenance in _HELD_PROVENANCES}
        stale = True
        resolved_at = previous.resolved_at
        holdings_ref = f"stale:{previous.resolved_at.isoformat()}"
        expired = now - previous.resolved_at > timedelta(
            hours=STALE_UNIVERSE_MAX_HOURS)

    stale_since = None
    if stale:
        carried = (previous is not None and previous.stale
                   and previous.stale_since is not None)
        stale_since = previous.stale_since if carried else now

    candidates = []
    for market, symbol in sorted(held | set(watch)):
        key = (market, symbol)
        if key in held and key in watch:
            provenance = UniverseProvenance.BOTH
        elif key in held:
            provenance = UniverseProvenance.HOLDING
        else:
            provenance = UniverseProvenance.WATCHLIST
        name = watch.get(key)
        if name is None and market == "KR":
            name = snapshot_names.get(symbol)
        if name is None:
            name = previous_names.get(key)
        candidates.append(_Candidate(market, symbol, name, provenance))

    return NewsUniverseV1(
        UNIVERSE_SCHEMA_VERSION, resolved_at, stale, stale_since,
        _route(candidates, expired=expired), holdings_ref, watchlist_ref)


def effective_finnhub_symbols(universe: NewsUniverseV1) -> tuple[str, ...]:
    routed = [entry for entry in universe.entries
              if entry.routing is UniverseRouting.FINNHUB_COMPANY]
    return tuple(entry.symbol for entry in sorted(routed, key=_priority))


def build_naver_query(universe: NewsUniverseV1) -> str | None:
    routed = [entry for entry in universe.entries
              if entry.routing is UniverseRouting.NAVER_QUERY]
    if not routed:
        return None
    return " OR ".join(entry.name for entry in sorted(routed, key=_priority))


_ARTIFACT_KEYS = frozenset({"schema_version", "latest", "previous"})
_UNIVERSE_KEYS = frozenset({
    "schema_version", "resolved_at", "stale", "stale_since", "entries",
    "holdings_ref", "watchlist_ref", "overflow_present"})
_ENTRY_KEYS = frozenset({"market", "symbol", "name", "provenance", "routing"})


def _universe_dict(universe: NewsUniverseV1) -> dict:
    return {
        "schema_version": universe.schema_version,
        "resolved_at": universe.resolved_at.isoformat(),
        "stale": universe.stale,
        "stale_since": (universe.stale_since.isoformat()
                        if universe.stale_since is not None else None),
        "entries": [{
            "market": entry.market, "symbol": entry.symbol,
            "name": entry.name, "provenance": entry.provenance.value,
            "routing": entry.routing.value,
        } for entry in universe.entries],
        "holdings_ref": universe.holdings_ref,
        "watchlist_ref": universe.watchlist_ref,
        "overflow_present": any(
            entry.routing is UniverseRouting.OVERFLOW
            for entry in universe.entries),
    }


def _aware_from(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("universe timestamps must be aware")
    return parsed


def _universe_from_dict(value) -> NewsUniverseV1:
    if type(value) is not dict or set(value) != _UNIVERSE_KEYS:
        raise ValueError("invalid universe artifact")
    entries = []
    if type(value["entries"]) is not list:
        raise ValueError("invalid universe entries")
    for row in value["entries"]:
        if type(row) is not dict or set(row) != _ENTRY_KEYS:
            raise ValueError("invalid universe entry")
        entries.append(NewsUniverseEntryV1(
            row["market"], row["symbol"], row["name"],
            UniverseProvenance(row["provenance"]),
            UniverseRouting(row["routing"])))
    return NewsUniverseV1(
        value["schema_version"], _aware_from(value["resolved_at"]),
        value["stale"],
        (_aware_from(value["stale_since"])
         if value["stale_since"] is not None else None),
        tuple(entries), value["holdings_ref"], value["watchlist_ref"])


def persist_news_universe(path: Path, latest: NewsUniverseV1, *,
                          previous: NewsUniverseV1 | None) -> None:
    if not isinstance(latest, NewsUniverseV1):
        raise ValueError("latest must be NewsUniverseV1")
    if previous is not None and not isinstance(previous, NewsUniverseV1):
        raise ValueError("previous must be NewsUniverseV1")
    payload = json.dumps({
        "schema_version": UNIVERSE_SCHEMA_VERSION,
        "latest": _universe_dict(latest),
        "previous": (_universe_dict(previous)
                     if previous is not None else None),
    }, ensure_ascii=False, sort_keys=True,
        separators=(",", ":")).encode("utf-8")
    write_atomic_json(Path(path), payload, max_bytes=UNIVERSE_MAX_BYTES)


def load_news_universe(path: Path) -> PersistedUniverseV1 | None:
    """Read the persisted universe; any corruption or bound violation is
    treated as absent (fail open, SDS §4.5)."""
    try:
        raw = read_atomic_json(Path(path), max_bytes=UNIVERSE_MAX_BYTES)
        value = json.loads(raw.decode("utf-8"))
        if type(value) is not dict or set(value) != _ARTIFACT_KEYS:
            raise ValueError("invalid universe artifact")
        if value["schema_version"] != UNIVERSE_SCHEMA_VERSION:
            raise ValueError("unsupported universe artifact")
        previous = (_universe_from_dict(value["previous"])
                    if value["previous"] is not None else None)
        return PersistedUniverseV1(_universe_from_dict(value["latest"]),
                                   previous)
    except (OSError, TypeError, KeyError, ValueError):
        return None
