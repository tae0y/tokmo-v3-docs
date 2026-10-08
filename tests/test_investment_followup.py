"""User-choice continuity, chronology, transport and quantitative integration."""
from copy import deepcopy
from datetime import datetime,timedelta,timezone
from decimal import Decimal
import json
from pathlib import Path
import sqlite3

import pytest

from tokmo.records.investment_followup import InvestmentFollowupStore,encode
from tokmo.programs.investment_followup import record_text,review_choice,deliver_pending,render_followup,next_delivery
from tokmo.programs.investment_followup_worker import run_tick
from tokmo.programs.quant_decision_analysis import make_packet
from tokmo.programs.quant_decision_inputs import pension_source_date,read_user_choices
from tokmo.values.quant_decisions import parse_snapshot

NOW=datetime(2026,9,7,1,tzinfo=timezone.utc)
BASE=Path(__file__).parent/'fixtures/quant_decision_cases/base-synthetic.json'


@pytest.fixture
def store(tmp_path):
    value=InvestmentFollowupStore(tmp_path/'choices.sqlite3')
    yield value
    value.close()


def interpretation(action='HOLD',**kw):
    return dict(action=action,phase='DECIDED',account='personal-a',scope='personal',keys=['KR:000001'],
        summary='장기 전망을 믿고 보유한다',reason_quotes=['장기 전망을 믿고'],condition_quotes=[],
        suggested_checks=['수요와 시장 위험을 살핀다'],supersedes=None,**kw)


def choice(store,source='m1',value=None,at=NOW):
    return store.record_choice(raw_text='장기 전망을 믿고 보유하기로 했어',interpretation=value or interpretation(),source_id=source,at=at)


def evidence(key='war',at=NOW):
    return [dict(id=key,text='전쟁 장기화와 운송 차질이 공식 확인되었다. 기업 수주 변화는 확인되지 않았다.',
                 available_at=at.isoformat(),source='합성 공식 발표',kind='news')]


def response(**kw):
    value=dict(material_change=True,notification_reason='기존에 감수한 위험과 다른 운송 차질이 확인됐다.',effect='REVIEW',business='SUPPORTED',market_risk='CHANGED',notify=True,urgency='NORMAL',
               pending_action='REPLACE',comparison='장기 전망은 유지되지만 예상보다 전쟁이 길어져 계좌 위험을 재검토할 이유가 생겼다.',
               new_facts=[dict(text='전쟁이 장기화됐다.',sources=['war'])],next_checks=['운송 차질 완화를 확인한다'],analysis=None)
    value.update(kw)
    if 'change' not in kw:
        material=value['notify'] and value['material_change']
        value['change']={'kind':'MATERIAL' if material else 'CONTEXT',
            'before':'이전에는 위험이 제한적이라고 판단했다.', 'after':value['comparison'],
            'meaning':value['notification_reason'],
            'sources':list(dict.fromkeys(s for f in value['new_facts'] for s in f['sources']))}
    return value


def review(store,c,at=NOW,raw=None,ev=None,snapshot=None,trigger='news',model=None):
    return review_choice(store,c,ev if ev is not None else evidence(),snapshot=snapshot,
                         model=model or (lambda p:deepcopy(raw or response())),at=at,trigger=trigger)


def test_input_preserves_user_words_and_separates_intent_from_execution(store):
    raw='장기 전망을 믿고 조금 팔 생각이야'
    value=interpretation();value['phase']='INTENT';value['action']='TRIM'
    result=record_text(store,text=raw,source_id='message',at=NOW,
        catalog={'accounts':[{'account':'personal-a','scope':'personal'}],'instruments':{'KR:000001':'가상 기업'}},
        model=lambda p:{'choice':value,'clarification':None,'duplicate_of':None})
    c=result['choice']
    assert c['raw_text']==raw and c['execution_status']=='NOT_VERIFIED'
    assert c['interpretation']['suggested_checks'] and c['interpretation']['condition_quotes']==[]
    duplicate=record_text(store,text=raw,source_id='message',at=NOW,catalog={},model=lambda p:pytest.fail('duplicate model call'))
    assert duplicate['choice']['id']==c['id']


def test_question_is_not_adopted_as_a_choice(store):
    result=record_text(store,text='어떻게 할까?',source_id='question',at=NOW,catalog={},
        model=lambda p:{'clarification':'보유를 유지할지 먼저 결정해 주세요.','choice':None})
    assert result['choice'] is None and store.choices(at=NOW)==[]


