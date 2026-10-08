"""Analysis uses existing Quant; saved decisions never invent an order price."""
from copy import deepcopy
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal as D
import json
from pathlib import Path

import pytest

from tokmo.programs.rotation_analysis import (
    RotationAnalysisError, build_rotation_request, project_report, run_rotation_analysis,
)
from tokmo.records.rotation_analysis import RotationAnalysisStore
from tokmo.values.rotation_policy import load_policies
from tokmo.values.rotation_decision_file import parse_decision_file
from tokmo.values.quant_decisions import parse_snapshot, instant
from tokmo.programs.quant_decision_analysis import make_packet, validate_analysis
from test_quant_decisions import sample_input, response

NOW = datetime(2026, 9, 16, 1, 5, tzinfo=timezone.utc)
DEADLINE = NOW.replace(minute=40)


def policy():
    return load_policies(json.dumps({'schema':'rotation-policies-v1','experiments':[dict(
        experiment_id='rotation-test',account_scope='personal-a',symbol='005380',market='KR',quantity='1',
        initial_budget='450000',protected_quantity='7',fee_buffer='1500',default_buy='380000',
        buy_floor='350000',buy_ceiling='392000',sell_target_pct='3',sell_min_pct='2',
        valid_until='2027-01-01T00:00:00+09:00',plan_version='2026-09-15')]}))[0]


def context(held=True):
    return dict(quantity='1' if held else '0',cash='27498' if held else '450000',
                cost_basis='372500' if held else None,settled_at='2026-09-16T08:40:00+09:00',
                frozen=False,open_commands=[],fills=[],last_closed_at='2026-09-15T20:00:00+09:00')


def snapshot(held=True):
    data=sample_input();data['as_of']=NOW.isoformat()
    stock=data['instruments'][0]
    stock.update(symbol='005380',name='현대차 합성 검증',price='372500',price_at=NOW.isoformat(),tick_size='500')
    shift=(NOW.date()-timedelta(days=1))-date.fromisoformat(stock['bars'][-1]['date'])
    for bar in stock['bars']:
        bar['date']=(date.fromisoformat(bar['date'])+shift).isoformat()
        bar['available_at']=bar['date']+'T07:00:00+00:00'
        for field in ('open','high','low','close'):bar[field]=str(D(bar[field])*32)
    data['positions'][0].update(key='KR:005380',quantity='8' if held else '7',sellable='8' if held else '7',
                               average_cost='500000',observed_at=NOW.isoformat(),sellable_at=NOW.isoformat())
    data['cash'][0].update(available='27498' if held else '450000',observed_at=NOW.isoformat())
    return data


def report(held=True, price='380000', action=None, condition='지정가 도달 외 추가 조건 없음'):
    data=build_rotation_request(snapshot(held),policy(),context(held),now=NOW)
    s=parse_snapshot(data);item=next(i for i in s.instruments if i.symbol=='005380')
    packet=make_packet(s,item,'personal-a','personal')
    direction='SELL' if held else 'BUY'
    option=next((o for o in packet['options'] if o['action']==direction and o['quantity']=='1'),None)
    assert option is not None, 'Existing Quant must calculate a real one-share option'
    raw=response(packet,action or ('TRIM' if held else 'ADD'),option)
    if raw['action'] in {'HOLD','WATCH'}:raw['option_id']=None
    raw['price_proposal']=dict(side='SELL' if held else 'NEW_BUY',quantity='1',entry_price=price,
        reason='확인된 가격 범위와 반등 여력을 비교한 제안입니다.',condition=condition)
    if not held:raw['price_proposal']['exit_price']='392000'
    analysis=validate_analysis(raw,packet)
    return dict(report_id='qd-'+'a'*16,as_of=NOW.isoformat(),purpose=data['analysis_requests'][0]['purpose'],results=[dict(
        account='personal-a',scope='personal',key='KR:005380',scenario='cash',status='ANALYZED',
        analysis_source='model',packet=packet,analysis=analysis,selected_option=option,errors=[])])


def project(value, held=True):
    return project_report(value,policy=policy(),context=context(held),started_at=NOW,now=NOW+timedelta(seconds=80))


