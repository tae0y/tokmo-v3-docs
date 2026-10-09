import io
import os
import socket
import traceback
from datetime import date
from email.message import Message
from urllib.error import HTTPError, URLError

import pytest

from tokmo.facts.fred_live import (
    MAX_BODY_BYTES,
    FredLiveTransport,
    build_live_opener,
    load_fred_api_key,
)
from tokmo.values.fred_macro import FredOperation, FredRequest


class Response:
    status = 200
    headers = {"Content-Encoding": "identity", "Content-Length": "2"}

    def read(self, size):
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


class Opener:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, []

    def open(self, request, timeout):
        self.calls.append((request, timeout))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def test_synthetic_credential_exact_wire_and_exception_chain_secrecy(tmp_path):
    credential = tmp_path / "fred.env"
    credential.write_text("# FRED API credential\nFRED_API_KEY=" + "a" * 32 + "\n")
    os.chmod(credential, 0o600)
    key = load_fred_api_key(credential)
    request = FredRequest(
        FredOperation.SERIES_OBSERVATIONS,
        series_id="PAYEMS",
        output_type=4,
        realtime_start=date(2026, 1, 1),
        realtime_end=date(2026, 1, 2),
        observation_start=date(2025, 12, 1),
        observation_end=date(2025, 12, 31),
        limit=2,
    )
    opener = Opener(Response())
    outcome = FredLiveTransport(opener=opener)(request, key)
    assert outcome.code == "success" and outcome.body == b"{}"
    wire, timeout = opener.calls[0]
    assert wire.full_url.startswith(
        "https://api.stlouisfed.org/fred/series/observations?"
    )
    assert wire.full_url.endswith("api_key=" + "a" * 32)
    assert wire.method == "GET" and timeout == 10.0
    sentinel = "SENTINEL_SECRET_" + "FULL_URL"
    headers = Message()
    failure = Opener(HTTPError(sentinel, 403, "forbidden", headers, io.BytesIO()))
    result = FredLiveTransport(opener=failure)(request, key)
    rendered = "|".join(
        (
            str(result),
            repr(result),
            "".join(
                traceback.format_exception(
                    result.__class__, result, result.__traceback__
                )
            )
            if isinstance(result, BaseException)
            else "",
            repr(getattr(result, "__cause__", None)),
            repr(getattr(result, "__context__", None)),
        )
    )
    assert result.code == "auth_rejection"
    assert sentinel not in rendered
    assert len(failure.calls) == 1
    bad = tmp_path / "bad.env"
    bad.write_text("export FRED_API_KEY=" + "b" * 32 + "\n")
    os.chmod(bad, 0o600)
    with pytest.raises(ValueError, match="credential_grammar"):
        load_fred_api_key(bad)


@pytest.mark.parametrize(
    ("outcome", "code"),
    [
        (HTTPError("redacted", 302, "redirect", Message(), io.BytesIO()), "redirect"),
        (HTTPError("redacted", 429, "rate", Message(), io.BytesIO()), "http_429"),
        (HTTPError("redacted", 503, "down", Message(), io.BytesIO()), "http_5xx"),
        (
            HTTPError("redacted", 400, "bad", Message(), io.BytesIO()),
            "provider_bad_request_unknown",
        ),
        (
            HTTPError("redacted", 700, "other", Message(), io.BytesIO()),
            "protocol_failure",
        ),
        (URLError(TimeoutError()), "timeout"),
        (URLError(OSError()), "connection_failure"),
        (RuntimeError("raw-body-SENTINEL_SECRET_FULL_URL"), "protocol_failure"),
    ],
)
def test_static_failure_matrix_is_one_call_and_sanitized(outcome, code):
    opener = Opener(outcome)
    request = FredRequest(FredOperation.SERIES, series_id="PAYEMS")
    result = FredLiveTransport(opener=opener)(request, "a" * 32)
    assert result.code == code
    assert len(opener.calls) == 1
    assert "SENTINEL" not in repr(result)


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (
            b'{"error_code":400,"error_message":"Bad Request.  The value for variable api_key is not registered.  Read https://fred.stlouisfed.org/docs/api/api_key.html for more information."}',
            "api_key_not_registered",
        ),
        (
            b'{"error_message":"Bad Request.  The value for variable api_key is not registered.  Read https://fred.stlouisfed.org/docs/api/api_key.html for more information.","error_code":400}',
            "api_key_not_registered",
        ),
        (b"", "provider_bad_request_unknown"),
        (
            b'{"error_code":400,"error_message":"different"}',
            "provider_bad_request_unknown",
        ),
        (
            b'{"error_code":400,"error_code":400,"error_message":"different"}',
            "provider_bad_request_unknown",
        ),
    ],
)
def test_http_400_strict_classifier_direct(body, code):
    result = FredLiveTransport(
        opener=Opener(
            HTTPError("redacted", 400, "redacted", Message(), io.BytesIO(body))
        )
    )(FredRequest(FredOperation.SERIES, series_id="PAYEMS"), "a" * 32)
    assert result.code == code


@pytest.mark.parametrize(
    ("response", "code"),
    [
        (
            type("R", (Response,), {"headers": {"Content-Encoding": "gzip"}})(),
            "unsupported_content_encoding",
        ),
        (
            type(
                "R",
                (Response,),
                {"headers": {"Content-Length": str(MAX_BODY_BYTES + 1)}},
            )(),
            "body_too_large",
        ),
        (type("R", (Response,), {"status": 418})(), "protocol_failure"),
    ],
)
def test_response_static_failures(response, code):
    result = FredLiveTransport(opener=Opener(response))(
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"), "a" * 32
    )
    assert result.code == code


def test_live_opener_disables_proxies_and_refuses_redirects(monkeypatch):
    monkeypatch.setattr(
        socket,
        "create_connection",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("socket")),
    )
    opener = build_live_opener()
    proxies = [
        handler.proxies
        for handler in opener.handlers
        if handler.__class__.__name__ == "ProxyHandler"
    ]
    redirect = next(
        handler
        for handler in opener.handlers
        if handler.__class__.__name__ == "_NoRedirect"
    )
    assert not proxies or proxies == [{}]
    assert (
        redirect.redirect_request(
            None, None, 302, "moved", {}, "https://example.invalid"
        )
        is None
    )


def test_post_build_wire_reassertion_fails_before_opener(monkeypatch):
    class MutatedRequest:
        method = "GET"
        full_url = "https://example.invalid/stolen?api_key=secret"

    monkeypatch.setattr(
        "tokmo.facts.fred_live.urllib.request.Request", lambda *a, **k: MutatedRequest()
    )
    opener = Opener(Response())
    result = FredLiveTransport(opener=opener)(
        FredRequest(FredOperation.SERIES, series_id="PAYEMS"), "a" * 32
    )
    assert result.code == "protocol_failure"
    assert opener.calls == []
