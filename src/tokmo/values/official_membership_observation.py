"""Reusable official-membership observation values and canonical digests."""

from __future__ import annotations

from dataclasses import dataclass, fields
from datetime import date, datetime, timezone
from enum import Enum
from hashlib import sha256
from re import fullmatch

from tokmo.values.material_pack import canonical_encode

_HASH_RE = r"[0-9a-f]{64}"
_ID_RE = r"[A-Za-z0-9._:-]{1,128}"
_PATH_RE = r"/[A-Za-z0-9._~/{}/-]{1,255}"
_VERSION_RE = r"[A-Za-z0-9._:-]{1,64}"
_SYMBOL_RE = r"[A-Z0-9._-]{1,32}"
_HOST_RE = r"[A-Za-z0-9.-]{1,128}"
_CIK_RE = r"[0-9]{10}"
_TOKEN_RE = r"[A-Za-z0-9]{1,16}"
_DETAIL_RE = r"[a-z0-9_:-]{1,64}"
_CLASSIFICATION_RE = r"(OFFICIAL_CAPTURE|RECONSTRUCTED_CONTRACT)"
_REF_PREFIX = "omsrcv1:sha256:"
_MATERIAL_REF_PREFIX = "ommatv1:sha256:"
_SIC_REF_PREFIX = "omsicv1:sha256:"


def _ascii_match(value: str, pattern: str, code: str) -> None:
    if type(value) is not str or fullmatch(pattern, value) is None:
        raise ValueError(code)


def _aware(value: datetime, code: str) -> None:
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(code)


def _plain(value):
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _plain(getattr(value, field.name))
            for field in fields(value)
            if field.name not in {"source_ref", "payload_sha256"}
        }
    return value


def _digest(prefix: str, value) -> str:
    return prefix + sha256(canonical_encode(_plain(value))).hexdigest()


class ListingVenueV1(str, Enum):
    NASDAQ = "NASDAQ"
    NYSE = "NYSE"


class SecurityClassV1(str, Enum):
    COMMON_EQUITY = "COMMON_EQUITY"
    UNSUPPORTED = "UNSUPPORTED"


class MembershipSectorV1(str, Enum):
    FINANCIALS = "FINANCIALS"
    NON_FINANCIALS = "NON_FINANCIALS"


class MembershipPolicyV1(str, Enum):
    OBSERVED_CURRENT_LISTING_V1 = "OBSERVED_CURRENT_LISTING_V1"


class ObservationSourceKindV1(str, Enum):
    NASDAQ_LISTED = "NASDAQ_LISTED"
    OTHER_LISTED = "OTHER_LISTED"
    SEC_REGISTRY = "SEC_REGISTRY"
    SEC_ISSUER = "SEC_ISSUER"


class MembershipUnavailableReasonV1(str, Enum):
    ABSENT_LISTING = "ABSENT_LISTING"
    STALE_OBSERVATION = "STALE_OBSERVATION"
    FUTURE_OBSERVATION = "FUTURE_OBSERVATION"
    UNSUPPORTED_VENUE = "UNSUPPORTED_VENUE"
    UNSUPPORTED_TYPE = "UNSUPPORTED_TYPE"
    UNSUPPORTED_STATUS = "UNSUPPORTED_STATUS"
    IDENTITY_CONFLICT = "IDENTITY_CONFLICT"
    MALFORMED_SIC = "MALFORMED_SIC"
    MISSING_SIC = "MISSING_SIC"
    SOURCE_SKEW = "SOURCE_SKEW"
    PARTIAL_ACQUISITION = "PARTIAL_ACQUISITION"
    COHORT_MISMATCH = "COHORT_MISMATCH"
    SUPPORTING_REGISTRY_ABSENT = "SUPPORTING_REGISTRY_ABSENT"
    REGISTRY_CONTRADICTION = "REGISTRY_CONTRADICTION"


@dataclass(frozen=True, slots=True)
class RequestIdentityV1:
    method: str
    host: str
    path_template: str
    source_schema_version: str

    def __post_init__(self) -> None:
        if self.method != "GET":
            raise ValueError("request_method")
        _ascii_match(self.host, _HOST_RE, "request_host")
        _ascii_match(self.path_template, _PATH_RE, "request_path")
        _ascii_match(
            self.source_schema_version,
            _VERSION_RE,
            "request_source_schema_version",
        )


