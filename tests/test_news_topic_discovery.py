from tokmo.programs.news_topic_discovery import (
    candidate_requires_classifier,
    discover_topics,
)
from tokmo.values.news_discovery import NewsDocument


def test_whole_corpus_alias_syndication_direct_and_suppression():
    docs = [
        NewsDocument(
            "1", "모더나 백신 임상 결과", "x", "wire-a", publisher_name="연합뉴스"
        ),
        NewsDocument(
            "2",
            "Moderna vaccine trial result",
            "x",
            "wire-a",
            publisher_name="연합뉴스",
        ),
        NewsDocument(
            "3", "Moderna vaccine update", "y", "bbc", publisher_name="BBC News"
        ),
        NewsDocument("4", "SK하이닉스 주주환원", "z", "ir", publisher_name="JTBC"),
        NewsDocument("5", "Reuters 기자 애널리스트 목표가", "n", "rating"),
    ]
    result = discover_topics(docs, holdings={"SK하이닉스"}, threshold=2, cap=10)
    by_key = {candidate.key: candidate for candidate in result.candidates}
    assert by_key["ent:moderna"].distinct_story_count == 2
    assert by_key["ent:sk-hynix"].lane == "DIRECT_HOLDING"
    assert all("reuters" not in item.key for item in result.candidates)
    assert result.syndicated_documents == 1


def test_candidate_cap_is_observable_and_concept_emergence_survives():
    docs = [
        NewsDocument(
            str(i),
            f"LK-99 초전도체 소식 {i}",
            str(i),
            f"s{i}",
            publisher_name=("연합뉴스", "JTBC", "BBC")[i % 3],
        )
        for i in range(4)
    ]
    result = discover_topics(docs, holdings=set(), threshold=2, cap=1)
    assert result.candidates[0].lane == "CONCEPT_EMERGENCE"
    assert result.dropped_over_cap >= 0


def test_generic_frequency_terms_do_not_consume_classifier_slots_but_entity_survives():
    docs = [
        NewsDocument(
            str(i),
            f"NVIDIA stocks could rise more than this AI debt {i}",
            f"story-{i}",
            f"source-{i % 3}",
            publisher_name=("연합뉴스", "JTBC", "BBC")[i % 3],
        )
        for i in range(6)
    ]

    result = discover_topics(docs, holdings={"미국 AI 빅테크"}, threshold=2, cap=20)

    terms = {candidate.phrase_text for candidate in result.candidates}
    assert "nvidia" in terms
    assert not ({"stocks", "could", "rise", "more", "than", "this", "ai"} & terms)


def test_prefilter_keeps_material_token_when_mixed_with_stopword():
    assert candidate_requires_classifier("tok:x", "AI debt") is True
    assert candidate_requires_classifier("tok:x", "US sanctions") is True
    assert candidate_requires_classifier("tok:x", "글로벌 공급망") is True


def test_prefilter_keeps_direct_holding_even_when_term_is_generic():
    assert candidate_requires_classifier("tok:x", "AI", lane="DIRECT_HOLDING") is True
    assert candidate_requires_classifier("tok:x", "AI") is False


def test_prefilter_keeps_canonical_entity_even_when_alias_text_is_generic():
    assert candidate_requires_classifier("ent:nvidia", "this") is True


def test_verified_extra_aliases_promote_terms_to_entity_keys():
    docs = [
        NewsDocument(
            str(i),
            f"NVDA 데이터센터 수주 확대 {i}",
            f"story-{i}",
            f"source-{i % 2}",
            publisher_name=("연합뉴스", "JTBC")[i % 2],
        )
        for i in range(4)
    ]
    without = discover_topics(docs, holdings=set(), threshold=2, cap=20)
    assert all(c.key != "ent:nvidia" for c in without.candidates)
    promoted = discover_topics(
        docs,
        holdings=set(),
        threshold=2,
        cap=20,
        extra_aliases={"ent:nvidia": ("NVDA",)},
    )
    assert any(c.key == "ent:nvidia" for c in promoted.candidates)
    # The alias term itself no longer surfaces as an anonymous token key.
    assert all(
        not (c.key.startswith("tok:") and c.phrase_text == "nvda")
        for c in promoted.candidates
    )


# -------- B2 (2026-08-22 reclosure): alias matching is boundary-safe, the
# same language-aware contract taxonomy admission uses — a short Korean
# alias never matches inside an unrelated compound and a latin alias never
# matches inside a larger token.


def test_korean_alias_never_matches_inside_unrelated_token():
    docs = [
        NewsDocument(
            str(i), f"지하닉스 유통망 점검 {i}", f"story-{i}", f"source-{i % 2}"
        )
        for i in range(4)
    ]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=2,
        cap=20,
        extra_aliases={"ent:sk-hynix": ("하닉",)},
    )
    assert all(c.key != "ent:sk-hynix" for c in result.candidates)


