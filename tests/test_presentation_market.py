"""Domain formatter contract tests for WTI, YieldCurve, and UnavailableFact."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tokmo.presentation import format_value
from tokmo.presentation.options import (
    FormatOptions, RAW, NUMBER, USD, KRW, KOREAN,
    CurrencyStyle, DateStyle,
)
from tokmo.presentation.registry import (
    CurrencyConflictError,
)
from tokmo.values import WtiFact, YieldCurveFact, UnavailableFact


# === WtiFact ===

class TestWtiFactFormatter:
    def _make_fact(self, value="78.42", period_date=None):
        return WtiFact(
            source="eia_wti_rwtc",
            series="RWTC",
            period=period_date or date(2026, 7, 25),
            value=Decimal(value),
            units="$/BBL",
            observed_at=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_default_output_exact_match(self):
        """Default domain preset: USD, max2/min2, ISO date, unit included."""
        fact = self._make_fact()
        result = format_value(fact)
        assert result == "WTI (2026-07-25): $78.42/배럴"

    def test_raw_output(self):
        fact = self._make_fact()
        result = format_value(fact, options=RAW)
        assert result == "WTI (2026-07-25): 78.42"

    def test_usd_output(self):
        fact = self._make_fact()
        result = format_value(fact, options=USD)
        assert result == "WTI (2026-07-25): $78.42/배럴"

    def test_krw_conflict(self):
        fact = self._make_fact()
        with pytest.raises(CurrencyConflictError):
            format_value(fact, options=KRW)

    def test_currency_none_allowed(self):
        """CurrencyStyle.NONE is fine — the value is just a plain number.

        NUMBER preset has include_unit=False, so no /배럴 suffix.
        """
        fact = self._make_fact()
        result = format_value(fact, options=NUMBER)
        assert result == "WTI (2026-07-25): 78.42"

    def test_explicit_korean_date_style_is_applied(self):
        fact = self._make_fact()
        result = format_value(fact, options=KOREAN)
        assert result == "WTI (2026년 7월 25일): 78.42/배럴"

    def test_fact_immutable(self):
        fact = self._make_fact()
        format_value(fact)
        format_value(fact, options=RAW)
        assert fact.value == Decimal("78.42")
        assert fact.period == date(2026, 7, 25)


# === YieldCurveFact ===

class TestYieldCurveFormatter:
    def _make_fact(self, two="4.25", ten="4.40"):
        return YieldCurveFact(
            source="us_treasury_daily_par_yield_curve",
            market="US",
            rate_date=date(2026, 7, 25),
            two_year_percent=Decimal(two) if two else None,
            ten_year_percent=Decimal(ten) if ten else None,
            observed_at=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_default_output_exact_match(self):
        fact = self._make_fact()
        result = format_value(fact)
        assert result == "미국 국채금리 (2026-07-25): 2년 4.25% · 10년 4.40% · 스프레드 0.15%p"

    def test_two_year_null(self):
        fact = self._make_fact(two=None, ten="4.40")
        result = format_value(fact)
        assert "2년 없음" in result
        assert "10년 4.40%" in result
        assert "스프레드 없음" in result

    def test_both_null(self):
        fact = self._make_fact(two=None, ten=None)
        result = format_value(fact)
        assert "2년 없음" in result
        assert "10년 없음" in result
        assert "스프레드 없음" in result

    def test_explicit_korean_date_style_is_applied(self):
        fact = self._make_fact()
        result = format_value(fact, options=KOREAN)
        assert result.startswith("미국 국채금리 (2026년 7월 25일):")

    def test_fact_immutable(self):
        fact = self._make_fact()
        format_value(fact)
        assert fact.two_year_percent == Decimal("4.25")
        assert fact.ten_year_percent == Decimal("4.40")


# === UnavailableFact ===

class TestUnavailableFormatter:
    def _make_fact(self, reason="config_missing"):
        return UnavailableFact(
            source="eia_wti_rwtc",
            reason=reason,
            observed_at=datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc),
        )

    def test_default_output(self):
        fact = self._make_fact()
        result = format_value(fact)
        assert result == "조회 불가: config_missing"

    def test_different_reason(self):
        fact = self._make_fact(reason="transport_error")
        result = format_value(fact)
        assert result == "조회 불가: transport_error"


# === format_value public API ===

class TestFormatValueAPI:
    def test_options_none_uses_domain_default(self):
        fact = WtiFact(
            source="eia_wti_rwtc", series="RWTC",
            period=date(2026, 7, 25), value=Decimal("78.42"),
            units="$/BBL", observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        result = format_value(fact)
        assert "$78.42" in result

    def test_options_explicit_applies(self):
        fact = WtiFact(
            source="eia_wti_rwtc", series="RWTC",
            period=date(2026, 7, 25), value=Decimal("78.42"),
            units="$/BBL", observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        result = format_value(fact, options=RAW)
        assert "$" not in result

    def test_unregistered_type_raises(self):
        from tokmo.presentation.registry import UnsupportedPresentationType
        with pytest.raises(UnsupportedPresentationType):
            format_value(42)

    def test_unregistered_type_no_str_fallback(self):
        """A plain dict should not be silently stringified."""
        from tokmo.presentation.registry import UnsupportedPresentationType
        with pytest.raises(UnsupportedPresentationType):
            format_value({"secret": "value"})
