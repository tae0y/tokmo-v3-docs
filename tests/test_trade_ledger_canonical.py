"""Deterministic canonical payload serialization for trade-ledger events.

Byte equality of this payload is the authoritative duplicate definition, so
these tests pin the exact key order, NFC normalization, canonical decimal
grammar, null handling, and SHA-256 hashing. All values are synthetic.
"""

import hashlib
import json
import unicodedata
from datetime import date

import pytest

from tokmo.records.trade_ledger_canonical import (
    canonical_decimal_string,
    canonical_payload_bytes,
    is_canonical_decimal_string,
    payload_hash_hex,
)
from tokmo.values.trade_ledger import TradeLedgerEvent


def event(**overrides):
    values = dict(
        source="retirement_excel_v1",
        account_scope="retirement_pension_1",
        source_record_key="row-000001",
        event_kind="EXECUTION",
        supersedes_source_record_key=None,
        trade_date=date(2026, 7, 1),
        transaction_type_raw="ETF매수",
        instrument_raw="Fixture ETF Alpha",
        quantity="3",
        unit_price="2500",
        amount="7500",
        medium_raw="FixtureApp",
        currency_raw=None,
        settlement_date=None,
        fee=None,
        tax=None,
    )
    values.update(overrides)
    return TradeLedgerEvent(**values)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0", "0"),
        ("-0", "0"),
        ("0.000", "0"),
        ("0E-8", "0"),
        ("1.50", "1.5"),
        ("007", "7"),
        ("7.842E+1", "78.42"),
        ("1e2", "100"),
        ("1E-3", "0.001"),
        ("-2.30", "-2.3"),
        ("-0.5", "-0.5"),
        (".5", "0.5"),
        ("+7", "7"),
        ("123456789012345678901234567890.1", "123456789012345678901234567890.1"),
    ],
)
def test_canonical_decimal_strings_are_deterministic(raw, expected):
    assert canonical_decimal_string(raw) == expected
    # Idempotent: the canonical form maps to itself.
    assert canonical_decimal_string(expected) == expected
    assert is_canonical_decimal_string(expected)


def test_canonical_decimal_none_passes_through():
    assert canonical_decimal_string(None) is None


@pytest.mark.parametrize("raw", ["NaN", "sNaN", "Infinity", "-Infinity", "inf"])
def test_non_finite_decimals_raise_the_closed_token(raw):
    with pytest.raises(ValueError) as captured:
        canonical_decimal_string(raw)
    assert str(captured.value) == "non_finite_decimal"
    assert raw not in str(captured.value)
    assert not is_canonical_decimal_string(raw)


def test_malformed_decimals_raise_the_closed_token_without_echoing():
    for raw in ("", "abc", "1,000", "1.2.3", "--1"):
        with pytest.raises(ValueError) as captured:
            canonical_decimal_string(raw)
        assert str(captured.value) == "invalid_decimal"
        assert not is_canonical_decimal_string(raw)


@pytest.mark.parametrize(
    "text,canonical",
    [
        ("01", False),
        ("1.50", False),
        ("-0", False),
        ("+1", False),
        ("1e2", False),
        ("1.", False),
        ("0.5", True),
        ("-12.75", True),
        ("0", True),
    ],
)
def test_is_canonical_decimal_string_accepts_only_the_canonical_grammar(
    text, canonical
):
    assert is_canonical_decimal_string(text) is canonical


def test_payload_bytes_use_the_frozen_key_order_and_compact_separators():
    payload = canonical_payload_bytes(event())
    decoded = json.loads(payload.decode("utf-8"))
    assert list(decoded) == [
        "payload_version",
        "source",
        "account_scope",
        "source_record_key",
        "event_kind",
        "trade_date",
        "transaction_type_raw",
        "instrument_raw",
        "quantity",
        "unit_price",
        "amount",
        "medium_raw",
        "currency_raw",
        "settlement_date",
        "fee",
        "tax",
        "supersedes_source_record_key",
    ]
    assert decoded["payload_version"] == 1
    assert decoded["trade_date"] == "2026-07-01"
    assert decoded["currency_raw"] is None
    assert decoded["settlement_date"] is None
    assert decoded["fee"] is None
    assert decoded["tax"] is None
    assert decoded["supersedes_source_record_key"] is None
    # Compact separators: no whitespace after "," or ":" anywhere.
    assert b", " not in payload
    assert b": " not in payload
    assert "ETF매수".encode("utf-8") in payload


def test_payload_bytes_are_nfc_normalized_and_otherwise_verbatim():
    composed = event(instrument_raw="Fixture 가나 ETF")
    decomposed = event(
        instrument_raw=unicodedata.normalize("NFD", "Fixture 가나 ETF")
    )
    assert composed.instrument_raw != decomposed.instrument_raw
    assert canonical_payload_bytes(composed) == canonical_payload_bytes(decomposed)
    spaced = event(transaction_type_raw="매수정정*", instrument_raw="Fixture  double  space")
    decoded = json.loads(canonical_payload_bytes(spaced).decode("utf-8"))
    assert decoded["transaction_type_raw"] == "매수정정*"
    assert decoded["instrument_raw"] == "Fixture  double  space"


def test_payload_bytes_include_identity_and_nullables_and_hash_matches():
    linked = event(
        source="fixture_future_source_1",
        account_scope="fixture_scope_1",
        source_record_key="fix-0002",
        event_kind="CORRECTION",
        supersedes_source_record_key="fix-0001",
        currency_raw="FIX",
        settlement_date=date(2026, 7, 3),
        fee="1.5",
        tax="0",
    )
    payload = canonical_payload_bytes(linked)
    decoded = json.loads(payload.decode("utf-8"))
    assert decoded["source"] == "fixture_future_source_1"
    assert decoded["supersedes_source_record_key"] == "fix-0001"
    assert decoded["settlement_date"] == "2026-07-03"
    assert decoded["fee"] == "1.5"
    assert decoded["tax"] == "0"
    assert payload_hash_hex(payload) == hashlib.sha256(payload).hexdigest()


def test_numerically_equal_but_textually_distinct_decimals_share_bytes():
    assert canonical_payload_bytes(event(quantity="3")) == canonical_payload_bytes(
        event(quantity="3")
    )
    # The store validates canonical input, but the serializer itself is
    # deterministic across equivalent decimal spellings.
    assert canonical_payload_bytes(event(unit_price="2500")) == canonical_payload_bytes(
        event(unit_price="2.5E+3")
    )
    assert canonical_payload_bytes(event()) != canonical_payload_bytes(
        event(amount="7500.1")
    )


def test_empty_string_never_collapses_to_null():
    # An empty nullable string is not "absent from source"; the serializer
    # must not silently rewrite it to null.
    empty_currency = event(currency_raw="")
    decoded = json.loads(canonical_payload_bytes(empty_currency).decode("utf-8"))
    assert decoded["currency_raw"] == ""
