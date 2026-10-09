"""Slice 5 — Delta/admission/payload tests (B3, M5, M11).

M11: behavioral tests for freshness classification, reminder text privacy,
scope identity, and the pension materialization entrypoint.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from tokmo.programs.pension_materialization import (
    materialize_pension_holdings,
    reminder_text,
)
from tokmo.values.current_holdings import HoldingItemV1
from tokmo.values.pension_holdings import (
    PensionFreshnessV1,
    PensionHoldingsRevisionV1,
    classify_pension_freshness,
    pension_account_scope,
)

_KST = ZoneInfo("Asia/Seoul")


class TestFreshnessClassification:
    def test_current_manual_source(self):
        as_of = datetime(2026, 7, 15, tzinfo=_KST)
        now = datetime(2026, 8, 5, tzinfo=_KST)
        freshness = classify_pension_freshness(as_of, now)
        assert freshness.freshness_class == "CURRENT_MANUAL_SOURCE"
        assert freshness.source_age_days == 21

    def test_aged_manual_source(self):
        as_of = datetime(2026, 6, 1, tzinfo=_KST)
        now = datetime(2026, 8, 5, tzinfo=_KST)
        freshness = classify_pension_freshness(as_of, now)
        assert freshness.freshness_class == "AGED_MANUAL_SOURCE"
        assert freshness.source_age_days >= 31

    def test_kst_date_only_boundary(self):
        """Date-only boundary: KST midnight minus KST midnight."""
        as_of = datetime(2026, 7, 5, tzinfo=_KST)
        now = datetime(2026, 8, 5, tzinfo=_KST)
        freshness = classify_pension_freshness(as_of, now)
        assert freshness.source_age_days == 31
        assert freshness.freshness_class == "AGED_MANUAL_SOURCE"


class TestReminderTextPrivacy:
    """M8: reminder text contains only age/as-of/update request."""

    def test_text_has_no_holdings_identity_values(self):
        freshness = PensionFreshnessV1(
            revision_id="a" * 32,
            source_as_of=datetime(2026, 7, 1, tzinfo=_KST),
            source_age_days=35,
            freshness_class="AGED_MANUAL_SOURCE",
        )
        text = reminder_text(freshness)
        assert "35" in text
        assert "2026-07-01" in text
        assert "069500" not in text  # no symbols
        assert "0167A0" not in text
        assert "100" not in text  # no quantities
        assert "pension_landing" not in text  # no paths
        assert "toss-acct" not in text  # no account identity
        assert "pension-excel" not in text  # no pension scope


class TestPensionScopeIdentity:
    """B3: revision-independent scope identity."""

    def test_scope_is_stable(self):
        scope1 = pension_account_scope()
        scope2 = pension_account_scope()
        assert scope1 == scope2  # revision-independent


class TestMaterializePensionHoldingsM11:
    """M11: behavioral test that materialize_pension_holdings converts
    a PensionHoldingsRevisionV1 to composite-compatible account data."""

    def test_materialize_produces_correct_account_data(self):
        holdings = (
            HoldingItemV1(
                symbol="069500",
                quantity="10",
                avg_price="45000",
                currency="KRW",
                market_raw="KR",
                market_field="market",
            ),
        )
        revision = PensionHoldingsRevisionV1(
            revision_id="a" * 32,
            schema_version=1,
            source_content_sha256="b" * 64,
            parser_version="pension-excel-v1",
            sheet_schema_version=1,
            holdings=holdings,
            source_as_of=datetime(2026, 7, 1, tzinfo=_KST),
            uploaded_at=datetime(2026, 7, 1, tzinfo=_KST),
            validated_at=datetime(2026, 7, 1, tzinfo=_KST),
            semantic_digest="c" * 64,
            reconciliation_digest="d" * 64,
            holding_count=1,
            liquidation_flag=0,
        )
        result = materialize_pension_holdings(revision, pension_ordinal=1)
        assert result["account_scope"] == pension_account_scope()
        assert result["account_type_label"] == "pension-excel-v1"
        assert result["ordinal"] == 1
        assert result["source_provenance"] == "PENSION_EXCEL"
        assert len(result["holdings"]) == 1
        assert result["holdings"][0]["symbol"] == "069500"

    def test_materialize_empty_holdings(self):
        """LIQUIDATED revision produces empty holdings list."""
        revision = PensionHoldingsRevisionV1(
            revision_id="a" * 32,
            schema_version=1,
            source_content_sha256="b" * 64,
            parser_version="pension-excel-v1",
            sheet_schema_version=1,
            holdings=(),
            source_as_of=datetime(2026, 7, 1, tzinfo=_KST),
            uploaded_at=datetime(2026, 7, 1, tzinfo=_KST),
            validated_at=datetime(2026, 7, 1, tzinfo=_KST),
            semantic_digest="c" * 64,
            reconciliation_digest="d" * 64,
            holding_count=0,
            liquidation_flag=1,
        )
        result = materialize_pension_holdings(revision, pension_ordinal=1)
        assert result["holdings"] == []
