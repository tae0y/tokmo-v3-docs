"""Source-specific NEWS connector contracts added after the v0.2 expansion."""

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from email.message import Message
import json
import sqlite3
from urllib.error import HTTPError

from tokmo.facts.bbc_news_live import BbcBusinessConnector, BbcBusinessRequest
from tokmo.facts.finnhub_news_live import (
    FinnhubCompanyNewsConnector,
    FinnhubCompanyNewsRequest,
    FinnhubMarketNewsConnector,
    FinnhubMarketNewsRequest,
    FinnhubNewsCredentials,
)
from tokmo.facts.google_news import parse_google_news_rss
from tokmo.facts.google_news_live import GoogleNewsConnector, GoogleNewsQuery
from tokmo.facts.jtbc_news_live import JtbcBreakingConnector, JtbcBreakingRequest
from tokmo.facts.investing_news_live import (
    InvestingAnalystConnector, InvestingAnalystRequest,
)
from tokmo.facts.naver_news_live import NaverNewsConnector, NaverNewsCredentials
from tokmo.facts.news_http import (
    ClosedNewsGet, NewsHttpResponse, NewsHttpTransport, _NoRedirect,
)
from tokmo.facts.yonhap_news_live import YonhapFeed, YonhapNewsConnector, YonhapNewsRequest
from tokmo.facts.naver_news import build_naver_news_request
from tokmo.values.news_connectors import NewsFetchStatus

NOW = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)


def test_google_rss_preserves_aggregator_and_publisher_provenance():
    body = b"""<?xml version="1.0" encoding="UTF-8"?>
    <rss version="2.0"><channel><item>
      <guid isPermaLink="false">google-guid-7</guid>
      <title>Company update - Publisher</title>
      <link>https://news.google.com/rss/articles/google-7?oc=5</link>
      <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate>
      <description><![CDATA[<a href="https://publisher.example/a">Company update</a>]]></description>
      <source url="https://publisher.example">Publisher</source>
    </item></channel></rss>"""

    result = parse_google_news_rss(body, observed_at=NOW, max_items=200)

    assert result.status is NewsFetchStatus.COLLECTED
    item = result.items[0]
    assert item.source_article_id == "google-guid-7"
    assert item.canonical_url == "https://news.google.com/rss/articles/google-7?oc=5"
    assert item.publisher_name == "Publisher"
    assert item.publisher_site_url == "https://publisher.example"
    assert "Publisher" not in repr(item)


def test_google_rss_rejects_missing_publisher_but_drops_insecure_metadata_url():
    missing = b"""<rss><channel><item><guid>g</guid><title>x</title>
    <link>https://news.google.com/rss/articles/x</link>
    <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate>
    </item></channel></rss>"""
    insecure = b"""<rss><channel><item><guid>g</guid><title>x</title>
    <link>https://news.google.com/rss/articles/x</link>
    <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate>
    <source url="http://publisher.example">Publisher</source>
    </item></channel></rss>"""

    assert parse_google_news_rss(missing, observed_at=NOW).status is NewsFetchStatus.CONTRACT_MISMATCH
    result = parse_google_news_rss(insecure, observed_at=NOW)
    assert result.status is NewsFetchStatus.COLLECTED
    assert result.items[0].publisher_name == "Publisher"
    assert result.items[0].publisher_site_url is None


def test_google_rss_rejects_dtd_and_entity_declarations():
    body = b'<!DOCTYPE rss [<!ENTITY x "EXPANDED">]><rss><channel><item>' \
           b'<guid>g</guid><title>&x;</title><link>https://news.google.com/rss/articles/x</link>' \
           b'<pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate>' \
           b'<source url="https://publisher.example">Publisher</source>' \
           b'</item></channel></rss>'
    assert parse_google_news_rss(body, observed_at=NOW).status is NewsFetchStatus.CONTRACT_MISMATCH


@dataclass
class FakeHttp:
    response: NewsHttpResponse

    def __post_init__(self):
        self.requests = []

    def get(self, request):
        self.requests.append(request)
        return self.response


