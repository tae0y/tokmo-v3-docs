"""User-facing instrument display contract (2026-08-21, user-approved).

One pure value contract owns every user-facing instrument rendering so the
string shape lives in exactly one place:

    KR: 한글명(종목코드)   — 현대차(005380)
    US: 영문티커(한글명)   — MRNA(모더나)

The name is never translated, transliterated, or guessed. Callers supply it
from canonical, Toss-evidenced identity (the Git-tracked playbook document's
``instrument_name``); when that identity is unavailable the display fails
soft to the canonical symbol/code alone rather than inventing a name.

This module is pure: no input/output, no clock, no provider. Renderers pass
values in and get one string back.
"""

from dataclasses import dataclass

INSTRUMENT_DISPLAY_MARKETS = frozenset({"KR", "US"})


class InstrumentDisplayError(ValueError):
    pass


@dataclass(frozen=True)
class InstrumentDisplayV1:
    """Canonical symbol plus the optional canonical name for one market."""

    market: str
    symbol: str
    name: "str | None"

    def __post_init__(self):
        if self.market not in INSTRUMENT_DISPLAY_MARKETS:
            raise InstrumentDisplayError("unknown instrument display market")
        if type(self.symbol) is not str or not self.symbol.strip():
            raise InstrumentDisplayError("instrument display needs a symbol")
        if self.name is not None and type(self.name) is not str:
            raise InstrumentDisplayError("instrument name must be text")


def format_instrument(value: InstrumentDisplayV1, *, name_first=False) -> str:
    """The one rendering: KR name-first, US ticker-first, else symbol only."""
    if not isinstance(value, InstrumentDisplayV1):
        raise InstrumentDisplayError("invalid instrument display value")
    name = value.name
    if name is None or not name.strip():
        # Fail soft: a missing canonical name never becomes an invented one.
        return value.symbol
    if value.market == "KR" or name_first:
        return name + "(" + value.symbol + ")"
    return value.symbol + "(" + name + ")"


def order_alert_title(market, symbol, name) -> str:
    """Order alerts only: Korean name first for both KR and US (2026-09-22)."""
    if (
        isinstance(name, str)
        and any("가" <= c <= "힣" for c in name)
        and "\n" not in name
        and "\r" not in name
        and market in INSTRUMENT_DISPLAY_MARKETS
    ):
        return format_instrument(
            InstrumentDisplayV1(market, symbol, name.strip()), name_first=True
        )
    return display_or_symbol(market, symbol, None)


def display_or_symbol(market, symbol, name) -> str:
    """Renderer-facing entry point; any invalid input falls back to symbol."""
    try:
        return format_instrument(InstrumentDisplayV1(market, symbol, name))
    except InstrumentDisplayError:
        return symbol if type(symbol) is str and symbol else ""
