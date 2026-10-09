#!/usr/bin/env python3
"""Read one numbered NEWS item. No model, network, DB writes or message sending."""

import argparse
import json
from pathlib import Path

from tokmo.programs.news_digest_lookup import read_digest_item


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--number", required=True, type=int)
    parser.add_argument("--date", help="Printed digest date, YYYY-MM-DD in Korea")
    parser.add_argument("--digest-id")
    parser.add_argument(
        "--reply-receipt", help="Exact stored Telegram group/message receipt"
    )
    root = Path(__file__).resolve().parents[1] / "var/data"
    parser.add_argument(
        "--state", type=Path, default=root / "stores/news_episodes.sqlite3"
    )
    parser.add_argument("--ops", type=Path, default=root / "ops/ops_kernel.sqlite3")
    args = parser.parse_args(argv)
    result = read_digest_item(
        args.state,
        args.ops,
        args.number,
        date=args.date,
        digest_id=args.digest_id,
        reply_receipt=args.reply_receipt,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
