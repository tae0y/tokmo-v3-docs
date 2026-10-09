"""Public API for the presentation layer.

This module exposes:
- ``format_value()``: the single entry point for rendering any typed value.
- ``build_default_registry()``: explicit assembly of the default registry.
- Presets and options types for callers who need fine-grained control.

Import direction
----------------

presentation → values (allowed)
presentation → presentation (allowed)
presentation → facts/records/programs/runtime/observability/composition (forbidden)
"""

from __future__ import annotations

from tokmo.presentation.formatters.market import register_market_formatters
from tokmo.presentation.formatters.results import register_result_formatters
from tokmo.presentation.options import (
    KOREAN,
    KRW,
    NUMBER,
    RAW,
    USD,
    CurrencyStyle,
    DateStyle,
    DateTimeStyle,
    FormatOptions,
    Locale,
    NumberStyle,
)
from tokmo.presentation.registry import (
    CurrencyConflictError,
    DuplicateRegistrationError,
    PresentationRegistry,
    UnsupportedPresentationType,
)
from tokmo.values import UnavailableFact, WtiFact, YieldCurveFact

__all__ = [
    # Options
    "FormatOptions",
    "NumberStyle",
    "CurrencyStyle",
    "DateStyle",
    "DateTimeStyle",
    "Locale",
    # Presets
    "RAW",
    "NUMBER",
    "KRW",
    "USD",
    "KOREAN",
    # Registry
    "PresentationRegistry",
    "UnsupportedPresentationType",
    "DuplicateRegistrationError",
    "CurrencyConflictError",
    "build_default_registry",
    # API
    "format_value",
]


def build_default_registry() -> PresentationRegistry:
    """Explicitly assemble the default registry with all known formatters.

    This function is the single bootstrap point. Import side-effects do
    not trigger registration — callers must invoke this explicitly.
    """
    registry = PresentationRegistry()
    register_market_formatters(registry)
    register_result_formatters(registry)

    return registry


# Module-level default registry (lazy)
_default_registry: PresentationRegistry | None = None


def _get_default_registry() -> PresentationRegistry:
    global _default_registry
    if _default_registry is None:
        _default_registry = build_default_registry()
    return _default_registry


def format_value(
    value: object,
    *,
    options: FormatOptions | None = None,
    registry: PresentationRegistry | None = None,
) -> str:
    """Render a typed value to its string representation.

    Parameters
    ----------
    value
        The typed value to format. Must be a registered type.
    options
        Presentation choices. If ``None``, the domain default preset for
        the value's type is used. If provided, the options are applied
        as-is.
    registry
        Custom registry. If ``None``, the module-level default registry
        is used.

    Raises
    ------
    UnsupportedPresentationType
        If the value's exact type has no registered formatter.
    CurrencyConflictError
        If the requested currency conflicts with the value's domain currency.
    """
    reg = registry if registry is not None else _get_default_registry()

    # Resolve domain default if options is None
    if options is None:
        options = reg.default_options_for(type(value))

    return reg.format(value, options=options)
