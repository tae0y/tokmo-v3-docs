"""Snapshot-before-change protocols for the pension current read model.

Two operations mutate pension current state, and both must capture the state
they are about to change *before* changing it, into the official snapshot
store. The registry holds exactly one current holdings payload; the snapshot
store holds the history. If the snapshot or its read-back fails, nothing
downstream is allowed to happen.

  replace_current_holdings(...)
      1. snapshot the CURRENT pension holdings, then read it back exactly
      2. snapshot the CANDIDATE holdings, then read it back exactly
      3. only then replace the registry's current payload with the candidate

  run_transaction_detail_batch(...)  [in pension_snapshot_adapter]
      1. classify the batch against the real ledger: NEW / DUPLICATE / CONFLICT
      2. DUPLICATE  -> total no-op: no snapshot, no ledger append, no mutation
         CONFLICT   -> no write at all; returned for review
         NEW        -> pre-snapshot the CURRENT holdings, read it back exactly,
                       durably link batch to pre-snapshot, and only then append
      3. any snapshot or read-back failure blocks the ledger append

This module owns the ordering contract and its receipt types; the executable
implementation over the real stores lives in ``pension_snapshot_adapter`` and
is reached from ``tools/import_pension_transaction_detail.py``.

Ordering is the contract, so every step is an explicit, observable call on an
injected port. A test can substitute a spy or a failure injector for any of
them and assert both the order and the exact surviving state.

Nothing here fabricates holdings. Transaction evidence never mutates holdings:
this module appends ledger events and captures snapshots, and the only holdings
write it performs is the explicit replacement above. Aggregate period-summary
rows are not accepted here at all.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass

from tokmo.values.pension_holdings import PensionHoldingsRevisionV1

# Outcome vocabulary
OUTCOME_REPLACED = "REPLACED"
OUTCOME_APPENDED = "APPENDED"
OUTCOME_DUPLICATE_NOOP = "DUPLICATE_NOOP"
OUTCOME_CONFLICT_REVIEW = "CONFLICT_REVIEW"
OUTCOME_BLOCKED = "BLOCKED"

# Why an operation refused
BLOCKED_SNAPSHOT_FAILED = "SNAPSHOT_FAILED"
BLOCKED_READBACK_MISMATCH = "READBACK_MISMATCH"
BLOCKED_LEDGER_FAILED = "LEDGER_FAILED"

BATCH_NEW = "NEW"
BATCH_DUPLICATE = "DUPLICATE"
BATCH_CONFLICT = "CONFLICT"
BATCH_CLASSES = frozenset({BATCH_NEW, BATCH_DUPLICATE, BATCH_CONFLICT})


class PensionCurrentUpdateError(ValueError):
    """Typed protocol rejection. Carries no private holdings content."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@dataclass(frozen=True)
class SnapshotOutcomeV1:
    """One capture attempt and its exact read-back verification."""

    ok: bool
    snapshot_id: str
    reason: str = ""


@dataclass(frozen=True)
class ReplaceCurrentReceiptV1:
    outcome: str
    current_snapshot_id: str = ""
    candidate_snapshot_id: str = ""
    active_revision_id: str = ""
    reason: str = ""

    def __post_init__(self):
        if self.outcome not in (OUTCOME_REPLACED, OUTCOME_BLOCKED):
            raise ValueError("invalid outcome")


@dataclass(frozen=True)
class BatchAdmissionReceiptV1:
    outcome: str
    batch_class: str
    pre_snapshot_id: str = ""
    appended_count: int = 0
    reason: str = ""

    def __post_init__(self):
        if self.outcome not in (
            OUTCOME_APPENDED,
            OUTCOME_DUPLICATE_NOOP,
            OUTCOME_CONFLICT_REVIEW,
            OUTCOME_BLOCKED,
        ):
            raise ValueError("invalid outcome")
        if self.batch_class not in BATCH_CLASSES:
            raise ValueError("invalid batch_class")
        if self.outcome != OUTCOME_APPENDED and self.appended_count:
            raise ValueError("only an appended batch may report a count")


