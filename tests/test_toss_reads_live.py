"""Wire-exact live tests for the 17 remaining Toss reads.

Verifies exact URLs/queries/headers, GET-only routing, body/error
hardening, no I/O on invalid or excluded identities, and body cap
boundary behavior.
"""

import json
import urllib.error
import urllib.request
from datetime import UTC, datetime, timedelta, timezone
from email.message import Message

import pytest

from tokmo.facts.toss import _TossTransportSuccess
from tokmo.facts.toss_reads import (
    REQUEST_TIMEOUT_SECONDS,
    TossReadOperation,
    build_read_request,
    query_read,
)
from tokmo.facts.toss_reads_live import (
    ACCOUNT_HEADER,
    MAX_BODY_BYTES,
    TossReadsLiveTransport,
    build_live_opener,
)

TOKEN = "SYNTHETIC-READ-TOKEN-0001"
ACCOUNT_SEQ = "1234567890"
NOW = datetime(2026, 7, 28, 3, 0, tzinfo=UTC)
KST = timezone(timedelta(hours=9))


class Response:
    status = 200

    def __init__(self, body=b'{"result":{}}', headers=None):
        self.body = body
        self.headers = headers or Message()

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class CapturingOpener:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def transport_with(response):
    transport = TossReadsLiveTransport()
    transport.opener = CapturingOpener(response)
    return transport


# --- wire-exact URLs -----------------------------------------------------


def test_orderbook_url_and_headers():
    transport = transport_with(Response())
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    transport(req)
    wire, timeout = transport.opener.calls[0]
    assert (
        wire.full_url == "https://openapi.tossinvest.com/api/v1/orderbook?symbol=005930"
    )
    assert wire.get_method() == "GET"
    assert wire.data is None
    assert timeout == REQUEST_TIMEOUT_SECONDS
    headers = {k.lower(): v for k, v in wire.header_items()}
    assert headers == {
        "authorization": f"Bearer {TOKEN}",
        "accept-encoding": "identity",
    }
    assert ACCOUNT_HEADER.lower() not in headers


def test_trades_url_with_count():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.TRADES, token=TOKEN, symbol="005930", count=10
    )
    transport(req)
    wire = transport.opener.calls[0][0]
    assert "count=10" in wire.full_url
    assert "symbol=005930" in wire.full_url


def test_candles_url_with_interval_and_count():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.STOCK_CANDLES,
        token=TOKEN,
        symbol="005930",
        interval="1d",
        count=5,
        adjusted=True,
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "interval=1d" in url
    assert "count=5" in url
    assert "adjusted=true" in url


def test_candles_before_datetime_encoded_plus():
    transport = transport_with(Response())
    before = datetime(2026, 3, 25, 9, 30, 0, tzinfo=KST)
    req = build_read_request(
        TossReadOperation.STOCK_CANDLES,
        token=TOKEN,
        symbol="005930",
        interval="1d",
        before=before,
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "%2B" in url  # + encoded as %2B


def test_stocks_batch_url():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.STOCKS, token=TOKEN, symbols=("005930", "AAPL")
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "symbols=005930,AAPL" in url


def test_stock_warnings_path_encoding():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.STOCK_WARNINGS, token=TOKEN, symbol="A.B-C"
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "/api/v1/stocks/A.B-C/warnings" in url


def test_exchange_rate_url():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.EXCHANGE_RATE,
        token=TOKEN,
        baseCurrency="USD",
        quoteCurrency="KRW",
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "baseCurrency=USD" in url
    assert "quoteCurrency=KRW" in url


def test_market_calendar_kr_url():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.KR_MARKET_CALENDAR, token=TOKEN, date="2026-03-25"
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert url.endswith("/api/v1/market-calendar/KR?date=2026-03-25")


def test_rankings_url():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.RANKINGS,
        token=TOKEN,
        type="MARKET_TRADING_AMOUNT",
        marketCountry="KR",
        duration="1d",
        count=5,
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "type=MARKET_TRADING_AMOUNT" in url
    assert "marketCountry=KR" in url
    assert "duration=1d" in url
    assert "count=5" in url


def test_indicator_candles_path():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.INDICATOR_CANDLES,
        token=TOKEN,
        symbol="KR_BOND_10Y",
        interval="1d",
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert "/api/v1/market-indicators/KR_BOND_10Y/candles" in url


def test_orders_url_with_account_header():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.ORDERS, token=TOKEN, account_seq=ACCOUNT_SEQ, status="OPEN"
    )
    transport(req)
    wire = transport.opener.calls[0][0]
    assert wire.full_url == "https://openapi.tossinvest.com/api/v1/orders?status=OPEN"
    headers = {k.lower(): v for k, v in wire.header_items()}
    assert headers["x-tossinvest-account"] == ACCOUNT_SEQ


