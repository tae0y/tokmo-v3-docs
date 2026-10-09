"""Private additive ``sec-targeted.sqlite3`` for the holdings-targeted SEC collector.

Tables: runs, attempts, filings, conflicts, target_universe, registry.

Atomic claim/overlap: a run claims a tick by inserting a row in ``runs``
before any I/O (attempt-before-I/O).  Overlapping ticks fail closed.

Filings use canonical accession identity: accession_number is the primary
key.  Exact duplicate content is skipped; immutable drift on the
same identity is a conflict and fails the run closed.
"""

import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    connect_private,
    path_identity,
    prepare_private_path,
    private_creation,
    sqlite_locked,
    table_is_strict,
)
from tokmo.values import SEC_SOURCE
from tokmo.values.sec_provenance import (
    COMBINED_PROVENANCE,
    TARGET_PROVENANCE,
    VALID_TARGET_PROVENANCE,
)

Clock = Callable[[], datetime]

DB_NAME = "sec-targeted.sqlite3"
SCHEMA_VERSION = 2

_DDL = (
    """CREATE TABLE storage_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT""",
    """CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    code          TEXT NOT NULL,
    started_count INTEGER NOT NULL,
    CHECK (length(run_id) BETWEEN 1 AND 128),
    CHECK (started_count >= 0)
) STRICT""",
    """CREATE TABLE attempts (
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    seq          INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    cik          TEXT,
    ticker       TEXT,
    started_at   TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    CHECK (seq >= 1),
    CHECK (endpoint IN ('sec_company_tickers', 'sec_submissions')),
    CHECK (cik IS NULL OR (length(cik) = 10 AND cik GLOB '[0-9]*'))
) STRICT""",
    """CREATE TABLE target_universe (
    universe_id      TEXT PRIMARY KEY,
    captured_at      TEXT NOT NULL,
    symbols          TEXT NOT NULL,
    source_snapshot  TEXT NOT NULL,
    stored_at        TEXT NOT NULL,
    CHECK (length(universe_id) BETWEEN 1 AND 128),
    CHECK (length(source_snapshot) BETWEEN 1 AND 256)
) STRICT""",
    """CREATE TABLE target_universe_members (
    universe_id TEXT NOT NULL REFERENCES target_universe(universe_id) ON DELETE RESTRICT,
    symbol      TEXT NOT NULL,
    provenance  TEXT NOT NULL,
    PRIMARY KEY (universe_id, symbol),
    CHECK (provenance IN ('HOLDING', 'WATCHLIST', 'HOLDING + WATCHLIST'))
) STRICT""",
    """CREATE TABLE registry (
    ticker       TEXT PRIMARY KEY,
    cik          TEXT NOT NULL,
    company_name TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    CHECK (length(ticker) BETWEEN 1 AND 32)
) STRICT""",
    """CREATE TABLE filings (
    accession_number  TEXT NOT NULL,
    cik                TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    company_name       TEXT NOT NULL,
    form               TEXT NOT NULL,
    filing_date        TEXT NOT NULL,
    report_date        TEXT,
    primary_document   TEXT NOT NULL,
    is_xbrl            INTEGER,
    observed_at        TEXT NOT NULL,
    stored_at          TEXT NOT NULL,
    provenance         TEXT NOT NULL,
    source             TEXT NOT NULL,
    run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    PRIMARY KEY (accession_number),
    CHECK (provenance IN ('HOLDING', 'WATCHLIST', 'HOLDING + WATCHLIST')),
    CHECK (source = 'sec_submissions')
) STRICT""",
    """CREATE TABLE conflicts (
    cik               TEXT NOT NULL,
    accession_number  TEXT NOT NULL,
    existing_form     TEXT NOT NULL,
    new_form          TEXT NOT NULL,
    detected_at       TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    PRIMARY KEY (accession_number)
) STRICT""",
    """CREATE INDEX filings_filing_date
    ON filings (filing_date DESC, cik, accession_number)""",
)
_TABLES = {
    "storage_metadata",
    "runs",
    "attempts",
    "target_universe",
    "registry",
    "filings",
    "conflicts",
    "target_universe_members",
}
_DDL_MEMBERS = """CREATE TABLE target_universe_members (
    universe_id TEXT NOT NULL REFERENCES target_universe(universe_id) ON DELETE RESTRICT,
    symbol      TEXT NOT NULL,
    provenance  TEXT NOT NULL,
    PRIMARY KEY (universe_id, symbol),
    CHECK (provenance IN ('HOLDING', 'WATCHLIST', 'HOLDING + WATCHLIST'))
) STRICT"""
_FILINGS_NEXT_DDL = """CREATE TABLE filings_next (
    accession_number  TEXT NOT NULL,
    cik                TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    company_name       TEXT NOT NULL,
    form               TEXT NOT NULL,
    filing_date        TEXT NOT NULL,
    report_date        TEXT,
    primary_document   TEXT NOT NULL,
    is_xbrl            INTEGER,
    observed_at        TEXT NOT NULL,
    stored_at          TEXT NOT NULL,
    provenance         TEXT NOT NULL,
    source             TEXT NOT NULL,
    run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    PRIMARY KEY (accession_number),
    CHECK (provenance IN ('HOLDING', 'WATCHLIST', 'HOLDING + WATCHLIST')),
    CHECK (source = 'sec_submissions')
) STRICT"""
_METADATA = (
    ("sec_targeted_schema_version", str(SCHEMA_VERSION)),
    ("sec_targeted_adapter", "tokmo-sec-targeted-next"),
)
_EXPECTED_TABLES = sorted(_TABLES)


