"""Bounded repair: real source/Pulse paths, synthetic I/O and temporary roots."""
from dataclasses import replace
from datetime import timedelta
import json

from test_opportunity_rotation import at
from test_opportunity_rotation_controller_readiness import capture_clocked
from test_pulse_cadence_token_coordination import _deps, T1
from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.programs.quant_input_capture import ExistingTokenReader
from tokmo.facts.toss_reads import TossReadOperation as Op
from test_opportunity_rotation_source import wire
from tokmo.values.opportunity_rotation import instant


def test_normal_and_four_quant_starts_leave_bounded_paper_progress(tmp_path):
    normal, paper = [], []
    _,_,fixture,_,_=capture_clocked(tmp_path/'source','08:00:00',1)
    public,_=fixture.transports
    def quant(**kw):
        # Exercise the production bearer reader with valid public responses.
        reader = ExistingTokenReader(token=kw['existing_token'], refs=None,
            transport=public, clock=lambda:T1, max_calls=4,
            deadline=kw['deadline'], **({'wire_recorder':kw['wire_recorder']} if 'wire_recorder' in kw else {}))
        for _ in range(4):
            reader(Op.STOCKS, symbols=('111111',))
        return dict(calls=reader.calls)
    def capture(**kw):
        paper.append(kw['remaining_budget'])
        for i in range(kw['remaining_budget']):
            kw['wire_recorder']('OPPORTUNITY_READ',f'{i:064x}')
        return dict(status='CAPTURED',calls=kw['remaining_budget'])
    deps=replace(_deps(tmp_path,T1,lambda _:True,normal),quant_input_capture=quant,opportunity_capture=capture)
    result=run_pulse_once(root=tmp_path,policy_version='pulse-60s-v1',scheduled_for_utc=T1,observed_now_utc=T1,deps=deps)
    assert paper == [6]
    assert result.wire_counts['QUANT_READ'] == 4
    assert result.wire_counts['total'] == 12


def test_completed_daily_cache_survives_next_day_same_completed_session(tmp_path):
    _,clock,tick,codes,wires=capture_clocked(tmp_path,'08:00:00',9)
    while not tick().get('discovery'):
        pass
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    public,prices=tick.transports
    daily=[]
    def transport(req):
        if req.interval=='1d':daily.append(req.symbol)
        return public(req)
    consumer=capture_consumer(data_root=tmp_path/'data',transport=transport,price_transport=prices,
        clock=lambda:clock[0],sleeper=lambda _:None)
    clock[0]=instant('2026-09-29T08:00:00+09:00')
    for _ in range(12):
        consumer(existing_token='synthetic',now=clock[0],deadline=clock[0]+timedelta(seconds=35),
            wire_recorder=lambda *a:None,remaining_budget=6)
        snapshot=json.loads((tmp_path/'data/opportunity-rotation/captured.json').read_text())
        clock[0]+=timedelta(minutes=1)
        if snapshot.get('discovery',{}).get('session',{}).get('date')=='2026-09-29':break
    assert snapshot['discovery']['session']['date']=='2026-09-29'
    assert daily==[]
    assert {c['symbol'] for c in snapshot['discovery']['candidates']}==set(codes)


def test_discovery_progress_survives_permanent_market_backlog(tmp_path):
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    _,clock,tick,codes,_=capture_clocked(tmp_path,'13:00:00',11,paginated=True)
    public,prices=tick.transports
    def sleeper(seconds):clock[0]+=timedelta(seconds=seconds)
    def transport(req):
        sleeper(1)
        return public(req)
    consumer=capture_consumer(data_root=tmp_path/'data',transport=transport,price_transport=prices,
        clock=lambda:clock[0],sleeper=sleeper)
    for _ in range(20):
        slot=clock[0]
        # Worst core seven + Quant four seconds have already elapsed.
        clock[0]+=timedelta(seconds=11)
        consumer(existing_token='synthetic',now=clock[0],deadline=slot+timedelta(seconds=35),
            wire_recorder=lambda *a:None,remaining_budget=6)
        snapshot=json.loads((tmp_path/'data/opportunity-rotation/captured.json').read_text())
        clock[0]=slot+timedelta(minutes=1)
        if snapshot.get('discovery'):break
    assert {c['symbol'] for c in snapshot.get('discovery',{}).get('candidates',[])}==set(codes)
    assert all(clock[0]-instant(r['at'])<timedelta(minutes=5) for r in snapshot['discovery']['rankings_as_of'])


