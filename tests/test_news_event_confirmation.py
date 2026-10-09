"""Confirmation-state closed vocabulary and deterministic derivation.

SDS 2026-09-01 §D.1–§D.3/§E.1 (decisions 7·8·9): four confirmation states
survive without collapse, the ONLY path to OFFICIAL_CONFIRMED is a
deterministic official primary-evidence ref, and a model claim can move the
derived state downward only (거짓수리 2·4 방어, J-12).
"""

import dataclasses

import pytest

from tokmo.values.news_event_confirmation import (
    CONFIRMATION_LABELS_KO,
    CONFIRMATION_STATES_V1,
    CORP_ACTION_PATTERNS_V1,
    EVENT_TYPES_V1,
    SURPRISE_CLAIMS_V1,
    OfficialEvidenceRefV1,
    derive_confirmation,
)

REF = OfficialEvidenceRefV1(
    source="opendart",
    ref_id="20260901000123",
    report_name="주요사항보고서(자기주식소각결정)",
    stock_code="000660",
    receipt_date="2026-09-01",
    matched_action="BUYBACK_RETIREMENT",
)


def test_confirmation_vocabulary_is_closed_and_fully_labelled():
    assert CONFIRMATION_STATES_V1 == frozenset(
        {
            "OFFICIAL_CONFIRMED",
            "MULTI_MEDIA_UNCONFIRMED",
            "SINGLE_MEDIA_UNCONFIRMED",
            "ANALYSIS_OPINION",
        }
    )
    assert set(CONFIRMATION_LABELS_KO) == set(CONFIRMATION_STATES_V1)
    assert CONFIRMATION_LABELS_KO["OFFICIAL_CONFIRMED"] == "공식 확인(공시·1차 출처)"
    assert (
        CONFIRMATION_LABELS_KO["MULTI_MEDIA_UNCONFIRMED"]
        == "복수 언론 보도, 공식 확인 전"
    )
    assert (
        CONFIRMATION_LABELS_KO["SINGLE_MEDIA_UNCONFIRMED"] == "단일 언론 보도, 미확인"
    )
    assert CONFIRMATION_LABELS_KO["ANALYSIS_OPINION"] == "분석·의견·전망"
    # Labels must never collide with the forbidden digest markers (§E.3).
    for label in CONFIRMATION_LABELS_KO.values():
        for marker in ("판정:", "대상:", "밤사이 최신 material 상태 통합:"):
            assert marker not in label


def test_event_type_and_surprise_vocabularies_are_closed():
    assert EVENT_TYPES_V1 == frozenset(
        {
            "CORPORATE_ACTION_DECIDED",
            "CORPORATE_ACTION_PROPOSED",
            "EARNINGS_GUIDANCE",
            "DIVIDEND_ROUTINE",
            "DIVIDEND_CHANGE",
            "MACRO_POLICY_EMERGENCY",
            "WAR_MARKET_DISRUPTION",
            "CONSUMER_MILESTONE",
            "ANALYST_OPINION",
            "RUMOR_SINGLE_CLAIM",
            "OTHER",
        }
    )
    assert SURPRISE_CLAIMS_V1 == frozenset({"ROUTINE", "SURPRISE", "UNKNOWN"})


def test_official_ref_is_the_only_promotion_path():
    # A deterministic ref alone yields OFFICIAL_CONFIRMED …
    assert derive_confirmation(REF, 0, 0, None) == "OFFICIAL_CONFIRMED"
    assert derive_confirmation(REF, 3, 2, "ANALYSIS_OPINION") == "OFFICIAL_CONFIRMED"
    # … and a model claim can NEVER promote without one (거짓수리 2·4).
    assert (
        derive_confirmation(None, 5, 0, "OFFICIAL_CONFIRMED")
        == "MULTI_MEDIA_UNCONFIRMED"
    )
    assert (
        derive_confirmation(None, 1, 0, "OFFICIAL_CONFIRMED")
        == "SINGLE_MEDIA_UNCONFIRMED"
    )
    assert derive_confirmation(None, 0, 2, "OFFICIAL_CONFIRMED") == "ANALYSIS_OPINION"


