"""Pure deterministic quant multifactor calculations and canonical pack builder."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import fields, replace
from datetime import UTC, date, datetime, timedelta
from decimal import (
    ROUND_FLOOR,
    ROUND_HALF_EVEN,
    Context,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import Enum
from hashlib import sha256
from json import dumps
from re import fullmatch
from types import MappingProxyType

from tokmo.values.quant_multifactor_core import (
    AccountingRegimeV1,
    AdjustmentV1,
    FactorResultV1,
    FactorSummaryV1,
    MarketV1,
    MemberResultV1,
    MetricResultV1,
    MetricSummaryV1,
    OrientationV1,
    PeriodTypeV1,
    QuantMultifactorPackV1,
    SectorV1,
    SessionDateRuleV1,
    WeightEntryV1,
)

MAX_MEMBERS = 256
MAX_DAILY_CLOSES_PER_MEMBER = 252
MAX_MONTHLY_CLOSES_PER_MEMBER = 13
MAX_INPUT_BYTES = 8_388_608
MAX_PACK_BYTES = 2_097_152
MIN_COHORT = 8
MIN_VALID_PER_METRIC = 5
FACTOR_METRICS = (
    (
        "value",
        (
            ("earnings_yield", OrientationV1.NORMAL, "RATIO"),
            ("book_to_market", OrientationV1.NORMAL, "RATIO"),
        ),
    ),
    (
        "quality",
        (
            ("roe", OrientationV1.NORMAL, "RATIO"),
            ("gross_profitability", OrientationV1.NORMAL, "RATIO"),
        ),
    ),
    ("growth", (("revenue_yoy_growth", OrientationV1.NORMAL, "RATIO"),)),
    ("price_momentum", (("momentum_12_1", OrientationV1.NORMAL, "RATIO"),)),
    (
        "stability",
        (
            ("realized_volatility", OrientationV1.INVERSE, "PCT_ANNUALIZED"),
            ("max_drawdown", OrientationV1.NORMAL, "DRAWDOWN"),
        ),
    ),
)
ALWAYS_LIMITATIONS = (
    "CALLER_ATTESTED_POINT_IN_TIME_MEMBERSHIP_NOT_INDEPENDENTLY_VERIFIED",
    "PRICE_ADJUSTMENT_POINT_IN_TIME_NOT_INDEPENDENTLY_VERIFIED",
    "SESSION_CALENDAR_NOT_INDEPENDENTLY_VERIFIED",
    "SURVIVORSHIP_BIAS_NOT_INDEPENDENTLY_CONTROLLED",
)


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    return value


SPEC = _freeze(
    {
        "domain": "quant-multifactor-spec-v1",
        "formula_version": "quant-multifactor-formula-v1",
        "metrics": {
            "earnings_yield": "net_income_ttm/market_cap",
            "book_to_market": "total_equity/market_cap",
            "roe": "net_income_ttm/positive_total_equity",
            "gross_profitability": "gross_profit_ttm/positive_total_assets;financials_inapplicable",
            "revenue_yoy_growth": "(current-prior)/positive_prior",
            "momentum_12_1": "m[-2]/m[-13]-1",
            "realized_volatility": "population_std(log_returns)*sqrt(252)*100",
            "max_drawdown": "min(close/running_peak-1)",
        },
        "orientations": {
            metric: orientation.value
            for _, metrics in FACTOR_METRICS
            for metric, orientation, _ in metrics
        },
        "cross_section": {
            "winsor": "type7 p05/p95 per metric clamp",
            "rank": "ascending average ties",
            "percentile": "(rank-1)/(valid_count-1)",
            "factor": "mean available sufficient metric percentiles",
        },
        "weights": {name: "0.2" for name, _ in FACTOR_METRICS},
        "composite": "min3 factors,min2 fundamental;present-weight renormalized contributions",
        "bounds": {
            "members": 256,
            "daily": 252,
            "monthly": 13,
            "input_bytes": 8388608,
            "pack_bytes": 2097152,
            "decimal_significant": 30,
            "decimal_fractional": 12,
            "code_bytes": 240,
            "current_days": 548,
            "prior_days": 914,
            "monthly_days": 45,
            "daily_days": 7,
            "rv_closes": 21,
            "mdd_closes": 2,
            "min_cohort": 8,
            "min_valid": 5,
        },
        "reason_priority": "stale-current>metric;growth:no-prior>nonconsecutive>stale-prior>nonpositive>missing;momentum:stale>short>gap;stability:stale>short>absent",
        "applicability": {
            "gross_profitability": "NON_FINANCIALS;FINANCIALS_INAPPLICABLE",
            "other_metrics": "ALL_SECTORS",
        },
        "units": {
            "ratios": "RATIO",
            "realized_volatility": "PCT_ANNUALIZED",
            "max_drawdown": "DRAWDOWN",
        },
        "thresholds": {
            "composite_factors": 3,
            "composite_fundamental_factors": 2,
            "degraded_members": "1..7",
            "normal_members": "8..256",
        },
        "staleness": {
            "current_fundamentals_days": 548,
            "prior_fundamentals_days": 914,
            "monthly_days": 45,
            "daily_days": 7,
            "inclusive": True,
        },
        "codes": {
            "always": ALWAYS_LIMITATIONS,
            "limitation_max_count": 16,
            "code_max_bytes": 240,
            "coverage": "FULL|PARTIAL|COVERAGE_INSUFFICIENT|COHORT_INSUFFICIENT",
        },
        "quantization": {
            "ratio_percentile_rank_score_contribution": 8,
            "percentage": 6,
            "price": 8,
            "rounding": "ROUND_HALF_EVEN",
            "intermediate_precision": 50,
        },
        "signs": {
            "revenue_ttm": "NONNEGATIVE",
            "net_income_ttm": "SIGNED",
            "gross_profit_ttm": "SIGNED",
            "total_equity": "SIGNED",
            "total_assets": "POSITIVE_IF_PRESENT",
            "diluted_shares_outstanding": "POSITIVE_IF_PRESENT",
            "close": "POSITIVE",
        },
        "serialization": {
            "string_ascii_caps": {
                "symbol": 32,
                "provenance": 128,
                "universe_id": 64,
                "version": 32,
                "month_id": 7,
            },
            "decimal": {"significant": 30, "fractional": 12, "fixed_bytes": 64},
            "pack_digest_excludes": "pack_digest",
        },
    }
)


def _context():
    return localcontext(
        Context(
            prec=50,
            rounding=ROUND_HALF_EVEN,
            traps=[InvalidOperation, DivisionByZero, Overflow],
        )
    )


def _quantize(value: Decimal | None, places: int) -> Decimal | None:
    if value is None:
        return None
    with _context():
        result = value.quantize(Decimal(1).scaleb(-places))
        canonical_decimal(result)
        return result


def canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("decimal_finite")
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    if text in ("-0", ""):
        text = "0"
    digits = len(value.as_tuple().digits)
    fractional = max(0, -value.as_tuple().exponent)
    if digits > 30 or fractional > 12 or len(text.encode("ascii")) > 64:
        raise ValueError("decimal_bounds")
    return text


def _plain(value):
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, Decimal):
        return canonical_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise ValueError("timestamp_timezone")
        return value.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if hasattr(value, "__dataclass_fields__"):
        return {
            field.name: _plain(getattr(value, field.name)) for field in fields(value)
        }
    return value


def canonical_bytes(value) -> bytes:
    return dumps(
        _plain(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def validate_limitations(values):
    result = tuple(sorted(set(values)))
    if len(result) > 16 or any(
        not isinstance(item, str) or len(item.encode("utf-8")) > 240 for item in result
    ):
        raise ValueError("limitations")
    return result


def bounded_pack_bytes(value) -> bytes:
    encoded = canonical_bytes(value)
    if len(encoded) > MAX_PACK_BYTES:
        raise ValueError("pack_bytes")
    return encoded


def _digest(value) -> str:
    return sha256(canonical_bytes(value)).hexdigest()


SPEC_DIGEST = _digest(SPEC)


def market_utc_offset(market: MarketV1, instant: datetime) -> timedelta:
    if instant.tzinfo is None or not 2007 <= instant.year <= 9999:
        raise ValueError("as_of")
    if market in (MarketV1.KR_KOSPI, MarketV1.KR_KOSDAQ):
        return timedelta(hours=9)
    utc = instant.astimezone(UTC)
    year = utc.year
    march = date(year, 3, 1)
    second_sunday = 8 + ((6 - march.weekday()) % 7)
    november = date(year, 11, 1)
    first_sunday = 1 + ((6 - november.weekday()) % 7)
    start = datetime(year, 3, second_sunday, 7, tzinfo=UTC)
    end = datetime(year, 11, first_sunday, 6, tzinfo=UTC)
    return timedelta(hours=-4 if start <= utc < end else -5)


def as_of_market_date(market: MarketV1, instant: datetime) -> date:
    try:
        utc = instant.astimezone(UTC)
        return (utc + market_utc_offset(market, utc)).date()
    except OverflowError as error:
        raise ValueError("as_of") from error


def type7_percentile(values: tuple[Decimal, ...], q: Decimal) -> Decimal:
    if not values or q < 0 or q > 1:
        raise ValueError("percentile_bounds")
    ordered = tuple(sorted(values))
    with _context() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        context.traps[InvalidOperation] = context.traps[DivisionByZero] = context.traps[
            Overflow
        ] = True
        position = q * Decimal(len(ordered) - 1)
        lower = int(position.to_integral_value(rounding=ROUND_FLOOR))
        upper = min(lower + 1, len(ordered) - 1)
        return ordered[lower] + (position - Decimal(lower)) * (
            ordered[upper] - ordered[lower]
        )


def calculate_growth(current_revenue, prior_revenue):
    if prior_revenue is not None and prior_revenue <= 0:
        return None, "UNAVAILABLE_NONPOSITIVE_PRIOR_REVENUE"
    if current_revenue is None or prior_revenue is None:
        return None, "UNAVAILABLE_INPUT_MISSING"
    with _context() as context:
        context.prec = 50
        return (current_revenue - prior_revenue) / prior_revenue, None


def calculate_momentum(closes):
    if len(closes) < 13:
        return None
    with _context() as context:
        context.prec = 50
        return closes[-2] / closes[-13] - 1


def calculate_max_drawdown(closes):
    if len(closes) < 2:
        return None
    with _context() as context:
        context.prec = 50
        peak = closes[0]
        worst = Decimal(0)
        for close in closes[1:]:
            peak = max(peak, close)
            worst = min(worst, close / peak - 1)
        return worst


def calculate_realized_volatility(closes):
    if len(closes) < 21:
        return None
    with _context() as context:
        context.prec = 50
        returns = tuple((closes[i] / closes[i - 1]).ln() for i in range(1, len(closes)))
        mean = sum(returns, Decimal(0)) / Decimal(len(returns))
        variance = sum(((item - mean) ** 2 for item in returns), Decimal(0)) / Decimal(
            len(returns)
        )
        return variance.sqrt() * Decimal(252).sqrt() * Decimal(100)


def rank_metric(values, inverse):
    if len(values) < 5:
        raise ValueError("metric_insufficient")
    lower = type7_percentile(values, Decimal("0.05"))
    upper = type7_percentile(values, Decimal("0.95"))
    clamped = tuple(min(upper, max(lower, value)) for value in values)
    transformed = tuple(-v if inverse else v for v in clamped)
    ordered = tuple(sorted(transformed))
    results = []
    for index, item in enumerate(transformed):
        positions = tuple(
            i + 1 for i, candidate in enumerate(ordered) if candidate == item
        )
        rank = sum((Decimal(p) for p in positions), Decimal(0)) / Decimal(
            len(positions)
        )
        results.append((clamped[index], rank, (rank - 1) / Decimal(len(values) - 1)))
    return tuple(results)


def aggregate_composite(scores):
    present = tuple(i for i, score in enumerate(scores) if score is not None)
    if len(present) < 3 or sum(i < 3 for i in present) < 2:
        return None, (None,) * 5
    with _context() as context:
        context.prec = 50
        denominator = Decimal("0.2") * Decimal(len(present))
        result = [None] * 5
        running = Decimal(0)
        composite = (
            sum((Decimal("0.2") * scores[i] for i in present), Decimal(0)) / denominator
        )
        for i in present[:-1]:
            result[i] = Decimal("0.2") * scores[i] / denominator
            running += result[i]
        result[present[-1]] = composite - running
        return composite, tuple(result)


def _ascii(value, minimum, maximum, pattern=None):
    if (
        not isinstance(value, str)
        or not minimum <= len(value) <= maximum
        or not value.isascii()
        or (pattern and not fullmatch(pattern, value))
    ):
        raise ValueError("string_bounds")


def _validate_decimal(value, policy):
    if value is None:
        return
    canonical_decimal(value)
    if policy == "nonnegative" and value < 0:
        raise ValueError("decimal_sign")
    if policy == "positive" and value <= 0:
        raise ValueError("decimal_sign")


def _validate_snapshot(snapshot, input_value, market_date):
    _ascii(snapshot.source_ref, 1, 128, r"[A-Za-z0-9._:/-]+")
    if snapshot.revision_of is not None:
        _ascii(snapshot.revision_of, 1, 128, r"[A-Za-z0-9._:/-]+")
    if (
        snapshot.fiscal_period.period_type is not PeriodTypeV1.ANNUAL
        or snapshot.fiscal_period.fiscal_quarter is not None
    ):
        raise ValueError("fiscal_period")
    if (
        not 2007 <= snapshot.fiscal_period.fiscal_year <= 9999
        or not 2007 <= snapshot.period_end.year <= 9999
        or snapshot.period_end > market_date
    ):
        raise ValueError("fiscal_date")
    if (
        snapshot.published_at.tzinfo is None
        or not 2007 <= snapshot.published_at.year <= 9999
        or snapshot.published_at > input_value.as_of
    ):
        raise ValueError("published_at")
    if snapshot.currency != input_value.currency:
        raise ValueError("currency")
    for value, policy in (
        (snapshot.revenue_ttm, "nonnegative"),
        (snapshot.net_income_ttm, "signed"),
        (snapshot.gross_profit_ttm, "signed"),
        (snapshot.total_equity, "signed"),
        (snapshot.total_assets, "positive"),
        (snapshot.diluted_shares_outstanding, "positive"),
    ):
        _validate_decimal(value, policy)


def validate_input(input_value):
    if (
        not isinstance(input_value.market, MarketV1)
        or not isinstance(input_value.accounting_regime, AccountingRegimeV1)
        or not isinstance(input_value.adjustment, AdjustmentV1)
        or not isinstance(input_value.session_date_rule, SessionDateRuleV1)
    ):
        raise ValueError("enum")
    if not 1 <= len(input_value.members) <= MAX_MEMBERS:
        raise ValueError("member_count")
    _ascii(input_value.currency, 3, 3, r"[A-Z]{3}")
    _ascii(input_value.price_source_ref, 1, 128, r"[A-Za-z0-9._:/-]+")
    _ascii(input_value.adjustment_version, 1, 32, r"[A-Za-z0-9._:/-]+")
    _ascii(input_value.universe_id, 1, 64, r"[A-Za-z0-9._:/-]+")
    _ascii(input_value.universe_policy_version, 1, 32, r"[A-Za-z0-9._:/-]+")
    if input_value.as_of.tzinfo is None or not 2007 <= input_value.as_of.year <= 9999:
        raise ValueError("as_of")
    if (
        input_value.price_adjustment_as_of.tzinfo is None
        or input_value.price_adjustment_as_of > input_value.as_of
        or not 2007 <= input_value.price_adjustment_as_of.year <= 9999
    ):
        raise ValueError("adjustment_as_of")
    expected_regime = (
        AccountingRegimeV1.US_GAAP
        if input_value.market is MarketV1.US_NYSE_NASDAQ
        else AccountingRegimeV1.K_IFRS
    )
    expected_session = (
        SessionDateRuleV1.US_NYSE_NASDAQ_SESSION
        if input_value.market is MarketV1.US_NYSE_NASDAQ
        else SessionDateRuleV1.KR_KRX_SESSION
    )
    if (
        input_value.accounting_regime is not expected_regime
        or input_value.session_date_rule is not expected_session
    ):
        raise ValueError("cohort_identity")
    market_date = as_of_market_date(input_value.market, input_value.as_of)
    if input_value.membership_as_of != market_date:
        raise ValueError("membership_as_of")
    symbols = set()
    for member in input_value.members:
        _ascii(member.symbol, 1, 32, r"[A-Z0-9._-]+")
        _ascii(member.membership_source_ref, 1, 128, r"[A-Za-z0-9._:/-]+")
        if (
            not isinstance(member.sector, SectorV1)
            or member.listed_at_as_of is not True
            or member.symbol in symbols
        ):
            raise ValueError("member_identity")
        symbols.add(member.symbol)
        _validate_snapshot(member.current_fundamentals, input_value, market_date)
        if member.prior_fundamentals is not None:
            _validate_snapshot(member.prior_fundamentals, input_value, market_date)
        if len(member.monthly_closes) > 13 or len(member.daily_closes) > 252:
            raise ValueError("close_count")
        previous_month = None
        previous_session = None
        for close in member.monthly_closes:
            _ascii(close.month_id, 7, 7, r"\d{4}-(0[1-9]|1[0-2])")
            if (
                close.month_id != close.session_date.strftime("%Y-%m")
                or close.session_date.weekday() > 4
                or not 2007 <= close.session_date.year <= 9999
                or close.currency != input_value.currency
                or (
                    previous_month is not None
                    and (
                        close.month_id <= previous_month
                        or close.session_date <= previous_session
                    )
                )
            ):
                raise ValueError("monthly_close")
            _validate_decimal(close.close, "positive")
            previous_month = close.month_id
            previous_session = close.session_date
        previous_date = None
        for close in member.daily_closes:
            if (
                close.date.weekday() > 4
                or not 2007 <= close.date.year <= 9999
                or close.currency != input_value.currency
                or (previous_date is not None and close.date <= previous_date)
            ):
                raise ValueError("daily_close")
            _validate_decimal(close.close, "positive")
            previous_date = close.date
    ordered = replace(
        input_value,
        members=tuple(sorted(input_value.members, key=lambda member: member.symbol)),
    )
    encoded = canonical_bytes(
        {
            "domain": "quant-multifactor-input-v1",
            "caps": {
                "members": 256,
                "daily": 252,
                "monthly": 13,
                "input_bytes": 8388608,
                "pack_bytes": 2097152,
                "string_ascii": {
                    "symbol": 32,
                    "provenance": 128,
                    "universe_id": 64,
                    "version": 32,
                    "month_id": 7,
                },
                "decimal": {"significant": 30, "fractional": 12, "fixed_bytes": 64},
            },
            "input": ordered,
        }
    )
    if len(encoded) > MAX_INPUT_BYTES:
        raise ValueError("input_bytes")
    return ordered, encoded


def _raw_metrics(member, market_date):
    current = member.current_fundamentals
    prior = member.prior_fundamentals
    eligible = tuple(item for item in member.daily_closes if item.date <= market_date)
    close = (
        None
        if not eligible or (market_date - eligible[-1].date).days > 7
        else eligible[-1].close
    )
    stale_current = (market_date - current.period_end).days > 548
    applicable_gp = (
        "INAPPLICABLE_SECTOR_FINANCIALS"
        if member.sector is SectorV1.FINANCIALS
        else "APPLICABLE"
    )
    result = {}
    if stale_current:
        for name in (
            "earnings_yield",
            "book_to_market",
            "roe",
            "gross_profitability",
            "revenue_yoy_growth",
        ):
            result[name] = (
                None,
                "UNAVAILABLE_STALE_FUNDAMENTALS",
                applicable_gp if name == "gross_profitability" else "APPLICABLE",
            )
    else:
        market_cap = (
            None
            if close is None or current.diluted_shares_outstanding is None
            else close * current.diluted_shares_outstanding
        )
        for name, numerator in (
            ("earnings_yield", current.net_income_ttm),
            ("book_to_market", current.total_equity),
        ):
            reason = (
                "UNAVAILABLE_MARKET_CAP"
                if market_cap is None
                else ("UNAVAILABLE_INPUT_MISSING" if numerator is None else None)
            )
            result[name] = (
                None if reason else numerator / market_cap,
                reason,
                "APPLICABLE",
            )
        reason = (
            "UNAVAILABLE_NEGATIVE_EQUITY"
            if current.total_equity is not None and current.total_equity <= 0
            else (
                "UNAVAILABLE_INPUT_MISSING"
                if current.net_income_ttm is None or current.total_equity is None
                else None
            )
        )
        result["roe"] = (
            None if reason else current.net_income_ttm / current.total_equity,
            reason,
            "APPLICABLE",
        )
        if member.sector is SectorV1.FINANCIALS:
            result["gross_profitability"] = (None, None, applicable_gp)
        else:
            reason = (
                "UNAVAILABLE_ZERO_DENOMINATOR"
                if current.total_assets is None or current.total_assets <= 0
                else (
                    "UNAVAILABLE_INPUT_MISSING"
                    if current.gross_profit_ttm is None
                    else None
                )
            )
            result["gross_profitability"] = (
                None if reason else current.gross_profit_ttm / current.total_assets,
                reason,
                "APPLICABLE",
            )
        if prior is None:
            growth = (None, "UNAVAILABLE_NO_PRIOR_SNAPSHOT")
        elif prior.fiscal_period.fiscal_year != current.fiscal_period.fiscal_year - 1:
            growth = (None, "UNAVAILABLE_NONCONSECUTIVE_PRIOR_PERIOD")
        elif (market_date - prior.period_end).days > 914:
            growth = (None, "UNAVAILABLE_STALE_FUNDAMENTALS")
        else:
            growth = calculate_growth(current.revenue_ttm, prior.revenue_ttm)
        result["revenue_yoy_growth"] = (growth[0], growth[1], "APPLICABLE")
    monthly = tuple(
        item for item in member.monthly_closes if item.session_date <= market_date
    )
    if monthly and (market_date - monthly[-1].session_date).days > 45:
        momentum = (None, "UNAVAILABLE_STALE_MONTHLY_SERIES")
    elif len(monthly) < 13:
        momentum = (None, "UNAVAILABLE_INSUFFICIENT_MONTHS")
    else:
        consecutive = all(
            (int(monthly[i].month_id[:4]) * 12 + int(monthly[i].month_id[5:]))
            - (int(monthly[i - 1].month_id[:4]) * 12 + int(monthly[i - 1].month_id[5:]))
            == 1
            for i in range(len(monthly) - 12, len(monthly))
        )
        momentum = (
            (calculate_momentum(tuple(item.close for item in monthly)), None)
            if consecutive
            else (None, "UNAVAILABLE_NONCONSECUTIVE_MONTH_IDS")
        )
    result["momentum_12_1"] = (momentum[0], momentum[1], "APPLICABLE")
    if eligible and (market_date - eligible[-1].date).days > 7:
        rv = mdd = (None, "UNAVAILABLE_STALE_PRICE_SERIES")
    elif not eligible:
        rv = mdd = (None, "UNAVAILABLE_CLOSE_AT_AS_OF")
    else:
        closes = tuple(item.close for item in eligible[-252:])
        rv = (
            calculate_realized_volatility(closes),
            None if len(closes) >= 21 else "UNAVAILABLE_INSUFFICIENT_DAYS",
        )
        mdd = (
            calculate_max_drawdown(closes),
            None if len(closes) >= 2 else "UNAVAILABLE_INSUFFICIENT_DAYS",
        )
    result["realized_volatility"] = (rv[0], rv[1], "APPLICABLE")
    result["max_drawdown"] = (mdd[0], mdd[1], "APPLICABLE")
    return close, result


def evaluate_quant_multifactor_core_v1(input_value):
    with _context():
        return _evaluate_quant_multifactor_core_v1(input_value)


def _evaluate_quant_multifactor_core_v1(input_value):
    ordered, input_encoded = validate_input(input_value)
    market_date = as_of_market_date(ordered.market, ordered.as_of)
    raw = []
    for member in ordered.members:
        raw.append(_raw_metrics(member, market_date))
    degraded = len(ordered.members) < MIN_COHORT
    ranked = {}
    summaries = []
    limitations = list(ALWAYS_LIMITATIONS)
    fye = {
        (
            m.current_fundamentals.period_end.month,
            28
            if m.current_fundamentals.period_end.month == 2
            and m.current_fundamentals.period_end.day in (28, 29)
            else m.current_fundamentals.period_end.day,
        )
        for m in ordered.members
    }
    if len(fye) > 1:
        limitations.append("FUNDAMENTALS_FISCAL_YEAR_END_DIFFERS")
    for factor, metrics in FACTOR_METRICS:
        metric_summaries = []
        for metric, orientation, _ in metrics:
            indices = [
                i
                for i, (_, values) in enumerate(raw)
                if values[metric][0] is not None and values[metric][2] == "APPLICABLE"
            ]
            values = tuple(raw[i][1][metric][0] for i in indices)
            sufficient = not degraded and len(values) >= 5
            if sufficient:
                computed = rank_metric(values, orientation is OrientationV1.INVERSE)
                ranked[metric] = {
                    index: computed[position] for position, index in enumerate(indices)
                }
                p05 = type7_percentile(values, Decimal("0.05"))
                p95 = type7_percentile(values, Decimal("0.95"))
            else:
                ranked[metric] = {}
                p05 = p95 = None
                if not degraded:
                    limitations.append(f"FACTOR_METRIC_INSUFFICIENT:{factor}:{metric}")
            places = 6 if metric == "realized_volatility" else 8
            metric_summaries.append(
                MetricSummaryV1(
                    metric,
                    orientation,
                    len(values),
                    5,
                    _quantize(p05, places),
                    _quantize(p95, places),
                    sufficient,
                    "INAPPLICABLE_SECTOR_FINANCIALS"
                    if metric == "gross_profitability"
                    and any(m.sector is SectorV1.FINANCIALS for m in ordered.members)
                    else "APPLICABLE",
                )
            )
        summaries.append(
            FactorSummaryV1(
                factor,
                tuple(metric_summaries),
                sum(item.sufficient for item in metric_summaries),
            )
        )
    members = []
    for index, member in enumerate(ordered.members):
        factors = []
        scores = []
        for factor, metrics in FACTOR_METRICS:
            metric_results = []
            percentiles = []
            for metric, orientation, unit in metrics:
                value, reason, applicability = raw[index][1][metric]
                info = ranked[metric].get(index)
                summary = next(
                    item
                    for group in summaries
                    for item in group.metric_summaries
                    if item.metric_name == metric
                )
                percentile = None if info is None else info[2]
                if percentile is not None:
                    percentiles.append(percentile)
                places = 6 if unit == "PCT_ANNUALIZED" else 8
                metric_results.append(
                    MetricResultV1(
                        metric,
                        _quantize(value, places),
                        unit,
                        value is not None,
                        reason,
                        applicability,
                        summary.winsor_p05,
                        summary.winsor_p95,
                        _quantize(None if info is None else info[0], places),
                        orientation,
                        _quantize(None if info is None else info[1], 8),
                        _quantize(percentile, 8),
                        summary.valid_count,
                        summary.sufficient,
                    )
                )
            score = (
                None
                if degraded or not percentiles
                else _quantize(
                    sum(percentiles, Decimal(0)) / Decimal(len(percentiles)), 8
                )
            )
            scores.append(score)
            factors.append(
                FactorResultV1(
                    factor,
                    tuple(metric_results),
                    f"FACTOR_METRIC_COVERAGE:{0 if degraded else len(percentiles)}:{len(metrics)}",
                    score,
                    None,
                )
            )
        composite, contributions = aggregate_composite(tuple(scores))
        composite = _quantize(composite, 8)
        contributions = tuple(_quantize(item, 8) for item in contributions)
        count = sum(score is not None for score in scores)
        fundamental = sum(score is not None for score in scores[:3])
        if composite is not None:
            present = [i for i, item in enumerate(contributions) if item is not None]
            residual = composite - sum((contributions[i] for i in present), Decimal(0))
            if residual:
                contributions = tuple(
                    (item + residual if i == present[-1] else item)
                    if item is not None
                    else None
                    for i, item in enumerate(contributions)
                )
        coverage = (
            "COVERAGE_INSUFFICIENT"
            if composite is None
            else ("FULL" if count == 5 else "PARTIAL")
        )
        if not degraded and composite is None:
            limitations.append(
                "COVERAGE_INSUFFICIENT_BELOW_MIN_FACTORS"
                if count < 3
                else "COVERAGE_INSUFFICIENT_BELOW_MIN_FUNDAMENTAL_FACTORS"
            )
        factors = tuple(
            replace(factor, contribution=contributions[i])
            for i, factor in enumerate(factors)
        )
        members.append(
            MemberResultV1(
                member.symbol,
                member.sector,
                _quantize(raw[index][0], 8),
                factors,
                0 if degraded else count,
                0 if degraded else fundamental,
                None if degraded else composite,
                coverage,
            )
        )
    if degraded:
        limitations.extend(
            ("COHORT_INSUFFICIENT", "COVERAGE_INSUFFICIENT_BELOW_MIN_COHORT")
        )
    limitations = validate_limitations(limitations)
    weights = tuple(WeightEntryV1(name, Decimal("0.2")) for name, _ in FACTOR_METRICS)
    input_digest = sha256(input_encoded).hexdigest()
    pack = QuantMultifactorPackV1(
        "quant-multifactor-core-v1",
        "1.0.0",
        "CORE_ANALYZER",
        "quant-multifactor-formula-v1",
        SPEC_DIGEST,
        input_digest,
        "",
        ordered.market,
        ordered.currency,
        ordered.accounting_regime,
        ordered.as_of,
        ordered.price_source_ref,
        ordered.adjustment,
        ordered.adjustment_version,
        ordered.price_adjustment_as_of,
        ordered.session_date_rule,
        ordered.universe_id,
        ordered.universe_policy_version,
        ordered.membership_as_of,
        len(ordered.members),
        8,
        "COHORT_INSUFFICIENT"
        if degraded
        else ("FULL" if all(m.coverage == "FULL" for m in members) else "PARTIAL"),
        tuple(summaries),
        tuple(members),
        weights,
        "EQUAL_WEIGHT_AVAILABLE_ONLY_RENORMALIZED_MIN3_FUND2",
        3,
        2,
        limitations,
    )
    digest_pack = _plain(pack)
    digest_pack.pop("pack_digest")
    digest = _digest({"domain": "quant-multifactor-pack-v1", "pack": digest_pack})
    pack = replace(pack, pack_digest=digest)
    bounded_pack_bytes(pack)
    return pack
