from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
from zoneinfo import ZoneInfo

from tokmo.programs.quant_multifactor_core import (
    evaluate_quant_multifactor_core_v1,
    market_utc_offset,
    validate_input,
)
from tokmo.programs.quant_multifactor_inputs import (
    _provenance_holdings_digest,
    assemble_quant_multifactor_input,
    page2_request_identity,
    requires_page2,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
    canonical_holdings_bytes,
)
from tokmo.values.fundamental_materials import (
    AccountingRegime,
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
    StatementScope,
)
from tokmo.values.official_membership_observation import (
    ObservationInstantV1,
    ObservationSourceKindV1,
)
from tokmo.values.quant_multifactor_core import AdjustmentV1, MarketV1, SectorV1
from tokmo.values.quant_multifactor_provenance import (
    ASSEMBLY_PROVENANCE_VERSION,
    AssemblyInputV1,
    CandleRequestIdentityV1,
    MemberAssemblyInputV1,
    MembershipProofV1,
    RawCandlePageV1,
    RawCandleV1,
)

AS_OF = datetime(2026, 8, 11, 20, tzinfo=UTC)


def holdings(
    symbols=tuple(f"S{i}" for i in range(8)),
    captured_at=AS_OF,
    scope="toss-acct-" + "0" * 24,
):
    items = tuple(HoldingItemV1(s, "1", "1", "USD", "US", None) for s in symbols)
    account = AccountHoldingsV1(scope, 1, items)
    identity = AccountScopeIdentityV1(scope, "personal", 1, None)
    return CurrentHoldingsV1(1, (account,), True, captured_at, {scope: identity})


def material(
    symbol,
    year=2025,
    period_end=date(2025, 12, 31),
    selection=MetricSelection.EXACT,
    currency="USD",
):
    metric = MetricEvidence(
        Decimal(100) if selection is MetricSelection.EXACT else None,
        "x" if selection is MetricSelection.EXACT else None,
        currency,
        selection,
    )
    shares = MetricEvidence(Decimal(10), "shares", "shares", MetricSelection.EXACT)
    return OfficialFundamentalMaterialV1(
        "sec",
        symbol,
        symbol,
        "US_NASDAQ",
        AccountingRegime.US_GAAP,
        StatementScope.CONSOLIDATED,
        year,
        None,
        period_end,
        f"f-{symbol}-{year}",
        "2026-01-01",
        datetime(2026, 1, 2, tzinfo=UTC),
        None,
        "us-gaap",
        "2025",
        "v1",
        metric,
        metric,
        metric,
        metric,
        metric,
        shares,
        "a",
        "b",
        datetime(2026, 1, 1, tzinfo=UTC),
        AS_OF,
    )


def page(symbol, count=200, invalid=False, start=date(2026, 8, 11)):
    rows = []
    day = start
    while len(rows) < count:
        if day.weekday() < 5:
            rows.append(
                RawCandleV1(
                    datetime.combine(day, datetime.min.time(), UTC),
                    Decimal(10),
                    "USD" if not invalid or len(rows) != 5 else "EUR",
                )
            )
        day -= timedelta(days=1)
    ident = CandleRequestIdentityV1(
        "qt", symbol, MarketV1.US_NYSE_NASDAQ, "1d", 200, False, 1, None
    )
    return RawCandlePageV1(ident, tuple(rows), count)


