"""Typed conversation price checkpoint on the existing follow-up store."""
from dataclasses import replace
from datetime import timedelta
from operator import lt, le, gt, ge
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from tokmo.records.investment_followup import validated_checkpoint
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.programs.investment_followup import statement, review_choice
from tokmo.values.quant_decisions import instant, number
from tokmo.values.session_gate import MARKET_TIMEZONE, Market
from tokmo.values.followup_failures import failed_attempt, retry_due, retry_attempt, failure_notice

QUOTE_MAX_AGE=timedelta(minutes=10)
# Saved regular-session packs: maximum provider/local lead 4.519861s; <0.5s margin.
PROVIDER_CLOCK_SKEW=timedelta(seconds=5)


def register_checkpoint(store, supplied, *, source_id, at, references=()):
    """Operator supplies user-approved JSON; no AI extraction or registration."""
    if not isinstance(supplied,dict) or set(supplied)!={'raw_text','interpretation','checkpoint'}:
        raise ValueError('승인 원문, 선택 해석, 체크포인트 JSON이 필요합니다')
    text=statement(supplied['raw_text'],limit=16000)
    value=supplied['interpretation']
    if not isinstance(value,dict):raise ValueError('선택 해석 객체가 필요합니다')
    if value.get('action') not in {'HOLD','TRIM','EXIT','BUY','ADD','CASH','WATCH'} or value.get('phase')!='DECIDED':
        raise ValueError('승인된 선택만 등록합니다')
    if value.get('scope') not in {'personal','retirement'}:raise ValueError('계좌 범위가 필요합니다')
    for key in ('account','summary'):statement(value.get(key))
    for key in ('reason_quotes','condition_quotes','suggested_checks'):
        if not isinstance(value.get(key),list):raise ValueError('선택 근거 배열이 필요합니다')
        for quote in value[key]:
            if statement(quote) not in text:raise ValueError('승인 원문에 없는 근거입니다')
    if not value['reason_quotes'] or value.get('supersedes'):
        raise ValueError('원래 이유가 있는 독립 체크포인트만 등록합니다')
    cp=validated_checkpoint(supplied['checkpoint'])
    return store.record_choice(raw_text=supplied['raw_text'],interpretation=value,source_id=source_id,
                               at=at,references=references,checkpoint=cp)


def read_quote(root, cp, *, at):
    """No-create quote read; only the market's cached regular interval counts."""
    root=Path(root); at=instant(at)
    try:
        market=Market(cp['key'].split(':')[0]); local_tz=ZoneInfo(MARKET_TIMEZONE[market])
        cache=CalendarCacheStore(root/'ops/calendar_cache.json',clock=lambda:at).read()
        day=(cache.kr if market is Market.KR else cache.us) if cache else None
        if day is None:return 'CALENDAR_UNAVAILABLE',None
        if day.trade_date!=at.astimezone(local_tz).date().isoformat():return 'CALENDAR_STALE',None
        if instant(day.fetched_at)>at or instant(cache.updated_at)>at:return 'CALENDAR_INVALID',None
        session=day.sessions.get('regularMarket')
        if session is None:return 'WAITING_SESSION',None
        start,end=instant(session.start),instant(session.end)
        if day.source_trade_date!=day.trade_date or start.astimezone(local_tz).date().isoformat()!=day.trade_date:
            return 'CALENDAR_INVALID',None
        if not start<=at<end:return 'WAITING_SESSION',None
    except (ValueError,TypeError,OSError):return 'CALENDAR_INVALID',None
    path=root/'stores/latest_quotes.sqlite3'
    if not path.is_file():return 'QUOTE_MISSING',None
    try:
        connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True,timeout=1)
        try:
            rows=connection.execute('SELECT last_price,currency_raw,provider_timestamp,fetched_at,schema_version '
                'FROM latest_quotes WHERE source=? AND symbol=?',('toss-current-price-v1',cp['key'].split(':')[1])).fetchall()
        finally:connection.close()
        if not rows:return 'QUOTE_MISSING',None
        if len(rows)!=1:return 'QUOTE_INVALID',None
        price,currency,observed,fetched,version=rows[0]
        if currency!=cp['currency'] or version!=1 or not observed or not fetched:return 'QUOTE_INVALID',None
        price=number(price); observed=instant(observed); fetched=instant(fetched)
        if price<=0:return 'QUOTE_INVALID',None
        if observed-at>PROVIDER_CLOCK_SKEW or fetched>at:return 'QUOTE_FUTURE',None
        if observed>at and not instant(cp['starts_at'])<=observed<instant(cp['expires_at']):
            return 'QUOTE_FUTURE',None
        if not start<=observed<end:return 'QUOTE_OUTSIDE_SESSION',None
        if at-observed>QUOTE_MAX_AGE or at-fetched>QUOTE_MAX_AGE:return 'QUOTE_STALE',None
        if observed-fetched>PROVIDER_CLOCK_SKEW:return 'QUOTE_INVALID',None
        if observed<instant(cp['starts_at']):return 'QUOTE_BEFORE_START',None
        return 'OK',dict(key=cp['key'],price=format(price,'f'),currency=currency,
            provider_timestamp=observed.isoformat(),fetched_at=fetched.isoformat(),
            observed_at=at.isoformat(),source='toss-current-price-v1')
    except (sqlite3.Error,OSError):return 'QUOTE_UNAVAILABLE',None
    except (ValueError,TypeError,AttributeError):return 'QUOTE_INVALID',None


