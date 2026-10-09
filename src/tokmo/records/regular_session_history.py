"""Durable official regular-session history for the production cadence (M2).

One append-only STRICT SQLite row per ``(market, local_date)`` holding only
the official Toss calendar verdict — ``regular`` or ``no_regular`` — plus the
admitting calendar attempt identity and the capture time. The store never
fabricates a weekday or holiday: every append must present validated kernel
provenance (a live claimed calendar receipt for a backfilled date, or the
durably SUCCEEDED attempt row for the current-date fact), and a conflicting
re-append fails closed without touching the first row.

``HistorySessionDatesV1`` is the production trades session-date source: it
reads only this store, walking descending local dates from the anchor until
``count`` regular sessions are found within at most 35 examined dates, and an
insufficient or corrupt history raises ``ValueError`` — the trades collector's
truthful ``failed_calendar``.
"""

import fcntl
import os
import re
import sqlite3
import stat
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path

from tokmo.values.ops_kernel import iso, parse_iso
from tokmo.values.session_gate import Market

HISTORY_SESSION_TOKENS = frozenset({"regular", "no_regular"})
BACKFILL_TARGET_SESSIONS = 20
BACKFILL_MAX_EXAMINED_DATES = 35
_HISTORY_SCHEMA_VERSION = 1
_SOURCE_IDENTITY = re.compile(r"calendar:(KR|US):\d{4}-\d{2}-\d{2}:[0-5]")

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS regular_session_history(
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  local_date TEXT NOT NULL CHECK(length(local_date)=10 AND
    local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  session TEXT NOT NULL CHECK(session IN ('regular','no_regular')),
  source_identity TEXT NOT NULL CHECK(length(source_identity) BETWEEN 1 AND 128),
  captured_at TEXT NOT NULL,
  PRIMARY KEY(market, local_date)
) STRICT;
PRAGMA user_version={_HISTORY_SCHEMA_VERSION};
"""


class RegularSessionHistoryError(ValueError):
    pass


class RegularSessionHistoryCorruptError(RegularSessionHistoryError):
    """The private file, schema, or row bytes are not trustworthy."""


class RegularSessionHistoryConflictError(RegularSessionHistoryError):
    """A different official fact already owns this (market, local_date)."""


class RegularSessionHistoryProvenanceError(RegularSessionHistoryError):
    """No validated official calendar provenance authorizes this append."""


def _validate_fact(market, local_date, session):
    if not isinstance(market, Market):
        raise RegularSessionHistoryError("unknown market")
    if type(local_date) is not date or isinstance(local_date, datetime):
        raise RegularSessionHistoryError("local date must be a date")
    if session not in HISTORY_SESSION_TOKENS:
        raise RegularSessionHistoryError("unknown session verdict token")


class RegularSessionHistoryStore:
    """Private durable append-only regular-session facts (construction I/O 0)."""

    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _check_private(self) -> None:
        if self.path.is_symlink():
            raise RegularSessionHistoryCorruptError("unsafe history store")
        if self.path.exists():
            value = os.lstat(self.path)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise RegularSessionHistoryCorruptError("unsafe history store")
            os.chmod(self.path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = self.path.with_name(self.path.name + suffix)
            if sidecar.exists():
                if not stat.S_ISREG(os.lstat(sidecar).st_mode):
                    raise RegularSessionHistoryCorruptError("unsafe history sidecar")
                os.chmod(sidecar, 0o600)

    @staticmethod
    def _open_flags() -> int:
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        return flags

    def _publish_fresh(self) -> None:
        """Create the store as one atomic publish of a fully-formed database.

        Fresh creation is the only moment the schema is ever written: the
        database is built WAL-complete at a private temp path and renamed
        into place, all under an exclusive flock, so no concurrent opener —
        thread or process — can observe a partial schema at ``self.path``.
        """
        lock_path = self.path.with_name(self.path.name + ".init-lock")
        lock_fd = os.open(lock_path, self._open_flags(), 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            if self.path.exists() or self.path.is_symlink():
                return  # another creator published first; validated on open
            fresh = self.path.with_name(self.path.name + ".fresh")
            for stale in (
                fresh,
                fresh.with_name(fresh.name + "-wal"),
                fresh.with_name(fresh.name + "-shm"),
            ):
                stale.unlink(missing_ok=True)
            os.close(os.open(fresh, self._open_flags(), 0o600))
            try:
                connection = sqlite3.connect(
                    fresh,
                    timeout=5.0,
                    isolation_level=None,
                )
                try:
                    connection.execute("PRAGMA trusted_schema=OFF")
                    connection.execute("PRAGMA journal_mode=WAL")
                    connection.execute("PRAGMA synchronous=FULL")
                    connection.executescript(_SCHEMA)
                finally:
                    connection.close()
                os.rename(fresh, self.path)
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except BaseException:
                fresh.unlink(missing_ok=True)
                raise
        finally:
            os.close(lock_fd)

    @contextmanager
    def _connect(self, *, create: bool):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        if not (self.path.exists() or self.path.is_symlink()):
            if not create:
                raise RegularSessionHistoryCorruptError("history store absent")
            self._publish_fresh()
        self._check_private()
        connection = sqlite3.connect(self.path, timeout=5.0, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA trusted_schema=OFF")
            connection.execute("PRAGMA foreign_keys=ON")
            journal = connection.execute("PRAGMA journal_mode=WAL").fetchone()[0]
            if journal.lower() != "wal":
                raise RegularSessionHistoryCorruptError("journal mode mismatch")
            connection.execute("PRAGMA synchronous=FULL")
            self._validate(connection)
            self._check_private()
            yield connection
        except sqlite3.Error as error:
            raise RegularSessionHistoryCorruptError(
                "history store unavailable"
            ) from error
        finally:
            connection.close()

    @staticmethod
    def _validate(connection) -> None:
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise RegularSessionHistoryCorruptError("history integrity check failed")
        version = connection.execute("PRAGMA user_version").fetchone()[0]
        if version != _HISTORY_SCHEMA_VERSION:
            raise RegularSessionHistoryCorruptError("history schema mismatch")

        def objects(database):
            return {
                (
                    row["type"],
                    row["name"],
                    row["tbl_name"],
                    None if row["sql"] is None else " ".join(row["sql"].split()),
                )
                for row in database.execute(
                    "SELECT type,name,tbl_name,sql FROM sqlite_schema"
                    " WHERE name NOT LIKE 'sqlite_stat%'"
                )
            }

        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            reference.executescript(_SCHEMA)
            if objects(connection) != objects(reference):
                raise RegularSessionHistoryCorruptError("history schema tamper")
        finally:
            reference.close()

    @staticmethod
    def _row(row) -> dict:
        if (
            row["session"] not in HISTORY_SESSION_TOKENS
            or not _SOURCE_IDENTITY.fullmatch(row["source_identity"])
            or date.fromisoformat(row["local_date"]).isoformat() != row["local_date"]
        ):
            raise RegularSessionHistoryCorruptError("non-canonical history row")
        return {
            "market": Market(row["market"]),
            "local_date": date.fromisoformat(row["local_date"]),
            "session": row["session"],
            "source_identity": row["source_identity"],
            "captured_at": parse_iso(row["captured_at"]),
        }

    def _insert(self, market, local_date, session, source_identity) -> str:
        if not _SOURCE_IDENTITY.fullmatch(source_identity or ""):
            raise RegularSessionHistoryProvenanceError(
                "non-canonical calendar source identity"
            )
        with self._connect(create=True) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT session FROM regular_session_history "
                    "WHERE market=? AND local_date=?",
                    (market.value, local_date.isoformat()),
                ).fetchone()
                if existing is not None:
                    if existing["session"] == session:
                        connection.execute("ROLLBACK")
                        return "duplicate"
                    raise RegularSessionHistoryConflictError(
                        "conflicting official fact for this market local date"
                    )
                connection.execute(
                    "INSERT INTO regular_session_history VALUES(?,?,?,?,?)",
                    (
                        market.value,
                        local_date.isoformat(),
                        session,
                        source_identity,
                        iso(self._clock()),
                    ),
                )
                connection.execute("COMMIT")
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
        return "created"

    def append_backfilled(
        self, market, local_date, session, *, receipt, authority
    ) -> str:
        """Append one backfilled official fact under a live claimed receipt.

        ``authority`` is the operations kernel: the receipt is validated
        against its current STARTED attempt/wire rows with a consumed
        provider claim before any byte is written; a forged, terminal, or
        mismatched receipt appends nothing.
        """
        _validate_fact(market, local_date, session)
        if authority is None:
            raise RegularSessionHistoryProvenanceError(
                "history append requires the kernel authority"
            )
        if (
            getattr(receipt, "market", None) is not market
            or getattr(receipt, "market_local_date", None) != local_date
        ):
            raise RegularSessionHistoryProvenanceError(
                "calendar receipt identity mismatch"
            )
        authority.verify_calendar_history_receipt(receipt, now=self._clock())
        return self._insert(market, local_date, session, receipt.native_identity)

    def append_settled(self, market, local_date, session, *, authority) -> str:
        """Append the current-date fact from its already-settled admission.

        Used when the fact was fetched (this tick or a prior one) through the
        kernel's calendar admission and durably committed: the SUCCEEDED
        attempt row with its consumed provider claim is the provenance. A
        cache entry with no such durable admission is never promoted into
        history — do not forge.
        """
        _validate_fact(market, local_date, session)
        if authority is None:
            raise RegularSessionHistoryProvenanceError(
                "history append requires the kernel authority"
            )
        provenance = authority.read_settled_calendar_provenance(market, local_date)
        if provenance is None:
            raise RegularSessionHistoryProvenanceError(
                "no validated official calendar provenance"
            )
        return self._insert(
            market,
            local_date,
            session,
            provenance["native_identity"],
        )

    def read(self, market, local_date) -> dict | None:
        _validate_fact(market, local_date, "regular")
        if not self.path.exists() and not self.path.is_symlink():
            return None
        with self._connect(create=False) as connection:
            row = connection.execute(
                "SELECT * FROM regular_session_history WHERE market=? AND local_date=?",
                (market.value, local_date.isoformat()),
            ).fetchone()
        return None if row is None else self._row(row)

    def next_backfill_target(
        self,
        market,
        anchor,
        *,
        sessions: int = BACKFILL_TARGET_SESSIONS,
        max_examined: int = BACKFILL_MAX_EXAMINED_DATES,
    ) -> date | None:
        """First missing historical local date on the deterministic walk.

        Descending from the anchor: the anchor itself is owned by the
        current-date refresh and is never a backfill target; the walk stops
        at ``sessions`` known regular dates or after ``max_examined`` local
        dates — date ``max_examined + 1`` is never inspected.
        """
        _validate_fact(market, anchor, "regular")
        found = 0
        cursor = anchor
        for _ in range(max_examined):
            row = self.read(market, cursor)
            if row is None:
                if cursor != anchor:
                    return cursor
            elif row["session"] == "regular":
                found += 1
                if found >= sessions:
                    return None
            cursor -= timedelta(days=1)
        return None


class HistorySessionDatesV1:
    """Production trades session-date source: durable official history only."""

    def __init__(self, store: RegularSessionHistoryStore):
        if not isinstance(store, RegularSessionHistoryStore):
            raise ValueError("history session dates require the history store")
        self._store = store

    def regular_session_dates(self, market, anchor_date, count):
        if not isinstance(market, Market):
            raise ValueError("unknown market")
        if type(anchor_date) is not date or isinstance(anchor_date, datetime):
            raise ValueError("anchor must be a date")
        if type(count) is not int or count < 1:
            raise ValueError("invalid session-date count")
        found: list[date] = []
        cursor = anchor_date
        examined = 0
        while len(found) < count:
            if examined >= BACKFILL_MAX_EXAMINED_DATES:
                raise ValueError("failed_calendar")
            row = self._store.read(market, cursor)
            if row is None:
                # a gap in the durable official record is insufficiency,
                # never a weekday guess
                raise ValueError("failed_calendar")
            examined += 1
            if row["session"] == "regular":
                found.append(cursor)
            cursor -= timedelta(days=1)
        return tuple(found)
