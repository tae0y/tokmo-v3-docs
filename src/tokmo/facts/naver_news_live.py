"""Naver News Search provider-isolated source adapter."""

from dataclasses import dataclass, field
from datetime import datetime

from tokmo.facts.naver_news import NaverNewsRequest, parse_naver_news
from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


@dataclass(frozen=True)
class NaverNewsCredentials:
    client_id: str = field(repr=False)
    client_secret: str = field(repr=False)

    def __post_init__(self) -> None:
        for value in (self.client_id, self.client_secret):
            if (
                type(value) is not str
                or not 8 <= len(value) <= 512
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)
            ):
                raise ValueError("invalid Naver credential")


class NaverNewsConnector:
    def __init__(self, http, credentials: NaverNewsCredentials) -> None:
        self._http = http
        self._credentials = credentials

    def fetch(
        self, request: NaverNewsRequest, *, observed_at: datetime
    ) -> NewsFetchResult:
        if type(request) is not NaverNewsRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(
            ClosedNewsGet(
                host="openapi.naver.com",
                path="/v1/search/news.json",
                query=(
                    ("query", request.query),
                    ("display", str(request.display)),
                    ("start", str(request.start)),
                    ("sort", request.sort),
                ),
                headers=(
                    ("Accept", "application/json"),
                    ("X-Naver-Client-Id", self._credentials.client_id),
                    ("X-Naver-Client-Secret", self._credentials.client_secret),
                ),
            )
        )
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(
                response.status,
                http_status=response.http_status,
                failure_class=response.failure_class,
            )
        parsed = parse_naver_news(
            response.body, observed_at=observed_at, max_items=request.display
        )
        return NewsFetchResult(
            parsed.status, parsed.items, response.http_status, len(response.body)
        )
