"""Immutable values for the pure quant multifactor core."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum


class MarketV1(str, Enum):
    KR_KOSPI = "KR_KOSPI"
    KR_KOSDAQ = "KR_KOSDAQ"
    US_NYSE_NASDAQ = "US_NYSE_NASDAQ"


class AccountingRegimeV1(str, Enum):
    K_IFRS = "K_IFRS"
    US_GAAP = "US_GAAP"


class SessionDateRuleV1(str, Enum):
    KR_KRX_SESSION = "KR_KRX_SESSION"
    US_NYSE_NASDAQ_SESSION = "US_NYSE_NASDAQ_SESSION"


class AdjustmentV1(str, Enum):
    RAW = "RAW"
    ADJ_SPLIT = "ADJ_SPLIT"
    ADJ_SPLIT_DIV = "ADJ_SPLIT_DIV"


class SectorV1(str, Enum):
    FINANCIALS = "FINANCIALS"
    NON_FINANCIALS = "NON_FINANCIALS"


class PeriodTypeV1(str, Enum):
    ANNUAL = "ANNUAL"
    QUARTERLY = "QUARTERLY"


class OrientationV1(str, Enum):
    NORMAL = "NORMAL"
    INVERSE = "INVERSE"


@dataclass(frozen=True, slots=True)
class FiscalPeriodIdentityV1:
    period_type: PeriodTypeV1
    fiscal_year: int
    fiscal_quarter: int | None = None


@dataclass(frozen=True, slots=True)
class FundamentalSnapshotV1:
    source_ref: str
    fiscal_period: FiscalPeriodIdentityV1
    period_end: date
    published_at: datetime
    revision_of: str | None
    currency: str
    revenue_ttm: Decimal | None
    net_income_ttm: Decimal | None
    gross_profit_ttm: Decimal | None
    total_equity: Decimal | None
    total_assets: Decimal | None
    diluted_shares_outstanding: Decimal | None


@dataclass(frozen=True, slots=True)
class MonthlyCloseV1:
    month_id: str
    session_date: date
    close: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class DailyCloseV1:
    date: date
    close: Decimal
    currency: str


@dataclass(frozen=True, slots=True)
class MemberObservationV1:
    symbol: str
    sector: SectorV1
    membership_source_ref: str
    listed_at_as_of: bool
    current_fundamentals: FundamentalSnapshotV1
    prior_fundamentals: FundamentalSnapshotV1 | None
    monthly_closes: tuple[MonthlyCloseV1, ...]
    daily_closes: tuple[DailyCloseV1, ...]


@dataclass(frozen=True, slots=True)
class CohortInputV1:
    market: MarketV1
    currency: str
    accounting_regime: AccountingRegimeV1
    as_of: datetime
    members: tuple[MemberObservationV1, ...]
    price_source_ref: str
    adjustment: AdjustmentV1
    adjustment_version: str
    price_adjustment_as_of: datetime
    session_date_rule: SessionDateRuleV1
    universe_id: str
    universe_policy_version: str
    membership_as_of: date


@dataclass(frozen=True, slots=True)
class MetricSummaryV1:
    metric_name: str
    orientation: OrientationV1
    valid_count: int
    min_valid_per_metric: int
    winsor_p05: Decimal | None
    winsor_p95: Decimal | None
    sufficient: bool
    applicability: str


@dataclass(frozen=True, slots=True)
class FactorSummaryV1:
    factor_name: str
    metric_summaries: tuple[MetricSummaryV1, ...]
    sufficient_metric_count: int


@dataclass(frozen=True, slots=True)
class MetricResultV1:
    metric_name: str
    raw_value: Decimal | None
    unit: str
    available: bool
    unavailable_reason: str | None
    applicability: str
    winsor_p05: Decimal | None
    winsor_p95: Decimal | None
    winsorized_value: Decimal | None
    orientation: OrientationV1
    average_rank: Decimal | None
    percentile: Decimal | None
    valid_count: int
    metric_sufficient: bool


@dataclass(frozen=True, slots=True)
class FactorResultV1:
    factor_name: str
    metrics: tuple[MetricResultV1, ...]
    metric_coverage: str
    factor_score: Decimal | None
    contribution: Decimal | None


@dataclass(frozen=True, slots=True)
class MemberResultV1:
    symbol: str
    sector: SectorV1
    close_at_as_of: Decimal | None
    factors: tuple[FactorResultV1, ...]
    factor_count: int
    fundamental_factor_count: int
    composite_score: Decimal | None
    coverage: str


@dataclass(frozen=True, slots=True)
class WeightEntryV1:
    factor_name: str
    weight: Decimal


@dataclass(frozen=True, slots=True)
class QuantMultifactorPackV1:
    program_id: str
    version: str
    kind: str
    formula_version: str
    spec_digest: str
    input_digest: str
    pack_digest: str
    market: MarketV1
    currency: str
    accounting_regime: AccountingRegimeV1
    as_of: datetime
    price_source_ref: str
    adjustment: AdjustmentV1
    adjustment_version: str
    price_adjustment_as_of: datetime
    session_date_rule: SessionDateRuleV1
    universe_id: str
    universe_policy_version: str
    membership_as_of: date
    cohort_size: int
    min_cohort: int
    coverage: str
    factor_summaries: tuple[FactorSummaryV1, ...]
    member_results: tuple[MemberResultV1, ...]
    weights: tuple[WeightEntryV1, ...]
    composite_policy: str
    min_factors_for_composite: int
    min_fundamental_factors_for_composite: int
    limitations: tuple[str, ...]