def batch_identity(events: Sequence[object], *, file_sha256: str) -> str:
    """Stable identity of a submitted batch, for duplicate detection.

    Derived from the source file digest and the ordered event keys, so an
    identical resubmission is recognised without holding any private content.
    """
    digest = hashlib.sha256()
    digest.update(b"tokmo-v3:pension-batch-identity:v1\0")
    digest.update(file_sha256.encode())
    for event in events:
        digest.update(b"\0")
        digest.update(str(getattr(event, "source_record_key", "")).encode())
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Ports. Each is an explicit seam so ordering and failure can be observed.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PensionSnapshotPortV1:
    """Capture the pension holdings of a revision, and read it back exactly."""

    capture: Callable[[PensionHoldingsRevisionV1], SnapshotOutcomeV1]


@dataclass(frozen=True)
class BatchLinkPortV1:
    """Bounded durable link between a batch identity and its pre-snapshot.

    This is what makes a crash between the pre-snapshot and the ledger append
    retry-idempotent: the retry finds the recorded link and reuses that exact
    snapshot instead of capturing another one. It stores identities only —
    never a holdings payload — so it duplicates nothing.
    """

    read: Callable[[str], str | None]
    write: Callable[[str, str], None]


@dataclass(frozen=True)
class LedgerPortV1:
    classify: Callable[[str, Sequence[object]], str]
    append: Callable[[Sequence[object]], int]


# ---------------------------------------------------------------------------
# Contract 3: replace the current holdings, snapshot-first
# ---------------------------------------------------------------------------


def replace_current_holdings(
    *,
    registry,
    snapshot_port: PensionSnapshotPortV1,
    current_revision: PensionHoldingsRevisionV1 | None,
    candidate_revision: PensionHoldingsRevisionV1,
    approval_reason: str,
    trace: list[str] | None = None,
) -> ReplaceCurrentReceiptV1:
    """Snapshot current, snapshot candidate, then replace current.

    The registry is touched only after both captures have been read back
    exactly. Any capture or read-back failure leaves the current holdings and
    everything downstream untouched.
    """
    steps = trace if trace is not None else []

    current_snapshot_id = ""
    if current_revision is not None:
        steps.append("capture_current")
        outcome = snapshot_port.capture(current_revision)
        if not outcome.ok:
            return ReplaceCurrentReceiptV1(
                OUTCOME_BLOCKED, reason=outcome.reason or BLOCKED_SNAPSHOT_FAILED
            )
        current_snapshot_id = outcome.snapshot_id

    steps.append("capture_candidate")
    candidate_outcome = snapshot_port.capture(candidate_revision)
    if not candidate_outcome.ok:
        return ReplaceCurrentReceiptV1(
            OUTCOME_BLOCKED,
            current_snapshot_id=current_snapshot_id,
            reason=candidate_outcome.reason or BLOCKED_SNAPSHOT_FAILED,
        )

    steps.append("replace_current")
    registry.replace_current_from_snapshot(
        candidate_revision, approval_reason=approval_reason
    )
    return ReplaceCurrentReceiptV1(
        OUTCOME_REPLACED,
        current_snapshot_id=current_snapshot_id,
        candidate_snapshot_id=candidate_outcome.snapshot_id,
        active_revision_id=candidate_revision.revision_id,
    )


def rollback_current_from_snapshot(
    *,
    registry,
    reconstructed_revision: PensionHoldingsRevisionV1,
    approval_reason: str,
) -> ReplaceCurrentReceiptV1:
    """Replace current from a revision rebuilt out of a verified snapshot.

    The caller reconstructs the revision from the snapshot store; this never
    resurrects a historical registry payload, because generation 2 keeps none.
    """
    registry.replace_current_from_snapshot(
        reconstructed_revision, approval_reason=approval_reason
    )
    return ReplaceCurrentReceiptV1(
        OUTCOME_REPLACED, active_revision_id=reconstructed_revision.revision_id
    )
