"""Scalar renderer contract tests for the presentation layer."""

from datetime import date, datetime, timezone
from decimal import Decimal

import pytest

from tokmo.presentation.options import (
    FormatOptions,
    NumberStyle,
    CurrencyStyle,
    DateStyle,
    DateTimeStyle,
    Locale,
    RAW,
    NUMBER,
    KRW,
    USD,
    KOREAN,
)
from tokmo.presentation.scalars import (
    format_number,
    format_money,
    format_percent,
    format_date,
    format_datetime,
)


# === Options validation ===

class TestFormatOptionsValidation:
    def test_defaults(self):
        opts = FormatOptions()
        assert opts.number_style is NumberStyle.GROUPED
        assert opts.max_decimal_places == 2
        assert opts.min_decimal_places == 0
        assert opts.currency_style is CurrencyStyle.NONE
        assert opts.include_unit is True
        assert opts.date_style is DateStyle.ISO
        assert opts.datetime_style is DateTimeStyle.ISO
        assert opts.timezone is None
        assert opts.locale is Locale.KO
        assert opts.null_text is None

    def test_max_decimal_places_none_allowed(self):
        FormatOptions(max_decimal_places=None)

    def test_max_decimal_places_zero_allowed(self):
        FormatOptions(max_decimal_places=0)

    def test_max_decimal_places_twelve_allowed(self):
        FormatOptions(max_decimal_places=12)

    def test_max_decimal_places_thirteen_rejected(self):
        with pytest.raises(ValueError):
            FormatOptions(max_decimal_places=13)

    def test_max_decimal_places_negative_rejected(self):
        with pytest.raises(ValueError):
            FormatOptions(max_decimal_places=-1)

    def test_max_decimal_places_bool_rejected(self):
        with pytest.raises(ValueError):
            FormatOptions(max_decimal_places=True)

    def test_min_decimal_places_twelve_allowed(self):
        FormatOptions(max_decimal_places=12, min_decimal_places=12)

    def test_min_decimal_places_thirteen_rejected(self):
        with pytest.raises(ValueError):
            FormatOptions(min_decimal_places=13)

    def test_min_greater_than_max_rejected(self):
        with pytest.raises(ValueError):
            FormatOptions(max_decimal_places=2, min_decimal_places=3)

    def test_min_equal_max_allowed(self):
        FormatOptions(max_decimal_places=2, min_decimal_places=2)

    def test_min_le_max_when_max_none(self):
        FormatOptions(max_decimal_places=None, min_decimal_places=5)

    def test_invalid_timezone_rejected(self):
        with pytest.raises(Exception):
            FormatOptions(timezone="Bogus/Zone")

    def test_valid_timezone_accepted(self):
        FormatOptions(timezone="Asia/Seoul")

    def test_frozen(self):
        opts = FormatOptions()
        with pytest.raises(Exception):
            opts.max_decimal_places = 5


class TestPresets:
    def test_raw_preset(self):
        assert RAW.number_style is NumberStyle.RAW
        assert RAW.max_decimal_places is None
        assert RAW.min_decimal_places == 0
        assert RAW.currency_style is CurrencyStyle.NONE
        assert RAW.include_unit is False

    def test_number_preset(self):
        assert NUMBER.number_style is NumberStyle.GROUPED
        assert NUMBER.max_decimal_places == 2
        assert NUMBER.min_decimal_places == 0
        assert NUMBER.currency_style is CurrencyStyle.NONE
        assert NUMBER.include_unit is False

    def test_krw_preset(self):
        assert KRW.number_style is NumberStyle.GROUPED
        assert KRW.max_decimal_places == 0
        assert KRW.min_decimal_places == 0
        assert KRW.currency_style is CurrencyStyle.KRW

    def test_usd_preset(self):
        assert USD.number_style is NumberStyle.GROUPED
        assert USD.max_decimal_places == 2
        assert USD.min_decimal_places == 2
        assert USD.currency_style is CurrencyStyle.USD

    def test_korean_preset(self):
        assert KOREAN.date_style is DateStyle.KOREAN
        assert KOREAN.datetime_style is DateTimeStyle.KOREAN
        assert KOREAN.locale is Locale.KO


# === format_number ===

class TestFormatNumberRaw:
    def test_raw_preserves_integer(self):
        assert format_number(100, options=RAW) == "100"

    def test_raw_preserves_decimal_trailing_zeros(self):
        assert format_number(Decimal("100.00"), options=RAW) == "100.00"

    def test_raw_preserves_scientific(self):
        assert format_number(Decimal("1E+5"), options=RAW) == "1E+5"

    def test_raw_preserves_decimal_value(self):
        assert format_number(Decimal("78.42"), options=RAW) == "78.42"


