from datetime import datetime,timedelta,timezone
import json
from types import SimpleNamespace
import pytest

from test_quant_decisions import sample_input
from tokmo.programs.quant_input_capture import ExistingTokenReader,capture_job
from tokmo.facts.toss_reads import TossReadOperation as Op

NOW=datetime(2026,9,7,7,tzinfo=timezone.utc)


def test_existing_account_mapping_read_does_not_initialize_or_change_store(tmp_path):
    from tokmo.programs.quant_input_capture import read_existing_account_refs
    from tokmo.records.account_native_refs import AccountNativeRefStoreV1
    import hashlib
    missing=tmp_path/'missing.sqlite3'
    assert not read_existing_account_refs(missing,clock=lambda:NOW).scopes() and not missing.exists()
    path=tmp_path/'refs.sqlite3';scope='toss-acct-a625b1d60ecf1ebc6735213a'
    AccountNativeRefStoreV1(path,clock=lambda:NOW).replace_refs({scope:'123'},receipt_binding='fake-holdings-receipt')
    path.chmod(0o400)
    before=(hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns,path.stat().st_mode)
    refs=read_existing_account_refs(path,clock=lambda:NOW)
    assert refs.reveal(scope)=='123' and '123' not in repr(refs)
    assert (hashlib.sha256(path.read_bytes()).hexdigest(),path.stat().st_mtime_ns,path.stat().st_mode)==before


def reader_with(transport,**kwargs):
    refs=SimpleNamespace(reveal=lambda account:'123')
    return ExistingTokenReader(token='existing-memory-token',refs=refs,transport=transport,clock=lambda:NOW,**kwargs)


def test_current_prices_use_injected_transport_and_shared_budget(monkeypatch):
    seen = []
    def prices(request):
        seen.append(request)
        assert request.token == 'existing-memory-token' and request.symbols == ('SAMPLE',)
        return SimpleNamespace(body=json.dumps({'result': [{'symbol':'SAMPLE',
            'lastPrice':'123','currency':'USD','timestamp':NOW.isoformat()}]}).encode())
    monkeypatch.setattr('tokmo.facts.toss_prices_live.TossPricesLiveTransport', lambda: prices)
    reader = reader_with(lambda request: pytest.fail('read transport used for current prices'), max_calls=1)
    assert reader('CURRENT_PRICES', symbols=('SAMPLE',))[0].symbol == 'SAMPLE'
    with pytest.raises(RuntimeError, match='call budget'):
        reader('CURRENT_PRICES', symbols=('SAMPLE',))
    assert len(seen) == 1 and reader.calls == [{'operation':'CURRENT_PRICES','status':'success'}]


def test_binds_exact_existing_token_to_get_request_without_authentication(tmp_path):
    seen=[]
    def transport(request):
        seen.append(request)
        return SimpleNamespace(body=json.dumps({'result':{'currency':request.currency,'cashBuyingPower':'200000'}}).encode())
    reader=reader_with(transport)
    assert reader(Op.BUYING_POWER,account='personal-a',currency='KRW').cash_buying_power=='200000'
    assert seen[0].token=='existing-memory-token' and seen[0].operation is Op.BUYING_POWER
    assert 'existing-memory-token' not in repr(reader) and 'existing-memory-token' not in json.dumps(reader.calls)


def test_expired_token_stops_this_capture_without_refresh_or_repeated_calls(tmp_path):
    seen=[]
    def transport(request):seen.append(request);return SimpleNamespace(code='auth_rejection')
    reader=reader_with(transport);result=capture_job(reader,data=sample_input(),path=tmp_path/'cache.json',job='funds',now=NOW)
    assert len(seen)==1 and reader.auth_failed and result['authentication_issued']==0
    assert result['failures']
    assert 'existing-memory-token' not in (tmp_path/'cache.json').read_text()