def test_review_wrapper_preserves_full_assessment_validation(store):
    c=choice(store)
    result=review(store,c,raw={'answer':{'review':response()}})
    assert result['assessment']==response()
    bad=response(new_facts=[dict(text='없는 출처',sources=['absent'])])
    with pytest.raises(ValueError,match='실제 제공한 근거'):
        review(store,c,at=NOW+timedelta(minutes=1),trigger='new',raw={'answer':{'review':bad}})


def test_review_wrapper_does_not_discard_conflicting_outer_fields(store):
    c=choice(store)
    with pytest.raises(ValueError,match='effect'):
        review(store,c,raw={'review':response(),'effect':'invalid'})


@pytest.mark.parametrize('layout',['string_envelope','analysis_quant'])
def test_string_envelope_and_supplied_evidence_alias_are_resolved(store,layout):
    from dataclasses import replace
    c=choice(store)
    snapshot=parse_snapshot(json.loads(BASE.read_text()))
    snapshot=replace(snapshot,user_choices=(c,))
    def model(prompt):
        packet=json.loads(prompt.split('\nINPUT\n',1)[1])
        quant=packet['quant']['KR:000001']
        alias=next(e['alias'] for e in quant['evidence'] if e['id']=='war')
        raw=response(new_facts=[dict(text='전쟁 장기화',sources=[alias])],analysis={
            'action':'HOLD','option_id':None,'summary':'보유 이유와 현재 위험을 함께 점검한다.',
            'reasons':[{'text':'실제 사용자 선택을 기준으로 검토한다.','sources':[c['id']]},
                       {'text':'제공된 가격과 지표를 살핀다.','sources':['price/features']}],
            'counterevidence':'시장 위험은 기업 전망과 별개다.',
            'change_conditions':['전쟁이 장기화되는지 확인한다.'],'thesis_status':'supported'})
        if layout=='analysis_quant':
            numeric=raw['analysis'];raw['analysis']=None
            return {'analysis':raw,'quant':{'KR:000001':numeric}}
        return json.dumps({'answer':json.dumps(raw)})
    result=review(store,c,snapshot=snapshot,model=model)
    assert result['assessment']['new_facts'][0]['sources']==['war']
    assert result['assessment']['analysis']['reasons'][0]['sources']==[c['id']]
    assert result['assessment']['analysis']['reasons'][1]['sources']==['price','features']


def test_two_quantitative_payloads_are_not_silently_selected(store):
    c=choice(store)
    with pytest.raises(ValueError,match='두 위치'):
        review(store,c,raw={'analysis':response(analysis={'action':'HOLD'}),'quant':{'action':'EXIT'}})


@pytest.mark.parametrize('reference,valid',[
    ('quant:KR:000001:holdings',True),
    ('quant:KR:999999:holdings',False),
    ('quant:KR:000001:invented',False),
])
def test_qualified_quant_reference_only_resolves_the_same_supplied_packet(store,reference,valid):
    c=choice(store);snapshot=parse_snapshot(json.loads(BASE.read_text()));calls=[]
    def model(prompt):
        calls.append(prompt)
        return response(analysis={
            'key':'KR:000001','action':'HOLD','option_id':None,'summary':'현재 보유 이유를 유지한다.',
            'reasons':[{'text':'제공된 보유 자료를 기준으로 판단한다.','sources':[reference]}],
            'counterevidence':'시장 위험은 따로 살핀다.',
            'change_conditions':['기업의 생산 전망이 바뀌는지 확인한다.'],'thesis_status':'supported'})
    if valid:
        result=review(store,c,snapshot=snapshot,model=model)
        assert result['assessment']['analysis']['reasons'][0]['sources']==['holdings']
        assert len(calls)==1
    else:
        with pytest.raises(ValueError,match='unknown evidence reference'):
            review(store,c,snapshot=snapshot,model=model)


def test_source_correction_identifies_the_actual_bad_reference(store):
    c=choice(store);calls=[]
    def model(prompt):
        calls.append(prompt)
        if len(calls)==1:
            return response(new_facts=[dict(text='전쟁 장기화',sources=['war-typo'])])
        assert '잘못된 참조' in prompt and 'war-typo' in prompt
        return response()
    result=review(store,c,model=model)
    assert len(calls)==2 and result['assessment']['new_facts'][0]['sources']==['war']


