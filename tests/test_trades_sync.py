import os
import json
import sqlite3
import fcntl
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.order_scan_state import (
    IngestAuditRow,
    IncompleteRetryRow,
    MarketPendingRow,
    OrderScanStateStore,
    ScanStateCorruptError,
    ScanStateLockedError,
    ScanStateV1,
)
from tokmo.records.receipt_store import ReceiptStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.programs.trades_sync import (
    bisect_identity_conflicts, provider_call_plan,
)
from tokmo.programs.legacy_g2 import legacy_run_trades_sync as run_trades_sync
from tokmo.values.current_holdings import (
    AccountHoldingsV1, AccountScopeIdentityV1, CurrentHoldingsV1, HoldingItemV1,
)
from tokmo.values.material_pack import JobKind
from tokmo.values.session_gate import Market
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded, OpsKernelConflictError, OpsKernelCooldownActive,
    ProducerRole,
)
from tokmo.values.request_parity import canonical_params_digest
from tokmo.values.toss_orders import Order, OrderExecution, PaginatedOrderResponse


def clock():
    return datetime(2026, 7, 30, tzinfo=timezone.utc)


def test_scan_store_round_trips_all_durable_dispositions_and_is_0600(tmp_path):
    path = tmp_path / "state.sqlite3"
    store = OrderScanStateStore(path, clock=clock)
    scope = "toss-acct-" + "b" * 24
    state = ScanStateV1(scope, "2026-07-29", "cursor", "2026-07-01",
                        "2026-07-30", None, False, None, False,
                        clock().isoformat())
    store.save_state(state)
    assert store.read_state(scope) == state
    store.add_quarantine_keys(scope, ("order-1",), "unknown_order_status")
    assert store.read_quarantine(scope) == ("order-1",)
    retry = IncompleteRetryRow(scope, "order-2", Market.KR, 1, 1, 1, 19,
                               "2026-07-01", None)
    store.add_incomplete_retry(retry)
    assert store.read_incomplete_retry() == (retry,)
    audit = IngestAuditRow("toss-orders-v1", scope, "order-3",
                           clock().isoformat(), 1, clock().isoformat())
    store.write_ingest_audit(audit)
    assert store.read_ingest_audit(
        ("toss-orders-v1", scope, "order-3")
    ) == audit
    pending = MarketPendingRow(scope, "order-4", "UNKNOWN", None, "FILLED",
                               "market_resolution_pending", 1, clock().isoformat())
    store.add_market_pending(pending)
    assert store.read_market_pending(scope) == (pending,)
    assert os.stat(path).st_mode & 0o777 == 0o600


def test_scan_store_rejects_symlink_and_corruption(tmp_path):
    target = tmp_path / "target"
    target.write_text("bad")
    link = tmp_path / "link"
    link.symlink_to(target)
    with pytest.raises(ScanStateCorruptError):
        OrderScanStateStore(link, clock=clock)
    corrupt = tmp_path / "corrupt.sqlite3"
    corrupt.write_text("not sqlite")
    with pytest.raises(ScanStateCorruptError):
        OrderScanStateStore(corrupt, clock=clock)


def test_incomplete_retry_resolution_and_expiry_are_scoped_by_both_primary_keys(tmp_path):
    store = OrderScanStateStore(tmp_path / "state.sqlite3", clock=clock)
    scopes = ("toss-acct-" + "1" * 24, "toss-acct-" + "2" * 24)
    for scope in scopes:
        store.add_incomplete_retry(
            IncompleteRetryRow(
                scope, "same-key", Market.KR, 1, 1, 1, 1,
                "2026-07-01", None,
            )
        )
    store.resolve_incomplete_retry(scopes[0], "same-key")
    assert [row.account_scope for row in store.read_incomplete_retry()] == [scopes[1]]
    store.expire_incomplete_retry(scopes[1], "same-key")
    assert store.read_incomplete_retry() == ()


def test_scan_store_repeated_reads_do_not_grow_file_descriptors(tmp_path):
    store = OrderScanStateStore(tmp_path / "state.sqlite3", clock=clock)
    before = len(os.listdir("/dev/fd"))
    for _ in range(200):
        store.read_state("toss-acct-" + "d" * 24)
    after = len(os.listdir("/dev/fd"))
    assert after <= before + 2


def test_scan_store_rejects_nonregular_path_reports_lock_and_reads_retry_fifo(tmp_path):
    directory = tmp_path / "directory.sqlite3"
    directory.mkdir()
    with pytest.raises(ScanStateCorruptError, match="non-regular"):
        OrderScanStateStore(directory, clock=clock)

    path = tmp_path / "state.sqlite3"
    store = OrderScanStateStore(path, clock=clock)
    scope = "toss-acct-" + "f" * 24
    store.add_incomplete_retry(IncompleteRetryRow(
        scope, "later", Market.US, 20, 1, 1, 0, "2026-07-29", None,
    ))
    store.add_incomplete_retry(IncompleteRetryRow(
        scope, "earlier", Market.US, 10, 1, 1, 0, "2026-07-29", None,
    ))
    assert [row.source_record_key for row in store.read_incomplete_retry()] == [
        "earlier", "later",
    ]

    locked = sqlite3.connect(path, timeout=0, isolation_level=None)
    locked.execute("BEGIN EXCLUSIVE")
    try:
        with pytest.raises(ScanStateLockedError):
            store.read_state(scope)
    finally:
        locked.rollback()
        locked.close()


def test_scheduler_reserves_exact_two_detail_and_ten_orders_without_borrowing():
    assert provider_call_plan(detail_ready=0) == (0, 10)
    assert provider_call_plan(detail_ready=99) == (2, 10)


class Receipt:
    def __init__(self, status, reason="", inserted_count=0, duplicate_count=0):
        self.status, self.reason = status, reason
        self.inserted_count, self.duplicate_count = inserted_count, duplicate_count


def test_only_identity_conflict_is_bisected_and_other_failures_stop():
    calls = []
    def append(events):
        calls.append(tuple(events))
        if len(events) > 1:
            return Receipt("FAILED", "batch_identity_conflict")
        if events[0] == "bad":
            return Receipt("FAILED", "batch_identity_conflict")
        return Receipt("SAVED", inserted_count=1)
    assert bisect_identity_conflicts(("good", "bad"), append) == ("bad",)
    assert len(calls) == 3
    with pytest.raises(RuntimeError, match="store_locked"):
        bisect_identity_conflicts(("a", "b"), lambda _: Receipt("FAILED", "store_locked"))


def test_run_trades_sync_happy_page_reaches_ledger_audit_state_pack_and_receipt(tmp_path):
    scope = "toss-acct-" + "a" * 24
    holding = HoldingItemV1("005930", "1", "70000", "KRW", "KR", None)
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (holding,)),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, "brokerage", 1, "masked")},
    )
    order = Order(
        "order-1", "005930", "BUY", "LIMIT", "DAY", "FILLED", "1", "KRW",
        clock(), OrderExecution(
            "1.00", "70000.0", "70000.00", "10.0", "2.0", clock(), None,
        ),
    )
    events = []

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            events.extend(batch)
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=len(batch),
                duplicate_count=0,
            )

    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    packs = MaterialPackWriter(tmp_path, clock=clock)
    receipts = ReceiptStore(tmp_path / "trades-receipt.json", clock=clock)
    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))
    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=100), session_result=SimpleNamespace(verdicts=()),
        holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((order,), None, False),
        ),
        detail_provider=lambda **kwargs: pytest.fail("detail should not be called"),
        store=Ledger(), scan_state=state, pack_writer=packs,
        receipt_store=receipts, reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        regular_session_dates=Calendar(),
        clock=clock, now=clock(),
        job_lock_path=tmp_path / "trades.lock",
    )
    assert result.status == "saved"
    assert len(events) == 1
    assert events[0].source_record_key == "order-1"
    assert state.read_ingest_audit(("toss-orders-v1", scope, "order-1")) is not None
    assert state.read_state(scope).watermark_date == "2026-07-29"
    assert packs.list_packs(JobKind.TRADES)
    assert receipts.read().last_success.tick_id == 100