def test_investing_live_adapter_owns_fixed_kr_rss_path():
    body = b"""<rss><channel><item><guid>i</guid><title>view</title>
    <link>https://kr.investing.com/news/a</link>
    <pubDate>Sun, 02 Aug 2026 15:30:00</pubDate></item></channel></rss>"""
    http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, body))

    result = InvestingAnalystConnector(http).fetch(
        InvestingAnalystRequest(), observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    assert http.requests[0].host == "kr.investing.com"
    assert http.requests[0].path == "/rss/news_1061.rss"
    assert http.requests[0].headers == (("User-Agent", "Tokmo/1.0 RSS Reader"),)


def test_bbc_connector_accepts_current_feed_size_above_30_items():
    rows = b"".join(
        f"<item><guid>g{i}</guid><title>title {i}</title>"
        f"<link>https://www.bbc.co.uk/news/articles/{i}</link>"
        f"<pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate></item>".encode()
        for i in range(31)
    )
    body = b"<rss><channel>" + rows + b"</channel></rss>"
    http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, body))

    result = BbcBusinessConnector(http).fetch(BbcBusinessRequest(), observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    assert len(result.items) == 31
    assert len(http.requests) == 1


def test_bbc_connector_deduplicates_section_repeats_and_title_only_variants():
    def item(guid, title="same", description="same description"):
        return (f"<item><guid>{guid}</guid><title>{title}</title>"
                f"<link>https://www.bbc.co.uk/news/articles/x?at_medium=RSS</link>"
                f"<description>{description}</description>"
                f"<pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate></item>").encode()
    duplicate = b"<rss><channel>" + item("https://www.bbc.co.uk/news/articles/x#0") \
        + item("https://www.bbc.co.uk/news/articles/x#3") + b"</channel></rss>"
    title_variant = b"<rss><channel>" + item("g0", "short title") \
        + item("g1", "expanded title") + b"</channel></rss>"

    exact = BbcBusinessConnector(FakeHttp(NewsHttpResponse(
        NewsFetchStatus.COLLECTED, 200, duplicate))).fetch(BbcBusinessRequest(), observed_at=NOW)
    variant = BbcBusinessConnector(FakeHttp(NewsHttpResponse(
        NewsFetchStatus.COLLECTED, 200, title_variant))).fetch(BbcBusinessRequest(), observed_at=NOW)

    assert exact.status is NewsFetchStatus.COLLECTED
    assert len(exact.items) == 1
    assert variant.status is NewsFetchStatus.COLLECTED
    assert len(variant.items) == 1
    assert variant.items[0].title == "short title"


def test_bbc_connector_rejects_non_title_drift_for_same_canonical_url():
    def item(guid, description):
        return (f"<item><guid>{guid}</guid><title>same</title>"
                f"<link>https://www.bbc.co.uk/news/articles/x?at_medium=RSS</link>"
                f"<description>{description}</description>"
                f"<pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate></item>").encode()
    drift = b"<rss><channel>" + item("g0", "first description") \
        + item("g1", "changed description") + b"</channel></rss>"

    rejected = BbcBusinessConnector(FakeHttp(NewsHttpResponse(
        NewsFetchStatus.COLLECTED, 200, drift))).fetch(BbcBusinessRequest(), observed_at=NOW)

    assert rejected.status is NewsFetchStatus.CONTRACT_MISMATCH


def test_closed_news_get_hides_query_and_headers_and_transport_rejects_unknown_host():
    request = ClosedNewsGet(
        host="evil.example",
        path="/x",
        query=(("token", "secret-token"),),
        headers=(("X-Secret", "secret-header"),),
    )
    shown = repr(request)
    assert "secret-token" not in shown
    assert "secret-header" not in shown
    assert "evil.example" not in shown

    transport = NewsHttpTransport(allowed_hosts=frozenset({"news.google.com"}))
    response = transport.get(request)
    assert response.status is NewsFetchStatus.CONTRACT_MISMATCH
    assert response.body == b""


def test_news_http_redirect_handler_never_builds_a_followup_request():
    handler = _NoRedirect()
    assert handler.redirect_request(
        object(), None, 302, "Found", {}, "https://evil.example/stolen",
    ) is None


class _Response:
    def __init__(self, body=b"ok", status=200, headers=None):
        self.body, self.status, self.headers = body, status, headers or {}
    def __enter__(self): return self
    def __exit__(self, *args): return False
    def read(self, limit): return self.body[:limit]


class _Opener:
    def __init__(self, result): self.result, self.calls = result, 0
    def open(self, request, timeout):
        self.calls += 1
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


def test_news_http_wire_maps_redirect_caps_encoding_auth_and_crlf_without_retry():
    request = ClosedNewsGet("allowed.example", "/feed")
    cases = (
        (HTTPError("https://allowed.example/feed", 302, "Found", Message(), None),
         NewsFetchStatus.TRANSPORT_FAILURE),
        (_Response(b"x" * 1_048_577), NewsFetchStatus.CONTRACT_MISMATCH),
        (_Response(headers={"Content-Length": "1048577"}),
         NewsFetchStatus.CONTRACT_MISMATCH),
        (_Response(headers={"Content-Length": "not-a-number"}),
         NewsFetchStatus.CONTRACT_MISMATCH),
        (_Response(headers={"Content-Encoding": "gzip"}),
         NewsFetchStatus.CONTRACT_MISMATCH),
        (HTTPError("https://allowed.example/feed", 429, "Rate", Message(), None),
         NewsFetchStatus.RATE_LIMITED),
        (HTTPError("https://allowed.example/feed", 401, "Auth", Message(), None),
         NewsFetchStatus.CONFIG_UNAVAILABLE),
        (HTTPError("https://allowed.example/feed", 403, "Auth", Message(), None),
         NewsFetchStatus.CONFIG_UNAVAILABLE),
    )
    for wire_result, expected in cases:
        opener = _Opener(wire_result)
        transport = NewsHttpTransport(
            allowed_hosts=frozenset({"allowed.example"}), opener=opener)
        assert transport.get(request).status is expected
        assert opener.calls == 1
    happy = _Opener(_Response(b"ok", headers={"Content-Length": "2"}))
    assert NewsHttpTransport(
        allowed_hosts=frozenset({"allowed.example"}), opener=happy,
    ).get(request).status is NewsFetchStatus.COLLECTED
    assert happy.calls == 1
    opener = _Opener(_Response())
    transport = NewsHttpTransport(
        allowed_hosts=frozenset({"allowed.example"}), opener=opener)
    assert transport.get(ClosedNewsGet("allowed.example", "/x\r\nInjected: yes")).status \
        is NewsFetchStatus.CONTRACT_MISMATCH
    assert opener.calls == 0


def test_google_connector_owns_keyword_query_and_uses_google_parser():
    body = b"""<rss><channel><item><guid>g</guid><title>x</title>
    <link>https://news.google.com/rss/articles/x?oc=5</link>
    <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate>
    <source url="https://publisher.example">Publisher</source>
    </item></channel></rss>"""
    http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, body))
    connector = GoogleNewsConnector(http)

    result = connector.fetch(GoogleNewsQuery("SK하이닉스"), observed_at=NOW)

    assert result.status is NewsFetchStatus.COLLECTED
    request = http.requests[0]
    assert request.host == "news.google.com"
    assert request.path == "/rss/search"
    assert request.query == (("q", "SK하이닉스"), ("hl", "ko"), ("gl", "KR"), ("ceid", "KR:ko"))


