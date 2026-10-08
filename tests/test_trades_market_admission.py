"""Stage 2-D: exact order-currency classification and mask admission."""

from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.legacy_g2 import legacy_run_trades_sync
from tokmo.programs.trades_sync import run_trades_sync
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.order_scan_state import IncompleteRetryRow, OrderScanStateStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1, AccountScopeIdentityV1, CurrentHoldingsV1, HoldingItemV1,
)
from tokmo.values.cadence_policy import cadence_tick_v1
from tokmo.values.open_market_mask import MaskBindingError, build_open_market_mask
from tokmo.values.session_gate import Market, SessionVerdict
from tokmo.values.toss_market_calendar import CalendarSession
from tokmo.values.toss_orders import Order, OrderExecution, PaginatedOrderResponse

NOW = datetime(2026, 8, 3, 16, 0, tzinfo=timezone.utc)
from datetime import timedelta
TICK1 = cadence_tick_v1("cadence-300s-v1", NOW)
TICK2 = cadence_tick_v1("cadence-300s-v1", NOW + timedelta(seconds=300))
KR_LOCAL = date(2026, 8, 4)   # 01:00 KST next day
US_LOCAL = date(2026, 8, 3)
SCOPE = "toss-acct-" + "c" * 24

SESSION = CalendarSession(
    datetime(2026, 8, 3, 13, 30, tzinfo=timezone.utc),
    datetime(2026, 8, 3, 20, 0, tzinfo=timezone.utc),
)


def clock():
    return NOW


def mask_for(kr_reason, us_reason, *, tick=None):
    tick = TICK1 if tick is None else tick
    verdicts = (
        SessionVerdict(Market.KR, kr_reason == "open", kr_reason, SESSION),
        SessionVerdict(Market.US, us_reason == "open", us_reason, SESSION),
    )
    return build_open_market_mask(
        policy_version=tick.policy_version,
        scheduled_for_utc=tick.scheduled_for_utc, tick_id=tick.tick_id,
        verdicts=verdicts,
        local_dates={Market.KR: KR_LOCAL, Market.US: US_LOCAL},
    )


def holdings_with(items):
    return CurrentHoldingsV1(
        1, (AccountHoldingsV1(SCOPE, 1, tuple(items)),), True, NOW,
        {SCOPE: AccountScopeIdentityV1(SCOPE, None, 1, None)},
    )


MIXED_HOLDINGS = holdings_with((
    HoldingItemV1("005930", "1", "70000", "KRW", "KR", None),
    HoldingItemV1("AAPL", "1", "180", "USD", "US", None),
    HoldingItemV1("MYST", "1", "10", None, None, None),
))


def order(key, symbol, *, status="FILLED", complete=True, currency="USD"):
    return Order(
        key, symbol, "SELL", "MARKET", "DAY", status, "1", currency, NOW,
        OrderExecution(
            "1", "10" if complete else None, "10" if complete else None,
            None, None, NOW if complete else None, None,
        ),
    )


class Ledger:
    def __init__(self):
        self.events = []

    def append_batch(self, batch, *, import_audit):
        self.events.extend(batch)
        return SimpleNamespace(
            status="SAVED", reason="", inserted_count=len(batch),
            duplicate_count=0,
        )


class Calendar:
    def __init__(self):
        self.calls = []

    def regular_session_dates(self, market, anchor_date, count):
        self.calls.append((market, anchor_date))
        return tuple(anchor_date for _ in range(count))


def run(tmp_path, orders, *, mask=None, holdings=MIXED_HOLDINGS, detail=None,
        scan_state=None, tick=None, calendar=None, orders_provider=None):
    incidents = []
    ledger = Ledger()
    calendar = calendar if calendar is not None else Calendar()
    detail_calls = []

    def default_detail(**kwargs):
        detail_calls.append(kwargs)
        return TossReadOutcome("success", orders[0]) if orders else None

    state = scan_state or OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    common = dict(
        holdings=holdings,
        orders_provider=orders_provider or (lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse(tuple(orders), None, False),
        )),
        detail_provider=detail or default_detail,
        store=ledger, scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents.append(args),
        regular_session_dates=calendar, clock=clock, now=NOW,
        job_lock_path=tmp_path / "trades.lock",
    )
    if mask is not None:
        result = run_trades_sync(
            tick=TICK1 if tick is None else tick, mask=mask, **common,
        )
    else:
        result = legacy_run_trades_sync(
            tick=SimpleNamespace(tick_id=100),
            session_result=SimpleNamespace(verdicts=()), **common,
        )
    return SimpleNamespace(
        result=result, state=state, ledger=ledger, incidents=incidents,
        calendar=calendar, detail_calls=detail_calls,
        receipts=ReceiptStore(tmp_path / "receipt.json", clock=clock),
    )


