"""Frozen public values for canonical portfolio snapshot calculations."""

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.material_pack import JobKind

# T1: PortfolioScope, ScopeUnresolved, and friends are now owned by
# portfolio_scope.py.  Re-export for backward compatibility.
from tokmo.values.portfolio_scope import (  # noqa: F401
    AccountScopeResolver,
    PortfolioScope,
    ScopeResolution,
    ScopeUnresolved,
    resolve_account_scope_pattern,
)
from tokmo.values.session_gate import Market


class SnapshotStatus(str, Enum):
    COMPLETE = "COMPLETE"
    SCOPE_RESOLUTION_INCOMPLETE = "SCOPE_RESOLUTION_INCOMPLETE"
    MATERIAL_INVALID = "MATERIAL_INVALID"


class PricingState(str, Enum):
    PRICED = "PRICED"
    UNPRICED = "UNPRICED"
    STALE_QUOTE = "STALE_QUOTE"


class RatioState(str, Enum):
    DEFINED = "DEFINED"
    ZERO_COST_UNDEFINED = "ZERO_COST_UNDEFINED"
    UNAVAILABLE_UNPRICED = "UNAVAILABLE_UNPRICED"
    UNAVAILABLE_FX = "UNAVAILABLE_FX"
    ZERO_DENOMINATOR_UNDEFINED = "ZERO_DENOMINATOR_UNDEFINED"


@dataclass(frozen=True)
class RatioValueV1:
    state: RatioState
    value: Decimal | None


class FxStatus(str, Enum):
    NOT_REQUIRED_SINGLE_KRW = "NOT_REQUIRED_SINGLE_KRW"
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    INVALID = "INVALID"


class TradeMarketState(str, Enum):
    ABSENT = "ABSENT"
    EXCLUDED_NON_PROCEED = "EXCLUDED_NON_PROCEED"
    STALE = "STALE"
    COMPLETE_EMPTY = "COMPLETE_EMPTY"
    COMPLETE_WITH_EXECUTIONS = "COMPLETE_WITH_EXECUTIONS"


class TradeGlobalQualityState(str, Enum):
    COMPLETE = "COMPLETE"
    UNATTRIBUTED_PARTIAL = "UNATTRIBUTED_PARTIAL"
    PAGINATION_PENDING = "PAGINATION_PENDING"
    PAGINATION_PENDING_AND_UNATTRIBUTED_PARTIAL = (
        "PAGINATION_PENDING_AND_UNATTRIBUTED_PARTIAL"
    )


class ConcentrationBasis(str, Enum):
    PRICED_MARKET_VALUE_KRW = "PRICED_MARKET_VALUE_KRW"
    TOTAL_COST_BASIS_KRW = "TOTAL_COST_BASIS_KRW"


@dataclass(frozen=True)
class MaterialEvidenceBindingV1:
    job: JobKind
    pack_tick_id: int
    captured_at: datetime
    content_hash: str
    file_sha256: str


@dataclass(frozen=True)
class FxEvidenceEnvelopeV1:
    schema_version: int
    source: str
    base_currency: str
    quote_currency: str
    rate: str
    observed_at: datetime
    acquired_at: datetime
    valid_from: datetime
    valid_until: datetime
    max_age_seconds: int
    reference_at: datetime
    reference_material_digest: str
    content_hash: str


@dataclass(frozen=True)
class PositionCalcV1:
    portfolio_scope: PortfolioScope
    market: Market
    symbol: str
    currency: str
    quantity: Decimal
    average_cost: Decimal
    total_cost_basis_native: Decimal
    current_price: Decimal | None
    priced_market_value_native: Decimal | None
    priced_cost_basis_native: Decimal | None
    unrealized_pnl_native: Decimal | None
    unrealized_return: RatioValueV1
    pricing_state: PricingState
    currency_partition_market_value_weight: Decimal | None
    market_currency_partition_market_value_weight: Decimal | None
    currency_partition_total_cost_basis_weight: Decimal | None
    market_currency_partition_total_cost_basis_weight: Decimal | None
    scope_krw_priced_market_value_weight: Decimal | None
    scope_krw_total_cost_basis_weight: Decimal | None
    internal_total_cost_basis_krw: Decimal | None
    priced_market_value_krw: Decimal | None
    internal_priced_cost_basis_krw: Decimal | None
    unrealized_pnl_krw: Decimal | None
    fx_binding_digest: str | None
    holdings_observed_at: datetime
    quote_observed_at: datetime | None
    pension_freshness_class: str | None = None
    pension_source_as_of: datetime | None = None


@dataclass(frozen=True)
class PartitionTotalsV1:
    portfolio_scope: PortfolioScope
    market: Market
    currency: str
    position_count: int
    priced_position_count: int
    unpriced_position_count: int
    total_quantity: Decimal
    total_cost_basis_native: Decimal
    priced_cost_basis_native: Decimal
    priced_market_value_native: Decimal
    unrealized_pnl_native: Decimal
    unrealized_return: RatioValueV1
    gross_gains_native: Decimal
    gross_losses_native: Decimal
    internal_total_cost_basis_krw: Decimal | None
    internal_priced_cost_basis_krw: Decimal | None
    priced_market_value_krw: Decimal | None
    unrealized_pnl_krw: Decimal | None
    gross_gains_krw: Decimal | None
    gross_losses_krw: Decimal | None
    fx_binding_digest: str | None
    priced_cost_basis_coverage_ratio: RatioValueV1
    profitable_count: int
    loss_count: int
    flat_count: int


