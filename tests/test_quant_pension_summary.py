import pytest
from test_quant_decisions import sample_input
from tokmo.programs.quant_pension_summary import summarize_rows
from tokmo.values.quant_decisions import parse_snapshot
from tokmo.programs.quant_decision_calculations import portfolio_totals,size_options


def summary():
    rows=[{'name':'시장ETF','evaluation_amount':'1000000'},
          {'name':'회사채','evaluation_amount':'2000000'},{'name':'현금성 상품','evaluation_amount':'300000'}]
    classification={'시장ETF':'etf','회사채':'bond','현금성 상품':'cash_like'}
    return summarize_rows(rows,classification,account='pension-a',source_as_of='2026-08-13T23:59:59+09:00',source='기존 계좌상태 엑셀')


def test_non_etf_totals_are_not_cash_buying_power_and_personal_assets_unchanged():
    d=sample_input();before=portfolio_totals(parse_snapshot(d));d.update(summary())
    d['positions'].append(dict(d['positions'][0],account='pension-a',scope='retirement'))
    s=parse_snapshot(d);totals=portfolio_totals(s)
    assert totals['personal:KRW']==before['personal:KRW']
    assert totals['retirement:KRW']['other_assets']==2300000
    assert totals['retirement:KRW']['cash']==0 and not totals['retirement:KRW']['complete']
    assert '2026-08-13' in totals['retirement:KRW']['summary_dates'][0]
    assert not any(o['action']=='BUY' for o in size_options(s,s.instruments[0],'pension-a','retirement'))


def test_pension_summary_requires_exact_product_classification():
    with pytest.raises(ValueError,match='classification'):
        summarize_rows([{'name':'미확인 상품','evaluation_amount':'100'}],{},account='pension-a',
                       source_as_of='2026-08-13T23:59:59+09:00',source='엑셀')
