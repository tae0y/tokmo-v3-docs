"""Approved PAPER examples. All market input here is explicitly synthetic."""
from copy import deepcopy
from datetime import datetime, timedelta
from decimal import Decimal
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from tokmo.programs.opportunity_rotation import Paper, build_packet
from tokmo.values.opportunity_rotation import Costs, digest

D = Decimal
DAY = '2026-09-28'
ROOT = Path(__file__).resolve().parents[1]


def at(time, day=DAY):
    return f'{day}T{time}+09:00'


def session(day=DAY):
    return dict(date=day, open=at('09:00:00', day), close=at('15:30:00', day), source='Toss KR_MARKET_CALENDAR')


def proposal(identity='b1', symbol='111111', side='BUY', price='10000', quantity=10, time='09:01:30', **extra):
    value = dict(id=identity, symbol=symbol, side=side, trigger='BELOW' if side == 'BUY' else 'ABOVE',
                 price=price, quantity=quantity, proposed_at=at(time), reason='거래 증가와 지지 확인',
                 expected_move='저항 재시험', invalidation='지지 이탈', review_at=at('14:00:00'),
                 assumed_exit_price=str(D(price)*D('.9')), target='NORMAL', weak_reason=None)
    value.update(extra)
    return value


def bar(time, symbol='111111', low='9900', high='11000', opening='10000', close='10000', day=DAY):
    return dict(symbol=symbol, at=(datetime.fromisoformat(at(time, day))+timedelta(minutes=1)).isoformat(), open=opening, high=high, low=low, close=close, volume='100',
                currency='KRW', interval='1m', adjusted=False)


def market(bars=(), time='09:05:00', symbols=('111111',), day=DAY, marks=None):
    end = at(time, day)
    valuation = datetime.fromisoformat(end).replace(second=0, microsecond=0).isoformat()
    rows = {(b['symbol'],b['at']):b for b in bars}
    minute = datetime.fromisoformat(at('09:01:00',day))
    while minute <= min(datetime.fromisoformat(end),datetime.fromisoformat(at('15:30:00',day))):
        for code in symbols:
            rows.setdefault((code,minute.isoformat()),dict(symbol=code,at=minute.isoformat(),open='10000',high='10000',low='10000',close='10000',volume='0',currency='KRW',interval='1m',adjusted=False))
        minute += timedelta(minutes=1)
    marks = {code:dict(q,at=(datetime.fromisoformat(q['at'])+timedelta(minutes=1)).isoformat()) for code,q in (marks or {}).items()}
    return dict(as_of=end, sessions=[session(day)], bars=list(bars)+[b for key,b in rows.items() if key not in {(v['symbol'],v['at']) for v in bars}],
                coverage=[dict(symbol=s, start=at('09:00:00', day), end=end, complete=True) for s in symbols],
                valuation_at=valuation, marks=marks or {}, source='synthetic fixture')


def paper(tmp_path, costs=None):
    p = Paper(tmp_path)
    p.initialize(costs or Costs('synthetic zero', '0', '0', '0', '0'))
    return p


def test_reservations_budget_five_symbols_and_atomic_failure(tmp_path):
    unaffordable = paper(tmp_path / 'five_500k')
    with pytest.raises(ValueError):
        unaffordable.propose([proposal(str(i), f'{i:06}', price='500000', quantity=1) for i in range(5)], session())
    assert unaffordable.report()['available_cash'] == '2000000'
    assert unaffordable.state()['proposals'] == {}
    p = paper(tmp_path)
    p.propose([proposal(str(i), f'{i:06}', price='400000', quantity=1) for i in range(5)], session())
    assert p.report()['available_cash'] == '0'
    before = p.state()
    with pytest.raises(ValueError):
        p.propose([proposal('six', '999999', quantity=1)], session())
    assert p.state() == before
    p.cancel('0', at('09:02:00'), '취소')
    assert p.report()['available_cash'] == '400000'
    with pytest.raises(ValueError):
        p.propose([proposal('large', '999999', price='500000', quantity=1)], session())
    assert p.report()['available_cash'] == '400000'