def _run_case(tmp_path, orders, *, provider=None, detail_provider=None,
              tick_id=100, lock_path=None):
    scope = "toss-acct-" + "c" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    calls, events, incidents = [], [], []
    class Ledger:
        def append_batch(self, batch, *, import_audit):
            events.extend(batch)
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=len(batch),
                duplicate_count=0,
            )
    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))
    def pages(**kwargs):
        calls.append(kwargs)
        return TossReadOutcome(
            "success", PaginatedOrderResponse(tuple(orders), None, False),
        )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=tick_id),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=provider or pages,
        detail_provider=(detail_provider or
                         (lambda **kwargs: TossReadOutcome("success", orders[0]))),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents.append(args),
        regular_session_dates=Calendar(), clock=clock, now=clock(),
        job_lock_path=lock_path or tmp_path / "lock",
    )
    return result, state, events, calls, incidents, scope


def _order(status, key="order-1", quantity="1", *, complete=True, symbol="SYM",
           currency="USD"):
    return Order(
        key, symbol, "SELL", "MARKET", "DAY", status, "1", currency, clock(),
        OrderExecution(
            quantity, "10" if complete else None, "10" if complete else None,
            None, None, clock() if complete else None, None,
        ),
    )


def test_detail_provider_unexpected_exception_is_fail_closed(tmp_path):
    def detail_stop(**_kwargs):
        raise RuntimeError("kernel_stop")

    with pytest.raises(RuntimeError, match="kernel_stop"):
        _run_case(
            tmp_path, (_order("PARTIAL_FILLED", complete=False),),
            detail_provider=detail_stop,
        )


def test_run_trades_sync_accepts_partial_canceled_fill_and_quarantines_ambiguous_unknown(tmp_path):
    orders = (
        _order("CANCELED", "filled-canceled", "0.5"),
        _order("CANCEL_REJECTED", "ambiguous"),
        _order("FUTURE_STATUS", "unknown"),
    )
    result, state, events, _calls, _incidents, scope = _run_case(tmp_path, orders)
    assert [event.source_record_key for event in events] == ["filled-canceled"]
    assert state.read_quarantine(scope) == ("ambiguous", "unknown")
    assert result.reason == "partial_quarantine"


def test_run_unknown_zero_fill_is_quarantined_and_existing_quarantine_blocks_later_fill(tmp_path):
    result, state, events, _calls, incidents, scope = _run_case(
        tmp_path, (_order("FUTURE_STATUS", "future", "0"),),
    )
    assert result.reason == "partial_quarantine"
    assert events == []
    assert state.read_quarantine(scope) == ("future",)
    assert len(incidents) == 1

    # Explicit reconciliation removal is the only way this key becomes eligible.
    result2, state2, events2, _calls2, incidents2, _scope2 = _run_case(
        tmp_path / "later", (_order("FILLED", "future"),),
    )
    state2.add_quarantine_keys(scope, ("future",), "unknown_order_status")
    # Re-run through the public entrypoint against the already quarantined store.
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    ledger_events = []
    class Ledger:
        def append_batch(self, batch, *, import_audit):
            ledger_events.extend(batch)
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=len(batch),
                duplicate_count=0,
            )
    run_trades_sync(
        tick=SimpleNamespace(tick_id=101),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((_order("FILLED", "future"),), None, False),
        ),
        detail_provider=lambda **kwargs: pytest.fail("detail called"),
        store=Ledger(), scan_state=state2,
        pack_writer=MaterialPackWriter(tmp_path / "later", clock=clock),
        receipt_store=ReceiptStore(tmp_path / "later-receipt.json", clock=clock),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents2.append(args),
        regular_session_dates=type("Calendar", (), {
            "regular_session_dates": lambda self, market, anchor_date, count:
                tuple(anchor_date for _ in range(count))
        })(),
        clock=clock, now=clock(),
        job_lock_path=tmp_path / "later-2.lock",
    )
    assert ledger_events == []
    assert len(incidents2) == 0


@pytest.mark.parametrize("failure_point", ("orders", "ledger", "audit"))
def test_run_detail_success_retry_is_ledger_first_and_survives_failures(
        tmp_path, monkeypatch, failure_point):
    scope = "toss-acct-" + "e" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        scope, "retry-order", Market.US, 1, 1, 1, 0, "2026-07-29", None,
    ))
    appended = []
    class Ledger:
        def append_batch(self, batch, *, import_audit):
            appended.extend(batch)
            if failure_point == "ledger":
                return SimpleNamespace(
                    status="FAILED", reason="store_write_failed",
                    inserted_count=0, duplicate_count=0,
                )
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=len(batch),
                duplicate_count=0,
            )
    if failure_point == "audit":
        monkeypatch.setattr(
            state, "write_ingest_audit",
            lambda row: (_ for _ in ()).throw(RuntimeError("audit crash")),
        )
    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            assert market is Market.US
            return tuple(
                date.fromordinal(anchor_date.toordinal() - offset)
                for offset in range(count - 1, -1, -1)
            )
    from datetime import date
    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: (
            TossReadOutcome("http_500", None) if failure_point == "orders"
            else TossReadOutcome(
                "success", PaginatedOrderResponse((), None, False),
            )
        ),
        detail_provider=lambda **kwargs: TossReadOutcome(
            "success", _order("FILLED", "retry-order"),
        ),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock",
    )
    assert [event.source_record_key for event in appended] == ["retry-order"]
    remaining = state.read_incomplete_retry()
    if failure_point == "orders":
        assert remaining == ()
        assert state.read_ingest_audit(
            ("toss-orders-v1", scope, "retry-order")
        ) is not None
        assert result.status == "partial"
    else:
        assert [row.source_record_key for row in remaining] == ["retry-order"]


def test_run_trades_sync_two_page_cursor_resumes_without_early_watermark(tmp_path):
    pages = [
        PaginatedOrderResponse((_order("FILLED", "page-1"),), "next", True),
        PaginatedOrderResponse((_order("FILLED", "page-2"),), None, False),
    ]
    seen = []
    def provider(**kwargs):
        seen.append(kwargs["cursor"])
        return TossReadOutcome("success", pages[len(seen) - 1])
    result, state, events, _calls, _incidents, scope = _run_case(
        tmp_path, (_order("FILLED"),), provider=provider,
    )
    assert seen == [None, "next"]
    assert [event.source_record_key for event in events] == ["page-1", "page-2"]
    assert state.read_state(scope).pending_cursor is None
    assert state.read_state(scope).watermark_date == "2026-07-29"
    assert result.completeness.pagination_pending is False