def test_new_completed_session_fetches_gap_and_new_candidate(tmp_path):
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    from test_opportunity_rotation_source import candle
    _,clock,tick,codes,_=capture_clocked(tmp_path,'08:00:00',3)
    while not tick().get('discovery'):pass
    public,prices=tick.transports;requests=[]
    codes.append('222222')
    clock[0]=instant('2026-09-29T08:00:00+09:00')
    def transport(req):
        response=public(req)
        body=json.loads(response.body)['result']
        if req.operation.value=='KR_MARKET_CALENDAR':
            body['previousBusinessDay']['date']='2026-09-28'
        if req.interval=='1d':
            requests.append((req.symbol,req.count))
            old=dict(candle('00:00:00'),timestamp='2026-09-23T00:00:00+09:00')
            latest=dict(old,timestamp='2026-09-28T00:00:00+09:00')
            body=dict(candles=[latest,old],nextBefore=None)
        return wire(body)
    consumer=capture_consumer(data_root=tmp_path/'data',transport=transport,price_transport=prices,
        clock=lambda:clock[0],sleeper=lambda _:None)
    for _ in range(20):
        consumer(existing_token='synthetic',now=clock[0],deadline=clock[0]+timedelta(seconds=35),
            wire_recorder=lambda *a:None,remaining_budget=6)
        snapshot=json.loads((tmp_path/'data/opportunity-rotation/captured.json').read_text())
        clock[0]+=timedelta(minutes=1)
        if snapshot.get('discovery',{}).get('completed_session')=='2026-09-28':break
    assert {c['symbol'] for c in snapshot['discovery']['candidates']}==set(codes)
    assert all(c['bars'][-1]['date']=='2026-09-28' for c in snapshot['discovery']['candidates'])
    assert requests and all(n<200 for code,n in requests if code!='222222')
    assert ('222222',200) in requests


