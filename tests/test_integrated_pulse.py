"""Integrated Pulse tick: lease/receipt/ceiling/ownership (Plan §9–§12)."""

import fcntl
import os
from datetime import datetime

import pytest

from tokmo.programs.execution_detection import PulseOrdersPageOutcomeV1
from tokmo.programs.integrated_pulse import (
    ROLLING_WIRE_CEILING,
    PulseCeilingError,
    PulseDepsV1,
    PulseRefusal,
    make_wire_recorder,
    pulse_main,
    run_pulse_once,
)
from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
from tokmo.records.calendar_lookahead import LookaheadDayV1, LookaheadSessionV1
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.operation_lane_registry import (
    LANE_GOVERNED_OPERATIONS,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_orders import Order, OrderExecution


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


SLOT = _utc("2026-08-10T01:00:00Z")
NOW = _utc("2026-08-10T01:00:01Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
_COUNTER = iter(range(1, 1_000_000))


def _cutover_registry():
    entries = {}
    for operation in sorted(LANE_GOVERNED_OPERATIONS):
        if operation in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S, OperationLane.CADENCE_300S, 0
            )
        else:
            entries[operation] = OperationLaneV1(
                operation, OperationLane.CADENCE_300S, None, None
            )
    return OperationLaneRegistryV1(entries=entries)


def _session(kind, start, end):
    return LookaheadSessionV1(kind, _utc(start), _utc(end))


def _kr_day():
    return LookaheadDayV1(
        Market.KR,
        "2026-08-10",
        "2026-08-10",
        (_session("regularMarket", "2026-08-10T00:00:00Z", "2026-08-10T06:30:00Z"),),
        NOW,
        "calendar:KR:2026-08-10:0",
    )


def _holdings():
    identity = {TOSS_SCOPE: AccountScopeIdentityV1(TOSS_SCOPE, "personal", 1, None)}
    accounts = (
        AccountHoldingsV1(
            TOSS_SCOPE,
            1,
            (HoldingItemV1("005930", "10", "60000", "KRW", "KR", "market"),),
        ),
    )
    return CurrentHoldingsV1(1, accounts, True, _utc("2026-08-10T00:55:00Z"), identity)


class _Refs:
    def scopes(self):
        return frozenset({TOSS_SCOPE})

    def reveal(self, scope):
        return "native-ref-1" if scope == TOSS_SCOPE else None


def _fact(symbol):
    return TossCurrentPriceFact(
        source="toss-current-price-v1",
        symbol=symbol,
        last_price="70000",
        currency_raw="KRW",
        timestamp=_utc("2026-08-10T01:00:00Z"),
        fetched_at=NOW,
    )


class _Providers:
    def __init__(self):
        self.token_calls = 0
        self.quote_calls = 0
        self.page_calls = 0
        self.detail_calls = 0

    def token(self):
        self.token_calls += 1
        return "token-value"

    def quotes(self, market, symbols):
        self.quote_calls += 1
        return PulseQuoteBatchOutcomeV1(
            "success", tuple(_fact(symbol) for symbol in symbols), ()
        )

    def orders(
        self, *, account_scope, native_ref, status, from_date, to_date, cursor, limit
    ):
        self.page_calls += 1
        execution = OrderExecution(
            filled_quantity="3",
            average_filled_price="70000",
            filled_amount="210000",
            commission=None,
            tax=None,
            filled_at=_utc("2026-08-10T00:59:00Z"),
            settlement_date=None,
        )
        order = Order(
            order_id="ord-001",
            symbol="005930",
            side="SELL",
            order_type="LIMIT",
            time_in_force="DAY",
            status="FILLED",
            quantity="3",
            currency="KRW",
            ordered_at=_utc("2026-08-10T00:58:00Z"),
            execution=execution,
        )
        return PulseOrdersPageOutcomeV1("success", (order,), None, False)

    def detail(self, *, account_scope, native_ref, order_id):
        self.detail_calls += 1


def _deps(
    tmp_path,
    providers,
    *,
    registry=None,
    quotes_enabled=True,
    execution_enabled=True,
    lookahead=None,
):
    quote_store = LatestQuoteStore(
        tmp_path / "stores" / "latest_quotes.sqlite3", clock=lambda: NOW
    )
    ledger = TradeLedgerStore(
        tmp_path / "stores" / "trade_ledger.sqlite3",
        clock=lambda: NOW,
        id_factory=lambda: f"batch-{next(_COUNTER):06d}",
    )
    scan_state = OrderScanStateStore(
        tmp_path / "stores" / "order_scan_state.sqlite3", clock=lambda: NOW
    )
    dialogue = ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3", clock=lambda: NOW
    )
    return PulseDepsV1(
        clock=lambda: NOW,
        lookahead_reader=lookahead
        or (lambda market: (_kr_day(),) if market is Market.KR else ()),
        holdings_reader=lambda: _holdings(),
        native_refs_reader=lambda: _Refs(),
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        token_fetch=providers.token,
        quotes_fetch=providers.quotes,
        orders_page_fetch=providers.orders,
        order_detail_fetch=providers.detail,
        quote_store=quote_store,
        ledger_store=ledger,
        scan_state_store=scan_state,
        dialogue_store=dialogue,
        dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda root: [],
        dialogue_cutoff=NOW,
        lane_registry=registry or _cutover_registry(),
        quotes_provider_enabled=quotes_enabled,
        execution_provider_enabled=execution_enabled,
    )


