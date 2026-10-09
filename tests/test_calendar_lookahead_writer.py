"""Calendar lookahead writer seam: Toss response → LookaheadDayV1 → store.

Plan §13.3: the 300s calendar owner preserves the provider response's
``today`` and ``next_business_day`` in CalendarLookaheadStoreV1 as current
and next business trade days, each bound to the calendar wire receipt
identity. CalendarCacheSchema2 is untouched; the writer only calls
verify_against_current_cache and store.replace_market_days.

US Day Market 09:00 KST (previous NY evening 20:00 EDT) and 13:00 KST (NY
midnight) continuity is preserved because the absolute UTC intervals are
copied verbatim — the trade-date identity is the provider's, never derived
from the market's current local date.
"""

from datetime import UTC, datetime

import pytest

from tokmo.records.calendar_cache import (
    CalendarDayCacheSchema2,
    CalendarSessionCacheSchema2,
)
from tokmo.records.calendar_lookahead import (
    CalendarLookaheadConflictError,
    CalendarLookaheadStoreV1,
    judge_lookahead_session,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketDay,
    UsMarketDay,
)

CLOCK_NOW = datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
KR_PROVENANCE = "calendar:KR:2026-08-10:0"
US_PROVENANCE = "calendar:US:2026-08-10:1"


def _store(tmp_path):
    return CalendarLookaheadStoreV1(
        tmp_path / "private" / "calendar_lookahead.sqlite3",
        clock=lambda: CLOCK_NOW,
    )


def _us_day(date_iso, dm=None, pre=None, reg=None, after=None):
    from datetime import date as date_cls

    return UsMarketDay(
        date=date_cls.fromisoformat(date_iso),
        day_market=dm,
        pre_market=pre,
        regular_market=reg,
        after_market=after,
    )


def _kr_day(date_iso, pre=None, reg=None, after=None):
    from datetime import date as date_cls

    return KrMarketDay(
        date=date_cls.fromisoformat(date_iso),
        integrated=IntegratedHour(
            pre_market=pre, regular_market=reg, after_market=after
        ),
    )


def _sess(start, end):
    return CalendarSession(
        start_time=datetime.fromisoformat(start.replace("Z", "+00:00")),
        end_time=datetime.fromisoformat(end.replace("Z", "+00:00")),
    )


# --- US Day Market continuity -------------------------------------------


def test_us_today_and_next_day_both_committed(tmp_path):
    """US today (2026-08-10 with Day Market 09:00-17:00 KST) and next
    business day are both committed as a 2-day window."""
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
        pre=_sess("2026-08-10T08:00:00Z", "2026-08-10T13:30:00Z"),
        reg=_sess("2026-08-10T13:30:00Z", "2026-08-10T20:00:00Z"),
        after=_sess("2026-08-10T20:00:00Z", "2026-08-10T23:50:00Z"),
    )
    next_day = _us_day(
        "2026-08-11",
        dm=_sess("2026-08-11T00:00:00Z", "2026-08-11T08:00:00Z"),
        pre=_sess("2026-08-11T08:00:00Z", "2026-08-11T13:30:00Z"),
        reg=_sess("2026-08-11T13:30:00Z", "2026-08-11T20:00:00Z"),
        after=_sess("2026-08-11T20:00:00Z", "2026-08-11T23:50:00Z"),
    )
    write_lookahead_from_calendar(
        market=Market.US,
        response_today=today,
        response_next=next_day,
        receipt_identity=US_PROVENANCE,
        store=store,
        current_cache_entry=None,
        fetched_at=CLOCK_NOW,
    )
    days = store.read_days(Market.US)
    assert len(days) == 2
    assert days[0].trade_date == "2026-08-10"
    assert days[1].trade_date == "2026-08-11"
    assert days[0].sessions[0].kind == "dayMarket"
    assert days[0].sessions[0].start_utc == datetime(2026, 8, 10, 0, 0, tzinfo=UTC)
    assert days[0].sessions[0].end_utc == datetime(2026, 8, 10, 8, 0, tzinfo=UTC)
    assert days[0].provenance == US_PROVENANCE


