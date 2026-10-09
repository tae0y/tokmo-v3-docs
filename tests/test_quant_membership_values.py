from dataclasses import FrozenInstanceError, replace
from datetime import UTC, date, datetime

import pytest

from tokmo.values.official_membership_observation import (
    ListingObservationV1,
    ListingVenueV1,
    MembershipMaterialV1,
    MembershipPolicyV1,
    MembershipSectorV1,
    ObservationInstantV1,
    ObservationSourceKindV1,
    ObservedSourcePayloadV1,
    OfficialMembershipAcquisitionV1,
    OfficialMembershipStoreReceiptV1,
    OfficialSicSnapshotV1,
    RequestIdentityV1,
    SecIssuerObservationV1,
    SecRegistryObservationV1,
    SecurityClassV1,
    SourceReceiptLinkV1,
)

NOW = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)


def request_identity(path_template="/directory/{family}.txt"):
    return RequestIdentityV1(
        method="GET",
        host="docs.example.test",
        path_template=path_template,
        source_schema_version="official-membership-source-v1",
    )


def payload(source_kind, subject_key, observed_at=NOW):
    return ObservedSourcePayloadV1(
        acquisition_id="acq-20260813-0001",
        source_kind=source_kind,
        request_identity=request_identity(),
        subject_key=subject_key,
        started_count=1,
        observed_at=observed_at,
        payload_bytes=f"{source_kind.value}:{subject_key}".encode("ascii"),
    )


def listing(symbol="AAPL", observed_at=NOW):
    body = payload(ObservationSourceKindV1.NASDAQ_LISTED, "nasdaqlisted")
    return ListingObservationV1(
        symbol=symbol,
        source_kind=ObservationSourceKindV1.NASDAQ_LISTED,
        venue=ListingVenueV1.NASDAQ,
        venue_token="Q",
        security_name_hash="a" * 64,
        security_class=SecurityClassV1.COMMON_EQUITY,
        observed_at=observed_at,
        directory_creation_time=observed_at,
        body_sha256=body.payload_sha256,
        row_sha256="b" * 64,
        source_schema_version="nasdaq-directory-v1",
        test_issue_token="N",
        financial_status_token="N",
        etf_token="N",
    )


def registry(symbol="AAPL", observed_at=NOW):
    body = payload(ObservationSourceKindV1.SEC_REGISTRY, "company_tickers_exchange")
    return SecRegistryObservationV1(
        symbol=symbol,
        cik="0000320193",
        exchange_token="Nasdaq",
        name_hash="c" * 64,
        observed_at=observed_at,
        body_sha256=body.payload_sha256,
        row_sha256="d" * 64,
        source_schema_version="sec-registry-v1",
    )


def issuer(symbol="AAPL", observed_at=NOW):
    body = payload(ObservationSourceKindV1.SEC_ISSUER, symbol)
    return SecIssuerObservationV1(
        cik="0000320193",
        symbol=symbol,
        exchange_token="NASDAQ",
        sic_code="3571",
        observed_at=observed_at,
        body_sha256=body.payload_sha256,
        source_schema_version="sec-submissions-v1",
    )


def sic_snapshot():
    return OfficialSicSnapshotV1(
        snapshot_version="sec-sic-html-v1",
        classification="RECONSTRUCTED_CONTRACT",
        codes=("6799", "3571", "6000"),
        documentation_ref="fixture-sec-sic-html-v1",
    )


def material(symbol="AAPL"):
    listing_observation = listing(symbol)
    registry_observation = registry(symbol)
    issuer_observation = issuer(symbol)
    return MembershipMaterialV1(
        symbol=symbol,
        cik="0000320193",
        venue=ListingVenueV1.NASDAQ,
        sector=MembershipSectorV1.NON_FINANCIALS,
        listed_at_as_of=True,
        membership_as_of=date(2026, 8, 12),
        acquisition_id="acq-20260813-0001",
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        membership_holdings_digest="f" * 64,
        security_policy_version="official-security-name-v1",
        sic_snapshot_version="sec-sic-html-v1",
        sic_code="3571",
        observation_instants=(
            ObservationInstantV1(
                ObservationSourceKindV1.NASDAQ_LISTED,
                listing_observation.observed_at,
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_REGISTRY,
                registry_observation.observed_at,
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_ISSUER,
                issuer_observation.observed_at,
            ),
        ),
        listing_source_ref=listing_observation.source_ref,
        registry_source_ref=registry_observation.source_ref,
        issuer_source_ref=issuer_observation.source_ref,
        sic_snapshot_ref=sic_snapshot().source_ref,
    )


