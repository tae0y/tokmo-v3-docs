"""Read-only NEWS G3 preflight receipt (classifier route + live lane).

Proves, without creating or mutating any store, the selected classifier
provider/model and its single-route config state, the route-authority
freshness judgment, the worker timeout/budget bounds, the async work-queue
and classifier-call counts, the canary/predecessor/trip ledger state, and
the NEWS Telegram outbox statuses with the external transport-start count.
Every database opens read-only via URI mode=ro; absent files and absent
tables read as empty state. No alert bodies, prompts, account identifiers,
or secrets appear in the output.
"""
import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from tokmo.programs.news_alert_delivery import live_delivery_authorized
from tokmo.programs.pulse_production_deps import (
    NEWS_DELIVERY_OFF,
    read_news_gate,
)
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_PROVIDER,
    NewsClassifierFailure,
    NewsClassifierRouteError,
    read_classifier_model,
)
from tokmo.values.news_config import NEWS_DAILY_MODEL_CALL_CAP
from tokmo.values.ops_kernel import created_on_or_after


class _ReadOnlyLedger:
    """Ledger reads over mode=ro SQLite; absent state reads as empty."""

    def __init__(self, path):
        self._path = Path(path)

    def _rows(self, query, parameters=()):
        if not self._path.exists():
            return None
        try:
            connection = sqlite3.connect(
                f"{self._path.resolve().as_uri()}?mode=ro", uri=True,
                timeout=0.25)
        except sqlite3.Error:
            return None
        try:
            return connection.execute(query, parameters).fetchall()
        except sqlite3.Error:
            return None
        finally:
            connection.close()

    def canary_attempt(self, activation_id):
        rows = self._rows(
            "SELECT activation_id,ops_alert_id,reserved_at,outcome,"
            "settled_at FROM canary_attempts WHERE activation_id=?",
            (activation_id,))
        return None if not rows else dict(zip(
            ("activation_id", "ops_alert_id", "reserved_at", "outcome",
             "settled_at"), rows[0]))

    def canary_slot_proof(self, activation_id):
        rows = self._rows(
            "SELECT activation_id,settled_outcome,slots_json,"
            "transport_starts,recorded_at FROM canary_slot_proofs"
            " WHERE activation_id=?", (activation_id,))
        return None if not rows else dict(zip(
            ("activation_id", "settled_outcome", "slots_json",
             "transport_starts", "recorded_at"), rows[0]))

    def live_trip(self, activation_id):
        rows = self._rows(
            "SELECT activation_id,reason,tripped_at FROM live_delivery_trips"
            " WHERE activation_id=?", (activation_id,))
        return None if not rows else dict(zip(
            ("activation_id", "reason", "tripped_at"), rows[0]))

    def work_queue_counts(self):
        rows = self._rows(
            "SELECT state,count(*) FROM classifier_work_queue"
            " GROUP BY state ORDER BY state")
        return dict(rows or ())

    def classifier_call_counts(self):
        rows = self._rows(
            "SELECT state,COALESCE(exit,''),count(*) FROM classifier_calls"
            " GROUP BY state,exit ORDER BY state,exit")
        counts = {}
        for state, exit_code, count in (rows or ()):
            key = f"{state}:{exit_code}" if exit_code else state
            counts[key] = count
        return counts


def _route_authority_state(routing_state_path, quota_latest_path, now):
    if routing_state_path is None or quota_latest_path is None:
        return None
    from tokmo.programs.news_classifier_executor import (
        build_route_authority_reader,
    )
    reader = build_route_authority_reader(
        routing_state_path=routing_state_path,
        quota_latest_path=quota_latest_path, now=now)
    try:
        authority = reader(None)
    except NewsClassifierFailure as failure:
        return {"state": failure.exit_code}
    return {"state": "OK",
            "observed_at": authority["quota_latest"]["observed_at"],
            "routing_state_sha256": authority["routing_state"]["sha256"],
            "quota_latest_sha256": authority["quota_latest"]["sha256"]}


