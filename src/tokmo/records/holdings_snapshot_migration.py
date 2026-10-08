"""N3: Frozen v1/v2 offline migration functions and verifiers for
holdings_snapshots.

This module owns immutable _V1_DDL/_V1_METADATA copied from the current
generation-1 owner and immutable _GEN2_DDL/_GEN2_METADATA. It does NOT import
the new binary's active _DDL or _METADATA.

The offline migration tool (tools/migrate_holdings_snapshots_v1_to_v2.py)
imports these functions.
"""
from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path

import sqlite3

from tokmo.records.private_sqlite import (
    connect_private,
    prepare_private_path,
)
from tokmo.records.durability import fsync_directory


@dataclass(frozen=True)
class V1Inventory:
    path: Path
    sha256: str
    row_counts: dict[str, int]


@dataclass(frozen=True)
class BackupReceipt:
    backup_path: Path
    manifest_path: Path
    sha256: str


@dataclass(frozen=True)
class GenerationR2Inventory:
    path: Path
    row_counts: dict[str, int]


# Frozen generation-1 DDL, copied verbatim from the generation-1 owner and
# immutable here. It is deliberately NOT imported from the active binary: the
# whole point of this module is to compare a live file against what the store
# looked like BEFORE the binary changed.
_V1_DDL = (
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
_V1_METADATA = frozenset({
    ("holdings_snapshot_schema_version", "1"),
    ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
})

# The store carries its version in storage_metadata, not in user_version, so
# user_version must still be the SQLite default. A shifted value means someone
# has been in the file.
_EXPECTED_USER_VERSION = 0

# Frozen introspection expectations, identical for both generations because a
# CHECK body does not appear in any of these PRAGMA outputs.
_TABLE_XINFO = {
    "storage_metadata": (
        (0, "key", "TEXT", 1, None, 1, 0),
        (1, "value", "TEXT", 1, None, 0, 0),
    ),
    "holdings_snapshots": tuple(
        (cid, name, kind, 1, None, int(cid == 0), 0)
        for cid, (name, kind) in enumerate((
            ("snapshot_id", "TEXT"), ("source", "TEXT"),
            ("captured_at", "TEXT"), ("discovered_account_count", "INTEGER"),
            ("content_hash", "TEXT"), ("criteria_version", "TEXT"),
            ("purpose_kind", "TEXT"), ("program_name", "TEXT"),
            ("authorized_at", "TEXT"), ("stored_at", "TEXT"),
            ("schema_version", "INTEGER"),
        ))
    ),
    "holdings_snapshot_accounts": (
        (0, "snapshot_id", "TEXT", 1, None, 1, 0),
        (1, "account_ordinal", "INTEGER", 1, None, 2, 0),
    ),
    "holdings_snapshot_items": tuple(
        (cid, name, "TEXT" if cid != 1 else "INTEGER",
         int(cid <= 5), None, cid + 1 if cid <= 2 else 0, 0)
        for cid, name in enumerate((
            "snapshot_id", "account_ordinal", "symbol", "quantity",
            "avg_price", "avg_price_field", "currency", "market_raw",
            "market_field", "name", "asset_type",
        ))
    ),
}
_FOREIGN_KEYS = {
    "storage_metadata": (),
    "holdings_snapshots": (),
    "holdings_snapshot_accounts": (
        (0, 0, "holdings_snapshots", "snapshot_id", "snapshot_id",
         "NO ACTION", "RESTRICT", "NONE"),
    ),
    "holdings_snapshot_items": (
        (0, 0, "holdings_snapshot_accounts", "snapshot_id", "snapshot_id",
         "NO ACTION", "RESTRICT", "NONE"),
        (0, 1, "holdings_snapshot_accounts", "account_ordinal",
         "account_ordinal", "NO ACTION", "RESTRICT", "NONE"),
    ),
}
_INDEX_LIST = {
    "storage_metadata": (
        (0, "sqlite_autoindex_storage_metadata_1", 1, "pk", 0),),
    "holdings_snapshots": (
        (0, "holdings_snapshots_capture_order", 0, "c", 0),
        (1, "sqlite_autoindex_holdings_snapshots_1", 1, "pk", 0),
    ),
    "holdings_snapshot_accounts": (
        (0, "sqlite_autoindex_holdings_snapshot_accounts_1", 1, "pk", 0),),
    "holdings_snapshot_items": (
        (0, "sqlite_autoindex_holdings_snapshot_items_1", 1, "pk", 0),),
}
_INDEX_XINFO = {
    "sqlite_autoindex_storage_metadata_1": (
        (0, 0, "key", 0, "BINARY", 1), (1, -1, None, 0, "BINARY", 0)),
    "holdings_snapshots_capture_order": (
        (0, 2, "captured_at", 0, "BINARY", 1),
        (1, 0, "snapshot_id", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0)),
    "sqlite_autoindex_holdings_snapshots_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, -1, None, 0, "BINARY", 0)),
    "sqlite_autoindex_holdings_snapshot_accounts_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, 1, "account_ordinal", 0, "BINARY", 1),
        (2, -1, None, 0, "BINARY", 0)),
    "sqlite_autoindex_holdings_snapshot_items_1": (
        (0, 0, "snapshot_id", 0, "BINARY", 1),
        (1, 1, "account_ordinal", 0, "BINARY", 1),
        (2, 2, "symbol", 0, "BINARY", 1),
        (3, -1, None, 0, "BINARY", 0)),
}


