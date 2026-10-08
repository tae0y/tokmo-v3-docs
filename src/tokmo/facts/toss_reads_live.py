"""Hardened one-attempt HTTPS GET transport for the 20 remaining Toss reads.

The host is a module constant, the method is a module constant (``GET``),
and the path/rate-group/account-rule are consumed from the closed spec
table. There is no caller host/path/method argument, no proxy discovery,
no redirect following, no retry, and a 2 MiB body cap.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.toss import (
    _TossTransportFailure,
    _TossTransportSuccess,
    valid_account_seq,
    valid_token,
)
from tokmo.facts.toss_reads import (
    REQUEST_TIMEOUT_SECONDS,
    SPEC_TABLE,
    TossReadOperation,
    TossReadRequest,
    valid_account_seq_int64,
    valid_iso_date,
)

__all__ = [
    "TOSS_HOST",
    "METHOD",
    "ACCOUNT_HEADER",
    "MAX_BODY_BYTES",
    "TossReadsLiveTransport",
    "build_live_opener",
]

TOSS_HOST = "openapi.tossinvest.com"
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


class TossReadsLiveTransport:
    """One authorized read per call; returns the source-private result union."""

    def __init__(self, *, https_handler=None) -> None:
        self.opener = build_live_opener(https_handler)

    def __call__(
            self,
            request: TossReadRequest) -> _TossTransportSuccess | _TossTransportFailure:
        try:
            url = _wire_url(request)
            headers = _wire_headers(request)
            if url is None or headers is None:
                return _TossTransportFailure(code="protocol_failure")
            wire = urllib.request.Request(
                url, data=None, method=METHOD, headers=headers)
            with self.opener.open(
                    wire, timeout=request.timeout_seconds) as response:
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


# =========================================================================
# Wire URL construction — host/path/query all from the closed spec table
# =========================================================================

def _wire_url(request: TossReadRequest) -> str | None:
    """Construct the wire-exact URL from the request's validated parameters.

    Returns ``None`` if any invariant is violated, which causes the caller
    to refuse before any I/O.
    """
    if request.timeout_seconds != REQUEST_TIMEOUT_SECONDS:
        return None
    spec = SPEC_TABLE.get(request.operation)
    if spec is None:
        return None

    # Account-header rule enforcement before I/O.
    if spec.account_rule == "required":
        if not valid_account_seq(request.account_seq):
            return None
    else:
        if request.account_seq is not None:
            return None

    op = request.operation.value
    path = spec.path

    # Build path with path parameters substituted.
    path_params = {}
    if "{symbol}" in path:
        sym = request.symbol
        if not sym:
            return None
        path_params["symbol"] = sym
    if "{orderId}" in path:
        oid = request.order_id
        if not oid:
            return None
        path_params["orderId"] = oid
    if "{conditionalOrderId}" in path:
        coid = request.conditional_order_id
        if not coid:
            return None
        path_params["conditionalOrderId"] = coid

    for key, val in path_params.items():
        placeholder = "{" + key + "}"
        if placeholder in path:
            path = path.replace(placeholder, urllib.parse.quote(val, safe=""))

    # Build query string.
    query_parts = _build_query(request, op)
    if query_parts is None:
        return None

    query = "&".join(query_parts)
    url = f"https://{TOSS_HOST}{path}"
    if query:
        url += f"?{query}"

    # Re-parse to verify integrity.
    parts = urllib.parse.urlsplit(url)
    if parts.scheme != "https" or parts.hostname != TOSS_HOST or parts.port is not None:
        return None
    # Verify the path after substitution has no remaining braces.
    if "{" in parts.path or "}" in parts.path:
        return None
    return url


def _build_query(request: TossReadRequest, op: str) -> list[str] | None:
    """Build the wire-exact query string parts for the operation.

    Returns ``None`` on any invariant failure.
    """
    parts: list[str] = []

    if op == "BUYING_POWER":
        if request.currency not in {"KRW", "USD"}:
            return None
        parts.append(f"currency={request.currency}")

    elif op == "SELLABLE_QUANTITY":
        if not request.symbol:
            return None
        parts.append(f"symbol={_q(request.symbol)}")

    elif op == "COMMISSIONS":
        pass

    elif op == "ORDERBOOK":
        parts.append(f"symbol={_q(request.symbol)}")

    elif op == "TRADES":
        parts.append(f"symbol={_q(request.symbol)}")
        if request.count is not None:
            parts.append(f"count={request.count}")

    elif op == "PRICE_LIMITS":
        parts.append(f"symbol={_q(request.symbol)}")

    elif op == "STOCK_CANDLES":
        parts.append(f"symbol={_q(request.symbol)}")
        parts.append(f"interval={request.interval}")
        if request.count is not None:
            parts.append(f"count={request.count}")
        if request.before is not None:
            parts.append(f"before={_dt(request.before)}")
        if request.adjusted is not None:
            parts.append(f"adjusted={'true' if request.adjusted else 'false'}")

    elif op == "STOCKS":
        joined = ",".join(request.symbols)
        parts.append(f"symbols={urllib.parse.quote(joined, safe=',.-')}")

    elif op == "STOCK_INVESTOR_TRADING":
        if re.fullmatch(r"[0-9A-Z]{6}", request.symbol) is None:
            return None
        if request.count is not None:
            if type(request.count) is not int or not 1 <= request.count <= 100: return None
            parts.append(f"count={request.count}")
        if request.until:
            if not valid_iso_date(request.until): return None
            parts.append(f"until={request.until}")

    elif op == "STOCK_WARNINGS":
        pass  # symbol is in path

    elif op == "EXCHANGE_RATE":
        if request.date_time is not None:
            parts.append(f"dateTime={_dt(request.date_time)}")
        parts.append(f"baseCurrency={request.base_currency}")
        parts.append(f"quoteCurrency={request.quote_currency}")

    elif op == "KR_MARKET_CALENDAR":
        if request.date:
            if not valid_iso_date(request.date):
                return None
            parts.append(f"date={request.date}")

    elif op == "US_MARKET_CALENDAR":
        if request.date:
            if not valid_iso_date(request.date):
                return None
            parts.append(f"date={request.date}")

    elif op == "RANKINGS":
        parts.append(f"type={request.ranking_type}")
        parts.append(f"marketCountry={request.market_country}")
        parts.append(f"duration={request.duration}")
        if request.exclude_investment_caution is not None:
            parts.append(
                f"excludeInvestmentCaution="
                f"{'true' if request.exclude_investment_caution else 'false'}")
        if request.count is not None:
            parts.append(f"count={request.count}")

    elif op == "INDICATOR_PRICES":
        joined = ",".join(request.symbols)
        parts.append(f"symbols={urllib.parse.quote(joined, safe='_,')}")

    elif op == "INDICATOR_CANDLES":
        pass  # symbol in path
        parts.append(f"interval={request.interval}")
        if request.count is not None:
            parts.append(f"count={request.count}")
        if request.before is not None:
            parts.append(f"before={_dt(request.before)}")

    elif op == "INDICATOR_INVESTOR_TRADING":
        pass  # symbol in path
        parts.append(f"interval={request.interval}")
        if request.count is not None:
            parts.append(f"count={request.count}")
        if request.until:
            if not valid_iso_date(request.until):
                return None
            parts.append(f"until={request.until}")

    elif op == "ORDERS":
        parts.append(f"status={request.status}")
        if request.symbol:
            parts.append(f"symbol={_q(request.symbol)}")
        if request.from_date:
            if not valid_iso_date(request.from_date):
                return None
            parts.append(f"from={request.from_date}")
        if request.to_date:
            if not valid_iso_date(request.to_date):
                return None
            parts.append(f"to={request.to_date}")
        if request.cursor:
            parts.append(f"cursor={urllib.parse.quote(request.cursor, safe='')}")
        if request.limit is not None:
            parts.append(f"limit={request.limit}")

    elif op == "ORDER_DETAIL":
        pass  # orderId in path

    elif op == "CONDITIONAL_ORDERS":
        parts.append(f"status={request.status}")
        if request.symbol:
            parts.append(f"symbol={_q(request.symbol)}")
        if request.cursor:
            parts.append(f"cursor={request.cursor}")
        if request.limit is not None:
            parts.append(f"limit={request.limit}")

    elif op == "CONDITIONAL_ORDER_DETAIL":
        pass  # conditionalOrderId in path

    else:
        return None

    return parts


def _q(value: str) -> str:
    """Percent-encode a query symbol value."""
    return urllib.parse.quote(value, safe=".-")


def _dt(value) -> str:
    """ISO datetime with ``+`` encoded as ``%2B``."""
    iso = value.isoformat()
    return urllib.parse.quote(iso, safe=":-")


# =========================================================================
# Wire headers
# =========================================================================

def _wire_headers(request: TossReadRequest) -> dict[str, str] | None:
    if not valid_token(request.token):
        return None
    spec = SPEC_TABLE.get(request.operation)
    if spec is None:
        return None
    headers = {
        "Authorization": f"{AUTH_SCHEME} {request.token}",
        "Accept-Encoding": "identity",
    }
    if spec.account_rule == "required":
        if not valid_account_seq(request.account_seq):
            return None
        if not valid_account_seq_int64(request.account_seq):
            return None
        headers[ACCOUNT_HEADER] = request.account_seq
    else:
        if request.account_seq is not None:
            return None
    return headers


# =========================================================================
# Response reading
# =========================================================================

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
            response.headers.get("X-RateLimit-Remaining"),
            MAX_RATE_LIMIT_REMAINING),
    )


def _map_http_error(err: urllib.error.HTTPError) -> _TossTransportFailure:
    code = err.code
    if code in _REDIRECT_CODES:
        return _TossTransportFailure(code="redirect")
    if code == 429:
        return _TossTransportFailure(
            code="http_429",
            retry_after=_bounded_int(
                err.headers.get("Retry-After") if err.headers else None,
                MAX_RETRY_AFTER),
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
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except ValueError:
        return None
    return min(ceiling, max(0, value))
