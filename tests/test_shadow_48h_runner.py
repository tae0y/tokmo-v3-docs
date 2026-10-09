"""Stage 4 B: the durable 48-hour live-shadow runner core.

One runner invokes the exact production tick seam every aligned five-minute
slot for exactly 48 hours from the recorded actual start. Launched early it
waits with provider I/O zero; it records ``actual_start`` exactly once;
crash resume continues future slots and records elapsed slots as MISSED
without ever replaying them; the terminal state is COMPLETE with exit 0 and
relaunch after completion is inert. Every preflight gate fails closed before
any provider work: expected commit, full-suite and independent-review PASS
markers, safe shadow root, order-write capability absent, read-only
credentials present, sane clock, single instance. Durable state is atomic,
private (0600), symlink-refusing, and never contains a secret.
"""

import fcntl
import json
import os
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.shadow_48h_runner import (
    Shadow48hConfigV1,
    ShadowRunnerDepsV1,
    run_48h_shadow,
)
from tokmo.records.prelaunch_markers import MarkerError
from tokmo.values.cadence_policy import CADENCE_OPERATIONS

UTC = UTC
COMMIT = "55b65f63359f0d3a97920ea794b9d5b60bc8003e"
START = datetime(2026, 8, 1, 21, 30, tzinfo=UTC)  # 2026-08-02T06:30 KST


class FakeTime:
    def __init__(self, now):
        self.now = now

    def clock(self):
        return self.now

    def sleep(self, seconds):
        assert seconds >= 0
        self.now += timedelta(seconds=seconds)


class Crash(BaseException):
    """Simulated process death: the runner must not swallow it."""


def make_config(tmp_path, **overrides):
    values = {
        "start_at_utc": START,
        "shadow_root": tmp_path / "shadow",
        "expected_commit": COMMIT,
        "full_suite_marker": tmp_path / "markers" / "full_suite.json",
        "review_marker": tmp_path / "markers" / "review.json",
        "duration_seconds": 1800,
        "interval_seconds": 300,
    }
    values.update(overrides)
    return Shadow48hConfigV1(**values)


def make_deps(
    fake,
    *,
    invoked,
    emits,
    summary=None,
    head=COMMIT,
    tick_runtime=5,
    environ=None,
    marker_error=None,
):
    def invoke_tick(slot, observed):
        invoked.append((slot, observed))
        fake.now += timedelta(seconds=tick_runtime)
        result = {
            "outcome": "SKIPPED_CLOSED",
            "reason": "both_markets_closed",
            "exit_code": 0,
            "lag_seconds": (observed - slot).total_seconds(),
            "mask_digest": None,
            "open_markets": [],
            "wire": {"total_started": 0},
            "bearer": "SECRET-BEARER-VALUE",  # must never reach state
        }
        if summary is not None:
            result.update(summary)
        return result

    def marker_verify(path, *, kind, expected_commit):
        if marker_error is not None and marker_error in Path(path).name:
            raise MarkerError(f"invalid marker {kind}")
        return {"kind": kind, "commit_sha": expected_commit}

    return ShadowRunnerDepsV1(
        clock=fake.clock,
        sleep=fake.sleep,
        invoke_tick=invoke_tick,
        git_head=lambda: head,
        credentials_check=lambda: True,
        marker_verify=marker_verify,
        environ={} if environ is None else environ,
        lifecycle_emit=lambda kind, signature=None, detail=None: emits.append(
            (kind, signature)
        ),
    )


def read_state(config):
    return json.loads((config.shadow_root / "harness" / "state.json").read_text())


def slots(config, count=None):
    total = config.duration_seconds // config.interval_seconds
    return [
        config.start_at_utc + timedelta(seconds=config.interval_seconds * i)
        for i in range(total if count is None else count)
    ]


# -- config validation ------------------------------------------------------


def test_config_validates_mode_alignment_and_commit(tmp_path):
    with pytest.raises(ValueError):
        make_config(tmp_path, mode="live")
    with pytest.raises(ValueError):
        make_config(tmp_path, start_at_utc=START + timedelta(seconds=7))
    with pytest.raises(ValueError):
        make_config(tmp_path, duration_seconds=1801)
    with pytest.raises(ValueError):
        make_config(tmp_path, expected_commit="55b65f6")
    config = make_config(tmp_path)
    assert config.mode == "live-shadow"
    assert config.duration_seconds // config.interval_seconds == 6


def test_production_config_shape_is_48h_at_300s(tmp_path):
    config = make_config(
        tmp_path,
        duration_seconds=172800,
        interval_seconds=300,
    )
    assert config.duration_seconds // config.interval_seconds == 576


