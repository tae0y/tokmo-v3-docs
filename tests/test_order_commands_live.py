"""Real transport code exercised using a capturing opener; no real sockets."""
import io
import json
import urllib.error
from dataclasses import replace

import pytest

from test_order_commands import buy, oto
from tokmo.facts.toss_order_commands import execute_order_command
from tokmo.facts.toss_order_commands_live import TossOrderCommandsLiveTransport, _NoOrderRedirect
from tokmo.values.order_commands import CancelOrder, OrderCommand, OrderOperation, ReplaceOrder


class Response(io.BytesIO):
    def __init__(self, status=200, body=b'{"result":{"orderId":"new"}}', headers=None):
        super().__init__(body)
        self.status = status
        self.headers = {} if headers is None else headers


class Opener:
    def __init__(self, make_response):
        self.calls = []
        self.make_response = make_response
    def open(self, request, timeout):
        self.calls.append((request, timeout))
        return self.make_response()


def run(cmd, factory, *, enabled=True):
    transport = TossOrderCommandsLiveTransport(enabled=enabled)
    opener = Opener(factory)
    transport.opener = opener
    outcome = execute_order_command(cmd, token="private-bearer", account_ref="123456", transport=transport)
    return outcome, opener.calls


def test_default_disabled_performs_zero_io():
    result, calls = run(buy(), lambda: pytest.fail("network"), enabled=False)
    assert result.code == "transport_disabled" and not calls
    assert TossOrderCommandsLiveTransport().enabled is False


@pytest.mark.parametrize("cmd,method,path,response", [
    (buy(), "POST", "/api/v1/orders", {"orderId": "new"}),
    (oto(), "POST", "/api/v1/conditional-orders", {"conditionalOrderId": "new"}),
    (OrderCommand(OrderOperation.MODIFY, "US", "LMT", ReplaceOrder("LIMIT", price="524"), "old"), "POST", "/api/v1/orders/old/modify", {"orderId": "new"}),
    (OrderCommand(OrderOperation.CANCEL, "US", "LMT", CancelOrder(), "old"), "POST", "/api/v1/orders/old/cancel", {"orderId": "new"}),
    (replace(oto(), operation=OrderOperation.CONDITIONAL_MODIFY, target_id="old", payload=replace(oto().payload, client_order_id=None)), "POST", "/api/v1/conditional-orders/old/modify", {"conditionalOrderId": "new"}),
])
def test_wire_routes_and_credentials(cmd, method, path, response):
    result, calls = run(cmd, lambda: Response(body=json.dumps({"result": response}).encode()))
    assert result.status == "ACCEPTED" and len(calls) == 1
    request, timeout = calls[0]
    assert request.full_url == "https://openapi.tossinvest.com" + path
    assert request.method == method and timeout == 10.0
    assert request.get_header("Authorization") == "Bearer private-bearer"
    assert request.get_header("X-tossinvest-account") == "123456"
    assert b"private-bearer" not in request.data and b"123456" not in request.data


def test_delete_204_has_no_json_body():
    cmd = OrderCommand(OrderOperation.CONDITIONAL_CANCEL, "KR", "005380", CancelOrder(), "old")
    result, calls = run(cmd, lambda: Response(204, b""))
    assert result.status == "ACCEPTED" and calls[0][0].method == "DELETE" and calls[0][0].data is None


def test_http_rejection_is_parsed_without_retry():
    def fail():
        raise urllib.error.HTTPError("https://openapi.tossinvest.com", 422, "rejected", {},
                                     io.BytesIO(b'{"error":{"code":"insufficient-buying-power"}}'))
    result, calls = run(buy(), fail)
    assert (result.status, result.code, len(calls)) == ("REJECTED", "insufficient-buying-power", 1)


@pytest.mark.parametrize("factory", [
    lambda: Response(headers={"Content-Encoding": "gzip"}),
    lambda: Response(headers={"Content-Length": "99999999"}),
    lambda: Response(body=b"x" * (1024 * 1024 + 1)),
    lambda: Response(body=b'{"result":{"orderId":"a","orderId":"b"}}'),
])
def test_bad_responses_are_unknown_and_not_retried(factory):
    result, calls = run(buy(), factory)
    assert result.status == "UNKNOWN" and len(calls) == 1


def test_redirects_are_never_followed():
    assert _NoOrderRedirect().redirect_request(None, None, 307, "", {}, "https://other.test") is None
