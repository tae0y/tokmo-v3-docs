"""Primitive parsing helpers and MISSING sentinel for Toss read values."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "MISSING",
    "Missing",
    "Opt",
    "RawStr",
    "_AwareDateTime",
    "_Decimal30",
    "_DecimalNoMax",
    "_MissingType",
    "_RawEnum",
    "_StrictDate",
]


# =========================================================================
# MISSING sentinel and three-state optional
# =========================================================================


class _MissingType:
    """Singleton sentinel for an optional key absent from the JSON payload.

    Distinct from ``None``, which represents an explicit JSON ``null``.
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self):
        return "MISSING"

    def __bool__(self):
        return False


MISSING = _MissingType()
# Alias for type-hint readability.
Missing = Any


class Opt:
    """Three-state optional extraction from a JSON dict.

    - key absent → ``MISSING``
    - key present, value ``None`` → ``None``
    - key present, value V → ``V``
    """

    @staticmethod
    def from_json(obj: dict, key: str):
        if key not in obj:
            return MISSING
        return obj[key]


# =========================================================================
# Primitive parsing helpers
# =========================================================================


class RawStr:
    """Raw non-empty string — used for raw response enums."""

    @staticmethod
    def parse(value) -> str | None:
        if not isinstance(value, str) or not value:
            return None
        return value


class _Decimal30:
    """Decimal string with ``maxLength: 30`` per OpenAPI."""

    @staticmethod
    def parse(value) -> str | None:
        if not isinstance(value, str) or isinstance(value, bool):
            return None
        if not 1 <= len(value) <= 30:
            return None
        try:
            d = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        if not d.is_finite():
            return None
        return value


class _DecimalNoMax:
    """Decimal string with no documented max length.

    Applies only the global bounded-string safety limit.
    """

    MAX_LENGTH = 4096

    @staticmethod
    def parse(value) -> str | None:
        if not isinstance(value, str) or isinstance(value, bool):
            return None
        if not 1 <= len(value) <= _DecimalNoMax.MAX_LENGTH:
            return None
        try:
            d = Decimal(value)
        except (InvalidOperation, ValueError):
            return None
        if not d.is_finite():
            return None
        return value


class _AwareDateTime:
    """Timezone-aware ``datetime``."""

    @staticmethod
    def parse(value) -> datetime | None:
        if not isinstance(value, str):
            return None
        try:
            dt = datetime.fromisoformat(value)
        except (ValueError, TypeError):
            return None
        if dt.tzinfo is None or dt.utcoffset() is None:
            return None
        return dt


class _StrictDate:
    """Strict ISO ``YYYY-MM-DD`` date."""

    @staticmethod
    def parse(value) -> date | None:
        if not isinstance(value, str):
            return None
        try:
            return date.fromisoformat(value)
        except (ValueError, TypeError):
            return None


class _RawEnum:
    """Raw non-empty string enum — unknown values are preserved."""

    @staticmethod
    def parse(value) -> str | None:
        return RawStr.parse(value)
