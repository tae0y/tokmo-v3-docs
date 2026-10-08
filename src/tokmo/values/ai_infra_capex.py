"""Immutable G1-C AI-infrastructure CAPEX values and frozen policy."""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
import hashlib
import json
import re
import unicodedata
from typing import Any

PROGRAM_ID_V1 = "ai-infra-capex-allocation-v1"
PROGRAM_VERSION_V1 = "1.0.0"
FORMULA_VERSION_V1 = "ai-infra-capex-allocation-formula-v1"
CLASSIFIER_VERSION_V1 = "capex-metadata-classifier-v1"

class CapexBucketV1(StrEnum):
    GPU_HBM="GPU_HBM"; NETWORK_INTERCONNECT="NETWORK_INTERCONNECT"; EDGE_SECURITY="EDGE_SECURITY"; HYPERSCALER_CAPEX_CONTEXT="HYPERSCALER_CAPEX_CONTEXT"
class EvidenceClassV1(StrEnum):
    VERIFIED_FACT="VERIFIED_FACT"; ATTRIBUTED_CLAIM="ATTRIBUTED_CLAIM"; SENSOR_INFERENCE="SENSOR_INFERENCE"; CONTRADICTION="CONTRADICTION"; MISSING_NEED="MISSING_NEED"
class ClaimantKindV1(StrEnum):
    COMPANY="COMPANY"; SUPPLIER="SUPPLIER"; ANALYST_OR_OPINION="ANALYST_OR_OPINION"; UNSPECIFIED="UNSPECIFIED"
class CapexEvidencePolarityV1(StrEnum):
    SUPPORT="SUPPORT"; COUNTER="COUNTER"; CONTEXT="CONTEXT"
class CapexSensorStateV1(StrEnum):
    DISCOVERY_ONLY="DISCOVERY_ONLY"; NEEDS_CONFIRMATION="NEEDS_CONFIRMATION"; INSUFFICIENT_LOCAL_EVIDENCE="INSUFFICIENT_LOCAL_EVIDENCE"
class NewsQueryStatusV1(StrEnum):
    OK="OK"; SCHEMA_MISMATCH="SCHEMA_MISMATCH"; STORE_UNAVAILABLE="STORE_UNAVAILABLE"; BUSY_OR_LOCKED="BUSY_OR_LOCKED"; CORRUPT="CORRUPT"; CAP_EXCEEDED="CAP_EXCEEDED"; MALFORMED_TIMESTAMP="MALFORMED_TIMESTAMP"
class MissingReasonCodeV1(StrEnum):
    NO_LOCAL_MATCH="NO_LOCAL_MATCH"; LOW_SOURCE_DIVERSITY="LOW_SOURCE_DIVERSITY"; LOCAL_COVERAGE_MINIMUM_UNMET="LOCAL_COVERAGE_MINIMUM_UNMET"; STALE_LOCAL_NEWS="STALE_LOCAL_NEWS"; QUERY_FAILED="QUERY_FAILED"; ATTRIBUTED_AMOUNT_UNVERIFIED="ATTRIBUTED_AMOUNT_UNVERIFIED"; SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION="SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION"; ANALYST_CLAIM_UNVERIFIED="ANALYST_CLAIM_UNVERIFIED"; CONTRADICTION_UNRESOLVED="CONTRADICTION_UNRESOLVED"; ALLOCATION_PERIOD_OR_CURRENCY_MISSING="ALLOCATION_PERIOD_OR_CURRENCY_MISSING"
class OfficialSourceKindV1(StrEnum):
    ISSUER_IR="ISSUER_IR"; REGULATOR_FILING="REGULATOR_FILING"; SUPPLIER_IR="SUPPLIER_IR"

@dataclass(frozen=True)
class NewsTermGroupV1:
    group_id: str; bucket: CapexBucketV1; terms: tuple[str,...]
    def __post_init__(self):
        if type(self.group_id) is not str or not self.group_id or not isinstance(self.bucket,CapexBucketV1) or type(self.terms) is not tuple or not self.terms or len(set(self.terms))!=len(self.terms): raise ValueError("invalid term group")
        if any(type(t) is not str or not 1<=len(t.encode())<=40 or t!=unicodedata.normalize("NFKC",t).casefold() or any(ord(c)<32 or ord(c)==127 for c in t) for t in self.terms): raise ValueError("invalid classifier term")

CAPEX_TERM_GROUPS_V1=(
 NewsTermGroupV1("gpu-hbm-v1",CapexBucketV1.GPU_HBM,("gpu","accelerator","ai chip","hbm","high bandwidth memory","가속기","ai 칩","고대역폭 메모리")),
 NewsTermGroupV1("network-interconnect-v1",CapexBucketV1.NETWORK_INTERCONNECT,("ethernet","infiniband","switch","interconnect","optical","network fabric","네트워크","인터커넥트","광통신")),
 NewsTermGroupV1("edge-security-v1",CapexBucketV1.EDGE_SECURITY,("edge","cdn","zero trust","cybersecurity","security","엣지","보안")),
 NewsTermGroupV1("hyperscaler-capex-context-v1",CapexBucketV1.HYPERSCALER_CAPEX_CONTEXT,("hyperscaler","cloud capex","datacenter capex","data center capex","cloud provider","하이퍼스케일러","클라우드 설비투자","데이터센터 투자")),)
