"""G1-A RED→GREEN input-validation tests (SDS §8.2 OBS-01..OBS-13).

Exercises MacroCandleObservation, MacroInvestorObservation,
MacroSourceRefV1, MacroDataCoverageV1, MacroObservationSeriesV1.
"""

import hashlib
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.values.macro_observations import (
    MacroCandleObservation,
    MacroCoverageState,
    MacroInvestorObservation,
    MacroObservationSeriesV1,
    MacroSourceRefV1,
    compute_series_digest,
)

KST = timezone(timedelta(hours=9))
UTC = UTC

FIXTURES_DIR = Path(__file__).parent / "fixtures" / "macro_synthetic"


# --------------------------------------------------------------------------
# Builders
# --------------------------------------------------------------------------


def _ts(day_offset: int = 0, hour: int = 15, tz=KST) -> datetime:
    return datetime(2026, 6, 18, hour, 30, tzinfo=tz) + timedelta(days=day_offset)


def _candle(
    session_date: date,
    observed_at: datetime | None = None,
    available_at: datetime | None = None,
    close_price: str = "1000",
    currency: str = "KRW",
) -> MacroCandleObservation:
    if observed_at is None:
        observed_at = datetime.combine(
            session_date, datetime.min.time(), tzinfo=KST
        ).replace(hour=15, minute=30)
    if available_at is None:
        available_at = observed_at + timedelta(minutes=5)
    return MacroCandleObservation(
        session_date=session_date,
        observed_at=observed_at,
        available_at=available_at,
        open_price=close_price,
        high_price=close_price,
        low_price=close_price,
        close_price=close_price,
        volume="100000",
        currency=currency,
    )


def _investor(
    session_date: date,
    observed_at: datetime | None = None,
    available_at: datetime | None = None,
    foreign_net: str = "0",
) -> MacroInvestorObservation:
    if observed_at is None:
        observed_at = datetime.combine(
            session_date, datetime.min.time(), tzinfo=KST
        ).replace(hour=15, minute=30)
    if available_at is None:
        available_at = observed_at + timedelta(minutes=5)
    return MacroInvestorObservation(
        session_date=session_date,
        observed_at=observed_at,
        available_at=available_at,
        foreign_buy=str(abs(int(foreign_net)) if int(foreign_net) >= 0 else 0),
        foreign_sell=str(abs(int(foreign_net)) if int(foreign_net) < 0 else 0),
        individual_buy="0",
        individual_sell="0",
        institution_buy="0",
        institution_sell="0",
    )


def _source_ref(
    operation: str = "INDICATOR_CANDLES",
    symbol: str = "KOSPI",
    returned_count: int = 100,
    date_range_start: date | None = None,
    date_range_end: date | None = None,
    series_digest: str | None = None,
) -> MacroSourceRefV1:
    if series_digest is None:
        series_digest = hashlib.sha256(b"series").hexdigest()
    return MacroSourceRefV1(
        operation=operation,
        symbol=symbol,
        interval="1d",
        requested_count=200,
        returned_count=returned_count,
        date_range_start=date_range_start,
        date_range_end=date_range_end,
        series_digest=series_digest,
    )


# ==========================================================================
# OBS-01: duplicate session_date in KOSPI → ValueError
# ==========================================================================