@pytest.mark.parametrize('reference,valid',[
    ('quant:KR:000001:price_context',True),('choice',True),
    ('quant:KR:000001:not_supplied',False),('quant:KR:999999:price_context',False)])
def test_new_facts_can_cite_supplied_quote_and_choice_without_inventing_sources(store,reference,valid):
    c=choice(store);snapshot=parse_snapshot(json.loads(BASE.read_text()))
    reference=c['id'] if reference=='choice' else reference
    raw=response(new_facts=[dict(text='제공된 가격 관측과 선택 조건을 비교한다.',sources=[reference])])
    if valid:assert review(store,c,snapshot=snapshot,raw=raw)['assessment']['new_facts'][0]['sources']==[reference]
    else:
        with pytest.raises(ValueError,match='실제 제공한 근거'):review(store,c,snapshot=snapshot,raw=raw)


@pytest.mark.parametrize('text,valid',[
    ('등록한 계획의 17,200원 보류 조건을 다시 확인한다.',True),
    ('등록한 계획의 17,201원 보류 조건을 다시 확인한다.',False),
    ('등록한 계획의 17,200달러 보류 조건을 다시 확인한다.',False)])
def test_registered_plan_conditions_reach_unit_aware_quant_validation(store,text,valid):
    from dataclasses import replace
    from tokmo.programs.quant_decision_analysis import validate_analysis
    c=dict(choice(store),plan={'kind':'DECISION','question':'17,200원 아래면 신규 매수를 보류한다.'})
    snapshot=replace(parse_snapshot(json.loads(BASE.read_text())),user_choices=(c,))
    p=make_packet(snapshot,snapshot.instruments[0],'personal-a','personal')
    raw={'action':'HOLD','option_id':None,'summary':'등록한 조건을 확인한다.',
         'reasons':[{'text':text,'sources':[c['id']]}],'counterevidence':'기업 변화도 함께 확인한다.',
         'change_conditions':['새로운 변화가 있는지 살핀다.'],'thesis_status':'supported'}
    if valid:assert validate_analysis(raw,p)['reasons'][0]['text']==text
    else:
        with pytest.raises(ValueError,match='unsupported monetary'):validate_analysis(raw,p)
    # Neither an unrelated account nor an AI's suggested check admits the price.
    p['current_user_choices'][0]['interpretation']['account']='other-account'
    with pytest.raises(ValueError):validate_analysis(raw,p)


def test_followup_keeps_earlier_delivered_facts_after_an_intervening_alert(store):
    c=choice(store)
    first=review(store,c,at=NOW,trigger='first')
    deliver_pending(store,at=NOW,sender=lambda body:'701')
    review(store,c,at=NOW+timedelta(minutes=1),trigger='silent',raw=response(notify=False,pending_action='KEEP'))
    second=review(store,c,at=NOW+timedelta(minutes=2),trigger='second',raw=response(comparison='다른 의미 있는 변화를 전달했다.'))
    deliver_pending(store,at=NOW+timedelta(minutes=2),sender=lambda body:'702')
    seen=[]
    def model(prompt):
        packet=json.loads(prompt.split('\nINPUT\n',1)[1]);seen.append(packet)
        return response(notify=False,pending_action='KEEP')
    review(store,c,at=NOW+timedelta(minutes=3),trigger='check-history',model=model)
    assert [r['id'] for r in seen[0]['delivered_history']]==[second['id'],first['id']]
    assert all(r['delivery_state']=='SENT' for r in seen[0]['delivered_history'])
    assert seen[0]['delivered_history'][1]['assessment']['new_facts']==first['assessment']['new_facts']


def test_unknown_reason_cannot_become_user_statement(store):
    c=interpretation();c['reason_quotes']=['손실은 20%까지 허용']
    with pytest.raises(ValueError,match='원문'):
        record_text(store,text='장기 전망을 믿고 보유',source_id='bad',at=NOW,
                    catalog={'accounts':[{'account':'personal-a','scope':'personal'}],'instruments':{'KR:000001':'가상'}},
                    model=lambda p:{'choice':c})
    assert store.connection.execute('select count(*) from investment_calls').fetchone()[0]==2
    assert not store.choices(at=NOW)


