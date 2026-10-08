from datetime import datetime, timedelta, timezone
import os
import sqlite3
import pytest
from pathlib import Path

from tokmo.programs.external_news_storage import bootstrap_external_news, save_external_news
from tokmo.records.external_news_query import (
    query_local_news_v1, read_discovery_documents_v1, _NEWS_SCAN_SQL_V1,
    _ReadAuthorizerV1,
)
from tokmo.values.ai_infra_capex import LocalNewsQueryV1, NewsQueryStatusV1, QueryPolicyV1, QUERY_POLICY_V1, PRODUCTION_QUERY_POLICY_V1, ClaimantKindV1, canonical_json_bytes
from tokmo.values.external_news import NewsWrite


NOW = datetime(2026, 8, 9, 3, 0, tzinfo=timezone.utc)


def test_real_public_n5_store_is_classified_without_raw_text_or_mutation(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, NewsWrite(canonical_url="https://news.example/a", title="Company GPU capex increase 10 billion", summary_text="invest accelerator", observed_at=NOW, published_at=NOW))
    before = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir()))
    result = query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW))
    after = (path.read_bytes(), path.stat().st_mtime_ns, sorted(p.name for p in path.parent.iterdir()))
    assert result.status is NewsQueryStatusV1.OK
    assert result.coverage.total_matched_rows == 1
    assert result.articles[0].claimant_kind.value == "COMPANY"
    assert result.articles[0].numeric_capex_language_present is True
    assert not hasattr(result.articles[0], "title")
    assert before == after
    assert os.stat(path).st_mode & 0o077 == 0


def test_missing_path_is_noncreating_typed_failure(tmp_path):
    path = tmp_path / "missing" / "store.sqlite3"
    result = query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW))
    assert result.status is NewsQueryStatusV1.STORE_UNAVAILABLE
    assert not path.parent.exists()


def test_schema_mismatch_and_malformed_timestamp_fail_typed(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, NewsWrite(canonical_url="https://news.example/a", title="GPU invest", observed_at=NOW, published_at=NOW))
    connection = sqlite3.connect(path)
    connection.execute("UPDATE news_documents SET published_at=?", ("2Z26-08-09T03:00:00.000000+00:00"[:32],))
    connection.commit(); connection.close()
    assert query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW)).status is NewsQueryStatusV1.MALFORMED_TIMESTAMP
    other = tmp_path / "other" / "external.sqlite3"
    bootstrap_external_news(other, now=NOW)
    connection = sqlite3.connect(other); connection.execute("CREATE TABLE extra(x INTEGER) STRICT"); connection.commit(); connection.close()
    assert query_local_news_v1(other, LocalNewsQueryV1(NOW-timedelta(days=30), NOW)).status is NewsQueryStatusV1.SCHEMA_MISMATCH


# Q01: n3/n4/meta/DDL variants cannot masquerade as exact n5.
@pytest.mark.parametrize("mutation", ["n3", "n4", "ddl_digest", "registry"])
def test_exact_n5_schema_variants_reject(mutation, tmp_path):
    path=tmp_path/mutation/"external.sqlite3"; bootstrap_external_news(path,now=NOW); connection=sqlite3.connect(path)
    if mutation in {"n3","n4"}:
        version=int(mutation[1]); connection.execute("UPDATE schema_meta SET schema_version=?,manifest_revision=?,schema_id=?",(version,version,f"external-news-{mutation}"))
    elif mutation=="ddl_digest": connection.execute("UPDATE schema_meta SET ddl_sha256=?",("0"*64,))
    else: connection.execute("UPDATE source_families SET adapter_key='tampered'")
    connection.commit();connection.close()
    assert query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW)).status is NewsQueryStatusV1.SCHEMA_MISMATCH


def test_nfkc_korean_multi_polarity_and_claimant_ambiguity(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, NewsWrite(canonical_url="https://news.example/a", title="회사 공급업체 ＧＰＵ 투자 축소 10 조원 capex", observed_at=NOW, published_at=NOW))
    article = query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW)).articles[0]
    assert {m.polarity.value for m in article.metadata_matches} == {"SUPPORT", "COUNTER"}
    assert article.claimant_ambiguous and article.claimant_kind.value == "UNSPECIFIED"
    assert article.numeric_capex_language_present


