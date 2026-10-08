"""G2.5 call-site wiring integration tests.

Verifies that the 300s production cadence's success paths now write to the
CalendarLookaheadStore and AccountNativeRefStore through the G2 writer seams,
without changing existing tick outcomes or making duplicate provider reads.

All tests use fixture deps (cadence_fixture_deps) — zero live provider I/O.
The cadence runs on a tmp_path root, and the stores are created under that
root's standard paths.
"""

from datetime import datetime, timezone

import pytest

from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.production_cadence import run_production_cadence_once
from tokmo.records.account_native_refs import AccountNativeRefStoreV1
from tokmo.records.calendar_lookahead import CalendarLookaheadStoreV1
from tokmo.values.session_gate import Market

SLOT = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
OBSERVED = datetime(2026, 8, 3, 14, 0, 1, tzinfo=timezone.utc)

# A valid Toss account scope pattern: toss-acct-<24 hex chars>
TOSS_SCOPE = "toss-acct-" + "a" * 24


def _open_cadence_fixture():
    """Fixture dict that makes both KR and US markets OPEN at the slot."""
    return {
        "token": "fixture-bearer",
        "calendar": {
            "KR": {
                "regular_start": "2026-08-03T00:00:00Z",
                "regular_end": "2026-08-03T06:30:00Z",
            },
            "US": {
                "regular_start": "2026-08-03T13:30:00Z",
                "regular_end": "2026-08-03T20:00:00Z",
            },
        },
        "accounts": [{
            "seed": "9", "holdings": [
                {"symbol": "AAPL", "quantity": "1", "avg_price": "100",
                 "currency": "USD", "market_raw": "US"},
            ],
        }],
        "quotes": {"AAPL": {"price": "101", "currency": "USD"}},
    }


def _assert_lookahead_mutation0(store, market):
    """Assert the lookahead store has zero days for the given market."""
    assert len(store.read_days(market)) == 0


# ── Calendar lookahead call-site wiring ──────────────────────────────────────


def test_calendar_success_writes_lookahead_today_and_next(tmp_path):
    """A successful 300s calendar refresh writes today + next business day
    into CalendarLookaheadStoreV1 through the writer seam."""
    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    fixture["calendar"]["US"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: OBSERVED,
    )
    kr_days = lookahead_store.read_days(Market.KR)
    us_days = lookahead_store.read_days(Market.US)
    assert len(kr_days) >= 1
    assert len(us_days) >= 1
    assert kr_days[0].trade_date == "2026-08-03"
    assert us_days[0].trade_date == "2026-08-03"
    if len(kr_days) == 2:
        assert kr_days[1].trade_date == "2026-08-04"
    if len(us_days) == 2:
        assert us_days[1].trade_date == "2026-08-04"


def test_calendar_success_lookahead_provenance_is_receipt_identity(tmp_path):
    """Lookahead days carry the calendar wire receipt identity as provenance."""
    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: OBSERVED,
    )
    kr_days = lookahead_store.read_days(Market.KR)
    assert kr_days[0].provenance.startswith("calendar:KR:")


def test_calendar_failed_no_lookahead_mutation(tmp_path):
    """When calendar refresh fails (provider error), the lookahead store is
    not mutated — zero writes."""
    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["fail"] = True
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    # Calendar failure means not COMPLETE
    assert result.outcome.value != "COMPLETE"

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: OBSERVED,
    )
    kr_days = lookahead_store.read_days(Market.KR)
    assert len(kr_days) == 0  # no mutation


# ── M1: cache conflict / cache update failed must NOT write lookahead ────────
#
# These tests unit-test the _write_lookahead_after_refresh gate directly,
# with a pre-populated cache so cache_entry is non-None (the integration
# tests above pass trivially when cache_entry is None).  The gate must be
# MarketRefreshState.SUCCESS ONLY — not _CALENDAR_WIRE_SUCCEEDED which
# erroneously includes CACHE_CONFLICT / CACHE_UPDATE_FAILED.

def _kr_calendar_response():
    """A minimal valid KR calendar response for lookahead writer input."""
    from tokmo.values.toss_market_calendar import (
        CalendarSession, IntegratedHour, KrMarketCalendarResponse, KrMarketDay,
    )
    from datetime import date as date_cls
    return KrMarketCalendarResponse(
        today=KrMarketDay(
            date_cls(2026, 8, 3),
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 3, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 3, 6, 30, tzinfo=timezone.utc),
                ),
            ),
        ),
        previous_business_day=KrMarketDay(
            date_cls(2026, 8, 1),
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 1, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 1, 6, 30, tzinfo=timezone.utc),
                ),
            ),
        ),
        next_business_day=KrMarketDay(
            date_cls(2026, 8, 4),
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 4, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 4, 6, 30, tzinfo=timezone.utc),
                ),
            ),
        ),
    )