def test_us_day_market_09kst_13kst_continuity(tmp_path):
    """The interval-membership gate must stay OPEN from 09:00 through
    13:00 KST without a trade-date break — the absolute UTC interval
    is continuous and trade_date is the provider's."""
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
        pre=_sess("2026-08-10T08:00:00Z", "2026-08-10T13:30:00Z"),
        reg=_sess("2026-08-10T13:30:00Z", "2026-08-10T20:00:00Z"),
        after=_sess("2026-08-10T20:00:00Z", "2026-08-10T23:50:00Z"),
    )
    write_lookahead_from_calendar(
        market=Market.US,
        response_today=today,
        response_next=None,
        receipt_identity=US_PROVENANCE,
        store=store,
        current_cache_entry=None,
        fetched_at=CLOCK_NOW,
    )
    at_09kst = datetime(2026, 8, 10, 0, 30, tzinfo=UTC)  # 09:30 KST
    at_13kst = datetime(2026, 8, 10, 4, 0, tzinfo=UTC)  # 13:00 KST
    v1 = judge_lookahead_session(Market.US, store.read_days(Market.US), at_09kst)
    v2 = judge_lookahead_session(Market.US, store.read_days(Market.US), at_13kst)
    assert v1.open and v2.open
    assert v1.trade_date == "2026-08-10"
    assert v2.trade_date == "2026-08-10"
    assert v1.session_kind == "dayMarket"
    assert v2.session_kind == "dayMarket"


def test_next_business_day_none_commits_only_today(tmp_path):
    """If the provider does not support next horizon or returns None, only
    today is committed — fail-closed, no arbitrary estimation."""
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    write_lookahead_from_calendar(
        market=Market.US,
        response_today=today,
        response_next=None,
        receipt_identity=US_PROVENANCE,
        store=store,
        current_cache_entry=None,
        fetched_at=CLOCK_NOW,
    )
    days = store.read_days(Market.US)
    assert len(days) == 1
    assert days[0].trade_date == "2026-08-10"


