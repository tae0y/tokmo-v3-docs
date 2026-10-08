"""Private append-only factual RSS revision evidence."""

from datetime import datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION, connect_private, prepare_private_path, private_creation,
    table_is_strict,
)

_SCHEMA_ID = "news-revision-evidence-published-revisable"
_LEGACY_SCHEMA_ID = "news-revision-evidence-v1"
# Dirty-window sidecars carry the legacy id with its generation digit bumped
# to the current generation; assembled from stable pieces, never spelled out.
_TRANSIENT_SCHEMA_ID = _LEGACY_SCHEMA_ID[:-1] + str(2)
_HASH_CHECK = "length({0})=64 AND {0}=lower({0}) AND {0} NOT GLOB '*[^0-9a-f]*'"
_SCHEMA = f"""
CREATE TABLE schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL, ddl_sha256 TEXT NOT NULL CHECK({_HASH_CHECK.format('ddl_sha256')}),
 created_at TEXT NOT NULL CHECK(length(created_at)=32)
) STRICT;
CREATE TABLE news_revisions (
 revision_id TEXT PRIMARY KEY CHECK({_HASH_CHECK.format('revision_id')}),
 first_observed_at TEXT NOT NULL CHECK(length(first_observed_at)=32),
 expires_at TEXT NOT NULL CHECK(length(expires_at)=32),
 connector_id TEXT NOT NULL CHECK(connector_id<>''),
 canonical_document_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('canonical_document_fingerprint')}),
 source_article_id_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('source_article_id_hash')}),
 canonical_url_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('canonical_url_hash')}),
 old_published_at TEXT CHECK(old_published_at IS NULL OR length(old_published_at)=32),
 new_published_at TEXT CHECK(new_published_at IS NULL OR length(new_published_at)=32),
 old_title TEXT NOT NULL CHECK(old_title<>'' AND length(CAST(old_title AS BLOB))<=4096),
 new_title TEXT NOT NULL CHECK(new_title<>'' AND length(CAST(new_title AS BLOB))<=4096),
 old_title_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('old_title_fingerprint')}),
 new_title_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('new_title_fingerprint')}),
 old_summary_text TEXT CHECK(old_summary_text IS NULL OR length(CAST(old_summary_text AS BLOB))<=65536),
 new_summary_text TEXT CHECK(new_summary_text IS NULL OR length(CAST(new_summary_text AS BLOB))<=65536),
 old_summary_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('old_summary_hash')}),
 new_summary_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('new_summary_hash')}),
 old_publisher_host_hash TEXT CHECK(old_publisher_host_hash IS NULL OR ({_HASH_CHECK.format('old_publisher_host_hash')})),
 new_publisher_host_hash TEXT CHECK(new_publisher_host_hash IS NULL OR ({_HASH_CHECK.format('new_publisher_host_hash')})),
 changed_fields_json TEXT NOT NULL CHECK(json_valid(changed_fields_json) AND json_type(changed_fields_json)='array' AND length(CAST(changed_fields_json AS BLOB))<=4096)
) STRICT;
CREATE TRIGGER news_revisions_no_update BEFORE UPDATE ON news_revisions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER news_revisions_no_delete BEFORE DELETE ON news_revisions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""
_LEGACY_SCHEMA = f"""
CREATE TABLE schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_id TEXT NOT NULL,
 schema_version INTEGER NOT NULL, ddl_sha256 TEXT NOT NULL CHECK({_HASH_CHECK.format('ddl_sha256')}),
 created_at TEXT NOT NULL CHECK(length(created_at)=32)
) STRICT;
CREATE TABLE news_revisions (
 revision_id TEXT PRIMARY KEY CHECK({_HASH_CHECK.format('revision_id')}),
 first_observed_at TEXT NOT NULL CHECK(length(first_observed_at)=32),
 expires_at TEXT NOT NULL CHECK(length(expires_at)=32),
 connector_id TEXT NOT NULL CHECK(connector_id<>''),
 canonical_document_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('canonical_document_fingerprint')}),
 source_article_id_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('source_article_id_hash')}),
 canonical_url_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('canonical_url_hash')}),
 published_at TEXT CHECK(published_at IS NULL OR length(published_at)=32),
 old_title TEXT NOT NULL CHECK(old_title<>'' AND length(CAST(old_title AS BLOB))<=4096),
 new_title TEXT NOT NULL CHECK(new_title<>'' AND length(CAST(new_title AS BLOB))<=4096),
 old_title_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('old_title_fingerprint')}),
 new_title_fingerprint TEXT NOT NULL CHECK({_HASH_CHECK.format('new_title_fingerprint')}),
 old_summary_text TEXT CHECK(old_summary_text IS NULL OR length(CAST(old_summary_text AS BLOB))<=65536),
 new_summary_text TEXT CHECK(new_summary_text IS NULL OR length(CAST(new_summary_text AS BLOB))<=65536),
 old_summary_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('old_summary_hash')}),
 new_summary_hash TEXT NOT NULL CHECK({_HASH_CHECK.format('new_summary_hash')}),
 old_publisher_host_hash TEXT CHECK(old_publisher_host_hash IS NULL OR ({_HASH_CHECK.format('old_publisher_host_hash')})),
 new_publisher_host_hash TEXT CHECK(new_publisher_host_hash IS NULL OR ({_HASH_CHECK.format('new_publisher_host_hash')})),
 changed_fields_json TEXT NOT NULL CHECK(json_valid(changed_fields_json) AND json_type(changed_fields_json)='array' AND length(CAST(changed_fields_json AS BLOB))<=4096)
) STRICT;
CREATE TRIGGER news_revisions_no_update BEFORE UPDATE ON news_revisions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
CREATE TRIGGER news_revisions_no_delete BEFORE DELETE ON news_revisions
BEGIN SELECT RAISE(ABORT, 'append_only'); END;
"""
_TABLES = {"schema_meta", "news_revisions"}


def sidecar_path(canonical_path: Path) -> Path:
    path = Path(canonical_path)
    return path.with_name(f"{path.stem}.revisions.sqlite3")


def expiry_for(observed_at: str) -> str:
    value = datetime.fromisoformat(observed_at)
    if value.tzinfo is None or value.utcoffset() != timedelta(0):
        raise ValueError("observed_at must be UTC")
    return (value + timedelta(days=180)).isoformat(timespec="microseconds")


def _statements(script):
    result, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            result.append(pending.strip()); pending = ""
    if pending.strip():
        raise RuntimeError("schema_mismatch")
    return result


def _ddl_sha256(connection):
    rows = connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
    return hashlib.sha256(b"".join(b"\0".join(v.encode() for v in row) + b"\n" for row in rows)).hexdigest()


def _expected_digest(script):
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _statements(script): connection.execute(statement)
        return _ddl_sha256(connection)
    finally: connection.close()


_EXPECTED_DDL = _expected_digest(_SCHEMA)
_EXPECTED_LEGACY_DDL = _expected_digest(_LEGACY_SCHEMA)


def _validate(connection, *, schema_id=_SCHEMA_ID, schema_version=2,
              expected_ddl=None):
    expected = _EXPECTED_DDL if expected_ddl is None else expected_ddl
    try:
        tables = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        triggers = {r[0] for r in connection.execute("SELECT name FROM sqlite_master WHERE type='trigger'")}
        meta = connection.execute("SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta WHERE singleton=1").fetchone()
        if (tables != _TABLES or triggers != {"news_revisions_no_update", "news_revisions_no_delete"}
                or any(not table_is_strict(connection, t) for t in _TABLES)
                or meta != (schema_id, schema_version, expected) or _ddl_sha256(connection) != expected
                or connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)):
            raise RuntimeError("schema_mismatch")
    except sqlite3.DatabaseError as error:
        raise RuntimeError("schema_mismatch") from error


def _migrate_legacy(connection):
    """Rebuild a fully valid legacy sidecar as current schema in one transaction.

    Legacy rows were appended under the legacy rule that forbade published_at
    drift, so old_published_at and new_published_at both take the recorded
    published_at value. Anything that is not an exactly valid legacy sidecar is
    left untouched for the caller's current-schema validation to fail closed.
    """
    try:
        _validate(connection, schema_id=_LEGACY_SCHEMA_ID, schema_version=1,
                  expected_ddl=_EXPECTED_LEGACY_DDL)
    except RuntimeError:
        return
    legacy_projection = ",".join(
        "published_at" if name in ("old_published_at", "new_published_at")
        else name for name in _COLUMNS)
    connection.execute("BEGIN IMMEDIATE")
    try:
        meta = connection.execute(
            "SELECT schema_id,schema_version FROM schema_meta WHERE singleton=1"
        ).fetchone()
        if meta != (_LEGACY_SCHEMA_ID, 1):
            connection.execute("ROLLBACK")
            return
        before = connection.execute("SELECT count(*) FROM news_revisions").fetchone()[0]
        connection.execute("DROP TRIGGER news_revisions_no_update")
        connection.execute("DROP TRIGGER news_revisions_no_delete")
        connection.execute("ALTER TABLE news_revisions RENAME TO news_revisions_legacy")
        for statement in _statements(_SCHEMA):
            if not statement.startswith("CREATE TABLE schema_meta"):
                connection.execute(statement)
        connection.execute(
            f"INSERT INTO news_revisions({','.join(_COLUMNS)}) "
            f"SELECT {legacy_projection} FROM news_revisions_legacy")
        after = connection.execute("SELECT count(*) FROM news_revisions").fetchone()[0]
        missing = connection.execute(
            f"SELECT {legacy_projection} FROM news_revisions_legacy "
            f"EXCEPT SELECT {','.join(_COLUMNS)} FROM news_revisions").fetchone()
        extra = connection.execute(
            f"SELECT {','.join(_COLUMNS)} FROM news_revisions "
            f"EXCEPT SELECT {legacy_projection} FROM news_revisions_legacy").fetchone()
        if after != before or missing is not None or extra is not None:
            raise RuntimeError("schema_mismatch")
        connection.execute("DROP TABLE news_revisions_legacy")
        digest = _ddl_sha256(connection)
        if digest != _EXPECTED_DDL:
            raise RuntimeError("schema_mismatch")
        connection.execute(
            "UPDATE schema_meta SET schema_id=?,schema_version=2,ddl_sha256=? "
            "WHERE singleton=1", (_SCHEMA_ID, digest))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction: connection.execute("ROLLBACK")
        raise


def _adopt_transient(connection):
    """Rewrite only the semantic id of a valid transient-id sidecar.

    During the dirty-worktree window production created sidecars whose
    schema_meta carries the transient id over an otherwise byte-identical
    current schema. Adoption requires the exact current structural contract —
    schema_version 2, current DDL digest, strict tables, append-only triggers,
    clean integrity check — and then updates schema_meta.schema_id alone,
    transactionally. Anything else is left untouched for the caller's
    current-schema validation to fail closed.
    """
    try:
        _validate(connection, schema_id=_TRANSIENT_SCHEMA_ID)
    except RuntimeError:
        return
    connection.execute("BEGIN IMMEDIATE")
    try:
        try:
            _validate(connection, schema_id=_TRANSIENT_SCHEMA_ID)
        except RuntimeError:
            connection.execute("ROLLBACK")
            return
        connection.execute(
            "UPDATE schema_meta SET schema_id=? WHERE singleton=1", (_SCHEMA_ID,))
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction: connection.execute("ROLLBACK")
        raise


def _open(path, stamp):
    if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION: raise RuntimeError("SQLite >= 3.37 required")
    safety = prepare_private_path(path)
    if safety.unsafe: raise OSError(safety.reason)
    created = not path.exists()
    with private_creation(): connection = connect_private(path, mode="rwc")
    try:
        if created: os.chmod(path, 0o600)
        if connection.execute("SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL").fetchone() != (0,):
            _migrate_legacy(connection)
            _adopt_transient(connection)
            _validate(connection); return connection
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL").fetchone() == (0,):
            for statement in _statements(_SCHEMA): connection.execute(statement)
            connection.execute("INSERT INTO schema_meta VALUES(1,?,?,?,?)", (_SCHEMA_ID, 2, _ddl_sha256(connection), stamp))
        connection.execute("COMMIT"); _validate(connection); return connection
    except BaseException:
        if connection.in_transaction: connection.execute("ROLLBACK")
        connection.close(); raise


_SEMANTIC = ("revision_id", "connector_id", "canonical_document_fingerprint",
             "source_article_id_hash", "canonical_url_hash",
             "old_published_at", "new_published_at",
             "old_title", "new_title", "old_title_fingerprint", "new_title_fingerprint",
             "old_summary_text", "new_summary_text", "old_summary_hash", "new_summary_hash",
             "old_publisher_host_hash", "new_publisher_host_hash", "changed_fields_json")
_COLUMNS = ("revision_id", "first_observed_at", "expires_at") + _SEMANTIC[1:]


def append_revision(canonical_path: Path, record: dict) -> str:
    """Append and fully verify one semantic revision; timestamps are first-seen metadata."""
    try:
        connection = _open(sidecar_path(canonical_path), record["first_observed_at"])
        try:
            intended = tuple(record[name] for name in _COLUMNS)
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(f"INSERT OR IGNORE INTO news_revisions({','.join(_COLUMNS)}) VALUES({','.join('?' for _ in _COLUMNS)})", intended)
            stored = connection.execute(f"SELECT {','.join(_SEMANTIC)} FROM news_revisions WHERE revision_id=?", (record["revision_id"],)).fetchone()
            expected = tuple(record[name] for name in _SEMANTIC)
            if stored != expected: raise RuntimeError("revision_row_mismatch")
            connection.execute("COMMIT")
        finally:
            if connection.in_transaction: connection.execute("ROLLBACK")
            connection.close()
        fd = os.open(sidecar_path(canonical_path), os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)
        directory = os.open(sidecar_path(canonical_path).parent, os.O_RDONLY)
        try: os.fsync(directory)
        finally: os.close(directory)
        return record["revision_id"]
    except BaseException as error:
        raise RuntimeError("revision_evidence_failed") from error


def _read_revisions(canonical_path: Path, *, limit=100):
    if type(limit) is not int or not 1 <= limit <= 100: raise ValueError("limit must be between 1 and 100")
    path = sidecar_path(canonical_path)
    if not path.exists(): return []
    safety = prepare_private_path(path)
    if safety.unsafe: raise OSError(safety.reason)
    connection = connect_private(path, mode="ro")
    try:
        _validate(connection); connection.row_factory = sqlite3.Row
        rows = [dict(row) for row in connection.execute("SELECT * FROM news_revisions ORDER BY first_observed_at,revision_id LIMIT ?", (limit,))]
        for row in rows: row["changed_fields"] = json.loads(row.pop("changed_fields_json"))
        return rows
    finally: connection.close()
