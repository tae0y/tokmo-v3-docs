from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokmo.facts.toss_reads import (
    TossReadOperation,
    build_read_request,
    parse_read_response,
)
from tokmo.programs.macro_canary_adapter import (
    build_macro_bundle,
    compute_firing_rate_status,
    merge_macro_pages,
    normalize_macro_pages,
    resolve_completed_session,
    validate_page_boundary,
)

FIX = Path(__file__).parent / "fixtures" / "macro_canary"
NOW = datetime(2026, 8, 9, 8, tzinfo=UTC)
TOKEN = "fixture-token"


def parsed(name, operation, **kwargs):
    req = build_read_request(operation, token=TOKEN, **kwargs)
    out = parse_read_response(req=req, body=(FIX / name).read_bytes(), fetched_at=NOW)
    assert out.code == "success"
    return out.value


def test_page_runtime_types_normalization_and_production_roundtrip():
    page = parsed(
        "kospi_page_current_v1.json",
        TossReadOperation.INDICATOR_CANDLES,
        symbol="KOSPI",
        interval="1d",
        count=200,
    )
    assert isinstance(page.next_before, datetime) and page.next_before.tzinfo
    residual = build_read_request(
        TossReadOperation.INDICATOR_CANDLES,
        token=TOKEN,
        symbol="KOSPI",
        interval="1d",
        count=200,
        before=page.next_before,
    )
    assert residual.before is page.next_before
    rows = normalize_macro_pages("KOSPI", (page,), NOW, date(2026, 8, 7))
    assert [r.session_date for r in rows] == [date(2026, 8, 6), date(2026, 8, 7)]


def test_investor_cursor_exact_iso_roundtrip():
    page = parsed(
        "investor_page_current_v1.json",
        TossReadOperation.INDICATOR_INVESTOR_TRADING,
        symbol="KOSPI",
        interval="1d",
        count=100,
    )
    assert type(page.next_until) is date
    req = build_read_request(
        TossReadOperation.INDICATOR_INVESTOR_TRADING,
        token=TOKEN,
        symbol="KOSPI",
        interval="1d",
        count=100,
        until=page.next_until.isoformat(),
    )
    assert date.fromisoformat(req.until) == page.next_until


def test_duplicate_overlap_and_bad_cursor_reject():
    current = parsed(
        "kospi_page_current_v1.json",
        TossReadOperation.INDICATOR_CANDLES,
        symbol="KOSPI",
        interval="1d",
        count=200,
    )
    for name in ("page_duplicate_v1.json", "page_bad_cursor_v1.json"):
        page = parsed(
            name,
            TossReadOperation.INDICATOR_CANDLES,
            symbol="KOSPI",
            interval="1d",
            count=200,
        )
        with pytest.raises(ValueError):
            normalize_macro_pages("KOSPI", (page,), NOW, date(2026, 8, 7))
    overlap = parsed(
        "page_overlap_v1.json",
        TossReadOperation.INDICATOR_CANDLES,
        symbol="KOSPI",
        interval="1d",
        count=200,
    )
    with pytest.raises(ValueError):
        validate_page_boundary("KOSPI", current, overlap)


def test_merge_bundle_calendar_and_rate_labels():
    k = [
        parsed(
            n,
            TossReadOperation.INDICATOR_CANDLES,
            symbol="KOSPI",
            interval="1d",
            count=200,
        )
        for n in ("kospi_page_current_v1.json", "kospi_page_older_v1.json")
    ]
    assert len(merge_macro_pages("KOSPI", tuple(k), NOW, date(2026, 8, 7))) == 4
    bundle, coverage = build_macro_bundle(
        tuple(k),
        (),
        (),
        fetched_at=NOW,
        completed_session_date=date(2026, 8, 7),
        as_of=NOW,
        calendar_dates=None,
    )
    assert bundle is None and coverage == "COVERAGE_CONTINUITY_UNPROVEN"
    assert compute_firing_rate_status(1, 0, normative=True)[0] == "FIRING_RATE_UNPROVEN"
    assert compute_firing_rate_status(20, 1, normative=False) == (
        "SHADOW_FORWARD_FIRING_RATE",
        "0.05",
    )


