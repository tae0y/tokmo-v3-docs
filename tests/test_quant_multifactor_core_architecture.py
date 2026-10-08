import ast
import builtins
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, DivisionByZero, InvalidOperation, Overflow, ROUND_CEILING, getcontext
from hashlib import sha256
from json import loads
from pathlib import Path


import pytest

from tokmo.programs.quant_multifactor_core import SPEC, SPEC_DIGEST, as_of_market_date, canonical_bytes, evaluate_quant_multifactor_core_v1, market_utc_offset, validate_input, validate_limitations, bounded_pack_bytes
from tokmo.values.quant_multifactor_core import AccountingRegimeV1, AdjustmentV1, CohortInputV1, DailyCloseV1, FiscalPeriodIdentityV1, FundamentalSnapshotV1, MarketV1, MemberObservationV1, MonthlyCloseV1, PeriodTypeV1, SectorV1, SessionDateRuleV1

ROOT = Path(__file__).resolve().parents[1]


def snapshot(symbol: str, year: int = 2025, prior: bool = False) -> FundamentalSnapshotV1:
    return FundamentalSnapshotV1(f"synthetic:{symbol}:{year}", FiscalPeriodIdentityV1(PeriodTypeV1.ANNUAL, year), date(year, 12, 31), datetime(2026, 2, 1, tzinfo=timezone.utc), f"synthetic:{symbol}:original" if prior else None, "USD", Decimal("100"), Decimal("10"), Decimal("40"), Decimal("50"), Decimal("200"), Decimal("10"))


def member(index: int, *, listed: bool = True, prior_year: int = 2024) -> MemberObservationV1:
    month_pairs = tuple((2025,m) for m in range(7,13)) + tuple((2026,m) for m in range(1,8))
    months = tuple(MonthlyCloseV1(f"{year:04d}-{month:02d}", date(year, month, 28) - timedelta(days=max(0,(date(year,month,28).weekday()-4))), Decimal(100 + offset), "USD") for offset,(year,month) in enumerate(month_pairs))
    days = tuple(DailyCloseV1(date(2026,7,13)+timedelta(days=i),Decimal(100+i),"USD") for i in range(25) if (date(2026,7,13)+timedelta(days=i)).weekday()<5)
    return MemberObservationV1(f"S{index:03d}", SectorV1.NON_FINANCIALS, f"synthetic:membership:{index}", listed, snapshot(str(index)), snapshot(str(index),prior_year,True), months, days)


def cohort(count: int = 8) -> CohortInputV1:
    return CohortInputV1(MarketV1.US_NYSE_NASDAQ,"USD",AccountingRegimeV1.US_GAAP,datetime(2026,8,10,12,tzinfo=timezone.utc),tuple(member(i) for i in range(count)),"synthetic:prices",AdjustmentV1.ADJ_SPLIT,"synthetic-v1",datetime(2026,8,10,tzinfo=timezone.utc),SessionDateRuleV1.US_NYSE_NASDAQ_SESSION,"synthetic-universe","synthetic-v1",date(2026,8,10))


def test_v01_validation_degraded_and_nonconsecutive_contracts() -> None:
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(cohort(), members=()))
    bad = replace(cohort(), members=(member(0, listed=False),)+cohort().members[1:])
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(bad)
    degraded=evaluate_quant_multifactor_core_v1(cohort(7))
    assert degraded.coverage == "COHORT_INSUFFICIENT"
    assert degraded.limitations == tuple(sorted((*degraded.limitations,)))
    expected_counts=(7,7,7,7,7,7,0,7)
    assert tuple(metric.valid_count for factor in degraded.member_results[0].factors for metric in factor.metrics) == expected_counts
    assert all(not metric.metric_sufficient for result in degraded.member_results for factor in result.factors for metric in factor.metrics)
    nonconsecutive=evaluate_quant_multifactor_core_v1(replace(cohort(),members=(member(0,prior_year=2023),)+cohort().members[1:]))
    growth=nonconsecutive.member_results[0].factors[2].metrics[0]
    assert growth.unavailable_reason == "UNAVAILABLE_NONCONSECUTIVE_PRIOR_PERIOD"


