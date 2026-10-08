"""G1-A RED→GREEN formula/threshold/digest tests (SDS §8.3 F-01..F-15).

F-01..F-08, F-13 are behavioral boundary tests that exercise the real
production entry point evaluate_macro_divergence (or percentile_rank) with
bundles constructed to hit exact threshold boundaries. F-11/F-12 test
digest/decimal properties. F-14/F-15 are tested via the analyzer file.
"""

import hashlib
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tokmo.values.macro_divergence import (
    ConfidenceCeiling,
    FOREIGN_OUTFLOW_STRESS_THRESHOLD,
    HIGH_PERCENTILE_THRESHOLD,
    MacroDivergenceComponent,
    MacroDivergenceComponentV1,
    MacroDivergenceState,
    MacroWindowSummaryV1,
    MacroSymbolContactV1,
    compute_pack_digest,
    map_confidence_ceiling,
    percentile_rank,
    PACK_BYTE_CAP,
)
from tokmo.values.macro_observations import (
    MacroCandleObservation,
    MacroCoverageState,
    MacroInvestorObservation,
    compute_series_digest,
)

KST = timezone(timedelta(hours=9))


# --------------------------------------------------------------------------
# Shared helpers — build bundles with exact boundary control
# --------------------------------------------------------------------------

def _gen_dates(n: int, end: date) -> list[date]:
    dates: list[date] = []
    cur = end
    while len(dates) < n:
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(dates))


def _candle(d: date, close: str) -> MacroCandleObservation:
    obs = datetime.combine(d, datetime.min.time(), tzinfo=KST).replace(hour=15, minute=30)
    return MacroCandleObservation(
        session_date=d, observed_at=obs, available_at=obs + timedelta(minutes=5),
        open_price=close, high_price=close, low_price=close,
        close_price=close, volume="100000", currency="KRW",
    )


def _investor(d: date, net: str) -> MacroInvestorObservation:
    obs = datetime.combine(d, datetime.min.time(), tzinfo=KST).replace(hour=15, minute=30)
    n = Decimal(net)
    buy = str(abs(n)) if n >= 0 else "0"
    sell = str(abs(n)) if n < 0 else "0"
    return MacroInvestorObservation(
        session_date=d, observed_at=obs, available_at=obs + timedelta(minutes=5),
        foreign_buy=buy, foreign_sell=sell,
        individual_buy="0", individual_sell="0",
        institution_buy="0", institution_sell="0",
    )


def _digest() -> str:
    return hashlib.sha256(b"canonical-spec-text").hexdigest()


def _core_lifecycle(as_of: datetime):
    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence  # noqa: F401
    from tokmo.values.signal_program_lifecycle import (
        ProgramKindV1, ProgramLifecycleSpecV1,
        ProgramLifecycleStateV1, ProgramLifecycleStatusV1,
        evaluate_lifecycle_status,
    )
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001", version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=90),
        expires_at=None, review_cadence_days=90,
        spec_digest=_digest(),
    )
    return evaluate_lifecycle_status(
        spec, as_of=as_of, last_evidence_at=None,
        evidence_count=0, renewal_count=0,
    )


def _build_bundle(kospi, proxy, investor, as_of):
    from tokmo.values.macro_observations import (
        MacroObservationSeriesV1, MacroSourceRefV1,
    )
    return MacroObservationSeriesV1(
        kospi_candles=tuple(kospi),
        proxy_candles=tuple(proxy),
        investor_records=tuple(investor),
        as_of=as_of,
        source_refs=(
            MacroSourceRefV1(
                operation="INDICATOR_CANDLES", symbol="KOSPI", interval="1d",
                requested_count=200, returned_count=len(kospi),
                date_range_start=kospi[0].session_date if kospi else None,
                date_range_end=kospi[-1].session_date if kospi else None,
                series_digest=compute_series_digest(tuple(kospi)),
            ),
            MacroSourceRefV1(
                operation="STOCK_CANDLES", symbol="261240", interval="1d",
                requested_count=200, returned_count=len(proxy),
                date_range_start=proxy[0].session_date if proxy else None,
                date_range_end=proxy[-1].session_date if proxy else None,
                series_digest=compute_series_digest(tuple(proxy)),
            ),
            MacroSourceRefV1(
                operation="INDICATOR_INVESTOR_TRADING", symbol="KOSPI", interval="1d",
                requested_count=100, returned_count=len(investor),
                date_range_start=investor[0].session_date if investor else None,
                date_range_end=investor[-1].session_date if investor else None,
                series_digest=compute_series_digest(tuple(investor)),
            ),
        ),
    )