def test_run_trades_sync_lock_contention_and_invalid_lock_never_call_provider(tmp_path):
    lock = tmp_path / "held.lock"
    fd = os.open(lock, os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    called = []
    try:
        result, _state, events, _calls, _incidents, _scope = _run_case(
            tmp_path, (_order("FILLED"),), lock_path=lock,
            provider=lambda **kwargs: called.append(kwargs),
        )
        assert result.reason == "skipped_lock_contention"
        assert not events
        assert called == []
    finally:
        os.close(fd)
    target = tmp_path / "target"
    target.touch()
    bad = tmp_path / "bad.lock"
    bad.symlink_to(target)
    result, _state, _events, _calls, _incidents, _scope = _run_case(
        tmp_path / "invalid", (_order("FILLED"),), lock_path=bad,
        provider=lambda **kwargs: pytest.fail("provider called"),
    )
    assert result.reason == "failed_store"


def test_stale_regular_window_precedes_weekly_backfill_and_backfill_never_moves_watermark(
        tmp_path):
    scope = "toss-acct-" + "7" * 24
    holding = HoldingItemV1("SYM", "1", "10", "USD", "US", None)
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (holding,)),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.save_state(ScanStateV1(
        scope, "2026-06-01", None, "2026-06-01", "2026-06-04",
        None, False, None, False, clock().isoformat(),
    ))
    calls = []

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count - 1, -1, -1))

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    def provider(**kwargs):
        calls.append(kwargs)
        return TossReadOutcome("success", PaginatedOrderResponse((), None, False))

    common = dict(
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=provider,
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(),
        clock=clock,
        now=clock(), job_lock_path=tmp_path / "lock",
    )
    run_trades_sync(tick=SimpleNamespace(tick_id=1), **common)
    assert (calls[0]["from_date"], calls[0]["to_date"]) == (
        "2026-06-01", "2026-06-04",
    )
    assert state.read_state(scope).watermark_date == "2026-07-29"

    run_trades_sync(tick=SimpleNamespace(tick_id=2), **common)
    assert calls[1]["from_date"] == "2026-07-11"
    assert state.read_state(scope).watermark_date == "2026-07-29"
    assert state.read_state(scope).last_backfill_date == "2026-07-30"


def test_removed_account_durable_retry_can_ingest_without_keyerror(tmp_path):
    current_scope = "toss-acct-" + "8" * 24
    removed_scope = "toss-acct-" + "9" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(current_scope, 1, ()),), True, clock(),
        {current_scope: AccountScopeIdentityV1(current_scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        removed_scope, "removed-order", Market.US, 1, 1, 1, 0,
        "2026-07-29", None,
    ))
    saved = []

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count - 1, -1, -1))

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            saved.extend(batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((), None, False)),
        detail_provider=lambda **kwargs: TossReadOutcome(
            "success", _order("FILLED", "removed-order")),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock",
    )
    assert result.status == "saved"
    assert [event.account_scope for event in saved] == [removed_scope]
    assert state.read_incomplete_retry() == ()
    assert state.read_ingest_audit(
        ("toss-orders-v1", removed_scope, "removed-order")
    ) is not None


def test_orders_exception_starts_count_toward_ten_call_budget(tmp_path):
    scopes = tuple(
        "toss-acct-" + format(index, "024x") for index in range(12)
    )
    holdings = CurrentHoldingsV1(
        1, tuple(AccountHoldingsV1(scope, index, ()) for index, scope in
                 enumerate(scopes, 1)), True, clock(),
        {
            scope: AccountScopeIdentityV1(scope, None, index, None)
            for index, scope in enumerate(scopes, 1)
        },
    )
    calls = []

    def raising_provider(**kwargs):
        calls.append(kwargs["account_scope"])
        raise RuntimeError("provider start failed")

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=1),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=raising_provider,
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=SimpleNamespace(), scan_state=OrderScanStateStore(
            tmp_path / "scan.sqlite3", clock=clock),
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock",
    )
    assert len(calls) == 10
    assert result.status == "partial"
    assert result.reason == "partial_accounts"


@pytest.mark.parametrize("removed", (True, False))
def test_retry_uses_stored_market_without_removed_scope_or_active_scope_drift(
        tmp_path, removed):
    current_scope = "toss-acct-" + "a" * 24
    removed_scope = "toss-acct-" + "b" * 24
    retry_scope = removed_scope if removed else current_scope
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(current_scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "KR", None),
        )),), True, clock(),
        {current_scope: AccountScopeIdentityV1(current_scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        retry_scope, "stored-us", Market.US, 1, 1, 1, 0,
        "2026-07-29", None,
    ))
    saved = []

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count - 1, -1, -1))

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            saved.extend(batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((), None, False)),
        detail_provider=lambda **kwargs: TossReadOutcome(
            "success", _order("FILLED", "stored-us")),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(),
        clock=clock, now=clock(), job_lock_path=tmp_path / "lock",
    )
    assert result.status == "saved"
    assert saved[0].trade_date.isoformat() == "2026-07-29"
    pack = json.loads(Path(result.pack_path).read_text())
    retry_account = next(
        account for account in pack["payload"]["accounts"]
        if account["account_scope"] == retry_scope
    )
    assert retry_account["inserted"][0]["classified_market"] == "US"
    assert retry_account["inserted"][0]["trade_date"] == "2026-07-29"
    assert state.read_incomplete_retry() == ()
    assert state.read_ingest_audit(
        ("toss-orders-v1", retry_scope, "stored-us")
    ) is not None


def test_age_twenty_retry_is_written_terminal_without_expiry_transaction_gap(
        tmp_path, monkeypatch):
    scope = "toss-acct-" + "6" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, ()),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        scope, "old-order", Market.US, 1, 19, 1, 19, "2026-07-10", None,
    ))
    monkeypatch.setattr(
        state, "expire_incomplete_retry",
        lambda *args: pytest.fail("separate terminal transaction used"),
    )

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count - 1, -1, -1))

    run_trades_sync(
        tick=SimpleNamespace(tick_id=20),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((), None, False)),
        detail_provider=lambda **kwargs: TossReadOutcome("http_500", None),
        store=SimpleNamespace(append_batch=lambda *args, **kwargs: None),
        scan_state=state, pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock,
        now=clock(), job_lock_path=tmp_path / "lock",
    )
    assert state.read_incomplete_retry() == ()
    with sqlite3.connect(state.path) as db:
        assert db.execute(
            "SELECT session_age,terminal_state FROM execution_incomplete_retry"
        ).fetchone() == (20, "INCOMPLETE_EXPIRED")


@pytest.mark.parametrize("table", (
    "order_scan_state", "execution_quarantine", "execution_incomplete_retry",
    "execution_ingest_audit", "execution_market_pending",
))
def test_scan_store_validates_exact_columns_for_every_table(tmp_path, table):
    path = tmp_path / f"{table}.sqlite3"
    OrderScanStateStore(path, clock=clock)
    with sqlite3.connect(path) as db:
        db.execute(f"ALTER TABLE {table} ADD COLUMN surprise TEXT")
    with pytest.raises(ScanStateCorruptError, match="schema"):
        OrderScanStateStore(path, clock=clock)


