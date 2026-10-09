"""News-first N1 vertical storage acceptance tests."""

import ast
import hashlib
import os
import sqlite3
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.external_news_storage import (
    bootstrap_external_news,
    save_external_news,
)
from tokmo.programs.news_intake import persist_news_result
from tokmo.records import news_conflict_audit
from tokmo.records.external_news import ExternalNewsStore
from tokmo.records.news_conflict_audit import (
    _read_conflicts,
    append_conflict,
    expiry_for,
)
from tokmo.records.news_revision_evidence import (
    _read_revisions as _read_revision_rows,
)
from tokmo.values.external_news import (
    ContentMode,
    ContentState,
    LicenseState,
    NewsWrite,
    SaveOutcome,
)
from tokmo.values.news_connectors import (
    BBC_BUSINESS_RSS,
    FINNHUB_COMPANY_NEWS,
    FINNHUB_MARKET_NEWS,
    GOOGLE_NEWS_SEARCH_RSS,
    INVESTING_ANALYST_RSS,
    YONHAP_MARKET_RSS,
    EvidenceRole,
    NewsConnectorItem,
    NewsFetchResult,
    NewsFetchStatus,
    TimeInterpretation,
)

NOW = datetime(2026, 8, 2, 1, 2, 3, 456789, tzinfo=UTC)


@pytest.mark.parametrize(
    "scenario", ["first_validator", "both_validators", "both_fk_scans"]
)
@pytest.mark.parametrize("batched", [False, True])
def test_slow_validation_allows_concurrent_writers(
    tmp_path, monkeypatch, scenario, batched
):
    import threading
    import time
    from concurrent.futures import ThreadPoolExecutor
    from contextlib import nullcontext

    from tokmo.records import external_news as module

    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    for i in range(20):
        ExternalNewsStore(path).save_connector_news(
            BBC_BUSINESS_RSS,
            _write(url=f"https://example.com/seed/{i}", source_article_id=f"seed-{i}"),
            target_ref="seed",
        )
    symmetric = scenario != "first_validator"
    entered = threading.Event()
    start = threading.Barrier(2)
    validate = module._validate_schema
    local = threading.local()

    def delayed(connection):
        if scenario == "both_fk_scans":
            # Delay inside the actual SQLite VM, with a SHARED reader lock,
            # rather than only sleeping before the validator takes any lock.
            active, steps = False, 0

            def progress():
                nonlocal steps
                if active:
                    steps += 1
                    if steps == 100:
                        local.scans += 1
                        time.sleep(1.4)
                return 0

            def trace(sql):
                nonlocal active
                active = sql == "PRAGMA foreign_key_check"

            connection.set_progress_handler(progress, 1)
            connection.set_trace_callback(trace)
        elif symmetric or local.first:
            entered.set()
            time.sleep(1.4)  # Below the observed 2.547188 s full FK scan.
        try:
            validate(connection)
        finally:
            connection.set_progress_handler(None, 0)
            connection.set_trace_callback(None)

    monkeypatch.setattr(module, "_validate_schema", delayed)

    def writer(first):
        local.first = first
        local.scans = 0
        store = ExternalNewsStore(path)
        if symmetric:
            start.wait(timeout=5)
        elif not first:
            assert entered.wait(5)
        with store.connector_batch() if batched else nullcontext():
            outcomes = [
                store.save_connector_news(
                    BBC_BUSINESS_RSS,
                    _write(
                        url=f"https://example.com/story/{i}",
                        source_article_id=f"article-{i}",
                    ),
                    target_ref="fixture:contention",
                ).outcome
                for i in range(3 if batched else 1)
            ]
            assert scenario != "both_fk_scans" or local.scans > 0
            return outcomes

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(writer, first) for first in (True, False)]
        outcomes = [
            outcome for future in futures for outcome in future.result(timeout=10)
        ]
    assert set(outcomes) == {SaveOutcome.INSERTED, SaveOutcome.DUPLICATE}
    assert (
        outcomes.count(SaveOutcome.INSERTED)
        == outcomes.count(SaveOutcome.DUPLICATE)
        == (3 if batched else 1)
    )
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (26 if batched else 22,)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


@pytest.mark.parametrize("count", [1, 10, 100])
def test_intake_batch_scans_integrity_once_and_commits_each_item(
    tmp_path, monkeypatch, count
):
    from tokmo.records import external_news as module

    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    statements = []
    connect = module.connect_private

    def traced(*args, **kwargs):
        connection = connect(*args, **kwargs)
        connection.set_trace_callback(statements.append)
        return connection

    monkeypatch.setattr(module, "connect_private", traced)
    items = tuple(
        NewsConnectorItem(
            canonical_url=f"https://example.com/{i}",
            title=f"Title {i}",
            source_article_id=str(i),
            observed_at=NOW,
        )
        for i in range(count)
    )
    for expected in (SaveOutcome.INSERTED, SaveOutcome.DUPLICATE):
        statements.clear()
        receipts = persist_news_result(
            path,
            BBC_BUSINESS_RSS,
            NewsFetchResult(NewsFetchStatus.COLLECTED, items),
            target_ref="feed:test",
        )
        assert [r.outcome for r in receipts] == [expected] * count
        assert statements.count("COMMIT") == count
        assert statements.count("PRAGMA foreign_key_check") == 1


@pytest.mark.parametrize(
    "mutation", ["schema", "foreign_key", "replace", "mode", "concurrent_commit"]
)
def test_batch_rechecks_external_changes_and_private_file_each_item(
    tmp_path, monkeypatch, mutation
):
    from tokmo.records import external_news as module

    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    validations = []
    validate = module._validate_schema

    def checked(connection):
        validations.append(1)
        validate(connection)

    monkeypatch.setattr(module, "_validate_schema", checked)
    with store.connector_batch():
        store.save_connector_news(BBC_BUSINESS_RSS, _write(), target_ref="fixture")
        if mutation == "replace":
            replacement = tmp_path / "replacement.sqlite3"
            bootstrap_external_news(replacement, now=NOW)
            replacement.replace(path)
        elif mutation == "mode":
            path.chmod(0o644)
        else:
            with sqlite3.connect(path) as other:
                other.execute(
                    {
                        "schema": "DELETE FROM schema_meta",
                        "foreign_key": "DELETE FROM source_families",
                        "concurrent_commit": "UPDATE sources SET metadata_json='{\"fixture\":true}'",
                    }[mutation]
                )
        if mutation == "concurrent_commit":
            assert (
                store.save_connector_news(
                    BBC_BUSINESS_RSS, _write(), target_ref="fixture"
                ).outcome
                is SaveOutcome.DUPLICATE
            )
            assert len(validations) == 2
        else:
            with pytest.raises((RuntimeError, OSError)):
                store.save_connector_news(
                    BBC_BUSINESS_RSS, _write(), target_ref="fixture"
                )
    path.chmod(0o600)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (
            (0,)
            if mutation == "replace"
            else (2,)
            if mutation == "concurrent_commit"
            else (1,)
        )


def test_singleton_after_batch_still_validates_and_busy_retains_cause(tmp_path):
    from tokmo.values.news_collection_diagnostics import exception_cause

    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    with store.connector_batch():
        store.save_connector_news(BBC_BUSINESS_RSS, _write(), target_ref="fixture")
    with sqlite3.connect(path) as other:
        other.execute("BEGIN IMMEDIATE")
        with pytest.raises(sqlite3.OperationalError) as caught:
            store.save_connector_news(BBC_BUSINESS_RSS, _write(), target_ref="fixture")
        assert exception_cause(caught.value) == "STORAGE_BUSY"
    with sqlite3.connect(path) as other:
        other.execute("DELETE FROM schema_meta")
    with pytest.raises(RuntimeError, match="schema_mismatch") as caught:
        store.save_connector_news(BBC_BUSINESS_RSS, _write(), target_ref="fixture")
    assert exception_cause(caught.value) == "STORAGE_SCHEMA"


