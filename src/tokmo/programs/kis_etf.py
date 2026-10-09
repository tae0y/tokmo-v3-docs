"""Reusable bounded KIS reads, cached token and source-time-aware facts."""

import hashlib
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tokmo.facts.kis_etf import (
    decode_response,
    parse_etf_response,
    valid_secret,
    validate_query,
)
from tokmo.values.kis_etf import KisError, KisRequest


class KisEtfClient:
    def __init__(self, *, credentials, state, transport, clock, pacer, max_requests):
        if type(max_requests) is not int or max_requests < 1:
            raise KisError("request_budget")
        self.credentials, self.state, self.transport = credentials, state, transport
        self.clock, self.pacer, self.max_requests = clock, pacer, max_requests
        self.requests = 0
        self.auth_requests = 0
        self.stopped = False
        self.auth_status = "not_started"
        self.key_digest = hashlib.sha256(
            (credentials.app_key + "\0" + credentials.app_secret).encode()
        ).hexdigest()

    def _send(self, request):
        if self.stopped:
            raise KisError("connection_stopped")
        if self.requests >= self.max_requests:
            raise KisError("request_budget")
        if self.requests:
            self.pacer(0.5)
        self.requests += 1
        if request.operation == "auth":
            self.auth_requests += 1
        try:
            return self.transport(request)
        except Exception:
            self.stopped = True
            raise KisError("transport_failure") from None

    def _token(self):
        now = self.clock()
        if not isinstance(now, datetime) or now.utcoffset() is None:
            raise KisError("clock")
        cached = self.state.read_token()
        if cached is not None:
            try:
                if type(cached) is not dict or set(cached) != {
                    "key_digest",
                    "access_token",
                    "expires_at",
                }:
                    raise ValueError()
                expiry = datetime.fromisoformat(cached["expires_at"])
                if expiry.utcoffset() is None or not valid_secret(
                    cached["access_token"]
                ):
                    raise ValueError()
                if cached["key_digest"] == self.key_digest and expiry > now + timedelta(
                    seconds=60
                ):
                    if self.auth_status != "issued":
                        self.auth_status = "cache_reused"
                    return cached["access_token"]
            except (ValueError, TypeError, KeyError):
                raise KisError("token_cache_invalid") from None
        try:
            data = decode_response(
                self._send(KisRequest("auth", self.credentials)), auth=True
            )
            token, seconds = data.get("access_token"), data.get("expires_in")
            if (
                not valid_secret(token)
                or data.get("token_type", "").lower() != "bearer"
                or type(seconds) is not int
                or not 60 < seconds <= 172800
            ):
                raise KisError("token_contract")
            expiry = now + timedelta(seconds=seconds)
            if data.get("access_token_token_expired") is not None:
                official_expiry = datetime.strptime(
                    data["access_token_token_expired"], "%Y-%m-%d %H:%M:%S"
                ).replace(tzinfo=ZoneInfo("Asia/Seoul"))
                expiry = min(expiry, official_expiry)
            if expiry <= self.clock() + timedelta(seconds=60):
                raise KisError("token_expired")
            self.state.save_token(
                {
                    "key_digest": self.key_digest,
                    "access_token": token,
                    "expires_at": expiry.isoformat(),
                }
            )
            self.auth_status = "issued"
            return token
        except Exception as error:
            self.stopped = True
            if isinstance(error, KisError):
                raise
            raise KisError("authentication_failure") from None

    def fetch(self, operation, symbol):
        validate_query(operation, symbol)
        if self.stopped:
            raise KisError("connection_stopped")
        with self.state.locked():
            token = self._token()
            today = self.clock().astimezone(ZoneInfo("Asia/Seoul")).date()
            reply = self._send(
                KisRequest(
                    operation,
                    self.credentials,
                    token,
                    symbol,
                    (today - timedelta(days=14)).strftime("%Y%m%d")
                    if operation == "nav_daily"
                    else None,
                    today.strftime("%Y%m%d") if operation == "nav_daily" else None,
                )
            )
            try:
                decode_response(reply)
                if any(
                    value.encode() in reply.body
                    for value in (
                        self.credentials.app_key,
                        self.credentials.app_secret,
                        token,
                    )
                ):
                    raise KisError("private_value_in_quote")
                self.state.save_quote_source(operation, symbol, reply.body)
                fact = parse_etf_response(
                    operation, symbol, reply, received_at=self.clock()
                )
            except KisError:
                # Authorization, protocol and provider failures stop this client;
                # they do not cause token refresh or implicit retries.
                self.stopped = True
                raise
            self.state.save_fact(fact)
            return fact
