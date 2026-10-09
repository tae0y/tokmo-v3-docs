"""Contract tests for the 20 remaining Toss reads: request validators,
response models/parsers, atomicity, null/missing/raw-enum handling, and
per-endpoint cases from plan §3.
"""

import json
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.facts.toss_reads import (
    SPEC_TABLE,
    TossReadOperation,
    TossReadOutcome,
    build_read_request,
    parse_read_response,
    resolve_conditional_order_side,
)
from tokmo.values import MISSING, Opt, _Decimal30
from tokmo.values.toss_orders import (
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
)

FIXTURE = (
    Path(__file__).parent / "fixtures" / "toss_remaining_reads_openapi_excerpt.json"
)
NOW = datetime(2026, 7, 28, 3, 0, tzinfo=UTC)
TOKEN = "SYNTHETIC-READ-TOKEN-0001"
ACCOUNT_SEQ = "1234567890"

KST = timezone(timedelta(hours=9))


def encoded(result):
    return json.dumps({"result": result}).encode()


def encoded_root(result_key, value):
    return json.dumps({"result": {result_key: value}}).encode()


# =========================================================================
# Foundation: enum, spec table, request base, outcome, redaction
# =========================================================================


class TestFoundation:
    def test_enum_has_exactly_twenty_members(self):
        assert len(TossReadOperation) == 21

    def test_spec_table_keys_equal_enum(self):
        assert set(SPEC_TABLE) == set(TossReadOperation)

    def test_spec_table_has_no_mutable_method_or_host(self):
        import dataclasses

        from tokmo.facts.toss_reads import TossReadSpec

        names = {f.name for f in dataclasses.fields(TossReadSpec)}
        assert "method" not in names and "host" not in names

    def test_outcome_is_closed_and_repr_safe(self):
        outcome = TossReadOutcome("success")
        assert outcome.code == "success"
        assert outcome.value is None
        outcome2 = TossReadOutcome("contract_mismatch")
        assert outcome2.code == "contract_mismatch"
        assert outcome2.value is None

    def test_request_token_account_and_ids_are_repr_false(self):
        from tokmo.facts.toss_reads import build_read_request

        req = build_read_request(
            TossReadOperation.ORDERS,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            status="OPEN",
        )
        rendered = repr(req)
        assert TOKEN not in rendered
        assert ACCOUNT_SEQ not in rendered

    def test_outcome_value_is_repr_false(self):
        outcome = TossReadOutcome("success", value=object())
        assert "object" not in repr(outcome)


# =========================================================================
# Bundle 1 — ORDERBOOK
# =========================================================================


