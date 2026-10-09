from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256

import pytest

from tokmo.facts.quant_membership import (
    parse_nasdaqlisted,
    parse_otherlisted,
    parse_sec_issuer,
    parse_sec_registry,
)
from tokmo.programs.quant_membership import (
    assemble_us_personal_membership_material,
    describe_us_personal_membership_cohort,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.official_membership_observation import (
    ListingObservationV1,
    ListingVenueV1,
    MembershipPolicyV1,
    MembershipSectorV1,
    MembershipUnavailableReasonV1,
    ObservationSourceKindV1,
    OfficialMembershipAcquisitionV1,
    OfficialSicSnapshotV1,
    SecIssuerObservationV1,
    SecRegistryObservationV1,
    SecurityClassV1,
)

AS_OF = datetime(2026, 8, 13, 1, 30, tzinfo=UTC)


def holdings(symbols, *, captured_at=AS_OF, markets=None, scopes=None):
    markets = markets or {symbol: "US" for symbol in symbols}
    scopes = scopes or ("toss-acct-" + "1" * 24,)
    accounts = []
    identity_map = {}
    for ordinal, scope in enumerate(scopes, start=1):
        items = tuple(
            HoldingItemV1(symbol, "1", "1", "USD", markets[symbol], None)
            for symbol in symbols
        )
        accounts.append(AccountHoldingsV1(scope, ordinal, items))
        identity_map[scope] = AccountScopeIdentityV1(scope, "personal", ordinal, None)
    return CurrentHoldingsV1(
        1,
        tuple(accounts),
        True,
        captured_at,
        identity_map,
    )


def listing(
    symbol,
    *,
    venue=ListingVenueV1.NASDAQ,
    observed_at=AS_OF - timedelta(minutes=5),
    security_class=SecurityClassV1.COMMON_EQUITY,
    test_issue="N",
    status="N",
    etf="N",
):
    return ListingObservationV1(
        symbol=symbol,
        source_kind=(
            ObservationSourceKindV1.NASDAQ_LISTED
            if venue is ListingVenueV1.NASDAQ
            else ObservationSourceKindV1.OTHER_LISTED
        ),
        venue=venue,
        venue_token="Q" if venue is ListingVenueV1.NASDAQ else "N",
        security_name_hash="a" * 64,
        security_class=security_class,
        observed_at=observed_at,
        directory_creation_time=observed_at,
        body_sha256="b" * 64,
        row_sha256=sha256(symbol.encode("ascii")).hexdigest(),
        source_schema_version="listing-v1",
        test_issue_token=test_issue,
        financial_status_token=status if venue is ListingVenueV1.NASDAQ else "NA",
        etf_token=etf,
    )


def registry(
    symbol,
    *,
    exchange="NASDAQ",
    cik="0000320193",
    observed_at=AS_OF - timedelta(minutes=4),
):
    return SecRegistryObservationV1(
        symbol=symbol,
        cik=cik,
        exchange_token=exchange,
        name_hash="c" * 64,
        observed_at=observed_at,
        body_sha256="d" * 64,
        row_sha256=sha256(f"registry:{symbol}".encode("ascii")).hexdigest(),
        source_schema_version="sec-registry-v1",
    )


def issuer(
    symbol,
    *,
    sic="3571",
    exchange="NASDAQ",
    cik="0000320193",
    observed_at=AS_OF - timedelta(minutes=3),
):
    return SecIssuerObservationV1(
        cik=cik,
        symbol=symbol,
        exchange_token=exchange,
        sic_code=sic,
        observed_at=observed_at,
        body_sha256=sha256(f"issuer:{symbol}:{sic}:{cik}".encode("ascii")).hexdigest(),
        source_schema_version="sec-submissions-v1",
    )


def acquisition(
    symbols,
    *,
    sic_codes=("3571", "5990", "6000", "6799", "7000"),
    listings=None,
    registry_rows=None,
    issuer_rows=None,
):
    return OfficialMembershipAcquisitionV1(
        acquisition_id="acq-20260813-0001",
        membership_policy=MembershipPolicyV1.OBSERVED_CURRENT_LISTING_V1,
        security_policy_version="official-security-name-v1",
        source_contract_version="official-membership-source-v1",
        sic_snapshot=OfficialSicSnapshotV1(
            snapshot_version="sec-sic-html-v1",
            classification="RECONSTRUCTED_CONTRACT",
            codes=tuple(sic_codes),
            documentation_ref="fixture-sec-sic-html-v1",
        ),
        listings=tuple(
            listings
            if listings is not None
            else (
                listing(
                    symbol,
                    venue=(
                        ListingVenueV1.NASDAQ
                        if symbol != "IBM"
                        else ListingVenueV1.NYSE
                    ),
                )
                for symbol in symbols
            )
        ),
        registry_rows=tuple(
            registry_rows
            if registry_rows is not None
            else (
                registry(symbol, exchange="NASDAQ" if symbol != "IBM" else "NYSE")
                for symbol in symbols
            )
        ),
        issuer_rows=tuple(
            issuer_rows
            if issuer_rows is not None
            else (
                issuer(
                    symbol,
                    sic="6000" if symbol == "JPM" else "3571",
                    exchange="NYSE" if symbol == "IBM" else "NASDAQ",
                )
                for symbol in symbols
            )
        ),
    )


def test_variable_exact_cohort_cardinality_and_deduped_personal_scope_succeed():
    scope_1 = "toss-acct-" + "1" * 24
    scope_2 = "toss-acct-" + "2" * 24
    value = holdings(("AAPL", "IBM", "JPM"), scopes=(scope_1, scope_2))
    result = assemble_us_personal_membership_material(
        holdings=value,
        evaluation_as_of=AS_OF,
        acquisition=acquisition(("AAPL", "IBM", "JPM")),
    )

    assert tuple(material.symbol for material in result.materials) == (
        "AAPL",
        "IBM",
        "JPM",
    )
    assert result.unavailable_reasons == ()
    assert len(result.proofs) == 3
    assert {proof.membership_holdings_digest for proof in result.proofs} == {
        sha256(result.holdings_bytes).hexdigest()
    }


def test_real_parsers_feed_mixed_nasdaq_nyse_assembler_with_canonical_exchange_tokens():
    import json
    from pathlib import Path

    fixtures = Path(__file__).parent / "fixtures" / "quant_membership"
    parsed_listings = (
        *parse_nasdaqlisted(
            (fixtures / "nasdaqlisted_sample.txt").read_bytes(),
            observed_at=AS_OF - timedelta(minutes=5),
            expected_symbols=("AAPL", "IBM"),
        ),
        *parse_otherlisted(
            (fixtures / "otherlisted_sample.txt").read_bytes(),
            observed_at=AS_OF - timedelta(minutes=5),
            expected_symbols=("AAPL", "IBM"),
        ),
    )
    registry_body = {
        "fields": ["cik", "name", "ticker", "exchange"],
        "data": [[320193, "Apple", "AAPL", "Nasdaq"], [51143, "IBM", "IBM", "NYSE"]],
    }
    parsed_registry = parse_sec_registry(
        json.dumps(registry_body).encode(),
        observed_at=AS_OF - timedelta(minutes=4),
        expected_symbols=("AAPL", "IBM"),
    )
    issuers = tuple(
        parse_sec_issuer(
            json.dumps(
                {
                    "cik": cik,
                    "sic": "3571",
                    "tickers": [symbol],
                    "exchanges": [exchange],
                }
            ).encode(),
            observed_at=AS_OF - timedelta(minutes=3),
            expected_symbol=symbol,
            expected_cik=normalized,
            expected_exchange=canonical,
        )
        for symbol, cik, normalized, exchange, canonical in (
            ("AAPL", 320193, "0000320193", "Nasdaq", "NASDAQ"),
            ("IBM", 51143, "0000051143", "NYSE", "NYSE"),
        )
    )
    result = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL", "IBM")),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL", "IBM"),
            listings=parsed_listings,
            registry_rows=parsed_registry,
            issuer_rows=issuers,
        ),
    )
    assert tuple(item.symbol for item in result.materials) == ("AAPL", "IBM")
    assert result.unavailable_reasons == ()