def test_revision_preserves_history_and_cancels_old_pending_at_delivery(store):
    first=choice(store);review(store,first)
    updated=interpretation('CASH');updated['supersedes']=first['id'];updated['phase']='REPORTED_EXECUTED'
    newer=choice(store,'m2',updated,at=NOW+timedelta(minutes=1))
    assert store.choice(first['id'])['raw_text']==first['raw_text']
    assert [c['id'] for c in store.choices(at=NOW)]==[first['id']]
    assert [c['id'] for c in store.choices(at=NOW+timedelta(minutes=1))]==[newer['id']]
    assert not deliver_pending(store,at=NOW+timedelta(minutes=2),sender=lambda b:pytest.fail('stale choice'))
    assert newer['execution_status']=='USER_REPORTED'


def test_cross_account_revision_is_rejected(store):
    first=choice(store)
    changed=interpretation();changed.update(scope='retirement',account='pension-a',supersedes=first['id'])
    with pytest.raises(ValueError,match='다른 계좌'):choice(store,'m2',changed)


def test_hold_and_cash_receive_different_followup_and_calculated_size(store):
    data=json.loads(BASE.read_text());snapshot=parse_snapshot(data)
    c=choice(store)
    def analyst(prompt):
        packet=json.loads(prompt.split('\nINPUT\n')[-1]);q=packet['quant']['KR:000001']
        opt=next(o for o in q['options'] if o['action']=='SELL' and o['holding_after']!='0')
        return response(analysis={'key':'KR:000001','action':'TRIM','option_id':opt['id'],
            'summary':'장기 전망을 유지하되 일부 축소로 현금 여유를 확보하는 편을 권한다.',
            'reasons':[{'text':'전쟁 장기화가 시장 위험을 바꿨다.','sources':['war']}],
            'counterevidence':'기업 수주 전망은 유지된다.','change_conditions':['운송 정상화'], 'thesis_status':'supported'})
    held=review(store,c,snapshot=snapshot,model=analyst)
    assert Decimal(held['selected_option']['holding_after'])<Decimal(held['selected_option']['holding_before'])
    assert '가용 자금' in render_followup(held)
    value=interpretation('CASH');value['supersedes']=c['id'];value['summary']='위험 완화까지 현금 대기'
    cash=choice(store,'m2',value,at=NOW+timedelta(minutes=1))
    data['positions']=[];cash_snapshot=parse_snapshot(data)
    r=review(store,cash,at=NOW+timedelta(minutes=2),snapshot=cash_snapshot,
        raw=response(effect='MAINTAIN',comparison='현금 대기의 이유가 여전히 남아 있으므로 재진입을 서두르지 않는다.'))
    assert not r['input']['quant']['KR:000001']['holdings']
    assert '현금' in r['assessment']['comparison']


def test_future_evidence_and_future_snapshot_do_not_leak(store):
    c=choice(store)
    future=evidence('future',NOW+timedelta(hours=1))
    r=review(store,c,ev=future,raw=response(effect='DATA_GAP',notify=False,pending_action='KEEP',new_facts=[]))
    assert r['evidence']==[]
    data=json.loads(BASE.read_text());data['as_of']=(NOW+timedelta(hours=1)).isoformat()
    with pytest.raises(ValueError,match='미래 계좌'):review(store,c,snapshot=parse_snapshot(data),trigger='later')


def test_idempotent_review_does_not_call_again_or_grow_recursive_context(store):
    c=choice(store);first=review(store,c)
    assert review(store,c,model=lambda p:pytest.fail('same request'))['id']==first['id']
    for n in range(1,4):
        r=review(store,c,at=NOW+timedelta(hours=n),trigger=f'check{n}',raw=response(notify=False,pending_action='KEEP'))
    assert 'input' not in r['input']['last_review']
    assert len(encode(r['input']))<10000


def test_pending_comparison_does_not_forget_unsent_alert(store):
    c=choice(store);first=review(store,c)
    silent=review(store,c,trigger='repeat',raw=response(notify=False,pending_action='KEEP'))
    assert [r['id'] for r in store.pending(at=NOW)]==[first['id']]
    cancelled=review(store,c,trigger='correction',raw=response(notify=False,pending_action='CANCEL'))
    assert not store.pending(at=NOW)
    assert silent['id']!=cancelled['id']


