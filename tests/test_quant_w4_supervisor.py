from __future__ import annotations

import ast
import hashlib
import json
import os
import plistlib
import re
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.calendar_lookahead import (
    CalendarLookaheadStoreV1,
    LookaheadDayV1,
    LookaheadSessionV1,
)
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.ops_kernel import CalendarStartReceiptV1
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import CalendarSession, UsMarketDay
from tools import quant_w4_supervisor as subject

FILES = subject.FILES
MARKET = {name: ("market:" + name).encode() for name in FILES}
QUANT = {name: ("quant:" + name).encode() for name in FILES}
PRISTINE_CADENCE_SCRIPT = subject.CADENCE_SCRIPT
PRISTINE_HERMES_JOBS = subject.HERMES_JOBS
# Every calendar value in this suite is derived from the one-shot plan so a
# rearm is a single PLAN edit; the two anchor tests below pin its exact bytes.
SESSION = subject.PLAN.completed_session_date
CLOSE = subject.PLAN.official_close_utc
SLOTS = subject.PLAN.slots
RUN_AT = subject.PLAN.run_at_utc


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def test_one_shot_plan_derives_all_dates_and_slots():
    assert subject.PLAN.target_kst_date == date(2026, 8, 22)
    assert subject.PLAN.completed_session_date == date(2026, 8, 21)
    assert subject.PLAN.official_close_utc == datetime(2026, 8, 21, 20, 0, tzinfo=UTC)
    assert subject.PLAN.run_at_utc == datetime(2026, 8, 21, 20, 2, tzinfo=UTC)
    assert subject.PLAN.slots == (
        datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
        datetime(2026, 8, 21, 20, 5, tzinfo=UTC),
        datetime(2026, 8, 21, 20, 15, tzinfo=UTC),
        datetime(2026, 8, 21, 20, 25, tzinfo=UTC),
    )
    assert subject.PLAN.prior_cutoff_utc == datetime(2026, 8, 21, 20, 4, tzinfo=UTC)


def test_plan_refuses_an_internally_inconsistent_rearm():
    with pytest.raises(subject.Refusal, match="one-shot plan"):  # stale KST day
        subject.RunPlan(
            target_kst_date=date(2026, 8, 21),
            completed_session_date=date(2026, 8, 21),
            official_close_utc=datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
        )
    with pytest.raises(subject.Refusal, match="one-shot plan"):  # stale session
        subject.RunPlan(
            target_kst_date=date(2026, 8, 22),
            completed_session_date=date(2026, 8, 20),
            official_close_utc=datetime(2026, 8, 21, 20, 0, tzinfo=UTC),
        )
    with pytest.raises(subject.Refusal, match="one-shot plan"):  # naive close
        subject.RunPlan(
            target_kst_date=date(2026, 8, 22),
            completed_session_date=date(2026, 8, 21),
            official_close_utc=datetime(2026, 8, 21, 20, 0),
        )


def test_no_schedule_literal_survives_outside_the_one_shot_plan():
    supervisor = Path(subject.__file__).read_text()
    tree = ast.parse(supervisor)
    plan = next(
        node
        for node in tree.body
        if isinstance(node, ast.Assign)
        and any(getattr(target, "id", None) == "PLAN" for target in node.targets)
    )
    assert [
        ast.unparse(node)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"date", "datetime"}
        and not plan.lineno <= node.lineno <= plan.end_lineno
    ] == []
    assert re.search(r"\d{4}-\d{2}-\d{2}", supervisor) is None
    assert re.search(r'"(Year|Month|Day)":\s*\d', supervisor) is None
    suite = Path(__file__).read_text()
    anchored = set()
    for node in ast.parse(suite).body:
        if isinstance(node, ast.FunctionDef) and node.name in (
            "test_one_shot_plan_derives_all_dates_and_slots",
            "test_plan_refuses_an_internally_inconsistent_rearm",
            "test_plist_is_exact_one_shot_and_installer_never_loads",
        ):
            anchored.update(range(node.lineno, node.end_lineno + 1))
    assert [
        (number, line)
        for number, line in enumerate(suite.splitlines(), 1)
        if number not in anchored
        and re.search(r"\d{4}-\d{2}-\d{2}|\b\d{4}, \d{1,2}, \d{1,2}", line)
    ] == []


@pytest.fixture(autouse=True)
def restore_module_globals():
    """write_arm and legacy in-test rebinding mutate subject.CADENCE_SCRIPT /
    subject.HERMES_JOBS; restore the pristine module values after every test
    so no test ordering can leak a previous tmp_path into load_and_validate."""
    yield
    subject.CADENCE_SCRIPT = PRISTINE_CADENCE_SCRIPT
    subject.HERMES_JOBS = PRISTINE_HERMES_JOBS


