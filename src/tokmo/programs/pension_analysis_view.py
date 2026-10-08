"""Explicit dated pension analysis view; never changes the ACTIVE registry."""
from datetime import datetime,time
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from tokmo.values.quant_decisions import instant,number


def load_baseline(annotation_path, *, as_of):
    annotation=json.loads(Path(annotation_path).read_text())
    if annotation.get('readback')!='exact_match' or annotation.get('actual_broker_capture_date')!='unknown':
        raise ValueError('지원하는 역산 보유 기준 자료가 아닙니다')
    if instant(annotation['recorded_at'])>instant(as_of):
        raise ValueError('당시 아직 확보하지 않은 보유 자료입니다')
    source_at=datetime.combine(datetime.fromisoformat(annotation['as_of_date']).date(),time.min,ZoneInfo('Asia/Seoul'))
    if source_at>instant(as_of):raise ValueError('미래 보유 기준 자료입니다')
    connection=sqlite3.connect(Path(annotation['snapshot_store']).resolve().as_uri()+'?mode=ro',uri=True)
    connection.row_factory=sqlite3.Row
    try:
        header=connection.execute('SELECT source FROM holdings_snapshots WHERE snapshot_id=?',(annotation['snapshot_id'],)).fetchone()
        if not header or header['source']!='pension-excel-v1':raise ValueError('연금 기준 스냅샷을 찾을 수 없습니다')
        items=[dict(r) for r in connection.execute('SELECT symbol,quantity,avg_price,currency,name,market_raw FROM holdings_snapshot_items WHERE snapshot_id=? ORDER BY symbol',(annotation['snapshot_id'],))]
    finally:connection.close()
    if len(items)!=annotation['holdings_count']:raise ValueError('보유 기준 자료의 종목 수가 다릅니다')
    return {'positions':items,'source_at':source_at.isoformat(),
            'coverage_through':source_at.replace(hour=23,minute=59,second=59,microsecond=999999).isoformat(),
            'source_precision':'date','recorded_at':annotation['recorded_at'],
            'basis':'HISTORICAL_RECONCILED','label':annotation['label'],'snapshot_id':annotation['snapshot_id'],
            'current_quantity_confirmed':False}


def reconcile_quantities(baseline, executions, *, through, complete_from, complete_through):
    """An explicitly covered execution extract may extend a dated baseline.

    An empty local ledger is not evidence of no trades. Coverage must come
    from its source/export or an explicit user no-trades confirmation.
    Reversed/corrected events must first be resolved by the existing ledger.
    """
    start=instant(baseline.get('coverage_through') or baseline['source_at']);end=instant(through)
    if end<start or instant(complete_from)>start or instant(complete_through)<end:
        raise ValueError('보유 기준 이후 전체 거래 기간이 확인되지 않았습니다')
    rows={r['symbol']:dict(r) for r in baseline['positions']}
    seen=set()
    for trade in sorted(executions,key=lambda e:instant(e['executed_at'])):
        at=instant(trade['executed_at'])
        if not start<at<=end:continue
        if trade['id'] in seen:raise ValueError('중복 체결입니다')
        seen.add(trade['id'])
        if trade.get('side') not in {'BUY','SELL'}:raise ValueError('매수/매도 방향 미확인')
        qty=number(trade['quantity'],minimum=Decimal('0.00000001'))
        price=number(trade['price'],minimum=0)
        row=rows.setdefault(trade['symbol'],dict(symbol=trade['symbol'],quantity='0',avg_price='0',currency=trade['currency'],name=trade.get('name',trade['symbol']),market_raw='KR'))
        before=number(row['quantity'],minimum=0);average=number(row['avg_price'],minimum=0)
        after=before+qty if trade['side']=='BUY' else before-qty
        if after<0:raise ValueError('보유량보다 많은 매도: 거래 누락/기준일을 확인해 주세요')
        if trade['currency']!=row['currency']:raise ValueError('체결 통화 불일치')
        row['quantity']=str(after)
        if trade['side']=='BUY':row['avg_price']=str((before*average+qty*price)/after)
    return dict(baseline,positions=[r for r in rows.values() if number(r['quantity'])>0],
                verified_through=end.isoformat(),current_quantity_confirmed=False,
                basis='RECONCILED_WITH_COVERED_EXECUTIONS',execution_ids=sorted(seen))


def apply_view(data, view):
    accounts={p['account'] for p in data['positions'] if p['scope']=='retirement'}
    accounts.update(a['account'] for a in data.get('accounts',[]) if a['scope']=='retirement')
    if len(accounts)!=1:raise ValueError('연금 분석 대상 계좌가 하나로 정해지지 않았습니다')
    account=next(iter(accounts));instruments={i['market']+':'+i['symbol']:i for i in data['instruments']}
    if view.get('account') and view['account']!=account:
        raise ValueError('연금 분석 자료의 계좌가 현재 계좌와 다릅니다')
    positions=[p for p in data['positions'] if p['scope']!='retirement']
    for row in view['positions']:
        key='KR:'+row['symbol']
        instruments.setdefault(key,dict(symbol=row['symbol'],market='KR',currency=row['currency'],name=row.get('name') or row['symbol'],kind='etf',evidence=[],price=None,price_at=None))
        positions.append(dict(account=account,scope='retirement',key=key,quantity=row['quantity'],average_cost=row['avg_price'],
            sellable=None,observed_at=view.get('verified_through') or view['source_at'],captured_at=data['as_of'],
            source_status=view['basis'],quantity_status=row.get('quantity_status','DATED')))
    data['positions']=positions;data['instruments']=list(instruments.values())
    data['pension_analysis_view']={k:v for k,v in view.items() if k!='positions'}
    note='거래 반영 계산 수량 · 새 계좌현황 도착 시 대조' if view['basis']=='TRANSACTIONS_DERIVED' else (
        '이후 거래 범위 확인 완료' if view['basis']=='RECONCILED_WITH_COVERED_EXECUTIONS' else '과거 보유 기준이며 이후 거래는 아직 이 분석에 연결되지 않음')
    data.setdefault('limitations',[]).append('퇴직연금 분석 보유량: '+view['label']+' · '+note)
    for issue in view.get('issues',[]):
        data['limitations'].append('연금 '+str(issue.get('symbol') or '거래 기간')+': '+issue['reason'])
    comparison=view.get('comparison',{})
    if comparison.get('status') in {'MISMATCH','UNRESOLVED'}:
        differences=comparison.get('differences',[])
        details=' / '.join(r['symbol']+' 계산 '+r['calculated']+'주, 계좌현황 '+r['statement']+'주' for r in differences[:5])
        data['limitations'].append('새 연금 계좌현황을 기준으로 사용합니다. 이전 계산과 대조: 수량 차이 '+str(len(differences))+
                                   '종목, 미해결 '+str(len(comparison.get('issues',[])))+'건.'+(' '+details if details else ''))
    return data