SUPPORT_MARKERS_V1=("increase","expand","invest","spend","allocate","demand","build","증가","확대","투자","지출","배분","수요","구축")
COUNTER_MARKERS_V1=("cut","decrease","defer","delay","cancel","constraint","reduce","축소","감소","연기","지연","취소","제약","삭감")
CLAIMANT_MARKERS_V1=((ClaimantKindV1.ANALYST_OR_OPINION,("analyst","brokerage","research firm","애널리스트","증권사","리서치")),(ClaimantKindV1.COMPANY,("company","corporation","hyperscaler","cloud provider","기업","회사","하이퍼스케일러","클라우드 사업자")),(ClaimantKindV1.SUPPLIER,("supplier","vendor","chipmaker","공급업체","공급사","반도체 업체")))
NUMERIC_CAPEX_MARKERS_V1=("capex","capital expenditure","capital spending","설비투자","자본적 지출","투자액","$","usd","krw","won","dollar","억원","조원","million","billion","trillion","%")
NUMERIC_PATTERN_V1=r"(?<![\w])(?:[0-9]+(?:[.,][0-9]+)*)(?![\w])"
CLASSIFIER_RULES_V1={"claimant":"opinion_or_analyst_only=>ANALYST;exactly_one_nonanalyst_family=>that_family;multiple_families_or_opinion_plus_nonanalyst=>UNSPECIFIED_AMBIGUOUS;none=>UNSPECIFIED","normalization":"unicode_nfkc_then_whitespace_collapse_then_casefold_v1","numeric":"numeric_pattern_match_AND_numeric_capex_marker_match_same_article","polarity":"support_and_counter=>SUPPORT+COUNTER;support_only=>SUPPORT;counter_only=>COUNTER;neither=>CONTEXT"}
CLASSIFIER_TABLE_OBJECT_V1={"claimant_markers":{k.value:list(v) for k,v in CLAIMANT_MARKERS_V1},"counter_markers":list(COUNTER_MARKERS_V1),"numeric_capex_markers":list(NUMERIC_CAPEX_MARKERS_V1),"numeric_pattern":NUMERIC_PATTERN_V1,"rules":CLASSIFIER_RULES_V1,"support_markers":list(SUPPORT_MARKERS_V1),"term_groups":[{"bucket":g.bucket.value,"group_id":g.group_id,"terms":list(g.terms)} for g in CAPEX_TERM_GROUPS_V1],"version":CLASSIFIER_VERSION_V1}
CLASSIFIER_TABLE_BYTES_V1=json.dumps(CLASSIFIER_TABLE_OBJECT_V1,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
CLASSIFIER_TABLE_DIGEST_V1=hashlib.sha256(CLASSIFIER_TABLE_BYTES_V1).hexdigest()

_SPEC_JSON='''{"buckets":["GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY","HYPERSCALER_CAPEX_CONTEXT"],"classifier_table_digest":"efef7c0fb6ae79a4fa89920ad93daf4d82462c60291e2a07fd8434850d4c3b31","coverage":{"destination_story_min":1,"distinct_source_min":2,"distinct_story_min":3},"eligibility":{"all_match_dedupe":"distinct_story_bucket_polarity","availability_rule":"revision_no_eq_1:first_seen_at;revision_no_gt_1:last_seen_at","claimant_aggregate_rule":"per_bucket_and_global_union_distinct_story_by_kind_with_ambiguity","conflict_rule":"support_count_gt_0_and_counter_count_gt_0_per_bucket","content_modes":["NEWS","OPINION"],"content_state":"METADATA_ONLY","effective_timestamp_rule":"published_at_else_first_seen_at","evidence_roles":["FACT_DISCOVERY","OPINION_DISCOVERY"],"future_availability":"exclude_and_count","future_published":"exclude_and_count_source_skew","malformed_timestamp":"fail_closed_before_comparison","numeric_aggregate_rule":"per_bucket_and_global_union_distinct_story","omitted_summary_rule":"per_bucket_polarity_count_difference_and_uncapped_omitted_weight","representative_evidence_order":["available_at_desc","effective_at_desc","source_id_asc","news_document_id_asc"],"story_key_rule":"content_hash_else_title_fingerprint","story_weight_rule":"max_member_weight_per_story_bucket_polarity","timestamp_format":"YYYY-MM-DDTHH:MM:SS.ffffff+00:00","timestamp_validation_scope":"all_news_documents_streaming_before_comparison","top_slice_rule":"stable_order_then_max_200_and_262144_bytes"},"external_automatic_starts":0,"formula_version":"ai-infra-capex-allocation-formula-v1","freshness_seconds":259200,"kind":"HYPOTHESIS_SENSOR","lifecycle":{"expiry_offset_days":120,"review_offset_days":45},"lookback_days":30,"normalization_caps":{"descriptor_bytes":160,"limitation_bytes_each":240,"limitations_count":16,"summary_bytes":1024,"title_bytes":512},"ordering":["available_at_desc","effective_at_desc","source_id_asc","news_document_id_asc"],"pack_selection":{"evidence_refs":"dedupe_evidence_digest_then_order_bucket_polarity_effective_desc_story_key_source_id_news_document_id_evidence_digest_take_40","missing_needs":"dedupe_bucket_reason_official_source_kind_then_order_bucket_reason_official_source_kind_need_id_take_16","omitted_evidence":"per_bucket_polarity_digest_count_uncapped_weight_summary","omitted_needs":"single_digest_count_summary_with_reason_and_bucket_counts"},"program_id":"ai-infra-capex-allocation-v1","query_caps":{"database_validation_rows":100000,"matched_output_bytes":262144,"matched_output_rows":200,"raw_normalized_bytes":67108864,"raw_temporal_rows":50000},"query_contract":"local-news-query-v1","result_caps":{"contradiction_refs":20,"evidence_refs":40,"member_document_ids":8,"missing_needs":16,"omitted_need_summaries":1,"omitted_summaries":12,"pack_bytes":131072},"scoring":{"analyst_or_opinion":1,"cap":6,"company":2,"needs_confirmation_abs_net_threshold":2,"supplier":1,"unspecified":1},"state_policy":{"insufficient":["query_non_ok","stale","coverage_below_min","destination_story_count_eq_0"],"needs_confirmation":["bucket_conflict","abs_net_ge_threshold","numeric_story_present","supplier_story_present","analyst_story_present","ambiguous_story_present","hyperscaler_context_story_present"]},"version":"1.0.0"}'''
SPEC_OBJECT_V1=json.loads(_SPEC_JSON)
SPEC_OBJECT_V1["eligibility"].update({
    "claimant_aggregate_rule":"per_bucket_polarity_then_bucket_and_global_distinct_story_union_by_kind_with_ambiguity",
    "numeric_aggregate_rule":"per_bucket_polarity_then_bucket_and_global_distinct_story_union",
    "omitted_summary_rule":"exact_all_match_polarity_story_member_set_difference_selected_evidence_story_keys",
    "polarity_semantic_rule":"per_bucket_polarity_distinct_story_members_with_max_weight_claimant_ambiguity_numeric_source_membership",
    "semantic_member_cap_rule":"max_400000_distinct_story_bucket_polarity_members_from_50000_temporal_rows",
    "matched_output_cap_state_rule":"top_200_and_262144_bytes_status_ok_truncated_all_match_state_unchanged",
})
SPEC_OBJECT_V1["state_policy"]["construction_backstop"]="canonical_pack_bytes_over_cap_or_invalid_construction_fail_closed_not_insufficient"
SPEC_OBJECT_V1["missing_reason_policy"]={
    "coverage_anchor_buckets":["GPU_HBM","NETWORK_INTERCONNECT","EDGE_SECURITY"],
    "coverage_fallback_mapping":{"destination_story_count_lt_1":"NO_LOCAL_MATCH_for_each_zero_destination_bucket","global_distinct_source_count_lt_2":"LOW_SOURCE_DIVERSITY_for_each_bucket_below_source_minimum","global_distinct_story_count_lt_3":"LOCAL_COVERAGE_MINIMUM_UNMET_for_each_coverage_anchor_bucket"},
    "coverage_fallback_official_source_kind":"ISSUER_IR",
    "coverage_fallback_subject_hint_rule":"digest_program_version_spec_scope_bucket_anchor_failed_minima_as_of",
    "dedupe":"bucket_reason_official_source_kind",
    "non_ok_without_semantics":"QUERY_FAILED_for_each_bucket_and_never_NO_LOCAL_MATCH",
    "ordered_enum":["NO_LOCAL_MATCH","LOW_SOURCE_DIVERSITY","LOCAL_COVERAGE_MINIMUM_UNMET","STALE_LOCAL_NEWS","QUERY_FAILED","ATTRIBUTED_AMOUNT_UNVERIFIED","SUPPLIER_CLAIM_NEEDS_BUYER_CONFIRMATION","ANALYST_CLAIM_UNVERIFIED","CONTRADICTION_UNRESOLVED","ALLOCATION_PERIOD_OR_CURRENCY_MISSING"],
}
del _SPEC_JSON
SPEC_BYTES_V1=json.dumps(SPEC_OBJECT_V1,sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
SPEC_DIGEST_V1=hashlib.sha256(SPEC_BYTES_V1).hexdigest()

def _utc(dt: datetime, name: str)->None:
    if type(dt) is not datetime or dt.tzinfo is None or dt.utcoffset()!=timedelta(0): raise ValueError(f"{name} must be aware UTC")
def canonical_json_bytes(value: Any)->bytes:
    def cv(v: Any):
        if isinstance(v,StrEnum): return v.value
        if isinstance(v,datetime): _utc(v,"datetime"); return v.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
        if is_dataclass(v): return {f.name:cv(getattr(v,f.name)) for f in fields(v)}
        if isinstance(v,(tuple,list)): return [cv(x) for x in v]
        if isinstance(v,dict): return {str(k):cv(x) for k,x in v.items()}
        return v
    return json.dumps(cv(value),sort_keys=True,separators=(",",":"),ensure_ascii=False).encode()
def digest_v1(value: Any)->str: return hashlib.sha256(canonical_json_bytes(value)).hexdigest()

COVERAGE_FALLBACK_BUCKETS_V1=(CapexBucketV1.GPU_HBM,CapexBucketV1.NETWORK_INTERCONNECT,CapexBucketV1.EDGE_SECURITY)
def coverage_fallback_subject_hint_digest_v1(bucket:CapexBucketV1,as_of:datetime)->str:
    if not isinstance(bucket,CapexBucketV1) or bucket not in COVERAGE_FALLBACK_BUCKETS_V1: raise ValueError("fallback bucket")
    _utc(as_of,"as_of")
    return digest_v1({"as_of":as_of,"bucket_anchor":bucket,"failed_minima":("GLOBAL_DISTINCT_STORY_COUNT_LT_3",),"program_id":PROGRAM_ID_V1,"scope":"PROGRAM_LEVEL_LOCAL_COVERAGE","spec_digest":SPEC_DIGEST_V1,"version":PROGRAM_VERSION_V1})
def coverage_fallback_need_id_v1(bucket:CapexBucketV1,as_of:datetime)->str:
    if not isinstance(bucket,CapexBucketV1) or bucket not in COVERAGE_FALLBACK_BUCKETS_V1: raise ValueError("fallback bucket")
    _utc(as_of,"as_of")
    return digest_v1({"as_of":as_of,"bucket":bucket,"official_source_kind":OfficialSourceKindV1.ISSUER_IR,"reason_code":MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET,"spec_digest":SPEC_DIGEST_V1})

def _strict_int(value:int,name:str,minimum:int=0,maximum:int|None=None)->None:
    if type(value) is not int or value<minimum or (maximum is not None and value>maximum): raise ValueError(f"{name} invalid")
def _digest(value:str,name:str)->None:
    if type(value) is not str or re.fullmatch(r"[0-9a-f]{64}",value) is None: raise ValueError(f"{name} must be lowercase sha256")
def _tuple(value:tuple,name:str,cap:int|None=None)->None:
    if type(value) is not tuple or (cap is not None and len(value)>cap): raise ValueError(f"{name} must be capped tuple")
_FORBIDDEN_OUTPUT_RE=re.compile(r"(?i)(https?://|(?:^|\s)/(?:users|home|private|var|tmp)/|bearer|api[_-]?key|secret|password|[\w.+-]+@[\w.-]+\.[a-z]{2,}|(?:\+?\d[\d .()-]{7,}\d)|\bbuy\b|\bsell\b|\border\b|advice|telegram|account)")
def _safe_output_text(value:str,name:str,cap:int)->None:
    if type(value) is not str or not value or len(value.encode())>cap or _FORBIDDEN_OUTPUT_RE.search(value): raise ValueError(f"{name} unsafe")
def structural_privacy_scan_v1(values:tuple[str,...])->bool:
    _tuple(values,"privacy_text_fields")
    return all(type(value) is str and _FORBIDDEN_OUTPUT_RE.search(value) is None for value in values)
def validate_semantic_member_cap_v1(count:int,*,temporal_rows:int=50000)->None:
    _strict_int(count,"semantic_member_count");_strict_int(temporal_rows,"temporal_rows",1)
    if count>temporal_rows*8: raise ValueError("semantic member cap")
def validate_pack_bytes_v1(size:int,*,cap:int=131072)->None:
    _strict_int(size,"pack_bytes");_strict_int(cap,"pack_cap",1)
    if size>cap: raise ValueError("pack byte cap")

@dataclass(frozen=True)
class QueryPolicyV1:
    database_validation_rows:int; temporal_rows:int; normalized_bytes:int; matched_output_rows:int; matched_output_bytes:int
    def __post_init__(self):
        for name in ("database_validation_rows","temporal_rows","normalized_bytes","matched_output_rows","matched_output_bytes"): _strict_int(getattr(self,name),name,1)
        if self.temporal_rows>self.database_validation_rows: raise ValueError("temporal_rows cannot exceed validation rows")
        if self.database_validation_rows>100000 or self.temporal_rows>50000 or self.normalized_bytes>67108864 or self.matched_output_rows>200 or self.matched_output_bytes>262144: raise ValueError("test policy may only tighten production caps")
PRODUCTION_QUERY_POLICY_V1=QueryPolicyV1(100000,50000,67108864,200,262144)
QUERY_POLICY_V1=PRODUCTION_QUERY_POLICY_V1

@dataclass(frozen=True)
class LocalNewsQueryV1:
    published_from: datetime; as_of: datetime
    def __post_init__(self):
        _utc(self.published_from,"published_from"); _utc(self.as_of,"as_of")
        if self.published_from != self.as_of-timedelta(days=30): raise ValueError("query must use exact 30-day lookback")
@dataclass(frozen=True)
class MetadataMatchV1:
    group_id:str; bucket:CapexBucketV1; polarity:CapexEvidencePolarityV1
    def __post_init__(self):
        if type(self.group_id) is not str or not self.group_id or len(self.group_id.encode())>80 or not isinstance(self.bucket,CapexBucketV1) or not isinstance(self.polarity,CapexEvidencePolarityV1): raise ValueError("invalid metadata match")
@dataclass(frozen=True)
class LocalNewsArticleV1:
    news_document_id:str; source_id:str; source_article_id_digest:str|None; title_fingerprint:str; content_hash:str|None; content_mode:str; evidence_role:str; publisher_name_digest:str|None; published_at:datetime|None; first_seen_at:datetime; last_seen_at:datetime; available_at:datetime; revision_no:int; metadata_matches:tuple[MetadataMatchV1,...]; claimant_kind:ClaimantKindV1; claimant_ambiguous:bool; numeric_capex_language_present:bool; title_excerpt_digest:str; summary_excerpt_digest:str|None; normalized_excerpt_bytes:int; article_classification_digest:str
    def __post_init__(self):
        for name in ("news_document_id","source_id","title_fingerprint"):
            if type(getattr(self,name)) is not str or not getattr(self,name): raise ValueError(f"{name} required")
        if re.fullmatch(r"[0-9a-f]{32}",self.news_document_id) is None or re.fullmatch(r"[0-9a-f]{64}",self.source_id) is None or re.fullmatch(r"[0-9a-f]{64}",self.title_fingerprint) is None: raise ValueError("article identifiers")
        for name in ("source_article_id_digest","content_hash","publisher_name_digest","title_excerpt_digest","summary_excerpt_digest","article_classification_digest"):
            value=getattr(self,name)
            if value is not None:_digest(value,name)
        for name in ("published_at","first_seen_at","last_seen_at","available_at"):
            value=getattr(self,name)
            if value is not None:_utc(value,name)
        _strict_int(self.revision_no,"revision_no",1); _strict_int(self.normalized_excerpt_bytes,"normalized_excerpt_bytes")
        if type(self.content_mode) is not str or self.content_mode not in ("NEWS","OPINION") or type(self.evidence_role) is not str or self.evidence_role not in ("FACT_DISCOVERY","OPINION_DISCOVERY"): raise ValueError("closed news classification values")
        _tuple(self.metadata_matches,"metadata_matches")
        if not self.metadata_matches or len(set(self.metadata_matches))!=len(self.metadata_matches): raise ValueError("metadata matches nonempty unique")
        if type(self.claimant_ambiguous) is not bool or type(self.numeric_capex_language_present) is not bool or not isinstance(self.claimant_kind,ClaimantKindV1): raise ValueError("invalid classification flags")
        if self.last_seen_at<self.first_seen_at or self.available_at!=(self.first_seen_at if self.revision_no==1 else self.last_seen_at): raise ValueError("invalid availability")
@dataclass(frozen=True)
class AllMatchPolarityStoryV1:
    story_key:str; claimant_kinds_present:tuple[ClaimantKindV1,...]; claimant_ambiguous:bool; numeric_capex_present:bool; source_ids:tuple[str,...]; points:int; member_semantic_digest:str
    def __post_init__(self):
        if type(self.story_key) is not str or re.fullmatch(r"(?:content|title):[0-9a-f]{64}",self.story_key) is None: raise ValueError("invalid story key")
        _tuple(self.claimant_kinds_present,"claimant_kinds_present");_tuple(self.source_ids,"source_ids")
        if any(not isinstance(k,ClaimantKindV1) for k in self.claimant_kinds_present) or self.claimant_kinds_present!=tuple(k for k in ClaimantKindV1 if k in self.claimant_kinds_present) or not self.claimant_kinds_present or self.source_ids!=tuple(sorted(set(self.source_ids))) or not self.source_ids or any(type(s) is not str or re.fullmatch(r"[0-9a-f]{64}",s) is None for s in self.source_ids): raise ValueError("member union order")
        if type(self.claimant_ambiguous) is not bool or type(self.numeric_capex_present) is not bool:_strict_int(True,"member_flags")
        _strict_int(self.points,"points",0,2);_digest(self.member_semantic_digest,"member_semantic_digest")

@dataclass(frozen=True)
class AllMatchPolarityAggregateV1:
    bucket:CapexBucketV1; polarity:CapexEvidencePolarityV1; members:tuple[AllMatchPolarityStoryV1,...]; distinct_story_count:int; uncapped_points:int; claimant_story_counts:tuple[int,...]; claimant_ambiguous_story_count:int; numeric_capex_story_count:int; distinct_source_count:int; story_key_membership_digest:str; source_membership_digest:str; claimant_membership_digest:str; numeric_membership_digest:str; membership_digest:str; aggregate_digest:str
    def __post_init__(self):
        _tuple(self.members,"members");_tuple(self.claimant_story_counts,"claimant_story_counts")
        if not isinstance(self.bucket,CapexBucketV1) or not isinstance(self.polarity,CapexEvidencePolarityV1): raise ValueError("polarity aggregate enums")
        for name in ("distinct_story_count","uncapped_points","claimant_ambiguous_story_count","numeric_capex_story_count","distinct_source_count"):_strict_int(getattr(self,name),name)
        if len(self.claimant_story_counts)!=len(ClaimantKindV1): raise ValueError("claimant count shape")
        for value in self.claimant_story_counts:_strict_int(value,"claimant_story_count")
        if self.members!=tuple(sorted(self.members,key=lambda m:m.story_key)) or len({m.story_key for m in self.members})!=len(self.members): raise ValueError("polarity member order")
        if self.distinct_story_count!=len(self.members) or self.uncapped_points!=sum(m.points for m in self.members) or (self.polarity is CapexEvidencePolarityV1.CONTEXT and self.uncapped_points!=0): raise ValueError("polarity arithmetic")
        expected=tuple(sum(k in m.claimant_kinds_present for m in self.members) for k in ClaimantKindV1)
        sources=sorted({s for m in self.members for s in m.source_ids})
        if self.claimant_story_counts!=expected or self.claimant_ambiguous_story_count!=sum(m.claimant_ambiguous for m in self.members) or self.numeric_capex_story_count!=sum(m.numeric_capex_present for m in self.members) or self.distinct_source_count!=len(sources): raise ValueError("polarity projections")
        for m in self.members:
            if m.points!=(0 if self.polarity is CapexEvidencePolarityV1.CONTEXT else m.points): raise ValueError("context points")
            if m.member_semantic_digest!=digest_v1({"domain":"all-match-polarity-story-v1","classifier_digest":CLASSIFIER_TABLE_DIGEST_V1,"spec_digest":SPEC_DIGEST_V1,"bucket":self.bucket,"polarity":self.polarity,"story_key":m.story_key,"claimant_kinds_present":m.claimant_kinds_present,"claimant_ambiguous":m.claimant_ambiguous,"numeric_capex_present":m.numeric_capex_present,"source_ids":m.source_ids,"points":m.points}): raise ValueError("member semantic digest mismatch")
        projections={"story_key_membership_digest":digest_v1(tuple(m.story_key for m in self.members)),"source_membership_digest":digest_v1(tuple(sources)),"claimant_membership_digest":digest_v1(tuple((m.story_key,m.claimant_kinds_present,m.claimant_ambiguous) for m in self.members)),"numeric_membership_digest":digest_v1(tuple((m.story_key,m.numeric_capex_present) for m in self.members))}
        if any(getattr(self,k)!=v for k,v in projections.items()): raise ValueError("projection digest mismatch")
        if self.membership_digest!=digest_v1({"member_semantic_digests":tuple(m.member_semantic_digest for m in self.members),**projections}): raise ValueError("membership digest mismatch")
        if self.aggregate_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="aggregate_digest"}): raise ValueError("polarity aggregate digest mismatch")