def test_v11_fixture_hash_and_digest_reconstruction_are_independent() -> None:
    fixture=(ROOT/"tests/fixtures/quant_multifactor_core/textbook.json").read_bytes()
    assert sha256(fixture).hexdigest() == "d382894126f4372ed65b9a27b491cb656559866014e3c96e05ed55ec62aa66a1"
    vector=loads(fixture)
    assert vector["metadata"] == {"currency":"USD","formula_version":"quant-multifactor-formula-v1","source":"hand-constructed-synthetic"}
    assert vector["expected"] == {"book_to_market":"-0.03","earnings_yield":"-0.02","growth":"0.2","max_drawdown":"-0.5","momentum":"0.11","type7_p05":"0.15","type7_p95":"85.30"}
    textbook_members=[]
    for original in cohort().members:
        current=replace(original.current_fundamentals,revenue_ttm=Decimal(vector["inputs"]["growth"]["current"]),net_income_ttm=Decimal("-20"),total_equity=Decimal("-30"),diluted_shares_outstanding=Decimal("100"))
        prior=replace(original.prior_fundamentals,revenue_ttm=Decimal(vector["inputs"]["growth"]["prior"]))
        closes=(Decimal("20"),)+tuple(Decimal("10") for _ in range(19))
        daily=tuple(replace(item,close=closes[i]) for i,item in enumerate(original.daily_closes))
        monthly=tuple(replace(item,close=Decimal(value)) for item,value in zip(original.monthly_closes,vector["inputs"]["momentum_closes"],strict=True))
        textbook_members.append(replace(original,current_fundamentals=current,prior_fundamentals=prior,daily_closes=daily,monthly_closes=monthly))
    produced=evaluate_quant_multifactor_core_v1(replace(cohort(),members=tuple(textbook_members)))
    assert metric(produced,0,0,0).raw_value==Decimal(vector["expected"]["earnings_yield"])
    assert metric(produced,0,0,1).raw_value==Decimal(vector["expected"]["book_to_market"])
    assert metric(produced,0,2,0).raw_value==Decimal(vector["expected"]["growth"])
    assert metric(produced,0,3,0).raw_value==Decimal(vector["expected"]["momentum"])
    assert metric(produced,0,4,1).raw_value==Decimal(vector["expected"]["max_drawdown"])
    assert sha256(canonical_bytes(SPEC)).hexdigest() == SPEC_DIGEST
    changed=dict(SPEC); changed["composite"]="mutated"
    assert sha256(canonical_bytes(changed)).hexdigest() != SPEC_DIGEST


def test_v12_dst_boundaries_and_no_io_runtime_spy(monkeypatch) -> None:
    before=datetime(2026,3,8,6,59,59,tzinfo=timezone.utc); start=datetime(2026,3,8,7,tzinfo=timezone.utc)
    end=datetime(2026,11,1,6,tzinfo=timezone.utc)
    assert (market_utc_offset(MarketV1.US_NYSE_NASDAQ,before),market_utc_offset(MarketV1.US_NYSE_NASDAQ,start),market_utc_offset(MarketV1.US_NYSE_NASDAQ,end-timedelta(seconds=1)),market_utc_offset(MarketV1.US_NYSE_NASDAQ,end)) == (timedelta(hours=-5),timedelta(hours=-4),timedelta(hours=-4),timedelta(hours=-5))
    monkeypatch.setattr(builtins,"open",lambda *args,**kwargs: (_ for _ in ()).throw(AssertionError("io")))
    assert evaluate_quant_multifactor_core_v1(cohort()).cohort_size == 8


