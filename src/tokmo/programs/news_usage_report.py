"""Join optional diagnostics to the authoritative call ledger; read only."""

import json
from collections import Counter, defaultdict
from datetime import timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.news_usage_diagnostics import usage_view
from tokmo.records.news_decision_store import timestamp


def usage_report(directory, calls, *, since, until=None):
    start = timestamp(since)
    end = timestamp(until) if until else None

    def in_window(at):
        return (
            bool(at) and timestamp(at) >= start and (end is None or timestamp(at) < end)
        )

    by_call = defaultdict(list)
    gates, runs, prompts, finishes = {}, {}, {}, {}
    malformed = 0
    low_date = (
        (start.astimezone(ZoneInfo("Asia/Seoul")) - timedelta(days=1))
        .date()
        .isoformat()
    )
    high_date = (
        end.astimezone(ZoneInfo("Asia/Seoul")).date().isoformat() if end else "9999"
    )
    for file in sorted(Path(directory).glob("*.jsonl")):
        if not low_date <= file.stem <= high_date:
            continue
        try:
            handle = file.open("rb")
        except OSError:
            malformed += 1
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict) or row.get("schema_version") != 1:
                        raise ValueError("unsupported diagnostic row")
                    kind = row["kind"]
                    if row.get("call_id"):
                        by_call[row["call_id"]].append(row)
                    if kind == "review_gate" and in_window(row.get("at")):
                        gates[row["gate_id"]] = row
                    if kind in {"run_started", "run_finished", "run_failed"}:
                        runs.setdefault(row["run_id"], {})[kind] = row
                    if kind == "evaluation_finished":
                        finishes[row.get("evaluation_id")] = row
                except (ValueError, TypeError, KeyError, UnicodeError):
                    malformed += 1
    totals = Counter()
    origins = defaultdict(Counter)
    reasons = defaultdict(Counter)
    models = defaultdict(Counter)
    rows = []
    selected = {r["id"]: r for r in calls}
    for key, row in selected.items():
        data = by_call.get(key, [])
        try:
            response = json.loads(row.get("response") or "{}")
            response = response if isinstance(response, dict) else {}
        except (ValueError, TypeError):
            response = {}
        ledger = usage_view(response.get("_usage"))
        observed = [u for r in data if (u := usage_view(r.get("usage"))) is not None]
        core = lambda u: tuple(
            u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        )
        conflict = len({core(u) for u in ([ledger] if ledger else []) + observed}) > 1
        # Missing cache/reasoning subsets do not turn the same total into a second bill.
        used = ledger or (max(observed, key=len) if observed and not conflict else None)
        origin_row = next((r for r in data if r["kind"] == "call_started"), {})
        purpose = origin_row.get("purpose", "unknown")
        reason = (
            row.get("review_reason") or origin_row.get("review_reason") or row["stage"]
        )
        final = finishes.get(origin_row.get("evaluation_id"), {})
        model = response.get("_model") or next(
            (r.get("model") for r in data if r.get("model")), "unknown"
        )
        item = dict(
            call_id=key,
            purpose=purpose,
            stage=row["stage"],
            review_reason=reason,
            measurement="ledger" if ledger else "diagnostic" if used else "unknown",
            usage=used,
            diagnostic_present=bool(data),
            usage_conflict=conflict,
            evaluation_accepted=final.get("accepted"),
            model=model,
            call_status=row["status"],
            parent_call_id=origin_row.get("parent_call_id"),
            evaluation_id=origin_row.get("evaluation_id"),
            proposal_ids=final.get("proposal_ids", []),
            decision_change=final.get("change"),
            decision_notify=final.get("notify"),
            recovery_attribution=origin_row.get("recovery_attribution"),
        )
        rows.append(item)
        totals["calls"] += 1
        totals["missing_call_diagnostics"] += not bool(origin_row)
        totals["usage_conflicts"] += conflict
        if used:
            totals["usage_known_calls"] += 1
            totals["total_tokens"] += used["total_tokens"]
            origins[purpose]["total_tokens"] += used["total_tokens"]
            reasons[reason]["total_tokens"] += used["total_tokens"]
            models[model]["total_tokens"] += used["total_tokens"]
            totals["supplemental_tokens"] += used["total_tokens"] if not ledger else 0
            for part in (
                "prompt_tokens",
                "completion_tokens",
                "cached_input_tokens",
                "reasoning_tokens",
            ):
                if part in used:
                    totals[part] += used[part]
        else:
            totals["usage_unknown_calls"] += 1
            origins[purpose]["usage_unknown_calls"] += 1
            reasons[reason]["usage_unknown_calls"] += 1
            models[model]["usage_unknown_calls"] += 1
        origins[purpose]["calls"] += 1
        reasons[reason]["calls"] += 1
        models[model]["calls"] += 1
        prompt = next((r for r in data if r["kind"] == "prompt"), None)
        if prompt:
            prompts[key] = {
                k: prompt[k]
                for k in (
                    "scope",
                    "size",
                    "areas",
                    "schema_size",
                    "request_size",
                    "policy_and_framing_chars",
                )
            }
    orphan_ids = [
        key
        for key, rs in by_call.items()
        if key not in selected
        and any(r["kind"] == "call_started" and in_window(r.get("at")) for r in rs)
    ]
    relevant_runs = {r["run_id"] for key in selected for r in by_call.get(key, [])}
    relevant_runs.update(r["run_id"] for r in gates.values())
    for key, value in runs.items():
        if in_window(value.get("run_started", {}).get("at")):
            relevant_runs.add(key)
    run_gaps = sum("run_finished" not in runs.get(key, {}) for key in relevant_runs)
    logging_errors = sum(
        runs.get(key, {}).get("run_finished", {}).get("logging_errors", 0)
        for key in relevant_runs
    )
    return dict(
        totals=dict(totals),
        by_purpose={k: dict(v) for k, v in origins.items()},
        by_model={k: dict(v) for k, v in models.items()},
        by_reason={k: dict(v) for k, v in reasons.items()},
        review_outcomes=dict(Counter(r["outcome"] for r in gates.values())),
        change_fields=dict(
            Counter(p for r in gates.values() for p in r["changes"].get("fields", []))
        ),
        gate_baselines=dict(Counter(r["changes"]["status"] for r in gates.values())),
        malformed_lines=malformed,
        incomplete_runs=run_gaps,
        logging_errors=logging_errors,
        failed_runs=sum("run_failed" in runs.get(key, {}) for key in relevant_runs),
        orphan_call_ids=orphan_ids,
        calls=rows,
        prompt_sizes=prompts,
        note="Known usage counted once per call; missing usage is unknown. Cache/reasoning are subsets. Sizes are characters/bytes, not token allocations. Reasons/fields may overlap. Proposal references and evaluation results can repeat across correction calls; they are not send counts.",
    )
