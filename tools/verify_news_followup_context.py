#!/usr/bin/env python3
"""Two small NEWS checks after a synthetic follow-up was delivered locally."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tokmo.records.investment_followup import read_news_context
from tokmo.records.news_decision_store import NewsDecisionStore,timestamp
from tokmo.programs.news_decision_engine import NewsDecisionEngine,RECORDED_TIME
from news_decision_models import build_models


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--replay-dir',required=True,type=Path)
    p.add_argument('--branch',default='cash')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--case',choices=['repeat','new-urgent'],action='append')
    a=p.parse_args()
    prior=json.loads((a.replay_dir/'report.json').read_text())
    if prior.get('synthetic') is not True or prior.get('transport')!='LOCAL_REPLAY_ONLY':
        p.error('가상 계좌의 로컬 전달 재현 결과만 사용합니다')
    if a.branch not in {b['id'] for b in prior['branches']}:p.error('분기가 없습니다')
    if a.output.exists() and any(a.output.iterdir()):p.error('이전 결과를 덮어쓰지 않습니다')
    a.output.mkdir(parents=True,exist_ok=True)
    state=a.replay_dir/a.branch/'followup.sqlite3'
    cases=[('repeat','2026-09-07T17:00:00+09:00','휴전 이행 확인…해상 운송 재개·원유 공급 차질 완화',
            '합성 검증: 휴전 합의 후 실제 공격 중단이 관측됐다. 해상 운송이 재개됐고 원유 공급 차질이 완화됐다. 단기 금융시장 유동성도 회복됐으며 휴전 지속 여부는 앞으로 확인해야 한다.',False),
           ('new-urgent','2026-09-07T23:10:00+09:00','새로운 대형 은행 유동성 위기',
            '합성 검증: 기존 전쟁 휴전과 별개로 여러 주요 은행이 지금 예금 인출을 중단했다. 중앙은행이 긴급 유동성 지원을 발표했고 주요 주가지수 선물이 갑자기 급락 중이다. 이전 후속 분석 이후 새롭게 확인된 사건이다.',True)]
    report={'synthetic':True,'real_messages':0,'cases':[]}
    for key,when,title,body,expected in cases:
        if a.case and key not in a.case:continue
        at=timestamp(when)
        context={'portfolio_status':'UNAVAILABLE','investment_followup':read_news_context(state,at=at)}
        store=NewsDecisionStore(a.output/(key+'.sqlite3'))
        try:
            engine=NewsDecisionEngine(store,*build_models(),clock=RECORDED_TIME,
                                     context_reader=lambda t:context,token_budget=60000)
            observation={'id':key,'observed_at':when,'title':title,'text':body,'source_kind':'NEWS',
                         'publisher':'합성 검증 발표','url':'https://example.invalid/'+key}
            result=engine.tick([observation],at)
            passed=not result['failures'] and bool(result['alerts'])==expected
            if expected:passed=passed and any(e['decision']['urgency']=='IMMEDIATE' for e in store.events())
            item={'id':key,'pass':passed,'expected_alert':expected,'result':result,'events':store.events(),
                  'calls':store.connection.execute('SELECT stage,status,count(*),sum(tokens) FROM news_decision_calls GROUP BY stage,status').fetchall()}
            report['cases'].append(item)
            print(json.dumps({'case':key,'pass':passed,'alerts':len(result['alerts']),'failures':result['failures']},ensure_ascii=False),flush=True)
        finally:store.close()
    report['pass']=all(c['pass'] for c in report['cases'])
    (a.output/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n')
    for path in a.output.iterdir():
        if path.is_file():path.chmod(0o600)
    return 0 if report['pass'] else 2


if __name__=='__main__':raise SystemExit(main())