def test_cost_inclusive_cash_and_principal_cap_with_add(tmp_path):
    p = paper(tmp_path, Costs('illustrative', '.001', '.001', '.002', '0'))
    with pytest.raises(ValueError):
        p.propose([proposal(str(i), f'{i:06}', price='400000', quantity=1) for i in range(5)], session())
    p.propose([proposal(price='100000', quantity=6)], session())
    p.advance(market([bar('09:02:00', low='99000', high='100000', opening='100000', close='100000')]))
    assert p.report()['cash'] == '1399400'
    assert p.report()['available_cash'] == '1399400'  # never subtract holdings twice
    p.propose([proposal('add', price='100000', quantity=4, time='09:06:00')], session())
    with pytest.raises(ValueError):
        p.propose([proposal('excess', quantity=1, time='09:06:00')], session())
    p.advance(market(time='09:07:00'))
    p.cancel('add', at('09:07:00'), '재검토')
    assert p.report()['cash'] == '1399400'
    assert p.report()['available_cash'] == '1399400'
    with pytest.raises(ValueError):
        p.initialize(Costs('reset', '0', '0', '0', '0'))


def test_no_retroactive_or_premarket_fills_no_incomplete_candle(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal()], session())
    p.advance(market([bar('08:59:00'), bar('09:00:00'), bar('09:01:00'), bar('09:05:00')]))
    assert p.report()['cash'] == '2000000'
    assert p.state()['proposals']['b1']['status'] == 'PENDING'
    p.advance(market([bar('09:06:00')], time='09:07:00'))
    assert p.report()['cash'] == '1900000'
    saved = p.state()
    assert Paper(tmp_path).advance(market([bar('09:06:00')], time='09:07:00')) == p.report()
    assert p.state() == saved
    with pytest.raises(ValueError):
        p.advance(market([bar('09:02:00')], time='09:03:00'))
    assert p.state() == saved


def test_same_minute_sell_and_duplicate_quantity_reservation(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal(time='09:00:00')], session())
    p.advance(market([bar('09:00:00')], time='09:01:00'))
    # Submitted after the candle: can never sell retrospectively in it.
    p.propose([proposal('sell', side='SELL', price='10500', time='09:01:00')], session())
    with pytest.raises(ValueError):
        p.propose([proposal('double', side='SELL', quantity=1, time='09:01:00')], session())
    p.advance(market([bar('09:00:00')], time='09:02:00'))
    assert p.report()['cash'] == '1900000'
    p.advance(market([bar('09:02:00')], time='09:03:00'))
    assert p.report()['cash'] == '2005000'
    assert p.report()['realized_pnl'] == '5000'


def test_partial_stop_gap_low_profit_and_lost_positions_persist(tmp_path):
    p = paper(tmp_path, Costs('fee assumption', '.001', '.001', '.002', '0'))
    p.propose([proposal(time='09:00:00')], session())
    p.advance(market([bar('09:00:00')], time='09:01:00'))
    p.propose([proposal('stop', side='SELL', trigger='BELOW', price='9500', quantity=4, time='09:01:00', reason='지지 붕괴 손절')], session())
    p.advance(market([bar('09:01:00', low='8900', high='9200', opening='9000', close='9100')], time='09:02:00'))
    r = p.report()
    assert r['positions']['111111']['principal'] == '60000'
    assert r['realized_pnl'] == '-4148'  # 36k - 40k - 40 buy fee - 36 sell fee - 72 tax
    assert r['positions']['111111']['buy_cost_basis'] == '60'
    p.propose([proposal('switch', side='SELL', price='10000', quantity=6, time='09:02:00', reason='더 좋은 후보로 교체')], session())
    p.advance(market([bar('09:02:00')], time='09:03:00'))
    assert p.report()['positions'] == {}
    assert p.report()['realized_pnl'] == '-4388'
    assert p.report()['cash'] == '1995612'
    assert len(p.report()['closed_trades']) == 2
    assert p.report()['trading_costs'] == '388'
    assert p.report()['closed_trades'][0]['holding_seconds'] == 60


def test_pending_expiry_no_proceeds_and_duplicate_identity(tmp_path):
    p = paper(tmp_path)
    order = proposal()
    p.propose([order], session())
    saved = p.state()
    p.propose([order], session())
    assert p.state() == saved
    with pytest.raises(ValueError):
        p.propose([dict(order, quantity=5)], session())
    p.advance(market(time='15:30:00'))
    assert p.report()['reserved_cash'] == '100000'  # auction END bar not observed yet
    final = market(time='15:31:00'); final['bars'].append(dict(bar('15:30:00'),volume='0'))
    p.advance(final)
    assert p.report()['available_cash'] == '2000000'
    assert p.state()['proposals']['b1']['status'] == 'EXPIRED'
    p.propose([order], session())
    assert p.state()['proposals']['b1']['status'] == 'EXPIRED'
    with pytest.raises(ValueError):
        p.propose([proposal('sell', side='SELL')], session())