# ---------------------------------------------------------------------------
# Provenance widening helpers
# ---------------------------------------------------------------------------


def _widen_provenance(current: str, incoming: str) -> str | None:
    """Return the monotonic widening of ``current`` with ``incoming``.

    Rules:
    - identical → return current (no change)
    - either is COMBINED → COMBINED (already max)
    - current != incoming, both valid → COMBINED
    - invalid inputs → None (caller must fail closed)

    Never narrows.  COMBINED is the terminal state.
    """
    if current == incoming:
        return current
    if current == COMBINED_PROVENANCE or incoming == COMBINED_PROVENANCE:
        return COMBINED_PROVENANCE
    if current in VALID_TARGET_PROVENANCE and incoming in VALID_TARGET_PROVENANCE:
        return COMBINED_PROVENANCE
    return None


@dataclass(frozen=True)
class RunClaimResult:
    status: str  # "claimed", "overlap"
    run_id: str = field(repr=False)


@dataclass(frozen=True)
class FilingSaveResult:
    status: str  # "saved", "duplicate", "conflict"
    accession_number: str
    cik: str


@dataclass(frozen=True)
class UniverseStoreResult:
    status: str  # "saved", "unchanged"
    universe_id: str


class SecTargetedStoreError(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _canonical_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def _new_connection(path: Path) -> sqlite3.Connection:
    """Create or open a private database with full schema validation."""
    safety = prepare_private_path(path)
    if safety.unsafe:
        raise SecTargetedStoreError("store_write_failed")
    exists = os.path.lexists(path)
    connection = None
    try:
        if not exists:
            with private_creation():
                connection = connect_private(path)
                os.chmod(path, 0o600)
        else:
            connection = connect_private(path, mode="rwc")
        if not exists:
            _initialize(connection)
        else:
            _maybe_migrate_v1(connection)
        _validate(connection)
        return connection
    except SecTargetedStoreError:
        if connection is not None:
            try:
                connection.close()
            except (sqlite3.Error, OSError):
                pass
        raise
    except sqlite3.Error as error:
        if connection is not None:
            try:
                connection.close()
            except (sqlite3.Error, OSError):
                pass
        reason = "store_locked" if sqlite_locked(error) else "store_corrupt"
        raise SecTargetedStoreError(reason) from error
    except OSError:
        if connection is not None:
            try:
                connection.close()
            except (sqlite3.Error, OSError):
                pass
        raise SecTargetedStoreError("store_corrupt") from None


def _initialize(connection: sqlite3.Connection) -> None:
    acquire_immediate_transaction(connection)
    try:
        for ddl in _DDL:
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO storage_metadata(key, value) VALUES (?, ?)",
            sorted(_METADATA),
        )
        connection.execute("COMMIT")
    except BaseException:
        try:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
        except (sqlite3.Error, OSError):
            pass
        raise


