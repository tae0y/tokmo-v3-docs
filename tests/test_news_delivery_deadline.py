"""A temporary live observation window expires without stopping internal analysis."""
from datetime import datetime, timezone
import json

import pytest

from tokmo.programs.news_decision_config import delivery_window_open

END = '2026-09-08T22:00:00+09:00'


@pytest.mark.parametrize('at,allowed', [
    ('2026-09-08T21:59:59+09:00', True),
    (END, False),
    ('2026-09-09T08:00:00+09:00', False),
    ('2026-09-08T13:00:00+00:00', False),
])
def test_deadline_is_exclusive_and_does_not_rearm_next_morning(at, allowed):
    assert delivery_window_open({'delivery_until': END}, datetime.fromisoformat(at)) is allowed


@pytest.mark.parametrize('end', ['bad', None, '', '2026-09-08T22:00:00'])
def test_invalid_config_does_not_accidentally_allow_sends(end):
    assert not delivery_window_open({'delivery_until': end}, datetime.now(timezone.utc))
    assert delivery_window_open({}, datetime.now(timezone.utc))


def test_followup_rechecks_deadline_before_each_claim(monkeypatch):
    import tokmo.programs.investment_followup as followup
    class Store:
        def __init__(self):self.claimed=[];self.settled=[]
        def pending(self, **kw):return [{'id':'a'}, {'id':'b'}]
        def claim(self, key, **kw):self.claimed.append(key);return True
        def settle(self, key, **kw):self.settled.append(key)
    store=Store();allowed=[True]
    monkeypatch.setattr(followup,'render_followup',lambda review:review['id'])
    def sender(body):allowed[0]=False;return '123'
    result=followup.deliver_pending(store,at=datetime.fromisoformat(END),sender=sender,can_send=lambda:allowed[0])
    assert result==[{'id':'a','status':'SENT'}]
    assert store.claimed==store.settled==['a']


def test_news_health_is_saved_without_stdout_notice_after_deadline(tmp_path, monkeypatch, capsys):
    import run_news_decision_worker as worker
    (tmp_path/'news_decision.json').write_text(json.dumps({'enabled':True,'delivery_until':END}))
    calls=[]
    def run(*args, **kw):
        calls.append(1)
        return {'status':'DEGRADED','health':{'review_errors':['test failure']}}
    monkeypatch.setattr(worker,'run_once',run)
    assert worker.main(['--root',str(tmp_path),'--now',END],build=lambda **kw:(None,None))==2
    summary=json.loads(capsys.readouterr().out)
    assert calls==[1] and 'notice' not in summary
    saved=json.loads((tmp_path/'news/decision-worker/latest.json').read_text())
    assert saved['health']['review_errors']==['test failure']
    assert saved['notice_suppressed']=='delivery_window_expired'


def test_followup_keeps_analysis_but_silences_expired_health(tmp_path, monkeypatch, capsys):
    import run_investment_followup_cron as cron
    (tmp_path/'investment_followup.json').write_text(json.dumps({
        'enabled':True,'delivery_enabled':True,'delivery_until':'2000-01-01T22:00:00+09:00'}))
    seen=[]
    def run(args):
        seen.extend(args)
        print(json.dumps({'errors':[{'error':'test failure'}]}))
        return 2
    monkeypatch.setattr(cron,'run',run)
    assert cron.main(['--root',str(tmp_path)])==0
    assert '--delivery-until' in seen and '--send' in seen
    assert capsys.readouterr().out==''
    assert json.loads((tmp_path/'ops/investment-followup/health.json').read_text())['status']=='DEGRADED'


def test_scheduled_observation_after_deadline_is_silent(tmp_path, monkeypatch, capsys):
    import report_news_observation as observation
    (tmp_path/'news_decision.json').write_text(json.dumps({'enabled':True,'delivery_until':'2000-01-01T22:00:00+09:00'}))
    monkeypatch.setattr(observation,'build_report',lambda *args, **kw:pytest.fail('expired scheduled send'))
    assert observation.main(['--root',str(tmp_path),'--hermes',str(tmp_path),'--send'])==0
    assert capsys.readouterr().out==''
