"""Strict parser for Google News search RSS aggregator results."""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit
from xml.etree import ElementTree

from tokmo.values.news_connectors import (
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
)


def parse_google_news_rss(
    body: bytes,
    *,
    observed_at: datetime,
    max_items: int = 200,
) -> NewsFetchResult:
    """Parse one bounded Google RSS body without resolving aggregator links."""
    try:
        lowered = body.lower()
        if b"<!doctype" in lowered or b"<!entity" in lowered:
            raise ValueError("DTD/entity declarations are forbidden")
        if max_items < 1 or max_items > 200:
            raise ValueError("invalid max_items")
        root = ElementTree.fromstring(body)
        nodes = root.findall("./channel/item")
        if len(nodes) > max_items:
            raise ValueError("item cap exceeded")
        items: list[NewsConnectorItem] = []
        identities: dict[str, str] = {}
        for node in nodes:
            guid = _required(node.findtext("guid"), "guid")
            link = _required(node.findtext("link"), "link")
            title = _required(node.findtext("title"), "title")
            source = node.find("source")
            if source is None:
                raise ValueError("missing source")
            publisher_name = _required(source.text, "publisher_name")
            publisher_site_url = _required(source.get("url"), "publisher_site_url")
            if urlsplit(publisher_site_url).scheme != "https":
                publisher_site_url = None
            published_at = parsedate_to_datetime(
                _required(node.findtext("pubDate"), "pubDate")
            )
            if published_at.tzinfo is None:
                raise ValueError("naive pubDate")
            path_identity = link.split("?", 1)[0]
            previous = identities.setdefault(path_identity, guid)
            if previous != guid:
                raise ValueError("canonical identity collision")
            items.append(NewsConnectorItem(
                canonical_url=link,
                source_article_id=guid,
                title=title,
                published_at=published_at,
                observed_at=observed_at,
                publisher_name=publisher_name,
                publisher_site_url=publisher_site_url,
            ))
        if not items:
            return NewsFetchResult(NewsFetchStatus.EMPTY)
        return NewsFetchResult(NewsFetchStatus.COLLECTED, tuple(items))
    except (ElementTree.ParseError, TypeError, ValueError, OverflowError):
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)


def _required(value: str | None, field: str) -> str:
    if value is None or not value.strip():
        raise ValueError(f"missing {field}")
    return value.strip()