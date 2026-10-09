"""Bounded NEWS producer stage (SDS 2026-08-21 §6): every gated tick runs
reconcile → morning release → two-lane drain (≤2) → checkpoint-aware expiry
(≤20), and only the 30-minute due slot additionally runs discovery.

This module publishes an inert dependency bundle plus pure report helpers.
The one executable entry, ``run_news_production``, is called exclusively by
the module-private helper inside ``run_pulse_once``'s held-flock lexical
scope (SDS §6.8); nothing here imports storage drivers or transports, and
all SQL lives in the records layer.
"""

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.programs.news_event_admission import (
    AdmissionInput,
    NewsAdmissionRejected,
    admit_news_event,
    reconcile_news_outbox,
    run_news_event_admission,
)
from tokmo.programs.news_investigation import (
    classify_event_guarded,
    run_news_investigation,
)
from tokmo.programs.news_taxonomy_admission import run_taxonomy_admission
from tokmo.programs.news_topic_discovery import (
    _PUBLIC_ALIASES,
    _clip_bytes,
    candidate_requires_classifier,
    discover_topics,
    persist_discovery_result,
    substantive_statement,
)
from tokmo.programs.personalized_event_analysis import analyze_event
from tokmo.values.news_classifier import DEFAULT_NEWS_CLASSIFIER_MODEL_ID
from tokmo.values.news_config import (
    DISCOVERY_WINDOW_HOURS,
    MORNING_RELEASE_KST,
    PRODUCER_EXPIRY_PER_TICK,
)
from tokmo.values.news_digest import DIGEST_STATEMENT_PREFIX
from tokmo.values.news_event import Evidence
from tokmo.values.news_event_confirmation import (
    CONFIRMATION_LABELS_KO,
    EVENT_ACTION_FAMILIES_V1,
    EVENT_TYPES_V1,
    derive_article_action_families,
    derive_confirmation,
    derive_relevance,
)
from tokmo.values.news_publisher_origins import count_independent_origins
from tokmo.values.personalized_event import PortfolioContext

