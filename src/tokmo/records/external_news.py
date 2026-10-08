"""N1 NEWS-only private SQLite store."""

import hashlib
from contextlib import contextmanager
import json
import os
from pathlib import Path
import posixpath
import re
import sqlite3
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit
import uuid

from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION, acquire_immediate_transaction,
    check_private_path_without_open, connect_private,
    prepare_private_path, private_creation, table_is_strict, path_identity,
)
from tokmo.records import news_conflict_audit, news_revision_evidence
from tokmo.values.external_news import (
    NewsReceipt, NewsWrite, SaveOutcome, canonical_utc,
)
from tokmo.values.news_connectors import NewsConnectorSpec

_SCHEMA = """
CREATE TABLE schema_meta (
 singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL CHECK(schema_version>=1),
 manifest_revision INTEGER NOT NULL CHECK(manifest_revision>=1), schema_id TEXT NOT NULL CHECK(schema_id<>''),
 ddl_sha256 TEXT NOT NULL CHECK(length(ddl_sha256)=64 AND ddl_sha256=lower(ddl_sha256) AND ddl_sha256 NOT GLOB '*[^0-9a-f]*'),
 created_at TEXT NOT NULL CHECK(length(created_at)=32), migrated_at TEXT CHECK(migrated_at IS NULL OR length(migrated_at)=32)
) STRICT;
CREATE TABLE source_families (
 family_key TEXT PRIMARY KEY CHECK(family_key<>''), display_name TEXT NOT NULL CHECK(display_name<>''),
 adapter_key TEXT NOT NULL UNIQUE CHECK(adapter_key<>''), record_type_key TEXT NOT NULL UNIQUE CHECK(record_type_key<>''),
 enabled INTEGER NOT NULL DEFAULT 1 CHECK(enabled IN (0,1)), registered_at TEXT NOT NULL CHECK(length(registered_at)=32),
 disabled_at TEXT CHECK(disabled_at IS NULL OR length(disabled_at)=32), config_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(config_json) AND json_type(config_json)='object'),
 CHECK((enabled=1 AND disabled_at IS NULL) OR (enabled=0 AND disabled_at IS NOT NULL))
) STRICT;
CREATE TABLE sources (
 source_id TEXT PRIMARY KEY CHECK(source_id<>''), family_key TEXT NOT NULL REFERENCES source_families(family_key) ON UPDATE RESTRICT ON DELETE RESTRICT,
 source_native_key TEXT NOT NULL CHECK(source_native_key<>''), display_name TEXT NOT NULL CHECK(display_name<>''), organization_id TEXT,
 channel_owner_id TEXT, canonical_url TEXT, active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
 created_at TEXT NOT NULL CHECK(length(created_at)=32), updated_at TEXT NOT NULL CHECK(length(updated_at)=32), retired_at TEXT CHECK(retired_at IS NULL OR length(retired_at)=32),
 metadata_json TEXT NOT NULL DEFAULT '{}' CHECK(json_valid(metadata_json) AND json_type(metadata_json)='object'), UNIQUE(family_key,source_native_key),
 CHECK((active=1 AND retired_at IS NULL) OR (active=0 AND retired_at IS NOT NULL)), CHECK(updated_at>=created_at)
) STRICT;
CREATE INDEX sources_family_idx ON sources(family_key);
CREATE INDEX sources_org_idx ON sources(organization_id);
CREATE TABLE source_connectors (
 connector_id TEXT PRIMARY KEY CHECK(connector_id<>''),
 source_id TEXT NOT NULL REFERENCES sources(source_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 origin_kind TEXT NOT NULL CHECK(origin_kind IN ('RSS','API')),
 display_name TEXT NOT NULL CHECK(display_name<>''),
 canonical_url TEXT NOT NULL CHECK(canonical_url<>''),
 created_at TEXT NOT NULL CHECK(length(created_at)=32),
 UNIQUE(source_id,connector_id)
) STRICT;
CREATE TABLE intake_requests (
 intake_request_id TEXT PRIMARY KEY CHECK(intake_request_id<>''), origin_kind TEXT NOT NULL CHECK(origin_kind IN ('RSS','API','CHANNEL','DIRECT_URL','FILE','CONVERSATION','MANUAL')),
 connector_id TEXT REFERENCES source_connectors(connector_id) ON UPDATE RESTRICT ON DELETE RESTRICT, source_id TEXT REFERENCES sources(source_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 requested_by TEXT NOT NULL CHECK(requested_by IN ('SYSTEM','USER','PROGRAM')), intent TEXT NOT NULL CHECK(intent<>''), target_ref TEXT,
 target_fingerprint TEXT CHECK(target_fingerprint IS NULL OR (length(target_fingerprint)=64 AND target_fingerprint=lower(target_fingerprint) AND target_fingerprint NOT GLOB '*[^0-9a-f]*')),
 requested_at TEXT NOT NULL CHECK(length(requested_at)=32), request_state TEXT NOT NULL CHECK(request_state IN ('PENDING','RUNNING','COMPLETED','RETRYABLE','FAILED','CANCELLED')),
 idempotency_key TEXT NOT NULL UNIQUE CHECK(idempotency_key<>''), retention_class TEXT NOT NULL DEFAULT 'RECEIPT_90D' CHECK(retention_class IN ('RECEIPT_90D','LONG_TERM')),
 expires_at TEXT CHECK(expires_at IS NULL OR length(expires_at)=32), pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)),
 CHECK((origin_kind IN ('RSS','API','CHANNEL') AND connector_id IS NOT NULL) OR (origin_kind IN ('DIRECT_URL','FILE','CONVERSATION','MANUAL') AND connector_id IS NULL)),
 CHECK(origin_kind NOT IN ('DIRECT_URL','FILE') OR target_fingerprint IS NOT NULL)
) STRICT;
CREATE INDEX intake_requests_pending_idx ON intake_requests(request_state,requested_at) WHERE request_state IN ('PENDING','RETRYABLE');
CREATE INDEX intake_requests_expiry_idx ON intake_requests(expires_at) WHERE pinned=0 AND expires_at IS NOT NULL;
CREATE TABLE intake_receipts (
 intake_receipt_id TEXT PRIMARY KEY CHECK(intake_receipt_id<>''), intake_request_id TEXT NOT NULL REFERENCES intake_requests(intake_request_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 attempt_no INTEGER NOT NULL CHECK(attempt_no>=1), started_at TEXT NOT NULL CHECK(length(started_at)=32), finished_at TEXT CHECK(finished_at IS NULL OR length(finished_at)=32),
 status TEXT NOT NULL CHECK(status IN ('STARTED','COLLECTED','DUPLICATE','NO_CHANGE','RETRYABLE','FAILED')), failure_code TEXT, transport_status INTEGER,
 bytes_received INTEGER CHECK(bytes_received IS NULL OR bytes_received>=0), response_fingerprint TEXT CHECK(response_fingerprint IS NULL OR (length(response_fingerprint)=64 AND response_fingerprint=lower(response_fingerprint) AND response_fingerprint NOT GLOB '*[^0-9a-f]*')),
 source_record_type TEXT, source_record_id TEXT, retention_class TEXT NOT NULL DEFAULT 'RECEIPT_90D' CHECK(retention_class IN ('RECEIPT_90D','LONG_TERM')),
 expires_at TEXT CHECK(expires_at IS NULL OR length(expires_at)=32), pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)), UNIQUE(intake_request_id,attempt_no),
 CHECK((status='STARTED' AND finished_at IS NULL) OR (status<>'STARTED' AND finished_at IS NOT NULL)),
 CHECK((source_record_type IS NULL AND source_record_id IS NULL) OR (source_record_type IS NOT NULL AND source_record_type<>'' AND source_record_id IS NOT NULL AND source_record_id<>'')),
 CHECK((status IN ('RETRYABLE','FAILED') AND failure_code IS NOT NULL) OR (status NOT IN ('RETRYABLE','FAILED') AND failure_code IS NULL))
) STRICT;
CREATE INDEX intake_receipts_request_idx ON intake_receipts(intake_request_id,attempt_no);
CREATE INDEX intake_receipts_expiry_idx ON intake_receipts(expires_at) WHERE pinned=0 AND expires_at IS NOT NULL;
CREATE TABLE news_documents (
  news_document_id TEXT PRIMARY KEY CHECK(news_document_id<>''), source_id TEXT NOT NULL REFERENCES sources(source_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 source_article_id TEXT, canonical_url TEXT NOT NULL CHECK(canonical_url<>''), canonical_url_hash TEXT NOT NULL CHECK(length(canonical_url_hash)=64 AND canonical_url_hash=lower(canonical_url_hash) AND canonical_url_hash NOT GLOB '*[^0-9a-f]*'),
 publisher_organization_id TEXT, publisher_name TEXT, publisher_site_url TEXT,
 byline_person_id TEXT, syndication_origin_id TEXT REFERENCES news_documents(news_document_id) ON UPDATE RESTRICT ON DELETE RESTRICT,
 content_mode TEXT NOT NULL CHECK(content_mode IN ('NEWS','ANALYSIS','OPINION','INTERVIEW','PRESS_RELEASE','RUMOR')), title TEXT NOT NULL CHECK(title<>''),
 title_fingerprint TEXT NOT NULL CHECK(length(title_fingerprint)=64 AND title_fingerprint=lower(title_fingerprint) AND title_fingerprint NOT GLOB '*[^0-9a-f]*'), summary_text TEXT, body_text TEXT,
 content_hash TEXT CHECK(content_hash IS NULL OR (length(content_hash)=64 AND content_hash=lower(content_hash) AND content_hash NOT GLOB '*[^0-9a-f]*')),
 content_state TEXT NOT NULL CHECK(content_state IN ('METADATA_ONLY','BODY_AVAILABLE','BODY_EXPIRED','PAYWALLED','LICENSE_RESTRICTED','REDACTED')),
 license_state TEXT NOT NULL CHECK(license_state IN ('PERMITTED','UNKNOWN','RESTRICTED')),
 evidence_role TEXT NOT NULL CHECK(evidence_role IN ('FACT_DISCOVERY','OPINION_DISCOVERY')),
 alert_eligible INTEGER NOT NULL CHECK(alert_eligible IN (0,1)),
 time_interpretation TEXT NOT NULL CHECK(time_interpretation IN ('SOURCE_NATIVE','EXPLICIT_ZONE','ASSUMED_KST')),
 published_at TEXT CHECK(published_at IS NULL OR length(published_at)=32), updated_at TEXT CHECK(updated_at IS NULL OR length(updated_at)=32),
 first_seen_at TEXT NOT NULL CHECK(length(first_seen_at)=32), last_seen_at TEXT NOT NULL CHECK(length(last_seen_at)=32), analysis_state TEXT NOT NULL DEFAULT 'PENDING' CHECK(analysis_state IN ('PENDING','RETRYABLE','ANALYZED','INVALID','NOT_REQUIRED')),
 retention_class TEXT NOT NULL DEFAULT 'RAW_30D' CHECK(retention_class IN ('RAW_30D','METADATA_180D','LONG_TERM')), expires_at TEXT CHECK(expires_at IS NULL OR length(expires_at)=32), compacted_at TEXT CHECK(compacted_at IS NULL OR length(compacted_at)=32), pinned INTEGER NOT NULL DEFAULT 0 CHECK(pinned IN (0,1)), revision_no INTEGER NOT NULL DEFAULT 1 CHECK(revision_no>=1),
 CHECK((content_state='BODY_AVAILABLE' AND body_text IS NOT NULL) OR (content_state<>'BODY_AVAILABLE' AND body_text IS NULL)), CHECK(content_state<>'BODY_AVAILABLE' OR license_state='PERMITTED'),
 CHECK(last_seen_at>=first_seen_at), CHECK(syndication_origin_id IS NULL OR syndication_origin_id<>news_document_id)
) STRICT;
CREATE UNIQUE INDEX news_documents_source_article_uq ON news_documents(source_id,source_article_id) WHERE source_article_id IS NOT NULL;
CREATE UNIQUE INDEX news_documents_source_url_uq ON news_documents(source_id,canonical_url_hash);
CREATE INDEX news_documents_publisher_content_hash_idx ON news_documents(publisher_organization_id,content_hash) WHERE content_hash IS NOT NULL;
CREATE INDEX news_documents_analysis_idx ON news_documents(analysis_state,first_seen_at) WHERE analysis_state IN ('PENDING','RETRYABLE');
CREATE INDEX news_documents_expiry_idx ON news_documents(expires_at) WHERE pinned=0 AND expires_at IS NOT NULL;
"""

