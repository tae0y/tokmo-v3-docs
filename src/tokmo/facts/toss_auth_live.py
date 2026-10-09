"""Single-operation HTTPS adapter for the official Toss token endpoint.

This is the only module in the tree that ever puts the client secret on the
wire. It exposes exactly one operation with a hard-coded host, path, and
grant, and a three-key form allowlist; there is no generic request surface, so
no other endpoint can be addressed from here.

Failures are returned as closed codes. Provider text, the URL, the form, and
the credentials never enter a return value or an exception.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.toss import (
    GRANT_TYPE,
    REQUEST_TIMEOUT_SECONDS,
    TOKEN_ENDPOINT_ID,
    TOKEN_FORM_KEYS,
    TossAuthRequest,
    _TossTransportFailure,
    _TossTransportSuccess,
    valid_credential,
)

TOSS_HOST = "openapi.tossinvest.com"
TOKEN_PATH = "/oauth2/token"
TOKEN_METHOD = "POST"
CONTENT_TYPE = "application/x-www-form-urlencoded"
MAX_BODY_BYTES = 64 * 1024
MAX_RETRY_AFTER = 3600

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


class TossAuthLiveTransport:
    """One token call per invocation; no retry and no token cache."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
        self, request: TossAuthRequest
    ) -> _TossTransportSuccess | _TossTransportFailure:
        try:
            url = _wire_url(request)
            form = _wire_form(request)
            if url is None or form is None:
                return _TossTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                url,
                data=form,
                method=TOKEN_METHOD,
                headers={"Content-Type": CONTENT_TYPE, "Accept-Encoding": "identity"},
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


def _wire_url(request: TossAuthRequest) -> str | None:
    """Wire-exact re-parse: any deviation in scheme, host, port, path, or
    query means the request is not the authorized operation."""
    if request.endpoint_id != TOKEN_ENDPOINT_ID:
        return None
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    url = f"https://{TOSS_HOST}{TOKEN_PATH}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port, parts.query) != (
        "https",
        TOSS_HOST,
        TOKEN_PATH,
        None,
        "",
    ):
        return None
    return url


def _wire_form(request: TossAuthRequest) -> bytes | None:
    """The allowlist is the whole form: three keys, grant hard-coded here."""
    if not valid_credential(request.client_id):
        return None
    if not valid_credential(request.client_secret):
        return None
    form = {
        "grant_type": GRANT_TYPE,
        "client_id": request.client_id,
        "client_secret": request.client_secret,
    }
    if set(form) != TOKEN_FORM_KEYS or form["grant_type"] != GRANT_TYPE:
        return None
    return urllib.parse.urlencode(form).encode()


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
    return _TossTransportSuccess(body=body)


def _map_http_error(err: urllib.error.HTTPError) -> _TossTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _TossTransportFailure(code="redirect")
    if code == 429:
        return _TossTransportFailure(
            code="http_429", retry_after=_bounded_retry_after(err.headers)
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


def _bounded_retry_after(headers) -> int | None:
    raw = headers.get("Retry-After") if headers is not None else None
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except ValueError:
        return None
    return min(MAX_RETRY_AFTER, max(0, seconds))
