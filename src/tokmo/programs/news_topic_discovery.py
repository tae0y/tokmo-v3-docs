"""Deterministic whole-corpus discovery over public NEWS metadata."""
from collections import defaultdict
import hashlib
import json
import logging
import re
import unicodedata
from pathlib import Path

from tokmo.values.news_config import CANDIDATE_CAP_PER_RUN, FREQ_MIN_SOURCES, FREQ_MIN_STORIES
from tokmo.values.news_discovery import (
    DiscoveryCandidate, DiscoveryDecision, DiscoveryResult,
    NewsDiscoveryLaneV1, NewsDiscoveryPolicyV1,
    load_public_aliases,
)
from tokmo.values.news_instrument_match import (
    COMPANY_LEGAL_SUFFIXES_V1,
    ETF_GENERIC_TOKENS_V1,
    is_etf_name,
    match_etf_reference,
    normalize_company_name,
)
from tokmo.values.news_publisher_origins import count_independent_origins

_LOG = logging.getLogger(__name__)
_TOKEN = re.compile(r"[0-9a-z]+(?:[-.][0-9a-z]+)*|[가-힣]{2,}")
_PARTICLES = ("으로부터", "에서는", "에게서", "으로", "에서", "에게", "까지", "부터", "처럼", "보다", "은", "는", "이", "가", "을", "를", "의", "에", "와", "과", "도", "로")
_STOP = frozenset({
    "속보", "뉴스", "관련", "대한", "기자", "소식", "업데이트", "결과",
    "체결", "급증",
    # G2 actual-miss corpus 2026-08-21: analyst-rating vocabulary and
    # English function words consumed 11/12 surfaced slots while public
    # entities were cap-truncated.  Keep material nouns (실적/매출/성장/etc.).
    "주식", "상향", "목표주", "등급", "매수", "종합", "조정", "하향",
    "목표", "전망", "지원", "유지",
    "to", "in", "the", "on", "as", "says", "and", "is", "of", "for",
    "are", "ai", "could", "more", "than", "this", "that", "us", "stocks",
    "rise", "plus", "p500", "글로벌",
})
# Counting-unit tokens (SDS 2026-09-01 W12/J-6): quantity units that survive
# digit splitting ("80만명" → "80" + "만명") are measurement fragments, never
# topics. Kept separate from _STOP so substantive-statement judgment on
# stored headlines is unchanged.
_COUNTING_UNITS = frozenset({"만명", "만대", "만원", "억원", "조원", "퍼센트"})
_PUBLIC_ALIASES,_SUPPRESSED=load_public_aliases(Path(__file__).parents[1]/"values"/"news_discovery_aliases.json")


def _normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())

_PUBLIC_ALIASES={key:tuple(_normalise(alias) for alias in aliases) for key,aliases in _PUBLIC_ALIASES.items()}
_SUPPRESSED=tuple(_normalise(item) for item in _SUPPRESSED)


def _strip_particle(token: str) -> str:
    for suffix in _PARTICLES:
        if token.endswith(suffix) and len(token) > len(suffix) + 1:
            return token[:-len(suffix)]
    return token


def _tokens(text: str) -> list[str]:
    return [_strip_particle(token) for token in _TOKEN.findall(_normalise(text))]


def _eligible_token(token: str) -> bool:
    return token not in _STOP and token not in _COUNTING_UNITS \
        and not token.isdigit() and len(token) >= 2


def candidate_requires_classifier(key: str, term: str, *, lane: str = "") -> bool:
    """Fail closed for legacy/open candidates that current discovery would
    no longer surface. Canonical entity aliases always remain eligible."""
    if str(key).startswith("ent:") or lane == "DIRECT_HOLDING":
        return True
    return substantive_statement(term)


def substantive_statement(text) -> bool:
    """True when the text carries at least one non-generic content token."""
    tokens = _tokens(str(text or ""))
    return any(not token.isdigit() and len(token) >= 2
               and token not in _STOP for token in tokens)


def _terms(text: str) -> set[str]:
    tokens = _tokens(text)
    unigrams = {token for token in tokens if _eligible_token(token)}
    adjacent_bigrams = {
        f"{left} {right}" for left, right in zip(tokens, tokens[1:])
        if _eligible_token(left) and _eligible_token(right)
    }
    return unigrams | adjacent_bigrams


