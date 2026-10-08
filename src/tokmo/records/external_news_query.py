"""Strict, bounded, read-only n5 NEWS query for G1-C."""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat
import unicodedata
from urllib.parse import quote

from tokmo.values.ai_infra_capex import (
    AllMatchBucketAggregateV1, AllMatchPolarityAggregateV1,
    AllMatchPolarityStoryV1, AllMatchSemanticAggregateV1, CAPEX_TERM_GROUPS_V1,
    CLASSIFIER_TABLE_DIGEST_V1, CLAIMANT_MARKERS_V1, COUNTER_MARKERS_V1,
    NUMERIC_CAPEX_MARKERS_V1, NUMERIC_PATTERN_V1, SUPPORT_MARKERS_V1,
    CapexBucketV1, CapexEvidencePolarityV1, ClaimantKindV1,
    LocalNewsArticleV1, LocalNewsCoverageV1, LocalNewsQueryResultV1,
    LocalNewsQueryV1, MetadataMatchV1, NewsQueryStatusV1, canonical_json_bytes,
    QueryPolicyV1, PRODUCTION_QUERY_POLICY_V1, SPEC_DIGEST_V1, digest_v1,
)

_MAX_DB_ROWS=100_000; _MAX_TEMPORAL_ROWS=50_000; _MAX_NORMALIZED_BYTES=67_108_864
_MAX_MATCHES=200; _MAX_MATCH_BYTES=262_144
_TS=re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}\+00:00\Z")
_NUMERIC=re.compile(NUMERIC_PATTERN_V1)
_NEWS_SCAN_SQL_V1="SELECT {columns} FROM news_documents ORDER BY news_document_id ASC"
_REPRESENTATIVE_ORDER_V1=("available_at_desc","effective_at_desc","source_id_asc","news_document_id_asc")
def _query_digest(query,policy):
    caps={"database_validation_rows":policy.database_validation_rows,"temporal_rows":policy.temporal_rows,"normalized_bytes":policy.normalized_bytes,"matched_output_rows":policy.matched_output_rows,"matched_output_bytes":policy.matched_output_bytes,"semantic_members_per_temporal_row":8,"title_bytes":512,"summary_bytes":1024}
    return digest_v1({"query":query,"contract":"local-news-query-v1","classifier":CLASSIFIER_TABLE_DIGEST_V1,"ordering":_REPRESENTATIVE_ORDER_V1,"caps":caps})

def _failure(query:LocalNewsQueryV1,status:NewsQueryStatusV1,code:str,policy:QueryPolicyV1=PRODUCTION_QUERY_POLICY_V1)->LocalNewsQueryResultV1:
    qd=_query_digest(query,policy)
    base={"status":status.value,"query_digest":qd,"error_code":code}
    snapshot=digest_v1(base);result_fields={"status":status,"query_digest":qd,"source_snapshot_digest":snapshot,"articles":(),"coverage":None,"all_match_semantics":None,"error_code":code}
    return LocalNewsQueryResultV1(**result_fields,result_digest=digest_v1(result_fields))

def _validate_readonly_news_path_v1(path:Path)->Path:
    if type(path) is not Path: path=Path(path)
    absolute=path.absolute(); parent=absolute.parent
    current=Path(absolute.anchor)
    for part in absolute.parts[1:]:
        current=current/part
        if stat.S_ISLNK(current.lstat().st_mode): raise OSError("unsafe_path_component")
    ps=parent.lstat(); fs=path.lstat()
    uid=os.geteuid()
    if stat.S_ISLNK(ps.st_mode) or not stat.S_ISDIR(ps.st_mode) or ps.st_uid!=uid or ps.st_mode&0o077: raise OSError("unsafe_parent")
    if stat.S_ISLNK(fs.st_mode) or not stat.S_ISREG(fs.st_mode) or fs.st_uid!=uid or fs.st_nlink!=1 or fs.st_mode&0o077: raise OSError("unsafe_file")
    return absolute

def _parse_ts(raw:str|None,nullable:bool)->datetime|None:
    if raw is None:
        if nullable:return None
        raise ValueError("required timestamp null")
    if type(raw) is not str or not _TS.fullmatch(raw): raise ValueError("timestamp shape")
    dt=datetime.fromisoformat(raw)
    if dt.utcoffset() is None or dt.utcoffset().total_seconds()!=0 or dt.isoformat(timespec="microseconds")!=raw: raise ValueError("timestamp roundtrip")
    return dt.astimezone(timezone.utc)

