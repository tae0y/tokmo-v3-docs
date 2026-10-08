#!/usr/bin/env python3
"""Quant report CLI: local inputs or explicit snapshots; no automatic send.

Examples:
  --snapshot case.json --model recorded --responses responses.json
  --data-root ... --project-root ... --supplement capture.json --model glm-5.3
  --extra personal-a:personal:KRW:2000000
The extra option applies to this invocation only. Model 'off' is pure calculation.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokmo.values.quant_decisions import parse_snapshot
from tokmo.programs.quant_decision_inputs import load_local_snapshot
from tokmo.programs.quant_decision_analysis import build_report, render_report
from tokmo.records.quant_decision_reports import DecisionReportStore, DecisionDeliveryQueue, atomic_private, meaningful_changes


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--snapshot',type=Path)
    parser.add_argument('--data-root',type=Path,default=Path(__file__).resolve().parents[1]/'var/data')
    parser.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    parser.add_argument('--supplement',type=Path,action='append',default=[])
    parser.add_argument('--pension-baseline',type=Path,help='과거 역산 보유 기준을 명시해 분석. 운영 잔고는 변경하지 않음')
    parser.add_argument('--pension-analysis',type=Path,help='명시적 과거 비교용 연금 분석 manifest. 현재 보유 분석과 구분')
    parser.add_argument('--responses',type=Path)
    parser.add_argument('--model',default='off',help='off/current, recorded, or a current product exact ID/selector from MODEL_ROUTING_STATE.md')
    parser.add_argument('--model-env',type=Path,default=Path.home()/'.hermes/.env')
    parser.add_argument('--timeout',type=int,default=None)
    parser.add_argument('--max-tokens',type=int,default=None)
    parser.add_argument('--max-model-calls',type=int,help='Optional total on-demand physical call budget, including validation rewrites')
    parser.add_argument('--request-context',type=Path,help='JSON list of requests scoped by key/account/scope; analysis only')
    parser.add_argument('--request-only',action='store_true',help='Analyze only explicit analysis_requests; preserve the full portfolio context')
    parser.add_argument('--complete-report',type=Path,help='Complete a prepared report using current-conversation responses; no model/API calls')
    parser.add_argument('--author-model',help='Actual current-conversation model, preserved as caller-declared provenance')
    parser.add_argument('--new-limit',type=int,default=8)
    parser.add_argument('--symbol',action='append',help='Analyze or look up only this instrument, e.g. KR:005380. Repeat to select several.')
    parser.add_argument('--extra',action='append',default=[])
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--lookup',help='Read a saved report ID without analysis')
    parser.add_argument('--periodic',action='store_true')
    parser.add_argument('--purpose',default='ordinary-stock-analysis',help='이번 질문 목적. 목적이 달라지면 이전 판단을 재사용하지 않음')
    parser.add_argument('--last-delivered',help='Report ID successfully delivered before this review')
    parser.add_argument('--compare-after-sales',action='store_true',help='Analyze reinvestment in new candidates after assumed selected sales')
    args=parser.parse_args(argv)
    from quant_decision_models import canonical_model
    if not args.periodic:
        args.model=canonical_model(args.model)
    if args.max_model_calls is not None and (args.max_model_calls<1 or args.periodic):parser.error('call budget is positive and on-demand only')
    if args.periodic and args.model=='current':parser.error('current conversation mode cannot be periodic')
    store=DecisionReportStore(args.output)
    from tokmo.programs.quant_dialogue import prepare_dialogue,complete_dialogue,report_pages
    if args.complete_report:
        if not args.responses or not args.author_model or args.periodic:
            parser.error('completion requires responses and author model; not periodic')
        report=complete_dialogue(json.loads(args.complete_report.read_text()),json.loads(args.responses.read_text()),
            author_model=args.author_model,completed_at=datetime.now(timezone.utc).isoformat())
        path=store.save(report)
        try:atomic_private(args.output/(report['report_id']+'.telegram.json'),{'pages':report_pages(report)})
        except ValueError as error:atomic_private(args.output/(report['report_id']+'.telegram.json'),{'error':str(error)})
        print(render_report(report));print('\n저장: '+str(path))
        return 2 if any(r['status']=='FAILED' for r in report['results']) else 0
    if args.lookup:
        report=store.read(args.lookup)
        if args.symbol:
            report['results']=[r for r in report['results'] if r['key'] in args.symbol]
            if not report['results']:parser.error('보고서에 요청 종목이 없습니다')
        print(render_report(report));return 0
    if args.new_limit<0 or (args.timeout is not None and args.timeout<=0) or (args.max_tokens is not None and args.max_tokens<128):
        parser.error('invalid analysis bounds')
    etf_refresh = {'status': 'SNAPSHOT_REPLAY', 'requests': 0}
    if args.snapshot is None:
        from tokmo.kis_composition import refresh_kis_etf_inputs
        etf_refresh = refresh_kis_etf_inputs(root=args.data_root, at=datetime.now(timezone.utc))
    data=json.loads(args.snapshot.read_text()) if args.snapshot else load_local_snapshot(args.project_root,args.data_root,as_of=datetime.now(timezone.utc),supplement=args.supplement,pension_baseline=args.pension_baseline,pension_analysis=args.pension_analysis,
        historical_pension=bool(args.pension_baseline or args.pension_analysis))
    if args.request_context:
        requests=json.loads(args.request_context.read_text())
        if not isinstance(requests,list):parser.error('request context must be a list')
        data['analysis_requests']=requests
    for text in args.extra:
        try:
            account,scope,currency,amount=text.split(':')
        except ValueError:
            parser.error('--extra is account:scope:currency:amount')
        data.setdefault('extra_capital',[]).append({'account':account,'scope':scope,'currency':currency,'amount':amount})
    snapshot=parse_snapshot(data)
    if args.request_only and not snapshot.analysis_requests:parser.error('--request-only requires analysis_requests')
    model=None
    recorded=json.loads(args.responses.read_text()) if args.responses else None
    if args.model=='recorded' and recorded is None:
        parser.error('recorded mode requires --responses')
    if args.model not in {'off','current','recorded'}:
        from quant_decision_models import QuantModel
        model=QuantModel(env_file=args.model_env,model=args.model,timeout=args.timeout,max_tokens=args.max_tokens,
                         on_demand=not args.periodic,receipt_dir=args.output/'model-receipts',max_calls=args.max_model_calls)
    history_root=(args.data_root/'private/quant-requests').resolve()
    history=DecisionReportStore(history_root) if args.output.resolve().is_relative_to(history_root) else store
    previous_run=(store.previous(before=snapshot.as_of) if args.periodic else None if args.model in {'off','current'} else
                  history.previous_successes(before=snapshot.as_of,purpose=args.purpose))
    report=build_report(snapshot,model=model,new_limit=args.new_limit,recorded=recorded,previous=previous_run,only_keys=args.symbol,purpose=args.purpose,
                        only_requests=args.request_only)
    report['etf_refresh'] = etf_refresh
    # Additional input metadata is part of the immutable report identity too.
    from tokmo.programs.quant_dialogue import fingerprint
    report['report_id']='qd-'+fingerprint({k:v for k,v in report.items() if k!='report_id'})[:16]
    path=store.save(report)
    if args.model in {'off','current'}:
        atomic_private(args.output/(report['report_id']+'.dialogue.json'),prepare_dialogue(report))
    if args.compare_after_sales:
        from tokmo.programs.quant_rotation_scenario import after_selected_sales
        projected,rotation=after_selected_sales(snapshot,report)
        if projected:
            alternative=build_report(projected,model=model,new_limit=args.new_limit,include_holdings=False,
                                     exclude_new=[s['key'] for s in rotation['sold']],purpose=args.purpose)
            store.save(alternative);rotation['alternative_report_id']=alternative['report_id']
            print(render_report(alternative)+'\n')
        atomic_private(args.output/(report['report_id']+'.rotation.json'),rotation)
    if model:
        atomic_private(args.output/(report['report_id']+'.model-calls.json'),model.calls)
    previous=store.read(args.last_delivered) if args.last_delivered else None
    disposition=DecisionDeliveryQueue(args.output).stage(report,now=snapshot.as_of) if args.periodic and not args.last_delivered else meaningful_changes(report,previous,on_demand=not args.periodic)
    atomic_private(args.output/(report['report_id']+'.delivery-preview.json'),disposition)
    rendered=render_report(report)
    if not args.periodic:
        try:atomic_private(args.output/(report['report_id']+'.telegram.json'),{'pages':report_pages(report)})
        except ValueError as error:atomic_private(args.output/(report['report_id']+'.telegram.json'),{'error':str(error)})
    text_path=args.output/(report['report_id']+'.txt')
    fd=__import__('os').open(text_path,__import__('os').O_WRONLY|__import__('os').O_CREAT|__import__('os').O_TRUNC,0o600)
    with __import__('os').fdopen(fd,'w') as stream:stream.write(rendered+'\n')
    print(rendered)
    print('\n저장: '+str(path))
    return 2 if any(r['status']=='FAILED' for r in report['results']) else 0


if __name__=='__main__':
    raise SystemExit(main())
