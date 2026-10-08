"""Toss holdings parser TDD: evidence-tier field names only, required
quantity/average price, verbatim preservation, and fail-closed envelopes."""

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.facts.toss import (
    AVG_PRICE_FIELDS,
    MARKET_FIELDS,
    SOURCE,
    TossAccessToken,
    TossAccountList,
    parse_accounts_response,
    parse_holdings_response,
    parse_token_response,
)
from tokmo.values import TOSS_SOURCE, TossAccountHoldingsFact, TossHoldingFact, UnavailableFact

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 7, 27, 9, 0, 0, tzinfo=timezone.utc)


def body(name):
    return (FIXTURES / f"toss_{name}.json").read_bytes()


def holdings(name="holdings_success", ordinal=1, count=1):
    return parse_holdings_response(
        body(name), account_ordinal=ordinal, account_count=count, captured_at=NOW)


# --- token -------------------------------------------------------------------

def test_token_response_parses_the_three_evidenced_fields():
    token = parse_token_response(body("token_success"), observed_at=NOW)

    assert isinstance(token, TossAccessToken)
    assert token.value == "SYNTHETIC-ACCESS-0001"
    assert token.expires_in == 3600
    assert token.token_type == "Bearer"


def test_token_value_is_absent_from_repr():
    token = parse_token_response(body("token_success"), observed_at=NOW)

    assert "SYNTHETIC-ACCESS-0001" not in repr(token)
    assert "3600" in repr(token)


@pytest.mark.parametrize("payload", [
    {"expires_in": 3600, "token_type": "Bearer"},
    {"access_token": "", "expires_in": 3600, "token_type": "Bearer"},
    {"access_token": "SYNTHETIC-A", "token_type": "Bearer"},
    {"access_token": "SYNTHETIC-A", "expires_in": 0, "token_type": "Bearer"},
    {"access_token": "SYNTHETIC-A", "expires_in": -1, "token_type": "Bearer"},
    {"access_token": "SYNTHETIC-A", "expires_in": True, "token_type": "Bearer"},
    {"access_token": "SYNTHETIC-A", "expires_in": 3600, "token_type": "Mac"},
    {"accessToken": "SYNTHETIC-A", "expires_in": 3600},
    [],
])
def test_token_response_without_the_evidenced_contract_is_unavailable(payload):
    result = parse_token_response(json.dumps(payload).encode(), observed_at=NOW)

    assert isinstance(result, UnavailableFact)
    assert result.reason == "contract_mismatch"


def test_token_response_that_is_not_json_is_a_parse_error():
    result = parse_token_response(b"<html>", observed_at=NOW)

    assert isinstance(result, UnavailableFact) and result.reason == "parse_error"


# --- accounts ----------------------------------------------------------------

def test_accounts_response_yields_provider_order_account_sequences():
    accounts = parse_accounts_response(body("accounts_success"), observed_at=NOW)

    assert isinstance(accounts, TossAccountList)
    assert accounts.seqs == ("SYNTHETIC-01", "SYNTHETIC-02")


def test_account_sequences_never_appear_in_repr():
    accounts = parse_accounts_response(body("accounts_success"), observed_at=NOW)

    assert "SYNTHETIC-01" not in repr(accounts)
    assert "count=2" in repr(accounts)


def test_numeric_account_sequence_is_canonicalised_to_text():
    accounts = parse_accounts_response(b'{"result":[{"accountSeq":12345}]}', observed_at=NOW)

    assert accounts.seqs == ("12345",)


def test_empty_accounts_result_is_an_empty_list_not_a_parser_failure():
    accounts = parse_accounts_response(b'{"result":[]}', observed_at=NOW)

    assert isinstance(accounts, TossAccountList) and accounts.seqs == ()


@pytest.mark.parametrize("raw", [
    b'{"result":{}}',
    b'{"result":[{"accountNumber":"1"}]}',
    b'{"result":[{"accountSeq":""}]}',
    b'{"result":[{"accountSeq":null}]}',
    b'{"result":[{"accountSeq":true}]}',
    b'{"result":["SYNTHETIC-01"]}',
    b'{"items":[]}',
])
def test_accounts_envelope_outside_the_evidence_is_unavailable(raw):
    result = parse_accounts_response(raw, observed_at=NOW)

    assert isinstance(result, UnavailableFact) and result.reason == "contract_mismatch"


# --- holdings ----------------------------------------------------------------

def test_holdings_response_builds_one_account_fact_in_response_order():
    fact = holdings(ordinal=2, count=3)

    assert isinstance(fact, TossAccountHoldingsFact)
    assert (fact.source, fact.captured_at) == (TOSS_SOURCE, NOW)
    assert (fact.account_ordinal, fact.account_count) == (2, 3)
    assert len(fact.holdings) == 2
    assert fact.source_ref == f"{TOSS_SOURCE}:{NOW.isoformat()}:2"


def test_holdings_preserve_provider_values_in_canonical_decimal_form():
    first, second = holdings().holdings

    assert isinstance(first, TossHoldingFact)
    assert (first.symbol, first.quantity, first.avg_price) == ("SYN001", "10", "71000")
    assert first.avg_price_field == "averagePurchasePrice"
    assert (second.quantity, second.avg_price) == ("2.5", "120.75")


