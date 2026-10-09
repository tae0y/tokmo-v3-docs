"""Side-effect-free NEWS G3 canary readback (approval packet §3/§4).

Proves, without creating or mutating any store, (a) whether Telegram
delivery is enabled at an instant, (b) the durable attempt-ledger row for
the gate's activation_id, and (c) the NEWS Telegram outbox statuses and
transport-start count. Every database is opened read-only via URI mode=ro;
absent files and absent tables read as empty state. No alert bodies,
account identifiers, or secrets appear in the output.
"""

import argparse
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from tokmo.programs.pulse_production_deps import (
    NEWS_DELIVERY_OFF,
    read_news_gate,
)
from tokmo.values.ops_kernel import created_on_or_after

_LEDGER_COLUMNS = (
    "activation_id",
    "ops_alert_id",
    "reserved_at",
    "outcome",
    "settled_at",
)


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


def _ledger_attempt(root, activation_id):
    rows = _read_only_rows(
        root / "stores" / "news_episodes.sqlite3",
        "SELECT activation_id,ops_alert_id,reserved_at,outcome,settled_at"
        " FROM canary_attempts WHERE activation_id=?",
        (activation_id,),
    )
    return dict(zip(_LEDGER_COLUMNS, rows[0])) if rows else None


def read_news_canary_state(root, *, now):
    root = Path(root)
    snapshot = read_news_gate(root)
    activation, producer_mode = snapshot
    delivery = getattr(snapshot, "delivery", NEWS_DELIVERY_OFF)
    attempt = None
    if delivery.activation_id is not None:
        attempt = _ledger_attempt(root, delivery.activation_id)
    outbox_rows = _read_only_rows(
        root / "ops" / "ops_kernel.sqlite3",
        "SELECT status,count(*),sum(attempted_at IS NOT NULL)"
        " FROM alert_outbox WHERE channel='TELEGRAM'"
        " AND origin IN ('NEWS_EVENT','NEWS_OPS') GROUP BY status",
    )
    telegram_outbox = {}
    transport_starts = 0
    for status, count, started in outbox_rows or ():
        telegram_outbox[status if status is not None else "NONE"] = count
        transport_starts += started or 0
    # Post-cutoff transport-eligible PENDING count (incident 2026-08-22):
    # same semantic created_at>=cutoff judgment the delivery selector
    # enforces, over the origins that mode would claim from.
    pending_post_cutoff = None
    if delivery.cutoff is not None:
        selector_origins = (
            "('NEWS_EVENT')"
            if delivery.mode == "TELEGRAM_CANARY"
            else "('NEWS_EVENT','NEWS_OPS')"
        )
        pending_rows = _read_only_rows(
            root / "ops" / "ops_kernel.sqlite3",
            "SELECT created_at FROM alert_outbox WHERE channel='TELEGRAM'"
            " AND status='PENDING' AND resolution='UNRESOLVED'"
            " AND origin IN " + selector_origins,
        )
        pending_post_cutoff = sum(
            1
            for (created,) in (pending_rows or ())
            if created_on_or_after(created, delivery.cutoff)
        )
    predecessor = delivery.predecessor_activation_id
    predecessor_attempt = None
    slot_proof = None
    live_trip = None
    off_reasons = []
    if delivery.mode == "TELEGRAM_LIVE":
        predecessor_attempt = _ledger_attempt(root, predecessor)
        proof_rows = _read_only_rows(
            root / "stores" / "news_episodes.sqlite3",
            "SELECT activation_id,settled_outcome,slots_json,"
            "transport_starts,recorded_at FROM canary_slot_proofs"
            " WHERE activation_id=?",
            (predecessor,),
        )
        if proof_rows:
            slot_proof = dict(
                zip(
                    (
                        "activation_id",
                        "settled_outcome",
                        "slots_json",
                        "transport_starts",
                        "recorded_at",
                    ),
                    proof_rows[0],
                )
            )
        trip_rows = _read_only_rows(
            root / "stores" / "news_episodes.sqlite3",
            "SELECT activation_id,reason,tripped_at FROM live_delivery_trips"
            " WHERE activation_id=?",
            (delivery.activation_id,),
        )
        if trip_rows:
            live_trip = dict(
                zip(("activation_id", "reason", "tripped_at"), trip_rows[0])
            )
        if now < delivery.cutoff:
            off_reasons.append("before_cutoff")
        if now >= delivery.window_end:
            off_reasons.append("window_expired")
        if predecessor_attempt is None or predecessor_attempt["outcome"] != "SENT":
            off_reasons.append("predecessor_unproven")
        elif slot_proof is None:
            off_reasons.append("proof_missing")
        if live_trip is not None:
            off_reasons.append("live_tripped")
    elif delivery.mode != "TELEGRAM_CANARY":
        off_reasons.append("gate_not_canary")
    else:
        if delivery.cutoff is not None and now < delivery.cutoff:
            off_reasons.append("before_cutoff")
        if delivery.window_end is not None and now >= delivery.window_end:
            off_reasons.append("window_expired")
        if attempt is not None:
            off_reasons.append("cap_consumed")
    return {
        "read_at": now.astimezone(UTC).isoformat(),
        "gate_enabled": activation.enabled,
        "producer_mode": producer_mode,
        "delivery_mode": delivery.mode,
        "activation_id": delivery.activation_id,
        "predecessor_activation_id": predecessor,
        "cutoff": None if delivery.cutoff is None else delivery.cutoff.isoformat(),
        "window_end": None
        if delivery.window_end is None
        else delivery.window_end.isoformat(),
        "telegram_attempt_cap": delivery.attempt_cap,
        "delivery_enabled_now": not off_reasons,
        "off_reasons": off_reasons,
        "attempt": attempt,
        "predecessor_attempt": predecessor_attempt,
        "slot_proof": slot_proof,
        "live_trip": live_trip,
        "telegram_outbox": telegram_outbox,
        "telegram_pending_post_cutoff": pending_post_cutoff,
        "telegram_transport_starts": transport_starts,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only NEWS G3 canary state (no store creation)."
    )
    parser.add_argument("--root", required=True)
    # The ambient clock stays in the composition root (live contract §7):
    # the operator states the judgment instant explicitly.
    parser.add_argument("--now", required=True, help="aware ISO-8601 judgment instant")
    arguments = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now must be ISO-8601")
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("--now must be timezone-aware")
    state = read_news_canary_state(Path(arguments.root), now=now)
    print(json.dumps(state, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