@pytest.mark.parametrize("boundary", ["validation", "begin", "cached_begin"])
@pytest.mark.parametrize(
    "mutation",
    ["commit", "schema", "foreign_key", "replace", "mode", "fk_off", "churn"],
)
def test_validation_races_recheck_or_fail_without_new_receipt(
    tmp_path, monkeypatch, boundary, mutation
):
    from tokmo.records import external_news as module
    from tokmo.values.news_collection_diagnostics import exception_cause

    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    replacement = tmp_path / "replacement.sqlite3"
    bootstrap_external_news(replacement, now=NOW)
    calls = []
    with store.connector_batch():
        store.save_connector_news(BBC_BUSINESS_RSS, _write(), target_ref="fixture")
        if boundary != "cached_begin":
            # Invalidate the cache normally; the injected race happens later.
            with sqlite3.connect(path) as other:
                other.execute(
                    "UPDATE sources SET metadata_json=json_object('fixture',0)"
                )
        hook = (
            "_validate_schema"
            if boundary == "validation"
            else "acquire_immediate_transaction"
        )
        original = getattr(module, hook)

        def racing(connection):
            if boundary == "validation":
                original(connection)
            calls.append(1)
            if len(calls) == 1 or mutation == "churn":
                if mutation == "replace":
                    replacement.replace(path)
                elif mutation == "mode":
                    path.chmod(0o644)
                elif mutation == "fk_off":
                    connection.execute("PRAGMA foreign_keys=OFF")
                else:
                    with sqlite3.connect(path) as other:
                        other.execute(
                            {
                                "schema": "DELETE FROM schema_meta",
                                "foreign_key": "DELETE FROM source_families",
                                "commit": "UPDATE sources SET metadata_json=json_object('fixture',1)",
                                "churn": f"UPDATE sources SET metadata_json=json_object('fixture',{len(calls)})",
                            }[mutation]
                        )
            if boundary != "validation":
                original(connection)

        monkeypatch.setattr(module, hook, racing)
        if mutation == "commit":
            assert (
                store.save_connector_news(
                    BBC_BUSINESS_RSS, _write(), target_ref="fixture"
                ).outcome
                is SaveOutcome.DUPLICATE
            )
            assert len(calls) == 2
        else:
            with pytest.raises((RuntimeError, OSError, sqlite3.Error)) as caught:
                store.save_connector_news(
                    BBC_BUSINESS_RSS, _write(), target_ref="fixture"
                )
            if mutation == "churn":
                assert caught.value.args == ("storage_changed_during_validation",)
                assert exception_cause(caught.value) == "STORAGE_ERROR"
                assert len(calls) == 2
            assert not store._batch_connection.in_transaction
    path.chmod(0o600)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (
            (0,) if mutation == "replace" else (2,) if mutation == "commit" else (1,)
        )


def test_investing_shadow_provenance_persists_in_n4_storage(tmp_path):
    path = tmp_path / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    item = NewsConnectorItem(
        canonical_url="https://kr.investing.com/news/a",
        title="Analyst view",
        observed_at=NOW,
        source_article_id="inv-a",
        published_at=NOW,
        content_mode=ContentMode.OPINION,
        evidence_role=EvidenceRole.OPINION_DISCOVERY,
        alert_eligible=False,
        time_interpretation=TimeInterpretation.ASSUMED_KST,
    )

    receipts = persist_news_result(
        path,
        INVESTING_ANALYST_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (item,), bytes_received=123),
        target_ref="feed:analyst-opinion",
    )

    assert receipts[0].outcome is SaveOutcome.INSERTED
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT content_mode,evidence_role,alert_eligible,time_interpretation "
            "FROM news_documents"
        ).fetchone() == ("OPINION", "OPINION_DISCOVERY", 0, "ASSUMED_KST")
        assert connection.execute(
            "SELECT schema_version,manifest_revision,schema_id FROM schema_meta"
        ).fetchone() == (5, 5, "external-news-n5")
    finally:
        connection.close()


_N4_REVISION_COLUMN = " revision_no INTEGER NOT NULL DEFAULT 1 CHECK(revision_no>=1),"


def _build_n4_store(path, *, documents=()):
    """Recreate the pre-revision schema so the migration can be exercised."""
    from tokmo.records import external_news as store_module

    schema = store_module._SCHEMA.replace(_N4_REVISION_COLUMN, "")
    assert schema != store_module._SCHEMA
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    connection = sqlite3.connect(path)
    try:
        for statement in store_module._schema_statements(schema):
            connection.execute(statement)
        stamp = NOW.isoformat(timespec="microseconds")
        connection.execute(
            "INSERT INTO schema_meta VALUES(1,4,4,'external-news-n4',?,?,NULL)",
            (store_module._ddl_sha256(connection), stamp),
        )
        connection.execute(
            "INSERT INTO source_families VALUES"
            "('NEWS','News documents','news_document_store','news_document',"
            "1,?,NULL,'{}')",
            (stamp,),
        )
        for document_id, title in documents:
            connection.execute(
                "INSERT INTO sources(source_id,family_key,source_native_key,"
                "display_name,created_at,updated_at) "
                "VALUES('s1','NEWS','k','n',?,?)",
                (stamp, stamp),
            )
            connection.execute(
                "INSERT INTO news_documents(news_document_id,source_id,"
                "canonical_url,canonical_url_hash,content_mode,title,"
                "title_fingerprint,content_state,license_state,evidence_role,"
                "alert_eligible,time_interpretation,first_seen_at,last_seen_at"
                ") VALUES(?,'s1','https://e.example/a',?,'NEWS',?,?,"
                "'METADATA_ONLY','UNKNOWN','FACT_DISCOVERY',1,"
                "'SOURCE_NATIVE',?,?)",
                (document_id, "a" * 64, title, "b" * 64, stamp, stamp),
            )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def test_n4_store_migrates_to_n5_matching_a_freshly_created_schema(tmp_path):
    legacy = tmp_path / "legacy" / "external.sqlite3"
    fresh = tmp_path / "fresh" / "external.sqlite3"
    _build_n4_store(legacy, documents=[("doc-1", "Existing headline")])
    bootstrap_external_news(fresh, now=NOW)

    bootstrap_external_news(legacy, now=NOW)

    connection = sqlite3.connect(legacy)
    other = sqlite3.connect(fresh)
    try:
        version, schema_id, digest, migrated_at = connection.execute(
            "SELECT schema_version,schema_id,ddl_sha256,migrated_at FROM schema_meta"
        ).fetchone()
        assert (version, schema_id) == (5, "external-news-n5")
        assert migrated_at is not None
        assert (
            digest == other.execute("SELECT ddl_sha256 FROM schema_meta").fetchone()[0]
        )
        assert connection.execute(
            "SELECT title,revision_no FROM news_documents"
        ).fetchall() == [("Existing headline", 1)]
    finally:
        connection.close()
        other.close()


def test_n5_migration_is_idempotent_across_reopens(tmp_path):
    path = tmp_path / "legacy" / "external.sqlite3"
    _build_n4_store(path, documents=[("doc-1", "Existing headline")])

    bootstrap_external_news(path, now=NOW)
    first = (
        sqlite3.connect(path)
        .execute("SELECT ddl_sha256,migrated_at FROM schema_meta")
        .fetchone()
    )
    bootstrap_external_news(path, now=NOW + timedelta(days=1))

    assert (
        sqlite3.connect(path)
        .execute("SELECT ddl_sha256,migrated_at FROM schema_meta")
        .fetchone()
        == first
    )