def test_missing_coverage_does_not_expire_and_simultaneous_safe(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal(str(i), f'{i:06}', price='400000', quantity=1) for i in reversed(range(5))], session())
    bad = market(time='15:30:00'); bad['coverage'] = []
    with pytest.raises(ValueError):
        p.advance(bad)
    bars = [bar('09:02:00', symbol=f'{i:06}', low='399000', high='400000', opening='400000', close='400000') for i in reversed(range(5))]
    p.advance(market(bars, symbols=tuple(f'{i:06}' for i in range(5))))
    assert p.report()['cash'] == '0'
    assert p.report()['nav'] is None
    assert len(p.report()['missing_marks']) == 5
    assert [t['id'] for t in p.state()['fills']] == ['0','1','2','3','4']


def test_benchmark_official_open_only_fees_residual_same_time(tmp_path):
    p = paper(tmp_path, Costs('assumption', '.001', '.001', '.002', '0'))
    early = market([bar('09:00:00', symbol='069500', opening='50000', low='49000', high='51000', close='50000', day='2026-09-25')], day='2026-09-25')
    p.advance(early)
    assert p.report()['benchmark']['status'] == 'PENDING_OPEN'
    monday = market([bar('09:00:00', symbol='069500', opening='50000', low='49000', high='51000', close='50000')],
                    marks={'069500':dict(price='51000', at=at('09:04:00'))})
    p.advance(monday)
    b = p.report()['benchmark']
    assert (b['quantity'], b['cash'], b['buy_fee'], b['nav']) == (39, '48050', '1950', '2037050')
    assert b['return'] == '0.018525'
    assert p.report()['nav'] == '2000000'
    p.advance(market(time='09:06:00'))
    assert p.report()['benchmark']['status'] == 'MISSING_MARK'
    assert p.state()['benchmark']['open_price'] == '50000'


def test_report_unrealized_drawdown_sampling_cash_and_review_not_forced(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal(time='09:00:00', review_at=at('09:01:00'))], session())
    p.advance(market([bar('09:00:00')], time='09:01:00', marks={'111111':dict(price='12000', at=at('09:00:00'))}))
    assert p.report()['nav'] == '2020000'
    assert p.report()['unrealized_pnl'] == '20000'
    p.advance(market(time='09:02:00', marks={'111111':dict(price='8000', at=at('09:01:00'))}))
    r = p.report()
    assert r['nav'] == '1980000'
    assert abs(D(r['observed_max_drawdown']) - D(40000)/D(2020000)) < D('1e-12')
    assert r['drawdown_basis'] == '완전한 가격 표본의 NAV; 장중 연속 최대낙폭 아님'
    assert r['positions']['111111']['quantity'] == 10
    assert r['review_due'] == ['b1']


def discovery():
    return dict(as_of=at('09:00:00'), session=session(), completed_session='2026-09-23',
                source='synthetic fixture', universe='provider maximum ranking union; no local cutoff',
                candidates=[dict(symbol='111111', name='합성종목', eligible=True, missing=[],
                                 quote=dict(price='10000', at=at('09:00:00')), bars=[dict(date='2026-09-23', open='10000', high='11000', low='9000', close='10000', volume='100')],
                                 ranking=[dict(type='MARKET_TRADING_AMOUNT', rank=1, amount='1000000', volume='100')],
                                 evidence=[], context_status={'news':'STORE_UNAVAILABLE'})], exclusions=[])


def decision(packet, **changes):
    value = dict(id='daily1', packet_id=packet['id'], author='test human', verified_by='test human',
                 verified_at=at('09:00:00'), assessments=[dict(symbol='111111', action='ENTER', reason='검증한 추세',
                    proposal={k:v for k,v in proposal(time='09:00:00',protection={'target_price':'10300','stop_price':'9500'}).items() if k!='proposed_at'})], cash_reason='남은 현금 대기')
    value.update(changes)
    return value


