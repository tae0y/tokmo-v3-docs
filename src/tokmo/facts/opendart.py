"""OpenDART corpCode ZIP/XML and disclosure-list JSON parsers/queries
(OpenDART Tracer 1a).

Transport and clock are mandatory injections. The API credential is carried
apart from request identity with ``repr=False``; only the source-private
live transport puts it on the wire, and no raw body, URL, or provider
message text ever leaves this module.
"""

import io
import json
import stat
import zipfile
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from xml.etree import ElementTree

from tokmo.facts.eia import LIVE_TRANSPORT_FAILURE_CODES
from tokmo.values import OPENDART_SOURCE, OpenDartDisclosureFact, UnavailableFact

SOURCE = OPENDART_SOURCE
CORP_CODE_ENDPOINT_ID = "opendart_corp_code"
LIST_ENDPOINT_ID = "opendart_list"
REQUEST_TIMEOUT_SECONDS = 10.0

STOCK_CODE_DIGITS = 6
CORP_CODE_DIGITS = 8
RECEIPT_NUMBER_DIGITS = 14
DATE_DIGITS = 8

# corpCode payload guard: the compressed body is bounded by the transport;
# these bound what the archive may expand to, all in memory. Proportional
# to the one official archive (a single XML entry), not a generic framework.
ZIP_MAX_ENTRIES = 4
ZIP_MAX_UNCOMPRESSED_BYTES = 64 * 1024 * 1024
ZIP_MAX_ERROR_ENVELOPE_BYTES = 64 * 1024
CORP_CODE_XML_NAME = "CORPCODE.xml"

# Official corp_cls classes from the development guide (KOSPI, KOSDAQ,
# KONEX, other).
CORP_CLASSES = frozenset({"Y", "K", "N", "E"})

# Official response status codes: 000 normal; 013 no matching data;
# 010/011/012/101 key or access rejection; 020/021 usage limit exceeded.
# Any other status is a contract mismatch, never a guessed semantic.
STATUS_SUCCESS = "000"
STATUS_NO_DATA = frozenset({"013"})
STATUS_KEY_REJECTED = frozenset({"010", "011", "012", "101"})
STATUS_RATE_LIMITED = frozenset({"020", "021"})

# Provider status classes ride next to the shared transport failure codes so
# the campaign controller can distinguish auth/rate stops from mismatches.
ATTEMPT_CODES = LIVE_TRANSPORT_FAILURE_CODES | {
    "success", "response_contract_failure",
    "provider_no_data", "provider_key_rejected", "provider_rate_limited",
}

_PROVIDER_REASONS = {
    "provider_no_data": "no_published_data",
    "provider_key_rejected": "contract_mismatch",
    "provider_rate_limited": "rate_limited",
}

# Same closed failure→reason mapping shape as the other live slices; kept
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
class OpenDartRequest:
    """Request identity plus the separately-carried API credential."""

    endpoint_id: str
    corp_code: str | None
    begin_date: str | None
    end_date: str | None
    timeout_seconds: float
    api_key: str = field(repr=False)


@dataclass(frozen=True)
class OpenDartCorpMapping:
    """Official stock_code→corp_code identity row; not a stored fact."""

    stock_code: str
    corp_code: str
    corp_name: str
    modify_date: date


