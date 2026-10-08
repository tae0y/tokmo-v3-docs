from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from tokmo.programs.pension_analysis_sources import (
    add_import, analysis_view, compare_statement, new_manifest, statement_import, transaction_import,
)

NOW = '2026-09-08T08:00:00+09:00'
IDENTITIES = {'기존ETF': {'kind':'etf', 'symbol':'000001'},
              '신규ETF': {'kind':'etf', 'symbol':'000002'},
              '현금성자산': {'kind':'other'}}


def source():
    value = new_manifest('pension-a')
    entry = statement_import(b'baseline', [{'종목명':'기존ETF','보유량':100,'매입금액':100000}],
                             IDENTITIES, through='2026-08-28', available_at='2026-08-30T08:00:00+09:00', label='잔고')
    return add_import(value, entry, kind='statement')


def export(rows, *, raw=b'trades', start='2026-08-21', end='2026-09-04', available=NOW):
    return transaction_import(raw, [dict(zip(['일자','거래구분','종목명','수량','단가','금액'], r)) for r in rows],
                              IDENTITIES, complete_from=start, complete_through=end, available_at=available)


def test_lagged_statement_later_trades_and_repeated_import():
    value = source()
    entry = export([('2026/08/28','ETF매수','기존ETF',100,1000,100000),
                    ('2026/09/03','ETF매수','기존ETF',10,2000,20000),
                    ('2026/09/04','ETF매수','신규ETF',5,3000,15000)])
    value = add_import(value, entry, kind='transactions')
    assert add_import(value, entry, kind='transactions') == value
    view = analysis_view(value, as_of=NOW)
    positions = {r['symbol']:r for r in view['positions']}
    assert positions['000001']['quantity'] == '110'
    assert positions['000002']['quantity'] == '5'
    assert view['label'] == '보유 기준 08-28 · 거래 반영 09-04'
    assert len(view['execution_ids']) == 2 and not view['issues']
    assert view['current_quantity_confirmed'] is False
    assert view['transaction_coverage_complete'] is True
    assert value['statements'][0]['positions'][0]['quantity'] == '100'


def test_overlap_uses_latest_export_per_day_without_merging_identical_executions():
    value = add_import(source(), export([('2026/09/03','ETF매수','기존ETF',10,2000,20000)],
                                        end='2026-09-03', available='2026-09-04T08:00:00+09:00'), kind='transactions')
    later = export([('2026/09/03','ETF매수','기존ETF',10,2000,20000),
                    ('2026/09/03','ETF매수','기존ETF',10,2000,20000)], raw=b'newer')
    view = analysis_view(add_import(value, later, kind='transactions'), as_of=NOW)
    assert view['positions'][0]['quantity'] == '120'
    assert len(view['execution_ids']) == 2


def test_empty_export_proves_no_trades_only_for_its_query_range():
    value = add_import(source(), export([], start='2026-09-02'), kind='transactions')
    view = analysis_view(value, as_of=NOW)
    assert view['positions'][0]['quantity_status'] == 'UNRESOLVED'
    assert view['issues'][0]['dates'] == ['2026-08-29','2026-08-30','2026-08-31','2026-09-01']
    complete = analysis_view(add_import(source(), export([]), kind='transactions'), as_of=NOW)
    assert not complete['issues'] and complete['positions'][0]['quantity_status'] == 'CALCULATED'


def test_correction_keeps_other_instrument_usable_then_explicit_resolution():
    entry = export([('2026/09/03','ETF매도','기존ETF',10,2000,20000),
                    ('2026/09/04','매도정정*','기존ETF',20,2000,40000),
                    ('2026/09/04','ETF매수','신규ETF',5,3000,15000)])
    value = add_import(source(), entry, kind='transactions')
    view = analysis_view(value, as_of=NOW)
    positions = {r['symbol']:r for r in view['positions']}
    assert positions['000001']['quantity_status'] == 'UNRESOLVED'
    assert positions['000002']['quantity'] == '5'
    value['resolutions'].append(dict(row_id=entry['rows'][1]['id'], action='replace', side='SELL',
                                     replaces=entry['rows'][0]['id'], reason='원거래와 정정 확인', recorded_at=NOW))
    fixed = analysis_view(value, as_of=NOW)
    assert not fixed['issues'] and fixed['positions'][0]['quantity'] == '80'


