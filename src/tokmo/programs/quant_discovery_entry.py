"""Reference-only trend pullback zones for the Quant discovery engine."""

from decimal import Decimal, ROUND_HALF_UP

from tokmo.values.quant_discovery import FactorStatus, ReferenceEntryZoneV1

Q = Decimal("0.000001")


def _q(value):
    return value.quantize(Q, rounding=ROUND_HALF_UP)


def build_reference_entry_zone(series, features) -> ReferenceEntryZoneV1 | None:
    if series.symbol != features.symbol or series.market is not features.market or len(series.bars) < 60:
        return None
    if any(bar.open is None or bar.high is None or bar.low is None or bar.close is None for bar in series.bars[-60:]):
        return None
    by_name = {factor.name: factor for factor in features.factors}
    required = ("close_to_sma_20", "close_to_sma_60", "rsi_14")
    if any(by_name[name].status is not FactorStatus.AVAILABLE for name in required):
        return None
    close = series.bars[-1].close
    sma20 = sum(bar.close for bar in series.bars[-20:]) / Decimal(20)
    sma60 = sum(bar.close for bar in series.bars[-60:]) / Decimal(60)
    rsi = by_name["rsi_14"].raw_value
    if not (close > sma20 > sma60 and Decimal(45) <= rsi <= Decimal(65)):
        return None
    true_ranges = []
    for index in range(len(series.bars) - 14, len(series.bars)):
        bar = series.bars[index]
        previous = series.bars[index - 1].close
        true_ranges.append(max(bar.high - bar.low, abs(bar.high - previous), abs(bar.low - previous)))
    atr = sum(true_ranges) / Decimal(14)
    if atr <= 0 or close > sma20 + atr:
        return None
    support20 = min(bar.low for bar in series.bars[-20:])
    lower = max(support20, sma20 - Decimal("0.5") * atr)
    upper = min(close, sma20 + Decimal("0.25") * atr)
    if lower > upper:
        return None
    invalidation = min(support20, lower - Decimal("0.5") * atr)
    return ReferenceEntryZoneV1(
        _q(lower), _q(upper), "TREND_PULLBACK",
        ("SMA20", "SMA60", "ATR14", "support20"), _q(invalidation), 1,
        _q(close), _q((lower - close) / close), 50, "단일 가격·거래량 setup",
        f"close <= {_q(invalidation)} or close < {_q(sma60)}",
    )