def test_explicit_bootstrap_creates_private_strict_n4_schema(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"

    bootstrap_external_news(path, now=NOW)

    assert stat.S_IMODE(os.lstat(path.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(path).st_mode) == 0o600
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {
            "schema_meta",
            "source_families",
            "sources",
            "intake_requests",
            "intake_receipts",
            "news_documents",
            "source_connectors",
        }
        strict = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
        assert all(strict[name] == 1 for name in tables)
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("delete",)
        assert connection.execute(
            "SELECT family_key, display_name, adapter_key, record_type_key "
            "FROM source_families"
        ).fetchone() == (
            "NEWS",
            "News documents",
            "news_document_store",
            "news_document",
        )
    finally:
        connection.close()


def test_bootstrap_rejects_existing_schema_missing_meta_without_repair(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    connection = sqlite3.connect(path)
    connection.execute("DELETE FROM schema_meta")
    connection.commit()
    before = connection.execute("SELECT count(*) FROM schema_meta").fetchone()
    connection.close()

    with pytest.raises(RuntimeError, match="schema_mismatch"):
        bootstrap_external_news(path, now=NOW)

    connection = sqlite3.connect(path)
    try:
        assert (
            connection.execute("SELECT count(*) FROM schema_meta").fetchone() == before
        )
    finally:
        connection.close()


def test_schema_meta_hash_matches_independent_sqlite_master_recalculation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT type,name,sql FROM sqlite_master "
            "WHERE sql IS NOT NULL ORDER BY type,name"
        ).fetchall()
        encoded = b"".join(
            b"\0".join(field.encode("utf-8") for field in row) + b"\n" for row in rows
        )
        independent = hashlib.sha256(encoded).hexdigest()
        stored = connection.execute(
            "SELECT ddl_sha256 FROM schema_meta WHERE singleton=1"
        ).fetchone()[0]
        assert stored == independent
    finally:
        connection.close()


def test_partial_bootstrap_is_rejected_without_schema_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    path.parent.mkdir(mode=0o700)
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE orphan(value TEXT) STRICT")
    connection.commit()
    os.chmod(path, 0o600)
    before = connection.execute(
        "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
    ).fetchall()
    connection.close()

    with pytest.raises(RuntimeError, match="schema_mismatch"):
        bootstrap_external_news(path, now=NOW)

    connection = sqlite3.connect(path)
    try:
        after = connection.execute(
            "SELECT type,name,sql FROM sqlite_master ORDER BY type,name"
        ).fetchall()
        assert after == before
    finally:
        connection.close()


def _write(url="https://news.example.com/story/7", **changes):
    values = dict(
        canonical_url=url,
        source_article_id="article-7",
        title="Markets rise",
        summary_text="Summary",
        content_mode=ContentMode.NEWS,
        content_state=ContentState.METADATA_ONLY,
        license_state=LicenseState.UNKNOWN,
        published_at=NOW,
        observed_at=NOW,
    )
    values.update(changes)
    return NewsWrite(**values)


def _conflicts(path):
    return _read_conflicts(path)


@pytest.mark.parametrize(
    "publisher_site_url",
    [
        "https://" + "a" * 64 + ".example/",
        "https://publisher..example/",
    ],
)
def test_news_write_rejects_malformed_publisher_host(publisher_site_url):
    with pytest.raises(ValueError, match="publisher_site_url has an invalid host"):
        _write(publisher_site_url=publisher_site_url)


@pytest.mark.parametrize("corrupt_existing", [False, True])
def test_conflict_host_corruption_is_classified_as_audit_failure(
    tmp_path, corrupt_existing
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write(publisher_site_url="https://publisher.example/"))
    malformed = "https://" + "a" * 64 + ".example/"
    incoming = _write("https://news.example.com/other", title="Changed")
    if corrupt_existing:
        connection = sqlite3.connect(path)
        connection.execute(
            "UPDATE news_documents SET publisher_site_url=?", (malformed,)
        )
        connection.commit()
        connection.close()
    else:
        object.__setattr__(incoming, "publisher_site_url", malformed)

    with pytest.raises(RuntimeError, match="^conflict_audit_failed$"):
        save_external_news(path, incoming)

    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()


def test_article_id_conflict_is_audited_after_canonical_rollback(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    original = save_external_news(path, _write())
    before = sqlite3.connect(path)
    counts = tuple(
        before.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sources", "intake_requests", "intake_receipts", "news_documents")
    )
    before.close()

    receipt = save_external_news(
        path, _write("https://news.example.com/other", title="Incoming changed")
    )

    assert receipt.outcome is SaveOutcome.CONFLICT
    assert receipt.news_document_id == original.news_document_id
    assert receipt.conflict_audit_id
    connection = sqlite3.connect(path)
    assert (
        tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "intake_requests",
                "intake_receipts",
                "news_documents",
            )
        )
        == counts
    )
    connection.close()
    row = _conflicts(path)[0]
    assert row["conflict_id"] == receipt.conflict_audit_id
    assert (
        row["match_by_article_id"],
        row["match_by_url_hash"],
        row["match_by_content_hash"],
        row["match_count"],
    ) == (1, 0, 0, 1)
    assert row["changed_fields"] == [
        "canonical_url",
        "canonical_url_hash",
        "title",
        "title_fingerprint",
    ]


@pytest.mark.parametrize(
    ("arm", "changes", "bitmap"),
    [
        ("url", {"source_article_id": "other", "title": "URL changed"}, (0, 1, 0)),
        (
            "content",
            {
                "url": "https://news.example.com/content-other",
                "source_article_id": "content-other",
                "title": "Content changed",
            },
            (0, 0, 1),
        ),
    ],
)
def test_url_and_content_conflict_arms_are_distinguishable(
    tmp_path, arm, changes, bitmap
):
    path = tmp_path / "private" / f"{arm}.sqlite3"
    bootstrap_external_news(path, now=NOW)
    content_hash = (
        hashlib.sha256(b"shared-content").hexdigest() if arm == "content" else None
    )
    save_external_news(path, _write(content_hash=content_hash))
    incoming = _write(content_hash=content_hash, **changes)

    receipt = save_external_news(path, incoming)

    assert receipt.outcome is SaveOutcome.CONFLICT
    row = _conflicts(path)[0]
    assert (
        row["match_by_article_id"],
        row["match_by_url_hash"],
        row["match_by_content_hash"],
    ) == bitmap


def test_multi_document_conflict_records_every_match_and_first_reference(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    left = save_external_news(
        path, _write("https://news.example.com/a", source_article_id="a")
    )
    right = save_external_news(
        path, _write("https://news.example.com/z", source_article_id="z")
    )

    receipt = save_external_news(
        path,
        _write("https://news.example.com/z", source_article_id="a", title="Collision"),
    )

    expected = sorted([left.news_document_id, right.news_document_id])
    assert receipt.news_document_id == expected[0]
    row = _conflicts(path)[0]
    assert row["match_count"] == 2
    assert len(row["matched_document_fingerprints"]) == 2
    assert (
        row["old_title_fingerprint"]
        == ExternalNewsStore(path).read_document(expected[0])["title_fingerprint"]
    )


def test_conflict_replay_is_idempotent(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write())
    incoming = _write("https://news.example.com/other", title="Changed")
    first = save_external_news(path, incoming)
    second = save_external_news(path, incoming)
    assert first.conflict_audit_id == second.conflict_audit_id
    assert len(_conflicts(path)) == 1


def test_zero_byte_sidecar_residue_bootstraps_on_public_conflict(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write())
    sidecar = path.with_name(f"{path.stem}.conflicts.sqlite3")
    sidecar.touch(mode=0o600)

    receipt = save_external_news(
        path, _write("https://news.example.com/other", title="Changed")
    )

    assert receipt.outcome is SaveOutcome.CONFLICT
    assert receipt.conflict_audit_id
    assert [row["conflict_id"] for row in _conflicts(path)] == [
        receipt.conflict_audit_id
    ]


def test_sidecar_first_create_race_revalidates_winner_schema(tmp_path, monkeypatch):
    path = tmp_path / "private" / "external.conflicts.sqlite3"
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
        news_conflict_audit, "connect_private", lambda *_args, **_kwargs: connection
    )
    monkeypatch.setattr(news_conflict_audit, "_validate", validated.append)

    assert (
        news_conflict_audit._open(path, NOW.isoformat(timespec="microseconds"))
        is connection
    )
    assert validated == [connection]


def _audit_record(conflict_id="c" * 64, **changes):
    stamp = NOW.isoformat(timespec="microseconds")
    record = {
        "conflict_id": conflict_id,
        "observed_at": stamp,
        "expires_at": expiry_for(stamp),
        "connector_id": None,
        "target_fingerprint": "a" * 64,
        "match_count": 1,
        "match_by_article_id": 1,
        "match_by_url_hash": 0,
        "match_by_content_hash": 0,
        "matched_document_fingerprints_json": '["' + "b" * 64 + '"]',
        "changed_fields_json": '["title"]',
        "old_published_at": stamp,
        "new_published_at": stamp,
        "old_publisher_host_hash": None,
        "new_publisher_host_hash": None,
        "old_title_fingerprint": "d" * 64,
        "new_title_fingerprint": "e" * 64,
        "incoming_article_id_hash": None,
        "incoming_canonical_url_hash": "f" * 64,
    }
    record.update(changes)
    return record


def test_append_conflict_rejects_ignored_check_violation(tmp_path):
    canonical = tmp_path / "private" / "external.sqlite3"

    with pytest.raises(RuntimeError, match="^conflict_audit_failed$"):
        append_conflict(canonical, _audit_record(target_fingerprint="a" * 63))

    assert _read_conflicts(canonical) == []


def test_append_conflict_rejects_divergent_replay(tmp_path):
    canonical = tmp_path / "private" / "external.sqlite3"
    record = _audit_record()
    assert append_conflict(canonical, record) == record["conflict_id"]

    with pytest.raises(RuntimeError, match="^conflict_audit_failed$"):
        append_conflict(
            canonical, {**record, "match_by_article_id": 0, "match_by_url_hash": 1}
        )

    row = _read_conflicts(canonical)[0]
    assert row["match_by_article_id"] == 1
    assert row["match_by_url_hash"] == 0


def test_duplicate_writes_no_conflict_and_sidecar_is_lazy(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write())
    receipt = save_external_news(path, _write())
    assert receipt.outcome is SaveOutcome.DUPLICATE
    assert receipt.conflict_audit_id is None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()


def test_conflict_sidecar_is_private_strict_append_only_and_redacted(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    raw = dict(
        title="RAW_TITLE_SENTINEL",
        summary_text="RAW_SUMMARY_SENTINEL",
        publisher_name="RAW_PUBLISHER_SENTINEL",
        publisher_site_url="https://raw-host-sentinel.example/path",
    )
    save_external_news(
        path,
        _write(
            body_text="RAW_BODY_SENTINEL",
            content_state=ContentState.BODY_AVAILABLE,
            license_state=LicenseState.PERMITTED,
            **raw,
        ),
    )
    save_external_news(
        path,
        _write(
            "https://news.example.com/full-url-sentinel?secret=RAW_KEY_SENTINEL",
            source_article_id="RAW_ARTICLE_ID_SENTINEL",
            title="different",
        ),
    )
    # Force an article-id conflict carrying all raw sentinels in the incoming evidence.
    save_external_news(
        path,
        _write(
            "https://news.example.com/incoming-full-url-sentinel?key=RAW_KEY_SENTINEL",
            source_article_id="article-7",
            body_text="RAW_BODY_SENTINEL",
            content_state=ContentState.BODY_AVAILABLE,
            license_state=LicenseState.PERMITTED,
            **raw,
        ),
    )
    sidecar = path.with_name(f"{path.stem}.conflicts.sqlite3")
    assert stat.S_IMODE(os.lstat(sidecar.parent).st_mode) == 0o700
    assert stat.S_IMODE(os.lstat(sidecar).st_mode) == 0o600
    connection = sqlite3.connect(sidecar)
    assert connection.execute(
        "SELECT schema_id,schema_version FROM schema_meta"
    ).fetchone() == ("news-conflict-audit-v1", 1)
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    digest = hashlib.sha256(
        b"".join(b"\0".join(field.encode() for field in row) + b"\n" for row in rows)
    ).hexdigest()
    assert connection.execute("SELECT ddl_sha256 FROM schema_meta").fetchone() == (
        digest,
    )
    assert all(
        row[5] == 1
        for row in connection.execute("PRAGMA table_list")
        if row[1] in {"schema_meta", "news_conflicts"}
    )
    conflict_id = connection.execute(
        "SELECT conflict_id FROM news_conflicts"
    ).fetchone()[0]
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "UPDATE news_conflicts SET match_count=1 WHERE conflict_id=?",
            (conflict_id,),
        )
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM news_conflicts WHERE conflict_id=?", (conflict_id,)
        )
    assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    connection.close()
    data = sidecar.read_bytes()
    for sentinel in (
        b"RAW_TITLE_SENTINEL",
        b"RAW_SUMMARY_SENTINEL",
        b"RAW_BODY_SENTINEL",
        b"incoming-full-url-sentinel",
        b"RAW_ARTICLE_ID_SENTINEL",
        b"raw-host-sentinel.example",
        b"RAW_KEY_SENTINEL",
    ):
        assert sentinel not in data


