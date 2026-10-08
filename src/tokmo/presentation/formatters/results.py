"""Domain formatter for UnavailableFact.

Renders as ``조회 불가: <reason>`` without any provider-specific context.
The calling program adds domain prefixes (e.g. ``WTI``) when composing
the final user-facing message.
"""

from __future__ import annotations

from tokmo.presentation.options import FormatOptions
from tokmo.presentation.registry import PresentationRegistry
from tokmo.values import UnavailableFact

__all__ = ["format_unavailable", "register_result_formatters"]


def format_unavailable(fact: UnavailableFact, options: FormatOptions) -> str:
    return f"조회 불가: {fact.reason}"


def register_result_formatters(registry: PresentationRegistry) -> None:
    registry.register(
        UnavailableFact,
        _unavailable_entry,
        default_options=FormatOptions(),
    )


def _unavailable_entry(fact: UnavailableFact, options: FormatOptions) -> str:
    return format_unavailable(fact, options)
