"""NEWS group runner behavior tests."""

from datetime import datetime, timezone
import fcntl
import json
import os

import pytest

from tokmo.programs.news_group_runner import (
    GroupSourceResult, build_connector_argv, classify_failure, run_news_group,
    sanitize_http_status,
)
from tokmo.programs.news_poll_manifest import NewsPollInvocation, NewsPollManifest
from tokmo.programs.news_poll_policy import NewsGroup


def manifest():
    return NewsPollManifest(
        "news16-v1", True, "naver query", 10, ("NVDA",),
    )


def test_group_runner_continues_after_source_failure_and_rejects_duplicate_tick(tmp_path):
    calls = []

    def execute(invocation, campaign_id):
        calls.append((invocation.source, campaign_id))
        status = "TRANSPORT_FAILURE" if invocation.source == "yna-market" else "COLLECTED"
        return GroupSourceResult(invocation.source, status, 0 if status != "COLLECTED" else 1, 0, 0)

    scheduled = datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc)
    first = run_news_group(
        NewsGroup.A, scheduled, manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute)
    second = run_news_group(
        NewsGroup.A, scheduled, manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute)

    assert [name for name, _campaign in calls] == ["bbc", "yna-market", "naver"]
    assert len({campaign for _name, campaign in calls}) == 3
    assert first.status == "PARTIAL_FAILURE"
    assert second.status == "DUPLICATE_TICK"
    receipt = tmp_path / "receipts" / f"{first.tick_id}.json"
    assert oct(os.stat(receipt).st_mode & 0o777) == "0o600"
    saved = json.loads(receipt.read_text())
    assert [row["status"] for row in saved["sources"]] == [
        "COLLECTED", "TRANSPORT_FAILURE", "COLLECTED"]


def test_group_runner_lock_busy_has_zero_executor_and_zero_tick_marker(tmp_path):
    lock_path = tmp_path / "tokmo-news-collection.lock"
    lock_path.touch(mode=0o600)
    with lock_path.open("r+") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        calls = []
        result = run_news_group(
            NewsGroup.A,
            datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
            manifest(), clock=lambda: 0.0, runtime_dir=tmp_path,
            executor=lambda *args: calls.append(args),
        )

    assert result.status == "SKIPPED_OVERLAP"
    assert calls == []
    assert not (tmp_path / "ticks").exists()


def test_connector_argv_is_bounded_and_passes_credentials_only_to_credential_source(tmp_path):
    common = dict(
        runner=tmp_path / "runner.py", db=tmp_path / "news.sqlite3",
        ledger=tmp_path / "attempts.sqlite3", credentials=tmp_path / "news.env",
        campaign_id="tick/source/fingerprint",
    )
    argv = build_connector_argv(
        NewsPollInvocation("finnhub-company", symbol="NVDA",
                           from_date="2026-08-01", to_date="2026-08-02"),
        **common,
    )
    assert argv[1:5] == [str(common["runner"]), "--source", "finnhub-company", "--db"]
    assert argv[argv.index("--ceiling") + 1] == "1"
    assert argv[argv.index("--credentials") + 1] == str(common["credentials"])
    assert argv[argv.index("--symbol") + 1] == "NVDA"
    public = build_connector_argv(NewsPollInvocation("bbc"), **common)
    assert "--credentials" not in public


def test_primary_collection_continues_during_full_sweep_but_cannot_overlap_itself(tmp_path):
    calls = []
    def execute(invocation, campaign_id):
        calls.append(invocation.source)
        return GroupSourceResult(invocation.source, 'COLLECTED', 1, 0, 0)
    scheduled = datetime(2026, 9, 7, 0, 5, tzinfo=timezone.utc)
    with (tmp_path / 'tokmo-news-collection.lock').open('a') as full:
        fcntl.flock(full, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = run_news_group(NewsGroup.A, scheduled, manifest(), clock=lambda: 0.0, runtime_dir=tmp_path,
                                executor=execute, fast_primary=True)
        assert result.status == 'SUCCEEDED'
        assert calls == ['yna-market']
        with (tmp_path / 'tokmo-news-primary.lock').open('a') as primary:
            fcntl.flock(primary, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = run_news_group(NewsGroup.B, scheduled.replace(minute=6), manifest(),
                                    clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute, fast_primary=True)
            assert result.status == 'SKIPPED_OVERLAP'
            assert calls == ['yna-market']


def test_collected_with_conflict_is_flagged_but_not_a_collection_failure(tmp_path):
    """A conflict is a document-level anomaly, not a failed fetch.

    It keeps its own status so the receipt still carries the signal, but it no
    longer reports the tick as broken when every source collected cleanly.
    """
    def execute(invocation, campaign_id):
        conflict = 1 if invocation.source == "yna-market" else 0
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, conflict)

    result = run_news_group(
        NewsGroup.A, datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute,
    )

    assert result.status == "SUCCEEDED_WITH_CONFLICTS"
    saved = json.loads(
        (tmp_path / "receipts" / f"{result.tick_id}.json").read_text())
    assert [row["conflict"] for row in saved["sources"]] == [0, 1, 0]


def test_clean_collection_without_conflicts_stays_plain_success(tmp_path):
    def execute(invocation, campaign_id):
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0)

    result = run_news_group(
        NewsGroup.A, datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute,
    )

    assert result.status == "SUCCEEDED"


