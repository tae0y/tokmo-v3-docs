"""URL buttons use the persisted production outbox, never a second sender."""
from types import SimpleNamespace
import pytest

from tokmo.programs.news_alert_delivery import run_alert_delivery, news_live_body_guard
from tokmo.programs.news_decision_engine import render_alert, evening_digest_alerts, digest_alerts, identity
from tokmo.programs.news_decision_outbox import queue_proposals
from tokmo.records.news_decision_store import timestamp
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from test_news_decision_engine import observation, decision

NOW = timestamp('2026-09-23T10:00:00+09:00')
ROLE = ProducerRole.HERMES


def deliver(tmp_path, alert, existing=None):
    engine = SimpleNamespace(pending_alerts=lambda: [alert], store=SimpleNamespace(events=lambda: []),
                             acknowledge=lambda *args: None)
    ops = OpsKernelStore(tmp_path/'ops.db', stable_phase_ids=frozenset())
    queued = queue_proposals(engine, ops, ROLE, cutoff=NOW, activation_enabled=True)
    sent = []
    def sender(body, *, reply_markup):
        sent.append((body, reply_markup))
        return str(len(sent))
    for _ in range(queued['queued']):
        result = run_alert_delivery(ops_kernel=ops, role=ROLE, telegram_send=sender,
            keyboard_resolver=lambda _: existing, delivery_binder=lambda *a, **k: None,
            now=timestamp(alert.get('digest_slot') or NOW), body_guard=news_live_body_guard)
        assert result['sent'] == 1
    # Requeue is idempotent: exact body/markup bytes survive repeat composition.
    assert queue_proposals(engine, ops, ROLE, cutoff=NOW, activation_enabled=True)['queued'] == 0
    for index in range(queued['queued']):
        saved = ops.read_outbox('nda-'+identity([alert['id'], index]))
        assert saved is not None and saved.status is not None
        assert saved.body_evidence[0] in [item[0] for item in sent]
        assert saved.status.value == 'SENT' and saved.provider_message_id
    ops.close()
    for _, markup in sent:
        if markup:
            rows=markup['inline_keyboard'][len((existing or {}).get('inline_keyboard', [])):]
            assert all(len(row)==2 for row in rows[:-1])
            assert not rows or 1<=len(rows[-1])<=2
    return sent


def test_source_url_moves_to_button_preserving_source_time_and_controls(tmp_path):
    body = render_alert(decision(['a'], portfolio_note='보유 이유 점검'), [dict(observation('a', NOW), publisher='연합뉴스')], NOW)
    controls = {'inline_keyboard': [[{'text': '상세', 'callback_data': 'news:detail'}]]}
    sent = deliver(tmp_path, dict(id='one', body=body, created_at=NOW.isoformat()), controls)
    text, markup = sent[0]
    assert 'https://example.invalid/a' not in text
    assert '출처: 연합뉴스 · ' in text
    assert markup['inline_keyboard'] == controls['inline_keyboard'] + [
        [{'text': '1 · 연합', 'url': 'https://example.invalid/a'}]]
    assert controls == {'inline_keyboard': [[{'text': '상세', 'callback_data': 'news:detail'}]]}


def article(key, text='요약'):
    body = render_alert(decision([key], title='기사 '+key, facts=text, portfolio_note='보유 이유 점검'),
                        [dict(observation(key, NOW), publisher='출처 '+key)], NOW)
    return dict(id=key, event_id=key, body=body, created_at=NOW.isoformat(), headline='기사 '+key,
                digest_summary=text, evening_slot='2026-09-23T18:00:00+09:00', digest_hour=18)


@pytest.mark.parametrize('kind', ['morning', 'numbered'])
def test_numbered_digest_buttons_belong_to_the_numbered_items_on_each_part(tmp_path, kind):
    import re
    alerts = [article(str(i), '중요한 경과 '*150) for i in range(7)]
    sent = deliver(tmp_path, evening_digest_alerts(alerts) if kind=='numbered' else digest_alerts(alerts))
    assert len(sent) > 1
    all_urls = []
    for body, markup in sent:
        numbers = re.findall(r'(?m)^(\d+)\. ', body)
        buttons = [b for row in markup['inline_keyboard'] for b in row]
        if kind=='numbered':assert [b['text'].split(' · ')[0] for b in buttons] == numbers
        all_urls.extend(b['url'] for b in buttons)
        assert 'https://' not in body
    assert sorted(all_urls) == [f'https://example.invalid/{i}' for i in range(7)]


@pytest.mark.parametrize('kind', ['morning', 'evening'])
def test_split_article_keeps_its_own_links_without_cross_article_buttons(tmp_path, kind):
    alerts = [article(key, key*3900+'😀'*150) for key in ('A', 'B')]
    alert = digest_alerts(alerts) if kind == 'morning' else evening_digest_alerts(alerts)
    sent = deliver(tmp_path, alert)
    assert len(sent) >= 4
    for body, markup in sent:
        assert len(body.encode('utf-16-le'))//2 <= 3500
        urls = {b['url'] for row in markup['inline_keyboard'] for b in row}
        for key in ('A', 'B'):
            assert (f'https://example.invalid/{key}' in urls) == (key in body)
        for row in markup['inline_keyboard']:
            for button in row:
                assert button['text'].split(' · ')[0] == {'A':'1', 'B':'2'}[button['url'].rsplit('/',1)[-1]]