def test_sixty_identical_public_runs_are_one_insert_59_duplicates_and_keep_top12(
        tmp_path):
    scope = "toss-acct-" + "5" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    packs = MaterialPackWriter(tmp_path, clock=clock)
    receipts = ReceiptStore(tmp_path / "receipt.json", clock=clock)
    seen = set()

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            inserted = sum(event.source_record_key not in seen for event in batch)
            seen.update(event.source_record_key for event in batch)
            return SimpleNamespace(
                status="SAVED", reason="", inserted_count=inserted,
                duplicate_count=len(batch) - inserted,
            )

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    summaries = []
    for tick_id in range(1, 61):
        result = run_trades_sync(
            tick=SimpleNamespace(tick_id=tick_id),
            session_result=SimpleNamespace(verdicts=()), holdings=holdings,
            orders_provider=lambda **kwargs: TossReadOutcome(
                "success", PaginatedOrderResponse(
                    (_order("FILLED", "same"),), None, False)),
            detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
            store=Ledger(), scan_state=state, pack_writer=packs,
            receipt_store=receipts, reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            regular_session_dates=Calendar(), clock=clock,
            now=clock(), job_lock_path=tmp_path / "lock",
        )
        pack = packs.read_and_verify(
            Path(result.pack_path), result.pack_sha256,
        )
        summaries.append(pack.payload["accounts"][0])
    assert sum(len(row["inserted"]) for row in summaries) == 1
    assert sum(len(row["duplicates"]) for row in summaries) == 59
    assert len(packs.list_packs(JobKind.TRADES)) == 12
    assert [row[0] for row in packs.list_packs(JobKind.TRADES)] == list(range(49, 61))
    assert receipts.read().last_success.tick_id == 60


def test_more_than_five_accounts_are_fair_with_fixed_provider_partitions(tmp_path):
    scopes = tuple("toss-acct-" + f"{index:x}" * 24 for index in range(1, 8))
    holdings = CurrentHoldingsV1(
        1, tuple(AccountHoldingsV1(scope, index, ()) for index, scope in enumerate(
            scopes, 1)), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, index, None)
         for index, scope in enumerate(scopes, 1)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    for index, scope in enumerate(scopes[:3]):
        state.add_incomplete_retry(IncompleteRetryRow(
            scope, f"retry-{index}", Market.US, 1, 1, 1, 0,
            "2026-07-29", None,
        ))
    order_calls, detail_calls = [], []

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date - timedelta(days=offset)
                         for offset in range(count - 1, -1, -1))

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    for tick_id in (1, 2):
        per_tick = []

        def provider(**kwargs):
            per_tick.append(kwargs["account_scope"])
            order_calls.append((tick_id, kwargs["account_scope"]))
            return TossReadOutcome(
                "success", PaginatedOrderResponse(
                    (), f"cursor-{len(per_tick)}", True),
            )

        def detail(**kwargs):
            detail_calls.append((tick_id, kwargs["account_scope"]))
            return TossReadOutcome("http_500", None)

        run_trades_sync(
            tick=SimpleNamespace(tick_id=tick_id),
            session_result=SimpleNamespace(verdicts=()), holdings=holdings,
            orders_provider=provider, detail_provider=detail, store=Ledger(),
            scan_state=state, pack_writer=MaterialPackWriter(tmp_path, clock=clock),
            receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
            reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
            regular_session_dates=Calendar(), clock=clock,
            now=clock(), job_lock_path=tmp_path / "lock",
        )
        assert len(per_tick) <= 10
        assert all(per_tick.count(scope) <= 2 for scope in scopes)
        assert set(per_tick) == set(scopes)
    assert all(sum(call[0] == tick for call in detail_calls) <= 2 for tick in (1, 2))


def test_unsupported_currency_pending_incident_once_and_remains_durable(
        tmp_path):
    scope = "toss-acct-" + "4" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, ()),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    packs = MaterialPackWriter(tmp_path, clock=clock)
    receipts = ReceiptStore(tmp_path / "receipt.json", clock=clock)
    incidents, saved = [], []

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            saved.extend(batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    common = dict(
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse(
                (_order("FILLED", "pending", currency="EUR"),), None, False)),
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=Ledger(), scan_state=state, pack_writer=packs, receipt_store=receipts,
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents.append(args),
        regular_session_dates=Calendar(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock",
    )
    first = run_trades_sync(
        tick=SimpleNamespace(tick_id=1), **common,
    )
    assert first.reason == "partial_incomplete"
    assert len(incidents) == 1
    assert [row.order_id for row in state.read_market_pending(scope)] == ["pending"]
    second = run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        **common,
    )
    assert second.reason == "partial_incomplete"
    assert len(incidents) == 1
    assert saved == []
    assert [row.order_id for row in state.read_market_pending(scope)] == ["pending"]
    assert state.read_ingest_audit(("toss-orders-v1", scope, "pending")) is None


def test_pack_before_receipt_orphan_is_invisible_then_same_tick_replay_reaps_it(
        tmp_path):
    scope = "toss-acct-" + "3" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, ()),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    real = MaterialPackWriter(tmp_path, clock=clock)
    receipts = ReceiptStore(tmp_path / "receipt.json", clock=clock)
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)

    class CrashAfterWrite:
        crashed = False
        list_packs = real.list_packs
        reap_orphans = real.reap_orphans

        def write(self, pack):
            path = real.write(pack)
            if not self.crashed:
                self.crashed = True
                raise RuntimeError("injected after pack")
            return path

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    class Ledger:
        seen = set()

        def append_batch(self, batch, *, import_audit):
            inserted = sum(item.source_record_key not in self.seen for item in batch)
            self.seen.update(item.source_record_key for item in batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=inserted,
                                   duplicate_count=len(batch) - inserted)

    common = dict(
        tick=SimpleNamespace(tick_id=7),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse(
                (_order("FILLED", "orphan"),), None, False)),
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=Ledger(), scan_state=state, pack_writer=CrashAfterWrite(),
        receipt_store=receipts, reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        regular_session_dates=Calendar(),
        clock=clock,
        now=clock(), job_lock_path=tmp_path / "lock",
    )
    assert run_trades_sync(**common).reason == "failed_pack"
    assert receipts.read().last_success is None
    assert [row[0] for row in real.list_packs(JobKind.TRADES)] == [7]
    replay = run_trades_sync(**common)
    assert replay.status == "saved"
    assert receipts.read().last_success.tick_id == 7
    assert [row[0] for row in real.list_packs(JobKind.TRADES)] == [7]


@pytest.mark.parametrize("failure_reason,expect_quarantine", (
    ("batch_identity_conflict", True),
    ("store_locked", False),
))
def test_integrated_batch_conflict_quarantines_only_bad_key_and_other_failure_does_not_freeze(
        tmp_path, failure_reason, expect_quarantine):
    good, bad = _order("FILLED", "good"), _order("FILLED", "bad")

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            keys = {event.source_record_key for event in batch}
            failed = (
                failure_reason != "batch_identity_conflict" or "bad" in keys
            )
            return SimpleNamespace(
                status="FAILED" if failed else "SAVED",
                reason=failure_reason if failed else "",
                inserted_count=0 if failed else len(batch), duplicate_count=0,
            )

    scope = "toss-acct-" + "c" * 24
    state = OrderScanStateStore(tmp_path / "case" / "scan.sqlite3", clock=clock)
    holding = HoldingItemV1("SYM", "1", "10", "USD", "US", None)
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (holding,)),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    incidents = []
    run_trades_sync(
        tick=SimpleNamespace(tick_id=1),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((good, bad), None, False)),
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path / "case", clock=clock),
        receipt_store=ReceiptStore(tmp_path / "case-receipt.json", clock=clock),
        reservation_seal=lambda *args: None,
        incident_writer=lambda *args: incidents.append(args),
        regular_session_dates=type("Calendar", (), {
            "regular_session_dates": lambda self, market, anchor_date, count:
                tuple(anchor_date for _ in range(count))
        })(),
        clock=clock, now=clock(),
        job_lock_path=tmp_path / "case.lock",
    )
    current = state.read_state(scope)
    if expect_quarantine:
        assert state.read_quarantine(scope) == ("bad",)
        assert state.read_ingest_audit(("toss-orders-v1", scope, "good")) is not None
        assert current.watermark_frozen is True
        assert current.watermark_date is None
        assert len(incidents) == 1
    else:
        assert state.read_quarantine(scope) == ()
        # A transient store failure leaves the entire scan state unchanged.
        assert current is None
        assert incidents == []