def test_order_detail_path():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.ORDER_DETAIL,
        token=TOKEN,
        account_seq=ACCOUNT_SEQ,
        order_id="ORDER-ID-001",
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert url.endswith("/api/v1/orders/ORDER-ID-001")
    headers = {k.lower(): v for k, v in transport.opener.calls[0][0].header_items()}
    assert headers["x-tossinvest-account"] == ACCOUNT_SEQ


def test_conditional_order_detail_path():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.CONDITIONAL_ORDER_DETAIL,
        token=TOKEN,
        account_seq=ACCOUNT_SEQ,
        conditional_order_id="COND-001",
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert url.endswith("/api/v1/conditional-orders/COND-001")


# --- no account header for bundle 1-2 ------------------------------------


@pytest.mark.parametrize(
    "op,symbol",
    [
        (TossReadOperation.ORDERBOOK, "005930"),
        (TossReadOperation.TRADES, "005930"),
        (TossReadOperation.PRICE_LIMITS, "005930"),
        (TossReadOperation.STOCK_WARNINGS, "005930"),
    ],
)
def test_bundle12_no_account_header(op, symbol):
    transport = transport_with(Response())
    req = build_read_request(op, token=TOKEN, symbol=symbol)
    transport(req)
    headers = {k.lower(): v for k, v in transport.opener.calls[0][0].header_items()}
    assert "x-tossinvest-account" not in headers


# --- account header rule: wrong direction rejected before I/O ------------


def test_bundle3_without_account_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.ORDERS,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        account_seq=None,
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []


def test_bundle12_with_account_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.ORDERBOOK,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        account_seq=ACCOUNT_SEQ,
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []


# --- opener hardening ----------------------------------------------------


def test_opener_disables_proxy_and_redirect():
    opener = build_live_opener()
    assert not any(type(h).__name__ == "ProxyHandler" for h in opener.handlers)
    redirect = next(
        h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    assert (
        redirect.redirect_request(None, None, 302, "", None, "https://invalid.example")
        is None
    )


@pytest.mark.parametrize(
    "status,code",
    [
        (400, "bad_request"),
        (401, "auth_rejection"),
        (403, "forbidden"),
        (429, "http_429"),
        (500, "http_5xx"),
        (302, "redirect"),
    ],
)
def test_http_failures_map_to_closed_codes(status, code):
    headers = Message()
    headers["Retry-After"] = "12"
    error = urllib.error.HTTPError("masked", status, "masked", headers, None)
    transport = transport_with(error)
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="X")
    outcome = transport(req)
    assert outcome.code == code
    assert len(transport.opener.calls) == 1
    assert outcome.retry_after == (12 if status == 429 else None)
    assert TOKEN not in repr(outcome)
    assert "masked" not in repr(outcome)


def test_body_too_large_rejected():
    transport = transport_with(Response(b"x" * (MAX_BODY_BYTES + 1)))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="X")
    assert transport(req).code == "body_too_large"


def test_non_identity_encoding_rejected():
    headers = Message()
    headers["Content-Encoding"] = "gzip"
    transport = transport_with(Response(headers=headers))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="X")
    assert transport(req).code == "unsupported_content_encoding"


