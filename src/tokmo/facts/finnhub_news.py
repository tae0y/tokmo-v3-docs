"""Strict Finnhub company/market news JSON parser."""

from datetime import datetime, timezone
import json

from tokmo.values.news_connectors import (
    NewsConnectorItem, NewsFetchResult, NewsFetchStatus,
)

_REQUIRED = frozenset({
    "category", "datetime", "headline", "id", "image", "related",
    "source", "summary", "url",
})


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def parse_finnhub_news(body: bytes, *, observed_at,
                       max_items: int = 500) -> NewsFetchResult:
    if type(body) is not bytes or not 1 <= max_items <= 500:
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    try:
        rows = json.loads(body.decode("utf-8"), object_pairs_hook=_unique_object)
        if type(rows) is not list or len(rows) > max_items:
            raise ValueError
        items = []
        for raw in rows:
            if type(raw) is not dict or not _REQUIRED <= set(raw):
                raise ValueError
            if (type(raw["datetime"]) is not int or type(raw["id"]) is not int
                    or any(type(raw[k]) is not str for k in
                           ("headline", "source", "summary", "url"))):
                raise ValueError
            published = datetime.fromtimestamp(raw["datetime"], tz=timezone.utc)
            items.append(NewsConnectorItem(
                canonical_url=raw["url"], title=raw["headline"],
                observed_at=observed_at, source_article_id=str(raw["id"]),
                summary_text=raw["summary"] or None, published_at=published,
                publisher_name=raw["source"] or None,
            ))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError,
            OverflowError, OSError):
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    if not items:
        return NewsFetchResult(NewsFetchStatus.EMPTY)
    return NewsFetchResult(NewsFetchStatus.COLLECTED, tuple(items),
                           bytes_received=len(body))
