"""Conditional-order fill detection lane (detection SDS v0.4 §4, RED-1..8).

The ledger keeps collecting from the regular surface only; the conditional
surface is a fast-alert lane. Detection signal = a leg's triggered_order_id
null→value transition; merge with the migrated regular event is carried by
the probe-confirmed primary key (triggered_order_id == source_record_key)
with the scope+symbol+±25h window as a defensive fallback only.
"""

from datetime import datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.conditional_detection import (
    PulseConditionalPageOutcomeV1,
    build_conditional_fill_question,
    run_conditional_detection,
)
from tokmo.programs.execution_detection import (
    NewTradeExecutionV1,
    PulseOrdersPageOutcomeV1,
)
from tokmo.programs.execution_dialogue import (
    PROMPT_VERSION,
    QUESTION_KEYBOARD,
    _KEYBOARDS_BY_PROMPT_KIND,
    run_execution_dialogue,
)
from tokmo.programs.integrated_pulse import (
    PulseCeilingError,
    PulseDepsV1,
    make_wire_recorder,
    run_pulse_once,
)
from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.cadence_policy import (
    CADENCE_OPERATIONS,
    CADENCE_POLICY_REGISTRY,
    GROUP_LIMIT_FIELDS,
    validate_cadence_operation,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.operation_lane_registry import (
    LANE_GOVERNED_OPERATIONS,
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
    OperationOwnershipError,
    validate_lane_startup,
)
from tokmo.values.pulse_policy import (
    PULSE_OPERATIONS,
    PULSE_REQUEST_LIMITS_V1,
)
from tokmo.values.session_gate import Market
from tokmo.values.toss_orders import (
    ConditionalOrderCondition,
    ConditionalOrderDetailResponse,
    Order,
    OrderExecution,
)
from tokmo.values.trade_event import (
    PROMPT_KINDS,
    TradeEventState,
    TradeEventTimesV1,
    conditional_alert_id,
    dialogue_dedupe_key,
    trade_event_id,
)
from tokmo.values.pulse_policy import pulse_tick_v1


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


NOW = _utc("2026-08-18T01:00:01Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
_COUNTER = iter(range(1, 1_000_000))

# consecutive pulse slots: detection alternates OPEN/CLOSED by tick parity
SLOT_A = _utc("2026-08-18T01:00:00Z")
SLOT_B = _utc("2026-08-18T01:01:00Z")
SLOT_C = _utc("2026-08-18T01:02:00Z")
SLOT_D = _utc("2026-08-18T01:03:00Z")


def _holdings():
    identity = {TOSS_SCOPE: AccountScopeIdentityV1(
        TOSS_SCOPE, "personal", 1, None)}
    accounts = (AccountHoldingsV1(TOSS_SCOPE, 1, (
        HoldingItemV1("TSLA", "10", "300", "USD", "US", "market"),
    )),)
    return CurrentHoldingsV1(
        1, accounts, True, _utc("2026-08-18T00:55:00Z"), identity)


class _Refs:
    def scopes(self):
        return frozenset({TOSS_SCOPE})

    def reveal(self, scope):
        return "native-ref-1" if scope == TOSS_SCOPE else None


class _Recorder:
    def __init__(self):
        self.calls = []

    def __call__(self, operation, identity):
        self.calls.append((operation, identity))
        return len(self.calls)


def _condition(status="WATCHING", toi=None):
    return ConditionalOrderCondition(
        type="STOP_LOSS", status=status, trigger_price="250",
        target_profit_rate=None, order_price="249", triggered_order_id=toi)


def _cond_order(coid="cond-1", *, first, second=None, market="US",
                symbol="TSLA", quantity="7", kind="SINGLE", status="OPEN"):
    return ConditionalOrderDetailResponse(
        conditional_order_id=coid, type=kind, status=status, symbol=symbol,
        market=market, quantity=quantity, order_type="LIMIT", first=first,
        created_at=_utc("2026-08-17T00:00:00Z"), expire_date=None,
        second=second)


class _ConditionalFetch:
    """Serves a fixed page per call; records the requested status/cursor."""

    def __init__(self, pages):
        self.pages = list(pages)
        self.requests = []

    def __call__(self, *, account_scope, native_ref, status, cursor, limit):
        self.requests.append(
            {"scope": account_scope, "native_ref": native_ref,
             "status": status, "cursor": cursor, "limit": limit})
        page = self.pages.pop(0) if self.pages else ()
        if isinstance(page, Exception):
            raise page
        return PulseConditionalPageOutcomeV1("success", tuple(page), None,
                                             False)


def _store(tmp_path, name="execution_dialogue"):
    return ExecutionDialogueStore(
        tmp_path / f"{name}.sqlite3", clock=lambda: NOW)


def _detect(store, slot, fetch, *, recorder=None):
    return run_conditional_detection(
        tick=pulse_tick_v1("pulse-60s-v1", slot),
        holdings_reader=lambda: _holdings(),
        native_refs_reader=lambda: _Refs(),
        token_provider=lambda: "token-value",
        conditional_orders_fetch=fetch,
        dialogue_store=store,
        clock=lambda: NOW, now=NOW,
        wire_recorder=recorder or _Recorder())


# --- User-visible conditional fill question contract -------------------------


def test_conditional_fill_question_exact_format_and_regular_trade_buttons():
    # 2026-08-21 display contract: the instrument line leads; every other
    # confirmed line is unchanged. Without a canonical name it is the symbol.
    assert build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="TSLA",
        market_class="US", instrument_name="테슬라") == "\n".join([
            "테슬라(TSLA)",
            "조건에 도달해 주문이 접수됐어요.",
            "체결 여부는 아직 확인되지 않았어요.",
            "",
            "추천문구",
            "- 플레이북 정보 부족으로 추천문구 없음",
            "",
            "답장으로 남긴 매매 사유는 체결 확인 전까지 임시 보관해요.",
        ])
    assert build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="TSLA").splitlines()[0] == "TSLA"
    assert build_conditional_fill_question(
        account_scope=TOSS_SCOPE,
        symbol="TSLA",
        recommendation="분할매수 조건을 다시 확인",
    ).splitlines()[5] == "- 분할매수 조건을 다시 확인"
    assert _KEYBOARDS_BY_PROMPT_KIND["conditional_fill_question"] \
        == QUESTION_KEYBOARD


