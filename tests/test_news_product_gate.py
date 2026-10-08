"""Real installed v2 gate + synthetic temporary authority; transport never live."""
from datetime import datetime, timezone
import json
from pathlib import Path
import sys

import pytest

import news_decision_models as models
import news_product_gate as product
from tokmo.programs.model_chain import ModelChain


@pytest.fixture
def gate_world(tmp_path, monkeypatch):
    # These are unit fixtures, never a runtime approval or independent review.
    gate = product.installed_gate()
    paths = {key: tmp_path/key for key in gate.PATHS}
    paths['core'].write_text('SYNTHETIC unit authority')
    paths['parser'].write_bytes(gate.PATHS['parser'].read_bytes())
    routing = {'routes': {'glm-5.3': dict(provider='zai', contract='zai-chat',
               uses=['product'], windows=['5시간', '주간'])}, 'selectors': {}}
    paths['routing'].write_text('## 현재 상태\n```model-routing\n' + json.dumps(routing) + '\n```\n')
    paths['quota'].write_text(json.dumps(dict(schema='model-quota-latest-v1',
        observed_at=datetime.now(timezone.utc).isoformat(), providers={'zai': dict(ok=True,
        windows=[dict(label=label, remaining=90) for label in ['5시간', '주간']])})))
    entry = tmp_path/'synthetic-entry.py'
    entry.write_text('# synthetic pinned entry; never executed\n')
    monkeypatch.setattr(gate, 'PATHS', paths)
    monkeypatch.setattr(product, 'PROJECT_ROOT', tmp_path)
    monkeypatch.setattr(product, 'APPROVAL', tmp_path/'synthetic-approval.json')
    monkeypatch.setattr(product, 'installed_gate', lambda: gate)
    # Exercise the gate's actual-process default, with test-only process facts.
    monkeypatch.setattr(sys, 'argv', [str(entry)])
    monkeypatch.setattr(sys.modules['__main__'], '__file__', str(entry))
    gate.issue(product='tokmo-news', runtime_root=tmp_path, entrypoints=[entry], files=[entry.name],
        models={'glm-5.3': dict(model='glm-5.3', provider='zai')},
        policy_approval='SYNTHETIC UNIT FIXTURE ONLY',
        review=dict(verdict='PASS', candidate_sha256='a'*64, reviewer='SYNTHETIC UNIT FIXTURE'),
        out_path=product.APPROVAL)
    monkeypatch.setenv('ZAI_API_KEY', 'synthetic-key-never-send')
    def denied(*args, **kwargs):
        raise AssertionError('physical transport reached without product admission')
    monkeypatch.setattr(models, 'zai_chat_once', denied)
    monkeypatch.setattr(__import__('subscription_json_model'), 'infer', denied)
    return gate, paths, entry


def adapter(tmp_path, stage='screen'):
    return models.DecisionModel('glm-5.3', stage, diagnostic_path=tmp_path/'screen-last-error.json')


def receipts(tmp_path, kind):
    return [json.loads(p.read_text()) for p in (tmp_path/'admissions').glob('*-'+kind+'.json')]


def chained(model):
    def fallback(request):
        raise AssertionError('admission/evidence error escaped into fallback')
    fallback.model = 'codex:fake'
    return ModelChain([model, fallback], stage='screen', clock=lambda: datetime.now(timezone.utc))


def test_missing_approval_stops_at_adapter_before_transport(tmp_path, monkeypatch, gate_world):
    product.APPROVAL.unlink()
    monkeypatch.delenv('ZAI_API_KEY')
    monkeypatch.delenv('GLM_API_KEY', raising=False)
    def credential_denied(*args):
        raise AssertionError('credential accessed before admission')
    from tokmo.programs import news_classifier_executor
    monkeypatch.setattr(news_classifier_executor, 'read_zai_credential_file', credential_denied)
    with pytest.raises(RuntimeError, match='NEWS_MODEL_ADMISSION_DENIED'):
        adapter(tmp_path)({'evidence': [], 'events': []})
    assert not receipts(tmp_path, 'CALL')


@pytest.mark.parametrize('fault', ['review', 'code', 'core', 'parser', 'quota', 'routing', 'identity', 'synthetic'])
def test_real_v2_denial_never_reaches_transport_or_fallback(tmp_path, monkeypatch, gate_world, fault):
    gate, paths, entry = gate_world
    if fault == 'review':
        data = json.loads(product.APPROVAL.read_text())
        data['review']['verdict'] = 'FAIL'
        product.APPROVAL.write_text(json.dumps(data))
    elif fault == 'code':
        entry.write_text('drift')
    elif fault == 'identity':
        monkeypatch.setattr(sys, 'argv', [str(tmp_path/'wrong.py')])
    elif fault == 'synthetic':
        admit = gate.admit
        monkeypatch.setattr(gate, 'admit', lambda *a, **kw: admit(*a, **kw,
            identity=dict(argv0=str(entry), main_file=str(entry), synthetic=True)))
    else:
        paths[fault].write_text('invalid or changed authority')
    with pytest.raises(product.NewsProductGateError, match='ADMISSION_DENIED'):
        chained(adapter(tmp_path))({})
    assert not receipts(tmp_path, 'CALL')


