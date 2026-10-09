"""Stage 4 C: repo-managed launchd deployment artifacts for the 48h shadow.

The installer generates the wrapper, LaunchAgent plist, and runner config
from repository truth for the user agent label
``com.woongs.tokmo-v3-48h-shadow`` under ``~/.hermes/runtime/tokmo-v3/
48h-shadow`` and ``~/Library/LaunchAgents``. Tests exercise only dry-run
and installs into a temporary HOME — never launchctl, never the real
LaunchAgents directory. The agent runs at load, keeps alive only on
crash/nonzero exit, and the runner itself waits until the configured
2026-08-02T06:30:00+09:00 start. Install is idempotent, refuses to touch
an unrelated plist, and uninstall removes only the exact owned artifacts.
"""

import json
import os
import plistlib
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tokmo.records.prelaunch_markers import create_pass_marker
from tools.install_48h_shadow_agent import (
    AGENT_LABEL,
    START_AT_LOCAL,
    doctor,
    generate_config,
    generate_plist,
    generate_wrapper,
    install,
    main,
    resolve_paths,
    uninstall,
)

COMMIT = "55b65f63359f0d3a97920ea794b9d5b60bc8003e"


def paths_for(tmp_path, project=None):
    return resolve_paths(
        home=tmp_path / "home",
        project_root=project or (tmp_path / "project"),
    )