# --- RED-1: transition detection, one page per tick, restart idempotent ----


def test_transition_null_to_value_is_the_only_alert_signal(tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([
        [_cond_order(first=_condition("WATCHING", None))],
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED")],
    ])
    first = _detect(store, SLOT_A, fetch)
    assert first.status == "saved"
    assert first.alerts_created == 0
    assert store.list_conditional_alerts() == ()

    second = _detect(store, SLOT_B, fetch)
    assert second.alerts_created == 1
    alerts = store.list_conditional_alerts()
    assert len(alerts) == 1
    alert = alerts[0]
    assert alert["conditional_order_id"] == "cond-1"
    assert alert["leg_index"] == 0
    assert alert["triggered_order_id"] == "ord-real-1"
    assert alert["prev_status"] == "WATCHING"
    assert alert["new_status"] == "COMPLETED"
    assert alert["market_class"] == "US"
    assert alert["migrated_event_id"] is None
    # submission observation is retained, with no outbound question
    key = dialogue_dedupe_key(
        alert["conditional_alert_id"], "conditional_fill_question",
        PROMPT_VERSION)
    row = store.read_outbox(key)
    assert row is None


def test_one_wire_per_tick_status_alternates_by_parity(tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([[], [], [], []])
    recorders = []
    for slot in (SLOT_A, SLOT_B, SLOT_C, SLOT_D):
        recorder = _Recorder()
        recorders.append(recorder)
        _detect(store, slot, fetch, recorder=recorder)
    for recorder in recorders:
        assert len(recorder.calls) == 1
        operation, identity = recorder.calls[0]
        assert operation == "CONDITIONAL_ORDERS"
        validate_cadence_operation(operation, identity)
    statuses = [request["status"] for request in fetch.requests]
    assert set(statuses) == {"OPEN", "CLOSED"}
    assert statuses[0] != statuses[1]
    assert statuses[0] == statuses[2]
    assert all(request["limit"] == 20 for request in fetch.requests)


def test_restart_reobservation_is_idempotent_no_second_alert(tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([
        [_cond_order(first=_condition("WATCHING", None))],
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED")],
        # restart replay of the same completed page
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED")],
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED")],
    ])
    _detect(store, SLOT_A, fetch)
    _detect(store, SLOT_B, fetch)
    replay = _detect(store, SLOT_C, fetch)
    assert replay.alerts_created == 0
    # crash between alert insert and observation write: re-observed
    # transition hits the UNIQUE(order, leg, status) no-op
    state = store.read_conditional_scan_state()
    state["observations"]["cond-1:0"]["triggered_order_id"] = None
    store.write_conditional_scan_state(state)
    crash_replay = _detect(store, SLOT_D, fetch)
    assert crash_replay.alerts_created == 0
    assert len(store.list_conditional_alerts()) == 1
    assert len(store.list_outbox()) == 0


# --- RED-2: merge — primary key, fallback dispositions, NULL key -----------


def _times(filled):
    return TradeEventTimesV1(
        filled_at=filled, provider_observed_at=None, discovered_at=NOW,
        stored_at=NOW, notified_at=None)


def _regular_event(order_id, symbol="TSLA", filled=None):
    event_id = trade_event_id("toss-orders-v1", TOSS_SCOPE, order_id)
    return NewTradeExecutionV1(
        event_id, TOSS_SCOPE, order_id, symbol, "SELL", "USD", "7", "251",
        "1757", "US", _times(filled or _utc("2026-08-18T00:30:00Z")))


def _run_dialogue(store, events):
    return run_execution_dialogue(
        new_events=tuple(events), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=None, activation_enabled=False,
        send_eligible_after=NOW, clock=lambda: NOW, now=NOW)


def _record_alert(store, *, coid="cond-1", leg=0, toi="ord-hit",
                  symbol="TSLA", new_status="COMPLETED"):
    alert_id = conditional_alert_id(coid, leg, new_status)
    outcome = store.record_conditional_alert(
        alert_id, conditional_order_id=coid, leg_index=leg,
        triggered_order_id=toi, account_scope=TOSS_SCOPE, symbol=symbol,
        market_class="US", order_type="LIMIT", quantity_ordered="7",
        prev_status="WATCHING", new_status=new_status)
    return alert_id, outcome


def test_primary_key_hit_merges_notifies_and_backfills(tmp_path):
    store = _store(tmp_path)
    alert_id, outcome = _record_alert(store, toi="ord-hit")
    assert outcome == "created"
    event = _regular_event("ord-hit")
    result = _run_dialogue(store, [event])
    assert result.registered == 1
    # A submission is not an execution: notify the actual fill once.
    question_key = dialogue_dedupe_key(
        event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(question_key) is not None
    assert store.read_event(event.trade_event_id)["state"] \
        is TradeEventState.ENQUEUED
    assert store.read_conditional_alert(alert_id)["migrated_event_id"] \
        == event.trade_event_id


def test_null_triggered_order_id_never_matches_the_primary_key(tmp_path):
    store = _store(tmp_path)
    _record_alert(store, toi=None)
    assert store.find_alert_by_triggered_order("ord-anything") is None


def test_fallback_single_candidate_merges(tmp_path):
    store = _store(tmp_path)
    # primary key misses (alert carries a different id), fallback matches
    alert_id, _outcome = _record_alert(store, toi="provider-renamed-id")
    event = _regular_event("ord-regular-1")
    _run_dialogue(store, [event])
    question_key = dialogue_dedupe_key(
        event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(question_key) is not None
    assert store.read_event(event.trade_event_id)["state"] \
        is TradeEventState.ENQUEUED
    assert store.read_conditional_alert(alert_id)["migrated_event_id"] \
        == event.trade_event_id


def test_fallback_two_candidates_never_merges_and_asks(tmp_path):
    store = _store(tmp_path)
    one, _ = _record_alert(store, coid="cond-1", toi="renamed-1")
    two, _ = _record_alert(store, coid="cond-2", toi="renamed-2")
    event = _regular_event("ord-regular-2")
    _run_dialogue(store, [event])
    question_key = dialogue_dedupe_key(
        event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(question_key) is not None
    assert store.read_event(event.trade_event_id)["state"] \
        is TradeEventState.ENQUEUED
    assert store.read_conditional_alert(one)["migrated_event_id"] is None
    assert store.read_conditional_alert(two)["migrated_event_id"] is None


def test_fallback_zero_candidates_asks_normally(tmp_path):
    store = _store(tmp_path)
    # an alert outside the ±25h window is not a candidate
    _record_alert(store, toi="renamed-x")
    event = _regular_event(
        "ord-late", filled=NOW + timedelta(hours=26))
    _run_dialogue(store, [event])
    question_key = dialogue_dedupe_key(
        event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(question_key) is not None


def test_migrated_alert_is_never_matched_again(tmp_path):
    store = _store(tmp_path)
    _alert_id, _ = _record_alert(store, toi="ord-hit")
    _run_dialogue(store, [_regular_event("ord-hit")])
    # a later distinct execution must not merge into the settled alert
    event = _regular_event("ord-second")
    _run_dialogue(store, [event])
    question_key = dialogue_dedupe_key(
        event.trade_event_id, "trade_reason_question", PROMPT_VERSION)
    assert store.read_outbox(question_key) is not None


# --- RED-2b: reverse arrival — migration precedes the alert ----------------


def test_reverse_arrival_suppresses_the_alert(tmp_path):
    store = _store(tmp_path)
    _run_dialogue(store, [_regular_event("ord-rev")])
    fetch = _ConditionalFetch([
        [_cond_order(first=_condition("WATCHING", None))],
        [_cond_order(first=_condition("COMPLETED", "ord-rev"),
                     status="CLOSED")],
    ])
    _detect(store, SLOT_A, fetch)
    result = _detect(store, SLOT_B, fetch)
    assert result.alerts_created == 0
    assert result.suppressed_reverse == 1
    assert store.list_conditional_alerts() == ()
    key = dialogue_dedupe_key(
        conditional_alert_id("cond-1", 0, "COMPLETED"),
        "conditional_fill_question", PROMPT_VERSION)
    assert store.read_outbox(key) is None


# --- RED-3: the conditional lane can never write the ledger ----------------


def test_conditional_lane_has_no_ledger_surface():
    source = (Path(__file__).parents[1] / "src" / "tokmo" / "programs"
              / "conditional_detection.py").read_text()
    assert "ledger" not in source.lower()
    assert "trade_ledger" not in source
    assert "append_batch" not in source


# --- RED-4: lane isolation and the closed wire total of seven --------------


def _cutover_registry():
    entries = {}
    for operation in sorted(LANE_GOVERNED_OPERATIONS):
        if operation == "CONDITIONAL_ORDERS":
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S, None, None)
        elif operation in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S,
                OperationLane.CADENCE_300S, 0)
        else:
            entries[operation] = OperationLaneV1(
                operation, OperationLane.CADENCE_300S, None, None)
    return OperationLaneRegistryV1(entries=entries)


def _kr_day():
    from tokmo.records.calendar_lookahead import (
        LookaheadDayV1,
        LookaheadSessionV1,
    )
    return LookaheadDayV1(
        Market.KR, "2026-08-18", "2026-08-18",
        (LookaheadSessionV1("regularMarket", _utc("2026-08-18T00:00:00Z"),
                            _utc("2026-08-18T06:30:00Z")),),
        NOW, "calendar:KR:2026-08-18:0",
    )


class _Providers:
    def __init__(self, conditional_error=None):
        self.conditional_error = conditional_error
        self.quote_calls = 0
        self.page_calls = 0
        self.conditional_calls = 0

    def token(self):
        return "token-value"

    def quotes(self, market, symbols):
        self.quote_calls += 1
        facts = tuple(TossCurrentPriceFact(
            source="toss-current-price-v1", symbol=symbol, last_price="70000",
            currency_raw="KRW", timestamp=_utc("2026-08-18T01:00:00Z"),
            fetched_at=NOW) for symbol in symbols)
        return PulseQuoteBatchOutcomeV1("success", facts, ())

    def orders(self, *, account_scope, native_ref, status, from_date,
               to_date, cursor, limit):
        self.page_calls += 1
        execution = OrderExecution(
            filled_quantity="3", average_filled_price="70000",
            filled_amount="210000", commission=None, tax=None,
            filled_at=_utc("2026-08-18T00:59:00Z"), settlement_date=None)
        order = Order(
            order_id="ord-001", symbol="005930", side="SELL",
            order_type="LIMIT", time_in_force="DAY", status="FILLED",
            quantity="3", currency="KRW",
            ordered_at=_utc("2026-08-18T00:58:00Z"), execution=execution)
        return PulseOrdersPageOutcomeV1("success", (order,), None, False)

    def detail(self, *, account_scope, native_ref, order_id):
        return None

    def conditional(self, *, account_scope, native_ref, status, cursor,
                    limit):
        self.conditional_calls += 1
        if self.conditional_error is not None:
            raise self.conditional_error
        return PulseConditionalPageOutcomeV1("success", (), None, False)


def _kr_holdings():
    identity = {TOSS_SCOPE: AccountScopeIdentityV1(
        TOSS_SCOPE, "personal", 1, None)}
    accounts = (AccountHoldingsV1(TOSS_SCOPE, 1, (
        HoldingItemV1("005930", "10", "60000", "KRW", "KR", "market"),
    )),)
    return CurrentHoldingsV1(
        1, accounts, True, _utc("2026-08-18T00:55:00Z"), identity)


def _pulse_deps(tmp_path, providers):
    return PulseDepsV1(
        clock=lambda: NOW,
        lookahead_reader=lambda market: (
            (_kr_day(),) if market is Market.KR else ()),
        holdings_reader=lambda: _kr_holdings(),
        native_refs_reader=lambda: _Refs(),
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        token_fetch=providers.token,
        quotes_fetch=providers.quotes,
        orders_page_fetch=providers.orders,
        order_detail_fetch=providers.detail,
        quote_store=LatestQuoteStore(
            tmp_path / "stores" / "latest_quotes.sqlite3", clock=lambda: NOW),
        ledger_store=TradeLedgerStore(
            tmp_path / "stores" / "trade_ledger.sqlite3", clock=lambda: NOW,
            id_factory=lambda: f"batch-{next(_COUNTER):06d}"),
        scan_state_store=OrderScanStateStore(
            tmp_path / "stores" / "order_scan_state.sqlite3",
            clock=lambda: NOW),
        dialogue_store=ExecutionDialogueStore(
            tmp_path / "stores" / "execution_dialogue.sqlite3",
            clock=lambda: NOW),
        dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda root: [],
        dialogue_cutoff=NOW,
        conditional_orders_fetch=providers.conditional,
        conditional_provider_enabled=True,
        lane_registry=_cutover_registry(),
        quotes_provider_enabled=True,
        execution_provider_enabled=True,
    )


def test_conditional_lane_failure_leaves_all_other_lanes_untouched(tmp_path):
    providers = _Providers(conditional_error=RuntimeError("provider down"))
    result = run_pulse_once(
        root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT_A, observed_now_utc=NOW,
        deps=_pulse_deps(tmp_path, providers))
    stages = {stage["stage"]: stage for stage in result.stages}
    assert stages["conditional"]["status"] == "failed"
    assert result.outcome == "PARTIAL"
    assert stages["quotes"]["status"] == "saved"
    assert stages["execution"]["status"] == "saved"
    assert stages["dialogue"]["status"] == "saved"
    # the regular execution was still ingested and enqueued
    store = ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3",
        clock=lambda: NOW)
    event_id = trade_event_id("toss-orders-v1", TOSS_SCOPE, "ord-001")
    assert store.read_event(event_id) is not None


def test_green_tick_with_conditional_lane_stays_within_seven_wires(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT_A, observed_now_utc=NOW,
        deps=_pulse_deps(tmp_path, providers))
    assert result.outcome == "SAVED"
    stage_names = [stage["stage"] for stage in result.stages]
    assert stage_names == ["quotes", "returns", "execution", "conditional",
                           "dialogue", "inbound"]
    counts = result.wire_counts
    assert counts["CONDITIONAL_ORDERS"] == 1
    assert counts["total"] <= 7
    assert providers.conditional_calls == 1


def test_wire_recorder_admits_exactly_one_conditional_and_total_seven(tmp_path):
    receipts = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: NOW)
    recorder = make_wire_recorder(receipts, "pulse-60s-v1", 1)
    recorder("TOKEN", "token:tick0")
    recorder("PRICE_BATCH", "quotes:0")
    recorder("PRICE_BATCH", "quotes:1")
    recorder("ORDERS_PAGE", "orders:0")
    recorder("ORDERS_PAGE", "orders:1")
    recorder("ORDER_DETAIL", "detail:ord-1")
    recorder("CONDITIONAL_ORDERS", "conditional:OPEN:0")
    assert sum(recorder.counts.values()) == 7
    with pytest.raises(PulseCeilingError):
        recorder("CONDITIONAL_ORDERS", "conditional:CLOSED:0")
    with pytest.raises(PulseCeilingError):
        recorder("PRICE_BATCH", "quotes:2")


# --- RED-5: two-leg OCO — one row per leg, per-leg merge -------------------


def test_two_leg_oco_records_one_alert_per_leg_with_independent_merge(
        tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([
        [_cond_order(kind="OCO", first=_condition("WATCHING", None),
                     second=_condition("WATCHING", None))],
        [_cond_order(kind="OCO", first=_condition("COMPLETED", "ord-A"),
                     second=_condition("WATCHING", None))],
        [_cond_order(kind="OCO", first=_condition("COMPLETED", "ord-A"),
                     second=_condition("COMPLETED", "ord-B"),
                     status="CLOSED")],
    ])
    _detect(store, SLOT_A, fetch)
    leg0 = _detect(store, SLOT_B, fetch)
    leg1 = _detect(store, SLOT_C, fetch)
    assert leg0.alerts_created == 1
    assert leg1.alerts_created == 1
    alerts = {alert["leg_index"]: alert
              for alert in store.list_conditional_alerts()}
    assert set(alerts) == {0, 1}
    assert alerts[0]["triggered_order_id"] == "ord-A"
    assert alerts[1]["triggered_order_id"] == "ord-B"
    # both migrated regular executions merge per leg (S4: merge count 2)
    event_a = _regular_event("ord-A")
    event_b = _regular_event("ord-B")
    _run_dialogue(store, [event_a, event_b])
    assert store.read_conditional_alert(
        alerts[0]["conditional_alert_id"])["migrated_event_id"] \
        == event_a.trade_event_id
    assert store.read_conditional_alert(
        alerts[1]["conditional_alert_id"])["migrated_event_id"] \
        == event_b.trade_event_id
    for event in (event_a, event_b):
        assert store.read_event(event.trade_event_id)["state"] \
            is TradeEventState.ENQUEUED


# --- RED-6: economics-free grammar -----------------------------------------


def test_question_carries_no_quantity_price_side_or_scope():
    # The instrument identity is now deliberately shown (2026-08-21 display
    # contract). Everything the ledger has not confirmed stays absent.
    message = build_conditional_fill_question(
        account_scope=TOSS_SCOPE, symbol="TSLA", market_class="US",
        instrument_name="테슬라")
    assert message.splitlines()[:2] == ["테슬라(TSLA)", "조건에 도달해 주문이 접수됐어요."]
    assert "체결 여부는 아직 확인되지 않았어요." in message
    assert TOSS_SCOPE not in message
    for forbidden in ("[개인]", "매수", "매도", "7주", "USD", "@"):
        assert forbidden not in message


def test_enqueued_question_never_names_the_ordered_quantity(tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([
        [_cond_order(first=_condition("WATCHING", None), quantity="7")],
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED", quantity="7")],
    ])
    _detect(store, SLOT_A, fetch)
    _detect(store, SLOT_B, fetch)
    rows = store.list_outbox()
    assert rows == ()


# --- RED-7: market mapping failure — no alert, log only --------------------


def test_unmappable_market_never_alerts(tmp_path):
    store = _store(tmp_path)
    fetch = _ConditionalFetch([
        [_cond_order(first=_condition("WATCHING", None), market="JP")],
        [_cond_order(first=_condition("COMPLETED", "ord-real-1"),
                     status="CLOSED", market="JP")],
    ])
    _detect(store, SLOT_A, fetch)
    result = _detect(store, SLOT_B, fetch)
    assert result.status == "saved"
    assert result.alerts_created == 0
    assert result.mapping_failures == 1
    assert store.list_conditional_alerts() == ()
    assert store.list_outbox() == ()


# --- RED-8: PROMPT_KINDS widening + synthetic trade_event_id ---------------


def test_prompt_kinds_widened_with_the_named_conditional_kind():
    assert "conditional_fill_question" in PROMPT_KINDS
    alert_id = conditional_alert_id("cond-9", 0, "COMPLETED")
    key = dialogue_dedupe_key(
        alert_id, "conditional_fill_question", PROMPT_VERSION)
    other = dialogue_dedupe_key(
        alert_id, "trade_reason_question", PROMPT_VERSION)
    assert len(key) == 64 and key != other


def test_legacy_submission_is_retained_but_never_flushes(tmp_path):
    store = _store(tmp_path)
    alert_id = conditional_alert_id("cond-9", 0, "COMPLETED")
    key = dialogue_dedupe_key(
        alert_id, "conditional_fill_question", PROMPT_VERSION)
    assert store.enqueue_outbox(
        key, trade_event_id=alert_id,
        prompt_kind="conditional_fill_question",
        prompt_version=PROMPT_VERSION,
        message_text="[개인] TSLA 조건주문 체결이 감지되었습니다") == "created"
    sent = []

    def sender(text, reply_markup=None):
        sent.append((text, reply_markup))
        return 900 + len(sent)

    result = run_execution_dialogue(
        new_events=(), dialogue_store=store,
        quote_reader=lambda symbol: None, playbook_root=None,
        ai_evaluator=None, sender=sender, activation_enabled=True,
        send_eligible_after=_utc("2026-08-18T00:00:00Z"),
        clock=lambda: NOW, now=NOW)
    assert result.send_calls == 0
    assert sent == []
    assert store.read_outbox(key)["status"] == "PENDING"
    assert store.read_outbox(key)["telegram_message_id"] is None


# --- registries and budgets (SDS §4.4) -------------------------------------


def test_pulse_budget_is_seven_with_one_conditional_start():
    limits = PULSE_REQUEST_LIMITS_V1
    assert limits.total_wire_starts == 7
    assert limits.conditional_order_starts == 1
    assert (limits.token_starts + limits.quote_starts + limits.trades_starts
            + limits.conditional_order_starts) == 7
    assert PULSE_OPERATIONS == (
        "TOKEN", "PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL",
        "CONDITIONAL_ORDERS")


def test_closed_registries_carry_the_conditional_operation():
    assert CADENCE_OPERATIONS["CONDITIONAL_ORDERS"] \
        == "CONDITIONAL_ORDER_HISTORY"
    assert GROUP_LIMIT_FIELDS["CONDITIONAL_ORDER_HISTORY"] \
        == "conditional_order_starts"
    assert validate_cadence_operation(
        "CONDITIONAL_ORDERS", "conditional:CLOSED:0") \
        == "CONDITIONAL_ORDER_HISTORY"
    for bad in ("conditional:open:0", "conditional:FILLED:0", "conditional:",
                "orders:0"):
        with pytest.raises(ValueError):
            validate_cadence_operation("CONDITIONAL_ORDERS", bad)


def test_cadence_300s_budget_is_unchanged():
    for policy in CADENCE_POLICY_REGISTRY.values():
        assert policy.request_limits.conditional_order_starts == 0


def test_lane_registry_grants_conditional_to_pulse_without_cutover():
    entry = OPERATION_LANE_REGISTRY_V1.entries["CONDITIONAL_ORDERS"]
    assert entry.recurring_owner is OperationLane.PULSE_60S
    assert entry.cutover_epoch_utc is None
    validate_lane_startup(
        OPERATION_LANE_REGISTRY_V1, lane=OperationLane.PULSE_60S,
        provider_operations=("CONDITIONAL_ORDERS",), epoch=0)
    with pytest.raises(OperationOwnershipError):
        validate_lane_startup(
            OPERATION_LANE_REGISTRY_V1, lane=OperationLane.CADENCE_300S,
            provider_operations=("CONDITIONAL_ORDERS",), epoch=0)


# --- pulse wire ledger CHECK widening (existing DBs) -----------------------


_OLD_WIRE_SCHEMA = """
CREATE TABLE IF NOT EXISTS pulse_wire_attempts(
  policy_version TEXT NOT NULL,
  tick_id INTEGER NOT NULL CHECK(tick_id >= 0),
  seq INTEGER NOT NULL CHECK(seq >= 0),
  operation TEXT NOT NULL CHECK(operation IN
    ('TOKEN','PRICE_BATCH','ORDERS_PAGE','ORDER_DETAIL')),
  native_identity TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('STARTED','SETTLED','UNKNOWN')),
  started_at TEXT NOT NULL,
  settled_at TEXT,
  PRIMARY KEY(policy_version, tick_id, seq)
) STRICT;
"""


def test_existing_wire_ledger_is_widened_once_preserving_rows(tmp_path):
    import os
    import sqlite3

    path = tmp_path / "ops" / "pulse_tick_receipts.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True)
    connection = sqlite3.connect(path)
    connection.executescript(_OLD_WIRE_SCHEMA)
    connection.execute(
        "INSERT INTO pulse_wire_attempts VALUES"
        "('pulse-60s-v1', 100, 0, 'TOKEN', 'token:tick0', 'SETTLED',"
        " '2026-08-18T00:00:00.000000Z', '2026-08-18T00:00:01.000000Z')")
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)

    store = PulseTickReceiptStore(path, clock=lambda: NOW)
    seq = store.start_wire(
        "pulse-60s-v1", 100, "CONDITIONAL_ORDERS", "conditional:OPEN:0")
    assert seq == 1
    wires = store.read_wires("pulse-60s-v1", 100)
    assert wires[0]["operation"] == "TOKEN"
    assert wires[0]["status"] == "SETTLED"
    assert wires[1]["operation"] == "CONDITIONAL_ORDERS"
    counts = store.wire_counts("pulse-60s-v1", 100)
    assert counts["CONDITIONAL_ORDERS"] == 1
    assert counts["total"] == 2
