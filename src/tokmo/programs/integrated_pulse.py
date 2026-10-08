"""One Pulse tick assembly (Plan §9.2–§12): lease → gate → stages → receipt.

The runner owns the Pulse-only lease (never the 300s ``cadence.lock``), the
MISSED/OVERLAP/DUPLICATE terminal semantics with catch-up forbidden, the
startup lane-ownership fail-closed check, the per-tick wire ceilings
(core token1 + quotes2 + trades3 + conditional1 = 7, Quant4, PAPER6),
the core35/combined85 rolling five-minute auto-pause, and
the crash reconcile of stale STARTED receipts and wires. Stages fail
independently; a quotes failure never blocks execution ingestion and vice
versa.
"""

from dataclasses import dataclass, field
import argparse
import fcntl
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Callable, Mapping

from tokmo.programs.conditional_detection import run_conditional_detection
from tokmo.programs.execution_detection import run_pulse_execution
from tokmo.programs.execution_dialogue import (
    consume_inbound,
    run_execution_dialogue,
)
from tokmo.programs.news_production import (
    production_stage_report,
    run_news_production,
)
from tokmo.programs.pulse_quotes import run_pulse_quotes
from tokmo.programs.pulse_quotes import QUOTE_SOURCE
from tokmo.programs.return_projection import project_returns
from tokmo.records.dialogue_activation import (
    GATE_FILE_NAME,
    DialogueActivationV1,
    read_dialogue_activation,
)
from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationOwnershipError,
    validate_lane_startup,
)
from tokmo.values.pulse_policy import (
    PULSE_POLICY_REGISTRY,
    PULSE_ACTIVE_WIRE_STARTS, PULSE_QUANT_INPUT_STARTS, PULSE_OPPORTUNITY_INPUT_STARTS,
    PULSE_REQUEST_LIMITS_V1,
    PULSE_TRADES_CHILD_LIMITS_V1,
    pulse_tick_v1,
)

ROLLING_WINDOW_SECONDS = 300
ROLLING_WIRE_CEILING = 5 * PULSE_ACTIVE_WIRE_STARTS
_PAUSE_MARKER = "pulse-paused.marker"
_LOCK_NAME = "pulse-60s.lock"


class PulseRefusal(RuntimeError):
    """Configuration/preflight refusal before any provider work."""


class PulseCeilingError(RuntimeError):
    """A stage tried to start a wire beyond the per-tick ceiling."""


def _parse_cli_utc(text: str) -> datetime:
    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp must be timezone-aware")
    return parsed


@dataclass(frozen=True)
class PulseDepsV1:
    clock: Callable
    lookahead_reader: Callable
    holdings_reader: Callable
    native_refs_reader: Callable
    watchlist_symbols: Mapping
    token_fetch: Callable
    quotes_fetch: Callable
    orders_page_fetch: Callable
    order_detail_fetch: Callable
    quote_store: object
    ledger_store: object
    scan_state_store: object
    dialogue_store: object
    dialogue_quote_reader: Callable
    playbook_validator: Callable
    dialogue_cutoff: datetime
    playbook_root: "Path | None" = None
    ai_evaluator: "Callable | None" = None
    telegram_sender: "Callable | None" = None
    updates_provider: "Callable | None" = None
    callback_ack: "Callable | None" = None
    news_callback_handler: "Callable | None" = None
    news_outbox_reconcile: "Callable | None" = None
    news_delivery_runner: "Callable | None" = None
    quant_market_close_runner: "Callable | None" = None
    # Optional consumer of the ALREADY acquired bearer. It cannot request
    # authentication through this seam. Its capture has separate receipts.
    quant_input_capture: "Callable | None" = None
    opportunity_capture: "Callable | None" = None
    # Inert data bundle only (SDS §6.8): no producer-executing callable
    # exists on deps; the stage runs solely inside run_pulse_once's flock.
    news_producer_deps: "object | None" = None
    conditional_orders_fetch: "Callable | None" = None
    dialogue_activation_enabled: bool = False
    quotes_provider_enabled: bool = False
    execution_provider_enabled: bool = False
    conditional_provider_enabled: bool = False
    lane_registry: OperationLaneRegistryV1 = OPERATION_LANE_REGISTRY_V1
    tick_cleanup: Callable = lambda: None
    # At 5-minute boundaries, production waits for the same-slot cadence
    # receipt to become COMPLETE before minting a Pulse token.  This prevents
    # the provider's single-valid-token rotation from invalidating cadence.
    cadence_boundary_gate: Callable = lambda scheduled_for_utc: True


