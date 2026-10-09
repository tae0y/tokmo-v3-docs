import json
import urllib.error
import urllib.request
from datetime import UTC, datetime
from email.message import Message

import pytest

from tokmo.facts.toss import _TossTransportFailure, _TossTransportSuccess
from tokmo.facts.toss_prices import (
    PRICES_ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    TossPricesRequest,
    build_prices_request,
    query_current_prices,
)
from tokmo.facts.toss_prices_live import (
    MAX_BODY_BYTES,
    METHOD,
    TossPricesLiveTransport,
    build_live_opener,
)

TOKEN = "SYNTHETIC-PRICE-TOKEN-0001"
NOW = datetime(2026, 7, 28, 3, 0, tzinfo=UTC)


class Response:
    status = 200

    def __init__(self, body=b'{"result":[]}', headers=None):
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
    transport = TossPricesLiveTransport()
    transport.opener = CapturingOpener(response)
    return transport


def test_request_validation_preserves_case_and_requires_1_to_200_unique_symbols():
    request = build_prices_request(symbols=("Abc.1", "x-y"), token=TOKEN)
    assert request.symbols == ("Abc.1", "x-y")

    invalid = [
        (),
        tuple(f"S{i}" for i in range(201)),
        ("ABC", "ABC"),
        ("A,B",),
        ("A B",),
        ("한글",),
        ("",),
        ["ABC"],
    ]
    for symbols in invalid:
        with pytest.raises(ValueError, match="invalid_symbols"):
            build_prices_request(symbols=symbols, token=TOKEN)


def test_wire_is_exact_get_with_literal_comma_and_bearer_only_headers():
    transport = transport_with(Response())

    outcome = transport(build_prices_request(symbols=("Abc.1", "x-y"), token=TOKEN))

    assert isinstance(outcome, _TossTransportSuccess)
    assert len(transport.opener.calls) == 1
    wire, timeout = transport.opener.calls[0]
    assert wire.full_url == (
        "https://openapi.tossinvest.com/api/v1/prices?symbols=Abc.1,x-y"
    )
    assert wire.get_method() == METHOD == "GET"
    assert wire.data is None
    assert timeout == REQUEST_TIMEOUT_SECONDS
    headers = {key.lower(): value for key, value in wire.header_items()}
    assert headers == {
        "authorization": f"Bearer {TOKEN}",
        "accept-encoding": "identity",
    }
    assert "x-tossinvest-account" not in headers


def test_closed_endpoint_identity_and_invalid_token_never_reach_io():
    transport = transport_with(Response())
    requests = [
        TossPricesRequest("toss_orders", REQUEST_TIMEOUT_SECONDS, ("ABC",), TOKEN),
        TossPricesRequest(PRICES_ENDPOINT_ID, 999, ("ABC",), TOKEN),
        TossPricesRequest(PRICES_ENDPOINT_ID, REQUEST_TIMEOUT_SECONDS, ("ABC",), ""),
        TossPricesRequest(
            PRICES_ENDPOINT_ID, REQUEST_TIMEOUT_SECONDS, ("ABC",), "bad\nheader"
        ),
    ]
    for request in requests:
        assert transport(request).code == "protocol_failure"
    assert transport.opener.calls == []
    assert TOKEN not in repr(requests[0])


def test_opener_disables_proxy_discovery_and_redirect_following():
    opener = build_live_opener()
    assert not any(
        type(handler).__name__ == "ProxyHandler" for handler in opener.handlers
    )
    redirect = next(
        handler
        for handler in opener.handlers
        if isinstance(handler, urllib.request.HTTPRedirectHandler)
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
def test_http_failures_map_to_closed_codes_without_retry(status, code):
    headers = Message()
    headers["Retry-After"] = "12"
    error = urllib.error.HTTPError("masked", status, "masked", headers, None)
    transport = transport_with(error)

    outcome = transport(build_prices_request(symbols=("ABC",), token=TOKEN))

    assert outcome.code == code
    assert len(transport.opener.calls) == 1
    assert outcome.retry_after == (12 if status == 429 else None)
    assert TOKEN not in repr(outcome)
    assert "masked" not in repr(outcome)


def test_body_encoding_and_numeric_rate_limit_are_bounded():
    too_large = transport_with(Response(b"x" * (MAX_BODY_BYTES + 1)))
    assert (
        too_large(build_prices_request(symbols=("ABC",), token=TOKEN)).code
        == "body_too_large"
    )

    headers = Message()
    headers["Content-Encoding"] = "gzip"
    encoded = transport_with(Response(headers=headers))
    assert (
        encoded(build_prices_request(symbols=("ABC",), token=TOKEN)).code
        == "unsupported_content_encoding"
    )

    headers = Message()
    headers["X-RateLimit-Remaining"] = "7"
    success = transport_with(Response(headers=headers))
    assert (
        success(
            build_prices_request(symbols=("ABC",), token=TOKEN)
        ).rate_limit_remaining
        == 7
    )


def test_query_starts_once_and_returns_all_facts_or_closed_failure():
    body = json.dumps(
        {
            "result": [
                {
                    "symbol": "Abc",
                    "lastPrice": "12.30",
                    "currency": "FUT",
                    "timestamp": None,
                }
            ]
        }
    ).encode()
    calls = []

    def success(request):
        calls.append(request)
        return _TossTransportSuccess(body, rate_limit_remaining=6)

    outcome = query_current_prices(
        symbols=("Abc",), token=TOKEN, transport=success, clock=lambda: NOW
    )
    assert outcome.code == "success"
    assert len(outcome.facts) == 1
    assert outcome.rate_limit_remaining == 6
    assert len(calls) == 1

    calls.clear()

    def failure(request):
        calls.append(request)
        return _TossTransportFailure("timeout")

    outcome = query_current_prices(
        symbols=("Abc",), token=TOKEN, transport=failure, clock=lambda: NOW
    )
    assert outcome.code == "timeout"
    assert outcome.facts == ()
    assert len(calls) == 1
