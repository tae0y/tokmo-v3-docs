#!/usr/bin/env python3
"""One stored disclosure listing through screen, enrichment and reanalysis.

Public sources were acquired earlier. This is a current-time integration
case, not proof the document was available at its publication time.
"""
import argparse
from datetime import datetime,timezone
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tokmo.records.news_decision_store import NewsDecisionStore
from tokmo.programs.news_decision_engine import NewsDecisionEngine
from news_decision_models import build_models


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--case',required=True,type=Path)
    p.add_argument('--document',required=True,type=Path)
    p.add_argument('--receipt',required=True)
    p.add_argument('--output',required=True,type=Path)
    a=p.parse_args()
    if a.output.exists() and any(a.output.iterdir()):p.error('이전 검증 자료를 덮어쓰지 않습니다')
    a.output.mkdir(parents=True,exist_ok=True)
    case=json.loads(a.case.read_text());body=json.loads(a.document.read_text())
    listing=next(row['observation'] for row in case['observations'] if row['observation']['id'].startswith('dart:'+a.receipt+':'))
    reads=[]
    def read(receipt):
        if receipt!=body['receipt']:raise ValueError('다른 공시 원문입니다')
        reads.append(receipt)
        return body
    store=NewsDecisionStore(a.output/'news.sqlite3')
    report={'scope':'current-time original-document integration; stored public source',
            'delivery':'UNAVAILABLE','historical_day_comparison':False}
    try:
        engine=NewsDecisionEngine(store,*build_models(),clock=lambda:datetime.now(timezone.utc),document_reader=read)
        report['listing']=engine.tick([listing],datetime.now(timezone.utc))
        report['enriched']=engine.tick([],datetime.now(timezone.utc))
        report['events']=store.events();report['source_reads']=reads
        report['calls']=store.connection.execute('select stage,status,count(*),sum(tokens) from news_decision_calls group by stage,status').fetchall()
        report['pass']=bool(len(reads)==1 and not report['listing']['failures'] and not report['enriched']['failures'] and
                            any(e['decision'].get('notify') and e.get('source_enrichment',{}).get('status')=='READY' for e in report['events']))
    finally:store.close()
    path=a.output/'report.json';path.write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n');path.chmod(0o600)
    print(json.dumps({k:report[k] for k in ('pass','calls','source_reads')},ensure_ascii=False))
    return 0 if report['pass'] else 2


if __name__=='__main__':raise SystemExit(main())
