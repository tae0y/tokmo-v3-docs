"""Incremental completed-execution ingestion, mask-free (Plan §17)."""

from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.programs.execution_detection import (
    PulseOrdersPageOutcomeV1,
    run_pulse_execution,
)
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.pulse_policy import pulse_tick_v1
from tokmo.values.toss_orders import Order, OrderExecution


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


TICK = pulse_tick_v1("pulse-60s-v1", _utc("2026-08-10T01:00:00Z"))
NEXT_TICK = pulse_tick_v1("pulse-60s-v1", _utc("2026-08-10T01:01:00Z"))
NOW = _utc("2026-08-10T01:00:01Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
TOSS_SCOPE_2 = "toss-acct-" + "b" * 24
PENSION_SCOPE = "pension-excel-" + "c" * 24


class _Refs:
    def __init__(self, refs):
        self._refs = refs

    def scopes(self):
        return frozenset(self._refs)

    def reveal(self, scope):
        return self._refs.get(scope)


def _holdings(scopes=(TOSS_SCOPE, PENSION_SCOPE)):
    identity = {}
    accounts = []
    for ordinal, scope in enumerate(scopes, start=1):
        label = ("pension-excel-v1" if scope.startswith("pension")
                 else "personal")
        identity[scope] = AccountScopeIdentityV1(scope, label, ordinal, None)
        accounts.append(AccountHoldingsV1(scope, ordinal, (
            HoldingItemV1("005930", "10", "60000", "KRW", "KR", "market"),
        )))
    return CurrentHoldingsV1(
        1, tuple(accounts), True, _utc("2026-08-10T00:55:00Z"), identity)


def _order(order_id="ord-001", *, status="FILLED", currency="KRW",
           filled_quantity="3", price="70000", amount="210000",
           filled_at="2026-08-10T00:59:00Z", complete=True):
    execution = OrderExecution(
        filled_quantity=filled_quantity,
        average_filled_price=price if complete else None,
        filled_amount=amount if complete else None,
        commission=None, tax=None,
        filled_at=None if filled_at is None else _utc(filled_at),
        settlement_date=None,
    )
    return Order(
        order_id=order_id, symbol="005930", side="SELL",
        order_type="LIMIT", time_in_force="DAY", status=status,
        quantity="3", currency=currency,
        ordered_at=_utc("2026-08-10T00:58:00Z"), execution=execution,
    )


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, operation, native_identity):
        self.calls.append((operation, native_identity))
        return len(self.calls)


class _PageProvider:
    """Scripted per-scope pages; records every call's arguments."""

    def __init__(self, script):
        self.script = dict(script)
        self.calls = []

    def __call__(self, *, account_scope, native_ref, status, from_date,
                 to_date, cursor, limit):
        self.calls.append({
            "account_scope": account_scope, "native_ref": native_ref,
            "status": status, "from_date": from_date, "to_date": to_date,
            "cursor": cursor, "limit": limit,
        })
        value = self.script.get((account_scope, cursor))
        if value is None:
            return PulseOrdersPageOutcomeV1("success", (), None, False)
        return value


def _read_ledger(ledger, scope):
    try:
        return ledger.read_scope(source="toss-orders-v1", account_scope=scope)
    except Exception:
        return ()


_BATCH_COUNTER = iter(range(1, 1_000_000))


def _run(tmp_path, *, tick=TICK, holdings="default", refs=None, pages=None,
         details=None, recorder=None, now=NOW):
    ledger = TradeLedgerStore(
        tmp_path / "stores" / "trade_ledger.sqlite3", clock=lambda: now,
        id_factory=lambda: f"batch-{next(_BATCH_COUNTER):06d}")
    scan_state = OrderScanStateStore(
        tmp_path / "stores" / "order_scan_state.sqlite3", clock=lambda: now)
    provider = pages if isinstance(pages, _PageProvider) else _PageProvider(
        pages or {})
    detail_calls = []

    def order_detail_fetch(*, account_scope, native_ref, order_id):
        detail_calls.append(order_id)
        if details and order_id in details:
            return details[order_id]
        return None

    result = run_pulse_execution(
        tick=tick,
        holdings_reader=lambda: (_holdings() if holdings == "default"
                                 else holdings),
        native_refs_reader=lambda: refs if refs is not None else _Refs(
            {TOSS_SCOPE: "native-ref-1"}),
        token_provider=lambda: "token-value",
        orders_page_fetch=provider,
        order_detail_fetch=order_detail_fetch,
        ledger_store=ledger,
        scan_state_store=scan_state,
        clock=lambda: now,
        now=now,
        wire_recorder=recorder if recorder is not None else _Recorder(),
    )
    return result, ledger, scan_state, provider, detail_calls


def test_orders_page_targets_only_live_order_capable_toss_scopes(tmp_path):
    result, _ledger, _state, provider, _details = _run(tmp_path)
    scopes_called = {call["account_scope"] for call in provider.calls}
    assert scopes_called == {TOSS_SCOPE}
    assert result.scope_status[PENSION_SCOPE] == "NOT_APPLICABLE"
    assert result.status == "saved"
    # Pension scope never justifies partial_accounts (§17.4).
    assert result.reason != "partial_accounts"