def test_funds_job_passes_real_request_builder_for_cash_and_fx(tmp_path):
    seen=[]
    def transport(request):
        seen.append(request)
        if request.operation is Op.BUYING_POWER:
            value={'currency':request.currency,'cashBuyingPower':'1000'}
        else:
            assert request.operation is Op.EXCHANGE_RATE
            assert (request.base_currency,request.quote_currency)==('USD','KRW')
            value={'baseCurrency':'USD','quoteCurrency':'KRW','rate':'1401','midRate':'1400',
                   'basisPoint':'7','rateChangeType':'UP','validFrom':NOW.isoformat(),
                   'validUntil':(NOW+timedelta(minutes=1)).isoformat()}
        return SimpleNamespace(body=json.dumps({'result':value}).encode())
    reader=reader_with(transport);result=capture_job(reader,data=sample_input(),path=tmp_path/'cache.json',job='funds',now=NOW)
    assert not result['failures'] and len(seen)==3
    assert all(r.token=='existing-memory-token' for r in seen)
    saved=json.loads((tmp_path/'cache.json').read_text())
    assert len(saved['cash'])==2 and saved['fx']['rate']=='1400'


def test_candles_job_uses_adjusted_daily_request_and_completed_sessions(tmp_path):
    data=sample_input();data['instruments']=data['instruments'][:1];seen=[]
    def transport(request):
        seen.append(request)
        if request.operation is Op.STOCKS:
            value=[{'symbol':'000001','name':'가상 ETF','englishName':'Sample ETF','isinCode':'KR-SAMPLE',
                    'market':'KOSPI','securityType':'ETF','isCommonShare':False,'status':'ACTIVE',
                    'currency':'KRW','sharesOutstanding':'1000'}]
        else:
            assert request.operation is Op.STOCK_CANDLES and request.adjusted is True and request.interval=='1d'
            assert request.before is not None and request.count==200
            value={'candles':[{'timestamp':t,'openPrice':'100','highPrice':'110','lowPrice':'90',
                    'closePrice':'105','volume':'1000','currency':'KRW'} for t in
                    ['2026-09-04T00:00:00+09:00','2026-09-08T00:00:00+09:00']], 'nextBefore':None}
        return SimpleNamespace(body=json.dumps({'result':value}).encode())
    reader=reader_with(transport);result=capture_job(reader,data=data,path=tmp_path/'cache.json',job='candles',now=NOW)
    assert not result['failures'] and len(seen)==2
    saved=json.loads((tmp_path/'cache.json').read_text())['instruments'][0]
    assert saved['kind']=='etf' and [b['date'] for b in saved['bars']]==['2026-09-04']
    assert saved.get('english_name') == 'Sample ETF'


def test_capture_identity_binds_market_and_symbol():
    from tokmo.programs.quant_decision_inputs import capture_market_data
    def fetch(op, **kwargs):
        if op is Op.STOCK_CANDLES: return SimpleNamespace(candles=[])
        return [SimpleNamespace(symbol='SAME',market=venue,currency=currency,
            name=english,english_name=english,security_type='STOCK',shares_outstanding='1')
            for venue,currency,english in [('NASDAQ','USD','US Company'),('KOSPI','KRW','KR Company')]]
    rows = capture_market_data(fetch,instruments=[dict(symbol='SAME',market=m) for m in ('US','KR')],now=NOW)['instruments']
    assert [r.get('english_name') for r in rows] == ['US Company','KR Company']


def test_orders_job_passes_real_builder_and_fee_parser(tmp_path):
    seen=[]
    def transport(request):
        seen.append(request)
        if request.operation is Op.COMMISSIONS:
            value=[{'marketCountry':'KR','commissionRate':'0.00015','startDate':None,'endDate':None},
                   {'marketCountry':'US','commissionRate':'0.001','startDate':None,'endDate':None}]
        elif request.operation is Op.ORDERS:value={'orders':[],'hasNext':False,'nextCursor':None}
        else:value={'conditionalOrders':[],'hasNext':False,'nextCursor':None}
        return SimpleNamespace(body=json.dumps({'result':value}).encode())
    reader=reader_with(transport);result=capture_job(reader,data=sample_input(),path=tmp_path/'cache.json',job='orders',now=NOW)
    assert not result['failures'] and len(seen)==3
    saved=json.loads((tmp_path/'cache.json').read_text())
    assert saved['fees']['personal-a:KR']['rate']=='0.00015' and saved['orders']==[]


