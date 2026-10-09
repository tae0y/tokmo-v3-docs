"""Bounded GET primitive shared only by approved NEWS source adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener

from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_OUTPUT_MAX_BYTES,
    NewsClassifierFailure,
)
from tokmo.values.news_connectors import NewsFetchStatus

_TIMEOUT_SECONDS = 10.0
_BODY_CAP = 1_048_576
_ZAI_CHAT_COMPLETIONS_PATH = "/api/coding/paas/v4/chat/completions"
_ZAI_DEFAULT_HOST = "https://api.z.ai"
OUTPUT_MAX_BYTES = NEWS_CLASSIFIER_OUTPUT_MAX_BYTES
MAX_TOKENS = 512


@dataclass(frozen=True)
class ClosedNewsGet:
    host: str = field(repr=False)
    path: str = field(repr=False)
    query: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    headers: tuple[tuple[str, str], ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class NewsHttpResponse:
    status: NewsFetchStatus
    http_status: int | None = None
    body: bytes = field(default=b"", repr=False)
    failure_class: str | None = None


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class NewsHttpTransport:
    """Execute a validated HTTPS GET without proxy, redirects, or retries."""

    def __init__(self, *, allowed_hosts: frozenset[str], opener: Any = None) -> None:
        self._allowed_hosts = allowed_hosts
        self._opener = opener or build_opener(ProxyHandler({}), _NoRedirect())

    def get(self, request: ClosedNewsGet) -> NewsHttpResponse:
        if not self._valid(request):
            return NewsHttpResponse(NewsFetchStatus.CONTRACT_MISMATCH)
        url = f"https://{request.host}{request.path}"
        if request.query:
            url = f"{url}?{urlencode(request.query)}"
        try:
            raw = Request(url, method="GET", headers=dict(request.headers))
            with self._opener.open(raw, timeout=_TIMEOUT_SECONDS) as response:
                code = int(response.status)
                if code != 200:
                    return NewsHttpResponse(NewsFetchStatus.TRANSPORT_FAILURE, code)
                content_encoding = response.headers.get("Content-Encoding", "identity")
                if content_encoding.lower() not in ("", "identity"):
                    return NewsHttpResponse(NewsFetchStatus.CONTRACT_MISMATCH, code)
                declared = response.headers.get("Content-Length")
                if declared is not None and (
                    not declared.isdigit() or int(declared) > _BODY_CAP
                ):
                    return NewsHttpResponse(NewsFetchStatus.CONTRACT_MISMATCH, code)
                body = response.read(_BODY_CAP + 1)
                if len(body) > _BODY_CAP:
                    return NewsHttpResponse(NewsFetchStatus.CONTRACT_MISMATCH, code)
                return NewsHttpResponse(NewsFetchStatus.COLLECTED, code, body)
        except HTTPError as exc:
            if exc.code == 429:
                status = NewsFetchStatus.RATE_LIMITED
            elif exc.code in (401, 403):
                status = NewsFetchStatus.CONFIG_UNAVAILABLE
            else:
                status = NewsFetchStatus.TRANSPORT_FAILURE
            return NewsHttpResponse(status, exc.code)
        except (URLError, TimeoutError, OSError) as error:
            reason = error.reason if isinstance(error, URLError) else error
            cause = (
                "TIMEOUT" if isinstance(reason, TimeoutError) else "CONNECTION_ERROR"
            )
            return NewsHttpResponse(
                NewsFetchStatus.TRANSPORT_FAILURE, failure_class=cause
            )

    def _valid(self, request: ClosedNewsGet) -> bool:
        if type(request) is not ClosedNewsGet:
            return False
        if (
            request.host not in self._allowed_hosts
            or not request.host
            or len(request.host) > 253
        ):
            return False
        if (
            not request.path.startswith("/")
            or len(request.path) > 2_000
            or "\r" in request.path
            or "\n" in request.path
        ):
            return False
        if len(request.query) > 16 or len(request.headers) > 16:
            return False
        for pairs in (request.query, request.headers):
            for key, value in pairs:
                if (
                    type(key) is not str
                    or type(value) is not str
                    or not key
                    or len(key) > 200
                    or len(value) > 4_000
                    or "\r" in key + value
                    or "\n" in key + value
                ):
                    return False
        return True


def zai_chat_endpoint(base_url=None):
    base = (base_url or _ZAI_DEFAULT_HOST).strip()
    host = (
        "https://open.bigmodel.cn"
        if ("open.bigmodel.cn" in base or "dev.bigmodel.cn" in base)
        else _ZAI_DEFAULT_HOST
    )
    return host + _ZAI_CHAT_COMPLETIONS_PATH


def zai_chat_once(
    prompt_text,
    *,
    timeout_seconds,
    api_key,
    base_url=None,
    opener=None,
    model=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    max_tokens=MAX_TOKENS,
    json_mode=False,
):
    """One bounded direct chat completion; retry and fallback are zero.

    ``max_tokens`` defaults to the NEWS classifier bound; the Quant finite
    canary passes its own larger closed-JSON bound through the same single
    non-agent POST (no tools, no session, no second route).
    ``json_mode`` opts into JSON syntax mode, without required-key guarantees."""
    if type(json_mode) is not bool:
        raise TypeError("json_mode must be bool")
    if not isinstance(api_key, str) or not api_key.strip():
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt_text}],
        "stream": False,
        "thinking": {"type": "enabled"},
        "reasoning_effort": "low",
        "max_tokens": max_tokens,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = Request(
        zai_chat_endpoint(base_url),
        data=body,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept-Language": "en-US,en",
        },
        method="POST",
    )
    handler = opener or build_opener(ProxyHandler({}), _NoRedirect())
    try:
        with handler.open(request, timeout=timeout_seconds) as response:
            raw = response.read(OUTPUT_MAX_BYTES + 1)
            status = int(getattr(response, "status", None) or response.getcode())
    except HTTPError as error:
        status = error.code
        if status == 429:
            failure = NewsClassifierFailure("QUOTA_EXCEEDED")
        elif status in (401, 403) or status >= 500:
            failure = NewsClassifierFailure("MODEL_UNAVAILABLE")
        else:
            failure = NewsClassifierFailure("TRANSPORT_FAIL")
        failure.failure_diagnostics = {
            "http_status": status,
            "error_class": "http_error",
        }
        raise failure from error
    except URLError as error:
        if isinstance(error.reason, TimeoutError):
            raise TimeoutError("glm call exceeded the bounded call budget") from error
        failure = NewsClassifierFailure("TRANSPORT_FAIL")
        failure.failure_diagnostics = {
            "http_status": None,
            "error_class": "connection_error",
        }
        raise failure from error
    except TimeoutError as error:
        raise TimeoutError("glm call exceeded the bounded call budget") from error
    if len(raw) > OUTPUT_MAX_BYTES:
        raise json.JSONDecodeError("model output exceeds the bounded cap", "", 0)
    try:
        payload = json.loads(raw)
        content = payload["choices"][0]["message"]["content"]
    except (ValueError, KeyError, IndexError, TypeError) as error:
        failure = NewsClassifierFailure("MODEL_UNAVAILABLE")
        failure.failure_diagnostics = {
            "http_status": status,
            "error_class": "malformed_response",
        }
        raise failure from error
    if not isinstance(content, str):
        failure = NewsClassifierFailure("MODEL_UNAVAILABLE")
        failure.failure_diagnostics = {
            "http_status": status,
            "error_class": "malformed_response",
        }
        raise failure
    return content
