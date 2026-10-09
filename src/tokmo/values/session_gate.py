"""API-declared tradable-session decisions."""

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from types import MappingProxyType

from tokmo.values._primitives import MISSING
from tokmo.values.toss_market_calendar import CalendarSession, KrMarketDay, UsMarketDay


class Market(str, Enum):
    KR = "KR"
    US = "US"


class SessionKind(str, Enum):
    DAY_MARKET = "dayMarket"
    PRE_MARKET = "preMarket"
    REGULAR_MARKET = "regularMarket"
    AFTER_MARKET = "afterMarket"


ENABLED_SESSION_KINDS: Mapping[Market, tuple[SessionKind, ...]] = MappingProxyType(
    {
        Market.KR: (
            SessionKind.PRE_MARKET,
            SessionKind.REGULAR_MARKET,
            SessionKind.AFTER_MARKET,
        ),
        Market.US: (
            SessionKind.DAY_MARKET,
            SessionKind.PRE_MARKET,
            SessionKind.REGULAR_MARKET,
            SessionKind.AFTER_MARKET,
        ),
    }
)


MARKET_TIMEZONE: Mapping[Market, str] = MappingProxyType(
    {
        Market.KR: "Asia/Seoul",
        Market.US: "America/New_York",
    }
)
# ``no_regular`` is the sole no-session reason (SDS G-9); the dead
# ``holiday`` token was removed with a full-graph producer search.
SESSION_REASON_TOKENS = frozenset(
    {
        "no_regular",
        "stale_cache",
        "cache_corrupt",
        "cache_absent",
        "before_open",
        "after_close",
        "open",
    }
)


@dataclass(frozen=True)
class SessionVerdict:
    market: Market
    open: bool
    reason: str
    session: CalendarSession | None

    def __post_init__(self):
        if self.reason not in SESSION_REASON_TOKENS:
            raise ValueError("unknown session reason")
        if self.open != (self.reason == "open"):
            raise ValueError("open flag and reason disagree")


def regular_session_for(
    market_day, market: Market, now: datetime
) -> CalendarSession | None:
    if market_day is None:
        return None
    if market is Market.KR:
        integrated = market_day.integrated
        if integrated is None or integrated is MISSING:
            return None
        session = integrated.regular_market
    else:
        session = market_day.regular_market
    if session is None or session is MISSING:
        return None
    if not isinstance(session, CalendarSession):
        raise ValueError("calendar regular session structure changed")
    for boundary in (session.start_time, session.end_time, now):
        if boundary.tzinfo is None or boundary.utcoffset() is None:
            raise ValueError("session comparisons require aware datetimes")
    return session


def sessions_for(market_day, market: Market, now: datetime, *, enabled_sessions):
    """Return valid enabled intervals; malformed provider data closes market."""
    if market_day is None:
        return ()
    expected = frozenset(Market)
    if (
        type(enabled_sessions) not in (dict, MappingProxyType)
        or set(enabled_sessions) != expected
    ):
        raise ValueError("enabled session policy must name every market exactly")
    kinds = enabled_sessions[market]
    if type(kinds) is not tuple or len(set(kinds)) != len(kinds):
        raise ValueError("enabled session policy must be an ordered unique tuple")
    supported = set(ENABLED_SESSION_KINDS[market])
    if any(
        not isinstance(kind, SessionKind) or kind not in supported for kind in kinds
    ):
        raise ValueError("unsupported enabled session kind")
    if market is Market.KR:
        if not isinstance(market_day, KrMarketDay):
            return ()
        integrated = market_day.integrated
        if integrated is None or integrated is MISSING:
            return ()
        values = {
            SessionKind.PRE_MARKET: integrated.pre_market,
            SessionKind.REGULAR_MARKET: integrated.regular_market,
            SessionKind.AFTER_MARKET: integrated.after_market,
        }
    else:
        if not isinstance(market_day, UsMarketDay):
            return ()
        values = {
            SessionKind.DAY_MARKET: market_day.day_market,
            SessionKind.PRE_MARKET: market_day.pre_market,
            SessionKind.REGULAR_MARKET: market_day.regular_market,
            SessionKind.AFTER_MARKET: market_day.after_market,
        }
    result = []
    for kind in kinds:
        value = values[kind]
        if value is None or value is MISSING:
            continue
        if not isinstance(value, CalendarSession):
            return ()
        boundaries = (value.start_time, value.end_time, now)
        if any(
            not isinstance(item, datetime)
            or item.tzinfo is None
            or item.utcoffset() is None
            for item in boundaries
        ):
            return ()
        if value.start_time >= value.end_time:
            return ()
        result.append(value)
    return tuple(result)


def _judge(day, market: Market, now: datetime, enabled_sessions) -> SessionVerdict:
    sessions = sessions_for(
        day,
        market,
        now,
        enabled_sessions=enabled_sessions,
    )
    if not sessions:
        return SessionVerdict(market, False, "no_regular", None)
    for session in sessions:
        if session.start_time <= now < session.end_time:
            return SessionVerdict(market, True, "open", session)
    if any(now < session.start_time for session in sessions):
        return SessionVerdict(market, False, "before_open", None)
    return SessionVerdict(market, False, "after_close", None)


def judge_sessions(
    kr: KrMarketDay | None,
    us: UsMarketDay | None,
    now: datetime,
    *,
    enabled_sessions=ENABLED_SESSION_KINDS,
) -> tuple[SessionVerdict, ...]:
    return (
        _judge(kr, Market.KR, now, enabled_sessions),
        _judge(us, Market.US, now, enabled_sessions),
    )
