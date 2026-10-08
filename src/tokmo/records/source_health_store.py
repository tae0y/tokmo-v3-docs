"""Private per-source NEWS health state and notification delivery receipts."""

import hashlib
import os
from pathlib import Path
import sqlite3

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION, connect_private, prepare_private_path, private_creation,
    table_is_strict,
)
from tokmo.values.news_source_health import MAX_ATTEMPTS_PER_NOTIFICATION

_SCHEMA_ID = "news-source-health-v1"
_SCHEMA = """
CREATE TABLE schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1),
 schema_id TEXT NOT NULL CHECK(schema_id<>''),
 schema_version INTEGER NOT NULL CHECK(schema_version>=1),
 ddl_sha256 TEXT NOT NULL CHECK(length(ddl_sha256)=64 AND ddl_sha256=lower(ddl_sha256)
   AND ddl_sha256 NOT GLOB '*[^0-9a-f]*'),
 created_at TEXT NOT NULL CHECK(length(created_at)=32)
) STRICT;
CREATE TABLE source_health (
 source TEXT PRIMARY KEY CHECK(source<>''),
 state TEXT NOT NULL CHECK(state IN ('HEALTHY','DEGRADED')),
 consecutive_failures INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_failures>=0),
 consecutive_successes INTEGER NOT NULL DEFAULT 0 CHECK(consecutive_successes>=0),
 last_tick_id TEXT,
 last_outcome TEXT CHECK(last_outcome IN ('SUCCESS','FAILURE')),
 pending_failure_pairs TEXT CHECK(pending_failure_pairs IS NULL OR
   (pending_failure_pairs<>'' AND length(pending_failure_pairs)<=1024)),
 pending_fingerprint TEXT CHECK(pending_fingerprint IS NULL OR (length(pending_fingerprint)=64)),
 pending_fingerprint_ticks INTEGER NOT NULL DEFAULT 0 CHECK(pending_fingerprint_ticks>=0),
 first_failure_at TEXT CHECK(first_failure_at IS NULL OR length(first_failure_at)=32),
 incident_id TEXT CHECK(incident_id IS NULL OR incident_id<>''),
 incident_started_at TEXT CHECK(incident_started_at IS NULL OR length(incident_started_at)=32),
 incident_confirmed_at TEXT CHECK(incident_confirmed_at IS NULL OR length(incident_confirmed_at)=32),
 incident_fingerprint TEXT CHECK(incident_fingerprint IS NULL OR length(incident_fingerprint)=64),
 incident_last_error TEXT CHECK(incident_last_error IS NULL
   OR (incident_last_error<>'' AND length(incident_last_error)<=120)),
 incident_failing_ticks INTEGER NOT NULL DEFAULT 0 CHECK(incident_failing_ticks>=0),
 incident_alert_delivered_at TEXT CHECK(incident_alert_delivered_at IS NULL
   OR length(incident_alert_delivered_at)=32),
 last_processed_slot_utc TEXT CHECK(last_processed_slot_utc IS NULL
   OR length(last_processed_slot_utc)=32),
 updated_at TEXT NOT NULL CHECK(length(updated_at)=32),
 CHECK((state='DEGRADED')=(incident_id IS NOT NULL)),
 CHECK((incident_id IS NOT NULL)=(incident_started_at IS NOT NULL)),
 CHECK((incident_id IS NOT NULL)=(incident_confirmed_at IS NOT NULL)),
 CHECK((incident_id IS NOT NULL)=(incident_fingerprint IS NOT NULL)),
 CHECK((incident_id IS NOT NULL)=(incident_last_error IS NOT NULL)),
 CHECK(incident_id IS NOT NULL OR incident_failing_ticks=0),
 CHECK(incident_id IS NOT NULL OR incident_alert_delivered_at IS NULL),
 CHECK(incident_id IS NOT NULL OR last_processed_slot_utc IS NULL),
 CHECK(state='HEALTHY' OR first_failure_at IS NULL),
 CHECK((pending_failure_pairs IS NULL)=(pending_fingerprint IS NULL)),
 CHECK(first_failure_at IS NULL OR (pending_failure_pairs IS NOT NULL AND pending_fingerprint IS NOT NULL
   AND pending_fingerprint_ticks>0))
) STRICT;
CREATE TABLE notifications (
 notification_id TEXT PRIMARY KEY CHECK(notification_id<>''),
 incident_id TEXT NOT NULL CHECK(incident_id<>''),
 source TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('DEGRADED','FINGERPRINT_CHANGED','REMINDER','RECOVERED')),
 dedupe_key TEXT NOT NULL,
 body_text TEXT NOT NULL CHECK(body_text<>'' AND length(body_text)<=1200),
 created_at TEXT NOT NULL CHECK(length(created_at)=32),
 attempted_at TEXT CHECK(attempted_at IS NULL OR length(attempted_at)=32),
 settled_at TEXT CHECK(settled_at IS NULL OR length(settled_at)=32),
 status TEXT NOT NULL CHECK(status IN ('PENDING','ATTEMPTING','SENT','FAILED','UNKNOWN','SUPERSEDED')),
 attempt_count INTEGER NOT NULL DEFAULT 0 CHECK(attempt_count>=0),
 provider_message_id TEXT,
 failure_reason TEXT CHECK(failure_reason IS NULL OR length(failure_reason)<=64),
 CHECK((status='PENDING')=((attempted_at IS NULL) AND (settled_at IS NULL))),
 CHECK((status IN ('SENT','FAILED','UNKNOWN','SUPERSEDED'))=(settled_at IS NOT NULL)),
 CHECK((provider_message_id IS NOT NULL)=(status='SENT'))
) STRICT;
CREATE UNIQUE INDEX notifications_dedupe_uq ON notifications(dedupe_key);
"""
_TABLES = {"schema_meta", "source_health", "notifications"}
_HEALTH_COLUMNS = (
    "source", "state", "consecutive_failures", "consecutive_successes",
    "last_tick_id", "last_outcome", "pending_failure_pairs",
    "pending_fingerprint", "pending_fingerprint_ticks", "first_failure_at",
    "incident_id", "incident_started_at", "incident_confirmed_at",
    "incident_fingerprint", "incident_last_error", "incident_failing_ticks",
    "incident_alert_delivered_at", "last_processed_slot_utc", "updated_at",
)
_NOTIFICATION_COLUMNS = (
    "notification_id", "incident_id", "source", "kind", "dedupe_key",
    "body_text", "created_at", "attempted_at", "settled_at", "status",
    "attempt_count", "provider_message_id", "failure_reason",
)
_KIND_PRIORITY = ("CASE kind WHEN 'DEGRADED' THEN 0 WHEN 'FINGERPRINT_CHANGED'"
                  " THEN 1 WHEN 'RECOVERED' THEN 2 ELSE 3 END")


