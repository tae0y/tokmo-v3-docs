"""Actual Pulse composition with synthetic transports and temporary stores."""
from dataclasses import replace
from datetime import timedelta, timezone
import json
from pathlib import Path

from test_opportunity_rotation import paper, at
from test_opportunity_rotation_source import wire, candle
from test_pulse_cadence_token_coordination import _deps, T1
from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.values.opportunity_rotation import instant


def test_capture_composed_with_pulse_one_bearer_resumes_and_wire_counts(tmp_path,monkeypatch):
    import tokmo.composition as composition
    from tokmo.facts.toss_reads import TossReadOperation as Op
    clock=[instant(at('09:01:00')).astimezone(timezone.utc)];calls=[]
    root=tmp_path/'data';paper(root/'opportunity-rotation/paper')
    project=tmp_path/'project';(project/'watchlists').mkdir(parents=True)
    (project/'.env.toss').write_text('TOSS_CLIENT_ID=synthetic\nTOSS_CLIENT_SECRET=synthetic\n')
    validator=project/'scripts/playbooks/validate_playbooks.py';validator.parent.mkdir(parents=True)
    validator.write_text('def validate_tree(root):\n    return []\n')
    for market in ('KR','US'):
        (project/'watchlists'/f'{market}.json').write_text(json.dumps({'schema_version':1,'market':market,'entries':[]}))
    class ReadTransport:
        def __init__(self,**kwargs):pass
        def __call__(self,req):
            calls.append((req.operation.value+(':'+req.interval if req.operation==Op.STOCK_CANDLES else ''),req.token))
            if req.operation==Op.KR_MARKET_CALENDAR:
                def day(d):return dict(date=d,integrated=dict(regularMarket=dict(startTime=d+'T09:00:00+09:00',endTime=d+'T15:30:00+09:00')))
                return wire(dict(today=day('2026-09-28'),previousBusinessDay=day('2026-09-23'),nextBusinessDay=day('2026-09-29')))
            if req.operation==Op.RANKINGS:
                return wire(dict(rankedAt=clock[0].isoformat(),rankings=[dict(rank=1,symbol='111111',currency='KRW',price=dict(lastPrice='10000',basePrice='9000'),tradingVolume='100',tradingAmount='1000000')]))
            if req.operation==Op.STOCKS:
                return wire([dict(symbol=s,name='합성',englishName='Synthetic',isinCode='KR'+s,market='KOSPI',securityType='STOCK',isCommonShare=True,status='ACTIVE',currency='KRW',sharesOutstanding='10000',koreanMarketDetail=dict(liquidationTrading=False,nxtSupported=False,krxTradingSuspended=False)) for s in req.symbols])
            if req.interval=='1d':
                c=candle('09:00:00');c['timestamp']='2026-09-23T00:00:00+09:00'
                return wire(dict(candles=[c],nextBefore=None))
            return wire(dict(candles=[candle('09:01:00','50000')],nextBefore=None))
    class PriceTransport:
        def __init__(self,**kwargs):pass
        def __call__(self,req):
            calls.append(('CURRENT_PRICES',req.token))
            return wire([dict(symbol=s,lastPrice='10000',currency='KRW',timestamp=clock[0].isoformat()) for s in req.symbols])
    monkeypatch.setattr(composition,'TossReadsLiveTransport',ReadTransport)
    monkeypatch.setattr(composition,'TossPricesLiveTransport',PriceTransport)
    configured=composition.build_pulse_production_live_deps(project_root=project,runtime_root=root,clock=lambda:clock[0],dialogue_cutoff=clock[0],environ={'TOKMO_OPPORTUNITY_CAPTURE':'1'},capture_sleeper=lambda seconds:None)
    assert configured.opportunity_capture is not None
    normal=[]
    # Real composition producer; existing Pulse fixture deps isolate all normal lanes.
    for i in range(20):
        base=_deps(root,clock[0],lambda _:True,normal)
        from tokmo.records.calendar_lookahead import LookaheadDayV1, LookaheadSessionV1
        from tokmo.values.session_gate import Market
        lookahead=lambda market:(LookaheadDayV1(Market.KR,'2026-09-28','2026-09-28',
            (LookaheadSessionV1('regularMarket',instant(at('09:00:00')).astimezone(timezone.utc),instant(at('15:30:00')).astimezone(timezone.utc)),),clock[0],'calendar:KR:2026-09-28:0'),) if market is Market.KR else ()
        deps=replace(base,lookahead_reader=lookahead,opportunity_capture=configured.opportunity_capture,token_fetch=lambda:normal.append('token') or 'synthetic-bearer')
        previous=len(calls)
        result=run_pulse_once(root=root,policy_version='pulse-60s-v1',scheduled_for_utc=clock[0],observed_now_utc=clock[0],deps=deps)
        assert len(calls)-previous<=6
        assert result.wire_counts['total']<=17
        if len(calls)>previous:assert result.wire_counts['OPPORTUNITY_READ']==len(calls)-previous
        clock[0]+=timedelta(minutes=1)
    assert all(token=='synthetic-bearer' for _,token in calls)
    assert normal.count('token')==20
    operations=[op for op,_ in calls]
    assert 'STOCK_CANDLES:1m' in operations and 'STOCKS' in operations
    captured=json.loads((root/'opportunity-rotation/captured.json').read_text())
    assert captured['discovery']['candidates'][0]['symbol']=='111111'
    assert captured['market']['bars'][0]['symbol']=='069500'
    assert 'synthetic-bearer' not in json.dumps(captured)
    disabled=composition.build_pulse_production_live_deps(project_root=project,runtime_root=root,clock=lambda:clock[0],dialogue_cutoff=clock[0])
    assert disabled.opportunity_capture is None
    print(json.dumps(dict(synthetic=True,pulse_ticks=20,normal_token_fetches=normal.count('token'),public_capture_wires=len(calls),all_capture_bearers_equal=True,credential_persisted=False,discovery_and_market_published=True,disabled_composition_has_no_capture=True)))


