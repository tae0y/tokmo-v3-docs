"""Stage 4 B/D: subprocess smoke tests for the 48-hour shadow runner CLI.

Real process boundaries: pre-start waiting performs provider I/O zero, a
duplicate launcher is refused by the flock, preflight fails closed on a
commit mismatch or absent PASS marker, an elapsed campaign resumes to
MISSED records and a terminal COMPLETE with exit 0, and a current slot is
invoked through the exact production cadence CLI seam in fixture dry-run
mode. No secret ever reaches stdout, stderr, or durable state.
"""

import json
import signal
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.records.prelaunch_markers import create_pass_marker

ROOT = Path(__file__).parents[1]
UTC = UTC
SECRET = "runner-cli-secret-bearer"


def repo_head() -> str:
    return subprocess.run(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def zulu(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def grid(value: datetime) -> datetime:
    return datetime.fromtimestamp(
        int(value.timestamp()) // 300 * 300,
        tz=UTC,
    )


def write_markers(tmp_path, commit):
    markers = tmp_path / "markers"
    created = datetime(2026, 8, 1, tzinfo=UTC)
    create_pass_marker(
        markers / "full_suite.json",
        kind="full-suite",
        commit_sha=commit,
        command=".venv/bin/python -m pytest -q",
        test_count=2466,
        created_at=created,
        provenance="hermes/full-suite",
    )
    create_pass_marker(
        markers / "review.json",
        kind="pilot-acceptance",
        commit_sha=commit,
        command="direct glm reclosure",
        test_count=1,
        created_at=created,
        provenance="direct-zai/glm-5.2",
    )
    return markers / "full_suite.json", markers / "review.json"


def write_fixture(tmp_path):
    path = tmp_path / "fixture.json"
    path.write_text(
        json.dumps(
            {
                "token": SECRET,
                "calendar": {"KR": {}, "US": {}},
                "accounts": [{"seed": "7", "holdings": []}],
                "quotes": {},
                "orders": {},
            }
        )
    )
    return path


def write_config(tmp_path, *, start_at, duration=1800, commit=None, markers=None):
    commit = repo_head() if commit is None else commit
    if markers is None:
        markers = write_markers(tmp_path, commit)
    config = {
        "start_at": zulu(start_at),
        "duration_seconds": duration,
        "interval_seconds": 300,
        "shadow_root": str(tmp_path / "shadow"),
        "expected_commit": commit,
        "full_suite_marker": str(markers[0]),
        "review_marker": str(markers[1]),
        "mode": "live-shadow",
        "lifecycle": "dry-run",
        "fixture": str(write_fixture(tmp_path)),
    }
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config))
    return path


def launch(config_path, tmp_path, **popen_kwargs):
    return subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "tools" / "run_48h_shadow.py"),
            "--config",
            str(config_path),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env={
            "PATH": "/usr/bin:/bin",
            "PYTHONPATH": str(ROOT / "src"),
            "HOME": str(tmp_path),
        },
        **popen_kwargs,
    )


def run_cli(config_path, tmp_path, timeout=60):
    process = launch(config_path, tmp_path)
    stdout, stderr = process.communicate(timeout=timeout)
    return process.returncode, stdout, stderr


def state_path(tmp_path):
    return tmp_path / "shadow" / "harness" / "state.json"


def test_pre_start_launch_waits_without_provider_io(tmp_path):
    start = grid(datetime.now(UTC)) + timedelta(hours=2)
    config_path = write_config(tmp_path, start_at=start)
    process = launch(config_path, tmp_path)
    try:
        time.sleep(1.5)
        assert process.poll() is None, process.communicate()[1]
    finally:
        process.send_signal(signal.SIGTERM)
        _stdout, stderr = process.communicate(timeout=10)
    assert f"waiting_until={zulu(start)}" in stderr
    # waiting performs no provider I/O and records nothing
    assert not state_path(tmp_path).exists()
    assert not (tmp_path / "shadow" / "ops").exists()


def test_duplicate_launcher_is_refused_with_provider_zero(tmp_path):
    start = grid(datetime.now(UTC)) + timedelta(hours=2)
    config_path = write_config(tmp_path, start_at=start)
    first = launch(config_path, tmp_path)
    try:
        time.sleep(1.2)
        assert first.poll() is None
        code, _stdout, stderr = run_cli(config_path, tmp_path, timeout=30)
        assert code == 1
        assert "refusal=duplicate_instance" in stderr
    finally:
        first.send_signal(signal.SIGTERM)
        first.communicate(timeout=10)


def test_commit_mismatch_fails_closed(tmp_path):
    start = grid(datetime.now(UTC)) + timedelta(hours=1)
    config_path = write_config(tmp_path, start_at=start, commit="e" * 40)
    code, _stdout, stderr = run_cli(config_path, tmp_path)
    assert code == 1
    assert "refusal=commit_mismatch" in stderr
    assert not state_path(tmp_path).exists()


