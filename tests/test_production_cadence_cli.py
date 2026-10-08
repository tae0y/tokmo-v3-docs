"""Stage 3-D: the `tokmo-cadence` CLI seam, subprocess-tested in dry-run mode.

One machine-readable JSON summary line on stdout, logs on stderr, stable exit
codes (0 truthful complete/skip, 1 configuration/preflight refusal, 2 every
duplicate/missed/overlap/partial/failure), and no secret ever printed.
"""

import json
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).parents[1]
SECRET = "cli-secret-bearer-value"


def fixture_payload():
    return {
        "token": SECRET,
        "calendar": {
            "KR": {"regular_start": "2026-08-03T00:00:00Z",
                   "regular_end": "2026-08-03T06:30:00Z"},
            "US": {"regular_start": "2026-08-03T13:30:00Z",
                   "regular_end": "2026-08-03T20:00:00Z"},
        },
        "accounts": [{"seed": "42", "label": "brokerage", "holdings": [
            {"symbol": "AAPL", "quantity": "1", "avg_price": "100",
             "currency": "USD", "market_raw": "US"},
        ]}],
        "quotes": {"AAPL": {"price": "101.5", "currency": "USD"}},
        "orders": {},
    }


def run_cli(args, cwd=ROOT, home="/tmp"):
    return subprocess.run(
        [sys.executable, "-c",
         "import sys; from tokmo.composition import cadence_main; "
         "sys.exit(cadence_main(sys.argv[1:]))", *args],
        capture_output=True, text=True, cwd=cwd,
        env={"PATH": "/usr/bin:/bin", "PYTHONPATH": str(ROOT / "src"),
             "HOME": str(home)},
    )


def write_fixture(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(fixture_payload()))
    return path


def base_args(tmp_path, slot="2026-08-03T10:00:00Z"):
    return [
        "--once", "--mode", "live-shadow",
        "--policy-version", "cadence-300s-v1",
        "--scheduled-for", slot,
        "--observed-now", slot.replace("00Z", "01Z"),
        "--root", str(tmp_path / "shadow-root"),
        "--fixture", str(write_fixture(tmp_path)),
    ]


def summary_of(completed):
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1, f"stdout must be one JSON summary line: {lines}"
    return json.loads(lines[0])


def test_closed_tick_dry_run_exits_zero_with_one_json_summary(tmp_path):
    completed = run_cli(base_args(tmp_path))
    assert completed.returncode == 0, completed.stderr
    summary = summary_of(completed)
    assert summary["outcome"] == "SKIPPED_CLOSED"
    assert summary["exit_code"] == 0
    assert summary["policy_version"] == "cadence-300s-v1"
    assert summary["scheduled_for_utc"] == "2026-08-03T10:00:00Z"
    assert summary["open_markets"] == []
    assert summary["wire"]["operation_started"] == {
        "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
    }
    assert {item["stage"]: item["status"] for item in summary["stages"]}[
        "holdings"] == "skipped"
    assert (tmp_path / "shadow-root" / "ops" / "cadence_ticks.sqlite3").is_file()


def test_open_tick_dry_run_completes_with_masked_wire_counts(tmp_path):
    completed = run_cli(base_args(tmp_path, slot="2026-08-03T14:00:00Z"))
    assert completed.returncode == 0, completed.stderr
    summary = summary_of(completed)
    assert summary["outcome"] == "COMPLETE"
    assert summary["open_markets"] == ["US"]
    assert summary["mask_digest"]
    assert summary["wire"]["operation_started"]["ACCOUNTS"] == 1


def test_duplicate_slot_invocation_exits_two(tmp_path):
    args = base_args(tmp_path)
    first = run_cli(args)
    assert first.returncode == 0, first.stderr
    second = run_cli(args)
    assert second.returncode == 2, second.stderr
    assert summary_of(second)["outcome"] == "DUPLICATE"


def test_secrets_never_reach_stdout_or_stderr(tmp_path):
    for slot in ("2026-08-03T10:00:00Z", "2026-08-03T14:00:00Z"):
        workdir = tmp_path / slot[11:13]
        workdir.mkdir()
        completed = run_cli(base_args(workdir, slot=slot))
        assert SECRET not in completed.stdout
        assert SECRET not in completed.stderr


def test_non_live_shadow_mode_is_a_configuration_refusal(tmp_path):
    args = base_args(tmp_path)
    args[args.index("live-shadow")] = "live"
    completed = run_cli(args)
    assert completed.returncode == 1
    # a refusal precedes all provider work: stdout carries no summary at all
    # and stderr names the exact refusal token
    assert completed.stdout == ""
    assert "refusal=unsupported_mode" in completed.stderr
    assert not (tmp_path / "shadow-root").exists()


