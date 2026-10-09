"""Calendar lookahead writer seam: Toss response → LookaheadDayV1 → store.

Plan §13.3: the 300s calendar owner preserves the provider response's
``today`` and ``next_business_day`` in CalendarLookaheadStoreV1. This
module converts ``KrMarketDay``/``UsMarketDay`` into ``LookaheadDayV1``
days (copying absolute UTC intervals verbatim), verifies against the
current cache, and atomically commits a 1–2 day window.

The existing ``CalendarCacheSchema2`` (one day per market) stays untouched.
No migration is needed because CalendarLookaheadStoreV1 is a separate
private store with its own schema.
"""

from datetime import UTC, date, datetime
from typing import TYPE_CHECKING

from tokmo.records.calendar_lookahead import (
    CalendarLookaheadConflictError,
    LookaheadDayV1,
    LookaheadSessionV1,
    verify_against_current_cache,
)
from tokmo.values._primitives import MISSING
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    KrMarketDay,
    UsMarketDay,
)

if TYPE_CHECKING:
    from tokmo.records.calendar_cache import CalendarDayCacheSchema2
    from tokmo.records.calendar_lookahead import CalendarLookaheadStoreV1


class LookaheadWriterError(ValueError):
    """Provider response is invalid or inconsistent — fail-closed."""


_SESSION_ATTRIBUTE_MAP_KR = {
    "preMarket": "pre_market",
    "regularMarket": "regular_market",
    "afterMarket": "after_market",
}

_SESSION_ATTRIBUTE_MAP_US = {
    "dayMarket": "day_market",
    "preMarket": "pre_market",
    "regularMarket": "regular_market",
    "afterMarket": "after_market",
}


def _to_utc(value: datetime) -> datetime:
    """Ensure aware UTC."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise LookaheadWriterError("calendar session time must be aware")
    return value.astimezone(UTC)


def _safe_date_iso(day, label: str) -> str:
    """Extract and validate the date attribute before calling isoformat.

    Raises ``LookaheadWriterError`` (not ``AttributeError``) for a
    malformed day object that lacks a valid ``date`` — fail-closed
    with the correct typed exception.
    """
    raw_date = getattr(day, "date", MISSING)
    if raw_date is MISSING or type(raw_date) is not date:
        raise LookaheadWriterError(f"{label} market day missing valid date")
    return raw_date.isoformat()


def _session_from_calendar(kind: str, session: CalendarSession) -> LookaheadSessionV1:
    if not isinstance(session, CalendarSession):
        raise LookaheadWriterError(f"missing {kind} session")
    return LookaheadSessionV1(
        kind, _to_utc(session.start_time), _to_utc(session.end_time)
    )


def _day_from_us(
    day: UsMarketDay,
    trade_date: str,
    source_trade_date: str,
    provenance: str,
    fetched_at: datetime,
) -> LookaheadDayV1:
    if not isinstance(day, UsMarketDay):
        raise LookaheadWriterError("invalid US market day")
    if day.date is MISSING or type(day.date) is not date:
        raise LookaheadWriterError("US market day missing date")
    sessions = []
    for kind in ("dayMarket", "preMarket", "regularMarket", "afterMarket"):
        attr = _SESSION_ATTRIBUTE_MAP_US[kind]
        raw = getattr(day, attr, MISSING)
        if raw is None or raw is MISSING:
            continue
        sessions.append(_session_from_calendar(kind, raw))  # type: ignore[arg-type]
    if not sessions:
        raise LookaheadWriterError("US market day has no sessions")
    return LookaheadDayV1(
        Market.US,
        trade_date,
        source_trade_date,
        tuple(sessions),
        fetched_at,
        provenance,
    )


def _day_from_kr(
    day: KrMarketDay,
    trade_date: str,
    source_trade_date: str,
    provenance: str,
    fetched_at: datetime,
) -> LookaheadDayV1:
    if not isinstance(day, KrMarketDay):
        raise LookaheadWriterError("invalid KR market day")
    if day.date is MISSING or type(day.date) is not date:
        raise LookaheadWriterError("KR market day missing date")
    if day.integrated is None or day.integrated is MISSING:
        raise LookaheadWriterError("KR market day has no integrated sessions")
    integrated = day.integrated
    sessions = []
    for kind in ("preMarket", "regularMarket", "afterMarket"):
        attr = _SESSION_ATTRIBUTE_MAP_KR[kind]
        raw = getattr(integrated, attr, MISSING)
        if raw is None or raw is MISSING:
            continue
        sessions.append(_session_from_calendar(kind, raw))  # type: ignore[arg-type]
    if not sessions:
        raise LookaheadWriterError("KR market day has no sessions")
    return LookaheadDayV1(
        Market.KR,
        trade_date,
        source_trade_date,
        tuple(sessions),
        fetched_at,
        provenance,
    )


def write_lookahead_from_calendar(
    *,
    market: Market,
    response_today,
    response_next,
    receipt_identity: str,
    store: "CalendarLookaheadStoreV1",
    current_cache_entry: "CalendarDayCacheSchema2 | None",
    fetched_at: datetime,
) -> None:
    """Convert Toss calendar response days and commit a 1–2 day window.

    ``response_today`` is the provider's ``today`` day object
    (``UsMarketDay`` or ``KrMarketDay``). ``response_next`` is the
    ``nextBusinessDay`` day object or ``None`` if the provider does not
    support the next horizon. ``receipt_identity`` must be a calendar
    wire receipt identity (e.g. ``calendar:US:2026-08-10:1``).

    Fail-closed conditions:
    - invalid provenance (not a calendar receipt identity)
    - nextBusinessDay date does not advance past today
    - same trade-date identity with different session bytes than current cache
    """
    if not isinstance(market, Market):
        raise ValueError("unknown market")
    if not isinstance(fetched_at, datetime) or fetched_at.tzinfo is None:
        raise ValueError("fetched_at must be aware")

    if market is Market.US:
        converter = _day_from_us
    else:
        converter = _day_from_kr

    today_day = converter(
        response_today,
        _safe_date_iso(response_today, "today"),
        _safe_date_iso(response_today, "today"),
        receipt_identity,
        fetched_at,
    )

    days = [today_day]

    if response_next is not None:
        next_day = converter(
            response_next,
            _safe_date_iso(response_next, "next"),
            _safe_date_iso(response_next, "next"),
            receipt_identity,
            fetched_at,
        )
        if next_day.trade_date <= today_day.trade_date:
            raise LookaheadWriterError("next business day must advance past today")
        days.append(next_day)

    # Verify each day against the current cache entry (if same trade_date)
    for day in days:
        try:
            verify_against_current_cache(day, current_cache_entry)
        except CalendarLookaheadConflictError:
            raise

    store.replace_market_days(market, tuple(days))
