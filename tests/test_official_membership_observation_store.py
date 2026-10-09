import sqlite3
import stat
from dataclasses import replace
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

import tokmo.records.official_membership_observation_store as membership_store_module
from tokmo.records.official_membership_observation_store import (
    OfficialMembershipObservationStore,
)
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
    OfficialSicSnapshotV1,
    RequestIdentityV1,
    SecIssuerObservationV1,
    SecRegistryObservationV1,
    SecurityClassV1,
)

NOW = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)


def request_identity(path_template="/directory/{family}.txt"):
    return RequestIdentityV1(
        method="GET",
        host="docs.example.test",
        path_template=path_template,
        source_schema_version="official-membership-source-v1",
    )


def payload(source_kind, subject_key, *, body=b"fixture-bytes", observed_at=NOW):
    return ObservedSourcePayloadV1(
        acquisition_id="acq-20260813-0001",
        source_kind=source_kind,
        request_identity=request_identity(),
        subject_key=subject_key,
        started_count=1,
        observed_at=observed_at,
        payload_bytes=body,
    )


def listing(symbol="AAPL", observed_at=NOW):
    source = payload(ObservationSourceKindV1.NASDAQ_LISTED, "nasdaqlisted")
    return ListingObservationV1(
        symbol=symbol,
        source_kind=ObservationSourceKindV1.NASDAQ_LISTED,
        venue=ListingVenueV1.NASDAQ,
        venue_token="Q",
        security_name_hash="a" * 64,
        security_class=SecurityClassV1.COMMON_EQUITY,
        observed_at=observed_at,
        directory_creation_time=observed_at,
        body_sha256=source.payload_sha256,
        row_sha256="b" * 64,
        source_schema_version="nasdaq-directory-v1",
        test_issue_token="N",
        financial_status_token="N",
        etf_token="N",
    )


def registry(symbol="AAPL", observed_at=NOW):
    source = payload(ObservationSourceKindV1.SEC_REGISTRY, "company_tickers_exchange")
    return SecRegistryObservationV1(
        symbol=symbol,
        cik="0000320193",
        exchange_token="Nasdaq",
        name_hash="c" * 64,
        observed_at=observed_at,
        body_sha256=source.payload_sha256,
        row_sha256="d" * 64,
        source_schema_version="sec-registry-v1",
    )


def issuer(symbol="AAPL", observed_at=NOW):
    source = payload(ObservationSourceKindV1.SEC_ISSUER, symbol)
    return SecIssuerObservationV1(
        cik="0000320193",
        symbol=symbol,
        exchange_token="NASDAQ",
        sic_code="3571",
        observed_at=observed_at,
        body_sha256=source.payload_sha256,
        source_schema_version="sec-submissions-v1",
    )


def sic_snapshot():
    return OfficialSicSnapshotV1(
        snapshot_version="sec-sic-html-v1",
        classification="RECONSTRUCTED_CONTRACT",
        codes=("3571", "6000", "6799"),
        documentation_ref="fixture-sec-sic-html-v1",
    )


def acquisition(acquisition_id="acq-20260813-0001", symbol="AAPL"):
    return OfficialMembershipAcquisitionV1(
        acquisition_id=acquisition_id,
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        security_policy_version="official-security-name-v1",
        source_contract_version="official-membership-source-v1",
        sic_snapshot=sic_snapshot(),
        listings=(listing(symbol),),
        registry_rows=(registry(symbol),),
        issuer_rows=(issuer(symbol),),
    )


def material(acquisition_id="acq-20260813-0001", symbol="AAPL"):
    listing_value = listing(symbol)
    registry_value = registry(symbol)
    issuer_value = issuer(symbol)
    return MembershipMaterialV1(
        symbol=symbol,
        cik="0000320193",
        venue=ListingVenueV1.NASDAQ,
        sector=MembershipSectorV1.NON_FINANCIALS,
        listed_at_as_of=True,
        membership_as_of=date(2026, 8, 12),
        acquisition_id=acquisition_id,
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        membership_holdings_digest="f" * 64,
        security_policy_version="official-security-name-v1",
        sic_snapshot_version="sec-sic-html-v1",
        sic_code="3571",
        observation_instants=(
            ObservationInstantV1(
                ObservationSourceKindV1.NASDAQ_LISTED,
                listing_value.observed_at,
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_REGISTRY,
                registry_value.observed_at,
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_ISSUER,
                issuer_value.observed_at,
            ),
        ),
        listing_source_ref=listing_value.source_ref,
        registry_source_ref=registry_value.source_ref,
        issuer_source_ref=issuer_value.source_ref,
        sic_snapshot_ref=sic_snapshot().source_ref,
    )


def payloads(acquisition_id="acq-20260813-0001", symbol="AAPL"):
    return (
        replace(
            payload(ObservationSourceKindV1.NASDAQ_LISTED, "nasdaqlisted"),
            acquisition_id=acquisition_id,
        ),
        replace(
            payload(
                ObservationSourceKindV1.SEC_REGISTRY,
                "company_tickers_exchange",
            ),
            acquisition_id=acquisition_id,
        ),
        replace(
            payload(ObservationSourceKindV1.SEC_ISSUER, symbol),
            acquisition_id=acquisition_id,
        ),
    )