@dataclass(frozen=True, slots=True)
class ObservationInstantV1:
    source_kind: ObservationSourceKindV1
    observed_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, ObservationSourceKindV1):
            raise ValueError("instant_source_kind")
        _aware(self.observed_at, "instant_observed_at")


@dataclass(frozen=True, slots=True)
class ObservedSourcePayloadV1:
    acquisition_id: str
    source_kind: ObservationSourceKindV1
    request_identity: RequestIdentityV1
    subject_key: str
    started_count: int
    observed_at: datetime
    payload_bytes: bytes
    payload_sha256: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.acquisition_id, _ID_RE, "payload_acquisition_id")
        if not isinstance(self.source_kind, ObservationSourceKindV1):
            raise ValueError("payload_source_kind")
        if not isinstance(self.request_identity, RequestIdentityV1):
            raise ValueError("payload_request_identity")
        _ascii_match(self.subject_key, _ID_RE, "payload_subject_key")
        if type(self.started_count) is not int or self.started_count < 1:
            raise ValueError("payload_started_count")
        _aware(self.observed_at, "payload_observed_at")
        if type(self.payload_bytes) is not bytes or not self.payload_bytes:
            raise ValueError("payload_bytes")
        object.__setattr__(
            self,
            "payload_sha256",
            sha256(self.payload_bytes).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class ListingObservationV1:
    symbol: str
    source_kind: ObservationSourceKindV1
    venue: ListingVenueV1
    venue_token: str
    security_name_hash: str
    security_class: SecurityClassV1
    observed_at: datetime
    directory_creation_time: datetime
    body_sha256: str
    row_sha256: str
    source_schema_version: str
    test_issue_token: str
    financial_status_token: str
    etf_token: str
    source_ref: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.symbol, _SYMBOL_RE, "listing_symbol")
        if self.source_kind not in {
            ObservationSourceKindV1.NASDAQ_LISTED,
            ObservationSourceKindV1.OTHER_LISTED,
        }:
            raise ValueError("listing_source_kind")
        if not isinstance(self.venue, ListingVenueV1):
            raise ValueError("listing_venue")
        _ascii_match(self.venue_token, _TOKEN_RE, "listing_venue_token")
        _ascii_match(
            self.security_name_hash,
            _HASH_RE,
            "listing_security_name_hash",
        )
        if not isinstance(self.security_class, SecurityClassV1):
            raise ValueError("listing_security_class")
        _aware(self.observed_at, "listing_observed_at")
        _aware(self.directory_creation_time, "listing_directory_creation_time")
        _ascii_match(self.body_sha256, _HASH_RE, "listing_body_sha256")
        _ascii_match(self.row_sha256, _HASH_RE, "listing_row_sha256")
        _ascii_match(
            self.source_schema_version,
            _VERSION_RE,
            "listing_source_schema_version",
        )
        for token, code in (
            (self.test_issue_token, "listing_test_issue_token"),
            (self.financial_status_token, "listing_financial_status_token"),
            (self.etf_token, "listing_etf_token"),
        ):
            _ascii_match(token, _TOKEN_RE, code)
        object.__setattr__(self, "source_ref", _digest(_REF_PREFIX, self))


@dataclass(frozen=True, slots=True)
class SecRegistryObservationV1:
    symbol: str
    cik: str
    exchange_token: str
    name_hash: str
    observed_at: datetime
    body_sha256: str
    row_sha256: str
    source_schema_version: str
    source_ref: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.symbol, _SYMBOL_RE, "registry_symbol")
        _ascii_match(self.cik, _CIK_RE, "registry_cik")
        _ascii_match(self.exchange_token, r"[A-Za-z]{4,16}", "registry_exchange")
        _ascii_match(self.name_hash, _HASH_RE, "registry_name_hash")
        _aware(self.observed_at, "registry_observed_at")
        _ascii_match(self.body_sha256, _HASH_RE, "registry_body_sha256")
        _ascii_match(self.row_sha256, _HASH_RE, "registry_row_sha256")
        _ascii_match(
            self.source_schema_version,
            _VERSION_RE,
            "registry_source_schema_version",
        )
        object.__setattr__(self, "source_ref", _digest(_REF_PREFIX, self))


