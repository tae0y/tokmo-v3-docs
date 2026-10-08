"""N7: Pension reminder delivery worker module.

Owns the claim→send→settle cycle using the kernel's
claim_pending_telegram → injected telegram_send callable → mark_sent/mark_failed.
"""
from collections.abc import Callable
from datetime import datetime

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import AlertStatus, ProducerRole


def run_pension_reminder_delivery(
    *,
    ops_kernel: OpsKernelStore,
    role: ProducerRole,
    telegram_send: Callable[[str], str],
    now: datetime,
) -> dict:
    """Claim one pending TELEGRAM alert, send it, settle it.

    Returns: {"claimed": int, "sent": int, "failed": int}

    M7: transport success + settle failure → UNKNOWN, never FAILED.
    The telegram_send and mark_sent are in separate try blocks so that
    a kernel-settle exception after a successful transport send does
    not incorrectly record the alert as FAILED (which could trigger
    a resend of an already-delivered message).
    """
    result = {"claimed": 0, "sent": 0, "failed": 0}
    claimed = ops_kernel.claim_pending_telegram(role, now)
    if claimed is None:
        return result
    result["claimed"] = 1
    body = claimed.body_evidence[0] if claimed.body_evidence else ""

    # Phase 1: transport send (determines sent vs failed)
    transport_ok = False
    provider_message_id = None
    try:
        provider_message_id = telegram_send(body)
        transport_ok = True
    except TimeoutError:
        ops_kernel.mark_failed(
            role, claimed.alert_id,
            failed_at=now, reason="provider_timeout",
        )
        result["failed"] = 1
        return result
    except Exception:
        ops_kernel.mark_failed(
            role, claimed.alert_id,
            failed_at=now, reason="provider_failed",
        )
        result["failed"] = 1
        return result

    # Phase 2: kernel settle (transport succeeded)
    # If mark_sent itself raises, the message WAS delivered but we
    # cannot confirm settlement — record UNKNOWN, never FAILED.
    try:
        ops_kernel.mark_sent(
            role, claimed.alert_id,
            delivered_at=now, provider_message_id=provider_message_id,
        )
        result["sent"] = 1
    except Exception:
        # Transport succeeded but kernel settle failed.
        # Do NOT mark_failed — the message was delivered.
        # Mark as UNKNOWN so it is never retransmitted.
        ops_kernel.mark_unknown(
            role, claimed.alert_id,
            settled_at=now, reason="settle_failed_after_delivery",
        )
        result["sent"] = 1  # transport-level success
    return result
