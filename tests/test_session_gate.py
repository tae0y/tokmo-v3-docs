from datetime import date, datetime, timezone

import pytest

from tokmo.values._primitives import MISSING
from tokmo.values.session_gate import Market, judge_sessions
from tokmo.values.toss_market_calendar import CalendarSession, IntegratedHour, KrMarketDay, UsMarketDay
from tokmo.programs.session_gate import run_session_gate
from tokmo.records.calendar_cache import (
    CalendarCacheSchema2, CalendarDayCacheSchema2, CalendarSessionCacheSchema2,
)


def cached_day(trade_date, start, end):
    sessions = {} if start is None else {
        "regularMarket": CalendarSessionCacheSchema2(start, end),
    }
    return CalendarDayCacheSchema2(
        trade_date, sessions, trade_date, "2026-07-30T01:00:00Z", 2,
        "toss-market-calendar-v1",
    )


def test_holiday_is_not_a_session_reason_token():
    """SDS G-9: ``no_regular`` is the sole no-session reason; the dead
    ``holiday`` token may not name the same state twice."""
    from tokmo.values.session_gate import SESSION_REASON_TOKENS, SessionVerdict

    assert "holiday" not in SESSION_REASON_TOKENS
    with pytest.raises(ValueError):
        SessionVerdict(Market.KR, False, "holiday", None)


def test_session_gate_handles_holiday_short_day_and_us_date():
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    short = CalendarSession(
        datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc),
    )
    us_regular = CalendarSession(
        datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 7, 0, tzinfo=timezone.utc),
    )
    kr = KrMarketDay(date(2026, 7, 30), IntegratedHour(regular_market=short))
    us = UsMarketDay(date(2026, 7, 29), day_market=us_regular, regular_market=us_regular)

    verdicts = judge_sessions(kr, us, now)
    assert verdicts[0].market is Market.KR
    assert (verdicts[0].open, verdicts[0].reason) == (False, "after_close")
    assert (verdicts[1].open, verdicts[1].reason) == (True, "open")

    closed = judge_sessions(
        KrMarketDay(date(2026, 7, 30), MISSING),
        UsMarketDay(date(2026, 7, 29), day_market=us_regular, regular_market=MISSING),
        now,
    )
    assert [item.reason for item in closed] == ["no_regular", "open"]


def test_session_gate_writer_owner_refreshes_while_read_only_fails_closed():
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    regular = CalendarSession(
        datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc),
    )
    kr = KrMarketDay(date(2026, 7, 30), IntegratedHour(regular_market=regular))
    us = UsMarketDay(date(2026, 7, 29), regular_market=regular)
    writes = []
    owner = run_session_gate(
        kr_calendar_provider=lambda: kr, us_calendar_provider=lambda: us,
        cache_read=lambda: None, cache_write=lambda left, right: writes.append((left, right)),
        now=now,
    )
    assert owner.any_open is True
    assert owner.cache_written is True
    assert writes == [(kr, us)]

    calls = []
    read_only = run_session_gate(
        kr_calendar_provider=lambda: calls.append("kr"),
        us_calendar_provider=lambda: calls.append("us"),
        cache_read=lambda: None, cache_write=None, now=now,
    )
    assert read_only.any_open is False
    assert {verdict.reason for verdict in read_only.verdicts} == {"cache_absent"}
    assert calls == []


def test_session_gate_stale_read_only_closes_only_stale_market():
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    cache = CalendarCacheSchema2(
        2,
        cached_day("2026-07-29", "2026-07-30T00:00:00Z", "2026-07-30T02:00:00Z"),
        cached_day("2026-07-29", "2026-07-30T00:00:00Z", "2026-07-30T02:00:00Z"),
        "2026-07-30T01:00:00Z",
    )
    calls = []
    result = run_session_gate(
        kr_calendar_provider=lambda: calls.append("kr"),
        us_calendar_provider=lambda: calls.append("us"),
        cache_read=lambda: cache, cache_write=None, now=now,
    )
    assert [(item.market, item.open, item.reason) for item in result.verdicts] == [
        (Market.KR, False, "stale_cache"),
        (Market.US, True, "open"),
    ]
    assert calls == []


def test_session_gate_owner_refetches_only_stale_market_and_writes_combined_cache():
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    cache = CalendarCacheSchema2(
        2,
        cached_day("2026-07-29", None, None),
        cached_day("2026-07-29", "2026-07-30T00:00:00Z", "2026-07-30T02:00:00Z"),
        "2026-07-30T01:00:00Z",
    )
    regular = CalendarSession(
        datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 2, 0, tzinfo=timezone.utc),
    )
    fresh_kr = KrMarketDay(date(2026, 7, 30), IntegratedHour(regular_market=regular))
    calls = []
    writes = []
    result = run_session_gate(
        kr_calendar_provider=lambda: calls.append("kr") or fresh_kr,
        us_calendar_provider=lambda: calls.append("us"),
        cache_read=lambda: cache,
        cache_write=lambda kr, us: writes.append((kr, us)),
        now=now,
    )
    assert calls == ["kr"]
    assert writes[0][0] is fresh_kr
    assert writes[0][1].date == date(2026, 7, 29)
    assert result.cache_written is True
    assert [item.open for item in result.verdicts] == [True, True]


def test_session_gate_provider_failure_marks_missing_or_stale_market_stale_cache():
    now = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
    result = run_session_gate(
        kr_calendar_provider=lambda: (_ for _ in ()).throw(RuntimeError("down")),
        us_calendar_provider=lambda: None,
        cache_read=lambda: None, cache_write=lambda _kr, _us: None, now=now,
    )
    assert result.any_open is False
    assert {item.reason for item in result.verdicts} == {"stale_cache"}


def test_session_gate_cache_corrupt_fails_closed_without_provider_or_write():
    calls = []
    result = run_session_gate(
        kr_calendar_provider=lambda: calls.append("kr"),
        us_calendar_provider=lambda: calls.append("us"),
        cache_read=lambda: (_ for _ in ()).throw(ValueError("corrupt")),
        cache_write=lambda _kr, _us: calls.append("write"),
        now=datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc),
    )
    assert result.any_open is False
    assert {item.reason for item in result.verdicts} == {"cache_corrupt"}
    assert calls == []
