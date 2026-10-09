"""Stage 2-A: immutable OpenMarketMaskV1 value, digest, and holdings projection.

The mask is the one tick-bound session authority (SDS §6): it is built exactly
once after independent calendar refresh/session judgment, and every downstream
stage binds to its exact identity/digest instead of reinterpreting verdicts.
"""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import MappingProxyType

import pytest

from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.open_market_mask import (
    MaskBindingError,
    MaskMarketEntryV1,
    OpenMarketMaskV1,
    build_open_market_mask,
    project_open_market_holdings,
    require_mask_binding,
)
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession

POLICY_VERSION = "cadence-300s-v1"
SLOT = datetime(2026, 8, 3, 0, 30, tzinfo=UTC)
TICK_ID = 5_952_390  # int(SLOT.timestamp()) // 300 — canonical derivation (B1)
KR_DATE = date(2026, 8, 3)
US_DATE = date(2026, 8, 2)

KR_SESSION = CalendarSession(
    datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
    datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
)
US_SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=UTC),
    datetime(2026, 8, 3, 20, 0, tzinfo=UTC),
)


def verdicts(kr_reason="open", us_reason="before_open"):
    return (
        SessionVerdict(
            Market.KR,
            kr_reason == "open",
            kr_reason,
            KR_SESSION if kr_reason in {"open", "before_open", "after_close"} else None,
        ),
        SessionVerdict(
            Market.US,
            us_reason == "open",
            us_reason,
            US_SESSION if us_reason in {"open", "before_open", "after_close"} else None,
        ),
    )


def build(
    kr_reason="open",
    us_reason="before_open",
    *,
    tick_id=TICK_ID,
    policy_version=POLICY_VERSION,
    slot=SLOT,
    kr_date=KR_DATE,
    us_date=US_DATE,
):
    return build_open_market_mask(
        policy_version=policy_version,
        scheduled_for_utc=slot,
        tick_id=tick_id,
        verdicts=verdicts(kr_reason, us_reason),
        local_dates={Market.KR: kr_date, Market.US: us_date},
    )


def holdings_state():
    scope_a = "toss-acct-" + "a" * 24
    scope_b = "toss-acct-" + "b" * 24
    return CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope_a,
                1,
                (
                    HoldingItemV1("005930", "3", "70000", "KRW", "KR", None),
                    HoldingItemV1("AAPL", "2", "180.5", "USD", "US", None),
                ),
            ),
            AccountHoldingsV1(
                scope_b,
                2,
                (
                    HoldingItemV1("MYSTERY", "1", "10", None, None, None),
                    HoldingItemV1("TSLA", "4", "300", "USD", "US", None),
                ),
            ),
        ),
        True,
        datetime(2026, 8, 3, 0, 29, tzinfo=UTC),
        {
            scope_a: AccountScopeIdentityV1(scope_a, "general", 1, None),
            scope_b: AccountScopeIdentityV1(scope_b, "isa", 2, None),
        },
    )


# -- construction / closed enum -------------------------------------------------


def test_mask_builds_open_subset_and_entry_identities():
    mask = build("open", "before_open")
    assert mask.markets == frozenset({Market.KR})
    assert mask.policy_version == POLICY_VERSION
    assert mask.scheduled_for_utc == SLOT
    assert mask.tick_id == TICK_ID
    assert set(mask.entries) == {Market.KR, Market.US}
    assert mask.entries[Market.KR].local_date == KR_DATE
    assert mask.entries[Market.US].local_date == US_DATE
    assert mask.entries[Market.US].reason == "before_open"
    assert len(mask.digest) == 64


def test_mask_both_closed_is_empty_and_still_digested():
    mask = build("after_close", "no_regular")
    assert mask.markets == frozenset()
    assert len(mask.digest) == 64


def test_mask_rejects_unknown_session_reason():
    with pytest.raises(ValueError):
        MaskMarketEntryV1(Market.KR, KR_DATE, "totally_new_reason", None, None)


def test_mask_open_entry_requires_aware_session_boundaries():
    with pytest.raises(ValueError):
        MaskMarketEntryV1(Market.KR, KR_DATE, "open", None, None)
    with pytest.raises(ValueError):
        MaskMarketEntryV1(
            Market.KR,
            KR_DATE,
            "open",
            datetime(2026, 8, 3, 0, 0),
            datetime(2026, 8, 3, 6, 30),
        )
    with pytest.raises(ValueError):
        MaskMarketEntryV1(
            Market.KR,
            KR_DATE,
            "open",
            KR_SESSION.end_time,
            KR_SESSION.start_time,
        )


def test_mask_markets_must_match_open_entries_exactly():
    mask = build("open", "before_open")
    with pytest.raises(ValueError):
        OpenMarketMaskV1(
            mask.policy_version,
            mask.scheduled_for_utc,
            mask.tick_id,
            frozenset({Market.KR, Market.US}),
            dict(mask.entries),
            mask.digest,
        )


