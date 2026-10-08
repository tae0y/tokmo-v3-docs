"""Frozen read-only decoder for the legacy Toss v1 ``holdings_store.json``.

This module is deliberately frozen: it carries its own copies of the legacy
envelope/record/holding field sets, the legacy policy token, the semantic
rules, and the canonical serialization, so legacy admission and the
deterministic migration digests can never drift when the live parser or the
legacy JSON store adapter changes. It imports only ``tokmo.values`` (the
zero-import leaf), never the legacy store adapter, facts, composition, or
any live module, and it never writes diagnostics or any other output.

Admission is fail-closed: only whole captures whose every record matches the
exact known legacy Toss v1 shape are grouped; anything unknown, missing,
extra, mixed, incomplete, or conflicting refuses the entire source with one
closed reason token that carries no decoded value.
"""

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation

from tokmo.values import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
    canonical_decimal,
)

# Frozen legacy contract constants (copied at design time; never imported
# from the live modules they mirror).
LEGACY_SOURCE = "toss-holdings-v1"
LEGACY_POLICY_VERSION = "toss1-v1"
LEGACY_ENVELOPE_FIELDS = frozenset({"records"})
LEGACY_RECORD_FIELDS = frozenset({
    "record_id", "source", "captured_at", "account_ordinal", "account_count",
    "holdings", "stored_at", "policy_version",
})
LEGACY_HOLDING_FIELDS = frozenset({
    "symbol", "quantity", "avg_price", "avg_price_field", "currency",
    "market_raw", "market_field", "name", "asset_type",
})
MAX_LEGACY_SOURCE_BYTES = 16 * 1024 * 1024
_LEGACY_ID_DOMAIN = b"tokmo-holdings-legacy-v1:"

LEGACY_REFUSAL_REASONS = frozenset({
    "source_too_large",
    "malformed_json",
    "envelope_outside_contract",
    "record_outside_contract",
    "non_toss_source",
    "policy_outside_contract",
    "record_id_outside_contract",
    "timestamp_outside_contract",
    "holding_outside_contract",
    "capture_incomplete",
    "capture_conflict",
})

# Nullable holding text fields with the exact length bounds the SQLite
# candidate enforces, so admitted captures cannot fail downstream CHECKs.
_NULLABLE_HOLDING_BOUNDS = (
    ("currency", 16), ("market_raw", 16), ("market_field", 64),
    ("name", 256), ("asset_type", 64),
)


@dataclass(frozen=True)
class LegacyValidationResult:
    """Closed decode outcome: a status/reason pair plus parsed captures.

    ``captures`` stays out of ``repr`` so no symbol, amount, or digest can
    render through routine output; the reason is always a closed token.
    """

    status: str
    reason: str
    captures: tuple[CompleteHoldingsSnapshot, ...] = field(repr=False)

    def __post_init__(self) -> None:
        if self.status == "VALID":
            if self.reason != "":
                raise ValueError("legacy_result_outside_contract")
        elif (self.status != "INVALID"
                or self.reason not in LEGACY_REFUSAL_REASONS
                or self.captures != ()):
            raise ValueError("legacy_result_outside_contract")


