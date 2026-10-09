import json
import sqlite3
import sys
from datetime import timedelta
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))
from report_news_decisions import report
from report_news_observation import build_report, deliver_once, previous_end, timestamp

from tokmo.records.news_decision_store import NewsDecisionStore

NOW = timestamp("2026-09-07T21:50:00+09:00")
CUTOFF = timestamp("2026-09-06T23:58:36+09:00")


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


@pytest.fixture
def environment(tmp_path):
    root, hermes = tmp_path / "data", tmp_path / "hermes"
    state = root / "stores/news_episodes.sqlite3"
    state.parent.mkdir(parents=True)
    store = NewsDecisionStore(state)
    store.connection.execute(
        "INSERT INTO live_delivery_trips VALUES ('old', 'transport_failed', ?)",
        (CUTOFF.isoformat(),),
    )
    store.connection.commit()
    store.close()
    (root / "ops").mkdir()
    with sqlite3.connect(root / "ops/ops_kernel.sqlite3") as db:
        db.execute(
            "CREATE TABLE alert_outbox (alert_id TEXT, status TEXT, body_evidence_json TEXT, severity TEXT, created_at TEXT, delivered_at TEXT, provider_message_id TEXT)"
        )
    put(
        root / "news_decision.json",
        {"enabled": True, "delivery_enabled": True, "cutoff": CUTOFF.isoformat()},
    )
    put(
        root / "news_activation.json",
        {
            "enabled": True,
            "activation_id": "current",
            "window_end": (NOW + timedelta(days=6)).isoformat(),
        },
    )
    at = (NOW - timedelta(minutes=1)).isoformat()
    put(
        root / "news/decision-worker/latest.json",
        {
            "started_at": at,
            "status": "OK",
            "sources": {"news": {"status": "OK"}},
            "input_states": {},
        },
    )
    for group in "ABC":
        put(
            root / f"news/group_runtime/receipts/news5-primary-v1:{at}:{group}.json",
            {"status": "SUCCEEDED"},
        )
    with sqlite3.connect(root / "opendart-bulk.sqlite3") as db:
        db.execute(
            "CREATE TABLE runs(status TEXT, started_at TEXT, bgn_de TEXT, end_de TEXT)"
        )
        db.execute(
            "INSERT INTO runs VALUES ('complete', ?, '20260907', '20260907')", (at,)
        )
    put(
        hermes / "runtime/tokmo-v3/pulse/20260907T1249/stdout.log",
        {
            "outcome": "SAVED",
            "stages": [{"stage": "news_delivery", "status": "complete"}],
        },
    )
    return root, hermes


def test_read_only_empty_day_is_not_detection_quality_proof(environment):
    root, hermes = environment
    before = {p: p.read_bytes() for p in root.rglob("*.sqlite3")}
    value = build_report(root, hermes, now=NOW)
    assert not value["health"][
        "issues"
    ]  # Old activation's trip must not contaminate this one.
    assert "실제 중요 알림 0건" in value["body"]
    assert "실제 탐지 품질은 아직 평가하지 못했습니다" in value["body"]
    assert "2026-09-07 21:50" in value["body"]
    assert before == {p: p.read_bytes() for p in root.rglob("*.sqlite3")}
    assert not (root / "news/decision-worker/observations").exists()


def test_missing_disabled_expired_or_stale_engine_still_has_feedback(environment):
    root, hermes = environment
    put(root / "news_decision.json", {"enabled": False, "cutoff": CUTOFF.isoformat()})
    put(
        root / "news_activation.json",
        {
            "enabled": False,
            "activation_id": "current",
            "window_end": CUTOFF.isoformat(),
        },
    )
    put(
        root / "news/decision-worker/latest.json",
        {"started_at": CUTOFF.isoformat(), "status": "OK"},
    )
    value = build_report(root, hermes, now=NOW)
    assert "분석 작업 기록 지연" in value["body"]
    assert "뉴스 분석 또는 발송 꺼짐" in value["body"]
    assert "운영 승인 기간 만료" in value["body"]
    (root / "stores/news_episodes.sqlite3").unlink()
    value = build_report(root, hermes, now=NOW)
    assert value["metrics"] is None and "집계 확인 불가" in value["body"]
    assert not (root / "stores/news_episodes.sqlite3").exists()


