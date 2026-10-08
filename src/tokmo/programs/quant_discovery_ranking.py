"""Pure peer standing for the Quant discovery engine."""

from decimal import Decimal, ROUND_HALF_UP

from tokmo.values.quant_discovery import FactorStandingV1, FactorOrientation


def compute_peer_standing(rows) -> tuple[FactorStandingV1, ...]:
    if not rows:
        return ()
    factor_names = {row[0] for row in rows}
    orientations = {row[3] for row in rows}
    approved_flags = {row[4] for row in rows}
    symbols = [row[1] for row in rows]
    if len(factor_names) != 1 or len(orientations) != 1 or len(approved_flags) != 1 or len(symbols) != len(set(symbols)):
        raise ValueError("standing rows must form one unique peer cohort")
    orientation = next(iter(orientations))
    reverse = orientation is FactorOrientation.HIGHER_IS_BETTER
    ordered = sorted(rows, key=lambda row: ((-row[2] if reverse else row[2]), row[1]))
    n = len(ordered)
    result = []
    index = 0
    while index < n:
        end = index
        while end + 1 < n and ordered[end + 1][2] == ordered[index][2]:
            end += 1
        ordinal = (Decimal(index + 1) + Decimal(end + 1)) / 2
        for row in ordered[index:end + 1]:
            percentile = None
            label = None
            if n >= 10 and row[4]:
                percentile = (Decimal(100) * (Decimal(n) - ordinal) / Decimal(n - 1)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
                label = f"검수 peer {n}개 중 백분위 {percentile:.1f}"
            result.append(FactorStandingV1(row[0], row[1], ordinal, n, row[2], orientation, percentile, label))
        index = end + 1
    return tuple(result)
