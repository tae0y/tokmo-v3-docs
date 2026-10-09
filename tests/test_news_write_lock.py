"""Cross-process write exclusion of the NEWS connector transaction.

A second connection in the same process shares SQLite's per-inode lock state
and still sees SQLITE_BUSY, so only a separate process proves POSIX exclusion.
"""

import json
import os
import sqlite3
import subprocess
import sys
from contextlib import nullcontext
from datetime import UTC, datetime
from pathlib import Path

import pytest

from tokmo.programs.external_news_storage import bootstrap_external_news
from tokmo.records.external_news import ExternalNewsStore

NOW = datetime(2026, 8, 2, 1, 2, 3, 456789, tzinfo=UTC)
SRC = str(Path(__file__).resolve().parents[1] / "src")
_PROBE = """
import sqlite3, sys
connection = sqlite3.connect(sys.argv[1], timeout=0, isolation_level=None)
try:
    connection.execute("BEGIN IMMEDIATE")
except sqlite3.OperationalError as error:
    print("BUSY" if error.sqlite_errorcode == sqlite3.SQLITE_BUSY else repr(error))
else:
    connection.execute("ROLLBACK")
    print("ACQUIRED")
"""
_WRITER = """
import json, sqlite3, sys
from datetime import datetime, timezone
from tokmo.programs.news_intake import persist_news_result
from tokmo.values.news_connectors import (
    BBC_BUSINESS_RSS, NewsConnectorItem, NewsFetchResult, NewsFetchStatus)
now = datetime(2026, 8, 2, 1, 2, 3, tzinfo=timezone.utc)
worker, count = sys.argv[2], int(sys.argv[3])
items = tuple(NewsConnectorItem(
    canonical_url=f"https://example.com/{worker}/{i}", title=f"Title {worker} {i}",
    source_article_id=f"{worker}-{i}", observed_at=now) for i in range(count))
saved = []
try:
    persist_news_result(sys.argv[1], BBC_BUSINESS_RSS,
        NewsFetchResult(NewsFetchStatus.COLLECTED, items),
        target_ref=f"feed:{worker}", on_saved=saved.append)
except (sqlite3.Error, RuntimeError) as error:
    cause = error.__cause__ if error.__cause__ is not None else error
    if getattr(cause, "sqlite_errorcode", None) not in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED) \
            and error.args != ("storage_changed_during_validation",):
        raise
print(json.dumps({"saved": len(saved)}))
"""


def _probe(path):
    return subprocess.run(
        [sys.executable, "-c", _PROBE, str(path)],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    ).stdout.strip()


@pytest.mark.parametrize("batched", [False, True])
def test_other_process_cannot_reserve_while_connector_transaction_is_open(
    tmp_path, batched
):
    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    store = ExternalNewsStore(path)
    with store.connector_batch() if batched else nullcontext():
        for _ in range(2 if batched else 1):  # cold, then cached validation
            with store._connector_transaction() as connection:
                assert connection.in_transaction
                assert _probe(path) == "BUSY"
            assert _probe(path) == "ACQUIRED"


def test_separate_process_writers_preserve_every_record_and_integrity(tmp_path):
    path = tmp_path / "news.sqlite3"
    bootstrap_external_news(path, now=NOW)
    env = {**os.environ, "PYTHONPATH": SRC, "PYTHONDONTWRITEBYTECODE": "1"}
    workers = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", _WRITER, str(path), f"w{n}", "40"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        for n in range(4)
    ]
    try:
        results = [worker.communicate(timeout=120) for worker in workers]
    finally:
        for worker in workers:
            if worker.poll() is None:
                worker.kill()
                worker.wait()
    assert [worker.returncode for worker in workers] == [0] * 4, [
        err[-2000:] for _, err in results
    ]
    saved = [json.loads(out)["saved"] for out, _ in results]
    # Bounded busy failures remain permitted; every committed prefix must
    # survive exactly. Never add retries or require all contenders to win.
    expected = {f"w{n}-{i}" for n, count in enumerate(saved) for i in range(count)}
    assert 0 < len(expected) <= 160
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA quick_check").fetchall() == [("ok",)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert {
            r[0]
            for r in connection.execute("SELECT source_article_id FROM news_documents")
        } == expected
        assert connection.execute(
            "SELECT count(*) FROM intake_receipts WHERE status='COLLECTED'"
        ).fetchone() == (len(expected),)
