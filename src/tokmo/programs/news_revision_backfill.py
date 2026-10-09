"""Carry already-recorded revision evidence into the documents it describes.

Before the store applied revisions, a corrected headline was written to the
evidence sidecar and then rolled back, so ``news_documents`` kept whatever the
first poll happened to see. The evidence rows hold the corrected text, so the
documents can be brought forward without re-fetching anything.

Only fields the sidecar stores as text are recoverable. A rotated GUID is kept
as a hash, so identity changes are reported and skipped rather than guessed.
"""

import hashlib
import json
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path

from tokmo.records import news_revision_evidence
from tokmo.records.private_sqlite import (
    acquire_immediate_transaction,
    connect_private,
    prepare_private_path,
)

# Columns the sidecar preserves as text, mapped to the evidence column that
# carries the new value.
_RECOVERABLE = {
    "title": "new_title",
    "title_fingerprint": "new_title_fingerprint",
    "summary_text": "new_summary_text",
    "published_at": "new_published_at",
}


@dataclass
class BackfillReport:
    documents_seen: int = 0
    documents_updated: int = 0
    revisions_read: int = 0
    revisions_unmatched: int = 0
    already_current: int = 0
    skipped_fields: dict = field(default_factory=dict)
    applied: bool = False

    def as_dict(self) -> dict:
        return {
            "documents_seen": self.documents_seen,
            "documents_updated": self.documents_updated,
            "revisions_read": self.revisions_read,
            "revisions_unmatched": self.revisions_unmatched,
            "already_current": self.already_current,
            "skipped_fields": dict(sorted(self.skipped_fields.items())),
            "applied": self.applied,
        }


def _document_fingerprint(news_document_id: str) -> str:
    return hashlib.sha256(
        "\x00".join(("v1", "canonical-document", news_document_id)).encode()
    ).hexdigest()


def _latest_by_document(sidecar: Path) -> tuple[dict, int]:
    """Group evidence rows by document, newest last."""
    if not sidecar.exists():
        return {}, 0
    connection = sqlite3.connect(f"file:{sidecar}?mode=ro", uri=True)
    try:
        rows = connection.execute(
            "SELECT canonical_document_fingerprint,first_observed_at,new_title,"
            "new_title_fingerprint,new_summary_text,new_published_at,"
            "changed_fields_json FROM news_revisions "
            "ORDER BY first_observed_at,revision_id"
        ).fetchall()
    finally:
        connection.close()
    grouped: dict[str, list] = {}
    for row in rows:
        grouped.setdefault(row[0], []).append(row)
    return grouped, len(rows)


def backfill_news_revisions(path: Path, *, apply: bool = False) -> BackfillReport:
    path = Path(path)
    safety = prepare_private_path(path)
    if safety.unsafe:
        raise OSError(safety.reason)
    grouped, total = _latest_by_document(news_revision_evidence.sidecar_path(path))
    report = BackfillReport(revisions_read=total, applied=apply)

    connection = connect_private(path, mode="rw" if apply else "ro")
    try:
        if connection.execute(
            "SELECT schema_id FROM schema_meta WHERE singleton=1"
        ).fetchone() != ("external-news-n5",):
            raise RuntimeError("backfill requires the n5 schema")
        documents = connection.execute(
            "SELECT news_document_id,title,title_fingerprint,summary_text,"
            "published_at,last_seen_at,revision_no FROM news_documents"
        ).fetchall()
        by_fingerprint = {_document_fingerprint(row[0]): row for row in documents}
        report.documents_seen = len(documents)
        report.revisions_unmatched = sum(
            len(rows) for key, rows in grouped.items() if key not in by_fingerprint
        )

        updates = []
        for key, revisions in grouped.items():
            document = by_fingerprint.get(key)
            if document is None:
                continue
            # Each row carries the whole new value of the fields it changed, so
            # walking the chain in order leaves the latest value per field.
            # Taking only the final row would drop a summary corrected earlier
            # and re-titled since.
            latest_value = {}
            for revision in revisions:
                changed = set(json.loads(revision[6]))
                for name in sorted(changed - set(_RECOVERABLE)):
                    report.skipped_fields[name] = report.skipped_fields.get(name, 0) + 1
                if "title" in changed:
                    latest_value["title"] = revision[2]
                    latest_value["title_fingerprint"] = revision[3]
                if "summary_text" in changed:
                    latest_value["summary_text"] = revision[4]
                if "published_at" in changed:
                    latest_value["published_at"] = revision[5]
            current = {
                "title": document[1],
                "title_fingerprint": document[2],
                "summary_text": document[3],
                "published_at": document[4],
            }
            assignments = {
                name: value
                for name, value in sorted(latest_value.items())
                if current[name] != value
            }
            # A title only counts as revised together with its fingerprint.
            if "title" in assignments:
                assignments["title_fingerprint"] = latest_value["title_fingerprint"]
            if not assignments:
                report.already_current += 1
                continue
            last_seen = max(document[5], revisions[-1][1])
            updates.append(
                (document[0], assignments, last_seen, document[6] + len(revisions))
            )

        report.documents_updated = len(updates)
        if not apply or not updates:
            return report

        acquire_immediate_transaction(connection)
        try:
            for document_id, assignments, last_seen, revision_no in updates:
                clause = ",".join(f"{name}=?" for name in assignments)
                connection.execute(
                    f"UPDATE news_documents SET {clause},last_seen_at=?,"
                    "revision_no=? WHERE news_document_id=?",
                    (*assignments.values(), last_seen, revision_no, document_id),
                )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        return report
    finally:
        connection.close()