@dataclass(frozen=True)
class TopPositionV1:
    portfolio_scope: PortfolioScope
    market: Market
    symbol: str
    currency: str
    basis: ConcentrationBasis
    value_krw: Decimal
    scope_krw_weight: Decimal


@dataclass(frozen=True)
class ScopeKrwTotalsV1:
    portfolio_scope: PortfolioScope
    fx_status: FxStatus
    internal_total_cost_basis_krw: Decimal | None
    internal_priced_cost_basis_krw: Decimal | None
    priced_market_value_krw: Decimal | None
    unrealized_pnl_krw: Decimal | None
    unrealized_return: RatioValueV1
    gross_gains_krw: Decimal | None
    gross_losses_krw: Decimal | None
    top_by_priced_market_value: tuple[TopPositionV1, ...]
    top_by_total_cost_basis: tuple[TopPositionV1, ...]
    concentration_top_n: int


@dataclass(frozen=True)
class PositionPresentationV1:
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
    unrealized_return: RatioValueV1
    pricing_state: PricingState
    fx_binding_digest: str | None


@dataclass(frozen=True)
class PartitionPresentationV1:
    portfolio_scope: PortfolioScope
    market: Market
    currency: str
    total_cost_basis_native: Decimal
    priced_cost_basis_native: Decimal
    evaluated_market_value_native: Decimal
    evaluated_market_value_krw: Decimal | None
    unrealized_pnl_native: Decimal
    unrealized_pnl_krw: Decimal | None
    unrealized_return: RatioValueV1
    priced_cost_basis_coverage_ratio: RatioValueV1
    fx_binding_digest: str | None


@dataclass(frozen=True)
class ScopePresentationV1:
    portfolio_scope: PortfolioScope
    fx_status: FxStatus
    evaluated_market_value_krw: Decimal | None
    unrealized_pnl_krw: Decimal | None
    unrealized_return: RatioValueV1
    partitions: tuple[PartitionPresentationV1, ...]
    freshness_class: str | None = None  # m6: pension freshness per scope


@dataclass(frozen=True)
class PortfolioPresentationV1:
    positions: tuple[PositionPresentationV1, ...]
    scopes: tuple[ScopePresentationV1, ...]
    material_bindings: tuple[MaterialEvidenceBindingV1, ...]


@dataclass(frozen=True)
class CompletedExecutionFactV1:
    portfolio_scope: PortfolioScope
    source_record_key_digest: str
    trade_date: str
    transaction_type_raw: str
    instrument_raw: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal
    medium_raw: str
    currency_raw: str | None
    classified_market: Market
    filled_at: datetime


@dataclass(frozen=True)
class TradeMarketCompletenessV1:
    market: Market
    market_readiness: MarketReadiness
    eligible_for_current_snapshot: bool
    state: TradeMarketState
    material_present_for_market: bool
    stale: bool
    inserted_count: int
    projected_execution_count: int


@dataclass(frozen=True)
class OpaqueTradeAccountQualityV1:
    duplicate_count: int
    quarantine_count: int
    incomplete_count: int


@dataclass(frozen=True)
class TradeGlobalQualityV1:
    state: TradeGlobalQualityState
    material_present: bool
    pagination_pending: bool
    account_qualities: tuple[OpaqueTradeAccountQualityV1, ...]
    duplicate_count: int
    quarantine_count: int
    incomplete_count: int


@dataclass(frozen=True)
class TradeFactsV1:
    markets: tuple[TradeMarketCompletenessV1, ...]
    global_quality: TradeGlobalQualityV1
    completed_executions: tuple[CompletedExecutionFactV1, ...]


@dataclass(frozen=True)
class MarketPopulationStatusV1:
    market: Market
    readiness: MarketReadiness
    proceed: bool
    stale_jobs: tuple[JobKind, ...]
    holdings_material_present: bool
    quotes_material_present: bool
    trades_material_present: bool
    included_position_count: int
    included_execution_count: int


@dataclass(frozen=True)
class PortfolioSnapshotBodyV1:
    schema_version: int
    tick_id: int
    reference_at: datetime
    market_population: tuple[MarketPopulationStatusV1, ...]
    positions: tuple[PositionCalcV1, ...]
    partition_totals: tuple[PartitionTotalsV1, ...]
    scope_krw_totals: tuple[ScopeKrwTotalsV1, ...]
    trade_facts: TradeFactsV1
    material_bindings: tuple[MaterialEvidenceBindingV1, ...]
    fx_evidence: FxEvidenceEnvelopeV1 | None
    fx_binding_digest: str | None


@dataclass(frozen=True)
class PortfolioSnapshotEnvelopeV1:
    status: SnapshotStatus
    body: PortfolioSnapshotBodyV1 | None
    semantic_digest: str | None
    unresolved_account_count: int
    invalid_reason: str | None
    computed_at: datetime | None = None