_KST = ZoneInfo("Asia/Seoul")
# Prompt-policy r2 (SDS 2026-09-01 §D.4/§H.1-1): this digest — not the
# persisted output_schema_id, which stays the v1 literal — is the version
# axis. Raising it opens a new classification-identity space; settled v1
# rows keep answering v1-identity replays only. (The SDS names this
# identity with the other version token; it is spelled r2 because the N2
# architecture seal reserves that token for the frozen legacy-path scan.)
_PROMPT_POLICY_DIGEST = hashlib.sha256(
    json.dumps(
        {"v": 3, "policy": "news-producer-event-class-r3"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
_MODEL_CONTRACT_DIGEST = hashlib.sha256(
    json.dumps(
        {"v": 1, "contract": "news-classifier-output-v1"},
        sort_keys=True,
        separators=(",", ":"),
    ).encode()
).hexdigest()
# Deterministic event-type projections (SDS 2026-09-01 §E.2): irreversible
# and systemic sets feed classify_event_guarded; the model never labels the
# event class itself.
_IRREVERSIBLE_EVENT_TYPES = frozenset(
    {
        "CORPORATE_ACTION_DECIDED",
        "DIVIDEND_CHANGE",
        "MACRO_POLICY_EMERGENCY",
        "WAR_MARKET_DISRUPTION",
    }
)
_SYSTEMIC_EVENT_TYPES = frozenset({"MACRO_POLICY_EMERGENCY", "WAR_MARKET_DISRUPTION"})


@dataclass(frozen=True)
class NewsProducerDepsV1:
    """Inert producer dependency bundle (SDS §6.8-1): stores, config, and
    supply seams only — no field executes the production pipeline."""

    episode_store: object
    ops_kernel: object
    role: object
    documents_reader: Callable
    holding_terms: Callable
    classifier_call: Callable
    classifier_config_path: Path
    admitted_catalog: Mapping
    gate_cutoff: datetime
    slot_seconds: int
    candidates_per_tick: int
    lease_seconds: int
    backoff_cap_seconds: int
    ttl_seconds: int
    attempt_max: int
    # G3 canary seam (approval packet §2.1): per-tick delivery judgment.
    # None/False keeps every admission SHADOW; the composition root supplies
    # the window/cap-aware judgment only in approved TELEGRAM_CANARY mode.
    delivery_activation: "Callable | None" = None
    # Durable async classifier handoff (live lane): when bound, the drain
    # freezes each classification request into the work queue and never
    # starts model transport inside the pulse lifecycle; the separately
    # invoked worker owns real calls. None preserves the synchronous seam.
    classifier_queue: "Callable | None" = None
    # Taxonomy scope seams (stages 1–6/M3): ``interest_terms`` supplies the
    # typed interest mapping ({"ENTITY"|"TOPIC"|"METRIC"|"CATALYST"|
    # "EFFECT": terms}; a flat iterable is treated as entity terms) and
    # ``identity_pairs`` the official local identity pairs
    # (canonical → aliases). None keeps the engine on holdings + the
    # public/verified catalogs.
    interest_terms: "Callable | None" = None
    identity_pairs: "Callable | None" = None
    # Official primary-evidence read seam (SDS 2026-09-01 §G.3): a
    # ``(stock_codes, date_from, date_to) -> OfficialEvidenceRefV1 tuple``
    # callable over the read-only OpenDART bulk store. None keeps the seam
    # fully inert — derived confirmation stays at the media states.
    official_evidence: "Callable | None" = None


@dataclass(frozen=True)
class NewsProductionResultV1:
    due: bool
    discovery_run_id: "str | None"
    discovery_status: "str | None"
    reconciled_calls: int
    released_holds: int
    claims_attempted: int
    claims_won: int
    claims_won_recovery: int
    claims_won_fresh: int
    borrowed_slots: int
    drained: int
    candidate_failures: int
    finalized: Mapping
    released_candidates: int
    expired_candidates: int
    taxonomy_admitted: int = 0
    taxonomy_pending: int = 0
    # §V10 official-lookup counters (A2/A3, decision 18): exact units —
    # logical attempts, tier-1 store opens, tier-2 transport starts,
    # pattern-matched refs, correlation-passed refs, typed failures.
    official_attempts: int = 0
    official_local_reads: int = 0
    official_http_starts: int = 0
    official_refs_returned: int = 0
    official_refs_correlated: int = 0
    official_failures: int = 0


def production_stage_report(result) -> "tuple[str, str | None]":
    """Stage status/reason derive from result fields only (SDS §6.1)."""
    if result.candidate_failures:
        return "partial", (
            f"candidate_failures:{result.candidate_failures},drained:{result.drained}"
        )
    if result.due:
        return "complete", (
            f"discovered:{result.discovery_run_id},drained:{result.drained}"
        )
    if (
        result.reconciled_calls
        or result.released_holds
        or result.drained
        or result.claims_attempted
        or result.released_candidates
        or result.expired_candidates
    ):
        return "complete", (
            f"drained:{result.drained},released:{result.released_holds}"
        )
    return "skipped", "not_due"


def _derived_episode_id(term):
    # Mirrors NewsEpisodeStore.upsert key normalisation for the read-only
    # muted pre-check; producer cause keys always equal the episode key.
    key = (
        term
        if len(term) == 64 and all(c in "0123456789abcdef" for c in term)
        else hashlib.sha256(term.encode()).hexdigest()
    )
    return "nep-" + hashlib.sha256(key.encode()).hexdigest()[:24]


def _candidate_documents(row):
    """(document_ids, latest_observed_at, headline, docs) from the stored
    candidate payload. ``docs`` is the v:2 per-doc provenance (title/source/
    role/publisher/observed_at, ≤3); v:1 and legacy list payloads read as
    empty provenance so their drains stay valid (J-13)."""
    try:
        payload = json.loads(row["evidence_story_keys_json"])
    except ValueError:
        payload = None
    if isinstance(payload, dict) and payload.get("v") in (1, 2):
        docs = (
            tuple(
                entry
                for entry in (payload.get("docs") or ())
                if isinstance(entry, dict)
            )
            if payload.get("v") == 2
            else ()
        )
        return (
            tuple(payload.get("document_ids") or ()),
            payload.get("latest_observed_at"),
            payload.get("headline"),
            docs,
        )
    if isinstance(payload, list):
        return tuple(payload), None, None, ()
    return (), None, None, ()


def display_aliases(entity_key, alias_map, identity_pairs):
    """Human display name + materiality comparison alias set for an ent:*
    candidate (SDS 2026-09-01 §F.3): display = first merged-catalog alias
    (canonical spelling), comparison = full catalog aliases ∪ official
    identity-pair aliases. A key known nowhere falls back to its bare name
    so phrase/token candidates keep their current behavior."""
    aliases = tuple(
        dict.fromkeys(
            tuple(alias_map.get(entity_key, ()))
            + tuple(identity_pairs.get(entity_key, ()))
        )
    )
    if not aliases:
        name = str(entity_key).split(":", 1)[-1].replace("_", " ").strip()
        return name, (name,)
    return str(aliases[0]), aliases


def _classifier_evidence_r3(
    *, display, lane, direct_holding, stories, sources, fact_n, opinion_n, evidence_docs
):
    """Bounded flat evidence contract r3 (SDS §D.4 + A3 §V1): values come
    only from already-persisted data; official evidence is deterministic
    post-classifier input and never enters the request/digest; canonical
    JSON stays ≤ 8 KiB by construction."""
    evidence = {
        "candidate": _clip_bytes(display, 80),
        "lane": str(lane),
        "holdings_relation": "DIRECT" if direct_holding else "NONE",
        "stories": str(int(stories)),
        "sources": str(int(sources)),
        "fact_sources": str(int(fact_n)),
        "opinion_sources": str(int(opinion_n)),
    }
    for index, doc in enumerate(evidence_docs[:3], start=1):
        evidence[f"headline_{index}"] = _clip_bytes(
            " ".join(str(doc.get("title") or "").split()), 200
        )
        evidence[f"headline_{index}_src"] = _clip_bytes(
            "|".join(
                str(doc.get(field) or "")
                for field in ("source_id", "role", "publisher", "observed_at")
            ),
            160,
        )
    return evidence


def _next_morning_release(now):
    local = now.astimezone(_KST)
    release = local.replace(hour=MORNING_RELEASE_KST, minute=0, second=0, microsecond=0)
    if local.hour >= MORNING_RELEASE_KST:
        release += timedelta(days=1)
    return release


_RELEVANCE_LABELS_KO = {
    "INDIRECT_MATERIAL": "관련: 간접 연관(모델 인용)",
    "THEMATIC_MATERIAL": "관련: 테마 연관(모델 인용)",
}


def render_news_alert_body(
    *,
    term,
    event_class,
    materiality,
    lane,
    stories,
    sources,
    headline=None,
    confirmation_state=None,
    relevance=None,
):
    """Render a user-facing alert; internal candidate JSON never crosses the
    Telegram boundary.

    Morning-alert repair (2026-08-22): the body's core is a meaningful
    event statement taken from stored evidence (the representative article
    title). Without one — or with a generic-only statement — this returns
    None and the caller fails closed instead of emitting counts-only
    boilerplate; summaries are never invented."""
    statement = " ".join(str(headline or "").split())
    if not substantive_statement(statement):
        return None
    raw = str(term)
    name = raw.split(":", 1)[-1].replace("_", " ").strip()
    known = {
        "nvidia": "NVIDIA",
        "tesla": "TSLA(테슬라)",
        "apple": "AAPL(애플)",
        "microsoft": "MSFT(마이크로소프트)",
    }
    display = known.get(name.casefold(), name)
    urgent = event_class == "CORE_BREAKING" and materiality != "WEAK"
    heading = "🚨 긴급 NEWS · " + display if urgent else "📰 " + display + " 뉴스 동향"
    relation = (
        "포트폴리오 직접 관련"
        if lane == "DIRECT_HOLDING"
        else "전체 뉴스 빈도에서 발견"
    )
    # W9 (SDS 2026-09-01 §E.3): the four-way confirmation distinction rides
    # a dedicated label line after the evidence line — "확인:" vocabulary,
    # never the forbidden digest markers. Unknown states omit the line.
    label = CONFIRMATION_LABELS_KO.get(confirmation_state)
    confirmation_line = f"\n확인: {label}" if label else ""
    # A1 §Q.3 daytime effect: model-promoted relevance surfaces as one
    # labelled line after the confirmation line; DIRECT stays expressed by
    # the existing relation wording, never by this line.
    relevance_label = _RELEVANCE_LABELS_KO.get(relevance)
    relevance_line = f"\n{relevance_label}" if relevance_label else ""
    return (
        f"{heading}\n"
        f"{DIGEST_STATEMENT_PREFIX}{statement}\n"
        f"근거: 기사 {int(stories)}건 · 출처 {int(sources)}곳 · {relation}"
        f"{confirmation_line}{relevance_line}"
    )


def _finalize(store, finalized, candidate_id, state, now):
    store.finalize_candidate(candidate_id, state, now=now)
    finalized[state] = finalized.get(state, 0) + 1


def _process_candidate(
    deps, store, row, now, finalized, *, alias_map, identity_pairs, official_counters
):
    candidate_id = row["candidate_id"]
    episode_id, settled_call = store.candidate_checkpoint(candidate_id)
    if (
        episode_id is None
        and settled_call is None
        and store.count_candidate_classifier_calls(candidate_id) >= deps.attempt_max
    ):
        _finalize(store, finalized, candidate_id, "EXPIRED", now)
        return "done"
    term = row["entity_key"] or row["phrase_text"]
    key = row["entity_key"] or ""
    if settled_call is None and not candidate_requires_classifier(
        key, term, lane=row["lane"]
    ):
        _finalize(store, finalized, candidate_id, "DROPPED_NOISE", now)
        return "done"
    document_ids, latest_observed, headline, evidence_docs = _candidate_documents(row)
    if episode_id is None and store.episode_state(_derived_episode_id(term)) == "MUTED":
        _finalize(store, finalized, candidate_id, "DROPPED_NOISE", now)
        return "done"
    delivery_enabled = (
        bool(deps.delivery_activation(now))
        if deps.delivery_activation is not None
        else False
    )
    if episode_id is not None:
        revision_no = store.revisions(episode_id)[-1].revision
        if store.next_alert_revision_no(episode_id) > revision_no:
            # C4 output is already durable from a pre-crash pass: converge
            # the outbox and close without touching the model (W-P8/W-P9).
            reconcile_news_outbox(
                store=store,
                ops_kernel=deps.ops_kernel,
                role=deps.role,
                activation_enabled=delivery_enabled,
                telegram_cutoff=deps.gate_cutoff,
                now=now,
            )
            _finalize(store, finalized, candidate_id, "MERGED_INTO_EPISODE", now)
            return "done"
    # Truthful evidence r2 (SDS §D.4): display identity, per-doc headline
    # provenance, deterministic FACT/OPINION source counts, and the official
    # primary-evidence token — all from already-persisted data.
    display, alias_terms = (
        (term, (term,)) if not key else display_aliases(key, alias_map, identity_pairs)
    )
    # D-5 (§R.2): FACT/OPINION independence per publisher ORIGIN — feeds
    # of one origin and every unresolved document collapse fail-closed.
    fact_n = count_independent_origins(
        (doc.get("source_id"), doc.get("publisher"))
        for doc in evidence_docs
        if doc.get("role") == "FACT_DISCOVERY" and doc.get("source_id")
    )
    opinion_n = count_independent_origins(
        (doc.get("source_id"), doc.get("publisher"))
        for doc in evidence_docs
        if doc.get("role") == "OPINION_DISCOVERY" and doc.get("source_id")
    )
    holding_terms = tuple(sorted(deps.holding_terms()))
    direct_holding = (
        row["lane"] == "DIRECT_HOLDING"
        or (bool(key) and key in identity_pairs)
        or bool(
            {str(alias).casefold() for alias in alias_terms}
            & {str(item).casefold() for item in holding_terms}
        )
    )
    # §V1 step 1: article action families from stored headlines only —
    # computed before any model/official involvement.
    article_families = derive_article_action_families(
        tuple(str(doc.get("title") or "") for doc in evidence_docs)
    )
    evidence = _classifier_evidence_r3(
        display=display,
        lane=row["lane"],
        direct_holding=direct_holding,
        stories=row["distinct_story_count"],
        sources=row["distinct_source_count"],
        fact_n=fact_n,
        opinion_n=opinion_n,
        evidence_docs=evidence_docs,
    )
    outcome = run_news_investigation(
        store=store,
        episode_id=None,
        candidate_id=candidate_id,
        purpose="EVENT_CLASS",
        evidence=evidence,
        prompt_policy_digest=_PROMPT_POLICY_DIGEST,
        model_contract_digest=_MODEL_CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog=deps.admitted_catalog,
        classifier_call=deps.classifier_call,
        now=now,
        retry_max=0,
        ops_kernel=deps.ops_kernel,
        role=deps.role,
        activation_enabled=False,
        config_path=deps.classifier_config_path,
        async_queue=deps.classifier_queue,
    )
    if outcome.get("status") != "OK":
        store.release_candidate(candidate_id, now=now)
        return (
            "budget" if outcome.get("exit") == "MODEL_BUDGET_EXCEEDED" else "released"
        )
    if episode_id is None:
        items = tuple(Evidence(doc_id, "CLAIM", None) for doc_id in document_ids)
        episode = store.upsert_with_cause(term, items, now, cause_key=term)
        episode_id = episode.episode_id
        store.attach_candidate_episode(candidate_id, episode_id, now=now)
        revision_no = episode.revision
    else:
        revision_no = store.revisions(episode_id)[-1].revision
    # Deterministic final judgment (SDS §E.1/§E.2, decision 9): the model
    # contributed bounded facts only; confirmation, event class,
    # materiality, and immediate eligibility are derived here. A legacy
    # v1-shaped output (no event_type key) degrades to OTHER — a model
    # label can never promote (m-4).
    event_type = outcome.get("event_type")
    if event_type not in EVENT_TYPES_V1:
        event_type = "OTHER"
    surprise = outcome.get("surprise_claim")
    # §V1 steps 3–5 (post-settlement only): event/article action
    # intersection gates AT MOST ONE bounded official lookup; only a
    # correlated ref ever reaches derive_confirmation. Same company/date
    # alone can never promote (§Z3).
    allowed_families = (
        EVENT_ACTION_FAMILIES_V1.get(event_type, frozenset()) & article_families
    )
    official_refs = ()
    if (
        deps.official_evidence is not None
        and direct_holding
        and key
        and allowed_families
    ):
        stock_codes = tuple(
            sorted(
                {
                    str(alias)
                    for alias in identity_pairs.get(key, ())
                    if str(alias).isdigit() and len(str(alias)) == 6
                }
            )
        )
        if stock_codes:
            observed_hint = datetime.fromisoformat(
                latest_observed if latest_observed else row["created_at"]
            )
            official_counters["official_attempts"] += 1
            lookup = deps.official_evidence(
                stock_codes,
                (observed_hint - timedelta(hours=DISCOVERY_WINDOW_HOURS)).date(),
                observed_hint.date(),
                allowed_families,
                candidate_id=candidate_id,
                attempt_no=store.count_candidate_classifier_calls(candidate_id),
                now=now,
                event_type=event_type,
            )
            official_refs = tuple(lookup.refs)
            official_counters["official_local_reads"] += lookup.local_reads
            official_counters["official_http_starts"] += lookup.http_starts
            official_counters["official_refs_returned"] += lookup.refs_returned
            official_counters["official_refs_correlated"] += lookup.refs_correlated
            official_counters["official_failures"] += 1 if lookup.failure else 0
    confirmation = derive_confirmation(
        official_refs[0] if official_refs else None,
        fact_n,
        opinion_n,
        outcome.get("confirmation_claim"),
    )
    systemic = event_type in _SYSTEMIC_EVENT_TYPES
    event_class = classify_event_guarded(
        direct_holding=direct_holding,
        official_primary=confirmation == "OFFICIAL_CONFIRMED",
        irreversible=event_type in _IRREVERSIBLE_EVENT_TYPES
        or (event_type == "EARNINGS_GUIDANCE" and surprise == "SURPRISE"),
        single_source_claim=row["distinct_source_count"] <= 1,
        systemic_transition=systemic,
        propagation_high=fact_n >= 3,
        classification_status="OK",
    )
    analysis = analyze_event(
        episode_id=episode_id,
        entities=tuple(alias_terms),
        scenarios=(),
        context=PortfolioContext(holding_terms, (), False),
        prior_reaction=None,
    )
    materiality = "DIRECT_HOLDING" if direct_holding else analysis.materiality
    # Relevance axis (A1 §Q.3): cited model claims may promote relevance
    # only — never identity, event class, or immediate eligibility.
    relevance = derive_relevance(
        direct_holding,
        outcome.get("relevance_claim"),
        tuple(outcome.get("relevance_citations") or ()),
        evidence,
    )
    # W6: the outbox kind reads episodes.event_class — persist the judged
    # class for THIS episode before admission (never retroactively).
    store.set_episode_event_class(episode_id, event_class, now=now)
    observed_at = datetime.fromisoformat(
        latest_observed if latest_observed else row["created_at"]
    )
    body = render_news_alert_body(
        term=term,
        event_class=event_class,
        materiality=materiality,
        lane=row["lane"],
        stories=row["distinct_story_count"],
        sources=row["distinct_source_count"],
        headline=headline,
        confirmation_state=confirmation,
        relevance=relevance,
    )
    material_change = "FIRST_ADMISSION" if revision_no == 1 else "SCALE_CHANGE"
    verdict = admit_news_event(
        AdmissionInput(
            episode_id,
            revision_no,
            event_class,
            material_change,
            observed_at,
            deps.gate_cutoff,
            False,
            delivery_enabled,
            admitted_at=now,
            immediate_eligible=(
                materiality == "DIRECT_HOLDING" and event_class == "CORE_BREAKING"
            )
            or systemic,
        )
    )
    status = verdict["status"]
    if status.startswith("REJECTED"):
        _finalize(store, finalized, candidate_id, "DROPPED_NOISE", now)
        return "done"
    if body is None:
        # No meaningful event statement in stored evidence: fail closed
        # and omit the alert entirely (no hold, no outbox); the episode
        # and classifier history above stay durable.
        _finalize(store, finalized, candidate_id, "MERGED_INTO_EPISODE", now)
        return "done"
    if status in ("SHADOW_HELD_MORNING", "HELD_MORNING"):
        store.hold_morning_revision(
            episode_id,
            revision_no,
            body,
            now=now,
            release_not_before=_next_morning_release(now),
        )
    else:
        run_news_event_admission(
            store=store,
            ops_kernel=deps.ops_kernel,
            role=deps.role,
            draft={
                "episode_id": episode_id,
                "revision_no": revision_no,
                "revision_kind": ("INITIAL" if revision_no == 1 else "MATERIAL_UPDATE"),
                "body_text": body,
                "portfolio_context_digest": "0" * 64,
                "confidence": 50,
                "material_change_kind": material_change,
                "material_change_observed_at": observed_at,
                "links": (),
            },
            activation_enabled=delivery_enabled,
            telegram_cutoff=deps.gate_cutoff,
            now=now,
        )
    _finalize(store, finalized, candidate_id, "MERGED_INTO_EPISODE", now)
    return "done"


def run_news_production(
    *, producer_deps: NewsProducerDepsV1, now
) -> NewsProductionResultV1:
    deps = producer_deps
    store = deps.episode_store
    reconciled = store.reconcile_classifier_calls(now=now)
    # M4: one release cycle → at most one Telegram-visible combined digest;
    # the gate cutoff partitions pre-cutoff backlog into a SHADOW-only
    # digest (§2.3) instead of mixing it into a Telegram-eligible body.
    released_holds = len(
        store.release_morning_holds(
            now_kst=now.astimezone(_KST), telegram_cutoff=deps.gate_cutoff
        )
    )
    recovery, fresh = store.list_open_candidates(
        now=now,
        ttl_seconds=deps.ttl_seconds,
        lease_seconds=deps.lease_seconds,
        backoff_cap_seconds=deps.backoff_cap_seconds,
        limit_per_lane=deps.candidates_per_tick,
    )
    recovery = list(recovery)
    fresh = list(fresh)
    plan = []
    borrowed = 0
    if recovery:
        plan.append(("recovery", recovery.pop(0)))
    if fresh:
        plan.append(("fresh", fresh.pop(0)))
    while len(plan) < deps.candidates_per_tick and (recovery or fresh):
        borrowed += 1
        if fresh:
            plan.append(("fresh", fresh.pop(0)))
        else:
            plan.append(("recovery", recovery.pop(0)))
    counters = {
        "claims_attempted": 0,
        "claims_won": 0,
        "claims_won_recovery": 0,
        "claims_won_fresh": 0,
        "drained": 0,
        "candidate_failures": 0,
        "released_candidates": 0,
    }
    finalized = {}
    stop = False
    # Drain-time alias context (SDS §F.3): merged public + verified catalog
    # and the official identity pairs feed display naming and the direct-
    # holding judgment for every candidate in this tick.
    drain_catalog = {key: tuple(aliases) for key, aliases in _PUBLIC_ALIASES.items()}
    for key, aliases in store.verified_entity_aliases().items():
        drain_catalog[key] = tuple(
            dict.fromkeys(drain_catalog.get(key, ()) + tuple(aliases))
        )
    drain_identity_pairs = (
        dict(deps.identity_pairs()) if deps.identity_pairs is not None else {}
    )
    official_counters = {
        "official_attempts": 0,
        "official_local_reads": 0,
        "official_http_starts": 0,
        "official_refs_returned": 0,
        "official_refs_correlated": 0,
        "official_failures": 0,
    }
    for lane, row in plan:
        if stop:
            break
        counters["claims_attempted"] += 1
        if not store.claim_candidate(
            row["candidate_id"], expected_updated_at=row["updated_at"], now=now
        ):
            continue
        counters["claims_won"] += 1
        counters[
            "claims_won_recovery" if lane == "recovery" else "claims_won_fresh"
        ] += 1
        try:
            token = _process_candidate(
                deps,
                store,
                row,
                now,
                finalized,
                alias_map=drain_catalog,
                identity_pairs=drain_identity_pairs,
                official_counters=official_counters,
            )
        except NewsAdmissionRejected:
            _finalize(store, finalized, row["candidate_id"], "DROPPED_NOISE", now)
            counters["drained"] += 1
        except Exception:
            # Per-candidate isolation (SDS §6.6): one failure never blocks
            # the tick's remaining slots, expiry, or discovery.
            counters["candidate_failures"] += 1
        else:
            counters["drained"] += 1
            if token == "released":
                counters["released_candidates"] += 1
            elif token == "budget":
                counters["released_candidates"] += 1
                stop = True
    expired = store.expire_stale_candidates(
        now=now, ttl_seconds=deps.ttl_seconds, limit=PRODUCER_EXPIRY_PER_TICK
    )
    slot_epoch = (int(now.timestamp()) // deps.slot_seconds) * deps.slot_seconds
    window_to = datetime.fromtimestamp(slot_epoch, UTC)
    due = not store.discovery_run_exists_for_window(window_to.isoformat())
    run_id = None
    discovery_status = None
    taxonomy_admitted = 0
    taxonomy_pending = 0
    if due:
        window_from = max(
            window_to - timedelta(hours=DISCOVERY_WINDOW_HOURS), deps.gate_cutoff
        )
        read = deps.documents_reader(window_from)
        discovery_status = read.status
        if read.status == "OK":
            holdings = set(deps.holding_terms())
            # Taxonomy admission first (stages 1–6, zero model calls), so
            # freshly verified aliases immediately shape this discovery run.
            catalog = {key: tuple(aliases) for key, aliases in _PUBLIC_ALIASES.items()}
            for key, aliases in store.verified_entity_aliases().items():
                catalog[key] = tuple(
                    dict.fromkeys(catalog.get(key, ()) + tuple(aliases))
                )
            supplied = deps.interest_terms() if deps.interest_terms is not None else {}
            typed_interests = (
                dict(supplied)
                if isinstance(supplied, Mapping)
                else {"ENTITY": tuple(supplied)}
            )
            taxonomy = run_taxonomy_admission(
                store=store,
                documents=read.documents,
                now=now,
                holdings=holdings,
                typed_interests=typed_interests,
                identity_catalog=catalog,
                official_identities=(
                    dict(deps.identity_pairs())
                    if deps.identity_pairs is not None
                    else None
                ),
            )
            taxonomy_admitted = taxonomy.admitted_auto
            taxonomy_pending = taxonomy.pending
            # §V6/§V7 instrument scans: official display names (never bare
            # symbols) from the official local identity pairs.
            official_names = {
                key: tuple(alias for alias in aliases if not str(alias).isdigit())
                for key, aliases in (
                    dict(deps.identity_pairs())
                    if deps.identity_pairs is not None
                    else {}
                ).items()
            }
            result = discover_topics(
                read.documents,
                holdings=holdings,
                extra_aliases=store.verified_entity_aliases(),
                official_names={
                    key: names for key, names in official_names.items() if names
                },
            )
            run_id = persist_discovery_result(
                store,
                result,
                window_from=window_from,
                window_to=window_to,
                documents=read.documents,
                now=now,
            )
        else:
            run_id = store.record_discovery_failure(
                window_from=window_from,
                window_to=window_to,
                status=read.status,
                now=now,
            )
    return NewsProductionResultV1(
        due=due,
        discovery_run_id=run_id,
        discovery_status=discovery_status,
        reconciled_calls=reconciled,
        released_holds=released_holds,
        borrowed_slots=borrowed,
        finalized=finalized,
        expired_candidates=expired,
        taxonomy_admitted=taxonomy_admitted,
        taxonomy_pending=taxonomy_pending,
        **counters,
        **official_counters,
    )
