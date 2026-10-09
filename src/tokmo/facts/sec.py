"""SEC ticker-map and submissions parsers/queries (SEC Tracer 1a).

Transport and clock are mandatory injections. The declared User-Agent is
carried apart from request identity with ``repr=False`` and only the
source-private live transport puts it on the wire.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime

from tokmo.facts.eia import (
    LIVE_TRANSPORT_FAILURE_CODES,
    LiveAttemptOutcome,
)
from tokmo.values import SEC_SOURCE, SecFilingFact, UnavailableFact

SOURCE = SEC_SOURCE
TICKERS_ENDPOINT_ID = "sec_company_tickers"
SUBMISSIONS_ENDPOINT_ID = "sec_submissions"
REQUEST_TIMEOUT_SECONDS = 10.0
CIK_DIGITS = 10

# Required column arrays in filings.recent; optional ones may be absent but
# must stay aligned when present.
REQUIRED_COLUMNS = ("accessionNumber", "form", "filingDate", "primaryDocument")
OPTIONAL_COLUMNS = ("reportDate", "isXBRL")

# Same closed failure→reason mapping shape as the EIA live slice; kept
# source-local instead of shared to avoid a generic multi-source abstraction.
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
class SecRequest:
    """Request identity plus the separately-carried declared User-Agent."""

    endpoint_id: str
    cik: str | None
    timeout_seconds: float
    user_agent: str = field(repr=False)


@dataclass(frozen=True)
class SecTickerMapping:
    """Official ticker→CIK mapping row; not a stored fact."""

    ticker: str
    cik: str
    company_name: str


@dataclass(frozen=True)
class _SecTransportSuccess:
    """Source-private union member; the body never reaches a public repr."""

    body: bytes = field(repr=False)
    status: int = 200


@dataclass(frozen=True)
class _SecTransportFailure:
    """Source-private union member: closed code only, nothing else."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in LIVE_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class SecQueryResult:
    result: SecTickerMapping | SecFilingFact | UnavailableFact
    attempt: LiveAttemptOutcome | None


SecTransport = Callable[[SecRequest], "_SecTransportSuccess | _SecTransportFailure"]
Clock = Callable[[], datetime]


def build_tickers_request(*, user_agent: str) -> SecRequest:
    return SecRequest(
        endpoint_id=TICKERS_ENDPOINT_ID,
        cik=None,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        user_agent=user_agent,
    )


def build_submissions_request(*, cik: str | None, user_agent: str) -> SecRequest:
    return SecRequest(
        endpoint_id=SUBMISSIONS_ENDPOINT_ID,
        cik=cik,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        user_agent=user_agent,
    )


def parse_company_tickers(
    body: bytes, *, ticker: str, observed_at: datetime
) -> SecTickerMapping | UnavailableFact:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    for entry in payload.values():
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("ticker"), str)
            or not isinstance(entry.get("title"), str)
            or type(entry.get("cik_str")) is not int
        ):
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        if entry["ticker"] != ticker:
            continue
        cik_number = entry["cik_str"]
        if not 0 < cik_number < 10**CIK_DIGITS or not entry["title"]:
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        return SecTickerMapping(
            ticker=ticker,
            cik=f"{cik_number:0{CIK_DIGITS}d}",
            company_name=entry["title"],
        )
    return UnavailableFact(SOURCE, "no_published_data", observed_at)


