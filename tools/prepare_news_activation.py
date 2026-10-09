"""Prepare a reviewable NEWS release packet. Reads production; writes private evidence only.

This tool has no install, activation, send, state-reset or rollback command.
Backups are preflight evidence; installation must back up again under both worker locks.
"""

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from validate_news_activation import FOLDER, PROJECT, write

RUNTIME = Path.home() / ".hermes/runtime/tokmo-v3/news-shadow-src/projects/tokmo-v3"
CANONICAL = Path.home() / "tokmo/tokmo-v3/projects/tokmo-v3"
DATA = Path(__file__).resolve().parents[1] / "var/data"


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def snapshot():
    gate = json.loads((DATA / "news_activation.json").read_text())
    config = json.loads((DATA / "news_decision.json").read_text())
    db = sqlite3.connect(
        (DATA / "stores/news_episodes.sqlite3").as_uri() + "?mode=ro", uri=True
    )
    db.row_factory = sqlite3.Row
    try:
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        trips = [
            dict(row)
            for row in db.execute(
                "SELECT * FROM live_delivery_trips WHERE activation_id=?",
                (gate["activation_id"],),
            )
        ]
        states = dict(
            db.execute("SELECT state,count(*) FROM news_decision_inputs GROUP BY state")
        )
        events = [
            json.loads(row[0])
            for row in db.execute("SELECT state FROM news_decision_events")
        ]
        pending = {
            p["id"]: {
                k: p.get(k)
                for k in ("id", "created_at", "kind", "importance", "urgency")
            }
            for event in events
            for p in event.get("pending_alerts", [])
        }
    finally:
        db.close()
    db = sqlite3.connect(
        (DATA / "ops/ops_kernel.sqlite3").as_uri() + "?mode=ro", uri=True
    )
    try:
        db.execute("PRAGMA query_only=ON")
        unsent = dict(
            db.execute(
                "SELECT status,count(*) FROM alert_outbox WHERE alert_id LIKE 'nda-%' AND status!='SENT' GROUP BY status"
            )
        )
    finally:
        db.close()
    return {
        "at": datetime.now(UTC).isoformat(),
        "activation": gate,
        "decision_flags": {
            k: config.get(k) for k in ("enabled", "delivery_enabled", "cutoff")
        },
        "trips": trips,
        "input_states": states,
        "pending_proposals": list(pending.values()),
        "unsent_outbox_parts": unsent,
    }


def main():
    folder = FOLDER / ("preflight-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S"))
    folder.mkdir(mode=0o700)
    candidate = json.loads((FOLDER.parent / "candidate-manifest.json").read_text())
    baseline = json.loads((FOLDER.parent / "baseline-manifest.json").read_text())
    rows, problems = [], []
    for item in candidate["files"]:
        name = item["path"]
        source, destination = PROJECT / name, RUNTIME / name
        before, after = digest(destination), digest(source)
        if before != item["before_sha256"]:
            problems.append("runtime changed: " + name)
        compile(source.read_bytes(), str(destination), "exec")
        for prefix, path in (("candidate", source), ("runtime-before", destination)):
            if path.is_file():
                backup = folder / prefix / name
                backup.parent.mkdir(parents=True, exist_ok=True)
                backup.write_bytes(path.read_bytes())
                backup.chmod(0o600)
                if digest(backup) != digest(path):
                    raise ValueError("preflight copy changed")
        rows.append(
            {
                "path": name,
                "before_sha256": before,
                "after_sha256": after,
                "before_mode": destination.stat().st_mode & 0o777
                if destination.exists()
                else None,
                "candidate_mode": source.stat().st_mode & 0o777,
            }
        )
    integrity = [
        {
            "path": row["path"],
            "canonical_unchanged": digest(CANONICAL / row["path"])
            == row["canonical_start"],
            "runtime_unchanged": digest(RUNTIME / row["path"]) == row["runtime"],
        }
        for row in baseline["files"]
    ]
    if not all(r["canonical_unchanged"] and r["runtime_unchanged"] for r in integrity):
        problems.append("baseline files changed in another session")
    release = CANONICAL / ".ai/releases/important-news-20260906/manifest.json"
    raw = release.read_bytes()
    (folder / "release-manifest-before.json").write_bytes(raw)
    (folder / "release-manifest-before.json").chmod(0o600)
    import defusedxml  # Same venv used by NEWS; no dependency installation.

    live = snapshot()
    blockers = [
        "full-day fresh-model comparison incomplete after JSON parsing failure",
        "document explanations required semantic correction; broad discovery-quality validation incomplete",
    ]
    if live["trips"]:
        blockers.append(
            "existing activation delivery trip; original provider failure cause unknown"
        )
    report = {
        "status": "BLOCKED",
        "technical_preflight": "PASS" if not problems else "FAIL",
        "problems": problems,
        "blockers": blockers,
        "files": rows,
        "integrity": integrity,
        "live": live,
        "release_manifest_sha256": hashlib.sha256(raw).hexdigest(),
        "dependency_defusedxml": defusedxml.__version__,
        "deployed": False,
        "approval_packet_complete": False,
        "remaining_packet_work": "after quality acceptance: guarded installer, new activation/cutoff proposal and preview of old pending exclusions",
        "backup_scope": "code and release manifest copied privately without locks; repeat under locks immediately before installation; no DB rewind",
    }
    write(folder / "report.json", report)
    print(
        json.dumps(
            {
                "report": str(folder / "report.json"),
                "status": report["status"],
                "technical_preflight": report["technical_preflight"],
                "files": len(rows),
                "pending_proposals": len(live["pending_proposals"]),
                "input_states": live["input_states"],
                "trips": live["trips"],
                "problems": problems,
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
