"""Tests for fundamental materialization program.

Assembles OfficialFundamentalMaterialV1 from source parse results.
Handles date-only available-at normalization via the next-completed-session
policy seam.
"""

from datetime import UTC, datetime
from pathlib import Path

from tokmo.programs.fundamental_materialization import (
    MaterializationResult,
    materialize_sec_fundamentals,
)
from tokmo.values.fundamental_materials import (
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    AvailableAtResolution,
    OfficialFundamentalMaterialV1,
    StatementScope,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sec_companyfacts"


def _load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


_AS_OF = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_MARKET = "US_NASDAQ"
_SCOPE = StatementScope.CONSOLIDATED


def _resolver(filed_raw: str) -> AvailableAtResolution:
    return AvailableAtResolution(
        available_at=datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
        policy_version=DATE_ONLY_POLICY_VERSION,
        session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
    )


class TestMaterializeSecFundamentals:
    def test_produces_current_and_prior_materials(self):
        body = _load_fixture("aapl_minimal.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=UTC)

        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=observed_at,
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert isinstance(result, MaterializationResult)
        assert result.materials is not None
        assert len(result.materials) == 2

        # Check that source_refs are distinct between current/prior.
        refs = {m.source_ref for m in result.materials}
        assert len(refs) == 2

    def test_materials_are_official_fundamental_material(self):
        body = _load_fixture("aapl_minimal.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=UTC)

        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=observed_at,
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        for m in result.materials:
            assert isinstance(m, OfficialFundamentalMaterialV1)

    def test_invalid_body_returns_unavailable(self):
        result = materialize_sec_fundamentals(
            b"invalid json",
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "parse_error"

    def test_source_refs_preserve_raw_body_sha256(self):
        body = _load_fixture("aapl_minimal.json")
        import hashlib

        expected = hashlib.sha256(body).hexdigest()

        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        for m in result.materials:
            assert m.raw_body_sha256 == expected

    def test_materials_are_sorted_by_fiscal_year(self):
        body = _load_fixture("aapl_minimal.json")
        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        fys = [m.fiscal_year for m in result.materials]
        assert fys == sorted(fys)

    def test_market_and_scope_are_passed_through(self):
        """M5: market/scope are verified inputs, not hardcoded."""
        body = _load_fixture("aapl_minimal.json")
        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_resolver,
            market="US_NYSE",
            statement_scope=StatementScope.NON_CONSOLIDATED,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        for m in result.materials:
            assert m.market == "US_NYSE"
            assert m.statement_scope == StatementScope.NON_CONSOLIDATED