def test_registered_family_append_read_and_second_consumer_round_trip(tmp_path):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)

    receipt = store.append_success(
        acquisition=acquisition(),
        payloads=payloads(),
        materials=(material(),),
    )
    second_consumer = OfficialMembershipObservationStore(path)

    assert receipt.status == "SAVED"
    assert store.read_acquisition("acq-20260813-0001") == acquisition()
    assert second_consumer.read_material("acq-20260813-0001", "AAPL") == material()
    assert tuple(
        item.symbol for item in second_consumer.read_materials("acq-20260813-0001")
    ) == ("AAPL",)
    assert not any(
        Path(f"{path}{suffix}").exists() for suffix in ("-journal", "-wal", "-shm")
    )


def test_unregistered_family_refuses_construction(tmp_path):
    with pytest.raises(ValueError, match="canonical_observation_family"):
        OfficialMembershipObservationStore(
            tmp_path / "official-membership" / "observations.sqlite3",
            family_name="BadFamilyV1",
        )


def test_identical_replay_is_duplicate_but_changed_payload_is_conflict(tmp_path):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)
    first = store.append_success(
        acquisition=acquisition(),
        payloads=payloads(),
        materials=(material(),),
    )
    duplicate = store.append_success(
        acquisition=acquisition(),
        payloads=payloads(),
        materials=(material(),),
    )
    conflicting_payloads = list(payloads())
    conflicting_payloads[-1] = replace(
        conflicting_payloads[-1],
        payload_bytes=b"changed-fixture-bytes",
    )
    conflict = store.append_success(
        acquisition=acquisition(),
        payloads=tuple(conflicting_payloads),
        materials=(material(),),
    )

    assert first.status == "SAVED"
    assert duplicate.status == "DUPLICATE"
    assert conflict.status == "CONFLICT"
    assert store.read_material("acq-20260813-0001", "AAPL") == material()


def test_failure_receipt_modes_and_exact_read_only(tmp_path):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)

    receipt = store.append_failure(
        acquisition=acquisition("acq-20260813-0002", "MSFT"),
        payloads=payloads("acq-20260813-0002", "MSFT"),
        reason="partial_acquisition",
        detail="missing_sec_issuer",
    )

    assert receipt.status == "SAVED"
    assert receipt.reason == "partial_acquisition"
    assert store.read_material("acq-20260813-0002", "MSFT") is None
    assert store.read_material("missing", "MSFT") is None
    assert store.read_receipt("acq-20260813-0002").reason == "partial_acquisition"
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_reads_refuse_unsafe_private_path(tmp_path):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)
    store.append_success(
        acquisition=acquisition(),
        payloads=payloads(),
        materials=(material(),),
    )

    path.chmod(0o644)
    with pytest.raises(ValueError, match="private_path"):
        store.read_material("acq-20260813-0001", "AAPL")


def test_materials_remain_readable_but_legacy_metadata_is_not_writable(tmp_path):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)
    store.append_success(
        acquisition=acquisition(),
        payloads=payloads(),
        materials=(material(),),
    )

    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE storage_metadata SET value='0' "
            "WHERE key='official_membership_schema_version'"
        )
        connection.commit()
    finally:
        connection.close()

    reader = OfficialMembershipObservationStore(path)
    assert reader.read_material("acq-20260813-0001", "AAPL") == material()
    with pytest.raises(ValueError, match="schema_mismatch"):
        reader.append_success(
            acquisition=acquisition("acq-20260813-0003", "MSFT"),
            payloads=payloads("acq-20260813-0003", "MSFT"),
            materials=(material("acq-20260813-0003", "MSFT"),),
        )


def test_write_failure_rolls_back_without_partial_rows(tmp_path, monkeypatch):
    path = tmp_path / "official-membership" / "observations.sqlite3"
    store = OfficialMembershipObservationStore(path)
    store.append_failure(
        acquisition=acquisition("seed-acq", "MSFT"),
        payloads=payloads("seed-acq", "MSFT"),
        reason="partial_acquisition",
        detail="missing_sec_issuer",
    )

    def crash_sentinel(_path):
        raise OSError("crash_sentinel")

    monkeypatch.setattr(
        membership_store_module,
        "assert_private_sidecars_while_open",
        crash_sentinel,
    )
    with pytest.raises(OSError, match="crash_sentinel"):
        store.append_success(
            acquisition=acquisition(),
            payloads=payloads(),
            materials=(material(),),
        )

    verifier = OfficialMembershipObservationStore(path)
    assert verifier.read_acquisition("acq-20260813-0001") is None
    assert verifier.read_material("acq-20260813-0001", "AAPL") is None
    assert verifier.read_materials("acq-20260813-0001") == ()
    assert verifier.read_receipt("acq-20260813-0001") is None
    assert verifier.read_receipt("seed-acq").reason == "partial_acquisition"