@dataclass(frozen=True, slots=True)
class SecIssuerObservationV1:
    cik: str
    symbol: str
    exchange_token: str
    sic_code: str
    observed_at: datetime
    body_sha256: str
    source_schema_version: str
    source_ref: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.cik, _CIK_RE, "issuer_cik")
        _ascii_match(self.symbol, _SYMBOL_RE, "issuer_symbol")
        _ascii_match(self.exchange_token, r"[A-Z]{4,16}", "issuer_exchange")
        _ascii_match(self.sic_code, r"[0-9]{4}", "issuer_sic_code")
        _aware(self.observed_at, "issuer_observed_at")
        _ascii_match(self.body_sha256, _HASH_RE, "issuer_body_sha256")
        _ascii_match(
            self.source_schema_version,
            _VERSION_RE,
            "issuer_source_schema_version",
        )
        object.__setattr__(self, "source_ref", _digest(_REF_PREFIX, self))


@dataclass(frozen=True, slots=True)
class OfficialSicSnapshotV1:
    snapshot_version: str
    classification: str
    codes: tuple[str, ...]
    documentation_ref: str
    source_ref: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.snapshot_version, _VERSION_RE, "sic_snapshot_version")
        _ascii_match(
            self.classification,
            _CLASSIFICATION_RE,
            "sic_snapshot_classification",
        )
        _ascii_match(self.documentation_ref, _ID_RE, "sic_snapshot_documentation_ref")
        if type(self.codes) is not tuple or not self.codes:
            raise ValueError("sic_snapshot_codes")
        normalized = tuple(sorted(set(self.codes)))
        if len(normalized) != len(self.codes):
            raise ValueError("sic_snapshot_codes")
        if any(fullmatch(r"[0-9]{4}", code) is None for code in normalized):
            raise ValueError("sic_snapshot_codes")
        object.__setattr__(self, "codes", normalized)
        object.__setattr__(self, "source_ref", _digest(_SIC_REF_PREFIX, self))


@dataclass(frozen=True, slots=True)
class OfficialMembershipAcquisitionV1:
    acquisition_id: str
    membership_policy: MembershipPolicyV1
    security_policy_version: str
    source_contract_version: str
    sic_snapshot: OfficialSicSnapshotV1
    listings: tuple[ListingObservationV1, ...]
    registry_rows: tuple[SecRegistryObservationV1, ...]
    issuer_rows: tuple[SecIssuerObservationV1, ...]

    def __post_init__(self) -> None:
        _ascii_match(self.acquisition_id, _ID_RE, "acquisition_id")
        if not isinstance(self.membership_policy, MembershipPolicyV1):
            raise ValueError("membership_policy")
        _ascii_match(
            self.security_policy_version,
            _VERSION_RE,
            "security_policy_version",
        )
        _ascii_match(
            self.source_contract_version,
            _VERSION_RE,
            "source_contract_version",
        )
        if not isinstance(self.sic_snapshot, OfficialSicSnapshotV1):
            raise ValueError("sic_snapshot")
        for value, expected, code in (
            (self.listings, ListingObservationV1, "listings"),
            (self.registry_rows, SecRegistryObservationV1, "registry_rows"),
            (self.issuer_rows, SecIssuerObservationV1, "issuer_rows"),
        ):
            if type(value) is not tuple or not all(isinstance(item, expected) for item in value):
                raise ValueError(code)