def parse_submissions(
    body: bytes, *, ticker: str, cik: str, company_name: str, observed_at: datetime
) -> SecFilingFact | UnavailableFact:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)

    recent = _aligned_recent(payload, cik)
    if recent is None:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not recent["accessionNumber"]:
        return UnavailableFact(SOURCE, "no_published_data", observed_at)

    accession = recent["accessionNumber"][0]
    form = recent["form"][0]
    primary_document = recent["primaryDocument"][0]
    raw_filing_date = recent["filingDate"][0]
    raw_report_date = recent["reportDate"][0] if "reportDate" in recent else None
    raw_is_xbrl = recent["isXBRL"][0] if "isXBRL" in recent else None

    for value in (accession, form, primary_document, raw_filing_date):
        if not isinstance(value, str) or not value:
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if raw_is_xbrl is not None and raw_is_xbrl not in (0, 1):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if raw_report_date is not None and not isinstance(raw_report_date, str):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    try:
        filing_date = date.fromisoformat(raw_filing_date)
        report_date = date.fromisoformat(raw_report_date) if raw_report_date else None
    except ValueError:
        return UnavailableFact(SOURCE, "parse_error", observed_at)

    return SecFilingFact(
        source=SOURCE,
        ticker=ticker,
        cik=cik,
        company_name=company_name,
        accession_number=accession,
        form=form,
        filing_date=filing_date,
        report_date=report_date,
        primary_document=primary_document,
        is_xbrl=None if raw_is_xbrl is None else bool(raw_is_xbrl),
        observed_at=observed_at,
    )


def _aligned_recent(payload, cik: str) -> dict | None:
    """filings.recent with the response CIK cross-checked and every present
    column array validated for identical length (index alignment)."""
    if not isinstance(payload, dict):
        return None
    response_cik = payload.get("cik")
    if response_cik is not None:
        if type(response_cik) not in (str, int):
            return None
        try:
            if int(response_cik) != int(cik):
                return None
        except ValueError:
            return None

    filings = payload.get("filings")
    if not isinstance(filings, dict):
        return None
    recent = filings.get("recent")
    if not isinstance(recent, dict):
        return None
    columns = [name for name in REQUIRED_COLUMNS + OPTIONAL_COLUMNS if name in recent]
    if any(name not in recent for name in REQUIRED_COLUMNS):
        return None
    if not all(isinstance(recent[name], list) for name in columns):
        return None
    if len({len(recent[name]) for name in columns}) != 1:
        return None
    return recent


def query_company_tickers(
    *, ticker: str, user_agent: str, transport: SecTransport, clock: Clock
) -> SecQueryResult:
    request = build_tickers_request(user_agent=user_agent)
    return _query(
        request,
        transport,
        clock,
        lambda body, observed_at: parse_company_tickers(
            body, ticker=ticker, observed_at=observed_at
        ),
        success_type=SecTickerMapping,
    )


def query_submissions(
    *,
    ticker: str,
    cik: str,
    company_name: str,
    user_agent: str,
    transport: SecTransport,
    clock: Clock,
) -> SecQueryResult:
    request = build_submissions_request(cik=cik, user_agent=user_agent)
    return _query(
        request,
        transport,
        clock,
        lambda body, observed_at: parse_submissions(
            body,
            ticker=ticker,
            cik=cik,
            company_name=company_name,
            observed_at=observed_at,
        ),
        success_type=SecFilingFact,
    )


def _query(
    request: SecRequest,
    transport: SecTransport,
    clock: Clock,
    parse,
    *,
    success_type: type,
) -> SecQueryResult:
    """One transport call per invocation; escaping exceptions are sanitized."""
    observed_at = clock()
    try:
        outcome = transport(request)
    except Exception:
        outcome = None

    if isinstance(outcome, _SecTransportSuccess):
        parsed = parse(outcome.body, observed_at)
        del outcome
        if isinstance(parsed, success_type):
            return SecQueryResult(parsed, LiveAttemptOutcome(code="success"))
        return SecQueryResult(
            parsed, LiveAttemptOutcome(code="response_contract_failure")
        )

    if isinstance(outcome, _SecTransportFailure):
        reason = _FAILURE_REASONS[outcome.code]
        fact = UnavailableFact(SOURCE, reason, observed_at, outcome.retry_after)
        return SecQueryResult(
            fact, LiveAttemptOutcome(code=outcome.code, retry_after=outcome.retry_after)
        )

    return SecQueryResult(
        UnavailableFact(SOURCE, "transport_error", observed_at),
        LiveAttemptOutcome(code="protocol_failure"),
    )
