"""BBC Business fixed RSS source adapter."""

from dataclasses import dataclass
from datetime import datetime
from xml.etree import ElementTree

from tokmo.facts.news_http import ClosedNewsGet, NewsHttpResponse
from tokmo.facts.news_rss import parse_rss_news
from tokmo.values.news_connectors import NewsFetchResult, NewsFetchStatus


@dataclass(frozen=True)
class BbcBusinessRequest:
    pass


class BbcBusinessConnector:
    def __init__(self, http) -> None:
        self._http = http

    def fetch(
        self, request: BbcBusinessRequest, *, observed_at: datetime
    ) -> NewsFetchResult:
        if type(request) is not BbcBusinessRequest:
            return NewsFetchResult(NewsFetchStatus.CONTRACT_MISMATCH)
        response: NewsHttpResponse = self._http.get(
            ClosedNewsGet(host="feeds.bbci.co.uk", path="/news/business/rss.xml")
        )
        if response.status is not NewsFetchStatus.COLLECTED:
            return NewsFetchResult(
                response.status,
                http_status=response.http_status,
                failure_class=response.failure_class,
            )
        body = _deduplicate_section_repeats(response.body)
        if body is None:
            return NewsFetchResult(
                NewsFetchStatus.CONTRACT_MISMATCH,
                http_status=response.http_status,
                bytes_received=len(response.body),
            )
        parsed = parse_rss_news(body, observed_at=observed_at, max_items=100)
        return NewsFetchResult(
            parsed.status, parsed.items, response.http_status, len(response.body)
        )


def _deduplicate_section_repeats(body: bytes) -> bytes | None:
    lowered = body.lower()
    if b"<!doctype" in lowered or b"<!entity" in lowered:
        return None
    try:
        root = ElementTree.fromstring(body)
        channel = root.find("./channel")
        if root.tag != "rss" or channel is None:
            return None
        seen = {}
        for item in list(channel.findall("item")):
            link = (item.findtext("link") or "").strip()
            identity = link.split("?", 1)[0]
            payload = tuple(
                (item.findtext(name) or "").strip()
                for name in (
                    "title",
                    "link",
                    "description",
                    "pubDate",
                )
            )
            if identity not in seen:
                seen[identity] = payload
                continue
            previous = seen[identity]
            if previous == payload or previous[1:] == payload[1:]:
                channel.remove(item)
            else:
                return None
        return ElementTree.tostring(root, encoding="utf-8")
    except (ElementTree.ParseError, TypeError, ValueError):
        return None
