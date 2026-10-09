"""Closed canonical JSON codec and immutable operations material packs."""

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from enum import Enum
from types import MappingProxyType

from tokmo.values import canonical_decimal

MATERIAL_PACK_SCHEMA_VERSION = 1
PACK_MAX_BYTES = 16 * 1024 * 1024
CANONICAL_MAX_DEPTH = 64
CANONICAL_MAX_LIST_COUNT = 4096
CANONICAL_MAX_STRING_LENGTH = 65536
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")

# N2: The sole parser-version constant for composite HOLDINGS v2 packs.
COMPOSITE_HOLDINGS_PARSER_VERSION = "composite-holdings-v2"
HOLDINGS_PAYLOAD_VERSION = 2

# N1: import scope patterns from leaf for v2 validator.
from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)


class JobKind(str, Enum):
    HOLDINGS = "holdings"
    QUOTES = "quotes"
    TRADES = "trades"


def _normalize(value, depth=0):
    if depth > CANONICAL_MAX_DEPTH:
        raise ValueError("maximum depth exceeded")
    if type(value) is str:
        if len(value) > CANONICAL_MAX_STRING_LENGTH:
            raise ValueError("maximum string length exceeded")
        return value
    if value is None or type(value) is bool or type(value) is int:
        return value
    if isinstance(value, Enum):
        return _normalize(value.value, depth)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("datetime must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    if type(value) in (list, tuple):
        if len(value) > CANONICAL_MAX_LIST_COUNT:
            raise ValueError("maximum list count exceeded")
        return [_normalize(item, depth + 1) for item in value]
    if type(value) is dict or isinstance(value, MappingProxyType):
        result = {}
        for key, item in value.items():
            if type(key) is not str:
                raise TypeError("canonical object keys must be strings")
            _normalize(key, depth + 1)
            result[key] = _normalize(item, depth + 1)
        return result
    raise TypeError(f"unsupported canonical type: {type(value).__name__}")


def canonical_encode(obj) -> bytes:
    return json.dumps(
        _normalize(obj),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")


def _reject_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def _reject_constant(value):
    raise ValueError(f"non-JSON constant: {value}")


def _check_bounds(value, depth=0):
    if depth > CANONICAL_MAX_DEPTH:
        raise ValueError("maximum depth exceeded")
    if type(value) is str and len(value) > CANONICAL_MAX_STRING_LENGTH:
        raise ValueError("maximum string length exceeded")
    if type(value) is list:
        if len(value) > CANONICAL_MAX_LIST_COUNT:
            raise ValueError("maximum list count exceeded")
        for item in value:
            _check_bounds(item, depth + 1)
    elif type(value) is dict:
        for key, item in value.items():
            _check_bounds(key, depth + 1)
            _check_bounds(item, depth + 1)


def canonical_decode(raw: bytes, max_bytes: int):
    if type(raw) is not bytes:
        raise TypeError("raw must be bytes")
    if type(max_bytes) is not int or max_bytes < 0:
        raise ValueError("max_bytes must be a non-negative int")
    if len(raw) > max_bytes:
        raise ValueError("max_bytes exceeded")
    try:
        value = json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=_reject_constant,
            parse_float=lambda _value: (_ for _ in ()).throw(
                ValueError("float not allowed")
            ),
        )
    except (UnicodeDecodeError, json.JSONDecodeError, RecursionError) as error:
        raise ValueError("invalid canonical JSON") from error
    _check_bounds(value)
    return value


def _require_exact_dict(value, keys, name):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(f"{name} must have exact keys")


def _string(value, name, nullable=False):
    if nullable and value is None:
        return
    if type(value) is not str:
        raise ValueError(f"{name} must be a string")


def _decimal_string(value, name):
    _string(value, name)
    try:
        if canonical_decimal(Decimal(value)) != value:
            raise ValueError(f"{name} must be canonical decimal")
    except InvalidOperation as error:
        raise ValueError(f"{name} must be canonical decimal") from error


def _aware_string(value, name, nullable=False):
    _string(value, name, nullable=nullable)
    if value is None:
        return
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{name} must be an ISO datetime") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{name} must be timezone-aware")


def _validate_holdings_payload(payload):
    """Dispatch holdings payload validation by payload_version field."""
    if type(payload) is not dict:
        raise ValueError("holdings payload must be a dict")
    payload_version = payload.get("payload_version", 1)
    if payload_version == 1:
        _validate_holdings_v1(payload)
    elif payload_version == 2:
        _validate_holdings_generation_r2(payload)
    else:
        raise ValueError("unknown holdings payload version")
    canonical_encode(payload)


