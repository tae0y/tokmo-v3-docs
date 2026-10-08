"""Registry contract tests for the presentation layer."""

import pytest

from tokmo.presentation.options import FormatOptions, NUMBER
from tokmo.presentation.registry import (
    PresentationRegistry,
    UnsupportedPresentationType,
    DuplicateRegistrationError,
)
from tokmo.presentation import build_default_registry, format_value


class TestRegistryCore:
    def test_register_and_format(self):
        registry = PresentationRegistry()

        class MyValue:
            pass

        def formatter(value, options):
            return "formatted"

        registry.register(MyValue, formatter)
        result = registry.format(MyValue(), options=FormatOptions())
        assert result == "formatted"

    def test_exact_type_dispatch(self):
        registry = PresentationRegistry()

        class Base:
            pass

        class Child(Base):
            pass

        registry.register(Base, lambda v, o: "base")
        # Child is not Base exactly
        with pytest.raises(UnsupportedPresentationType):
            registry.format(Child(), options=FormatOptions())

    def test_duplicate_registration_rejected(self):
        registry = PresentationRegistry()

        class MyValue:
            pass

        registry.register(MyValue, lambda v, o: "one")
        with pytest.raises(DuplicateRegistrationError):
            registry.register(MyValue, lambda v, o: "two")

    def test_unregistered_type_rejected(self):
        registry = PresentationRegistry()

        class Unknown:
            pass

        with pytest.raises(UnsupportedPresentationType):
            registry.format(Unknown(), options=FormatOptions())

    def test_formatter_returns_non_str_rejected(self):
        registry = PresentationRegistry()

        class MyValue:
            pass

        registry.register(MyValue, lambda v, o: 42)
        with pytest.raises(TypeError):
            registry.format(MyValue(), options=FormatOptions())

    def test_custom_registry_injected(self):
        registry = PresentationRegistry()

        class MyValue:
            pass

        registry.register(MyValue, lambda v, o: "custom")
        assert registry.format(MyValue(), options=FormatOptions()) == "custom"

    def test_custom_registry_owns_domain_default_without_prior_global_call(self):
        from datetime import date, datetime, timezone
        from decimal import Decimal

        from tokmo.presentation.formatters.market import register_market_formatters
        from tokmo.values import WtiFact

        registry = PresentationRegistry()
        register_market_formatters(registry)
        fact = WtiFact(
            source="eia_wti_rwtc",
            series="RWTC",
            period=date(2026, 7, 25),
            value=Decimal("78.42"),
            units="$/BBL",
            observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )

        assert format_value(fact, registry=registry) == (
            "WTI (2026-07-25): $78.42/배럴"
        )

    def test_registered_type_without_default_fails_closed_for_none_options(self):
        registry = PresentationRegistry()

        class MyValue:
            pass

        registry.register(MyValue, lambda value, options: "custom")
        with pytest.raises(
            UnsupportedPresentationType,
            match="no_domain_default_for_type:MyValue",
        ):
            format_value(MyValue(), registry=registry)


class TestDefaultRegistry:
    def test_has_wti_formatter(self):
        from tokmo.values import WtiFact
        registry = build_default_registry()
        from datetime import date, datetime
        from decimal import Decimal
        fact = WtiFact(
            source="eia_wti_rwtc", series="RWTC",
            period=date(2026, 7, 25), value=Decimal("78.42"),
            units="$/BBL", observed_at=datetime(2026, 7, 25, tzinfo=__import__("datetime").timezone.utc),
        )
        result = registry.format(fact, options=FormatOptions())
        assert "78.42" in result

    def test_has_yield_curve_formatter(self):
        from tokmo.values import YieldCurveFact
        registry = build_default_registry()
        from datetime import date, datetime
        from decimal import Decimal
        fact = YieldCurveFact(
            source="us_treasury_daily_par_yield_curve", market="US",
            rate_date=date(2026, 7, 25),
            two_year_percent=Decimal("4.25"), ten_year_percent=Decimal("4.40"),
            observed_at=datetime(2026, 7, 25, tzinfo=__import__("datetime").timezone.utc),
        )
        result = registry.format(fact, options=FormatOptions())
        assert "4.25" in result

    def test_has_unavailable_formatter(self):
        from tokmo.values import UnavailableFact
        registry = build_default_registry()
        from datetime import datetime, timezone
        fact = UnavailableFact(
            source="eia_wti_rwtc", reason="config_missing",
            observed_at=datetime(2026, 7, 25, tzinfo=timezone.utc),
        )
        result = registry.format(fact, options=FormatOptions())
        assert "조회 불가" in result
        assert "config_missing" in result