def test_sidecar_failure_never_returns_unaudited_conflict(tmp_path, monkeypatch):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write())
    monkeypatch.setattr(
        "tokmo.records.news_conflict_audit.append_conflict",
        lambda *a, **k: (_ for _ in ()).throw(sqlite3.OperationalError("locked")),
    )
    with pytest.raises(RuntimeError, match="conflict_audit_failed"):
        save_external_news(path, _write("https://news.example.com/other"))


def test_connector_conflict_is_audited(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = NewsConnectorItem(
        canonical_url="https://news.example.com/a",
        source_article_id="connector-id",
        title="Original",
        observed_at=NOW,
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first,)),
        target_ref="feed:bbc",
    )
    collision = NewsConnectorItem(
        canonical_url="https://news.example.com/b",
        source_article_id="connector-id",
        title="Changed",
        observed_at=NOW,
    )

    receipt = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (collision,)),
        target_ref="feed:bbc",
    )[0]

    assert receipt.outcome is SaveOutcome.CONFLICT
    assert receipt.conflict_audit_id
    assert _conflicts(path)[0]["connector_id"] == BBC_BUSINESS_RSS.connector_id


def test_more_than_three_distinct_matches_fails_closed_without_truncation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    shared = hashlib.sha256(b"corruption-seam").hexdigest()
    save_external_news(path, _write(content_hash=shared))
    connection = sqlite3.connect(path)
    base = connection.execute("SELECT * FROM news_documents").fetchone()
    columns = [
        row[1] for row in connection.execute("PRAGMA table_info(news_documents)")
    ]
    for index in range(3):
        clone = list(base)
        clone[columns.index("news_document_id")] = f"corrupt-{index}"
        clone[columns.index("source_article_id")] = f"corrupt-article-{index}"
        clone[columns.index("canonical_url")] = (
            f"https://news.example.com/corrupt-{index}"
        )
        clone[columns.index("canonical_url_hash")] = hashlib.sha256(
            f"unique-{index}".encode()
        ).hexdigest()
        connection.execute(
            f"INSERT INTO news_documents VALUES({','.join('?' for _ in clone)})", clone
        )
    connection.commit()
    before = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("intake_requests", "intake_receipts", "news_documents")
    )
    connection.close()

    with pytest.raises(RuntimeError, match="conflict_audit_failed"):
        save_external_news(
            path,
            _write(
                "https://news.example.com/incoming",
                source_article_id="incoming",
                content_hash=shared,
            ),
        )

    connection = sqlite3.connect(path)
    assert (
        tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("intake_requests", "intake_receipts", "news_documents")
        )
        == before
    )
    connection.close()
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()


def test_tampered_sidecar_schema_fails_closed(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write())
    save_external_news(path, _write("https://news.example.com/one", title="one"))
    sidecar = path.with_name(f"{path.stem}.conflicts.sqlite3")
    connection = sqlite3.connect(sidecar)
    connection.execute("UPDATE schema_meta SET ddl_sha256=?", ("0" * 64,))
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="conflict_audit_failed"):
        save_external_news(path, _write("https://news.example.com/two", title="two"))