def test_latin_alias_never_matches_inside_larger_token():
    docs = [
        NewsDocument(
            str(i), f"Bonus payout plan 확정 {i}", f"story-{i}", f"source-{i % 2}"
        )
        for i in range(4)
    ]
    result = discover_topics(
        docs, holdings=set(), threshold=2, cap=20, extra_aliases={"ent:us-gov": ("US",)}
    )
    assert all(c.key != "ent:us-gov" for c in result.candidates)


def test_multi_token_and_korean_head_aliases_still_match():
    docs = [
        NewsDocument(
            "1", "SK hynix HBM roadmap 공개", "s1", "wire-a", publisher_name="연합뉴스"
        ),
        NewsDocument("2", "SK hynix capex 확대", "s2", "wire-b", publisher_name="JTBC"),
        # Korean head attachment: alias attaches at a token head only.
        NewsDocument(
            "3", "하이닉스향 장비 수주 급증", "s3", "wire-a", publisher_name="연합뉴스"
        ),
        NewsDocument(
            "4", "하이닉스가 신규 팹 착공", "s4", "wire-b", publisher_name="JTBC"
        ),
    ]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=2,
        cap=20,
        extra_aliases={"ent:sk-hynix": ("하이닉스",)},
    )
    by_key = {c.key: c for c in result.candidates}
    assert by_key["ent:sk-hynix"].distinct_story_count == 4


# -------- S7 (SDS A1 §S + A3 §V9; JA-10/24/25): single-token frequency
# candidates require a shared, origin-aware second anchor.


_PUBS3 = ("연합뉴스", "JTBC", "BBC")


def test_ja10_shared_anchor_gates_single_token_candidates():
    anchored = [
        NewsDocument(
            str(i),
            "출범 관련 긴급조치 확산발생",
            f"story-{i}",
            f"s{i}",
            publisher_name=_PUBS3[i % 3],
        )
        for i in range(3)
    ]
    result = discover_topics(anchored, holdings=set(), threshold=3, cap=20)
    assert any(c.phrase_text == "출범" for c in result.candidates)
    # Same frequency, no shared second anchor → demoted with the reason.
    bare = [
        NewsDocument(
            str(i),
            f"출범 개별사안{i} 진행",
            f"story-{i}",
            f"s{i}",
            publisher_name=_PUBS3[i % 3],
        )
        for i in range(3)
    ]
    result = discover_topics(bare, holdings=set(), threshold=3, cap=20)
    assert all(c.phrase_text != "출범" for c in result.candidates)
    assert any(
        d.reason == "NO_SHARED_ANCHOR" and d.phrase_text == "출범"
        for d in result.decisions
    )


def test_ja24_common_newsroom_word_is_not_an_anchor():
    # Three unrelated stories share only 출범 + the common word 발표: no
    # lone unigram can anchor, and every candidate bigram contains the
    # frequency term itself.
    docs = [
        NewsDocument("1", "재단 출범 발표", "s1", "w1", publisher_name="연합뉴스"),
        NewsDocument("2", "동호회 출범 발표", "s2", "w2", publisher_name="JTBC"),
        NewsDocument("3", "협의체 출범 발표", "s3", "w3", publisher_name="BBC"),
    ]
    result = discover_topics(docs, holdings=set(), threshold=3, cap=20)
    demoted = {
        d.phrase_text for d in result.decisions if d.reason == "NO_SHARED_ANCHOR"
    }
    assert {"출범", "발표"} <= demoted
    assert all(c.phrase_text not in ("출범", "발표") for c in result.candidates)


def test_ja25_anchor_support_is_origin_aware():
    # The anchor bigram appears in three stories — but they are ONE
    # syndicated origin; support fails (§V9). The multi-origin control
    # with the same shape surfaces.
    syndicated = [
        NewsDocument(
            str(i),
            "규제심사 관련 전격개시 임박확정",
            f"story-{i}",
            f"s{i}",
            publisher_name="연합뉴스",
        )
        for i in range(3)
    ] + [
        NewsDocument(
            "x", "규제심사 별건사안 검토", "story-x", "sx", publisher_name="JTBC"
        )
    ]
    result = discover_topics(syndicated, holdings=set(), threshold=3, cap=20)
    assert all(c.phrase_text != "규제심사" for c in result.candidates)
    assert any(
        d.reason == "NO_SHARED_ANCHOR" and d.phrase_text == "규제심사"
        for d in result.decisions
    )
    control = [
        NewsDocument(
            str(i),
            "규제심사 관련 전격개시 임박확정",
            f"story-{i}",
            f"s{i}",
            publisher_name=_PUBS3[i % 3],
        )
        for i in range(3)
    ]
    result = discover_topics(control, holdings=set(), threshold=3, cap=20)
    assert any(c.phrase_text == "규제심사" for c in result.candidates)


