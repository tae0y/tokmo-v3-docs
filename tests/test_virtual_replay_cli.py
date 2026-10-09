"""Stage 4 A/D: the virtual replay CLI seam, subprocess-tested.

``python -m tokmo.programs.virtual_replay`` runs a named scenario against an
explicit shadow-only root, writes the durable JSON report to an explicit
shadow-only path, prints one machine-readable summary line, and exits 0 only
when every invariant assertion passed (1 configuration refusal, 2 invariant
violation). No secret and no live transport is reachable from this seam.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).parents[1]


def run_cli(args, home):
    return subprocess.run(
        [sys.executable, "-m", "tokmo.programs.virtual_replay", *args],
        capture_output=True,
        text=True,
        cwd=ROOT,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "src"),
            "HOME": str(home),
        },
    )


def test_smoke_replay_cli_writes_nonempty_report_and_exits_zero(tmp_path):
    report_path = tmp_path / "out" / "replay-report.json"
    completed = run_cli(
        [
            "--scenario",
            "smoke-12",
            "--root",
            str(tmp_path / "shadow"),
            "--report",
            str(report_path),
        ],
        home=tmp_path,
    )
    assert completed.returncode == 0, completed.stderr
    report = json.loads(report_path.read_text())
    assert report["schema"] == "tokmo-virtual-replay-report-v1"
    assert report["planned_ticks"] == 12
    assert all(entry["passed"] for entry in report["assertions"])
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert len(lines) == 1
    summary = json.loads(lines[0])
    assert summary["scenario"] == "smoke-12"
    assert summary["planned_ticks"] == 12
    assert summary["assertions_failed"] == 0
    assert summary["report_path"] == str(report_path)
    # the fixture bearer never reaches stdout, stderr, or the report
    for text in (completed.stdout, completed.stderr, report_path.read_text()):
        assert "fixture-bearer" not in text


def test_unknown_scenario_is_a_configuration_refusal(tmp_path):
    completed = run_cli(
        [
            "--scenario",
            "nope",
            "--root",
            str(tmp_path / "shadow"),
            "--report",
            str(tmp_path / "r.json"),
        ],
        home=tmp_path,
    )
    assert completed.returncode == 1
    assert completed.stdout == ""
    assert not (tmp_path / "shadow").exists()


def test_production_canonical_report_path_is_refused(tmp_path):
    completed = run_cli(
        [
            "--scenario",
            "smoke-12",
            "--root",
            str(tmp_path / "shadow"),
            "--report",
            str(tmp_path / ".local/share/tokmo-v3/r.json"),
        ],
        home=tmp_path,
    )
    assert completed.returncode == 1
    assert not (tmp_path / ".local/share/tokmo-v3/r.json").exists()


def test_production_canonical_root_is_refused(tmp_path):
    completed = run_cli(
        [
            "--scenario",
            "smoke-12",
            "--root",
            str(tmp_path / ".local/share/tokmo-v3"),
            "--report",
            str(tmp_path / "r.json"),
        ],
        home=tmp_path,
    )
    assert completed.returncode == 1


def test_missing_required_arguments_refuse(tmp_path):
    completed = run_cli(["--scenario", "smoke-12"], home=tmp_path)
    assert completed.returncode == 1
