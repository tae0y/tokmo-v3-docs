"""AI-MACRO-001 macro divergence analyzer (SDS §3.9, §4.6, §5).

Pure analyzer: validates a MacroObservationSeriesV1, computes the three
divergence components, and produces a MacroDivergencePackV1 evidence pack.
No I/O, no wall-clock, no random — caller supplies as_of and every timestamp.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from tokmo.values import canonical_decimal
from tokmo.values.macro_divergence import (
    COMMON_WINDOW_MIN,
    EXHAUSTION_FX_RETURN_THRESHOLD,
    EXHAUSTION_IMPROVEMENT_RATIO,
    EXHAUSTION_MIN_INVESTOR,
    EXHAUSTION_PRIOR_WINDOW,
    FOREIGN_OUTFLOW_STRESS_THRESHOLD,
    FORMULA_VERSION,
    FX_RETURN_WINDOW,
    HIGH_PERCENTILE_THRESHOLD,
    LOOKBACK_MAX,
    LOOKBACK_MIN,
    OUTFLOW_WINDOW,
    STALENESS_CEILING_CALENDAR_DAYS,
    ConfidenceCeiling,
    MacroDivergenceComponent,
    MacroDivergenceComponentV1,
    MacroDivergencePackV1,
    MacroDivergenceState,
    MacroSymbolContactV1,
    MacroWindowSummaryV1,
    compute_pack_digest,
    map_confidence_ceiling,
    percentile_rank,
)
from tokmo.values.macro_observations import (
    MacroCandleObservation,
    MacroCoverageState,
    MacroDataCoverageV1,
    MacroInvestorObservation,
    MacroObservationSeriesV1,
)
from tokmo.values.signal_program_lifecycle import (
    LifecycleReasonCode,
    ProgramKindV1,
    ProgramLifecycleStateV1,
    ProgramLifecycleStatusV1,
)

__all__ = ["evaluate_macro_divergence"]


# ==========================================================================
# Helpers
# ==========================================================================

def _require_aware(dt: datetime, name: str) -> None:
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _utc_iso(dt: datetime) -> str:
    """UTC RFC3339 with Z, real microseconds (§4.3.1 rule 4)."""
    utc = dt.astimezone(timezone.utc)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond:06d}Z"


# ==========================================================================
# §4.6  Structural validation
# ==========================================================================

def _validate_series_structure(
    observations: tuple,
    series_name: str,
    as_of: datetime,
) -> None:
    """Rules 1–5: per-observation structural checks, monotonicity, future leak."""
    prev_date: date | None = None
    seen_dates: set[date] = set()

    for obs in observations:
        # Rule 1: observed_at / available_at aware
        _require_aware(obs.observed_at, f"{series_name}.observed_at")
        _require_aware(obs.available_at, f"{series_name}.available_at")

        # Rule 2: strictly monotonically increasing session_date
        if prev_date is not None and obs.session_date <= prev_date:
            raise ValueError(
                f"{series_name}: session_date not strictly increasing at {obs.session_date}"
            )

        # Rule 3: no duplicates (implied by strict monotonicity, but explicit)
        if obs.session_date in seen_dates:
            raise ValueError(
                f"{series_name}: duplicate session_date {obs.session_date}"
            )
        seen_dates.add(obs.session_date)
        prev_date = obs.session_date

        # Rule 4: available_at <= as_of (future leak)
        if obs.available_at > as_of:
            raise ValueError(
                f"{series_name}: future leakage at {obs.session_date} "
                f"(available_at > as_of)"
            )

        # Rule 5: finite decimals already validated at construction time.


# ==========================================================================
# §4.6  Coverage determination
# ==========================================================================

_PER_SERIES_MIN = {"kospi": 60, "proxy": 60, "investor": 40}


def _compute_coverage(
    bundle: MacroObservationSeriesV1,
) -> MacroDataCoverageV1:
    """Determine coverage state (§4.6 rules 6–12)."""
    kospi = bundle.kospi_candles
    proxy = bundle.proxy_candles
    investor = bundle.investor_records

    kospi_count = len(kospi)
    proxy_count = len(proxy)
    investor_count = len(investor)

    # Rule 6: empty series
    if kospi_count == 0 or proxy_count == 0 or investor_count == 0:
        return MacroDataCoverageV1(
            state=MacroCoverageState.EMPTY,
            kospi_count=kospi_count, proxy_count=proxy_count,
            investor_count=investor_count,
            common_window_count=0,
            common_window_start=None, common_window_end=None,
            latest_common_session=None,
            reason="one or more series empty",
        )

    # Per-series minimums (necessary, not sufficient)
    if (kospi_count < _PER_SERIES_MIN["kospi"]
            or proxy_count < _PER_SERIES_MIN["proxy"]
            or investor_count < _PER_SERIES_MIN["investor"]):
        return MacroDataCoverageV1(
            state=MacroCoverageState.PARTIAL,
            kospi_count=kospi_count, proxy_count=proxy_count,
            investor_count=investor_count,
            common_window_count=0,
            common_window_start=None, common_window_end=None,
            latest_common_session=None,
            reason="one or more series below per-series minimum",
        )

    # Common window intersection (rules 7b–7d)
    kospi_dates = {c.session_date for c in kospi}
    proxy_dates = {c.session_date for c in proxy}
    investor_dates = {r.session_date for r in investor}
    common = sorted(kospi_dates & proxy_dates & investor_dates)
    common_count = len(common)

    if common_count < COMMON_WINDOW_MIN:
        return MacroDataCoverageV1(
            state=MacroCoverageState.PARTIAL,
            kospi_count=kospi_count, proxy_count=proxy_count,
            investor_count=investor_count,
            common_window_count=common_count,
            common_window_start=common[0] if common else None,
            common_window_end=common[-1] if common else None,
            latest_common_session=common[-1] if common else None,
            reason=f"common window {common_count} < {COMMON_WINDOW_MIN}",
        )

    latest_common = common[-1]

    # Staleness ceiling (rule 9)
    as_of_date = bundle.as_of.date()
    calendar_gap = (as_of_date - latest_common).days
    if calendar_gap > STALENESS_CEILING_CALENDAR_DAYS:
        return MacroDataCoverageV1(
            state=MacroCoverageState.STALE,
            kospi_count=kospi_count, proxy_count=proxy_count,
            investor_count=investor_count,
            common_window_count=common_count,
            common_window_start=common[0],
            common_window_end=common[-1],
            latest_common_session=latest_common,
            reason=f"latest common session {calendar_gap} calendar days before as_of",
        )

    return MacroDataCoverageV1(
        state=MacroCoverageState.COVERED,
        kospi_count=kospi_count, proxy_count=proxy_count,
        investor_count=investor_count,
        common_window_count=common_count,
        common_window_start=common[0],
        common_window_end=common[-1],
        latest_common_session=latest_common,
        reason="",
    )


# ==========================================================================
# §5.2–5.4  Component computations
# ==========================================================================

def _build_date_maps(
    bundle: MacroObservationSeriesV1, common_dates: list[date]
) -> tuple[dict[date, MacroCandleObservation], dict[date, MacroCandleObservation],
            dict[date, MacroInvestorObservation]]:
    kospi_map = {c.session_date: c for c in bundle.kospi_candles}
    proxy_map = {c.session_date: c for c in bundle.proxy_candles}
    investor_map = {r.session_date: r for r in bundle.investor_records}
    return kospi_map, proxy_map, investor_map


def _compute_index_high(
    common_dates: list[date],
    kospi_map: dict[date, MacroCandleObservation],
    proxy_map: dict[date, MacroCandleObservation],
) -> MacroDivergenceComponentV1:
    # §4.6 rule 7e: latest min(len, LOOKBACK_MAX) common dates
    window_n = min(len(common_dates), LOOKBACK_MAX)
    window_dates = common_dates[-window_n:]

    kospi_closes = [Decimal(kospi_map[d].close_price) for d in window_dates]
    proxy_closes = [Decimal(proxy_map[d].close_price) for d in window_dates]
    kospi_latest = kospi_closes[-1]
    proxy_latest = proxy_closes[-1]

    kospi_pct = percentile_rank(kospi_closes, kospi_latest)
    proxy_pct = percentile_rank(proxy_closes, proxy_latest)

    fired = (kospi_pct >= HIGH_PERCENTILE_THRESHOLD
             and proxy_pct >= HIGH_PERCENTILE_THRESHOLD)

    reason = (
        f"KOSPI percentile {canonical_decimal(kospi_pct)} >= {canonical_decimal(HIGH_PERCENTILE_THRESHOLD)}"
        f" and proxy percentile {canonical_decimal(proxy_pct)} >= {canonical_decimal(HIGH_PERCENTILE_THRESHOLD)}"
        if fired else
        f"KOSPI percentile {canonical_decimal(kospi_pct)} or proxy percentile {canonical_decimal(proxy_pct)}"
        f" below {canonical_decimal(HIGH_PERCENTILE_THRESHOLD)}"
    )

    return MacroDivergenceComponentV1(
        component=MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
        fired=fired,
        reason=reason,
        details={
            "kospi_percentile": canonical_decimal(kospi_pct),
            "proxy_percentile": canonical_decimal(proxy_pct),
            "kospi_window_n": str(window_n),
            "proxy_window_n": str(window_n),
            "threshold": canonical_decimal(HIGH_PERCENTILE_THRESHOLD),
        },
    )


def _compute_foreign_outflow(
    common_dates: list[date],
    investor_map: dict[date, MacroInvestorObservation],
) -> tuple[MacroDivergenceComponentV1, Decimal]:
    # §4.6 rule 7f: latest OUTFLOW_WINDOW common dates
    window_dates = common_dates[-OUTFLOW_WINDOW:]
    rolling_flow = Decimal("0")
    for d in window_dates:
        rec = investor_map[d]
        net = Decimal(rec.foreign_buy) - Decimal(rec.foreign_sell)
        rolling_flow += net

    fired = rolling_flow <= FOREIGN_OUTFLOW_STRESS_THRESHOLD

    reason = (
        f"rolling 20-session flow {canonical_decimal(rolling_flow)} <= "
        f"{canonical_decimal(FOREIGN_OUTFLOW_STRESS_THRESHOLD)}"
        if fired else
        f"rolling 20-session flow {canonical_decimal(rolling_flow)} > "
        f"{canonical_decimal(FOREIGN_OUTFLOW_STRESS_THRESHOLD)}"
    )

    return MacroDivergenceComponentV1(
        component=MacroDivergenceComponent.FOREIGN_OUTFLOW_CONFIRMATION,
        fired=fired,
        reason=reason,
        details={
            "rolling_flow_20": canonical_decimal(rolling_flow),
            "threshold": canonical_decimal(FOREIGN_OUTFLOW_STRESS_THRESHOLD),
            "window_n": str(OUTFLOW_WINDOW),
        },
    ), rolling_flow


def _compute_exhaustion(
    common_dates: list[date],
    investor_map: dict[date, MacroInvestorObservation],
    proxy_map: dict[date, MacroCandleObservation],
) -> tuple[MacroDivergenceComponentV1, Decimal, Decimal]:
    # §5.4: positions [T-39, T-20] = prior 20, [T-19, T] = current 20
    prior_dates = common_dates[-(EXHAUSTION_MIN_INVESTOR):-OUTFLOW_WINDOW]
    current_dates = common_dates[-OUTFLOW_WINDOW:]

    prior_flow = Decimal("0")
    for d in prior_dates:
        rec = investor_map[d]
        prior_flow += Decimal(rec.foreign_buy) - Decimal(rec.foreign_sell)

    current_flow = Decimal("0")
    for d in current_dates:
        rec = investor_map[d]
        current_flow += Decimal(rec.foreign_buy) - Decimal(rec.foreign_sell)

    # FX return over latest 8 common dates (§5.4 condition 3)
    fx_dates = common_dates[-FX_RETURN_WINDOW:]
    close_t7 = Decimal(proxy_map[fx_dates[0]].close_price)
    close_t = Decimal(proxy_map[fx_dates[-1]].close_price)
    if close_t7 == 0:
        fx_return = None
    else:
        fx_return = (close_t - close_t7) / close_t7

    # Condition 1: prior stressed
    prior_stressed = prior_flow <= FOREIGN_OUTFLOW_STRESS_THRESHOLD

    # Condition 2: current no longer stressed AND improvement >= 50%
    current_no_longer_stressed = current_flow > FOREIGN_OUTFLOW_STRESS_THRESHOLD
    improvement_threshold = prior_flow * EXHAUSTION_IMPROVEMENT_RATIO
    improvement_passed = current_flow >= improvement_threshold

    # Condition 3: FX return <= -0.03
    fx_return_passed = (fx_return is not None
                        and fx_return <= EXHAUSTION_FX_RETURN_THRESHOLD)

    fired = (prior_stressed
             and current_no_longer_stressed
             and improvement_passed
             and fx_return_passed)

    if fired:
        fx_str = canonical_decimal(fx_return) if fx_return is not None else "N/A"
        reason = (
            f"prior stressed {canonical_decimal(prior_flow)}, "
            f"current improved to {canonical_decimal(current_flow)}, "
            f"FX return {fx_str}"
        )
    else:
        parts = []
        if not prior_stressed:
            parts.append("prior not stressed")
        if not current_no_longer_stressed:
            parts.append("current still stressed")
        if not improvement_passed:
            parts.append("improvement < 50%")
        if fx_return is None:
            parts.append("FX return not computable: zero close")
        elif not fx_return_passed:
            parts.append("FX return not <= threshold")
        reason = "; ".join(parts) if parts else "not fired"

    details = {
        "prior_flow_20": canonical_decimal(prior_flow),
        "current_flow_20": canonical_decimal(current_flow),
        "fx_return_8": canonical_decimal(fx_return) if fx_return is not None else "N/A",
        "prior_stressed": "true" if prior_stressed else "false",
        "improvement_ratio_threshold": canonical_decimal(EXHAUSTION_IMPROVEMENT_RATIO),
        "improvement_passed": "true" if improvement_passed else "false",
        "current_no_longer_stressed": "true" if current_no_longer_stressed else "false",
        "fx_return_threshold": canonical_decimal(EXHAUSTION_FX_RETURN_THRESHOLD),
    }

    return MacroDivergenceComponentV1(
        component=MacroDivergenceComponent.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL,
        fired=fired,
        reason=reason,
        details=details,
    ), prior_flow, current_flow


# ==========================================================================
# §5.7  Primary state precedence
# ==========================================================================

def _determine_primary_state(components: tuple[MacroDivergenceComponentV1, ...]) -> str:
    idx_high, outflow, exhaustion = components
    if exhaustion.fired:
        return MacroDivergenceState.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL.value
    if outflow.fired:
        return MacroDivergenceState.FOREIGN_OUTFLOW_CONFIRMATION.value
    if idx_high.fired:
        return MacroDivergenceState.INDEX_HIGH_FX_PROXY_HIGH.value
    return MacroDivergenceState.NO_MATERIAL_DIVERGENCE.value


# ==========================================================================
# §3.9  Entry point
# ==========================================================================

_BLOCKING_STATUS_REASON: dict[ProgramLifecycleStatusV1, LifecycleReasonCode] = {
    ProgramLifecycleStatusV1.SHADOW: LifecycleReasonCode.NOT_YET_ACTIVE,
    ProgramLifecycleStatusV1.DORMANT: LifecycleReasonCode.DORMANT,
    ProgramLifecycleStatusV1.RETIRED: LifecycleReasonCode.RETIRED,
    ProgramLifecycleStatusV1.EXPIRED: LifecycleReasonCode.EXPIRED,
}


def _build_blocked_pack(
    bundle: MacroObservationSeriesV1,
    lifecycle: ProgramLifecycleStateV1,
) -> MacroDivergencePackV1:
    """Lifecycle-blocked pack (§3.9 step 3): coverage_state=None, all blocked."""
    reason_code = _BLOCKING_STATUS_REASON[lifecycle.status]

    spec = lifecycle.spec
    common_dates = _safe_common_dates(bundle)

    # Compute window summary if possible
    kospi_map = {c.session_date: c for c in bundle.kospi_candles}
    proxy_map = {c.session_date: c for c in bundle.proxy_candles}

    latest = common_dates[-1] if common_dates else None
    kospi_close = (
        canonical_decimal(Decimal(kospi_map[latest].close_price))
        if latest and latest in kospi_map else ""
    )
    proxy_close = (
        canonical_decimal(Decimal(proxy_map[latest].close_price))
        if latest and latest in proxy_map else ""
    )

    window_summary = MacroWindowSummaryV1(
        as_of=_utc_iso(lifecycle.as_of),
        latest_common_session=latest.isoformat() if latest else "",
        common_window_count=len(common_dates),
        kospi_latest_close=kospi_close,
        proxy_latest_close=proxy_close,
        kospi_percentile=None, proxy_percentile=None,
        foreign_rolling_flow_20=None, foreign_prior_flow_20=None,
        fx_return_8=None,
    )

    symbol_contacts = _build_symbol_contacts(bundle, common_dates)

    blocked_components = tuple(
        MacroDivergenceComponentV1(
            component=comp,
            fired=False,
            reason="lifecycle blocked",
            details={},
        )
        for comp in (
            MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
            MacroDivergenceComponent.FOREIGN_OUTFLOW_CONFIRMATION,
            MacroDivergenceComponent.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL,
        )
    )

    evidence = (f"lifecycle blocked: {reason_code.value}",)
    counter_evidence = (
        f"program status {lifecycle.status.value} prevents evaluation",
    )

    pack = MacroDivergencePackV1(
        program_id=spec.program_id,
        program_version=spec.version,
        program_kind=spec.kind.value,
        formula_version=FORMULA_VERSION,
        spec_digest=spec.spec_digest,
        as_of=_utc_iso(lifecycle.as_of),
        lifecycle_status=lifecycle.status.value,
        lifecycle_reason=reason_code.value,
        lifecycle_review_due_at=_utc_iso(spec.review_due_at),
        coverage_state=None,
        coverage_reason="",
        components=blocked_components,
        primary_state=MacroDivergenceState.INSUFFICIENT_DATA.value,
        window_summary=window_summary,
        symbol_contacts=symbol_contacts,
        evidence=evidence,
        counter_evidence=counter_evidence,
        source_refs=bundle.source_refs,
    )
    # Compute and set digest
    digest = _compute_pack_digest_excluding_self(pack)
    object.__setattr__(pack, "pack_digest", digest)
    return pack


def _safe_common_dates(bundle: MacroObservationSeriesV1) -> list[date]:
    if not bundle.kospi_candles or not bundle.proxy_candles or not bundle.investor_records:
        return []
    kospi_dates = {c.session_date for c in bundle.kospi_candles}
    proxy_dates = {c.session_date for c in bundle.proxy_candles}
    investor_dates = {r.session_date for r in bundle.investor_records}
    return sorted(kospi_dates & proxy_dates & investor_dates)


def _build_symbol_contacts(
    bundle: MacroObservationSeriesV1, common_dates: list[date]
) -> tuple[MacroSymbolContactV1, ...]:
    kospi_map = {c.session_date: c for c in bundle.kospi_candles}
    proxy_map = {c.session_date: c for c in bundle.proxy_candles}
    investor_map = {r.session_date: r for r in bundle.investor_records}

    latest = common_dates[-1] if common_dates else None

    contacts = []
    if latest and latest in kospi_map:
        contacts.append(MacroSymbolContactV1(
            symbol="KOSPI", role="INDEX",
            latest_session=latest.isoformat(),
            latest_close=canonical_decimal(Decimal(kospi_map[latest].close_price)),
        ))
    if latest and latest in proxy_map:
        contacts.append(MacroSymbolContactV1(
            symbol="261240", role="FX_PROXY",
            latest_session=latest.isoformat(),
            latest_close=canonical_decimal(Decimal(proxy_map[latest].close_price)),
        ))
    if latest and latest in investor_map:
        contacts.append(MacroSymbolContactV1(
            symbol="KOSPI", role="INVESTOR_FLOW",
            latest_session=latest.isoformat(),
            latest_close=None,
        ))
    return tuple(contacts)


def _compute_pack_digest_excluding_self(pack: MacroDivergencePackV1) -> str:
    """Compute digest over all pack fields except pack_digest itself."""
    from dataclasses import fields, is_dataclass
    from enum import Enum
    from datetime import datetime, date as date_cls
    from decimal import Decimal

    def serialize(obj):
        if obj is None:
            return None
        if isinstance(obj, Enum):
            return obj.value
        if isinstance(obj, Decimal):
            return canonical_decimal(obj)
        if isinstance(obj, datetime):
            utc = obj.astimezone(timezone.utc)
            return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond:06d}Z"
        if isinstance(obj, date_cls):
            return obj.isoformat()
        if is_dataclass(obj) and not isinstance(obj, type):
            return {f.name: serialize(getattr(obj, f.name)) for f in fields(obj)}
        if isinstance(obj, dict):
            result = {}
            for k, v in obj.items():
                if isinstance(v, bool):
                    result[k] = "true" if v else "false"
                elif isinstance(v, int) and not isinstance(v, bool):
                    result[k] = str(v)
                else:
                    result[k] = serialize(v)
            return result
        if isinstance(obj, (list, tuple)):
            return [serialize(item) for item in obj]
        return obj

    pack_dict = {}
    for f in fields(pack):
        if f.name == "pack_digest":
            continue
        pack_dict[f.name] = serialize(getattr(pack, f.name))

    return compute_pack_digest(pack_dict)


def evaluate_macro_divergence(
    bundle: MacroObservationSeriesV1,
    *,
    lifecycle: ProgramLifecycleStateV1,
) -> MacroDivergencePackV1:
    """Pure entry point (§3.9).

    Pre-evaluation checks in order:
    1. Validate lifecycle (construction-time validation already done).
    2. Require lifecycle.as_of == bundle.as_of.
    3. Check lifecycle blocking status.
    4. Normal validation + component computation.
    """
    spec = lifecycle.spec

    # Step 2: as_of equality
    if lifecycle.as_of != bundle.as_of:
        raise ValueError(
            f"lifecycle.as_of != bundle.as_of: "
            f"{lifecycle.as_of} != {bundle.as_of}"
        )

    # Step 3: lifecycle blocking
    if lifecycle.status in _BLOCKING_STATUS_REASON:
        return _build_blocked_pack(bundle, lifecycle)

    # Step 4: ACTIVE or REVIEW_DUE → proceed
    if lifecycle.status is ProgramLifecycleStatusV1.ACTIVE:
        reason_code = LifecycleReasonCode.OK
    elif lifecycle.status is ProgramLifecycleStatusV1.REVIEW_DUE:
        reason_code = LifecycleReasonCode.REVIEW_DUE
    else:
        # Should not reach here (DORMANT/RETIRED/SHADOW/EXPIRED handled above)
        raise ValueError(f"unexpected lifecycle status: {lifecycle.status}")

    # Structural validation (§4.6 rules 1–5)
    _validate_series_structure(bundle.kospi_candles, "kospi_candles", bundle.as_of)
    _validate_series_structure(bundle.proxy_candles, "proxy_candles", bundle.as_of)
    _validate_series_structure(bundle.investor_records, "investor_records", bundle.as_of)

    # Coverage determination (§4.6 rules 6–12)
    coverage = _compute_coverage(bundle)

    # Determine primary state
    if coverage.state is not MacroCoverageState.COVERED:
        primary_state = MacroDivergenceState.INSUFFICIENT_DATA.value
        components = tuple(
            MacroDivergenceComponentV1(
                component=comp,
                fired=False,
                reason=f"coverage {coverage.state.value}: {coverage.reason}",
                details={},
            )
            for comp in (
                MacroDivergenceComponent.INDEX_HIGH_FX_PROXY_HIGH,
                MacroDivergenceComponent.FOREIGN_OUTFLOW_CONFIRMATION,
                MacroDivergenceComponent.OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL,
            )
        )
    else:
        # Compute common dates for component evaluation
        kospi_dates = {c.session_date for c in bundle.kospi_candles}
        proxy_dates = {c.session_date for c in bundle.proxy_candles}
        investor_dates = {r.session_date for r in bundle.investor_records}
        common_dates = sorted(kospi_dates & proxy_dates & investor_dates)
        kospi_map, proxy_map, investor_map = _build_date_maps(bundle, common_dates)

        # §5.2: INDEX_HIGH_FX_PROXY_HIGH
        comp_index = _compute_index_high(common_dates, kospi_map, proxy_map)
        # §5.3: FOREIGN_OUTFLOW_CONFIRMATION
        comp_outflow, rolling_flow = _compute_foreign_outflow(common_dates, investor_map)
        # §5.4: OUTFLOW_EXHAUSTION_FX_PROXY_REVERSAL
        comp_exhaustion, prior_flow, current_flow = _compute_exhaustion(
            common_dates, investor_map, proxy_map
        )

        components = (comp_index, comp_outflow, comp_exhaustion)
        primary_state = _determine_primary_state(components)

    # Build window summary
    common_dates = _safe_common_dates(bundle)
    latest = common_dates[-1] if common_dates else None
    kospi_map = {c.session_date: c for c in bundle.kospi_candles}
    proxy_map = {c.session_date: c for c in bundle.proxy_candles}
    investor_map = {r.session_date: r for r in bundle.investor_records}

    kospi_close = ""
    proxy_close = ""
    kospi_pct_str = None
    proxy_pct_str = None
    rolling_flow_str = None
    prior_flow_str = None
    fx_return_str = None

    if latest:
        if latest in kospi_map:
            kospi_close = canonical_decimal(Decimal(kospi_map[latest].close_price))
        if latest in proxy_map:
            proxy_close = canonical_decimal(Decimal(proxy_map[latest].close_price))

    if coverage.state is MacroCoverageState.COVERED:
        window_dates = common_dates[-min(len(common_dates), LOOKBACK_MAX):]
        kospi_closes = [Decimal(kospi_map[d].close_price) for d in window_dates]
        proxy_closes = [Decimal(proxy_map[d].close_price) for d in window_dates]
        kospi_pct_str = canonical_decimal(percentile_rank(kospi_closes, kospi_closes[-1]))
        proxy_pct_str = canonical_decimal(percentile_rank(proxy_closes, proxy_closes[-1]))

        flow_dates = common_dates[-OUTFLOW_WINDOW:]
        rf: Decimal = Decimal("0")
        for d in flow_dates:
            rf += Decimal(investor_map[d].foreign_buy) - Decimal(investor_map[d].foreign_sell)
        rolling_flow_str = canonical_decimal(rf)

        prior_dates = common_dates[-EXHAUSTION_MIN_INVESTOR:-OUTFLOW_WINDOW]
        pf: Decimal = Decimal("0")
        for d in prior_dates:
            pf += Decimal(investor_map[d].foreign_buy) - Decimal(investor_map[d].foreign_sell)
        prior_flow_str = canonical_decimal(pf)

        fx_dates = common_dates[-FX_RETURN_WINDOW:]
        ct7 = Decimal(proxy_map[fx_dates[0]].close_price)
        ct = Decimal(proxy_map[fx_dates[-1]].close_price)
        if ct7 != 0:
            fx_return_str = canonical_decimal((ct - ct7) / ct7)

    window_summary = MacroWindowSummaryV1(
        as_of=_utc_iso(lifecycle.as_of),
        latest_common_session=latest.isoformat() if latest else "",
        common_window_count=len(common_dates),
        kospi_latest_close=kospi_close,
        proxy_latest_close=proxy_close,
        kospi_percentile=kospi_pct_str,
        proxy_percentile=proxy_pct_str,
        foreign_rolling_flow_20=rolling_flow_str,
        foreign_prior_flow_20=prior_flow_str,
        fx_return_8=fx_return_str,
    )

    symbol_contacts = _build_symbol_contacts(bundle, common_dates)

    # Evidence / counter-evidence
    evidence = _build_evidence(primary_state, components, coverage)
    counter_evidence = _build_counter_evidence(primary_state, components, coverage)

    confidence = map_confidence_ceiling(primary_state)

    pack = MacroDivergencePackV1(
        program_id=spec.program_id,
        program_version=spec.version,
        program_kind=spec.kind.value,
        formula_version=FORMULA_VERSION,
        spec_digest=spec.spec_digest,
        as_of=_utc_iso(lifecycle.as_of),
        lifecycle_status=lifecycle.status.value,
        lifecycle_reason=reason_code.value,
        lifecycle_review_due_at=_utc_iso(spec.review_due_at),
        coverage_state=coverage.state,
        coverage_reason=coverage.reason,
        components=components,
        primary_state=primary_state,
        window_summary=window_summary,
        symbol_contacts=symbol_contacts,
        evidence=evidence,
        counter_evidence=counter_evidence,
        confidence_ceiling=confidence.value,
        source_refs=bundle.source_refs,
    )

    digest = _compute_pack_digest_excluding_self(pack)
    object.__setattr__(pack, "pack_digest", digest)
    return pack


def _build_evidence(
    primary_state: str,
    components: tuple[MacroDivergenceComponentV1, ...],
    coverage: MacroDataCoverageV1,
) -> tuple[str, ...]:
    lines = []
    for comp in components:
        if comp.fired:
            lines.append(f"{comp.component.value}: {comp.reason}")
    if not lines:
        if coverage.state is not MacroCoverageState.COVERED:
            lines.append(f"coverage {coverage.state.value}: insufficient for evaluation")
        else:
            lines.append("no component fired: no material divergence detected")
    return tuple(lines[:20])


def _build_counter_evidence(
    primary_state: str,
    components: tuple[MacroDivergenceComponentV1, ...],
    coverage: MacroDataCoverageV1,
) -> tuple[str, ...]:
    lines = []
    for comp in components:
        if not comp.fired:
            short = comp.reason[:200] if len(comp.reason) > 200 else comp.reason
            lines.append(f"{comp.component.value} not fired: {short}")
    if not lines:
        lines.append("all evaluated components fired")
    return tuple(lines[:10])