def _save_rows(path, count, *, title="unrelated bulletin", start=0):
    for i in range(start, start + count):
        save_external_news(path, NewsWrite(canonical_url=f"https://news{i % 3}.example/item/{i}", title=f"{title} {i}", observed_at=NOW-timedelta(seconds=i), published_at=NOW-timedelta(seconds=i)))


# Q02/Q14: unsafe inode forms and modes reject without mutation.
def test_path_symlink_hardlink_and_public_mode_fail_closed(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"; bootstrap_external_news(path, now=NOW)
    query = LocalNewsQueryV1(NOW-timedelta(days=30), NOW)
    symlink = path.with_name("link.sqlite3"); symlink.symlink_to(path)
    hardlink = path.with_name("hard.sqlite3"); os.link(path, hardlink)
    os.chmod(path, 0o644)
    for candidate in (path, symlink, hardlink):
        assert query_local_news_v1(candidate, query).status is NewsQueryStatusV1.STORE_UNAVAILABLE
    assert symlink.is_symlink() and hardlink.exists()


def test_path_owner_mismatch_rejects_before_sqlite_open(monkeypatch,tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);before=path.read_bytes()
    monkeypatch.setattr(os,"geteuid",lambda:os.stat(path).st_uid+1)
    assert query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW)).status is NewsQueryStatusV1.STORE_UNAVAILABLE
    assert path.read_bytes()==before


# Q08/Q15: the same production loop is exercised with immutable small caps.
def test_policy_boundaries_database_temporal_bytes_and_output(tmp_path):
    query = LocalNewsQueryV1(NOW-timedelta(days=30), NOW)
    path = tmp_path / "db" / "external.sqlite3"; bootstrap_external_news(path, now=NOW); _save_rows(path, 3)
    assert query_local_news_v1(path, query, policy=QueryPolicyV1(3, 3, 4096, 2, 262144)).status is NewsQueryStatusV1.OK
    assert query_local_news_v1(path, query, policy=QueryPolicyV1(2, 2, 4096, 2, 262144)).error_code == "database_validation_rows"
    assert query_local_news_v1(path, query, policy=QueryPolicyV1(3, 2, 4096, 2, 262144)).error_code == "temporal_envelope"
    matched = tmp_path / "matched" / "external.sqlite3"; bootstrap_external_news(matched, now=NOW); _save_rows(matched, 3, title="GPU overview")
    result = query_local_news_v1(matched, query, policy=QueryPolicyV1(3, 3, 4096, 2, 262144))
    assert result.status is NewsQueryStatusV1.OK and result.coverage.total_matched_rows == 3
    assert result.coverage.returned_count == 2 and result.coverage.truncated_rows == 1
    assert result.query_digest!=query_local_news_v1(matched,query).query_digest
    assert QUERY_POLICY_V1 == QueryPolicyV1(100000, 50000, 67108864, 200, 262144)


def test_normalized_and_serialized_byte_caps_exact_plus_one(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,1,title="GPU overview")
    query=LocalNewsQueryV1(NOW-timedelta(days=30),NOW); baseline=query_local_news_v1(path,query)
    normalized=baseline.coverage.temporal_candidate_normalized_bytes; article_bytes=len(canonical_json_bytes(baseline.articles[0]))
    exact=query_local_news_v1(path,query,policy=QueryPolicyV1(1,1,normalized,1,article_bytes))
    assert exact.status is NewsQueryStatusV1.OK and exact.coverage.returned_count==1
    assert query_local_news_v1(path,query,policy=QueryPolicyV1(1,1,normalized-1,1,article_bytes)).error_code=="temporal_envelope"
    clipped=query_local_news_v1(path,query,policy=QueryPolicyV1(1,1,normalized,1,article_bytes-1))
    assert clipped.status is NewsQueryStatusV1.OK and clipped.coverage.returned_count==0 and clipped.coverage.truncated_rows==1


