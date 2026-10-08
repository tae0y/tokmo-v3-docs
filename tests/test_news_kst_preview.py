import hashlib
import sqlite3

import pytest

from preview_news_kst import preview, project


def test_preview_preserves_bytes_ids_audits_and_detects_mixed_order(tmp_path):
    path = tmp_path / "source.sqlite3"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE news_documents(id TEXT, published_at TEXT, first_seen_at TEXT, canonical_json TEXT);"
        "CREATE TABLE news_decision_calls(id TEXT, started_at TEXT, request TEXT, response TEXT);")
    connection.executemany("INSERT INTO news_documents VALUES(?,?,?,?)", [
        ("older", "2026-09-07T12:00:00+09:00", "2026-09-07T12:00:00.000000+09:00", '{"published_at":"2026-09-07"}'),
        ("newer", "2026-09-07T04:00:00+00:00", "2026-09-07T04:00:00.000000+00:00", '{"id":"keep-hash"}')])
    connection.execute("INSERT INTO news_decision_calls VALUES('call','2026-09-07T04:00:00Z','{}','{}')")
    connection.commit(); connection.close()
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = preview(path)
    assert result["writes"] == 0 and result["invalid_fields"] == 0
    assert set(result["tables"]) == {"news_documents"}
    table = result["tables"]["news_documents"]
    assert table["fields"]["published_at"]["lexical_order_would_change"]
    assert table["fields"]["first_seen_at"]["would_change"] == 1
    assert table["non_time_values_sha256"] == table["projected_non_time_values_sha256"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert len(project("2026-09-07T04:00:00.000000+00:00")) == 32
    with pytest.raises(ValueError):
        project("20260907")
    with pytest.raises(ValueError):
        project("2026-09-07T13:00:00")