_EXPECTED_TABLES = frozenset({
    "schema_meta", "source_families", "sources", "intake_requests",
    "intake_receipts", "news_documents", "source_connectors",
})

_IDENTITY_SELECT = (
    "news_document_id,source_article_id,canonical_url,canonical_url_hash,"
    "publisher_name,publisher_site_url,"
    "content_mode,title,title_fingerprint,summary_text,body_text,content_hash,"
    "content_state,license_state,evidence_role,alert_eligible,time_interpretation,published_at"
)
_IDENTITY_FIELDS = (
    "source_article_id", "canonical_url", "canonical_url_hash", "publisher_name",
    "publisher_site_url", "content_mode", "title", "title_fingerprint",
    "summary_text", "body_text", "content_hash", "content_state", "license_state",
    "evidence_role", "alert_eligible", "time_interpretation", "published_at",
)
_FIELD_INDEXES = {name: index for index, name in enumerate(_IDENTITY_FIELDS)}
_ARTICLE_ID_INDEX = _FIELD_INDEXES["source_article_id"]
_URL_HASH_INDEX = _FIELD_INDEXES["canonical_url_hash"]


def _ddl_sha256(connection: sqlite3.Connection) -> str:
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master "
        "WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    encoded = b"".join(
        b"\0".join(field.encode("utf-8") for field in row) + b"\n"
        for row in rows
    )
    return hashlib.sha256(encoded).hexdigest()