def test_registry_absence_and_contradiction_are_distinct_fail_closed_paths():
    absent = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(("AAPL",), registry_rows=()),
    )
    contradiction = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            registry_rows=(registry("AAPL", exchange="NYSE"),),
        ),
    )

    assert absent.proofs == ()
    assert absent.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.SUPPORTING_REGISTRY_ABSENT),
    )
    assert contradiction.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.REGISTRY_CONTRADICTION),
    )


def test_future_stale_skew_and_conflicting_market_holdings_fail_closed():
    boundary = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", observed_at=AS_OF - timedelta(minutes=30)),),
            registry_rows=(
                registry("AAPL", observed_at=AS_OF - timedelta(minutes=25)),
            ),
            issuer_rows=(issuer("AAPL", observed_at=AS_OF - timedelta(minutes=20)),),
        ),
    )
    future = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", observed_at=AS_OF + timedelta(seconds=1)),),
        ),
    )
    stale = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", observed_at=AS_OF - timedelta(minutes=31)),),
        ),
    )
    skew = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            issuer_rows=(issuer("AAPL", observed_at=AS_OF - timedelta(minutes=16)),),
        ),
    )
    conflict_holdings = holdings(
        ("AAPL",),
        scopes=("toss-acct-" + "1" * 24, "toss-acct-" + "2" * 24),
        markets={"AAPL": "US"},
    )
    object.__setattr__(
        conflict_holdings.accounts[1].holdings[0],
        "market_raw",
        "KR",
    )
    conflict = assemble_us_personal_membership_material(
        holdings=conflict_holdings,
        evaluation_as_of=AS_OF,
        acquisition=acquisition(("AAPL",)),
    )

    assert boundary.proofs and boundary.unavailable_reasons == ()
    assert future.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.FUTURE_OBSERVATION),
    )
    assert stale.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.STALE_OBSERVATION),
    )
    assert skew.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.SOURCE_SKEW),
    )
    assert conflict.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.IDENTITY_CONFLICT),
    )


