"""Closed vocabularies and typed snapshots for the personalized NEWS universe.

The universe is the canonical current-holdings ∪ watchlist selection that
drives per-entity NEWS routing (SDS §4.1). Names are canonical display names
and are never guessed; a KR entry without a resolvable Korean name stays in
the universe but out of the Naver query (D12).
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
import re

UNIVERSE_SCHEMA_VERSION = 1
UNIVERSE_MAX_BYTES = 64 * 1024
STALE_UNIVERSE_MAX_HOURS = 72
FINNHUB_SYMBOL_CAP = 10
NAVER_QUERY_MAX_CHARS = 200

# Watchlist symbol grammars (mirrors watchlists.py; re-declared so this
# values leaf stays import-light) and the manifest/Finnhub routing grammar
# (news_poll_manifest.py:61).
KR_SYMBOL_RE = re.compile(r"^[0-9A-Z]{6}$")
US_SYMBOL_RE = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,31}$")
FINNHUB_SYMBOL_RE = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")

# Market vocabulary: the two routable markets plus the surfaced-only bucket
# for holdings whose market metadata classify_market cannot recognize.
UNKNOWN_MARKET = "UNKNOWN"
_MARKETS = frozenset({"KR", "US", UNKNOWN_MARKET})


class UniverseProvenance(str, Enum):
    HOLDING = "HOLDING"
    WATCHLIST = "WATCHLIST"
    BOTH = "BOTH"


class UniverseRouting(str, Enum):
    FINNHUB_COMPANY = "FINNHUB_COMPANY"
    NAVER_QUERY = "NAVER_QUERY"
    UNROUTABLE_NO_NAME = "UNROUTABLE_NO_NAME"
    UNROUTABLE_MARKET = "UNROUTABLE_MARKET"
    OVERFLOW = "OVERFLOW"


@dataclass(frozen=True)
class NewsUniverseEntryV1:
    market: str
    symbol: str
    name: str | None
    provenance: UniverseProvenance
    routing: UniverseRouting

    def __post_init__(self) -> None:
        if self.market not in _MARKETS:
            raise ValueError("invalid universe market")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("invalid universe symbol")
        if self.name is not None and (
                type(self.name) is not str or not self.name.strip()):
            raise ValueError("invalid universe name")
        if not isinstance(self.provenance, UniverseProvenance):
            raise ValueError("invalid universe provenance")
        if not isinstance(self.routing, UniverseRouting):
            raise ValueError("invalid universe routing")
        if (self.market == UNKNOWN_MARKET
                and self.routing is not UniverseRouting.UNROUTABLE_MARKET):
            raise ValueError("unknown market must be unroutable")


def _aware(value: datetime, label: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{label} must be aware")


@dataclass(frozen=True)
class NewsUniverseV1:
    schema_version: int
    resolved_at: datetime
    stale: bool
    stale_since: datetime | None
    entries: tuple[NewsUniverseEntryV1, ...]
    holdings_ref: str
    watchlist_ref: str

    def __post_init__(self) -> None:
        if self.schema_version != UNIVERSE_SCHEMA_VERSION:
            raise ValueError("unsupported universe schema")
        _aware(self.resolved_at, "resolved_at")
        if type(self.stale) is not bool:
            raise ValueError("stale must be bool")
        if self.stale_since is not None:
            _aware(self.stale_since, "stale_since")
        if self.stale_since is not None and not self.stale:
            raise ValueError("stale_since requires stale")
        if type(self.entries) is not tuple or not all(
                isinstance(entry, NewsUniverseEntryV1)
                for entry in self.entries):
            raise ValueError("entries must be NewsUniverseEntryV1 tuple")
        keys = [(entry.market, entry.symbol) for entry in self.entries]
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate universe entry")
        for label, value in (("holdings_ref", self.holdings_ref),
                             ("watchlist_ref", self.watchlist_ref)):
            if type(value) is not str or not value:
                raise ValueError(f"invalid {label}")


@dataclass(frozen=True)
class PersistedUniverseV1:
    """Loaded artifact: the latest universe plus the embedded prior
    successful resolution (D13)."""

    latest: NewsUniverseV1
    previous: NewsUniverseV1 | None

    def __post_init__(self) -> None:
        if not isinstance(self.latest, NewsUniverseV1):
            raise ValueError("latest must be NewsUniverseV1")
        if self.previous is not None and not isinstance(
                self.previous, NewsUniverseV1):
            raise ValueError("previous must be NewsUniverseV1")
