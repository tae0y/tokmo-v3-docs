"""Repair acceptance, synthetic prices only; no provider/model calls."""
from decimal import Decimal

from test_opportunity_rotation import at, bar, market, paper, proposal, session
from tokmo.programs.opportunity_rotation import Paper


def test_long_drawdown_survives_readback_and_next_advance(tmp_path):
    p = paper(tmp_path)
    p.propose([proposal(time='09:00:00')], session())
    p.advance(market([bar('09:00:00')], time='09:01:00', marks={'111111': {'price':'12000','at':at('09:00:00')}}))
    p.advance(market(time='09:02:00', marks={'111111': {'price':'11000','at':at('09:01:00')}}))
    result = Paper(tmp_path).advance(market(time='09:03:00', marks={'111111': {'price':'11000','at':at('09:02:00')}}))
    assert result['nav'] == '2010000'
    assert abs(Decimal(result['observed_max_drawdown']) - Decimal(10000)/2020000) < Decimal('1e-12')
    assert len(result['observed_max_drawdown']) <= 14


def test_late_end_bar_reconciles_before_cancel_and_close(tmp_path):
    import pytest
    p = paper(tmp_path)
    p.propose([proposal(price='400000',quantity=1,time='09:00:00')], session())
    def captured(rows, observed):
        m = market(time=observed)
        m['bars'] = rows
        m['coverage'][0]['complete'] = False
        return m
    # Exact provider END timestamps, intentionally missing 09:01.
    hit = dict(bar('09:02:00',low='399000',high='401000',opening='400000',close='400000'), at=at('09:02:00'))
    p.advance(captured([hit], '09:03:00'))
    assert p.report()['reserved_cash'] == '400000'
    with pytest.raises(ValueError, match='reconcil'):
        p.cancel('b1', at('09:03:00'), '자료 확인 전 취소 금지')
    first = dict(hit, at=at('09:01:00'), low='410000',high='410000',open='410000',close='410000')
    result = p.advance(captured([first], '15:31:00'))
    assert result['cash'] == '1600000'
    fill = p.state()['fills'][0]
    assert fill['at'] == at('09:02:00') and fill['observed_at'] == at('15:31:00')
    assert Paper(tmp_path).advance(captured([first], '15:31:00'))['cash'] == '1600000'


def test_first_completed_end_bar_benchmark_and_official_close(tmp_path):
    p = paper(tmp_path)
    first = dict(bar('09:01:00',symbol='069500',low='50000',high='50000',opening='50000',close='50000'),at=at('09:01:00'))
    m = market(time='09:01:00');m['bars']=[first]
    assert p.advance(m)['benchmark']['open_price'] == '50000'
    p.propose([proposal(time='15:29:00',review_at=at('16:00:00'))],session())
    last = dict(bar('15:30:00'),at=at('15:30:00'))
    m=market(time='15:30:00');m['bars']=[last]
    p.advance(m)
    assert p.state()['proposals']['b1']['status'] == 'FILLED'


def test_standing_protection_next_bar_stop_first_and_one_quantity(tmp_path):
    import pytest
    p = paper(tmp_path)
    p.propose([proposal(time='09:00:00', protection={'target_price':'10300','stop_price':'9500'})],session())
    # Both exits touched on BUY minute: none allowed until next bar.
    p.advance(market([bar('09:00:00',low='9000',high='11000')],time='09:01:00'))
    assert len(p.state()['fills']) == 1
    assert p.report()['pending'][0]['quantity'] == 10
    with pytest.raises(ValueError):
        p.propose([proposal('oversell',side='SELL',time='09:01:00',quantity=1)],session())
    # Both target and stop in next minute: adverse stop wins, including gap.
    p.advance(market([bar('09:01:00',low='8900',high='11000',opening='9000')],time='09:02:00'))
    assert p.report()['positions'] == {}
    assert p.report()['cash'] == '1990000'
    assert p.state()['fills'][-1]['exit_condition'] == 'STOP'
    p.advance(market(time='09:03:00'))
    assert len(p.state()['fills']) == 2


def test_target_and_late_protection_replay(tmp_path):
    p=paper(tmp_path)
    p.propose([proposal(time='09:00:00',protection={'target_price':'10300','stop_price':'9500'})],session())
    m=market([bar('09:00:00',low='9900',high='10400'),bar('09:01:00',low='9900',high='10400')],time='15:31:00')
    p.advance(m)
    assert [f['side'] for f in p.state()['fills']] == ['BUY','SELL']
    assert p.report()['cash']=='2003000'
    assert p.state()['fills'][-1]['exit_condition']=='TARGET'
    assert Paper(tmp_path).advance(m)['cash']=='2003000'


def test_close_settlement_waits_for_marks_and_complete_pending_window(tmp_path):
    p=paper(tmp_path)
    p.propose([proposal(time='15:29:00',review_at=at('16:00:00'))],session())
    m=market(time='15:31:00');m['bars']=[];m['coverage'][0]['complete']=True
    r=p.advance(m)
    assert r['reserved_cash']=='100000'
    assert r['settlement']['status']=='PENDING_DATA'
    m=market([bar('15:29:00',low='9900')],time='15:32:00')
    m['valuation_at']=at('15:30:00');m['marks']={'111111':{'price':'10000','at':at('15:30:00')}}
    r=p.advance(m)
    assert r['settlement']['status']=='PENDING_DATA'  # benchmark close/open absent
    assert r['cash']=='1900000'