def test_request_keeps_actual_account_and_scopes_one_share():
    original=snapshot();result=build_rotation_request(original,policy(),context(),now=NOW)
    assert result['positions']==original['positions'] and result['cash']==original['cash']
    request=result['analysis_requests'][0]
    assert (request['side'],request['quantity'])==('SELL','1')
    rotation=request['rotation']
    assert rotation['cost_basis']['price']=='372500'
    assert rotation['sell_min_pct']=='2' and rotation['sell_target_pct']=='3'
    assert 'min_spread' not in json.dumps(request)
    assert '20일선' in request['question'] and '3%' in request['question']
    assert 'analysis_requests' not in original


def test_quant_request_only_keeps_portfolio_but_analyzes_just_the_requested_account():
    from tokmo.programs.quant_decision_analysis import build_report
    data=build_rotation_request(snapshot(),policy(),context(),now=NOW)
    data['positions'].append(dict(data['positions'][0],account='another-account'))
    data['extra_capital']=[dict(account='personal-a',scope='personal',currency='KRW',amount='1000000')]
    result=build_report(parse_snapshot(data),only_requests=True,new_limit=0,only_keys=['KR:005380'])
    assert len(result['results'])==1
    assert result['results'][0]['account']=='personal-a'
    assert result['portfolio']


def test_quant_includes_requested_sell_size_without_reducing_real_holdings():
    row=report()['results'][0]
    assert row['selected_option']['quantity']=='1'
    assert row['selected_option']['holding_before']=='8'
    assert row['selected_option']['holding_after']=='7'


@pytest.mark.parametrize('field', ['quote','position','bars'])
def test_old_market_or_account_inputs_fail_before_analysis(field):
    data=snapshot();state=context()
    if field=='quote':
        data['instruments'][0]['price_at']='2026-09-16T08:50:00+09:00'
        state['regular_open_at']='2026-09-16T09:00:00+09:00'
    elif field=='position':data['positions'][0]['observed_at']='2026-09-15T10:05:00+09:00'
    else:
        for bar in data['instruments'][0]['bars']:
            bar['date']=(date.fromisoformat(bar['date'])-timedelta(days=20)).isoformat()
    with pytest.raises(RotationAnalysisError):build_rotation_request(data,policy(),state,now=NOW)


def test_full_quant_report_and_handoff_use_real_validated_one_share_option():
    from tokmo.programs.quant_decision_analysis import build_report
    data=build_rotation_request(snapshot(),policy(),context(),now=NOW)
    ready=report()['results'][0]['analysis']
    calls=[]
    def model(prompt):calls.append(prompt);return ready
    value=build_report(parse_snapshot(data),model=model,only_requests=True,new_limit=0,
                       only_keys=['KR:005380'],purpose='rotation-test')
    assert len(calls)==1 and value['results'][0]['status']=='ANALYZED'
    decision=project(value)
    assert decision['status']=='COMPLETE' and decision['decision']['sell_price']=='380000'


def test_rotation_price_must_be_explicit_in_the_existing_quant_validator():
    value=report();row=value['results'][0]
    raw=deepcopy(row['analysis']);raw['price_proposal'].pop('entry_price')
    with pytest.raises(ValueError,match='explicit entry_price'):
        validate_analysis(raw,row['packet'])


def test_no_inventory_uses_new_buy_hypothesis_without_erasing_protected_shares():
    request=build_rotation_request(snapshot(False),policy(),context(False),now=NOW)['analysis_requests'][0]
    assert request['side']=='NEW_BUY' and request['budget']=='450000'
    assert request['rotation']['protected_position']['quantity']=='7'


@pytest.mark.parametrize('change,code',[
    ({'quantity':'2'},'inventory_invalid'),({'frozen':True},'inventory_frozen'),
    ({'cost_basis':None},'cost_basis_unknown'),({'open_commands':[{'status':'UNKNOWN'}]},'orders_unresolved'),
    ({'settled_at':'2026-09-15T19:00:00+09:00'},'settlement_stale'),
])
def test_invalid_inventory_never_reaches_quant(change,code):
    state=dict(context(),**change)
    with pytest.raises(RotationAnalysisError,match=code):
        build_rotation_request(snapshot(),policy(),state,now=NOW)


def test_validated_sell_maps_sell_hypothesis_entry_price_to_exit():
    result=project(report())
    assert result['status']=='COMPLETE'
    assert result['decision']['action']=='EXIT' and result['decision']['sell_price']=='380000'
    assert result['decision']['buy_price'] is None
    parsed=parse_decision_file(json.dumps(result),policy=policy(),now=NOW+timedelta(seconds=81),
                               trade_date=NOW.date(),cost_basis=D('372500'))
    assert parsed.in_band is True