def _normalized(sql: str) -> str:
    return " ".join(sql.split())

# Frozen generation-2 DDL and metadata
_GEN2_METADATA = frozenset({
    ("holdings_snapshot_schema_version", "2"),
    ("holdings_snapshot_adapter", "tokmo-holdings-snapshots-v1"),
})


def _assert_exact_schema(conn, ddl, metadata, *, label: str) -> None:
    """M-02: exact schema match, not a metadata-and-count sniff.

    Object inventory, per-object SQL, STRICT flags, table_xinfo, foreign-key
    lists, index lists and index_xinfo, metadata rows and user_version must all
    match the frozen generation. Anything unexpected — an added index, a
    relaxed CHECK, a dropped STRICT, a stray object, a shifted user_version —
    is rejected here, BEFORE any backup or install happens.
    """
    expected_objects = {
        ("table", "storage_metadata"): ddl[0],
        ("table", "holdings_snapshots"): ddl[1],
        ("table", "holdings_snapshot_accounts"): ddl[2],
        ("table", "holdings_snapshot_items"): ddl[3],
        ("index", "holdings_snapshots_capture_order"): ddl[4],
    }
    observed_rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_master").fetchall()
    observed_names = {(kind, name) for kind, name, _ in observed_rows}
    expected_names = set(expected_objects) | {
        ("index", name) for name in _INDEX_XINFO
        if name.startswith("sqlite_autoindex_")}
    if observed_names != expected_names:
        raise SidecarBuildError(f"{label}_object_inventory_mismatch")
    observed = {
        (kind, name): sql for kind, name, sql in observed_rows
        if not name.startswith("sqlite_autoindex_")}
    for key, sql in expected_objects.items():
        if _normalized(observed[key]) != _normalized(sql):
            raise SidecarBuildError(f"{label}_object_sql_mismatch")

    strict = {
        row[1]: row[5] for row in conn.execute("PRAGMA table_list")
        if row[1] in _MIGRATION_TABLES
    }
    if strict != {table: 1 for table in _MIGRATION_TABLES}:
        raise SidecarBuildError(f"{label}_strict_mismatch")

    for table in _MIGRATION_TABLES:
        if tuple(conn.execute(f"PRAGMA table_xinfo({table})")) != _TABLE_XINFO[table]:
            raise SidecarBuildError(f"{label}_table_xinfo_mismatch:{table}")
        if tuple(conn.execute(f"PRAGMA foreign_key_list({table})")) != _FOREIGN_KEYS[table]:
            raise SidecarBuildError(f"{label}_foreign_key_mismatch:{table}")
        indexes = tuple(conn.execute(f"PRAGMA index_list({table})"))
        if indexes != _INDEX_LIST[table]:
            raise SidecarBuildError(f"{label}_index_list_mismatch:{table}")
        for index in indexes:
            if tuple(conn.execute(
                    f"PRAGMA index_xinfo('{index[1]}')")) != _INDEX_XINFO[index[1]]:
                raise SidecarBuildError(f"{label}_index_xinfo_mismatch:{index[1]}")

    if set(conn.execute("SELECT key, value FROM storage_metadata")) != metadata:
        raise SidecarBuildError(f"{label}_metadata_mismatch")
    if conn.execute("PRAGMA user_version").fetchone()[0] != _EXPECTED_USER_VERSION:
        raise SidecarBuildError(f"{label}_user_version_mismatch")


