"""Investing KR analyst-opinion RSS live adapter."""

from dataclasses import dataclass
from datetime import datetime

from tokmo.facts.investing_news import parse_investing_news
from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


@dataclass(frozen=True)
class InvestingAnalystRequest:
    pass


class InvestingAnalystConnector:
    def __init__(self, http) -> None:
        self._http = http

    def fetch(self, request: InvestingAnalystRequest, *, observed_at: datetime) -> NewsFetchResult:
        if type(request) is not InvestingAnalystRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(ClosedNewsGet(
            host="kr.investing.com", path="/rss/news_1061.rss",
            headers=(("User-Agent", "Tokmo/1.0 RSS Reader"),),
        ))
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(response.status, http_status=response.http_status,
                                   failure_class=response.failure_class)
        parsed = parse_investing_news(response.body, observed_at=observed_at, max_items=100)
        return NewsFetchResult(parsed.status, parsed.items, response.http_status,
                               len(response.body))
