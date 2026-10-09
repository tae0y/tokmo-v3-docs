from decimal import Decimal

from tokmo.programs.quant_multifactor_core import rank_metric


def test_v08_v09_type7_clamp_average_ties_and_n_minus_one() -> None:
    ranked = rank_metric(
        (Decimal(0), Decimal(1), Decimal(1), Decimal(2), Decimal(100)), False
    )
    assert ranked == (
        (Decimal("0.20"), Decimal(1), Decimal(0)),
        (Decimal(1), Decimal("2.5"), Decimal("0.375")),
        (Decimal(1), Decimal("2.5"), Decimal("0.375")),
        (Decimal(2), Decimal(4), Decimal("0.75")),
        (Decimal("80.40"), Decimal(5), Decimal(1)),
    )


def test_required_numeric_counterfactual_mutants_are_killed() -> None:
    values = (Decimal(0), Decimal(1), Decimal(1), Decimal(2), Decimal(100))
    actual = rank_metric(values, False)
    assert actual[1][1] != Decimal(2)  # ordinal ties
    assert actual[1][1] != Decimal(1)  # dense ties
    assert actual[1][2] != (actual[1][1] - 1) / Decimal(5)  # n denominator
    assert actual[0][0] != Decimal(0)  # trim/no clamp
    inverse = rank_metric(
        (Decimal(1), Decimal(2), Decimal(3), Decimal(4), Decimal(5)), True
    )
    assert inverse[0][2] > inverse[-1][2]  # RV inverse, not normal
    mdd = rank_metric(
        (
            Decimal("-0.5"),
            Decimal("-0.4"),
            Decimal("-0.3"),
            Decimal("-0.2"),
            Decimal("-0.1"),
        ),
        False,
    )
    assert mdd[-1][2] > mdd[0][2]  # MDD normal, not inverse
