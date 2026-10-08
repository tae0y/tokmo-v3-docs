from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

from tokmo.records.news_market_observations import read_market_observations
from tokmo.programs.news_incremental_review import market_progress, market_basis
from test_news_incremental_review import setup, AT


def source(path):
    c=sqlite3.connect(path)
    c.executescript('CREATE TABLE market_regime_shadow_claim(identity_digest,market,symbol,state);'
        'CREATE TABLE market_regime_shadow_result(identity_digest,fetched_at,evaluated_at,completed_session_date,result_payload);')
    for day,number in [('2026-09-07','-0.1'),('2026-09-08','-0.11'),('2026-09-10','-0.12')]:
        data=dict(symbol='1',kind='CORE_ANALYZER',as_of=day,last_session_date=day,formula_version='v1',adjustment_mode='ADJUSTED',
            components=[dict(metrics=[dict(name='drawdown',unit='RETURN',value=number,available=True)])])
        c.execute('INSERT INTO market_regime_shadow_claim VALUES(?,?,?,?)',(day,'KR','1','SUCCEEDED'))
        c.execute('INSERT INTO market_regime_shadow_result VALUES(?,?,?,?,?)',(day,day+'T16:00:00+09:00',day+'T16:00:01+09:00',day,json.dumps(data)))
    c.commit();c.close()


def test_reader_is_dated_read_only_and_excludes_future_captures(tmp_path):
    path=tmp_path/'s.db';source(path);before=path.read_bytes()
    data=read_market_observations(path,['KR:1','KR:2'],at=AT)
    assert data['series'][0]['session']=='2026-09-08'
    assert data['series'][0]['metrics']['drawdown']==dict(value='-0.11',unit='RETURN')
    assert data['series'][1]['status']=='MISSING'
    assert path.read_bytes()==before
    old=read_market_observations(path,['KR:1'],at=AT+timedelta(days=10))
    assert old['series'][0]['status']=='AGE_LIMIT_EXCEEDED'
    assert read_market_observations(tmp_path/'absent',['KR:1'],at=AT)['status']=='MISSING'
    assert not (tmp_path/'absent').exists()


def test_invalid_measurement_is_not_replaced_with_old_success(tmp_path):
    path=tmp_path/'s.db';source(path)
    c=sqlite3.connect(path)
    value=json.loads(c.execute("select result_payload from market_regime_shadow_result where identity_digest='2026-09-08'").fetchone()[0])
    value['components'][0]['metrics'][0]['value']='NaN'
    c.execute("update market_regime_shadow_result set result_payload=? where identity_digest='2026-09-08'",(json.dumps(value),));c.commit();c.close()
    assert read_market_observations(path,['KR:1'],at=AT)['series'][0]['status']=='INVALID'


def snapshot(value='-0.11', session='2026-09-08'):
    return dict(status='AVAILABLE',series=[dict(key='KR:1',status='DATED',session=session,source_id='source:'+session,
        available_at=session+'T16:00:00+09:00',formula='v1',adjustment_mode='ADJUSTED',metrics={'drawdown':dict(value=value,unit='RETURN')})])


def test_cumulative_and_last_assessment_anchors_are_distinct():
    previous=dict(market_anchor=snapshot('-0.1','2026-09-07'),context_snapshot={'market_observations':snapshot()})
    current=snapshot('-0.12','2026-09-09')
    changes=market_progress(previous,current)
    assert [(r['anchor'],r['changes']['drawdown']['delta']) for r in changes]==[
        ('FIRST_OBSERVATION','-0.02'),('LAST_ASSESSMENT','-0.01')]
    current['series'][0]['status']='AGE_LIMIT_EXCEEDED'
    assert not market_progress(previous,current)


def test_missing_first_capture_does_not_permanently_erase_original_anchor():
    from tokmo.programs.news_incremental_review import first_market_anchor
    empty=first_market_anchor(None,dict(status='MISSING',series=[]))
    first=first_market_anchor(empty,snapshot())
    assert first['series'][0]['metrics']['drawdown']['value']=='-0.11'
    later=first_market_anchor(first,snapshot('-0.12','2026-09-09'))
    assert later==first


def test_metric_change_reassesses_but_repeated_reads_do_not_move_anchor(tmp_path):
    store,engine,calls,data=setup(tmp_path)
    try:
        data['_review_market_observations']=snapshot()
        result=engine.tick([],AT+timedelta(hours=6))
        assert result['calls']==1 and not result['failures']
        original=deepcopy(store.events()[0]['market_anchor'])
        data['_review_market_observations']['series'][0]['available_at']='2026-09-09T16:00:00+09:00'
        result=engine.tick([],AT+timedelta(hours=12))
        assert result['calls']==0 and result['reused_reviews']
        data['_review_market_observations']=snapshot('-0.12','2026-09-09')
        # Probe a due review before the unchanged-source expiry.
        e=store.events()[0];e['next_check_at']=(AT+timedelta(hours=13)).isoformat();store.save_event(e)
        result=engine.tick([],AT+timedelta(hours=13))
        assert result['calls']==1 and not result['failures']
        assert 'MARKET_CHANGED' in result['review_reuse_misses'][0]['reasons']
        assert store.events()[0]['market_anchor']==original
        assert calls[-1]['context']['market_progress'][0]['changes']['drawdown']['delta']=='-0.01'
    finally:store.close()


def test_missing_data_invalidates_reuse_without_inventing_market_direction(tmp_path):
    store,engine,calls,data=setup(tmp_path)
    try:
        data['_review_market_observations']=snapshot()
        engine.tick([],AT+timedelta(hours=6))
        data['_review_market_observations']=dict(status='ERROR',series=[])
        result=engine.tick([],AT+timedelta(hours=12))
        assert result['calls']==1 and not result['failures']
        assert calls[-1]['context']['market_observations']['status']=='ERROR'
        assert calls[-1]['context']['market_progress']==[]
    finally:store.close()
