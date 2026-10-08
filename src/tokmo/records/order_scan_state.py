"""Bounded durable state for completed-order scanning."""

from contextlib import contextmanager
from dataclasses import asdict, dataclass
import os
from pathlib import Path
import sqlite3
import stat

from tokmo.values.session_gate import Market


class ScanStateCorruptError(ValueError):
    pass


class ScanStateLockedError(RuntimeError):
    pass


@dataclass(frozen=True)
class ScanStateV1:
    account_scope: str
    watermark_date: str | None
    pending_cursor: str | None
    pending_window_from: str | None
    pending_window_to: str | None
    backfill_cursor: str | None
    backfill_active: bool
    last_backfill_date: str | None
    watermark_frozen: bool
    updated_at: str


@dataclass(frozen=True)
class IncompleteRetryRow:
    account_scope: str
    source_record_key: str
    classified_market: Market
    first_seen_tick_id: int
    attempts: int
    last_attempt_tick_id: int
    session_age: int
    reference_date: str
    terminal_state: str | None


@dataclass(frozen=True)
class IngestAuditRow:
    source: str
    account_scope: str
    source_record_key: str
    filled_at_utc: str
    tick_id: int
    stored_at: str


@dataclass(frozen=True)
class MarketPendingRow:
    account_scope: str
    order_id: str
    symbol: str
    currency: str | None
    status: str
    reason: str
    first_seen_tick_id: int
    updated_at: str


_SCHEMA = """
CREATE TABLE IF NOT EXISTS order_scan_state(
  account_scope TEXT PRIMARY KEY, watermark_date TEXT,
  pending_cursor TEXT, pending_window_from TEXT,
  pending_window_to TEXT, backfill_cursor TEXT,
  backfill_active INTEGER NOT NULL DEFAULT 0,
  last_backfill_date TEXT,
  watermark_frozen INTEGER NOT NULL DEFAULT 0,
  updated_at TEXT NOT NULL) STRICT;
CREATE TABLE IF NOT EXISTS execution_quarantine(
  account_scope TEXT, source_record_key TEXT,
  quarantined_at TEXT, reason TEXT,
  PRIMARY KEY(account_scope,source_record_key)) STRICT;
CREATE TABLE IF NOT EXISTS execution_incomplete_retry(
  account_scope TEXT, source_record_key TEXT,
  classified_market TEXT NOT NULL CHECK(classified_market IN ('KR','US')),
  first_seen_tick_id INTEGER, attempts INTEGER,
  last_attempt_tick_id INTEGER, session_age INTEGER NOT NULL DEFAULT 0,
  reference_date TEXT NOT NULL, terminal_state TEXT,
  PRIMARY KEY(account_scope,source_record_key)) STRICT;
CREATE TABLE IF NOT EXISTS execution_ingest_audit(
  source TEXT, account_scope TEXT, source_record_key TEXT,
  filled_at_utc TEXT NOT NULL, tick_id INTEGER, stored_at TEXT,
  PRIMARY KEY(source,account_scope,source_record_key)) STRICT;
CREATE TABLE IF NOT EXISTS execution_market_pending(
  account_scope TEXT, order_id TEXT, symbol TEXT, currency TEXT,
  status TEXT, reason TEXT, first_seen_tick_id INTEGER,
  updated_at TEXT NOT NULL,
  PRIMARY KEY(account_scope,order_id)) STRICT;
"""