def _maybe_migrate_v1(connection: sqlite3.Connection) -> None:
    """Migrate schema 1 → 2: per-symbol provenance + widened filing provenance.

    Two additive changes:
    1. ``target_universe_members`` table backfilled from ``target_universe``
       symbols (all rows get provenance='HOLDING').
    2. ``filings`` table CHECK constraint widened from ``provenance='HOLDING'``
       to the three-way enum.  SQLite cannot ALTER CHECK, so the table is
       rebuilt via create-copy-drop-rename inside the same transaction.

    The version is re-checked *inside* ``BEGIN IMMEDIATE`` so that two
    concurrent openers do not both migrate: the loser sees the updated
    version and returns without touching the schema.
    """
    try:
        meta = dict(
            connection.execute("SELECT key, value FROM storage_metadata").fetchall()
        )
    except sqlite3.Error:
        return
    if meta.get("sec_targeted_schema_version") != "1":
        return
    acquire_immediate_transaction(connection)
    try:
        # Re-check version inside the write transaction to prevent
        # a concurrent opener from double-migrating.
        row = connection.execute(
            "SELECT value FROM storage_metadata WHERE key='sec_targeted_schema_version'"
        ).fetchone()
        if row is None or row[0] != "1":
            connection.execute("ROLLBACK")
            return

        # 1. Create target_universe_members (by name, not positional DDL)
        existing_tables = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "target_universe_members" not in existing_tables:
            connection.execute(_DDL_MEMBERS)

        # Backfill from target_universe symbols
        rows = connection.execute(
            "SELECT universe_id, symbols FROM target_universe"
        ).fetchall()
        for universe_id, symbols_json in rows:
            try:
                symbols = json.loads(symbols_json)
            except (json.JSONDecodeError, TypeError):
                raise SecTargetedStoreError("schema_mismatch")
            if not isinstance(symbols, list) or not all(
                isinstance(symbol, str) and symbol for symbol in symbols
            ):
                raise SecTargetedStoreError("schema_mismatch")
            # Only backfill rows not already present (idempotent)
            existing_members = {
                r[0]
                for r in connection.execute(
                    "SELECT symbol FROM target_universe_members WHERE universe_id=?",
                    (universe_id,),
                )
            }
            to_insert = [
                (universe_id, symbol, TARGET_PROVENANCE)
                for symbol in symbols
                if symbol not in existing_members
            ]
            if to_insert:
                connection.executemany(
                    "INSERT INTO target_universe_members"
                    "(universe_id, symbol, provenance) VALUES (?, ?, ?)",
                    to_insert,
                )

        # 2. Rebuild filings table with widened provenance CHECK
        existing_tables = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if "filings" in existing_tables:
            connection.execute(_FILINGS_NEXT_DDL)
            connection.execute(
                "INSERT INTO filings_next "
                "SELECT accession_number, cik, ticker, company_name, "
                "form, filing_date, report_date, primary_document, "
                "is_xbrl, observed_at, stored_at, provenance, source, "
                "run_id FROM filings"
            )
            connection.execute("DROP TABLE filings")
            connection.execute("ALTER TABLE filings_next RENAME TO filings")

        # Recreate filings index by name
        existing_indexes = {
            r[0]
            for r in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
        if "filings_filing_date" not in existing_indexes:
            connection.execute(
                "CREATE INDEX filings_filing_date"
                " ON filings (filing_date DESC, cik, accession_number)"
            )

        connection.execute(
            "UPDATE storage_metadata SET value=? WHERE key=?",
            (str(SCHEMA_VERSION), "sec_targeted_schema_version"),
        )
        connection.execute(
            "UPDATE storage_metadata SET value=? WHERE key=?",
            ("tokmo-sec-targeted-next", "sec_targeted_adapter"),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise


def _validate(connection: sqlite3.Connection) -> None:
    """Full schema and integrity validation on every open."""
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise SecTargetedStoreError("store_corrupt")
    tables = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )
    }
    if sorted(tables) != _EXPECTED_TABLES:
        raise SecTargetedStoreError("schema_mismatch")
    for table in _EXPECTED_TABLES:
        if not table_is_strict(connection, table):
            raise SecTargetedStoreError("schema_mismatch")
    meta = dict(
        connection.execute("SELECT key, value FROM storage_metadata").fetchall()
    )
    if meta != dict(_METADATA):
        raise SecTargetedStoreError("schema_mismatch")
    if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
        raise SecTargetedStoreError("store_corrupt")
    if connection.execute("PRAGMA foreign_key_check").fetchone():
        raise SecTargetedStoreError("store_corrupt")