def test_fact_source_count_drives_the_base_state():
    assert derive_confirmation(None, 2, 0, None) == "MULTI_MEDIA_UNCONFIRMED"
    assert derive_confirmation(None, 3, 1, None) == "MULTI_MEDIA_UNCONFIRMED"
    assert derive_confirmation(None, 1, 0, None) == "SINGLE_MEDIA_UNCONFIRMED"
    assert derive_confirmation(None, 0, 0, None) == "ANALYSIS_OPINION"
    assert derive_confirmation(None, 0, 3, None) == "ANALYSIS_OPINION"


def test_model_claim_applies_downward_only():
    assert (
        derive_confirmation(None, 2, 0, "SINGLE_MEDIA_UNCONFIRMED")
        == "SINGLE_MEDIA_UNCONFIRMED"
    )
    assert derive_confirmation(None, 2, 0, "ANALYSIS_OPINION") == "ANALYSIS_OPINION"
    assert derive_confirmation(None, 1, 0, "ANALYSIS_OPINION") == "ANALYSIS_OPINION"
    # Upward claims and garbage claims are ignored.
    assert (
        derive_confirmation(None, 1, 0, "MULTI_MEDIA_UNCONFIRMED")
        == "SINGLE_MEDIA_UNCONFIRMED"
    )
    assert (
        derive_confirmation(None, 0, 0, "MULTI_MEDIA_UNCONFIRMED") == "ANALYSIS_OPINION"
    )
    assert (
        derive_confirmation(None, 2, 0, "TOTALLY_CONFIRMED")
        == "MULTI_MEDIA_UNCONFIRMED"
    )
    assert derive_confirmation(None, 2, 0, "") == "MULTI_MEDIA_UNCONFIRMED"


def test_relevance_vocabulary_and_derivation_are_bounded():
    """SDS A1 §Q (decisions 13·14): cited model claims may promote
    relevance only; identity stays code-owned and uncited claims are void."""
    from tokmo.values.news_event_confirmation import (
        RELEVANCE_CLAIMS_V1,
        derive_relevance,
    )

    assert RELEVANCE_CLAIMS_V1 == frozenset(
        {"NONE", "INDIRECT_MATERIAL", "THEMATIC_MATERIAL"}
    )
    evidence = {"headline_1": "삼성전자 파운드리 수주", "headline_1_src": "x"}
    # direct holding is identity-owned — the model claim is irrelevant.
    assert (
        derive_relevance(True, "THEMATIC_MATERIAL", ("headline_1",), evidence)
        == "DIRECT"
    )
    assert derive_relevance(True, None, (), evidence) == "DIRECT"
    # cited claims promote relevance…
    assert (
        derive_relevance(False, "INDIRECT_MATERIAL", ("headline_1",), evidence)
        == "INDIRECT_MATERIAL"
    )
    assert (
        derive_relevance(False, "THEMATIC_MATERIAL", ("headline_1",), evidence)
        == "THEMATIC_MATERIAL"
    )
    # …but uncited, unresolved, or off-vocabulary claims never do (JA-12).
    assert derive_relevance(False, "INDIRECT_MATERIAL", (), evidence) == "NONE"
    assert derive_relevance(False, "INDIRECT_MATERIAL", ("ghost",), evidence) == "NONE"
    assert derive_relevance(False, "DIRECT", ("headline_1",), evidence) == "NONE"
    assert derive_relevance(False, None, ("headline_1",), evidence) == "NONE"


def test_official_ref_is_frozen_and_action_patterns_are_closed():
    with pytest.raises(dataclasses.FrozenInstanceError):
        REF.ref_id = "tampered"  # type: ignore[misc]
    assert set(CORP_ACTION_PATTERNS_V1) == {
        "BUYBACK_RETIREMENT",
        "BUYBACK",
        "DIVIDEND_DECISION",
        "CAPITAL_INCREASE",
        "MERGER_ACQUISITION",
    }
    assert "자기주식소각" in CORP_ACTION_PATTERNS_V1["BUYBACK_RETIREMENT"]
    assert "자기주식취득" in CORP_ACTION_PATTERNS_V1["BUYBACK"]
    assert "배당결정" in CORP_ACTION_PATTERNS_V1["DIVIDEND_DECISION"]
    assert "유상증자결정" in CORP_ACTION_PATTERNS_V1["CAPITAL_INCREASE"]
    assert "합병" in CORP_ACTION_PATTERNS_V1["MERGER_ACQUISITION"]
    for patterns in CORP_ACTION_PATTERNS_V1.values():
        assert isinstance(patterns, tuple) and patterns
