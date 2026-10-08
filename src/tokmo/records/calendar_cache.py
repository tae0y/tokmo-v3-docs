"""Typed mutable market-calendar cache."""

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import date, datetime, timezone
from enum import Enum
import fcntl
import hashlib
import os
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values._primitives import MISSING
from tokmo.values.calendar_refresh import CalendarFetchResult, CalendarIdentityError
from tokmo.values.material_pack import canonical_decode, canonical_encode
from tokmo.values.session_gate import ENABLED_SESSION_KINDS, MARKET_TIMEZONE, Market, SessionKind
from tokmo.values.toss_market_calendar import CalendarSession, KrMarketDay, UsMarketDay

CALENDAR_CACHE_SCHEMA_VERSION = 2
CALENDAR_CACHE_MAX_BYTES = 64 * 1024
CALENDAR_PARSER_VERSION = "toss-market-calendar-v1"


class CalendarCacheCorruptError(ValueError):
    pass


class CalendarCacheConflictError(ValueError):
    """A market-level replacement was refused without touching the file."""


class CalendarCacheCasConflictError(CalendarCacheConflictError):
    """The caller's expected previous digest no longer matches the store."""


class CalendarCacheSameDateConflictError(CalendarCacheConflictError):
    """Same trade date with different content requires a reviewed replacement."""


class CalendarReplaceResult(Enum):
    REPLACED = "REPLACED"
    UNCHANGED = "UNCHANGED"


