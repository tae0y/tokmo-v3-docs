import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

HOOK = Path.home() / ".hermes" / "hooks" / "tokmo_finance_source_guard.py"


def call(tool, args, *, home, session=None):
    payload = {
        "hook_event_name": "pre_tool_call",
        "tool_name": tool,
        "tool_input": args,
        "session_id": session or uuid.uuid4().hex,
    }
    env = os.environ.copy()
    env["HOME"] = str(home)
    cp = subprocess.run(
        [sys.executable, str(HOOK)], input=json.dumps(payload), text=True,
        capture_output=True, env=env, check=True,
    )
    return json.loads(cp.stdout) if cp.stdout.strip() else None


def test_first_finance_research_search_is_broadly_blocked(tmp_path):
    result = call(
        "web_search",
        {"query": "005930 OpenDART CFS XBRL 재무제표 source 조사"},
        home=tmp_path,
    )
    assert result["action"] == "block"
    assert "TOKMO_INTERNAL_FIRST" in result["message"]
    assert "internal sources" in result["message"]


def test_same_session_same_query_second_attempt_is_automatically_allowed(tmp_path):
    session = "same-session"
    args = {"query": "KOSDAQ 금융 비금융 sector classification official source"}
    assert call("web_search", args, home=tmp_path, session=session)["action"] == "block"
    assert call("web_search", args, home=tmp_path, session=session) is None
    assert call("web_search", args, home=tmp_path, session=session) is None


def test_second_attempt_requires_same_session_and_same_scope(tmp_path):
    session = "scope-session"
    first = {"query": "OpenDART API 공식 개발가이드"}
    other = {"query": "KOSDAQ 공식 분류 source"}
    assert call("web_search", first, home=tmp_path, session=session)["action"] == "block"
    assert call("web_search", other, home=tmp_path, session=session)["action"] == "block"
    assert call("web_search", first, home=tmp_path, session="different-session")["action"] == "block"


def test_current_quote_also_uses_one_internal_first_gate_only(tmp_path):
    session = "quote-session"
    args = {"query": "005930 현재가"}
    first = call("web_search", args, home=tmp_path, session=session)
    assert first["action"] == "block"
    assert "Toss" in first["message"]
    assert call("web_search", args, home=tmp_path, session=session) is None


def test_holdings_also_uses_one_internal_first_gate_only(tmp_path):
    session = "holdings-session"
    args = {"query": "삼성전자 보유수량과 평단"}
    first = call("web_search", args, home=tmp_path, session=session)
    assert first["action"] == "block"
    assert "Toss-origin account canonical store" in first["message"]
    assert call("web_search", args, home=tmp_path, session=session) is None


def test_first_gate_message_says_external_retry_is_allowed_without_receipt(tmp_path):
    result = call("web_search", {"query": "현재 USD/KRW 환율"}, home=tmp_path)
    message = result["message"]
    assert "retry the same external request" in message
    assert "without another block" in message
    assert "receipt" not in message.lower()
    assert "explicit user approval" not in message
    assert "local_checked" in message
    assert "unresolved_gap" in message
    assert "why_external_can_change_answer" in message


def test_non_finance_search_is_allowed_immediately(tmp_path):
    assert call("web_search", {"query": "Python dataclass documentation"}, home=tmp_path) is None


def test_browser_finance_navigation_uses_same_one_time_gate(tmp_path):
    session = "browser-session"
    args = {"url": "https://example.com/finance?symbol=005930"}
    assert call("computer_use", args, home=tmp_path, session=session)["action"] == "block"
    assert call("computer_use", args, home=tmp_path, session=session) is None


def test_direct_third_party_quote_terminal_remains_hard_blocked(tmp_path):
    session = "terminal-session"
    args = {"command": "curl https://polling.finance.naver.com/api/realtime/domestic/stock/091230"}
    assert call("terminal", args, home=tmp_path, session=session)["action"] == "block"
    assert call("terminal", args, home=tmp_path, session=session)["action"] == "block"


def test_official_toss_terminal_is_allowed(tmp_path):
    args = {"command": "python query.py # TossReadsLiveTransport openapi.tossinvest.com"}
    assert call("terminal", args, home=tmp_path) is None


def test_malformed_non_dict_input_is_ignored(tmp_path):
    assert call("web_search", {}, home=tmp_path) is None