# Q09/Q14: writer lock produces typed retry-0 failure and creates no sidecar.
def test_busy_locked_is_prompt_and_sidecar_free(tmp_path):
    path = tmp_path / "private" / "external.sqlite3"; bootstrap_external_news(path, now=NOW); _save_rows(path, 1, title="GPU overview")
    connection = sqlite3.connect(path, isolation_level=None); connection.execute("BEGIN EXCLUSIVE")
    before = sorted(p.name for p in path.parent.iterdir())
    try:
        result = query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW))
    finally:
        connection.execute("ROLLBACK"); connection.close()
    assert result.status is NewsQueryStatusV1.BUSY_OR_LOCKED
    assert sorted(p.name for p in path.parent.iterdir()) == before


def test_database_parent_and_preexisting_sidecars_unchanged_across_outcomes(tmp_path):
    query=LocalNewsQueryV1(NOW-timedelta(days=30),NOW)
    for case in ("success","schema","timestamp","cap","preexisting"):
        path=tmp_path/case/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,2,title="GPU overview")
        connection=sqlite3.connect(path)
        if case=="schema":connection.execute("UPDATE schema_meta SET ddl_sha256=?",("0"*64,))
        elif case=="timestamp":connection.execute("PRAGMA ignore_check_constraints=ON");connection.execute("UPDATE news_documents SET published_at=?",("2Z26-08-09T03:00:00.000000+00:00",))
        connection.commit();connection.close()
        sidecars=[]
        if case=="preexisting":
            for suffix in ("-wal","-shm","-journal"):
                side=Path(str(path)+suffix);side.write_bytes((case+suffix).encode());sidecars.append(side)
        before=(path.read_bytes(),path.stat().st_mtime_ns,path.parent.stat().st_mtime_ns,tuple((s.name,s.read_bytes(),s.stat().st_mtime_ns) for s in sidecars))
        policy=QueryPolicyV1(1,1,4096,1,262144) if case=="cap" else PRODUCTION_QUERY_POLICY_V1
        result=query_local_news_v1(path,query,policy=policy)
        expected={"success":NewsQueryStatusV1.OK,"schema":NewsQueryStatusV1.SCHEMA_MISMATCH,"timestamp":NewsQueryStatusV1.MALFORMED_TIMESTAMP,"cap":NewsQueryStatusV1.CAP_EXCEEDED,"preexisting":NewsQueryStatusV1.CORRUPT}[case]
        assert result.status is expected
        after=(path.read_bytes(),path.stat().st_mtime_ns,path.parent.stat().st_mtime_ns,tuple((s.name,s.read_bytes(),s.stat().st_mtime_ns) for s in sidecars))
        assert after==before


# Q15: all three timestamp columns are parsed before eligibility.
@pytest.mark.parametrize("column,bad", [("published_at", "2Z26-08-09T03:00:00.000000+00:00"), ("first_seen_at", "102X-08-09T03:00:00.000000+00:00"), ("last_seen_at", "2Z26-08-09T03:00:00.000000+00:00")])
def test_every_timestamp_column_malformed_fails(column, bad, tmp_path):
    path = tmp_path / column / "external.sqlite3"; bootstrap_external_news(path, now=NOW); _save_rows(path, 1, title="GPU overview")
    connection = sqlite3.connect(path); connection.execute("PRAGMA ignore_check_constraints=ON"); connection.execute(f"UPDATE news_documents SET {column}=?", (bad,)); connection.commit(); connection.close()
    assert query_local_news_v1(path, LocalNewsQueryV1(NOW-timedelta(days=30), NOW)).status is NewsQueryStatusV1.MALFORMED_TIMESTAMP