def test_receipt_tracks_actual_delivery_and_timeout_is_not_resent(store):
    c=choice(store);r=review(store,c)
    def fail(body):raise TimeoutError()
    assert deliver_pending(store,at=NOW,sender=fail)[0]['status']=='UNKNOWN'
    assert not deliver_pending(store,at=NOW,sender=lambda b:pytest.fail('unknown resend'))
    assert store.latest_review(c['id'],at=NOW,delivered=True) is None
    fresh=review(store,c,trigger='risk-relief',raw=response(effect='REENTRY',market_risk='RELIEVED'))
    assert deliver_pending(store,at=NOW,sender=lambda b:'replay:local:1')[0]['status']=='SENT'
    assert store.latest_review(c['id'],at=NOW,delivered=True)['id']==fresh['id']


def test_night_queue_survives_restart_and_has_urgent_exception(tmp_path):
    path=tmp_path/'db';store=InvestmentFollowupStore(path);c=choice(store)
    late=datetime(2026,9,7,14,tzinfo=timezone.utc)
    r=review(store,c,at=late)
    assert r['deliver_after']=='2026-09-08T08:00:00+09:00'
    store.close();store=InvestmentFollowupStore(path)
    assert not store.pending(at=late)
    assert len(store.pending(at=datetime(2026,9,7,23,tzinfo=timezone.utc)))==1
    urgent=review(store,c,at=late,trigger='urgent',raw=response(urgency='IMMEDIATE'))
    assert [v['id'] for v in store.pending(at=late)]==[urgent['id']]
    store.close()


def test_unfinished_model_call_does_not_retry_each_tick(store):
    def fail(prompt):raise TimeoutError()
    c=choice(store)
    with pytest.raises(TimeoutError):review(store,c,model=fail)
    with pytest.raises(ValueError,match='재시도'):review(store,c,model=lambda p:pytest.fail('automatic retry'))
    assert store.connection.execute('select count(*) from investment_calls').fetchone()[0]==1


def test_unambiguous_model_shape_is_normalized_without_extra_call(store):
    data=json.loads(BASE.read_text());snapshot=parse_snapshot(data);c=choice(store);calls=[]
    def model(prompt):
        calls.append(prompt)
        return response(key='KR:000001',pending_action='KEEP',analysis={
            'action':'HOLD','option_id':None,'summary':'보유를 유지하며 사업 변화와 시장 위험을 확인한다.',
            'reasons':[{'text':'기업 전망이 유지된다.','sources':['business-1']}],
            'counterevidence':'전쟁 장기화는 부담이다.','change_conditions':['운송 정상화'], 'thesis_status':'supported'})
    r=review(store,c,snapshot=snapshot,model=model)
    assert r['assessment']['analysis']['key']=='KR:000001' and len(calls)==1
    def wrapped(prompt):
        raw=model(prompt);raw['pending_action']='REPLACE';raw['analysis']={'KR:000001':raw['analysis']};return {'answer':raw}
    r=review(store,c,snapshot=snapshot,model=wrapped,trigger='wrapper')
    assert r['assessment']['analysis']['action']=='HOLD' and len(calls)==2
    def nested(prompt):
        raw=model(prompt);raw['pending_action']='REPLACE';raw['quant']=raw.pop('analysis')
        return {'target':'KR:000001','analysis':raw}
    r=review(store,c,snapshot=snapshot,model=nested,trigger='nested')
    assert r['assessment']['analysis']['action']=='HOLD' and len(calls)==3


def test_portfolio_percent_in_unchecked_prose_requires_correction(store):
    c=choice(store)
    with pytest.raises(ValueError,match='숫자 없이'):
        review(store,c,raw=response(comparison='주식 비중 100% 집중으로 위험하다'))


def test_historical_pension_reconciliation_requires_full_later_trade_coverage():
    from tokmo.programs.pension_analysis_view import reconcile_quantities
    baseline={'source_at':'2026-08-28T00:00:00+09:00','coverage_through':'2026-08-28T23:59:59.999999+09:00',
              'positions':[{'symbol':'0162Z0','quantity':'3140','avg_price':'12000','currency':'KRW'}]}
    args={'through':'2026-09-07T20:00:00+09:00','complete_from':'2026-08-28T00:00:00+09:00','complete_through':'2026-09-07T20:00:00+09:00'}
    trades=[{'id':'already-in-baseline','symbol':'0162Z0','executed_at':'2026-08-28T10:00:00+09:00','side':'SELL','quantity':'200','price':'13000','currency':'KRW'},
            {'id':'later','symbol':'0162Z0','executed_at':'2026-09-01T10:00:00+09:00','side':'SELL','quantity':'100','price':'13000','currency':'KRW'}]
    r=reconcile_quantities(baseline,trades,**args)
    assert r['positions'][0]['quantity']=='3040' and r['execution_ids']==['later']
    assert baseline['positions'][0]['quantity']=='3140'
    args['complete_through']='2026-07-13T15:00:00+09:00'
    with pytest.raises(ValueError,match='전체 거래'):reconcile_quantities(baseline,[],**args)


