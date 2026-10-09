#!/usr/bin/env python3
"""Apply recorded revision evidence to the documents it describes.

Reports what it would do unless --apply is given.
"""

import argparse
import json
import sys
from pathlib import Path

from tokmo.programs.news_revision_backfill import backfill_news_revisions


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument(
        "--apply", action="store_true", help="write the updates; otherwise report only"
    )
    args = parser.parse_args(argv)
    report = backfill_news_revisions(args.db, apply=args.apply)
    json.dump(report.as_dict(), sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
