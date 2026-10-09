"""Paused-first installer; it never mutates launchd."""

import argparse
import hashlib
import json
import os
import plistlib
import subprocess
from dataclasses import dataclass
from pathlib import Path

AGENT_LABEL = "com.woongs.tokmo-v3-macro-runtime"
RUNTIME_CANDIDATE_PATHS = (
    "src/tokmo/programs/macro_fred_inputs.py",
    "src/tokmo/values/fred_query.py",
    "tools/install_macro_runtime_agent.py",
    "tools/run_macro_runtime.py",
)


@dataclass(frozen=True)
class AgentPaths:
    home: Path
    project_root: Path
    runtime_root: Path
    wrapper: Path
    config: Path
    state_root: Path
    stdout_log: Path
    stderr_log: Path
    plist: Path
    venv_python: Path
    runner: Path


def resolve_paths(home=None, project_root=None):
    home = Path.home() if home is None else Path(home)
    project_root = (
        Path(__file__).resolve().parents[1]
        if project_root is None
        else Path(project_root)
    )
    runtime = (
        project_root / "var/runtime/macro-runtime"
    )  # relocated from ~/.hermes/runtime (2026-09-24)
    return AgentPaths(
        home,
        project_root,
        runtime,
        runtime / "run_macro_runtime.zsh",
        runtime / "config.json",
        (home / ".local/share/tokmo-v3").resolve() / "macro-runtime",
        runtime / "logs/stdout.log",
        runtime / "logs/stderr.log",
        home / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist",
        project_root / ".venv/bin/python",
        project_root / "tools/run_macro_runtime.py",
    )


def generate_wrapper(paths):
    return f'#!/bin/zsh\nset -u\nfor log in "{paths.stdout_log}" "{paths.stderr_log}"; do\n  if [ -f "$log" ] && [ "$(/usr/bin/stat -f%z "$log")" -gt 1048576 ]; then /bin/mv "$log" "$log.1"; fi\ndone\nexec >>"{paths.stdout_log}" 2>>"{paths.stderr_log}"\nexport PYTHONPATH="{paths.project_root / "src"}"\nexec "{paths.venv_python}" "{paths.runner}" --config "{paths.config}"\n'


def generate_config(paths, *, expected_commit, expected_candidate_sha256=""):
    return {
        "schema": "tokmo.macro.runtime-config.v1",
        "enabled": False,
        "lifecycle": "SHADOW",
        "policy": "PAUSED",
        "expected_commit": expected_commit,
        "expected_candidate_sha256": expected_candidate_sha256,
        "root": str(paths.state_root),
        "fred_enrichment": False,
        "fred_root": str(paths.home / ".local/share/tokmo-v3/fred-ondemand/v1"),
        "fred_credential": str(
            paths.project_root / ".secrets/hermes-credentials/fred.env"
        ),
    }


def generate_plist(paths):
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": [str(paths.wrapper)],
            "StartCalendarInterval": {"Hour": 16, "Minute": 10},
            "StandardOutPath": "/dev/null",
            "StandardErrorPath": "/dev/null",
            "WorkingDirectory": str(paths.project_root),
        }
    ).decode()


def validate_activation_preconditions(paths, timezone_path=Path("/etc/localtime")):
    timezone_path = Path(timezone_path)
    if not timezone_path.is_symlink():
        raise ValueError("system timezone must be canonical Asia/Seoul")
    try:
        timezone_target = timezone_path.resolve(strict=True)
    except OSError as error:
        raise ValueError("system timezone unavailable") from error
    if timezone_target.parts[-3:] != ("zoneinfo", "Asia", "Seoul"):
        raise ValueError("system timezone must be Asia/Seoul")
    for path, executable in ((paths.venv_python, True), (paths.runner, False)):
        try:
            resolved = path.resolve(strict=True)
            link_owned = path.lstat().st_uid == os.getuid()
        except OSError:
            raise ValueError("runtime executable invalid")
        if (
            (path.is_symlink() and path != paths.venv_python)
            or not resolved.is_file()
            or not link_owned
            or resolved.stat().st_uid != os.getuid()
            or (executable and not os.access(resolved, os.X_OK))
        ):
            raise ValueError("runtime executable invalid")