def test_orders_page_is_called_with_closed_status_and_window(tmp_path):
    _result, _ledger, _state, provider, _details = _run(tmp_path)
    call = provider.calls[0]
    assert call["status"] == "CLOSED"
    assert call["native_ref"] == "native-ref-1"
    assert call["from_date"] <= call["to_date"]


def test_toss_scope_without_native_ref_is_partial_accounts(tmp_path):
    holdings = _holdings((TOSS_SCOPE, TOSS_SCOPE_2, PENSION_SCOPE))
    result, _ledger, _state, provider, _details = _run(
        tmp_path, holdings=holdings, refs=_Refs({TOSS_SCOPE: "native-ref-1"}))
    assert result.scope_status[TOSS_SCOPE_2] == "account_unmapped"
    assert result.status == "partial"
    assert result.reason == "partial_accounts"
    assert {call["account_scope"] for call in provider.calls} == {TOSS_SCOPE}


def test_completeness_denominator_is_source_specific(tmp_path):
    holdings = _holdings((TOSS_SCOPE, TOSS_SCOPE_2, PENSION_SCOPE))
    result, _ledger, _state, _provider, _details = _run(
        tmp_path, holdings=holdings, refs=_Refs({TOSS_SCOPE: "native-ref-1"}))
    assert result.completeness.obligated_scopes == 2  # toss scopes only
    assert result.completeness.scanned_scopes == 1
    assert result.completeness.failed_scopes == 1
    assert result.completeness.not_applicable_scopes == 1


def test_new_filled_order_appends_exactly_one_ledger_event(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order(),), None, False)}
    result, ledger, _state, _provider, detail_calls = _run(
        tmp_path, pages=pages)
    assert result.inserted_count == 1
    assert len(result.new_events) == 1
    event = result.new_events[0]
    assert event.source_record_key == "ord-001"
    assert event.account_scope == TOSS_SCOPE
    assert detail_calls == []  # complete order needs no detail
    rows = _read_ledger(ledger, TOSS_SCOPE)
    assert len(rows) == 1


def test_duplicate_rescan_appends_zero_and_emits_no_event(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order(),), None, False)}
    first, _ledger, _state, _provider, _d = _run(tmp_path, pages=pages)
    assert first.inserted_count == 1
    second, ledger, _state2, _provider2, _d2 = _run(
        tmp_path, tick=NEXT_TICK, pages=pages,
        now=_utc("2026-08-10T01:01:01Z"))
    assert second.inserted_count == 0
    assert second.duplicate_count == 1
    assert second.new_events == ()
    assert len(_read_ledger(ledger, TOSS_SCOPE)) == 1


def test_partial_fill_terminal_states_are_real_executions(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success",
        (_order("ord-p1", status="PARTIAL_FILLED", filled_quantity="1"),
         _order("ord-p2", status="CANCELED", filled_quantity="2")),
        None, False)}
    result, ledger, _state, _provider, _details = _run(tmp_path, pages=pages)
    assert result.inserted_count == 2
    keys = {event.source_record_key for event in result.new_events}
    assert keys == {"ord-p1", "ord-p2"}


def test_zero_filled_closed_order_is_not_an_execution(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order("ord-c", status="CANCELED", filled_quantity="0"),),
        None, False)}
    result, ledger, _state, _provider, _details = _run(tmp_path, pages=pages)
    assert result.inserted_count == 0
    assert _read_ledger(ledger, TOSS_SCOPE) == ()


def test_closed_market_execution_is_stored_without_any_mask(tmp_path):
    # §17.2: ingestion is mask-independent. A USD execution while every
    # market is closed still lands in the ledger on this very tick.
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success",
        (_order("ord-us", currency="USD", price="90.5", amount="271.5"),),
        None, False)}
    result, ledger, _state, _provider, _details = _run(tmp_path, pages=pages)
    assert result.inserted_count == 1
    assert result.new_events[0].classified_market == "US"


def test_unknown_currency_defers_to_durable_market_pending(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order("ord-x", currency="JPY"),), None, False)}
    result, ledger, state, _provider, _details = _run(tmp_path, pages=pages)
    assert result.inserted_count == 0
    pending = state.read_market_pending(TOSS_SCOPE)
    assert len(pending) == 1
    assert pending[0].reason == "market_resolution_pending"
    assert _read_ledger(ledger, TOSS_SCOPE) == ()


def test_ambiguous_status_is_durably_quarantined(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order("ord-a", status="CANCEL_REJECTED"),), None, False)}
    result, ledger, state, _provider, _details = _run(tmp_path, pages=pages)
    assert result.inserted_count == 0
    assert state.read_quarantine(TOSS_SCOPE) == ("ord-a",)
    assert _read_ledger(ledger, TOSS_SCOPE) == ()
    # Quarantined keys never enter the dialogue pipeline.
    assert result.new_events == ()


