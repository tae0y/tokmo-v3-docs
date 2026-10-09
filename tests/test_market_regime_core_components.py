import hashlib
import json
from decimal import Decimal
from pathlib import Path

from tokmo.programs import market_regime_core as core
from tokmo.values.market_regime_core import decimal_context

D = Decimal
FIXTURES = Path(__file__).parent / "fixtures" / "market_regime_core"
FIXTURE_SHA256 = {
    "COMPRESSED_EXACT_075_MEDIAN_BOUNDARY.json": "ee164e6051b6af7bc5ada02a8419feaa76b5da18ca47cbb73380bb6ccd4f2824",
    "textbook_vectors.json": "2ec3ac57fa8447b96a6c42c75d618a2ea7d83020e09d37823af02bcc8dabc7ce",
}


def load_fixture(name: str) -> dict[str, object]:
    raw = (FIXTURES / name).read_bytes()
    assert hashlib.sha256(raw).hexdigest() == FIXTURE_SHA256[name]
    fixture = json.loads(raw)
    assert fixture["synthetic"] is True
    return fixture


def independent_rsi(
    closes: tuple[Decimal, ...], *, seed_period: int = 14, rolling_sma: bool = False
) -> Decimal:
    """Small test oracle for the Wilder seed/first-recurrence contract."""
    with decimal_context():
        differences = tuple(
            closes[index] - closes[index - 1] for index in range(1, len(closes))
        )
        gains = tuple(max(value, D(0)) for value in differences)
        losses = tuple(max(-value, D(0)) for value in differences)
        if rolling_sma:
            average_gain = sum(gains[-14:], D(0)) / D(14)
            average_loss = sum(losses[-14:], D(0)) / D(14)
        else:
            average_gain = sum(gains[:seed_period], D(0)) / D(seed_period)
            average_loss = sum(losses[:seed_period], D(0)) / D(seed_period)
            for gain, loss in zip(gains[14:], losses[14:], strict=True):
                average_gain = (average_gain * D(13) + gain) / D(14)
                average_loss = (average_loss * D(13) + loss) / D(14)
        return D(100) - D(100) / (D(1) + average_gain / average_loss)


def independent_rv20(closes: tuple[Decimal, ...], annualizer: Decimal) -> Decimal:
    """Independent 20-log-return population-volatility oracle."""
    with decimal_context():
        returns = tuple(
            (closes[index] / closes[index - 1]).ln() for index in range(1, 21)
        )
        center = sum(returns, D(0)) / D(20)
        variance = sum(((value - center) ** 2 for value in returns), D(0)) / D(20)
        return variance.sqrt() * annualizer.sqrt() * D(100)


def test_v03_rsi_wilder_seed_edges_and_warmup() -> None:
    assert core.rsi14(tuple([D("10")] * 14)) is None
    assert core.rsi14(tuple([D("10")] * 15)) == D("50")
    assert core.rsi14(tuple(D(index) for index in range(1, 16))) == D("100")
    assert core.rsi14(tuple(D(20 - index) for index in range(15))) == D("0")
    fixture = load_fixture("textbook_vectors.json")["rsi_wilder"]
    closes = tuple(map(D, fixture["closes"]))
    expected = independent_rsi(closes)
    assert closes[14] != closes[13] and closes[15] != closes[14]
    assert expected.quantize(D("0.000001")) == D(fixture["expected_rsi6"])
    assert core.rsi14(closes).quantize(D("0.000001")) == D(fixture["expected_rsi6"])


def test_v04_trend_warmups_alignment_locations_and_slopes() -> None:
    rising = tuple(D(index) for index in range(1, 201))
    for size, available in (
        (19, False),
        (20, True),
        (24, True),
        (25, True),
        (49, True),
        (50, True),
        (59, True),
        (60, True),
        (199, True),
        (200, True),
    ):
        values = core.trend_values(rising[:size])
        assert (values["sma20"] is not None) is (size >= 20)
        assert (values["slope20_5"] is not None) is (size >= 25)
        assert (values["sma50"] is not None) is (size >= 50)
        assert (values["slope50_10"] is not None) is (size >= 60)
        assert (values["sma200"] is not None) is (size == 200)
        assert values["slope200"] is None
    assert core.trend_values(rising)["state"] == "UP"
    flat = core.trend_values(tuple([D("10")] * 200))
    assert flat["alignment"] == "MIXED" and flat["location20"] == "AT"


