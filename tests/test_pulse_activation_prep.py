"""Executable contracts for the paused Pulse activation artifacts."""

import importlib.util
import json
import os
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest


PROJECT = Path(__file__).parents[1]
WRAPPER = PROJECT / "ops/hermes-cron/tokmo_v3_pulse.sh"


def _runner_module():
    spec = importlib.util.spec_from_file_location(
        "activation_prep_runner", PROJECT / "tools/run_tokmo_pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_runner_passes_slot_and_observed_now_from_one_sample(monkeypatch):
    module = _runner_module()
    samples = iter((
        datetime(2026, 8, 11, 0, 0, 59, 900000, tzinfo=timezone.utc),
        datetime(2026, 8, 11, 0, 1, 0, 100000, tzinfo=timezone.utc),
    ))
    calls = []

    class BoundaryDateTime:
        @classmethod
        def now(cls, tz):
            calls.append(tz)
            return next(samples)

        fromtimestamp = staticmethod(datetime.fromtimestamp)

    captured = {}
    monkeypatch.setattr(module, "datetime", BoundaryDateTime)
    def capture(args, *, environ=None, capture_clock=None):
        captured["args"] = args
        captured["environ"] = environ
        captured["capture_clock"] = capture_clock
        return 0

    monkeypatch.setattr(module, "pulse_main", capture)
    assert module.main() == 0
    args = captured["args"]
    assert len(calls) == 1
    assert args[args.index("--scheduled-for") + 1] == "2026-08-11T00:00:00Z"
    assert args[args.index("--observed-now") + 1] == "2026-08-11T00:00:59.900000Z"
    # The ambient edge injects the process environment (G3 SDS §4.1);
    # src itself never reads os.environ.
    assert captured["environ"] is not None
    assert captured['capture_clock']() == datetime(2026, 8, 11, 0, 1, 0, 100000, tzinfo=timezone.utc)
    assert len(calls) == 2  # Capture time advances independently of the slot identity.


def _write_executable(path, text):
    path.write_text(text)
    path.chmod(0o700)


@pytest.fixture
def wrapper_env(tmp_path):
    project = tmp_path / "canonical/projects/tokmo-v3"
    project.mkdir(parents=True)
    runtime = tmp_path / "runtime"
    fake = tmp_path / "completion-safe-cli"
    alert = tmp_path / "send-system-alert"
    _write_executable(fake, """#!/bin/bash
set -eu
while [ "$#" -gt 0 ]; do
  case "$1" in
    --stdout) out="$2"; shift 2;;
    --stderr) err="$2"; shift 2;;
    --manifest) manifest="$2"; shift 2;;
    --cwd) cwd="$2"; shift 2;;
    --) shift; break;;
    *) shift;;
  esac
done
[ "$cwd" = "$TOKMO_PULSE_PROJECT" ] || exit 8
printf '%s\n' "${FAKE_STDOUT:-}" >"$out"
printf '%s\n' "${FAKE_STDERR:-}" >"$err"
printf 'launcher diagnostic\n' 
printf '{"child_exit_code":%s}\n' "${FAKE_CHILD_RC:-0}" >"$manifest"
exit "${FAKE_WRAPPER_RC:-0}"
""")
    _write_executable(alert, """#!/bin/bash
printf '%s\n' "$*" >>"$ALERT_CAPTURE"
""")
    env = os.environ.copy()
    env.update({
        "TOKMO_PULSE_PROJECT": str(project),
        "TOKMO_PULSE_RUNTIME": str(runtime),
        "TOKMO_COMPLETION_SAFE_CLI": str(fake),
        "TOKMO_SYSTEM_ALERT_SCRIPT": str(alert),
        "ALERT_CAPTURE": str(tmp_path / "alerts"),
        "TOKMO_PULSE_NOW_EPOCH": "1786406400",
    })
    return env, runtime, Path(env["ALERT_CAPTURE"])


def _run_wrapper(env):
    return subprocess.run(["bash", str(WRAPPER)], env=env, text=True,
                          capture_output=True, check=False)


def _manifest_paths(runtime):
    return sorted(runtime.glob("*/manifest.json"))


def test_wrapper_success_is_silent_and_private(wrapper_env):
    env, runtime, alerts = wrapper_env
    result = _run_wrapper(env)
    assert result.returncode == 0
    assert result.stdout == ""
    assert not alerts.exists()
    base = _manifest_paths(runtime)[0].parent
    assert stat.S_IMODE((base / "launcher.log").stat().st_mode) == 0o600


def test_wrapper_child_failure_alerts_with_pointers_and_launcher_head(wrapper_env):
    env, runtime, alerts = wrapper_env
    env["FAKE_CHILD_RC"] = "2"
    result = _run_wrapper(env)
    assert result.returncode == 1
    manifest = _manifest_paths(runtime)[0]
    assert json.loads(manifest.read_text())["child_exit_code"] == 2
    alert = alerts.read_text()
    assert "wrapper_rc=0 child_rc=2" in alert
    assert "launcher diagnostic" in alert
    assert all(str(manifest.parent / name) in alert for name in
               ("manifest.json", "stdout.log", "stderr.log"))


def test_wrapper_suffix_is_distinct_and_unsafe_suffix_fails_closed(wrapper_env):
    env, runtime, _ = wrapper_env
    assert _run_wrapper(env).returncode == 0
    env["TOKMO_PULSE_SLOT_SUFFIX"] = "-reconcile-42"
    assert _run_wrapper(env).returncode == 0
    assert len(_manifest_paths(runtime)) == 2
    env["TOKMO_PULSE_SLOT_SUFFIX"] = "../../unsafe"
    result = _run_wrapper(env)
    assert result.returncode == 1
    assert len(_manifest_paths(runtime)) == 2


def test_wrapper_launcher_error_is_captured(wrapper_env):
    env, runtime, alerts = wrapper_env
    env["FAKE_WRAPPER_RC"] = "7"
    result = _run_wrapper(env)
    assert result.returncode == 1
    assert "launcher diagnostic" in _manifest_paths(runtime)[0].with_name("launcher.log").read_text()
    assert "launcher diagnostic" in alerts.read_text()


def test_wrapper_alert_throttle_and_state_mode(wrapper_env):
    env, runtime, alerts = wrapper_env
    env["FAKE_CHILD_RC"] = "2"
    assert _run_wrapper(env).returncode == 1
    assert _run_wrapper(env).returncode == 1
    assert alerts.read_text().count("tokmo-v3-pulse failure") == 1
    env["TOKMO_PULSE_NOW_EPOCH"] = str(int(env["TOKMO_PULSE_NOW_EPOCH"]) + 901)
    assert _run_wrapper(env).returncode == 1
    assert alerts.read_text().count("tokmo-v3-pulse failure") == 2
    state = runtime / ".alert_fingerprint_state"
    assert stat.S_IMODE(state.stat().st_mode) == 0o600


def execution_stage_accepted(stage):
    return stage.get("status") == "saved" and stage.get("reason") == "saved"


@pytest.mark.parametrize("stage, accepted", [
    ({"status": "saved", "reason": "saved"}, True),
    ({"status": "saved", "reason": "partial_accounts"}, False),
    ({"status": "saved", "reason": "pagination_pending"}, False),
    ({"status": "partial", "reason": "saved"}, False),
])
def test_execution_stage_acceptance_is_observable(stage, accepted):
    assert execution_stage_accepted(stage) is accepted


def tick_ids_are_contiguous(tick_ids):
    return all(right == left + 1 for left, right in zip(tick_ids, tick_ids[1:]))


def test_tick_id_gap_is_a_stop_condition():
    assert tick_ids_are_contiguous([10, 11, 12])
    assert not tick_ids_are_contiguous([10, 12])
