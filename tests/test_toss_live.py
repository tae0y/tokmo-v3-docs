"""Toss data adapter boundary: exactly two GET operations, the account header
required on one and forbidden on the other, and no reachable write path."""

import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from tokmo.facts.toss import (
    ACCOUNTS_ENDPOINT_ID,
    HOLDINGS_ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    TossDataRequest,
    _TossTransportFailure,
    _TossTransportSuccess,
    build_accounts_request,
    build_holdings_request,
)
from tokmo.facts.toss_live import (
    ACCOUNT_HEADER,
    ACCOUNTS_PATH,
    HOLDINGS_PATH,
    MAX_BODY_BYTES,
    METHOD,
    TOSS_HOST,
    TossDataLiveTransport,
    build_live_opener,
)

FIXTURES = Path(__file__).parent / "fixtures"
ACCOUNTS_BODY = (FIXTURES / "toss_accounts_success.json").read_bytes()
HOLDINGS_BODY = (FIXTURES / "toss_holdings_success.json").read_bytes()
TOKEN = "SENTINEL-ACCESS-TOKEN-0001"
SEQ = "SENTINEL-SEQ-01"


class Response:
    status = 200

    def __init__(self, body=ACCOUNTS_BODY, headers=None):
        self.body = body
        self.headers = headers or Message()

    def read(self, size):
        return self.body[:size]

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class CapturingOpener:
    def __init__(self, response=None):
        self.response = Response() if response is None else response
        self.calls = []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def transport_with(response=None):
    transport = TossDataLiveTransport()
    transport.opener = CapturingOpener(response)
    return transport


def headers_of(request):
    return {name.lower(): value for name, value in request.header_items()}


# --- the two authorized operations -------------------------------------------


def test_accounts_is_a_get_without_the_account_header():
    transport = transport_with()

    result = transport(build_accounts_request(token=TOKEN))

    assert isinstance(result, _TossTransportSuccess)
    request, timeout = transport.opener.calls[0]
    assert len(transport.opener.calls) == 1
    assert request.full_url == "https://openapi.tossinvest.com/api/v1/accounts"
    assert request.full_url == f"https://{TOSS_HOST}{ACCOUNTS_PATH}"
    assert request.get_method() == METHOD == "GET"
    assert timeout == REQUEST_TIMEOUT_SECONDS
    assert request.data is None
    assert headers_of(request) == {
        "authorization": f"Bearer {TOKEN}",
        "accept-encoding": "identity",
    }
    assert ACCOUNT_HEADER.lower() not in headers_of(request)


def test_holdings_is_a_get_carrying_the_account_header():
    transport = transport_with(Response(HOLDINGS_BODY))

    transport(build_holdings_request(token=TOKEN, account_seq=SEQ))

    request, _timeout = transport.opener.calls[0]
    assert request.full_url == "https://openapi.tossinvest.com/api/v1/holdings"
    assert request.full_url == f"https://{TOSS_HOST}{HOLDINGS_PATH}"
    assert request.get_method() == "GET"
    assert headers_of(request) == {
        "authorization": f"Bearer {TOKEN}",
        "accept-encoding": "identity",
        ACCOUNT_HEADER.lower(): SEQ,
    }


def test_neither_operation_ever_carries_a_query_string_or_body():
    transport = transport_with()
    transport(build_accounts_request(token=TOKEN))
    transport(build_holdings_request(token=TOKEN, account_seq=SEQ))

    for request, _timeout in transport.opener.calls:
        assert "?" not in request.full_url
        assert request.data is None


def test_the_opener_registers_no_proxy_and_follows_no_redirect():
    """The empty ProxyHandler registers no protocol method, so its absence
    from the handler chain is exactly the evidence that no proxy is consulted."""
    opener = build_live_opener()

    assert not any(
        type(handler).__name__ == "ProxyHandler" for handler in opener.handlers
    )
    redirect = next(
        h for h in opener.handlers if isinstance(h, urllib.request.HTTPRedirectHandler)
    )
    assert redirect.redirect_request(None, None, 302, "", None, "https://evil") is None


# --- write paths and header misuse are unreachable before I/O ----------------


@pytest.mark.parametrize(
    "endpoint_id",
    [
        "toss_orders",
        "toss_conditional_orders",
        "toss_oauth_token",
        "toss_buying_power",
        "",
        "TOSS_ACCOUNTS",
        "toss_accounts/../orders",
    ],
)
def test_no_operation_outside_the_two_reads_can_be_constructed(endpoint_id):
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=endpoint_id,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            token=TOKEN,
            account_seq=None,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


def test_accounts_with_an_account_sequence_is_refused_before_any_wire_call():
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=ACCOUNTS_ENDPOINT_ID,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            token=TOKEN,
            account_seq=SEQ,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


