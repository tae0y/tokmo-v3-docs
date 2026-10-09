"""Typed values shared by news connectors and downstream programs."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from urllib.parse import urlsplit

from tokmo.values.external_news import (
    ContentMode,
    EvidenceRole,
    TimeInterpretation,
)


class NewsFetchStatus(StrEnum):
    COLLECTED = "COLLECTED"
    EMPTY = "EMPTY"
    CONFIG_UNAVAILABLE = "CONFIG_UNAVAILABLE"
    TRANSPORT_FAILURE = "TRANSPORT_FAILURE"
    RATE_LIMITED = "RATE_LIMITED"
    CONTRACT_MISMATCH = "CONTRACT_MISMATCH"


class NewsOriginKind(StrEnum):
    RSS = "RSS"
    API = "API"


REVISABLE_FIELDS = frozenset(
    {
        "publisher_name",
        "title",
        "title_fingerprint",
        "summary_text",
        "published_at",
    }
)


@dataclass(frozen=True)
class NewsConnectorSpec:
    """How one upstream feed behaves, declared by the connector itself.

    ``revisable_fields`` names the payload fields this publisher legitimately
    rewrites after first publication; anything outside the set stays a
    conflict. An empty set means the connector never takes the revision path.

    ``identity_churns`` marks feeds that rotate their RSS GUID or cosmetic
    publisher metadata while serving the same canonical URL. For those the URL
    is the document identity and a rotated GUID is a refresh, not a new
    article.
    """

    connector_id: str
    origin_kind: NewsOriginKind
    source_native_key: str
    display_name: str
    canonical_url: str
    revisable_fields: frozenset[str] = frozenset()
    identity_churns: bool = False

    def __post_init__(self) -> None:
        for name in ("connector_id", "source_native_key", "display_name"):
            value = getattr(self, name)
            if type(value) is not str or not value.strip() or len(value) > 200:
                raise ValueError(f"{name} must be non-empty and bounded")
        if not isinstance(self.origin_kind, NewsOriginKind):
            raise ValueError("origin_kind must be NewsOriginKind")
        if not isinstance(self.revisable_fields, frozenset):
            raise ValueError("revisable_fields must be a frozenset")
        if not self.revisable_fields <= REVISABLE_FIELDS:
            raise ValueError("revisable_fields must be drawn from REVISABLE_FIELDS")
        if type(self.identity_churns) is not bool:
            raise ValueError("identity_churns must be bool")
        _https_url(self.canonical_url)


def _utc(value: datetime, name: str) -> None:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


def _https_url(value: str) -> None:
    if type(value) is not str:
        raise ValueError("canonical_url must be a string")
    parsed = urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("canonical_url must be credential-free absolute HTTPS")


# Feeds that churn also rewrite the publisher label on a settled article, so
# the churning connectors carry it alongside the headline fields.
_HEADLINE_REVISIONS = frozenset(
    {
        "publisher_name",
        "title",
        "title_fingerprint",
        "summary_text",
    }
)
_TIMED_REVISIONS = _HEADLINE_REVISIONS | {"published_at"}
_LABEL_REVISIONS = frozenset({"publisher_name", "title", "title_fingerprint"})

BBC_BUSINESS_RSS = NewsConnectorSpec(
    "bbc-business-rss-v1",
    NewsOriginKind.RSS,
    "connector:bbc-business-rss",
    "BBC Business RSS",
    "https://feeds.bbci.co.uk/news/business/rss.xml",
    revisable_fields=_TIMED_REVISIONS,
    identity_churns=True,
)
NAVER_NEWS_API = NewsConnectorSpec(
    "naver-news-search-v1",
    NewsOriginKind.API,
    "connector:naver-news-search",
    "Naver News Search API",
    "https://developers.naver.com/docs/serviceapi/search/news/news.md",
)
FINNHUB_COMPANY_NEWS = NewsConnectorSpec(
    "finnhub-company-news-v1",
    NewsOriginKind.API,
    "connector:finnhub-company-news",
    "Finnhub Company News",
    "https://finnhub.io/docs/api",
    revisable_fields=_TIMED_REVISIONS,
)
FINNHUB_MARKET_NEWS = NewsConnectorSpec(
    "finnhub-market-news-v1",
    NewsOriginKind.API,
    "connector:finnhub-market-news",
    "Finnhub Market News",
    "https://finnhub.io/docs/api",
    revisable_fields=_TIMED_REVISIONS,
)
YONHAP_MARKET_RSS = NewsConnectorSpec(
    "yonhap-market-rss-v1",
    NewsOriginKind.RSS,
    "connector:yonhap-market-rss",
    "Yonhap Market RSS",
    "https://www.yna.co.kr/rss/market.xml",
    revisable_fields=_HEADLINE_REVISIONS,
    identity_churns=True,
)
YONHAP_INDUSTRY_RSS = NewsConnectorSpec(
    "yonhap-industry-rss-v1",
    NewsOriginKind.RSS,
    "connector:yonhap-industry-rss",
    "Yonhap Industry RSS",
    "https://www.yna.co.kr/rss/industry.xml",
    revisable_fields=_HEADLINE_REVISIONS,
    identity_churns=True,
)
JTBC_BREAKING_RSS = NewsConnectorSpec(
    "jtbc-breaking-rss-v1",
    NewsOriginKind.RSS,
    "connector:jtbc-breaking-rss",
    "JTBC Breaking RSS",
    "https://news-ex.jtbc.co.kr/v1/get/rss/newsflesh",
    revisable_fields=_HEADLINE_REVISIONS,
    identity_churns=True,
)
GOOGLE_NEWS_SEARCH_RSS = NewsConnectorSpec(
    "google-news-search-rss-v1",
    NewsOriginKind.RSS,
    "connector:google-news-search-rss",
    "Google News Search RSS",
    "https://news.google.com/rss/search",
    revisable_fields=_LABEL_REVISIONS,
    identity_churns=True,
)
INVESTING_ANALYST_RSS = NewsConnectorSpec(
    "investing-analyst-opinion-rss-v1",
    NewsOriginKind.RSS,
    "connector:investing-analyst-opinion-rss",
    "Investing Analyst Opinion RSS",
    "https://kr.investing.com/rss/news_1061.rss",
)


@dataclass(frozen=True)
class NewsConnectorItem:
    canonical_url: str = field(repr=False)
    title: str = field(repr=False)
    observed_at: datetime
    source_article_id: str | None = field(default=None, repr=False)
    summary_text: str | None = field(default=None, repr=False)
    publisher_name: str | None = field(default=None, repr=False)
    publisher_site_url: str | None = field(default=None, repr=False)
    published_at: datetime | None = None
    content_mode: ContentMode = ContentMode.NEWS
    evidence_role: EvidenceRole = EvidenceRole.FACT_DISCOVERY
    alert_eligible: bool = True
    time_interpretation: TimeInterpretation = TimeInterpretation.SOURCE_NATIVE

    def __post_init__(self) -> None:
        _https_url(self.canonical_url)
        if type(self.title) is not str or not self.title.strip():
            raise ValueError("title must be non-empty")
        _utc(self.observed_at, "observed_at")
        if self.published_at is not None:
            _utc(self.published_at, "published_at")
        for name in ("source_article_id", "summary_text", "publisher_name"):
            value = getattr(self, name)
            if value is not None and (type(value) is not str or not value.strip()):
                raise ValueError(f"{name} must be non-empty when present")
        if self.publisher_site_url is not None:
            _https_url(self.publisher_site_url)
        if not isinstance(self.content_mode, ContentMode):
            raise ValueError("content_mode must be ContentMode")
        if not isinstance(self.evidence_role, EvidenceRole):
            raise ValueError("evidence_role must be EvidenceRole")
        if type(self.alert_eligible) is not bool:
            raise ValueError("alert_eligible must be bool")
        if not isinstance(self.time_interpretation, TimeInterpretation):
            raise ValueError("time_interpretation must be TimeInterpretation")


@dataclass(frozen=True)
class NewsFetchResult:
    status: NewsFetchStatus
    items: tuple[NewsConnectorItem, ...] = ()
    http_status: int | None = None
    bytes_received: int | None = None
    failure_class: str | None = None
    failure_stage: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.status, NewsFetchStatus):
            raise ValueError("status must be NewsFetchStatus")
        if self.status is NewsFetchStatus.COLLECTED and not self.items:
            raise ValueError("COLLECTED requires items")
        if self.status is not NewsFetchStatus.COLLECTED and self.items:
            raise ValueError("non-COLLECTED result cannot carry items")