def test_raw_daily_retention_across_completed_sessions(tmp_path):
    from test_opportunity_rotation_source import candle
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    from tokmo.values.opportunity_rotation import digest
    _,clock,tick,codes,_=capture_clocked(tmp_path,'08:00:00',5)
    public,prices=tick.transports
    path=tmp_path/'data/opportunity-rotation/captured.json'
    old=dict(candle('00:00:00'),timestamp='2026-09-23T00:00:00+09:00')
    params=dict(symbol=codes[0],interval='1d',count=200,adjusted=True,before='2026-09-23T23:59:59+09:00')
    key=digest(dict(operation='STOCK_CANDLES',params=params))
    original_at='2026-09-24T08:00:00+09:00'
    # Legacy cold raw input must survive rollover until parsed and promoted.
    path.write_text(json.dumps(dict(day='2026-09-27',work=dict(discovery=dict(job=None,cache={key:
        dict(operation='STOCK_CANDLES',params=params,body=dict(result=dict(candles=[old],nextBefore=None)),
             observed_at=original_at)})))))
    requests=[];retained=[];paused=False;partial=None;previous=None
    def transport(req):
        nonlocal paused
        body=json.loads(public(req).body)['result']
        if req.operation.value=='KR_MARKET_CALENDAR':body['previousBusinessDay']['date']=completed
        if req.interval=='1d':
            requests.append((completed,req.symbol,req.count))
            overlap=old
            if completed=='2026-09-29' and req.symbol==codes[0] and req.count<200:
                overlap=dict(old,openPrice='11000',highPrice='11000',lowPrice='11000',closePrice='11000')
                if not paused:
                    paused=True
                    clock[0]+=timedelta(seconds=30)  # Defer the required full-page fallback.
            body=dict(candles=[dict(old,timestamp=completed+'T00:00:00+09:00'),overlap],nextBefore=None)
        return wire(body)
    consumer=capture_consumer(data_root=tmp_path/'data',transport=transport,price_transport=prices,
        clock=lambda:clock[0],sleeper=lambda _:None)
    for day,completed in [('2026-09-28','2026-09-23'),('2026-09-29','2026-09-28'),
                          ('2026-09-30','2026-09-29'),('2026-10-01','2026-09-30'),('2026-10-02','2026-09-30')]:
        clock[0]=instant(day+'T08:00:00+09:00')
        for _ in range(30):
            result=consumer(existing_token='synthetic',now=clock[0],deadline=clock[0]+timedelta(seconds=35),
                wire_recorder=lambda *a:None,remaining_budget=6)
            snapshot=json.loads(path.read_text())
            assert result['status']!='FAILED',snapshot.get('last_error')
            raw={k:v for k,v in snapshot['work']['discovery']['cache'].items()
                 if v['operation']=='STOCK_CANDLES' and v['params']['interval']=='1d'}
            if paused and partial is None:
                pending={k:v for k,v in raw.items() if v['params']['symbol']==codes[0]
                         and v['params']['before'][:10]==completed}
                assert len(pending)==1  # Old promoted history cannot replace this newer gap response.
                partial=next(iter(pending.values()))
                assert snapshot['daily'][codes[0]]['completed_session']=='2026-09-28'
            clock[0]+=timedelta(minutes=1)
            if snapshot.get('discovery',{}).get('session',{}).get('date')==day:break
        assert snapshot['discovery']['session']['date']==day
        assert set(snapshot['daily'])==set(codes)
        assert all(r['completed_session']==completed for r in snapshot['daily'].values())
        assert {c['symbol'] for c in snapshot['discovery']['candidates']}==set(codes)
        if completed=='2026-09-23':assert snapshot['daily'][codes[0]]['observed_at']==original_at
        if day=='2026-10-02':assert snapshot['daily']==previous
        previous=snapshot['daily']
        retained.append(dict(day=day,completed=completed,keys=sorted(raw)))
    assert ('2026-09-23',codes[0],200) not in requests
    assert requests.count(('2026-09-29',codes[0],2))==1  # Resume replays the partial raw response.
    assert requests.count(('2026-09-29',codes[0],200))==1
    assert sum(d=='2026-09-30' for d,_,_ in requests)==len(codes)
    assert partial['observed_at'].startswith('2026-09-30')
    print(json.dumps(dict(retained_raw_by_session=retained,partial_replayed=True)))
    assert [r['keys'] for r in retained]==[[],[],[],[],[]]


import pytest


@pytest.mark.parametrize('damage',[None,'currency','market','adjustment','coverage','provenance','candle_provenance','future','symbol'])
def test_quant_public_history_acceptance(tmp_path,damage):
    from tokmo.programs.quant_input_capture import capture_job
    from tokmo.programs.opportunity_rotation_capture import capture_consumer
    from tokmo.facts.opportunity_rotation import TossOpportunitySource
    _,clock,tick,codes,_=capture_clocked(tmp_path,'08:00:00',1)
    public,prices=tick.transports;daily=[]
    source=TossOpportunitySource(token='synthetic',transport=public,price_transport=prices,
        clock=lambda:clock[0],max_calls=4,pace=lambda:None)
    path=tmp_path/'data/ops/quant-inputs/latest.json'
    capture_job(lambda op,**kw:source._request(op.value,**kw),data=dict(positions=[],
        instruments=[dict(symbol=codes[0],market='KR')],completed_session='2026-09-23'),
        path=path,job='candles',now=clock[0])
    cache=json.loads(path.read_text());row=cache['instruments'][0]
    if damage=='currency':row['currency']='USD'
    if damage=='market':row['market']='US'
    if damage=='adjustment':row['bars'][0]['adjusted']=False
    if damage=='coverage':row['metrics']['price_history']['actual_count']=999
    if damage=='candle_provenance':row['metrics']['price_history']['source']='untrusted'
    if damage=='provenance':row['metrics']['shares_outstanding']['source']='unknown'
    if damage=='future':row['metrics']['price_history']['available_at']='2027-01-01T00:00:00+09:00'
    if damage=='symbol':row['symbol']='222222'
    cache['cash']=[dict(account='synthetic-private-not-public')]
    path.write_text(json.dumps(cache))
    observed=row['metrics']['price_history']['available_at']
    def transport(req):
        if req.interval=='1d':daily.append(req.symbol)
        return public(req)
    consumer=capture_consumer(data_root=tmp_path/'data',transport=transport,price_transport=prices,
        clock=lambda:clock[0],sleeper=lambda _:None)
    for _ in range(10):
        consumer(existing_token='synthetic',now=clock[0],deadline=clock[0]+timedelta(seconds=35),
            wire_recorder=lambda *a:None,remaining_budget=6)
        saved=json.loads((tmp_path/'data/opportunity-rotation/captured.json').read_text())
        clock[0]+=timedelta(minutes=1)
        if saved.get('discovery'):break
    assert bool(daily)==bool(damage)
    if not damage:assert saved['discovery']['candidates'][0]['history_observed_at']==observed
    assert 'synthetic-private-not-public' not in json.dumps(saved)


