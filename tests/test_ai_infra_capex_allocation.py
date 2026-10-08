from datetime import datetime, timedelta, timezone
from dataclasses import replace
import json
from pathlib import Path
from unittest.mock import patch
import uuid

import pytest

from tokmo.programs.ai_infra_capex_allocation import evaluate_ai_infra_capex_allocation_v1
from tokmo.programs.external_news_storage import bootstrap_external_news, save_external_news
from tokmo.records.external_news import ExternalNewsStore
from tokmo.records.external_news_query import query_local_news_v1
from tokmo.values.ai_infra_capex import SPEC_DIGEST_V1, LocalNewsQueryV1, QueryPolicyV1, PRODUCTION_QUERY_POLICY_V1, canonical_json_bytes, digest_v1
from tokmo.values.external_news import ContentMode, EvidenceRole, NewsWrite
from tokmo.values.news_connectors import BBC_BUSINESS_RSS, INVESTING_ANALYST_RSS, YONHAP_MARKET_RSS
from tokmo.values.signal_program_lifecycle import (
    ProgramKindV1, ProgramLifecycleSpecV1, ProgramLifecycleStateV1,
    ProgramLifecycleStatusV1, evaluate_lifecycle_status,
)

UTC = timezone.utc
CONNECTORS = {
    "BBC_BUSINESS_RSS": BBC_BUSINESS_RSS,
    "YONHAP_MARKET_RSS": YONHAP_MARKET_RSS,
    "INVESTING_ANALYST_RSS": INVESTING_ANALYST_RSS,
}


def _dt(raw):
    return datetime.fromisoformat(raw.replace("Z", "+00:00"))


def _spec(activated):
    return ProgramLifecycleSpecV1(
        "ai-infra-capex-allocation-v1", "1.0.0", ProgramKindV1.HYPOTHESIS_SENSOR,
        activated, activated + timedelta(days=45), activated + timedelta(days=120),
        45, SPEC_DIGEST_V1,
    )


def _state(as_of, *, status=None, activated=None, renewal_count=0):
    activated = activated or datetime(2026, 8, 1, 3, tzinfo=UTC)
    if status is None:
        return evaluate_lifecycle_status(_spec(activated), as_of=as_of, last_evidence_at=None, evidence_count=0, renewal_count=renewal_count)
    return ProgramLifecycleStateV1(_spec(activated), status, None, 0, renewal_count, as_of)


def _seed_public_store(tmp_path):
    seed = json.loads(Path("tests/fixtures/ai_infra_capex/news_n5_seed_v1.json").read_text())
    as_of = _dt(seed["as_of"]); path = tmp_path / "private" / "external.sqlite3"
    bootstrap_external_news(path, now=_dt(seed["activation"]))
    store = ExternalNewsStore(path); ids=iter(uuid.UUID(int=i) for i in range(1,1000))
    with patch("tokmo.records.external_news.uuid.uuid4",side_effect=lambda:next(ids)):
        for row in seed["rows"]:
            spec = CONNECTORS[row["connector"]]
            observed = as_of + timedelta(hours=row["observed_offset_hours"])
            published = as_of + timedelta(hours=row["published_offset_hours"])
            write = NewsWrite(
                canonical_url=row["url"], source_article_id=row["id"], title=row["title"],
                summary_text=row["summary"], observed_at=observed, published_at=published,
                content_mode=ContentMode(row["mode"]), evidence_role=EvidenceRole(row["role"]),
            )
            store.save_connector_news(spec, write, target_ref=f"fixture:{row['id']}")
            if "revision" in row:
                rev = row["revision"]
                store.save_connector_news(spec, NewsWrite(
                    canonical_url=row["url"], source_article_id=row["id"], title=rev["title"],
                    summary_text=rev["summary"], observed_at=as_of + timedelta(hours=rev["observed_offset_hours"]),
                    published_at=published, content_mode=ContentMode(row["mode"]),
                    evidence_role=EvidenceRole(row["role"]),
                ), target_ref=f"fixture:{row['id']}:revision")
    return path, as_of, _dt(seed["activation"])