_HANGUL_ONLY = re.compile(r"[가-힣]+")


def _token_matches(text_token, term_token):
    """Boundary-safe single-token match (shared with taxonomy admission):
    latin/numeric term tokens match whole text tokens only (``us`` never
    matches inside ``bonus``); Korean term tokens attach at a token head
    only (``하닉`` matches ``하닉스`` but never inside ``지하닉스``)."""
    if text_token == term_token:
        return True
    return (_HANGUL_ONLY.fullmatch(term_token) is not None
            and text_token.startswith(term_token))


def _contains_exact_sequence(tokens, term):
    """Full token-sequence equality at a boundary span (§V7): unlike
    ``_contains_term`` there is NO Korean head-attach — the whole
    normalized legal name must appear verbatim; independently observed
    partial tokens can never reconstruct a company identity."""
    term_tokens = _tokens(term)
    if not term_tokens:
        return False
    span = len(term_tokens)
    return any(tokens[i:i + span] == term_tokens
               for i in range(len(tokens) - span + 1))


def _contains_term(tokens, term):
    """True when the term's token sequence occurs on token boundaries."""
    term_tokens = _tokens(term)
    if not term_tokens:
        return False
    span = len(term_tokens)
    return any(
        all(_token_matches(tokens[i + j], term_tokens[j])
            for j in range(span))
        for i in range(len(tokens) - span + 1))


_ANCHOR_EXCLUDED_TOKENS = frozenset(
    token for suffix in COMPANY_LEGAL_SUFFIXES_V1
    for token in _TOKEN.findall(suffix.casefold())) | ETF_GENERIC_TOKENS_V1


def _has_shared_anchor(term, members, alias_map, *, min_stories,
                       min_origins):
    """D-6 (SDS A1 §S + A3 §V9): a single-token frequency candidate needs a
    SHARED second anchor — a registered entity/alias match or an adjacent
    bigram of eligible tokens (never the frequency term itself, stopwords,
    counting units, ETF generics, or legal-suffix fragments) — present in
    ≥``min_stories`` member stories AND spanning ≥``min_origins``
    independent origins (one syndicated origin is one unit)."""
    anchors: dict = defaultdict(dict)
    for member in members:
        tokens = _tokens(member.title)
        for key, aliases in alias_map.items():
            if any(_contains_term(tokens, alias) for alias in aliases):
                anchors[("ent", key)][member.story_fingerprint] = member
        for left, right in zip(tokens, tokens[1:]):
            if term in (left, right):
                continue
            if not (_eligible_token(left) and _eligible_token(right)):
                continue
            if left in _ANCHOR_EXCLUDED_TOKENS \
                    or right in _ANCHOR_EXCLUDED_TOKENS:
                continue
            anchors[("bigram", left, right)][
                member.story_fingerprint] = member
    for stories in anchors.values():
        if len(stories) < min_stories:
            continue
        if count_independent_origins(
                (member.source_id, member.publisher_name)
                for member in stories.values()) >= min_origins:
            return True
    return False


def _key(term: str, alias_map=None) -> str:
    for key, aliases in (_PUBLIC_ALIASES if alias_map is None
                         else alias_map).items():
        if term in aliases:
            return key
    return "tok:" + hashlib.sha256(term.encode()).hexdigest()[:16]


def _lane(term: str, key: str, holdings: set[str],
          alias_map=None) -> NewsDiscoveryLaneV1:
    alias_map = _PUBLIC_ALIASES if alias_map is None else alias_map
    normal_holdings = {_normalise(item) for item in holdings}
    aliases = {_normalise(item) for item in alias_map.get(key, ())}
    # Exact identity only (SDS A1 §O, 결정 11): whole-term/whole-alias
    # equality. The former holding-subtoken clause (any term inside the
    # holding's token set) let group prefixes like ``sk`` establish
    # DIRECT_HOLDING and is removed — full-name/ETF candidates now come
    # from the dedicated §V6/§V7 scans instead.
    if term in normal_holdings or aliases & normal_holdings:
        return NewsDiscoveryLaneV1.DIRECT_HOLDING
    if re.search(r"[a-z]+-?[0-9]+", term) and key not in alias_map:
        return NewsDiscoveryLaneV1.CONCEPT_EMERGENCE
    if key.startswith("phrase:"):
        return NewsDiscoveryLaneV1.MACRO_GEOPOLITICAL
    return NewsDiscoveryLaneV1.WHOLE_CORPUS_FREQUENCY


