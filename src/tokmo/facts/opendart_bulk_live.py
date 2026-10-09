"""Authorized GET-only composition leaf for the bulk disclosure lane."""

import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.opendart_bulk import BulkRequest, BulkTransportFailure

HOST = "opendart.fss.or.kr"
PATH = "/api/list.json"
METHOD = "GET"
TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 2 * 1024 * 1024


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class OpenDartBulkLiveTransport:
    def __init__(self, *, https_handler=None):
        self.opener = urllib.request.OpenerDirector()
        for handler in (
            urllib.request.ProxyHandler({}),
            urllib.request.HTTPSHandler() if https_handler is None else https_handler,
            _NoRedirect(),
            urllib.request.HTTPDefaultErrorHandler(),
            urllib.request.HTTPErrorProcessor(),
        ):
            self.opener.add_handler(handler)

    def __call__(self, request: BulkRequest, api_key: str) -> bytes:
        if not isinstance(api_key, str) or not api_key:
            raise BulkTransportFailure("protocol_failure")
        pairs = (*request.query_pairs, ("crtfc_key", api_key))
        query = urllib.parse.urlencode(pairs)
        wire = urllib.request.Request(
            f"https://{HOST}{PATH}?{query}",
            data=None,
            method=METHOD,
            headers={"Accept-Encoding": "identity"},
        )
        try:
            with self.opener.open(wire, timeout=TIMEOUT_SECONDS) as response:
                if response.status != 200:
                    raise BulkTransportFailure("protocol_failure")
                encoding = response.headers.get("Content-Encoding")
                if encoding and encoding.lower().strip() != "identity":
                    raise BulkTransportFailure("unsupported_content_encoding")
                body = response.read(MAX_BODY_BYTES + 1)
                if len(body) > MAX_BODY_BYTES:
                    raise BulkTransportFailure("body_too_large")
                return body
        except urllib.error.HTTPError as error:
            if error.code in (401, 403):
                code = "auth_rejection"
            elif error.code == 429:
                code = "http_429"
            elif 500 <= error.code < 600:
                code = "http_5xx"
            elif error.code in (301, 302, 303, 307, 308):
                code = "redirect"
            elif 400 <= error.code < 500:
                code = "bad_request"
            else:
                code = "protocol_failure"
            raise BulkTransportFailure(code) from None
        except urllib.error.URLError as error:
            code = (
                "timeout"
                if isinstance(getattr(error, "reason", None), TimeoutError)
                else "connection_failure"
            )
            raise BulkTransportFailure(code) from None
        except TimeoutError:
            raise BulkTransportFailure("timeout") from None
        except OSError:
            raise BulkTransportFailure("connection_failure") from None
        except BulkTransportFailure:
            raise
        except Exception:
            raise BulkTransportFailure("protocol_failure") from None