def _norm(value:str|None,cap:int)->tuple[str,str,int]:
    full=" ".join(unicodedata.normalize("NFKC",value or "").split()).casefold()
    raw=full.encode(); cut=raw[:cap]
    while cut:
        try: bounded=cut.decode(); break
        except UnicodeDecodeError: cut=cut[:-1]
    else: bounded=""
    return bounded,hashlib.sha256(raw).hexdigest(),len(cut)

class _ReadAuthorizerV1:
    def __init__(self): self._begins=0;self._rollbacks=0
    def __call__(self,action,p1,p2,db,trigger):
        if action==sqlite3.SQLITE_SELECT:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_READ and (p1 or "") in {"schema_meta","source_families","news_documents","sqlite_master","sqlite_schema"}:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_PRAGMA and (p1 or "").lower() in {"data_version","table_list"} and p2 is None:return sqlite3.SQLITE_OK
        if action==sqlite3.SQLITE_TRANSACTION:
            verb=(p1 or "").upper()
            if verb=="BEGIN" and self._begins==0:self._begins=1;return sqlite3.SQLITE_OK
            if verb=="ROLLBACK" and self._begins==1 and self._rollbacks==0:self._rollbacks=1;return sqlite3.SQLITE_OK
        return sqlite3.SQLITE_DENY

def _classify(text:str,role:str):
    matches=[]; support=any(x in text for x in SUPPORT_MARKERS_V1); counter=any(x in text for x in COUNTER_MARKERS_V1)
    pols=(CapexEvidencePolarityV1.SUPPORT,CapexEvidencePolarityV1.COUNTER) if support and counter else ((CapexEvidencePolarityV1.SUPPORT,) if support else ((CapexEvidencePolarityV1.COUNTER,) if counter else (CapexEvidencePolarityV1.CONTEXT,)))
    for group in CAPEX_TERM_GROUPS_V1:
        if any(term in text for term in group.terms): matches.extend(MetadataMatchV1(group.group_id,group.bucket,p) for p in pols)
    families=[kind for kind,markers in CLAIMANT_MARKERS_V1 if any(m in text for m in markers)]
    nonanalyst=[x for x in families if x is not ClaimantKindV1.ANALYST_OR_OPINION]
    opinion=role=="OPINION_DISCOVERY"
    ambiguous=len(nonanalyst)>1 or (opinion and bool(nonanalyst)) or (ClaimantKindV1.ANALYST_OR_OPINION in families and bool(nonanalyst))
    if ambiguous: claimant=ClaimantKindV1.UNSPECIFIED
    elif opinion or ClaimantKindV1.ANALYST_OR_OPINION in families: claimant=ClaimantKindV1.ANALYST_OR_OPINION
    elif len(nonanalyst)==1: claimant=nonanalyst[0]
    else: claimant=ClaimantKindV1.UNSPECIFIED
    numeric=bool(_NUMERIC.search(text)) and any(x in text for x in NUMERIC_CAPEX_MARKERS_V1)
    return tuple(sorted(set(matches),key=lambda m:(list(CapexBucketV1).index(m.bucket),m.group_id,list(CapexEvidencePolarityV1).index(m.polarity)))),claimant,ambiguous,numeric