def test_cold_whole_universe_with_continuous_four_quant_wires(tmp_path,monkeypatch):
    from test_opportunity_rotation_capacity import composed, cli_step
    from test_opportunity_rotation import proposal,session
    p,clock,tick,codes,events,measurements=composed(tmp_path,monkeypatch,start='11:30:00',count=159)
    for i,code in enumerate(codes[:5]):
        p.propose([proposal(id=f'capacity-{i}',symbol=code,time='11:29:00',quantity=1)],session())
    first=None
    while clock[0]<=instant(at('13:30:00')):
        current=clock[0]
        snapshot=tick(quant_reads=4)
        if snapshot.get('market'):p.advance(snapshot['market'])
        if snapshot.get('discovery') and first is None:first=current.isoformat()
    d,m=snapshot['discovery'],snapshot['market']
    assert {c['symbol'] for c in d['candidates']}==set(codes)
    assert all(c['bars'] and instant(at('13:30:30'))-instant(c['quote']['at'])<=timedelta(minutes=5) for c in d['candidates'])
    assert all(instant(at('13:30:30'))-instant(r['at'])<=timedelta(minutes=5) for r in d['rankings_as_of'])
    assert instant(at('13:30:30'))-instant(m['valuation_at'])<=timedelta(minutes=5)
    assert set(m['marks'])==set(codes[:5])|{'069500'} and all(c['complete'] for c in m['coverage'])
    result,output=cli_step(tmp_path/'data','13:30:30')
    assert result.returncode==2 and output['decision']['status']=='FAILED',output
    assert 'GATE_NOT_INSTALLED' in output['decision']['reason'] or 'MANIFEST' in output['decision']['reason']
    assert sum(e['lane']=='QUANT_READ' for e in events)==4*len(measurements)
    assert sum(e['interval']=='1d' for e in events)==len(codes)
    print(json.dumps(dict(first_discovery=first,candidates=len(codes),ticks=len(measurements),
        wires=len(events),quant_wires=sum(e['lane']=='QUANT_READ' for e in events),
        maximum_elapsed=max(m['elapsed'] for m in measurements),valuation_at=m['valuation_at'],
        readiness='actual CLI reached missing model gate; zero external calls')))