# -- happy path: wait, run all slots, terminal COMPLETE ---------------------


def test_early_launch_waits_without_provider_io_then_runs_all_slots(tmp_path):
    fake = FakeTime(START - timedelta(seconds=120))
    invoked, emits = [], []
    config = make_config(tmp_path)
    deps = make_deps(fake, invoked=invoked, emits=emits)
    exit_code = run_48h_shadow(config, deps)
    assert exit_code == 0
    assert [slot for slot, _observed in invoked] == slots(config)
    # every invocation happened at or after its slot and after start
    assert all(observed >= slot for slot, observed in invoked)
    assert invoked[0][1] >= START
    state = read_state(config)
    assert state["schema"] == "tokmo-48h-shadow-state-v1"
    assert state["actual_start"] == "2026-08-01T21:30:00Z"
    assert state["intended_end"] == "2026-08-01T22:00:00Z"
    assert state["terminal"]["status"] == "COMPLETE"
    assert state["terminal"]["counts"]["SKIPPED_CLOSED"] == 6
    assert emits[0] == ("START", None)
    assert emits[-1][0] == "FINISH"
    assert [kind for kind, _ in emits].count("FINISH") == 1


def test_tick_records_carry_lag_duration_wire_and_receipt_reference(tmp_path):
    fake = FakeTime(START)
    invoked, emits = [], []
    config = make_config(tmp_path)
    exit_code = run_48h_shadow(
        config,
        make_deps(fake, invoked=invoked, emits=emits),
    )
    assert exit_code == 0
    state = read_state(config)
    record = state["ticks"]["2026-08-01T21:30:00Z"]
    assert record["status"] == "SKIPPED_CLOSED"
    assert record["exit_code"] == 0
    assert record["duration_ms"] == 5000
    assert record["wire"] == {"total_started": 0}
    assert record["receipt"] == {
        "policy_version": "cadence-300s-v1",
        "scheduled_for_utc": "2026-08-01T21:30:00Z",
    }
    assert state["terminal"]["lag_max_seconds"] >= 0.0
    assert "lag_avg_seconds" in state["terminal"]


def test_tick_records_carry_only_sanitized_stage_evidence(tmp_path):
    fake = FakeTime(START)
    invoked, emits = [], []
    config = make_config(tmp_path)
    stages = [
        {
            "stage": "history",
            "status": "ok",
            "reason": "backfill_deferred",
            "latency_ms": 3,
            "bearer": "SECRET-STAGE-VALUE",
        },
        {
            "stage": "quotes",
            "status": "ok",
            "reason": None,
            "latency_ms": "not-a-number",
        },
    ]
    exit_code = run_48h_shadow(
        config,
        make_deps(
            fake,
            invoked=invoked,
            emits=emits,
            summary={"stages": stages},
        ),
    )
    assert exit_code == 0
    state = read_state(config)
    record = state["ticks"]["2026-08-01T21:30:00Z"]
    # only the four bounded stage fields survive, with type-checked values
    assert record["stages"] == [
        {
            "stage": "history",
            "status": "ok",
            "reason": "backfill_deferred",
            "latency_ms": 3,
        },
        {"stage": "quotes", "status": "ok", "reason": None, "latency_ms": None},
    ]
    assert "SECRET-STAGE-VALUE" not in json.dumps(state)


def test_terminal_reports_history_bootstrap_and_unverified_routes(tmp_path):
    fake = FakeTime(START)
    invoked, emits = [], []
    config = make_config(tmp_path)
    summary = {
        "stages": [
            {
                "stage": "history",
                "status": "ok",
                "reason": "backfill_deferred",
                "latency_ms": 1,
            }
        ],
        "wire": {"total_started": 1, "operation_started": {"CALENDAR_DAY_KR": 1}},
    }
    exit_code = run_48h_shadow(
        config,
        make_deps(
            fake,
            invoked=invoked,
            emits=emits,
            summary=summary,
        ),
    )
    assert exit_code == 0
    terminal = read_state(config)["terminal"]
    assert terminal["history_bootstrap"] == {
        "backfill_deferred_ticks": 6,
        "history_ok_ticks": 6,
    }
    assert terminal["unverified_routes"] == sorted(
        set(CADENCE_OPERATIONS) - {"CALENDAR_DAY_KR"}
    )


def test_overrunning_tick_emits_deduplicated_stalled(tmp_path):
    fake = FakeTime(START)
    invoked, emits = [], []
    config = make_config(tmp_path)
    deps = make_deps(fake, invoked=invoked, emits=emits, tick_runtime=310)
    assert run_48h_shadow(config, deps) == 0
    stalled = [entry for entry in emits if entry[0] == "STALLED"]
    assert stalled
    assert all(signature == "tick_overrun" for _kind, signature in stalled)