def test_below_minimum_keeps_price_and_waits_for_owner_approval():
    result=project(report(price='379500'))
    assert result['status']=='WAITING_APPROVAL'
    assert result['decision']['sell_price']=='379500'
    assert result['proposal_id']=='20260916-today-rotation-test'
    assert 'APPROVED' not in json.dumps(result)


@pytest.mark.parametrize('price,expected', [('383675','384000'),('499999','500000'),('500001','501000')])
def test_sell_proposal_rounds_up_at_its_actual_price_tier(price,expected):
    original=report(price=price);before=deepcopy(original)
    result=project(original)
    assert result['status']=='COMPLETE'
    assert result['decision']['sell_price']==expected
    assert result['decision']['total_amount']==expected
    assert original==before
    assert price in result['analysis_summary'] and expected in result['analysis_summary']
    parse_decision_file(json.dumps(result),policy=policy(),now=NOW+timedelta(seconds=81),
                        trade_date=NOW.date(),cost_basis=D('372500'))


def test_buy_rounds_down_and_attached_sell_rounds_up_without_changing_report():
    original=report(False,price='383675')
    original['results'][0]['analysis']['price_proposal']['exit_price']='395123'
    before=deepcopy(original);result=project(original,False)
    assert result['status']=='COMPLETE'
    assert result['decision']['buy_price']==result['decision']['total_amount']=='383500'
    assert result['decision']['sell_price']=='395500'
    assert original==before


@pytest.mark.parametrize('held,price,expected', [(False,'394123','394000'),(True,'379123','379500')])
def test_tick_alignment_does_not_clamp_an_out_of_band_proposal(held,price,expected):
    original=report(held,price=price)
    if not held:original['results'][0]['analysis']['price_proposal']['exit_price']='407000'
    result=project(original,held)
    assert result['status']=='WAITING_APPROVAL'
    assert result['decision']['sell_price' if held else 'buy_price']==expected
    assert price in result['analysis_summary'] and expected in result['analysis_summary']


@pytest.mark.parametrize('protected', [8,9,10])
def test_protected_quantity_comes_from_policy_in_validation_and_question(protected):
    updated=replace(policy(),protected_quantity=D(protected))
    data=snapshot();data['positions'][0].update(quantity=str(protected+1),sellable=str(protected+1))
    request=build_rotation_request(data,updated,context(),now=NOW)['analysis_requests'][0]
    assert request['rotation']['protected_position']['quantity']==str(protected)
    assert f'보호 {protected}주' in request['question'] and '보호 7주' not in request['question']
    assert request['quantity']=='1'


def test_policy_change_does_not_hide_inventory_mismatch_and_capacity_stays_whole():
    with pytest.raises(RotationAnalysisError,match='account_inventory_mismatch'):
        build_rotation_request(snapshot(),replace(policy(),protected_quantity=D(10)),context(),now=NOW)
    # Capacity is configuration now; the policy type already guarantees it is a
    # whole number of shares, so the analysis only guards what it can still see:
    # inventory outside the capacity is a refusal, never a clamp.
    with pytest.raises(RotationAnalysisError,match='inventory_invalid'):
        build_rotation_request(snapshot(),policy(),dict(context(),quantity='2'),now=NOW)


def test_a_two_share_capacity_asks_for_the_part_the_lot_still_needs():
    """One of two held tops the lot up; a full lot sells all of it."""
    pol=replace(policy(),quantity=D(2),protected_quantity=D(8))
    data=snapshot();data['positions'][0].update(quantity='9',sellable='9')
    request=build_rotation_request(data,pol,context(),now=NOW)['analysis_requests'][0]
    assert (request['quantity'],request['side'])==('1','NEW_BUY')
    assert request['rotation']['capacity']=={'quantity':'2','remaining':'1','today':'1'}
    assert '회전용 1주만 판단한다' in request['question'] and '회전 정원은 2주' in request['question']
    full=snapshot();full['positions'][0].update(quantity='10',sellable='10')
    filled=build_rotation_request(full,pol,dict(context(),quantity='2'),now=NOW)['analysis_requests'][0]
    assert (filled['quantity'],filled['side'])==('2','SELL')


