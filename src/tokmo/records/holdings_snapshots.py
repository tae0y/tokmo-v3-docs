"""Canonical serialization and content hash for complete holdings snapshots.

One holdings-specific serializer produces the canonical bytes used by
append, read reconstruction, migration, and fixtures alike, so byte
equality — and therefore ``content_hash`` — is deterministic for a given
capture. The canonical form excludes ``snapshot_id``, permit metadata,
``stored_at``, and schema versions: it covers exactly the captured holdings
semantics.
"""

import hashlib
import json
import os
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path

from tokmo.records.holdings_persistence import (
    CRITERIA_VERSION_V1,
    MIGRATION_CRITERIA_VERSION,
    MIGRATION_PROGRAM_TOKEN,
    NAMED_PROGRAM_ALLOWLIST_V1,
    PENSION_CRITERIA_VERSION,  # N4
    PENSION_NAMED_PROGRAM_ALLOWLIST,  # N4
    USER_REQUEST_PROGRAM_NAME,
    HoldingsPersistencePermitV1,
    PurposeKind,
    is_valid_legacy_digest,
    is_valid_uuid4_hex,
    validate_permit,
)
from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    connect_private,
    fsync_directory,
    path_identity,
    prepare_private_path,
    private_creation,
    sqlite_locked,
)
from tokmo.values import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    HoldingsSnapshotHeader,
    TossHoldingFact,
    canonical_decimal,
)

# The one canonical location of this store, relative to the data root.
#
# It has always lived under the Toss leaf: that is where the live database is,
# what the canary protected-path list names, and what
# ``composition._holdings_snapshot_store_path`` and
# ``composition._holdings_route_paths`` resolve. Those two do NOT delegate
# here: ``composition.py`` is sealed byte-identical at the base commit by
# another slice's contract, so it keeps its own literal spellings. Parity is
# therefore enforced by structural test -- tests/test_pension_canonical_topology
# .py calls both composition resolvers for real and compares them against this
# declaration -- rather than by delegation.
#
# Operator tools must resolve THIS location or they would create a second,
# parallel snapshot authority. It is declared in the module that owns the
# store, which every caller already imports, so nobody has to reach the
# composition root just for a path.
TOSS_LEAF_NAME = "toss"
HOLDINGS_SNAPSHOT_FILENAME = "holdings_snapshots.sqlite3"
HOLDINGS_SNAPSHOT_RELATIVE_PATH = (TOSS_LEAF_NAME, HOLDINGS_SNAPSHOT_FILENAME)


def holdings_snapshot_store_path(data_root: Path) -> Path:
    """The one canonical holdings snapshot database under *data_root*.

    *data_root* is the ``.../share/tokmo-v3`` directory itself, which is what
    operator tools receive as ``--root``.
    """
    return Path(data_root).joinpath(*HOLDINGS_SNAPSHOT_RELATIVE_PATH)


# Domain separator for deterministic legacy-migration snapshot IDs.
_LEGACY_ID_DOMAIN = b"tokmo-holdings-legacy-v1:"
HOLDINGS_CLOSED_REASON_TOKENS = frozenset(
    {
        "invalid_snapshot",
        "invalid_permit",
        "duplicate",
        "conflict",
        "schema_mismatch",
        "store_corrupt",
        "store_locked",
        "family_locked",
        "store_write_failed",
        "cutover_incomplete",
    }
)

