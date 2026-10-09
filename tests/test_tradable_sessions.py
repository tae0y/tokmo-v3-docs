from datetime import UTC, date, datetime, timedelta

import pytest

from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.values._primitives import MISSING
from tokmo.values.session_gate import (
    ENABLED_SESSION_KINDS,
    Market,
    SessionKind,
    judge_sessions,
    regular_session_for,
)
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketDay,
    UsMarketDay,
)

BASE = datetime(2026, 8, 3, tzinfo=UTC)


def session(start_hour, end_hour):
    return CalendarSession(
        BASE + timedelta(hours=start_hour), BASE + timedelta(hours=end_hour)
    )


def days():
    return (
        KrMarketDay(
            date(2026, 8, 3),
            IntegratedHour(
                pre_market=session(0, 1),
                regular_market=session(2, 3),
                after_market=session(4, 5),
            ),
        ),
        UsMarketDay(
            date(2026, 8, 2),
            day_market=session(0, 1),
            pre_market=session(2, 3),
            regular_market=session(4, 5),
            after_market=session(6, 7),
        ),
    )


@pytest.mark.parametrize(
    "market,hour",
    [
        (Market.KR, 0),
        (Market.KR, 2),
        (Market.KR, 4),
        (Market.US, 0),
        (Market.US, 2),
        (Market.US, 4),
        (Market.US, 6),
    ],
)
def test_every_api_declared_supported_session_is_open_at_exact_start(market, hour):
    kr, us = days()
    verdict = judge_sessions(kr, us, BASE + timedelta(hours=hour))[market is Market.US]
    assert (verdict.open, verdict.reason) == (True, "open")


@pytest.mark.parametrize(
    "market,hour",
    [
        (Market.KR, 1),
        (Market.KR, 3),
        (Market.KR, 5),
        (Market.US, 1),
        (Market.US, 3),
        (Market.US, 5),
        (Market.US, 7),
    ],
)
def test_exact_end_and_inter_session_gaps_are_closed(market, hour):
    kr, us = days()
    verdict = judge_sessions(kr, us, BASE + timedelta(hours=hour))[market is Market.US]
    assert verdict.open is False


def test_all_null_or_missing_sessions_are_closed():
    kr = KrMarketDay(
        date(2026, 8, 3),
        IntegratedHour(
            pre_market=None,
            regular_market=MISSING,
            after_market=None,
        ),
    )
    us = UsMarketDay(
        date(2026, 8, 2),
        day_market=None,
        pre_market=MISSING,
        regular_market=None,
        after_market=MISSING,
    )
    assert [v.open for v in judge_sessions(kr, us, BASE)] == [False, False]


def test_enabled_policy_can_remove_and_readd_one_session_kind():
    kr, us = days()
    without_pre = {
        **ENABLED_SESSION_KINDS,
        Market.KR: tuple(
            kind
            for kind in ENABLED_SESSION_KINDS[Market.KR]
            if kind is not SessionKind.PRE_MARKET
        ),
    }
    assert judge_sessions(kr, us, BASE, enabled_sessions=without_pre)[0].open is False
    assert judge_sessions(kr, us, BASE, enabled_sessions=ENABLED_SESSION_KINDS)[0].open


def test_malformed_boundary_fails_closed():
    malformed = CalendarSession(BASE + timedelta(hours=2), BASE)
    kr = KrMarketDay(date(2026, 8, 3), IntegratedHour(pre_market=malformed))
    assert judge_sessions(kr, None, BASE + timedelta(hours=1))[0].open is False


def test_cache_roundtrip_and_restart_preserves_all_supported_sessions(tmp_path):
    path = tmp_path / "calendar.json"
    kr, us = days()
    CalendarCacheStore(path, clock=lambda: BASE).write(kr, us)
    reopened = CalendarCacheStore(path, clock=lambda: BASE).read()
    assert reopened.schema_version == 2
    assert set(reopened.kr.sessions) == {"preMarket", "regularMarket", "afterMarket"}
    assert set(reopened.us.sessions) == {
        "dayMarket",
        "preMarket",
        "regularMarket",
        "afterMarket",
    }


def test_regular_history_semantics_ignore_non_regular_existence():
    kr = KrMarketDay(date(2026, 8, 3), IntegratedHour(pre_market=session(0, 1)))
    us = UsMarketDay(date(2026, 8, 2), day_market=session(0, 1))
    assert regular_session_for(kr, Market.KR, BASE) is None
    assert regular_session_for(us, Market.US, BASE) is None
