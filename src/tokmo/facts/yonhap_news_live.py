"""Yonhap provider-owned RSS adapters."""

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.facts.news_rss import parse_rss_news
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


class YonhapFeed(StrEnum):
    MARKET = "market"
    INDUSTRY = "industry"


@dataclass(frozen=True)
class YonhapNewsRequest:
    feed: YonhapFeed

    def __post_init__(self) -> None:
        if not isinstance(self.feed, YonhapFeed):
            raise ValueError("feed must be YonhapFeed")


class YonhapNewsConnector:
    def __init__(self, http) -> None:
        self._http = http

    def fetch(
        self, request: YonhapNewsRequest, *, observed_at: datetime
    ) -> NewsFetchResult:
        if type(request) is not YonhapNewsRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(
            ClosedNewsGet(host="www.yna.co.kr", path=f"/rss/{request.feed.value}.xml")
        )
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(
                response.status,
                http_status=response.http_status,
                failure_class=response.failure_class,
            )
        parsed = parse_rss_news(response.body, observed_at=observed_at, max_items=200)
        return NewsFetchResult(
            parsed.status, parsed.items, response.http_status, len(response.body)
        )