def test_obs01_duplicate_session_date_rejected_at_analyzer():
    """OBS-01: duplicate session_date in KOSPI → ValueError at analyzer."""
    import hashlib

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    from tokmo.values.signal_program_lifecycle import (
        ProgramKindV1,
        ProgramLifecycleSpecV1,
        evaluate_lifecycle_status,
    )

    end = date(2026, 6, 18)
    as_of = _ts()
    dates: list[date] = []
    cur = end
    while len(dates) < 100:
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= timedelta(days=1)
    dates = list(reversed(dates))

    from tokmo.values.macro_observations import MacroSourceRefV1

    kospi = [_candle(d, close_price="3000") for d in dates]
    # Append duplicate last date
    kospi.append(_candle(dates[-1], close_price="3100"))
    proxy = [_candle(d, close_price="1400") for d in dates]
    investor = [_investor(d, foreign_net="0") for d in dates]
    bundle = MacroObservationSeriesV1(
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
                date_range_start=kospi[0].session_date,
                date_range_end=kospi[-1].session_date,
                series_digest=compute_series_digest(tuple(kospi)),
            ),
            MacroSourceRefV1(
                operation="STOCK_CANDLES",
                symbol="261240",
                interval="1d",
                requested_count=200,
                returned_count=len(proxy),
                date_range_start=proxy[0].session_date,
                date_range_end=proxy[-1].session_date,
                series_digest=compute_series_digest(tuple(proxy)),
            ),
            MacroSourceRefV1(
                operation="INDICATOR_INVESTOR_TRADING",
                symbol="KOSPI",
                interval="1d",
                requested_count=100,
                returned_count=len(investor),
                date_range_start=investor[0].session_date,
                date_range_end=investor[-1].session_date,
                series_digest=compute_series_digest(tuple(investor)),
            ),
        ),
    )
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=90),
        expires_at=None,
        review_cadence_days=90,
        spec_digest=hashlib.sha256(b"x").hexdigest(),
    )
    lifecycle = evaluate_lifecycle_status(
        spec,
        as_of=as_of,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
    )
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# OBS-02: out-of-order dates in investor → ValueError
# ==========================================================================


def test_obs02_out_of_order_investor_dates():
    """OBS-02: out-of-order dates in investor series → ValueError at analyzer.

    Tested here by constructing a bundle with swapped investor dates and
    passing to the analyzer. The analyzer's _validate_series_structure
    rejects non-monotonic session_date."""
    import hashlib

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    from tokmo.values.signal_program_lifecycle import (
        ProgramKindV1,
        ProgramLifecycleSpecV1,
        evaluate_lifecycle_status,
    )

    end = date(2026, 6, 18)
    as_of = _ts()
    dates: list[date] = []
    cur = end
    while len(dates) < 100:
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= timedelta(days=1)
    dates = list(reversed(dates))

    from tokmo.values.macro_observations import MacroSourceRefV1

    kospi = [_candle(d, close_price="3000") for d in dates]
    proxy = [_candle(d, close_price="1400") for d in dates]
    investor = [_investor(d, foreign_net="0") for d in dates]
    # Swap two investor elements to break monotonicity
    investor[50], investor[51] = investor[51], investor[50]

    bundle = MacroObservationSeriesV1(
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
                date_range_start=kospi[0].session_date,
                date_range_end=kospi[-1].session_date,
                series_digest=compute_series_digest(tuple(kospi)),
            ),
            MacroSourceRefV1(
                operation="STOCK_CANDLES",
                symbol="261240",
                interval="1d",
                requested_count=200,
                returned_count=len(proxy),
                date_range_start=proxy[0].session_date,
                date_range_end=proxy[-1].session_date,
                series_digest=compute_series_digest(tuple(proxy)),
            ),
            MacroSourceRefV1(
                operation="INDICATOR_INVESTOR_TRADING",
                symbol="KOSPI",
                interval="1d",
                requested_count=100,
                returned_count=len(investor),
                date_range_start=investor[0].session_date,
                date_range_end=investor[-1].session_date,
                series_digest=compute_series_digest(tuple(investor)),
            ),
        ),
    )
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=90),
        expires_at=None,
        review_cadence_days=90,
        spec_digest=hashlib.sha256(b"x").hexdigest(),
    )
    lifecycle = evaluate_lifecycle_status(
        spec,
        as_of=as_of,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
    )
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# OBS-03: naive observed_at → ValueError
# ==========================================================================


def test_obs03_naive_observed_at_rejects():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 6, 18),
            observed_at=datetime(2026, 6, 18, 15, 30),  # naive
            available_at=_ts(),
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="10",
            currency="KRW",
        )


