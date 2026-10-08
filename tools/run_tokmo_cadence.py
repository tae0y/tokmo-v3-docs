#!/usr/bin/env python3
"""Run one canonical Tokmo V3 production cadence slot."""
from __future__ import annotations

import os
import sys
from datetime import datetime, timezone
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
SRC = PROJECT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from tokmo.composition import cadence_main
from tokmo.values.cadence_policy import CADENCE_MARKET_PORTFOLIO_POLICY_V1


ACTIVE_POLICY = CADENCE_MARKET_PORTFOLIO_POLICY_V1


def aligned_slot(now: datetime) -> str:
    epoch = int(now.timestamp())
    seconds = ACTIVE_POLICY.cadence_seconds
    aligned = epoch - epoch % seconds
    return datetime.fromtimestamp(aligned, timezone.utc).isoformat().replace("+00:00", "Z")


def main() -> int:
    root = Path(__file__).resolve().parents[1] / "var" / "data"
    return cadence_main([
        "--once", "--mode", "production",
        "--policy-version", ACTIVE_POLICY.policy_version,
        "--scheduled-for", aligned_slot(datetime.now(timezone.utc)),
        "--root", str(root),
    ])


if __name__ == "__main__":
    raise SystemExit(main())
