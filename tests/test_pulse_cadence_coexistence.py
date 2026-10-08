"""Pulse↔300s coexistence: consumer seams, identity/lock separation (§18)."""

from datetime import datetime, timezone

import pytest

from tokmo.programs.quotes_sync import (
    ConsumedQuoteRowV1,
    consume_latest_quotes,
)
from tokmo.programs.trades_sync import consume_execution_window
from tokmo.records.latest_quotes import LatestQuoteStoreReadError
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.pulse_policy import pulse_tick_v1
from tokmo.values.session_gate import Market


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


CADENCE_TICK = cadence_tick_v1(
    "cadence-300s-market-portfolio-v1", _utc("2026-08-10T01:00:00Z"))
PULSE_TICK = pulse_tick_v1("pulse-60s-v1", _utc("2026-08-10T01:00:00Z"))


def _row(symbol, provider_ts="2026-08-10T00:59:30Z",
         fetched="2026-08-10T00:59:31Z", stored="2026-08-10T00:59:32Z"):
    return {
        "symbol": symbol, "last_price": "70000", "currency_raw": "KRW",
        "provider_timestamp": provider_ts, "fetched_at": fetched,
        "stored_at": stored,
    }


def test_quote_consumption_preserves_all_three_instants():
    result = consume_latest_quotes(
        tick=CADENCE_TICK,
        symbols_by_market={Market.KR: ("005930",)},
        latest_reader=lambda market, symbols: [_row("005930")],
    )
    assert result.status == "saved"
    row = result.rows_by_market[Market.KR][0]
    assert isinstance(row, ConsumedQuoteRowV1)
    assert row.provider_timestamp == "2026-08-10T00:59:30Z"
    assert row.fetched_at == "2026-08-10T00:59:31Z"
    assert row.stored_at == "2026-08-10T00:59:32Z"


def test_stale_quote_is_never_promoted_to_fresh():
    # An hours-old observation flows through with its own old instants —
    # the consumer has no path that rewrites them to "now".
    result = consume_latest_quotes(
        tick=CADENCE_TICK,
        symbols_by_market={Market.KR: ("005930",)},
        latest_reader=lambda market, symbols: [_row(
            "005930", provider_ts="2026-08-09T20:00:00Z",
            fetched="2026-08-09T20:00:01Z", stored="2026-08-09T20:00:02Z")],
    )
    row = result.rows_by_market[Market.KR][0]
    assert row.provider_timestamp == "2026-08-09T20:00:00Z"
    assert row.stored_at == "2026-08-09T20:00:02Z"


def test_quote_consumption_refuses_a_pulse_tick_identity():
    with pytest.raises(ValueError):
        consume_latest_quotes(
            tick=PULSE_TICK,
            symbols_by_market={Market.KR: ("005930",)},
            latest_reader=lambda market, symbols: [_row("005930")],
        )


def test_busy_store_is_typed_store_busy():
    def busy_reader(market, symbols):
        raise LatestQuoteStoreReadError("store_locked")

    result = consume_latest_quotes(
        tick=CADENCE_TICK,
        symbols_by_market={Market.KR: ("005930",)},
        latest_reader=busy_reader,
    )
    assert result.status == "failed"
    assert result.reason == "STORE_BUSY"


def test_other_read_failures_are_not_store_busy():
    def broken_reader(market, symbols):
        raise LatestQuoteStoreReadError("store_corrupt")

    result = consume_latest_quotes(
        tick=CADENCE_TICK,
        symbols_by_market={Market.KR: ("005930",)},
        latest_reader=broken_reader,
    )
    assert result.status == "failed"
    assert result.reason == "failed_store_read"


def _audit_rows():
    return [
        {"source": "toss-orders-v1", "account_scope": "toss-acct-" + "a" * 24,
         "source_record_key": f"ord-{index}",
         "stored_at": f"2026-08-10T01:0{index}:00Z"}
        for index in range(5)
    ]


