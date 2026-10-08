#!/usr/bin/env python3
"""Re-evaluate held news with production models in isolated local stores.

No Telegram sender, operational writes, screening, or automatic retries. The
current evidence/portfolio snapshot is evaluated at the requested morning time.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from news_decision_models import build_models
from tokmo.programs.news_decision_engine import NewsDecisionEngine, RECORDED_TIME, digest_alerts
from tokmo.programs.news_portfolio_context import read_news_portfolio
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--at', required=True)
    parser.add_argument('--exclude-id', action='append', default=[])
    parser.add_argument('--workers', type=int, choices=(1, 2, 3), default=3)
    args = parser.parse_args()
    root, output = args.root.resolve(), args.output.resolve()
    if root in output.parents or output == root or output.exists():
        parser.error('새로운 운영 저장소 밖의 결과 폴더를 지정합니다')
    os.umask(0o077)
    output.mkdir(parents=True, mode=0o700)
    config = json.loads((root / 'news_decision.json').read_text())
    at = timestamp(args.at)
    snapshot_at = datetime.now(timezone.utc).isoformat()
    connection = sqlite3.connect((root / 'stores/news_episodes.sqlite3').as_uri() + '?mode=ro', uri=True)
    try:
        events = [json.loads(row[0]) for row in connection.execute('SELECT state FROM news_decision_events ORDER BY id')]
    finally:
        connection.close()
    candidates = [event for event in events if event.get('held_until')
                  and timestamp(event['held_until']) <= at and event['id'] not in args.exclude_id]
    context = read_news_portfolio(root, config['project_root'], at=at,
                                  followup=config.get('followup_context_enabled') is True)
    snapshot = dict(snapshot_at=snapshot_at, evaluation_at=args.at,
                    candidates=[e['id'] for e in candidates], excluded=args.exclude_id,
                    models={k:config.get(k) for k in ('screen_model','analysis_model','analysis_reasoning_effort','context_mode','timeout_seconds')},
                    context=context, events=events,
                    boundary='현재 확보 자료를 08시 시각으로 평가. 미래 자료 및 새로운 원문 수집 없음. 운영 상태/발송에 영향 없음.')
    (output / 'snapshot.json').write_text(json.dumps(snapshot, ensure_ascii=False))

    def evaluate(event):
        store = NewsDecisionStore(output / (event['id'] + '.sqlite3'))
        report = dict(decisions=[], alerts=[], failures=[], calls=0)
        try:
            for state in events:
                store.save_event(deepcopy(state))
            screen, analyze = build_models(screen_model=config['screen_model'], analysis_model=config['analysis_model'],
                timeout=config['timeout_seconds'], analysis_reasoning_effort=config['analysis_reasoning_effort'])
            engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME,
                context_reader=lambda when: deepcopy(context), context_mode=config.get('context_mode','compact'))
            engine._tick_now = at
            try:
                result = engine._analyze([], deepcopy(event), at, report)
                decision = result['decision']
                error = None
            except Exception as exc:
                decision = None
                error = type(exc).__name__ + ': ' + str(exc)[:300]
            row = dict(event_id=event['id'], original_title=event['decision']['title'],
                       decision=decision, selected=bool(report['alerts']), error=error, result=report)
            (output / (event['id'] + '.json')).write_text(json.dumps(row, ensure_ascii=False, indent=2))
            return row
        finally:
            store.close()

    results = []
    print(json.dumps({'started':snapshot_at,'candidates':len(candidates),'workers':args.workers},ensure_ascii=False),flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(evaluate,event) for event in candidates]
        for future in as_completed(futures):
            row=future.result(); results.append(row)
            print(json.dumps({'done':len(results),'total':len(candidates),'title':row['original_title'],
                              'selected':row['selected'],'error':row['error'],'calls':row['result']['calls']},ensure_ascii=False),flush=True)
    results.sort(key=lambda row:row['event_id'])
    alerts=[alert for row in results for alert in row['result']['alerts']]
    report=dict(snapshot_at=snapshot_at,evaluation_at=args.at,transport='LOCAL_PREVIEW_ONLY',
                candidate_count=len(candidates),selected_count=sum(row['selected'] for row in results),
                errors=sum(row['error'] is not None for row in results),results=results,
                model_calls=sum(row['result']['calls'] for row in results),
                tokens=sum(call.get('usage',{}).get('total_tokens',0) for row in results for call in row['result'].get('model_calls',[])),
                snapshot_sha256=hashlib.sha256((output/'snapshot.json').read_bytes()).hexdigest())
    (output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
    if alerts:
        (output/'proposed-messages.txt').write_text(digest_alerts(alerts)['body']+'\n')
    print(json.dumps({k:report[k] for k in ('candidate_count','selected_count','errors','model_calls','tokens')},ensure_ascii=False),flush=True)
    return 0 if report['errors']==0 else 2


if __name__=='__main__':
    raise SystemExit(main())