def test_packet_decision_validation_execution_separate_stale_and_unknown(tmp_path):
    p = paper(tmp_path)
    packet = build_packet(discovery(), p.state())
    assert p.state()['proposals'] == {}
    assert packet['automation'] == 'COMMON_PRODUCT_GATE_REQUIRED'
    assert packet['candidates'][0]['features']['max_drawdown']['value'] is None
    invalid = decision(packet); invalid['assessments'][0]['proposal']['quantity'] = True
    with pytest.raises(ValueError): p.decide(packet, invalid, at('09:01:00'))
    invalid = decision(packet); invalid['assessments'][0]['symbol'] = '222222'
    with pytest.raises(ValueError): p.decide(packet, invalid, at('09:01:00'))
    invalid = decision(packet); invalid['assessments'][0]['proposal']['weak_reason'] = None; invalid['assessments'][0]['proposal']['target'] = 'WEAK'
    with pytest.raises(ValueError): p.decide(packet, invalid, at('09:01:00'))
    p.decide(packet, decision(packet), at('09:01:00'))
    assert p.state()['proposals']['b1']['proposed_at'] == at('09:01:00')
    saved = p.state(); p.decide(packet, decision(packet), at('09:02:00')); assert p.state() == saved
    with pytest.raises(ValueError): p.decide(packet, decision(packet,id='changed'), at('09:02:00'))
    altered = deepcopy(packet); altered['candidates'][0]['quote']['price'] = '999'
    with pytest.raises(ValueError): p.decide(altered, decision(altered,id='tamper'), at('09:02:00'))


def test_cash_hold_valid_and_cli_vertical_without_default_state(tmp_path):
    def run(*args):
        result = subprocess.run([sys.executable,str(ROOT/'tools/run_opportunity_rotation.py'),'--state-root',str(tmp_path/'paper'),*args],cwd=tmp_path,env={**os.environ,'PYTHONPATH':str(ROOT/'src')+os.pathsep+os.environ.get('PYTHONPATH','')},text=True,capture_output=True)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)
    costs = tmp_path/'costs.json'; costs.write_text(json.dumps(dict(label='synthetic zero',buy_rate='0',sell_rate='0',stock_tax_rate='0',benchmark_tax_rate='0')))
    run('init','--costs',str(costs))
    source = tmp_path/'source.json'; source.write_text(json.dumps(discovery()))
    packet_path = tmp_path/'packet.json'
    packet = run('discover','--source-file',str(source),'--output',str(packet_path))
    choice = decision(packet,assessments=[dict(symbol='111111', action='WATCH', reason='가격 대기')])
    choice_path = tmp_path/'decision.json'; choice_path.write_text(json.dumps(choice))
    run('decide','--packet',str(packet_path),'--decision-file',str(choice_path),'--at',at('09:01:00'))
    marks = tmp_path/'market.json'; marks.write_text(json.dumps(market()))
    run('advance','--source-file',str(marks))
    assert run('report')['cash'] == '2000000'
    assert run('journal')['decisions']['daily1']['verified_by'] == 'test human'
    assert not (tmp_path/'var').exists()


def test_strict_decision_fields_future_source_and_no_prestart_trades(tmp_path):
    p = paper(tmp_path)
    pkt = build_packet(discovery(), p.state())
    extra = decision(pkt); extra['assessments'][0]['proposal']['proposed_at'] = at('08:00:00')
    with pytest.raises(ValueError): p.decide(pkt, extra, at('09:01:00'))
    extra = decision(pkt); extra['assessments'][0]['replacement_symbol'] = '222222'
    with pytest.raises(ValueError): p.decide(pkt, extra, at('09:01:00'))
    future = discovery(); future['candidates'][0]['quote']['at'] = at('10:00:00')
    with pytest.raises(ValueError): build_packet(future,p.state())
    before = proposal(proposed_at=at('09:00:00','2026-09-25'))
    with pytest.raises(ValueError): p.propose([before], session('2026-09-25'))


