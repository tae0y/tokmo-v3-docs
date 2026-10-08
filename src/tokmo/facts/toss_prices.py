"""Typed Toss current-price facts and the closed one-attempt query boundary."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
import json
import re

from tokmo.values import (
    TOSS_PRICE_SOURCE,
    TossCurrentPriceFact,
    canonical_decimal,
)

PRICES_ENDPOINT_ID = "toss_current_prices"
REQUEST_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class TossPricesRequest:
    """The sole current-price read identity; credentials stay out of repr."""

    endpoint_id: str
    timeout_seconds: float
    symbols: tuple[str, ...] = field(repr=False)
    token: str = field(repr=False)


@dataclass(frozen=True)
class TossPriceQueryOutcome:
    """All facts on success, or none; provider values stay out of repr."""

    code: str
    facts: tuple[TossCurrentPriceFact, ...] = field(repr=False, default=())
    retry_after: int | None = None
    rate_limit_remaining: int | None = None


_SYMBOL_RE = re.compile(r"^[A-Za-z0-9.-]+$")
_PRICE_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)?$")


def valid_symbols(symbols) -> bool:
    return (
        isinstance(symbols, tuple)
        and 1 <= len(symbols) <= 200
        and len(set(symbols)) == len(symbols)
        and all(isinstance(symbol, str) and _SYMBOL_RE.fullmatch(symbol)
                for symbol in symbols)
    )


def _aware(value) -> bool:
    return (isinstance(value, datetime) and value.tzinfo is not None
            and value.utcoffset() is not None)


def build_prices_request(
        *, symbols: tuple[str, ...], token: str) -> TossPricesRequest:
    if not valid_symbols(symbols):
        raise ValueError("invalid_symbols")
    return TossPricesRequest(
        endpoint_id=PRICES_ENDPOINT_ID,
        timeout_seconds=REQUEST_TIMEOUT_SECONDS,
        symbols=symbols,
        token=token,
    )


def parse_prices_response(
        body: bytes, *, requested_symbols: tuple[str, ...],
        fetched_at: datetime) -> TossPriceQueryOutcome:
    if not valid_symbols(requested_symbols) or not _aware(fetched_at):
        return TossPriceQueryOutcome("contract_mismatch")
    if not isinstance(body, bytes):
        return TossPriceQueryOutcome("parse_error")
    try:
        payload = json.loads(body)
    except (TypeError, ValueError, UnicodeError):
        return TossPriceQueryOutcome("parse_error")
    if not isinstance(payload, dict) or not isinstance(payload.get("result"), list):
        return TossPriceQueryOutcome("contract_mismatch")

    facts = []
    for item in payload["result"]:
        if not isinstance(item, dict):
            return TossPriceQueryOutcome("contract_mismatch")
        symbol = item.get("symbol")
        price_raw = item.get("lastPrice")
        currency = item.get("currency")
        timestamp_raw = item.get("timestamp")
        if (not isinstance(symbol, str) or not isinstance(price_raw, str)
                or not 1 <= len(price_raw) <= 30
                or not _PRICE_RE.fullmatch(price_raw)
                or not isinstance(currency, str) or not currency):
            return TossPriceQueryOutcome("contract_mismatch")
        try:
            price = Decimal(price_raw)
            timestamp = (
                None if timestamp_raw is None
                else datetime.fromisoformat(timestamp_raw))
        except (InvalidOperation, TypeError, ValueError):
            return TossPriceQueryOutcome("contract_mismatch")
        if (not price.is_finite() or price < 0
                or (timestamp is not None and not _aware(timestamp))):
            return TossPriceQueryOutcome("contract_mismatch")
        facts.append(TossCurrentPriceFact(
            source=TOSS_PRICE_SOURCE,
            symbol=symbol,
            last_price=canonical_decimal(price),
            currency_raw=currency,
            timestamp=timestamp,
            fetched_at=fetched_at,
        ))
    returned = tuple(fact.symbol for fact in facts)
    if len(set(returned)) != len(returned) or set(returned) != set(requested_symbols):
        return TossPriceQueryOutcome("contract_mismatch")
    return TossPriceQueryOutcome("success", tuple(facts))


def query_current_prices(
        *, symbols: tuple[str, ...], token: str,
        transport: Callable, clock: Callable[[], datetime],
        ) -> TossPriceQueryOutcome:
    """Start exactly one transport attempt, then parse atomically."""
    try:
        request = build_prices_request(symbols=symbols, token=token)
    except ValueError:
        return TossPriceQueryOutcome("protocol_failure")
    reply = transport(request)
    if hasattr(reply, "body"):
        parsed = parse_prices_response(
            reply.body, requested_symbols=symbols, fetched_at=clock())
        return TossPriceQueryOutcome(
            parsed.code,
            parsed.facts,
            rate_limit_remaining=getattr(reply, "rate_limit_remaining", None),
        )
    return TossPriceQueryOutcome(
        getattr(reply, "code", "protocol_failure"),
        retry_after=getattr(reply, "retry_after", None),
    )
