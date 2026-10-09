"""A local correction must not look like a new market warning or hide real changes."""

import news_decision_models as models
import pytest
from test_news_decision_engine import NOW, decision, observation

from tokmo.programs.news_decision_engine import render_alert, validate_decision


def correction(**extra):
    return (
        decision(
            change="CORRECTION",
            urgency="NORMAL",
            importance="HIGH",
            title="정정: PLUS 고배당주를 반도체 ETF로 잘못 연결했어",
            facts="이전 안내에서 PLUS 고배당주를 반도체 호재와 연결한 설명을 철회해.",
            interpretation="당시 급등 보도를 번복하는 내용은 아니며, 이번 정정에 추가된 시장 변화 소식은 없어.",
            why_now="잘못된 ETF 연결을 바로잡는다.",
            portfolio_note="전체 계좌의 상세 설명",
            next_check="실적과 원문 자료의 다음 확인 조건",
            correction_notice="EXPLANATION_ONLY",
        )
        | extra
    )


def test_local_correction_keeps_scope_and_internal_analysis_without_unrelated_quote():
    value = validate_decision(correction(), [observation()], None)
    body = render_alert(
        value, [observation()], NOW, followup=True, market_lines=["삼성전자 -1.7%"]
    )
    assert body.startswith("[설명 정정] PLUS 고배당주를 반도체 ETF로 잘못 연결했어")
    assert value["facts"] in body and value["interpretation"] in body
    assert all(
        text not in body
        for text in (
            "삼성전자 -1.7%",
            "내 상황:",
            "달라진 점:",
            "다음 확인:",
            "보도 확인",
        )
    )
    assert (
        value["portfolio_note"] and value["next_check"]
    )  # Internal analysis is retained.
    assert value["notify"] is True and value["importance"] == "HIGH"
    assert (
        "https://example.invalid/a" in body
    )  # Existing source-button conversion remains possible.


@pytest.mark.parametrize("notice", ["MATERIAL", "UNCERTAIN", None, {}, "unknown"])
def test_material_uncertain_legacy_or_invalid_hint_preserves_full_explanation(notice):
    value = validate_decision(
        correction(correction_notice=notice), [observation()], None
    )
    body = render_alert(
        value, [observation()], NOW, followup=True, market_lines=["계좌 위험 변화"]
    )
    assert "정정: PLUS" not in body
    assert all(text in body for text in ("내 상황:", "다음 확인:", "계좌 위험 변화"))


def test_urgent_correction_cannot_hide_detail_even_with_brief_hint():
    value = validate_decision(correction(urgency="IMMEDIATE"), [observation()], None)
    body = render_alert(value, [observation()], NOW)
    assert "다음 확인:" in body and value["portfolio_note"] in body


def test_local_source_correction_has_distinct_heading_without_repeated_prefixes():
    value = correction(
        correction_notice="SOURCE_ONLY",
        title="[정정] 정정: 원문 정정: 공시의 담당자 표기 변경",
    )
    body = render_alert(value, [observation()], NOW)
    assert body.startswith("[원문 정정] 공시의 담당자 표기 변경\n")
    assert "다음 확인:" not in body


def test_presentation_hint_does_not_shorten_a_new_event():
    value = validate_decision(correction(change="NEW"), [observation()], None)
    assert "correction_notice" not in value
    assert "다음 확인:" in render_alert(value, [observation()], NOW)


def test_copy_changes_keep_reuse_but_analysis_policy_changes_invalidate_it(monkeypatch):
    model = models.DecisionModel("codex:gpt-6-astra", "analyze")
    before = (model.policy_fingerprint, model.incremental_policy_fingerprint)
    monkeypatch.setattr(models, "CORRECTION_PRESENTATION", "Different display wording")
    assert (model.policy_fingerprint, model.incremental_policy_fingerprint) == before
    monkeypatch.setattr(
        models, "ANALYZE", models.ANALYZE + "\nChanged investment policy"
    )
    assert model.policy_fingerprint != before[0]


def test_model_receives_presentation_contract_without_an_extra_call(
    monkeypatch, tmp_path
):
    import subscription_json_model

    calls = []

    def infer(prompt, **kwargs):
        calls.append((prompt, kwargs))
        return "{}", {}

    monkeypatch.setattr(subscription_json_model, "infer", infer)
    models.DecisionModel(
        "codex:gpt-6-astra", "analyze", diagnostic_path=tmp_path / "diagnostic.json"
    )({"evidence": []})
    assert len(calls) == 1 and models.CORRECTION_PRESENTATION in calls[0][0]
    assert "correction_notice" in calls[0][1]["schema"]["properties"]
    assert "correction_notice" not in models.response_schema("screen")["properties"]
