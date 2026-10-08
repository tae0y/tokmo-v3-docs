"""Executable adapters binding the pension protocols to the real stores.

This is the production wiring the protocols in ``pension_current_update`` were
written against. Without it those protocols would be abstract helpers with no
caller, so every operational path — the import CLI, the transaction-detail
entrypoint — goes through here.

Three things live here:

  * ``deterministic_snapshot_id`` — a snapshot identity derived from what the
    capture *is*, not from randomness. It is a well-formed version-4 UUID, so
    the official store accepts it, but the same logical capture always yields
    the same id. Combined with the reservation written before the capture,
    this is what makes a crash between the snapshot commit and its read-back
    recoverable: the retry re-appends the same id with the same content, the
    store answers DUPLICATE, and no second snapshot is ever created.

  * ``pension_snapshot_port`` — capture + exact read-back against the real
    ``HoldingsSnapshotStore`` under a real pension permit.

  * ``classify_pension_batch`` / ``run_transaction_detail_batch`` — the real
    transaction-detail path over the real ``TradeLedgerStore``, classified from
    the ledger's own uniqueness key and payload hash.

No holdings payload, private source cell or filesystem path is ever written
into the batch link metadata: identities only.
"""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from uuid import UUID

from tokmo.programs.pension_current_update import (
    BATCH_CONFLICT,
    BATCH_DUPLICATE,
    BATCH_NEW,
    BLOCKED_READBACK_MISMATCH,
    BLOCKED_SNAPSHOT_FAILED,
    OUTCOME_APPENDED,
    OUTCOME_BLOCKED,
    OUTCOME_CONFLICT_REVIEW,
    OUTCOME_DUPLICATE_NOOP,
    BatchAdmissionReceiptV1,
    PensionSnapshotPortV1,
    SnapshotOutcomeV1,
    batch_identity,
)
from tokmo.programs.pension_trade_ingestion import (
    PENSION_TRADE_SCOPE,
    PENSION_TRADE_SOURCE,
)
from tokmo.records.holdings_persistence import authorize_pension_import
from tokmo.records.holdings_snapshots import (
    PENSION_SNAPSHOT_SOURCE,
    canonical_utc_isoformat,
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
    snapshot_content_hash,
)
from tokmo.records.trade_ledger_canonical import (
    canonical_payload_bytes,
    payload_hash_hex,
)
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.pension_holdings import (
    PensionHoldingsRevisionV1,
    pension_semantic_digest,
)

PENSION_IMPORT_PROGRAM = "tokmo-v3-pension-import-v1"

BINDING_CURRENT = "current"
BINDING_CANDIDATE = "candidate"
BINDING_BATCH_PRE = "batch-pre"

_SNAPSHOT_ID_DOMAIN = "tokmo-v3:pension-snapshot-identity:v1"


def deterministic_snapshot_id(*, binding: str, content_hash: str) -> str:
    """A stable version-4-shaped snapshot id for one exact capture.

    RI-01: the identity binds the FULL canonical content hash the official
    store computes -- source, ``captured_at`` (the holdings ``source_as_of``),
    account count, and every holdings field in canonical order -- not just the
    holdings semantic digest. Two captures whose canonical content differs in
    any way, including only ``source_as_of``, therefore get distinct ids, while
    normalization-equivalent content collapses to the same id exactly as the
    store's own hash does.

    ``UUID(bytes=..., version=4)`` forces the version and variant bits, which is
    what the official store's ``is_valid_uuid4_hex`` requires, so a
    deterministic identity is admissible without weakening that check.
    """
    digest = hashlib.sha256(
        f"{_SNAPSHOT_ID_DOMAIN}\0{binding}\0{content_hash}".encode()
    ).digest()
    return UUID(bytes=digest[:16], version=4).hex