# -- mask binding / empty mask -----------------------------------------------------


def test_mask_tick_mismatch_fails_closed_before_any_scan(tmp_path):
    with pytest.raises(MaskBindingError):
        run(
            tmp_path, (), mask=mask_for("after_close", "open", tick=TICK2),
            orders_provider=lambda **kwargs: pytest.fail("provider must not run"),
        )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    assert state.read_incomplete_retry() == ()
    assert not (tmp_path / "receipt.json").exists()


def test_empty_mask_is_truthful_skip_with_scan_state_mutation_zero(tmp_path):
    run_result = run(
        tmp_path, (), mask=mask_for("after_close", "after_close"),
        orders_provider=lambda **kwargs: pytest.fail("provider must not run"),
        detail=lambda **kwargs: pytest.fail("detail must not run"),
    )
    assert (run_result.result.status, run_result.result.reason) == (
        "skipped", "skipped_market_closed",
    )
    assert run_result.state.read_state(SCOPE) is None
    receipt = run_result.receipts.read()
    assert receipt.mask_digest == mask_for("after_close", "after_close").digest
    assert receipt.attempt.reason == "skipped_market_closed"


# -- evidence 7: KR+US+unknown page under a US-only mask ---------------------------


def test_us_only_mask_admits_only_open_uniquely_classified_records(tmp_path):
    mask = mask_for("after_close", "open")
    orders = (
        order("us-1", "AAPL"),
        order("kr-1", "005930", currency="KRW"),
        order("unknown-1", "MYST", currency="EUR"),
        order("us-needs-detail", "AAPL", complete=False),
    )
    detail_calls = []

    def detail(**kwargs):
        detail_calls.append(kwargs)
        return TossReadOutcome("success", order("us-needs-detail", "AAPL"))

    run_result = run(tmp_path, orders, mask=mask, detail=detail)

    # ledger admits open uniquely classified records only
    assert sorted(event.source_record_key for event in run_result.ledger.events) == [
        "us-1", "us-needs-detail",
    ]
    # exactly one detail start, for the open-market incomplete order
    assert [call["order_id"] for call in detail_calls] == ["us-needs-detail"]
    # closed-market and unknown records are durably withheld
    pending = {
        row.order_id: row.reason
        for row in run_result.state.read_market_pending(SCOPE)
    }
    assert pending == {
        "kr-1": "market_closed_pending",
        "unknown-1": "market_resolution_pending",
    }
    receipt = run_result.receipts.read()
    assert receipt.mask_digest == mask.digest
    assert receipt.completeness.incomplete_count == 2


def test_closed_market_pending_record_applies_when_its_market_opens(tmp_path):
    us_mask = mask_for("after_close", "open")
    kr_order = order("kr-1", "005930", currency="KRW")
    first = run(tmp_path, (kr_order,), mask=us_mask)
    assert first.ledger.events == []
    assert [row.order_id for row in first.state.read_market_pending(SCOPE)] == ["kr-1"]

    kr_mask = mask_for("open", "after_close", tick=TICK2)
    second = run(
        tmp_path, (kr_order,), mask=kr_mask, tick=TICK2,
        scan_state=first.state,
    )
    assert [event.source_record_key for event in second.ledger.events] == ["kr-1"]
    assert second.state.read_market_pending(SCOPE) == ()


def test_krw_order_absent_from_holdings_classifies_kr_and_fetches_detail(tmp_path):
    detailed = order("kr-absent", "NEWKR", currency="KRW")
    page_order = order("kr-absent", "NEWKR", currency="KRW", complete=False)
    calls = []

    def detail(**kwargs):
        calls.append(kwargs)
        return TossReadOutcome("success", detailed)

    result = run(
        tmp_path, (page_order,), mask=mask_for("open", "after_close"),
        holdings=holdings_with(()), detail=detail,
    )
    assert_classified(result, tmp_path, "kr-absent", "KR")
    assert [call["order_id"] for call in calls] == ["kr-absent"]


