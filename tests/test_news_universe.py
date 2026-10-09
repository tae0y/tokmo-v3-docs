"""Dynamic personalized NEWS universe: resolution, routing, staleness (SDS §4)."""

import json
import os
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.programs.news_poll_manifest import NewsPollManifest, plan_news_group
from tokmo.programs.news_poll_policy import NewsGroup
from tokmo.programs.news_universe import (
    build_naver_query,
    effective_finnhub_symbols,
    load_news_universe,
    persist_news_universe,
    resolve_news_universe,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.news_universe import (
    STALE_UNIVERSE_MAX_HOURS,
    UNIVERSE_MAX_BYTES,
    NewsUniverseEntryV1,
    UniverseProvenance,
    UniverseRouting,
)
from tokmo.values.session_gate import Market

NOW = datetime(2026, 9, 1, 3, 0, tzinfo=UTC)
CAPTURED = datetime(2026, 9, 1, 2, 50, tzinfo=UTC)
PERSONAL = "toss-acct-" + "a" * 24
PENSION = "pension-excel-" + "b" * 24


def _holdings(by_scope, captured=CAPTURED):
    accounts = []
    identities = {}
    for ordinal, (scope, items) in enumerate(by_scope.items(), start=1):
        accounts.append(
            AccountHoldingsV1(
                scope,
                ordinal,
                tuple(
                    HoldingItemV1(symbol, "1", "10", "USD", market_raw, "market")
                    for symbol, market_raw in items
                ),
            )
        )
        identities[scope] = AccountScopeIdentityV1(scope, None, ordinal, None)
    return CurrentHoldingsV1(1, tuple(accounts), True, captured, identities)


WATCHLISTS = {
    Market.KR: (("005930", "삼성전자"),),
    Market.US: (("NVDA", "NVIDIA Corporation"), ("V", "Visa Inc.")),
}


def _resolve(**overrides):
    arguments = dict(
        holdings=_holdings(
            {PERSONAL: [("NVDA", "US"), ("GOOGL", "US"), ("000660", "KR")]}
        ),
        watchlist_identities=WATCHLISTS,
        snapshot_names={"000660": "SK하이닉스"},
        previous=None,
        now=NOW,
    )
    arguments.update(overrides)
    return resolve_news_universe(**arguments)


def _entry(universe, symbol):
    matches = [entry for entry in universe.entries if entry.symbol == symbol]
    assert len(matches) == 1, (symbol, universe.entries)
    return matches[0]


def test_union_dedupe_provenance_and_pension_exclusion():
    """R-UNI-1: held+watched=BOTH; pension-only excluded; personal+pension in."""
    holdings = _holdings(
        {
            PERSONAL: [("NVDA", "US"), ("000660", "KR")],
            PENSION: [("999999", "KR"), ("000660", "KR")],
        }
    )
    universe = _resolve(holdings=holdings)
    assert universe.stale is False
    assert universe.stale_since is None
    assert universe.resolved_at == NOW
    assert _entry(universe, "NVDA").provenance is UniverseProvenance.BOTH
    assert _entry(universe, "V").provenance is UniverseProvenance.WATCHLIST
    assert _entry(universe, "000660").provenance is UniverseProvenance.HOLDING
    assert not any(entry.symbol == "999999" for entry in universe.entries)
    assert universe.holdings_ref == f"current_holdings:{CAPTURED.isoformat()}"
    assert universe.watchlist_ref == "watchlists:1/2"


def test_entries_are_deterministically_ordered():
    first = _resolve()
    second = _resolve()
    assert first.entries == second.entries
    assert list(first.entries) == sorted(
        first.entries, key=lambda entry: (entry.market, entry.symbol)
    )


def test_market_split_unclassifiable_is_surfaced_never_routed():
    """R-UNI-2: classify_market None -> UNROUTABLE_MARKET, in artifact only."""
    holdings = _holdings({PERSONAL: [("NVDA", "US"), ("MYSTERY1", None)]})
    universe = _resolve(holdings=holdings)
    entry = _entry(universe, "MYSTERY1")
    assert entry.routing is UniverseRouting.UNROUTABLE_MARKET
    assert entry.market == "UNKNOWN"
    assert "MYSTERY1" not in effective_finnhub_symbols(universe)
    query = build_naver_query(universe)
    assert query is None or "MYSTERY1" not in query


def test_us_symbol_outside_finnhub_grammar_is_surfaced_not_routed():
    """SDS §4.2-3: US symbol failing [A-Z][A-Z0-9.-]{0,9} never reaches Finnhub."""
    holdings = _holdings({PERSONAL: [("0COIN", "US"), ("NVDA", "US")]})
    universe = _resolve(holdings=holdings)
    entry = _entry(universe, "0COIN")
    assert entry.routing is UniverseRouting.UNROUTABLE_MARKET
    assert entry.market == "US"
    assert "0COIN" not in effective_finnhub_symbols(universe)


def test_kr_name_priority_watchlist_snapshot_previous_then_unroutable():
    """R-UNI-3: watchlist name > snapshot name > previous name > no name."""
    previous = _resolve(
        holdings=_holdings({PERSONAL: [("035420", "KR")]}),
        snapshot_names={"035420": "네이버"},
    )
    assert _entry(previous, "035420").name == "네이버"
    holdings = _holdings(
        {
            PERSONAL: [
                ("005930", "KR"),  # watchlist name wins even against snapshot
                ("000660", "KR"),  # snapshot name
                ("035420", "KR"),  # previous universe entry name
                ("123450", "KR"),  # nowhere -> unroutable, never guessed
            ]
        }
    )
    universe = _resolve(
        holdings=holdings,
        snapshot_names={"000660": "SK하이닉스", "005930": "스냅샷이름"},
        previous=previous,
    )
    assert _entry(universe, "005930").name == "삼성전자"
    assert _entry(universe, "000660").name == "SK하이닉스"
    assert _entry(universe, "035420").name == "네이버"
    unnamed = _entry(universe, "123450")
    assert unnamed.name is None
    assert unnamed.routing is UniverseRouting.UNROUTABLE_NO_NAME
    query = build_naver_query(universe)
    assert query == "삼성전자 OR SK하이닉스 OR 네이버"


def test_us_routes_finnhub_and_kr_never_enters_finnhub_invocations():
    """R-RTE-1: effective symbols are US-only, structurally."""
    universe = _resolve()
    symbols = effective_finnhub_symbols(universe)
    assert set(symbols) == {"NVDA", "GOOGL", "V"}
    assert _entry(universe, "000660").routing is UniverseRouting.NAVER_QUERY
    manifest = NewsPollManifest("news16-v1", False, "unused", 10, symbols)
    plan = plan_news_group(NewsGroup.B, manifest, on_date="2026-09-01")
    company = [
        invocation for invocation in plan if invocation.source == "finnhub-company"
    ]
    assert {invocation.symbol for invocation in company} == set(symbols)
    assert all("000660" != invocation.symbol for invocation in plan)


def test_naver_query_priority_order_and_whole_name_truncation():
    """R-RTE-2: OR-join in D10 order; 200-char cap at whole-name boundaries."""
    long_names = {
        f"{300000 + index:06d}": f"회사{index}" + "명" * 30 for index in range(9)
    }
    holdings = _holdings(
        {PERSONAL: [(symbol, "KR") for symbol in long_names] + [("005930", "KR")]}
    )
    universe = _resolve(
        holdings=holdings,
        snapshot_names=long_names,
        watchlist_identities={Market.KR: (("005930", "삼성전자"),), Market.US: ()},
    )
    query = build_naver_query(universe)
    assert query is not None
    assert len(query) <= 200
    parts = query.split(" OR ")
    # BOTH-provenance 삼성전자 outranks every HOLDING-only long name.
    assert parts[0] == "삼성전자"
    routed = [
        entry
        for entry in universe.entries
        if entry.routing is UniverseRouting.NAVER_QUERY
    ]
    overflowed = [
        entry for entry in universe.entries if entry.routing is UniverseRouting.OVERFLOW
    ]
    assert len(parts) == len(routed)
    assert overflowed, "the 9 long names cannot all fit under 200 chars"
    for entry in routed:
        assert entry.name in parts


def test_twelve_us_symbols_route_ten_by_priority_with_two_overflow():
    """R-CAP-1: D10 priority (BOTH > HOLDING > WATCHLIST, then lexicographic)."""
    held = [(f"H{index}", "US") for index in range(10)] + [("NVDA", "US")]
    universe = _resolve(
        holdings=_holdings({PERSONAL: held}),
        watchlist_identities={
            Market.KR: (),
            Market.US: (("NVDA", "NVIDIA Corporation"), ("V", "Visa Inc.")),
        },
        snapshot_names={},
    )
    symbols = effective_finnhub_symbols(universe)
    assert len(symbols) == 10
    # NVDA is BOTH -> first; then the ten held H0..H9 lexicographically,
    # capped at nine of them; watchlist-only V overflows.
    assert symbols == ("NVDA",) + tuple(f"H{index}" for index in range(9))
    assert _entry(universe, "H9").routing is UniverseRouting.OVERFLOW
    assert _entry(universe, "V").routing is UniverseRouting.OVERFLOW
    for symbol in symbols:
        assert len(symbol) <= 10 and symbol[0].isalpha()


def test_holdings_failure_falls_back_to_previous_union_watchlist():
    """R-STL-1: previous ∪ current watchlist, stale surfaced and persisted."""
    previous = _resolve()
    later = NOW + timedelta(hours=1)
    stale = resolve_news_universe(
        holdings=None,
        watchlist_identities=WATCHLISTS,
        snapshot_names={"000660": "SK하이닉스"},
        previous=previous,
        now=later,
    )
    assert stale.stale is True
    assert stale.stale_since == later
    assert stale.resolved_at == previous.resolved_at
    assert stale.holdings_ref == f"stale:{previous.resolved_at.isoformat()}"
    assert _entry(stale, "GOOGL").provenance is UniverseProvenance.HOLDING
    assert _entry(stale, "NVDA").provenance is UniverseProvenance.BOTH
    assert set(effective_finnhub_symbols(stale)) == {"NVDA", "GOOGL", "V"}
    # stale_since survives a second failing resolution unchanged.
    again = resolve_news_universe(
        holdings=None,
        watchlist_identities=WATCHLISTS,
        snapshot_names={},
        previous=stale,
        now=later + timedelta(hours=1),
    )
    assert again.stale_since == later
    # a fresh success clears staleness.
    recovered = resolve_news_universe(
        holdings=_holdings({PERSONAL: [("NVDA", "US")]}),
        watchlist_identities=WATCHLISTS,
        snapshot_names={},
        previous=again,
        now=later + timedelta(hours=2),
    )
    assert recovered.stale is False
    assert recovered.stale_since is None


def test_watchlist_failure_keeps_previous_watchlist_entries():
    """SDS §4.5-2: WatchlistError symmetrically reuses previous intent."""
    previous = _resolve()
    stale = resolve_news_universe(
        holdings=_holdings({PERSONAL: [("GOOGL", "US")]}),
        watchlist_identities=None,
        snapshot_names={},
        previous=previous,
        now=NOW + timedelta(hours=1),
    )
    assert stale.stale is True
    assert stale.watchlist_ref == "stale"
    assert _entry(stale, "V").provenance is UniverseProvenance.WATCHLIST
    assert _entry(stale, "GOOGL").provenance is UniverseProvenance.HOLDING


def test_72h_expiry_drops_holding_only_routing_and_no_previous_falls_back():
    """R-STL-2: expired HOLDING-only entries leave routing; WATCHLIST/BOTH stay;
    no-previous + fresh-failure resolves to None (static manifest fallback)."""
    previous = _resolve()
    expired_at = NOW + timedelta(hours=STALE_UNIVERSE_MAX_HOURS + 1)
    stale = resolve_news_universe(
        holdings=None,
        watchlist_identities=WATCHLISTS,
        snapshot_names={"000660": "SK하이닉스"},
        previous=previous,
        now=expired_at,
    )
    assert stale.stale is True
    googl = _entry(stale, "GOOGL")
    assert googl.routing not in (
        UniverseRouting.FINNHUB_COMPANY,
        UniverseRouting.NAVER_QUERY,
    )
    assert "GOOGL" not in effective_finnhub_symbols(stale)
    assert set(effective_finnhub_symbols(stale)) == {"NVDA", "V"}
    kr_query = build_naver_query(stale)
    assert kr_query == "삼성전자"
    assert (
        resolve_news_universe(
            holdings=None,
            watchlist_identities=WATCHLISTS,
            snapshot_names={},
            previous=None,
            now=NOW,
        )
        is None
    )


def test_persist_load_roundtrip_embeds_previous_and_is_private(tmp_path):
    """SDS §4.2-5/D13: atomic 0600 artifact ≤64KiB with embedded previous."""
    first = _resolve()
    second = resolve_news_universe(
        holdings=None,
        watchlist_identities=WATCHLISTS,
        snapshot_names={},
        previous=first,
        now=NOW + timedelta(hours=1),
    )
    path = tmp_path / "news" / "news_universe.json"
    persist_news_universe(path, second, previous=first)
    assert os.stat(path).st_mode & 0o777 == 0o600
    assert path.stat().st_size <= UNIVERSE_MAX_BYTES
    payload = json.loads(path.read_bytes())
    assert payload["latest"]["overflow_present"] is False
    loaded = load_news_universe(path)
    assert loaded is not None
    assert loaded.latest == second
    assert loaded.previous == first


def test_corrupt_or_oversized_universe_artifact_loads_as_absent(tmp_path):
    """R-STL-3: unparseable or oversized artifact is treated as absent."""
    path = tmp_path / "news_universe.json"
    assert load_news_universe(path) is None
    path.write_bytes(b"not-json")
    assert load_news_universe(path) is None
    path.write_text(json.dumps({"schema_version": 1, "latest": None, "previous": None}))
    assert load_news_universe(path) is None
    path.write_bytes(b"x" * (UNIVERSE_MAX_BYTES + 1))
    assert load_news_universe(path) is None


def test_entry_vocabulary_is_closed():
    with pytest.raises(ValueError):
        NewsUniverseEntryV1(
            "JP", "7203", None, UniverseProvenance.HOLDING, UniverseRouting.OVERFLOW
        )
    with pytest.raises(ValueError):
        NewsUniverseEntryV1(
            "UNKNOWN",
            "X",
            None,
            UniverseProvenance.HOLDING,
            UniverseRouting.FINNHUB_COMPANY,
        )
    with pytest.raises(ValueError):
        NewsUniverseEntryV1(
            "US", "", None, UniverseProvenance.HOLDING, UniverseRouting.OVERFLOW
        )