def inspect_v1(path: Path) -> V1Inventory:
    """Open the source read-only and verify its schema exactly (M-02)."""
    resolved = path.resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"v1 store not found: {resolved}")
    raw = resolved.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    conn = connect_private(resolved, mode="ro")
    try:
        try:
            _assert_exact_schema(conn, _V1_DDL, _V1_METADATA, label="source")
        except SidecarBuildError as error:
            try:
                _assert_exact_schema(
                    conn, _GEN2_DDL, _GEN2_METADATA, label="source")
            except SidecarBuildError:
                raise error from None
            raise SidecarBuildError("source_not_generation_one") from error
        if conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise SidecarBuildError("source_corrupt")
        if conn.execute("PRAGMA foreign_key_check").fetchone():
            raise SidecarBuildError("source_corrupt")
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in _MIGRATION_TABLES
        }
    finally:
        conn.close()
    return V1Inventory(path=resolved, sha256=sha256, row_counts=counts)


def backup_v1(path: Path, backup: Path, manifest: Path) -> BackupReceipt:
    """Create a mode-0600 backup of the v1 store with SHA-256 manifest."""
    resolved = path.resolve()
    raw = resolved.read_bytes()
    sha256 = hashlib.sha256(raw).hexdigest()
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(raw)
    os.chmod(backup, 0o600)
    manifest_data = f"source={resolved}\nsha256={sha256}\n"
    manifest.write_text(manifest_data)
    os.chmod(manifest, 0o600)
    fsync_directory(backup.parent)
    return BackupReceipt(backup_path=backup, manifest_path=manifest, sha256=sha256)


_MIGRATION_TABLES = (
    "storage_metadata", "holdings_snapshots",
    "holdings_snapshot_accounts", "holdings_snapshot_items",
)

