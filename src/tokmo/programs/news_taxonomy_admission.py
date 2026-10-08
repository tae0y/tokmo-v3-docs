"""Deterministic NEWS taxonomy auto-admission (stages 1–6, 2026-08-22).

Pure extraction over the existing discovery document window: entity
aliases, bounded topics, metrics, controlled-vocabulary catalysts, and
observable effects become typed registrations without any model call.
Ambiguous evidence lands in the durable PENDING_MODEL queue; only the
explicit ``drain_taxonomy_pending`` seam (stage 7, injected adjudicator)
ever resolves it — there is no automatic fallback.

Scope sources only (no full-market preload): holdings/interest terms, the
identity catalog (public + verified aliases, official local identity
pairs), and repeated local NEWS observations. Alias discovery may read a
document's already-stored non-title evidence (``NewsDocument.summary``);
nothing is ever fetched to widen it.

All term/marker matching is token/boundary-safe: latin and numeric term
tokens match whole text tokens only (``us`` never matches inside
``bonus``), and Korean term tokens attach at a token head only
(``한화`` matches ``한화에어로`` but never inside ``대한화학``).
"""
import re

from tokmo.programs.news_topic_discovery import (
    _HANGUL_ONLY,
    _contains_term,
    _eligible_token,
    _normalise,
    _tokens,
)
from tokmo.values.news_config import FREQ_MIN_SOURCES, FREQ_MIN_STORIES
from tokmo.values.news_taxonomy import (
    CATALYST_TYPES_V1,
    GENERIC_EVENT_MARKERS_V1,
    OBSERVED_EFFECT_MARKERS_V1,
    PREDICTIVE_MARKERS_V1,
    TaxonomyAdmissionResultV1,
    TaxonomyObservationV1,
    build_adjudication_request,
)

# Alias↔canonical pair punctuation: parenthetical or bracketed co-mention
# inside one stored field, e.g. ``엔비디아(NVDA)`` or ``SK하이닉스[000660]``.
_PAREN = re.compile(r"([0-9A-Za-z가-힣.&\-]{2,40})\s*[(\[]([^()\[\]]{1,40})[)\]]")
_VALUE_UNIT = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(%|퍼센트|bp|조\s?원|조|억\s?원|억|만\s?원|달러|원"
    r"|billion|million|bn|유로|엔|배)")
_PERIOD = re.compile(
    r"([1-4]분기|q[1-4]|상반기|하반기|\d{4}년|연간|yoy|qoq|전년|전월)")
_MAGNITUDE = re.compile(r"(\d+(?:[.,]\d+)?)\s*%")

# Typed interest lanes (M3): entity terms feed the global scope; the other
# kinds scope only their own extraction lane, never entity alias identity.
_TYPED_INTEREST_KINDS = ("ENTITY", "TOPIC", "METRIC", "CATALYST", "EFFECT")


def _substantive_tokens(text):
    return [token for token in _tokens(text)
            if _eligible_token(token) and not token.isdigit()]


def _story_evidence(members):
    documents = sorted({doc.document_id for doc in members})
    stories = sorted({doc.story_fingerprint for doc in members})
    sources = sorted({doc.source_id for doc in members})
    return {"document_ids": documents, "stories": stories,
            "sources": sources}


def _subject(tokens, catalog, scope_terms):
    """Deterministic scoped subject: catalog entity first, then scope term."""
    entities = sorted(
        key for key, aliases in catalog.items()
        if any(alias and _contains_term(tokens, alias) for alias in aliases))
    if entities:
        return entities[0]
    scoped = sorted(term for term in scope_terms
                    if term and _contains_term(tokens, term))
    return scoped[0] if scoped else None


def _alias_candidate_ok(alias, known_aliases):
    if alias in known_aliases or len(alias) < 2:
        return False
    tokens = _tokens(alias)
    if not tokens:
        return False
    # A pure short number is never an alias; official numeric codes
    # (e.g. 005930) are at least four digits long.
    if all(token.isdigit() for token in tokens):
        return len(alias) >= 4
    return bool(_substantive_tokens(alias))


def _segment_starts(alias):
    """Name-segment start offsets: string start, after a separator, and at a
    script boundary (latin/digit ↔ hangul)."""
    starts = [0]
    for index in range(1, len(alias)):
        previous, char = alias[index - 1], alias[index]
        if not previous.isalnum():
            starts.append(index)
            continue
        if bool(_HANGUL_ONLY.fullmatch(char)) \
                != bool(_HANGUL_ONLY.fullmatch(previous)):
            starts.append(index)
    return tuple(index for index in starts
                 if index < len(alias) and alias[index].isalnum())


