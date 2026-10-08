"""Typed, provider-safe portfolio EVENT payload values."""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, fields, is_dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import re
from types import MappingProxyType

from tokmo.values.analysis_contracts import (
    AnalysisKind, FreshnessState, MaterialKindV1, TrustClass,
)
from tokmo.values.material_pack import (
    CANONICAL_MAX_DEPTH, CANONICAL_MAX_LIST_COUNT, CANONICAL_MAX_STRING_LENGTH,
    canonical_encode,
)
from tokmo.values import canonical_decimal
from tokmo.values.portfolio_calculations import (
    FxStatus, PortfolioScope, PricingState, RatioState, SnapshotStatus,
)
from tokmo.values.session_gate import Market

_HASH = re.compile(r"^[0-9a-f]{64}$")
_TOKEN = re.compile(r"^[A-Za-z0-9_.:+/-]{1,256}$")
_KR_SYMBOL = re.compile(r"^[0-9]{6}$")
_US_SYMBOL = re.compile(r"^[A-Z0-9][A-Z0-9.-]{0,19}$")
_DIRECTION_RAW = re.compile(r"^[A-Za-z0-9가-힣 _./+()\-]{1,32}$")
_TRADE_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def normalize_instrument_symbol_v1(market: Market, instrument_raw: object) -> str | None:
    if type(instrument_raw) is not str:
        return None
    grammar = _KR_SYMBOL if market is Market.KR else _US_SYMBOL if market is Market.US else None
    return instrument_raw if grammar is not None and grammar.fullmatch(instrument_raw) else None


def map_execution_direction_v1(raw: str) -> ExecutionDirectionV1:
    if type(raw) is not str or not _DIRECTION_RAW.fullmatch(raw):
        raise ValueError("invalid direction_raw_fact")
    token = raw.casefold()
    if token in {"buy", "매수"}:
        return ExecutionDirectionV1.BUY
    if token in {"sell", "매도"}:
        return ExecutionDirectionV1.SELL
    return ExecutionDirectionV1.OTHER


class PortfolioPayloadBuildStatus(str, Enum):
    READY = "READY"
    NO_CANDIDATE = "NO_CANDIDATE"
    SNAPSHOT_INCOMPLETE = "SNAPSHOT_INCOMPLETE"
    PRIVATE_SCOPE_UNRESOLVED = "PRIVATE_SCOPE_UNRESOLVED"
    CONTRACT_INVALID = "CONTRACT_INVALID"
    # Caller-local classifications only: they are never persisted or exposed on
    # EventAnalysisExecutionOutcomeV1; authoritative observability is the final
    # selected/suppressed/no-call counts and durable request state.
    OVERSIZE = "OVERSIZE"


class CandidateSuppressionReasonV1(str, Enum):
    UNHELD_NO_POSITION = "UNHELD_NO_POSITION"
    HELD_POSITION_NOT_CANONICALLY_MATCHED = "HELD_POSITION_NOT_CANONICALLY_MATCHED"
    POSITION_CLOSED_NO_CURRENT_POSITION = "POSITION_CLOSED_NO_CURRENT_POSITION"
    POSITION_NOT_CANONICALLY_MATCHED = "POSITION_NOT_CANONICALLY_MATCHED"
    EXECUTION_INSTRUMENT_UNMATCHED = "EXECUTION_INSTRUMENT_UNMATCHED"
    OPERATIONAL_INCIDENT_NO_PORTFOLIO_DETAIL = "OPERATIONAL_INCIDENT_NO_PORTFOLIO_DETAIL"
    CAP_NOT_SELECTED = "CAP_NOT_SELECTED"
    EXPLICITLY_DEFERRED_NO_CALL = "EXPLICITLY_DEFERRED_NO_CALL"
    SAMPLED_AUDIT_NO_CALL = "SAMPLED_AUDIT_NO_CALL"
    TERMINAL_ASSIGNMENT_NO_CALL = "TERMINAL_ASSIGNMENT_NO_CALL"
    # Caller-local prompt-bound classification; not a durable reason column.
    OVERSIZE_MODEL_REQUEST = "OVERSIZE_MODEL_REQUEST"