def test_connector_save_inserts_api_identity_publisher_provenance_and_schema_v3(
    tmp_path,
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    item = NewsConnectorItem(
        canonical_url="https://news.example.com/finnhub/7",
        source_article_id="fh-7",
        title="Company update",
        summary_text="Metadata only",
        published_at=NOW,
        publisher_name="Example Publisher",
        publisher_site_url="https://publisher.example/",
        observed_at=NOW,
    )
    result = NewsFetchResult(NewsFetchStatus.COLLECTED, (item,), bytes_received=321)

    receipts = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        result,
        target_ref="symbol:AAPL",
    )

    assert len(receipts) == 1
    assert receipts[0].outcome is SaveOutcome.INSERTED
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_native_key,display_name,canonical_url FROM sources"
        ).fetchone() == (
            "connector:finnhub-company-news",
            "Finnhub Company News",
            "https://finnhub.io/docs/api",
        )
        assert connection.execute(
            "SELECT origin_kind,connector_id,target_ref,request_state "
            "FROM intake_requests"
        ).fetchone() == (
            "API",
            "finnhub-company-news-v1",
            "symbol:AAPL",
            "COMPLETED",
        )
        assert connection.execute(
            "SELECT status,bytes_received,source_record_type FROM intake_receipts"
        ).fetchone() == ("COLLECTED", 321, "news_document")
        assert connection.execute(
            "SELECT content_state,license_state,body_text FROM news_documents"
        ).fetchone() == ("METADATA_ONLY", "UNKNOWN", None)
        assert connection.execute("PRAGMA foreign_key_check").fetchone() is None
        assert connection.execute(
            "SELECT publisher_name,publisher_site_url FROM news_documents"
        ).fetchone() == ("Example Publisher", "https://publisher.example/")
        assert connection.execute(
            "SELECT schema_version,manifest_revision,schema_id FROM schema_meta"
        ).fetchone() == (5, 5, "external-news-n5")
    finally:
        connection.close()


def test_connector_duplicate_reuses_document_but_appends_poll_attempt_receipt(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    item = NewsConnectorItem(
        canonical_url="https://news.example.com/finnhub/7",
        source_article_id="fh-7",
        title="Company update",
        observed_at=NOW,
    )
    result = NewsFetchResult(NewsFetchStatus.COLLECTED, (item,))
    first = persist_news_result(
        path, FINNHUB_COMPANY_NEWS, result, target_ref="symbol:AAPL"
    )[0]
    second = persist_news_result(
        path, FINNHUB_COMPANY_NEWS, result, target_ref="symbol:AAPL"
    )[0]
    assert first.outcome is SaveOutcome.INSERTED
    assert second.outcome is SaveOutcome.DUPLICATE
    assert second.news_document_id == first.news_document_id
    assert second.intake_request_id == first.intake_request_id
    assert second.intake_receipt_id != first.intake_receipt_id
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT attempt_no,status FROM intake_receipts ORDER BY attempt_no"
        ).fetchall() == [(1, "COLLECTED"), (2, "DUPLICATE")]
    finally:
        connection.close()


def test_finnhub_locator_rotation_refreshes_locator_with_marked_duplicate_receipt(
    tmp_path,
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    old_url = "https://finnhub.io/api/news?id=" + "a" * 64
    new_url = "https://finnhub.io/api/news?id=" + "b" * 64
    first_item = NewsConnectorItem(
        canonical_url=old_url,
        source_article_id="8279958",
        title="Company update",
        summary_text="Metadata only",
        published_at=NOW,
        observed_at=NOW,
    )
    second_item = NewsConnectorItem(
        canonical_url=new_url,
        source_article_id="8279958",
        title="Company update",
        summary_text="Metadata only",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="symbol:GOOGL",
    )[0]

    second = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (second_item,)),
        target_ref="symbol:GOOGL",
    )[0]
    exact_repeat_item = NewsConnectorItem(
        canonical_url=new_url,
        source_article_id="8279958",
        title="Company update",
        summary_text="Metadata only",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=30),
    )
    third = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (exact_repeat_item,)),
        target_ref="symbol:GOOGL",
    )[0]

    assert second.outcome is SaveOutcome.DUPLICATE
    assert second.news_document_id == first.news_document_id
    assert third.outcome is SaveOutcome.DUPLICATE
    assert third.news_document_id == first.news_document_id
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT canonical_url,last_seen_at FROM news_documents"
        ).fetchone() == (new_url, "2026-08-02T01:17:03.456789+00:00")
        assert connection.execute(
            "SELECT status,response_fingerprint FROM intake_receipts "
            "ORDER BY finished_at"
        ).fetchall() == [
            ("COLLECTED", None),
            ("DUPLICATE", hashlib.sha256(("v1\x00" + new_url).encode()).hexdigest()),
            ("DUPLICATE", None),
        ]
    finally:
        connection.close()


def test_legacy_bare_finnhub_locator_can_refresh_to_valid_locator(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = NewsConnectorItem(
        canonical_url="https://finnhub.io/api/news?id=101",
        source_article_id="legacy-1",
        title="Legacy article",
        observed_at=NOW,
    )
    repaired = NewsConnectorItem(
        canonical_url="https://finnhub.io/api/news?id=" + "c" * 64,
        source_article_id="legacy-1",
        title="Legacy article",
        observed_at=NOW + timedelta(minutes=15),
    )
    inserted = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first,)),
        target_ref="symbol:GOOGL",
    )[0]
    legacy_url = "https://finnhub.io/api/news"
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "UPDATE news_documents SET canonical_url=?,canonical_url_hash=?",
            (legacy_url, hashlib.sha256(("v1\x00" + legacy_url).encode()).hexdigest()),
        )
        connection.commit()
    finally:
        connection.close()

    refreshed = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (repaired,)),
        target_ref="symbol:GOOGL",
    )[0]

    assert refreshed.outcome is SaveOutcome.DUPLICATE
    assert refreshed.news_document_id == inserted.news_document_id


def test_finnhub_market_locator_rotation_uses_same_guarded_refresh(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = NewsConnectorItem(
        canonical_url="https://finnhub.io/api/news?id=101",
        source_article_id="market-1",
        title="Market update",
        observed_at=NOW,
    )
    changed = NewsConnectorItem(
        canonical_url="https://finnhub.io/api/news?id=102",
        source_article_id="market-1",
        title="Market update",
        observed_at=NOW + timedelta(minutes=15),
    )
    inserted = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first,)),
        target_ref="category:general",
    )[0]

    refreshed = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (changed,)),
        target_ref="category:general",
    )[0]

    assert refreshed.outcome is SaveOutcome.DUPLICATE
    assert refreshed.news_document_id == inserted.news_document_id


def test_finnhub_locator_rotation_owned_by_other_document_remains_conflict(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    old_url = "https://finnhub.io/api/news?id=101"
    occupied_url = "https://finnhub.io/api/news?id=102"
    first = NewsConnectorItem(
        canonical_url=old_url,
        source_article_id="article-a",
        title="Article A",
        observed_at=NOW,
    )
    occupied = NewsConnectorItem(
        canonical_url=occupied_url,
        source_article_id="article-b",
        title="Article B",
        observed_at=NOW,
    )
    persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first, occupied)),
        target_ref="symbol:GOOGL",
    )
    before = sqlite3.connect(path)
    try:
        counts = tuple(
            before.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("intake_requests", "intake_receipts", "news_documents")
        )
    finally:
        before.close()
    collision = NewsConnectorItem(
        canonical_url=occupied_url,
        source_article_id="article-a",
        title="Article A",
        observed_at=NOW + timedelta(minutes=15),
    )

    conflict = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (collision,)),
        target_ref="symbol:GOOGL",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    connection = sqlite3.connect(path)
    try:
        after = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("intake_requests", "intake_receipts", "news_documents")
        )
        assert after == counts
        assert connection.execute(
            "SELECT canonical_url FROM news_documents "
            "WHERE source_article_id='article-a'"
        ).fetchone() == (old_url,)
    finally:
        connection.close()


