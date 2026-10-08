"""Savings must retain unrelated shocks, missing detail and timed/changed reviews."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from tokmo.programs.news_decision_engine import NewsDecisionEngine, RECORDED_TIME
from tokmo.records.news_decision_store import NewsDecisionStore, timestamp, kst
from test_news_decision_engine import observation, decision, screen

AT = timestamp('2026-09-09T10:00:00+09:00')


def event(n):
    row = observation('old-' + str(n), AT-timedelta(minutes=200-n))
    return dict(id=f'event-{n:03}', evidence=[row], updated_at=row['observed_at'],
        decision=decision([row['id']], title=f'사업 {n} 진행 보고', facts='과거 사실의 긴 설명. '*80),
        context_memory=dict(status='MODEL_INTERPRETATION', topic=f'사업 {n}', phase='진행 중',
            summary='과거 해석과 확인된 내용을 구분하는 요약. '*20, watch='성과와 후속 결과 확인', source_ids=[row['id']]))


def test_catalog_keeps_all_candidates_while_only_details_are_retrieved(tmp_path):
    store = NewsDecisionStore(tmp_path/'s.db')
    try:
        engine = NewsDecisionEngine(store, screen, None, clock=RECORDED_TIME)
        events = [event(n) for n in range(100)]
        evidence = [observation('shock', AT)]
        evidence[0]['title'] = '새 국가에서 시작된 전면전: 보유 종목과 무관한 최초 충격'
        before = engine._screen_request(evidence, events)
        engine.context_mode = 'efficient'
        after = engine._screen_request(evidence, events)
        assert {e['event_id'] for e in before['events']} == {e['event_id'] for e in after['events']}
        assert after['evidence'] == before['evidence']
        assert sum(e['detail_included'] for e in after['events']) == 12
        assert len(json.dumps(after)) < .5 * len(json.dumps(before))
        assert after['events_omitted'] == 0
    finally:
        store.close()


@pytest.mark.parametrize('ignored', [False, True])
def test_catalog_only_nomination_restores_full_event_before_final_judgment(tmp_path, ignored):
    store = NewsDecisionStore(tmp_path/'s.db'); seen = []
    for n in range(30): store.save_event(event(n))
    def select(request):
        row = next(e for e in request['events'] if not e['detail_included'])
        item = dict(event_id=row['event_id'], evidence_ids=['a'], comparison_event_ids=[], event_reason='목록의 같은 사건에 부착')
        return dict(candidates=[] if ignored else [item], ignored=[dict(event_id=row['event_id'],
            evidence_id='a', reason='제목이 비슷한 기존 사건')] if ignored else [])
    def analyze(request):
        seen.append(request)
        return decision(['a'], notify=False, change='NONE')
    try:
        result = NewsDecisionEngine(store, select, analyze, clock=RECORDED_TIME, context_mode='efficient').tick([observation('a', AT)], AT)
        assert not result['failures'] and len(seen) == 1
        assert seen[0]['events'][0]['decision']['facts'].startswith('과거 사실')
        assert seen[0]['events'][0]['context_memory']['summary']
        if ignored: assert result['detail_escalations'] == ['a']
    finally: store.close()


def setup_review(tmp_path, *, plan=True, context=None):
    store = NewsDecisionStore(tmp_path/'s.db'); calls = []
    context = context if context is not None else {}
    def analyze(request):
        calls.append(deepcopy(request))
        return decision([r['id'] for r in request['evidence']], notify=False, change='NONE', urgency='NORMAL',
            recheck=dict(after=kst(AT+timedelta(hours=18)), reason='다음 확인 일정 전이며 새 자료 없이 경과를 확정할 수 없음') if plan else None)
    analyze.model = 'recorded-analysis'; analyze.policy_fingerprint = 'policy-1'
    engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME, context_mode='efficient',
        reuse_unchanged_reviews=True, context_reader=lambda at:dict(deepcopy(context), observed_at=kst(at)))
    result = engine.tick([observation('a', AT)], AT)
    assert not result['failures']
    return store, engine, calls, context


def test_reuse_survives_restart_but_does_not_advance_judgment_and_expires(tmp_path):
    store, engine, calls, context = setup_review(tmp_path)
    before = deepcopy(store.events()[0]); store.close()
    store = NewsDecisionStore(tmp_path/'s.db'); engine.store = store
    try:
        for hours in (6, 12):
            result = engine.tick([], AT+timedelta(hours=hours))
            assert result['calls'] == 0 and len(result['reused_reviews']) == 1 and not result['alerts']
            assert store.events()[0]['updated_at'] == before['updated_at']
        result = engine.tick([], AT+timedelta(hours=18))
        assert result['calls'] == 1 and len(calls) == 2 and not result['failures']
        assert calls[-1]['review_reason'] == 'PERIODIC_RECHECK'
    finally: store.close()


@pytest.mark.parametrize('change', ['quantity', 'choice', 'cash', 'stale', 'policy', 'source', 'related', 'delivery'])
def test_new_meaningful_context_and_evidence_invalidate_reuse(tmp_path, change):
    context = dict(holdings=[dict(key='KR:1', name='보유 기업', quantity='1')],
                   cash_status='INVESTED', portfolio_status='FRESH', investment_followup={'choices':[]})
    store, engine, calls, context = setup_review(tmp_path, context=context)
    try:
        if change == 'quantity': context['holdings'][0]['quantity'] = '2'
        elif change == 'choice': context['investment_followup']['choices'].append({'reason':'일부 현금 확보'})
        elif change == 'cash': context['cash_status'] = 'CASH_AVAILABLE'
        elif change == 'stale': context['portfolio_status'] = 'STALE'
        elif change == 'policy': engine.analyze.policy_fingerprint = 'policy-2'
        else:
            old = store.events()[0]
            if change == 'source':
                # Context-only observations still invalidate the last analysis basis.
                old['evidence'].append(observation('observed-only', AT+timedelta(hours=1)))
            elif change == 'related':
                other = event(999); store.save_event(other)
                old['review_context_ids'] = [other['id']]
            else: old['last_delivery'] = 'SENT'
            store.save_event(old)
        result = engine.tick([], AT+timedelta(hours=6))
        assert len(calls) == 2 and result['calls'] == 1 and not result.get('reused_reviews')
        assert not result['failures']
    finally: store.close()


def test_new_candidate_never_waits_for_reuse_deadline(tmp_path):
    store, engine, calls, _ = setup_review(tmp_path)
    try:
        result = engine.tick([observation('new-risk', AT+timedelta(minutes=5))], AT+timedelta(minutes=5))
        assert len(calls) == 2 and not result.get('reused_reviews') and not result['failures']
    finally: store.close()


def test_identical_prior_context_uses_explicit_reference_but_changed_context_is_preserved(tmp_path):
    store, engine, calls, context = setup_review(tmp_path, context={'cash_status':'CASH_AVAILABLE'})
    try:
        engine.tick([observation('b', AT+timedelta(minutes=1))], AT+timedelta(minutes=1))
        assert calls[-1]['events'][0]['context_snapshot']['same_content_as'] == 'context'
        assert store.events()[0]['context_snapshot']['cash_status'] == 'CASH_AVAILABLE'
        context['cash_status'] = 'INVESTED'
        engine.tick([observation('c', AT+timedelta(minutes=2))], AT+timedelta(minutes=2))
        assert calls[-1]['events'][0]['context_snapshot']['cash_status'] == 'CASH_AVAILABLE'
        assert calls[-1]['context']['cash_status'] == 'INVESTED'
    finally: store.close()


@pytest.mark.parametrize('kind', ['morning', 'evening', 'recovery'])
def test_release_and_delivery_recovery_always_reassess(tmp_path, kind):
    store, engine, calls, _ = setup_review(tmp_path)
    try:
        old = store.events()[0]
        if kind == 'recovery': old['recovery_review_at'] = kst(AT+timedelta(hours=6))
        else:
            old['held_until'] = kst(AT+timedelta(hours=6))
            if kind == 'evening': old['held_kind'] = 'EVENING_HEADLINES'
        store.save_event(old)
        result = engine.tick([], AT+timedelta(hours=6))
        assert len(calls) == 2 and not result.get('reused_reviews') and not result['failures']
        assert calls[-1]['review_reason'] == {'morning':'MORNING_RELEASE','evening':'EVENING_RELEASE','recovery':'DELIVERY_RECOVERY'}[kind]
    finally: store.close()


@pytest.mark.parametrize('after', [None, 'bad', '2026-09-08T10:00:00+09:00', '2026-12-01T10:00:00+09:00'])
def test_missing_or_bad_auxiliary_plan_retains_existing_schedule(tmp_path, after):
    store, engine, calls, _ = setup_review(tmp_path, plan=False)
    try:
        from tokmo.programs.news_context_efficiency import review_plan
        assert review_plan({'after':after, 'reason':'대기'}, decision(), AT) is None
        result = engine.tick([], AT+timedelta(hours=6))
        assert len(calls) == 2 and not result['failures']
    finally: store.close()


def test_model_deadline_earlier_than_normal_interval_is_not_delayed(tmp_path):
    store = NewsDecisionStore(tmp_path/'early.db')
    def analyze(request):
        return decision(notify=False, change='NONE', importance='MEDIUM',
            recheck={'after':kst(AT+timedelta(hours=4)), 'reason':'공식 발표 예정 시각'})
    try:
        engine = NewsDecisionEngine(store, screen, analyze, clock=RECORDED_TIME, reuse_unchanged_reviews=True)
        engine.tick([observation('a',AT)],AT)
        assert timestamp(store.events()[0]['next_check_at']) == AT+timedelta(hours=4)
        result = engine.tick([],AT+timedelta(hours=4))
        assert result['calls'] == 1 and not result.get('reused_reviews')
    finally: store.close()


def test_compact_context_trims_etf_membership_and_review_history_idempotently():
    from tokmo.programs.news_context_efficiency import compact_context
    context = dict(
        holdings=[dict(key='KR:000660', name='SK하이닉스', quantity=10)], exposures=[dict(key='KR:000660', weight='0.4')],
        etf_context=[dict(symbol='0162Z0', market='KR', holding_status='CONFIRMED_IN_SNAPSHOT',
            nav=dict(usable_for_price_decision=False, note='괴리율 설명'),
            components=dict(coverage='COMPLETE_DISCLOSURE', note='공시 기준 추정', asset_weights_pct=dict(stock='50.19', bond='46.43'),
                rows=[dict(asset_kind='stock', code='KR7000660001', key='KR:000660', name='SK하이닉스', weight_pct='25.15'),
                      dict(asset_kind='bond', code='KR103501GF30', key='ISIN:KR103501GF30', name='국고채', weight_pct='16.71')]))],
        investment_followup=dict(status='OK', choices=[dict(id='choice-1', keys=['KR:000660'], summary='선택')], delivered_reviews=[
            dict(id='r1', choice_id='choice-1', delivered_at='2026-09-10T10:00:00+09:00', comparison='옛 비교', summary='옛', new_facts=[dict(sources=['s1'], text='옛 사실')]),
            dict(id='r2', choice_id='choice-1', delivered_at='2026-09-17T10:00:00+09:00', comparison='새 비교', summary='새',
                 new_facts=[dict(sources=['s2'], text='새 사실'), dict(sources=['s3'], text='둘째'), dict(sources=['s4'], text='셋째')]),
            dict(id='r3', choice_id='choice-2', delivered_at='2026-09-01T10:00:00+09:00', comparison='다른 선택', summary='다른', new_facts=[])]))
    once = compact_context(context)
    etf = once['etf_context'][0]
    assert etf['components'] == dict(coverage='COMPLETE_DISCLOSURE', asset_weights_pct=dict(stock='50.19', bond='46.43'),
                                     rows=[dict(key='KR:000660', code='KR7000660001', name='SK하이닉스', weight_pct='25.15')], non_stock_rows=1)
    assert etf['nav'] == dict(usable_for_price_decision=False) and etf['symbol'] == '0162Z0'
    assert once['investment_followup']['delivered_reviews'] == [
        dict(id='r3', choice_id='choice-2', delivered_at='2026-09-01T10:00:00+09:00', comparison='다른 선택', summary='다른', new_facts=[]),
        dict(id='r2', choice_id='choice-1', delivered_at='2026-09-17T10:00:00+09:00', comparison='새 비교', summary='새', new_facts=['새 사실', '둘째'])]
    assert once['holdings'] == context['holdings'] and once['exposures'] == context['exposures']
    assert once['investment_followup']['choices'] == context['investment_followup']['choices']
    assert compact_context(once) == once  # stored snapshots are compacted again on read
    assert context['etf_context'][0]['components']['rows'][1]['asset_kind'] == 'bond'  # input untouched
