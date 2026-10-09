#!/usr/bin/env python3
"""Run one canonical Tokmo V3 Pulse 60-second slot.

The runner supplies the production composition with an aligned slot and its
actual observation time from one UTC clock sample.  Ownership and startup
validation remain the responsibility of ``pulse_main`` and the lane registry;
the documentary Hermes cron artifact ships paused and uninstalled.
"""

from __future__ import annotations

import os
import sys
from datetime import UTC, datetime
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
OPERATION_ROOT = Path("/home/tokmo/tokmo-v3")
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.programs.integrated_pulse import pulse_main
from tokmo.values.pulse_policy import PULSE_POLICY_V1

ACTIVE_PULSE_POLICY = PULSE_POLICY_V1


def aligned_slot(now: datetime) -> str:
    epoch = int(now.timestamp())
    seconds = ACTIVE_PULSE_POLICY.cadence_seconds
    aligned = epoch - epoch % seconds
    return datetime.fromtimestamp(aligned, UTC).isoformat().replace("+00:00", "Z")


def main() -> int:
    # Real data root (relocated into the project on 2026-09-24). ~/.local/share/tokmo-v3 is a
    # compatibility symlink; strict stores refuse symlinked ancestors, so pass the real path.
    root = PROJECT / "var" / "data"
    now = datetime.now(UTC)
    args = [
        "--once",
        "--mode",
        "production",
        "--policy-version",
        ACTIVE_PULSE_POLICY.policy_version,
        "--scheduled-for",
        aligned_slot(now),
        "--observed-now",
        now.isoformat().replace("+00:00", "Z"),
        "--root",
        str(root),
        "--project-root",
        str(OPERATION_ROOT),
    ]
    if os.environ.get("TOKMO_PULSE_RECONCILE_ONLY") == "1":
        args.append("--reconcile-only")
    # Ambient edge: the process environment is read here only and injected;
    # src never touches os.environ (G3 activation SDS §4.1).
    return pulse_main(
        args, environ=dict(os.environ), capture_clock=lambda: datetime.now(UTC)
    )


if __name__ == "__main__":
    raise SystemExit(main())