def test_news_worker_reads_low_event_and_keeps_news_first_path_independent(store,tmp_path):
    c=choice(store)
    db=tmp_path/'news.sqlite3';conn=sqlite3.connect(db)
    conn.execute('create table news_decision_events(state text)')
    e={'id':'event-no-ticker','updated_at':NOW.isoformat(),'decision':{'title':'운송 차질','importance':'LOW','notify':False,'evidence_ids':['war']},
       'evidence':[{'id':'war','observed_at':NOW.isoformat(),'title':'운송 차질','text':'전쟁 장기화','publisher':'합성 자료'}]}
    conn.execute('insert into news_decision_events values(?)',(json.dumps(e),));conn.commit();conn.close()
    def matcher(prompt):
        assert 'LOW' in prompt and '현금 대기' in prompt
        return {'matches':[{'choice_id':c['id'],'event_ids':[e['id']],'reason':'시장 위험 변화','assessment':'REVIEW'}],'unmatched_reason':'나머지 없음'}
    r=run_tick(store,news_path=db,snapshot=None,at=NOW,model=lambda p:response(),screen_model=matcher)
    assert r['reviewed'][0]['notify'] and r['remaining']==0
    again=run_tick(store,news_path=db,snapshot=None,at=NOW,model=lambda p:pytest.fail('unchanged event'))
    assert not again['reviewed']
    assert sqlite3.connect(db).execute('select count(*) from news_decision_events').fetchone()[0]==1


def test_source_date_comes_from_pension_registry_and_choices_reach_quant(tmp_path):
    root=tmp_path; (root/'stores').mkdir();conn=sqlite3.connect(root/'stores/pension_registry.sqlite3')
    conn.executescript("create table pension_revisions(revision_id,state,source_as_of);create table pension_active_pointer(active_revision_id);")
    conn.execute('insert into pension_revisions values(?,?,?)',('v','ACTIVE','2026-08-12T15:00:00+00:00'))
    conn.execute('insert into pension_active_pointer values(?)',('v',));conn.commit();conn.close()
    assert pension_source_date(root,at=NOW)=='2026-08-12T15:00:00+00:00'
    s=InvestmentFollowupStore(root/'private/investment-followup.sqlite3');c=choice(s);s.close()
    data=json.loads(BASE.read_text());data['user_choices']=read_user_choices(root,at=NOW)
    data['positions'][0].update(observed_at='2026-08-12T15:00:00+00:00',captured_at=NOW.isoformat(),source_status='PENSION_SOURCE_DATED')
    snapshot=parse_snapshot(data);packet=make_packet(snapshot,snapshot.instruments[0],'personal-a','personal')
    assert packet['current_user_choices'][0]['id']==c['id']
    assert packet['holdings'][0]['observed_at']!=packet['holdings'][0]['captured_at']


def test_worker_failure_retries_are_bounded_and_explicit_recovery_remains_available(store,tmp_path):
    c=choice(store);calls=[]
    def broken(prompt):calls.append(prompt);raise TimeoutError()
    args=dict(news_path=tmp_path/'missing-news',snapshot=None)
    first=run_tick(store,**args,at=NOW,model=broken)
    assert {'stage':'news_source','error':'UNAVAILABLE'} in first['errors']
    for hour in [1,7,24]:
        result=run_tick(store,**args,at=NOW+timedelta(hours=hour),model=broken)
        assert any(e['error']=='TimeoutError' for e in result['errors'])
    assert len(calls)==3
    retry=run_tick(store,**args,at=NOW+timedelta(hours=25),attempt='after-investigation',
        model=lambda p:response(effect='DATA_GAP',business='UNCERTAIN',market_risk='UNKNOWN',
                                notify=False,pending_action='KEEP',new_facts=[]))
    assert retry['reviewed'] and store.watermark('failed_review:'+c['id']) is None
    assert retry['errors']==[{'stage':'news_source','error':'UNAVAILABLE'}]