class SecTargetedStore:
    """Additive-only filing store with atomic claim/overlap."""

    def __init__(self, path: Path, *, clock: Clock) -> None:
        self._path = Path(path)
        self._clock = clock

    # --- run claim / overlap ---

    def claim_run(
        self,
        *,
        run_id: str,
        started_at: datetime,
    ) -> RunClaimResult:
        """Insert a run row before any I/O.  Returns overlap on PK collision."""
        connection = _new_connection(self._path)
        tx_identity = path_identity(self._path)
        try:
            acquire_immediate_transaction(connection)
            existing = connection.execute(
                "SELECT 1 FROM runs WHERE run_id=?", (run_id,)
            ).fetchone()
            if existing is not None:
                connection.execute("ROLLBACK")
                return RunClaimResult("overlap", run_id)
            connection.execute(
                "INSERT INTO runs(run_id, started_at, code, started_count) "
                "VALUES (?, ?, 'started', 0)",
                (run_id, _canonical_utc(started_at)),
            )
            if path_identity(self._path) != tx_identity:
                connection.execute("ROLLBACK")
                raise OSError("unsafe_path_swap")
            connection.execute("COMMIT")
            return RunClaimResult("claimed", run_id)
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            if sqlite_locked(error):
                raise SecTargetedStoreError("store_locked") from error
            raise SecTargetedStoreError("store_write_failed") from error
        except OSError:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_corrupt") from None
        finally:
            connection.close()

    def complete_run(
        self,
        *,
        run_id: str,
        code: str,
        started_count: int,
        started_at: datetime,
    ) -> None:
        connection = _new_connection(self._path)
        try:
            acquire_immediate_transaction(connection)
            connection.execute(
                "UPDATE runs SET code=?, started_count=? WHERE run_id=?",
                (code, started_count, run_id),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_write_failed") from error
        finally:
            connection.close()

    # --- attempts ---

    def record_attempt(
        self,
        *,
        run_id: str,
        seq: int,
        endpoint: str,
        cik: str | None,
        ticker: str | None,
        started_at: datetime,
        outcome_code: str,
    ) -> None:
        connection = _new_connection(self._path)
        try:
            acquire_immediate_transaction(connection)
            connection.execute(
                "INSERT INTO attempts(run_id, seq, endpoint, cik, ticker, "
                "started_at, outcome_code) VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(run_id, seq) DO UPDATE SET "
                "outcome_code=excluded.outcome_code",
                (
                    run_id,
                    seq,
                    endpoint,
                    cik,
                    ticker,
                    _canonical_utc(started_at),
                    outcome_code,
                ),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_write_failed") from error
        finally:
            connection.close()

    # --- target universe ---

    def store_universe(
        self,
        *,
        universe_id: str,
        captured_at: datetime,
        symbols: frozenset[str],
        source_snapshot: str,
        provenance: dict[str, str] | None = None,
    ) -> UniverseStoreResult:
        """Persist target universe provenance without account details.

        If a universe with the same id already exists:
        - Same symbols JSON: widen member provenance monotonically
          (HOLDING/WATCHLIST → COMBINED) inside the same transaction.
          If provenance is identical, returns ``unchanged``.
        - Different symbols JSON: fail closed with ``conflict``.

        Provenance values are validated *before* any SQL.  Invalid values
        fail truthfully with no partial write.
        """
        # Validate provenance before opening a connection or SQL.
        members = (
            {symbol: TARGET_PROVENANCE for symbol in symbols}
            if provenance is None
            else provenance
        )
        if set(members) != set(symbols):
            raise SecTargetedStoreError("provenance_key_mismatch")
        for symbol, prov in members.items():
            if prov not in VALID_TARGET_PROVENANCE:
                raise SecTargetedStoreError("invalid_provenance")

        connection = _new_connection(self._path)
        now = _canonical_utc(self._clock())
        try:
            acquire_immediate_transaction(connection)
            existing = connection.execute(
                "SELECT symbols FROM target_universe WHERE universe_id=?",
                (universe_id,),
            ).fetchone()
            syms_json = json.dumps(sorted(symbols))
            if existing is not None:
                if existing[0] != syms_json:
                    # Symbols changed for same id: conflict, no partial write
                    connection.execute("ROLLBACK")
                    raise SecTargetedStoreError("conflict")
                # Same symbols: widen provenance monotonically.
                # Load existing member provenance and compare.
                existing_members = {
                    r[0]: r[1]
                    for r in connection.execute(
                        "SELECT symbol, provenance FROM "
                        "target_universe_members WHERE universe_id=?",
                        (universe_id,),
                    )
                }
                any_change = False
                for symbol in sorted(symbols):
                    current = existing_members.get(symbol)
                    incoming = members[symbol]
                    if current is None:
                        # New member row (shouldn't happen if symbols match,
                        # but be defensive)
                        connection.execute(
                            "INSERT INTO target_universe_members"
                            "(universe_id, symbol, provenance) "
                            "VALUES (?, ?, ?)",
                            (universe_id, symbol, incoming),
                        )
                        any_change = True
                    else:
                        widened = _widen_provenance(current, incoming)
                        if widened is None:
                            # Invalid narrowing or bad value: fail closed
                            connection.execute("ROLLBACK")
                            raise SecTargetedStoreError("provenance_narrowing_conflict")
                        if widened != current:
                            connection.execute(
                                "UPDATE target_universe_members "
                                "SET provenance=? "
                                "WHERE universe_id=? AND symbol=?",
                                (widened, universe_id, symbol),
                            )
                            any_change = True
                connection.execute("COMMIT")
                if any_change:
                    return UniverseStoreResult("saved", universe_id)
                return UniverseStoreResult("unchanged", universe_id)
            connection.execute(
                "INSERT INTO target_universe(universe_id, captured_at, symbols, "
                "source_snapshot, stored_at) VALUES (?, ?, ?, ?, ?)",
                (
                    universe_id,
                    _canonical_utc(captured_at),
                    syms_json,
                    source_snapshot,
                    now,
                ),
            )
            connection.executemany(
                "INSERT INTO target_universe_members"
                "(universe_id, symbol, provenance) VALUES (?, ?, ?)",
                ((universe_id, symbol, members[symbol]) for symbol in sorted(symbols)),
            )
            connection.execute("COMMIT")
            return UniverseStoreResult("saved", universe_id)
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_write_failed") from error
        finally:
            connection.close()

    def load_last_universe(self) -> tuple[str, ...] | None:
        """Load the most recently stored universe symbols (oldest key order)."""
        if not os.path.lexists(self._path):
            return None
        try:
            connection = _new_connection(self._path)
        except SecTargetedStoreError:
            return None
        try:
            row = connection.execute(
                "SELECT symbols FROM target_universe "
                "ORDER BY stored_at DESC, universe_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            return tuple(json.loads(row[0]))
        except (sqlite3.Error, json.JSONDecodeError):
            return None
        finally:
            connection.close()

    def load_last_universe_with_provenance(
        self,
    ) -> tuple[frozenset[str], dict[str, str]] | None:
        """Load most recent universe symbols with per-symbol provenance.

        Returns ``(symbols, {symbol: provenance})`` or ``None``.
        Preserves last union provenance for fallback paths.
        """
        if not os.path.lexists(self._path):
            return None
        try:
            connection = _new_connection(self._path)
        except SecTargetedStoreError:
            return None
        try:
            row = connection.execute(
                "SELECT universe_id, symbols FROM target_universe "
                "ORDER BY stored_at DESC, universe_id DESC LIMIT 1"
            ).fetchone()
            if row is None:
                return None
            universe_id = row[0]
            symbols = frozenset(json.loads(row[1]))
            prov_rows = connection.execute(
                "SELECT symbol, provenance FROM target_universe_members "
                "WHERE universe_id=?",
                (universe_id,),
            ).fetchall()
            provenance = {r[0]: r[1] for r in prov_rows}
            return symbols, provenance
        except (sqlite3.Error, json.JSONDecodeError):
            return None
        finally:
            connection.close()

    # --- registry ---

    def store_registry(
        self,
        *,
        ticker: str,
        cik: str,
        company_name: str,
        refreshed_at: datetime,
    ) -> None:
        connection = _new_connection(self._path)
        try:
            acquire_immediate_transaction(connection)
            connection.execute(
                "INSERT INTO registry(ticker, cik, company_name, refreshed_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(ticker) DO UPDATE SET "
                "cik=excluded.cik, company_name=excluded.company_name, "
                "refreshed_at=excluded.refreshed_at",
                (ticker, cik, company_name, _canonical_utc(refreshed_at)),
            )
            connection.execute("COMMIT")
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_write_failed") from error
        finally:
            connection.close()

    def load_registry(self) -> dict[str, tuple[str, str, str]]:
        """Return {ticker: (cik, company_name, refreshed_at)} from the cache."""
        if not os.path.lexists(self._path):
            return {}
        try:
            connection = _new_connection(self._path)
        except SecTargetedStoreError:
            return {}
        try:
            rows = connection.execute(
                "SELECT ticker, cik, company_name, refreshed_at FROM registry"
            ).fetchall()
            return {row[0]: (row[1], row[2], row[3]) for row in rows}
        except sqlite3.Error:
            return {}
        finally:
            connection.close()

    # --- filings ---

    def save_filing(
        self,
        *,
        filing,
        run_id: str,
        stored_at: datetime,
        provenance: str = "HOLDING",
    ) -> FilingSaveResult:
        """Insert one filing.  Returns duplicate/conflict on identity hit.

        On duplicate identical accession, provenance is widened monotonically
        (HOLDING|WATCHLIST → COMBINED).  Payload conflicts still fail closed.

        Provenance is validated *before* any SQL.
        """
        if provenance not in VALID_TARGET_PROVENANCE:
            raise SecTargetedStoreError("invalid_provenance")
        connection = _new_connection(self._path)
        try:
            acquire_immediate_transaction(connection)
            existing = connection.execute(
                "SELECT form, filing_date, report_date, primary_document, "
                "is_xbrl, ticker, company_name, provenance "
                "FROM filings WHERE accession_number=?",
                (filing.accession_number,),
            ).fetchone()
            if existing is not None:
                new_tuple = (
                    filing.form,
                    filing.filing_date.isoformat(),
                    (filing.report_date.isoformat() if filing.report_date else None),
                    filing.primary_document,
                    filing.is_xbrl,
                )
                if tuple(existing[:5]) == new_tuple:
                    # Monotonic provenance widening on identical payload
                    current_prov = existing[7]
                    if current_prov != provenance:
                        widened = _widen_provenance(current_prov, provenance)
                        if widened is not None and widened != current_prov:
                            connection.execute(
                                "UPDATE filings SET provenance=? "
                                "WHERE accession_number=?",
                                (widened, filing.accession_number),
                            )
                    connection.execute("COMMIT")
                    return FilingSaveResult(
                        "duplicate", filing.accession_number, filing.cik
                    )
                connection.execute(
                    "INSERT INTO conflicts(cik, accession_number, "
                    "existing_form, new_form, detected_at, run_id) "
                    "VALUES (?, ?, ?, ?, ?, ?) "
                    "ON CONFLICT(accession_number) DO UPDATE SET "
                    "cik=excluded.cik, existing_form=excluded.existing_form, "
                    "new_form=excluded.new_form, "
                    "detected_at=excluded.detected_at, run_id=excluded.run_id",
                    (
                        filing.cik,
                        filing.accession_number,
                        existing[0],
                        filing.form,
                        _canonical_utc(stored_at),
                        run_id,
                    ),
                )
                connection.execute("COMMIT")
                return FilingSaveResult("conflict", filing.accession_number, filing.cik)
            connection.execute(
                "INSERT INTO filings(cik, accession_number, ticker, "
                "company_name, form, filing_date, report_date, "
                "primary_document, is_xbrl, observed_at, stored_at, "
                "provenance, source, run_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    filing.cik,
                    filing.accession_number,
                    filing.ticker,
                    filing.company_name,
                    filing.form,
                    filing.filing_date.isoformat(),
                    filing.report_date.isoformat() if filing.report_date else None,
                    filing.primary_document,
                    None if filing.is_xbrl is None else int(filing.is_xbrl),
                    _canonical_utc(filing.observed_at),
                    _canonical_utc(stored_at),
                    provenance,
                    SEC_SOURCE,
                    run_id,
                ),
            )
            connection.execute("COMMIT")
            return FilingSaveResult("saved", filing.accession_number, filing.cik)
        except sqlite3.Error as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise SecTargetedStoreError("store_write_failed") from error
        finally:
            connection.close()

    def filing_count(self) -> int:
        if not os.path.lexists(self._path):
            return 0
        try:
            connection = _new_connection(self._path)
        except SecTargetedStoreError:
            return 0
        try:
            return connection.execute("SELECT COUNT(*) FROM filings").fetchone()[0]
        except sqlite3.Error:
            return 0
        finally:
            connection.close()
