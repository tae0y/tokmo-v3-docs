from copy import deepcopy
import hashlib

import pytest

from tokmo.programs.news_decision_engine import evening_digest_alerts
from tokmo.programs.news_decision_outbox import _digest_parts, queue_proposals
from tokmo.programs.news_digest_lookup import read_digest_item
from tokmo.records.news_decision_store import NewsDecisionStore
from tokmo.records.ops_kernel import OpsKernelStore
from test_news_decision_engine import observation
from test_news_evening_digest import ROLE, at, engine


def make_digest(e, day='2026-09-09', count=15, prefix='a'):
    when = at('10:00', day)
    e.tick([dict(observation(prefix+str(n),when), title='사업 계약 '+str(n), text='계약 내용 '+str(n)) for n in range(count)],when)
    return e.tick([],at('18:00',day))['alerts'][0]


def setup(tmp_path):
    store = NewsDecisionStore(tmp_path/'news.db')
    ops = OpsKernelStore(tmp_path/'ops.db',stable_phase_ids=frozenset())
    return store, ops, engine(store)


def send_one(e, ops, when, receipt='telegram-group:-123:101'):
    queue_proposals(e,ops,ROLE,cutoff=at('00:00'),activation_enabled=True)
    row=ops.claim_pending_telegram(ROLE,when)
    ops.mark_sent(ROLE,row.alert_id,when,receipt)


def read(tmp_path, number, **kw):
    return read_digest_item(tmp_path/'news.db',tmp_path/'ops.db',number,**kw)


def test_number_finds_frozen_sources_after_restart_and_later_analysis(tmp_path):
    store,ops,e=setup(tmp_path)
    digest=make_digest(e)
    lines=[line for line in digest['body'].splitlines()[2:] if line and not line.startswith('   - ')]
    assert len(lines)==15
    assert all(line==f"{n}. {entry['title']}" for n,(line,entry) in enumerate(zip(lines,digest['entries']),1))
    assert len([line for line in digest['body'].splitlines() if line.startswith('   - ')])==15
    assert 'https://' not in digest['body'] and '보도 확인' not in digest['body']
    queue_proposals(e,ops,ROLE,cutoff=at('00:00'),activation_enabled=True)
    assert read(tmp_path,15)['status']=='NOT_FOUND'
    sent=ops.claim_pending_telegram(ROLE,at('18:01'))
    ops.mark_sent(ROLE,sent.alert_id,at('18:01'),'telegram-group:-123:101')
    original=read(tmp_path,15)
    assert original['title']==digest['entries'][14]['title']
    assert original['summary']==digest['entries'][14]['summary']
    assert original['snapshot_available'] and original['sources_at_digest'][0]['url'].startswith('https://')
    chosen=next(event for event in store.events() if event['id']==original['event_id'])
    assert len(chosen['queued_digest_entries'])==1 and not chosen['pending_digest_details']
    assert 'evidence' not in chosen['last_alert']['entries'][0]  # no full archive replicated per event
    changed=deepcopy(chosen)
    changed.update(decision=dict(chosen['decision'], title='나중에 달라진 분석'), updated_at='2026-09-09T20:00:00+09:00', evidence=[])
    changed.pop('last_alert',None)
    store.save_event(changed);store.close();ops.close()
    paths=[tmp_path/'news.db',tmp_path/'ops.db']
    before=[hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]
    restored=read(tmp_path,15,reply_receipt='telegram-group:-123:101')
    assert restored['title']==original['title'] and restored['sources_at_digest']==original['sources_at_digest']
    assert restored['summary']==original['summary']
    assert restored['later_analysis']['decision']['title']=='나중에 달라진 분석'
    assert before==[hashlib.sha256(path.read_bytes()).hexdigest() for path in paths]


def test_latest_means_received_digest_and_date_keeps_old_numbers(tmp_path):
    store,ops,e=setup(tmp_path)
    first=make_digest(e);send_one(e,ops,at('18:01'))
    second=make_digest(e,day='2026-09-10',count=1,prefix='b')
    queue_proposals(e,ops,ROLE,cutoff=at('00:00'),activation_enabled=True)
    assert read(tmp_path,15)['digest_id']==first['id']
    assert read(tmp_path,1,date='2026-09-10')['status']=='NOT_FOUND'
    row=ops.claim_pending_telegram(ROLE,at('18:01','2026-09-10'))
    ops.mark_sent(ROLE,row.alert_id,at('18:01','2026-09-10'),'telegram-group:-123:102')
    assert read(tmp_path,1)['digest_id']==second['id']
    assert read(tmp_path,15)['status']=='NUMBER_NOT_FOUND'
    assert read(tmp_path,15,date='2026-09-09')['digest_id']==first['id']
    assert read(tmp_path,15,reply_receipt='telegram-group:-999:101')['status']=='NOT_FOUND'
    store.close();ops.close()


def test_partial_delivery_is_queryable_without_claiming_complete_send(tmp_path):
    store,ops,e=setup(tmp_path)
    when=at('10:00')
    e.tick([dict(observation(str(n),when),title='긴 제목 '+str(n)+' 설명'*150) for n in range(15)],when)
    e.tick([],at('18:00'))
    send_one(e,ops,at('18:01'))
    result=read(tmp_path,1)
    assert result['status']=='OK' and result['delivery_status']=='PARTIAL'
    store.close();ops.close()


def test_titles_are_plain_lines_and_wrapped_messages_preserve_numbering():
    alerts=[dict(id=str(n),event_id='e'+str(n),created_at=at('18:00').isoformat(),
        evening_slot=at('18:00').isoformat(),digest_hour=18,
        headline='[계약 성사](https://example.invalid/a)\n새 조건 https://example.invalid/b',body='unused') for n in range(30)]
    digest=evening_digest_alerts(alerts)
    assert digest['body'].splitlines()[2]=='1. 계약 성사 새 조건'
    parts=_digest_parts(digest['body'],limit=90)
    assert ''.join(parts)==digest['body'] and len(parts)>1
    assert all(len(part.encode('utf-16-le'))//2<=90 for part in parts)
    assert all(not part or part.splitlines()[0].startswith(tuple(str(n)+'.' for n in range(1,31))) for part in parts[1:])


def test_invalid_number_does_not_create_a_database(tmp_path):
    with pytest.raises(ValueError):read(tmp_path,0)
    assert not list(tmp_path.iterdir())