@dataclass(frozen=True)
class AllMatchBucketAggregateV1:
    bucket:CapexBucketV1; polarity_aggregates:tuple[AllMatchPolarityAggregateV1,...]; support_distinct_story_count:int; counter_distinct_story_count:int; context_distinct_story_count:int; support_uncapped_points:int; counter_uncapped_points:int; support_points:int; counter_points:int; net_points:int; conflict:bool; claimant_story_counts:tuple[int,...]; claimant_ambiguous_story_count:int; numeric_capex_story_count:int; distinct_source_count:int; aggregate_digest:str
    def __post_init__(self):
        if not isinstance(self.bucket,CapexBucketV1): raise ValueError("bucket enum required")
        _tuple(self.polarity_aggregates,"polarity_aggregates")
        if tuple(p.polarity for p in self.polarity_aggregates)!=tuple(CapexEvidencePolarityV1) or any(p.bucket is not self.bucket for p in self.polarity_aggregates): raise ValueError("polarity aggregate order")
        for name in ("support_distinct_story_count","counter_distinct_story_count","context_distinct_story_count","support_uncapped_points","counter_uncapped_points","support_points","counter_points","claimant_ambiguous_story_count","numeric_capex_story_count","distinct_source_count"):_strict_int(getattr(self,name),name)
        if type(self.net_points) is not int or not -6<=self.net_points<=6: raise ValueError("net_points invalid")
        _tuple(self.claimant_story_counts,"claimant_story_counts")
        if len(self.claimant_story_counts)!=len(ClaimantKindV1) or any(type(x) is not int or x<0 for x in self.claimant_story_counts): raise ValueError("claimant counts")
        support,counter,context=self.polarity_aggregates
        if (self.support_distinct_story_count,self.counter_distinct_story_count,self.context_distinct_story_count,self.support_uncapped_points,self.counter_uncapped_points)!=(support.distinct_story_count,counter.distinct_story_count,context.distinct_story_count,support.uncapped_points,counter.uncapped_points): raise ValueError("bucket polarity projections")
        all_members={m.story_key:m for p in self.polarity_aggregates for m in p.members}; claimant=tuple(sum(k in {kind for p in self.polarity_aggregates for m in p.members if m.story_key==story for kind in m.claimant_kinds_present} for story in all_members) for k in ClaimantKindV1)
        ambiguous=sum(any(m.claimant_ambiguous for p in self.polarity_aggregates for m in p.members if m.story_key==story) for story in all_members);numeric=sum(any(m.numeric_capex_present for p in self.polarity_aggregates for m in p.members if m.story_key==story) for story in all_members);sources={s for p in self.polarity_aggregates for m in p.members for s in m.source_ids}
        if self.claimant_story_counts!=claimant or self.claimant_ambiguous_story_count!=ambiguous or self.numeric_capex_story_count!=numeric or self.distinct_source_count!=len(sources): raise ValueError("bucket union projections")
        if self.support_points!=min(6,self.support_uncapped_points) or self.counter_points!=min(6,self.counter_uncapped_points) or self.net_points!=self.support_points-self.counter_points or type(self.conflict) is not bool or self.conflict!=(self.support_distinct_story_count>0 and self.counter_distinct_story_count>0): raise ValueError("aggregate arithmetic")
        _digest(self.aggregate_digest,"aggregate_digest")
        if self.aggregate_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="aggregate_digest"}): raise ValueError("bucket aggregate digest mismatch")