def test_v12_recursive_ast_boundary() -> None:
    allowed={"__future__","collections.abc","dataclasses","datetime","decimal","enum","hashlib","json","re","types","tokmo.values.quant_multifactor_core"}
    forbidden=("zoneinfo","socket","sqlite3","subprocess","pathlib","os","time","logging","numpy","pandas")
    for relative in ("src/tokmo/values/quant_multifactor_core.py","src/tokmo/programs/quant_multifactor_core.py"):
        tree=ast.parse((ROOT/relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node,(ast.Import,ast.ImportFrom)):
                names=[item.name for item in node.names] if isinstance(node,ast.Import) else [node.module or ""]
                assert all(name in allowed and not name.startswith(forbidden) for name in names)
            if isinstance(node,ast.Constant) and isinstance(node.value,float): pytest.fail("binary float literal")

    assert {path.relative_to(ROOT).as_posix() for path in ROOT.glob("src/tokmo/*/quant_multifactor_core.py")} == {"src/tokmo/programs/quant_multifactor_core.py","src/tokmo/values/quant_multifactor_core.py"}


def test_v11_actual_legal_maximum_shapes_and_cardinality_caps() -> None:
    maximum=Decimal("123456789012345678.123456789012")
    calendar=[]; cursor=date(2025,8,26)
    while len(calendar)<252:
        if cursor.weekday()<5: calendar.append(cursor)
        cursor += timedelta(days=1)
    month_pairs=tuple((2025,m) for m in range(8,13))+tuple((2026,m) for m in range(1,9))
    months=tuple(MonthlyCloseV1(f"{year:04d}-{month:02d}",date(year,month,28)-timedelta(days=max(0,date(year,month,28).weekday()-4)),maximum,"USD") for year,month in month_pairs)
    daily=tuple(DailyCloseV1(day,maximum,"USD") for day in calendar)
    members=[]
    for index in range(256):
        suffix=f"{index:03d}"; symbol=("S"*29)+suffix
        current=FundamentalSnapshotV1(("C"*124)+suffix,FiscalPeriodIdentityV1(PeriodTypeV1.ANNUAL,2025),date(2025,12,31),datetime(2026,2,1,tzinfo=timezone.utc),("R"*124)+suffix,"USD",maximum,maximum,maximum,maximum,maximum,maximum)
        prior=FundamentalSnapshotV1(("P"*124)+suffix,FiscalPeriodIdentityV1(PeriodTypeV1.ANNUAL,2024),date(2024,12,31),datetime(2025,2,1,tzinfo=timezone.utc),("O"*124)+suffix,"USD",maximum,maximum,maximum,maximum,maximum,maximum)
        members.append(MemberObservationV1(symbol,SectorV1.NON_FINANCIALS,("M"*124)+suffix,True,current,prior,months,daily))
    maximum_input=CohortInputV1(MarketV1.US_NYSE_NASDAQ,"USD",AccountingRegimeV1.US_GAAP,datetime(2026,8,10,12,tzinfo=timezone.utc),tuple(members),"X"*128,AdjustmentV1.ADJ_SPLIT,"A"*32,datetime(2026,8,10,tzinfo=timezone.utc),SessionDateRuleV1.US_NYSE_NASDAQ_SESSION,"U"*64,"V"*32,date(2026,8,10))
    _,encoded=validate_input(maximum_input)
    assert len(encoded) == 6_071_959
    pack=evaluate_quant_multifactor_core_v1(maximum_input); packed=canonical_bytes(pack)
    assert len(pack.member_results)==256 and sum(len(f.metrics) for f in pack.member_results[0].factors)==8
    assert len(packed) == 853_240
    registry=["CALLER_ATTESTED_POINT_IN_TIME_MEMBERSHIP_NOT_INDEPENDENTLY_VERIFIED","SURVIVORSHIP_BIAS_NOT_INDEPENDENTLY_CONTROLLED","PRICE_ADJUSTMENT_POINT_IN_TIME_NOT_INDEPENDENTLY_VERIFIED","SESSION_CALENDAR_NOT_INDEPENDENTLY_VERIFIED","FUNDAMENTALS_FISCAL_YEAR_END_DIFFERS","COVERAGE_INSUFFICIENT_BELOW_MIN_FACTORS","COVERAGE_INSUFFICIENT_BELOW_MIN_FUNDAMENTAL_FACTORS"]
    registry += [f"FACTOR_METRIC_INSUFFICIENT:{f}:{m}" for f,m in (("value","earnings_yield"),("value","book_to_market"),("quality","roe"),("quality","gross_profitability"),("growth","revenue_yoy_growth"),("price_momentum","momentum_12_1"),("stability","realized_volatility"),("stability","max_drawdown"))]
    extreme_members=tuple(replace(result,factors=tuple(replace(factor,metrics=tuple(replace(item,raw_value=maximum,winsor_p05=maximum,winsor_p95=maximum,winsorized_value=maximum,average_rank=Decimal("256"),percentile=Decimal("1")) for item in factor.metrics)) for factor in result.factors)) for result in pack.member_results)
    maximal_pack=replace(pack,member_results=extreme_members,limitations=validate_limitations(tuple(registry)),pack_digest="f"*64)
    maximal_packed=bounded_pack_bytes(maximal_pack)
    assert len(maximal_packed) == 1_091_307
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(maximum_input,members=maximum_input.members+(maximum_input.members[0],)))
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(maximum_input,members=(replace(maximum_input.members[0],daily_closes=daily+(daily[-1],)),)))
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(maximum_input,members=(replace(maximum_input.members[0],monthly_closes=months+(months[-1],)),)))


