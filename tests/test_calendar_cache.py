from datetime import date, datetime, timezone

import pytest

from tokmo.records.calendar_cache import (
    CalendarCacheCorruptError,
    CalendarCacheStore,
    CalendarCacheSchema2,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import CalendarSession, IntegratedHour, KrMarketDay, UsMarketDay

NOW = datetime(2026, 7, 30, 0, 30, tzinfo=timezone.utc)


def test_calendar_cache_typed_roundtrip_half_none_and_per_market_staleness(tmp_path):
    path = tmp_path / "calendar.json"
    store = CalendarCacheStore(path, clock=lambda: NOW)
    session = CalendarSession(
        datetime(2026, 7, 29, 23, 0, tzinfo=timezone.utc),
        datetime(2026, 7, 30, 6, 0, tzinfo=timezone.utc),
    )
    kr = KrMarketDay(date(2026, 7, 30), IntegratedHour(regular_market=None))
    us = UsMarketDay(date(2026, 7, 29), regular_market=session)
    store.write(kr, us)
    loaded = store.read()
    assert isinstance(loaded, CalendarCacheSchema2)
    assert loaded.kr.sessions == {}
    assert loaded.us.sessions["regularMarket"].start.endswith("Z")
    assert store.is_stale(Market.KR, NOW) is False
    assert store.is_stale(Market.US, NOW) is False
    boundary = datetime(2026, 7, 30, 14, 30, tzinfo=timezone.utc)
    assert store.is_stale(Market.KR, boundary) is False
    assert store.is_stale(Market.US, boundary) is True

    bad = {
        "schema_version": 1, "updated_at": "2026-07-30T00:30:00Z", "us": None,
        "kr": {
            "trade_date": "2026-07-30", "regular_start": None,
            "regular_end": "2026-07-30T06:00:00Z", "source_trade_date": "2026-07-30",
            "fetched_at": "2026-07-30T00:30:00Z", "schema_version": 1,
            "parser_version": "toss-market-calendar-v1",
        },
    }
    path.write_bytes(canonical_encode(bad))
    original = path.read_bytes()
    with pytest.raises(CalendarCacheCorruptError):
        store.read()
    assert path.read_bytes() == original


def test_calendar_cache_write_accepts_one_missing_market_and_same_dates_are_noop(tmp_path):
    path = tmp_path / "calendar.json"
    store = CalendarCacheStore(path, clock=lambda: NOW)
    us = UsMarketDay(date(2026, 7, 29), regular_market=None)

    store.write(None, us)
    first_bytes = path.read_bytes()
    first_stat = path.stat()
    assert store.read().kr is None

    store.write(None, us)
    assert path.read_bytes() == first_bytes
    assert path.stat().st_ino == first_stat.st_ino


def test_valid_v1_cache_is_compatible_but_incomplete_and_forces_refresh(tmp_path):
    path = tmp_path / "calendar.json"
    legacy_day = {
        "trade_date": "2026-07-30", "regular_start": "2026-07-30T00:00:00Z",
        "regular_end": "2026-07-30T06:00:00Z",
        "source_trade_date": "2026-07-30", "fetched_at": "2026-07-30T00:30:00Z",
        "schema_version": 1, "parser_version": "toss-market-calendar-v1",
    }
    path.write_bytes(canonical_encode({
        "schema_version": 1, "kr": legacy_day, "us": None,
        "updated_at": "2026-07-30T00:30:00Z",
    }))
    loaded = CalendarCacheStore(path, clock=lambda: NOW).read()
    assert loaded.schema_version == 2
    assert loaded.kr is None and loaded.us is None
    assert CalendarCacheStore(path, clock=lambda: NOW).is_stale(Market.KR, NOW)
