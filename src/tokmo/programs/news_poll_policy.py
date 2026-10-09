"""Versioned NEWS polling group and tick policy."""

from datetime import UTC, datetime
from enum import StrEnum


class NewsGroup(StrEnum):
    A = "A"
    B = "B"
    C = "C"


NEWS_GROUPS_V1 = {
    NewsGroup.A: ("bbc", "yna-market", "naver"),
    NewsGroup.B: ("finnhub-company", "jtbc-breaking", "investing-analyst"),
    NewsGroup.C: ("finnhub-market", "yna-industry"),
}
_GROUP_OFFSET = {NewsGroup.A: 0, NewsGroup.B: 5, NewsGroup.C: 10}


def news_tick_id(
    group: NewsGroup, scheduled_for: datetime, *, fast_primary=False
) -> str:
    if (
        not isinstance(scheduled_for, datetime)
        or scheduled_for.tzinfo is None
        or scheduled_for.utcoffset() is None
    ):
        raise ValueError("scheduled_for must be timezone-aware")
    scheduled_utc = scheduled_for.astimezone(UTC)
    interval = 5 if fast_primary else 15
    offset = (
        {NewsGroup.A: 0, NewsGroup.B: 1, NewsGroup.C: 2}[group]
        if fast_primary
        else _GROUP_OFFSET[group]
    )
    if scheduled_utc.minute % interval != offset:
        raise ValueError("group_offset mismatch")
    slot = scheduled_utc.replace(second=0, microsecond=0)
    prefix = "news5-primary-v1" if fast_primary else "news16-v1"
    return f"{prefix}:{slot.strftime('%Y-%m-%dT%H:%M:%S.%fZ')}:{group.value}"
