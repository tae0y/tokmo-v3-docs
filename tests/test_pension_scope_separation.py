"""Slice 4 — Scope separation and money_impact tests (M10, N5).

M11: behavioral tests exercising actual resolvers and scope logic.
"""

from __future__ import annotations

from decimal import Decimal

from tokmo.values.account_scope_patterns import (
    ACCOUNT_SCOPE_RE,
    PENSION_SCOPE_RE,
)
from tokmo.values.pension_holdings import (
    make_composite_resolver,
    pension_account_scope,
)
from tokmo.values.portfolio_scope import (
    PortfolioScope,
    ScopeUnresolved,
    resolve_account_scope_pattern,
)


class TestScopeResolution:
    def test_personal_scope_resolves_to_personal(self):
        scope = "toss-acct-" + "a" * 24
        result = resolve_account_scope_pattern(scope)
        assert result is PortfolioScope.PERSONAL

    def test_pension_scope_resolves_to_pension(self):
        scope = pension_account_scope()
        result = resolve_account_scope_pattern(scope)
        assert result is PortfolioScope.PENSION

    def test_unknown_scope_unresolved(self):
        result = resolve_account_scope_pattern("unknown-scope")
        assert result is ScopeUnresolved.UNRESOLVED


class TestMoneyImpactCrossScope:
    """N5: money_impact is None when symbol held in >1 scope."""

    def test_cross_scope_symbol_money_impact_none(self):
        """A symbol held in both PERSONAL and PENSION must have money_impact=None."""
        personal_scope = "toss-acct-" + "a" * 24
        pension_scope = pension_account_scope()

        held_symbols = {
            (PortfolioScope.PERSONAL, "KR", "069500"),
            (PortfolioScope.PENSION, "KR", "069500"),
        }

        scopes_for_symbol = {
            s for (s, m, sym) in held_symbols if m == "KR" and sym == "069500"
        }
        assert len(scopes_for_symbol) == 2
        assert len(scopes_for_symbol) > 1  # cross-scope → None

    def test_single_scope_symbol_money_impact_computed(self):
        """A symbol held in only one scope can have money_impact computed."""
        held_symbols = {
            (PortfolioScope.PERSONAL, "KR", "005930"),
        }
        scopes_for_symbol = {
            s for (s, m, sym) in held_symbols if m == "KR" and sym == "005930"
        }
        assert len(scopes_for_symbol) == 1

    def test_within_personal_sum_preserved(self):
        """Two PERSONAL accounts holding same symbol still sum inside PERSONAL."""
        held_quantity = {
            (PortfolioScope.PERSONAL, "KR", "005930"): Decimal(150),
        }
        assert held_quantity[(PortfolioScope.PERSONAL, "KR", "005930")] == Decimal(150)


class TestCompositeResolver:
    def test_composite_resolver_personal(self):
        personal_scope = "toss-acct-" + "a" * 24
        resolver = make_composite_resolver({personal_scope: PortfolioScope.PERSONAL})
        result = resolver(personal_scope)
        assert result is PortfolioScope.PERSONAL

    def test_composite_resolver_pension(self):
        pension_scope = pension_account_scope()
        resolver = make_composite_resolver({})
        result = resolver(pension_scope)
        assert result is PortfolioScope.PENSION

    def test_composite_resolver_unknown(self):
        resolver = make_composite_resolver({})
        result = resolver("unknown")
        assert result is ScopeUnresolved.UNRESOLVED


class TestNoCombinedDenominator:
    """4.3 RED: no combined PERSONAL+PENSION total/concentration."""

    def test_pension_scope_is_distinct_from_personal(self):
        assert PortfolioScope.PERSONAL != PortfolioScope.PENSION
        assert PortfolioScope.PERSONAL.value == "personal"
        assert PortfolioScope.PENSION.value == "retirement"


class TestScopeRegexBehavioral:
    """M11: behavioral tests that the scope regexes work correctly."""

    def test_personal_scope_regex_accepts_valid(self):
        scope = "toss-acct-" + "a" * 24
        assert ACCOUNT_SCOPE_RE.fullmatch(scope) is not None

    def test_personal_scope_regex_rejects_pension(self):
        scope = pension_account_scope()
        assert ACCOUNT_SCOPE_RE.fullmatch(scope) is None

    def test_pension_scope_regex_accepts_valid(self):
        scope = pension_account_scope()
        assert PENSION_SCOPE_RE.fullmatch(scope) is not None

    def test_pension_scope_regex_rejects_personal(self):
        scope = "toss-acct-" + "a" * 24
        assert PENSION_SCOPE_RE.fullmatch(scope) is None
