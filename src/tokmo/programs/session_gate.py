"""Session-gate orchestration with an injected single-writer cache seam."""

from dataclasses import dataclass
from datetime import date, datetime
from zoneinfo import ZoneInfo

from tokmo.values.session_gate import (
    MARKET_TIMEZONE,
    Market,
    SessionVerdict,
    judge_sessions,
)
from tokmo.values.toss_market_calendar import CalendarSession, IntegratedHour, KrMarketDay, UsMarketDay

B_C_D_READ_ONLY_OWNER = frozenset({"B", "C", "D"})


@dataclass(frozen=True)
class SessionGateResult:
    now: datetime
    verdicts: tuple[SessionVerdict, ...]
    any_open: bool
    cache_written: bool


def _closed(reason):
    verdicts = (
        SessionVerdict(Market.KR, False, reason, None),
        SessionVerdict(Market.US, False, reason, None),
    )
    return verdicts


def _session(value):
    if value is None:
        return None
    start = datetime.fromisoformat(value.start.replace("Z", "+00:00"))
    end = datetime.fromisoformat(value.end.replace("Z", "+00:00"))
    return CalendarSession(start, end)


def _days_from_cache(cache):
    kr = None
    if cache.kr is not None:
        kr = KrMarketDay(
            date.fromisoformat(cache.kr.trade_date),
            IntegratedHour(
                pre_market=_session(cache.kr.sessions.get("preMarket")),
                regular_market=_session(cache.kr.sessions.get("regularMarket")),
                after_market=_session(cache.kr.sessions.get("afterMarket")),
            ),
        )
    us = None
    if cache.us is not None:
        us = UsMarketDay(
            date.fromisoformat(cache.us.trade_date),
            day_market=_session(cache.us.sessions.get("dayMarket")),
            pre_market=_session(cache.us.sessions.get("preMarket")),
            regular_market=_session(cache.us.sessions.get("regularMarket")),
            after_market=_session(cache.us.sessions.get("afterMarket")),
        )
    return kr, us


def _stale(day, market, now):
    if day is None:
        return True
    return date.fromisoformat(day.trade_date) != now.astimezone(
        ZoneInfo(MARKET_TIMEZONE[market])
    ).date()


def run_session_gate(*, kr_calendar_provider, us_calendar_provider,
                     cache_read, cache_write, now: datetime) -> SessionGateResult:
    try:
        cache = cache_read()
    except Exception:
        verdicts = _closed("cache_corrupt")
        return SessionGateResult(now, verdicts, False, False)

    if cache is None:
        if cache_write is None:
            verdicts = _closed("cache_absent")
            return SessionGateResult(now, verdicts, False, False)
        try:
            kr = kr_calendar_provider()
            us = us_calendar_provider()
        except Exception:
            verdicts = _closed("stale_cache")
            return SessionGateResult(now, verdicts, False, False)
        cache_write(kr, us)
        verdicts = judge_sessions(kr, us, now)
        return SessionGateResult(now, verdicts, any(item.open for item in verdicts), True)

    kr_stale = _stale(cache.kr, Market.KR, now)
    us_stale = _stale(cache.us, Market.US, now)
    if (kr_stale or us_stale) and cache_write is None:
        kr, us = _days_from_cache(cache)
        judged = judge_sessions(kr, us, now)
        verdicts = tuple(
            SessionVerdict(item.market, False, "stale_cache", None)
            if ((item.market is Market.KR and kr_stale)
                or (item.market is Market.US and us_stale))
            else item
            for item in judged
        )
        return SessionGateResult(now, verdicts, any(item.open for item in verdicts), False)

    kr, us = _days_from_cache(cache)
    if kr_stale or us_stale:
        try:
            if kr_stale:
                kr = kr_calendar_provider()
            if us_stale:
                us = us_calendar_provider()
        except Exception:
            judged = judge_sessions(kr, us, now)
            verdicts = tuple(
                SessionVerdict(item.market, False, "stale_cache", None)
                if ((item.market is Market.KR and kr_stale)
                    or (item.market is Market.US and us_stale))
                else item
                for item in judged
            )
            return SessionGateResult(now, verdicts, any(item.open for item in verdicts), False)
        cache_write(kr, us)
        written = True
    else:
        written = False
    verdicts = judge_sessions(kr, us, now)
    return SessionGateResult(now, verdicts, any(item.open for item in verdicts), written)
