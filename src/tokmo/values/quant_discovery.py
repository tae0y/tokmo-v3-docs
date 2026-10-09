"""Immutable values for the Quant discovery engine."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any


class ScalarEnum(str, Enum):
    def __hash__(self) -> int:
        return hash(self.value)


class QuantProductKind(ScalarEnum):
    MARKET_DISCOVERY = "MARKET_DISCOVERY"
    PERSONAL_PEER = "PERSONAL_PEER"
    PENSION_EFFICIENCY = "PENSION_EFFICIENCY"


class QuantMarketProfileV1(ScalarEnum):
    KR = "KR"
    US = "US"


class FactorOrientation(ScalarEnum):
    HIGHER_IS_BETTER = "HIGHER_IS_BETTER"
    LOWER_IS_BETTER = "LOWER_IS_BETTER"


class FactorStatus(ScalarEnum):
    AVAILABLE = "AVAILABLE"
    INSUFFICIENT = "INSUFFICIENT"
    INVALID = "INVALID"
    ZERO_DENOMINATOR = "ZERO_DENOMINATOR"


class QuantUnavailableCode(ScalarEnum):
    NO_COMPLETED_SESSION = "NO_COMPLETED_SESSION"
    RANKING_NO_DATA = "RANKING_NO_DATA"
    PROVIDER_FAILURE = "PROVIDER_FAILURE"
    PROVIDER_CONTRACT_MISMATCH = "PROVIDER_CONTRACT_MISMATCH"
    STOCK_METADATA_UNAVAILABLE = "STOCK_METADATA_UNAVAILABLE"
    CANDLE_SESSION_NOT_READY = "CANDLE_SESSION_NOT_READY"
    INSUFFICIENT_PRICE_HISTORY = "INSUFFICIENT_PRICE_HISTORY"
    PEER_UNCONFIRMED = "PEER_UNCONFIRMED"
    PERSONAL_SOURCE_UNAVAILABLE = "PERSONAL_SOURCE_UNAVAILABLE"
    PENSION_ACTIVE_REVISION_ABSENT = "PENSION_ACTIVE_REVISION_ABSENT"
    PENSION_WEEK_NOT_FINAL = "PENSION_WEEK_NOT_FINAL"


def _decimal(value: Decimal | None, name: str, *, nonnegative: bool = False) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, Decimal)
        or not value.is_finite()
    ):
        raise ValueError(f"{name} must be a finite Decimal or None")
    if nonnegative and value < 0:
        raise ValueError(f"{name} must be non-negative")


@dataclass(frozen=True)
class PriceBarV1:
    session_date: date
    open: Decimal | None
    high: Decimal | None
    low: Decimal | None
    close: Decimal | None
    volume: Decimal | None
    trading_amount: Decimal | None

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date):
            raise ValueError("session_date must be a date")
        for name in ("open", "high", "low", "close"):
            _decimal(getattr(self, name), name)
        _decimal(self.volume, "volume", nonnegative=True)
        _decimal(self.trading_amount, "trading_amount", nonnegative=True)
        if self.high is not None and self.low is not None and self.high < self.low:
            raise ValueError("high must not be below low")
        if self.close is not None:
            if self.close <= 0:
                raise ValueError("close must be positive")
            if self.high is not None and self.close > self.high:
                raise ValueError("close must not exceed high")
            if self.low is not None and self.close < self.low:
                raise ValueError("close must not be below low")


@dataclass(frozen=True)
class PriceSeriesV1:
    symbol: str
    market: QuantMarketProfileV1
    bars: tuple[PriceBarV1, ...]

    def __post_init__(self) -> None:
        if not self.symbol or not isinstance(self.bars, tuple) or len(self.bars) > 200:
            raise ValueError("invalid price series")
        dates = tuple(bar.session_date for bar in self.bars)
        if dates != tuple(sorted(dates)) or len(set(dates)) != len(dates):
            raise ValueError("bars must have ascending unique dates")


@dataclass(frozen=True)
class FactorValueV1:
    name: str
    raw_value: Decimal | None
    orientation: FactorOrientation
    status: FactorStatus
    observed_count: int
    minimum_count: int

    def __post_init__(self) -> None:
        _decimal(self.raw_value, "raw_value")
        if (
            not self.name
            or isinstance(self.observed_count, bool)
            or isinstance(self.minimum_count, bool)
        ):
            raise ValueError("invalid factor metadata")
        if self.observed_count < 0 or self.minimum_count < 1:
            raise ValueError("invalid observation counts")
        if (self.status is FactorStatus.AVAILABLE) != (self.raw_value is not None):
            raise ValueError("available status and raw value must agree")


@dataclass(frozen=True)
class FeatureSetV1:
    symbol: str
    market: QuantMarketProfileV1
    completed_session_date: date
    factors: tuple[FactorValueV1, ...]

    def __post_init__(self) -> None:
        names = tuple(f.name for f in self.factors)
        if len(names) != len(set(names)):
            raise ValueError("duplicate factor names")


@dataclass(frozen=True)
class FeatureOutcomeV1:
    features: FeatureSetV1
    unavailable_code: QuantUnavailableCode | None


@dataclass(frozen=True)
class FactorStandingV1:
    factor_name: str
    symbol: str
    ordinal: Decimal
    sample_size: int
    raw_value: Decimal
    orientation: FactorOrientation
    percentile: Decimal | None
    percentile_label: str | None

    def __post_init__(self) -> None:
        _decimal(self.ordinal, "ordinal")
        _decimal(self.raw_value, "raw_value")
        _decimal(self.percentile, "percentile")
        if self.sample_size < 1 or self.ordinal < 1 or self.ordinal > self.sample_size:
            raise ValueError("invalid standing")
        if self.sample_size < 10 and (
            self.percentile is not None or self.percentile_label is not None
        ):
            raise ValueError("percentile requires ten peers")
        if (self.percentile is None) != (self.percentile_label is None):
            raise ValueError("percentile and label must agree")


@dataclass(frozen=True)
class ReferenceEntryZoneV1:
    lower: Decimal
    upper: Decimal
    setup_kind: str
    evidence: tuple[str, ...]
    invalidation: Decimal
    expiry_completed_sessions: int
    latest_close: Decimal
    distance_fraction: Decimal
    confidence: int
    confidence_reason: str
    change_condition: str


@dataclass(frozen=True)
class QuantCandidateV1:
    symbol: str
    market: QuantMarketProfileV1
    name: str
    instrument_type: str
    source_rankings: tuple[int, ...]
    ranking_turnover: Decimal | None
    ranking_change_rate: Decimal | None
    leverage_factor: Decimal | None


@dataclass(frozen=True)
class QuantRunIdentityV1:
    product_kind: QuantProductKind
    market: QuantMarketProfileV1
    completed_session_date: date | None
    completed_iso_week: str | None
    profile_revision: str
    peer_registry_revision: str | None
    report_revision: str
    identity_sha256: str

    def __post_init__(self) -> None:
        if (self.completed_session_date is None) == (self.completed_iso_week is None):
            raise ValueError("exactly one completed-period identity is required")


@dataclass(frozen=True)
class DecisionCardV1:
    symbol: str
    market: QuantMarketProfileV1
    name: str
    factors: tuple[FactorValueV1, ...]
    standings: tuple[FactorStandingV1, ...]
    entry_zone: ReferenceEntryZoneV1 | None
    unavailable_codes: tuple[QuantUnavailableCode, ...]


@dataclass(frozen=True)
class QuantRunReportV1:
    identity: QuantRunIdentityV1
    cards: tuple[DecisionCardV1, ...]
    unavailable_codes: tuple[QuantUnavailableCode, ...]
    operation_counts: tuple[tuple[str, int], ...]
    delivery_state: str = "NOT_ACTIVATED"


@dataclass(frozen=True)
class QuantRunReceiptV1:
    identity: QuantRunIdentityV1
    outcome: str
    operation_counts: tuple[tuple[str, int], ...]
    report_sha256: str
    delivery_state: str = "NOT_ACTIVATED"

    def __post_init__(self) -> None:
        if len(self.report_sha256) != 64 or any(
            c not in "0123456789abcdef" for c in self.report_sha256
        ):
            raise ValueError("invalid report digest")


@dataclass(frozen=True)
class PeerGroupV1:
    group_id: str
    product_kind: QuantProductKind
    market: QuantMarketProfileV1
    instrument_type: str
    reference_symbol: str
    peer_symbols: tuple[str, ...]
    provenance_note: str
    last_reviewed: date
    labels: tuple[tuple[str, Any], ...]


@dataclass(frozen=True)
class PeerRegistryV1:
    schema: str
    revision: str
    reviewed_at: date
    groups: tuple[PeerGroupV1, ...]


_REQUIRED = {
    "group_id",
    "product_kind",
    "market",
    "instrument_type",
    "reference_symbol",
    "peer_symbols",
    "provenance_note",
    "last_reviewed",
}
_OPTIONAL = {
    "industry",
    "exposure",
    "index",
    "region",
    "theme",
    "hedge",
    "leverage_factor",
    "inverse",
    "duration",
}


_REQUIRED_TEXT = ("group_id", "instrument_type", "reference_symbol", "provenance_note")


def _immutable_label(key: str, value: object) -> object:
    if key == "leverage_factor":
        if isinstance(value, bool) or not isinstance(value, (int, float, str)):
            raise ValueError("leverage_factor must be a finite numeric scalar")
        try:
            number = Decimal(str(value))
        except Exception as exc:
            raise ValueError("leverage_factor must be a finite numeric scalar") from exc
        if not number.is_finite():
            raise ValueError("leverage_factor must be a finite numeric scalar")
        return value
    if key in {"inverse", "hedge"} and not isinstance(value, (bool, str)):
        raise ValueError(f"{key} label must be bool or string")
    scalar_types = (str, int, float, bool, type(None))

    def safe_scalar(item: object) -> bool:
        return isinstance(item, scalar_types) and not (
            isinstance(item, float) and not Decimal(str(item)).is_finite()
        )

    if safe_scalar(value):
        return value
    if isinstance(value, list) and all(safe_scalar(item) for item in value):
        return tuple(value)
    raise ValueError(f"{key} label must be an immutable scalar or flat scalar tuple")


def _iso_date(value: object, name: str) -> date:
    if not isinstance(value, str):
        raise ValueError(f"{name} must be an ISO date")
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an ISO date") from exc


def load_peer_registry(path: str | Path) -> PeerRegistryV1:
    raw = Path(path).read_bytes()
    try:
        data = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid registry JSON") from exc
    canonical = (
        json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
        + "\n"
    ).encode()
    if raw != canonical:
        raise ValueError("registry must use canonical JSON bytes")
    if not isinstance(data, dict) or set(data) != {
        "schema",
        "revision",
        "reviewed_at",
        "groups",
    }:
        raise ValueError("unknown or missing top-level registry field")
    if (
        data["schema"] != "quant-peer-registry-v1"
        or not isinstance(data["revision"], str)
        or not data["revision"]
    ):
        raise ValueError("invalid registry identity")
    if not isinstance(data["groups"], list):
        raise ValueError("groups must be a list")
    groups = []
    identities = set()
    for item in data["groups"]:
        if (
            not isinstance(item, dict)
            or not _REQUIRED <= set(item)
            or set(item) - _REQUIRED - _OPTIONAL
        ):
            raise ValueError("unknown or missing group field")
        if any(
            not isinstance(item[field], str) or not item[field]
            for field in _REQUIRED_TEXT
        ):
            raise ValueError("required scalar fields must be non-empty strings")
        peers = item["peer_symbols"]
        if (
            not isinstance(peers, list)
            or not peers
            or peers != sorted(peers)
            or any(not isinstance(x, str) or not x for x in peers)
            or len(peers) != len(set(peers))
        ):
            raise ValueError("peer symbols must be non-empty, unique, and sorted")
        if item["reference_symbol"] in peers:
            raise ValueError("reference symbol cannot be a peer")
        product = QuantProductKind(
            item["product_kind"]
            + ("_PEER" if item["product_kind"] == "PERSONAL" else "_EFFICIENCY")
        )
        if product not in (
            QuantProductKind.PERSONAL_PEER,
            QuantProductKind.PENSION_EFFICIENCY,
        ):
            raise ValueError("invalid peer namespace")
        market = QuantMarketProfileV1(item["market"])
        identity = (product, market, item["group_id"])
        if identity in identities:
            raise ValueError("duplicate peer group identity")
        identities.add(identity)
        labels = tuple(
            (key, _immutable_label(key, item[key]))
            for key in sorted(_OPTIONAL & set(item))
        )
        groups.append(
            PeerGroupV1(
                item["group_id"],
                product,
                market,
                item["instrument_type"],
                item["reference_symbol"],
                tuple(peers),
                item["provenance_note"],
                _iso_date(item["last_reviewed"], "last_reviewed"),
                labels,
            )
        )
    if [group.group_id for group in groups] != sorted(
        group.group_id for group in groups
    ):
        raise ValueError("groups must be sorted")
    return PeerRegistryV1(
        data["schema"],
        data["revision"],
        _iso_date(data["reviewed_at"], "reviewed_at"),
        tuple(groups),
    )
