"""Production CLI for the 48-hour live-shadow runner (Stage 4 B).

This is the ambient edge: everything the sealed ``src/tokmo`` graph must
not touch lives here — the process environment, the real wall clock and
sleep, the git subprocess for the expected-commit gate, the stdlib HTTPS
opener for the optional Telegram lifecycle sender, and the exact
production cadence CLI seam (``tokmo.composition:cadence_main``). The
runner core in ``tokmo.programs.shadow_48h_runner`` receives all of these
as injected dependencies and stays free of live transports, environment
reads, and ambient clocks.

Usage: .venv/bin/python tools/run_48h_shadow.py --config <config.json>
"""

import argparse
import contextlib
import io
import json
import os
import subprocess
import sys
import time
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

_TOOL_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = _TOOL_DIR.parent
if str(PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT / "src"))

from tokmo.observability.shadow_lifecycle import (
    HarnessLifecycleEventV1,
    LifecycleTransportError,
    build_lifecycle_sink,
)
from tokmo.programs.shadow_48h_runner import (
    Shadow48hConfigV1,
    ShadowRunnerDepsV1,
    load_config,
    run_48h_shadow,
)
from tokmo.records.prelaunch_markers import verify_pass_marker


def _zulu(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _clock() -> datetime:
    return datetime.now(UTC)


def _telegram_opener(url: str, data: bytes, timeout: float) -> None:
    """stdlib HTTPS only, built at send time; the URL carries the env-only
    token and is never logged."""
    request = urllib.request.Request(url, data=data, method="POST")
    with urllib.request.urlopen(request, timeout=timeout):
        pass


def _invoke_production_tick(config: Shadow48hConfigV1):
    def invoke(slot: datetime, observed: datetime) -> dict:
        # the exact production CLI seam, one aligned tick per call
        from tokmo.composition import cadence_main

        argv = [
            "--once",
            "--mode",
            "live-shadow",
            "--policy-version",
            config.policy_version,
            "--scheduled-for",
            _zulu(slot),
            "--observed-now",
            _zulu(observed),
            "--root",
            str(config.shadow_root),
        ]
        if config.fixture_path is not None:
            argv += ["--fixture", str(config.fixture_path)]
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            exit_code = cadence_main(argv)
        lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
        summary = json.loads(lines[-1]) if lines else {}
        summary["exit_code"] = exit_code
        return summary

    return invoke


def _git_head() -> str:
    completed = subprocess.run(
        ["git", "-C", str(PROJECT_ROOT), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    return completed.stdout.strip()


def _credentials_check() -> bool:
    """Presence of the read-only Toss credential names only; values never
    reach any log or state."""
    from tokmo.composition import TOSS_ENV_NAME

    env_path = PROJECT_ROOT / TOSS_ENV_NAME
    if not env_path.is_file():
        return False
    names = set()
    for line in env_path.read_text().splitlines():
        if "=" in line and line.split("=", 1)[1].strip():
            names.add(line.split("=", 1)[0].strip())
    return {"TOSS_CLIENT_ID", "TOSS_CLIENT_SECRET"} <= names


def build_production_runner_deps(config: Shadow48hConfigV1) -> ShadowRunnerDepsV1:
    sink = build_lifecycle_sink(
        config.lifecycle,
        state_dir=config.shadow_root / "harness",
        clock=_clock,
        environ=os.environ,
        opener=_telegram_opener,
    )

    def lifecycle_emit(kind, signature=None, detail=None):
        try:
            sink(
                HarnessLifecycleEventV1(
                    kind=kind,
                    label="tokmo-v3-48h-shadow",
                    at=_clock(),
                    signature=signature,
                    detail=detail,
                )
            )
        except Exception:
            pass  # lifecycle may never fail the harness

    return ShadowRunnerDepsV1(
        clock=_clock,
        sleep=time.sleep,
        invoke_tick=_invoke_production_tick(config),
        git_head=_git_head,
        credentials_check=_credentials_check,
        marker_verify=verify_pass_marker,
        environ=os.environ,
        lifecycle_emit=lifecycle_emit,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="tokmo-48h-shadow")
    parser.add_argument("--config", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1
    try:
        config = load_config(Path(args.config))
    except (OSError, ValueError, KeyError):
        print("tokmo-48h-shadow refusal=config_invalid", file=sys.stderr)
        return 1
    try:
        deps = build_production_runner_deps(config)
    except LifecycleTransportError:
        print(
            "tokmo-48h-shadow refusal=lifecycle_transport_unavailable", file=sys.stderr
        )
        return 1
    return run_48h_shadow(config, deps)


if __name__ == "__main__":
    raise SystemExit(main())
