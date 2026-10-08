"""Single-operation HTTPS adapter for Toss current prices.

The host, path, method, and query key are closed here. Calls have no proxy
discovery, redirect following, account header, retry, or write reachability.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.toss import (
    _TossTransportFailure,
    _TossTransportSuccess,
    valid_token,
)
from tokmo.facts.toss_prices import (
    PRICES_ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    TossPricesRequest,
    valid_symbols,
)

TOSS_HOST = "openapi.tossinvest.com"
PRICES_PATH = "/api/v1/prices"
METHOD = "GET"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_RETRY_AFTER = 3600
MAX_RATE_LIMIT_REMAINING = 1_000_000

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_live_opener(https_handler=None) -> urllib.request.OpenerDirector:
    opener = urllib.request.OpenerDirector()
    for handler in (
        urllib.request.ProxyHandler({}),
        urllib.request.HTTPSHandler() if https_handler is None else https_handler,
        _NoRedirectHandler(),
        urllib.request.HTTPDefaultErrorHandler(),
        urllib.request.HTTPErrorProcessor(),
    ):
        opener.add_handler(handler)
    return opener


class TossPricesLiveTransport:
    """One and only one official current-price GET per invocation."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
            self, request: TossPricesRequest
            ) -> _TossTransportSuccess | _TossTransportFailure:
        try:
            url = _wire_url(request)
            headers = _wire_headers(request)
            if url is None or headers is None:
                return _TossTransportFailure("protocol_failure")
            wire = urllib.request.Request(
                url, data=None, method=METHOD, headers=headers)
            with self.opener.open(
                    wire, timeout=request.timeout_seconds) as response:
                return _read_success(response)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            if isinstance(getattr(err, "reason", None), TimeoutError):
                return _TossTransportFailure("timeout")
            return _TossTransportFailure("connection_failure")
        except TimeoutError:
            return _TossTransportFailure("timeout")
        except OSError:
            return _TossTransportFailure("connection_failure")
        except Exception:
            return _TossTransportFailure("protocol_failure")


def _wire_url(request: TossPricesRequest) -> str | None:
    if (not isinstance(request, TossPricesRequest)
            or request.endpoint_id != PRICES_ENDPOINT_ID
            or request.timeout_seconds != REQUEST_TIMEOUT_SECONDS
            or not valid_symbols(request.symbols)):
        return None
    joined = ",".join(request.symbols)
    query = f"symbols={urllib.parse.quote(joined, safe=',.-')}"
    url = f"https://{TOSS_HOST}{PRICES_PATH}?{query}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port) != (
            "https", TOSS_HOST, PRICES_PATH, None):
        return None
    if urllib.parse.parse_qs(parts.query, strict_parsing=True) != {
            "symbols": [joined]}:
        return None
    return url


def _wire_headers(request: TossPricesRequest) -> dict[str, str] | None:
    if not valid_token(request.token):
        return None
    return {
        "Authorization": f"Bearer {request.token}",
        "Accept-Encoding": "identity",
    }


def _read_success(response) -> _TossTransportSuccess | _TossTransportFailure:
    if getattr(response, "status", None) != 200:
        return _TossTransportFailure("protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return _TossTransportFailure("unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return _TossTransportFailure("body_too_large")
        except ValueError:
            return _TossTransportFailure("unsupported_content_encoding")
    body = response.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return _TossTransportFailure("body_too_large")
    return _TossTransportSuccess(
        body=body,
        rate_limit_remaining=_bounded_int(
            response.headers.get("X-RateLimit-Remaining"),
            MAX_RATE_LIMIT_REMAINING),
    )


def _map_http_error(err: urllib.error.HTTPError) -> _TossTransportFailure:
    if err.code in _REDIRECT_CODES:
        return _TossTransportFailure("redirect")
    if err.code == 429:
        return _TossTransportFailure(
            "http_429",
            retry_after=_bounded_int(
                err.headers.get("Retry-After") if err.headers else None,
                MAX_RETRY_AFTER),
        )
    if err.code == 401:
        return _TossTransportFailure("auth_rejection")
    if err.code == 403:
        return _TossTransportFailure("forbidden")
    if 400 <= err.code < 500:
        return _TossTransportFailure("bad_request")
    if 500 <= err.code < 600:
        return _TossTransportFailure("http_5xx")
    return _TossTransportFailure("protocol_failure")


def _bounded_int(raw, ceiling: int) -> int | None:
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return min(ceiling, max(0, value))