def _abbreviates(candidate, alias):
    """Deterministic contraction test: the candidate is an in-order
    character subsequence of the alias beginning at a name-segment start
    (``하닉``/``sk하닉`` ⊂ ``sk하이닉스``, ``삼전`` ⊂ ``삼성전자``) — never a
    mid-word fragment and never the alias itself."""
    if len(candidate) < 2 or len(candidate) >= len(alias):
        return False
    for start in _segment_starts(alias):
        if alias[start] != candidate[0]:
            continue
        position = start
        for char in candidate:
            found = alias.find(char, position)
            if found == -1:
                break
            position = found + 1
        else:
            return True
    return False


def _merged_alias_maps(alias_to_entity, official_identities):
    """Catalog + official local identities as one known-alias view."""
    known = {alias: set(entities)
             for alias, entities in alias_to_entity.items()}
    for canonical, aliases in dict(official_identities or {}).items():
        for alias in aliases:
            known.setdefault(_normalise(str(alias)), set()).add(canonical)
    by_entity = {}
    for alias, entities in known.items():
        for entity in entities:
            by_entity.setdefault(entity, []).append(alias)
    return known, {entity: tuple(sorted(aliases))
                   for entity, aliases in by_entity.items()}


def _alias_observations(documents, alias_to_entity, official_identities):
    sightings = {}

    def record(alias, canonical, doc, display):
        entry = sightings.setdefault(
            alias, {"display": display, "by_canonical": {}})
        entry["by_canonical"].setdefault(canonical, []).append(doc)

    for canonical, aliases in sorted((official_identities or {}).items()):
        for alias in aliases:
            normalized = _normalise(str(alias))
            known = alias_to_entity.get(normalized)
            if known == {canonical}:
                continue
            if known and known != {canonical}:
                # Local official identity disagrees with the catalog: this
                # alias is ambiguous evidence, not an overwrite.
                for other in sorted(known):
                    record(normalized, other, None, str(alias))
            record(normalized, canonical, None, str(alias))
    known_aliases, aliases_by_entity = _merged_alias_maps(
        alias_to_entity, official_identities)
    for doc in documents:
        summary = str(getattr(doc, "summary", None) or "")
        recorded = set()

        def observe(candidate, canonical, display, doc=doc,
                    recorded=recorded):
            if (candidate, canonical) in recorded:
                return
            recorded.add((candidate, canonical))
            record(candidate, canonical, doc, display)

        # Pair punctuation in any already-stored field (title or summary).
        for text in (doc.title, summary):
            for left, inside in _PAREN.findall(text):
                for known_raw, candidate_raw in ((left, inside),
                                                 (inside, left)):
                    known = _normalise(known_raw)
                    candidate = _normalise(candidate_raw)
                    entities = known_aliases.get(known)
                    if entities is None or len(entities) != 1:
                        continue
                    if not _alias_candidate_ok(candidate, known_aliases):
                        continue
                    observe(candidate, next(iter(entities)), candidate_raw)
                    break
        # Title contraction + canonical mention in stored non-title
        # evidence: the core local-corpus shape (title ``하닉 …``, summary
        # ``SK하이닉스[000660] …``). Only contraction-shaped title tokens
        # qualify — arbitrary leftover title words never become aliases.
        if not summary:
            continue
        summary_tokens = _tokens(summary)
        mentioned = sorted(
            entity for entity, aliases in aliases_by_entity.items()
            if any(_contains_term(summary_tokens, alias)
                   for alias in aliases))
        if not mentioned:
            continue
        title_tokens = _tokens(doc.title)
        candidates = list(title_tokens) + [
            left + right for left, right
            in zip(title_tokens, title_tokens[1:])]
        for candidate in candidates:
            if not _alias_candidate_ok(candidate, known_aliases):
                continue
            for canonical in mentioned:
                if any(_abbreviates(candidate, alias)
                       for alias in aliases_by_entity[canonical]):
                    observe(candidate, canonical, candidate)
    observations = []
    for alias in sorted(sightings):
        entry = sightings[alias]
        by_canonical = entry["by_canonical"]
        evidence = {
            canonical: _story_evidence([doc for doc in docs if doc])
            for canonical, docs in sorted(by_canonical.items())}
        if len(by_canonical) > 1:
            observations.append(TaxonomyObservationV1(
                "ENTITY_ALIAS", alias, None, "PENDING_MODEL",
                "ALIAS_COLLISION", entry["display"],
                {"by_canonical": evidence}))
            continue
        ((canonical, docs),) = by_canonical.items()
        official = any(doc is None for doc in docs)
        stories = {doc.story_fingerprint for doc in docs if doc}
        if official:
            state, reason = "VERIFIED_AUTO", "OFFICIAL_LOCAL_IDENTITY"
        elif len(stories) >= 2:
            state, reason = "VERIFIED_AUTO", "ALIAS_COOCCURRENCE"
        else:
            state, reason = "CANDIDATE", "ALIAS_SINGLE_SIGHTING"
        observations.append(TaxonomyObservationV1(
            "ENTITY_ALIAS", alias, canonical, state, reason,
            entry["display"], {"canonical": canonical} | evidence[canonical]))
    return observations