@dataclass(frozen=True, slots=True)
class MembershipMaterialV1:
    symbol: str
    cik: str
    venue: ListingVenueV1
    sector: MembershipSectorV1
    listed_at_as_of: bool
    membership_as_of: date
    acquisition_id: str
    membership_policy: MembershipPolicyV1
    membership_holdings_digest: str
    security_policy_version: str
    sic_snapshot_version: str
    sic_code: str
    observation_instants: tuple[ObservationInstantV1, ...]
    listing_source_ref: str
    registry_source_ref: str
    issuer_source_ref: str
    sic_snapshot_ref: str
    source_ref: str = ""

    def __post_init__(self) -> None:
        _ascii_match(self.symbol, _SYMBOL_RE, "material_symbol")
        _ascii_match(self.cik, _CIK_RE, "material_cik")
        if not isinstance(self.venue, ListingVenueV1):
            raise ValueError("material_venue")
        if not isinstance(self.sector, MembershipSectorV1):
            raise ValueError("material_sector")
        if self.listed_at_as_of is not True or not isinstance(self.membership_as_of, date):
            raise ValueError("material_membership_as_of")
        _ascii_match(self.acquisition_id, _ID_RE, "material_acquisition_id")
        if not isinstance(self.membership_policy, MembershipPolicyV1):
            raise ValueError("material_membership_policy")
        _ascii_match(
            self.membership_holdings_digest,
            _HASH_RE,
            "material_membership_holdings_digest",
        )
        _ascii_match(
            self.security_policy_version,
            _VERSION_RE,
            "material_security_policy_version",
        )
        _ascii_match(
            self.sic_snapshot_version,
            _VERSION_RE,
            "material_sic_snapshot_version",
        )
        _ascii_match(self.sic_code, r"[0-9]{4}", "material_sic_code")
        if type(self.observation_instants) is not tuple or len(self.observation_instants) < 3:
            raise ValueError("material_observation_instants")
        if not all(isinstance(item, ObservationInstantV1) for item in self.observation_instants):
            raise ValueError("material_observation_instants")
        if len({item.source_kind for item in self.observation_instants}) != len(self.observation_instants):
            raise ValueError("material_observation_instants")
        for reference, code in (
            (self.listing_source_ref, "material_listing_source_ref"),
            (self.registry_source_ref, "material_registry_source_ref"),
            (self.issuer_source_ref, "material_issuer_source_ref"),
        ):
            _ascii_match(reference, rf"{_REF_PREFIX}{_HASH_RE}", code)
        _ascii_match(
            self.sic_snapshot_ref,
            rf"{_SIC_REF_PREFIX}{_HASH_RE}",
            "material_sic_snapshot_ref",
        )
        object.__setattr__(self, "source_ref", _digest(_MATERIAL_REF_PREFIX, self))


@dataclass(frozen=True, slots=True)
class SourceReceiptLinkV1:
    source_kind: ObservationSourceKindV1
    subject_key: str
    request_identity: RequestIdentityV1
    started_count: int
    observed_at: datetime
    payload_sha256: str

    def __post_init__(self) -> None:
        if not isinstance(self.source_kind, ObservationSourceKindV1):
            raise ValueError("receipt_source_kind")
        _ascii_match(self.subject_key, _ID_RE, "receipt_subject_key")
        if not isinstance(self.request_identity, RequestIdentityV1):
            raise ValueError("receipt_request_identity")
        if type(self.started_count) is not int or self.started_count < 1:
            raise ValueError("receipt_started_count")
        _aware(self.observed_at, "receipt_observed_at")
        _ascii_match(self.payload_sha256, _HASH_RE, "receipt_payload_sha256")


@dataclass(frozen=True, slots=True)
class OfficialMembershipStoreReceiptV1:
    status: str
    acquisition_id: str
    reason: str
    promoted_symbols: tuple[str, ...]
    stored_at: datetime
    source_receipts: tuple[SourceReceiptLinkV1, ...]

    def __post_init__(self) -> None:
        if self.status not in {"SAVED", "DUPLICATE", "CONFLICT"}:
            raise ValueError("store_receipt_status")
        _ascii_match(self.acquisition_id, _ID_RE, "store_receipt_acquisition_id")
        if self.reason:
            _ascii_match(self.reason, _DETAIL_RE, "store_receipt_reason")
        if type(self.promoted_symbols) is not tuple:
            raise ValueError("store_receipt_promoted_symbols")
        if any(fullmatch(_SYMBOL_RE, item) is None for item in self.promoted_symbols):
            raise ValueError("store_receipt_promoted_symbols")
        _aware(self.stored_at, "store_receipt_stored_at")
        if type(self.source_receipts) is not tuple or not all(
            isinstance(item, SourceReceiptLinkV1) for item in self.source_receipts
        ):
            raise ValueError("store_receipt_sources")
