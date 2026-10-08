#!/usr/bin/env python3
"""Refresh a selected stock's official financial inputs, outside live stores.

Existing OpenDART/SEC read transports and issuer registries are reused.
Output is a Quant supplement, never a write to the existing fundamentals DB.
"""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from tokmo.facts.opendart_live import OpenDartLiveTransport
from tokmo.facts.opendart_source_contract import OpenDartTraceJsonRequest
from tokmo.facts.sec_fundamentals import build_companyfacts_request
from tokmo.facts.sec_fundamentals_live import SECCompanyFactsLiveTransport
from tokmo.runtime.config import load_opendart_config, load_sec_config
from tokmo.programs.quant_decision_fundamentals import dart_metrics, sec_metrics
from tokmo.records.quant_decision_reports import atomic_private


def issuer(root, market, symbol):
    path = root / ('opendart-bulk.sqlite3' if market == 'KR' else 'sec-targeted.sqlite3')
    with sqlite3.connect(path.as_uri()+'?mode=ro',uri=True) as connection:
        if market == 'KR':
            rows=connection.execute('SELECT DISTINCT corp_code FROM disclosures WHERE stock_code=?',(symbol,)).fetchall()
        else:
            rows=connection.execute('SELECT DISTINCT cik FROM registry WHERE upper(ticker)=?',(symbol.upper().replace('.','-'),)).fetchall()
    if len(rows)!=1:raise ValueError('issuer mapping absent or ambiguous')
    return rows[0][0]


def main(argv=None):
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--data-root',type=Path,default=Path(__file__).resolve().parents[1]/'var/data')
    p.add_argument('--credentials',type=Path,help='Optional override for both existing source config files')
    p.add_argument('--dart-config',type=Path,default=Path.home()/'.config/tokmo-v3/opendart.env')
    p.add_argument('--sec-config',type=Path,default=Path.home()/'.config/tokmo-v3/sec.env')
    p.add_argument('--symbols',required=True,help='KR:005380,US:NVDA')
    p.add_argument('--year',type=int,help='KR fiscal year; default previous calendar year')
    p.add_argument('--output',required=True,type=Path)
    p.add_argument('--source-cache',type=Path,help='Optional private copy of the official response for later offline checks')
    args=p.parse_args(argv);now=datetime.now(timezone.utc);result={'instruments':[],'calls':[],'captured_at':now.isoformat()}
    registry=None
    for key in dict.fromkeys(args.symbols.split(',')):
        market,symbol=key.split(':');call={'key':key,'status':'PREPARING','provider_started':False};result['calls'].append(call)
        try:
            try:
                identity=issuer(args.data_root,market,symbol)
            except ValueError:
                if market!='US':raise
                # Internal registry covers selected names only. Use the
                # existing official ticker-map transport once for this run.
                if registry is None:
                    from tokmo.facts.sec import build_tickers_request
                    from tokmo.facts.sec_live import SecLiveTransport
                    reply=SecLiveTransport()(build_tickers_request(user_agent=load_sec_config(args.credentials or args.sec_config).user_agent))
                    if not hasattr(reply,'body'):raise ValueError('official SEC ticker map unavailable')
                    registry=json.loads(reply.body)
                rows=[row for row in registry.values() if row.get('ticker')==symbol.upper().replace('.','-')]
                if len(rows)!=1:raise ValueError('official issuer mapping absent or ambiguous')
                identity=str(rows[0]['cik_str']).zfill(10)
                call['mapping_source']='https://www.sec.gov/files/company_tickers.json'
                call['company_name']=rows[0]['title']
            if market=='KR':
                request=OpenDartTraceJsonRequest(endpoint='fnlttSinglAcnt',corp_code=str(identity),business_year=str(args.year or now.year-1),
                                                api_key=load_opendart_config(args.credentials or args.dart_config).api_key)
                call['provider_started']=True
                reply=OpenDartLiveTransport().trace_json(request)
                if not hasattr(reply,'body'):raise ValueError(getattr(reply,'code','provider_failure'))
                payload=json.loads(reply.body)
                metrics,evidence=dart_metrics(payload,corp_code=str(identity),year=args.year or now.year-1,as_of=now)
            elif market=='US':
                request=build_companyfacts_request(cik=str(identity).zfill(10),user_agent=load_sec_config(args.credentials or args.sec_config).user_agent)
                call['provider_started']=True
                reply=SECCompanyFactsLiveTransport()(request)
                if not hasattr(reply,'body'):raise ValueError(getattr(reply,'code','provider_failure'))
                payload=json.loads(reply.body)
                metrics,evidence=sec_metrics(payload,as_of=now)
            else:raise ValueError('unsupported market')
            if args.source_cache:
                atomic_private(args.source_cache/(market+'-'+symbol+'.json'),{'captured_at':now.isoformat(),'payload':payload})
            result['instruments'].append({'symbol':symbol,'market':market,'metrics':metrics,'evidence':evidence})
            call.update(status='COLLECTED' if metrics else 'NO_METRICS',metrics=len(metrics))
        except Exception as error:
            call.update(status='FAILED',error_class=type(error).__name__)
    atomic_private(args.output,result)
    print(json.dumps({'output':str(args.output),'calls':result['calls']},ensure_ascii=False))
    return 2 if any(c['status']=='FAILED' for c in result['calls']) else 0


if __name__=='__main__':raise SystemExit(main())
