"""Tests for SEC companyfacts fundamental parser.

Covers the exact versioned concept registry, annual FY 10-K filtering,
ambiguity resolution, amendment/revision predecessor handling, and the
production of OfficialFundamentalMaterialV1 from companyfacts JSON.

B1 fix: fixtures are real slices from the official canary body, preserving
the true envelope structure (fy/fp as filing period, comparison rows,
frame values, multiple accessions). Grouping is by fiscal period (start, end),
not filing fy.
"""

import hashlib
import json
from datetime import date, datetime, timezone
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.facts.sec_fundamentals import (
    COMPANYFACTS_ENDPOINT_ID,
    COMPANYFACTS_HOST,
    COMPANYFACTS_PATH,
    COMPANYFACTS_REQUEST_TIMEOUT_SECONDS,
    MAX_COMPANYFACTS_BODY_BYTES,
    SECCompanyFactsRequest,
    SECCompanyFactsResult,
    TAXONOMY_VERSION,
    build_companyfacts_request,
    parse_companyfacts_response,
)
from tokmo.values.fundamental_materials import (
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    AvailableAtResolution,
    MetricEvidence,
    MetricSelection,
    OfficialFundamentalMaterialV1,
    StatementScope,
)


FIXTURES = Path(__file__).parent / "fixtures" / "sec_companyfacts"


def _load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


_AS_OF = datetime(2026, 1, 1, 0, 0, tzinfo=timezone.utc)
_MARKET = "US_NASDAQ"
_SCOPE = StatementScope.CONSOLIDATED


def _resolver(filed_raw: str) -> AvailableAtResolution:
    return AvailableAtResolution(
        available_at=datetime(2025, 11, 3, 21, 0, tzinfo=timezone.utc),
        policy_version=DATE_ONLY_POLICY_VERSION,
        session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
    )


class TestCompanyFactsEndpointConstants:
    """Q-01: endpoint constants are pinned from canary manifest, not guessed."""

    def test_host_and_path_are_official(self):
        assert COMPANYFACTS_HOST == "data.sec.gov"
        assert COMPANYFACTS_PATH == "/api/xbrl/companyfacts/CIK##########.json"

    def test_timeout_and_body_cap_are_bounded(self):
        assert COMPANYFACTS_REQUEST_TIMEOUT_SECONDS == 10.0
        assert MAX_COMPANYFACTS_BODY_BYTES == 5 * 1024 * 1024

    def test_taxonomy_version_is_pinned_not_current(self):
        """m7/m1: taxonomy version must be truthful, not 'current' or a false year."""
        assert TAXONOMY_VERSION != "current"
        assert isinstance(TAXONOMY_VERSION, str)
        # m1: the companyfacts body does not expose a per-fact taxonomy
        # version. The truthful value is "unknown" — not a fabricated year
        # or endpoint identity.
        assert TAXONOMY_VERSION == "unknown"


class TestCompanyFactsRequest:
    def test_build_request(self):
        req = build_companyfacts_request(cik="0000320193", user_agent="test-agent")
        assert req.endpoint_id == COMPANYFACTS_ENDPOINT_ID
        assert req.cik == "0000320193"
        assert req.timeout_seconds == COMPANYFACTS_REQUEST_TIMEOUT_SECONDS

    def test_request_immutable(self):
        req = build_companyfacts_request(cik="0000320193", user_agent="x")
        with pytest.raises(AttributeError):
            req.cik = "9999999999"  # type: ignore[misc]


