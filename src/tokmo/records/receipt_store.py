"""Mutable atomic receipt storage."""

from datetime import datetime
from pathlib import Path

from tokmo.records.atomic_json import read_atomic_json, write_atomic_json
from tokmo.values.material_pack import JobKind, canonical_decode
from tokmo.values.receipt_v1 import (
    RECEIPT_SCHEMA_VERSION,
    AttemptStatus,
    AttemptV1,
    CompletenessV1,
    LastSuccessV1,
    MarketFreshness,
    ReceiptV1,
    canonical_receipt_bytes,
)

RECEIPT_MAX_BYTES = 64 * 1024


class ReceiptCorruptError(ValueError):
    pass


def _exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError("exact receipt keys required")


def _time(value):
    if type(value) is not str:
        raise ValueError("timestamp must be string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be aware")
    return parsed


def _decode_receipt(raw: bytes) -> ReceiptV1:
    value = canonical_decode(raw, max_bytes=RECEIPT_MAX_BYTES)
    required = {"schema_version", "job", "attempt", "last_success", "markets", "completeness"}
    if type(value) is not dict or "mask_digest" in value:
        _exact(value, required | {"mask_digest"})
    else:
        _exact(value, required)
    attempt = value["attempt"]
    _exact(attempt, {"tick_id", "status", "reason", "completed_at"})
    typed_attempt = AttemptV1(
        attempt["tick_id"], AttemptStatus(attempt["status"]), attempt["reason"], _time(attempt["completed_at"]),
    )
    success = value["last_success"]
    typed_success = None
    if success is not None:
        _exact(success, {"tick_id", "pack_path", "pack_sha256", "captured_at", "completed_at"})
        typed_success = LastSuccessV1(
            success["tick_id"], success["pack_path"], success["pack_sha256"],
            _time(success["captured_at"]), _time(success["completed_at"]),
        )
    if type(value["markets"]) is not dict:
        raise ValueError("markets must be object")
    markets = {}
    for key, market in value["markets"].items():
        _exact(market, {"market", "status", "last_success_tick_id", "captured_at"})
        markets[key] = MarketFreshness(
            market["market"], market["status"], market["last_success_tick_id"],
            None if market["captured_at"] is None else _time(market["captured_at"]),
        )
    completeness = value["completeness"]
    typed_completeness = None
    if completeness is not None:
        _exact(completeness, {
            "accounts_ok", "accounts_failed", "pagination_pending",
            "quarantine_count", "incomplete_count",
        })
        typed_completeness = CompletenessV1(
            completeness["accounts_ok"], completeness["accounts_failed"],
            completeness["pagination_pending"],
            completeness["quarantine_count"], completeness["incomplete_count"],
        )
    result = ReceiptV1(
        value["schema_version"], JobKind(value["job"]), typed_attempt,
        typed_success, markets, typed_completeness,
        mask_digest=value.get("mask_digest"),
    )
    if canonical_receipt_bytes(result) != raw:
        raise ValueError("non-canonical receipt")
    return result


class ReceiptStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self.clock = clock

    def read(self) -> ReceiptV1 | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        try:
            return _decode_receipt(read_atomic_json(self.path, max_bytes=RECEIPT_MAX_BYTES))
        except (OSError, TypeError, ValueError) as error:
            raise ReceiptCorruptError("corrupt receipt") from error

    def replace(self, receipt: ReceiptV1) -> None:
        write_atomic_json(
            self.path, canonical_receipt_bytes(receipt), max_bytes=RECEIPT_MAX_BYTES,
        )


def with_preserved_last_success(prev: ReceiptV1, attempt: AttemptV1) -> ReceiptV1:
    if attempt.status is AttemptStatus.SAVED:
        raise ValueError("preservation helper is for non-saved attempts")
    return ReceiptV1(
        prev.schema_version, prev.job, attempt, prev.last_success,
        dict(prev.markets), prev.completeness,
    )
