from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
from types import SimpleNamespace

import pytest

from tokmo.facts.toss_reads import TossReadOperation as Op, build_read_request, parse_read_response
from tokmo.facts.toss_reads_live import _wire_url
from tokmo.programs.quant_input_capture import ExistingTokenReader, capture_job
from tokmo.programs.quant_investor_flows import capture_flows, compact_flows, read_flows
from tokmo.values.quant_decisions import parse_snapshot

NOW = datetime.fromisoformat('2026-09-14T12:00:00+09:00')


def body(*, personal=None, at=NOW):
    return dict(records=[dict(date=at.date().isoformat(), updatedAt=at.isoformat(), individual=personal,
        foreigner=dict(buyVolume='100', sellVolume='987754', netBuyVolume='-987654'),
        institution=dict(buyVolume='123', sellVolume='100', netBuyVolume='23', breakdown=None))], nextUntil=None)


def parsed(value):
    req=build_read_request(Op.STOCK_INVESTOR_TRADING,token='existing-token',symbol='000001',count=21)
    result=parse_read_response(req=req,body=json.dumps(dict(result=value)).encode(),fetched_at=NOW)
    assert result.code=='success'
    return result.value


def series():
    item=capture_flows(lambda *a,**kw:parsed(body()),targets=['KR:000001'],now=NOW)['investor_flows']['KR:000001']
    day=NOW.date()
    for n in range(20):
        day-=timedelta(days=1)
        while day.weekday()>=5: day-=timedelta(days=1)
        item['records'].append(dict(date=day.isoformat(),updated_at=day.isoformat()+'T18:00:00+09:00',
            net=dict(individual='100',foreigner='-150',institution='50')))
    return item


def test_stock_wire_contract_and_atomic_null_parse():
    req=build_read_request(Op.STOCK_INVESTOR_TRADING,token='existing-token',symbol='000001',count=21,until='2026-09-14')
    assert _wire_url(req).endswith('/api/v1/stocks/000001/investor-trading?count=21&until=2026-09-14')
    assert parsed(body()).records[0].individual is None
    raw=body();raw['records'].append(dict(raw['records'][0],foreigner={'buyVolume':'1.5'}))
    assert parse_read_response(req=req,body=json.dumps(dict(result=raw)).encode(),fetched_at=NOW).code!='success'
    for kw in ({'symbol':'AAPL'},{'count':101},{'interval':'1d'},{'account_seq':'123'}):
        with pytest.raises((ValueError,TypeError)):
            build_read_request(Op.STOCK_INVESTOR_TRADING,token='existing-token',**(dict(symbol='000001')|kw))


def test_completed_windows_exclude_provisional_and_preserve_personal_unknown():
    result=compact_flows({'KR:000001':series()},key='KR:000001',now=NOW)['KR:000001']
    assert result['unit']=='shares' and result['exchange']=='KRX+NXT'
    assert result['latest']['basis']=='PROVISIONAL' and result['latest']['net']['individual'] is None
    assert [w['net']['foreigner'] for w in result['windows']]==['-750','-3000']
    assert all(w['complete'] for w in result['windows'])
    assert result['windows'][0]['end']=='2026-09-11'
    short=series();short['records']=short['records'][:3]
    r=compact_flows({'KR:000001':short},key='KR:000001',now=NOW)['KR:000001']
    assert r['windows'][0]['observed_sessions']==2 and not r['windows'][0]['complete']


def test_no_fabricated_foreigner_money_or_personal_and_bad_net_is_rejected():
    raw=body();raw['records'][0]['foreigner']['netBuyVolume']='1'
    r=capture_flows(lambda *a,**kw:parsed(raw),targets=['KR:000001'],now=NOW)
    assert r['failures'] and not r['investor_flows']
    raw=body(at=NOW+timedelta(minutes=1))
    r=capture_flows(lambda *a,**kw:parsed(raw),targets=['KR:000001'],now=NOW)
    assert r['failures']


def test_row_updated_during_request_uses_receipt_time_not_tick_start():
    during=NOW+timedelta(seconds=1)
    def fetch(*a,**kw):return parsed(body(at=during))
    fetch.clock=lambda:NOW+timedelta(seconds=2)
    result=capture_flows(fetch,targets=['KR:000001'],now=NOW)
    assert not result['failures']
    assert result['investor_flows']['KR:000001']['fetched_at']==fetch.clock().isoformat()


def test_market_krw_and_stock_share_contracts_stay_distinct():
    amount=SimpleNamespace(buy_amount='100000000000',sell_amount='50000000000')
    market=SimpleNamespace(records=[SimpleNamespace(date=NOW.date(),updated_at=NOW,individual=amount,
        foreigner=amount,institution_buy_amount='1',institution_sell_amount='2')])
    def fetch(op,**kw):
        assert kw['count']==21
        if op is Op.INDICATOR_INVESTOR_TRADING:
            assert kw['interval']=='1d';return market
        assert 'interval' not in kw;return parsed(body())
    r=capture_flows(fetch,targets=['KOSPI','KR:000001'],now=NOW)
    assert not r['failures']
    flows=compact_flows(r['investor_flows'],key='KR:000001',now=NOW)
    assert flows['KOSPI']['latest']['net']['foreigner']=='50000000000'
    assert flows['KOSPI']['unit']=='KRW' and flows['KR:000001']['unit']=='shares'
    assert flows['KOSPI']['foreigner_basis']!=flows['KR:000001']['foreigner_basis']