def test_future_source_skew_and_revision_availability_are_excluded_and_counted(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW)
    save_external_news(path,NewsWrite(canonical_url="https://future.example/source",title="GPU overview future source",observed_at=NOW,published_at=NOW+timedelta(microseconds=1)))
    save_external_news(path,NewsWrite(canonical_url="https://future.example/revision",title="GPU overview future revision",observed_at=NOW,published_at=NOW))
    connection=sqlite3.connect(path);connection.execute("UPDATE news_documents SET revision_no=2,last_seen_at=? WHERE canonical_url LIKE '%revision'",((NOW+timedelta(microseconds=1)).isoformat(timespec="microseconds"),));connection.commit();connection.close()
    result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW))
    assert result.coverage.future_source_skew_count==1 and result.coverage.future_availability_count==1
    assert result.coverage.total_matched_rows==0


# Q03/Q04/Q07/Q10/Q13: exact temporal bounds, revision availability, and snapshot mutation.
def test_temporal_boundaries_revision_availability_and_snapshot_binding(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW-timedelta(days=40))
    for i,at in enumerate((NOW-timedelta(days=30),NOW-timedelta(days=30,microseconds=1),NOW)):
        save_external_news(path,NewsWrite(canonical_url=f"https://time.example/{i}",title=f"GPU overview boundary {i}",observed_at=at,published_at=at))
    q=LocalNewsQueryV1(NOW-timedelta(days=30),NOW); first=query_local_news_v1(path,q)
    assert first.coverage.total_matched_rows==2 and first.coverage.available_effective_from==NOW-timedelta(days=30)
    connection=sqlite3.connect(path);connection.execute("UPDATE news_documents SET summary_text='semantic mutation' WHERE published_at<?",((NOW-timedelta(days=30)).isoformat(timespec="microseconds"),));connection.commit();connection.close()
    second=query_local_news_v1(path,q)
    assert second.source_snapshot_digest!=first.source_snapshot_digest and second.result_digest!=first.result_digest
    with pytest.raises(ValueError): LocalNewsQueryV1((NOW-timedelta(days=30)).replace(tzinfo=None),NOW)
    with pytest.raises(ValueError): LocalNewsQueryV1(NOW-timedelta(days=30),NOW.astimezone(timezone(timedelta(hours=9))))


# Q06/Q12: content hash wins over title fingerprint and copyrighted text never exits.
def test_content_hash_story_dedupe_unicode_boundary_and_no_text(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW); shared="a"*64
    for i in range(2):
        save_external_news(path,NewsWrite(canonical_url=f"https://dedupe{i}.example/a",title="ＧＰＵ overview "+"가"*600,summary_text="summary sentinel "+"나"*1100,content_hash=shared,observed_at=NOW,published_at=NOW))
    result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW))
    assert result.coverage.total_matched_rows==2 and result.coverage.total_matched_distinct_stories==1
    assert result.articles[0].normalized_excerpt_bytes<=1536
    encoded=canonical_json_bytes(result).decode()
    assert "summary sentinel" not in encoded and "가"*21 not in encoded and "https://" not in encoded


# Q16/Q20: isolated integrity succeeds; the exact production SELECT plans a full table scan.
def test_isolated_integrity_and_truthful_full_scan_plan(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,1,title="GPU overview")
    connection=sqlite3.connect(path)
    assert connection.execute("PRAGMA quick_check").fetchone()==("ok",) and connection.execute("PRAGMA foreign_key_check").fetchone() is None
    columns="news_document_id,source_id,source_article_id,title_fingerprint,content_hash,content_mode,evidence_role,publisher_name,published_at,first_seen_at,last_seen_at,revision_no,content_state,title,summary_text"
    plan=connection.execute("EXPLAIN QUERY PLAN "+_NEWS_SCAN_SQL_V1.format(columns=columns)).fetchall();connection.close()
    assert any("SCAN news_documents" in row[3] for row in plan)
    def guard(sql):assert sql=="SELECT {columns} FROM news_documents ORDER BY news_document_id ASC" and " WHERE " not in sql.upper()
    guard(_NEWS_SCAN_SQL_V1)
    with pytest.raises(AssertionError):guard(_NEWS_SCAN_SQL_V1.replace(" ORDER BY"," WHERE title LIKE '%GPU%' ORDER BY"))