def make_fake_project(tmp_path):
    """A self-contained fake project root: git identity, venv python,
    credential names, and both PASS markers for HEAD."""
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    env = {
        "PATH": "/usr/bin:/bin",
        "HOME": str(tmp_path),
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    run = lambda *args: subprocess.run(
        ["git", "-C", str(project), *args],
        env=env,
        check=True,
        capture_output=True,
    )
    run("init", "-q")
    (project / "README.md").write_text("fixture project\n")
    run("add", "README.md")
    run("commit", "-qm", "fixture")
    head = subprocess.run(
        ["git", "-C", str(project), "rev-parse", "HEAD"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    python = project / ".venv" / "bin" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/zsh\nexit 0\n")
    python.chmod(0o755)
    (project / ".env.toss").write_text(
        "TOSS_CLIENT_ID=x\nTOSS_CLIENT_SECRET=y\n",
    )
    created = datetime(2026, 8, 1, tzinfo=UTC)
    evidence = project / ".hermes" / "evidence" / "stage4"
    create_pass_marker(
        evidence / "full_suite_pass.json",
        kind="full-suite",
        commit_sha=head,
        command="pytest -q",
        test_count=2500,
        created_at=created,
        provenance="hermes/full-suite",
    )
    create_pass_marker(
        evidence / "independent_review_pass.json",
        kind="pilot-acceptance",
        commit_sha=head,
        command="direct glm reclosure",
        test_count=1,
        created_at=created,
        provenance="direct-zai/glm-5.2",
    )
    return project, head


# -- artifact generation ----------------------------------------------------


def test_paths_are_rooted_in_hermes_runtime_and_launch_agents(tmp_path):
    paths = paths_for(tmp_path)
    home = tmp_path / "home"
    runtime = home / ".hermes" / "runtime" / "tokmo-v3" / "48h-shadow"
    assert paths.runtime_root == runtime
    assert paths.wrapper == runtime / "run_48h_shadow.zsh"
    assert paths.config == runtime / "config.json"
    assert paths.shadow_root == runtime / "shadow"
    assert paths.plist == (home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist")
    assert paths.venv_python == (tmp_path / "project" / ".venv" / "bin" / "python")
    # prelaunch markers live in the repository, not the runtime root
    assert paths.full_suite_marker == (
        tmp_path
        / "project"
        / ".hermes"
        / "evidence"
        / "stage4"
        / "full_suite_pass.json"
    )


def test_plist_is_run_at_load_keep_alive_on_crash_only(tmp_path):
    paths = paths_for(tmp_path)
    plist = plistlib.loads(generate_plist(paths).encode())
    assert plist["Label"] == "com.woongs.tokmo-v3-48h-shadow"
    assert plist["ProgramArguments"] == [str(paths.wrapper)]
    assert plist["RunAtLoad"] is True
    # crash/nonzero relaunches; successful completion stays inert
    assert plist["KeepAlive"] == {"SuccessfulExit": False}
    assert plist["StandardOutPath"] == str(paths.stdout_log)
    assert plist["StandardErrorPath"] == str(paths.stderr_log)
    assert Path(plist["StandardOutPath"]).is_absolute()
    # minimal environment only
    assert set(plist["EnvironmentVariables"]) == {"PATH", "HOME"}
    assert plist["EnvironmentVariables"]["PATH"] == "/usr/bin:/bin"


def test_wrapper_sources_zshrc_silently_and_execs_exact_venv_python(tmp_path):
    paths = paths_for(tmp_path)
    wrapper = generate_wrapper(paths)
    assert wrapper.startswith("#!/bin/zsh\n")
    assert 'source "$HOME/.zshrc" >/dev/null 2>&1' in wrapper
    assert f'exec "{paths.venv_python}"' in wrapper
    assert str(paths.project_root / "tools" / "run_48h_shadow.py") in wrapper
    assert f'--config "{paths.config}"' in wrapper
    # bounded logs: the wrapper rotates oversized logs before exec
    assert "LOG_MAX_BYTES" in wrapper
    assert f'"{paths.stdout_log}"' in wrapper


def test_config_pins_the_kst_start_and_48h_campaign(tmp_path):
    paths = paths_for(tmp_path)
    config = generate_config(paths, expected_commit=COMMIT)
    assert config["start_at"] == START_AT_LOCAL == "2026-08-02T06:30:00+09:00"
    start = datetime.fromisoformat(config["start_at"])
    assert int(start.timestamp()) % 300 == 0  # aligned to the cadence grid
    assert config["duration_seconds"] == 172800
    assert config["interval_seconds"] == 300
    assert config["mode"] == "live-shadow"
    assert config["expected_commit"] == COMMIT
    assert config["shadow_root"] == str(paths.shadow_root)
    assert config["full_suite_marker"] == str(paths.full_suite_marker)
    assert config["review_marker"] == str(paths.review_marker)
    assert config["lifecycle"] == "telegram"
    assert "fixture" not in config  # live shadow, not a fixture dry-run


# -- dry run and install (temp HOME only; launchctl never runs) -------------


def test_dry_run_writes_nothing(tmp_path, capsys):
    project, _head = make_fake_project(tmp_path)
    assert (
        main(
            [
                "--dry-run",
                "--home",
                str(tmp_path / "home"),
                "--project-root",
                str(project),
                "--expected-commit",
                COMMIT,
            ]
        )
        == 0
    )
    output = capsys.readouterr().out
    assert AGENT_LABEL in output
    assert "run_48h_shadow.zsh" in output
    assert not (tmp_path / "home").exists()


def test_install_is_idempotent_private_and_owned(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    actions = install(paths, expected_commit=head, dry_run=False)
    assert actions  # every artifact reported
    assert paths.wrapper.is_file()
    assert os.access(paths.wrapper, os.X_OK)
    assert oct(paths.config.stat().st_mode & 0o777) == "0o600"
    assert oct(paths.runtime_root.stat().st_mode & 0o777) == "0o700"
    assert paths.plist.is_file()
    plist = plistlib.loads(paths.plist.read_bytes())
    assert plist["Label"] == AGENT_LABEL
    config = json.loads(paths.config.read_text())
    assert config["expected_commit"] == head
    # idempotent: a second install converges to the same artifacts
    install(paths, expected_commit=head, dry_run=False)
    assert json.loads(paths.config.read_text()) == config


def test_install_never_overwrites_an_unrelated_plist(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    paths.plist.parent.mkdir(parents=True)
    foreign = plistlib.dumps({"Label": "com.example.other"})
    paths.plist.write_bytes(foreign)
    with pytest.raises(ValueError):
        install(paths, expected_commit=head, dry_run=False)
    assert paths.plist.read_bytes() == foreign


def test_uninstall_removes_only_owned_artifacts(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    install(paths, expected_commit=head, dry_run=False)
    unrelated = paths.plist.parent / "com.example.other.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": "com.example.other"}))
    uninstall(paths)
    assert not paths.plist.exists()
    assert not paths.wrapper.exists()
    assert not paths.config.exists()
    assert unrelated.exists()
    # shadow evidence is preserved
    assert paths.shadow_root.parent.exists()


def test_uninstall_refuses_a_foreign_plist_at_the_owned_path(tmp_path):
    project, _head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    paths.plist.parent.mkdir(parents=True)
    foreign = plistlib.dumps({"Label": "com.example.other"})
    paths.plist.write_bytes(foreign)
    with pytest.raises(ValueError):
        uninstall(paths)
    assert paths.plist.read_bytes() == foreign


# -- doctor -----------------------------------------------------------------


def test_doctor_passes_on_a_healthy_offline_install(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    install(paths, expected_commit=head, dry_run=False)
    checks = {entry["check"]: entry for entry in doctor(paths, offline=True)}
    failed = [name for name, entry in checks.items() if entry["status"] == "fail"]
    assert failed == [], failed
    assert checks["plist_lint"]["status"] == "ok"
    assert checks["plist_label"]["status"] == "ok"
    assert checks["wrapper_executable"]["status"] == "ok"
    assert checks["expected_commit_matches_head"]["status"] == "ok"
    assert checks["marker_full_suite"]["status"] == "ok"
    assert checks["marker_independent_review"]["status"] == "ok"
    assert checks["credentials_names_present"]["status"] == "ok"
    assert checks["start_end_timestamps"]["status"] == "ok"
    assert checks["no_running_duplicate"]["status"] == "ok"
    assert checks["launchctl_status"]["status"] == "skipped"  # offline
    # names only: no credential value in any doctor detail
    assert "TOSS_CLIENT_ID=x" not in json.dumps(list(checks.values()))


def test_doctor_reports_commit_and_marker_failures(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    install(paths, expected_commit="c" * 40, dry_run=False)
    checks = {entry["check"]: entry for entry in doctor(paths, offline=True)}
    assert checks["expected_commit_matches_head"]["status"] == "fail"
    assert checks["marker_full_suite"]["status"] == "fail"


def test_doctor_verifies_artifact_ownership(tmp_path, monkeypatch):
    import tools.install_48h_shadow_agent as installer

    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    install(paths, expected_commit=head, dry_run=False)
    checks = {entry["check"]: entry for entry in doctor(paths, offline=True)}
    assert checks["artifacts_owner"]["status"] == "ok"
    monkeypatch.setattr(installer, "_current_uid", lambda: os.getuid() + 1)
    checks = {entry["check"]: entry for entry in doctor(paths, offline=True)}
    assert checks["artifacts_owner"]["status"] == "fail"


def test_doctor_fails_on_missing_wrapper(tmp_path):
    project, head = make_fake_project(tmp_path)
    paths = paths_for(tmp_path, project)
    install(paths, expected_commit=head, dry_run=False)
    paths.wrapper.unlink()
    checks = {entry["check"]: entry for entry in doctor(paths, offline=True)}
    assert checks["wrapper_executable"]["status"] == "fail"


def test_cli_requires_exactly_one_action(tmp_path):
    assert main(["--home", str(tmp_path)]) == 1
    assert (
        main(
            [
                "--dry-run",
                "--uninstall",
                "--home",
                str(tmp_path),
            ]
        )
        == 1
    )
