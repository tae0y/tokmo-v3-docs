"""Synthetic repeat requests and denied/fake transports; no live inputs."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import quant_decision_models as models
from datetime import datetime, timezone, timedelta
from copy import deepcopy
from test_quant_decisions import sample_input, response
from tokmo.programs.quant_decision_analysis import make_packet, run_analysis, reusable_analysis
from tokmo.values.quant_decisions import parse_snapshot

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


EXAMPLE = Path(__file__).resolve().parents[3] / 'shared/model_routing/example-routing.md'


@pytest.fixture(autouse=True)
def routing_fixture(monkeypatch, tmp_path):
    route = tmp_path / 'MODEL_ROUTING_STATE.md'
    route.write_bytes(EXAMPLE.read_bytes())
    monkeypatch.setattr(models, 'ROUTING_PATH', route)


def fake_adapter(monkeypatch, tmp_path):
    route = tmp_path/'route.md'
    route.write_text(EXAMPLE.read_text())
    quota = tmp_path/'quota.json'
    quota.write_text(json.dumps(dict(schema='model-quota-latest-v1', observed_at=NOW.isoformat(),
        providers={'zai': {'ok': True, 'windows': [dict(label=k, remaining=1.0) for k in ('5시간','주간')]}})))
    monkeypatch.setattr(models, 'require_product_scope', lambda: {
        'routing_state_sha256':__import__('hashlib').sha256(route.read_bytes()).hexdigest(),
        'resolver_sha256':__import__('hashlib').sha256(models.RESOLVER_PATH.read_bytes()).hexdigest()})
    monkeypatch.setattr(models, 'ROUTING_PATH', route, raising=False)
    monkeypatch.setattr(models, 'QUOTA_PATH', quota, raising=False)
    monkeypatch.setattr(models, 'utc_now', lambda: NOW, raising=False)
    monkeypatch.setattr(models, 'read_zai_credential_file', lambda _: 'fake')
    physical = []
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({'choices':[{'message':{'content':'{}'}}], 'usage':{
                'prompt_tokens':10,'completion_tokens':4,'total_tokens':14,
                'completion_tokens_details':{'reasoning_tokens':2}}}).encode()
    class Transport:
        def open(self, request, **kwargs):
            physical.append(json.loads(request.data))
            return Reply()
    model = models.QuantModel(env_file=tmp_path/'unused', opener=Transport(),
                              on_demand=True, receipt_dir=tmp_path/'receipts')
    return model, physical, quota, route


@pytest.mark.parametrize('bad', ['missing','malformed','stale','future','naive','schema','provider','missing_week',
    'exhausted_week','nan','infinity','boolean','negative','over100','duplicate','route','historic_route'])
def test_every_request_rechecks_exact_route_and_nested_quota(monkeypatch, tmp_path, bad):
    model, physical, quota, route = fake_adapter(monkeypatch, tmp_path)
    touched = []
    request = models.Request
    monkeypatch.setattr(models, 'read_zai_credential_file', lambda _: touched.append('credential') or 'fake')
    monkeypatch.setattr(models, 'Request', lambda *a, **kw: touched.append('request') or request(*a, **kw))
    assert model('synthetic') == '{}'
    assert touched == ['credential', 'request']
    touched.clear()
    q = json.loads(quota.read_text())
    if bad in {'stale','future','naive'}:
        q['observed_at'] = {'stale':(NOW-timedelta(minutes=46)).isoformat(),
            'future':(NOW+timedelta(minutes=6)).isoformat(),'naive':'2026-09-10T00:00:00'}[bad]
    elif bad == 'schema': q['schema'] = 'unknown'
    elif bad == 'provider': q['providers']['zai']['ok'] = False
    elif bad == 'missing_week': q['providers']['zai']['windows'].pop()
    elif bad == 'duplicate': q['providers']['zai']['windows'].append(dict(label='주간',remaining=99))
    elif bad in {'exhausted_week','nan','infinity','boolean','negative','over100'}:
        q['providers']['zai']['windows'][1]['remaining'] = {
            'exhausted_week':0,'nan':float('nan'),'infinity':float('inf'),
            'boolean':True,'negative':-1,'over100':101}[bad]
    quota.write_text(json.dumps(q))
    if bad == 'missing': quota.unlink()
    if bad == 'malformed': quota.write_text('{')
    if bad == 'route': route.write_text(route.read_text().replace('glm-5.3"','synthetic-removed-99"'))
    if bad == 'historic_route': route.write_text(route.read_text().replace('## 현재 상태','## 과거'))
    with pytest.raises(PermissionError, match='^QUANT_ROUTE_OR_QUOTA_BLOCKED$'): model('synthetic rewrite')
    assert len(physical) == 1 and model.calls[-1]['transport_started'] is False
    assert touched == []


def test_receipts_and_bounds_and_validator_two_calls(monkeypatch, tmp_path):
    model, physical, _, _ = fake_adapter(monkeypatch, tmp_path)
    s = parse_snapshot(sample_input()); packet = make_packet(s,s.instruments[0],'personal-a','personal')
    assert run_analysis(packet, model)[0] is None
    assert len(physical) == 2
    assert model.timeout==240
    assert all(p['max_tokens']==16384 and p['reasoning_effort']=='max' and
               p['response_format']=={'type':'json_object'} for p in physical)
    receipts = [json.loads(p.read_text()) for p in (tmp_path/'receipts').glob('*.json')]
    assert len(receipts)==2 and all(r['transport_started'] and r['status']=='RETURNED' for r in receipts)
    assert receipts[-1]['usage']['reasoning_tokens'] == 2
    assert all(r['response']=='{}' and 'prompt' not in r for r in receipts)
    monkeypatch.setattr(models, 'atomic_private', lambda *a: (_ for _ in ()).throw(OSError('disk')))
    with pytest.raises(OSError): model('synthetic')
    assert len(physical) == 2


def test_optional_physical_call_budget_counts_validation_rewrites(monkeypatch,tmp_path):
    model,physical,_,_=fake_adapter(monkeypatch,tmp_path);model.max_calls=1
    s=parse_snapshot(sample_input());packet=make_packet(s,s.instruments[0],'personal-a','personal')
    analysis,errors=run_analysis(packet,model)
    assert analysis is None and len(physical)==1
    assert model.calls[-1]['block_reason']=='QUANT_CALL_BUDGET_EXHAUSTED'
    assert model.calls[-1]['transport_started'] is False


@pytest.mark.parametrize('tokens', [3000, 4096, 5000, 6000])
def test_existing_zai_caller_preserves_base_behavior(monkeypatch, tmp_path, tokens):
    model, physical, _, _ = fake_adapter(monkeypatch, tmp_path)
    model.on_demand, model.max_tokens = False, tokens
    monkeypatch.setattr(models, 'require_product_scope', lambda: pytest.fail('new permission inherited'))
    assert model('synthetic') == '{}'
    assert physical[0]['max_tokens'] == tokens
    assert model.calls[-1]['response'] == '{}'
    assert model.calls[-1]['usage'] == dict(prompt_tokens=10, completion_tokens=4, total_tokens=14)
    def fail(*args, **kwargs): raise TimeoutError('synthetic')
    model.opener.open = fail
    with pytest.raises(TimeoutError): model('synthetic')
    assert model.calls[-1]['status'] == 'FAILED' and model.calls[-1]['error_class'] == 'TimeoutError'
    assert not (tmp_path/'receipts').exists()


def test_existing_codex_caller_preserves_response_usage_and_failure(monkeypatch, tmp_path):
    import subscription_json_model as subscription
    def run(*args):
        return '\n'.join(json.dumps(x) for x in [
            {'type':'item.completed','item':{'type':'agent_message','text':'{}'}},
            {'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':4}}])
    monkeypatch.setattr(subscription, '_run', run)
    monkeypatch.setattr(models, 'require_product_scope', lambda: pytest.fail('new permission inherited'))
    model = models.QuantModel(env_file=tmp_path/'unused', model='codex:gpt-6-astra',
                              max_tokens=6000, reasoning_effort='xhigh')
    assert model('synthetic') == '{}'
    assert model.reuse_contract['provider'] == 'codex'
    assert model.calls[-1]['response'] == '{}' and model.calls[-1]['resolved_model'] == 'gpt-6-astra'
    assert model.calls[-1]['usage'] == dict(prompt_tokens=10, completion_tokens=4, total_tokens=14)
    def fail(*args): raise TimeoutError('synthetic')
    monkeypatch.setattr(subscription, '_run', fail)
    with pytest.raises(TimeoutError): model('synthetic')
    assert model.calls[-1]['status'] == 'FAILED' and model.calls[-1]['error_class'] == 'TimeoutError'


def test_cli_denial_keeps_calculations_reason_and_receipt(tmp_path, capsys):
    from run_quant_decisions import main
    snapshot = tmp_path/'input.json'; snapshot.write_text(json.dumps(sample_input()))
    output = tmp_path/'request'
    assert main(['--snapshot',str(snapshot),'--symbol','KR:000001','--model','glm-5.3',
                 '--output',str(output)]) == 2
    report = next(json.loads(p.read_text()) for p in output.glob('qd-*.json')
                  if 'schema_version' in json.loads(p.read_text()))
    row = report['results'][0]
    assert row['packet']['options'] and row['status']=='FAILED'
    assert 'QUANT_PRODUCT_SCOPE_NOT_APPROVED' in str(row['errors'])
    assert len(list((output/'model-receipts').glob('*.json'))) == 1
    assert 'QUANT_PRODUCT_SCOPE_NOT_APPROVED' in capsys.readouterr().out


@pytest.mark.parametrize('change', ['contract_missing','contract_changed','purpose','scope','fx',
    'cash_freshness','source_status','evidence_source','manual','calculated','failed'])
def test_reuse_requires_contract_identity_and_complete_current_context(change):
    s = parse_snapshot(sample_input()); packet = make_packet(s,s.instruments[0],'personal-a','personal')
    packet.update(analysis_contract={'schema':'quant-analysis-v1','model':'synthetic'},purpose='ordinary')
    old = dict(status='ANALYZED',analysis_source='model',packet=deepcopy(packet),analysis=response(packet))
    assert reusable_analysis(packet,old)
    if change=='contract_missing': old['packet'].pop('analysis_contract')
    elif change=='contract_changed': packet['analysis_contract']['model']='changed'
    elif change=='purpose': packet['purpose']='tax planning'
    elif change=='scope': packet['scope']='retirement'
    elif change=='fx': packet['fx']['rate']='1500'
    elif change=='cash_freshness': packet['as_of']=(s.as_of+timedelta(minutes=31)).isoformat()
    elif change=='source_status': packet['holdings'][0]['source_status']='UNAVAILABLE'
    elif change=='evidence_source': packet['evidence'][0]['source']='changed source'
    elif change=='manual': old['analysis_source']='manual'
    else: old['status']=change.upper()
    assert reusable_analysis(packet,old) is None


def test_cli_reuses_success_across_request_directories_with_current_calculations(tmp_path, capsys):
    from run_quant_decisions import main
    data = sample_input()
    data['instruments'][0]['evidence'][0]['metadata'] = {'time_basis':'current_document_read'}
    snapshot = tmp_path/'input.json'; responses = tmp_path/'responses.json'
    responses.write_text(json.dumps({'personal-a:KR:000001':response({})}))
    root = tmp_path/'private/quant-requests'
    def invoke(folder):
        snapshot.write_text(json.dumps(data))
        assert main(['--snapshot',str(snapshot),'--data-root',str(tmp_path),'--symbol','KR:000001',
                     '--model','recorded','--responses',str(responses),'--output',str(root/folder)]) == 0
        path = next(p for p in (root/folder).glob('qd-*.json') if 'schema_version' in json.loads(p.read_text()))
        return path,json.loads(path.read_text())
    first_path, first = invoke('first')
    original = first_path.read_bytes()
    assert first['results'][0]['packet'].get('analysis_contract')
    later = (datetime.fromisoformat(data['as_of'])+timedelta(minutes=1)).isoformat()
    data['as_of'] = later
    for i in data['instruments']: i['price_at'] = later
    for c in data['cash']: c['observed_at'] = later
    for h in data['positions']: h['observed_at'] = later
    data['fx']['observed_at'] = later
    data['instruments'][0]['evidence'][0]['available_at'] = later
    responses.write_text('{}')
    _, warm = invoke('second')
    row = warm['results'][0]
    assert row['status']=='ANALYZED' and row['analysis_source']=='reused'
    assert row['packet']['cash_evidence'][0]['observed_at']==later
    assert row['analysis_origin']['report_id']==first['report_id']
    assert row['analysis_origin']['path']==str(first_path.resolve())
    assert first_path.read_bytes()==original and not list(root.rglob('delivery-state.json'))
    assert '새로운 판단 근거 없음' in capsys.readouterr().out
    first_path.unlink()
    data['as_of']=(datetime.fromisoformat(later)+timedelta(minutes=1)).isoformat()
    _, missing = invoke('third')
    assert missing['results'][0]['status']=='CALCULATED'


def test_snapshot_outside_request_root_does_not_scan_live_history(tmp_path, monkeypatch):
    from run_quant_decisions import main, DecisionReportStore
    seen = []
    monkeypatch.setattr(DecisionReportStore,'previous_successes',
                        lambda self,**kwargs: seen.append(self.root) or {'results':[]})
    snapshot=tmp_path/'input.json'; snapshot.write_text(json.dumps(sample_input()))
    responses=tmp_path/'responses.json'; responses.write_text('{}')
    main(['--snapshot',str(snapshot),'--model','recorded','--responses',str(responses),
          '--output',str(tmp_path/'output'),'--new-limit','0'])
    assert seen == [tmp_path/'output']


@pytest.mark.parametrize('change',['same','cash','fx','price','quantity','sellable','orders','extra',
    'news_revision','playbook','choices','purpose','model','prompt','validator'])
def test_repeated_model_decision_recalculates_and_invalidates(monkeypatch, change):
    from tokmo.programs import quant_decision_analysis as analysis
    calls=[]
    def model(prompt): calls.append(prompt); return response({})
    model.reuse_contract={'provider':'synthetic','model':'synthetic'}
    data=sample_input(); first=analysis.build_report(parse_snapshot(data),model=model,new_limit=0)
    data['as_of']=(datetime.fromisoformat(data['as_of'])+timedelta(minutes=1)).isoformat()
    kwargs={}
    if change=='cash': data['cash'][0]['available']='1000000'
    elif change=='fx': data['fx']['rate']='1500'
    elif change=='price': data['instruments'][0]['price']='12000'
    elif change=='quantity': data['positions'][0].update(quantity='120',sellable='120')
    elif change=='sellable': data['positions'][0]['sellable']='90'
    elif change=='orders': data['orders']=[dict(account='personal-a',key='KR:000001',kind='unavailable')]
    elif change=='extra': data['extra_capital']=[dict(account='personal-a',scope='personal',currency='KRW',amount='1')]
    elif change=='news_revision': data['instruments'][0]['evidence'][0]['metadata']={'revision_no':2}
    elif change=='playbook': data['instruments'][0]['evidence'][0]['text']='사업 근거가 변경된 합성 자료'
    elif change=='choices': first['results'][0]['packet']['current_user_choices']=[{'id':'old-choice'}]
    elif change=='purpose': kwargs['purpose']='different question'
    elif change=='model': model.reuse_contract={'provider':'synthetic','model':'changed'}
    elif change=='prompt': monkeypatch.setattr(analysis,'MODEL_INSTRUCTION',analysis.MODEL_INSTRUCTION+' changed')
    elif change=='validator': first['results'][0]['packet']['analysis_contract']['validator_sha256']='unknown'
    second=analysis.build_report(parse_snapshot(data),model=model,new_limit=0,previous=first,**kwargs)
    row=second['results'][-1] if change=='extra' else second['results'][0]
    if change=='extra':
        assert second['results'][0]['analysis_source']=='reused' and row['scenario']=='extra'
    assert row['analysis_source']==('reused' if change=='same' else 'model')
    assert len(calls)==(1 if change=='same' else 2)
    if change in {'cash','fx','price','quantity','sellable','extra'}:
        assert row['packet']['options'] != first['results'][0]['packet']['options']


def test_receipt_before_transport_is_an_unknown_start_intent(monkeypatch,tmp_path):
    model,physical,_,_=fake_adapter(monkeypatch,tmp_path)
    delegate=model.opener.open
    def open_request(*args,**kwargs):
        saved=json.loads(next((tmp_path/'receipts').glob('*.json')).read_text())
        assert saved['status']=='STARTING' and saved['transport_started'] is None
        assert saved['provider_started']=='UNKNOWN'
        return delegate(*args,**kwargs)
    model.opener.open=open_request
    model('synthetic')
    assert len(physical)==1


def test_receipt_binds_canonical_authority_paths_and_file_versions(monkeypatch,tmp_path):
    model,_,quota,route=fake_adapter(monkeypatch,tmp_path)
    model('synthetic')
    authority=model.calls[0]['authority']
    assert authority.get('routing_state_path')==str(route.resolve())
    assert authority['quota_snapshot_path']==str(quota.resolve())
    assert authority['routing_state_mtime_ns']==route.stat().st_mtime_ns
    assert authority['quota_snapshot_mtime_ns']==quota.stat().st_mtime_ns
    assert authority['route_admission']=='ADMITTED'


def test_route_parser_receipt_hashes_the_bytes_actually_executed(monkeypatch, tmp_path):
    from hashlib import sha256
    fake_adapter(monkeypatch, tmp_path)
    parser = tmp_path/'resolver.py'
    original = models.RESOLVER_PATH.read_bytes() + b"\nfrom pathlib import Path\nPath(__file__).write_text('changed after read')\n"
    parser.write_bytes(original)
    monkeypatch.setattr(models, 'RESOLVER_PATH', parser)
    facts = models.route_authority('glm-5.3')
    assert parser.read_bytes() != original
    assert facts['route_parser_sha256'] == sha256(original).hexdigest()


def test_reanalysis_explains_changed_account_and_unchanged_instrument_evidence():
    from tokmo.programs.quant_decision_analysis import build_report, render_report
    def model(prompt): return response({})
    model.reuse_contract={'provider':'synthetic','model':'synthetic'}
    data=sample_input(); first=build_report(parse_snapshot(data),model=model,new_limit=0)
    data['cash'][0]['available']='1000000'
    second=build_report(parse_snapshot(data),model=model,new_limit=0,previous=first)
    row=second['results'][0]
    assert row['analysis_source']=='model'
    comparison=row.get('previous_analysis')
    assert comparison is not None
    assert 'cash_evidence' in comparison['changed_fields'] and 'features' in comparison['same_fields']
    assert comparison['as_of']==first['as_of'] and comparison['report_id']==first['report_id']
    text=render_report(second)
    assert '이전 분석 대비 변경:' in text and '가용 자금' in text and '동일: 기술지표' in text


def test_unapproved_product_scope_blocks_before_credentials_or_transport(monkeypatch, tmp_path):
    touched = []
    monkeypatch.setattr(models, 'read_zai_credential_file', lambda _: touched.append('credential') or 'fake')
    class Transport:
        def open(self, *args, **kwargs):
            touched.append('transport')
            raise AssertionError('unexpected transport')
    model = models.QuantModel(env_file=tmp_path/'unused', opener=Transport(), on_demand=True)
    with pytest.raises(PermissionError, match='QUANT_PRODUCT_SCOPE_NOT_APPROVED'):
        model('synthetic')
    assert touched == []
    assert model.calls[0]['status'] == 'BLOCKED'
    assert model.calls[0]['transport_started'] is False


def installed_adapter(monkeypatch, tmp_path):
    """Synthetic installed files/authority and process identity; no live manifest."""
    from hashlib import sha256
    import importlib
    original_gate = models.require_product_scope
    source = Path(__file__).resolve().parents[1]
    model, physical, quota, route = fake_adapter(monkeypatch, tmp_path)
    root = (tmp_path/'installed').resolve(); root.mkdir(mode=0o700)
    files = {}
    for name in models.PRODUCT_FILES:
        path = root/name; path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes((source/name).read_bytes())
        files[name] = sha256(path.read_bytes()).hexdigest()
        if name != 'tools/run_quant_decisions.py' or 'run_quant_decisions' in sys.modules:
            module = importlib.import_module(Path(name).stem if name.startswith('tools/') else name[4:-3].replace('/', '.'))
            monkeypatch.setattr(module, '__file__', str(path))
    core = tmp_path/'core.md'; core.write_text('synthetic exact owner-approved core bytes\n')
    manifest = dict(schema='quant-on-demand-install-v2', runtime_root=str(root),
        entrypoint=str(root/'tools/run_quant_decisions.py'), files=files,
        review={'verdict':'PASS', 'candidate_sha256':'1'*64}, policy_approval='synthetic-owner-decision',
        authority={str(core.resolve()):sha256(core.read_bytes()).hexdigest(), str(route.resolve()):'current'},
        resolver_sha256=sha256(models.RESOLVER_PATH.read_bytes()).hexdigest(),
        scope=deepcopy(models.PRODUCT_SCOPE))
    approval = root/'quant-on-demand-approval.json'
    approval.write_text(json.dumps(manifest)); approval.chmod(0o600)
    monkeypatch.setattr(models, 'RUNTIME_ROOT', root, raising=False)
    monkeypatch.setattr(models, 'CORE_PATH', core.resolve(), raising=False)
    monkeypatch.setattr(models, 'require_product_scope', original_gate)
    monkeypatch.setattr(sys, 'argv', [manifest['entrypoint']])
    monkeypatch.setattr(sys.modules['__main__'], '__file__', manifest['entrypoint'])
    return model, physical, approval, manifest, core, route


@pytest.mark.parametrize('bad', ['missing','malformed','mode','root_mode','unapproved','policy',
    'scope','root','entrypoint','alternate_script','copied_module','missing_hash','extra_hash',
    'file_drift','core_drift','manifest_symlink','file_symlink','root_symlink',
    'owner','source_mode','argv','authority_symlink'])
def test_installed_binding_rechecks_before_credentials_and_request(monkeypatch, tmp_path, bad):
    from hashlib import sha256
    model, physical, approval, manifest, core, route = installed_adapter(monkeypatch, tmp_path)
    touched = []
    request = models.Request
    monkeypatch.setattr(models, 'read_zai_credential_file', lambda _: touched.append('credential') or 'fake')
    monkeypatch.setattr(models, 'Request', lambda *a, **kw: touched.append('request') or request(*a, **kw))
    assert model('synthetic') == '{}' and len(physical) == 1
    assert touched == ['credential', 'request']; touched.clear()
    receipt = json.loads(next((tmp_path/'receipts').glob('*.json')).read_text())
    binding = receipt['authority']['product_scope']
    assert binding['manifest_sha256'] == sha256(approval.read_bytes()).hexdigest()
    assert binding['core_sha256'] == sha256(core.read_bytes()).hexdigest()
    assert binding['routing_state_sha256'] == sha256(route.read_bytes()).hexdigest()
    if bad == 'missing': approval.unlink()
    elif bad == 'malformed': approval.write_text('{')
    elif bad == 'mode': approval.chmod(0o644)
    elif bad == 'root_mode': approval.parent.chmod(0o777)
    elif bad == 'owner': monkeypatch.setattr(models.os, 'getuid', lambda: approval.stat().st_uid+1)
    elif bad == 'source_mode': (approval.parent/'tools/run_quant_decisions.py').chmod(0o666)
    elif bad == 'argv': monkeypatch.setattr(sys, 'argv', [str(tmp_path/'run_quant_decisions.py')])
    elif bad == 'unapproved': manifest['review']['verdict'] = 'CONDITIONAL_PASS'
    elif bad == 'policy': manifest['policy_approval'] = ''
    elif bad == 'scope': manifest['scope']['max_tokens'] = 6000
    elif bad == 'root': manifest['runtime_root'] = str(tmp_path)
    elif bad == 'entrypoint': manifest['entrypoint'] = str(tmp_path/'run_quant_decisions.py')
    elif bad == 'alternate_script': monkeypatch.setattr(sys.modules['__main__'], '__file__', str(tmp_path/'other.py'))
    elif bad == 'copied_module': monkeypatch.setattr(models, '__file__', str(tmp_path/'quant_decision_models.py'))
    elif bad == 'missing_hash': manifest['files'].pop('src/tokmo/programs/quant_decision_analysis.py')
    elif bad == 'extra_hash': manifest['files']['../arbitrary.py'] = '0'*64
    elif bad == 'file_drift': (approval.parent/'tools/run_quant_decisions.py').write_text('changed')
    elif bad == 'core_drift': core.write_text('changed core')
    elif bad == 'route_drift': route.write_text(route.read_text()+'\nchanged authority')
    elif bad == 'authority_symlink':
        outside = tmp_path/'outside'; outside.write_bytes(core.read_bytes())
        core.unlink(); core.symlink_to(outside)
    elif bad in {'manifest_symlink','file_symlink'}:
        target = approval if bad == 'manifest_symlink' else approval.parent/'tools/run_quant_decisions.py'
        outside = tmp_path/'outside'; outside.write_bytes(target.read_bytes()); outside.chmod(0o600)
        target.unlink(); target.symlink_to(outside)
    elif bad == 'root_symlink':
        root = approval.parent; moved = tmp_path/'moved'; root.rename(moved); root.symlink_to(moved)
    if bad in {'unapproved','policy','scope','root','entrypoint','missing_hash','extra_hash'}:
        approval.write_text(json.dumps(manifest))
    with pytest.raises(PermissionError, match='^QUANT_PRODUCT_SCOPE_NOT_APPROVED$'): model('synthetic rewrite')
    assert touched == [] and len(physical) == 1
    assert model.calls[-1]['status'] == 'BLOCKED' and model.calls[-1]['transport_started'] is False


def test_empty_scope_result_cannot_open_product_transport(monkeypatch, tmp_path):
    model, physical, _, _ = fake_adapter(monkeypatch, tmp_path)
    monkeypatch.setattr(models, 'require_product_scope', lambda: None)
    with pytest.raises(PermissionError, match='^QUANT_PRODUCT_SCOPE_NOT_APPROVED$'): model('synthetic')
    assert physical == []


def test_installed_snapshot_cli_uses_binding_and_shared_validator(monkeypatch, tmp_path, capsys):
    import runpy
    model, physical, approval, manifest, _, _ = installed_adapter(monkeypatch, tmp_path)
    # Snapshot CLI need not import the local-store-only context module.
    monkeypatch.delitem(sys.modules, 'tokmo.programs.quant_decision_context')
    class Reply:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def read(self, limit):
            return json.dumps({'choices':[{'message':{'content':json.dumps(response({}))}}]}).encode()
    def transport(request, **kwargs): physical.append(request); return Reply()
    model.opener.open = transport
    monkeypatch.setattr(models, 'build_opener', lambda *args: model.opener)
    snapshot = tmp_path/'input.json'; snapshot.write_text(json.dumps(sample_input()))
    output = tmp_path/'request'
    monkeypatch.setattr(sys, 'argv', [manifest['entrypoint'], '--snapshot', str(snapshot),
        '--symbol', 'KR:000001', '--model', 'glm-5.3', '--output', str(output)])
    with pytest.raises(SystemExit) as result: runpy.run_path(manifest['entrypoint'], run_name='__main__')
    assert result.value.code == 0 and len(physical) == 1
    saved = [json.loads(p.read_text()) for p in (output/'model-receipts').glob('*.json')]
    assert saved[0]['status'] == 'RETURNED' and saved[0]['authority']['product_scope']['manifest_path'] == str(approval)


def test_installed_quant_upgrade_only_routing_bytes(monkeypatch, tmp_path):
    from hashlib import sha256
    model, physical, approval, _, _, route = installed_adapter(monkeypatch, tmp_path)
    before = approval.read_bytes()
    original = (Path(__file__).resolve().parents[3] / 'shared/model_routing/example-routing.md').read_text()
    for selected in ('glm-5.3', 'synthetic-glm-99'):
        route.write_text(original.replace('glm-5.3"', selected+'"') + '\nUnrelated prose.\n')
        fresh = models.QuantModel(env_file=tmp_path/'unused', model='glm', opener=model.opener,
            on_demand=True, receipt_dir=tmp_path/'receipts')
        assert fresh('synthetic') == '{}'
        assert physical[-1]['model'] == selected
        assert fresh.calls[-1]['authority']['routing_state_sha256'] == sha256(route.read_bytes()).hexdigest()
        assert models.canonical_model('glm') == selected
        assert selected in models.model_profiles()
        assert models.canonical_model('fable') == 'claude:claude-fable-5-1'
        assert approval.read_bytes() == before
    with pytest.raises(PermissionError): model('old exact pin')
    assert len(physical) == 2


@pytest.mark.parametrize('changed', ['routing', 'quota', 'code'])
def test_quant_rechecks_after_intent_before_transport(monkeypatch, tmp_path, changed):
    model, physical, approval, _, _, route = installed_adapter(monkeypatch, tmp_path)
    write = models.atomic_private
    def mutate(path, record):
        write(path, record)
        if record['status'] != 'STARTING': return
        if changed == 'routing': route.write_text(route.read_text()+'\nchanged\n')
        elif changed == 'quota': models.QUOTA_PATH.write_text(models.QUOTA_PATH.read_text()+'\n')
        else: (approval.parent/'tools/run_quant_decisions.py').write_text('changed')
    monkeypatch.setattr(models, 'atomic_private', mutate)
    with pytest.raises(PermissionError): model('synthetic')
    assert physical == [] and model.calls[-1]['transport_started'] is False


def test_periodic_exact_setting_does_not_acquire_on_demand_scope(monkeypatch, tmp_path):
    from run_quant_decisions import main
    snapshot = tmp_path/'input.json'; snapshot.write_text(json.dumps(sample_input()))
    monkeypatch.setattr(models, 'canonical_model', lambda name: pytest.fail('periodic route entered product resolver'))
    def legacy(**kwargs):
        assert kwargs['on_demand'] is False and kwargs['model'] == 'codex:gpt-6-astra'
        raise ValueError('legacy adapter reached')
    monkeypatch.setattr(models, 'QuantModel', legacy)
    with pytest.raises(ValueError, match='legacy adapter reached'):
        main(['--periodic', '--snapshot', str(snapshot), '--model', 'codex:gpt-6-astra', '--output', str(tmp_path/'out')])
