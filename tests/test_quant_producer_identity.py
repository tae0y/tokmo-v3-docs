"""Producer integration contracts; fake transport, real parser/capture/store."""
from datetime import datetime, timezone
import hashlib
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from tokmo.programs import quant_input_capture as capture
from tokmo.facts.toss_reads import TossReadOperation as Op

NOW = datetime(2026, 9, 10, tzinfo=timezone.utc)


@pytest.fixture
def producer(monkeypatch):
    expected = os.environ.get('QUANT_PRODUCER_EXPECTED_ROOT')
    if expected:
        assert Path(capture.__file__).resolve().is_relative_to(Path(expected).resolve())
    candidate = os.environ.get('QUANT_PRODUCER_CANDIDATE')
    if candidate:
        spec = importlib.util.spec_from_file_location('producer_identity_candidate', candidate)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        monkeypatch.setattr(capture, 'capture_market_data', module.capture_market_data)
    path = Path(capture.capture_market_data.__code__.co_filename)
    print('capture_source', path, hashlib.sha256(path.read_bytes()).hexdigest())
    return capture


def test_official_english_name_reaches_persisted_capture(producer, tmp_path):
    seen = []
    def transport(request):
        seen.append(request.operation)
        if request.operation is Op.STOCKS:
            value = [{'symbol':'SAMPLE','name':'가상 기업','englishName':'Example Aerospace',
                'market':'NASDAQ','securityType':'STOCK','currency':'USD',
                'isinCode':'SAMPLE','isCommonShare':True,'status':'ACTIVE','sharesOutstanding':'100'}]
        else:
            assert request.operation is Op.STOCK_CANDLES
            assert request.adjusted and request.count == 200 and request.interval == '1d'
            value = {'candles':[], 'nextBefore':None}
        return SimpleNamespace(body=json.dumps({'result':value}).encode())
    reader = producer.ExistingTokenReader(token='synthetic-existing-token', refs=None,
        transport=transport, clock=lambda:NOW)
    path = tmp_path/'capture.json'
    result = producer.capture_job(reader, data={'positions':[], 'instruments':[
        {'symbol':'SAMPLE','market':'US'}]}, path=path, job='candles', now=NOW)
    assert not result['failures'] and result['authentication_issued'] == 0
    assert seen == [Op.STOCKS, Op.STOCK_CANDLES]
    row = json.loads(path.read_text())['instruments'][0]
    assert (row['market'], row['symbol'], row['name']) == ('US','SAMPLE','가상 기업')
    assert row.get('english_name') == 'Example Aerospace'


def test_same_symbol_official_names_remain_market_scoped(producer):
    items = [SimpleNamespace(symbol='SHARED', market=market, name=name,
        english_name=name, currency=currency, security_type='STOCK', shares_outstanding=100)
        for market, name, currency in [('NASDAQ','US Example','USD'),('KOSPI','KR Example','KRW')]]
    def fetch(operation, **kwargs):
        return items if operation is Op.STOCKS else SimpleNamespace(candles=[])
    result = producer.capture_market_data(fetch, instruments=[
        {'symbol':'SHARED','market':'US'},{'symbol':'SHARED','market':'KR'}], now=NOW)
    assert not result['failures']
    assert [(r['market'],r.get('english_name'),r.get('currency'))
        for r in result['instruments']] == [('US','US Example','USD'),('KR','KR Example','KRW')]
