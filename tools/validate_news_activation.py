"""One frozen day of fresh NEWS decisions. No sender, operations DB or production-state writes.

prepare reads existing original observations/requests, including excluded and ERROR inputs.
replay uses the ordinary engine with recorded time and an explicit counterfactual delivery
projection in a private replay DB. It creates no provider receipt. Source fetches stay disabled:
documents acquired later are evaluated separately, never smuggled into historical input.
"""
import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import fcntl
import hashlib
import json
from pathlib import Path
import re
import sqlite3
import sys

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from tokmo.programs.news_decision_engine import NewsDecisionEngine, RECORDED_TIME
from tokmo.records.news_decision_store import NewsDecisionStore, kst, timestamp
from news_decision_models import build_models

FOLDER = PROJECT / ".hermes/evidence/news-value-20260907/activation"
SOURCE = Path.home() / ".local/share/tokmo-v3/stores/news_episodes.sqlite3"
MODEL_CAP = 806  # 2 * (387 recorded calls for a full day + 16 bounded RCA calls).
INHERITED_MODEL_ATTEMPTS = 12  # Earlier six-case checks, corrections and blocked attempts.


def write(path, value):
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")
    temporary.chmod(0o600)
    temporary.replace(path)


def code_hashes():
    paths = ["tools/validate_news_activation.py", "tools/news_decision_models.py",
             "src/tokmo/programs/news_document_quality.py",
             "src/tokmo/programs/news_decision_engine.py", "src/tokmo/records/news_decision_store.py"]
    return {name: hashlib.sha256((PROJECT / name).read_bytes()).hexdigest() for name in paths}