@pytest.mark.parametrize('stage', ['screen', 'analyze', 'analyze-first'])
def test_each_physical_call_orders_intent_recheck_transport_result(tmp_path, monkeypatch, gate_world, stage):
    gate, _, _ = gate_world
    order = []
    original_write, original_recheck = product.atomic_private, gate.recheck
    def write(path, value):
        order.append(value['status'])
        original_write(path, value)
    def recheck(*args, **kwargs):
        order.append('recheck')
        original_recheck(*args, **kwargs)
    def transport(prompt, **kwargs):
        order.append('transport')
        assert kwargs['model'] == 'glm-5.3' and kwargs['json_mode'] is True
        assert kwargs['timeout_seconds'] == 90 and kwargs['max_tokens'] == 4096
        kwargs['opener'].usage = {'total_tokens': 12}
        return 'not-json'  # Preserve the existing one-correction decoder path.
    monkeypatch.setattr(product, 'atomic_private', write)
    monkeypatch.setattr(gate, 'recheck', recheck)
    monkeypatch.setattr(models, 'zai_chat_once', transport)
    model = (models.build_first_pass_model('glm-5.3', diagnostic_root=tmp_path)
             if stage == 'analyze-first' else adapter(tmp_path, stage))
    for correction in [False, True]:
        result = model(dict(context={'private': 'PRIVATE-CONTENT'}, correction={'reason': 'PRIVATE-REASON'} if correction else {}))
        assert result['invalid_model_reason'] == 'invalid_json' and result['_usage'] == {'total_tokens': 12}
    assert order == ['CALL', 'recheck', 'transport', 'RESULT'] * 2
    calls, results = receipts(tmp_path, 'CALL'), receipts(tmp_path, 'RESULT')
    assert len({c['call_id'] for c in calls}) == 2
    assert {c['call_id'] for c in calls} == {r['call_id'] for r in results}
    assert {c['correction'] for c in calls} == {False, True}
    assert all(c['stage'] == stage and c['admission']['route']['quota']['sha256'] for c in calls)
    assert all(r['outcome'] == 'SUCCEEDED' and r['transport_started'] for r in results)
    raw = json.dumps(calls + results)
    assert all(s not in raw for s in ['PRIVATE-CONTENT', 'PRIVATE-REASON', 'synthetic-key-never-send', 'not-json'])
    assert all(p.stat().st_mode & 0o777 == 0o600 for p in (tmp_path/'admissions').iterdir())


def test_route_change_during_call_receipt_is_rechecked_before_transport(tmp_path, monkeypatch, gate_world):
    _, paths, _ = gate_world
    original = product.atomic_private
    def write(path, value):
        original(path, value)
        if value['status'] == 'CALL':
            paths['routing'].write_text(paths['routing'].read_text() + '\nchanged during intent\n')
    monkeypatch.setattr(product, 'atomic_private', write)
    with pytest.raises(product.NewsProductGateError, match='PREFLIGHT_STALE'):
        chained(adapter(tmp_path))({})
    assert receipts(tmp_path, 'RESULT')[0]['outcome'] == 'NOT_SENT'


@pytest.mark.parametrize('failed_record,provider_error', [('CALL', False), ('RESULT', False), ('RESULT', True)])
def test_receipt_failure_blocks_success_fallback_and_next_worker_call(tmp_path, monkeypatch, gate_world, failed_record, provider_error):
    original = product.atomic_private
    sent = []
    def write(path, value):
        if value['status'] == failed_record:
            raise OSError('PRIVATE-DISK-ERROR')
        original(path, value)
    def transport(*args, **kwargs):
        sent.append(1)
        if provider_error:
            raise TimeoutError('PRIVATE-PROVIDER-ERROR')
        return '{}'
    monkeypatch.setattr(product, 'atomic_private', write)
    monkeypatch.setattr(models, 'zai_chat_once', transport)
    with pytest.raises(product.NewsProductGateError, match=failed_record+'_RECEIPT_FAILED'):
        chained(adapter(tmp_path))({})
    assert len(sent) == (failed_record == 'RESULT')
    if failed_record == 'RESULT':
        monkeypatch.setattr(product, 'atomic_private', original)
        for model_id in ['glm-5.3', 'codex:gpt-5.6-sol']:
            restarted = models.DecisionModel(model_id, 'analyze', transport='hermes',
                                             diagnostic_path=tmp_path/'analyze-last-error.json')
            with pytest.raises(product.NewsProductGateError, match='RESULT_UNRESOLVED'):
                restarted({})
        assert len(sent) == 1 and not receipts(tmp_path, 'RESULT')


def test_provider_failure_is_recorded_and_existing_exception_preserved(tmp_path, monkeypatch, gate_world):
    error = TimeoutError('PRIVATE-PROVIDER-ERROR')
    def transport(*args, **kwargs):
        raise error
    monkeypatch.setattr(models, 'zai_chat_once', transport)
    with pytest.raises(TimeoutError) as caught:
        adapter(tmp_path)({})
    assert caught.value is error
    record = receipts(tmp_path, 'RESULT')[0]
    assert record['outcome'] == 'ERROR' and record['error_type'] == 'TimeoutError'
    assert 'PRIVATE-PROVIDER-ERROR' not in json.dumps(record)
    product.require_complete_receipts(tmp_path/'screen-last-error.json')
