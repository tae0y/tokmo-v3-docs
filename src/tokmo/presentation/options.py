"""Format options, enums, and presets for the presentation layer.

This module defines the closed set of presentation choices available
to callers who need a string representation of a typed value.  All
enums use explicit values so typos are caught at the type level, not
silently accepted at runtime.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

__all__ = [
    "NumberStyle",
    "CurrencyStyle",
    "DateStyle",
    "DateTimeStyle",
    "Locale",
    "FormatOptions",
    "RAW",
    "NUMBER",
    "KRW",
    "USD",
    "KOREAN",
]


class NumberStyle(Enum):
    RAW = "raw"
    GROUPED = "grouped"


class CurrencyStyle(Enum):
    NONE = "none"
    KRW = "krw"
    USD = "usd"


class DateStyle(Enum):
    ISO = "iso"
    COMPACT = "compact"
    KOREAN = "korean"


class DateTimeStyle(Enum):
    ISO = "iso"
    COMPACT = "compact"
    KOREAN = "korean"


class Locale(Enum):
    KO = "ko"
    EN = "en"


@dataclass(frozen=True)
class FormatOptions:
    """Immutable set of presentation choices.

    Fields default to the general NUMBER preset, not to a domain-specific
    one.  Domain formatters supply their own default via the registry when
    the caller passes ``options=None``.
    """

    number_style: NumberStyle = NumberStyle.GROUPED
    max_decimal_places: int | None = 2
    min_decimal_places: int = 0
    currency_style: CurrencyStyle = CurrencyStyle.NONE
    include_unit: bool = True
    date_style: DateStyle = DateStyle.ISO
    datetime_style: DateTimeStyle = DateTimeStyle.ISO
    timezone: str | None = None
    locale: Locale = Locale.KO
    null_text: str | None = None

    def __post_init__(self) -> None:
        # max_decimal_places: None or 0..12
        if self.max_decimal_places is not None:
            if isinstance(self.max_decimal_places, bool):
                raise ValueError("max_decimal_places_must_not_be_bool")
            if not isinstance(self.max_decimal_places, int):
                raise ValueError("max_decimal_places_must_be_int_or_none")
            if not 0 <= self.max_decimal_places <= 12:
                raise ValueError("max_decimal_places_out_of_range")

        # min_decimal_places: 0..12
        if isinstance(self.min_decimal_places, bool):
            raise ValueError("min_decimal_places_must_not_be_bool")
        if not isinstance(self.min_decimal_places, int):
            raise ValueError("min_decimal_places_must_be_int")
        if not 0 <= self.min_decimal_places <= 12:
            raise ValueError("min_decimal_places_out_of_range")

        # min <= max (only when max is not None)
        if self.max_decimal_places is not None:
            if self.min_decimal_places > self.max_decimal_places:
                raise ValueError("min_decimal_places_exceeds_max")

        # timezone validation
        if self.timezone is not None:
            ZoneInfo(self.timezone)  # raises if invalid


# === Presets ===

RAW = FormatOptions(
    number_style=NumberStyle.RAW,
    max_decimal_places=None,
    min_decimal_places=0,
    currency_style=CurrencyStyle.NONE,
    include_unit=False,
)

NUMBER = FormatOptions(
    number_style=NumberStyle.GROUPED,
    max_decimal_places=2,
    min_decimal_places=0,
    currency_style=CurrencyStyle.NONE,
    include_unit=False,
)

KRW = FormatOptions(
    number_style=NumberStyle.GROUPED,
    max_decimal_places=0,
    min_decimal_places=0,
    currency_style=CurrencyStyle.KRW,
)

USD = FormatOptions(
    number_style=NumberStyle.GROUPED,
    max_decimal_places=2,
    min_decimal_places=2,
    currency_style=CurrencyStyle.USD,
)

KOREAN = FormatOptions(
    date_style=DateStyle.KOREAN,
    datetime_style=DateTimeStyle.KOREAN,
    locale=Locale.KO,
)
