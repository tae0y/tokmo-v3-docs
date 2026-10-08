"""Pension materialization: ACTIVE revision → typed holdings → composite integration.

Evaluates freshness and reminder events.
"""
from __future__ import annotations

import hashlib
from datetime import datetime
from zoneinfo import ZoneInfo

from tokmo.values.pension_holdings import (
    PensionHoldingsRevisionV1,
    PensionFreshnessV1,
    classify_pension_freshness,
    pension_account_scope,
)
from tokmo.records.ops_kernel import OpsKernelStore, OpsKernelConflictError
from tokmo.records.pension_revisions import PensionRegistryStore
from tokmo.values.ops_kernel import (
    AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus, IncidentKeyV1,
    ProducerRole, ReplayResult, alert_dedupe_key,
)


_KST = ZoneInfo("Asia/Seoul")


def materialize_pension_holdings(
    revision: PensionHoldingsRevisionV1,
    *,
    pension_ordinal: int,
) -> dict:
    """Convert an ACTIVE revision to composite-holdings-compatible account data.

    Returns a dict with account_scope, account_type_label, ordinal,
    holdings list, source_provenance, and pension provenance fields.
    """
    account_scope = pension_account_scope()
    holdings = [
        {
            "symbol": h.symbol,
            "quantity": h.quantity,
            "avg_price": h.avg_price,
            "currency": h.currency,
            "market_raw": h.market_raw,
            "market_field": h.market_field,
        }
        for h in revision.holdings
    ]
    return {
        "account_scope": account_scope,
        "account_type_label": "pension-excel-v1",
        "ordinal": pension_ordinal,
        "holdings": holdings,
        "source_provenance": "PENSION_EXCEL",
    }


def build_pension_provenance(
    revision: PensionHoldingsRevisionV1,
    *,
    tick_now: datetime,
) -> dict:
    """Build the pension_provenance sub-object for the composite payload."""
    freshness = classify_pension_freshness(revision.source_as_of, tick_now)
    return {
        "revision_id": revision.revision_id,
        "source_content_sha256": revision.source_content_sha256,
        "semantic_digest": revision.semantic_digest,
        "source_as_of": revision.source_as_of.isoformat(),
        "source_age_days": freshness.source_age_days,
        "freshness_class": freshness.freshness_class,
        "parser_version": revision.parser_version,
    }


def reminder_text(freshness: PensionFreshnessV1) -> str:
    """Sanitized reminder text: age, source-as-of date, update request.

    NO holdings, account identity, quantities, values, or local path.
    """
    kst_as_of = freshness.source_as_of.astimezone(_KST)
    return (
        f"퇴직연금 Excel 기준일 {kst_as_of.strftime('%Y-%m-%d')} "
        f"(만 {freshness.source_age_days}일 경과). "
        f"최신 워크북 업데이트가 필요합니다."
    )


def evaluate_reminder(
    registry: PensionRegistryStore,
    ops_kernel: OpsKernelStore,
    *,
    tick_now: datetime,
    tick_id: int,
    production_mode: bool,
    replay: bool,
    send_enabled: bool,
    role: ProducerRole,
) -> str | None:
    """Reserve and enqueue one naturally due reminder behind both gates.

    Crash-safe ordering (SDS §3.5):
    1. reserve_reminder (durable reservation with pre-registered alert_id)
    2. record_pension_reminder_outbox (commits the alert row)
    3. link_reminder_to_alert

    A surviving non-PENDING outbox row with the same alert_id is treated
    as a DUPLICATE return (already handled), not a conflict.
    """
    if not production_mode or replay or not send_enabled:
        return None
    active = registry.read_active_revision()
    if active is None:
        return None
    freshness = classify_pension_freshness(active.source_as_of, tick_now)
    if freshness.source_age_days < 31:
        return None
    reminder_id = hashlib.sha256(
        f"{active.revision_id}:PENSION_EXCEL_AGE_31D".encode()
    ).hexdigest()
    if registry.read_reminder(reminder_id) is not None:
        return None

    key = IncidentKeyV1("pension-excel-age-31d", active.revision_id)
    ops_kernel.observe_incident(
        role, key=key, impact_ref=f"revision:{active.revision_id}", now=tick_now,
    )
    alert_id = hashlib.sha256(
        f"pension-reminder:{active.revision_id}".encode()
    ).hexdigest()[:32]

    # B3: Reserve FIRST — pre-register alert_id per §3.5 crash matrix.
    # If the process crashes after this commit, the reservation row survives
    # with the alert_id linkage; the next tick sees the reservation and
    # skips, while a surviving PENDING outbox row is delivered normally.
    registry.reserve_reminder(
        reminder_id, active.revision_id, freshness.source_age_days, tick_now,
        alert_id=alert_id,
    )

    identity = f"{key.kind}\0{key.subject}"
    outbox = AlertOutboxV1(
        alert_id=alert_id, origin=AlertOrigin.INCIDENT, decision_id=None,
        incident_kind=key.kind, incident_subject=key.subject, requeue_of=None,
        tick_id=tick_id, subject_id=active.revision_id,
        dedupe_key=alert_dedupe_key(AlertOrigin.INCIDENT, identity, "HIGH"),
        channel=AlertChannel.TELEGRAM, severity="HIGH",
        body_evidence=(reminder_text(freshness),), created_at=tick_now,
        status=AlertStatus.PENDING,
    )
    try:
        result = ops_kernel.record_pension_reminder_outbox(
            role, outbox, activation_enabled=True,
        )
    except OpsKernelConflictError:
        # B3: A surviving outbox row with the same alert_id (from a crash
        # in the previous tick between reserve and outbox-link) is already
        # handled — treat as DUPLICATE, not a conflict.
        registry.link_reminder_to_alert(reminder_id, alert_id)
        return None
    registry.link_reminder_to_alert(reminder_id, alert_id)
    return alert_id if result is ReplayResult.CREATED else None
