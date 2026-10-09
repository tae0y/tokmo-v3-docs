"""Scheduler-facing NEWS group CLI wiring tests."""

import importlib.util
import json
import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.news_group_runner import GroupRunResult
from tokmo.programs.news_poll_manifest import NewsPollInvocation
from tokmo.programs.news_poll_policy import NewsGroup, news_tick_id
from tokmo.programs.news_source_health import (
    apply_source_health,
    deliver_pending,
    observe_tick,
)
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.source_health_store import SourceHealthStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)

SCRIPT = Path(__file__).parents[1] / "tools" / "run_news_group.py"
SPEC = importlib.util.spec_from_file_location("run_news_group_tool", SCRIPT)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

SCHEDULED = datetime(2026, 8, 31, 12, 5, tzinfo=UTC)


def failing_sources():
    return [
        {
            "source": "finnhub-company",
            "status": "TRANSPORT_FAILURE",
            "inserted": 0,
            "duplicate": 0,
            "conflict": 0,
            "revision": 0,
            "http_status": 503,
            "failure_class": "HTTP_503",
            "target": "NVDA",
        },
        {
            "source": "jtbc-breaking",
            "status": "COLLECTED",
            "inserted": 1,
            "duplicate": 0,
            "conflict": 0,
            "revision": 0,
            "http_status": 200,
            "failure_class": None,
            "target": None,
        },
    ]


def run_main(tmp_path, monkeypatch, status, sources, gate=None, extra=()):
    class Clock:
        fromisoformat = staticmethod(datetime.fromisoformat)

        @staticmethod
        def now(tz):
            return SCHEDULED + timedelta(minutes=2)

    monkeypatch.setattr(MODULE, "datetime", Clock)
    runtime = tmp_path / "runtime"
    (runtime / "receipts").mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "news16-v1",
                "naver": {"enabled": False, "query": "q", "display": 10},
                "finnhub-company": {"symbols": ["NVDA"]},
            }
        )
    )
    os.chmod(manifest, 0o600)
    tick = news_tick_id(NewsGroup.B, SCHEDULED)
    (runtime / "receipts" / f"{tick}.json").write_text(
        json.dumps({"tick_id": tick, "status": status, "sources": sources})
    )
    if gate is not None:
        gate_path = runtime / "source_health_activation.json"
        gate_path.write_text(json.dumps(gate))
        os.chmod(gate_path, 0o600)
    monkeypatch.setattr(
        MODULE, "run_news_group", lambda *args, **kwargs: GroupRunResult(tick, status)
    )
    argv = [
        "--group",
        "B",
        "--scheduled-for",
        SCHEDULED.isoformat(),
        "--manifest",
        str(manifest),
        "--runtime",
        str(runtime),
        "--db",
        str(tmp_path / "db"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--credentials",
        str(tmp_path / "creds.env"),
        *extra,
    ]
    return MODULE.main(argv), tick, tmp_path / "source_health.sqlite3"


def seed_delivered_incident(health_db):
    """Two failing pre-ticks + a fake delivery => confirmed, delivered."""
    store = SourceHealthStore(health_db)
    for n, when in enumerate(
        (
            datetime(2026, 8, 31, 11, 35, tzinfo=UTC),
            datetime(2026, 8, 31, 11, 50, tzinfo=UTC),
        )
    ):
        payload = {
            "tick_id": f"pre-{n}",
            "status": "PARTIAL_FAILURE",
            "sources": failing_sources(),
        }
        apply_source_health(store, observe_tick(payload, now=when), now=when)
    deliver_pending(
        store,
        telegram_send=lambda body: "7",
        now=datetime(2026, 8, 31, 11, 51, tzinfo=UTC),
    )
    return store


def test_child_executor_decodes_bounded_runner_result(tmp_path):
    payload = {
        "status": "COLLECTED",
        "source": "bbc",
        "inserted": 1,
        "duplicate": 2,
        "conflict": 0,
        "revision": 3,
        "http_status": 200,
    }
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return subprocess.CompletedProcess(argv, 0, json.dumps(payload), "")

    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=run,
    )

    assert result.source == "bbc"
    assert (result.status, result.inserted, result.duplicate, result.conflict) == (
        "COLLECTED",
        1,
        2,
        0,
    )
    assert result.revision == 3
    assert result.http_status == 200
    assert result.failure_class is None
    assert calls[0][1]["timeout"] == 240
    assert calls[0][1]["capture_output"] is True
    from dataclasses import asdict

    from tokmo.values.news_collection_diagnostics import collection_complete

    assert collection_complete(asdict(result))  # successful legacy child JSON


