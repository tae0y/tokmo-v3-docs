"""EIA WTI parser/query. Transport and clock are mandatory injections."""

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import json

from tokmo.values import UnavailableFact, WtiFact, WTI_SERIES, WTI_SOURCE, WTI_UNITS

SOURCE = WTI_SOURCE
ENDPOINT_ID = "eia_petroleum_data"
SERIES = WTI_SERIES
UNITS = WTI_UNITS
REQUEST_TIMEOUT_SECONDS = 10.0

# Live contract §2: the exact non-secret query multimap, in wire order. The
# credential is never part of this map; the live transport appends it as the
# final pair immediately before the wire.
WTI_QUERY_MULTIMAP: tuple[tuple[str, str], ...] = (
    ("frequency", "daily"),
    ("data[0]", "value"),
    ("facets[series][]", SERIES),
    ("sort[0][column]", "period"),
    ("sort[0][direction]", "desc"),
    ("offset", "0"),
    ("length", "1"),
)

# Live contract §6/D1: closed outcome codes. The transport may only produce
# the failure subset; "success" and "response_contract_failure" are decided
# here after parsing.
LIVE_TRANSPORT_FAILURE_CODES = frozenset({
    "timeout", "connection_failure", "http_429", "http_5xx",
    "auth_rejection", "bad_request", "redirect", "body_too_large",
    "unsupported_content_encoding", "protocol_failure",
})
LIVE_ATTEMPT_CODES = LIVE_TRANSPORT_FAILURE_CODES | {"success", "response_contract_failure"}
RETRY_CANDIDATE_CODES = frozenset({"timeout", "connection_failure", "http_5xx", "http_429"})

# Closed mapping from transport failure codes to typed unavailability reasons.
_FAILURE_REASONS = {
    "timeout": "transport_error",
    "connection_failure": "transport_error",
    "http_5xx": "transport_error",
    "http_429": "rate_limited",
    "auth_rejection": "contract_mismatch",
    "bad_request": "contract_mismatch",
    "redirect": "transport_error",
    "body_too_large": "transport_error",
    "unsupported_content_encoding": "transport_error",
    "protocol_failure": "transport_error",
}


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: Mapping[str, str]


@dataclass(frozen=True)
class EiaRequest:
    """Request identity plus a separately-carried credential.

    The credential never joins ``params``, and ``repr`` excludes it, so a
    transport, log line, or exception that renders the request cannot leak
    the key as part of a query string.
    """

    endpoint_id: str
    params: Mapping[str, str]
    timeout_seconds: float
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class LiveAttemptOutcome:
    """Public closed outcome of one live attempt (contract §6/D1)."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in LIVE_ATTEMPT_CODES:
            raise ValueError("live_attempt_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class WtiQueryResult:
    """Common query result: typed fact plus, on live paths, a closed attempt
    outcome. Fixture transports keep ``attempt=None`` (contract §6)."""

    fact: WtiFact | UnavailableFact
    attempt: LiveAttemptOutcome | None


@dataclass(frozen=True)
class _LiveTransportSuccess:
    """Module-private union member (D1). ``body`` passed bounded validation
    inside the transport and never reaches a public dataclass or repr."""

    body: bytes = field(repr=False)
    status: int = 200


@dataclass(frozen=True)
class _LiveTransportFailure:
    """Module-private union member (D1): closed code only — no body, URL,
    query, headers, or exception."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in LIVE_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


Transport = Callable[[EiaRequest], HttpResponse]
LiveTransport = Callable[[EiaRequest], "_LiveTransportSuccess | _LiveTransportFailure"]
Clock = Callable[[], datetime]


def build_wti_request(*, api_key: str) -> EiaRequest:
    """Shared request builder: exact multimap identity, credential apart."""
    return EiaRequest(
        endpoint_id=ENDPOINT_ID,
        params=dict(WTI_QUERY_MULTIMAP),
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        api_key=api_key,
    )


def parse_wti_response(body: bytes, *, observed_at: datetime) -> WtiFact | UnavailableFact:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)

    try:
        rows = payload["response"]["data"]
    except (KeyError, TypeError):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not isinstance(rows, list):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not rows:
        return UnavailableFact(SOURCE, "no_published_data", observed_at)

    row = rows[0]
    if not isinstance(row, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if row.get("series") != SERIES or row.get("units") != UNITS:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if "period" not in row or "value" not in row:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    try:
        period = date.fromisoformat(row["period"])
        value = Decimal(str(row["value"]))
    except (ValueError, InvalidOperation, TypeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not value.is_finite():
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    return WtiFact(SOURCE, SERIES, period, value, UNITS, observed_at)


def query_wti(*, api_key: str | None, transport: Transport, clock: Clock) -> WtiQueryResult:
    """Fixture-path query: request creation, one transport call, parser.

    Keeps the pre-live contract intact: fixture transports return an
    ``HttpResponse`` and the result carries ``attempt=None``.
    """
    observed_at = clock()
    if not api_key:
        return WtiQueryResult(UnavailableFact(SOURCE, "config_missing", observed_at), None)

    request = build_wti_request(api_key=api_key)
    try:
        response = transport(request)
    except (TimeoutError, ConnectionError, OSError):
        return WtiQueryResult(UnavailableFact(SOURCE, "transport_error", observed_at), None)

    if response.status == 429:
        retry_after = _retry_after_seconds(response.headers)
        return WtiQueryResult(
            UnavailableFact(SOURCE, "rate_limited", observed_at, retry_after), None)
    if response.status != 200:
        return WtiQueryResult(UnavailableFact(SOURCE, "transport_error", observed_at), None)
    return WtiQueryResult(parse_wti_response(response.body, observed_at=observed_at), None)


def query_wti_live(*, api_key: str, transport: LiveTransport, clock: Clock) -> WtiQueryResult:
    """Live-path query consuming the module-private transport union (D1).

    Exactly one transport call; the raw body is consumed by the parser and
    its local reference dropped immediately. Retry/STOP decisions belong to
    the composition/controller, never here.
    """
    observed_at = clock()
    request = build_wti_request(api_key=api_key)
    try:
        outcome = transport(request)
    except Exception:
        # The live transport contract forbids raising; anything that still
        # escapes is a protocol violation and is sanitized here.
        outcome = None

    if isinstance(outcome, _LiveTransportSuccess):
        parsed = parse_wti_response(outcome.body, observed_at=observed_at)
        del outcome
        if isinstance(parsed, WtiFact):
            return WtiQueryResult(parsed, LiveAttemptOutcome(code="success"))
        return WtiQueryResult(parsed, LiveAttemptOutcome(code="response_contract_failure"))

    if isinstance(outcome, _LiveTransportFailure):
        reason = _FAILURE_REASONS[outcome.code]
        fact = UnavailableFact(SOURCE, reason, observed_at, outcome.retry_after)
        return WtiQueryResult(
            fact, LiveAttemptOutcome(code=outcome.code, retry_after=outcome.retry_after))

    return WtiQueryResult(
        UnavailableFact(SOURCE, "transport_error", observed_at),
        LiveAttemptOutcome(code="protocol_failure"),
    )


def get_wti(*, api_key: str | None, transport: Transport, clock: Clock) -> WtiFact | UnavailableFact:
    """Backward-compatible façade over the fixture-path query (contract §6)."""
    return query_wti(api_key=api_key, transport=transport, clock=clock).fact


def _retry_after_seconds(headers: Mapping[str, str]) -> int | None:
    raw = headers.get("Retry-After")
    if raw is None or not raw.isdigit():
        return None
    return int(raw)