@dataclass(frozen=True)
class OpenDartAttemptOutcome:
    """Closed outcome of one live attempt, including provider status classes."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in ATTEMPT_CODES:
            raise ValueError("opendart_attempt_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class _ProviderStatusFailure:
    """Module-private classification of a non-success official status."""

    code: str

    def __post_init__(self) -> None:
        if self.code not in _PROVIDER_REASONS:
            raise ValueError("provider_status_code_outside_contract")


@dataclass(frozen=True)
class _OpenDartTransportSuccess:
    """Source-private union member; the body never reaches a public repr."""

    body: bytes = field(repr=False)
    status: int = 200


@dataclass(frozen=True)
class _OpenDartTransportFailure:
    """Source-private union member: closed code only, nothing else."""

    code: str
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.code not in LIVE_TRANSPORT_FAILURE_CODES:
            raise ValueError("live_transport_code_outside_contract")
        if self.retry_after is not None and self.code != "http_429":
            raise ValueError("retry_after_requires_http_429")


@dataclass(frozen=True)
class OpenDartQueryResult:
    result: "OpenDartCorpMapping | OpenDartDisclosureFact | UnavailableFact"
    attempt: OpenDartAttemptOutcome | None


OpenDartTransport = Callable[
    [OpenDartRequest], "_OpenDartTransportSuccess | _OpenDartTransportFailure"]
Clock = Callable[[], datetime]


def build_corp_code_request(*, api_key: str) -> OpenDartRequest:
    return OpenDartRequest(
        endpoint_id=CORP_CODE_ENDPOINT_ID, corp_code=None,
        begin_date=None, end_date=None,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS, api_key=api_key,
    )


def build_list_request(*, corp_code: str | None, begin_date: str | None,
                       end_date: str | None, api_key: str) -> OpenDartRequest:
    return OpenDartRequest(
        endpoint_id=LIST_ENDPOINT_ID, corp_code=corp_code,
        begin_date=begin_date, end_date=end_date,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS, api_key=api_key,
    )


def parse_corp_code_zip(body: bytes, *, stock_code: str, observed_at: datetime,
                        ) -> "OpenDartCorpMapping | UnavailableFact | _ProviderStatusFailure":
    buffer = io.BytesIO(body)
    if not zipfile.is_zipfile(buffer):
        return _classify_error_envelope(body, observed_at)
    try:
        with zipfile.ZipFile(buffer) as archive:
            xml_bytes = _bounded_xml_entry(archive)
    except (zipfile.BadZipFile, OSError, RuntimeError, ValueError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if xml_bytes is None:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    return _mapping_from_xml(xml_bytes, stock_code, observed_at)


def parse_disclosure_list(body: bytes, *, corp_code: str, stock_code: str,
                          observed_at: datetime,
                          ) -> "OpenDartDisclosureFact | UnavailableFact | _ProviderStatusFailure":
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    if not isinstance(payload, dict) or not isinstance(payload.get("status"), str):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if payload["status"] != STATUS_SUCCESS:
        return _classify_status(payload["status"], observed_at)
    if not _page_is_first(payload.get("page_no")):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    rows = payload.get("list")
    if not isinstance(rows, list):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not rows:
        return UnavailableFact(SOURCE, "no_published_data", observed_at)
    row = rows[0]
    if not isinstance(row, dict):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)

    text: dict[str, str] = {}
    for name in ("corp_code", "corp_name", "stock_code", "corp_cls",
                 "report_nm", "rcept_no", "flr_nm", "rcept_dt"):
        value = row.get(name)
        if not isinstance(value, str) or not value.strip():
            return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
        text[name] = value.strip()

    # Identity cross-check: the row must be the requested listed company, and
    # list[0] is "latest" only because the request pinned sort=date desc on
    # the first page (enforced by the transport's fixed query pairs).
    if (text["corp_code"], text["stock_code"]) != (corp_code, stock_code):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if text["corp_cls"] not in CORP_CLASSES:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    if not _exact_digits(text["rcept_no"], RECEIPT_NUMBER_DIGITS):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    receipt_date = _yyyymmdd(text["rcept_dt"])
    if receipt_date is None:
        return UnavailableFact(SOURCE, "parse_error", observed_at)

    raw_remark = row.get("rm")
    if raw_remark is not None and not isinstance(raw_remark, str):
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    remark = raw_remark.strip() if isinstance(raw_remark, str) else None

    return OpenDartDisclosureFact(
        source=SOURCE, stock_code=stock_code, corp_code=corp_code,
        corp_name=text["corp_name"], corp_cls=text["corp_cls"],
        report_name=text["report_nm"], receipt_number=text["rcept_no"],
        filer_name=text["flr_nm"], receipt_date=receipt_date,
        remark=remark or None, observed_at=observed_at,
    )


def _bounded_xml_entry(archive: zipfile.ZipFile) -> bytes | None:
    """The single expected XML entry, or None on any structural violation."""
    entries = archive.infolist()
    if not entries or len(entries) > ZIP_MAX_ENTRIES:
        return None
    total_declared = 0
    for entry in entries:
        name = entry.filename
        file_type = stat.S_IFMT(entry.external_attr >> 16)
        if (name.startswith("/") or "\\" in name or name.endswith("/")
                or ".." in name.split("/")
                or (file_type and file_type != stat.S_IFREG)):
            return None
        total_declared += entry.file_size
    if total_declared > ZIP_MAX_UNCOMPRESSED_BYTES:
        return None
    named = [entry for entry in entries if entry.filename == CORP_CODE_XML_NAME]
    if len(named) != 1:
        return None
    with archive.open(named[0]) as handle:
        xml_bytes = handle.read(ZIP_MAX_UNCOMPRESSED_BYTES + 1)
    if len(xml_bytes) > ZIP_MAX_UNCOMPRESSED_BYTES:
        return None
    return xml_bytes


def _mapping_from_xml(xml_bytes: bytes, stock_code: str, observed_at: datetime,
                      ) -> "OpenDartCorpMapping | UnavailableFact":
    """In-memory stdlib parse; ElementTree performs no external entity or
    network expansion. Only the selected row is materialized."""
    try:
        for _event, element in ElementTree.iterparse(
                io.BytesIO(xml_bytes), events=("end",)):
            if element.tag != "list":
                continue
            row_stock = _stripped_text(element, "stock_code")
            if row_stock != stock_code:
                element.clear()
                continue
            corp_code = _stripped_text(element, "corp_code")
            corp_name = _stripped_text(element, "corp_name")
            modify_raw = _stripped_text(element, "modify_date")
            if (not _exact_digits(row_stock, STOCK_CODE_DIGITS)
                    or not _exact_digits(corp_code, CORP_CODE_DIGITS)
                    or not corp_name):
                return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
            modify_date = _yyyymmdd(modify_raw)
            if modify_date is None:
                return UnavailableFact(SOURCE, "parse_error", observed_at)
            return OpenDartCorpMapping(
                stock_code=row_stock, corp_code=corp_code,
                corp_name=corp_name, modify_date=modify_date)
    except ElementTree.ParseError:
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    return UnavailableFact(SOURCE, "no_published_data", observed_at)


def _classify_error_envelope(body: bytes, observed_at: datetime,
                             ) -> "UnavailableFact | _ProviderStatusFailure":
    """A non-ZIP corpCode body is acceptable only as the official XML error
    envelope; its status is classified and its message never surfaced."""
    if len(body) > ZIP_MAX_ERROR_ENVELOPE_BYTES:
        return UnavailableFact(SOURCE, "contract_mismatch", observed_at)
    try:
        root = ElementTree.fromstring(body)
    except ElementTree.ParseError:
        return UnavailableFact(SOURCE, "parse_error", observed_at)
    status = root.findtext("status")
    return _classify_status(status.strip() if isinstance(status, str) else "",
                            observed_at)


def _classify_status(status: str, observed_at: datetime,
                     ) -> "UnavailableFact | _ProviderStatusFailure":
    if status in STATUS_NO_DATA:
        return _ProviderStatusFailure("provider_no_data")
    if status in STATUS_KEY_REJECTED:
        return _ProviderStatusFailure("provider_key_rejected")
    if status in STATUS_RATE_LIMITED:
        return _ProviderStatusFailure("provider_rate_limited")
    return UnavailableFact(SOURCE, "contract_mismatch", observed_at)


def _page_is_first(value) -> bool:
    """The response must confirm the requested first page (int or digit str)."""
    if type(value) is int:
        return value == 1
    return isinstance(value, str) and value.strip() == "1"


def _stripped_text(element, tag: str) -> str:
    text = element.findtext(tag)
    return text.strip() if isinstance(text, str) else ""


def _exact_digits(value, count: int) -> bool:
    return (isinstance(value, str) and len(value) == count
            and value.isascii() and value.isdigit())


def _yyyymmdd(raw: str) -> date | None:
    """Deterministic normalization of an 8-digit YYYYMMDD date."""
    if not _exact_digits(raw, DATE_DIGITS):
        return None
    try:
        return date(int(raw[:4]), int(raw[4:6]), int(raw[6:8]))
    except ValueError:
        return None


def query_corp_code(*, stock_code: str, api_key: str,
                    transport: OpenDartTransport, clock: Clock) -> OpenDartQueryResult:
    request = build_corp_code_request(api_key=api_key)
    return _query(request, transport, clock,
                  lambda body, observed_at: parse_corp_code_zip(
                      body, stock_code=stock_code, observed_at=observed_at),
                  success_type=OpenDartCorpMapping)


def query_disclosure_list(*, corp_code: str, stock_code: str, begin_date: str,
                          end_date: str, api_key: str,
                          transport: OpenDartTransport, clock: Clock) -> OpenDartQueryResult:
    request = build_list_request(corp_code=corp_code, begin_date=begin_date,
                                 end_date=end_date, api_key=api_key)
    return _query(request, transport, clock,
                  lambda body, observed_at: parse_disclosure_list(
                      body, corp_code=corp_code, stock_code=stock_code,
                      observed_at=observed_at),
                  success_type=OpenDartDisclosureFact)


def _query(request: OpenDartRequest, transport: OpenDartTransport, clock: Clock,
           parse, *, success_type: type) -> OpenDartQueryResult:
    """One transport call per invocation; escaping exceptions are sanitized."""
    observed_at = clock()
    try:
        outcome = transport(request)
    except Exception:
        outcome = None

    if isinstance(outcome, _OpenDartTransportSuccess):
        parsed = parse(outcome.body, observed_at)
        del outcome
        if isinstance(parsed, success_type):
            return OpenDartQueryResult(parsed, OpenDartAttemptOutcome(code="success"))
        if isinstance(parsed, _ProviderStatusFailure):
            fact = UnavailableFact(SOURCE, _PROVIDER_REASONS[parsed.code], observed_at)
            return OpenDartQueryResult(fact, OpenDartAttemptOutcome(code=parsed.code))
        return OpenDartQueryResult(
            parsed, OpenDartAttemptOutcome(code="response_contract_failure"))

    if isinstance(outcome, _OpenDartTransportFailure):
        reason = _FAILURE_REASONS[outcome.code]
        fact = UnavailableFact(SOURCE, reason, observed_at, outcome.retry_after)
        return OpenDartQueryResult(
            fact, OpenDartAttemptOutcome(code=outcome.code,
                                         retry_after=outcome.retry_after))

    return OpenDartQueryResult(
        UnavailableFact(SOURCE, "transport_error", observed_at),
        OpenDartAttemptOutcome(code="protocol_failure"),
    )
