from copy import deepcopy
from datetime import timedelta
from pathlib import Path
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'tools'))

import pytest
import run_news_decision_worker as worker
from news_decision_models import decode_response,screen_references,build_models
from tokmo.programs.news_document_quality import source_amounts,validate_document_amounts
from tokmo.records.news_decision_store import timestamp
from test_news_decision_engine import decision

T=timestamp('2026-09-14T20:00:00+09:00')


def test_json_string_answer_preserves_exact_classification_and_sibling_fields():
    value={'candidates':[],'ignored':[{'evidence_id':'news:abc:1','reason':'반복'}]}
    assert decode_response(json.dumps({'answer':json.dumps(value)}))==value
    assert decode_response(json.dumps({'answer':json.dumps({'candidates':[]}), 'ignored':value['ignored']}))==value
    assert decode_response(json.dumps({'answer':{'answer':json.dumps(value)}}))==value
    conflict={'answer':json.dumps(value),'ignored':[]}
    assert decode_response(json.dumps(conflict))==conflict
    assert decode_response(json.dumps({'answer':json.dumps(value)}),finish_reason='length')['invalid_model_reason']=='output_truncated'
    assert decode_response('{"answer":"broken"}')=={'answer':'broken'}


def test_screen_references_are_request_local_and_unknown_ids_stay_invalid():
    req={'evidence':[{'id':'dart:123:first:aaa','text':'company body'},{'id':'news:xyz:1','text':'another'}],
         'correction':{'allowed_evidence_ids':['dart:123:first:aaa','news:xyz:1']}}
    before=deepcopy(req);wire,restore=screen_references(req)
    assert req==before and [e['id'] for e in wire['evidence']]==['INPUT_1','INPUT_2']
    assert wire['correction']['allowed_evidence_ids']==['INPUT_1','INPUT_2']
    response=restore({'candidates':[{'evidence_ids':['INPUT_1']}],
                      'ignored':[{'evidence_id':'INPUT_2','reason':'repeat'},{'evidence_id':'INPUT_99','reason':'invented'}]})
    assert response['candidates'][0]['evidence_ids']==['dart:123:first:aaa']
    assert response['ignored'][0]['evidence_id']=='news:xyz:1'
    assert response['ignored'][1]['evidence_id']=='INPUT_99'


def test_news_cli_failure_is_private_classified_and_not_retried(tmp_path,monkeypatch):
    import subscription_json_model as adapter
    import stat
    calls=[]
    class Process:
        returncode=1
        def communicate(self,*args,**kwargs):
            return json.dumps({'type':'turn.failed','error':{'message':'stream disconnected private-input'}}),''
    def start(*args,**kwargs):calls.append(1);return Process()
    monkeypatch.setattr(adapter.subprocess,'Popen',start)
    import news_subscription_model
    monkeypatch.setattr(news_subscription_model,'run_news',lambda argv,prompt,cwd,timeout,**kw: adapter._run(argv,prompt,cwd,timeout,**kw))
    _,model=build_models(analysis_model='codex:gpt-6-astra',diagnostic_root=tmp_path)
    with pytest.raises(adapter.SubscriptionInferenceError) as raised:model({'evidence':[]})
    assert len(calls)==1 and raised.value.category=='TRANSIENT'
    assert 'private-input' not in str(raised.value)
    saved=tmp_path/'analyze-last-error.json'
    assert 'private-input' in saved.read_text() and stat.S_IMODE(saved.stat().st_mode)==0o600
    assert '통신' in worker.explain_error(str(raised.value))


@pytest.mark.parametrize('fixture,amount',[(1,'3,000,000,000'),(2,'5,000,000,000'),(3,'99,999,806,500')])
def test_actual_flattened_disclosures_keep_monetary_header_units(fixture,amount):
    text=(Path(__file__).parent/f'fixtures/news_document_amount_table_{fixture}.txt').read_text()
    evidence=[dict(id='doc',text=text,content_scope='original_document',body_verified=True)]
    d=decision(['doc'],facts=f'공시 금액 {amount}원.')
    validate_document_amounts(d,evidence)
    with pytest.raises(ValueError,match='ungrounded document amount'):
        validate_document_amounts(dict(d,facts='공시 금액 123,456,789원.'),evidence)


def test_mixed_units_and_following_sections_do_not_authorize_money():
    text='3. 취득\n(단위 : 원, 주, %)\n수량\n금액\n100\n900\n4. 별도\n12345\n'
    assert source_amounts(text)==[]
    text='3. 금액\n(단위 : 백만원)\n계약금액\n5,000\n4. 수량\n70,000\n'
    rows=source_amounts(text)
    assert any(r['value']=='5000000000' for r in rows)
    assert not any(r['value'] in ('70000','70000000000') for r in rows)
    mixed='금액(원)\n취득금액(원)\n수량\n100\n900\n'
    assert source_amounts(mixed)==[]
    assert source_amounts('(단위 : 원)\n수량\n100\n금액\n900\n')==[]


def report(*,tick=False):
    health={'input_errors':[dict(error='QUOTA_EXCEEDED',count=83,ids=['old'],first_observed_at='2026-09-10T18:55:00+09:00',last_observed_at='2026-09-10T20:42:00+09:00')],
            'review_errors':[],'unavailable_sources':[],'tick_failures':[]}
    if tick:health['tick_failures']=[dict(stage='urgent_queue',kind='OSError',count=1,detail='OSError')]
    return dict(status='DEGRADED',health=health,ticks=[dict(calls=2,failures=[])])


def test_disappearing_and_returning_known_symptom_does_not_reprint_backlog():
    state,notice=worker.health_notice(report(tick=True),None,T)
    assert '자료 수집 09-10' in notice and '당시 모델 사용 한도' in notice
    state,notice=worker.health_notice(report(),state,T+timedelta(minutes=1))
    assert notice is None
    state,notice=worker.health_notice(report(tick=True),state,T+timedelta(minutes=2))
    assert notice is None
    new=report();new['health']['input_errors'].append(dict(error='new unique failure',count=1,ids=['new']))
    state,notice=worker.health_notice(new,state,T+timedelta(minutes=3))
    assert 'new unique failure' in notice and 'QUOTA_EXCEEDED' not in notice
    assert '입력 84건' in notice and '이번 실행: 새 처리 오류 없음' in notice
    _,notice=worker.health_notice(dict(status='OK',health={}),state,T+timedelta(minutes=4))
    assert '복구' in notice
