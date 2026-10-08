"""SEC companyfacts live HTTPS transport (G1A).

GET-only, no-redirect, identity-encoding HTTPS adapter for the official
companyfacts endpoint. Follows the exact same pattern as sec_live.py but
with the 5 MB body cap from the canary measurement (3,789,099 bytes).

No retry, no redirect following, no proxy discovery. The declared
User-Agent goes on the wire header only and never joins the URL, argv,
logs, or any returned value.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.sec_fundamentals import (
    COMPANYFACTS_ENDPOINT_ID,
    COMPANYFACTS_REQUEST_TIMEOUT_SECONDS,
    MAX_COMPANYFACTS_BODY_BYTES,
    SECCompanyFactsRequest,
    SECCompanyFactsTransportFailure,
    SECCompanyFactsTransportSuccess,
)

COMPANYFACTS_HOST_LIVE = "data.sec.gov"
COMPANYFACTS_PATH_LIVE = "/api/xbrl/companyfacts/CIK##########.json"
METHOD = "GET"
CIK_DIGITS = 10

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


class SECCompanyFactsLiveTransport:
    """One official GET per call; returns the source-private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
        self, request: SECCompanyFactsRequest,
    ) -> SECCompanyFactsTransportSuccess | SECCompanyFactsTransportFailure:
        try:
            url = self._wire_url(request)
            if url is None:
                return SECCompanyFactsTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                url,
                data=None,
                method=METHOD,
                headers={
                    "User-Agent": request.user_agent,
                    "Accept-Encoding": "identity",
                },
            )
            with self.opener.open(
                wire, timeout=request.timeout_seconds
            ) as response:
                return _read_success(response)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, TimeoutError):
                return SECCompanyFactsTransportFailure(code="timeout")
            return SECCompanyFactsTransportFailure(code="connection_failure")
        except TimeoutError:
            return SECCompanyFactsTransportFailure(code="timeout")
        except OSError:
            return SECCompanyFactsTransportFailure(code="connection_failure")
        except Exception:
            return SECCompanyFactsTransportFailure(code="protocol_failure")

    def _wire_url(self, request: SECCompanyFactsRequest) -> str | None:
        if request.timeout_seconds != COMPANYFACTS_REQUEST_TIMEOUT_SECONDS:
            return None
        if not isinstance(request.user_agent, str) or not request.user_agent:
            return None
        if request.endpoint_id != COMPANYFACTS_ENDPOINT_ID:
            return None

        cik = request.cik
        if (
            not isinstance(cik, str)
            or len(cik) != CIK_DIGITS
            or not cik.isascii()
            or not cik.isdigit()
        ):
            return None

        host = COMPANYFACTS_HOST_LIVE
        path = f"/api/xbrl/companyfacts/CIK{cik}.json"
        url = f"https://{host}{path}"
        parts = urllib.parse.urlsplit(url)
        if (parts.scheme, parts.hostname, parts.path, parts.port, parts.query) != (
            "https",
            host,
            path,
            None,
            "",
        ):
            return None
        return url


def _read_success(
    response,
) -> SECCompanyFactsTransportSuccess | SECCompanyFactsTransportFailure:
    if getattr(response, "status", None) != 200:
        return SECCompanyFactsTransportFailure(code="protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return SECCompanyFactsTransportFailure(code="unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > MAX_COMPANYFACTS_BODY_BYTES:
                return SECCompanyFactsTransportFailure(code="body_too_large")
        except ValueError:
            return SECCompanyFactsTransportFailure(
                code="unsupported_content_encoding"
            )
    body = response.read(MAX_COMPANYFACTS_BODY_BYTES + 1)
    if len(body) > MAX_COMPANYFACTS_BODY_BYTES:
        return SECCompanyFactsTransportFailure(code="body_too_large")
    return SECCompanyFactsTransportSuccess(body=body)


def _map_http_error(err: urllib.error.HTTPError) -> SECCompanyFactsTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return SECCompanyFactsTransportFailure(code="redirect")
    if code == 429:
        return SECCompanyFactsTransportFailure(code="http_429")
    if code in (401, 403):
        return SECCompanyFactsTransportFailure(code="auth_rejection")
    if 400 <= code < 500:
        return SECCompanyFactsTransportFailure(code="bad_request")
    if 500 <= code < 600:
        return SECCompanyFactsTransportFailure(code="http_5xx")
    return SECCompanyFactsTransportFailure(code="protocol_failure")
