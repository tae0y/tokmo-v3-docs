"""Counterexample tests for G1A contract gap repair (C1-C4).

These tests verify that the four contract gaps identified by Hermes are
sealed:

C1: consecutive prior cannot use 180..550 days. Require exact adjacent
    fiscal-year identity with bounded real fiscal-year length (363..372
    days). Reject 180-day (semiannual) and 550-day (~18-month) gaps.

C2: malformed/nonempty period_start must fail closed, never except
    ValueError: pass to None. Conflicting start dates across metrics for
    the same period must also fail.

C3: DATE_ONLY_POLICY_VERSION cannot be stamped merely because an arbitrary
    callable returned a datetime. The resolver must produce a typed
    AvailableAtResolution carrying policy_version and session_history_source.

C4: OpenDART endpoint architecture test dead AST loop/pass removed.
"""

import json
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tokmo.facts.sec_fundamentals import (
    _MAX_FISCAL_YEAR_DAYS,
    _MIN_FISCAL_YEAR_DAYS,
    parse_companyfacts_response,
)
from tokmo.programs.fundamental_materialization import (
    materialize_sec_fundamentals,
)
from tokmo.values.fundamental_materials import (
    DATE_ONLY_POLICY_VERSION,
    DATE_ONLY_SESSION_HISTORY_SOURCE,
    AvailableAtResolution,
    MetricSelection,
    StatementScope,
)

FIXTURES = Path(__file__).parent / "fixtures" / "sec_companyfacts"

_AS_OF = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
_MARKET = "US_NASDAQ"
_SCOPE = StatementScope.CONSOLIDATED


def _typed_resolver(filed_raw: str) -> AvailableAtResolution:
    """Typed fake implementing the exact resolver contract."""
    return AvailableAtResolution(
        available_at=datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
        policy_version=DATE_ONLY_POLICY_VERSION,
        session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
    )