def test_values_are_frozen_and_digest_bound():
    value = material()
    receipt = OfficialMembershipStoreReceiptV1(
        status="SAVED",
        acquisition_id="acq-20260813-0001",
        reason="",
        promoted_symbols=("AAPL",),
        stored_at=NOW,
        source_receipts=(
            SourceReceiptLinkV1(
                source_kind=ObservationSourceKindV1.NASDAQ_LISTED,
                subject_key="nasdaqlisted",
                request_identity=request_identity(),
                started_count=1,
                observed_at=NOW,
                payload_sha256="1" * 64,
            ),
        ),
    )
    with pytest.raises(FrozenInstanceError):
        value.symbol = "MSFT"
    assert value.source_ref.startswith("ommatv1:sha256:")
    assert receipt.source_receipts[0].payload_sha256 == "1" * 64
    assert replace(value, sic_code="3572").source_ref != value.source_ref


def test_acquisition_payloads_and_sic_snapshot_normalize_deterministically():
    acquisition = OfficialMembershipAcquisitionV1(
        acquisition_id="acq-20260813-0001",
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        security_policy_version="official-security-name-v1",
        source_contract_version="official-membership-source-v1",
        sic_snapshot=sic_snapshot(),
        listings=(listing(),),
        registry_rows=(registry(),),
        issuer_rows=(issuer(),),
    )
    same_codes = replace(
        sic_snapshot(),
        codes=("3571", "6000", "6799"),
    )
    assert acquisition.sic_snapshot.source_ref == same_codes.source_ref
    assert acquisition.listings[0].source_ref.startswith("omsrcv1:sha256:")
    assert (
        payload(
            ObservationSourceKindV1.SEC_ISSUER,
            "AAPL",
        ).payload_sha256
        != payload(
            ObservationSourceKindV1.SEC_ISSUER,
            "MSFT",
        ).payload_sha256
    )


@pytest.mark.parametrize(
    ("factory", "changes"),
    (
        (request_identity, {"method": "POST"}),
        (request_identity, {"host": "not ascii-\ud55c"}),
        (payload, {"subject_key": ""}),
        (listing, {"venue_token": ""}),
        (listing, {"directory_creation_time": NOW.replace(tzinfo=None)}),
        (registry, {"cik": "320193"}),
        (issuer, {"sic_code": "357"}),
        (material, {"membership_holdings_digest": "bad"}),
    ),
)
def test_values_reject_invalid_identity_bounds(factory, changes):
    if factory is request_identity:
        values = {
            "method": "GET",
            "host": "docs.example.test",
            "path_template": "/directory/{family}.txt",
            "source_schema_version": "official-membership-source-v1",
        }
        values.update(changes)
        with pytest.raises(ValueError):
            RequestIdentityV1(**values)
        return
    if factory is payload:
        values = {
            "acquisition_id": "acq-20260813-0001",
            "source_kind": ObservationSourceKindV1.SEC_ISSUER,
            "request_identity": request_identity(),
            "subject_key": "AAPL",
            "started_count": 1,
            "observed_at": NOW,
            "payload_bytes": b"x",
        }
        values.update(changes)
        with pytest.raises(ValueError):
            ObservedSourcePayloadV1(**values)
        return
    if factory is listing:
        with pytest.raises(ValueError):
            replace(listing(), **changes)
        return
    if factory is registry:
        with pytest.raises(ValueError):
            replace(registry(), **changes)
        return
    if factory is issuer:
        with pytest.raises(ValueError):
            replace(issuer(), **changes)
        return
    with pytest.raises(ValueError):
        replace(material(), **changes)