@dataclass(frozen=True)
class AllMatchSemanticAggregateV1:
    bucket_aggregates:tuple[AllMatchBucketAggregateV1,...]; claimant_story_counts:tuple[int,...]; claimant_ambiguous_story_count:int; numeric_capex_story_count:int; hyperscaler_context_story_count:int; destination_story_count:int; matched_distinct_source_count:int; all_match_semantic_digest:str
    def __post_init__(self):
        _tuple(self.bucket_aggregates,"bucket_aggregates"); _tuple(self.claimant_story_counts,"claimant_story_counts")
        if tuple(x.bucket for x in self.bucket_aggregates)!=tuple(CapexBucketV1) or len(self.claimant_story_counts)!=len(ClaimantKindV1): raise ValueError("semantic enum order")
        for name in ("claimant_ambiguous_story_count","numeric_capex_story_count","hyperscaler_context_story_count","destination_story_count","matched_distinct_source_count"):_strict_int(getattr(self,name),name)
        if any(type(x) is not int or x<0 for x in self.claimant_story_counts): raise ValueError("semantic claimant counts")
        validate_semantic_member_cap_v1(sum(len(p.members) for b in self.bucket_aggregates for p in b.polarity_aggregates))
        story_members={}
        for bucket in self.bucket_aggregates:
            for polarity in bucket.polarity_aggregates:
                for member in polarity.members:story_members.setdefault(member.story_key,[]).append((bucket.bucket,member))
        global_claimants=tuple(sum(any(k in member.claimant_kinds_present for _,member in entries) for entries in story_members.values()) for k in ClaimantKindV1);global_ambiguous=sum(any(member.claimant_ambiguous for _,member in entries) for entries in story_members.values());global_numeric=sum(any(member.numeric_capex_present for _,member in entries) for entries in story_members.values());hyper=sum(any(bucket is CapexBucketV1.HYPERSCALER_CAPEX_CONTEXT for bucket,_ in entries) for entries in story_members.values());destination=sum(any(bucket is not CapexBucketV1.HYPERSCALER_CAPEX_CONTEXT for bucket,_ in entries) for entries in story_members.values());sources={s for entries in story_members.values() for _,member in entries for s in member.source_ids}
        if (self.claimant_story_counts,self.claimant_ambiguous_story_count,self.numeric_capex_story_count,self.hyperscaler_context_story_count,self.destination_story_count,self.matched_distinct_source_count)!=(global_claimants,global_ambiguous,global_numeric,hyper,destination,len(sources)): raise ValueError("global semantic projections")
        _digest(self.all_match_semantic_digest,"all_match_semantic_digest")
        if self.all_match_semantic_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="all_match_semantic_digest"}): raise ValueError("semantic aggregate digest mismatch")