def _eval(bundle, as_of):
    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    return evaluate_macro_divergence(bundle, lifecycle=_core_lifecycle(as_of))


# --------------------------------------------------------------------------
# State and component closed-set checks
# --------------------------------------------------------------------------

def test_divergence_state_closed_set():
    members = {m.value for m in MacroDivergenceState}
    assert members == {
        "INSUFFICIENT_DATA",
        "NO_MATERIAL_DIVERGENCE",
        "INDEX_HIGH_FX_PROXY_HIGH",
        "FOREIGN_OUTFLOW_CONFIRMATION",
        "OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL",
    }


def test_divergence_component_closed_set():
    members = {m.value for m in MacroDivergenceComponent}
    assert members == {
        "INDEX_HIGH_FX_PROXY_HIGH",
        "FOREIGN_OUTFLOW_CONFIRMATION",
        "OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL",
    }


def test_confidence_ceiling_closed_set():
    members = {m.value for m in ConfidenceCeiling}
    assert members == {"DIRECT_FLOW_DATA", "PROXY_ROLL_SENSITIVE", "NOT_APPLICABLE"}


# ==========================================================================
# F-01: percentile_rank exactly 0.90 fires via behavioral boundary
# ==========================================================================

def test_f01_percentile_exactly_090_fires():
    """100 closes: 10 at 2001, 89 at 1000, latest at 2000 (90th rank).
    count_le(2000) = 90 (89 at 1000 + 1 at 2000). percentile = 90/100 = 0.90.
    The >= comparison means exactly 0.90 fires."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = []
    for i in range(100):
        if i < 10:
            close = "2001"
        elif i == 99:
            close = "2000"  # latest: exactly at 90th rank
        else:
            close = "1000"
        kospi.append(_candle(dates[i], close))
    proxy = list(kospi)  # same pattern for proxy
    investor = [_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[0].fired is True
    assert pack.window_summary.kospi_percentile == "0.9"


# ==========================================================================
# F-02: percentile 89/100 = 0.89 does NOT fire
# ==========================================================================

def test_f02_percentile_089_does_not_fire():
    """100 closes: 11 at 2001, 88 at 1000, latest at 2000.
    count_le(2000) = 89. percentile = 89/100 = 0.89 < 0.90 → does not fire."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = []
    for i in range(100):
        if i < 11:
            close = "2001"
        elif i == 99:
            close = "2000"
        else:
            close = "1000"
        kospi.append(_candle(dates[i], close))
    # proxy at 0.90 to isolate: KOSPI side fails → component doesn't fire
    proxy = []
    for i in range(100):
        if i < 10:
            close = "2001"
        elif i == 99:
            close = "2000"
        else:
            close = "1000"
        proxy.append(_candle(dates[i], close))
    investor = [_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[0].fired is False
    assert pack.window_summary.kospi_percentile == "0.89"


# ==========================================================================
# F-03: foreign flow exactly -20조 fires (inclusive <=) via analyzer
# ==========================================================================

def test_f03_flow_exactly_20t_fires():
    """Latest 20 sessions: each net = -1조 → total = -20조 exactly.
    FOREIGN_OUTFLOW_CONFIRMATION fires because <= is inclusive."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # Sub-90th percentile KOSPI/proxy
    kospi = [_candle(d, "1000") for d in dates]
    kospi[-1] = _candle(dates[-1], "1001")  # only 1 above → low percentile
    proxy = [_candle(d, "1000") for d in dates]
    proxy[-1] = _candle(dates[-1], "1001")
    investor = [_investor(d, "0") for d in dates]
    for i in range(80, 100):
        investor[i] = _investor(dates[i], "-1000000000000")  # -1조 each → -20조 total
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[1].fired is True
    assert pack.window_summary.foreign_rolling_flow_20 == "-20000000000000"


# ==========================================================================
# F-04: foreign flow -19.999조 does NOT fire
# ==========================================================================

def test_f04_flow_19_999t_does_not_fire():
    """Latest 20 sessions: total = -19.999조 (just above -20조 threshold)."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    investor = [_investor(d, "0") for d in dates]
    # -19.999조 / 20 = -999.95조 per session = -999950000000000
    per_session = Decimal("-19999000000000") / 20
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(per_session))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[1].fired is False


# ==========================================================================
# F-05: exhaustion boundary: prior -39조, current -19.5조 (= prior*0.5),
#       FX return exactly -0.03 → FIRES
# ==========================================================================