class ExecutionDirectionV1(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    OTHER = "OTHER"


@dataclass(frozen=True)
class CandidateScopeBindingV1:
    delta_id: str
    portfolio_scope: PortfolioScope


@dataclass(frozen=True)
class PortfolioPositionDetailV1:
    portfolio_scope: PortfolioScope
    market: Market
    symbol: str
    currency: str
    quantity: Decimal
    average_cost_native: Decimal
    current_price_native: Decimal | None
    invested_cost_basis_native: Decimal
    evaluated_market_value_native: Decimal | None
    evaluated_market_value_krw: Decimal | None
    unrealized_pnl_native: Decimal | None
    unrealized_pnl_krw: Decimal | None
    unrealized_return_state: RatioState
    unrealized_return: Decimal | None
    pricing_state: PricingState
    currency_partition_market_value_weight: Decimal | None
    market_currency_partition_market_value_weight: Decimal | None
    currency_partition_total_cost_basis_weight: Decimal | None
    market_currency_partition_total_cost_basis_weight: Decimal | None
    scope_krw_priced_market_value_weight: Decimal | None
    scope_krw_total_cost_basis_weight: Decimal | None
    holdings_observed_at: datetime
    quote_observed_at: datetime | None
    fx_binding_digest: str | None


@dataclass(frozen=True)
class PortfolioCandidateSummaryV1:
    portfolio_scope: PortfolioScope
    scope_fx_status: FxStatus
    scope_evaluated_market_value_krw: Decimal | None
    scope_unrealized_pnl_krw: Decimal | None
    scope_unrealized_return_state: RatioState
    scope_unrealized_return: Decimal | None
    partition_market: Market
    partition_currency: str
    partition_position_count: int
    partition_priced_position_count: int
    partition_unpriced_position_count: int
    partition_total_cost_basis_native: Decimal
    partition_priced_cost_basis_native: Decimal
    partition_evaluated_market_value_native: Decimal
    partition_evaluated_market_value_krw: Decimal | None
    partition_unrealized_pnl_native: Decimal
    partition_unrealized_pnl_krw: Decimal | None
    partition_unrealized_return_state: RatioState
    partition_unrealized_return: Decimal | None
    priced_cost_basis_coverage_state: RatioState
    priced_cost_basis_coverage_ratio: Decimal | None
    fx_binding_digest: str | None
    source_freshness: str | None = None
    source_as_of: str | None = None


@dataclass(frozen=True)
class CompletedExecutionPayloadV1:
    portfolio_scope: PortfolioScope
    market: Market
    symbol: str
    direction: ExecutionDirectionV1
    direction_raw_fact: str
    quantity: Decimal
    unit_price_native: Decimal
    amount_native: Decimal
    currency: str
    trade_date: str
    filled_at: datetime
    completion_status: str

    def __post_init__(self):
        if not isinstance(self.portfolio_scope, PortfolioScope) or not isinstance(self.market, Market):
            raise ValueError("invalid execution scope/market")
        if normalize_instrument_symbol_v1(self.market, self.symbol) != self.symbol:
            raise ValueError("invalid execution symbol")
        if not isinstance(self.direction, ExecutionDirectionV1):
            raise ValueError("invalid execution direction")
        if map_execution_direction_v1(self.direction_raw_fact) is not self.direction:
            raise ValueError("execution direction mismatch")
        for value, name in (
            (self.quantity, "quantity"), (self.unit_price_native, "unit_price_native"),
            (self.amount_native, "amount_native"),
        ):
            if not isinstance(value, Decimal) or not value.is_finite() or value < 0:
                raise ValueError(f"invalid {name}")
        expected_currency = "KRW" if self.market is Market.KR else "USD"
        if self.currency != expected_currency:
            raise ValueError("invalid execution currency")
        if type(self.trade_date) is not str or not _TRADE_DATE.fullmatch(self.trade_date):
            raise ValueError("invalid trade_date")
        try:
            parsed_date = date.fromisoformat(self.trade_date)
        except ValueError as exc:
            raise ValueError("invalid trade_date") from exc
        if parsed_date.isoformat() != self.trade_date:
            raise ValueError("invalid trade_date")
        if not isinstance(self.filled_at, datetime) or self.filled_at.tzinfo is None or self.filled_at.utcoffset() is None:
            raise ValueError("filled_at must be aware")
        if self.completion_status != "COMPLETED_EXECUTION_FACT":
            raise ValueError("invalid completion_status")


@dataclass(frozen=True)
class PortfolioPayloadEvidenceRefV1:
    evidence_id: str
    kind: MaterialKindV1
    source: str
    observed_at: datetime
    content_hash: str
    schema_version: int
    parser_version: str
    trust_class: TrustClass
    freshness_state: FreshnessState
    expires_at: datetime


@dataclass(frozen=True)
class PortfolioEventCandidatePayloadV1:
    schema_version: int
    analysis_kind: AnalysisKind
    candidate_id: str
    delta_id: str
    material_kind: MaterialKindV1
    market: Market
    symbol: str
    tick_id: int
    portfolio_semantic_digest: str
    material_binding_digest: str
    admission_policy_version: str
    admission_policy_digest: str
    admission_reason_codes: tuple[str, ...]
    mask_digest: str | None
    detail: PortfolioPositionDetailV1
    summary: PortfolioCandidateSummaryV1
    completed_execution: CompletedExecutionPayloadV1 | None
    evidence_refs: tuple[PortfolioPayloadEvidenceRefV1, ...]
    payload_digest: str

    def __post_init__(self):
        if self.schema_version != 1 or self.analysis_kind is not AnalysisKind.EVENT:
            raise ValueError("invalid portfolio payload version")
        if self.material_kind not in {
            MaterialKindV1.HOLDINGS_CHANGE, MaterialKindV1.QUOTE_STATE,
            MaterialKindV1.COMPLETED_EXECUTION,
        }:
            raise ValueError("invalid portfolio payload kind")
        for value in (self.portfolio_semantic_digest, self.material_binding_digest,
                      self.admission_policy_digest, self.payload_digest):
            if not isinstance(value, str) or not _HASH.fullmatch(value):
                raise ValueError("invalid portfolio payload digest")
        if self.mask_digest is not None and not _HASH.fullmatch(self.mask_digest):
            raise ValueError("invalid portfolio payload digest")
        if (self.detail.portfolio_scope is not self.summary.portfolio_scope
                or self.detail.market is not self.market
                or self.detail.symbol != self.symbol):
            raise ValueError("portfolio payload identity mismatch")


@dataclass(frozen=True)
class CandidateSuppressionV1:
    candidate_id: str
    reason: CandidateSuppressionReasonV1


@dataclass(frozen=True)
class PortfolioPayloadBuildResultV1:
    status: PortfolioPayloadBuildStatus
    snapshot_status: SnapshotStatus
    portfolio_semantic_digest: str | None
    selected: tuple[PortfolioEventCandidatePayloadV1, ...]
    suppressions: tuple[CandidateSuppressionV1, ...]
    admitted_count: int
    selected_count: int
    suppressed_admitted_count: int

    def __post_init__(self):
        if self.selected_count != len(self.selected):
            raise ValueError("portfolio payload count mismatch")
        if self.suppressed_admitted_count != len(self.suppressions):
            raise ValueError("portfolio payload count mismatch")
        if self.admitted_count != self.selected_count + self.suppressed_admitted_count:
            raise ValueError("portfolio payload count mismatch")


def normalize_portfolio_payload_v1(value: object, _depth: int = 0) -> object:
    if _depth > CANONICAL_MAX_DEPTH:
        raise ValueError("maximum depth exceeded")
    if value is None or type(value) in (bool, int):
        return value
    if type(value) is str:
        if len(value) > CANONICAL_MAX_STRING_LENGTH:
            raise ValueError("maximum string length exceeded")
        return value
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise TypeError("unsupported portfolio payload type")
        return canonical_decimal(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise TypeError("unsupported portfolio payload type")
        return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if isinstance(value, Enum):
        return normalize_portfolio_payload_v1(value.value, _depth + 1)
    if is_dataclass(value):
        params = getattr(type(value), "__dataclass_params__", None)
        if type(value).__module__ != __name__ or not params or not params.frozen:
            raise TypeError("unsupported portfolio payload type")
        return {
            field.name: normalize_portfolio_payload_v1(getattr(value, field.name), _depth + 1)
            for field in fields(value)
        }
    if type(value) in (tuple, list):
        if len(value) > CANONICAL_MAX_LIST_COUNT:
            raise ValueError("maximum list count exceeded")
        return [normalize_portfolio_payload_v1(item, _depth + 1) for item in value]
    if type(value) is frozenset:
        if len(value) > CANONICAL_MAX_LIST_COUNT:
            raise ValueError("maximum list count exceeded")
        normalized = [normalize_portfolio_payload_v1(item, _depth + 1) for item in value]
        encoded = [(canonical_encode(item), item) for item in normalized]
        encoded.sort(key=lambda row: row[0])
        if any(encoded[i - 1][0] == encoded[i][0] for i in range(1, len(encoded))):
            raise TypeError("unsupported portfolio payload type")
        return [item for _, item in encoded]
    if type(value) is dict or type(value) is MappingProxyType:
        if any(type(key) is not str for key in value):
            raise TypeError("unsupported portfolio payload type")
        return {
            key: normalize_portfolio_payload_v1(item, _depth + 1)
            for key, item in value.items()
        }
    raise TypeError("unsupported portfolio payload type")


def compute_portfolio_payload_digest_v1(payload: PortfolioEventCandidatePayloadV1) -> str:
    normalized = normalize_portfolio_payload_v1(payload)
    normalized.pop("payload_digest")
    preimage = {
        "v": 1,
        "domain": "tokmo-v3:portfolio-event-candidate-payload:v1",
        "payload": normalized,
    }
    return hashlib.sha256(canonical_encode(preimage)).hexdigest()


def canonical_portfolio_payload_bytes_v1(payload: PortfolioEventCandidatePayloadV1) -> bytes:
    if compute_portfolio_payload_digest_v1(payload) != payload.payload_digest:
        raise ValueError("portfolio payload digest mismatch")
    return canonical_encode(normalize_portfolio_payload_v1(payload))
