"""Presentation instructions reach existing writers, without a polishing call."""

import json

import pytest
from test_quant_decisions import response, sample_input

from tokmo.programs import quant_decision_analysis as quant

MARKER = "[톡모 알람용 경량 한국어 윤문]"


def test_youtube_repair_preserves_original_prompt_and_explicit_channel_format():
    from pathlib import Path

    folder = Path(__file__).parents[1] / ".ai/evidence/alert-korean-style-20260930"
    original = (folder / "youtube-prompt-after.txt").read_text()
    repaired = (folder / "youtube-prompt-after-repair.txt").read_text()
    assert repaired.startswith(original)
    clarification = repaired[len(original) :]
    assert "고정 제목·행 순서·확신도·추적 결과 표기" in clarification
    assert "윤문 지침보다 우선한다" in clarification
    assert "투자 확신도를 생략하라는 뜻이 아니다" in clarification
    assert repaired.count(MARKER) == 1


@pytest.mark.parametrize(
    "module",
    [
        "tokmo.programs.quant_decision_analysis",
        "tokmo.programs.quant_contextual_analysis",
        "news_decision_models",
        "opportunity_rotation_model",
    ],
)
def test_json_writers_leave_layout_to_renderer_and_preserve_literal_symbols(module):
    from importlib import import_module

    style = import_module(module).ALARM_WRITING_STYLE
    assert "필드 안의 문장은 평문으로 쓴다" in style
    assert "강조용 마크다운이나 장식용 제목·목록 기호를 추가하지 않는다" in style
    assert "제목과 한줄 요약은 한 줄로 쓰고" in style
    assert "직접 인용·고유명사·코드·식별자에 원래 있는 기호는 그대로 보존한다" in style
    assert "굵은 글씨는 필요한 곳에만" not in style
    assert "가운뎃점·줄표·긴 괄호를 과하게 쓰지 않는다." in style


def test_followup_style_is_outside_nested_quant_contract():
    from tokmo.programs.investment_followup import followup_prompt_header

    prompt = followup_prompt_header()
    assert prompt.index(MARKER) > prompt.index("최종 출력 형식:")
    assert prompt.count(MARKER) == 1


def test_quant_writer_receives_style_once_without_an_extra_call():
    calls = []
    from tokmo.values.quant_decisions import parse_snapshot

    prepared = quant.build_report(parse_snapshot(sample_input()))
    packet = prepared["results"][0]["packet"]

    def model(prompt):
        calls.append(prompt)
        return response(packet)

    result, errors = quant.run_analysis(packet, model)
    assert result and errors == [] and len(calls) == 1
    assert calls[0].count(MARKER) == 1
    assert json.loads(calls[0].split("\nINPUT\n")[1])["options"] == packet["options"]
    assert MARKER not in quant.MODEL_INSTRUCTION  # Keep analysis policy text separate.


def test_followup_and_dialogue_receive_style_without_changing_input():
    from tokmo.programs.investment_followup import followup_prompt
    from tokmo.programs.quant_dialogue import prepare_dialogue

    packet = {"trigger": {"reason": "USER_PLAN"}, "raw_user_text": "26,000원·30주"}
    prompt = followup_prompt(packet)
    assert prompt.count(MARKER) == 1
    assert json.loads(prompt.split("\nINPUT\n")[1]) == packet
    assert (
        prepare_dialogue({"report_id": "fixture", "results": []})["instructions"].count(
            MARKER
        )
        == 1
    )


def test_news_writers_receive_style_but_screening_does_not(monkeypatch, tmp_path):
    import news_decision_models as news
    import subscription_json_model

    calls = []

    def infer(prompt, **kwargs):
        calls.append(prompt)
        return "{}", {}

    monkeypatch.setattr(subscription_json_model, "infer", infer)
    for request in (
        {"evidence": []},
        {"evidence": [], "previous": {"change": "UPDATE"}},
        {"evidence": [], "instruction": "correction fixture"},
    ):
        model = news.DecisionModel(
            "codex:gpt-6-astra", "analyze", diagnostic_path=tmp_path / "diagnostic.json"
        )
        before = model.policy_fingerprint, model.incremental_policy_fingerprint
        model(request)
        assert calls[-1].count(MARKER) == 1
        assert news.ALARM_WRITING_STYLE == quant.ALARM_WRITING_STYLE
        with monkeypatch.context() as scoped:
            scoped.setattr(news, "ALARM_WRITING_STYLE", "presentation-only")
            assert (
                model.policy_fingerprint,
                model.incremental_policy_fingerprint,
            ) == before
    news.DecisionModel(
        "codex:gpt-6-astra", "screen", diagnostic_path=tmp_path / "diagnostic.json"
    )({"evidence": []})
    assert len(calls) == 4 and MARKER not in calls[-1]


def test_contextual_card_and_paper_receive_same_style(tmp_path):
    from test_opportunity_rotation_review2 import model_prompt
    from test_quant_card_cycle2 import _ADAPTER_PACK

    from tokmo.programs.quant_contextual_analysis import build_analyzer_request

    system = build_analyzer_request(_ADAPTER_PACK)["system"]
    assert quant.ALARM_WRITING_STYLE in system and system.count(MARKER) == 1
    # Existing fake transport writes a real receipt; no broker or model is called.
    packet = {
        "id": "synthetic-style",
        "candidates": [],
        "portfolio": {},
        "decision_contract": {},
    }
    prompt = model_prompt(packet, tmp_path)
    assert prompt.count(MARKER) == 1
    assert json.loads(prompt.split("\n", 1)[1])["portfolio"] == packet["portfolio"]


def test_current_youtube_prompt_only_appends_style_and_keeps_all_existing_rules():
    from pathlib import Path

    folder = Path(__file__).parents[1] / ".ai/evidence/alert-korean-style-20260930"
    before = (folder / "youtube-prompt-before.txt").read_text()
    after = (folder / "youtube-prompt-after.txt").read_text()
    from hashlib import sha256

    manifest = json.loads((folder / "manifest.json").read_text())
    assert after.startswith(before + "\n")
    assert sha256(after.encode()).hexdigest() == manifest["youtube"]["after_sha256"]
    assert after.count(MARKER) == 1 and "정확히 `[SILENT]`" in after
