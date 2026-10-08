"""Deterministic return projection (Plan §16): wire 0, no canonical DB."""

from datetime import datetime, timezone
from decimal import Decimal

import pytest

from tokmo.programs.return_projection import (
    RETURN_FRESHNESS_MAX_AGE_SECONDS,
    project_returns,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.portfolio_calculations import (
    FxEvidenceEnvelopeV1,
    PricingState,
    RatioState,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


TOSS_SCOPE = "toss-acct-" + "a" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24
HOLDINGS_AS_OF = _utc("2026-08-10T00:55:00Z")
COMPUTED_AT = _utc("2026-08-10T01:00:00Z")


def _holdings(items_by_scope=None):
    items_by_scope = items_by_scope or {
        TOSS_SCOPE: (
            HoldingItemV1("005930", "10", "60000", "KRW", "KR", "market"),
            HoldingItemV1("SPCX", "4", "100.5", "USD", "US", "market"),
        ),
        PENSION_SCOPE: (
            HoldingItemV1("069500", "700", "30000", "KRW", "KR", "market"),
        ),
    }
    identity = {}
    accounts = []
    for ordinal, (scope, items) in enumerate(items_by_scope.items(), start=1):
        label = ("pension-excel-v1" if scope.startswith("pension")
                 else "personal")
        identity[scope] = AccountScopeIdentityV1(scope, label, ordinal, None)
        accounts.append(AccountHoldingsV1(scope, ordinal, tuple(items)))
    return CurrentHoldingsV1(
        1, tuple(accounts), True, HOLDINGS_AS_OF, identity)


def _quote(symbol, price, currency="KRW", observed="2026-08-10T00:59:30Z"):
    return TossCurrentPriceFact(
        source="toss-current-price-v1", symbol=symbol, last_price=price,
        currency_raw=currency, timestamp=_utc(observed),
        fetched_at=_utc(observed),
    )


def _project(quotes=None, holdings=None, computed_at=COMPUTED_AT, fx=None,
             reconciliation_pending=False):
    quotes = quotes if quotes is not None else {
        "005930": _quote("005930", "70000"),
        "SPCX": _quote("SPCX", "90.25", "USD"),
        "069500": _quote("069500", "33000"),
    }
    return project_returns(
        holdings=holdings or _holdings(),
        quotes=quotes,
        computed_at=computed_at,
        fx_envelope=fx,
        holdings_reconciliation_pending=reconciliation_pending,
    )


def _position(projection, symbol, scope=None):
    for position in projection.positions:
        if position.symbol == symbol and (
                scope is None or position.account_scope == scope):
            return position
    raise AssertionError(f"position {symbol} missing")


def test_gain_loss_and_flat_arithmetic_is_exact_decimal():
    projection = _project()
    gain = _position(projection, "005930")
    assert gain.cost_basis_native == Decimal("600000")
    assert gain.market_value_native == Decimal("700000")
    assert gain.unrealized_pnl_native == Decimal("100000")
    assert gain.unrealized_return.state is RatioState.DEFINED
    assert gain.unrealized_return.value == Decimal(
        "100000") / Decimal("600000")
    loss = _position(projection, "SPCX")
    assert loss.cost_basis_native == Decimal("402.0")
    assert loss.unrealized_pnl_native == Decimal("361.00") - Decimal("402.0")
    flat = _project(quotes={
        "005930": _quote("005930", "60000"),
        "SPCX": _quote("SPCX", "100.5", "USD"),
        "069500": _quote("069500", "30000"),
    })
    assert _position(flat, "005930").unrealized_pnl_native == Decimal("0")


def test_zero_cost_basis_never_divides():
    holdings = _holdings({
        TOSS_SCOPE: (HoldingItemV1("BONUS", "5", "0", "KRW", "KR", "market"),),
    })
    projection = _project(
        holdings=holdings, quotes={"BONUS": _quote("BONUS", "1000")})
    position = _position(projection, "BONUS")
    assert position.unrealized_return.state is RatioState.ZERO_COST_UNDEFINED
    assert position.unrealized_return.value is None
    assert position.unrealized_pnl_native == Decimal("5000")


def test_missing_quote_is_unpriced_and_stale_quote_is_labelled():
    projection = _project(quotes={
        "SPCX": _quote("SPCX", "90.25", "USD"),
        "069500": _quote(
            "069500", "33000",
            observed="2026-08-09T20:00:00Z"),  # hours old
    })
    unpriced = _position(projection, "005930")
    assert unpriced.pricing_state is PricingState.UNPRICED
    assert unpriced.market_value_native is None
    assert unpriced.unrealized_return.state is RatioState.UNAVAILABLE_UNPRICED
    stale = _position(projection, "069500")
    assert stale.pricing_state is PricingState.STALE_QUOTE
    fresh = _position(projection, "SPCX")
    assert fresh.pricing_state is PricingState.PRICED


def test_currency_mismatch_forbids_the_calculation():
    projection = _project(quotes={
        "005930": _quote("005930", "70000", currency="USD"),
        "SPCX": _quote("SPCX", "90.25", "USD"),
        "069500": _quote("069500", "33000"),
    })
    mismatched = _position(projection, "005930")
    assert mismatched.pricing_state is PricingState.UNPRICED
    assert mismatched.calculation_barrier == "currency_mismatch"
    assert mismatched.market_value_native is None


def test_scope_partition_separates_personal_and_pension():
    projection = _project()
    scopes = {total.portfolio_scope for total in projection.scope_totals}
    assert scopes == {"PERSONAL", "PENSION"}
    personal = next(total for total in projection.scope_totals
                    if total.portfolio_scope == "PERSONAL")
    pension = next(total for total in projection.scope_totals
                   if total.portfolio_scope == "PENSION")
    # Denominators never mix scopes (§16.4).
    assert personal.cost_basis_by_currency["KRW"] == Decimal("600000")
    assert pension.cost_basis_by_currency["KRW"] == Decimal("21000000")


def test_krw_aggregate_requires_fresh_fx_evidence():
    projection = _project()
    assert projection.krw_aggregate_state == "unavailable"
    fx = FxEvidenceEnvelopeV1(
        schema_version=1, source="toss-fx", base_currency="USD",
        quote_currency="KRW", rate="1500",
        observed_at=_utc("2026-08-10T00:59:00Z"),
        acquired_at=_utc("2026-08-10T00:59:00Z"),
        valid_from=_utc("2026-08-10T00:59:00Z"),
        valid_until=_utc("2026-08-10T01:09:00Z"),
        max_age_seconds=600,
        reference_at=COMPUTED_AT,
        reference_material_digest="d" * 64,
        content_hash="c" * 64,
    )
    with_fx = _project(fx=fx)
    assert with_fx.krw_aggregate_state == "available"
    expired = FxEvidenceEnvelopeV1(
        schema_version=1, source="toss-fx", base_currency="USD",
        quote_currency="KRW", rate="1500",
        observed_at=_utc("2026-08-09T00:59:00Z"),
        acquired_at=_utc("2026-08-09T00:59:00Z"),
        valid_from=_utc("2026-08-09T00:59:00Z"),
        valid_until=_utc("2026-08-09T01:09:00Z"),
        max_age_seconds=600,
        reference_at=COMPUTED_AT,
        reference_material_digest="d" * 64,
        content_hash="c" * 64,
    )
    assert _project(fx=expired).krw_aggregate_state == "unavailable"


def test_wrong_fx_envelope_direction_is_rejected():
    fx = FxEvidenceEnvelopeV1(
        schema_version=1, source="toss-fx", base_currency="KRW",
        quote_currency="USD", rate="0.00066",
        observed_at=_utc("2026-08-10T00:59:00Z"),
        acquired_at=_utc("2026-08-10T00:59:00Z"),
        valid_from=_utc("2026-08-10T00:59:00Z"),
        valid_until=_utc("2026-08-10T01:09:00Z"),
        max_age_seconds=600,
        reference_at=COMPUTED_AT,
        reference_material_digest="d" * 64,
        content_hash="c" * 64,
    )
    with pytest.raises(ValueError):
        _project(fx=fx)


def test_three_instants_are_bound_to_every_projection():
    projection = _project()
    assert projection.holdings_as_of == HOLDINGS_AS_OF
    assert projection.computed_at == COMPUTED_AT
    position = _position(projection, "005930")
    assert position.quote_observed_at == _utc("2026-08-10T00:59:30Z")
    unpriced = _project(quotes={
        "SPCX": _quote("SPCX", "90.25", "USD"),
        "069500": _quote("069500", "33000"),
    })
    assert _position(unpriced, "005930").quote_observed_at is None


def test_reconciliation_pending_flag_is_carried_not_invented():
    assert _project().holdings_reconciliation_pending is False
    assert _project(
        reconciliation_pending=True).holdings_reconciliation_pending is True


def test_freshness_constant_matches_the_documented_policy():
    assert RETURN_FRESHNESS_MAX_AGE_SECONDS == 300
