"""Private pension revision registry — operational state for pension holdings.

This store tracks the lifecycle of pension holdings revisions:
CANDIDATE_VALID → PENDING_REVIEW → ACTIVE → SUPERSEDED, with rollback support.

It is NON-CANONICAL operational state. The canonical holdings live in the
holdings_snapshots store. This registry stores revision metadata plus a
canonical-encoded copy of the holdings tuple (``holdings_json``) so that
``read_active_revision`` can reconstruct the full ``PensionHoldingsRevisionV1``
without a join to the canonical store.

Tables (all STRICT):
    - pension_revisions: one row per revision
    - pension_activation_events: append-only event log
    - pension_active_pointer: singleton pointing at the current ACTIVE revision
    - pension_reminder_events: dedup reminders for aged-source alerts

Schema version: 1 (PRAGMA user_version=1).
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    connect_private,
    path_identity,
    prepare_private_path,
    private_creation,
    table_is_strict,
)
from tokmo.records.durability import fsync_directory
from tokmo.values.pension_holdings import (
    PENSION_HOLDINGS_SCHEMA_VERSION,
    PENSION_PARSER_VERSION,
    PensionHoldingsRevisionV1,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.material_pack import canonical_encode, canonical_decode

Clock = Callable[[], datetime]

SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# DDL — the SHA-256 of the *stripped* DDL statements is computed at module
# load and asserted inside _validate on every open.
# ---------------------------------------------------------------------------

_DDL_STATEMENTS: list[str] = [
    """CREATE TABLE pension_revisions (
    revision_id              TEXT PRIMARY KEY CHECK(length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'),
    schema_version           INTEGER NOT NULL CHECK(schema_version = 1),
    source_content_sha256    TEXT NOT NULL CHECK(length(source_content_sha256) = 64),
    parser_version           TEXT NOT NULL,
    sheet_schema_version     INTEGER NOT NULL,
    semantic_digest          TEXT NOT NULL CHECK(length(semantic_digest) = 64),
    reconciliation_digest    TEXT NOT NULL CHECK(length(reconciliation_digest) = 64),
    holding_count            INTEGER NOT NULL CHECK(holding_count >= 0),
    liquidation_flag         INTEGER NOT NULL DEFAULT 0 CHECK(liquidation_flag IN (0, 1)),
    source_as_of             TEXT NOT NULL,
    uploaded_at              TEXT NOT NULL,
    validated_at             TEXT NOT NULL,
    approved_at              TEXT,
    approval_reason          TEXT,
    state                    TEXT NOT NULL CHECK(state IN ('CANDIDATE_VALID', 'PENDING_REVIEW', 'ACTIVE', 'REJECTED', 'SUPERSEDED')),
    created_at               TEXT NOT NULL,
    holdings_json            TEXT NOT NULL,
    previous_active_revision_id TEXT REFERENCES pension_revisions(revision_id)
) STRICT""",

    """CREATE TABLE pension_activation_events (
    seq                      INTEGER PRIMARY KEY AUTOINCREMENT,
    revision_id              TEXT NOT NULL REFERENCES pension_revisions(revision_id),
    event_type               TEXT NOT NULL CHECK(event_type IN ('CANDIDATE_INSERTED', 'PENDING_REVIEW', 'ACTIVATED', 'REJECTED', 'SUPERSEDED', 'ROLLED_BACK')),
    activation_generation    INTEGER,
    previous_active_revision_id TEXT REFERENCES pension_revisions(revision_id),
    approved_at              TEXT,
    approval_reason          TEXT,
    recorded_at              TEXT NOT NULL
) STRICT""",

    """CREATE TABLE pension_active_pointer (
    singleton                INTEGER PRIMARY KEY CHECK(singleton = 1),
    active_revision_id       TEXT NOT NULL CHECK(length(active_revision_id) = 32 AND active_revision_id NOT GLOB '*[^0-9a-f]*') REFERENCES pension_revisions(revision_id),
    activation_generation    INTEGER NOT NULL CHECK(activation_generation >= 1),
    set_at                   TEXT NOT NULL
) STRICT""",

    """CREATE UNIQUE INDEX pension_one_active
    ON pension_revisions(state) WHERE state = 'ACTIVE'""",

    """CREATE TABLE pension_reminder_events (
    reminder_id              TEXT PRIMARY KEY CHECK(length(reminder_id) = 64),
    revision_id              TEXT NOT NULL CHECK(length(revision_id) = 32) REFERENCES pension_revisions(revision_id),
    reminder_kind            TEXT NOT NULL CHECK(reminder_kind = 'PENSION_EXCEL_AGE_31D'),
    source_age_days          INTEGER NOT NULL CHECK(source_age_days >= 31),
    reserved_at              TEXT NOT NULL,
    alert_id                 TEXT NOT NULL
) STRICT""",

    """CREATE UNIQUE INDEX pension_reminder_unique_revision_kind
    ON pension_reminder_events(revision_id, reminder_kind)""",

    """CREATE INDEX pension_activation_events_revision
    ON pension_activation_events(revision_id)""",

    """CREATE INDEX pension_revisions_state_lookup
    ON pension_revisions(state)""",
]

_PENSION_TABLES = {
    "pension_revisions",
    "pension_activation_events",
    "pension_active_pointer",
    "pension_reminder_events",
}

_HOLDINGS_MAX_BYTES = 4 * 1024 * 1024


def _ddl_sha256() -> str:
    """Compute SHA-256 of the DDL as recorded in sqlite_master after exec."""
    conn = sqlite3.connect(":memory:")
    try:
        for stmt in _DDL_STATEMENTS:
            conn.execute(stmt)
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        rows = conn.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        encoded = b"".join(
            b"\0".join(value.encode() for value in row) + b"\n" for row in rows
        )
        return hashlib.sha256(encoded).hexdigest()
    finally:
        conn.close()


PENSION_REGISTRY_DDL_SHA256 = "41fc7e728f2e9aff8520ce0e7bd62956e453695be7bea5b8e5542da584faa29f"

# ---------------------------------------------------------------------------
# Second schema generation: the registry as a CURRENT READ MODEL.
#
# Generation 1 keeps a full holdings payload on every revision row, including
# superseded ones, so holdings history is duplicated between this registry and
# the official snapshot store. Generation 2 removes that duplication: snapshot
# history is the sole holdings-history payload, and this registry carries the
# payload of exactly one current revision.
#
# It differs from generation 1 in exactly one place: a CHECK on
# ``pension_revisions`` forbidding a holdings payload on any row that is
# neither current nor in flight. Historical rows keep their audit identity and
# their foreign keys, and carry the canonical empty payload ``[]`` — no
# symbols, quantities or average prices survive on them.
#
# A freshly created registry is still generation 1. Generation 2 is reached
# only by the explicit offline migration in ``pension_registry_migration.py``.
# ---------------------------------------------------------------------------

SCHEMA_GENERATION_ONE = 1
SCHEMA_GENERATION_TWO = 2
EMPTY_HOLDINGS_PAYLOAD = "[]"
# Only the one current row may carry a holdings payload. A candidate does
# NOT persist holdings on generation 2: its verified payload is installed at
# activation time from the candidate snapshot that was already appended and
# read back, so the registry never holds two payloads at once.
PAYLOAD_BEARING_STATES = ("ACTIVE",)

_DDL_STATEMENTS_GEN2: list[str] = [
    """CREATE TABLE pension_revisions (
    revision_id              TEXT PRIMARY KEY CHECK(length(revision_id) = 32 AND revision_id NOT GLOB '*[^0-9a-f]*'),
    schema_version           INTEGER NOT NULL CHECK(schema_version = 1),
    source_content_sha256    TEXT NOT NULL CHECK(length(source_content_sha256) = 64),
    parser_version           TEXT NOT NULL,
    sheet_schema_version     INTEGER NOT NULL,
    semantic_digest          TEXT NOT NULL CHECK(length(semantic_digest) = 64),
    reconciliation_digest    TEXT NOT NULL CHECK(length(reconciliation_digest) = 64),
    holding_count            INTEGER NOT NULL CHECK(holding_count >= 0),
    liquidation_flag         INTEGER NOT NULL DEFAULT 0 CHECK(liquidation_flag IN (0, 1)),
    source_as_of             TEXT NOT NULL,
    uploaded_at              TEXT NOT NULL,
    validated_at             TEXT NOT NULL,
    approved_at              TEXT,
    approval_reason          TEXT,
    state                    TEXT NOT NULL CHECK(state IN ('CANDIDATE_VALID', 'PENDING_REVIEW', 'ACTIVE', 'REJECTED', 'SUPERSEDED')),
    created_at               TEXT NOT NULL,
    holdings_json            TEXT NOT NULL,
    previous_active_revision_id TEXT REFERENCES pension_revisions(revision_id),
    CHECK (state = 'ACTIVE' OR holdings_json = '[]')
) STRICT""",
    _DDL_STATEMENTS[1],
    _DDL_STATEMENTS[2],
    _DDL_STATEMENTS[3],
    _DDL_STATEMENTS[4],
    _DDL_STATEMENTS[5],
    _DDL_STATEMENTS[6],
    _DDL_STATEMENTS[7],

    """CREATE TABLE pension_batch_pre_snapshots (
    batch_identity           TEXT PRIMARY KEY CHECK(length(batch_identity) = 64 AND batch_identity NOT GLOB '*[^0-9a-f]*'),
    snapshot_id              TEXT NOT NULL UNIQUE CHECK(length(snapshot_id) = 32 AND snapshot_id NOT GLOB '*[^0-9a-f]*'),
    holdings_semantic_digest TEXT NOT NULL CHECK(length(holdings_semantic_digest) = 64),
    reservation_source_as_of TEXT NOT NULL CHECK(length(reservation_source_as_of) = 32 AND reservation_source_as_of LIKE '____-__-__T__:__:__.______+00:00'),
    link_state               TEXT NOT NULL CHECK(link_state IN ('RESERVED', 'CAPTURED', 'APPENDED')),
    reserved_at              TEXT NOT NULL,
    captured_at              TEXT,
    appended_at              TEXT,
    CHECK (link_state <> 'RESERVED' OR (captured_at IS NULL AND appended_at IS NULL)),
    CHECK (link_state <> 'CAPTURED' OR (captured_at IS NOT NULL AND appended_at IS NULL)),
    CHECK (link_state <> 'APPENDED' OR (captured_at IS NOT NULL AND appended_at IS NOT NULL))
) STRICT""",
]

# Generation 2 carries one extra operational table.
_PENSION_TABLES_GEN2 = _PENSION_TABLES | {"pension_batch_pre_snapshots"}


def _ddl_sha256_for(statements: list[str]) -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in statements:
            connection.execute(statement)
        connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        encoded = b"".join(
            b"\0".join(value.encode() for value in row) + b"\n" for row in rows
        )
        return hashlib.sha256(encoded).hexdigest()
    finally:
        connection.close()


PENSION_REGISTRY_DDL_SHA256_GEN2 = (
    "3cb42694d28f64035caa619c5fbc30be8708502d6154de78eb960df2284c1a60"
)
_REGISTRY_GENERATIONS = {
    SCHEMA_GENERATION_ONE: PENSION_REGISTRY_DDL_SHA256,
    SCHEMA_GENERATION_TWO: PENSION_REGISTRY_DDL_SHA256_GEN2,
}

# m1: pre/post bootstrap table inventory for validation.
_PENSION_TABLE_INVENTORY = frozenset(_PENSION_TABLES | {"sqlite_sequence"})


# ---------------------------------------------------------------------------
# Error type
# ---------------------------------------------------------------------------


class PensionRegistryCorruptError(ValueError):
    """Raised when the pension registry store fails validation."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


# ---------------------------------------------------------------------------
# Serialization helpers
# ---------------------------------------------------------------------------


def _canonical_utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _holdings_to_json(holdings: tuple[HoldingItemV1, ...]) -> str:
    obj = [
        {
            "symbol": h.symbol,
            "quantity": h.quantity,
            "avg_price": h.avg_price,
            "currency": h.currency,
            "market_raw": h.market_raw,
            "market_field": h.market_field,
        }
        for h in holdings
    ]
    return canonical_encode(obj).decode("utf-8")


def _holdings_from_json(raw: str) -> tuple[HoldingItemV1, ...]:
    decoded = canonical_decode(raw.encode("utf-8"), _HOLDINGS_MAX_BYTES)
    if not isinstance(decoded, list):
        raise PensionRegistryCorruptError("holdings_json_not_list")
    result: list[HoldingItemV1] = []
    for item in decoded:
        if not isinstance(item, dict):
            raise PensionRegistryCorruptError("holdings_json_item_not_dict")
        try:
            result.append(HoldingItemV1(
                symbol=item["symbol"],
                quantity=item["quantity"],
                avg_price=item["avg_price"],
                currency=item["currency"],
                market_raw=item["market_raw"],
                market_field=item["market_field"],
            ))
        except (KeyError, ValueError, TypeError) as exc:
            raise PensionRegistryCorruptError("holdings_json_item_invalid") from exc
    return tuple(result)


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class PensionRegistryStore:
    """Append-only pension revision registry with activation lifecycle."""

    def __init__(self, path: Path, *, clock: Clock) -> None:
        self._path = Path(path)
        self._clock = clock

    # ------------------------------------------------------------------
    # Connection / schema lifecycle
    # ------------------------------------------------------------------

    def _open(self) -> sqlite3.Connection:
        """Open (creating if necessary) and validate the database."""
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise PensionRegistryCorruptError("sqlite_version_too_old")
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            raise PensionRegistryCorruptError(safety.reason)
        exists = os.path.lexists(self._path)
        connection: sqlite3.Connection | None = None
        try:
            if not exists:
                with private_creation():
                    connection = connect_private(self._path, mode="rwc")
                    os.chmod(self._path, 0o600)
            else:
                connection = connect_private(self._path, mode="rwc")
            has_objects = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchone()[0]
            if has_objects:
                self._validate(connection)
            else:
                self._bootstrap(connection)
            return connection
        except PensionRegistryCorruptError:
            if connection is not None:
                try:
                    connection.close()
                except (sqlite3.Error, OSError):
                    pass
            raise
        except (sqlite3.Error, OSError) as error:
            if connection is not None:
                try:
                    connection.close()
                except (sqlite3.Error, OSError):
                    pass
            raise PensionRegistryCorruptError("store_open_failed") from error

    def _bootstrap(self, connection: sqlite3.Connection) -> None:
        """Create schema v1 from scratch with full validation."""
        acquire_immediate_transaction(connection)
        try:
            # Re-check inside the write lock.
            has_objects = connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchone()[0]
            if has_objects:
                connection.execute("ROLLBACK")
                self._validate(connection)
                return
            for stmt in _DDL_STATEMENTS:
                connection.execute(stmt)
            connection.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
            connection.execute("COMMIT")
        except BaseException:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise
        self._validate(connection)

    def _validate(self, connection: sqlite3.Connection) -> None:
        """Run all validation checks. Raises PensionRegistryCorruptError on failure."""
        try:
            # 1. user_version
            uv = connection.execute("PRAGMA user_version").fetchone()
            if uv is None or uv[0] != SCHEMA_VERSION:
                raise PensionRegistryCorruptError("user_version_mismatch")

            # 2. quick_check
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise PensionRegistryCorruptError("quick_check_failed")

            # 3. foreign_key_check
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise PensionRegistryCorruptError("foreign_key_check_failed")

            # 4. Table set (sqlite_sequence is auto-created by AUTOINCREMENT)
            tables = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                )
            }
            tables.discard("sqlite_sequence")
            if tables not in (_PENSION_TABLES, _PENSION_TABLES_GEN2):
                raise PensionRegistryCorruptError("table_set_mismatch")

            # 5. All tables STRICT
            for table in tables:
                if not table_is_strict(connection, table):
                    raise PensionRegistryCorruptError("not_strict_table")

            # 6. DDL checksum — exactly one known generation, else fail closed.
            actual_ddl = self._compute_ddl_sha256(connection)
            if actual_ddl not in _REGISTRY_GENERATIONS.values():
                raise PensionRegistryCorruptError("ddl_checksum_mismatch")

            # 7. File mode
            if self._path.exists():
                mode = os.stat(self._path).st_mode
                import stat as _stat
                if not _stat.S_ISREG(mode):
                    raise PensionRegistryCorruptError("not_regular_file")
                if _stat.S_IMODE(mode) != 0o600:
                    raise PensionRegistryCorruptError("file_mode_unsafe")
        except PensionRegistryCorruptError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise PensionRegistryCorruptError("validation_error") from error

    def schema_generation(self) -> int:
        """The registry schema generation, or a typed failure."""
        connection = self._open()
        try:
            return self._generation(connection)
        finally:
            connection.close()

    def _generation(self, connection: sqlite3.Connection) -> int:
        actual = self._compute_ddl_sha256(connection)
        for generation, digest in _REGISTRY_GENERATIONS.items():
            if actual == digest:
                return generation
        raise PensionRegistryCorruptError("ddl_checksum_mismatch")

    def _erase_non_current_payloads(
        self, connection: sqlite3.Connection, current_revision_id: str,
    ) -> None:
        """Generation 2: the registry keeps exactly one holdings payload.

        Every row other than the current one is reduced to the canonical empty
        payload, so no historical symbols, quantities or average prices remain
        anywhere in this store. Snapshot history is the sole holdings-history
        payload. The rows themselves survive for audit and reminder foreign
        keys.
        """
        connection.execute(
            "UPDATE pension_revisions SET holdings_json = ? "
            "WHERE revision_id <> ? AND holdings_json <> ?",
            (EMPTY_HOLDINGS_PAYLOAD, current_revision_id,
             EMPTY_HOLDINGS_PAYLOAD),
        )

    def _compute_ddl_sha256(self, connection: sqlite3.Connection) -> str:
        rows = connection.execute(
            "SELECT type, name, sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type, name"
        ).fetchall()
        encoded = b"".join(
            b"\0".join(value.encode() for value in row) + b"\n" for row in rows
        )
        return hashlib.sha256(encoded).hexdigest()

    # ------------------------------------------------------------------
    # Write helpers
    # ------------------------------------------------------------------

    def _write_tx(self) -> tuple[sqlite3.Connection, tuple[int, int]]:
        """Open, validate, acquire IMMEDIATE, return (connection, path_identity)."""
        connection = self._open()
        identity = path_identity(self._path)
        acquire_immediate_transaction(connection)
        return connection, identity

    def _commit(self, connection: sqlite3.Connection, identity: tuple[int, int]) -> None:
        """COMMIT, fsync parent dir, verify path identity."""
        if path_identity(self._path) != identity:
            connection.execute("ROLLBACK")
            raise PensionRegistryCorruptError("path_swap_detected")
        connection.execute("COMMIT")
        fsync_directory(self._path.parent)

    # ------------------------------------------------------------------
    # Lifecycle: create_candidate
    # ------------------------------------------------------------------

    def create_candidate(self, revision: PensionHoldingsRevisionV1) -> str:
        """Insert a CANDIDATE_VALID revision row and CANDIDATE_INSERTED event.

        Returns the revision_id.
        """
        now_str = _canonical_utc(self._clock())
        holdings_json = _holdings_to_json(revision.holdings)
        connection, identity = self._write_tx()
        try:
            # M-01: generation 2 keeps exactly one holdings payload, the
            # current one. A candidate carries only its metadata and digests;
            # its verified payload is installed at activation from the
            # candidate snapshot that was already appended and read back.
            if self._generation(connection) == SCHEMA_GENERATION_TWO:
                holdings_json = EMPTY_HOLDINGS_PAYLOAD
            connection.execute(
                """INSERT INTO pension_revisions(
                    revision_id, schema_version, source_content_sha256,
                    parser_version, sheet_schema_version,
                    semantic_digest, reconciliation_digest,
                    holding_count, liquidation_flag,
                    source_as_of, uploaded_at, validated_at,
                    approved_at, approval_reason, state, created_at,
                    holdings_json, previous_active_revision_id
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                          'CANDIDATE_VALID', ?, ?, NULL)""",
                (
                    revision.revision_id,
                    revision.schema_version,
                    revision.source_content_sha256,
                    revision.parser_version,
                    revision.sheet_schema_version,
                    revision.semantic_digest,
                    revision.reconciliation_digest,
                    revision.holding_count,
                    revision.liquidation_flag,
                    _canonical_utc(revision.source_as_of),
                    _canonical_utc(revision.uploaded_at),
                    _canonical_utc(revision.validated_at),
                    now_str,
                    holdings_json,
                ),
            )
            connection.execute(
                """INSERT INTO pension_activation_events(
                    revision_id, event_type, activation_generation,
                    previous_active_revision_id, approved_at, approval_reason,
                    recorded_at
                ) VALUES (?, 'CANDIDATE_INSERTED', NULL, NULL, NULL, NULL, ?)""",
                (revision.revision_id, now_str),
            )
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            raise
        except sqlite3.IntegrityError as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("candidate_insert_failed") from error
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()
        return revision.revision_id

    # ------------------------------------------------------------------
    # Lifecycle: promote_to_pending
    # ------------------------------------------------------------------

    def promote_to_pending(self, revision_id: str) -> None:
        """Transition CANDIDATE_VALID → PENDING_REVIEW."""
        now_str = _canonical_utc(self._clock())
        connection, identity = self._write_tx()
        try:
            row = connection.execute(
                "SELECT state FROM pension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("revision_not_found")
            if row[0] != "CANDIDATE_VALID":
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError(
                    f"illegal_state_transition:{row[0]}"
                )
            connection.execute(
                "UPDATE pension_revisions SET state = 'PENDING_REVIEW' "
                "WHERE revision_id = ?",
                (revision_id,),
            )
            connection.execute(
                """INSERT INTO pension_activation_events(
                    revision_id, event_type, activation_generation,
                    previous_active_revision_id, approved_at, approval_reason,
                    recorded_at
                ) VALUES (?, 'PENDING_REVIEW', NULL, NULL, NULL, NULL, ?)""",
                (revision_id, now_str),
            )
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            raise
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Lifecycle: activate
    # ------------------------------------------------------------------

    def activate(self, revision_id: str, *, approval_reason: str) -> None:
        """Promote a revision to ACTIVE in a single transaction.

        Steps:
        1. Supersede the current ACTIVE revision (if any).
        2. Set target revision state = ACTIVE.
        3. Insert ACTIVATED event.
        4. UPSERT pension_active_pointer.

        The unique partial index ``pension_one_active`` enforces exactly-one
        ACTIVE at the DB level.
        """
        now_str = _canonical_utc(self._clock())
        connection, identity = self._write_tx()
        try:
            row = connection.execute(
                "SELECT state FROM pension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("revision_not_found")
            if row[0] == "ACTIVE":
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("already_active")
            if row[0] not in ("CANDIDATE_VALID", "PENDING_REVIEW", "SUPERSEDED"):
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError(
                    f"illegal_state_transition:{row[0]}"
                )

            # Find current ACTIVE (if any).
            active_row = connection.execute(
                "SELECT revision_id FROM pension_revisions WHERE state = 'ACTIVE'"
            ).fetchone()
            previous_active_id = active_row[0] if active_row else None

            # Compute next generation.
            ptr_row = connection.execute(
                "SELECT activation_generation FROM pension_active_pointer "
                "WHERE singleton = 1"
            ).fetchone()
            next_generation = (ptr_row[0] + 1) if ptr_row else 1

            generation = self._generation(connection)
            if generation == SCHEMA_GENERATION_TWO:
                # The candidate row holds no payload on generation 2, so a bare
                # activation could only install an empty current state. Current
                # is installed exclusively through
                # ``replace_current_from_snapshot``, which carries the payload
                # verified against the candidate snapshot.
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError(
                    "activation_requires_verified_snapshot")

            # 1. Supersede current ACTIVE. On generation 2 the state change
            # and the payload erasure are one statement: a SUPERSEDED row is
            # never allowed to exist holding content, not even transiently
            # inside the transaction.
            if previous_active_id is not None:
                if generation == SCHEMA_GENERATION_TWO:
                    connection.execute(
                        "UPDATE pension_revisions SET state = 'SUPERSEDED', "
                        "holdings_json = ? WHERE revision_id = ?",
                        (EMPTY_HOLDINGS_PAYLOAD, previous_active_id),
                    )
                else:
                    connection.execute(
                        "UPDATE pension_revisions SET state = 'SUPERSEDED' "
                        "WHERE revision_id = ?",
                        (previous_active_id,),
                    )

            # 2. Target → ACTIVE.
            connection.execute(
                "UPDATE pension_revisions SET state = 'ACTIVE', "
                "approved_at = ?, approval_reason = ? "
                "WHERE revision_id = ?",
                (now_str, approval_reason, revision_id),
            )

            # 3. ACTIVATED event.
            connection.execute(
                """INSERT INTO pension_activation_events(
                    revision_id, event_type, activation_generation,
                    previous_active_revision_id, approved_at, approval_reason,
                    recorded_at
                ) VALUES (?, 'ACTIVATED', ?, ?, ?, ?, ?)""",
                (
                    revision_id,
                    next_generation,
                    previous_active_id,
                    now_str,
                    approval_reason,
                    now_str,
                ),
            )

            # 4. UPSERT pointer.
            connection.execute(
                """INSERT INTO pension_active_pointer(
                    singleton, active_revision_id, activation_generation, set_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    active_revision_id = excluded.active_revision_id,
                    activation_generation = excluded.activation_generation,
                    set_at = excluded.set_at""",
                (revision_id, next_generation, now_str),
            )

            # 5. Generation 2 only: leave exactly one holdings payload behind
            # (this also clears any leftover unactivated candidate).
            if generation == SCHEMA_GENERATION_TWO:
                self._erase_non_current_payloads(connection, revision_id)

            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise
        except sqlite3.IntegrityError as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("activation_integrity_error") from error
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Lifecycle: rollback_to
    # ------------------------------------------------------------------

    def rollback_to(self, target_revision_id: str, *, approval_reason: str) -> None:
        """Roll back: current ACTIVE → SUPERSEDED, target → ACTIVE, ROLLED_BACK event.

        Generation 1 only. On generation 2 a historical row carries no holdings
        payload by construction, so rolling back "to" it would resurrect an
        empty revision; rollback there goes through
        ``replace_current_from_snapshot`` instead and is rejected here.
        """
        now_str = _canonical_utc(self._clock())
        connection, identity = self._write_tx()
        try:
            if self._generation(connection) == SCHEMA_GENERATION_TWO:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError(
                    "rollback_by_historical_payload_forbidden")
            row = connection.execute(
                "SELECT state FROM pension_revisions WHERE revision_id = ?",
                (target_revision_id,),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("revision_not_found")
            if row[0] == "ACTIVE":
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("target_already_active")

            # Find current ACTIVE.
            active_row = connection.execute(
                "SELECT revision_id FROM pension_revisions WHERE state = 'ACTIVE'"
            ).fetchone()
            previous_active_id = active_row[0] if active_row else None

            # Compute next generation.
            ptr_row = connection.execute(
                "SELECT activation_generation FROM pension_active_pointer "
                "WHERE singleton = 1"
            ).fetchone()
            next_generation = (ptr_row[0] + 1) if ptr_row else 1

            # 1. Supersede current ACTIVE.
            if previous_active_id is not None:
                connection.execute(
                    "UPDATE pension_revisions SET state = 'SUPERSEDED' "
                    "WHERE revision_id = ?",
                    (previous_active_id,),
                )

            # 2. Target → ACTIVE.
            # m2: do NOT overwrite historical approved_at/approval_reason.
            # Only set state = ACTIVE; the original approval provenance is
            # preserved in the revision row and the ROLLED_BACK event carries
            # the rollback reason.
            connection.execute(
                "UPDATE pension_revisions SET state = 'ACTIVE' "
                "WHERE revision_id = ?",
                (target_revision_id,),
            )

            # 3. ROLLED_BACK event.
            connection.execute(
                """INSERT INTO pension_activation_events(
                    revision_id, event_type, activation_generation,
                    previous_active_revision_id, approved_at, approval_reason,
                    recorded_at
                ) VALUES (?, 'ROLLED_BACK', ?, ?, ?, ?, ?)""",
                (
                    target_revision_id,
                    next_generation,
                    previous_active_id,
                    now_str,
                    approval_reason,
                    now_str,
                ),
            )

            # 4. UPSERT pointer.
            connection.execute(
                """INSERT INTO pension_active_pointer(
                    singleton, active_revision_id, activation_generation, set_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    active_revision_id = excluded.active_revision_id,
                    activation_generation = excluded.activation_generation,
                    set_at = excluded.set_at""",
                (target_revision_id, next_generation, now_str),
            )

            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            raise
        except sqlite3.IntegrityError as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("rollback_integrity_error") from error
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Generation 2: replace the current read model from a verified snapshot
    # ------------------------------------------------------------------

    def replace_current_from_snapshot(
        self, revision: PensionHoldingsRevisionV1, *, approval_reason: str,
    ) -> None:
        """Atomically make *revision* the one current holdings payload.

        This is the generation-2 rollback and replacement primitive. The caller
        reconstructs *revision* from a verified pension snapshot — snapshot
        history is the sole holdings-history payload — and this method installs
        it as current in one transaction, leaving every other row with the
        canonical empty payload.

        Idempotent on an already-current revision id.
        """
        if type(approval_reason) is not str or not approval_reason:
            raise PensionRegistryCorruptError("missing_approval_reason")
        now_str = _canonical_utc(self._clock())
        holdings_json = _holdings_to_json(revision.holdings)
        connection, identity = self._write_tx()
        try:
            if self._generation(connection) != SCHEMA_GENERATION_TWO:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("requires_current_read_model")

            existing = connection.execute(
                "SELECT state FROM pension_revisions WHERE revision_id = ?",
                (revision.revision_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO pension_revisions(
                        revision_id, schema_version, source_content_sha256,
                        parser_version, sheet_schema_version,
                        semantic_digest, reconciliation_digest,
                        holding_count, liquidation_flag,
                        source_as_of, uploaded_at, validated_at,
                        approved_at, approval_reason, state, created_at,
                        holdings_json, previous_active_revision_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL,
                              'CANDIDATE_VALID', ?, ?, NULL)""",
                    (
                        revision.revision_id, revision.schema_version,
                        revision.source_content_sha256, revision.parser_version,
                        revision.sheet_schema_version, revision.semantic_digest,
                        revision.reconciliation_digest, revision.holding_count,
                        revision.liquidation_flag,
                        _canonical_utc(revision.source_as_of),
                        _canonical_utc(revision.uploaded_at),
                        _canonical_utc(revision.validated_at),
                        now_str, EMPTY_HOLDINGS_PAYLOAD,
                    ),
                )
                connection.execute(
                    """INSERT INTO pension_activation_events(
                        revision_id, event_type, activation_generation,
                        previous_active_revision_id, approved_at,
                        approval_reason, recorded_at
                    ) VALUES (?, 'CANDIDATE_INSERTED', NULL, NULL, NULL, NULL, ?)""",
                    (revision.revision_id, now_str),
                )

            active_row = connection.execute(
                "SELECT revision_id FROM pension_revisions WHERE state = 'ACTIVE'"
            ).fetchone()
            previous_active_id = active_row[0] if active_row else None
            if previous_active_id == revision.revision_id:
                previous_active_id = None
            elif previous_active_id is not None:
                connection.execute(
                    "UPDATE pension_revisions SET state = 'SUPERSEDED', "
                    "holdings_json = ? WHERE revision_id = ?",
                    (EMPTY_HOLDINGS_PAYLOAD, previous_active_id))

            # Install the target as the one current revision. State and payload
            # move together: a historical row is never allowed to hold content,
            # so it becomes ACTIVE in the same statement that gives it the
            # payload the caller rebuilt from the verified snapshot. Nothing
            # this store retained is ever resurrected.
            connection.execute(
                "UPDATE pension_revisions SET state = 'ACTIVE', "
                "holdings_json = ?, holding_count = ?, semantic_digest = ?, "
                "approved_at = ?, approval_reason = ? WHERE revision_id = ?",
                (holdings_json, revision.holding_count,
                 revision.semantic_digest, now_str, approval_reason,
                 revision.revision_id),
            )

            pointer_row = connection.execute(
                "SELECT activation_generation FROM pension_active_pointer "
                "WHERE singleton = 1"
            ).fetchone()
            next_generation = (pointer_row[0] + 1) if pointer_row else 1
            connection.execute(
                """INSERT INTO pension_activation_events(
                    revision_id, event_type, activation_generation,
                    previous_active_revision_id, approved_at, approval_reason,
                    recorded_at
                ) VALUES (?, 'ROLLED_BACK', ?, ?, ?, ?, ?)""",
                (revision.revision_id, next_generation, previous_active_id,
                 now_str, approval_reason, now_str),
            )
            connection.execute(
                """INSERT INTO pension_active_pointer(
                    singleton, active_revision_id, activation_generation, set_at
                ) VALUES (1, ?, ?, ?)
                ON CONFLICT(singleton) DO UPDATE SET
                    active_revision_id = excluded.active_revision_id,
                    activation_generation = excluded.activation_generation,
                    set_at = excluded.set_at""",
                (revision.revision_id, next_generation, now_str),
            )
            self._erase_non_current_payloads(connection, revision.revision_id)
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Bounded durable batch <-> pre-snapshot link (generation 2)
    #
    # Identities only: a batch identity, the snapshot id reserved for it, and
    # the semantic digest of the holdings that snapshot captures. No holdings
    # payload, no private source cells, no filesystem paths.
    #
    # The reservation is written BEFORE the capture, so a crash anywhere after
    # it — including after the snapshot commit but before its read-back — is
    # recoverable: the retry finds the same reserved snapshot id, re-appends it
    # (the official store answers DUPLICATE for identical content) and
    # converges, without ever creating a second snapshot.
    # ------------------------------------------------------------------

    def reserve_batch_pre_snapshot(
        self, batch_identity: str, snapshot_id: str, holdings_digest: str,
        source_as_of: datetime,
    ) -> dict:
        """Reserve, or return the existing reservation for, a batch.

        The reservation binds the batch identity to the snapshot id, the
        holdings semantic digest AND the ``source_as_of`` of the holdings being
        captured, all at reservation time. That last binding is what lets a much
        later recovery validate the linked capture without consulting current
        state, which may legitimately have moved on since.

        Idempotent on ``batch_identity``; any differing binding is a conflict
        and fails closed, so a retry can never fork into a second snapshot.
        """
        now_str = _canonical_utc(self._clock())
        source_as_of_str = _canonical_utc(source_as_of)
        connection, identity = self._write_tx()
        try:
            if self._generation(connection) != SCHEMA_GENERATION_TWO:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("requires_current_read_model")
            row = connection.execute(
                "SELECT snapshot_id, holdings_semantic_digest, link_state, "
                "reservation_source_as_of "
                "FROM pension_batch_pre_snapshots WHERE batch_identity = ?",
                (batch_identity,),
            ).fetchone()
            if row is not None:
                connection.execute("ROLLBACK")
                if (row[0] != snapshot_id or row[1] != holdings_digest
                        or row[3] != source_as_of_str):
                    raise PensionRegistryCorruptError(
                        "batch_pre_snapshot_conflict")
                return {"batch_identity": batch_identity,
                        "snapshot_id": row[0],
                        "holdings_semantic_digest": row[1],
                        "reservation_source_as_of": row[3],
                        "link_state": row[2], "created": False}
            connection.execute(
                """INSERT INTO pension_batch_pre_snapshots(
                    batch_identity, snapshot_id, holdings_semantic_digest,
                    reservation_source_as_of,
                    link_state, reserved_at, captured_at, appended_at
                ) VALUES (?, ?, ?, ?, 'RESERVED', ?, NULL, NULL)""",
                (batch_identity, snapshot_id, holdings_digest,
                 source_as_of_str, now_str),
            )
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise
        except sqlite3.IntegrityError as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError(
                "batch_pre_snapshot_conflict") from error
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()
        return {"batch_identity": batch_identity, "snapshot_id": snapshot_id,
                "holdings_semantic_digest": holdings_digest,
                "reservation_source_as_of": source_as_of_str,
                "link_state": "RESERVED", "created": True}

    def advance_batch_pre_snapshot(self, batch_identity: str, state: str) -> None:
        """Move a reservation to CAPTURED or APPENDED. Idempotent."""
        if state not in ("CAPTURED", "APPENDED"):
            raise PensionRegistryCorruptError("invalid_link_state")
        now_str = _canonical_utc(self._clock())
        connection, identity = self._write_tx()
        try:
            row = connection.execute(
                "SELECT link_state, captured_at FROM pension_batch_pre_snapshots "
                "WHERE batch_identity = ?", (batch_identity,)).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("batch_pre_snapshot_missing")
            if row[0] == state:
                connection.execute("ROLLBACK")
                return
            if state == "CAPTURED":
                connection.execute(
                    "UPDATE pension_batch_pre_snapshots SET link_state = ?, "
                    "captured_at = ? WHERE batch_identity = ?",
                    (state, now_str, batch_identity))
            else:
                connection.execute(
                    "UPDATE pension_batch_pre_snapshots SET link_state = ?, "
                    "captured_at = COALESCE(captured_at, ?), appended_at = ? "
                    "WHERE batch_identity = ?",
                    (state, now_str, now_str, batch_identity))
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    def read_batch_pre_snapshot(self, batch_identity: str) -> dict | None:
        connection = self._open()
        try:
            row = connection.execute(
                "SELECT snapshot_id, holdings_semantic_digest, link_state, "
                "reservation_source_as_of "
                "FROM pension_batch_pre_snapshots WHERE batch_identity = ?",
                (batch_identity,),
            ).fetchone()
            if row is None:
                return None
            return {"batch_identity": batch_identity, "snapshot_id": row[0],
                    "holdings_semantic_digest": row[1], "link_state": row[2],
                    "reservation_source_as_of": row[3]}
        finally:
            connection.close()

    def count_batch_pre_snapshots(self) -> int:
        connection = self._open()
        try:
            return connection.execute(
                "SELECT count(*) FROM pension_batch_pre_snapshots"
            ).fetchone()[0]
        finally:
            connection.close()

    def count_payload_bearing_rows(self) -> int:
        """How many rows still carry a holdings payload. Generation 2: one."""
        connection = self._open()
        try:
            return connection.execute(
                "SELECT count(*) FROM pension_revisions WHERE holdings_json <> ?",
                (EMPTY_HOLDINGS_PAYLOAD,),
            ).fetchone()[0]
        finally:
            connection.close()

    def count_superseded_payload_rows(self) -> int:
        """Rows in a historical state that still carry holdings content."""
        connection = self._open()
        try:
            return connection.execute(
                "SELECT count(*) FROM pension_revisions "
                "WHERE state IN ('SUPERSEDED', 'REJECTED') "
                "AND holdings_json <> ?", (EMPTY_HOLDINGS_PAYLOAD,),
            ).fetchone()[0]
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Read: active revision
    # ------------------------------------------------------------------

    def read_active_revision(self) -> PensionHoldingsRevisionV1 | None:
        """Read the ACTIVE revision row and reconstruct the full revision."""
        connection = self._open()
        try:
            row = connection.execute(
                """SELECT revision_id, schema_version, source_content_sha256,
                          parser_version, sheet_schema_version,
                          semantic_digest, reconciliation_digest,
                          holding_count, liquidation_flag,
                          source_as_of, uploaded_at, validated_at,
                          holdings_json
                   FROM pension_revisions
                   WHERE state = 'ACTIVE'"""
            ).fetchone()
            if row is None:
                return None
            return self._row_to_revision(row)
        finally:
            connection.close()

    def read_active_revision_ro(self) -> PensionHoldingsRevisionV1 | None:
        """Strict no-create/no-bootstrap read of the existing ACTIVE revision."""
        if not os.path.lexists(self._path):
            return None
        try:
            import stat as _stat
            path_stat = os.lstat(self._path)
            if (not _stat.S_ISREG(path_stat.st_mode)
                    or _stat.S_IMODE(path_stat.st_mode) != 0o600
                    or path_stat.st_nlink != 1):
                raise PensionRegistryCorruptError("read_only_path_unsafe")
            uri = f"file:{self._path.resolve().as_posix()}?mode=ro"
            connection = sqlite3.connect(uri, uri=True)
            try:
                connection.execute("PRAGMA query_only = ON")
                self._validate(connection)
                row = connection.execute(
                    """SELECT revision_id, schema_version, source_content_sha256,
                              parser_version, sheet_schema_version,
                              semantic_digest, reconciliation_digest,
                              holding_count, liquidation_flag,
                              source_as_of, uploaded_at, validated_at, holdings_json
                       FROM pension_revisions WHERE state = 'ACTIVE'"""
                ).fetchone()
                return None if row is None else self._row_to_revision(row)
            finally:
                connection.close()
        except PensionRegistryCorruptError:
            raise
        except (sqlite3.Error, OSError) as error:
            raise PensionRegistryCorruptError("read_only_open_failed") from error

    @staticmethod
    def _row_to_revision(row) -> PensionHoldingsRevisionV1:
        (
            revision_id,
            schema_version,
            source_content_sha256,
            parser_version,
            sheet_schema_version,
            semantic_digest,
            reconciliation_digest,
            holding_count,
            liquidation_flag,
            source_as_of_str,
            uploaded_at_str,
            validated_at_str,
            holdings_json,
        ) = row
        holdings = _holdings_from_json(holdings_json)
        return PensionHoldingsRevisionV1(
            revision_id=revision_id,
            schema_version=schema_version,
            source_content_sha256=source_content_sha256,
            parser_version=parser_version,
            sheet_schema_version=sheet_schema_version,
            holdings=holdings,
            source_as_of=datetime.fromisoformat(source_as_of_str),
            uploaded_at=datetime.fromisoformat(uploaded_at_str),
            validated_at=datetime.fromisoformat(validated_at_str),
            semantic_digest=semantic_digest,
            reconciliation_digest=reconciliation_digest,
            holding_count=holding_count,
            liquidation_flag=liquidation_flag,
        )

    # ------------------------------------------------------------------
    # Read: pointer / generation helpers (for tests)
    # ------------------------------------------------------------------

    def read_pointer(self) -> dict | None:
        """Return {active_revision_id, activation_generation, set_at} or None."""
        connection = self._open()
        try:
            row = connection.execute(
                "SELECT active_revision_id, activation_generation, set_at "
                "FROM pension_active_pointer WHERE singleton = 1"
            ).fetchone()
            if row is None:
                return None
            return {
                "active_revision_id": row[0],
                "activation_generation": row[1],
                "set_at": row[2],
            }
        finally:
            connection.close()

    def read_revision_metadata(self, revision_id: str) -> dict | None:
        """Identity and digests of a revision, without its holdings payload.

        This is how an activation verifies a re-presented candidate: the
        registry keeps the candidate's digests but never its holdings, so the
        operator must supply the source again and it must digest-match.
        """
        connection = self._open()
        try:
            row = connection.execute(
                "SELECT revision_id, state, source_content_sha256, "
                "semantic_digest, holding_count, source_as_of "
                "FROM pension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            if row is None:
                return None
            return {
                "revision_id": row[0], "state": row[1],
                "source_content_sha256": row[2], "semantic_digest": row[3],
                "holding_count": row[4], "source_as_of": row[5],
            }
        finally:
            connection.close()

    def read_revision_state(self, revision_id: str) -> str | None:
        """Return the state of a revision, or None if not found."""
        connection = self._open()
        try:
            row = connection.execute(
                "SELECT state FROM pension_revisions WHERE revision_id = ?",
                (revision_id,),
            ).fetchone()
            return row[0] if row else None
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Reminders
    # ------------------------------------------------------------------

    def reserve_reminder(
        self,
        reminder_id: str,
        revision_id: str,
        source_age_days: int,
        now: datetime,
        *,
        alert_id: str = "",
    ) -> None:
        """Insert a pension_reminder_events row.

        When *alert_id* is provided, the row is created with the alert_id
        pre-registered (§3.5 crash-safe ordering: reserve before outbox).

        Raises PensionRegistryCorruptError on duplicate reminder_id.
        """
        now_str = _canonical_utc(now)
        connection, identity = self._write_tx()
        try:
            connection.execute(
                """INSERT INTO pension_reminder_events(
                    reminder_id, revision_id, reminder_kind,
                    source_age_days, reserved_at, alert_id
                ) VALUES (?, ?, 'PENSION_EXCEL_AGE_31D', ?, ?, ?)""",
                (reminder_id, revision_id, source_age_days, now_str, alert_id),
            )
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            raise
        except sqlite3.IntegrityError as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("reminder_duplicate") from error
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    def link_reminder_to_alert(self, reminder_id: str, alert_id: str) -> None:
        """Update the alert_id on an existing reminder row."""
        connection, identity = self._write_tx()
        try:
            cursor = connection.execute(
                "UPDATE pension_reminder_events SET alert_id = ? "
                "WHERE reminder_id = ?",
                (alert_id, reminder_id),
            )
            if cursor.rowcount == 0:
                connection.execute("ROLLBACK")
                raise PensionRegistryCorruptError("reminder_not_found")
            self._commit(connection, identity)
        except PensionRegistryCorruptError:
            raise
        except (sqlite3.Error, OSError) as error:
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            raise PensionRegistryCorruptError("store_write_failed") from error
        finally:
            connection.close()

    def read_reminder(self, reminder_id: str) -> sqlite3.Row | None:
        """Return the reminder row or None."""
        connection = self._open()
        try:
            connection.row_factory = sqlite3.Row
            return connection.execute(
                "SELECT reminder_id, revision_id, reminder_kind, "
                "source_age_days, reserved_at, alert_id "
                "FROM pension_reminder_events WHERE reminder_id = ?",
                (reminder_id,),
            ).fetchone()
        finally:
            connection.close()
