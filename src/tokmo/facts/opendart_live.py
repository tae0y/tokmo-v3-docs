"""Small, single-call HTTPS adapter for the two official OpenDART endpoints.

The API credential joins the wire query as the final pair immediately before
the request and never appears in argv, logs, artifacts, exceptions, or
return values.
"""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.opendart import (
    CORP_CODE_DIGITS,
    CORP_CODE_ENDPOINT_ID,
    DATE_DIGITS,
    LIST_ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    OpenDartRequest,
    _exact_digits,
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
)
from tokmo.facts.opendart_source_contract import OpenDartTraceJsonRequest

OPENDART_HOST = "opendart.fss.or.kr"
CORP_CODE_PATH = "/api/corpCode.xml"
LIST_PATH = "/api/list.json"
METHOD = "GET"
MAX_ZIP_BODY_BYTES = 8 * 1024 * 1024
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
TRACE_JSON_PATHS = {
    "fnlttSinglAcnt": "/api/fnlttSinglAcnt.json",
    "fnlttSinglAcntAll": "/api/fnlttSinglAcntAll.json",
    "stockTotqySttus": "/api/stockTotqySttus.json",
}

# Fixed non-secret list query pairs: explicit descending date sort and a
# single first-page row, so list[0] is the latest disclosure in range.
LIST_FIXED_PAIRS = (
    ("sort", "date"), ("sort_mth", "desc"), ("page_no", "1"), ("page_count", "1"),
)

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