def test_execution_window_is_half_open_with_no_gap_or_overlap():
    first = consume_execution_window(
        tick=CADENCE_TICK, audit_reader=_audit_rows,
        window_from_utc=_utc("2026-08-10T01:00:00Z"),
        window_to_utc=_utc("2026-08-10T01:02:00Z"))
    second = consume_execution_window(
        tick=CADENCE_TICK, audit_reader=_audit_rows,
        window_from_utc=_utc("2026-08-10T01:02:00Z"),
        window_to_utc=_utc("2026-08-10T01:05:00Z"))
    first_keys = {row[2] for row in first.record_keys}
    second_keys = {row[2] for row in second.record_keys}
    # The boundary row (stored exactly at 01:02) lands in exactly one window.
    assert first_keys == {"ord-0", "ord-1"}
    assert second_keys == {"ord-2", "ord-3", "ord-4"}
    assert not first_keys & second_keys
    assert first_keys | second_keys == {f"ord-{index}" for index in range(5)}


def test_execution_window_refuses_pulse_tick_and_bad_windows():
    with pytest.raises(ValueError):
        consume_execution_window(
            tick=PULSE_TICK, audit_reader=_audit_rows,
            window_from_utc=_utc("2026-08-10T01:00:00Z"),
            window_to_utc=_utc("2026-08-10T01:02:00Z"))
    with pytest.raises(ValueError):
        consume_execution_window(
            tick=CADENCE_TICK, audit_reader=_audit_rows,
            window_from_utc=_utc("2026-08-10T01:02:00Z"),
            window_to_utc=_utc("2026-08-10T01:02:00Z"))


def test_consumer_seams_never_touch_a_provider():
    # The seams have no provider parameter at all: consumption is expressed
    # purely over injected store readers (wire starts structurally zero).
    import inspect
    quote_params = set(inspect.signature(consume_latest_quotes).parameters)
    trade_params = set(inspect.signature(consume_execution_window).parameters)
    assert quote_params == {"tick", "symbols_by_market", "latest_reader"}
    assert trade_params == {"tick", "audit_reader", "window_from_utc",
                            "window_to_utc"}


def test_receipt_stores_are_physically_separate_files(tmp_path):
    from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    now = _utc("2026-08-10T01:00:01Z")
    cadence = CadenceTickReceiptStore(
        tmp_path / "ops" / "cadence_tick_receipts.sqlite3",
        clock=lambda: now)
    pulse = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: now)
    cadence.begin_tick("cadence-300s-market-portfolio-v1",
                       CADENCE_TICK.scheduled_for_utc, CADENCE_TICK.tick_id,
                       observed_start=now, lag_seconds=1.0,
                       max_lag_seconds=60)
    pulse.begin_tick("pulse-60s-v1", PULSE_TICK.scheduled_for_utc,
                     PULSE_TICK.tick_id, observed_start=now,
                     lag_seconds=1.0, max_lag_seconds=15)
    assert (tmp_path / "ops" / "cadence_tick_receipts.sqlite3").exists()
    assert (tmp_path / "ops" / "pulse_tick_receipts.sqlite3").exists()
    # Same wall instant, disjoint identities in disjoint files.
    assert cadence.read_tick(
        "cadence-300s-market-portfolio-v1",
        CADENCE_TICK.scheduled_for_utc)["tick_id"] == CADENCE_TICK.tick_id
    assert pulse.read_tick(
        "pulse-60s-v1",
        PULSE_TICK.scheduled_for_utc)["tick_id"] == PULSE_TICK.tick_id
    assert CADENCE_TICK.tick_id != PULSE_TICK.tick_id


def test_pulse_lease_name_is_not_the_cadence_lease():
    from tokmo.programs.integrated_pulse import _LOCK_NAME
    assert _LOCK_NAME == "pulse-60s.lock"
    assert _LOCK_NAME != "cadence.lock"