def test_first_failed_receipt_has_no_last_success_and_later_failure_preserves_success(
        tmp_path):
    scope = "toss-acct-" + "2" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, ()),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    receipts = ReceiptStore(tmp_path / "receipt.json", clock=clock)

    class BrokenPacks:
        def list_packs(self, job):
            return []

        def reap_orphans(self, job, keep, refs):
            return 0

        def write(self, pack):
            raise RuntimeError("pack failure")

    class Calendar:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    base = dict(
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: TossReadOutcome(
            "success", PaginatedOrderResponse((), None, False)),
        detail_provider=lambda **kwargs: pytest.fail("DETAIL called"),
        store=SimpleNamespace(), receipt_store=receipts,
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=Calendar(), clock=clock,
        now=clock(), job_lock_path=tmp_path / "lock",
    )
    run_trades_sync(
        tick=SimpleNamespace(tick_id=1),
        scan_state=OrderScanStateStore(tmp_path / "scan1.sqlite3", clock=clock),
        pack_writer=BrokenPacks(), **base,
    )
    assert receipts.read().attempt.status.value == "failed"
    assert receipts.read().last_success is None
    run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        scan_state=OrderScanStateStore(tmp_path / "scan2.sqlite3", clock=clock),
        pack_writer=MaterialPackWriter(tmp_path, clock=clock), **base,
    )
    assert receipts.read().last_success.tick_id == 2
    run_trades_sync(
        tick=SimpleNamespace(tick_id=3),
        scan_state=OrderScanStateStore(tmp_path / "scan3.sqlite3", clock=clock),
        pack_writer=BrokenPacks(), **base,
    )
    assert receipts.read().attempt.status.value == "failed"
    assert receipts.read().last_success.tick_id == 2


