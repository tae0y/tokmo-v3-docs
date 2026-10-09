"""Structural NEWS alert renderer boundary and privacy guard."""

import re


class NewsPayloadError(ValueError):
    pass


_PRIVATE = re.compile(
    r"(?:account[_ -]?id|/Users/|/home/|[A-Za-z]:\\|secret|token|api[_-]?key|bearer\s)",
    re.IGNORECASE,
)


def contains_private_markers(text):
    """Shared privacy judgment: True when the text carries account, local
    path, or credential markers that must never reach a transport."""
    return _PRIVATE.search(str(text)) is not None


def validate_news_payload(
    *, sections, links, callback_data, revision_kind, provisional
):
    if set(sections) != {str(i) for i in range(1, 11)} or any(
        not str(value).strip() for value in sections.values()
    ):
        raise NewsPayloadError("ten required sections")
    body = "\n".join(str(value) for value in sections.values())
    if _PRIVATE.search(body) or _PRIVATE.search(callback_data):
        raise NewsPayloadError("private payload")
    if not 1 <= len(links) <= 3 or any(
        not link.startswith(("https://", "http://")) for link in links
    ):
        raise NewsPayloadError("invalid links")
    if (
        len(callback_data.encode()) > 64
        or re.fullmatch(r"n1:[ud]:[0-9a-f]{32}", callback_data) is None
    ):
        raise NewsPayloadError("invalid callback")
    if revision_kind == "EARLY_FACTUAL" and (
        not provisional or not re.search(r"잠정|미확인|unknown", body, re.IGNORECASE)
    ):
        raise NewsPayloadError("early factual must be provisional")
    if revision_kind == "ACTION_DRAFT" and not (
        "확인" in body and re.search(r"\d", body)
    ):
        raise NewsPayloadError("action confirmation/range required")
    return True