def test_child_executor_maps_malformed_output_to_runner_failure(tmp_path):
    def run(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 3, "not-json", "secret-like stderr")

    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=run,
    )

    assert result.status == "RUNNER_FAILURE"
    assert (result.inserted, result.duplicate, result.conflict) == (0, 0, 0)
    assert result.http_status is None
    assert result.failure_class == "CHILD_EXIT"
    assert result.revision == 0


@pytest.mark.parametrize("revision", [True, 1.0, -1, None])
def test_parent_requires_exact_nonnegative_integer_revision(tmp_path, revision):
    payload = {
        "status": "COLLECTED",
        "source": "bbc",
        "inserted": 0,
        "duplicate": 0,
        "conflict": 0,
        "revision": revision,
        "http_status": 200,
    }
    child = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, json.dumps(payload), ""
    )
    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=child,
    )
    assert result.status == "RUNNER_FAILURE"
    assert result.revision == 0


def test_child_executor_preserves_403_status_and_class_without_retry(tmp_path):
    payload = {
        "status": "CONFIG_UNAVAILABLE",
        "source": "finnhub-company",
        "inserted": 0,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
        "http_status": 403,
    }
    calls = []

    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 3, json.dumps(payload), "")

    result = MODULE.execute_invocation(
        NewsPollInvocation(
            "finnhub-company",
            symbol="NVDA",
            from_date="2026-08-01",
            to_date="2026-08-02",
        ),
        "tick/finnhub-company/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=run,
    )

    assert result.status == "CONFIG_UNAVAILABLE"
    assert result.http_status == 403
    assert result.failure_class == "HTTP_403"
    assert len(calls) == 1


def test_child_executor_stamps_invocation_symbol_as_target(tmp_path):
    payload = {
        "status": "COLLECTED",
        "source": "finnhub-company",
        "inserted": 1,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
        "http_status": 200,
    }
    child = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, json.dumps(payload), ""
    )
    result = MODULE.execute_invocation(
        NewsPollInvocation(
            "finnhub-company",
            symbol="NVDA",
            from_date="2026-08-01",
            to_date="2026-08-02",
        ),
        "tick/finnhub-company/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=child,
    )
    assert result.target == "NVDA"
    failed = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=lambda argv, **kwargs: subprocess.CompletedProcess(argv, 3, "x", ""),
    )
    assert failed.status == "RUNNER_FAILURE"
    assert failed.target is None


def test_child_executor_marks_typed_failure_without_http_status(tmp_path):
    payload = {
        "status": "TRANSPORT_FAILURE",
        "source": "bbc",
        "inserted": 0,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
        "http_status": None,
    }
    child = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 3, json.dumps(payload), ""
    )
    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=child,
    )
    assert result.http_status is None
    assert result.failure_class == "NO_HTTP_STATUS"


@pytest.mark.parametrize("http_status", ["403", True, 42, 600, 403.0])
def test_parent_rejects_unsanitized_http_status_as_runner_failure(
    tmp_path, http_status
):
    payload = {
        "status": "CONFIG_UNAVAILABLE",
        "source": "bbc",
        "inserted": 0,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
        "http_status": http_status,
    }
    child = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 3, json.dumps(payload), ""
    )
    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=child,
    )
    assert result.status == "RUNNER_FAILURE"
    assert result.http_status is None
    assert result.failure_class == "CHILD_INVALID_OUTPUT"


def test_parent_requires_http_status_key_in_runner_output(tmp_path):
    payload = {
        "status": "COLLECTED",
        "source": "bbc",
        "inserted": 1,
        "duplicate": 0,
        "conflict": 0,
        "revision": 0,
    }
    child = lambda argv, **kwargs: subprocess.CompletedProcess(
        argv, 0, json.dumps(payload), ""
    )
    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "tick/bbc/fp",
        runner=tmp_path / "one.py",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "env",
        run=child,
    )
    assert result.status == "RUNNER_FAILURE"


