"""Opt-in, one-attempt real Toss order transport. No automatic activation."""

import urllib.error
import urllib.request

from tokmo.facts.toss import valid_token
from tokmo.facts.toss_order_commands import encode_order_command
from tokmo.facts.toss_reads import valid_account_seq_int64
from tokmo.values.order_commands import (
    OrderTransportDisabled,
    OrderWireRequest,
    OrderWireResponse,
)

TOSS_ORDER_HOST = "https://openapi.tossinvest.com"
ORDER_TIMEOUT_SECONDS = 10.0
MAX_ORDER_RESPONSE_BYTES = 1024 * 1024


class _NoOrderRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class TossOrderCommandsLiveTransport:
    """Credentials are supplied by the caller's shared token authority.

    Construction sends nothing. Setting enabled=True opts in to real writes
    on subsequent calls; the program must journal and authorize them first.
    """

    def __init__(self, *, enabled=False, https_handler=None):
        if type(enabled) is not bool:
            raise ValueError("enabled_boolean")
        self.enabled = enabled
        self.opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler() if https_handler is None else https_handler,
            _NoOrderRedirect(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            self.opener.add_handler(handler)

    def __call__(self, request):
        if not self.enabled:
            raise OrderTransportDisabled("order_transport_disabled")
        if (
            type(request) is not OrderWireRequest
            or not valid_token(request.token)
            or not valid_account_seq_int64(request.account_ref)
        ):
            raise OrderTransportDisabled("invalid_order_credentials")
        try:
            method, path, body = encode_order_command(request.command)
        except (ValueError, TypeError, ArithmeticError) as error:
            raise OrderTransportDisabled("invalid_order_command") from error
        wire = urllib.request.Request(
            TOSS_ORDER_HOST + path,
            data=body,
            method=method,
            headers={
                "Authorization": "Bearer " + request.token,
                "X-Tossinvest-Account": request.account_ref,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            },
        )
        try:
            with self.opener.open(wire, timeout=ORDER_TIMEOUT_SECONDS) as response:
                return _read(response)
        except urllib.error.HTTPError as error:
            try:
                return _read(error)
            finally:
                error.close()


def _read(response):
    status = (
        response.code
        if isinstance(response, urllib.error.HTTPError)
        else response.status
    )
    encoding = response.headers.get("Content-Encoding", "identity").strip().lower()
    if encoding != "identity":
        raise ValueError("response_encoding")
    declared = response.headers.get("Content-Length")
    if declared is not None and (
        int(declared) < 0 or int(declared) > MAX_ORDER_RESPONSE_BYTES
    ):
        raise ValueError("response_size")
    body = response.read(MAX_ORDER_RESPONSE_BYTES + 1)
    if len(body) > MAX_ORDER_RESPONSE_BYTES:
        raise ValueError("response_size")
    return OrderWireResponse(status, body)