def _f6_vertical(tmp_path, *, ledger=None, now=clock):
    scope = "toss-acct-" + "f" * 24
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    kernel = OpsKernelStore(tmp_path / "ops.sqlite3")
    invocation = "f" * 32
    params = {"account_scope": scope, "window_from": "2026-07-30",
              "window_to": "2026-07-30", "request_cursor": None}
    kernel.plan_requests(ProducerRole.COLLECTOR, 77, "TRADES", ({
        "ordinal": 0, "operation": "ORDERS_PAGE",
        "params_digest": canonical_params_digest("ORDERS_PAGE", params),
        "method": "GET", "host": "openapi.tossinvest.com",
        "normalized_path_template": "/api/v1/orders", "account_ordinal": None,
        "market": None, "local_trade_date": None, "canonical_params": params,
    },), invocation_id=invocation, now=clock())
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    wires, details, events = [], [], []

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            events.extend(batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    common = dict(
        tick=SimpleNamespace(tick_id=77), session_result=SimpleNamespace(verdicts=()),
        holdings=holdings,
        orders_provider=lambda **kwargs: (wires.append(kwargs) or TossReadOutcome(
            "success", PaginatedOrderResponse((_order("FILLED", "f6-order"),), None, False))),
        detail_provider=lambda **kwargs: (details.append(kwargs) or pytest.fail("DETAIL called")),
        store=ledger or Ledger(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=type("Calendar", (), {
            "regular_session_dates": lambda self, market, anchor_date, count:
                tuple(anchor_date for _ in range(count))
        })(), clock=clock, now=now(),
        job_lock_path=tmp_path / "lock", ops_kernel=kernel, invocation_id=invocation,
    )
    return common, kernel, state, wires, details, events, scope


def test_f6_fresh_planned_starts_before_wire_and_replay_uses_persisted_window(tmp_path):
    common, kernel, state, wires, details, events, scope = _f6_vertical(tmp_path)
    common["orders_provider"] = lambda **kwargs: (
        pytest.fail("wire before STARTED")
        if kernel.read_planned_requests(77, "TRADES")[0]["state"] != "STARTED"
        else (wires.append(kwargs) or TossReadOutcome(
            "success", PaginatedOrderResponse((_order("FILLED", "f6-order"),), None, False)))
    )
    result = run_trades_sync(**common)
    assert result.status == "saved"
    assert len(wires) == 1 and details == [] and len(events) == 1
    assert kernel.read_planned_requests(77, "TRADES")[0]["exit"] == "OK"
    assert state.read_state(scope).watermark_date == "2026-07-29"


def test_f7_page2_is_planned_only_from_page1_response_then_started_wired_and_settled(tmp_path):
    common, kernel, state, wires, _details, events, scope = _f6_vertical(tmp_path)
    pages = (
        PaginatedOrderResponse((_order("FILLED", "f7-page-1"),), "next", True),
        PaginatedOrderResponse((_order("FILLED", "f7-page-2"),), None, False),
    )
    def provider(**kwargs):
        rows = kernel.read_planned_requests(77, "TRADES")
        assert rows[len(wires)]["state"] == "STARTED"
        wires.append(kwargs)
        return TossReadOutcome("success", pages[len(wires) - 1])
    common["orders_provider"] = provider
    result = run_trades_sync(**common)
    rows = kernel.read_planned_requests(77, "TRADES")
    assert [wire["cursor"] for wire in wires] == [None, "next"]
    assert [(row["ordinal"], row["state"], row["exit"]) for row in rows] == [
        (0, "SETTLED", "OK"), (1, "SETTLED", "OK")]
    assert [event.source_record_key for event in events] == ["f7-page-1", "f7-page-2"]
    assert state.read_state(scope).pending_cursor is None
    assert result.status == "saved"


def test_f7_third_page_reopen_stops_pre_wire_with_incident(tmp_path):
    common, kernel, _state, wires, _details, _events, scope = _f6_vertical(tmp_path)
    incidents = []
    common["incident_writer"] = lambda *args: incidents.append(args)
    pages = (
        PaginatedOrderResponse((_order("FILLED", "f7-p1"),), "next", True),
        PaginatedOrderResponse((_order("FILLED", "f7-p2"),), "third", True),
    )

    def provider(**kwargs):
        wires.append(kwargs)
        return TossReadOutcome("success", pages[len(wires) - 1])

    common["orders_provider"] = provider
    first = run_trades_sync(**common)
    assert first.reason == "partial_pagination"
    assert len(wires) == 2
    reopened = {**common, "ops_kernel": OpsKernelStore(kernel.path),
                "orders_provider": lambda **kwargs: pytest.fail("third page reached wire")}
    denied = run_trades_sync(**reopened)
    assert (denied.status, denied.reason) == ("failed", "failed_ceiling")
    assert reopened["receipt_store"].read().attempt.reason == "failed_ceiling"
    assert len(wires) == 2
    assert incidents == [("orders-page-ceiling", frozenset({scope}))]
    assert len(OpsKernelStore(kernel.path).read_planned_requests(77, "TRADES")) == 2


def test_f6_transient_ledger_failure_stays_replayable_and_mutates_no_scan_state(tmp_path):
    class LockedLedger:
        def append_batch(self, batch, *, import_audit):
            error = sqlite3.OperationalError("database is locked")
            error.sqlite_errorcode = sqlite3.SQLITE_BUSY
            raise error
    common, kernel, state, wires, _details, _events, scope = _f6_vertical(
        tmp_path, ledger=LockedLedger())
    result = run_trades_sync(**common)
    row = kernel.read_planned_requests(77, "TRADES")[0]
    assert result.status == "partial"
    assert row["state"] == "APPLYING" and row["exit"] is None
    assert state.read_state(scope) is None
    assert len(wires) == 1


@pytest.mark.parametrize("terminal_exit", ("OK", "STORE_APPLY_FAILED"))
def test_f6_terminal_rows_never_reapply(tmp_path, terminal_exit):
    common, kernel, state, wires, details, events, scope = _f6_vertical(tmp_path)
    run_trades_sync(**common)
    if terminal_exit == "STORE_APPLY_FAILED":
        with sqlite3.connect(kernel.path) as db:
            db.execute("UPDATE planned_requests SET exit='STORE_APPLY_FAILED' WHERE tick_id=77")
    before = (len(wires), len(details), len(events), state.read_state(scope))
    run_trades_sync(**{**common, "now": datetime(2026, 8, 5, tzinfo=timezone.utc)})
    assert (len(wires), len(details), len(events), state.read_state(scope)) == before


class _ProcessCrash(BaseException):
    pass


@pytest.mark.parametrize("phase", ("checkpointed", "ledger", "scan"))
def test_f6_production_vertical_process_crash_reopens_and_converges_without_wire(
        tmp_path, phase):
    ledger_path = tmp_path / "ledger" / "trades.sqlite3"
    ledger = TradeLedgerStore(ledger_path, clock=clock, id_factory=lambda: "batch-1")
    common, kernel, state, wires, details, _events, scope = _f6_vertical(
        tmp_path, ledger=ledger)

    class Proxy:
        def __init__(self, target):
            self.target = target
        def __getattr__(self, name):
            return getattr(self.target, name)

    if phase == "checkpointed":
        class CrashKernel(Proxy):
            def checkpoint_orders_page(self, *args, **kwargs):
                result = self.target.checkpoint_orders_page(*args, **kwargs)
                raise _ProcessCrash("after checkpoint")
        common["ops_kernel"] = CrashKernel(kernel)
    elif phase == "ledger":
        class CrashLedger(Proxy):
            def append_batch(self, *args, **kwargs):
                result = self.target.append_batch(*args, **kwargs)
                raise _ProcessCrash("after ledger")
        common["store"] = CrashLedger(ledger)
    else:
        class CrashScan(Proxy):
            def save_state(self, value):
                self.target.save_state(value)
                raise _ProcessCrash("after scan")
        common["scan_state"] = CrashScan(state)

    with pytest.raises(_ProcessCrash):
        run_trades_sync(**common)
    persisted = OpsKernelStore(kernel.path).begin_page_apply(
        ProducerRole.COLLECTOR, 77, 0, invocation_id="e" * 32, now=clock(),
    )["orders_page"]
    assert (persisted["window_from"], persisted["window_to"],
            persisted["request_cursor"], persisted["has_next"],
            persisted["next_cursor"]) == (
        "2026-07-30", "2026-07-30", None, False, None,
    )
    replay = {**common,
        "ops_kernel": OpsKernelStore(kernel.path),
        "scan_state": OrderScanStateStore(state.path, clock=clock),
        "store": TradeLedgerStore(ledger_path, clock=clock, id_factory=lambda: "batch-2"),
        "invocation_id": "e" * 32,
        "now": datetime(2026, 8, 5, tzinfo=timezone.utc),
    }
    before = (len(wires), len(details))
    result = run_trades_sync(**replay)
    assert (len(wires), len(details)) == before
    assert result.status == "saved"
    assert replay["ops_kernel"].read_planned_requests(77, "TRADES")[0]["exit"] == "OK"
    assert [row.source_record_key for row in replay["store"].read_scope(
        source="toss-orders-v1", account_scope=scope)] == ["f6-order"]
    assert replay["scan_state"].read_ingest_audit(
        ("toss-orders-v1", scope, "f6-order")) is not None
    assert replay["scan_state"].read_state(scope).watermark_date == "2026-07-29"


@pytest.mark.parametrize("code,exit", (
    ("http_500", "PROVIDER_ERROR"), ("schema_mismatch", "PARSE_FAIL"),
    ("http_429", "RATE_LIMITED"), ("auth_rejection", "AUTH_FAIL"),
    ("redirect", "REDIRECT"), ("transport_error", "TRANSPORT_FAIL"),
))
def test_f6_provider_non_success_settles_and_same_tick_rerun_skips(code, exit, tmp_path):
    common, kernel, _state, wires, details, events, _scope = _f6_vertical(tmp_path)
    common["orders_provider"] = lambda **kwargs: (
        wires.append(kwargs) or TossReadOutcome(code, None))
    first = run_trades_sync(**common)
    assert first.status == "partial"
    assert kernel.read_planned_requests(77, "TRADES")[0]["exit"] == exit
    before = (len(wires), len(details), len(events))
    second = run_trades_sync(**common)
    assert second.status in ("saved", "partial")
    assert (len(wires), len(details), len(events)) == before


def test_f6_provider_exception_settles_coherently_and_rerun_is_uncaught_zero(tmp_path):
    common, kernel, _state, wires, details, events, _scope = _f6_vertical(tmp_path)
    def provider(**kwargs):
        wires.append(kwargs)
        raise RuntimeError("typed provider failure")
    common["orders_provider"] = provider
    first = run_trades_sync(**common)
    assert first.status == "partial" and first.reason == "partial_accounts"
    assert kernel.read_planned_requests(77, "TRADES")[0]["exit"] == "PROVIDER_ERROR"
    before = (len(wires), len(details), len(events))
    run_trades_sync(**common)
    assert (len(wires), len(details), len(events)) == before


def test_f6_deterministic_store_failure_records_exact_incident_and_terminalizes(tmp_path):
    class FailedLedger:
        def append_batch(self, batch, *, import_audit):
            return SimpleNamespace(status="FAILED", reason="store_write_failed",
                                   inserted_count=0, duplicate_count=0)
    incidents = []
    common, kernel, state, wires, details, events, scope = _f6_vertical(
        tmp_path, ledger=FailedLedger())
    common["incident_writer"] = lambda *args: incidents.append(args)
    result = run_trades_sync(**common)
    row = kernel.read_planned_requests(77, "TRADES")[0]
    page_digest = incidents[0][1][2]
    assert result.status == "partial"
    assert (row["state"], row["exit"]) == ("SETTLED", "STORE_APPLY_FAILED")
    assert incidents == [("store-apply-failed",
                          (77, 0, page_digest, "ledger_append", "STORE_APPLY_FAILED"))]
    assert state.read_state(scope) is None
    before = (len(wires), len(details), len(events), tuple(incidents))
    run_trades_sync(**common)
    assert (len(wires), len(details), len(events), tuple(incidents)) == before


@pytest.mark.parametrize("denial", (
    OpsKernelBudgetExceeded, OpsKernelCooldownActive, OpsKernelConflictError,
))
def test_f10_trades_typed_gate_denial_is_failed_ceiling_before_transport(
        tmp_path, denial):
    common, kernel, _state, wires, _details, _events, _scope = _f6_vertical(tmp_path)
    common["orders_provider"] = lambda **kwargs: (_ for _ in ()).throw(
        denial("runtime request denied"))
    result = run_trades_sync(**common)
    receipt = common["receipt_store"].read()
    assert wires == []
    assert [row for row in kernel.read_planned_requests(77, "TRADES")
            if row["state"] in ("PLANNED", "ARMED")] == []
    assert (result.status, result.reason) == ("failed", "failed_ceiling")
    assert (receipt.attempt.status.value, receipt.attempt.reason) == (
        "failed", "failed_ceiling")


@pytest.mark.parametrize("denial", (
    OpsKernelBudgetExceeded, OpsKernelCooldownActive, OpsKernelConflictError,
))
def test_f10_incomplete_retry_detail_denial_stops_before_orders_wire(tmp_path, denial):
    scope = "toss-acct-" + "d" * 24
    state = OrderScanStateStore(tmp_path / "scan.sqlite3", clock=clock)
    state.add_incomplete_retry(IncompleteRetryRow(
        scope, "retry-order", Market.US, 1, 1, 1, 0, "2026-07-29", None,
    ))
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )),), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    wires = []
    receipts = ReceiptStore(tmp_path / "receipt.json", clock=clock)
    kernel = OpsKernelStore(tmp_path / "ops.sqlite3")
    result = run_trades_sync(
        tick=SimpleNamespace(tick_id=2),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: wires.append(kwargs),
        detail_provider=lambda **kwargs: (_ for _ in ()).throw(
            denial("detail request denied")),
        store=SimpleNamespace(), scan_state=state,
        pack_writer=MaterialPackWriter(tmp_path / "packs", clock=clock),
        receipt_store=receipts, reservation_seal=lambda *args: None,
        incident_writer=lambda *args: None,
        regular_session_dates=type("Calendar", (), {
            "regular_session_dates": lambda self, market, anchor_date, count:
                tuple(anchor_date - timedelta(days=offset)
                      for offset in range(count - 1, -1, -1))
        })(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock",
    )
    assert wires == []
    assert kernel.read_planned_requests(2, "TRADES") == ()
    assert (result.status, result.reason) == ("failed", "failed_ceiling")
    assert receipts.read().attempt.reason == "failed_ceiling"


@pytest.mark.parametrize("denial", (
    OpsKernelBudgetExceeded, OpsKernelCooldownActive, OpsKernelConflictError,
))
def test_f10_normal_detail_denial_stops_without_subsequent_orders_wire(tmp_path, denial):
    common, _kernel, _state, wires, details, _events, _scope = _f6_vertical(tmp_path)
    common.pop("ops_kernel")
    common.pop("invocation_id")
    common["orders_provider"] = lambda **kwargs: (
        wires.append(kwargs) or TossReadOutcome(
            "success", PaginatedOrderResponse(
                (_order("FILLED", "needs-detail", complete=False),), "next", True)))
    common["detail_provider"] = lambda **kwargs: (
        details.append(kwargs) or (_ for _ in ()).throw(denial("detail request denied")))
    kernel = OpsKernelStore(tmp_path / "empty-ops.sqlite3")
    result = run_trades_sync(**common)
    assert len(wires) == 1
    assert len(details) == 1
    assert kernel.read_planned_requests(77, "TRADES") == ()
    assert (result.status, result.reason) == ("failed", "failed_ceiling")
    assert common["receipt_store"].read().attempt.reason == "failed_ceiling"


def test_f10_store_apply_failed_parent_has_no_child_and_later_scope_runs(tmp_path):
    scopes = ("toss-acct-" + "1" * 24, "toss-acct-" + "2" * 24)
    common, kernel, wires, _details, _events = _n5_multi_account_case(
        tmp_path, scopes, scopes, tick_id=78)
    common["holdings"] = CurrentHoldingsV1(
        1, tuple(AccountHoldingsV1(scope, index, (
            HoldingItemV1("SYM", "1", "10", "USD", "US", None),
        )) for index, scope in enumerate(scopes, 1)), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, index, None)
         for index, scope in enumerate(scopes, 1)},
    )
    real_ledger = TradeLedgerStore(
        tmp_path / "ledger.sqlite3", clock=clock, id_factory=lambda: "batch")

    class FailFirstApply:
        def __init__(self):
            self.calls = 0
        def append_batch(self, batch, *, import_audit):
            self.calls += 1
            if self.calls == 1:
                return SimpleNamespace(status="FAILED", reason="store_write_failed",
                                       inserted_count=0, duplicate_count=0)
            return real_ledger.append_batch(batch, import_audit=import_audit)

    common["store"] = FailFirstApply()
    common["orders_provider"] = lambda **kwargs: (
        wires.append(kwargs) or TossReadOutcome(
            "success", PaginatedOrderResponse(
                (_order("FILLED", "first"),) if kwargs["account_scope"] == scopes[0]
                else (_order("FILLED", "second"),),
                "forbidden-child" if kwargs["account_scope"] == scopes[0] else None,
                kwargs["account_scope"] == scopes[0],
            )))
    incidents = []
    common["incident_writer"] = lambda *args: incidents.append(args)
    result = run_trades_sync(**common)
    rows = kernel.read_planned_requests(78, "TRADES")

    assert result.status == "partial"
    assert [wire["account_scope"] for wire in wires] == list(scopes)
    assert [(row["ordinal"], row["state"], row["exit"]) for row in rows] == [
        (0, "SETTLED", "STORE_APPLY_FAILED"), (1, "SETTLED", "OK")]
    assert len(rows) == 2
    assert [row.source_record_key for row in real_ledger.read_scope(
        source="toss-orders-v1", account_scope=scopes[1])] == ["second"]
    assert len(incidents) == 1 and incidents[0][0] == "store-apply-failed"


