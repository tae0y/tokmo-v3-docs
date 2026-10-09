"""Deterministic price features for the Quant discovery engine."""

import math
from decimal import ROUND_HALF_UP, Decimal

from tokmo.values.quant_discovery import (
    FactorOrientation as Orientation,
)
from tokmo.values.quant_discovery import (
    FactorStatus as Status,
)
from tokmo.values.quant_discovery import (
    FactorValueV1,
    FeatureOutcomeV1,
    FeatureSetV1,
    PriceSeriesV1,
    QuantUnavailableCode,
)

Q = Decimal("0.000001")
ANNUALIZER = 252
FACTOR_ORDER = (
    "ret_5",
    "ret_20",
    "ret_60",
    "ret_120",
    "close_to_sma_20",
    "close_to_sma_60",
    "close_to_sma_120",
    "rsi_14",
    "macd_histogram",
    "realized_vol_63",
    "downside_vol_63",
    "max_drawdown",
    "price_position_200",
    "risk_adjusted_momentum",
    "volume_change_20",
    "trading_amount_change_20",
)


def _q(value: Decimal | float) -> Decimal:
    return (value if isinstance(value, Decimal) else Decimal(str(value))).quantize(
        Q, rounding=ROUND_HALF_UP
    )


def _factor(name, value, orientation, observed, minimum, status=None):
    if status is None:
        status = Status.AVAILABLE if value is not None else Status.INSUFFICIENT
    return FactorValueV1(
        name,
        _q(value) if value is not None else None,
        orientation,
        status,
        observed,
        minimum,
    )


def _ema(values: list[float], period: int) -> list[float | None]:
    result: list[float | None] = [None] * len(values)
    if len(values) < period:
        return result
    current = sum(values[:period]) / period
    result[period - 1] = current
    alpha = 2 / (period + 1)
    for index in range(period, len(values)):
        current = alpha * values[index] + (1 - alpha) * current
        result[index] = current
    return result