def test_later_statement_compares_at_its_cutoff_and_rebase_keeps_later_trade_once():
    value = add_import(source(), export([('2026/09/03','ETF매수','기존ETF',10,2000,20000),
                                         ('2026/09/04','ETF매수','기존ETF',5,2000,10000)]), kind='transactions')
    statement = statement_import(b'later statement', [{'종목명':'기존ETF','보유량':110,'매입금액':120000}],
                                 IDENTITIES, through='2026-09-03', available_at=NOW, label='새 잔고')
    assert compare_statement(value, statement, as_of=NOW)['status'] == 'MATCH'
    rebased = analysis_view(add_import(value, statement, kind='statement'), as_of=NOW)
    assert rebased['positions'][0]['quantity'] == '115'
    assert len(rebased['execution_ids']) == 1
    wrong = deepcopy(statement); wrong['positions'][0]['quantity'] = '109'
    comparison = compare_statement(value, wrong, as_of=NOW)
    assert comparison['status'] == 'MISMATCH' and comparison['differences'][0]['calculated'] == '110'


def test_late_received_trade_file_is_not_available_to_historical_review():
    value = add_import(source(), export([('2026/09/03','ETF매수','기존ETF',10,2000,20000)]), kind='transactions')
    before = analysis_view(value, as_of='2026-09-07T08:00:00+09:00')
    assert before['positions'][0]['quantity'] == '100'
    assert before['verified_through'].startswith('2026-08-28')
    assert before['positions'][0]['quantity_status'] == 'DATED'


def test_workbook_cli_preserves_source_after_attachment_disappears(tmp_path, capsys):
    import openpyxl
    from prepare_pension_analysis import main
    identities=tmp_path/'identities.json'; identities.write_text(json.dumps(IDENTITIES))
    workbook=tmp_path/'statement.xlsx'
    wb=openpyxl.Workbook();ws=wb.active
    ws.append(['종목명','보유량','매입금액','평가금액','운용비율'])
    ws.append(['기존ETF',100,100000,120000,1])
    wb.save(workbook)
    state=tmp_path/'analysis.json'
    args=['statement','--state',str(state),'--account','pension-a','--workbook',str(workbook),
          '--identities',str(identities),'--through','2026-08-28','--at',NOW]
    assert main(args)==0
    capsys.readouterr()
    manifest=json.loads(state.read_text()); source_path=Path(manifest['statements'][0]['source_path'])
    assert source_path.read_bytes()==workbook.read_bytes()
    assert source_path.stat().st_mode & 0o777 == 0o600
    assert main(args)==0 and len(json.loads(state.read_text())['statements'])==1
    workbook.unlink()
    assert main(['preview','--state',str(state),'--at',NOW])==0
    assert source_path.exists()


def test_workbook_comparison_survives_reimport_and_preview(tmp_path, capsys):
    import openpyxl
    from prepare_pension_analysis import main
    identities=tmp_path/'identities.json';identities.write_text(json.dumps(IDENTITIES))
    value=add_import(source(),export([]),kind='transactions')
    state=tmp_path/'analysis.json';state.write_text(json.dumps(value))
    workbook=tmp_path/'new.xlsx';wb=openpyxl.Workbook();ws=wb.active
    ws.append(['종목명','보유량','매입금액']);ws.append(['기존ETF',95,95000]);wb.save(workbook)
    args=['statement','--state',str(state),'--workbook',str(workbook),'--identities',str(identities),
          '--through','2026-09-03','--at',NOW]
    assert main(args)==0
    latest=json.loads(state.read_text())['statements'][-1]
    assert latest['comparison']['status']=='MISMATCH'
    assert latest['comparison']['differences'][0]['calculated']=='100'
    assert main(args)==0
    assert main(['preview','--state',str(state),'--at',NOW])==0
    assert analysis_view(json.loads(state.read_text()),as_of=NOW)['comparison']==latest['comparison']


