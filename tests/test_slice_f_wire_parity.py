"""F8 wire parity through the production Toss live transport entrypoints."""

from dataclasses import dataclass
from email.message import Message
import io
import urllib.parse
import urllib.request
import urllib.response

import pytest

from tokmo.facts.toss import (
    build_accounts_request,
    build_auth_request,
    build_holdings_request,
)
from tokmo.facts.toss_auth_live import TossAuthLiveTransport
from tokmo.facts.toss_live import TossDataLiveTransport
from tokmo.facts.toss_prices import build_prices_request
from tokmo.facts.toss_prices_live import TossPricesLiveTransport
from tokmo.facts.toss_reads import (
    TossReadOperation,
    build_read_request,
)
from tokmo.facts.toss_reads_live import TossReadsLiveTransport


class _Response:
    status = 200
    headers: dict[str, str] = {}

    def read(self, _limit: int) -> bytes:
        return b"{}"

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


@dataclass(frozen=True)
class _RecordedRequest:
    method: str
    host: str
    path: str
    query: dict[str, tuple[str, ...]]
    header_names: frozenset[str]
    content_type: str | None
    form_keys: frozenset[str]


class RecordingHTTPSHandler(urllib.request.HTTPSHandler):
    """Record wire shape only; never retain credential, token, or header values."""

    def __init__(self) -> None:
        super().__init__()
        self.requests: list[_RecordedRequest] = []

    def https_open(self, request):
        parts = urllib.parse.urlsplit(request.full_url)
        headers = {name.lower(): value for name, value in request.header_items()}
        selected_headers = frozenset(headers) & frozenset({
            "authorization", "accept-encoding", "content-type",
            "x-tossinvest-account",
        })
        form = urllib.parse.parse_qs(
            request.data.decode("ascii"), strict_parsing=True
        ) if request.data is not None else {}
        query = urllib.parse.parse_qs(
            parts.query, keep_blank_values=True, strict_parsing=True
        ) if parts.query else {}
        self.requests.append(_RecordedRequest(
            method=request.get_method(),
            host=parts.hostname or "",
            path=parts.path,
            query={key: tuple(values) for key, values in query.items()},
            header_names=selected_headers,
            content_type=headers.get("content-type"),
            form_keys=frozenset(form),
        ))
        return _Response()


class ScriptedHTTPSHandler(RecordingHTTPSHandler):
    """Return synthetic wire outcomes while retaining the socket-free record."""

    def __init__(self, outcomes) -> None:
        super().__init__()
        self.outcomes = iter(outcomes)

    def https_open(self, request):
        super().https_open(request)
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome


def _redirect_response(url: str):
    headers = Message()
    headers["Location"] = "https://openapi.tossinvest.com/redirect-target"
    response = urllib.response.addinfourl(
        io.BytesIO(b""), headers, url, code=302
    )
    setattr(response, "msg", "Found")
    return response


_TOKEN = "fixture-token"
_ACCOUNT = "123456789"


def _auth_case(handler):
    return TossAuthLiveTransport(https_handler=handler), build_auth_request(
        client_id="fixture-client", client_secret="fixture-secret"
    )


def _accounts_case(handler):
    return TossDataLiveTransport(https_handler=handler), build_accounts_request(
        token=_TOKEN
    )


def _holdings_case(handler):
    return TossDataLiveTransport(https_handler=handler), build_holdings_request(
        token=_TOKEN, account_seq=_ACCOUNT
    )


def _prices_case(handler):
    return TossPricesLiveTransport(https_handler=handler), build_prices_request(
        symbols=("005930", "AAPL"), token=_TOKEN
    )


def _read_case(operation, **kwargs):
    def build(handler):
        return TossReadsLiveTransport(https_handler=handler), build_read_request(
            operation, token=_TOKEN, **kwargs
        )
    return build


