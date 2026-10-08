"""Sealed legacy G2 acceptance wrappers (Stage 2 repair M1/M2).

This module is the only home of the historical, pre-mask G2 acceptance
behavior: session-verdict market states, host-date window anchors, the
single-source market classification shortcut, and the hand-coded US-DST
local-date calculation. Everything here is **legacy**: it exists so the
already-reviewed bounded G2 acceptance runs (fake-day/shadow/canary) keep
their historical bytes and hashes exactly, and it may only be reached from
the closed historical allowlist enforced by
``tests/test_stage2_architecture.py``. Production cadence uses the
mask-mandatory seams in the owning program modules and must never import
this module.
"""

from datetime import datetime, timedelta, timezone

from tokmo.programs.analysis_barrier import (
    _LEGACY_G2_CAPABILITY as _BARRIER_LEGACY, _assemble,
)
from tokmo.programs.holdings_sync import (
    _LEGACY_G2_CAPABILITY as _HOLDINGS_LEGACY, _locked_holdings_sync,
)
from tokmo.programs.quotes_sync import (
    _LEGACY_G2_CAPABILITY as _QUOTES_LEGACY, _locked_quotes_sync,
)
from tokmo.programs.trades_sync import (
    _LEGACY_G2_CAPABILITY as _TRADES_LEGACY, _locked_trades_sync,
)
from tokmo.values.session_gate import MARKET_TIMEZONE, Market


def _legacy_us_dst(instant: datetime) -> bool:
    """Legacy hand-coded US Eastern DST window (G2 acceptance only)."""
    year = instant.year
    march_first = datetime(year, 3, 1, tzinfo=timezone.utc)
    first_sunday = 1 + (6 - march_first.weekday()) % 7
    second_sunday = first_sunday + 7
    november_first = datetime(year, 11, 1, tzinfo=timezone.utc)
    november_sunday = 1 + (6 - november_first.weekday()) % 7
    start = datetime(year, 3, second_sunday, 7, tzinfo=timezone.utc)
    end = datetime(year, 11, november_sunday, 6, tzinfo=timezone.utc)
    return start <= instant.astimezone(timezone.utc) < end


def _legacy_local_date(value: datetime, market: Market):
    """Legacy fixed-offset market-local date (G2 acceptance only)."""
    name = MARKET_TIMEZONE[market]
    if name == "Asia/Seoul":
        offset = 9
    else:
        offset = -4 if _legacy_us_dst(value) else -5
    return (value.astimezone(timezone.utc) + timedelta(hours=offset)).date()


def _legacy_market_date_ok(captured_at, market, now):
    return _legacy_local_date(captured_at, market) == _legacy_local_date(now, market)


def legacy_run_holdings_sync(*, tick, session_result, holdings_provider,
                             store, pack_writer, receipt_store,
                             reservation_seal, incident_writer, clock, now,
                             job_lock_path):
    """Legacy G2 holdings collector: session-verdict states, no mask."""
    return _locked_holdings_sync(
        tick=tick, session_result=session_result,
        holdings_provider=holdings_provider, store=store,
        pack_writer=pack_writer, receipt_store=receipt_store,
        reservation_seal=reservation_seal, incident_writer=incident_writer,
        clock=clock, now=now, job_lock_path=job_lock_path, mask=None,
        legacy=_HOLDINGS_LEGACY,
    )


def legacy_run_quotes_sync(*, tick, session_result, holdings, quotes_provider,
                           quote_store, pack_writer, receipt_store,
                           reservation_seal, incident_writer, clock, now,
                           job_lock_path):
    """Legacy G2 quotes collector: session-verdict states, no mask."""
    return _locked_quotes_sync(
        tick=tick, session_result=session_result, holdings=holdings,
        quotes_provider=quotes_provider, quote_store=quote_store,
        pack_writer=pack_writer, receipt_store=receipt_store,
        reservation_seal=reservation_seal, incident_writer=incident_writer,
        clock=clock, now=now, job_lock_path=job_lock_path, mask=None,
        legacy=_QUOTES_LEGACY,
    )


def legacy_run_trades_sync(*, tick, session_result, holdings, orders_provider,
                           detail_provider, store, scan_state, pack_writer,
                           receipt_store, reservation_seal, incident_writer,
                           regular_session_dates, clock, now,
                           job_lock_path, ops_kernel=None, invocation_id=None):
    """Legacy G2 trades collector: session-verdict states, host-date
    anchors, exact order-currency market classification, no mask."""
    return _locked_trades_sync(
        tick=tick, session_result=session_result, holdings=holdings,
        orders_provider=orders_provider, detail_provider=detail_provider,
        store=store, scan_state=scan_state, pack_writer=pack_writer,
        receipt_store=receipt_store, reservation_seal=reservation_seal,
        incident_writer=incident_writer,
        regular_session_dates=regular_session_dates, clock=clock, now=now,
        job_lock_path=job_lock_path, ops_kernel=ops_kernel,
        invocation_id=invocation_id, mask=None, legacy=_TRADES_LEGACY,
    )


def legacy_assemble_analysis_materials(*, tick, open_markets, receipt_readers,
                                       pack_reader, now):
    """Legacy G2 analysis barrier: caller-supplied open markets and the
    legacy hand-coded DST local-date freshness, no mask."""
    return _assemble(
        tick=tick, open_markets=open_markets, receipt_readers=receipt_readers,
        pack_reader=pack_reader, now=now, mask=None,
        legacy_market_date_ok=_legacy_market_date_ok, legacy=_BARRIER_LEGACY,
    )