def _receipts(tmp_path):
    return PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: NOW
    )


def test_green_tick_saves_receipt_with_bounded_wire_counts(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers),
    )
    assert result.outcome == "SAVED"
    assert result.exit_code == 0
    counts = result.wire_counts
    assert counts["TOKEN"] == 1
    assert counts["PRICE_BATCH"] == 1
    assert counts["ORDERS_PAGE"] == 1
    assert counts["total"] <= 6
    receipt = _receipts(tmp_path).read_tick("pulse-60s-v1", SLOT)
    assert receipt["status"] == "SAVED"
    stage_names = [stage["stage"] for stage in receipt["stages"]]
    assert stage_names == [
        "quotes",
        "returns",
        "execution",
        "conditional",
        "dialogue",
        "inbound",
    ]
    returns_stage = next(
        stage for stage in receipt["stages"] if stage["stage"] == "returns"
    )
    assert returns_stage == {
        "stage": "returns",
        "status": "derived",
        "reason": "positions:1",
    }
    # Every started wire settled inside the tick.
    wires = _receipts(tmp_path).read_wires("pulse-60s-v1", result.tick_id)
    assert wires and all(wire["status"] == "SETTLED" for wire in wires)


def test_duplicate_slot_invocation_makes_zero_provider_calls(tmp_path):
    providers = _Providers()
    first = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers),
    )
    assert first.outcome == "SAVED"
    calls_before = (providers.token_calls, providers.quote_calls, providers.page_calls)
    second = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers),
    )
    assert second.outcome == "DUPLICATE"
    assert (
        providers.token_calls,
        providers.quote_calls,
        providers.page_calls,
    ) == calls_before
    events = _receipts(tmp_path).read_events("pulse-60s-v1", SLOT)
    assert [event["kind"] for event in events] == ["DUPLICATE"]


def test_excess_lag_is_terminal_missed_with_zero_wire(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=_utc("2026-08-10T01:00:16Z"),  # lag 16 > 15
        deps=_deps(tmp_path, providers),
    )
    assert result.outcome == "MISSED"
    assert providers.token_calls == 0
    receipt = _receipts(tmp_path).read_tick("pulse-60s-v1", SLOT)
    assert receipt["status"] == "MISSED"
    # No catch-up: the next slot proceeds normally.
    next_slot = _utc("2026-08-10T01:01:00Z")
    ok = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=next_slot,
        observed_now_utc=_utc("2026-08-10T01:01:01Z"),
        deps=_deps(tmp_path, providers),
    )
    assert ok.outcome == "SAVED"
    missed = _receipts(tmp_path).read_tick("pulse-60s-v1", SLOT)
    assert missed["status"] == "MISSED"  # never re-run


def test_held_lease_is_overlap_with_zero_provider_io(tmp_path):
    providers = _Providers()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, parents=True)
    holder = os.open(runtime / "pulse-60s.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = run_pulse_once(
            root=tmp_path,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=SLOT,
            observed_now_utc=NOW,
            deps=_deps(tmp_path, providers),
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert result.outcome == "OVERLAP"
    assert providers.token_calls == 0
    receipt = _receipts(tmp_path).read_tick("pulse-60s-v1", SLOT)
    assert receipt["status"] == "OVERLAP"


def test_default_registry_refuses_enabled_provider_stages(tmp_path):
    # G1 production truth: the shipped registry grants Pulse nothing, so a
    # configuration claiming provider mode fails closed with wire zero.
    from tokmo.values.operation_lane_registry import OPERATION_LANE_REGISTRY_V1

    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers, registry=OPERATION_LANE_REGISTRY_V1),
    )
    assert result.outcome == "FAILED"
    assert result.reason == "owner_conflict"
    assert providers.token_calls == 0
    assert providers.quote_calls == 0
    assert providers.page_calls == 0


