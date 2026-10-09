"""S1: KST time basis for NEWS-owned decision fields, mixed legacy-UTC/KST reads, per-call clock."""

import importlib.util
import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from tokmo.programs.news_decision_engine import RECORDED_TIME, NewsDecisionEngine
from tokmo.records.news_decision_inputs import (
    read_disclosure_observations,
    read_news_observations,
)
from tokmo.records.news_decision_store import NewsDecisionStore, encoded, kst, timestamp

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from report_news_decisions import report
from report_news_observation import health


class Clock:
    """Test seam: the instant every clock read returns; model fakes move it to simulate elapsed time."""

    def __init__(self, now):
        self.now = timestamp(now)

    def __call__(self):
        return self.now


def observation(key, at, **extra):
    return dict(
        id=key,
        observed_at=timestamp(at).isoformat(),
        source_kind="NEWS",
        title=key,
        text="본문 " + key,
        publisher="출처",
        url="https://example.invalid/" + key,
        **extra,
    )


def decision(ids, **overrides):
    return (
        dict(
            title="시장 충격",
            facts="전쟁 발생 보도",
            interpretation="유가 상승",
            why_now="새 사건",
            next_check="공식 발표",
            importance="HIGH",
            urgency="IMMEDIATE",
            confirmation="REPORTED",
            change="NEW",
            evidence_ids=list(ids),
            open=True,
            notify=True,
        )
        | overrides
    )


def test_alert_uses_each_cited_sources_publication_time_not_analysis_or_collection():
    from tokmo.programs.news_decision_engine import render_alert

    at = timestamp("2026-09-08T20:40:00+09:00")
    evidence = [
        observation("a", at, published_at="2026-09-08T05:05:00Z"),
        observation("b", at, published_at="2026-09-08T14:20:00+09:00"),
        observation("unused", at, published_at="2026-09-08T13:00:00+09:00"),
    ]
    body = render_alert(decision(["a", "b"], portfolio_note=""), evidence, at)
    assert (
        "기사 게시: 2026-09-08 14:05" in body and "기사 게시: 2026-09-08 14:20" in body
    )
    assert "13:00" not in body and "20:40" not in body and "판단 시각" not in body


@pytest.mark.parametrize("date", ["20260908", "2026-09-08"])
def test_disclosure_shows_only_collection_time(date):
    from tokmo.programs.news_decision_engine import source_time_label

    label = source_time_label(
        {
            "source_kind": "DISCLOSURE",
            "publication_date": date,
            "observed_at": "2026-09-08T05:31:00Z",
        }
    )
    assert label == "수집: 2026-09-08 14:31"


def test_assumed_timezone_and_unknown_publication_are_explicit():
    from tokmo.programs.news_decision_engine import source_time_label

    assert (
        source_time_label(
            {
                "source_kind": "NEWS",
                "published_at": "2026-09-08T11:25:00+09:00",
                "time_interpretation": "ASSUMED_KST",
            }
        )
        == "기사 게시: 2026-09-08 11:25 (시간대 미확인)"
    )
    assert (
        source_time_label(
            {
                "source_kind": "NEWS",
                "published_at": "bad",
                "first_seen_at": "2026-09-08T05:01:00Z",
                "observed_at": "2026-09-08T05:40:00Z",
            }
        )
        == "기사 게시 시각 미확인 · 수집: 2026-09-08 14:01"
    )
    assert source_time_label({"source_kind": "NEWS"}) == "기사 게시 시각 미확인"