def member(
    symbol,
    proof=True,
    selection=MetricSelection.EXACT,
    currency="USD",
    count=137,
    invalid=False,
):
    p = (
        MembershipProofV1(
            symbol,
            MarketV1.US_NYSE_NASDAQ,
            True,
            date(2026, 8, 11),
            SectorV1.NON_FINANCIALS,
            "0" * 64,
            (
                ObservationInstantV1(
                    ObservationSourceKindV1.NASDAQ_LISTED,
                    AS_OF - timedelta(minutes=5),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.SEC_REGISTRY,
                    AS_OF - timedelta(minutes=4),
                ),
                ObservationInstantV1(
                    ObservationSourceKindV1.SEC_ISSUER,
                    AS_OF - timedelta(minutes=3),
                ),
            ),
            "qmemv2:sha256:" + sha256(symbol.encode()).hexdigest(),
        )
        if proof
        else None
    )
    return MemberAssemblyInputV1(
        p,
        (
            material(symbol, selection=selection, currency=currency),
            material(symbol, 2024, date(2024, 12, 31), currency=currency),
        ),
        page(symbol, count, invalid),
        None,
    )


def assembly(members):
    holdings_value = holdings(tuple(sorted(m.proof.symbol for m in members if m.proof)))
    holdings_digest = sha256(canonical_holdings_bytes(holdings_value)).hexdigest()
    normalized = []
    for item in members:
        if item.proof is None:
            normalized.append(item)
            continue
        proof = item.proof
        if proof.membership_holdings_digest == "0" * 64:
            proof = replace(proof, membership_holdings_digest=holdings_digest)
        normalized.append(
            replace(
                item,
                proof=proof,
            )
        )
    return AssemblyInputV1(
        holdings_value,
        AS_OF,
        tuple(normalized),
        "us-pilot-8-v1",
        "universe-v1",
        "raw-v1",
    )


def test_qb01_qb02_missing_proof_and_bad_holdings_fail_closed():
    members = [member(f"S{i}") for i in range(8)]
    members[0] = member("S0", proof=False)
    assert (
        assemble_quant_multifactor_input(
            AssemblyInputV1(
                holdings(),
                AS_OF,
                tuple(members),
                "us-pilot-8-v1",
                "universe-v1",
                "raw-v1",
            )
        ).cohort_input
        is None
    )
    stale = replace(
        assembly(tuple(member(f"S{i}") for i in range(8))),
        holdings=holdings(captured_at=AS_OF - timedelta(seconds=1)),
    )
    assert assemble_quant_multifactor_input(stale).cohort_input is None
    pension = replace(
        assembly(tuple(member(f"S{i}") for i in range(8))),
        holdings=holdings(scope="pension-excel-" + "0" * 24),
    )
    assert assemble_quant_multifactor_input(pension).cohort_input is None
    empty = AssemblyInputV1(
        holdings(symbols=(), scope="pension-excel-" + "0" * 24),
        AS_OF,
        (),
        "us-pilot-8-v1",
        "universe-v1",
        "raw-v1",
    )
    outcome = assemble_quant_multifactor_input(empty)
    assert outcome.cohort_input is None
    assert any(
        reason.selection.value == "COHORT_UNAVAILABLE"
        and reason.detail == "empty_cohort"
        for reason in outcome.reasons
    )


def test_qb05_selection_currency_and_published_at_preserved():
    members = [member(f"S{i}") for i in range(8)]
    members[0] = member("S0", selection=MetricSelection.AMBIGUOUS)
    members[1] = member("S1", selection=MetricSelection.UNAVAILABLE)
    outcome = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert "ambiguous" in repr(outcome.reasons) and "unavailable" in repr(
        outcome.reasons
    )
    mixed = [member(f"S{i}") for i in range(8)]
    mixed[0] = member("S0", currency="EUR")
    assert assemble_quant_multifactor_input(assembly(tuple(mixed))).cohort_input is None
    good = assemble_quant_multifactor_input(
        assembly(tuple(member(f"S{i}") for i in range(8)))
    ).cohort_input
    assert good.members[0].current_fundamentals.published_at == datetime(
        2026, 1, 2, tzinfo=UTC
    )