# LC-C01..04, S05, S10, S12
def test_public_fixture_vector_lifecycle_and_exact_pack(tmp_path):
    path, as_of, activated = _seed_public_store(tmp_path)
    query = LocalNewsQueryV1(as_of - timedelta(days=30), as_of)
    result = query_local_news_v1(path, query)
    expected = json.loads(Path("tests/fixtures/ai_infra_capex/expected_packs_v1.json").read_text())["vectors"][0]
    for at, status in (
        (activated - timedelta(microseconds=1), ProgramLifecycleStatusV1.SHADOW),
        (activated + timedelta(days=120), ProgramLifecycleStatusV1.EXPIRED),
    ):
        calls = []
        blocked_query = LocalNewsQueryV1(at - timedelta(days=30), at)
        run = evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(at, activated=activated), query=blocked_query, query_local_news=lambda q: calls.append(q))
        assert run.pack is None and run.blocked_reason is not None and calls == []
    for status in (ProgramLifecycleStatusV1.DORMANT, ProgramLifecycleStatusV1.RETIRED):
        calls = []
        run = evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of, status=status, activated=activated), query=query, query_local_news=lambda q: calls.append(q))
        assert run.pack is None and run.blocked_reason.value == status.value and calls == []
    for at in (activated, activated + timedelta(days=45), activated + timedelta(days=120) - timedelta(microseconds=1)):
        q = LocalNewsQueryV1(at - timedelta(days=30), at); calls = []
        run = evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(at, activated=activated), query=q, query_local_news=lambda actual: calls.append(actual) or query_local_news_v1(path,actual))
        assert calls == [q]
        assert run.pack is not None and run.blocked_reason is None
        assert canonical_json_bytes(run.pack)
    run = evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of, activated=activated), query=query, query_local_news=lambda q: result)
    pack = run.pack
    assert pack.state.value == expected["state"]
    assert result.query_digest == expected["query_digest"]
    assert result.source_snapshot_digest == expected["source_snapshot_digest"]
    assert result.result_digest == expected["query_result_digest"]
    assert result.all_match_semantics.all_match_semantic_digest == expected["all_match_semantic_digest"]
    assert pack.lifecycle_digest == expected["lifecycle_digest"]
    assert [n.need_digest for n in pack.missing_needs] == expected["need_digests"]
    assert pack.pack_digest == expected["pack_digest"]
    assert result.coverage.total_matched_rows == expected["total_matched_rows"]
    assert result.coverage.total_matched_distinct_stories == expected["total_matched_distinct_stories"]
    assert len(pack.evidence) == expected["evidence_count"]
    assert len(pack.contradictions) == expected["contradiction_count"]
    assert len(pack.missing_needs) == expected["missing_need_count"]
    repeat=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of,activated=activated),query=query,query_local_news=lambda _:result).pack
    assert canonical_json_bytes(repeat)==canonical_json_bytes(pack)
    rendered=canonical_json_bytes(pack).decode().casefold()
    for forbidden in ("https://","/users/","bearer ","api_key","password"," buy "," sell ","%"," usd "," krw ","because"):
        assert forbidden not in rendered


# LC-C05: renewal/reactivation is forbidden, never swallowed.
def test_lifecycle_renewal_and_wrong_period_reject_before_query():
    at = datetime(2026, 8, 9, tzinfo=UTC); query = LocalNewsQueryV1(at-timedelta(days=30), at)
    calls = []
    with pytest.raises(ValueError, match="frozen G1-C lifecycle"):
        evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(at, renewal_count=1), query=query, query_local_news=lambda q: calls.append(q))
    lifecycle=_state(at);extended=replace(lifecycle,spec=replace(lifecycle.spec,expires_at=lifecycle.spec.expires_at+timedelta(microseconds=1)))
    with pytest.raises(ValueError,match="frozen G1-C lifecycle"):
        evaluate_ai_infra_capex_allocation_v1(lifecycle=extended,query=query,query_local_news=lambda q:calls.append(q))
    assert calls == []


# LC-C06, S07/S08/S11/S12/S16: strict cross-field construction cannot be bypassed.
def test_public_pack_and_nested_values_reject_counterfactual_mutations(tmp_path):
    path, as_of, activated = _seed_public_store(tmp_path); query = LocalNewsQueryV1(as_of-timedelta(days=30), as_of)
    result = query_local_news_v1(path, query)
    pack = evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of, activated=activated), query=query, query_local_news=lambda q: result).pack
    bucket = result.all_match_semantics.bucket_aggregates[0]
    mutations = [
        lambda: replace(result.coverage, total_matched_rows=-1),
        lambda: replace(bucket, net_points=bucket.net_points + 1),
        lambda: replace(result.all_match_semantics, destination_story_count=-1),
        lambda: replace(result, status=type(result.status).OK, all_match_semantics=None),
        lambda: replace(pack.assessments[0], net_points=99),
        lambda: replace(pack.evidence[0], descriptor="https://secret.example/order buy"),
        lambda: replace(pack.missing_needs[0], automatic_search_starts=True),
        lambda: replace(pack, privacy_scan_passed=False),
        lambda: replace(pack, evidence=pack.evidence + (pack.evidence[0],) * 40),
    ]
    for mutate in mutations:
        with pytest.raises(ValueError):
            mutate()
    polarity=bucket.polarity_aggregates[0]
    for field in ("distinct_story_count","uncapped_points","claimant_ambiguous_story_count","numeric_capex_story_count","distinct_source_count"):
        with pytest.raises(ValueError,match="invalid"):replace(polarity,**{field:True})
    with pytest.raises(ValueError,match="claimant_story_count"):replace(polarity,claimant_story_counts=(True,)+polarity.claimant_story_counts[1:])
    with pytest.raises(ValueError,match="closed news classification"):replace(result.articles[0],content_mode="BLOG")
    with pytest.raises(ValueError,match="closed news classification"):replace(result.articles[0],evidence_role="QUOTE")
    with pytest.raises(ValueError,match="assessment bucket enum"):replace(pack.assessments[0],bucket=pack.assessments[0].bucket.value)
    with pytest.raises(ValueError,match="state/lifecycle"):replace(pack,state=pack.state.value)
    with pytest.raises(ValueError,match="state/lifecycle"):replace(pack,lifecycle_status="ACTIVE ")
    assert all(e.evidence_class.value != "VERIFIED_FACT" for e in pack.evidence)
    assert all(n.automatic_search_starts == 0 for n in pack.missing_needs)


