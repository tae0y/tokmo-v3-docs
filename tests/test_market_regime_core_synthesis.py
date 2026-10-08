from decimal import Decimal as D

from tokmo.programs.market_regime_core import synthesize
from tokmo.values.market_regime_core import Regime


def parts():
    trend = {"state": "NEUTRAL", "alignment": "MIXED", "slope20_5": D("0"), "slope50_10": D("0")}
    momentum = {"state": "MIXED", "rsi_state": "NEUTRAL", "macd": D("0"), "signal": D("0")}
    volatility = {"state": "NORMAL", "bollinger_position": D("0.5")}
    volume = {"state": "NORMAL"}
    position = {"state": "MIDDLE", "range_position": D("0.5"), "drawdown": D("-0.01")}
    return trend, momentum, volatility, volume, position


def test_v09_all_regimes_and_first_match_precedence() -> None:
    t, m, v, vol, p = parts()
    assert synthesize(t, m, v, vol, p, 199)[0] == Regime.INSUFFICIENT
    assert synthesize(t, m, v, vol, p, 200)[0] == Regime.RANGE
    t["slope20_5"] = D("0.02")
    assert synthesize(t, m, v, vol, p, 200)[0] == Regime.TRANSITION
    t.update(state="UP", alignment="BULLISH"); m["state"] = "BULLISH"
    assert synthesize(t, m, v, vol, p, 200)[0] == Regime.UPTREND
    t.update(state="DOWN", alignment="BEARISH"); m["state"] = "BEARISH"
    assert synthesize(t, m, v, vol, p, 200)[0] == Regime.DOWNTREND
    v["state"] = "HIGH"
    regime, reasons, counters, conflicts = synthesize(t, m, v, vol, p, 200)
    assert regime == Regime.HIGH_VOLATILITY
    assert reasons.index("TREND_DOWN") < reasons.index("MOMENTUM_DOWN") < reasons.index("VOLATILITY_HIGH")
    assert counters == ("DIRECTIONAL_PREDICATE_PRESENT",) and conflicts == ()


def test_v09_conflicts_are_closed_and_sorted() -> None:
    t, m, v, vol, p = parts()
    t.update(state="UP", alignment="MIXED"); m["state"] = "BEARISH"
    conflicts = synthesize(t, m, v, vol, p, 200)[3]
    assert conflicts == tuple(sorted(conflicts))
    assert conflicts == ("MIXED_ALIGNMENT_DIRECTIONAL_MOMENTUM", "TREND_UP_MOMENTUM_DOWN")
