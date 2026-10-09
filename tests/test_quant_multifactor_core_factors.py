from decimal import Decimal

from tokmo.programs.quant_multifactor_core import (
    calculate_growth,
    calculate_max_drawdown,
    calculate_momentum,
    type7_percentile,
)


def test_v02_type7_uses_decimal_without_early_rounding() -> None:
    values = tuple(Decimal(item) for item in ("0", "1", "2", "100"))
    assert type7_percentile(values, Decimal("0.05")) == Decimal("0.15")
    assert type7_percentile(values, Decimal("0.95")) == Decimal("85.30")


def test_v05_growth_uses_signed_change_over_positive_prior() -> None:
    assert calculate_growth(Decimal(0), Decimal(80)) == (Decimal(-1), None)
    assert calculate_growth(Decimal(120), Decimal(100)) == (Decimal("0.2"), None)
    assert calculate_growth(Decimal(120), Decimal(0)) == (
        None,
        "UNAVAILABLE_NONPOSITIVE_PRIOR_REVENUE",
    )


def test_v06_momentum_excludes_most_recent_close() -> None:
    closes = tuple(Decimal(item) for item in range(100, 113))
    assert calculate_momentum(closes) == Decimal("0.11")
    assert calculate_momentum(closes[:-1]) is None


def test_v07_max_drawdown_keeps_negative_normal_orientation() -> None:
    assert calculate_max_drawdown((Decimal(100), Decimal(50), Decimal(90))) == Decimal(
        "-0.5"
    )
    assert calculate_max_drawdown((Decimal(100),)) is None
