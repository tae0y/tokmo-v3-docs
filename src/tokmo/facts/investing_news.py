"""Investing KR analyst-opinion RSS parser with KST fallback timestamps."""

import re
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from html import unescape
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from defusedxml import ElementTree

from tokmo.values.external_news import ContentMode
from tokmo.values.news_connectors import (
    EvidenceRole,
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
    TimeInterpretation,
)

_KST = ZoneInfo("Asia/Seoul")


def _text(value: str) -> str:
    return " ".join(unescape(re.sub(r"<[^>]*>", "", value)).split())


def _published(value: str):
    if re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", value):
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M:%S")
    else:
        parsed = parsedate_to_datetime(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        parsed = parsed.replace(tzinfo=_KST)
        interpretation = TimeInterpretation.ASSUMED_KST
    else:
        interpretation = TimeInterpretation.EXPLICIT_ZONE
    return parsed.astimezone(UTC), interpretation


def parse_investing_news(
    body: bytes, *, observed_at, max_items: int = 100
) -> NewsFetchResult:
    if type(body) is not bytes or not 1 <= max_items <= 100:
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
            title = _text(raw.findtext("title") or "")
            link = (raw.findtext("link") or "").strip()
            parsed_link = urlsplit(link)
            if (
                not title
                or parsed_link.scheme != "https"
                or parsed_link.hostname != "kr.investing.com"
            ):
                raise ValueError
            guid = (raw.findtext("guid") or "").strip() or None
            identity = link.split("?", 1)[0]
            if guid is not None:
                previous = identities.setdefault(identity, guid)
                if previous != guid:
                    raise ValueError
            pub_raw = (raw.findtext("pubDate") or "").strip()
            published, interpretation = (
                _published(pub_raw)
                if pub_raw
                else (None, TimeInterpretation.SOURCE_NATIVE)
            )
            summary = _text(raw.findtext("description") or "") or None
            items.append(
                NewsConnectorItem(
                    canonical_url=link,
                    title=title,
                    observed_at=observed_at,
                    source_article_id=guid,
                    summary_text=summary,
                    publisher_name="Investing.com",
                    publisher_site_url="https://kr.investing.com/",
                    published_at=published,
                    content_mode=ContentMode.OPINION,
                    evidence_role=EvidenceRole.OPINION_DISCOVERY,
                    alert_eligible=False,
                    time_interpretation=interpretation,
                )
            )
    except (ElementTree.ParseError, ValueError, TypeError, OverflowError):
        return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
    if not items:
        return NewsFetchResult(NewsFetchStatus.EMPTY)
    return NewsFetchResult(
        NewsFetchStatus.COLLECTED, tuple(items), bytes_received=len(body)
    )
