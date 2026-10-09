"""CalendarLookaheadStoreV1 and the interval-membership quote gate (§13).

The US Day Market fixture is the sanitized real 2026-08-10 official Toss
calendar: trade_date 2026-08-10 with dayMarket 00:00–08:00Z (09:00–17:00
KST), preMarket 08:00–13:30Z, regularMarket 13:30–20:00Z, afterMarket
20:00–23:50Z. The trade-date identity is the Toss ``trade_date``; the gate
selects by provider-declared absolute ``[start, end)`` membership in UTC,
never by the market's current local date.
"""

from datetime import datetime

import pytest

from tokmo.records.calendar_cache import (
    CalendarDayCacheSchema2,
    CalendarSessionCacheSchema2,
)
from tokmo.records.calendar_lookahead import (
    CalendarLookaheadConflictError,
    CalendarLookaheadCorruptError,
    CalendarLookaheadStoreV1,
    LookaheadDayV1,
    LookaheadSessionV1,
    judge_lookahead_session,
    verify_against_current_cache,
)
from tokmo.values.session_gate import Market


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


CLOCK_NOW = _utc("2026-08-10T00:00:00Z")


def _session(kind, start, end):
    return LookaheadSessionV1(kind, _utc(start), _utc(end))


def _us_day_edt(trade_date="2026-08-10", offset_days=0):
    from datetime import timedelta

    def shift(text):
        value = _utc(text) + timedelta(days=offset_days)
        return value.isoformat().replace("+00:00", "Z")

    return LookaheadDayV1(
        market=Market.US,
        trade_date=trade_date,
        source_trade_date=trade_date,
        sessions=(
            _session(
                "dayMarket",
                shift("2026-08-10T00:00:00Z"),
                shift("2026-08-10T08:00:00Z"),
            ),
            _session(
                "preMarket",
                shift("2026-08-10T08:00:00Z"),
                shift("2026-08-10T13:30:00Z"),
            ),
            _session(
                "regularMarket",
                shift("2026-08-10T13:30:00Z"),
                shift("2026-08-10T20:00:00Z"),
            ),
            _session(
                "afterMarket",
                shift("2026-08-10T20:00:00Z"),
                shift("2026-08-10T23:50:00Z"),
            ),
        ),
        fetched_at=CLOCK_NOW,
        provenance="calendar:US:2026-08-10:0",
    )


def _store(tmp_path):
    return CalendarLookaheadStoreV1(
        tmp_path / "stores" / "calendar_lookahead.sqlite3",
        clock=lambda: CLOCK_NOW,
    )


# --- typed day validation -------------------------------------------------


def test_lookahead_day_rejects_naive_overlapping_or_reversed_sessions():
    with pytest.raises(ValueError):
        LookaheadSessionV1(
            "dayMarket", datetime(2026, 8, 10), _utc("2026-08-10T08:00:00Z")
        )
    with pytest.raises(ValueError):
        _session("dayMarket", "2026-08-10T08:00:00Z", "2026-08-10T00:00:00Z")
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.US,
            "2026-08-10",
            "2026-08-10",
            (
                _session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
                _session("preMarket", "2026-08-10T07:00:00Z", "2026-08-10T13:30:00Z"),
            ),
            CLOCK_NOW,
            "calendar:US:2026-08-10:0",
        )


def test_lookahead_day_rejects_unknown_session_kind_and_bad_dates():
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.US,
            "2026-08-10",
            "2026-08-10",
            (
                _session(
                    "midnightMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"
                ),
            ),
            CLOCK_NOW,
            "calendar:US:2026-08-10:0",
        )
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.US,
            "2026-8-10",
            "2026-08-10",
            (_session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),),
            CLOCK_NOW,
            "calendar:US:2026-08-10:0",
        )
    # KR cannot carry a US-only dayMarket session.
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.KR,
            "2026-08-10",
            "2026-08-10",
            (_session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),),
            CLOCK_NOW,
            "calendar:KR:2026-08-10:0",
        )


