"""Stage 1-A: market-local calendar request dates and typed fetch identity.

One aware UTC instant owns both markets' local dates: KR from Asia/Seoul and
US from America/New_York. ``utc_now().date()`` is never a market trade date.
"""

from datetime import UTC, date, datetime

import pytest

from tokmo.values.calendar_refresh import market_local_date
from tokmo.values.session_gate import Market


def test_same_utc_instant_yields_different_kr_us_local_dates():
    # 19:00Z: KST is already tomorrow, New York is still today.
    now = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    kr = market_local_date(Market.KR, now)
    us = market_local_date(Market.US, now)
    assert kr == date(2026, 8, 2)
    assert us == date(2026, 8, 1)
    assert kr != us
    # Neither market may silently receive the UTC calendar date twice.
    assert (kr, us) != (now.date(), now.date())


@pytest.mark.parametrize(
    ("instant", "market", "expected"),
    (
        # KST midnight boundary (UTC 15:00).
        (datetime(2026, 8, 1, 14, 59, 59, tzinfo=UTC), Market.KR, date(2026, 8, 1)),
        (datetime(2026, 8, 1, 15, 0, tzinfo=UTC), Market.KR, date(2026, 8, 2)),
        # New York midnight under EDT (UTC-4).
        (datetime(2026, 8, 2, 3, 59, 59, tzinfo=UTC), Market.US, date(2026, 8, 1)),
        (datetime(2026, 8, 2, 4, 0, tzinfo=UTC), Market.US, date(2026, 8, 2)),
        # New York midnight under EST (UTC-5).
        (datetime(2026, 1, 15, 4, 59, 59, tzinfo=UTC), Market.US, date(2026, 1, 14)),
        (datetime(2026, 1, 15, 5, 0, tzinfo=UTC), Market.US, date(2026, 1, 15)),
        # Spring-forward 2026-03-08: the skipped 02:xx hour stays one local date.
        (datetime(2026, 3, 8, 6, 59, tzinfo=UTC), Market.US, date(2026, 3, 8)),
        (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), Market.US, date(2026, 3, 8)),
        # Fall-back 2026-11-01: the repeated 01:30 local hour is two distinct
        # UTC instants mapping to the same local date.
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), Market.US, date(2026, 11, 1)),
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), Market.US, date(2026, 11, 1)),
    ),
)
def test_market_local_midnight_and_dst_boundaries(instant, market, expected):
    assert market_local_date(market, instant) == expected


def test_market_local_date_requires_aware_instant_and_known_market():
    with pytest.raises(ValueError):
        market_local_date(Market.KR, datetime(2026, 8, 1, 19, 0))
    with pytest.raises(ValueError):
        market_local_date("KR", datetime(2026, 8, 1, 19, 0, tzinfo=UTC))


# --- typed CalendarFetchResult identity (SDS §4 step 4) ---


def _receipt(market=Market.KR, trade_date=date(2026, 8, 3), ordinal=0):
    from tokmo.values.ops_kernel import CalendarStartReceiptV1

    return CalendarStartReceiptV1(
        "cadence-test-v1",
        datetime(2026, 8, 1, 19, 0, tzinfo=UTC),
        market,
        trade_date,
        ordinal,
        f"calendar:{market.value}:{trade_date.isoformat()}:{ordinal}",
        "STARTED",
    )


def _kr_day(trade_date=date(2026, 8, 3), session="valid"):
    from tokmo.values.toss_market_calendar import (
        CalendarSession,
        IntegratedHour,
        KrMarketDay,
    )

    sessions = {
        "valid": CalendarSession(
            datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
            datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
        ),
        "none": None,
        "naive": CalendarSession(
            datetime(2026, 8, 3, 9, 0), datetime(2026, 8, 3, 15, 30)
        ),
        "inverted": CalendarSession(
            datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
            datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
        ),
    }
    return KrMarketDay(trade_date, IntegratedHour(regular_market=sessions[session]))


def test_calendar_fetch_result_accepts_exact_market_date_and_boundaries():
    from tokmo.values.calendar_refresh import CalendarFetchResult

    result = CalendarFetchResult(Market.KR, date(2026, 8, 3), _kr_day(), _receipt())
    assert result.market is Market.KR
    assert result.requested_local_date == date(2026, 8, 3)
    # A no-regular-session day (holiday) is a valid identity.
    CalendarFetchResult(
        Market.KR, date(2026, 8, 3), _kr_day(session="none"), _receipt()
    )


def test_calendar_fetch_result_rejects_wrong_market_day_type():
    from tokmo.values.calendar_refresh import CalendarFetchResult, CalendarIdentityError
    from tokmo.values.toss_market_calendar import UsMarketDay

    with pytest.raises(CalendarIdentityError):
        CalendarFetchResult(
            Market.KR, date(2026, 8, 3), UsMarketDay(date(2026, 8, 3)), _receipt()
        )
    with pytest.raises(CalendarIdentityError):
        CalendarFetchResult(
            Market.US, date(2026, 8, 3), _kr_day(), _receipt(market=Market.US)
        )
    with pytest.raises(CalendarIdentityError):
        CalendarFetchResult(
            Market.KR, date(2026, 8, 3), {"date": "2026-08-03"}, _receipt()
        )


def test_calendar_fetch_result_rejects_returned_date_mismatch():
    from tokmo.values.calendar_refresh import CalendarDateError, CalendarFetchResult

    with pytest.raises(CalendarDateError):
        CalendarFetchResult(
            Market.KR,
            date(2026, 8, 3),
            _kr_day(trade_date=date(2026, 8, 4)),
            _receipt(),
        )
    with pytest.raises(CalendarDateError):
        CalendarFetchResult(
            Market.KR,
            datetime(2026, 8, 3, tzinfo=UTC),
            _kr_day(),
            _receipt(),
        )


@pytest.mark.parametrize("session", ("naive", "inverted"))
def test_calendar_fetch_result_rejects_invalid_regular_boundaries(session):
    from tokmo.values.calendar_refresh import CalendarBoundaryError, CalendarFetchResult

    with pytest.raises(CalendarBoundaryError):
        CalendarFetchResult(
            Market.KR, date(2026, 8, 3), _kr_day(session=session), _receipt()
        )


def test_integrated_calendar_stage_requests_market_local_dates_not_utc_date(tmp_path):
    """G-1: the composed calendar stage must never pass one UTC date to both markets."""
    from tokmo.programs.integrated_fake_cadence import (
        IntegratedCadenceDepsV1,
        _production_stage_deps,
    )
    from tokmo.values.ops_tick import tick_id_from_utc

    now = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
    requested = {}

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        requested[market] = local_trade_date

    deps = IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=lambda gate: None,
        quotes_provider_factory=lambda gate: None,
        orders_provider_factory=lambda gate: None,
        detail_provider_factory=lambda gate: None,
        glm_call=lambda *a, **k: None,
        sol_call=lambda *a, **k: None,
        regular_session_dates=None,
        clock=lambda: now,
    )
    stages = _production_stage_deps(tmp_path / "root", deps)
    tick = tick_id_from_utc(now)
    providers = stages.calendar_provider(tick=tick, ops_kernel=object())
    for market in Market:
        providers[market]()

    assert requested[Market.KR] == date(2026, 8, 2)
    assert requested[Market.US] == date(2026, 8, 1)