def test_usd_order_ignores_conflicting_holding_and_closed_us_skips_detail(tmp_path):
    conflicting = holdings_with((
        HoldingItemV1("CONFLICT", "1", "10", "KRW", "KR", None),
    ))
    incomplete = order("us-conflict", "CONFLICT", currency="USD", complete=False)
    closed = run(
        tmp_path, (incomplete,), mask=mask_for("open", "after_close"),
        holdings=conflicting,
        detail=lambda **kwargs: pytest.fail("closed US order must not fetch detail"),
    )
    assert pending_reasons(closed) == {"us-conflict": "market_closed_pending"}
    assert closed.ledger.events == []

    opened = run(
        tmp_path / "opened", (order("us-conflict", "CONFLICT", currency="USD"),),
        mask=mask_for("after_close", "open"), holdings=conflicting,
    )
    assert_classified(opened, tmp_path / "opened", "us-conflict", "US")


@pytest.mark.parametrize("currency", [None, "EUR", "usd", " USD", "USD ", ""])
def test_non_exact_or_unsupported_currency_is_unresolved_without_detail(
        tmp_path, currency):
    result = run(
        tmp_path, (order("bad-currency", "AAPL", currency=currency),),
        mask=mask_for("open", "open"),
        detail=lambda **kwargs: pytest.fail("unresolved order must not fetch detail"),
    )
    assert_durably_pending(result, "bad-currency")


def test_holdings_market_conflict_cannot_change_exact_currency_classification(tmp_path):
    conflict = holdings_with((
        HoldingItemV1("AAPL", "1", "180", "KRW", "KR", None),
    ))
    result = run(
        tmp_path, (order("currency-wins", "AAPL", currency="USD"),),
        mask=mask_for("open", "open"), holdings=conflict,
    )
    assert_classified(result, tmp_path, "currency-wins", "US")


def test_masked_inserted_rows_and_pack_carry_mask_identity(tmp_path):
    mask = mask_for("after_close", "open")
    run_result = run(tmp_path, (order("us-1", "AAPL"),), mask=mask)
    packs = MaterialPackWriter(tmp_path, clock=clock)
    from tokmo.values.material_pack import JobKind
    listed = packs.list_packs(JobKind.TRADES)
    assert listed
    tick_id, path, digest = listed[0]
    pack = packs.read_and_verify(path, digest)
    assert pack.mask_digest == mask.digest
    assert pack.market_partition == ("US",)
    inserted = [
        row for account in pack.payload["accounts"] for row in account["inserted"]
    ]
    assert {row["classified_market"] for row in inserted} == {"US"}


# -- evidence 3/7: retry/detail queues gated by the mask ---------------------------


def test_closed_market_retry_rows_never_start_detail(tmp_path):
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        SCOPE, "kr-retry", Market.KR, 1, 1, 1, 0, "2026-07-01", None,
    ))
    run_result = run(
        tmp_path, (order("us-1", "AAPL"),),
        mask=mask_for("after_close", "open"), scan_state=state,
        detail=lambda **kwargs: pytest.fail("closed-market detail must not run"),
    )
    assert run_result.result.status == "partial"
    row = run_result.state.read_incomplete_retry()[0]
    assert (row.source_record_key, row.attempts) == ("kr-retry", 1)  # untouched


# -- evidence 8: no KR default in session-date/window selection --------------------


def test_mixed_account_uses_each_markets_own_local_date_anchor(tmp_path):
    mask = mask_for("open", "open")
    run_result = run(
        tmp_path, (order("us-1", "AAPL"),), mask=mask,
    )
    anchors = {market: anchor for market, anchor in run_result.calendar.calls}
    assert anchors[Market.KR] == KR_LOCAL
    assert anchors[Market.US] == US_LOCAL


def test_all_unknown_account_cannot_default_to_kr(tmp_path):
    unknown_only = holdings_with((
        HoldingItemV1("MYST", "1", "10", None, None, None),
    ))
    run_result = run(
        tmp_path, (order("mystery-1", "MYST", currency="EUR"),),
        holdings=unknown_only,
    )
    # The account-scoped scan anchors both markets' own windows, never a
    # silent KR-only default.
    markets_consulted = {market for market, _anchor in run_result.calendar.calls}
    assert markets_consulted == {Market.KR, Market.US}
    # The unclassified record never reaches the ledger and never picks KR.
    assert run_result.ledger.events == []
    assert [
        (row.order_id, row.reason)
        for row in run_result.state.read_market_pending(SCOPE)
    ] == [("mystery-1", "market_resolution_pending")]