class OrderScanStateStore:
    def __init__(self, path, *, clock):
        self.path = Path(path)
        self.clock = clock
        self._validate_path()
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            self._validate_parent()
            if not self.path.exists():
                flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                if hasattr(os, "O_NOFOLLOW"):
                    flags |= os.O_NOFOLLOW
                fd = os.open(self.path, flags, 0o600)
                os.fchmod(fd, 0o600)
                os.close(fd)
            self._validate_path(require_regular=True)
            with self._connection() as db:
                db.executescript(_SCHEMA)
                self._validate_schema(db)
        except ScanStateCorruptError:
            raise
        except sqlite3.OperationalError as error:
            self._raise_database(error)
        except (OSError, sqlite3.DatabaseError) as error:
            raise ScanStateCorruptError("corrupt scan state") from error

    def _validate_parent(self):
        current = self.path.parent
        while current != current.parent:
            if current.is_symlink():
                raise ScanStateCorruptError("symlink parent rejected")
            current = current.parent
        os.chmod(self.path.parent, 0o700)

    def _validate_path(self, require_regular=False):
        if self.path.is_symlink():
            raise ScanStateCorruptError("symlink scan state rejected")
        if require_regular:
            try:
                mode = os.lstat(self.path).st_mode
            except OSError as error:
                raise ScanStateCorruptError("corrupt scan state") from error
            if not stat.S_ISREG(mode):
                raise ScanStateCorruptError("non-regular scan state rejected")
            os.chmod(self.path, 0o600)

    @contextmanager
    def _connection(self):
        db = None
        try:
            db = sqlite3.connect(self.path, timeout=1)
            db.execute("PRAGMA journal_mode=DELETE")
            db.execute("PRAGMA synchronous=FULL")
            db.execute("PRAGMA busy_timeout=1000")
            yield db
            db.commit()
        except sqlite3.OperationalError as error:
            if db is not None:
                db.rollback()
            self._raise_database(error)
        except sqlite3.DatabaseError as error:
            if db is not None:
                db.rollback()
            raise ScanStateCorruptError("corrupt scan state") from error
        finally:
            if db is not None:
                db.close()

    @staticmethod
    def _raise_database(error):
        text = str(error).lower()
        if "locked" in text or "busy" in text:
            raise ScanStateLockedError("scan state locked") from error
        raise ScanStateCorruptError("corrupt scan state") from error

    @staticmethod
    def _validate_schema(db):
        expected = {
            "order_scan_state": (
                "account_scope", "watermark_date", "pending_cursor",
                "pending_window_from", "pending_window_to", "backfill_cursor",
                "backfill_active", "last_backfill_date", "watermark_frozen",
                "updated_at",
            ),
            "execution_incomplete_retry": (
                "account_scope", "source_record_key", "classified_market",
                "first_seen_tick_id", "attempts", "last_attempt_tick_id",
                "session_age", "reference_date", "terminal_state",
            ),
            "execution_quarantine": (
                "account_scope", "source_record_key", "quarantined_at", "reason",
            ),
            "execution_ingest_audit": (
                "source", "account_scope", "source_record_key",
                "filled_at_utc", "tick_id", "stored_at",
            ),
            "execution_market_pending": (
                "account_scope", "order_id", "symbol", "currency", "status",
                "reason", "first_seen_tick_id", "updated_at",
            ),
        }
        for table, columns in expected.items():
            actual = tuple(
                row[1] for row in db.execute(f"PRAGMA table_info({table})")
            )
            if actual != columns:
                raise ScanStateCorruptError("noncanonical scan state schema")

    def read_state(self, scope):
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM order_scan_state WHERE account_scope=?", (scope,)
            ).fetchone()
        return None if row is None else ScanStateV1(
            *row[:6], bool(row[6]), row[7], bool(row[8]), row[9],
        )

    def save_state(self, state):
        values = asdict(state)
        values["backfill_active"] = int(state.backfill_active)
        values["watermark_frozen"] = int(state.watermark_frozen)
        self._replace("order_scan_state", values)

    def read_quarantine(self, scope):
        with self._connection() as db:
            rows = db.execute(
                "SELECT source_record_key FROM execution_quarantine "
                "WHERE account_scope=? ORDER BY source_record_key", (scope,)
            ).fetchall()
        return tuple(row[0] for row in rows)

    def add_quarantine_keys(self, scope, keys, reason):
        timestamp = self.clock().isoformat()
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "INSERT OR REPLACE INTO execution_quarantine VALUES(?,?,?,?)",
                ((scope, key, timestamp, reason) for key in keys),
            )

    def remove_quarantine_keys(self, scope, keys):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.executemany(
                "DELETE FROM execution_quarantine WHERE account_scope=? "
                "AND source_record_key=?", ((scope, key) for key in keys),
            )

    def read_incomplete_retry(self):
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM execution_incomplete_retry "
                "WHERE terminal_state IS NULL "
                "ORDER BY first_seen_tick_id,account_scope,source_record_key"
            ).fetchall()
        try:
            return tuple(
                IncompleteRetryRow(row[0], row[1], Market(row[2]), *row[3:])
                for row in rows
            )
        except (TypeError, ValueError) as error:
            raise ScanStateCorruptError("noncanonical incomplete retry") from error

    def add_incomplete_retry(self, row):
        values = asdict(row)
        values["classified_market"] = row.classified_market.value
        self._replace("execution_incomplete_retry", values)

    def resolve_incomplete_retry(self, account_scope, key):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM execution_incomplete_retry "
                "WHERE account_scope=? AND source_record_key=?",
                (account_scope, key),
            )

    def expire_incomplete_retry(self, account_scope, key):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "UPDATE execution_incomplete_retry SET "
                "terminal_state='INCOMPLETE_EXPIRED' "
                "WHERE account_scope=? AND source_record_key=?",
                (account_scope, key),
            )

    def read_ingest_audit(self, key):
        with self._connection() as db:
            row = db.execute(
                "SELECT * FROM execution_ingest_audit WHERE "
                "source=? AND account_scope=? AND source_record_key=?", tuple(key)
            ).fetchone()
        return None if row is None else IngestAuditRow(*row)

    def write_ingest_audit(self, row):
        self._replace("execution_ingest_audit", asdict(row))

    def read_market_pending(self, scope):
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM execution_market_pending WHERE account_scope=? "
                "ORDER BY order_id", (scope,)
            ).fetchall()
        return tuple(MarketPendingRow(*row) for row in rows)

    def read_all_market_pending(self):
        with self._connection() as db:
            rows = db.execute(
                "SELECT * FROM execution_market_pending ORDER BY account_scope,order_id"
            ).fetchall()
        return tuple(MarketPendingRow(*row) for row in rows)

    def add_market_pending(self, row):
        self._replace("execution_market_pending", asdict(row))

    def resolve_market_pending(self, scope, order_id):
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM execution_market_pending WHERE account_scope=? AND order_id=?",
                (scope, order_id),
            )

    def _replace(self, table, values):
        columns = tuple(values)
        sql = (
            f"INSERT OR REPLACE INTO {table} ({','.join(columns)}) "
            f"VALUES ({','.join('?' for _ in columns)})"
        )
        with self._connection() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(sql, tuple(values[column] for column in columns))
