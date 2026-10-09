"""G3 telegram send activation (activation SDS v0.2 §4–§5, RED-1..RED-5).

The gate file alone decides activation; env is required only when the gate
resolves enabled=true. Collection lanes never halt for any gate/env state.
The cutoff wired into the tick is the gate's FIXED value, never the per-tick
advancing scheduled slot, so a row unsent in its own tick keeps eligibility.
"""

import json
import os
from datetime import datetime
from pathlib import Path

import pytest

from tokmo.programs.execution_detection import (
    NewTradeExecutionV1,
    PulseOrdersPageOutcomeV1,
)
from tokmo.programs.execution_dialogue import (
    PROMPT_VERSION,
    run_execution_dialogue,
)
from tokmo.programs.integrated_pulse import PulseDepsV1, run_pulse_once
from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
from tokmo.records.dialogue_activation import (
    GATE_FILE_NAME,
    DialogueActivationV1,
    read_dialogue_activation,
)
from tokmo.records.execution_dialogue_store import ExecutionDialogueStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.order_scan_state import OrderScanStateStore
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
from tokmo.values.trade_event import (
    TradeEventState,
    TradeEventTimesV1,
    dialogue_dedupe_key,
    trade_event_id,
)


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


SLOT = _utc("2026-08-18T01:00:00Z")
NOW = _utc("2026-08-18T01:00:01Z")
TOSS_SCOPE = "toss-acct-" + "a" * 24
_COUNTER = iter(range(1, 1_000_000))


# --- gate file reader (RED-2 / RED-3b) ------------------------------------


def _write_gate(tmp_path, payload, *, raw=None):
    path = tmp_path / GATE_FILE_NAME
    text = raw if raw is not None else json.dumps(payload)
    path.write_text(text, encoding="utf-8")
    os.chmod(path, 0o600)
    return path


def test_gate_absent_resolves_disabled(tmp_path):
    gate = read_dialogue_activation(tmp_path / GATE_FILE_NAME)
    assert gate == DialogueActivationV1(False, None, None)


def test_gate_enabled_false_resolves_disabled(tmp_path):
    path = _write_gate(
        tmp_path,
        {"enabled": False, "cutoff": "2026-08-18T00:00:00Z", "approved_by": "woong"},
    )
    assert read_dialogue_activation(path).enabled is False


def test_gate_malformed_json_resolves_disabled(tmp_path):
    path = _write_gate(tmp_path, None, raw="{not json")
    assert read_dialogue_activation(path).enabled is False


def test_gate_naive_cutoff_resolves_disabled(tmp_path):
    path = _write_gate(
        tmp_path,
        {"enabled": True, "cutoff": "2026-08-18T00:00:00", "approved_by": "woong"},
    )
    assert read_dialogue_activation(path).enabled is False


def test_gate_non_bool_enabled_or_missing_fields_resolve_disabled(tmp_path):
    for payload in (
        {"enabled": "true", "cutoff": "2026-08-18T00:00:00Z", "approved_by": "woong"},
        {"enabled": True, "approved_by": "woong"},
        {"enabled": True, "cutoff": "2026-08-18T00:00:00Z"},
        {"enabled": True, "cutoff": "2026-08-18T00:00:00Z", "approved_by": ""},
        [],
    ):
        path = _write_gate(tmp_path, payload)
        assert read_dialogue_activation(path).enabled is False


def test_gate_symlink_resolves_disabled(tmp_path):
    real = _write_gate(
        tmp_path,
        {"enabled": True, "cutoff": "2026-08-18T00:00:00Z", "approved_by": "woong"},
    )
    link = tmp_path / "link.json"
    link.symlink_to(real)
    assert read_dialogue_activation(link).enabled is False


def test_gate_valid_resolves_enabled_with_fixed_utc_cutoff(tmp_path):
    path = _write_gate(
        tmp_path,
        {
            "enabled": True,
            "cutoff": "2026-08-18T09:00:00+09:00",
            "approved_by": "woong",
        },
    )
    gate = read_dialogue_activation(path)
    assert gate.enabled is True
    assert gate.cutoff == _utc("2026-08-18T00:00:00Z")
    assert gate.cutoff.tzinfo is not None
    assert gate.approved_by == "woong"


# --- production resolver: fixed cutoff replaces per-tick advancing ---------