def _us_calendar_response():
    """A minimal valid US calendar response for lookahead writer input."""
    from tokmo.values.toss_market_calendar import (
        CalendarSession, UsMarketCalendarResponse, UsMarketDay,
    )
    from datetime import date as date_cls
    return UsMarketCalendarResponse(
        today=UsMarketDay(
            date_cls(2026, 8, 3),
            regular_market=CalendarSession(
                datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
            ),
        ),
        previous_business_day=UsMarketDay(
            date_cls(2026, 8, 1),
            regular_market=CalendarSession(
                datetime(2026, 8, 1, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 1, 20, 0, tzinfo=timezone.utc),
            ),
        ),
        next_business_day=UsMarketDay(
            date_cls(2026, 8, 4),
            regular_market=CalendarSession(
                datetime(2026, 8, 4, 13, 30, tzinfo=timezone.utc),
                datetime(2026, 8, 4, 20, 0, tzinfo=timezone.utc),
            ),
        ),
    )


def _cache_entry_kr():
    """A valid KR cache entry matching the calendar response's today."""
    from tokmo.records.calendar_cache import (
        CalendarDayCacheSchema2, CalendarSessionCacheSchema2,
    )
    return CalendarDayCacheSchema2(
        trade_date="2026-08-03",
        sessions={"regularMarket": CalendarSessionCacheSchema2(
            start="2026-08-03T00:00:00Z", end="2026-08-03T06:30:00Z",
        )},
        source_trade_date="2026-08-03",
        fetched_at="2026-08-03T00:00:00Z",
        schema_version=2,
        parser_version="toss-market-calendar-v1",
    )


def _cache_entry_us():
    """A valid US cache entry matching the calendar response's today."""
    from tokmo.records.calendar_cache import (
        CalendarDayCacheSchema2, CalendarSessionCacheSchema2,
    )
    return CalendarDayCacheSchema2(
        trade_date="2026-08-03",
        sessions={"regularMarket": CalendarSessionCacheSchema2(
            start="2026-08-03T13:30:00Z", end="2026-08-03T20:00:00Z",
        )},
        source_trade_date="2026-08-03",
        fetched_at="2026-08-03T00:00:00Z",
        schema_version=2,
        parser_version="toss-market-calendar-v1",
    )


def _make_cache_store(tmp_path, kr_entry=None, us_entry=None):
    """Create a CalendarCacheStore pre-populated with the given entries."""
    from tokmo.records.calendar_cache import (
        CalendarCacheSchema2, CalendarCacheStore,
        _cache_bytes, CALENDAR_CACHE_MAX_BYTES,
    )
    from tokmo.records.atomic_json import write_atomic_json
    path = tmp_path / "ops" / "calendar_cache.json"
    store = CalendarCacheStore(path, clock=lambda: OBSERVED)
    if kr_entry is not None or us_entry is not None:
        schema = CalendarCacheSchema2(
            schema_version=2,
            kr=kr_entry,
            us=us_entry,
            updated_at="2026-08-03T00:00:00Z",
        )
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        write_atomic_json(path, _cache_bytes(schema), max_bytes=CALENDAR_CACHE_MAX_BYTES)
    return store


class _FakeDeps:
    """Minimal deps stand-in with just .clock."""
    def __init__(self, clock):
        self.clock = clock


def _make_receipt(market, date_iso):
    """Create a CalendarStartReceiptV1 with a valid native_identity."""
    from tokmo.values.ops_kernel import CalendarStartReceiptV1
    from datetime import date as date_cls
    return CalendarStartReceiptV1(
        policy_version="cadence-300s-v1",
        scheduled_for_utc=SLOT,
        market=market,
        market_local_date=date_cls.fromisoformat(date_iso),
        attempt_ordinal=0,
        native_identity=f"calendar:{market.value}:{date_iso}:0",
        state="STARTED",
    )