def test_current_prior_and_mixed_material_currency_fail_typed():
    members = [member(f"S{i}") for i in range(8)]
    prior = material("S0", 2024, date(2024, 12, 31), currency="EUR")
    members[0] = replace(members[0], materials=(members[0].materials[0], prior))
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None and any(
        r.selection.value == "INVALID_CURRENCY" for r in out.reasons
    )
    mixed = material("S0")
    object.__setattr__(
        mixed,
        "total_assets",
        MetricEvidence(Decimal(100), "x", "EUR", MetricSelection.EXACT),
    )
    object.__setattr__(mixed, "_source_ref", mixed._compute_source_ref())
    members = [member(f"S{i}") for i in range(8)]
    members[0] = replace(members[0], materials=(mixed, members[0].materials[1]))
    assert (
        assemble_quant_multifactor_input(assembly(tuple(members))).cohort_input is None
    )


def test_qb05c_nonconsecutive_year_is_not_relabelled():
    members = [member(f"S{i}") for i in range(8)]
    current = material("S0", 2026, date(2026, 1, 3))
    prior = material("S0", 2024, date(2024, 12, 30))
    members[0] = replace(members[0], materials=(current, prior))
    outcome = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert (
        outcome.cohort_input.members[0].prior_fundamentals.fiscal_period.fiscal_year
        == 2024
    )
    pack = evaluate_quant_multifactor_core_v1(outcome.cohort_input)
    growth = next(
        x
        for x in pack.member_results[0].factors[2].metrics
        if x.metric_name == "revenue_yoy_growth"
    )
    assert growth.unavailable_reason == "UNAVAILABLE_NONCONSECUTIVE_PRIOR_PERIOD"


def test_qb06_qb07_qb08_raw_count_and_actual_page2_identity():
    assert not requires_page2(page("A", 137))
    assert requires_page2(page("A", 200, True))
    first = page2_request_identity(page("A", 200))
    changed = page2_request_identity(page("A", 200, start=date(2026, 8, 8)))
    assert first.before != changed.before and first.page_index == 2


def test_qb09_qb10_daily_page1_monthly_merged_and_member_scope_validation():
    members = [member(f"S{i}") for i in range(8)]
    members[0] = member("S0", invalid=True)
    assert (
        assemble_quant_multifactor_input(assembly(tuple(members))).cohort_input is None
    )
    members = [member(f"S{i}", count=137) for i in range(8)]
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is not None and all(
        len(m.daily_closes) == 137 for m in out.cohort_input.members
    )


def test_actual_page2_daily_is_page1_only_and_monthly_is_merged():
    members = []
    for index in range(8):
        base = member(f"S{index}", count=200)
        identity = page2_request_identity(base.page1)
        older = page(f"S{index}", count=150, start=date(2025, 10, 31))
        page2 = replace(older, request=identity)
        members.append(replace(base, page2=page2))
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    observed = out.cohort_input.members[0]
    assert len(observed.daily_closes) == 200
    assert len(observed.monthly_closes) == 13
    assert observed.daily_closes[0].date > date(2025, 10, 31)
    wrong = replace(
        members[0].page2,
        request=replace(
            members[0].page2.request,
            before=members[0].page2.request.before - timedelta(seconds=1),
        ),
    )
    broken = list(members)
    broken[0] = replace(broken[0], page2=wrong)
    failure = assemble_quant_multifactor_input(assembly(tuple(broken)))
    assert failure.cohort_input is None and any(
        r.selection.value == "INVALID_CANDLES" for r in failure.reasons
    )


def test_qb11_qb12_qb13_qb18_permutation_privacy_and_raw_adjustment():
    members = tuple(member(f"S{i}") for i in range(8))
    assert (
        assemble_quant_multifactor_input(assembly(members[:7])).cohort_input is not None
    )
    one = assemble_quant_multifactor_input(assembly(members))
    two = assemble_quant_multifactor_input(assembly(tuple(reversed(members))))
    assert one.provenance_digest == two.provenance_digest
    assert (
        one.cohort_input == two.cohort_input
        and one.cohort_input.adjustment is AdjustmentV1.RAW
    )
    failing_members = list(members)
    failing_members[0] = replace(
        failing_members[0],
        proof=replace(failing_members[0].proof, membership_holdings_digest="1" * 64),
    )
    failure = assemble_quant_multifactor_input(assembly(tuple(failing_members)))
    assert failure.reasons and all(
        "S0" not in reason.detail for reason in failure.reasons
    )


