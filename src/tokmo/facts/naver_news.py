"""Closed Naver News Search request and strict JSON parser."""

import json
from dataclasses import dataclass
from datetime import UTC
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

from tokmo.values.news_connectors import (
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
)


@dataclass(frozen=True)
class NaverNewsRequest:
    query: str
    display: int
    host: str = "openapi.naver.com"
    path: str = "/v1/search/news.json"
    start: int = 1
    sort: str = "date"
    query_keys: tuple[str, ...] = ("query", "display", "start", "sort")


def build_naver_news_request(
    query: str, *, display: int = 10, start: int = 1, sort: str = "date"
) -> NaverNewsRequest:
    if type(query) is not str or not query.strip() or len(query) > 200:
        raise ValueError("query must be non-empty and bounded")
    if type(display) is not int or not 1 <= display <= 10:
        raise ValueError("display must be 1..10 for walking slice")
    if start != 1 or sort != "date":
        raise ValueError("walking slice fixes start=1 and sort=date")
    return NaverNewsRequest(query.strip(), display)


class _TextOnly(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self.parts.append(data)


def _plain(value: str) -> str:
    parser = _TextOnly()
    parser.feed(value)
    parser.close()
    return " ".join(unescape("".join(parser.parts)).split())


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _valid_https(value: object) -> bool:
    if type(value) is not str:
        return False
    parsed = urlsplit(value.strip())
    return bool(
        parsed.scheme == "https"
        and parsed.hostname
        and parsed.username is None
        and parsed.password is None
    )


def parse_naver_news(
    body: bytes, *, observed_at, max_items: int = 10
) -> NewsFetchResult:
    if type(body) is not bytes or not 1 <= max_items <= 10:
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    try:
        root = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        raw_items = root.get("items") if type(root) is dict else None
        if type(raw_items) is not list or len(raw_items) > max_items:
            raise ValueError
        items = []
        for raw in raw_items:
            if type(raw) is not dict:
                raise ValueError
            required = ("title", "link", "description", "pubDate")
            if any(type(raw.get(key)) is not str for key in required):
                raise ValueError
            title = _plain(raw["title"])
            summary = _plain(raw["description"])
            canonical = raw.get("originallink")
            if not _valid_https(canonical):
                canonical = raw["link"]
            if not _valid_https(canonical):
                raise ValueError
            canonical_value = str(canonical).strip()
            published = parsedate_to_datetime(raw["pubDate"])
            if published.tzinfo is None or published.utcoffset() is None:
                raise ValueError
            items.append(
                NewsConnectorItem(
                    canonical_url=canonical_value,
                    title=title,
                    summary_text=summary or None,
                    observed_at=observed_at,
                    published_at=published.astimezone(UTC),
                )
            )
    except (
        UnicodeDecodeError,
        json.JSONDecodeError,
        ValueError,
        TypeError,
        OverflowError,
    ):
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    if not items:
        return NewsFetchResult(NewsFetchStatus.EMPTY)
    return NewsFetchResult(
        NewsFetchStatus.COLLECTED, tuple(items), bytes_received=len(body)
    )
