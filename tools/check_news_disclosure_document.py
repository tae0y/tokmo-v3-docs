"""Fetch exactly one public disclosure document to a private evaluation artifact. Never sends alerts."""

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from run_news_decision_worker import build_document_reader


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--receipt", required=True)
    args = parser.parse_args(argv)
    folder = (
        Path(__file__).resolve().parents[1]
        / ".hermes"
        / "evidence"
        / "news-value-20260907"
    )
    folder.mkdir(parents=True, exist_ok=True)
    at = datetime.now(UTC).astimezone()
    try:
        value = build_document_reader()(args.receipt)
    except ValueError as exc:
        print(json.dumps({"status": "UNAVAILABLE", "reason": str(exc)}))
        return 1
    path = folder / (
        "document-" + args.receipt + "-" + at.strftime("%Y%m%dT%H%M%S") + ".json"
    )
    path.write_text(
        json.dumps(
            dict(
                value,
                receipt=args.receipt,
                observed_at=at.isoformat(),
                scope="fetched now; not available in historical replay",
            ),
            ensure_ascii=False,
            indent=2,
        )
        + "\n"
    )
    print(
        json.dumps(
            {"status": "OK", "characters": len(value["text"]), "artifact": str(path)}
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