def test_lookahead_day_requires_canonical_provenance_identity():
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.US,
            "2026-08-10",
            "2026-08-10",
            (_session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),),
            CLOCK_NOW,
            "made-up-provenance",
        )
    with pytest.raises(ValueError):
        LookaheadDayV1(
            Market.US,
            "2026-08-10",
            "2026-08-10",
            (_session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),),
            CLOCK_NOW,
            "calendar:KR:2026-08-10:0",  # market mismatch
        )


# --- store ---------------------------------------------------------------


def test_store_roundtrips_current_and_next_days(tmp_path):
    store = _store(tmp_path)
    current = _us_day_edt()
    following = _us_day_edt("2026-08-11", offset_days=1)
    store.replace_market_days(Market.US, (current, following))
    days = store.read_days(Market.US)
    assert [day.trade_date for day in days] == ["2026-08-10", "2026-08-11"]
    assert days[0] == current
    assert days[1] == following


def test_store_caps_at_two_days_and_requires_ascending_distinct_dates(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.replace_market_days(
            Market.US,
            (
                _us_day_edt(),
                _us_day_edt("2026-08-11", offset_days=1),
                _us_day_edt("2026-08-12", offset_days=2),
            ),
        )
    with pytest.raises(ValueError):
        store.replace_market_days(
            Market.US,
            (
                _us_day_edt("2026-08-11", offset_days=1),
                _us_day_edt(),
            ),
        )
    with pytest.raises(ValueError):
        store.replace_market_days(Market.US, (_us_day_edt(), _us_day_edt()))
    with pytest.raises(ValueError):
        store.replace_market_days(Market.US, ())


def test_store_is_market_isolated_and_replacement_prunes_stale_days(tmp_path):
    store = _store(tmp_path)
    store.replace_market_days(
        Market.US,
        (
            _us_day_edt(),
            _us_day_edt("2026-08-11", offset_days=1),
        ),
    )
    kr_day = LookaheadDayV1(
        Market.KR,
        "2026-08-10",
        "2026-08-10",
        (_session("regularMarket", "2026-08-10T00:00:00Z", "2026-08-10T06:30:00Z"),),
        CLOCK_NOW,
        "calendar:KR:2026-08-10:0",
    )
    store.replace_market_days(Market.KR, (kr_day,))
    assert [d.trade_date for d in store.read_days(Market.US)] == [
        "2026-08-10",
        "2026-08-11",
    ]
    assert [d.trade_date for d in store.read_days(Market.KR)] == ["2026-08-10"]
    # Rolling forward replaces the market's whole window.
    store.replace_market_days(
        Market.US,
        (
            _us_day_edt("2026-08-11", offset_days=1),
            _us_day_edt("2026-08-12", offset_days=2),
        ),
    )
    assert [d.trade_date for d in store.read_days(Market.US)] == [
        "2026-08-11",
        "2026-08-12",
    ]
    assert [d.trade_date for d in store.read_days(Market.KR)] == ["2026-08-10"]


def test_store_file_is_private_0600(tmp_path):
    import stat as stat_module

    store = _store(tmp_path)
    store.replace_market_days(Market.US, (_us_day_edt(),))
    mode = stat_module.S_IMODE(
        (tmp_path / "stores" / "calendar_lookahead.sqlite3").stat().st_mode
    )
    assert mode == 0o600


def test_corrupt_store_fails_closed_on_read(tmp_path):
    store = _store(tmp_path)
    store.replace_market_days(Market.US, (_us_day_edt(),))
    path = tmp_path / "stores" / "calendar_lookahead.sqlite3"
    path.write_bytes(b"garbage-not-a-database")
    with pytest.raises(CalendarLookaheadCorruptError):
        store.read_days(Market.US)


def test_read_of_absent_store_returns_no_days(tmp_path):
    assert _store(tmp_path).read_days(Market.US) == ()


# --- conflict against the current CalendarCacheSchema2 entry --------------


def _cache_day(trade_date="2026-08-10"):
    return CalendarDayCacheSchema2(
        trade_date=trade_date,
        sessions={
            "dayMarket": CalendarSessionCacheSchema2(
                "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"
            ),
            "preMarket": CalendarSessionCacheSchema2(
                "2026-08-10T08:00:00Z", "2026-08-10T13:30:00Z"
            ),
            "regularMarket": CalendarSessionCacheSchema2(
                "2026-08-10T13:30:00Z", "2026-08-10T20:00:00Z"
            ),
            "afterMarket": CalendarSessionCacheSchema2(
                "2026-08-10T20:00:00Z", "2026-08-10T23:50:00Z"
            ),
        },
        source_trade_date=trade_date,
        fetched_at="2026-08-10T00:00:00Z",
        schema_version=2,
        parser_version="toss-market-calendar-v1",
    )


def test_matching_current_cache_identity_passes_verification():
    verify_against_current_cache(_us_day_edt(), _cache_day())


def test_conflicting_current_cache_identity_fails_closed():
    day = _us_day_edt()
    mismatched = CalendarDayCacheSchema2(
        trade_date="2026-08-10",
        sessions={
            "dayMarket": CalendarSessionCacheSchema2(
                "2026-08-10T00:00:00Z", "2026-08-10T07:00:00Z"
            ),
        },
        source_trade_date="2026-08-10",
        fetched_at="2026-08-10T00:00:00Z",
        schema_version=2,
        parser_version="toss-market-calendar-v1",
    )
    with pytest.raises(CalendarLookaheadConflictError):
        verify_against_current_cache(day, mismatched)


def test_different_trade_date_or_absent_cache_entry_is_not_compared():
    verify_against_current_cache(_us_day_edt("2026-08-11", offset_days=1), _cache_day())
    verify_against_current_cache(_us_day_edt(), None)


# --- interval-membership gate (§13.4/§13.7) -------------------------------


def _judge(now_text, days=None):
    days = (_us_day_edt(),) if days is None else days
    return judge_lookahead_session(Market.US, days, _utc(now_text))


def test_us_day_market_is_open_at_0900_kst_on_the_trade_date():
    # 09:00 KST 2026-08-10 == 00:00Z: the Day Market of trade_date
    # 2026-08-10 begins on the previous New York evening (20:00 EDT 08-09).
    verdict = _judge("2026-08-10T00:00:00Z")
    assert verdict.open is True
    assert verdict.trade_date == "2026-08-10"
    assert verdict.session_kind == "dayMarket"


def test_membership_beats_new_york_local_date_before_ny_midnight():
    # 00:30Z: America/New_York local date is still 2026-08-09 (EDT −4), yet
    # the provider-declared interval of trade_date 2026-08-10 is open. A
    # local-date-equality admission gate would wrongly report CLOSED here.
    verdict = _judge("2026-08-10T00:30:00Z")
    assert verdict.open is True
    assert verdict.trade_date == "2026-08-10"


def test_1259_to_1300_kst_continuity_across_ny_midnight():
    # 12:59 KST == 03:59Z and 13:00 KST == 04:00Z (New York midnight under
    # EDT): the same Day Market stays open with the same trade_date — the
    # rollover instant is not a session boundary.
    before = _judge("2026-08-10T03:59:00Z")
    after = _judge("2026-08-10T04:00:00Z")
    assert before.open and after.open
    assert before.trade_date == after.trade_date == "2026-08-10"
    assert before.session_kind == after.session_kind == "dayMarket"


def test_day_to_pre_boundary_at_1700_kst_has_no_gap_or_overlap():
    # 17:00 KST == 08:00Z: dayMarket [ , 08:00Z) closes, preMarket
    # [08:00Z, ) opens at the same instant.
    end_of_day = _judge("2026-08-10T07:59:59Z")
    start_of_pre = _judge("2026-08-10T08:00:00Z")
    assert end_of_day.session_kind == "dayMarket"
    assert start_of_pre.session_kind == "preMarket"
    assert end_of_day.open and start_of_pre.open


def test_pre_to_regular_and_regular_to_after_boundaries():
    # 22:30 KST == 13:30Z, 05:00(+1d) KST == 20:00Z.
    assert _judge("2026-08-10T13:29:59Z").session_kind == "preMarket"
    assert _judge("2026-08-10T13:30:00Z").session_kind == "regularMarket"
    assert _judge("2026-08-10T19:59:59Z").session_kind == "regularMarket"
    assert _judge("2026-08-10T20:00:00Z").session_kind == "afterMarket"
    closed = _judge("2026-08-10T23:50:00Z")
    assert closed.open is False


def test_est_winter_fixture_follows_absolute_intervals_automatically():
    # EST (−5): the same session kinds sit one hour later in UTC. The gate
    # uses the provider's absolute intervals, so the offset change needs no
    # code path of its own.
    winter = LookaheadDayV1(
        Market.US,
        "2026-01-15",
        "2026-01-15",
        (
            _session("dayMarket", "2026-01-15T01:00:00Z", "2026-01-15T09:00:00Z"),
            _session("preMarket", "2026-01-15T09:00:00Z", "2026-01-15T14:30:00Z"),
            _session("regularMarket", "2026-01-15T14:30:00Z", "2026-01-15T21:00:00Z"),
        ),
        CLOCK_NOW,
        "calendar:US:2026-01-15:0",
    )
    open_verdict = judge_lookahead_session(
        Market.US, (winter,), _utc("2026-01-15T01:00:00Z")
    )
    assert open_verdict.open and open_verdict.session_kind == "dayMarket"
    closed_verdict = judge_lookahead_session(
        Market.US, (winter,), _utc("2026-01-15T00:30:00Z")
    )
    assert closed_verdict.open is False


def test_next_day_session_is_selected_from_the_lookahead_window():
    days = (_us_day_edt(), _us_day_edt("2026-08-11", offset_days=1))
    verdict = judge_lookahead_session(Market.US, days, _utc("2026-08-11T00:30:00Z"))
    assert verdict.open is True
    assert verdict.trade_date == "2026-08-11"


def test_weekend_or_holiday_absence_is_closed_with_wire_zero_reason():
    verdict = judge_lookahead_session(Market.US, (), _utc("2026-08-08T01:00:00Z"))
    assert verdict.open is False
    assert verdict.reason == "lookahead_absent"


def test_exhausted_window_fails_closed_as_stale():
    verdict = _judge("2026-08-11T05:00:00Z")  # past every stored interval
    assert verdict.open is False
    assert verdict.reason == "lookahead_exhausted"


def test_overlapping_days_conflict_fails_closed():
    days = (_us_day_edt(), _us_day_edt("2026-08-11"))  # same intervals, two dates
    verdict = judge_lookahead_session(Market.US, days, _utc("2026-08-10T01:00:00Z"))
    assert verdict.open is False
    assert verdict.reason == "session_conflict"


def test_wrong_market_day_in_input_fails_closed():
    kr_day = LookaheadDayV1(
        Market.KR,
        "2026-08-10",
        "2026-08-10",
        (_session("regularMarket", "2026-08-10T00:00:00Z", "2026-08-10T06:30:00Z"),),
        CLOCK_NOW,
        "calendar:KR:2026-08-10:0",
    )
    verdict = judge_lookahead_session(
        Market.US, (kr_day,), _utc("2026-08-10T01:00:00Z")
    )
    assert verdict.open is False
    assert verdict.reason == "market_mismatch"


def test_naive_now_is_rejected():
    with pytest.raises(ValueError):
        judge_lookahead_session(Market.US, (_us_day_edt(),), datetime(2026, 8, 10))
