"""Toss investor trading types: amounts, breakdowns, records."""

from dataclasses import dataclass, field
from datetime import date, datetime

from tokmo.values._primitives import MISSING, Missing

__all__ = [
    "_INSTITUTION_BREAKDOWN_KEYS",
    "InstitutionTradingBreakdown",
    "InvestorTradingAmount",
    "InvestorTradingRecord",
    "InvestorTradingResponse",
]


@dataclass(frozen=True)
class InvestorTradingAmount:
    buy_amount: str = field(repr=False)
    sell_amount: str = field(repr=False)


_INSTITUTION_BREAKDOWN_KEYS = (
    "financialInvestment",
    "insurance",
    "trust",
    "privateEquityFund",
    "bank",
    "otherFinancialInstitution",
    "pensionFund",
)


@dataclass(frozen=True)
class InstitutionTradingBreakdown:
    financial_investment: InvestorTradingAmount = field(repr=False)
    insurance: InvestorTradingAmount = field(repr=False)
    trust: InvestorTradingAmount = field(repr=False)
    private_equity_fund: InvestorTradingAmount = field(repr=False)
    bank: InvestorTradingAmount = field(repr=False)
    other_financial_institution: InvestorTradingAmount = field(repr=False)
    pension_fund: InvestorTradingAmount = field(repr=False)


@dataclass(frozen=True)
class InvestorTradingRecord:
    date: date
    updated_at: datetime
    individual: InvestorTradingAmount = field(repr=False)
    foreigner: InvestorTradingAmount = field(repr=False)
    institution_buy_amount: str = field(repr=False)
    institution_sell_amount: str = field(repr=False)
    institution_breakdown: InstitutionTradingBreakdown = field(repr=False)
    other_corporation: InvestorTradingAmount = field(repr=False)


@dataclass(frozen=True)
class InvestorTradingResponse:
    records: tuple[InvestorTradingRecord, ...] = field(repr=False)
    next_until: date | None | Missing = MISSING


@dataclass(frozen=True)
class InvestorTradingVolume:
    buy_volume: str = field(repr=False)
    sell_volume: str = field(repr=False)
    net_buy_volume: str = field(repr=False)


@dataclass(frozen=True)
class StockInvestorTradingRecord:
    """Selected trading fields; holdings/CFD are not trading-flow evidence."""

    date: date
    updated_at: datetime
    individual: InvestorTradingVolume | None | Missing = field(repr=False)
    foreigner: InvestorTradingVolume = field(repr=False)
    institution: InvestorTradingVolume = field(repr=False)


@dataclass(frozen=True)
class StockInvestorTradingResponse:
    records: tuple[StockInvestorTradingRecord, ...] = field(repr=False)
    next_until: date | None | Missing = MISSING
