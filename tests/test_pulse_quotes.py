"""Pulse quote lane: gate, targets, monotonic latest-upsert (Plan §13–§15)."""

from datetime import datetime, timedelta, timezone

import pytest

from tokmo.records.calendar_lookahead import LookaheadDayV1, LookaheadSessionV1
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.programs.pulse_quotes import (
    PulseQuoteBatchOutcomeV1,
    run_pulse_quotes,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.pulse_policy import pulse_tick_v1
from tokmo.values.session_gate import Market


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


TICK_SLOT = _utc("2026-08-10T01:00:00Z")  # 10:00 KST — KR/US day both open
TICK = pulse_tick_v1("pulse-60s-v1", TICK_SLOT)
NOW = _utc("2026-08-10T01:00:01Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24


def _session(kind, start, end):
    return LookaheadSessionV1(kind, _utc(start), _utc(end))


def _us_day():
    return LookaheadDayV1(
        Market.US, "2026-08-10", "2026-08-10",
        (
            _session("dayMarket", "2026-08-10T00:00:00Z", "2026-08-10T08:00:00Z"),
            _session("preMarket", "2026-08-10T08:00:00Z", "2026-08-10T13:30:00Z"),
        ),
        NOW, "calendar:US:2026-08-10:0",
    )


def _kr_day():
    return LookaheadDayV1(
        Market.KR, "2026-08-10", "2026-08-10",
        (_session("regularMarket", "2026-08-10T00:00:00Z",
                  "2026-08-10T06:30:00Z"),),
        NOW, "calendar:KR:2026-08-10:0",
    )


def _lookahead(days_by_market):
    def read(market):
        value = days_by_market.get(market, ())
        if isinstance(value, Exception):
            raise value
        return value
    return read


def _holdings():
    identity = {
        TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, "personal", 1, None),
        PENSION_SCOPE: AccountScopeIdentityV1(
            PENSION_SCOPE, "pension-excel-v1", 2, None),
    }
    accounts = (
        AccountHoldingsV1(TOSS_SCOPE, 1, (
            HoldingItemV1("005930", "10", "60000", "KRW", "KR", "market"),
            HoldingItemV1("SPCX", "3", "100", "USD", "US", "market"),
        )),
        AccountHoldingsV1(PENSION_SCOPE, 2, (
            HoldingItemV1("069500", "700", "30000", "KRW", "KR", "market"),
        )),
    )
    return CurrentHoldingsV1(
        1, accounts, True, _utc("2026-08-10T00:55:00Z"), identity)


def _fact(symbol, price="100", currency="KRW", ts="2026-08-10T01:00:00Z",
          fetched="2026-08-10T01:00:01Z"):
    return TossCurrentPriceFact(
        source="toss-current-price-v1", symbol=symbol, last_price=price,
        currency_raw=currency, timestamp=None if ts is None else _utc(ts),
        fetched_at=_utc(fetched),
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, operation, native_identity):
        self.calls.append((operation, native_identity))
        return len(self.calls)


def _run(tmp_path, *, lookahead=None, holdings="default", provider=None,
         watchlists=None, recorder=None, token=None, clock=None):
    store = LatestQuoteStore(
        tmp_path / "stores" / "latest_quotes.sqlite3",
        clock=clock or (lambda: NOW),
    )
    outcomes = provider or {}

    def quotes_provider(market, symbols):
        value = outcomes.get(market)
        if isinstance(value, Exception):
            raise value
        if callable(value):
            return value(symbols)
        if value is None:
            return PulseQuoteBatchOutcomeV1(
                "success",
                tuple(_fact(symbol,
                            currency="KRW" if market is Market.KR else "USD")
                      for symbol in symbols),
                (),
            )
        return value

    def holdings_reader():
        if holdings == "default":
            return _holdings()
        if isinstance(holdings, Exception):
            raise holdings
        return holdings

    result = run_pulse_quotes(
        tick=TICK,
        lookahead_reader=lookahead or _lookahead(
            {Market.KR: (_kr_day(),), Market.US: (_us_day(),)}),
        holdings_reader=holdings_reader,
        watchlist_symbols=watchlists if watchlists is not None else {
            Market.KR: frozenset({"000660"}), Market.US: frozenset({"TSLA"})},
        token_provider=token or (lambda: "token-value"),
        quotes_provider=quotes_provider,
        quote_store=store,
        clock=clock or (lambda: NOW),
        now=NOW,
        wire_recorder=recorder if recorder is not None else _Recorder(),
    )
    return result, store


def test_both_markets_closed_means_zero_wire_starts(tmp_path):
    recorder = _Recorder()
    token_calls = []
    result, _store = _run(
        tmp_path,
        lookahead=_lookahead({Market.KR: (), Market.US: ()}),
        recorder=recorder,
        token=lambda: token_calls.append(1) or "token",
    )
    assert result.status == "skipped"
    assert result.reason == "skipped_market_closed"
    assert recorder.calls == []
    assert token_calls == []


def test_open_markets_quote_holdings_union_watchlist_with_pension(tmp_path):
    recorder = _Recorder()
    result, store = _run(tmp_path, recorder=recorder)
    assert result.status == "saved"
    # Pension symbol 069500 is a Toss quote target (§2.4); dedupe and
    # market classification hold; watchlists join their market's batch.
    assert result.targets[Market.KR] == ("000660", "005930", "069500")
    assert result.targets[Market.US] == ("SPCX", "TSLA")
    kr = store.read_latest(
        source="toss-current-price-v1", symbols=("005930", "069500", "000660"))
    assert {fact.symbol for fact in kr} == {"005930", "069500", "000660"}
    # One PRICE_BATCH per open market; the shared lane token is acquired
    # lazily by the injected provider (recorded at the runner seam).
    operations = [call[0] for call in recorder.calls]
    assert operations.count("PRICE_BATCH") == 2
    assert result.wire_starts == 2


def test_missing_holdings_is_not_an_empty_portfolio(tmp_path):
    recorder = _Recorder()
    result, store = _run(tmp_path, holdings=None, recorder=recorder)
    assert result.status == "saved"
    assert result.holdings_status == "HOLDINGS_UNAVAILABLE"
    # Watchlist targets still quote; holdings-derived targets are skipped
    # truthfully rather than being treated as an empty portfolio.
    assert result.targets[Market.KR] == ("000660",)
    assert result.targets[Market.US] == ("TSLA",)


def test_holdings_read_failure_degrades_to_watchlist_only(tmp_path):
    result, _store = _run(tmp_path, holdings=RuntimeError("read failed"))
    assert result.holdings_status == "HOLDINGS_UNAVAILABLE"
    assert result.targets[Market.KR] == ("000660",)


def test_holdings_as_of_is_propagated_never_forged(tmp_path):
    result, _store = _run(tmp_path)
    assert result.holdings_as_of == _utc("2026-08-10T00:55:00Z")


def test_invalid_watchlist_fails_only_that_market(tmp_path):
    result, _store = _run(
        tmp_path,
        watchlists={Market.KR: ("not", "a", "frozenset"),
                    Market.US: frozenset({"TSLA"})},
    )
    assert result.status == "partial"
    assert result.market_reasons[Market.KR] == "failed_watchlist_contract"
    # Holdings-derived US targets proceed.
    assert result.targets[Market.US] == ("SPCX", "TSLA")


def test_monotonic_guard_drops_older_observation(tmp_path):
    recorder = _Recorder()
    first, store = _run(tmp_path, recorder=recorder)
    older = {
        Market.KR: PulseQuoteBatchOutcomeV1(
            "success",
            tuple(_fact(symbol, price="1",
                        ts="2026-08-10T00:30:00Z",
                        fetched="2026-08-10T00:30:01Z")
                  for symbol in ("000660", "005930", "069500")),
            (),
        ),
        Market.US: PulseQuoteBatchOutcomeV1(
            "success",
            tuple(_fact(symbol, price="1", currency="USD",
                        ts="2026-08-10T00:30:00Z",
                        fetched="2026-08-10T00:30:01Z")
                  for symbol in ("SPCX", "TSLA")),
            (),
        ),
    }
    result, store2 = _run(tmp_path, provider=older)
    assert set(result.dropped_stale) == {
        "000660", "005930", "069500", "SPCX", "TSLA"}
    kept = store2.read_latest(
        source="toss-current-price-v1", symbols=("005930",))[0]
    assert kept.last_price == "100"  # the newer value survives
    assert kept.timestamp == _utc("2026-08-10T01:00:00Z")


def test_currency_conflict_fails_closed_per_symbol(tmp_path):
    _first, _store = _run(tmp_path)
    conflicted = {
        Market.KR: PulseQuoteBatchOutcomeV1(
            "success",
            (
                _fact("005930", price="200", currency="USD",
                      ts="2026-08-10T01:30:00Z",
                      fetched="2026-08-10T01:30:01Z"),
                _fact("000660", price="200",
                      ts="2026-08-10T01:30:00Z",
                      fetched="2026-08-10T01:30:01Z"),
                _fact("069500", price="200",
                      ts="2026-08-10T01:30:00Z",
                      fetched="2026-08-10T01:30:01Z"),
            ),
            (),
        ),
    }
    result, store = _run(tmp_path, provider=conflicted)
    assert "005930" in result.rejected_currency_conflict
    unchanged = store.read_latest(
        source="toss-current-price-v1", symbols=("005930",))[0]
    assert unchanged.currency_raw == "KRW"
    assert unchanged.last_price == "100"
    updated = store.read_latest(
        source="toss-current-price-v1", symbols=("000660",))[0]
    assert updated.last_price == "200"


def test_partial_batch_upserts_only_successful_symbols(tmp_path):
    partial = {
        Market.KR: PulseQuoteBatchOutcomeV1(
            "success",
            (_fact("005930"), _fact("069500")),
            ("000660",),
        ),
    }
    result, store = _run(tmp_path, provider=partial)
    assert result.failed_symbols == ("000660",)
    saved = store.read_latest(
        source="toss-current-price-v1", symbols=("005930", "069500"))
    assert {fact.symbol for fact in saved} == {"005930", "069500"}
    with pytest.raises(Exception):
        store.read_latest(source="toss-current-price-v1", symbols=("000660",))


def test_provider_failure_fails_that_market_only(tmp_path):
    result, _store = _run(
        tmp_path,
        provider={Market.KR: PulseQuoteBatchOutcomeV1("http_429", (), ())},
    )
    assert result.status == "partial"
    assert result.market_reasons[Market.KR] == "failed_provider_rate_limit"
    assert result.targets[Market.US] == ("SPCX", "TSLA")


def test_unrequested_symbol_in_response_is_a_contract_failure(tmp_path):
    result, _store = _run(
        tmp_path,
        provider={Market.KR: PulseQuoteBatchOutcomeV1(
            "success", (_fact("999999"),), ())},
    )
    assert result.market_reasons[Market.KR] == "failed_provider_contract"


def test_one_price_batch_per_open_market_never_more(tmp_path):
    recorder = _Recorder()
    _result, _store = _run(tmp_path, recorder=recorder)
    assert [call[0] for call in recorder.calls].count("PRICE_BATCH") == 2


def test_store_busy_is_truthful_store_busy_reason(tmp_path):
    class _BusyStore:
        def save_batch(self, facts):
            from tokmo.records.latest_quotes import LatestQuoteSaveReceipt
            return LatestQuoteSaveReceipt(
                "FAILED", 0, 0, "store_locked", NOW)

        def read_latest(self, *, source, symbols):
            from tokmo.records.latest_quotes import LatestQuoteStoreReadError
            raise LatestQuoteStoreReadError("missing_latest_quote")

    result = run_pulse_quotes(
        tick=TICK,
        lookahead_reader=_lookahead({Market.KR: (_kr_day(),), Market.US: ()}),
        holdings_reader=lambda: _holdings(),
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        token_provider=lambda: "token",
        quotes_provider=lambda market, symbols: PulseQuoteBatchOutcomeV1(
            "success", tuple(_fact(symbol) for symbol in symbols), ()),
        quote_store=_BusyStore(),
        clock=lambda: NOW,
        now=NOW,
        wire_recorder=_Recorder(),
    )
    assert result.status in ("failed", "partial")
    assert result.market_reasons[Market.KR] == "STORE_BUSY"


def test_lookahead_failure_fails_closed_for_that_market(tmp_path):
    from tokmo.records.calendar_lookahead import CalendarLookaheadCorruptError
    recorder = _Recorder()
    result, _store = _run(
        tmp_path,
        lookahead=_lookahead({
            Market.KR: CalendarLookaheadCorruptError("corrupt"),
            Market.US: (),
        }),
        recorder=recorder,
    )
    assert result.status == "skipped"
    assert result.market_reasons[Market.KR] == "lookahead_corrupt"
    assert recorder.calls == []
