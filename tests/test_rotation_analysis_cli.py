import importlib.util
from pathlib import Path
from datetime import timedelta
from types import SimpleNamespace

from test_rotation_analysis import NOW, DEADLINE


def tool():
    path=Path(__file__).parents[1]/'tools/run_hyundai_rotation_analysis.py'
    spec=importlib.util.spec_from_file_location('rotation_analysis_cli',path)
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    return module


def test_wait_loop_checks_owner_resolution_without_second_analysis():
    current=[NOW];calls=[]
    def run():
        calls.append(current[0]);return {'status':'WAITING_APPROVAL'} if len(calls)==1 else {'status':'COMPLETE'}
    def sleep(seconds):current[0]+=timedelta(seconds=seconds)
    result=tool().run_with_wait(run,clock=lambda:current[0],sleep=sleep,watch=True)
    assert result['status']=='COMPLETE' and len(calls)==2


def test_wait_loop_calls_expiry_at_exact_deadline():
    current=[DEADLINE-timedelta(seconds=2)]
    def run():return {'status':'EXPIRED' if current[0]>=DEADLINE else 'WAITING_APPROVAL'}
    result=tool().run_with_wait(run,clock=lambda:current[0],sleep=lambda s:current.__setitem__(0,current[0]+timedelta(seconds=s)),watch=True)
    assert result['status']=='EXPIRED' and current[0]==DEADLINE


def test_wait_loop_follows_the_document_deadline_past_the_analysis_close():
    closes=DEADLINE+timedelta(minutes=7);current=[DEADLINE-timedelta(seconds=2)]
    def run():
        if current[0]>=closes:return {'status':'EXPIRED'}
        return {'status':'WAITING_APPROVAL','valid_until':closes.isoformat()}
    result=tool().run_with_wait(run,clock=lambda:current[0],sleep=lambda s:current.__setitem__(0,current[0]+timedelta(seconds=s)),watch=True)
    assert result['status']=='EXPIRED' and current[0]==closes


def test_cli_help_has_no_side_effects(capsys):
    import pytest
    with pytest.raises(SystemExit) as exited:tool().main(['--help'])
    assert exited.value.code==0
    assert '--close-only' in capsys.readouterr().out


def test_cli_allows_snapshot_captured_after_start_and_prepares_before_validation(monkeypatch,tmp_path,capsys):
    import json
    from test_rotation_analysis import Clock,policy,context,snapshot,report
    from tokmo.records.rotation_analysis import RotationAnalysisStore
    module=tool();clock=Clock();calls=[];captures=[]
    class AdvancingDateTime:
        @staticmethod
        def now(tz):
            clock.at+=timedelta(milliseconds=1)
            return clock.at
    def load(project_root,data_root,*,as_of,supplement):
        captures.append(as_of)
        data=snapshot();data['as_of']=as_of.isoformat()
        clock.at+=timedelta(seconds=22)
        return data
    def analyze(data,deadline):
        calls.append(data['as_of'])
        result=report();result['as_of']=data['as_of']
        return result
    monkeypatch.setattr(module,'datetime',AdvancingDateTime)
    monkeypatch.setattr(module,'select_policy',lambda state:policy())
    monkeypatch.setattr(module,'saved_market_day',lambda *a,**kw:{'regular_open_at':NOW.replace(hour=0).isoformat(),'last_closed_at':None})
    monkeypatch.setattr(module,'read_rotation_context',lambda *a,**kw:context())
    monkeypatch.setattr(module,'load_local_snapshot',load)
    monkeypatch.setattr(module,'ExistingQuantAnalyzer',lambda **kw:analyze)
    monkeypatch.setattr(module,'group_sender',lambda root:lambda *a,**kw:'telegram-group:-123:7')
    assert module.main(['--state-dir',str(tmp_path)])==0
    result=RotationAnalysisStore(tmp_path/'decisions').read(policy(),NOW.date())
    assert result['started_at']<captures[0].isoformat()<result['produced_at']
    assert result['data_as_of']==captures[0].isoformat() and calls==[captures[0].isoformat()]
    output=json.loads(capsys.readouterr().out)
    assert output['status']=='COMPLETE' and output['notification']=='SENT'


import json
import sqlite3
import pytest
from test_rotation_analysis import policy
from test_rotation_analysis_inputs import closed_calendar
from tokmo.records.rotation_analysis import RotationAnalysisStore
from tokmo.values.rotation_decision_file import parse_decision_file


@pytest.mark.parametrize('case', ['holiday','close_first','previous_failed','unknown','unknown_close','stale_cache','stale_history',
    'future_history','regular_history','wrong_source','missing_cache','corrupt_cache','missing_history',
    'corrupt_history','missing_lookahead','corrupt_lookahead','lookahead_conflict','before_open','after_close'])
