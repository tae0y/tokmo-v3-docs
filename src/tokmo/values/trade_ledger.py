"""Dependency-free typed contracts for the trade-ledger batch storage pilot.

The event preserves the observed seven source columns losslessly with
date-only precision; nullable fields mean "absent from source", never a
default. Value-bearing representations are constant redactions so no
instrument, quantity, price, amount, date, key, or scope can leak through
exception, log, or report paths.
"""

from dataclasses import dataclass
from datetime import date, datetime

# Frozen identity labels approved 2026-07-28 (decision D1).
TRADE_LEDGER_SOURCE = "retirement_excel_v1"
TRADE_LEDGER_ACCOUNT_SCOPE = "retirement_pension_1"

TRADE_LEDGER_SCHEMA_VERSION = 1
TRADE_LEDGER_PAYLOAD_VERSION = 1

EVENT_KINDS = frozenset({"EXECUTION", "CORRECTION"})

# The closed diagnostic vocabulary: receipts, exceptions, and audit rows may
# carry these tokens and nothing else.
CLOSED_REASON_TOKENS = frozenset({
    "invalid_batch",
    "source_contract_violation",
    "batch_identity_conflict",
    "supersession_invalid",
    "schema_mismatch",
    "store_corrupt",
    "store_locked",
    "store_write_failed",
    "store_read_failed",
    "audit_write_failed",
    "promotion_link_failed",
    "promotion_recovery_required",
    "candidate_missing",
    "canonical_exists",
    "wal_checkpoint_failed",
    "sidecars_present",
    "directory_mismatch",
})


@dataclass(frozen=True, repr=False)
class TradeLedgerEvent:
    """One typed executed-trade fact in the observed source shape."""

    source: str
    account_scope: str
    source_record_key: str
    event_kind: str
    supersedes_source_record_key: str | None
    trade_date: date
    transaction_type_raw: str
    instrument_raw: str
    quantity: str
    unit_price: str
    amount: str
    medium_raw: str
    currency_raw: str | None = None
    settlement_date: date | None = None
    fee: str | None = None
    tax: str | None = None

    def __repr__(self) -> str:
        return "<TradeLedgerEvent REDACTED>"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, repr=False)
class LedgerStoredRow:
    """One ledger event paired with its store-persisted timestamp.

    This is a separate read record: ``event`` is the immutable semantic
    trade fact (never altered), and ``stored_at`` is the timezone-aware
    UTC datetime the store assigned when the event was persisted. It
    does not extend or alter the ``TradeLedgerEvent`` semantic contract
    — it is an independent typed view for stored_at-window reads.
    """

    event: TradeLedgerEvent
    stored_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.event, TradeLedgerEvent):
            raise ValueError("invalid event")
        if (
            not isinstance(self.stored_at, datetime)
            or self.stored_at.tzinfo is None
            or self.stored_at.utcoffset() is None
        ):
            raise ValueError("stored_at must be timezone-aware")

    def __repr__(self) -> str:
        return "<LedgerStoredRow REDACTED>"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, repr=False)
class ImportAuditMeta:
    """Audit-only import metadata: never identity, never judgment input."""

    file_sha256: str | None
    importer_version: str

    def __repr__(self) -> str:
        return "<ImportAuditMeta REDACTED>"

    def __str__(self) -> str:
        return self.__repr__()


@dataclass(frozen=True, repr=False)
class TradeBatchSaveReceipt:
    """Sanitized append receipt: counts, closed tokens, opaque IDs only."""

    status: str
    import_batch_id: str
    submitted_count: int
    new_candidate_count: int
    inserted_count: int
    duplicate_count: int
    conflict_count: int
    invalid_count: int
    reason: str
    stored_at: datetime

    def __post_init__(self) -> None:
        if self.status not in ("SAVED", "FAILED"):
            raise ValueError("receipt_status_invalid")
        if self.status == "SAVED":
            if self.reason != "":
                raise ValueError("receipt_reason_invalid")
        elif self.reason not in CLOSED_REASON_TOKENS:
            raise ValueError("receipt_reason_invalid")
        classified = (
            self.new_candidate_count
            + self.duplicate_count
            + self.conflict_count
            + self.invalid_count
        )
        if self.submitted_count != classified:
            raise ValueError("count_invariant_broken")
        if self.status == "FAILED" and self.inserted_count != 0:
            raise ValueError("failed_receipt_inserted_count_nonzero")
        if self.status == "SAVED" and self.inserted_count != self.new_candidate_count:
            raise ValueError("saved_receipt_inserted_count_mismatch")

    def __repr__(self) -> str:
        return (
            f"<TradeBatchSaveReceipt status={self.status!r} "
            f"import_batch_id={self.import_batch_id!r} "
            f"submitted={self.submitted_count} new={self.new_candidate_count} "
            f"inserted={self.inserted_count} duplicate={self.duplicate_count} "
            f"conflict={self.conflict_count} invalid={self.invalid_count} "
            f"reason={self.reason!r}>"
        )

    def __str__(self) -> str:
        return self.__repr__()