def metric(pack, member_index: int, factor_index: int, metric_index: int):
    return pack.member_results[member_index].factors[factor_index].metrics[metric_index]


@pytest.mark.parametrize("mutation", ("lookahead", "quarterly", "duplicate", "future_adjustment", "membership", "zero_close", "negative_revenue", "zero_assets", "zero_shares"))
def test_v01_independent_validation_rejections(mutation: str) -> None:
    value = cohort()
    first = value.members[0]
    current = first.current_fundamentals
    if mutation == "lookahead": current = replace(current, published_at=value.as_of + timedelta(seconds=1))
    elif mutation == "quarterly": current = replace(current, fiscal_period=FiscalPeriodIdentityV1(PeriodTypeV1.QUARTERLY, 2025, 4))
    elif mutation == "negative_revenue": current = replace(current, revenue_ttm=Decimal("-1"))
    elif mutation == "zero_assets": current = replace(current, total_assets=Decimal("0"))
    elif mutation == "zero_shares": current = replace(current, diluted_shares_outstanding=Decimal("0"))
    if mutation in {"lookahead", "quarterly", "negative_revenue", "zero_assets", "zero_shares"}:
        value = replace(value, members=(replace(first, current_fundamentals=current),) + value.members[1:])
    elif mutation == "duplicate": value = replace(value, members=(first, first) + value.members[2:])
    elif mutation == "future_adjustment": value = replace(value, price_adjustment_as_of=value.as_of + timedelta(seconds=1))
    elif mutation == "membership": value = replace(value, membership_as_of=value.membership_as_of-timedelta(days=1))
    elif mutation == "zero_close": value = replace(value, members=(replace(first, daily_closes=(replace(first.daily_closes[0], close=Decimal(0)),)+first.daily_closes[1:]),)+value.members[1:])
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(value)


def test_v01_permutation_digest_and_degraded_exact_shape() -> None:
    value = cohort(7)
    a = evaluate_quant_multifactor_core_v1(value)
    b = evaluate_quant_multifactor_core_v1(replace(value, members=tuple(reversed(value.members))))
    assert (a.input_digest, a.pack_digest, a.member_results) == (b.input_digest, b.pack_digest, b.member_results)
    assert all(r.factor_count == r.fundamental_factor_count == 0 and r.composite_score is None for r in a.member_results)
    assert all(f.factor_score is None and f.contribution is None and (f.metric_coverage.endswith(":0:2") or f.metric_coverage.endswith(":0:1")) for r in a.member_results for f in r.factors)
    assert all(m.winsorized_value is m.average_rank is m.percentile is None for r in a.member_results for f in r.factors for m in f.metrics)
    summary = {m.metric_name:m.valid_count for f in a.factor_summaries for m in f.metric_summaries}
    assert all(m.valid_count == summary[m.metric_name] for r in a.member_results for f in r.factors for m in f.metrics)


