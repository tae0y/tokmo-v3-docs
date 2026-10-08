#!/usr/bin/env python3
"""Optional one-minute scheduler entry for five-minute primary-source polls.

Keeps the existing 15-minute all-source jobs. It is inert until scheduled.
"""
import argparse
from datetime import datetime, timezone
from pathlib import Path

from run_news_group import main as group_main


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--credentials", type=Path, required=True)
    args = parser.parse_args(argv)
    now = datetime.now(timezone.utc).replace(second=0, microsecond=0)
    group = {0: "A", 1: "B", 2: "C"}.get(now.minute % 5)
    if group is None or (group == "A" and now.minute % 15 == 0):
        return 0
    return group_main(["--group", group, "--fast-primary", "--scheduled-for", now.isoformat(),
        "--manifest", str(args.manifest), "--credentials", str(args.credentials),
        "--runtime", str(args.root / "news/group_runtime"),
        "--db", str(args.root / "news/external_news_v4.sqlite3"),
        "--ledger", str(args.root / "news/fetch_attempts.sqlite3"),
        "--health-db", str(args.root / "news/source_health.sqlite3")])


if __name__ == "__main__":
    raise SystemExit(main())
