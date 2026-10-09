"""Closed v0.2 factual RSS revision-evidence acceptance tests."""

import json
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.external_news_storage import bootstrap_external_news
from tokmo.programs.news_intake import persist_news_result
from tokmo.records import external_news, news_revision_evidence
from tokmo.records.external_news import ExternalNewsStore
from tokmo.records.news_conflict_audit import _read_conflicts
from tokmo.records.news_revision_evidence import _read_revisions
from tokmo.values.external_news import (
    ContentMode,
    ContentState,
    LicenseState,
    NewsWrite,
    SaveOutcome,
)
from tokmo.values.news_connectors import (
    GOOGLE_NEWS_SEARCH_RSS,
    JTBC_BREAKING_RSS,
    EvidenceRole,
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
    TimeInterpretation,
)

NOW = datetime(2026, 8, 3, 1, 2, 3, 456789, tzinfo=UTC)


def _item(*, observed_at=NOW, title="First title", summary="First summary"):
    return NewsConnectorItem(
        canonical_url="https://news.jtbc.co.kr/article/NB123",
        source_article_id="NB123",
        title=title,
        summary_text=summary,
        publisher_name="JTBC",
        publisher_site_url="https://news.jtbc.co.kr/",
        published_at=NOW - timedelta(minutes=5),
        observed_at=observed_at,
    )


def _persist(path, item, spec=JTBC_BREAKING_RSS):
    return persist_news_result(
        path,
        spec,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (item,), bytes_received=123),
        target_ref="feed:breaking",
    )[0]


def test_public_jtbc_revision_is_append_only_duplicate_and_replay_ignores_observed_at(
    tmp_path,
):
    path = tmp_path / "private" / "external_news_v4.sqlite3"
    bootstrap_external_news(path, now=NOW)
    inserted = _persist(path, _item())
    revised = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=1),
            title="Corrected title",
            summary="Corrected summary",
        ),
    )
    replay = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(days=1),
            title="Corrected title",
            summary="Corrected summary",
        ),
    )

    assert inserted.outcome is SaveOutcome.INSERTED
    assert revised.outcome is replay.outcome is SaveOutcome.DUPLICATE
    assert revised.revision_evidence_id is not None
    # The correction is applied, so the replay has nothing left to revise.
    assert replay.revision_evidence_id is None
    rows = _read_revisions(path)
    assert len(rows) == 1
    assert rows[0]["revision_id"] == revised.revision_evidence_id
    assert rows[0]["first_observed_at"] == (NOW + timedelta(minutes=1)).isoformat(
        timespec="microseconds"
    )
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT title,summary_text,revision_no FROM news_documents"
        ).fetchone() == ("Corrected title", "Corrected summary", 2)
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (3,)
    finally:
        connection.close()


def test_raw_title_variants_with_same_fingerprint_get_distinct_ids_and_replay(tmp_path):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    first = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=1),
            title="Corrected Title",
            summary="Corrected summary",
        ),
    )
    second = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=2),
            title="  corrected   title  ",
            summary="Corrected summary",
        ),
    )
    # The document now carries the second variant, so re-delivering it is a
    # plain duplicate rather than a further revision.
    replay = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(days=2),
            title="  corrected   title  ",
            summary="Corrected summary",
        ),
    )

    assert first.revision_evidence_id != second.revision_evidence_id
    assert replay.outcome is SaveOutcome.DUPLICATE
    assert replay.revision_evidence_id is None
    rows = _read_revisions(path)
    assert len(rows) == 2
    assert {row["new_title"] for row in rows} == {
        "Corrected Title",
        "  corrected   title  ",
    }


def _write(**changes):
    values = {
        "canonical_url": "https://news.jtbc.co.kr/article/NB123",
        "source_article_id": "NB123",
        "title": "First title",
        "summary_text": "First summary",
        "observed_at": NOW,
        "publisher_name": "JTBC",
        "publisher_site_url": "https://news.jtbc.co.kr/",
        "published_at": NOW - timedelta(minutes=5),
    }
    values.update(changes)
    return NewsWrite(**values)