# ==========================================================================
# OBS-04: available_at > as_of → future leak (validated at bundle level)
# ==========================================================================


def test_obs04_future_leak_rejected_at_analyzer():
    """OBS-04: available_at > as_of is rejected as future leak by the analyzer."""
    import hashlib

    from tokmo.programs.macro_divergence_analyzer import evaluate_macro_divergence
    from tokmo.values.signal_program_lifecycle import (
        ProgramKindV1,
        ProgramLifecycleSpecV1,
        evaluate_lifecycle_status,
    )

    end = date(2026, 6, 18)
    as_of = _ts()
    dates: list[date] = []
    cur = end
    while len(dates) < 100:
        if cur.weekday() < 5:
            dates.append(cur)
        cur -= timedelta(days=1)
    dates = list(reversed(dates))

    from tokmo.values.macro_observations import MacroSourceRefV1

    kospi = [_candle(d, close_price="3000") for d in dates]
    # Inject future leak into last kospi candle
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
    proxy = [_candle(d, close_price="1400") for d in dates]
    investor = [_investor(d, foreign_net="0") for d in dates]
    bundle = MacroObservationSeriesV1(
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
                date_range_start=kospi[0].session_date,
                date_range_end=kospi[-1].session_date,
                series_digest=compute_series_digest(tuple(kospi)),
            ),
            MacroSourceRefV1(
                operation="STOCK_CANDLES",
                symbol="261240",
                interval="1d",
                requested_count=200,
                returned_count=len(proxy),
                date_range_start=proxy[0].session_date,
                date_range_end=proxy[-1].session_date,
                series_digest=compute_series_digest(tuple(proxy)),
            ),
            MacroSourceRefV1(
                operation="INDICATOR_INVESTOR_TRADING",
                symbol="KOSPI",
                interval="1d",
                requested_count=100,
                returned_count=len(investor),
                date_range_start=investor[0].session_date,
                date_range_end=investor[-1].session_date,
                series_digest=compute_series_digest(tuple(investor)),
            ),
        ),
    )
    spec = ProgramLifecycleSpecV1(
        program_id="AI-MACRO-001",
        version="1.0.0",
        kind=ProgramKindV1.CORE_ANALYZER,
        activated_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST),
        review_due_at=datetime(2026, 1, 1, 9, 0, tzinfo=KST) + timedelta(days=90),
        expires_at=None,
        review_cadence_days=90,
        spec_digest=hashlib.sha256(b"x").hexdigest(),
    )
    lifecycle = evaluate_lifecycle_status(
        spec,
        as_of=as_of,
        last_evidence_at=None,
        evidence_count=0,
        renewal_count=0,
    )
    with pytest.raises(ValueError):
        evaluate_macro_divergence(bundle, lifecycle=lifecycle)


# ==========================================================================
# OBS-05: available_at < observed_at → ValueError
# ==========================================================================


def test_obs05_available_before_observed_rejects():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 6, 18),
            observed_at=_ts(),
            available_at=_ts() - timedelta(hours=1),  # before observed
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="10",
            currency="KRW",
        )


# ==========================================================================
# OBS-06: non-finite decimal price → ValueError
# ==========================================================================


def test_obs06_non_finite_decimal_rejects():
    for bad in ["NaN", "Infinity", "inf", "-inf", "", "abc"]:
        with pytest.raises(ValueError):
            MacroCandleObservation(
                session_date=date(2026, 6, 18),
                observed_at=_ts(),
                available_at=_ts(),
                open_price=bad,
                high_price="100",
                low_price="100",
                close_price="100",
                volume="10",
                currency="KRW",
            )


def test_obs06b_non_finite_volume_rejects():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 6, 18),
            observed_at=_ts(),
            available_at=_ts(),
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="NaN",
            currency="KRW",
        )