def test_partial_failure_keeps_original_observation_and_marks_delay(tmp_path):
    path=tmp_path/'ops/quant-inputs/latest.json';path.parent.mkdir(parents=True)
    saved=series();path.write_text(json.dumps(dict(investor_flows={'KR:000001':saved},cash=[{'keep':True}])))
    def fail(*a,**kw):raise RuntimeError('http_429')
    capture_job(fail,data=dict(positions=[],flow_targets=['KR:000001']),path=path,job='investor_flows',now=NOW+timedelta(minutes=1))
    r=read_flows(tmp_path)['KR:000001']
    assert r['records']==saved['records'] and r['fetched_at']==saved['fetched_at'] and r['error']=='http_429'
    assert json.loads(path.read_text())['cash']==[{'keep':True}]
    assert compact_flows({'KR:000001':r},key='KR:000001',now=NOW)['KR:000001']['status']=='DELAYED'


def test_transport_uses_existing_token_and_stops_on_auth_rejection(tmp_path):
    seen=[]
    def transport(req):
        seen.append(req);assert req.token=='existing-token'
        return SimpleNamespace(code='auth_rejection',retry_after=None,rate_limit_remaining=None)
    reader=ExistingTokenReader(token='existing-token',refs=None,transport=transport,clock=lambda:NOW,max_calls=3)
    capture_job(reader,data=dict(positions=[],flow_targets=['KR:000001','KR:000002']),
        path=tmp_path/'latest.json',job='investor_flows',now=NOW)
    assert len(seen)==1 and reader.auth_failed


def packet():
    from tokmo.programs.quant_decision_analysis import make_packet
    from test_quant_decisions import sample_input
    data=sample_input();data['as_of']=NOW.isoformat();data['investor_flows']={'KR:000001':series()}
    snap=parse_snapshot(data)
    return make_packet(snap,snap.instruments[0],'personal-a','personal')


def test_same_context_for_quant_and_dialogue_with_unit_grounding():
    from test_quant_decisions import response
    from tokmo.programs.quant_decision_analysis import validate_analysis, AnalysisValidationError
    p=packet();raw=response(p)
    raw['reasons']=[dict(text='외국인이 987,654주 순매도했습니다.',sources=['investor_flows'])]
    assert validate_analysis(raw,p)['reasons'][0]['sources']==['investor_flows']
    raw['reasons'][0]['text']='외국인이 987,654원 순매도했습니다.'
    with pytest.raises(AnalysisValidationError):validate_analysis(raw,p)


def test_unchanged_flow_record_updates_do_not_force_reanalysis():
    from tokmo.programs.quant_decision_analysis import _reuse_context
    p=packet();other=deepcopy(p)
    other['investor_flows']['KR:000001']['latest']['updated_at']=(NOW-timedelta(minutes=1)).isoformat()
    assert _reuse_context(p)==_reuse_context(other)
    other['investor_flows']['KR:000001']['latest']['net']['foreigner']='-100'
    assert _reuse_context(p)!=_reuse_context(other)


def test_us_omits_domestic_flows_and_future_or_stale_data_not_current():
    assert compact_flows({'KR:000001':series()},key='US:V',now=NOW)=={}
    old=series();old['records']=old['records'][1:]
    assert compact_flows({'KR:000001':old},key='KR:000001',now=NOW)['KR:000001']['status']=='LATEST_REPORTED_DAY_ONLY'
    assert compact_flows({'KR:000001':series()},key='KR:000001',now=NOW+timedelta(hours=2))['KR:000001']['status']=='DELAYED'


def test_refresh_opt_in_and_only_kr_inventory(tmp_path,monkeypatch):
    from tokmo.programs.quant_input_refresh import inventory_jobs
    data=dict(positions=[],instruments=[dict(market='KR',symbol='000001',price='100',price_at=NOW.isoformat()),
                                       dict(market='US',symbol='SAMPLE',price='100',price_at=NOW.isoformat())])
    cal=dict(days=[],history={'KR':{},'US':{}})
    jobs,_=inventory_jobs(data,cal,NOW,investor_flows=True)
    flows=[j for j in jobs if j['kind']=='investor_flows']
    assert [j['data']['flow_targets'] for j in flows]==[['KOSPI','KOSDAQ'],['KR:000001']]
    jobs,_=inventory_jobs(data,cal,NOW)
    assert not any(j['kind']=='investor_flows' for j in jobs)