def test_v05_macd_sma_seed_warmup_and_pinned_vector() -> None:
    fixture = load_fixture("textbook_vectors.json")["macd_arithmetic_progression"]
    start, stop = map(int, fixture["closes"].split(".."))
    closes = tuple(D(index) for index in range(start, stop + 1))
    for size in (25, 26, 33, 34, 35):
        values = core.macd_values(closes[:size])
        assert (values["macd"] is not None) is (size >= 26)
        assert (values["signal"] is not None) is (size >= 34)
    # A unit-step arithmetic progression has EMA12-EMA26 exactly 7 after both SMA seeds.
    assert core.macd_values(closes)["macd"] == D(fixture["expected_macd"])
    assert core.macd_values(closes)["signal"] == D(fixture["expected_signal"])
    assert core.macd_values(closes)["histogram"] == D("0")


def test_v06_atr_rv_bollinger_and_adaptive_boundaries() -> None:
    highs = (D("10"), D("15"))
    lows = (D("9"), D("14"))
    closes = (D("10"), D("14.5"))
    assert core.true_ranges(highs, lows, closes) == (D("5"),)
    flat = tuple([D("10")] * 39)
    values = core.volatility_values(flat, flat, flat)
    assert values["bollinger_position"] == D("0.5")
    assert values["rv20"] == 0
    assert values["state"] == "COMPRESSED"
    boundary = load_fixture("COMPRESSED_EXACT_075_MEDIAN_BOUNDARY.json")
    assert (
        core.classify_volatility(
            D(boundary["atr_percent"]),
            D(boundary["bandwidth"]),
            D(boundary["atrp_median20"]),
            D(boundary["bandwidth_median20"]),
        )
        == boundary["expected_state"]
    )
    assert (
        core.classify_volatility(D("3.0000000001"), D("6"), D("4"), D("8")) == "NORMAL"
    )
    assert core.classify_volatility(D("6"), D("1"), D("4"), D("8")) == "HIGH"
    rv_fixture = load_fixture("textbook_vectors.json")["rv20"]
    varying = tuple(map(D, rv_fixture["closes"]))
    oracle252 = independent_rv20(varying, D(rv_fixture["annualizer"]))
    mutant365 = independent_rv20(varying, D(365))
    assert oracle252 != 0 and oracle252 != mutant365
    assert oracle252.quantize(D("0.000001")) == D(rv_fixture["expected_rv6"])
    assert mutant365.quantize(D("0.000001")) == D(
        rv_fixture["annualizer365_mutant_rv6"]
    )
    assert (
        core.SPEC_DIGEST
        == "014daa7272541dbe8c45617858ffbd4e818e3e988a453d87939c293f2f082335"
    )
    actual = core.volatility_values(varying, varying, varying)["rv20"]
    assert actual.quantize(D("0.000001")) == D(rv_fixture["expected_rv6"])


def test_v07_volume_dual_thresholds_and_zero_rules() -> None:
    assert core.volume_values(tuple([D("0")] * 20))["state"] == "ALL_ZERO"
    assert core.volume_values(tuple([D("5")] * 20))["state"] == "NORMAL"
    # Ratio alone is insufficient: one large prior observation suppresses latest z.
    candidate = tuple([D("1")] * 18 + [D("100"), D("10")])
    values = core.volume_values(candidate)
    assert (
        values["ratio"] >= D("1.5")
        and values["z"] < D("2")
        and values["state"] == "NORMAL"
    )
    low = tuple([D("10")] * 19 + [D("0")])
    assert core.volume_values(low)["ratio"] == 0


