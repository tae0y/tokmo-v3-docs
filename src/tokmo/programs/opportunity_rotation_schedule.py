"""Once-per-slot portfolio decisions; caller owns invocation, never a daemon."""

from copy import deepcopy
from datetime import timedelta
from urllib.parse import urlparse

from tokmo.facts.opportunity_rotation import unavailable_history
from tokmo.programs.opportunity_rotation import (
    _decide,
    _reconciled,
    _resolve_saved,
    build_packet,
    decision_basis,
    pending,
)
from tokmo.values.opportunity_rotation import instant, require_text, validate_session


def _apply_saved(paper, key, now):
    saved = paper.store.transaction(lambda s: _resolve_saved(s, key, now))["slots"][key]
    return dict(status=saved["status"], slot=key, reason=saved["reason"])


def run_slot(paper, discovery, *, clock, model, exceptional=None):
    now = instant(clock())
    # Saved responses resume even after the original call window, without a
    # second model. advance also respects the pre-recorded transition boundary.
    for key, saved in paper.state().get("slots", {}).items():
        if saved["status"] == "WAITING_DATA":
            return _apply_saved(paper, key, now)
    opening, closing = validate_session(discovery["session"])
    if now.date() != opening.date() or not opening <= now < closing:
        return dict(status="CLOSED")
    key = None
    for time in ("10:05", "13:30"):
        due = instant(opening.date().isoformat() + "T" + time + ":00+09:00")
        if due <= now < due + timedelta(minutes=5):
            key = opening.date().isoformat() + ":" + time
    if exceptional is not None:
        # Explicit structured official evidence already collected by NEWS/DART.
        evidence = next(
            (
                e
                for c in discovery["candidates"]
                for e in c.get("evidence", [])
                if e.get("id") == exceptional.get("evidence_id")
            ),
            None,
        )
        official = bool(
            evidence
            and (
                (
                    evidence.get("kind") == "disclosure"
                    and urlparse(evidence.get("source", "")).hostname
                    == "dart.fss.or.kr"
                    and evidence.get("metadata", {}).get("content_extent")
                    == "body_excerpt"
                )
                or (
                    evidence.get("kind") == "news"
                    and evidence.get("metadata", {}).get("official") is True
                )
            )
        )
        if not official or exceptional.get("material") is not True:
            raise ValueError("material official evidence required")
        require_text(exceptional.get("reason"))
        if not opening <= instant(evidence["available_at"]) <= now:
            raise ValueError("exception evidence outside current session")
        key = opening.date().isoformat() + ":official:" + exceptional["evidence_id"]
    if key is None:
        return dict(status="OUTSIDE_SLOT", reason="no backdated catch-up")
    state = paper.state()
    if key in state.get("slots", {}):
        return dict(status="DUPLICATE", slot=key, previous=state["slots"][key])
    if not timedelta(0) <= now - instant(discovery["as_of"]) <= timedelta(minutes=5):
        return dict(status="DATA_NOT_READY", reason="discovery freshness")
    readiness_at = instant(state["valuation_at"]) if state.get("valuation_at") else now
    if pending(state) and not timedelta(0) <= now - readiness_at <= timedelta(
        minutes=5
    ):
        return dict(status="DATA_NOT_READY", reason="pending snapshot stale")
    if any(not _reconciled(state, p, readiness_at) for p in pending(state)):
        return dict(status="DATA_NOT_READY", reason="pending validity reconciliation")
    required = set(state["positions"]) | {p["symbol"] for p in pending(state)}
    candidates = {c["symbol"]: c for c in discovery["candidates"]}
    if not candidates or required - set(candidates):
        return dict(status="DATA_NOT_READY", reason="held/pending universe missing")
    for c in candidates.values():
        quote = c.get("quote")
        history_ready = bool(c.get("bars")) or (
            c["symbol"] not in required
            and unavailable_history(c, discovery["completed_session"])
        )
        if (
            not history_ready
            or not quote
            or not timedelta(0) <= now - instant(quote["at"]) <= timedelta(minutes=5)
        ):
            return dict(
                status="DATA_NOT_READY", reason="candidate facts incomplete or stale"
            )

    def claim(s):
        if key in s.setdefault("slots", {}):
            raise ValueError("slot already claimed")
        s["slots"][key] = dict(
            status="CALLING", at=now.isoformat(), exceptional=exceptional
        )
        return s

    paper.store.transaction(claim)
    packet = build_packet(discovery, paper.state())
    basis = decision_basis(paper.state())
    try:
        decision = model.decide(packet, call_id=key, at=now.isoformat())
        actual = instant(clock())
        effective = actual.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if not now <= actual < effective < closing:
            raise ValueError("model result outside decision session")
        decision["verified_at"] = actual.isoformat()

        def preserve(s):
            if decision["id"] != key:
                raise ValueError("decision slot identity mismatch")
            saved = s["slots"][key]
            saved.update(
                status="WAITING_DATA",
                packet=packet,
                decision=decision,
                basis=basis,
                recorded_at=actual.isoformat(),
                effective_at=effective.isoformat(),
                reason="pending validity reconciliation",
            )
            # Validate a conditional future change on the existing trial copy.
            # Ordinary backdate checks still run; no ledger clock is rewound.
            candidate = deepcopy(s)
            _decide(
                candidate,
                packet,
                decision,
                effective.isoformat(),
                reconciled=True,
                prospective=True,
            )
            saved["changes"] = {
                i: {
                    k: v
                    for k, v in p.items()
                    if k not in s["proposals"].get(i, {}) or s["proposals"][i][k] != v
                }
                for i, p in candidate["proposals"].items()
                if p != s["proposals"].get(i)
            }
            recorded = instant(clock())
            if not actual <= recorded < effective:
                raise ValueError("intent recording missed its future boundary")
            saved["recorded_at"] = recorded.isoformat()
            return s

        saved = paper.store.transaction(
            preserve, commit_check=lambda: actual <= instant(clock()) < effective
        )["slots"][key]
    except Exception as error:
        status, reason = (
            "FAILED",
            str(error) if isinstance(error, ValueError) else type(error).__name__,
        )

        def finish(s):
            s["slots"][key].update(status=status, reason=reason)
            return s

        paper.store.transaction(finish)
        return dict(status=status, slot=key, reason=reason)
    return _apply_saved(paper, key, instant(saved["recorded_at"]))