def test_existing_quant_reads_preserve_paper_allowance(tmp_path):
    # Existing normal quotes use TOKEN+PRICE=2; four optional Quant wires leave
    # only one slot. No extra authentication needed when bearer already exists.
    calls=[];seen=[]
    deps=_deps(tmp_path,T1,lambda _:True,calls)
    deps=replace(deps,quant_input_capture=lambda **kw:{'calls':[{}]*4},
        opportunity_capture=lambda **kw:seen.append(kw) or {'status':'CAPTURED','calls':1})
    run_pulse_once(root=tmp_path,policy_version='pulse-60s-v1',scheduled_for_utc=T1,observed_now_utc=T1,deps=deps)
    assert seen[0]['remaining_budget']==6


def test_uninitialized_capture_does_not_request_a_pulse_token(tmp_path):
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    calls=[]
    consumer=capture_consumer(data_root=tmp_path,transport=lambda r:None,price_transport=lambda r:None,clock=lambda:T1,sleeper=lambda seconds:None)
    deps=replace(_deps(tmp_path,T1,lambda _:True,calls),opportunity_capture=consumer,quotes_provider_enabled=False)
    run_pulse_once(root=tmp_path,policy_version='pulse-60s-v1',scheduled_for_utc=T1,observed_now_utc=T1,deps=deps)
    assert 'token' not in calls


def test_caller_passes_actual_remaining_allowance_after_normal_and_quant(tmp_path):
    calls=[];seen=[]
    def capture(**kw):
        seen.append(kw['remaining_budget'])
        for i in range(kw['remaining_budget']):
            kw['wire_recorder']('OPPORTUNITY_READ',f'{i:064x}')
        return {'status':'CAPTURED','calls':kw['remaining_budget']}
    deps=replace(_deps(tmp_path,T1,lambda _:True,calls),
        quant_input_capture=lambda **kw:{'calls':[{},{}]},opportunity_capture=capture)
    result=run_pulse_once(root=tmp_path,policy_version='pulse-60s-v1',scheduled_for_utc=T1,observed_now_utc=T1,deps=deps)
    assert seen==[6]  # PAPER has its own bounded allowance after Quant.
    assert result.wire_counts['OPPORTUNITY_READ']==6
    assert result.wire_counts['total']==8  # This injected Quant stub performs no wires.
