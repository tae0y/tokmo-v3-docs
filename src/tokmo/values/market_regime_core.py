"""Immutable contracts and canonical encoding for market-regime CORE v1."""

from __future__ import annotations

import hashlib
import json
import re
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
from datetime import UTC, date, datetime
from decimal import (
    ROUND_HALF_EVEN,
    Decimal,
    DivisionByZero,
    InvalidOperation,
    Overflow,
    localcontext,
)
from enum import Enum
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

PROGRAM_ID = "market-regime-core-v1"
VERSION = "1.0.0"
KIND = "CORE_ANALYZER"
FORMULA_VERSION = "market-regime-formula-v1"
REQUIRED_SESSIONS = 200
PACK_BYTE_CAP = 65536


@contextmanager
def decimal_context():
    """Yield the formula context without changing the process context."""
    with localcontext() as context:
        context.prec = 50
        context.rounding = ROUND_HALF_EVEN
        context.traps[InvalidOperation] = True
        context.traps[DivisionByZero] = True
        context.traps[Overflow] = True
        yield context


class AdjustmentMode(str, Enum):
    ADJUSTED = "ADJUSTED"
    UNADJUSTED = "UNADJUSTED"


class Regime(str, Enum):
    UPTREND = "UPTREND"
    DOWNTREND = "DOWNTREND"
    RANGE = "RANGE"
    HIGH_VOLATILITY = "HIGH_VOLATILITY"
    TRANSITION = "TRANSITION"
    INSUFFICIENT = "INSUFFICIENT"


def canonical_decimal(value: Decimal) -> str:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError("Decimal must be finite")
    if value.is_zero():
        return "0"
    rendered = format(value, "f")
    if "." in rendered:
        rendered = rendered.rstrip("0").rstrip(".")
    return rendered


def _decimal_valid(value: Decimal, field_name: str, *, positive: bool) -> None:
    if not isinstance(value, Decimal) or not value.is_finite():
        raise ValueError(f"{field_name} must be finite Decimal")
    raw = value.as_tuple()
    significant = len(raw.digits)
    fractional = max(-raw.exponent, 0)
    if significant > 30 or fractional > 12:
        raise ValueError(f"{field_name} precision")
    if (positive and value <= 0) or (not positive and value < 0):
        raise ValueError(f"{field_name} out of range")


@dataclass(frozen=True)
class NormalizedDailyBarV1:
    session_date: date
    timestamp: datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise ValueError("session_date")
        if (
            not isinstance(self.timestamp, datetime)
            or self.timestamp.tzinfo is None
            or self.timestamp.utcoffset() is None
        ):
            raise ValueError("timestamp must be timezone-aware")
        for name in ("open", "high", "low", "close"):
            _decimal_valid(getattr(self, name), name, positive=True)
        _decimal_valid(self.volume, "volume", positive=False)
        if self.high < max(self.open, self.close, self.low):
            raise ValueError("high violates OHLC")
        if self.low > min(self.open, self.close, self.high):
            raise ValueError("low violates OHLC")


_SYMBOL = re.compile(r"[A-Z0-9._-]{1,32}\Z", re.ASCII)
_CURRENCY = re.compile(r"[A-Z]{3}\Z", re.ASCII)


@dataclass(frozen=True)
class NormalizedDailySeriesV1:
    symbol: str
    currency: str
    adjustment_mode: AdjustmentMode
    session_timezone: str
    as_of: date
    bars: tuple[NormalizedDailyBarV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.symbol, str) or _SYMBOL.fullmatch(self.symbol) is None:
            raise ValueError("symbol")
        if (
            not isinstance(self.currency, str)
            or _CURRENCY.fullmatch(self.currency) is None
        ):
            raise ValueError("currency")
        if not isinstance(self.adjustment_mode, AdjustmentMode):
            raise ValueError("adjustment_mode")
        if not isinstance(self.as_of, date) or isinstance(self.as_of, datetime):
            raise ValueError("as_of")
        if (
            not isinstance(self.bars, tuple)
            or not 1 <= len(self.bars) <= REQUIRED_SESSIONS
        ):
            raise ValueError("bars length must be 1..200")
        try:
            zone = ZoneInfo(self.session_timezone)
        except (ZoneInfoNotFoundError, TypeError):
            raise ValueError("session_timezone") from None
        previous: NormalizedDailyBarV1 | None = None
        dates: set[date] = set()
        stamps: set[datetime] = set()
        for index, bar in enumerate(self.bars):
            if not isinstance(bar, NormalizedDailyBarV1):
                raise ValueError(f"bar[{index}]")
            if bar.session_date in dates:
                raise ValueError(f"duplicate session_date at bar[{index}]")
            if bar.timestamp in stamps:
                raise ValueError(f"duplicate timestamp at bar[{index}]")
            if bar.timestamp.tzinfo != zone:
                raise ValueError(f"timestamp timezone at bar[{index}]")
            if bar.timestamp.astimezone(zone).date() != bar.session_date:
                raise ValueError(f"session_date mismatch at bar[{index}]")
            if bar.session_date > self.as_of:
                raise ValueError(f"session_date after as_of at bar[{index}]")
            if previous is not None and (bar.session_date, bar.timestamp) <= (
                previous.session_date,
                previous.timestamp,
            ):
                raise ValueError(f"bar order at bar[{index}]")
            dates.add(bar.session_date)
            stamps.add(bar.timestamp)
            previous = bar

    @property
    def input_digest(self) -> str:
        body = _to_primitive(self)
        return digest_object({"domain": "market-regime-input-v1", **body})


