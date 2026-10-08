"""Single-call sealed HTTPS adapter for the five FRED operations."""

from dataclasses import dataclass, field
from pathlib import Path
import json
import os
import re
import stat
import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.fred import PATHS, build_query_pairs
from tokmo.values.fred_macro import FredRequest

HOST = "api.stlouisfed.org"
TIMEOUT_SECONDS = 10.0
MAX_BODY_BYTES = 1_048_576
MAX_CREDENTIAL_BYTES = 4096
FAILURE_CODES = frozenset({
    "timeout", "connection_failure", "redirect", "auth_rejection",
    "bad_request", "http_429", "http_5xx", "body_too_large",
    "unsupported_content_encoding", "protocol_failure",
    "api_key_not_registered", "provider_bad_request_unknown",
})

_UNREGISTERED_MESSAGE = ("Bad Request.  The value for variable api_key is not "
                         "registered.  Read https://fred.stlouisfed.org/docs/api/"
                         "api_key.html for more information.")


@dataclass(frozen=True)
class FredTransportOutcome:
    code: str
    body: bytes | None = field(default=None, repr=False)

    def __post_init__(self):
        if self.code not in FAILURE_CODES | {"success"}:
            raise ValueError("closed_transport_code")
        if (self.code == "success") != (type(self.body) is bytes):
            raise ValueError("transport_body_contract")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_live_opener():
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())


def load_fred_api_key(path: Path) -> str:
    try:
        info = os.lstat(path)
    except OSError as error:
        raise ValueError("credential_unsafe") from None
    uid = os.getuid() if hasattr(os, "getuid") else None
    if (not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode)
            or stat.S_IMODE(info.st_mode) != 0o600
            or uid is None or info.st_uid != uid or info.st_size > MAX_CREDENTIAL_BYTES):
        raise ValueError("credential_unsafe")
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError("credential_unsafe")
            raw = os.read(descriptor, MAX_CREDENTIAL_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        raise ValueError("credential_unsafe") from None
    if len(raw) > MAX_CREDENTIAL_BYTES or not raw.endswith(b"\n") or b"\r" in raw:
        raise ValueError("credential_grammar")
    try:
        lines = raw.decode("ascii").splitlines()
    except UnicodeDecodeError:
        raise ValueError("credential_grammar") from None
    assignments = [line for line in lines if line and not line.startswith("#")]
    if len(assignments) != 1 or re.fullmatch(r"FRED_API_KEY=([a-z0-9]{32})", assignments[0]) is None:
        raise ValueError("credential_grammar")
    return assignments[0].split("=", 1)[1]


class FredLiveTransport:
    def __init__(self, *, opener):
        self._opener = opener

    def __call__(self, request: FredRequest, api_key: str) -> FredTransportOutcome:
        try:
            if re.fullmatch(r"[a-z0-9]{32}", api_key) is None:
                return FredTransportOutcome("protocol_failure")
            expected = (*build_query_pairs(request), ("api_key", api_key))
            query = "&".join(f"{urllib.parse.quote(key, safe='')}={urllib.parse.quote(value, safe='')}"
                             for key, value in expected)
            url = f"https://{HOST}{PATHS[request.operation]}?{query}"
            parts = urllib.parse.urlsplit(url)
            parsed_pairs = tuple(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
            if ((parts.scheme, parts.hostname, parts.port, parts.path) !=
                    ("https", HOST, None, PATHS[request.operation]) or parsed_pairs != expected):
                return FredTransportOutcome("protocol_failure")
            wire = urllib.request.Request(url, data=None, method="GET", headers={
                "User-Agent": "tokmo-fred-canonical/1",
                "Accept": "application/json",
                "Accept-Encoding": "identity",
            })
            wire_parts = urllib.parse.urlsplit(wire.full_url)
            wire_pairs = tuple(urllib.parse.parse_qsl(wire_parts.query, keep_blank_values=True))
            if ((wire_parts.scheme, wire_parts.hostname, wire_parts.port, wire_parts.path) !=
                    ("https", HOST, None, PATHS[request.operation])
                    or wire_pairs != expected or wire.method != "GET"):
                return FredTransportOutcome("protocol_failure")
            with self._opener.open(wire, timeout=TIMEOUT_SECONDS) as response:
                return _read_response(response)
        except BaseException as error:
            code = _http_400_code(error) if (isinstance(error, urllib.error.HTTPError)
                                              and error.code == 400) else _failure_code(error)
            del error
            return FredTransportOutcome(code)


def _http_400_code(error: urllib.error.HTTPError) -> str:
    body = None
    try:
        try:
            body = error.read(4097)
        except Exception:
            body = None
        finally:
            try:
                error.close()
            except Exception:
                pass
        encoding = error.headers.get("Content-Encoding")
        if (encoding is not None and encoding.strip().lower() != "identity") or type(body) is not bytes or len(body) > 4096:
            return "provider_bad_request_unknown"
        def unique(pairs):
            result = {}
            for key, value in pairs:
                if key in result:
                    raise ValueError("duplicate")
                result[key] = value
            return result
        value = json.loads(body.decode("utf-8"), object_pairs_hook=unique)
        if (type(value) is dict and set(value) == {"error_code", "error_message"}
                and type(value["error_code"]) is int and value["error_code"] == 400
                and type(value["error_message"]) is str
                and value["error_message"] == _UNREGISTERED_MESSAGE):
            return "api_key_not_registered"
    except Exception:
        pass
    return "provider_bad_request_unknown"


def _read_response(response) -> FredTransportOutcome:
    if getattr(response, "status", None) != 200:
        return FredTransportOutcome("protocol_failure")
    encoding = response.headers.get("Content-Encoding")
    if encoding is not None and encoding.strip().lower() != "identity":
        return FredTransportOutcome("unsupported_content_encoding")
    declared = response.headers.get("Content-Length")
    if declared is not None:
        try:
            if int(declared) > MAX_BODY_BYTES or int(declared) < 0:
                return FredTransportOutcome("body_too_large")
        except (TypeError, ValueError):
            return FredTransportOutcome("protocol_failure")
    body = response.read(MAX_BODY_BYTES + 1)
    if type(body) is not bytes or len(body) > MAX_BODY_BYTES:
        return FredTransportOutcome("body_too_large")
    return FredTransportOutcome("success", body)


def _failure_code(error: BaseException) -> str:
    if isinstance(error, urllib.error.HTTPError):
        if error.code in (301, 302, 303, 307, 308): return "redirect"
        if error.code in (401, 403): return "auth_rejection"
        if error.code == 429: return "http_429"
        if 400 <= error.code < 500: return "bad_request"
        if 500 <= error.code < 600: return "http_5xx"
        return "protocol_failure"
    if isinstance(error, urllib.error.URLError):
        return "timeout" if isinstance(getattr(error, "reason", None), TimeoutError) else "connection_failure"
    if isinstance(error, TimeoutError): return "timeout"
    if isinstance(error, OSError): return "connection_failure"
    return "protocol_failure"
