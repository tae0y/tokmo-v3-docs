import hashlib
import importlib.util
from pathlib import Path

import pytest


def module(name):
    path = Path(__file__).resolve().parents[1] / "tools" / (name + ".py")
    spec = importlib.util.spec_from_file_location(name, path)
    value = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(value)
    return value


def packet():
    return {"recipient_sha256": hashlib.sha256(b"test-chat").hexdigest(),
            "messages": [{"id": "first", "body": "[재현테스트] 합성 자료"}, {"id": "second", "body": "[재현테스트] 후속 합성 자료"}]}


def test_test_sender_refuses_wrong_destination_before_receipt_or_transport(tmp_path):
    tool = module("send_news_decision_test")
    receipt = tmp_path / "receipt.json"
    with pytest.raises(ValueError, match="Recipient"):
        tool.deliver(packet(), receipt, chat_id="wrong", sender=lambda text: pytest.fail("Unexpected send"))
    assert not receipt.exists()


def test_test_sender_timeout_does_not_send_later_messages_or_retry(tmp_path):
    tool = module("send_news_decision_test")
    calls = []
    def send(text):
        calls.append(text)
        raise TimeoutError("provider ambiguity")
    receipt = tmp_path / "receipt.json"
    result = tool.deliver(packet(), receipt, chat_id="test-chat", sender=send)
    assert result["status"] == "UNKNOWN" and len(calls) == 1
    with pytest.raises(FileExistsError):
        tool.deliver(packet(), receipt, chat_id="test-chat", sender=send)
    assert len(calls) == 1