def test_v05_reason_priority_and_prior_staleness_boundaries() -> None:
    value=cohort(); first=value.members[0]; stale=replace(first.current_fundamentals, period_end=date(2024,2,8), diluted_shares_outstanding=None, total_equity=Decimal("-1"))
    packed=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,sector=SectorV1.FINANCIALS,current_fundamentals=stale,prior_fundamentals=None),)+value.members[1:]))
    assert [metric(packed,0,*p).unavailable_reason for p in ((0,0),(0,1),(1,0),(1,1),(2,0))] == ["UNAVAILABLE_STALE_FUNDAMENTALS"]*5
    assert metric(packed,0,1,1).applicability == "INAPPLICABLE_SECTOR_FINANCIALS"
    for day, expected in ((date(2024,2,8), None),(date(2024,2,7),"UNAVAILABLE_STALE_FUNDAMENTALS")):
        prior=replace(first.prior_fundamentals,period_end=day,fiscal_period=FiscalPeriodIdentityV1(PeriodTypeV1.ANNUAL,2024))
        got=metric(evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,prior_fundamentals=prior),)+value.members[1:])),0,2,0)
        assert got.unavailable_reason == expected


def test_v06_v07_reason_priorities_and_boundaries() -> None:
    value=cohort(); first=value.members[0]
    # Twelve otherwise-valid closes, with an as-of that makes the latest stale:
    # stale must beat insufficient.
    stale=replace(first, monthly_closes=first.monthly_closes[:12])
    stale_value=replace(value,as_of=datetime(2026,8,25,12,tzinfo=timezone.utc),membership_as_of=date(2026,8,25),members=(stale,)+value.members[1:])
    got=evaluate_quant_multifactor_core_v1(stale_value)
    assert metric(got,0,3,0).unavailable_reason == "UNAVAILABLE_STALE_MONTHLY_SERIES"
    old_days=tuple(replace(x,date=x.date-timedelta(days=364)) for x in first.daily_closes[:1])
    got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,daily_closes=old_days),)+value.members[1:]))
    assert metric(got,0,4,0).unavailable_reason == metric(got,0,4,1).unavailable_reason == "UNAVAILABLE_STALE_PRICE_SERIES"
    got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,daily_closes=()),)+value.members[1:]))
    assert metric(got,0,4,0).unavailable_reason == metric(got,0,4,1).unavailable_reason == "UNAVAILABLE_CLOSE_AT_AS_OF"


def test_v11_registry_boundaries_and_bounded_encoder_no_truncation() -> None:
    assert validate_limitations(("X"*240,)) == ("X"*240,)
    with pytest.raises(ValueError, match="limitations"): validate_limitations(("X"*241,))
    payload={"legal":"X"*(2_097_152)}
    with pytest.raises(ValueError, match="pack_bytes"): bounded_pack_bytes(payload)
    assert payload["legal"].endswith("X")  # rejection did not truncate/mutate semantics


def test_v11_actual_registry_maximum_is_fifteen_distinct_codes() -> None:
    codes=["CALLER_ATTESTED_POINT_IN_TIME_MEMBERSHIP_NOT_INDEPENDENTLY_VERIFIED","SURVIVORSHIP_BIAS_NOT_INDEPENDENTLY_CONTROLLED","PRICE_ADJUSTMENT_POINT_IN_TIME_NOT_INDEPENDENTLY_VERIFIED","SESSION_CALENDAR_NOT_INDEPENDENTLY_VERIFIED","FUNDAMENTALS_FISCAL_YEAR_END_DIFFERS","COVERAGE_INSUFFICIENT_BELOW_MIN_FACTORS","COVERAGE_INSUFFICIENT_BELOW_MIN_FUNDAMENTAL_FACTORS"]
    metrics=(("value","earnings_yield"),("value","book_to_market"),("quality","roe"),("quality","gross_profitability"),("growth","revenue_yoy_growth"),("price_momentum","momentum_12_1"),("stability","realized_volatility"),("stability","max_drawdown"))
    codes += [f"FACTOR_METRIC_INSUFFICIENT:{f}:{m}" for f,m in metrics]
    assert len(validate_limitations(tuple(reversed(codes)))) == 15