def _parse_time(value: str) -> datetime:
    if type(value) is not str:
        raise ValueError("timestamp must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    canonical = parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if canonical != value:
        raise ValueError("timestamp must be canonical UTC")
    return parsed


def _iso_time(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class CalendarSessionCacheSchema2:
    start: str
    end: str

    def __post_init__(self):
        start = _parse_time(self.start)
        end = _parse_time(self.end)
        if start >= end:
            raise ValueError("calendar session must have positive duration")


@dataclass(frozen=True)
class CalendarDayCacheSchema2:
    trade_date: str
    sessions: dict[str, CalendarSessionCacheSchema2]
    source_trade_date: str
    fetched_at: str
    schema_version: int
    parser_version: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != CALENDAR_CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported calendar day cache")
        for value in (self.trade_date, self.source_trade_date):
            if date.fromisoformat(value).isoformat() != value:
                raise ValueError("invalid trade date")
        if type(self.sessions) is not dict:
            raise ValueError("invalid calendar sessions")
        for kind, value in self.sessions.items():
            if type(kind) is not str or not isinstance(value, CalendarSessionCacheSchema2):
                raise ValueError("invalid cached calendar session")
        _parse_time(self.fetched_at)
        if type(self.parser_version) is not str or not self.parser_version:
            raise ValueError("invalid parser_version")


@dataclass(frozen=True)
class CalendarCacheSchema2:
    schema_version: int
    kr: CalendarDayCacheSchema2 | None
    us: CalendarDayCacheSchema2 | None
    updated_at: str

    def __post_init__(self):
        if type(self.schema_version) is not int or self.schema_version != CALENDAR_CACHE_SCHEMA_VERSION:
            raise ValueError("unsupported calendar cache")
        if self.kr is not None and not isinstance(self.kr, CalendarDayCacheSchema2):
            raise ValueError("invalid KR cache")
        if self.us is not None and not isinstance(self.us, CalendarDayCacheSchema2):
            raise ValueError("invalid US cache")
        _parse_time(self.updated_at)


def _day_dict(day):
    if day is None:
        return None
    return {
        "trade_date": day.trade_date,
        "sessions": {kind: {"start": value.start, "end": value.end}
                     for kind, value in day.sessions.items()},
        "source_trade_date": day.source_trade_date,
        "fetched_at": day.fetched_at, "schema_version": day.schema_version,
        "parser_version": day.parser_version,
    }


def _cache_bytes(cache):
    return canonical_encode({
        "schema_version": cache.schema_version, "kr": _day_dict(cache.kr),
        "us": _day_dict(cache.us), "updated_at": cache.updated_at,
    })


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("calendar cache exact keys required")


def _decode_day(value, market):
    if value is None:
        return None
    keys = {
        "trade_date", "sessions", "source_trade_date",
        "fetched_at", "schema_version", "parser_version",
    }
    _exact(value, keys)
    if type(value["sessions"]) is not dict:
        raise ValueError("invalid calendar sessions")
    supported = {kind.value for kind in ENABLED_SESSION_KINDS[market]}
    if not set(value["sessions"]).issubset(supported):
        raise ValueError("calendar parser contract drift")
    sessions = {}
    for kind, boundary in value["sessions"].items():
        _exact(boundary, {"start", "end"})
        sessions[kind] = CalendarSessionCacheSchema2(**boundary)
    return CalendarDayCacheSchema2(**{**value, "sessions": sessions})


def _decode_cache(raw):
    value = canonical_decode(raw, max_bytes=CALENDAR_CACHE_MAX_BYTES)
    _exact(value, {"schema_version", "kr", "us", "updated_at"})
    if value["schema_version"] == 1:
        # V1 contained only regular boundaries. Validate its outer identity,
        # then discard both incomplete entries so callers refresh fail-closed.
        legacy_keys = {
            "trade_date", "regular_start", "regular_end", "source_trade_date",
            "fetched_at", "schema_version", "parser_version",
        }
        for entry in (value["kr"], value["us"]):
            if entry is None:
                continue
            _exact(entry, legacy_keys)
            if entry["schema_version"] != 1:
                raise ValueError("invalid legacy calendar day")
            for key in ("trade_date", "source_trade_date"):
                if date.fromisoformat(entry[key]).isoformat() != entry[key]:
                    raise ValueError("invalid legacy trade date")
            if (entry["regular_start"] is None) != (entry["regular_end"] is None):
                raise ValueError("incomplete legacy regular session")
            if entry["regular_start"] is not None:
                if _parse_time(entry["regular_start"]) >= _parse_time(entry["regular_end"]):
                    raise ValueError("invalid legacy regular session")
            _parse_time(entry["fetched_at"])
        _parse_time(value["updated_at"])
        return CalendarCacheSchema2(2, None, None, value["updated_at"])
    result = CalendarCacheSchema2(
        value["schema_version"], _decode_day(value["kr"], Market.KR),
        _decode_day(value["us"], Market.US),
        value["updated_at"],
    )
    if _cache_bytes(result) != raw:
        raise ValueError("non-canonical calendar cache")
    return result


def calendar_market_digest(entry: "CalendarDayCacheSchema2 | None") -> str | None:
    """CAS digest of one market's cache entry; None when the entry is absent."""
    if entry is None:
        return None
    if not isinstance(entry, CalendarDayCacheSchema2):
        raise ValueError("invalid calendar cache entry")
    return hashlib.sha256(canonical_encode(_day_dict(entry))).hexdigest()


def _semantic_entry(entry: "CalendarDayCacheSchema2"):
    """Content identity for same-date comparison; fetch time is metadata."""
    projection = _day_dict(entry)
    projection.pop("fetched_at")
    return projection


def _to_cached(day, market, fetched_at):
    if day is None:
        return None
    values = {}
    if market is Market.KR:
        integrated = day.integrated
        source = {} if integrated is None or integrated is MISSING else {
            SessionKind.PRE_MARKET: integrated.pre_market,
            SessionKind.REGULAR_MARKET: integrated.regular_market,
            SessionKind.AFTER_MARKET: integrated.after_market,
        }
    else:
        source = {
            SessionKind.DAY_MARKET: day.day_market,
            SessionKind.PRE_MARKET: day.pre_market,
            SessionKind.REGULAR_MARKET: day.regular_market,
            SessionKind.AFTER_MARKET: day.after_market,
        }
    for kind in ENABLED_SESSION_KINDS[market]:
        session = source.get(kind, MISSING)
        if session is None or session is MISSING:
            continue
        if not isinstance(session, CalendarSession):
            raise ValueError("calendar session structure changed")
        values[kind.value] = CalendarSessionCacheSchema2(
            _iso_time(session.start_time), _iso_time(session.end_time),
        )
    return CalendarDayCacheSchema2(
        trade_date=day.date.isoformat(),
        sessions=values,
        source_trade_date=day.date.isoformat(),
        fetched_at=fetched_at,
        schema_version=CALENDAR_CACHE_SCHEMA_VERSION,
        parser_version=CALENDAR_PARSER_VERSION,
    )


class CalendarCacheStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self.clock = clock

    def read(self) -> CalendarCacheSchema2 | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            return _decode_cache(read_atomic_json(self.path, max_bytes=CALENDAR_CACHE_MAX_BYTES))
        except (OSError, TypeError, ValueError) as error:
            raise CalendarCacheCorruptError("corrupt calendar cache") from error

    def write(self, kr: KrMarketDay | None, us: UsMarketDay | None) -> None:
        now = _iso_time(self.clock())
        candidate = CalendarCacheSchema2(
            CALENDAR_CACHE_SCHEMA_VERSION,
            _to_cached(kr, Market.KR, now), _to_cached(us, Market.US, now), now,
        )
        previous = self.read()
        if (previous is not None
                and (None if previous.kr is None else previous.kr.trade_date)
                == (None if candidate.kr is None else candidate.kr.trade_date)
                and (None if previous.us is None else previous.us.trade_date)
                == (None if candidate.us is None else candidate.us.trade_date)):
            return
        write_atomic_json(
            self.path, _cache_bytes(candidate), max_bytes=CALENDAR_CACHE_MAX_BYTES,
        )

    @contextmanager
    def _store_lock(self):
        """One exclusive lock over read -> CAS -> validation -> atomic publish."""
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        lock_path = self.path.with_name(self.path.name + ".lock")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    def replace_market(
        self, market: Market, expected_previous_digest: str | None,
        fetch: CalendarFetchResult, *, authority,
    ) -> CalendarReplaceResult:
        """Locked CAS replacement of one market entry, sealed by the kernel.

        ``authority`` is the authoritative OpsKernel store: immediately before
        the file write, the receipt's one-time cache-commit claim is consumed
        there against the exact STARTED/claimed rows (B1). A forged, stale,
        terminal, or reused receipt raises and mutates cache zero.
        """
        if authority is None:
            raise ValueError("calendar cache commit requires the kernel authority")
        if not isinstance(market, Market):
            raise ValueError("unknown market")
        if not isinstance(fetch, CalendarFetchResult) or fetch.market is not market:
            raise CalendarIdentityError("fetch result market mismatch")
        with self._store_lock():
            previous = self.read()
            current = None if previous is None else (
                previous.kr if market is Market.KR else previous.us
            )
            if calendar_market_digest(current) != expected_previous_digest:
                raise CalendarCacheCasConflictError("calendar cache CAS conflict")
            now = _iso_time(self.clock())
            candidate = _to_cached(fetch.day, market, now)
            if current is not None and current.trade_date == candidate.trade_date:
                if _semantic_entry(current) == _semantic_entry(candidate):
                    return CalendarReplaceResult.UNCHANGED
                raise CalendarCacheSameDateConflictError(
                    "same-date content conflict requires reviewed replacement"
                )
            kr = candidate if market is Market.KR else (
                None if previous is None else previous.kr
            )
            us = candidate if market is Market.US else (
                None if previous is None else previous.us
            )
            # Durable one-time consumption in the kernel DB; raises (and so
            # publishes nothing) unless the receipt is currently claimed and
            # not yet cache-committed.
            authority.consume_calendar_cache_commit(fetch.receipt, now=self.clock())
            cache = CalendarCacheSchema2(CALENDAR_CACHE_SCHEMA_VERSION, kr, us, now)
            write_atomic_json(
                self.path, _cache_bytes(cache), max_bytes=CALENDAR_CACHE_MAX_BYTES,
            )
            return CalendarReplaceResult.REPLACED

    def is_stale(self, market: Market, now: datetime) -> bool:
        if now.tzinfo is None or now.utcoffset() is None:
            raise ValueError("now must be aware")
        cache = self.read()
        if cache is None:
            return True
        day = cache.kr if market is Market.KR else cache.us
        if day is None:
            return True
        return date.fromisoformat(day.trade_date) != now.astimezone(
            ZoneInfo(MARKET_TIMEZONE[market])
        ).date()