@pytest.mark.parametrize('kind', ['important', 'headline', 'followup', 'correction', 'explanation', 'source'])
def test_every_individual_news_surface_has_source_buttons(tmp_path, kind):
    change = 'CORRECTION' if kind in {'correction', 'explanation', 'source'} else 'NEW'
    notice = {'explanation': 'EXPLANATION_ONLY', 'source': 'SOURCE_ONLY'}.get(kind)
    body = render_alert(decision(['a','b','c'], change=change, correction_notice=notice, urgency='NORMAL',
                        portfolio_note='' if kind == 'headline' else '보유 이유 점검'),
                        [observation(key, NOW) for key in ('a','b','c')], NOW, followup=kind == 'followup')
    text, markup = deliver(tmp_path, dict(id=kind, body=body, created_at=NOW.isoformat()))[0]
    assert 'https://' not in text and '출처: ' in text
    assert markup['inline_keyboard'][0][0]['url'] == 'https://example.invalid/a'


def test_absent_unsafe_and_duplicate_sources_are_not_invented_or_rewritten():
    from tokmo.programs.news_source_buttons import source_buttons
    for body in ['출처 미확인', '출처: 공급자\njavascript:alert(1)', '출처: 공급자\nhttps://u:p@example.invalid/a', '출처: 공급자\nhttps://[broken']:
        assert source_buttons(body) == (body, [])
    body = '출처: 공급자 · 게시 시각 미확인\nhttps://example.invalid/a?x=1&y=2'
    text, buttons = source_buttons(body+'\n\n'+body)
    assert len(buttons) == 1 and buttons[0]['url'].endswith('?x=1&y=2')
    assert text.count('게시 시각 미확인') == 2


def test_private_url_remains_in_privacy_guard_after_removal_from_visible_text(tmp_path):
    from tokmo.programs.news_source_buttons import source_buttons, MARKER
    import json
    text, buttons = source_buttons('출처: 공급자\nhttps://example.invalid/a?api_key=not-a-real-value')
    assert 'api_key' not in text
    assert not news_live_body_guard(SimpleNamespace(body_evidence=(text, MARKER+json.dumps(buttons))))


@pytest.mark.parametrize('kind', ['morning', 'evening'])
def test_article_source_labels_match_user_format(tmp_path, kind):
    publishers = [('연합뉴스', 'DART'), ('연합뉴스',), ('BBC', 'YTN')]
    alerts = []
    for number, names in enumerate(publishers, 1):
        keys = [f'{number}-{i}' for i in range(1, len(names)+1)]
        alert = article(str(number))
        alert['body'] = render_alert(decision(keys, portfolio_note='보유 이유 점검'),
            [dict(observation(key, NOW), publisher=name) for key, name in zip(keys, names)], NOW)
        alerts.append(alert)
    bundle = digest_alerts(alerts) if kind == 'morning' else evening_digest_alerts(alerts)
    sent = deliver(tmp_path, bundle)
    buttons = [b for _, markup in sent for row in markup['inline_keyboard'] for b in row]
    assert [b['text'] for b in buttons] == ['1-1 · 연합', '1-2 · DART공시', '2 · 연합', '3-1 · BBC', '3-2 · YTN']
    assert [b['url'] for b in buttons] == [f'https://example.invalid/{key}' for key in ('1-1','1-2','2-1','3-1','3-2')]
    assert [len(row) for row in sent[0][1]['inline_keyboard']] == [2, 2, 1]


@pytest.mark.parametrize('count', range(5))
def test_source_rows_pair_across_markers_preserving_exact_buttons_and_controls(count):
    import json
    from tokmo.programs.news_source_buttons import MARKER, source_markup
    buttons=[{'text': f'원문 {i+1} · 출처', 'url': f'https://example.invalid/{i}?q=%2F&a=한글#끝'}
             for i in range(count)]
    controls={'inline_keyboard': [[{'text': '상세', 'callback_data': 'news:detail'}]], 'extra': True}
    before=json.dumps(controls)
    outbox=SimpleNamespace(body_evidence=tuple(MARKER+json.dumps([b]) for b in buttons))
    result=source_markup(outbox,controls)
    assert result==dict(controls,inline_keyboard=controls['inline_keyboard']+[buttons[i:i+2] for i in range(0,count,2)])
    assert json.dumps(controls)==before
    if not count:assert result is controls


def test_legacy_evening_metadata_and_no_source_item_keep_visible_numbers():
    from copy import deepcopy
    from tokmo.programs.news_decision_outbox import presentation_parts
    old = [{'text':'8번 원문 1 · 연합뉴스','url':'https://example.invalid/shared'},
           {'text':'8번 원문 2 · DART','url':'https://example.invalid/dart'}]
    alert = dict(kind='EVENING_DIGEST', entries=[dict(number=7), dict(number=8, source_buttons=old)])
    before = deepcopy(alert)
    buttons = presentation_parts(alert, '[저녁 뉴스 모아보기]\n\n7. 링크 없는 뉴스\n\n8. 두 번째 뉴스')[0][1]
    assert [b['text'] for b in buttons] == ['8-1 · 연합', '8-2 · DART공시']
    assert [b['url'] for b in buttons] == [b['url'] for b in old]
    assert alert == before
