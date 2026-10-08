#!/usr/bin/env python3
"""Finite paired model comparison on saved NEWS requests; no stores or delivery.

Cases: {"cases":[{"id":"...","request":{...},"expect_notify":true}]}
Omit expect_notify for feedback that does not establish a yes/no answer.
"""
import argparse
import copy
import hashlib
import json
from pathlib import Path
import time

from news_decision_models import DecisionModel
from tokmo.programs.news_decision_engine import validate_decision, render_alert
from tokmo.records.news_decision_store import timestamp


def evaluate(cases, model, output, *, timeout=120):
    output = Path(output)
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    rows = []
    source_hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in [
        Path(__file__).resolve(), Path(__file__).with_name('news_decision_models.py'),
        Path(__file__).with_name('subscription_json_model.py')]}
    callback = DecisionModel(model, 'analyze', timeout=timeout)
    for case in cases:
        request = copy.deepcopy(case['request'])
        started = time.monotonic()
        row = dict(id=case['id'], model=model,
            input_sha256=hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False).encode()).hexdigest())
        try:
            raw = callback(request)
            row['response'] = raw
            previous = request.get('events', [None])[0] if request.get('events') else None
            comparison = request.get('comparison', {})
            bodies = [body for body in [(previous or {}).get('last_sent', {}).get('body'),
                *[p.get('body') for p in comparison.get('pending', [])],
                *[p.get('body') for p in comparison.get('other_events', [])]] if isinstance(body, str)]
            decision = validate_decision(raw, request['evidence'], previous,
                baseline_applicable=bool(bodies), require_notify=True, prior_bodies=bodies)
            row.update(valid=True, decision=decision)
            row['preview'] = render_alert(decision, request['evidence'], timestamp(request['now']),
                followup=bool((previous or {}).get('last_sent')))
            if 'expect_notify' in case:
                row['expected_notify'] = case['expect_notify']
                row['expectation_basis'] = case.get('expectation_basis', 'reviewer expectation; user endorsement not established')
                row['notify_matches'] = decision['notify'] == case['expect_notify']
        except Exception as error:
            row.update(valid=False, error=type(error).__name__ + ': ' + str(error)[:250])
        row['elapsed_seconds'] = round(time.monotonic() - started, 3)
        rows.append(row)
        path = output / 'results.json'
        path.write_text(json.dumps(dict(model=model, calls=len(rows), code_sha256=source_hashes, telegram_sending='disabled',
            scope='saved request comparison; not full engine replay', rows=rows), ensure_ascii=False, indent=2) + '\n')
        path.chmod(0o600)
        print(json.dumps({k: row[k] for k in ('id', 'valid', 'notify_matches', 'elapsed_seconds', 'error') if k in row}), flush=True)
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--case', type=Path, required=True)
    parser.add_argument('--model', required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--timeout', type=int, default=120)
    parser.add_argument('--ids', nargs='*')
    args = parser.parse_args()
    cases = json.loads(args.case.read_text())['cases']
    if args.ids:
        cases = [c for c in cases if c['id'] in args.ids]
    if not cases or len(cases) > 50:
        parser.error('provide 1..50 cases per comparison run')
    rows = evaluate(cases, args.model, args.output, timeout=args.timeout)
    return 0 if all(r['valid'] and r.get('notify_matches', True) for r in rows) else 2


if __name__ == '__main__':
    raise SystemExit(main())