def test_invalid_child_encoding_is_a_bounded_runner_failure(tmp_path):
    def run(*args, **kwargs):
        raise UnicodeDecodeError("utf-8", b"token=SECRET\xff", 12, 13, "invalid")

    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        "fixture",
        runner=tmp_path / "runner",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "credentials",
        run=run,
    )
    assert result.status == "RUNNER_FAILURE"
    assert result.failure_class == "CHILD_INVALID_OUTPUT"
    assert result.diagnostics["stage"] == "CHILD"
    assert "SECRET" not in repr(result)


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("timeout", "CHILD_TIMEOUT"),
        ("empty", "CHILD_NO_OUTPUT"),
        ("malformed", "CHILD_INVALID_OUTPUT"),
        ("exit", "CHILD_EXIT"),
        ("hostile", "CHILD_INVALID_OUTPUT"),
        ("partial", "INCOMPLETE_STORAGE"),
    ],
)
def test_child_failure_keeps_safe_phase_progress_and_actual_elapsed(
    tmp_path, mode, expected
):
    import hashlib
    from dataclasses import asdict

    campaign = "fixture-campaign"
    trace = hashlib.sha256(campaign.encode()).hexdigest()[:16]
    diag = dict(
        stage="PERSIST",
        cause=None,
        received=7,
        persisted=3,
        elapsed_ms=239000,
        http_status=200,
        trace=trace,
    )
    stderr = (
        "token=SECRET https://host/?query=PRIVATE\nNEWS_PROGRESS "
        + json.dumps(diag)
        + "\n"
    )
    payload = dict(
        status="COLLECTED",
        source="bbc",
        inserted=3,
        duplicate=0,
        conflict=0,
        revision=0,
        http_status=200,
        diagnostics=diag,
    )

    def run(argv, **kwargs):
        assert kwargs["timeout"] == 240
        if mode == "timeout":
            raise subprocess.TimeoutExpired(argv, 240, stderr=stderr.encode())
        if mode == "hostile":
            payload["status"] = "SECRET https://host/?query=PRIVATE"
        stdout = {"empty": "", "malformed": "[]"}.get(mode, json.dumps(payload))
        return subprocess.CompletedProcess(
            argv, 9 if mode == "exit" else 0, stdout, stderr
        )

    times = iter([10.0, 251.25])
    result = MODULE.execute_invocation(
        NewsPollInvocation("bbc"),
        campaign,
        runner=tmp_path / "runner",
        db=tmp_path / "db",
        ledger=tmp_path / "ledger",
        credentials=tmp_path / "credentials",
        run=run,
        clock=lambda: next(times),
    )
    assert result.status == "RUNNER_FAILURE"
    assert result.failure_class == expected
    assert result.diagnostics["stage"] == "PERSIST"
    assert result.diagnostics["received"] == 7
    assert result.diagnostics["persisted"] == 3
    assert result.diagnostics["elapsed_ms"] == 241250
    assert result.diagnostics["trace"] == trace
    assert result.diagnostics["exit_code"] == (
        None if mode == "timeout" else 9 if mode == "exit" else 0
    )
    assert "SECRET" not in json.dumps(asdict(result))


def test_gate_off_keeps_exit_codes_and_creates_no_health_state(
    tmp_path, monkeypatch, capsys
):
    """R-EXC-1 (OFF): no gate file -> byte-identical behavior, zero residue."""
    code, tick, health_db = run_main(
        tmp_path, monkeypatch, "PARTIAL_FAILURE", failing_sources()
    )
    assert code == 3
    assert f"TOKMO_NEWS_GROUP_FAILED: status=PARTIAL_FAILURE tick={tick}" in (
        capsys.readouterr().out
    )
    assert not health_db.exists()
    code, _tick, health_db = run_main(tmp_path, monkeypatch, "SUCCEEDED", [])
    assert code == 0
    assert not health_db.exists()


@pytest.mark.parametrize("gate", ["RECORD", "DELIVER"])
def test_record_and_undelivered_deliver_keep_raw_exit(
    tmp_path, monkeypatch, capsys, gate
):
    """RECORD preserves the raw exit; DELIVER gives a first failure its grace."""
    code, tick, health_db = run_main(
        tmp_path, monkeypatch, "PARTIAL_FAILURE", failing_sources(), gate=gate
    )
    assert code == (0 if gate == "DELIVER" else 3)
    captured = capsys.readouterr()
    assert f"status=PARTIAL_FAILURE tick={tick}" in captured.out + captured.err
    assert health_db.exists()
    recorded = SourceHealthStore(health_db).read_health(
        "finnhub-company", now=SCHEDULED.isoformat(timespec="microseconds")
    )
    assert recorded["consecutive_failures"] == 1


