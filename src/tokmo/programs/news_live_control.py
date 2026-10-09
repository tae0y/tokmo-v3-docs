"""NEWS live-lane control: canary duplicate-observation proof + rollback.

``prove-canary`` converts the passive rollback read-back (approval packet
§3 step 4) into the durable ledger row TELEGRAM_LIVE requires: it refuses
unless the canary attempt settled SENT, at least two natural pulse slots
completed after settlement, and the canonical outbox shows exactly one
post-cutoff NEWS Telegram transport start — the canary attempt's own row
under the current gate's bound TELEGRAM_CANARY cutoff (incident
2026-08-22: historical pre-cutoff transport starts never veto). All
evidence reads are read-only; only the proof row itself is written; the
gate file is never mutated and nothing here promotes to live.

``rollback`` is the delivery-only manual OFF: it records a durable live
trip for the activation. It never edits the gate file, never stops the
SHADOW producer or any unrelated pulse lane, and has no undo path.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tokmo.programs.pulse_production_deps import read_news_gate
from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.values.ops_kernel import created_on_or_after


class NewsLiveControlError(ValueError):
    pass


def _read_only_rows(path, query, parameters=()):
    if not Path(path).exists():
        return None
    try:
        connection = sqlite3.connect(
            f"{Path(path).resolve().as_uri()}?mode=ro", uri=True, timeout=0.25
        )
    except sqlite3.Error:
        return None
    try:
        return connection.execute(query, parameters).fetchall()
    except sqlite3.Error:
        return None
    finally:
        connection.close()


def record_canary_duplicate_proof(*, root, activation_id, now, store=None):
    """Record the durable two-slot zero-duplicate proof for one canary.

    The transport-start judgment is bounded, not global: it binds to the
    current gate's TELEGRAM_CANARY delivery for exactly this activation
    and takes the aware cutoff from that gate. Only semantically
    post-cutoff attempted NEWS Telegram rows count
    (``created_on_or_after``; a missing, malformed, or naive created_at
    fails closed to excluded), exactly one such row may exist, and it
    must be the canary attempt's own ``ops_alert_id`` row — so historical
    pre-cutoff transport starts never veto, while any second post-cutoff
    start or a foreign post-cutoff start refuses the proof. The stored
    ``transport_starts=1`` records that bounded post-cutoff count.
    """
    root = Path(root)
    ledger = (
        store
        if store is not None
        else NewsEpisodeStore(root / "stores" / "news_episodes.sqlite3")
    )
    attempt = ledger.canary_attempt(activation_id)
    if attempt is None or attempt["outcome"] != "SENT":
        raise NewsLiveControlError("canary attempt is not durably SENT")
    proof = ledger.canary_slot_proof(activation_id)
    if proof is not None:
        return {
            "replay": True,
            "slots": json.loads(proof["slots_json"]),
            "transport_starts": proof["transport_starts"],
        }
    delivery = read_news_gate(root).delivery
    if (
        delivery.mode != "TELEGRAM_CANARY"
        or delivery.activation_id != activation_id
        or delivery.cutoff is None
    ):
        raise NewsLiveControlError(
            "gate carries no TELEGRAM_CANARY delivery for activation"
            f" {activation_id!r}; refusing proof without a bound cutoff"
        )
    attempted = (
        _read_only_rows(
            root / "ops" / "ops_kernel.sqlite3",
            "SELECT alert_id,created_at FROM alert_outbox"
            " WHERE channel='TELEGRAM'"
            " AND origin IN ('NEWS_EVENT','NEWS_OPS')"
            " AND attempted_at IS NOT NULL",
        )
        or []
    )
    post_cutoff = tuple(
        alert_id
        for alert_id, created in attempted
        if created_on_or_after(created, delivery.cutoff)
    )
    if len(post_cutoff) != 1:
        raise NewsLiveControlError(
            f"expected exactly one post-cutoff NEWS Telegram transport"
            f" start, observed {len(post_cutoff)}"
        )
    if post_cutoff[0] != attempt["ops_alert_id"]:
        raise NewsLiveControlError(
            "the sole post-cutoff transport start is not the canary"
            " attempt's own outbox row; refusing proof"
        )
    transport_starts = 1  # the bounded post-cutoff count just proven
    settled_at = datetime.fromisoformat(attempt["settled_at"]).astimezone(UTC)
    # pulse receipts store "%Y-%m-%dT%H:%M:%S.%fZ"; compare in that format
    slots = (
        _read_only_rows(
            root / "ops" / "pulse_tick_receipts.sqlite3",
            "SELECT scheduled_for_utc FROM pulse_tick_receipts"
            " WHERE started_at>? AND status IN ('SAVED','PARTIAL')"
            " ORDER BY scheduled_for_utc",
            (settled_at.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),),
        )
        or []
    )
    if len(slots) < 2:
        raise NewsLiveControlError(
            "fewer than two completed pulse slots after canary settlement"
        )
    observed = tuple(row[0] for row in slots[:2])
    ledger.record_canary_slot_proof(activation_id, observed, transport_starts, now=now)
    return {
        "replay": False,
        "slots": list(observed),
        "transport_starts": transport_starts,
        "recorded_at": now.astimezone(UTC).isoformat(),
    }


def record_manual_live_rollback(*, root, activation_id, reason, now, store=None):
    """Durable delivery-only OFF for one live activation; no gate edit."""
    root = Path(root)
    ledger = (
        store
        if store is not None
        else NewsEpisodeStore(root / "stores" / "news_episodes.sqlite3")
    )
    created = ledger.record_live_trip(activation_id, reason, now=now)
    trip = ledger.live_trip(activation_id)
    return {"tripped": True, "created": created, "trip": trip}


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="NEWS live-lane proof and delivery-only rollback."
    )
    parser.add_argument("command", choices=("prove-canary", "rollback"))
    parser.add_argument("--root", required=True)
    parser.add_argument("--activation-id", required=True)
    parser.add_argument("--now", required=True, help="aware ISO-8601 judgment instant")
    parser.add_argument("--reason", default="manual_rollback")
    arguments = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now must be ISO-8601")
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("--now must be timezone-aware")
    if arguments.command == "prove-canary":
        report = record_canary_duplicate_proof(
            root=Path(arguments.root), activation_id=arguments.activation_id, now=now
        )
    else:
        report = record_manual_live_rollback(
            root=Path(arguments.root),
            activation_id=arguments.activation_id,
            reason=arguments.reason,
            now=now,
        )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