# source_article_id and publisher_name are absent by design: for a connector
# whose feed churns, the canonical URL carries identity, so a rotated GUID is a
# refresh and the publisher label is a declared revisable field. Connectors
# that do not churn still treat the GUID as immutable -- see the finnhub
# article-id drift tests.
_IMMUTABLE_DRIFTS = [
    ("canonical_url", {}, {"canonical_url": "https://news.jtbc.co.kr/article/NB124"}),
    ("publisher_site_url", {}, {"publisher_site_url": "https://news.jtbc.co.kr/news/"}),
    ("content_mode", {}, {"content_mode": ContentMode.ANALYSIS}),
    (
        "body_text",
        {
            "body_text": "body one",
            "content_hash": "1" * 64,
            "content_state": ContentState.BODY_AVAILABLE,
            "license_state": LicenseState.PERMITTED,
        },
        {
            "body_text": "body two",
            "content_hash": "1" * 64,
            "content_state": ContentState.BODY_AVAILABLE,
            "license_state": LicenseState.PERMITTED,
        },
    ),
    ("content_hash", {"content_hash": "1" * 64}, {"content_hash": "2" * 64}),
    (
        "content_state",
        {"content_state": ContentState.PAYWALLED},
        {"content_state": ContentState.REDACTED},
    ),
    (
        "license_state",
        {"license_state": LicenseState.RESTRICTED},
        {"license_state": LicenseState.UNKNOWN},
    ),
    ("evidence_role", {}, {"evidence_role": EvidenceRole.OPINION_DISCOVERY}),
    ("alert_eligible", {}, {"alert_eligible": False}),
    (
        "time_interpretation",
        {},
        {"time_interpretation": TimeInterpretation.EXPLICIT_ZONE},
    ),
    ("published_at", {}, {"published_at": NOW - timedelta(minutes=4)}),
]


@pytest.mark.parametrize(
    "field,baseline,drift",
    _IMMUTABLE_DRIFTS,
    ids=[case[0] for case in _IMMUTABLE_DRIFTS],
)
def test_each_immutable_newswrite_drift_conflicts_without_revision(
    tmp_path, field, baseline, drift
):
    path = tmp_path / field / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    inserted = store.save_connector_news(
        JTBC_BREAKING_RSS, _write(**baseline), target_ref="feed:breaking"
    )
    incoming = dict(baseline)
    incoming.update(drift)
    incoming.update(
        {
            "title": "Corrected title",
            "summary_text": "Corrected summary",
            "observed_at": NOW + timedelta(minutes=1),
        }
    )
    conflict = store.save_connector_news(
        JTBC_BREAKING_RSS, _write(**incoming), target_ref="feed:breaking"
    )

    assert inserted.outcome is SaveOutcome.INSERTED
    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.conflict_audit_id
    assert conflict.revision_evidence_id is None
    assert len(_read_conflicts(path)) == 1
    assert _read_revisions(path) == []
    assert not news_revision_evidence.sidecar_path(path).exists()


