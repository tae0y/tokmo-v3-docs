"""Actual failure shapes: unit conversion, unsupported new assertions, JSON truncation."""
from datetime import timedelta
import json

import pytest

from news_decision_models import decode_response
from tokmo.programs.news_document_quality import document_amounts, validate_document_amounts
from tokmo.records.news_decision_store import NewsDecisionStore
from test_news_decision_engine import Engine, observation, decision, screen
from test_news_decision_s3 import DAY


def document(text="운영자금 (원)\n23,000,000,000\n발행가 (원)\n500"):
    return dict(observation("a", DAY, "DISCLOSURE"), text=text, body_verified=True, content_scope="original_document")


def test_exact_korean_units_are_supplied_and_tenfold_error_is_rejected():
    source = document()
    assert {(r["value"], r["display"]) for r in document_amounts([source])} == {("23000000000", "230억 원"), ("500", "500원")}
    good = decision(["a"], facts="운영자금 230억 원을 조달하고 발행가는 500원이다.")
    validate_document_amounts(good, [source])
    for field in ("title", "facts", "interpretation", "why_now", "next_check", "digest_summary"):
        with pytest.raises(ValueError, match="ungrounded document amount"):
            validate_document_amounts(good | {field: "운영자금 2,300억 원"}, [source])
    with pytest.raises(ValueError):
        validate_document_amounts(good | {"facts": "운영자금2,300억원"}, [source])


def test_usd_billion_conversion_keeps_currency_and_components():
    source = document("A purchase price of approximately $11.9 billion and retention of up to $1.0 billion.")
    good = decision(["a"], facts="주주 지급 약 119억 달러, 직원 유지 보상 최대 10억 달러다.")
    validate_document_amounts(good, [source])
    for text in ("119억 원", "인수 대금 129억 달러", "직원 보상 100억 달러"):
        with pytest.raises(ValueError, match="ungrounded document amount"):
            validate_document_amounts(good | {"facts": text}, [source])


def test_only_cited_visible_sources_support_amount_and_regular_news_is_unchanged():
    source = document()
    bad = decision(["a"], facts="2,300억 원을 조달한다.")
    with pytest.raises(ValueError):
        validate_document_amounts(bad, [source, document("2,300억 원") | {"id": "uncited"}])
    validate_document_amounts(bad, [observation("a", DAY)])


def test_source_audit_shares_existing_correction_allowance_and_keeps_discovery(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []
    def analyze(req):
        seen.append(req)
        return decision(["a"], facts="운영자금 230억 원", interpretation="기존 주주의 자금 지원을 확인했다.")
    result = Engine(store, screen, analyze).tick([document()], DAY)
    assert len(seen) == 2 and seen[1]["correction"]["source_grounding_review"] is True
    assert len(result["alerts"]) == 1 and not result["failures"]
    store.close()


def test_correction_can_quote_old_wrong_amount_but_new_fact_must_be_grounded():
    change = {"prior_claim": "2,300억 원", "new_fact": "230억 원", "why_not_covered": "앞선 금액을 정정", "support_ids": ["a"]}
    fixed = decision(["a"], facts="2,300억 원으로 전달한 금액은 230억 원의 오기다.", change="CORRECTION", material_change=change)
    validate_document_amounts(fixed, [document()], ["앞서 2,300억 원으로 알림"])
    with pytest.raises(ValueError):
        validate_document_amounts(fixed, [document()])
    with pytest.raises(ValueError, match="new_fact"):
        validate_document_amounts(fixed | {"material_change": change | {"new_fact": "2,300억 원"}}, [document()], ["2,300억 원"])


def test_amount_error_gets_one_correction_and_durable_original_response(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []
    def analyze(req):
        seen.append(req)
        return decision(["a"], facts="운영자금 " + ("230억 원" if req.get("correction") else "2,300억 원"))
    result = Engine(store, screen, analyze).tick([document()], DAY)
    assert len(seen) == 2 and not result["failures"] and len(result["alerts"]) == 1
    assert "2,300억 원" in seen[1]["correction"]["previous_response"]["facts"]
    assert any(row["display"] == "230억 원" for row in seen[0]["document_amounts"])
    store.close()


@pytest.mark.parametrize("raw", ["", '{"candidates":[', "[]"])
def test_bad_generated_json_uses_existing_single_correction_and_keeps_raw(tmp_path, raw):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []
    def classify(req):
        seen.append(req)
        return screen(req) if req.get("correction") else decode_response(raw)
    result = Engine(store, classify, lambda req: decision(["a"])).tick([observation("a", DAY)], DAY)
    assert len(seen) == 2 and not result["failures"] and len(result["alerts"]) == 1
    assert seen[1]["correction"]["previous_response"]["invalid_model_response"] == raw
    responses = [json.loads(r[0]) for r in store.connection.execute("SELECT response FROM news_decision_calls WHERE stage='screen'")]
    assert any(r.get("invalid_model_response") == raw for r in responses)
    store.close()


def test_persistent_malformed_analysis_fails_visibly_after_two_calls(tmp_path):
    store = NewsDecisionStore(tmp_path / "state.sqlite3")
    seen = []
    def analyze(req):
        seen.append(req)
        return decode_response('{"facts":')
    result = Engine(store, screen, analyze).tick([observation("a", DAY)], DAY)
    assert len(seen) == 2 and result["failures"] and not result["alerts"]
    assert store.counts() == {"ERROR": 1}
    store.close()


def test_truncated_json_is_not_accepted_even_if_parseable():
    assert decode_response('{"answer": {"facts": "x"}}') == {"facts": "x"}
    assert decode_response('{"facts":"x"}', finish_reason="length")["invalid_model_reason"] == "output_truncated"
