"""Stage 3 M2: Sunday five-minute bootstrap of the regular-session history.

A closed Sunday sequence of production cadence ticks — the identical
``run_production_cadence_once`` at the 300-second policy grid — must
progressively accumulate the official regular-session history to twenty KR and
twenty US regular dates within at most 35 ticks (175 minutes), spending at
most one historical calendar wire per market per tick and recording only the
exact official fixture facts (weekends and holidays included, never a weekday
guess). The following Monday's open tick then serves trades twenty real
descending session dates from that durable history alone.
"""

import dataclasses
from datetime import date, datetime, timedelta, timezone

from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.production_cadence import run_production_cadence_once
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.regular_session_history import (
    HistorySessionDatesV1,
    RegularSessionHistoryStore,
)
from tokmo.values.session_gate import Market

POLICY = "cadence-300s-v1"

# Sunday 2026-08-02, 05:00Z: KR local 14:00 KST Sunday, US local 01:00 EDT
# Sunday — both markets share the same closed local date for the whole run.
SUNDAY_BASE = datetime(2026, 8, 2, 5, 0, tzinfo=timezone.utc)
MONDAY_OPEN = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)  # KR regular open

# One mid-window holiday per market so the bootstrap walk itself must cross a
# weekday that is officially no_regular (never inferred from the weekday).
KR_HOLIDAYS = frozenset({date(2026, 7, 15)})
US_HOLIDAYS = frozenset({date(2026, 7, 24)})
FIXTURE_FIRST_DAY = date(2026, 6, 20)
FIXTURE_LAST_DAY = date(2026, 8, 7)


def official_regular(market: Market, day: date) -> bool:
    holidays = KR_HOLIDAYS if market is Market.KR else US_HOLIDAYS
    return day.weekday() < 5 and day not in holidays


def official_day_entry(market: Market, day: date):
    if not official_regular(market, day):
        return {}
    if market is Market.KR:  # 09:00-15:30 KST
        return {
            "regular_start": f"{day.isoformat()}T00:00:00Z",
            "regular_end": f"{day.isoformat()}T06:30:00Z",
        }
    return {  # 09:30-16:00 New York, EDT all fixture dates
        "regular_start": f"{day.isoformat()}T13:30:00Z",
        "regular_end": f"{day.isoformat()}T20:00:00Z",
    }


def bootstrap_fixture():
    days = {
        market: {
            day.isoformat(): official_day_entry(market, day)
            for day in (
                FIXTURE_FIRST_DAY + timedelta(days=offset)
                for offset in range(
                    (FIXTURE_LAST_DAY - FIXTURE_FIRST_DAY).days + 1
                )
            )
        }
        for market in Market
    }
    return {
        "token": "bootstrap-fixture-bearer",
        "calendar": {
            "KR": {"days": days[Market.KR]},
            "US": {"days": days[Market.US]},
        },
        "accounts": [{
            "seed": "77", "label": "brokerage",
            "holdings": [
                {"symbol": "005930", "quantity": "5", "avg_price": "70000",
                 "currency": "KRW", "market_raw": "KR"},
                {"symbol": "AAPL", "quantity": "2", "avg_price": "180",
                 "currency": "USD", "market_raw": "US"},
            ],
        }],
        "quotes": {
            "005930": {"price": "70100", "currency": "KRW"},
            "AAPL": {"price": "181.25", "currency": "USD"},
        },
        "orders": {},
    }


def run_tick(root, slot, fixture, *, session_dates=None):
    observed = slot + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: observed,
    )
    if session_dates is not None:
        deps = dataclasses.replace(deps, regular_session_dates=session_dates)
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=slot,
        observed_now_utc=observed, deps=deps,
    )
    return result, probe


def expected_sessions(market: Market, anchor: date, count: int):
    found = []
    cursor = anchor
    while len(found) < count:
        if official_regular(market, cursor):
            found.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(found)


def test_sunday_bootstrap_reaches_twenty_sessions_then_monday_trades_run(tmp_path):
    root = tmp_path / "shadow-root"
    fixture = bootstrap_fixture()
    history = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=lambda: SUNDAY_BASE,
    )
    sunday_anchor = date(2026, 8, 2)

    ticks_used = None
    for index in range(35):
        slot = SUNDAY_BASE + timedelta(seconds=300 * index)
        result, _probe = run_tick(root, slot, fixture)
        assert result.outcome.value == "SKIPPED_CLOSED", (index, result.reason)

        report = OpsKernelStore(
            root / "ops" / "ops_kernel.sqlite3"
        ).read_cadence_tick_report(POLICY, slot)
        started = report["operation_started"]
        # bounded spend: at most one historical date per market per tick,
        # plus the lazily admitted TOKEN; nothing else on a closed Sunday
        assert started.get("CALENDAR_DAY_KR", 0) <= 1
        assert started.get("CALENDAR_DAY_US", 0) <= 1
        assert started.get("TOKEN", 0) <= 1
        assert set(started) <= {"TOKEN", "CALENDAR_DAY_KR", "CALENDAR_DAY_US"}

        if (history.next_backfill_target(Market.KR, sunday_anchor) is None
                and history.next_backfill_target(Market.US, sunday_anchor) is None):
            ticks_used = index + 1
            break

    assert ticks_used is not None, "bootstrap must complete within 35 ticks"
    assert ticks_used <= 35
    # deterministic evidence: one cold tick (current-date refresh consumes
    # the calendar allocation) plus thirty backfill dates per market
    # (2026-08-01 back to 2026-07-03, weekends and one holiday included)
    # at one date per market per tick = 31 ticks, 155 fixture minutes.
    assert ticks_used == 31

    # every stored row is the exact official fixture fact — never a guess
    for market in Market:
        walked = 0
        cursor = sunday_anchor
        regular_found = 0
        while regular_found < 20 and walked < 35:
            row = history.read(market, cursor)
            assert row is not None, (market, cursor)
            expected = "regular" if official_regular(market, cursor) else "no_regular"
            assert row["session"] == expected, (market, cursor)
            if row["session"] == "regular":
                regular_found += 1
            walked += 1
            cursor -= timedelta(days=1)
        assert regular_found == 20, market

    # a completed bootstrap makes the next closed tick wire-free again
    idle_slot = SUNDAY_BASE + timedelta(seconds=300 * ticks_used)
    _result, _probe = run_tick(root, idle_slot, fixture)
    idle_report = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3"
    ).read_cadence_tick_report(POLICY, idle_slot)
    assert idle_report["total_started"] == 0

    # Monday: the open tick's trades read twenty real dates from history only
    reader = HistorySessionDatesV1(history)
    monday_result, _probe = run_tick(
        root, MONDAY_OPEN, fixture, session_dates=reader,
    )
    assert monday_result.outcome.value == "COMPLETE", monday_result.reason
    stages = {status.stage: status for status in monday_result.stages}
    assert stages["trades"].status == "ok"
    assert monday_result.open_markets == ("KR",)

    kr_dates = reader.regular_session_dates(Market.KR, date(2026, 8, 3), 20)
    assert kr_dates == expected_sessions(Market.KR, date(2026, 8, 3), 20)
    us_dates = reader.regular_session_dates(Market.US, date(2026, 8, 2), 20)
    assert us_dates == expected_sessions(Market.US, date(2026, 8, 2), 20)
