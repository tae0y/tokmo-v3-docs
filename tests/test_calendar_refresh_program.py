"""Stage 1-C: independent per-market calendar refresh with typed results (G-7)
bound to OpsKernel STARTED calendar receipts (M5, SDS §4/§7.3), with B1
receipt authenticity: the typed receipt dataclass alone is never authority.

A stale market may fetch only after a one-time DB-backed provider claim
against the exact planned/STARTED cadence rows in the operations kernel.
A hand-constructed receipt with canonical fields, a terminal/UNKNOWN
receipt, a field-mismatched receipt, or a receipt whose claim was already
consumed all mean provider call zero and cache mutation zero.
"""

from datetime import UTC, date, datetime, timedelta

import pytest

from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)
from tokmo.values.calendar_refresh import market_local_date
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

# 19:00Z: KR local date 2026-08-02, US local date 2026-08-01.
NOW = datetime(2026, 8, 1, 19, 0, tzinfo=UTC)
KR_DATE = date(2026, 8, 2)
US_DATE = date(2026, 8, 1)
ROLE = ProducerRole.COLLECTOR
CHILD_LIMITS = TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0)


def at(minutes):
    return NOW + timedelta(minutes=minutes)


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


def make_kernel(tmp_path, name="ops"):
    return OpsKernelStore(tmp_path / name / "ops_kernel.sqlite3")


def issue_receipt(kernel, market, local_date, *, slot=NOW):
    """The only green path to a receipt: plan and start through the kernel."""
    kernel.plan_calendar_slot(ROLE, market, local_date, slot, now=slot)
    return kernel.start_calendar_attempt(
        ROLE,
        policy(),
        scheduled_for_utc=slot,
        market=market,
        market_local_date=local_date,
        eligible_slot_utc=slot,
        now=slot,
        child_limits=CHILD_LIMITS,
    )


def issue_receipts(kernel, *, slot=NOW, now=NOW):
    return {
        market: issue_receipt(kernel, market, market_local_date(market, now), slot=slot)
        for market in Market
    }


def kr_day(trade_date=KR_DATE):
    session = CalendarSession(
        datetime(trade_date.year, trade_date.month, trade_date.day, 0, 0, tzinfo=UTC),
        datetime(trade_date.year, trade_date.month, trade_date.day, 6, 30, tzinfo=UTC),
    )
    return KrMarketDay(trade_date, IntegratedHour(regular_market=session))


def us_day(trade_date=US_DATE, *, naive=False):
    if naive:
        session = CalendarSession(
            datetime(trade_date.year, trade_date.month, trade_date.day, 9, 30),
            datetime(trade_date.year, trade_date.month, trade_date.day, 16, 0),
        )
    else:
        session = CalendarSession(
            datetime(
                trade_date.year, trade_date.month, trade_date.day, 13, 30, tzinfo=UTC
            ),
            datetime(
                trade_date.year, trade_date.month, trade_date.day, 20, 0, tzinfo=UTC
            ),
        )
    return UsMarketDay(trade_date, regular_market=session)


def make_store(tmp_path, name="calendar_cache.json"):
    return CalendarCacheStore(tmp_path / "ops" / name, clock=lambda: NOW)


def run(store, kernel, kr_fetcher, us_fetcher, now=NOW, receipts=None):
    from tokmo.programs.calendar_refresh import refresh_markets

    return refresh_markets(
        store=store,
        fetchers={Market.KR: kr_fetcher, Market.US: us_fetcher},
        now=now,
        receipts=issue_receipts(kernel, now=now) if receipts is None else receipts,
        kernel=kernel,
    )


def by_market(results):
    return {result.market: result for result in results}


