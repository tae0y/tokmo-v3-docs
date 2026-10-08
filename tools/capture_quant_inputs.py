#!/usr/bin/env python3
"""One small Quant input job using the existing collector's token.

No token issuance or renewal. The collector supplies the bearer over stdin;
the user is not asked to copy a token. A same-process integration can call
capture_job directly with its existing authenticated GET reader instead.
Output is a private supplement cache, never the production holdings DB.
"""
from __future__ import annotations
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))

from tokmo.facts.toss_reads_live import TossReadsLiveTransport
from tokmo.programs.quant_decision_inputs import load_local_snapshot,validate_history_request,HistoryRequestError
from tokmo.programs.quant_input_capture import ExistingTokenReader,capture_job,read_existing_account_refs


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root',type=Path,default=Path(__file__).resolve().parents[1]/'var/data')
    p.add_argument('--project-root',type=Path,default=Path(__file__).resolve().parents[1])
    p.add_argument('--pension-analysis',type=Path)
    p.add_argument('--token-stdin',action='store_true',required=True)
    p.add_argument('--job',choices=('funds','orders','sellable','candles'),default='funds')
    p.add_argument('--offset',type=int,default=0)
    p.add_argument('--batch',type=int,help='기본 candles=1, 다른 작업=3; 호출 예산은 별도 유지')
    history=p.add_mutually_exclusive_group()
    history.add_argument('--history-start',help='완료 일봉 시작일 YYYY-MM-DD; 미지정은 직전 달력 1년')
    history.add_argument('--history-sessions',type=int,help='짧거나 긴 완료 거래일 개수를 명시')
    p.add_argument('--history-reason',help='이번 분석의 기간 선택 이유')
    p.add_argument('--symbol',action='append',help='조회 대상 KR:005380 또는 US:TSLA; 반복 가능')
    p.add_argument('--max-calls',type=int,default=4)
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    now=datetime.now(timezone.utc)
    selected=None
    if args.symbol:
        if args.job!='candles':p.error('--symbol is only for candles')
        import re
        if any(not re.fullmatch(r'(KR|US):[A-Za-z0-9.-]{1,24}',s) for s in args.symbol):p.error('invalid symbol')
        selected=[dict(market=s.split(':')[0],symbol=s.split(':')[1]) for s in dict.fromkeys(args.symbol)]
        if args.offset or (args.batch is not None and args.batch < len(selected)):
            p.error('explicit symbols require offset=0 and a batch covering every symbol')
        args.batch=len(selected)
    if args.batch is None: args.batch=1 if args.job=='candles' else 3
    if args.offset < 0 or args.batch < 1 or args.max_calls < 1:p.error('invalid capture bounds')
    try:
        validate_history_request(history_start=args.history_start, history_sessions=args.history_sessions,
                                 instruments=selected or (), now=now)
    except HistoryRequestError as error:
        p.error(str(error))
    token=sys.stdin.readline(8192).strip()
    if not token:p.error('existing token required')
    data=load_local_snapshot(args.project_root,args.data_root,as_of=now,pension_analysis=args.pension_analysis)
    if selected is not None:data['instruments']=selected
    refs=(None if args.job=='candles' else
          read_existing_account_refs(args.data_root/'ops/account_native_refs.sqlite3',clock=lambda:now))
    reader=ExistingTokenReader(token=token,refs=refs,transport=TossReadsLiveTransport(),
        clock=lambda:datetime.now(timezone.utc),max_calls=args.max_calls)
    result=capture_job(reader,data=data,path=args.output,job=args.job,now=now,offset=args.offset,batch=args.batch,
        history_start=args.history_start,history_sessions=args.history_sessions,history_reason=args.history_reason)
    if args.job=='candles':
        result['requested_symbols']=[i['market']+':'+i['symbol'] for i in data['instruments'][args.offset:args.offset+args.batch]]
        result['incomplete_symbols']=[key for key in result['requested_symbols']
            if any(f.get('key',key)==key for f in result['failures'])]
    print(json.dumps(result,ensure_ascii=False))
    return 2 if result['failures'] else 0

if __name__=='__main__':raise SystemExit(main())
