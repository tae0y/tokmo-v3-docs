"""Accumulate evidence by choice, not elapsed minutes. Existing periodic review remains."""

from datetime import timedelta

from tokmo.values.followup_failures import retry_due
from tokmo.values.quant_decisions import instant

MAX_BUNDLE_EVENTS = 32
# The screen weighs the newest unjudged facts; older ones stay in the bundle
# and are counted, so the matcher knows how much it is not being shown.
MAX_SCREEN_EVENTS = 10


def event_context(event):
    """Facts and dates for semantic comparison; AI summaries are labelled separately."""
    rows = event.get("evidence", [])
    cited = set(event.get("decision", {}).get("evidence_ids", []))
    selected = [r for r in rows if r["id"] in cited or r in rows[-3:]]
    facts = []
    for row in selected:
        text = row.get("text", "")
        facts.append(
            {
                "id": row["id"],
                "title": row.get("title", ""),
                "text": text[:400],
                "text_truncated": len(text) > 400,
                "published_at": row.get("published_at"),
                "available_at": row["observed_at"],
                "content_scope": row.get("content_scope"),
            }
        )
    return {
        "event_id": event["id"],
        "updated_at": event["updated_at"],
        "facts": facts,
        "omitted_sources": len(rows) - len(selected),
        "news_interpretation": {
            k: event.get("decision", {}).get(k)
            for k in ("title", "summary", "why_now", "importance")
        },
    }


def choice_context(store, choice, at):
    """Keep user-visible and internal assessments distinct, with unreviewed facts."""
    last = store.latest_review(choice["id"], at=at, delivered=True)
    if last is None and choice.get("supersedes"):
        last = store.latest_review(choice["supersedes"], at=at, delivered=True)
    current = store.latest_review(choice["id"], at=at)

    def brief(row):
        if not row:
            return None
        a = row["assessment"]
        # The facts a judgment already weighed are its baseline. A silent
        # "context only" verdict must move that baseline as much as a delivered
        # one, or every later article re-opens the same delta.
        judged = [
            e["id"]
            for e in (row.get("input") or {}).get("evidence") or []
            if isinstance(e, dict) and e.get("id")
        ]
        return {
            "id": row["id"],
            "reviewed_at": row["reviewed_at"],
            "delivered_at": row.get("delivered_at"),
            "comparison": a["comparison"],
            "new_facts": a["new_facts"],
            "next_checks": a["next_checks"],
            "change": a.get("change"),
            "judged_evidence_ids": judged,
        }

    queued = store.watermark("followup_bundle:" + choice["id"], {}) or {}
    events = sorted(
        queued.get("events", {}).values(), key=lambda e: e.get("updated_at", "")
    )
    recent = events[-MAX_SCREEN_EVENTS:]
    return {
        "last_delivered": brief(last),
        "current_assessment": brief(current),
        "accumulating": {
            "event_ids": list(queued.get("events", {})),
            "facts": [event_context(e) for e in recent],
            "omitted_events": len(events) - len(recent),
            "reasons": queued.get("reasons", []),
        },
    }


def match_choice_context(store, choice, at):
    """Historical comparison is not the current batch's selectable event list.

    Keep the facts, source references and dates; omit historical event handles
    that the matcher cannot return. Full event records remain in the bundle.
    """
    context = choice_context(store, choice, at)
    context["role"] = "COMPARISON_ONLY"
    context["accumulating"].pop("event_ids", None)
    for event in context["accumulating"]["facts"]:
        event.pop("event_id", None)
    return context


def bundle_matches(store, choices, matches, events, *, at, attempt, interval_hours=6):
    """Known context is recorded; possible judgment changes are reviewed this tick.

    No per-event delay. Old 60-minute bundles are drained on the next tick.
    The existing periodic review and queue-size bound include retained facts;
    RECORD is not permanent exclusion. No source version is consumed before its
    facts or review task have been durably retained.
    """
    by_event = {e["id"]: e for e in events}
    by_match = {m["choice_id"]: m for m in matches}
    tasks, retained, waiting = [], {}, []
    for choice in choices:
        key = "followup_bundle:" + choice["id"]
        bundle = store.watermark(key, {}) or {}
        legacy = bool(bundle and bundle.get("mode") != "choice-evidence-state")
        match = by_match.get(choice["id"])
        if bundle:
            bundle["events"].update(
                {eid: by_event[eid] for eid in bundle["events"] if eid in by_event}
            )
        if match:
            if not bundle:
                bundle = {"first_at": at.isoformat(), "events": {}, "reasons": []}
            bundle["events"].update({eid: by_event[eid] for eid in match["event_ids"]})
            bundle["reasons"] = list(
                dict.fromkeys([*bundle["reasons"], match["reason"]])
            )[-4:]
        if not bundle:
            continue
        last = store.latest_review(choice["id"], at=at)
        periodic = last is None or at - instant(last["reviewed_at"]) >= timedelta(
            hours=interval_hours
        )
        requested = bool(match and match.get("assessment") == "REVIEW")
        due = (
            legacy
            or requested
            or periodic
            or len(bundle["events"]) >= MAX_BUNDLE_EVENTS
            or bundle.get("review_required", False)
        )
        bundle.update(mode="choice-evidence-state", review_required=due)
        bundle.pop("due_at", None)
        store.set_watermark(key, bundle)
        failure = store.watermark("failed_review:" + choice["id"])
        if failure and not match and not retry_due(failure, at=at, attempt=attempt):
            due = False
        if due:
            retained.update(bundle["events"])
            tasks.append(
                {
                    "choice_id": choice["id"],
                    "event_ids": list(bundle["events"]),
                    "reason": " / ".join(bundle["reasons"]),
                    "bundle": {
                        "first_at": bundle["first_at"],
                        "event_count": len(bundle["events"]),
                        "trigger": "LEGACY_DRAIN"
                        if legacy
                        else "MEANING_CHANGE"
                        if requested
                        else "PERIODIC_OR_CAPACITY",
                    },
                }
            )
        else:
            waiting.append(choice["id"])
    return tasks, list(retained.values()), waiting


def acknowledge_bundle(store, task):
    """The review/failure journal owns the input; don't discard newly added facts."""
    key = "followup_bundle:" + task["choice_id"]
    bundle = store.watermark(key, {}) or {}
    if not bundle:
        return
    for event_id in task["event_ids"]:
        bundle["events"].pop(event_id, None)
    # The carried-over demand is settled by this review (or its journaled
    # failure). Leftover facts are judged afresh from new matches, not re-run
    # every tick on the strength of the old flag.
    bundle["review_required"] = False
    store.set_watermark(key, bundle if bundle["events"] else None)