def test_cache_conflict_does_not_write_lookahead(tmp_path, monkeypatch):
    """M1: When refresh yields CACHE_CONFLICT for a market, the lookahead
    writer must not be called. The gate must be SUCCESS ONLY.

    This test pre-populates the cache so cache_entry is non-None (the
    condition under which the bug manifests), then verifies the lookahead
    store is mutation0 for the conflict market."""
    from tokmo.programs import production_cadence as pc_module
    from tokmo.programs.calendar_refresh import MarketRefreshResult, MarketRefreshState

    cache_store = _make_cache_store(tmp_path, kr_entry=_cache_entry_kr())

    # Track writer calls
    writer_calls = []
    real_writer = pc_module.write_lookahead_from_calendar

    def spy_writer(**kwargs):
        writer_calls.append(kwargs.get("market"))
        return real_writer(**kwargs)

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", spy_writer)

    lookahead_path = tmp_path / "ops" / "calendar_lookahead.sqlite3"
    lookahead_store = CalendarLookaheadStoreV1(lookahead_path, clock=lambda: OBSERVED)

    kr_result = MarketRefreshResult(
        Market.KR, datetime(2026, 8, 3, tzinfo=timezone.utc).date(),
        MarketRefreshState.CACHE_CONFLICT, False,
        receipt=_make_receipt(Market.KR, "2026-08-03"),
    )

    pc_module._write_lookahead_after_refresh(
        root=tmp_path,
        results={Market.KR: kr_result},
        receipts={Market.KR: _make_receipt(Market.KR, "2026-08-03")},
        responses={Market.KR: _kr_calendar_response()},
        cache_store=cache_store,
        deps=_FakeDeps(lambda: OBSERVED),
    )

    # CACHE_CONFLICT → writer must NOT be called
    assert Market.KR not in writer_calls
    assert len(lookahead_store.read_days(Market.KR)) == 0


def test_cache_update_failed_does_not_write_lookahead(tmp_path, monkeypatch):
    """M1: When refresh yields CACHE_UPDATE_FAILED for a market, the lookahead
    writer must not be called."""
    from tokmo.programs import production_cadence as pc_module
    from tokmo.programs.calendar_refresh import MarketRefreshResult, MarketRefreshState

    cache_store = _make_cache_store(tmp_path, us_entry=_cache_entry_us())

    writer_calls = []
    real_writer = pc_module.write_lookahead_from_calendar

    def spy_writer(**kwargs):
        writer_calls.append(kwargs.get("market"))
        return real_writer(**kwargs)

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", spy_writer)

    lookahead_store = CalendarLookaheadStoreV1(
        tmp_path / "ops" / "calendar_lookahead.sqlite3", clock=lambda: OBSERVED,
    )

    us_result = MarketRefreshResult(
        Market.US, datetime(2026, 8, 3, tzinfo=timezone.utc).date(),
        MarketRefreshState.CACHE_UPDATE_FAILED, False,
        receipt=_make_receipt(Market.US, "2026-08-03"),
    )

    pc_module._write_lookahead_after_refresh(
        root=tmp_path,
        results={Market.US: us_result},
        receipts={Market.US: _make_receipt(Market.US, "2026-08-03")},
        responses={Market.US: _us_calendar_response()},
        cache_store=cache_store,
        deps=_FakeDeps(lambda: OBSERVED),
    )

    assert Market.US not in writer_calls
    assert len(lookahead_store.read_days(Market.US)) == 0


def test_lookahead_gate_per_market_independence(tmp_path, monkeypatch):
    """M1/m5: KR failure (CACHE_CONFLICT) does not suppress US success.
    The lookahead writer must be called for SUCCESS markets only."""
    from tokmo.programs import production_cadence as pc_module
    from tokmo.programs.calendar_refresh import MarketRefreshResult, MarketRefreshState

    cache_store = _make_cache_store(
        tmp_path, kr_entry=_cache_entry_kr(), us_entry=_cache_entry_us(),
    )

    writer_calls = []
    real_writer = pc_module.write_lookahead_from_calendar

    def spy_writer(**kwargs):
        writer_calls.append(kwargs.get("market"))
        return real_writer(**kwargs)

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", spy_writer)

    lookahead_store = CalendarLookaheadStoreV1(
        tmp_path / "ops" / "calendar_lookahead.sqlite3", clock=lambda: OBSERVED,
    )

    local_date = datetime(2026, 8, 3, tzinfo=timezone.utc).date()
    kr_result = MarketRefreshResult(
        Market.KR, local_date,
        MarketRefreshState.CACHE_CONFLICT, False,
        receipt=_make_receipt(Market.KR, "2026-08-03"),
    )
    us_result = MarketRefreshResult(
        Market.US, local_date,
        MarketRefreshState.SUCCESS, True,
        receipt=_make_receipt(Market.US, "2026-08-03"),
    )

    pc_module._write_lookahead_after_refresh(
        root=tmp_path,
        results={Market.KR: kr_result, Market.US: us_result},
        receipts={
            Market.KR: _make_receipt(Market.KR, "2026-08-03"),
            Market.US: _make_receipt(Market.US, "2026-08-03"),
        },
        responses={
            Market.KR: _kr_calendar_response(),
            Market.US: _us_calendar_response(),
        },
        cache_store=cache_store,
        deps=_FakeDeps(lambda: OBSERVED),
    )

    # Only US (SUCCESS) should be written; KR (CACHE_CONFLICT) must not
    assert Market.US in writer_calls
    assert Market.KR not in writer_calls
    assert len(lookahead_store.read_days(Market.US)) >= 1
    assert len(lookahead_store.read_days(Market.KR)) == 0