def write_arm(tmp_path, **changes):
    tmp_path.mkdir(parents=True, exist_ok=True)
    repo = tmp_path / "repo"
    root = tmp_path / "runtime"
    repo.mkdir()
    subject.CADENCE_SCRIPT = (
        repo / "projects/tokmo-v3/ops/hermes-cron/tokmo_v3_cadence.sh"
    )
    subject.CADENCE_SCRIPT.parent.mkdir(parents=True)
    subject.CADENCE_SCRIPT.write_text("#!/bin/sh\n")
    jobs = tmp_path / "hermes/cron/jobs.json"
    jobs.parent.mkdir(parents=True)
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": subject.JOB_ID,
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "*/5 * * * *",
                            "display": "*/5 * * * *",
                        },
                        "no_agent": True,
                        "script": subject.CADENCE_SCRIPT.name,
                        "workdir": str(repo / "projects/tokmo-v3"),
                        "deliver": "local",
                    }
                ]
            }
        )
    )
    subject.HERMES_JOBS = jobs
    (root / "runtime").mkdir(parents=True)
    (root / "ops").mkdir()
    for path in (
        root / "ops/cadence_ticks.sqlite3",
        root / "calendar_cache.json",
        root / "ops/regular_session_history.sqlite3",
        root / "ops/calendar_lookahead.sqlite3",
        root / "ops/ops_kernel.sqlite3",
    ):
        path.touch()
    for name, raw in MARKET.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(raw)
    values = {
        "schema": 1,
        "repo_root": str(repo.resolve()),
        "runtime_root": str(root.resolve()),
        "cadence_lock": str((root / "runtime/cadence.lock").resolve()),
        "receipt_db": str((root / "ops/cadence_ticks.sqlite3").resolve()),
        "quant_db": str((root / "ops/quant_recurring.sqlite3").resolve()),
        "ops_db": str((root / "ops/ops_kernel.sqlite3").resolve()),
        "calendar_cache": str((root / "calendar_cache.json").resolve()),
        "history_db": str((root / "ops/regular_session_history.sqlite3").resolve()),
        "lookahead_db": str((root / "ops/calendar_lookahead.sqlite3").resolve()),
        "jobs_store": str(jobs.resolve()),
        "job_id": subject.JOB_ID,
        "base_commit": subject.BASE_COMMIT,
        "market_commit": subject.MARKET_COMMIT,
        "quant_commit": subject.QUANT_COMMIT,
        "python": sys.executable,
        "files": [
            {
                "path": name,
                "market_sha256": digest(MARKET[name]),
                "quant_sha256": digest(QUANT[name]),
            }
            for name in FILES
        ],
    }
    values.update(changes)
    arm = tmp_path / "arm.json"
    arm.write_text(json.dumps(values))
    arm.chmod(0o600)
    return arm, repo, root


def fake_git(repo, market=MARKET, quant=QUANT):
    blobs = {(subject.MARKET_COMMIT, key): value for key, value in market.items()}
    blobs.update({(subject.QUANT_COMMIT, key): value for key, value in quant.items()})

    def run(args):
        if args[:2] == ["rev-parse", "HEAD"]:
            return b"deployment-descendant-commit\n"
        if args[:2] == ["status", "--porcelain"]:
            return b""
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return str(repo).encode() + b"\n"
        if args[:2] == ["merge-base", "--is-ancestor"]:
            return b""
        if args[:2] == ["cat-file", "-e"]:
            return b""
        if args[0] == "cat-file":
            return blobs[(args[2].split(":", 1)[0], args[2].split(":", 1)[1])]
        raise AssertionError(args)

    return run


