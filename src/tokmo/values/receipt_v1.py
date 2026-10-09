"""Typed frozen operations receipts."""

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tokmo.values.account_scope_patterns import (  # N1: leaf re-export
    ACCOUNT_SCOPE_RE as ACCOUNT_SCOPE_RE,
)
from tokmo.values.material_pack import JobKind, canonical_encode

RECEIPT_SCHEMA_VERSION = 1
RECEIPT_REASON_TOKENS = frozenset(
    {
        "saved",
        "skipped_holdings_absent",
        "skipped_market_closed",
        "skipped_lock_contention",
        "skipped_zero_holdings",
        "skipped_stale",
        "skipped_zero_quote_targets",
        "failed_watchlist_contract",
        "failed_store",
        "failed_pack",
        "failed_receipt",
        "failed_parse",
        "failed_calendar",
        "cache_corrupt",
        "cache_stale",
        "failed_provider_auth",
        "failed_provider_rate_limit",
        "failed_provider_contract",
        "failed_provider_other",
        "failed_ceiling",
        "partial_accounts",
        "partial_pagination",
        "partial_incomplete",
        "partial_quarantine",
        "failed_pension_source",  # M7: PENSION source missing/corrupt
    }
)
# N1: ACCOUNT_SCOPE_RE re-exported from account_scope_patterns.py above.


class AttemptStatus(str, Enum):
    SAVED = "saved"
    SKIPPED = "skipped"
    FAILED = "failed"
    PARTIAL = "partial"


def _aware(value: datetime | None, name: str, *, nullable: bool = False) -> None:
    if value is None:
        if nullable:
            return
        raise ValueError(f"{name} must be timezone-aware")
    if (
        not isinstance(value, datetime)
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(f"{name} must be timezone-aware")


@dataclass(frozen=True)
class AttemptV1:
    tick_id: int
    status: AttemptStatus
    reason: str
    completed_at: datetime

    def __post_init__(self):
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid attempt tick_id")
        if not isinstance(self.status, AttemptStatus):
            raise ValueError("invalid attempt status")
        if self.reason not in RECEIPT_REASON_TOKENS:
            raise ValueError("unknown receipt reason")
        if (self.status is AttemptStatus.SAVED) != (self.reason == "saved"):
            raise ValueError("saved status and reason must agree")
        _aware(self.completed_at, "completed_at")


@dataclass(frozen=True)
class LastSuccessV1:
    tick_id: int
    pack_path: str
    pack_sha256: str
    captured_at: datetime
    completed_at: datetime

    def __post_init__(self):
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("invalid success tick_id")
        if type(self.pack_path) is not str or not self.pack_path:
            raise ValueError("invalid pack_path")
        if not re.fullmatch(r"[0-9a-f]{64}", self.pack_sha256):
            raise ValueError("invalid pack_sha256")
        _aware(self.captured_at, "captured_at")
        _aware(self.completed_at, "completed_at")


@dataclass(frozen=True)
class MarketFreshness:
    market: str
    status: str
    last_success_tick_id: int | None
    captured_at: datetime | None

    def __post_init__(self):
        if type(self.market) is not str or type(self.status) is not str:
            raise ValueError("invalid market freshness")
        if (
            self.last_success_tick_id is not None
            and type(self.last_success_tick_id) is not int
        ):
            raise ValueError("invalid market tick_id")
        _aware(self.captured_at, "captured_at", nullable=True)


@dataclass(frozen=True)
class CompletenessV1:
    accounts_ok: int
    accounts_failed: int
    pagination_pending: bool
    quarantine_count: int
    incomplete_count: int

    def __post_init__(self):
        if any(
            type(value) is not int
            for value in (
                self.accounts_ok,
                self.accounts_failed,
                self.quarantine_count,
                self.incomplete_count,
            )
        ):
            raise ValueError("invalid account counts")
        if (
            min(
                self.accounts_ok,
                self.accounts_failed,
                self.quarantine_count,
                self.incomplete_count,
            )
            < 0
        ):
            raise ValueError("account counts must be non-negative")
        if type(self.pagination_pending) is not bool:
            raise ValueError("pagination_pending must be bool")


@dataclass(frozen=True)
class ReceiptV1:
    schema_version: int
    job: JobKind
    attempt: AttemptV1
    last_success: LastSuccessV1 | None
    markets: Mapping[str, MarketFreshness]
    completeness: CompletenessV1 | None
    # Stage 2 mask binding (SDS §6): present only for attempts made under an
    # OpenMarketMaskV1; serialized only when set so historical receipt bytes
    # remain valid unchanged.
    mask_digest: str | None = None

    def __post_init__(self):
        if (
            type(self.schema_version) is not int
            or self.schema_version != RECEIPT_SCHEMA_VERSION
            or not isinstance(self.job, JobKind)
        ):
            raise ValueError("unsupported receipt")
        if not isinstance(self.attempt, AttemptV1):
            raise ValueError("invalid attempt")
        if self.attempt.status is AttemptStatus.SAVED and self.last_success is None:
            raise ValueError("saved receipt requires last_success")
        if type(self.markets) is not dict:
            raise ValueError("markets must be a dict")
        for key, value in self.markets.items():
            if (
                type(key) is not str
                or not isinstance(value, MarketFreshness)
                or key != value.market
            ):
                raise ValueError("invalid markets")
        if (self.completeness is not None) != (self.job is JobKind.TRADES):
            raise ValueError("completeness is required only for trades")
        if self.mask_digest is not None and (
            type(self.mask_digest) is not str
            or not re.fullmatch(r"[0-9a-f]{64}", self.mask_digest)
        ):
            raise ValueError("invalid mask_digest")


def receipt_dict(receipt: ReceiptV1):
    attempt = receipt.attempt
    success = receipt.last_success
    completeness = receipt.completeness
    result = {
        "schema_version": receipt.schema_version,
        "job": receipt.job,
        "attempt": {
            "tick_id": attempt.tick_id,
            "status": attempt.status,
            "reason": attempt.reason,
            "completed_at": attempt.completed_at,
        },
        "last_success": None
        if success is None
        else {
            "tick_id": success.tick_id,
            "pack_path": success.pack_path,
            "pack_sha256": success.pack_sha256,
            "captured_at": success.captured_at,
            "completed_at": success.completed_at,
        },
        "markets": {
            key: {
                "market": value.market,
                "status": value.status,
                "last_success_tick_id": value.last_success_tick_id,
                "captured_at": value.captured_at,
            }
            for key, value in receipt.markets.items()
        },
        "completeness": None
        if completeness is None
        else {
            "accounts_ok": completeness.accounts_ok,
            "accounts_failed": completeness.accounts_failed,
            "pagination_pending": completeness.pagination_pending,
            "quarantine_count": completeness.quarantine_count,
            "incomplete_count": completeness.incomplete_count,
        },
    }
    if receipt.mask_digest is not None:
        result["mask_digest"] = receipt.mask_digest
    return result


def canonical_receipt_bytes(receipt: ReceiptV1) -> bytes:
    return canonical_encode(receipt_dict(receipt))