# -------- S5 (SDS A1 §O/§P + A3 §V6/§V7; JA-1..3, JA-6): exact corporate
# identity, dedicated full-legal-name scanning, and unique-ETF resolution.


def test_ja1_group_prefix_never_makes_direct_holding():
    docs = [
        NewsDocument(
            str(i), f"삼성 그룹 인사 개편 {i}", f"story-{i}", f"source-{i % 2}"
        )
        for i in range(4)
    ]
    result = discover_topics(docs, holdings={"삼성전자"}, threshold=2, cap=20)
    assert all(c.lane != "DIRECT_HOLDING" for c in result.candidates)


def test_ja2_partial_latin_token_never_makes_direct_holding():
    docs = [
        NewsDocument(
            str(i), f"sk 그룹 지배구조 개편 {i}", f"story-{i}", f"source-{i % 2}"
        )
        for i in range(4)
    ]
    result = discover_topics(docs, holdings={"SK하이닉스"}, threshold=2, cap=20)
    assert all(c.lane != "DIRECT_HOLDING" for c in result.candidates)


def test_exact_whole_holding_term_still_direct():
    docs = [
        NewsDocument(str(i), f"삼성전자 실적 발표 {i}", f"story-{i}", f"source-{i % 2}")
        for i in range(4)
    ]
    result = discover_topics(docs, holdings={"삼성전자"}, threshold=2, cap=20)
    by_term = {c.phrase_text: c for c in result.candidates}
    assert by_term["삼성전자"].lane == "DIRECT_HOLDING"


def test_ja3_full_legal_name_scan_forms_direct_candidate():
    # The four-token official name exceeds the bigram bound; only the
    # dedicated full-name scan (§V7) can form the DIRECT candidate — and a
    # suffixed corporate form still matches after normalization.
    docs = [
        NewsDocument("1", "한화에어로스페이스 수출 계약 체결", "s1", "w1"),
        NewsDocument("2", "주식회사 한화에어로스페이스 신규 수주", "s2", "w2"),
    ]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=3,
        cap=20,
        official_names={"ent:kr:012450": ("한화에어로스페이스",)},
    )
    by_key = {c.key: c for c in result.candidates}
    assert "ent:kr:012450" in by_key
    assert by_key["ent:kr:012450"].lane == "DIRECT_HOLDING"
    assert by_key["ent:kr:012450"].distinct_story_count == 2


def test_ja3_suffixed_official_name_matches_bare_headline():
    """SOL M1: the closed legal-suffix normalization applies to the
    OFFICIAL name too — a suffixed registration must match bare headline
    mentions (both orientations of §O.1-2), including spacing variants."""
    docs = [
        NewsDocument("1", "한화에어로스페이스 수출 계약 체결", "s1", "w1"),
        NewsDocument("2", "한화에어로스페이스 신규 수주 발표", "s2", "w2"),
    ]
    for official in (
        "주식회사 한화에어로스페이스",
        "주식회사  한화에어로스페이스",
        "한화에어로스페이스(주)",
        "㈜한화에어로스페이스",
    ):
        result = discover_topics(
            docs,
            holdings=set(),
            threshold=3,
            cap=20,
            official_names={"ent:kr:012450": (official,)},
        )
        by_key = {c.key: c for c in result.candidates}
        assert "ent:kr:012450" in by_key, official
        assert by_key["ent:kr:012450"].lane == "DIRECT_HOLDING"
        assert by_key["ent:kr:012450"].distinct_story_count == 2


def test_min1_suppressed_decision_counts_independent_origins():
    """SOL MIN1: DOCUMENT_SUPPRESSED audit rows count independent origins,
    coherent with every other D-5 counting point."""
    docs = [
        NewsDocument(
            "1", "Reuters 기자 시황 정리", "s1", "w1", publisher_name="연합뉴스"
        ),
        NewsDocument(
            "2", "Reuters 기자 시황 브리핑", "s2", "w2", publisher_name="연합뉴스TV"
        ),  # same origin: yna
        NewsDocument("3", "Reuters 기자 시황 요약", "s3", "w3", publisher_name="JTBC"),
    ]
    result = discover_topics(docs, holdings=set(), threshold=3, cap=20)
    (suppressed,) = [d for d in result.decisions if d.reason == "DOCUMENT_SUPPRESSED"]
    assert suppressed.distinct_story_count == 3
    assert suppressed.distinct_source_count == 2  # yna + jtbc origins


