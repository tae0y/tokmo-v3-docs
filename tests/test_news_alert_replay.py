"""Replay checks time isolation and uses the actual engine for example scenarios."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "replay_news_alerts", ROOT / "tools/replay_news_alerts.py"
)
replay_module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(replay_module)
FIXTURES = ROOT / "tests/fixtures/news_alert_cases"


def test_no_future_observations_or_context_and_until(tmp_path):
    seen = []

    class Store:
        def __init__(self, path):
            self.closed = False

        def close(self):
            self.closed = True

    class Engine:
        def __init__(self, store, screen, analyze, *, context_reader, clock):
            assert clock == "recorded"
            self.context_reader = context_reader

        def tick(self, observations, now):
            seen.append((now.isoformat(), observations, self.context_reader(now)))
            return {"alerts": [], "failures": [], "calls": 0}

    case = {
        "version": 1,
        "id": "chronology",
        "observations": [
            {"id": "future", "observed_at": "2026-09-01T12:00:00+09:00"},
            {"id": "first", "observed_at": "2026-09-01T10:00:00+09:00"},
        ],
        "checkpoints": ["2026-09-01T11:00:00+09:00"],
        "context": [
            {"observed_at": "2026-09-01T12:00:00+09:00", "context": {"cash": 100}},
            {"observed_at": "2026-09-01T10:00:00+09:00", "context": {"holding": "ABC"}},
        ],
        "expectations": [{"at": "2026-09-01T12:00:00+09:00", "alert_count": 1}],
    }
    report = replay_module.replay(
        case,
        tmp_path / "state.db",
        until="2026-09-01T11:00:00+09:00",
        engine_factory=Engine,
        store_factory=Store,
    )
    assert [[row["id"] for row in batch] for _, batch, _ in seen] == [["first"], []]
    assert all(context == {"holding": "ABC"} for _, _, context in seen)
    assert report["skipped_expectations"] == 1
    assert report["ai_quality_validated"] is False


def test_recorded_response_exhaustion_does_not_retry():
    model = replay_module.RecordedModel([{"candidates": []}], "screen")
    response = model({})
    assert response["candidates"] == []
    assert response["_model"] == "recorded" and response["_usage"]["total_tokens"] == 0
    with pytest.raises(ValueError, match="exhausted"):
        model({})
    assert model.used == 1


def test_production_storage_and_symlink_are_refused(tmp_path):
    production = tmp_path / ".local/share/tokmo-v3"
    production.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(production, target_is_directory=True)
    for path in (production / "replay.db", alias / "replay.db"):
        with pytest.raises(ValueError, match="production"):
            replay_module.safe_output(path)


@pytest.mark.parametrize("name", ["synthetic-war-correction", "synthetic-overnight"])
def test_synthetic_cases_use_real_engine(tmp_path, name):
    case = json.loads((FIXTURES / f"{name}.json").read_text())
    report = replay_module.replay(case, tmp_path / "replay.db")
    assert report["passed"], json.dumps(report, ensure_ascii=False, indent=2)
    assert report["telegram_sending"] == "disabled"
    assert report["mode"] == "recorded"


def test_cli_keeps_report_but_default_removes_state(tmp_path, capsys):
    result = replay_module.main(
        [
            "--case",
            str(FIXTURES / "synthetic-war-correction.json"),
            "--output",
            str(tmp_path / "reports"),
        ]
    )
    assert result == 0
    summary = json.loads(capsys.readouterr().out)
    report_path = Path(summary["report"])
    report = json.loads(report_path.read_text())
    assert report["state_path"] is None
    assert not list(report_path.parent.glob("*.sqlite3"))
    assert report_path.with_suffix(".md").is_file()


def test_keep_state_creates_fresh_db_for_each_run(tmp_path, capsys):
    args = [
        "--case",
        str(FIXTURES / "synthetic-overnight.json"),
        "--output",
        str(tmp_path / "reports"),
        "--keep-state",
    ]
    paths = []
    for _ in range(2):
        assert replay_module.main(args) == 0
        summary = json.loads(capsys.readouterr().out)
        report = json.loads(Path(summary["report"]).read_text())
        paths.append(Path(report["state_path"]))
    assert paths[0] != paths[1]
    assert all(path.is_file() for path in paths)


def test_restart_keeps_source_times_and_drains_at_same_judgment_time(tmp_path):
    seen = []

    class Store:
        def __init__(self, path):
            self.remaining = 2

        def close(self):
            pass

        def pending(self):
            return self.remaining > 0

    class Engine:
        def __init__(self, store, *args, context_reader, clock):
            self.store = store

        def tick(self, observations, now):
            seen.append((now.isoformat(), observations))
            self.store.remaining -= 1
            return {"alerts": [], "failures": [], "calls": 0}

    original = {"id": "old", "observed_at": "2026-09-01T09:00:00+09:00"}
    case = dict(
        version=1,
        id="backlog",
        start_at="2026-09-01T13:00:00+09:00",
        observations=[original],
        drain_backlog=True,
        max_drain_passes=3,
    )
    assert replay_module.replay(
        case, tmp_path / "state.db", engine_factory=Engine, store_factory=Store
    )["passed"]
    assert seen == [
        ("2026-09-01T13:00:00+09:00", [original]),
        ("2026-09-01T13:00:00+09:00", []),
    ]


def test_simulated_receipts_advance_actual_engine_comparison_history(tmp_path):
    from tokmo.records.news_decision_store import NewsDecisionStore

    case = json.loads((FIXTURES / "synthetic-war-correction.json").read_text())
    case["simulate_delivery"] = True
    state = tmp_path / "replay.db"
    report = replay_module.replay(case, state)
    assert report["passed"], report
    store = NewsDecisionStore(state)
    try:
        sent = [e for e in store.events() if e.get("last_sent")]
        assert sent and any(len(e["delivered_history"]) >= 2 for e in sent)
        assert report["telegram_sending"] == "disabled"
    finally:
        store.close()