def test_variable_exact_cohort_and_exact_proof_set_semantics():
    three = tuple(member(f"S{i}") for i in range(3))
    out = assemble_quant_multifactor_input(assembly(three))
    assert out.cohort_input is not None and out.admissible_count == 3
    extra_holdings = replace(
        assembly(three), holdings=holdings(tuple(f"S{i}" for i in range(4)))
    )
    assert assemble_quant_multifactor_input(extra_holdings).cohort_input is None
    duplicate = three[:-1] + (member("S0"),)
    first = assemble_quant_multifactor_input(
        replace(assembly(three), members=duplicate)
    )
    second = assemble_quant_multifactor_input(
        replace(assembly(three), members=tuple(reversed(duplicate)))
    )
    assert (
        first.cohort_input is None
        and first.reasons
        and first.provenance_digest == second.provenance_digest
    )


def test_membership_holdings_digest_and_observation_instants_are_validated():
    members = list(tuple(member(f"S{i}") for i in range(3)))
    bad_digest = replace(
        members[0].proof,
        membership_holdings_digest="1" * 64,
    )
    members[0] = replace(members[0], proof=bad_digest)
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None and any(
        r.detail == "membership_proof" for r in out.reasons
    )
    future = replace(
        member("S0").proof,
        observation_instants=(
            ObservationInstantV1(
                ObservationSourceKindV1.NASDAQ_LISTED,
                AS_OF + timedelta(seconds=1),
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_REGISTRY,
                AS_OF - timedelta(minutes=1),
            ),
            ObservationInstantV1(
                ObservationSourceKindV1.SEC_ISSUER,
                AS_OF - timedelta(minutes=1),
            ),
        ),
    )
    out = assemble_quant_multifactor_input(
        replace(
            assembly(tuple(member(f"S{i}") for i in range(3))),
            members=(replace(member("S0"), proof=future),)
            + tuple(member(f"S{i}") for i in range(1, 3)),
        )
    )
    assert out.cohort_input is None and any(
        r.detail == "membership_proof" for r in out.reasons
    )


def test_failure_digest_binds_non_sensitive_sources_policies_and_reasons():
    seven = list(tuple(member(f"S{i}") for i in range(7)))
    seven[0] = replace(
        seven[0],
        proof=replace(seven[0].proof, membership_holdings_digest="1" * 64),
    )
    seven = tuple(seven)
    one = assemble_quant_multifactor_input(assembly(seven))
    two = assemble_quant_multifactor_input(
        replace(assembly(seven), universe_policy_version="changed-v1")
    )
    changed_member = replace(
        seven[0], materials=(material("S0", 2023, date(2023, 12, 31)),)
    )
    three = assemble_quant_multifactor_input(assembly((changed_member,) + seven[1:]))
    assert (
        len({one.provenance_digest, two.provenance_digest, three.provenance_digest})
        == 3
    )
    assert one.reasons and all("S0" not in reason.detail for reason in one.reasons)


def test_member_core_invariants_fail_typed_before_core():
    bad = material("S0", period_end=date(2027, 1, 1))
    members = [member(f"S{i}") for i in range(8)]
    members[0] = replace(members[0], materials=(bad,))
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None and any(
        r.selection.value == "INVALID_FUNDAMENTALS" for r in out.reasons
    )
    negative = material("S0")
    object.__setattr__(
        negative,
        "revenue_ttm",
        MetricEvidence(Decimal(-1), "x", "USD", MetricSelection.EXACT),
    )
    object.__setattr__(negative, "_source_ref", negative._compute_source_ref())
    members[0] = replace(members[0], materials=(negative,))
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None and any(
        r.selection.value == "INVALID_FUNDAMENTALS" for r in out.reasons
    )