# Frozen generation-2 DDL, owned here and deliberately NOT imported from the
# active store binary. It differs from the frozen generation-1 schema in
# exactly one place: the ``source`` CHECK also admits the pension source.
_GEN2_DDL = (
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


class SidecarBuildError(ValueError):
    """Typed migration rejection."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _copy_table(source_conn, target_conn, table: str) -> int:
    columns = [
        row[1] for row in source_conn.execute(f"PRAGMA table_info({table})")
    ]
    placeholders = ", ".join("?" for _ in columns)
    rows = source_conn.execute(
        f"SELECT {', '.join(columns)} FROM {table}"
    ).fetchall()
    for row in rows:
        target_conn.execute(
            f"INSERT INTO {table} VALUES ({placeholders})", row)
    return len(rows)


def build_generation_r2_sidecar(v1: Path, sidecar: Path) -> GenerationR2Inventory:
    """Create a new mode-0600 SQLite file with generation-2 tables.

    Copies all data from v1 in FK-safe order. Never drops a table, never
    disables foreign keys, and never writes to the source. The caller is
    responsible for having proved that no process holds the source store.
    """
    source_path = Path(v1).resolve()
    target_path = Path(sidecar)
    if not source_path.exists():
        raise FileNotFoundError(f"source store not found: {source_path}")
    if target_path.exists():
        raise SidecarBuildError("sidecar_already_exists")

    source_conn = connect_private(source_path, mode="ro")
    try:
        try:
            _assert_exact_schema(
                source_conn, _V1_DDL, _V1_METADATA, label="source")
        except SidecarBuildError as error:
            # An already-migrated store is a distinct, friendlier answer than
            # "your schema is wrong"; anything else keeps its precise reason.
            try:
                _assert_exact_schema(
                    source_conn, _GEN2_DDL, _GEN2_METADATA, label="source")
            except SidecarBuildError:
                raise error from None
            raise SidecarBuildError("source_not_generation_one") from error
        if source_conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise SidecarBuildError("source_corrupt")
        if source_conn.execute("PRAGMA foreign_key_check").fetchone():
            raise SidecarBuildError("source_corrupt")

        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_conn = connect_private(target_path, mode="rwc")
        try:
            os.chmod(target_path, 0o600)
            target_conn.execute("BEGIN IMMEDIATE")
            for statement in _GEN2_DDL:
                target_conn.execute(statement)
            target_conn.executemany(
                "INSERT INTO storage_metadata(key, value) VALUES (?, ?)",
                sorted(_GEN2_METADATA),
            )
            counts = {"storage_metadata": len(_GEN2_METADATA)}
            # FK-safe order: parents before children.
            for table in ("holdings_snapshots", "holdings_snapshot_accounts",
                          "holdings_snapshot_items"):
                counts[table] = _copy_table(source_conn, target_conn, table)
            target_conn.execute("COMMIT")
        except BaseException:
            try:
                if target_conn.in_transaction:
                    target_conn.execute("ROLLBACK")
            except (sqlite3.Error, OSError):
                pass
            target_conn.close()
            try:
                target_path.unlink()
            except OSError:
                pass
            raise
        target_conn.close()
        fsync_directory(target_path.parent)
    finally:
        source_conn.close()
    return GenerationR2Inventory(path=target_path, row_counts=counts)


def verify_generation_r2_sidecar(
    v1: V1Inventory, sidecar: Path,
) -> GenerationR2Inventory:
    """Independently verify the generation-2 sidecar.

    Re-opens both files read-only and proves: generation-2 metadata, integrity
    and foreign-key cleanliness, identical row counts for every carried table,
    and byte-identical content for every snapshot row. The source is re-hashed
    to prove the build did not mutate it.
    """
    source_path = Path(v1.path).resolve()
    target_path = Path(sidecar)
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != v1.sha256:
        raise SidecarBuildError("source_mutated_during_build")

    target_conn = connect_private(target_path, mode="ro")
    source_conn = connect_private(source_path, mode="ro")
    try:
        # M-02: the sidecar is held to the same exactness as the source.
        _assert_exact_schema(
            target_conn, _GEN2_DDL, _GEN2_METADATA, label="sidecar")
        if target_conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise SidecarBuildError("sidecar_corrupt")
        if target_conn.execute("PRAGMA foreign_key_check").fetchone():
            raise SidecarBuildError("sidecar_corrupt")

        counts: dict[str, int] = {}
        for table in _MIGRATION_TABLES:
            observed = target_conn.execute(
                f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            counts[table] = observed
            if table == "storage_metadata":
                continue
            if observed != v1.row_counts[table]:
                raise SidecarBuildError(f"row_count_mismatch:{table}")
            columns = [
                row[1] for row in source_conn.execute(
                    f"PRAGMA table_info({table})")
            ]
            order = ", ".join(columns)
            query = f"SELECT {order} FROM {table} ORDER BY {order}"
            if (source_conn.execute(query).fetchall()
                    != target_conn.execute(query).fetchall()):
                raise SidecarBuildError(f"row_content_mismatch:{table}")
    finally:
        target_conn.close()
        source_conn.close()
    return GenerationR2Inventory(path=target_path, row_counts=counts)


def install_generation_r2_sidecar(path: Path, sidecar: Path) -> None:
    """Atomically replace the live store with the sidecar."""
    os.replace(sidecar, path)
    fsync_directory(path.parent)


def restore_v1_backup(path: Path, backup: Path, manifest: Path) -> None:
    """Restore the v1 backup file to the original path."""
    os.replace(backup, path)
    fsync_directory(path.parent)