@dataclass(frozen=True)
class PulseTickResultV1:
    outcome: str
    reason: "str | None"
    policy_version: str
    tick_id: int
    lag_seconds: float
    stages: tuple = ()
    wire_counts: Mapping = field(default_factory=dict)
    rolling_wire_starts: int = 0
    exit_code: int = 0


def make_wire_recorder(receipts: PulseTickReceiptStore, policy_version: str,
                       tick_id: int, *, opportunity_enabled=False, quant_enabled=False):
    """Per-tick ceiling-enforcing wire recorder (§12.2). No hidden retry:
    every start consumes budget whether or not the call later succeeds."""
    counts = {"TOKEN": 0, "PRICE_BATCH": 0, "ORDERS_PAGE": 0,
              "ORDER_DETAIL": 0, "CONDITIONAL_ORDERS": 0}
    limits = {
        "TOKEN": PULSE_REQUEST_LIMITS_V1.token_starts,
        "PRICE_BATCH": PULSE_REQUEST_LIMITS_V1.quote_starts,
        "ORDERS_PAGE": PULSE_TRADES_CHILD_LIMITS_V1.orders_page_starts,
        "ORDER_DETAIL": PULSE_TRADES_CHILD_LIMITS_V1.order_detail_starts,
        "CONDITIONAL_ORDERS":
            PULSE_REQUEST_LIMITS_V1.conditional_order_starts,
    }
    if opportunity_enabled:
        counts['OPPORTUNITY_READ'] = 0
        limits['OPPORTUNITY_READ'] = PULSE_OPPORTUNITY_INPUT_STARTS
    if quant_enabled:
        counts['QUANT_READ'] = 0
        limits['QUANT_READ'] = PULSE_QUANT_INPUT_STARTS
    started = []

    def recorder(operation, native_identity):
        if operation not in counts:
            raise PulseCeilingError("operation is not pulse-expressible")
        if counts[operation] >= limits[operation]:
            raise PulseCeilingError(f"{operation} ceiling exceeded")
        trades_used = counts["ORDERS_PAGE"] + counts["ORDER_DETAIL"]
        if operation in ("ORDERS_PAGE", "ORDER_DETAIL") and trades_used >= \
                PULSE_REQUEST_LIMITS_V1.trades_starts:
            raise PulseCeilingError("trades group ceiling exceeded")
        if sum(counts.values()) >= PULSE_ACTIVE_WIRE_STARTS:
            raise PulseCeilingError("total wire ceiling exceeded")
        seq = receipts.start_wire(policy_version, tick_id, operation,
                                  native_identity)
        counts[operation] += 1
        started.append(seq)
        return seq

    recorder.counts = counts
    recorder.started = started
    return recorder


def _run_news_producer_stage(producer_deps, observed_now):
    """Sole caller of run_news_production; its one callsite lives inside
    run_pulse_once's held-flock lexical scope (SDS §6.8, R-LCK-2)."""
    return run_news_production(producer_deps=producer_deps, now=observed_now)