def discover_topics(documents, *, holdings, threshold=FREQ_MIN_STORIES,
                    source_threshold=FREQ_MIN_SOURCES, cap=CANDIDATE_CAP_PER_RUN,
                    extra_aliases=None, official_names=None):
    docs = tuple(documents)
    # Verified taxonomy aliases extend the public catalog for this run:
    # auto-admitted entities surface as ent:* candidates instead of
    # anonymous tok:* keys (entity-reserved cap slots, fewer model calls).
    alias_map = dict(_PUBLIC_ALIASES)
    for key, aliases in dict(extra_aliases or {}).items():
        merged = alias_map.get(key, ()) + tuple(
            _normalise(str(alias)) for alias in aliases)
        alias_map[key] = tuple(dict.fromkeys(merged))
    occurrences: dict[str, list] = defaultdict(list)
    terms_by_key: dict[str, str] = {}
    stopword_occurrences: dict[str, list] = defaultdict(list)
    numeric_occurrences: dict[str, list] = defaultdict(list)
    suppression_occurrences: dict[str, list] = defaultdict(list)
    # Dedicated instrument scans (SDS A3 §V6/§V7): official names split
    # into the ETF universe (brand-first-token names, unique-candidate
    # resolution) and company legal names (whole-sequence equality only).
    instrument_names = dict(official_names or {})
    etf_universe = [(key, str(name)) for key, names in
                    instrument_names.items() for name in names
                    if is_etf_name(name)]
    # §O.1-2 (SOL M1): the closed legal-suffix normalization applies to
    # the OFFICIAL name before the scan sequence is built, so suffixed
    # registrations match bare headline mentions in both orientations.
    company_names = [(key, normalize_company_name(name))
                     for key, names in instrument_names.items()
                     for name in names if not is_etf_name(name)]
    company_names = [(key, name) for key, name in company_names if name]
    scan_direct_keys: set[str] = set()
    ambiguous_etf_docs: list = []
    for doc in docs:
        text = _normalise(doc.title)
        matched_suppressions = tuple(item for item in _SUPPRESSED if item in text)
        if matched_suppressions:
            for item in matched_suppressions:
                suppression_occurrences[item].append(doc)
            continue
        tokens = _tokens(text)
        for token in tokens:
            if token in _STOP:
                stopword_occurrences[token].append(doc)
            elif token.isdigit():
                numeric_occurrences[token].append(doc)
        terms = _terms(text)
        for term in terms:
            key = _key(term, alias_map)
            occurrences[key].append(doc)
            terms_by_key.setdefault(key, term)
        # Alias reuse is boundary-safe (B2 2026-08-22): the taxonomy
        # admission matching contract, never a raw substring scan.
        for key, aliases in alias_map.items():
            if any(_contains_term(tokens, alias) for alias in aliases):
                occurrences[key].append(doc)
                terms_by_key[key] = aliases[0]
        # §V7 full-legal-name scan: the whole normalized name sequence at a
        # boundary span, independent of the n-gram bound.
        for key, name in company_names:
            if _contains_exact_sequence(tokens, name):
                occurrences[key].append(doc)
                terms_by_key.setdefault(key, _normalise(name))
                scan_direct_keys.add(key)
        # §V6 ETF reference scan: brand-anchored span match, admissible
        # only when exactly one instrument satisfies it (P.4-4).
        if etf_universe:
            hits = sorted({key for key, name in etf_universe
                           if match_etf_reference(doc.title, name).matched})
            if len(hits) == 1:
                key = hits[0]
                occurrences[key].append(doc)
                terms_by_key.setdefault(key, _normalise(
                    dict(etf_universe)[key]))
                scan_direct_keys.add(key)
            elif len(hits) > 1:
                ambiguous_etf_docs.append(doc)
    candidates = []
    decisions = []
    for key, members in occurrences.items():
        term = terms_by_key[key]
        by_story = {member.story_fingerprint: member for member in members}
        # D-5 (§R.2): independence is per publisher ORIGIN, never per feed
        # — Yonhap dual feeds are one unit and unknown origins collapse
        # into one shared bucket.
        origin_n = count_independent_origins(
            (member.source_id, member.publisher_name)
            for member in by_story.values())
        lane = _lane(term, key, set(holdings), alias_map)
        if key in scan_direct_keys:
            lane = NewsDiscoveryLaneV1.DIRECT_HOLDING
        if ((len(by_story) >= threshold and origin_n >= source_threshold)
                or lane is NewsDiscoveryLaneV1.DIRECT_HOLDING):
            # D-6 (§S/§V9): a lone-token frequency candidate must share a
            # second anchor across independent origins; ent:*/phrase:/
            # bigram/DIRECT candidates are exempt by construction.
            if (key.startswith("tok:")
                    and lane is not NewsDiscoveryLaneV1.DIRECT_HOLDING
                    and len(_tokens(term)) == 1
                    and not _has_shared_anchor(
                        term, tuple(by_story.values()), alias_map,
                        min_stories=threshold,
                        min_origins=source_threshold)):
                decisions.append(DiscoveryDecision(
                    "NO_SHARED_ANCHOR", key, term, len(by_story), origin_n,
                    tuple(sorted({member.document_id
                                  for member in members}))))
                continue
            candidates.append(DiscoveryCandidate(
                key, lane.value, len(by_story),
                tuple(sorted({member.document_id for member in members})),
                origin_n, term,
            ))
        else:
            decisions.append(DiscoveryDecision(
                "BELOW_THRESHOLD", key, term, len(by_story), origin_n,
                tuple(sorted({member.document_id for member in members}))))
    candidates.sort(key=lambda item: (
        -(item.lane == NewsDiscoveryLaneV1.DIRECT_HOLDING.value),
        -item.distinct_story_count, -item.distinct_source_count, item.key,
    ))
    entity_reserve = min(cap, max(1, cap // 3)) if cap > 0 else 0
    reserved = tuple(
        item for item in candidates if item.key.startswith("ent:"))[
            :entity_reserve]
    reserved_keys = {item.key for item in reserved}
    selected = list(reserved)
    remaining = [item for item in candidates
                 if item.key not in reserved_keys]
    selected.extend(remaining[:max(0, cap-len(selected))])
    selected_keys = {item.key for item in selected}
    selected.sort(key=lambda item: (
        -(item.lane == NewsDiscoveryLaneV1.DIRECT_HOLDING.value),
        -item.distinct_story_count, -item.distinct_source_count, item.key))
    dropped_candidates = tuple(
        item for item in candidates if item.key not in selected_keys)
    dropped_keys = tuple(item.key for item in dropped_candidates)
    decisions.extend(DiscoveryDecision(
        "CAP_TRUNCATED", item.key, item.phrase_text,
        item.distinct_story_count, item.distinct_source_count,
        item.document_ids) for item in dropped_candidates)
    for term, members in sorted(stopword_occurrences.items()):
        by_story = {member.story_fingerprint: member for member in members}
        decisions.append(DiscoveryDecision(
            "STOPWORD", None, term, len(by_story),
            len({member.source_id for member in by_story.values()}),
            tuple(sorted({member.document_id for member in members}))))
    for term, members in sorted(numeric_occurrences.items()):
        by_story = {member.story_fingerprint: member for member in members}
        decisions.append(DiscoveryDecision(
            "NUMERIC_ONLY", None, term, len(by_story),
            len({member.source_id for member in by_story.values()}),
            tuple(sorted({member.document_id for member in members}))))
    for term, members in sorted(suppression_occurrences.items()):
        by_story = {member.story_fingerprint: member for member in members}
        decisions.append(DiscoveryDecision(
            "DOCUMENT_SUPPRESSED", None, term, len(by_story),
            # D-5 coherence (SOL MIN1): audit counts use independent
            # origins, exactly like every other counting point.
            count_independent_origins(
                (member.source_id, member.publisher_name)
                for member in by_story.values()),
            tuple(sorted({member.document_id for member in members}))))
    # §P.4-4 fail-closed audit: two or more instruments satisfying the ETF
    # reference means no automatic identity — recorded, never guessed.
    for doc in ambiguous_etf_docs:
        decisions.append(DiscoveryDecision(
            "ETF_MATCH_AMBIGUOUS", None, _normalise(doc.title)[:80], 1, 1,
            (doc.document_id,)))
    decisions.sort(key=lambda item: (
        item.reason, -(item.distinct_story_count),
        -(item.distinct_source_count), item.key or "", item.phrase_text or ""))
    if dropped_keys:
        dropped_digest = hashlib.sha256(
            "\0".join(dropped_keys).encode()).hexdigest()
        _LOG.warning(
            "news discovery cap: dropped_over_cap=%d dropped_keys_digest=%s",
            len(dropped_keys), dropped_digest)
    policy = NewsDiscoveryPolicyV1(threshold, source_threshold, cap)
    snapshot = hashlib.sha256(json.dumps([
        (doc.document_id, doc.story_fingerprint, doc.source_id, _normalise(doc.title)) for doc in docs
    ], sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return DiscoveryResult(
        tuple(selected), len(docs) - len({doc.story_fingerprint for doc in docs}),
        len(dropped_keys), snapshot, policy.policy_digest, dropped_keys,
        dropped_candidates, tuple(decisions),
    )


def _clip_bytes(text, limit):
    """Bounded UTF-8-safe truncation for stored evidence strings."""
    return str(text).encode("utf-8")[:limit].decode("utf-8", "ignore")


def persist_discovery_result(store,result,*,window_from,window_to,documents,now,status="OK"):
    documents=tuple(documents)
    run_id="ndr-"+hashlib.sha256(f"{result.corpus_snapshot_digest}\0{result.policy_digest}\0{window_to.isoformat()}".encode()).hexdigest()[:24]
    run=(run_id,window_from.isoformat(),window_to.isoformat(),result.corpus_snapshot_digest,
        result.policy_digest,len(documents),len({d.story_fingerprint for d in documents}),
        result.dropped_over_cap,status,now.isoformat(),now.isoformat())
    observed={doc.document_id:doc.observed_at for doc in documents}
    titles={doc.document_id:doc.title for doc in documents}
    by_id={doc.document_id:doc for doc in documents}
    candidates=[]
    for item in result.candidates:
        cid="ndc-"+hashlib.sha256(f"{run_id}\0{item.key}".encode()).hexdigest()[:24]
        entity=item.key if item.key.startswith("ent:") else None
        latest=max((observed[doc_id] for doc_id in item.document_ids
                    if observed.get(doc_id) is not None),default=item.latest_observed_at)
        # Payload v:2 (SDS 2026-09-01 W3): per-doc title/source/role/publisher
        # provenance (newest ≤3) rides next to the v:1-compatible fields, so
        # a rollback's v:1 read path stays valid on residual rows.
        members=sorted(
            (by_id[doc_id] for doc_id in item.document_ids if doc_id in by_id),
            key=lambda doc:((doc.observed_at or ""),doc.document_id),
            reverse=True)
        payload={"v":2,"document_ids":list(item.document_ids),
            "latest_observed_at":latest,
            "docs":[{"title":_clip_bytes(" ".join(str(doc.title).split()),200),
                     "source_id":doc.source_id,
                     "role":doc.evidence_role,
                     "publisher":doc.publisher_name,
                     "observed_at":doc.observed_at}
                    for doc in members[:3]]}
        # Stored representative statement for the alert body: the actual
        # title of the latest observed evidence document, never invented.
        headline_id=max(
            (doc_id for doc_id in item.document_ids if doc_id in titles),
            key=lambda doc_id:(observed.get(doc_id) or "",doc_id),
            default=None)
        if headline_id is not None and str(titles[headline_id]).strip():
            payload["headline"]=" ".join(str(titles[headline_id]).split())[:200]
        evidence=json.dumps(payload,separators=(",",":"),ensure_ascii=False)
        candidates.append((cid,run_id,item.lane,entity,item.phrase_text,item.distinct_story_count,
            item.distinct_source_count,evidence,"SURFACED",None,
            now.isoformat(),now.isoformat()))
    store.record_discovery_run(run,candidates,now=now)
    return run_id
