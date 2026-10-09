"""Scalar renderers: numbers, money, percentages, dates, and datetimes.

These functions handle the low-level string representation of individual
primitive values.  They never touch network, filesystem, environment,
or clock.  Float is rejected — the project's financial representation
requires exact decimal arithmetic.

Key contracts
-------------

- ``RAW`` + ``max_decimal_places=None``: ``str(value)`` as-is.
- ``GROUPED`` + ``max_decimal_places=None``: expand scientific notation,
  preserve original decimal scale, add thousands separators.
- ``GROUPED`` + ``max_decimal_places=N``: round to N decimal places using
  ``ROUND_HALF_UP`` when the value exceeds N, pad to ``min_decimal_places``
  with trailing zeros.
"""

from __future__ import annotations

from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from zoneinfo import ZoneInfo

from tokmo.presentation.options import (
    CurrencyStyle,
    DateStyle,
    DateTimeStyle,
    FormatOptions,
    Locale,
    NumberStyle,
)

__all__ = [
    "format_date",
    "format_datetime",
    "format_money",
    "format_number",
    "format_percent",
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _validate_numeric(value: Decimal | int) -> None:
    """Reject float, bool, str, and other non-exact-numeric types."""
    if isinstance(value, bool):
        raise TypeError("bool_is_not_numeric")
    if isinstance(value, float):
        raise TypeError("float_not_allowed_use_decimal")
    if not isinstance(value, (Decimal, int)):
        raise TypeError(f"expected_decimal_or_int_got_{type(value).__name__}")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non_finite_decimal_not_allowed")


def _to_decimal(value: Decimal | int) -> Decimal:
    """Convert to Decimal without float contamination."""
    if isinstance(value, int):
        return Decimal(value)
    return value  # already Decimal


def _null_text(options: FormatOptions) -> str:
    if options.null_text is not None:
        return options.null_text
    return "없음" if options.locale is Locale.KO else "N/A"


# ---------------------------------------------------------------------------
# Number rendering
# ---------------------------------------------------------------------------


def _format_raw_number(value: Decimal, options: FormatOptions) -> str:
    """RAW mode: str(value) as-is. If max_decimal_places is set, round first."""
    if options.max_decimal_places is not None:
        q = Decimal(1).scaleb(-options.max_decimal_places)
        rounded = value.quantize(q, rounding=ROUND_HALF_UP)
        s = str(rounded)
    else:
        s = str(value)
    return s


def _format_grouped_number(value: Decimal, options: FormatOptions) -> str:
    """GROUPED mode: thousands separators, optional rounding and padding."""
    # Determine the working decimal
    if options.max_decimal_places is not None:
        q = Decimal(1).scaleb(-options.max_decimal_places)
        working = value.quantize(q, rounding=ROUND_HALF_UP)
    else:
        # No max: expand scientific notation, keep original scale
        working = value

    # Produce plain string (no exponent)
    plain = _decimal_to_plain_str(working)

    if options.max_decimal_places is not None:
        # quantize pads to max; strip excess trailing zeros down to min
        plain = _strip_and_pad_decimals(plain, options.min_decimal_places)
    else:
        # No max: preserve original scale; only pad to min if needed
        plain = _pad_to_min(plain, options.min_decimal_places)

    # Insert thousands separators in the integer part
    return _insert_thousands_separators(plain)


def _decimal_to_plain_str(d: Decimal) -> str:
    """Convert Decimal to a plain string without exponent notation."""
    # format(d, 'f') expands exponents while preserving the Decimal scale,
    # including signed zero such as Decimal("-0.00").
    return format(d, "f")


def _strip_and_pad_decimals(plain: str, min_places: int) -> str:
    """Strip trailing zeros but never below min_places; pad to min_places."""
    if "." in plain:
        int_part, frac_part = plain.split(".", 1)
    else:
        int_part, frac_part = plain, ""

    # Strip excess trailing zeros, but keep at least min_places
    while len(frac_part) > min_places and frac_part.endswith("0"):
        frac_part = frac_part[:-1]

    # Pad to min_places
    if len(frac_part) < min_places:
        frac_part = frac_part.ljust(min_places, "0")

    if frac_part:
        return f"{int_part}.{frac_part}"
    return int_part


def _pad_to_min(plain: str, min_places: int) -> str:
    """Pad fraction to min_places, preserving original trailing zeros."""
    if min_places == 0:
        return plain
    if "." in plain:
        int_part, frac_part = plain.split(".", 1)
    else:
        int_part, frac_part = plain, ""
    if len(frac_part) < min_places:
        frac_part = frac_part.ljust(min_places, "0")
    if frac_part:
        return f"{int_part}.{frac_part}"
    return int_part


def _insert_thousands_separators(plain: str) -> str:
    """Add commas to the integer part of a plain numeric string."""
    negative = plain.startswith("-")
    if negative:
        plain = plain[1:]
    if "." in plain:
        int_part, frac_part = plain.split(".", 1)
    else:
        int_part, frac_part = plain, None

    # Insert commas from right
    result = []
    for i, ch in enumerate(reversed(int_part)):
        if i > 0 and i % 3 == 0:
            result.append(",")
        result.append(ch)
    int_part = "".join(reversed(result))

    if frac_part is not None:
        result_str = f"{int_part}.{frac_part}"
    else:
        result_str = int_part
    return ("-" if negative else "") + result_str


def format_number(value: Decimal | int, *, options: FormatOptions) -> str:
    """Render a number according to the given options."""
    _validate_numeric(value)
    d = _to_decimal(value)

    if options.number_style is NumberStyle.RAW:
        return _format_raw_number(d, options)
    return _format_grouped_number(d, options)


# ---------------------------------------------------------------------------
# Money rendering
# ---------------------------------------------------------------------------


def format_money(value: Decimal | int, *, options: FormatOptions) -> str:
    """Render a number with an optional currency symbol."""
    num_str = format_number(value, options=options)
    if options.currency_style is CurrencyStyle.KRW:
        return f"{num_str}원"
    if options.currency_style is CurrencyStyle.USD:
        return f"${num_str}" if not num_str.startswith("-") else f"-${num_str[1:]}"
    # NONE: return as-is
    return num_str


# ---------------------------------------------------------------------------
# Percent rendering
# ---------------------------------------------------------------------------


def format_percent(
    value: Decimal | int | None,
    *,
    options: FormatOptions,
    suffix: str = "%",
) -> str:
    """Render a value with a percent-like suffix."""
    if value is None:
        return _null_text(options)
    num_str = format_number(value, options=options)
    return f"{num_str}{suffix}"


# ---------------------------------------------------------------------------
# Date rendering
# ---------------------------------------------------------------------------


def format_date(value: date, *, options: FormatOptions) -> str:
    """Render a date in ISO, compact, or Korean style."""
    if options.date_style is DateStyle.ISO:
        return value.isoformat()
    if options.date_style is DateStyle.COMPACT:
        return value.strftime("%Y%m%d")
    # KOREAN
    return f"{value.year}년 {value.month}월 {value.day}일"


# ---------------------------------------------------------------------------
# Datetime rendering
# ---------------------------------------------------------------------------


def format_datetime(value: datetime, *, options: FormatOptions) -> str:
    """Render a datetime in ISO, compact, or Korean style."""
    working = value

    # Timezone conversion (only for aware datetimes)
    if options.timezone is not None:
        if working.tzinfo is None or working.utcoffset() is None:
            raise ValueError("naive_datetime_with_timezone_not_allowed")
        working = working.astimezone(ZoneInfo(options.timezone))

    if options.datetime_style is DateTimeStyle.ISO:
        return working.isoformat()
    if options.datetime_style is DateTimeStyle.COMPACT:
        return working.strftime("%Y%m%d%H%M%S")
    # KOREAN
    return f"{working.year}년 {working.month}월 {working.day}일 {working.hour}시 {working.minute}분"