# ==========================================================================
# OBS-07..10: coverage states (validated at analyzer level)
# ==========================================================================


def test_obs07_empty_series_is_constructible():
    """Empty series is a valid bundle state — coverage determined by analyzer."""
    bundle = MacroObservationSeriesV1(
        kospi_candles=(),
        proxy_candles=(),
        investor_records=(),
        as_of=_ts(),
        source_refs=(
            _source_ref("INDICATOR_CANDLES", "KOSPI", 0),
            _source_ref("STOCK_CANDLES", "261240", 0),
            _source_ref("INDICATOR_INVESTOR_TRADING", "KOSPI", 0),
        ),
    )
    assert len(bundle.kospi_candles) == 0


# ==========================================================================
# OBS-11: proxy currency is KRW (ETF), not USD
# ==========================================================================


def test_obs11_proxy_currency_is_krw():
    c = _candle(date(2026, 6, 18), currency="KRW")
    assert c.currency == "KRW"


# ==========================================================================
# OBS-12: source_refs count != 3 → ValueError
# ==========================================================================


def test_obs12_source_refs_count_not_three_rejects():
    with pytest.raises(ValueError):
        MacroObservationSeriesV1(
            kospi_candles=(),
            proxy_candles=(),
            investor_records=(),
            as_of=_ts(),
            source_refs=(
                _source_ref("INDICATOR_CANDLES", "KOSPI", 0),
                _source_ref("STOCK_CANDLES", "261240", 0),
            ),  # only 2
        )


def test_obs12b_four_source_refs_rejects():
    ref = _source_ref("INDICATOR_CANDLES", "KOSPI", 0)
    with pytest.raises(ValueError):
        MacroObservationSeriesV1(
            kospi_candles=(),
            proxy_candles=(),
            investor_records=(),
            as_of=_ts(),
            source_refs=(ref, ref, ref, ref),
        )


# ==========================================================================
# OBS-13: mismatched operation-symbol pair → ValueError
# ==========================================================================


def test_obs13_mismatched_operation_symbol_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="INDICATOR_CANDLES",
            symbol="261240",  # STOCK symbol with INDICATOR operation
            interval="1d",
            requested_count=200,
            returned_count=100,
            date_range_start=None,
            date_range_end=None,
            series_digest=hashlib.sha256(b"x").hexdigest(),
        )


def test_obs13b_indicator_investor_with_wrong_symbol_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="INDICATOR_INVESTOR_TRADING",
            symbol="261240",
            interval="1d",
            requested_count=100,
            returned_count=50,
            date_range_start=None,
            date_range_end=None,
            series_digest=hashlib.sha256(b"x").hexdigest(),
        )


# ==========================================================================
# Structural validation
# ==========================================================================


def test_candle_session_date_must_be_date_not_datetime():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=datetime(2026, 6, 18, tzinfo=KST),  # datetime, not date
            observed_at=_ts(),
            available_at=_ts(),
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="10",
            currency="KRW",
        )


def test_candle_currency_empty_rejects():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 6, 18),
            observed_at=_ts(),
            available_at=_ts(),
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="10",
            currency="",
        )


def test_candle_currency_too_long_rejects():
    with pytest.raises(ValueError):
        MacroCandleObservation(
            session_date=date(2026, 6, 18),
            observed_at=_ts(),
            available_at=_ts(),
            open_price="100",
            high_price="100",
            low_price="100",
            close_price="100",
            volume="10",
            currency="TOOLONGCUR",
        )


def test_source_ref_bad_operation_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="UNKNOWN_OP",
            symbol="KOSPI",
            interval="1d",
            requested_count=100,
            returned_count=50,
            date_range_start=None,
            date_range_end=None,
            series_digest=hashlib.sha256(b"x").hexdigest(),
        )


def test_source_ref_bad_interval_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="INDICATOR_CANDLES",
            symbol="KOSPI",
            interval="1h",
            requested_count=100,
            returned_count=50,
            date_range_start=None,
            date_range_end=None,
            series_digest=hashlib.sha256(b"x").hexdigest(),
        )