def test_f05_exhaustion_boundary_fires():
    """Prior 20 = -39조 (stressed), current 20 = -19.5조 (= prior*0.5).
    Both strict > -20조 and >= improvement threshold pass. FX return = -0.03."""
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    # Override last 8 proxy closes: 1000 → 970 exactly (-3%)
    proxy[-1] = _candle(dates[-1], "970")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-39000000000000") / 20  # -1.95조 each → -39조
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-19500000000000") / 20  # -0.975조 each → -19.5조
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is True
    assert pack.components[2].details["prior_flow_20"] == "-39000000000000"
    assert pack.components[2].details["current_flow_20"] == "-19500000000000"
    assert pack.components[2].details["fx_return_8"] == "-0.03"


# ==========================================================================
# F-05b: prior -39조, current -19.6조 (passes > -20조 but fails >= -19.5조)
# ==========================================================================

def test_f05b_exhaustion_improvement_just_below_fails():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    proxy[-1] = _candle(dates[-1], "970")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-39000000000000") / 20
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-19600000000000") / 20  # -19.6조 total
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is False
    assert pack.components[2].details["improvement_passed"] == "false"


# ==========================================================================
# F-05c: coherence: prior -40조, current exactly -20조
#   exhaustion NOT fire (strict > fails), but OUTFLOW DOES fire (inclusive <=)
# ==========================================================================

def test_f05c_coherence_prior40_current20():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    proxy[-1] = _candle(dates[-1], "970")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-40000000000000") / 20  # -40조
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-20000000000000") / 20  # exactly -20조
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    # Exhaustion: current > -20조 is False → does not fire
    assert pack.components[2].fired is False
    assert pack.components[2].details["current_no_longer_stressed"] == "false"
    # FOREIGN_OUTFLOW_CONFIRMATION: -20조 <= -20조 → fires
    assert pack.components[1].fired is True


# ==========================================================================
# F-06: FX return -0.029 does NOT fire exhaustion
# ==========================================================================

def test_f06_return_0029_does_not_fire():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    # Return = (971 - 1000) / 1000 = -0.029 (just above -0.03)
    proxy[-1] = _candle(dates[-1], "971")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-39000000000000") / 20
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-4700000000000") / 20
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is False


# ==========================================================================
# F-07: prior NOT stressed (> -20조) → exhaustion does not fire
# ==========================================================================

def test_f07_prior_not_stressed():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    proxy[-1] = _candle(dates[-1], "970")
    investor = [_investor(d, "0") for d in dates]
    # Prior: -19조 (above -20조 threshold → not stressed)
    prior_per = Decimal("-19000000000000") / 20
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-4700000000000") / 20
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is False
    assert pack.components[2].details["prior_stressed"] == "false"


# ==========================================================================
# F-08: current still stressed (current <= -20조) → fails > rule
# ==========================================================================

def test_f08_current_still_stressed():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    proxy[-1] = _candle(dates[-1], "970")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-40000000000000") / 20
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    # Current: -21조 (still <= -20조 → fails strict >)
    current_per = Decimal("-21000000000000") / 20
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is False
    assert pack.components[2].details["current_no_longer_stressed"] == "false"


# ==========================================================================
# F-12: decimal precision equality (canonical_decimal)
# ==========================================================================

def test_f12_decimal_precision_canonical_equality():
    from tokmo.values import canonical_decimal
    d1 = canonical_decimal(Decimal("9063.8"))
    d2 = canonical_decimal(Decimal("9063.80"))
    d3 = canonical_decimal(Decimal("9063.800"))
    assert d1 == d2 == d3 == "9063.8"


# ==========================================================================
# F-13: tie rule — percentile_rank is inclusive (max-rank)
# ==========================================================================

def test_f13_tie_rule_inclusive():
    """percentile_rank with ties: all equal values counted as <=."""
    window = [Decimal("100")] * 99 + [Decimal("200")]
    current = Decimal("100")
    pct = percentile_rank(window, current)
    assert pct == Decimal("0.99")


def test_f13b_percentile_all_same_values():
    """All 100 values identical → percentile = 100/100 = 1.0."""
    window = [Decimal("500")] * 100
    pct = percentile_rank(window, Decimal("500"))
    assert pct == Decimal("1")


# ==========================================================================
# Pack digest determinism (§6.6)
# ==========================================================================

def test_pack_digest_deterministic():
    fields = {"as_of": "2026-06-18T07:30:00.000000Z", "program_id": "AI-MACRO-001"}
    d1 = compute_pack_digest(fields)
    d2 = compute_pack_digest(fields)
    assert d1 == d2
    assert len(d1) == 64