def test_cold_cache_kr_failure_does_not_suppress_us_attempt(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    calls = []

    def kr_fetcher(*, market, local_trade_date, receipt):
        calls.append(market)
        raise RuntimeError("provider down")

    def us_fetcher(*, market, local_trade_date, receipt):
        calls.append(market)
        return us_day(local_trade_date)

    results = by_market(run(store, kernel, kr_fetcher, us_fetcher))
    assert calls == [Market.KR, Market.US]
    assert results[Market.KR].state is MarketRefreshState.PROVIDER_FAILED
    assert results[Market.KR].refreshed is False
    assert results[Market.US].state is MarketRefreshState.SUCCESS
    assert results[Market.US].refreshed is True
    cache = store.read()
    assert cache.kr is None and cache.us.trade_date == "2026-08-01"


def test_cold_cache_us_failure_does_not_suppress_kr_attempt(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)

    def us_fetcher(*, market, local_trade_date, receipt):
        raise RuntimeError("provider down")

    results = by_market(
        run(
            store,
            kernel,
            lambda *, market, local_trade_date, receipt: kr_day(local_trade_date),
            us_fetcher,
        )
    )
    assert results[Market.KR].state is MarketRefreshState.SUCCESS
    assert results[Market.US].state is MarketRefreshState.PROVIDER_FAILED
    cache = store.read()
    assert cache.kr.trade_date == "2026-08-02" and cache.us is None


def test_wrong_market_date_and_boundary_results_never_commit(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    cases = (
        (lambda d: us_day(d), MarketRefreshState.INVALID_IDENTITY),  # KR got a US day
        (lambda d: kr_day(date(2026, 8, 9)), MarketRefreshState.INVALID_DATE),
    )
    minutes = 0
    for build, expected in cases:
        receipts = {
            Market.KR: issue_receipt(kernel, Market.KR, KR_DATE, slot=at(minutes)),
            Market.US: issue_receipt(kernel, Market.US, US_DATE, slot=at(minutes)),
        }
        results = by_market(
            run(
                store,
                kernel,
                lambda *, market, local_trade_date, receipt, build=build: build(
                    local_trade_date
                ),
                lambda *, market, local_trade_date, receipt: us_day(
                    local_trade_date, naive=True
                ),
                receipts=receipts,
            )
        )
        assert results[Market.KR].state is expected
        assert results[Market.US].state is MarketRefreshState.INVALID_BOUNDARY
        assert store.read() is None  # nothing was committed
        # Settle both consumed ordinals and step past the retry backoff so the
        # next case starts a fresh kernel-issued attempt.
        for market, local_date in ((Market.KR, KR_DATE), (Market.US, US_DATE)):
            kernel.settle_calendar_attempt(
                ROLE,
                market,
                local_date,
                receipts[market].attempt_ordinal,
                "FAILED",
                now=at(minutes + 1),
            )
        minutes += 7


def test_fresh_cache_is_success_without_any_fetch_or_receipt(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    run(
        store,
        kernel,
        lambda *, market, local_trade_date, receipt: kr_day(local_trade_date),
        lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
    )

    def forbidden(**_):
        raise AssertionError("fresh cache must not fetch")

    # A cache-hit tick needs no admission receipt and reserves no capacity.
    results = by_market(
        run(
            store,
            kernel,
            forbidden,
            forbidden,
            receipts={Market.KR: None, Market.US: None},
        )
    )
    for market in Market:
        assert results[market].state is MarketRefreshState.SUCCESS
        assert results[market].refreshed is False
        assert results[market].receipt is None


def test_one_stale_market_refreshes_alone(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    run(
        store,
        kernel,
        lambda *, market, local_trade_date, receipt: kr_day(local_trade_date),
        lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
    )

    # New York local midnight passed (04:00Z): only US is stale; KR is still
    # inside its 2026-08-02 local date.
    later = datetime(2026, 8, 2, 4, 30, tzinfo=UTC)

    def kr_forbidden(**_):
        raise AssertionError("KR is fresh and must not fetch")

    results = by_market(
        run(
            store,
            kernel,
            kr_forbidden,
            lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
            now=later,
            receipts={
                Market.KR: None,
                Market.US: issue_receipt(
                    kernel, Market.US, date(2026, 8, 2), slot=later
                ),
            },
        )
    )
    assert results[Market.US].state is MarketRefreshState.SUCCESS
    assert results[Market.US].refreshed is True
    assert results[Market.KR].state is MarketRefreshState.SUCCESS
    assert results[Market.KR].refreshed is False
    assert store.read().us.trade_date == "2026-08-02"
    assert store.read().kr.trade_date == "2026-08-02"


def test_corrupt_shared_cache_closes_both_markets_with_zero_fetches(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    (tmp_path / "ops").mkdir(parents=True, exist_ok=True)
    (tmp_path / "ops" / "calendar_cache.json").write_bytes(b"{corrupt")

    def forbidden(**_):
        raise AssertionError("corrupt cache must not fetch")

    results = by_market(
        run(
            store,
            kernel,
            forbidden,
            forbidden,
            receipts={Market.KR: None, Market.US: None},
        )
    )
    for market in Market:
        assert results[market].state is MarketRefreshState.CACHE_UPDATE_FAILED
        assert results[market].refreshed is False


# --- M6: CAS conflict forces re-read/rejudge, never a provider re-call ---


def test_cas_conflict_with_valid_racing_commit_rejudges_to_current_success(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState
    from tokmo.values.calendar_refresh import CalendarFetchResult

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    # The racing writer is an independent process with its own kernel-issued,
    # provider-claimed receipt for the same market-local date.
    racing_kernel = make_kernel(tmp_path, name="racing")
    racing_receipt = issue_receipt(racing_kernel, Market.KR, KR_DATE)
    racing_kernel.claim_calendar_provider(racing_receipt, now=NOW)
    racing = CalendarCacheStore(
        tmp_path / "ops" / "calendar_cache.json", clock=lambda: NOW
    )
    calls = []

    def kr_fetcher(*, market, local_trade_date, receipt):
        # Another writer commits the same valid KR local date mid-flight.
        calls.append(market)
        racing.replace_market(
            Market.KR,
            None,
            CalendarFetchResult(
                Market.KR,
                local_trade_date,
                kr_day(local_trade_date),
                racing_receipt,
            ),
            authority=racing_kernel,
        )
        return kr_day(local_trade_date)

    results = by_market(
        run(
            store,
            kernel,
            kr_fetcher,
            lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
        )
    )
    kr_result = results[Market.KR]
    # The racing writer's entry IS the current local date: rejudge to a typed
    # usable success without calling the provider again or overwriting.
    assert calls == [Market.KR]  # exactly one provider call, no re-call
    assert kr_result.state is MarketRefreshState.SUCCESS
    assert kr_result.refreshed is False
    assert kr_result.rejudged is True
    assert results[Market.US].state is MarketRefreshState.SUCCESS
    assert store.read().kr.trade_date == "2026-08-02"


def test_cas_conflict_with_corrupt_reread_fails_closed(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    path = tmp_path / "ops" / "calendar_cache.json"
    path.parent.mkdir(parents=True, exist_ok=True)

    def kr_fetcher(*, market, local_trade_date, receipt):
        # A writer corrupts the shared file mid-flight: the CAS check and the
        # forced re-read both see untrustworthy state.
        path.write_bytes(b"{corrupt")
        return kr_day(local_trade_date)

    results = by_market(
        run(
            store,
            kernel,
            kr_fetcher,
            lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
        )
    )
    assert results[Market.KR].state is MarketRefreshState.CACHE_UPDATE_FAILED
    assert results[Market.KR].refreshed is False
    assert results[Market.KR].rejudged is False


def test_cas_conflict_with_foreign_date_commit_stays_conflict(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState
    from tokmo.values.calendar_refresh import CalendarFetchResult

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    foreign = date(2026, 8, 9)
    racing_kernel = make_kernel(tmp_path, name="racing")
    racing_receipt = issue_receipt(racing_kernel, Market.KR, foreign)
    racing_kernel.claim_calendar_provider(racing_receipt, now=NOW)
    racing = CalendarCacheStore(
        tmp_path / "ops" / "calendar_cache.json", clock=lambda: NOW
    )
    calls = []

    def kr_fetcher(*, market, local_trade_date, receipt):
        calls.append(market)
        racing.replace_market(
            Market.KR,
            None,
            CalendarFetchResult(Market.KR, foreign, kr_day(foreign), racing_receipt),
            authority=racing_kernel,
        )
        return kr_day(local_trade_date)

    results = by_market(
        run(
            store,
            kernel,
            kr_fetcher,
            lambda *, market, local_trade_date, receipt: us_day(local_trade_date),
        )
    )
    # The re-read entry is NOT the requested local date: no usable rejudge,
    # no overwrite, and still no provider re-call.
    assert calls == [Market.KR]
    assert results[Market.KR].state is MarketRefreshState.CACHE_CONFLICT
    assert results[Market.KR].refreshed is False
    assert results[Market.KR].rejudged is False
    assert store.read().kr.trade_date == foreign.isoformat()


# --- M5: STARTED-receipt binding at the production transport seam ---


def test_stale_market_without_started_receipt_fetches_zero(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    calls = []

    def counting(*, market, local_trade_date, receipt):
        calls.append(market)
        return (
            kr_day(local_trade_date)
            if market is Market.KR
            else us_day(local_trade_date)
        )

    results = by_market(
        run(
            store,
            kernel,
            counting,
            counting,
            receipts={Market.KR: None, Market.US: None},
        )
    )
    assert calls == []
    for market in Market:
        assert results[market].state is MarketRefreshState.INVALID_IDENTITY
        assert results[market].refreshed is False
    assert store.read() is None  # cache mutation zero


def test_mismatched_untyped_or_subclassed_receipt_fetches_zero(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    calls = []

    def counting(**_):
        calls.append(1)
        raise AssertionError("must not fetch")

    # Even genuinely kernel-issued receipts are refused when their identity
    # does not match the requested market/local date.
    wrong_market = issue_receipt(kernel, Market.US, US_DATE)
    wrong_date = issue_receipt(kernel, Market.KR, date(2026, 8, 9), slot=at(5))

    class Forged:
        market = Market.KR
        market_local_date = KR_DATE
        state = "STARTED"

    class SubReceipt(CalendarStartReceiptV1):
        pass

    subclassed = SubReceipt(
        "cadence-test-v1",
        NOW,
        Market.KR,
        KR_DATE,
        0,
        "calendar:KR:2026-08-02:0",
        "STARTED",
    )

    for kr_receipt in (wrong_market, wrong_date, Forged(), subclassed, "receipt"):
        results = by_market(
            run(
                store,
                kernel,
                counting,
                counting,
                receipts={Market.KR: kr_receipt, Market.US: None},
            )
        )
        assert results[Market.KR].state is MarketRefreshState.INVALID_IDENTITY
    assert calls == []
    assert store.read() is None


def test_kernel_issued_receipt_permits_exactly_one_call_and_binds_commit(tmp_path):
    """B1 green path: a receipt planned/started through the kernel performs
    exactly one provider call and one cache commit, and the one-time claim is
    consumed in the kernel DB."""
    from tokmo.programs.calendar_refresh import MarketRefreshState

    kernel = make_kernel(tmp_path)
    receipt = issue_receipt(kernel, Market.KR, KR_DATE)

    store = make_store(tmp_path)
    calls = []

    def kr_fetcher(*, market, local_trade_date, receipt):
        calls.append((market, local_trade_date, receipt.native_identity))
        return kr_day(local_trade_date)

    def us_forbidden(**_):
        raise AssertionError("US has no receipt and must not fetch")

    results = by_market(
        run(
            store,
            kernel,
            kr_fetcher,
            us_forbidden,
            receipts={Market.KR: receipt, Market.US: None},
        )
    )
    assert calls == [(Market.KR, KR_DATE, "calendar:KR:2026-08-02:0")]
    kr_result = results[Market.KR]
    assert kr_result.state is MarketRefreshState.SUCCESS
    assert kr_result.refreshed is True
    assert kr_result.receipt is receipt  # cache commit refers to the same identity
    assert store.read().kr.trade_date == KR_DATE.isoformat()
    # The provider claim is one-time in the kernel DB: a second claim of the
    # very same receipt fails even before any terminal settlement.
    with pytest.raises(OpsKernelError):
        kernel.claim_calendar_provider(receipt, now=at(1))
    # Terminal settlement closes the same durable identity in the kernel.
    settled = kernel.settle_calendar_attempt(
        ProducerRole.COLLECTOR,
        Market.KR,
        KR_DATE,
        receipt.attempt_ordinal,
        "SUCCEEDED",
        now=NOW,
    )
    assert settled["state"] == "SUCCEEDED"


def test_refresh_requires_receipts_and_kernel(tmp_path):
    from tokmo.programs.calendar_refresh import refresh_markets

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    with pytest.raises(TypeError):
        refresh_markets(
            store=store,
            fetchers={market: (lambda **_: None) for market in Market},
            now=NOW,
            kernel=kernel,
        )
    with pytest.raises(TypeError):
        refresh_markets(
            store=store,
            fetchers={market: (lambda **_: None) for market in Market},
            now=NOW,
            receipts={Market.KR: None, Market.US: None},
        )


# --- B1: DB-backed receipt authenticity and one-time claim ---


def test_hand_constructed_receipt_with_canonical_fields_is_refused(tmp_path):
    """The dataclass alone is not authority: a forged receipt whose canonical
    fields are perfectly formed, but which has no planned/STARTED kernel rows,
    performs provider call zero and cache mutation zero."""
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)  # no planned slot, no started attempt
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return kr_day(kwargs["local_trade_date"])

    forged = CalendarStartReceiptV1(
        "cadence-test-v1",
        NOW,
        Market.KR,
        KR_DATE,
        0,
        "calendar:KR:2026-08-02:0",
        "STARTED",
    )
    results = by_market(
        run(
            store,
            kernel,
            counting,
            counting,
            receipts={Market.KR: forged, Market.US: None},
        )
    )
    assert calls == []
    assert results[Market.KR].state is MarketRefreshState.INVALID_IDENTITY
    assert results[Market.KR].refreshed is False
    assert store.read() is None


def test_receipt_field_mismatch_with_kernel_rows_is_refused(tmp_path):
    """Every receipt field must match the current kernel rows exactly: a
    forged sibling of a genuine STARTED attempt differing only in policy
    version or scheduled slot is refused with provider zero."""
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    issue_receipt(kernel, Market.KR, KR_DATE)  # genuine STARTED rows exist
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return kr_day(kwargs["local_trade_date"])

    wrong_policy = CalendarStartReceiptV1(
        "cadence-forged-v1",
        NOW,
        Market.KR,
        KR_DATE,
        0,
        "calendar:KR:2026-08-02:0",
        "STARTED",
    )
    wrong_slot = CalendarStartReceiptV1(
        "cadence-test-v1",
        at(60),
        Market.KR,
        KR_DATE,
        0,
        "calendar:KR:2026-08-02:0",
        "STARTED",
    )
    wrong_ordinal = CalendarStartReceiptV1(
        "cadence-test-v1",
        NOW,
        Market.KR,
        KR_DATE,
        3,
        "calendar:KR:2026-08-02:3",
        "STARTED",
    )
    for forged in (wrong_policy, wrong_slot, wrong_ordinal):
        results = by_market(
            run(
                store,
                kernel,
                counting,
                counting,
                receipts={Market.KR: forged, Market.US: None},
            )
        )
        assert results[Market.KR].state is MarketRefreshState.INVALID_IDENTITY
    assert calls == []
    assert store.read() is None


def test_same_receipt_second_refresh_performs_provider_zero(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    kernel = make_kernel(tmp_path)
    receipt = issue_receipt(kernel, Market.KR, KR_DATE)
    store = make_store(tmp_path)
    run(
        store,
        kernel,
        lambda *, market, local_trade_date, receipt: kr_day(local_trade_date),
        lambda **_: (_ for _ in ()).throw(AssertionError("no US receipt")),
        receipts={Market.KR: receipt, Market.US: None},
    )
    assert store.read().kr.trade_date == KR_DATE.isoformat()

    # The identical receipt object is still typed and still says STARTED, but
    # its one-time kernel claim is consumed: a second refresh against a fresh
    # cache performs provider call zero and cache mutation zero.
    second = make_store(tmp_path, name="calendar_cache_second.json")
    calls = []

    def counting(**kwargs):
        calls.append(1)
        return kr_day(kwargs["local_trade_date"])

    results = by_market(
        run(
            second,
            kernel,
            counting,
            counting,
            receipts={Market.KR: receipt, Market.US: None},
        )
    )
    assert calls == []
    assert results[Market.KR].state is MarketRefreshState.INVALID_IDENTITY
    assert second.read() is None


def test_terminal_or_unknown_receipt_is_refused(tmp_path):
    from tokmo.programs.calendar_refresh import MarketRefreshState

    store = make_store(tmp_path)
    kernel = make_kernel(tmp_path)
    kr_receipt = issue_receipt(kernel, Market.KR, KR_DATE)
    kernel.settle_calendar_attempt(
        ROLE,
        Market.KR,
        KR_DATE,
        kr_receipt.attempt_ordinal,
        "FAILED",
        now=at(1),
    )
    us_receipt = issue_receipt(kernel, Market.US, US_DATE, slot=at(2))
    kernel.reconcile_cadence(ROLE, now=at(3))  # crash path: US attempt -> UNKNOWN
    calls = []

    def counting(**_):
        calls.append(1)
        raise AssertionError("terminal receipts must not fetch")

    # Both stale receipt objects still carry the frozen "STARTED" value; the
    # kernel rows are terminal, so both are refused with provider zero.
    results = by_market(
        run(
            store,
            kernel,
            counting,
            counting,
            receipts={Market.KR: kr_receipt, Market.US: us_receipt},
        )
    )
    assert calls == []
    assert results[Market.KR].state is MarketRefreshState.INVALID_IDENTITY
    assert results[Market.US].state is MarketRefreshState.INVALID_IDENTITY
    assert store.read() is None


def test_receipt_reused_for_direct_cache_mutation_is_refused(tmp_path):
    from tokmo.values.calendar_refresh import CalendarFetchResult

    kernel = make_kernel(tmp_path)
    receipt = issue_receipt(kernel, Market.KR, KR_DATE)
    store = make_store(tmp_path)
    run(
        store,
        kernel,
        lambda *, market, local_trade_date, receipt: kr_day(local_trade_date),
        lambda **_: (_ for _ in ()).throw(AssertionError("no US receipt")),
        receipts={Market.KR: receipt, Market.US: None},
    )
    assert store.read().kr.trade_date == KR_DATE.isoformat()

    # Bypassing refresh and driving the cache store directly with the already
    # consumed receipt must mutate nothing.
    second = make_store(tmp_path, name="calendar_cache_second.json")
    fetch = CalendarFetchResult(Market.KR, KR_DATE, kr_day(KR_DATE), receipt)
    with pytest.raises(OpsKernelError):
        second.replace_market(Market.KR, None, fetch, authority=kernel)
    assert second.read() is None