@dataclass(frozen=True)
class LocalNewsCoverageV1:
    requested_from:datetime; requested_as_of:datetime; database_validation_row_count:int; available_effective_from:datetime|None; available_effective_to:datetime|None; available_at_to:datetime|None; temporal_candidate_count:int; temporal_candidate_normalized_bytes:int; future_source_skew_count:int; future_availability_count:int; total_matched_rows:int; total_matched_distinct_stories:int; returned_count:int; returned_distinct_story_count:int; matched_distinct_source_count:int; total_matched_summaries_missing:int; total_matched_revisions_present:int; total_matched_duplicate_cluster_count:int; truncated_rows:int; truncated_bytes:int; coverage_gap:str|None
    def __post_init__(self):
        _utc(self.requested_from,"requested_from");_utc(self.requested_as_of,"requested_as_of")
        if self.requested_from!=self.requested_as_of-timedelta(days=30): raise ValueError("coverage lookback")
        for name in ("available_effective_from","available_effective_to","available_at_to"):
            if getattr(self,name) is not None:_utc(getattr(self,name),name)
        for name in ("database_validation_row_count","temporal_candidate_count","temporal_candidate_normalized_bytes","future_source_skew_count","future_availability_count","total_matched_rows","total_matched_distinct_stories","returned_count","returned_distinct_story_count","matched_distinct_source_count","total_matched_summaries_missing","total_matched_revisions_present","total_matched_duplicate_cluster_count","truncated_rows","truncated_bytes"):_strict_int(getattr(self,name),name)
        if self.returned_count>self.total_matched_rows or self.returned_distinct_story_count>self.total_matched_distinct_stories or self.total_matched_distinct_stories>self.total_matched_rows or self.truncated_rows!=self.total_matched_rows-self.returned_count or self.total_matched_summaries_missing>self.total_matched_rows or self.total_matched_revisions_present>self.total_matched_rows: raise ValueError("coverage count inconsistency")
        if self.coverage_gap not in (None,"LATEST_REVISION_NOT_HISTORICALLY_RECONSTRUCTABLE"): raise ValueError("coverage gap")
