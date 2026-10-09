"""Offline migration of the pension registry to the current read model.

Generation 1 keeps a full holdings payload on every revision row, including
superseded ones, so holdings history is duplicated between the registry and the
official snapshot store. Generation 2 keeps the payload of exactly one current
revision and reduces every other row to the canonical empty payload, while
preserving those rows for audit and reminder foreign keys.

The migration is offline and sidecar-based, exactly like the snapshot-store
migration: build a new file from the old one, verify it independently, then
install it atomically. The source is never written to. Nothing here runs at
runtime and nothing here is scheduled.

Refusals are typed and fail closed: a source that is not generation 1, a source
that fails integrity or foreign-key checks, a sidecar that already exists, or a
sidecar whose audit rows do not match the source.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.pension_revisions import (
    _DDL_STATEMENTS_GEN2,
    EMPTY_HOLDINGS_PAYLOAD,
    PENSION_REGISTRY_DDL_SHA256,
    PENSION_REGISTRY_DDL_SHA256_GEN2,
    SCHEMA_VERSION,
)
from tokmo.records.private_sqlite import connect_private

_CARRIED_TABLES = (
    "pension_revisions",
    "pension_activation_events",
    "pension_active_pointer",
    "pension_reminder_events",
)


@dataclass(frozen=True)
class RegistryInventory:
    path: Path
    sha256: str
    row_counts: dict[str, int]
    active_revision_id: str | None
    payload_bearing_rows: int


class RegistryMigrationError(ValueError):
    """Typed migration rejection."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _ddl_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type, name, sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type, name"
    ).fetchall()
    encoded = b"".join(
        b"\0".join(value.encode() for value in row) + b"\n" for row in rows
    )
    return hashlib.sha256(encoded).hexdigest()


def inspect_registry(path: Path) -> RegistryInventory:
    """Open the registry read-only and record exactly what must survive."""
    resolved = Path(path).resolve()
    if not resolved.exists():
        raise FileNotFoundError(f"registry not found: {resolved}")
    raw = resolved.read_bytes()
    connection = connect_private(resolved, mode="ro")
    try:
        if _ddl_sha256(connection) != PENSION_REGISTRY_DDL_SHA256:
            raise RegistryMigrationError("source_not_generation_one")
        if connection.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RegistryMigrationError("source_corrupt")
        if connection.execute("PRAGMA foreign_key_check").fetchone():
            raise RegistryMigrationError("source_corrupt")
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in _CARRIED_TABLES
        }
        active = connection.execute(
            "SELECT revision_id FROM pension_revisions WHERE state = 'ACTIVE'"
        ).fetchone()
        payloads = connection.execute(
            "SELECT count(*) FROM pension_revisions WHERE holdings_json <> ?",
            (EMPTY_HOLDINGS_PAYLOAD,),
        ).fetchone()[0]
    finally:
        connection.close()
    return RegistryInventory(
        path=resolved,
        sha256=hashlib.sha256(raw).hexdigest(),
        row_counts=counts,
        active_revision_id=active[0] if active else None,
        payload_bearing_rows=payloads,
    )


def build_current_read_model_sidecar(
    source: Path,
    sidecar: Path,
) -> RegistryInventory:
    """Build the generation-2 sidecar, carrying every audit row forward.

    Every revision row keeps its identity, state, timestamps and approval
    provenance. Only the holdings payload changes: the one current ACTIVE row
    keeps it, every other row is reduced to the canonical empty payload, so no
    historical symbols, quantities or average prices remain.
    """
    source_path = Path(source).resolve()
    target_path = Path(sidecar)
    if target_path.exists():
        raise RegistryMigrationError("sidecar_already_exists")
    inventory = inspect_registry(source_path)

    source_conn = connect_private(source_path, mode="ro")
    try:
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_conn = connect_private(target_path, mode="rwc")
        try:
            os.chmod(target_path, 0o600)
            target_conn.execute("BEGIN IMMEDIATE")
            for statement in _DDL_STATEMENTS_GEN2:
                target_conn.execute(statement)
            target_conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

            for table in _CARRIED_TABLES:
                columns = [
                    row[1] for row in source_conn.execute(f"PRAGMA table_info({table})")
                ]
                rows = source_conn.execute(
                    f"SELECT {', '.join(columns)} FROM {table}"
                ).fetchall()
                if table == "pension_revisions":
                    revision_index = columns.index("revision_id")
                    payload_index = columns.index("holdings_json")
                    rewritten = []
                    for row in rows:
                        values = list(row)
                        if values[revision_index] != inventory.active_revision_id:
                            values[payload_index] = EMPTY_HOLDINGS_PAYLOAD
                        rewritten.append(tuple(values))
                    rows = rewritten
                placeholders = ", ".join("?" for _ in columns)
                target_conn.executemany(
                    f"INSERT INTO {table}({', '.join(columns)}) "
                    f"VALUES ({placeholders})",
                    rows,
                )
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
    return inventory


