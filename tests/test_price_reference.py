from datetime import datetime, timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.programs.price_reference import (read_calendar, read_cache, quote_session,
    comparison_reference, capture_price_references)
from tokmo.programs.news_portfolio_context import price_lines, read_news_portfolio
from tokmo.programs.quant_input_capture import ExistingTokenReader, requested_capture_consumer

NOW = datetime.fromisoformat('2026-09-09T11:00:00+09:00')


def us_day(day):
    return dict(market='US', trade_date=day, fetched_at='2026-09-08T13:00:00+09:00', sessions=[
        dict(kind='dayMarket',start=day+'T00:00:00+00:00',end=day+'T08:00:00+00:00'),
        dict(kind='preMarket',start=day+'T08:00:00+00:00',end=day+'T13:30:00+00:00'),
        dict(kind='regularMarket',start=day+'T13:30:00+00:00',end=day+'T20:00:00+00:00'),
        dict(kind='afterMarket',start=day+'T20:00:00+00:00',end=day+'T23:50:00+00:00')])


def calendar():
    return dict(days=[us_day('2026-09-08'),us_day('2026-09-09')],
        history={'US':{'2026-09-03':True,'2026-09-04':True,'2026-09-05':False,
            '2026-09-06':False,'2026-09-07':False,'2026-09-08':True}})


def bar(day, close='100'):
    return dict(date=day, close=close, currency='USD',available_at=day+'T16:00:00-04:00',adjusted=True)


def context(quote_at, bars):
    return dict(instruments=[dict(key='US:V',symbol='V',market='US',name='비자')],holdings=[],
        _market_context=dict(calendar=calendar(),quotes={'V':dict(price='110',currency='USD',
            price_at=quote_at,age_seconds=0)},instruments=[],references={'US:V':dict(
                captured_at='2026-09-09T09:00:00+09:00',bars=bars)}))


@pytest.mark.parametrize('at,expected', [
    ('2026-09-08T14:00:00+09:00','2026-09-04'),
    ('2026-09-09T04:59:00+09:00','2026-09-04'),
    ('2026-09-09T08:49:00+09:00','2026-09-04'),
    ('2026-09-09T09:00:00+09:00','2026-09-08'),
    ('2026-09-09T09:19:00+09:00','2026-09-08'),
])
def test_us_previous_session_changes_at_broker_day_market_not_ny_midnight(at, expected):
    assert quote_session(calendar(),'US',at)['previous_date'] == expected


def test_fresh_previous_close_has_no_date_explanation_and_stale_close_has_one():
    at='2026-09-09T09:19:00+09:00'
    fresh='\n'.join(price_lines(['US:V'],context(at,[bar('2026-09-04'),bar('2026-09-08','108')]),at=at))
    assert 'V(비자)\n$110.00 / +1.9%' in fresh
    assert '등락은' not in fresh and '미갱신' not in fresh
    stale='\n'.join(price_lines(['US:V'],context(at,[bar('2026-09-04')]),at=at))
    assert '$110.00 / +10.0%' in stale
    assert '등락은 09-04 종가 대비 · 직전 거래일 09-08 자료 미갱신' in stale
    missing='\n'.join(price_lines(['US:V'],context(at,[]),at=at))
    assert '/ +0.0%' not in missing and '직전 거래일 09-08 종가 미갱신' in missing


def test_calendar_gaps_are_not_silently_treated_as_holidays():
    cal=calendar();del cal['history']['US']['2026-09-07']
    ref=comparison_reference(market='US',price_at='2026-09-08T14:00:00+09:00',currency='USD',
        bars=[bar('2026-09-04')],calendar=cal,at=NOW)
    assert ref['status']=='CALENDAR_UNKNOWN' and ref['previous_date'] is None


def test_future_close_and_wrong_currency_are_not_used():
    ref=comparison_reference(market='US',price_at=NOW,currency='USD',bars=[
        bar('2026-09-04'),dict(bar('2026-09-08'),currency='KRW'),bar('2026-09-09')],
        calendar=calendar(),at=NOW)
    assert ref['bar']['date']=='2026-09-04' and ref['status']=='STALE'