def _validate_schema(connection: sqlite3.Connection) -> None:
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if tables != _EXPECTED_TABLES:
            raise RuntimeError("schema_mismatch")
        if any(not table_is_strict(connection, table) for table in _EXPECTED_TABLES):
            raise RuntimeError("schema_mismatch")
        meta = connection.execute(
            "SELECT schema_version,manifest_revision,schema_id,ddl_sha256 "
            "FROM schema_meta WHERE singleton=1"
        ).fetchone()
        if meta != (5, 5, "external-news-n5", _ddl_sha256(connection)):
            raise RuntimeError("schema_mismatch")
        registry = connection.execute(
            "SELECT family_key,display_name,adapter_key,record_type_key "
            "FROM source_families"
        ).fetchall()
        if registry != [(
            "NEWS", "News documents", "news_document_store", "news_document"
        )]:
            raise RuntimeError("schema_mismatch")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise RuntimeError("schema_mismatch")
    except sqlite3.DatabaseError as error:
        raise RuntimeError("schema_mismatch") from error


# SQLite splices an added column in after the last column definition and
# before the table constraints, so this text reproduces the n5 DDL of a freshly
# created store byte for byte -- which _validate_schema depends on.
_ADD_REVISION_NO = (
    "ALTER TABLE news_documents ADD COLUMN "
    "revision_no INTEGER NOT NULL DEFAULT 1 CHECK(revision_no>=1)"
)


def _migrate_n4_to_n5(connection: sqlite3.Connection, *, now) -> bool:
    """Give existing documents a revision counter. Returns whether it ran."""
    try:
        meta = connection.execute(
            "SELECT schema_version,schema_id FROM schema_meta WHERE singleton=1"
        ).fetchone()
    except sqlite3.DatabaseError as error:
        raise RuntimeError("schema_mismatch") from error
    if meta != (4, "external-news-n4"):
        return False
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(_ADD_REVISION_NO)
        connection.execute(
            "UPDATE schema_meta SET schema_version=5,manifest_revision=5,"
            "schema_id='external-news-n5',ddl_sha256=?,migrated_at=? "
            "WHERE singleton=1",
            (_ddl_sha256(connection), canonical_utc(now, "now")),
        )
        connection.execute("COMMIT")
    except BaseException:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    return True


def _schema_statements(script: str) -> list[str]:
    statements = []
    pending = ""
    for line in script.splitlines(keepends=True):
        pending += line
        if sqlite3.complete_statement(pending):
            statements.append(pending.strip())
            pending = ""
    if pending.strip():
        raise RuntimeError("schema_mismatch")
    return statements


class ExternalNewsStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self._batch_connection = None
        self._batch_identity = None
        self._validated_version = None

    @contextmanager
    def connector_batch(self):
        """Reuse integrity validation only on this batch's private connection.

        Every item still commits independently. Under each write lock, another
        connection's commit invalidates validation (including FK-off writes).
        No validation result survives closing the batch or replacing its file.
        """
        if self._batch_connection is not None:
            raise RuntimeError("nested_connector_batch")
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        identity = path_identity(self.path)
        connection = connect_private(self.path, mode="rw")
        try:
            if path_identity(self.path) != identity:
                raise OSError("unsafe_path_swap")
            self._batch_connection = connection
            self._batch_identity = identity
            yield
        finally:
            self._batch_connection = None
            self._batch_identity = None
            self._validated_version = None
            connection.close()

    @contextmanager
    def _connector_transaction(self):
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        batched = self._batch_connection is not None
        if batched and path_identity(self.path) != self._batch_identity:
            raise OSError("unsafe_path_swap")
        identity = self._batch_identity if batched else path_identity(self.path)
        connection = self._batch_connection if batched else connect_private(self.path, mode="rw")
        try:
            # Validate without a write reservation. Bind the result to the
            # version BEFORE the scan: a commit during any validation query
            # or before BEGIN must invalidate it. BEGIN then closes that race.
            # One restart accommodates a competing writer; continued churn
            # fails closed instead of scanning forever or using stale data.
            for attempt in range(2):
                version = connection.execute("PRAGMA data_version").fetchone()[0]
                if not batched or self._validated_version != version:
                    _validate_schema(connection)
                acquire_immediate_transaction(connection)
                # lstat only: opening and closing another descriptor for this
                # file would silently drop the POSIX lock BEGIN just took.
                safety = check_private_path_without_open(self.path)
                if safety.unsafe:
                    raise OSError(safety.reason)
                if path_identity(self.path) != identity:
                    raise OSError("unsafe_path_swap")
                if connection.execute("PRAGMA foreign_keys").fetchone() != (1,):
                    raise RuntimeError("schema_mismatch")
                if connection.execute("PRAGMA data_version").fetchone()[0] == version:
                    break
                connection.execute("ROLLBACK")
                self._validated_version = None
            else:
                raise sqlite3.OperationalError("storage_changed_during_validation")
            if batched:
                self._validated_version = version
            yield connection
        finally:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            if not batched:
                connection.close()

    def bootstrap(self, *, now) -> None:
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise RuntimeError("SQLite >= 3.37 required")
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        created = not self.path.exists()
        with private_creation():
            connection = connect_private(self.path, mode="rwc")
        try:
            if created:
                os.chmod(self.path, 0o600)
            elif connection.execute(
                "SELECT count(*) FROM sqlite_master WHERE sql IS NOT NULL"
            ).fetchone() == (0,):
                created = True
            else:
                _migrate_n4_to_n5(connection, now=now)
                _validate_schema(connection)
                return
            connection.execute("BEGIN IMMEDIATE")
            for statement in _schema_statements(_SCHEMA):
                connection.execute(statement)
            stamp = canonical_utc(now, "now")
            digest = _ddl_sha256(connection)
            connection.execute("INSERT INTO schema_meta VALUES(1,5,5,'external-news-n5',?,?,NULL)", (digest, stamp))
            connection.execute("INSERT INTO source_families VALUES('NEWS','News documents','news_document_store','news_document',1,?,NULL,'{}')", (stamp,))
            for table in _EXPECTED_TABLES:
                if not table_is_strict(connection, table):
                    raise RuntimeError("schema_mismatch")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise RuntimeError("schema_mismatch")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def read_document(self, news_document_id: str) -> dict | None:
        if not self.path.exists():
            return None
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        connection = connect_private(self.path, mode="ro")
        try:
            _validate_schema(connection)
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT * FROM news_documents WHERE news_document_id=?",
                (news_document_id,),
            ).fetchone()
            return dict(row) if row is not None else None
        finally:
            connection.close()

    def _after_revision_append(self, revision_id: str) -> None:
        """Crash-test seam after private evidence durability."""

    def _complete_revision_receipt(
        self, *, spec, source_id, stamp, target_ref, target_fingerprint,
        idempotency_key, bytes_received, existing_id, stored, payload,
        changed_names, revision_id,
    ) -> NewsReceipt:
        with self._connector_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources(source_id,family_key,source_native_key,display_name,canonical_url,created_at,updated_at) VALUES(?,'NEWS',?,?,?,?,?)",
                (source_id, spec.source_native_key, spec.display_name,
                 spec.canonical_url, stamp, stamp))
            source = connection.execute(
                "SELECT source_native_key,display_name,canonical_url FROM sources WHERE source_id=?",
                (source_id,)).fetchone()
            if source != (spec.source_native_key, spec.display_name, spec.canonical_url):
                raise RuntimeError("source_identity_mismatch")
            connection.execute(
                "INSERT OR IGNORE INTO source_connectors(connector_id,source_id,origin_kind,display_name,canonical_url,created_at) VALUES(?,?,?,?,?,?)",
                (spec.connector_id, source_id, spec.origin_kind.value,
                 spec.display_name, spec.canonical_url, stamp))
            connector = connection.execute(
                "SELECT source_id,origin_kind,display_name,canonical_url FROM source_connectors WHERE connector_id=?",
                (spec.connector_id,)).fetchone()
            if connector != (source_id, spec.origin_kind.value, spec.display_name, spec.canonical_url):
                raise RuntimeError("connector_identity_mismatch")
            current = connection.execute(
                f"SELECT {_IDENTITY_SELECT} FROM news_documents WHERE news_document_id=?",
                (existing_id,)).fetchone()
            if current is None or tuple(current[1:]) != stored:
                raise RuntimeError("revision_canonical_mismatch")
            # The evidence row is already durable; carrying the revised fields
            # into the document is what makes this a version rather than a
            # discarded write. Column names come from _IDENTITY_FIELDS, never
            # from the payload.
            assignments = ",".join(f"{name}=?" for name in changed_names)
            connection.execute(
                f"UPDATE news_documents SET {assignments},last_seen_at=?,"
                "revision_no=revision_no+1 WHERE news_document_id=?",
                (*(payload[_FIELD_INDEXES[name]] for name in changed_names),
                 stamp, existing_id),
            )
            prior = connection.execute(
                "SELECT intake_request_id FROM intake_requests WHERE idempotency_key=?",
                (idempotency_key,)).fetchone()
            request_id = (prior[0] if prior is not None else
                          _hash("v1", "intake-request", idempotency_key))
            connection.execute(
                "INSERT OR IGNORE INTO intake_requests(intake_request_id,origin_kind,connector_id,source_id,requested_by,intent,target_ref,target_fingerprint,requested_at,request_state,idempotency_key) VALUES(?,?,?,?, 'PROGRAM','SAVE_NEWS',?,?,?,'COMPLETED',?)",
                (request_id, spec.origin_kind.value, spec.connector_id, source_id,
                 target_ref, target_fingerprint, stamp, idempotency_key))
            request = connection.execute(
                "SELECT origin_kind,connector_id,source_id,target_ref,target_fingerprint,idempotency_key FROM intake_requests WHERE intake_request_id=?",
                (request_id,)).fetchone()
            if request != (spec.origin_kind.value, spec.connector_id, source_id,
                            target_ref, target_fingerprint, idempotency_key):
                raise RuntimeError("request_identity_mismatch")
            attempt_no = int(connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM intake_receipts WHERE intake_request_id=?",
                (request_id,)).fetchone()[0])
            receipt_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO intake_receipts(intake_receipt_id,intake_request_id,attempt_no,started_at,finished_at,status,bytes_received,response_fingerprint,source_record_type,source_record_id) VALUES(?,?,?,?,?,'DUPLICATE',?,?,'news_document',?)",
                (receipt_id, request_id, attempt_no, stamp, stamp, bytes_received,
                 revision_id, existing_id))
            connection.execute("COMMIT")
            return NewsReceipt(SaveOutcome.DUPLICATE, existing_id, request_id,
                               receipt_id, None, revision_id)

    def _audit_conflict(self, *, connector_id: str | None, target_fingerprint: str,
                        stamp: str, payload: tuple, matches_by_arm: tuple[list, list, list]) -> NewsReceipt:
        try:
            article_matches, url_matches, content_matches = matches_by_arm
            matches_by_id = {
                row[0]: row
                for row in article_matches + url_matches + content_matches
            }
            if not 1 <= len(matches_by_id) <= 3:
                raise RuntimeError("invalid_match_count")
            ordered = [matches_by_id[key] for key in sorted(matches_by_id)]
            reference = ordered[0]
            matched_fingerprints = sorted(
                _hash("v1", "matched-document", row[0]) for row in ordered)
            changed = [
                name for name, old, new
                in zip(_IDENTITY_FIELDS, reference[1:], payload) if old != new
            ]
            old_host = _normalized_host(reference[5])
            new_host = _normalized_host(payload[4])
            incoming_article_hash = (
                _hash("v1", "incoming-article-id", payload[0])
                if payload[0] is not None else None
            )
            inputs = (
                connector_id or "DIRECT_URL", target_fingerprint,
                incoming_article_hash or "", payload[2], payload[7],
                payload[10] or "", *matched_fingerprints, stamp,
            )
            conflict_id = _hash("v1", "news-conflict-id", *inputs)
            record = {
                "conflict_id": conflict_id, "observed_at": stamp,
                "expires_at": news_conflict_audit.expiry_for(stamp),
                "connector_id": connector_id,
                "target_fingerprint": target_fingerprint,
                "match_count": len(ordered),
                "match_by_article_id": int(bool(article_matches)),
                "match_by_url_hash": int(bool(url_matches)),
                "match_by_content_hash": int(bool(content_matches)),
                "matched_document_fingerprints_json": json.dumps(
                    matched_fingerprints, separators=(",", ":")),
                "changed_fields_json": json.dumps(
                    changed, separators=(",", ":")),
                "old_published_at": reference[17],
                "new_published_at": payload[16],
                "old_publisher_host_hash": (
                    _hash("v1", "publisher-host", old_host)
                    if old_host else None),
                "new_publisher_host_hash": (
                    _hash("v1", "publisher-host", new_host)
                    if new_host else None),
                "old_title_fingerprint": reference[8],
                "new_title_fingerprint": payload[7],
                "incoming_article_id_hash": incoming_article_hash,
                "incoming_canonical_url_hash": payload[2],
            }
            audit_id = news_conflict_audit.append_conflict(self.path, record)
        except BaseException as error:
            raise RuntimeError("conflict_audit_failed") from error
        return NewsReceipt(SaveOutcome.CONFLICT, reference[0], None, None, audit_id)

    def save_direct_url(self, value: NewsWrite) -> NewsReceipt:
        canonical_url, domain = _canonicalize_url(value.canonical_url)
        stamp = canonical_utc(value.observed_at, "observed_at")
        published = (canonical_utc(value.published_at, "published_at")
                     if value.published_at else None)
        url_hash = _hash("v1", canonical_url)
        title_hash = _hash("v1", "NEWS", " ".join(value.title.casefold().split()))
        source_id = _hash("source", domain)
        idempotency_key = _hash(
            "v1", "DIRECT_URL", "", source_id, url_hash, "SAVE_NEWS"
        )
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OSError(safety.reason)
        connection = connect_private(self.path, mode="rw")
        try:
            _validate_schema(connection)
            acquire_immediate_transaction(connection)
            prior = connection.execute(
                "SELECT intake_request_id FROM intake_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            request_id = prior[0] if prior is not None else uuid.uuid4().hex
            receipt_id = None
            if prior is not None:
                receipt = connection.execute(
                    "SELECT intake_receipt_id FROM intake_receipts WHERE intake_request_id=? AND attempt_no=1",
                    (request_id,),
                ).fetchone()
                receipt_id = receipt[0] if receipt is not None else None
            document_id = uuid.uuid4().hex
            connection.execute(
                "INSERT OR IGNORE INTO sources(source_id,family_key,source_native_key,display_name,canonical_url,created_at,updated_at) VALUES(?, 'NEWS', ?, ?, ?, ?, ?)",
                (source_id, f"domain:{domain}", domain, f"https://{domain}/", stamp, stamp),
            )
            if prior is None:
                connection.execute(
                    "INSERT INTO intake_requests(intake_request_id,origin_kind,connector_id,source_id,requested_by,intent,target_ref,target_fingerprint,requested_at,request_state,idempotency_key) VALUES(?,'DIRECT_URL',NULL,?,'PROGRAM','SAVE_NEWS',?,?,?,'COMPLETED',?)",
                    (request_id, source_id, canonical_url, url_hash, stamp,
                     idempotency_key),
                )
            article_matches = []
            if value.source_article_id is not None:
                article_matches = connection.execute(
                    f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                    "WHERE source_id=? AND source_article_id=?",
                    (source_id, value.source_article_id),
                ).fetchall()
            url_matches = connection.execute(
                f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                "WHERE source_id=? AND canonical_url_hash=?",
                (source_id, url_hash),
            ).fetchall()
            content_matches = []
            if value.content_hash is not None:
                # N1 provisional sources have no publisher organization yet.
                content_matches = connection.execute(
                    f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                    "WHERE source_id=? AND content_hash=?",
                    (source_id, value.content_hash),
                ).fetchall()
            matches_by_id = {row[0]: row for row in article_matches + url_matches + content_matches}
            if matches_by_id:
                existing = next(iter(matches_by_id.values()))
                payload = (
                    value.source_article_id, canonical_url, url_hash,
                    value.publisher_name, value.publisher_site_url,
                    value.content_mode.value, value.title, title_hash,
                    value.summary_text, value.body_text, value.content_hash,
                    value.content_state.value, value.license_state.value,
                    value.evidence_role.value, int(value.alert_eligible),
                    value.time_interpretation.value, published,
                )
                stored = tuple(existing[1:])
                if len(matches_by_id) == 1 and stored == payload:
                    existing_id = existing[0]
                    if receipt_id is None:
                        receipt_id = uuid.uuid4().hex
                        connection.execute(
                            "INSERT INTO intake_receipts(intake_receipt_id,intake_request_id,attempt_no,started_at,finished_at,status,source_record_type,source_record_id) VALUES(?,?,1,?,?,'DUPLICATE','news_document',?)",
                            (receipt_id, request_id, stamp, stamp, existing_id),
                        )
                    connection.execute("COMMIT")
                    return NewsReceipt(SaveOutcome.DUPLICATE, existing_id,
                                       request_id, receipt_id)
                connection.execute("ROLLBACK")
                return self._audit_conflict(
                    connector_id=None, target_fingerprint=url_hash, stamp=stamp,
                    payload=payload,
                    matches_by_arm=(article_matches, url_matches, content_matches),
                )
            connection.execute(
                "INSERT INTO news_documents(news_document_id,source_id,source_article_id,canonical_url,canonical_url_hash,publisher_name,publisher_site_url,content_mode,title,title_fingerprint,summary_text,body_text,content_hash,content_state,license_state,evidence_role,alert_eligible,time_interpretation,published_at,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, source_id, value.source_article_id, canonical_url,
                 url_hash, value.publisher_name, value.publisher_site_url,
                 value.content_mode.value, value.title, title_hash,
                 value.summary_text, value.body_text, value.content_hash,
                 value.content_state.value, value.license_state.value,
                 value.evidence_role.value, int(value.alert_eligible),
                 value.time_interpretation.value, published,
                 stamp, stamp),
            )
            receipt_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO intake_receipts(intake_receipt_id,intake_request_id,attempt_no,started_at,finished_at,status,source_record_type,source_record_id) VALUES(?,?,1,?,?,'COLLECTED','news_document',?)",
                (receipt_id, request_id, stamp, stamp, document_id),
            )
            connection.execute("COMMIT")
            return NewsReceipt(SaveOutcome.INSERTED, document_id, request_id, receipt_id)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def save_connector_news(
        self, spec: NewsConnectorSpec, value: NewsWrite, *, target_ref: str,
        bytes_received: int | None = None,
    ) -> NewsReceipt:
        if (type(target_ref) is not str or not target_ref.strip()
                or len(target_ref) > 500
                or any(ord(ch) < 32 or ord(ch) == 127 for ch in target_ref)):
            raise ValueError("target_ref must be non-empty, bounded, and control-free")
        if (bytes_received is not None
                and (type(bytes_received) is not int or bytes_received < 0)):
            raise ValueError("bytes_received must be a non-negative integer")
        canonical_url, _domain = _canonicalize_url(value.canonical_url)
        stamp = canonical_utc(value.observed_at, "observed_at")
        published = (canonical_utc(value.published_at, "published_at")
                     if value.published_at else None)
        url_hash = _hash("v1", canonical_url)
        title_hash = _hash("v1", "NEWS", " ".join(value.title.casefold().split()))
        source_id = _hash("source", spec.source_native_key)
        target_fingerprint = _hash("v1", target_ref)
        idempotency_key = _hash(
            "v1", spec.origin_kind.value, spec.connector_id, source_id,
            target_fingerprint, url_hash, "SAVE_NEWS",
        )
        with self._connector_transaction() as connection:
            connection.execute(
                "INSERT OR IGNORE INTO sources(source_id,family_key,source_native_key,display_name,canonical_url,created_at,updated_at) VALUES(?,'NEWS',?,?,?,?,?)",
                (source_id, spec.source_native_key, spec.display_name,
                 spec.canonical_url, stamp, stamp),
            )
            source = connection.execute(
                "SELECT source_native_key,display_name,canonical_url FROM sources "
                "WHERE source_id=?", (source_id,),
            ).fetchone()
            if source != (spec.source_native_key, spec.display_name,
                          spec.canonical_url):
                raise RuntimeError("source_identity_mismatch")
            connection.execute(
                "INSERT OR IGNORE INTO source_connectors(connector_id,source_id,origin_kind,display_name,canonical_url,created_at) VALUES(?,?,?,?,?,?)",
                (spec.connector_id, source_id, spec.origin_kind.value,
                 spec.display_name, spec.canonical_url, stamp),
            )
            connector = connection.execute(
                "SELECT source_id,origin_kind,display_name,canonical_url "
                "FROM source_connectors WHERE connector_id=?",
                (spec.connector_id,),
            ).fetchone()
            if connector != (source_id, spec.origin_kind.value, spec.display_name,
                              spec.canonical_url):
                raise RuntimeError("connector_identity_mismatch")
            prior = connection.execute(
                "SELECT intake_request_id FROM intake_requests WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            request_id = prior[0] if prior is not None else uuid.uuid4().hex
            attempt_no = 1 if prior is None else int(connection.execute(
                "SELECT COALESCE(MAX(attempt_no),0)+1 FROM intake_receipts "
                "WHERE intake_request_id=?", (request_id,),
            ).fetchone()[0])
            if prior is None:
                connection.execute(
                    "INSERT INTO intake_requests(intake_request_id,origin_kind,connector_id,source_id,requested_by,intent,target_ref,target_fingerprint,requested_at,request_state,idempotency_key) VALUES(?,?,?,?, 'PROGRAM','SAVE_NEWS',?,?,?,'COMPLETED',?)",
                    (request_id, spec.origin_kind.value, spec.connector_id,
                     source_id, target_ref, target_fingerprint, stamp,
                     idempotency_key),
                )
            article_matches = []
            if value.source_article_id is not None:
                article_matches = connection.execute(
                    f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                    "WHERE source_id=? AND source_article_id=?",
                    (source_id, value.source_article_id),
                ).fetchall()
            url_matches = connection.execute(
                f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                "WHERE source_id=? AND canonical_url_hash=?",
                (source_id, url_hash),
            ).fetchall()
            content_matches = []
            if value.content_hash is not None:
                content_matches = connection.execute(
                    f"SELECT {_IDENTITY_SELECT} FROM news_documents "
                    "WHERE source_id=? AND content_hash=?",
                    (source_id, value.content_hash),
                ).fetchall()
            matches_by_id = {row[0]: row for row in article_matches + url_matches + content_matches}
            payload = (
                value.source_article_id, canonical_url, url_hash,
                value.publisher_name, value.publisher_site_url,
                value.content_mode.value, value.title, title_hash,
                value.summary_text, value.body_text, value.content_hash,
                value.content_state.value, value.license_state.value,
                value.evidence_role.value, int(value.alert_eligible),
                value.time_interpretation.value, published,
            )
            if matches_by_id:
                existing = next(iter(matches_by_id.values()))
                stored = tuple(existing[1:])
                if len(matches_by_id) == 1 and stored == payload:
                    existing_id = existing[0]
                    receipt_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO intake_receipts(intake_receipt_id,intake_request_id,attempt_no,started_at,finished_at,status,bytes_received,source_record_type,source_record_id) VALUES(?,?,?,?,?,'DUPLICATE',?,'news_document',?)",
                        (receipt_id, request_id, attempt_no, stamp, stamp,
                         bytes_received, existing_id),
                    )
                    connection.execute("COMMIT")
                    return NewsReceipt(SaveOutcome.DUPLICATE, existing_id,
                                       request_id, receipt_id)
                locator_refresh = (
                    len(matches_by_id) == 1
                    and spec.connector_id in {
                        "finnhub-company-news-v1", "finnhub-market-news-v1",
                    }
                    and value.source_article_id is not None
                    and existing[1] == value.source_article_id
                    and (_is_finnhub_news_locator(existing[2])
                         or _is_legacy_bare_finnhub_news_locator(existing[2]))
                    and _is_finnhub_news_locator(canonical_url)
                    and tuple(existing[4:]) == payload[3:]
                )
                if locator_refresh:
                    existing_id = existing[0]
                    connection.execute(
                        "UPDATE news_documents SET canonical_url=?,"
                        "canonical_url_hash=?,last_seen_at=? "
                        "WHERE news_document_id=?",
                        (canonical_url, url_hash, stamp, existing_id),
                    )
                    receipt_id = uuid.uuid4().hex
                    connection.execute(
                        "INSERT INTO intake_receipts("
                        "intake_receipt_id,intake_request_id,attempt_no,"
                        "started_at,finished_at,status,bytes_received,"
                        "response_fingerprint,source_record_type,source_record_id"
                        ") VALUES(?,?,?,?,?,'DUPLICATE',?,?,"
                        "'news_document',?)",
                        (receipt_id, request_id, attempt_no, stamp, stamp,
                         bytes_received, url_hash, existing_id),
                    )
                    connection.execute("COMMIT")
                    return NewsReceipt(SaveOutcome.DUPLICATE, existing_id,
                                       request_id, receipt_id)
                # The connector declares which fields it may revise; an empty
                # declaration never takes the factual revision path. The
                # canonical URL carries document identity, so a churning feed
                # may also rotate its GUID without becoming a new article.
                permitted_indexes = frozenset(
                    _FIELD_INDEXES[name] for name in spec.revisable_fields
                )
                changed_indexes = {
                    index for index, (old, new) in enumerate(zip(stored, payload))
                    if old != new
                }
                identity_held = (
                    stored[_URL_HASH_INDEX] == payload[_URL_HASH_INDEX]
                    and (spec.identity_churns
                         or stored[_ARTICLE_ID_INDEX] == payload[_ARTICLE_ID_INDEX])
                )
                # A rotating GUID is identity, not content, so it is allowed on
                # its own; every other changed field must be declared revisable.
                revised_indexes = changed_indexes - {_ARTICLE_ID_INDEX}
                revision_shaped = (
                    len(matches_by_id) == 1
                    and bool(value.source_article_id)
                    and identity_held
                    and changed_indexes
                    and revised_indexes <= permitted_indexes
                )
                limits_ok = (
                    len(existing[7].encode("utf-8")) <= 4096
                    and len(value.title.encode("utf-8")) <= 4096
                    and (existing[9] is None or len(existing[9].encode("utf-8")) <= 65536)
                    and (value.summary_text is None or len(value.summary_text.encode("utf-8")) <= 65536)
                )
                if revision_shaped and limits_ok:
                    existing_id = existing[0]
                    changed_names = sorted(
                        _IDENTITY_FIELDS[index] for index in changed_indexes)
                    changed_json = json.dumps(changed_names, separators=(",", ":"))
                    old_summary_hash = _hash("v1", "summary", existing[9] or "")
                    new_summary_hash = _hash("v1", "summary", value.summary_text or "")
                    document_fingerprint = _hash("v1", "canonical-document", existing_id)
                    article_hash = _hash("v1", "source-article-id", value.source_article_id)
                    old_host = _normalized_host(existing[5])
                    new_host = _normalized_host(value.publisher_site_url)
                    semantic_parts = (
                        "news-revision-evidence-published-revisable", spec.connector_id,
                        document_fingerprint, article_hash, url_hash,
                        existing[17] or "", published or "",
                        existing[8], title_hash,
                        _hash("v1", "raw-title", existing[7]),
                        _hash("v1", "raw-title", value.title),
                        old_summary_hash, new_summary_hash, changed_json,
                    )
                    revision_id = _length_delimited_hash(semantic_parts)
                    record = {
                        "revision_id": revision_id, "first_observed_at": stamp,
                        "expires_at": news_revision_evidence.expiry_for(stamp),
                        "connector_id": spec.connector_id,
                        "canonical_document_fingerprint": document_fingerprint,
                        "source_article_id_hash": article_hash,
                        "canonical_url_hash": url_hash,
                        "old_published_at": existing[17],
                        "new_published_at": published,
                        "old_title": existing[7], "new_title": value.title,
                        "old_title_fingerprint": existing[8],
                        "new_title_fingerprint": title_hash,
                        "old_summary_text": existing[9],
                        "new_summary_text": value.summary_text,
                        "old_summary_hash": old_summary_hash,
                        "new_summary_hash": new_summary_hash,
                        "old_publisher_host_hash": (_hash("v1", "publisher-host", old_host) if old_host else None),
                        "new_publisher_host_hash": (_hash("v1", "publisher-host", new_host) if new_host else None),
                        "changed_fields_json": changed_json,
                    }
                    connection.execute("ROLLBACK")
                    evidence_id = news_revision_evidence.append_revision(self.path, record)
                    self._after_revision_append(evidence_id)
                    return self._complete_revision_receipt(
                        spec=spec, source_id=source_id, stamp=stamp,
                        target_ref=target_ref, target_fingerprint=target_fingerprint,
                        idempotency_key=idempotency_key,
                        bytes_received=bytes_received, existing_id=existing_id,
                        stored=stored, payload=payload,
                        changed_names=changed_names, revision_id=evidence_id)
                connection.execute("ROLLBACK")
                return self._audit_conflict(
                    connector_id=spec.connector_id,
                    target_fingerprint=target_fingerprint, stamp=stamp,
                    payload=payload,
                    matches_by_arm=(article_matches, url_matches, content_matches),
                )
            document_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO news_documents(news_document_id,source_id,source_article_id,canonical_url,canonical_url_hash,publisher_name,publisher_site_url,content_mode,title,title_fingerprint,summary_text,body_text,content_hash,content_state,license_state,evidence_role,alert_eligible,time_interpretation,published_at,first_seen_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (document_id, source_id, value.source_article_id, canonical_url,
                 url_hash, value.publisher_name, value.publisher_site_url,
                 value.content_mode.value, value.title, title_hash,
                 value.summary_text, value.body_text, value.content_hash,
                 value.content_state.value, value.license_state.value,
                 value.evidence_role.value, int(value.alert_eligible),
                 value.time_interpretation.value, published,
                 stamp, stamp),
            )
            receipt_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO intake_receipts(intake_receipt_id,intake_request_id,attempt_no,started_at,finished_at,status,bytes_received,source_record_type,source_record_id) VALUES(?,?,?,?,?,'COLLECTED',?,'news_document',?)",
                (receipt_id, request_id, attempt_no, stamp, stamp, bytes_received,
                 document_id),
            )
            connection.execute("COMMIT")
            return NewsReceipt(SaveOutcome.INSERTED, document_id, request_id,
                               receipt_id)


