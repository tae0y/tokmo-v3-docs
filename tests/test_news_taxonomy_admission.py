"""NEWS taxonomy auto-admission RED suite (stages 1–6, 2026-08-22).

Deterministic typed registration over the existing discovery corpus:
entity aliases, topics, metrics, catalysts, observable effects, and the
provider-neutral durable pending queue. No model call anywhere in here.
"""
import json
import sqlite3
from datetime import datetime, timezone

import pytest

from tokmo.programs.news_taxonomy_admission import (
    drain_taxonomy_pending,
    run_taxonomy_admission,
)
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.news_discovery import NewsDocument
from tokmo.values.news_taxonomy import (
    CATALYST_TYPES_V1,
    adjudication_job_id,
    build_adjudication_batch_request,
    build_adjudication_request,
    taxonomy_evidence_digest,
)

UTC = timezone.utc
NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)

CATALOG = {
    "ent:nvidia": ("NVIDIA", "엔비디아"),
    "ent:sk-hynix": ("SK hynix", "SK하이닉스"),
    "ent:alpha": ("AlphaCorp",),
    "ent:beta": ("BetaCorp",),
}


def _store(tmp_path):
    return NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")


def _docs(titles):
    return tuple(
        NewsDocument(f"d{i}", title, f"story-{i}", f"src-{i % 3}",
                     NOW.isoformat())
        for i, title in enumerate(titles))


def _run(store, titles, **kwargs):
    kwargs.setdefault("identity_catalog", CATALOG)
    return run_taxonomy_admission(
        store=store, documents=_docs(titles), now=NOW, **kwargs)


# ------------------------------------------------------------ schema/migration

def test_taxonomy_ledger_migration_upgrades_existing_store(tmp_path):
    from tokmo.records.news_episode_store import _actual_ddl_digest
    path = tmp_path / "stores" / "news_episodes.sqlite3"
    store = _store(tmp_path)
    store.upsert("보존사건", (), NOW)
    store.close()
    # Reconstruct the pre-taxonomy shape from the actual current schema:
    # drop the new ledgers and re-seal the digest, exactly like a store
    # created before this change.
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE taxonomy_decisions")
    connection.execute("DROP TABLE taxonomy_units")
    connection.execute("UPDATE schema_meta SET ddl_sha256=?",
                       (_actual_ddl_digest(connection),))
    connection.commit()
    connection.close()
    upgraded = NewsEpisodeStore(path)
    assert upgraded.list_taxonomy_units() == ()
    result = _run(upgraded, ["AlphaCorp(알파주식회사) 신제품 공개",
                             "AlphaCorp(알파주식회사) 유럽 출시"])
    assert result.admitted_auto >= 1
    # Prior episode history intact through the upgrade.
    assert upgraded.upsert("보존사건", (), NOW).revision == 2
    upgraded.close()


# ----------------------------------------------------- stage 1: entity aliases

def test_official_local_identity_pairs_admit_verified_auto(tmp_path):
    store = _store(tmp_path)
    _run(store, [], official_identities={"ent:nvda": ("NVDA", "엔비디아")})
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["reason"] == "OFFICIAL_LOCAL_IDENTITY"
    assert unit["canonical_key"] == "ent:nvda"