def _bulk_public_store(tmp_path, rows):
    path=tmp_path/"private"/"bulk.sqlite3"; bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC)); as_of=datetime(2026,8,9,3,tzinfo=UTC)
    for i,(title,mode,role) in enumerate(rows):
        save_external_news(path,NewsWrite(canonical_url=f"https://source{i%2}.example/items/{i}",title=f"{title} story-{i}",observed_at=as_of-timedelta(minutes=i),published_at=as_of-timedelta(minutes=i),content_mode=mode,evidence_role=role))
    return path,as_of


# Q17..19, S02..06, S13/S16: sole semantic triggers after returned row 200 remain authoritative.
def test_all_match_semantics_beyond_row_200_cannot_weaken_state(tmp_path):
    context=[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*200
    tail=[
        ("GPU increase cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),
        ("supplier GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),
        ("analyst GPU overview",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY),
        ("company supplier GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),
        ("GPU capex 10 billion",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),
        ("hyperscaler cloud capex overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),
    ]
    path,as_of=_bulk_public_store(tmp_path,context+tail); query=LocalNewsQueryV1(as_of-timedelta(days=30),as_of); result=query_local_news_v1(path,query)
    assert result.coverage.total_matched_rows==206 and result.coverage.returned_count==200
    sem=result.all_match_semantics; gpu=sem.bucket_aggregates[0]
    assert gpu.conflict and sem.numeric_capex_story_count and sem.claimant_ambiguous_story_count and sem.hyperscaler_context_story_count
    assert sem.claimant_story_counts[1]>0 and sem.claimant_story_counts[2]>0
    pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=query,query_local_news=lambda q:result).pack
    assert pack.state.value=="NEEDS_CONFIRMATION"
    assert any(x.omitted_distinct_story_count for x in pack.omitted_evidence_summaries)
    assert {n.reason_code.value for n in pack.missing_needs}>={"ATTRIBUTED_AMOUNT_UNVERIFIED","SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION","ANALYST_CLAIM_UNVERIFIED","CONTRADICTION_UNRESOLVED"}


# S17/S18: selection boundaries preserve all-match semantics and digest-only omission accounting.
def test_evidence_40_41_and_missing_need_16_17_boundaries(tmp_path):
    exact_path,exact_as_of=_bulk_public_store(tmp_path/"exact40",[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*40);exact_q=LocalNewsQueryV1(exact_as_of-timedelta(days=30),exact_as_of);exact_result=query_local_news_v1(exact_path,exact_q);exact_pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(exact_as_of),query=exact_q,query_local_news=lambda _:exact_result).pack
    assert len(exact_pack.evidence)==40 and exact_pack.omitted_evidence_summaries==()
    path,as_of=_bulk_public_store(tmp_path,[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*41); query=LocalNewsQueryV1(as_of-timedelta(days=30),as_of); result=query_local_news_v1(path,query)
    pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=query,query_local_news=lambda q:result).pack
    assert len(pack.evidence)==40 and sum(s.omitted_distinct_story_count for s in pack.omitted_evidence_summaries)==1
    terms=("GPU","interconnect","edge security","cloud capex")
    rows=[]
    for term in terms:
        rows.extend([(f"supplier {term} increase",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),(f"analyst {term} increase",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY),(f"company {term} increase cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),(f"company {term} capex 10 billion increase",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)])
    rich,as_of=_bulk_public_store(tmp_path/"rich",rows); q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of); rr=query_local_news_v1(rich,q); rich_pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:rr).pack
    assert len(rich_pack.missing_needs)==16 and rich_pack.omitted_need_summary is not None and rich_pack.omitted_need_summary.total_omitted>=1
    assert sum(n for _,n in rich_pack.omitted_need_summary.reason_counts)==rich_pack.omitted_need_summary.total_omitted


# S01/S05/S06/S09/S10: public-store discovery, stale, empty, and query failure semantics.
def test_state_coverage_freshness_and_typed_failure_semantics(tmp_path):
    cases=[]
    discovery,as_of=_bulk_public_store(tmp_path/"discovery",[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*3)
    cases.append((discovery,as_of,"DISCOVERY_ONLY"))
    stale_path=tmp_path/"stale"/"private"/"bulk.sqlite3";bootstrap_external_news(stale_path,now=datetime(2026,8,1,tzinfo=UTC));
    for i in range(3): save_external_news(stale_path,NewsWrite(canonical_url=f"https://stale{i%2}.example/{i}",title=f"GPU overview {i}",observed_at=as_of-timedelta(hours=72,microseconds=1),published_at=as_of-timedelta(hours=72,microseconds=1)))
    cases.append((stale_path,as_of,"INSUFFICIENT_LOCAL_EVIDENCE"))
    empty=tmp_path/"empty"/"private"/"bulk.sqlite3";bootstrap_external_news(empty,now=datetime(2026,8,1,tzinfo=UTC));cases.append((empty,as_of,"INSUFFICIENT_LOCAL_EVIDENCE"))
    for path,at,expected in cases:
        q=LocalNewsQueryV1(at-timedelta(days=30),at); result=query_local_news_v1(path,q); pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(at),query=q,query_local_news=lambda _:result).pack
        assert pack.state.value==expected
        if expected.startswith("INSUFFICIENT"): assert pack.missing_needs
    missing=tmp_path/"does-not-exist"/"store.sqlite3";q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);failure=query_local_news_v1(missing,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:failure).pack
    assert pack.state.value=="INSUFFICIENT_LOCAL_EVIDENCE" and {n.reason_code.value for n in pack.missing_needs}=={"QUERY_FAILED"}


def test_exact_72_hours_is_fresh_not_stale(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"fresh.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC))
    for i in range(3):save_external_news(path,NewsWrite(canonical_url=f"https://fresh{i%2}.example/{i}",title=f"GPU overview {i}",observed_at=as_of-timedelta(hours=72),published_at=as_of-timedelta(hours=72)))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    assert pack.state.value=="DISCOVERY_ONLY" and all(n.reason_code.value!="STALE_LOCAL_NEWS" for n in pack.missing_needs)


def test_duplicate_story_rows_cluster_into_one_bounded_evidence_reference(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"cluster.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC));shared="d"*64
    for i in range(9):save_external_news(path,NewsWrite(canonical_url=f"https://cluster{i}.example/a",title="company GPU increase",content_hash=shared,observed_at=as_of-timedelta(seconds=i),published_at=as_of-timedelta(seconds=i)))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    assert len(pack.evidence)==1 and len(pack.evidence[0].news_document_ids)==8 and tuple(sorted(pack.evidence[0].news_document_ids))==pack.evidence[0].news_document_ids


@pytest.mark.parametrize("unsafe",["contact ops@example.com","call +1 (212) 555-0100","https://bad.example","/Users/name/key","api_key secret","buy order advice"])
def test_structural_privacy_scan_rejects_every_free_text_threat(tmp_path,unsafe):
    path,as_of=_bulk_public_store(tmp_path,[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*3);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    with pytest.raises(ValueError,match="unsafe"):replace(pack,limitations=(unsafe,))


# D-C1-01/D-PRIV-01: every claimant×numeric×polarity member outside output drives an exact summary.
@pytest.mark.parametrize("claimant_text,claimant",[("company","COMPANY"),("supplier","SUPPLIER"),("analyst","ANALYST_OR_OPINION"),("bulletin","UNSPECIFIED")])
@pytest.mark.parametrize("numeric",[False,True])
@pytest.mark.parametrize("polarity,marker,points",[("SUPPORT","increase",1),("COUNTER","cut",1),("CONTEXT","overview",0)])
def test_delta_polarity_member_matrix_outside_output(claimant_text,claimant,numeric,polarity,marker,points,tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"matrix.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC))
    save_external_news(path,NewsWrite(canonical_url="https://recent.example/a",title="GPU overview recent",observed_at=as_of,published_at=as_of))
    title=f"{claimant_text} GPU {marker}"+(" capex 10 billion" if numeric else "")
    mode=ContentMode.OPINION if claimant=="ANALYST_OR_OPINION" else ContentMode.NEWS;role=EvidenceRole.OPINION_DISCOVERY if mode is ContentMode.OPINION else EvidenceRole.FACT_DISCOVERY
    save_external_news(path,NewsWrite(canonical_url="https://older.example/b",title=title,observed_at=as_of-timedelta(minutes=1),published_at=as_of-timedelta(minutes=1),content_mode=mode,evidence_role=role))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q,policy=QueryPolicyV1(2,2,4096,1,262144))
    member=next(m for p in result.all_match_semantics.bucket_aggregates[0].polarity_aggregates if p.polarity.value==polarity for m in p.members if "title:" in m.story_key and m.claimant_kinds_present[0].value==claimant and m.numeric_capex_present is numeric)
    expected_points=0 if polarity=="CONTEXT" else (2 if claimant=="COMPANY" else 1)
    assert member.points==expected_points and len(member.member_semantic_digest)==64
    pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    summary=next(s for s in pack.omitted_evidence_summaries if s.bucket.value=="GPU_HBM" and s.polarity.value==polarity)
    assert summary.numeric_capex_present is numeric and summary.omitted_points==expected_points and claimant in {k.value for k in summary.claimant_kinds_present}


# D-C1-02/03: row duplicates union kinds/sources/numeric and max weight once; polarity stays independent.
def test_delta_multirow_story_union_max_weight_and_independent_polarities(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"union.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC));shared="b"*64
    rows=[("company GPU increase cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),("supplier GPU increase cut capex 10",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),("analyst GPU increase cut",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY)]
    for i,(title,mode,role) in enumerate(rows):save_external_news(path,NewsWrite(canonical_url=f"https://union{i}.example/a",title=title,content_hash=shared,observed_at=as_of-timedelta(minutes=i),published_at=as_of-timedelta(minutes=i),content_mode=mode,evidence_role=role))
    result=query_local_news_v1(path,LocalNewsQueryV1(as_of-timedelta(days=30),as_of));pols=result.all_match_semantics.bucket_aggregates[0].polarity_aggregates
    support,counter,context=pols
    assert support.members[0].story_key==counter.members[0].story_key=="content:"+shared and context.members==()
    assert tuple(k.value for k in support.members[0].claimant_kinds_present)==("COMPANY","SUPPLIER","ANALYST_OR_OPINION")
    assert support.members[0].source_ids==tuple(sorted(support.members[0].source_ids)) and len(support.members[0].source_ids)==3
    assert support.members[0].numeric_capex_present and support.members[0].points==counter.members[0].points==2


# S10-D01/02: successful row/byte truncation is OK and all-match-state invariant.
def test_delta_output_truncation_matches_unlimited_production_aggregate_oracle(tmp_path):
    path,as_of=_bulk_public_store(tmp_path,[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*2);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of)
    limited=query_local_news_v1(path,q,policy=QueryPolicyV1(2,2,4096,1,262144));unlimited=query_local_news_v1(path,q,policy=QueryPolicyV1(2,2,4096,2,262144))
    assert limited.status.value==unlimited.status.value=="OK" and limited.coverage.truncated_rows==1 and unlimited.coverage.truncated_rows==0
    assert limited.all_match_semantics==unlimited.all_match_semantics
    left=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:limited).pack;right=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:unlimited).pack
    assert (left.state,left.assessments,{n.reason_code for n in left.missing_needs})==(right.state,right.assessments,{n.reason_code for n in right.missing_needs})
    first_bytes=len(canonical_json_bytes(unlimited.articles[0]));byte_limited=query_local_news_v1(path,q,policy=QueryPolicyV1(2,2,4096,2,first_bytes));byte_pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:byte_limited).pack
    assert byte_limited.status.value=="OK" and byte_limited.coverage.truncated_rows==1 and byte_limited.all_match_semantics==unlimited.all_match_semantics
    assert (byte_pack.state,byte_pack.assessments,{n.reason_code for n in byte_pack.missing_needs})==(right.state,right.assessments,{n.reason_code for n in right.missing_needs})


@pytest.mark.parametrize("polarity,marker",[("SUPPORT","increase"),("COUNTER","cut"),("CONTEXT","overview")])
def test_delta_ambiguous_unspecified_member_each_polarity(polarity,marker,tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"ambiguous.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC));save_external_news(path,NewsWrite(canonical_url="https://ambiguous.example/a",title=f"company supplier GPU {marker}",observed_at=as_of,published_at=as_of))
    result=query_local_news_v1(path,LocalNewsQueryV1(as_of-timedelta(days=30),as_of));member=next(m for p in result.all_match_semantics.bucket_aggregates[0].polarity_aggregates if p.polarity.value==polarity for m in p.members)
    assert member.claimant_kinds_present[0].value=="UNSPECIFIED" and member.claimant_ambiguous


# reviewer m2/m3 + S16-delta: digest namespaces differ; contradiction-only display remains omitted conservatively.
def test_delta_member_projection_digest_mutations_and_contradiction_omission(tmp_path):
    path,as_of=_bulk_public_store(tmp_path,[("company GPU increase cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*3);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);polarity=result.all_match_semantics.bucket_aggregates[0].polarity_aggregates[0];member=polarity.members[0]
    assert member.member_semantic_digest!=polarity.story_key_membership_digest
    bad=replace(member,member_semantic_digest="0"*64)
    with pytest.raises(ValueError,match="member semantic digest mismatch"):replace(polarity,members=(bad,)+polarity.members[1:])
    with pytest.raises(ValueError,match="projection digest mismatch"):replace(polarity,story_key_membership_digest="0"*64)
    pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    counter_keys={e.distinct_story_key for e in pack.contradictions};selected_counter={e.distinct_story_key for e in pack.evidence if e.polarity.value=="COUNTER"}
    assert counter_keys and not selected_counter
    counter_summary=next(s for s in pack.omitted_evidence_summaries if s.bucket.value=="GPU_HBM" and s.polarity.value=="COUNTER")
    assert counter_summary.omitted_distinct_story_count==len(counter_keys)
    assert pack.assessments[0].conflict and pack.state.value=="NEEDS_CONFIRMATION"


def test_delta_physical_permutation_preserves_members_omissions_and_pack(tmp_path):
    path,as_of=_bulk_public_store(tmp_path,[("company GPU increase cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),("supplier network overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY),("analyst edge overview",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY)]);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of)
    before=query_local_news_v1(path,q);pack_before=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:before).pack
    import sqlite3
    connection=sqlite3.connect(path);connection.execute("VACUUM");connection.close()
    after=query_local_news_v1(path,q);pack_after=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:after).pack
    assert canonical_json_bytes(before)==canonical_json_bytes(after)
    assert canonical_json_bytes(pack_before)==canonical_json_bytes(pack_after)


def test_delta_raw_cap_is_insufficient_and_pack_construction_backstop_has_no_pack(monkeypatch,tmp_path):
    path,as_of=_bulk_public_store(tmp_path,[("GPU overview",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY)]*3);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);failure=query_local_news_v1(path,q,policy=QueryPolicyV1(2,2,4096,2,262144))
    pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:failure).pack
    assert failure.status.value=="CAP_EXCEEDED" and pack.state.value=="INSUFFICIENT_LOCAL_EVIDENCE" and any(n.reason_code.value=="QUERY_FAILED" for n in pack.missing_needs)
    ok=query_local_news_v1(path,q);import tokmo.values.ai_infra_capex as values_module
    monkeypatch.setattr(values_module,"validate_pack_bytes_v1",lambda size,cap=131072:(_ for _ in ()).throw(ValueError("pack byte cap")))
    with pytest.raises(ValueError,match="pack byte cap"):
        evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:ok)


def _m1_two_story_public_pack(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"m1.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC))
    for story,content_hash in (("alpha","a"*64),("beta","b"*64)):
        for source in ("one","two"):
            save_external_news(path,NewsWrite(canonical_url=f"https://{source}.example/{story}",title=f"company GPU interconnect edge cloud capex overview {story}",content_hash=content_hash,observed_at=as_of-timedelta(minutes=1),published_at=as_of-timedelta(minutes=1)))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    return q,result,pack


# M1-RED/f1/f3: exact public-path counterexample and exact three-anchor fallback.
def test_m1_exact_two_story_coverage_fallback_public_path(tmp_path):
    q,result,pack=_m1_two_story_public_pack(tmp_path)
    sem=result.all_match_semantics
    assert result.status.value=="OK" and result.coverage.total_matched_distinct_stories==2 and sem.matched_distinct_source_count==2
    assert all(sum((p.distinct_story_count for p in b.polarity_aggregates))==2 and b.distinct_source_count==2 for b in sem.bucket_aggregates)
    assert sem.numeric_capex_story_count==0 and sem.claimant_ambiguous_story_count==0 and sem.claimant_story_counts[1:]==(0,0,0)
    assert all(not b.conflict and b.net_points==0 for b in sem.bucket_aggregates) and q.as_of-result.coverage.available_at_to<timedelta(hours=72)
    assert pack.state.value=="INSUFFICIENT_LOCAL_EVIDENCE"
    assert [(n.bucket.value,n.reason_code.value,n.required_evidence_class.value,n.official_source_kind.value,n.earliest_needed_at,n.automatic_search_starts) for n in pack.missing_needs]==[(b,"LOCAL_COVERAGE_MINIMUM_UNMET","VERIFIED_FACT","ISSUER_IR",q.as_of,0) for b in ("GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY")]


# M1-EACH/M1-ZERO/f1/f2: all OK predicates are evaluated independently.
def test_m1_each_insufficient_cause_zero_and_truthful_coexistence(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);empty=tmp_path/"empty"/"private"/"m1.sqlite3";bootstrap_external_news(empty,now=datetime(2026,8,1,tzinfo=UTC));q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(empty,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    reasons=[(n.bucket.value,n.reason_code.value) for n in pack.missing_needs]
    assert reasons==[(b,r) for b in ("GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY","HYPERSCALER_CAPEX_CONTEXT") for r in (("NO_LOCAL_MATCH","LOCAL_COVERAGE_MINIMUM_UNMET") if b!="HYPERSCALER_CAPEX_CONTEXT" else ("NO_LOCAL_MATCH",))]
    _,two_result,two_pack=_m1_two_story_public_pack(tmp_path/"two")
    assert {n.reason_code.value for n in two_pack.missing_needs}=={"LOCAL_COVERAGE_MINIMUM_UNMET"} and two_result.coverage.total_matched_distinct_stories==2
    for label,age,sources,expected in (("stale",timedelta(hours=72,microseconds=1),2,{"STALE_LOCAL_NEWS","NO_LOCAL_MATCH"}),("one-source",timedelta(minutes=1),1,{"LOW_SOURCE_DIVERSITY","NO_LOCAL_MATCH"})):
        path=tmp_path/label/"private"/"m1.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC))
        for i in range(3):save_external_news(path,NewsWrite(canonical_url=f"https://s{i%sources}.example/{i}",title=f"GPU overview {i}",observed_at=as_of-age,published_at=as_of-age))
        rr=query_local_news_v1(path,q);pp=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:rr).pack
        assert pp.state.value=="INSUFFICIENT_LOCAL_EVIDENCE" and {n.reason_code.value for n in pp.missing_needs}==expected


# M1-NONOK: unknown semantics never produce inferred semantic reasons.
def test_m1_every_non_ok_status_emits_query_failed_only(tmp_path):
    import sqlite3
    from tokmo.values.ai_infra_capex import NewsQueryStatusV1
    as_of=datetime(2026,8,9,3,tzinfo=UTC);q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);results=[query_local_news_v1(tmp_path/"missing.sqlite3",q)]
    schema=tmp_path/"schema"/"x.db";bootstrap_external_news(schema,now=as_of);c=sqlite3.connect(schema);c.execute("UPDATE schema_meta SET ddl_sha256='"+"0"*64+"'");c.commit();c.close();results.append(query_local_news_v1(schema,q))
    malformed=tmp_path/"malformed"/"x.db";bootstrap_external_news(malformed,now=as_of);save_external_news(malformed,NewsWrite(canonical_url="https://m.example/a",title="GPU",observed_at=as_of,published_at=as_of));c=sqlite3.connect(malformed);c.execute("PRAGMA ignore_check_constraints=ON");c.execute("UPDATE news_documents SET published_at='bad'");c.commit();c.close();results.append(query_local_news_v1(malformed,q))
    capped=tmp_path/"cap"/"x.db";bootstrap_external_news(capped,now=as_of);save_external_news(capped,NewsWrite(canonical_url="https://c.example/a",title="GPU",observed_at=as_of,published_at=as_of));results.append(query_local_news_v1(capped,q,policy=QueryPolicyV1(1,1,4096,1,262144)));save_external_news(capped,NewsWrite(canonical_url="https://c.example/b",title="GPU",observed_at=as_of,published_at=as_of));results[-1]=query_local_news_v1(capped,q,policy=QueryPolicyV1(1,1,4096,1,262144))
    corrupt=tmp_path/"corrupt"/"x.db";corrupt.parent.mkdir(mode=0o700);corrupt.write_bytes(b"not sqlite");corrupt.chmod(0o600);results.append(query_local_news_v1(corrupt,q))
    busy=tmp_path/"busy"/"x.db";bootstrap_external_news(busy,now=as_of);lock=sqlite3.connect(busy,isolation_level=None);lock.execute("BEGIN EXCLUSIVE")
    try:results.append(query_local_news_v1(busy,q))
    finally:lock.execute("ROLLBACK");lock.close()
    assert {r.status for r in results}==set(NewsQueryStatusV1)-{NewsQueryStatusV1.OK}
    for result in results:
        pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _,r=result:r).pack
        assert [(n.bucket.value,n.reason_code.value) for n in pack.missing_needs]==[(b,"QUERY_FAILED") for b in ("GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY","HYPERSCALER_CAPEX_CONTEXT")]


# M1-VECTOR: repeat public bytes and all active bindings are deterministic.
def test_m1_v02_public_vector_repeat_bytes(tmp_path):
    q,result,pack=_m1_two_story_public_pack(tmp_path);again=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(q.as_of),query=q,query_local_news=lambda _:result).pack
    assert canonical_json_bytes(pack)==canonical_json_bytes(again) and pack.spec_digest=="d403c8444bffc7e5ca958e5293ee511488fdc7743c03c863b7d8ce9353844c15"


# M1-STRICT/f3: fallback shape and insufficient-selected-need invariant fail closed.
def test_m1_fallback_value_shape_and_pack_cross_links_are_strict(tmp_path):
    from dataclasses import fields
    from tokmo.values.ai_infra_capex import CapexBucketV1, OfficialSourceKindV1
    _,_,pack=_m1_two_story_public_pack(tmp_path);need=pack.missing_needs[0]
    def mutate(**changes):
        values={f.name:getattr(need,f.name) for f in fields(need) if f.name!="need_digest"};values.update(changes)
        return replace(need,**changes,need_digest=digest_v1(values))
    for changes in ({"reason_code":need.reason_code.value},{"bucket":CapexBucketV1.HYPERSCALER_CAPEX_CONTEXT},{"official_source_kind":OfficialSourceKindV1.REGULATOR_FILING},{"earliest_needed_at":need.as_of-timedelta(microseconds=1)},{"subject_hint_digest":"0"*64},{"need_id":"0"*64},{"automatic_search_starts":True}):
        with pytest.raises(ValueError):mutate(**changes)
    with pytest.raises(ValueError,match="insufficient state requires"):replace(pack,missing_needs=())
    with pytest.raises(ValueError,match="missing need order"):replace(pack,missing_needs=tuple(reversed(pack.missing_needs)))


# M1-CAPS/M1-ORDER: 19 real candidates retain all three fallback anchors and summarize 3.
def test_m1_fallback_coexists_with_stable_16_cap_and_omitted_summary(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"caps.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC));terms="GPU interconnect edge cloud capex"
    rows=(("company increase capex 10",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY,"one"),("supplier cut",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY,"two"),("analyst overview",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY,"one"))
    for story,content_hash in (("alpha","c"*64),("beta","d"*64)):
        for i,(claim,mode,role,source) in enumerate(rows):save_external_news(path,NewsWrite(canonical_url=f"https://{source}.example/{story}/{i}",title=f"{claim} {terms} {story}",content_hash=content_hash,observed_at=as_of-timedelta(minutes=i),published_at=as_of-timedelta(minutes=i),content_mode=mode,evidence_role=role))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    assert result.coverage.total_matched_distinct_stories==2 and len(pack.missing_needs)==16 and pack.omitted_need_summary.total_omitted==3
    assert [n.bucket.value for n in pack.missing_needs if n.reason_code.value=="LOCAL_COVERAGE_MINIMUM_UNMET"]==["GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY"]
    assert sum(n for _,n in pack.omitted_need_summary.reason_counts)==3 and pack.state.value=="INSUFFICIENT_LOCAL_EVIDENCE"
    summary=pack.omitted_need_summary
    with pytest.raises(ValueError):replace(summary,reason_counts=tuple(reversed(summary.reason_counts)))
    with pytest.raises(ValueError):replace(summary,reason_counts=summary.reason_counts+(summary.reason_counts[0],),total_omitted=summary.total_omitted+summary.reason_counts[0][1])
    with pytest.raises(ValueError):replace(summary,reason_counts=((summary.reason_counts[0][0],True),)+summary.reason_counts[1:])


# n1: deterministic 8+8 selection may omit only the EDGE fallback suffix.
def test_partial_fallback_prefix_is_accounted_by_omitted_summary_public_path(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);at=as_of-timedelta(hours=72,microseconds=1);path=tmp_path/"private"/"partial.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC));terms="GPU interconnect"
    rows=(("supplier increase cut capex 10 alpha",ContentMode.NEWS,EvidenceRole.FACT_DISCOVERY,"e"*64),("analyst increase cut beta",ContentMode.OPINION,EvidenceRole.OPINION_DISCOVERY,"f"*64))
    for i,(claim,mode,role,content_hash) in enumerate(rows):save_external_news(path,NewsWrite(canonical_url=f"https://only.example/story/{i}",title=f"{claim} {terms}",content_hash=content_hash,observed_at=at,published_at=at,content_mode=mode,evidence_role=role))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    assert len(pack.missing_needs)==16 and [(n.bucket.value,n.reason_code.value) for n in pack.missing_needs if n.reason_code.value=="LOCAL_COVERAGE_MINIMUM_UNMET"]==[("GPU_HBM","LOCAL_COVERAGE_MINIMUM_UNMET"),("NETWORK_INTERCONNECT","LOCAL_COVERAGE_MINIMUM_UNMET")]
    summary=pack.omitted_need_summary;assert dict((r.value,n) for r,n in summary.reason_counts)["LOCAL_COVERAGE_MINIMUM_UNMET"]==1 and dict((b.value,n) for b,n in summary.bucket_counts)["EDGE_SECURITY"]>=1
    fallback=tuple(n for n in pack.missing_needs if n.reason_code.value=="LOCAL_COVERAGE_MINIMUM_UNMET")
    with pytest.raises(ValueError,match="fallback omission accounting"):replace(pack,omitted_need_summary=None)
    with pytest.raises(ValueError,match="fallback omission accounting"):replace(pack,missing_needs=tuple(n for n in pack.missing_needs if n is not fallback[-1]))
    with pytest.raises(ValueError,match="fallback cannot appear only omitted"):replace(pack,missing_needs=tuple(n for n in pack.missing_needs if n not in fallback))


# n3: destination=0 independently fails with otherwise sufficient context coverage.
def test_destination_zero_independent_hyperscaler_only_public_path(tmp_path):
    as_of=datetime(2026,8,9,3,tzinfo=UTC);path=tmp_path/"private"/"destination.sqlite3";bootstrap_external_news(path,now=datetime(2026,8,1,tzinfo=UTC))
    for i,word in enumerate(("alpha","beta","gamma")):save_external_news(path,NewsWrite(canonical_url=f"https://context{i%2}.example/{word}",title=f"cloud capex overview {word}",observed_at=as_of-timedelta(minutes=i),published_at=as_of-timedelta(minutes=i)))
    q=LocalNewsQueryV1(as_of-timedelta(days=30),as_of);result=query_local_news_v1(path,q);pack=evaluate_ai_infra_capex_allocation_v1(lifecycle=_state(as_of),query=q,query_local_news=lambda _:result).pack
    assert result.coverage.total_matched_distinct_stories==3 and result.all_match_semantics.matched_distinct_source_count==2 and result.all_match_semantics.destination_story_count==0 and q.as_of-result.coverage.available_at_to<timedelta(hours=72)
    assert pack.state.value=="INSUFFICIENT_LOCAL_EVIDENCE" and [(n.bucket.value,n.reason_code.value) for n in pack.missing_needs]==[(b,"NO_LOCAL_MATCH") for b in ("GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY")]