def query_local_news_v1(path:Path,query:LocalNewsQueryV1,*,policy:QueryPolicyV1=PRODUCTION_QUERY_POLICY_V1)->LocalNewsQueryResultV1:
    if not isinstance(policy,QueryPolicyV1): raise ValueError("policy must be QueryPolicyV1")
    try: absolute=_validate_readonly_news_path_v1(path)
    except (OSError,ValueError): return _failure(query,NewsQueryStatusV1.STORE_UNAVAILABLE,"unsafe_or_missing_store",policy)
    connection=None
    try:
        connection=sqlite3.connect(f"file:{quote(str(absolute),safe='/')}?mode=ro",uri=True,timeout=0,isolation_level=None)
        connection.execute("PRAGMA query_only=ON"); connection.execute("PRAGMA busy_timeout=0"); connection.execute("PRAGMA temp_store=MEMORY")
        if connection.execute("PRAGMA query_only").fetchone()!=(1,) or connection.execute("PRAGMA busy_timeout").fetchone()!=(0,) or connection.execute("PRAGMA temp_store").fetchone()!=(2,): return _failure(query,NewsQueryStatusV1.STORE_UNAVAILABLE,"pragma_mismatch",policy)
        authorizer=_ReadAuthorizerV1(); connection.set_authorizer(authorizer); connection.execute("BEGIN DEFERRED")
        meta=connection.execute("SELECT schema_version,manifest_revision,schema_id,ddl_sha256 FROM schema_meta WHERE singleton=1").fetchone()
        if not meta or meta[:3]!=(5,5,"external-news-n5"): return _failure(query,NewsQueryStatusV1.SCHEMA_MISMATCH,"schema_meta",policy)
        required={"schema_meta","source_families","sources","source_connectors","intake_requests","intake_receipts","news_documents"}
        table_rows=[r for r in connection.execute("PRAGMA table_list") if r[2]=="table" and not r[1].startswith("sqlite_")]; tables={r[1] for r in table_rows}
        ddl_rows=connection.execute("SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name").fetchall()
        ddl=hashlib.sha256(b"".join(b"\0".join(field.encode() for field in row)+b"\n" for row in ddl_rows)).hexdigest()
        registry=connection.execute("SELECT family_key,display_name,adapter_key,record_type_key FROM source_families ORDER BY family_key").fetchall()
        if tables!=required or any(r[5]!=1 for r in table_rows) or meta[3]!=ddl or registry!=[("NEWS","News documents","news_document_store","news_document")]:return _failure(query,NewsQueryStatusV1.SCHEMA_MISMATCH,"table_set_or_ddl",policy)
        cols="news_document_id,source_id,source_article_id,title_fingerprint,content_hash,content_mode,evidence_role,publisher_name,published_at,first_seen_at,last_seen_at,revision_no,content_state,title,summary_text"
        scanned_row_digests=[];database_rows=0;temporal=[];norm_bytes=0;future_pub=0;future_avail=0
        data_version=connection.execute("PRAGMA data_version").fetchone()[0]
        for row in connection.execute(_NEWS_SCAN_SQL_V1.format(columns=cols)):
            if database_rows>=policy.database_validation_rows:return _failure(query,NewsQueryStatusV1.CAP_EXCEEDED,"database_validation_rows",policy)
            database_rows+=1;scanned_row_digests.append(digest_v1(row))
            try:
                pub=_parse_ts(row[8],True); first=_parse_ts(row[9],False); last=_parse_ts(row[10],False)
                if last<first: raise ValueError
            except ValueError:return _failure(query,NewsQueryStatusV1.MALFORMED_TIMESTAMP,"news_document_timestamp",policy)
            if row[12]!="METADATA_ONLY" or row[5] not in {"NEWS","OPINION"} or row[6] not in {"FACT_DISCOVERY","OPINION_DISCOVERY"} or first>query.as_of: continue
            effective=pub or first
            if pub is not None and pub>query.as_of: future_pub+=1; continue
            available=first if row[11]==1 else last
            if available>query.as_of: future_avail+=1; continue
            if effective<query.published_from or effective>query.as_of:continue
            title,td,tb=_norm(row[13],512); summary,sd,sb=_norm(row[14],1024); norm_bytes+=tb+sb
            if len(temporal)>=policy.temporal_rows or norm_bytes>policy.normalized_bytes:return _failure(query,NewsQueryStatusV1.CAP_EXCEEDED,"temporal_envelope",policy)
            metadata=(row[0],row[1],hashlib.sha256(row[2].encode()).hexdigest() if row[2] else None,row[3],row[4],row[5],row[6],hashlib.sha256(row[7].encode()).hexdigest() if row[7] else None,row[11])
            temporal.append((metadata,pub,first,last,effective,available,title,summary,td,sd if row[14] is not None else None,tb+sb))
        articles=[]
        for row,pub,first,last,effective,available,title,summary,td,sd,nbytes in temporal:
            mm,claimant,ambiguous,numeric=_classify(f"{title} {summary}",row[6])
            if not mm:continue
            base={"id":row[0],"matches":mm,"claimant":claimant,"ambiguous":ambiguous,"numeric":numeric,"classifier":CLASSIFIER_TABLE_DIGEST_V1,"title_digest":td,"summary_digest":sd}
            articles.append(LocalNewsArticleV1(row[0],row[1],row[2],row[3],row[4],row[5],row[6],row[7],pub,first,last,available,row[8],mm,claimant,ambiguous,numeric,td,sd,nbytes,digest_v1(base)))
        articles.sort(key=lambda a:(-a.available_at.timestamp(),-(a.published_at or a.first_seen_at).timestamp(),a.source_id,a.news_document_id))
        story=lambda a:"content:"+a.content_hash if a.content_hash else "title:"+a.title_fingerprint
        bucket_story=defaultdict(set); claimant_stories=defaultdict(set); ambiguous_stories=set(); numeric_stories=set()
        weights={ClaimantKindV1.COMPANY:2,ClaimantKindV1.SUPPLIER:1,ClaimantKindV1.ANALYST_OR_OPINION:1,ClaimantKindV1.UNSPECIFIED:1}; member_data={}
        for a in articles:
            sk=story(a); claimant_stories[a.claimant_kind].add(sk)
            if a.claimant_ambiguous:ambiguous_stories.add(sk)
            if a.numeric_capex_language_present:numeric_stories.add(sk)
            for m in a.metadata_matches:
                bucket_story[m.bucket,m.polarity].add(sk); key=(sk,m.bucket,m.polarity); data=member_data.setdefault(key,{"kinds":set(),"ambiguous":False,"numeric":False,"sources":set(),"points":0});data["kinds"].add(a.claimant_kind);data["ambiguous"]|=a.claimant_ambiguous;data["numeric"]|=a.numeric_capex_language_present;data["sources"].add(a.source_id);data["points"]=max(data["points"],0 if m.polarity is CapexEvidencePolarityV1.CONTEXT else weights[a.claimant_kind])
        if len(member_data)>policy.temporal_rows*8:return _failure(query,NewsQueryStatusV1.CAP_EXCEEDED,"semantic_member_cap",policy)
        bags=[]
        for b in CapexBucketV1:
            polarity_aggregates=[]
            for polarity in CapexEvidencePolarityV1:
                members=[]
                for (sk,bb,p),data in sorted(member_data.items(),key=lambda item:item[0][0]):
                    if bb is not b or p is not polarity:continue
                    kinds=tuple(k for k in ClaimantKindV1 if k in data["kinds"]);sources_tuple=tuple(sorted(data["sources"]));points=data["points"]
                    pre={"domain":"all-match-polarity-story-v1","classifier_digest":CLASSIFIER_TABLE_DIGEST_V1,"spec_digest":SPEC_DIGEST_V1,"bucket":b,"polarity":polarity,"story_key":sk,"claimant_kinds_present":kinds,"claimant_ambiguous":data["ambiguous"],"numeric_capex_present":data["numeric"],"source_ids":sources_tuple,"points":points}
                    members.append(AllMatchPolarityStoryV1(sk,kinds,data["ambiguous"],data["numeric"],sources_tuple,points,digest_v1(pre)))
                members=tuple(members);story_digest=digest_v1(tuple(m.story_key for m in members));sources_all=tuple(sorted({s for m in members for s in m.source_ids}));source_digest=digest_v1(sources_all);claim_digest=digest_v1(tuple((m.story_key,m.claimant_kinds_present,m.claimant_ambiguous) for m in members));numeric_digest=digest_v1(tuple((m.story_key,m.numeric_capex_present) for m in members));membership=digest_v1({"member_semantic_digests":tuple(m.member_semantic_digest for m in members),"story_key_membership_digest":story_digest,"source_membership_digest":source_digest,"claimant_membership_digest":claim_digest,"numeric_membership_digest":numeric_digest})
                values={"bucket":b,"polarity":polarity,"members":members,"distinct_story_count":len(members),"uncapped_points":sum(m.points for m in members),"claimant_story_counts":tuple(sum(k in m.claimant_kinds_present for m in members) for k in ClaimantKindV1),"claimant_ambiguous_story_count":sum(m.claimant_ambiguous for m in members),"numeric_capex_story_count":sum(m.numeric_capex_present for m in members),"distinct_source_count":len(sources_all),"story_key_membership_digest":story_digest,"source_membership_digest":source_digest,"claimant_membership_digest":claim_digest,"numeric_membership_digest":numeric_digest,"membership_digest":membership}
                polarity_aggregates.append(AllMatchPolarityAggregateV1(**values,aggregate_digest=digest_v1(values)))
            pols=tuple(polarity_aggregates);support,counter,context=pols;allkeys={m.story_key for p in pols for m in p.members};claim_counts=tuple(sum(any(k in m.claimant_kinds_present for p in pols for m in p.members if m.story_key==sk) for sk in allkeys) for k in ClaimantKindV1);ambiguity=sum(any(m.claimant_ambiguous for p in pols for m in p.members if m.story_key==sk) for sk in allkeys);numeric=sum(any(m.numeric_capex_present for p in pols for m in p.members if m.story_key==sk) for sk in allkeys);source_count=len({s for p in pols for m in p.members for s in m.source_ids})
            values={"bucket":b,"polarity_aggregates":pols,"support_distinct_story_count":support.distinct_story_count,"counter_distinct_story_count":counter.distinct_story_count,"context_distinct_story_count":context.distinct_story_count,"support_uncapped_points":support.uncapped_points,"counter_uncapped_points":counter.uncapped_points,"support_points":min(6,support.uncapped_points),"counter_points":min(6,counter.uncapped_points),"net_points":min(6,support.uncapped_points)-min(6,counter.uncapped_points),"conflict":bool(support.members and counter.members),"claimant_story_counts":claim_counts,"claimant_ambiguous_story_count":ambiguity,"numeric_capex_story_count":numeric,"distinct_source_count":source_count}
            bags.append(AllMatchBucketAggregateV1(**values,aggregate_digest=digest_v1(values)))
        allstories={story(a) for a in articles}; destination=set().union(*(bucket_story[b,p] for b in list(CapexBucketV1)[:3] for p in CapexEvidencePolarityV1)); hyper=set().union(*(bucket_story[CapexBucketV1.HYPERSCALER_CAPEX_CONTEXT,p] for p in CapexEvidencePolarityV1))
        semantic_fields={"bucket_aggregates":tuple(bags),"claimant_story_counts":tuple(len(claimant_stories[k]) for k in ClaimantKindV1),"claimant_ambiguous_story_count":len(ambiguous_stories),"numeric_capex_story_count":len(numeric_stories),"hyperscaler_context_story_count":len(hyper),"destination_story_count":len(destination),"matched_distinct_source_count":len({a.source_id for a in articles})}
        semantics=AllMatchSemanticAggregateV1(**semantic_fields,all_match_semantic_digest=digest_v1(semantic_fields))
        returned=[]; used=0
        for a in articles[:policy.matched_output_rows]:
            size=len(canonical_json_bytes(a))
            if used+size>policy.matched_output_bytes:break
            returned.append(a);used+=size
        omitted=articles[len(returned):]; omitted_bytes=sum(len(canonical_json_bytes(a)) for a in omitted)
        effects=[(a.published_at or a.first_seen_at) for a in articles]; sources={a.source_id for a in articles}
        coverage=LocalNewsCoverageV1(query.published_from,query.as_of,database_rows,min(effects) if effects else None,max(effects) if effects else None,max((a.available_at for a in articles),default=None),len(temporal),norm_bytes,future_pub,future_avail,len(articles),len(allstories),len(returned),len({story(a) for a in returned}),len(sources),sum(a.summary_excerpt_digest is None for a in articles),sum(a.revision_no>1 for a in articles),sum(1 for s in allstories if sum(story(a)==s for a in articles)>1),len(omitted),omitted_bytes,"LATEST_REVISION_NOT_HISTORICALLY_RECONSTRUCTABLE" if any(a.revision_no>1 for a in articles) else None)
        qd=_query_digest(query,policy); snapshot=digest_v1({"schema":meta,"data_version":data_version,"database_rows":database_rows,"scanned_row_digests":sorted(scanned_row_digests),"temporal_candidate_digests":sorted(digest_v1((row[0],row[8],td,sd,nbytes)) for row,_,_,_,_,_,_,_,td,sd,nbytes in temporal),"classified_rows":sorted(a.article_classification_digest for a in articles),"semantic":semantics.all_match_semantic_digest,"future":[future_pub,future_avail],"caps":policy})
        result_fields={"status":NewsQueryStatusV1.OK,"query_digest":qd,"source_snapshot_digest":snapshot,"articles":tuple(returned),"coverage":coverage,"all_match_semantics":semantics,"error_code":None}
        return LocalNewsQueryResultV1(**result_fields,result_digest=digest_v1(result_fields))
    except sqlite3.OperationalError as error:
        code=str(error).lower(); status=NewsQueryStatusV1.BUSY_OR_LOCKED if "locked" in code or "busy" in code else NewsQueryStatusV1.CORRUPT
        return _failure(query,status,"sqlite_read_failed",policy)
    except sqlite3.DatabaseError:return _failure(query,NewsQueryStatusV1.CORRUPT,"sqlite_corrupt",policy)
    finally:
        if connection is not None:
            try:
                if connection.in_transaction:connection.execute("ROLLBACK")
            finally:connection.close()