def _statements(script):
    result, pending = [], ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            result.append(pending.strip())
            pending = ""
    if pending.strip():
        raise RuntimeError("schema_mismatch")
    return result


def _ddl_sha256(connection):
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL "
        "ORDER BY type,name").fetchall()
    return hashlib.sha256(b"".join(
        b"\0".join(value.encode() for value in row) + b"\n" for row in rows
    )).hexdigest()


def _expected_digest(script):
    connection = sqlite3.connect(":memory:")
    try:
        for statement in _statements(script):
            connection.execute(statement)
        return _ddl_sha256(connection)
    finally:
        connection.close()


_EXPECTED_DDL = _expected_digest(_SCHEMA)


def _validate(connection):
    try:
        tables = {row[0] for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        meta = connection.execute(
            "SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta "
            "WHERE singleton=1").fetchone()
        if (tables != _TABLES
                or any(not table_is_strict(connection, table) for table in _TABLES)
                or meta != (_SCHEMA_ID, 1, _EXPECTED_DDL)
                or _ddl_sha256(connection) != _EXPECTED_DDL
                or connection.execute("PRAGMA integrity_check").fetchone() != ("ok",)):
            raise RuntimeError("schema_mismatch")
    except sqlite3.DatabaseError as error:
        raise RuntimeError("schema_mismatch") from error


def _verify_health_row(connection, row):
    stored = connection.execute(
        f"SELECT {','.join(_HEALTH_COLUMNS)} FROM source_health WHERE source=?",
        (row["source"],)).fetchone()
    if stored != tuple(row[name] for name in _HEALTH_COLUMNS):
        raise RuntimeError("health_row_mismatch")


class SourceHealthStore:
    def __init__(self, path):
        self._path = Path(path)

    def _open(self, stamp):
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError("schema_mismatch")
        safety = prepare_private_path(self._path)
        if safety.unsafe:
            raise OSError(safety.reason)
        created = not self._path.exists()
        with private_creation():
            connection = connect_private(self._path, mode="rwc")
        try:
            if created:
                os.chmod(self._path, 0o600)
            populated = "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
            if connection.execute(populated).fetchone() == (0,):
                connection.execute("BEGIN IMMEDIATE")
                if connection.execute(populated).fetchone() == (0,):
                    for statement in _statements(_SCHEMA):
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_meta VALUES(1,?,?,?,?)",
                        (_SCHEMA_ID, 1, _ddl_sha256(connection), stamp))
                connection.execute("COMMIT")
            _validate(connection)
            return connection
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise

    def _commit_and_sync(self, connection):
        connection.execute("COMMIT")
        connection.close()
        for target in (self._path, self._path.parent):
            fd = os.open(target, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)

    def _mutate(self, now, work):
        connection = self._open(now)
        try:
            connection.execute("BEGIN IMMEDIATE")
            result = work(connection)
            self._commit_and_sync(connection)
            return result
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            connection.close()
            raise

    def _read(self, now, work):
        connection = self._open(now)
        try:
            return work(connection)
        finally:
            connection.close()

    def read_health(self, source, *, now):
        def work(connection):
            stored = connection.execute(
                f"SELECT {','.join(_HEALTH_COLUMNS)} FROM source_health "
                "WHERE source=?", (source,)).fetchone()
            return None if stored is None else dict(zip(_HEALTH_COLUMNS, stored))
        return self._read(now, work)

    def read_notifications(self, incident_id, *, now):
        def work(connection):
            rows = connection.execute(
                f"SELECT {','.join(_NOTIFICATION_COLUMNS)} FROM notifications "
                "WHERE incident_id=? ORDER BY created_at,notification_id",
                (incident_id,)).fetchall()
            return tuple(dict(zip(_NOTIFICATION_COLUMNS, row)) for row in rows)
        return self._read(now, work)

    def commit_tick(self, row, *, mints, supersede_ids, now):
        def work(connection):
            connection.execute(
                f"INSERT OR REPLACE INTO source_health({','.join(_HEALTH_COLUMNS)}) "
                f"VALUES({','.join('?' for _ in _HEALTH_COLUMNS)})",
                tuple(row[name] for name in _HEALTH_COLUMNS))
            for notification_id in supersede_ids:
                connection.execute(
                    "UPDATE notifications SET status='SUPERSEDED',settled_at=?,"
                    "failure_reason=NULL WHERE notification_id=? AND status IN "
                    "('PENDING','ATTEMPTING','FAILED')", (now, notification_id))
            for record in mints:
                connection.execute(
                    "INSERT OR IGNORE INTO notifications(notification_id,"
                    "incident_id,source,kind,dedupe_key,body_text,created_at,"
                    "status,attempt_count) VALUES(?,?,?,?,?,?,?,'PENDING',0)",
                    tuple(record[name] for name in (
                        "notification_id", "incident_id", "source", "kind",
                        "dedupe_key", "body_text", "created_at")))
                minted = connection.execute(
                    "SELECT count(*) FROM notifications WHERE dedupe_key=?",
                    (record["dedupe_key"],)).fetchone()
                if minted != (1,):
                    raise RuntimeError("notification_mint_mismatch")
            _verify_health_row(connection, row)
        self._mutate(now, work)

    def reconcile_delivery(self, *, stale_before, now):
        def work(connection):
            connection.execute(
                "UPDATE notifications SET status='UNKNOWN',settled_at=?,"
                "failure_reason='attempt_stale_crash' WHERE status='ATTEMPTING' "
                "AND attempted_at<?", (now, stale_before))
            connection.execute(
                "UPDATE notifications SET status='PENDING',attempted_at=NULL,"
                "settled_at=NULL,failure_reason=NULL WHERE status='FAILED' "
                "AND attempt_count<?", (MAX_ATTEMPTS_PER_NOTIFICATION,))
        self._mutate(now, work)

    def supersede_pending_before(self, before, *, now):
        """Retire unsent prior-night notices; preserve all history and attempts."""
        def work(connection):
            rows = connection.execute(
                "SELECT notification_id FROM notifications WHERE created_at<? "
                "AND status IN ('PENDING','FAILED')", (before,)).fetchall()
            connection.execute(
                "UPDATE notifications SET status='SUPERSEDED',settled_at=?,"
                "failure_reason=NULL WHERE created_at<? "
                "AND status IN ('PENDING','FAILED')", (now, before))
            return tuple(row[0] for row in rows)
        return self._mutate(now, work)

    def claim_next(self, source, *, now, prepare=None,
                   suppress_unseen_recovery=False):
        def work(connection):
            found = connection.execute(
                "SELECT notification_id FROM notifications WHERE source=? AND "
                f"status='PENDING' ORDER BY {_KIND_PRIORITY},created_at,"
                "notification_id LIMIT 1", (source,)).fetchone()
            if found is None:
                return None
            candidate = dict(zip(_NOTIFICATION_COLUMNS, connection.execute(
                f"SELECT {','.join(_NOTIFICATION_COLUMNS)} FROM notifications "
                "WHERE notification_id=?", (found[0],)).fetchone()))
            if suppress_unseen_recovery and candidate["kind"] == "RECOVERED":
                notified = connection.execute(
                    "SELECT 1 FROM notifications WHERE incident_id=? "
                    "AND kind!='RECOVERED' AND status IN "
                    "('SENT','UNKNOWN','ATTEMPTING') LIMIT 1",
                    (candidate["incident_id"],)).fetchone()
                if notified is None:
                    connection.execute(
                        "UPDATE notifications SET status='SUPERSEDED',settled_at=? "
                        "WHERE notification_id=?", (now, found[0]))
                    return None
            body = candidate["body_text"] if prepare is None else prepare(candidate)
            if body is None:
                # Deferral is not a provider attempt, failure, or delivery.
                return None
            connection.execute(
                "UPDATE notifications SET status='ATTEMPTING',attempted_at=?,"
                "attempt_count=attempt_count+1,body_text=? WHERE notification_id=?",
                (now, body, found[0]))
            claimed = connection.execute(
                f"SELECT {','.join(_NOTIFICATION_COLUMNS)} FROM notifications "
                "WHERE notification_id=?", (found[0],)).fetchone()
            return dict(zip(_NOTIFICATION_COLUMNS, claimed))
        return self._mutate(now, work)

    def settle(self, notification_id, status, *, now, provider_message_id=None,
               failure_reason=None):
        def work(connection):
            row = connection.execute(
                "SELECT kind,incident_id,source FROM notifications WHERE "
                "notification_id=? AND status='ATTEMPTING'",
                (notification_id,)).fetchone()
            if row is None:
                raise RuntimeError("settle_requires_attempting_row")
            connection.execute(
                "UPDATE notifications SET status=?,settled_at=?,"
                "provider_message_id=?,failure_reason=? WHERE notification_id=?",
                (status, now, provider_message_id, failure_reason,
                 notification_id))
            if status == "SENT" and row[0] == "DEGRADED":
                connection.execute(
                    "UPDATE source_health SET incident_alert_delivered_at=? "
                    "WHERE source=? AND incident_id=? AND "
                    "incident_alert_delivered_at IS NULL", (now, row[2], row[1]))
        self._mutate(now, work)

    def pending_sources(self, *, now):
        def work(connection):
            rows = connection.execute(
                "SELECT DISTINCT source FROM notifications WHERE "
                "status='PENDING' ORDER BY source").fetchall()
            return tuple(row[0] for row in rows)
        return self._read(now, work)