# -- crash resume -----------------------------------------------------------


def crash_then_resume(tmp_path, *, downtime_slots):
    config = make_config(tmp_path)
    fake = FakeTime(START)
    invoked, emits = [], []
    deps = make_deps(fake, invoked=invoked, emits=emits)
    crash_on = {"after": 2}

    def crashing_invoke(slot, observed):
        if len(invoked) >= crash_on["after"]:
            raise Crash()
        return deps.invoke_tick(slot, observed)

    with pytest.raises(Crash):
        run_48h_shadow(config, replace(deps, invoke_tick=crashing_invoke))
    assert len(invoked) == 2
    first_state = read_state(config)
    assert first_state["terminal"] is None
    # process is dead for `downtime_slots` slots, then relaunches
    fake.now = START + timedelta(seconds=300 * (2 + downtime_slots))
    resumed, resumed_emits = [], []
    resume_deps = make_deps(fake, invoked=resumed, emits=resumed_emits)
    assert run_48h_shadow(config, resume_deps) == 0
    return config, invoked, resumed, resumed_emits


def test_resume_after_crash_marks_downtime_missed_and_never_replays(tmp_path):
    config, first, resumed, _emits = crash_then_resume(
        tmp_path,
        downtime_slots=2,
    )
    state = read_state(config)
    # actual_start survives the crash exactly once
    assert state["actual_start"] == "2026-08-01T21:30:00Z"
    # slots 0-1 completed pre-crash; slot 2 crashed mid-invoke; slot 3
    # elapsed while down; both recorded MISSED, never re-invoked
    all_slots = [slot.isoformat().replace("+00:00", "Z") for slot in slots(config)]
    statuses = {slot: state["ticks"][slot]["status"] for slot in all_slots}
    assert statuses[all_slots[0]] == "SKIPPED_CLOSED"
    assert statuses[all_slots[1]] == "SKIPPED_CLOSED"
    assert statuses[all_slots[2]] == "MISSED"
    assert statuses[all_slots[3]] == "MISSED"
    assert state["ticks"][all_slots[2]]["reason"] == "elapsed_during_downtime"
    # only the remaining future slots were invoked on resume
    assert [slot for slot, _observed in resumed] == slots(config)[4:]
    assert state["terminal"]["status"] == "COMPLETE"
    assert state["terminal"]["counts"]["MISSED"] == 2


def test_relaunch_after_complete_is_inert(tmp_path):
    config = make_config(tmp_path)
    fake = FakeTime(START)
    invoked, emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits),
        )
        == 0
    )
    before = read_state(config)
    again, again_emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=again, emits=again_emits),
        )
        == 0
    )
    assert again == []  # no new tick, provider zero
    assert again_emits == []  # no second START or FINISH
    assert read_state(config) == before


def test_tick_exception_is_recorded_and_the_campaign_continues(tmp_path):
    config = make_config(tmp_path)
    fake = FakeTime(START)
    invoked, emits = [], []
    deps = make_deps(fake, invoked=invoked, emits=emits)
    calls = {"n": 0}

    def flaky_invoke(slot, observed):
        calls["n"] += 1
        if calls["n"] == 2:
            fake.now += timedelta(seconds=5)
            raise RuntimeError("transient tick failure")
        return deps.invoke_tick(slot, observed)

    assert run_48h_shadow(config, replace(deps, invoke_tick=flaky_invoke)) == 0
    state = read_state(config)
    failed = state["ticks"]["2026-08-01T21:35:00Z"]
    assert failed["status"] == "HARNESS_ERROR"
    assert failed["reason"] == "exception_RuntimeError"
    assert "transient tick failure" not in json.dumps(state)
    fatal = [entry for entry in emits if entry[0] == "FATAL"]
    assert fatal == [("FATAL", "invoke_RuntimeError")]
    assert state["terminal"]["counts"]["HARNESS_ERROR"] == 1


# -- preflight fail-closed gates --------------------------------------------


def preflight_case(tmp_path, capsys, token, **deps_overrides):
    config = deps_overrides.pop("config", None) or make_config(tmp_path)
    fake = FakeTime(START - timedelta(seconds=60))
    invoked, emits = [], []
    deps = make_deps(fake, invoked=invoked, emits=emits)
    for name, value in deps_overrides.items():
        deps = replace(deps, **{name: value})
    exit_code = run_48h_shadow(config, deps)
    assert exit_code == 1
    assert invoked == []  # provider zero
    assert not (config.shadow_root / "harness" / "state.json").exists()
    assert f"refusal={token}" in capsys.readouterr().err
    return emits