def test_finnhub_locator_rotation_with_title_drift_remains_conflict(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    old_url = "https://finnhub.io/api/news?id=" + "a" * 64
    new_url = "https://finnhub.io/api/news?id=" + "b" * 64
    first = NewsConnectorItem(
        canonical_url=old_url,
        source_article_id="8279958",
        title="Company update",
        observed_at=NOW,
    )
    changed = NewsConnectorItem(
        canonical_url=new_url,
        source_article_id="8279958",
        title="Changed title",
        observed_at=NOW + timedelta(minutes=15),
    )
    inserted = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first,)),
        target_ref="symbol:GOOGL",
    )[0]

    conflict = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (changed,)),
        target_ref="symbol:GOOGL",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.news_document_id == inserted.news_document_id
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT canonical_url,title FROM news_documents"
        ).fetchone() == (old_url, "Company update")
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_finnhub_connector_non_finnhub_locator_drift_remains_conflict(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = NewsConnectorItem(
        canonical_url="https://publisher.example/story/1",
        source_article_id="8279958",
        title="Company update",
        observed_at=NOW,
    )
    changed = NewsConnectorItem(
        canonical_url="https://publisher.example/story/2",
        source_article_id="8279958",
        title="Company update",
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first,)),
        target_ref="symbol:GOOGL",
    )

    conflict = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (changed,)),
        target_ref="symbol:GOOGL",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT canonical_url FROM news_documents"
        ).fetchone() == ("https://publisher.example/story/1",)
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_bbc_guid_rotation_same_url_payload_refreshes_document_identity(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.bbc.com/news/articles/story-1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="old-guid",
        title="Business update",
        published_at=NOW,
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="new-guid",
        title="Business update",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )[0]

    revised = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:business",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == ["source_article_id"]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_article_id,title,revision_no FROM news_documents"
        ).fetchone() == ("new-guid", "Business update", 2)
        assert (
            connection.execute(
                "SELECT response_fingerprint FROM intake_receipts ORDER BY attempt_no"
            ).fetchall()[1][0]
            is not None
        )
    finally:
        connection.close()


def test_bbc_guid_rotation_with_title_change_is_revision_not_conflict(tmp_path):
    """Rotation and revision arriving together stay one document.

    BBC rotates the GUID while correcting the headline. Because the connector
    declares ``identity_churns`` the canonical URL carries identity, so the
    pair is a revision and the document takes the corrected headline.
    """
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.bbc.com/news/articles/story-1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="old-guid",
        title="Business update",
        published_at=NOW,
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="new-guid",
        title="Business update, corrected",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )

    revised = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:business",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == [
        "source_article_id",
        "title",
        "title_fingerprint",
    ]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_article_id,title,revision_no FROM news_documents"
        ).fetchone() == ("new-guid", "Business update, corrected", 2)
    finally:
        connection.close()


def test_bbc_guid_rotation_with_published_change_is_revision_not_conflict(tmp_path):
    """The highest-volume live conflict shape becomes a revision.

    Matches the observed ``["source_article_id","published_at"]`` conflicts.
    """
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.bbc.com/news/articles/story-2"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="old-guid",
        title="Business update",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="new-guid",
        title="Business update",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )

    revised = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:business",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == ["published_at", "source_article_id"]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_article_id,published_at,revision_no FROM news_documents"
        ).fetchone() == (
            "new-guid",
            NOW.isoformat(timespec="microseconds"),
            2,
        )
    finally:
        connection.close()


def test_yonhap_title_revision_same_identity_is_marked_duplicate(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.yna.co.kr/view/AKR1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="AKR1",
        title="시장 제목",
        published_at=NOW,
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="AKR1",
        title="수정된 시장 제목",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        YONHAP_MARKET_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:market",
    )

    revised = persist_news_result(
        path,
        YONHAP_MARKET_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:market",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT title,revision_no FROM news_documents"
        ).fetchone() == ("수정된 시장 제목", 2)
    finally:
        connection.close()


def test_google_cosmetic_title_publisher_revision_is_recorded_and_applied(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://news.google.com/rss/articles/article-1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="article-1",
        title="Headline - Publisher One",
        publisher_name="Publisher One",
        publisher_site_url="https://publisher.example/",
        published_at=NOW,
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="article-1",
        title="Headline - Publisher 1",
        publisher_name="Publisher 1",
        publisher_site_url="https://publisher.example/",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        GOOGLE_NEWS_SEARCH_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="query:portfolio",
    )[0]

    revised = persist_news_result(
        path,
        GOOGLE_NEWS_SEARCH_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="query:portfolio",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT title,publisher_name,last_seen_at,analysis_state,revision_no "
            "FROM news_documents"
        ).fetchone() == (
            "Headline - Publisher 1",
            "Publisher 1",
            "2026-08-02T01:17:03.456789+00:00",
            "PENDING",
            2,
        )
        receipts = connection.execute(
            "SELECT status,response_fingerprint FROM intake_receipts "
            "ORDER BY attempt_no"
        ).fetchall()
        assert receipts[0] == ("COLLECTED", None)
        assert receipts[1][0] == "DUPLICATE"
        assert receipts[1][1] is not None
        assert len(receipts[1][1]) == 64
    finally:
        connection.close()


def test_bbc_published_at_revision_same_identity_is_duplicate_with_old_new_evidence(
    tmp_path,
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.bbc.com/news/articles/story-1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="story-1",
        title="Business update",
        summary_text="First summary",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="story-1",
        title="Business update",
        summary_text="First summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )[0]

    revised = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:business",
    )[0]
    replay = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(
            NewsFetchStatus.COLLECTED,
            (
                NewsConnectorItem(
                    canonical_url=url,
                    source_article_id="story-1",
                    title="Business update",
                    summary_text="First summary",
                    published_at=NOW,
                    observed_at=NOW + timedelta(days=1),
                ),
            ),
        ),
        target_ref="feed:business",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.revision_evidence_id is not None
    assert revised.conflict_audit_id is None
    # Once the revision is applied the same payload is simply a duplicate --
    # there is nothing left to revise, so no second evidence row appears.
    assert replay.outcome is SaveOutcome.DUPLICATE
    assert replay.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == ["published_at"]
    assert rows[0]["old_published_at"] == (NOW - timedelta(hours=1)).isoformat(
        timespec="microseconds"
    )
    assert rows[0]["new_published_at"] == NOW.isoformat(timespec="microseconds")
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT title,published_at,revision_no FROM news_documents"
        ).fetchone() == (
            "Business update",
            NOW.isoformat(timespec="microseconds"),
            2,
        )
    finally:
        connection.close()


def test_bbc_combined_title_summary_published_revision_is_single_evidence_row(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.bbc.com/news/articles/story-2"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="story-2",
        title="Business update",
        summary_text="First summary",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="story-2",
        title="Business update, corrected",
        summary_text="Corrected summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )

    revised = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="feed:business",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.revision_evidence_id is not None
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == [
        "published_at",
        "summary_text",
        "title",
        "title_fingerprint",
    ]
    assert rows[0]["old_title"] == "Business update"
    assert rows[0]["new_title"] == "Business update, corrected"
    assert rows[0]["old_summary_text"] == "First summary"
    assert rows[0]["new_summary_text"] == "Corrected summary"


def test_bbc_identity_drift_with_published_change_remains_conflict(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first_item = NewsConnectorItem(
        canonical_url="https://www.bbc.com/news/articles/story-3",
        source_article_id="story-3",
        title="Business update",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    drifted_item = NewsConnectorItem(
        canonical_url="https://www.bbc.com/news/articles/story-3-updated",
        source_article_id="story-3",
        title="Business update",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )

    conflict = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (drifted_item,)),
        target_ref="feed:business",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.conflict_audit_id is not None
    assert conflict.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.revisions.sqlite3").exists()


def test_yonhap_published_at_drift_remains_conflict_without_revision(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://www.yna.co.kr/view/AKR2"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="AKR2",
        title="시장 제목",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    drifted_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="AKR2",
        title="시장 제목",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        YONHAP_MARKET_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:market",
    )

    conflict = persist_news_result(
        path,
        YONHAP_MARKET_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (drifted_item,)),
        target_ref="feed:market",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.revisions.sqlite3").exists()


def test_finnhub_market_metadata_revision_same_identity_is_duplicate_with_evidence(
    tmp_path,
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://finnhub.io/api/news?id=123"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="123",
        title="Market headline",
        summary_text="First summary",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="123",
        title="Market headline, corrected",
        summary_text="Corrected summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="category:general",
    )[0]

    revised = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="category:general",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.revision_evidence_id is not None
    assert revised.conflict_audit_id is None
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["connector_id"] == "finnhub-market-news-v1"
    assert rows[0]["changed_fields"] == [
        "published_at",
        "summary_text",
        "title",
        "title_fingerprint",
    ]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT title,summary_text,published_at,revision_no FROM news_documents"
        ).fetchone() == (
            "Market headline, corrected",
            "Corrected summary",
            NOW.isoformat(timespec="microseconds"),
            2,
        )
    finally:
        connection.close()


def test_finnhub_market_article_id_drift_is_not_guid_rotation_and_conflicts(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://finnhub.io/api/news?id=123"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="123",
        title="Market headline",
        published_at=NOW,
        observed_at=NOW,
    )
    drifted_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="124",
        title="Market headline",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="category:general",
    )

    conflict = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (drifted_item,)),
        target_ref="category:general",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.revisions.sqlite3").exists()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_article_id FROM news_documents"
        ).fetchone() == ("123",)
    finally:
        connection.close()


