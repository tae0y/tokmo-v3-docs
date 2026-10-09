"""Fixture-first contract tests for RSS, Naver, and Finnhub news parsers."""

import json
from datetime import UTC, datetime

from tokmo.facts.finnhub_news import parse_finnhub_news
from tokmo.facts.investing_news import parse_investing_news
from tokmo.facts.naver_news import build_naver_news_request, parse_naver_news
from tokmo.facts.news_rss import parse_rss_news
from tokmo.values.external_news import ContentMode
from tokmo.values.news_connectors import EvidenceRole, NewsFetchStatus

NOW = datetime(2026, 8, 2, 7, 0, tzinfo=UTC)


def test_bbc_rss_maps_metadata_without_treating_summary_as_body():
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <guid>bbc-7</guid><title>Markets &amp; rates</title>
      <link>https://www.bbc.com/news/articles/abc</link>
      <description><![CDATA[<p>Summary <b>only</b>.</p>]]></description>
      <pubDate>Sun, 02 Aug 2026 06:30:00 GMT</pubDate>
    </item></channel></rss>"""

    result = parse_rss_news(body, observed_at=NOW, max_items=100)

    assert result.status is NewsFetchStatus.COLLECTED
    assert len(result.items) == 1
    item = result.items[0]
    assert item.source_article_id == "bbc-7"
    assert item.title == "Markets & rates"
    assert item.summary_text == "Summary only."
    assert item.published_at == datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
    assert "Summary" not in repr(item)


def test_rss_rejects_dtd_http_link_and_over_cap_as_whole_invocation():
    dtd = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss/>'
    assert (
        parse_rss_news(dtd, observed_at=NOW).status is NewsFetchStatus.CONTRACT_MISMATCH
    )
    insecure = b"<rss><channel><item><title>x</title><link>http://x.example/a</link></item></channel></rss>"
    assert (
        parse_rss_news(insecure, observed_at=NOW).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )
    two = (
        b"<rss><channel>"
        + b"<item><title>x</title><link>https://x.example/a</link></item>" * 2
        + b"</channel></rss>"
    )
    assert (
        parse_rss_news(two, observed_at=NOW, max_items=1).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )


def test_rss_rejects_late_dtd_and_guid_collision_before_partial_storage():
    late_dtd = b"<!--" + b"x" * 5000 + b"--><!DOCTYPE rss><rss><channel/></rss>"
    assert (
        parse_rss_news(late_dtd, observed_at=NOW).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )
    collision = b"""<rss><channel>
      <item><guid>guid-a</guid><title>a</title><link>https://x.example/a?one=1</link></item>
      <item><guid>guid-b</guid><title>b</title><link>https://x.example/a?two=2</link></item>
    </channel></rss>"""
    assert (
        parse_rss_news(collision, observed_at=NOW).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )


def test_naver_request_is_closed_and_parser_prefers_originallink():
    request = build_naver_news_request("SK하이닉스", display=1)
    assert request.host == "openapi.naver.com"
    assert request.path == "/v1/search/news.json"
    assert request.query_keys == ("query", "display", "start", "sort")
    body = json.dumps(
        {
            "items": [
                {
                    "title": "<b>SK하이닉스</b> 상승",
                    "originallink": "https://news.example.com/a?utm_source=n",
                    "link": "https://n.news.naver.com/a",
                    "description": "반도체 &amp; AI <b>뉴스</b>",
                    "pubDate": "Sun, 02 Aug 2026 15:30:00 +0900",
                }
            ]
        }
    ).encode()

    result = parse_naver_news(body, observed_at=NOW, max_items=10)

    assert result.status is NewsFetchStatus.COLLECTED
    item = result.items[0]
    assert item.canonical_url == "https://news.example.com/a?utm_source=n"
    assert item.title == "SK하이닉스 상승"
    assert item.summary_text == "반도체 & AI 뉴스"
    assert item.published_at == datetime(2026, 8, 2, 6, 30, tzinfo=UTC)


def test_naver_rejects_duplicate_json_keys_bad_limits_and_invalid_urls():
    duplicate = b'{"items":[],"items":[]}'
    assert (
        parse_naver_news(duplicate, observed_at=NOW).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )
    bad = json.dumps(
        {
            "items": [
                {
                    "title": "x",
                    "link": "javascript:x",
                    "description": "y",
                    "pubDate": "bad",
                }
            ]
        }
    ).encode()
    assert (
        parse_naver_news(bad, observed_at=NOW).status
        is NewsFetchStatus.CONTRACT_MISMATCH
    )
    for kwargs in ({"display": 0}, {"display": 11}, {"start": 2}, {"sort": "sim"}):
        try:
            build_naver_news_request("x", **kwargs)
        except ValueError:
            pass
        else:
            raise AssertionError(kwargs)


def test_naver_falls_back_to_https_link_when_originallink_is_invalid():
    body = json.dumps(
        {
            "items": [
                {
                    "title": "x",
                    "originallink": "http://legacy.example/a",
                    "link": "https://n.news.naver.com/a",
                    "description": "y",
                    "pubDate": "Sun, 02 Aug 2026 15:30:00 +0900",
                }
            ]
        }
    ).encode()
    result = parse_naver_news(body, observed_at=NOW)
    assert result.status is NewsFetchStatus.COLLECTED
    assert result.items[0].canonical_url == "https://n.news.naver.com/a"


def test_finnhub_accepts_large_company_batch_with_typed_metadata():
    rows = [
        {
            "category": "company",
            "datetime": 1785650400,
            "headline": f"Headline {i}",
            "id": i,
            "image": "",
            "related": "AAPL",
            "source": "Example",
            "summary": "Summary",
            "url": f"https://news.example.com/{i}",
        }
        for i in range(245)
    ]

    result = parse_finnhub_news(
        json.dumps(rows).encode(), observed_at=NOW, max_items=500
    )

    assert result.status is NewsFetchStatus.COLLECTED
    assert len(result.items) == 245
    assert result.items[0].source_article_id == "0"
    assert result.items[0].published_at.tzinfo is UTC


def test_finnhub_rejects_missing_fields_bool_timestamp_and_over_cap():
    base = {
        "category": "company",
        "datetime": 1785650400,
        "headline": "h",
        "id": 1,
        "image": "",
        "related": "AAPL",
        "source": "s",
        "summary": "x",
        "url": "https://news.example.com/1",
    }
    missing = dict(base)
    missing.pop("url")
    boolean = dict(base)
    boolean["datetime"] = True
    for rows, cap in (([missing], 500), ([boolean], 500), ([base, base], 1)):
        result = parse_finnhub_news(
            json.dumps(rows).encode(), observed_at=NOW, max_items=cap
        )
        assert result.status is NewsFetchStatus.CONTRACT_MISMATCH


def test_empty_lists_are_available_empty_not_failure():
    assert (
        parse_naver_news(b'{"items":[]}', observed_at=NOW).status
        is NewsFetchStatus.EMPTY
    )
    assert parse_finnhub_news(b"[]", observed_at=NOW).status is NewsFetchStatus.EMPTY
    assert (
        parse_rss_news(b"<rss><channel/></rss>", observed_at=NOW).status
        is NewsFetchStatus.EMPTY
    )


def test_investing_naive_pubdate_uses_kst_and_is_shadow_opinion():
    body = b"""<rss><channel><item>
      <guid>inv-1</guid><title>Analyst view</title>
      <link>https://kr.investing.com/news/stock-market-news/article-1</link>
      <pubDate>Sun, 02 Aug 2026 15:30:00</pubDate>
    </item></channel></rss>"""

    result = parse_investing_news(body, observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    item = result.items[0]
    assert item.published_at == datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
    assert item.content_mode is ContentMode.OPINION
    assert item.evidence_role is EvidenceRole.OPINION_DISCOVERY
    assert item.alert_eligible is False


def test_investing_live_numeric_pubdate_uses_kst():
    body = b"""<rss><channel><item>
      <title>Live analyst view</title>
      <link>https://kr.investing.com/news/stock-market-news/article-live</link>
      <pubDate>2026-08-02 15:30:00</pubDate>
    </item></channel></rss>"""

    result = parse_investing_news(body, observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    assert result.items[0].published_at == datetime(2026, 8, 2, 6, 30, tzinfo=UTC)
    assert result.items[0].time_interpretation.value == "ASSUMED_KST"


def test_investing_explicit_utc_and_us_timezone_override_kst_fallback():
    body = b"""<rss><channel>
      <item><guid>utc</guid><title>UTC view</title>
        <link>https://kr.investing.com/news/a</link>
        <pubDate>Sun, 02 Aug 2026 15:30:00 UTC</pubDate></item>
      <item><guid>edt</guid><title>EDT view</title>
        <link>https://kr.investing.com/news/b</link>
        <pubDate>Sun, 02 Aug 2026 15:30:00 EDT</pubDate></item>
    </channel></rss>"""

    result = parse_investing_news(body, observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    assert result.items[0].published_at == datetime(2026, 8, 2, 15, 30, tzinfo=UTC)
    assert result.items[1].published_at == datetime(2026, 8, 2, 19, 30, tzinfo=UTC)


def test_investing_rejects_malformed_date_dtd_and_guid_url_collision():
    malformed = b"<rss><channel><item><guid>x</guid><title>x</title><link>https://kr.investing.com/news/x</link><pubDate>bad</pubDate></item></channel></rss>"
    dtd = b'<!DOCTYPE rss [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss/>'
    collision = b"""<rss><channel>
      <item><guid>a</guid><title>a</title><link>https://kr.investing.com/news/x?one=1</link></item>
      <item><guid>b</guid><title>b</title><link>https://kr.investing.com/news/x?two=2</link></item>
    </channel></rss>"""
    for body in (malformed, dtd, collision):
        assert (
            parse_investing_news(body, observed_at=NOW).status
            is NewsFetchStatus.CONTRACT_MISMATCH
        )
