"""Small, single-call HTTPS adapter for the official EIA WTI endpoint."""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.eia import (
    ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    WTI_QUERY_MULTIMAP,
    EiaRequest,
    _LiveTransportFailure,
    _LiveTransportSuccess,
)

EIA_HOST = "api.eia.gov"
EIA_PATH = "/v2/petroleum/pri/spt/data/"
METHOD = "GET"
MAX_BODY_BYTES = 1_048_576

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


class EiaLiveTransport:
    """One official GET per call; returns the private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
        self, request: EiaRequest
    ) -> _LiveTransportSuccess | _LiveTransportFailure:
        try:
            url = _wire_url(request)
            if url is None:
                return _LiveTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(url, data=None, method=METHOD)
            with self.opener.open(wire, timeout=request.timeout_seconds) as response:
                return _read_success(response)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, TimeoutError):
                return _LiveTransportFailure(code="timeout")
            return _LiveTransportFailure(code="connection_failure")
        except TimeoutError:
            return _LiveTransportFailure(code="timeout")
        except OSError:
            return _LiveTransportFailure(code="connection_failure")
        except Exception:
            return _LiveTransportFailure(code="protocol_failure")


def _wire_url(request: EiaRequest) -> str | None:
    if request.endpoint_id != ENDPOINT_ID:
        return None
    if tuple(request.params.items()) != WTI_QUERY_MULTIMAP:
        return None
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    api_key = request.api_key
    if not isinstance(api_key, str) or not api_key:
        return None

    pairs = (*WTI_QUERY_MULTIMAP, ("api_key", api_key))
    query = "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}"
        for key, value in pairs
    )
    url = f"https://{EIA_HOST}{EIA_PATH}?{query}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port) != (
        "https",
        EIA_HOST,
        EIA_PATH,
        None,
    ):
        return None
    return url


def _read_success(response) -> _LiveTransportSuccess | _LiveTransportFailure:
    if getattr(response, "status", None) != 200:
        return _LiveTransportFailure(code="protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return _LiveTransportFailure(code="unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_BODY_BYTES:
                return _LiveTransportFailure(code="body_too_large")
        except ValueError:
            return _LiveTransportFailure(code="unsupported_content_encoding")
    body = response.read(MAX_BODY_BYTES + 1)
    if len(body) > MAX_BODY_BYTES:
        return _LiveTransportFailure(code="body_too_large")
    return _LiveTransportSuccess(body=body)


def _map_http_error(err: urllib.error.HTTPError) -> _LiveTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _LiveTransportFailure(code="redirect")
    if code == 429:
        return _LiveTransportFailure(
            code="http_429", retry_after=_retry_after_seconds(err.headers)
        )
    if code in (401, 403):
        return _LiveTransportFailure(code="auth_rejection")
    if 400 <= code < 500:
        return _LiveTransportFailure(code="bad_request")
    if 500 <= code < 600:
        return _LiveTransportFailure(code="http_5xx")
    return _LiveTransportFailure(code="protocol_failure")


def _retry_after_seconds(headers) -> int | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(3600, max(5, int(raw)))
    except (TypeError, ValueError):
        return None
