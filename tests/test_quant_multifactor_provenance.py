from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime

import pytest

from tokmo.values.official_membership_observation import (
    ObservationInstantV1,
    ObservationSourceKindV1,
)
from tokmo.values.quant_multifactor_core import MarketV1, SectorV1
from tokmo.values.quant_multifactor_provenance import (
    ASSEMBLY_PROVENANCE_VERSION,
    AssemblyInputV1,
    CandleRequestIdentityV1,
    MemberSelection,
    MembershipProofV1,
)


def valid_instants():
    return (
        ObservationInstantV1(
            ObservationSourceKindV1.NASDAQ_LISTED,
            datetime(2026, 8, 11, 19, 55, tzinfo=UTC),
        ),
        ObservationInstantV1(
            ObservationSourceKindV1.SEC_REGISTRY,
            datetime(2026, 8, 11, 19, 56, tzinfo=UTC),
        ),
        ObservationInstantV1(
            ObservationSourceKindV1.SEC_ISSUER,
            datetime(2026, 8, 11, 19, 57, tzinfo=UTC),
        ),
    )


def test_frozen_bounded_membership_proof_and_g1b_reason_vocabulary():
    proof = MembershipProofV1(
        "AAPL",
        MarketV1.US_NYSE_NASDAQ,
        True,
        date(2026, 8, 11),
        SectorV1.NON_FINANCIALS,
        "f" * 64,
        valid_instants(),
        "qmemv2:sha256:" + "a" * 64,
    )
    with pytest.raises(FrozenInstanceError):
        proof.symbol = "MSFT"
    assert ASSEMBLY_PROVENANCE_VERSION == "quant-multifactor-assembly-provenance-v2"
    assert proof.membership_holdings_digest == "f" * 64
    assert (
        proof.observation_instants[0].source_kind
        is ObservationSourceKindV1.NASDAQ_LISTED
    )
    assert MemberSelection.MEMBER_UNAVAILABLE.value == "MEMBER_UNAVAILABLE"
    assert (
        MemberSelection.AMBIGUOUS_METRIC_PRESERVED.value
        != MemberSelection.UNAVAILABLE_METRIC_PRESERVED.value
    )


def test_page_identity_is_bounded_and_actual_before_is_bound():
    identity = CandleRequestIdentityV1(
        "qt",
        "AAPL",
        MarketV1.US_NYSE_NASDAQ,
        "1d",
        200,
        False,
        2,
        datetime(2026, 1, 2, tzinfo=UTC),
    )
    assert identity.canonical_ref.startswith("qcandlev1:sha256:")
    assert len(identity.canonical_ref) < 128


def test_membership_proof_rejects_qmemv1_prefix():
    with pytest.raises(ValueError, match="membership_source_ref"):
        MembershipProofV1(
            "AAPL",
            MarketV1.US_NYSE_NASDAQ,
            True,
            date(2026, 8, 11),
            SectorV1.NON_FINANCIALS,
            "f" * 64,
            valid_instants(),
            "qmemv1:sha256:" + "a" * 64,
        )


def test_membership_proof_rejects_invalid_membership_digest():
    with pytest.raises(ValueError, match="membership_holdings_digest"):
        MembershipProofV1(
            "AAPL",
            MarketV1.US_NYSE_NASDAQ,
            True,
            date(2026, 8, 11),
            SectorV1.NON_FINANCIALS,
            "bad",
            valid_instants(),
            "qmemv2:sha256:" + "a" * 64,
        )


def test_membership_proof_rejects_duplicate_source_kinds():
    with pytest.raises(ValueError, match="membership_observation_instants"):
        MembershipProofV1(
            "AAPL",
            MarketV1.US_NYSE_NASDAQ,
            True,
            date(2026, 8, 11),
            SectorV1.NON_FINANCIALS,
            "f" * 64,
            (
                ObservationInstantV1(
                    ObservationSourceKindV1.NASDAQ_LISTED,
                    datetime(2026, 8, 11, 19, 55, tzinfo=UTC),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.NASDAQ_LISTED,
                    datetime(2026, 8, 11, 19, 56, tzinfo=UTC),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.SEC_ISSUER,
                    datetime(2026, 8, 11, 19, 57, tzinfo=UTC),
                ),
            ),
            "qmemv2:sha256:" + "a" * 64,
        )


def test_membership_proof_rejects_missing_required_source_kinds():
    with pytest.raises(ValueError, match="membership_observation_instants"):
        MembershipProofV1(
            "AAPL",
            MarketV1.US_NYSE_NASDAQ,
            True,
            date(2026, 8, 11),
            SectorV1.NON_FINANCIALS,
            "f" * 64,
            (
                ObservationInstantV1(
                    ObservationSourceKindV1.OTHER_LISTED,
                    datetime(2026, 8, 11, 19, 55, tzinfo=UTC),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.NASDAQ_LISTED,
                    datetime(2026, 8, 11, 19, 56, tzinfo=UTC),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.SEC_ISSUER,
                    datetime(2026, 8, 11, 19, 57, tzinfo=UTC),
                ),
            ),
            "qmemv2:sha256:" + "a" * 64,
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"lane": "mr"},
        {"page_index": 3},
        {"page_index": 1, "before": datetime(2026, 1, 2, tzinfo=UTC)},
        {"page_index": 2, "before": None},
        {"page_index": 2, "before": datetime(2026, 1, 2)},
        {"symbol": "삼성"},
    ),
)
def test_page_identity_rejects_lane_page_before_and_non_ascii(changes):
    values = dict(
        lane="qt",
        symbol="AAPL",
        market=MarketV1.US_NYSE_NASDAQ,
        interval="1d",
        count=200,
        adjusted=False,
        page_index=1,
        before=None,
    )
    values.update(changes)
    with pytest.raises(ValueError):
        CandleRequestIdentityV1(**values)


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("universe_id", ""),
        ("universe_id", "bad value"),
        ("universe_id", "u" * 65),
        ("universe_policy_version", ""),
        ("universe_policy_version", "p" * 33),
        ("adjustment_version", "bad value"),
        ("adjustment_version", "a" * 33),
    ),
)
def test_assembly_input_rejects_invalid_core_scope_strings_as_caller_contract(
    field, value
):
    from unittest.mock import Mock

    values = dict(
        holdings=Mock(),
        as_of=datetime(2026, 8, 11, tzinfo=UTC),
        members=(),
        universe_id="us-pilot-8-v1",
        universe_policy_version="universe-v1",
        adjustment_version="raw-v1",
    )
    values[field] = value
    with pytest.raises(ValueError, match=field):
        AssemblyInputV1(**values)