def test_analysis_key_follows_supplied_policy_instead_of_fixed_symbol():
    updated=replace(policy(),symbol='000660');data=snapshot()
    data['positions'][0]['key']='KR:000660';data['instruments'][0].update(symbol='000660',name='다른 대상 합성 검증')
    request=build_rotation_request(data,updated,context(),now=NOW)['analysis_requests'][0]
    assert request['key']=='KR:000660' and '다른 대상 합성 검증' in request['question']
    value=report();row=value['results'][0];row['key']=row['packet']['key']='KR:000660'
    row['packet']['user_request']=request
    result=project_report(value,policy=updated,context=context(),started_at=NOW,now=NOW+timedelta(seconds=80))
    assert result['decision']['symbol']=='000660'


def test_context_scope_is_not_restricted_by_hardcoded_market_or_symbol():
    from tokmo.programs.rotation_analysis import check_context
    assert check_context(replace(policy(),symbol='SAMPLE',market='US'),context(),NOW)==1


def test_explicit_buy_uses_both_proposed_prices():
    result=project(report(False),False)
    assert (result['status'],result['decision']['action'])==('COMPLETE','ENTER')
    assert result['decision']['buy_price']=='380000' and result['decision']['sell_price']=='392000'


def test_watch_with_prices_stays_hold_and_never_becomes_enter():
    result=project(report(False,action='WATCH'),False)
    assert result['decision']['action']=='HOLD'
    assert result['decision']['buy_price'] is result['decision']['sell_price'] is None


def test_conditional_sell_waits_for_owner_approval_with_condition_shown():
    result=project(report(condition='20일선 회복을 확인한 뒤 매도'))
    assert (result['status'],result['decision']['action'])==('WAITING_APPROVAL','EXIT')
    assert result['decision']['sell_price']=='380000' and result['proposal_id']=='20260916-today-rotation-test'
    assert '20일선 회복을 확인한 뒤 매도' in result['analysis_summary']
    assert '20일선 회복을 확인한 뒤 매도' in result['decision']['reason']
    assert '승인 범위 밖' not in result['analysis_summary']


def test_conditional_stop_loss_below_floor_waits_with_both_notices():
    result=project(report(price='347500',condition='기존 조건주문 취소 확인 후 대체'))
    assert (result['status'],result['decision']['action'],result['decision']['sell_price'])==('WAITING_APPROVAL','EXIT','347500')
    assert '승인 범위 밖' in result['analysis_summary'] and '기존 조건주문 취소 확인 후 대체' in result['analysis_summary']


def test_conditional_buy_below_band_waits_for_owner_approval():
    result=project(report(False,price='320000',condition='20일선 지지 확인 후 진입'),False)
    assert (result['status'],result['decision']['action'])==('WAITING_APPROVAL','ENTER')
    assert (result['decision']['buy_price'],result['decision']['sell_price'])==('320000','392000')
    assert '20일선 지지 확인 후 진입' in result['analysis_summary']


def test_missing_condition_is_not_treated_as_price_only():
    # Existing Quant already rejects blank text; an absent key is the remaining gap.
    value=report();value['results'][0]['analysis']['price_proposal'].pop('condition')
    result=project(value)
    assert result['status']=='WAITING_APPROVAL' and result['decision']['action']=='EXIT'
    assert '조건 내용 미기재' in result['analysis_summary']


@pytest.mark.parametrize('mutate,code',[
    (lambda r:r['results'][0].update(status='FAILED'), 'quant_failed'),
    (lambda r:r['results'][0].update(account='other'), 'report_scope'),
    (lambda r:r['results'][0]['analysis'].pop('price_proposal'), 'price_proposal_missing'),
    (lambda r:r['results'][0]['analysis']['price_proposal'].update(quantity='7'), 'proposal_quantity'),
    (lambda r:r['results'][0]['analysis']['price_proposal'].update(entry_price='NaN'), 'invalid_number'),
    (lambda r:r['results'][0]['selected_option'].update(quantity='7'), 'protected_position'),
])
def test_invalid_results_do_not_create_a_consumable_decision(mutate,code):
    value=report();mutate(value)
    with pytest.raises(RotationAnalysisError,match=code):project(value)


class Clock:
    def __init__(self):self.at=NOW
    def __call__(self):return self.at


def run(tmp_path, analyzer, *, clock=None, sender=None, warn=None):
    return run_rotation_analysis(policy=policy(),store=RotationAnalysisStore(tmp_path),
        load_inputs=lambda:(snapshot(),context()),analyze=analyzer,clock=clock or Clock(),
        notify=sender or (lambda doc:{'message_id':'test-1'}),warn=warn)


