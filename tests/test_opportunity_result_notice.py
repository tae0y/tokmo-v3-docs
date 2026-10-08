"""PAPER result notice: one stdout summary per durably applied slot, never on waiting/failure."""
from pathlib import Path
import subprocess
import sys

import pytest

from tokmo.programs.opportunity_result_notice import drain, render
from tokmo.programs.opportunity_rotation import Paper
from tokmo.values.opportunity_rotation import Costs

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / 'tools'))
import run_opportunity_result_notice as cli  # noqa: E402
CASH_REASON = '보유 종목과 미체결 주문이 없어, 이번에는 200만원을 현금으로 유지해.'


def slot(status, assessments=(), key='2026-10-02:10:05'):
    names = [('066570', 'LG전자'), ('009150', '삼성전기'), ('005930', '삼성전자')]  # 삼성전자: input only
    return dict(status=status, at='2026-10-02T10:05:29+09:00', reason=None if status=='DECIDED' else 'pending',
        packet=dict(candidates=[dict(symbol=s, name=n) for s, n in names]),
        decision=dict(id=key, cash_reason=CASH_REASON, assessments=list(assessments)),
        applied_at='2026-10-02T10:07:00+09:00' if status=='DECIDED' else None)


WATCH = [dict(symbol='066570', action='WATCH', reason='LG전자는 우선 관찰할 후보야. 다만 근거가 부족해.'),
         dict(symbol='009150', action='WATCH', reason='삼성전기는 신규 매수는 보류해.')]
BUY = dict(symbol='066570', action='ENTER', reason='수주 확인 뒤 소액 진입.', proposal=dict(id='p1', symbol='066570', side='BUY'))


@pytest.fixture
def paper(tmp_path):
    p = Paper(tmp_path / 'paper')
    p.initialize(Costs('synthetic zero', '0', '0', '0', '0'))
    return p


def put(paper, key, value, proposals=None):
    def change(s):
        s.setdefault('slots', {})[key] = value
        s['proposals'].update(proposals or {})
        return s
    paper.store.transaction(change)


def test_no_trade_summary_lists_only_assessed_names_with_stored_reasons(paper):
    put(paper, '2026-10-02:10:05', slot('DECIDED', WATCH))
    text = render(paper.state(), '2026-10-02:10:05')
    head = text.splitlines()[0]
    assert head == '[톡모 모의투자 · 10-02 10:05] 분석 완료 · 신규 매수 제안 없음 · 현금 200만원'
    assert '검토: LG전자(관찰), 삼성전기(관찰)' in text
    assert CASH_REASON in text and '다만 근거가 부족해.' in text
    assert '삼성전자' not in text and '005930' not in text


def test_buy_intent_is_not_reported_as_fill(paper):
    order = dict(id='p1', symbol='066570', side='BUY', status='PENDING', reserved='500000')
    put(paper, '2026-10-02:10:05', slot('DECIDED', [BUY]), {'p1': order})
    text = render(paper.state(), '2026-10-02:10:05')
    assert '분석 완료 · 매수 제안 1건(모의 주문 대기 1건, 체결 아님) · 현금 200만원 · 가용 150만원' in text
    assert 'LG전자(매수 제안)' in text and '모의 체결' not in text
    put(paper, '2026-10-02:10:05', slot('DECIDED', [BUY]), {'p1': dict(order, status='FILLED', reserved='0')})
    assert '매수 제안 1건(모의 체결 1건)' in render(paper.state(), '2026-10-02:10:05')


def test_empty_assessments_are_stated_plainly(paper):
    put(paper, '2026-10-02:13:30', slot('DECIDED', key='2026-10-02:13:30'))
    assert '검토: 별도 검토 의견 없음' in render(paper.state(), '2026-10-02:13:30')


def test_drain_baselines_history_then_emits_each_applied_slot_once(paper, tmp_path):
    ledger = tmp_path / 'notice' / 'ledger.json'
    put(paper, '2026-10-01:10:05', slot('DECIDED', WATCH, key='2026-10-01:10:05'))
    assert drain(paper, ledger, now='2026-10-02T09:00:00+09:00') is None  # no backlog blast
    for status in ('CALLING', 'WAITING_DATA', 'FAILED', 'SUPERSEDED'):
        put(paper, '2026-10-02:10:05', slot(status, WATCH))
        assert drain(paper, ledger, now='2026-10-02T10:06:00+09:00') is None
    put(paper, '2026-10-02:10:05', slot('DECIDED', WATCH))
    text = drain(paper, ledger, now='2026-10-02T10:08:00+09:00')
    assert text.startswith('[톡모 모의투자 · 10-02 10:05]')
    assert drain(paper, ledger, now='2026-10-02T10:09:00+09:00') is None
    assert drain(Paper(paper.store.root), ledger, now='2026-10-02T10:10:00+09:00') is None  # restart
    assert '"status": "EMITTED_TO_CRON_STDOUT"' in ledger.read_text()


def test_cli_prints_once_and_unwritable_ledger_prints_nothing(paper, tmp_path, capsys):
    ledger = tmp_path / 'ledger.json'
    args = ['--state-root', str(paper.store.root), '--ledger', str(ledger)]
    assert cli.main(args) == 0 and capsys.readouterr().out == ''  # baseline on empty history
    put(paper, '2026-10-02:10:05', slot('DECIDED', WATCH))
    blocked = tmp_path / 'blocked'
    blocked.write_text('')
    assert cli.main(['--state-root', str(paper.store.root), '--ledger', str(blocked / 'x.json')]) == 2
    assert capsys.readouterr().out == ''
    assert cli.main(args) == 0 and '[톡모 모의투자 · 10-02 10:05]' in capsys.readouterr().out
    assert cli.main(args) == 0 and capsys.readouterr().out == ''


def test_notice_program_uses_caller_supplied_time():
    source = (PROJECT / 'src/tokmo/programs/opportunity_result_notice.py').read_text()
    assert 'datetime.now(' not in source
    import inspect
    assert inspect.signature(drain).parameters['now'].default is inspect.Parameter.empty


def test_wrapper_runs_notice_only_after_successful_step():
    wrapper = PROJECT / 'ops/hermes-cron/tokmo_v3_opportunity_rotation.sh'
    assert subprocess.run(['bash', '-n', str(wrapper)]).returncode == 0
    body = wrapper.read_text()
    success = body.split(' step >"$OUT" 2>"$ERR"; then', 1)[1].split('\nfi\n', 1)[0]
    assert 'run_opportunity_result_notice.py' in success
    assert 'run_opportunity_result_notice.py' not in body.split('\nfi\n', 1)[1]