def read_news_preflight(root, *, now, config_path=None,
                        routing_state_path=None, quota_latest_path=None,
                        timeout_seconds=None):
    from tokmo.programs.news_classifier_worker import (
        WORKER_CALL_TIMEOUT_SECONDS,
    )
    root = Path(root)
    snapshot = read_news_gate(root)
    activation, producer_mode = snapshot
    delivery = getattr(snapshot, "delivery", NEWS_DELIVERY_OFF)
    ledger = _ReadOnlyLedger(root / "stores" / "news_episodes.sqlite3")
    config = Path(config_path) if config_path is not None \
        else root / "news_classifier_model.json"
    try:
        configured_model = read_classifier_model(config)
        config_state = "FILE" if config.exists() else "DEFAULT"
    except NewsClassifierRouteError:
        configured_model = None
        config_state = "INVALID"
    off_reasons = []
    if delivery.mode == "TELEGRAM_LIVE":
        authorized, reason = live_delivery_authorized(ledger, delivery, now)
        if not authorized:
            off_reasons.append(reason)
    elif delivery.mode == "TELEGRAM_CANARY":
        if delivery.cutoff is not None and now < delivery.cutoff:
            off_reasons.append("before_cutoff")
        if delivery.window_end is not None and now >= delivery.window_end:
            off_reasons.append("window_expired")
        if ledger.canary_attempt(delivery.activation_id) is not None:
            off_reasons.append("cap_consumed")
    else:
        off_reasons.append("delivery_off")
    predecessor = delivery.predecessor_activation_id
    ops_ledger = _ReadOnlyLedger(root / "ops" / "ops_kernel.sqlite3")
    outbox_rows = ops_ledger._rows(
        "SELECT status,count(*),sum(attempted_at IS NOT NULL)"
        " FROM alert_outbox WHERE channel='TELEGRAM'"
        " AND origin IN ('NEWS_EVENT','NEWS_OPS') GROUP BY status")
    telegram_outbox = {}
    transport_starts = 0
    for status, count, started in (outbox_rows or ()):
        telegram_outbox[status if status is not None else "NONE"] = count
        transport_starts += started or 0
    # Post-cutoff transport-eligible PENDING count (incident 2026-08-22):
    # mirrors the delivery selector's semantic created_at>=cutoff judgment
    # over the origins the active mode would claim from.
    pending_post_cutoff = None
    if delivery.cutoff is not None:
        selector_origins = "('NEWS_EVENT')" \
            if delivery.mode == "TELEGRAM_CANARY" \
            else "('NEWS_EVENT','NEWS_OPS')"
        pending_rows = ops_ledger._rows(
            "SELECT created_at FROM alert_outbox WHERE channel='TELEGRAM'"
            " AND status='PENDING' AND resolution='UNRESOLVED'"
            " AND origin IN " + selector_origins)
        pending_post_cutoff = sum(
            1 for (created,) in (pending_rows or ())
            if created_on_or_after(created, delivery.cutoff))
    return {
        "read_at": now.astimezone(timezone.utc).isoformat(),
        "gate": {
            "enabled": activation.enabled,
            "producer_mode": producer_mode,
            "delivery_mode": delivery.mode,
            "activation_id": delivery.activation_id,
            "predecessor_activation_id": predecessor,
            "cutoff": None if delivery.cutoff is None
            else delivery.cutoff.isoformat(),
            "window_end": None if delivery.window_end is None
            else delivery.window_end.isoformat(),
        },
        "classifier": {
            "configured_model": configured_model,
            "provider": NEWS_CLASSIFIER_PROVIDER
            if configured_model == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
            else None,
            "config_state": config_state,
            "retry_max": 0,
            "daily_call_cap": NEWS_DAILY_MODEL_CALL_CAP,
            "timeout_seconds": timeout_seconds
            if timeout_seconds is not None else WORKER_CALL_TIMEOUT_SECONDS,
        },
        "route_authority": _route_authority_state(
            routing_state_path, quota_latest_path, now),
        "work_queue": ledger.work_queue_counts(),
        "classifier_calls": ledger.classifier_call_counts(),
        "predecessor": None if predecessor is None else {
            "attempt": ledger.canary_attempt(predecessor),
            "slot_proof": ledger.canary_slot_proof(predecessor),
        },
        "live_trip": None if delivery.activation_id is None
        else ledger.live_trip(delivery.activation_id),
        "telegram_outbox": telegram_outbox,
        "telegram_pending_post_cutoff": pending_post_cutoff,
        "telegram_transport_starts": transport_starts,
        "delivery_enabled_now": not off_reasons,
        "off_reasons": off_reasons,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Read-only NEWS G3 preflight receipt.")
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", required=True,
                        help="aware ISO-8601 judgment instant")
    parser.add_argument("--config")
    parser.add_argument("--routing-state")
    parser.add_argument("--quota-latest")
    arguments = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now must be ISO-8601")
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("--now must be timezone-aware")
    receipt = read_news_preflight(
        Path(arguments.root), now=now,
        config_path=arguments.config,
        routing_state_path=arguments.routing_state,
        quota_latest_path=arguments.quota_latest)
    print(json.dumps(receipt, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