def test_v08_position_exact_200_extrema_edges() -> None:
    close199 = tuple([D("50")] * 199)
    partial = core.position_values(
        tuple([D("100")] + [D("50")] * 198), tuple([D("1")] + [D("50")] * 198), close199
    )
    assert (
        partial["high200"] is None
        and partial["partial_high_n"] == 100
        and partial["partial_low_n"] == 1
    )
    equal = core.position_values(
        tuple([D("7")] * 200), tuple([D("7")] * 200), tuple([D("7")] * 200)
    )
    assert equal["range_position"] == D("0.5") and equal["drawdown"] == 0
    highs = tuple([D("100")] * 200)
    lows = tuple([D("0.1")] * 200)
    at_upper = core.position_values(highs, lows, tuple([D("80.02")] * 200))
    at_lower = core.position_values(highs, lows, tuple([D("20.08")] * 200))
    assert at_upper["range_position"] == D("0.8") and at_upper["state"] == "UPPER"
    assert at_lower["range_position"] == D("0.2") and at_lower["state"] == "LOWER"


def test_sds_mutation_kills_for_formula_operators_and_indexes() -> None:
    fixture = load_fixture("textbook_vectors.json")["rsi_wilder"]
    closes = tuple(map(D, fixture["closes"]))
    with decimal_context():
        differences = tuple(
            closes[index] - closes[index - 1] for index in range(1, len(closes))
        )
        gains = tuple(max(value, D(0)) for value in differences)
        losses = tuple(max(-value, D(0)) for value in differences)
        seed14_gain = sum(gains[:14], D(0)) / D(14)
        seed13_gain = sum(gains[:13], D(0)) / D(13)
    assert differences[13] != 0 and seed14_gain != seed13_gain
    mutant_rsi_seed13 = independent_rsi(closes, seed_period=13)
    mutant_rsi_rolling = independent_rsi(closes, rolling_sma=True)
    actual_rsi = core.rsi14(closes)
    assert mutant_rsi_seed13.quantize(D("0.000001")) == D(fixture["seed13_mutant_rsi6"])
    assert mutant_rsi_rolling.quantize(D("0.000001")) == D(
        fixture["rolling_sma_mutant_rsi6"]
    )
    assert mutant_rsi_seed13 != actual_rsi and mutant_rsi_rolling != actual_rsi
    old_vacuous = tuple(
        map(
            D,
            [
                "44.34",
                "44.09",
                "44.15",
                "43.61",
                "44.33",
                "44.83",
                "45.10",
                "45.42",
                "45.84",
                "46.08",
                "45.89",
                "46.03",
                "45.61",
                "46.28",
                "46.28",
            ],
        )
    )
    assert independent_rsi(old_vacuous, seed_period=13) == independent_rsi(old_vacuous)
    nonlinear = tuple(D(index * index) for index in range(1, 36))
    first_close_seed = nonlinear[0]
    alpha12 = D(2) / 13
    for value in nonlinear[1:12]:
        first_close_seed = alpha12 * value + (1 - alpha12) * first_close_seed
    assert first_close_seed != core.ema_series(nonlinear, 12)[11]
    assert (
        max(D("1"), abs(D("15") - D("14.5")), abs(D("14") - D("14.5")))
        != core.true_ranges(
            (D("10"), D("15")), (D("9"), D("14")), (D("10"), D("14.5"))
        )[0]
    )
    sample = (sum((x - D("2")) ** 2 for x in (D("1"), D("2"), D("3"))) / 2).sqrt()
    assert sample != core.population_std((D("1"), D("2"), D("3")))
    assert D(365).sqrt() != D(252).sqrt()
    assert (
        core.classify_volatility(D("3"), D("6"), D("4"), D("8")) == "COMPRESSED"
    )  # kills <= to <
    ratio_only = core.volume_values(tuple([D("1")] * 18 + [D("100"), D("10")]))
    assert (
        ratio_only["ratio"] >= D("1.5") and ratio_only["state"] == "NORMAL"
    )  # kills AND to OR
    assert max((D("100"), D("50"))) != max(
        (D("50"), D("50"))
    )  # kills high extrema to close
    assert (
        core.position_values(
            tuple([D("2")] * 199), tuple([D("1")] * 199), tuple([D("1.5")] * 199)
        )["state"]
        == "PARTIAL"
    )
    rounded = D("1.0049").quantize(D("0.01")) + D("1.0049").quantize(D("0.01"))
    assert rounded != (D("1.0049") + D("1.0049")).quantize(
        D("0.01")
    )  # kills early quantization