class TestFormatNumberGrouped:
    def test_grouped_integer(self):
        assert format_number(Decimal("100000"), options=NUMBER) == "100,000"

    def test_grouped_scientific_expanded(self):
        assert format_number(Decimal("1E+5"), options=NUMBER) == "100,000"

    def test_grouped_preserves_original_decimals_when_max_none(self):
        from dataclasses import replace
        opts = replace(NUMBER, max_decimal_places=None)
        assert format_number(Decimal("100000.00"), options=opts) == "100,000.00"

    def test_grouped_zero_preserves_original_decimals_when_max_none(self):
        from dataclasses import replace
        opts = replace(NUMBER, max_decimal_places=None)
        assert format_number(Decimal("0.00"), options=opts) == "0.00"
        assert format_number(Decimal("-0.00"), options=opts) == "-0.00"

    def test_contract_100(self):
        assert format_number(Decimal("100"), options=NUMBER) == "100"

    def test_contract_100_5(self):
        assert format_number(Decimal("100.5"), options=NUMBER) == "100.5"

    def test_contract_100_12(self):
        assert format_number(Decimal("100.12"), options=NUMBER) == "100.12"

    def test_contract_100_125_round_half_up(self):
        assert format_number(Decimal("100.125"), options=NUMBER) == "100.13"

    def test_contract_100_1267_round_half_up(self):
        assert format_number(Decimal("100.1267"), options=NUMBER) == "100.13"

    def test_zero(self):
        assert format_number(Decimal("0"), options=NUMBER) == "0"

    def test_negative_grouped(self):
        assert format_number(Decimal("-100000"), options=NUMBER) == "-100,000"

    def test_negative_with_decimals(self):
        assert format_number(Decimal("-100.5"), options=NUMBER) == "-100.5"

    def test_very_large_number(self):
        result = format_number(Decimal("999999999999999"), options=NUMBER)
        assert result == "999,999,999,999,999"


class TestFormatNumberMinMax:
    def test_usd_preset_pads_to_two(self):
        assert format_number(Decimal("100"), options=USD) == "100.00"

    def test_usd_preset_pads_half(self):
        assert format_number(Decimal("100.5"), options=USD) == "100.50"

    def test_krw_rounds_to_integer(self):
        assert format_number(Decimal("100000"), options=KRW) == "100,000"


class TestFormatNumberRejections:
    def test_float_rejected(self):
        with pytest.raises(TypeError):
            format_number(3.14, options=NUMBER)

    def test_bool_rejected(self):
        with pytest.raises(TypeError):
            format_number(True, options=NUMBER)

    def test_string_rejected(self):
        with pytest.raises(TypeError):
            format_number("100", options=NUMBER)

    @pytest.mark.parametrize("value", [Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")])
    def test_non_finite_decimal_rejected(self, value):
        with pytest.raises(ValueError, match="non_finite_decimal_not_allowed"):
            format_number(value, options=NUMBER)


class TestFormatNumberImmutability:
    def test_original_decimal_unchanged(self):
        d = Decimal("100.1267")
        format_number(d, options=NUMBER)
        assert d == Decimal("100.1267")


# === format_money ===

class TestFormatMoney:
    def test_krw_positive(self):
        assert format_money(Decimal("100000"), options=KRW) == "100,000원"

    def test_krw_negative(self):
        assert format_money(Decimal("-100000"), options=KRW) == "-100,000원"

    def test_usd_positive(self):
        assert format_money(Decimal("100"), options=USD) == "$100.00"

    def test_usd_half(self):
        assert format_money(Decimal("100.5"), options=USD) == "$100.50"

    def test_usd_negative(self):
        assert format_money(Decimal("-100"), options=USD) == "-$100.00"

    def test_none_currency(self):
        assert format_money(Decimal("1000"), options=NUMBER) == "1,000"


# === format_percent ===

class TestFormatPercent:
    def test_basic_percent(self):
        assert format_percent(Decimal("4.25"), options=NUMBER) == "4.25%"

    def test_none_returns_ko_null(self):
        assert format_percent(None, options=FormatOptions()) == "없음"

    def test_none_returns_en_null(self):
        opts = FormatOptions(locale=Locale.EN)
        assert format_percent(None, options=opts) == "N/A"

    def test_none_custom_null_text(self):
        opts = FormatOptions(null_text="--")
        assert format_percent(None, options=opts) == "--"

    def test_custom_suffix(self):
        assert format_percent(Decimal("0.15"), options=USD, suffix="%p") == "0.15%p"


# === format_date ===

class TestFormatDate:
    def test_iso(self):
        assert format_date(date(2026, 7, 25), options=FormatOptions()) == "2026-07-25"

    def test_compact(self):
        opts = FormatOptions(date_style=DateStyle.COMPACT)
        assert format_date(date(2026, 7, 25), options=opts) == "20260725"

    def test_korean(self):
        opts = FormatOptions(date_style=DateStyle.KOREAN)
        assert format_date(date(2026, 7, 25), options=opts) == "2026년 7월 25일"


# === format_datetime ===

class TestFormatDatetime:
    def test_iso_aware(self):
        dt = datetime(2026, 7, 25, 12, 30, 0, tzinfo=timezone.utc)
        assert format_datetime(dt, options=FormatOptions()) == "2026-07-25T12:30:00+00:00"

    def test_timezone_conversion(self):
        dt = datetime(2026, 7, 25, 12, 0, 0, tzinfo=timezone.utc)
        opts = FormatOptions(timezone="Asia/Seoul")
        result = format_datetime(dt, options=opts)
        assert "21:00" in result
        assert "+09:00" in result

    def test_naive_datetime_no_timezone_ok(self):
        dt = datetime(2026, 7, 25, 12, 0, 0)
        result = format_datetime(dt, options=FormatOptions())
        assert result == "2026-07-25T12:00:00"

    def test_naive_datetime_with_timezone_rejected(self):
        dt = datetime(2026, 7, 25, 12, 0, 0)
        opts = FormatOptions(timezone="Asia/Seoul")
        with pytest.raises(ValueError):
            format_datetime(dt, options=opts)

    def test_korean_style(self):
        dt = datetime(2026, 7, 25, 12, 30, 0, tzinfo=timezone.utc)
        opts = FormatOptions(datetime_style=DateTimeStyle.KOREAN)
        assert format_datetime(dt, options=opts) == "2026년 7월 25일 12시 30분"
