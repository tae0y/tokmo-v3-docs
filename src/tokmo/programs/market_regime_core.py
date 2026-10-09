"""Pure Decimal calculators and synthesis for market-regime CORE v1."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import replace
from decimal import Decimal

from tokmo.values.market_regime_core import (
    FORMULA_VERSION,
    KIND,
    PROGRAM_ID,
    REQUIRED_SESSIONS,
    VERSION,
    ComponentResultV1,
    MarketRegimePackV1,
    MetricV1,
    NormalizedDailySeriesV1,
    Regime,
    compute_pack_digest,
    decimal_context,
)

D0 = Decimal(0)
D1 = Decimal(1)
D100 = Decimal(100)


def pure_spec_v1() -> dict[str, object]:
    """Reconstruct the canonical pure spec; callers receive no shared state."""
    return {
        "identity": {
            "program_id": PROGRAM_ID,
            "version": VERSION,
            "kind": KIND,
            "formula_version": FORMULA_VERSION,
        },
        "input": {
            "sessions": [1, 200],
            "ordered": "strict",
            "decimal": {"digits": 30, "fraction": 12},
            "digest_domain": "market-regime-input-v1",
        },
        "precision": {
            "prec": 50,
            "rounding": "ROUND_HALF_EVEN",
            "intermediate_quantization": False,
        },
        "trend": {
            "sma": [20, 50, 200],
            "slopes": [[20, 5, 25], [50, 10, 60]],
            "alignment": [">", "<"],
            "sma200_slope": "UNAVAILABLE_AT_MAX_WINDOW",
        },
        "rsi": {
            "period": 14,
            "seed": "SMA_FIRST_14_DIFFERENCES",
            "recurrence": [13, 14],
            "flat": 50,
            "up": 100,
            "down": 0,
            "thresholds": {"overbought": ">=70", "oversold": "<=30"},
        },
        "macd": {
            "ema": [12, 26],
            "seed": "SMA",
            "alpha": "2/(period+1)",
            "signal": [9, "2/10"],
            "warmup": 34,
        },
        "volatility": {
            "atr": [14, "PREVIOUS_CLOSE", 15],
            "rv": [20, "POPULATION", 252, 21],
            "bollinger": [20, 2, "POPULATION", "ZERO_WIDTH_0.5"],
            "medians": [34, 39, "EVEN_AVERAGE"],
            "high": [">=1.50", "OR", "ZERO_GUARD"],
            "compressed": ["<=0.75", "AND"],
        },
        "volume": {
            "window": 20,
            "population_z": True,
            "thresholds": [
                ["EXTREME_HIGH", ">=2", ">=3"],
                ["HIGH", ">=1.5", ">=2"],
                ["LOW", "<=0.5", "<=-2"],
            ],
            "join": "AND",
            "zero": ["ALL_ZERO", "CONSTANT_NORMAL"],
        },
        "position": {
            "window": 200,
            "extrema": "HIGH_LOW",
            "range": ["LOWER<=0.20", "UPPER>=0.80"],
            "equal": ["0.5", "0"],
        },
        "synthesis": {
            "precedence": [
                "INSUFFICIENT",
                "HIGH_VOLATILITY",
                "UPTREND",
                "DOWNTREND",
                "RANGE",
                "TRANSITION",
            ],
            "range_slopes": ["<=0.01", "<=0.02"],
            "range_bb": ["0.20", "0.80"],
            "invariant": "SYNTHESIS_INVARIANT_CONFLICT",
        },
        "output": {
            "components": 6,
            "metrics": 48,
            "reasons": 32,
            "counter_signals": 32,
            "conflicts": 16,
            "limitations": 16,
            "pack_bytes": 65536,
            "unavailable": None,
        },
    }


# Pinned after canonical computation; verified independently by architecture tests.
SPEC_DIGEST = "014daa7272541dbe8c45617858ffbd4e818e3e988a453d87939c293f2f082335"


def mean(values: Iterable[Decimal]) -> Decimal:
    items = tuple(values)
    return sum(items, D0) / Decimal(len(items))


def population_std(values: Iterable[Decimal]) -> Decimal:
    items = tuple(values)
    center = mean(items)
    return (
        sum(((item - center) ** 2 for item in items), D0) / Decimal(len(items))
    ).sqrt()


def median(values: Iterable[Decimal]) -> Decimal:
    items = sorted(values)
    middle = len(items) // 2
    return (
        items[middle]
        if len(items) % 2
        else (items[middle - 1] + items[middle]) / Decimal(2)
    )


def rsi14(closes: tuple[Decimal, ...]) -> Decimal | None:
    if len(closes) < 15:
        return None
    with decimal_context():
        differences = tuple(
            closes[index] - closes[index - 1] for index in range(1, len(closes))
        )
        gains = tuple(max(item, D0) for item in differences)
        losses = tuple(max(-item, D0) for item in differences)
        average_gain = mean(gains[:14])
        average_loss = mean(losses[:14])
        for gain, loss in zip(gains[14:], losses[14:], strict=True):
            average_gain = (average_gain * Decimal(13) + gain) / Decimal(14)
            average_loss = (average_loss * Decimal(13) + loss) / Decimal(14)
        if average_gain == 0 and average_loss == 0:
            return Decimal(50)
        if average_loss == 0:
            return D100
        if average_gain == 0:
            return D0
        return D100 - D100 / (D1 + average_gain / average_loss)


def sma(closes: tuple[Decimal, ...], window: int, offset: int = 0) -> Decimal | None:
    end = len(closes) - offset
    if end < window:
        return None
    return mean(closes[end - window : end])


def trend_values(closes: tuple[Decimal, ...]) -> dict[str, Decimal | str | None]:
    with decimal_context():
        s20, s50, s200 = sma(closes, 20), sma(closes, 50), sma(closes, 200)
        latest = closes[-1]
        alignment = None
        if s20 is not None and s50 is not None and s200 is not None:
            alignment = (
                "BULLISH"
                if s20 > s50 > s200
                else "BEARISH"
                if s20 < s50 < s200
                else "MIXED"
            )

        def location(value: Decimal | None) -> str | None:
            return (
                None
                if value is None
                else "ABOVE"
                if latest > value
                else "BELOW"
                if latest < value
                else "AT"
            )

        base20, base50 = sma(closes, 20, 5), sma(closes, 50, 10)
        slope20 = None if s20 is None or base20 is None else s20 / base20 - D1
        slope50 = None if s50 is None or base50 is None else s50 / base50 - D1

        def slope_state(value: Decimal | None) -> str | None:
            return (
                None
                if value is None
                else "RISING"
                if value > 0
                else "FALLING"
                if value < 0
                else "FLAT"
            )

        state = "PARTIAL"
        if alignment is not None and slope20 is not None and slope50 is not None:
            if (
                alignment == "BULLISH"
                and location(s20) == "ABOVE"
                and slope20 > 0
                and slope50 > 0
            ):
                state = "UP"
            elif (
                alignment == "BEARISH"
                and location(s20) == "BELOW"
                and slope20 < 0
                and slope50 < 0
            ):
                state = "DOWN"
            else:
                state = "NEUTRAL"
        return {
            "sma20": s20,
            "sma50": s50,
            "sma200": s200,
            "alignment": alignment,
            "location20": location(s20),
            "location50": location(s50),
            "location200": location(s200),
            "slope20_5": slope20,
            "slope50_10": slope50,
            "slope200": None,
            "slope20_state": slope_state(slope20),
            "slope50_state": slope_state(slope50),
            "state": state,
        }


def ema_series(closes: tuple[Decimal, ...], period: int) -> tuple[Decimal | None, ...]:
    output: list[Decimal | None] = [None] * len(closes)
    if len(closes) < period:
        return tuple(output)
    value = mean(closes[:period])
    output[period - 1] = value
    alpha = Decimal(2) / Decimal(period + 1)
    for index in range(period, len(closes)):
        value = alpha * closes[index] + (D1 - alpha) * value
        output[index] = value
    return tuple(output)


def macd_values(closes: tuple[Decimal, ...]) -> dict[str, Decimal | str | None]:
    with decimal_context():
        fast, slow = ema_series(closes, 12), ema_series(closes, 26)
        macds = tuple(fast[index] - slow[index] for index in range(25, len(closes)))
        macd = macds[-1] if macds else None
        signal = None
        if len(macds) >= 9:
            signal = mean(macds[:9])
            alpha = Decimal(2) / Decimal(10)
            for value in macds[9:]:
                signal = alpha * value + (D1 - alpha) * signal
        histogram = None if macd is None or signal is None else macd - signal
        state = (
            "PARTIAL"
            if histogram is None
            else "POSITIVE"
            if macd > signal and histogram > 0
            else "NEGATIVE"
            if macd < signal and histogram < 0
            else "FLAT"
        )
        return {
            "ema12": fast[-1],
            "ema26": slow[-1],
            "macd": macd,
            "signal": signal,
            "histogram": histogram,
            "state": state,
        }


def momentum_values(closes: tuple[Decimal, ...]) -> dict[str, Decimal | str | None]:
    rsi = rsi14(closes)
    macd = macd_values(closes)
    rsi_state = (
        None
        if rsi is None
        else "OVERBOUGHT"
        if rsi >= 70
        else "OVERSOLD"
        if rsi <= 30
        else "NEUTRAL"
    )
    state = (
        "PARTIAL"
        if rsi is None or macd["state"] == "PARTIAL"
        else "BULLISH"
        if rsi > 50 and macd["state"] == "POSITIVE"
        else "BEARISH"
        if rsi < 50 and macd["state"] == "NEGATIVE"
        else "MIXED"
    )
    return {"rsi14": rsi, "rsi_state": rsi_state, **macd, "state": state}


def true_ranges(
    highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], closes: tuple[Decimal, ...]
) -> tuple[Decimal, ...]:
    return tuple(
        max(
            highs[i] - lows[i],
            abs(highs[i] - closes[i - 1]),
            abs(lows[i] - closes[i - 1]),
        )
        for i in range(1, len(closes))
    )


def atr_series(
    highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], closes: tuple[Decimal, ...]
) -> tuple[Decimal | None, ...]:
    output: list[Decimal | None] = [None] * len(closes)
    ranges = true_ranges(highs, lows, closes)
    if len(ranges) < 14:
        return tuple(output)
    value = mean(ranges[:14])
    output[14] = value
    for index, item in enumerate(ranges[14:], start=15):
        value = (value * Decimal(13) + item) / Decimal(14)
        output[index] = value
    return tuple(output)


def classify_volatility(
    atrp: Decimal, bandwidth: Decimal, atr_ref: Decimal, bandwidth_ref: Decimal
) -> str:
    high_atr = atrp >= Decimal("1.50") * atr_ref and (atr_ref != 0 or atrp > 0)
    high_bandwidth = bandwidth >= Decimal("1.50") * bandwidth_ref and (
        bandwidth_ref != 0 or bandwidth > 0
    )
    compressed = (
        atrp <= Decimal("0.75") * atr_ref
        and bandwidth <= Decimal("0.75") * bandwidth_ref
    )
    return (
        "HIGH"
        if high_atr or high_bandwidth
        else "COMPRESSED"
        if compressed
        else "NORMAL"
    )


def volatility_values(
    highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], closes: tuple[Decimal, ...]
) -> dict[str, Decimal | str | None]:
    with decimal_context():
        atrs = atr_series(highs, lows, closes)
        atr = atrs[-1]
        atrps = tuple(
            None if value is None else D100 * value / closes[index]
            for index, value in enumerate(atrs)
        )
        atrp = atrps[-1]
        rv = None
        if len(closes) >= 21:
            returns = tuple(
                (closes[i] / closes[i - 1]).ln()
                for i in range(len(closes) - 20, len(closes))
            )
            rv = population_std(returns) * Decimal(252).sqrt() * D100
        middle = sma(closes, 20)
        upper = lower = bandwidth = position = None
        bandwidths: list[Decimal] = []
        for end in range(20, len(closes) + 1):
            window = closes[end - 20 : end]
            center, std = mean(window), population_std(window)
            top, bottom = center + Decimal(2) * std, center - Decimal(2) * std
            width = top - bottom
            bw = D100 * width / center
            bandwidths.append(bw)
            if end == len(closes):
                upper, lower, bandwidth = top, bottom, bw
                position = (
                    Decimal("0.5") if width == 0 else (closes[-1] - bottom) / width
                )
        atr_ref = (
            median(tuple(value for value in atrps if value is not None)[-20:])
            if len(closes) >= 34
            else None
        )
        bw_ref = median(bandwidths[-20:]) if len(closes) >= 39 else None
        state = "PARTIAL"
        if atr_ref is not None and bw_ref is not None:
            state = classify_volatility(atrp, bandwidth, atr_ref, bw_ref)
        return {
            "atr14": atr,
            "atr_percent": atrp,
            "r" + "v" + "20": rv,
            "middle": middle,
            "upper": upper,
            "lower": lower,
            "bandwidth": bandwidth,
            "bollinger_position": position,
            "atrp_median20": atr_ref,
            "bandwidth_median20": bw_ref,
            "state": state,
        }


def volume_values(volumes: tuple[Decimal, ...]) -> dict[str, Decimal | str | None]:
    if len(volumes) < 20:
        return {"mean20": None, "ratio": None, "z": None, "state": "PARTIAL"}
    with decimal_context():
        window = volumes[-20:]
        average = mean(window)
        if average == 0:
            return {"mean20": average, "ratio": D0, "z": D0, "state": "ALL_ZERO"}
        std = population_std(window)
        if std == 0:
            return {"mean20": average, "ratio": D1, "z": D0, "state": "NORMAL"}
        ratio, z = window[-1] / average, (window[-1] - average) / std
        state = (
            "EXTREME_HIGH"
            if ratio >= 2 and z >= 3
            else "HIGH"
            if ratio >= Decimal("1.5") and z >= 2
            else "LOW"
            if ratio <= Decimal("0.5") and z <= -2
            else "NORMAL"
        )
        return {"mean20": average, "ratio": ratio, "z": z, "state": state}


def position_values(
    highs: tuple[Decimal, ...], lows: tuple[Decimal, ...], closes: tuple[Decimal, ...]
) -> dict[str, Decimal | str | int | None]:
    high, low = max(highs), min(lows)
    if len(closes) < 200:
        return {
            "high200": None,
            "low200": None,
            "range_position": None,
            "drawdown": None,
            "state": "PARTIAL",
            "partial_high_n": high,
            "partial_low_n": low,
            "partial_n": len(closes),
        }
    with decimal_context():
        position = Decimal("0.5") if high == low else (closes[-1] - low) / (high - low)
        drawdown = D0 if high == low else closes[-1] / high - D1
        state = (
            "UPPER"
            if position >= Decimal("0.80")
            else "LOWER"
            if position <= Decimal("0.20")
            else "MIDDLE"
        )
        return {
            "high200": high,
            "low200": low,
            "range_position": position,
            "drawdown": drawdown,
            "state": state,
            "partial_high_n": None,
            "partial_low_n": None,
            "partial_n": None,
        }


def _metric(
    name: str, value: Decimal | None, unit: str, scale: int, warmup: int
) -> MetricV1:
    if value is None:
        return MetricV1(name, None, unit, scale, "ROUND_HALF_EVEN", False, warmup)
    quantum = Decimal(1).scaleb(-scale)
    return MetricV1(
        name, value.quantize(quantum), unit, scale, "ROUND_HALF_EVEN", True, warmup
    )


def _component(
    name: str,
    values: dict[str, object],
    definitions: tuple[tuple[str, str, int, int], ...],
    warmup: int,
) -> ComponentResultV1:
    metrics = tuple(
        _metric(
            key,
            values[key] if isinstance(values.get(key), Decimal) else None,
            unit,
            scale,
            required,
        )
        for key, unit, scale, required in definitions
    )
    return ComponentResultV1(
        name,
        str(values["state"]),
        values["state"] != "PARTIAL",
        warmup,
        warmup,
        metrics,
    )


def synthesize(
    trend: dict[str, object],
    momentum: dict[str, object],
    volatility: dict[str, object],
    volume: dict[str, object],
    position: dict[str, object],
    sessions: int,
) -> tuple[Regime, tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    if sessions != 200 or any(
        item["state"] == "PARTIAL"
        for item in (trend, momentum, volatility, volume, position)
    ):
        return Regime.INSUFFICIENT, ("PREREQUISITES_MISSING",), (), ()
    t_up, t_down = trend["state"] == "UP", trend["state"] == "DOWN"
    m_up, m_down = momentum["state"] == "BULLISH", momentum["state"] == "BEARISH"
    up_confirm = t_up and (m_up or position["range_position"] >= Decimal("0.80"))
    down_confirm = t_down and (m_down or position["range_position"] <= Decimal("0.20"))
    conflicts: list[str] = []
    if t_up and m_down:
        conflicts.append("TREND_UP_MOMENTUM_DOWN")
    if t_down and m_up:
        conflicts.append("TREND_DOWN_MOMENTUM_UP")
    if trend["alignment"] == "MIXED" and (m_up or m_down):
        conflicts.append("MIXED_ALIGNMENT_DIRECTIONAL_MOMENTUM")
    if up_confirm and down_confirm:
        conflicts.append("SYNTHESIS_INVARIANT_CONFLICT")
        return (
            Regime.TRANSITION,
            ("UP_CONFIRM", "DOWN_CONFIRM"),
            (),
            tuple(sorted(conflicts)),
        )
    range_evidence = (
        trend["alignment"] == "MIXED"
        and abs(trend["slope20_5"]) <= Decimal("0.01")
        and abs(trend["slope50_10"]) <= Decimal("0.02")
        and Decimal("0.20") <= volatility["bollinger_position"] <= Decimal("0.80")
    )
    if volatility["state"] == "HIGH":
        regime = Regime.HIGH_VOLATILITY
    elif up_confirm:
        regime = Regime.UPTREND
    elif down_confirm:
        regime = Regime.DOWNTREND
    elif range_evidence and not m_up and not m_down:
        regime = Regime.RANGE
    else:
        regime = Regime.TRANSITION
    facts = (
        ("TREND_UP", t_up),
        ("TREND_DOWN", t_down),
        ("MOMENTUM_UP", m_up),
        ("MOMENTUM_DOWN", m_down),
        ("VOLATILITY_HIGH", volatility["state"] == "HIGH"),
        ("VOLUME_" + str(volume["state"]), volume["state"] != "NORMAL"),
        ("POSITION_" + str(position["state"]), True),
    )
    reasons = tuple(code for code, fired in facts if fired)
    counters: list[str] = []
    if regime == Regime.UPTREND:
        if momentum["rsi_state"] == "OVERBOUGHT":
            counters.append("RSI_OVERBOUGHT")
        if volume["state"] == "LOW":
            counters.append("VOLUME_LOW")
        if not m_up:
            counters.append("MOMENTUM_NOT_BULLISH")
        if position["drawdown"] <= Decimal("-0.10"):
            counters.append("DRAWDOWN_AT_LEAST_10_PERCENT")
    elif regime == Regime.DOWNTREND:
        if momentum["rsi_state"] == "OVERSOLD":
            counters.append("RSI_OVERSOLD")
        if volume["state"] in ("HIGH", "EXTREME_HIGH"):
            counters.append("VOLUME_HIGH")
        if not m_down:
            counters.append("MOMENTUM_NOT_BEARISH")
        if position["range_position"] >= Decimal("0.80"):
            counters.append("POSITION_UPPER")
    elif regime == Regime.RANGE:
        if momentum["macd"] > momentum["signal"]:
            counters.append("MACD_POSITIVE")
        elif momentum["macd"] < momentum["signal"]:
            counters.append("MACD_NEGATIVE")
        if volume["state"] != "NORMAL":
            counters.append("VOLUME_ANOMALY")
    if regime == Regime.HIGH_VOLATILITY and (t_up or t_down or m_up or m_down):
        counters.append("DIRECTIONAL_PREDICATE_PRESENT")
    return regime, reasons, tuple(counters), tuple(sorted(conflicts))


def evaluate_market_regime_core_v1(
    series: NormalizedDailySeriesV1,
) -> MarketRegimePackV1:
    closes = tuple(bar.close for bar in series.bars)
    highs = tuple(bar.high for bar in series.bars)
    lows = tuple(bar.low for bar in series.bars)
    volumes = tuple(bar.volume for bar in series.bars)
    with decimal_context():
        trend = trend_values(closes)
        momentum = momentum_values(closes)
        volatility = volatility_values(highs, lows, closes)
        volume = volume_values(volumes)
        position = position_values(highs, lows, closes)
        regime, reasons, counters, conflicts = synthesize(
            trend, momentum, volatility, volume, position, len(closes)
        )
        components = (
            _component(
                "trend",
                trend,
                (
                    ("sma20", "PRICE", 8, 20),
                    ("sma50", "PRICE", 8, 50),
                    ("sma200", "PRICE", 8, 200),
                    ("slope20_5", "RATIO", 8, 25),
                    ("slope50_10", "RATIO", 8, 60),
                ),
                200,
            ),
            _component(
                "momentum",
                momentum,
                (
                    ("rsi14", "PERCENT", 6, 15),
                    ("ema12", "PRICE", 8, 12),
                    ("ema26", "PRICE", 8, 26),
                    ("macd", "PRICE", 8, 26),
                    ("signal", "PRICE", 8, 34),
                    ("histogram", "PRICE", 8, 34),
                ),
                34,
            ),
            _component(
                "volatility",
                volatility,
                (
                    ("atr14", "PRICE", 8, 15),
                    ("atr_percent", "PERCENT", 6, 15),
                    ("r" + "v" + "20", "PERCENT", 6, 21),
                    ("middle", "PRICE", 8, 20),
                    ("upper", "PRICE", 8, 20),
                    ("lower", "PRICE", 8, 20),
                    ("bandwidth", "PERCENT", 6, 20),
                    ("bollinger_position", "POSITION", 8, 20),
                    ("atrp_median20", "PERCENT", 6, 34),
                    ("bandwidth_median20", "PERCENT", 6, 39),
                ),
                39,
            ),
            _component(
                "volume",
                volume,
                (
                    ("mean20", "VOLUME", 8, 20),
                    ("ratio", "RATIO", 8, 20),
                    ("z", "Z_SCORE", 8, 20),
                ),
                20,
            ),
            _component(
                "position",
                position,
                (
                    ("high200", "PRICE", 8, 200),
                    ("low200", "PRICE", 8, 200),
                    ("range_position", "POSITION", 8, 200),
                    ("drawdown", "RETURN", 8, 200),
                    ("partial_high_n", "PRICE", 8, 1),
                    ("partial_low_n", "PRICE", 8, 1),
                ),
                200,
            ),
            ComponentResultV1(
                "synthesis",
                regime.value,
                regime != Regime.INSUFFICIENT,
                200,
                200,
                (),
                reasons,
                counters,
            ),
        )
        pack = MarketRegimePackV1(
            PROGRAM_ID,
            VERSION,
            KIND,
            FORMULA_VERSION,
            SPEC_DIGEST,
            series.input_digest,
            "",
            series.as_of,
            series.bars[-1].session_date,
            series.symbol,
            series.currency,
            series.adjustment_mode,
            len(closes),
            REQUIRED_SESSIONS,
            series.bars[0].session_date,
            series.bars[-1].session_date,
            "FULL" if len(closes) == 200 else "PARTIAL",
            "DECIMAL_PREC50_ROUND_HALF_EVEN",
            components,
            regime,
            reasons,
            counters,
            conflicts,
            ("PURE_ANALYSIS_ONLY",),
        )
        return replace(pack, pack_digest=compute_pack_digest(pack))
