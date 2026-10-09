import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

P = Path(__file__).parents[1] / "tools/run_macro_runtime.py"
spec = importlib.util.spec_from_file_location("run_macro_runtime", P)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)


def test_help_preflight_paused_network_zero(tmp_path):
    assert m.main(["--help"]) == 0
    assert m.main(["--preflight"]) == 0
    cfg = tmp_path / "c.json"
    cfg.write_text(
        json.dumps(
            {"enabled": False, "policy": "PAUSED", "root": str(tmp_path / "state")}
        )
    )
    assert m.main(["--config", str(cfg), "--paused"]) == 0


def test_runner_calls_composition_seam(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(m, "invoke_macro_runtime", lambda deps: calls.append(deps) or 0)
    assert m.main(["--preflight"]) == 0 and calls == []


def test_unknown_root_refuses_and_enabled_branch_builds_stores_with_injected_home(
    monkeypatch, tmp_path
):
    assert m.main(["--preflight", "--root", str(tmp_path)]) == 1
    import tokmo.programs.macro_canary_adapter as adapter
    import tokmo.records.calendar_cache as calendar
    import tokmo.records.regular_session_history as history
    import tokmo.runtime.config as runtime_config
    from tokmo import composition

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    captured = []
    h = object()
    c = object()
    monkeypatch.setattr(history, "RegularSessionHistoryStore", lambda *a, **k: h)
    monkeypatch.setattr(calendar, "CalendarCacheStore", lambda *a, **k: c)
    monkeypatch.setattr(
        adapter,
        "resolve_completed_session",
        lambda *a: __import__("datetime").date(2026, 8, 7),
    )
    monkeypatch.setattr(
        runtime_config,
        "load_toss_config",
        lambda p: type("T", (), {"client_id": "c", "client_secret": "s"})(),
    )
    monkeypatch.setattr(
        composition,
        "build_macro_runtime_deps",
        lambda closed: captured.append(closed) or closed,
    )
    result = m.build_runtime_deps(
        {"enabled": True, "policy": "SHADOW", "root": str(tmp_path / "state")}
    )
    assert (
        result["session_authorities"] == (h, c)
        and result["completed_session"].isoformat() == "2026-08-07"
        and result["credentials"] == ("c", "s")
    )


# --- AR-12/AR-13/AR-14: FRED config gating in build_runtime_deps ---


def test_ar12_config_without_fred_enrichment_leaves_fred_caller_absent(
    monkeypatch, tmp_path
):
    """A config without fred_enrichment produces a dependency graph with
    fred_caller absent."""
    import tokmo.programs.macro_canary_adapter as adapter
    import tokmo.records.calendar_cache as calendar
    import tokmo.records.regular_session_history as history
    import tokmo.runtime.config as runtime_config
    from tokmo import composition

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    h = object()
    c = object()
    monkeypatch.setattr(history, "RegularSessionHistoryStore", lambda *a, **k: h)
    monkeypatch.setattr(calendar, "CalendarCacheStore", lambda *a, **k: c)
    monkeypatch.setattr(
        adapter,
        "resolve_completed_session",
        lambda *a: __import__("datetime").date(2026, 8, 7),
    )
    monkeypatch.setattr(
        runtime_config,
        "load_toss_config",
        lambda p: type("T", (), {"client_id": "c", "client_secret": "s"})(),
    )
    captured = []
    monkeypatch.setattr(
        composition,
        "build_macro_runtime_deps",
        lambda closed: captured.append(closed) or closed,
    )
    result = m.build_runtime_deps(
        {"enabled": True, "policy": "SHADOW", "root": str(tmp_path / "state")}
    )
    assert "fred_caller" not in result


def test_ar13_fred_enrichment_true_with_missing_keys_returns_1(monkeypatch, tmp_path):
    """fred_enrichment: true with fred_root/fred_credential missing
    must return code 1 without running anything. AR-13 update (v0.3):
    config now needs valid expected_commit + subprocess stub for B2 pin."""
    import tokmo.programs.macro_canary_adapter as adapter
    import tokmo.records.calendar_cache as calendar
    import tokmo.records.regular_session_history as history
    import tokmo.runtime.config as runtime_config

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    h = object()
    c = object()
    monkeypatch.setattr(history, "RegularSessionHistoryStore", lambda *a, **k: h)
    monkeypatch.setattr(calendar, "CalendarCacheStore", lambda *a, **k: c)
    monkeypatch.setattr(
        adapter,
        "resolve_completed_session",
        lambda *a: __import__("datetime").date(2026, 8, 7),
    )
    monkeypatch.setattr(
        runtime_config,
        "load_toss_config",
        lambda p: type("T", (), {"client_id": "c", "client_secret": "s"})(),
    )
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"1" * 40 + b"\n"})(),
    )
    cfg = tmp_path / "fred_missing.json"
    cfg.write_text(
        json.dumps(
            {
                "enabled": True,
                "policy": "SHADOW",
                "root": str(tmp_path / "state"),
                "expected_commit": "1" * 40,
                "expected_candidate_sha256": hashlib.sha256(
                    ("1" * 40 + "\n").encode()
                ).hexdigest(),
                "fred_enrichment": True,
            }
        )
    )
    assert m.main(["--config", str(cfg)]) == 1