def test_head_commit_mismatch_fails_closed(tmp_path, capsys):
    preflight_case(
        tmp_path,
        capsys,
        "commit_mismatch",
        git_head=lambda: "f" * 40,
    )


def test_absent_full_suite_marker_fails_closed(tmp_path, capsys):
    fake = FakeTime(START)
    config = make_config(tmp_path)
    invoked, emits = [], []
    deps = make_deps(
        fake,
        invoked=invoked,
        emits=emits,
        marker_error="full_suite",
    )
    assert run_48h_shadow(config, deps) == 1
    assert invoked == []
    assert "refusal=marker_full_suite_invalid" in capsys.readouterr().err


def test_absent_review_marker_fails_closed(tmp_path, capsys):
    fake = FakeTime(START)
    config = make_config(tmp_path)
    invoked, emits = [], []
    deps = make_deps(
        fake,
        invoked=invoked,
        emits=emits,
        marker_error="review",
    )
    assert run_48h_shadow(config, deps) == 1
    assert "refusal=marker_independent_review_invalid" in capsys.readouterr().err


def test_missing_credentials_fail_closed(tmp_path, capsys):
    preflight_case(
        tmp_path,
        capsys,
        "credentials_unavailable",
        credentials_check=lambda: False,
    )


def test_order_write_capability_fails_closed(tmp_path, capsys):
    preflight_case(
        tmp_path,
        capsys,
        "order_write_capability_enabled",
        environ={"TOKMO_ENABLE_ORDER_WRITES": "1"},
    )


def test_grossly_wrong_clock_fails_closed(tmp_path, capsys):
    config = make_config(tmp_path)
    fake = FakeTime(START - timedelta(days=40))
    invoked, emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits),
        )
        == 1
    )
    assert invoked == []
    assert "refusal=clock_out_of_range" in capsys.readouterr().err


def test_production_canonical_root_fails_closed(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    config = make_config(
        tmp_path,
        shadow_root=tmp_path / ".local" / "share" / "tokmo-v3",
    )
    fake = FakeTime(START)
    invoked, emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits),
        )
        == 1
    )
    assert "refusal=unsafe_shadow_root" in capsys.readouterr().err


def test_duplicate_launcher_fails_closed_with_provider_zero(tmp_path, capsys):
    config = make_config(tmp_path)
    harness = config.shadow_root / "harness"
    harness.mkdir(mode=0o700, parents=True)
    descriptor = os.open(harness / "runner.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        fake = FakeTime(START)
        invoked, emits = [], []
        assert (
            run_48h_shadow(
                config,
                make_deps(fake, invoked=invoked, emits=emits),
            )
            == 1
        )
        assert invoked == []
        assert "refusal=duplicate_instance" in capsys.readouterr().err
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_state_config_mismatch_refuses_resume(tmp_path, capsys):
    config = make_config(tmp_path)
    fake = FakeTime(START)
    invoked, emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits),
        )
        == 0
    )
    other = make_config(tmp_path, expected_commit="a" * 40)
    fresh, fresh_emits = [], []
    deps = make_deps(fake, invoked=fresh, emits=fresh_emits, head="a" * 40)
    assert run_48h_shadow(other, deps) == 1
    assert fresh == []
    assert "refusal=state_config_mismatch" in capsys.readouterr().err


def test_symlinked_state_fails_closed(tmp_path, capsys):
    config = make_config(tmp_path)
    harness = config.shadow_root / "harness"
    harness.mkdir(mode=0o700, parents=True)
    victim = tmp_path / "victim.json"
    victim.write_text("{}")
    (harness / "state.json").symlink_to(victim)
    fake = FakeTime(START)
    invoked, emits = [], []
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits),
        )
        == 1
    )
    assert "refusal=state_unsafe" in capsys.readouterr().err
    assert victim.read_text() == "{}"


# -- durable state hygiene --------------------------------------------------


def test_state_is_private_and_never_contains_secrets(tmp_path):
    config = make_config(tmp_path)
    fake = FakeTime(START)
    invoked, emits = [], []
    environ = {
        "TELEGRAM_STOCK_BOT_TOKEN": "123:telegram-secret",
        "TOSS_CLIENT_SECRET": "toss-secret",
    }
    assert (
        run_48h_shadow(
            config,
            make_deps(fake, invoked=invoked, emits=emits, environ=environ),
        )
        == 0
    )
    state_path = config.shadow_root / "harness" / "state.json"
    assert oct(state_path.stat().st_mode & 0o777) == "0o600"
    text = state_path.read_text()
    assert "SECRET-BEARER-VALUE" not in text  # summary keys are allowlisted
    assert "telegram-secret" not in text
    assert "toss-secret" not in text