def test_cli_calendar_skip_is_confirmed_terminal_and_has_no_expensive_work(tmp_path,monkeypatch,capsys,case):
    from tokmo.records.calendar_cache import CalendarCacheStore
    from tokmo.values.toss_market_calendar import CalendarSession,IntegratedHour,KrMarketDay
    module=tool();current=[NOW];sent=[]
    ops=closed_calendar(tmp_path/'data')
    if case in {'unknown','unknown_close'}:
        with sqlite3.connect(ops/'regular_session_history.sqlite3') as db:db.execute('DELETE FROM regular_session_history')
    if case in {'stale_cache','corrupt_cache'}:
        path=ops/'calendar_cache.json'
        path.write_text(path.read_text().replace(NOW.date().isoformat(),(NOW-timedelta(days=1)).date().isoformat()) if case=='stale_cache' else '{')
    if case in {'stale_history','future_history','regular_history','wrong_source'}:
        field,value={'stale_history':('captured_at',(NOW-timedelta(days=1)).isoformat()),
            'future_history':('captured_at',(NOW+timedelta(hours=1)).isoformat()),
            'regular_history':('session','regular'),'wrong_source':('source_identity','calendar:US:2026-09-16:0')}[case]
        with sqlite3.connect(ops/'regular_session_history.sqlite3') as db:
            db.execute(f'UPDATE regular_session_history SET {field}=?',(value,))
    for part,name in [('cache','calendar_cache.json'),('history','regular_session_history.sqlite3'),('lookahead','calendar_lookahead.sqlite3')]:
        if case=='missing_'+part:(ops/name).unlink()
        if case=='corrupt_'+part and part!='cache':(ops/name).write_bytes(b'broken')
    if case in {'lookahead_conflict','before_open','after_close'}:
        with sqlite3.connect(ops/'calendar_lookahead.sqlite3') as db:
            row=json.loads(db.execute('SELECT payload_json FROM calendar_lookahead').fetchone()[0])
            previous=row['trade_date'];row['trade_date']=row['source_trade_date']=NOW.date().isoformat()
            for s in row['sessions']:
                for k in ('start','end'):s[k]=s[k].replace(previous,NOW.date().isoformat())
            db.execute('UPDATE calendar_lookahead SET trade_date=?,payload_json=?',(row['trade_date'],json.dumps(row)))
    if case in {'before_open','after_close'}:
        with sqlite3.connect(ops/'regular_session_history.sqlite3') as db:
            db.execute("UPDATE regular_session_history SET session='regular'")
        (ops/'calendar_cache.json').unlink()
        session=CalendarSession(NOW.replace(hour=0,minute=0),NOW.replace(hour=6,minute=30))
        CalendarCacheStore(ops/'calendar_cache.json',clock=lambda:NOW).write(KrMarketDay(NOW.date(),IntegratedHour(regular_market=session)),None)
        current[0]=NOW.replace(hour=23)-timedelta(days=1) if case=='before_open' else NOW.replace(hour=7)
        with pytest.raises(ValueError,match='calendar_closed_or_unknown:'):module.saved_market_day(tmp_path/'data',now=current[0])
        return
    if case in {'close_first','unknown_close'}:current[0]=DEADLINE
    monkeypatch.setattr(module,'datetime',SimpleNamespace(now=lambda tz:current[0]))
    monkeypatch.setattr(module,'select_policy',lambda _:policy())
    def forbidden(*a,**kw):pytest.fail('holiday/calendar error must precede inputs and model')
    for name in ('read_rotation_context','load_local_snapshot'):
        monkeypatch.setattr(module,name,forbidden)
    monkeypatch.setattr(module,'ExistingQuantAnalyzer',lambda **kw:forbidden)
    monkeypatch.setattr(module,'group_sender',lambda _:lambda body,**kw:sent.append((body,kw)) or 'telegram-group:-123:7')
    monkeypatch.setattr(module.time,'sleep',forbidden)
    argv=['--state-dir',str(tmp_path/'state'),'--data-root',str(tmp_path/'data'),'--project-root',str(tmp_path),'--watch']
    if case=='previous_failed':
        from tokmo.programs.rotation_analysis import base_document
        old=base_document(policy(),started_at=NOW,now=NOW)
        old.update(status='FAILED',failure_reason='input_preparation_failed')
        store=RotationAnalysisStore(tmp_path/'state/decisions');store.write(policy(),NOW.date(),old)
        store.record_delivery(policy(),NOW.date(),{'status':'SENT','receipt':{'message_id':'old'}})
        previous={p:p.read_bytes() for p in store.root.glob('*.json')}
    expected='SKIPPED' if case in {'holiday','close_first'} else 'FAILED'
    before={p:p.read_bytes() for p in ops.iterdir()}
    assert module.main(argv+(['--close-only'] if case in {'close_first','unknown_close'} else []))==(0 if expected=='SKIPPED' else 2)
    assert json.loads(capsys.readouterr().out)['status']==expected
    store=RotationAnalysisStore(tmp_path/'state/decisions');doc=store.read(policy(),NOW.date())
    assert doc['status']==expected and doc['decision'] is None and doc['band_proposal'] is None
    assert parse_decision_file(json.dumps(doc),policy=policy(),now=current[0],trade_date=NOW.date()).decision is None
    if expected=='SKIPPED':
        assert doc['failure_reason'] is None and not doc.get('failure_code') and not doc.get('proposal_id')
        assert '휴장일 미실행' in sent[0][0] and all(s not in sent[0][0] for s in ('실패','승인','진단 코드'))
        assert sent[0][1]['reply_markup'] is None and not (tmp_path/'state/analysis').exists()
    saved={p:p.read_bytes() for p in store.root.glob('*.json')}
    current[0]=DEADLINE+timedelta(minutes=1)
    for flags in ([],['--close-only']):module.main(argv+flags)
    if case=='previous_failed':assert saved==previous
    assert len(sent)==(0 if case=='previous_failed' else 1) and saved=={p:p.read_bytes() for p in store.root.glob('*.json')}
    assert before=={p:p.read_bytes() for p in ops.iterdir()}