# -- B2: exact order currency is the sole market-classification input --------------


def pending_reasons(run_result):
    return {
        row.order_id: row.reason
        for row in run_result.state.read_market_pending(SCOPE)
    }


def assert_durably_pending(run_result, order_id):
    """An unsupported currency stays pending with detail and ledger at zero."""
    assert run_result.ledger.events == []
    assert run_result.detail_calls == []
    assert pending_reasons(run_result) == {order_id: "market_resolution_pending"}


def assert_classified(run_result, tmp_path, order_id, market_value):
    """An exact currency yields one event and its exact market in the pack."""
    assert [event.source_record_key for event in run_result.ledger.events] == [order_id]
    assert pending_reasons(run_result) == {}
    from tokmo.values.material_pack import JobKind
    packs = MaterialPackWriter(tmp_path, clock=clock)
    listed = packs.list_packs(JobKind.TRADES)
    assert listed
    pack = packs.read_and_verify(listed[0][1], listed[0][2])
    inserted = [
        row for account in pack.payload["accounts"] for row in account["inserted"]
    ]
    assert {row["classified_market"] for row in inserted} == {market_value}


DUAL_CURRENCY_HOLDINGS = (
    HoldingItemV1("DUAL", "1", "70000", "KRW", "KR", None),
    HoldingItemV1("DUAL", "1", "50", "USD", "US", None),
)


def test_dual_currency_holdings_cannot_change_exact_usd_classification(tmp_path):
    run_result = run(
        tmp_path, (order("dual-1", "DUAL", currency="USD"),),
        mask=mask_for("open", "open"),
        holdings=holdings_with(DUAL_CURRENCY_HOLDINGS),
    )
    assert_classified(run_result, tmp_path, "dual-1", "US")


def test_dual_currency_holdings_cannot_change_exact_krw_classification(tmp_path):
    run_result = run(
        tmp_path, (order("dual-2", "DUAL", currency="KRW"),),
        mask=mask_for("open", "open"),
        holdings=holdings_with(DUAL_CURRENCY_HOLDINGS),
    )
    assert_classified(run_result, tmp_path, "dual-2", "KR")


def test_exact_usd_classifies_without_holdings_evidence(tmp_path):
    run_result = run(
        tmp_path, (order("dual-3", "DUAL", currency="USD"),),
        mask=mask_for("open", "open"),
        holdings=holdings_with(DUAL_CURRENCY_HOLDINGS),
    )
    assert_classified(run_result, tmp_path, "dual-3", "US")


def test_missing_currency_is_pending_even_with_unique_holdings(tmp_path):
    run_result = run(
        tmp_path, (order("us-1", "AAPL", currency=None),),
        mask=mask_for("open", "open"),
    )
    assert_durably_pending(run_result, "us-1")


def test_unknown_currency_is_pending(tmp_path):
    run_result = run(
        tmp_path, (order("us-1", "AAPL", currency="EUR"),),
        mask=mask_for("open", "open"),
    )
    assert_durably_pending(run_result, "us-1")


def test_exact_usd_classifies_independently_of_holdings(tmp_path):
    run_result = run(
        tmp_path, (order("us-1", "AAPL"),), mask=mask_for("open", "open"),
    )
    assert [event.source_record_key for event in run_result.ledger.events] == ["us-1"]
    assert pending_reasons(run_result) == {}


def test_exact_krw_classifies_independently_of_holdings(tmp_path):
    run_result = run(
        tmp_path, (order("kr-1", "005930", currency="KRW"),),
        mask=mask_for("open", "open"),
    )
    assert [event.source_record_key for event in run_result.ledger.events] == ["kr-1"]
    assert pending_reasons(run_result) == {}


def test_legacy_mixed_account_consults_both_markets_never_only_kr(tmp_path):
    mixed_classified = holdings_with((
        HoldingItemV1("005930", "1", "70000", "KRW", "KR", None),
        HoldingItemV1("AAPL", "1", "180", "USD", "US", None),
    ))
    run_result = run(tmp_path, (order("us-1", "AAPL"),), holdings=mixed_classified)
    markets_consulted = {market for market, _anchor in run_result.calendar.calls}
    assert markets_consulted == {Market.KR, Market.US}