class _Refusal(Exception):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _canonical_utc_isoformat(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    """Refuse duplicate object member names at every nesting level instead
    of the silent last-value-wins of plain ``json.loads``; the refusal
    carries only the closed token, never the key or value."""
    obj: dict = {}
    for key, value in pairs:
        if key in obj:
            raise _Refusal("malformed_json")
        obj[key] = value
    return obj


def _require(condition: bool, reason: str) -> None:
    if not condition:
        raise _Refusal(reason)


def _nonempty_str(value: object) -> bool:
    return isinstance(value, str) and value != ""


def _validate_amount(value: object) -> None:
    _require(_nonempty_str(value) and len(value) <= 64,
             "holding_outside_contract")
    try:
        amount = Decimal(value)
    except InvalidOperation:
        raise _Refusal("holding_outside_contract") from None
    _require(
        amount.is_finite() and amount >= 0
        and canonical_decimal(amount) == value,
        "holding_outside_contract")


def _validate_holding(holding: object) -> TossHoldingFact:
    _require(isinstance(holding, dict), "holding_outside_contract")
    _require(set(holding) == LEGACY_HOLDING_FIELDS, "holding_outside_contract")
    _require(_nonempty_str(holding["symbol"]) and len(holding["symbol"]) <= 64,
             "holding_outside_contract")
    _require(_nonempty_str(holding["avg_price_field"]),
             "holding_outside_contract")
    _validate_amount(holding["quantity"])
    _validate_amount(holding["avg_price"])
    for name, bound in _NULLABLE_HOLDING_BOUNDS:
        value = holding[name]
        _require(
            value is None or (_nonempty_str(value) and len(value) <= bound),
            "holding_outside_contract")
    _require((holding["market_raw"] is None) == (holding["market_field"] is None),
             "holding_outside_contract")
    return TossHoldingFact(
        symbol=holding["symbol"],
        quantity=holding["quantity"],
        avg_price=holding["avg_price"],
        avg_price_field=holding["avg_price_field"],
        currency=holding["currency"],
        market_raw=holding["market_raw"],
        market_field=holding["market_field"],
        name=holding["name"],
        asset_type=holding["asset_type"],
    )


def _validate_record(record: object, seen_record_ids: set[str]) -> dict:
    _require(isinstance(record, dict), "record_outside_contract")
    source = record.get("source")
    _require(_nonempty_str(source), "record_outside_contract")
    _require(source == LEGACY_SOURCE, "non_toss_source")
    _require(set(record) == LEGACY_RECORD_FIELDS, "record_outside_contract")
    for name in ("record_id", "captured_at", "stored_at", "policy_version"):
        _require(isinstance(record[name], str), "record_outside_contract")
    _require(_nonempty_str(record["stored_at"]), "record_outside_contract")
    _require(record["policy_version"] == LEGACY_POLICY_VERSION,
             "policy_outside_contract")
    _require(_nonempty_str(record["record_id"]), "record_id_outside_contract")
    _require(record["record_id"] not in seen_record_ids,
             "record_id_outside_contract")
    seen_record_ids.add(record["record_id"])

    ordinal, count = record["account_ordinal"], record["account_count"]
    _require(type(ordinal) is int and type(count) is int,
             "record_outside_contract")
    _require(1 <= ordinal <= count, "record_outside_contract")
    _require(isinstance(record["holdings"], list), "record_outside_contract")

    for name in ("captured_at", "stored_at"):
        try:
            parsed = datetime.fromisoformat(record[name])
        except ValueError:
            raise _Refusal("timestamp_outside_contract") from None
        _require(parsed.tzinfo is not None, "timestamp_outside_contract")

    holdings = tuple(_validate_holding(entry) for entry in record["holdings"])
    symbols = [item.symbol for item in holdings]
    _require(len(symbols) == len(set(symbols)), "holding_outside_contract")
    return {
        "captured_at_raw": record["captured_at"],
        "captured_at": datetime.fromisoformat(record["captured_at"]),
        "account_ordinal": ordinal,
        "account_count": count,
        "holdings": tuple(sorted(holdings, key=lambda item: item.symbol)),
    }


def _canonical_capture_bytes(
    captured_at: datetime, account_count: int,
    accounts: tuple[tuple[int, tuple[TossHoldingFact, ...]], ...],
) -> bytes:
    """Frozen copy of the SDS §5.2 canonical serialization over legacy rows."""
    obj = {
        "source": LEGACY_SOURCE,
        "captured_at": _canonical_utc_isoformat(captured_at),
        "discovered_account_count": account_count,
        "accounts": [
            {
                "account_ordinal": ordinal,
                "holdings": [
                    {
                        "symbol": item.symbol,
                        "quantity": item.quantity,
                        "avg_price": item.avg_price,
                        "avg_price_field": item.avg_price_field,
                        "currency": item.currency,
                        "market_raw": item.market_raw,
                        "market_field": item.market_field,
                        "name": item.name,
                        "asset_type": item.asset_type,
                    }
                    for item in holdings
                ],
            }
            for ordinal, holdings in accounts
        ],
    }
    text = json.dumps(obj, ensure_ascii=False, allow_nan=False,
                      separators=(",", ":"), sort_keys=False)
    return text.encode("utf-8")


def _group_captures(rows: list[dict]) -> tuple[CompleteHoldingsSnapshot, ...]:
    groups: dict[datetime, list[dict]] = {}
    for row in rows:
        groups.setdefault(row["captured_at"], []).append(row)

    captures = []
    for instant in sorted(groups):
        members = groups[instant]
        _require(len({row["captured_at_raw"] for row in members}) == 1,
                 "capture_conflict")
        _require(len({row["account_count"] for row in members}) == 1,
                 "capture_conflict")
        count = members[0]["account_count"]
        ordinals = [row["account_ordinal"] for row in members]
        _require(len(ordinals) == len(set(ordinals)), "capture_conflict")
        _require(set(ordinals) == set(range(1, count + 1)),
                 "capture_incomplete")
        ordered = sorted(members, key=lambda row: row["account_ordinal"])
        account_rows = tuple(
            (row["account_ordinal"], row["holdings"]) for row in ordered)
        canonical = _canonical_capture_bytes(instant, count, account_rows)
        captures.append(CompleteHoldingsSnapshot(
            snapshot_id=hashlib.sha256(
                _LEGACY_ID_DOMAIN + canonical).hexdigest(),
            source=LEGACY_SOURCE,
            captured_at=instant,
            discovered_account_count=count,
            accounts=tuple(
                HoldingsSnapshotAccount(ordinal, holdings)
                for ordinal, holdings in account_rows),
            content_hash=hashlib.sha256(canonical).hexdigest(),
        ))
    return tuple(captures)


def validate_legacy_json(raw_bytes: bytes) -> LegacyValidationResult:
    """Frozen read-only semantic validation of one whole legacy source.

    Returns ``VALID`` with every capture parsed, grouped, and bound to its
    deterministic domain-separated snapshot ID, or ``INVALID`` with one
    closed reason token and no captures. Never writes anything.
    """
    try:
        _require(isinstance(raw_bytes, bytes), "malformed_json")
        _require(len(raw_bytes) <= MAX_LEGACY_SOURCE_BYTES, "source_too_large")
        try:
            payload = json.loads(
                raw_bytes, object_pairs_hook=_object_without_duplicates)
        except (ValueError, UnicodeDecodeError):
            raise _Refusal("malformed_json") from None
        _require(isinstance(payload, dict), "envelope_outside_contract")
        _require(set(payload) == LEGACY_ENVELOPE_FIELDS,
                 "envelope_outside_contract")
        _require(isinstance(payload["records"], list),
                 "envelope_outside_contract")
        seen_record_ids: set[str] = set()
        rows = [
            _validate_record(record, seen_record_ids)
            for record in payload["records"]
        ]
        captures = _group_captures(rows)
    except _Refusal as refusal:
        return LegacyValidationResult("INVALID", refusal.reason, ())
    return LegacyValidationResult("VALID", "", captures)
