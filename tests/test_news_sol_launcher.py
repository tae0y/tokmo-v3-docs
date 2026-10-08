"""Bounded NEWS addition to the installed common launcher; no real AI in tests."""
import importlib.machinery
import json
import os
from pathlib import Path
import types
from datetime import timedelta
import pytest

CANDIDATE = Path(os.environ.get('NEWS_LAUNCHER_CANDIDATE', str(Path.home()/'.hermes/scripts/minimal-continuity-cli')))

@pytest.fixture
def setup(tmp_path, monkeypatch):
    m = types.ModuleType('news_launcher')
    m.__file__ = str(CANDIDATE)
    exec(compile(CANDIDATE.read_text(), str(CANDIDATE), 'exec'), m.__dict__)
    monkeypatch.setattr(m, 'home', lambda: tmp_path/'home')
    m.home().mkdir()
    (m.home()/'MODEL_ROUTING_STATE.md').write_text('# Routing\n## 현재 상태\n- routing admission: exact model ID\n- SOL alternative route: `model=gpt-5.6-sol`\n')
    quota = m.home()/'cron/state/model-quota-latest.json'
    m.atomic_json(quota, {'schema':'model-quota-latest-v1', 'observed_at':m.iso_now(),
        'providers':{'codex':{'ok':True,'windows':[{'label':'Session','remaining':90}]}}})
    work = tmp_path/'work'; work.mkdir()
    prompt = tmp_path/'prompt.txt'; prompt.write_text('harmless news fixture')
    req = dict(schema=m.SCHEMA,task_key='news-fixture',purpose='tokmo-news-confirmation',
        role='analysis',provider='codex',model='gpt-5.6-sol',continuity='fresh',workspace=str(work),
        prompt_file=str(prompt),output_dir=str(tmp_path/'output'),timeout_seconds=120,max_turns=1,
        response_schema={'type':'object','properties':{'ok':{'type':'boolean'}},'required':['ok'],'additionalProperties':False},
        expected_terminal_condition='one JSON news judgment')
    return m,req,quota


def test_news_is_short_but_general_jobs_keep_their_contract(setup):
    m,r,q=setup
    assert not m.validate_request(dict(r))[0]
    general=dict(r,purpose='implementation',role='implementation')
    errors=m.validate_request(general)[0]
    assert {'response_schema','timeout_seconds','max_turns'} <= {e['field'] for e in errors}

@pytest.mark.parametrize('field,value', [('provider','claude'),('model','gpt-6-astra'),('role','implementation'),
    ('continuity','resume'),('timeout_seconds',121),('timeout_seconds',float('nan')),('max_turns',2),
    ('response_schema',None),('executable','/bin/true'),('tools','all')])
def test_news_contract_is_not_an_arbitrary_agent_escape(setup,field,value):
    m,r,q=setup; r[field]=value
    assert m.validate_request(r)[0]

@pytest.mark.parametrize('windows', [[],[{'label':'Session','remaining':0}],
    [{'label':'Session','remaining':True}],[{'label':'Session','remaining':float('nan')}],
    [{'label':'Session','remaining':101}],[{'label':'other','remaining':90}],
    [{'label':'Session','remaining':90},{'label':'Week','remaining':0}]])
def test_unavailable_or_exhausted_news_quota_is_denied(setup,windows):
    m,r,q=setup; data=m.read_json(q); data['providers']['codex']['windows']=windows; m.atomic_json(q,data)
    assert m.routing_receipt(r)[1]

@pytest.mark.parametrize('minutes', [-46,3])
def test_stale_or_future_observation_is_denied(setup,minutes):
    m,r,q=setup; data=m.read_json(q); data['observed_at']=(m.now()+timedelta(minutes=minutes)).isoformat();m.atomic_json(q,data)
    assert m.routing_receipt(r)[1]