def _topic_observations(documents, catalog, known_aliases, scope_terms):
    phrases = {}
    for doc in documents:
        tokens = _tokens(doc.title)
        scoped = _subject(tokens, catalog, scope_terms) is not None
        for left, right in zip(tokens, tokens[1:]):
            if not (_eligible_token(left) and _eligible_token(right)):
                continue
            if left in known_aliases or right in known_aliases:
                continue  # entity identity is not a topic
            entry = phrases.setdefault(f"{left} {right}",
                                       {"docs": [], "scoped": False})
            entry["docs"].append(doc)
            entry["scoped"] = entry["scoped"] or scoped
    observations = []
    for phrase in sorted(phrases):
        entry = phrases[phrase]
        stories = {doc.story_fingerprint for doc in entry["docs"]}
        sources = {doc.source_id for doc in entry["docs"]}
        if len(stories) < FREQ_MIN_STORIES or len(sources) < FREQ_MIN_SOURCES:
            continue
        state, reason = (("VERIFIED_AUTO", "TOPIC_SCOPED_REPEATED")
                         if entry["scoped"] else
                         ("PENDING_MODEL", "TOPIC_UNSCOPED_REPEATED"))
        observations.append(TaxonomyObservationV1(
            "TOPIC", phrase, None, state, reason, phrase,
            _story_evidence(entry["docs"])))
    return observations


def _metric_observations(documents, catalog, scope_terms):
    observations = []
    for doc in documents:
        text = _normalise(doc.title)
        matches = _VALUE_UNIT.findall(text)
        if not matches:
            continue
        tokens = _tokens(text)
        subject = _subject(tokens, catalog, scope_terms)
        if subject is None and not _substantive_tokens(text):
            continue
        period = _PERIOD.search(text)
        period_text = period.group(1) if period else ""
        for value, unit in matches:
            unit = " ".join(unit.split())
            evidence = {"subject": subject, "value": value, "unit": unit,
                        "period": period_text,
                        "document_id": doc.document_id,
                        "story": doc.story_fingerprint,
                        "source": doc.source_id,
                        "observed_at": doc.observed_at}
            state, reason = (("VERIFIED_AUTO", "METRIC_COMPLETE")
                             if subject is not None else
                             ("PENDING_MODEL", "METRIC_SUBJECT_UNRESOLVED"))
            observations.append(TaxonomyObservationV1(
                "METRIC",
                f"{subject or ''}|{value}|{unit}|{period_text}"
                f"|{doc.story_fingerprint}",
                subject, state, reason, doc.title, evidence))
    return observations


def _catalyst_observations(documents, catalog, scope_terms):
    observations = []
    for doc in documents:
        tokens = _tokens(doc.title)
        types = sorted(
            ctype for ctype, markers in CATALYST_TYPES_V1.items()
            if any(_contains_term(tokens, marker) for marker in markers))
        generic = any(_contains_term(tokens, marker)
                      for marker in GENERIC_EVENT_MARKERS_V1)
        if not types and not generic:
            continue
        subject = _subject(tokens, catalog, scope_terms)
        if subject is None:
            continue
        evidence = {"subject": subject, "types": types,
                    "document_id": doc.document_id,
                    "story": doc.story_fingerprint, "source": doc.source_id}
        if len(types) == 1:
            state, reason, canonical = "VERIFIED_AUTO", "CATALYST_TYPED", \
                types[0]
        elif types:
            state, reason, canonical = "PENDING_MODEL", \
                "CATALYST_AMBIGUOUS", None
        else:
            state, reason, canonical = "PENDING_MODEL", \
                "CATALYST_UNKNOWN_TYPE", None
        observations.append(TaxonomyObservationV1(
            "CATALYST",
            f"{subject}|{'+'.join(types) or 'UNKNOWN'}"
            f"|{doc.story_fingerprint}",
            canonical, state, reason, doc.title, evidence))
    return observations


