"""Small, single-call HTTPS adapter for the two official SEC endpoints.

The declared User-Agent goes onto the wire header only; it never joins the
URL, argv, logs, or any returned value.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.sec import (
    CIK_DIGITS,
    REQUEST_TIMEOUT_SECONDS,
    SUBMISSIONS_ENDPOINT_ID,
    TICKERS_ENDPOINT_ID,
    SecRequest,
    _SecTransportFailure,
    _SecTransportSuccess,
)

TICKERS_HOST = "www.sec.gov"
TICKERS_PATH = "/files/company_tickers.json"
SUBMISSIONS_HOST = "data.sec.gov"
SUBMISSIONS_PATH_PREFIX = "/submissions/CIK"
METHOD = "GET"
MAX_BODY_BYTES = 2 * 1024 * 1024

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


class SecLiveTransport:
    """One official GET per call; returns the source-private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(self, request: SecRequest) -> _SecTransportSuccess | _SecTransportFailure:
        try:
            url = _wire_url(request)
            if url is None:
                return _SecTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                url, data=None, method=METHOD,
                headers={"User-Agent": request.user_agent,
                         "Accept-Encoding": "identity"},
            )
            with self.opener.open(wire, timeout=request.timeout_seconds) as response:
                return _read_success(response)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, TimeoutError):
                return _SecTransportFailure(code="timeout")
            return _SecTransportFailure(code="connection_failure")
        except TimeoutError:
            return _SecTransportFailure(code="timeout")
        except OSError:
            return _SecTransportFailure(code="connection_failure")
        except Exception:
            return _SecTransportFailure(code="protocol_failure")


def _wire_url(request: SecRequest) -> str | None:
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    if not isinstance(request.user_agent, str) or not request.user_agent:
        return None

    if request.endpoint_id == TICKERS_ENDPOINT_ID:
        if request.cik is not None:
            return None
        host, path = TICKERS_HOST, TICKERS_PATH
    elif request.endpoint_id == SUBMISSIONS_ENDPOINT_ID:
        cik = request.cik
        if (not isinstance(cik, str) or len(cik) != CIK_DIGITS
                or not cik.isascii() or not cik.isdigit()):
            return None
        host, path = SUBMISSIONS_HOST, f"{SUBMISSIONS_PATH_PREFIX}{cik}.json"
    else:
        return None

    url = f"https://{host}{path}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port, parts.query) != (
            "https", host, path, None, ""):
        return None
    return url


def _read_success(response) -> _SecTransportSuccess | _SecTransportFailure:
    if getattr(response, "status", None) != 200:
        return _SecTransportFailure(code="protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return _SecTransportFailure(code="unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return _SecTransportFailure(code="body_too_large")
        except ValueError:
            return _SecTransportFailure(code="unsupported_content_encoding")
    body = response.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return _SecTransportFailure(code="body_too_large")
    return _SecTransportSuccess(body=body)


def _map_http_error(err: urllib.error.HTTPError) -> _SecTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _SecTransportFailure(code="redirect")
    if code == 429:
        return _SecTransportFailure(code="http_429")
    if code in (401, 403):
        return _SecTransportFailure(code="auth_rejection")
    if 400 <= code < 500:
        return _SecTransportFailure(code="bad_request")
    if 500 <= code < 600:
        return _SecTransportFailure(code="http_5xx")
    return _SecTransportFailure(code="protocol_failure")