def test_disabled_provider_stages_run_with_zero_provider_io(tmp_path):
    from tokmo.values.operation_lane_registry import OPERATION_LANE_REGISTRY_V1

    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(
            tmp_path,
            providers,
            registry=OPERATION_LANE_REGISTRY_V1,
            quotes_enabled=False,
            execution_enabled=False,
        ),
    )
    assert result.outcome == "SAVED"
    assert providers.token_calls == 0
    assert providers.quote_calls == 0
    assert providers.page_calls == 0
    receipt = _receipts(tmp_path).read_tick("pulse-60s-v1", SLOT)
    stages = {stage["stage"]: stage for stage in receipt["stages"]}
    assert stages["quotes"]["status"] == "disabled"
    assert stages["execution"]["status"] == "disabled"


def test_both_markets_closed_is_skipped_closed_quote_wire_zero(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(
            tmp_path, providers, execution_enabled=False, lookahead=lambda market: ()
        ),
    )
    assert result.outcome == "SKIPPED_CLOSED"
    assert result.exit_code == 0
    assert providers.quote_calls == 0
    assert providers.token_calls == 0


def test_quotes_failure_does_not_block_execution_stage(tmp_path):
    providers = _Providers()

    def failing_quotes(market, symbols):
        providers.quote_calls += 1
        return PulseQuoteBatchOutcomeV1("http_429", (), ())

    deps = _deps(tmp_path, providers)
    object.__setattr__(deps, "quotes_fetch", failing_quotes)
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=deps,
    )
    assert result.outcome == "PARTIAL"
    stages = {stage["stage"]: stage for stage in result.stages}
    assert stages["quotes"]["status"] == "failed"
    assert stages["execution"]["status"] == "saved"
    assert providers.page_calls == 1


def test_wire_recorder_enforces_every_ceiling(tmp_path):
    receipts = _receipts(tmp_path)
    recorder = make_wire_recorder(receipts, "pulse-60s-v1", 1)
    recorder("TOKEN", "token:tick0")
    with pytest.raises(PulseCeilingError):
        recorder("TOKEN", "token:tick0")
    recorder("PRICE_BATCH", "quotes:0")
    recorder("PRICE_BATCH", "quotes:1")
    with pytest.raises(PulseCeilingError):
        recorder("PRICE_BATCH", "quotes:2")
    recorder("ORDERS_PAGE", "orders:0")
    recorder("ORDERS_PAGE", "orders:1")
    with pytest.raises(PulseCeilingError):
        recorder("ORDERS_PAGE", "orders:2")
    recorder("ORDER_DETAIL", "detail:ord-1")
    with pytest.raises(PulseCeilingError):
        recorder("ORDER_DETAIL", "detail:ord-2")
    recorder("CONDITIONAL_ORDERS", "conditional:OPEN:0")
    with pytest.raises(PulseCeilingError):
        recorder("CONDITIONAL_ORDERS", "conditional:CLOSED:0")
    # 1+2+2+1+1 = 7 = the closed total; nothing further can start.
    assert sum(recorder.counts.values()) == 7
    with pytest.raises(PulseCeilingError):
        recorder("STOCK_CANDLES_DAILY", "candles:mr:KR:005930:2026-08-10:1")


def test_crash_reconcile_settles_stale_started_slot_and_wires(tmp_path):
    receipts = _receipts(tmp_path)
    stale_slot = _utc("2026-08-10T00:58:00Z")
    stale_tick = int(stale_slot.timestamp()) // 60
    receipts.begin_tick(
        "pulse-60s-v1",
        stale_slot,
        stale_tick,
        observed_start=stale_slot,
        lag_seconds=0.5,
        max_lag_seconds=15,
    )
    receipts.start_wire("pulse-60s-v1", stale_tick, "TOKEN", "token:tick0")
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers),
    )
    assert result.outcome == "SAVED"
    stale = receipts.read_tick("pulse-60s-v1", stale_slot)
    assert stale["status"] == "UNKNOWN"
    wires = receipts.read_wires("pulse-60s-v1", stale_tick)
    assert wires[0]["status"] == "UNKNOWN"


