"""Executable NEWS polling policy contracts."""

from datetime import UTC, datetime

import pytest

from tokmo.programs.news_poll_policy import (
    NEWS_GROUPS_V1,
    NewsGroup,
    news_tick_id,
)


def test_news_groups_v1_partition_every_source_exactly_once():
    assert NEWS_GROUPS_V1 == {
        NewsGroup.A: ("bbc", "yna-market", "naver"),
        NewsGroup.B: ("finnhub-company", "jtbc-breaking", "investing-analyst"),
        NewsGroup.C: ("finnhub-market", "yna-industry"),
    }
    flattened = [source for group in NEWS_GROUPS_V1.values() for source in group]
    assert len(flattened) == len(set(flattened)) == 8
    assert "google-news-search" not in flattened


def test_news_tick_id_is_canonical_15_minute_slot_per_group():
    assert (
        news_tick_id(NewsGroup.B, datetime(2026, 8, 2, 12, 20, 59, tzinfo=UTC))
        == "news16-v1:2026-08-02T12:20:00.000000Z:B"
    )
    with pytest.raises(ValueError, match="group_offset"):
        news_tick_id(NewsGroup.B, datetime(2026, 8, 2, 12, 21, tzinfo=UTC))


def test_news_tick_rejects_naive_datetime():
    with pytest.raises(ValueError, match="scheduled_for"):
        news_tick_id(NewsGroup.A, datetime(2026, 8, 2, 12, 0))
