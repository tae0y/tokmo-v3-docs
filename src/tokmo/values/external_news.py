"""Immutable values for the N1 direct-URL NEWS storage seam."""

import ipaddress
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from urllib.parse import urlsplit

_HEX64 = re.compile(r"[0-9a-f]{64}\Z")


class SaveOutcome(StrEnum):
    INSERTED = "INSERTED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"


class ContentMode(StrEnum):
    NEWS = "NEWS"
    ANALYSIS = "ANALYSIS"
    OPINION = "OPINION"
    INTERVIEW = "INTERVIEW"
    PRESS_RELEASE = "PRESS_RELEASE"
    RUMOR = "RUMOR"


class ContentState(StrEnum):
    METADATA_ONLY = "METADATA_ONLY"
    BODY_AVAILABLE = "BODY_AVAILABLE"
    BODY_EXPIRED = "BODY_EXPIRED"
    PAYWALLED = "PAYWALLED"
    LICENSE_RESTRICTED = "LICENSE_RESTRICTED"
    REDACTED = "REDACTED"


class LicenseState(StrEnum):
    PERMITTED = "PERMITTED"
    UNKNOWN = "UNKNOWN"
    RESTRICTED = "RESTRICTED"


class EvidenceRole(StrEnum):
    FACT_DISCOVERY = "FACT_DISCOVERY"
    OPINION_DISCOVERY = "OPINION_DISCOVERY"


class TimeInterpretation(StrEnum):
    SOURCE_NATIVE = "SOURCE_NATIVE"
    EXPLICIT_ZONE = "EXPLICIT_ZONE"
    ASSUMED_KST = "ASSUMED_KST"


def canonical_utc(value: datetime, name: str) -> str:
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError(f"{name} must be UTC-aware")
    return value.astimezone(UTC).isoformat(timespec="microseconds")


def _nonempty(value: str, name: str) -> None:
    if type(value) is not str or not value.strip():
        raise ValueError(f"{name} must be non-empty")


@dataclass(frozen=True)
class NewsWrite:
    canonical_url: str
    title: str
    observed_at: datetime
    source_article_id: str | None = None
    publisher_name: str | None = field(default=None, repr=False)
    publisher_site_url: str | None = field(default=None, repr=False)
    summary_text: str | None = field(default=None, repr=False)
    body_text: str | None = field(default=None, repr=False)
    content_hash: str | None = None
    content_mode: ContentMode = ContentMode.NEWS
    content_state: ContentState = ContentState.METADATA_ONLY
    license_state: LicenseState = LicenseState.UNKNOWN
    published_at: datetime | None = None
    evidence_role: EvidenceRole = EvidenceRole.FACT_DISCOVERY
    alert_eligible: bool = True
    time_interpretation: TimeInterpretation = TimeInterpretation.SOURCE_NATIVE

    def __post_init__(self) -> None:
        _nonempty(self.canonical_url, "canonical_url")
        parsed = urlsplit(self.canonical_url)
        try:
            port = parsed.port
            ascii_host = (parsed.hostname or "").encode("idna").decode("ascii")
        except (UnicodeError, ValueError) as error:
            raise ValueError("canonical_url has an invalid host or port") from error
        if (
            parsed.scheme.lower() not in {"http", "https"}
            or not ascii_host
            or parsed.username is not None
            or parsed.password is not None
            or ascii_host.endswith(".")
        ):
            raise ValueError("canonical_url must be credential-free absolute HTTP(S)")
        del port  # Access above is validation; canonicalization handles default ports.
        try:
            ipaddress.ip_address(ascii_host)
        except ValueError:
            pass
        else:
            raise ValueError("canonical_url must use a registrable domain")
        if any(
            not label
            or label.startswith("-")
            or label.endswith("-")
            or re.fullmatch(r"[a-zA-Z0-9-]+", label) is None
            for label in ascii_host.split(".")
        ):
            raise ValueError("canonical_url has an invalid host")
        if not all(
            isinstance(value, expected)
            for value, expected in (
                (self.content_mode, ContentMode),
                (self.content_state, ContentState),
                (self.license_state, LicenseState),
                (self.evidence_role, EvidenceRole),
                (self.time_interpretation, TimeInterpretation),
            )
        ):
            raise ValueError("content enum fields must use their typed enum values")
        if type(self.alert_eligible) is not bool:
            raise ValueError("alert_eligible must be bool")
        _nonempty(self.title, "title")
        canonical_utc(self.observed_at, "observed_at")
        if self.published_at is not None:
            canonical_utc(self.published_at, "published_at")
        for name in (
            "source_article_id",
            "publisher_name",
            "publisher_site_url",
            "summary_text",
            "body_text",
        ):
            value = getattr(self, name)
            if value is not None:
                _nonempty(value, name)
        if self.publisher_site_url is not None:
            publisher = urlsplit(self.publisher_site_url)
            try:
                publisher_port = publisher.port
                publisher_host = (
                    (publisher.hostname or "").encode("idna").decode("ascii")
                )
            except (UnicodeError, ValueError) as error:
                raise ValueError(
                    "publisher_site_url has an invalid host or port"
                ) from error
            if (
                publisher.scheme != "https"
                or not publisher_host
                or publisher.username is not None
                or publisher.password is not None
                or publisher_host.endswith(".")
            ):
                raise ValueError(
                    "publisher_site_url must be credential-free absolute HTTPS"
                )
            del publisher_port
            try:
                ipaddress.ip_address(publisher_host)
            except ValueError:
                pass
            else:
                raise ValueError("publisher_site_url must use a registrable domain")
            if any(
                not label
                or label.startswith("-")
                or label.endswith("-")
                or re.fullmatch(r"[a-zA-Z0-9-]+", label) is None
                for label in publisher_host.split(".")
            ):
                raise ValueError("publisher_site_url has an invalid host")
        if self.content_hash is not None and not _HEX64.fullmatch(self.content_hash):
            raise ValueError("content_hash must be lowercase sha256")
        if self.content_state is ContentState.BODY_AVAILABLE:
            if (
                self.body_text is None
                or self.license_state is not LicenseState.PERMITTED
            ):
                raise ValueError("available body requires text and permitted license")
        elif self.body_text is not None:
            raise ValueError("body_text requires BODY_AVAILABLE")


@dataclass(frozen=True)
class NewsReceipt:
    outcome: SaveOutcome
    news_document_id: str
    intake_request_id: str | None
    intake_receipt_id: str | None
    conflict_audit_id: str | None = None
    revision_evidence_id: str | None = None
