from tokmo.values.decimal_canon import (
    Decimal30CanonicalizationViolation,
    canonicalize_decimal30,
)


def test_decimal30_normalization_and_exponents():
    assert [canonicalize_decimal30(v) for v in
            ("185.50", "0.00", "-0", "007", "1e5", "1.5E-3")] == [
        "185.5", "0", "0", "7", "100000", "0.0015",
    ]


def test_decimal30_rejects_invalid_nonfinite_nonstring_and_bounds():
    for value in ("NaN", "Infinity", "", "1" * 31, 1):
        assert isinstance(
            canonicalize_decimal30(value), Decimal30CanonicalizationViolation
        )