def _is_legacy_bare_finnhub_news_locator(value: str) -> bool:
    parsed = urlsplit(value)
    return (
        parsed.scheme == "https"
        and (parsed.hostname or "").lower() == "finnhub.io"
        and parsed.path == "/api/news"
        and not parsed.query
        and not parsed.fragment
    )


def _is_finnhub_news_locator(value: str) -> bool:
    parsed = urlsplit(value)
    pairs = parse_qsl(parsed.query, keep_blank_values=True)
    if (parsed.scheme != "https" or (parsed.hostname or "").lower() != "finnhub.io"
            or parsed.path != "/api/news" or len(pairs) != 1
            or pairs[0][0] != "id"):
        return False
    locator_id = pairs[0][1]
    return (locator_id.isdigit()
            or re.fullmatch(r"[0-9A-Fa-f]{64}", locator_id) is not None)


def _hash(*parts: str) -> str:
    return hashlib.sha256("\x00".join(parts).encode()).hexdigest()


def _length_delimited_hash(parts: tuple[str, ...]) -> str:
    encoded = b"".join(
        len(value.encode("utf-8")).to_bytes(8, "big") + value.encode("utf-8")
        for value in parts)
    return hashlib.sha256(encoded).hexdigest()


def _normalized_host(raw: str | None) -> str | None:
    if raw is None:
        return None
    try:
        host = urlsplit(raw).hostname
        return host.encode("idna").decode("ascii").lower() if host else None
    except (UnicodeError, ValueError) as error:
        raise RuntimeError("conflict_audit_failed") from error