def test_resolver_gate_absent_falls_back_to_scheduled_disabled(tmp_path):
    from tokmo.programs.integrated_pulse import resolve_production_dialogue

    resolved = resolve_production_dialogue(tmp_path, SLOT)
    assert resolved.enabled is False
    assert resolved.cutoff == SLOT


def test_resolver_enabled_gate_returns_the_gates_fixed_cutoff(tmp_path):
    from tokmo.programs.integrated_pulse import resolve_production_dialogue

    runtime = tmp_path / "runtime"
    runtime.mkdir(parents=True)
    _write_gate(
        runtime,
        {"enabled": True, "cutoff": "2026-08-17T23:00:00Z", "approved_by": "woong"},
    )
    resolved = resolve_production_dialogue(tmp_path, SLOT)
    assert resolved.enabled is True
    assert resolved.cutoff == _utc("2026-08-17T23:00:00Z")
    assert resolved.cutoff != SLOT


def test_pulse_main_wires_gate_resolution_not_scheduled(tmp_path):
    # pulse_main production mode refuses any non-canonical root, so the
    # wiring of the three change sites is asserted at source level: the
    # per-tick advancing ``dialogue_cutoff=scheduled`` injection is gone.
    source = (
        Path(__file__).parents[1] / "src" / "tokmo" / "programs" / "integrated_pulse.py"
    ).read_text()
    assert "dialogue_cutoff=scheduled" not in source
    assert "resolve_production_dialogue" in source
    assert "dialogue_activation_enabled=activation.enabled" in source
    assert "dialogue_cutoff=activation.cutoff" in source


# --- composition seam (RED-1 four-combination matrix) ----------------------


def _project_root(tmp_path):
    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True, exist_ok=True)
    validator = project / "scripts" / "playbooks" / "validate_playbooks.py"
    validator.parent.mkdir(parents=True, exist_ok=True)
    validator.write_text(
        "def validate_tree(root):\n"
        "    return ['invalid'] if (root / 'personal/US/BROKEN.md').exists() else []\n",
        encoding="utf-8",
    )
    (project / ".env.toss").write_text(
        "TOSS_CLIENT_ID=test-client-id\nTOSS_CLIENT_SECRET=test-secret\n"
    )
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    return project


def _live_deps(tmp_path, *, enabled, environ):
    from tokmo.composition import build_pulse_production_live_deps

    return build_pulse_production_live_deps(
        project_root=_project_root(tmp_path),
        clock=lambda: NOW,
        runtime_root=tmp_path / "runtime-root",
        dialogue_cutoff=SLOT,
        dialogue_activation_enabled=enabled,
        environ=environ,
    )


_ENV = {
    "TELEGRAM_STOCK_BOT_TOKEN": "123456:test-token",
    "TELEGRAM_STOCK_BOT_CHAT_ID": "777",
}


def test_live_telegram_updates_filters_to_authorized_private_chat():
    from tokmo.facts import telegram_dialogue_live as live

    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 10,
                "message": {
                    "message_id": 20,
                    "from": {"id": 777},
                    "chat": {"id": 777, "type": "private"},
                    "text": "추가: 이유",
                    "reply_to_message": {"message_id": 101},
                },
            },
            {
                "update_id": 11,
                "message": {
                    "message_id": 21,
                    "from": {"id": 999},
                    "chat": {"id": 999, "type": "private"},
                    "text": "unauthorized",
                },
            },
            {
                "update_id": 12,
                "message": {
                    "message_id": 22,
                    "from": {"id": 777},
                    "chat": {"id": 777, "type": "group"},
                    "text": "group",
                },
            },
            {
                "update_id": 13,
                "message": {
                    "message_id": 23,
                    "from": {"id": 777},
                    "chat": {"id": 777, "type": "private"},
                    "photo": [],
                },
            },
        ],
    }
    seen = []

    def opener(url, timeout):
        seen.append((url, timeout))
        return json.dumps(payload).encode()

    updates = live.fetch_dialogue_updates(
        offset=10, bot_token="123456:test-token", chat_id="777", opener=opener
    )
    assert updates[0] == {
        "update_id": 10,
        "message_id": 20,
        "reply_to_message_id": 101,
        "text": "추가: 이유",
    }
    assert updates[1:] == (
        {"update_id": 11, "ignored": True},
        {"update_id": 12, "ignored": True},
        {"update_id": 13, "ignored": True},
    )
    assert len(seen) == 1
    assert "offset=10" in seen[0][0]
    assert "timeout=0" in seen[0][0]
    assert "allowed_updates=" in seen[0][0]