_CASES = (
    pytest.param("TOKEN", _auth_case, "POST", "/oauth2/token", {}, False,
                 frozenset({"grant_type", "client_id", "client_secret"}), id="TOKEN"),
    pytest.param("ACCOUNTS", _accounts_case, "GET", "/api/v1/accounts", {}, False,
                 frozenset(), id="ACCOUNTS"),
    pytest.param("ACCOUNT_HOLDINGS", _holdings_case, "GET", "/api/v1/holdings", {}, True,
                 frozenset(), id="ACCOUNT_HOLDINGS"),
    pytest.param("PRICE_BATCH", _prices_case, "GET", "/api/v1/prices",
                 {"symbols": ("005930,AAPL",)}, False, frozenset(), id="PRICE_BATCH"),
    pytest.param("ORDERS_PAGE", _read_case(TossReadOperation.ORDERS,
                 account_seq=_ACCOUNT, status="CLOSED"), "GET", "/api/v1/orders",
                 {"status": ("CLOSED",)}, True, frozenset(), id="ORDERS_PAGE-no-cursor"),
    pytest.param("ORDERS_PAGE", _read_case(TossReadOperation.ORDERS,
                 account_seq=_ACCOUNT, status="CLOSED", cursor="cursor_2"), "GET",
                 "/api/v1/orders", {"status": ("CLOSED",), "cursor": ("cursor_2",)},
                 True, frozenset(), id="ORDERS_PAGE-cursor"),
    pytest.param("ORDER_DETAIL", _read_case(TossReadOperation.ORDER_DETAIL,
                 account_seq=_ACCOUNT, order_id="order-42"), "GET",
                 "/api/v1/orders/order-42", {}, True, frozenset(), id="ORDER_DETAIL"),
    pytest.param("CALENDAR_DAY_KR", _read_case(TossReadOperation.KR_MARKET_CALENDAR),
                 "GET", "/api/v1/market-calendar/KR", {}, False, frozenset(),
                 id="CALENDAR_DAY_KR-no-date"),
    pytest.param("CALENDAR_DAY_KR", _read_case(TossReadOperation.KR_MARKET_CALENDAR,
                 date="2026-07-30"), "GET", "/api/v1/market-calendar/KR",
                 {"date": ("2026-07-30",)}, False, frozenset(),
                 id="CALENDAR_DAY_KR-date"),
    pytest.param("CALENDAR_DAY_US", _read_case(TossReadOperation.US_MARKET_CALENDAR,
                 date="2026-07-30"), "GET", "/api/v1/market-calendar/US",
                 {"date": ("2026-07-30",)}, False, frozenset(), id="CALENDAR_DAY_US-date"),
)


_TRANSPORT_CASES = (
    pytest.param(_auth_case, id="auth"),
    pytest.param(_accounts_case, id="data"),
    pytest.param(_prices_case, id="prices"),
    pytest.param(
        _read_case(TossReadOperation.KR_MARKET_CALENDAR), id="reads"
    ),
)


@pytest.mark.parametrize(
    "operation,build,method,path,query,account_required,form_keys", _CASES
)
def test_production_live_transport_wire_matches_closed_operation_mapping(
        operation, build, method, path, query, account_required, form_keys):
    handler = RecordingHTTPSHandler()
    transport, request = build(handler)

    transport(request)

    assert len(handler.requests) == 1
    recorded = handler.requests[0]
    assert recorded.method == method
    assert recorded.host == "openapi.tossinvest.com"
    assert recorded.path == path
    assert recorded.query == query
    assert ("x-tossinvest-account" in recorded.header_names) is account_required

    if operation == "TOKEN":
        assert recorded.header_names == frozenset({"content-type", "accept-encoding"})
        assert recorded.content_type == "application/x-www-form-urlencoded"
        assert recorded.form_keys == form_keys
    else:
        assert recorded.header_names == (
            frozenset({"authorization", "accept-encoding", "x-tossinvest-account"})
            if account_required else frozenset({"authorization", "accept-encoding"})
        )
        assert recorded.content_type is None
        assert recorded.form_keys == frozenset()

    # The recording is structural: none of the private fixture values persist in it.
    rendered = repr(recorded)
    assert all(secret not in rendered for secret in (
        _TOKEN, _ACCOUNT, "fixture-client", "fixture-secret"
    ))


@pytest.mark.parametrize("build", _TRANSPORT_CASES)
def test_production_live_transport_opens_once_without_redirect_or_retry(
        build, monkeypatch):
    real_socket_calls = []

    def forbid_real_socket(*args, **kwargs):
        real_socket_calls.append((args, kwargs))
        raise AssertionError("real socket/DNS is forbidden in wire parity")

    monkeypatch.setattr("socket.create_connection", forbid_real_socket)
    monkeypatch.setattr("socket.getaddrinfo", forbid_real_socket)

    redirect = ScriptedHTTPSHandler((
        _redirect_response("https://openapi.tossinvest.com/source"),
        _Response(),
    ))
    transport, request = build(redirect)
    result = transport(request)
    assert result.code == "redirect"
    assert len(redirect.requests) == 1

    timeout = ScriptedHTTPSHandler((
        TimeoutError("synthetic timeout"), _Response()
    ))
    transport, request = build(timeout)
    result = transport(request)
    assert result.code == "timeout"
    assert len(timeout.requests) == 1

    assert real_socket_calls == []