def test_f6_incident_failure_stops_and_leaves_applying_without_scan_mutation(tmp_path):
    class FailedLedger:
        def append_batch(self, batch, *, import_audit):
            return SimpleNamespace(status="FAILED", reason="store_write_failed",
                                   inserted_count=0, duplicate_count=0)
    common, kernel, state, _wires, _details, _events, scope = _f6_vertical(
        tmp_path, ledger=FailedLedger())
    common["incident_writer"] = lambda *args: (_ for _ in ()).throw(OSError("incident"))
    with pytest.raises(RuntimeError, match="store_corrupt: store apply audit failed"):
        run_trades_sync(**common)
    row = kernel.read_planned_requests(77, "TRADES")[0]
    assert (row["state"], row["exit"]) == ("APPLYING", None)
    assert state.read_state(scope) is None


def test_f6_production_trade_ledger_store_locked_receipt_is_transient(tmp_path):
    ledger_path = tmp_path / "ledger" / "trades.sqlite3"
    ledger = TradeLedgerStore(ledger_path, clock=clock, id_factory=lambda: "seed")
    seed, _kernel, _state, _wires, _details, _events, _scope = _f6_vertical(
        tmp_path / "seed", ledger=ledger)
    assert run_trades_sync(**seed).status == "saved"
    locked = sqlite3.connect(ledger_path, timeout=0, isolation_level=None)
    locked.execute("BEGIN EXCLUSIVE")
    try:
        common, kernel, state, wires, _details, _events, scope = _f6_vertical(
            tmp_path / "locked",
            ledger=TradeLedgerStore(ledger_path, clock=clock, id_factory=lambda: "locked"),
        )
        result = run_trades_sync(**common)
        row = kernel.read_planned_requests(77, "TRADES")[0]
        assert result.status == "partial"
        assert (row["state"], row["exit"]) == ("APPLYING", None)
        assert state.read_state(scope) is None
        assert len(wires) == 1
    finally:
        locked.rollback()
        locked.close()