def test_live_telegram_updates_requires_exact_sender_identity():
    # SOL BLOCKER: a matching private chat alone must never authorize a
    # message — the sender (message.from.id) must ALSO be the configured
    # allowed identity, exactly as the callback path already requires.
    from tokmo.facts import telegram_dialogue_live as live

    malicious = "추가: 공격자가 주입한 매매 사유"
    payload = {
        "ok": True,
        "result": [
            {
                "update_id": 30,
                "message": {
                    "message_id": 40,
                    "from": {"id": 424242},
                    "chat": {"id": 777, "type": "private"},
                    "text": malicious,
                    "reply_to_message_id": 101,
                },
            },
            {
                "update_id": 31,
                "message": {
                    "message_id": 41,
                    "chat": {"id": 777, "type": "private"},
                    "text": malicious,
                },
            },
            {
                "update_id": 32,
                "message": {
                    "message_id": 42,
                    "from": {},
                    "chat": {"id": 777, "type": "private"},
                    "text": malicious,
                },
            },
        ],
    }
    updates = live.fetch_dialogue_updates(
        offset=30,
        bot_token="123456:test-token",
        chat_id="777",
        opener=lambda url, timeout: json.dumps(payload).encode(),
    )
    # content-free ignored envelopes: cursor identity only, zero retained
    # text, message id, or reply binding from the unauthorized sender.
    assert updates == (
        {"update_id": 30, "ignored": True},
        {"update_id": 31, "ignored": True},
        {"update_id": 32, "ignored": True},
    )
    for envelope in updates:
        assert set(envelope) == {"update_id", "ignored"}
    assert malicious not in json.dumps(list(updates), ensure_ascii=False)


def test_live_telegram_updates_rejects_malformed_provider_response():
    from tokmo.facts import telegram_dialogue_live as live

    with pytest.raises(live.DialogueUpdatesRejected):
        live.fetch_dialogue_updates(
            offset=0,
            bot_token="token",
            chat_id="777",
            opener=lambda url, timeout: b'{"ok":false}',
        )


def test_matrix_gate_absent_env_absent_is_legacy(tmp_path):
    deps = _live_deps(tmp_path, enabled=False, environ={})
    assert deps.dialogue_activation_enabled is False
    assert deps.telegram_sender is None


def test_matrix_gate_absent_env_present_stays_legacy_no_send(tmp_path):
    deps = _live_deps(tmp_path, enabled=False, environ=dict(_ENV))
    assert deps.dialogue_activation_enabled is False


def test_matrix_gate_true_env_absent_yields_activation_without_sender(tmp_path):
    deps = _live_deps(tmp_path, enabled=True, environ={})
    assert deps.dialogue_activation_enabled is True
    assert deps.telegram_sender is None
    partial = _live_deps(
        tmp_path,
        enabled=True,
        environ={"TELEGRAM_STOCK_BOT_TOKEN": "123456:test-token"},
    )
    assert partial.telegram_sender is None


def test_matrix_gate_true_env_present_composes_sender(tmp_path):
    deps = _live_deps(tmp_path, enabled=True, environ=dict(_ENV))
    assert deps.dialogue_activation_enabled is True
    assert callable(deps.telegram_sender)
    assert callable(deps.updates_provider)


def test_composed_sender_supports_markup_and_callback_ack(tmp_path):
    import inspect

    deps = _live_deps(tmp_path, enabled=True, environ=dict(_ENV))
    # The outbox flush passes reply_markup for question/preview bubbles.
    assert "reply_markup" in inspect.signature(deps.telegram_sender).parameters
    # Same stock-bot token acks callback queries so the spinner clears.
    assert callable(deps.callback_ack)
    no_env = _live_deps(tmp_path, enabled=True, environ={})
    assert no_env.callback_ack is None


def test_inbound_stage_wires_callback_ack():
    source = (
        Path(__file__).parents[1] / "src" / "tokmo" / "programs" / "integrated_pulse.py"
    ).read_text()
    assert "callback_ack=deps.callback_ack" in source