from dataclasses import dataclass


@dataclass(frozen=True)
class DiscoveryDocumentsV1:
    """Typed union for the discovery reader (SDS §6.4): the producer copies
    ``status`` verbatim into ``discovery_runs.status`` — never a string
    match outside this module."""
    status: str  # OK|STORE_UNAVAILABLE|SCHEMA_MISMATCH|CAP_EXCEEDED|CORRUPT|BUSY_OR_LOCKED
    documents: tuple
    query_only: int
    scanned: int


def _discovery_failure_status(error) -> str:
    message=str(error).casefold()
    if "unable to open" in message: return "STORE_UNAVAILABLE"
    if "locked" in message or "busy" in message: return "BUSY_OR_LOCKED"
    if "no such table" in message or "no such column" in message: return "SCHEMA_MISMATCH"
    return "CORRUPT"


def read_discovery_documents_v1(path: Path, *, window_from: datetime,
                                max_rows: int, write_probe=False):
    """Small n5 metadata seam with the same ro URI/query_only ownership.

    Bounded and typed: never raises on store degradation — every failure is
    one of the six ``discovery_runs.status`` tokens. The write probe stays a
    propagating ownership assertion for the architecture regression."""
    absolute=Path(path).resolve()
    try:
        connection=sqlite3.connect(f"file:{quote(str(absolute),safe='/')}?mode=ro",uri=True,timeout=0.25)
    except (sqlite3.Error,OSError) as error:
        return DiscoveryDocumentsV1(_discovery_failure_status(error),(),0,0)
    try:
        try:
            connection.execute("PRAGMA query_only=ON")
            connection.execute("PRAGMA busy_timeout=250")
            query_only=connection.execute("PRAGMA query_only").fetchone()[0]
            if query_only != 1: raise sqlite3.OperationalError("query_only unavailable")
        except sqlite3.OperationalError as error:
            return DiscoveryDocumentsV1(_discovery_failure_status(error),(),0,0)
        except sqlite3.DatabaseError:
            return DiscoveryDocumentsV1("CORRUPT",(),0,0)
        if write_probe: connection.execute("PRAGMA user_version=1")
        try:
            rows=connection.execute("""
              SELECT news_document_id,title,COALESCE(content_hash,title_fingerprint),source_id,first_seen_at,summary_text,evidence_role,publisher_name
              FROM news_documents AS n
              WHERE COALESCE(published_at,first_seen_at)>=? AND revision_no=(SELECT max(revision_no) FROM news_documents WHERE news_document_id=n.news_document_id)
              ORDER BY news_document_id
            """,(window_from.isoformat(),)).fetchmany(max_rows+1)
        except sqlite3.OperationalError as error:
            return DiscoveryDocumentsV1(_discovery_failure_status(error),(),query_only,0)
        except sqlite3.DatabaseError:
            return DiscoveryDocumentsV1("CORRUPT",(),query_only,0)
        if len(rows)>max_rows:
            return DiscoveryDocumentsV1("CAP_EXCEEDED",(),query_only,len(rows))
        from tokmo.values.news_discovery import NewsDocument
        return DiscoveryDocumentsV1(
            "OK",tuple(NewsDocument(*row) for row in rows),query_only,len(rows))
    finally:connection.close()
