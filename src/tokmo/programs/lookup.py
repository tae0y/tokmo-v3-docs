"""Thin lookup program: composition and presentation, never source parsing.

All dependencies (credential, transports, clock) are explicit injections
supplied by a composition root. This module never reads the process
environment, never calls an ambient clock, and carries no live transport;
live wiring is out of Tracer 1a scope.
"""

import sys
from collections.abc import Callable
from datetime import datetime

from tokmo.facts.eia import Transport as EiaTransport
from tokmo.facts.eia import get_wti
from tokmo.facts.treasury import Transport as TreasuryTransport
from tokmo.facts.treasury import get_yield_curve
from tokmo.presentation import format_value
from tokmo.values import UnavailableFact, WtiFact, YieldCurveFact

Clock = Callable[[], datetime]


def format_yield_curve(fact: YieldCurveFact) -> str:
    """Compatibility wrapper — delegates to the presentation layer."""
    return format_value(fact)


def format_wti(result: WtiFact | UnavailableFact) -> str:
    """Compatibility wrapper — delegates to the presentation layer.

    For UnavailableFact, the program adds the ``WTI`` prefix to the
    common ``조회 불가: reason`` output.
    """
    if isinstance(result, UnavailableFact):
        return f"WTI {format_value(result)}"
    return format_value(result)


def main(
    argv: list[str],
    *,
    api_key: str | None,
    eia_transport: EiaTransport,
    treasury_transport: TreasuryTransport,
    clock: Clock,
) -> int:
    source = argv[0] if argv else "treasury"
    if source == "treasury":
        now = clock()
        fact = get_yield_curve(
            year=now.year, month=now.month, transport=treasury_transport, clock=clock
        )
        print(format_yield_curve(fact))
        return 0
    if source == "wti":
        result = get_wti(api_key=api_key, transport=eia_transport, clock=clock)
        print(format_wti(result))
        return 2 if isinstance(result, UnavailableFact) else 0
    print("지원 source: treasury | wti", file=sys.stderr)
    return 64