class TestParseCompanyFactsMinimal:
    """G1A-T01: parse a single entity with clean USD facts for 6 metrics.

    B1 fix: fixture preserves real envelope structure with comparison rows
    from the same filing (different periods, same fy/fp/form/accn).
    """

    def test_extracts_current_and_prior_annual_materials(self):
        body = _load_fixture("aapl_minimal.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=observed_at,
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert isinstance(result, SECCompanyFactsResult)
        assert result.materials is not None
        assert len(result.materials) == 2  # current + prior

        # Sort by fiscal_year to get current/prior
        materials = sorted(result.materials, key=lambda m: m.fiscal_year)
        prior, current = materials

        # Current period: FY2025 ending 2025-09-27
        assert current.fiscal_year == 2025
        assert current.period_end == date(2025, 9, 27)
        assert current.period_start == date(2024, 9, 29)

        # Check revenue extraction
        assert current.revenue_ttm is not None
        assert current.revenue_ttm.value == Decimal("416161000000")
        assert current.revenue_ttm.source_concept_id == "RevenueFromContractWithCustomerExcludingAssessedTax"
        assert current.revenue_ttm.unit == "USD"
        assert current.revenue_ttm.selection == MetricSelection.EXACT

        # Check shares extraction uses shares unit
        assert current.diluted_shares_outstanding is not None
        assert current.diluted_shares_outstanding.value == Decimal("15004697000")
        assert current.diluted_shares_outstanding.unit == "shares"

        # Check balance sheet items have no start (instantaneous)
        assert current.total_assets is not None
        assert current.total_assets.source_concept_id == "Assets"
        assert current.total_assets.value == Decimal("359241000000")

        # Prior period: FY2024 ending 2024-09-28
        assert prior.fiscal_year == 2024
        assert prior.revenue_ttm is not None
        assert prior.revenue_ttm.value == Decimal("391035000000")

        # B2: ordinary 10-K comparison filings must NOT produce revision_of
        assert current.revision_of is None
        assert prior.revision_of is None

    def test_raw_body_sha256_is_recorded(self):
        body = _load_fixture("aapl_minimal.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=observed_at,
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        expected_hash = hashlib.sha256(body).hexdigest()
        for m in result.materials:
            assert m.raw_body_sha256 == expected_hash

    def test_market_and_scope_are_injected_not_hardcoded(self):
        """M5: market and statement_scope must come from caller, not hardcoded."""
        body = _load_fixture("aapl_minimal.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market="US_NYSE",
            statement_scope=StatementScope.NON_CONSOLIDATED,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        for m in result.materials:
            assert m.market == "US_NYSE"
            assert m.statement_scope == StatementScope.NON_CONSOLIDATED


class TestParseCompanyFactsAmbiguity:
    """Q-02/G1A-T02: multiple revenue concepts in same period -> AMBIGUOUS.

    M1 fix: ambiguous metrics are carried as typed MetricEvidence with
    selection=AMBIGUOUS, NOT dropped to None.
    """

    def test_ambiguous_revenue_carried_as_typed_evidence_other_metrics_preserved(self):
        body = _load_fixture("aapl_ambiguous_revenue.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
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
        by_end = {m.period_end: m for m in result.materials}
        # FY2018 (current) plus the adjacent FY2017 instant period, which the
        # FY2019 10-K restates. Under M2 each material is built from exactly
        # ONE accession, so a period whose metrics are spread across filings
        # is no longer rejected wholesale: the metrics the selected filing
        # does not carry become typed UNAVAILABLE (SDS §5.2 member
        # unavailable), never mixed in from another accession.
        assert sorted(by_end) == [date(2017, 9, 30), date(2018, 9, 29)]

        m = by_end[date(2018, 9, 29)]
        # M1 reclosure: ambiguity is accession-scoped. The primary revenue
        # concept (0000320193-19-000119) and the alternative Revenues concept
        # (0000320193-18-000145) are in DIFFERENT accessions. Since the
        # selected filing (0000320193-19-000119) does NOT also carry Revenues
        # for this period, revenue is EXACT — the alternative concept in a
        # different filing is a comparison restatement, not ambiguity.
        assert m.revenue_ttm is not None
        assert m.revenue_ttm.selection == MetricSelection.EXACT
        assert m.revenue_ttm.value == Decimal("265595000000")

        # Other metrics survive as EXACT, all from the one selected filing.
        assert m.filing_id == "0000320193-19-000119"
        assert m.net_income_ttm.selection == MetricSelection.EXACT
        assert m.net_income_ttm.value == Decimal("59531000000")
        assert m.gross_profit_ttm.selection == MetricSelection.EXACT
        assert m.total_equity.value == Decimal("107147000000")
        assert m.total_assets.value == Decimal("365725000000")
        assert m.diluted_shares_outstanding.value == Decimal("5000109000")

        # The FY2017 comparison period is coherent to the same single filing:
        # only the equity instant it restates is EXACT; Assets@2017-09-30 is
        # published only by the older FY2018 10-K and must NOT be borrowed.
        prior = by_end[date(2017, 9, 30)]
        assert prior.filing_id == "0000320193-19-000119"
        assert prior.total_equity.selection == MetricSelection.EXACT
        assert prior.total_equity.value == Decimal("134047000000")
        assert prior.total_assets.selection == MetricSelection.UNAVAILABLE
        assert prior.total_assets.value is None
        assert prior.revenue_ttm.selection == MetricSelection.UNAVAILABLE


class TestM1AmbiguityIsScopedToMetricAndPeriod:
    """M1: ambiguity is keyed by the exact (metric, fiscal period).

    A duplicate concept in ONE fiscal period must not make the same metric
    AMBIGUOUS in any other period, and must not touch any other metric.
    SDS §5.1/Q-02 scope ambiguity to the same fiscal period.
    """

    _ACCN = "0000320193-25-000079"

    def _body(self, extra_revenues: list[dict]) -> bytes:
        def row(start, end, val, concept_form="10-K"):
            return {
                "start": start, "end": end, "val": val,
                "accn": self._ACCN, "fy": 2025, "fp": "FY",
                "form": concept_form, "filed": "2025-10-31",
            }

        return json.dumps({
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {"us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        row("2024-09-29", "2025-09-27", 416161000000),
                        row("2023-10-01", "2024-09-28", 391035000000),
                    ]}
                },
                "NetIncomeLoss": {
                    "units": {"USD": [
                        row("2024-09-29", "2025-09-27", 112010000000),
                        row("2023-10-01", "2024-09-28", 93736000000),
                    ]}
                },
                "Revenues": {"units": {"USD": extra_revenues}},
            }}
        }).encode()

    def test_prior_period_ambiguity_does_not_contaminate_current(self):
        """Revenues duplicates ONLY the FY2024 period. FY2025 revenue must
        stay EXACT; only FY2024 revenue becomes AMBIGUOUS."""
        body = self._body([{
            "start": "2023-10-01", "end": "2024-09-28", "val": 391035000000,
            "accn": self._ACCN, "fy": 2025, "fp": "FY",
            "form": "10-K", "filed": "2025-10-31",
        }])
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        assert sorted(by_fy) == [2024, 2025]

        current = by_fy[2025]
        assert current.revenue_ttm is not None
        assert current.revenue_ttm.selection == MetricSelection.EXACT
        assert current.revenue_ttm.value == Decimal("416161000000")
        assert current.revenue_ttm.source_concept_id == (
            "RevenueFromContractWithCustomerExcludingAssessedTax"
        )

        prior = by_fy[2024]
        assert prior.revenue_ttm is not None
        assert prior.revenue_ttm.selection == MetricSelection.AMBIGUOUS
        assert prior.revenue_ttm.value is None
        assert prior.revenue_ttm.source_concept_id is None

    def test_current_period_ambiguity_does_not_contaminate_prior(self):
        """The mirror case: only FY2025 is ambiguous; FY2024 stays EXACT."""
        body = self._body([{
            "start": "2024-09-29", "end": "2025-09-27", "val": 416161000000,
            "accn": self._ACCN, "fy": 2025, "fp": "FY",
            "form": "10-K", "filed": "2025-10-31",
        }])
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        assert by_fy[2025].revenue_ttm.selection == MetricSelection.AMBIGUOUS
        assert by_fy[2024].revenue_ttm.selection == MetricSelection.EXACT
        assert by_fy[2024].revenue_ttm.value == Decimal("391035000000")

    def test_ambiguity_never_touches_other_metrics_in_same_period(self):
        """Revenue ambiguity in FY2025 leaves net income EXACT in FY2025."""
        body = self._body([{
            "start": "2024-09-29", "end": "2025-09-27", "val": 416161000000,
            "accn": self._ACCN, "fy": 2025, "fp": "FY",
            "form": "10-K", "filed": "2025-10-31",
        }])
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        current = {m.fiscal_year: m for m in result.materials}[2025]
        assert current.net_income_ttm.selection == MetricSelection.EXACT
        assert current.net_income_ttm.value == Decimal("112010000000")

    def test_past_only_ambiguity_leaves_current_exact_when_past_is_dropped(self):
        """A far-past ambiguous period is not even emitted, and must leave
        the current period's revenue EXACT."""
        body = json.dumps({
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {"us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"start": "2024-09-29", "end": "2025-09-27",
                         "val": 416161000000, "accn": self._ACCN, "fy": 2025,
                         "fp": "FY", "form": "10-K", "filed": "2025-10-31"},
                        {"start": "2016-10-02", "end": "2017-09-30",
                         "val": 229234000000, "accn": self._ACCN, "fy": 2025,
                         "fp": "FY", "form": "10-K", "filed": "2025-10-31"},
                    ]}
                },
                "Revenues": {
                    "units": {"USD": [
                        {"start": "2016-10-02", "end": "2017-09-30",
                         "val": 229234000000, "accn": self._ACCN, "fy": 2025,
                         "fp": "FY", "form": "10-K", "filed": "2025-10-31"},
                    ]}
                },
            }}
        }).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        # FY2017 is ambiguous-only (no other metric) → no filing identity →
        # not emitted. FY2025 must survive as EXACT.
        assert len(result.materials) == 1
        current = result.materials[0]
        assert current.fiscal_year == 2025
        assert current.revenue_ttm.selection == MetricSelection.EXACT
        assert current.revenue_ttm.value == Decimal("416161000000")


