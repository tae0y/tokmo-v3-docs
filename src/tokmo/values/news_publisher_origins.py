"""Publisher/syndication origin normalization (D-5).

SDS 2026-09-01 A1 §R as repaired by §V8/§W1/§X1-X3 (decisions 15·19-trace):
independence is counted per ORIGIN, never per feed. The table is keyed by
the canonical ``tokmo.values.news_connectors`` ``source_native_key`` and
consulted at runtime through the exact persisted derivation
``source_id = sha256(b"source\\0" + source_native_key)`` — byte-identical
to ``records/external_news.py`` persistence (test-pinned, JA-26). Runner
aliases (``yna-market`` …) are invocation names only and never keys.

Fail-closed: a source outside the table confers NO origin by itself; a
portal/unknown document resolves only through the closed publisher alias
map, and every unresolved document collapses into the single unknown
bucket (at most ONE independence unit per counting population).
"""
import hashlib
import unicodedata

from tokmo.values.news_instrument_match import normalize_company_name

# source_native_key → ("ORIGIN", origin_id) | ("PORTAL", None). All nine
# catalogued connector specs are covered (JA-27 pins the bijection).
PUBLISHER_ORIGINS_V1 = {
    "connector:yonhap-market-rss": ("ORIGIN", "yna"),
    "connector:yonhap-industry-rss": ("ORIGIN", "yna"),
    "connector:jtbc-breaking-rss": ("ORIGIN", "jtbc"),
    "connector:bbc-business-rss": ("ORIGIN", "bbc"),
    "connector:investing-analyst-opinion-rss": ("ORIGIN", "investing"),
    "connector:naver-news-search": ("PORTAL", None),
    "connector:finnhub-company-news": ("PORTAL", None),
    "connector:finnhub-market-news": ("PORTAL", None),
    "connector:google-news-search-rss": ("PORTAL", None),
}

# Closed publisher-name aliases (§V8): applied after NFKC casefold,
# whitespace collapse, and the §O.1-2 legal-suffix strip. Origin tokens
# coalesce with the wire table above (연합 계열 → yna, JA-26).
KNOWN_PUBLISHER_ALIASES_V1 = {
    "연합뉴스": "yna",
    "연합뉴스tv": "yna",
    "jtbc": "jtbc",
    "bbc": "bbc",
    "bbc news": "bbc",
}

UNKNOWN_ORIGIN_BUCKET = "__unknown__"


def persisted_source_id(source_native_key):
    """The exact n5 persistence derivation (§X1) — byte-identical to
    ``external_news.py``'s ``_hash("source", source_native_key)``."""
    return hashlib.sha256(
        b"source\0" + str(source_native_key).encode("utf-8")).hexdigest()


_HASHED_TABLE = {persisted_source_id(key): entry
                 for key, entry in PUBLISHER_ORIGINS_V1.items()}


def _normalize_publisher(publisher_name):
    if publisher_name is None:
        return ""
    collapsed = " ".join(unicodedata.normalize(
        "NFKC", str(publisher_name)).casefold().split())
    return normalize_company_name(collapsed)


def resolve_publisher_origin(source_id, publisher_name):
    """origin_id | None for one persisted document (§V8 precedence).

    A first-party wire source_id decides alone (the transport is truth);
    portal and out-of-table source_ids resolve only through the publisher
    alias map; anything unresolved is None — never an independence unit of
    its own."""
    entry = _HASHED_TABLE.get(str(source_id))
    if entry is not None and entry[0] == "ORIGIN":
        return entry[1]
    return KNOWN_PUBLISHER_ALIASES_V1.get(
        _normalize_publisher(publisher_name))


def count_independent_origins(pairs):
    """Distinct-origin count over ``(source_id, publisher_name)`` pairs:
    resolved origins count individually; ALL unresolved documents collapse
    into one shared unknown unit (§V8 단일 버킷, at most +1)."""
    origins = set()
    unknown = False
    for source_id, publisher_name in pairs:
        origin = resolve_publisher_origin(source_id, publisher_name)
        if origin is None:
            unknown = True
        else:
            origins.add(origin)
    return len(origins) + (1 if unknown else 0)