def _acquire_pulse_lock(runtime_dir: Path):
    runtime_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(runtime_dir / _LOCK_NAME, flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return descriptor
    except BlockingIOError:
        os.close(descriptor)
        return None


def build_fixture_pulse_deps(*, fixture: dict, clock: Callable,
                             root: Path) -> PulseDepsV1:
    """Fixture-only deps (G1): every provider seam is served from the given
    JSON document; no transport, credential, or runtime-root access exists
    on any path. Stores are created under the isolated ``root``."""
    from tokmo.programs.execution_detection import PulseOrdersPageOutcomeV1
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.records.calendar_lookahead import (
        LookaheadDayV1,
        LookaheadSessionV1,
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
    from tokmo.values.session_gate import Market

    if type(fixture) is not dict:
        raise PulseRefusal("invalid pulse fixture")
    root = Path(root)

    def _parse(text):
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            raise PulseRefusal("fixture timestamps must be aware")
        return parsed

    def lookahead_reader(market):
        days = []
        for raw in fixture.get("lookahead", {}).get(market.value, []):
            sessions = tuple(
                LookaheadSessionV1(item["kind"], _parse(item["start"]),
                                   _parse(item["end"]))
                for item in raw["sessions"]
            )
            days.append(LookaheadDayV1(
                market, raw["trade_date"], raw["source_trade_date"],
                sessions, _parse(raw["fetched_at"]), raw["provenance"]))
        return tuple(days)

    def holdings_reader():
        raw = fixture.get("holdings")
        if raw is None:
            return None
        identity = {}
        accounts = []
        for ordinal, account in enumerate(raw["accounts"], start=1):
            scope = account["account_scope"]
            identity[scope] = AccountScopeIdentityV1(
                scope, account.get("label"), ordinal, None)
            accounts.append(AccountHoldingsV1(scope, ordinal, tuple(
                HoldingItemV1(item["symbol"], item["quantity"],
                              item["avg_price"], item.get("currency"),
                              item.get("market_raw"), item.get("market_field"))
                for item in account["holdings"])))
        return CurrentHoldingsV1(
            1, tuple(accounts), True, _parse(raw["captured_at"]), identity)

    class _FixtureRefs:
        def scopes(self):
            return frozenset(fixture.get("native_refs", {}))

        def reveal(self, scope):
            return fixture.get("native_refs", {}).get(scope)

    def quotes_fetch(market, symbols):
        table = fixture.get("quotes", {})
        facts = []
        failed = []
        for symbol in symbols:
            raw = table.get(symbol)
            if raw is None:
                failed.append(symbol)
                continue
            facts.append(TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=symbol,
                last_price=raw["last_price"], currency_raw=raw["currency"],
                timestamp=_parse(raw["timestamp"]),
                fetched_at=_parse(raw["fetched_at"])))
        return PulseQuoteBatchOutcomeV1("success", tuple(facts),
                                        tuple(failed))

    def orders_page_fetch(*, account_scope, native_ref, status, from_date,
                          to_date, cursor, limit):
        del native_ref, status, from_date, to_date, cursor, limit
        raw = fixture.get("orders", {}).get(account_scope, [])
        from tokmo.values.toss_orders import Order, OrderExecution
        orders = tuple(
            Order(
                order_id=item["order_id"], symbol=item["symbol"],
                side=item["side"], order_type="LIMIT", time_in_force="DAY",
                status=item.get("status", "FILLED"),
                quantity=item["quantity"], currency=item["currency"],
                ordered_at=_parse(item["ordered_at"]),
                execution=OrderExecution(
                    filled_quantity=item["filled_quantity"],
                    average_filled_price=item.get("average_filled_price"),
                    filled_amount=item.get("filled_amount"),
                    commission=None, tax=None,
                    filled_at=_parse(item["filled_at"]),
                    settlement_date=None))
            for item in raw
        )
        return PulseOrdersPageOutcomeV1("success", orders, None, False)

    def order_detail_fetch(*, account_scope, native_ref, order_id):
        del account_scope, native_ref, order_id
        return None

    watchlists = {
        Market(market): frozenset(symbols)
        for market, symbols in fixture.get("watchlists", {}).items()
    }
    for market in (Market.KR, Market.US):
        watchlists.setdefault(market, frozenset())
    _counter = iter(range(1, 1_000_000))
    return PulseDepsV1(
        clock=clock,
        lookahead_reader=lookahead_reader,
        holdings_reader=holdings_reader,
        native_refs_reader=lambda: _FixtureRefs(),
        watchlist_symbols=watchlists,
        token_fetch=lambda: "fixture-token",
        quotes_fetch=quotes_fetch,
        orders_page_fetch=orders_page_fetch,
        order_detail_fetch=order_detail_fetch,
        quote_store=LatestQuoteStore(
            root / "stores" / "latest_quotes.sqlite3", clock=clock),
        ledger_store=TradeLedgerStore(
            root / "stores" / "trade_ledger.sqlite3", clock=clock,
            id_factory=lambda: f"pulse-fixture-{next(_counter):06d}"),
        scan_state_store=OrderScanStateStore(
            root / "stores" / "order_scan_state.sqlite3", clock=clock),
        dialogue_store=ExecutionDialogueStore(
            root / "stores" / "execution_dialogue.sqlite3", clock=clock),
        dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda playbook_root: [],
        dialogue_cutoff=clock(),
        quotes_provider_enabled=bool(
            fixture.get("quotes_provider_enabled", False)),
        execution_provider_enabled=bool(
            fixture.get("execution_provider_enabled", False)),
        lane_registry=OPERATION_LANE_REGISTRY_V1,
    )


def resolve_production_dialogue(canonical_root: Path,
                                scheduled: datetime) -> DialogueActivationV1:
    """Resolve the send-activation gate for one production tick (§4.2).

    Enabled gate: the gate's FIXED approved cutoff is injected, replacing
    the abolished per-tick advancing ``scheduled`` cutoff that permanently
    starved rows left unsent in their own tick. Absent/false/malformed
    gate: legacy behavior — activation off, cutoff ``scheduled`` (inert,
    since nothing sends while activation is off).
    """
    activation = read_dialogue_activation(
        Path(canonical_root) / "runtime" / GATE_FILE_NAME)
    if activation.enabled and activation.cutoff is not None:
        return activation
    return DialogueActivationV1(False, scheduled, None)


def pulse_main(argv: list[str] | None = None, *, environ=None, capture_clock=None) -> int:
    """Run exactly one isolated Pulse slot.

    This entry stays outside ``tokmo.composition`` so the existing 300-second
    production composition remains within its reviewed architecture seal.
    Production assembles canonical dependencies and validates lane ownership
    before stage execution; fixture mode has no credential or network path.
    """
    parser = argparse.ArgumentParser(prog="tokmo-pulse", add_help=True)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--mode", default="production")
    parser.add_argument("--policy-version", default="pulse-60s-v1")
    parser.add_argument(
        "--scheduled-for", "--scheduled-for-utc", dest="scheduled_for"
    )
    parser.add_argument("--observed-now")
    parser.add_argument("--root")
    parser.add_argument("--fixture")
    parser.add_argument("--project-root")
    parser.add_argument("--reconcile-only", action="store_true")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    def refuse(reason: str) -> int:
        print(f"tokmo-pulse refusal={reason}", file=sys.stderr)
        return 1

    if not args.once:
        return refuse("once_required")
    if args.reconcile_only:
        if not args.root or not args.scheduled_for:
            return refuse("root_and_scheduled_for_required")
        try:
            scheduled = _parse_cli_utc(args.scheduled_for)
            store = PulseTickReceiptStore(
                Path(args.root) / "ops" / "pulse_tick_receipts.sqlite3",
                clock=lambda: scheduled)
            store.reconcile_stale(
                now=scheduled, exclude=(args.policy_version, scheduled))
        except (OSError, ValueError):
            return refuse("reconcile_failed")
        return 0
    if args.mode not in ("fixture", "production"):
        return refuse("unsupported_mode")
    if not args.root or not args.scheduled_for:
        return refuse("root_and_scheduled_for_required")
    if args.mode == "fixture" and not args.fixture:
        return refuse("fixture_root_and_scheduled_for_required")
    try:
        scheduled = _parse_cli_utc(args.scheduled_for)
        observed = (
            scheduled
            if args.observed_now is None
            else _parse_cli_utc(args.observed_now)
        )
    except ValueError:
        return refuse("invalid_timestamp")
    try:
        if args.mode == "fixture":
            try:
                fixture = json.loads(Path(args.fixture).read_text())
            except (OSError, ValueError):
                return refuse("fixture_unreadable")
            deps = build_fixture_pulse_deps(
                fixture=fixture, clock=lambda: observed, root=Path(args.root))
        else:
            canonical_root = Path.home() / ".local" / "share" / "tokmo-v3"
            if Path(args.root).resolve() != canonical_root.resolve():
                return refuse("production_requires_canonical_root")
            if not args.project_root:
                return refuse("project_root_required")
            # The caller's root is the real directory (the ~/.local/share path may be a
            # compatibility symlink since 2026-09-24); strict stores refuse symlinked ancestors.
            canonical_root = Path(args.root)
            from tokmo.composition import build_pulse_production_live_deps
            activation = resolve_production_dialogue(canonical_root, scheduled)
            deps = build_pulse_production_live_deps(
                project_root=Path(args.project_root), clock=lambda: observed, capture_clock=capture_clock,
                runtime_root=canonical_root,
                dialogue_cutoff=activation.cutoff,
                dialogue_activation_enabled=activation.enabled,
                environ=environ)
        result = run_pulse_once(
            root=Path(args.root),
            policy_version=args.policy_version,
            scheduled_for_utc=scheduled,
            observed_now_utc=observed,
            deps=deps,
        )
    except PulseRefusal as error:
        return refuse(str(error).replace(" ", "_"))
    except Exception as error:
        print(f"tokmo-pulse failure={type(error).__name__}", file=sys.stderr)
        return 2
    print(json.dumps({
        "outcome": result.outcome,
        "reason": result.reason,
        "policy_version": result.policy_version,
        "tick_id": result.tick_id,
        "lag_seconds": result.lag_seconds,
        "stages": [dict(stage) for stage in result.stages],
        "wire_counts": dict(result.wire_counts),
        "rolling_wire_starts": result.rolling_wire_starts,
        "exit_code": result.exit_code,
    }, sort_keys=True))
    return result.exit_code


def run_pulse_once(*, root: Path, policy_version: str,
                   scheduled_for_utc: datetime,
                   observed_now_utc: datetime,
                   deps: PulseDepsV1) -> PulseTickResultV1:
    policy = PULSE_POLICY_REGISTRY.get(policy_version)
    if policy is None:
        raise PulseRefusal("unknown pulse policy")
    tick = pulse_tick_v1(policy_version, scheduled_for_utc)
    if (not isinstance(observed_now_utc, datetime)
            or observed_now_utc.tzinfo is None):
        raise PulseRefusal("observed now must be aware")
    lag = (observed_now_utc - scheduled_for_utc).total_seconds()
    if lag < 0:
        raise PulseRefusal("observed now precedes the scheduled slot")
    root = Path(root)
    receipts = PulseTickReceiptStore(
        root / "ops" / "pulse_tick_receipts.sqlite3", clock=deps.clock)

    def _result(outcome, reason, stages=(), wire_counts=None, rolling=0,
                exit_code=2):
        return PulseTickResultV1(
            outcome, reason, policy_version, tick.tick_id, lag,
            tuple(stages), dict(wire_counts or {}), rolling, exit_code)

    # §11: a late slot is terminal MISSED and never caught up.
    if lag > policy.max_start_lag_seconds:
        receipts.record_missed(
            policy_version, scheduled_for_utc, tick.tick_id,
            observed_start=observed_now_utc, lag_seconds=lag,
            max_lag_seconds=policy.max_start_lag_seconds)
        return _result("MISSED", "excess_start_lag")

    # §10.1: pulse-only lease; the 300s cadence.lock is never touched.
    descriptor = _acquire_pulse_lock(root / "runtime")
    if descriptor is None:
        receipts.record_overlap(
            policy_version, scheduled_for_utc, tick.tick_id,
            observed_start=observed_now_utc, lag_seconds=lag,
            max_lag_seconds=policy.max_start_lag_seconds)
        return _result("OVERLAP", "lease_held")
    try:
        # §11: settle crashed STARTED slots/wires before this slot begins.
        receipts.reconcile_stale(
            now=observed_now_utc,
            exclude=(policy_version, scheduled_for_utc))
        begun = receipts.begin_tick(
            policy_version, scheduled_for_utc, tick.tick_id,
            observed_start=observed_now_utc, lag_seconds=lag,
            max_lag_seconds=policy.max_start_lag_seconds)
        if begun == "duplicate":
            return _result("DUPLICATE", "duplicate_slot")

        # §8.2/§12.4: startup ownership fail-closed, provider I/O zero.
        provider_operations = []
        if deps.quotes_provider_enabled:
            provider_operations.append("PRICE_BATCH")
        if deps.execution_provider_enabled:
            provider_operations.extend(("ORDERS_PAGE", "ORDER_DETAIL"))
        if deps.conditional_provider_enabled:
            provider_operations.append("CONDITIONAL_ORDERS")
        epoch = int(scheduled_for_utc.timestamp())
        try:
            validate_lane_startup(
                deps.lane_registry, lane=OperationLane.PULSE_60S,
                provider_operations=tuple(provider_operations), epoch=epoch)
        except OperationOwnershipError:
            receipts.finish_tick(
                policy_version, scheduled_for_utc, status="FAILED",
                reason="owner_conflict", stages=(), wire_counts={},
                finished_at=deps.clock())
            return _result("FAILED", "owner_conflict")

        # The provider keeps only one valid bearer.  At a shared 5-minute
        # boundary cadence must finish its holdings token/use cycle before
        # Pulse mints a new token.  A missing, non-COMPLETE, or timed-out
        # cadence receipt fails closed before any Pulse wire start.
        if provider_operations and epoch % 300 == 0:
            if deps.cadence_boundary_gate(scheduled_for_utc) is not True:
                receipts.finish_tick(
                    policy_version, scheduled_for_utc, status="FAILED",
                    reason="cadence_not_complete", stages=(), wire_counts={},
                    finished_at=deps.clock())
                return _result("FAILED", "cadence_not_complete")

        # §12.3: rolling five-minute ceiling with durable auto-pause.
        pause_marker = root / "runtime" / _PAUSE_MARKER
        rolling = receipts.rolling_wire_starts(
            policy_version, now_epoch=epoch,
            window_seconds=ROLLING_WINDOW_SECONDS)
        if pause_marker.exists():
            receipts.finish_tick(
                policy_version, scheduled_for_utc, status="FAILED",
                reason="paused", stages=(), wire_counts={},
                finished_at=deps.clock())
            return _result("FAILED", "paused", rolling=rolling)
        prospective = (PULSE_REQUEST_LIMITS_V1.total_wire_starts
                       if provider_operations else 0)
        core_rolling = receipts.rolling_wire_starts(policy_version, now_epoch=epoch,
            window_seconds=ROLLING_WINDOW_SECONDS, core_only=True)
        if (core_rolling + prospective > 5 * PULSE_REQUEST_LIMITS_V1.total_wire_starts
                or rolling + PULSE_ACTIVE_WIRE_STARTS > ROLLING_WIRE_CEILING):
            pause_marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            pause_marker.write_text("rolling_ceiling_exceeded")
            os.chmod(pause_marker, 0o600)
            receipts.finish_tick(
                policy_version, scheduled_for_utc, status="FAILED",
                reason="rolling_ceiling_exceeded", stages=(), wire_counts={},
                finished_at=deps.clock())
            return _result("FAILED", "rolling_ceiling_exceeded",
                           rolling=rolling)

        recorder = make_wire_recorder(receipts, policy_version, tick.tick_id,
                                      opportunity_enabled=deps.opportunity_capture is not None,
                                      quant_enabled=deps.quant_input_capture is not None)
        token_state = {"value": None}

        def shared_token():
            # Lane-local lazy token (§8.1): at most one TOKEN wire per tick,
            # shared by the quotes and execution stages; never persisted.
            if token_state["value"] is None:
                recorder("TOKEN", "token:tick0")
                token_state["value"] = deps.token_fetch()
            return token_state["value"]

        stages = []

        def _stage(name, status, reason):
            stages.append({"stage": name, "status": status, "reason": reason})

        # 1) quotes (§13–§15)
        quotes_result = None
        if not deps.quotes_provider_enabled:
            _stage("quotes", "disabled", "owner_not_granted")
        else:
            try:
                quotes_result = run_pulse_quotes(
                    tick=tick, lookahead_reader=deps.lookahead_reader,
                    holdings_reader=deps.holdings_reader,
                    watchlist_symbols=deps.watchlist_symbols,
                    token_provider=shared_token,
                    quotes_provider=deps.quotes_fetch,
                    quote_store=deps.quote_store, clock=deps.clock,
                    now=observed_now_utc, wire_recorder=recorder)
                _stage("quotes", quotes_result.status, quotes_result.reason)
            except PulseCeilingError:
                _stage("quotes", "failed", "failed_ceiling")
            except Exception:
                _stage("quotes", "failed", "failed_exception")

        # 2) returns (§16) — pure derivation, wire zero. Read canonical
        # holdings and latest-value rows rather than claiming availability
        # solely from quote-stage status.
        if quotes_result is None or quotes_result.status not in (
                "saved", "partial"):
            _stage("returns", "skipped", "no_quote_projection_input")
        else:
            try:
                projection_holdings = deps.holdings_reader()
                if projection_holdings is None:
                    _stage("returns", "skipped", "holdings_unavailable")
                else:
                    symbols = tuple(sorted({
                        item.symbol
                        for account in projection_holdings.accounts
                        for item in account.holdings
                    }))
                    latest = (
                        () if not symbols else deps.quote_store.read_latest(
                            source=QUOTE_SOURCE, symbols=symbols)
                    )
                    projection = project_returns(
                        holdings=projection_holdings,
                        quotes={row.symbol: row for row in latest},
                        computed_at=observed_now_utc,
                    )
                    _stage(
                        "returns", "derived",
                        f"positions:{len(projection.positions)}",
                    )
            except Exception as error:
                reason = (
                    "STORE_BUSY"
                    if getattr(error, "reason", None) == "store_locked"
                    else "projection_failed"
                )
                _stage("returns", "failed", reason)

        # 3) execution ingestion (§17) — mask-free.
        execution_result = None
        if not deps.execution_provider_enabled:
            _stage("execution", "disabled", "owner_not_granted")
        else:
            try:
                execution_result = run_pulse_execution(
                    tick=tick, holdings_reader=deps.holdings_reader,
                    native_refs_reader=deps.native_refs_reader,
                    token_provider=shared_token,
                    orders_page_fetch=deps.orders_page_fetch,
                    order_detail_fetch=deps.order_detail_fetch,
                    ledger_store=deps.ledger_store,
                    scan_state_store=deps.scan_state_store,
                    clock=deps.clock, now=observed_now_utc,
                    wire_recorder=recorder)
                _stage("execution", execution_result.status,
                       execution_result.reason)
            except PulseCeilingError:
                _stage("execution", "failed", "failed_ceiling")
            except Exception:
                _stage("execution", "failed", "failed_exception")

        # 3b) conditional-order fill detection (conditional SDS v0.4 §4) —
        # fast-alert lane only, ledger write zero; runs before dialogue so a
        # same-tick regular arrival merges against the fresh alert. Fails
        # independently: no other lane ever waits on it.
        if not deps.conditional_provider_enabled:
            _stage("conditional", "disabled", "owner_not_granted")
        else:
            try:
                conditional_result = run_conditional_detection(
                    tick=tick, holdings_reader=deps.holdings_reader,
                    native_refs_reader=deps.native_refs_reader,
                    token_provider=shared_token,
                    conditional_orders_fetch=deps.conditional_orders_fetch,
                    dialogue_store=deps.dialogue_store,
                    clock=deps.clock, now=observed_now_utc,
                    wire_recorder=recorder,
                    playbook_root=deps.playbook_root)
                _stage("conditional", conditional_result.status,
                       conditional_result.reason)
            except PulseCeilingError:
                _stage("conditional", "failed", "failed_ceiling")
            except Exception:
                _stage("conditional", "failed", "failed_exception")

        if deps.news_producer_deps is not None:
            try:
                production = _run_news_producer_stage(
                    deps.news_producer_deps, observed_now_utc)
                _stage("news_producer", *production_stage_report(production))
            except Exception:
                _stage("news_producer", "failed", "failed_exception")
        if deps.news_outbox_reconcile is not None:
            try:
                deps.news_outbox_reconcile(observed_now_utc)
                _stage("news_outbox", "complete", None)
            except Exception:
                _stage("news_outbox", "failed", "failed_exception")
        if deps.news_delivery_runner is not None:
            try:
                deps.news_delivery_runner(observed_now_utc)
                _stage("news_delivery", "complete", None)
            except Exception:
                _stage("news_delivery", "failed", "failed_exception")
        if deps.quant_market_close_runner is not None:
            try:
                deps.quant_market_close_runner(observed_now_utc)
                _stage("quant_market_close", "complete", None)
            except Exception:
                _stage("quant_market_close", "failed", "failed_exception")

        # 4) dialogue (§19–§22) — G3-off default, send zero.
        new_events = () if execution_result is None else \
            execution_result.new_events
        try:
            dialogue_result = run_execution_dialogue(
                new_events=new_events, dialogue_store=deps.dialogue_store,
                quote_reader=deps.dialogue_quote_reader,
                playbook_root=deps.playbook_root,
                ai_evaluator=deps.ai_evaluator,
                sender=deps.telegram_sender,
                activation_enabled=deps.dialogue_activation_enabled,
                send_eligible_after=deps.dialogue_cutoff,
                clock=deps.clock, now=observed_now_utc)
            if dialogue_result.status == "send_unavailable":
                # Gate enabled but no sender (env missing): register and
                # enqueue proceeded, rows stay PENDING; only the send step
                # failed, surfaced as this stage's durable health signal.
                _stage("dialogue", "failed", "send_unavailable")
            else:
                _stage("dialogue", dialogue_result.status,
                       f"enqueued:{dialogue_result.enqueued}")
        except Exception:
            _stage("dialogue", "failed", "failed_exception")

        # 5) inbound (§22.3) — disabled before G3.
        try:
            if deps.updates_provider is None:
                _stage("inbound", "skipped", "no_updates_provider")
            else:
                inbound_result = consume_inbound(
                    dialogue_store=deps.dialogue_store,
                    updates_provider=deps.updates_provider,
                    playbook_root=deps.playbook_root,
                    playbook_validator=deps.playbook_validator,
                    sender=deps.telegram_sender,
                    callback_ack=deps.callback_ack,
                    news_callback_handler=deps.news_callback_handler,
                    activation_enabled=deps.dialogue_activation_enabled,
                    send_eligible_after=deps.dialogue_cutoff,
                    clock=deps.clock, now=observed_now_utc)
                _stage("inbound", inbound_result.status,
                       f"appends:{inbound_result.appends}; failures:{inbound_result.append_failures}; "
                       + ",".join(inbound_result.failure_reasons))
        except Exception:
            _stage("inbound", "failed", "failed_exception")

        # Existing quote/trade/news/inbound work gets priority. Optional
        # reads may use only the remaining time and the already issued token.
        optional_blocked = False
        if deps.quant_input_capture is not None and token_state['value'] is not None:
            try:
                from datetime import timedelta
                capture=deps.quant_input_capture(existing_token=token_state['value'],
                    now=deps.clock(),deadline=scheduled_for_utc+timedelta(seconds=35),
                    wire_recorder=recorder)
                count=recorder.counts['QUANT_READ']
                optional_blocked = any(c.get('status') in ('http_429','auth_rejection','forbidden')
                    for c in capture.get('calls',[]))
                _stage('quant_inputs','skipped' if capture.get('failures') or capture.get('skipped') else 'complete',
                       ('capture_unavailable;' if capture.get('failures') else '')+f'additional_reads:{count}')
            except Exception:
                # Normal results survive capture failure; no new token is
                # requested. Capture keeps its own non-secret attempt record.
                optional_blocked = True
                _stage('quant_inputs','skipped','capture_exception')

        # Explicit optional product reads use actual remaining allowance, inside the
        # core7 + Quant4 + PAPER6 and rolling85. Same bearer and deadline.
        if deps.opportunity_capture is not None:
            try:
                from datetime import timedelta
                deadline = scheduled_for_utc+timedelta(seconds=35)
                from tokmo.facts.toss_reads import REQUEST_TIMEOUT_SECONDS
                remaining = min(PULSE_OPPORTUNITY_INPUT_STARTS, PULSE_ACTIVE_WIRE_STARTS-sum(recorder.counts.values()),
                    ROLLING_WIRE_CEILING-rolling-sum(recorder.counts.values()))
                if not optional_blocked and getattr(deps.opportunity_capture,'due',lambda now:True)(deps.clock()) and remaining > (1 if token_state['value'] is None else 0) and (deadline-deps.clock()).total_seconds() >= REQUEST_TIMEOUT_SECONDS+3:
                    token = shared_token()
                    remaining = min(PULSE_OPPORTUNITY_INPUT_STARTS, PULSE_ACTIVE_WIRE_STARTS-sum(recorder.counts.values()),
                        ROLLING_WIRE_CEILING-rolling-sum(recorder.counts.values()))
                    result = deps.opportunity_capture(existing_token=token,now=deps.clock(),
                        deadline=deadline,wire_recorder=recorder,remaining_budget=remaining)
                    _stage('opportunity_inputs',result['status'],f"public_reads:{result['calls']}")
                else:
                    _stage('opportunity_inputs','skipped','no_spare_budget_or_time')
            except Exception:
                _stage('opportunity_inputs','skipped','capture_unavailable')

        for seq in recorder.started:
            receipts.settle_wire(policy_version, tick.tick_id, seq)
        wire_counts = receipts.wire_counts(policy_version, tick.tick_id)
        failed = [item for item in stages if item["status"] == "failed"]
        active = [item for item in stages
                  if item["status"] not in ("disabled", "skipped")]
        if failed:
            status = "FAILED" if len(failed) == len(active) else "PARTIAL"
            reason = failed[0]["reason"]
        elif quotes_result is not None and quotes_result.status == "skipped" \
                and execution_result is None:
            status, reason = "SKIPPED_CLOSED", quotes_result.reason
        else:
            status, reason = "SAVED", None
        receipts.finish_tick(
            policy_version, scheduled_for_utc, status=status, reason=reason,
            stages=stages, wire_counts=wire_counts,
            finished_at=deps.clock())
        rolling_after = receipts.rolling_wire_starts(
            policy_version, now_epoch=epoch + 60,
            window_seconds=ROLLING_WINDOW_SECONDS)
        exit_code = 0 if status in ("SAVED", "SKIPPED_CLOSED") else 2
        return _result(status, reason, stages, wire_counts, rolling_after,
                       exit_code)
    finally:
        try:
            deps.tick_cleanup()
        finally:
            pass
        os.close(descriptor)