def test_default_composition_keeps_activation_false(tmp_path):
    from tokmo.composition import build_pulse_production_live_deps

    deps = build_pulse_production_live_deps(
        project_root=_project_root(tmp_path),
        clock=lambda: NOW,
        runtime_root=tmp_path / "runtime-root",
        dialogue_cutoff=SLOT,
    )
    assert deps.dialogue_activation_enabled is False
    assert deps.telegram_sender is None


def test_live_composition_wires_canonical_playbook_root_and_validator(tmp_path):
    from tokmo.composition import build_pulse_production_live_deps

    project = _project_root(tmp_path)
    playbooks = project / "playbooks"
    (playbooks / "personal" / "US").mkdir(parents=True)
    (playbooks / "personal" / "US" / "BROKEN.md").write_text(
        'playbook_id: "personal:US:BROKEN"\n', encoding="utf-8"
    )

    deps = build_pulse_production_live_deps(
        project_root=project,
        clock=lambda: NOW,
        runtime_root=tmp_path / "runtime-root",
        dialogue_cutoff=SLOT,
        dialogue_activation_enabled=True,
        environ=dict(_ENV),
    )

    assert deps.playbook_root == playbooks
    assert deps.playbook_validator(playbooks)


# --- tick-level matrix: send step fails, collection lanes never halt -------


def _cutover_registry():
    entries = {}
    for operation in sorted(LANE_GOVERNED_OPERATIONS):
        if operation == "CONDITIONAL_ORDERS":
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S, None, None
            )
        elif operation in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S, OperationLane.CADENCE_300S, 0
            )
        else:
            entries[operation] = OperationLaneV1(
                operation, OperationLane.CADENCE_300S, None, None
            )
    return OperationLaneRegistryV1(entries=entries)


def _kr_day():
    from tokmo.records.calendar_lookahead import (
        LookaheadDayV1,
        LookaheadSessionV1,
    )

    return LookaheadDayV1(
        Market.KR,
        "2026-08-18",
        "2026-08-18",
        (
            LookaheadSessionV1(
                "regularMarket",
                _utc("2026-08-18T00:00:00Z"),
                _utc("2026-08-18T06:30:00Z"),
            ),
        ),
        NOW,
        "calendar:KR:2026-08-18:0",
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
    return CurrentHoldingsV1(1, accounts, True, _utc("2026-08-18T00:55:00Z"), identity)


class _Refs:
    def scopes(self):
        return frozenset({TOSS_SCOPE})

    def reveal(self, scope):
        return "native-ref-1" if scope == TOSS_SCOPE else None


class _Providers:
    def __init__(self):
        self.token_calls = 0
        self.quote_calls = 0
        self.page_calls = 0

    def token(self):
        self.token_calls += 1
        return "token-value"

    def quotes(self, market, symbols):
        self.quote_calls += 1
        facts = tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1",
                symbol=symbol,
                last_price="70000",
                currency_raw="KRW",
                timestamp=_utc("2026-08-18T01:00:00Z"),
                fetched_at=NOW,
            )
            for symbol in symbols
        )
        return PulseQuoteBatchOutcomeV1("success", facts, ())

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
            filled_at=_utc("2026-08-18T00:59:00Z"),
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
            ordered_at=_utc("2026-08-18T00:58:00Z"),
            execution=execution,
        )
        return PulseOrdersPageOutcomeV1("success", (order,), None, False)

    def detail(self, *, account_scope, native_ref, order_id):
        return None


def _deps(tmp_path, providers, *, activation, sender, cutoff=None):
    return PulseDepsV1(
        clock=lambda: NOW,
        lookahead_reader=lambda market: (_kr_day(),) if market is Market.KR else (),
        holdings_reader=lambda: _holdings(),
        native_refs_reader=lambda: _Refs(),
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        token_fetch=providers.token,
        quotes_fetch=providers.quotes,
        orders_page_fetch=providers.orders,
        order_detail_fetch=providers.detail,
        quote_store=LatestQuoteStore(
            tmp_path / "stores" / "latest_quotes.sqlite3", clock=lambda: NOW
        ),
        ledger_store=TradeLedgerStore(
            tmp_path / "stores" / "trade_ledger.sqlite3",
            clock=lambda: NOW,
            id_factory=lambda: f"batch-{next(_COUNTER):06d}",
        ),
        scan_state_store=OrderScanStateStore(
            tmp_path / "stores" / "order_scan_state.sqlite3", clock=lambda: NOW
        ),
        dialogue_store=ExecutionDialogueStore(
            tmp_path / "stores" / "execution_dialogue.sqlite3", clock=lambda: NOW
        ),
        dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda root: [],
        dialogue_cutoff=cutoff or SLOT,
        telegram_sender=sender,
        dialogue_activation_enabled=activation,
        lane_registry=_cutover_registry(),
        quotes_provider_enabled=True,
        execution_provider_enabled=True,
    )