def test_deliver_gate_suppresses_exit_for_delivered_incident(
    tmp_path, monkeypatch, capsys
):
    """R-EXC-2: delivered DEGRADED incident -> exit 0, receipt stays truthful."""
    health_db = tmp_path / "source_health.sqlite3"
    seed_delivered_incident(health_db)
    code, tick, _path = run_main(
        tmp_path, monkeypatch, "PARTIAL_FAILURE", failing_sources(), gate="DELIVER"
    )
    captured = capsys.readouterr()
    assert code == 0
    assert "TOKMO_NEWS_GROUP_FAILED" not in captured.out
    assert f"news_group status=PARTIAL_FAILURE tick={tick} health=suppressed" in (
        captured.err
    )


def test_corrupt_health_db_fails_open_to_raw_exit(tmp_path, monkeypatch, capsys):
    """R-EXC-3: health exception -> raw mapping + one stderr marker line."""
    corrupt = tmp_path / "source_health.sqlite3"
    corrupt.write_bytes(b"not-a-database")
    os.chmod(corrupt, 0o600)
    code, _tick, _path = run_main(
        tmp_path, monkeypatch, "PARTIAL_FAILURE", failing_sources(), gate="DELIVER"
    )
    captured = capsys.readouterr()
    assert code == 3
    # Only the exception class name escapes (sanitized fail-open marker).
    assert "TOKMO_NEWS_HEALTH_DEGRADED: " in captured.err
    assert "TOKMO_NEWS_GROUP_FAILED" in captured.out


def test_skipped_overlap_stays_exit_3_even_under_deliver(tmp_path, monkeypatch, capsys):
    """SDS §3.8: SKIPPED_OVERLAP wrote no receipt and is never suppressed."""
    seed_delivered_incident(tmp_path / "source_health.sqlite3")
    code, _tick, _path = run_main(
        tmp_path, monkeypatch, "SKIPPED_OVERLAP", [], gate="DELIVER"
    )
    assert code == 3
    assert "TOKMO_NEWS_GROUP_FAILED" in capsys.readouterr().out


def test_invalid_or_loose_gate_file_reads_as_off(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    gate_path = runtime / "source_health_activation.json"
    gate_path.write_text("not-json")
    os.chmod(gate_path, 0o600)
    assert MODULE._health_mode(gate_path) == "OFF"
    gate_path.write_text(json.dumps("DELIVER"))
    os.chmod(gate_path, 0o644)
    assert MODULE._health_mode(gate_path) == "OFF"
    os.chmod(gate_path, 0o600)
    assert MODULE._health_mode(gate_path) == "DELIVER"
    assert MODULE._health_mode(runtime / "absent.json") == "OFF"


PILOT_SYMBOLS = ("GOOGL", "NVDA", "MSFT", "TSLA")
_ADDED_AT = "2026-08-01T00:00:00+00:00"


def _watch_entry(symbol, name):
    return {
        "symbol": symbol,
        "name": name,
        "status": "active",
        "added_at": _ADDED_AT,
        "source": "user",
    }


def _write_watchlists(
    root,
    *,
    kr=(("005930", "삼성전자"),),
    us=(("NVDA", "NVIDIA Corporation"), ("V", "Visa Inc.")),
):
    root.mkdir(parents=True, exist_ok=True)
    for market, rows in (("KR", kr), ("US", us)):
        (root / f"{market}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "market": market,
                    "entries": [_watch_entry(symbol, name) for symbol, name in rows],
                },
                ensure_ascii=False,
            )
        )


def _write_holdings(path, items):
    scope = "toss-acct-" + "c" * 24
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope,
                1,
                tuple(
                    HoldingItemV1(symbol, "1", "10", "USD", market_raw, "market")
                    for symbol, market_raw in items
                ),
            ),
        ),
        True,
        SCHEDULED,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    CurrentHoldingsStore(path, clock=lambda: SCHEDULED).replace(
        state, identity_fingerprints={scope: "0" * 64}
    )