_DDL = (
    """CREATE TABLE storage_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT""",
    """CREATE TABLE holdings_snapshots (
    snapshot_id              TEXT PRIMARY KEY,
    source                   TEXT NOT NULL,
    captured_at              TEXT NOT NULL,
    discovered_account_count INTEGER NOT NULL,
    content_hash             TEXT NOT NULL,
    criteria_version         TEXT NOT NULL,
    purpose_kind             TEXT NOT NULL,
    program_name             TEXT NOT NULL,
    authorized_at            TEXT NOT NULL,
    stored_at                TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    CHECK (length(snapshot_id) IN (32, 64)),
    CHECK (source = 'toss-holdings-v1'),
    CHECK (discovered_account_count >= 1),
    CHECK (
        (criteria_version = 'holdings-persistence-v1'
         AND purpose_kind IN ('USER_REQUESTED_CAPTURE',
                              'APPROVED_NAMED_PROGRAM'))
        OR
        (criteria_version = 'legacy-factstore-import-v1'
         AND purpose_kind = 'APPROVED_MIGRATION')
        OR
        (criteria_version = 'pension-excel-persistence-v1'
         AND purpose_kind = 'APPROVED_NAMED_PROGRAM')
    ),
    CHECK (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (schema_version = 1)
) STRICT""",
    """CREATE TABLE holdings_snapshot_accounts (
    snapshot_id      TEXT NOT NULL REFERENCES holdings_snapshots(snapshot_id)
                         ON DELETE RESTRICT,
    account_ordinal  INTEGER NOT NULL,
    PRIMARY KEY (snapshot_id, account_ordinal),
    CHECK (account_ordinal >= 1)
) STRICT""",
    """CREATE TABLE holdings_snapshot_items (
    snapshot_id      TEXT NOT NULL,
    account_ordinal  INTEGER NOT NULL,
    symbol           TEXT NOT NULL,
    quantity         TEXT NOT NULL,
    avg_price        TEXT NOT NULL,
    avg_price_field  TEXT NOT NULL,
    currency         TEXT,
    market_raw       TEXT,
    market_field     TEXT,
    name             TEXT,
    asset_type       TEXT,
    PRIMARY KEY (snapshot_id, account_ordinal, symbol),
    FOREIGN KEY (snapshot_id, account_ordinal)
        REFERENCES holdings_snapshot_accounts (snapshot_id, account_ordinal)
        ON DELETE RESTRICT,
    CHECK (length(symbol) BETWEEN 1 AND 64),
    CHECK (length(quantity) BETWEEN 1 AND 64),
    CHECK (length(avg_price) BETWEEN 1 AND 64),
    CHECK (
        (market_raw IS NULL AND market_field IS NULL)
        OR
        (market_raw IS NOT NULL AND market_field IS NOT NULL)
    ),
    CHECK (currency IS NULL OR length(currency) BETWEEN 1 AND 16),
    CHECK (market_raw IS NULL OR length(market_raw) BETWEEN 1 AND 16),
    CHECK (market_field IS NULL OR length(market_field) BETWEEN 1 AND 64),
    CHECK (name IS NULL OR length(name) BETWEEN 1 AND 256),
    CHECK (asset_type IS NULL OR length(asset_type) BETWEEN 1 AND 64)
) STRICT""",
    """CREATE INDEX holdings_snapshots_capture_order
    ON holdings_snapshots (captured_at, snapshot_id)""",
)
_METADATA = {
    ("holdings_snapshot_schema_version", "1"),
    ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
}

# --- second schema generation: the same store, widened to admit pension -----
#
# Generation 1 accepts only Toss captures. Generation 2 differs from it in
# EXACTLY one character range: the ``source`` CHECK also admits the pension
# source. Nothing else changes — same tables, columns, indexes, foreign keys
# and adapter token — so a generation-2 store reconstructs generation-1 rows
# byte-identically.
#
# A freshly created store is still generation 1. Generation 2 is reached only
# by the explicit offline migration in ``holdings_snapshot_migration.py``;
# there is no implicit or runtime upgrade.
TOSS_SNAPSHOT_SOURCE = "toss-holdings-v1"
PENSION_SNAPSHOT_SOURCE = "pension-excel-v1"
SCHEMA_GENERATION_ONE = 1
SCHEMA_GENERATION_TWO = 2

