"""Typed current/next business trade-day lookahead store and quote gate (§13).

``CalendarCacheSchema2`` stays untouched (one day per market, no migration).
This private store holds at most the current and next business trade day per
market as absolute provider-declared ``[start, end)`` UTC intervals keyed by
the Toss ``trade_date`` identity, each bound to the calendar wire receipt
identity that produced it. The Pulse quote gate judges open/closed by
interval membership in UTC — never by the market's current local date, which
misclassifies the US Day Market between 09:00 and 13:00 KST.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import os
from pathlib import Path
import sqlite3
import stat

from tokmo.values.cadence_policy import validate_cadence_operation
from tokmo.values.material_pack import canonical_decode, canonical_encode
from tokmo.values.session_gate import ENABLED_SESSION_KINDS, Market

LOOKAHEAD_SCHEMA_VERSION = 1
_MAX_DAYS_PER_MARKET = 2
_PAYLOAD_MAX_BYTES = 64 * 1024


class CalendarLookaheadCorruptError(ValueError):
    pass


class CalendarLookaheadConflictError(ValueError):
    """Same trade-date identity with different session bytes (fail-closed)."""


def _aware_utc(value) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() == timedelta(0))


def _iso(value: datetime) -> str:
    return value.isoformat().replace("+00:00", "Z")


def _canonical_date(value) -> None:
    if type(value) is not str or date.fromisoformat(value).isoformat() != value:
        raise ValueError("invalid trade date")


@dataclass(frozen=True)
class LookaheadSessionV1:
    kind: str
    start_utc: datetime
    end_utc: datetime

    def __post_init__(self):
        if type(self.kind) is not str or not self.kind:
            raise ValueError("invalid session kind")
        if not _aware_utc(self.start_utc) or not _aware_utc(self.end_utc):
            raise ValueError("session boundaries must be aware UTC")
        if self.start_utc >= self.end_utc:
            raise ValueError("session must have positive duration")


@dataclass(frozen=True)
class LookaheadDayV1:
    market: Market
    trade_date: str
    source_trade_date: str
    sessions: tuple[LookaheadSessionV1, ...]
    fetched_at: datetime
    provenance: str

    def __post_init__(self):
        if not isinstance(self.market, Market):
            raise ValueError("unknown market")
        _canonical_date(self.trade_date)
        _canonical_date(self.source_trade_date)
        if type(self.sessions) is not tuple or not self.sessions:
            raise ValueError("lookahead day requires sessions")
        supported = {kind.value for kind in ENABLED_SESSION_KINDS[self.market]}
        kinds = []
        for session in self.sessions:
            if not isinstance(session, LookaheadSessionV1):
                raise ValueError("invalid lookahead session")
            if session.kind not in supported:
                raise ValueError("unsupported session kind for market")
            kinds.append(session.kind)
        if len(set(kinds)) != len(kinds):
            raise ValueError("duplicate session kind")
        ordered = sorted(self.sessions, key=lambda item: item.start_utc)
        if tuple(ordered) != self.sessions:
            raise ValueError("sessions must be sorted by start")
        for earlier, later in zip(ordered, ordered[1:]):
            if later.start_utc < earlier.end_utc:
                raise ValueError("sessions must not overlap")
        if not _aware_utc(self.fetched_at):
            raise ValueError("fetched_at must be aware UTC")
        operation = ("CALENDAR_DAY_KR" if self.market is Market.KR
                     else "CALENDAR_DAY_US")
        try:
            validate_cadence_operation(operation, self.provenance)
        except ValueError:
            raise ValueError("provenance must be a calendar receipt identity") from None


def _day_payload(day: LookaheadDayV1) -> dict:
    return {
        "market": day.market.value,
        "trade_date": day.trade_date,
        "source_trade_date": day.source_trade_date,
        "sessions": [
            {"kind": session.kind, "start": _iso(session.start_utc),
             "end": _iso(session.end_utc)}
            for session in day.sessions
        ],
        "fetched_at": _iso(day.fetched_at),
        "provenance": day.provenance,
        "schema_version": LOOKAHEAD_SCHEMA_VERSION,
    }


def _day_from_payload(raw: bytes) -> LookaheadDayV1:
    value = canonical_decode(raw, max_bytes=_PAYLOAD_MAX_BYTES)
    expected = {"market", "trade_date", "source_trade_date", "sessions",
                "fetched_at", "provenance", "schema_version"}
    if type(value) is not dict or set(value) != expected:
        raise ValueError("non-canonical lookahead payload")
    if value["schema_version"] != LOOKAHEAD_SCHEMA_VERSION:
        raise ValueError("unsupported lookahead payload")
    sessions = tuple(
        LookaheadSessionV1(
            item["kind"],
            datetime.fromisoformat(item["start"].replace("Z", "+00:00")),
            datetime.fromisoformat(item["end"].replace("Z", "+00:00")),
        )
        for item in value["sessions"]
    )
    return LookaheadDayV1(
        Market(value["market"]), value["trade_date"],
        value["source_trade_date"], sessions,
        datetime.fromisoformat(value["fetched_at"].replace("Z", "+00:00")),
        value["provenance"],
    )


def verify_against_current_cache(day: LookaheadDayV1, cached) -> None:
    """Fail closed when the same trade-date identity carries different bytes.

    ``cached`` is the market's current ``CalendarDayCacheSchema2`` entry (or
    None). A different trade date is a different identity and is not
    compared; the same trade date must agree on every session interval.
    """
    if not isinstance(day, LookaheadDayV1):
        raise ValueError("invalid lookahead day")
    if cached is None or cached.trade_date != day.trade_date:
        return
    ours = {
        session.kind: (_iso(session.start_utc), _iso(session.end_utc))
        for session in day.sessions
    }
    theirs = {
        kind: (
            _iso(datetime.fromisoformat(value.start.replace("Z", "+00:00"))),
            _iso(datetime.fromisoformat(value.end.replace("Z", "+00:00"))),
        )
        for kind, value in cached.sessions.items()
    }
    if ours != theirs:
        raise CalendarLookaheadConflictError(
            "lookahead and current cache disagree on the same trade date")


_SCHEMA = """
CREATE TABLE IF NOT EXISTS calendar_lookahead(
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  trade_date TEXT NOT NULL,
  payload_json TEXT NOT NULL,
  fetched_at TEXT NOT NULL,
  schema_version INTEGER NOT NULL CHECK(schema_version = 1),
  PRIMARY KEY(market, trade_date)
) STRICT;
"""


class CalendarLookaheadStoreV1:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                value = os.lstat(self.path)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise CalendarLookaheadCorruptError("unsafe lookahead store")
                os.chmod(self.path, 0o600)
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            return connection
        except sqlite3.DatabaseError as error:
            connection.close()
            raise CalendarLookaheadCorruptError("corrupt lookahead store") from error
        except BaseException:
            connection.close()
            raise

    def replace_market_days(self, market: Market,
                            days: tuple[LookaheadDayV1, ...]) -> None:
        """Atomically replace one market's window with 1..2 ascending days."""
        if not isinstance(market, Market):
            raise ValueError("unknown market")
        if (type(days) is not tuple or not 1 <= len(days)
                <= _MAX_DAYS_PER_MARKET):
            raise ValueError("lookahead window holds one or two days")
        dates = []
        for day in days:
            if not isinstance(day, LookaheadDayV1) or day.market is not market:
                raise ValueError("day market mismatch")
            dates.append(day.trade_date)
        if dates != sorted(set(dates)) or len(set(dates)) != len(dates):
            raise ValueError("days must be distinct and ascending")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    "DELETE FROM calendar_lookahead WHERE market=?",
                    (market.value,),
                )
                for day in days:
                    connection.execute(
                        "INSERT INTO calendar_lookahead VALUES(?,?,?,?,?)",
                        (market.value, day.trade_date,
                         canonical_encode(_day_payload(day)).decode(),
                         _iso(day.fetched_at), LOOKAHEAD_SCHEMA_VERSION),
                    )
                connection.execute("COMMIT")
            except BaseException:
                connection.execute("ROLLBACK")
                raise
        except sqlite3.DatabaseError as error:
            raise CalendarLookaheadCorruptError("corrupt lookahead store") from error
        finally:
            connection.close()
        if hasattr(os, "getuid") and self.path.stat().st_uid == os.getuid():
            os.chmod(self.path, 0o600)

    def read_days(self, market: Market) -> tuple[LookaheadDayV1, ...]:
        if not isinstance(market, Market):
            raise ValueError("unknown market")
        if not self.path.exists():
            return ()
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT trade_date, payload_json FROM calendar_lookahead "
                "WHERE market=? ORDER BY trade_date",
                (market.value,),
            ).fetchall()
        except sqlite3.DatabaseError as error:
            raise CalendarLookaheadCorruptError("corrupt lookahead store") from error
        finally:
            connection.close()
        days = []
        for trade_date, payload in rows:
            try:
                day = _day_from_payload(payload.encode())
            except (TypeError, ValueError, KeyError) as error:
                raise CalendarLookaheadCorruptError(
                    "corrupt lookahead payload") from error
            if day.trade_date != trade_date or day.market is not market:
                raise CalendarLookaheadCorruptError("lookahead identity drift")
            days.append(day)
        return tuple(days)