def _effect_observations(documents, catalog, scope_terms):
    observations = []
    for doc in documents:
        text = _normalise(doc.title)
        tokens = _tokens(text)
        predicted = any(_contains_term(tokens, marker)
                        for marker in PREDICTIVE_MARKERS_V1)
        directions = sorted({
            direction for marker, direction
            in OBSERVED_EFFECT_MARKERS_V1.items()
            if _contains_term(tokens, marker)})
        if not predicted and not directions:
            continue
        subject = _subject(tokens, catalog, scope_terms)
        if subject is None:
            continue
        magnitude = _MAGNITUDE.search(text)
        evidence = {"subject": subject, "directions": directions,
                    "predicted": predicted,
                    "magnitude_pct": magnitude.group(1) if magnitude else None,
                    "document_id": doc.document_id,
                    "story": doc.story_fingerprint, "source": doc.source_id}
        if predicted:
            state, reason, tag = "PENDING_MODEL", \
                "EFFECT_PREDICTED_UNRESOLVED", "PREDICTED"
        elif len(directions) == 1:
            state, reason, tag = "VERIFIED_AUTO", "EFFECT_OBSERVED", \
                directions[0]
        else:
            state, reason, tag = "PENDING_MODEL", "EFFECT_AMBIGUOUS", \
                "AMBIGUOUS"
        observations.append(TaxonomyObservationV1(
            "EFFECT", f"{subject}|{tag}|{doc.story_fingerprint}",
            subject, state, reason, doc.title, evidence))
    return observations


def run_taxonomy_admission(*, store, documents, now, holdings=frozenset(),
                           interest_terms=frozenset(), typed_interests=None,
                           identity_catalog=None, official_identities=None):
    documents = tuple(documents)
    typed = {}
    for kind, terms in dict(typed_interests or {}).items():
        if kind in _TYPED_INTEREST_KINDS:
            typed[kind] = tuple(
                _normalise(str(term)) for term in terms if str(term).strip())
    catalog = {key: tuple(_normalise(str(alias)) for alias in aliases)
               for key, aliases in dict(identity_catalog or {}).items()}
    alias_to_entity = {}
    for key, aliases in catalog.items():
        for alias in aliases:
            alias_to_entity.setdefault(alias, set()).add(key)
    # Official local identity pairs are identities too: subject scoping and
    # the observed-evidence alias channels see the merged catalog, while
    # the official-disagreement check above keeps the catalog-only view.
    merged_catalog = dict(catalog)
    for key, aliases in dict(official_identities or {}).items():
        normalized = tuple(_normalise(str(alias)) for alias in aliases)
        merged_catalog[key] = tuple(dict.fromkeys(
            merged_catalog.get(key, ()) + normalized))
    known_aliases, _ = _merged_alias_maps(alias_to_entity,
                                          official_identities)
    scope_terms = {_normalise(str(term))
                   for term in (*holdings, *interest_terms,
                                *typed.get("ENTITY", ()))
                   if str(term).strip()}

    def lane_scope(kind):
        return scope_terms | set(typed.get(kind, ()))

    observations = (
        _alias_observations(documents, alias_to_entity, official_identities)
        + _topic_observations(documents, merged_catalog, known_aliases,
                              lane_scope("TOPIC"))
        + _metric_observations(documents, merged_catalog,
                               lane_scope("METRIC"))
        + _catalyst_observations(documents, merged_catalog,
                                 lane_scope("CATALYST"))
        + _effect_observations(documents, merged_catalog,
                               lane_scope("EFFECT")))
    counts = {"VERIFIED_AUTO": 0, "PENDING_MODEL": 0, "CANDIDATE": 0}
    for observation in sorted(observations,
                              key=lambda o: (o.kind, o.unit_key)):
        store.record_taxonomy_observation({
            "kind": observation.kind, "unit_key": observation.unit_key,
            "canonical_key": observation.canonical_key,
            "proposed_state": observation.proposed_state,
            "reason": observation.reason,
            "display_text": observation.display_text,
            "evidence": observation.evidence}, now=now)
        counts[observation.proposed_state] += 1
    return TaxonomyAdmissionResultV1(
        observations=len(observations),
        admitted_auto=counts["VERIFIED_AUTO"],
        pending=counts["PENDING_MODEL"],
        candidates=counts["CANDIDATE"])


def drain_taxonomy_pending(store, adjudicator, *, now, limit=8):
    """Stage-7 seam: resolve pending units through one explicitly injected
    adjudicator callable. Nothing in stages 1–6 wires this; a missing or
    malformed verdict fails closed with the unit left pending."""
    resolved = 0
    for row in store.list_taxonomy_pending(limit=limit):
        verdict = adjudicator(build_adjudication_request(row))
        if not isinstance(verdict, dict) \
                or verdict.get("verdict") not in ("VERIFIED", "REJECTED"):
            raise ValueError("adjudicator returned an invalid verdict")
        store.resolve_taxonomy_pending(
            row["unit_id"], verdict["verdict"],
            reason=str(verdict.get("reason") or "adjudicated"),
            canonical_key=verdict.get("canonical_key"), now=now)
        resolved += 1
    return resolved
