"""Domain formatters for market observations: WTI and Yield Curve.

These formatters compose scalar renderers into full sentences.  Each
domain formatter knows its own domain-default preset and applies it
when the caller does not supply explicit options.

Key contracts
-------------

- ``WtiFact`` has no ``currency`` field; the formatter explicitly states
  ``actual_currency=USD`` from the type's semantic meaning.  Generic
  field introspection is not used.
- Requesting ``KRW`` on a USD-denominated value raises
  ``CurrencyConflictError`` — presentation never converts.
- ``YieldCurveFact`` uses Korean labels with ``%`` and ``%p`` suffixes.
"""

from __future__ import annotations

from tokmo.presentation.options import (
    CurrencyStyle,
    FormatOptions,
    Locale,
)
from tokmo.presentation.registry import CurrencyConflictError, PresentationRegistry
from tokmo.presentation.scalars import (
    format_date,
    format_money,
    format_number,
    format_percent,
)
from tokmo.values import WtiFact, YieldCurveFact

__all__ = [
    "WTI_DEFAULT_OPTIONS",
    "YIELD_CURVE_DEFAULT_OPTIONS",
    "format_wti",
    "format_yield_curve",
    "register_market_formatters",
]


# === Domain default options ===

WTI_DEFAULT_OPTIONS = FormatOptions(
    max_decimal_places=2,
    min_decimal_places=2,
    currency_style=CurrencyStyle.USD,
    include_unit=True,
)

YIELD_CURVE_DEFAULT_OPTIONS = FormatOptions(
    max_decimal_places=2,
    min_decimal_places=2,
    locale=Locale.KO,
)


# === WTI ===


def format_wti(fact: WtiFact, options: FormatOptions) -> str:
    """Render a WTI fact as a sentence.

    The WTI value is USD/barrel. The formatter states this currency
    explicitly from the type's meaning; it does not inspect fields.
    """
    # Currency conflict check: USD domain vs requested currency
    actual_currency = CurrencyStyle.USD
    if options.currency_style not in (CurrencyStyle.NONE, actual_currency):
        raise CurrencyConflictError(
            f"wti_is_usd_requested_{options.currency_style.value}"
        )

    date_str = format_date(fact.period, options=options)

    if options.currency_style is CurrencyStyle.USD:
        money_str = format_money(fact.value, options=options)
    else:
        # NONE: plain number, no currency symbol
        money_str = format_number(fact.value, options=options)

    if options.include_unit:
        return f"WTI ({date_str}): {money_str}/배럴"
    return f"WTI ({date_str}): {money_str}"


# === Yield Curve ===


def format_yield_curve(fact: YieldCurveFact, options: FormatOptions) -> str:
    """Render a yield curve fact as a Korean sentence."""
    date_str = format_date(fact.rate_date, options=options)

    two = format_percent(
        fact.two_year_percent,
        options=options,
        suffix="%",
    )
    ten = format_percent(
        fact.ten_year_percent,
        options=options,
        suffix="%",
    )
    spread = format_percent(
        fact.spread_percentage_points,
        options=options,
        suffix="%p",
    )

    return f"미국 국채금리 ({date_str}): 2년 {two} · 10년 {ten} · 스프레드 {spread}"


# === Registration ===


def register_market_formatters(registry: PresentationRegistry) -> None:
    """Register WTI and YieldCurve formatters on the given registry."""
    registry.register(
        WtiFact,
        _wti_entry,
        default_options=WTI_DEFAULT_OPTIONS,
    )
    registry.register(
        YieldCurveFact,
        _yield_curve_entry,
        default_options=YIELD_CURVE_DEFAULT_OPTIONS,
    )


def _wti_entry(fact: WtiFact, options: FormatOptions) -> str:
    """Registry entry: uses WTI domain default when options is a placeholder.

    The registry always passes a non-None FormatOptions. When the public
    ``format_value`` API receives ``options=None``, it resolves the domain
    default before calling the registry. So here we just delegate.
    """
    return format_wti(fact, options)


def _yield_curve_entry(fact: YieldCurveFact, options: FormatOptions) -> str:
    return format_yield_curve(fact, options)