def prepare(until):
    FOLDER.mkdir(parents=True, exist_ok=True, mode=0o700)
    if (FOLDER / "case.json").exists():
        raise ValueError("frozen case already exists")
    end = timestamp(until)
    # Explicit Korean date independent of the host's timezone.
    start = timestamp(kst(end)[:10] + "T00:00:00+09:00")
    c = sqlite3.connect(SOURCE.as_uri() + "?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        c.execute("PRAGMA query_only=ON"); c.execute("BEGIN")
        inputs = [dict(r) for r in c.execute("SELECT * FROM news_decision_inputs WHERE julianday(observed_at)>=julianday(?) AND julianday(observed_at)<julianday(?)", (start.isoformat(), end.isoformat()))]
        calls = [dict(r) for r in c.execute("SELECT * FROM news_decision_calls WHERE julianday(started_at)>=julianday(?) AND julianday(started_at)<julianday(?) ORDER BY julianday(started_at),id", (start.isoformat(), end.isoformat()))]
        preceding = c.execute("SELECT count(*) FROM news_decision_calls WHERE julianday(started_at)<julianday(?)", (start.isoformat(),)).fetchone()[0]
    finally:
        c.close()
    if preceding:
        raise ValueError("a nonempty initial history requires an explicit as-of initial state")
    first, contexts = {}, []
    for call in calls:
        req = json.loads(call["request"])
        at = kst(call["started_at"])
        if call["stage"] == "screen":
            for item in req.get("evidence", []):
                first.setdefault(item["id"], at)
        elif req.get("context"):
            value = req["context"]
            if not contexts or contexts[-1]["context"] != value:
                contexts.append({"available_at": at, "context": value})
    observations = []
    for row in inputs:
        if row["id"] not in first:
            raise ValueError("original input never exposed in a recorded screen: " + row["id"])
        item = json.loads(row["payload"])
        if timestamp(item["observed_at"]) > timestamp(first[row["id"]]):
            raise ValueError("future source at original screen")
        observations.append({"available_at": first[row["id"]], "observation": item, "original_state": row["state"]})
    value = {"version": 1, "since": kst(start), "until": kst(end), "observations": observations,
             "contexts": contexts, "initial_events": [], "original_calls": len(calls),
             "original_states": dict(Counter(row["state"] for row in inputs)), "source": str(SOURCE),
             "scope": "all original decision inputs; original screen availability; no later source documents"}
    write(FOLDER / "case.json", value)
    write(FOLDER / "manifest.json", {"code_hashes": code_hashes(), "case_sha256": hashlib.sha256((FOLDER / "case.json").read_bytes()).hexdigest(),
        "model_call_cap_new": MODEL_CAP, "model_attempts_inherited": INHERITED_MODEL_ATTEMPTS,
        "purpose_total_cap": MODEL_CAP + INHERITED_MODEL_ATTEMPTS, "models": ["glm-5.3-flash", "glm-5.3"],
        "network_retry": 0, "source_fetches": 0, "sending": "unavailable", "private_payload": "same source evidence and portfolio context previously used by the existing NEWS models",
        "retention": "private validation artifact; review for removal after release acceptance or within 30 days"})
    print(json.dumps({"inputs": len(inputs), "original_calls": len(calls), "states": value["original_states"], "case": str(FOLDER / "case.json")}))


def context_at(contexts, now):
    available = [row for row in contexts if timestamp(row["available_at"]) <= timestamp(now)]
    return dict(available[-1]["context"]) if available else {"portfolio_status": "UNAVAILABLE_AT_THIS_TIME"}


def virtual_delivery(engine, now):
    """Explicit fictional delivery in an isolated counterfactual; never a Telegram receipt.

    last_sent projects what this replay's user would have received, so each following decision
    compares against the replay's own selected body. Digest evidence coverage remains UNKNOWN,
    matching production when constituent evidence was not preserved in the proposal.
    """
    alerts = engine.pending_alerts()
    for alert in alerts:
        ids = {alert["id"], *alert.get("constituent_ids", [])}
        for event in engine.store.events():
            if not any(item["id"] in ids for item in event.get("pending_alerts", [])):
                continue
            event["pending_alerts"] = [item for item in event["pending_alerts"] if item["id"] not in ids]
            event["last_sent"] = {"alert_id": alert["id"], "body": alert["body"], "delivered_at": kst(now),
                                  "simulation": "COUNTERFACTUAL_ONLY"}
            # Production represents unknown digest coverage by absence of evidence_ids.
            # The literal string UNKNOWN would be mistaken for an iterable of evidence ids.
            if alert.get("evidence_ids"):
                event["last_sent"]["evidence_ids"] = list(alert["evidence_ids"])
            event["last_delivery"] = "SENT"  # Fictional model context; no ops row or provider ID exists.
            event["completed_proposal_id"] = alert["id"]
            if alert.get("evidence_ids") and event.get("unsent_evidence"):
                event["unsent_evidence"]["ids"] = [key for key in event["unsent_evidence"]["ids"] if key not in alert["evidence_ids"]]
            event["comparison_proposals"] = [item for item in event.get("comparison_proposals", []) if item["id"] not in ids]
            engine.store.save_event(event)
    return [dict(alert, virtual_delivered_at=kst(now), simulation="COUNTERFACTUAL_ONLY") for alert in alerts]


class BoundedModel:
    def __init__(self, callback, ledger):
        self.callback, self.ledger = callback, ledger
        self.model, self.max_output_tokens = callback.model, callback.max_output_tokens

    def __call__(self, request):
        # Reuse the existing provider route. A stale/failed quota read blocks new calls.
        quota_path = Path.home() / ".hermes/cron/state/model-quota-latest.json"
        quota = json.loads(quota_path.read_text())
        age = (datetime.now(timezone.utc) - timestamp(quota["observed_at"])).total_seconds()
        provider = quota.get("providers", {}).get("zai", {})
        if not 0 <= age <= 2700 or not provider.get("ok") or any(w.get("remaining", 0) <= 5 for w in provider.get("windows", [])):
            raise ValueError("activation_quota_unavailable_or_low")
        with (FOLDER / "model-budget.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            path = FOLDER / "model-budget.json"
            if path.exists():
                current = json.loads(path.read_text())
                self.ledger.clear(); self.ledger.update(current)
            if self.ledger["used"] >= MODEL_CAP:
                raise ValueError("activation_model_cap")
            self.ledger["used"] += 1
            self.ledger["latest"] = {"model": self.model, "historical_now": request["now"], "quota_observed_at": quota["observed_at"]}
            write(path, self.ledger)  # Failed attempts consume the same bound, including concurrent checks.
        try:
            return self.callback(request)
        except Exception as exc:
            raise ValueError("activation_model_unavailable_" + type(exc).__name__) from None


def run_folder(name):
    if name is None:
        return FOLDER
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,63}", name):
        raise ValueError("invalid run name")
    return FOLDER / name


def freeze(name):
    if name is None:
        raise ValueError("new run name required; original evidence is immutable")
    folder = run_folder(name)
    folder.mkdir(mode=0o700)
    original = json.loads((FOLDER / "manifest.json").read_text())
    if hashlib.sha256((FOLDER / "case.json").read_bytes()).hexdigest() != original["case_sha256"]:
        raise ValueError("original case changed")
    value = dict(original, code_hashes=code_hashes(), run=name,
                 frozen_at=datetime.now(timezone.utc).isoformat(), shared_budget=str(FOLDER / "model-budget.json"))
    write(folder / "manifest.json", value)
    for relative in value["code_hashes"]:
        path = folder / "code" / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((PROJECT / relative).read_bytes()); path.chmod(0o600)
    print(json.dumps({"frozen_run": name, "source_case_unchanged": True, "budget_reset": False}))


def replay(name=None):
    folder = run_folder(name)
    manifest = json.loads((folder / "manifest.json").read_text())
    if manifest["code_hashes"] != code_hashes() or manifest["case_sha256"] != hashlib.sha256((FOLDER / "case.json").read_bytes()).hexdigest():
        raise ValueError("frozen code or case changed")
    path = folder / "counterfactual.sqlite3"
    if path.exists():
        raise ValueError("replay state already exists; preserve it for explicit review")
    case = json.loads((FOLDER / "case.json").read_text())
    ledger = json.loads((FOLDER / "model-budget.json").read_text()) if (FOLDER / "model-budget.json").exists() else {"used": 0, "inherited": INHERITED_MODEL_ATTEMPTS}
    initial_attempts = ledger["used"]
    screen, analyze = [BoundedModel(callback, ledger) for callback in build_models()]
    by_time = defaultdict(list)
    for row in case["observations"]:
        by_time[timestamp(row["available_at"])].append(row["observation"])
    points = set(by_time)
    at, end = timestamp(case["since"]), timestamp(case["until"])
    while at < end:
        points.add(at); at += timedelta(minutes=1)
    report = {"mode": "FRESH_MODEL_COUNTERFACTUAL", "since": case["since"], "until": case["until"],
              "sending": "unavailable", "delivery_assumption": "all selected notifications complete at each replay tick; no transport latency",
              "source_enrichment": "disabled in history; later documents evaluated separately", "initial_model_attempts": initial_attempts,
              "timeline": [], "virtual_notifications": [], "failures": []}
    store = NewsDecisionStore(path)
    try:
        for now in sorted(points):
            engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME,
                context_reader=lambda time: context_at(case["contexts"], time))
            result = engine.tick(by_time.get(now, []), now)
            delivered = virtual_delivery(engine, now)
            if result["calls"] or result["failures"] or delivered:
                report["timeline"].append({"now": kst(now), "result": result})
                report["virtual_notifications"].extend(delivered)
                report["failures"].extend(dict(f, at=kst(now)) for f in result["failures"])
                report.update(model_attempts=ledger["used"], run_model_attempts=ledger["used"] - initial_attempts, input_states=store.counts(), virtual_notification_count=len(report["virtual_notifications"]), last_historical_at=kst(now))
                write(folder / "replay-report.json", report)
                print(json.dumps({key: report[key] for key in ["last_historical_at", "model_attempts", "input_states", "virtual_notification_count"]}), flush=True)
            if any(f.get("reason", "").startswith("activation_") or f.get("reason") in {"HTTPError", "URLError", "TimeoutError"} for f in result["failures"]):
                report["stopped"] = "model availability/cap; no retry"
                break
        report["complete"] = not report.get("stopped") and not store.counts().get("READY") and sum(store.counts().values()) == len(case["observations"])
        report["error_inputs"] = [list(r) for r in store.errors()]
        report["events"] = store.events()
        report["call_tokens"] = store.connection.execute("SELECT coalesce(sum(tokens),0) FROM news_decision_calls").fetchone()[0]
        write(folder / "replay-report.json", report)
    finally:
        store.close()
    return 0 if report["complete"] and not report["failures"] else 2


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["prepare", "freeze", "replay"])
    parser.add_argument("--run")
    parser.add_argument("--until", default="2026-09-07T20:58:55.992814+09:00")
    args = parser.parse_args()
    if args.mode == "prepare":
        prepare(args.until); return 0
    if args.mode == "freeze":
        freeze(args.run); return 0
    return replay(args.run)


if __name__ == "__main__":
    raise SystemExit(main())
