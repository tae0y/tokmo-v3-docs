"""Pure offline parsers for official-membership observations."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from hashlib import sha256
from importlib import resources
from zoneinfo import ZoneInfo

from tokmo.values.official_membership_observation import (
    ListingObservationV1,
    ListingVenueV1,
    ObservationSourceKindV1,
    OfficialSicSnapshotV1,
    SecIssuerObservationV1,
    SecRegistryObservationV1,
    SecurityClassV1,
)
from tokmo.values.session_gate import MARKET_TIMEZONE, Market

SECURITY_NAME_POLICY_VERSION = "official-security-name-v1"
NASDAQLISTED_HEADER = (
    "Symbol|Security Name|Market Category|Test Issue|Financial Status|"
    "Round Lot Size|ETF|NextShares"
)
OTHERLISTED_HEADER = (
    "ACT Symbol|Security Name|Exchange|CQS Symbol|ETF|Round Lot Size|"
    "Test Issue|NASDAQ Symbol"
)
_FOOTER_PREFIX = "File Creation Time: "
# The directory footer carries a bare wall clock with no zone.  The official
# field-definition page documents the clock form but not its zone; Nasdaq
# technical specifications place Nasdaq system time in U.S. Eastern, so the
# wall clock is resolved through the project's canonical US market zone rather
# than being stamped naive-as-UTC.  DST-ambiguous wall clocks resolve with
# ``fold=0``, i.e. the first (daylight) occurrence.
_DIRECTORY_TIMEZONE = ZoneInfo(MARKET_TIMEZONE[Market.US])
_KNOWN_NASDAQ_TOKENS = {"Q", "G", "S"}
# "M" is present in the captured official otherlisted body.  It is admitted as a
# known non-target venue token only; the supported-target policy below still
# accepts nothing but "N", so a cohort symbol listed on "M" fails closed.
_KNOWN_OTHERLISTED_TOKENS = {"N", "A", "P", "Z", "V", "M"}
_KNOWN_FLAG_TOKENS = {"N", "Y"}
_KNOWN_STATUS_TOKENS = {"N", "D", "E", "G", "H", "J", "K", "Q"}
_NASDAQ_LISTED_FIELD_COUNT = len(NASDAQLISTED_HEADER.split("|"))
_OTHERLISTED_FIELD_COUNT = len(OTHERLISTED_HEADER.split("|"))
_POSITIVE_FORMS = (
    "COMMON STOCK",
    "COMMON SHARE",
    "COMMON SHARES",
    "ORDINARY SHARE",
    "ORDINARY SHARES",
)
_DENY_FORMS = (
    "ADR",
    "ADS",
    "AMERICAN DEPOSITARY",
    "DEPOSITARY",
    "PREFERRED",
    "WARRANT",
    "RIGHT",
    "UNIT",
    "NOTE",
    "DEBENTURE",
    "BOND",
    "FUND",
    "ETF",
    "ETN",
    "TRUST",
    "ACQUISITION",
    "BLANK CHECK",
    "SPAC",
    "WHEN ISSUED",
)
_EXCHANGE_MAP = {"Nasdaq": "NASDAQ", "NASDAQ": "NASDAQ", "NYSE": "NYSE"}


def normalize_cik(value: object) -> str:
    if type(value) not in {int, str}:
        raise ValueError("cik")
    text = str(value)
    if not text or len(text) > 10 or re.fullmatch(r"[0-9]+", text) is None:
        raise ValueError("cik")
    return text.zfill(10)


def _decode(raw: bytes) -> str:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ValueError("decode") from error
    if "\r" in text:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    return text


def _footer_timestamp_digits(timestamp_text: str) -> str:
    """Exactly two official footer clock forms, normalized to MMDDYYYYHHMM.

    The captured official directories emit ``MMDDYYYYHH:MM``; the historical
    documented form is the bare twelve-digit ``MMDDYYYYHHMM``.  Nothing else is
    admitted: any other colon position or count, any non-ASCII-digit character,
    and any extra character all fail closed here rather than at ``strptime``.
    """
    if re.fullmatch(r"[0-9]{12}", timestamp_text) is not None:
        return timestamp_text
    if re.fullmatch(r"[0-9]{10}:[0-9]{2}", timestamp_text) is not None:
        return timestamp_text[:10] + timestamp_text[11:]
    raise ValueError("footer")


def _parse_footer(value: str, *, field_count: int) -> datetime:
    # The captured nasdaqlisted footer pads to its full header width and the
    # captured otherlisted footer carries exactly one delimiter fewer.  Admit
    # only those two observed shapes: degenerate padding is not an official
    # form, and any field after the first must be empty so no data can hide in
    # the footer row.
    fields = value.split("|")
    if not field_count - 1 <= len(fields) <= field_count or any(
        field != "" for field in fields[1:]
    ):
        raise ValueError("footer")
    first_field = fields[0]
    if not first_field.startswith(_FOOTER_PREFIX):
        raise ValueError("footer")
    digits = _footer_timestamp_digits(first_field.removeprefix(_FOOTER_PREFIX))
    try:
        parsed = datetime.strptime(digits, "%m%d%Y%H%M")
    except ValueError as error:
        raise ValueError("footer") from error
    return parsed.replace(tzinfo=_DIRECTORY_TIMEZONE).astimezone(UTC)


def _normalized_security_name(value: str) -> str:
    try:
        normalized = re.sub(
            r"[^A-Z0-9]+",
            " ",
            value.encode("ascii").decode("ascii").upper(),
        )
    except UnicodeEncodeError as error:
        raise ValueError("security_name") from error
    normalized = " ".join(normalized.split())
    return f" {normalized} "


def classify_security_name(
    value: str,
    *,
    policy_version: str = SECURITY_NAME_POLICY_VERSION,
) -> SecurityClassV1:
    if policy_version != SECURITY_NAME_POLICY_VERSION:
        raise ValueError("policy")
    normalized = _normalized_security_name(value)
    if any(f" {token} " in normalized for token in _DENY_FORMS):
        return SecurityClassV1.UNSUPPORTED
    if any(f" {token} " in normalized for token in _POSITIVE_FORMS):
        return SecurityClassV1.COMMON_EQUITY
    return SecurityClassV1.UNSUPPORTED


def _required_symbols(expected_symbols: tuple[str, ...]) -> tuple[str, ...]:
    if type(expected_symbols) is not tuple or not expected_symbols:
        raise ValueError("expected_symbols")
    if len(set(expected_symbols)) != len(expected_symbols):
        raise ValueError("expected_symbols")
    return tuple(sorted(expected_symbols))


def parse_nasdaqlisted(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_symbols: tuple[str, ...],
) -> tuple[ListingObservationV1, ...]:
    text = _decode(raw)
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != NASDAQLISTED_HEADER:
        raise ValueError("header")
    directory_creation_time = _parse_footer(
        lines[-1],
        field_count=_NASDAQ_LISTED_FIELD_COUNT,
    )
    expected = set(_required_symbols(expected_symbols))
    body_sha256 = sha256(raw).hexdigest()
    rows = {}
    for line in lines[1:-1]:
        fields = line.split("|")
        if len(fields) != 8:
            raise ValueError("row_fields")
        market_category, test_issue, financial_status, etf_token = (
            fields[2],
            fields[3],
            fields[4],
            fields[6],
        )
        if market_category not in _KNOWN_NASDAQ_TOKENS:
            raise ValueError("venue_token")
        if test_issue not in _KNOWN_FLAG_TOKENS:
            raise ValueError("test_issue_token")
        if etf_token not in _KNOWN_FLAG_TOKENS:
            raise ValueError("etf_token")
        if financial_status not in _KNOWN_STATUS_TOKENS:
            raise ValueError("financial_status_token")
        symbol = fields[0]
        if symbol not in expected:
            continue
        if symbol in rows:
            raise ValueError("duplicate")
        rows[symbol] = ListingObservationV1(
            symbol=symbol,
            source_kind=ObservationSourceKindV1.NASDAQ_LISTED,
            venue=ListingVenueV1.NASDAQ,
            venue_token=market_category,
            security_name_hash=sha256(fields[1].encode("utf-8")).hexdigest(),
            security_class=classify_security_name(fields[1]),
            observed_at=observed_at,
            directory_creation_time=directory_creation_time,
            body_sha256=body_sha256,
            row_sha256=sha256(line.encode("utf-8")).hexdigest(),
            source_schema_version="nasdaq-directory-v1",
            test_issue_token=test_issue,
            financial_status_token=financial_status,
            etf_token=etf_token,
        )
    return tuple(rows[symbol] for symbol in sorted(rows))


def parse_otherlisted(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_symbols: tuple[str, ...],
) -> tuple[ListingObservationV1, ...]:
    text = _decode(raw)
    lines = text.splitlines()
    if len(lines) < 3 or lines[0] != OTHERLISTED_HEADER:
        raise ValueError("header")
    directory_creation_time = _parse_footer(
        lines[-1],
        field_count=_OTHERLISTED_FIELD_COUNT,
    )
    expected = set(_required_symbols(expected_symbols))
    body_sha256 = sha256(raw).hexdigest()
    rows = {}
    for line in lines[1:-1]:
        fields = line.split("|")
        if len(fields) != 8:
            raise ValueError("row_fields")
        exchange_token, etf_token, test_issue = fields[2], fields[4], fields[6]
        if exchange_token not in _KNOWN_OTHERLISTED_TOKENS:
            raise ValueError("venue_token_unknown")
        if etf_token not in _KNOWN_FLAG_TOKENS:
            raise ValueError("etf_token")
        if test_issue not in _KNOWN_FLAG_TOKENS:
            raise ValueError("test_issue_token")
        symbol = fields[0]
        if symbol not in expected:
            continue
        if symbol in rows:
            raise ValueError("duplicate")
        if exchange_token != "N":
            raise ValueError("venue_token_unsupported")
        rows[symbol] = ListingObservationV1(
            symbol=symbol,
            source_kind=ObservationSourceKindV1.OTHER_LISTED,
            venue=ListingVenueV1.NYSE,
            venue_token=exchange_token,
            security_name_hash=sha256(fields[1].encode("utf-8")).hexdigest(),
            security_class=classify_security_name(fields[1]),
            observed_at=observed_at,
            directory_creation_time=directory_creation_time,
            body_sha256=body_sha256,
            row_sha256=sha256(line.encode("utf-8")).hexdigest(),
            source_schema_version="otherlisted-directory-v1",
            test_issue_token=test_issue,
            financial_status_token="NA",
            etf_token=etf_token,
        )
    return tuple(rows[symbol] for symbol in sorted(rows))


def sec_ticker_form(symbol: str) -> str:
    """Normalize a cohort symbol to SEC's share-class ticker form.

    SEC registry/submissions publish share-class separators as hyphens
    (``BRK-B``) while the cohort and nasdaqtrader directories use dots
    (``BRK.B``).  Applied at the SEC boundary only; cohort-identity and
    nasdaqtrader-directory symbols keep their dot form everywhere else.
    """
    if type(symbol) is not str:
        raise ValueError("symbols")
    return symbol.replace(".", "-")


def parse_sec_registry(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_symbols: tuple[str, ...],
) -> tuple[SecRegistryObservationV1, ...]:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("registry_json") from error
    if data.get("fields") != ["cik", "name", "ticker", "exchange"]:
        raise ValueError("registry_fields")
    expected = set(_required_symbols(expected_symbols))
    expected_sec = {sec_ticker_form(symbol): symbol for symbol in expected}
    if len(expected_sec) != len(expected):
        raise ValueError("expected_symbols_sec_conflict")
    body_sha256 = sha256(raw).hexdigest()
    rows = {}
    for row in data.get("data", []):
        if type(row) is not list or len(row) != 4:
            raise ValueError("registry_row")
        symbol = (
            expected_sec.get(sec_ticker_form(row[2])) if (type(row[2]) is str) else None
        )
        if symbol is None:
            continue
        if symbol in rows:
            raise ValueError("registry_duplicate")
        exchange = _EXCHANGE_MAP.get(row[3])
        if exchange is None:
            raise ValueError("registry_exchange")
        try:
            cik = normalize_cik(row[0])
        except ValueError:
            raise ValueError("registry_cik")
        rows[symbol] = SecRegistryObservationV1(
            symbol=symbol,
            cik=cik,
            exchange_token=exchange,
            name_hash=sha256(str(row[1]).encode("utf-8")).hexdigest(),
            observed_at=observed_at,
            body_sha256=body_sha256,
            row_sha256=sha256(
                json.dumps(row, separators=(",", ":"), ensure_ascii=True).encode(
                    "utf-8"
                )
            ).hexdigest(),
            source_schema_version="sec-registry-v1",
        )
    if set(rows) != expected:
        raise ValueError("registry_missing")
    return tuple(rows[symbol] for symbol in sorted(rows))


def parse_sec_issuer(
    raw: bytes,
    *,
    observed_at: datetime,
    expected_symbol: str,
    expected_cik: str,
    expected_exchange: str,
) -> SecIssuerObservationV1:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("issuer_json") from error
    try:
        body_cik = normalize_cik(data.get("cik"))
        normalized_expected_cik = normalize_cik(expected_cik)
    except ValueError as error:
        raise ValueError("issuer_cik") from error
    if body_cik != normalized_expected_cik:
        raise ValueError("issuer_cik")
    sic_code = data.get("sic")
    if re.fullmatch(r"[0-9]{4}", str(sic_code) if sic_code is not None else "") is None:
        raise ValueError("issuer_sic")
    tickers = data.get("tickers")
    exchanges = data.get("exchanges")
    if (
        type(tickers) is not list
        or type(exchanges) is not list
        or len(tickers) != len(exchanges)
    ):
        raise ValueError("issuer_pairs")
    expected_sec_ticker = sec_ticker_form(expected_symbol)
    target_pairs = []
    for symbol, exchange in zip(tickers, exchanges, strict=True):
        if type(symbol) is not str or sec_ticker_form(symbol) != expected_sec_ticker:
            continue
        canonical_exchange = _EXCHANGE_MAP.get(exchange)
        if canonical_exchange is None:
            raise ValueError("issuer_exchange")
        target_pairs.append((symbol, canonical_exchange))
    if len(target_pairs) != 1 or target_pairs[0][1] != expected_exchange:
        raise ValueError("issuer_exchange")
    return SecIssuerObservationV1(
        cik=normalized_expected_cik,
        symbol=expected_symbol,
        exchange_token=expected_exchange,
        sic_code=str(sic_code),
        observed_at=observed_at,
        body_sha256=sha256(raw).hexdigest(),
        source_schema_version="sec-submissions-v1",
    )


def parse_reconstructed_sic_snapshot(raw: bytes) -> OfficialSicSnapshotV1:
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as error:
        raise ValueError("sic_snapshot_json") from error
    return OfficialSicSnapshotV1(
        snapshot_version=data["snapshot_version"],
        classification=data["classification"],
        codes=tuple(data["codes"]),
        documentation_ref=data["documentation_ref"],
    )


def load_production_sic_snapshot() -> OfficialSicSnapshotV1:
    root = resources.files("tokmo.data")
    artifact_ref = root.joinpath("sec_sic_html_v1.json")
    manifest_ref = root.joinpath("sec_sic_html_v1.manifest.json")
    if not artifact_ref.is_file() or not manifest_ref.is_file():
        raise ValueError("production_sic_resource_missing")
    try:
        artifact_raw = artifact_ref.read_bytes()
        artifact = json.loads(artifact_raw)
        manifest = json.loads(manifest_ref.read_bytes())
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError("production_sic_resource_invalid") from error
    if (
        set(artifact) != {"schema_version", "codes"}
        or artifact["schema_version"] != "sec-sic-html-v1"
        or set(manifest)
        != {
            "artifact_sha256",
            "capture_instant",
            "classification",
            "extraction_contract_version",
            "official_source_sha256",
            "row_count",
            "source_row_count",
            "source",
        }
        or manifest["classification"] != "OFFICIAL_CAPTURE"
        or manifest["source"] != "SEC_SIC_DOCUMENTATION"
        or manifest["extraction_contract_version"] != "sec-sic-html-v1"
        or manifest["artifact_sha256"] != sha256(artifact_raw).hexdigest()
        or manifest["row_count"] != len(artifact["codes"])
        or manifest["source_row_count"] != manifest["row_count"]
    ):
        raise ValueError("production_sic_resource_invalid")
    return OfficialSicSnapshotV1(
        snapshot_version="sec-sic-html-v1",
        classification="OFFICIAL_CAPTURE",
        codes=tuple(artifact["codes"]),
        documentation_ref="sec-sic-official-capture-v1",
    )