def setup(root, symbols=('V',)):
    (root/'stores').mkdir();(root/'ops').mkdir()
    c=sqlite3.connect(root/'ops/calendar_lookahead.sqlite3')
    c.execute('CREATE TABLE calendar_lookahead(payload_json)')
    c.executemany('INSERT INTO calendar_lookahead VALUES(?)',[(json.dumps(d),) for d in calendar()['days']])
    c.commit();c.close()
    c=sqlite3.connect(root/'ops/regular_session_history.sqlite3')
    c.execute('CREATE TABLE regular_session_history(market,local_date,session,captured_at)')
    c.executemany('INSERT INTO regular_session_history VALUES(?,?,?,?)',[
        ('US',day,'regular' if opened else 'no_regular','2026-09-08T13:00:00+09:00')
        for day,opened in calendar()['history']['US'].items()])
    c.commit();c.close()
    c=sqlite3.connect(root/'stores/latest_quotes.sqlite3')
    c.execute('CREATE TABLE latest_quotes(source,symbol,currency_raw,provider_timestamp,fetched_at,last_price)')
    c.executemany('INSERT INTO latest_quotes VALUES(?,?,?,?,?,?)',[
        ('toss-current-price-v1',s,'USD',NOW.isoformat(),NOW.isoformat(),'110') for s in symbols])
    c.commit();c.close()


def response(days=('2026-09-08','2026-09-04')):
    return SimpleNamespace(body=json.dumps({'result':{'candles':[
        dict(timestamp=day+'T00:00:00-04:00',openPrice='100',highPrice='109',lowPrice='99',
             closePrice='108',volume='10',currency='USD') for day in days], 'nextBefore':None}}).encode())


def reader(seen, *, at=NOW, reply=None, seconds=35):
    def transport(request):
        seen.append(request)
        assert request.operation is Op.STOCK_CANDLES
        assert request.count==10 and request.adjusted is True
        assert request.before.isoformat()=='2026-09-09T00:00:00-04:00'
        assert request.token=='already-issued-token'
        return reply if reply is not None else response()
    return ExistingTokenReader(token='already-issued-token',refs=None,transport=transport,
        clock=lambda:at,max_calls=3,deadline=at+timedelta(seconds=seconds))


def test_ten_bar_capture_is_cached_and_news_uses_it_without_quant_supplement(tmp_path):
    setup(tmp_path);seen=[]
    result=capture_price_references(tmp_path,reader(seen),now=NOW)
    assert not result['failures'] and len(seen)==1 and result['authentication_issued']==0
    assert 'already-issued-token' not in (tmp_path/'stores/price_reference.json').read_text()
    assert not (tmp_path/'ops/quant-inputs/latest.json').exists()
    assert capture_price_references(tmp_path,reader(seen),now=NOW)['skipped'] and len(seen)==1
    ref=read_cache(tmp_path)['instruments']['US:V']
    assert ref['source'] == 'Toss GET /api/v1/candles'
    c=context(NOW.isoformat(),[]);c['_market_context']['references']['US:V']=ref
    assert '$110.00 / +1.9%' in '\n'.join(price_lines(['US:V'],c,at=NOW))


def test_larger_universe_finishes_across_ticks_with_three_call_limit(tmp_path):
    setup(tmp_path,('A','B','C','D','V'));seen=[]
    capture_price_references(tmp_path,reader(seen),now=NOW)
    assert len(seen)==3
    capture_price_references(tmp_path,reader(seen),now=NOW)
    assert len(seen)==5
    capture_price_references(tmp_path,reader(seen),now=NOW)
    assert len(seen)==5


def test_missing_latest_bar_preserves_dated_fallback_and_retries_after_five_minutes(tmp_path):
    setup(tmp_path);seen=[]
    result=capture_price_references(tmp_path,reader(seen,reply=response(('2026-09-04',))),now=NOW)
    assert result['failures'][0]['reason']=='previous_session_close_not_returned'
    capture_price_references(tmp_path,reader(seen),now=NOW+timedelta(minutes=1));assert len(seen)==1
    later=NOW+timedelta(minutes=5)
    capture_price_references(tmp_path,reader(seen,at=later),now=later)
    assert len(seen)==2 and read_cache(tmp_path)['instruments']['US:V']['attempt']['error'] is None


