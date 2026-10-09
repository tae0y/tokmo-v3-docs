from decimal import Decimal, localcontext

from tokmo.programs.quant_multifactor_core import aggregate_composite


def test_v10_min3_fund2_and_available_weight_denominator() -> None:
    scores = (Decimal("0.2"), Decimal("0.4"), None, Decimal("0.8"), None)
    composite, contributions = aggregate_composite(scores)
    assert composite == Decimal("0.46666666666666666666666666666666666666666666666667")
    with localcontext() as context:
        context.prec = 50
        assert (
            sum((item for item in contributions if item is not None), Decimal(0))
            == composite
        )
    assert aggregate_composite((Decimal(1), None, None, Decimal(1), Decimal(1))) == (
        None,
        (None, None, None, None, None),
    )


def test_composite_denominator_threshold_and_contribution_mutants_are_killed() -> None:
    scores = (Decimal("0.2"), Decimal("0.4"), None, Decimal("0.8"), None)
    composite, contributions = aggregate_composite(scores)
    assert composite != Decimal("0.28")  # full-weight denominator mutant
    assert aggregate_composite((Decimal(1), None, None, Decimal(1), None))[0] is None
    assert (
        aggregate_composite((Decimal(1), None, None, Decimal(1), Decimal(1)))[0] is None
    )
    with localcontext() as context:
        context.prec = 50
        assert (
            sum((item for item in contributions if item is not None), Decimal(0))
            == composite
        )
