"""Publisher/syndication origin normalization (D-5; JA-20..23, JA-26/27).

The origin table is keyed by canonical connector native keys converted
through the exact persisted source_id derivation; Yonhap feeds coalesce to
``yna``; unknown collapses fail-closed into one bucket.
"""

from tokmo.values.news_connectors import (
    BBC_BUSINESS_RSS,
    FINNHUB_COMPANY_NEWS,
    FINNHUB_MARKET_NEWS,
    GOOGLE_NEWS_SEARCH_RSS,
    INVESTING_ANALYST_RSS,
    JTBC_BREAKING_RSS,
    NAVER_NEWS_API,
    YONHAP_INDUSTRY_RSS,
    YONHAP_MARKET_RSS,
)
from tokmo.values.news_publisher_origins import (
    KNOWN_PUBLISHER_ALIASES_V1,
    PUBLISHER_ORIGINS_V1,
    count_independent_origins,
    persisted_source_id,
    resolve_publisher_origin,
)

YNA_MARKET = persisted_source_id("connector:yonhap-market-rss")
YNA_INDUSTRY = persisted_source_id("connector:yonhap-industry-rss")
NAVER = persisted_source_id("connector:naver-news-search")
GOOGLE = persisted_source_id("connector:google-news-search-rss")


def test_ja26_persisted_derivation_matches_production_and_coalesces_yna():
    # Byte-identical to the records-layer persistence derivation that the
    # real intake writes into every n5 row (external_news.py:
    # ``source_id = _hash("source", spec.source_native_key)``).
    from tokmo.records.external_news import _hash as production_hash

    for spec in (YONHAP_MARKET_RSS, YONHAP_INDUSTRY_RSS):
        assert persisted_source_id(spec.source_native_key) == production_hash(
            "source", spec.source_native_key
        )
    # The two real persisted Yonhap feed IDs coalesce to ONE origin (yna).
    origins = {
        resolve_publisher_origin(source_id, None)
        for source_id in (YNA_MARKET, YNA_INDUSTRY)
    }
    assert origins == {"yna"}
    assert count_independent_origins([(YNA_MARKET, None), (YNA_INDUSTRY, None)]) == 1


def test_ja27_all_nine_catalog_specs_resolve_non_unknown():
    native_keys = {
        spec.source_native_key
        for spec in (
            BBC_BUSINESS_RSS,
            NAVER_NEWS_API,
            FINNHUB_COMPANY_NEWS,
            FINNHUB_MARKET_NEWS,
            YONHAP_MARKET_RSS,
            YONHAP_INDUSTRY_RSS,
            JTBC_BREAKING_RSS,
            GOOGLE_NEWS_SEARCH_RSS,
            INVESTING_ANALYST_RSS,
        )
    }
    assert native_keys == set(PUBLISHER_ORIGINS_V1)  # exact bijection
    for key, (kind, origin) in PUBLISHER_ORIGINS_V1.items():
        assert kind in ("ORIGIN", "PORTAL")
        if kind == "ORIGIN":
            assert resolve_publisher_origin(persisted_source_id(key), None) == origin
        else:
            # portals resolve through publisher mapping — a KNOWN
            # publisher never lands in the unknown bucket (JA-20).
            assert (
                resolve_publisher_origin(persisted_source_id(key), "연합뉴스") == "yna"
            )


def test_ja20_to_23_portal_and_precedence_matrix():
    # JA-20 portal + known publisher → the ORIGIN of the publisher
    assert resolve_publisher_origin(NAVER, "연합뉴스") == "yna"
    assert resolve_publisher_origin(GOOGLE, "BBC News") == "bbc"
    # JA-21 portal + unknown publisher → None (unknown bucket)
    assert resolve_publisher_origin(NAVER, "지역매체") is None
    # JA-22 missing publisher → None
    assert resolve_publisher_origin(NAVER, None) is None
    assert resolve_publisher_origin(GOOGLE, "") is None
    # JA-23 conflicting mappings: a first-party wire source_id WINS over a
    # contradictory publisher name (transport is truth).
    assert resolve_publisher_origin(YNA_MARKET, "JTBC") == "yna"
    # out-of-table source_id confers no origin by itself…
    assert resolve_publisher_origin("f" * 64, None) is None
    # …but a known publisher still resolves it.
    assert resolve_publisher_origin("f" * 64, "JTBC") == "jtbc"


def test_unknowns_collapse_to_a_single_bucket():
    pairs = [("x" * 64, None), ("y" * 64, "매체일"), ("z" * 64, "매체이")]
    assert count_independent_origins(pairs) == 1
    assert count_independent_origins(pairs + [(YNA_MARKET, None)]) == 2
    assert count_independent_origins([]) == 0
    assert "연합뉴스" in KNOWN_PUBLISHER_ALIASES_V1
