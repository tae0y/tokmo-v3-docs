#!/usr/bin/env python3
"""Cron-facing one-shot SEC targeted operation.

Future no_agent cron target (10,40 * * * * KST).  Calls
run_sec_targeted_operation exactly once.  Exit 0 only when code == "complete";
every other outcome exits nonzero with a fixed sanitized stderr containing
only code and aggregate counts (no ticker, CIK, company name, user agent,
or file path).  Any exception is mapped to a fixed sanitized category.

Success is silent: both stdout and stderr are empty on exit 0.
"""

import argparse
import json
import sys
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
for bootstrap in (ROOT, SRC):
    if str(bootstrap) not in sys.path:
        sys.path.insert(0, str(bootstrap))

from tokmo.composition import run_sec_targeted_operation
from tokmo.values.news_collection_diagnostics import failure_diagnostics


def _sanitized_stderr(result) -> str:
    """Fixed sanitized line: code and aggregate counts only."""
    return (
        json.dumps(
            {
                "code": result.code,
                "started": result.started,
                "target_count": result.target_count,
                "filings_saved": result.filings_saved,
                "duplicates": result.duplicates,
                "conflicts": result.conflicts,
                "unresolved_count": len(result.unresolved),
            },
            sort_keys=True,
        )
        + "\n"
    )


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="run-sec-targeted-operation")
    parser.add_argument("--home", required=True, type=Path)
    args = parser.parse_args(argv)

    started = time.monotonic()
    try:
        result = run_sec_targeted_operation(home=args.home)
    except Exception as error:
        print(
            json.dumps(
                dict(
                    code="internal_error",
                    diagnostics=failure_diagnostics(
                        error,
                        elapsed_ms=int((time.monotonic() - started) * 1000),
                        trace=uuid4().hex[:16],
                    ),
                ),
                separators=(",", ":"),
            ),
            file=sys.stderr,
        )
        return 1

    if result.code == "complete":
        return 0

    sys.stderr.write(_sanitized_stderr(result))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