def test_auth_failure_does_not_renew_and_retries_stop_after_three_attempts(tmp_path):
    setup(tmp_path,('A','V'));seen=[]
    for minutes,expected in [(0,1),(1,2),(5,3),(6,4),(20,5),(21,6),(60,6)]:
        # One rejected token ends that tick. Each symbol has three attempts
        # across later normal ticks, with no authentication in this consumer.
        at=NOW+timedelta(minutes=minutes)
        capture_price_references(tmp_path,reader(seen,at=at,reply=SimpleNamespace(code='auth_rejection')),now=at)
        assert len(seen)==expected
    assert [r.symbol for r in seen]==['A','V','A','V','A','V']
    assert read_cache(tmp_path)['instruments']['US:A']['attempt']['count']==3


def test_deadline_deferral_uses_no_attempt_and_calendar_reads_create_no_missing_db(tmp_path):
    assert read_calendar(tmp_path,at=NOW)==dict(days=[],history={'KR':{},'US':{}})
    assert list(tmp_path.iterdir())==[]
    setup(tmp_path);seen=[]
    capture_price_references(tmp_path,reader(seen,seconds=2),now=NOW)
    assert seen==[] and read_cache(tmp_path)['instruments']=={}


def test_rate_limit_stops_batch_and_honors_broker_retry_after(tmp_path):
    setup(tmp_path,('A','V'));seen=[]
    capture_price_references(tmp_path,reader(seen,reply=SimpleNamespace(code='http_429',retry_after=180)),now=NOW)
    assert len(seen)==1
    capture_price_references(tmp_path,reader(seen,at=NOW+timedelta(minutes=1)),now=NOW+timedelta(minutes=1))
    assert len(seen)==1
    capture_price_references(tmp_path,reader(seen,at=NOW+timedelta(minutes=3)),now=NOW+timedelta(minutes=3))
    assert [request.symbol for request in seen]==['A','V']


def test_normal_collector_empty_quant_queue_reuses_token_for_price_references(tmp_path,monkeypatch):
    setup(tmp_path);seen=[]
    def transport(request):
        seen.append(request);assert request.token=='normal-pulse-token'
        return response()
    consumer=requested_capture_consumer(project_root=tmp_path,data_root=tmp_path,clock=lambda:NOW,transport=transport)
    result=consumer(existing_token='normal-pulse-token',now=NOW,deadline=NOW+timedelta(seconds=35))
    assert result['job']=='price_references' and len(seen)==1
    assert not (tmp_path/'ops/quant-inputs/request.json').exists()


def test_calendar_archive_survives_existing_lookahead_rotation(tmp_path):
    setup(tmp_path);capture_price_references(tmp_path,reader([]),now=NOW)
    c=sqlite3.connect(tmp_path/'ops/calendar_lookahead.sqlite3')
    c.execute('DELETE FROM calendar_lookahead')
    c.execute('INSERT INTO calendar_lookahead VALUES(?)',(json.dumps(us_day('2026-09-10')),))
    c.commit();c.close()
    cal=read_calendar(tmp_path,at=NOW+timedelta(days=1))
    assert quote_session(cal,'US','2026-09-09T09:19:00+09:00')['previous_date']=='2026-09-08'


def test_kr_and_winter_us_sessions_use_calendar_times():
    cal=dict(days=[dict(market='KR',trade_date='2026-09-09',sessions=[dict(kind='regularMarket',
        start='2026-09-09T09:00:00+09:00',end='2026-09-09T15:30:00+09:00')]),
        dict(market='US',trade_date='2026-01-06',sessions=[dict(kind='dayMarket',
        start='2026-01-06T01:00:00+00:00',end='2026-01-06T09:00:00+00:00')])],
        history={'KR':{'2026-09-08':True},'US':{'2026-01-05':True}})
    assert quote_session(cal,'KR','2026-09-09T15:30:00+09:00')['previous_date']=='2026-09-08'
    assert quote_session(cal,'US','2026-01-06T10:01:00+09:00')['previous_date']=='2026-01-05'