def test_ja3_partial_tokens_never_reconstruct_company_identity():
    docs = [
        NewsDocument("1", "한화 그룹 개편", "s1", "w1"),
        NewsDocument("2", "에어로스페이스 산업 동향", "s2", "w2"),
    ]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=3,
        cap=20,
        official_names={"ent:kr:012450": ("한화에어로스페이스",)},
    )
    assert all(c.key != "ent:kr:012450" for c in result.candidates)


def test_ja4_etf_reference_scan_forms_direct_candidate():
    docs = [
        NewsDocument("1", "솔 반도체 2 순매수 지속", "s1", "w1"),
        NewsDocument("2", "SOL AI TOP2 자금 유입", "s2", "w2"),
    ]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=3,
        cap=20,
        official_names={"ent:kr:455850": ("SOL AI반도체 TOP2",)},
    )
    by_key = {c.key: c for c in result.candidates}
    assert "ent:kr:455850" in by_key
    assert by_key["ent:kr:455850"].lane == "DIRECT_HOLDING"
    assert by_key["ent:kr:455850"].distinct_story_count == 2


def test_ja6_ambiguous_etf_reference_fails_closed_with_decision():
    docs = [NewsDocument("1", "솔 반도체 2 순매수", "s1", "w1")]
    result = discover_topics(
        docs,
        holdings=set(),
        threshold=3,
        cap=20,
        official_names={
            "ent:kr:455850": ("SOL AI반도체 TOP2",),
            "ent:kr:466920": ("SOL 반도체 소부장 2",),
        },
    )
    assert all(not c.key.startswith("ent:kr:4") for c in result.candidates)
    reasons = {decision.reason for decision in result.decisions}
    assert "ETF_MATCH_AMBIGUOUS" in reasons


# -------- S3 (SDS 2026-09-01 §F.2, J-5): the colloquial SK하이닉스 variants
# in the PUBLIC catalog surface the same ent:sk-hynix candidate — 하닉 via
# Korean head attachment, SK 하닉 via the tokenised alias sequence, and
# 하이닉스 directly.


def test_public_catalog_colloquial_variants_merge_into_sk_hynix():
    docs = [
        NewsDocument("1", "하닉, 자사주 소각 결의", "s1", "wire-a"),
        NewsDocument("2", "SK 하닉 자사주 소각 발표", "s2", "wire-b"),
        NewsDocument("3", "SK하닉 소각 규모 확대", "s3", "wire-a"),
        NewsDocument("4", "하이닉스 자사주 소각 공시", "s4", "wire-c"),
    ]
    result = discover_topics(docs, holdings={"SK하이닉스"}, threshold=2, cap=20)
    by_key = {candidate.key: candidate for candidate in result.candidates}
    assert "ent:sk-hynix" in by_key
    assert by_key["ent:sk-hynix"].distinct_story_count == 4
    assert by_key["ent:sk-hynix"].lane == "DIRECT_HOLDING"


# -------- S0 (SDS 2026-09-01 W12/J-6): counting-unit tokens are measurement
# fragments ("80만명" → "80" + "만명"), never discovery topics. The 노브랜드버거
# 80만 false positive surfaced through exactly this seam.


def test_counting_unit_token_never_becomes_candidate():
    docs = [
        NewsDocument(
            str(i),
            f"노브랜드버거 앱 회원 80만명 돌파 순항중 {i}",
            f"story-{i}",
            f"source-{i % 2}",
            publisher_name=("연합뉴스", "JTBC")[i % 2],
        )
        for i in range(4)
    ]
    result = discover_topics(docs, holdings=set(), threshold=2, cap=20)
    terms = {candidate.phrase_text for candidate in result.candidates}
    assert "노브랜드버거" in terms  # the real subject keeps surfacing
    assert "만명" not in terms
    assert not any(
        "만명" in (candidate.phrase_text or "") for candidate in result.candidates
    )


def test_counting_unit_vocabulary_is_blocked_and_material_tokens_survive():
    from tokmo.programs.news_topic_discovery import (
        _COUNTING_UNITS,
        _eligible_token,
    )

    for unit in ("만명", "만대", "만원", "억원", "조원", "퍼센트"):
        assert unit in _COUNTING_UNITS
        assert _eligible_token(unit) is False
    for token in ("돌파", "소각", "배당"):
        assert _eligible_token(token) is True


def test_exact_alias_match_is_unaffected_by_boundary_contract():
    docs = [
        NewsDocument(
            str(i),
            f"모더나 임상 3상 결과 발표 {i}",
            f"story-{i}",
            f"source-{i % 2}",
            publisher_name=("연합뉴스", "JTBC")[i % 2],
        )
        for i in range(4)
    ]
    result = discover_topics(docs, holdings=set(), threshold=2, cap=20)
    assert any(c.key == "ent:moderna" for c in result.candidates)