def test_conflict_alongside_a_transport_failure_still_reports_partial(tmp_path):
    def execute(invocation, campaign_id):
        if invocation.source == "naver":
            return GroupSourceResult("naver", "TRANSPORT_FAILURE", 0, 0, 0)
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 1)

    result = run_news_group(
        NewsGroup.A, datetime(2026, 8, 2, 12, 0, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute,
    )

    assert result.status == "PARTIAL_FAILURE"


def test_group_receipt_persists_sanitized_transport_diagnostics(tmp_path):
    def execute(invocation, campaign_id):
        if invocation.source == "naver":
            return GroupSourceResult(
                "naver", "CONFIG_UNAVAILABLE", 0, 0, 0, 0, 403, "HTTP_403")
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0, 0, 200)

    result = run_news_group(
        NewsGroup.A, datetime(2026, 8, 4, 12, 0, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute)

    assert result.status == "PARTIAL_FAILURE"
    receipt = tmp_path / "receipts" / f"{result.tick_id}.json"
    saved = json.loads(receipt.read_text())
    rows = {row["source"]: row for row in saved["sources"]}
    assert rows["naver"]["http_status"] == 403
    assert rows["naver"]["failure_class"] == "HTTP_403"
    assert rows["bbc"]["http_status"] == 200
    assert rows["bbc"]["failure_class"] is None
    text = receipt.read_text()
    for banned in ("token", "api_key", "Authorization", "https://", "query"):
        assert banned not in text


def test_failure_class_is_bounded_and_status_sanitizer_rejects_drift():
    assert classify_failure("COLLECTED", 200) is None
    assert classify_failure("EMPTY", 200) is None
    assert classify_failure("CONFIG_UNAVAILABLE", 403) == "HTTP_403"
    assert classify_failure("RATE_LIMITED", 429) == "HTTP_429"
    assert classify_failure("TRANSPORT_FAILURE", None) == "NO_HTTP_STATUS"
    assert sanitize_http_status(None) is None
    assert sanitize_http_status(403) == 403
    for invalid in ("403", True, 42, 600, 403.0):
        with pytest.raises(ValueError):
            sanitize_http_status(invalid)


def test_group_b_paces_consecutive_finnhub_calls_and_never_sleeps_public_sources(tmp_path):
    events = []
    four_symbols = NewsPollManifest(
        "news16-v1", True, "naver query", 10, ("NVDA", "MSFT", "AAPL", "TSLA"))

    def execute(invocation, campaign_id):
        events.append(("exec", invocation.source))
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0)

    result = run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 5, tzinfo=timezone.utc),
        four_symbols, runtime_dir=tmp_path, executor=execute,
        clock=lambda: 0.0, sleeper=lambda gap: events.append(("sleep", gap)),
    )

    assert result.status == "SUCCEEDED"
    assert events == [
        ("exec", "finnhub-company"),
        ("sleep", 2.0), ("exec", "finnhub-company"),
        ("sleep", 2.0), ("exec", "finnhub-company"),
        ("sleep", 2.0), ("exec", "finnhub-company"),
        ("exec", "jtbc-breaking"),
        ("exec", "investing-analyst"),
    ]


def test_finnhub_gap_is_durable_across_group_c_then_group_b_runs(tmp_path):
    now = {"t": 100.0}
    naps = []

    def execute(invocation, campaign_id):
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0)

    run_news_group(
        NewsGroup.C, datetime(2026, 8, 31, 12, 10, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    assert naps == []

    now["t"] = 100.5
    run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 20, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    assert naps == [pytest.approx(1.5)]


def test_pacing_skips_when_interval_already_elapsed_and_bounds_bad_markers(tmp_path):
    now = {"t": 100.0}
    naps = []

    def execute(invocation, campaign_id):
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0)

    run_news_group(
        NewsGroup.C, datetime(2026, 8, 31, 12, 10, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    now["t"] = 200.0
    run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 20, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    assert naps == []

    marker = tmp_path / "tokmo-news-finnhub-pace.json"
    marker.write_text(json.dumps({"monotonic": 1e12}))
    run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 35, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    assert naps == [2.0]

    marker.write_text("not-json")
    run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 50, tzinfo=timezone.utc),
        manifest(), runtime_dir=tmp_path, executor=execute,
        clock=lambda: now["t"], sleeper=naps.append,
    )
    assert naps == [2.0]


def test_receipt_rows_carry_optional_target_for_multi_target_sources(tmp_path):
    """R-TGT-1: per-target diagnostics live only in the receipt's target key."""
    def execute(invocation, campaign_id):
        return GroupSourceResult(invocation.source, "COLLECTED", 1, 0, 0,
                                 target=invocation.symbol)

    result = run_news_group(
        NewsGroup.B, datetime(2026, 8, 31, 12, 5, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute)

    assert result.status == "SUCCEEDED"
    saved = json.loads(
        (tmp_path / "receipts" / f"{result.tick_id}.json").read_text())
    targets = {row["source"]: row["target"] for row in saved["sources"]}
    assert targets == {"finnhub-company": "NVDA", "jtbc-breaking": None,
                       "investing-analyst": None}


def test_revision_metric_is_trailing_and_group_remains_successful(tmp_path):
    def execute(invocation, campaign_id):
        return GroupSourceResult(invocation.source, "COLLECTED", 0, 0, 0, 1)

    result = run_news_group(
        NewsGroup.A, datetime(2026, 8, 3, 12, 0, tzinfo=timezone.utc),
        manifest(), clock=lambda: 0.0, runtime_dir=tmp_path, executor=execute)
    assert result.status == "SUCCEEDED"
    payload = json.loads(
        (tmp_path / "receipts" / f"{result.tick_id}.json").read_text())
    assert all(row["revision"] == 1 for row in payload["sources"])
