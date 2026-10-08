import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from tokmo.facts.toss_prices import parse_prices_response
from tokmo.values import TOSS_PRICE_SOURCE, TossCurrentPriceFact


FIXTURE = Path(__file__).parent / "fixtures" / "toss_prices_openapi_excerpt.json"
FETCHED_AT = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)


def encoded(items):
    return json.dumps({"result": items}).encode()


def test_official_kr_us_examples_parse_and_canonicalize_decimal():
    examples = json.loads(FIXTURE.read_text())["examples"]

    outcome = parse_prices_response(
        encoded([examples["us"], examples["kr"]]),
        requested_symbols=("005930", "AAPL"),
        fetched_at=FETCHED_AT,
    )

    assert outcome.code == "success"
    assert outcome.facts == (
        TossCurrentPriceFact(
            source=TOSS_PRICE_SOURCE,
            symbol="AAPL",
            last_price="185.7",
            currency_raw="USD",
            timestamp=datetime(
                2026, 3, 25, 22, 30, 0, 456000,
                tzinfo=timezone(timedelta(hours=9))),
            fetched_at=FETCHED_AT,
        ),
        TossCurrentPriceFact(
            source=TOSS_PRICE_SOURCE,
            symbol="005930",
            last_price="72000",
            currency_raw="KRW",
            timestamp=datetime(
                2026, 3, 25, 9, 30, 0, 123000,
                tzinfo=timezone(timedelta(hours=9))),
            fetched_at=FETCHED_AT,
        ),
    )
    rendered = repr(outcome) + repr(outcome.facts[0])
    assert "185.7" not in rendered
    assert "72000" not in rendered


def test_null_timestamp_and_unknown_nonempty_currency_are_successful():
    outcome = parse_prices_response(
        encoded([{
            "symbol": "Future.X",
            "lastPrice": "0.0000",
            "currency": "SYNTH",
            "timestamp": None,
        }]),
        requested_symbols=("Future.X",),
        fetched_at=FETCHED_AT,
    )

    assert outcome.code == "success"
    assert outcome.facts[0].timestamp is None
    assert outcome.facts[0].currency_raw == "SYNTH"
    assert outcome.facts[0].last_price == "0"


def test_response_symbol_set_is_exact_unique_and_case_sensitive():
    for requested, returned in [
        (("Abc",), ["ABC"]),
        (("Abc", "XYZ"), ["Abc"]),
        (("Abc",), ["Abc", "XYZ"]),
        (("Abc",), ["Abc", "Abc"]),
    ]:
        outcome = parse_prices_response(
            encoded([{
                "symbol": symbol,
                "lastPrice": "1",
                "currency": "USD",
                "timestamp": None,
            } for symbol in returned]),
            requested_symbols=requested,
            fetched_at=FETCHED_AT,
        )
        assert outcome.code == "contract_mismatch"
        assert outcome.facts == ()


def test_malformed_items_fail_all_or_nothing_without_value_leakage():
    invalid_values = [
        -1, "-0.01", "", "NaN", "Infinity", True,
    ]
    for invalid in invalid_values:
        outcome = parse_prices_response(
            encoded([
                {"symbol": "OK", "lastPrice": "2", "currency": "USD",
                 "timestamp": None},
                {"symbol": "BAD", "lastPrice": invalid, "currency": "USD",
                 "timestamp": None},
            ]),
            requested_symbols=("OK", "BAD"),
            fetched_at=FETCHED_AT,
        )
        assert outcome.code == "contract_mismatch"
        assert outcome.facts == ()
        if str(invalid):
            assert str(invalid) not in repr(outcome)


def test_naive_or_non_string_timestamp_and_invalid_fetched_at_fail_closed():
    item = {"symbol": "ABC", "lastPrice": "1", "currency": "USD"}
    for timestamp in ["2026-03-25T09:30:00", 1770000000, True, "bad"]:
        outcome = parse_prices_response(
            encoded([{**item, "timestamp": timestamp}]),
            requested_symbols=("ABC",), fetched_at=FETCHED_AT)
        assert outcome.code == "contract_mismatch"
        assert outcome.facts == ()

    for fetched_at in [
        datetime(2026, 7, 28, 3, 0),
        "2026-07-28T03:00:00+00:00",
    ]:
        outcome = parse_prices_response(
            encoded([{**item, "timestamp": None}]),
            requested_symbols=("ABC",), fetched_at=fetched_at)
        assert outcome.code == "contract_mismatch"
        assert outcome.facts == ()


def test_malformed_json_envelope_and_required_fields_fail_closed():
    bodies = [
        b"not-json",
        b"[]",
        b"{}",
        b'{"result":{}}',
        encoded([{"symbol": "ABC", "lastPrice": "1", "timestamp": None}]),
        encoded([{"symbol": "ABC", "currency": "USD", "timestamp": None}]),
        encoded([{"lastPrice": "1", "currency": "USD", "timestamp": None}]),
    ]
    for body in bodies:
        outcome = parse_prices_response(
            body, requested_symbols=("ABC",), fetched_at=FETCHED_AT)
        assert outcome.code in {"parse_error", "contract_mismatch"}
        assert outcome.facts == ()


def test_price_respects_official_thirty_character_bound():
    outcome = parse_prices_response(
        encoded([{
            "symbol": "ABC", "lastPrice": "1" * 31,
            "currency": "USD", "timestamp": None,
        }]),
        requested_symbols=("ABC",), fetched_at=FETCHED_AT)

    assert outcome.code == "contract_mismatch"
    assert outcome.facts == ()


def test_price_rejects_non_plain_decimal_without_expanding_exponent():
    for invalid in [
        "1E+9999999999999999", "1e5", "1_0", " 1 ", "+1", ".1", "1.",
    ]:
        outcome = parse_prices_response(
            encoded([{
                "symbol": "ABC", "lastPrice": invalid,
                "currency": "USD", "timestamp": None,
            }]),
            requested_symbols=("ABC",), fetched_at=FETCHED_AT)

        assert outcome.code == "contract_mismatch"
        assert outcome.facts == ()