def test_pack_digest_different_input_changes():
    assert compute_pack_digest({"a": "1"}) != compute_pack_digest({"a": "2"})


# ==========================================================================
# M1: microsecond serialization in digest (behavioral)
# ==========================================================================

def test_m1_different_microseconds_different_digest():
    """Different observed_at microseconds → different series_digest."""
    from tokmo.values.macro_observations import _canonical_utc_iso
    d = date(2026, 6, 18)
    dt_0us = datetime(2026, 6, 18, 6, 30, 0, 0, tzinfo=timezone.utc)
    dt_1us = datetime(2026, 6, 18, 6, 30, 0, 1, tzinfo=timezone.utc)
    assert _canonical_utc_iso(dt_0us) == "2026-06-18T06:30:00.000000Z"
    assert _canonical_utc_iso(dt_1us) == "2026-06-18T06:30:00.000001Z"

    c_0 = _candle(d, "1000")
    c_1 = _candle(d, "1000")
    object.__setattr__(c_0, "observed_at", dt_0us)
    object.__setattr__(c_1, "observed_at", dt_1us)
    assert compute_series_digest((c_0,)) != compute_series_digest((c_1,))


def test_m1_equivalent_instants_canonicalize_identically():
    """Same instant in different timezones → same canonical UTC string."""
    from tokmo.values.macro_observations import _canonical_utc_iso
    dt_utc = datetime(2026, 6, 18, 7, 30, 0, 500000, tzinfo=timezone.utc)
    dt_kst = datetime(2026, 6, 18, 16, 30, 0, 500000, tzinfo=KST)
    assert _canonical_utc_iso(dt_utc) == _canonical_utc_iso(dt_kst)


def test_m1_six_fractional_digits_always():
    from tokmo.values.macro_observations import _canonical_utc_iso
    for us in (0, 1, 100, 999999):
        dt = datetime(2026, 1, 1, 0, 0, 0, us, tzinfo=timezone.utc)
        s = _canonical_utc_iso(dt)
        frac = s.split(".")[1].rstrip("Z")
        assert len(frac) == 6


# ==========================================================================
# m1: 32KB aggregate cap with deterministic tail-drop
# ==========================================================================

def test_m1_byte_cap_tail_drop_deterministic():
    """compute_pack_digest with >32KB evidence tail-drops deterministically."""
    huge_evidence = tuple("x" * 400 for _ in range(100))
    fields = {
        "program_id": "test",
        "evidence": huge_evidence,
        "counter_evidence": (),
    }
    d1 = compute_pack_digest(fields)
    d2 = compute_pack_digest(fields)
    assert d1 == d2  # deterministic


def test_m1_byte_cap_produces_valid_output():
    """A huge pack still produces a valid 64-hex digest."""
    huge_evidence = tuple("y" * 400 for _ in range(200))
    fields = {"program_id": "test", "evidence": huge_evidence}
    d = compute_pack_digest(fields)
    assert len(d) == 64
    assert all(c in "0123456789abcdef" for c in d)


# ==========================================================================
# m2: serializer primitives — bool/int remain JSON primitives except in details
# ==========================================================================

def test_m2_top_level_int_stays_json_number():
    """A top-level int field serializes as a JSON number, not a string."""
    import json
    from tokmo.values.macro_divergence import _serialize_for_digest
    result = _serialize_for_digest({"count": 42})
    assert result["count"] == 42  # int, not str
    s = json.dumps(result, separators=(",", ":"))
    assert '"count":42' in s  # JSON number, not "42"


def test_m2_top_level_bool_stays_json_bool():
    import json
    from tokmo.values.macro_divergence import _serialize_for_digest
    result = _serialize_for_digest({"active": True})
    assert result["active"] is True
    s = json.dumps(result, separators=(",", ":"))
    assert '"active":true' in s


def test_m2_details_str_values_pass_through():
    """§6.1: details dict values are strings (enforced at construction).
    The serializer passes them through as JSON strings."""
    import json
    from tokmo.values.macro_divergence import _serialize_for_digest
    result = _serialize_for_digest({"details": {"fired": "true", "count": "5"}})
    s = json.dumps(result, separators=(",", ":"))
    assert '"fired":"true"' in s
    assert '"count":"5"' in s


