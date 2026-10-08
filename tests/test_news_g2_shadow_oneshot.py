"""G2 actual-miss bounded NEWS SHADOW gate — strict RED contract."""
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from tokmo.programs.news_g2_shadow_oneshot import run_news_g2_shadow_oneshot
from tokmo.programs.news_topic_discovery import discover_topics
from tokmo.values.news_discovery import NewsDocument as _NewsDocument

# D-5 fixture shim: synthetic sources map deterministically onto the three
# known publisher origins so origin-count thresholds mirror source counts.
_PUBS = ("연합뉴스", "JTBC", "BBC")


def NewsDocument(*args, **kwargs):  # noqa: N802 — fixture shim
    import dataclasses as _dataclasses
    doc = _NewsDocument(*args, **kwargs)
    if doc.publisher_name is None:
        doc = _dataclasses.replace(
            doc, publisher_name=_PUBS[sum(doc.source_id.encode()) % 3])
    return doc

UTC = timezone.utc
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)
CUTOFF = NOW - timedelta(days=7)


def _corpus(path):
    path.parent.mkdir(parents=True)
    connection = sqlite3.connect(path)
    connection.execute(
        "CREATE TABLE news_documents(news_document_id TEXT, title TEXT,"
        " title_fingerprint TEXT, content_hash TEXT, source_id TEXT,"
        " first_seen_at TEXT, revision_no INTEGER, summary_text TEXT,"
        " published_at TEXT, evidence_role TEXT, publisher_name TEXT)")
    seen = (NOW - timedelta(hours=1)).isoformat()
    for index in range(3):
        connection.execute(
            "INSERT INTO news_documents"
            " VALUES(?,?,?,NULL,?,?,1,NULL,NULL,'FACT_DISCOVERY',?)",
            (f"d{index}", "반도체충격 관련 공급둔화 우려확산 급증", f"fp{index}",
             f"s{index}", seen,
             _PUBS[index % 3]))
    connection.commit()
    connection.close()
    path.chmod(0o600)


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_g2_oneshot_uses_explicit_corpus_without_activation_file(tmp_path):
    corpus = tmp_path / "canonical" / "external_news_v4.sqlite3"
    shadow = tmp_path / "shadow"
    _corpus(corpus)
    before = (corpus.stat().st_size, corpus.stat().st_mtime_ns, _digest(corpus))

    result = run_news_g2_shadow_oneshot(
        root=shadow, corpus_path=corpus, cutoff=CUTOFF,
        observed_now_utc=NOW)

    assert result["status"] == "SAVED"
    assert result["wire_counts"]
    assert all(count == 0 for count in result["wire_counts"].values())
    assert result["source"] == {
        "size": before[0], "mtime_ns": before[1], "sha256": before[2]}
    assert result["source_unchanged"] is True
    assert result["metrics"]["discovery_runs"] == 1
    assert result["metrics"]["scanned_documents"] == 3
    # D-6: the anchored term plus its shared coherence bigram surface.
    assert result["metrics"]["discovery_candidates"] == 2
    assert result["metrics"]["candidate_states"] == {"SURFACED": 2}
    assert result["metrics"]["classifier_calls"] == 0
    assert result["metrics"]["telegram_news_outbox"] == 0
    assert result["metrics"]["news_event_outbox"] == 0
    decisions = result["actual_miss_evidence"]["decisions"]
    assert any(item["reason"] == "STOPWORD"
               and item["phrase_text"] == "급증"
               and item["distinct_story_count"] == 3 for item in decisions)
    assert result["actual_miss_evidence"]["dropped_over_cap"] == 0
    assert result["actual_miss_evidence"]["matches_production"] is True
    assert result["snapshot"]["sha256"]
    assert result["snapshot"]["path"].endswith("corpus_snapshot.sqlite3")
    assert not (shadow / "news_activation.json").exists()
    assert (corpus.stat().st_size, corpus.stat().st_mtime_ns, _digest(corpus)) == before


def test_g2_oneshot_reuses_pulse_slot_and_is_duplicate_safe(tmp_path):
    corpus = tmp_path / "canonical" / "external_news_v4.sqlite3"
    shadow = tmp_path / "shadow"
    _corpus(corpus)

    first = run_news_g2_shadow_oneshot(
        root=shadow, corpus_path=corpus, cutoff=CUTOFF,
        observed_now_utc=NOW)
    second = run_news_g2_shadow_oneshot(
        root=shadow, corpus_path=corpus, cutoff=CUTOFF,
        observed_now_utc=NOW)

    assert first["status"] == "SAVED"
    assert second["status"] == "DUPLICATE"
    assert second["metrics"]["discovery_runs"] == 1
    assert second["metrics"]["discovery_candidates"] == 2


def test_g2_oneshot_floors_to_producer_grid_not_only_pulse_grid(tmp_path):
    corpus = tmp_path / "canonical" / "external_news_v4.sqlite3"
    _corpus(corpus)
    observed = NOW + timedelta(minutes=17)

    result = run_news_g2_shadow_oneshot(
        root=tmp_path / "shadow", corpus_path=corpus, cutoff=CUTOFF,
        observed_now_utc=observed)

    assert result["scheduled_for_utc"] == NOW.isoformat()
    assert result["actual_miss_evidence"]["matches_production"] is True


