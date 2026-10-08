#!/usr/bin/env python3
"""Run exactly five bounded pilot invocations; ordinary success is silent."""

import argparse
from pathlib import Path
import json
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for bootstrap in (ROOT, SRC):
    if str(bootstrap) not in sys.path:
        sys.path.insert(0, str(bootstrap))

from tools.opendart_bulk import main as bulk_main, failure_details


def run_five_polls(*, call, monotonic=time.monotonic, sleep=time.sleep):
    previous_start = None
    for ordinal in range(1, 6):
        if previous_start is not None:
            remaining = previous_start + 60.0 - monotonic()
            if remaining > 0:
                sleep(remaining)
        previous_start = monotonic()
        code = call()
        if code:
            return code
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(prog="run-opendart-pilot")
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)
    completed = 0
    details = {}
    def call():
        nonlocal completed
        code = bulk_main(["pilot-tick", "--home", str(args.home)],
                         emit=lambda value: details.update(failure_details(value)))
        if code == 0:
            completed += 1
        return code
    code = run_five_polls(call=call)
    summary = {"status": "complete", "polls": completed} if code == 0 else {
        "status": "failed", "completed_polls": completed, **details}
    print(json.dumps(summary, sort_keys=True, separators=(",", ":")))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