def test_holdings_record_the_evidenced_optional_field_names_verbatim():
    first = holdings().holdings[0]

    assert (first.currency, first.market_raw, first.market_field) == ("KRW", "KR", "market")
    assert (first.name, first.asset_type) == ("SYNTHETIC-EQUITY-A", "equity")


def test_holdings_accept_each_evidenced_fallback_field_name():
    fallback = holdings("holdings_fallback_fields").holdings[0]
    minimal = holdings("holdings_minimal").holdings[0]

    assert (fallback.avg_price_field, fallback.avg_price) == ("averagePrice", "1000.5")
    assert (fallback.quantity, fallback.market_field) == ("3", "marketCountry")
    assert (fallback.market_raw, fallback.name) == ("KR", "SYNTHETIC-EQUITY-C")
    assert (minimal.avg_price_field, minimal.avg_price) == ("avgPrice", "9.9")


def test_absent_optional_fields_stay_none_and_market_is_never_inferred():
    minimal = holdings("holdings_minimal").holdings[0]

    assert minimal.currency is None
    assert minimal.market_raw is None and minimal.market_field is None
    assert minimal.name is None and minimal.asset_type is None


def test_market_is_not_inferred_from_currency():
    fact = holdings_from({"symbol": "SYN", "quantity": "1", "avgPrice": "1",
                          "currency": "KRW"})

    assert fact.holdings[0].market_raw is None


def test_holding_values_are_absent_from_repr():
    text = repr(holdings())

    for value in ("SYN001", "71000", "2.5", "SYNTHETIC-EQUITY-A"):
        assert value not in text
    assert "averagePurchasePrice" in text


def test_unknown_extra_provider_keys_are_ignored_not_parsed():
    fact = holdings_from({"symbol": "SYN", "quantity": "1", "avgPrice": "2",
                          "evaluationAmount": "999", "profitLoss": "-1"})

    assert fact.holdings[0].avg_price == "2"


def holdings_from(item, *, ordinal=1, count=1):
    raw = json.dumps({"result": {"items": [item]}}).encode()
    return parse_holdings_response(raw, account_ordinal=ordinal,
                                   account_count=count, captured_at=NOW)


@pytest.mark.parametrize("item", [
    {"quantity": "1", "avgPrice": "2"},
    {"symbol": "", "quantity": "1", "avgPrice": "2"},
    {"symbol": "SYN", "avgPrice": "2"},
    {"symbol": "SYN", "quantity": "1"},
    {"symbol": "SYN", "quantity": "1", "unitCost": "2"},
    {"symbol": "SYN", "quantity": "abc", "avgPrice": "2"},
    {"symbol": "SYN", "quantity": "1", "avgPrice": "NaN"},
    {"symbol": "SYN", "quantity": "-1", "avgPrice": "2"},
    {"symbol": "SYN", "quantity": None, "avgPrice": "2"},
    {"symbol": "SYN", "quantity": True, "avgPrice": "2"},
    {"symbol": "SYN", "quantity": "1", "avgPrice": "2", "currency": 840},
    {"symbol": "SYN", "quantity": "1", "avgPrice": "2", "market": 1},
])
def test_missing_or_unevidenced_required_fields_fail_the_whole_response(item):
    result = holdings_from(item)

    assert isinstance(result, UnavailableFact) and result.reason == "contract_mismatch"


def test_one_bad_item_invalidates_every_item_of_that_account():
    raw = json.dumps({"result": {"items": [
        {"symbol": "SYN", "quantity": "1", "avgPrice": "2"},
        {"symbol": "BAD", "quantity": "1"},
    ]}}).encode()

    result = parse_holdings_response(raw, account_ordinal=1, account_count=1,
                                     captured_at=NOW)

    assert isinstance(result, UnavailableFact) and result.reason == "contract_mismatch"


@pytest.mark.parametrize("raw", [
    b'{"result":[]}',
    b'{"result":{"holdings":[]}}',
    b'{"items":[]}',
    b'{"result":{"items":{}}}',
    b'{"result":{"items":["SYN"]}}',
])
def test_holdings_envelope_outside_the_evidence_is_unavailable(raw):
    result = parse_holdings_response(raw, account_ordinal=1, account_count=1,
                                     captured_at=NOW)

    assert isinstance(result, UnavailableFact) and result.reason == "contract_mismatch"


def test_an_account_with_no_holdings_is_a_valid_empty_snapshot():
    fact = parse_holdings_response(b'{"result":{"items":[]}}', account_ordinal=1,
                                   account_count=1, captured_at=NOW)

    assert isinstance(fact, TossAccountHoldingsFact) and fact.holdings == ()


def test_holdings_body_that_is_not_json_is_a_parse_error():
    result = parse_holdings_response(b"not json", account_ordinal=1,
                                     account_count=1, captured_at=NOW)

    assert isinstance(result, UnavailableFact) and result.reason == "parse_error"


def test_candidate_field_names_are_exactly_the_evidence_tier_names():
    assert AVG_PRICE_FIELDS == ("averagePurchasePrice", "averagePrice", "avgPrice")
    assert MARKET_FIELDS == ("market", "marketCountry")
    assert SOURCE == TOSS_SOURCE
