"""One-shot, success-silent macro shadow policy."""

import hashlib
from dataclasses import dataclass
from datetime import date, datetime


@dataclass(frozen=True)
class MacroRuntimePolicyV1:
    enabled: bool
    policy: str

    def __post_init__(self):
        if self.policy not in ("PAUSED", "SHADOW"):
            raise ValueError("invalid policy")


@dataclass(frozen=True)
class MacroRuntimeTriggerV1:
    scheduled_at: datetime
    completed_session: date | None
    replay: bool = False


@dataclass(frozen=True)
class MacroRuntimeResultV1:
    identity: str | None
    status: str
    starts: int


def eligible_completed_session(completed_session, now):
    return (
        type(completed_session) is date
        and isinstance(now, datetime)
        and now.tzinfo is not None
        and completed_session <= now.date()
    )


def macro_dedupe_identity(completed_session):
    if type(completed_session) is not date:
        raise ValueError("session required")
    return hashlib.sha256(
        (
            "AI-MACRO-001|v1|closed-formula|completed-session|"
            + completed_session.isoformat()
            + "|initial-three-reads"
        ).encode()
    ).hexdigest()


def run_macro_runtime_once(policy, trigger, receipt_store, evaluate):
    pause_identity = hashlib.sha256(b"AI-MACRO-001|GLOBAL_PAUSE|v1").hexdigest()
    if trigger.replay and eligible_completed_session(
        trigger.completed_session, trigger.scheduled_at
    ):
        identity = macro_dedupe_identity(trigger.completed_session)
        receipt = receipt_store.read_receipt(identity)
        if receipt is None or receipt.get("state") not in (
            "SUCCEEDED",
            "FAILED",
            "UNKNOWN",
        ):
            return {"identity": identity, "status": "REPLAY_UNAVAILABLE", "starts": 0}
        return {
            "identity": identity,
            "status": "REPLAY",
            "starts": 0,
            "evidence_digest": receipt.get("evidence_digest"),
        }
    if (
        not policy.enabled
        or policy.policy != "SHADOW"
        or not eligible_completed_session(
            trigger.completed_session, trigger.scheduled_at
        )
    ):
        return {"identity": None, "status": "SKIPPED", "starts": 0}
    if receipt_store.read_receipt(pause_identity) is not None:
        return {"identity": None, "status": "GLOBAL_PAUSED", "starts": 0}
    identity = macro_dedupe_identity(trigger.completed_session)
    if receipt_store.read_receipt(identity) is not None:
        return {"identity": identity, "status": "DUPLICATE", "starts": 0}
    receipt_store.reserve(
        identity,
        {
            "completed_session": trigger.completed_session.isoformat(),
            "scheduled_at": trigger.scheduled_at.isoformat(),
        },
    )
    try:
        result = evaluate()
        starts = int(result.get("starts", 0))
        if starts < 0 or starts > 4:
            raise ValueError("start cap")
        status = result.get("status", "FIRING_RATE_UNPROVEN")
        evidence_digest = result.get("evidence_digest")
        previous = []
        for path in receipt_store.root.glob("*.json"):
            try:
                row = receipt_store.read_receipt(path.stem)
            except ValueError:
                continue
            if (
                row is not None
                and row.get("completed_session") is not None
                and row.get("state") == "SUCCEEDED"
            ):
                previous.append(row)
        prior = max(
            previous, key=lambda row: row.get("forward_denominator", 0), default={}
        )
        denominator = int(prior.get("forward_denominator", 0)) + 1
        numerator = int(prior.get("forward_numerator", 0)) + (
            1 if result.get("fired") is True else 0
        )
        from tokmo.programs.macro_canary_adapter import compute_firing_rate_status

        forward_status, forward_rate = compute_firing_rate_status(
            denominator, numerator, normative=False
        )
        terminal = "FAILED" if status == "FAILED" else "SUCCEEDED"
        fields = {
            "starts": starts,
            "calibration_status": status,
            "evidence_digest": evidence_digest,
            "forward_denominator": denominator,
            "forward_numerator": numerator,
            "forward_status": forward_status,
            "forward_rate": forward_rate,
        }
        for key in (
            "pack_digest",
            "source_digest",
            "spec_digest",
            "lifecycle_digest",
            "request_manifest_digest",
            "request_cap",
            "continuity",
            "common_count",
            "common_start",
            "common_end",
            "fred_status",
            "fred_schedule_source",
            "fred_release_slot",
            "fred_due_state",
            "fred_coverage",
            "fred_provider_starts",
            "fred_bls_starts",
            "fred_query_identity",
            "fred_revision_identity",
            "fred_provenance_digest",
            "fred_direction",
            "fred_current_value",
            "fred_current_date",
            "fred_prior_value",
            "fred_prior_date",
            "fred_error_class",
        ):
            fields[key] = result.get(key)
        receipt_store.settle(identity, terminal, fields)
        if terminal == "FAILED":
            try:
                receipt_store.reserve(
                    pause_identity, {"reason": "MATERIAL_PROVIDER_FAILURE"}
                )
                receipt_store.settle(pause_identity, "FAILED", {"starts": 0})
            except ValueError:
                pass
        return {
            "identity": identity,
            "status": status,
            "starts": starts,
            "evidence_digest": evidence_digest,
        }
    except Exception:
        receipt_store.settle(
            identity,
            "FAILED",
            {"starts": 0, "calibration_status": "FIRING_RATE_UNPROVEN"},
        )
        try:
            receipt_store.reserve(
                pause_identity, {"reason": "MATERIAL_RUNTIME_FAILURE"}
            )
            receipt_store.settle(pause_identity, "FAILED", {"starts": 0})
        except ValueError:
            pass
        return {"identity": identity, "status": "FAILED", "starts": 0}