def install(paths, *, expected_commit, dry_run, timezone_path=Path("/etc/localtime")):
    if len(expected_commit) != 40:
        raise ValueError("expected commit invalid")
    validate_activation_preconditions(paths, timezone_path)
    try:
        scoped = subprocess.run(
            [
                "git",
                "-C",
                str(paths.project_root),
                "diff",
                "--binary",
                expected_commit,
                "--",
                *RUNTIME_CANDIDATE_PATHS,
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise ValueError("candidate identity unavailable") from error
    if scoped.returncode != 0:
        raise ValueError("expected commit mismatch")
    candidate = hashlib.sha256(scoped.stdout).hexdigest()
    artifacts = (
        (paths.wrapper, generate_wrapper(paths), 0o700),
        (
            paths.config,
            json.dumps(
                generate_config(
                    paths,
                    expected_commit=expected_commit,
                    expected_candidate_sha256=candidate,
                ),
                sort_keys=True,
            )
            + "\n",
            0o600,
        ),
        (paths.plist, generate_plist(paths), 0o600),
    )
    if dry_run:
        return [
            {"action": "would-write", "artifact": path.name} for path, _, _ in artifacts
        ]
    if paths.plist.exists() and (
        paths.plist.is_symlink()
        or plistlib.loads(paths.plist.read_bytes()).get("Label") != AGENT_LABEL
    ):
        raise ValueError("unowned plist")
    for directory in (paths.runtime_root, paths.stdout_log.parent, paths.state_root):
        if directory.is_symlink():
            raise ValueError("generated directory symlink")
        directory.mkdir(parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
    paths.plist.parent.mkdir(parents=True, exist_ok=True)
    results = []
    for log in (paths.stdout_log, paths.stderr_log):
        if log.exists() and log.stat().st_size > 1048576:
            os.replace(log, log.with_suffix(log.suffix + ".1"))
        if not log.exists():
            descriptor = os.open(log, os.O_WRONLY | os.O_CREAT, 0o600)
            os.close(descriptor)
        os.chmod(log, 0o600)
    for path, content, mode in artifacts:
        if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
            raise ValueError("generated artifact unsafe")
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", closefd=False) as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.chmod(temporary, mode)
            os.replace(temporary, path)
            parent = os.open(path.parent, os.O_RDONLY)
            os.fsync(parent)
            os.close(parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temporary.unlink(missing_ok=True)
        results.append({"action": "wrote", "artifact": path.name})
    return results


def doctor(paths, *, offline=False):
    try:
        config = json.loads(paths.config.read_text())
    except (OSError, ValueError):
        config = {}
    policy_valid = (
        config.get("enabled") is False and config.get("policy") == "PAUSED"
    ) or (config.get("enabled") is True and config.get("policy") == "SHADOW")
    checks = [{"check": "config_policy", "status": "ok" if policy_valid else "fail"}]
    expected = config.get("expected_commit")
    expected_candidate = config.get("expected_candidate_sha256")
    try:
        scoped = subprocess.run(
            [
                "git",
                "-C",
                str(paths.project_root),
                "diff",
                "--binary",
                expected,
                "--",
                *RUNTIME_CANDIDATE_PATHS,
            ],
            capture_output=True,
            check=False,
            timeout=5,
        )
        candidate_ok = (
            scoped.returncode == 0
            and isinstance(expected, str)
            and len(expected) == 40
            and hashlib.sha256(scoped.stdout).hexdigest() == expected_candidate
        )
    except (OSError, subprocess.TimeoutExpired, TypeError):
        candidate_ok = False
    checks.append(
        {"check": "runtime_candidate", "status": "ok" if candidate_ok else "fail"}
    )
    for path, mode in (
        (paths.runtime_root, 0o700),
        (paths.wrapper, 0o700),
        (paths.config, 0o600),
        (paths.plist, 0o600),
        (paths.stdout_log, 0o600),
        (paths.stderr_log, 0o600),
    ):
        safe = (
            path.exists()
            and not path.is_symlink()
            and path.stat().st_uid == os.getuid()
            and (path.stat().st_mode & 0o777) == mode
        )
        checks.append(
            {"check": f"owned_{path.name}", "status": "ok" if safe else "fail"}
        )
    try:
        label = plistlib.loads(paths.plist.read_bytes()).get("Label")
    except Exception:
        label = None
    checks.append(
        {"check": "fixed_label", "status": "ok" if label == AGENT_LABEL else "fail"}
    )
    if offline:
        checks.append({"check": "launchctl_status", "status": "skipped"})
    else:
        result = subprocess.run(
            ["/bin/launchctl", "print", f"gui/{os.getuid()}/{AGENT_LABEL}"],
            capture_output=True,
            text=True,
            check=False,
        )
        checks.append(
            {
                "check": "launchctl_status",
                "status": "ok" if result.returncode == 0 else "fail",
            }
        )
    return checks


def uninstall(paths):
    if (
        paths.plist.exists()
        and plistlib.loads(paths.plist.read_bytes()).get("Label") != AGENT_LABEL
    ):
        raise ValueError("unowned plist")
    results = []
    for path in (paths.plist, paths.wrapper, paths.config):
        if path.is_symlink() or (path.exists() and path.stat().st_uid != os.getuid()):
            raise ValueError("unowned artifact")
        if path.exists():
            path.unlink()
            results.append({"action": "removed", "artifact": path.name})
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(prog="install-macro-runtime-agent")
    for flag in (
        "dry-run",
        "install",
        "doctor",
        "uninstall",
        "enable-shadow",
        "pause",
        "repin",
    ):
        parser.add_argument("--" + flag, action="store_true")
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--home")
    parser.add_argument("--project-root")
    parser.add_argument("--expected-commit")
    try:
        args = parser.parse_args(argv)
    except SystemExit as error:
        return 0 if error.code == 0 else 1
    if (
        sum(
            (
                args.dry_run,
                args.install,
                args.doctor,
                args.uninstall,
                args.enable_shadow,
                args.pause,
                args.repin,
            )
        )
        != 1
    ):
        return 1
    paths = resolve_paths(args.home, args.project_root)
    try:
        if args.dry_run or args.install:
            install(
                paths, expected_commit=args.expected_commit or "", dry_run=args.dry_run
            )
            return 0
        if args.doctor:
            return (
                0
                if all(
                    x["status"] in ("ok", "skipped")
                    for x in doctor(paths, offline=args.offline)
                )
                else 1
            )
        if args.uninstall:
            uninstall(paths)
            return 0
        for path, mode in (
            (paths.wrapper, 0o700),
            (paths.config, 0o600),
            (paths.plist, 0o600),
        ):
            if (
                path.is_symlink()
                or not path.is_file()
                or path.stat().st_uid != os.getuid()
                or (path.stat().st_mode & 0o777) != mode
            ):
                raise ValueError("owned artifact invalid")
        if plistlib.loads(paths.plist.read_bytes()).get("Label") != AGENT_LABEL:
            raise ValueError("owned plist invalid")
        value = json.loads(paths.config.read_text())
        if args.pause:
            value.update(enabled=False, policy="PAUSED")
        else:
            if args.enable_shadow:
                validate_activation_preconditions(paths)
            expected = args.expected_commit or value.get("expected_commit")
            if not isinstance(expected, str) or len(expected) != 40:
                return 1
            try:
                scoped = subprocess.run(
                    [
                        "git",
                        "-C",
                        str(paths.project_root),
                        "diff",
                        "--binary",
                        expected,
                        "--",
                        *RUNTIME_CANDIDATE_PATHS,
                    ],
                    capture_output=True,
                    check=False,
                    timeout=5,
                )
            except (OSError, subprocess.TimeoutExpired):
                return 1
            if scoped.returncode != 0:
                return 1
            candidate = hashlib.sha256(scoped.stdout).hexdigest()
            if args.repin:
                if value.get("enabled") is not False or value.get("policy") != "PAUSED":
                    return 1
                value.update(
                    expected_commit=expected, expected_candidate_sha256=candidate
                )
            elif (
                value.get("expected_commit") != expected
                or value.get("expected_candidate_sha256") != candidate
            ):
                return 1
            else:
                value.update(enabled=True, policy="SHADOW")
        data = json.dumps(value, sort_keys=True) + "\n"
        temp = paths.config.with_name(f".{paths.config.name}.{os.getpid()}.tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(descriptor, "w", closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            os.close(descriptor)
            descriptor = -1
            os.replace(temp, paths.config)
            parent = os.open(paths.config.parent, os.O_RDONLY)
            os.fsync(parent)
            os.close(parent)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            temp.unlink(missing_ok=True)
        return 0
    except (OSError, ValueError):
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