@dataclass(frozen=True)
class PulseSessionVerdictV1:
    market: Market
    open: bool
    reason: str
    trade_date: "str | None"
    session_kind: "str | None"
    session_end_utc: "datetime | None"


def _closed(market, reason):
    return PulseSessionVerdictV1(market, False, reason, None, None, None)


def judge_lookahead_session(market: Market,
                            days: tuple[LookaheadDayV1, ...],
                            now_utc: datetime) -> PulseSessionVerdictV1:
    """Interval-membership session verdict (§13.4).

    Provider-declared absolute ``[start, end)`` membership picks the
    ``trade_date`` — a session that begins on the previous New York evening
    is open before the New York midnight rollover. Any malformed, wrong-
    market, overlapping, or exhausted window fails closed.
    """
    if not isinstance(market, Market):
        raise ValueError("unknown market")
    if not _aware_utc(now_utc):
        raise ValueError("now must be aware UTC")
    if type(days) is not tuple:
        return _closed(market, "lookahead_corrupt")
    if not days:
        return _closed(market, "lookahead_absent")
    intervals = []
    for day in days:
        if not isinstance(day, LookaheadDayV1):
            return _closed(market, "lookahead_corrupt")
        if day.market is not market:
            return _closed(market, "market_mismatch")
        for session in day.sessions:
            intervals.append((session.start_utc, session.end_utc,
                              day.trade_date, session.kind))
    intervals.sort(key=lambda item: (item[0], item[1]))
    for earlier, later in zip(intervals, intervals[1:]):
        if later[0] < earlier[1]:
            return _closed(market, "session_conflict")
    active = [item for item in intervals if item[0] <= now_utc < item[1]]
    if len(active) == 1:
        start, end, trade_date, kind = active[0]
        return PulseSessionVerdictV1(market, True, "open", trade_date, kind, end)
    if now_utc >= max(item[1] for item in intervals):
        return _closed(market, "lookahead_exhausted")
    return _closed(market, "closed_between_sessions")