def test_unequal_4_2_4_pages_build_bundle_with_continuity_separate():
    from datetime import timedelta

    from tokmo.programs.macro_canary_adapter import _G1B_LIFECYCLE_SPEC
    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    from tokmo.values.signal_program_lifecycle import (
        ProgramLifecycleStateV1,
        ProgramLifecycleStatusV1,
    )

    k = tuple(
        parsed(
            n,
            TossReadOperation.INDICATOR_CANDLES,
            symbol="KOSPI",
            interval="1d",
            count=200,
        )
        for n in ("kospi_page_current_v1.json", "kospi_page_older_v1.json")
    )
    i = (
        parsed(
            "investor_page_current_v1.json",
            TossReadOperation.INDICATOR_INVESTOR_TRADING,
            symbol="KOSPI",
            interval="1d",
            count=100,
        ),
    )
    pc = parsed(
        "proxy_page_current_v1.json",
        TossReadOperation.STOCK_CANDLES,
        symbol="261240",
        interval="1d",
        count=200,
    )
    po = parsed(
        "proxy_page_older_v1.json",
        TossReadOperation.STOCK_CANDLES,
        symbol="261240",
        interval="1d",
        count=200,
    )
    extra = replace(
        po.candles[0], timestamp=po.candles[0].timestamp - timedelta(days=1)
    )
    po = replace(po, candles=po.candles + (extra,))
    bundle, continuity = build_macro_bundle(
        k,
        i,
        (pc, po),
        fetched_at=NOW,
        completed_session_date=date(2026, 8, 7),
        as_of=NOW,
        calendar_dates={date(2026, 8, 6), date(2026, 8, 7)},
    )
    assert (
        len(bundle.kospi_candles),
        len(bundle.investor_records),
        len(bundle.proxy_candles),
    ) == (4, 2, 4)
    assert continuity == "COVERAGE_CONTINUITY_UNPROVEN"
    pack = evaluate_macro_divergence(
        bundle,
        lifecycle=ProgramLifecycleStateV1(
            _G1B_LIFECYCLE_SPEC, ProgramLifecycleStatusV1.ACTIVE, None, 0, 0, NOW
        ),
    )
    assert pack.pack_digest and _G1B_LIFECYCLE_SPEC.spec_digest != "0" * 64


def test_completed_session_uses_both_read_only_authorities():
    class H:
        def read(self, market, day):
            return {"session": "regular", "local_date": day}

    class C:
        def read(self):
            session = type("S", (), {"end": "2026-08-09T07:00:00Z"})()
            day = type(
                "D",
                (),
                {"trade_date": "2026-08-09", "sessions": {"regularMarket": session}},
            )()
            return type("Cache", (), {"kr": day})()

    assert resolve_completed_session(
        H(), C(), datetime(2026, 8, 9, 8, tzinfo=UTC)
    ) == date(2026, 8, 9)
    with pytest.raises(ValueError):
        resolve_completed_session(H(), C(), datetime(2026, 8, 9, 6, tzinfo=UTC))


def test_completed_session_roundtrip_through_production_stores(tmp_path):
    from tokmo.records.calendar_cache import CalendarCacheStore
    from tokmo.records.regular_session_history import RegularSessionHistoryStore
    from tokmo.values.session_gate import Market
    from tokmo.values.toss_market_calendar import (
        CalendarSession,
        IntegratedHour,
        KrMarketDay,
    )

    now = datetime(2026, 8, 9, 8, tzinfo=UTC)
    day = date(2026, 8, 9)
    history = RegularSessionHistoryStore(
        tmp_path / "regular.sqlite3", clock=lambda: now
    )
    history._insert(Market.KR, day, "regular", "calendar:KR:2026-08-09:0")
    cache = CalendarCacheStore(tmp_path / "calendar.json", clock=lambda: now)
    cache.write(
        KrMarketDay(
            day,
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 9, 0, tzinfo=UTC),
                    datetime(2026, 8, 9, 7, tzinfo=UTC),
                )
            ),
        ),
        None,
    )
    before = (history.path.read_bytes(), cache.path.read_bytes())
    assert resolve_completed_session(history, cache, now) == day
    assert before == (history.path.read_bytes(), cache.path.read_bytes())