def test_v12_year_edges_and_spec_is_independently_reconstructed() -> None:
    assert as_of_market_date(MarketV1.KR_KOSPI, datetime(2007,1,1,tzinfo=timezone.utc)) == date(2007,1,1)
    with pytest.raises(ValueError): market_utc_offset(MarketV1.US_NYSE_NASDAQ,datetime(2006,12,31,tzinfo=timezone.utc))
    expected_sections={"domain","formula_version","metrics","orientations","cross_section","weights","composite","bounds","reason_priority","applicability","units","thresholds","staleness","codes","quantization","signs","serialization"}
    assert set(SPEC) == expected_sections
    baseline=sha256(canonical_bytes(SPEC)).hexdigest(); assert baseline == SPEC_DIGEST == "4855546273a8a765eb7bc780ca63fc7dfeeb4789b7809b2ee9f9c47b49edb1c4"
    for section in expected_sections:
        mutant=dict(SPEC); mutant[section]={"mutation":section}
        assert sha256(canonical_bytes(mutant)).hexdigest() != baseline


def test_repair_b1_b2_output_quantization_and_global_context_independence() -> None:
    value=cohort(); first=value.members[0]
    current=replace(first.current_fundamentals,total_equity=Decimal("1"),diluted_shares_outstanding=Decimal("3"))
    value=replace(value,members=(replace(first,current_fundamentals=current,daily_closes=tuple(replace(x,close=Decimal("1")) for x in first.daily_closes)),)+value.members[1:])
    baseline=evaluate_quant_multifactor_core_v1(value)
    assert metric(baseline,0,0,1).raw_value == Decimal("0.33333333")
    context=getcontext(); saved=context.copy()
    try:
        context.prec=7; context.rounding=ROUND_CEILING
        context.traps[InvalidOperation]=False; context.traps[DivisionByZero]=False; context.traps[Overflow]=False
        changed=evaluate_quant_multifactor_core_v1(value)
        assert (changed.input_digest,changed.pack_digest,changed.member_results)==(baseline.input_digest,baseline.pack_digest,baseline.member_results)
    finally:
        context.prec=saved.prec; context.rounding=saved.rounding
        for signal, trapped in saved.traps.items(): context.traps[signal]=trapped


def test_repair_m1_m2_priority_and_monthly_future_filter() -> None:
    value=cohort(); first=value.members[0]
    current=replace(first.current_fundamentals,revenue_ttm=None)
    prior=replace(first.prior_fundamentals,revenue_ttm=Decimal("0"))
    got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,current_fundamentals=current,prior_fundamentals=prior),)+value.members[1:]))
    assert metric(got,0,2,0).unavailable_reason == "UNAVAILABLE_NONPOSITIVE_PRIOR_REVENUE"
    future=tuple(replace(x,session_date=x.session_date+timedelta(days=728),month_id=(x.session_date+timedelta(days=728)).strftime("%Y-%m")) for x in first.monthly_closes)
    # Future closes are structurally valid but ineligible at this as-of.
    got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,monthly_closes=future),)+value.members[1:]))
    assert metric(got,0,3,0).raw_value is None