def test_empty_registered_pension_account_can_receive_analysis_holdings():
    from tokmo.programs.pension_analysis_view import apply_view
    data=dict(positions=[],instruments=[],as_of=NOW,accounts=[dict(account='pension-a',scope='retirement')])
    result=apply_view(data,analysis_view(source(),as_of=NOW))
    assert result['positions'][0]['quantity']=='100'


def test_analysis_input_uses_new_pension_inventory_and_dated_close_when_quote_missing(tmp_path,monkeypatch):
    from types import SimpleNamespace
    import tokmo.programs.quant_decision_inputs as inputs
    value=source();value['account']='pension-excel-test'
    value=add_import(value,export([('2026/09/04','ETF매수','신규ETF',5,3000,15000)]),kind='transactions')
    manifest=tmp_path/'analysis.json';manifest.write_text(json.dumps(value))
    state=SimpleNamespace(captured_at=datetime.fromisoformat(NOW),
                          accounts=[SimpleNamespace(account_scope='pension-excel-test',holdings=[])])
    monkeypatch.setattr(inputs.CurrentHoldingsStore,'read',lambda self:state)
    supplement={'instruments':[dict(market='KR',symbol='000002',bars=[
        dict(date='2026-09-04',available_at='2026-09-04T15:30:00+09:00',close='3200',adjusted=True),
        dict(date='2026-09-08',available_at='2026-09-08T15:30:00+09:00',close='9999',adjusted=True)])]}
    result=inputs.load_local_snapshot(tmp_path,tmp_path,as_of=NOW,include_context=False,pension_analysis=manifest,supplement=supplement,historical_pension=True)
    row=next(i for i in result['instruments'] if i['symbol']=='000002')
    assert row['price']=='3200' and row['price_at']=='2026-09-04T15:30:00+09:00'
    assert '종가' in row['sources'][-1]
    assert next(p for p in result['positions'] if p['key']=='KR:000002')['quantity']=='5'
    assert result['pension_analysis_view']['verified_through'].startswith('2026-09-04')


def test_unresolved_quantity_does_not_poison_other_sizing_or_invent_average_cost():
    from tokmo.values.quant_decisions import parse_snapshot
    from tokmo.programs.quant_decision_calculations import portfolio_totals, size_options
    from tokmo.programs.quant_decision_analysis import make_packet
    data={'schema_version':1,'as_of':NOW,'instruments':[
        dict(market='KR',symbol='000001',currency='KRW',name='기존ETF',kind='etf',price='2000',price_at=NOW),
        dict(market='KR',symbol='000002',currency='KRW',name='신규ETF',kind='etf',price='3000',price_at=NOW)],
        'positions':[
            dict(account='pension-a',scope='retirement',key='KR:000001',quantity='100',average_cost='1000',observed_at=NOW,quantity_status='UNRESOLVED'),
            dict(account='pension-a',scope='retirement',key='KR:000002',quantity='20',average_cost=None,observed_at=NOW,quantity_status='CALCULATED')],
        'cash':[]}
    snapshot=parse_snapshot(data)
    total=portfolio_totals(snapshot)['retirement:KRW']
    assert total['equity']==60000 and total['missing_quantities']==['KR:000001']
    assert not size_options(snapshot,snapshot.instruments[0],'pension-a','retirement')
    options=size_options(snapshot,snapshot.instruments[1],'pension-a','retirement')
    assert {o['quantity'] for o in options}=={5,10,20}
    assert all(o['requires_broker_confirmation'] and not o['entry_ready'] for o in options)
    assert next(o for o in options if o['quantity']==5)['remaining_portion_of_holding']==__import__('decimal').Decimal('0.75')
    packet=make_packet(snapshot,snapshot.instruments[1],'pension-a','retirement')
    assert packet['holdings'][0]['unrealized_return'] is None
    assert packet['portfolio_holdings'][0]['market_value'] is None
    assert packet['unquantified_actions']=={}
