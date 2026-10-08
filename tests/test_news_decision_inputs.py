import hashlib
import json
import sqlite3

from tokmo.records.news_decision_inputs import read_news_observations, read_disclosure_observations, read_portfolio_context
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp


def news_db(path):
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE news_documents(news_document_id TEXT, source_id TEXT, title TEXT, body_text TEXT, summary_text TEXT, canonical_url TEXT, published_at TEXT, updated_at TEXT, first_seen_at TEXT, last_seen_at TEXT, revision_no INTEGER)")
    for key, first, last, revision in [("a", "09:00", "09:00", 1), ("b", "09:00", "09:00", 1), ("c", "09:01", "09:01", 1), ("revised", "09:00", "10:00", 2)]:
        stamp = lambda value: "2026-09-01T" + value + ":00+09:00"
        connection.execute("INSERT INTO news_documents VALUES(?,?,?,?,?,?,?,?,?,?,?)", (key,"test",key,"current text",None,"https://example.invalid/"+key,None,None,stamp(first),stamp(last),revision))
    connection.commit()
    connection.close()


def test_readonly_missing_and_same_time_cursor_do_not_lose_rows(tmp_path):
    path = tmp_path / "news.db"
    args = dict(since="2026-09-01T08:00:00+09:00", until="2026-09-01T09:30:00+09:00", limit=1)
    assert read_news_observations(path, **args)["status"] == "UNAVAILABLE"
    assert not path.exists()
    news_db(path)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    first = read_news_observations(path, **args)
    assert len(first["observations"]) == 2 and first["has_more"]
    second = read_news_observations(path, **(args | {"since": first["next_since"]}))
    assert [item["title"] for item in second["observations"]] == ["c"]
    assert not second["has_more"]
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_latest_revision_cannot_leak_into_past_and_reread_is_idempotent(tmp_path):
    path = tmp_path / "news.db"
    news_db(path)
    rows = read_news_observations(path, since="2026-09-01T09:30:00+09:00", until="2026-09-01T10:30:00+09:00")["observations"]
    assert len(rows) == 1 and rows[0]["history_scope"] == "current_revision_only"
    assert timestamp(rows[0]["observed_at"]) == timestamp("2026-09-01T10:00:00+09:00")
    store = NewsDecisionStore(tmp_path / "state.db")
    store.ingest(rows, timestamp("2026-09-01T11:00:00+09:00"))
    store.ingest([rows[0] | {"observed_at": "2026-09-01T10:15:00+09:00"}], timestamp("2026-09-01T11:00:00+09:00"))
    assert store.counts() == {"READY": 1}
    store.close()


def test_disclosure_reconstructs_then_known_listing_not_latest_body(tmp_path):
    path = tmp_path / "dart.db"
    connection = sqlite3.connect(path)
    connection.executescript("CREATE TABLE disclosures(rcept_no TEXT,corp_name TEXT,report_nm TEXT,canonical_json TEXT,first_observed_at TEXT,rcept_dt TEXT,stock_code TEXT); CREATE TABLE dart_revisions(revision_id INTEGER,rcept_no TEXT,previous_canonical_json TEXT,observed_canonical_json TEXT,observed_at TEXT)")
    old = json.dumps({"corp_name": "회사", "report_nm": "자기주식 소각 결정", "rm": ""})
    new = json.dumps({"corp_name": "회사", "report_nm": "자기주식 소각 결정 정정", "rm": "정정"})
    connection.execute("INSERT INTO disclosures VALUES(?,?,?,?,?,?,?)", ("123","회사","최신 제목",new,"2026-09-01T09:00:00+09:00","20260901","000001"))
    connection.execute("INSERT INTO dart_revisions VALUES(?,?,?,?,?)", (1,"123",old,new,"2026-09-01T10:00:00+09:00"))
    connection.commit(); connection.close()
    result = read_disclosure_observations(path, since="2026-09-01T08:00:00+09:00", until="2026-09-01T09:30:00+09:00")
    assert len(result["observations"]) == 1
    row = result["observations"][0]
    assert "정정" not in row["title"] and row["body_verified"] is False
    assert row["published_at"] is None


def test_portfolio_uses_capture_time_and_excludes_account_identity(tmp_path):
    path = tmp_path / "holdings.json"
    path.write_text(json.dumps({"captured_at": "2026-09-01T10:00:00+09:00", "accounts": [{"account_scope": "private-123", "holdings": [{"symbol": "005930", "quantity": 1}]}]}))
    assert read_portfolio_context(path, timestamp("2026-09-01T09:00:00+09:00"))["portfolio_status"] == "UNAVAILABLE_AT_THIS_TIME"
    result = read_portfolio_context(path, timestamp("2026-09-01T11:00:00+09:00"))
    assert result["portfolio_status"] == "OK" and "private-123" not in json.dumps(result)
