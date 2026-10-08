from datetime import datetime, timedelta
from types import SimpleNamespace as NS
from zoneinfo import ZoneInfo
import pytest

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.programs.quant_decision_inputs import capture_market_data
from tokmo.values._primitives import MISSING

NOW = datetime.fromisoformat('2026-09-26T11:00:00+09:00')
ZONE = ZoneInfo('America/New_York')
END = datetime.fromisoformat('2026-09-25T00:00:00-04:00').astimezone(ZONE)


def source(days=900):
    candles = [NS(timestamp=END-timedelta(days=n), open_price='100', high_price='110',
                  low_price='90', close_price='105', volume='1000') for n in range(days)
               if (END-timedelta(days=n)).weekday() < 5]
    seen = []
    def fetch(op, **kw):
        if op is Op.STOCKS:
            return []
        seen.append(kw)
        eligible = [c for c in candles if c.timestamp <= kw['before']]
        page = eligible[:kw['count']]
        cursor = page[-1].timestamp-timedelta(days=1) if len(eligible)>len(page) else None
        return NS(candles=page, next_before=cursor)
    return fetch, seen


def capture(fetch, **kwargs):
    return capture_market_data(fetch, instruments=[dict(symbol='SAMPLE', market='US')],
                               now=NOW, completed_session='2026-09-25', **kwargs)


def test_default_calendar_year_paginates_and_reports_actual_coverage():
    fetch, seen = source()
    result = capture(fetch)
    row = result['instruments'][0]
    assert not result['failures']
    assert len(seen) == 2
    assert row['bars'][0]['date'] == '2025-09-25'
    assert row['bars'][-1]['date'] == '2026-09-25'
    assert len({b['date'] for b in row['bars']}) == len(row['bars']) > 200
    coverage = row['metrics']['price_history']
    assert coverage['requested_start'] == '2025-09-25'
    assert coverage['stop_reason'] == 'TARGET_REACHED'
    assert coverage['actual_count'] == len(row['bars'])
    fetch, seen = source()
    kr = capture_market_data(fetch, instruments=[dict(symbol='000001', market='KR')],
        now=NOW, completed_session='2026-09-25')['instruments'][0]
    assert len(seen) == 2 and len(kr['bars']) == len(row['bars'])
    assert kr['bars'][-1]['available_at'] == '2026-09-25T15:30:00+09:00'


def test_short_sessions_and_long_calendar_range_are_choices_not_hard_year_limit():
    fetch, seen = source()
    row = capture(fetch, history_sessions=60, history_reason='단기 조정 확인')['instruments'][0]
    assert len(row['bars']) == 60 and len(seen) == 1 and seen[0]['count'] == 60
    assert row['metrics']['price_history']['reason'] == '단기 조정 확인'
    fetch, seen = source()
    row = capture(fetch, history_start='2024-09-25', history_reason='장기 지지 확인')['instruments'][0]
    assert row['bars'][0]['date'] == '2024-09-25' and len(seen) == 3
    fetch, seen = source()
    row = capture(fetch, history_sessions=240)['instruments'][0]
    assert len(row['bars']) == 240 and [x['count'] for x in seen] == [200, 40]


def test_second_page_failure_retains_partial_bars_without_claiming_full_year():
    fetch, seen = source()
    def failing(op, **kw):
        if op is Op.STOCK_CANDLES and seen: raise RuntimeError('fixture_failure')
        return fetch(op, **kw)
    result = capture(failing)
    assert result['failures']
    row = result['instruments'][0]
    assert len(row['bars']) == 200
    assert row['metrics']['price_history']['status'] == 'PARTIAL'
    assert row['metrics']['price_history']['stop_reason'] == 'FETCH_FAILED'


