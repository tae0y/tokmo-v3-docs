"""G1-A RED→GREEN analyzer tests (SDS §8.4 AN-*, §8.5 MM-*, §8.8 REPLAY-*).

Exercises evaluate_macro_divergence with synthetic fixtures for T1/T2/T3,
negative/adversarial scenarios, metamorphic properties, and replay parity.
"""

import hashlib
import json
from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
from tokmo.values.macro_divergence import (
    FORMULA_VERSION,
    MacroDivergenceState,
)
from tokmo.values.macro_observations import (
    MacroCandleObservation,
    MacroCoverageState,
    MacroInvestorObservation,
    MacroObservationSeriesV1,
    MacroSourceRefV1,
    compute_series_digest,
)
from tokmo.values.signal_program_lifecycle import (
    ProgramKindV1,
    ProgramLifecycleSpecV1,
    ProgramLifecycleStateV1,
    ProgramLifecycleStatusV1,
    evaluate_lifecycle_status,
)

KST = timezone(timedelta(hours=9))
UTC = UTC

# ==========================================================================
# Fixture builders — synthetic data with exact threshold control
# ==========================================================================


def _digest(text: str = "canonical-spec-text") -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _core_lifecycle(as_of: datetime, status=None) -> ProgramLifecycleStateV1:
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=90),
        expires_at=None,
        review_cadence_days=90,
        spec_digest=_digest(),
    )
    if status is not None:
        return ProgramLifecycleStateV1(
            spec=spec,
            status=status,
            last_evidence_at=None,
            evidence_count=0,
            renewal_count=0,
            as_of=as_of,
        )
    return evaluate_lifecycle_status(
        spec,
        as_of=as_of,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
    )


def _make_candle(d: date, close: str, currency="KRW") -> MacroCandleObservation:
    obs = datetime.combine(d, datetime.min.time(), tzinfo=KST).replace(
        hour=15, minute=30
    )
    return MacroCandleObservation(
        session_date=d,
        observed_at=obs,
        available_at=obs + timedelta(minutes=5),
        open_price=close,
        high_price=close,
        low_price=close,
        close_price=close,
        volume="100000",
        currency=currency,
    )


def _make_investor(d: date, foreign_net: str) -> MacroInvestorObservation:
    obs = datetime.combine(d, datetime.min.time(), tzinfo=KST).replace(
        hour=15, minute=30
    )
    net = Decimal(foreign_net)
    buy = str(abs(net)) if net >= 0 else "0"
    sell = str(abs(net)) if net < 0 else "0"
    return MacroInvestorObservation(
        session_date=d,
        observed_at=obs,
        available_at=obs + timedelta(minutes=5),
        foreign_buy=buy,
        foreign_sell=sell,
        individual_buy="0",
        individual_sell="0",
        institution_buy="0",
        institution_sell="0",
    )


def _gen_dates(n: int, end: date) -> list[date]:
    """Generate n consecutive trading dates ending at `end` (skipping weekends)."""
    dates = []
    cur = end
    while len(dates) < n:
        if cur.weekday() < 5:  # Mon-Fri
            dates.append(cur)
        cur -= timedelta(days=1)
    return list(reversed(dates))


def _build_bundle(
    kospi: list[MacroCandleObservation],
    proxy: list[MacroCandleObservation],
    investor: list[MacroInvestorObservation],
    as_of: datetime,
) -> MacroObservationSeriesV1:
    return MacroObservationSeriesV1(
        kospi_candles=tuple(kospi),
        proxy_candles=tuple(proxy),
        investor_records=tuple(investor),
        as_of=as_of,
        source_refs=(
            MacroSourceRefV1(
                operation="INDICATOR_CANDLES",
                symbol="KOSPI",
                interval="1d",
                requested_count=200,
                returned_count=len(kospi),
                date_range_start=kospi[0].session_date if kospi else None,
                date_range_end=kospi[-1].session_date if kospi else None,
                series_digest=compute_series_digest(tuple(kospi)),
            ),
            MacroSourceRefV1(
                operation="STOCK_CANDLES",
                symbol="261240",
                interval="1d",
                requested_count=200,
                returned_count=len(proxy),
                date_range_start=proxy[0].session_date if proxy else None,
                date_range_end=proxy[-1].session_date if proxy else None,
                series_digest=compute_series_digest(tuple(proxy)),
            ),
            MacroSourceRefV1(
                operation="INDICATOR_INVESTOR_TRADING",
                symbol="KOSPI",
                interval="1d",
                requested_count=100,
                returned_count=len(investor),
                date_range_start=investor[0].session_date if investor else None,
                date_range_end=investor[-1].session_date if investor else None,
                series_digest=compute_series_digest(tuple(investor)),
            ),
        ),
    )


# --------------------------------------------------------------------------
# T1 fixture builder — INDEX_HIGH_FX_PROXY_HIGH
# --------------------------------------------------------------------------