def test_absent_marker_fails_closed(tmp_path):
    start = grid(datetime.now(UTC)) + timedelta(hours=1)
    config_path = write_config(
        tmp_path,
        start_at=start,
        markers=(tmp_path / "nope-full.json", tmp_path / "nope-review.json"),
    )
    code, _stdout, stderr = run_cli(config_path, tmp_path)
    assert code == 1
    assert "refusal=marker_full_suite_invalid" in stderr


def test_elapsed_campaign_resumes_to_missed_and_terminal_complete(tmp_path):
    start = grid(datetime.now(UTC) - timedelta(hours=3))
    config_path = write_config(tmp_path, start_at=start)
    config = json.loads(config_path.read_text())
    harness = tmp_path / "shadow" / "harness"
    harness.mkdir(parents=True)
    state_path(tmp_path).write_text(
        json.dumps(
            {
                "schema": "tokmo-48h-shadow-state-v1",
                "config": {
                    "start_at_utc": config["start_at"],
                    "duration_seconds": 1800,
                    "interval_seconds": 300,
                    "expected_commit": config["expected_commit"],
                    "mode": "live-shadow",
                    "policy_version": "cadence-300s-v1",
                },
                "actual_start": config["start_at"],
                "intended_end": zulu(start + timedelta(seconds=1800)),
                "ticks": {},
                "terminal": None,
            }
        )
    )
    code, stdout, _stderr = run_cli(config_path, tmp_path)
    assert code == 0
    terminal = json.loads(stdout.splitlines()[-1])
    assert terminal["status"] == "COMPLETE"
    assert terminal["terminal"]["counts"] == {"MISSED": 6}
    state = json.loads(state_path(tmp_path).read_text())
    assert all(
        record["status"] == "MISSED" and record["reason"] == "elapsed_during_downtime"
        for record in state["ticks"].values()
    )
    # never replayed: the production tick seam never ran, provider zero
    assert not (tmp_path / "shadow" / "ops").exists()

    # relaunch after COMPLETE is inert with exit 0
    code, stdout, _stderr = run_cli(config_path, tmp_path)
    assert code == 0
    assert json.loads(stdout.splitlines()[-1])["status"] == "ALREADY_COMPLETE"


def test_current_slot_runs_through_the_production_cli_seam(tmp_path):
    # avoid racing the slot boundary: keep >=20s of the window ahead of the
    # subprocess launch so the slot cannot elapse before the first invoke
    if int(datetime.now(UTC).timestamp()) % 300 > 280:
        time.sleep(25)
    start = grid(datetime.now(UTC))
    config_path = write_config(tmp_path, start_at=start, duration=300)
    # resume semantics put the first aligned slot at the recorded
    # actual_start, so the current live slot is invoked immediately instead
    # of waiting out the wall clock
    harness = tmp_path / "shadow" / "harness"
    harness.mkdir(parents=True)
    config = json.loads(config_path.read_text())
    state_path(tmp_path).write_text(
        json.dumps(
            {
                "schema": "tokmo-48h-shadow-state-v1",
                "config": {
                    "start_at_utc": config["start_at"],
                    "duration_seconds": 300,
                    "interval_seconds": 300,
                    "expected_commit": config["expected_commit"],
                    "mode": "live-shadow",
                    "policy_version": "cadence-300s-v1",
                },
                "actual_start": config["start_at"],
                "intended_end": zulu(start + timedelta(seconds=300)),
                "ticks": {},
                "terminal": None,
            }
        )
    )
    code, stdout, stderr = run_cli(config_path, tmp_path, timeout=120)
    assert code == 0, stderr
    state = json.loads(state_path(tmp_path).read_text())
    [record] = state["ticks"].values()
    # the exact cadence CLI ran: its durable receipt store exists and the
    # recorded outcome comes from the production vocabulary (lag decides
    # SKIPPED_CLOSED versus MISSED here)
    assert record["status"] in ("SKIPPED_CLOSED", "MISSED")
    assert (tmp_path / "shadow" / "ops" / "cadence_ticks.sqlite3").is_file()
    assert state["terminal"]["status"] == "COMPLETE"
    # bounded lifecycle only: START then FINISH, no stock alerts
    lifecycle = tmp_path / "shadow" / "harness" / "lifecycle_log.jsonl"
    kinds = [json.loads(line)["kind"] for line in lifecycle.read_text().splitlines()]
    assert kinds == ["START", "FINISH"]
    # no secret in stdout, stderr, or durable state
    for text in (stdout, stderr, state_path(tmp_path).read_text()):
        assert SECRET not in text


def test_invalid_config_is_refused(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"start_at": "2026-08-02T06:30:00+09:00"}))
    code, _stdout, stderr = run_cli(path, tmp_path)
    assert code == 1
    assert "refusal=config_invalid" in stderr