def test_mask_requires_entries_for_both_markets():
    mask = build()
    with pytest.raises(ValueError):
        OpenMarketMaskV1(
            mask.policy_version,
            mask.scheduled_for_utc,
            mask.tick_id,
            frozenset({Market.KR}),
            {Market.KR: mask.entries[Market.KR]},
            mask.digest,
        )


def test_mask_digest_is_self_verifying_and_unforgeable():
    mask = build()
    with pytest.raises(ValueError):
        replace(mask, digest="0" * 64)
    with pytest.raises(ValueError):
        replace(mask, tick_id=mask.tick_id + 1)  # digest no longer matches


def test_mask_is_immutable():
    mask = build()
    with pytest.raises(Exception):
        mask.markets = frozenset()
    assert isinstance(mask.entries, MappingProxyType)
    with pytest.raises(TypeError):
        mask.entries[Market.KR] = None
    assert type(mask.markets) is frozenset


def test_mask_rejects_naive_slot_and_bad_tick():
    with pytest.raises(ValueError):
        build(slot=datetime(2026, 8, 3, 0, 30))
    with pytest.raises(ValueError):
        build(tick_id=-1)
    with pytest.raises(ValueError):
        build(policy_version="")


# -- digest sensitivity ----------------------------------------------------------


def test_same_policy_tick_different_open_set_changes_digest():
    assert build("open", "before_open").digest != build("open", "open").digest


def test_same_open_set_different_local_date_changes_digest():
    base = build("open", "before_open")
    shifted = build("open", "before_open", us_date=date(2026, 8, 3))
    assert base.markets == shifted.markets
    assert base.digest != shifted.digest


def test_same_open_set_different_session_reason_changes_digest():
    base = build("open", "before_open")
    other = build("open", "after_close")
    assert base.markets == other.markets
    assert base.digest != other.digest


def test_policy_version_slot_and_tick_bind_the_digest():
    base = build()
    # The only other constructible identity is another canonical slot; its
    # digest differs. Foreign policy versions and caller-chosen tick ids are
    # rejected at construction (B1), so no forged same-tick digest can exist.
    shifted = build(
        slot=SLOT + timedelta(seconds=300),
        tick_id=TICK_ID + 1,
    )
    assert base.digest != shifted.digest
    with pytest.raises(ValueError):
        build(policy_version="cadence-60s-v2")
    with pytest.raises(ValueError):
        build(tick_id=TICK_ID + 1)
    with pytest.raises(ValueError):
        build(slot=datetime(2026, 8, 3, 0, 35, tzinfo=UTC))


# -- binding requirement (fail closed) -------------------------------------------


def test_require_mask_binding_rejects_missing_mask():
    with pytest.raises(MaskBindingError):
        require_mask_binding(None)
    with pytest.raises(MaskBindingError):
        require_mask_binding(object())


def test_require_mask_binding_rejects_mismatched_identity():
    mask = build()
    assert require_mask_binding(mask, tick_id=TICK_ID) is mask
    with pytest.raises(MaskBindingError):
        require_mask_binding(mask, tick_id=TICK_ID + 1)
    with pytest.raises(MaskBindingError):
        require_mask_binding(mask, policy_version="cadence-60s-v2")
    with pytest.raises(MaskBindingError):
        require_mask_binding(
            mask, scheduled_for_utc=datetime(2026, 8, 3, 0, 35, tzinfo=UTC)
        )
    with pytest.raises(MaskBindingError):
        require_mask_binding(mask, digest="0" * 64)


# -- holdings projection ----------------------------------------------------------


def test_projection_us_only_keeps_full_scopes_but_us_subjects_only():
    mask = build("before_open", "open")  # US-only open
    projection = project_open_market_holdings(holdings_state(), mask)
    assert projection.mask_digest == mask.digest
    assert projection.tick_id == mask.tick_id
    assert [account.account_scope for account in projection.accounts] == [
        account.account_scope for account in holdings_state().accounts
    ]
    symbols = {
        item.symbol for account in projection.accounts for item in account.holdings
    }
    assert symbols == {"AAPL", "TSLA"}
    assert projection.unknown_symbols == frozenset({"MYSTERY"})


def test_projection_kr_only_inverse():
    mask = build("open", "after_close")
    projection = project_open_market_holdings(holdings_state(), mask)
    symbols = {
        item.symbol for account in projection.accounts for item in account.holdings
    }
    assert symbols == {"005930"}


def test_projection_unknown_market_never_falls_back():
    mask = build("open", "open")
    projection = project_open_market_holdings(holdings_state(), mask)
    symbols = {
        item.symbol for account in projection.accounts for item in account.holdings
    }
    assert "MYSTERY" not in symbols
    assert projection.unknown_symbols == frozenset({"MYSTERY"})


def test_projection_empty_mask_has_no_subjects():
    mask = build("after_close", "no_regular")
    projection = project_open_market_holdings(holdings_state(), mask)
    assert all(account.holdings == () for account in projection.accounts)


def test_projection_requires_a_real_mask():
    with pytest.raises(MaskBindingError):
        project_open_market_holdings(holdings_state(), None)
