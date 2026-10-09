"""Pinned, direct, single-call official KRX HTTPS adapter."""

import http.client

from tokmo.facts.krx import (
    ENDPOINT_ID,
    REQUEST_TIMEOUT_SECONDS,
    KrxRequest,
    _KrxTransportFailure,
    _KrxTransportSuccess,
)

KRX_HOST = "data-dbg.krx.co.kr"
KRX_PATH = "/svc/apis/sto/stk_bydd_trd"
MAX_BODY_BYTES = 4 * 1024 * 1024
_REDIRECTS = frozenset({301, 302, 303, 307, 308})


class KrxLiveTransport:
    def __init__(self, *, connection_factory=http.client.HTTPSConnection):
        self._connection_factory = connection_factory

    def __call__(self, request: KrxRequest):
        try:
            if (
                request.endpoint_id != ENDPOINT_ID
                or request.timeout_seconds != REQUEST_TIMEOUT_SECONDS
                or not _date_digits(request.base_date)
                or not isinstance(request.api_key, str)
                or not request.api_key
            ):
                return _KrxTransportFailure("protocol_failure")
            path = f"{KRX_PATH}?basDd={request.base_date}"
            connection = self._connection_factory(
                KRX_HOST, timeout=request.timeout_seconds
            )
            try:
                connection.request(
                    "GET",
                    path,
                    headers={
                        "AUTH_KEY": request.api_key,
                        "Accept-Encoding": "identity",
                    },
                )
                response = connection.getresponse()
                if response.status != 200:
                    return _KrxTransportFailure(_status_code(response.status))
                encoding = response.getheader("Content-Encoding")
                if encoding and encoding.strip().lower() != "identity":
                    return _KrxTransportFailure("unsupported_content_encoding")
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    return _KrxTransportFailure("body_too_large")
                return _KrxTransportSuccess(body)
            finally:
                connection.close()
        except TimeoutError:
            return _KrxTransportFailure("timeout")
        except http.client.HTTPException:
            return _KrxTransportFailure("protocol_failure")
        except OSError:
            return _KrxTransportFailure("connection_failure")
        except Exception:
            return _KrxTransportFailure("protocol_failure")


def _status_code(status):
    if status in _REDIRECTS:
        return "redirect"
    if status in (401, 403):
        return "auth_rejection"
    if status == 429:
        return "http_429"
    if isinstance(status, int) and 500 <= status < 600:
        return "http_5xx"
    if isinstance(status, int) and 400 <= status < 500:
        return "bad_request"
    return "protocol_failure"


def _date_digits(value):
    if (
        not isinstance(value, str)
        or len(value) != 8
        or not value.isascii()
        or not value.isdigit()
    ):
        return False
    try:
        from datetime import datetime

        datetime.strptime(value, "%Y%m%d")
    except ValueError:
        return False
    return True