def _validate_holdings_v1(payload):
    """Existing v1 validation: exact keys, per-account, per-holding."""
    _require_exact_dict(payload, {"accounts", "global_inventory"}, "holdings payload")
    if (
        type(payload["global_inventory"]) is not bool
        or type(payload["accounts"]) is not list
    ):
        raise ValueError("invalid holdings payload")
    account_keys = {"account_scope", "account_type_label", "ordinal", "holdings"}
    holding_keys = {
        "symbol",
        "quantity",
        "avg_price",
        "currency",
        "market_raw",
        "market_field",
    }
    for account in payload["accounts"]:
        _require_exact_dict(account, account_keys, "holdings account")
        if not (
            type(account["account_scope"]) is str
            and type(account["account_type_label"]) is str
            and type(account["ordinal"]) is int
            and type(account["holdings"]) is list
        ):
            raise ValueError("invalid holdings account")
        for holding in account["holdings"]:
            _require_exact_dict(holding, holding_keys, "holding")
            _string(holding["symbol"], "symbol")
            _decimal_string(holding["quantity"], "quantity")
            _decimal_string(holding["avg_price"], "avg_price")
            for key in ("currency", "market_raw", "market_field"):
                _string(holding[key], key, nullable=True)


def _validate_holdings_generation_r2(payload):
    """Composite HOLDINGS generation r2 validator (M3).

    Per-holding validation at v1 strictness, provenance field validation,
    cross-checks account-level pension fields against pension_provenance,
    and enforces exactly-one PENSION_EXCEL account.
    """
    _require_exact_dict(
        payload,
        {
            "accounts",
            "global_inventory",
            "payload_version",
            "pension_provenance",
        },
        "holdings generation r2 payload",
    )
    if payload["payload_version"] != 2:
        raise ValueError("generation r2 payload_version must be 2")
    if type(payload["global_inventory"]) is not bool:
        raise ValueError("invalid global_inventory")

    pp = payload["pension_provenance"]
    _require_exact_dict(
        pp,
        {
            "revision_id",
            "source_content_sha256",
            "semantic_digest",
            "source_as_of",
            "source_age_days",
            "freshness_class",
            "parser_version",
        },
        "pension_provenance",
    )
    # M9: type-guard provenance fields before len()/fullmatch.
    if not isinstance(pp["revision_id"], str):
        raise ValueError("invalid pension revision_id")
    if not isinstance(pp["source_content_sha256"], str):
        raise ValueError("invalid pension source_content_sha256")
    if not isinstance(pp["semantic_digest"], str):
        raise ValueError("invalid pension semantic_digest")
    if len(pp["revision_id"]) != 32 or not all(
        c in "0123456789abcdef" for c in pp["revision_id"]
    ):
        raise ValueError("invalid pension revision_id")
    if len(pp["source_content_sha256"]) != 64:
        raise ValueError("invalid pension source_content_sha256")
    if len(pp["semantic_digest"]) != 64:
        raise ValueError("invalid pension semantic_digest")
    _aware_string(pp["source_as_of"], "pension source_as_of")
    if type(pp["source_age_days"]) is not int or pp["source_age_days"] < 0:
        raise ValueError("invalid pension source_age_days")
    if pp["freshness_class"] not in ("CURRENT_MANUAL_SOURCE", "AGED_MANUAL_SOURCE"):
        raise ValueError("invalid pension freshness_class")
    if pp["parser_version"] != "pension-excel-v1":
        raise ValueError("invalid pension parser_version")

    holding_keys = {
        "symbol",
        "quantity",
        "avg_price",
        "currency",
        "market_raw",
        "market_field",
    }
    pension_excel_count = 0
    for account in payload["accounts"]:
        base_keys = {
            "account_scope",
            "account_type_label",
            "ordinal",
            "holdings",
            "source_provenance",
        }
        prov = account.get("source_provenance")
        if prov == "PENSION_EXCEL":
            pension_excel_count += 1
            keys = base_keys | {
                "pension_revision_id",
                "pension_source_as_of",
                "pension_source_age_days",
                "pension_freshness_class",
                "pension_semantic_digest",
            }
        elif prov == "TOSS_PERSONAL":
            keys = base_keys
        else:
            raise ValueError("invalid source_provenance")
        _require_exact_dict(account, keys, "holdings generation r2 account")

        # M9: v1-equivalent account-level type checks.
        if not (
            type(account["account_scope"]) is str
            and type(account["account_type_label"]) is str
            and type(account["ordinal"]) is int
            and type(account["holdings"]) is list
        ):
            raise ValueError("invalid holdings account")

        if prov == "TOSS_PERSONAL":
            if ACCOUNT_SCOPE_RE.fullmatch(account["account_scope"]) is None:
                raise ValueError("TOSS_PERSONAL scope mismatch")
        elif prov == "PENSION_EXCEL":
            if PENSION_SCOPE_RE.fullmatch(account["account_scope"]) is None:
                raise ValueError("PENSION_EXCEL scope mismatch")
            if account["pension_revision_id"] != pp["revision_id"]:
                raise ValueError("pension revision_id mismatch")
            if account["pension_freshness_class"] != pp["freshness_class"]:
                raise ValueError("pension freshness_class mismatch")
            if account["pension_semantic_digest"] != pp["semantic_digest"]:
                raise ValueError("pension semantic_digest mismatch")

        for holding in account["holdings"]:
            _require_exact_dict(holding, holding_keys, "holding")
            _string(holding["symbol"], "symbol")
            _decimal_string(holding["quantity"], "quantity")
            _decimal_string(holding["avg_price"], "avg_price")
            for key in ("currency", "market_raw", "market_field"):
                _string(holding[key], key, nullable=True)

    if pension_excel_count != 1:
        raise ValueError("generation r2 payload must have exactly one PENSION_EXCEL")