def test_rolling_ceiling_pauses_pulse_durably(tmp_path):
    receipts = _receipts(tmp_path)
    # Fill the trailing five slots to the ceiling.
    base_tick = int(SLOT.timestamp()) // 60
    filled = 0
    tick = base_tick
    while filled < ROLLING_WIRE_CEILING:
        for ordinal in range(6):
            operation, identity = [
                ("TOKEN", "token:tick0"),
                ("PRICE_BATCH", "quotes:0"),
                ("PRICE_BATCH", "quotes:1"),
                ("ORDERS_PAGE", "orders:0"),
                ("ORDERS_PAGE", "orders:1"),
                ("ORDER_DETAIL", "detail:ord-x"),
            ][ordinal]
            receipts.start_wire("pulse-60s-v1", tick, operation, identity)
            filled += 1
            if filled >= ROLLING_WIRE_CEILING:
                break
        tick -= 1
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers),
    )
    assert result.outcome == "FAILED"
    assert result.reason == "rolling_ceiling_exceeded"
    assert providers.token_calls == 0
    assert (tmp_path / "runtime" / "pulse-paused.marker").exists()
    next_result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=_utc("2026-08-10T01:01:00Z"),
        observed_now_utc=_utc("2026-08-10T01:01:01Z"),
        deps=_deps(tmp_path, providers),
    )
    assert next_result.outcome == "FAILED"
    assert next_result.reason == "paused"
    assert providers.token_calls == 0


def test_prospective_ceiling_allows_at_ceiling_and_blocks_beyond(tmp_path):
    receipts = _receipts(tmp_path)
    current = int(SLOT.timestamp()) // 60
    identities = [
        ("TOKEN", "token:tick0"),
        ("PRICE_BATCH", "quotes:0"),
        ("PRICE_BATCH", "quotes:1"),
        ("ORDERS_PAGE", "orders:0"),
        ("ORDERS_PAGE", "orders:1"),
        ("ORDER_DETAIL", "detail:ord-x"),
        ("CONDITIONAL_ORDERS", "conditional:OPEN:0"),
    ]
    # 4 trailing slots × 7 = 28 in-window; 28 + prospective 7 = 35 = ceiling
    for tick_id in range(current - 4, current):
        for operation, identity in identities:
            receipts.start_wire("pulse-60s-v1", tick_id, operation, identity)
    allowed = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, _Providers()),
    )
    assert allowed.reason != "rolling_ceiling_exceeded"
    overflow_root = tmp_path / "overflow"
    overflow = PulseTickReceiptStore(
        overflow_root / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: NOW
    )
    # 4 trailing slots × 8 = 32 in-window; 32 + 7 = 39 > 35 blocks
    for index in range(32):
        overflow.start_wire(
            "pulse-60s-v1",
            current - 1 - index // 8,
            "ORDER_DETAIL",
            f"detail:overflow-{index}",
        )
    blocked = run_pulse_once(
        root=overflow_root,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(overflow_root, _Providers()),
    )
    assert blocked.reason == "rolling_ceiling_exceeded"


def test_reconcile_only_uses_actual_receipt_store_and_protects_current_slot(tmp_path):
    stale = _utc("2026-08-10T00:58:00Z")
    scheduled = _utc("2026-08-10T01:00:00Z")
    store = _receipts(tmp_path)
    for slot in (stale, scheduled):
        tick_id = int(slot.timestamp()) // 60
        store.begin_tick(
            "pulse-60s-v1",
            slot,
            tick_id,
            observed_start=slot,
            lag_seconds=0,
            max_lag_seconds=15,
        )
        store.start_wire("pulse-60s-v1", tick_id, "TOKEN", "token:tick0")
    assert (
        pulse_main(
            [
                "--once",
                "--mode",
                "production",
                "--reconcile-only",
                "--policy-version",
                "pulse-60s-v1",
                "--scheduled-for",
                scheduled.isoformat(),
                "--root",
                str(tmp_path),
            ]
        )
        == 0
    )
    assert store.read_tick("pulse-60s-v1", stale)["status"] == "UNKNOWN"
    assert store.read_tick("pulse-60s-v1", scheduled)["status"] == "STARTED"


def test_cadence_policy_versions_are_refused_outright(tmp_path):
    providers = _Providers()
    with pytest.raises(PulseRefusal):
        run_pulse_once(
            root=tmp_path,
            policy_version="cadence-300s-v1",
            scheduled_for_utc=_utc("2026-08-10T01:00:00Z"),
            observed_now_utc=NOW,
            deps=_deps(tmp_path, providers),
        )


def test_pulse_never_touches_the_cadence_lock(tmp_path):
    # §10.1: the 300s lane's cadence.lock stays untouched even while held.
    providers = _Providers()
    runtime = tmp_path / "runtime"
    runtime.mkdir(mode=0o700, parents=True)
    holder = os.open(runtime / "cadence.lock", os.O_WRONLY | os.O_CREAT, 0o600)
    fcntl.flock(holder, fcntl.LOCK_EX)
    try:
        result = run_pulse_once(
            root=tmp_path,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=SLOT,
            observed_now_utc=NOW,
            deps=_deps(tmp_path, providers),
        )
    finally:
        fcntl.flock(holder, fcntl.LOCK_UN)
        os.close(holder)
    assert result.outcome == "SAVED"  # a running 300s tick never blocks Pulse
