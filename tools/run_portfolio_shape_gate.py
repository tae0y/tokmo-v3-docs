#!/usr/bin/env python3
"""Opt-in read-only portfolio production shape gate."""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for bootstrap in (ROOT, SRC):
    if str(bootstrap) not in sys.path:
        sys.path.insert(0, str(bootstrap))

from tokmo.programs.portfolio_shape_gate import main


if __name__ == "__main__":
    raise SystemExit(main())