def test_listing_type_and_status_gates_fail_closed():
    unsupported_security = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", security_class=SecurityClassV1.UNSUPPORTED),),
        ),
    )
    etf = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", etf="Y"),),
        ),
    )
    test_issue = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", test_issue="Y"),),
        ),
    )
    delinquent = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL", status="D"),),
        ),
    )

    assert unsupported_security.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.UNSUPPORTED_TYPE),
    )
    assert etf.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.UNSUPPORTED_TYPE),
    )
    assert test_issue.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.UNSUPPORTED_STATUS),
    )
    assert delinquent.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.UNSUPPORTED_STATUS),
    )


def test_registry_and_issuer_cik_mismatch_is_identity_conflict():
    result = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            issuer_rows=(issuer("AAPL", cik="0000051143"),),
        ),
    )

    assert result.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.IDENTITY_CONFLICT),
    )


def test_duplicate_and_extra_supplied_rows_fail_whole_cohort():
    duplicate_listing = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            listings=(listing("AAPL"), listing("AAPL")),
        ),
    )
    extra_registry = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL",),
            registry_rows=(registry("AAPL"), registry("MSFT")),
        ),
    )

    assert duplicate_listing.materials == ()
    assert duplicate_listing.proofs == ()
    assert duplicate_listing.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.IDENTITY_CONFLICT),
    )
    assert extra_registry.unavailable_reasons == (
        ("MSFT", MembershipUnavailableReasonV1.COHORT_MISMATCH),
    )


def test_observation_market_date_must_match_evaluation_market_date():
    boundary_as_of = datetime(2026, 8, 13, 4, 10, tzinfo=UTC)
    result = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",), captured_at=boundary_as_of),
        evaluation_as_of=boundary_as_of,
        acquisition=acquisition(
            ("AAPL",),
            listings=(
                listing("AAPL", observed_at=datetime(2026, 8, 13, 3, 50, tzinfo=UTC)),
            ),
            registry_rows=(
                registry("AAPL", observed_at=datetime(2026, 8, 13, 3, 51, tzinfo=UTC)),
            ),
            issuer_rows=(
                issuer("AAPL", observed_at=datetime(2026, 8, 13, 3, 52, tzinfo=UTC)),
            ),
        ),
    )

    assert result.unavailable_reasons == (
        ("AAPL", MembershipUnavailableReasonV1.SOURCE_SKEW),
    )


@pytest.mark.parametrize(
    ("instant", "expected_market_date"),
    (
        (datetime(2026, 3, 8, 6, 59, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 3, 8, 7, 0, tzinfo=UTC), date(2026, 3, 8)),
        (datetime(2026, 11, 1, 5, 30, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 11, 1, 6, 30, tzinfo=UTC), date(2026, 11, 1)),
        (datetime(2026, 8, 14, 3, 59, tzinfo=UTC), date(2026, 8, 13)),
        (datetime(2026, 8, 14, 4, 0, tzinfo=UTC), date(2026, 8, 14)),
        # Before the 2007 US DST rule change, IANA history differs from a
        # hand-coded modern rule: 23:30 EST is still the prior market date.
        (datetime(2005, 11, 1, 4, 30, tzinfo=UTC), date(2005, 10, 31)),
    ),
)
def test_membership_as_of_uses_canonical_us_market_timezone(
    instant, expected_market_date
):
    binding = describe_us_personal_membership_cohort(
        holdings(("AAPL",), captured_at=instant),
        instant,
    )
    assert binding.membership_as_of == expected_market_date