class OpenDartLiveTransport:
    """One official GET per call; returns the source-private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(self, request: OpenDartRequest,
                 ) -> _OpenDartTransportSuccess | _OpenDartTransportFailure:
        try:
            wire_plan = _wire_url(request)
            if wire_plan is None:
                return _OpenDartTransportFailure(code="protocol_failure")
            url, max_body = wire_plan
            wire = urllib.request.Request(
                url, data=None, method=METHOD,
                headers={"Accept-Encoding": "identity"},
            )
            with self.opener.open(wire, timeout=request.timeout_seconds) as response:
                return _read_success(response, max_body)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            reason = getattr(err, "reason", None)
            if isinstance(reason, TimeoutError):
                return _OpenDartTransportFailure(code="timeout")
            return _OpenDartTransportFailure(code="connection_failure")
        except TimeoutError:
            return _OpenDartTransportFailure(code="timeout")
        except OSError:
            return _OpenDartTransportFailure(code="connection_failure")
        except Exception:
            return _OpenDartTransportFailure(code="protocol_failure")

    def trace_json(self, request: "OpenDartTraceJsonRequest",
                   ) -> _OpenDartTransportSuccess | _OpenDartTransportFailure:
        """One production-signature GET; reused by the bounded KR fundamentals enrichment."""
        try:
            wire_plan = _trace_wire_url(request)
            if wire_plan is None:
                return _OpenDartTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                wire_plan, data=None, method=METHOD,
                headers={"Accept-Encoding": "identity"},
            )
            with self.opener.open(wire, timeout=request.timeout_seconds) as response:
                return _read_success(response, MAX_JSON_BODY_BYTES)
        except urllib.error.HTTPError as err:
            return _map_http_error(err)
        except urllib.error.URLError as err:
            if isinstance(getattr(err, "reason", None), TimeoutError):
                return _OpenDartTransportFailure(code="timeout")
            return _OpenDartTransportFailure(code="connection_failure")
        except TimeoutError:
            return _OpenDartTransportFailure(code="timeout")
        except OSError:
            return _OpenDartTransportFailure(code="connection_failure")
        except Exception:
            return _OpenDartTransportFailure(code="protocol_failure")


def _trace_wire_url(request: OpenDartTraceJsonRequest) -> str | None:
    path = TRACE_JSON_PATHS.get(request.endpoint)
    if (path is None or request.timeout_seconds != REQUEST_TIMEOUT_SECONDS
            or not _exact_digits(request.corp_code, CORP_CODE_DIGITS)
            or not _exact_digits(request.business_year, 4)
            or request.report_code != "11011" or not request.api_key):
        return None
    if request.endpoint == "fnlttSinglAcntAll":
        if request.fs_div != "CFS":
            return None
    elif request.fs_div is not None:
        return None
    pairs = [
        ("corp_code", request.corp_code),
        ("bsns_year", request.business_year),
        ("reprt_code", request.report_code),
    ]
    if request.fs_div is not None:
        pairs.append(("fs_div", request.fs_div))
    pairs.append(("crtfc_key", request.api_key))
    query = urllib.parse.urlencode(pairs, quote_via=urllib.parse.quote)
    url = f"https://{OPENDART_HOST}{path}?{query}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port) != (
            "https", OPENDART_HOST, path, None):
        return None
    return url


def _wire_url(request: OpenDartRequest) -> tuple[str, int] | None:
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    api_key = request.api_key
    if not isinstance(api_key, str) or not api_key:
        return None

    if request.endpoint_id == CORP_CODE_ENDPOINT_ID:
        if (request.corp_code, request.begin_date, request.end_date) != (
                None, None, None):
            return None
        path, max_body = CORP_CODE_PATH, MAX_ZIP_BODY_BYTES
        pairs = (("crtfc_key", api_key),)
    elif request.endpoint_id == LIST_ENDPOINT_ID:
        if not _exact_digits(request.corp_code, CORP_CODE_DIGITS):
            return None
        if (not _exact_digits(request.begin_date, DATE_DIGITS)
                or not _exact_digits(request.end_date, DATE_DIGITS)):
            return None
        path, max_body = LIST_PATH, MAX_JSON_BODY_BYTES
        pairs = (
            ("corp_code", request.corp_code),
            ("bgn_de", request.begin_date),
            ("end_de", request.end_date),
            *LIST_FIXED_PAIRS,
            ("crtfc_key", api_key),
        )
    else:
        return None

    query = "&".join(
        f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}"
        for key, value in pairs
    )
    url = f"https://{OPENDART_HOST}{path}?{query}"
    parts = urllib.parse.urlsplit(url)
    if (parts.scheme, parts.hostname, parts.path, parts.port) != (
            "https", OPENDART_HOST, path, None):
        return None
    return url, max_body


def _read_success(response, max_body: int,
                  ) -> _OpenDartTransportSuccess | _OpenDartTransportFailure:
    if getattr(response, "status", None) != 200:
        return _OpenDartTransportFailure(code="protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding and encoding.strip().lower() != "identity":
        return _OpenDartTransportFailure(code="unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared:
        try:
            if int(declared) > max_body:
                return _OpenDartTransportFailure(code="body_too_large")
        except ValueError:
            return _OpenDartTransportFailure(code="unsupported_content_encoding")
    body = response.read(max_body + 1)
    if len(body) > max_body:
        return _OpenDartTransportFailure(code="body_too_large")
    return _OpenDartTransportSuccess(body=body)


def _map_http_error(err: urllib.error.HTTPError) -> _OpenDartTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _OpenDartTransportFailure(code="redirect")
    if code == 429:
        return _OpenDartTransportFailure(
            code="http_429", retry_after=_retry_after_seconds(err.headers))
    if code in (401, 403):
        return _OpenDartTransportFailure(code="auth_rejection")
    if 400 <= code < 500:
        return _OpenDartTransportFailure(code="bad_request")
    if 500 <= code < 600:
        return _OpenDartTransportFailure(code="http_5xx")
    return _OpenDartTransportFailure(code="protocol_failure")


def _retry_after_seconds(headers) -> int | None:
    raw = headers.get("Retry-After")
    if raw is None:
        return None
    try:
        return min(3600, max(2, int(raw)))
    except (TypeError, ValueError):
        return None