def test_finnhub_company_metadata_revision_matches_market_connector(tmp_path):
    """Company news revises like market news; the two share a publisher."""
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://news.example.com/apple-story"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="9001",
        title="Company headline",
        summary_text="First summary",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    drifted_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="9001",
        title="Company headline, corrected",
        summary_text="Corrected summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="symbol:AAPL",
    )

    revised = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (drifted_item,)),
        target_ref="symbol:AAPL",
    )[0]

    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.conflict_audit_id is None
    assert revised.revision_evidence_id is not None
    assert not path.with_name(f"{path.stem}.conflicts.sqlite3").exists()
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == [
        "published_at",
        "summary_text",
        "title",
        "title_fingerprint",
    ]


def test_finnhub_market_published_at_only_revision_same_identity_is_duplicate(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://finnhub.io/api/news?id=200"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="200",
        title="Market headline",
        summary_text="Summary",
        published_at=NOW - timedelta(hours=1),
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="200",
        title="Market headline",
        summary_text="Summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    first = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="category:general",
    )[0]
    revised = persist_news_result(
        path,
        FINNHUB_MARKET_NEWS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="category:general",
    )[0]
    assert revised.outcome is SaveOutcome.DUPLICATE
    assert revised.news_document_id == first.news_document_id
    assert revised.revision_evidence_id is not None
    assert revised.conflict_audit_id is None
    rows = _read_revision_rows(path)
    assert len(rows) == 1
    assert rows[0]["changed_fields"] == ["published_at"]


def test_bbc_url_only_drift_same_article_id_remains_conflict(tmp_path):
    """URL is immutable identity; changing only URL must conflict, not revise."""
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first_item = NewsConnectorItem(
        canonical_url="https://www.bbc.com/news/articles/story-url-1",
        source_article_id="story-url-1",
        title="Business update",
        published_at=NOW,
        observed_at=NOW,
    )
    drifted_item = NewsConnectorItem(
        canonical_url="https://www.bbc.com/news/articles/story-url-1-revised",
        source_article_id="story-url-1",
        title="Business update",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="feed:business",
    )
    conflict = persist_news_result(
        path,
        BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (drifted_item,)),
        target_ref="feed:business",
    )[0]
    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.revision_evidence_id is None
    assert not path.with_name(f"{path.stem}.revisions.sqlite3").exists()


def test_google_summary_revision_remains_conflict_without_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    url = "https://news.google.com/rss/articles/article-1"
    first_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="article-1",
        title="Headline - Publisher",
        publisher_name="Publisher",
        summary_text="First summary",
        published_at=NOW,
        observed_at=NOW,
    )
    revised_item = NewsConnectorItem(
        canonical_url=url,
        source_article_id="article-1",
        title="Headline - Publisher",
        publisher_name="Publisher",
        summary_text="Changed summary",
        published_at=NOW,
        observed_at=NOW + timedelta(minutes=15),
    )
    persist_news_result(
        path,
        GOOGLE_NEWS_SEARCH_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (first_item,)),
        target_ref="query:portfolio",
    )

    conflict = persist_news_result(
        path,
        GOOGLE_NEWS_SEARCH_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, (revised_item,)),
        target_ref="query:portfolio",
    )[0]

    assert conflict.outcome is SaveOutcome.CONFLICT
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT summary_text FROM news_documents"
        ).fetchone() == ("First summary",)
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_non_collected_result_is_not_persisted(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    receipts = persist_news_result(
        path,
        FINNHUB_COMPANY_NEWS,
        NewsFetchResult(NewsFetchStatus.EMPTY),
        target_ref="symbol:AAPL",
    )
    assert receipts == ()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM intake_requests"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_direct_url_save_inserts_atomic_request_receipt_and_news(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)

    receipt = save_external_news(path, _write())

    assert receipt.outcome is SaveOutcome.INSERTED
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM sources").fetchone() == (1,)
        assert connection.execute(
            "SELECT source_native_key FROM sources"
        ).fetchone() == ("domain:example.com",)
        assert connection.execute(
            "SELECT origin_kind, connector_id, request_state FROM intake_requests"
        ).fetchone() == ("DIRECT_URL", None, "COMPLETED")
        assert connection.execute(
            "SELECT status, source_record_type FROM intake_receipts"
        ).fetchone() == ("COLLECTED", "news_document")
        assert connection.execute("SELECT count(*) FROM news_documents").fetchone() == (
            1,
        )
    finally:
        connection.close()


def test_tracking_fragment_retry_reuses_deterministic_request_and_receipt(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = save_external_news(path, _write())

    second = save_external_news(
        path, _write("https://news.example.com/story/7?utm_source=x#section")
    )

    assert first.outcome is SaveOutcome.INSERTED
    assert second.outcome is SaveOutcome.DUPLICATE
    assert second.news_document_id == first.news_document_id
    assert second.intake_request_id == first.intake_request_id
    assert second.intake_receipt_id == first.intake_receipt_id
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM news_documents").fetchone() == (
            1,
        )
        assert connection.execute(
            "SELECT count(*) FROM intake_requests"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT count(DISTINCT idempotency_key) FROM intake_requests"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_empty_query_allowlist_makes_business_query_variant_duplicate(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = save_external_news(path, _write())

    second = save_external_news(
        path, _write("https://news.example.com/story/7?ref=x&page=2")
    )

    assert second.outcome is SaveOutcome.DUPLICATE
    assert second.news_document_id == first.news_document_id


def test_dot_segment_path_variant_is_duplicate(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    first = save_external_news(path, _write())

    second = save_external_news(path, _write("https://news.example.com/a/../story/7"))

    assert second.outcome is SaveOutcome.DUPLICATE
    assert second.news_document_id == first.news_document_id


def test_same_identity_changed_immutable_payload_conflicts_with_zero_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    inserted = save_external_news(path, _write())
    connection = sqlite3.connect(path)
    before = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sources", "intake_requests", "intake_receipts", "news_documents")
    )
    connection.close()

    conflict = save_external_news(path, _write(title="Changed title"))

    assert conflict.outcome is SaveOutcome.CONFLICT
    assert conflict.news_document_id == inserted.news_document_id
    connection = sqlite3.connect(path)
    try:
        after = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "intake_requests",
                "intake_receipts",
                "news_documents",
            )
        )
        assert after == before
        assert connection.execute("SELECT title FROM news_documents").fetchone() == (
            "Markets rise",
        )
    finally:
        connection.close()


def test_source_article_and_url_matching_different_rows_conflicts_without_mutation(
    tmp_path,
):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write(source_article_id="article-a"))
    save_external_news(
        path,
        _write(
            "https://news.example.com/story/8",
            source_article_id="article-b",
        ),
    )
    connection = sqlite3.connect(path)
    before = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sources", "intake_requests", "intake_receipts", "news_documents")
    )
    connection.close()

    conflict = save_external_news(
        path,
        _write(
            "https://news.example.com/story/8",
            source_article_id="article-a",
        ),
    )

    assert conflict.outcome is SaveOutcome.CONFLICT
    connection = sqlite3.connect(path)
    try:
        after = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "intake_requests",
                "intake_receipts",
                "news_documents",
            )
        )
        assert after == before
    finally:
        connection.close()