# Q08: unrelated temporal rows consume only the raw envelope, never hide matches.
def test_more_than_2000_unrelated_plus_five_relevant_returns_all_five(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,2001)
    _save_rows(path,5,title="GPU overview",start=3000)
    result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW))
    assert result.status is NewsQueryStatusV1.OK and result.coverage.temporal_candidate_count==2006
    assert result.coverage.total_matched_rows==5 and result.coverage.returned_count==5


# Q05/Q17: physical rowid rewrite cannot alter canonical ordering or digests.
def test_vacuum_rowid_permutation_is_semantically_invariant(tmp_path):
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,8,title="GPU overview")
    query=LocalNewsQueryV1(NOW-timedelta(days=30),NOW);before=query_local_news_v1(path,query)
    connection=sqlite3.connect(path);connection.execute("VACUUM");connection.close();after=query_local_news_v1(path,query)
    assert canonical_json_bytes(before)==canonical_json_bytes(after)


def test_insertion_order_permutation_is_canonically_invariant(tmp_path):
    query=LocalNewsQueryV1(NOW-timedelta(days=30),NOW);rows=list(range(6));results=[]
    for label,order in (("forward",rows),("reverse",list(reversed(rows)))):
        path=tmp_path/label/"external.sqlite3";bootstrap_external_news(path,now=NOW-timedelta(days=1))
        for i in order:save_external_news(path,NewsWrite(canonical_url=f"https://stable{i%2}.example/{i}",title=f"GPU overview {i}",observed_at=NOW-timedelta(minutes=i),published_at=NOW-timedelta(minutes=i)))
        results.append(query_local_news_v1(path,query))
    assert results[0].all_match_semantics==results[1].all_match_semantics
    assert [(a.available_at,a.title_fingerprint,a.source_id) for a in results[0].articles]==[(a.available_at,a.title_fingerprint,a.source_id) for a in results[1].articles]


# Q09: authorizer permits only its own reads and one BEGIN/ROLLBACK, never self-blocks.
def test_authorizer_allowlist_and_normative_sequence_are_pinned():
    authorizer=_ReadAuthorizerV1()
    assert authorizer(sqlite3.SQLITE_SELECT,None,None,None,None)==sqlite3.SQLITE_OK
    assert authorizer(sqlite3.SQLITE_TRANSACTION,"BEGIN",None,None,None)==sqlite3.SQLITE_OK
    assert authorizer(sqlite3.SQLITE_TRANSACTION,"BEGIN",None,None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_TRANSACTION,"ROLLBACK",None,None,None)==sqlite3.SQLITE_OK
    assert authorizer(sqlite3.SQLITE_TRANSACTION,"ROLLBACK",None,None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_TRANSACTION,"COMMIT",None,None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_INSERT,"news_documents",None,None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_READ,"intake_receipts","receipt_id",None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_PRAGMA,"journal_mode","WAL",None,None)==sqlite3.SQLITE_DENY
    assert authorizer(sqlite3.SQLITE_PRAGMA,"query_only","OFF",None,None)==sqlite3.SQLITE_DENY
    source=Path("src/tokmo/records/external_news_query.py").read_text()
    ordered=["PRAGMA query_only=ON","PRAGMA busy_timeout=0","PRAGMA temp_store=MEMORY","PRAGMA query_only\").fetchone","set_authorizer(authorizer)","BEGIN DEFERRED","PRAGMA data_version","ROLLBACK"]
    cursor=0
    for token in ordered:
        cursor=source.index(token,cursor)+len(token)


def test_symlinked_grandparent_is_rejected_without_creating_or_mutating(tmp_path):
    real=tmp_path/"real";real.mkdir(mode=0o700);path=real/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW)
    link=tmp_path/"linked";link.symlink_to(real,target_is_directory=True);candidate=link/"private"/"external.sqlite3";before=path.read_bytes()
    result=query_local_news_v1(candidate,LocalNewsQueryV1(NOW-timedelta(days=30),NOW))
    assert result.status is NewsQueryStatusV1.STORE_UNAVAILABLE and path.read_bytes()==before and link.is_symlink()