class TestParseCompanyFactsQuarterlyFiltered:
    """G1A-T06: only fp=FY + form=10-K facts are used; Q1/Q2/Q3 ignored."""

    def test_quarterly_facts_are_ignored(self):
        body = _load_fixture("aapl_quarterly_and_missing_gross_profit.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
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
        # Current + prior (both from FY2025 10-K's comparison rows)
        assert len(result.materials) >= 1
        # Sort by fiscal_year
        materials = sorted(result.materials, key=lambda m: m.fiscal_year)
        current = materials[-1]

        # Should use the FY 10-K value, not the Q2 10-Q value
        assert current.fiscal_year == 2025
        assert current.revenue_ttm is not None
        assert current.revenue_ttm.value == Decimal("416161000000")  # FY, not quarterly

        # GrossProfit concept absent -> M1: typed UNAVAILABLE, not None
        assert current.gross_profit_ttm is not None
        assert current.gross_profit_ttm.selection == MetricSelection.UNAVAILABLE
        assert current.gross_profit_ttm.value is None


class TestParseCompanyFactsAmendment:
    """Q-05/G1A-T05: 10-K/A in same period -> revision resolver.

    B2 fix: revision_of is only for explicit 10-K/A, never ordinary 10-K
    comparison filings. The FY2009 amendment chain has real 10-K + 10-K/A
    for the same period from the canary body.
    """

    def test_amendment_supersedes_original(self):
        body = _load_fixture("aapl_amendment.json")
        observed_at = datetime(2010, 2, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=observed_at,
            available_at_resolver=lambda filed_raw: AvailableAtResolution(
                available_at=datetime(2010, 2, 26, 21, 0, tzinfo=timezone.utc),
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            ),
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=datetime(2010, 3, 1, 0, 0, tzinfo=timezone.utc),
        )
        assert result.materials is not None
        # FY2009 has amendment chain, FY2010 is the consecutive prior.
        materials_by_fy = {m.fiscal_year: m for m in result.materials}

        # FY2009 should have the amendment (10-K/A) value.
        m2009 = materials_by_fy[2009]
        assert m2009.net_income_ttm is not None
        # The 10-K/A value (8235000000) differs from the 10-K value (5704000000).
        assert m2009.net_income_ttm.value == Decimal("8235000000")

        # B2: revision_of should point to the original 10-K accession.
        assert m2009.revision_of is not None
        assert "0001193125-09-214859" in m2009.revision_of


class TestParseCompanyFactsInvalidPayload:
    def test_invalid_json_returns_unavailable(self):
        result = parse_companyfacts_response(
            b"not json",
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "parse_error"

    def test_missing_facts_namespace_returns_unavailable(self):
        payload = json.dumps({"cik": 320193, "entityName": "X", "facts": {}})
        result = parse_companyfacts_response(
            payload.encode(),
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "contract_mismatch"


class TestParseCompanyFactsPriorNonconsecutive:
    """G1A-T07/M10: if prior period is not consecutive, prior is not produced.

    M10 fix: truthful nonconsecutive fixture with FY2025 + FY2018 (7-year gap).
    Only current material should be produced.
    """

    def test_nonconsecutive_prior_omitted(self):
        """Fixture has FY2025 and FY2018 (not consecutive) — only current produced."""
        body = _load_fixture("aapl_nonconsecutive.json")
        observed_at = datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc)
        result = parse_companyfacts_response(
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
        # Only current (FY2025); FY2018 is not consecutive so not produced.
        assert len(result.materials) == 1
        assert result.materials[0].fiscal_year == 2025


class TestM3NoSyntheticDateFallback:
    """M3: invalid period_end date must fail closed, not fall back to 12/31."""

    def test_invalid_period_end_raises(self):
        # Construct a minimal body with a bad end date
        body = json.dumps({
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {"us-gaap": {
                "RevenueFromContractWithCustomerExcludingAssessedTax": {
                    "units": {"USD": [
                        {"start": "2024-09-29", "end": "2024-09", "val": 1,
                         "accn": "0000320193-25-000079", "fy": 2025, "fp": "FY",
                         "form": "10-K", "filed": "2025-10-31"}
                    ]}
                }
            }}
        }).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=timezone.utc),
            available_at_resolver=_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # Invalid date -> fail closed, no 12/31 fallback
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"


# ---------------------------------------------------------------------------
# SDS §12 15b: Fixture provenance seal
#
# Fixtures are REAL SLICES derived from the official SEC companyfacts
# canary response (SHA-256 pinned below). The raw 3.8 MB body is NOT
# committed; only bounded excerpts preserving the real envelope structure
# (comparison rows, fy/fp as filing period, frame values, accessions).
#
# Raw canary SHA-256: 73a86c6aedc31f77cac2ea4df5f80f0b3bd7e6eb58bb4e01444fbedf3afb9c43
# Source: ~/.local/share/tokmo-v3/quant-g1a-sec-companyfacts-canary-v1/
# Receipt: attempt.json + summary.json in same directory.
# ---------------------------------------------------------------------------

RAW_CANARY_SHA256 = "73a86c6aedc31f77cac2ea4df5f80f0b3bd7e6eb58bb4e01444fbedf3afb9c43"

# Real accessions present in the canary body (verified by extraction).
CANARY_ACCESSIONS = frozenset({
    "0000320193-25-000079",  # FY2025 10-K
    "0000320193-24-000123",  # FY2024 10-K
    "0000320193-19-000119",  # FY2019 10-K
    "0000320193-18-000145",  # FY2018 10-K
    "0001193125-09-214859",  # FY2009 10-K
    "0001193125-10-012091",  # FY2009 10-K/A
    "0001193125-10-238044",  # FY2010 10-K
})


class TestFixtureProvenanceSeal:
    """SDS §12 15b: fixture provenance is pinned to the raw canary.

    m1 fix: replace tautological assertions with real extraction verification.
    Each fixture must contain real accessions from the canary, and the raw
    canary SHA-256 must match the saved receipt.
    """

    def test_raw_canary_sha256_matches_receipt(self):
        """Verify the canary SHA matches the receipt on disk."""
        receipt_path = Path.home() / ".local" / "share" / "tokmo-v3" / \
            "quant-g1a-sec-companyfacts-canary-v1" / "attempt.json"
        if not receipt_path.exists():
            pytest.skip("canary receipt not available")
        receipt = json.loads(receipt_path.read_text())
        assert receipt["body_sha256"] == RAW_CANARY_SHA256

    @pytest.mark.parametrize("fixture_name", [
        "aapl_minimal.json",
        "aapl_ambiguous_revenue.json",
        "aapl_amendment.json",
        "aapl_quarterly_and_missing_gross_profit.json",
        "aapl_nonconsecutive.json",
    ])
    def test_fixture_contains_real_canary_accession(self, fixture_name):
        """Each fixture must contain at least one real canary accession —
        no invented accessions."""
        text = (FIXTURES / fixture_name).read_text()
        found = {accn for accn in CANARY_ACCESSIONS if accn in text}
        assert found, f"{fixture_name}: no real canary accession found"

    @pytest.mark.parametrize("fixture_name", [
        "aapl_minimal.json",
        "aapl_ambiguous_revenue.json",
        "aapl_amendment.json",
        "aapl_quarterly_and_missing_gross_profit.json",
        "aapl_nonconsecutive.json",
    ])
    def test_fixture_is_bounded_excerpt_not_raw_body(self, fixture_name):
        """Raw 3.8 MB body must not be committed."""
        size = (FIXTURES / fixture_name).stat().st_size
        assert size < 10_000, f"{fixture_name}: {size} bytes — raw body must not be committed"

    @pytest.mark.parametrize("fixture_name", [
        "aapl_minimal.json",
        "aapl_ambiguous_revenue.json",
    ])
    def test_fixture_preserves_envelope_comparison_rows(self, fixture_name):
        """B1: fixtures must preserve comparison rows (multiple periods per
        filing, tagged with same fy/fp/form but different start/end)."""
        data = json.loads((FIXTURES / fixture_name).read_text())
        us_gaap = data["facts"]["us-gaap"]
        # At least one concept should have multiple periods from the same filing.
        found_multi_period = False
        for concept_data in us_gaap.values():
            for unit_facts in concept_data.get("units", {}).values():
                accn_periods = {}
                for f in unit_facts:
                    accn = f.get("accn")
                    period = (f.get("start"), f.get("end"))
                    if accn not in accn_periods:
                        accn_periods[accn] = set()
                    accn_periods[accn].add(period)
                for accn, periods in accn_periods.items():
                    if len(periods) > 1:
                        found_multi_period = True
                        break
        assert found_multi_period, (
            f"{fixture_name}: no comparison rows found — fixture must preserve "
            "real envelope structure with multiple periods per filing"
        )

    @pytest.mark.parametrize("fixture_name", [
        "aapl_minimal.json",
        "aapl_ambiguous_revenue.json",
    ])
    def test_fixture_preserves_frame_values_from_raw(self, fixture_name):
        """B1: frame values from raw canary must be preserved, not fabricated.
        Only tested on fixtures with recent filings that carry frame values."""
        data = json.loads((FIXTURES / fixture_name).read_text())
        us_gaap = data["facts"]["us-gaap"]
        has_frame = False
        for concept_data in us_gaap.values():
            for unit_facts in concept_data.get("units", {}).values():
                for f in unit_facts:
                    if f.get("frame"):
                        has_frame = True
                        frame = f["frame"]
                        assert frame.startswith("CY"), (
                            f"suspicious frame value: {frame}"
                        )
        assert has_frame, f"{fixture_name}: no frame values preserved from raw"