def test_running_is_durable_before_analysis_and_retry_is_noop(tmp_path):
    store=RotationAnalysisStore(tmp_path)
    def analyze(data,deadline):
        assert store.read(policy(),NOW.date())['status']=='RUNNING'
        assert deadline==DEADLINE
        return report()
    outcome=run(tmp_path,analyze)
    assert outcome['status']=='COMPLETE'
    run(tmp_path,lambda *_:pytest.fail('same day must not analyze again'))
    assert (store.path(policy(),NOW.date()).stat().st_mode & 0o777)==0o600


@pytest.mark.parametrize('snapshot_offset,expected',[
    (timedelta(seconds=1),'COMPLETE'),
    (timedelta(seconds=22,milliseconds=1),'FAILED'),
    (-timedelta(hours=1,minutes=59,seconds=50),'FAILED'),
])
def test_input_age_is_checked_after_preparation_without_changing_source_time(tmp_path,snapshot_offset,expected):
    clock=Clock();calls=[];captured=NOW+snapshot_offset
    def inputs():
        data=snapshot();data['as_of']=captured.isoformat()
        # Keep the snapshot internally consistent. In the successful case,
        # holdings, cash and quote were also captured after the run began.
        data['positions'][0]['observed_at']=captured.isoformat()
        data['positions'][0]['sellable_at']=captured.isoformat()
        data['cash'][0]['observed_at']=captured.isoformat()
        data['instruments'][0]['price_at']=captured.isoformat()
        clock.at=NOW+timedelta(seconds=22)
        return data,context()
    def analyze(data,deadline):
        calls.append(data['as_of'])
        assert deadline==DEADLINE
        result=report();result['as_of']=data['as_of']
        clock.at+=timedelta(seconds=1)
        return result
    result=run_rotation_analysis(policy=policy(),store=RotationAnalysisStore(tmp_path),
        load_inputs=inputs,analyze=analyze,clock=clock,notify=lambda doc:{'message_id':'m'})
    assert result['status']==expected
    assert result['started_at']==NOW.isoformat()
    if expected=='COMPLETE':
        assert calls==[captured.isoformat()] and result['data_as_of']==captured.isoformat()
        assert result['produced_at']==clock.at.isoformat()
        assert parse_decision_file(json.dumps(result),policy=policy(),now=clock.at,
            trade_date=NOW.date(),cost_basis=D('372500')).status=='COMPLETE'
    else:
        assert result['failure_code']=='snapshot_stale' and not calls


def test_preparation_reaching_deadline_expires_without_starting_model(tmp_path):
    clock=Clock();calls=[];notices=[]
    def inputs():
        clock.at=DEADLINE
        return snapshot(),context()
    def analyze(*args):
        calls.append(args)
        return report()
    result=run_rotation_analysis(policy=policy(),store=RotationAnalysisStore(tmp_path),
        load_inputs=inputs,analyze=analyze,clock=clock,
        notify=lambda doc:notices.append(doc['status']) or {'message_id':'m'})
    assert result['status']=='EXPIRED' and result['failure_code']=='analysis_timeout'
    assert not calls and notices==['EXPIRED']


def test_failure_alert_and_no_fallback_or_reopen(tmp_path):
    sent=[]
    def fail(*_):raise TimeoutError('model timed out')
    result=run(tmp_path,fail,sender=lambda d:sent.append(d['status']) or {'message_id':'m'})
    assert result['status']=='FAILED' and result['failure_code']=='model_timeout'
    assert sent==['FAILED']
    run(tmp_path,lambda *_:pytest.fail('failed day reopened'))


def test_result_after_deadline_expires_instead_of_completing(tmp_path):
    clock=Clock()
    def late(*_):clock.at=DEADLINE;return report()
    result=run(tmp_path,late,clock=clock)
    assert result['status']=='EXPIRED' and result['failure_code']=='analysis_timeout'


def test_failed_notification_is_recorded_without_repeating_analysis(tmp_path):
    def no_send(_):raise OSError('telegram unavailable')
    result=run(tmp_path,lambda *_:report(),sender=no_send)
    assert result['status']=='COMPLETE'
    receipt=RotationAnalysisStore(tmp_path).delivery_receipt(policy(),NOW.date())
    assert receipt['status']=='FAILED' and receipt['error']=='OSError'
    run(tmp_path,lambda *_:pytest.fail('delivery failure restarted analysis'))