def verify_current_read_model_sidecar(
    inventory: RegistryInventory,
    sidecar: Path,
) -> None:
    """Independently prove the sidecar before it is installed.

    Checks: the source was not mutated during the build; the sidecar carries
    the generation-2 schema; integrity and foreign keys are clean; every audit
    row survived; the one current revision kept its payload; and no other row
    carries holdings content.
    """
    source_path = Path(inventory.path)
    target_path = Path(sidecar)
    if hashlib.sha256(source_path.read_bytes()).hexdigest() != inventory.sha256:
        raise RegistryMigrationError("source_mutated_during_build")

    source_conn = connect_private(source_path, mode="ro")
    target_conn = connect_private(target_path, mode="ro")
    try:
        if _ddl_sha256(target_conn) != PENSION_REGISTRY_DDL_SHA256_GEN2:
            raise RegistryMigrationError("sidecar_not_current_read_model")
        if target_conn.execute("PRAGMA quick_check").fetchone() != ("ok",):
            raise RegistryMigrationError("sidecar_corrupt")
        if target_conn.execute("PRAGMA foreign_key_check").fetchone():
            raise RegistryMigrationError("sidecar_corrupt")

        for table in _CARRIED_TABLES:
            observed = target_conn.execute(f"SELECT count(*) FROM {table}").fetchone()[
                0
            ]
            if observed != inventory.row_counts[table]:
                raise RegistryMigrationError(f"row_count_mismatch:{table}")

        # B-03: the preserved families are compared column-for-column, in a
        # stable order, not by counts.
        for table, order in (
            ("pension_activation_events", "seq"),
            ("pension_active_pointer", "singleton"),
            ("pension_reminder_events", "reminder_id"),
        ):
            columns = [
                row[1] for row in source_conn.execute(f"PRAGMA table_info({table})")
            ]
            if columns != [
                row[1] for row in target_conn.execute(f"PRAGMA table_info({table})")
            ]:
                raise RegistryMigrationError(f"column_set_altered:{table}")
            query = f"SELECT {', '.join(columns)} FROM {table} ORDER BY {order}"
            if (
                source_conn.execute(query).fetchall()
                != target_conn.execute(query).fetchall()
            ):
                raise RegistryMigrationError(f"rows_altered:{table}")

        # pension_revisions: EVERY column is compared verbatim except the one
        # intentionally transformed column.
        #
        #   ALLOWED TRANSFORMATION (the only one):
        #     holdings_json -> '[]' for every row that is not the current
        #     ACTIVE revision. The current row's payload is carried verbatim.
        #
        #   NOT transformed, and therefore compared byte-for-byte: revision_id,
        #   schema_version, source_content_sha256, parser_version,
        #   sheet_schema_version, semantic_digest, reconciliation_digest,
        #   holding_count, liquidation_flag, source_as_of, uploaded_at,
        #   validated_at, approved_at, approval_reason, state, created_at,
        #   previous_active_revision_id.
        revision_columns = [
            row[1]
            for row in source_conn.execute("PRAGMA table_info(pension_revisions)")
        ]
        if revision_columns != [
            row[1]
            for row in target_conn.execute("PRAGMA table_info(pension_revisions)")
        ]:
            raise RegistryMigrationError("column_set_altered:pension_revisions")
        preserved = [name for name in revision_columns if name != "holdings_json"]
        query = (
            f"SELECT {', '.join(preserved)} FROM pension_revisions ORDER BY revision_id"
        )
        if (
            source_conn.execute(query).fetchall()
            != target_conn.execute(query).fetchall()
        ):
            raise RegistryMigrationError("audit_rows_altered")

        # And the one transformed column, row by row.
        source_payloads = dict(
            source_conn.execute(
                "SELECT revision_id, holdings_json FROM pension_revisions"
            )
        )
        target_payloads = dict(
            target_conn.execute(
                "SELECT revision_id, holdings_json FROM pension_revisions"
            )
        )
        if set(source_payloads) != set(target_payloads):
            raise RegistryMigrationError("revision_identity_altered")
        for revision_id, original in source_payloads.items():
            carried = target_payloads[revision_id]
            expected = (
                original
                if revision_id == inventory.active_revision_id
                else EMPTY_HOLDINGS_PAYLOAD
            )
            if carried != expected:
                raise RegistryMigrationError("payload_transformation_incorrect")

        payload_rows = target_conn.execute(
            "SELECT revision_id FROM pension_revisions WHERE holdings_json <> ?",
            (EMPTY_HOLDINGS_PAYLOAD,),
        ).fetchall()
        expected = (
            []
            if inventory.active_revision_id is None
            else [(inventory.active_revision_id,)]
        )
        if payload_rows != expected:
            raise RegistryMigrationError("payload_not_reduced_to_current")

        historical = target_conn.execute(
            "SELECT count(*) FROM pension_revisions "
            "WHERE state IN ('SUPERSEDED', 'REJECTED') AND holdings_json <> ?",
            (EMPTY_HOLDINGS_PAYLOAD,),
        ).fetchone()[0]
        if historical:
            raise RegistryMigrationError("historical_payload_retained")

        if inventory.active_revision_id is not None:
            carried = target_conn.execute(
                "SELECT holdings_json FROM pension_revisions WHERE revision_id=?",
                (inventory.active_revision_id,),
            ).fetchone()[0]
            original = source_conn.execute(
                "SELECT holdings_json FROM pension_revisions WHERE revision_id=?",
                (inventory.active_revision_id,),
            ).fetchone()[0]
            if carried != original:
                raise RegistryMigrationError("current_payload_altered")
    finally:
        source_conn.close()
        target_conn.close()


def install_current_read_model_sidecar(path: Path, sidecar: Path) -> None:
    """Atomically replace the live registry with the verified sidecar."""
    os.replace(sidecar, path)
    fsync_directory(Path(path).parent)