def test_changed_choice_cannot_be_claimed_after_pending_list_was_read(store):
    c=choice(store);r=review(store,c)
    assert store.pending(at=NOW)
    revised=interpretation('CASH');revised['supersedes']=c['id']
    choice(store,'new-choice',revised,at=NOW+timedelta(seconds=1))
    assert not store.claim(r['id'],at=NOW+timedelta(seconds=2))


def test_unknown_delivery_remains_degraded_on_next_cli_tick(store,tmp_path,monkeypatch,capsys):
    import run_investment_followup as cli
    c=choice(store);review(store,c)
    deliver_pending(store,at=NOW,sender=lambda body:None)
    monkeypatch.setattr(cli,'QuantModel',lambda **kw:object())
    monkeypatch.setattr(cli,'run_tick',lambda *args,**kw:{'reviewed':[],'errors':[]})
    args=['tick','--state',str(tmp_path/'choices.sqlite3'),'--snapshot',str(BASE),'--at',NOW.isoformat()]
    assert cli.main(args)==2
    output=json.loads(capsys.readouterr().out)
    assert output['errors'][0]['error']=='RECEIPT_UNCONFIRMED'
    assert output['errors'][0]['deliveries'][0]['status']=='UNKNOWN'


def test_optional_cron_is_inert_until_enabled(tmp_path,monkeypatch):
    import run_investment_followup_cron as cron
    monkeypatch.setattr(cron,'run',lambda *args:pytest.fail('disabled worker ran'))
    assert cron.main(['--root',str(tmp_path)])==0
    (tmp_path/'investment_followup.json').write_text('{"enabled":false}')
    assert cron.main(['--root',str(tmp_path)])==0
    assert not (tmp_path/'ops').exists()


def test_news_context_uses_only_confirmed_followup_even_without_holdings(store,tmp_path):
    from tokmo.records.news_decision_inputs import read_portfolio_context
    args=dict(path=tmp_path/'no-holdings.json',followup_path=tmp_path/'choices.sqlite3')
    c=choice(store);r=review(store,c)
    ctx=read_portfolio_context(**args,now=NOW)
    assert ctx['portfolio_status']=='UNAVAILABLE'
    assert ctx['investment_followup']['choices'][0]['id']==c['id']
    assert not ctx['investment_followup']['delivered_reviews']
    assert 'personal-a' not in encode(ctx)
    deliver_pending(store,at=NOW+timedelta(minutes=1),sender=lambda b:'synthetic-local-receipt')
    assert not read_portfolio_context(**args,now=NOW)['investment_followup']['delivered_reviews']
    ctx=read_portfolio_context(**args,now=NOW+timedelta(minutes=2))
    assert ctx['investment_followup']['delivered_reviews'][0]['id']==r['id']
    assert ctx['investment_followup']['delivered_reviews'][0]['delivered_at']=='2026-09-07T10:01:00+09:00'
    assert 'cash_before' not in encode(ctx)
    # Existing behavior stays exactly opt-in and no missing DB is initialized.
    assert 'investment_followup' not in read_portfolio_context(args['path'],NOW)
    ctx=read_portfolio_context(args['path'],NOW,followup_path=tmp_path/'missing.db')
    assert ctx['investment_followup']['status']=='NOT_AVAILABLE'
    assert not (tmp_path/'missing.db').exists()


def test_news_context_retains_earlier_sent_facts_but_excludes_future_receipts(store):
    from tokmo.records.investment_followup import read_news_context
    c=choice(store)
    for n in range(3):
        at=NOW+timedelta(minutes=n*2)
        review(store,c,at=at,trigger=str(n))
        deliver_pending(store,at=at+timedelta(minutes=1),sender=lambda b:'local-test-receipt')
    context=read_news_context(store.connection.execute('PRAGMA database_list').fetchone()[2],at=NOW+timedelta(minutes=4))
    assert len(context['delivered_reviews'])==2
    assert [r['delivered_at'][-14:-6] for r in context['delivered_reviews']]==['10:03:00','10:01:00']


