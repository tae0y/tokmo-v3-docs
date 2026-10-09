"""Pure official-membership assembly and proof adaptation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from hashlib import sha256
from zoneinfo import ZoneInfo

from tokmo.values.account_scope_patterns import ACCOUNT_SCOPE_RE, PENSION_SCOPE_RE
from tokmo.values.current_holdings import (
    CurrentHoldingsV1,
    canonical_holdings_bytes,
    classify_market,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.official_membership_observation import (
    ListingVenueV1,
    MembershipMaterialV1,
    MembershipPolicyV1,
    MembershipSectorV1,
    MembershipUnavailableReasonV1,
    ObservationInstantV1,
    ObservationSourceKindV1,
    OfficialMembershipAcquisitionV1,
    SecurityClassV1,
)
from tokmo.values.quant_multifactor_core import MarketV1, SectorV1
from tokmo.values.quant_multifactor_provenance import (
    QMEM_SOURCE_REF_PREFIX,
    MembershipProofV1,
    membership_observation_instants_are_complete,
)
from tokmo.values.session_gate import MARKET_TIMEZONE, Market

MAX_OBSERVATION_AGE = timedelta(minutes=30)
MAX_OBSERVATION_SKEW = timedelta(minutes=10)
_US_MARKET_TIMEZONE = ZoneInfo(MARKET_TIMEZONE[Market.US])


@dataclass(frozen=True, slots=True)
class MembershipCohortBindingV1:
    expected_symbols: tuple[str, ...]
    membership_holdings_digest: str
    membership_as_of: date
    holdings_bytes: bytes


@dataclass(frozen=True, slots=True)
class MembershipAssemblyResultV1:
    materials: tuple[MembershipMaterialV1, ...]
    proofs: tuple[MembershipProofV1, ...]
    unavailable_reasons: tuple[tuple[str, MembershipUnavailableReasonV1], ...]
    holdings_bytes: bytes


def _us_market_date(instant: datetime) -> date:
    """Return the market-local date using the canonical IANA timezone rules."""
    return instant.astimezone(_US_MARKET_TIMEZONE).date()


def describe_us_personal_membership_cohort(
    holdings: CurrentHoldingsV1,
    evaluation_as_of: datetime,
) -> MembershipCohortBindingV1:
    if evaluation_as_of.tzinfo is None or evaluation_as_of.utcoffset() is None:
        raise ValueError("evaluation_as_of")
    if holdings.captured_at > evaluation_as_of:
        raise ValueError("holdings_future")
    if evaluation_as_of - holdings.captured_at > MAX_OBSERVATION_AGE:
        raise ValueError("holdings_stale")
    if _us_market_date(holdings.captured_at) != _us_market_date(evaluation_as_of):
        raise ValueError("holdings_market_date")
    symbol_markets: dict[str, object] = {}
    for account in holdings.accounts:
        scope = account.account_scope
        if ACCOUNT_SCOPE_RE.fullmatch(scope) is not None:
            include = True
        elif PENSION_SCOPE_RE.fullmatch(scope) is not None:
            include = False
        else:
            raise ValueError("account_scope")
        if not include:
            continue
        for item in account.holdings:
            market = classify_market(item.market_raw, item.market_field)
            if market is None:
                raise ValueError("market_unknown")
            prior = symbol_markets.get(item.symbol)
            if prior is not None and prior is not market:
                raise ValueError(f"market_conflict:{item.symbol}")
            symbol_markets[item.symbol] = market
    expected_symbols = tuple(
        sorted(
            symbol
            for symbol, market in symbol_markets.items()
            if getattr(market, "value", None) == "US"
        )
    )
    holdings_bytes = canonical_holdings_bytes(holdings)
    return MembershipCohortBindingV1(
        expected_symbols=expected_symbols,
        membership_holdings_digest=sha256(holdings_bytes).hexdigest(),
        membership_as_of=_us_market_date(evaluation_as_of),
        holdings_bytes=holdings_bytes,
    )


def proof_is_admissible(
    proof: MembershipProofV1,
    *,
    binding: MembershipCohortBindingV1,
    evaluation_as_of: datetime,
) -> bool:
    if (
        proof.market is not MarketV1.US_NYSE_NASDAQ
        or proof.listed_at_as_of is not True
        or proof.membership_as_of != binding.membership_as_of
        or proof.membership_holdings_digest != binding.membership_holdings_digest
        or not membership_observation_instants_are_complete(proof.observation_instants)
    ):
        return False
    observed = tuple(item.observed_at for item in proof.observation_instants)
    if not observed:
        return False
    if any(item > evaluation_as_of for item in observed):
        return False
    if any(evaluation_as_of - item > MAX_OBSERVATION_AGE for item in observed):
        return False
    if max(observed) - min(observed) > MAX_OBSERVATION_SKEW:
        return False
    return all(_us_market_date(item) == binding.membership_as_of for item in observed)


def _sector_from_sic(sic_code: str) -> MembershipSectorV1:
    code = int(sic_code)
    if 6000 <= code <= 6799:
        return MembershipSectorV1.FINANCIALS
    return MembershipSectorV1.NON_FINANCIALS


def _proof_source_ref(
    *,
    symbol: str,
    sector: MembershipSectorV1,
    binding: MembershipCohortBindingV1,
    acquisition: OfficialMembershipAcquisitionV1,
    listing_source_ref: str,
    registry_source_ref: str,
    issuer_source_ref: str,
    instants: tuple[ObservationInstantV1, ...],
) -> str:
    payload = {
        "symbol": symbol,
        "market": MarketV1.US_NYSE_NASDAQ.value,
        "membership_as_of": binding.membership_as_of.isoformat(),
        "sector": sector.value,
        "membership_holdings_digest": binding.membership_holdings_digest,
        "acquisition_id": acquisition.acquisition_id,
        "membership_policy": acquisition.membership_policy.value,
        "security_policy_version": acquisition.security_policy_version,
        "source_contract_version": acquisition.source_contract_version,
        "sic_snapshot_ref": acquisition.sic_snapshot.source_ref,
        "listing_source_ref": listing_source_ref,
        "registry_source_ref": registry_source_ref,
        "issuer_source_ref": issuer_source_ref,
        "observation_instants": tuple(
            {
                "source_kind": item.source_kind.value,
                "observed_at": item.observed_at,
            }
            for item in instants
        ),
    }
    return QMEM_SOURCE_REF_PREFIX + sha256(canonical_encode(payload)).hexdigest()


def _duplicate_symbols(rows: tuple) -> tuple[str, ...]:
    seen = set()
    duplicates = set()
    for item in rows:
        if item.symbol in seen:
            duplicates.add(item.symbol)
        seen.add(item.symbol)
    return tuple(sorted(duplicates))


def _extra_symbols(rows: tuple, expected_symbols: set[str]) -> tuple[str, ...]:
    return tuple(sorted({item.symbol for item in rows} - expected_symbols))


def assemble_us_personal_membership_material(
    *,
    holdings: CurrentHoldingsV1,
    evaluation_as_of: datetime,
    acquisition: OfficialMembershipAcquisitionV1,
) -> MembershipAssemblyResultV1:
    try:
        binding = describe_us_personal_membership_cohort(holdings, evaluation_as_of)
    except ValueError as error:
        symbol = str(error).partition(":")[2] or ""
        reason = (
            MembershipUnavailableReasonV1.IDENTITY_CONFLICT
            if str(error).startswith("market_conflict:")
            else MembershipUnavailableReasonV1.COHORT_MISMATCH
        )
        return MembershipAssemblyResultV1(
            (), (), ((symbol, reason),), canonical_holdings_bytes(holdings)
        )
    expected_symbols = set(binding.expected_symbols)
    for rows in (
        acquisition.listings,
        acquisition.registry_rows,
        acquisition.issuer_rows,
    ):
        duplicate_symbols = _duplicate_symbols(rows)
        if duplicate_symbols:
            return MembershipAssemblyResultV1(
                (),
                (),
                tuple(
                    (symbol, MembershipUnavailableReasonV1.IDENTITY_CONFLICT)
                    for symbol in duplicate_symbols
                ),
                binding.holdings_bytes,
            )
        extra_symbols = _extra_symbols(rows, expected_symbols)
        if extra_symbols:
            return MembershipAssemblyResultV1(
                (),
                (),
                tuple(
                    (symbol, MembershipUnavailableReasonV1.COHORT_MISMATCH)
                    for symbol in extra_symbols
                ),
                binding.holdings_bytes,
            )
    listings = {item.symbol: item for item in acquisition.listings}
    registry_rows = {item.symbol: item for item in acquisition.registry_rows}
    issuer_rows = {item.symbol: item for item in acquisition.issuer_rows}
    materials = []
    proofs = []
    reasons = []
    for symbol in binding.expected_symbols:
        listing = listings.get(symbol)
        if listing is None:
            reasons.append((symbol, MembershipUnavailableReasonV1.ABSENT_LISTING))
            continue
        registry = registry_rows.get(symbol)
        if registry is None:
            reasons.append(
                (symbol, MembershipUnavailableReasonV1.SUPPORTING_REGISTRY_ABSENT)
            )
            continue
        issuer = issuer_rows.get(symbol)
        if issuer is None:
            reasons.append((symbol, MembershipUnavailableReasonV1.PARTIAL_ACQUISITION))
            continue
        instants = (
            ObservationInstantV1(listing.source_kind, listing.observed_at),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_REGISTRY, registry.observed_at
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_ISSUER, issuer.observed_at
            ),
        )
        observed = tuple(item.observed_at for item in instants)
        if any(item > evaluation_as_of for item in observed):
            reasons.append((symbol, MembershipUnavailableReasonV1.FUTURE_OBSERVATION))
            continue
        if any(evaluation_as_of - item > MAX_OBSERVATION_AGE for item in observed):
            reasons.append((symbol, MembershipUnavailableReasonV1.STALE_OBSERVATION))
            continue
        if max(observed) - min(observed) > MAX_OBSERVATION_SKEW:
            reasons.append((symbol, MembershipUnavailableReasonV1.SOURCE_SKEW))
            continue
        if any(_us_market_date(item) != binding.membership_as_of for item in observed):
            reasons.append((symbol, MembershipUnavailableReasonV1.SOURCE_SKEW))
            continue
        if listing.security_class is not SecurityClassV1.COMMON_EQUITY:
            reasons.append((symbol, MembershipUnavailableReasonV1.UNSUPPORTED_TYPE))
            continue
        if listing.etf_token != "N":
            reasons.append((symbol, MembershipUnavailableReasonV1.UNSUPPORTED_TYPE))
            continue
        if listing.test_issue_token != "N" or (
            listing.source_kind is ObservationSourceKindV1.NASDAQ_LISTED
            and listing.financial_status_token != "N"
        ):
            reasons.append((symbol, MembershipUnavailableReasonV1.UNSUPPORTED_STATUS))
            continue
        registry_venue = {
            "NASDAQ": ListingVenueV1.NASDAQ,
            "NYSE": ListingVenueV1.NYSE,
        }.get(registry.exchange_token)
        if registry_venue is None or registry_venue is not listing.venue:
            reasons.append(
                (symbol, MembershipUnavailableReasonV1.REGISTRY_CONTRADICTION)
            )
            continue
        if registry.cik != issuer.cik:
            reasons.append((symbol, MembershipUnavailableReasonV1.IDENTITY_CONFLICT))
            continue
        if issuer.exchange_token != listing.venue.value:
            reasons.append(
                (symbol, MembershipUnavailableReasonV1.REGISTRY_CONTRADICTION)
            )
            continue
        if issuer.sic_code not in acquisition.sic_snapshot.codes:
            reasons.append((symbol, MembershipUnavailableReasonV1.MISSING_SIC))
            continue
        sector = _sector_from_sic(issuer.sic_code)
        material = MembershipMaterialV1(
            symbol=symbol,
            cik=issuer.cik,
            venue=listing.venue,
            sector=sector,
            listed_at_as_of=True,
            membership_as_of=binding.membership_as_of,
            acquisition_id=acquisition.acquisition_id,
            membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
            membership_holdings_digest=binding.membership_holdings_digest,
            security_policy_version=acquisition.security_policy_version,
            sic_snapshot_version=acquisition.sic_snapshot.snapshot_version,
            sic_code=issuer.sic_code,
            observation_instants=instants,
            listing_source_ref=listing.source_ref,
            registry_source_ref=registry.source_ref,
            issuer_source_ref=issuer.source_ref,
            sic_snapshot_ref=acquisition.sic_snapshot.source_ref,
        )
        materials.append(material)
        proofs.append(
            MembershipProofV1(
                symbol=symbol,
                market=MarketV1.US_NYSE_NASDAQ,
                listed_at_as_of=True,
                membership_as_of=binding.membership_as_of,
                sector=(
                    SectorV1.FINANCIALS
                    if sector is MembershipSectorV1.FINANCIALS
                    else SectorV1.NON_FINANCIALS
                ),
                membership_holdings_digest=binding.membership_holdings_digest,
                observation_instants=instants,
                source_ref=_proof_source_ref(
                    symbol=symbol,
                    sector=sector,
                    binding=binding,
                    acquisition=acquisition,
                    listing_source_ref=listing.source_ref,
                    registry_source_ref=registry.source_ref,
                    issuer_source_ref=issuer.source_ref,
                    instants=instants,
                ),
            )
        )
    if reasons:
        return MembershipAssemblyResultV1(
            (),
            (),
            tuple(reasons),
            binding.holdings_bytes,
        )
    return MembershipAssemblyResultV1(
        tuple(sorted(materials, key=lambda item: item.symbol)),
        tuple(sorted(proofs, key=lambda item: item.symbol)),
        (),
        binding.holdings_bytes,
    )
