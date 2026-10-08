"""Read one existing SEC filing's public primary document, with a durable four-attempt cap."""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from tokmo.records.news_decision_inputs import read_sec_observations
from run_news_decision_worker import build_sec_document_reader
from validate_news_activation import FOLDER, write


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accession", required=True)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    result = read_sec_observations(Path.home() / ".local/share/tokmo-v3/sec-targeted.sqlite3",
                                   since="2026-01-01T00:00:00+09:00", until=now)
    matches = [row for row in result["observations"] if row["accession_number"] == args.accession]
    if result["status"] != "OK" or len(matches) != 1:
        raise ValueError("existing SEC identity unavailable")
    FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = FOLDER / "sec-document-budget.json"
    budget = json.loads(path.read_text()) if path.exists() else {"purpose": "SEC primary document activation validation",
        "cap": 4, "basis": "2 * (one normal verification + one RCA request)", "attempts": [], "retry": 0}
    if len(budget["attempts"]) >= budget["cap"]:
        raise ValueError("SEC validation cap exhausted")
    attempt = {"accession": args.accession, "at": now.isoformat(), "status": "STARTED"}
    budget["attempts"].append(attempt); write(path, budget)
    try:
        value = build_sec_document_reader()(matches[0])
    except ValueError as exc:
        attempt.update(status="UNAVAILABLE", reason=str(exc)); write(path, budget)
        print(json.dumps(attempt)); return 1
    output = FOLDER / ("sec-document-" + args.accession + "-" + now.strftime("%Y%m%dT%H%M%S") + ".json")
    write(output, {"listing": matches[0], "document": value, "observed_at": datetime.now(timezone.utc).isoformat(),
                   "scope": "fetched now for document validation; not historical replay input"})
    attempt.update(status="OK", characters=len(value["text"]), artifact=str(output)); write(path, budget)
    print(json.dumps(attempt)); return 0


if __name__ == "__main__":
    raise SystemExit(main())