def test_ar14_paused_still_returns_0_before_fred_construction(tmp_path):
    """--paused/enabled: false/policy: PAUSED still return 0
    before any FRED construction."""
    for enabled, policy, paused in (
        (False, "PAUSED", False),
        (True, "SHADOW", True),
        (True, "PAUSED", False),
    ):
        cfg = tmp_path / f"cfg_{enabled}_{policy}_{paused}.json"
        cfg.write_text(
            json.dumps(
                {
                    "enabled": enabled,
                    "policy": policy,
                    "root": str(tmp_path / "state"),
                    "fred_enrichment": True,
                    "fred_root": str(tmp_path / "fred"),
                    "fred_credential": str(tmp_path / "fred.env"),
                }
            )
        )
        argv = ["--config", str(cfg)]
        if paused:
            argv.append("--paused")
        assert m.main(argv) == 0


# --- B1/B2 v0.3: session-authority stderr sanitization + commit-pin ---

import io
from contextlib import redirect_stderr


@pytest.mark.parametrize(
    "error,expected",
    [
        *(
            (ValueError(reason), reason)
            for reason in sorted(m.SESSION_AUTHORITY_REASONS)
        ),
        (
            OSError("/Users/private/runtime.sqlite3 token=secret"),
            "session authority unclassified",
        ),
        (
            ValueError("/Users/private/config.json api_key=secret"),
            "session authority unclassified",
        ),
    ],
)
def test_session_authority_stderr_six_reasons(monkeypatch, tmp_path, error, expected):
    """Execute the resolver failure branch and expose only the closed reason."""
    import tokmo.programs.macro_canary_adapter as adapter
    import tokmo.records.calendar_cache as calendar
    import tokmo.records.regular_session_history as history
    from tokmo import composition

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(history, "RegularSessionHistoryStore", lambda *a, **k: object())
    monkeypatch.setattr(calendar, "CalendarCacheStore", lambda *a, **k: object())
    monkeypatch.setattr(
        adapter, "resolve_completed_session", lambda *a: (_ for _ in ()).throw(error)
    )
    monkeypatch.setattr(composition, "build_macro_runtime_deps", lambda closed: closed)
    err = io.StringIO()
    with redirect_stderr(err):
        result = m.build_runtime_deps(
            {"enabled": True, "policy": "SHADOW", "root": str(tmp_path / "state")}
        )
    assert result["completed_session"] is None
    assert err.getvalue() == f"session-authority: {expected}\n"
    assert not any(
        leak in err.getvalue()
        for leak in ("/Users/", ".sqlite3", ".json", "token", "secret", "api_key")
    )


