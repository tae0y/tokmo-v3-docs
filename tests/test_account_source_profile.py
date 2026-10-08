"""AccountSourceProfileV1: quote routing ≠ transaction routing (Plan §17.4)."""

from datetime import datetime, timezone

import pytest

from tokmo.values.account_source_profile import (
    AccountSourceProfileV1,
    HoldingsSourceV1,
    QuoteSourceV1,
    TransactionSourceV1,
    derive_account_source_profiles,
    orders_scan_status,
    toss_orders_target_scopes,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)

TOSS_SCOPE = "toss-acct-" + "a" * 24
TOSS_SCOPE_2 = "toss-acct-" + "b" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24
CAPTURED = datetime(2026, 8, 10, 0, 0, tzinfo=timezone.utc)


def _item(symbol, market="KR"):
    return HoldingItemV1(symbol, "1", "100", "KRW", market, "market")


def _holdings(accounts):
    identity = {}
    built = []
    for ordinal, (scope, items, label) in enumerate(accounts, start=1):
        identity[scope] = AccountScopeIdentityV1(scope, label, ordinal, None)
        built.append(AccountHoldingsV1(scope, ordinal, tuple(items)))
    return CurrentHoldingsV1(1, tuple(built), True, CAPTURED, identity)


def _composite():
    return _holdings([
        (TOSS_SCOPE, [_item("005930"), _item("SPCX", "US")], "personal"),
        (PENSION_SCOPE, [_item("069500"), _item("0204D0")], "pension-excel-v1"),
    ])


def test_toss_scope_routes_quotes_and_orders_to_toss():
    profiles = derive_account_source_profiles(
        _composite(), native_ref_scopes=frozenset({TOSS_SCOPE}),
    )
    by_scope = {profile.account_scope: profile for profile in profiles}
    toss = by_scope[TOSS_SCOPE]
    assert toss.holdings_source is HoldingsSourceV1.TOSS
    assert toss.quote_source is QuoteSourceV1.TOSS_CURRENT_PRICE
    assert toss.transaction_source is TransactionSourceV1.TOSS_ORDERS
    assert toss.supports_live_orders is True
    assert toss.native_order_ref_present is True


def test_pension_scope_quotes_toss_but_transactions_stay_excel():
    # §2.4/§2.5: pension symbols remain Toss quote targets while the
    # transaction source stays the retirement Excel import path.
    profiles = derive_account_source_profiles(
        _composite(), native_ref_scopes=frozenset({TOSS_SCOPE}),
    )
    pension = {p.account_scope: p for p in profiles}[PENSION_SCOPE]
    assert pension.holdings_source is HoldingsSourceV1.PENSION_EXCEL
    assert pension.quote_source is QuoteSourceV1.TOSS_CURRENT_PRICE
    assert pension.transaction_source is TransactionSourceV1.RETIREMENT_EXCEL_IMPORT
    assert pension.supports_live_orders is False
    assert pension.native_order_ref_present is False


def test_orders_target_excludes_pension_and_refless_toss_scope():
    holdings = _holdings([
        (TOSS_SCOPE, [_item("005930")], "personal"),
        (TOSS_SCOPE_2, [_item("000660")], "personal"),
        (PENSION_SCOPE, [_item("069500")], "pension-excel-v1"),
    ])
    profiles = derive_account_source_profiles(
        holdings, native_ref_scopes=frozenset({TOSS_SCOPE}),
    )
    assert toss_orders_target_scopes(profiles) == (TOSS_SCOPE,)


def test_scan_status_partitions_not_applicable_from_unmapped():
    # §17.4: pension is NOT_APPLICABLE (never a failure); a Toss scope with
    # no native ref is the genuine fail-closed account_unmapped state.
    holdings = _holdings([
        (TOSS_SCOPE, [_item("005930")], "personal"),
        (TOSS_SCOPE_2, [_item("000660")], "personal"),
        (PENSION_SCOPE, [_item("069500")], "pension-excel-v1"),
    ])
    profiles = {
        p.account_scope: p for p in derive_account_source_profiles(
            holdings, native_ref_scopes=frozenset({TOSS_SCOPE}),
        )
    }
    assert orders_scan_status(profiles[PENSION_SCOPE]) == "NOT_APPLICABLE"
    assert orders_scan_status(profiles[TOSS_SCOPE_2]) == "ACCOUNT_UNMAPPED"
    assert orders_scan_status(profiles[TOSS_SCOPE]) == "SCAN_REQUIRED"


def test_profiles_cover_every_account_exactly_once_in_order():
    profiles = derive_account_source_profiles(
        _composite(), native_ref_scopes=frozenset({TOSS_SCOPE}),
    )
    assert [p.account_scope for p in profiles] == [TOSS_SCOPE, PENSION_SCOPE]


def test_pension_profile_construction_rejects_incoherent_routing():
    with pytest.raises(ValueError):
        AccountSourceProfileV1(
            PENSION_SCOPE, HoldingsSourceV1.PENSION_EXCEL,
            QuoteSourceV1.TOSS_CURRENT_PRICE,
            TransactionSourceV1.TOSS_ORDERS, False, False,
        )
    with pytest.raises(ValueError):
        AccountSourceProfileV1(
            PENSION_SCOPE, HoldingsSourceV1.PENSION_EXCEL,
            QuoteSourceV1.TOSS_CURRENT_PRICE,
            TransactionSourceV1.RETIREMENT_EXCEL_IMPORT, True, False,
        )
    with pytest.raises(ValueError):
        AccountSourceProfileV1(
            PENSION_SCOPE, HoldingsSourceV1.PENSION_EXCEL,
            QuoteSourceV1.TOSS_CURRENT_PRICE,
            TransactionSourceV1.RETIREMENT_EXCEL_IMPORT, False, True,
        )


def test_toss_profile_construction_rejects_excel_transaction_source():
    with pytest.raises(ValueError):
        AccountSourceProfileV1(
            TOSS_SCOPE, HoldingsSourceV1.TOSS,
            QuoteSourceV1.TOSS_CURRENT_PRICE,
            TransactionSourceV1.RETIREMENT_EXCEL_IMPORT, False, False,
        )


def test_live_orders_support_requires_native_ref_consistency():
    # supports_live_orders reflects the source capability (Toss), while the
    # ref-present flag reflects this snapshot; both feed the target gate.
    profile = AccountSourceProfileV1(
        TOSS_SCOPE, HoldingsSourceV1.TOSS, QuoteSourceV1.TOSS_CURRENT_PRICE,
        TransactionSourceV1.TOSS_ORDERS, True, False,
    )
    assert toss_orders_target_scopes((profile,)) == ()
    assert orders_scan_status(profile) == "ACCOUNT_UNMAPPED"


def test_invalid_scope_shape_is_rejected():
    with pytest.raises(ValueError):
        AccountSourceProfileV1(
            "not-a-scope", HoldingsSourceV1.TOSS,
            QuoteSourceV1.TOSS_CURRENT_PRICE,
            TransactionSourceV1.TOSS_ORDERS, True, True,
        )
