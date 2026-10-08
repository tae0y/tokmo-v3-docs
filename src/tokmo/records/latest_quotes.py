"""Private SQLite latest-state storage for typed current-price facts."""

from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
import os
from pathlib import Path
import re
import sqlite3
import stat
import threading

from tokmo.values import TossCurrentPriceFact, canonical_decimal


LATEST_QUOTE_SCHEMA_VERSION = 1
MIN_SQLITE_VERSION = (3, 37, 0)
BUSY_TIMEOUT_MS = 1000

_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_PRICE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")
_UMASK_LOCK = threading.Lock()

_LATEST_COLUMNS = (
    ("source", "TEXT", 1, 1),
    ("symbol", "TEXT", 1, 2),
    ("last_price", "TEXT", 1, 0),
    ("currency_raw", "TEXT", 1, 0),
    ("provider_timestamp", "TEXT", 0, 0),
    ("fetched_at", "TEXT", 1, 0),
    ("stored_at", "TEXT", 1, 0),
    ("schema_version", "INTEGER", 1, 0),
)
_METADATA_COLUMNS = (
    ("key", "TEXT", 1, 1),
    ("value", "TEXT", 1, 0),
)

_CREATE_LATEST = """
CREATE TABLE latest_quotes (
    source TEXT NOT NULL,
    symbol TEXT NOT NULL,
    last_price TEXT NOT NULL,
    currency_raw TEXT NOT NULL,
    provider_timestamp TEXT,
    fetched_at TEXT NOT NULL,
    stored_at TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    PRIMARY KEY (source, symbol),
    CHECK (length(source) BETWEEN 1 AND 128),
    CHECK (length(symbol) BETWEEN 1 AND 64),
    CHECK (length(last_price) BETWEEN 1 AND 30),
    CHECK (length(currency_raw) BETWEEN 1 AND 64),
    CHECK (schema_version = 1)
) STRICT
"""
_CREATE_METADATA = """
CREATE TABLE storage_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT
"""
_UPSERT = """
INSERT INTO latest_quotes (
    source, symbol, last_price, currency_raw, provider_timestamp,
    fetched_at, stored_at, schema_version
) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(source, symbol) DO UPDATE SET
    last_price=excluded.last_price,
    currency_raw=excluded.currency_raw,
    provider_timestamp=excluded.provider_timestamp,
    fetched_at=excluded.fetched_at,
    stored_at=excluded.stored_at,
    schema_version=excluded.schema_version
"""


@dataclass(frozen=True)
class LatestQuoteSaveReceipt:
    status: str
    inserted_count: int
    updated_count: int
    reason: str = field(repr=False)
    stored_at: datetime = field(repr=False)


class LatestQuoteStoreReadError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _aware(value: object) -> bool:
    return (
        isinstance(value, datetime)
        and value.tzinfo is not None
        and value.utcoffset() is not None
    )


def _valid_price(value: object) -> bool:
    if not isinstance(value, str) or not 1 <= len(value) <= 30:
        return False
    if _PRICE_RE.fullmatch(value) is None:
        return False
    try:
        number = Decimal(value)
        return (
            number.is_finite()
            and number >= 0
            and canonical_decimal(number) == value
        )
    except (InvalidOperation, ValueError):
        return False


def _valid_fact(fact: object) -> bool:
    return (
        isinstance(fact, TossCurrentPriceFact)
        and isinstance(fact.source, str)
        and 1 <= len(fact.source) <= 128
        and isinstance(fact.symbol, str)
        and 1 <= len(fact.symbol) <= 64
        and _SYMBOL_RE.fullmatch(fact.symbol) is not None
        and _valid_price(fact.last_price)
        and isinstance(fact.currency_raw, str)
        and 1 <= len(fact.currency_raw) <= 64
        and (fact.timestamp is None or _aware(fact.timestamp))
        and _aware(fact.fetched_at)
    )


def _valid_batch(facts: object) -> bool:
    if type(facts) is not tuple or not 1 <= len(facts) <= 200:
        return False
    if not all(_valid_fact(fact) for fact in facts):
        return False
    sources = {fact.source for fact in facts}
    symbols = [fact.symbol for fact in facts]
    return len(sources) == 1 and len(set(symbols)) == len(symbols)


def _failure(reason: str, stored_at: datetime) -> LatestQuoteSaveReceipt:
    return LatestQuoteSaveReceipt("FAILED", 0, 0, reason, stored_at)


def _sqlite_locked(error: sqlite3.Error) -> bool:
    return getattr(error, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }


@contextmanager
def _private_creation():
    with _UMASK_LOCK:
        prior = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(prior)


class LatestQuoteStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime]) -> None:
        self._path = path
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._path, timeout=1.0, isolation_level=None
        )
        try:
            connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
            mode = connection.execute("PRAGMA journal_mode=DELETE").fetchone()
            connection.execute("PRAGMA synchronous=FULL")
            synchronous = connection.execute("PRAGMA synchronous").fetchone()
            if mode != ("delete",) or synchronous != (2,):
                raise OSError
            return connection
        except BaseException:
            connection.close()
            raise

    def _prepare_path(self) -> bool:
        parent = self._path.parent
        if parent.exists():
            if not parent.is_dir():
                return False
            if stat.S_IMODE(parent.stat().st_mode) & 0o077:
                return False
        else:
            with _private_creation():
                parent.mkdir(mode=0o700, parents=True)
            if stat.S_IMODE(parent.stat().st_mode) != 0o700:
                return False
        if self._path.exists() and not self._path.is_file():
            return False
        return True

    @staticmethod
    def _columns(connection: sqlite3.Connection, table: str):
        return tuple(
            (row[1], row[2], row[3], row[5])
            for row in connection.execute(f"PRAGMA table_info({table})")
        )

    def _schema_metadata_valid(self, connection: sqlite3.Connection) -> bool:
        tables = {
            row[1]: row for row in connection.execute("PRAGMA table_list")
        }
        if (
            tables.get("latest_quotes", (None,) * 6)[5] != 1
            or tables.get("storage_metadata", (None,) * 6)[5] != 1
        ):
            return False
        if self._columns(connection, "latest_quotes") != _LATEST_COLUMNS:
            return False
        if self._columns(connection, "storage_metadata") != _METADATA_COLUMNS:
            return False
        if connection.execute(
            "SELECT key, value FROM storage_metadata"
        ).fetchall() != [("latest_quote_schema_version", "1")]:
            return False
        return True

    def _schema_constraints_valid(
        self, connection: sqlite3.Connection
    ) -> bool:
        connection.execute("SAVEPOINT validate_latest_quote_schema")
        valid = True
        baseline = (
            "s",
            "A",
            "1",
            "c",
            None,
            "2026-01-01T00:00:00+00:00",
            "2026-01-01T00:00:00+00:00",
            1,
        )
        try:
            connection.execute("DELETE FROM latest_quotes")
            connection.execute(
                "INSERT INTO latest_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                baseline,
            )
            invalid_rows = (
                baseline,
                ("", "B", *baseline[2:]),
                ("s" * 129, "B", *baseline[2:]),
                ("s", "", *baseline[2:]),
                ("s", "B" * 65, *baseline[2:]),
                ("s", "B", "", *baseline[3:]),
                ("s", "B", "1" * 31, *baseline[3:]),
                ("s", "B", "1", "", *baseline[4:]),
                ("s", "B", "1", "c" * 65, *baseline[4:]),
                (*baseline[:-1], 2),
            )
            for row in invalid_rows:
                try:
                    connection.execute(
                        "INSERT INTO latest_quotes VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        row,
                    )
                except sqlite3.IntegrityError:
                    continue
                valid = False
                break
        finally:
            connection.execute("ROLLBACK TO validate_latest_quote_schema")
            connection.execute("RELEASE validate_latest_quote_schema")
        return valid

    def _initialize(self, connection: sqlite3.Connection) -> None:
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(_CREATE_LATEST)
            connection.execute(_CREATE_METADATA)
            connection.execute(
                "INSERT INTO storage_metadata(key, value) VALUES (?, ?)",
                ("latest_quote_schema_version", "1"),
            )
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def save_batch(
        self, facts: tuple[TossCurrentPriceFact, ...]
    ) -> LatestQuoteSaveReceipt:
        try:
            stored_at = self._clock()
        except BaseException:
            stored_at = datetime.min.replace(tzinfo=timezone.utc)
            return _failure("invalid_batch", stored_at)
        if not _aware(stored_at) or not _valid_batch(facts):
            return _failure("invalid_batch", stored_at)
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            return _failure("schema_mismatch", stored_at)
        try:
            if not self._prepare_path():
                return _failure("store_write_failed", stored_at)
            existed = self._path.exists()
            was_empty = existed and self._path.stat().st_size == 0
            if not existed:
                with _private_creation():
                    connection = self._connect()
            else:
                connection = self._connect()
            try:
                if not existed or was_empty:
                    self._initialize(connection)
                    if hasattr(os, "getuid") and self._path.stat().st_uid == os.getuid():
                        os.chmod(self._path, 0o600)
                connection.execute("BEGIN IMMEDIATE")
                try:
                    if (
                        existed
                        and not was_empty
                        and (
                            not self._schema_metadata_valid(connection)
                            or not self._schema_constraints_valid(connection)
                        )
                    ):
                        connection.execute("ROLLBACK")
                        return _failure("schema_mismatch", stored_at)
                    source = facts[0].source
                    placeholders = ",".join("?" for _ in facts)
                    existing = {
                        row[0] for row in connection.execute(
                            f"SELECT symbol FROM latest_quotes "
                            f"WHERE source=? AND symbol IN ({placeholders})",
                            (source, *(fact.symbol for fact in facts)),
                        )
                    }
                    rows = [
                        (
                            fact.source,
                            fact.symbol,
                            fact.last_price,
                            fact.currency_raw,
                            None if fact.timestamp is None
                            else fact.timestamp.isoformat(),
                            fact.fetched_at.isoformat(),
                            stored_at.isoformat(),
                            LATEST_QUOTE_SCHEMA_VERSION,
                        )
                        for fact in facts
                    ]
                    connection.executemany(_UPSERT, rows)
                    connection.execute("COMMIT")
                except BaseException:
                    connection.execute("ROLLBACK")
                    raise
            finally:
                connection.close()
            return LatestQuoteSaveReceipt(
                "SAVED", len(facts) - len(existing), len(existing), "saved", stored_at
            )
        except sqlite3.DatabaseError as error:
            if _sqlite_locked(error):
                reason = "store_locked"
            elif getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_NOTADB:
                reason = "store_corrupt"
            else:
                reason = "store_write_failed"
            return _failure(reason, stored_at)
        except (OSError, ValueError):
            return _failure("store_write_failed", stored_at)

    def read_latest(
        self, *, source: str, symbols: tuple[str, ...]
    ) -> tuple[TossCurrentPriceFact, ...]:
        if (
            not isinstance(source, str)
            or not 1 <= len(source) <= 128
            or type(symbols) is not tuple
            or not 1 <= len(symbols) <= 200
            or not all(
                isinstance(symbol, str)
                and 1 <= len(symbol) <= 64
                and _SYMBOL_RE.fullmatch(symbol) is not None
                for symbol in symbols
            )
            or len(set(symbols)) != len(symbols)
        ):
            raise LatestQuoteStoreReadError("store_read_failed") from None
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise LatestQuoteStoreReadError("schema_mismatch") from None
        try:
            if (
                not self._path.exists()
                or not self._path.is_file()
                or self._path.stat().st_size == 0
            ):
                raise LatestQuoteStoreReadError("store_read_failed")
            connection = self._connect()
            try:
                if not self._schema_metadata_valid(connection):
                    raise LatestQuoteStoreReadError("schema_mismatch")
                placeholders = ",".join("?" for _ in symbols)
                rows = connection.execute(
                    "SELECT source, symbol, last_price, currency_raw, "
                    "provider_timestamp, fetched_at, schema_version "
                    f"FROM latest_quotes WHERE source=? "
                    f"AND symbol IN ({placeholders})",
                    (source, *symbols),
                ).fetchall()
            finally:
                connection.close()
            if len(rows) != len(symbols):
                raise LatestQuoteStoreReadError("missing_latest_quote")
            by_symbol = {}
            for row in rows:
                try:
                    (
                        stored_source,
                        symbol,
                        price,
                        currency,
                        timestamp_text,
                        fetched_text,
                        version,
                    ) = row
                    timestamp = (
                        None
                        if timestamp_text is None
                        else datetime.fromisoformat(timestamp_text)
                    )
                    fetched_at = datetime.fromisoformat(fetched_text)
                    fact = TossCurrentPriceFact(
                        source=stored_source,
                        symbol=symbol,
                        last_price=price,
                        currency_raw=currency,
                        timestamp=timestamp,
                        fetched_at=fetched_at,
                    )
                except (TypeError, ValueError):
                    raise LatestQuoteStoreReadError("store_corrupt") from None
                if (
                    version != LATEST_QUOTE_SCHEMA_VERSION
                    or stored_source != source
                    or symbol not in symbols
                    or symbol in by_symbol
                    or not _valid_fact(fact)
                ):
                    raise LatestQuoteStoreReadError("store_corrupt") from None
                by_symbol[symbol] = fact
            return tuple(by_symbol[symbol] for symbol in symbols)
        except LatestQuoteStoreReadError:
            raise
        except sqlite3.DatabaseError as error:
            reason = (
                "store_locked"
                if _sqlite_locked(error)
                else "store_corrupt"
                if getattr(error, "sqlite_errorcode", None) == sqlite3.SQLITE_NOTADB
                else "store_read_failed"
            )
            raise LatestQuoteStoreReadError(reason) from None

    def read_latest_with_stored_at(self, *, source: str,
                                   symbols: tuple[str, ...]) -> tuple[dict, ...]:
        """Consumer view preserving provider, fetch, and storage instants."""
        facts = self.read_latest(source=source, symbols=symbols)
        try:
            placeholders = ",".join("?" for _ in symbols)
            with self._connect() as connection:
                rows = connection.execute(
                    "SELECT symbol, stored_at FROM latest_quotes WHERE source=? "
                    f"AND symbol IN ({placeholders})", (source, *symbols),
                ).fetchall()
            stored = {row[0]: row[1] for row in rows}
            if set(stored) != set(symbols):
                raise LatestQuoteStoreReadError("missing_latest_quote")
            return tuple({
                "symbol": fact.symbol,
                "last_price": fact.last_price,
                "currency_raw": fact.currency_raw,
                "provider_timestamp": (None if fact.timestamp is None
                                       else fact.timestamp.isoformat()),
                "fetched_at": fact.fetched_at.isoformat(),
                "stored_at": stored[fact.symbol],
            } for fact in facts)
        except LatestQuoteStoreReadError:
            raise
        except sqlite3.DatabaseError as error:
            reason = "store_locked" if _sqlite_locked(error) else "store_read_failed"
            raise LatestQuoteStoreReadError(reason) from None
        except (OSError, TypeError, ValueError):
            raise LatestQuoteStoreReadError("store_read_failed") from None