def test_m2_dataclass_bool_field_stays_json_bool():
    """A frozen dataclass with a bool field serializes as JSON bool."""
    import json
    from dataclasses import dataclass
    from tokmo.values.macro_divergence import _serialize_for_digest

    @dataclass(frozen=True)
    class _Probe:
        flag: bool
        count: int

    result = _serialize_for_digest(_Probe(flag=True, count=7))
    s = json.dumps(result, separators=(",", ":"))
    assert '"flag":true' in s
    assert '"count":7' in s


# ==========================================================================
# m3: zero-close → distinct "not computable" reason
# ==========================================================================

def test_m3_zero_close_not_computable_reason():
    """When close_{T-7} is zero, the exhaustion reason says 'not computable'."""
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    proxy = [_candle(d, "1000") for d in dates]
    # Make close_{T-7} (position 92) = 0
    proxy[92] = _candle(dates[92], "0")
    investor = [_investor(d, "0") for d in dates]
    prior_per = Decimal("-40000000000000") / 20
    for i in range(60, 80):
        investor[i] = _investor(dates[i], str(prior_per))
    current_per = Decimal("-4700000000000") / 20
    for i in range(80, 100):
        investor[i] = _investor(dates[i], str(current_per))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.components[2].fired is False
    assert "not computable" in pack.components[2].reason
    assert pack.components[2].details["fx_return_8"] == "N/A"


# ==========================================================================
# m4: limitation date range wording
# ==========================================================================

def test_m4_limitation_date_range_wording():
    """The mandatory limitation text uses the Mar–Aug 2026 range form."""
    from tokmo.values.macro_divergence import _PACK_DEFAULT_LIMITATIONS
    regime_text = [l for l in _PACK_DEFAULT_LIMITATIONS if "regime sample" in l]
    assert len(regime_text) == 1
    assert "2026-03~2026-08" in regime_text[0]


# ==========================================================================
# Confidence ceiling mapping — real behavioral test for all 5 states
# ==========================================================================

def test_confidence_ceiling_mapping_all_states():
    """Verify map_confidence_ceiling for every MacroDivergenceState (§6.5)."""
    assert map_confidence_ceiling("INDEX_HIGH_FX_PROXY_HIGH") is ConfidenceCeiling.PROXY_ROLL_SENSITIVE
    assert map_confidence_ceiling("FOREIGN_OUTFLOW_CONFIRMATION") is ConfidenceCeiling.DIRECT_FLOW_DATA
    assert map_confidence_ceiling("OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL") is ConfidenceCeiling.PROXY_ROLL_SENSITIVE
    assert map_confidence_ceiling("INSUFFICIENT_DATA") is ConfidenceCeiling.NOT_APPLICABLE
    assert map_confidence_ceiling("NO_MATERIAL_DIVERGENCE") is ConfidenceCeiling.NOT_APPLICABLE


def test_confidence_ceiling_mapping_in_pack_output():
    """Verify the pack's confidence_ceiling field matches the mapping for T1."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_candle(d, "1000") for d in dates]
    kospi[-1] = _candle(dates[-1], "2000")
    proxy = list(kospi)
    investor = [_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    pack = _eval(bundle, as_of)
    assert pack.primary_state == "INDEX_HIGH_FX_PROXY_HIGH"
    assert pack.confidence_ceiling == "PROXY_ROLL_SENSITIVE"


# ==========================================================================
# Component V1 structure
# ==========================================================================

def test_component_v1_construction():
    c = MacroDivergenceComponentV1(
        component=MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
        fired=True,
        reason="test",
        details={"threshold": "0.90"},
    )
    assert c.fired is True


def test_component_v1_details_too_many_keys_rejects():
    with pytest.raises(ValueError):
        MacroDivergenceComponentV1(
            component=MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
            fired=True, reason="test",
            details={f"k{i}": str(i) for i in range(21)},
        )


def test_window_summary_v1_construction():
    ws = MacroWindowSummaryV1(
        as_of="2026-06-18T16:30:00+09:00",
        latest_common_session="2026-06-18",
        common_window_count=100,
        kospi_latest_close="9063.84", proxy_latest_close="1523",
        kospi_percentile="0.95", proxy_percentile="0.92",
        foreign_rolling_flow_20="-24000000000000",
        foreign_prior_flow_20="-40300000000000",
        fx_return_8="-0.033",
    )
    assert ws.common_window_count == 100


def test_symbol_contact_v1_construction():
    sc = MacroSymbolContactV1(
        symbol="261240", role="FX_PROXY",
        latest_session="2026-06-18", latest_close="1523",
    )
    assert sc.role == "FX_PROXY"