def test_holdings_without_an_account_sequence_is_refused_before_any_wire_call():
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=HOLDINGS_ENDPOINT_ID,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            token=TOKEN,
            account_seq=None,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


@pytest.mark.parametrize(
    "seq",
    [
        "",
        "seq with space",
        "seq\nX-Injected: 1",
        "seq\rX: 1",
        "s" * 65,
        " seq",
        12345,
        None,
    ],
)
def test_unusable_account_sequences_never_reach_a_header(seq):
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=HOLDINGS_ENDPOINT_ID,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            token=TOKEN,
            account_seq=seq,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


@pytest.mark.parametrize("token", ["", "tok en", "tok\nen", None, 1, "t" * 4097])
def test_unusable_tokens_never_reach_a_header(token):
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=ACCOUNTS_ENDPOINT_ID,
            timeout_seconds=REQUEST_TIMEOUT_SECONDS,
            token=token,
            account_seq=None,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


def test_a_deviating_timeout_is_refused_before_any_wire_call():
    transport = transport_with()

    result = transport(
        TossDataRequest(
            endpoint_id=ACCOUNTS_ENDPOINT_ID,
            timeout_seconds=600.0,
            token=TOKEN,
            account_seq=None,
        )
    )

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


# --- closed failure mapping --------------------------------------------------


@pytest.mark.parametrize(
    "status,expected",
    [
        (400, "bad_request"),
        (401, "auth_rejection"),
        (403, "forbidden"),
        (404, "bad_request"),
        (409, "bad_request"),
        (500, "http_5xx"),
        (302, "redirect"),
        (308, "redirect"),
    ],
)
def test_http_status_maps_to_a_closed_code(status, expected):
    error = urllib.error.HTTPError("masked", status, "masked", Message(), None)

    result = transport_with(error)(build_accounts_request(token=TOKEN))

    assert result.code == expected


def test_rate_limiting_carries_only_a_bounded_retry_after():
    headers = Message()
    headers["Retry-After"] = "99999"
    error = urllib.error.HTTPError("masked", 429, "masked", headers, None)

    result = transport_with(error)(build_accounts_request(token=TOKEN))

    assert result.code == "http_429"
    assert result.retry_after == 3600


def test_rate_limit_remaining_is_captured_as_a_bounded_integer():
    headers = Message()
    headers["X-RateLimit-Remaining"] = "4"
    plain = transport_with(Response(ACCOUNTS_BODY))
    counted = transport_with(Response(ACCOUNTS_BODY, headers))

    assert counted(build_accounts_request(token=TOKEN)).rate_limit_remaining == 4
    assert plain(build_accounts_request(token=TOKEN)).rate_limit_remaining is None


def test_a_malformed_rate_limit_header_is_dropped_not_propagated():
    headers = Message()
    headers["X-RateLimit-Remaining"] = "many"

    result = transport_with(Response(ACCOUNTS_BODY, headers))(
        build_accounts_request(token=TOKEN)
    )

    assert result.rate_limit_remaining is None


def test_transport_faults_map_to_closed_codes():
    timeout = urllib.error.URLError(TimeoutError())
    refused = urllib.error.URLError("masked")
    request = build_accounts_request(token=TOKEN)

    assert transport_with(timeout)(request).code == "timeout"
    assert transport_with(refused)(request).code == "connection_failure"
    assert transport_with(RuntimeError("boom"))(request).code == "protocol_failure"


def test_oversized_and_reencoded_bodies_are_refused():
    oversized = Response(b"x" * (MAX_BODY_BYTES + 1))
    gzipped = Response(ACCOUNTS_BODY)
    gzipped.headers["Content-Encoding"] = "gzip"
    request = build_accounts_request(token=TOKEN)

    assert transport_with(oversized)(request).code == "body_too_large"
    assert transport_with(gzipped)(request).code == "unsupported_content_encoding"


def test_a_declared_oversize_content_length_is_refused_before_reading():
    headers = Message()
    headers["Content-Length"] = str(MAX_BODY_BYTES + 1)

    result = transport_with(Response(ACCOUNTS_BODY, headers))(
        build_accounts_request(token=TOKEN)
    )

    assert result.code == "body_too_large"


# --- redaction ---------------------------------------------------------------


def test_token_and_account_sequence_stay_out_of_representations():
    request = build_holdings_request(token=TOKEN, account_seq=SEQ)
    error = urllib.error.HTTPError(
        "masked", 403, '{"code":"forbidden"}', Message(), None
    )
    failure = transport_with(error)(request)

    for text in (repr(request), str(request), repr(failure), str(failure)):
        assert TOKEN not in text and SEQ not in text
    assert "forbidden" == failure.code
    assert "api/v1" not in repr(failure)