def test_yonhap_and_jtbc_are_separate_fixed_feed_connectors():
    body = b"""<rss><channel><item><guid>g</guid><title>x</title>
    <link>https://www.yna.co.kr/view/AKR1</link>
    <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate></item></channel></rss>"""
    yna_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, body))
    yna = YonhapNewsConnector(yna_http)
    result = yna.fetch(YonhapNewsRequest(YonhapFeed.MARKET), observed_at=NOW)
    assert result.status is NewsFetchStatus.COLLECTED
    assert yna_http.requests[0].host == "www.yna.co.kr"
    assert yna_http.requests[0].path == "/rss/market.xml"
    assert yna_http.requests[0].query == ()

    jtbc_body = body.replace(b"www.yna.co.kr/view/AKR1", b"news.jtbc.co.kr/article/NB1")
    jtbc_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, jtbc_body))
    jtbc = JtbcBreakingConnector(jtbc_http)
    result = jtbc.fetch(JtbcBreakingRequest(), observed_at=NOW)
    assert result.status is NewsFetchStatus.COLLECTED
    assert jtbc_http.requests[0].host == "news-ex.jtbc.co.kr"
    assert jtbc_http.requests[0].path == "/v1/get/rss/newsflesh"
    assert jtbc_http.requests[0].query == ()