def test_incomplete_order_uses_the_single_detail_slot(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order("ord-i", complete=False),), None, False)}
    details = {"ord-i": _order("ord-i")}
    result, ledger, _state, _provider, detail_calls = _run(
        tmp_path, pages=pages, details=details)
    assert detail_calls == ["ord-i"]
    assert result.inserted_count == 1


def test_second_incomplete_order_defers_durably_beyond_detail_budget(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success",
        (_order("ord-i1", complete=False), _order("ord-i2", complete=False)),
        None, False)}
    details = {"ord-i1": _order("ord-i1"), "ord-i2": _order("ord-i2")}
    result, _ledger, state, _provider, detail_calls = _run(
        tmp_path, pages=pages, details=details)
    assert detail_calls == ["ord-i1"]  # ORDER_DETAIL ≤ 1 per tick
    assert result.inserted_count == 1
    retry = state.read_incomplete_retry()
    assert [row.source_record_key for row in retry] == ["ord-i2"]


def test_pagination_persists_cursor_and_resumes_next_tick(tmp_path):
    page_one = PulseOrdersPageOutcomeV1(
        "success", (_order("ord-1"),), "cursor-2", True)
    page_two = PulseOrdersPageOutcomeV1(
        "success", (_order("ord-2"),), None, False)
    first, _ledger, state, provider, _details = _run(
        tmp_path,
        pages={(TOSS_SCOPE, None): page_one, (TOSS_SCOPE, "cursor-2"): page_two},
    )
    assert first.inserted_count == 2  # both pages inside the 2-page budget
    saved = state.read_state(TOSS_SCOPE)
    assert saved.pending_cursor is None  # window completed
    cursors = [call["cursor"] for call in provider.calls]
    assert cursors == [None, "cursor-2"]


def test_pagination_beyond_page_budget_resumes_from_durable_cursor(tmp_path):
    script = {
        (TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
            "success", (_order("ord-1"),), "cursor-2", True),
        (TOSS_SCOPE, "cursor-2"): PulseOrdersPageOutcomeV1(
            "success", (_order("ord-2"),), "cursor-3", True),
        (TOSS_SCOPE, "cursor-3"): PulseOrdersPageOutcomeV1(
            "success", (_order("ord-3"),), None, False),
    }
    first, _ledger, state, provider, _details = _run(
        tmp_path, pages=_PageProvider(script))
    assert first.inserted_count == 2
    assert state.read_state(TOSS_SCOPE).pending_cursor == "cursor-3"
    assert first.pagination_pending is True
    second, _ledger2, state2, provider2, _d = _run(
        tmp_path, tick=NEXT_TICK, pages=_PageProvider(script),
        now=_utc("2026-08-10T01:01:01Z"))
    assert provider2.calls[0]["cursor"] == "cursor-3"
    assert second.inserted_count == 1
    assert state2.read_state(TOSS_SCOPE).pending_cursor is None


def test_replay_after_crash_between_append_and_receipt_is_idempotent(tmp_path):
    # Crash boundary §11: the ledger append survived, the receipt did not.
    # Re-running the same slot's work discovers the same order and lands on
    # DUPLICATE with zero new rows and zero new events.
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order(),), None, False)}
    first, ledger, _state, _provider, _details = _run(tmp_path, pages=pages)
    assert first.inserted_count == 1
    replay, ledger2, _state2, _provider2, _d2 = _run(
        tmp_path, pages=pages)  # same tick identity replays
    assert replay.inserted_count == 0
    assert replay.duplicate_count == 1
    assert len(_read_ledger(ledger2, TOSS_SCOPE)) == 1


def test_holdings_absent_skips_with_zero_wire(tmp_path):
    recorder = _Recorder()
    result, _ledger, _state, provider, _details = _run(
        tmp_path, holdings=None, recorder=recorder)
    assert result.status == "skipped"
    assert result.reason == "skipped_holdings_absent"
    assert provider.calls == []
    assert recorder.calls == []


def test_wire_recorder_sees_page_and_detail_operations(tmp_path):
    recorder = _Recorder()
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "success", (_order("ord-i", complete=False),), None, False)}
    details = {"ord-i": _order("ord-i")}
    _result, _ledger, _state, _provider, _d = _run(
        tmp_path, pages=pages, details=details, recorder=recorder)
    operations = [call[0] for call in recorder.calls]
    assert operations.count("ORDERS_PAGE") == 1
    assert operations.count("ORDER_DETAIL") == 1


def test_provider_failure_marks_scope_failed_without_retry(tmp_path):
    pages = {(TOSS_SCOPE, None): PulseOrdersPageOutcomeV1(
        "http_429", (), None, False)}
    result, _ledger, _state, provider, _details = _run(tmp_path, pages=pages)
    assert result.scope_status[TOSS_SCOPE] == "failed_provider_rate_limit"
    assert result.reason == "partial_accounts"
    assert len(provider.calls) == 1  # no hidden retry (§12.2)