def test_day_boundaries_actual_tokens_and_inflight_are_separate(environment):
    root, _ = environment
    start = NOW - timedelta(days=1)
    store = NewsDecisionStore(root / "stores/news_episodes.sqlite3")
    for key, at, actual in [
        ("before", start - timedelta(seconds=1), 99),
        ("first", start, 42),
        ("end", NOW, 99),
    ]:
        store.begin_call(key, "screen", {}, at, 1000, None)
        store.finish_call(key, {"_usage": {"total_tokens": actual}})
    store.begin_call("inflight", "screen", {}, NOW - timedelta(minutes=1), 1000, None)
    store.begin_call("failed", "screen", {}, NOW - timedelta(minutes=2), 1000, None)
    store.finish_call("failed", error="timeout")
    store.begin_call("estimated", "screen", {}, NOW - timedelta(minutes=3), 1000, None)
    store.finish_call("estimated", {})
    store.close()
    value = report(root / "stores/news_episodes.sqlite3", since=start, until=NOW)
    assert value["model_calls"] == 4
    assert value["failed_model_calls"] == 1 and value["in_progress_model_calls"] == 1
    assert (
        value["tokens_reported"] == 42 and value["tokens_reserved_or_estimated"] == 3000
    )


def test_split_alert_sent_after_window_start_counts_once_and_simulated_is_excluded(
    environment,
):
    root, _ = environment
    start = NOW - timedelta(hours=1)
    before = start - timedelta(minutes=1)
    evidence = json.dumps(
        ["news_proposal_id:one", "news_observed_at:" + before.isoformat()]
    )
    with sqlite3.connect(root / "ops/ops_kernel.sqlite3") as db:
        for key, at in (("a", before), ("b", start + timedelta(seconds=30))):
            db.execute(
                "INSERT INTO alert_outbox VALUES (?, 'SENT', ?, 'NEWS_IMMEDIATE', ?, ?, 'telegram-group:example:1')",
                ("nda-" + key, evidence, before.isoformat(), at.isoformat()),
            )
        db.execute(
            "INSERT INTO alert_outbox VALUES ('nda-sim', 'SENT', ?, 'NEWS_IMMEDIATE', ?, ?, 'SIMULATED-one')",
            (
                json.dumps(
                    ["news_proposal_id:two", "news_observed_at:" + before.isoformat()]
                ),
                before.isoformat(),
                start.isoformat(),
            ),
        )
    value = report(
        root / "stores/news_episodes.sqlite3",
        root / "ops/ops_kernel.sqlite3",
        since=start,
        until=NOW,
    )
    assert value["sent_alerts"] == 1
    assert value["urgent_collected_to_sent_seconds"] == {
        "samples": 1,
        "p95": 90,
        "max": 90,
    }
    assert value["simulated_delivery_samples"] == 1


def test_current_trip_and_shared_runner_failure_are_visible(environment):
    root, hermes = environment
    with sqlite3.connect(root / "stores/news_episodes.sqlite3") as db:
        db.execute(
            "INSERT INTO live_delivery_trips VALUES ('current', 'transport_failed', ?)",
            (NOW.isoformat(),),
        )
    put(
        hermes / "runtime/tokmo-v3/pulse/20260907T1245/stdout.log",
        {"outcome": "FAILED", "reason": "cadence_not_complete", "stages": []},
    )
    value = build_report(root, hermes, now=NOW)
    assert "현재 뉴스 발송이 오류로 중지됨" in value["body"]
    assert "공용 실행기 일부 회차 실패 1회" in value["body"]
    assert value["health"]["details"]["발송 실행"]["cadence_not_complete"] == 1


def test_missing_news_source_is_not_hidden_by_fresh_worker(environment):
    root, hermes = environment
    for path in (root / "news/group_runtime/receipts").glob("*:B.json"):
        path.unlink()
    value = build_report(root, hermes, now=NOW)
    assert "JTBC 속보 수집 기록 없음" in value["body"]


def test_daily_resume_covers_gap_but_final_is_cumulative(environment):
    root, hermes = environment
    folder = root / "news/decision-worker/observations"
    end = NOW - timedelta(hours=3)
    put(
        folder / "daily-20260906.receipt.json",
        {"status": "SENT", "until": end.isoformat()},
    )
    put(
        folder / "daily-20260907.receipt.json",
        {"status": "UNKNOWN", "until": (end + timedelta(hours=1)).isoformat()},
    )
    assert previous_end(folder, CUTOFF, NOW) == end
    assert build_report(root, hermes, now=NOW)["since"] == end.isoformat()
    final = build_report(root, hermes, now=NOW, final=True)
    assert final["since"] == CUTOFF.isoformat()
    assert "정기 안정화 보고는 이번으로 종료" in final["body"]


@pytest.mark.parametrize("failure", [False, True])
def test_once_per_day_even_after_ambiguous_failure(environment, failure):
    root, hermes = environment
    value = build_report(root, hermes, now=NOW)
    calls = []

    def sender(body):
        calls.append(body)
        if failure:
            raise TimeoutError("do not expose credentials from exception text")
        return "telegram-group:test:99"

    folder = root / "news/decision-worker/observations"
    first = deliver_once(value, folder, sender)
    second = deliver_once(value, folder, sender)
    assert len(calls) == 1 and second["already_attempted"]
    assert first["status"] == ("UNKNOWN" if failure else "SENT")
    assert "credentials" not in json.dumps(first)
    assert json.loads((folder / first["report"]).read_text())["body"] == value["body"]