@pytest.mark.parametrize('failure',['http_429','auth_rejection','transport_exception'])
def test_failed_quant_start_is_durable_and_blocks_further_optional_io(tmp_path,failure):
    from tokmo.facts.toss import _TossTransportFailure
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    normal,seen=[],[]
    def transport(req):
        if failure=='transport_exception':raise RuntimeError('synthetic transport failure')
        return _TossTransportFailure(code=failure)
    def quant(**kw):
        reader=ExistingTokenReader(token=kw['existing_token'],refs=None,transport=transport,
            clock=lambda:T1,deadline=kw['deadline'],wire_recorder=kw['wire_recorder'])
        try:reader(Op.STOCKS,symbols=('111111',))
        except RuntimeError:
            if failure=='transport_exception':raise
        return dict(calls=reader.calls,failures=[{}])
    deps=replace(_deps(tmp_path,T1,lambda _:True,normal),quant_input_capture=quant,
        opportunity_capture=lambda **kw:seen.append(kw))
    result=run_pulse_once(root=tmp_path,policy_version='pulse-60s-v1',scheduled_for_utc=T1,observed_now_utc=T1,deps=deps)
    assert result.wire_counts['QUANT_READ']==1
    assert not seen
    store=PulseTickReceiptStore(tmp_path/'ops/pulse_tick_receipts.sqlite3',clock=lambda:T1)
    assert any(w['operation']=='QUANT_READ' for w in store.read_wires('pulse-60s-v1',int(T1.timestamp())//60))


def test_real_composed_automatic_quant_inventory_keeps_refresh_cadence(tmp_path,monkeypatch):
    from test_opportunity_rotation_capacity import composed
    _,clock,tick,codes,events,_=composed(tmp_path,monkeypatch,start='11:30:00',count=17,quant_inventory=27)
    for _ in range(55):snapshot=tick()
    refresh=json.loads((tmp_path/'data/ops/quant-inputs/refresh.json').read_text())
    assert refresh['inventory_count']==27
    successful=[j for j in refresh['jobs'].values() if j.get('last_success_at')]
    assert len(successful)>=27
    assert all(instant(j['next_at'])-instant(j['last_success_at'])==timedelta(minutes=15) for j in successful)
    assert {c['symbol'] for c in snapshot['discovery']['candidates']}==set(codes)
    assert any(e['lane']=='QUANT_READ' and e['op']=='BUYING_POWER' for e in events)
    assert any(e['lane']=='QUANT_READ' and e['interval']=='1d' for e in events)
    print(json.dumps(dict(automatic_quant_inventory=27,successful_jobs=len(successful),
        quant_wires=sum(e['lane']=='QUANT_READ' for e in events),paper_candidates=len(codes))))


def test_optional_budget_and_legacy_rollback_rolling_are_compatible(tmp_path):
    import subprocess
    from tokmo.programs.integrated_pulse import make_wire_recorder, PulseCeilingError
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    path=tmp_path/'receipts.sqlite3';version='pulse-60s-v1';epoch=int(T1.timestamp())
    store=PulseTickReceiptStore(path,clock=lambda:T1)
    for tick in range(epoch//60-4,epoch//60):
        rec=make_wire_recorder(store,version,tick,quant_enabled=True,opportunity_enabled=True)
        for op,ids in [('TOKEN',['token:tick0']),('PRICE_BATCH',['quotes:0','quotes:1']),
                       ('ORDERS_PAGE',['orders:0','orders:1']),('ORDER_DETAIL',['detail:order']),
                       ('CONDITIONAL_ORDERS',['conditional:OPEN:0'])]:
            for identity in ids:rec(op,identity)
        for i in range(4):rec('QUANT_READ',f'{i:064x}')
        for i in range(6):rec('OPPORTUNITY_READ',f'{i:064x}')
        with pytest.raises(PulseCeilingError):rec('OPPORTUNITY_READ','f'*64)
        with pytest.raises(PulseCeilingError):rec('PRICE_BATCH','quotes:2')
    assert store.rolling_wire_starts(version,now_epoch=epoch)==68
    # Exact pre-repair reader on the upgraded DB, no operational checkout import.
    source=subprocess.check_output(['git','show','f308ca56e71945c83a56a40bb67de83d84d635cd:projects/tokmo-v3/src/tokmo/records/pulse_tick_receipts.py'],text=True)
    namespace={};exec(compile(source,'exact-base-pulse-receipts','exec'),namespace)
    legacy=namespace['PulseTickReceiptStore'](path,clock=lambda:T1)
    assert legacy.rolling_wire_starts(version,now_epoch=epoch)==28
    assert legacy.rolling_wire_starts(version,now_epoch=epoch)+7<=35