def test_bbc_naver_and_finnhub_connectors_own_distinct_request_contracts():
    rss = b"""<rss><channel><item><guid>g</guid><title>x</title>
    <link>https://www.bbc.com/news/articles/x</link>
    <pubDate>Sun, 02 Aug 2026 08:30:00 GMT</pubDate></item></channel></rss>"""
    bbc_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, rss))
    assert BbcBusinessConnector(bbc_http).fetch(
        BbcBusinessRequest(), observed_at=NOW
    ).status is NewsFetchStatus.COLLECTED
    assert bbc_http.requests[0].host == "feeds.bbci.co.uk"

    naver_body = b'{"items":[]}'
    naver_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, naver_body))
    naver_credentials = NaverNewsCredentials("client-id-123", "client-secret-123")
    naver = NaverNewsConnector(naver_http, naver_credentials)
    assert naver.fetch(
        build_naver_news_request("SK하이닉스"), observed_at=NOW
    ).status is NewsFetchStatus.EMPTY
    assert naver_http.requests[0].host == "openapi.naver.com"
    assert {key for key, _ in naver_http.requests[0].headers} == {
        "Accept", "X-Naver-Client-Id", "X-Naver-Client-Secret"
    }
    assert "client-secret-123" not in repr(naver_credentials)

    finnhub_body = b"[]"
    finnhub_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, finnhub_body))
    finnhub_credentials = FinnhubNewsCredentials("finnhub-key-123")
    company = FinnhubCompanyNewsConnector(finnhub_http, finnhub_credentials)
    request = FinnhubCompanyNewsRequest("AAPL", date(2026, 8, 1), date(2026, 8, 2))
    assert company.fetch(request, observed_at=NOW).status is NewsFetchStatus.EMPTY
    assert finnhub_http.requests[0].path == "/api/v1/company-news"
    assert {key for key, _ in finnhub_http.requests[0].query} == {
        "symbol", "from", "to", "token"
    }
    market_http = FakeHttp(NewsHttpResponse(NewsFetchStatus.COLLECTED, 200, finnhub_body))
    market = FinnhubMarketNewsConnector(market_http, finnhub_credentials)
    assert market.fetch(FinnhubMarketNewsRequest(), observed_at=NOW).status is NewsFetchStatus.EMPTY
    assert market_http.requests[0].path == "/api/v1/news"
    assert "finnhub-key-123" not in repr(finnhub_credentials)


def _finnhub_row(source):
    return {
        "category": "company", "datetime": 1754121600, "headline": "Chip ramp",
        "id": 7001, "image": "", "related": "NVDA", "source": source,
        "summary": "capacity up", "url": "https://finnhub.example/story-7001",
    }


def test_finnhub_parser_maps_raw_source_to_publisher_name():
    """R-PUB-1: raw["source"] -> publisher_name; empty string -> None."""
    from tokmo.facts.finnhub_news import parse_finnhub_news

    named = parse_finnhub_news(
        json.dumps([_finnhub_row("MarketWatch")]).encode(), observed_at=NOW)
    assert named.status is NewsFetchStatus.COLLECTED
    assert named.items[0].publisher_name == "MarketWatch"
    assert named.items[0].publisher_site_url is None

    unnamed = parse_finnhub_news(
        json.dumps([_finnhub_row("")]).encode(), observed_at=NOW)
    assert unnamed.status is NewsFetchStatus.COLLECTED
    assert unnamed.items[0].publisher_name is None


def test_finnhub_refetch_revises_null_publisher_name_with_evidence(tmp_path):
    """R-PUB-1: re-save of an existing NULL-publisher row takes the revision
    path (publisher_name is a declared revisable field for Finnhub specs)."""
    from tokmo.facts.finnhub_news import parse_finnhub_news
    from tokmo.programs.external_news_storage import bootstrap_external_news
    from tokmo.programs.news_intake import persist_news_result
    from tokmo.values.external_news import SaveOutcome
    from tokmo.values.news_connectors import (
        FINNHUB_COMPANY_NEWS, NewsConnectorItem, NewsFetchResult,
    )

    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    historical = NewsConnectorItem(
        canonical_url="https://finnhub.example/story-7001",
        title="Chip ramp", observed_at=NOW, source_article_id="7001",
        summary_text="capacity up",
        published_at=datetime(2025, 8, 2, 8, 0, tzinfo=timezone.utc),
    )
    first = persist_news_result(
        path, FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (historical,)),
        target_ref="symbol:NVDA")[0]
    assert first.outcome is SaveOutcome.INSERTED

    refetched = parse_finnhub_news(
        json.dumps([_finnhub_row("MarketWatch")]).encode(),
        observed_at=NOW + timedelta(minutes=15))
    revised = persist_news_result(
        path, FINNHUB_COMPANY_NEWS, refetched, target_ref="symbol:NVDA")[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.revision_evidence_id is not None
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT publisher_name, revision_no FROM news_documents"
        ).fetchone() == ("MarketWatch", 2)
    finally:
        connection.close()