def test_waiting_approval_expires_at_hard_close_without_model_call(tmp_path):
    run(tmp_path,lambda *_:report(price='379500'))
    clock=Clock();clock.at=DEADLINE
    result=run(tmp_path,lambda *_:pytest.fail('approval wait restarted analysis'),clock=clock)
    assert result['status']=='EXPIRED' and result['failure_code']=='approval_timeout'


# --- approval window: 30 minutes after the notice was delivered (2026-10-02) ---

def waiting_sent_at(tmp_path, sent, *, sender=None, warn=None):
    clock=Clock();clock.at=sent
    return run(tmp_path,lambda *_:report(price='379500'),clock=clock,sender=sender,warn=warn),clock


def test_approval_deadline_is_thirty_minutes_after_the_delivered_notice(tmp_path):
    seen=[]
    def sender(doc):seen.append(doc['valid_until']);return {'message_id':'1'}
    sent=NOW.replace(minute=12)
    result,_=waiting_sent_at(tmp_path,sent,sender=sender)
    assert result['status']=='WAITING_APPROVAL'
    assert instant(result['valid_until'])==sent+timedelta(minutes=30)
    assert seen==[result['valid_until']], 'the notice must show the same deadline the file binds'
    assert instant(RotationAnalysisStore(tmp_path).read(policy(),NOW.date())['valid_until'])==sent+timedelta(minutes=30)


def test_hard_close_at_1040_does_not_end_a_live_approval_window(tmp_path):
    _,clock=waiting_sent_at(tmp_path,NOW.replace(minute=12))
    clock.at=DEADLINE
    result=run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock)
    assert result['status']=='WAITING_APPROVAL'
    clock.at=NOW.replace(minute=42)
    result=run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock)
    assert result['status']=='EXPIRED' and result['failure_code']=='approval_timeout'
    assert '10:42' in result['analysis_summary']


def test_undelivered_notice_keeps_the_analysis_close(tmp_path):
    def no_send(_):raise OSError('telegram unavailable')
    _,clock=waiting_sent_at(tmp_path,NOW.replace(minute=12),sender=no_send)
    clock.at=DEADLINE
    result=run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock)
    assert result['status']=='EXPIRED' and result['failure_code']=='approval_timeout'


def test_warning_goes_out_once_five_minutes_before_the_deadline(tmp_path):
    warnings=[]
    def warn(doc,closes):warnings.append(closes);return {'message_id':'w1'}
    _,clock=waiting_sent_at(tmp_path,NOW.replace(minute=12),warn=warn)
    store=RotationAnalysisStore(tmp_path)
    for minute in (20,36):
        clock.at=NOW.replace(minute=minute)
        assert run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock,warn=warn)['status']=='WAITING_APPROVAL'
    assert warnings==[], 'no warning before deadline-5min'
    for minute in (37,38,41):
        clock.at=NOW.replace(minute=minute)
        assert run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock,warn=warn)['status']=='WAITING_APPROVAL'
    assert warnings==[NOW.replace(minute=42)], 'exactly one warning, bound to the real deadline'
    receipt=store.delivery_receipt(policy(),NOW.date())
    assert receipt['status']=='SENT' and receipt['warning']['status']=='SENT' and receipt['warning']['receipt']=={'message_id':'w1'}
    clock.at=NOW.replace(minute=42)
    assert run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock,warn=warn)['status']=='EXPIRED'
    assert warnings==[NOW.replace(minute=42)], 'the warning never restarts the window and never repeats'


def test_failed_warning_is_recorded_once_and_never_retried(tmp_path):
    attempts=[]
    def warn(doc,closes):attempts.append(closes);raise OSError('telegram unavailable')
    _,clock=waiting_sent_at(tmp_path,NOW.replace(minute=12),warn=warn)
    for minute in (37,38):
        clock.at=NOW.replace(minute=minute)
        run(tmp_path,lambda *_:pytest.fail('no second analysis'),clock=clock,warn=warn)
    assert len(attempts)==1
    receipt=RotationAnalysisStore(tmp_path).delivery_receipt(policy(),NOW.date())
    assert receipt['warning']['status']=='FAILED' and receipt['status']=='SENT'
