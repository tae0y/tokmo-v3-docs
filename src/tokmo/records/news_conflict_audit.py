"""Private append-only diagnostics for NEWS identity conflicts."""

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    connect_private,
    prepare_private_path,
    private_creation,
    table_is_strict,
)

_SCHEMA_ID = "news-conflict-audit-v1"
_SCHEMA = """
CREATE TABLE schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 schema_id TEXT NOT NULL, schema_version INTEGER NOT NULL,
 ddl_sha256 TEXT NOT NULL CHECK(length(ddl_sha256)=64),
 created_at TEXT NOT NULL CHECK(length(created_at)=32)
) STRICT;
CREATE TABLE news_conflicts (
 conflict_id TEXT PRIMARY KEY,
 observed_at TEXT NOT NULL CHECK(length(observed_at)=32),
 expires_at TEXT NOT NULL CHECK(length(expires_at)=32),
 connector_id TEXT,
 target_fingerprint TEXT NOT NULL CHECK(length(target_fingerprint)=64),
 match_count INTEGER NOT NULL CHECK(match_count BETWEEN 1 AND 3),
 match_by_article_id INTEGER NOT NULL CHECK(match_by_article_id IN (0,1)),
 match_by_url_hash INTEGER NOT NULL CHECK(match_by_url_hash IN (0,1)),
 match_by_content_hash INTEGER NOT NULL CHECK(match_by_content_hash IN (0,1)),
 matched_document_fingerprints_json TEXT NOT NULL CHECK(json_valid(matched_document_fingerprints_json)),
 changed_fields_json TEXT NOT NULL CHECK(json_valid(changed_fields_json)),
 old_published_at TEXT, new_published_at TEXT,
 old_publisher_host_hash TEXT, new_publisher_host_hash TEXT,
 old_title_fingerprint TEXT NOT NULL, new_title_fingerprint TEXT NOT NULL,
 incoming_article_id_hash TEXT, incoming_canonical_url_hash TEXT NOT NULL
) STRICT;
CREATE TRIGGER news_conflicts_no_update BEFORE UPDATE ON news_conflicts
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER news_conflicts_no_delete BEFORE DELETE ON news_conflicts
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""
_TABLES = {"schema_meta", "news_conflicts"}


def sidecar_path(canonical_path: Path) -> Path:
    path = Path(canonical_path)
    return path.with_name(f"{path.stem}.conflicts.sqlite3")


def _statements(script: str) -> list[str]:
    result, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            result.append(pending.strip())
            pending = ""
    if pending.strip():
        raise RuntimeError("schema_mismatch")
    return result


def _ddl_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    encoded = b"".join(
        b"\0".join(value.encode() for value in row) + b"\n" for row in rows
    )
    return hashlib.sha256(encoded).hexdigest()


def _expected_ddl_sha256() -> str:
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _statements(_SCHEMA):
            connection.execute(statement)
        return _ddl_sha256(connection)
    finally:
        connection.close()


_EXPECTED_DDL_SHA256 = _expected_ddl_sha256()


def _validate(connection: sqlite3.Connection) -> None:
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger'"
            )
        }
        meta = connection.execute(
            "SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta WHERE singleton=1"
        ).fetchone()
        if (
            tables != _TABLES
            or triggers != {"news_conflicts_no_update", "news_conflicts_no_delete"}
            or any(not table_is_strict(connection, name) for name in _TABLES)
            or meta != (_SCHEMA_ID, 1, _EXPECTED_DDL_SHA256)
            or _ddl_sha256(connection) != _EXPECTED_DDL_SHA256
            or connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)
        ):
            raise RuntimeError("schema_mismatch")
    except sqlite3.DatabaseError as error:
        raise RuntimeError("schema_mismatch") from error


def _open(path: Path, observed_at: str) -> sqlite3.Connection:
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
        raise RuntimeError("SQLite >= 3.37 required")
    safety = prepare_private_path(path)
    if safety.unsafe:
        raise OSError(safety.reason)
    created = not path.exists()
    with private_creation():
        connection = connect_private(path, mode="rwc")
    try:
        if created:
            os.chmod(path, 0o600)
        has_objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchone() != (0,)
        if has_objects:
            _validate(connection)
            return connection
        connection.execute("BEGIN IMMEDIATE")
        # A competing first creator may have completed while this connection
        # waited for the bootstrap lock.
        has_objects = connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchone() != (0,)
        if not has_objects:
            for statement in _statements(_SCHEMA):
                connection.execute(statement)
            connection.execute(
                "INSERT INTO schema_meta VALUES(1,?,?,?,?)",
                (_SCHEMA_ID, 1, _ddl_sha256(connection), observed_at),
            )
        connection.execute("COMMIT")
        _validate(connection)
        return connection
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        connection.close()
        raise


def append_conflict(canonical_path: Path, record: dict) -> str:
    """Durably append one already-redacted conflict record."""
    path = sidecar_path(canonical_path)
    try:
        connection = _open(path, record["observed_at"])
        try:
            connection.execute("BEGIN IMMEDIATE")
            columns = (
                "conflict_id",
                "observed_at",
                "expires_at",
                "connector_id",
                "target_fingerprint",
                "match_count",
                "match_by_article_id",
                "match_by_url_hash",
                "match_by_content_hash",
                "matched_document_fingerprints_json",
                "changed_fields_json",
                "old_published_at",
                "new_published_at",
                "old_publisher_host_hash",
                "new_publisher_host_hash",
                "old_title_fingerprint",
                "new_title_fingerprint",
                "incoming_article_id_hash",
                "incoming_canonical_url_hash",
            )
            intended = tuple(record[name] for name in columns)
            connection.execute(
                f"INSERT OR IGNORE INTO news_conflicts({','.join(columns)}) "
                f"VALUES({','.join('?' for _ in columns)})",
                intended,
            )
            stored = connection.execute(
                f"SELECT {','.join(columns)} FROM news_conflicts WHERE conflict_id=?",
                (record["conflict_id"],),
            ).fetchone()
            if stored != intended:
                raise RuntimeError("audit_row_mismatch")
            connection.execute("COMMIT")
            return record["conflict_id"]
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
    except BaseException as error:
        raise RuntimeError("conflict_audit_failed") from error


def expiry_for(observed_at: str) -> str:
    value = datetime.fromisoformat(observed_at)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("observed_at must be UTC")
    return (value + timedelta(days=90)).isoformat(timespec="microseconds")


def _read_conflicts(canonical_path: Path, *, limit: int = 100) -> list[dict]:
    """Private, bounded diagnostic seam; never used by relay/report surfaces."""
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be between 1 and 100")
    path = sidecar_path(canonical_path)
    if not path.exists():
        return []
    safety = prepare_private_path(path)
    if safety.unsafe:
        raise OSError(safety.reason)
    connection = connect_private(path, mode="ro")
    try:
        _validate(connection)
        connection.row_factory = sqlite3.Row
        rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM news_conflicts ORDER BY observed_at,conflict_id LIMIT ?",
                (limit,),
            )
        ]
        for row in rows:
            row["matched_document_fingerprints"] = json.loads(
                row.pop("matched_document_fingerprints_json")
            )
            row["changed_fields"] = json.loads(row.pop("changed_fields_json"))
        return rows
    finally:
        connection.close()