def test_repair_m6_provenance_string_currency_reasons_boundaries_and_normal_permutation() -> None:
    value=cohort(); baseline=evaluate_quant_multifactor_core_v1(value); first=value.members[0]
    for changed in (
        replace(first.current_fundamentals,source_ref="synthetic:changed"),
        replace(first.current_fundamentals,revision_of="synthetic:changed"),
    ):
        got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,current_fundamentals=changed),)+value.members[1:]))
        assert got.input_digest != baseline.input_digest and got.pack_digest != baseline.pack_digest
    for bad in ("X"*129,"한글","secret=token","line\nbreak"):
        with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(value,price_source_ref=bad))
    with pytest.raises(ValueError): evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,daily_closes=(replace(first.daily_closes[0],currency="KRW"),)+first.daily_closes[1:]),)+value.members[1:]))
    pairs=((2025,6),)+tuple((2025,m) for m in range(8,13))+tuple((2026,m) for m in range(1,8))
    gapped=tuple(MonthlyCloseV1(f"{y:04d}-{m:02d}",date(y,m,28)-timedelta(days=max(0,date(y,m,28).weekday()-4)),Decimal("100"),"USD") for y,m in pairs)
    got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,monthly_closes=gapped),)+value.members[1:]))
    assert metric(got,0,3,0).unavailable_reason == "UNAVAILABLE_NONCONSECUTIVE_MONTH_IDS"
    sparse=replace(first,current_fundamentals=replace(first.current_fundamentals,gross_profit_ttm=None))
    limited=evaluate_quant_multifactor_core_v1(replace(value,members=(sparse,)+tuple(replace(m,current_fundamentals=replace(m.current_fundamentals,gross_profit_ttm=None)) for m in value.members[1:5])+value.members[5:]))
    insufficient=metric(limited,5,1,1)
    assert insufficient.raw_value == Decimal("0.2")
    assert insufficient.available is True
    assert insufficient.percentile is None
    assert insufficient.unavailable_reason is None
    assert insufficient.metric_sufficient is False
    assert insufficient.valid_count == 3
    assert "FACTOR_METRIC_INSUFFICIENT:quality:gross_profitability" in limited.limitations
    assert all(
        item.unavailable_reason != "UNAVAILABLE_METRIC_INSUFFICIENT"
        for result in limited.member_results
        for factor in result.factors
        for item in factor.metrics
    )
    reversed_pack=evaluate_quant_multifactor_core_v1(replace(value,members=tuple(reversed(value.members))))
    assert (reversed_pack.input_digest,reversed_pack.pack_digest,reversed_pack.member_results)==(baseline.input_digest,baseline.pack_digest,baseline.member_results)


def test_pack_level_three_factor_residual_exact_sum_and_permutation_stability() -> None:
    value=cohort()
    members=tuple(
        replace(
            item,
            current_fundamentals=replace(item.current_fundamentals,diluted_shares_outstanding=None),
            monthly_closes=item.monthly_closes[:12],
        )
        for item in value.members
    )
    legal=replace(value,members=members)
    pack=evaluate_quant_multifactor_core_v1(legal)
    reversed_pack=evaluate_quant_multifactor_core_v1(replace(legal,members=tuple(reversed(members))))
    for result in pack.member_results:
        contributions=tuple(factor.contribution for factor in result.factors)
        assert contributions == (
            None,
            Decimal("0.16666667"),
            Decimal("0.16666667"),
            None,
            Decimal("0.16666666"),
        )
        assert sum((item for item in contributions if item is not None),Decimal(0)) == result.composite_score == Decimal("0.50000000")
    assert (reversed_pack.input_digest,reversed_pack.pack_digest,reversed_pack.member_results) == (pack.input_digest,pack.pack_digest,pack.member_results)