def test_hold_review_switch_no_future_proceeds_and_add_basis(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal(price='100000',quantity=10,time='09:00:00')],session())
    p.advance(market([bar('09:00:00',low='100000',high='100000',opening='100000',close='100000')],time='09:01:00'))
    p.propose([proposal('sale',side='SELL',price='101000',quantity=10,time='09:01:00')],session())
    assert p.report()['cash']=='1000000'
    with pytest.raises(ValueError):
        p.propose([proposal('new','222222',price='100000',quantity=10,time='09:01:00'),proposal('new2','333333',quantity=1,time='09:01:00')],session())
    p.cancel('sale',at('09:01:00'),'다시 유지')
    data=discovery();data['as_of']=at('09:01:00');pkt=build_packet(data,p.state())
    choice=decision(pkt,verified_at=at('09:01:00'),assessments=[dict(symbol='111111',action='HOLD',reason='추세 유지')])
    p.decide(pkt,choice,at('09:02:00'))
    assert p.report()['positions']['111111']['quantity']==10


def test_replay_does_not_accept_conflicting_bars_and_next_day_expiry(tmp_path):
    p=paper(tmp_path);p.propose([proposal()],session())
    bad=market([bar('09:02:00'),bar('09:02:00',close='10500')])
    with pytest.raises(ValueError):p.advance(bad)
    nxt=market(time='09:00:00',day='2026-09-29');nxt['sessions'].insert(0,session())
    nxt['bars'].extend(market(time='15:30:00')['bars'])
    nxt['bars'].append(dict(bar('15:30:00'),volume='0'))  # actual final auction interval
    nxt['coverage'].append(dict(symbol='111111',start=at('09:00:00'),end=at('15:30:00'),complete=True))
    p.advance(nxt)
    assert p.report()['available_cash']=='2000000'
    assert p.state()['proposals']['b1']['status']=='EXPIRED'
    unchanged=p.state()
    with pytest.raises(ValueError):p.advance(market())
    assert p.state()==unchanged


def test_failed_decision_is_atomic_including_cancel_and_sell_reservation(tmp_path):
    p=paper(tmp_path);p.propose([proposal()],session())
    pkt=build_packet(discovery(),p.state())
    data=decision(pkt,assessments=[dict(symbol='111111',action='CANCEL',proposal_id='b1',reason='교체'),dict(symbol='222222',action='EXIT',reason='없는 보유')])
    before=p.state()
    with pytest.raises(ValueError):p.decide(pkt,data,at('09:02:00'))
    assert p.state()==before


def test_won_rounding_and_fee_allocation_conserve_cash(tmp_path):
    p=paper(tmp_path,Costs('half won assumption','.00005','.00005','.00005','0'))
    p.propose([proposal(quantity=3,time='09:00:00')],session())
    p.advance(market([bar('09:00:00')],time='09:01:00'))
    assert p.report()['cash']=='1969998'  # 1.5 won rounds half up, never binary float
    for i in range(3):
        t=f'09:0{i+1}:00';end=f'09:0{i+2}:00'
        p.propose([proposal(f's{i}',side='SELL',quantity=1,price='10000',time=t)],session())
        p.advance(market([bar(t)],time=end))
    assert p.report()['cash']=='1999992'
    assert p.report()['realized_pnl']=='-8'
    assert p.report()['trading_costs']=='8'


def test_cancellation_clock_cannot_release_reservation_into_the_past(tmp_path):
    p=paper(tmp_path);p.propose([proposal()],session())
    p.advance(market(time='09:10:00'))
    p.cancel('b1',at('09:10:00'),'나중 취소')
    before=p.state()
    with pytest.raises(ValueError):p.advance(market(time='09:05:00'))
    assert p.state()==before
    with pytest.raises(ValueError):p.propose([proposal('past',time='09:09:00')],session())


def test_added_position_reports_average_and_gross_versus_net(tmp_path):
    p=paper(tmp_path,Costs('test costs','.001','.001','.002','0'))
    p.propose([proposal(quantity=2,time='09:00:00')],session())
    p.advance(market([bar('09:00:00')],time='09:01:00'))
    p.propose([proposal('add',quantity=2,price='8000',time='09:01:00')],session())
    p.advance(market([bar('09:01:00',opening='8000',low='7900',high='8100',close='8000')],time='09:02:00',marks={'111111':dict(price='9000',at=at('09:01:00'))}))
    r=p.report();pos=r['positions']['111111']
    assert pos['average_acquisition_price']=='9000'
    assert pos['cost_inclusive_average_price']=='9009'
    assert pos['gross_unrealized_pnl']=='0' and pos['unrealized_pnl']=='-36'
    assert pos['price_return']=='0' and r['gross_total_pnl']=='0'