def test_f6_scan_state_locked_error_is_transient_and_keeps_applying(tmp_path):
    common, kernel, state, wires, _details, _events, scope = _f6_vertical(tmp_path)
    original = state.write_ingest_audit
    state.write_ingest_audit = lambda row: (_ for _ in ()).throw(
        ScanStateLockedError("database locked"))
    try:
        result = run_trades_sync(**common)
    finally:
        state.write_ingest_audit = original
    row = kernel.read_planned_requests(77, "TRADES")[0]
    assert result.status == "partial"
    assert (row["state"], row["exit"]) == ("APPLYING", None)
    assert state.read_state(scope) is None
    assert len(wires) == 1


def _n5_multi_account_case(tmp_path, scopes, planned_scopes, *, tick_id=78):
    holdings = CurrentHoldingsV1(
        1, tuple(AccountHoldingsV1(scope, index, ()) for index, scope in
                 enumerate(scopes, 1)), True, clock(),
        {scope: AccountScopeIdentityV1(scope, None, index, None)
         for index, scope in enumerate(scopes, 1)},
    )
    kernel = OpsKernelStore(tmp_path / "ops.sqlite3")
    invocation = "d" * 32
    rows = []
    for ordinal, scope in enumerate(planned_scopes):
        params = {"account_scope": scope, "window_from": "2026-07-30",
                  "window_to": "2026-07-30", "request_cursor": None}
        rows.append({
            "ordinal": ordinal, "operation": "ORDERS_PAGE",
            "params_digest": canonical_params_digest("ORDERS_PAGE", params),
            "method": "GET", "host": "openapi.tossinvest.com",
            "normalized_path_template": "/api/v1/orders", "account_ordinal": None,
            "market": None, "local_trade_date": None, "canonical_params": params,
        })
    kernel.plan_requests(ProducerRole.COLLECTOR, tick_id, "TRADES", tuple(rows),
                         invocation_id=invocation, now=clock())
    wires, details, events = [], [], []

    class Ledger:
        def append_batch(self, batch, *, import_audit):
            events.extend(batch)
            return SimpleNamespace(status="SAVED", reason="",
                                   inserted_count=len(batch), duplicate_count=0)

    common = dict(
        tick=SimpleNamespace(tick_id=tick_id),
        session_result=SimpleNamespace(verdicts=()), holdings=holdings,
        orders_provider=lambda **kwargs: (wires.append(kwargs) or TossReadOutcome(
            "success", PaginatedOrderResponse((), None, False))),
        detail_provider=lambda **kwargs: (details.append(kwargs) or pytest.fail("DETAIL called")),
        store=Ledger(), scan_state=OrderScanStateStore(
            tmp_path / "scan.sqlite3", clock=clock),
        pack_writer=MaterialPackWriter(tmp_path, clock=clock),
        receipt_store=ReceiptStore(tmp_path / "receipt.json", clock=clock),
        reservation_seal=lambda *args: None, incident_writer=lambda *args: None,
        regular_session_dates=type("Calendar", (), {
            "regular_session_dates": lambda self, market, anchor_date, count:
                tuple(anchor_date for _ in range(count))
        })(), clock=clock, now=clock(),
        job_lock_path=tmp_path / "lock", ops_kernel=kernel,
        invocation_id=invocation,
    )
    return common, kernel, wires, details, events


def _request_day_count(kernel):
    with sqlite3.connect(kernel.path) as db:
        row = db.execute(
            "SELECT provider_started_count FROM request_day_counters"
        ).fetchone()
    return 0 if row is None else row[0]


def test_n5_planned_window_mismatch_rejects_before_start_and_wire(tmp_path):
    scope = "toss-acct-" + "1" * 24
    common, kernel, wires, details, events = _n5_multi_account_case(
        tmp_path, (scope,), (scope,),
    )
    with sqlite3.connect(kernel.path) as db:
        db.execute(
            "UPDATE planned_requests SET params_digest=? WHERE tick_id=78 AND ordinal=0",
            (canonical_params_digest("ORDERS_PAGE", {
                "account_scope": scope, "window_from": "2026-07-29",
                "window_to": "2026-07-30", "request_cursor": "planned-cursor",
            }),),
        )
    result = run_trades_sync(**common)
    row = kernel.read_planned_requests(78, "TRADES")[0]
    assert result.status == "partial" and result.reason == "partial_accounts"
    assert wires == [] and details == [] and events == []
    assert row["state"] == "PLANNED" and row["started_at"] is None
    assert _request_day_count(kernel) == 0
    assert common["scan_state"].read_state(scope) is None
    assert common["receipt_store"].read().attempt.status.value == "partial"


def test_n5_two_account_rotation_mismatch_rejects_each_planned_ordinal_pre_wire(tmp_path):
    scopes = ("toss-acct-" + "2" * 24, "toss-acct-" + "3" * 24)
    common, kernel, wires, details, events = _n5_multi_account_case(
        tmp_path, scopes, scopes, tick_id=79,
    )
    result = run_trades_sync(**common)
    rows = kernel.read_planned_requests(79, "TRADES")
    assert result.status == "partial" and result.reason == "partial_accounts"
    assert wires == [] and details == [] and events == []
    assert [(row["ordinal"], row["state"], row["started_at"]) for row in rows] == [
        (0, "PLANNED", None), (1, "PLANNED", None),
    ]
    assert _request_day_count(kernel) == 0
    assert all(common["scan_state"].read_state(scope) is None for scope in scopes)
    assert common["receipt_store"].read().attempt.status.value == "partial"


def test_n5_reopen_rebinds_replayed_scope_then_rejects_live_ordinal_scope_mismatch(
        tmp_path):
    scopes = ("toss-acct-" + "4" * 24, "toss-acct-" + "5" * 24)
    common, kernel, wires, details, events = _n5_multi_account_case(
        tmp_path, scopes, scopes, tick_id=78,
    )

    class CrashAfterCheckpoint:
        def __getattr__(self, name):
            return getattr(kernel, name)

        def checkpoint_orders_page(self, *args, **kwargs):
            result = kernel.checkpoint_orders_page(*args, **kwargs)
            raise _ProcessCrash("after ordinal zero checkpoint")

    with pytest.raises(_ProcessCrash):
        run_trades_sync(**{**common, "ops_kernel": CrashAfterCheckpoint()})
    assert [row["state"] for row in kernel.read_planned_requests(78, "TRADES")] == [
        "CHECKPOINTED", "PLANNED",
    ]
    before_wire = (len(wires), len(details), _request_day_count(kernel))

    reversed_accounts = tuple(
        AccountHoldingsV1(account.account_scope, ordinal, account.holdings)
        for ordinal, account in enumerate(reversed(common["holdings"].accounts), 1)
    )
    reversed_holdings = CurrentHoldingsV1(
        1, reversed_accounts, True, clock(), common["holdings"].identity_map,
    )
    reopened_kernel = OpsKernelStore(kernel.path)
    reopened_state = OrderScanStateStore(common["scan_state"].path, clock=clock)
    replay = run_trades_sync(**{
        **common, "holdings": reversed_holdings, "ops_kernel": reopened_kernel,
        "scan_state": reopened_state, "invocation_id": "e" * 32,
    })
    rows = reopened_kernel.read_planned_requests(78, "TRADES")
    assert replay.status == "partial" and replay.reason == "partial_accounts"
    assert (len(wires), len(details), _request_day_count(reopened_kernel)) == before_wire
    assert [(row["ordinal"], row["state"], row["started_at"] is None) for row in rows] == [
        (0, "SETTLED", False), (1, "PLANNED", True),
    ]
    assert reopened_state.read_state(scopes[0]) is not None
    assert reopened_state.read_state(scopes[1]) is None
    assert events == []
    assert common["receipt_store"].read().attempt.status.value == "partial"
