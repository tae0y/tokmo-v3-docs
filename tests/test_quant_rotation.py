from decimal import Decimal as D
from test_quant_decision_replay_delivery import case_fixture
from tokmo.values.quant_decisions import parse_snapshot
from tokmo.programs.quant_decision_analysis import build_report
from tokmo.programs.quant_rotation_scenario import after_selected_sales


def test_rotation_uses_net_sale_proceeds_only_in_separate_scenario():
    d,case=case_fixture();s=parse_snapshot(d);r=build_report(s,recorded=case['steps'][0]['responses'],new_limit=0)
    projected,details=after_selected_sales(s,r)
    proceeds=D(r['results'][0]['selected_option']['cash_delta'])
    assert projected.cash[0].available==s.cash[0].available+proceeds
    assert s.cash[0].available==2000000 and s.positions[0].quantity==100
    assert projected.positions[0].quantity==75 and projected.positions[0].sellable==75
    assert details['actual_orders']==0 and projected.scenario_label


def test_rotation_does_not_reuse_conditional_order_quantity_without_reconciliation():
    d,case=case_fixture();d['orders']=[{'account':'personal-a','key':'KR:000001','kind':'conditional'}]
    s=parse_snapshot(d);r=build_report(s,recorded=case['steps'][0]['responses'],new_limit=0)
    projected,details=after_selected_sales(s,r)
    assert projected is None and details['skipped']
    assert after_selected_sales(s,r,orders_reconciled=True)[0] is not None


def test_pension_calculated_quantity_allows_explicit_reinvestment_comparison():
    from tokmo.programs.quant_decision_analysis import make_packet
    from test_quant_decisions import response
    d,_=case_fixture()
    for p in d['positions']:
        p.update(scope='retirement',account='pension-a',sellable=None,quantity_status='CALCULATED')
    for c in d['cash']:c.update(scope='retirement',account='pension-a')
    d['instruments'][0].update(kind='etf',pension_eligible=True)
    s=parse_snapshot(d);packet=make_packet(s,s.instruments[0],'pension-a','retirement')
    option=next(o for o in packet['options'] if o['action']=='SELL' and D(o['quantity'])==25)
    report=build_report(s,recorded={'pension-a:KR:000001':response(packet,'TRIM',option)},new_limit=0)
    projected,detail=after_selected_sales(s,report)
    assert projected.positions[0].quantity==75
    assert projected.positions[0].sellable is None
    assert detail['sold'][0]['requires_broker_confirmation']
    assert projected.cash[0].available==s.cash[0].available+D(option['cash_delta'])
    assert s.positions[0].quantity==100 and s.cash[0].available==2000000


def test_pension_rotation_with_unknown_cash_uses_only_hypothetical_sale_proceeds():
    from tokmo.programs.quant_decision_analysis import make_packet
    from test_quant_decisions import response
    from tokmo.programs.quant_decision_calculations import portfolio_totals
    d,_=case_fixture();d['cash']=[]
    for p in d['positions']:
        p.update(scope='retirement',account='pension-a',sellable=None,quantity_status='CALCULATED')
    d['instruments'][0].update(kind='etf',pension_eligible=True)
    s=parse_snapshot(d);packet=make_packet(s,s.instruments[0],'pension-a','retirement')
    option=next(o for o in packet['options'] if o['action']=='SELL' and D(o['quantity'])==25)
    report=build_report(s,recorded={'pension-a:KR:000001':response(packet,'TRIM',option)},new_limit=0)
    projected,detail=after_selected_sales(s,report)
    assert projected and not s.cash and s.positions[0].quantity==100
    assert projected.positions[0].quantity==75 and projected.cash[0].available==D(option['cash_delta'])
    assert projected.cash[0].total_cash is None and detail['sold'][0]['proceeds_only']
    assert projected.cash[0].basis=='hypothetical_sale_proceeds_only'
    assert '예수금' in ' '.join(projected.limitations)
    total=portfolio_totals(projected)['retirement:KRW']
    assert not total['complete'] and total['basis']!='계좌 전체'


def test_mistyped_option_gets_precise_single_correction_without_guessing_id():
    from tokmo.programs.quant_decision_analysis import make_packet,run_analysis
    from test_quant_decisions import response
    d,_=case_fixture();s=parse_snapshot(d);p=make_packet(s,s.instruments[0],'personal-a','personal')
    option=next(o for o in p['options'] if o['action']=='SELL' and D(o['quantity'])==25)
    calls=[]
    def model(prompt):
        calls.append(prompt);raw=response(p,'TRIM',option)
        if len(calls)==1:raw['option_id']=option['id'][:-1]
        else:
            assert 'unknown option_id' in prompt and '선택 가능한 정확한 id' in prompt
            assert option['id'] in prompt
        return raw
    result,errors=run_analysis(p,model)
    assert result['option_id']==option['id'] and len(calls)==2 and len(errors)==1
