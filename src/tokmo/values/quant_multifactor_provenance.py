"""Frozen offline-assembler inputs and provenance values for Quant G1B."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from re import fullmatch

from tokmo.values.current_holdings import CurrentHoldingsV1
from tokmo.values.fundamental_materials import OfficialFundamentalMaterialV1
from tokmo.values.official_membership_observation import (
    ObservationInstantV1,
    ObservationSourceKindV1,
)
from tokmo.values.quant_multifactor_core import CohortInputV1, MarketV1, SectorV1


class MemberSelection(str, Enum):
    MEMBER_UNAVAILABLE = "MEMBER_UNAVAILABLE"
    AMBIGUOUS_METRIC_PRESERVED = "AMBIGUOUS_METRIC_PRESERVED"
    UNAVAILABLE_METRIC_PRESERVED = "UNAVAILABLE_METRIC_PRESERVED"
    INVALID_CURRENCY = "INVALID_CURRENCY"
    INVALID_CANDLES = "INVALID_CANDLES"
    NONCONSECUTIVE_PRIOR_PRESERVED = "NONCONSECUTIVE_PRIOR_PRESERVED"
    INVALID_FUNDAMENTALS = "INVALID_FUNDAMENTALS"
    COHORT_UNAVAILABLE = "COHORT_UNAVAILABLE"


US_STORED_MARKET_VOCABULARY = ("US_NYSE", "US_NASDAQ", "US_NYSE_NASDAQ")
US_MARKET_VOCABULARY_DERIVATION = (
    "g1a:test_sec_fundamentals:183",
    "g1a:test_fundamental_material_store:57,91",
    "g1a:test_fundamental_material_values:41,74,108,141",
)
ASSEMBLY_PROVENANCE_VERSION = "quant-multifactor-assembly-provenance-v" + "2"
QMEM_SOURCE_REF_PREFIX = "qmemv" + "2" + ":sha256:"
_LISTING_SOURCE_KINDS = frozenset(
    {
        ObservationSourceKindV1.NASDAQ_LISTED,
        ObservationSourceKindV1.OTHER_LISTED,
    }
)
_REQUIRED_NON_LISTING_SOURCE_KINDS = frozenset(
    {
        ObservationSourceKindV1.SEC_REGISTRY,
        ObservationSourceKindV1.SEC_ISSUER,
    }
)


def membership_observation_instants_are_complete(
    observation_instants: tuple[ObservationInstantV1, ...],
) -> bool:
    if (
        type(observation_instants) is not tuple
        or len(observation_instants) != 3
        or not all(
            isinstance(item, ObservationInstantV1) for item in observation_instants
        )
    ):
        return False
    source_kinds = {item.source_kind for item in observation_instants}
    if len(source_kinds) != 3:
        return False
    return len(
        source_kinds & _LISTING_SOURCE_KINDS
    ) == 1 and _REQUIRED_NON_LISTING_SOURCE_KINDS.issubset(source_kinds)


@dataclass(frozen=True, slots=True)
class MembershipProofV1:
    symbol: str
    market: MarketV1
    listed_at_as_of: bool
    membership_as_of: date
    sector: SectorV1
    membership_holdings_digest: str
    observation_instants: tuple[ObservationInstantV1, ...]
    source_ref: str

    def __post_init__(self) -> None:
        if fullmatch(r"[A-Z0-9._-]{1,32}", self.symbol) is None:
            raise ValueError("membership_symbol")
        if (
            not isinstance(self.market, MarketV1)
            or not isinstance(self.sector, SectorV1)
            or not isinstance(self.membership_as_of, date)
        ):
            raise ValueError("membership_identity")
        if fullmatch(r"[0-9a-f]{64}", self.membership_holdings_digest) is None:
            raise ValueError("membership_holdings_digest")
        if not membership_observation_instants_are_complete(self.observation_instants):
            raise ValueError("membership_observation_instants")
        if (
            not self.source_ref.startswith(QMEM_SOURCE_REF_PREFIX)
            or len(self.source_ref) != 78
        ):
            raise ValueError("membership_source_ref")
        suffix = self.source_ref.removeprefix(QMEM_SOURCE_REF_PREFIX)
        if any(character not in "0123456789abcdef" for character in suffix):
            raise ValueError("membership_source_ref")


@dataclass(frozen=True, slots=True)
class CandleRequestIdentityV1:
    lane: str
    symbol: str
    market: MarketV1
    interval: str
    count: int
    adjusted: bool
    page_index: int
    before: datetime | None

    def __post_init__(self) -> None:
        if (
            self.lane != "qt"
            or self.interval != "1d"
            or self.count != 200
            or self.adjusted is not False
            or self.page_index not in (1, 2)
        ):
            raise ValueError("candle_request_identity")
        if fullmatch(r"[A-Z0-9._-]{1,32}", self.symbol) is None:
            raise ValueError("candle_symbol")
        if self.page_index == 1 and self.before is not None:
            raise ValueError("page1_before")
        if self.page_index == 2 and (self.before is None or self.before.tzinfo is None):
            raise ValueError("page2_before")

    @property
    def canonical_ref(self) -> str:
        payload = "|".join(
            (
                self.lane,
                self.symbol,
                self.market.value,
                self.interval,
                str(self.count),
                str(self.adjusted).lower(),
                str(self.page_index),
                self.before.isoformat() if self.before else "",
            )
        )
        return "qcandlev1:sha256:" + sha256(payload.encode("ascii")).hexdigest()


@dataclass(frozen=True, slots=True)
class RawCandleV1:
    timestamp: datetime
    close: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class RawCandlePageV1:
    """A provider page in the frozen newest-first (strict descending) order."""

    request: CandleRequestIdentityV1
    rows: tuple[RawCandleV1, ...]
    raw_row_count: int

    def __post_init__(self) -> None:
        if self.raw_row_count != len(self.rows) or not 0 <= self.raw_row_count <= 200:
            raise ValueError("raw_row_count")
        if any(
            row.timestamp.tzinfo is None or row.timestamp.utcoffset() is None
            for row in self.rows
        ):
            raise ValueError("raw_timestamp")
        if any(
            self.rows[index].timestamp <= self.rows[index + 1].timestamp
            for index in range(len(self.rows) - 1)
        ):
            raise ValueError("raw_page_order")


@dataclass(frozen=True, slots=True)
class MemberAssemblyInputV1:
    proof: MembershipProofV1 | None
    materials: tuple[OfficialFundamentalMaterialV1, ...]
    page1: RawCandlePageV1
    page2: RawCandlePageV1 | None


@dataclass(frozen=True, slots=True)
class AssemblyInputV1:
    holdings: CurrentHoldingsV1
    as_of: datetime
    members: tuple[MemberAssemblyInputV1, ...]
    universe_id: str
    universe_policy_version: str
    adjustment_version: str

    def __post_init__(self) -> None:
        bounds = (
            ("universe_id", self.universe_id, 64),
            ("universe_policy_version", self.universe_policy_version, 32),
            ("adjustment_version", self.adjustment_version, 32),
        )
        for name, value, maximum in bounds:
            if (
                not isinstance(value, str)
                or fullmatch(rf"[A-Za-z0-9._:/-]{{1,{maximum}}}", value) is None
            ):
                raise ValueError(name)


@dataclass(frozen=True, slots=True)
class MemberReasonV1:
    selection: MemberSelection
    detail: str
    metric_selections: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.selection, MemberSelection)
            or fullmatch(r"[a-z_]{1,48}", self.detail) is None
        ):
            raise ValueError("member_reason")
        if len(self.metric_selections) > 6 or any(
            fullmatch(r"[a-z_]{1,48}", name) is None
            or fullmatch(r"[a-z_]{1,24}", selection) is None
            for name, selection in self.metric_selections
        ):
            raise ValueError("metric_selections")


@dataclass(frozen=True, slots=True)
class AssemblyOutcomeV1:
    cohort_input: CohortInputV1 | None
    reasons: tuple[MemberReasonV1, ...]
    provenance_digest: str
    admissible_count: int
    market_vocabulary: tuple[str, ...]
    provenance_version: str
    market_vocabulary_derivation: tuple[str, ...]
