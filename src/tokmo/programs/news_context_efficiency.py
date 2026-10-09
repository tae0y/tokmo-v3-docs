"""Small model views and bounded reuse of an explicitly scheduled review.

Retrieval chooses detail, never which new evidence the screen may judge.
Reuse is not a fresh judgment, a source refresh, or a delivery receipt.
"""

import hashlib
import re
from copy import deepcopy
from datetime import timedelta

from tokmo.programs.news_context_memory import memory_view, observation_view
from tokmo.records.news_decision_store import encoded, kst, timestamp

VERSION = "news-efficiency-20260910"
DETAIL_LIMIT = 12


def _terms(text):
    words = re.findall(r"[a-z0-9]+|[가-힣]+", text.lower())
    # Korean particles need not be identical for detail retrieval. This is
    # generic text overlap, not a list of allowed companies or event types.
    return set(words) | {
        word[i : i + 2]
        for word in words
        if re.fullmatch("[가-힣]+", word)
        for i in range(len(word) - 1)
    }


def screen_view(events, batch, overlap):
    query = _terms(
        " ".join(str(row.get(k) or "") for row in batch for k in ("title", "text"))
    )

    def rank(event):
        memory = memory_view(event)
        words = _terms(
            event["decision"]["title"]
            + " "
            + memory.get("topic", "")
            + " "
            + memory.get("watch", "")
        )
        score = len(query & words) / max(1, len(words)) ** 0.5
        return (
            event["id"] not in overlap,
            -score,
            -timestamp(event["updated_at"]).timestamp(),
            event["id"],
        )

    # Keep the existing 100-event exposure bound and direct-document priority.
    catalog = sorted(
        events,
        key=lambda e: (
            e["id"] not in overlap,
            -timestamp(e["updated_at"]).timestamp(),
            e["id"],
        ),
    )[:100]
    detailed = {e["id"] for e in sorted(catalog, key=rank)[:DETAIL_LIMIT]}
    rows = []
    for event in sorted(catalog, key=lambda e: e["id"]):
        memory = memory_view(event)
        sent = event.get("last_sent") or {}
        row = dict(
            event_id=event["id"],
            title=event["decision"]["title"],
            open=event["decision"]["open"],
            phase=memory.get("phase"),
            last_delivery=event.get("last_delivery", "NOT_SENT"),
            last_sent_at=sent.get("delivered_at"),
            detail_included=event["id"] in detailed,
        )
        if row["detail_included"]:
            row.update(
                context_memory=memory,
                context_observations=observation_view(event),
                last_sent_memory=deepcopy(sent.get("context_memory")),
            )
        rows.append(row)
    return rows, detailed


def context_basis(context):
    """Ignore read timestamps; retain stale status, choices and holdings changes.

    Display quotes do not drive this NEWS judgment. Quant remains responsible
    for price-triggered analysis; source measurements in events are retained.
    """

    def clean(value):
        if isinstance(value, dict):
            return {
                k: clean(v)
                for k, v in value.items()
                if k not in {"observed_at", "captured_at", "identity_at"}
                and not k.startswith("_")
            }
        if isinstance(value, list):
            return sorted((clean(v) for v in value), key=encoded)
        return value

    return clean(context)


def followup_scope(value, context):
    """Missing/invalid auxiliary scope keeps the complete follow-up context."""
    all_choices = dict(all_choices=True, choice_ids=[])
    if not isinstance(value, dict) or type(value.get("all_choices")) is not bool:
        return all_choices
    if value["all_choices"]:
        return all_choices
    ids = value.get("choice_ids")
    followup = context.get("investment_followup") or {}
    known = {c.get("id") for c in followup.get("choices", [])}
    known.update(r.get("choice_id") for r in followup.get("delivered_reviews", []))
    if not isinstance(ids, list) or any(
        not isinstance(i, str) or i not in known for i in ids
    ):
        return all_choices
    return dict(all_choices=False, choice_ids=sorted(set(ids)))


def scoped_context(context, scope):
    """Keep every holding and user choice; scope only delivered review detail."""
    result = deepcopy(context)
    followup = result.get("investment_followup")
    if isinstance(followup, dict) and scope and not scope["all_choices"]:
        followup["delivered_reviews"] = [
            r
            for r in followup.get("delivered_reviews", [])
            if r.get("choice_id") in scope["choice_ids"]
        ]
    return result


REVIEW_FACTS_BOUND = 2


def compact_context(context):
    """Model view of the account context: ETF membership as stock rows of key/name/weight (bonds
    and cash remain as the asset split and a count), one latest delivered review per choice with
    its facts as text, no repeated boilerplate notes. Holdings, instruments, exposures and every
    user choice are kept whole. Idempotent, and applied alike to the current context and to stored
    snapshots so prior deltas compare like with like."""
    result = deepcopy(context)
    rows = result.get("etf_context")
    if isinstance(rows, list):
        result["etf_context"] = [
            _compact_etf(row) if isinstance(row, dict) else row for row in rows
        ]
    followup = result.get("investment_followup")
    if isinstance(followup, dict) and isinstance(
        followup.get("delivered_reviews"), list
    ):
        latest = {}
        for review in followup["delivered_reviews"]:
            if not isinstance(review, dict):
                continue
            key = review.get("choice_id")
            if key not in latest or _review_order(review) > _review_order(latest[key]):
                latest[key] = review
        followup["delivered_reviews"] = [
            _compact_review(r) for r in sorted(latest.values(), key=_review_order)
        ]
    return result