# ── M3: real monkeypatch-injected writer-failure tests ───────────────────────


def test_lookahead_writer_exception_does_not_change_cadence_outcome(
    tmp_path, monkeypatch,
):
    """M3: If the lookahead writer raises an actual injected exception, the
    cadence tick outcome must be unchanged AND the store must be mutation0.
    The stage evidence must show the writer was attempted and skipped."""
    from tokmo.programs import production_cadence as pc_module

    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    fixture["calendar"]["US"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )

    # Inject a documented evidence-writer exception. Unexpected programmer
    # exceptions intentionally propagate instead of being hidden.
    def boom(**kwargs):
        from tokmo.programs.calendar_lookahead_writer import LookaheadWriterError
        raise LookaheadWriterError("injected lookahead writer failure")

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", boom)

    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: OBSERVED,
    )
    # The injected failure means no days were committed
    assert len(lookahead_store.read_days(Market.KR)) == 0
    assert len(lookahead_store.read_days(Market.US)) == 0

    # Observable stage evidence: calendar stage must still show ok
    calendar_stage = next(
        s for s in result.stages if s.stage == "calendar"
    )
    assert calendar_stage.status == "ok"


def test_native_ref_writer_exception_does_not_change_cadence_outcome(
    tmp_path, monkeypatch,
):
    """M3: If the native ref writer raises an actual injected exception, the
    cadence tick outcome must be unchanged AND the ref store must be mutation0."""
    from tokmo.programs import production_cadence as pc_module

    fixture = _open_cadence_fixture()
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )

    def boom(**kwargs):
        from tokmo.programs.account_native_ref_writer import NativeRefWriterError
        raise NativeRefWriterError("injected native ref writer failure")

    monkeypatch.setattr(pc_module, "write_native_refs_from_accounts", boom)

    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    ref_store = AccountNativeRefStoreV1(
        root / "ops" / "account_native_refs.sqlite3",
        clock=lambda: OBSERVED,
    )
    # The injected failure means no refs were committed
    mapping = ref_store.read_refs()
    assert len(list(mapping.scopes())) == 0

    # Holdings stage must still be ok
    holdings_stage = next(
        s for s in result.stages if s.stage == "holdings"
    )
    assert holdings_stage.status == "ok"


def test_unexpected_lookahead_programmer_error_propagates(tmp_path, monkeypatch):
    """Unexpected bugs are never silently converted to evidence-only skips."""
    from tokmo.programs import production_cadence as pc_module

    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )

    def boom(**kwargs):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", boom)
    with pytest.raises(RuntimeError, match="programmer bug"):
        run_production_cadence_once(
            root=tmp_path / "shadow", policy_version="cadence-300s-v1",
            scheduled_for_utc=SLOT, observed_now_utc=OBSERVED, deps=deps,
        )


