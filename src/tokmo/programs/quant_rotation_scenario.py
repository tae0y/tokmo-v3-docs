"""Compare reinvestment only AFTER explicitly assumed sales; never execute."""
from dataclasses import asdict
from decimal import Decimal
from tokmo.programs.quant_decision_calculations import plain
from tokmo.values.quant_decisions import parse_snapshot


def after_selected_sales(snapshot,report,*,orders_reconciled=False):
    data=plain(asdict(snapshot));data['schema_version']=1
    data['extra_capital']=[]
    sold=[];skipped=[]
    for row in report['results']:
        o=row.get('selected_option')
        if row['status']!='ANALYZED' or not o or o['action']!='SELL' or o['scenario']!='cash':continue
        if o.get('requires_order_review') and not orders_reconciled:
            skipped.append({'identity':row['identity'],'reason':'기존 조건주문 조정 가정이 필요함'});continue
        cash=next((c for c in data['cash'] if c['account']==o['account'] and c['currency']==o['currency']),None)
        position=next((p for p in data['positions'] if p['account']==o['account'] and p['key']==o['key']),None)
        if not position:
            skipped.append({'identity':row['identity'],'reason':'현재 보유량 근거 미확인'});continue
        qty=Decimal(o['quantity']);delta=Decimal(o['cash_delta'])
        estimated=bool(o.get('requires_broker_confirmation') and position.get('quantity_status')=='CALCULATED' and position['scope']=='retirement')
        limit=position['quantity'] if estimated else position['sellable']
        if limit is None or qty>Decimal(limit):raise ValueError('sale exceeds confirmed or explicitly calculated quantity')
        if cash is None:
            # Zero is the starting budget of this separate proceeds-only scenario,
            # never an assertion that the account's actual cash balance is zero.
            cash={'account':o['account'],'scope':position['scope'],'currency':o['currency'],
                  'available':'0','total_cash':None,'observed_at':data['as_of'],
                  'source':'기존 예수금 미확인 · 가상 매도대금만 재투자', 'basis':'hypothetical_sale_proceeds_only'}
            data['cash'].append(cash)
            data['limitations'].append('기존 예수금은 미확인입니다. 매도대금만으로 교체를 비교하며 실제 계좌 전체 비중을 뜻하지 않습니다.')
        cash['available']=str(Decimal(cash['available'])+delta)
        if cash['total_cash'] is not None:cash['total_cash']=str(Decimal(cash['total_cash'])+delta)
        cash['source']='가상 매도 대금 확보 후 · '+cash['source']
        position['quantity']=str(Decimal(position['quantity'])-qty)
        if position['sellable'] is not None:position['sellable']=str(Decimal(position['sellable'])-qty)
        sold.append({'key':o['key'],'quantity':o['quantity'],'net_proceeds':o['cash_delta'],'currency':o['currency'],
                     'requires_broker_confirmation':estimated,
                     'proceeds_only':cash.get('basis')=='hypothetical_sale_proceeds_only'})
        if estimated:data['limitations'].append('연금 거래 반영 계산 수량으로 매도 후 교체를 비교했습니다. 실제 매도가능수량은 증권사 확인이 필요합니다.')
    data['scenario_label']='선택한 매도가 계산 가격에 완료되고 대금을 사용할 수 있다는 가정의 재투자 비교'
    data['limitations'].append('매도대금 확보 후의 가정안입니다. 실제 매도·결제·환전은 실행하지 않았습니다.')
    return (parse_snapshot(data) if sold else None),{'sold':sold,'skipped':skipped,'actual_orders':0}