@dataclass(frozen=True)
class MetricV1:
    name: str
    value: Decimal | None
    unit: str
    scale: int
    rounding: str
    available: bool
    warmup_required: int


@dataclass(frozen=True)
class ComponentResultV1:
    name: str
    state: str
    available: bool
    warmup_required: int
    window_sessions: int
    metrics: tuple[MetricV1, ...]
    predicate_codes: tuple[str, ...] = ()
    counter_signals: tuple[str, ...] = ()


@dataclass(frozen=True)
class MarketRegimePackV1:
    program_id: str
    version: str
    kind: str
    formula_version: str
    spec_digest: str
    input_digest: str
    pack_digest: str
    as_of: date
    session_date: date
    symbol: str
    currency: str
    adjustment_mode: AdjustmentMode
    received_sessions: int
    required_sessions: int
    first_session_date: date
    last_session_date: date
    coverage: str
    precision_policy: str
    components: tuple[ComponentResultV1, ...]
    regime: Regime
    reasons: tuple[str, ...]
    counter_signals: tuple[str, ...]
    conflicts: tuple[str, ...]
    limitations: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            len(self.components) > 6
            or sum(len(item.metrics) for item in self.components) > 48
        ):
            raise ValueError("component or metric cap")
        for name, cap in (
            ("reasons", 32),
            ("counter_signals", 32),
            ("conflicts", 16),
            ("limitations", 16),
        ):
            values = getattr(self, name)
            if len(values) > cap:
                raise ValueError(f"{name} cap")
            for code in values:
                if not isinstance(code, str) or not code.isascii() or len(code) > 64:
                    raise ValueError(f"{name} code")
        if self.pack_digest:
            encoded = canonical_json_bytes(_to_primitive(self))
            if len(encoded) > PACK_BYTE_CAP:
                raise ValueError("pack byte cap")


def _timestamp(value: datetime) -> str:
    utc = value.astimezone(UTC)
    return f"{utc:%Y-%m-%dT%H:%M:%S}.{utc.microsecond:06d}Z"


def _to_primitive(value: Any, *, omit_pack_digest: bool = False) -> Any:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, Decimal):
        rendered = canonical_decimal(value)
        if len(rendered.encode("ascii")) > 64:
            raise ValueError("Decimal string cap")
        return rendered
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, datetime):
        return _timestamp(value)
    if isinstance(value, date):
        return value.isoformat()
    if is_dataclass(value) and not isinstance(value, type):
        return {
            field.name: _to_primitive(
                getattr(value, field.name), omit_pack_digest=omit_pack_digest
            )
            for field in fields(value)
            if not (omit_pack_digest and field.name == "pack_digest")
        }
    if isinstance(value, dict):
        return {
            key: _to_primitive(item, omit_pack_digest=omit_pack_digest)
            for key, item in value.items()
        }
    if isinstance(value, (tuple, list)):
        return [
            _to_primitive(item, omit_pack_digest=omit_pack_digest) for item in value
        ]
    raise TypeError(f"unsupported canonical type: {type(value).__name__}")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def digest_object(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(_to_primitive(value))).hexdigest()


def compute_pack_digest(pack: MarketRegimePackV1) -> str:
    return digest_object(
        {
            "domain": "market-regime-pack-v1",
            **_to_primitive(pack, omit_pack_digest=True),
        }
    )
