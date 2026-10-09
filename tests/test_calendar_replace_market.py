"""Stage 1-B: market-level locked CAS replacement in the calendar cache (G-6),
sealed by the kernel-backed cache-commit claim (B1).

`replace_market` requires the authoritative OpsKernel: before mutating the
file it consumes the receipt's one-time cache-commit claim against the exact
STARTED/claimed kernel rows. A forged, unclaimed, terminal, or already
consumed receipt mutates nothing.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tokmo.records.calendar_cache import (
    CalendarCacheCorruptError,
    CalendarCacheStore,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)
from tokmo.values.calendar_refresh import CalendarFetchResult, CalendarIdentityError
from tokmo.values.ops_kernel import (
    CalendarStartReceiptV1,
    OpsKernelError,
    ProducerRole,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketDay,
    UsMarketDay,
)

NOW = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
ROLE = ProducerRole.COLLECTOR
CHILD_LIMITS = TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0)


def policy():
    return CadencePolicyV1(
        policy_version="cadence-test-v1",
        cadence_seconds=60,
        max_start_lag_seconds=10,
        child_timeout_seconds=40,
        termination_margin_seconds=10,
        request_limits=TickRequestLimitsV1(
            total_wire_starts=27,
            token_starts=0,
            calendar_starts=2,
            holdings_starts=0,
            quote_starts=0,
            trades_starts=0,
        ),
    )


def make_kernel(tmp_path, name="kernel"):
    return OpsKernelStore(tmp_path / name / "ops_kernel.sqlite3")


def issue_receipt(kernel, market, trade_date, *, slot=NOW, claim=True):
    kernel.plan_calendar_slot(ROLE, market, trade_date, slot, now=slot)
    receipt = kernel.start_calendar_attempt(
        ROLE,
        policy(),
        scheduled_for_utc=slot,
        market=market,
        market_local_date=trade_date,
        eligible_slot_utc=slot,
        now=slot,
        child_limits=CHILD_LIMITS,
    )
    if claim:
        kernel.claim_calendar_provider(receipt, now=slot)
    return receipt


def make_receipt(market, trade_date, ordinal=0):
    """Value-level receipt for typed-identity tests; NOT kernel authority."""
    return CalendarStartReceiptV1(
        "cadence-test-v1",
        NOW,
        market,
        trade_date,
        ordinal,
        f"calendar:{market.value}:{trade_date.isoformat()}:{ordinal}",
        "STARTED",
    )


def kr_day(trade_date=date(2026, 8, 2), start_hour=0):
    session = CalendarSession(
        datetime(
            trade_date.year, trade_date.month, trade_date.day, start_hour, 0, tzinfo=UTC
        ),
        datetime(trade_date.year, trade_date.month, trade_date.day, 6, 30, tzinfo=UTC),
    )
    return KrMarketDay(trade_date, IntegratedHour(regular_market=session))


def kr_fetch(receipt, trade_date=date(2026, 8, 2), start_hour=0):
    return CalendarFetchResult(
        Market.KR, trade_date, kr_day(trade_date, start_hour), receipt
    )


def us_fetch(receipt, trade_date=date(2026, 8, 1)):
    session = CalendarSession(
        datetime(trade_date.year, trade_date.month, trade_date.day, 13, 30, tzinfo=UTC),
        datetime(trade_date.year, trade_date.month, trade_date.day, 20, 0, tzinfo=UTC),
    )
    day = UsMarketDay(trade_date, regular_market=session)
    return CalendarFetchResult(Market.US, trade_date, day, receipt)


def test_fetch_result_requires_a_matching_started_receipt():
    """M5: no receipt, or a receipt for another market/date, can never form a
    committable fetch result, so the cache mutation is structurally zero."""
    day = kr_day()
    with pytest.raises(TypeError):
        CalendarFetchResult(Market.KR, date(2026, 8, 2), day)
    with pytest.raises(CalendarIdentityError):
        CalendarFetchResult(Market.KR, date(2026, 8, 2), day, None)
    with pytest.raises(CalendarIdentityError):
        CalendarFetchResult(
            Market.KR,
            date(2026, 8, 2),
            day,
            make_receipt(Market.US, date(2026, 8, 2)),
        )
    from tokmo.values.calendar_refresh import CalendarDateError

    with pytest.raises(CalendarDateError):
        CalendarFetchResult(
            Market.KR,
            date(2026, 8, 2),
            day,
            make_receipt(Market.KR, date(2026, 8, 9)),
        )


def make_store(tmp_path):
    return CalendarCacheStore(
        tmp_path / "ops" / "calendar_cache.json", clock=lambda: NOW
    )


def test_replace_market_commits_target_and_preserves_counterpart_exactly(tmp_path):
    from tokmo.records.calendar_cache import (
        CalendarReplaceResult,
        calendar_market_digest,
    )

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    us_receipt = issue_receipt(kernel, Market.US, date(2026, 8, 1))
    kr_receipt = issue_receipt(kernel, Market.KR, date(2026, 8, 2))
    assert (
        store.replace_market(
            Market.US,
            None,
            us_fetch(us_receipt),
            authority=kernel,
        )
        is CalendarReplaceResult.REPLACED
    )
    us_before = store.read().us

    digest = calendar_market_digest(store.read().kr)
    assert digest is None
    assert (
        store.replace_market(
            Market.KR,
            None,
            kr_fetch(kr_receipt),
            authority=kernel,
        )
        is CalendarReplaceResult.REPLACED
    )

    loaded = store.read()
    assert loaded.kr.trade_date == "2026-08-02"
    assert loaded.us == us_before  # non-target market byte-identical entry


def test_replace_market_cas_conflict_never_overwrites(tmp_path):
    from tokmo.records.calendar_cache import (
        CalendarCacheCasConflictError,
        calendar_market_digest,
    )

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    first = issue_receipt(kernel, Market.KR, date(2026, 8, 2))
    store.replace_market(Market.KR, None, kr_fetch(first), authority=kernel)
    before = (tmp_path / "ops" / "calendar_cache.json").read_bytes()

    # Writer A read the old (absent) state; writer B committed meanwhile.
    second = issue_receipt(kernel, Market.KR, date(2026, 8, 3))
    retry_fetch = kr_fetch(second, trade_date=date(2026, 8, 3))
    with pytest.raises(CalendarCacheCasConflictError):
        store.replace_market(Market.KR, None, retry_fetch, authority=kernel)
    assert (tmp_path / "ops" / "calendar_cache.json").read_bytes() == before

    # Correct digest succeeds after a fresh read; the CAS refusal above did
    # not burn the one-time cache-commit claim.
    digest = calendar_market_digest(store.read().kr)
    store.replace_market(Market.KR, digest, retry_fetch, authority=kernel)
    assert store.read().kr.trade_date == "2026-08-03"


def test_replace_market_same_date_exact_content_is_noop(tmp_path):
    from tokmo.records.calendar_cache import (
        CalendarReplaceResult,
        calendar_market_digest,
    )

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    store.replace_market(
        Market.KR,
        None,
        kr_fetch(issue_receipt(kernel, Market.KR, date(2026, 8, 2))),
        authority=kernel,
    )
    before = (tmp_path / "ops" / "calendar_cache.json").read_bytes()
    digest = calendar_market_digest(store.read().kr)

    # An independent racing writer (its own kernel authority) replays the
    # exact same content: a no-op, never a second file write.
    racing_kernel = make_kernel(tmp_path, name="racing")
    replay = kr_fetch(issue_receipt(racing_kernel, Market.KR, date(2026, 8, 2)))
    assert (
        store.replace_market(
            Market.KR,
            digest,
            replay,
            authority=racing_kernel,
        )
        is CalendarReplaceResult.UNCHANGED
    )
    assert (tmp_path / "ops" / "calendar_cache.json").read_bytes() == before


def test_replace_market_same_date_conflicting_content_is_explicit_conflict(tmp_path):
    from tokmo.records.calendar_cache import (
        CalendarCacheSameDateConflictError,
        calendar_market_digest,
    )

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    store.replace_market(
        Market.KR,
        None,
        kr_fetch(issue_receipt(kernel, Market.KR, date(2026, 8, 2))),
        authority=kernel,
    )
    before = (tmp_path / "ops" / "calendar_cache.json").read_bytes()
    digest = calendar_market_digest(store.read().kr)

    racing_kernel = make_kernel(tmp_path, name="racing")
    conflicting = kr_fetch(
        issue_receipt(racing_kernel, Market.KR, date(2026, 8, 2)),
        start_hour=1,
    )
    with pytest.raises(CalendarCacheSameDateConflictError):
        store.replace_market(Market.KR, digest, conflicting, authority=racing_kernel)
    assert (tmp_path / "ops" / "calendar_cache.json").read_bytes() == before


def test_replace_market_fsync_failure_preserves_previous_file(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    store.replace_market(
        Market.KR,
        None,
        kr_fetch(issue_receipt(kernel, Market.KR, date(2026, 8, 2))),
        authority=kernel,
    )
    before = (tmp_path / "ops" / "calendar_cache.json").read_bytes()
    from tokmo.records.calendar_cache import calendar_market_digest

    digest = calendar_market_digest(store.read().kr)
    monkeypatch.setattr(
        "tokmo.records.atomic_json.os.fsync",
        lambda fd: (_ for _ in ()).throw(OSError("injected fsync failure")),
    )
    next_receipt = issue_receipt(kernel, Market.KR, date(2026, 8, 3))
    with pytest.raises(OSError):
        store.replace_market(
            Market.KR,
            digest,
            kr_fetch(next_receipt, trade_date=date(2026, 8, 3)),
            authority=kernel,
        )
    monkeypatch.undo()
    assert (tmp_path / "ops" / "calendar_cache.json").read_bytes() == before


def test_replace_market_rejects_market_identity_mismatch_and_untyped_day(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    us_receipt = issue_receipt(kernel, Market.US, date(2026, 8, 1))
    with pytest.raises(CalendarIdentityError):
        store.replace_market(Market.KR, None, us_fetch(us_receipt), authority=kernel)
    with pytest.raises(CalendarIdentityError):
        store.replace_market(Market.KR, None, kr_day(), authority=kernel)  # raw day
    assert store.read() is None


def test_replace_market_corrupt_cache_fails_closed_without_write(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    path = tmp_path / "ops" / "calendar_cache.json"
    store.replace_market(
        Market.KR,
        None,
        kr_fetch(issue_receipt(kernel, Market.KR, date(2026, 8, 2))),
        authority=kernel,
    )
    path.write_bytes(b"{corrupt")
    us_receipt = issue_receipt(kernel, Market.US, date(2026, 8, 1))
    with pytest.raises(CalendarCacheCorruptError):
        store.replace_market(Market.US, None, us_fetch(us_receipt), authority=kernel)
    assert path.read_bytes() == b"{corrupt"


# --- B1: the cache commit itself is sealed by the kernel-backed claim ---


def test_replace_market_requires_a_kernel_authority(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    fetch = kr_fetch(issue_receipt(kernel, Market.KR, date(2026, 8, 2)))
    with pytest.raises(TypeError):
        store.replace_market(Market.KR, None, fetch)
    with pytest.raises(ValueError):
        store.replace_market(Market.KR, None, fetch, authority=None)
    assert store.read() is None


def test_direct_replace_with_hand_constructed_receipt_mutates_nothing(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)  # no planned/started/claimed rows at all
    forged = kr_fetch(make_receipt(Market.KR, date(2026, 8, 2)))
    with pytest.raises(OpsKernelError):
        store.replace_market(Market.KR, None, forged, authority=kernel)
    assert store.read() is None


def test_direct_replace_with_unclaimed_receipt_mutates_nothing(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    unclaimed = issue_receipt(kernel, Market.KR, date(2026, 8, 2), claim=False)
    with pytest.raises(OpsKernelError):
        store.replace_market(Market.KR, None, kr_fetch(unclaimed), authority=kernel)
    assert store.read() is None


def test_direct_replace_with_terminal_receipt_mutates_nothing(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    receipt = issue_receipt(kernel, Market.KR, date(2026, 8, 2))
    kernel.settle_calendar_attempt(
        ROLE,
        Market.KR,
        date(2026, 8, 2),
        receipt.attempt_ordinal,
        "FAILED",
        now=NOW + timedelta(minutes=1),
    )
    with pytest.raises(OpsKernelError):
        store.replace_market(Market.KR, None, kr_fetch(receipt), authority=kernel)
    assert store.read() is None


def test_direct_replace_with_consumed_receipt_mutates_nothing(tmp_path):
    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    receipt = issue_receipt(kernel, Market.KR, date(2026, 8, 2))
    fetch = kr_fetch(receipt)
    store.replace_market(Market.KR, None, fetch, authority=kernel)
    before = (tmp_path / "ops" / "calendar_cache.json").read_bytes()

    second = CalendarCacheStore(
        tmp_path / "ops" / "calendar_cache_second.json",
        clock=lambda: NOW,
    )
    with pytest.raises(OpsKernelError):
        second.replace_market(Market.KR, None, fetch, authority=kernel)
    assert second.read() is None
    assert (tmp_path / "ops" / "calendar_cache.json").read_bytes() == before
