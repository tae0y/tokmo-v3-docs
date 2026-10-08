#!/usr/bin/env python3
"""Read-only observation report, including token use and actual send latency."""
import argparse
from contextlib import closing
import json
import math
from pathlib import Path
import sqlite3
from collections import Counter

from tokmo.records.news_decision_store import timestamp


def connect(path):
    connection = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA query_only=ON")
    return connection


def usage_by_model(calls):
    """Provider-reported cache subsets only; reservations never become usage."""
    groups = {}
    for row in calls:
        response = json.loads(row.get('response') or '{}')
        usage = response.get('_usage') or {}
        key = (response.get('_model', 'unknown'), row['stage'], response.get('_reasoning_effort'))
        value = groups.setdefault(key, dict(model=key[0], stage=key[1], reasoning_effort=key[2],
            calls=0, usage_reported_calls=0, input_tokens=0, output_tokens=0, total_tokens=0,
            cache_reported_calls=0, cache_unknown_calls=0, cached_input_tokens=0,
            cache_measured_input_tokens=0, uncached_input_tokens_measured=0))
        value['calls'] += 1
        number = lambda n: type(n) is int and n >= 0
        actual = row['status'] == 'OK' and all(number(usage.get(k)) for k in ('prompt_tokens','completion_tokens','total_tokens'))
        if actual:
            value['usage_reported_calls'] += 1
            for dest, source in [('input_tokens','prompt_tokens'), ('output_tokens','completion_tokens'), ('total_tokens','total_tokens')]:
                value[dest] += usage[source]
        details = usage.get('prompt_tokens_details') or {}
        cached = details.get('cached_tokens') if isinstance(details, dict) else None
        if actual and number(cached) and cached <= usage['prompt_tokens']:
            value['cache_reported_calls'] += 1
            value['cached_input_tokens'] += cached
            value['cache_measured_input_tokens'] += usage['prompt_tokens']
            value['uncached_input_tokens_measured'] += usage['prompt_tokens'] - cached
        else:
            value['cache_unknown_calls'] += 1
    for value in groups.values():
        denominator = value['cache_measured_input_tokens']
        value['cache_hit_ratio_measured'] = value['cached_input_tokens'] / denominator if denominator else None
    return list(groups.values())