_DDL_GEN2 = (
    _DDL[0],
    """CREATE TABLE holdings_snapshots (
    snapshot_id              TEXT PRIMARY KEY,
    source                   TEXT NOT NULL,
    captured_at              TEXT NOT NULL,
    discovered_account_count INTEGER NOT NULL,
    content_hash             TEXT NOT NULL,
    criteria_version         TEXT NOT NULL,
    purpose_kind             TEXT NOT NULL,
    program_name             TEXT NOT NULL,
    authorized_at            TEXT NOT NULL,
    stored_at                TEXT NOT NULL,
    schema_version           INTEGER NOT NULL,
    CHECK (length(snapshot_id) IN (32, 64)),
    CHECK (source IN ('toss-holdings-v1', 'pension-excel-v1')),
    CHECK (discovered_account_count >= 1),
    CHECK (
        (criteria_version = 'holdings-persistence-v1'
         AND purpose_kind IN ('USER_REQUESTED_CAPTURE',
                              'APPROVED_NAMED_PROGRAM'))
        OR
        (criteria_version = 'legacy-factstore-import-v1'
         AND purpose_kind = 'APPROVED_MIGRATION')
        OR
        (criteria_version = 'pension-excel-persistence-v1'
         AND purpose_kind = 'APPROVED_NAMED_PROGRAM')
    ),
    CHECK (length(content_hash) = 64 AND content_hash NOT GLOB '*[^0-9a-f]*'),
    CHECK (schema_version = 1)
) STRICT""",
    _DDL[2],
    _DDL[3],
    _DDL[4],
)
_METADATA_GEN2 = {
    ("holdings_snapshot_schema_version", "2"),
    ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
}
_GENERATIONS = {
    SCHEMA_GENERATION_ONE: (_DDL, frozenset(_METADATA)),
    SCHEMA_GENERATION_TWO: (_DDL_GEN2, frozenset(_METADATA_GEN2)),
}
_TABLES = {
    "storage_metadata",
    "holdings_snapshots",
    "holdings_snapshot_accounts",
    "holdings_snapshot_items",
}
_TABLE_XINFO = {
    "storage_metadata": (
        (0, "key", "TEXT", 1, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
    ),
    "holdings_snapshots": tuple(
        (cid, name, kind, 1, None, int(cid == 0), 0)
        for cid, (name, kind) in enumerate(
            (
                ("snapshot_id", "TEXT"),
                ("source", "TEXT"),
                ("captured_at", "TEXT"),
                ("discovered_account_count", "INTEGER"),
                ("content_hash", "TEXT"),
                ("criteria_version", "TEXT"),
                ("purpose_kind", "TEXT"),
                ("program_name", "TEXT"),
                ("authorized_at", "TEXT"),
                ("stored_at", "TEXT"),
                ("schema_version", "INTEGER"),
            )
        )
    ),
    "holdings_snapshot_accounts": (
        (0, "snapshot_id", "TEXT", 1, None, 1, 0),
        (1, "account_ordinal", "INTEGER", 1, None, 2, 0),
    ),
    "holdings_snapshot_items": tuple(
        (
            cid,
            name,
            "TEXT" if cid != 1 else "INTEGER",
            int(cid <= 5),
            None,
            cid + 1 if cid <= 2 else 0,
            0,
        )
        for cid, name in enumerate(
            (
                "snapshot_id",
                "account_ordinal",
                "symbol",
                "quantity",
                "avg_price",
                "avg_price_field",
                "currency",
                "market_raw",
                "market_field",
                "name",
                "asset_type",
            )
        )
    ),
}
_FOREIGN_KEYS = {
    "storage_metadata": (),
    "holdings_snapshots": (),
    "holdings_snapshot_accounts": (
        (
            0,
            0,
            "holdings_snapshots",
            "snapshot_id",
            "snapshot_id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
    "holdings_snapshot_items": (
        (
            0,
            0,
            "holdings_snapshot_accounts",
            "snapshot_id",
            "snapshot_id",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
        (
            0,
            1,
            "holdings_snapshot_accounts",
            "account_ordinal",
            "account_ordinal",
            "NO ACTION",
            "RESTRICT",
            "NONE",
        ),
    ),
}
_INDEX_LIST = {
    "storage_metadata": ((0, "sqlite_autoindex_storage_metadata_1", 1, "pk", 0),),
    "holdings_snapshots": (
        (0, "holdings_snapshots_capture_order", 0, "c", 0),
        (1, "sqlite_autoindex_holdings_snapshots_1", 1, "pk", 0),
    ),
    "holdings_snapshot_accounts": (
        (0, "sqlite_autoindex_holdings_snapshot_accounts_1", 1, "pk", 0),
    ),
    "holdings_snapshot_items": (
        (0, "sqlite_autoindex_holdings_snapshot_items_1", 1, "pk", 0),
    ),
}
_INDEX_XINFO = {
    "sqlite_autoindex_storage_metadata_1": (
        (0, 0, "key", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "holdings_snapshots_capture_order": (
        (0, 2, "captured_at", 0, "BINARY", 1),
        (1, 0, "snapshot_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_holdings_snapshots_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_holdings_snapshot_accounts_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, 1, "account_ordinal", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0),
    ),
    "sqlite_autoindex_holdings_snapshot_items_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, 1, "account_ordinal", 0, "BINARY", 1),
        (2, 2, "symbol", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0),
    ),
}
# Identical across both generations: only a CHECK body differs, and CHECK
# bodies do not appear in the object-name set.
_EXPECTED_OBJECT_NAMES = frozenset(
    {("table", table) for table in _TABLES}
    | {("index", "holdings_snapshots_capture_order")}
    | {("index", name) for name in _INDEX_XINFO if name.startswith("sqlite_autoindex_")}
)


@dataclass(frozen=True)
class HoldingsAppendReceipt:
    status: str
    snapshot_id: str = field(repr=False)
    reason: str = field(repr=False)
    submitted_count: int = field(repr=False)
    inserted_count: int = field(repr=False)
    duplicate_count: int = field(repr=False)
    conflict_count: int = field(repr=False)
    stored_at: datetime = field(repr=False)

    def __post_init__(self) -> None:
        expected = {
            "SAVED": ("", 1, 0, 0),
            "DUPLICATE": ("duplicate", 0, 1, 0),
            "CONFLICT": ("conflict", 0, 0, 1),
        }
        if self.submitted_count != 1:
            raise ValueError("invalid_receipt")
        if self.status == "FAILED":
            if (
                self.reason not in HOLDINGS_CLOSED_REASON_TOKENS
                or self.reason in {"duplicate", "conflict"}
                or any((self.inserted_count, self.duplicate_count, self.conflict_count))
            ):
                raise ValueError("invalid_receipt")
        elif expected.get(self.status) != (
            self.reason,
            self.inserted_count,
            self.duplicate_count,
            self.conflict_count,
        ):
            raise ValueError("invalid_receipt")


class HoldingsSnapshotReadError(Exception):
    def __init__(self, reason: str) -> None:
        if reason not in {
            "not_found",
            "schema_mismatch",
            "store_locked",
            "store_corrupt",
        }:
            reason = "store_corrupt"
        self.reason = reason
        super().__init__(reason)


def canonical_utc_isoformat(dt: datetime) -> str:
    """Canonical timestamp: astimezone(UTC), microseconds, +00:00 suffix."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def canonical_snapshot_bytes(snapshot: CompleteHoldingsSnapshot) -> bytes:
    """The canonical UTF-8 JSON bytes of one complete capture.

    Fixed key order, no insignificant whitespace, ``ensure_ascii=False`` so
    provider strings keep their exact code-point sequence, and accounts and
    holdings in canonical ``(account_ordinal, symbol)`` ascending order.
    """
    obj = {
        "source": snapshot.source,
        "captured_at": canonical_utc_isoformat(snapshot.captured_at),
        "discovered_account_count": snapshot.discovered_account_count,
        "accounts": [
            {
                "account_ordinal": account.account_ordinal,
                "holdings": [
                    {
                        "symbol": item.symbol,
                        "quantity": canonical_decimal(Decimal(item.quantity)),
                        "avg_price": canonical_decimal(Decimal(item.avg_price)),
                        "avg_price_field": item.avg_price_field,
                        "currency": item.currency,
                        "market_raw": item.market_raw,
                        "market_field": item.market_field,
                        "name": item.name,
                        "asset_type": item.asset_type,
                    }
                    for item in sorted(account.holdings, key=lambda held: held.symbol)
                ],
            }
            for account in snapshot.accounts
        ],
    }
    text = json.dumps(
        obj, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=False
    )
    return text.encode("utf-8")


def snapshot_content_hash(snapshot: CompleteHoldingsSnapshot) -> str:
    """SHA-256 hexdigest of the canonical serialization."""
    return hashlib.sha256(canonical_snapshot_bytes(snapshot)).hexdigest()


def legacy_snapshot_id(canonical_bytes: bytes) -> str:
    """Deterministic domain-separated snapshot ID for one legacy capture."""
    return hashlib.sha256(_LEGACY_ID_DOMAIN + canonical_bytes).hexdigest()


def validate_snapshot_id(value: str) -> bool:
    return isinstance(value, str) and (
        is_valid_uuid4_hex(value) or is_valid_legacy_digest(value)
    )


def validate_program_name_pairing(
    *,
    criteria_version: str,
    purpose_kind: str,
    program_name: str,
) -> bool:
    if (
        criteria_version == CRITERIA_VERSION_V1
        and purpose_kind == PurposeKind.USER_REQUESTED_CAPTURE.value
    ):
        return program_name == USER_REQUEST_PROGRAM_NAME
    if (
        criteria_version == CRITERIA_VERSION_V1
        and purpose_kind == PurposeKind.APPROVED_NAMED_PROGRAM.value
    ):
        return program_name in NAMED_PROGRAM_ALLOWLIST_V1
    if (
        criteria_version == PENSION_CRITERIA_VERSION
        and purpose_kind == PurposeKind.APPROVED_NAMED_PROGRAM.value
    ):
        return program_name in PENSION_NAMED_PROGRAM_ALLOWLIST
    return (
        criteria_version == MIGRATION_CRITERIA_VERSION
        and purpose_kind == PurposeKind.APPROVED_MIGRATION.value
        and program_name == MIGRATION_PROGRAM_TOKEN
    )


def parse_utc_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or canonical_utc_isoformat(parsed) != value:
        raise ValueError("timestamp_not_canonical")
    return parsed.astimezone(UTC)


def canonical_decimal_str(value: str) -> str:
    try:
        parsed = Decimal(value)
        result = canonical_decimal(parsed)
    except (InvalidOperation, ValueError):
        raise ValueError("decimal_not_canonical") from None
    if result != value:
        raise ValueError("decimal_not_canonical")
    return result


def validate_nullable_nonempty_text(
    value: str | None,
    *,
    min_len: int,
    max_len: int,
) -> bool:
    return value is None or (
        isinstance(value, str) and value != "" and min_len <= len(value) <= max_len
    )


def _normalized(sql: str) -> str:
    return " ".join(sql.split())


class HoldingsSnapshotStore:
    def __init__(
        self,
        path: Path,
        *,
        clock: Callable[[], datetime],
        initialize_precreated: bool = False,
    ) -> None:
        self._path = Path(path)
        self._clock = clock
        self._initialize_precreated = initialize_precreated

    def _receipt(
        self, status: str, snapshot_id: str, stored_at: datetime, reason: str = ""
    ) -> HoldingsAppendReceipt:
        return HoldingsAppendReceipt(
            status,
            snapshot_id,
            reason,
            1,
            int(status == "SAVED"),
            int(status == "DUPLICATE"),
            int(status == "CONFLICT"),
            stored_at,
        )

    def _failure(
        self, snapshot_id: str, stored_at: datetime, reason: str
    ) -> HoldingsAppendReceipt:
        return HoldingsAppendReceipt(
            "FAILED", snapshot_id, reason, 1, 0, 0, 0, stored_at
        )

    def _initialize(self, connection: sqlite3.Connection) -> None:
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

    def _detect_generation(self, connection: sqlite3.Connection) -> int | None:
        """Return the recognized schema generation, or None to fail closed.

        Exactly one generation may match. An unknown, tampered, or partially
        migrated schema — including a metadata version this binary does not
        know — returns None and is reported as ``schema_mismatch``.
        """
        # Object names first, exactly as this store has always discriminated:
        # an unreadable sqlite_master is corruption and must propagate, while a
        # readable one whose objects are foreign is a schema mismatch. Both
        # generations carry the identical object-name set, so this check is
        # generation-independent and must pass before metadata is touched.
        observed_names = {
            (kind, name)
            for kind, name, _ in connection.execute(
                "SELECT type, name, sql FROM sqlite_master"
            )
        }
        if observed_names != _EXPECTED_OBJECT_NAMES:
            return None
        observed_metadata = set(
            connection.execute("SELECT key, value FROM storage_metadata")
        )
        for generation, (ddl, metadata) in _GENERATIONS.items():
            if observed_metadata != metadata:
                continue
            return generation if self._schema_valid(connection, ddl) else None
        return None

    def _schema_valid(self, connection: sqlite3.Connection, ddl) -> bool:
        objects = connection.execute(
            "SELECT type, name, sql FROM sqlite_master"
        ).fetchall()
        expected = {
            ("table", "storage_metadata"): ddl[0],
            ("table", "holdings_snapshots"): ddl[1],
            ("table", "holdings_snapshot_accounts"): ddl[2],
            ("table", "holdings_snapshot_items"): ddl[3],
            ("index", "holdings_snapshots_capture_order"): ddl[4],
        }
        expected_names = set(expected) | {
            ("index", name)
            for name in _INDEX_XINFO
            if name.startswith("sqlite_autoindex_")
        }
        observed_names = {(kind, name) for kind, name, _ in objects}
        if observed_names != expected_names:
            return False
        observed = {
            (kind, name): sql
            for kind, name, sql in objects
            if not name.startswith("sqlite_autoindex_")
        }
        if set(observed) != set(expected):
            return False
        if any(
            _normalized(observed[key]) != _normalized(sql)
            for key, sql in expected.items()
        ):
            return False
        strict = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_list")
            if row[1] in _TABLES
        }
        if strict != {table: 1 for table in _TABLES}:
            return False
        for table in _TABLES:
            if (
                tuple(connection.execute(f"PRAGMA table_xinfo({table})"))
                != _TABLE_XINFO[table]
            ):
                return False
            if (
                tuple(connection.execute(f"PRAGMA foreign_key_list({table})"))
                != _FOREIGN_KEYS[table]
            ):
                return False
            indexes = tuple(connection.execute(f"PRAGMA index_list({table})"))
            if indexes != _INDEX_LIST[table]:
                return False
            for index in indexes:
                if (
                    tuple(connection.execute(f"PRAGMA index_xinfo('{index[1]}')"))
                    != _INDEX_XINFO[index[1]]
                ):
                    return False
        return True

    def _validate_database(self, connection: sqlite3.Connection) -> int:
        try:
            generation = self._detect_generation(connection)
            if generation is None:
                raise HoldingsSnapshotReadError("schema_mismatch")
            if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
                raise HoldingsSnapshotReadError("store_corrupt")
            if connection.execute("PRAGMA foreign_key_check").fetchone():
                raise HoldingsSnapshotReadError("store_corrupt")
            for row in connection.execute("SELECT snapshot_id FROM holdings_snapshots"):
                self._reconstruct(connection, row[0])
            return generation
        except HoldingsSnapshotReadError:
            raise
        except sqlite3.DatabaseError as error:
            raise HoldingsSnapshotReadError("store_corrupt") from error

    def _reconstruct(
        self,
        connection: sqlite3.Connection,
        snapshot_id: str,
    ) -> CompleteHoldingsSnapshot:
        parent = connection.execute(
            "SELECT snapshot_id, source, captured_at, discovered_account_count, "
            "content_hash, criteria_version, purpose_kind, program_name, "
            "authorized_at, stored_at "
            "FROM holdings_snapshots WHERE snapshot_id=?",
            (snapshot_id,),
        ).fetchone()
        if parent is None:
            raise HoldingsSnapshotReadError("not_found")
        try:
            if not validate_snapshot_id(parent[0]) or not validate_program_name_pairing(
                criteria_version=parent[5],
                purpose_kind=parent[6],
                program_name=parent[7],
            ):
                raise ValueError
            captured_at = parse_utc_timestamp(parent[2])
            parse_utc_timestamp(parent[8])
            parse_utc_timestamp(parent[9])
            account_rows = connection.execute(
                "SELECT account_ordinal FROM holdings_snapshot_accounts "
                "WHERE snapshot_id=? ORDER BY account_ordinal",
                (snapshot_id,),
            ).fetchall()
            if [row[0] for row in account_rows] != list(range(1, parent[3] + 1)):
                raise ValueError
            accounts = []
            for (ordinal,) in account_rows:
                items = []
                for row in connection.execute(
                    "SELECT symbol, quantity, avg_price, avg_price_field, "
                    "currency, market_raw, market_field, name, asset_type "
                    "FROM holdings_snapshot_items WHERE snapshot_id=? "
                    "AND account_ordinal=? ORDER BY symbol",
                    (snapshot_id, ordinal),
                ):
                    (
                        symbol,
                        quantity,
                        avg_price,
                        avg_field,
                        currency,
                        market_raw,
                        market_field,
                        name,
                        asset_type,
                    ) = row
                    canonical_decimal_str(quantity)
                    canonical_decimal_str(avg_price)
                    if not (
                        isinstance(symbol, str)
                        and 1 <= len(symbol) <= 64
                        and isinstance(avg_field, str)
                        and avg_field
                        and ((market_raw is None) == (market_field is None))
                        and validate_nullable_nonempty_text(
                            currency, min_len=1, max_len=16
                        )
                        and validate_nullable_nonempty_text(
                            market_raw, min_len=1, max_len=16
                        )
                        and validate_nullable_nonempty_text(
                            market_field, min_len=1, max_len=64
                        )
                        and validate_nullable_nonempty_text(
                            name, min_len=1, max_len=256
                        )
                        and validate_nullable_nonempty_text(
                            asset_type, min_len=1, max_len=64
                        )
                    ):
                        raise ValueError
                    items.append(
                        TossHoldingFact(
                            symbol,
                            quantity,
                            avg_price,
                            avg_field,
                            currency,
                            market_raw,
                            market_field,
                            name,
                            asset_type,
                        )
                    )
                accounts.append(HoldingsSnapshotAccount(ordinal, tuple(items)))
            value = CompleteHoldingsSnapshot(
                parent[0], parent[1], captured_at, parent[3], tuple(accounts), parent[4]
            )
            if snapshot_content_hash(value) != parent[4]:
                raise ValueError
            return value
        except (TypeError, ValueError):
            raise HoldingsSnapshotReadError("store_corrupt") from None

    def _open_read(self) -> sqlite3.Connection:
        return self._open_read_generation()[0]

    def _open_read_generation(self) -> tuple[sqlite3.Connection, int]:
        if not os.path.lexists(self._path):
            raise HoldingsSnapshotReadError("not_found")
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            raise HoldingsSnapshotReadError("store_corrupt")
        try:
            connection = connect_private(self._path, mode="ro")
            return connection, self._validate_database(connection)
        except HoldingsSnapshotReadError:
            raise
        except sqlite3.Error as error:
            reason = "store_locked" if sqlite_locked(error) else "store_corrupt"
            raise HoldingsSnapshotReadError(reason) from error
        except OSError as error:
            raise HoldingsSnapshotReadError("store_corrupt") from error

    def _valid_write(
        self, snapshot: object, permit: object, *, migration: bool
    ) -> str | None:
        if not isinstance(snapshot, CompleteHoldingsSnapshot):
            return "invalid_snapshot"
        if not isinstance(permit, HoldingsPersistencePermitV1):
            return "invalid_permit"
        if not validate_permit(permit, snapshot_id=snapshot.snapshot_id):
            return "invalid_permit"
        if migration != (permit.criteria_version == MIGRATION_CRITERIA_VERSION):
            return "invalid_permit"
        # Source and criteria must agree. The DDL couples criteria to purpose
        # but says nothing about source, so without this a pension capture
        # could be persisted under a Toss user-request permit and its
        # provenance would no longer identify the lane that produced it.
        if (snapshot.source == PENSION_SNAPSHOT_SOURCE) != (
            permit.criteria_version == PENSION_CRITERIA_VERSION
        ):
            return "invalid_permit"
        if snapshot_content_hash(snapshot) != snapshot.content_hash:
            return "invalid_snapshot"
        if migration:
            if (
                legacy_snapshot_id(canonical_snapshot_bytes(snapshot))
                != snapshot.snapshot_id
            ):
                return "invalid_snapshot"
        elif not is_valid_uuid4_hex(snapshot.snapshot_id):
            return "invalid_snapshot"
        return None

    def append(
        self, snapshot: CompleteHoldingsSnapshot, permit: HoldingsPersistencePermitV1
    ) -> HoldingsAppendReceipt:
        return self._append(snapshot, permit, migration=False)

    def import_legacy(
        self,
        snapshot: CompleteHoldingsSnapshot,
        permit: HoldingsPersistencePermitV1,
    ) -> HoldingsAppendReceipt:
        return self._append(snapshot, permit, migration=True)

    def _append(
        self,
        snapshot: CompleteHoldingsSnapshot,
        permit: HoldingsPersistencePermitV1,
        *,
        migration: bool,
    ) -> HoldingsAppendReceipt:
        stored_at = self._clock()
        snapshot_id = getattr(snapshot, "snapshot_id", "")
        invalid = self._valid_write(snapshot, permit, migration=migration)
        if invalid:
            return self._failure(snapshot_id, stored_at, invalid)
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            return self._failure(snapshot_id, stored_at, "store_write_failed")
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            return self._failure(snapshot_id, stored_at, "store_write_failed")
        exists = os.path.lexists(self._path)
        created_by_this_call = not exists
        new_file = not exists or (
            self._initialize_precreated and os.path.getsize(self._path) == 0
        )
        connection = None
        receipt = None
        committed_new_row = False

        def rollback_best_effort() -> None:
            if connection is None:
                return
            try:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass

        try:
            if not new_file:
                validation_connection = connect_private(self._path, mode="ro")
                try:
                    validation_connection.execute("PRAGMA busy_timeout=0")
                    generation = self._validate_database(validation_connection)
                finally:
                    validation_connection.close()
                # A pension capture is only storable once the store has been
                # explicitly migrated. On a generation-1 store this fails
                # closed rather than widening the schema implicitly.
                if (
                    snapshot.source == PENSION_SNAPSHOT_SOURCE
                    and generation < SCHEMA_GENERATION_TWO
                ):
                    return self._failure(snapshot_id, stored_at, "schema_mismatch")
            elif snapshot.source == PENSION_SNAPSHOT_SOURCE:
                # A store created by this call is generation 1 by definition.
                return self._failure(snapshot_id, stored_at, "schema_mismatch")
            if new_file and not exists:
                with private_creation():
                    connection = connect_private(self._path)
                    os.chmod(self._path, 0o600)
            elif new_file:
                connection = connect_private(self._path)
            else:
                connection = connect_private(self._path)
            if new_file:
                self._initialize(connection)
                self._validate_database(connection)
            transaction_identity = path_identity(self._path)
            acquire_immediate_transaction(connection)
            existing = connection.execute(
                "SELECT content_hash, criteria_version, purpose_kind, "
                "program_name, authorized_at FROM holdings_snapshots "
                "WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            metadata = (
                snapshot.content_hash,
                permit.criteria_version,
                permit.purpose_kind.value,
                permit.program_name,
                canonical_utc_isoformat(permit.authorized_at),
            )
            if existing is not None:
                reconstructed = self._reconstruct(connection, snapshot_id)
                status = (
                    "DUPLICATE"
                    if existing == metadata
                    and canonical_snapshot_bytes(reconstructed)
                    == canonical_snapshot_bytes(snapshot)
                    else "CONFLICT"
                )
                connection.execute("ROLLBACK")
                receipt = self._receipt(status, snapshot_id, stored_at, status.lower())
            else:
                connection.execute(
                    "INSERT INTO holdings_snapshots VALUES "
                    "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (
                        snapshot_id,
                        snapshot.source,
                        canonical_utc_isoformat(snapshot.captured_at),
                        snapshot.discovered_account_count,
                        snapshot.content_hash,
                        permit.criteria_version,
                        permit.purpose_kind.value,
                        permit.program_name,
                        canonical_utc_isoformat(permit.authorized_at),
                        canonical_utc_isoformat(stored_at),
                    ),
                )
                for account in snapshot.accounts:
                    connection.execute(
                        "INSERT INTO holdings_snapshot_accounts VALUES (?, ?)",
                        (snapshot_id, account.account_ordinal),
                    )
                    for item in sorted(
                        account.holdings, key=lambda value: value.symbol
                    ):
                        connection.execute(
                            "INSERT INTO holdings_snapshot_items VALUES "
                            "(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                            (
                                snapshot_id,
                                account.account_ordinal,
                                item.symbol,
                                canonical_decimal(Decimal(item.quantity)),
                                canonical_decimal(Decimal(item.avg_price)),
                                item.avg_price_field,
                                item.currency,
                                item.market_raw,
                                item.market_field,
                                item.name,
                                item.asset_type,
                            ),
                        )
                connection.execute("COMMIT")
                committed_new_row = True
                if path_identity(self._path) != transaction_identity:
                    acquire_immediate_transaction(connection)
                    connection.execute(
                        "DELETE FROM holdings_snapshot_items WHERE snapshot_id=?",
                        (snapshot_id,),
                    )
                    connection.execute(
                        "DELETE FROM holdings_snapshot_accounts WHERE snapshot_id=?",
                        (snapshot_id,),
                    )
                    connection.execute(
                        "DELETE FROM holdings_snapshots WHERE snapshot_id=?",
                        (snapshot_id,),
                    )
                    connection.execute("COMMIT")
                    committed_new_row = False
                    raise OSError("unsafe_path_swap")
                receipt = self._receipt("SAVED", snapshot_id, stored_at)
        except sqlite3.Error as error:
            rollback_best_effort()
            reason = "store_locked" if sqlite_locked(error) else "store_write_failed"
            receipt = self._failure(snapshot_id, stored_at, reason)
        except HoldingsSnapshotReadError as error:
            rollback_best_effort()
            receipt = self._failure(snapshot_id, stored_at, error.reason)
        except (OSError, ValueError):
            rollback_best_effort()
            receipt = self._failure(snapshot_id, stored_at, "store_write_failed")
        finally:
            if connection is not None:
                try:
                    connection.close()
                except (sqlite3.Error, OSError):
                    if receipt is None or receipt.status == "SAVED":
                        receipt = self._failure(
                            snapshot_id, stored_at, "store_write_failed"
                        )
            if new_file and os.path.lexists(self._path):
                try:
                    fsync_directory(self._path.parent)
                except OSError:
                    receipt = self._failure(
                        snapshot_id, stored_at, "store_write_failed"
                    )
                    if committed_new_row and created_by_this_call:
                        try:
                            self._path.unlink()
                        except OSError:
                            pass
        return receipt or self._failure(snapshot_id, stored_at, "store_write_failed")

    def read_snapshot(self, snapshot_id: str) -> CompleteHoldingsSnapshot:
        """Read one capture by id, of either source.

        Reading by explicit id can never *accidentally* return a pension row,
        so this stays source-agnostic; the ordered/aggregate readers below are
        the ones that must be scoped.
        """
        connection = self._open_read()
        try:
            return self._reconstruct(connection, snapshot_id)
        finally:
            connection.close()

    # ------------------------------------------------------------------
    # Toss-scoped readers.
    #
    # These are the pre-existing consumer APIs (latest / pair / report / SEC).
    # On a generation-1 store every row is Toss, so the added filter is a
    # no-op and their behaviour is bit-for-bit what it always was. On a
    # generation-2 store the filter is what guarantees a pension capture can
    # never be silently served to a Toss consumer.
    # ------------------------------------------------------------------

    def read_latest(self) -> CompleteHoldingsSnapshot:
        return self._read_latest_for(TOSS_SNAPSHOT_SOURCE)

    def read_latest_pair(
        self,
    ) -> tuple[CompleteHoldingsSnapshot | None, CompleteHoldingsSnapshot | None]:
        return self._read_latest_pair_for(TOSS_SNAPSHOT_SOURCE)

    def list_capture_headers(
        self,
        limit: int = 100,
    ) -> tuple[HoldingsSnapshotHeader, ...]:
        return self._list_capture_headers_for(TOSS_SNAPSHOT_SOURCE, limit)

    # ------------------------------------------------------------------
    # Pension-scoped readers (generation 2 only).
    # ------------------------------------------------------------------

    def read_latest_pension_snapshot(self) -> CompleteHoldingsSnapshot:
        return self._read_latest_for(
            PENSION_SNAPSHOT_SOURCE, require_generation=SCHEMA_GENERATION_TWO
        )

    def read_latest_pension_pair(
        self,
    ) -> tuple[CompleteHoldingsSnapshot | None, CompleteHoldingsSnapshot | None]:
        return self._read_latest_pair_for(
            PENSION_SNAPSHOT_SOURCE, require_generation=SCHEMA_GENERATION_TWO
        )

    def list_pension_capture_headers(
        self,
        limit: int = 100,
    ) -> tuple[HoldingsSnapshotHeader, ...]:
        return self._list_capture_headers_for(
            PENSION_SNAPSHOT_SOURCE, limit, require_generation=SCHEMA_GENERATION_TWO
        )

    # ------------------------------------------------------------------

    def _open_scoped(self, require_generation: int | None):
        connection, generation = self._open_read_generation()
        if require_generation is not None and generation < require_generation:
            connection.close()
            raise HoldingsSnapshotReadError("schema_mismatch")
        return connection

    def _read_latest_for(
        self,
        source: str,
        *,
        require_generation: int | None = None,
    ) -> CompleteHoldingsSnapshot:
        connection = self._open_scoped(require_generation)
        try:
            row = connection.execute(
                "SELECT snapshot_id FROM holdings_snapshots WHERE source=? "
                "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 1",
                (source,),
            ).fetchone()
            if row is None:
                raise HoldingsSnapshotReadError("not_found")
            return self._reconstruct(connection, row[0])
        finally:
            connection.close()

    def _read_latest_pair_for(
        self,
        source: str,
        *,
        require_generation: int | None = None,
    ) -> tuple[CompleteHoldingsSnapshot | None, CompleteHoldingsSnapshot | None]:
        connection = self._open_scoped(require_generation)
        try:
            rows = connection.execute(
                "SELECT snapshot_id FROM holdings_snapshots WHERE source=? "
                "ORDER BY captured_at DESC, snapshot_id DESC LIMIT 2",
                (source,),
            ).fetchall()
            values = [self._reconstruct(connection, row[0]) for row in rows]
            if not values:
                return None, None
            if len(values) == 1:
                return None, values[0]
            return values[1], values[0]
        finally:
            connection.close()

    def _list_capture_headers_for(
        self,
        source: str,
        limit: int,
        *,
        require_generation: int | None = None,
    ) -> tuple[HoldingsSnapshotHeader, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= 1000
        ):
            raise ValueError("limit_outside_contract")
        connection = self._open_scoped(require_generation)
        try:
            rows = connection.execute(
                "SELECT snapshot_id, source, captured_at, "
                "discovered_account_count, content_hash "
                "FROM holdings_snapshots WHERE source=? "
                "ORDER BY captured_at DESC, snapshot_id DESC LIMIT ?",
                (source, limit),
            ).fetchall()
            return tuple(
                HoldingsSnapshotHeader(
                    row[0], row[1], parse_utc_timestamp(row[2]), row[3], row[4]
                )
                for row in rows
            )
        finally:
            connection.close()
