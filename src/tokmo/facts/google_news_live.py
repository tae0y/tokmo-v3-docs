"""Google News search RSS source adapter."""

from dataclasses import dataclass
from datetime import datetime

from tokmo.facts.google_news import parse_google_news_rss
from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


@dataclass(frozen=True)
class GoogleNewsQuery:
    keyword: str

    def __post_init__(self) -> None:
        if type(self.keyword) is not str or not self.keyword.strip() or len(self.keyword) > 200:
            raise ValueError("keyword must be non-empty and bounded")


class GoogleNewsConnector:
    def __init__(self, http) -> None:
        self._http = http

    def fetch(self, request: GoogleNewsQuery, *, observed_at: datetime) -> NewsFetchResult:
        if type(request) is not GoogleNewsQuery:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(ClosedNewsGet(
            host="news.google.com",
            path="/rss/search",
            query=(("q", request.keyword.strip()), ("hl", "ko"), ("gl", "KR"), ("ceid", "KR:ko")),
        ))
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(response.status, http_status=response.http_status,
                                   failure_class=response.failure_class)
        parsed = parse_google_news_rss(response.body, observed_at=observed_at, max_items=200)
        return NewsFetchResult(parsed.status, parsed.items, response.http_status, len(response.body))