def test_alias_cooccurrence_repeated_stories_admits_verified_auto(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개 임박"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["reason"] == "ALIAS_COOCCURRENCE"
    assert unit["canonical_key"] == "ent:nvidia"
    assert store.verified_entity_aliases()["ent:nvidia"] == ("nvda",)


def test_single_story_alias_stays_candidate_not_verified(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert unit["state"] == "CANDIDATE"
    assert "ent:nvidia" not in store.verified_entity_aliases()


def test_alias_collision_across_entities_is_pending_never_lww(tmp_path):
    store = _store(tmp_path)
    _run(store, ["AlphaCorp(타이탄) 수주 공시", "AlphaCorp(타이탄) 증설 결정",
                 "BetaCorp(타이탄) 소송 제기", "BetaCorp(타이탄) 판결 임박"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "타이탄")
    assert unit["state"] == "PENDING_MODEL"
    assert unit["reason"] == "ALIAS_COLLISION"
    # A later single-entity re-observation must not silently re-verify.
    _run(store, ["AlphaCorp(타이탄) 후속 공시", "AlphaCorp(타이탄) 후속 발표"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "타이탄")
    assert unit["state"] == "PENDING_MODEL"
    assert store.verified_entity_aliases() == {}


def test_cross_run_alias_collision_downgrades_and_keeps_canonical(tmp_path):
    store = _store(tmp_path)
    _run(store, ["AlphaCorp(타이탄) 수주 공시", "AlphaCorp(타이탄) 증설 결정"])
    assert store.taxonomy_unit("ENTITY_ALIAS", "타이탄")["state"] \
        == "VERIFIED_AUTO"
    _run(store, ["BetaCorp(타이탄) 소송 제기", "BetaCorp(타이탄) 판결 임박"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "타이탄")
    assert unit["state"] == "PENDING_MODEL"
    assert unit["reason"] == "ALIAS_COLLISION"
    # The originally verified canonical is preserved, not overwritten.
    assert unit["canonical_key"] == "ent:alpha"


def test_known_alias_of_same_entity_is_not_readmitted(tmp_path):
    store = _store(tmp_path)
    _run(store, ["NVIDIA(엔비디아) 실적 발표", "NVIDIA(엔비디아) 신제품 공개"])
    # 엔비디아 is already a canonical catalog alias of ent:nvidia — nothing new.
    assert store.taxonomy_unit("ENTITY_ALIAS", "엔비디아") is None


# ------------------------------------------------------------- stage 2: topics

def test_scoped_repeated_bounded_phrase_admits_topic(tmp_path):
    store = _store(tmp_path)
    _run(store, ["SK하이닉스 메모리 감산 결정", "메모리 감산 확산 조짐",
                 "메모리 감산 장기화 우려 부상"],
         holdings={"SK하이닉스"})
    unit = store.taxonomy_unit("TOPIC", "메모리 감산")
    assert unit["state"] == "VERIFIED_AUTO"


def test_repeated_unscoped_phrase_is_pending_model(tmp_path):
    store = _store(tmp_path)
    _run(store, ["우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
                 "우주 광물 기업 급증세"])
    unit = store.taxonomy_unit("TOPIC", "우주 광물")
    assert unit["state"] == "PENDING_MODEL"


def test_generic_function_words_never_become_topics_or_pending(tmp_path):
    store = _store(tmp_path)
    result = _run(store, ["stocks could rise more than this",
                          "stocks could rise more than this again",
                          "us stocks could rise more than that"])
    assert result.pending == 0
    assert store.list_taxonomy_units() == ()


def test_below_threshold_phrase_is_not_admitted(tmp_path):
    store = _store(tmp_path)
    _run(store, ["메모리 감산 결정"], holdings={"SK하이닉스"})
    unit = store.taxonomy_unit("TOPIC", "메모리 감산")
    assert unit is None or unit["state"] != "VERIFIED_AUTO"


# ------------------------------------------------------------ stage 3: metrics

def test_metric_with_subject_value_unit_period_admits_observation(tmp_path):
    store = _store(tmp_path)
    result = _run(store, ["SK하이닉스 2분기 영업이익 9조 돌파"],
                  holdings={"SK하이닉스"})
    units = store.list_taxonomy_units(kind="METRIC", state="VERIFIED_AUTO")
    assert len(units) == 1
    payload = units[0]
    assert payload["canonical_key"] == "ent:sk-hynix"
    assert "9" in payload["unit_key"] and "조" in payload["unit_key"]
    assert result.admitted_auto >= 1


def test_metric_value_unit_without_subject_is_pending(tmp_path):
    store = _store(tmp_path)
    _run(store, ["반도체 수출 120억 달러 기록"])
    units = store.list_taxonomy_units(kind="METRIC")
    assert len(units) == 1
    assert units[0]["state"] == "PENDING_MODEL"


def test_bare_number_without_unit_produces_nothing(tmp_path):
    store = _store(tmp_path)
    _run(store, ["지수 3800 첫 안착"])
    assert store.list_taxonomy_units(kind="METRIC") == ()


# ---------------------------------------------------------- stage 4: catalysts

def test_single_controlled_type_with_scope_admits_catalyst(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아 실적 서프라이즈"], holdings=set())
    units = store.list_taxonomy_units(kind="CATALYST", state="VERIFIED_AUTO")
    assert len(units) == 1
    assert units[0]["canonical_key"] == "EARNINGS_RELEASE"
    assert units[0]["canonical_key"] in CATALYST_TYPES_V1


def test_multiple_controlled_types_is_pending_ambiguous(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아 실적 발표와 자사주 매입 결정"])
    units = store.list_taxonomy_units(kind="CATALYST")
    assert len(units) == 1
    assert units[0]["state"] == "PENDING_MODEL"
    assert units[0]["reason"] == "CATALYST_AMBIGUOUS"


def test_generic_event_marker_without_type_is_pending_unknown(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아 중대 발표 예고"])
    units = store.list_taxonomy_units(kind="CATALYST")
    assert len(units) == 1
    assert units[0]["state"] == "PENDING_MODEL"
    assert units[0]["reason"] == "CATALYST_UNKNOWN_TYPE"


# ------------------------------------------------------------ stage 5: effects

def test_observed_move_with_scope_admits_effect(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아 주가 5% 급등"])
    units = store.list_taxonomy_units(kind="EFFECT", state="VERIFIED_AUTO")
    assert len(units) == 1
    assert "UP" in units[0]["unit_key"]


def test_predicted_move_stays_pending_unresolved(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아 급등 전망 확산"])
    units = store.list_taxonomy_units(kind="EFFECT")
    assert len(units) == 1
    assert units[0]["state"] == "PENDING_MODEL"
    assert units[0]["reason"] == "EFFECT_PREDICTED_UNRESOLVED"


def test_unscoped_generic_move_produces_nothing(tmp_path):
    store = _store(tmp_path)
    _run(store, ["stocks could rise more than this"])
    assert store.list_taxonomy_units(kind="EFFECT") == ()


# --------------------------------------------- stage 6: pending queue/replay

def test_replay_same_corpus_is_idempotent(tmp_path):
    store = _store(tmp_path)
    titles = ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개",
              "우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
              "우주 광물 기업 급증세"]
    _run(store, titles)
    units_first = store.list_taxonomy_units()
    decisions_first = store.connection.execute(
        "SELECT count(*) FROM taxonomy_decisions").fetchone()[0]
    _run(store, titles)
    assert store.list_taxonomy_units() == units_first
    assert store.connection.execute(
        "SELECT count(*) FROM taxonomy_decisions").fetchone()[0] \
        == decisions_first


def test_every_unit_has_reason_and_evidence_digest(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개",
                 "우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
                 "우주 광물 기업 급증세"])
    for unit in store.list_taxonomy_units():
        assert unit["reason"]
        assert len(unit["evidence_digest"]) == 64
        decisions = store.taxonomy_decisions(unit["unit_id"])
        assert decisions and all(d["reason"] for d in decisions)


def test_adjudication_request_is_provider_neutral(tmp_path):
    store = _store(tmp_path)
    _run(store, ["우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
                 "우주 광물 기업 급증세"])
    (pending,) = store.list_taxonomy_pending(limit=8)
    request = build_adjudication_request(pending)
    flat = str(sorted(request)) + str(request)
    for forbidden in ("provider", "model_id", "gpt", "glm", "gemini"):
        assert forbidden not in flat.casefold()
    assert request["kind"] == "TOPIC"
    assert request["unit_key"] == "우주 광물"
    assert request["evidence"]


def test_pending_survives_reopen_and_admission_never_calls_adjudicator(
        tmp_path):
    store = _store(tmp_path)
    path = store.path
    _run(store, ["우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
                 "우주 광물 기업 급증세"])
    store.close()
    reopened = NewsEpisodeStore(path)
    (pending,) = reopened.list_taxonomy_pending(limit=8)
    assert pending["state"] == "PENDING_MODEL"
    # run_taxonomy_admission has no adjudicator seam at all: model
    # resolution only ever happens through the explicit drain below.
    import inspect
    assert "adjudicator" not in inspect.signature(
        run_taxonomy_admission).parameters


def test_drain_requires_explicit_adjudicator_and_records_provenance(
        tmp_path):
    store = _store(tmp_path)
    _run(store, ["우주 광물 채굴 첫 성공", "우주 광물 시장 개화",
                 "우주 광물 기업 급증세"])
    calls = []

    def adjudicator(request):
        calls.append(request)
        return {"verdict": "VERIFIED", "reason": "adjudicated-verified"}

    with pytest.raises(TypeError):
        drain_taxonomy_pending(store, now=NOW)  # no positional default
    resolved = drain_taxonomy_pending(store, adjudicator, now=NOW, limit=8)
    assert resolved == 1 and len(calls) == 1
    unit = store.taxonomy_unit("TOPIC", "우주 광물")
    assert unit["state"] == "VERIFIED_MODEL"
    decisions = store.taxonomy_decisions(unit["unit_id"])
    assert decisions[-1]["to_state"] == "VERIFIED_MODEL"


# ------------------- M1: non-title stored evidence (summary) alias discovery

SK_CATALOG = {
    "ent:sk-hynix": ("SK hynix", "SK하이닉스"),
    "ent:samsung-electronics": ("삼성전자",),
}


def _sdocs(rows):
    """rows: (title, summary) pairs — one distinct story per row."""
    return tuple(
        NewsDocument(f"d{i}", title, f"story-{i}", f"src-{i % 3}",
                     NOW.isoformat(), summary)
        for i, (title, summary) in enumerate(rows))


def _run_summaries(store, rows, **kwargs):
    kwargs.setdefault("identity_catalog", SK_CATALOG)
    return run_taxonomy_admission(
        store=store, documents=_sdocs(rows), now=NOW, **kwargs)


def test_title_abbreviation_with_summary_canonical_admits_hynix_alias(
        tmp_path):
    """The core user example: title alias + canonical in the stored summary
    (never fetched, already-stored non-title evidence)."""
    store = _store(tmp_path)
    _run_summaries(store, [
        ("하닉 주주환원 시동", "SK하이닉스[000660]가 자사주 매입을 발표했다"),
        ("하닉 HBM 증설 검토", "SK하이닉스[000660] HBM 라인 투자 확대"),
    ])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "하닉")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["canonical_key"] == "ent:sk-hynix"
    # The bracket pair inside the stored summary registers the code too.
    code = store.taxonomy_unit("ENTITY_ALIAS", "000660")
    assert code["state"] == "VERIFIED_AUTO"
    assert code["canonical_key"] == "ent:sk-hynix"
    # Arbitrary leftover title words are never inferred as aliases.
    assert store.taxonomy_unit("ENTITY_ALIAS", "주주환원") is None
    assert store.taxonomy_unit("ENTITY_ALIAS", "시동") is None
    aliases = store.verified_entity_aliases()["ent:sk-hynix"]
    assert "하닉" in aliases and "000660" in aliases


def test_sk_hanik_and_samjeon_abbreviations_admit(tmp_path):
    store = _store(tmp_path)
    _run_summaries(store, [
        ("SK하닉 4분기 실적 기대", "SK하이닉스 실적 시즌 개막"),
        ("SK하닉 신형 메모리 출하", "SK하이닉스 신규 공급 계약 체결"),
        ("삼전 주가 재평가 국면", "삼성전자[005930] 외국인 순매수 지속"),
        ("삼전 배당 확대 채비", "삼성전자[005930] 주주환원 확대 계획"),
    ])
    hanik = store.taxonomy_unit("ENTITY_ALIAS", "sk하닉")
    assert hanik["state"] == "VERIFIED_AUTO"
    assert hanik["canonical_key"] == "ent:sk-hynix"
    samjeon = store.taxonomy_unit("ENTITY_ALIAS", "삼전")
    assert samjeon["state"] == "VERIFIED_AUTO"
    assert samjeon["canonical_key"] == "ent:samsung-electronics"
    code = store.taxonomy_unit("ENTITY_ALIAS", "005930")
    assert code["state"] == "VERIFIED_AUTO"
    assert code["canonical_key"] == "ent:samsung-electronics"


def test_summary_evidence_single_sighting_stays_candidate(tmp_path):
    store = _store(tmp_path)
    _run_summaries(store, [
        ("하닉 주주환원 시동", "SK하이닉스 자사주 매입 발표"),
    ])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "하닉")
    assert unit["state"] == "CANDIDATE"
    assert "ent:sk-hynix" not in store.verified_entity_aliases()


def test_summary_evidence_cross_entity_conflict_is_pending(tmp_path):
    store = _store(tmp_path)
    _run_summaries(store, [
        ("하닉 수주 소식", "하이닉공업 대형 수주 공시"),
        ("하닉 수주 반복 확인", "하이닉공업 추가 수주 계약"),
        ("하닉 소송 제기", "하닉전자 특허 소송 개시"),
        ("하닉 판결 임박 관측", "하닉전자 소송 판결 예정"),
    ], identity_catalog={"ent:hy-one": ("하이닉공업",),
                         "ent:hy-two": ("하닉전자",)})
    unit = store.taxonomy_unit("ENTITY_ALIAS", "하닉")
    assert unit["state"] == "PENDING_MODEL"
    assert unit["reason"] == "ALIAS_COLLISION"
    assert store.verified_entity_aliases() == {}


def test_mid_word_fragment_never_becomes_alias(tmp_path):
    """A title token that only matches mid-word inside a canonical alias
    (never at a name-segment start) is not abbreviation evidence."""
    store = _store(tmp_path)
    _run_summaries(store, [
        ("이스 신제품 발표 소식", "SK하이닉스 신제품 발표"),
        ("이스 후속 보도 정리", "SK하이닉스 후속 계약"),
    ])
    assert store.taxonomy_unit("ENTITY_ALIAS", "이스") is None


def test_official_pairs_extend_summary_alias_discovery(tmp_path):
    """A holdings-official identity (no public-catalog entry) is usable
    canonical evidence for summary-side abbreviation discovery."""
    store = _store(tmp_path)
    _run_summaries(store, [
        ("삼전 어닝 서프라이즈", "삼성전자 잠정 실적 발표"),
        ("삼전 수출 회복 신호", "삼성전자 반도체 수출 회복"),
    ], identity_catalog={},
        official_identities={"ent:kr:005930": ("005930", "삼성전자")})
    unit = store.taxonomy_unit("ENTITY_ALIAS", "삼전")
    assert unit["state"] == "VERIFIED_AUTO"
    assert unit["canonical_key"] == "ent:kr:005930"


# ---------------------------------- M5: token/boundary-safe subject matching


def test_catalyst_marker_never_matches_inside_korean_word(tmp_path):
    # '출시' (PRODUCT_LAUNCH) must not fire inside '대출시장'.
    store = _store(tmp_path)
    _run(store, ["엔비디아 대출시장 확대"])
    assert store.list_taxonomy_units(kind="CATALYST") == ()


def test_short_latin_alias_never_matches_inside_word(tmp_path):
    # Catalog alias 'US' must not scope via the substring inside 'bonus'.
    store = _store(tmp_path)
    _run(store, ["bonus 실적 발표"],
         identity_catalog={"ent:us-market": ("US",)})
    assert store.list_taxonomy_units(kind="CATALYST") == ()


def test_korean_scope_fragment_never_matches_inside_word(tmp_path):
    # Holding '한화' must not scope via the substring inside '대한화학'.
    store = _store(tmp_path)
    _run(store, ["대한화학 실적 발표"], holdings={"한화"},
         identity_catalog={})
    assert store.list_taxonomy_units() == ()


def test_effect_marker_requires_token_boundary(tmp_path):
    # '상승' must not fire inside '무상승계'.
    store = _store(tmp_path)
    _run(store, ["엔비디아 무상승계 절차 개시"])
    assert store.list_taxonomy_units(kind="EFFECT") == ()


def test_compound_korean_token_still_matches_at_head(tmp_path):
    # Boundary safety must not break head-attached Korean compounds.
    store = _store(tmp_path)
    _run(store, ["한화에어로 수출 물량 5% 급등"], holdings={"한화"},
         identity_catalog={})
    units = store.list_taxonomy_units(kind="EFFECT", state="VERIFIED_AUTO")
    assert len(units) == 1


# --------------------------------------- M3: typed interest-term scope lanes


def test_typed_interest_terms_scope_only_their_lanes(tmp_path):
    store = _store(tmp_path)
    run_taxonomy_admission(
        store=store, documents=_docs(["수출액 120억 달러 기록"]), now=NOW,
        identity_catalog={}, typed_interests={"METRIC": ("수출액",)})
    units = store.list_taxonomy_units(kind="METRIC")
    assert len(units) == 1 and units[0]["state"] == "VERIFIED_AUTO"
    other = _store(tmp_path / "other")
    run_taxonomy_admission(
        store=other, documents=_docs(["수출액 120억 달러 기록"]), now=NOW,
        identity_catalog={}, typed_interests={"CATALYST": ("수출액",)})
    units = other.list_taxonomy_units(kind="METRIC")
    assert len(units) == 1 and units[0]["state"] == "PENDING_MODEL"


def test_entity_interest_terms_scope_topics(tmp_path):
    store = _store(tmp_path)
    run_taxonomy_admission(
        store=store, documents=_docs(
            ["SK하이닉스 메모리 감산 결정", "메모리 감산 확산 조짐",
             "메모리 감산 장기화 우려 부상"]), now=NOW,
        identity_catalog={}, typed_interests={"ENTITY": ("SK하이닉스",)})
    unit = store.taxonomy_unit("TOPIC", "메모리 감산")
    assert unit["state"] == "VERIFIED_AUTO"


def test_tombstone_preserves_history_and_blocks_readmission(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    store.tombstone_taxonomy_unit(unit["unit_id"], "manual retire", now=NOW)
    after = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert after["state"] == "TOMBSTONED"
    assert len(store.taxonomy_decisions(unit["unit_id"])) >= 2
    _run(store, ["엔비디아(NVDA) 재관측 일", "엔비디아(NVDA) 재관측 이"])
    assert store.taxonomy_unit("ENTITY_ALIAS", "nvda")["state"] \
        == "TOMBSTONED"
    assert not hasattr(store, "delete_taxonomy_unit")


# ------------- invariant: payload_json/evidence_digest one atomic matched pair


def _matched_pair(unit):
    payload = json.loads(unit["payload_json"])
    assert taxonomy_evidence_digest(payload) == unit["evidence_digest"]
    return payload


def test_candidate_to_verified_upgrade_rewrites_payload_with_digest(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표"])
    first = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert first["state"] == "CANDIDATE"
    first_payload = _matched_pair(first)
    _run(store, ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    assert unit["state"] == "VERIFIED_AUTO"
    payload = _matched_pair(unit)
    # The stored payload is the latest combined observation evidence, not
    # the stale single-sighting one the digest no longer identifies.
    assert payload["document_ids"] == ["d0", "d1"]
    # The first observation's evidence survives in the append-only log.
    decisions = store.taxonomy_decisions(unit["unit_id"])
    assert decisions[0]["evidence_digest"] == first["evidence_digest"]
    assert json.loads(decisions[0]["evidence_json"]) == first_payload


def test_collision_downgrade_rewrites_payload_with_digest(tmp_path):
    store = _store(tmp_path)
    _run(store, ["AlphaCorp(타이탄) 수주 확정", "AlphaCorp(타이탄) 증설 검토"])
    verified = store.taxonomy_unit("ENTITY_ALIAS", "타이탄")
    assert verified["state"] == "VERIFIED_AUTO"
    verified_payload = _matched_pair(verified)
    _run(store, ["BetaCorp(타이탄) 소송 접수", "BetaCorp(타이탄) 특허 분쟁"])
    unit = store.taxonomy_unit("ENTITY_ALIAS", "타이탄")
    assert unit["state"] == "PENDING_MODEL"
    assert unit["reason"] == "ALIAS_COLLISION"
    payload = _matched_pair(unit)
    # The stored payload is the collision-run observation evidence while
    # the originally verified canonical stays on the unit row.
    assert payload["canonical"] == "ent:beta"
    assert unit["canonical_key"] == "ent:alpha"
    # The verified-era evidence remains readable in the decision log.
    verified_decision = [d for d in store.taxonomy_decisions(unit["unit_id"])
                         if d["to_state"] == "VERIFIED_AUTO"][-1]
    assert verified_decision["evidence_digest"] == verified["evidence_digest"]
    assert json.loads(verified_decision["evidence_json"]) == verified_payload


def test_stage7_request_and_job_identity_use_current_matched_pair(tmp_path):
    store = _store(tmp_path)
    path = store.path
    _run(store, ["AlphaCorp(타이탄) 수주 확정", "AlphaCorp(타이탄) 증설 검토"])
    _run(store, ["BetaCorp(타이탄) 소송 접수", "BetaCorp(타이탄) 특허 분쟁"])
    store.close()
    reopened = NewsEpisodeStore(path)
    rows = reopened.list_taxonomy_adjudication_eligible(limit=8)
    assert any(row["reason"] == "ALIAS_COLLISION" for row in rows)
    for row in rows:
        # What stage 7 sends (payload) is exactly what the job identity
        # keys (digest) — for every eligible row, after close/reopen.
        payload = _matched_pair(row)
        assert build_adjudication_request(row)["evidence"] == payload
    batch = build_adjudication_batch_request(rows)
    assert [unit["evidence"] for unit in batch["units"]] \
        == [json.loads(row["payload_json"]) for row in rows]
    recomputed = [{"unit_id": row["unit_id"],
                   "evidence_digest": taxonomy_evidence_digest(
                       json.loads(row["payload_json"]))} for row in rows]
    assert adjudication_job_id(rows) == adjudication_job_id(recomputed)
    reopened.close()


def test_duplicate_replay_after_upgrade_changes_nothing(tmp_path):
    store = _store(tmp_path)
    _run(store, ["엔비디아(NVDA) 실적 발표"])
    titles = ["엔비디아(NVDA) 실적 발표", "엔비디아(NVDA) 신제품 공개"]
    _run(store, titles)
    unit = store.taxonomy_unit("ENTITY_ALIAS", "nvda")
    _matched_pair(unit)
    decisions = store.taxonomy_decisions(unit["unit_id"])
    _run(store, titles)
    assert store.taxonomy_unit("ENTITY_ALIAS", "nvda") == unit
    assert store.taxonomy_decisions(unit["unit_id"]) == decisions


# -------- S3 (SDS 2026-09-01 §F.2/§F.4, J-5 부속): the colloquial
# ent:sk-hynix aliases enter the public catalog with ZERO normalised
# collisions against every other key's aliases, suppressions, stopwords,
# and counting units — the precondition for any alias-catalog diff.


def test_sk_hynix_colloquial_aliases_registered_without_collisions():
    from tokmo.programs.news_topic_discovery import (
        _COUNTING_UNITS,
        _PUBLIC_ALIASES,
        _STOP,
        _SUPPRESSED,
        _normalise,
    )
    new_aliases = {_normalise(alias) for alias in ("하이닉스", "SK하닉", "하닉")}
    assert new_aliases <= set(_PUBLIC_ALIASES["ent:sk-hynix"])
    for key, aliases in _PUBLIC_ALIASES.items():
        if key != "ent:sk-hynix":
            assert not (new_aliases & set(aliases)), key
    assert not (new_aliases & set(_SUPPRESSED))
    assert not (new_aliases & _STOP)
    assert not (new_aliases & _COUNTING_UNITS)
