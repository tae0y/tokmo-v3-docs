#!/usr/bin/env python3
"""Small reusable replay of the real news engine; never sends Telegram messages.

PYTHONPATH=src python tools/replay_news_alerts.py --case CASE.json
Recorded mode checks processing behavior, NOT fresh AI judgment quality.
Live mode explicitly enables model calls, but still never sends messages.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
import sys
import tempfile


def instant(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("Replay times must include a timezone")
    return result


def safe_output(path: Path) -> Path:
    path = path.expanduser().resolve()
    # Also catches alternate HOME fixtures and symlinks into canonical storage.
    if "/.local/share/tokmo-v3" in str(path) or "/.hermes" in str(path):
        raise ValueError("Replay output/state must not use production storage")
    return path


class RecordedModel:
    def __init__(self, responses: list[dict], name: str):
        self.responses = copy.deepcopy(responses)
        self.name = name
        self.used = 0

    def __call__(self, request: dict) -> dict:
        if self.used >= len(self.responses):
            raise ValueError(f"Recorded {self.name} responses exhausted; add a response or use live mode")
        response = self.responses[self.used]
        self.used += 1
        # Convenience for fixtures: resolve the event created earlier this run.
        events = request.get("events", [])
        first_id = (events[0].get("event_id") or events[0].get("id")) if events else None
        def resolve(value):
            if value == "$first_event":
                if first_id is None:
                    raise ValueError("$first_event requires an existing event")
                return first_id
            if isinstance(value, dict):
                return {key: resolve(item) for key, item in value.items()}
            if isinstance(value, list):
                return [resolve(item) for item in value]
            return value
        result = resolve(response)
        result["_recorded_source_model"] = result.get("_model")
        result["_model"] = "recorded"
        result["_usage"] = {"total_tokens": 0}
        result["_elapsed_seconds"] = 0
        return result


def check_expectations(expectations: list[dict], timeline: list[dict]) -> list[dict]:
    results = []
    for expected in expectations:
        entries = timeline
        if expected.get("at"):
            entries = [row for row in entries if instant(row["now"]) == instant(expected["at"])]
        alerts = [alert for row in entries for alert in row["result"].get("alerts", [])]
        text = json.dumps(alerts, ensure_ascii=False)
        checks = []
        if "alert_count" in expected:
            checks.append(len(alerts) == expected["alert_count"])
        if "min_alert_count" in expected:
            checks.append(len(alerts) >= expected["min_alert_count"])
        if "max_alert_count" in expected:
            checks.append(len(alerts) <= expected["max_alert_count"])
        if "alert_evidence_id" in expected:
            checks.append(any(expected["alert_evidence_id"] in alert.get("evidence_ids", []) for alert in alerts))
        if "urgency" in expected:
            checks.append(any(alert.get("urgency") == expected["urgency"] for alert in alerts))
        if "contains" in expected:
            checks.append(expected["contains"] in text)
        if "not_contains" in expected:
            checks.append(expected["not_contains"] not in text)
        results.append({"expected": expected, "passed": bool(checks) and all(checks)})
    return results


def replay(case: dict, state_path: Path, *, mode="recorded", until=None,
           screen=None, analyze=None, engine_factory=None, store_factory=None, context_mode=None, reuse_unchanged_reviews=False, scoped_review_reuse=False) -> dict:
    if case.get("version") != 1:
        raise ValueError("Case version must be 1")
    safe_output(state_path)
    observations = sorted(case.get("observations", []), key=lambda row: instant(row["observed_at"]))
    ids = [row["id"] for row in observations]
    if len(ids) != len(set(ids)):
        raise ValueError("Observation ids must be unique")
    contexts = sorted(case.get("context", []), key=lambda row: instant(row["observed_at"]))
    times = {instant(row["observed_at"]) for row in observations}
    times.update(instant(value) for value in case.get("checkpoints", []))
    times.update(instant(row["observed_at"]) for row in contexts)
    start_at = instant(case['start_at']) if case.get('start_at') else None
    if start_at:
        # A restart/backlog simulation changes processing time, not the
        # source's observed/published times. Original facts stay intact.
        times = {t for t in times if t >= start_at} | {start_at}
    limit = instant(until) if until else None
    if screen is None or analyze is None:
        if mode != "recorded":
            raise ValueError("Live mode requires model callbacks")
        responses = case.get("recorded_responses", {})
        screen = RecordedModel(responses.get("screen", []), "screen")
        analyze = RecordedModel(responses.get("analyze", []), "analyze")
    if engine_factory is None:
        from tokmo.programs.news_decision_engine import NewsDecisionEngine
        engine_factory = NewsDecisionEngine
    if store_factory is None:
        from tokmo.records.news_decision_store import NewsDecisionStore
        store_factory = NewsDecisionStore
    def context_reader(now):
        available = [row["context"] for row in contexts if instant(row["observed_at"]) <= now]
        return copy.deepcopy(available[-1]) if available else {}
    store = store_factory(state_path)
    timeline = []
    ops = None
    simulated_receipt = 9000000000
    try:
        # Replay is recorded-time mode: every model call uses the tick's recorded now.
        from tokmo.programs.news_decision_engine import RECORDED_TIME
        options = {'context_mode':context_mode} if context_mode else {}
        if reuse_unchanged_reviews:
            options['reuse_unchanged_reviews'] = True
        if scoped_review_reuse:
            options['scoped_review_reuse'] = True
        engine = engine_factory(store, screen, analyze, context_reader=context_reader, clock=RECORDED_TIME, **options)
        if case.get("simulate_delivery"):
            from tokmo.records.ops_kernel import OpsKernelStore
            from tokmo.values.ops_kernel import ProducerRole
            from tokmo.programs.news_decision_outbox import queue_proposals, reconcile_deliveries
            from tokmo.programs.news_alert_delivery import run_alert_delivery
            ops = OpsKernelStore(state_path.with_name("replay-ops.sqlite3"), stable_phase_ids=frozenset())
        for now in sorted(times):
            if limit and now > limit:
                break
            batch = [copy.deepcopy(row) for row in observations if instant(row['observed_at']) == now
                or (start_at == now and instant(row['observed_at']) < now)]
            passes = case.get('max_drain_passes', 100) if case.get('drain_backlog') else 1
            if type(passes) is not int or not 1 <= passes <= 100:
                raise ValueError('max_drain_passes must be 1..100')
            for _ in range(passes):
                result = engine.tick(batch, now=now)
                if ops is not None:
                    queued = queue_proposals(engine, ops, ProducerRole.HERMES, cutoff=min(times), activation_enabled=True, test=True)
                    deliveries = []
                    for index in range(queued["queued"]):
                        # Only this isolated store receives a local receipt. Use
                        # the real receipt shape so reconciliation and history
                        # run too; never weaken production receipt validation.
                        simulated_receipt += 1
                        deliveries.append(run_alert_delivery(ops_kernel=ops, role=ProducerRole.HERMES, now=now,
                            telegram_send=lambda body, **kw: str(simulated_receipt),
                            keyboard_resolver=lambda key: None, delivery_binder=lambda *a, **kw: None))
                    reconcile_deliveries(engine, ops)
                    result["simulated_delivery"] = deliveries
                timeline.append({"now": now.isoformat(), "observation_ids": [row["id"] for row in batch], "result": result})
                batch = []
                if not case.get('drain_backlog') or result.get('failures') or not store.pending():
                    break
    finally:
        store.close()
        if ops is not None:
            ops.close()
    applicable = [item for item in case.get("expectations", [])
                  if not limit or (item.get("at") and instant(item["at"]) <= limit)]
    assertions = check_expectations(applicable, timeline)
    failures = [failure for row in timeline for failure in row["result"].get("failures", [])]
    remaining = timeline[-1]["result"].get("input_states", {}).get("READY", 0) if timeline else 0
    return {"version": 1, "case_id": case["id"], "description": case.get("description", ""),
            "mode": mode, "ai_quality_validated": False, "fresh_ai_judgment": mode == "live", "telegram_sending": "disabled",
            "delivery_simulated": bool(case.get("simulate_delivery")),
            "receipt_scope": "LOCAL_REPLAY_ONLY; numeric receipts are synthetic, never external delivery proof",
            "note": "Recorded responses test processing only; no fresh AI quality validation." if mode == "recorded" else "Live model replay; review judgments and evidence before claiming quality.",
            "until": until, "timeline": timeline, "assertions": assertions,
            "skipped_expectations": len(case.get("expectations", [])) - len(applicable),
            "calls": sum(row["result"].get("calls", 0) for row in timeline),
            "unprocessed_inputs": remaining,
            "passed": not failures and not remaining and all(item["passed"] for item in assertions)}


def markdown(report: dict) -> str:
    rows = [f"# News replay: {report['case_id']}", "", report["description"], "",
            f"Mode: {report['mode']}. Telegram: disabled. Passed: {report['passed']}", "", report["note"], ""]
    for entry in report["timeline"]:
        rows += [f"## {entry['now']}", "", "Input: " + ", ".join(entry["observation_ids"]), "",
                 "```json", json.dumps(entry["result"], ensure_ascii=False, indent=2, default=str), "```", ""]
    rows += ["## Expectations", "", "```json", json.dumps(report["assertions"], ensure_ascii=False, indent=2), "```", ""]
    return "\n".join(rows)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--case", required=True, type=Path)
    parser.add_argument("--until", help="Timezone-aware simulated cutoff; no wall-clock waiting")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--mode", choices=("recorded", "live"), default="recorded")
    parser.add_argument("--model")
    parser.add_argument('--screen-model')
    parser.add_argument('--analysis-model')
    parser.add_argument('--analysis-reasoning-effort',choices=('low','medium','high','xhigh','max'))
    parser.add_argument('--context-mode',choices=('compact','full','efficient','incremental'),default='compact')
    parser.add_argument('--reuse-unchanged-reviews', action='store_true')
    parser.add_argument('--scoped-review-reuse', action='store_true')
    parser.add_argument('--timeout-seconds',type=int,default=90)
    parser.add_argument("--keep-state", action="store_true", help="Keep a newly created replay DB alongside reports")
    args = parser.parse_args(argv)
    try:
        case = json.loads(args.case.read_text())
        root = Path(__file__).resolve().parents[1]
        paths = list((root / "src/tokmo").glob("*/news_decision*.py")) + [Path(__file__).resolve(), root / "tools/news_decision_models.py",
            root / 'src/tokmo/programs/news_context_memory.py',
            root / 'src/tokmo/programs/news_context_efficiency.py',
            root / 'src/tokmo/programs/news_incremental_review.py',
            root / 'src/tokmo/records/news_market_observations.py',
            root / "tools/subscription_json_model.py",
            root / "src/tokmo/records/ops_kernel.py", root / "src/tokmo/programs/pulse_production_deps.py"]
        code_hashes = {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest() for path in paths}
        case_hash = hashlib.sha256(args.case.read_bytes()).hexdigest()
        # Unique run directories prevent accidentally overwriting earlier evidence.
        base = safe_output(args.output or Path(tempfile.gettempdir()) / "tokmo-news-replays")
        base.mkdir(parents=True, exist_ok=True)
        output = Path(tempfile.mkdtemp(prefix="run-", dir=base))
        callbacks = {}
        if args.mode == "live":
            from news_decision_models import build_models
            choices = {k: v for k, v in dict(model=args.model, screen_model=args.screen_model,
                analysis_model=args.analysis_model, analysis_reasoning_effort=args.analysis_reasoning_effort).items() if v}
            choices['timeout'] = args.timeout_seconds
            screen, analyze = build_models(**choices)
            callbacks = {"screen": screen, "analyze": analyze}
        elif args.model or args.screen_model or args.analysis_model or args.analysis_reasoning_effort:
            raise ValueError("model selection requires --mode live")
        with tempfile.TemporaryDirectory(prefix="tokmo-news-state-") as temporary:
            state = output / "replay.sqlite3" if args.keep_state else Path(temporary) / "replay.sqlite3"
            report = replay(case, state, mode=args.mode, until=args.until, context_mode=args.context_mode,
                            reuse_unchanged_reviews=args.reuse_unchanged_reviews,
                            scoped_review_reuse=args.scoped_review_reuse, **callbacks)
        report["state_path"] = str(state) if args.keep_state else None
        report["requested_model"] = args.model
        report['requested_analysis_reasoning_effort'] = args.analysis_reasoning_effort
        report['context_mode'] = args.context_mode
        report['reuse_unchanged_reviews'] = args.reuse_unchanged_reviews
        report['scoped_review_reuse'] = args.scoped_review_reuse
        report['model_reasoning_efforts'] = {role:getattr(callback,'reasoning_effort',None)
                                            for role,callback in callbacks.items()}
        report["created_at"] = datetime.now(timezone.utc).isoformat()
        report["code_sha256"] = code_hashes
        report["case_sha256"] = case_hash
        try:
            report["code_revision"] = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parents[1], text=True, stderr=subprocess.DEVNULL).strip()
        except (OSError, subprocess.CalledProcessError):
            report["code_revision"] = "unknown"
        (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n")
        (output / "report.md").write_text(markdown(report))
        print(json.dumps({"passed": report["passed"], "mode": args.mode, "report": str(output / "report.json"), "telegram_sending": "disabled"}))
        return 0 if report["passed"] else 2
    except (ValueError, OSError, KeyError, ImportError) as exc:
        print(f"Replay error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
