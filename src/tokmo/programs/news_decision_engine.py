"""Shared news decision path for chronological replay and the NEWS worker.

Transport, clock and account reads are injected. No keyword/holding-count gate
decides which articles may be seen by a model. An alert is an outbox proposal,
not a claim that Telegram delivered it.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from tokmo.programs.news_document_quality import (
    document_amounts,
    validate_document_amounts,
)
from tokmo.records.news_decision_store import (
    encoded,
    kst,
    kst_field,
    kst_projection,
    timestamp,
)

KST = ZoneInfo("Asia/Seoul")
# Replay contract: every model call uses the tick's recorded ``now`` instead of a live clock.
RECORDED_TIME = "recorded"
# Every model request states its time basis; no session calendar is bound, so nothing may
# claim pre-market/open/closed status.
TIME_BASIS = {"time_basis": "Asia/Seoul", "session_status": "UNVERIFIED"}


def identity(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()[:24]


# Operational bound on comparison bodies/evidence shown to a model call; omissions are reported, never implied.
COMPARISON_BOUND = 8
# A due periodic recheck older than this is retired without a model call (new evidence still reopens the event).
STALE_RECHECK_HOURS = 72
# Stored-original fields compared between versions of one document (published_at as an instant).
DELTA_FIELDS = ("title", "text", "url", "published_at", "publisher")
# Context keys that only say when the portfolio was read; they never make a transition new.
CONTEXT_TIME_KEYS = frozenset({"observed_at", "captured_at", "portfolio_status"})


def doc_key(key):
    """Source-native document identity: provenance only, never event identity or a duplicate verdict."""
    parts = str(key).split(":")
    if parts[0] == "news" and len(parts) == 3:
        return "news:" + parts[1]
    if parts[0] == "dart" and len(parts) == 4:
        return "dart:" + parts[1]
    if parts[0] == "sec" and len(parts) in {3, 4}:
        return "sec:" + parts[1]
    return key


def _changed(old, new):
    changed = []
    for field in DELTA_FIELDS:
        before, after = old.get(field), new.get(field)
        if before == after:
            continue
        if field == "published_at" and before and after:
            try:
                if timestamp(before) == timestamp(after):
                    continue
            except (ValueError, TypeError, AttributeError):
                pass
        changed.append(field)
    return sorted(changed)


def _text_change(old, new, limit=4000, window=160):
    before, after = str(old.get("text") or ""), str(new.get("text") or "")
    offset = next(
        (index for index, (x, y) in enumerate(zip(before, after)) if x != y),
        min(len(before), len(after)),
    )
    start = max(0, offset - window)
    covered_end = max(limit, offset + window)
    return {
        "offset": offset,
        "prev": before[start : offset + window],
        "new": after[start : offset + window],
        "visible_in_model_text": offset < limit,
        "semantic_support_incomplete": before[covered_end:] != after[covered_end:],
    }


def evidence_delta(exposed, history, cited):
    """Provenance delta of every exposed item over stored originals. ``version_prev`` is the version of
    the same document that the delivered baseline cited (DELIVERED) when that is known and present, else
    the immediately preceding observed version (OBSERVATION), else nothing (NONE); the observation-relative
    view is always given separately. ``cited`` is the delivered baseline's evidence ids or "UNKNOWN". An
    uncited document is "not cited", never "not covered by the body"."""
    by_id = {item["id"]: item for item in history}
    order = [item["id"] for item in history]
    known = cited != "UNKNOWN"
    delivered = {}
    for key in cited if known else []:
        delivered.setdefault(doc_key(key), key)
    result = []
    for item in exposed:
        key, doc = item["id"], doc_key(item["id"])
        position = order.index(key) if key in order else len(order)
        observed_prev = next(
            (other for other in reversed(order[:position]) if doc_key(other) == doc),
            None,
        )
        cited_version = delivered.get(doc)
        if cited_version and cited_version != key and cited_version in by_id:
            prev, basis = cited_version, "DELIVERED"
        elif observed_prev:
            prev, basis = observed_prev, "OBSERVATION"
        else:
            prev, basis = None, "NONE"
        row = {
            "id": key,
            "doc_key": doc,
            "version_prev": prev,
            "version_prev_basis": basis,
            "changed_fields": _changed(by_id[prev], item) if prev else None,
            "title_prev": by_id[prev].get("title") if prev else None,
            "title_new": item.get("title"),
            "cited_in_delivered": (key in cited) if known else "UNKNOWN",
            "delivered_version": cited_version if known else "UNKNOWN",
            "observed_prev": observed_prev,
            "changed_since_observed": _changed(by_id[observed_prev], item)
            if observed_prev
            else None,
        }
        if prev and "text" in row["changed_fields"]:
            row["text_change"] = _text_change(by_id[prev], item)
        result.append(row)
    return result


def morning(now):
    local = timestamp(now).astimezone(KST)
    result = local.replace(hour=8, minute=0, second=0, microsecond=0)
    return result + timedelta(days=1) if local.hour >= 22 else result


def night(now):
    return timestamp(now).astimezone(KST).hour not in range(8, 22)


def headline_only(decision):
    return (
        not decision.get("portfolio_note", "").strip()
        and decision["urgency"] != "IMMEDIATE"
        and decision["change"] != "CORRECTION"
    )


def needs_confirmation(decision):
    """Why a first-pass judgment must be confirmed by the primary model, or None when the first
    pass settles it: anything that would wake or individually notify the user, change delivered
    standing, or is judged HIGH. Silence and a MEDIUM evening headline (digest line) settle on the
    first pass. Tolerates a contract-invalid response; the caller's validation still applies."""
    if decision.get("urgency") == "IMMEDIATE":
        return "IMMEDIATE"
    if decision.get("change") in {"CORRECTION", "RESOLVED"}:
        return str(decision["change"])
    if decision.get("importance") == "HIGH":
        return "HIGH"
    if (
        decision.get("notify") is True
        and str(decision.get("portfolio_note") or "").strip()
    ):
        return "NOTIFY"
    return None


def evening_slot(now, hour=18, *, next_slot=False):
    local = timestamp(now).astimezone(KST)
    slot = local.replace(hour=hour, minute=0, second=0, microsecond=0)
    return slot + timedelta(days=1) if next_slot and local >= slot else slot


def _digest_text(value):
    if not isinstance(value, str):
        return ""
    value = re.sub(r"\[([^\]]+)\]\(https?://[^\s)]+\)", r"\1", value)
    value = re.sub(r"https?://\S+|www\.\S+", "", value)
    return " ".join(value.split()).strip()


def evening_digest_alerts(alerts):
    from tokmo.programs.news_source_buttons import source_buttons

    alerts = sorted(alerts, key=lambda item: (item["created_at"], item["id"]))
    entries = []
    for index, alert in enumerate(alerts, 1):
        title = alert.get("headline") or alert["body"].splitlines()[0].removeprefix(
            "[뉴스 한줄] "
        )
        # The digest is plain text. Keep sources and the original judgment in the internal index.
        title = _digest_text(title) or "제목 확인 필요"
        summary = _digest_text(alert.get("digest_summary"))
        entries.append(
            dict(
                number=index,
                title=title,
                summary=summary,
                event_id=alert["event_id"],
                proposal_id=alert["id"],
                digest_slot=alert["evening_slot"],
                analyzed_at=alert["created_at"],
                source_buttons=source_buttons(alert["body"], number=index)[1],
            )
        )
    date = timestamp(alerts[0]["evening_slot"]).astimezone(KST).strftime("%m-%d")
    return dict(
        id="evening-digest-" + identity([item["id"] for item in alerts]),
        kind="EVENING_DIGEST",
        event_ids=[item["event_id"] for item in alerts],
        constituent_ids=[item["id"] for item in alerts],
        body=f"[저녁 뉴스 모아보기 · {date}]\n\n"
        + "\n\n".join(
            f"{item['number']}. {item['title']}"
            + (f"\n   - {item['summary']}" if item["summary"] else "")
            for item in entries
        ),
        entries=entries,
        digest_hour=alerts[0]["digest_hour"],
        digest_slot=alerts[0]["evening_slot"],
        created_at=kst(min(timestamp(item["created_at"]) for item in alerts)),
        evidence_snapshots={
            item["event_id"]: item.get("evidence_ids", []) for item in alerts
        },
        context_snapshots={
            item["event_id"]: item["context_memory"]
            for item in alerts
            if item.get("context_memory")
        },
    )


def digest_alerts(alerts):
    alerts = sorted(alerts, key=lambda item: item["id"])
    return {
        "id": "digest-" + identity([item["id"] for item in alerts]),
        "kind": "MORNING_DIGEST",
        "event_ids": [item["event_id"] for item in alerts],
        "constituent_ids": [item["id"] for item in alerts],
        "body": "[밤사이 중요 소식]\n\n"
        + "\n\n────────\n\n".join(item["body"] for item in alerts),
        "created_at": kst(min(timestamp(item["created_at"]) for item in alerts)),
        "evidence_snapshots": {
            item["event_id"]: item.get("evidence_ids", []) for item in alerts
        },
        "context_snapshots": {
            item["event_id"]: item["context_memory"]
            for item in alerts
            if item.get("context_memory")
        },
    }


def model_evidence(items, *, text_limit):
    """Model view of evidence: named time keys in KST (stored legacy rows stay UTC text), text bounded."""
    result = []
    for item in items:
        text = str(item.get("text") or "")
        limit = (
            12000
            if text_limit == 4000 and item.get("content_scope") == "original_document"
            else text_limit
        )
        result.append(
            dict(
                kst_projection(item),
                text=text[:limit],
                text_truncated=len(text) > limit,
            )
        )
    return result


def prior_times(prior):
    """Model view of a prior event: its named time fields in KST, nested ones included."""
    prior = {
        key: kst_field(value) if key in {"updated_at", "held_until"} else value
        for key, value in prior.items()
    }
    for key, child in (
        ("context_snapshot", "observed_at"),
        ("last_alert", "created_at"),
        ("last_sent", "delivered_at"),
    ):
        if isinstance(prior.get(key), dict) and prior[key].get(child) is not None:
            prior[key] = dict(prior[key], **{child: kst_field(prior[key][child])})
    return prior


def validate_decision(
    value,
    evidence,
    previous,
    *,
    baseline_applicable=False,
    require_notify=False,
    prior_bodies=(),
):
    """``baseline_applicable``: a delivered, pending or other-event body was supplied for comparison, so
    every non-NONE decision (NEW included) must state its material change; without one, a first report
    may omit it. Already-cited/reobserved ids are flags for the model, never a validation predicate."""
    if not isinstance(value, dict):
        raise ValueError("analysis must be an object")
    if value.get("invalid_model_reason"):
        raise ValueError("model output: " + value["invalid_model_reason"])
    if (require_notify or "notify" in value) and type(value.get("notify")) is not bool:
        raise ValueError("notify must be boolean")
    for key in ("title", "facts", "interpretation", "why_now", "next_check"):
        if not isinstance(value.get(key), str) or not value[key].strip():
            raise ValueError("missing analytical text: " + key)
    for key, choices in {
        "importance": {"HIGH", "MEDIUM", "LOW"},
        "urgency": {"IMMEDIATE", "NORMAL"},
        "confirmation": {"OFFICIAL", "REPORTED", "UNVERIFIED"},
        "change": {"NEW", "UPDATE", "CORRECTION", "NONE", "RESOLVED"},
    }.items():
        # Type guard first: a JSON container here is a contract error, not a crash.
        if not isinstance(value.get(key), str) or value[key] not in choices:
            raise ValueError("invalid " + key)
    ids = value.get("evidence_ids")
    available = {item["id"]: item for item in evidence}
    if (
        not isinstance(ids, list)
        or not ids
        or any(not isinstance(key, str) or key not in available for key in ids)
    ):
        raise ValueError("unresolved evidence")
    if value["confirmation"] == "OFFICIAL" and not any(
        available[key].get("source_kind") in {"DISCLOSURE", "OFFICIAL"}
        or (
            available[key].get("source_kind") == "MARKET"
            and available[key].get("source_authority") == "OFFICIAL"
        )
        for key in ids
    ):
        raise ValueError("official confirmation without official evidence")
    if previous is not None and value.get("event_id") not in (None, previous["id"]):
        raise ValueError("analysis changed event identity")
    # No invented probabilities or digit ban. Rendering preserves grounded
    # numerical explanations; correctness is evaluated against cited sources.
    result = {
        key: value[key].strip()
        for key in ("title", "facts", "interpretation", "why_now", "next_check")
    }
    result.update(
        {key: value[key] for key in ("importance", "urgency", "confirmation", "change")}
    )
    # Legacy audit validation may omit it; every fresh engine call requires the new contract.
    result["notify"] = value.get("notify", True)
    result["evidence_ids"] = list(dict.fromkeys(ids))
    result["portfolio_note"] = (
        value.get("portfolio_note", "")
        if isinstance(value.get("portfolio_note", ""), str)
        else ""
    )
    # Display copy is optional for older adapters/replays. Length never rejects a valid decision.
    if isinstance(value.get("digest_summary"), str) and value["digest_summary"].strip():
        result["digest_summary"] = value["digest_summary"].strip()
    # Presentation only. Missing/invalid hints keep the full correction and do
    # not reject an otherwise valid warning or trigger another model call.
    notice = value.get("correction_notice")
    if (
        value["change"] == "CORRECTION"
        and isinstance(notice, str)
        and notice in {"EXPLANATION_ONLY", "SOURCE_ONLY", "MATERIAL", "UNCERTAIN"}
    ):
        result["correction_notice"] = notice
    # An optional price-display hint, never a prerequisite for a warning.
    if isinstance(value.get("subject_keys"), list):
        result["subject_keys"] = list(
            dict.fromkeys(k for k in value["subject_keys"] if isinstance(k, str))
        )[:2]
    result["open"] = value.get("open", value["change"] != "RESOLVED") is not False
    change = value.get("material_change")
    if change in (None, {}):
        if value["change"] != "NONE" and baseline_applicable:
            raise ValueError(
                "material_change required: a delivered, pending or other-event body was supplied for comparison"
            )
        change = None
    elif value["change"] == "NONE":
        raise ValueError("material_change must be null for NONE")
    else:
        texts = ("prior_claim", "new_fact", "why_not_covered")
        if (
            not isinstance(change, dict)
            or any(not isinstance(change.get(key), str) for key in texts)
            or not change["new_fact"].strip()
            or not change["why_not_covered"].strip()
        ):
            raise ValueError(
                "material_change requires prior_claim, new_fact and why_not_covered text"
            )
        support = change.get("support_ids")
        if (
            not isinstance(support, list)
            or not support
            or any(
                not isinstance(key, str) or key not in result["evidence_ids"]
                for key in support
            )
        ):
            raise ValueError(
                "material_change support_ids must be nonempty ids among evidence_ids"
            )
        change = {key: change[key].strip() for key in texts} | {
            "support_ids": list(dict.fromkeys(support))
        }
    result["material_change"] = change
    validate_document_amounts(result, evidence, prior_bodies)
    return result