def test_streaming_validation_does_not_retain_out_of_window_full_text(tmp_path):
    import tracemalloc
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW-timedelta(days=40))
    for i in range(6):
        at=NOW-timedelta(days=31,seconds=i);save_external_news(path,NewsWrite(canonical_url=f"https://old.example/{i}",title=f"old {i}",summary_text="x",observed_at=at,published_at=at))
    payload="z"*(1024*1024);connection=sqlite3.connect(path);connection.execute("UPDATE news_documents SET summary_text=?",(payload,));connection.commit();connection.close();del payload
    tracemalloc.start();result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW));_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert result.status is NewsQueryStatusV1.OK and result.coverage.database_validation_row_count==6 and result.coverage.temporal_candidate_count==0
    assert peak<4*1024*1024


def test_in_window_unbounded_metadata_is_hashed_before_temporal_retention(tmp_path):
    import tracemalloc
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW-timedelta(days=1))
    for i in range(8):save_external_news(path,NewsWrite(canonical_url=f"https://meta{i}.example/a",title=f"GPU overview {i}",observed_at=NOW-timedelta(minutes=i),published_at=NOW-timedelta(minutes=i)))
    connection=sqlite3.connect(path)
    for i,(doc,) in enumerate(connection.execute("SELECT news_document_id FROM news_documents ORDER BY news_document_id")):
        huge=(str(i)+"x"*(1024*1024-1));connection.execute("UPDATE news_documents SET source_article_id=?,publisher_name=? WHERE news_document_id=?",(huge,huge,doc))
    connection.commit();connection.close();del huge
    tracemalloc.start();result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW),policy=QueryPolicyV1(8,8,4096,8,262144));_,peak=tracemalloc.get_traced_memory();tracemalloc.stop()
    assert result.status is NewsQueryStatusV1.OK and result.coverage.temporal_candidate_count==8 and result.coverage.returned_count==8
    assert peak<8*1024*1024 and all(a.source_article_id_digest and a.publisher_name_digest for a in result.articles)


# reviewer m1 / D-CAP-01: adapter member drift is typed CAP_EXCEEDED, not construction ValueError.
def test_adapter_semantic_member_cap_overflow_is_typed_non_ok(monkeypatch,tmp_path):
    from tokmo.values.ai_infra_capex import MetadataMatchV1,CapexBucketV1,CapexEvidencePolarityV1
    path=tmp_path/"private"/"external.sqlite3";bootstrap_external_news(path,now=NOW);_save_rows(path,1,title="GPU overview")
    triples=[]
    for bucket in CapexBucketV1:
        for polarity in CapexEvidencePolarityV1:
            triples.append(MetadataMatchV1(f"drift-{len(triples)}",bucket,polarity))
    monkeypatch.setattr("tokmo.records.external_news_query._classify",lambda text,role:(tuple(triples[:9]),ClaimantKindV1.UNSPECIFIED,False,False))
    result=query_local_news_v1(path,LocalNewsQueryV1(NOW-timedelta(days=30),NOW),policy=QueryPolicyV1(1,1,4096,1,262144))
    assert result.status is NewsQueryStatusV1.CAP_EXCEEDED and result.error_code=="semantic_member_cap" and result.articles==()


def test_discovery_window_coalesces_published_at_over_first_seen(tmp_path):
    """R-BKL-1 (S16): a recovered old story (published_at 4 days back,
    first_seen_at now) is excluded from the 72 h discovery window, while a
    NULL-published_at document with fresh first_seen_at stays included."""
    path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=NOW)
    save_external_news(path, NewsWrite(
        canonical_url="https://news.example/recovered-old",
        title="Recovered outage backlog story", summary_text="old backlog",
        observed_at=NOW, published_at=NOW - timedelta(days=4)))
    save_external_news(path, NewsWrite(
        canonical_url="https://news.example/fresh-unstamped",
        title="Fresh unstamped story", summary_text="fresh",
        observed_at=NOW, published_at=None))
    result = read_discovery_documents_v1(
        path, window_from=NOW - timedelta(hours=72), max_rows=10)
    assert result.status == "OK"
    titles = {document.title for document in result.documents}
    assert titles == {"Fresh unstamped story"}
