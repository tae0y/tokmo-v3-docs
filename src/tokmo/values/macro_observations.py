"""Normalized macro observation types (SDS §4).

Pure value objects for candle observations, investor observations,
source references, coverage, and the observation-series bundle.
All datetimes are caller-supplied; no I/O, no wall-clock.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum

__all__ = [
    "MacroCandleObservation",
    "MacroInvestorObservation",
    "MacroSourceRefV1",
    "MacroCoverageState",
    "MacroDataCoverageV1",
    "MacroObservationSeriesV1",
    "compute_series_digest",
]


# Validation patterns
_OPERATION_SYMBOL: dict[str, str] = {
    "INDICATOR_CANDLES": "KOSPI",
    "INDICATOR_INVESTOR_TRADING": "KOSPI",
    "STOCK_CANDLES": "261240",
}
_VALID_OPERATIONS = frozenset(_OPERATION_SYMBOL)
_DIGEST_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


def _require_aware(dt: datetime, field_name: str) -> None:
    if not isinstance(dt, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _require_finite_decimal_text(value, field_name: str) -> None:
    if not isinstance(value, str) or isinstance(value, bool) or not value:
        raise ValueError(f"{field_name} must be non-empty decimal text")
    try:
        d = Decimal(value)
    except (InvalidOperation, ValueError):
        raise ValueError(f"{field_name} must be finite decimal text")
    if not d.is_finite():
        raise ValueError(f"{field_name} must be finite decimal text")


# ==========================================================================
# §4.1  MacroCandleObservation
# ==========================================================================

@dataclass(frozen=True)
class MacroCandleObservation:
    session_date: date
    observed_at: datetime
    available_at: datetime
    open_price: str = field(repr=False)
    high_price: str = field(repr=False)
    low_price: str = field(repr=False)
    close_price: str = field(repr=False)
    volume: str = field(repr=False)
    currency: str

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise ValueError("session_date must be date, not datetime")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise ValueError("available_at must be >= observed_at")
        _require_finite_decimal_text(self.open_price, "open_price")
        _require_finite_decimal_text(self.high_price, "high_price")
        _require_finite_decimal_text(self.low_price, "low_price")
        _require_finite_decimal_text(self.close_price, "close_price")
        _require_finite_decimal_text(self.volume, "volume")
        if not isinstance(self.currency, str) or not self.currency:
            raise ValueError("currency must be non-empty str")
        if len(self.currency) > 8:
            raise ValueError("currency must be <= 8 chars")


# ==========================================================================
# §4.2  MacroInvestorObservation
# ==========================================================================

@dataclass(frozen=True)
class MacroInvestorObservation:
    session_date: date
    observed_at: datetime
    available_at: datetime
    foreign_buy: str = field(repr=False)
    foreign_sell: str = field(repr=False)
    individual_buy: str = field(repr=False)
    individual_sell: str = field(repr=False)
    institution_buy: str = field(repr=False)
    institution_sell: str = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.session_date, date) or isinstance(
            self.session_date, datetime
        ):
            raise ValueError("session_date must be date, not datetime")
        _require_aware(self.observed_at, "observed_at")
        _require_aware(self.available_at, "available_at")
        if self.available_at < self.observed_at:
            raise ValueError("available_at must be >= observed_at")
        for fname in (
            "foreign_buy", "foreign_sell",
            "individual_buy", "individual_sell",
            "institution_buy", "institution_sell",
        ):
            _require_finite_decimal_text(getattr(self, fname), fname)


# ==========================================================================
# §4.3  MacroSourceRefV1
# ==========================================================================

@dataclass(frozen=True)
class MacroSourceRefV1:
    operation: str
    symbol: str
    interval: str
    requested_count: int
    returned_count: int
    date_range_start: date | None
    date_range_end: date | None
    series_digest: str = field(repr=False)

    def __post_init__(self) -> None:
        if self.operation not in _VALID_OPERATIONS:
            raise ValueError(f"operation must be one of {sorted(_VALID_OPERATIONS)}")
        if self.symbol != _OPERATION_SYMBOL[self.operation]:
            raise ValueError(
                f"symbol {self.symbol!r} does not match operation {self.operation!r}"
            )
        if self.interval != "1d":
            raise ValueError("interval must be '1d'")
        for fname in ("requested_count", "returned_count"):
            val = getattr(self, fname)
            if not isinstance(val, int) or isinstance(val, bool) or val < 0:
                raise ValueError(f"{fname} must be int >= 0")
        if self.date_range_start is not None and not isinstance(
            self.date_range_start, date
        ):
            raise ValueError("date_range_start must be date or None")
        if self.date_range_end is not None and not isinstance(
            self.date_range_end, date
        ):
            raise ValueError("date_range_end must be date or None")
        if not _DIGEST_RE.match(self.series_digest):
            raise ValueError("series_digest must be 64-char lowercase hex")


# ==========================================================================
# §4.3.1  Canonical series serialization → SHA-256
# ==========================================================================

# Exact closed field sets per observation type (§4.3.1 rule 2).
# available_at is EXCLUDED (rule 3).

_CANDLE_DIGEST_FIELDS = (
    "session_date",
    "observed_at",
    "open_price",
    "high_price",
    "low_price",
    "close_price",
    "volume",
    "currency",
)

_CANDLE_DECIMAL_FIELDS = frozenset({
    "open_price", "high_price", "low_price", "close_price", "volume",
})

_INVESTOR_DIGEST_FIELDS = (
    "session_date",
    "observed_at",
    "foreign_buy",
    "foreign_sell",
    "individual_buy",
    "individual_sell",
    "institution_buy",
    "institution_sell",
)

_INVESTOR_DECIMAL_FIELDS = frozenset({
    "foreign_buy", "foreign_sell",
    "individual_buy", "individual_sell",
    "institution_buy", "institution_sell",
})


def _canonical_utc_iso(dt: datetime) -> str:
    """Timezone-aware datetime → UTC RFC3339 with Z, real microseconds.

    Per §4.3.1 rule 4: truncate to microseconds, always emit exactly 6
    fractional digits.
    """
    from datetime import timezone as _tz

    utc_dt = dt.astimezone(_tz.utc)
    return f"{utc_dt:%Y-%m-%dT%H:%M:%S}.{utc_dt.microsecond:06d}Z"


def _canonical_decimal_str(value: str) -> str:
    """Apply canonical_decimal semantics for digest consistency."""
    from tokmo.values import canonical_decimal

    return canonical_decimal(Decimal(value))


def _obs_to_digest_dict(obs) -> dict:
    if isinstance(obs, MacroCandleObservation):
        fields = _CANDLE_DIGEST_FIELDS
    elif isinstance(obs, MacroInvestorObservation):
        fields = _INVESTOR_DIGEST_FIELDS
    else:
        raise TypeError(f"unsupported observation type: {type(obs)}")

    result: dict[str, object] = {}
    for fname in fields:
        val = getattr(obs, fname)
        if isinstance(val, datetime):
            result[fname] = _canonical_utc_iso(val)
        elif isinstance(val, date):
            result[fname] = val.isoformat()
        elif fname in _CANDLE_DECIMAL_FIELDS or fname in _INVESTOR_DECIMAL_FIELDS:
            result[fname] = _canonical_decimal_str(val)
        else:
            result[fname] = val
    return result


def compute_series_digest(observations: tuple) -> str:
    """SHA-256 over canonical JSON serialization of the observation series.

    Per §4.3.1:
    - Sort ascending by session_date (input order independent).
    - Closed field set; available_at excluded.
    - Datetimes → UTC RFC3339 with Z and 6 fractional digits.
    - Decimals → canonical_decimal string form.
    - JSON: sort_keys, compact separators, ensure_ascii=False, allow_nan=False.
    """
    entries = [_obs_to_digest_dict(obs) for obs in observations]
    entries.sort(key=lambda d: d["session_date"])  # type: ignore[arg-type]
    canonical = json.dumps(
        entries,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ==========================================================================
# §4.4  Coverage state and data
# ==========================================================================

class MacroCoverageState(Enum):
    COVERED = "COVERED"
    PARTIAL = "PARTIAL"
    STALE = "STALE"
    EMPTY = "EMPTY"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True)
class MacroDataCoverageV1:
    state: MacroCoverageState
    kospi_count: int
    proxy_count: int
    investor_count: int
    common_window_count: int
    common_window_start: date | None
    common_window_end: date | None
    latest_common_session: date | None
    reason: str


# ==========================================================================
# §4.5  MacroObservationSeriesV1 — the normalized input bundle
# ==========================================================================

@dataclass(frozen=True)
class MacroObservationSeriesV1:
    kospi_candles: tuple[MacroCandleObservation, ...] = field(repr=False)
    proxy_candles: tuple[MacroCandleObservation, ...] = field(repr=False)
    investor_records: tuple[MacroInvestorObservation, ...] = field(repr=False)
    as_of: datetime
    source_refs: tuple[MacroSourceRefV1, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.kospi_candles, tuple):
            raise ValueError("kospi_candles must be a tuple")
        if not isinstance(self.proxy_candles, tuple):
            raise ValueError("proxy_candles must be a tuple")
        if not isinstance(self.investor_records, tuple):
            raise ValueError("investor_records must be a tuple")
        _require_aware(self.as_of, "as_of")
        if not isinstance(self.source_refs, tuple) or len(self.source_refs) != 3:
            raise ValueError("source_refs must have exactly 3 entries")
