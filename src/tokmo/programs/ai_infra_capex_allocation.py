"""Pure deterministic G1-C CAPEX hypothesis sensor."""
from __future__ import annotations

from datetime import timedelta

from tokmo.values.ai_infra_capex import (
    BucketAssessmentV1, CapexAllocationEvidencePackV1, CapexBucketV1, CapexEvidenceRefV1,
    CapexSensorRunV1, CapexSensorStateV1, EvidenceClassV1,
    CapexEvidencePolarityV1, MissingEvidenceNeedV1, MissingReasonCodeV1,
    OfficialSourceKindV1, OmittedEvidenceSummaryV1, OmittedNeedSummaryV1,
    LocalNewsQueryResultV1, LocalNewsQueryV1, NewsQueryStatusV1,
    PROGRAM_ID_V1, PROGRAM_VERSION_V1, FORMULA_VERSION_V1, SPEC_DIGEST_V1,
    ClaimantKindV1, COVERAGE_FALLBACK_BUCKETS_V1, coverage_fallback_need_id_v1,
    coverage_fallback_subject_hint_digest_v1, digest_v1, structural_privacy_scan_v1,
)
from tokmo.values.signal_program_lifecycle import (
    LifecycleReasonCode, ProgramKindV1, ProgramLifecycleStatusV1,
    ProgramLifecycleStateV1,
)

_BLOCKED={
 ProgramLifecycleStatusV1.SHADOW:LifecycleReasonCode.NOT_YET_ACTIVE,
 ProgramLifecycleStatusV1.DORMANT:LifecycleReasonCode.DORMANT,
 ProgramLifecycleStatusV1.RETIRED:LifecycleReasonCode.RETIRED,
 ProgramLifecycleStatusV1.EXPIRED:LifecycleReasonCode.EXPIRED,
}