def test_rate_limit_header_bounded():
    headers = Message()
    headers["X-RateLimit-Remaining"] = "7"
    transport = transport_with(Response(headers=headers))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="X")
    outcome = transport(req)
    assert isinstance(outcome, _TossTransportSuccess)
    assert outcome.rate_limit_remaining == 7


def test_exactly_one_call_per_query():
    body = json.dumps({"result": {"currency": "KRW", "asks": [], "bids": []}}).encode()
    calls = []

    def success(request):
        calls.append(request)
        return _TossTransportSuccess(body)

    outcome = query_read(
        build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930"),
        transport=success,
        clock=lambda: NOW,
    )
    assert outcome.code == "success"
    assert len(calls) == 1


def test_no_second_call_when_next_before_present():
    """Pagination metadata must not trigger a follow-up call."""
    body = json.dumps(
        {
            "result": {
                "candles": [],
                "nextBefore": "2026-03-24T09:00:00+09:00",
            }
        }
    ).encode()
    calls = []

    def success(request):
        calls.append(request)
        return _TossTransportSuccess(body)

    outcome = query_read(
        build_read_request(
            TossReadOperation.STOCK_CANDLES, token=TOKEN, symbol="005930", interval="1d"
        ),
        transport=success,
        clock=lambda: NOW,
    )
    assert outcome.code == "success"
    assert len(calls) == 1


def test_invalid_token_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.ORDERBOOK,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token="bad\nheader",
    )
    assert transport(req).code == "protocol_failure"
    assert transport.opener.calls == []


def test_token_redacted_from_outcome():
    body = json.dumps({"result": {"currency": "KRW", "asks": [], "bids": []}}).encode()
    transport = transport_with(Response(body))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    outcome = transport(req)
    assert TOKEN not in repr(outcome)


# --- strict date params revalidated at the wire; injection refused --------


def test_calendar_invalid_date_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.KR_MARKET_CALENDAR,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        date="NOT-A-DATE&evil=1",
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []


def test_orders_from_injection_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.ORDERS,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        account_seq=ACCOUNT_SEQ,
        status="CLOSED",
        from_date="2026-01-01&injected=x",
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []


def test_investor_trading_invalid_until_no_io():
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.INDICATOR_INVESTOR_TRADING,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        symbol="KOSPI",
        interval="1d",
        until="garbage value",
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []


def test_orders_valid_dates_roundtrip_without_extra_keys():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.ORDERS,
        token=TOKEN,
        account_seq=ACCOUNT_SEQ,
        status="CLOSED",
        to="2026-03-01",
        **{"from": "2026-01-01"},
    )
    transport(req)
    wire = transport.opener.calls[0][0]
    assert wire.full_url == (
        "https://openapi.tossinvest.com/api/v1/orders"
        "?status=CLOSED&from=2026-01-01&to=2026-03-01"
    )


def test_investor_trading_valid_until_roundtrip():
    transport = transport_with(Response())
    req = build_read_request(
        TossReadOperation.INDICATOR_INVESTOR_TRADING,
        token=TOKEN,
        symbol="KOSPI",
        interval="1d",
        until="2026-03-25",
    )
    transport(req)
    url = transport.opener.calls[0][0].full_url
    assert url.endswith(
        "/api/v1/market-indicators/KOSPI/investor-trading?interval=1d&until=2026-03-25"
    )


# --- accountSeq int64 revalidated at the wire ------------------------------


@pytest.mark.parametrize(
    "bad_seq",
    [
        "not-a-number",
        "1e5",
        "9999999999999999999999",
        "+123",
        "9223372036854775808",
    ],
)
def test_non_int64_account_seq_no_io(bad_seq):
    from tokmo.facts.toss_reads import TossReadRequest

    transport = transport_with(Response())
    req = TossReadRequest(
        operation=TossReadOperation.ORDERS,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        token=TOKEN,
        account_seq=bad_seq,
        status="OPEN",
    )
    outcome = transport(req)
    assert outcome.code == "protocol_failure"
    assert transport.opener.calls == []
