"""Slice 4 — pure KR membership facts: cohort predicate, plan, admission.

Every symbol, ISIN, and the ETF security-type token here is synthetic /
RECONSTRUCTED_CONTRACT only (Plan/SDS §13). The production-pinned vocabulary
stays fail-closed on unpinned tokens (SDS_GAP-1/2); tests that admit ETFs
inject a clearly reconstructed vocabulary.
"""

from datetime import date, datetime, timezone
from types import MappingProxyType

import pytest


NOW = datetime(2026, 8, 21, 21, 10, tzinfo=timezone.utc)
SESSION = date(2026, 8, 21)
TOSS_SCOPE = "toss-acct-" + "a" * 24
PENSION_SCOPE = "pension-excel-" + "b" * 24


def _holdings(rows, *, captured_at=NOW):
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )

    accounts = []
    identity_map = {}
    for ordinal, (scope, items) in enumerate(rows, start=1):
        accounts.append(AccountHoldingsV1(scope, ordinal, tuple(
            HoldingItemV1(symbol, "1", "100", currency, market_raw, None)
            for symbol, market_raw, currency in items
        )))
        identity_map[scope] = AccountScopeIdentityV1(scope, "brokerage", ordinal, None)
    return CurrentHoldingsV1(1, tuple(accounts), True, captured_at, identity_map)


def _reconstructed_vocabulary():
    from tokmo.facts.quant_kr_membership import KrMembershipVocabularyV1
    from tokmo.values.quant_multifactor_core import MarketV1

    return KrMembershipVocabularyV1(
        active_status_tokens=frozenset({"ACTIVE"}),
        stock_security_type_token="STOCK",
        etf_security_type_tokens=frozenset({"RECONSTRUCTED_ETF"}),
        market_tokens=MappingProxyType({
            "KOSPI": MarketV1.KR_KOSPI,
            "RECONSTRUCTED_KOSDAQ": MarketV1.KR_KOSDAQ,
        }),
    )


def _stock_info(symbol, **overrides):
    from tokmo.values.toss_reference import KrMarketDetail, StockInfo

    fields = {
        "symbol": symbol,
        "name": symbol,
        "english_name": symbol,
        "isin_code": f"KR7{symbol}00000"[:12].ljust(12, "0"),
        "market": "KOSPI",
        "security_type": "STOCK",
        "is_common_share": True,
        "status": "ACTIVE",
        "currency": "KRW",
        "shares_outstanding": "1000",
        "list_date": date(2020, 1, 2),
        "delist_date": None,
        "korean_market_detail": KrMarketDetail(
            liquidation_trading=False,
            nxt_supported=True,
            krx_trading_suspended=False,
        ),
    }
    fields.update(overrides)
    return StockInfo(**fields)


def _five_member_infos():
    infos = [
        _stock_info("RCS001"),
        _stock_info("RCS002"),
    ]
    for index in range(1, 4):
        infos.append(_stock_info(
            f"RCE00{index}",
            security_type="RECONSTRUCTED_ETF",
            is_common_share=False,
        ))
    return tuple(infos)


def test_kr_cohort_predicate_includes_personal_kr_and_excludes_pension_and_us():
    from tokmo.facts.quant_kr_membership import predict_kr_personal_cohort

    holdings = _holdings((
        (TOSS_SCOPE, (
            ("RCS002", "KR", "KRW"),
            ("RCS001", "KR", "KRW"),
            ("RCUS01", "US", "USD"),
        )),
        (PENSION_SCOPE, (
            ("RCP001", "KR", "KRW"),
        )),
    ))
    assert predict_kr_personal_cohort(holdings) == ("RCS001", "RCS002")
    assert predict_kr_personal_cohort(None) == ()


def test_kr_membership_plan_is_exactly_one_stocks_start_and_bounded():
    from tokmo.facts.quant_kr_membership import kr_membership_operation_plan

    plan = kr_membership_operation_plan(
        ("RCS002", "RCS001", "RCE001", "RCE002", "RCE003"), SESSION,
    )
    assert plan == (
        ("MEMBERSHIP_TOSS_STOCKS", "membership:toss-stocks:2026-08-21"),
    )
    with pytest.raises(ValueError):  # 6-member cohort refused pre-wire
        kr_membership_operation_plan(
            tuple(f"RC000{index}" for index in range(1, 7)), SESSION,
        )
    with pytest.raises(ValueError):
        kr_membership_operation_plan((), SESSION)
    with pytest.raises(ValueError):  # duplicates refused
        kr_membership_operation_plan(("RCS001", "RCS001"), SESSION)


def test_five_member_synthetic_cohort_admits_under_reconstructed_vocabulary():
    from tokmo.facts.quant_kr_membership import admit_kr_membership
    from tokmo.values.quant_multifactor_core import MarketV1

    infos = _five_member_infos()
    expected = tuple(sorted(info.symbol for info in infos))
    result = admit_kr_membership(
        infos,
        expected_symbols=expected,
        vocabulary=_reconstructed_vocabulary(),
    )
    assert result.refusals == ()
    assert tuple(member.symbol for member in result.admitted) == expected
    assert all(
        member.market is MarketV1.KR_KOSPI for member in result.admitted
    )
    stock_members = [m for m in result.admitted if m.security_type == "STOCK"]
    etf_members = [
        m for m in result.admitted if m.security_type == "RECONSTRUCTED_ETF"
    ]
    assert (len(stock_members), len(etf_members)) == (2, 3)