def prepare(m,r,monkeypatch,tmp_path,tool=False):
    fake=tmp_path/'fake-codex'
    fake.write_text('#!/usr/bin/env python3\nimport sys,json,os\n'
        'if "--version" in sys.argv: print("fixture 1");sys.exit(0)\n'
        'assert not any(k in os.environ for k in ("OPENAI_API_KEY","CODEX_API_KEY","ANTHROPIC_API_KEY"))\n'
        'assert sys.argv[-1]=="-" and "--ignore-user-config" in sys.argv and "--ephemeral" in sys.argv\n'
        'assert sys.stdin.read()=="harmless news fixture"\n'
        'print(json.dumps({"type":"thread.started","thread_id":"fixture"}))\n'
        + ('print(json.dumps({"type":"item.completed","item":{"type":"command_execution"}}))\n' if tool else '')
        + 'print(json.dumps({"type":"item.completed","item":{"type":"agent_message","text":"{\\"ok\\":true}"}}))\n'
        'print(json.dumps({"type":"turn.completed","usage":{"input_tokens":1,"output_tokens":1}}))\n')
    fake.chmod(0o700)
    monkeypatch.setattr(m,'executable',lambda _:str(fake))
    req=tmp_path/'request.json';m.atomic_json(req,r)
    assert m.preflight(req)==0
    return req,Path(r['output_dir'])


def test_real_preflight_run_receipts_and_no_tool_or_key_escape(setup,monkeypatch,tmp_path):
    m,r,q=setup
    for k in ['OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY']:monkeypatch.setenv(k,'fixture-not-a-real-key')
    req,out=prepare(m,r,monkeypatch,tmp_path)
    plan=m.read_json(out/'PLAN.json')
    assert 'model_reasoning_effort="high"' in plan['argv']
    assert 'features.shell_tool=false' in plan['argv'] and 'web_search="disabled"' in plan['argv']
    assert m.run(req,out/'READY.json')==0
    result=m.read_json(out/'RESULT.json')
    assert result['route_admission']=='ADMITTED' and result['model_api_started']=='TRUE'
    assert result['fallback_used'] is False and result['descriptor_action']=='none'
    assert not m.descriptor_path(r['task_key']).exists()


def test_schema_drift_and_quota_drift_block_before_ready_consumption(setup,monkeypatch,tmp_path):
    m,r,q=setup; req,out=prepare(m,r,monkeypatch,tmp_path)
    schema=out/'response-schema.json'; original=schema.read_bytes();schema.write_text('{}')
    assert m.run(req,out/'READY.json')==2 and (out/'READY.json').exists()
    schema.write_bytes(original)
    data=m.read_json(q);data['providers']['codex']['windows'][0]['remaining']=0;m.atomic_json(q,data)
    assert m.run(req,out/'READY.json')==2 and (out/'READY.json').exists()
    assert not (out/'RESULT.json').exists()


def test_unexpected_tool_event_is_failed_not_success(setup,monkeypatch,tmp_path):
    m,r,q=setup;req,out=prepare(m,r,monkeypatch,tmp_path,tool=True)
    assert m.run(req,out/'READY.json')==2
    assert m.read_json(out/'RESULT.json')['external_cli_result']=='FAILED'


@pytest.mark.parametrize('field',['timeout_seconds','max_turns'])
def test_news_requires_explicit_short_bounds(setup,field):
    m,req,_=setup
    del req[field]
    assert m.validate_request(req)[0]


def test_native_cli_warning_is_not_a_tool_call(setup):
    m,_,_=setup
    events=[{'type':'item.completed','item':{'type':'error','message':'Under-development features enabled: skip_host_skill_discovery.'}},
            {'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
            {'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}]
    assert m.news_output_error('\n'.join(json.dumps(event) for event in events)) is None


def test_news_child_timeout_is_bounded(setup):
    import sys,subprocess
    m,req,_=setup
    with pytest.raises(subprocess.TimeoutExpired):
        m.run_child([sys.executable,'-c','import time;time.sleep(10)'],req['workspace'],0.1,
                    Path(req['output_dir'])/'CANCEL.json',prompt='fixture')