def _dialogue_store(tmp_path):
    return ExecutionDialogueStore(
        tmp_path / "stores" / "execution_dialogue.sqlite3", clock=lambda: NOW
    )


def _question_key():
    event_id = trade_event_id("toss-orders-v1", TOSS_SCOPE, "ord-001")
    return event_id, dialogue_dedupe_key(
        event_id, "trade_reason_question", PROMPT_VERSION
    )


def test_gate_true_env_missing_fails_send_step_only_with_signal(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers, activation=True, sender=None),
    )
    stages = {stage["stage"]: stage for stage in result.stages}
    # send step failure is a durable health signal on the dialogue stage only
    assert stages["dialogue"]["status"] == "failed"
    assert stages["dialogue"]["reason"] == "send_unavailable"
    assert result.outcome == "PARTIAL"
    # collection lanes proceed untouched
    assert stages["quotes"]["status"] == "saved"
    assert stages["execution"]["status"] == "saved"
    assert providers.quote_calls == 1
    assert providers.page_calls == 1
    # register/enqueue proceeded: the question row survives as PENDING
    event_id, key = _question_key()
    store = _dialogue_store(tmp_path)
    assert store.read_outbox(key)["status"] == "PENDING"
    assert store.read_event(event_id)["state"] is TradeEventState.ENQUEUED


def test_gate_absent_is_legacy_saved_tick_no_signal(tmp_path):
    providers = _Providers()
    result = run_pulse_once(
        root=tmp_path,
        policy_version="pulse-60s-v1",
        scheduled_for_utc=SLOT,
        observed_now_utc=NOW,
        deps=_deps(tmp_path, providers, activation=False, sender=None),
    )
    assert result.outcome == "SAVED"
    stages = {stage["stage"]: stage for stage in result.stages}
    assert stages["dialogue"]["status"] == "saved"
    _event_id, key = _question_key()
    assert _dialogue_store(tmp_path).read_outbox(key)["status"] == "PENDING"


# --- RED-3: fixed cutoff sends new rows only, backlog stays ---------------


class _Sender:
    def __init__(self, error=None):
        self.sent = []
        self.error = error
        self._next = 100

    def __call__(self, text, reply_markup=None):
        if self.error is not None:
            raise self.error
        self.sent.append(text)
        self._next += 1
        return self._next


def _times(clock_now):
    return TradeEventTimesV1(
        filled_at=clock_now,
        provider_observed_at=None,
        discovered_at=clock_now,
        stored_at=clock_now,
        notified_at=None,
    )


def _new_event(order_id, clock_now, symbol="TSLA"):
    event_id = trade_event_id("toss-orders-v1", TOSS_SCOPE, order_id)
    return NewTradeExecutionV1(
        event_id,
        TOSS_SCOPE,
        order_id,
        symbol,
        "SELL",
        "USD",
        "3",
        "90.5",
        "271.5",
        "US",
        _times(clock_now),
    )


def _run(store, events, *, sender, activation, cutoff, now):
    return run_execution_dialogue(
        new_events=tuple(events),
        dialogue_store=store,
        quote_reader=lambda symbol: None,
        playbook_root=None,
        ai_evaluator=None,
        sender=sender,
        activation_enabled=activation,
        send_eligible_after=cutoff,
        clock=lambda: now,
        now=now,
    )


def test_backlog_before_cutoff_is_never_sent_only_new_rows_send(tmp_path):
    clock = {"now": _utc("2026-08-14T01:00:00Z")}
    store = ExecutionDialogueStore(
        tmp_path / "execution_dialogue.sqlite3", clock=lambda: clock["now"]
    )
    backlog = [
        _new_event(
            f"backlog-{index}",
            clock["now"],
            symbol=("TSLA", "TSLA", "TSLA", "SPCX", "SPCX", "MSFT")[index],
        )
        for index in range(6)
    ]
    _run(
        store,
        backlog,
        sender=None,
        activation=False,
        cutoff=clock["now"],
        now=clock["now"],
    )
    assert len(store.list_outbox(status="PENDING")) == 6

    cutoff = _utc("2026-08-18T00:00:00Z")  # approved fixed cutoff
    clock["now"] = _utc("2026-08-18T01:00:00Z")
    sender = _Sender()
    result = _run(
        store,
        [_new_event("fresh-1", clock["now"])],
        sender=sender,
        activation=True,
        cutoff=cutoff,
        now=clock["now"],
    )
    assert result.send_calls == 1
    assert len(sender.sent) == 1
    backlog_rows = [
        store.read_outbox(
            dialogue_dedupe_key(
                event.trade_event_id, "trade_reason_question", PROMPT_VERSION
            )
        )
        for event in backlog
    ]
    assert all(row["status"] == "PENDING" for row in backlog_rows)