def test_production_mode_requires_exact_canonical_root(tmp_path):
    args = base_args(tmp_path)
    args[args.index("live-shadow")] = "production"
    fixture_index = args.index("--fixture")
    del args[fixture_index:fixture_index + 2]
    observed_index = args.index("--observed-now")
    del args[observed_index:observed_index + 2]
    completed = run_cli(args)
    assert completed.returncode == 1
    assert "refusal=production_mode_requires_canonical_root" in completed.stderr
    assert not (tmp_path / "shadow-root").exists()


def test_production_mode_forbids_fixture_even_at_canonical_root(tmp_path):
    args = base_args(tmp_path)
    args[args.index("live-shadow")] = "production"
    home = tmp_path / "home"
    args[args.index("--root") + 1] = str(home / ".local/share/tokmo-v3")
    completed = run_cli(args, home=home)
    assert completed.returncode == 1
    assert "refusal=production_mode_forbids_fixture" in completed.stderr
    assert not (home / ".local/share/tokmo-v3").exists()


@pytest.mark.parametrize(
    ("extra", "reason"),
    [
        (["--replay"], "production_mode_forbids_replay"),
        (["--observed-now", "2026-08-03T14:00:01Z"],
         "production_mode_forbids_observed_now"),
    ],
)
def test_production_mode_forbids_shadow_only_controls(tmp_path, extra, reason):
    args = base_args(tmp_path)
    args[args.index("live-shadow")] = "production"
    fixture_index = args.index("--fixture")
    del args[fixture_index:fixture_index + 2]
    args.extend(extra)
    completed = run_cli(args)
    assert completed.returncode == 1
    assert f"refusal={reason}" in completed.stderr
    assert not (tmp_path / "shadow-root").exists()


def test_scheduled_for_utc_alias_is_supported(tmp_path):
    args = base_args(tmp_path)
    args[args.index("--scheduled-for")] = "--scheduled-for-utc"
    completed = run_cli(args)
    assert completed.returncode == 0
    assert summary_of(completed)["outcome"] == "SKIPPED_CLOSED"


def test_accounts_transport_failure_logs_sanitized_category(tmp_path):
    args = base_args(tmp_path, slot="2026-08-03T14:00:00Z")
    payload = fixture_payload()
    payload["accounts_fail"] = "server_error"
    path = tmp_path / "accounts-failing-fixture.json"
    path.write_text(json.dumps(payload))
    args[args.index("--fixture") + 1] = str(path)
    completed = run_cli(args)
    assert completed.returncode == 2
    assert "provider_failure operation=ACCOUNTS category=server_error" in completed.stderr
    assert SECRET not in completed.stderr


def test_stage_failure_still_emits_one_json_summary_with_exit_two(tmp_path):
    args = base_args(tmp_path, slot="2026-08-03T14:00:00Z")
    payload = fixture_payload()
    payload["quotes_fail"] = True
    path = tmp_path / "failing-fixture.json"
    path.write_text(json.dumps(payload))
    args[args.index("--fixture") + 1] = str(path)
    completed = run_cli(args)
    assert completed.returncode == 2, completed.stderr
    summary = summary_of(completed)
    assert summary["outcome"] == "PARTIAL"
    assert summary["exit_code"] == 2
    stages = {item["stage"]: item["status"] for item in summary["stages"]}
    assert stages["quotes"] == "failed"


def test_noncanonical_slot_is_a_configuration_refusal(tmp_path):
    completed = run_cli(base_args(tmp_path, slot="2026-08-03T10:00:07Z"))
    assert completed.returncode == 1


def test_missing_once_flag_is_a_configuration_refusal(tmp_path):
    args = base_args(tmp_path)
    args.remove("--once")
    completed = run_cli(args)
    assert completed.returncode == 1


def test_production_preflight_refusals_preserve_failure_class(monkeypatch, capsys):
    from tokmo import composition
    from tokmo.runtime.config import ConfigError
    from tokmo.watchlists import WatchlistError

    args = [
        "--once", "--mode", "production",
        "--policy-version", "cadence-300s-v1",
        "--scheduled-for", "2026-08-03T10:00:00Z",
        "--root", str(Path.home() / ".local/share/tokmo-v3"),
    ]
    cases = [
        (ConfigError("missing_keys"), "credential_preflight_failed"),
        (WatchlistError("duplicate_symbol"),
         "watchlist_preflight_failed_duplicate_symbol"),
        (OSError("private path"), "production_preflight_io_failed"),
        (ValueError("unsafe detail"), "production_preflight_validation_failed"),
    ]
    for error, reason in cases:
        def fail(**_kwargs):
            raise error
        monkeypatch.setattr(composition, "build_production_cadence_deps", fail)
        assert composition.cadence_main(args) == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert captured.err == f"tokmo-cadence refusal={reason}\n"
        assert "private path" not in captured.err
        assert "unsafe detail" not in captured.err


def test_production_canonical_root_is_refused(tmp_path):
    args = base_args(tmp_path)
    args[args.index("--root") + 1] = "/tmp/.local/share/tokmo-v3"
    completed = run_cli(args)
    assert completed.returncode == 1