@pytest.mark.parametrize('terminal', [None, MISSING, 'bad-cursor', 'repeat'])
def test_short_provider_history_is_reported_not_as_a_full_year(terminal):
    fetch, seen = source(days=15)
    def ending(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES:
            page.next_before = kw['before'] if terminal == 'repeat' else terminal
            if terminal is None or terminal is MISSING:
                import json
                from tokmo.facts.toss_reads import build_read_request, parse_read_response
                value = dict(candles=[dict(timestamp=c.timestamp.isoformat(), openPrice=c.open_price,
                    highPrice=c.high_price, lowPrice=c.low_price, closePrice=c.close_price,
                    volume=c.volume, currency='USD') for c in page.candles])
                if terminal is None: value['nextBefore'] = None
                parsed = parse_read_response(req=build_read_request(op, token='fixture-only', **kw),
                    body=json.dumps(dict(result=value)).encode(), fetched_at=NOW)
                assert parsed.code == 'success' and parsed.value.next_before is terminal
                page = parsed.value
        return page
    result = capture(ending)
    row = result['instruments'][0]
    assert row['metrics']['price_history']['status'] == 'PARTIAL'
    assert row['metrics']['price_history']['stop_reason'] == (
        'SOURCE_EXHAUSTED' if terminal is None or terminal is MISSING else 'FETCH_FAILED')
    assert bool(result['failures']) == (terminal is not None and terminal is not MISSING)
    assert len(seen) == 1
    if terminal is not None and terminal is not MISSING:
        complete = capture(ending, history_sessions=1)
        assert not complete['failures']
        assert complete['instruments'][0]['metrics']['price_history']['stop_reason'] == 'TARGET_REACHED'


def test_overlapping_page_uses_last_received_bar():
    fetch, seen = source()
    def conflict(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES and len(seen) == 2:
            page.candles.append(NS(timestamp=END, open_price='100', high_price='110',
                                   low_price='90', close_price='106', volume='1000'))
        return page
    result = capture(conflict)
    assert not result['failures']
    assert result['instruments'][0]['bars'][-1]['close'] == '106'
    assert len({b['date'] for b in result['instruments'][0]['bars']}) == len(result['instruments'][0]['bars'])


def test_empty_page_ends_capture_without_spending_remaining_budget():
    calls = []
    def empty(op, **kw):
        if op is Op.STOCKS: return []
        calls.append(kw)
        return NS(candles=[], next_before=kw['before']-timedelta(days=1))
    result = capture(empty)
    assert len(calls) == 1 and not result['failures']
    history = result['instruments'][0]['metrics']['price_history']
    assert history['status'] == 'PARTIAL' and history['stop_reason'] == 'SOURCE_EXHAUSTED'
    assert history['actual_count'] == 0


def test_repeated_cursor_stops_and_excludes_incomplete_future_bars():
    fetch, seen = source()
    def stuck(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES: page.next_before = kw['before']
        return page
    result = capture(stuck)
    assert len(seen) == 1 and result['failures'][0]['reason'] == 'candle_cursor_not_advancing'
    fetch, seen = source()
    def future(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES and len(seen) == 1:
            page.candles[0] = NS(timestamp=END+timedelta(days=4), open_price='100',
                high_price='110', low_price='90', close_price='105', volume='1000')
        return page
    row = capture(future, history_sessions=60)['instruments'][0]
    assert len(row['bars']) == 60 and all(b['date'] <= '2026-09-25' for b in row['bars'])



def test_existing_reader_budget_caps_pagination_and_returns_partial():
    from tokmo.programs.quant_input_capture import ExistingTokenReader
    import json
    def transport(request):
        if request.operation is Op.STOCKS: value=[]
        else:
            value={'candles':[dict(timestamp=(END-timedelta(days=n)).isoformat(), openPrice='100',
                highPrice='110',lowPrice='90',closePrice='105',volume='1000',currency='USD')
                for n in range(200)],'nextBefore':(END-timedelta(days=200)).isoformat()}
        return NS(body=json.dumps({'result':value}).encode())
    reader = ExistingTokenReader(token='test-memory-only', refs=None, transport=transport,
                                 clock=lambda: NOW, max_calls=2)
    result = capture(reader)
    assert len(reader.calls) == 2 and result['failures']
    assert result['instruments'][0]['metrics']['price_history']['status'] == 'PARTIAL'
    assert result['instruments'][0]['metrics']['price_history']['failure_reason'] == 'capture_call_budget'


def test_live_cli_shape_uses_same_capture_job(tmp_path, monkeypatch):
    import io, json
    import capture_quant_inputs as cli
    fetch, seen = source()
    monkeypatch.setattr(cli, 'load_local_snapshot', lambda *a, **kw: dict(positions=[],instruments=[]))
    monkeypatch.setattr(cli, 'ExistingTokenReader', lambda **kw: fetch)
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO('test-memory-only\n'))
    output=tmp_path/'captured.json'
    assert cli.main(['--token-stdin','--job','candles','--symbol','US:SAMPLE',
        '--history-sessions','60','--history-reason','短期','--output',str(output)]) == 0
    row=json.loads(output.read_text())['instruments'][0]
    assert row['symbol']=='SAMPLE' and len(row['bars'])==60
    assert row['metrics']['price_history']['reason']=='短期'
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO('test-memory-only\n'))
    assert cli.main(['--token-stdin','--job','candles','--symbol','US:SAMPLE',
        '--history-start','2024-09-25','--output',str(output)]) == 0
    assert json.loads(output.read_text())['instruments'][0]['bars'][0]['date']=='2024-09-25'


@pytest.mark.parametrize('budget,expected', [(4, 0), (2, 2)])
def test_cli_multiple_symbols_are_complete_or_explicitly_partial(tmp_path, monkeypatch, capsys, budget, expected):
    import io, json
    import capture_quant_inputs as cli
    from test_quant_automatic_history import setup
    _, _, calls, _ = setup(tmp_path, monkeypatch, ('AAA', 'BBB'))
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport
    monkeypatch.setattr(cli, 'TossReadsLiveTransport', TossReadsLiveTransport)
    monkeypatch.setattr(cli, 'load_local_snapshot', lambda *a, **kw: dict(positions=[], instruments=[]))
    monkeypatch.setattr(cli.sys, 'stdin', io.StringIO('fixture-only\n'))
    monkeypatch.setattr(cli, 'datetime', NS(now=lambda *a: NOW))
    output = tmp_path/'captured.json'
    code = cli.main(['--token-stdin', '--job', 'candles', '--symbol', 'US:AAA', '--symbol', 'US:BBB',
        '--history-sessions', '60', '--max-calls', str(budget), '--output', str(output)])
    assert code == expected and len(calls) <= budget
    result = json.loads(capsys.readouterr().out)
    assert result['requested_symbols'] == ['US:AAA', 'US:BBB']
    assert result['incomplete_symbols'] == ([] if expected == 0 else ['US:BBB'])
    rows = json.loads(output.read_text())['instruments']
    assert {r['symbol'] for r in rows if len(r['bars']) == 60} == ({'AAA', 'BBB'} if expected == 0 else {'AAA'})
    before = len(calls)
    monkeypatch.setattr(cli, 'load_local_snapshot', lambda *a, **kw: pytest.fail('invalid CLI reached storage'))
    for invalid in (['--history-sessions', '0'], ['--history-start', '2025/09/25'], ['--batch', '1']):
        with pytest.raises(SystemExit) as error:
            cli.main(['--token-stdin', '--job', 'candles', '--symbol', 'US:AAA', '--symbol', 'US:BBB',
                      '--output', str(output), *invalid])
        assert error.value.code == 2 and len(calls) == before


@pytest.mark.parametrize('span', [{}, dict(history_sessions=60), dict(history_start='2024-09-25')])
def test_selected_history_reaches_local_snapshot_parse_and_analysis_packet(tmp_path, monkeypatch, span):
    import json
    from decimal import Decimal
    from tokmo.programs import quant_decision_inputs as inputs
    from tokmo.programs.quant_input_capture import capture_job
    from tokmo.values.quant_decisions import parse_snapshot
    from tokmo.programs.quant_decision_analysis import make_packet
    monkeypatch.setattr(inputs.CurrentHoldingsStore, 'read', lambda self: NS(captured_at=NOW, accounts=[]))
    data = dict(positions=[], instruments=[dict(market='US', symbol='SAMPLE')])
    path = tmp_path/'cache.json'
    fetch, _ = source()
    def history(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES:
            for candle in page.candles:
                if 300 <= (END-candle.timestamp).days <= 320:
                    candle.close_price='200'; candle.high_price='210'
        return page
    if span.get('history_sessions'):
        capture_job(history, data=data, path=path, job='candles', now=NOW, history_start='2024-09-25')
    result = capture_job(history, data=data, path=path, job='candles', now=NOW, history_reason='기간 검증', **span)
    assert not result['failures']
    raw = inputs.load_local_snapshot(tmp_path, tmp_path, as_of=NOW, supplement=path, include_context=False)
    snapshot = parse_snapshot(raw); instrument = snapshot.instruments[0]
    expected = json.loads(path.read_text())['instruments'][0]['metrics']['price_history']
    assert len(instrument.bars) == expected['actual_count']
    assert instrument.metrics['price_history']['reason'] == '기간 검증'
    packet = make_packet(snapshot, instrument, 'personal-a', 'personal')
    assert packet['metrics']['price_history']['requested_sessions'] == span.get('history_sessions')
    summary = packet['price_context']['history']
    assert summary['count'] == len(instrument.bars) and summary['from_date'] == expected['actual_start']
    assert summary['to_date'] == expected['actual_end']
    assert Decimal(summary['max_drawdown']) == (Decimal(0) if span.get('history_sessions') else Decimal('-.475'))
    assert packet['price_context']['moving_averages']['20']['value'] == '105'
    assert packet['features']['max_drawdown'] == '0.000000'  # existing 200-session indicator window
    assert 'bars' not in packet and len(json.dumps(summary)) < 2000
    raw['instruments'][0]['metrics']['price_history'].pop('available_at')
    legacy = parse_snapshot(raw).instruments[0]
    assert len(legacy.bars) == expected['actual_count'] and legacy.metrics['price_history']['reason'] == '기간 검증'