def validate_screen(value, ids, event_ids, report):
    """Exact-coverage screen contract over ``ids``: every candidate ref is a supplied input,
    nothing is classified twice, every ignored entry names an input and a reason.
    With a supplied catalog, every candidate explicitly states its attachment,
    comparison list (possibly empty), and a short reason. This checks that a
    judgment was supplied, not that the model's judgment is semantically right.
    Stored historical calls are not migrated by this live-response validator.
    Returns (candidates, ignored, covered); omission is judged by the caller."""
    if not isinstance(value, dict):
        raise ValueError("screen must be an object")
    if value.get("invalid_model_reason"):
        raise ValueError("model output: " + value["invalid_model_reason"])
    candidates, ignored = value.get("candidates"), value.get("ignored")
    if not isinstance(candidates, list) or not isinstance(ignored, list):
        raise ValueError("screen requires candidates and explicit ignored reasons")
    covered, dismissed = set(), set()
    for candidate in candidates:
        refs = candidate.get("evidence_ids") if isinstance(candidate, dict) else None
        if (
            not isinstance(refs, list)
            or not refs
            or any(not isinstance(key, str) or key not in ids for key in refs)
        ):
            raise ValueError("screen references absent input")
        if len(refs) != len(set(refs)) or covered.intersection(refs):
            raise ValueError("screen classified input twice")
        event_id = candidate.get("event_id")
        # String check before membership: a JSON container is a contract error, not a crash.
        if event_id is not None and (
            not isinstance(event_id, str) or event_id not in event_ids
        ):
            raise ValueError("screen references absent event")
        # An absent judgment must not be normalized into a reviewed new event.
        # With no catalog there is nothing to compare; legacy empty-catalog
        # responses remain readable without manufacturing a review reason.
        if event_ids:
            reason = candidate.get("event_reason")
            if (
                "event_id" not in candidate
                or "comparison_event_ids" not in candidate
                or not isinstance(reason, str)
                or not reason.strip()
                or len(reason) > 400
            ):
                raise ValueError(
                    "screen requires explicit event_id, comparison_event_ids and event_reason (1-400 chars)"
                )
        elif "event_reason" in candidate and (
            not isinstance(candidate["event_reason"], str)
            or not candidate["event_reason"].strip()
            or len(candidate["event_reason"]) > 400
        ):
            raise ValueError("screen event_reason must be 1-400 chars")
        # References never attach the candidate. Reject explicit false/null,
        # containers, invented IDs and duplicate references instead of coercing.
        comparison = candidate.get("comparison_event_ids", [])
        if (
            not isinstance(comparison, list)
            or len(comparison) > COMPARISON_BOUND
            or any(
                not isinstance(key, str) or key not in event_ids for key in comparison
            )
        ):
            raise ValueError("screen comparison_event_ids references absent event")
        if len(comparison) != len(set(comparison)) or event_id in comparison:
            raise ValueError(
                "screen comparison_event_ids must be distinct from each other and event_id"
            )
        covered.update(refs)
    for item in ignored:
        key = item.get("evidence_id") if isinstance(item, dict) else None
        if not isinstance(key, str) or key not in ids:
            raise ValueError("invalid ignored input")
        if key in dismissed:
            raise ValueError("screen classified input twice")
        event_id = item.get("event_id")
        if event_id is not None and (
            not isinstance(event_id, str) or event_id not in event_ids
        ):
            raise ValueError("screen ignored references absent event")
        if key in covered:
            # Candidate precedence only; two ignored entries for one input are a contract error.
            report.setdefault("normalizations", []).append(
                {
                    "evidence_id": key,
                    "reason": "candidate takes priority over redundant ignored entry",
                }
            )
            continue
        reason = item.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            raise ValueError("ignored input requires reason")
        dismissed.add(key)
        covered.add(key)
    return candidates, ignored, covered


def source_time_label(item):
    """News uses publication time; disclosures show only their collection time."""
    if item.get("source_kind") == "DISCLOSURE":
        try:
            return "수집: " + timestamp(item.get("observed_at")).astimezone(
                KST
            ).strftime("%Y-%m-%d %H:%M")
        except (ValueError, TypeError, AttributeError):
            return "수집 시각 미확인"
    label = "기사 게시" if item.get("source_kind") == "NEWS" else "자료 게시"
    published = item.get("published_at")
    try:
        displayed = timestamp(published).astimezone(KST).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError, AttributeError):
        displayed = None
    if displayed:
        suffix = (
            " (시간대 미확인)"
            if item.get("time_interpretation") == "ASSUMED_KST"
            else ""
        )
        return label + ": " + displayed + suffix
    date = str(item.get("publication_date") or published or "")
    try:
        day = datetime.strptime(
            date, "%Y%m%d" if re.fullmatch(r"\d{8}", date) else "%Y-%m-%d"
        ).strftime("%Y-%m-%d")
    except ValueError:
        day = None
    text = label + ": " + day + " (게시 시각 미제공)" if day else label + " 시각 미확인"
    for field in ("first_seen_at", "observed_at"):
        try:
            collected = (
                timestamp(item.get(field)).astimezone(KST).strftime("%Y-%m-%d %H:%M")
            )
        except (ValueError, TypeError, AttributeError):
            continue
        return text + " · 수집: " + collected
    return text


def render_alert(decision, evidence, now, *, followup=False, market_lines=()):
    labels = {
        "MORNING_RELEASE": "아침 전달",
        "EVENING_RELEASE": "저녁 전달",
        "PERIODIC_RECHECK": "후속 점검",
        "NEW_EVIDENCE": "새 정보",
        "SENT": "발송 확인",
        "PENDING": "전달 대기",
        "QUEUED": "전달 대기",
    }
    decision = {
        key: re.sub(
            r"\b(?:MORNING_RELEASE|EVENING_RELEASE|PERIODIC_RECHECK|NEW_EVIDENCE|SENT|PENDING|QUEUED)\b",
            lambda match: labels[match[0]],
            value,
        )
        if isinstance(value, str)
        else value
        for key, value in decision.items()
    }
    label = {
        "OFFICIAL": "공식 자료 확인",
        "REPORTED": "보도 확인",
        "UNVERIFIED": "미확인 보도·추가 확인 중",
    }[decision["confirmation"]]
    heading = (
        "정정"
        if decision["change"] == "CORRECTION"
        else "후속"
        if followup
        else "중요 뉴스"
    )
    title = decision["title"]
    notice = decision.get("correction_notice")
    correction = decision["change"] == "CORRECTION"
    brief_correction = (
        correction
        and decision["urgency"] != "IMMEDIATE"
        and notice in ("EXPLANATION_ONLY", "SOURCE_ONLY")
    )
    if correction:
        title = (
            re.sub(
                r"^(?:(?:\[(?:설명 |원문 )?정정\]|(?:설명 |원문 )?정정[:：])\s*)+",
                "",
                title,
            ).strip()
            or title
        )
        if notice == "EXPLANATION_ONLY":
            heading = "설명 정정"
        elif notice == "SOURCE_ONLY":
            heading = "원문 정정"
    # Keep the full analysis internally for follow-up and on-demand detail.
    # Ordinary discovery with no personal implication gets a headline/link;
    # local corrections show the corrected claim and its limited implication.
    # Material, uncertain, legacy and wake-worthy corrections retain detail.
    if brief_correction:
        parts = [f"[{heading}] {title}", decision["facts"], decision["interpretation"]]
    elif headline_only(decision):
        parts = [f"[뉴스 한줄] {decision['title']}", label]
    else:
        parts = [
            f"[{heading}] {title}",
            label,
            decision["facts"],
            "의미: " + decision["interpretation"],
        ]
        if followup and decision["why_now"] != decision["interpretation"]:
            parts.append("달라진 점: " + decision["why_now"])
        if decision["portfolio_note"]:
            parts.append("내 상황: " + decision["portfolio_note"])
        if market_lines:
            parts.append("\n".join(market_lines))
        parts.append("다음 확인: " + decision["next_check"])
    by_id = {item["id"]: item for item in evidence}
    for key in decision["evidence_ids"][:4]:
        item = by_id[key]
        publisher = str(item.get("publisher") or item.get("source_kind"))
        if re.fullmatch(r"[a-f0-9]{32,64}", publisher):
            url = str(item.get("url") or "")
            publisher = next(
                (
                    name
                    for domain, name in (
                        ("yna.co.kr", "연합뉴스"),
                        ("jtbc.co.kr", "JTBC"),
                        ("bbc.co", "BBC"),
                    )
                    if domain in url
                ),
                item.get("source_kind", "뉴스"),
            )
        parts.append(
            "출처: "
            + publisher
            + " · "
            + source_time_label(item)
            + "\n"
            + str(item.get("url") or key)
        )
    return "\n\n".join(parts)


