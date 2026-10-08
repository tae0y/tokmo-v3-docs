#!/usr/bin/env python3
"""Evaluate saved decision cases with a selected model; no account/network writes.

Each case points to a snapshot plus independent evidence updates and expected
action sets. A passing action set does not certify investment performance;
the saved Korean reasoning is also reviewed by a person.
"""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'src'))
from tokmo.values.quant_decisions import parse_snapshot
from tokmo.programs.quant_decision_analysis import build_report,render_report
from tokmo.records.quant_decision_reports import DecisionReportStore,atomic_private


def evaluate(path,output,model):
    case=json.loads(Path(path).read_text())
    data=json.loads((Path(path).parent/case['base']).read_text())
    for field,value in case.get('replace',{}).items():data[field]=deepcopy(value)
    instruments={f"{i['market']}:{i['symbol']}":i for i in data['instruments']}
    for key,patch in case.get('instrument_updates',{}).items():instruments[key].update(deepcopy(patch))
    report=build_report(parse_snapshot(data),model=model,new_limit=case.get('new_limit',8))
    DecisionReportStore(output).save(report)
    checks=[]
    for key,allowed in case['expected_actions'].items():
        rows=[r for r in report['results'] if r['key']==key]
        actions=[r['analysis']['action'] for r in rows if r['analysis']]
        checks.append({'key':key,'actions':actions,'allowed':allowed,'passed':bool(actions) and all(a in allowed for a in actions)})
    return {'case':case['name'],'purpose':case['purpose'],'classification':case['classification'],
        'report_id':report['report_id'],'checks':checks,'passed':all(c['passed'] for c in checks),
        'decisions':[{'key':r['key'],'analysis':r['analysis'],'status':r['status'],'errors':r['errors'],
                      'option':r['selected_option']} for r in report['results']]}


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--cases',type=Path,nargs='+',required=True)
    p.add_argument('--model',default='glm-5.3')
    p.add_argument('--model-env',type=Path,default=Path.home()/'.hermes/.env')
    p.add_argument('--output',type=Path,required=True)
    args=p.parse_args(argv)
    from quant_decision_models import QuantModel
    model=QuantModel(env_file=args.model_env,model=args.model)
    results=[]
    for path in args.cases:
        result=evaluate(path,args.output,model);results.append(result)
        atomic_private(args.output/'evaluation.json',{'cases':results,'model_calls':model.calls,'actual_sends':0})
        print(json.dumps({'case':result['case'],'passed':result['passed'],'checks':result['checks']},ensure_ascii=False),flush=True)
    return 0 if all(r['passed'] for r in results) else 2

if __name__=='__main__':raise SystemExit(main())
