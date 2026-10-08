"""Validated cross-event sources must survive presentation and digest archives."""
from datetime import timedelta

import pytest

from tokmo.programs.news_decision_engine import NewsDecisionEngine, RECORDED_TIME
from tokmo.programs.news_decision_outbox import queue_proposals
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from test_news_decision_engine import observation, decision
from test_news_context_memory import memory


@pytest.mark.parametrize('delivery', ['immediate', 'evening', 'morning'])
def test_restored_related_citation_reaches_alert_and_archive_without_merging_events(tmp_path, delivery):
    at = timestamp('2026-09-16T17:00:00+09:00')
    store = NewsDecisionStore(tmp_path/'state.db')
    seen = []
    def screening(q):
        return dict(candidates=[dict(evidence_ids=[q['evidence'][0]['id']],event_id=None, event_reason='별개 사건의 원문 비교',
                    comparison_event_ids=[q['events'][0]['event_id']] if q['events'] else [])],ignored=[])
    def analyze(q):
        seen.append(q)
        ids = {r['id'] for r in q['evidence']}
        if ids == {'a'}:
            return decision(['a'],notify=False,open=False,context_update=memory(['a']))
        note = '' if delivery=='evening' else '보유 이유에 영향을 주는 공시'
        if 'a' not in ids:
            return decision(['b'],source_requests=['a'],urgency='NORMAL',portfolio_note=note)
        return decision(['b','a'],source_requests=[],urgency='NORMAL',portfolio_note=note,
                        context_update=memory(['b','a']))
    hour = 18 if delivery=='evening' else None
    engine = NewsDecisionEngine(store,screening,analyze,clock=RECORDED_TIME,headline_digest_hour=hour)
    original = dict(observation('a',at),text='원문 전문 '*300, publisher='관련 공시 원문')
    assert not engine.tick([original],at)['failures']
    stored_original=store.observation('a')
    first_id = store.events()[0]['id']
    next_at = at+timedelta(hours=5) if delivery=='morning' else at+timedelta(minutes=1)
    result = engine.tick([observation('b',next_at)],next_at)
    assert not result['failures']
    assert len(seen)==3 and seen[-1]['correction']['restored_source_ids']==['a']
    if delivery!='immediate':
        assert not result['alerts']
        store.close();store=NewsDecisionStore(tmp_path/'state.db')
        engine=NewsDecisionEngine(store,screening,analyze,clock=RECORDED_TIME,headline_digest_hour=hour)
        release=timestamp('2026-09-16T18:00:00+09:00') if delivery=='evening' else timestamp('2026-09-17T08:00:00+09:00')
        result=engine.tick([],release)
        assert not result['failures']
    else:
        release=next_at
    assert len(result['alerts'])==1
    alert=result['alerts'][0]
    assert len(store.events())==2
    source=next(e for e in store.events() if e['id']==first_id)
    target=next(e for e in store.events() if e['id']!=first_id)
    assert [r['id'] for r in source['evidence']]==['a']
    assert [r['id'] for r in target['evidence']]==['b']
    assert target['review_error'] is None
    if delivery=='evening':
        assert alert['kind']=='EVENING_DIGEST'
        details=next(iter(target['pending_digest_details'].values()))
        assert {r['id'] for r in details['evidence']}=={'a','b'}
        assert next(r for r in details['evidence'] if r['id']=='a')==stored_original
        ops=OpsKernelStore(tmp_path/'ops.db',stable_phase_ids=frozenset())
        try:
            queued=queue_proposals(engine,ops,ProducerRole.HERMES,cutoff=at,activation_enabled=True)
            assert queued['queued']==1
            saved=next(e for e in store.events() if e['id']==target['id'])
            archive=saved['queued_digest_entries'][alert['id']]
            assert {r['id'] for r in archive['evidence']}=={'a','b'}
        finally:ops.close()
    else:
        assert 'https://example.invalid/a' in alert['body']
        assert 'https://example.invalid/b' in alert['body']
    store.close()


def test_related_source_not_shown_to_model_still_fails_validation(tmp_path):
    at=timestamp('2026-09-16T10:00:00+09:00')
    store=NewsDecisionStore(tmp_path/'state.db')
    engine=NewsDecisionEngine(store,lambda q:dict(candidates=[dict(evidence_ids=['b'],event_id=None)],ignored=[]),
                             lambda q:decision(['b','not-provided']),clock=RECORDED_TIME)
    result=engine.tick([observation('b',at)],at)
    assert result['failures'] and not result['alerts']
    assert result['calls']==3  # screen, analysis, one existing contract correction
    store.close()