def report(state, ops=None, *, since, until=None, diagnostics=None):
    start = timestamp(since)
    end = timestamp(until) if until else None
    def in_window(value):
        return bool(value) and timestamp(value) >= start and (end is None or timestamp(value) < end)
    with closing(connect(state)) as connection:
        # Stored times mix legacy UTC text and KST text; prefilter by instant, never by string.
        calls = [dict(row) for row in connection.execute("SELECT id,stage,status,tokens,started_at,response,json_extract(request,'$.review_reason') review_reason,CASE WHEN json_type(response,'$._usage.total_tokens')='integer' THEN json_extract(response,'$._usage.total_tokens') END AS actual FROM news_decision_calls WHERE julianday(started_at)>=julianday(?)", (start.isoformat(),)) if in_window(row["started_at"])]
        states = {}
        reuse_misses = Counter()
        reuse_count = source_noops = 0
        for row in connection.execute('SELECT state FROM news_decision_events'):
            event = json.loads(row['state'])
            reuse_misses.update(event.get('review_reuse_misses', {}))
            reuse_count += event.get('review_reuse_count', 0)
            source_noops += event.get('source_restore_noop_count', 0)
        for row in connection.execute("SELECT state,observed_at FROM news_decision_inputs WHERE julianday(observed_at)>=julianday(?)", (start.isoformat(),)):
            if in_window(row["observed_at"]):
                states[row["state"]] = states.get(row["state"], 0) + 1
    actual_calls = [row for row in calls if row["status"] == "OK" and type(row["actual"]) is int and row["actual"] >= 0]
    result = {"since": start.isoformat(), "input_states": states, "model_calls": len(calls),
              "reuse_cumulative": dict(reused=reuse_count, misses=dict(reuse_misses), source_restore_noops=source_noops,
                  note='현재 사건에 보존된 누적 계측. since/until 기간 집계나 절감 토큰 추정이 아님.'),
              "usage_by_model": usage_by_model(calls),
              "tokens_actual_or_reserved_on_failure": sum(row["tokens"] for row in calls),
              "tokens_reported": sum(row["actual"] for row in actual_calls),
              "tokens_reserved_or_estimated": sum(row["tokens"] for row in calls) - sum(row["tokens"] for row in actual_calls),
              "failed_model_calls": sum(row["status"] not in ("OK", "STARTED") for row in calls),
              "in_progress_model_calls": sum(row["status"] == "STARTED" for row in calls),
              "sent_alerts": 0,
              "delivery_parts": {}, "urgent_collected_to_sent_seconds": {"samples": 0, "p95": None},
              "note": "No ground-truth precision/recall claim. No major event means live detection quality is unproven."}
    if end:
        result["until"] = end.isoformat()
    groups = {}
    if ops is not None:
        with closing(connect(ops)) as connection:
            rows = [dict(row) for row in connection.execute("SELECT * FROM alert_outbox WHERE alert_id LIKE 'nda-%'")]
        latencies = []
        virtual_latencies = []
        for row in rows:
            status = row["status"]
            if in_window(row["created_at"]):
                result["delivery_parts"][status] = result["delivery_parts"].get(status, 0) + 1
            evidence = json.loads(row["body_evidence_json"])
            proposal = next((part.removeprefix("news_proposal_id:") for part in evidence if part.startswith("news_proposal_id:")), row["alert_id"])
            groups.setdefault(proposal, []).append(row)
        for group in groups.values():
            if not all(row["status"] == "SENT" and row["delivered_at"] for row in group):
                continue
            delivered = max(timestamp(row["delivered_at"]) for row in group)
            if not in_window(delivered):
                continue
            simulated = any(str(row["provider_message_id"]).startswith("SIMULATED-") for row in group)
            if not simulated:
                result["sent_alerts"] += 1
            if group[0]["severity"] == "NEWS_IMMEDIATE":
                evidence = json.loads(group[0]["body_evidence_json"])
                observed = next((part.removeprefix("news_observed_at:") for part in evidence if part.startswith("news_observed_at:")), None)
                if observed:
                    target = virtual_latencies if simulated else latencies
                    target.append((delivered - timestamp(observed)).total_seconds())
        if latencies:
            result["urgent_collected_to_sent_seconds"] = {"samples": len(latencies), "p95": sorted(latencies)[math.ceil(len(latencies) * .95) - 1], "max": max(latencies)}
        if virtual_latencies:
            result["simulated_delivery_samples"] = len(virtual_latencies)
            result["note"] += " Simulated delivery is excluded from operational latency."
    if diagnostics is not None:
        from tokmo.programs.news_usage_report import usage_report
        result['usage_diagnostics'] = usage_report(diagnostics, calls, since=since, until=until)
        ids = {key for row in result['usage_diagnostics']['calls'] for key in row['proposal_ids']}
        result['usage_diagnostics']['proposal_delivery'] = {
            key: dict(outbox_checked=ops is not None,
                      parts=[dict(status=r['status'], delivered_at=r['delivered_at'],
                                  simulated=str(r['provider_message_id']).startswith('SIMULATED-'))
                             for r in groups.get(key, [])]) for key in ids}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state", required=True, type=Path)
    parser.add_argument("--ops", type=Path)
    parser.add_argument("--since", required=True)
    parser.add_argument("--until")
    parser.add_argument("--diagnostics", type=Path)
    args = parser.parse_args(argv)
    print(json.dumps(report(args.state, args.ops, since=args.since, until=args.until, diagnostics=args.diagnostics), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