@dataclass(frozen=True)
class LocalNewsQueryResultV1:
    status:NewsQueryStatusV1; query_digest:str; source_snapshot_digest:str; result_digest:str; articles:tuple[LocalNewsArticleV1,...]; coverage:LocalNewsCoverageV1|None; all_match_semantics:AllMatchSemanticAggregateV1|None; error_code:str|None
    def __post_init__(self):
        if not isinstance(self.status,NewsQueryStatusV1): raise ValueError("query status enum")
        for name in ("query_digest","source_snapshot_digest","result_digest"):_digest(getattr(self,name),name)
        _tuple(self.articles,"articles")
        if self.status is NewsQueryStatusV1.OK:
            if self.coverage is None or self.all_match_semantics is None or self.error_code is not None: raise ValueError("OK result shape")
            if len(self.articles)!=self.coverage.returned_count or self.coverage.matched_distinct_source_count!=self.all_match_semantics.matched_distinct_source_count: raise ValueError("result coverage cross-link")
        elif self.articles or self.coverage is not None or self.all_match_semantics is not None or type(self.error_code) is not str or not self.error_code: raise ValueError("failure result shape")
        if self.result_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="result_digest"}): raise ValueError("result digest mismatch")
@dataclass(frozen=True)
class CapexEvidenceRefV1:
    evidence_class:EvidenceClassV1; claimant_kind:ClaimantKindV1; polarity:CapexEvidencePolarityV1; bucket:CapexBucketV1; source_id:str; news_document_ids:tuple[str,...]; source_article_id_digests:tuple[str,...]; distinct_story_key:str; effective_at:datetime; available_at:datetime; descriptor:str; title_excerpt_digest:str; summary_excerpt_digest:str|None; semantic_member_digest:str; evidence_digest:str
    def __post_init__(self):
        _tuple(self.news_document_ids,"news_document_ids",8);_tuple(self.source_article_id_digests,"source_article_id_digests",8)
        if not isinstance(self.evidence_class,EvidenceClassV1) or not isinstance(self.claimant_kind,ClaimantKindV1) or not isinstance(self.polarity,CapexEvidencePolarityV1) or not isinstance(self.bucket,CapexBucketV1): raise ValueError("evidence enums")
        if not self.news_document_ids or len(set(self.news_document_ids))!=len(self.news_document_ids) or self.news_document_ids!=tuple(sorted(self.news_document_ids)) or any(type(x) is not str or re.fullmatch(r"[0-9a-f]{32}",x) is None for x in self.news_document_ids) or self.source_article_id_digests!=tuple(sorted(set(self.source_article_id_digests))) or type(self.source_id) is not str or re.fullmatch(r"[0-9a-f]{64}",self.source_id) is None or type(self.distinct_story_key) is not str or re.fullmatch(r"(?:content|title):[0-9a-f]{64}",self.distinct_story_key) is None or len(self.descriptor.encode())>160: raise ValueError("evidence cap")
        if self.evidence_class is EvidenceClassV1.VERIFIED_FACT: raise ValueError("metadata cannot verify fact")
        for name in ("title_excerpt_digest","semantic_member_digest","evidence_digest"):_digest(getattr(self,name),name)
        if self.summary_excerpt_digest is not None:_digest(self.summary_excerpt_digest,"summary_excerpt_digest")
        for value in self.source_article_id_digests:_digest(value,"source_article_id_digest")
        _utc(self.effective_at,"effective_at");_utc(self.available_at,"available_at")
        _safe_output_text(self.descriptor,"descriptor",160)
        if self.evidence_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="evidence_digest"}): raise ValueError("evidence digest mismatch")
