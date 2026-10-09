"""Strict RSS 2.0 metadata parser for NEWS connectors."""

from datetime import UTC
from email.utils import parsedate_to_datetime
from html import unescape
from html.parser import HTMLParser
from xml.etree import ElementTree

from tokmo.values.news_connectors import (
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
)


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


def parse_rss_news(
    body: bytes, *, observed_at, max_items: int = 100
) -> NewsFetchResult:
    if type(body) is not bytes or not 1 <= max_items <= 500:
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    try:
        root = ElementTree.fromstring(body)
        if root.tag != "rss":
            raise ValueError
        raw_items = root.findall("./channel/item")
        if len(raw_items) > max_items:
            raise ValueError
        items = []
        identities: dict[str, str] = {}
        for raw in raw_items:
            title = _plain(raw.findtext("title") or "")
            link = (raw.findtext("link") or "").strip()
            if not title or not link:
                raise ValueError
            guid = (raw.findtext("guid") or "").strip() or None
            path_identity = link.split("?", 1)[0]
            if guid is not None:
                previous = identities.setdefault(path_identity, guid)
                if previous != guid:
                    raise ValueError
            description = _plain(raw.findtext("description") or "") or None
            pub_raw = (raw.findtext("pubDate") or "").strip()
            published = None
            if pub_raw:
                published = parsedate_to_datetime(pub_raw)
                if published.tzinfo is None or published.utcoffset() is None:
                    raise ValueError
                published = published.astimezone(UTC)
            items.append(
                NewsConnectorItem(
                    canonical_url=link,
                    title=title,
                    observed_at=observed_at,
                    source_article_id=guid,
                    summary_text=description,
                    published_at=published,
                )
            )
    except (ElementTree.ParseError, ValueError, TypeError, OverflowError):
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    if not items:
        return NewsFetchResult(NewsFetchStatus.EMPTY)
    return NewsFetchResult(
        NewsFetchStatus.COLLECTED, tuple(items), bytes_received=len(body)
    )