def build_pension_snapshot(
    revision: PensionHoldingsRevisionV1, *, binding: str,
) -> CompleteHoldingsSnapshot:
    """The pension holdings of *revision* as an official snapshot capture.

    The content hash is computed first (it deliberately excludes the snapshot
    id), the id is derived from it, and the final value carries both.
    """
    holdings = tuple(sorted((
        TossHoldingFact(
            symbol=item.symbol, quantity=item.quantity,
            avg_price=item.avg_price, avg_price_field="average_cost",
            currency=item.currency, market_raw=item.market_raw,
            market_field=item.market_field, name=None, asset_type=None)
        for item in revision.holdings
    ), key=lambda item: item.symbol))
    accounts = (HoldingsSnapshotAccount(1, holdings),)
    draft = CompleteHoldingsSnapshot(
        "0" * 32, PENSION_SNAPSHOT_SOURCE, revision.source_as_of,
        1, accounts, "0" * 64)
    content_hash = snapshot_content_hash(draft)
    snapshot_id = deterministic_snapshot_id(
        binding=binding, content_hash=content_hash)
    return CompleteHoldingsSnapshot(
        snapshot_id, PENSION_SNAPSHOT_SOURCE, revision.source_as_of,
        1, accounts, content_hash)


def pension_snapshot_identity(
    revision: PensionHoldingsRevisionV1, *, binding: str,
) -> str:
    """The id this capture will have, without touching any store."""
    return build_pension_snapshot(revision, binding=binding).snapshot_id


def pension_snapshot_port(
    store: HoldingsSnapshotStore, *, binding: str,
) -> PensionSnapshotPortV1:
    """Capture into the official store and read the capture back exactly.

    ``DUPLICATE`` is a success: it means this exact capture is already durable,
    which is precisely the state a retry after a crash finds.
    """
    def capture(revision: PensionHoldingsRevisionV1) -> SnapshotOutcomeV1:
        value = build_pension_snapshot(revision, binding=binding)
        # The official store treats a re-append as DUPLICATE only when the
        # permit metadata matches too, and that metadata includes
        # ``authorized_at``. A wall-clock permit would therefore turn every
        # retry-after-restart into a CONFLICT, defeating the deterministic id.
        # The authorization instant is bound to the capture itself, so the
        # whole append is idempotent across processes.
        permit = authorize_pension_import(
            snapshot_id=value.snapshot_id,
            program_name=PENSION_IMPORT_PROGRAM,
            clock=lambda: value.captured_at)
        receipt = store.append(value, permit)
        if receipt.status not in ("SAVED", "DUPLICATE"):
            return SnapshotOutcomeV1(
                False, value.snapshot_id,
                receipt.reason or BLOCKED_SNAPSHOT_FAILED)
        try:
            loaded = store.read_snapshot(value.snapshot_id)
        except HoldingsSnapshotReadError as error:
            return SnapshotOutcomeV1(
                False, value.snapshot_id, error.reason)
        if loaded != value:
            return SnapshotOutcomeV1(
                False, value.snapshot_id, BLOCKED_READBACK_MISMATCH)
        return SnapshotOutcomeV1(True, value.snapshot_id)
    return PensionSnapshotPortV1(capture=capture)


# ---------------------------------------------------------------------------
# Approved transaction-detail authorization
#
# The ledger would eventually refuse an unauthorized source or scope, but only
# AFTER the pre-snapshot and the durable link were written. Authorization
# therefore happens here, before any store is opened, against the exact
# already-approved pension trade contract.
# ---------------------------------------------------------------------------

TRANSACTION_DETAIL_PLAN_VERSION = "pension-transaction-detail-plan-v1"
APPROVED_DETAIL_SOURCE = PENSION_TRADE_SOURCE          # retirement_excel_v1
APPROVED_DETAIL_ACCOUNT_SCOPE = PENSION_TRADE_SCOPE    # retirement_pension_1
APPROVED_DETAIL_IMPORTER_VERSION = "pension-transaction-detail-v1"
# Exactly the two kinds the ledger schema admits; no wildcard.
APPROVED_DETAIL_EVENT_KINDS = frozenset({"EXECUTION", "CORRECTION"})
APPROVED_DETAIL_RECORD_KIND = "TRANSACTION_DETAIL"
REJECTED_AGGREGATE_RECORD_KINDS = frozenset({
    "AGGREGATE_SUMMARY", "PERIOD_SUMMARY", "AGGREGATE",
})
# Every per-execution field a detail row must carry. An aggregate row cannot.
REQUIRED_DETAIL_FIELDS = (
    "source_record_key", "trade_date", "transaction_type_raw",
    "instrument_raw", "quantity", "unit_price", "amount", "medium_raw",
)