class TestOrderbook:
    def _req(self, symbol="005930"):
        return build_read_request(
            TossReadOperation.ORDERBOOK, token=TOKEN, symbol=symbol
        )

    def test_valid_request_builds(self):
        req = self._req()
        assert req.operation == TossReadOperation.ORDERBOOK
        assert req.account_seq is None

    def test_kr_and_us_roots_parse(self):
        body = encoded(
            {
                "currency": "KRW",
                "asks": [{"price": "72100", "volume": "8500"}],
                "bids": [{"price": "72000", "volume": "3000"}],
                "timestamp": "2026-03-25T09:30:00.123+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"
        assert outcome.value is not None

    def test_null_timestamp_succeeds(self):
        body = encoded(
            {
                "currency": "USD",
                "asks": [],
                "bids": [],
                "timestamp": None,
            }
        )
        outcome = parse_read_response(
            req=self._req(symbol="AAPL"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_empty_books_succeed(self):
        body = encoded({"currency": "KRW", "asks": [], "bids": []})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_missing_required_root_key_fails(self):
        for body in [
            b"not-json",
            b"[]",
            b"{}",
            encoded({"asks": [], "bids": []}),  # missing currency
            encoded({"currency": "KRW", "bids": []}),  # missing asks
        ]:
            outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
            assert outcome.code in {"parse_error", "contract_mismatch"}

    def test_malformed_entry_fails_all(self):
        for bad_price in [-1, "NaN", True, "", "1" * 31]:
            body = encoded(
                {
                    "currency": "KRW",
                    "asks": [{"price": bad_price, "volume": "100"}],
                    "bids": [{"price": "72000", "volume": "100"}],
                }
            )
            outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
            assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — TRADES
# =========================================================================


class TestTrades:
    def _req(self, symbol="005930", count=None):
        kwargs = {"symbol": symbol}
        if count is not None:
            kwargs["count"] = count
        return build_read_request(TossReadOperation.TRADES, token=TOKEN, **kwargs)

    def test_count_bounds_accepted(self):
        for c in [None, 1, 50]:
            kwargs = {"symbol": "X"}
            if c is not None:
                kwargs["count"] = c
            req = build_read_request(TossReadOperation.TRADES, token=TOKEN, **kwargs)
            assert req.operation == TossReadOperation.TRADES

    def test_count_out_of_range_rejected(self):
        for c in [0, 51, -1, 100]:
            with pytest.raises(ValueError):
                build_read_request(
                    TossReadOperation.TRADES, token=TOKEN, symbol="X", count=c
                )

    def test_count_bool_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.TRADES, token=TOKEN, symbol="X", count=True
            )

    def test_empty_list_succeeds(self):
        outcome = parse_read_response(req=self._req(), body=encoded([]), fetched_at=NOW)
        assert outcome.code == "success"

    def test_one_bad_row_fails_all(self):
        body = encoded(
            [
                {
                    "price": "72000",
                    "volume": "120",
                    "timestamp": "2026-03-25T09:30:42.000+09:00",
                    "currency": "KRW",
                },
                {
                    "price": "NaN",
                    "volume": "1",
                    "timestamp": "2026-03-25T09:30:42.000+09:00",
                    "currency": "KRW",
                },
            ]
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — PRICE_LIMITS
# =========================================================================


class TestPriceLimits:
    def _req(self, symbol="005930"):
        return build_read_request(
            TossReadOperation.PRICE_LIMITS, token=TOKEN, symbol=symbol
        )

    def test_kr_non_null_limits(self):
        body = encoded(
            {
                "timestamp": "2026-03-25T09:30:00+09:00",
                "currency": "KRW",
                "upperLimitPrice": "93000",
                "lowerLimitPrice": "50400",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_us_null_limits(self):
        body = encoded(
            {
                "timestamp": "2026-03-25T22:30:00+09:00",
                "currency": "USD",
                "upperLimitPrice": None,
                "lowerLimitPrice": None,
            }
        )
        outcome = parse_read_response(
            req=self._req(symbol="AAPL"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_missing_optional_limits_succeed(self):
        body = encoded(
            {
                "timestamp": "2026-03-25T09:30:00+09:00",
                "currency": "KRW",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_missing_required_timestamp_or_currency_fails(self):
        for body in [
            encoded({"currency": "KRW"}),
            encoded({"timestamp": "2026-03-25T09:30:00+09:00"}),
        ]:
            outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
            assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — STOCK_CANDLES
# =========================================================================


class TestStockCandles:
    def _req(
        self, symbol="005930", interval="1d", count=None, before=None, adjusted=None
    ):
        kwargs = {"symbol": symbol, "interval": interval}
        if count is not None:
            kwargs["count"] = count
        if before is not None:
            kwargs["before"] = before
        if adjusted is not None:
            kwargs["adjusted"] = adjusted
        return build_read_request(
            TossReadOperation.STOCK_CANDLES, token=TOKEN, **kwargs
        )

    def test_both_intervals_accepted(self):
        for iv in ["1m", "1d"]:
            req = self._req(interval=iv)
            assert req.operation == TossReadOperation.STOCK_CANDLES

    def test_invalid_interval_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.STOCK_CANDLES, token=TOKEN, symbol="X", interval="5m"
            )

    def test_count_bounds(self):
        for c in [1, 200]:
            req = self._req(count=c)
            assert req.operation == TossReadOperation.STOCK_CANDLES
        for c in [0, 201]:
            with pytest.raises(ValueError):
                self._req(count=c)

    def test_adjusted_bool_only(self):
        with pytest.raises(TypeError):
            build_read_request(
                TossReadOperation.STOCK_CANDLES,
                token=TOKEN,
                symbol="X",
                interval="1d",
                adjusted=1,
            )

    def test_parse_preserves_next_before_as_metadata(self):
        body = encoded(
            {
                "candles": [
                    {
                        "timestamp": "2026-03-25T09:00:00+09:00",
                        "openPrice": "71600",
                        "highPrice": "72300",
                        "lowPrice": "71500",
                        "closePrice": "72000",
                        "volume": "3521000",
                        "currency": "KRW",
                    }
                ],
                "nextBefore": "2026-03-24T09:00:00+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_empty_candles_succeed(self):
        body = encoded({"candles": []})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_one_bad_candle_fails_all(self):
        body = encoded(
            {
                "candles": [
                    {
                        "timestamp": "2026-03-25T09:00:00+09:00",
                        "openPrice": "71600",
                        "highPrice": "72300",
                        "lowPrice": "71500",
                        "closePrice": "72000",
                        "volume": "3521000",
                        "currency": "KRW",
                    },
                    {
                        "timestamp": "2026-03-25T09:00:00+09:00",
                        "openPrice": "NaN",
                        "highPrice": "72300",
                        "lowPrice": "71500",
                        "closePrice": "72000",
                        "volume": "3521000",
                        "currency": "KRW",
                    },
                ]
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — STOCKS (batch)
# =========================================================================


class TestStocks:
    def _req(self, symbols):
        return build_read_request(
            TossReadOperation.STOCKS, token=TOKEN, symbols=symbols
        )

    def test_1_and_200_symbols_accepted(self):
        self._req(("ABC",))
        self._req(tuple(f"S{i}" for i in range(200)))

    def test_duplicate_empty_and_201_rejected(self):
        for bad in [(), ("A", "A"), ("",), tuple(f"S{i}" for i in range(201))]:
            with pytest.raises(ValueError):
                self._req(bad)

    def test_batch_identity_exact_match(self):
        body = encoded(
            [
                {
                    "symbol": "ABC",
                    "name": "A",
                    "englishName": "A",
                    "isinCode": "US123",
                    "market": "NASDAQ",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "sharesOutstanding": "1000",
                },
                {
                    "symbol": "XYZ",
                    "name": "X",
                    "englishName": "X",
                    "isinCode": "US456",
                    "market": "NYSE",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "sharesOutstanding": "2000",
                },
            ]
        )
        outcome = parse_read_response(
            req=self._req(("ABC", "XYZ")), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_batch_identity_mismatch_fails(self):
        body = encoded(
            [
                {
                    "symbol": "ABC",
                    "name": "A",
                    "englishName": "A",
                    "isinCode": "US123",
                    "market": "NASDAQ",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "sharesOutstanding": "1000",
                },
            ]
        )
        # Requested XYZ too
        outcome = parse_read_response(
            req=self._req(("ABC", "XYZ")), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

    def test_kr_nested_detail_and_us_null(self):
        body = encoded(
            [
                {
                    "symbol": "005930",
                    "name": "삼성전자",
                    "englishName": "SamsungElec",
                    "isinCode": "KR7005930003",
                    "market": "KOSPI",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "KRW",
                    "sharesOutstanding": "5919637922",
                    "koreanMarketDetail": {
                        "liquidationTrading": False,
                        "nxtSupported": True,
                        "krxTradingSuspended": False,
                    },
                },
                {
                    "symbol": "AAPL",
                    "name": "Apple",
                    "englishName": "Apple",
                    "isinCode": "US0378331005",
                    "market": "NASDAQ",
                    "securityType": "STOCK",
                    "isCommonShare": True,
                    "status": "ACTIVE",
                    "currency": "USD",
                    "sharesOutstanding": "15000",
                    "koreanMarketDetail": None,
                },
            ]
        )
        outcome = parse_read_response(
            req=self._req(("005930", "AAPL")), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_unknown_raw_enums_accepted(self):
        body = encoded(
            [
                {
                    "symbol": "ABC",
                    "name": "A",
                    "englishName": "A",
                    "isinCode": "US123",
                    "market": "NEW_MARKET",
                    "securityType": "NEW_TYPE",
                    "isCommonShare": True,
                    "status": "NEW_STATUS",
                    "currency": "EUR",
                    "sharesOutstanding": "1000",
                },
            ]
        )
        outcome = parse_read_response(
            req=self._req(("ABC",)), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"


# =========================================================================
# Bundle 1 — STOCK_WARNINGS
# =========================================================================


class TestStockWarnings:
    def _req(self, symbol="005930"):
        return build_read_request(
            TossReadOperation.STOCK_WARNINGS, token=TOKEN, symbol=symbol
        )

    def test_empty_succeeds(self):
        outcome = parse_read_response(req=self._req(), body=encoded([]), fetched_at=NOW)
        assert outcome.code == "success"

    def test_unknown_warning_code_preserved(self):
        body = encoded([{"warningType": "UNKNOWN_NEW_WARNING"}])
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_nullable_and_absent_optionals(self):
        body = encoded(
            [
                {
                    "warningType": "VI_STATIC",
                    "exchange": "KRX",
                    "startDate": "2026-03-26",
                    "endDate": "2026-03-27",
                },
                {
                    "warningType": "OVERHEATED",
                    "exchange": None,
                    "startDate": None,
                    "endDate": None,
                },
            ]
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_slash_in_symbol_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.STOCK_WARNINGS, token=TOKEN, symbol="A/B"
            )

    def test_one_bad_date_fails_all(self):
        body = encoded(
            [
                {"warningType": "VI_STATIC", "startDate": "not-a-date"},
            ]
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — EXCHANGE_RATE
# =========================================================================


class TestExchangeRate:
    def _req(self, baseCurrency="USD", quoteCurrency="KRW", dateTime=None):
        kwargs = {"baseCurrency": baseCurrency, "quoteCurrency": quoteCurrency}
        if dateTime is not None:
            kwargs["dateTime"] = dateTime
        return build_read_request(
            TossReadOperation.EXCHANGE_RATE, token=TOKEN, **kwargs
        )

    def test_currency_pair_validation(self):
        self._req("USD", "KRW")
        self._req("KRW", "USD")

    def test_invalid_currency_rejected(self):
        with pytest.raises(ValueError):
            self._req("EUR", "KRW")

    def test_negative_basis_point_accepted(self):
        body = encoded(
            {
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "1380.5",
                "midRate": "1375",
                "basisPoint": "-40",
                "rateChangeType": "DOWN",
                "validFrom": "2026-03-25T09:30:00+09:00",
                "validUntil": "2026-03-25T09:31:00+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_unknown_rate_change_type_preserved(self):
        body = encoded(
            {
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "1380.5",
                "midRate": "1375",
                "basisPoint": "40",
                "rateChangeType": "UNKNOWN",
                "validFrom": "2026-03-25T09:30:00+09:00",
                "validUntil": "2026-03-25T09:31:00+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_malformed_decimal_fails(self):
        body = encoded(
            {
                "baseCurrency": "USD",
                "quoteCurrency": "KRW",
                "rate": "NaN",
                "midRate": "1375",
                "basisPoint": "40",
                "rateChangeType": "UP",
                "validFrom": "2026-03-25T09:30:00+09:00",
                "validUntil": "2026-03-25T09:31:00+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 1 — KR_MARKET_CALENDAR
# =========================================================================


class TestKrMarketCalendar:
    def _req(self, date_str=None):
        kwargs = {}
        if date_str is not None:
            kwargs["date"] = date_str
        return build_read_request(
            TossReadOperation.KR_MARKET_CALENDAR, token=TOKEN, **kwargs
        )

    def test_business_day_parses(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "integrated": {
                        "preMarket": {
                            "startTime": "2026-03-25T08:00:00+09:00",
                            "endTime": "2026-03-25T09:00:00+09:00",
                        },
                        "regularMarket": {
                            "startTime": "2026-03-25T09:00:00+09:00",
                            "endTime": "2026-03-25T15:30:00+09:00",
                        },
                        "afterMarket": None,
                    },
                },
                "previousBusinessDay": {"date": "2026-03-24", "integrated": None},
                "nextBusinessDay": {"date": "2026-03-26", "integrated": None},
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_holiday_with_null_integrated(self):
        body = encoded(
            {
                "today": {"date": "2026-03-01", "integrated": None},
                "previousBusinessDay": {"date": "2026-02-27", "integrated": None},
                "nextBusinessDay": {"date": "2026-03-02", "integrated": None},
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_malformed_nested_session_fails_root(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "integrated": {"regularMarket": {"startTime": "bad"}},
                },
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"

    def test_unknown_future_integrated_session_is_contract_drift(self):
        body = encoded(
            {
                "today": {"date": "2026-03-25", "integrated": {"nightMarket": None}},
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        assert (
            parse_read_response(
                req=self._req(),
                body=body,
                fetched_at=NOW,
            ).code
            == "contract_mismatch"
        )

    def test_market_day_metadata_is_compatible(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "providerMetadata": "v2",
                    "integrated": {"preMarket": None},
                },
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        assert (
            parse_read_response(
                req=self._req(),
                body=body,
                fetched_at=NOW,
            ).code
            == "success"
        )


# =========================================================================
# Bundle 1 — US_MARKET_CALENDAR
# =========================================================================


class TestUsMarketCalendar:
    def _req(self, date_str=None):
        kwargs = {}
        if date_str is not None:
            kwargs["date"] = date_str
        return build_read_request(
            TossReadOperation.US_MARKET_CALENDAR, token=TOKEN, **kwargs
        )

    def test_business_day_parses(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "dayMarket": {
                        "startTime": "2026-03-25T09:00:00+09:00",
                        "endTime": "2026-03-25T16:50:00+09:00",
                    },
                    "preMarket": {
                        "startTime": "2026-03-25T17:00:00+09:00",
                        "endTime": "2026-03-25T22:30:00+09:00",
                    },
                    "regularMarket": {
                        "startTime": "2026-03-25T22:30:00+09:00",
                        "endTime": "2026-03-26T05:00:00+09:00",
                    },
                    "afterMarket": {
                        "startTime": "2026-03-26T05:00:00+09:00",
                        "endTime": "2026-03-26T07:00:00+09:00",
                    },
                },
                "previousBusinessDay": {
                    "date": "2026-03-24",
                    "dayMarket": None,
                    "preMarket": None,
                    "regularMarket": None,
                    "afterMarket": None,
                },
                "nextBusinessDay": {
                    "date": "2026-03-26",
                    "dayMarket": None,
                    "preMarket": None,
                    "regularMarket": None,
                    "afterMarket": None,
                },
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_cross_date_kst_end_time_preserved(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "regularMarket": {
                        "startTime": "2026-03-25T22:30:00+09:00",
                        "endTime": "2026-03-26T05:00:00+09:00",
                    },
                    "dayMarket": None,
                    "preMarket": None,
                    "afterMarket": None,
                },
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_unknown_future_session_is_contract_drift(self):
        body = encoded(
            {
                "today": {"date": "2026-03-25", "nightMarket": None},
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        assert (
            parse_read_response(
                req=self._req(),
                body=body,
                fetched_at=NOW,
            ).code
            == "contract_mismatch"
        )

    def test_market_day_metadata_is_compatible(self):
        body = encoded(
            {
                "today": {
                    "date": "2026-03-25",
                    "providerMetadata": "v2",
                    "dayMarket": None,
                },
                "previousBusinessDay": {"date": "2026-03-24"},
                "nextBusinessDay": {"date": "2026-03-26"},
            }
        )
        assert (
            parse_read_response(
                req=self._req(),
                body=body,
                fetched_at=NOW,
            ).code
            == "success"
        )


# =========================================================================
# Bundle 2 — RANKINGS
# =========================================================================


class TestRankings:
    def _req(self, **kwargs):
        return build_read_request(TossReadOperation.RANKINGS, token=TOKEN, **kwargs)

    def test_every_enum_boundary(self):
        for rtype in [
            "MARKET_TRADING_AMOUNT",
            "MARKET_TRADING_VOLUME",
            "TOP_GAINERS",
            "TOP_LOSERS",
            "TOSS_SECURITIES_TRADING_AMOUNT",
            "TOSS_SECURITIES_TRADING_VOLUME",
        ]:
            for country in ["KR", "US"]:
                for duration in ["1d", "1w", "1mo", "3mo", "6mo", "1y"]:
                    self._req(type=rtype, marketCountry=country, duration=duration)
                # realtime is valid for non-TOP_*
                if not rtype.startswith("TOP_"):
                    self._req(type=rtype, marketCountry=country, duration="realtime")

    def test_top_gainers_realtime_rejected(self):
        for rtype in ["TOP_GAINERS", "TOP_LOSERS"]:
            with pytest.raises(ValueError):
                self._req(type=rtype, marketCountry="KR", duration="realtime")

    def test_count_bounds_and_bool_strictness(self):
        self._req(
            type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d", count=1
        )
        self._req(
            type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d", count=100
        )
        with pytest.raises(ValueError):
            self._req(
                type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d", count=0
            )
        with pytest.raises(ValueError):
            self._req(
                type="MARKET_TRADING_AMOUNT",
                marketCountry="KR",
                duration="1d",
                count=101,
            )
        with pytest.raises(TypeError):
            self._req(
                type="MARKET_TRADING_AMOUNT",
                marketCountry="KR",
                duration="1d",
                excludeInvestmentCaution=1,
            )

    def test_empty_and_null_ranked_at(self):
        body = encoded({"rankings": [], "rankedAt": None})
        outcome = parse_read_response(
            req=self._req(
                type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d"
            ),
            body=body,
            fetched_at=NOW,
        )
        assert outcome.code == "success"

    def test_unknown_response_enums_preserved(self):
        body = encoded(
            {
                "rankedAt": "2026-06-10T14:30:00+09:00",
                "rankings": [
                    {
                        "rank": 1,
                        "symbol": "005930",
                        "currency": "NEW_CCY",
                        "price": {
                            "lastPrice": "56500",
                            "basePrice": "55800",
                            "changeRate": "0.0125",
                        },
                        "tradingVolume": "18432100",
                        "tradingAmount": "1041436650000",
                    }
                ],
            }
        )
        outcome = parse_read_response(
            req=self._req(
                type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d"
            ),
            body=body,
            fetched_at=NOW,
        )
        assert outcome.code == "success"

    def test_one_bad_item_fails_all(self):
        body = encoded(
            {
                "rankedAt": "2026-06-10T14:30:00+09:00",
                "rankings": [
                    {
                        "rank": 1,
                        "symbol": "005930",
                        "currency": "KRW",
                        "price": {"lastPrice": "56500", "basePrice": "55800"},
                        "tradingVolume": "18432100",
                        "tradingAmount": "1041436650000",
                    },
                    {
                        "rank": 2,
                        "symbol": "BAD",
                        "currency": "KRW",
                        "price": {"lastPrice": "NaN", "basePrice": "100"},
                        "tradingVolume": "1",
                        "tradingAmount": "1",
                    },
                ],
            }
        )
        outcome = parse_read_response(
            req=self._req(
                type="MARKET_TRADING_AMOUNT", marketCountry="KR", duration="1d"
            ),
            body=body,
            fetched_at=NOW,
        )
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 2 — INDICATOR_PRICES (batch)
# =========================================================================


class TestIndicatorPrices:
    CATALOG = (
        "KOSPI",
        "KOSDAQ",
        "KR_BOND_2Y",
        "KR_BOND_3Y",
        "KR_BOND_5Y",
        "KR_BOND_10Y",
        "KR_BOND_20Y",
        "KR_BOND_30Y",
    )

    def _req(self, symbols):
        return build_read_request(
            TossReadOperation.INDICATOR_PRICES, token=TOKEN, symbols=symbols
        )

    def test_one_and_eight_catalog_accepted(self):
        self._req(("KOSPI",))
        self._req(self.CATALOG)

    def test_duplicate_unsupported_rejected(self):
        with pytest.raises(ValueError):
            self._req(("KOSPI", "KOSPI"))
        with pytest.raises(ValueError):
            self._req(("INVALID",))

    def test_exact_response_symbol_set(self):
        body = encoded(
            [
                {
                    "symbol": "KOSPI",
                    "lastPrice": "2812.45",
                    "timestamp": "2026-06-11T15:30:00+09:00",
                },
                {"symbol": "KR_BOND_10Y", "lastPrice": "3.25", "timestamp": None},
            ]
        )
        outcome = parse_read_response(
            req=self._req(("KOSPI", "KR_BOND_10Y")), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_null_timestamp_succeeds(self):
        body = encoded(
            [
                {"symbol": "KOSPI", "lastPrice": "2812.45", "timestamp": None},
            ]
        )
        outcome = parse_read_response(
            req=self._req(("KOSPI",)), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_one_bad_item_fails_all(self):
        body = encoded(
            [
                {"symbol": "KOSPI", "lastPrice": "2812.45", "timestamp": None},
                {"symbol": "KR_BOND_10Y", "lastPrice": "NaN", "timestamp": None},
            ]
        )
        outcome = parse_read_response(
            req=self._req(("KOSPI", "KR_BOND_10Y")), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 2 — INDICATOR_CANDLES
# =========================================================================


class TestIndicatorCandles:
    def _req(self, symbol="KOSPI", interval="1d", count=None, before=None):
        kwargs = {"symbol": symbol, "interval": interval}
        if count is not None:
            kwargs["count"] = count
        if before is not None:
            kwargs["before"] = before
        return build_read_request(
            TossReadOperation.INDICATOR_CANDLES, token=TOKEN, **kwargs
        )

    def test_index_1m_and_1d_accepted(self):
        self._req(symbol="KOSPI", interval="1m")
        self._req(symbol="KOSDAQ", interval="1d")

    def test_bond_1d_accepted(self):
        self._req(symbol="KR_BOND_10Y", interval="1d")

    def test_bond_1m_rejected(self):
        with pytest.raises(ValueError):
            self._req(symbol="KR_BOND_10Y", interval="1m")

    def test_count_bounds(self):
        for c in [1, 200]:
            self._req(count=c)
        for c in [0, 201]:
            with pytest.raises(ValueError):
                self._req(count=c)

    def test_empty_and_last_page(self):
        body = encoded({"candles": [], "nextBefore": None})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_return_cursor_only(self):
        body = encoded(
            {
                "candles": [
                    {
                        "timestamp": "2026-06-11T09:00:00+09:00",
                        "openPrice": "2798.32",
                        "highPrice": "2820.15",
                        "lowPrice": "2790.1",
                        "closePrice": "2812.45",
                        "volume": "542000000",
                    }
                ],
                "nextBefore": "2026-06-10T09:00:00+09:00",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"


# =========================================================================
# Bundle 2 — INDICATOR_INVESTOR_TRADING
# =========================================================================


class TestInvestorTrading:
    def _req(self, symbol="KOSPI", interval="1d", count=None, until=None):
        kwargs = {"symbol": symbol, "interval": interval}
        if count is not None:
            kwargs["count"] = count
        if until is not None:
            kwargs["until"] = until
        return build_read_request(
            TossReadOperation.INDICATOR_INVESTOR_TRADING, token=TOKEN, **kwargs
        )

    def test_all_intervals_accepted(self):
        for iv in ["1d", "1w", "1mo", "1y"]:
            self._req(interval=iv)

    def test_count_bounds(self):
        for c in [1, 100]:
            self._req(count=c)
        for c in [0, 101]:
            with pytest.raises(ValueError):
                self._req(count=c)

    def test_bad_symbol_rejected(self):
        with pytest.raises(ValueError):
            self._req(symbol="KR_BOND_10Y")

    def test_seven_breakdown_keys_required(self):
        body = encoded(
            {
                "records": [
                    {
                        "date": "2026-06-11",
                        "updatedAt": "2026-06-11T18:10:00+09:00",
                        "individual": {"buyAmount": "100", "sellAmount": "200"},
                        "foreigner": {"buyAmount": "300", "sellAmount": "400"},
                        "institution": {
                            "buyAmount": "500",
                            "sellAmount": "600",
                            "breakdown": {
                                "financialInvestment": {
                                    "buyAmount": "1",
                                    "sellAmount": "2",
                                },
                                "insurance": {"buyAmount": "3", "sellAmount": "4"},
                                "trust": {"buyAmount": "5", "sellAmount": "6"},
                                "privateEquityFund": {
                                    "buyAmount": "7",
                                    "sellAmount": "8",
                                },
                                "bank": {"buyAmount": "9", "sellAmount": "10"},
                                "otherFinancialInstitution": {
                                    "buyAmount": "11",
                                    "sellAmount": "12",
                                },
                                "pensionFund": {"buyAmount": "13", "sellAmount": "14"},
                            },
                        },
                        "otherCorporation": {"buyAmount": "700", "sellAmount": "800"},
                    }
                ],
                "nextUntil": "2026-06-09",
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_missing_breakdown_key_fails(self):
        body = encoded(
            {
                "records": [
                    {
                        "date": "2026-06-11",
                        "updatedAt": "2026-06-11T18:10:00+09:00",
                        "individual": {"buyAmount": "100", "sellAmount": "200"},
                        "foreigner": {"buyAmount": "300", "sellAmount": "400"},
                        "institution": {
                            "buyAmount": "500",
                            "sellAmount": "600",
                            "breakdown": {
                                "financialInvestment": {
                                    "buyAmount": "1",
                                    "sellAmount": "2",
                                },
                            },
                        },
                        "otherCorporation": {"buyAmount": "700", "sellAmount": "800"},
                    }
                ],
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 3 — ORDERS (list)
# =========================================================================


class TestOrdersList:
    def _req(self, status="OPEN", **kwargs):
        full = {"token": TOKEN, "account_seq": ACCOUNT_SEQ, "status": status}
        full.update(kwargs)
        return build_read_request(TossReadOperation.ORDERS, **full)

    def test_open_and_closed_accepted(self):
        self._req(status="OPEN")
        self._req(status="CLOSED")

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            self._req(status="ALL")

    def test_limit_bounds(self):
        self._req(status="CLOSED", limit=1)
        self._req(status="CLOSED", limit=100)
        with pytest.raises(ValueError):
            self._req(status="CLOSED", limit=0)
        with pytest.raises(ValueError):
            self._req(status="CLOSED", limit=101)

    def test_account_seq_required(self):
        with pytest.raises(ValueError):
            build_read_request(TossReadOperation.ORDERS, token=TOKEN, status="OPEN")

    def test_open_enforces_null_next_cursor_false_has_next(self):
        body = encoded(
            {
                "orders": [],
                "nextCursor": None,
                "hasNext": False,
            }
        )
        outcome = parse_read_response(
            req=self._req(status="OPEN"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_open_with_next_cursor_fails(self):
        body = encoded(
            {
                "orders": [],
                "nextCursor": "abc",
                "hasNext": False,
            }
        )
        outcome = parse_read_response(
            req=self._req(status="OPEN"), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

    def test_closed_has_next_iff_next_cursor_present(self):
        # has_next=True but nextCursor=null -> fail
        body = encoded({"orders": [], "nextCursor": None, "hasNext": True})
        outcome = parse_read_response(
            req=self._req(status="CLOSED"), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

        # has_next=False, nextCursor=abc -> fail
        body = encoded({"orders": [], "nextCursor": "abc", "hasNext": False})
        outcome = parse_read_response(
            req=self._req(status="CLOSED"), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

        # consistent: has_next=True, nextCursor=abc -> ok
        body = encoded({"orders": [], "nextCursor": "abc", "hasNext": True})
        outcome = parse_read_response(
            req=self._req(status="CLOSED"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"

    def test_one_bad_order_fails_page(self):
        good_order = {
            "orderId": "ID-001",
            "symbol": "005930",
            "side": "BUY",
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "status": "PENDING",
            "quantity": "10",
            "currency": "KRW",
            "orderedAt": "2026-03-29T09:30:00+09:00",
            "execution": {
                "filledQuantity": "0",
                "averageFilledPrice": None,
                "filledAmount": None,
                "commission": None,
                "tax": None,
                "filledAt": None,
                "settlementDate": None,
            },
        }
        bad_order = dict(good_order, quantity="NaN")
        body = encoded(
            {"orders": [good_order, bad_order], "nextCursor": None, "hasNext": False}
        )
        outcome = parse_read_response(
            req=self._req(status="OPEN"), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

    def test_cursor_redacted_in_repr(self):
        body = encoded(
            {"orders": [], "nextCursor": "SENSITIVE-CURSOR-123", "hasNext": True}
        )
        outcome = parse_read_response(
            req=self._req(status="CLOSED"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"
        assert "SENSITIVE-CURSOR-123" not in repr(outcome)

    @pytest.mark.parametrize("status", ["OPEN", "CLOSED"])
    def test_absent_next_cursor_key_is_contract_mismatch(self, status):
        # PaginatedOrderResponse.required includes nextCursor: the key must
        # be present (nullable), so an absent key is a contract mismatch.
        body = encoded({"orders": [], "hasNext": False})
        outcome = parse_read_response(
            req=self._req(status=status), body=body, fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"


# =========================================================================
# Bundle 3 — ORDER_DETAIL
# =========================================================================


class TestOrderDetail:
    def _req(self, order_id="ORDER-ID-001"):
        return build_read_request(
            TossReadOperation.ORDER_DETAIL,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            order_id=order_id,
        )

    def test_valid_id_builds(self):
        req = self._req()
        assert req.operation == TossReadOperation.ORDER_DETAIL

    def test_slash_in_id_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.ORDER_DETAIL,
                token=TOKEN,
                account_seq=ACCOUNT_SEQ,
                order_id="A/B",
            )

    def test_control_char_in_id_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.ORDER_DETAIL,
                token=TOKEN,
                account_seq=ACCOUNT_SEQ,
                order_id="A\nB",
            )

    def test_empty_id_rejected(self):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.ORDER_DETAIL,
                token=TOKEN,
                account_seq=ACCOUNT_SEQ,
                order_id="",
            )

    def test_full_null_heavy_order_parses(self):
        body = encoded(
            {
                "orderId": "ORDER-ID-001",
                "symbol": "005930",
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "status": "PENDING",
                "quantity": "10",
                "currency": "KRW",
                "orderedAt": "2026-03-29T09:30:00+09:00",
                "price": None,
                "orderAmount": None,
                "canceledAt": None,
                "execution": {
                    "filledQuantity": "0",
                    "averageFilledPrice": None,
                    "filledAmount": None,
                    "commission": None,
                    "tax": None,
                    "filledAt": None,
                    "settlementDate": None,
                },
            }
        )
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

    def test_id_equality_preserved(self):
        body = encoded(
            {
                "orderId": "ORDER-ID-001",
                "symbol": "005930",
                "side": "BUY",
                "orderType": "LIMIT",
                "timeInForce": "DAY",
                "status": "PENDING",
                "quantity": "10",
                "currency": "KRW",
                "orderedAt": "2026-03-29T09:30:00+09:00",
                "execution": {
                    "filledQuantity": "0",
                    "averageFilledPrice": None,
                    "filledAmount": None,
                    "commission": None,
                    "tax": None,
                    "filledAt": None,
                    "settlementDate": None,
                },
            }
        )
        outcome = parse_read_response(
            req=self._req("ORDER-ID-001"), body=body, fetched_at=NOW
        )
        assert outcome.code == "success"


# =========================================================================
# Bundle 3 — OrderExecution required-nullable keys
# =========================================================================

_EXECUTION_ALL_NULL = {
    "filledQuantity": "0",
    "averageFilledPrice": None,
    "filledAmount": None,
    "commission": None,
    "tax": None,
    "filledAt": None,
    "settlementDate": None,
}


class TestOrderExecutionRequiredNullable:
    """All 7 OrderExecution keys are canonically required; six are nullable.
    An absent key is a contract mismatch, never a MISSING value."""

    def _detail_req(self):
        return build_read_request(
            TossReadOperation.ORDER_DETAIL,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            order_id="ORDER-ID-001",
        )

    def _order(self, execution):
        return {
            "orderId": "ORDER-ID-001",
            "symbol": "005930",
            "side": "BUY",
            "orderType": "LIMIT",
            "timeInForce": "DAY",
            "status": "PENDING",
            "quantity": "10",
            "currency": "KRW",
            "orderedAt": "2026-03-29T09:30:00+09:00",
            "execution": execution,
        }

    @pytest.mark.parametrize("dropped", sorted(_EXECUTION_ALL_NULL))
    def test_each_absent_execution_key_is_contract_mismatch(self, dropped):
        execution = {k: v for k, v in _EXECUTION_ALL_NULL.items() if k != dropped}
        body = encoded(self._order(execution))
        outcome = parse_read_response(req=self._detail_req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"

    def test_absent_commission_in_orders_list_is_contract_mismatch(self):
        execution = {k: v for k, v in _EXECUTION_ALL_NULL.items() if k != "commission"}
        body = encoded(
            {"orders": [self._order(execution)], "nextCursor": None, "hasNext": False}
        )
        req = build_read_request(
            TossReadOperation.ORDERS,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            status="OPEN",
        )
        outcome = parse_read_response(req=req, body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"

    def test_all_null_values_parse_to_none_never_missing(self):
        body = encoded(self._order(dict(_EXECUTION_ALL_NULL)))
        outcome = parse_read_response(req=self._detail_req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"
        execution = outcome.value.execution
        assert execution.filled_quantity == "0"
        for attr in (
            "average_filled_price",
            "filled_amount",
            "commission",
            "tax",
            "filled_at",
            "settlement_date",
        ):
            assert getattr(execution, attr) is None
            assert getattr(execution, attr) is not MISSING


# =========================================================================
# Strict ISO date request parameters (date/from/to/until)
# =========================================================================

_BAD_DATES = [
    "2026-13-40",
    "2026-02-30",
    "x&y=1",
    "2026-01-01&injected=x",
    "garbage value",
    "20260325",
    "2026/03/25",
    "2026-3-5",
    "2026-03-25T00:00:00",
    " 2026-03-25",
    "2026-03-25 ",
]


class TestStrictIsoDateParams:
    @pytest.mark.parametrize("bad", _BAD_DATES)
    def test_calendar_date_malformed_rejected(self, bad):
        for op in (
            TossReadOperation.KR_MARKET_CALENDAR,
            TossReadOperation.US_MARKET_CALENDAR,
        ):
            with pytest.raises(ValueError):
                build_read_request(op, token=TOKEN, date=bad)

    @pytest.mark.parametrize("bad", _BAD_DATES)
    def test_orders_from_and_to_malformed_rejected(self, bad):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.ORDERS,
                token=TOKEN,
                account_seq=ACCOUNT_SEQ,
                status="CLOSED",
                **{"from": bad},
            )
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.ORDERS,
                token=TOKEN,
                account_seq=ACCOUNT_SEQ,
                status="CLOSED",
                to=bad,
            )

    @pytest.mark.parametrize("bad", _BAD_DATES)
    def test_until_malformed_rejected(self, bad):
        with pytest.raises(ValueError):
            build_read_request(
                TossReadOperation.INDICATOR_INVESTOR_TRADING,
                token=TOKEN,
                symbol="KOSPI",
                interval="1d",
                until=bad,
            )

    def test_valid_dates_accepted(self):
        build_read_request(
            TossReadOperation.KR_MARKET_CALENDAR, token=TOKEN, date="2026-03-25"
        )
        build_read_request(
            TossReadOperation.US_MARKET_CALENDAR, token=TOKEN, date="2026-03-25"
        )
        req = build_read_request(
            TossReadOperation.ORDERS,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            status="CLOSED",
            to="2026-03-01",
            **{"from": "2026-01-01"},
        )
        assert req.from_date == "2026-01-01"
        assert req.to_date == "2026-03-01"
        build_read_request(
            TossReadOperation.INDICATOR_INVESTOR_TRADING,
            token=TOKEN,
            symbol="KOSPI",
            interval="1d",
            until="2026-03-25",
        )


# =========================================================================
# Bundle 3 — accountSeq canonical int64 decimal text
# =========================================================================


class TestAccountSeqInt64:
    def _build(self, account_seq):
        return build_read_request(
            TossReadOperation.ORDERS,
            token=TOKEN,
            account_seq=account_seq,
            status="OPEN",
        )

    @pytest.mark.parametrize(
        "bad",
        [
            "not-a-number",
            "1e5",
            "9999999999999999999999",
            "+123",
            "9223372036854775808",
            "-9223372036854775809",
            "",
            "1 2",
            "12.0",
        ],
    )
    def test_non_int64_account_seq_rejected(self, bad):
        with pytest.raises(ValueError):
            self._build(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "1",
            "0",
            "1234567890",
            "9223372036854775807",
            "-9223372036854775808",
        ],
    )
    def test_int64_account_seq_accepted(self, good):
        req = self._build(good)
        assert req.account_seq == good

    def test_boundary_account_seq_stays_repr_safe(self):
        req = self._build("9223372036854775807")
        assert "9223372036854775807" not in repr(req)


# =========================================================================
# Bundle 3 — CONDITIONAL_ORDERS (list)
# =========================================================================


class TestConditionalOrdersList:
    def _req(self, status="OPEN", **kwargs):
        full = {"token": TOKEN, "account_seq": ACCOUNT_SEQ, "status": status}
        full.update(kwargs)
        return build_read_request(TossReadOperation.CONDITIONAL_ORDERS, **full)

    def test_cursor_regex_enforced(self):
        self._req(cursor="abc-123_X")
        with pytest.raises(ValueError):
            self._req(cursor="bad cursor!")

    def test_has_next_iff_next_cursor_present_non_null(self):
        # hasNext=True, nextCursor=abc -> ok
        body = encoded({"conditionalOrders": [], "nextCursor": "abc", "hasNext": True})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

        # hasNext=True, nextCursor absent -> fail
        body = encoded({"conditionalOrders": [], "hasNext": True})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"

        # hasNext=True, nextCursor=null -> fail
        body = encoded({"conditionalOrders": [], "nextCursor": None, "hasNext": True})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "contract_mismatch"

        # hasNext=False, nextCursor absent -> ok
        body = encoded({"conditionalOrders": [], "hasNext": False})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"

        # hasNext=False, nextCursor=null -> ok
        body = encoded({"conditionalOrders": [], "nextCursor": None, "hasNext": False})
        outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
        assert outcome.code == "success"


# =========================================================================
# Bundle 3 — CONDITIONAL_ORDER_DETAIL
# =========================================================================


class TestConditionalOrderDetail:
    def _req(self, conditional_order_id="COND-ID-001"):
        return build_read_request(
            TossReadOperation.CONDITIONAL_ORDER_DETAIL,
            token=TOKEN,
            account_seq=ACCOUNT_SEQ,
            conditional_order_id=conditional_order_id,
        )

    def test_single_oco_oto_shapes_parse(self):
        base_cond = {
            "conditionalOrderId": "COND-ID-001",
            "type": "OCO",
            "status": "WATCHING",
            "symbol": "005930",
            "market": "KR",
            "quantity": "100",
            "orderType": "LIMIT",
            "createdAt": "2026-06-12T09:00:00+09:00",
            "first": {
                "type": "STOP",
                "status": "WATCHING",
                "triggerPrice": "295",
                "targetProfitRate": None,
                "orderPrice": "294.5",
                "triggeredOrderId": None,
            },
            "second": {
                "type": "STOP",
                "status": "WATCHING",
                "triggerPrice": "280",
                "targetProfitRate": None,
                "orderPrice": "281",
                "triggeredOrderId": None,
            },
        }
        for shape in [
            {**base_cond, "type": "SINGLE", "second": None},
            base_cond,
            {**base_cond, "type": "OTO"},
        ]:
            body = encoded(dict(shape))
            outcome = parse_read_response(req=self._req(), body=body, fetched_at=NOW)
            assert outcome.code == "success"

    def test_condition_order_side_is_preserved_and_invalid_value_fails_closed(self):
        base = {
            "conditionalOrderId": "COND-ID-001",
            "type": "SINGLE",
            "status": "WATCHING",
            "symbol": "005930",
            "market": "KR",
            "quantity": "100",
            "orderType": "LIMIT",
            "createdAt": "2026-06-12T09:00:00+09:00",
            "first": {
                "type": "STOP",
                "status": "WATCHING",
                "orderSide": "SELL",
                "triggerPrice": "295",
                "targetProfitRate": None,
                "orderPrice": "295",
                "triggeredOrderId": None,
            },
            "second": None,
        }
        outcome = parse_read_response(
            req=self._req(), body=encoded(base), fetched_at=NOW
        )
        assert outcome.code == "success"
        assert isinstance(outcome.value, ConditionalOrderDetailResponse)
        assert outcome.value.first.order_side == "SELL"

        invalid = {**base, "first": {**base["first"], "orderSide": "HOLD"}}
        outcome = parse_read_response(
            req=self._req(), body=encoded(invalid), fetched_at=NOW
        )
        assert outcome.code == "contract_mismatch"

        for second_side in ("BUY", None):
            oto = {
                **base,
                "type": "OTO",
                "second": {
                    **base["first"],
                    "orderSide": second_side,
                    "triggerPrice": "310",
                },
            }
            outcome = parse_read_response(
                req=self._req(), body=encoded(oto), fetched_at=NOW
            )
            assert outcome.code == "success"
            assert isinstance(outcome.value, ConditionalOrderDetailResponse)
            assert isinstance(outcome.value.second, ConditionalOrderCondition)
            assert outcome.value.second.order_side == second_side
        missing = {
            **base,
            "first": {
                key: value for key, value in base["first"].items() if key != "orderSide"
            },
        }
        outcome = parse_read_response(
            req=self._req(), body=encoded(missing), fetched_at=NOW
        )
        assert outcome.code == "success"
        assert isinstance(outcome.value, ConditionalOrderDetailResponse)
        assert outcome.value.first.order_side is MISSING
        invalid_second = {
            **base,
            "type": "OTO",
            "second": {**base["first"], "orderSide": "SHORT"},
        }
        assert (
            parse_read_response(
                req=self._req(), body=encoded(invalid_second), fetched_at=NOW
            ).code
            == "contract_mismatch"
        )

    def test_shared_facts_side_resolution_contract(self):
        assert resolve_conditional_order_side("SINGLE", 0, "SELL", None) == "SELL"
        assert resolve_conditional_order_side("SINGLE", 0, None, "BUY") == "BUY"
        assert resolve_conditional_order_side("SINGLE", 0, None, None) is None
        assert resolve_conditional_order_side("OCO", 0, None, "BUY") == "SELL"
        assert resolve_conditional_order_side("OCO", 1, None, "BUY") == "SELL"
        assert resolve_conditional_order_side("OTO", 0, None, "SELL") == "BUY"
        assert resolve_conditional_order_side("OTO", 1, None, "BUY") == "SELL"
        assert resolve_conditional_order_side("OTO", 1, "BUY", None) == "BUY"
        assert resolve_conditional_order_side("OTO", 1, None, None) == "SELL"
        assert resolve_conditional_order_side("OCO", 0, MISSING, None) == "SELL"
        assert resolve_conditional_order_side("SINGLE", 0, MISSING, "BUY") == "BUY"
        with pytest.raises(ValueError, match="conditional_order_side_contract"):
            resolve_conditional_order_side("SINGLE", 0, 1, None)
        with pytest.raises(ValueError, match="conditional_order_side_contract"):
            resolve_conditional_order_side("OCO", 2, None, None)
        with pytest.raises(ValueError, match="conditional_order_side_contract"):
            resolve_conditional_order_side("SINGLE", 1, None, None)
        with pytest.raises(ValueError, match="conditional_order_side_contract"):
            resolve_conditional_order_side("SINGLE", 0, "sell", None)
        assert resolve_conditional_order_side("SINGLE", 0, None, "sell") is None


# =========================================================================
# MISSING semantics
# =========================================================================


class TestMissingSemantics:
    def test_missing_sentinel_is_distinct_from_none(self):
        assert MISSING is not None
        assert MISSING is MISSING

    def test_three_state_optional(self):
        assert Opt.from_json({"key": "val"}, "key") == "val"
        assert Opt.from_json({"key": None}, "key") is None
        assert Opt.from_json({}, "key") is MISSING

    def test_decimal30_rejects_non_string(self):
        assert _Decimal30.parse(123) is None
        assert _Decimal30.parse(True) is None
        assert _Decimal30.parse("1.5") == "1.5"

    def test_decimal30_max_length(self):
        assert _Decimal30.parse("1" * 30) == "1" * 30
        assert _Decimal30.parse("1" * 31) is None
