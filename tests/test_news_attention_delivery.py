"""Delivery length changes must retain stored analysis and exceptional warnings."""

import pytest
from test_news_decision_engine import NOW, Engine, decision, observation, screen

from tokmo.programs.news_decision_engine import render_alert
from tokmo.records.news_decision_store import NewsDecisionStore


def test_ordinary_discovery_sends_headline_and_preserves_full_analysis(tmp_path):
    store = NewsDecisionStore(tmp_path / "news.sqlite3")
    engine = Engine(
        store, screen, lambda request: decision(urgency="NORMAL", portfolio_note="")
    )
    daytime = NOW.replace(hour=12)
    result = engine.tick([observation(at=daytime)], daytime)
    body = result["alerts"][0]["body"]
    assert body.startswith("[뉴스 한줄] 시장 충격")
    assert "보도 확인" in body and "https://example.invalid/a" in body
    assert (
        "전쟁 발생 보도" not in body
        and "의미:" not in body
        and "다음 확인:" not in body
    )
    assert store.events()[0]["decision"]["facts"] == "전쟁 발생 보도"
    store.close()


@pytest.mark.parametrize(
    "extra",
    [
        {"urgency": "IMMEDIATE"},
        {"change": "CORRECTION"},
        {"portfolio_note": "선택한 장기 보유 이유와 연결되는 새 결과입니다."},
    ],
)
def test_urgent_correction_or_personal_implication_keeps_explanation(extra):
    value = decision(urgency="NORMAL", portfolio_note="") | extra
    body = render_alert(value, [observation()], NOW)
    assert "전쟁 발생 보도" in body and "의미:" in body and "다음 확인:" in body
    assert "[뉴스 한줄]" not in body


def test_missing_personal_note_does_not_discard_followup_change_title():
    value = decision(
        urgency="NORMAL",
        portfolio_note=" ",
        change="UPDATE",
        title="대출 지원이 실제 시작됐다는 후속 보도",
    )
    body = render_alert(value, [observation()], NOW, followup=True)
    assert "대출 지원이 실제 시작됐다는 후속 보도" in body and "내 상황:" not in body
