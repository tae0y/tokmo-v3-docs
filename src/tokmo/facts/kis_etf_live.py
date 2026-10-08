"""Sealed KIS authentication/ETF HTTPS transport and private key-file loader."""
import json
import os
from pathlib import Path
import stat
import urllib.error
import urllib.parse
import urllib.request

from tokmo.facts.kis_etf import build_request, valid_secret, TR_IDS
from tokmo.values.kis_etf import KisCredentials, KisResponse, KisError

ORIGIN = "https://openapi.koreainvestment.com:9443"


def load_kis_credentials(path):
    path = Path(path)
    try:
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or stat.S_IMODE(info.st_mode) != 0o600 or info.st_uid != os.getuid() or info.st_size > 20000:
            raise KisError("credential_unsafe")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            opened = os.fstat(fd)
            if (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_uid) != (info.st_dev, info.st_ino, info.st_mode, info.st_uid):
                raise KisError("credential_unsafe")
            raw = os.read(fd, 20001)
        finally:
            os.close(fd)
        if len(raw) > 20000:
            raise KisError("credential_size")
        values = {}
        for line in raw.decode("ascii").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, value = line.split("=", 1)
            key, value = key.strip(), value.strip()
            if key in values or not valid_secret(value):
                raise KisError("credential_format")
            values[key] = value
        if set(values) != {"KIS_APP_KEY", "KIS_APP_SECRET"}:
            raise KisError("credential_fields")
        return KisCredentials(values["KIS_APP_KEY"], values["KIS_APP_SECRET"])
    except KisError:
        raise
    except (OSError, ValueError, UnicodeError):
        raise KisError("credential_load") from None


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def build_kis_opener():
    opener = urllib.request.OpenerDirector()
    for handler in (urllib.request.ProxyHandler({}), urllib.request.HTTPSHandler(), _NoRedirect(),
                    urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPErrorProcessor()):
        opener.add_handler(handler)
    return opener


class KisLiveTransport:
    def __init__(self, *, opener):
        self.opener = opener

    def __call__(self, request):
        method, path, params, body = build_request(request)
        url = ORIGIN + path + ("?" + urllib.parse.urlencode(params) if params else "")
        headers = {"Content-Type": "application/json; charset=utf-8", "Accept": "application/json",
                   "Accept-Encoding": "identity", "User-Agent": "tokmo-kis-etf/1"}
        if request.operation != "auth":
            headers.update(authorization="Bearer " + request.token, appkey=request.credentials.app_key,
                           appsecret=request.credentials.app_secret, tr_id=TR_IDS[request.operation], custtype="P", tr_cont="")
        wire = urllib.request.Request(url, data=None if body is None else json.dumps(body).encode(), method=method, headers=headers)
        try:
            with self.opener.open(wire, timeout=10) as reply:
                return _read(reply)
        except urllib.error.HTTPError as error:
            try:
                if 300 <= error.code < 400:
                    raise KisError("redirect_refused")
                return _read(error)
            finally:
                error.close()
        except (TimeoutError, urllib.error.URLError, OSError):
            raise KisError("connection_failure") from None


def _read(reply):
    if reply.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
        raise KisError("response_encoding")
    declared = reply.headers.get("Content-Length")
    if declared is not None:
        try:
            length = int(declared)
        except ValueError:
            raise KisError("response_size") from None
        if not 0 <= length <= 1048576:
            raise KisError("response_size")
    body = reply.read(1048577)
    if len(body) > 1048576:
        raise KisError("response_size")
    return KisResponse(reply.status, body, reply.headers.get("tr_cont", ""))