def _validate_payload(job: JobKind, payload: Mapping[str, object]) -> None:
    if type(payload) is not dict:
        raise ValueError("payload must be a dict")
    if job is JobKind.HOLDINGS:
        _validate_holdings_payload(payload)
    elif job is JobKind.QUOTES:
        _require_exact_dict(payload, {"markets"}, "quotes payload")
        markets = payload["markets"]
        if type(markets) is not dict or not set(markets) <= {"KR", "US"}:
            raise ValueError("invalid quote markets")
        quote_keys = {
            "symbol",
            "last_price",
            "currency_raw",
            "provider_timestamp",
            "fetched_at",
            "stored_at",
        }
        for rows in markets.values():
            if type(rows) is not list:
                raise ValueError("quote market must be a list")
            for row in rows:
                _require_exact_dict(row, quote_keys, "quote")
                _string(row["symbol"], "symbol")
                _decimal_string(row["last_price"], "last_price")
                _string(row["currency_raw"], "currency_raw")
                for key in ("provider_timestamp",):
                    _aware_string(row[key], key, nullable=True)
                for key in ("fetched_at", "stored_at"):
                    _aware_string(row[key], key)
    else:
        if set(payload) == {"executions"}:
            rows = payload["executions"]
            if type(rows) is not list:
                raise ValueError("consumer executions must be a list")
            keys = {"source", "account_scope", "source_record_key", "stored_at"}
            for row in rows:
                _require_exact_dict(row, keys, "consumer execution")
                for key in ("source", "account_scope", "source_record_key"):
                    _string(row[key], key)
                _aware_string(row["stored_at"], "stored_at")
            canonical_encode(payload)
            return
        _require_exact_dict(
            payload, {"accounts", "pagination_pending"}, "trades payload"
        )
        if (
            type(payload["accounts"]) is not list
            or type(payload["pagination_pending"]) is not bool
        ):
            raise ValueError("invalid trades payload")
        account_keys = {
            "account_scope",
            "inserted",
            "duplicates",
            "quarantine",
            "incomplete",
        }
        inserted_keys = {
            "source_record_key",
            "trade_date",
            "transaction_type_raw",
            "instrument_raw",
            "quantity",
            "unit_price",
            "amount",
            "medium_raw",
            "currency_raw",
            "classified_market",
            "filled_at",
        }
        for account in payload["accounts"]:
            _require_exact_dict(account, account_keys, "trades account")
            _string(account["account_scope"], "account_scope")
            for key in ("inserted", "duplicates", "quarantine", "incomplete"):
                if type(account[key]) is not list:
                    raise ValueError("invalid trades account")
            for key in ("duplicates", "quarantine", "incomplete"):
                if any(type(item) is not str for item in account[key]):
                    raise ValueError(f"{key} entries must be strings")
            for row in account["inserted"]:
                _require_exact_dict(row, inserted_keys, "inserted trade")
                for key in (
                    "source_record_key",
                    "trade_date",
                    "transaction_type_raw",
                    "instrument_raw",
                    "medium_raw",
                    "classified_market",
                ):
                    _string(row[key], key)
                try:
                    if (
                        date.fromisoformat(row["trade_date"]).isoformat()
                        != row["trade_date"]
                    ):
                        raise ValueError("trade_date must be ISO date")
                except ValueError as error:
                    raise ValueError("trade_date must be ISO date") from error
                _aware_string(row["filled_at"], "filled_at")
                for key in ("quantity", "unit_price", "amount"):
                    _decimal_string(row[key], key)
                _string(row["currency_raw"], "currency_raw", nullable=True)
                if row["classified_market"] not in {"KR", "US"}:
                    raise ValueError("invalid classified_market")
    canonical_encode(payload)


