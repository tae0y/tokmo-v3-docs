"""Two-operation HTTPS adapter for the official Toss read endpoints.

Exactly two operations exist here: the accounts read, which must not carry an
account header, and the holdings read, which must. The verb is a module
constant, the two paths are module constants, and neither is derived from
caller input, so this adapter cannot address any other endpoint and cannot
perform a state-changing call.

Transport is hardened the same way as the other tracer adapters: no proxy
discovery, no redirect following, identity encoding only, a single attempt, a
bounded body, and closed failure codes that carry no provider text.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.toss import (
    ACCOUNTS_ENDPOINT_ID,
    HOLDINGS_ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    TossDataRequest,
    _TossTransportFailure,
    _TossTransportSuccess,
    valid_account_seq,
    valid_token,
)

TOSS_HOST = "openapi.tossinvest.com"
ACCOUNTS_PATH = "/api/v1/accounts"
HOLDINGS_PATH = "/api/v1/holdings"
METHOD = "GET"
ACCOUNT_HEADER = "X-Tossinvest-Account"
AUTH_SCHEME = "Bearer"
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_RETRY_AFTER = 3600
MAX_RATE_LIMIT_REMAINING = 1_000_000

_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_live_opener(https_handler=None) -> urllib.request.OpenerDirector:
    """An explicit empty ProxyHandler avoids environment proxy discovery."""
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


class TossDataLiveTransport:
    """One authorized read per call; returns the source-private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
        self, request: TossDataRequest
    ) -> _TossTransportSuccess | _TossTransportFailure:
        try:
            url = _wire_url(request)
            headers = _wire_headers(request)
            if url is None or headers is None:
                return _TossTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                url, data=None, method=METHOD, headers=headers
            )
            with self.opener.open(wire, timeout=request.timeout_seconds) as response:
                return _read_success(response)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, TimeoutError):
                return _TossTransportFailure(code="timeout")
            return _TossTransportFailure(code="connection_failure")
        except TimeoutError:
            return _TossTransportFailure(code="timeout")
        except OSError:
            return _TossTransportFailure(code="connection_failure")
        except Exception:
            return _TossTransportFailure(code="protocol_failure")


def _wire_url(request: TossDataRequest) -> str | None:
    """Wire-exact re-parse of one of the two authorized paths.

    The account-header rule is enforced here rather than at the header step so
    that an accounts request carrying an account sequence, or a holdings
    request lacking one, is refused before any I/O.
    """
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    if request.endpoint_id == ACCOUNTS_ENDPOINT_ID:
        if request.account_seq is not None:
            return None
        path = ACCOUNTS_PATH
    elif request.endpoint_id == HOLDINGS_ENDPOINT_ID:
        if not valid_account_seq(request.account_seq):
            return None
        path = HOLDINGS_PATH
    else:
        return None

    url = f"https://{TOSS_HOST}{path}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port, parts.query) != (
        "https",
        TOSS_HOST,
        path,
        None,
        "",
    ):
        return None
    return url


def _wire_headers(request: TossDataRequest) -> dict[str, str] | None:
    if not valid_token(request.token):
        return None
    headers = {
        "Authorization": f"{AUTH_SCHEME} {request.token}",
        "Accept-Encoding": "identity",
    }
    if request.endpoint_id == HOLDINGS_ENDPOINT_ID:
        if not valid_account_seq(request.account_seq):
            return None
        headers[ACCOUNT_HEADER] = request.account_seq
    return headers


def _read_success(response) -> _TossTransportSuccess | _TossTransportFailure:
    if getattr(response, "status", None) != 200:
        return _TossTransportFailure(code="protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return _TossTransportFailure(code="unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return _TossTransportFailure(code="body_too_large")
        except ValueError:
            return _TossTransportFailure(code="unsupported_content_encoding")
    body = response.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return _TossTransportFailure(code="body_too_large")
    return _TossTransportSuccess(
        body=body,
        rate_limit_remaining=_bounded_int(
            response.headers.get("X-RateLimit-Remaining"), MAX_RATE_LIMIT_REMAINING
        ),
    )


def _map_http_error(err: urllib.error.HTTPError) -> _TossTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _TossTransportFailure(code="redirect")
    if code == 429:
        return _TossTransportFailure(
            code="http_429",
            retry_after=_bounded_int(
                err.headers.get("Retry-After") if err.headers else None, MAX_RETRY_AFTER
            ),
        )
    if code == 401:
        return _TossTransportFailure(code="auth_rejection")
    if code == 403:
        return _TossTransportFailure(code="forbidden")
    if 400 <= code < 500:
        return _TossTransportFailure(code="bad_request")
    if 500 <= code < 600:
        return _TossTransportFailure(code="http_5xx")
    return _TossTransportFailure(code="protocol_failure")


def _bounded_int(raw, ceiling: int) -> int | None:
    """Header numerics are kept as bounded integers; raw header text never is."""
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return min(ceiling, max(0, value))
