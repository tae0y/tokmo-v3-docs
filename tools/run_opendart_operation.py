#!/usr/bin/env python3
"""Cron-facing one-shot OpenDART operation: silent success, closed failure."""

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for bootstrap in (ROOT, SRC):
    if str(bootstrap) not in sys.path:
        sys.path.insert(0, str(bootstrap))

from tools.opendart_bulk import failure_details
from tools.opendart_bulk import main as bulk_main


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run-opendart-operation")
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)
    details = {}
    code = bulk_main(
        ["operate", "--home", str(args.home)],
        emit=lambda value: details.update(failure_details(value)),
    )
    if code:
        print(
            json.dumps(dict(status="failed", **details), separators=(",", ":")),
            file=sys.stderr,
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