def run_universe_main(
    tmp_path,
    monkeypatch,
    *,
    holdings=None,
    naver_enabled=False,
    kr_watch=(("005930", "삼성전자"),),
    universe_flags=True,
):
    runtime = tmp_path / "runtime"
    (runtime / "receipts").mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "version": "news16-v1",
                "naver": {
                    "enabled": naver_enabled,
                    "query": "SK하이닉스 OR 현대차 OR 아모레퍼시픽",
                    "display": 10,
                },
                "finnhub-company": {"symbols": list(PILOT_SYMBOLS)},
            },
            ensure_ascii=False,
        )
    )
    os.chmod(manifest, 0o600)
    manifest_bytes = manifest.read_bytes()
    tick = news_tick_id(NewsGroup.B, SCHEDULED)
    captured = []

    def record(group, scheduled_for, manifest_value, **kwargs):
        captured.append(manifest_value)
        return GroupRunResult(tick, "SUCCEEDED")

    monkeypatch.setattr(MODULE, "run_news_group", record)
    watch_dir = tmp_path / "watchlists"
    _write_watchlists(watch_dir, kr=kr_watch)
    holdings_path = tmp_path / "stores" / "current_holdings.json"
    if holdings is not None:
        holdings_path.parent.mkdir(parents=True, exist_ok=True)
        _write_holdings(holdings_path, holdings)
    universe_store = runtime / "news_universe.json"
    argv = [
        "--group",
        "B",
        "--scheduled-for",
        SCHEDULED.isoformat(),
        "--manifest",
        str(manifest),
        "--runtime",
        str(runtime),
        "--db",
        str(tmp_path / "db"),
        "--ledger",
        str(tmp_path / "ledger.sqlite3"),
        "--credentials",
        str(tmp_path / "creds.env"),
    ]
    if universe_flags:
        argv += [
            "--universe",
            "--holdings-store",
            str(holdings_path),
            "--watchlists-dir",
            str(watch_dir),
            "--universe-store",
            str(universe_store),
        ]
    code = MODULE.main(argv)
    assert manifest.read_bytes() == manifest_bytes, "manifest file mutated"
    return code, captured, universe_store


def test_universe_flag_absent_is_byte_identical_inert(tmp_path, monkeypatch):
    """R-WIR-1: without --universe the pilot manifest flows through untouched
    and no universe artifact is created anywhere."""
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=[("GOOGL", "US")], universe_flags=False
    )
    assert code == 0
    assert captured[0].finnhub_symbols == PILOT_SYMBOLS
    assert captured[0].naver_query == "SK하이닉스 OR 현대차 OR 아모레퍼시픽"
    assert not universe_store.exists()
    assert not list(tmp_path.rglob("news_universe.json"))


def test_universe_replaces_pilot_symbols_and_persists_artifact(tmp_path, monkeypatch):
    """SDS §4.8: routed US set becomes the effective finnhub symbols; the
    universe artifact is persisted privately; the manifest file never changes."""
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=[("GOOGL", "US"), ("000660", "KR")]
    )
    assert code == 0
    # D10 order: held GOOGL (HOLDING) outranks watchlist-only NVDA and V.
    assert captured[0].finnhub_symbols == ("GOOGL", "NVDA", "V")
    assert captured[0].naver_enabled is False
    assert universe_store.exists()
    assert os.stat(universe_store).st_mode & 0o777 == 0o600
    artifact = json.loads(universe_store.read_bytes())
    assert artifact["latest"]["stale"] is False


def test_universe_zero_routable_kr_skips_naver_without_manifest_edit(
    tmp_path, monkeypatch, capsys
):
    """R-RTE-3 (MINOR-1): zero routable KR entries -> the enabled Naver lane is
    omitted from the effective plan only; the file's naver.enabled is untouched."""
    code, captured, _store = run_universe_main(
        tmp_path,
        monkeypatch,
        holdings=[("GOOGL", "US")],
        naver_enabled=True,
        kr_watch=(),
    )
    assert code == 0
    assert captured[0].naver_enabled is False
    assert "TOKMO_NEWS_UNIVERSE_NAVER_SKIPPED: no_routable_kr" in (
        capsys.readouterr().err
    )