def test_next_business_day_date_mismatch_fails_closed(tmp_path):
    """If response_next's date doesn't advance past today, fail-closed."""
    from tokmo.programs.calendar_lookahead_writer import (
        LookaheadWriterError,
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    bad_next = _us_day(
        "2026-08-10",  # same date, not actually next
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    with pytest.raises(LookaheadWriterError):
        write_lookahead_from_calendar(
            market=Market.US,
            response_today=today,
            response_next=bad_next,
            receipt_identity=US_PROVENANCE,
            store=store,
            current_cache_entry=None,
            fetched_at=CLOCK_NOW,
        )


def test_current_cache_conflict_fails_closed(tmp_path):
    """Same trade-date identity with different session bytes → conflict."""
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    conflicting_cache = CalendarDayCacheSchema2(
        trade_date="2026-08-10",
        sessions={
            "dayMarket": CalendarSessionCacheSchema2(
                start="2026-08-10T01:00:00Z",  # different!
                end="2026-08-10T08:00:00Z",
            ),
        },
        source_trade_date="2026-08-10",
        fetched_at="2026-08-10T00:00:00Z",
        schema_version=2,
        parser_version="toss-market-calendar-v1",
    )
    with pytest.raises(CalendarLookaheadConflictError):
        write_lookahead_from_calendar(
            market=Market.US,
            response_today=today,
            response_next=None,
            receipt_identity=US_PROVENANCE,
            store=store,
            current_cache_entry=conflicting_cache,
            fetched_at=CLOCK_NOW,
        )


def test_kr_today_and_next_day_committed(tmp_path):
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _kr_day(
        "2026-08-10",
        pre=_sess("2026-08-10T00:00:00Z", "2026-08-10T00:30:00Z"),
        reg=_sess("2026-08-10T00:30:00Z", "2026-08-10T06:30:00Z"),
        after=_sess("2026-08-10T06:30:00Z", "2026-08-10T10:00:00Z"),
    )
    next_day = _kr_day(
        "2026-08-11",
        pre=_sess("2026-08-11T00:00:00Z", "2026-08-11T00:30:00Z"),
        reg=_sess("2026-08-11T00:30:00Z", "2026-08-11T06:30:00Z"),
        after=_sess("2026-08-11T06:30:00Z", "2026-08-11T10:00:00Z"),
    )
    write_lookahead_from_calendar(
        market=Market.KR,
        response_today=today,
        response_next=next_day,
        receipt_identity=KR_PROVENANCE,
        store=store,
        current_cache_entry=None,
        fetched_at=CLOCK_NOW,
    )
    days = store.read_days(Market.KR)
    assert len(days) == 2
    assert days[0].trade_date == "2026-08-10"
    assert days[1].trade_date == "2026-08-11"
    assert {s.kind for s in days[0].sessions} == {
        "preMarket",
        "regularMarket",
        "afterMarket",
    }
    assert days[0].provenance == KR_PROVENANCE


def test_invalid_provenance_rejected(tmp_path):
    """Provenance must be a calendar receipt identity."""
    from tokmo.programs.calendar_lookahead_writer import (
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    with pytest.raises(ValueError):
        write_lookahead_from_calendar(
            market=Market.US,
            response_today=today,
            response_next=None,
            receipt_identity="not-a-receipt",
            store=store,
            current_cache_entry=None,
            fetched_at=CLOCK_NOW,
        )


def test_kr_day_without_sessions_fails_closed(tmp_path):
    """A KR day with no sessions at all is a provider error."""
    from tokmo.programs.calendar_lookahead_writer import (
        LookaheadWriterError,
        write_lookahead_from_calendar,
    )

    store = _store(tmp_path)
    empty_today = KrMarketDay(
        date=__import__("datetime").date(2026, 8, 10),
        integrated=IntegratedHour(),  # all MISSING → no sessions
    )
    with pytest.raises((LookaheadWriterError, ValueError)):
        write_lookahead_from_calendar(
            market=Market.KR,
            response_today=empty_today,
            response_next=None,
            receipt_identity=KR_PROVENANCE,
            store=store,
            current_cache_entry=None,
            fetched_at=CLOCK_NOW,
        )


def test_malformed_today_date_raises_typed_error(tmp_path):
    """A malformed day object with an invalid date attribute must raise
    LookaheadWriterError, not AttributeError."""
    from tokmo.programs.calendar_lookahead_writer import (
        LookaheadWriterError,
        write_lookahead_from_calendar,
    )

    class BadDay:
        # Mimics the day interface but date is a string, not a date object
        date = "not-a-date"

    store = _store(tmp_path)
    with pytest.raises(LookaheadWriterError):
        write_lookahead_from_calendar(
            market=Market.US,
            response_today=BadDay(),
            response_next=None,
            receipt_identity=US_PROVENANCE,
            store=store,
            current_cache_entry=None,
            fetched_at=CLOCK_NOW,
        )


def test_malformed_next_date_raises_typed_error(tmp_path):
    """A malformed next day with invalid date must raise
    LookaheadWriterError, not AttributeError."""
    from tokmo.programs.calendar_lookahead_writer import (
        LookaheadWriterError,
        write_lookahead_from_calendar,
    )

    class BadNext:
        date = "not-a-date"

    store = _store(tmp_path)
    today = _us_day(
        "2026-08-10",
        dm=_sess("2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
    )
    with pytest.raises(LookaheadWriterError):
        write_lookahead_from_calendar(
            market=Market.US,
            response_today=today,
            response_next=BadNext(),
            receipt_identity=US_PROVENANCE,
            store=store,
            current_cache_entry=None,
            fetched_at=CLOCK_NOW,
        )