def _canonicalize_url(raw: str) -> tuple[str, str]:
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("canonical_url must be an absolute HTTP(S) URL")
    host = parsed.hostname.encode("idna").decode("ascii").lower()
    port = parsed.port
    netloc = host if port is None or (parsed.scheme.lower(), port) in {("http", 80), ("https", 443)} else f"{host}:{port}"
    raw_path = parsed.path or "/"
    normalized_path = posixpath.normpath(raw_path)
    if raw_path.endswith("/") and not normalized_path.endswith("/"):
        normalized_path += "/"
    path = quote(normalized_path, safe="/%:@-._~!$&'()*+,;=")
    query = ""
    if host == "finnhub.io" and path == "/api/news":
        pairs = parse_qsl(parsed.query, keep_blank_values=True)
        ids = [value for key, value in pairs if key == "id"]
        valid_id = (len(ids) == 1 and ids[0].isascii()
                    and (ids[0].isdigit()
                         or re.fullmatch(r"[0-9A-Fa-f]{64}", ids[0]) is not None))
        if not ids or pairs != [("id", ids[0])]:
            valid_id = False
        if not valid_id:
            raise ValueError("Finnhub news URL requires one numeric or SHA-256 id")
        query = urlencode((("id", ids[0]),))
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, query, ""))
    labels = host.split(".")
    public_suffix_labels = 2 if (
        len(labels) >= 3 and len(labels[-1]) == 2
        and labels[-2] in {"ac", "co", "com", "edu", "go", "gov", "ne", "net", "or", "org", "pe", "re"}
    ) else 1
    if len(labels) <= public_suffix_labels:
        raise ValueError("canonical_url host has no registrable domain")
    domain = ".".join(labels[-(public_suffix_labels + 1):])
    return canonical, domain