def test_unexpected_native_ref_programmer_error_is_not_silent(
    tmp_path, monkeypatch,
):
    """An unexpected native-ref bug must affect the tick, not disappear."""
    from tokmo.programs import production_cadence as pc_module

    deps, _ = build_fixture_cadence_deps(
        fixture=_open_cadence_fixture(), clock=lambda: OBSERVED,
    )

    def boom(**kwargs):
        raise RuntimeError("programmer bug")

    monkeypatch.setattr(pc_module, "write_native_refs_from_accounts", boom)
    result = run_production_cadence_once(
        root=tmp_path / "shadow", policy_version="cadence-300s-v1",
        scheduled_for_utc=SLOT, observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "PARTIAL"
    assert next(s for s in result.stages if s.stage == "holdings").status == "ok"
    assert next(s for s in result.stages if s.stage == "snapshot").status == "ok"
    assert any(s.status != "ok" for s in result.stages if s.stage not in (
        "calendar", "session_gate", "holdings", "snapshot", "projection",
    ))


# ── m2: malformed next_business_day cannot suppress another market ───────────


def test_malformed_next_business_day_does_not_suppress_other_market(
    tmp_path, monkeypatch,
):
    """m2: A malformed .next_business_day on one market's response must not
    raise an unhandled exception that suppresses the other market's lookahead
    write. The response type is validated and the access is inside per-market
    try, so one market's malformed next cannot suppress another."""

    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    fixture["calendar"]["US"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )

    # Wrap the fetcher to inject a malformed next_business_day into KR response
    real_calendar_fetch = deps.calendar_fetch

    def malformed_next_fetch(*, market, local_trade_date, receipt, bearer):
        response = real_calendar_fetch(
            market=market, local_trade_date=local_trade_date,
            receipt=receipt, bearer=bearer,
        )
        if market is Market.KR:
            # Inject a response where next_business_day.date is missing
            # — simulates a malformed provider wrapper
            from dataclasses import replace as dc_replace
            from tokmo.values._primitives import MISSING
            bad_next = dc_replace(
                response.next_business_day, date=MISSING,
            )
            return dc_replace(response, next_business_day=bad_next)
        return response

    object.__setattr__(deps, "calendar_fetch", malformed_next_fetch)

    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3",
        clock=lambda: OBSERVED,
    )
    # KR had malformed next — its lookahead write fails gracefully
    # US must still have its lookahead written (per-market independence)
    us_days = lookahead_store.read_days(Market.US)
    assert len(us_days) >= 1  # US not suppressed by KR's malformed next
    assert us_days[0].trade_date == "2026-08-03"


# ── m1: writer exceptions produce bounded stage evidence ────────────────────


def test_lookahead_writer_failure_records_bounded_stage_reason(
    tmp_path, monkeypatch,
):
    """m1: A typed writer exception (LookaheadWriterError) must produce a
    bounded sanitized stage reason — no secret/path/raw ref. A lifecycle
    event with the sanitized reason is emitted."""
    from tokmo.programs import production_cadence as pc_module

    events = []

    def capturing_sink(event):
        events.append(event)

    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    fixture["calendar"]["US"]["next_date"] = "2026-08-04"
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
        lifecycle_sink=capturing_sink,
    )

    def typed_fail(**kwargs):
        from tokmo.programs.calendar_lookahead_writer import LookaheadWriterError
        raise LookaheadWriterError("injected typed writer failure")

    monkeypatch.setattr(pc_module, "write_lookahead_from_calendar", typed_fail)

    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    # At least one stage event related to lookahead failure should be recorded
    # with a sanitized reason (no secret/path/raw ref in the reason text)
    lookahead_events = [
        e for e in events
        if (getattr(e, "stage", None) == "calendar"
            and (getattr(e, "reason", "") or "").startswith("lookahead_skipped_"))
    ]
    assert len(lookahead_events) >= 1
    for ev in lookahead_events:
        reason = getattr(ev, "reason", "") or ""
        # Must not contain path separators, file extensions, or secrets
        assert "/" not in reason
        assert ".sqlite" not in reason
        assert "bearer" not in reason.lower()


# ── Account native ref call-site wiring ──────────────────────────────────────


def test_holdings_complete_writes_toss_native_refs(tmp_path):
    """After a COMPLETE holdings stage, the Toss account native refs are
    persisted through the native ref writer seam."""
    fixture = _open_cadence_fixture()
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    ref_store = AccountNativeRefStoreV1(
        root / "ops" / "account_native_refs.sqlite3",
        clock=lambda: OBSERVED,
    )
    mapping = ref_store.read_refs()
    # The fixture account "9" should have a native ref persisted
    scopes = list(mapping.scopes())
    assert len(scopes) >= 1
    for scope in scopes:
        ref = mapping.reveal(scope)
        assert ref is not None
        assert ref  # non-empty