def test_g2_oneshot_rejects_nonisolated_root_before_writes(tmp_path):
    corpus = tmp_path / "canonical" / "external_news_v4.sqlite3"
    _corpus(corpus)

    with pytest.raises(ValueError, match="isolated"):
        run_news_g2_shadow_oneshot(
            root=corpus.parent, corpus_path=corpus, cutoff=CUTOFF,
            observed_now_utc=NOW)

    assert tuple(corpus.parent.iterdir()) == (corpus,)


def test_discovery_audit_preserves_suppression_threshold_and_cap_reasons():
    docs = []
    for term_index in range(13):
        term = "후보" + chr(ord("가") + term_index) + "충격"
        for source_index in range(3):
            docs.append(NewsDocument(
                f"{term_index}-{source_index}",
                f"{term} 관련 공동앵커 발생확산 급증",
                f"story-{term_index}-{source_index}",
                f"source-{source_index}"))
    docs.append(NewsDocument(
        "suppressed", "Reuters 기자 반도체충격", "story-suppressed", "source-x"))
    docs.append(NewsDocument(
        "thin", "희소사건", "story-thin", "source-thin"))

    result = discover_topics(docs, holdings=(), cap=12)
    reasons = {item.reason for item in result.decisions}

    # D-6: the shared coherence bigram is one extra candidate over cap.
    assert result.dropped_over_cap == 2
    assert len(result.dropped_candidates) == 2
    assert {"CAP_TRUNCATED", "DOCUMENT_SUPPRESSED", "BELOW_THRESHOLD",
            "STOPWORD"} <= reasons
    assert any(item.reason == "DOCUMENT_SUPPRESSED"
               and item.phrase_text == "reuters 기자"
               and item.document_ids == ("suppressed",)
               for item in result.decisions)
    assert any(item.reason == "BELOW_THRESHOLD"
               and item.phrase_text == "희소사건"
               for item in result.decisions)


def test_actual_rating_english_and_numeric_noise_rejected_but_nvidia_surfaces():
    noise = "주식 상향 목표주 등급 to in 매수 종합 조정 하향 the"
    docs = tuple(NewsDocument(
        f"n{i}", f"NVIDIA {noise} 10", f"story-n{i}", f"source-{i}")
        for i in range(3))

    result = discover_topics(docs, holdings=())
    phrases = {item.phrase_text for item in result.candidates}
    stopped = {item.phrase_text for item in result.decisions
               if item.reason == "STOPWORD"}

    assert {item.key for item in result.candidates} == {"ent:nvidia"}
    assert phrases == {"nvidia"}
    assert set(noise.split()) <= stopped
    assert any(item.reason == "NUMERIC_ONLY" and item.phrase_text == "10"
               for item in result.decisions)


def test_public_entity_alias_is_reserved_ahead_of_generic_cap_noise():
    docs = []
    for index in range(12):
        term = "일반" + chr(ord("가") + index) + "사건"
        for source in range(10):
            docs.append(NewsDocument(
                f"g{index}-{source}", f"{term} 관련 수요폭증 관측확대",
                f"sg{index}-{source}", f"source-{source}"))
    for source in range(3):
        docs.append(NewsDocument(
            f"nv-{source}", "NVIDIA", f"snv-{source}",
            f"source-{source}"))

    result = discover_topics(docs, holdings=(), cap=12)

    assert "ent:nvidia" in {item.key for item in result.candidates}
    # D-6: +1 shared coherence bigram candidate over the cap.
    assert result.dropped_over_cap == 2


def test_entity_reservation_is_bounded_to_one_third_of_cap():
    docs = []
    aliases = ("NVIDIA", "SK hynix", "Google", "Marvell", "Moderna")
    for index, alias in enumerate(aliases):
        for source in range(3):
            docs.append(NewsDocument(
                f"e{index}-{source}", alias, f"se{index}-{source}",
                f"source-{source}"))
    for index in range(12):
        term = "대형" + chr(ord("가") + index) + "사건"
        for source in range(10):
            docs.append(NewsDocument(
                f"m{index}-{source}", f"{term} 관련 수요폭증 관측확대",
                f"sm{index}-{source}", f"source-{source}"))

    result = discover_topics(docs, holdings=(), cap=12)

    entity_count = sum(item.key.startswith("ent:")
                       for item in result.candidates)
    assert entity_count == 4
    assert sum(not item.key.startswith("ent:")
               for item in result.candidates) == 8


def test_stopword_does_not_create_false_nonadjacent_bigram():
    docs = tuple(NewsDocument(
        f"a{i}", "apple in china", f"sa{i}", f"source-{i}")
        for i in range(3))

    result = discover_topics(docs, holdings=())

    phrases = {item.phrase_text for item in result.candidates}
    assert "apple china" not in phrases
    # D-6 (SDS A1 §S): lone unigrams sharing no second anchor are demoted
    # with an auditable reason instead of surfacing.
    assert phrases == set()
    demoted = {item.phrase_text for item in result.decisions
               if item.reason == "NO_SHARED_ANCHOR"}
    assert {"apple", "china"} <= demoted