def test_commit_pin_mismatch_returns_zero_no_deps(monkeypatch, tmp_path):
    """B2: HEAD mismatch -> return 0, build_runtime_deps not called."""
    calls = []
    monkeypatch.setattr(
        m, "build_runtime_deps", lambda config: calls.append(config) or 0
    )
    monkeypatch.setattr(m, "invoke_macro_runtime", lambda deps: 0)
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"deadbeef\n"})(),
    )
    cfg = tmp_path / "pin_mismatch.json"
    cfg.write_text(
        json.dumps(
            {
                "enabled": True,
                "policy": "SHADOW",
                "root": str(tmp_path / "state"),
                "expected_commit": "1" * 40,
                "expected_candidate_sha256": "2" * 64,
            }
        )
    )
    err = io.StringIO()
    with redirect_stderr(err):
        assert m.main(["--config", str(cfg)]) == 0
    assert calls == []
    assert "commit-pin: mismatch" in err.getvalue()


def test_commit_pin_missing_expected_commit_returns_zero(monkeypatch, tmp_path):
    """B2: missing expected_commit -> return 0."""
    calls = []
    monkeypatch.setattr(
        m, "build_runtime_deps", lambda config: calls.append(config) or 0
    )
    monkeypatch.setattr(
        m.subprocess,
        "run",
        lambda *a, **k: type("R", (), {"returncode": 0, "stdout": b"1" * 40 + b"\n"})(),
    )
    cfg = tmp_path / "no_commit.json"
    cfg.write_text(
        json.dumps(
            {"enabled": True, "policy": "SHADOW", "root": str(tmp_path / "state")}
        )
    )
    err = io.StringIO()
    with redirect_stderr(err):
        assert m.main(["--config", str(cfg)]) == 0
    assert calls == []
    assert "commit-pin: mismatch" in err.getvalue()


def test_commit_pin_unavailable_returns_zero(monkeypatch, tmp_path):
    """B2: git unavailable (FileNotFoundError) -> 'commit-pin: unavailable', exit 0."""
    calls = []
    monkeypatch.setattr(
        m, "build_runtime_deps", lambda config: calls.append(config) or 0
    )

    def boom(*a, **k):
        raise FileNotFoundError("git not found")

    monkeypatch.setattr(m.subprocess, "run", boom)
    cfg = tmp_path / "git_missing.json"
    cfg.write_text(
        json.dumps(
            {
                "enabled": True,
                "policy": "SHADOW",
                "root": str(tmp_path / "state"),
                "expected_commit": "1" * 40,
                "expected_candidate_sha256": "2" * 64,
            }
        )
    )
    err = io.StringIO()
    with redirect_stderr(err):
        assert m.main(["--config", str(cfg)]) == 0
    assert calls == []
    assert "commit-pin: unavailable" in err.getvalue()


def test_commit_pin_match_proceeds(monkeypatch, tmp_path):
    """B2: the scoped candidate matches, so the real seam is invoked."""
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return type(
            "R", (), {"returncode": 0, "stdout": b"reviewed \xff scoped diff"}
        )()

    monkeypatch.setattr(m.subprocess, "run", run)
    monkeypatch.setattr(m, "build_runtime_deps", lambda config: {"config": config})
    invoked = []
    monkeypatch.setattr(
        m, "invoke_macro_runtime", lambda deps: invoked.append(deps) or 7
    )
    cfg = tmp_path / "pin_ok.json"
    cfg.write_text(
        json.dumps(
            {
                "enabled": True,
                "policy": "SHADOW",
                "root": str(tmp_path / "state"),
                "expected_commit": "1" * 40,
                "expected_candidate_sha256": hashlib.sha256(
                    b"reviewed \xff scoped diff"
                ).hexdigest(),
            }
        )
    )
    assert m.main(["--config", str(cfg)]) == 7
    assert invoked and calls[0][1]["timeout"] == 5
    assert "text" not in calls[0][1]
    assert calls[0][0][3:5] == ["diff", "--binary"]