def test_cohort_binding_rejects_unknown_scope_market_unknown_and_captured_at_mismatch():
    unknown_scope = holdings(("AAPL",))
    object.__setattr__(unknown_scope.accounts[0], "account_scope", "brokerage-plain")
    with pytest.raises(ValueError, match="account_scope"):
        describe_us_personal_membership_cohort(unknown_scope, AS_OF)

    unknown_market = holdings(("AAPL",))
    object.__setattr__(unknown_market.accounts[0].holdings[0], "market_raw", None)
    with pytest.raises(ValueError, match="market_unknown"):
        describe_us_personal_membership_cohort(unknown_market, AS_OF)

    earlier = describe_us_personal_membership_cohort(
        holdings(("AAPL",), captured_at=AS_OF - timedelta(minutes=30)), AS_OF
    )
    assert earlier.expected_symbols == ("AAPL",)
    with pytest.raises(ValueError, match="holdings_stale"):
        describe_us_personal_membership_cohort(
            holdings(("AAPL",), captured_at=AS_OF - timedelta(minutes=30, seconds=1)),
            AS_OF,
        )
    with pytest.raises(ValueError, match="holdings_future"):
        describe_us_personal_membership_cohort(
            holdings(("AAPL",), captured_at=AS_OF + timedelta(seconds=1)), AS_OF
        )


def test_kr_holdings_are_excluded_without_failing_us_cohort():
    binding = describe_us_personal_membership_cohort(
        holdings(
            ("AAPL", "005930"),
            markets={"AAPL": "US", "005930": "KR"},
        ),
        AS_OF,
    )

    assert binding.expected_symbols == ("AAPL",)


def test_sic_membership_precedes_sector_mapping_and_digest_changes_with_policy_inputs():
    boundary = assemble_us_personal_membership_material(
        holdings=holdings(("JPM", "BAC", "MMM", "GE")),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("JPM", "BAC", "MMM", "GE"),
            issuer_rows=(
                issuer("JPM", sic="6000"),
                issuer("BAC", sic="6799"),
                issuer("MMM", sic="5990"),
                issuer("GE", sic="7000"),
            ),
        ),
    )
    gap_codes = assemble_us_personal_membership_material(
        holdings=holdings(("GE", "MMM")),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("GE", "MMM"),
            issuer_rows=(
                issuer("GE", sic="6800"),
                issuer("MMM", sic="5999"),
            ),
        ),
    )
    changed_policy = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL",)),
        evaluation_as_of=AS_OF,
        acquisition=replace(
            acquisition(("AAPL",)),
            security_policy_version="official-security-name-v2",
        ),
    )

    assert {material.symbol: material.sector for material in boundary.materials} == {
        "JPM": MembershipSectorV1.FINANCIALS,
        "BAC": MembershipSectorV1.FINANCIALS,
        "MMM": MembershipSectorV1.NON_FINANCIALS,
        "GE": MembershipSectorV1.NON_FINANCIALS,
    }
    assert gap_codes.unavailable_reasons == (
        ("GE", MembershipUnavailableReasonV1.MISSING_SIC),
        ("MMM", MembershipUnavailableReasonV1.MISSING_SIC),
    )
    assert (
        changed_policy.proofs[0].source_ref
        != assemble_us_personal_membership_material(
            holdings=holdings(("AAPL",)),
            evaluation_as_of=AS_OF,
            acquisition=acquisition(("AAPL",)),
        )
        .proofs[0]
        .source_ref
    )


def test_proof_digest_is_permutation_invariant_but_changes_with_source_hash_mutations():
    base_acquisition = acquisition(("AAPL", "IBM"))
    ordered = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL", "IBM")),
        evaluation_as_of=AS_OF,
        acquisition=base_acquisition,
    )
    permuted = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL", "IBM")),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL", "IBM"),
            listings=tuple(reversed(base_acquisition.listings)),
            registry_rows=tuple(reversed(base_acquisition.registry_rows)),
            issuer_rows=tuple(reversed(base_acquisition.issuer_rows)),
        ),
    )
    mutated = assemble_us_personal_membership_material(
        holdings=holdings(("AAPL", "IBM")),
        evaluation_as_of=AS_OF,
        acquisition=acquisition(
            ("AAPL", "IBM"),
            listings=(
                replace(listing("AAPL"), row_sha256="f" * 64),
                listing("IBM", venue=ListingVenueV1.NYSE),
            ),
            registry_rows=(registry("AAPL"), registry("IBM", exchange="NYSE")),
            issuer_rows=(issuer("AAPL"), issuer("IBM", exchange="NYSE")),
        ),
    )

    assert ordered.proofs == permuted.proofs
    assert ordered.proofs[0].source_ref != mutated.proofs[0].source_ref