@dataclass(frozen=True)
class MissingEvidenceNeedV1:
    need_id:str; bucket:CapexBucketV1; required_evidence_class:EvidenceClassV1; reason_code:MissingReasonCodeV1; official_source_kind:OfficialSourceKindV1; subject_hint_digest:str; earliest_needed_at:datetime; as_of:datetime; automatic_search_starts:int; need_digest:str
    def __post_init__(self):
        if self.required_evidence_class is not EvidenceClassV1.VERIFIED_FACT or type(self.automatic_search_starts) is not int or self.automatic_search_starts!=0: raise ValueError("missing need is inert official verification")
        if not isinstance(self.bucket,CapexBucketV1) or not isinstance(self.reason_code,MissingReasonCodeV1) or not isinstance(self.official_source_kind,OfficialSourceKindV1): raise ValueError("missing need enums")
        for name in ("need_id","subject_hint_digest","need_digest"):_digest(getattr(self,name),name)
        _utc(self.earliest_needed_at,"earliest_needed_at");_utc(self.as_of,"as_of")
        if self.reason_code is MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET:
            if self.bucket not in COVERAGE_FALLBACK_BUCKETS_V1 or self.official_source_kind is not OfficialSourceKindV1.ISSUER_IR or self.earliest_needed_at!=self.as_of or self.subject_hint_digest!=coverage_fallback_subject_hint_digest_v1(self.bucket,self.as_of) or self.need_id!=coverage_fallback_need_id_v1(self.bucket,self.as_of): raise ValueError("fallback need shape")
        if self.need_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="need_digest"}): raise ValueError("need digest mismatch")
@dataclass(frozen=True)
class BucketAssessmentV1:
    bucket:CapexBucketV1; support_points:int; counter_points:int; net_points:int; support_distinct_stories:int; counter_distinct_stories:int; distinct_sources:int; conflict:bool; qualitative_only:bool; inference_label:str
    def __post_init__(self):
        if not isinstance(self.bucket,CapexBucketV1): raise ValueError("assessment bucket enum")
        for name in ("support_points","counter_points","support_distinct_stories","counter_distinct_stories","distinct_sources"):_strict_int(getattr(self,name),name)
        if type(self.net_points) is not int or not -6<=self.net_points<=6 or type(self.conflict) is not bool: raise ValueError("assessment strict fields")
        if self.support_points>6 or self.counter_points>6 or self.net_points!=self.support_points-self.counter_points or self.conflict!=(self.support_distinct_stories>0 and self.counter_distinct_stories>0) or self.qualitative_only is not True or self.inference_label!="metadata-only sensor inference": raise ValueError("assessment arithmetic")
@dataclass(frozen=True)
class OmittedEvidenceSummaryV1:
    bucket:CapexBucketV1; polarity:CapexEvidencePolarityV1; omitted_distinct_story_count:int; omitted_points:int; claimant_kinds_present:tuple[ClaimantKindV1,...]; numeric_capex_present:bool; summary_digest:str
    def __post_init__(self):
        _strict_int(self.omitted_distinct_story_count,"omitted_distinct_story_count",1);_strict_int(self.omitted_points,"omitted_points");_tuple(self.claimant_kinds_present,"claimant_kinds_present");_digest(self.summary_digest,"summary_digest")
        if not isinstance(self.bucket,CapexBucketV1) or not isinstance(self.polarity,CapexEvidencePolarityV1) or type(self.numeric_capex_present) is not bool or any(not isinstance(k,ClaimantKindV1) for k in self.claimant_kinds_present) or self.claimant_kinds_present!=tuple(k for k in ClaimantKindV1 if k in self.claimant_kinds_present): raise ValueError("omitted evidence shape")
