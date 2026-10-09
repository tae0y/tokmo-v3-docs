"""Exact corporate identity + bounded ETF token-coverage matching.

SDS 2026-09-01 A1 §O/§P as repaired by A3 §V6 (decisions 11·12): companies
match exactly (registered identity/alias or normalized full legal name —
never fuzzy, never group prefixes); ETF/product names match through the
deterministic brand-anchored span extraction with reference-side weighted
coverage ≥ 0.8, numeric preservation, signal floor, and unique-candidate
fail-closed adjudication. No Levenshtein, no raw substrings.
"""

from tokmo.values.news_instrument_match import (
    COMPANY_LEGAL_SUFFIXES_V1,
    ETF_BRAND_TOKENS_V1,
    ETF_GENERIC_TOKENS_V1,
    ETF_REFERENCE_MAX_TOKENS,
    company_name_exact_match,
    is_etf_name,
    match_etf_reference,
    normalize_company_name,
    resolve_etf_reference,
)

OFFICIAL = "SOL AI반도체 TOP2"


# ------------------------------------------------------------ §O companies


def test_company_normalization_strips_closed_legal_suffixes():
    assert "주식회사" in COMPANY_LEGAL_SUFFIXES_V1
    assert normalize_company_name(
        "주식회사 한화에어로스페이스"
    ) == normalize_company_name("한화에어로스페이스")
    assert normalize_company_name("삼성전자(주)") == normalize_company_name("삼성전자")
    assert normalize_company_name("㈜  카카오") == normalize_company_name("카카오")


def test_company_exact_match_is_whole_name_only():
    assert company_name_exact_match("삼성전자", "삼성전자") is True
    assert company_name_exact_match("주식회사 삼성전자", "삼성전자") is True
    # Group prefixes and partial company tokens NEVER match (JA-1/JA-2).
    assert company_name_exact_match("삼성", "삼성전자") is False
    assert company_name_exact_match("전자", "삼성전자") is False
    assert company_name_exact_match("sk", "SK하이닉스") is False
    assert company_name_exact_match("삼성전자우선주", "삼성전자") is False


def test_etf_gate_is_brand_first_token():
    assert "sol" in ETF_BRAND_TOKENS_V1 and "kodex" in ETF_BRAND_TOKENS_V1
    assert is_etf_name("SOL AI반도체 TOP2") is True
    assert is_etf_name("KODEX 200") is True
    assert is_etf_name("삼성전자") is False
    assert is_etf_name("SK하이닉스") is False


# ------------------------------------- §P/§V6 ETF pair arithmetic (JA-4/5)


def test_p5_positive_references_match():
    assert match_etf_reference("솔 반도체 2 순매수", OFFICIAL).matched
    assert match_etf_reference("SOL AI TOP2 강세", OFFICIAL).matched


def test_p5_negative_references_reject():
    # numeric mismatch (P.4-3)
    assert not match_etf_reference("SOL AI반도체 TOP3", OFFICIAL).matched
    # unresolved distinctive token drops coverage (P.4-1 / JA-15)
    assert not match_etf_reference("솔 배당", OFFICIAL).matched
    # signal floor: brand alone / brand+generic (P.4-2)
    assert not match_etf_reference("솔", OFFICIAL).matched
    assert not match_etf_reference("솔 ETF", OFFICIAL).matched


def test_ja16_official_number_omitted_is_allowed():
    outcome = match_etf_reference("솔 AI 반도체 담았다", OFFICIAL)
    assert outcome.matched  # uniqueness (not arithmetic) governs omission


def test_ja17_repeated_tokens_are_deduplicated():
    doubled = match_etf_reference("솔 반도체 반도체 2", OFFICIAL)
    single = match_etf_reference("솔 반도체 2", OFFICIAL)
    assert doubled.matched and single.matched
    assert doubled.coverage == single.coverage


def test_ja18_brand_transliteration_round_trip():
    assert match_etf_reference("솔 AI 반도체 2", OFFICIAL).matched
    assert match_etf_reference("SOL AI 반도체 2", OFFICIAL).matched


def test_ja19_weighted_boundary_cases_around_080():
    # §V6 arithmetic: only span-eligible foreign tokens (numerics) can sit
    # unresolved inside the span — non-numeric foreign tokens terminate it.
    official = "SOL 배당 성장 50"  # sol(B1)+배당(D2)+성장(D2)+50(N2) = 7
    # all resolved → 1.0 ≥ 0.8
    full = match_etf_reference("솔 배당 성장 50", official)
    assert full.matched and full.coverage == 1.0
    # foreign numeric in span: unresolved weight 2/7 > 20% → coverage
    # rejection AND the numeric-preservation rule both fire (JA-15 analog).
    outcome = match_etf_reference("솔 배당 성장 7", official)
    assert not outcome.matched
    assert outcome.coverage < 0.8
    # a non-numeric foreign token never enters the span — the span ends
    # before it, so the remaining resolved prefix still matches.
    prefix = match_etf_reference("솔 배당 성장 코스피 마감", official)
    assert prefix.matched


def test_span_extraction_is_brand_anchored_and_bounded():
    # No brand anchor in the headline → ETF matching is never attempted.
    assert not match_etf_reference("반도체 2 급등", OFFICIAL).matched
    # Tokens outside the anchored span never enter the denominator: the
    # trailing verb ends the span, so the reference still matches.
    assert match_etf_reference("급등장서 솔 반도체 2 순매수했다", OFFICIAL).matched
    assert ETF_REFERENCE_MAX_TOKENS == 6
    assert "top" in ETF_GENERIC_TOKENS_V1


# ------------------------------------------- §P.4-4 uniqueness (JA-6)


def test_unique_candidate_admits_and_ambiguity_fails_closed():
    universe = {
        "ent:kr:aaaaaa": "SOL AI반도체 TOP2",
        "ent:kr:bbbbbb": "SOL 배당 성장 50",
    }
    key, reason = resolve_etf_reference("솔 반도체 2 순매수", universe)
    assert key == "ent:kr:aaaaaa" and reason == "MATCHED"
    ambiguous = {
        "ent:kr:aaaaaa": "SOL AI반도체 TOP2",
        "ent:kr:cccccc": "SOL 반도체 소부장 2",
    }
    key, reason = resolve_etf_reference("솔 반도체 2", ambiguous)
    assert key is None and reason == "ETF_MATCH_AMBIGUOUS"
    key, reason = resolve_etf_reference("코스피 마감 시황", universe)
    assert key is None and reason == "NO_MATCH"
