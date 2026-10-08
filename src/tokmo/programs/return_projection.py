"""Deterministic Pulse return projection (Plan §16).

Pure derivation — zero wire, zero canonical rows. Inputs are the canonical
holdings snapshot and latest quotes; the output binds the three instants
``(holdings_as_of, quote_observed_at, computed_at)`` and never merges
different-instant values into one "now". Every fail-closed state from §16.3
is typed; the KRW aggregate exists only under fresh base-USD/quote-KRW FX
evidence and no exchange rate is ever estimated.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Mapping

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)
from tokmo.values.current_holdings import CurrentHoldingsV1, classify_market
from tokmo.values.portfolio_calculations import (
    FxEvidenceEnvelopeV1,
    PricingState,
    RatioState,
    RatioValueV1,
)

# Quote freshness for a 60s lane: one 300s cadence window without a fresher
# observation marks the quote stale (§16.3).
RETURN_FRESHNESS_MAX_AGE_SECONDS = 300


@dataclass(frozen=True)
class PositionReturnV1:
    account_scope: str
    portfolio_scope: str
    market: "str | None"
    symbol: str
    currency: "str | None"
    quantity: Decimal
    average_cost: Decimal
    cost_basis_native: Decimal
    current_price: "Decimal | None"
    market_value_native: "Decimal | None"
    unrealized_pnl_native: "Decimal | None"
    unrealized_return: RatioValueV1
    pricing_state: PricingState
    calculation_barrier: "str | None"
    quote_observed_at: "datetime | None"


@dataclass(frozen=True)
class ScopeReturnTotalsV1:
    portfolio_scope: str
    position_count: int
    priced_position_count: int
    cost_basis_by_currency: Mapping
    market_value_by_currency: Mapping
    unrealized_pnl_by_currency: Mapping


@dataclass(frozen=True)
class ReturnProjectionV1:
    holdings_as_of: datetime
    computed_at: datetime
    positions: tuple
    scope_totals: tuple
    krw_aggregate_state: str
    holdings_reconciliation_pending: bool


def _decimal(value) -> "Decimal | None":
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return number if number.is_finite() else None


def _scope_kind(scope: str) -> "str | None":
    if ACCOUNT_SCOPE_RE.fullmatch(scope) is not None:
        return "PERSONAL"
    if PENSION_SCOPE_RE.fullmatch(scope) is not None:
        return "PENSION"
    return None


def _validate_fx(fx, computed_at) -> str:
    if fx is None:
        return "unavailable"
    if not isinstance(fx, FxEvidenceEnvelopeV1):
        raise ValueError("invalid FX evidence envelope")
    if fx.base_currency != "USD" or fx.quote_currency != "KRW":
        raise ValueError("FX envelope must be base USD / quote KRW")
    if _decimal(fx.rate) is None or _decimal(fx.rate) <= 0:
        raise ValueError("invalid FX rate")
    if not fx.valid_from <= computed_at < fx.valid_until:
        return "unavailable"
    age = (computed_at - fx.observed_at).total_seconds()
    if age < 0 or age > fx.max_age_seconds:
        return "unavailable"
    return "available"


def project_returns(*, holdings: CurrentHoldingsV1, quotes: Mapping,
                    computed_at: datetime,
                    fx_envelope: "FxEvidenceEnvelopeV1 | None" = None,
                    holdings_reconciliation_pending: bool = False,
                    freshness_max_age_seconds: int = RETURN_FRESHNESS_MAX_AGE_SECONDS,
                    ) -> ReturnProjectionV1:
    if not isinstance(holdings, CurrentHoldingsV1):
        raise ValueError("projection requires the canonical holdings snapshot")
    if (not isinstance(computed_at, datetime) or computed_at.tzinfo is None
            or computed_at.utcoffset() is None):
        raise ValueError("computed_at must be aware")
    krw_state = _validate_fx(fx_envelope, computed_at)
    positions = []
    totals = {}
    for account in holdings.accounts:
        scope_kind = _scope_kind(account.account_scope)
        if scope_kind is None:
            continue
        bucket = totals.setdefault(scope_kind, {
            "count": 0, "priced": 0, "cost": {}, "value": {}, "pnl": {},
        })
        for item in account.holdings:
            quantity = _decimal(item.quantity)
            average_cost = _decimal(item.avg_price)
            market = classify_market(item.market_raw, item.market_field)
            if quantity is None or average_cost is None:
                positions.append(PositionReturnV1(
                    account.account_scope, scope_kind,
                    None if market is None else market.value, item.symbol,
                    item.currency, Decimal(0), Decimal(0), Decimal(0), None,
                    None, None,
                    RatioValueV1(RatioState.UNAVAILABLE_UNPRICED, None),
                    PricingState.UNPRICED, "holding_decimal_invalid", None,
                ))
                continue
            cost_basis = quantity * average_cost
            quote = quotes.get(item.symbol) if quotes is not None else None
            barrier = None
            price = None
            observed = None
            pricing = PricingState.UNPRICED
            if quote is not None:
                price = _decimal(quote.last_price)
                observed = quote.timestamp or quote.fetched_at
                if price is None:
                    barrier = "quote_decimal_invalid"
                    price = None
                    observed = None
                elif (item.currency is not None
                        and quote.currency_raw != item.currency):
                    barrier = "currency_mismatch"
                    price = None
                    observed = None
                else:
                    age = (computed_at - observed).total_seconds()
                    pricing = (PricingState.STALE_QUOTE
                               if age > freshness_max_age_seconds
                               else PricingState.PRICED)
            if price is None:
                market_value = None
                pnl = None
                ratio = RatioValueV1(RatioState.UNAVAILABLE_UNPRICED, None)
            else:
                market_value = quantity * price
                pnl = market_value - cost_basis
                if cost_basis == 0:
                    ratio = RatioValueV1(RatioState.ZERO_COST_UNDEFINED, None)
                else:
                    ratio = RatioValueV1(RatioState.DEFINED, pnl / cost_basis)
            positions.append(PositionReturnV1(
                account.account_scope, scope_kind,
                None if market is None else market.value, item.symbol,
                item.currency, quantity, average_cost, cost_basis, price,
                market_value, pnl, ratio, pricing, barrier, observed,
            ))
            currency = item.currency or "UNKNOWN"
            bucket["count"] += 1
            bucket["cost"][currency] = bucket["cost"].get(
                currency, Decimal(0)) + cost_basis
            if market_value is not None:
                bucket["priced"] += 1
                bucket["value"][currency] = bucket["value"].get(
                    currency, Decimal(0)) + market_value
                bucket["pnl"][currency] = bucket["pnl"].get(
                    currency, Decimal(0)) + pnl
    scope_totals = tuple(
        ScopeReturnTotalsV1(
            scope_kind, bucket["count"], bucket["priced"],
            dict(bucket["cost"]), dict(bucket["value"]), dict(bucket["pnl"]),
        )
        for scope_kind, bucket in sorted(totals.items())
    )
    return ReturnProjectionV1(
        holdings.captured_at, computed_at, tuple(positions), scope_totals,
        krw_state, bool(holdings_reconciliation_pending),
    )