def test_call_budget_is_bounded_without_a_retry_loop():
    seen=[]
    def transport(request):seen.append(request);return SimpleNamespace(body=b'{"result":{"sellableQuantity":"1"}}')
    reader=reader_with(transport,max_calls=1)
    reader(Op.SELLABLE_QUANTITY,account='personal-a',symbol='005380')
    with pytest.raises(RuntimeError,match='budget'):reader(Op.SELLABLE_QUANTITY,account='personal-a',symbol='000660')
    assert len(seen)==1


def test_partial_capture_preserves_previous_values_original_times_and_unrelated_fields(tmp_path):
    path=tmp_path/'cache.json'
    old={'cash':[{'account':'personal-a','scope':'personal','currency':'KRW','available':'1000',
                 'observed_at':(NOW-timedelta(days=1)).isoformat(),'source':'old source'}],
         'instruments':[{'market':'KR','symbol':'005380','name':'현대차'}]}
    path.write_text(json.dumps(old))
    def failed(*args,**kwargs):raise RuntimeError('provider failure')
    result=capture_job(failed,data=sample_input(),path=path,job='funds',now=NOW)
    saved=json.loads(path.read_text())
    assert saved['cash']==old['cash'] and saved['instruments']==old['instruments']
    assert saved['jobs']['funds:0']['failures'] and result['authentication_issued']==0


def test_sellable_job_does_not_query_cash_fx_or_orders(tmp_path):
    calls=[]
    def fetch(op,**kwargs):calls.append(op);return SimpleNamespace(sellable_quantity='80')
    capture_job(fetch,data=sample_input(),path=tmp_path/'cache.json',job='sellable',now=NOW)
    assert calls==[Op.SELLABLE_QUANTITY]
    row=json.loads((tmp_path/'cache.json').read_text())['positions'][0]
    assert row['sellable']=='80' and row['observed_at']==NOW.isoformat()


def test_fully_sold_account_still_captures_cash_for_new_investments(tmp_path):
    data=sample_input();data['positions']=[]
    data['accounts']=[{'account':'personal-a','scope':'personal'},
                      {'account':'retirement-a','scope':'retirement'}]
    calls=[]
    def fetch(op,**kwargs):
        calls.append((op,kwargs))
        if op is Op.BUYING_POWER:
            return SimpleNamespace(currency=kwargs['currency'],cash_buying_power='1000')
        return SimpleNamespace(mid_rate='1300',valid_from=NOW)
    result=capture_job(fetch,data=data,path=tmp_path/'cache.json',job='funds',now=NOW)
    saved=json.loads((tmp_path/'cache.json').read_text())
    assert not result['failures'] and len(calls)==3
    assert {c['account'] for c in saved['cash']}=={'personal-a'}
    assert {c['currency'] for c in saved['cash']}=={'KRW','USD'}


def test_conditional_group_projection_supports_existing_runtime_value_types(tmp_path):
    from tokmo.values.toss_orders import (ConditionalOrderCondition,ConditionalOrderDetailResponse,
        PaginatedConditionalOrderResponse,PaginatedOrderResponse)
    leg=ConditionalOrderCondition(type='PRICE',status='OPEN',order_price='15000')
    order=ConditionalOrderDetailResponse(conditional_order_id='fake-oco',type='OCO',status='OPEN',
        symbol='000001',market='KR',quantity='10',order_type='LIMIT',first=leg,second=leg,created_at=NOW)
    def fetch(op,**kwargs):
        if op is Op.COMMISSIONS:return ()
        if op is Op.ORDERS:return PaginatedOrderResponse(orders=())
        return PaginatedConditionalOrderResponse(conditional_orders=(order,))
    result=capture_job(fetch,data=sample_input(),path=tmp_path/'cache.json',job='orders',now=NOW)
    saved=json.loads((tmp_path/'cache.json').read_text())
    assert not result['failures']
    projected=saved['orders'][0]
    assert projected['quantity']=='10' and projected['type']=='OCO'
    assert [leg['side'] for leg in projected['legs']]==['SELL','SELL']