def test_production_vocabulary_fails_closed_on_unpinned_etf_token():
    from tokmo.facts.quant_kr_membership import (
        KR_MEMBERSHIP_VOCABULARY_V1,
        admit_kr_membership,
    )

    infos = _five_member_infos()
    expected = tuple(sorted(info.symbol for info in infos))
    result = admit_kr_membership(infos, expected_symbols=expected)
    # SDS_GAP-1: the ETF security-type token is unpinned in production, so
    # the three ETF members refuse fail-closed; nothing is guessed.
    assert KR_MEMBERSHIP_VOCABULARY_V1.etf_security_type_tokens == frozenset()
    refused = dict(result.refusals)
    assert set(refused) == {"RCE001", "RCE002", "RCE003"}
    assert set(refused.values()) == {"security_type_unknown"}
    assert tuple(m.symbol for m in result.admitted) == ("RCS001", "RCS002")


def test_each_admission_gate_refuses_fail_closed():
    from tokmo.facts.quant_kr_membership import admit_kr_membership
    from tokmo.values._primitives import MISSING
    from tokmo.values.toss_reference import KrMarketDetail

    vocabulary = _reconstructed_vocabulary()
    cases = {
        "delisted": _stock_info("RCX001", delist_date=date(2026, 1, 2)),
        "krx_trading_suspended": _stock_info(
            "RCX001",
            korean_market_detail=KrMarketDetail(
                liquidation_trading=False,
                nxt_supported=True,
                krx_trading_suspended=True,
            ),
        ),
        "currency_not_krw": _stock_info("RCX001", currency="USD"),
        "isin_not_kr": _stock_info("RCX001", isin_code="US0000RCX001"),
        "market_token_unknown": _stock_info("RCX001", market="NEW_MARKET"),
        "security_type_unknown": _stock_info("RCX001", security_type="NEW_TYPE"),
        "status_not_active": _stock_info("RCX001", status="NEW_STATUS"),
        "not_common_share": _stock_info("RCX001", is_common_share=False),
        "market_detail_missing": _stock_info(
            "RCX001", korean_market_detail=MISSING,
        ),
    }
    for expected_reason, info in cases.items():
        result = admit_kr_membership(
            (info,), expected_symbols=("RCX001",), vocabulary=vocabulary,
        )
        assert result.admitted == ()
        assert result.refusals == (("RCX001", expected_reason),), expected_reason


def test_cohort_echo_mismatch_is_refused():
    from tokmo.facts.quant_kr_membership import admit_kr_membership

    infos = (_stock_info("RCS001"),)
    with pytest.raises(ValueError, match="cohort_echo"):
        admit_kr_membership(
            infos,
            expected_symbols=("RCS001", "RCS002"),
            vocabulary=_reconstructed_vocabulary(),
        )
    with pytest.raises(ValueError, match="cohort_echo"):
        admit_kr_membership(
            (_stock_info("RCS001"), _stock_info("RCS001")),
            expected_symbols=("RCS001",),
            vocabulary=_reconstructed_vocabulary(),
        )


def test_provenance_digest_is_deterministic_over_typed_fields_only():
    from tokmo.facts.quant_kr_membership import (
        admit_kr_membership,
        kr_provenance_digest,
    )

    vocabulary = _reconstructed_vocabulary()
    infos = _five_member_infos()
    expected = tuple(sorted(info.symbol for info in infos))
    admitted = admit_kr_membership(
        infos, expected_symbols=expected, vocabulary=vocabulary,
    ).admitted
    digest = kr_provenance_digest(
        members=admitted,
        request_identity="membership:toss-stocks:2026-08-21",
        observed_at=NOW,
    )
    assert len(digest) == 64
    assert digest == kr_provenance_digest(
        members=admitted,
        request_identity="membership:toss-stocks:2026-08-21",
        observed_at=NOW,
    )
    assert digest != kr_provenance_digest(
        members=admitted,
        request_identity="membership:toss-stocks:2026-08-20",
        observed_at=NOW,
    )
    assert digest != kr_provenance_digest(
        members=admitted[:4],
        request_identity="membership:toss-stocks:2026-08-21",
        observed_at=NOW,
    )


def test_admitted_members_bridge_to_material_member_records():
    from tokmo.facts.quant_kr_membership import (
        admit_kr_membership,
        kr_material_members,
    )
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        encode_membership_material,
    )

    vocabulary = _reconstructed_vocabulary()
    infos = _five_member_infos()
    expected = tuple(sorted(info.symbol for info in infos))
    admitted = admit_kr_membership(
        infos, expected_symbols=expected, vocabulary=vocabulary,
    ).admitted
    members = kr_material_members(admitted)
    assert [row["symbol"] for row in members] == list(expected)
    raw = encode_membership_material(
        profile_id=KR_PROFILE_ID,
        policy_version="cadence-300s-quant-profiles-shadow-v1",
        completed_session_date=SESSION,
        request_identity="membership:toss-stocks:2026-08-21",
        observed_at=NOW,
        members=members,
    )
    assert raw  # bounded canonical bytes, no prose
