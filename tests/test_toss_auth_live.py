"""Toss auth adapter boundary: exactly one authorized token operation, a
hard-coded grant, a three-key form allowlist, and closed failure codes that
never carry credential, URL, or provider text."""

import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

import pytest

from tokmo.facts.toss import (
    GRANT_TYPE,
    REQUEST_TIMEOUT_SECONDS,
    TOKEN_FORM_KEYS,
    TossAuthRequest,
    _TossTransportFailure,
    _TossTransportSuccess,
    build_auth_request,
)
from tokmo.facts.toss_auth_live import (
    MAX_BODY_BYTES, TOKEN_PATH, TOSS_HOST, TossAuthLiveTransport, build_live_opener,
)

FIXTURES = Path(__file__).parent / "fixtures"
TOKEN_BODY = (FIXTURES / "toss_token_success.json").read_bytes()
CLIENT_ID = "SENTINEL-CLIENT-ID-0001"
CLIENT_SECRET = "SENTINEL-CLIENT-SECRET-0001"
TOKEN_URL = "https://openapi.tossinvest.com/oauth2/token"


class Response:
    status = 200

    def __init__(self, body=TOKEN_BODY, headers=None):
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
    transport = TossAuthLiveTransport()
    transport.opener = CapturingOpener(response)
    return transport


def auth_request(**overrides):
    base = dict(client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    base.update(overrides)
    return build_auth_request(**base)


# --- the single authorized operation ----------------------------------------

def test_the_only_wire_operation_is_the_official_token_endpoint():
    transport = transport_with()

    result = transport(auth_request())

    assert isinstance(result, _TossTransportSuccess)
    request, timeout = transport.opener.calls[0]
    assert len(transport.opener.calls) == 1
    assert request.full_url == TOKEN_URL
    assert request.full_url == f"https://{TOSS_HOST}{TOKEN_PATH}"
    assert timeout == REQUEST_TIMEOUT_SECONDS


def test_the_form_carries_the_hard_coded_grant_and_nothing_beyond_the_allowlist():
    transport = transport_with()

    transport(auth_request())

    request, _timeout = transport.opener.calls[0]
    pairs = [pair.split("=", 1) for pair in request.data.decode().split("&")]
    assert {key for key, _value in pairs} == TOKEN_FORM_KEYS
    assert dict(pairs)["grant_type"] == GRANT_TYPE == "client_credentials"
    assert "?" not in request.full_url


def test_the_token_request_sends_form_encoding_identity_and_no_authorization():
    transport = transport_with()

    transport(auth_request())

    request, _timeout = transport.opener.calls[0]
    headers = {name.lower(): value for name, value in request.header_items()}
    assert headers == {
        "content-type": "application/x-www-form-urlencoded",
        "accept-encoding": "identity",
    }


def test_the_opener_registers_no_proxy_and_follows_no_redirect():
    """The empty ProxyHandler registers no protocol method, so its absence
    from the handler chain is exactly the evidence that no proxy is consulted."""
    opener = build_live_opener()

    assert not any(type(handler).__name__ == "ProxyHandler"
                   for handler in opener.handlers)
    redirect = next(h for h in opener.handlers
                    if isinstance(h, urllib.request.HTTPRedirectHandler))
    assert redirect.redirect_request(None, None, 302, "", None, "https://evil") is None
    error = urllib.error.HTTPError("masked", 302, "masked", Message(), None)
    assert transport_with(error)(auth_request()).code == "redirect"


# --- pre-I/O refusals: nothing reaches the opener ----------------------------

@pytest.mark.parametrize("overrides", [
    {"client_id": ""},
    {"client_secret": ""},
    {"client_id": "with\ttab"},
    {"client_secret": "line\nbreak"},
    {"client_secret": "s" * 513},
    {"client_id": None},
    {"client_secret": 1234},
])
def test_unusable_credentials_fail_before_any_wire_call(overrides):
    transport = transport_with()

    result = transport(auth_request(**overrides))

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


@pytest.mark.parametrize("request_kwargs", [
    {"endpoint_id": "toss_orders"},
    {"endpoint_id": "toss_accounts"},
    {"endpoint_id": ""},
    {"timeout_seconds": 600.0},
])
def test_any_request_identity_other_than_the_token_operation_is_refused(request_kwargs):
    fields = dict(endpoint_id="toss_oauth_token",
                  timeout_seconds=REQUEST_TIMEOUT_SECONDS,
                  client_id=CLIENT_ID, client_secret=CLIENT_SECRET)
    fields.update(request_kwargs)
    transport = transport_with()

    result = transport(TossAuthRequest(**fields))

    assert result == _TossTransportFailure(code="protocol_failure")
    assert transport.opener.calls == []


# --- closed failure mapping --------------------------------------------------

@pytest.mark.parametrize("status,expected", [
    (400, "bad_request"), (401, "auth_rejection"), (403, "forbidden"),
    (404, "bad_request"), (500, "http_5xx"), (503, "http_5xx"),
    (301, "redirect"), (302, "redirect"), (307, "redirect"),
])
def test_http_status_maps_to_a_closed_code(status, expected):
    error = urllib.error.HTTPError("masked", status, "masked", Message(), None)

    result = transport_with(error)(auth_request())

    assert result.code == expected


def test_rate_limiting_carries_only_a_bounded_retry_after():
    headers = Message()
    headers["Retry-After"] = "120"
    error = urllib.error.HTTPError("masked", 429, "masked", headers, None)

    result = transport_with(error)(auth_request())

    assert result == _TossTransportFailure(code="http_429", retry_after=120)


def test_transport_faults_map_to_closed_codes():
    timeout = urllib.error.URLError(TimeoutError())
    refused = urllib.error.URLError("masked")

    assert transport_with(timeout)(auth_request()).code == "timeout"
    assert transport_with(refused)(auth_request()).code == "connection_failure"
    assert transport_with(RuntimeError("boom"))(auth_request()).code == "protocol_failure"


def test_oversized_and_reencoded_token_bodies_are_refused():
    oversized = Response(b"x" * (MAX_BODY_BYTES + 1))
    gzipped = Response()
    gzipped.headers["Content-Encoding"] = "gzip"

    assert transport_with(oversized)(auth_request()).code == "body_too_large"
    assert transport_with(gzipped)(auth_request()).code == "unsupported_content_encoding"


def test_non_200_success_status_is_a_protocol_failure():
    response = Response()
    response.status = 204

    assert transport_with(response)(auth_request()).code == "protocol_failure"


# --- redaction ---------------------------------------------------------------

def test_credentials_never_appear_in_request_or_failure_representations():
    request = auth_request()
    error = urllib.error.HTTPError(
        TOKEN_URL, 401, '{"error":{"code":"expired-token"}}', Message(), None)
    failure = transport_with(error)(auth_request())

    for text in (repr(request), str(request), repr(failure), str(failure)):
        assert CLIENT_ID not in text and CLIENT_SECRET not in text
    assert "expired-token" not in repr(failure)
    assert "tossinvest" not in repr(failure)