def test_provisional_content_hash_fallback_is_source_scoped(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    content_hash = "a" * 64
    first = save_external_news(path, _write(content_hash=content_hash))
    second = save_external_news(
        path,
        _write(
            "https://other.example.net/story/7",
            source_article_id="article-7",
            content_hash=content_hash,
        ),
    )

    assert first.outcome is SaveOutcome.INSERTED
    assert second.outcome is SaveOutcome.INSERTED


def test_reopen_read_rejects_tampered_schema_meta_without_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    inserted = save_external_news(path, _write())
    connection = sqlite3.connect(path)
    connection.execute("UPDATE schema_meta SET schema_id='tampered'")
    connection.commit()
    connection.close()

    with pytest.raises(RuntimeError, match="schema_mismatch"):
        ExternalNewsStore(path).read_document(inserted.news_document_id)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT schema_id FROM schema_meta").fetchone() == (
            "tampered",
        )
        assert connection.total_changes == 0
    finally:
        connection.close()


def test_reopen_save_rejects_tampered_ddl_without_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    connection = sqlite3.connect(path)
    connection.execute("CREATE INDEX tampered_idx ON news_documents(title)")
    connection.commit()
    before = tuple(
        connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in ("sources", "intake_requests", "intake_receipts", "news_documents")
    )
    connection.close()

    with pytest.raises(RuntimeError, match="schema_mismatch"):
        save_external_news(path, _write())

    connection = sqlite3.connect(path)
    try:
        after = tuple(
            connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "sources",
                "intake_requests",
                "intake_receipts",
                "news_documents",
            )
        )
        assert after == before
        assert connection.execute(
            "SELECT count(*) FROM sqlite_master WHERE name='tampered_idx'"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_normal_read_of_missing_db_creates_nothing(tmp_path):
    path = tmp_path / "absent" / "external.sqlite3"

    assert ExternalNewsStore(path).read_document("missing") is None

    assert not path.parent.exists()
    assert not path.exists()
    assert not Path(f"{path}-journal").exists()
    assert not Path(f"{path}-wal").exists()
    assert not Path(f"{path}-shm").exists()


def test_news_storage_import_boundary_has_values_records_and_no_forbidden_lanes():
    root = Path(__file__).parents[1] / "src" / "tokmo"
    files = [
        root / "records" / "external_news.py",
        root / "records" / "news_conflict_audit.py",
        root / "records" / "news_revision_evidence.py",
        root / "programs" / "external_news_storage.py",
    ]
    imported = set()
    program_imported = set()
    for file in files:
        tree = ast.parse(file.read_text())
        modules = {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        modules.update(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        imported.update(modules)
        if file.name == "external_news_storage.py":
            program_imported.update(modules)
    assert "tokmo.values.external_news" in program_imported
    assert "tokmo.records.external_news" in imported
    assert not any(
        token in module.casefold()
        for module in imported
        for token in ("facts", "requests", "http", "socket", "price", "order")
    )
    relay_files = [
        *root.glob("**/*telegram*.py"),
        root / "programs" / "analysis_pipeline.py",
        root / "programs" / "prompt_builder.py",
    ]
    assert all("news_conflict_audit" not in file.read_text() for file in relay_files)
    assert all("news_revision_evidence" not in file.read_text() for file in relay_files)
    assert all("_read_revisions" not in file.read_text() for file in relay_files)


def test_news_write_rejects_noncanonical_url_non_utc_and_bad_hash():
    with pytest.raises(ValueError):
        _write("relative/story")
    with pytest.raises(ValueError):
        _write(observed_at=NOW.replace(tzinfo=None))
    with pytest.raises(ValueError):
        _write(content_hash="A" * 64)


def test_provisional_source_uses_registrable_domain(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write("https://news.example.co.uk/story/7"))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_native_key FROM sources"
        ).fetchone() == ("domain:example.co.uk",)
    finally:
        connection.close()


def test_read_document_happy_path_returns_saved_document(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    inserted = save_external_news(path, _write())

    document = ExternalNewsStore(path).read_document(inserted.news_document_id)

    assert document is not None
    assert document["title"] == "Markets rise"
    assert document["canonical_url"] == "https://news.example.com/story/7"


def test_save_rejects_unsafe_db_mode_without_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    os.chmod(path, 0o644)

    with pytest.raises(OSError):
        save_external_news(path, _write())

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT count(*) FROM news_documents").fetchone() == (
            0,
        )
    finally:
        connection.close()


def test_news_write_repr_redacts_raw_text():
    value = _write(
        summary_text="secret-summary",
        body_text="secret-body",
        content_state=ContentState.BODY_AVAILABLE,
        license_state=LicenseState.PERMITTED,
    )

    rendered = repr(value)

    assert "secret-summary" not in rendered
    assert "secret-body" not in rendered


def test_body_available_round_trips_through_read_api(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    inserted = save_external_news(
        path,
        _write(
            body_text="licensed-body",
            content_state=ContentState.BODY_AVAILABLE,
            license_state=LicenseState.PERMITTED,
        ),
    )

    document = ExternalNewsStore(path).read_document(inserted.news_document_id)

    assert document is not None
    assert document["body_text"] == "licensed-body"
    assert document["content_state"] == "BODY_AVAILABLE"
    assert document["license_state"] == "PERMITTED"


def test_bootstrap_recovers_only_completely_empty_existing_db(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    path.parent.mkdir(mode=0o700)
    sqlite3.connect(path).close()
    os.chmod(path, 0o600)

    bootstrap_external_news(path, now=NOW)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT schema_id FROM schema_meta").fetchone() == (
            "external-news-n5",
        )
    finally:
        connection.close()


def test_korean_public_domain_is_not_collapsed_and_ip_literal_is_rejected(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, _write("https://www.mois.go.kr/story/7"))
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT source_native_key FROM sources"
        ).fetchone() == ("domain:mois.go.kr",)
    finally:
        connection.close()

    with pytest.raises(ValueError):
        _write("https://93.184.216.34/story/7")


def test_news_write_rejects_plain_string_enums_and_malformed_hosts():
    with pytest.raises(ValueError):
        _write(content_mode="NEWS")
    with pytest.raises(ValueError):
        _write("https://example.com:abc/story/7")
    with pytest.raises(ValueError):
        _write("https://example.com./story/7")


def test_finnhub_semantic_id_query_is_preserved_as_document_identity(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    result = NewsFetchResult(
        NewsFetchStatus.COLLECTED,
        (
            NewsConnectorItem(
                canonical_url="https://finnhub.io/api/news?id=101",
                source_article_id="101",
                title="First",
                observed_at=NOW,
            ),
            NewsConnectorItem(
                canonical_url="https://finnhub.io/api/news?id=102",
                source_article_id="102",
                title="Second",
                observed_at=NOW,
            ),
        ),
    )

    receipts = persist_news_result(
        path, FINNHUB_COMPANY_NEWS, result, target_ref="symbol:NVDA"
    )

    assert [receipt.outcome for receipt in receipts] == [
        SaveOutcome.INSERTED,
        SaveOutcome.INSERTED,
    ]
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT canonical_url FROM news_documents ORDER BY source_article_id"
        ).fetchall() == [
            ("https://finnhub.io/api/news?id=101",),
            ("https://finnhub.io/api/news?id=102",),
        ]
    finally:
        connection.close()


def test_finnhub_current_sha256_identity_query_is_preserved(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    identity = "a" * 64
    result = NewsFetchResult(
        NewsFetchStatus.COLLECTED,
        (
            NewsConnectorItem(
                canonical_url=f"https://finnhub.io/api/news?id={identity}",
                source_article_id="live-identity",
                title="Current Finnhub identity",
                observed_at=NOW,
            ),
        ),
    )

    receipts = persist_news_result(
        path, FINNHUB_COMPANY_NEWS, result, target_ref="symbol:GOOGL"
    )

    assert receipts[0].outcome is SaveOutcome.INSERTED
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT canonical_url FROM news_documents"
        ).fetchone() == (f"https://finnhub.io/api/news?id={identity}",)
    finally:
        connection.close()