def test_universe_routable_kr_builds_dynamic_naver_query(tmp_path, monkeypatch, capsys):
    """R-RTE-2 CLI arm: >=1 routable KR entry restores the built query."""
    code, captured, _store = run_universe_main(
        tmp_path, monkeypatch, holdings=[("GOOGL", "US")], naver_enabled=True
    )
    assert code == 0
    assert captured[0].naver_enabled is True
    assert captured[0].naver_query == "삼성전자"
    assert "TOKMO_NEWS_UNIVERSE_NAVER_SKIPPED" not in capsys.readouterr().err


def test_universe_overflow_emits_bounded_stderr_note(tmp_path, monkeypatch, capsys):
    """R-CAP-1 CLI arm: 13 eligible US symbols -> 10 routed, note us=3 kr=0."""
    held = [(f"H{index}", "US") for index in range(10)] + [("GOOGL", "US")]
    code, captured, _store = run_universe_main(tmp_path, monkeypatch, holdings=held)
    assert code == 0
    assert len(captured[0].finnhub_symbols) == 10
    assert "TOKMO_NEWS_UNIVERSE_OVERFLOW: us=3 kr=0" in capsys.readouterr().err


def test_universe_holdings_failure_uses_previous_and_marks_stale(
    tmp_path, monkeypatch, capsys
):
    """R-STL-1 CLI arm: corrupt holdings -> previous ∪ watchlist, stale noted."""
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=[("GOOGL", "US"), ("000660", "KR")]
    )
    assert code == 0
    fresh_symbols = captured[0].finnhub_symbols
    capsys.readouterr()
    holdings_path = tmp_path / "stores" / "current_holdings.json"
    holdings_path.write_bytes(b"corrupt")
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=None
    )
    # The second run_universe_main rebuilt tmp_path fixtures but kept the
    # corrupt holdings file and the persisted universe artifact in place.
    assert code == 0
    assert captured[0].finnhub_symbols == fresh_symbols
    captured_err = capsys.readouterr().err
    assert "TOKMO_NEWS_UNIVERSE_STALE: since=" in captured_err
    artifact = json.loads(universe_store.read_bytes())
    assert artifact["latest"]["stale"] is True


def test_universe_without_previous_or_holdings_falls_back_to_manifest(
    tmp_path, monkeypatch, capsys
):
    """R-STL-2 CLI arm: no previous artifact + fresh failure -> pilot manifest."""
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=None
    )
    assert code == 0
    assert captured[0].finnhub_symbols == PILOT_SYMBOLS
    assert not universe_store.exists()
    assert "TOKMO_NEWS_UNIVERSE_FALLBACK: static_manifest" in (capsys.readouterr().err)


def test_universe_corrupt_artifact_is_treated_absent_with_note(
    tmp_path, monkeypatch, capsys
):
    """R-STL-3 CLI arm: unparseable artifact -> treated absent, resolved fresh."""
    runtime = tmp_path / "runtime"
    (runtime / "receipts").mkdir(parents=True, exist_ok=True)
    (runtime / "news_universe.json").write_bytes(b"garbage")
    code, captured, universe_store = run_universe_main(
        tmp_path, monkeypatch, holdings=[("GOOGL", "US")]
    )
    assert code == 0
    assert captured[0].finnhub_symbols == ("GOOGL", "NVDA", "V")
    assert "TOKMO_NEWS_UNIVERSE_CORRUPT: treated_absent" in (capsys.readouterr().err)
    artifact = json.loads(universe_store.read_bytes())
    assert artifact["latest"]["stale"] is False


def test_health_telegram_env_is_validated_like_connector_credentials(tmp_path):
    env = tmp_path / "health.env"
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=42\n")
    os.chmod(env, 0o600)
    assert callable(MODULE._load_health_sender(env))
    os.chmod(env, 0o644)
    with pytest.raises(ValueError):
        MODULE._load_health_sender(env)
    os.chmod(env, 0o600)
    env.write_text("TELEGRAM_BOT_TOKEN=abc\n")
    with pytest.raises(ValueError):
        MODULE._load_health_sender(env)
    env.write_text("TELEGRAM_BOT_TOKEN=abc\nOTHER=1\nTELEGRAM_CHAT_ID=42\n")
    with pytest.raises(ValueError):
        MODULE._load_health_sender(env)
