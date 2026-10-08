"""Finnhub company/market provider-isolated NEWS adapters."""

from dataclasses import dataclass, field
from datetime import date, datetime
import re

from tokmo.facts.finnhub_news import parse_finnhub_news
from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus

_SYMBOL = re.compile(r"[A-Z][A-Z0-9.-]{0,19}\Z")


@dataclass(frozen=True)
class FinnhubNewsCredentials:
    api_key: str = field(repr=False)

    def __post_init__(self) -> None:
        value = self.api_key
        if (type(value) is not str or not 8 <= len(value) <= 512
                or any(ch.isspace() or ord(ch) < 32 or ord(ch) == 127 for ch in value)):
            raise ValueError("invalid Finnhub credential")


@dataclass(frozen=True)
class FinnhubCompanyNewsRequest:
    symbol: str
    from_date: date
    to_date: date

    def __post_init__(self) -> None:
        if (type(self.symbol) is not str or _SYMBOL.fullmatch(self.symbol) is None
                or type(self.from_date) is not date or type(self.to_date) is not date
                or self.from_date > self.to_date
                or (self.to_date - self.from_date).days > 31):
            raise ValueError("invalid Finnhub company-news request")


@dataclass(frozen=True)
class FinnhubMarketNewsRequest:
    category: str = "general"

    def __post_init__(self) -> None:
        if self.category != "general":
            raise ValueError("only category=general is allowed")


class FinnhubCompanyNewsConnector:
    def __init__(self, http, credentials: FinnhubNewsCredentials) -> None:
        self._http = http
        self._credentials = credentials

    def fetch(self, request: FinnhubCompanyNewsRequest, *, observed_at: datetime) -> NewsFetchResult:
        if type(request) is not FinnhubCompanyNewsRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(ClosedNewsGet(
            host="finnhub.io", path="/api/v1/company-news",
            query=(("symbol", request.symbol), ("from", request.from_date.isoformat()),
                   ("to", request.to_date.isoformat()), ("token", self._credentials.api_key)),
        ))
        return _finish(response, observed_at)


class FinnhubMarketNewsConnector:
    def __init__(self, http, credentials: FinnhubNewsCredentials) -> None:
        self._http = http
        self._credentials = credentials

    def fetch(self, request: FinnhubMarketNewsRequest, *, observed_at: datetime) -> NewsFetchResult:
        if type(request) is not FinnhubMarketNewsRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(ClosedNewsGet(
            host="finnhub.io", path="/api/v1/news",
            query=(("category", request.category), ("token", self._credentials.api_key)),
        ))
        return _finish(response, observed_at)


def _finish(response: NewsHttpResponse, observed_at: datetime) -> NewsFetchResult:
    if response.status is not NewsFetchStatus.COLLECTED:
        return NewsFetchResult(response.status, http_status=response.http_status,
                                   failure_class=response.failure_class)
    parsed = parse_finnhub_news(response.body, observed_at=observed_at, max_items=500)
    return NewsFetchResult(parsed.status, parsed.items, response.http_status, len(response.body))