class NewsDecisionEngine:
    def __init__(
        self,
        store,
        screen,
        analyze,
        *,
        clock,
        context_reader=None,
        token_budget=None,
        on_urgent=None,
        document_reader=None,
        sec_document_reader=None,
        context_mode="compact",
        headline_digest_hour=None,
        reuse_unchanged_reviews=False,
        scoped_review_reuse=False,
        diagnostics=None,
        event_detail_enabled=False,
        typed_review_enabled=False,
        model_wait_enabled=False,
        first_pass=None,
        confirm_enabled=True,
    ):
        """``clock`` returns the current aware instant and is read right before every model call;
        ``RECORDED_TIME`` is the explicit replay mode where each call uses the tick's recorded now.
        ``first_pass`` (optional) answers every analysis first under stage ``analyze_first``; the
        primary ``analyze`` model is then asked only when ``needs_confirmation`` says so and
        ``confirm_enabled`` is true (off: the first pass settles everything, e.g. a primary outage)."""
        self.store, self.screen, self.analyze = store, screen, analyze
        self.first_pass, self.confirm_enabled = first_pass, confirm_enabled is not False
        self._last_tier = None
        self.diagnostics = diagnostics
        self.event_detail_enabled = event_detail_enabled is True
        self.typed_review_enabled = typed_review_enabled is True
        from tokmo.programs.news_model_wait import ModelWait

        self.model_wait = ModelWait(store) if model_wait_enabled is True else None
        if self.model_wait is None and any(
            hasattr(model, "available") for model in (screen, analyze, first_pass)
        ):
            # A chain holds inputs while all its members wait; only ModelWait resumes held inputs.
            raise ValueError("a model chain requires model_wait_enabled=True")
        self._usage_comparison_context = None
        self.context_mode = context_mode
        self.scoped_review_reuse = scoped_review_reuse is True
        self.reuse_unchanged_reviews = reuse_unchanged_reviews is True
        if headline_digest_hour is not None and (
            type(headline_digest_hour) is not int or not 8 <= headline_digest_hour < 22
        ):
            raise ValueError("headline_digest_hour must be 8..21 or null")
        self.headline_digest_hour = headline_digest_hour
        if context_mode not in {"compact", "full", "efficient", "incremental"}:
            raise ValueError(
                "context_mode must be compact, full, efficient or incremental"
            )
        self.clock = clock
        self.context_reader = context_reader or (lambda now: {})
        self.token_budget = token_budget
        self.on_urgent = on_urgent
        self.on_morning = None
        self.document_reader = document_reader
        self.sec_document_reader = sec_document_reader
        self._document_reads = (
            0  # One source request per worker pass, including failures.
        )
        self._tick_now = None

    def _now(self):
        return (
            self._tick_now if self.clock == RECORDED_TIME else timestamp(self.clock())
        )

    def _call(
        self,
        stage,
        request,
        report,
        *,
        context=None,
        followup_scope=None,
        portfolio_basis=None,
        detail_event=None,
    ):
        """One model call at one captured instant: the clock is read once here, the request's
        ``context`` (when a builder is given) is built and validated for that instant, and the same
        instant is ``request.now`` and the call row's ``started_at``. Hash, reservation and the
        callback all see that finished request; it is never edited afterwards. Every correction is
        a new call and therefore captures its own fresh instant and context.
        With a first-pass model, an analysis runs there first (stage ``analyze_first``) and the
        primary model sees the identical finished request only when confirmation is owed; a
        correction of a primary answer goes back to the primary.
        Returns (response, instant, request as sent)."""
        at = self._now()
        if context is not None:
            request = dict(request, context=context(at))
        if stage == "analyze" and self.context_mode in {"efficient", "incremental"}:
            from tokmo.programs.news_context_efficiency import (
                compact_context,
                compress_prior_context,
                scoped_context,
            )
            from tokmo.programs.news_context_efficiency import (
                followup_scope as checked_scope,
            )
            from tokmo.programs.news_context_efficiency import portfolio_basis as basis

            scope = checked_scope(followup_scope, request.get("context", {}))
            # New holdings/choices or stale status require a fresh broad view.
            if portfolio_basis is not None and portfolio_basis != basis(
                self._display_context
            ):
                scope = checked_scope(None, {})
            current = compact_context(scoped_context(request.get("context", {}), scope))
            if detail_event:
                from tokmo.programs.news_incremental_review import event_context

                current = event_context(detail_event, current)
            request = dict(request, context=current, followup_scope=scope)
            views = []
            for prior in request.get("events", []):
                snapshot = prior.get("context_snapshot")
                if isinstance(snapshot, dict):
                    if self.context_mode == "incremental":
                        from tokmo.programs.news_incremental_review import (
                            event_context,
                            prior_delta,
                        )

                        historical = compact_context(scoped_context(snapshot, scope))
                        if detail_event:
                            historical = event_context(detail_event, historical)
                        view = prior_delta(historical, current)
                    else:
                        view = compress_prior_context(
                            compact_context(scoped_context(snapshot, scope)), current
                        )
                    prior = dict(prior, context_snapshot=view)
                views.append(prior)
            request = dict(request, events=views)
        if self.context_mode == "incremental":
            request = dict(request, incremental_review=True)
        if stage == "analyze" and self.typed_review_enabled:
            request = dict(request, typed_review_enabled=True)
        request = dict(request, now=kst(at), **TIME_BASIS)
        if stage == "analyze" and self.first_pass is not None:
            if request.get("correction") and self._last_tier == "primary":
                return (
                    self._invoke("analyze", self.analyze, request, report, at),
                    at,
                    request,
                )
            first = self._invoke("analyze_first", self.first_pass, request, report, at)
            self._last_tier = "first"
            reason = needs_confirmation(first)
            if not (self.confirm_enabled and reason):
                report["first_pass_settled"] = report.get("first_pass_settled", 0) + 1
                return first, at, request
            report.setdefault("confirmations", []).append(
                dict(
                    reason=reason,
                    evidence_ids=[row["id"] for row in request.get("evidence", [])],
                )
            )
            confirmed = self._invoke("analyze", self.analyze, request, report, at)
            self._last_tier = "primary"
            return confirmed, at, request
        callback = self.screen if stage == "screen" else self.analyze
        return self._invoke(stage, callback, request, report, at), at, request

    def _invoke(self, stage, callback, request, report, at):
        """One journaled model call over a finished request: outage check, reservation and budget,
        the call row keyed by stage/request/model, then the callback. Returns the response."""
        if self.model_wait:
            self.model_wait.check(callback, at)
        if hasattr(callback, "available") and not callback.available(at):
            # Every chain member is still inside its own wait: hold the work. No journal row,
            # no reservation and no ModelWait attempt, because no physical attempt happens.
            from tokmo.programs.news_model_wait import ModelDeferred

            raise ModelDeferred(callback.hold_state(at))
        reservation = len(encoded(request).encode()) + getattr(
            callback, "max_output_tokens", 0
        )
        call_identity = {
            "stage": stage,
            "request": request,
            "model": getattr(callback, "model", "recorded"),
        }
        if getattr(callback, "reasoning_effort", None) is not None:
            call_identity["reasoning_effort"] = callback.reasoning_effort
        key = identity(call_identity)
        previous = self.store.begin_call(
            key, stage, request, at, reservation, self.token_budget
        )
        if previous is not None:
            if self.diagnostics:
                self.diagnostics.safe(
                    "emit",
                    "call_reused",
                    call_id=key,
                    evaluation_id=self.diagnostics.evaluation_id,
                )
            return previous
        report["calls"] += 1
        try:
            from tokmo.programs.news_usage_diagnostics import observe_call

            if hasattr(callback, "attempt_sink"):
                # A chain journals every failed member attempt under this call's id: the attempt keeps
                # its reservation in the day's budget and the next member is admitted only within it.
                callback.attempt_sink = lambda member, category, when: (
                    self.store.record_failed_attempt(
                        key,
                        stage,
                        member.model,
                        when,
                        reservation,
                        category,
                        self.token_budget,
                    )
                )
            with observe_call(self.diagnostics, key, stage, request, kst(at)):
                result = callback(request)
            if not isinstance(result, dict):
                raise ValueError("model response must be an object")
            self.store.finish_call(key, response=result)
            if self.model_wait:
                self.model_wait.succeeded(callback)
            if self.diagnostics:
                self.diagnostics.safe("call_finished", key, result)
            report.setdefault("model_calls", []).append(
                {
                    "stage": stage,
                    "model": result.get("_model", "recorded"),
                    "reasoning_effort": result.get(
                        "_reasoning_effort", getattr(callback, "reasoning_effort", None)
                    ),
                    "usage": result.get("_usage", {}),
                    "elapsed_seconds": result.get("_elapsed_seconds"),
                }
            )
            return result
        except Exception as exc:
            self.store.finish_call(key, error=type(exc).__name__)
            if self.diagnostics:
                self.diagnostics.safe(
                    "emit",
                    "call_failed",
                    call_id=key,
                    evaluation_id=self.diagnostics.evaluation_id,
                    error_type=type(exc).__name__,
                )
            if self.model_wait:
                state = self.model_wait.failed(callback, exc, self._now(), key)
                if state:
                    from tokmo.programs.news_model_wait import ModelDeferred

                    raise ModelDeferred(state) from exc
            raise
        finally:
            if hasattr(callback, "attempt_sink"):
                callback.attempt_sink = None

    def _context(self, now):
        context = self.context_reader(now)
        if not isinstance(context, dict):
            raise ValueError("context must be an object")
        if context.get("observed_at") and timestamp(context["observed_at"]) > timestamp(
            now
        ):
            raise ValueError("future portfolio context")
        self._display_context = context
        context = {k: v for k, v in context.items() if not k.startswith("_")}
        if self.context_mode != "full":
            from tokmo.programs.news_context_memory import compact_portfolio

            context = compact_portfolio(context)
        return (
            dict(context, observed_at=kst_field(context["observed_at"]))
            if context.get("observed_at")
            else context
        )

    def tick(self, observations, now: datetime):
        # ``now`` is the run snapshot: collection cutoff, future-observation guard, due checks.
        # Each actual model call reads the clock itself (see _call).
        now = self._tick_now = timestamp(now)
        report = {"decisions": [], "alerts": [], "failures": [], "calls": 0}
        from tokmo.programs.news_model_wait import ModelDeferred

        self.store.ingest(observations, now)
        processed_events = set()
        morning_events = [
            event
            for event in self.store.events()
            if event.get("held_until")
            and timestamp(event["held_until"]) <= now
            and event.get("held_kind") != "EVENING_HEADLINES"
        ]
        self._review_due(morning_events, now, report, processed_events)
        self._finish_morning(report)
        # Also publish durable successful constituents left by an interrupted pass.
        if self.on_morning:
            for alert in self.pending_alerts():
                if alert.get("kind") == "MORNING_DIGEST":
                    self.on_morning(alert)
        processed_events.update(event["id"] for event in morning_events)
        batch = self.store.pending()
        if batch:
            ids = {item["id"] for item in batch}
            events = self.store.events()
            request = self._screen_request(batch, events)
            if self.diagnostics:
                self.diagnostics.safe("evaluation", "screen")
            event_ids = {event["event_id"] for event in request["events"]}
            try:
                screened, _, _ = self._call(
                    "screen", request, report, context=self._context
                )
                try:
                    candidates, ignored, covered = validate_screen(
                        screened, ids, event_ids, report
                    )
                except ValueError as exc:
                    # One explicit contract correction by the same model for this batch.
                    # Transport failures raise inside _call and never reach here. The
                    # correction must cover every input itself: it replaces the omission
                    # repair below, so the extra screen call per batch is at most one.
                    correction = {
                        "stage": "screen",
                        "evidence_ids": sorted(ids),
                        "reason": str(exc)[:200],
                        "status": "FAILED",
                    }
                    report.setdefault("corrections", []).append(correction)
                    corrected, _, _ = self._call(
                        "screen",
                        dict(
                            request,
                            correction={
                                "reason": correction["reason"],
                                "allowed_evidence_ids": sorted(ids),
                                "allowed_event_ids": sorted(event_ids),
                                "response_shape": {
                                    "candidates": [
                                        {
                                            "evidence_ids": [
                                                "copy an allowed_evidence_id exactly"
                                            ],
                                            "event_id": None,
                                            "comparison_event_ids": [],
                                            "event_reason": "사건 소속/비교 필요 또는 관련 없음의 근거",
                                        }
                                    ],
                                    "ignored": [
                                        {
                                            "evidence_id": "copy an allowed_evidence_id exactly",
                                            "reason": "제외 이유",
                                            "event_id": None,
                                        }
                                    ],
                                },
                                "previous_response": {
                                    key: value
                                    for key, value in screened.items()
                                    if not key.startswith("_")
                                },
                                "instruction": "누락된 사건 비교는 events의 제목·phase·출처 시각과 원문을 대조해 판단하라. 같은 사건은 event_id, 별개 사건 비교는 comparison_event_ids에 지정하고 관련 없으면 null/[]와 그 근거를 event_reason에 적어라. 근거 없는 기존 연결은 수정하라. allowed_evidence_ids의 문자열을 접수번호·해시까지 그대로 복사하라. 서로 다른 id의 일부를 조합하지 말라. ignored는 문자열 배열이 아니라 evidence_id와 reason을 가진 객체 배열이다(id 키 사용 금지). 모든 입력을 정확히 한 번 분류하고 candidates와 ignored 두 배열을 모두 출력하라.",
                            },
                        ),
                        report,
                        context=self._context,
                    )
                    candidates, ignored, covered = validate_screen(
                        corrected, ids, event_ids, report
                    )
                    if covered != ids:
                        raise ValueError("screen correction omitted input")
                    correction["status"] = "OK"
                missing = ids - covered
                if missing:
                    # One bounded repair of omitted inputs; valid results do
                    # not disappear because another article was omitted.
                    try:
                        repair_request = dict(
                            self._screen_request(
                                [item for item in batch if item["id"] in missing],
                                events,
                            ),
                            repair="Classify every supplied id exactly once; prior batch omitted these inputs.",
                        )
                        repair, _, _ = self._call(
                            "screen", repair_request, report, context=self._context
                        )
                        more_candidates, more_ignored, repaired = validate_screen(
                            repair,
                            missing,
                            {event["event_id"] for event in repair_request["events"]},
                            report,
                        )
                        if repaired != missing:
                            raise ValueError("screen repair omitted or invented input")
                        candidates, ignored, covered = (
                            candidates + more_candidates,
                            ignored + more_ignored,
                            covered | repaired,
                        )
                    except ModelDeferred:
                        raise  # Preserve all inputs; do not turn transport waiting into contract errors.
                    except Exception as exc:
                        self.store.mark(missing, "ERROR", str(exc)[:200])
                        report["failures"].append(
                            {
                                "stage": "screen_repair",
                                "evidence_ids": sorted(missing),
                                "reason": str(exc)[:200],
                            }
                        )
                        ids -= missing
                if covered != ids:
                    raise ValueError("screen silently omitted input")
                if self.context_mode in {"efficient", "incremental"}:
                    detailed = {
                        e["event_id"]
                        for e in request["events"]
                        if e.get("detail_included")
                    }
                    # A short catalog entry is enough to nominate an event,
                    # not to establish that its full story was already covered.
                    retained = []
                    for row in ignored:
                        if row.get("event_id") and row["event_id"] not in detailed:
                            candidates.append(
                                dict(
                                    event_id=row["event_id"],
                                    evidence_ids=[row["evidence_id"]],
                                )
                            )
                            report.setdefault("detail_escalations", []).append(
                                row["evidence_id"]
                            )
                        else:
                            retained.append(row)
                    ignored = retained
            except ModelDeferred as exc:
                report.setdefault("model_deferred", []).append(
                    dict(stage="screen", **exc.state)
                )
            except Exception as exc:
                self.store.mark(ids, "ERROR", str(exc)[:200])
                report["failures"].append({"stage": "screen", "reason": str(exc)[:200]})
                if self.diagnostics:
                    self.diagnostics.safe(
                        "emit",
                        "evaluation_finished",
                        evaluation_id=self.diagnostics.evaluation_id,
                        task="screen",
                        accepted=False,
                        error_type=type(exc).__name__,
                    )
            else:
                # Same-event candidates of one batch are one analysis over their merged inputs; unattached
                # candidates stay separate (the screen decided they are different events).
                if self.diagnostics:
                    self.diagnostics.safe(
                        "emit",
                        "evaluation_finished",
                        evaluation_id=self.diagnostics.evaluation_id,
                        task="screen",
                        accepted=True,
                        candidate_count=len(candidates),
                        ignored_count=len(ignored),
                    )
                merged = {}
                for index, candidate in enumerate(candidates):
                    row = merged.setdefault(
                        candidate.get("event_id") or index,
                        {
                            "event_id": candidate.get("event_id"),
                            "evidence_ids": [],
                            "comparison_event_ids": [],
                        },
                    )
                    row["evidence_ids"] += candidate["evidence_ids"]
                    row["comparison_event_ids"] += (
                        candidate.get("comparison_event_ids") or []
                    )
                for candidate in merged.values():
                    selected = [
                        item
                        for item in batch
                        if item["id"] in candidate["evidence_ids"]
                    ]
                    previous = next(
                        (
                            event
                            for event in self.store.events()
                            if event["id"] == candidate["event_id"]
                        ),
                        None,
                    )
                    try:
                        event = self._analyze(
                            selected,
                            previous,
                            now,
                            report,
                            comparison_ids=list(
                                dict.fromkeys(candidate["comparison_event_ids"])
                            ),
                        )
                        processed_events.add(event["id"])
                    except ModelDeferred as exc:
                        self.store.defer_candidate(candidate, self._now())
                        report.setdefault("model_deferred", []).append(
                            dict(stage="analyze", **exc.state)
                        )
                    except Exception as exc:
                        self.store.mark(
                            candidate["evidence_ids"], "ERROR", str(exc)[:200]
                        )
                        report["failures"].append(
                            {
                                "stage": "analyze",
                                "evidence_ids": candidate["evidence_ids"],
                                "reason": str(exc)[:200],
                            }
                        )
                    else:
                        self.store.mark(candidate["evidence_ids"], "DONE")
                candidate_ids = {
                    key for item in candidates for key in item["evidence_ids"]
                }
                # Low-value observations still belong to a known event. This
                # records facts without a new analysis or a shifted send anchor.
                for row in ignored:
                    if row["evidence_id"] in candidate_ids:
                        continue
                    event = next(
                        (
                            e
                            for e in self.store.events()
                            if e["id"] == row.get("event_id")
                        ),
                        None,
                    )
                    if event is None:
                        continue
                    observations = {e["id"]: e for e in event.get("evidence", [])}
                    observations.update(
                        {e["id"]: e for e in batch if e["id"] == row["evidence_id"]}
                    )
                    event["evidence"] = list(observations.values())
                    from tokmo.programs.news_context_memory import numeric_progress

                    event["context_observations"] = {
                        "observed_through": kst(
                            max(timestamp(e["observed_at"]) for e in event["evidence"])
                        ),
                        "observations": len(event["evidence"]),
                        "numeric_progress": numeric_progress(
                            event["evidence"],
                            baseline_ids=set(
                                (event.get("last_sent") or {}).get("evidence_ids") or []
                            ),
                            at=now,
                        ),
                    }
                    self.store.save_event(event)
                    report.setdefault("context_only_inputs", []).append(
                        row["evidence_id"]
                    )
                self.store.mark(ids - candidate_ids, "DONE")
        if self.model_wait:
            # New arrivals get the first opportunity; stored classification is
            # reused after a restart, with current account context and send history.
            report["closed_deferred_duplicates"] = self.store.close_duplicate_deferred(
                self._now()
            )
            for task in self.store.deferred_candidates():
                receipt = self.store.control("accepted_inputs:" + task["deferred_id"])
                if receipt and receipt.get("input_ids") == sorted(task["evidence_ids"]):
                    # Assessment and receipt commit atomically. A crash before
                    # WAITING->DONE must not run the same analysis again.
                    self.store.finish_deferred(task, "DONE")
                    processed_events.add(receipt["event_id"])
                    continue
                try:
                    self.model_wait.check(self.analyze, self._now())
                except ModelDeferred as exc:
                    report.setdefault("model_deferred", []).append(
                        dict(stage="resume", **exc.state)
                    )
                    break
                selected = [self.store.observation(key) for key in task["evidence_ids"]]
                previous = next(
                    (e for e in self.store.events() if e["id"] == task.get("event_id")),
                    None,
                )
                try:
                    if any(row is None for row in selected):
                        raise ValueError("deferred input is missing")
                    # Classification may predate newer assessments/sends. Reuse
                    # the local catalog retrieval (no GLM call), so resumption
                    # can compare those developments rather than replay an old
                    # candidate as an isolated new story.
                    catalog = self._screen_request(selected, self.store.events())
                    comparisons = list(
                        dict.fromkeys(
                            task.get("comparison_event_ids", [])
                            + [
                                r["event_id"]
                                for r in catalog["events"]
                                if r.get("detail_included", True)
                            ]
                        )
                    )
                    event = self._analyze(
                        selected,
                        previous,
                        now,
                        report,
                        comparison_ids=comparisons,
                        deferred_at=task["deferred_at"],
                    )
                    processed_events.add(event["id"])
                except ModelDeferred as exc:
                    report.setdefault("model_deferred", []).append(
                        dict(stage="resume", **exc.state)
                    )
                    break
                except Exception as exc:
                    self.store.finish_deferred(task, "ERROR", str(exc)[:200])
                    report["failures"].append(
                        dict(
                            stage="analyze",
                            evidence_ids=task["evidence_ids"],
                            reason=str(exc)[:200],
                        )
                    )
                else:
                    self.store.finish_deferred(task, "DONE")
        self._review_due(self.store.events(), now, report, processed_events)
        self._finish_morning(report)
        self._finish_evening(now, report)
        report["input_states"] = self.store.counts()
        return report

    def _review_due(self, events, now, report, processed_events):
        from tokmo.programs.news_model_wait import ModelDeferred

        # Reassess held content at release, not merely forward last night's text.
        for event in events:
            if event["id"] in processed_events:
                continue
            if event.get("review_error") and self.typed_review_enabled:
                self._retire_unchanged_failure(event, report)
            if self._retire_stale_recheck(event, now, report):
                continue
            due = event.get("held_until") or event.get("next_check_at")
            if not due or timestamp(due) > now or event.get("review_error"):
                continue
            if (
                event.get("held_kind") == "EVENING_HEADLINES"
                and self.headline_digest_hour is not None
                and not self._evening_open(now)
            ):
                event["held_until"] = kst(
                    evening_slot(now, self.headline_digest_hour, next_slot=True)
                )
                self.store.save_event(event)
                continue
            try:
                if self._reuse_review(event, now, report):
                    continue
                self._analyze([], event, now, report)
            except ModelDeferred as exc:
                report.setdefault("model_deferred", []).append(
                    dict(stage="recheck", **exc.state)
                )
                break  # All remaining due events remain due, not individually failed.
            except Exception as exc:
                reason = (
                    "news_recheck_invalid"
                    if isinstance(exc, ValueError)
                    else "news_recheck_failed"
                )
                event["review_error"] = reason
                self.store.save_event(event)
                report["failures"].append(
                    {"stage": "recheck", "event_id": event["id"], "reason": reason}
                )

    def _finish_morning(self, report):
        # Include durable successes from an interrupted reassessment, grouped by day.
        events, groups = self.store.events(), {}
        for event in events:
            for item in event.get("pending_alerts", []):
                if item.get("morning"):
                    day = timestamp(item["created_at"]).astimezone(KST).date()
                    groups.setdefault(day, {})[item["id"]] = item
        report["alerts"] = [
            item for item in report["alerts"] if not item.get("morning")
        ]
        for group in groups.values():
            morning_alerts = list(group.values())
            digest = digest_alerts(morning_alerts)
            report["alerts"].append(digest)
            # Each event refers to the same durable digest. Flattening dedupes
            # by id, so a restart cannot turn the digest into separate sends.
            changed = []
            for event in events:
                if event["id"] in digest["event_ids"]:
                    event["pending_alerts"] = [
                        item
                        for item in event.get("pending_alerts", [])
                        if item["id"] not in digest["constituent_ids"]
                    ]
                    event["pending_alerts"].append(digest)
                    event["last_alert"] = digest
                    changed.append(event)
            self.store.save_events(changed)

    def _evening_open(self, at):
        hour = self.headline_digest_hour
        if hour is None or not hour <= timestamp(at).astimezone(KST).hour < 22:
            return False
        prepared = self.store.cursor("evening_digest_prepared", None)
        return prepared is None or timestamp(prepared) < timestamp(
            evening_slot(at, hour)
        )

    def _finish_evening(self, now, report):
        if not self._evening_open(now):
            return
        events = self.store.events()
        alerts = list(
            {
                a["id"]: a
                for e in events
                for a in e.get("pending_alerts", [])
                if a.get("evening_slot")
            }.values()
        )
        if alerts:
            digest = evening_digest_alerts(alerts)
            ids = set(digest["constituent_ids"])
            report["alerts"] = [a for a in report["alerts"] if a["id"] not in ids] + [
                digest
            ]
            changed = []
            for event in events:
                if any(a["id"] in ids for a in event.get("pending_alerts", [])):
                    event["pending_alerts"] = [
                        a for a in event["pending_alerts"] if a["id"] not in ids
                    ] + [digest]
                    if event.get("last_alert", {}).get("id") in ids:
                        event["last_alert"] = digest
                    changed.append(event)
        else:
            changed = []
        # The batch and daily marker commit together: restart cannot release it twice.
        self.store.save_events(
            changed,
            cursor=(
                "evening_digest_prepared",
                evening_slot(now, self.headline_digest_hour),
            ),
        )

    def _review_fingerprint(self, event):
        from tokmo.programs.news_context_efficiency import _digest

        return _digest(self._review_basis(event))

    def _review_basis(self, event):
        from tokmo.programs.news_context_efficiency import review_basis

        related = [
            e
            for e in self.store.events()
            if e["id"] in event.get("review_context_ids", [])
        ]
        policy = [
            getattr(self.analyze, "policy_fingerprint", None),
            getattr(self.analyze, "model", "recorded"),
            getattr(self.analyze, "reasoning_effort", None),
            self.context_mode,
            self.headline_digest_hour,
        ]
        if self.scoped_review_reuse:
            policy.append("scoped-review-reuse-v1")
        if self.context_mode == "incremental":
            policy.append(getattr(self.analyze, "incremental_policy_fingerprint", None))
        if event.get("review_efficiency_contract") == 1:
            # Adopt only after a normal accepted analysis. Installing the new
            # auxiliary format alone must not invalidate every legacy event.
            policy.append(getattr(self.analyze, "review_efficiency_fingerprint", None))
        context = getattr(self, "_display_context", {})
        basis = review_basis(event, context, policy, related)
        if self.scoped_review_reuse:
            from tokmo.programs.news_context_efficiency import _digest
            from tokmo.programs.news_incremental_review import dependency_context

            basis["PORTFOLIO_CHANGED"] = _digest(dependency_context(event, context))
        if self.context_mode == "incremental":
            from tokmo.programs.news_context_efficiency import _digest
            from tokmo.programs.news_incremental_review import market_basis

            basis["MARKET_CHANGED"] = _digest(market_basis(context, event))
        return basis

    def _reuse_miss(self, event, report, *reasons):
        at = kst(self._now())
        if self.diagnostics:
            self.diagnostics.safe(
                "gate",
                event,
                at=at,
                outcome="REASSESS",
                reasons=reasons,
                context=self._usage_comparison_context,
                scoped=self.scoped_review_reuse,
            )
        counts = event.setdefault("review_reuse_misses", {})
        for reason in reasons:
            counts[reason] = counts.get(reason, 0) + 1
        event["last_review_reuse_miss"] = dict(at=at, reasons=list(reasons))
        self.store.save_event(event)
        report.setdefault("review_reuse_misses", []).append(
            dict(event_id=event["id"], at=at, reasons=list(reasons))
        )
        return False

    def _retire_stale_recheck(self, event, now, report):
        """A periodic recheck (failed or simply never run) whose due time is more than
        ``STALE_RECHECK_HOURS`` old is retired without a model call: nothing new reached the event
        meanwhile, so the check would only re-read old evidence. New evidence attached by the
        screen still analyzes the event. Held or pending proposals are never touched."""
        due = event.get("next_check_at")
        if (
            not due
            or timestamp(due) + timedelta(hours=STALE_RECHECK_HOURS) > now
            or event.get("held_until")
            or event.get("recovery_review_at")
            or event.get("pending_alerts")
            or self._pending_bodies(event)
        ):
            return False
        event.setdefault("retired_review_failures", []).append(
            dict(
                reason="STALE_RECHECK",
                error=event.get("review_error"),
                closed_at=kst(now),
                judged_at=event.get("updated_at"),
                due_at=due,
            )
        )
        event["review_error"] = None
        event["next_check_at"] = None
        event.pop("review_reuse_plan", None)
        self.store.save_event(event)
        report.setdefault("retired_stale_rechecks", []).append(event["id"])
        return True

    def _retire_unchanged_failure(self, event, report):
        """Close only a proven redundant attempt, never a legacy event deadline."""
        plan = event.get("review_reuse_plan") or {}
        if (
            plan.get("version") != 1
            or plan.get("kind") != "NEW_FACTS"
            or event.get("held_until")
            or event.get("recovery_review_at")
            or self._pending_bodies(event)
            or event.get("source_enrichment")
        ):
            return
        at = self._now()
        try:
            bound = timestamp(plan["safety_at"])
            maximum = timedelta(
                hours=24 if event["decision"]["importance"] == "HIGH" else 72
            )
            if not at < bound <= timestamp(plan["judged_at"]) + maximum:
                return
        except (ValueError, TypeError, KeyError, AttributeError):
            return
        self._context(at)
        if plan.get("basis") != self._review_basis(event):
            return
        event.setdefault("retired_review_failures", []).append(
            dict(
                reason="UNCHANGED_WAIT_FOR_NEW_FACTS",
                error=event["review_error"],
                closed_at=kst(at),
                judged_at=event["updated_at"],
            )
        )
        event["review_error"] = None
        event["next_check_at"] = kst(at)
        self.store.save_event(event)
        report.setdefault("retired_review_failures", []).append(event["id"])

    def _reuse_review(self, event, now, report):
        self._usage_comparison_context = None
        plan = event.get("review_reuse_plan")
        if not self.reuse_unchanged_reviews:
            return False
        if (
            event.get("held_until")
            or event.get("recovery_review_at")
            or self._pending_bodies(event)
        ):
            return self._reuse_miss(event, report, "DELIVERY_OR_RECOVERY_PENDING")
        if event.get("review_error"):
            return self._reuse_miss(event, report, "REVIEW_ERROR")
        if event.get("source_enrichment", {}).get("status") in {
            "READER_UNAVAILABLE",
            "PASS_LIMIT",
            "STARTED",
        }:
            return self._reuse_miss(event, report, "SOURCE_PENDING")
        if not plan:
            return self._reuse_miss(event, report, "NO_PLAN")
        try:
            until = timestamp(plan["after"])
        except (ValueError, TypeError, KeyError, AttributeError):
            return self._reuse_miss(event, report, "INVALID_PLAN")
        at = self._now()
        if (
            self.typed_review_enabled
            and plan.get("version") == 1
            and plan.get("kind") == "NEW_FACTS"
        ):
            try:
                until = timestamp(plan["safety_at"])
                maximum = timedelta(
                    hours=24 if event["decision"]["importance"] == "HIGH" else 72
                )
                if (
                    not timestamp(plan["judged_at"])
                    < until
                    <= timestamp(plan["judged_at"]) + maximum
                ):
                    raise ValueError("invalid safety bound")
            except (ValueError, TypeError, KeyError, AttributeError):
                return self._reuse_miss(event, report, "INVALID_PLAN")
        if at >= until:
            return self._reuse_miss(event, report, "DEADLINE_REACHED")
        self._context(
            at
        )  # Fresh read, including stale status and actual held quantities.
        basis = self._review_basis(event)
        if self.diagnostics:
            self._usage_comparison_context = self._display_context
        if not isinstance(plan.get("basis"), dict):
            return self._reuse_miss(event, report, "LEGACY_PLAN")
        changed = [
            key for key, value in basis.items() if value != plan["basis"].get(key)
        ]
        if changed:
            return self._reuse_miss(event, report, *changed)
        interval = timedelta(
            hours=6 if event["decision"]["importance"] == "HIGH" else 24
        )
        event["next_check_at"] = kst(min(at + interval, until))
        event["review_reuse_count"] = event.get("review_reuse_count", 0) + 1
        event["last_review_reuse_at"] = kst(at)
        if self.diagnostics:
            self.diagnostics.safe(
                "gate",
                event,
                at=kst(at),
                outcome="REUSED",
                reasons=[],
                context=self._usage_comparison_context,
                scoped=self.scoped_review_reuse,
            )
        self.store.save_event(event)
        report.setdefault("reused_reviews", []).append(
            dict(
                event_id=event["id"],
                judged_at=plan["judged_at"],
                checked_at=kst(at),
                reanalyze_by=kst(until),
                reason=plan["reason"],
            )
        )
        return True

    @staticmethod
    def _brief(events, reserve=()):
        """At most 100 events, prioritizing document overlap then recent observations."""
        ranked = sorted(
            events,
            key=lambda item: (
                item["id"] not in reserve,
                -timestamp(item["updated_at"]).timestamp(),
                item["id"],
            ),
        )
        tail = sorted(
            ranked[:100], key=lambda item: (timestamp(item["updated_at"]), item["id"])
        )
        from tokmo.programs.news_context_memory import memory_view, observation_view

        return [
            {
                "event_id": event["id"],
                "title": event["decision"]["title"],
                "facts": event["decision"]["facts"],
                "open": event["decision"]["open"],
                "context_memory": memory_view(event),
                "context_observations": observation_view(event),
                "last_sent_memory": (event.get("last_sent") or {}).get(
                    "context_memory"
                ),
                "last_delivery": event.get("last_delivery", "NOT_SENT"),
                "last_sent_at": kst_field(
                    (event.get("last_sent") or {}).get("delivered_at")
                ),
            }
            for event in tail
        ]

    @staticmethod
    def _sent_events(events, exposed, exclude=None, nominated=()):
        """Other events' actual delivered bodies for comparison: direct document overlap with the exposed
        evidence first, then nominated events, ordered by delivered_at then event id, one per alert id.
        Returns (bounded rows, omitted count)."""
        docs = {doc_key(item["id"]) for item in exposed}
        rows = []
        for event in events:
            sent = event.get("last_sent")
            if event["id"] == exclude or not sent:
                continue
            overlap = any(
                doc_key(item["id"]) in docs for item in event.get("evidence", [])
            )
            if overlap or event["id"] in nominated:
                rows.append(
                    (
                        0 if overlap else 1,
                        -timestamp(sent["delivered_at"]).timestamp(),
                        event["id"],
                        {
                            "event_id": event["id"],
                            "alert_id": sent["alert_id"],
                            "delivered_at": kst_field(sent["delivered_at"]),
                            "body": sent["body"],
                            "direct_overlap": overlap,
                        },
                    )
                )
        view, seen = [], set()
        for _, _, _, row in sorted(rows, key=lambda row: row[:3]):
            if row["alert_id"] not in seen:
                seen.add(row["alert_id"])
                view.append(row)
        return view[:COMPARISON_BOUND], max(0, len(view) - COMPARISON_BOUND)

    def _screen_request(self, batch, events):
        docs = {doc_key(item["id"]) for item in batch}
        known = {}
        for event in events:
            for item in event.get("evidence", []):
                if doc_key(item["id"]) in docs:
                    known.setdefault(doc_key(item["id"]), []).append(event["id"])
        overlap = {event_id for ids in known.values() for event_id in ids}
        # Recent actual sends fill the remaining body slots after direct overlap (nominated = every sent event).
        if self.context_mode in {"efficient", "incremental"}:
            from tokmo.programs.news_context_efficiency import screen_view

            brief, detailed = screen_view(events, batch, overlap)
            bodies, omitted = self._sent_events(events, batch, nominated=detailed)
        else:
            bodies, omitted = self._sent_events(
                events, batch, nominated={event["id"] for event in events}
            )
            brief = self._brief(events, reserve=overlap)
        supplied = {item["event_id"] for item in brief}
        by_id = {event["id"]: event for event in events}
        for row in brief:
            row["source_time_context"] = self._source_time_context(
                by_id[row["event_id"]]
            )
        return {
            "task": "screen",
            "evidence": [
                dict(
                    item,
                    known_event_ids=sorted(
                        set(known.get(doc_key(item["id"]), [])) & supplied
                    ),
                )
                for item in model_evidence(batch, text_limit=700)
            ],
            "events": brief,
            "events_omitted": len(events) - len(brief),
            "sent_bodies": bodies,
            "sent_bodies_omitted": omitted,
            "context": {},
        }

    @staticmethod
    def _source_time_context(event):
        """Two source anchors, not an inferred event date or a full history.

        Keep the oldest/latest observation with its original publication
        provenance, even for catalog-only events. Missing publication remains
        unknown; neither observation nor publication is occurrence time.
        This model view does not add fields to persisted events.
        """
        evidence = sorted(
            event.get("evidence", []),
            key=lambda item: (timestamp(item["observed_at"]), item["id"]),
        )
        anchors = {item["id"]: item for item in evidence[:1] + evidence[-1:]}
        return [
            {
                "id": item["id"],
                "title": str(item.get("title") or "")[:200],
                "published_at": kst_field(item.get("published_at")),
                "publication_date": item.get("publication_date"),
                "observed_at": kst_field(item.get("observed_at")),
                "time_interpretation": item.get("time_interpretation"),
            }
            for item in anchors.values()
        ]

    @staticmethod
    def _pending_bodies(previous):
        """Proposed-but-not-delivered bodies of this event, newest first: unqueued pending proposals, the
        last proposal while it is not the delivered baseline, and retained comparison snapshots. A legacy
        proposal without a stored body stays ``body: null`` (UNKNOWN), never rebuilt from the decision."""
        if not previous:
            return []
        sent_id = (previous.get("last_sent") or {}).get("alert_id")
        refs = previous.get("delivery_refs", {})
        rows = [
            (item, "QUEUED" if item["id"] in refs else "PENDING")
            for item in previous.get("pending_alerts", [])
        ]
        last = previous.get("last_alert")
        if (
            last
            and previous.get("completed_proposal_id") != last.get("id")
            and last.get("id") != sent_id
            and all(item["id"] != last["id"] for item, _ in rows)
        ):
            state = previous.get("last_delivery", "NOT_SENT")
            rows.append((last, "UNKNOWN" if state == "SENT" else state))
        for item in previous.get("comparison_proposals", []):
            if item["id"] != sent_id and all(
                item["id"] != row["id"] for row, _ in rows
            ):
                rows.append(
                    (
                        item,
                        item.get("state")
                        or ("QUEUED" if item["id"] in refs else "PENDING"),
                    )
                )
        rows.sort(
            key=lambda row: timestamp(
                row[0].get("created_at") or previous["updated_at"]
            ),
            reverse=True,
        )
        return [
            {
                "alert_id": item["id"],
                "kind": item.get("kind", "NEWS"),
                "state": state,
                "created_at": kst_field(item.get("created_at")),
                "body": item.get("body"),
                "evidence_ids": item.get("evidence_ids"),
                "delivered": False,
            }
            for item, state in rows
        ]

    def _analyze(
        self, selected, previous, now, report, comparison_ids=(), deferred_at=None
    ):
        if not self.diagnostics:
            return self._analyze_impl(
                selected, previous, now, report, comparison_ids, deferred_at
            )
        evaluation_id = self.diagnostics.safe(
            "evaluation", "analyze", (previous or {}).get("id")
        )
        alert_start = len(report["alerts"])
        try:
            event = self._analyze_impl(
                selected, previous, now, report, comparison_ids, deferred_at
            )
        except Exception as exc:
            self.diagnostics.safe(
                "emit",
                "evaluation_finished",
                evaluation_id=evaluation_id,
                task="analyze",
                event_id=(previous or {}).get("id"),
                accepted=False,
                error_type=type(exc).__name__,
            )
            raise
        self.diagnostics.safe(
            "emit",
            "evaluation_finished",
            evaluation_id=evaluation_id,
            task="analyze",
            event_id=event["id"],
            accepted=True,
            notify=event["decision"]["notify"],
            change=event["decision"]["change"],
            proposal_ids=[r["id"] for r in report["alerts"][alert_start:]],
        )
        return event

    def _analyze_impl(
        self, selected, previous, now, report, comparison_ids=(), deferred_at=None
    ):
        from tokmo.programs.news_context_memory import (
            compact_prior,
            numeric_progress,
            related_context,
            update_memory,
        )

        expanded_portfolio = False

        def analysis_context(at):
            result = self._context(at)
            if self.context_mode == "incremental":
                from tokmo.programs.news_incremental_review import (
                    dependent_keys,
                    market_progress,
                    market_view,
                )

                # Exact quantities clarify why a relevant account change invalidated reuse.
                # Values/weights are not invented when quotes or ETF weights are missing.
                if self.scoped_review_reuse:
                    keys = dependent_keys(
                        previous
                        if previous and not selected and not expanded_portfolio
                        else {},
                        self._display_context,
                    )
                    result["exposures"] = [
                        {
                            k: v
                            for k, v in r.items()
                            if k
                            in {
                                "key",
                                "account",
                                "scope",
                                "quantity",
                                "currency",
                                "observed_at",
                                "quantity_basis",
                            }
                        }
                        for r in self._display_context.get("holdings", [])
                        if r.get("key") in keys
                    ]
                view = market_view(
                    self._display_context, previous if not selected else None
                )
                if view is not None:
                    result["market_observations"] = view
                    result["market_progress"] = market_progress(previous, view)
            return result

        if previous and not selected:
            comparison_ids = comparison_ids or previous.get("review_context_ids", [])
        evidence = {item["id"]: item for item in (previous or {}).get("evidence", [])}
        evidence.update({item["id"]: item for item in selected})
        evidence = list(evidence.values())
        prior_context = {
            key: value
            for key, value in (previous or {}).items()
            if key
            in {
                "id",
                "decision",
                "context_snapshot",
                "updated_at",
                "last_delivery",
                "held_until",
                "last_sent",
                "source_enrichment",
                "delivered_history",
                "context_memory",
            }
        }
        if previous:
            # last_delivery is the state of the last *proposal*; last_sent is the last actual send.
            prior_context["last_proposal_state"] = previous.get(
                "last_delivery", "NOT_SENT"
            )
        if previous and previous.get("last_alert"):
            prior_context["last_alert"] = {
                key: previous["last_alert"][key] for key in ("id", "created_at")
            }
        if previous:
            prior_context = prior_times(
                prior_context
            )  # model view only; the stored event is not rewritten
            if self.context_mode != "full":
                prior_context = compact_prior(prior_context)
        current_ids = {item["id"] for item in selected}
        last_sent = (previous or {}).get("last_sent") or {}
        pending = self._pending_bodies(previous)
        # Bounded retained evidence, earlier unsent decisions/proposals before the recent tail: DONE inputs
        # never lose eligibility; whatever still does not fit is counted, not treated as repetition.
        background = (previous or {}).get("memory_sources", [])
        old_pool = {item["id"]: item for item in background + evidence}
        older = [item for item in old_pool.values() if item["id"] not in current_ids]
        by_id = {item["id"]: item for item in older}
        unsent = (previous or {}).get("unsent_evidence") or {}
        priority = list(
            ((previous or {}).get("context_memory") or {}).get("source_ids", [])
        )
        priority += list(last_sent.get("evidence_ids") or []) + list(
            unsent.get("ids", [])
        )
        priority += [key for row in pending for key in row.get("evidence_ids") or []]
        priority += list(
            (previous or {}).get("decision", {}).get("evidence_ids", [])
        ) + list(last_sent.get("evidence_ids") or [])
        chosen = list(
            dict.fromkeys(
                [key for key in priority if key in by_id]
                + [item["id"] for item in reversed(older)]
            )
        )[:COMPARISON_BOUND]
        previous_evidence = [item for item in older if item["id"] in chosen]
        full_evidence = model_evidence(previous_evidence + selected, text_limit=4000)
        summarized = set(
            ((previous or {}).get("context_memory") or {}).get("source_ids", [])
        )
        visible_evidence = [
            dict(row, text=row["text"][:800], text_truncated=True)
            if self.context_mode != "full"
            and row["id"] in summarized
            and row["id"] not in current_ids
            and row.get("content_scope") != "original_document"
            and len(row["text"]) > 800
            else row
            for row in full_evidence
        ]
        others, others_omitted = self._sent_events(
            self.store.events(),
            previous_evidence + selected,
            exclude=(previous or {}).get("id"),
            nominated=set(comparison_ids),
        )
        cited = (
            "UNKNOWN"
            if last_sent and not last_sent.get("evidence_ids")
            else list(last_sent.get("evidence_ids") or [])
        )
        comparison = {
            "delivered": {
                "alert_id": last_sent["alert_id"],
                "delivered_at": kst_field(last_sent.get("delivered_at")),
                "cited_ids": cited,
                "cited_doc_keys": "UNKNOWN"
                if cited == "UNKNOWN"
                else sorted({doc_key(key) for key in cited}),
            }
            if last_sent
            else None,
            "pending": pending[:COMPARISON_BOUND],
            "other_events": others,
            "evidence_delta": evidence_delta(
                previous_evidence + selected, evidence, cited
            ),
            "coverage": {
                "baseline_applicable": bool(last_sent or pending or others),
                "delivered_versions": "UNKNOWN"
                if cited == "UNKNOWN"
                else "KNOWN"
                if last_sent
                else "NONE",
                "pending_omitted": max(0, len(pending) - COMPARISON_BOUND),
                "other_events_omitted": others_omitted,
                "evidence_omitted": len(older) - len(previous_evidence),
            },
        }
        review_reason = (
            "NEW_EVIDENCE"
            if selected
            else "EVENING_RELEASE"
            if previous and previous.get("held_kind") == "EVENING_HEADLINES"
            else "MORNING_RELEASE"
            if previous and previous.get("held_until")
            else "PERIODIC_RECHECK"
        )
        recovery_due = bool(
            previous
            and previous.get("recovery_review_at")
            and timestamp(previous["recovery_review_at"]) <= now
        )
        if recovery_due:
            review_reason = "DELIVERY_RECOVERY"
        if recovery_due and not last_sent:
            # A retired unsent draft is not an accepted importance judgment.
            # Re-evaluate its original evidence without repeating its prose.
            prior_context.pop("decision", None)
        request = {
            "task": "analyze",
            "decision_contract": "alert-value-v1",
            "evidence": visible_evidence,
            "review_reason": review_reason,
            "events": [prior_context] if previous else [],
            "comparison": comparison,
        }
        if deferred_at:
            request["model_wait_recovery"] = dict(
                classified_at=deferred_at,
                note="모델 장애로 미뤄진 후보. 지금 새로 발생한 사실이 아니다. 원문/관측 시각과 현재 실제 전달 이력을 비교해 지금도 전달할 가치가 있는지 판단한다. 과거 분류 통과는 현재 발송 승인이 아니다.",
            )
        if self.context_mode == "incremental" and previous:
            request["review_trigger"] = dict(
                reasons=(previous.get("last_review_reuse_miss") or {}).get(
                    "reasons", []
                ),
                evidence_changed=bool(selected),
                previous_assessed_at=previous.get("updated_at"),
                recheck_plan={
                    k: v
                    for k, v in (previous.get("review_reuse_plan") or {}).items()
                    if k in {"after", "reason", "judged_at", "kind", "safety_at"}
                },
                note="재사용 기한은 사업 실패/연기 증거가 아니다. 현재 입력의 원천 관측 시각과 자료 공백을 구분한다.",
            )
        if self.reuse_unchanged_reviews:
            request["review_reuse_enabled"] = True
        related = related_context(
            self.store.events(),
            set(comparison_ids),
            exclude=(previous or {}).get("id"),
            at=now,
        )
        # Restore temporal anchors only for the IDs selected by screen; this is
        # not another search or classification step before analysis.
        related_by_id = {row["event_id"]: row for row in related}
        sources = {row["id"]: row for row in background + evidence}
        for event in self.store.events():
            if event["id"] in related_by_id:
                related_by_id[event["id"]]["source_time_context"] = (
                    self._source_time_context(event)
                )
                sources.update(
                    {
                        row["id"]: row
                        for row in event.get("evidence", [])
                        if timestamp(row["observed_at"]) <= now
                    }
                )
        remembered = set(
            ((previous or {}).get("context_memory") or {}).get("source_ids", [])
        )
        remembered.update(
            key for row in related for key in row["memory"].get("source_ids", [])
        )
        request["continuity"] = {
            "related_events": related,
            "last_sent_at": last_sent.get("delivered_at"),
            "hours_since_last_send": max(
                0, (now - timestamp(last_sent["delivered_at"])).total_seconds() / 3600
            )
            if last_sent
            else None,
            "numeric_progress": numeric_progress(
                evidence, baseline_ids=set(last_sent.get("evidence_ids") or []), at=now
            ),
            "source_index": [
                {
                    "id": key,
                    "title": sources[key].get("title", ""),
                    "observed_at": kst(sources[key]["observed_at"]),
                }
                for key in sorted(remembered)
                if key in sources
            ],
            "note": "내부 상태와 실제 전달 기준은 별개다. 시간 경과만으로 악화/변화 없음/사용자 읽음을 단정하지 않는다.",
        }
        from tokmo.programs.news_context_efficiency import (
            followup_scope,
            portfolio_basis,
            scoped_context,
            source_expansions,
        )

        expansions = source_expansions(
            visible_evidence, sources, remembered, model_evidence
        )
        request["source_availability"] = dict(
            expandable_ids=sorted(expansions), local_stored_only=True
        )
        if any(
            item.get("content_scope") == "original_document"
            for item in visible_evidence
        ):
            request["document_amounts"] = document_amounts(visible_evidence)
        applicable = comparison["coverage"]["baseline_applicable"]
        prior_bodies = [
            body
            for body in [
                last_sent.get("body"),
                *[row.get("body") for row in pending],
                *[row.get("body") for row in others],
            ]
            if isinstance(body, str)
        ]
        # The portfolio context is built inside _call for the captured call instant (freshness and
        # future-check at that instant); a correction rebuilds it for its own instant.
        scope = (
            (previous or {}).get("followup_scope")
            if review_reason == "PERIODIC_RECHECK"
            else None
        )
        basis = (
            (previous or {}).get("followup_portfolio_basis")
            if scope is not None
            else None
        )
        if (
            self.scoped_review_reuse
            and previous
            and previous.get("review_dependencies")
        ):
            from tokmo.programs.news_incremental_review import review_dependencies

            if not review_dependencies(
                previous["review_dependencies"], getattr(self, "_display_context", {})
            )["all_portfolio"]:
                # Still expose all choices/held identities. A relevant new report
                # can use the existing explicit context expansion path.
                basis = None
        detail_event = (
            previous
            if (
                self.event_detail_enabled
                and self.context_mode == "incremental"
                and review_reason == "PERIODIC_RECHECK"
            )
            else None
        )
        # An identity added/removed since the last assessment may create a new
        # relationship the old dependency scope cannot know.
        if detail_event:
            from tokmo.programs.news_incremental_review import portfolio_keys

            current = analysis_context(self._now())
            if portfolio_keys(current) != portfolio_keys(
                previous.get("context_snapshot") or {}
            ):
                detail_event = None
                expanded_portfolio = True
        raw, at, sent = self._call(
            "analyze",
            request,
            report,
            context=analysis_context,
            followup_scope=scope,
            portfolio_basis=basis,
            detail_event=detail_event,
        )
        requested = raw.get("source_requests") or []
        allowed_sources = remembered | {row["id"] for row in visible_evidence}
        requested = (
            list(
                dict.fromkeys(
                    k
                    for k in requested
                    if isinstance(k, str) and k in allowed_sources and k in sources
                )
            )[:8]
            if isinstance(requested, list)
            else []
        )
        unavailable = [key for key in requested if key not in expansions]
        if unavailable:
            report.setdefault("source_restore_noops", []).append(
                dict(
                    event_id=(previous or {}).get("id"),
                    source_ids=unavailable,
                    reason="NO_ADDITIONAL_LOCAL_TEXT",
                )
            )
        requested = [key for key in requested if key in expansions]
        if requested:
            expanded = {row["id"]: row for row in visible_evidence}
            expanded.update({key: expansions[key] for key in requested})
            visible_evidence = list(expanded.values())
            request = dict(
                request,
                evidence=visible_evidence,
                source_availability=dict(
                    expandable_ids=sorted(set(expansions) - set(requested)),
                    local_stored_only=True,
                ),
            )
            if any(
                e.get("content_scope") == "original_document" for e in visible_evidence
            ):
                request["document_amounts"] = document_amounts(visible_evidence)
        desired = followup_scope(raw.get("followup_scope"), self._display_context)
        desired_reviews = (
            scoped_context(self._display_context, desired).get("investment_followup")
            or {}
        ).get("delivered_reviews", [])
        shown = {
            r["id"]
            for r in (sent["context"].get("investment_followup") or {}).get(
                "delivered_reviews", []
            )
        }
        followup_expansion = self.context_mode in {"efficient", "incremental"} and any(
            r["id"] not in shown for r in desired_reviews
        )
        portfolio_expansion = False
        if sent["context"].get("detail_scope"):
            from tokmo.programs.news_incremental_review import (
                dependent_keys,
                review_dependencies,
            )

            wanted = dict(
                review_dependencies=raw.get("review_dependencies"),
                followup_scope=raw.get("followup_scope"),
            )
            shown_choices = {
                r["id"]
                for r in (sent["context"].get("investment_followup") or {}).get(
                    "choices", []
                )
            }
            all_choices = {
                r["id"]
                for r in (self._display_context.get("investment_followup") or {}).get(
                    "choices", []
                )
            }
            portfolio_expansion = (
                review_dependencies(
                    raw.get("review_dependencies"), self._display_context
                )["all_portfolio"]
                or not dependent_keys(wanted, self._display_context)
                <= set(sent["context"]["detail_scope"]["keys"])
                or not (
                    all_choices
                    if desired["all_choices"]
                    else set(desired["choice_ids"])
                )
                <= shown_choices
            )
        source_review = bool(request.get("document_amounts")) or any(
            item.get("content_scope") == "original_document"
            for item in visible_evidence
        )
        try:
            if requested:
                raise ValueError("requested_original_sources_restored")
            if followup_expansion:
                raise ValueError("followup_context_expanded")
            if portfolio_expansion:
                raise ValueError("portfolio_context_expanded")
            decision = validate_decision(
                raw,
                visible_evidence,
                previous,
                baseline_applicable=applicable,
                require_notify=True,
                prior_bodies=prior_bodies,
            )
            if source_review:
                # Spend the existing one-correction allowance on a source audit even when
                # the response has a valid shape. No third call or extra model is added.
                raise ValueError("source_grounding_review")
        except ValueError as exc:
            # One explicit correction of a contract violation by the same model.
            # Transport failures raise inside _call and never reach here; the
            # original call row and response stay as evidence; a second
            # violation is a normal analysis failure, not another repair.
            correction = {
                "stage": "analyze",
                "evidence_ids": sorted(current_ids),
                "reason": str(exc)[:200],
                "status": "FAILED",
            }
            report.setdefault("corrections", []).append(correction)
            if portfolio_expansion:
                expanded_portfolio = True
            corrected, at, sent = self._call(
                "analyze",
                dict(
                    request,
                    correction={
                        "reason": correction["reason"],
                        "source_grounding_review": source_review,
                        "restored_source_ids": requested,
                        "unavailable_source_ids": unavailable,
                        "followup_context_expanded": followup_expansion,
                        "previous_response": {
                            key: value
                            for key, value in raw.items()
                            if not key.startswith("_")
                        },
                        "instruction": (
                            "원문 대조 검토다. previous_response의 각 사실·해석·왜 지금·다음 확인을 evidence 원문과 대조하라. 숫자·통화·단위뿐 아니라 원문에 없는 주체, 청약/매매 가능성, 지분 희석, 회사 재무능력, 사업 확대·전략 의도, 업계 지위를 삭제하거나 근거가 있는 의미로 고쳐라. 주주배정이라는 단어만으로 일반 투자자가 참여한다고 해석하지 말라. 지분율 불변이라는 원문 조건을 일반론보다 우선한다. 권유가 없어도 새 회사를 발견할 구체적 이유는 유지하고 확인된 자금 지원·계약 조건의 의미를 설명하라. 증거가 부족하면 불확실성을 정확히 쓰되 임의로 가치를 낮추지 말라. 잘못된 내용은 판단 유지를 핑계로 보존하지 말라. 지금이 장외라는 추측을 하지 말라. 완전한 분석 JSON 객체 하나만 출력하라."
                            if source_review
                            else "실제로 확장된 근거는 restored_source_ids에만 있다. unavailable_source_ids는 추가 원문이 없다. context에는 전체 선택 후속 보고서를 제공했다. 추가된 내용으로 판단을 확인하고 아직 모르는 사항은 그대로 남겨라. source_requests=[]로 마무리하라. 추가 호출은 없다."
                            if requested or followup_expansion or portfolio_expansion
                            else "이전 응답은 위 reason 때문에 계약 검증에 실패했다. 판단 내용을 새로 만들지 말고 previous_response의 판단을 유지한 채 지적된 위반만 고쳐 완전한 객체를 다시 출력하라."
                        ),
                    },
                ),
                report,
                context=analysis_context,
            )
            decision = validate_decision(
                corrected,
                visible_evidence,
                previous,
                baseline_applicable=applicable,
                require_notify=True,
                prior_bodies=prior_bodies,
            )
            raw = corrected
            correction["status"] = "OK"
        # ``at`` is the instant of the call that produced the accepted decision and ``sent`` its
        # request; ``now`` (run snapshot) keeps deciding what was due.
        context = sent["context"]
        event_id = (
            previous["id"]
            if previous
            else "event-" + identity(sorted(decision["evidence_ids"]))
        )
        event = dict(
            previous or {},
            id=event_id,
            decision=decision,
            evidence=evidence,
            context_snapshot=context,
            updated_at=kst(at),
            review_error=None,
        )
        if self.typed_review_enabled or self.event_detail_enabled:
            event["review_efficiency_contract"] = 1
        if self.context_mode in {"efficient", "incremental"}:
            event["followup_scope"] = followup_scope(
                raw.get("followup_scope"), self._display_context
            )
            event["followup_portfolio_basis"] = portfolio_basis(self._display_context)
        if self.context_mode == "incremental":
            from tokmo.programs.news_incremental_review import review_dependencies

            event["review_dependencies"] = review_dependencies(
                raw.get("review_dependencies"), self._display_context
            )
            if context.get("market_observations"):
                from tokmo.programs.news_incremental_review import first_market_anchor

                event["market_anchor"] = first_market_anchor(
                    event.get("market_anchor"), context["market_observations"]
                )
        if unavailable:
            event["source_restore_noop_count"] = (
                event.get("source_restore_noop_count", 0) + 1
            )
        memory, issue = update_memory(
            raw.get("context_update"), previous, visible_evidence, at=at
        )
        if memory:
            event["context_memory"] = memory
            memory["first_observed_at"] = (
                (previous or {}).get("context_memory") or {}
            ).get("first_observed_at") or kst(
                min(timestamp(e["observed_at"]) for e in evidence)
            )
            own_ids = {row["id"] for row in evidence}
            event["memory_sources"] = [
                sources[k]
                for k in memory.get("source_ids", [])
                if k in sources and k not in own_ids
            ]
        if issue:
            event["memory_status"] = issue
            if issue != "NOT_PROVIDED":
                report.setdefault("memory_issues", []).append(
                    {"event_id": event_id, "reason": issue}
                )
        else:
            event["memory_status"] = "UPDATED"
        event["context_observations"] = {
            "observed_through": kst(max(timestamp(e["observed_at"]) for e in evidence)),
            "observations": len(evidence),
            "numeric_progress": numeric_progress(
                evidence, baseline_ids=set(last_sent.get("evidence_ids") or []), at=at
            ),
        }
        if recovery_due:
            event.pop("recovery_review_at", None)
        report["decisions"].append(dict(decision, event_id=event_id))
        held_due = (
            previous is not None
            and previous.get("held_until")
            and timestamp(previous["held_until"]) <= now
        )
        evening_due = bool(
            held_due and previous.get("held_kind") == "EVENING_HEADLINES"
        )
        if held_due:
            event["held_until"] = None
        changed = decision["change"] != "NONE"
        worthy = decision["importance"] in {"HIGH", "MEDIUM"}
        correction = (
            decision["change"] in {"CORRECTION", "RESOLVED"}
            and previous
            and previous.get("last_alert")
        )
        value_due = bool(
            previous
            and previous.get("decision", {}).get("notify") is False
            and unsent.get("ids")
            and not last_sent
            and not pending
        )
        wants_alert = bool(
            decision["notify"]
            and (
                (worthy and (changed or held_due or value_due or recovery_due))
                or correction
            )
        )
        # Materiality was judged against the supplied bodies. The accepted transition identifies this
        # decision: same delivered baseline, change, confirmation, importance, urgency, exact support
        # versions, material-change payload, legacy prose and substantive context. Equal prose alone
        # neither cancels a changed claim nor authorizes a message; an exact repeat reuses its proposal.
        signature = identity(
            {
                key: decision[key]
                for key in ("facts", "interpretation", "portfolio_note", "confirmation")
            }
        )
        material = decision["material_change"]
        transition = identity(
            [
                event_id,
                last_sent.get("alert_id", ""),
                decision["change"],
                decision["confirmation"],
                decision["importance"],
                decision["urgency"],
                sorted(
                    material["support_ids"] if material else decision["evidence_ids"]
                ),
                {
                    key: material[key]
                    for key in ("prior_claim", "new_fact", "why_not_covered")
                }
                if material
                else None,
                signature,
                identity(
                    {
                        key: value
                        for key, value in context.items()
                        if key not in CONTEXT_TIME_KEYS
                    }
                ),
            ]
        )
        if event.get("recovery_generation"):
            # An explicitly retired failed outbox must not capture the new
            # assessment under its old terminal id, even if the prose matches.
            transition = identity([transition, event["recovery_generation"]])
        if wants_alert or (worthy and changed):
            # Evidence of decisions not yet delivered stays eligible for the model's view until a later actual send.
            event["unsent_evidence"] = {
                "observed_at": kst(at),
                "ids": list(
                    dict.fromkeys(
                        list(unsent.get("ids", [])) + decision["evidence_ids"]
                    )
                ),
            }
        if wants_alert and previous and previous.get("last_transition") == transition:
            wants_alert = False
            report.setdefault("normalizations", []).append(
                {
                    "event_id": event_id,
                    "reason": "same accepted transition; existing proposal "
                    + str(previous.get("last_alert", {}).get("id"))
                    + " reused",
                }
            )
        event["next_check_at"] = (
            kst(at + timedelta(hours=6 if decision["importance"] == "HIGH" else 24))
            if decision["open"]
            else None
        )
        if not worthy or (
            event.get("held_kind") == "EVENING_HEADLINES" and not decision["notify"]
        ):
            event["held_until"] = None
            event.pop("held_kind", None)
        evening_release = bool(
            wants_alert
            and evening_due
            and self._evening_open(at)
            and headline_only(decision)
        )
        if (
            wants_alert
            and headline_only(decision)
            and self.headline_digest_hour is not None
            and not evening_release
        ):
            # Keep the original slot during daytime updates; a fresh selection after the cutoff goes to tomorrow.
            existing = (
                (previous or {}).get("held_until")
                if (previous or {}).get("held_kind") == "EVENING_HEADLINES"
                else None
            )
            event["held_until"] = (
                existing
                if existing and timestamp(existing) > now
                else kst(evening_slot(now, self.headline_digest_hour, next_slot=True))
            )
            event["held_kind"] = "EVENING_HEADLINES"
        elif (
            wants_alert
            and night(at)
            and not (
                decision["importance"] == "HIGH" and decision["urgency"] == "IMMEDIATE"
            )
        ):
            event["held_until"] = kst(morning(at))
            event.pop("held_kind", None)
        elif wants_alert:
            event["held_until"] = None
            event.pop("held_kind", None)
            market_lines = []
            try:
                from tokmo.programs.news_portfolio_context import price_lines

                market_lines = price_lines(
                    decision.get("subject_keys", []),
                    getattr(self, "_display_context", context),
                    at=at,
                )
            except (ValueError, KeyError, TypeError, ArithmeticError) as error:
                report.setdefault("display_issues", []).append(type(error).__name__)
            # Validation also admits restored sources from related event memory.
            # Carry their original metadata into presentation and digest details;
            # they remain references, not new inputs belonging to this event.
            alert_sources = {item["id"]: item for item in evidence}
            alert_sources.update(
                (key, sources[key]) for key in decision["evidence_ids"]
            )
            alert_evidence = list(alert_sources.values())
            predecessor = (previous or {}).get("last_alert", {}).get("id", "")
            alert = {
                "id": "decision-alert-" + identity([transition, predecessor]),
                "event_id": event_id,
                "evidence_ids": decision["evidence_ids"],
                "importance": decision["importance"],
                "urgency": decision["urgency"],
                "source_observed_at": kst(
                    min(timestamp(item["observed_at"]) for item in selected)
                )
                if selected
                else None,
                "kind": "CORRECTION" if decision["change"] == "CORRECTION" else "NEWS",
                "created_at": kst(at),
                "body": render_alert(
                    decision,
                    alert_evidence,
                    at,
                    followup=bool(previous and previous.get("last_sent")),
                    market_lines=market_lines,
                ),
                "morning": bool(held_due and not evening_due),
                "transition": transition,
                "predecessor_id": predecessor,
                "baseline_alert_id": last_sent.get("alert_id", ""),
                "material_change": material,
            }
            if evening_release:
                alert.update(
                    evening_slot=kst(evening_slot(at, self.headline_digest_hour)),
                    digest_hour=self.headline_digest_hour,
                    headline=decision["title"],
                    digest_summary=decision.get("digest_summary")
                    or decision["interpretation"],
                )
                event.setdefault("pending_digest_details", {})[alert["id"]] = dict(
                    decision=decision, evidence=alert_evidence
                )
            if event.get("context_memory") and issue is None:
                alert["context_memory"] = event["context_memory"]
            pending_alerts = list(event.get("pending_alerts", []))
            refs = event.get("delivery_refs", {})
            # An unqueued ordinary proposal of this event is replaced by the newer coherent body (no outbox row
            # exists yet); QUEUED bodies and morning/digest proposals are never touched.
            replaced = [
                item["id"]
                for item in pending_alerts
                if not item.get("morning")
                and item.get("kind") not in {"MORNING_DIGEST", "EVENING_DIGEST"}
                and item["id"] not in refs
                and item["id"] != alert["id"]
            ]
            if replaced:
                pending_alerts = [
                    item for item in pending_alerts if item["id"] not in replaced
                ]
                for key in replaced:
                    event.get("pending_digest_details", {}).pop(key, None)
            old = event.get("last_alert") or {}
            # The superseded proposal that is neither delivered nor still pending (queued/failed/unknown) is
            # kept as comparison data until it becomes the delivered baseline.
            if (
                old.get("id")
                and old["id"]
                not in (
                    alert["id"],
                    last_sent.get("alert_id"),
                    event.get("completed_proposal_id"),
                    *replaced,
                )
                and all(item["id"] != old["id"] for item in pending_alerts)
            ):
                retained = event.setdefault("comparison_proposals", [])
                if all(item["id"] != old["id"] for item in retained):
                    retained.append(
                        {
                            key: old.get(key)
                            for key in (
                                "id",
                                "kind",
                                "body",
                                "evidence_ids",
                                "constituent_ids",
                                "created_at",
                                "importance",
                                "urgency",
                            )
                        }
                        | {"state": event.get("last_delivery")}
                    )
            event["last_transition"] = transition
            event["last_alert"] = alert
            event["last_delivery"] = "PENDING"
            event["pending_alerts"] = pending_alerts + [alert]
            report["alerts"].append(alert)
        if event.get("comparison_proposals"):
            event["comparison_proposals"] = [
                item
                for item in event["comparison_proposals"]
                if item["id"] != last_sent.get("alert_id")
            ]
        event.pop("review_reuse_plan", None)
        if self.reuse_unchanged_reviews:
            from tokmo.programs.news_context_efficiency import review_plan

            plan = review_plan(
                raw.get("recheck"), decision, at, typed=self.typed_review_enabled
            )
            if plan:
                event["review_context_ids"] = sorted(set(comparison_ids))
                plan["fingerprint"] = self._review_fingerprint(event)
                plan["basis"] = self._review_basis(event)
                event["review_reuse_plan"] = plan
                event["next_check_at"] = kst(
                    min(timestamp(event["next_check_at"]), timestamp(plan["after"]))
                )
        if self.diagnostics:
            from tokmo.programs.news_usage_diagnostics import BASELINE_KEY

            diagnostic_basis = self.diagnostics.safe(
                "baseline", event, self._display_context, self.scoped_review_reuse
            )
            if diagnostic_basis is not None:
                event[BASELINE_KEY] = diagnostic_basis
        if self.model_wait and selected:
            self.store.save_event(event, accepted_inputs=[r["id"] for r in selected])
        else:
            self.store.save_event(event)
        # A provisional LOW based on a thin listing must not prevent the requested source
        # check forever. Screening already admitted this candidate and open keeps the question
        # unresolved; the same one-document/pass cap and 24h retry interval still apply.
        # With no source readers (historical replay), preserve the old diagnostic projection.
        if (
            not decision["notify"]
            and decision["open"]
            and (
                worthy
                or self.document_reader is not None
                or self.sec_document_reader is not None
            )
        ):
            self._enrich(event, at, report)
        if (
            wants_alert
            and not event.get("held_until")
            and not event.get("last_alert", {}).get("morning")
            and decision["importance"] == "HIGH"
            and decision["urgency"] == "IMMEDIATE"
            and self.on_urgent
        ):
            try:
                self.on_urgent(event["last_alert"])
            except Exception as exc:
                report["failures"].append(
                    {
                        "stage": "urgent_queue",
                        "event_id": event_id,
                        "reason": type(exc).__name__,
                    }
                )
        return event

    def _enrich(self, event, at, report):
        """Target one unresolved listing on the existing event/recheck path; no new scheduler or DB."""
        enriched = event.get("source_enrichment") or {}
        done_docs = {
            doc_key(item["id"])
            for item in event["evidence"]
            if item.get("body_verified")
        }
        cited = set(event["decision"]["evidence_ids"])
        listing = next(
            (
                item
                for item in reversed(event["evidence"])
                if item["id"] in cited
                and item.get("content_scope") == "listing_only"
                and doc_key(item["id"]) not in done_docs
                and (
                    re.fullmatch(r"dart:[0-9]{14}:[^:]+:[^:]+", item["id"])
                    or (
                        item.get("source_system") == "SEC"
                        and re.fullmatch(
                            r"sec:[0-9]{10}-[0-9]{2}-[0-9]{6}:metadata", item["id"]
                        )
                    )
                )
            ),
            None,
        )
        if listing is None:
            return
        if enriched.get("retry_at") and timestamp(enriched["retry_at"]) > at:
            return
        reader = (
            self.sec_document_reader
            if listing.get("source_system") == "SEC"
            else self.document_reader
        )
        status = (
            "READER_UNAVAILABLE"
            if reader is None
            else "PASS_LIMIT"
            if self._document_reads
            else "STARTED"
        )
        event["source_enrichment"] = {
            "status": status,
            "evidence_id": listing["id"],
            "needed": event["decision"]["next_check"],
            "attempted_at": kst(at),
            "retry_at": event["next_check_at"],
        }
        self.store.save_event(
            event
        )  # Persist bounded retry before any provider operation.
        if status != "STARTED":
            return
        self._document_reads += 1
        try:
            body = reader(
                listing
                if listing.get("source_system") == "SEC"
                else listing["id"].split(":")[1]
            )
            if (
                not isinstance(body, dict)
                or not isinstance(body.get("text"), str)
                or not body["text"].strip()
            ):
                raise ValueError("document_empty")
            observed = (
                self._now()
            )  # Actual availability, never the listing's publication/collection time.
            document = dict(
                listing,
                id=doc_key(listing["id"]) + ":document:" + identity(body),
                text=body["text"],
                source_document_sha256=body.get("source_document_sha256"),
                observed_at=kst(observed),
                content_scope="original_document",
                body_verified=True,
                parent_evidence_id=listing["id"],
            )
            for key in ("document_scope", "attachments_verified"):
                if key in body:
                    document[key] = body[key]
            stored = self.store.observation(document["id"])
            if stored is not None:
                document = stored  # Same source version keeps its first availability and original provenance.
            else:
                self.store.ingest([document], observed)
            # The receipt uniquely identifies this event's requested source, so no second screen is needed.
            # Save evidence and the due recheck before marking its input consumed (restart-safe).
            event["evidence"].append(document)
            event["source_enrichment"].update(
                status="READY", observed_at=kst(observed), document_id=document["id"]
            )
            event["next_check_at"] = kst(observed)
            unsent = event.setdefault(
                "unsent_evidence", {"observed_at": kst(observed), "ids": []}
            )
            unsent["ids"] = list(dict.fromkeys(unsent["ids"] + [document["id"]]))
            self.store.save_event(event)
            if stored is None:
                self.store.mark([document["id"]], "DONE")
        except Exception as exc:
            reason = str(exc)
            if not re.fullmatch(r"document_[a-z_0-9]+", reason):
                reason = "document_enrichment_failed"
            event["source_enrichment"].update(status="UNAVAILABLE", reason=reason)
            self.store.save_event(event)
        report.setdefault("enrichments", []).append(
            dict(event["source_enrichment"], event_id=event["id"])
        )

    def pending_alerts(self):
        alerts = list(
            {
                alert["id"]: alert
                for event in self.store.events()
                for alert in event.get("pending_alerts", [])
            }.values()
        )
        groups = {}
        for alert in alerts:
            if alert.get("morning"):
                groups.setdefault(
                    timestamp(alert["created_at"]).astimezone(KST).date().isoformat(),
                    [],
                ).append(alert)
        evenings = {}
        for alert in alerts:
            if alert.get("evening_slot"):
                evenings.setdefault(alert["evening_slot"], []).append(alert)
        return (
            [
                alert
                for alert in alerts
                if not alert.get("morning") and not alert.get("evening_slot")
            ]
            + [digest_alerts(group) for group in groups.values()]
            + [evening_digest_alerts(group) for group in evenings.values()]
        )

    def retire_unqueued_proposals(self, alert_ids, *, at, reason):
        """Explicit recovery only: archive reviewed stale drafts, then re-evaluate.

        Does not clear trips, reset inputs, send, or alter queued/failed receipts.
        Call under the existing worker locks after reviewing the exact ids.
        """
        if not reason or not reason.strip():
            raise ValueError("recovery reason required")
        targets = set(alert_ids)
        events = self.store.events()
        known = set()
        changes = []
        for event in events:
            known.update(p["id"] for p in event.get("retired_pending", []))
            selected = [
                p for p in event.get("pending_alerts", []) if p["id"] in targets
            ]
            known.update(p["id"] for p in selected)
            if any(p["id"] in event.get("delivery_refs", {}) for p in selected):
                raise ValueError("queued proposal requires receipt investigation")
            if selected:
                changes.append((event, selected))
        if not targets <= known:
            raise ValueError("proposal changed or absent; review current state")
        for event, selected in changes:
            removed = {p["id"] for p in selected}
            event.setdefault("retired_pending", []).extend(
                dict(p, retired_at=kst(at), retired_reason=reason, state="NOT_SENT")
                for p in selected
            )
            event["pending_alerts"] = [
                p for p in event.get("pending_alerts", []) if p["id"] not in removed
            ]
            event["comparison_proposals"] = [
                p
                for p in event.get("comparison_proposals", [])
                if p["id"] not in removed
            ]
            if event.get("last_alert", {}).get("id") in removed:
                event.pop("last_alert", None)
                event.pop("last_transition", None)
                event["last_delivery"] = "NOT_SENT"
            event["recovery_review_at"] = kst(at)
            event["next_check_at"] = kst(at)
            event["recovery_generation"] = identity(
                [event.get("recovery_generation"), sorted(removed)]
            )
            self.store.save_event(event)
        return {
            "retired": sum(len(selected) for _, selected in changes),
            "events": [e["id"] for e, _ in changes],
        }

    def acknowledge(self, alert_id, status):
        if status not in {"QUEUED", "SENT", "FAILED", "UNKNOWN"}:
            raise ValueError("invalid delivery status")
        proposal = next(
            (item for item in self.pending_alerts() if item["id"] == alert_id), {}
        )
        matching = {alert_id, *proposal.get("constituent_ids", [])}
        # Only events that actually change are rewritten.
        for event in self.store.events():
            pending = [
                item
                for item in event.get("pending_alerts", [])
                if item["id"] not in matching
            ]
            changed = len(pending) != len(event.get("pending_alerts", []))
            if event.get("last_alert", {}).get("id") in matching:
                changed = (
                    changed
                    or event.get("last_delivery") != status
                    or bool(proposal and event["last_alert"] != proposal)
                )
                event["last_delivery"] = status
                if proposal:
                    event["last_alert"] = proposal
            if changed:
                event["pending_alerts"] = pending
                self.store.save_event(event)