def _review_order(review):
    return (str(review.get("delivered_at") or ""), str(review.get("id") or ""))


def _compact_etf(row):
    result = dict(row)
    components = result.get("components")
    if isinstance(components, dict):
        components = {k: v for k, v in components.items() if k != "note"}
        members = components.get("rows")
        if isinstance(members, list):
            stocks = [
                r
                for r in members
                if isinstance(r, dict) and r.get("asset_kind", "stock") == "stock"
            ]
            # ``code`` stays: ETF relevance is matched by local code as well as by canonical key.
            components["rows"] = [
                {k: r[k] for k in ("key", "code", "name", "weight_pct") if k in r}
                for r in stocks
            ]
            components.setdefault("non_stock_rows", len(members) - len(stocks))
        result["components"] = components
    nav = result.get("nav")
    if isinstance(nav, dict):
        result["nav"] = {k: v for k, v in nav.items() if k != "note"}
    return result


def _compact_review(review):
    result = {k: v for k, v in review.items() if k != "new_facts"}
    facts = review.get("new_facts")
    if isinstance(facts, list):
        result["new_facts"] = [
            f.get("text") if isinstance(f, dict) else f
            for f in facts[:REVIEW_FACTS_BOUND]
        ]
    return result


def _digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


def portfolio_basis(context):
    result = context_basis(context)
    if isinstance(result.get("investment_followup"), dict):
        result["investment_followup"].pop("delivered_reviews", None)
    return _digest(result)


def review_basis(event, context, policy, related=()):
    """Named, hashed dependencies explain misses without persisting portfolio text."""
    scope = followup_scope(event.get("followup_scope"), context)
    followup = (
        scoped_context(context_basis(context), scope).get("investment_followup") or {}
    )

    def fields(*names):
        return {k: event.get(k) for k in names}

    related_rows = []
    for row in related:
        value = {
            k: row.get(k) for k in ("id", "context_memory", "last_sent", "evidence")
        }
        if isinstance(value["context_memory"], dict):
            value["context_memory"] = {
                k: v for k, v in value["context_memory"].items() if k != "reviewed_at"
            }
        related_rows.append(value)
    return {
        k: _digest(v)
        for k, v in dict(
            POLICY_CHANGED=[VERSION, policy],
            EVIDENCE_CHANGED=fields(
                "evidence", "memory_sources", "unsent_evidence", "source_enrichment"
            ),
            JUDGMENT_CHANGED=fields("context_memory", "decision"),
            PORTFOLIO_CHANGED=portfolio_basis(context),
            FOLLOWUP_REPORT_CHANGED=[scope, followup.get("delivered_reviews", [])],
            DELIVERY_CHANGED=fields(
                "last_sent",
                "last_delivery",
                "recovery_generation",
                "comparison_proposals",
            ),
            RELATED_EVENT_CHANGED=sorted(related_rows, key=lambda r: r["id"]),
        ).items()
    }


def review_fingerprint(event, context, policy, related=()):
    return _digest(review_basis(event, context, policy, related))


def compress_prior_context(prior, current):
    """Reference shared fields but preserve every changed historical value."""
    if context_basis(prior) == context_basis(current):
        return dict(
            same_content_as="context",
            observed_at=prior.get("observed_at"),
            note="계좌/선택 내용은 현재 context와 동일. 이전 확인 시각은 새 판단을 뜻하지 않음.",
        )
    result = deepcopy(prior)
    for key, value in prior.items():
        if (
            isinstance(value, (dict, list))
            and key in current
            and context_basis({"v": value}) == context_basis({"v": current[key]})
        ):
            result[key] = dict(same_content_as="context." + key)
    return result


def source_expansions(visible, sources, remembered, render):
    """Only advertise a restoration when the supplied source text can grow."""
    current = {r["id"]: r for r in visible}
    expanded = {}
    for key in sorted(set(current) | set(remembered)):
        if key not in sources:
            continue
        row = render([sources[key]], text_limit=4000)[0]
        old_text = (current.get(key) or {}).get("text", "")
        if row.get("text") and (key not in current or row["text"] != old_text):
            expanded[key] = row
    return expanded


def review_plan(value, decision, at, *, typed=False):
    """An invalid/missing auxiliary plan keeps the existing 6/24h schedule."""
    if not decision.get("open") or not isinstance(value, dict):
        return None
    reason = value.get("reason")
    if not isinstance(reason, str) or not reason.strip() or len(reason) > 400:
        return None
    try:
        until = timestamp(value["after"])
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    # Operational backstop, not a definition of a meaningful market change.
    maximum = timedelta(hours=24 if decision["importance"] == "HIGH" else 72)
    if not at < until <= at + maximum:
        return None
    plan = dict(after=kst(until), reason=reason.strip(), judged_at=kst(at))
    if typed:
        kind = value.get("kind")
        if kind not in {"NEW_FACTS", "DEADLINE", "PERSISTENCE", "SAFETY"}:
            return plan  # Legacy/invalid hint preserves the earlier hard deadline.
        plan.update(kind=kind, safety_at=kst(at + maximum), version=1)
    return plan
