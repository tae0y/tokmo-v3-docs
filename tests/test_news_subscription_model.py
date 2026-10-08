"""Subscription inference is finite, isolated and charged to existing call accounting."""
import json
import subprocess

import pytest

import subscription_json_model as adapter
from news_decision_models import DecisionModel


def test_codex_uses_supplied_json_schema_and_reports_usage_without_tools(monkeypatch):
    seen = []
    def run(argv, prompt, cwd, timeout):
        seen.append((argv, prompt, cwd, timeout))
        return '\n'.join(json.dumps(e) for e in [
            {'type':'item.completed','item':{'type':'agent_message','text':'{"ok":true}'}},
            {'type':'turn.completed','usage':{'input_tokens':120,'output_tokens':15}}])
    monkeypatch.setattr(adapter, '_run', run)
    text, meta = adapter.infer('archived input',provider='codex',model='gpt-6-astra',timeout=37,schema={'type':'object'})
    assert json.loads(text) == {'ok':True}
    assert meta['_usage']['total_tokens'] == 135
    argv, prompt, cwd, timeout = seen[0]
    assert '--ignore-user-config' in argv and '--ephemeral' in argv and '--output-schema' in argv
    assert 'features.shell_tool=false' in argv and 'features.multi_agent=false' in argv
    assert timeout == 37 and prompt == 'archived input'


@pytest.mark.parametrize('event', [
    {'type':'item.completed','item':{'type':'command_execution'}},
    {'type':'turn.failed','error':{'message':'provider unavailable'}},
])
def test_tool_use_or_failed_turn_never_becomes_a_news_decision(monkeypatch, event):
    monkeypatch.setattr(adapter,'_run',lambda *a:json.dumps(event))
    with pytest.raises((ValueError,RuntimeError)):
        adapter.infer('input',provider='codex',model='gpt-6-astra',timeout=10)


def test_timeout_ends_only_own_process_group_and_does_not_retry_or_expose_prompt(monkeypatch):
    calls, signals = [], []
    class Process:
        pid = 999001
        def communicate(self, *args, **kwargs):
            calls.append(kwargs)
            if len(calls) == 1: raise subprocess.TimeoutExpired('codex',1)
            return '', ''
    created = []
    def start(*args, **kwargs):
        created.append(kwargs)
        assert kwargs['start_new_session']
        assert 'OPENAI_API_KEY' not in kwargs['env']
        return Process()
    monkeypatch.setenv('OPENAI_API_KEY','never-display-this')
    monkeypatch.setattr(adapter.subprocess,'Popen',start)
    monkeypatch.setattr(adapter.os,'killpg',lambda pid,sig:signals.append(pid))
    with pytest.raises(TimeoutError,match='timed out'):
        adapter._run(['codex'],'private input','/tmp',1)
    assert len(created) == 1 and signals == [999001] and len(calls) == 2


def test_news_adapter_does_not_fallback_to_zai_on_subscription_failure(monkeypatch):
    calls = []
    def fail(*args, **kwargs):
        calls.append(kwargs)
        raise TimeoutError('timed out')
    monkeypatch.setattr(adapter,'infer',fail)
    with pytest.raises(TimeoutError):
        DecisionModel('codex:gpt-6-astra','analyze')({'evidence':[]})
    assert len(calls) == 1


@pytest.mark.parametrize('usage,expected', [
    ({'input_tokens':100, 'output_tokens':20, 'cached_input_tokens':80, 'reasoning_output_tokens':12},
     {'prompt_tokens':100, 'completion_tokens':20, 'total_tokens':120,
      'prompt_tokens_details':{'cached_tokens':80}, 'completion_tokens_details':{'reasoning_tokens':12}}),
    ({'input_tokens':100, 'output_tokens':20, 'cached_input_tokens':0},
     {'prompt_tokens':100, 'completion_tokens':20, 'total_tokens':120, 'prompt_tokens_details':{'cached_tokens':0}}),
    ({'input_tokens':100, 'output_tokens':20, 'cached_input_tokens':101},
     {'prompt_tokens':100, 'completion_tokens':20, 'total_tokens':120}),
    ({}, {}),
])
def test_usage_preserves_cache_subsets_and_unknown_is_not_zero(monkeypatch, usage, expected):
    monkeypatch.setattr(adapter, '_run', lambda *a:'\n'.join(map(json.dumps, [
        {'type':'item.completed','item':{'type':'agent_message','text':'{}'}},
        {'type':'turn.completed','usage':usage}])))
    _, metadata = adapter.infer('input', provider='codex', model='gpt-6-astra', timeout=10)
    assert metadata['_usage'] == expected