def evaluate_ai_infra_capex_allocation_v1(*,lifecycle:ProgramLifecycleStateV1,query:LocalNewsQueryV1,query_local_news)->CapexSensorRunV1:
    """Evaluate once when ACTIVE/REVIEW_DUE; blocked states touch no query seam."""
    if lifecycle.status in _BLOCKED:return CapexSensorRunV1(None,_BLOCKED[lifecycle.status])
    spec=lifecycle.spec
    if (spec.program_id!=PROGRAM_ID_V1 or spec.version!=PROGRAM_VERSION_V1 or spec.kind is not ProgramKindV1.HYPOTHESIS_SENSOR or spec.spec_digest!=SPEC_DIGEST_V1 or spec.review_due_at!=spec.activated_at+timedelta(days=45) or spec.expires_at!=spec.activated_at+timedelta(days=120) or spec.review_cadence_days!=45 or lifecycle.renewal_count!=0): raise ValueError("invalid frozen G1-C lifecycle")
    if query.as_of!=lifecycle.as_of:raise ValueError("query/lifecycle as_of mismatch")
    result=query_local_news(query)
    if not isinstance(result,LocalNewsQueryResultV1):raise TypeError("query must return LocalNewsQueryResultV1")
    semantics=result.all_match_semantics; coverage=result.coverage
    insufficient=result.status is not NewsQueryStatusV1.OK or semantics is None or coverage is None
    if not insufficient:
        newest=coverage.available_at_to
        insufficient=(semantics.destination_story_count==0 or coverage.total_matched_distinct_stories<3 or semantics.matched_distinct_source_count<2 or newest is None or query.as_of-newest>timedelta(hours=72))
    triggers=False
    if semantics is not None:
        claimant=dict(zip(ClaimantKindV1,semantics.claimant_story_counts))
        triggers=(any(b.conflict or abs(b.net_points)>=2 for b in semantics.bucket_aggregates) or semantics.numeric_capex_story_count>0 or claimant.get(ClaimantKindV1.SUPPLIER,0)>0 or claimant.get(ClaimantKindV1.ANALYST_OR_OPINION,0)>0 or semantics.claimant_ambiguous_story_count>0 or semantics.hyperscaler_context_story_count>0)
    state=CapexSensorStateV1.INSUFFICIENT_LOCAL_EVIDENCE if insufficient else (CapexSensorStateV1.NEEDS_CONFIRMATION if triggers else CapexSensorStateV1.DISCOVERY_ONLY)
    assessments=tuple(BucketAssessmentV1(b.bucket,b.support_points,b.counter_points,b.net_points,b.support_distinct_story_count,b.counter_distinct_story_count,b.distinct_source_count,b.conflict,True,"metadata-only sensor inference") for b in (semantics.bucket_aggregates if semantics else ()))
    member_index={(b.bucket,p.polarity,m.story_key):m for b in (semantics.bucket_aggregates if semantics else ()) for p in b.polarity_aggregates for m in p.members};polarity_index={(b.bucket,p.polarity):p for b in (semantics.bucket_aggregates if semantics else ()) for p in b.polarity_aggregates}
    candidate_groups={}
    for article in result.articles:
        story=("content:"+article.content_hash if article.content_hash else "title:"+article.title_fingerprint)
        effective=article.published_at or article.first_seen_at
        for match in article.metadata_matches:
            cls=EvidenceClassV1.CONTRADICTION if match.polarity is CapexEvidencePolarityV1.COUNTER else EvidenceClassV1.ATTRIBUTED_CLAIM
            descriptor=f"{article.claimant_kind.value.lower()} attributed {match.polarity.value.lower()} claim"
            member=member_index.get((match.bucket,match.polarity,story))
            if member is None:raise ValueError("evidence member missing")
            key=(story,match.bucket,match.polarity,cls);candidate_groups.setdefault(key,[]).append((article,effective,descriptor,member))
    candidates=[]
    for (story,bucket,polarity,cls),rows in candidate_groups.items():
        rows.sort(key=lambda x:(-x[0].available_at.timestamp(),-x[1].timestamp(),x[0].source_id,x[0].news_document_id))
        article,effective,descriptor,member=rows[0]
        ids=tuple(sorted({x[0].news_document_id for x in rows}))[:8]
        source_digests=tuple(sorted({x[0].source_article_id_digest for x in rows if x[0].source_article_id_digest}))[:8]
        eb={"evidence_class":cls,"claimant_kind":article.claimant_kind,"polarity":polarity,"bucket":bucket,"source_id":article.source_id,"news_document_ids":ids,"source_article_id_digests":source_digests,"distinct_story_key":story,"effective_at":effective,"available_at":article.available_at,"descriptor":descriptor,"title_excerpt_digest":article.title_excerpt_digest,"summary_excerpt_digest":article.summary_excerpt_digest,"semantic_member_digest":member.member_semantic_digest}
        candidates.append(CapexEvidenceRefV1(**eb,evidence_digest=digest_v1(eb)))
    candidates=sorted(candidates,key=lambda e:(list(type(e.bucket)).index(e.bucket),list(type(e.polarity)).index(e.polarity),-e.effective_at.timestamp(),e.distinct_story_key,e.source_id,e.news_document_ids[0],e.evidence_digest))
    evidence=tuple(e for e in candidates if e.evidence_class is not EvidenceClassV1.CONTRADICTION)[:40]; contradiction_candidates=tuple(e for e in candidates if e.evidence_class is EvidenceClassV1.CONTRADICTION)
    if len(contradiction_candidates)>20: raise ValueError("contradiction reference cap")
    contradictions=contradiction_candidates
    omitted=[]
    if semantics:
        for aggregate in semantics.bucket_aggregates:
            for polarity_aggregate in aggregate.polarity_aggregates:
                selected={e.distinct_story_key:e for e in evidence if e.bucket is aggregate.bucket and e.polarity is polarity_aggregate.polarity}
                for key,ref in selected.items():
                    member=next((m for m in polarity_aggregate.members if m.story_key==key),None)
                    if member is None or ref.semantic_member_digest!=member.member_semantic_digest:raise ValueError("selected evidence semantic mismatch")
                omitted_members=tuple(m for m in polarity_aggregate.members if m.story_key not in selected)
                if omitted_members:
                    kinds=tuple(k for k in ClaimantKindV1 if any(k in m.claimant_kinds_present for m in omitted_members));selected_digests=tuple(sorted(member_index[(aggregate.bucket,polarity_aggregate.polarity,key)].member_semantic_digest for key in selected));omitted_digests=tuple(sorted(m.member_semantic_digest for m in omitted_members));display={"bucket":aggregate.bucket,"polarity":polarity_aggregate.polarity,"omitted_distinct_story_count":len(omitted_members),"omitted_points":sum(m.points for m in omitted_members),"claimant_kinds_present":kinds,"numeric_capex_present":any(m.numeric_capex_present for m in omitted_members)};summary_digest=digest_v1({"display":display,"polarity_aggregate_digest":polarity_aggregate.aggregate_digest,"selected_semantic_member_digests":selected_digests,"omitted_semantic_member_digests":omitted_digests})
                    omitted.append(OmittedEvidenceSummaryV1(**display,summary_digest=summary_digest))
    need_candidates=[]
    def add_need(bucket,reason,source):
        fallback=reason is MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET
        nid=coverage_fallback_need_id_v1(bucket,query.as_of) if fallback else digest_v1({"need":(bucket,reason,source),"as_of":query.as_of})
        hint=coverage_fallback_subject_hint_digest_v1(bucket,query.as_of) if fallback else SPEC_DIGEST_V1
        base={"need_id":nid,"bucket":bucket,"required_evidence_class":EvidenceClassV1.VERIFIED_FACT,"reason_code":reason,"official_source_kind":source,"subject_hint_digest":hint,"earliest_needed_at":query.as_of,"as_of":query.as_of,"automatic_search_starts":0}
        need_candidates.append(MissingEvidenceNeedV1(**base,need_digest=digest_v1(base)))
    for bucket in CapexBucketV1:
        agg=next((b for b in semantics.bucket_aggregates if b.bucket is bucket),None) if semantics else None
        total=(agg.support_distinct_story_count+agg.counter_distinct_story_count+agg.context_distinct_story_count) if agg else 0
        if result.status is not NewsQueryStatusV1.OK:add_need(bucket,MissingReasonCodeV1.QUERY_FAILED,OfficialSourceKindV1.ISSUER_IR)
        elif total==0:add_need(bucket,MissingReasonCodeV1.NO_LOCAL_MATCH,OfficialSourceKindV1.ISSUER_IR)
        if result.status is NewsQueryStatusV1.OK and semantics is not None and semantics.matched_distinct_source_count<2 and agg and total>0 and agg.distinct_source_count<2:add_need(bucket,MissingReasonCodeV1.LOW_SOURCE_DIVERSITY,OfficialSourceKindV1.ISSUER_IR)
        if result.status is NewsQueryStatusV1.OK and agg and total>0 and coverage and coverage.available_at_to and query.as_of-coverage.available_at_to>timedelta(hours=72):add_need(bucket,MissingReasonCodeV1.STALE_LOCAL_NEWS,OfficialSourceKindV1.ISSUER_IR)
        if agg and agg.numeric_capex_story_count:add_need(bucket,MissingReasonCodeV1.ATTRIBUTED_AMOUNT_UNVERIFIED,OfficialSourceKindV1.REGULATOR_FILING);add_need(bucket,MissingReasonCodeV1.ALLOCATION_PERIOD_OR_CURRENCY_MISSING,OfficialSourceKindV1.ISSUER_IR)
        if agg and agg.claimant_story_counts[list(ClaimantKindV1).index(ClaimantKindV1.SUPPLIER)]:add_need(bucket,MissingReasonCodeV1.SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION,OfficialSourceKindV1.SUPPLIER_IR)
        if agg and agg.claimant_story_counts[list(ClaimantKindV1).index(ClaimantKindV1.ANALYST_OR_OPINION)]:add_need(bucket,MissingReasonCodeV1.ANALYST_CLAIM_UNVERIFIED,OfficialSourceKindV1.ISSUER_IR)
        if agg and agg.conflict:add_need(bucket,MissingReasonCodeV1.CONTRADICTION_UNRESOLVED,OfficialSourceKindV1.REGULATOR_FILING)
    if result.status is NewsQueryStatusV1.OK and semantics is not None and coverage is not None and coverage.total_matched_distinct_stories<3:
        for bucket in COVERAGE_FALLBACK_BUCKETS_V1:add_need(bucket,MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET,OfficialSourceKindV1.ISSUER_IR)
    for need in need_candidates:
        agg=next((b for b in semantics.bucket_aggregates if b.bucket is need.bucket),None) if semantics else None
        total=(agg.support_distinct_story_count+agg.counter_distinct_story_count+agg.context_distinct_story_count) if agg else 0
        valid={
            MissingReasonCodeV1.QUERY_FAILED:result.status is not NewsQueryStatusV1.OK and semantics is None,
            MissingReasonCodeV1.NO_LOCAL_MATCH:result.status is NewsQueryStatusV1.OK and agg is not None and total==0,
            MissingReasonCodeV1.LOW_SOURCE_DIVERSITY:result.status is NewsQueryStatusV1.OK and semantics is not None and semantics.matched_distinct_source_count<2 and agg is not None and total>0 and agg.distinct_source_count<2,
            MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET:result.status is NewsQueryStatusV1.OK and semantics is not None and coverage is not None and coverage.total_matched_distinct_stories<3 and need.bucket in COVERAGE_FALLBACK_BUCKETS_V1,
            MissingReasonCodeV1.STALE_LOCAL_NEWS:result.status is NewsQueryStatusV1.OK and total>0 and coverage is not None and coverage.available_at_to is not None and query.as_of-coverage.available_at_to>timedelta(hours=72),
        }.get(need.reason_code,True)
        if not valid:raise ValueError("missing need predicate mismatch")
    reason_order=list(MissingReasonCodeV1); source_order=list(OfficialSourceKindV1)
    need_candidates=sorted({(n.bucket,n.reason_code,n.official_source_kind):n for n in need_candidates}.values(),key=lambda n:(list(type(n.bucket)).index(n.bucket),reason_order.index(n.reason_code),source_order.index(n.official_source_kind),n.need_id))
    missing_needs=tuple(need_candidates[:16]); omitted_need_summary=None
    if len(need_candidates)>16:
        tail=need_candidates[16:]; membership=digest_v1(tuple(n.need_digest for n in tail)); reasons=tuple((r,sum(n.reason_code is r for n in tail)) for r in MissingReasonCodeV1 if any(n.reason_code is r for n in tail)); buckets=tuple((b,sum(n.bucket is b for n in tail)) for b in type(tail[0].bucket) if any(n.bucket is b for n in tail)); summary_base=(len(tail),reasons,buckets,membership); omitted_need_summary=OmittedNeedSummaryV1(*summary_base,digest_v1(summary_base))
    lifecycle_digest=digest_v1({"program_id":PROGRAM_ID_V1,"version":PROGRAM_VERSION_V1,"kind":spec.kind.value,"spec_digest":SPEC_DIGEST_V1,"activated_at":spec.activated_at,"review_due_at":spec.review_due_at,"expires_at":spec.expires_at,"as_of":lifecycle.as_of,"status":lifecycle.status.value,"renewal_count":lifecycle.renewal_count})
    limitations=("metadata-only attributed claims; structured official verification remains required",)
    free_text=tuple(e.descriptor for e in evidence+contradictions)+limitations
    base={"program_id":PROGRAM_ID_V1,"version":PROGRAM_VERSION_V1,"kind":"HYPOTHESIS_SENSOR","formula_version":FORMULA_VERSION_V1,"spec_digest":SPEC_DIGEST_V1,"lifecycle_status":lifecycle.status.value,"lifecycle_digest":lifecycle_digest,"as_of":query.as_of,"query":query,"query_digest":result.query_digest,"source_snapshot_digest":result.source_snapshot_digest,"query_result_digest":result.result_digest,"all_match_semantic_digest":semantics.all_match_semantic_digest if semantics else None,"state":state,"assessments":assessments,"evidence":evidence,"contradictions":contradictions,"omitted_evidence_summaries":tuple(omitted),"missing_needs":missing_needs,"omitted_need_summary":omitted_need_summary,"coverage":coverage,"limitations":limitations,"privacy_scan_passed":structural_privacy_scan_v1(free_text)}
    pack=CapexAllocationEvidencePackV1(**base,pack_digest=digest_v1(base))
    return CapexSensorRunV1(pack,None)
