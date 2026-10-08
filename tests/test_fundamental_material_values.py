"""Tests for OfficialFundamentalMaterialV1 immutable value types.

These tests verify the immutable, bounded material types that carry
official fundamental data from SEC/OpenDART sources into the quant core.
"""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tokmo.values.fundamental_materials import (
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    FUNDAMENTAL_SOURCE_DIGEST_PREFIX,
    SEC_FUNDAMENTAL_SOURCE,
    AccountingRegime,
    AvailableAtResolution,
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
    StatementScope,
)


def _evidence(value, concept_id, unit, selection=MetricSelection.EXACT):
    return MetricEvidence(
        value=Decimal(value),
        source_concept_id=concept_id,
        unit=unit,
        selection=selection,
    )


class TestOfficialFundamentalMaterialV1Immutability:
    def test_material_is_frozen(self):
        material = OfficialFundamentalMaterialV1(
            source=SEC_FUNDAMENTAL_SOURCE,
            entity_id="0000320193",
            canonical_symbol="AAPL",
            market="US_NYSE_NASDAQ",
            accounting_regime=AccountingRegime.US_GAAP,
            statement_scope=StatementScope.CONSOLIDATED,
            fiscal_year=2025,
            period_start=date(2024, 9, 29),
            period_end=date(2025, 9, 27),
            filing_id="0000320193-25-000079",
            filed_at_raw="2025-10-31",
            available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
            revision_of=None,
            taxonomy_namespace="us-gaap",
            taxonomy_version="2024",
            mapping_version="v1",
            revenue_ttm=_evidence("416161000000", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            net_income_ttm=_evidence("112010000000", "NetIncomeLoss", "USD"),
            gross_profit_ttm=None,
            total_equity=_evidence("73733000000", "StockholdersEquity", "USD"),
            total_assets=_evidence("359241000000", "Assets", "USD"),
            diluted_shares_outstanding=_evidence("15004697000", "WeightedAverageNumberOfDilutedSharesOutstanding", "shares"),
            source_request_identity_digest="abc123",
            raw_body_sha256="def456",
            observed_at=datetime(2025, 10, 31, 22, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        with pytest.raises(AttributeError):
            material.fiscal_year = 2024  # type: ignore[misc]

    def test_material_with_all_metrics_none_is_valid(self):
        """Missing metrics are allowed — they are typed unavailable at a higher level."""
        material = OfficialFundamentalMaterialV1(
            source=SEC_FUNDAMENTAL_SOURCE,
            entity_id="0000320193",
            canonical_symbol="AAPL",
            market="US_NYSE_NASDAQ",
            accounting_regime=AccountingRegime.US_GAAP,
            statement_scope=StatementScope.CONSOLIDATED,
            fiscal_year=2025,
            period_start=date(2024, 9, 29),
            period_end=date(2025, 9, 27),
            filing_id="0000320193-25-000079",
            filed_at_raw="2025-10-31",
            available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
            revision_of=None,
            taxonomy_namespace="us-gaap",
            taxonomy_version="2024",
            mapping_version="v1",
            revenue_ttm=None,
            net_income_ttm=None,
            gross_profit_ttm=None,
            total_equity=None,
            total_assets=None,
            diluted_shares_outstanding=None,
            source_request_identity_digest="abc123",
            raw_body_sha256="def456",
            observed_at=datetime(2025, 10, 31, 22, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert material.revenue_ttm is None
        assert material.source_ref.startswith(FUNDAMENTAL_SOURCE_DIGEST_PREFIX)


class TestSourceRefDigest:
    def test_source_ref_starts_with_prefix(self):
        material = OfficialFundamentalMaterialV1(
            source=SEC_FUNDAMENTAL_SOURCE,
            entity_id="0000320193",
            canonical_symbol="AAPL",
            market="US_NYSE_NASDAQ",
            accounting_regime=AccountingRegime.US_GAAP,
            statement_scope=StatementScope.CONSOLIDATED,
            fiscal_year=2025,
            period_start=date(2024, 9, 29),
            period_end=date(2025, 9, 27),
            filing_id="0000320193-25-000079",
            filed_at_raw="2025-10-31",
            available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
            revision_of=None,
            taxonomy_namespace="us-gaap",
            taxonomy_version="2024",
            mapping_version="v1",
            revenue_ttm=_evidence("416161000000", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            net_income_ttm=None,
            gross_profit_ttm=None,
            total_equity=None,
            total_assets=None,
            diluted_shares_outstanding=None,
            source_request_identity_digest="abc123",
            raw_body_sha256="def456",
            observed_at=datetime(2025, 10, 31, 22, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert material.source_ref.startswith("qfundv1:sha256:")
        assert len(material.source_ref) < 80

    def test_source_ref_is_deterministic(self):
        """Same inputs → same source_ref."""
        kwargs = dict(
            source=SEC_FUNDAMENTAL_SOURCE,
            entity_id="0000320193",
            canonical_symbol="AAPL",
            market="US_NYSE_NASDAQ",
            accounting_regime=AccountingRegime.US_GAAP,
            statement_scope=StatementScope.CONSOLIDATED,
            fiscal_year=2025,
            period_start=date(2024, 9, 29),
            period_end=date(2025, 9, 27),
            filing_id="0000320193-25-000079",
            filed_at_raw="2025-10-31",
            available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
            revision_of=None,
            taxonomy_namespace="us-gaap",
            taxonomy_version="2024",
            mapping_version="v1",
            revenue_ttm=_evidence("416161000000", "RevenueFromContractWithCustomerExcludingAssessedTax", "USD"),
            net_income_ttm=None,
            gross_profit_ttm=None,
            total_equity=None,
            total_assets=None,
            diluted_shares_outstanding=None,
            source_request_identity_digest="abc123",
            raw_body_sha256="def456",
            observed_at=datetime(2025, 10, 31, 22, 0, tzinfo=timezone.utc),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc),
        )
        m1 = OfficialFundamentalMaterialV1(**kwargs)
        m2 = OfficialFundamentalMaterialV1(**kwargs)
        assert m1.source_ref == m2.source_ref


class TestDateOnlyPolicyConstant:
    def test_policy_version_is_exact(self):
        assert DATE_ONLY_POLICY_VERSION == "official-date-only-next-completed-session-v1"


class TestMetricEvidence:
    def test_metric_evidence_is_frozen(self):
        ev = MetricEvidence(
            value=Decimal("100"),
            source_concept_id="Revenues",
            unit="USD",
            selection=MetricSelection.EXACT,
        )
        with pytest.raises(AttributeError):
            ev.value = Decimal("200")  # type: ignore[misc]

    def test_ambiguous_selection(self):
        ev = MetricEvidence(
            value=None,
            source_concept_id=None,
            unit="USD",
            selection=MetricSelection.AMBIGUOUS,
        )
        assert ev.selection == MetricSelection.AMBIGUOUS
        assert ev.value is None