class TransactionDetailUnauthorized(ValueError):
    """The batch is not the approved pension transaction-detail contract."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def authorize_transaction_detail_plan(plan) -> None:
    """Refuse anything that is not the approved contract. No inference.

    Raises before any registry, snapshot or ledger object is constructed.
    """
    if not isinstance(plan, dict):
        raise TransactionDetailUnauthorized("plan_not_an_object")
    if plan.get("plan_version") != TRANSACTION_DETAIL_PLAN_VERSION:
        raise TransactionDetailUnauthorized("unauthorized_plan_version")
    if plan.get("source") != APPROVED_DETAIL_SOURCE:
        raise TransactionDetailUnauthorized("unauthorized_source")
    if plan.get("account_scope") != APPROVED_DETAIL_ACCOUNT_SCOPE:
        raise TransactionDetailUnauthorized("unauthorized_account_scope")
    if plan.get("importer_version") != APPROVED_DETAIL_IMPORTER_VERSION:
        raise TransactionDetailUnauthorized("unauthorized_importer_version")
    count = plan.get("expected_record_count")
    if not isinstance(count, int) or isinstance(count, bool) or count < 1:
        raise TransactionDetailUnauthorized("invalid_expected_record_count")
    digest = plan.get("source_sha256")
    if not isinstance(digest, str) or len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest):
        raise TransactionDetailUnauthorized("invalid_source_sha256")


def authorize_transaction_detail_document(document, plan) -> None:
    """Refuse aggregate input and any row without per-execution identity."""
    if not isinstance(document, dict):
        raise TransactionDetailUnauthorized("document_not_an_object")
    record_kind = document.get("record_kind")
    if record_kind in REJECTED_AGGREGATE_RECORD_KINDS:
        raise TransactionDetailUnauthorized("aggregate_input_rejected")
    if record_kind != APPROVED_DETAIL_RECORD_KIND:
        raise TransactionDetailUnauthorized("unauthorized_record_kind")
    rows = document.get("records")
    if not isinstance(rows, list) or not rows:
        raise TransactionDetailUnauthorized("empty_batch")
    if len(rows) != plan["expected_record_count"]:
        raise TransactionDetailUnauthorized("record_count_contract_mismatch")
    for row in rows:
        if not isinstance(row, dict):
            raise TransactionDetailUnauthorized("detail_row_not_an_object")
        for field_name in REQUIRED_DETAIL_FIELDS:
            if field_name not in row:
                raise TransactionDetailUnauthorized(
                    f"detail_row_missing:{field_name}")
        if row.get("event_kind", "EXECUTION") not in APPROVED_DETAIL_EVENT_KINDS:
            raise TransactionDetailUnauthorized("unauthorized_event_kind")


# ---------------------------------------------------------------------------
# Real transaction-detail classification over the real ledger
# ---------------------------------------------------------------------------


BLOCKED_LINK_UNVERIFIED = "LINK_UNVERIFIED"


def verify_linked_pre_snapshot(
    snapshot_store: HoldingsSnapshotStore, link: dict, *, binding: str,
) -> str | None:
    """Prove a linked pre-snapshot exactly, or return a typed reason.

    RI-02 requires more than "read_snapshot did not raise". A link may only be
    finalized when the capture it points at is provably the one the reservation
    described:

      1. it loads at all;
      2. its source is the pension source;
      3. it is structurally what the pension capture contract allows
         (exactly one account, at least one holding, canonical content hash
         matching its own bytes);
      4. the holdings semantic digest, recomputed from the LOADED holdings with
         the SAME canonical function the reservation used, equals
         ``link["holdings_semantic_digest"]``;
      5. the deterministic id, recomputed from the batch binding and the LOADED
         official ``content_hash``, equals ``link["snapshot_id"]``;
      6. its ``captured_at`` equals the ``source_as_of`` recorded in the
         reservation ITSELF.

    Point 6 deliberately does not consult current state. Recovery can happen
    long after the crash, by which time the current holdings may legitimately
    have been replaced with a different ``source_as_of``; judging a historical
    pre-snapshot against retry-time current would then reject the very capture
    that is correct. The reservation is the immutable record of what was being
    captured, so it is what the capture is judged against.

    Returns ``None`` when every check passes, otherwise a short typed reason.
    """
    reserved_as_of = link.get("reservation_source_as_of")
    if not isinstance(reserved_as_of, str) or not reserved_as_of:
        return "reservation_binding_missing"
    try:
        loaded = snapshot_store.read_snapshot(link["snapshot_id"])
    except HoldingsSnapshotReadError as error:
        return f"unreadable:{error.reason}"

    if loaded.source != PENSION_SNAPSHOT_SOURCE:
        return "wrong_source"
    if loaded.discovered_account_count != 1 or len(loaded.accounts) != 1:
        return "wrong_account_count"
    account = loaded.accounts[0]
    if account.account_ordinal != 1 or not account.holdings:
        return "malformed_account"
    if snapshot_content_hash(loaded) != loaded.content_hash:
        return "content_hash_mismatch"

    try:
        rebuilt = tuple(
            HoldingItemV1(
                symbol=item.symbol, quantity=item.quantity,
                avg_price=item.avg_price, currency=item.currency,
                market_raw=item.market_raw, market_field=item.market_field)
            for item in account.holdings
        )
    except (TypeError, ValueError):
        return "malformed_holdings"
    if pension_semantic_digest(rebuilt) != link["holdings_semantic_digest"]:
        return "holdings_digest_mismatch"

    expected_id = deterministic_snapshot_id(
        binding=binding, content_hash=loaded.content_hash)
    if expected_id != link["snapshot_id"]:
        return "identity_mismatch"

    if canonical_utc_isoformat(loaded.captured_at) != reserved_as_of:
        return "captured_at_mismatch"
    return None


def classify_pension_batch(
    ledger_path: Path, events: Sequence[object], *, source: str,
    account_scope: str,
) -> str:
    """Classify a submitted batch against the ledger, read-only.

    Uses the ledger's own uniqueness key ``(source, account_scope,
    source_record_key)`` and its own ``payload_hash``:

      * every key present with an identical payload hash -> DUPLICATE
      * no key present                                   -> NEW
      * anything else (partial overlap, or a present key
        whose payload differs)                           -> CONFLICT
    """
    if not events:
        return BATCH_CONFLICT
    resolved = Path(ledger_path)
    if not resolved.exists():
        return BATCH_NEW
    # An unreadable or non-database ledger is a conflict, not an exception:
    # the caller must write nothing rather than crash mid-protocol.
    try:
        connection = sqlite3.connect(
            f"file:{resolved.resolve()}?mode=ro", uri=True)
    except (sqlite3.Error, OSError):
        return BATCH_CONFLICT
    try:
        rows = connection.execute(
            "SELECT source_record_key, payload_hash FROM trade_ledger_events "
            "WHERE source = ? AND account_scope = ?",
            (source, account_scope)).fetchall()
        stored = {row[0]: row[1] for row in rows}
    except (sqlite3.Error, OSError):
        return BATCH_CONFLICT
    finally:
        try:
            connection.close()
        except (sqlite3.Error, OSError):
            pass

    present = 0
    mismatched = 0
    for event in events:
        key = getattr(event, "source_record_key", None)
        if key is None:
            return BATCH_CONFLICT
        if key in stored:
            present += 1
            if payload_hash_hex(canonical_payload_bytes(event)) != stored[key]:
                mismatched += 1
    if present == 0:
        return BATCH_NEW
    if present == len(events) and mismatched == 0:
        return BATCH_DUPLICATE
    return BATCH_CONFLICT


def run_transaction_detail_batch(
    *,
    registry,
    snapshot_store: HoldingsSnapshotStore,
    ledger_store,
    ledger_path: Path,
    events: Sequence[object],
    file_sha256: str,
    importer_version: str,
    source: str,
    account_scope: str,
    clock: Callable[[], datetime],
    trace: list[str] | None = None,
) -> BatchAdmissionReceiptV1:
    """The executable transaction-detail path.

    Ordering, enforced here and observable through *trace*:
      classify -> reserve link -> pre-snapshot -> mark captured -> ledger append

    The reservation is durable BEFORE the capture, so a crash at any later
    point converges on retry to exactly one snapshot and one complete ledger
    append. An exact duplicate stops at classification and captures nothing.
    """
    from tokmo.values.trade_ledger import ImportAuditMeta

    steps = trace if trace is not None else []
    identity = batch_identity(events, file_sha256=file_sha256)

    steps.append("classify")
    batch_class = classify_pension_batch(
        ledger_path, events, source=source, account_scope=account_scope)

    if batch_class == BATCH_CONFLICT:
        # A conflict never finalizes a link and never writes anything.
        return BatchAdmissionReceiptV1(
            OUTCOME_CONFLICT_REVIEW, BATCH_CONFLICT, reason="conflict_review")

    if batch_class == BATCH_DUPLICATE:
        steps.append("read_link")
        link = registry.read_batch_pre_snapshot(identity)
        if link is None:
            # RI-02: an ordinary pre-existing duplicate. No link, no snapshot,
            # nothing created.
            return BatchAdmissionReceiptV1(
                OUTCOME_DUPLICATE_NOOP, BATCH_DUPLICATE)
        if link["link_state"] == "APPENDED":
            return BatchAdmissionReceiptV1(
                OUTCOME_DUPLICATE_NOOP, BATCH_DUPLICATE,
                pre_snapshot_id=link["snapshot_id"])
        # RI-02: the ledger committed and the process died before the link was
        # finalized. The classification already proved the ledger holds this
        # exact complete batch; now prove the linked capture EXACTLY before
        # finalizing. Any mismatch leaves the link CAPTURED for review and
        # writes nothing.
        # Judged entirely against the durable reservation: current state is
        # deliberately NOT read here, because it may legitimately have moved on
        # since the crash.
        steps.append("verify_link")
        problem = verify_linked_pre_snapshot(
            snapshot_store, link, binding=f"{BINDING_BATCH_PRE}:{identity}")
        if problem is not None:
            return BatchAdmissionReceiptV1(
                OUTCOME_BLOCKED, BATCH_DUPLICATE,
                pre_snapshot_id=link["snapshot_id"],
                reason=f"{BLOCKED_LINK_UNVERIFIED}:{problem}")
        steps.append("finalize_link")
        registry.advance_batch_pre_snapshot(identity, "APPENDED")
        return BatchAdmissionReceiptV1(
            OUTCOME_DUPLICATE_NOOP, BATCH_DUPLICATE,
            pre_snapshot_id=link["snapshot_id"])

    current = registry.read_active_revision()
    if current is None:
        return BatchAdmissionReceiptV1(
            OUTCOME_BLOCKED, BATCH_NEW, reason=BLOCKED_SNAPSHOT_FAILED)

    steps.append("reserve_link")
    binding = f"{BINDING_BATCH_PRE}:{identity}"
    reserved_id = pension_snapshot_identity(current, binding=binding)
    link = registry.reserve_batch_pre_snapshot(
        identity, reserved_id, current.semantic_digest, current.source_as_of)

    steps.append("pre_snapshot")
    port = pension_snapshot_port(snapshot_store, binding=binding)
    outcome = port.capture(current)
    if not outcome.ok:
        return BatchAdmissionReceiptV1(
            OUTCOME_BLOCKED, BATCH_NEW, pre_snapshot_id=link["snapshot_id"],
            reason=outcome.reason or BLOCKED_SNAPSHOT_FAILED)

    steps.append("mark_captured")
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")

    steps.append("ledger_append")
    receipt = ledger_store.append_batch(
        tuple(events),
        import_audit=ImportAuditMeta(file_sha256, importer_version))
    if receipt.status != "SAVED" or receipt.inserted_count != len(events):
        return BatchAdmissionReceiptV1(
            OUTCOME_BLOCKED, BATCH_NEW, pre_snapshot_id=outcome.snapshot_id,
            reason=f"LEDGER_FAILED:{receipt.status}")

    registry.advance_batch_pre_snapshot(identity, "APPENDED")
    return BatchAdmissionReceiptV1(
        OUTCOME_APPENDED, BATCH_NEW, pre_snapshot_id=outcome.snapshot_id,
        appended_count=receipt.inserted_count)