def checkpoint_snapshot(snapshot, cp, quote, *, at):
    """Only direct fresh quotes set current price; supplements cannot override it."""
    if snapshot is None:return None
    if snapshot.as_of>at:raise ValueError('미래 계좌 자료입니다')
    return replace(snapshot,as_of=at,instruments=tuple(replace(item,
        price=number(quote['price']) if quote else None,
        price_at=instant(quote['provider_timestamp']) if quote else None,
        sources=('toss-current-price-v1',)) if item.key==cp['key'] else item for item in snapshot.instruments))


def run_checkpoints(store, choices, *, data_root, snapshot, at, model, max_reviews, attempt, snapshot_loader=None):
    result={'checkpoints':[],'reviewed':[],'errors':[]}
    work=[]
    for choice in choices:
        if 'checkpoint' not in choice:continue
        key='checkpoint:'+choice['id']; state=store.watermark(key,{})
        state['last_checked_at']=at.isoformat()
        try:
            cp=validated_checkpoint(choice['checkpoint'])
            status=store.checkpoint_state(choice,at=at)['status']
        except (ValueError,KeyError,TypeError):
            state.update(status='CONFIG_INVALID',last_check_status='CONFIG_INVALID')
            store.set_watermark(key,state)
            result['errors'].append({'stage':'price_checkpoint','choice_id':choice['id'],'error':'CONFIG_INVALID'})
            result['checkpoints'].append(dict(state,choice_id=choice['id']))
            continue
        if status in {'CANCELLED','EXPIRED','NOT_STARTED','SUPERSEDED','AWAITING_USER'}:
            state['last_check_status']=status
        else:
            quote_status,quote=read_quote(data_root,cp,at=at)
            state['last_check_status']=quote_status
            if not state.get('observation') and quote:
                if {'<':lt,'<=':le,'>':gt,'>=':ge}[cp['direction']](number(quote['price']),number(cp['threshold'])):
                    state.update(observation=quote,status='PENDING_REVIEW',last_check_status='HIT')
                else:state.update(status='MONITORING',last_check_status='MONITORING')
            elif not state.get('observation'):state['status']=quote_status
            if state.get('observation'):work.append((choice,state,quote))
            elif quote_status not in {'OK','WAITING_SESSION'}:
                result['errors'].append({'stage':'price_checkpoint','choice_id':choice['id'],'error':quote_status})
        # Commit every first observation before any model invocation.
        store.set_watermark(key,state)
    for choice,state,quote in work[:max_reviews]:
        failure=state.get('failure')
        if not retry_due(failure,at=at,attempt=attempt):
            result['errors'].append(dict(stage='price_checkpoint',choice_id=choice['id'],**failure_notice(failure)))
            continue
        cp=choice['checkpoint']; observation=state['observation']
        evidence=[dict(id='checkpoint-price',kind='price',text='Toss 정규장 관측 가격: '+observation['price']+' '+observation['currency'],
                       available_at=observation['observed_at'],source=observation['source'])]
        trigger=dict(reason='PRICE_CHECKPOINT',observation=observation,question=cp['question'],
            original_why=choice['interpretation']['reason_quotes'],
            data_note='관측 가격은 당시 장중 조건 도달 근거다. 현재 가격은 별도 직접 시세만 사용한다. '
                '최신 보유·현금·종목 관련 자료의 날짜와 결측을 밝히고 원문 이유와 질문에 따라 유지·추가매수·축소 등 대응을 재검토한다. '
                '장중 도달은 종가 이탈·다음 거래일 회복 실패·구간 안착·거래량 개선의 확인이 아니다. 근거가 없으면 미확인으로 남긴다. '
                '이 체크포인트는 종가나 후속 거래일을 자동 감시하지 않는다. 자동 주문이나 추가 행동의 승인이 아니다.')
        try:
            current=snapshot_loader() if snapshot_loader else snapshot
            review_at=max(at,current.as_of) if snapshot_loader and current else at
            if current:
                evidence=[dict(id=e.id,kind=e.kind,text=e.text,available_at=e.available_at.isoformat(),source=e.source)
                    for item in current.instruments if item.key==cp['key'] for e in item.evidence
                    if e.scope is None or e.scope==choice['interpretation']['scope']]+evidence
            review=review_choice(store,choice,evidence,snapshot=checkpoint_snapshot(current,cp,quote,at=review_at),
                model=model,at=review_at,trigger=trigger,attempt=retry_attempt(attempt,failure))
            state.update(status='AWAITING_USER',review_id=review['id'],failure=None)
            result['reviewed'].append(dict(id=review['id'],choice_id=choice['id'],notify=True,effect=review['assessment']['effect']))
        except Exception as error:
            failure=failed_attempt(error,failure,at=at,attempt=attempt)
            state.update(status='REVIEW_FAILED',failure=failure)
            result['errors'].append(dict(stage='price_checkpoint',choice_id=choice['id'],**failure_notice(failure)))
        store.set_watermark('checkpoint:'+choice['id'],state)
    result['checkpoints'].extend(store.checkpoint_state(c,at=at) for c in choices
        if 'checkpoint' in c and not any(s['choice_id']==c['id'] for s in result['checkpoints']))
    return result