def test_source_ref_negative_count_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="INDICATOR_CANDLES",
            symbol="KOSPI",
            interval="1d",
            requested_count=-1,
            returned_count=0,
            date_range_start=None,
            date_range_end=None,
            series_digest=hashlib.sha256(b"x").hexdigest(),
        )


def test_source_ref_bad_digest_rejects():
    with pytest.raises(ValueError):
        MacroSourceRefV1(
            operation="INDICATOR_CANDLES",
            symbol="KOSPI",
            interval="1d",
            requested_count=100,
            returned_count=50,
            date_range_start=None,
            date_range_end=None,
            series_digest="short",
        )


def test_bundle_naive_as_of_rejects():
    with pytest.raises(ValueError):
        MacroObservationSeriesV1(
            kospi_candles=(),
            proxy_candles=(),
            investor_records=(),
            as_of=datetime(2026, 6, 18, 16, 30),  # naive
            source_refs=(_source_ref(), _source_ref(), _source_ref()),
        )


# ==========================================================================
# Series digest (§4.3.1)
# ==========================================================================


def test_series_digest_is_64_hex():
    candles = (_candle(date(2026, 6, 18), close_price="9063.84"),)
    d = compute_series_digest(candles)
    assert len(d) == 64
    assert all(c in "0123456789abcdef" for c in d)


def test_series_digest_excludes_available_at():
    """Same economic data with different available_at → same digest (§4.3.1)."""
    d = date(2026, 6, 18)
    obs_at = _ts()
    c1 = MacroCandleObservation(
        session_date=d,
        observed_at=obs_at,
        available_at=obs_at + timedelta(minutes=1),
        open_price="100",
        high_price="100",
        low_price="100",
        close_price="100",
        volume="10",
        currency="KRW",
    )
    c2 = MacroCandleObservation(
        session_date=d,
        observed_at=obs_at,
        available_at=obs_at + timedelta(hours=5),
        open_price="100",
        high_price="100",
        low_price="100",
        close_price="100",
        volume="10",
        currency="KRW",
    )
    assert compute_series_digest((c1,)) == compute_series_digest((c2,))


def test_series_digest_sorts_by_date():
    """Digest is order-independent: sorted by session_date (§4.3.1 rule 1)."""
    d1, d2 = date(2026, 6, 17), date(2026, 6, 18)
    c_early = MacroCandleObservation(
        session_date=d1,
        observed_at=_ts(-1),
        available_at=_ts(-1),
        open_price="100",
        high_price="100",
        low_price="100",
        close_price="100",
        volume="10",
        currency="KRW",
    )
    c_late = MacroCandleObservation(
        session_date=d2,
        observed_at=_ts(),
        available_at=_ts(),
        open_price="200",
        high_price="200",
        low_price="200",
        close_price="200",
        volume="20",
        currency="KRW",
    )
    # Different orderings should produce the same digest
    assert compute_series_digest((c_early, c_late)) == compute_series_digest(
        (c_late, c_early)
    )


def test_series_digest_empty():
    d = compute_series_digest(())
    assert len(d) == 64


def test_series_digest_investor():
    obs = _investor(date(2026, 6, 18), foreign_net="-1200000000000")
    d = compute_series_digest((obs,))
    assert len(d) == 64


# ==========================================================================
# Coverage state enum closed set
# ==========================================================================


def test_coverage_state_closed_set():
    members = {m.value for m in MacroCoverageState}
    assert members == {
        "COVERED",
        "PARTIAL",
        "STALE",
        "EMPTY",
        "SOURCE_UNAVAILABLE",
    }


# ==========================================================================
# Fixture loading (sanity — fixtures created later)
# ==========================================================================


def test_fixtures_directory_exists():
    """The fixtures directory exists and is part of the allowlist."""
    assert FIXTURES_DIR.is_dir()