@pytest.mark.parametrize('scope,market,symbol,label',[('personal','US','SAMPLE','미국'),('retirement','KR','0162Z0','퇴직연금')])
def test_followup_uses_own_account_and_currency_for_us_and_pension(store,scope,market,symbol,label):
    from test_quant_decisions import sample_input
    data=sample_input();key=market+':'+symbol
    if scope=='retirement':
        data['instruments'][0].update(symbol=symbol,kind='etf',name='가상 반도체 채권 혼합 ETF')
        data['instruments'][0]['evidence'].append({'id':'etf-profile','kind':'etf_profile',
            'text':'합성 구성 자료: 반도체 주식 절반과 채권 절반이므로 주식 하락 위험이 일부 남는다.',
            'source':'합성 상품 설명','available_at':NOW.isoformat(),'scope':'retirement'})
        data['positions'][0].update(account='pension-a',scope=scope,key=key)
        data['cash'].append(dict(data['cash'][0],account='pension-a',scope=scope,available='300000',total_cash='300000'))
    else:
        data['positions'].append(dict(data['positions'][0],key=key,quantity='2',sellable='2',average_cost='100'))
    value=interpretation();value.update(scope=scope,account='pension-a' if scope=='retirement' else 'personal-a',keys=[key])
    c=choice(store,value=value);snapshot=parse_snapshot(data)
    def model(prompt):
        q=json.loads(prompt.split('\nINPUT\n')[-1])['quant'][key]
        assert all(p['scope']==scope for p in q['holdings'])
        opt=next(o for o in q['options'] if o['action']=='SELL')
        return response(analysis={'key':key,'action':'TRIM','option_id':opt['id'],
            'summary':'시장 위험 변화에 맞춰 일부 조절을 권한다.','reasons':[{'text':'시장 위험이 커졌다.','sources':['war']}],
            'counterevidence':'장기 전망은 유지된다.','change_conditions':['운송 정상화'],'thesis_status':'supported'})
    result=review(store,c,snapshot=snapshot,model=model)
    assert '['+label+']' in render_followup(result)
    option=result['selected_option']
    assert option['currency']==('USD' if market=='US' else 'KRW')
    assert Decimal(option['cash_before'])==Decimal('1000' if market=='US' else '300000')


def test_a_shape_error_is_corrected_with_a_compact_prompt_not_the_whole_packet(store):
    c=choice(store);prompts=[]
    def model(prompt):
        prompts.append(prompt)
        return response(effect='WRONG') if len(prompts)==1 else response()
    r=review(store,c,model=model)
    assert r['assessment']['effect']=='REVIEW' and len(prompts)==2
    first,second=prompts
    # The correction carries the contract, the valid ids and the bad answer — never the packet again.
    assert '\nINPUT\n' in first and '\nINPUT\n' not in second
    assert '전쟁 장기화와 운송 차질' in first and '전쟁 장기화와 운송 차질' not in second
    assert '잘못된 effect' in second and '사용 가능한 근거 id' in second and '"war"' in second


def test_review_packet_carries_quant_evidence_as_ids_only_and_history_without_working(store):
    data=json.loads(BASE.read_text());snapshot=parse_snapshot(data);c=choice(store);prompts=[]
    def model(prompt):
        prompts.append(prompt)
        # A second notified review must REPLACE the first one's pending message.
        return response(key='KR:000001',pending_action='KEEP' if len(prompts)==1 else 'REPLACE',analysis={
            'action':'HOLD','option_id':None,'summary':'보유를 유지하며 사업 변화와 시장 위험을 확인한다.',
            'reasons':[{'text':'기업 전망이 유지된다.','sources':['business-1']}],
            'counterevidence':'전쟁 장기화는 부담이다.','change_conditions':['운송 정상화'],'thesis_status':'supported'})
    first=review(store,c,snapshot=snapshot,model=model)
    packet=json.loads(prompts[0].split('\nINPUT\n',1)[1])
    rows=packet['quant']['KR:000001']['evidence']
    assert rows and all(set(r)<={'id','kind','alias','text'} for r in rows)   # text stays for numeric grounding; URL/timestamps/metadata do not
    assert all('source' not in r and 'available_at' not in r for r in rows)
    assert first['assessment']['analysis']['key']=='KR:000001'        # quant validation still resolves those ids
    review(store,c,at=NOW+timedelta(minutes=1),snapshot=snapshot,model=model,trigger='again')
    packet=json.loads(prompts[1].split('\nINPUT\n',1)[1])
    assert 'analysis' not in packet['last_review']['assessment'] and packet['last_review']['assessment']['comparison']
