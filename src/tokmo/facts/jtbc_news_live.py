"""JTBC provider-owned breaking RSS adapter."""

from dataclasses import dataclass
from datetime import datetime

from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.facts.news_rss import parse_rss_news
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


@dataclass(frozen=True)
class JtbcBreakingRequest:
    pass


class JtbcBreakingConnector:
    def __init__(self, http) -> None:
        self._http = http

    def fetch(self, request: JtbcBreakingRequest, *, observed_at: datetime) -> NewsFetchResult:
        if type(request) is not JtbcBreakingRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(ClosedNewsGet(
            host="news-ex.jtbc.co.kr", path="/v1/get/rss/newsflesh"
        ))
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(response.status, http_status=response.http_status,
                                   failure_class=response.failure_class)
        parsed = parse_rss_news(response.body, observed_at=observed_at, max_items=100)
        return NewsFetchResult(parsed.status, parsed.items, response.http_status, len(response.body))