def test_new_writes_are_kst_and_mixed_representations_compare_by_instant(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    now = timestamp("2026-09-07T14:00:00+09:00")
    legacy = dict(
        observation("legacy", "2026-09-07T04:11:00+00:00"),
        first_seen_at="2026-09-07T04:10:00+00:00",
        published_at="2026-09-07T04:00:00+00:00",
    )
    store.connection.execute(
        "INSERT INTO news_decision_inputs(id,observed_at,payload) VALUES(?,?,?)",
        ("legacy", legacy["observed_at"], encoded(legacy)),
    )
    store.connection.commit()
    store.ingest([observation("new", "2026-09-07T12:00:00+09:00")], now)
    stored = store.connection.execute(
        "SELECT observed_at FROM news_decision_inputs WHERE id='new'"
    ).fetchone()[0]
    assert stored == "2026-09-07T12:00:00+09:00"
    # Instant order, not string order: 12:00 KST precedes the legacy 04:11Z (=13:11 KST) row.
    assert [row["id"] for row in store.pending()] == ["new", "legacy"]
    # The same legacy content re-read in KST representation is the same observation, not a conflict.
    reread = dict(
        legacy,
        observed_at="2026-09-07T13:11:00+09:00",
        first_seen_at="2026-09-07T13:10:00+09:00",
        published_at="2026-09-07T13:00:00+09:00",
        time_interpretation="SOURCE_NATIVE",
    )
    store.ingest([reread], now)
    assert store.counts() == {"READY": 2}
    with pytest.raises(ValueError, match="reused"):
        store.ingest([dict(reread, text="다른 본문")], now)
    revised = dict(reread, id="news:doc:2", history_scope="current_revision_only")
    store.ingest([revised], now)
    store.ingest([dict(revised, observed_at="2026-09-07T13:40:00+09:00")], now)
    assert store.counts() == {"READY": 3}
    store.mark(["legacy", "new"], "ERROR", "x")
    assert [key for key, _ in store.errors()] == ["new", "legacy"]
    store.advance("news", "2026-09-07T05:00:00+00:00")
    assert store.cursor("news", None) == "2026-09-07T14:00:00+09:00"
    store.close()


def test_daily_budget_is_the_kst_calendar_day_including_legacy_reservations(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    # Legacy UTC-represented analysis reservation at 23:30 KST on 09-07, still STARTED.
    store.connection.execute(
        "INSERT INTO news_decision_calls VALUES('legacy','analyze','2026-09-07T14:30:00+00:00','STARTED',50,50,'{}',NULL,NULL)"
    )
    store.connection.commit()
    late = timestamp("2026-09-07T23:50:00+09:00")
    assert store.begin_call("k1", "analyze", {"now": kst(late)}, late, 40, 100) is None
    assert (
        store.connection.execute(
            "SELECT started_at FROM news_decision_calls WHERE id='k1'"
        ).fetchone()[0]
        == "2026-09-07T23:50:00+09:00"
    )
    with pytest.raises(ValueError, match="budget"):
        store.begin_call("k2", "analyze", {}, late + timedelta(minutes=1), 11, 100)
    after_midnight = timestamp("2026-09-08T00:30:00+09:00")
    assert store.begin_call("k3", "analyze", {}, after_midnight, 11, 100) is None
    with pytest.raises(ValueError, match="budget"):
        store.begin_call("k4", "analyze", {}, after_midnight, 90, 100)
    store.close()


def test_each_model_call_reads_the_clock_and_pins_kst_now_to_its_call_row(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    run = timestamp("2026-09-07T23:58:30+09:00")
    clock = Clock("2026-09-07T23:58:40+09:00")
    seen = []

    def screen_model(request):
        seen.append(request)
        clock.now = timestamp("2026-09-07T23:59:50+09:00")
        return {"candidates": [{"evidence_ids": ["a"]}], "ignored": []}

    def analyze_model(request):
        seen.append(request)
        clock.now = timestamp("2026-09-08T00:00:20+09:00")
        broken = decision(["a"])
        broken.pop("why_now")
        return decision(["a"]) if "correction" in request else broken

    engine = NewsDecisionEngine(store, screen_model, analyze_model, clock=clock)
    result = engine.tick([observation("a", run)], run)
    assert not result["failures"] and len(seen) == 3
    assert [item["now"] for item in seen] == [
        "2026-09-07T23:58:40+09:00",
        "2026-09-07T23:59:50+09:00",
        "2026-09-08T00:00:20+09:00",
    ]
    assert all(
        item["time_basis"] == "Asia/Seoul" and item["session_status"] == "UNVERIFIED"
        for item in seen
    )
    rows = store.connection.execute(
        "SELECT started_at,request,tokens FROM news_decision_calls ORDER BY rowid"
    ).fetchall()
    assert (
        [row[0] for row in rows]
        == [json.loads(row[1])["now"] for row in rows]
        == [item["now"] for item in seen]
    )
    event = store.events()[0]
    assert (
        event["updated_at"] == "2026-09-08T00:00:20+09:00"
        and event["next_check_at"] == "2026-09-08T06:00:20+09:00"
    )
    alert = result["alerts"][0]
    assert (
        alert["created_at"] == "2026-09-08T00:00:20+09:00"
        and alert["source_observed_at"] == "2026-09-07T23:58:30+09:00"
    )
    assert "판단 시각:" not in alert["body"]
    assert "기사 게시 시각 미확인 · 수집: 2026-09-07 23:58" in alert["body"]
    # Budget day follows the call instant: only the post-midnight call counts on 09-08.
    third = rows[2][2]
    with pytest.raises(ValueError, match="budget"):
        store.begin_call(
            "x", "analyze", {}, timestamp("2026-09-08T01:00:00+09:00"), 1, third
        )
    assert (
        store.begin_call(
            "x", "analyze", {}, timestamp("2026-09-08T01:00:00+09:00"), 1, third + 1
        )
        is None
    )
    store.close()


def test_portfolio_context_is_built_for_the_captured_call_instant_and_refreshed_per_correction(
    tmp_path,
):
    """R4 (added after the repair, not a RED case): one clock read per call; the context in the request
    is evaluated at that same instant, and a correction captures its own instant and context."""
    store = NewsDecisionStore(tmp_path / "state.db")
    captured = timestamp("2026-09-06T13:00:00+09:00")
    holdings = tmp_path / "holdings.json"
    holdings.write_text(
        json.dumps({"captured_at": captured.isoformat(), "accounts": []})
    )
    from tokmo.records.news_decision_inputs import read_portfolio_context

    clock = Clock(
        captured + timedelta(days=1, seconds=-1)
    )  # 1s inside the 24h freshness window
    seen = []

    def analyze_model(request):
        seen.append(request)
        clock.now = captured + timedelta(
            days=1, seconds=1
        )  # the correction happens after the boundary
        broken = decision(["a"])
        broken.pop("why_now")
        return decision(["a"]) if "correction" in request else broken

    engine = NewsDecisionEngine(
        store,
        lambda request: None,
        analyze_model,
        clock=clock,
        context_reader=lambda at: read_portfolio_context(holdings, at),
    )
    engine._analyze(
        [observation("a", captured)],
        None,
        captured,
        {"calls": 0, "decisions": [], "alerts": []},
    )
    assert [item["context"]["portfolio_status"] for item in seen] == ["OK", "STALE"]
    assert [item["now"] for item in seen] == [
        "2026-09-07T12:59:59+09:00",
        "2026-09-07T13:00:01+09:00",
    ]
    assert [
        row[0]
        for row in store.connection.execute(
            "SELECT started_at FROM news_decision_calls ORDER BY rowid"
        )
    ] == [item["now"] for item in seen]
    assert (
        store.events()[0]["context_snapshot"]["portfolio_status"] == "STALE"
    )  # the snapshot is the context actually sent
    store.close()


def test_recorded_mode_uses_tick_time_and_legacy_call_rows_stay_untouched(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    legacy = (
        "legacy",
        "analyze",
        "2026-09-07T04:11:00.352933+00:00",
        "OK",
        10,
        10,
        '{"now":"2026-09-07T04:11:00.352933+00:00","task":"analyze"}',
        '{"title":"old"}',
        None,
    )
    store.connection.execute(
        "INSERT INTO news_decision_calls VALUES(?,?,?,?,?,?,?,?,?)", legacy
    )
    store.connection.commit()
    seen = []
    screen_model = lambda request: (
        seen.append(request) or {"candidates": [{"evidence_ids": ["a"]}], "ignored": []}
    )
    engine = NewsDecisionEngine(
        store,
        screen_model,
        lambda request: seen.append(request) or decision(["a"]),
        clock=RECORDED_TIME,
    )
    at = timestamp("2026-09-07T13:11:00.352933+09:00")
    engine.tick([observation("a", at)], at)
    assert [item["now"] for item in seen] == ["2026-09-07T13:11:00.352933+09:00"] * 2
    rows = store.connection.execute(
        "SELECT * FROM news_decision_calls ORDER BY rowid"
    ).fetchall()
    assert rows[0] == legacy and len(rows) == 3
    assert (
        engine.tick([], at)["calls"] == 0
    )  # same recorded instant: replayed from the store, no new call
    store.close()


def test_brief_orders_events_by_instant_across_representations(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.db")
    base = {"decision": {"title": "t", "facts": "f", "open": True}}
    store.save_event(dict(base, id="utc-later", updated_at="2026-09-07T05:00:00+00:00"))
    store.save_event(
        dict(
            base,
            id="kst-earlier",
            updated_at="2026-09-07T12:00:00+09:00",
            last_sent={"delivered_at": "2026-09-07T11:00:00+09:00"},
        )
    )
    brief = NewsDecisionEngine._brief(store.events())
    assert [item["event_id"] for item in brief] == ["kst-earlier", "utc-later"]
    assert (
        brief[0]["last_sent_at"] == "2026-09-07T11:00:00+09:00"
        and brief[1]["last_sent_at"] is None
    )
    store.close()


def test_input_adapters_emit_kst_named_fields_and_keep_source_provenance(tmp_path):
    news = tmp_path / "news.db"
    with sqlite3.connect(news) as connection:
        connection.execute(
            "CREATE TABLE news_documents(news_document_id TEXT, source_id TEXT, title TEXT, body_text TEXT, summary_text TEXT, canonical_url TEXT, published_at TEXT, updated_at TEXT, first_seen_at TEXT, last_seen_at TEXT, revision_no INTEGER, time_interpretation TEXT)"
        )
        connection.execute(
            "INSERT INTO news_documents VALUES('d','finnhub','제목','본문',NULL,'https://example.invalid/d','2026-09-07T03:30:00.000000+00:00',NULL,'2026-09-07T04:10:00.123456+00:00','2026-09-07T04:10:00.123456+00:00',1,'SOURCE_NATIVE')"
        )
        connection.execute(
            "INSERT INTO news_documents VALUES('n','yna','제목2','본문2',NULL,'https://example.invalid/n',NULL,NULL,'2026-09-07T04:11:00+00:00','2026-09-07T04:11:00+00:00',1,'ASSUMED_KST')"
        )
    rows = read_news_observations(
        news, since="2026-09-07T04:00:00+00:00", until="2026-09-07T05:00:00+00:00"
    )
    by_id = {row["id"]: row for row in rows["observations"]}
    assert by_id["news:d:1"]["observed_at"] == "2026-09-07T13:10:00.123456+09:00"
    assert by_id["news:d:1"]["first_seen_at"] == "2026-09-07T13:10:00.123456+09:00"
    assert by_id["news:d:1"]["published_at"] == "2026-09-07T12:30:00+09:00"
    assert by_id["news:d:1"]["time_interpretation"] == "SOURCE_NATIVE"
    assert (
        by_id["news:n:1"]["published_at"] is None
        and by_id["news:n:1"]["time_interpretation"] == "ASSUMED_KST"
    )
    assert rows["next_since"] == "2026-09-07T14:00:00+09:00"
    dart = tmp_path / "dart.db"
    with sqlite3.connect(dart) as connection:
        connection.executescript(
            "CREATE TABLE disclosures(rcept_no TEXT,corp_name TEXT,report_nm TEXT,canonical_json TEXT,first_observed_at TEXT,rcept_dt TEXT,stock_code TEXT); CREATE TABLE dart_revisions(revision_id INTEGER,rcept_no TEXT,previous_canonical_json TEXT,observed_canonical_json TEXT,observed_at TEXT)"
        )
        connection.execute(
            "INSERT INTO disclosures VALUES('20260907000125','회사','보고서','{\"corp_name\":\"회사\",\"report_nm\":\"보고서\"}','2026-09-07T00:30:00+00:00','20260907',NULL)"
        )
    row = read_disclosure_observations(
        dart, since="2026-09-07T00:00:00+00:00", until="2026-09-07T01:00:00+00:00"
    )["observations"][0]
    assert (
        row["observed_at"] == "2026-09-07T09:30:00+09:00"
        and row["publication_date"] == "20260907"
        and row["published_at"] is None
    )


def test_reports_filter_and_stale_calls_by_instant_not_string(tmp_path):
    root = tmp_path / "data"
    (root / "stores").mkdir(parents=True)
    (root / "ops").mkdir()
    store = NewsDecisionStore(root / "stores/news_episodes.sqlite3")
    now = timestamp("2026-09-07T14:00:00+09:00")
    for key, started, status in (
        ("legacy-in", "2026-09-07T04:11:00+00:00", "OK"),
        ("kst-in", "2026-09-07T13:30:00+09:00", "STARTED"),
        ("kst-out", "2026-09-06T23:59:00+09:00", "OK"),
    ):
        store.connection.execute(
            "INSERT INTO news_decision_calls VALUES(?,?,?,?,?,?,?,?,?)",
            (key, "analyze", started, status, 5, 5, "{}", None, None),
        )
    for key, observed in (
        ("legacy", "2026-09-07T04:11:00+00:00"),
        ("kst", "2026-09-07T13:30:00+09:00"),
        ("early", "2026-09-06T23:59:00+09:00"),
    ):
        store.connection.execute(
            "INSERT INTO news_decision_inputs(id,observed_at,payload,state) VALUES(?,?,?,'DONE')",
            (key, observed, "{}"),
        )
    store.connection.commit()
    store.close()
    with sqlite3.connect(root / "ops/ops_kernel.sqlite3") as db:
        db.execute(
            "CREATE TABLE alert_outbox (alert_id TEXT, status TEXT, body_evidence_json TEXT, severity TEXT, created_at TEXT, delivered_at TEXT, provider_message_id TEXT)"
        )
    since = "2026-09-07T00:00:00+09:00"
    metrics = report(
        root / "stores/news_episodes.sqlite3",
        root / "ops/ops_kernel.sqlite3",
        since=since,
        until=now.isoformat(),
    )
    assert (
        metrics["model_calls"] == 2
        and metrics["in_progress_model_calls"] == 1
        and metrics["input_states"] == {"DONE": 2}
    )
    (root / "news_decision.json").write_text(
        json.dumps({"enabled": True, "delivery_enabled": True, "cutoff": since})
    )
    (root / "news_activation.json").write_text(
        json.dumps(
            {
                "enabled": True,
                "activation_id": "current",
                "window_end": (now + timedelta(days=1)).isoformat(),
            }
        )
    )
    issues = health(root, tmp_path / "hermes", since=timestamp(since), now=now)[
        "issues"
    ]
    assert "10분 넘게 종료되지 않은 AI 호출 1건" in issues


def test_news_microsecond_neighbor_survives_sqlite_cursor_rounding(tmp_path):
    path = tmp_path / "news.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE news_documents(news_document_id TEXT, source_id TEXT, title TEXT, first_seen_at TEXT, last_seen_at TEXT, revision_no INTEGER)"
        )
        for key, at in [
            ("a", "2026-09-07T04:10:00.123100+00:00"),
            ("b", "2026-09-07T04:10:00.123200+00:00"),
        ]:
            db.execute(
                "INSERT INTO news_documents VALUES(?,?,?,?,?,1)",
                (key, "source", key, at, at),
            )
    page = read_news_observations(
        path,
        since="2026-09-07T13:10:00.123100+09:00",
        until="2026-09-07T14:00:00+09:00",
    )
    assert [r["id"] for r in page["observations"]] == ["news:b:1"]


def test_policy_text_states_kst_basis_unverified_session_and_true_sent_baseline():
    spec = importlib.util.spec_from_file_location(
        "news_decision_models",
        Path(__file__).resolve().parents[1] / "tools/news_decision_models.py",
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert (
        "Asia/Seoul" in module.POLICY
        and "UNVERIFIED" in module.POLICY
        and "last_sent" in module.POLICY
    )
    assert "last_delivery가 SENT인 경우에만" not in module.POLICY
