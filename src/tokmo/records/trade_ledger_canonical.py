"""Deterministic versioned canonical payload for trade-ledger events.

"Exact duplicate" is defined by byte equality of this payload — never by
file hash, filename, or object identity. The payload covers every semantic
field including the identity triple and excludes everything about how or
when an event was imported. Strings are NFC-normalized and otherwise exact
source text; decimals use one canonical finite grammar; dates are ISO
date-only; absent nullable fields serialize as JSON null. The SHA-256 hash
is an integrity/index aid only — byte comparison stays authoritative.
"""

import hashlib
import json
import re
import unicodedata
from decimal import Decimal, InvalidOperation

from tokmo.values.trade_ledger import TRADE_LEDGER_PAYLOAD_VERSION, TradeLedgerEvent

# Canonical finite decimal grammar: no exponent, no leading "+", no leading
# zeros, no trailing fractional zeros, a single leading "-" only for
# negative values, and zero always spelled "0".
_CANONICAL_DECIMAL_RE = re.compile(r"^-?(?:0|[1-9][0-9]*)(?:\.[0-9]*[1-9])?$")


def canonical_decimal_string(value: str | None) -> str | None:
    """Return the canonical decimal spelling of ``value`` (None passes)."""
    if value is None:
        return None
    try:
        number = Decimal(value)
    except (InvalidOperation, TypeError, ValueError):
        raise ValueError("invalid_decimal") from None
    if not number.is_finite():
        raise ValueError("non_finite_decimal")
    if number.is_zero():
        return "0"

    sign, raw_digits, exponent = number.as_tuple()
    digits = list(raw_digits)
    while digits[-1] == 0:
        digits.pop()
        exponent += 1
    coefficient = "".join(str(digit) for digit in digits)
    if exponent >= 0:
        plain = coefficient + ("0" * exponent)
    else:
        point = len(coefficient) + exponent
        if point > 0:
            plain = coefficient[:point] + "." + coefficient[point:]
        else:
            plain = "0." + ("0" * -point) + coefficient
    return ("-" if sign else "") + plain


def is_canonical_decimal_string(value: object) -> bool:
    """True only for text already in the canonical finite decimal grammar."""
    if not isinstance(value, str) or _CANONICAL_DECIMAL_RE.fullmatch(value) is None:
        return False
    try:
        return canonical_decimal_string(value) == value
    except ValueError:
        return False


def _nfc(text: str | None) -> str | None:
    if text is None:
        return None
    return unicodedata.normalize("NFC", text)


def canonical_payload_bytes(event: TradeLedgerEvent) -> bytes:
    """UTF-8 bytes of the fixed-order, whitespace-free canonical JSON."""
    payload = {
        "payload_version": TRADE_LEDGER_PAYLOAD_VERSION,
        "source": _nfc(event.source),
        "account_scope": _nfc(event.account_scope),
        "source_record_key": _nfc(event.source_record_key),
        "event_kind": _nfc(event.event_kind),
        "trade_date": event.trade_date.isoformat(),
        "transaction_type_raw": _nfc(event.transaction_type_raw),
        "instrument_raw": _nfc(event.instrument_raw),
        "quantity": canonical_decimal_string(event.quantity),
        "unit_price": canonical_decimal_string(event.unit_price),
        "amount": canonical_decimal_string(event.amount),
        "medium_raw": _nfc(event.medium_raw),
        "currency_raw": _nfc(event.currency_raw),
        "settlement_date": (
            None if event.settlement_date is None
            else event.settlement_date.isoformat()
        ),
        "fee": canonical_decimal_string(event.fee),
        "tax": canonical_decimal_string(event.tax),
        "supersedes_source_record_key": _nfc(event.supersedes_source_record_key),
    }
    return json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def payload_hash_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()