def test_churning_feed_guid_rotation_with_revision_records_both_changes(tmp_path):
    """A rotated GUID rides along with the revision instead of conflicting."""
    path = tmp_path / "churn" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    inserted = store.save_connector_news(
        JTBC_BREAKING_RSS, _write(), target_ref="feed:breaking"
    )

    revised = store.save_connector_news(
        JTBC_BREAKING_RSS,
        _write(
            source_article_id="NB124",
            title="Corrected title",
            summary_text="Corrected summary",
            observed_at=NOW + timedelta(minutes=1),
        ),
        target_ref="feed:breaking",
    )

    assert inserted.outcome is SaveOutcome.INSERTED
    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == inserted.news_document_id
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id
    assert _read_conflicts(path) == []
    rows = _read_revisions(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == [
        "source_article_id",
        "summary_text",
        "title",
        "title_fingerprint",
    ]


@pytest.mark.parametrize("field,limit", [("title", 4096), ("summary", 65536)])
def test_revision_utf8_byte_boundary_and_over_limit_routes_redacted_conflict(
    tmp_path, field, limit
):
    path = tmp_path / field / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    changes = (
        {"title": "x" * limit, "summary": "Changed summary"}
        if field == "title"
        else {"title": "Changed title", "summary": "x" * limit}
    )
    accepted = _persist(path, _item(observed_at=NOW + timedelta(minutes=1), **changes))
    assert accepted.revision_evidence_id
    changes[field] = "é" * (limit // 2 + 1)
    conflict = _persist(path, _item(observed_at=NOW + timedelta(minutes=2), **changes))
    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.revision_evidence_id is None
    assert len(_read_revisions(path)) == 1
    raw = path.with_name(f"{path.stem}.conflicts.sqlite3").read_bytes()
    assert changes[field].encode() not in raw


def test_google_title_summary_drift_stays_conflict_and_has_no_revision_sidecar(
    tmp_path,
):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item(), GOOGLE_NEWS_SEARCH_RSS)
    drift = _persist(
        path, _item(title="Changed", summary="Changed"), GOOGLE_NEWS_SEARCH_RSS
    )
    assert drift.outcome is SaveOutcome.CONFLICT
    assert drift.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.revisions.sqlite3").exists()


def test_revision_sidecar_is_private_strict_append_only_and_bounded_reader(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    _persist(path, _item(title="Changed", summary="Changed"))
    sidecar = path.with_name(f"{path.stem}.revisions.sqlite3")
    assert stat.S_IMODE(os.lstat(sidecar.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(sidecar).st_mode) == 0o600
    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute(
            "SELECT schema_id,schema_version FROM schema_meta"
        ).fetchone() == ("news-revision-evidence-published-revisable", 2)
        assert all(
            row[5] == 1
            for row in connection.execute("PRAGMA table_list")
            if row[1] in {"schema_meta", "news_revisions"}
        )
        revision_id = connection.execute(
            "SELECT revision_id FROM news_revisions"
        ).fetchone()[0]
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "UPDATE news_revisions SET connector_id='x' WHERE revision_id=?",
                (revision_id,),
            )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute(
                "DELETE FROM news_revisions WHERE revision_id=?", (revision_id,)
            )
    finally:
        connection.close()
    assert len(_read_revisions(path, limit=100)) == 1
    with pytest.raises(ValueError):
        _read_revisions(path, limit=101)


def test_zero_byte_revision_sidecar_residue_bootstraps_on_public_revision(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = news_revision_evidence.sidecar_path(path)
    sidecar.touch(mode=0o600)

    receipt = _persist(path, _item(title="Changed", summary="Changed"))

    assert receipt.revision_evidence_id
    assert [row["revision_id"] for row in _read_revisions(path)] == [
        receipt.revision_evidence_id
    ]


def test_revision_sidecar_first_create_race_revalidates_winner_schema(
    tmp_path, monkeypatch
):
    path = tmp_path / "private" / "external.revisions.sqlite3"
    path.parent.mkdir(mode=0o700)
    path.touch(mode=0o600)

    class RaceConnection:
        def __init__(self):
            self.counts = iter([(0,), (4,)])
            self.in_transaction = False

        def execute(self, sql, parameters=()):
            del parameters
            if sql.startswith("SELECT count(*) FROM sqlite_master"):
                return self
            if sql == "BEGIN IMMEDIATE":
                self.in_transaction = True
            elif sql == "COMMIT":
                self.in_transaction = False
            return self

        def fetchone(self):
            return next(self.counts)

        def close(self):
            pass

    connection = RaceConnection()
    validated = []
    monkeypatch.setattr(
        news_revision_evidence, "connect_private", lambda *_args, **_kwargs: connection
    )
    monkeypatch.setattr(news_revision_evidence, "_validate", validated.append)

    assert (
        news_revision_evidence._open(path, NOW.isoformat(timespec="microseconds"))
        is connection
    )
    assert validated == [connection]


_LEGACY_PUBLISHED = (NOW - timedelta(minutes=5)).isoformat(timespec="microseconds")


def _seed_legacy_sidecar(path):
    sidecar = news_revision_evidence.sidecar_path(path)
    connection = sqlite3.connect(sidecar)
    try:
        for statement in news_revision_evidence._statements(
            news_revision_evidence._LEGACY_SCHEMA
        ):
            connection.execute(statement)
        stamp = NOW.isoformat(timespec="microseconds")
        connection.execute(
            "INSERT INTO schema_meta VALUES(1,?,?,?,?)",
            (
                "news-revision-evidence-v1",
                1,
                news_revision_evidence._EXPECTED_LEGACY_DDL,
                stamp,
            ),
        )
        row = {
            "revision_id": "1" * 64,
            "first_observed_at": stamp,
            "expires_at": news_revision_evidence.expiry_for(stamp),
            "connector_id": "jtbc-breaking-rss-v1",
            "canonical_document_fingerprint": "2" * 64,
            "source_article_id_hash": "3" * 64,
            "canonical_url_hash": "4" * 64,
            "published_at": _LEGACY_PUBLISHED,
            "old_title": "Legacy old",
            "new_title": "Legacy new",
            "old_title_fingerprint": "5" * 64,
            "new_title_fingerprint": "6" * 64,
            "old_summary_text": "old summary",
            "new_summary_text": "new summary",
            "old_summary_hash": "7" * 64,
            "new_summary_hash": "8" * 64,
            "old_publisher_host_hash": None,
            "new_publisher_host_hash": None,
            "changed_fields_json": '["summary_text","title","title_fingerprint"]',
        }
        connection.execute(
            f"INSERT INTO news_revisions({','.join(row)}) "
            f"VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(sidecar, 0o600)
    return sidecar


def test_populated_v1_sidecar_migrates_in_place_preserving_every_row(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = _seed_legacy_sidecar(path)

    receipt = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=1),
            title="Corrected title",
            summary="Corrected summary",
        ),
    )

    assert receipt.outcome is SaveOutcome.DUPLICATE
    assert receipt.revision_evidence_id
    rows = _read_revisions(path)
    assert len(rows) == 2
    legacy = next(row for row in rows if row["revision_id"] == "1" * 64)
    assert legacy["old_published_at"] == _LEGACY_PUBLISHED
    assert legacy["new_published_at"] == _LEGACY_PUBLISHED
    assert legacy["old_title"] == "Legacy old"
    assert legacy["new_summary_text"] == "new summary"
    assert legacy["changed_fields"] == ["summary_text", "title", "title_fingerprint"]
    fresh = next(
        row for row in rows if row["revision_id"] == receipt.revision_evidence_id
    )
    assert fresh["old_published_at"] == fresh["new_published_at"]
    assert fresh["changed_fields"] == ["summary_text", "title", "title_fingerprint"]
    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute(
            "SELECT schema_id,schema_version FROM schema_meta"
        ).fetchone() == ("news-revision-evidence-published-revisable", 2)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name LIKE '%legacy%'"
        ).fetchone() == (0,)
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE news_revisions SET connector_id='x'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM news_revisions")
    finally:
        connection.close()


def test_tampered_v1_sidecar_is_not_migrated_and_fails_closed(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = _seed_legacy_sidecar(path)
    connection = sqlite3.connect(sidecar)
    connection.execute("UPDATE schema_meta SET ddl_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="^revision_evidence_failed$"):
        _persist(path, _item(title="Changed", summary="Changed"))

    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute(
            "SELECT schema_id,schema_version FROM schema_meta"
        ).fetchone() == ("news-revision-evidence-v1", 1)
        assert connection.execute("SELECT count(*) FROM news_revisions").fetchone() == (
            1,
        )
    finally:
        connection.close()


def _seed_transient_sidecar(path):
    sidecar = news_revision_evidence.sidecar_path(path)
    connection = sqlite3.connect(sidecar)
    try:
        for statement in news_revision_evidence._statements(
            news_revision_evidence._SCHEMA
        ):
            connection.execute(statement)
        stamp = NOW.isoformat(timespec="microseconds")
        connection.execute(
            "INSERT INTO schema_meta VALUES(1,?,?,?,?)",
            (
                "news-revision-evidence-v2",
                2,
                news_revision_evidence._EXPECTED_DDL,
                stamp,
            ),
        )
        row = {
            "revision_id": "a" * 64,
            "first_observed_at": stamp,
            "expires_at": news_revision_evidence.expiry_for(stamp),
            "connector_id": "bbc-world-rss-v1",
            "canonical_document_fingerprint": "b" * 64,
            "source_article_id_hash": "c" * 64,
            "canonical_url_hash": "d" * 64,
            "old_published_at": _LEGACY_PUBLISHED,
            "new_published_at": _LEGACY_PUBLISHED,
            "old_title": "Transient old",
            "new_title": "Transient new",
            "old_title_fingerprint": "e" * 64,
            "new_title_fingerprint": "f" * 64,
            "old_summary_text": "old summary",
            "new_summary_text": "new summary",
            "old_summary_hash": "0" * 64,
            "new_summary_hash": "9" * 64,
            "old_publisher_host_hash": None,
            "new_publisher_host_hash": None,
            "changed_fields_json": '["summary_text","title","title_fingerprint"]',
        }
        connection.execute(
            f"INSERT INTO news_revisions({','.join(row)}) "
            f"VALUES({','.join('?' for _ in row)})",
            tuple(row.values()),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(sidecar, 0o600)
    return sidecar


def test_transient_schema_id_matches_production_string_without_source_literal():
    assert news_revision_evidence._TRANSIENT_SCHEMA_ID == "news-revision-evidence-v2"
    source = Path(news_revision_evidence.__file__).read_text().lower()
    assert "v2" not in source


def test_populated_transient_sidecar_is_adopted_preserving_every_row(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = _seed_transient_sidecar(path)

    receipt = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=1),
            title="Corrected title",
            summary="Corrected summary",
        ),
    )

    assert receipt.outcome is SaveOutcome.DUPLICATE
    assert receipt.revision_evidence_id
    rows = _read_revisions(path)
    assert len(rows) == 2
    adopted = next(row for row in rows if row["revision_id"] == "a" * 64)
    assert adopted["old_published_at"] == _LEGACY_PUBLISHED
    assert adopted["new_published_at"] == _LEGACY_PUBLISHED
    assert adopted["connector_id"] == "bbc-world-rss-v1"
    assert adopted["old_title"] == "Transient old"
    assert adopted["new_title"] == "Transient new"
    assert adopted["new_summary_text"] == "new summary"
    assert adopted["changed_fields"] == ["summary_text", "title", "title_fingerprint"]
    assert stat.S_IMODE(os.stat(sidecar).st_mode) == 0o600
    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute(
            "SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta"
        ).fetchone() == (
            "news-revision-evidence-published-revisable",
            2,
            news_revision_evidence._EXPECTED_DDL,
        )
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("UPDATE news_revisions SET connector_id='x'")
        with pytest.raises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM news_revisions")
    finally:
        connection.close()


@pytest.mark.parametrize(
    "tamper",
    [
        "UPDATE schema_meta SET schema_version=3",
        "UPDATE schema_meta SET ddl_sha256='" + "0" * 64 + "'",
        "DROP TRIGGER news_revisions_no_delete",
    ],
)
def test_tampered_transient_sidecar_is_not_adopted_and_fails_closed(tmp_path, tamper):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = _seed_transient_sidecar(path)
    connection = sqlite3.connect(sidecar)
    connection.execute(tamper)
    connection.commit()
    before = connection.execute(
        "SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta"
    ).fetchone()
    connection.close()

    with pytest.raises(RuntimeError, match="^revision_evidence_failed$"):
        _persist(path, _item(title="Changed", summary="Changed"))

    connection = sqlite3.connect(sidecar)
    try:
        assert (
            connection.execute(
                "SELECT schema_id,schema_version,ddl_sha256 FROM schema_meta"
            ).fetchone()
            == before
        )
        assert before[0] == "news-revision-evidence-v2"
        assert connection.execute("SELECT count(*) FROM news_revisions").fetchone() == (
            1,
        )
    finally:
        connection.close()


def test_transient_adoption_is_idempotent_across_reopens(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = _seed_transient_sidecar(path)

    for minutes, title in ((1, "Corrected title"), (2, "Another title")):
        receipt = _persist(
            path,
            _item(
                observed_at=NOW + timedelta(minutes=minutes),
                title=title,
                summary="Corrected summary",
            ),
        )
        assert receipt.revision_evidence_id
    rows = _read_revisions(path)
    assert len(rows) == 3
    assert {row["revision_id"] for row in rows} > {"a" * 64}
    connection = sqlite3.connect(sidecar)
    try:
        assert connection.execute(
            "SELECT schema_id,schema_version FROM schema_meta"
        ).fetchone() == ("news-revision-evidence-published-revisable", 2)
    finally:
        connection.close()


def test_revision_module_is_storage_private_and_relay_guarded():
    root = Path(__file__).parents[1] / "src" / "tokmo"
    relay_files = [
        *root.glob("**/*telegram*.py"),
        root / "programs" / "analysis_pipeline.py",
        root / "programs" / "prompt_builder.py",
    ]
    assert all("news_revision_evidence" not in file.read_text() for file in relay_files)
    assert news_revision_evidence.__name__ == "tokmo.records.news_revision_evidence"


def test_crash_after_sidecar_append_leaves_canonical_rolled_back_and_replay_closes(
    tmp_path, monkeypatch
):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    revised = _item(
        observed_at=NOW + timedelta(minutes=1), title="Corrected", summary="Corrected"
    )
    monkeypatch.setattr(
        ExternalNewsStore,
        "_after_revision_append",
        lambda self, revision_id: (_ for _ in ()).throw(RuntimeError("crash")),
    )
    with pytest.raises(RuntimeError, match="crash"):
        _persist(path, revised)
    first_id = _read_revisions(path)[0]["revision_id"]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (1,)
        assert connection.execute("SELECT title FROM news_documents").fetchone() == (
            "First title",
        )
    finally:
        connection.close()
    monkeypatch.undo()
    replay = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(days=1), title="Corrected", summary="Corrected"
        ),
    )
    assert replay.revision_evidence_id == first_id
    assert len(_read_revisions(path)) == 1


def test_divergent_semantic_replay_under_same_id_fails_closed(tmp_path):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    receipt = _persist(path, _item(title="Corrected", summary="Corrected"))
    stored = _read_revisions(path)[0]
    record = dict(stored)
    record["changed_fields_json"] = json.dumps(
        record.pop("changed_fields"), separators=(",", ":")
    )
    record["new_title"] = "divergent"
    with pytest.raises(RuntimeError, match="^revision_evidence_failed$"):
        news_revision_evidence.append_revision(path, record)
    assert _read_revisions(path)[0]["revision_id"] == receipt.revision_evidence_id


def test_revision_sidecar_rejects_symlink_and_tampered_digest(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    sidecar = news_revision_evidence.sidecar_path(path)
    sidecar.unlink(missing_ok=True)
    sidecar.symlink_to(tmp_path / "elsewhere")
    with pytest.raises(RuntimeError, match="revision_evidence_failed"):
        _persist(path, _item(title="Changed", summary="Changed"))
    sidecar.unlink()
    _persist(path, _item(title="Changed", summary="Changed"))
    connection = sqlite3.connect(sidecar)
    connection.execute("UPDATE schema_meta SET ddl_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="revision_evidence_failed"):
        _persist(path, _item(title="Changed again", summary="Changed again"))


def test_revision_fresh_transaction_recreates_missing_request(tmp_path):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM intake_receipts")
    connection.execute("DELETE FROM intake_requests")
    connection.commit()
    connection.close()

    receipt = _persist(
        path,
        _item(
            observed_at=NOW + timedelta(minutes=1),
            title="Corrected",
            summary="Corrected",
        ),
    )
    assert receipt.revision_evidence_id
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM intake_requests"
        ).fetchone() == (1,)
        assert connection.execute("SELECT status FROM intake_receipts").fetchone() == (
            "DUPLICATE",
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
    finally:
        connection.close()


def test_revision_fresh_reopen_rechecks_canonical_path_safety(tmp_path, monkeypatch):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    _persist(path, _item())
    calls = []
    original = external_news.prepare_private_path
    rechecks = []
    original_recheck = external_news.check_private_path_without_open

    def checked(candidate):
        calls.append(candidate)
        return original(candidate)

    def rechecked(candidate):
        rechecks.append(candidate)
        return original_recheck(candidate)

    monkeypatch.setattr(external_news, "prepare_private_path", checked)
    monkeypatch.setattr(external_news, "check_private_path_without_open", rechecked)
    receipt = _persist(path, _item(title="Corrected", summary="Corrected"))

    assert receipt.revision_evidence_id
    # Batch open and both pre-BEGIN checks; post-BEGIN checks must not close
    # another DB descriptor and accidentally release the write reservation.
    assert calls == [path] * 3
    assert rechecks == [path] * 2