def compute_price_features(
    series: PriceSeriesV1, *, completed_session_date
) -> FeatureOutcomeV1:
    if not series.bars or series.bars[-1].session_date != completed_session_date:
        raise ValueError("series must end on the completed session")
    if any(bar.close is None for bar in series.bars):
        raise ValueError("all closes are required")
    closes = [bar.close for bar in series.bars]
    n = len(closes)
    factors = []
    for period in (5, 20, 60, 120):
        factors.append(
            _factor(
                f"ret_{period}",
                closes[-1] / closes[-1 - period] - 1 if n >= period + 1 else None,
                Orientation.HIGHER_IS_BETTER,
                n,
                period + 1,
            )
        )
    for period in (20, 60, 120):
        if n < period:
            factors.append(
                _factor(
                    f"close_to_sma_{period}",
                    None,
                    Orientation.HIGHER_IS_BETTER,
                    n,
                    period,
                )
            )
        else:
            mean = sum(closes[-period:]) / Decimal(period)
            factors.append(
                _factor(
                    f"close_to_sma_{period}",
                    closes[-1] / mean - 1 if mean > 0 else None,
                    Orientation.HIGHER_IS_BETTER,
                    n,
                    period,
                    Status.ZERO_DENOMINATOR if mean <= 0 else None,
                )
            )
    if n < 15:
        factors.append(_factor("rsi_14", None, Orientation.HIGHER_IS_BETTER, n, 15))
    else:
        deltas = [closes[i] - closes[i - 1] for i in range(n - 14, n)]
        gain = sum(max(x, Decimal(0)) for x in deltas) / Decimal(14)
        loss = sum(max(-x, Decimal(0)) for x in deltas) / Decimal(14)
        if gain == loss == 0:
            factors.append(
                _factor(
                    "rsi_14",
                    None,
                    Orientation.HIGHER_IS_BETTER,
                    n,
                    15,
                    Status.ZERO_DENOMINATOR,
                )
            )
        else:
            value = (
                Decimal(100)
                if loss == 0
                else Decimal(100) - Decimal(100) / (1 + gain / loss)
            )
            factors.append(
                _factor("rsi_14", value, Orientation.HIGHER_IS_BETTER, n, 15)
            )
    floats = [float(x) for x in closes]
    if n < 34:
        factors.append(
            _factor("macd_histogram", None, Orientation.HIGHER_IS_BETTER, n, 34)
        )
    else:
        fast, slow = _ema(floats, 12), _ema(floats, 26)
        macd = [fast[i] - slow[i] for i in range(25, n)]
        signal = _ema(macd, 9)[-1]
        factors.append(
            _factor(
                "macd_histogram", macd[-1] - signal, Orientation.HIGHER_IS_BETTER, n, 34
            )
        )
    returns = [math.log(floats[i] / floats[i - 1]) for i in range(1, n)]
    realized = None
    if n >= 64:
        window = returns[-63:]
        mean = sum(window) / len(window)
        realized = math.sqrt(
            sum((x - mean) ** 2 for x in window) / len(window)
        ) * math.sqrt(ANNUALIZER)
        downside = math.sqrt(
            sum(min(x, 0) ** 2 for x in window) / len(window)
        ) * math.sqrt(ANNUALIZER)
    else:
        downside = None
    factors.append(
        _factor("realized_vol_63", realized, Orientation.LOWER_IS_BETTER, n, 64)
    )
    factors.append(
        _factor("downside_vol_63", downside, Orientation.LOWER_IS_BETTER, n, 64)
    )
    peak = closes[0]
    drawdown = Decimal(0)
    for close in closes[1:]:
        peak = max(peak, close)
        drawdown = min(drawdown, close / peak - 1)
    factors.append(
        _factor(
            "max_drawdown",
            drawdown if n >= 2 else None,
            Orientation.HIGHER_IS_BETTER,
            n,
            2,
        )
    )
    if n == 200:
        spread = max(closes) - min(closes)
        factors.append(
            _factor(
                "price_position_200",
                (closes[-1] - min(closes)) / spread if spread else None,
                Orientation.HIGHER_IS_BETTER,
                n,
                200,
                Status.ZERO_DENOMINATOR if spread == 0 else None,
            )
        )
    else:
        factors.append(
            _factor("price_position_200", None, Orientation.HIGHER_IS_BETTER, n, 200)
        )
    ret120 = closes[-1] / closes[-121] - 1 if n >= 121 else None
    if n < 121 or realized is None:
        factors.append(
            _factor(
                "risk_adjusted_momentum", None, Orientation.HIGHER_IS_BETTER, n, 121
            )
        )
    elif _q(realized) == 0:
        factors.append(
            _factor(
                "risk_adjusted_momentum",
                None,
                Orientation.HIGHER_IS_BETTER,
                n,
                121,
                Status.ZERO_DENOMINATOR,
            )
        )
    else:
        factors.append(
            _factor(
                "risk_adjusted_momentum",
                ret120 / Decimal(str(realized)),
                Orientation.HIGHER_IS_BETTER,
                n,
                121,
            )
        )
    for name, attr in (
        ("volume_change_20", "volume"),
        ("trading_amount_change_20", "trading_amount"),
    ):
        values = [getattr(bar, attr) for bar in series.bars[-40:]]
        if n < 40 or any(value is None for value in values):
            factors.append(
                _factor(
                    name, None, Orientation.HIGHER_IS_BETTER, min(n, len(values)), 40
                )
            )
        else:
            previous = sum(values[:20]) / Decimal(20)
            recent = sum(values[20:]) / Decimal(20)
            factors.append(
                _factor(
                    name,
                    recent / previous - 1 if previous else None,
                    Orientation.HIGHER_IS_BETTER,
                    40,
                    40,
                    Status.ZERO_DENOMINATOR if previous == 0 else None,
                )
            )
    ordered = tuple(sorted(factors, key=lambda factor: FACTOR_ORDER.index(factor.name)))
    features = FeatureSetV1(
        series.symbol, series.market, completed_session_date, ordered
    )
    close_factors = ordered[:-2]
    unavailable = (
        QuantUnavailableCode.INSUFFICIENT_PRICE_HISTORY
        if any(f.status is Status.INSUFFICIENT for f in close_factors)
        else None
    )
    return FeatureOutcomeV1(features, unavailable)
