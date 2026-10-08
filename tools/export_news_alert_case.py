#!/usr/bin/env python3
"""Export collected evidence to a reusable case; source DBs are read-only."""
import argparse
from datetime import timedelta
import json
from pathlib import Path

from tokmo.records.news_decision_inputs import read_news_observations, read_disclosure_observations
from tokmo.records.news_decision_store import timestamp
from replay_news_alerts import safe_output


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--news-db", type=Path)
    parser.add_argument("--disclosure-db", type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until", required=True)
    parser.add_argument("--id", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)
    if not (args.news_db or args.disclosure_db):
        parser.error("At least one source DB is required")
    if timestamp(args.until) <= timestamp(args.since):
        parser.error("until must be after since")
    observations, sources = [], []
    for path, reader in ((args.news_db, read_news_observations), (args.disclosure_db, read_disclosure_observations)):
        if path is None:
            continue
        cursor = args.since
        while True:
            result = reader(path, since=cursor, until=args.until)
            if result["status"] == "UNAVAILABLE":
                raise ValueError("Source unavailable: " + str(path))
            observations.extend(result["observations"])
            if not result["has_more"]:
                break
            cursor = result["next_since"]
        sources.append(str(path))
    end = timestamp(args.until)
    # Enough virtual ticks to drain same-time batches without wall-clock wait.
    checkpoints = [(end + timedelta(minutes=index)).isoformat() for index in range(1, (len(observations) + 19) // 20 + 1)]
    case = {"version": 1, "id": args.id, "description": "Historical collection replay; manually review expected alerts before evaluation.",
            "sources": sources, "availability_note": "Collection times only. Revised news lacks older body snapshots; only the latest revision's conservative availability is exported. Disclosure listings are not document bodies.",
            "observations": sorted(observations, key=lambda row: (timestamp(row["observed_at"]), row["id"])),
            "context": [], "checkpoints": checkpoints, "expectations": [], "recorded_responses": {"screen": [], "analyze": []}}
    output = safe_output(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x") as stream:
        json.dump(case, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps({"case": str(output), "observations": len(observations), "sources_readonly": True}))


if __name__ == "__main__":
    main()