def _load_fixture(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


# ---------------------------------------------------------------------------
# C1: consecutive prior fiscal-year identity — bounded real fiscal-year length
# ---------------------------------------------------------------------------


class TestC1ConsecutivePriorFiscalYearIdentity:
    """C1: reject 180..550 day window; require 363..372 day adjacency."""

    def test_fiscal_year_day_bounds_are_bounded(self):
        """The bounds must be 363..372, not 180..550."""
        assert _MIN_FISCAL_YEAR_DAYS == 363
        assert _MAX_FISCAL_YEAR_DAYS == 372

    def test_reject_semiannual_gap(self):
        """A 182-day gap (semiannual) must NOT produce a prior material."""
        body = _load_fixture("aapl_semiannual_gap.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        # Only current; semiannual prior must NOT be produced.
        assert len(result.materials) == 1
        assert result.materials[0].period_end == date(2025, 9, 27)

    def test_reject_18month_gap(self):
        """A 549-day gap (~18-month) must NOT produce a prior material."""
        body = _load_fixture("aapl_18month_gap.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        assert len(result.materials) == 1
        assert result.materials[0].period_end == date(2025, 9, 27)

    def test_accept_53week_consecutive_gap(self):
        """A 371-day gap (53-week fiscal year) MUST produce a prior material."""
        body = _load_fixture("aapl_53week_consecutive.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        # Both current and prior should be produced (371-day gap is within 363..372).
        assert len(result.materials) == 2

    def test_7year_gap_still_rejected(self):
        """The existing 7-year nonconsecutive fixture must still be rejected."""
        body = _load_fixture("aapl_nonconsecutive.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        assert len(result.materials) == 1
        assert result.materials[0].fiscal_year == 2025


# ---------------------------------------------------------------------------
# C2: malformed/conflicting period_start must fail closed
# ---------------------------------------------------------------------------


class TestC2MalformedPeriodStartFailClosed:
    """C2: malformed period_start must fail closed, never except: pass to None."""

    def test_malformed_period_start_fails_closed(self):
        """A nonempty period_start that can't parse must reject the material."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "accn": "0000320193-25-000079",
                                        "filed": "2025-10-31",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # Malformed start → fail closed → no_published_data
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_conflicting_period_start_fails_closed(self):
        """Two metrics with different start dates for the same period must fail."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "accn": "0000320193-25-000079",
                                        "filed": "2025-10-31",
                                    }
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-28",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "accn": "0000320193-25-000079",
                                        "filed": "2025-10-31",
                                    }
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # Conflicting starts → fail closed
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_instant_only_metrics_without_start_are_valid(self):
        """Instant-only metrics (no start) should not cause failure."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "end": "2025-09-27",
                                        "val": 359241000000,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "accn": "0000320193-25-000079",
                                        "filed": "2025-10-31",
                                    }
                                ]
                            }
                        }
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # Instant-only metrics with no start are valid.
        assert result.materials is not None
        assert len(result.materials) == 1
        assert result.materials[0].period_start is None


# ---------------------------------------------------------------------------
# C3: typed AvailableAtResolution contract — reject arbitrary callable
# ---------------------------------------------------------------------------


class TestC3TypedResolverContract:
    """C3: DATE_ONLY_POLICY_VERSION cannot be stamped by arbitrary callable."""

    def test_arbitrary_lambda_returning_datetime_is_rejected(self):
        """A plain lambda returning datetime must fail — not a typed resolution."""
        body = _load_fixture("aapl_minimal.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=lambda filed_raw: datetime(
                2025, 11, 3, 21, 0, tzinfo=UTC
            ),
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # Plain lambda returns datetime, not AvailableAtResolution → fail closed.
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_typed_resolver_produces_materials(self):
        """A typed AvailableAtResolution resolver produces materials correctly."""
        body = _load_fixture("aapl_minimal.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        assert len(result.materials) == 2

    def test_available_at_resolution_carries_session_history_source(self):
        """The material must carry the session_history_source from the resolver."""
        body = _load_fixture("aapl_minimal.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        for m in result.materials:
            assert (
                m.available_at_session_history_source
                == DATE_ONLY_SESSION_HISTORY_SOURCE
            )

    def test_wrong_policy_version_rejected_by_value_type(self):
        """AvailableAtResolution with wrong policy_version raises at construction."""
        with pytest.raises(ValueError, match="policy_version_mismatch"):
            AvailableAtResolution(
                available_at=datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
                policy_version="wrong-version",
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

    @pytest.mark.parametrize("spoofed_source", ["", "source-a", "source-b"])
    def test_noncanonical_session_history_source_rejected(self, spoofed_source):
        """Only the reviewed canonical session-history source may claim the policy."""
        with pytest.raises(ValueError, match="session_history_source_mismatch"):
            AvailableAtResolution(
                available_at=datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=spoofed_source,
            )

    def test_naive_available_at_rejected(self):
        """Session-close evidence must be an aware datetime."""
        with pytest.raises(ValueError, match="requires_aware_datetime"):
            AvailableAtResolution(
                available_at=datetime(2025, 11, 3, 21, 0),
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

    def test_available_at_before_as_of_enforced(self):
        """available_at > as_of must be rejected even with typed resolver."""

        def future_resolver(filed_raw: str) -> AvailableAtResolution:
            return AvailableAtResolution(
                available_at=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

        body = _load_fixture("aapl_minimal.json")
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=future_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"


# ---------------------------------------------------------------------------
# C3: typed resolver through materialization program
# ---------------------------------------------------------------------------


class TestC3MaterializationTypedResolver:
    """The materialization program must also reject arbitrary callables."""

    def test_arbitrary_lambda_rejected_through_materialization(self):
        body = _load_fixture("aapl_minimal.json")
        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=lambda filed_raw: datetime(
                2025, 11, 3, 21, 0, tzinfo=UTC
            ),
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_typed_resolver_works_through_materialization(self):
        body = _load_fixture("aapl_minimal.json")
        result = materialize_sec_fundamentals(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        assert len(result.materials) == 2


# ---------------------------------------------------------------------------
# M2: §5.2 "latest admissible annual revision" selection
#
# current must be the latest annual revision whose available_at <= as_of.
# A newer-but-inadmissible filing (or a newer-but-inadmissible period) must
# NOT erase older point-in-time material: the parser falls back to the next
# older admissible accession / period end. The selected accession must stay
# coherent (all metrics from one filing) and revision_of must reflect the
# revision chain that was actually admissible at as_of.
# ---------------------------------------------------------------------------

# A bounded stand-in for official regular-session history. The date-only
# policy resolves a filing date D to the official close of the first regular
# session AFTER D — so a Friday filing resolves to the following Monday close,
# and a date with no following official session is member-unavailable.
_OFFICIAL_SESSION_CLOSES = {
    date(2009, 10, 28): datetime(2009, 10, 28, 20, 0, tzinfo=UTC),
    date(2010, 1, 26): datetime(2010, 1, 26, 21, 0, tzinfo=UTC),
    date(2010, 10, 28): datetime(2010, 10, 28, 20, 0, tzinfo=UTC),
    date(2024, 11, 4): datetime(2024, 11, 4, 21, 0, tzinfo=UTC),
    date(2025, 11, 3): datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
}


def _session_resolver(filed_raw: str):
    """Date-only next-completed-session resolver over official session rows.

    Returns ``None`` (not an AvailableAtResolution) when the filing date has
    no following official session row — SDS §5.4 member-unavailable.
    """
    filed = date.fromisoformat(filed_raw)
    following = sorted(d for d in _OFFICIAL_SESSION_CLOSES if d > filed)
    if not following:
        return None
    return AvailableAtResolution(
        available_at=_OFFICIAL_SESSION_CLOSES[following[0]],
        policy_version=DATE_ONLY_POLICY_VERSION,
        session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
    )


_OLD_ACCN = "0000320193-24-000123"
_NEW_ACCN = "0000320193-25-000079"


def _two_filing_same_period_body() -> bytes:
    """One fiscal period (FY2024) reported by two filings.

    The FY2024 10-K (filed 2024-11-01) reports 391035000000; the FY2025 10-K
    (filed 2025-10-31) restates the same period as a comparison row.
    """

    def rows(accn, filed, revenue, net_income):
        return (
            {
                "start": "2023-10-01",
                "end": "2024-09-28",
                "val": revenue,
                "accn": accn,
                "fy": 2025,
                "fp": "FY",
                "form": "10-K",
                "filed": filed,
            },
            {
                "start": "2023-10-01",
                "end": "2024-09-28",
                "val": net_income,
                "accn": accn,
                "fy": 2025,
                "fp": "FY",
                "form": "10-K",
                "filed": filed,
            },
        )

    old_rev, old_ni = rows(_OLD_ACCN, "2024-11-01", 391035000000, 93736000000)
    new_rev, new_ni = rows(_NEW_ACCN, "2025-10-31", 391000000000, 93700000000)
    return json.dumps(
        {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [old_rev, new_rev]}
                    },
                    "NetIncomeLoss": {"units": {"USD": [old_ni, new_ni]}},
                }
            },
        }
    ).encode()


def _two_period_body() -> bytes:
    """Two consecutive fiscal periods, each from its own filing."""

    def rows(accn, filed, start, end, revenue, net_income):
        return (
            {
                "start": start,
                "end": end,
                "val": revenue,
                "accn": accn,
                "fy": 2025,
                "fp": "FY",
                "form": "10-K",
                "filed": filed,
            },
            {
                "start": start,
                "end": end,
                "val": net_income,
                "accn": accn,
                "fy": 2025,
                "fp": "FY",
                "form": "10-K",
                "filed": filed,
            },
        )

    fy24_rev, fy24_ni = rows(
        _OLD_ACCN,
        "2024-11-01",
        "2023-10-01",
        "2024-09-28",
        391035000000,
        93736000000,
    )
    fy25_rev, fy25_ni = rows(
        _NEW_ACCN,
        "2025-10-31",
        "2024-09-29",
        "2025-09-27",
        416161000000,
        112010000000,
    )
    return json.dumps(
        {
            "cik": 320193,
            "entityName": "Apple Inc.",
            "facts": {
                "us-gaap": {
                    "RevenueFromContractWithCustomerExcludingAssessedTax": {
                        "units": {"USD": [fy24_rev, fy25_rev]}
                    },
                    "NetIncomeLoss": {"units": {"USD": [fy24_ni, fy25_ni]}},
                }
            },
        }
    ).encode()


def _parse(body: bytes, *, as_of: datetime, resolver=_session_resolver):
    return parse_companyfacts_response(
        body,
        cik="0000320193",
        canonical_symbol="AAPL",
        observed_at=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        available_at_resolver=resolver,
        market=_MARKET,
        statement_scope=_SCOPE,
        as_of=as_of,
    )


class TestM2LatestAdmissibleFilingSelection:
    """A newer inadmissible FILING must fall back to the older accession."""

    def test_falls_back_to_older_accession_when_newest_filing_is_future(self):
        result = _parse(
            _two_filing_same_period_body(),
            as_of=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
        )
        assert result.unavailable_reason is None
        assert result.materials is not None
        assert len(result.materials) == 1
        m = result.materials[0]
        # The 2025 restatement is not yet available; the 2024 original is.
        assert m.filing_id == _OLD_ACCN
        assert m.filed_at_raw == "2024-11-01"
        assert m.revenue_ttm.value == Decimal(391035000000)
        assert m.net_income_ttm.value == Decimal(93736000000)
        assert m.available_at == datetime(2024, 11, 4, 21, 0, tzinfo=UTC)
        assert m.available_at <= m.as_of
        assert m.revision_of is None

    def test_newest_accession_wins_once_it_is_admissible(self):
        result = _parse(
            _two_filing_same_period_body(),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        m = result.materials[0]
        assert m.filing_id == _NEW_ACCN
        assert m.revenue_ttm.value == Decimal(391000000000)
        assert m.available_at == datetime(2025, 11, 3, 21, 0, tzinfo=UTC)

    def test_selected_material_is_coherent_to_one_accession(self):
        """Every EXACT metric must come from the single selected filing —
        values must never be mixed across accessions."""
        result = _parse(
            _two_filing_same_period_body(),
            as_of=datetime(2025, 1, 1, 0, 0, tzinfo=UTC),
        )
        m = result.materials[0]
        assert (m.revenue_ttm.value, m.net_income_ttm.value) == (
            Decimal(391035000000),
            Decimal(93736000000),
        )


class TestM2LatestAdmissiblePeriodSelection:
    """A newer inadmissible PERIOD must not erase older PIT material."""

    def test_falls_back_to_older_period_when_newest_period_is_future(self):
        result = _parse(
            _two_period_body(),
            as_of=datetime(2025, 6, 1, 0, 0, tzinfo=UTC),
        )
        assert result.unavailable_reason is None
        assert result.materials is not None
        assert len(result.materials) == 1
        m = result.materials[0]
        assert m.fiscal_year == 2024
        assert m.period_end == date(2024, 9, 28)
        assert m.filing_id == _OLD_ACCN
        assert m.revenue_ttm.value == Decimal(391035000000)
        assert m.available_at <= m.as_of

    def test_newest_period_is_current_once_admissible(self):
        result = _parse(
            _two_period_body(),
            as_of=datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        assert sorted(by_fy) == [2024, 2025]
        assert by_fy[2025].period_end == date(2025, 9, 27)
        assert by_fy[2024].period_end == date(2024, 9, 28)


class TestM2NextCompletedSessionBoundary:
    """§5.4: available_at is the close of the first session AFTER filing."""

    def test_friday_filing_is_inadmissible_until_monday_close(self):
        body = _two_period_body()
        # 2025-10-31 is a Friday; the next official session close is
        # Monday 2025-11-03 21:00Z.
        one_second_early = datetime(2025, 11, 3, 20, 59, 59, tzinfo=UTC)
        result = _parse(body, as_of=one_second_early)
        assert result.materials is not None
        # FY2025 not yet admissible → falls back to FY2024.
        assert [m.fiscal_year for m in result.materials] == [2024]

    def test_monday_close_instant_is_admissible_inclusive(self):
        body = _two_period_body()
        exact_close = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)
        result = _parse(body, as_of=exact_close)
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        assert 2025 in by_fy
        assert by_fy[2025].available_at == exact_close

    def test_filing_with_no_following_official_session_is_member_unavailable(self):
        """A filing date with no following official session row cannot be
        admitted — the parser falls back to the older admissible period."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": _NEW_ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2026-10-30",
                                    },
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": _OLD_ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        # 2026-10-30 has no following session row in the official history.
        assert _session_resolver("2026-10-30") is None
        result = _parse(
            body,
            as_of=datetime(2027, 1, 1, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        assert [m.fiscal_year for m in result.materials] == [2024]
        assert result.materials[0].filing_id == _OLD_ACCN


class TestM2RevisionChainUnderAsOf:
    """§5.3 + §5.2: revision_of must match the revision admissible at as_of."""

    def test_before_amendment_the_original_filing_is_selected(self):
        body = _load_fixture("aapl_amendment.json")
        result = _parse(
            body,
            as_of=datetime(2009, 12, 31, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        m2009 = by_fy[2009]
        # The 10-K/A (filed 2010-01-25) does not exist yet at this as_of.
        assert m2009.filing_id == "0001193125-09-214859"
        assert m2009.net_income_ttm.value == Decimal(5704000000)
        assert m2009.total_assets.value == Decimal(53851000000)
        assert m2009.revision_of is None
        assert m2009.available_at == datetime(2009, 10, 28, 20, 0, tzinfo=UTC)

    def test_after_amendment_the_amendment_is_selected_with_revision_of(self):
        body = _load_fixture("aapl_amendment.json")
        result = _parse(
            body,
            as_of=datetime(2010, 6, 30, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        m2009 = by_fy[2009]
        assert m2009.filing_id == "0001193125-10-012091"
        assert m2009.net_income_ttm.value == Decimal(8235000000)
        assert m2009.total_assets.value == Decimal(47501000000)
        # B2/§5.3: revision_of points at the superseded original 10-K.
        assert m2009.revision_of == "0001193125-09-214859"
        # FY2010 (filed 2010-10-27) is still in the future at this as_of.
        assert 2010 not in by_fy

    def test_amendment_does_not_retroactively_change_earlier_as_of(self):
        """A future revision must not rewrite an earlier point in time."""
        body = _load_fixture("aapl_amendment.json")
        early = _parse(
            body,
            as_of=datetime(2009, 12, 31, 0, 0, tzinfo=UTC),
        )
        late = _parse(
            body,
            as_of=datetime(2010, 6, 30, 0, 0, tzinfo=UTC),
        )
        early2009 = {m.fiscal_year: m for m in early.materials}[2009]
        late2009 = {m.fiscal_year: m for m in late.materials}[2009]
        assert early2009.source_ref != late2009.source_ref
        assert early2009.filing_id != late2009.filing_id


# ---------------------------------------------------------------------------
# C4: OpenDART endpoint architecture test — dead AST loop/pass removed
# ---------------------------------------------------------------------------


class TestC4OpenDARTArchitectureTestIsExact:
    """C4: the OpenDART endpoint architecture test must be exact, not dead.

    The old test had an AST walk loop that did nothing (pass), then relied
    on a substring check. The new test walks the AST and checks each string
    constant directly — no dead loop.
    """

    def test_no_dead_pass_in_opendart_endpoint_test(self):
        """The architecture test must not contain a dead loop with pass."""
        import ast

        path = Path(__file__).parent / "test_fundamental_material_architecture.py"
        source = path.read_text()
        tree = ast.parse(source)

        # Find test_opendart_live_has_no_endpoint_paths
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "test_opendart_live_has_no_endpoint_paths"
            ):
                # The function body must not contain a bare Pass statement
                # that follows a loop/if inside the function body.
                for child in ast.walk(node):
                    assert not isinstance(child, ast.Pass), (
                        "test_opendart_live_has_no_endpoint_paths contains dead pass"
                    )
                break
        else:
            pytest.fail("test_opendart_live_has_no_endpoint_paths not found")

    def test_opendart_architecture_test_checks_string_constants(self):
        """The new test must check AST string constants, not just raw text."""
        import ast

        path = Path(__file__).parent / "test_fundamental_material_architecture.py"
        source = path.read_text()
        tree = ast.parse(source)

        for node in ast.walk(tree):
            if (
                isinstance(node, ast.FunctionDef)
                and node.name == "test_opendart_live_has_no_endpoint_paths"
            ):
                source_text = ast.unparse(node)
                # Must iterate over collected string_consts, not just raw text.
                assert "string_consts" in source_text, (
                    "test must check AST string constants, not just raw text"
                )
                break


# ---------------------------------------------------------------------------
# B1 (reclosure): source_ref deletion bypass — payload key is REQUIRED,
# not permissive. SQL PK, payload source_ref, and recomputed digest must
# all agree in triple-exact verification.
# ---------------------------------------------------------------------------


class TestB1SourceRefDeletionBypass:
    """B1 reclosure: deleting source_ref from the payload must fail closed."""

    def test_payload_without_source_ref_key_fails(self, tmp_path):
        """A payload with the source_ref key deleted must NOT silently
        pass verification — it is a schema violation."""
        import json
        import sqlite3

        from tokmo.facts.sec_fundamentals import parse_companyfacts_response
        from tokmo.records.fundamental_material_store import (
            FundamentalMaterialStore,
            FundamentalMaterialStoreError,
        )

        FIXTURES = Path(__file__).parent / "fixtures" / "sec_companyfacts"
        result = parse_companyfacts_response(
            (FIXTURES / "aapl_minimal.json").read_bytes(),
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        material = result.materials[0]
        store = FundamentalMaterialStore(tmp_path / "fundamentals.db")
        store.insert(material)

        conn = sqlite3.connect(str(tmp_path / "fundamentals.db"))
        row = conn.execute(
            "SELECT payload_json FROM fundamental_materials WHERE source_ref = ?",
            (material.source_ref,),
        ).fetchone()
        payload = json.loads(row[0])
        del payload["source_ref"]
        conn.execute(
            "UPDATE fundamental_materials SET payload_json = ? WHERE source_ref = ?",
            (json.dumps(payload), material.source_ref),
        )
        conn.commit()
        conn.close()

        with pytest.raises(FundamentalMaterialStoreError, match="source_ref_missing"):
            store.get_by_source_ref(material.source_ref)


# ---------------------------------------------------------------------------
# M1 (reclosure): ambiguity identity at exact (metric, start, end).
# Same end different start does NOT collide. Future amendment alternative
# does not retroactively contaminate pre-amendment as_of.
# ---------------------------------------------------------------------------


class TestM1ExactPeriodIdentity:
    """M1 reclosure: ambiguity must be keyed at accession-scoped (metric, start, end)."""

    _ACCN = "0000320193-25-000079"

    def test_same_end_different_start_does_not_collide_on_ambiguity(self):
        """Two revenue periods with different ends: ambiguity concept
        overlaps only one. The other must stay EXACT."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 93736000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    # Only overlaps the FY2024 period, NOT FY2025.
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        by_fy = {m.fiscal_year: m for m in result.materials}
        # FY2025 revenue must be EXACT (no ambiguity concept overlap).
        assert by_fy[2025].revenue_ttm.selection == MetricSelection.EXACT
        assert by_fy[2025].revenue_ttm.value == Decimal(416161000000)
        # FY2024 revenue must be AMBIGUOUS (Revenues overlaps, same accession).
        assert by_fy[2024].revenue_ttm.selection == MetricSelection.AMBIGUOUS


class TestM1AccessionScopedAmbiguity:
    """M1 reclosure: a future amendment's alternative concept must NOT
    retroactively contaminate an earlier accession's primary fact."""

    _ACCN_A = "0000320193-24-000123"
    _ACCN_B = "0000320193-25-000079"

    def test_pre_amendment_accession_remains_exact(self):
        """Accession A's primary revenue is EXACT at pre-amendment as_of
        even though a future accession B carries Revenues for the same
        (start, end). Ambiguity is only evaluated within the same accession."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN_A,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    },
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN_B,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 93736000000,
                                        "accn": self._ACCN_A,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    # Alternative concept in accession B only (future filing).
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN_B,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()

        def resolver(filed_raw):
            av = (
                datetime(2024, 11, 4, 21, 0, tzinfo=UTC)
                if filed_raw == "2024-11-01"
                else datetime(2025, 11, 3, 21, 0, tzinfo=UTC)
            )
            return AvailableAtResolution(
                available_at=av,
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

        # Pre-amendment as_of: only accession A is admissible.
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2024, 12, 1, 0, 0, tzinfo=UTC),
            available_at_resolver=resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=datetime(2024, 12, 1, 0, 0, tzinfo=UTC),
        )
        assert result.materials is not None
        m = result.materials[0]
        assert m.filing_id == self._ACCN_A
        # Revenue must be EXACT — B's Revenues is in a different accession.
        assert m.revenue_ttm is not None
        assert m.revenue_ttm.selection == MetricSelection.EXACT
        assert m.revenue_ttm.value == Decimal(391035000000)


# ---------------------------------------------------------------------------
# M2 (reclosure): coherent same-accession filed/form/fy.
# Conflicting filed dates within the same accession must reject the accession.
# ---------------------------------------------------------------------------


class TestM2CoherentAccessionIdentity:
    """M2 reclosure: metrics from the same accession with conflicting
    filed dates or form-family must be rejected."""

    _ACCN = "0000320193-25-000079"

    def test_conflicting_filed_dates_same_accession_rejected(self):
        """Revenue filed 2025-10-31 and Assets filed 2026-01-15 under the
        same accession must NOT produce a material mixing them."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "end": "2025-09-27",
                                        "val": 359241000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2026-01-15",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=datetime(2025, 11, 5, 0, 0, tzinfo=UTC),
        )
        # Conflicting filed dates within the same accession → fail closed.
        # No other accession to fall back to → no_published_data.
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_conflicting_form_family_same_accession_rejected(self):
        """Revenue form=10-K and Assets form=10-K/A under the same accession
        is incoherent — the accession must be rejected."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Assets": {
                            "units": {
                                "USD": [
                                    {
                                        "end": "2025-09-27",
                                        "val": 359241000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K/A",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_conflicting_filed_between_ambiguous_and_exact_rejected(self):
        """M2 reclosure: ambiguous revenue (filed 2026) + exact net_income
        (filed 2025) in the same accession must be rejected. ALL evidence —
        EXACT or AMBIGUOUS — participates in coherence checks."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2026-01-15",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2026-01-15",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=datetime(2026, 6, 1, 0, 0, tzinfo=UTC),
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_conflicting_fy_same_accession_rejected(self):
        """M2 reclosure: two metrics with different fy in the same accession
        must be rejected (fy coherence check)."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"


# ---------------------------------------------------------------------------
# m1: taxonomy identity — "unknown" is truthful, not a false version claim.
# ---------------------------------------------------------------------------


class TestM1TaxonomyIdentity:
    """m1: TAXONOMY_VERSION must be truthful, not a false year or endpoint
    identity in the version field."""

    def test_taxonomy_version_is_not_false_year(self):
        from tokmo.facts.sec_fundamentals import TAXONOMY_VERSION

        # Must not be a year like "2024" — the body doesn't carry it.
        assert not (len(TAXONOMY_VERSION) == 4 and TAXONOMY_VERSION.isdigit()), (
            f"TAXONOMY_VERSION={TAXONOMY_VERSION!r} is a false year claim"
        )

    def test_taxonomy_version_is_not_endpoint_identity(self):
        from tokmo.facts.sec_fundamentals import TAXONOMY_VERSION

        # Must not contain endpoint/contract identity — that's not a version.
        assert "companyfacts" not in TAXONOMY_VERSION
        assert "endpoint" not in TAXONOMY_VERSION

    def test_taxonomy_version_is_explicit_unknown(self):
        from tokmo.facts.sec_fundamentals import TAXONOMY_VERSION

        # The companyfacts body does not expose taxonomy version per fact.
        # The truthful representation is "unknown".
        assert TAXONOMY_VERSION == "unknown"


# ---------------------------------------------------------------------------
# M1/M2 (second reclosure): alt evidence that causes AMBIGUOUS must
# participate in coherence + as_of checks. A future-dated alt in the same
# accession/period cannot bypass as_of by hiding behind the primary's
# earlier filed date. Alt form/fy conflicts must also reject.
# ---------------------------------------------------------------------------


class TestM1M2AltEvidenceCoherence:
    """M1/M2 second reclosure: every alt_matching fact that causes AMBIGUOUS
    is evidence and must be included with primary in filed/form/fy/start/end
    coherence and available_at<=as_of validation."""

    _ACCN = "0000320193-25-000079"

    def test_alt_future_filed_rejects_accession_under_earlier_as_of(self):
        """Primary filed 2025-10-31, alt Revenues filed 2026-01-15 same
        accn/period. Under as_of 2025-11-05, the alt is inadmissible and
        must reject the whole accession."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2026-01-15",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()

        def resolver(filed_raw):
            av = (
                datetime(2025, 11, 3, 21, 0, tzinfo=UTC)
                if filed_raw == "2025-10-31"
                else datetime(2026, 2, 2, 21, 0, tzinfo=UTC)
            )
            return AvailableAtResolution(
                available_at=av,
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=datetime(2025, 11, 5, 0, 0, tzinfo=UTC),
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_alt_form_family_conflict_rejects(self):
        """Primary form=10-K, alt Revenues form=10-K/A same accn/period
        must reject — the alt is evidence and its form conflicts."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K/A",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_alt_fy_conflict_rejects(self):
        """Primary fy=2025, alt Revenues fy=2024 same accn/period must
        reject — the alt is evidence and its fy conflicts."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_alt_different_start_does_not_trigger_ambiguity(self):
        """Alt with a different start (but same end) must NOT trigger
        ambiguity — only exact (start, end) match counts. The primary
        must remain EXACT."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-28",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()
        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=_typed_resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is not None
        m = result.materials[0]
        assert m.revenue_ttm is not None
        assert m.revenue_ttm.selection == MetricSelection.EXACT


class TestM2AltResolverExceptionFailClosed:
    """M2 reclosure: a malformed alt filed that makes the resolver raise
    ValueError/TypeError must reject the accession cleanly (typed
    no_published_data or older fallback), never let a raw exception escape."""

    _ACCN_NEW = "0000320193-25-000079"
    _ACCN_OLD = "0000320193-24-000123"

    def test_malformed_alt_filed_rejects_without_raw_exception(self):
        """Primary + alt both filed 'not-a-date' (coherence passes), but
        the resolver raises ValueError. Must get no_published_data, not
        a raw ValueError."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "not-a-date",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "not-a-date",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "not-a-date",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()

        def resolver(filed_raw):
            from datetime import date as _date

            _date.fromisoformat(filed_raw)  # raises ValueError
            return AvailableAtResolution(
                available_at=datetime(2025, 11, 3, 21, 0, tzinfo=UTC),
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"

    def test_malformed_alt_filed_falls_back_to_older_coherent_accession(self):
        """When the newest accession has a malformed alt filed but an older
        coherent accession exists for an adjacent period, the parser must
        fall back to the older accession instead of raising."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 391035000000,
                                        "accn": self._ACCN_OLD,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    },
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "not-a-date",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "not-a-date",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2023-10-01",
                                        "end": "2024-09-28",
                                        "val": 93736000000,
                                        "accn": self._ACCN_OLD,
                                        "fy": 2024,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2024-11-01",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()

        def resolver(filed_raw):
            from datetime import date as _date

            d = _date.fromisoformat(filed_raw)  # raises on 'not-a-date'
            if filed_raw == "2024-11-01":
                av = datetime(2024, 11, 4, 21, 0, tzinfo=UTC)
            else:
                av = datetime(2025, 11, 3, 21, 0, tzinfo=UTC)
            return AvailableAtResolution(
                available_at=av,
                policy_version=DATE_ONLY_POLICY_VERSION,
                session_history_source=DATE_ONLY_SESSION_HISTORY_SOURCE,
            )

        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=resolver,
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        # FY2025 accession rejected (malformed filed); FY2024 is the
        # latest admissible coherent period.
        assert result.materials is not None
        assert len(result.materials) == 1
        assert result.materials[0].fiscal_year == 2024
        assert result.materials[0].filing_id == self._ACCN_OLD

    def test_resolver_invalid_typed_result_still_fail_closed(self):
        """Resolver returning a non-AvailableAtResolution must still
        fail closed — this was already handled, verify no regression."""
        body = json.dumps(
            {
                "cik": 320193,
                "entityName": "Apple Inc.",
                "facts": {
                    "us-gaap": {
                        "RevenueFromContractWithCustomerExcludingAssessedTax": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "Revenues": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 416161000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                        "NetIncomeLoss": {
                            "units": {
                                "USD": [
                                    {
                                        "start": "2024-09-29",
                                        "end": "2025-09-27",
                                        "val": 112010000000,
                                        "accn": self._ACCN_NEW,
                                        "fy": 2025,
                                        "fp": "FY",
                                        "form": "10-K",
                                        "filed": "2025-10-31",
                                    },
                                ]
                            }
                        },
                    }
                },
            }
        ).encode()

        result = parse_companyfacts_response(
            body,
            cik="0000320193",
            canonical_symbol="AAPL",
            observed_at=datetime(2025, 11, 1, 12, 0, tzinfo=UTC),
            available_at_resolver=lambda fr: "not-a-resolution",
            market=_MARKET,
            statement_scope=_SCOPE,
            as_of=_AS_OF,
        )
        assert result.materials is None
        assert result.unavailable_reason == "no_published_data"
