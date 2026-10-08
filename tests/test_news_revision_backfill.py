"""Backfill of already-recorded revision evidence into news documents."""

from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from tokmo.programs.external_news_storage import bootstrap_external_news
from tokmo.programs.news_intake import persist_news_result
from tokmo.programs.news_revision_backfill import backfill_news_revisions
from tokmo.records.external_news import ExternalNewsStore
from tokmo.values.news_connectors import (
    JTBC_BREAKING_RSS, NewsConnectorItem, NewsFetchResult, NewsFetchStatus,
)

NOW = datetime(2026, 8, 2, 1, 2, 3, 456789, tzinfo=timezone.utc)
URL = "https://news.jtbc.co.kr/article/NB123"


def _item(*, title="First title", summary="First summary", observed_at=NOW,
          published_at=None, article_id="NB123"):
    return NewsConnectorItem(
        canonical_url=URL, source_article_id=article_id, title=title,
        summary_text=summary, observed_at=observed_at,
        published_at=published_at or NOW - timedelta(minutes=5),
    )


def _persist(path, item):
    return persist_news_result(
        path, JTBC_BREAKING_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (item,)),
        target_ref="feed:breaking",
    )[0]


def _stale_the_document(path, **columns):
    """Undo the applied revision so the document looks pre-backfill."""
    connection = sqlite3.connect(path)
    try:
        clause = ",".join(f"{name}=?" for name in columns)
        connection.execute(
            f"UPDATE news_documents SET {clause},revision_no=1",
            tuple(columns.values()))
        connection.commit()
    finally:
        connection.close()


def _document(path):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT title,summary_text,revision_no FROM news_documents"
        ).fetchone()
    finally:
        connection.close()


def test_dry_run_reports_the_work_without_touching_documents(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    _persist(path, _item(title="Corrected title", summary="Corrected summary",
                         observed_at=NOW + timedelta(minutes=1)))
    _stale_the_document(path, title="First title", summary_text="First summary",
                        title_fingerprint="b" * 64)

    report = backfill_news_revisions(path)

    assert report.applied is False
    assert report.revisions_read == 1
    assert report.documents_updated == 1
    assert report.revisions_unmatched == 0
    assert _document(path) == ("First title", "First summary", 1)


def test_apply_brings_the_document_to_the_latest_recorded_revision(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    _persist(path, _item(title="Corrected title", summary="Corrected summary",
                         observed_at=NOW + timedelta(minutes=1)))
    _persist(path, _item(title="Final title", summary="Corrected summary",
                         observed_at=NOW + timedelta(minutes=2)))
    _stale_the_document(path, title="First title", summary_text="First summary",
                        title_fingerprint="b" * 64)

    report = backfill_news_revisions(path, apply=True)

    assert report.applied is True
    assert report.revisions_read == 2
    assert report.documents_updated == 1
    assert _document(path) == ("Final title", "Corrected summary", 3)


def test_backfill_is_idempotent(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    _persist(path, _item(title="Corrected title", summary="Corrected summary",
                         observed_at=NOW + timedelta(minutes=1)))
    _stale_the_document(path, title="First title", summary_text="First summary",
                        title_fingerprint="b" * 64)
    backfill_news_revisions(path, apply=True)
    after_first = _document(path)

    second = backfill_news_revisions(path, apply=True)

    assert second.documents_updated == 0
    assert second.already_current == 1
    assert _document(path) == after_first


def test_documents_without_evidence_are_left_alone(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())

    report = backfill_news_revisions(path, apply=True)

    assert report.revisions_read == 0
    assert report.documents_updated == 0
    assert _document(path) == ("First title", "First summary", 1)


def test_unrecoverable_fields_are_reported_and_skipped(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    # A rotated GUID is stored only as a hash, so it cannot be replayed.
    _persist(path, _item(title="Corrected title", summary="First summary",
                         article_id="NB124",
                         observed_at=NOW + timedelta(minutes=1)))
    _stale_the_document(path, title="First title", title_fingerprint="b" * 64)

    report = backfill_news_revisions(path, apply=True)

    assert report.skipped_fields == {"source_article_id": 1}
    assert _document(path)[0] == "Corrected title"
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_article_id FROM news_documents").fetchone() == (
            "NB124",)
    finally:
        connection.close()


def test_backfill_refuses_a_pre_migration_store(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE schema_meta SET schema_id='external-news-n4'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(RuntimeError, match="n5 schema"):
        backfill_news_revisions(path)
