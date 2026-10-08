"""Reasoning settings reach inference and distinct settings remain distinct experiments."""
import json
import sqlite3

import pytest

import subscription_json_model as adapter
from news_decision_models import DecisionModel
from quant_decision_models import QuantModel


@pytest.mark.parametrize('effort', ['high', 'xhigh'])
def test_news_and_followup_send_selected_effort_to_subscription_cli(monkeypatch, tmp_path, effort):
    argv_seen = []
    def run(argv, *args):
        argv_seen.append(argv)
        return '\n'.join(json.dumps(event) for event in [
            {'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
            {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':2}}])
    monkeypatch.setattr(adapter, '_run', run)
    # The live NEWS analyze stage routes through the common launcher (news_subscription_model.run_news);
    # the test only checks that the selected effort reaches the CLI argv, so the launcher delegates here.
    import news_subscription_model
    monkeypatch.setattr(news_subscription_model, 'run_news', lambda argv, prompt, cwd, timeout, **kw: run(argv, prompt, cwd, timeout))
    news = DecisionModel('codex:gpt-6-astra', 'analyze', reasoning_effort=effort)
    followup = QuantModel(env_file=tmp_path/'no-credentials', model='codex:gpt-6-astra', reasoning_effort=effort)
    assert news({'evidence':[]})['_reasoning_effort'] == effort
    assert json.loads(followup('input')) == {'ok':True}
    assert followup.calls[-1]['reasoning_effort'] == effort
    assert len(argv_seen) == 2
    assert all('model_reasoning_effort='+json.dumps(effort) in argv for argv in argv_seen)


def test_news_worker_config_reaches_final_model_only(tmp_path, monkeypatch, capsys):
    import run_news_decision_worker as worker
    (tmp_path/'news_decision.json').write_text(json.dumps({
        'enabled':True, 'delivery_enabled':False, 'screen_model':'glm-5.3',
        'analysis_model':'codex:gpt-6-astra', 'analysis_reasoning_effort':'high'}))
    seen = []
    def tick(*args, **kwargs):
        seen.append((kwargs['screen'],kwargs['analyze']))
        return {'status':'OK','health':{}}
    monkeypatch.setattr(worker,'run_once',tick)
    assert worker.main(['--root',str(tmp_path),'--now','2026-09-08T10:00:00+09:00']) == 0
    screen, analyze = seen[0]
    assert (screen.model, screen.reasoning_effort) == ('glm-5.3','low')
    assert (analyze.model, analyze.reasoning_effort) == ('codex:gpt-6-astra','high')


def test_followup_config_reaches_cli_without_enabling_delivery(tmp_path, monkeypatch):
    import run_investment_followup as cli
    import run_investment_followup_cron as cron
    (tmp_path/'investment_followup.json').write_text(json.dumps({
        'enabled':True,'delivery_enabled':False,'analysis_model':'codex:gpt-6-astra',
        'screen_model':'glm-5.3','analysis_reasoning_effort':'xhigh'}))
    seen = []
    def execute(args, at):
        seen.append(args)
        print(json.dumps({'reviewed':[], 'errors':[]}))
        return 0
    monkeypatch.setattr(cli,'execute',execute)
    assert cron.main(['--root',str(tmp_path)]) == 0
    assert seen[0].analysis_reasoning_effort == 'xhigh'
    assert seen[0].screen_model == 'glm-5.3' and not seen[0].send


def test_news_call_reuse_distinguishes_reasoning_levels(tmp_path):
    from tokmo.programs.news_decision_engine import NewsDecisionEngine
    from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
    at = timestamp('2026-09-08T10:00:00+09:00')
    class Model:
        model='codex:gpt-6-astra'
        reasoning_effort='high'
        def __call__(self, request):
            return {'ok':self.reasoning_effort}
    model=Model()
    store=NewsDecisionStore(tmp_path/'state.sqlite3')
    try:
        engine=NewsDecisionEngine(store,None,model,clock=lambda:at)
        report={'calls':0}
        assert engine._call('analyze',{},report)[0]['ok'] == 'high'
        assert engine._call('analyze',{},report)[0]['ok'] == 'high'
        assert report['calls'] == 1
        model.reasoning_effort='xhigh'
        assert engine._call('analyze',{},report)[0]['ok'] == 'xhigh'
        assert report['calls'] == 2
    finally:
        store.close()


@pytest.mark.parametrize('saved_effort,reused', [('high',True),('medium',False),(None,False)])
def test_followup_replay_reuses_only_matching_reasoning(tmp_path, saved_effort, reused):
    from replay_investment_followup import ExactRequestReuse
    report={'model_roles':{'analysis':'codex:gpt-6-astra'},
            'model_reasoning_efforts':{'analysis':saved_effort}}
    (tmp_path/'report.json').write_text(json.dumps(report))
    folder=tmp_path/'branch';folder.mkdir()
    with sqlite3.connect(folder/'followup.sqlite3') as db:
        db.execute('CREATE TABLE investment_calls(id,request,response,status,usage)')
        db.execute('INSERT INTO investment_calls VALUES(?,?,?,?,?)',
                   ('review:a','same prompt','{"answer":"saved"}','OK','{}'))
    class Model:
        model='codex:gpt-6-astra'
        reasoning_effort='high'
        def __init__(self):self.calls=[]
        def __call__(self,prompt):
            self.calls.append({'model':self.model,'reasoning_effort':self.reasoning_effort,'status':'RETURNED'})
            return {'answer':'fresh'}
    backend=Model()
    wrapped=ExactRequestReuse(backend,tmp_path,'analysis')
    assert wrapped('same prompt') == {'answer':'saved' if reused else 'fresh'}
    assert len(backend.calls) == (0 if reused else 1)


def test_news_replay_records_actual_reasoning_setting(tmp_path, monkeypatch, capsys):
    import replay_news_alerts as replay
    case=tmp_path/'case.json';case.write_text('{"version":1,"id":"effort"}')
    def run(case, state, **kwargs):
        assert kwargs['screen'].model == 'glm-5.3'
        assert kwargs['analyze'].reasoning_effort == 'xhigh'
        return {'passed':True,'mode':'live'}
    monkeypatch.setattr(replay,'replay',run)
    monkeypatch.setattr(replay,'markdown',lambda report:'test report')
    assert replay.main(['--case',str(case),'--mode','live','--screen-model','glm-5.3',
                        '--analysis-model','codex:gpt-6-astra','--analysis-reasoning-effort','xhigh',
                        '--output',str(tmp_path/'out')]) == 0
    from pathlib import Path
    report=json.loads(Path(json.loads(capsys.readouterr().out)['report']).read_text())
    assert report['model_reasoning_efforts'] == {'screen':'low','analyze':'xhigh'}