@dataclass(frozen=True)
class MaterialPackV1:
    schema_version: int
    job: JobKind
    tick_id: int
    market_partition: tuple[str, ...]
    payload: Mapping[str, object]
    captured_at: datetime
    source_timestamps: Mapping[str, str | None]
    parser_version: str
    content_hash: str
    # Stage 2 mask binding (SDS §6): present only for packs produced under an
    # OpenMarketMaskV1; serialized only when set so historical pack bytes,
    # content hashes, and stored G2 evidence stay valid unchanged.
    mask_digest: str | None = None

    def __post_init__(self):
        if (
            type(self.schema_version) is not int
            or self.schema_version != MATERIAL_PACK_SCHEMA_VERSION
        ):
            raise ValueError("unsupported material pack schema")
        if type(self.tick_id) is not int or self.tick_id < 0:
            raise ValueError("tick_id must be a non-negative int")
        if not isinstance(self.job, JobKind):
            raise ValueError("invalid job")
        if type(self.market_partition) is not tuple or any(
            type(x) is not str for x in self.market_partition
        ):
            raise ValueError("invalid market_partition")
        if self.captured_at.tzinfo is None or self.captured_at.utcoffset() is None:
            raise ValueError("captured_at must be aware")
        if type(self.source_timestamps) is not dict or set(self.source_timestamps) != {
            "provider",
            "fetched",
            "stored",
        }:
            raise ValueError("invalid source_timestamps")
        for value in self.source_timestamps.values():
            _aware_string(value, "source timestamp", nullable=True)
        if type(self.parser_version) is not str or not self.parser_version:
            raise ValueError("invalid parser_version")
        _validate_payload(self.job, self.payload)
        if self.mask_digest is not None and (
            type(self.mask_digest) is not str
            or not _HASH_RE.fullmatch(self.mask_digest)
        ):
            raise ValueError("invalid mask_digest")
        if not _HASH_RE.fullmatch(self.content_hash):
            raise ValueError("invalid content_hash")
        if pack_content_hash(self) != self.content_hash:
            raise ValueError("content_hash mismatch")


def _pack_dict(pack: MaterialPackV1, include_hash: bool):
    result = {
        "schema_version": pack.schema_version,
        "job": pack.job,
        "tick_id": pack.tick_id,
        "market_partition": pack.market_partition,
        "payload": pack.payload,
        "captured_at": pack.captured_at,
        "source_timestamps": pack.source_timestamps,
        "parser_version": pack.parser_version,
    }
    if pack.mask_digest is not None:
        result["mask_digest"] = pack.mask_digest
    if include_hash:
        result["content_hash"] = pack.content_hash
    return result


def canonical_pack_bytes(pack: MaterialPackV1) -> bytes:
    return canonical_encode(_pack_dict(pack, False))


def canonical_pack_file_bytes(pack: MaterialPackV1) -> bytes:
    encoded = canonical_encode(_pack_dict(pack, True))
    if len(encoded) > PACK_MAX_BYTES:
        raise ValueError("material pack exceeds PACK_MAX_BYTES")
    return encoded


def pack_content_hash(pack: MaterialPackV1) -> str:
    return hashlib.sha256(canonical_pack_bytes(pack)).hexdigest()


def build_material_pack_v1(**fields_without_hash) -> MaterialPackV1:
    if "content_hash" in fields_without_hash:
        raise TypeError("content_hash is factory-owned")
    fields_without_hash.setdefault("mask_digest", None)
    provisional = object.__new__(MaterialPackV1)
    for field, value in fields_without_hash.items():
        object.__setattr__(provisional, field, value)
    digest = hashlib.sha256(canonical_pack_bytes(provisional)).hexdigest()
    result = MaterialPackV1(content_hash=digest, **fields_without_hash)
    canonical_pack_file_bytes(result)
    return result
