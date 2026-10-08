"""Stock health delivery acknowledges the actual send and leaves stdout empty."""
import json
import sys

import pytest

from news_health_stock_route import build_stock_health_sender
from run_news_decision_worker import Notice

saved_path=list(sys.path)
import tokmo_news_classifier_worker as wrapper
sys.path[:]=saved_path


def credential(tmp_path):
    path=tmp_path/'stock.env'
    path.write_text('TELEGRAM_BOT_TOKEN=test-stock-token\nTELEGRAM_CHAT_ID=123\n')
    path.chmod(0o600)
    return path


def test_scheduled_notice_uses_stock_sender_and_no_hermes_stdout(tmp_path,capsys):
    calls=[]; delivered=[]
    sender=build_stock_health_sender(credential(tmp_path),lambda **kw:calls.append(kw) or 42)
    notice=Notice('[뉴스 판단 점검] 문제 종류 변경','DEGRADED')
    assert wrapper.emit_stock_notice(notice,delivered,sender)=='42'
    assert capsys.readouterr().out==''
    assert calls==[{'body_text':str(notice),'bot_token':'test-stock-token','chat_id':'123'}]
    summary=json.dumps(dict(summary=wrapper.SUMMARY,status='DEGRADED',code=2,
                            notice=str(notice),notice_handoff={'status':'OK'}))
    wrapper.validated_summary(summary,2,delivered)


def test_failed_stock_send_never_acknowledges_or_falls_back(tmp_path,capsys):
    def fail(**kw):raise TimeoutError('test')
    delivered=[];sender=build_stock_health_sender(credential(tmp_path),fail)
    with pytest.raises(TimeoutError):
        wrapper.emit_stock_notice(Notice('[뉴스 판단 점검] 오류','DEGRADED'),delivered,sender)
    assert not delivered and capsys.readouterr().out==''


def test_invalid_notice_rejected_before_sender_call():
    with pytest.raises(RuntimeError):
        wrapper.emit_stock_notice('unversioned notice',[],lambda body:pytest.fail(body))


@pytest.mark.parametrize('receipt',[None,'','not-a-receipt'])
def test_missing_provider_receipt_not_acknowledged(tmp_path,receipt):
    delivered=[];sender=build_stock_health_sender(credential(tmp_path),lambda **kw:receipt)
    with pytest.raises(ValueError):
        wrapper.emit_stock_notice(Notice('notice','DEGRADED'),delivered,sender)
    assert not delivered


def test_unsafe_credentials_rejected_before_transport(tmp_path):
    path=credential(tmp_path);path.chmod(0o644)
    with pytest.raises(ValueError):build_stock_health_sender(path,lambda **kw:pytest.fail('send'))
