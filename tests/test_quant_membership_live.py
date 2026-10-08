from datetime import datetime, timezone
from email.message import Message
from io import BytesIO
import urllib.error
import urllib.request

import pytest


def test_official_membership_live_transport_module_exists():
    from importlib.util import find_spec

    assert find_spec("tokmo.facts.official_membership_live") is not None


class FakeResponse:
    def __init__(self, body=b"ok", headers=None, status=200):
        self._stream = BytesIO(body)
        self.headers = {"Content-Length": str(len(body))} if headers is None else headers
        self.status = status
        self.read_amounts = []

    def read(self, amount=-1):
        self.read_amounts.append(amount)
        return self._stream.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None


def test_typed_transport_uses_exact_identity_timeout_encoding_and_cap(monkeypatch):
    from tokmo.facts.official_membership_live import (
        EndpointKindV1,
        OfficialMembershipLiveTransport,
        OfficialMembershipRequestV1,
    )

    seen = {}

    class Opener:
        def open(self, request, timeout):
            seen.update(request=request, timeout=timeout)
            return FakeResponse(b"abc")

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    result = OfficialMembershipLiveTransport(clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)).fetch(
        OfficialMembershipRequestV1(EndpointKindV1.NASDAQ_LISTED),
        sec_user_agent=None,
    )
    assert result.ok and result.body == b"abc"
    assert seen["request"].full_url == "https://www.nasdaqtrader.com/dynamic/symdir/nasdaqlisted.txt"
    assert seen["request"].method == "GET"
    assert seen["request"].get_header("Accept-encoding") == "identity"
    assert seen["timeout"] == 10.0


def test_transport_rejects_overflow_and_never_retries(monkeypatch):
    from tokmo.facts.official_membership_live import (
        EndpointKindV1,
        OfficialMembershipLiveTransport,
        OfficialMembershipRequestV1,
    )

    calls = 0

    class Opener:
        def open(self, request, timeout):
            nonlocal calls
            calls += 1
            return FakeResponse(b"x", {"Content-Length": str(4 * 1024 * 1024 + 1)})

    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    result = OfficialMembershipLiveTransport(clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)).fetch(
        OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC),
        sec_user_agent="fixture-agent contact@example.test",
    )
    assert not result.ok and result.code == "BODY_TOO_LARGE"
    assert calls == 1


def test_transport_accepts_missing_content_length_and_enforces_streamed_cap(monkeypatch):
    from tokmo.facts.official_membership_live import EndpointKindV1, OfficialMembershipLiveTransport, OfficialMembershipRequestV1
    chunked = FakeResponse(b"abc", {"Transfer-Encoding": "chunked"})
    streamed_overflow = FakeResponse(
        b"x" * (4 * 1024 * 1024 + 1), {"Transfer-Encoding": "chunked"})
    responses = [chunked, streamed_overflow]
    class Opener:
        def open(self, request, timeout):
            return responses.pop(0)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    transport = OfficialMembershipLiveTransport(clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))
    identity = OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC)
    missing = transport.fetch(identity, sec_user_agent="fixture contact@example.test")
    assert missing.ok and missing.body == b"abc"
    assert transport.fetch(identity, sec_user_agent="fixture contact@example.test").code == "BODY_TOO_LARGE"
    assert chunked.read_amounts == [identity.cap + 1]
    assert streamed_overflow.read_amounts == [identity.cap + 1]


@pytest.mark.parametrize("headers", (
    {},
    {"Transfer-Encoding": "gzip, chunked"},
    {"Transfer-Encoding": "chunked "},
))
def test_transport_rejects_unframed_or_ambiguous_missing_content_length(
        monkeypatch, headers):
    from tokmo.facts.official_membership_live import EndpointKindV1, OfficialMembershipLiveTransport, OfficialMembershipRequestV1
    response = FakeResponse(b"partial", headers)
    class Opener:
        def open(self, request, timeout):
            return response
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    result = OfficialMembershipLiveTransport(
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)
    ).fetch(
        OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC),
        sec_user_agent="fixture contact@example.test",
    )
    assert not result.ok and result.code == "CONTENT_LENGTH"
    assert response.read_amounts == []


def test_transport_rejects_invalid_or_mismatched_present_content_length(monkeypatch):
    from tokmo.facts.official_membership_live import EndpointKindV1, OfficialMembershipLiveTransport, OfficialMembershipRequestV1
    responses = [
        FakeResponse(b"abc", {"Content-Length": "４"}),
        FakeResponse(b"abc", {"Content-Length": "4"}),
    ]
    class Opener:
        def open(self, request, timeout):
            return responses.pop(0)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    transport = OfficialMembershipLiveTransport(
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc))
    identity = OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC)
    assert transport.fetch(identity, sec_user_agent="fixture contact@example.test").code == "CONTENT_LENGTH"
    assert transport.fetch(identity, sec_user_agent="fixture contact@example.test").code == "CONTENT_LENGTH"


def test_transport_preserves_sanitized_http_status_without_response_body(monkeypatch):
    from tokmo.facts.official_membership_live import EndpointKindV1, OfficialMembershipLiveTransport, OfficialMembershipRequestV1
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(
                request.full_url, 403, "raw provider text", Message(), None)
    monkeypatch.setattr(urllib.request, "build_opener", lambda *handlers: Opener())
    result = OfficialMembershipLiveTransport(
        clock=lambda: datetime(2026, 8, 13, tzinfo=timezone.utc)
    ).fetch(
        OfficialMembershipRequestV1(EndpointKindV1.SEC_SIC),
        sec_user_agent="fixture contact@example.test",
    )
    assert not result.ok and result.code == "HTTP_403" and result.status == 403
    assert result.body == b""
