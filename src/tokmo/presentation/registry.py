"""Presentation registry: typed value dispatch to domain formatters.

The registry maps an exact type to a formatter function.  It does NOT
use subclass fallback or duck-typing — unregistered types raise
``UnsupportedPresentationType`` (fail-closed) rather than falling back
to ``str(value)``, because a stray ``str()`` could leak sensitive data
(positions, account numbers) into user-visible output.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from tokmo.presentation.options import FormatOptions

__all__ = [
    "Formatter",
    "PresentationRegistry",
    "UnsupportedPresentationType",
    "DuplicateRegistrationError",
    "CurrencyConflictError",
]

Formatter = Callable[[Any, FormatOptions], str]


class UnsupportedPresentationType(Exception):
    """The value's exact type has no registered formatter."""


class DuplicateRegistrationError(Exception):
    """A formatter is already registered for this exact type."""


class CurrencyConflictError(Exception):
    """The requested currency conflicts with the value's domain currency."""


@dataclass
class PresentationRegistry:
    """Exact-type dispatch table for domain formatters."""

    _formatters: dict[type, Formatter] = field(default_factory=dict)
    _default_options: dict[type, FormatOptions] = field(default_factory=dict)

    def register(
        self,
        value_type: type,
        formatter: Formatter,
        *,
        default_options: FormatOptions | None = None,
    ) -> None:
        if value_type in self._formatters:
            raise DuplicateRegistrationError(
                f"type_already_registered:{value_type.__name__}"
            )
        self._formatters[value_type] = formatter
        if default_options is not None:
            self._default_options[value_type] = default_options

    def default_options_for(self, value_type: type) -> FormatOptions:
        options = self._default_options.get(value_type)
        if options is None:
            raise UnsupportedPresentationType(
                f"no_domain_default_for_type:{value_type.__name__}"
            )
        return options

    def format(self, value: object, *, options: FormatOptions) -> str:
        exact_type = type(value)
        formatter = self._formatters.get(exact_type)
        if formatter is None:
            raise UnsupportedPresentationType(
                f"no_formatter_for_type:{exact_type.__name__}"
            )
        result = formatter(value, options)
        if not isinstance(result, str):
            raise TypeError(
                f"formatter_returned_non_str:{type(result).__name__}"
            )
        return result