def test_malformed_gate_or_naive_cutoff_locks_no_send(tmp_path):
    clock_now = _utc("2026-08-18T01:00:00Z")
    store = ExecutionDialogueStore(
        tmp_path / "execution_dialogue.sqlite3", clock=lambda: clock_now
    )
    sender = _Sender()
    result = _run(
        store,
        [_new_event("ord-x", clock_now)],
        sender=sender,
        activation=True,
        cutoff=datetime(2026, 8, 18),
        now=clock_now,
    )
    assert result.send_calls == 0
    assert sender.sent == []


# --- RED-4: settle semantics ----------------------------------------------


def test_timeout_unknown_429_failed_normal_sent_with_message_id(tmp_path):
    clock_now = _utc("2026-08-18T01:00:00Z")

    def store_for(name):
        return ExecutionDialogueStore(
            tmp_path / f"{name}.sqlite3", clock=lambda: clock_now
        )

    cutoff = _utc("2026-08-18T00:00:00Z")
    # timeout -> UNKNOWN, never resent
    store = store_for("timeout")
    sender = _Sender(error=TimeoutError("timed out"))
    _run(
        store,
        [_new_event("ord-t", clock_now)],
        sender=sender,
        activation=True,
        cutoff=cutoff,
        now=clock_now,
    )
    key = dialogue_dedupe_key(
        _new_event("ord-t", clock_now).trade_event_id,
        "trade_reason_question",
        PROMPT_VERSION,
    )
    assert store.read_outbox(key)["status"] == "UNKNOWN"
    retry = _Sender()
    _run(store, [], sender=retry, activation=True, cutoff=cutoff, now=clock_now)
    assert retry.sent == []

    # simulated 429 (non-timeout exception) -> FAILED terminal, no retry
    store = store_for("rate-limit")
    error = RuntimeError("telegram send rejected: 429 Too Many Requests")
    _run(
        store,
        [_new_event("ord-r", clock_now)],
        sender=_Sender(error=error),
        activation=True,
        cutoff=cutoff,
        now=clock_now,
    )
    key = dialogue_dedupe_key(
        _new_event("ord-r", clock_now).trade_event_id,
        "trade_reason_question",
        PROMPT_VERSION,
    )
    assert store.read_outbox(key)["status"] == "FAILED"
    retry = _Sender()
    _run(store, [], sender=retry, activation=True, cutoff=cutoff, now=clock_now)
    assert retry.sent == []

    # normal -> SENT exactly once with the provider message id bound
    store = store_for("sent")
    sender = _Sender()
    _run(
        store,
        [_new_event("ord-s", clock_now)],
        sender=sender,
        activation=True,
        cutoff=cutoff,
        now=clock_now,
    )
    key = dialogue_dedupe_key(
        _new_event("ord-s", clock_now).trade_event_id,
        "trade_reason_question",
        PROMPT_VERSION,
    )
    row = store.read_outbox(key)
    assert row["status"] == "SENT"
    assert row["telegram_message_id"] == 101
    _run(store, [], sender=sender, activation=True, cutoff=cutoff, now=clock_now)
    assert len(sender.sent) == 1


# --- RED-4b: new opener contract parses the response body ------------------


def test_adapter_parses_message_id_from_response_body():
    from tokmo.facts.telegram_dialogue_live import send_dialogue_message

    calls = []

    def opener(url, body, timeout):
        calls.append((url, body, timeout))
        return b'{"ok": true, "result": {"message_id": 4242}}'

    message_id = send_dialogue_message(
        body_text="[개인] TSLA 매도 체결",
        bot_token="123456:tok",
        chat_id="777",
        opener=opener,
    )
    assert message_id == 4242
    url, body, timeout = calls[0]
    assert url == "https://api.telegram.org/bot123456:tok/sendMessage"
    assert b"chat_id=777" in body
    assert timeout == 10.0