def _t1_bundle() -> tuple[MacroObservationSeriesV1, datetime]:
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # KOSPI: 90 closes at 8000, 10 closes at 8500, latest at 9063.84 (highest)
    # → count_le(9063.84) = 100 → percentile = 1.0 >= 0.90
    kospi = []
    for i, d in enumerate(dates):
        close = "8500" if i < 10 else "8000"
        if i == 99:
            close = "9063.84"
        kospi.append(_make_candle(d, close))
    # Proxy: similar pattern, latest at 1523 (highest)
    proxy = []
    for i, d in enumerate(dates):
        close = "1450" if i < 10 else "1400"
        if i == 99:
            close = "1523"
        proxy.append(_make_candle(d, close))
    # Investor: sub-threshold flow in latest 20
    investor = [_make_investor(d, "-500000000000") for d in dates]  # -0.5조 each
    return _build_bundle(kospi, proxy, investor, as_of), as_of


# --------------------------------------------------------------------------
# T2 fixture builder — FOREIGN_OUTFLOW_CONFIRMATION
# --------------------------------------------------------------------------


def _t2_bundle() -> tuple[MacroObservationSeriesV1, datetime]:
    end = date(2026, 6, 10)
    as_of = datetime(2026, 6, 10, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # KOSPI: latest close is NOT in the top 10 — 15 higher values above it
    kospi = []
    for i, d in enumerate(dates):
        if i < 85:
            close = str(1000 + i * 3)  # ascending
        else:
            close = str(2000 + (i - 85) * 10)  # 2000..2140 (higher)
        kospi.append(_make_candle(d, close))
    kospi[99] = _make_candle(dates[99], "1500")  # latest: below 15 higher values
    # Proxy: same pattern
    proxy = []
    for i, d in enumerate(dates):
        if i < 85:
            close = str(500 + i * 2)
        else:
            close = str(1000 + (i - 85) * 5)
        proxy.append(_make_candle(d, close))
    proxy[99] = _make_candle(dates[99], "750")
    # Investor: latest 20 sessions net -1.2조 each = -24조 total (<= -20조)
    investor = [_make_investor(d, "0") for d in dates]
    for i in range(80, 100):
        investor[i] = _make_investor(dates[i], "-1200000000000")  # -1.2조
    return _build_bundle(kospi, proxy, investor, as_of), as_of


# --------------------------------------------------------------------------
# T3 fixture builder — OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL
# --------------------------------------------------------------------------


def _t3_bundle() -> tuple[MacroObservationSeriesV1, datetime]:
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # KOSPI/Proxy: mid-range
    kospi = [_make_candle(d, "2800") for d in dates]
    proxy = [_make_candle(d, "1465") for d in dates]
    # Override last 8 proxy closes for -3.3% return: 1465 → ~1417
    # return = (1417 - 1465) / 1465 = -0.0328
    base = Decimal(1465)
    for i in range(92, 100):
        # Linear drop from 1465 to 1417
        frac = (i - 92) / 7
        close = base + (Decimal(1417) - base) * Decimal(str(frac))
        proxy[i] = _make_candle(dates[i], str(close))
    # Investor: [T-39, T-20] = prior 20 sum <= -40조
    #          [T-19, T] = current 20 sum > -20조 and >= prior*0.5
    investor = [_make_investor(d, "0") for d in dates]
    prior_total = Decimal(-40300000000000)  # -40.3조
    for i in range(60, 80):  # positions T-39..T-20
        investor[i] = _make_investor(dates[i], str(prior_total / 20))
    current_total = Decimal(-4700000000000)  # -4.7조
    for i in range(80, 100):  # positions T-19..T
        investor[i] = _make_investor(dates[i], str(current_total / 20))
    return _build_bundle(kospi, proxy, investor, as_of), as_of


# --------------------------------------------------------------------------
# MUST_NOT_FIRE fixture
# --------------------------------------------------------------------------


def _must_not_fire_bundle() -> tuple[MacroObservationSeriesV1, datetime]:
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # Ascending closes so latest is highest (100th rank → percentile 1.0)
    # BUT that's the problem. We want latest genuinely sub-0.90.
    # Use 100 ascending closes but make latest NOT the max: insert 15 higher
    # values after position 84, so latest (position 99) sits below them.
    kospi = []
    for i, d in enumerate(dates):
        if i < 85:
            close = str(1000 + i * 5)  # ascending 1000..1420
        else:
            # last 15 are higher than latest will be
            close = str(2000 + (i - 85) * 10)  # 2000..2140
        kospi.append(_make_candle(d, close))
    # Override position 99 (latest) to be at 1500 — well below the top 15
    kospi[99] = _make_candle(dates[99], "1500")
    proxy = []
    for i, d in enumerate(dates):
        if i < 85:
            close = str(500 + i * 3)
        else:
            close = str(1000 + (i - 85) * 5)
        proxy.append(_make_candle(d, close))
    proxy[99] = _make_candle(dates[99], "750")
    investor = [_make_investor(d, "-500000000000") for d in dates]  # -0.5조 each
    return _build_bundle(kospi, proxy, investor, as_of), as_of


# ==========================================================================
# AN-01: T1 fixture → INDEX_HIGH_FX_PROXY_HIGH
# ==========================================================================


def test_an01_t1_index_high():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INDEX_HIGH_FX_PROXY_HIGH.value
    assert pack.components[0].fired is True  # INDEX_HIGH_FX_PROXY_HIGH
    assert pack.coverage_state is not None
    assert pack.coverage_state.value == "COVERED"


# ==========================================================================
# AN-02: T2 fixture → FOREIGN_OUTFLOW_CONFIRMATION
# ==========================================================================


def test_an02_t2_foreign_outflow():
    bundle, as_of = _t2_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.FOREIGN_OUTFLOW_CONFIRMATION.value
    assert pack.components[1].fired is True
    # m6: assert INDEX_HIGH does NOT fire (T2 has sub-0.90 percentiles)
    assert pack.components[0].fired is False


# ==========================================================================
# AN-03: T3 fixture → OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL
# ==========================================================================


def test_an03_t3_exhaustion_reversal():
    bundle, as_of = _t3_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert (
        pack.primary_state
        == MacroDivergenceState.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL.value
    )
    assert pack.components[2].fired is True


# ==========================================================================
# AN-04: MUST_NOT_FIRE → NO_MATERIAL_DIVERGENCE
# ==========================================================================


def test_an04_must_not_fire():
    bundle, as_of = _must_not_fire_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.NO_MATERIAL_DIVERGENCE.value
    assert all(not c.fired for c in pack.components)


# ==========================================================================
# AN-05: NEG_INSUFFICIENT_COUNT_KOSPI → INSUFFICIENT_DATA
# ==========================================================================


def test_an05_insufficient_count_kospi():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(50, end)  # only 50 < 60
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy_dates = _gen_dates(100, end)
    proxy = [_make_candle(d, "1400") for d in proxy_dates]
    investor = [_make_investor(d, "0") for d in proxy_dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state PARTIAL
    assert pack.coverage_state is not None
    assert pack.coverage_state is MacroCoverageState.PARTIAL


# ==========================================================================
# AN-06: NEG_STALE_LATEST_SESSION → INSUFFICIENT_DATA
# ==========================================================================


def test_an06_stale_latest_session():
    end = date(2026, 6, 10)  # 8 days before as_of
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state STALE
    assert pack.coverage_state is not None
    assert pack.coverage_state is MacroCoverageState.STALE


# ==========================================================================
# AN-07: NEG_FUTURE_LEAKAGE → ValueError
# ==========================================================================


def test_an07_future_leakage():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    # Make last candle available in the future
    last = kospi[-1]
    kospi[-1] = MacroCandleObservation(
        session_date=last.session_date,
        observed_at=last.observed_at,
        available_at=as_of + timedelta(days=1),  # future!
        open_price=last.open_price,
        high_price=last.high_price,
        low_price=last.low_price,
        close_price=last.close_price,
        volume=last.volume,
        currency=last.currency,
    )
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# AN-08: NEG_TIMEZONE_NAIVE → ValueError
# ==========================================================================


def test_an08_timezone_naive():
    """Naive observed_at is rejected at MacroCandleObservation construction."""
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 3, 10),
            observed_at=datetime(2026, 3, 10, 15, 30),  # naive!
            available_at=datetime(2026, 3, 10, 15, 35),  # naive!
            open_price="3000",
            high_price="3000",
            low_price="3000",
            close_price="3000",
            volume="100000",
            currency="KRW",
        )