def test_session02_stale_contradictory_and_unsafe_authorities_refuse(tmp_path):
    class H:
        def __init__(self, session="regular"):
            self.session = session

        def read(self, market, day):
            return {"session": self.session, "local_date": day}

    class C:
        def __init__(self, trade="2026-08-08"):
            self.trade = trade

        def read(self):
            session = type("S", (), {"end": "2026-08-09T07:00:00Z"})()
            day = type(
                "D",
                (),
                {"trade_date": self.trade, "sessions": {"regularMarket": session}},
            )()
            return type("Cache", (), {"kr": day})()

    now = datetime(2026, 8, 9, 8, tzinfo=UTC)
    with pytest.raises(ValueError):
        resolve_completed_session(H(), C(), now)
    with pytest.raises(ValueError):
        resolve_completed_session(H("no_regular"), C("2026-08-09"), now)
    unsafe = tmp_path / "calendar.json"
    unsafe.symlink_to(tmp_path / "missing")
    from tokmo.records.calendar_cache import CalendarCacheStore

    with pytest.raises(ValueError):
        resolve_completed_session(
            H(), CalendarCacheStore(unsafe, clock=lambda: now), now
        )
    from tokmo.values.toss_market_calendar import (
        CalendarSession,
        IntegratedHour,
        KrMarketDay,
    )

    mode_path = tmp_path / "mode.json"
    mode_store = CalendarCacheStore(mode_path, clock=lambda: now)
    mode_store.write(
        KrMarketDay(
            date(2026, 8, 9),
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 9, 0, tzinfo=UTC),
                    datetime(2026, 8, 9, 7, tzinfo=UTC),
                )
            ),
        ),
        None,
    )
    mode_path.chmod(0o644)
    with pytest.raises(ValueError):
        resolve_completed_session(H(), mode_store, now)


def test_rate01_fetched_now_pages_reject_historical_as_of():
    from tokmo.programs.macro_canary_adapter import _G1B_LIFECYCLE_SPEC
    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    from tokmo.values.signal_program_lifecycle import (
        ProgramLifecycleStateV1,
        ProgramLifecycleStatusV1,
    )

    k = (
        parsed(
            "kospi_page_current_v1.json",
            TossReadOperation.INDICATOR_CANDLES,
            symbol="KOSPI",
            interval="1d",
            count=200,
        ),
    )
    i = (
        parsed(
            "investor_page_current_v1.json",
            TossReadOperation.INDICATOR_INVESTOR_TRADING,
            symbol="KOSPI",
            interval="1d",
            count=100,
        ),
    )
    p = (
        parsed(
            "proxy_page_current_v1.json",
            TossReadOperation.STOCK_CANDLES,
            symbol="261240",
            interval="1d",
            count=200,
        ),
    )
    historical = datetime(2026, 6, 17, 8, tzinfo=UTC)
    bundle, _ = build_macro_bundle(
        k,
        i,
        p,
        fetched_at=NOW,
        completed_session_date=date(2026, 8, 7),
        as_of=historical,
        calendar_dates=None,
    )
    historical_spec = replace(
        _G1B_LIFECYCLE_SPEC, activated_at=datetime(2026, 3, 1, tzinfo=UTC)
    )
    with pytest.raises(ValueError, match="future leakage"):
        evaluate_macro_divergence(
            bundle,
            lifecycle=ProgramLifecycleStateV1(
                historical_spec, ProgramLifecycleStatusV1.ACTIVE, None, 0, 0, historical
            ),
        )
