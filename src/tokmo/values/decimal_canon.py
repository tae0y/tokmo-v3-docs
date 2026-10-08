"""Canonical decimal spelling for bounded Toss wire decimals."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Decimal30CanonicalizationViolation:
    reason: str


def canonicalize_decimal30(value: str) -> str | Decimal30CanonicalizationViolation:
    if type(value) is not str or not 1 <= len(value) <= 30:
        return Decimal30CanonicalizationViolation("invalid_decimal30")
    try:
        number = Decimal(value)
    except InvalidOperation:
        return Decimal30CanonicalizationViolation("invalid_decimal30")
    if not number.is_finite():
        return Decimal30CanonicalizationViolation("invalid_decimal30")
    if number.is_zero():
        return "0"
    result = format(number, "f")
    if "." in result:
        result = result.rstrip("0").rstrip(".")
    if result.startswith("-0") and Decimal(result).is_zero():
        return "0"
    if result.startswith("0") and len(result) > 1 and result[1] != ".":
        result = result.lstrip("0") or "0"
    if result.startswith("-0") and len(result) > 2 and result[2] != ".":
        result = "-" + (result[1:].lstrip("0") or "0")
    return result