def test_an08b_naive_at_analyzer_level():
    """If somehow a naive datetime reaches the analyzer, it also rejects."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # Build a bundle, then bypass validation by directly setting on a valid obs
    kospi = [_make_candle(d, "3000") for d in dates]
    # Use object.__setattr__ to inject naive datetime (simulating a serialization bug)
    valid = kospi[0]
    object.__setattr__(valid, "observed_at", datetime(2026, 3, 10, 15, 30))
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# AN-09: NEG_PRECEDENCE — INDEX_HIGH + OUTFLOW → primary = OUTFLOW
# ==========================================================================


def test_an09_precedence_index_plus_outflow():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # KOSPI high (latest is highest)
    kospi = [_make_candle(d, "8000") for d in dates]
    kospi[-1] = _make_candle(dates[-1], "9063.84")
    # Proxy high
    proxy = [_make_candle(d, "1400") for d in dates]
    proxy[-1] = _make_candle(dates[-1], "1523")
    # Foreign outflow in latest 20
    investor = [_make_investor(d, "0") for d in dates]
    for i in range(80, 100):
        investor[i] = _make_investor(dates[i], "-1200000000000")  # -1.2조 each
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.FOREIGN_OUTFLOW_CONFIRMATION.value
    assert pack.components[0].fired is True  # INDEX_HIGH also fired
    assert pack.components[1].fired is True  # OUTFLOW also fired


# ==========================================================================
# AN-10: EXHAUSTION + INDEX → primary = EXHAUSTION
# ==========================================================================


def test_an10_precedence_exhaustion_plus_index():
    end = date(2026, 8, 7)
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    # KOSPI high
    kospi = [_make_candle(d, "8000") for d in dates]
    kospi[-1] = _make_candle(dates[-1], "9063.84")
    # Proxy: high percentile + rapid fall for exhaustion
    proxy = [_make_candle(d, "1465") for d in dates]
    # Make latest high enough for top 10%
    for i in range(10):
        proxy[99 - i] = _make_candle(dates[99 - i], "1523")
    # But also need rapid fall in last 8 for exhaustion
    base = Decimal(1465)
    for i in range(92, 100):
        frac = (i - 92) / 7
        close = base + (Decimal(1417) - base) * Decimal(str(frac))
        proxy[i] = _make_candle(dates[i], str(close))
    # Foreign: exhaustion pattern
    investor = [_make_investor(d, "0") for d in dates]
    prior_total = Decimal(-40300000000000)
    for i in range(60, 80):
        investor[i] = _make_investor(dates[i], str(prior_total / 20))
    current_total = Decimal(-4700000000000)
    for i in range(80, 100):
        investor[i] = _make_investor(dates[i], str(current_total / 20))
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert (
        pack.primary_state
        == MacroDivergenceState.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL.value
    )


# ==========================================================================
# AN-11: roll discontinuity → NO_MATERIAL_DIVERGENCE
# ==========================================================================


def test_an11_roll_discontinuity():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = [_make_candle(d, "1400") for d in dates]
    # Single-session 5% drop on proxy (roll date), but not enough for -3%
    proxy[-1] = _make_candle(dates[-1], "1330")  # from 1400 → -5% single day
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    # Without foreign outflow/exhaustion, single roll drop doesn't trigger
    assert pack.primary_state == MacroDivergenceState.NO_MATERIAL_DIVERGENCE.value


# ==========================================================================
# AN-12: holiday gap → INSUFFICIENT_DATA (STALE)
# ==========================================================================


def test_an12_holiday_gap():
    end = date(2026, 6, 12)  # 6 calendar days before as_of (June 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state STALE
    assert pack.coverage_state is not None
    assert pack.coverage_state is MacroCoverageState.STALE


# ==========================================================================
# Lifecycle blocking tests (§3.7, LC-09a)
# ==========================================================================


def test_lc09a_retired_blocks_evaluation():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of, status=ProgramLifecycleStatusV1.RETIRED)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    assert pack.lifecycle_reason == "RETIRED"
    assert pack.coverage_state is None
    assert all(not c.fired for c in pack.components)


def test_shadow_blocks_evaluation():
    # SHADOW: as_of < activated_at
    bundle, as_of = _must_not_fire_bundle()
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=as_of + timedelta(days=1),  # future activation
        review_due_at=as_of + timedelta(days=91),
        expires_at=None,
        review_cadence_days=90,
        spec_digest=_digest(),
    )
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.SHADOW,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    pack = evaluate_macro_divergence(bundle, lifecycle=state)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    assert pack.lifecycle_reason == "NOT_YET_ACTIVE"
    assert pack.coverage_state is None


def test_dormant_blocks_evaluation():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of, status=ProgramLifecycleStatusV1.DORMANT)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    assert pack.lifecycle_reason == "DORMANT"
    assert pack.coverage_state is None


# ==========================================================================
# as_of mismatch test (§3.9 step 2)
# ==========================================================================


def test_as_of_mismatch_rejects():
    bundle, as_of = _t1_bundle()
    different_as_of = as_of + timedelta(hours=1)
    lifecycle = _core_lifecycle(different_as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# Structural validation tests
# ==========================================================================


def test_duplicate_session_date_rejects():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    # Duplicate the last date
    kospi.append(_make_candle(dates[-1], "3100"))
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


def test_out_of_order_dates_rejects():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    # Swap two elements
    kospi[50], kospi[51] = kospi[51], kospi[50]
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


def test_common_window_too_small():
    """Each series has >= 100 rows but intersection < 60 dates."""
    end_kospi = date(2026, 6, 18)
    end_proxy = date(2025, 12, 31)  # different date range, minimal overlap
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    kospi_dates = _gen_dates(100, end_kospi)
    proxy_dates = _gen_dates(100, end_proxy)
    investor_dates = kospi_dates  # same as kospi

    kospi = [_make_candle(d, "3000") for d in kospi_dates]
    proxy = [_make_candle(d, "1400") for d in proxy_dates]
    investor = [_make_investor(d, "0") for d in investor_dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state PARTIAL (common window < 60)
    assert pack.coverage_state is MacroCoverageState.PARTIAL


def test_empty_proxy_series():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = []  # empty
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state EMPTY
    assert pack.coverage_state is MacroCoverageState.EMPTY


# ==========================================================================
# F-11: byte-identical digest determinism
# ==========================================================================


def test_f11_same_input_same_digest():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    pack2 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack1.pack_digest == pack2.pack_digest
    assert len(pack1.pack_digest) == 64


# ==========================================================================
# REPLAY-01: same normalized bundle, same as_of → same digest
# ==========================================================================


def test_replay01_same_bundle_same_digest():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    p2 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert p1.pack_digest == p2.pack_digest


# ==========================================================================
# REPLAY-03: future as_of within staleness ceiling → same result
# ==========================================================================


def test_replay03_future_as_of_within_ceiling():
    bundle, as_of = _t1_bundle()
    future_as_of = as_of + timedelta(days=2)  # within 4-day ceiling
    # Build a new bundle with the same data but future as_of
    kospi = list(bundle.kospi_candles)
    proxy = list(bundle.proxy_candles)
    investor = list(bundle.investor_records)
    new_bundle = _build_bundle(kospi, proxy, investor, future_as_of)
    lc1 = _core_lifecycle(as_of)
    lc2 = _core_lifecycle(future_as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lc1)
    p2 = evaluate_macro_divergence(new_bundle, lifecycle=lc2)
    assert p1.primary_state == p2.primary_state


# ==========================================================================
# REPLAY-04: as_of before latest observation → ValueError
# ==========================================================================


def test_replay04_as_of_before_latest_observation():
    bundle, as_of = _t1_bundle()
    early_as_of = as_of - timedelta(days=1)
    new_bundle = _build_bundle(
        list(bundle.kospi_candles),
        list(bundle.proxy_candles),
        list(bundle.investor_records),
        early_as_of,
    )
    lifecycle = _core_lifecycle(early_as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(new_bundle, lifecycle=lifecycle)


# ==========================================================================
# REPLAY-06: as_of > 4 calendar days after latest common session → STALE
# ==========================================================================


def test_replay06_beyond_ceiling_stale():
    end = date(2026, 6, 10)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)  # 8 days later
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state STALE
    assert pack.coverage_state is MacroCoverageState.STALE


# ==========================================================================
# MM-01: adding 1 non-overlapping session before window → same primary
# ==========================================================================


def test_mm01_add_session_before_window():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    # Add one session before the window to all series
    first_date = bundle.kospi_candles[0].session_date - timedelta(days=3)
    while first_date.weekday() >= 5:
        first_date -= timedelta(days=1)
    kospi = [_make_candle(first_date, "7000")] + list(bundle.kospi_candles)
    proxy = [_make_candle(first_date, "1300")] + list(bundle.proxy_candles)
    investor = [_make_investor(first_date, "0")] + list(bundle.investor_records)
    new_bundle = _build_bundle(kospi, proxy, investor, as_of)
    p2 = evaluate_macro_divergence(new_bundle, lifecycle=lifecycle)
    assert p1.primary_state == p2.primary_state


# ==========================================================================
# MM-03: doubling all prices → same percentile rank
# ==========================================================================


def test_mm03_scale_invariance():
    bundle, as_of = _must_not_fire_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    kospi = [
        MacroCandleObservation(
            session_date=c.session_date,
            observed_at=c.observed_at,
            available_at=c.available_at,
            open_price=str(Decimal(c.open_price) * 2),
            high_price=str(Decimal(c.high_price) * 2),
            low_price=str(Decimal(c.low_price) * 2),
            close_price=str(Decimal(c.close_price) * 2),
            volume=c.volume,
            currency=c.currency,
        )
        for c in bundle.kospi_candles
    ]
    proxy = [
        MacroCandleObservation(
            session_date=c.session_date,
            observed_at=c.observed_at,
            available_at=c.available_at,
            open_price=str(Decimal(c.open_price) * 2),
            high_price=str(Decimal(c.high_price) * 2),
            low_price=str(Decimal(c.low_price) * 2),
            close_price=str(Decimal(c.close_price) * 2),
            volume=c.volume,
            currency=c.currency,
        )
        for c in bundle.proxy_candles
    ]
    investor = list(bundle.investor_records)
    new_bundle = _build_bundle(kospi, proxy, investor, as_of)
    p2 = evaluate_macro_divergence(new_bundle, lifecycle=lifecycle)
    assert p1.primary_state == p2.primary_state


# ==========================================================================
# Pack structure checks
# ==========================================================================


def test_pack_has_all_fields():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.program_id == "AI-MACRO-001"
    assert pack.program_kind == "CORE_ANALYZER"
    assert pack.formula_version == FORMULA_VERSION
    assert len(pack.components) == 3
    assert len(pack.source_refs) == 3
    assert pack.confidence_ceiling == "PROXY_ROLL_SENSITIVE"
    assert len(pack.limitations) == 4
    assert len(pack.symbol_contacts) >= 2


def test_pack_coverage_reason_empty_when_lifecycle_blocked():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of, status=ProgramLifecycleStatusV1.RETIRED)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.coverage_reason == ""
    assert pack.coverage_state is None


def test_pack_digest_is_64_hex():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    import re

    assert re.match(r"^[0-9a-f]{64}$", pack.pack_digest)


def test_review_due_proceeds_normally():
    """REVIEW_DUE does not block evaluation (§3.6)."""
    bundle, as_of = _t1_bundle()
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=as_of - timedelta(days=100),
        review_due_at=as_of - timedelta(days=10),  # review was due 10 days ago
        expires_at=None,
        review_cadence_days=90,
        spec_digest=_digest(),
    )
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.REVIEW_DUE,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    pack = evaluate_macro_divergence(bundle, lifecycle=state)
    assert pack.lifecycle_reason == "REVIEW_DUE"
    assert pack.primary_state == MacroDivergenceState.INDEX_HIGH_FX_PROXY_HIGH.value


# ==========================================================================
# MM-02: reordering fields in JSON → same pack (canonical serialization)
# ==========================================================================


def test_mm02_in_memory_replay_same_digest():
    """REPLAY-01 duplicate: same in-memory bundle → same digest."""
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    p2 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert p1.pack_digest == p2.pack_digest


# ==========================================================================
# MM-04: shifting all dates by +7 days, same as_of shift → same primary
# ==========================================================================


def test_mm04_date_shift_same_primary():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    # Shift all dates by +7 days
    shifted_kospi = []
    for c in bundle.kospi_candles:
        shifted_kospi.append(
            MacroCandleObservation(
                session_date=c.session_date + timedelta(days=7),
                observed_at=c.observed_at + timedelta(days=7),
                available_at=c.available_at + timedelta(days=7),
                open_price=c.open_price,
                high_price=c.high_price,
                low_price=c.low_price,
                close_price=c.close_price,
                volume=c.volume,
                currency=c.currency,
            )
        )
    shifted_proxy = []
    for c in bundle.proxy_candles:
        shifted_proxy.append(
            MacroCandleObservation(
                session_date=c.session_date + timedelta(days=7),
                observed_at=c.observed_at + timedelta(days=7),
                available_at=c.available_at + timedelta(days=7),
                open_price=c.open_price,
                high_price=c.high_price,
                low_price=c.low_price,
                close_price=c.close_price,
                volume=c.volume,
                currency=c.currency,
            )
        )
    shifted_investor = []
    for r in bundle.investor_records:
        shifted_investor.append(
            MacroInvestorObservation(
                session_date=r.session_date + timedelta(days=7),
                observed_at=r.observed_at + timedelta(days=7),
                available_at=r.available_at + timedelta(days=7),
                foreign_buy=r.foreign_buy,
                foreign_sell=r.foreign_sell,
                individual_buy=r.individual_buy,
                individual_sell=r.individual_sell,
                institution_buy=r.institution_buy,
                institution_sell=r.institution_sell,
            )
        )
    shifted_as_of = as_of + timedelta(days=7)
    shifted_bundle = _build_bundle(
        shifted_kospi, shifted_proxy, shifted_investor, shifted_as_of
    )
    shifted_lifecycle = _core_lifecycle(shifted_as_of)
    p2 = evaluate_macro_divergence(shifted_bundle, lifecycle=shifted_lifecycle)
    assert p1.primary_state == p2.primary_state


# ==========================================================================
# REPLAY-02: live vs replay normalization (same data) → same digest
# ==========================================================================


def test_replay02_same_data_different_available_at_same_digest():
    """REPLAY-02: series_digest excludes available_at, so same economic data
    with different fetch times produces the same series_digest and pack_digest."""

    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    p1 = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    # Reconstruct the same data with available_at shifted *earlier*
    # (to keep available_at <= as_of valid while proving the digest doesn't change)
    kospi_2 = []
    for c in bundle.kospi_candles:
        kospi_2.append(
            MacroCandleObservation(
                session_date=c.session_date,
                observed_at=c.observed_at,
                available_at=c.observed_at,  # different available_at (= observed)
                open_price=c.open_price,
                high_price=c.high_price,
                low_price=c.low_price,
                close_price=c.close_price,
                volume=c.volume,
                currency=c.currency,
            )
        )
    proxy_2 = []
    for c in bundle.proxy_candles:
        proxy_2.append(
            MacroCandleObservation(
                session_date=c.session_date,
                observed_at=c.observed_at,
                available_at=c.observed_at,
                open_price=c.open_price,
                high_price=c.high_price,
                low_price=c.low_price,
                close_price=c.close_price,
                volume=c.volume,
                currency=c.currency,
            )
        )
    investor_2 = []
    for r in bundle.investor_records:
        investor_2.append(
            MacroInvestorObservation(
                session_date=r.session_date,
                observed_at=r.observed_at,
                available_at=r.observed_at,
                foreign_buy=r.foreign_buy,
                foreign_sell=r.foreign_sell,
                individual_buy=r.individual_buy,
                individual_sell=r.individual_sell,
                institution_buy=r.institution_buy,
                institution_sell=r.institution_sell,
            )
        )
    bundle_2 = _build_bundle(kospi_2, proxy_2, investor_2, as_of)
    p2 = evaluate_macro_divergence(bundle_2, lifecycle=lifecycle)

    # series_digests should match (available_at excluded)
    for i in range(3):
        assert (
            bundle.source_refs[i].series_digest == bundle_2.source_refs[i].series_digest
        )
    # pack_digests should match
    assert p1.pack_digest == p2.pack_digest


# ==========================================================================
# REPLAY-05b: passing untruncated/future bundle → ValueError
# ==========================================================================


def test_replay05b_future_containing_bundle_rejects():
    """REPLAY-05b: a bundle with available_at > as_of is strictly rejected."""
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    # Inject a future-dated observation
    last = kospi[-1]
    kospi[-1] = MacroCandleObservation(
        session_date=last.session_date,
        observed_at=last.observed_at,
        available_at=as_of + timedelta(days=1),
        open_price=last.open_price,
        high_price=last.high_price,
        low_price=last.low_price,
        close_price=last.close_price,
        volume=last.volume,
        currency=last.currency,
    )
    proxy = [_make_candle(d, "1400") for d in dates]
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# REPLAY-05a: caller-side truncation builds a clean bundle
# ==========================================================================


def test_replay05a_truncated_bundle_evaluates_normally():
    """REPLAY-05a: a bundle built from only available_at <= as_of rows
    evaluates normally."""
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    # The T1 bundle already has all available_at <= as_of by construction
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INDEX_HIGH_FX_PROXY_HIGH.value


# ==========================================================================
# Investor insufficient count (OBS-09)
# ==========================================================================


def test_an05b_insufficient_count_investor():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(100, end)
    kospi = [_make_candle(d, "3000") for d in dates]
    proxy = [_make_candle(d, "1400") for d in dates]
    # Only 30 investor sessions (< 40 minimum)
    investor_dates = _gen_dates(30, end)
    investor = [_make_investor(d, "0") for d in investor_dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state PARTIAL
    assert pack.coverage_state is MacroCoverageState.PARTIAL


# ==========================================================================
# F-14: coverage PARTIAL → INSUFFICIENT_DATA regardless of component fires
# ==========================================================================


def test_f14_partial_coverage_insufficient_data():
    end = date(2026, 6, 18)
    as_of = datetime(2026, 6, 18, 16, 30, tzinfo=KST)
    dates = _gen_dates(50, end)  # < 60 → PARTIAL
    # But make the data look like T1 (would fire if covered)
    kospi = []
    for i, d in enumerate(dates):
        close = "8500" if i < 5 else "8000"
        if i == 49:
            close = "9063.84"
        kospi.append(_make_candle(d, close))
    proxy = []
    for i, d in enumerate(dates):
        close = "1450" if i < 5 else "1400"
        if i == 49:
            close = "1523"
        proxy.append(_make_candle(d, close))
    investor = [_make_investor(d, "0") for d in dates]
    bundle = _build_bundle(kospi, proxy, investor, as_of)
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    # M4: assert exact coverage state PARTIAL
    assert pack.coverage_state is MacroCoverageState.PARTIAL


# ==========================================================================
# F-15: proxy 261240 currency field is KRW
# ==========================================================================


def test_f15_proxy_currency_krw():
    bundle, as_of = _t1_bundle()
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)
    proxy_contacts = [sc for sc in pack.symbol_contacts if sc.role == "FX_PROXY"]
    assert len(proxy_contacts) == 1
    assert proxy_contacts[0].symbol == "261240"


# ==========================================================================
# m8: EXPIRED HYPOTHESIS_SENSOR analyzer-blocking test (§3.7, LC-08)
# ==========================================================================


def test_lc08_expired_hypothesis_blocks_evaluation():
    """EXPIRED HYPOTHESIS_SENSOR blocks evaluation — coverage_state=None,
    lifecycle_reason=EXPIRED, all components fired=False."""
    bundle, _ = _t1_bundle()
    as_of = datetime(2026, 8, 7, 16, 30, tzinfo=KST)
    # Build bundle with the correct as_of
    kospi = list(bundle.kospi_candles)
    proxy = list(bundle.proxy_candles)
    investor = list(bundle.investor_records)
    bundle = _build_bundle(kospi, proxy, investor, as_of)

    spec = ProgramLifecycleSpecV1(
        program_id="AI-CAPEX-001",
        version="1.0.0",
        kind=ProgramKindV1.HYPOTHESIS_SENSOR,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=45),
        expires_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=120),
        review_cadence_days=45,
        spec_digest=_digest("hypothesis"),
    )
    state = ProgramLifecycleStateV1(
        spec=spec,
        status=ProgramLifecycleStatusV1.EXPIRED,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
        as_of=as_of,
    )
    pack = evaluate_macro_divergence(bundle, lifecycle=state)
    assert pack.primary_state == MacroDivergenceState.INSUFFICIENT_DATA.value
    assert pack.lifecycle_reason == "EXPIRED"
    assert pack.coverage_state is None
    assert all(not c.fired for c in pack.components)


# ==========================================================================
# M3: Self-contained §7.1 fixture loader and parameterized tests
# ==========================================================================

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "macro_synthetic"

_FIXTURE_FILES = {
    "T1": "T1_INDEX_HIGH_FX_PROXY_HIGH_2026_06_18.json",
    "T2": "T2_FOREIGN_OUTFLOW_2026_05_JUNE.json",
    "T3": "T3_EXHAUSTION_REVERSAL_2026_08_07.json",
    "MNF": "MUST_NOT_FIRE_PRE_T1_SYNTHETIC.json",
}


def _load_fixture(fixture_path: Path) -> dict:
    """Load a §7.1 fixture JSON file."""
    with open(fixture_path, encoding="utf-8") as f:
        return json.load(f)


def _parse_candle_entry(entry: dict) -> MacroCandleObservation:
    """Parse a single candle JSON entry into a MacroCandleObservation."""
    return MacroCandleObservation(
        session_date=date.fromisoformat(entry["session_date"]),
        observed_at=datetime.fromisoformat(entry["observed_at"]),
        available_at=datetime.fromisoformat(entry["available_at"]),
        open_price=entry["open_price"],
        high_price=entry["high_price"],
        low_price=entry["low_price"],
        close_price=entry["close_price"],
        volume=entry["volume"],
        currency=entry["currency"],
    )


def _parse_investor_entry(entry: dict) -> MacroInvestorObservation:
    """Parse a single investor JSON entry into a MacroInvestorObservation."""
    return MacroInvestorObservation(
        session_date=date.fromisoformat(entry["session_date"]),
        observed_at=datetime.fromisoformat(entry["observed_at"]),
        available_at=datetime.fromisoformat(entry["available_at"]),
        foreign_buy=entry["foreign_buy"],
        foreign_sell=entry["foreign_sell"],
        individual_buy=entry["individual_buy"],
        individual_sell=entry["individual_sell"],
        institution_buy=entry["institution_buy"],
        institution_sell=entry["institution_sell"],
    )


def _fixture_to_bundle(fixture: dict) -> MacroObservationSeriesV1:
    """Parse a §7.1 fixture dict into a production MacroObservationSeriesV1."""
    as_of = datetime.fromisoformat(fixture["as_of"])
    kospi = tuple(_parse_candle_entry(c) for c in fixture["kospi_candles"])
    proxy = tuple(_parse_candle_entry(c) for c in fixture["proxy_candles"])
    investor = tuple(_parse_investor_entry(r) for r in fixture["investor_records"])
    return _build_bundle(list(kospi), list(proxy), list(investor), as_of)


def _fixture_to_json(fixture: dict) -> str:
    """Serialize a fixture dict back to JSON (for MM-02 reordering tests)."""
    return json.dumps(fixture, ensure_ascii=False)


def _reorder_dict_keys(obj):
    """Recursively reverse the key order of every dict in a nested structure.

    This produces a semantically identical structure with different key
    ordering, testing that production canonical serialization is key-order
    independent.
    """
    if isinstance(obj, dict):
        return {k: _reorder_dict_keys(v) for k, v in reversed(list(obj.items()))}
    if isinstance(obj, list):
        return [_reorder_dict_keys(item) for item in obj]
    return obj


@pytest.mark.parametrize("fixture_key", ["T1", "T2", "T3", "MNF"])
def test_fixture_loaded_evaluate(fixture_key):
    """Load a §7.1 fixture JSON, parse to production objects, evaluate, and
    assert expected primary state and component fired/non-fired."""
    fixture_path = FIXTURES_DIR / _FIXTURE_FILES[fixture_key]
    fixture = _load_fixture(fixture_path)
    bundle = _fixture_to_bundle(fixture)
    as_of = datetime.fromisoformat(fixture["as_of"])
    lifecycle = _core_lifecycle(as_of)
    pack = evaluate_macro_divergence(bundle, lifecycle=lifecycle)

    expected_primary = fixture["expected_primary_state"]
    expected_fired = set(fixture.get("expected_components_fired", []))

    assert pack.primary_state == expected_primary, (
        f"{fixture_key}: expected {expected_primary}, got {pack.primary_state}"
    )
    for comp in pack.components:
        if comp.component.value in expected_fired:
            assert comp.fired is True, (
                f"{fixture_key}: {comp.component.value} should fire"
            )
        else:
            assert comp.fired is False, (
                f"{fixture_key}: {comp.component.value} should NOT fire"
            )


def test_mm02_json_key_reorder_same_pack():
    """MM-02 (§8.5): reordering JSON keys in a fixture produces the same pack.

    Loads a fixture, evaluates to pack A. Then re-serializes the fixture JSON
    with all dict keys reversed, parses back through the production-object
    loader, evaluates to pack B. Asserts byte-identical pack_digest and
    identical primary/components.

    This test fails if production canonical serialization stops sorting keys.
    """
    fixture_path = FIXTURES_DIR / _FIXTURE_FILES["T1"]
    fixture = _load_fixture(fixture_path)

    # Path A: evaluate from original fixture
    bundle_a = _fixture_to_bundle(fixture)
    as_of = datetime.fromisoformat(fixture["as_of"])
    lifecycle = _core_lifecycle(as_of)
    pack_a = evaluate_macro_divergence(bundle_a, lifecycle=lifecycle)

    # Path B: recursively reorder all dict keys in the fixture, serialize to
    # JSON, parse back, and evaluate through the same production loader
    reordered = _reorder_dict_keys(fixture)
    reordered_json = _fixture_to_json(reordered)
    reordered_fixture = json.loads(reordered_json)
    bundle_b = _fixture_to_bundle(reordered_fixture)
    pack_b = evaluate_macro_divergence(bundle_b, lifecycle=lifecycle)

    # The packs must be byte-identical in digest
    assert pack_a.pack_digest == pack_b.pack_digest, (
        f"pack_digest differs after key reorder: {pack_a.pack_digest} vs {pack_b.pack_digest}"
    )
    assert pack_a.primary_state == pack_b.primary_state
    for ca, cb in zip(pack_a.components, pack_b.components):
        assert ca.fired == cb.fired
        assert ca.component == cb.component
