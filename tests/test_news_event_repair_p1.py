import sqlite3
from datetime import datetime, timezone

import pytest

from tokmo.programs.news_topic_discovery import discover_topics
from tokmo.records.external_news_query import read_discovery_documents_v1 as read_discovery_documents
from tokmo.values.news_discovery import NewsDiscoveryLaneV1, NewsDocument


# D-5 fixture shim: synthetic sources map deterministically onto the three
# known publisher origins so origin-count thresholds mirror source counts.
_PUBS = ("연합뉴스", "JTBC", "BBC")


def _doc(index: int, title: str, source: str | None = None) -> NewsDocument:
    resolved = source or f"source-{index}"
    return NewsDocument(f"d{index}", title, f"story-{index}", resolved,
                        publisher_name=_PUBS[sum(resolved.encode()) % 3])


def test_unknown_unknown_frequency_source_dedupe_and_cap_log(caplog):
    docs = [_doc(i, f"삼성전자 HBM4 양산 계약 체결 cluster{i}") for i in range(9)]
    result = discover_topics(docs, holdings=set(), threshold=3, cap=12)
    hbm = next(candidate for candidate in result.candidates if candidate.phrase_text == "hbm4")
    assert hbm.distinct_story_count == 9
    # D-5 (SDS A1 §R): the persisted count is now the independent ORIGIN
    # count — nine synthetic feeds resolve onto the three known origins.
    assert hbm.distinct_source_count == 3
    assert hbm.lane in {lane.value for lane in NewsDiscoveryLaneV1}

    many = []
    for token_no in range(20):
        many.extend(
            _doc(1000 + token_no * 10 + source_no,
                 f"novelterm{token_no} 관련 공급확대 지속상승", f"s{source_no}")
            for source_no in range(3)
        )
    capped = discover_topics(many, holdings=set(), threshold=3, cap=12)
    # D-6: the shared coherence bigram itself is one extra candidate.
    assert capped.dropped_over_cap == 9
    assert any("dropped_over_cap=9" in record.message for record in caplog.records)


def test_source_dedupe_is_required_not_document_count():
    docs = [_doc(i, "unknownburst 신기술", "one-source") for i in range(5)]
    result = discover_topics(docs, holdings=set(), threshold=3, cap=12)
    assert not any("unknownburst" in candidate.key for candidate in result.candidates)


def test_public_value_chain_and_macro_aliases_are_real_corpus_candidates():
    docs=[]
    for index in range(3):
        docs.extend((_doc(100+index,"Marvell Google custom silicon",f"mv{index}"),
            _doc(200+index,"Iran oil shipping disruption",f"ir{index}")))
    result=discover_topics(docs,holdings=set(),threshold=3,cap=12)
    by_key={candidate.key:candidate for candidate in result.candidates}
    assert {"ent:marvell","ent:google","phrase:iran-oil"}<=by_key.keys()
    assert by_key["phrase:iran-oil"].lane==NewsDiscoveryLaneV1.MACRO_GEOPOLITICAL.value

def test_value_chain_and_macro_phrase_lanes_without_hardcoded_keys(monkeypatch):
    import tokmo.programs.news_topic_discovery as discovery
    aliases=dict(discovery._PUBLIC_ALIASES)
    aliases["phrase:strait-disruption"]=("strait disruption",)
    monkeypatch.setattr(discovery,"_PUBLIC_ALIASES",aliases)
    docs=[_doc(i,"Marvell Google strait disruption",f"source-{i}") for i in range(3)]
    by_key={item.key:item for item in discovery.discover_topics(docs,holdings=set()).candidates}
    assert {"ent:marvell","ent:google","phrase:strait-disruption"}<=by_key.keys()
    assert by_key["phrase:strait-disruption"].lane==NewsDiscoveryLaneV1.MACRO_GEOPOLITICAL.value


def test_n5_discovery_seam_is_ro_and_query_only(tmp_path):
    path=tmp_path/"n5.sqlite"; connection=sqlite3.connect(path)
    connection.execute("CREATE TABLE news_documents(news_document_id TEXT, title TEXT, title_fingerprint TEXT, content_hash TEXT, source_id TEXT, first_seen_at TEXT, revision_no INTEGER, summary_text TEXT, published_at TEXT, evidence_role TEXT, publisher_name TEXT)")
    connection.execute("INSERT INTO news_documents VALUES('d1','unknown burst','fp',NULL,'s1','2026-08-21T00:00:00+00:00',1,NULL,NULL,'FACT_DISCOVERY',NULL)")
    connection.commit(); connection.close()
    window_from=datetime(2026,8,20,tzinfo=timezone.utc)
    result = read_discovery_documents(path, window_from=window_from, max_rows=100)
    assert result.status=="OK" and result.query_only==1
    assert len(result.documents)==1
    assert result.documents[0].observed_at=="2026-08-21T00:00:00+00:00"
    with pytest.raises(sqlite3.OperationalError):
        read_discovery_documents(path,window_from=window_from,max_rows=100,write_probe=True)


def test_n5_discovery_seam_row_cap_and_absence_are_typed(tmp_path):
    path=tmp_path/"n5.sqlite"; connection=sqlite3.connect(path)
    connection.execute("CREATE TABLE news_documents(news_document_id TEXT, title TEXT, title_fingerprint TEXT, content_hash TEXT, source_id TEXT, first_seen_at TEXT, revision_no INTEGER, summary_text TEXT, published_at TEXT, evidence_role TEXT, publisher_name TEXT)")
    for index in range(4):
        connection.execute("INSERT INTO news_documents VALUES(?,?,?,NULL,?,?,1,NULL,NULL,'FACT_DISCOVERY',NULL)",
            (f"d{index}","cap burst",f"fp{index}",f"s{index}","2026-08-21T00:00:00+00:00"))
    connection.commit(); connection.close()
    window_from=datetime(2026,8,20,tzinfo=timezone.utc)
    capped=read_discovery_documents(path,window_from=window_from,max_rows=3)
    assert capped.status=="CAP_EXCEEDED" and capped.documents==()
    absent=read_discovery_documents(tmp_path/"missing.sqlite",window_from=window_from,max_rows=3)
    assert absent.status=="STORE_UNAVAILABLE" and absent.documents==()