@dataclass(frozen=True)
class OmittedNeedSummaryV1:
    total_omitted:int; reason_counts:tuple[tuple[MissingReasonCodeV1,int],...]; bucket_counts:tuple[tuple[CapexBucketV1,int],...]; omitted_membership_digest:str; summary_digest:str
    def __post_init__(self):
        _strict_int(self.total_omitted,"total_omitted",1);_tuple(self.reason_counts,"reason_counts");_tuple(self.bucket_counts,"bucket_counts");_digest(self.omitted_membership_digest,"omitted_membership_digest");_digest(self.summary_digest,"summary_digest")
        if sum(n for _,n in self.reason_counts)!=self.total_omitted or sum(n for _,n in self.bucket_counts)!=self.total_omitted: raise ValueError("omitted need accounting")
        if len({r for r,_ in self.reason_counts})!=len(self.reason_counts) or len({b for b,_ in self.bucket_counts})!=len(self.bucket_counts) or self.reason_counts!=tuple((r,n) for r in MissingReasonCodeV1 for rr,n in self.reason_counts if rr is r) or self.bucket_counts!=tuple((b,n) for b in CapexBucketV1 for bb,n in self.bucket_counts if bb is b) or any(type(n) is not int or n<=0 for _,n in self.reason_counts+self.bucket_counts): raise ValueError("omitted need order")
@dataclass(frozen=True)
class CapexAllocationEvidencePackV1:
    program_id:str; version:str; kind:str; formula_version:str; spec_digest:str; lifecycle_status:str; lifecycle_digest:str; as_of:datetime; query:LocalNewsQueryV1; query_digest:str; source_snapshot_digest:str; query_result_digest:str; all_match_semantic_digest:str|None; state:CapexSensorStateV1; assessments:tuple[BucketAssessmentV1,...]; evidence:tuple[CapexEvidenceRefV1,...]; contradictions:tuple[CapexEvidenceRefV1,...]; omitted_evidence_summaries:tuple[OmittedEvidenceSummaryV1,...]; missing_needs:tuple[MissingEvidenceNeedV1,...]; omitted_need_summary:OmittedNeedSummaryV1|None; coverage:LocalNewsCoverageV1|None; limitations:tuple[str,...]; privacy_scan_passed:bool; pack_digest:str
    def __post_init__(self):
        if len(self.evidence)>40 or len(self.contradictions)>20 or len(self.omitted_evidence_summaries)>12 or len(self.missing_needs)>16 or len(self.limitations)>16: raise ValueError("pack field cap")
        if type(self.privacy_scan_passed) is not bool or not self.privacy_scan_passed: raise ValueError("pack privacy")
        if not isinstance(self.state,CapexSensorStateV1) or type(self.lifecycle_status) is not str or self.lifecycle_status not in ("ACTIVE","REVIEW_DUE"): raise ValueError("pack state/lifecycle enum")
        if self.state is CapexSensorStateV1.INSUFFICIENT_LOCAL_EVIDENCE and not self.missing_needs: raise ValueError("insufficient state requires selected missing need")
        validate_pack_bytes_v1(len(canonical_json_bytes(self)))
        for name in ("spec_digest","lifecycle_digest","query_digest","source_snapshot_digest","query_result_digest","pack_digest"):_digest(getattr(self,name),name)
        if self.all_match_semantic_digest is not None:_digest(self.all_match_semantic_digest,"all_match_semantic_digest")
        for name in ("assessments","evidence","contradictions","omitted_evidence_summaries","missing_needs","limitations"):_tuple(getattr(self,name),name)
        if tuple(a.bucket for a in self.assessments) not in ((),tuple(CapexBucketV1)) or len({e.evidence_digest for e in self.evidence})!=len(self.evidence): raise ValueError("pack ordering/dedupe")
        for value in self.limitations:_safe_output_text(value,"limitation",240)
        if any(e.evidence_class is EvidenceClassV1.CONTRADICTION for e in self.evidence) or any(e.evidence_class is not EvidenceClassV1.CONTRADICTION for e in self.contradictions): raise ValueError("evidence lane separation")
        if len({(s.bucket,s.polarity) for s in self.omitted_evidence_summaries})!=len(self.omitted_evidence_summaries) or len({(n.bucket,n.reason_code,n.official_source_kind) for n in self.missing_needs})!=len(self.missing_needs): raise ValueError("pack semantic dedupe")
        reason_order=list(MissingReasonCodeV1);source_order=list(OfficialSourceKindV1)
        if self.missing_needs!=tuple(sorted(self.missing_needs,key=lambda n:(list(CapexBucketV1).index(n.bucket),reason_order.index(n.reason_code),source_order.index(n.official_source_kind),n.need_id))): raise ValueError("missing need order")
        fallback=tuple(n.bucket for n in self.missing_needs if n.reason_code is MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET)
        if fallback!=COVERAGE_FALLBACK_BUCKETS_V1[:len(fallback)]: raise ValueError("fallback need prefix")
        omitted_fallback=0 if self.omitted_need_summary is None else dict(self.omitted_need_summary.reason_counts).get(MissingReasonCodeV1.LOCAL_COVERAGE_MINIMUM_UNMET,0)
        if not fallback:
            if omitted_fallback: raise ValueError("fallback cannot appear only omitted")
        elif len(fallback)<len(COVERAGE_FALLBACK_BUCKETS_V1):
            suffix=COVERAGE_FALLBACK_BUCKETS_V1[len(fallback):]
            bucket_counts={} if self.omitted_need_summary is None else dict(self.omitted_need_summary.bucket_counts)
            if omitted_fallback!=len(suffix) or any(bucket_counts.get(bucket,0)<1 for bucket in suffix): raise ValueError("fallback omission accounting")
        elif omitted_fallback: raise ValueError("fully selected fallback cannot be omitted")
        if (self.program_id,self.version,self.kind,self.formula_version,self.spec_digest)!=(PROGRAM_ID_V1,PROGRAM_VERSION_V1,"HYPOTHESIS_SENSOR",FORMULA_VERSION_V1,SPEC_DIGEST_V1): raise ValueError("pack frozen identity")
        if self.pack_digest!=digest_v1({f.name:getattr(self,f.name) for f in fields(self) if f.name!="pack_digest"}): raise ValueError("pack digest mismatch")
@dataclass(frozen=True)
class CapexSensorRunV1:
    pack:CapexAllocationEvidencePackV1|None; blocked_reason:Any|None
    def __post_init__(self):
        if (self.pack is None)==(self.blocked_reason is None): raise ValueError("exactly one run outcome required")
