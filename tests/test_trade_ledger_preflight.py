"""Source-contract registry, key/sequence preflight, and closed exceptions.

The store never trusts the importer for sequence integrity: the versioned
per-source contract validates key structure before any classification.
These tests pin the retirement_excel_v1 full-history contract and the
registry's fail-closed behavior for unknown sources/scopes.
"""

import pytest

from tokmo.records.trade_ledger_preflight import (
    SOURCE_CONTRACTS,
    SourceContract,
    registered_contract,
    sequence_preflight_ok,
)
from tokmo.records.trade_ledger_privacy import (
    BatchConflictError,
    LedgerError,
    SourceContractViolationError,
    StoreCorruptError,
    StoreLockedError,
    StoreReadError,
)


RETIREMENT = SOURCE_CONTRACTS["retirement_excel_v1"]


def keys(*numbers):
    return tuple(f"row-{number:06d}" for number in numbers)


def test_registry_holds_frozen_retirement_and_toss_orders_contracts():
    # Additive period-export source; both legacy contracts remain frozen below.
    assert set(SOURCE_CONTRACTS) == {"retirement_excel_v1", "retirement_excel_delta_v1", "toss-orders-v1"}
    assert RETIREMENT.source == "retirement_excel_v1"
    assert RETIREMENT.contract_version == 1
    assert RETIREMENT.account_scopes == frozenset({"retirement_pension_1"})
    assert RETIREMENT.sequential_full_history is True
    assert RETIREMENT.allows_linked_supersession is False
    assert RETIREMENT.scope_validator is None
    toss = SOURCE_CONTRACTS["toss-orders-v1"]
    assert registered_contract(
        SOURCE_CONTRACTS, toss.source, "toss-acct-" + "a" * 24
    ) is toss
    assert registered_contract(SOURCE_CONTRACTS, toss.source, "raw-account") is None


def test_registered_contract_fails_closed_on_unknown_source_or_scope():
    assert registered_contract(
        SOURCE_CONTRACTS, "retirement_excel_v1", "retirement_pension_1"
    ) is RETIREMENT
    assert registered_contract(
        SOURCE_CONTRACTS, "fixture_unregistered_source", "retirement_pension_1"
    ) is None
    assert registered_contract(
        SOURCE_CONTRACTS, "retirement_excel_v1", "fixture_other_scope"
    ) is None


@pytest.mark.parametrize(
    "key,valid",
    [
        ("row-000001", True),
        ("row-000085", True),
        ("row-999999", True),
        ("row-000000", False),
        ("row-1", False),
        ("row-0000001", False),
        ("abc", False),
        ("ROW-000001", False),
        ("row-00001a", False),
        ("", False),
    ],
)
def test_retirement_key_format_is_row_dash_six_digits_at_least_one(key, valid):
    assert (RETIREMENT.key_number(key) is not None) is valid


def test_sequence_preflight_requires_contiguity_from_one():
    assert sequence_preflight_ok(RETIREMENT, keys(1))
    assert sequence_preflight_ok(RETIREMENT, keys(1, 2, 3))
    assert sequence_preflight_ok(RETIREMENT, keys(3, 1, 2))  # order-independent
    assert not sequence_preflight_ok(RETIREMENT, keys(2))
    assert not sequence_preflight_ok(RETIREMENT, keys(50))
    assert not sequence_preflight_ok(RETIREMENT, keys(1, 3))
    assert not sequence_preflight_ok(RETIREMENT, keys(1, 2, 4, 5))
    assert not sequence_preflight_ok(RETIREMENT, ())
    assert not sequence_preflight_ok(RETIREMENT, ("row-000001", "abc"))


def test_non_sequential_contract_checks_key_format_only():
    fixture = SourceContract(
        source="fixture_future_source_1",
        contract_version=1,
        account_scopes=frozenset({"fixture_scope_1"}),
        key_pattern=r"fix-[a-z0-9]{4}",
        sequential_full_history=False,
        allows_linked_supersession=True,
    )
    assert sequence_preflight_ok(fixture, ("fix-a1b2", "fix-zz99"))
    assert not sequence_preflight_ok(fixture, ("fix-a1b2", "row-000001"))
    assert not sequence_preflight_ok(fixture, ())
    assert fixture.key_number("fix-a1b2") is None


def test_ledger_errors_carry_closed_reasons_and_redacted_text_only():
    for cls in (
        LedgerError,
        SourceContractViolationError,
        BatchConflictError,
        StoreCorruptError,
        StoreLockedError,
        StoreReadError,
    ):
        error = cls("store_corrupt")
        assert error.reason == "store_corrupt"
        assert str(error) == repr(error)
        assert error.reason in repr(error)
    coerced = LedgerError("/tmp/fixture/private.db contains 12.5 shares")
    assert coerced.reason == "invalid_batch"
    assert "fixture" not in repr(coerced)
    assert "12.5" not in str(coerced)