def test_adapter_never_swallows_failures_unlike_lifecycle_opener():
    from tokmo.facts.telegram_dialogue_live import (
        DialogueSendRejected,
        send_dialogue_message,
    )

    def timeout_opener(url, body, timeout):
        raise TimeoutError("socket timeout")

    with pytest.raises(TimeoutError):
        send_dialogue_message(
            body_text="x", bot_token="t", chat_id="c", opener=timeout_opener
        )

    def rejected_opener(url, body, timeout):
        return b'{"ok": false, "description": "chat not found"}'

    with pytest.raises(DialogueSendRejected):
        send_dialogue_message(
            body_text="x", bot_token="t", chat_id="c", opener=rejected_opener
        )

    def garbage_opener(url, body, timeout):
        return b"<html>gateway error</html>"

    with pytest.raises(Exception):
        send_dialogue_message(
            body_text="x", bot_token="t", chat_id="c", opener=garbage_opener
        )


def test_dialogue_transport_is_a_registered_live_module():
    relative = Path("src/tokmo/facts/telegram_dialogue_live.py")
    assert (Path(__file__).parents[1] / relative).exists()
    architecture = (Path(__file__).parent / "test_architecture.py").read_text()
    assert "facts/telegram_dialogue_live.py" in architecture


# --- RED-5: zero effect on quotes/trades/execution; eligibility persists ---


def test_activation_has_zero_effect_on_collection_lanes(tmp_path):
    roots = {"off": tmp_path / "off", "on": tmp_path / "on"}
    results = {}
    providers = {}
    for label, root in roots.items():
        provider = _Providers()
        providers[label] = provider
        sender = _Sender() if label == "on" else None
        results[label] = run_pulse_once(
            root=root,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=SLOT,
            observed_now_utc=NOW,
            deps=_deps(
                root,
                provider,
                activation=(label == "on"),
                sender=sender,
                cutoff=_utc("2026-08-18T00:00:00Z"),
            ),
        )
    assert results["off"].outcome == results["on"].outcome == "SAVED"
    assert dict(results["off"].wire_counts) == dict(results["on"].wire_counts)
    for label in ("off", "on"):
        assert providers[label].quote_calls == 1
        assert providers[label].page_calls == 1
    off_stages = {s["stage"]: s for s in results["off"].stages}
    on_stages = {s["stage"]: s for s in results["on"].stages}
    for stage in ("quotes", "returns", "execution"):
        assert off_stages[stage] == on_stages[stage]


def test_fixed_cutoff_keeps_unsent_rows_eligible_across_ticks(tmp_path):
    fixed_cutoff = _utc("2026-08-18T00:00:00Z")
    tick_n = _utc("2026-08-18T01:00:00Z")
    tick_n1 = _utc("2026-08-18T01:01:00Z")
    clock = {"now": tick_n}
    store = ExecutionDialogueStore(
        tmp_path / "execution_dialogue.sqlite3", clock=lambda: clock["now"]
    )
    # tick N: activation on but env missing -> row enqueued, unsent
    result_n = _run(
        store,
        [_new_event("ord-n", tick_n)],
        sender=None,
        activation=True,
        cutoff=fixed_cutoff,
        now=tick_n,
    )
    assert result_n.status == "send_unavailable"
    assert result_n.enqueued == 1
    # tick N+1 with the SAME fixed cutoff: the row is still eligible.
    # (The abolished per-tick advancing cutoff — scheduled(N+1) — would
    # starve it: created_at(N) < scheduled(N+1).)
    clock["now"] = tick_n1
    sender = _Sender()
    result_n1 = _run(
        store, [], sender=sender, activation=True, cutoff=fixed_cutoff, now=tick_n1
    )
    assert result_n1.send_calls == 1
    assert len(sender.sent) == 1
    key = dialogue_dedupe_key(
        _new_event("ord-n", tick_n).trade_event_id,
        "trade_reason_question",
        PROMPT_VERSION,
    )
    assert store.read_outbox(key)["status"] == "SENT"
    # counter-proof: an advancing cutoff would have found zero rows
    assert len(store.list_outbox(status="PENDING", send_eligible_after=tick_n1)) == 0
