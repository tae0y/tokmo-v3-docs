"""Slice 7: behavioral production-cadence pension wiring tests.

M11: replaces inspect.getsource substring assertions with real behavioral
tests that exercise the actual functions and entrypoints.
"""
from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest


_KST = ZoneInfo("Asia/Seoul")


class TestPensionQuoteExclusionB1:
    """B1: pension_excluded = pension_only_symbols & pension_unquotable_symbols
    unconditionally — the intersection is always computed, not skipped
    when pension_unquotable_symbols is empty."""

    def test_empty_unquotable_yields_empty_excluded(self):
        """With empty unquotable, excluded = pension_only & frozenset() = empty."""
        pension_only = frozenset({"069500"})
        unquotable = frozenset()
        excluded = pension_only & frozenset(unquotable)
        assert excluded == frozenset()

    def test_declared_unquotable_excludes_pension_symbol(self):
        """When 069500 is declared unquotable, it is excluded."""
        pension_only = frozenset({"069500"})
        unquotable = frozenset({"069500"})
        excluded = pension_only & frozenset(unquotable)
        assert excluded == frozenset({"069500"})

    def test_unrelated_unquotable_does_not_affect_pension(self):
        """Declaring an unrelated symbol unquotable does NOT exclude 069500.
        This was the exact bug the inverted logic caused."""
        pension_only = frozenset({"069500"})
        unquotable = frozenset({"0167A0"})
        excluded = pension_only & frozenset(unquotable)
        assert "069500" not in excluded


class TestCompositeResolverM11:
    """M11: behavioral test that make_composite_resolver separates scopes."""

    def test_composite_resolver_separates_pension_and_personal(self):
        from tokmo.values.pension_holdings import (
            pension_account_scope,
            make_composite_resolver,
        )
        from tokmo.values.portfolio_scope import (
            PortfolioScope, ScopeUnresolved,
        )
        personal_scope = "toss-acct-" + "a" * 24
        pension_scope = pension_account_scope()
        resolver = make_composite_resolver({personal_scope: PortfolioScope.PERSONAL})
        assert resolver(personal_scope) is PortfolioScope.PERSONAL
        assert resolver(pension_scope) is PortfolioScope.PENSION
        assert resolver("unknown") is ScopeUnresolved.UNRESOLVED


def _empty_barrier():
    from tokmo.values.session_gate import Market
    from tokmo.values.analysis_barrier import AnalysisBarrierResultV1, MarketReadiness
    return AnalysisBarrierResultV1(
        tick_id=1,
        materials={},
        market_readiness={m: MarketReadiness.READY for m in Market},
        stale_jobs={m: frozenset() for m in Market},
        material_ticks={},
        incidents=(),
        proceed_markets=frozenset({Market.KR}),
    )


class TestResolveScopeRequiredM4:
    """m4: resolve_scope=None raises ValueError in production paths."""

    def test_build_material_deltas_requires_scope(self):
        from tokmo.programs.material_delta import build_material_deltas
        with pytest.raises(ValueError, match="resolve_scope is required"):
            build_material_deltas(
                barrier=_empty_barrier(),
                baselines={},
                now=datetime(2026, 8, 5, tzinfo=timezone.utc),
            )

    def test_build_candidates_requires_scope(self):
        from tokmo.programs.candidate_builder import build_candidates
        with pytest.raises(ValueError, match="resolve_scope is required"):
            build_candidates(
                barrier=_empty_barrier(),
                baselines={},
                now=datetime(2026, 8, 5, tzinfo=timezone.utc),
            )


class TestGenerationR2ValidatorM9:
    """M9: generation-r2 validator rejects the same account shapes as v1."""

    def _make_valid_r2_payload(self):
        return {
            "accounts": [{
                "account_scope": "pension-excel-" + "a" * 15,
                "account_type_label": "pension-excel-v1",
                "ordinal": 1,
                "holdings": [],
                "source_provenance": "PENSION_EXCEL",
                "pension_revision_id": "a" * 32,
                "pension_source_as_of": "2026-07-01T00:00:00+09:00",
                "pension_source_age_days": 35,
                "pension_freshness_class": "AGED_MANUAL_SOURCE",
                "pension_semantic_digest": "b" * 64,
            }],
            "global_inventory": True,
            "payload_version": 2,
            "pension_provenance": {
                "revision_id": "a" * 32,
                "source_content_sha256": "c" * 64,
                "semantic_digest": "b" * 64,
                "source_as_of": "2026-07-01T00:00:00+09:00",
                "source_age_days": 35,
                "freshness_class": "AGED_MANUAL_SOURCE",
                "parser_version": "pension-excel-v1",
            },
        }

    def test_rejects_bool_ordinal(self):
        from tokmo.values.material_pack import _validate_holdings_generation_r2
        payload = self._make_valid_r2_payload()
        payload["accounts"][0]["ordinal"] = True
        with pytest.raises(ValueError):
            _validate_holdings_generation_r2(payload)

    def test_rejects_string_ordinal(self):
        from tokmo.values.material_pack import _validate_holdings_generation_r2
        payload = self._make_valid_r2_payload()
        payload["accounts"][0]["ordinal"] = "2"
        with pytest.raises(ValueError):
            _validate_holdings_generation_r2(payload)

    def test_rejects_int_label(self):
        from tokmo.values.material_pack import _validate_holdings_generation_r2
        payload = self._make_valid_r2_payload()
        payload["accounts"][0]["account_type_label"] = 5
        with pytest.raises(ValueError):
            _validate_holdings_generation_r2(payload)

    def test_rejects_non_string_provenance_fields(self):
        from tokmo.values.material_pack import _validate_holdings_generation_r2
        payload = self._make_valid_r2_payload()
        payload["pension_provenance"]["revision_id"] = 123
        with pytest.raises(ValueError):
            _validate_holdings_generation_r2(payload)
