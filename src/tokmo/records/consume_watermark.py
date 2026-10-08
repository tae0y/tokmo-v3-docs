"""Durable per-source/account watermark for the 300s trade consumer."""

from dataclasses import dataclass
from datetime import datetime, timezone
import os
from pathlib import Path
import sqlite3


def _utc(value, name):
    if (not isinstance(value, datetime) or value.tzinfo is None
            or value.utcoffset() is None):
        raise ValueError(f"{name} must be aware")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class ConsumeWatermarkEntryV1:
    source: str
    account_scope: str
    last_consumed_stored_at: datetime
    advanced_at: datetime
    tick_id: int


class ConsumeWatermarkStoreV1:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self):
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=1000")
        connection.execute("PRAGMA journal_mode=DELETE")
        connection.execute("""
          CREATE TABLE IF NOT EXISTS consume_watermarks(
            source TEXT NOT NULL, account_scope TEXT NOT NULL,
            last_consumed_stored_at TEXT NOT NULL,
            advanced_at TEXT NOT NULL, tick_id INTEGER NOT NULL,
            PRIMARY KEY(source, account_scope)
          ) STRICT
        """)
        os.chmod(self.path, 0o600)
        return connection

    @staticmethod
    def _entry(row):
        return ConsumeWatermarkEntryV1(
            row["source"], row["account_scope"],
            datetime.fromisoformat(row["last_consumed_stored_at"]),
            datetime.fromisoformat(row["advanced_at"]), row["tick_id"])

    def read(self, *, source, account_scope):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM consume_watermarks WHERE source=? AND account_scope=?",
                (source, account_scope)).fetchone()
        return None if row is None else self._entry(row)

    def bootstrap(self, *, source, account_scope, cutover_epoch_utc):
        cutover = _utc(cutover_epoch_utc, "cutover_epoch_utc")
        advanced = _utc(self._clock(), "clock")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO consume_watermarks VALUES (?,?,?,?,?)",
                (source, account_scope, cutover.isoformat(),
                 advanced.isoformat(), 0))

    def advance(self, *, source, account_scope, last_consumed_stored_at,
                advanced_at, tick_id):
        last = _utc(last_consumed_stored_at, "last_consumed_stored_at")
        advanced = _utc(advanced_at, "advanced_at")
        if type(tick_id) is not int or tick_id < 0:
            raise ValueError("invalid tick_id")
        with self._connect() as connection:
            current = connection.execute(
                "SELECT last_consumed_stored_at FROM consume_watermarks "
                "WHERE source=? AND account_scope=?", (source, account_scope),
            ).fetchone()
            if current is None:
                raise ValueError("watermark must be bootstrapped")
            if last < datetime.fromisoformat(current[0]):
                raise ValueError("watermark cannot move backward")
            connection.execute(
                "UPDATE consume_watermarks SET last_consumed_stored_at=?, "
                "advanced_at=?, tick_id=? WHERE source=? AND account_scope=?",
                (last.isoformat(), advanced.isoformat(), tick_id,
                 source, account_scope))

    def read_all(self, *, source):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM consume_watermarks WHERE source=? "
                "ORDER BY account_scope", (source,)).fetchall()
        return tuple(self._entry(row) for row in rows)
