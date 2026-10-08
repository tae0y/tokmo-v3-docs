"""Operational notices must not bypass the user's 22:00-08:00 quiet hours."""
from copy import deepcopy
from datetime import datetime, timedelta
import json

import pytest

import run_news_decision_worker as worker
from tokmo.programs.news_source_health import apply_collection_health, observe_tick, deliver_pending, health_adjusted_exit
from tokmo.records.news_decision_store import timestamp
from tokmo.records.source_health_store import SourceHealthStore
from test_news_source_health import apply_tick, row, canonical_instant


ERROR={'status':'DEGRADED','health':{'input_errors':[{'error':'invalid ignored input','count':2,'ids':['a','b']}]}}


def setup(root,monkeypatch,result):
    (root/'news_decision.json').write_text(json.dumps({'enabled':True,'cutoff':'2026-09-01T00:00:00+09:00'}))
    monkeypatch.setattr(worker,'run_once',lambda *a,**kw:deepcopy(result))


def run(root,now,emitted,capsys):
    code=worker.main(['--root',str(root),'--now',now],emit=emitted.append,build=lambda **kw:(None,None))
    summary=json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    latest=json.loads((root/'news/decision-worker/latest.json').read_text())
    return code,summary,latest


@pytest.mark.parametrize('result',[ERROR,{'status':'EXCEPTION','health':{'exception':'OSError: test'}}])
def test_overnight_first_error_is_retained_and_emitted_once_at_eight(tmp_path,monkeypatch,capsys,result):
    setup(tmp_path,monkeypatch,result);emitted=[]
    for when in ('2026-09-08T22:00:00+09:00','2026-09-09T05:38:00+09:00','2026-09-09T07:59:00+09:00'):
        _,summary,latest=run(tmp_path,when,emitted,capsys)
        assert not emitted and 'notice' not in summary
        assert latest['health']==result['health'] and latest['status']==result['status']
        assert latest['notice_suppressed']=='quiet_hours'
        assert latest['health_notice']['notified_at'] is None
        assert latest['health_notice']['since']=='2026-09-08T22:00:00+09:00'
    run(tmp_path,'2026-09-09T08:00:00+09:00',emitted,capsys)
    assert len(emitted)==1 and '최초 알림' in emitted[0]
    for when in ('2026-09-09T09:00:00+09:00','2026-09-09T12:00:00+09:00'):
        run(tmp_path,when,emitted,capsys)
    assert len(emitted)==1


def test_legacy_hourly_notice_does_not_resend_after_upgrade(tmp_path,monkeypatch,capsys):
    setup(tmp_path,monkeypatch,ERROR);emitted=[]
    state,_=worker.health_notice(ERROR,None,timestamp('2026-09-08T20:26:00+09:00'))
    state.update(notified_at='2026-09-09T05:38:00+09:00',next_due_at='2026-09-09T06:38:00+09:00',last_kind='REMINDER')
    folder=tmp_path/'news/decision-worker';folder.mkdir(parents=True)
    (folder/'latest.json').write_text(json.dumps({'health_notice':state}))
    for when in ('2026-09-09T06:38:00+09:00','2026-09-09T08:00:00+09:00','2026-09-09T09:00:00+09:00'):
        _,_,latest=run(tmp_path,when,emitted,capsys)
    assert not emitted and latest['status']=='DEGRADED'
    assert latest['health_notice']['next_due_at'] is None


def test_overnight_change_reassesses_current_error_and_recovery(tmp_path,monkeypatch,capsys):
    current=deepcopy(ERROR);setup(tmp_path,monkeypatch,current);emitted=[]
    run(tmp_path,'2026-09-08T21:59:00+09:00',emitted,capsys)
    current['health']['input_errors'][0]['error']='new failure'
    run(tmp_path,'2026-09-08T22:00:00+09:00',emitted,capsys)
    assert len(emitted)==1
    run(tmp_path,'2026-09-09T08:00:00+09:00',emitted,capsys)
    assert len(emitted)==2 and '문제 종류 변경' in emitted[-1] and 'new failure' in emitted[-1]
    current.update(status='OK',health={})
    run(tmp_path,'2026-09-09T22:00:00+09:00',emitted,capsys)
    assert len(emitted)==2
    run(tmp_path,'2026-09-10T08:00:00+09:00',emitted,capsys)
    assert len(emitted)==3 and '복구' in emitted[-1]


def test_error_that_resolves_before_first_morning_notice_stays_silent(tmp_path,monkeypatch,capsys):
    current=deepcopy(ERROR);setup(tmp_path,monkeypatch,current);emitted=[]
    run(tmp_path,'2026-09-08T22:00:00+09:00',emitted,capsys)
    current.update(status='OK',health={})
    run(tmp_path,'2026-09-09T08:00:00+09:00',emitted,capsys)
    assert not emitted


def test_health_handoff_checks_finish_time_when_a_run_crosses_22(tmp_path,monkeypatch,capsys):
    setup(tmp_path,monkeypatch,ERROR);emitted=[]
    instants=iter([timestamp('2026-09-08T21:59:00+09:00'),timestamp('2026-09-08T22:00:00+09:00')])
    class Clock:
        @staticmethod
        def now(tz):return next(instants)
    monkeypatch.setattr(worker,'datetime',Clock)
    monkeypatch.setattr(worker,'build_document_reader',lambda:None)
    monkeypatch.setattr(worker,'build_sec_document_reader',lambda:None)
    worker.main(['--root',str(tmp_path)],emit=emitted.append,build=lambda **kw:(None,None))
    capsys.readouterr()
    latest=json.loads((tmp_path/'news/decision-worker/latest.json').read_text())
    assert not emitted and latest['notice_suppressed']=='quiet_hours'


def test_source_failure_and_recovery_wait_without_cron_failure_bypass(tmp_path):
    store=SourceHealthStore(tmp_path/'source-health.sqlite3');emitted=[]
    t=datetime.fromisoformat('2026-09-08T22:00:00+09:00')
    bad=[row('yna-market','RUNNER_FAILURE')]
    for i in range(2):
        now=t+timedelta(minutes=5*i)
        apply_tick(store,'failure-'+str(i),bad,now)
        assert deliver_pending(store,telegram_send=emitted.append,now=now).attempted==0
        assert health_adjusted_exit(3,store,('yna-market',),mode='DELIVER',now=now)==0
    incident=store.read_health('yna-market',now=canonical_instant(now))
    pending=store.read_notifications(incident['incident_id'],now=canonical_instant(now))
    assert pending[0]['status']=='PENDING' and pending[0]['attempt_count']==0
    morning=datetime.fromisoformat('2026-09-09T08:00:00+09:00')
    assert deliver_pending(store,telegram_send=emitted.append,now=morning).sent==0
    apply_collection_health(store,observe_tick({'tick_id':'fresh-morning','sources':bad},now=morning),now=morning)
    assert deliver_pending(store,telegram_send=lambda text:emitted.append(text) or '1',now=morning).sent==1
    # A later recovery is also routine; it cannot wake the user.
    for i in range(2):
        now=morning+timedelta(hours=14,minutes=5*i)
        apply_tick(store,'recovered-'+str(i),[row('yna-market','COLLECTED')],now)
        assert deliver_pending(store,telegram_send=emitted.append,now=now).attempted==0
    assert len(emitted)==1
    assert deliver_pending(store,telegram_send=lambda text:emitted.append(text) or '2',now=morning+timedelta(days=1)).sent==0
    assert len(emitted)==1
    assert health_adjusted_exit(3,store,('unknown-source',),mode='DELIVER',now=t)==3