def test_holdings_complete_native_ref_receipt_binding(tmp_path):
    """m5: The native refs are bound to an exact receipt binding of
    policy_version:tick_id."""
    fixture = _open_cadence_fixture()
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"

    ref_store = AccountNativeRefStoreV1(
        root / "ops" / "account_native_refs.sqlite3",
        clock=lambda: OBSERVED,
    )
    mapping = ref_store.read_refs()
    binding = mapping.receipt_binding
    assert binding is not None
    # m5: exact value, not just "non-empty"
    assert binding == f"{result.policy_version}:{result.tick_id}"


def test_holdings_failed_no_native_ref_mutation(tmp_path):
    """When the holdings stage fails (market closed / mask failure), the
    native ref store is not mutated."""
    fixture = _open_cadence_fixture()
    # Make calendar fail so the mask fails and holdings stage is blocked
    fixture["calendar"]["KR"]["fail"] = True
    fixture["calendar"]["US"]["fail"] = True
    deps, _ = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )

    ref_store = AccountNativeRefStoreV1(
        root / "ops" / "account_native_refs.sqlite3",
        clock=lambda: OBSERVED,
    )
    mapping = ref_store.read_refs()
    assert len(list(mapping.scopes())) == 0  # no mutation


# ── No duplicate provider reads ──────────────────────────────────────────────


def test_no_duplicate_calendar_reads_for_lookahead(tmp_path):
    """m5: The lookahead write uses the same calendar response — no second
    CALENDAR provider read is issued. Each market is read EXACTLY once."""
    fixture = _open_cadence_fixture()
    fixture["calendar"]["KR"]["next_date"] = "2026-08-04"
    fixture["calendar"]["US"]["next_date"] = "2026-08-04"
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture, clock=lambda: OBSERVED,
    )
    root = tmp_path / "shadow"
    run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=SLOT,
        observed_now_utc=OBSERVED, deps=deps,
    )
    # m5: exact count, not <=2. Each market's calendar is read exactly once
    # by the main refresh. The lookahead write adds zero extra reads.
    assert probe.calls.get("CALENDAR_DAY_KR", 0) == 1
    assert probe.calls.get("CALENDAR_DAY_US", 0) == 1


# ── Pulse production deps E2E (no live provider) ─────────────────────────────


def test_pulse_production_deps_assembles_from_canonical_root(tmp_path):
    """build_pulse_production_deps creates all stores under the isolated root
    and returns a PulseDepsV1 with wired readers — no live provider needed."""
    from tokmo.programs.pulse_production_deps import build_pulse_production_deps
    from tokmo.programs.integrated_pulse import PulseDepsV1

    deps = build_pulse_production_deps(
        root=tmp_path, clock=lambda: OBSERVED,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=OBSERVED,
    )
    assert isinstance(deps, PulseDepsV1)
    # Readers work without live providers
    assert deps.holdings_reader() is None  # empty store → None, not empty portfolio
    assert deps.lookahead_reader(Market.KR) == ()  # empty store
    assert deps.lookahead_reader(Market.US) == ()


# ── Consumer seam remains wire0 ──────────────────────────────────────────────


def test_quotes_trades_consumer_seam_remains_wire0(tmp_path):
    """The post-cutover consumer seam functions read from stores without
    starting provider wires. The active production constant is unchanged."""
    from tokmo.programs.quotes_trades_consumer import (
        read_latest_quotes_for_consumer,
    )
    from tokmo.records.latest_quotes import LatestQuoteStore
    from tokmo.values.market_observations import TossCurrentPriceFact

    store = LatestQuoteStore(
        tmp_path / "stores" / "latest_quotes.sqlite3",
        clock=lambda: OBSERVED,
    )
    fact = TossCurrentPriceFact(
        source="toss-current-price-v1", symbol="AAPL",
        last_price="220.5", currency_raw="USD",
        timestamp=OBSERVED, fetched_at=OBSERVED,
    )
    store.save_batch((fact,))
    result = read_latest_quotes_for_consumer(
        store=store, source="toss-current-price-v1", symbols=("AAPL",),
    )
    assert len(result) == 1  # one row read, wire zero

    # Future T keeps cadence as fallback and flips to Pulse at the boundary.
    from tokmo.values.operation_lane_registry import OPERATION_LANE_REGISTRY_V1
    from tokmo.values.operation_lane_registry import OperationLane

    price_batch = OPERATION_LANE_REGISTRY_V1.entries["PRICE_BATCH"]
    assert price_batch is not None
    assert price_batch.recurring_owner is OperationLane.PULSE_60S
    assert price_batch.fallback_owner is OperationLane.CADENCE_300S
    assert price_batch.cutover_epoch_utc is not None