def test_config_mutation_matrix_and_dirty_targets_refuse(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    assert config.repo == repo
    assert config.ops_db.name == "ops_kernel.sqlite3"
    mutations = {
        "repo_root": str(tmp_path / "wrong"),
        "job_id": "wrong",
        "base_commit": "0" * 40,
        "market_commit": "1" * 40,
        "quant_commit": "2" * 40,
        "python": "/bin/false",
        "receipt_db": str(tmp_path / "wrong.sqlite3"),
        "ops_db": str(tmp_path / "wrong-ops.sqlite3"),
        "quant_db": str(tmp_path / "wrong-quant.sqlite3"),
        "calendar_cache": str(tmp_path / "wrong-calendar.json"),
        "history_db": str(tmp_path / "wrong-history.sqlite3"),
        "lookahead_db": str(tmp_path / "wrong-lookahead.sqlite3"),
        "jobs_store": str(tmp_path / "wrong-jobs.json"),
    }
    for field, value in mutations.items():
        bad, _, _ = write_arm(tmp_path / field, **{field: value})
        with pytest.raises(subject.Refusal):
            subject.load_and_validate(bad)
    subject.HERMES_JOBS = Path(json.loads(arm.read_text())["jobs_store"])
    subject.CADENCE_SCRIPT = (
        repo / "projects/tokmo-v3/ops/hermes-cron/tokmo_v3_cadence.sh"
    )
    values = json.loads(arm.read_text())
    values["files"][1]["market_sha256"] = "0" * 64
    arm.write_text(json.dumps(values))
    with pytest.raises(subject.Refusal, match="target bytes"):
        subject.load_and_validate(arm)
    values["files"][1]["market_sha256"] = digest(MARKET[FILES[1]])
    arm.write_text(json.dumps(values))
    (repo / FILES[0]).write_bytes(b"dirty")
    with pytest.raises(subject.Refusal, match="target bytes"):
        subject.load_and_validate(arm)


def test_arm_mode_symlink_and_missing_paths_fail_closed(tmp_path, monkeypatch):
    arm, repo, root = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    arm.chmod(0o644)
    with pytest.raises(subject.Refusal, match="0600"):
        subject.load_and_validate(arm)
    arm.chmod(0o600)
    arm.unlink()
    arm.symlink_to(tmp_path / "missing")
    with pytest.raises(subject.Refusal, match="arm config"):
        subject.load_and_validate(arm)
    arm, missing_repo, _ = write_arm(tmp_path / "missing")
    monkeypatch.setattr(subject, "REPO", missing_repo)
    monkeypatch.setattr(subject, "git", fake_git(missing_repo))
    Path(json.loads(arm.read_text())["cadence_lock"]).parent.rmdir()
    with pytest.raises(subject.Refusal, match="runtime path"):
        subject.load_and_validate(arm)


def test_transition_holds_exact_cadence_lock_and_recovery_after_each_file(
    tmp_path, monkeypatch
):
    for crash_after in (1, 2, 3):
        arm, repo, root = write_arm(tmp_path / str(crash_after))
        monkeypatch.setattr(subject, "REPO", repo)
        monkeypatch.setattr(subject, "git", fake_git(repo))
        config = subject.load_and_validate(arm)
        with pytest.raises(subject.SimulatedCrash):
            with subject.acquire(config.supervisor_lock):
                with subject.acquire(config.cadence_lock) as lease:
                    with pytest.raises(subject.Refusal):
                        with subject.acquire(config.cadence_lock):
                            pass
                    subject.transition(
                        config, subject.QUANT_COMMIT, lease, crash_after=crash_after
                    )
        fresh = subject.load_and_validate(arm, recovery=True)
        assert subject.recover(fresh) == "restored"
        assert {(repo / p).read_bytes() for p in FILES} == set(MARKET.values())
        journal = json.loads(config.journal.read_text())
        assert journal["state"] == "MARKET_RESTORED"
        assert len(journal["files"]) == 3
        with subject.acquire(config.cadence_lock):
            assert subject.recover(fresh) == "already_restored"


def test_recovery_refuses_while_cadence_lease_unavailable(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    with pytest.raises(subject.SimulatedCrash):
        with subject.acquire(config.cadence_lock) as lease:
            subject.transition(config, subject.QUANT_COMMIT, lease, crash_after=1)
    with subject.acquire(config.cadence_lock):
        with pytest.raises(subject.Refusal, match="cadence lease"):
            subject.recover(config)


def test_recovery_refuses_while_primary_supervisor_lock_is_held(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    config.journal.write_text('{"schema":1,"state":"TRANSITIONING","files":[]}')
    with subject.acquire(config.supervisor_lock):
        with pytest.raises(subject.Refusal, match="lock unavailable"):
            subject.recover(config)


def test_corrupt_journal_recovers_from_digest_checked_market_commit(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    with pytest.raises(subject.SimulatedCrash):
        with subject.acquire(config.cadence_lock) as lease:
            subject.transition(config, subject.QUANT_COMMIT, lease, crash_after=2)
    config.journal.write_bytes(b"{corrupt")
    assert subject.recover(config) == "restored"
    assert all((repo / name).read_bytes() == MARKET[name] for name in FILES)


def test_sigkill_equivalent_fresh_process_recovery_bundle_restores(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    bundle = subject.install_bundle(
        arm.resolve(),
        (tmp_path / "agents").resolve(),
        Path(sys.executable),
        Path(subject.__file__).resolve(),
    )
    assert [
        plistlib.loads(path.read_bytes())["ProgramArguments"][2] for path in bundle
    ] == ["run", "recover", "recover"]
    child = os.fork()
    if child == 0:
        count = {"value": 0}

        def kill_after_second():
            count["value"] += 1
            if count["value"] == 2:
                os._exit(9)

        with subject.acquire(config.supervisor_lock):
            with subject.acquire(config.cadence_lock) as lease:
                subject.transition(
                    config, subject.QUANT_COMMIT, lease, observer=kill_after_second
                )
    _, status = os.waitpid(child, 0)
    assert os.waitstatus_to_exitcode(status) == 9
    fresh = subject.load_and_validate(arm, recovery=True)
    assert subject.recover(fresh) == "restored"
    assert all((repo / name).read_bytes() == MARKET[name] for name in FILES)


def test_canonical_admission_calls_existing_authorities(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    calls = []
    monkeypatch.setattr(
        subject, "canonical_session", lambda cfg, now: calls.append(now) or SESSION
    )
    identity = subject.preflight_session(config, RUN_AT)
    assert calls and identity.completed_session_date == SESSION
    assert (
        identity.identity_digest
        == subject.QuantDueIdentityV1(
            subject.QUANT_RECURRING_PROGRAM_ID,
            subject.QUANT_POLICY,
            subject.MarketV1.US_NYSE_NASDAQ,
            SESSION,
            subject.QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
        ).identity_digest
    )
    assert subject.membership_plan_starts(4) == 7
    assert subject.QUANT_CANDLE_MAX_PAGES == 2
    with pytest.raises(subject.Refusal, match="session"):  # wrong KST date
        subject.preflight_session(config, RUN_AT - timedelta(days=1))
    with pytest.raises(subject.Refusal, match="session"):  # wrong KST minute
        subject.preflight_session(config, RUN_AT + timedelta(minutes=1))


def test_plist_is_exact_one_shot_and_installer_never_loads(tmp_path, monkeypatch):
    arm, _, _ = write_arm(tmp_path)
    directory = tmp_path / "agents"
    calls = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: calls.append(a))
    paths = subject.install_bundle(
        arm.resolve(),
        directory.resolve(),
        Path(sys.executable),
        Path(subject.__file__).resolve(),
    )
    values = [plistlib.loads(path.read_bytes()) for path in paths]
    assert len({value["Label"] for value in values}) == 3
    assert [
        (value["ProgramArguments"][2], value["StartCalendarInterval"])
        for value in values
    ] == [
        ("run", {"Year": 2026, "Month": 8, "Day": 22, "Hour": 5, "Minute": 2}),
        ("recover", {"Year": 2026, "Month": 8, "Day": 22, "Hour": 5, "Minute": 40}),
        ("recover", {"Year": 2026, "Month": 8, "Day": 22, "Hour": 6, "Minute": 30}),
    ]
    assert all(
        value["RunAtLoad"] is False and value["KeepAlive"] is False for value in values
    )
    assert all(
        Path(item).is_absolute()
        for value in values
        for item in value["ProgramArguments"][:2]
    )
    assert not calls
    assert all("hermes" not in value["ProgramArguments"] for value in values)


def test_cli_unknown_args_and_duplicate_supervisor_lock_refuse(tmp_path):
    with pytest.raises(SystemExit):
        subject.main(["--unknown"])
    lock = tmp_path / "supervisor.lock"
    with subject.acquire(lock):
        with pytest.raises(subject.Refusal, match="lock unavailable"):
            with subject.acquire(lock):
                pass


def test_doctor_is_read_only(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    monkeypatch.setattr(
        subject, "canonical_session", lambda config, now: subject.SESSION_DATE
    )
    before = {p: (repo / p).read_bytes() for p in FILES}
    result = subject.doctor(arm)
    assert result["state"] == "ready"
    assert result["observed_head"] == "deployment-descendant-commit"
    assert before == {p: (repo / p).read_bytes() for p in FILES}
    assert not (tmp_path / "runtime/quant-w4-journal.json").exists()


def test_source_has_no_scheduler_provider_or_side_effect_imports():
    source = Path(subject.__file__).read_text()
    forbidden = (
        "pause",
        "resume",
        "cron create",
        "cadence_main(",
        "toss_client",
        "token_manager",
        "telegram",
        "order_executor",
        "requests",
        "urllib",
    )
    assert all(token not in source.lower() for token in forbidden)
    assert "_day_from_payload" not in source
    assert "CalendarLookaheadStoreV1" in source


def test_scheduler_authority_schema_and_repo_copy_refuse(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    good_git = fake_git(repo)
    monkeypatch.setattr(subject, "git", good_git)
    assert (
        subject.load_and_validate(arm).observed_head == "deployment-descendant-commit"
    )
    jobs = Path(json.loads(arm.read_text())["jobs_store"])
    for value in (
        {"jobs": []},
        {
            "jobs": [
                {
                    "id": subject.JOB_ID,
                    "enabled": False,
                    "schedule": {
                        "kind": "cron",
                        "expr": "*/5 * * * *",
                        "display": "*/5 * * * *",
                    },
                    "no_agent": True,
                    "script": subject.CADENCE_SCRIPT.name,
                    "workdir": str(repo / "projects/tokmo-v3"),
                    "deliver": "local",
                }
            ]
        },
        {"wrong": []},
    ):
        jobs.write_text(json.dumps(value))
        with pytest.raises(subject.Refusal, match="job authority"):
            subject.load_and_validate(arm)
    jobs.write_text("not-json")
    with pytest.raises(subject.Refusal, match="job authority"):
        subject.load_and_validate(arm)
    write_arm(tmp_path / "restore-globals")
    subject.HERMES_JOBS = Path(json.loads(arm.read_text())["jobs_store"])
    subject.CADENCE_SCRIPT = (
        repo / "projects/tokmo-v3/ops/hermes-cron/tokmo_v3_cadence.sh"
    )
    jobs.write_text(
        json.dumps(
            {
                "jobs": [
                    {
                        "id": subject.JOB_ID,
                        "enabled": True,
                        "schedule": {
                            "kind": "cron",
                            "expr": "*/5 * * * *",
                            "display": "*/5 * * * *",
                        },
                        "no_agent": True,
                        "script": subject.CADENCE_SCRIPT.name,
                        "workdir": str(repo / "projects/tokmo-v3"),
                        "deliver": "local",
                    }
                ]
            }
        )
    )

    def copied(args):
        if args[:2] == ["rev-parse", "--show-toplevel"]:
            return str(tmp_path / "copy").encode() + b"\n"
        return good_git(args)

    (tmp_path / "copy").mkdir()
    monkeypatch.setattr(subject, "git", copied)
    with pytest.raises(subject.Refusal, match="canonical repo"):
        subject.load_and_validate(arm)


def test_operation_whitelist_is_bound_to_canonical_closed_vocabulary():
    allowed = {name: 1 for name in subject.CADENCE_OPERATIONS}
    subject._verify_zero_effects({"wire_counts": {"operation_started": allowed}})
    with pytest.raises(subject.Refusal, match="unknown cadence operation"):
        subject._verify_zero_effects(
            {"wire_counts": {"operation_started": {"NEW_UNREVIEWED_READ": 1}}}
        )


def test_generated_plist_passes_plutil_lint(tmp_path):
    arm, _, _ = write_arm(tmp_path)
    paths = subject.install_bundle(
        arm.resolve(),
        (tmp_path / "agents").resolve(),
        Path(sys.executable),
        Path(subject.__file__).resolve(),
    )
    for plist in paths:
        result = subprocess.run(["plutil", "-lint", str(plist)], capture_output=True)
        assert result.returncode == 0, result.stderr


RECEIPT_SCHEMA = """
CREATE TABLE cadence_tick_receipts(
 policy_version TEXT, scheduled_for_utc TEXT, tick_id INTEGER, status TEXT,
 reason TEXT, observed_start TEXT, started_at TEXT, finished_at TEXT,
 lag_seconds REAL, max_lag_seconds INTEGER, mask_digest TEXT,
 open_markets_json TEXT, local_dates_json TEXT, stages_json TEXT,
 wire_counts_json TEXT, calendar_attempts_json TEXT,
 PRIMARY KEY(policy_version,scheduled_for_utc));
"""


def make_receipt_db(path):
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as database:
        database.executescript(RECEIPT_SCHEMA)


def receipt_row(policy, slot, status, *, stages=(), operations=None, groups=None):
    stamp = subject.slot_identity(slot)
    finished = None if status == "STARTED" else stamp
    counts = {
        "total_started": sum((operations or {}).values()),
        "group_started": groups or {},
        "operation_started": operations or {},
        "state_counts": {} if status == "STARTED" else {"SUCCEEDED": 1},
    }
    return (
        policy,
        stamp,
        1,
        status,
        None,
        stamp,
        stamp,
        finished,
        0.0,
        60,
        "a" * 64,
        "[]",
        "{}",
        json.dumps(list(stages)),
        json.dumps(counts),
        "[]",
    )


def insert_receipt(path, row):
    with sqlite3.connect(path) as database:
        database.execute(
            "INSERT OR REPLACE INTO cadence_tick_receipts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            row,
        )


def make_ops_db(path, rows=()):
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE cadence_wire_starts(policy_version TEXT,scheduled_for_utc TEXT,native_identity TEXT)"
        )
        database.executemany("INSERT INTO cadence_wire_starts VALUES(?,?,?)", rows)


def make_quant_db(path, identity):
    path.unlink(missing_ok=True)
    with sqlite3.connect(path) as database:
        database.execute(
            "CREATE TABLE quant_recurring_due(identity_digest TEXT,state TEXT)"
        )
        database.execute(
            "CREATE TABLE quant_recurring_result(identity_digest TEXT,membership_starts INTEGER,candle_page1_starts INTEGER,candle_page2_starts INTEGER,cohort_size INTEGER,admissible_count INTEGER)"
        )
        database.execute(
            "INSERT INTO quant_recurring_due VALUES(?,'SUCCEEDED')",
            (identity.identity_digest,),
        )
        database.execute(
            "INSERT INTO quant_recurring_result VALUES(?,7,4,0,4,4)",
            (identity.identity_digest,),
        )


def test_wait_tick_uses_production_z_identity_and_observes_started(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[1]
    assert subject.slot_identity(slot) == slot.strftime("%Y-%m-%dT%H:%M:%S.%f") + "Z"

    def scheduler():
        time.sleep(0.35)
        insert_receipt(
            config.receipt_db, receipt_row(subject.QUANT_POLICY, slot, "STARTED")
        )
        time.sleep(0.35)
        insert_receipt(
            config.receipt_db, receipt_row(subject.QUANT_POLICY, slot, "COMPLETE")
        )

    thread = threading.Thread(target=scheduler)
    thread.start()
    row = subject._wait_tick(config, subject.QUANT_POLICY, slot, time.monotonic() + 2)
    thread.join()
    assert row["status"] == "COMPLETE"


def test_prior_settled_accepts_existing_complete_but_target_does_not(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
    )
    assert (
        subject._settled_prior(config, subject.MARKET_POLICY, slot)["status"]
        == "COMPLETE"
    )
    with pytest.raises(subject.Refusal, match="already exists"):
        subject._wait_tick(config, subject.MARKET_POLICY, slot, time.monotonic() + 0.1)


def test_prior_polls_started_to_complete_and_rejects_terminal_or_bad_order(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "STARTED")
    )
    ticks = {"value": 0.0}

    def settle(unused):
        ticks["value"] += 0.25
        insert_receipt(
            config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
        )

    assert (
        subject._wait_prior(
            config,
            subject.MARKET_POLICY,
            slot,
            120.0,
            monotonic=lambda: ticks["value"],
            sleep=settle,
        )["status"]
        == "COMPLETE"
    )
    for status in ("MISSED", "FAILED"):
        insert_receipt(
            config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, status)
        )
        with pytest.raises(subject.Refusal, match="durably settled"):
            subject._settled_prior(config, subject.MARKET_POLICY, slot)
    bad = list(receipt_row(subject.MARKET_POLICY, slot, "COMPLETE"))
    bad[6] = subject.slot_identity(slot + timedelta(seconds=2))
    bad[7] = subject.slot_identity(slot + timedelta(seconds=1))
    insert_receipt(config.receipt_db, tuple(bad))
    with pytest.raises(subject.Refusal, match="durably settled"):
        subject._settled_prior(config, subject.MARKET_POLICY, slot)


def test_target_rejects_late_arrival_and_missed_without_started(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[1]
    step = {"value": 0}

    def late(unused):
        step["value"] += 1
        row = list(
            receipt_row(
                subject.QUANT_POLICY,
                slot,
                "STARTED" if step["value"] == 1 else "COMPLETE",
            )
        )
        row[5] = subject.slot_identity(slot + timedelta(seconds=91))
        insert_receipt(config.receipt_db, tuple(row))

    with pytest.raises(subject.Refusal, match="lifecycle"):
        subject._wait_tick(
            config,
            subject.QUANT_POLICY,
            slot,
            10,
            monotonic=lambda: float(step["value"]),
            sleep=late,
        )
    make_receipt_db(config.receipt_db)
    missed = {"value": 0}

    def publish_missed(unused):
        missed["value"] += 1
        insert_receipt(
            config.receipt_db, receipt_row(subject.QUANT_POLICY, slot, "MISSED")
        )

    with pytest.raises(subject.Refusal, match="lifecycle"):
        subject._wait_tick(
            config,
            subject.QUANT_POLICY,
            slot,
            10,
            monotonic=lambda: float(missed["value"]),
            sleep=publish_missed,
        )


def _session_fixtures(
    config,
    now,
    *,
    trade_date=SESSION,
    history="regular",
    lookahead_open=True,
    close_shift=timedelta(0),
):
    config.calendar_cache.unlink(missing_ok=True)
    close = CLOSE + close_shift
    regular = CalendarSession(CLOSE - timedelta(hours=6, minutes=30), close)
    after = CalendarSession(close, CLOSE + timedelta(hours=3, minutes=50))
    CalendarCacheStore(config.calendar_cache, clock=lambda: now).write(
        None, UsMarketDay(trade_date, regular_market=regular, after_market=after)
    )
    config.history_db.unlink(missing_ok=True)
    store = RegularSessionHistoryStore(config.history_db, clock=lambda: now)
    receipt = CalendarStartReceiptV1(
        subject.MARKET_POLICY,
        SLOTS[0],
        Market.US,
        trade_date,
        0,
        f"calendar:US:{trade_date}:0",
        "STARTED",
    )
    authority = type(
        "Authority",
        (),
        {"verify_calendar_history_receipt": lambda self, value, now: None},
    )()
    if history is not None:
        store.append_backfilled(
            Market.US, trade_date, history, receipt=receipt, authority=authority
        )
    config.lookahead_db.unlink(missing_ok=True)
    if lookahead_open:
        kind, start, end = "afterMarket", regular.end_time, after.end_time
    else:
        kind, start, end = "regularMarket", regular.start_time, regular.end_time
    day = LookaheadDayV1(
        Market.US,
        trade_date.isoformat(),
        trade_date.isoformat(),
        (LookaheadSessionV1(kind, start, end),),
        now,
        f"calendar:US:{trade_date}:0",
    )
    CalendarLookaheadStoreV1(
        config.lookahead_db, clock=lambda: now
    ).replace_market_days(Market.US, (day,))


def test_real_canonical_session_authorities_and_refusals(tmp_path, monkeypatch):
    now = RUN_AT
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    _session_fixtures(config, now)
    assert subject.canonical_session(config, now) == SESSION
    cases = (
        (SESSION - timedelta(days=7), "regular", True),
        (SESSION, None, True),
        (SESSION, "no_regular", True),
        (SESSION, "regular", False),
    )
    for trade_date, history, lookahead in cases:
        _session_fixtures(
            config,
            now,
            trade_date=trade_date,
            history=history,
            lookahead_open=lookahead,
        )
        with pytest.raises(subject.Refusal):
            subject.canonical_session(config, now)
    _session_fixtures(config, now)
    with pytest.raises(subject.Refusal):
        subject.canonical_session(config, CLOSE - timedelta(minutes=1))
    _session_fixtures(config, now, close_shift=timedelta(hours=1))
    with pytest.raises(subject.Refusal):  # later-than-PLAN official close
        subject.canonical_session(config, now)
    _session_fixtures(config, now, close_shift=timedelta(hours=-1))
    with pytest.raises(subject.Refusal):  # earlier-than-PLAN official close
        subject.canonical_session(config, now)
    config.calendar_cache.unlink()
    with pytest.raises(subject.Refusal):
        subject.canonical_session(config, now)


def test_replay_rejects_quant_candles_and_each_prohibited_effect(tmp_path):
    config = type(
        "Config",
        (),
        {"ops_db": tmp_path / "ops.sqlite3", "quant_db": tmp_path / "quant.sqlite3"},
    )()
    slot = SLOTS[2]
    identity = subject.QuantDueIdentityV1(
        subject.QUANT_RECURRING_PROGRAM_ID,
        subject.QUANT_POLICY,
        subject.MarketV1.US_NYSE_NASDAQ,
        SESSION,
        subject.QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    make_quant_db(config.quant_db, identity)
    make_ops_db(config.ops_db)
    base = {
        "policy_version": subject.QUANT_POLICY,
        "wire_counts": {
            "group_started": {"MEMBERSHIP": 0},
            "operation_started": {},
            "state_counts": {},
        },
        "stages": [{"stage": "quant_membership", "reason": "already_terminal"}],
    }
    subject._validate_quant(config, identity, base, slot=slot, replay=True)
    make_ops_db(
        config.ops_db,
        (
            (
                subject.QUANT_POLICY,
                subject.slot_identity(slot),
                f"candles:qt:US:AAPL:{SESSION}:1",
            ),
        ),
    )
    with pytest.raises(subject.Refusal, match="Quant candle"):
        subject._validate_quant(config, identity, base, slot=slot, replay=True)
    make_ops_db(
        config.ops_db,
        (
            (
                subject.QUANT_POLICY,
                subject.slot_identity(slot),
                f"membership:registry:{SESSION}",
            ),
        ),
    )
    with pytest.raises(subject.Refusal, match="ALREADY_TERMINAL"):
        subject._validate_quant(config, identity, base, slot=slot, replay=True)
    make_ops_db(config.ops_db)
    for category in (
        "NEWS_WRITE",
        "MODEL_CALL",
        "DELIVERY_SEND",
        "ORDER_WRITE",
        "ACCOUNT_WRITE",
    ):
        bad = json.loads(json.dumps(base))
        bad["wire_counts"]["operation_started"][category] = 1
        with pytest.raises(subject.Refusal, match="unknown cadence operation"):
            subject._validate_quant(config, identity, bad, slot=slot, replay=True)


def test_run_observes_natural_started_lifecycles_and_policy_bytes(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    make_ops_db(config.ops_db)
    identity = subject.QuantDueIdentityV1(
        subject.QUANT_RECURRING_PROGRAM_ID,
        subject.QUANT_POLICY,
        subject.MarketV1.US_NYSE_NASDAQ,
        SESSION,
        subject.QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    monkeypatch.setattr(subject, "preflight_session", lambda config, now: identity)
    slots = list(SLOTS)
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slots[0], "COMPLETE")
    )
    events = []
    clock = {"utc": RUN_AT, "mono": 100.0, "index": 0, "started": False}
    cadence = {"active": True}

    def all_bytes(expected):
        return all((repo / name).read_bytes() == expected[name] for name in FILES)

    operations = {
        "MEMBERSHIP_NASDAQ_LISTED": 1,
        "MEMBERSHIP_OTHER_LISTED": 1,
        "MEMBERSHIP_SEC_REGISTRY": 1,
        "MEMBERSHIP_SEC_ISSUER": 4,
        "STOCK_CANDLES_DAILY": 7,
    }
    schedules = (
        (
            subject.QUANT_POLICY,
            slots[1],
            (
                {"stage": "quant_membership", "reason": "DUE"},
                {"stage": "quant_shadow", "reason": "SUCCEEDED"},
            ),
            operations,
            {"MEMBERSHIP": 7},
        ),
        (
            subject.QUANT_POLICY,
            slots[2],
            ({"stage": "quant_membership", "reason": "already_terminal"},),
            {"STOCK_CANDLES_DAILY": 3},
            {"MEMBERSHIP": 0},
        ),
        (
            subject.MARKET_POLICY,
            slots[3],
            (
                {"stage": "quant_membership", "reason": "policy_not_pinned"},
                {"stage": "quant_shadow", "reason": "policy_not_pinned"},
            ),
            {"STOCK_CANDLES_DAILY": 3},
            {"MEMBERSHIP": 0},
        ),
    )

    def slot_sleep(unused):
        assert cadence["active"] is True
        policy, slot, stages, operations_for_slot, groups = schedules[clock["index"]]
        if not clock["started"]:
            delta = (slot - clock["utc"]).total_seconds()
            assert delta >= 0
            clock["utc"] = slot
            clock["mono"] += delta
            if clock["index"] == 0:
                assert all_bytes(QUANT)
                make_quant_db(config.quant_db, identity)
                with sqlite3.connect(config.ops_db) as database:
                    database.executemany(
                        "INSERT INTO cadence_wire_starts VALUES(?,?,?)",
                        [
                            (
                                subject.QUANT_POLICY,
                                subject.slot_identity(slots[1]),
                                f"candles:qt:US:S{index}:{SESSION}:1",
                            )
                            for index in range(4)
                        ],
                    )
            if clock["index"] == 2:
                assert all_bytes(MARKET)
            publish(
                policy,
                slot,
                stages=stages,
                operations=operations_for_slot,
                groups=groups,
                terminal=False,
            )
            clock["started"] = True
        else:
            publish(
                policy,
                slot,
                stages=stages,
                operations=operations_for_slot,
                groups=groups,
                terminal=True,
            )
            clock["started"] = False
            clock["index"] += 1

    def publish(policy, slot, *, stages, operations, groups, terminal):
        status = "COMPLETE" if terminal else "STARTED"
        if not terminal:
            events.append(
                (
                    status,
                    policy,
                    clock["utc"],
                    all_bytes(QUANT if policy == subject.QUANT_POLICY else MARKET),
                )
            )
        insert_receipt(
            config.receipt_db,
            receipt_row(
                policy,
                slot,
                status,
                stages=stages,
                operations=operations,
                groups=groups,
            ),
        )

    subject.run(
        config,
        now=lambda: clock["utc"],
        monotonic=lambda: clock["mono"],
        sleep=slot_sleep,
    )
    assert [event[1] for event in events] == [
        subject.QUANT_POLICY,
        subject.QUANT_POLICY,
        subject.MARKET_POLICY,
    ]
    assert all(event[3] for event in events)
    assert [event[2] for event in events] == slots[1:]
    assert all_bytes(MARKET) and cadence["active"] is True


def test_run_restores_partial_transition_on_ordinary_exception(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
    )
    monkeypatch.setattr(subject, "preflight_session", lambda config, now: object())
    original = subject.transition
    monkeypatch.setattr(
        subject,
        "transition",
        lambda config, commit, lease: original(config, commit, lease, crash_after=1),
    )
    with pytest.raises(subject.SimulatedCrash):
        subject.run(config, now=lambda: RUN_AT)
    assert all((repo / name).read_bytes() == MARKET[name] for name in FILES)
    assert json.loads(config.journal.read_text())["state"] == "MARKET_RESTORED"


def test_run_preserves_primary_and_restore_failure_types(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
    )
    monkeypatch.setattr(subject, "preflight_session", lambda config, now: object())
    original = subject.transition
    monkeypatch.setattr(
        subject,
        "transition",
        lambda config, commit, lease: original(config, commit, lease, crash_after=1),
    )
    monkeypatch.setattr(
        subject,
        "_restore_journal",
        lambda config: (_ for _ in ()).throw(OSError("restore")),
    )
    with pytest.raises(
        subject.RunAndRecoveryFailure, match="primary=SimulatedCrash;restore=OSError"
    ):
        subject.run(config, now=lambda: RUN_AT)
    failure = json.loads(config.journal.read_text())
    assert (failure["state"], failure["primary_type"], failure["recovery_type"]) == (
        "RESTORE_FAILED",
        "SimulatedCrash",
        "OSError",
    )


def test_run_preserves_canary_and_market_readback_failures(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
    )
    monkeypatch.setattr(subject, "preflight_session", lambda config, now: object())
    monkeypatch.setattr(subject, "_wait_tick", lambda *args, **kwargs: {})
    monkeypatch.setattr(
        subject,
        "_validate_quant",
        lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("canary")),
    )
    monkeypatch.setattr(
        subject,
        "_validate_market",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("market")),
    )
    with pytest.raises(
        subject.RunAndRecoveryFailure, match="primary=ValueError;market=OSError"
    ):
        subject.run(config, now=lambda: RUN_AT)
    assert all((repo / name).read_bytes() == MARKET[name] for name in FILES)


def test_read_only_observers_refuse_absent_db_without_creation(tmp_path):
    slot = SLOTS[1]
    receipt = tmp_path / "missing-receipt.sqlite3"
    config = type("Config", (), {"receipt_db": receipt})()
    with pytest.raises(subject.Refusal, match="receipt DB absent"):
        subject._read_tick(config, subject.QUANT_POLICY, slot)
    assert not receipt.exists()
    quant = tmp_path / "missing-quant.sqlite3"
    config = type("Config", (), {"quant_db": quant})()
    with pytest.raises(subject.Refusal, match="Quant DB absent"):
        subject._read_quant(config, object())
    assert not quant.exists()


def test_canary_uses_qt_native_count_not_inflated_daily_operation(tmp_path):
    config = type(
        "Config",
        (),
        {"ops_db": tmp_path / "ops.sqlite3", "quant_db": tmp_path / "quant.sqlite3"},
    )()
    slot = SLOTS[1]
    identity = subject.QuantDueIdentityV1(
        subject.QUANT_RECURRING_PROGRAM_ID,
        subject.QUANT_POLICY,
        subject.MarketV1.US_NYSE_NASDAQ,
        subject.SESSION_DATE,
        subject.QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    make_quant_db(config.quant_db, identity)
    rows = [
        (
            subject.QUANT_POLICY,
            subject.slot_identity(slot),
            f"candles:qt:US:S{index}:{SESSION}:1",
        )
        for index in range(9)
    ]
    make_ops_db(config.ops_db, rows)
    row = {
        "policy_version": subject.QUANT_POLICY,
        "wire_counts": {
            "group_started": {"MEMBERSHIP": 7},
            "operation_started": {
                "MEMBERSHIP_NASDAQ_LISTED": 1,
                "MEMBERSHIP_OTHER_LISTED": 1,
                "MEMBERSHIP_SEC_REGISTRY": 1,
                "MEMBERSHIP_SEC_ISSUER": 4,
                "STOCK_CANDLES_DAILY": 11,
            },
            "state_counts": {},
        },
        "stages": [],
    }
    with pytest.raises(subject.Refusal, match="wire counts"):
        subject._validate_quant(config, identity, row, slot=slot)


def test_market_readback_requires_inert_quant_stages_and_zero_qt(tmp_path):
    config = type("Config", (), {"ops_db": tmp_path / "ops.sqlite3"})()
    slot = SLOTS[3]
    make_ops_db(config.ops_db)
    row = {
        "policy_version": subject.MARKET_POLICY,
        "wire_counts": {
            "group_started": {"MEMBERSHIP": 0},
            "operation_started": {},
            "state_counts": {},
        },
        "stages": [
            {"stage": "quant_membership", "reason": "policy_not_pinned"},
            {"stage": "quant_shadow", "reason": "policy_not_pinned"},
        ],
    }
    subject._validate_market(config, row, slot=slot)
    row["stages"][1]["reason"] = "SUCCEEDED"
    with pytest.raises(subject.Refusal, match="Market readback"):
        subject._validate_market(config, row, slot=slot)


def test_session_refusal_precedes_transition(tmp_path, monkeypatch):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    calls = []
    monkeypatch.setattr(
        subject,
        "preflight_session",
        lambda config, now: (_ for _ in ()).throw(subject.Refusal("session refused")),
    )
    monkeypatch.setattr(
        subject, "transition", lambda *args, **kwargs: calls.append(args)
    )
    with pytest.raises(subject.Refusal, match="session refused"):
        subject.run(config)
    assert calls == [] and not config.journal.exists()


def test_deadline_is_anchored_to_future_slot_not_call_time():
    now = RUN_AT
    replay = SLOTS[2]
    assert subject.slot_deadline(replay, now_utc=now, monotonic_now=100.0) == 1270.0


def test_replay_literal_is_bound_to_production_source():
    source = (
        Path(subject.__file__).parents[1]
        / "src/tokmo/programs/quant_recurring_shadow.py"
    ).read_text()
    assert '"skipped", "already_terminal"' in source
    assert subject.REPLAY_REASON == "already_terminal"


def test_acquire_until_retries_contended_lock_with_injected_clock(tmp_path):
    lock = tmp_path / "contended.lock"
    acquired = threading.Event()
    release = threading.Event()
    ticks = {"value": 0.0}
    sleeps = []

    def holder():
        with subject.acquire(lock):
            acquired.set()
            release.wait(10)

    def monotonic():
        return ticks["value"]

    def sleep(unused):
        sleeps.append(ticks["value"])
        if len(sleeps) == 2:
            release.set()
        ticks["value"] += 1.0

    thread = threading.Thread(target=holder)
    thread.start()
    assert acquired.wait(5)
    try:
        with subject._acquire_until(
            lock, 300.0, monotonic=monotonic, sleep=sleep
        ) as lease:
            assert isinstance(lease, int)
            assert ticks["value"] >= 2.0
    finally:
        release.set()
        thread.join(5)
    assert len(sleeps) >= 2
    assert max(sleeps) < 300.0


def test_run_restore_retry_uses_injected_clocks_under_real_contention(
    tmp_path, monkeypatch
):
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    config = subject.load_and_validate(arm)
    make_receipt_db(config.receipt_db)
    slot = SLOTS[0]
    insert_receipt(
        config.receipt_db, receipt_row(subject.MARKET_POLICY, slot, "COMPLETE")
    )
    monkeypatch.setattr(subject, "preflight_session", lambda config, now: object())
    original_transition = subject.transition
    monkeypatch.setattr(
        subject,
        "transition",
        lambda config, commit, lease: original_transition(
            config, commit, lease, crash_after=1
        ),
    )
    original_needs = subject._journal_needs_restore
    held = {}
    ticks = {"value": 0.0}
    sleeps = []

    def needs_restore(config):
        result = original_needs(config)
        # After the crash has unwound (cadence lock free again), take the lock
        # ourselves so the restore retry in run()'s finally block meets real
        # flock contention on its first _acquire_until attempt. Keep the
        # context manager referenced so GC finalization cannot release it.
        if result and "context" not in held:
            held["context"] = subject.acquire(config.cadence_lock)
            held["context"].__enter__()
        return result

    monkeypatch.setattr(subject, "_journal_needs_restore", needs_restore)

    def monotonic():
        return ticks["value"]

    def sleep(unused):
        sleeps.append(unused)
        if len(sleeps) == 1:
            held["context"].__exit__(None, None, None)
        ticks["value"] += 1.0

    with pytest.raises(subject.SimulatedCrash):
        subject.run(
            config,
            now=lambda: RUN_AT,
            monotonic=monotonic,
            sleep=sleep,
        )
    assert sleeps  # the restore retry observed injected clocks, not time.sleep
    assert all((repo / name).read_bytes() == MARKET[name] for name in FILES)
    assert json.loads(config.journal.read_text())["state"] == "MARKET_RESTORED"


def test_module_global_mutations_do_not_leak_into_later_tests(tmp_path, monkeypatch):
    # Entry guard: earlier tests mutate subject.CADENCE_SCRIPT/HERMES_JOBS via
    # write_arm; the autouse restore_module_globals fixture must have returned
    # the module to import-time state before this test started.
    assert subject.CADENCE_SCRIPT == PRISTINE_CADENCE_SCRIPT
    assert subject.HERMES_JOBS == PRISTINE_HERMES_JOBS
    arm, repo, _ = write_arm(tmp_path)
    monkeypatch.setattr(subject, "REPO", repo)
    monkeypatch.setattr(subject, "git", fake_git(repo))
    subject.load_and_validate(arm)  # validates against this test's tmp globals
    # Under restored (pristine) globals, the same arm must fail closed instead
    # of validating against paths leaked from a previous test's tmp_path.
    subject.CADENCE_SCRIPT = PRISTINE_CADENCE_SCRIPT
    subject.HERMES_JOBS = PRISTINE_HERMES_JOBS
    with pytest.raises(subject.Refusal, match="jobs store"):
        subject.load_and_validate(arm)
