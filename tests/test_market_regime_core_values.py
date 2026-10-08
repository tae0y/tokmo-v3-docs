from dataclasses import replace
from datetime import date, datetime, timedelta
from decimal import Decimal, getcontext
from zoneinfo import ZoneInfo

import pytest

from tokmo.values.market_regime_core import (
    AdjustmentMode,
    NormalizedDailyBarV1,
    NormalizedDailySeriesV1,
    canonical_decimal,
    compute_pack_digest,
    decimal_context,
    digest_object,
)
from tokmo.programs.market_regime_core import SPEC_DIGEST, evaluate_market_regime_core_v1, pure_spec_v1


SEOUL = ZoneInfo("Asia/Seoul")


def bar(day: int, close: str = "100", **changes: str) -> NormalizedDailyBarV1:
    values = {"open": close, "high": close, "low": close, "close": close, "volume": "10"}
    values.update(changes)
    return NormalizedDailyBarV1(
        session_date=date(2026, 1, day), timestamp=datetime(2026, 1, day, 15, tzinfo=SEOUL),
        **{key: Decimal(value) for key, value in values.items()},
    )


def series(*bars: NormalizedDailyBarV1) -> NormalizedDailySeriesV1:
    return NormalizedDailySeriesV1("ABC", "KRW", AdjustmentMode.ADJUSTED, "Asia/Seoul", date(2026, 1, 31), bars)


def test_v01_input_validation_order_and_digest() -> None:
    one, two = bar(1), bar(2, "101")
    assert series(one, two).input_digest == series(one, two).input_digest
    assert series(one, two).input_digest != series(one, bar(2, "102")).input_digest
    with pytest.raises(ValueError, match="order"): series(two, one)
    with pytest.raises(ValueError, match="duplicate session_date"): series(one, NormalizedDailyBarV1(date(2026, 1, 1), datetime(2026, 1, 1, 16, tzinfo=SEOUL), *(Decimal("100"),) * 4, Decimal("10")))
    with pytest.raises(ValueError, match="high"): bar(1, open="101", high="100")
    with pytest.raises(ValueError, match="close"): bar(1, "0", open="1", high="1", low="0.1")
    with pytest.raises(ValueError, match="volume"): bar(1, volume="-1")
    with pytest.raises(ValueError, match="finite"): bar(1, close="NaN")
    with pytest.raises(ValueError, match="1..200"): series(*([one] * 201))
    with pytest.raises(ValueError, match="symbol"): NormalizedDailySeriesV1("abc", "KRW", AdjustmentMode.ADJUSTED, "Asia/Seoul", date(2026, 1, 31), (one,))


def test_v02_decimal_policy_is_canonical_and_does_not_leak() -> None:
    before = (getcontext().prec, getcontext().rounding, getcontext().traps.copy())
    assert canonical_decimal(Decimal("1.2300")) == "1.23"
    assert canonical_decimal(Decimal("-0.000")) == "0"
    with decimal_context() as context:
        assert context.prec == 50
        assert context.rounding == "ROUND_HALF_EVEN"
        assert Decimal(1) / Decimal(3) == Decimal("0.33333333333333333333333333333333333333333333333333")
    assert (getcontext().prec, getcontext().rounding, getcontext().traps.copy()) == before


def test_v10_serialization_pack_digest_and_caps() -> None:
    start = date(2025, 1, 1)
    bars = tuple(NormalizedDailyBarV1(start + timedelta(days=i), datetime.combine(start + timedelta(days=i), datetime.min.time(), SEOUL), Decimal(100 + i), Decimal(101 + i), Decimal(99 + i), Decimal(100 + i), Decimal("1000")) for i in range(200))
    pack = evaluate_market_regime_core_v1(NormalizedDailySeriesV1("SYNTH", "USD", AdjustmentMode.UNADJUSTED, "Asia/Seoul", bars[-1].session_date, bars))
    assert pack.pack_digest == compute_pack_digest(pack)
    assert pack.pack_digest != compute_pack_digest(replace(pack, symbol="OTHER"))
    assert pack.spec_digest == SPEC_DIGEST and len(pack.components) == 6
    assert all(metric.value is None for component in pack.components for metric in component.metrics if not metric.available)


def test_v10_independent_frozen_spec_digest_kills_drift() -> None:
    # Independently reconstructed from SDS §§3–7, not imported from production.
    spec = {
        "identity":{"program_id":"market-regime-core-v1","version":"1.0.0","kind":"CORE_ANALYZER","formula_version":"market-regime-formula-v1"},
        "input":{"sessions":[1,200],"ordered":"strict","decimal":{"digits":30,"fraction":12},"digest_domain":"market-regime-input-v1"},
        "precision":{"prec":50,"rounding":"ROUND_HALF_EVEN","intermediate_quantization":False},
        "trend":{"sma":[20,50,200],"slopes":[[20,5,25],[50,10,60]],"alignment":[">","<"],"sma200_slope":"UNAVAILABLE_AT_MAX_WINDOW"},
        "rsi":{"period":14,"seed":"SMA_FIRST_14_DIFFERENCES","recurrence":[13,14],"flat":50,"up":100,"down":0,"thresholds":{"overbought":">=70","oversold":"<=30"}},
        "macd":{"ema":[12,26],"seed":"SMA","alpha":"2/(period+1)","signal":[9,"2/10"],"warmup":34},
        "volatility":{"atr":[14,"PREVIOUS_CLOSE",15],"rv":[20,"POPULATION",252,21],"bollinger":[20,2,"POPULATION","ZERO_WIDTH_0.5"],"medians":[34,39,"EVEN_AVERAGE"],"high":[">=1.50","OR","ZERO_GUARD"],"compressed":["<=0.75","AND"]},
        "volume":{"window":20,"population_z":True,"thresholds":[["EXTREME_HIGH",">=2",">=3"],["HIGH",">=1.5",">=2"],["LOW","<=0.5","<=-2"]],"join":"AND","zero":["ALL_ZERO","CONSTANT_NORMAL"]},
        "position":{"window":200,"extrema":"HIGH_LOW","range":["LOWER<=0.20","UPPER>=0.80"],"equal":["0.5","0"]},
        "synthesis":{"precedence":["INSUFFICIENT","HIGH_VOLATILITY","UPTREND","DOWNTREND","RANGE","TRANSITION"],"range_slopes":["<=0.01","<=0.02"],"range_bb":["0.20","0.80"],"invariant":"SYNTHESIS_INVARIANT_CONFLICT"},
        "output":{"components":6,"metrics":48,"reasons":32,"counter_signals":32,"conflicts":16,"limitations":16,"pack_bytes":65536,"unavailable":None},
    }
    assert spec == pure_spec_v1()
    assert digest_object(spec) == SPEC_DIGEST
    for mutation in (("rv", 2, 365), ("compressed", 0, "<0.75")):
        changed = {**spec, "volatility": {**spec["volatility"]}}
        values = list(changed["volatility"][mutation[0]])
        values[mutation[1]] = mutation[2]
        changed["volatility"][mutation[0]] = values
        assert digest_object(changed) != SPEC_DIGEST