def test_duplicate_daily_calendar_date_fails_member_typed_before_core():
    members = [member(f"S{i}") for i in range(8)]
    page1 = members[0].page1
    newest = page1.rows[0]
    duplicate = replace(newest, timestamp=newest.timestamp - timedelta(hours=1))
    rows = (newest, duplicate) + page1.rows[1:-1]
    members[0] = replace(
        members[0], page1=replace(page1, rows=rows, raw_row_count=len(rows))
    )
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None
    assert any(
        r.selection.value == "INVALID_CANDLES" and r.detail == "candles"
        for r in out.reasons
    )


def test_available_at_year_out_of_range_fails_typed_before_core():
    bad = material("S0", year=2007, period_end=date(2007, 12, 31))
    object.__setattr__(bad, "available_at", datetime(2006, 12, 31, tzinfo=UTC))
    object.__setattr__(bad, "_source_ref", bad._compute_source_ref())
    members = [member(f"S{i}") for i in range(8)]
    members[0] = replace(members[0], materials=(bad,))
    out = assemble_quant_multifactor_input(assembly(tuple(members)))
    assert out.cohort_input is None and any(
        r.selection.value == "INVALID_FUNDAMENTALS" for r in out.reasons
    )


def test_invalid_as_of_returns_early_typed_and_permutation_deterministic():
    members = tuple(member(f"S{i}") for i in range(8))
    for invalid in (
        datetime(2026, 8, 11, 20),
        datetime(2006, 8, 11, 20, tzinfo=UTC),
        datetime.fromisoformat("2007-01-01T00:30:00+09:00"),
        datetime.fromisoformat("9999-12-31T23:00:00-05:00"),
    ):
        one = assemble_quant_multifactor_input(
            replace(assembly(members), as_of=invalid)
        )
        two = assemble_quant_multifactor_input(
            replace(assembly(tuple(reversed(members))), as_of=invalid)
        )
        assert one.cohort_input is None and one.reasons
        assert any(
            r.selection.value == "COHORT_UNAVAILABLE" and r.detail == "as_of_identity"
            for r in one.reasons
        )
        assert one.provenance_digest == two.provenance_digest


def test_provenance_holdings_digest_is_separate_from_membership_holdings_digest():
    ordered = holdings(tuple(f"S{i}" for i in range(3)))
    permuted = holdings(tuple(reversed(tuple(f"S{i}" for i in range(3)))))
    assert _provenance_holdings_digest(ordered) == _provenance_holdings_digest(permuted)
    assert (
        sha256(canonical_holdings_bytes(ordered)).hexdigest()
        != sha256(canonical_holdings_bytes(permuted)).hexdigest()
    )


def test_successful_assembled_input_always_passes_frozen_validation_and_core():
    out = assemble_quant_multifactor_input(
        assembly(tuple(member(f"S{i}") for i in range(8)))
    )
    assert out.cohort_input is not None
    assert out.provenance_version == ASSEMBLY_PROVENANCE_VERSION
    validate_input(out.cohort_input)
    evaluate_quant_multifactor_core_v1(out.cohort_input)


def test_qb17_dst_boundary_agreement():
    zone = ZoneInfo("America/New_York")
    for instant in (
        datetime(2026, 3, 8, 6, 59, tzinfo=UTC),
        datetime(2026, 3, 8, 7, tzinfo=UTC),
        datetime(2026, 11, 1, 5, 59, tzinfo=UTC),
        datetime(2026, 11, 1, 6, tzinfo=UTC),
    ):
        assert (
            market_utc_offset(MarketV1.US_NYSE_NASDAQ, instant)
            == instant.astimezone(zone).utcoffset()
        )