def test_repair_recency_boundaries_and_supported_extreme_date() -> None:
    value=replace(cohort(),as_of=datetime(2026,8,11,12,tzinfo=timezone.utc),membership_as_of=date(2026,8,11)); first=value.members[0]
    for days, reason in ((7,None),(8,"UNAVAILABLE_STALE_PRICE_SERIES")):
        latest=value.membership_as_of-timedelta(days=days)
        daily=(DailyCloseV1(latest,Decimal("100"),"USD"),)
        got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,daily_closes=daily),)+value.members[1:]))
        assert metric(got,0,4,1).unavailable_reason == ("UNAVAILABLE_INSUFFICIENT_DAYS" if reason is None else reason)
    monthly_value=replace(cohort(),as_of=datetime(2026,8,3,12,tzinfo=timezone.utc),price_adjustment_as_of=datetime(2026,8,3,tzinfo=timezone.utc),membership_as_of=date(2026,8,3)); first=monthly_value.members[0]
    pairs=tuple((2025,m) for m in range(6,13))+tuple((2026,m) for m in range(1,7))
    for age,expected in ((45,None),(46,"UNAVAILABLE_STALE_MONTHLY_SERIES")):
        months=[]
        for y,m in pairs:
            session=date(y,m,28)-timedelta(days=max(0,date(y,m,28).weekday()-4))
            months.append(MonthlyCloseV1(f"{y:04d}-{m:02d}",session,Decimal("100"),"USD"))
        months[-1]=replace(months[-1],session_date=monthly_value.membership_as_of-timedelta(days=age))
        got=evaluate_quant_multifactor_core_v1(replace(monthly_value,members=(replace(first,monthly_closes=tuple(months)),)+monthly_value.members[1:]))
        assert metric(got,0,3,0).unavailable_reason == expected
    with pytest.raises(ValueError): as_of_market_date(MarketV1.KR_KOSPI,datetime(9999,12,31,20,tzinfo=timezone.utc))


def test_repair_production_formula_vectors_and_548_549_boundary() -> None:
    value=cohort(); members=[]
    for i,original in enumerate(value.members):
        current=replace(original.current_fundamentals,revenue_ttm=Decimal(110+i*10),net_income_ttm=Decimal(5+i),gross_profit_ttm=Decimal(20+i*3),total_equity=Decimal(40+i*2),total_assets=Decimal(100+i*5),diluted_shares_outstanding=Decimal("10"))
        prior=replace(original.prior_fundamentals,revenue_ttm=Decimal("100"))
        members.append(replace(original,current_fundamentals=current,prior_fundamentals=prior))
    pack=evaluate_quant_multifactor_core_v1(replace(value,members=tuple(reversed(members))))
    assert [metric(pack,i,2,0).raw_value for i in range(8)] == [Decimal(f"0.{i+1}0000000") for i in range(8)]
    assert [metric(pack,i,2,0).percentile for i in range(8)] == [Decimal(x) for x in ("0","0.14285714","0.28571429","0.42857143","0.57142857","0.71428571","0.85714286","1")]
    first=value.members[0]
    for age,expected in ((548,None),(549,"UNAVAILABLE_STALE_FUNDAMENTALS")):
        stale=replace(first.current_fundamentals,period_end=value.membership_as_of-timedelta(days=age))
        got=evaluate_quant_multifactor_core_v1(replace(value,members=(replace(first,current_fundamentals=stale),)+value.members[1:]))
        assert metric(got,0,0,0).unavailable_reason == expected


def test_repair_rv_population_sqrt252_and_mdd_production_vector() -> None:
    value=cohort(); closes=tuple(Decimal(x) for x in (100,102,101,105,103,107,106,110,108,111,109,113,112,115,114,118,116,119,117,121,120,123,122,125,124))
    dates=[]; cursor=date(2026,8,10)
    while len(dates)<25:
        if cursor.weekday()<5: dates.append(cursor)
        cursor-=timedelta(days=1)
    dates.reverse(); daily=tuple(DailyCloseV1(day,close,"USD") for day,close in zip(dates,closes,strict=True))
    members=tuple(replace(item,daily_closes=daily) for item in value.members)
    pack=evaluate_quant_multifactor_core_v1(replace(value,members=members))
    assert metric(pack,0,4,0).raw_value == Decimal("35.308091")
    assert metric(pack,0,4,1).raw_value == Decimal("-0.01904762")
