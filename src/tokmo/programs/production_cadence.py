"""One production-shaped cadence tick in read-only live shadow (Stage 3).

``run_production_cadence_once`` executes the fixed one-tick state machine:

    acquire no-overlap lease -> canonical ``CadenceTickV1`` -> independent
    KR/US calendar refresh through admitted wires -> one immutable
    ``OpenMarketMaskV1`` -> truthful skip when empty -> account-wide holdings
    once -> full snapshot persisted -> open-market projection -> mask-only
    quotes -> account trade pages with uniquely classified open details ->
    analysis barrier -> candidate/material preparation -> typed lifecycle
    sink -> durable terminal receipt.

One tick/mask identity flows end to end; no stage reinterprets session or
date. Every provider wire runs through the mandatory
:class:`~tokmo.programs.cadence_wire.CadenceWireExecutorV1` (calendar keeps
its kernel retry/claim authority), so unplanned, duplicate, or over-limit
requests perform callback zero. A required-fact failure blocks downstream; a
single market's calendar failure closes only that market; a truthful skip,
partial, or failure is always recorded — never rewritten as success.

``live_shadow`` is the only enabled mode: read-only providers, no order
writes, no model or alert transport, and the production canonical data root
is refused until a separate explicit activation contract exists.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
import fcntl
import hashlib
import os
from pathlib import Path
import re
import secrets
from types import MappingProxyType
from typing import Callable, Mapping

from tokmo.facts.toss import TossCampaignOutcome
from tokmo.programs.analysis_barrier import assemble_analysis_materials
from tokmo.programs.cadence_wire import CadenceWireExecutorV1, LazyWireTokenV1
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1, OperationLane, active_owner,
)
from tokmo.programs.calendar_lookahead_writer import (
    LookaheadWriterError,
    write_lookahead_from_calendar,
)
from tokmo.programs.calendar_refresh import (
    MarketRefreshResult,
    MarketRefreshState,
    refresh_markets,
)
from tokmo.programs.account_native_ref_writer import (
    NativeRefWriterError,
    write_native_refs_from_accounts,
)
from tokmo.programs.holdings_sync import run_holdings_sync
from tokmo.programs.pension_materialization import (
    evaluate_reminder, materialize_pension_holdings,
)
from tokmo.programs.pension_reminder_delivery import run_pension_reminder_delivery
from tokmo.programs.quotes_sync import run_quotes_sync
from tokmo.programs.market_regime_shadow import run_market_regime_shadow
from tokmo.programs.quant_market_profiles import (
    run_profiles_membership,
    run_profiles_shadow,
)
from tokmo.programs.quant_recurring_shadow import (
    evaluate_quant_due,
    run_quant_membership_acquisition,
    run_quant_recurring_shadow,
)
from tokmo.programs.trades_sync import run_trades_sync
from tokmo.programs.execution_detection import LEDGER_SOURCE
from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore
from tokmo.records.calendar_cache import (
    CalendarCacheCorruptError,
    CalendarCacheStore,
)
from tokmo.records.calendar_lookahead import (
    CalendarLookaheadConflictError,
    CalendarLookaheadCorruptError,
    CalendarLookaheadStoreV1,
)
from tokmo.records.account_native_refs import AccountNativeRefStoreV1, NativeRefCorruptError
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.consume_watermark import ConsumeWatermarkStoreV1
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
from tokmo.records.pension_revisions import PensionRegistryStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.records.regular_session_history import (
    RegularSessionHistoryProvenanceError,
    RegularSessionHistoryStore,
)
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.cadence_policy import (
    CADENCE_POLICY_REGISTRY,
    CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    TradesChildLimitsV1,
    cadence_tick_v1,
)
from tokmo.values.quant_recurring import (
    QuantDueDecisionV1,
    QuantTickCalendarInputV1,
)
from tokmo.values.calendar_refresh import CalendarFetchResult, market_local_date
from tokmo.values.material_pack import JobKind
from tokmo.values.open_market_mask import (
    MaskBindingError,
    build_open_market_mask,
    require_tick_mask_binding,
)
from tokmo.values.ops_kernel import OpsKernelBudgetExceeded, OpsKernelError, ProducerRole
from tokmo.values.production_cadence import (
    AnalysisExecutionV1,
    CADENCE_MODE_LIVE_SHADOW, CADENCE_MODE_PRODUCTION,
    CADENCE_STAGE_ORDER,
    CadenceLifecycleEventV1,
    CadenceOutcome,
    OUTCOME_EXIT_CODES,
    StageStatusV1,
    null_lifecycle_sink,
)
from tokmo.values.session_gate import (
    Market,
    SessionVerdict,
    judge_sessions,
    regular_session_for,
)
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketDay,
    UsMarketDay,
)


class ProductionCadenceRefusal(ValueError):
    """Configuration/preflight refusal before any provider work (exit 1)."""


# The production canonical data root stays refused in live shadow until a
# separate explicit activation contract exists (Stage 3 C).
def _production_canonical_root() -> Path:
    return (Path.home() / ".local" / "share" / "tokmo-v3").resolve()  # may be a symlink into the project (2026-09-24)


_TRUTHFUL_CLOSED_REASONS = frozenset({"no_regular", "before_open", "after_close"})
_DETAIL_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,63}")

# Calendar refresh states whose wire attempt carried a truthful provider
# response (the cache outcome is separate evidence, SDS §7 table).  Used
# for kernel settlement: CACHE_CONFLICT / CACHE_UPDATE_FAILED mean the wire
# ran but the cache commit failed — the kernel still settles SUCCEEDED.
_CALENDAR_WIRE_SETTLED_SUCCEEDED = frozenset({
    MarketRefreshState.SUCCESS,
    MarketRefreshState.CACHE_CONFLICT,
    MarketRefreshState.CACHE_UPDATE_FAILED,
})


@dataclass(frozen=True)
class ProductionCadenceDepsV1:
    """Typed read-only production cadence dependencies (Stage 3 C).

    Only read-only per-wire provider callables, the trades session-date seam,
    the injected clock, and a typed local lifecycle sink. No order-write API
    is importable or representable here, and ``live_shadow`` is the only
    enabled mode.
    """

    mode: str
    clock: Callable[[], datetime]
    token_fetch: Callable
    calendar_fetch: Callable
    accounts_fetch: Callable
    account_holdings_fetch: Callable
    quotes_fetch: Callable
    orders_page_fetch: Callable
    order_detail_fetch: Callable
    stock_candles_fetch: Callable
    regular_session_dates: object
    lifecycle_sink: Callable = field(default=null_lifecycle_sink, repr=False)
    watchlist_symbols: Mapping = field(default_factory=dict, repr=False)
    analysis_execution: AnalysisExecutionV1 | None = field(default=None, repr=False)
    pension_registry_path: Path | None = field(default=None, repr=False)
    pension_composition_enabled: bool = field(default=False, repr=False)
    pension_reminder_send_enabled: bool = field(default=False, repr=False)
    pension_unquotable_symbols: frozenset[str] = field(default_factory=frozenset, repr=False)
    membership_fetch: Callable | None = field(default=None, repr=False)
    sec_user_agent: Callable | None = field(default=None, repr=False)
    sic_snapshot_loader: Callable | None = field(default=None, repr=False)
    stocks_membership_fetch: Callable | None = field(default=None, repr=False)


    def __post_init__(self):
        if self.mode not in (CADENCE_MODE_LIVE_SHADOW, CADENCE_MODE_PRODUCTION):
            raise ProductionCadenceRefusal(
                "live_shadow is the only enabled cadence mode"
            )
        providers = (
            self.clock, self.token_fetch, self.calendar_fetch,
            self.accounts_fetch, self.account_holdings_fetch,
            self.quotes_fetch, self.orders_page_fetch,
            self.order_detail_fetch, self.lifecycle_sink,
            self.stock_candles_fetch,
        )
        if not all(callable(item) for item in providers):
            raise ProductionCadenceRefusal(
                "cadence deps require read-only provider callables"
            )
        if any(
            item is not None and not callable(item)
            for item in (
                self.membership_fetch,
                self.sec_user_agent,
                self.sic_snapshot_loader,
                self.stocks_membership_fetch,
            )
        ):
            raise ProductionCadenceRefusal("invalid quant recurring dependency")
        if self.analysis_execution is not None and not isinstance(
            self.analysis_execution, AnalysisExecutionV1,
        ):
            raise ProductionCadenceRefusal(
                "analysis execution must be typed fixture facade"
            )
        if not all(
            isinstance(market, Market)
            and isinstance(symbols, frozenset)
            and all(isinstance(symbol, str) and symbol for symbol in symbols)
            for market, symbols in self.watchlist_symbols.items()
        ):
            raise ProductionCadenceRefusal("invalid watchlist symbols")


@dataclass(frozen=True)
class ProductionCadenceResultV1:
    outcome: CadenceOutcome
    reason: str | None
    exit_code: int
    policy_version: str
    scheduled_for_utc: datetime
    tick_id: int
    lag_seconds: float
    mask_digest: str | None
    open_markets: tuple[str, ...]
    local_dates: Mapping[str, str]
    calendar: Mapping[str, str]
    stages: tuple[StageStatusV1, ...]
    wire: Mapping


class _StageLog:
    """Ordered per-stage status recorder plus typed lifecycle emission."""

    def __init__(self, deps, policy_version: str, tick_id: int):
        self._deps = deps
        self._policy_version = policy_version
        self._tick_id = tick_id
        self._statuses: dict[str, StageStatusV1] = {}

    def emit(self, kind: str, *, stage=None, status=None, reason=None) -> None:
        try:
            self._deps.lifecycle_sink(CadenceLifecycleEventV1(
                kind, self._policy_version, self._tick_id, self._deps.clock(),
                stage=stage, status=status, reason=reason,
            ))
        except Exception:
            pass  # a sink failure may never mutate or fail the tick

    def record(self, stage: str, status: str, reason: str | None = None,
               started_at: datetime | None = None) -> None:
        latency = 0
        if started_at is not None:
            latency = max(
                0, int((self._deps.clock() - started_at).total_seconds() * 1000)
            )
        self._statuses[stage] = StageStatusV1(stage, status, reason, latency)
        self.emit("stage_finished", stage=stage, status=status, reason=reason)

    def statuses(self) -> tuple[StageStatusV1, ...]:
        return tuple(
            self._statuses.get(stage, StageStatusV1(stage, "not_reached"))
            for stage in CADENCE_STAGE_ORDER
        )

    def status_of(self, stage: str) -> str:
        recorded = self._statuses.get(stage)
        return "not_reached" if recorded is None else recorded.status


def _existing_quant_receipt_store(path: Path):
    if not path.is_file():
        return None
    return QuantRecurringReceiptStore(path)


def _prepare_quant_receipts_for_due(*, path, existing, context, now, policy):
    if context.decision is not QuantDueDecisionV1.DUE:
        return existing
    store = existing if existing is not None else QuantRecurringReceiptStore(path)
    store.reconcile_stale(now=now, policy=policy)
    return store


def _quant_tick_calendar_input(*, results, verdicts, mask, cache_store):
    us_result = results.get(Market.US)
    us_verdict = next(
        (verdict for verdict in verdicts if verdict.market is Market.US),
        None,
    )
    if us_result is None or us_verdict is None:
        raise ValueError("quant_calendar_input_missing")
    try:
        cache = cache_store.read()
        entry = None if cache is None else cache.us
        cache_trade_date = (
            None if entry is None else date.fromisoformat(entry.trade_date)
        )
    except (CalendarCacheCorruptError, OSError, ValueError):
        cache_trade_date = None
    return QuantTickCalendarInputV1(
        us_refresh_state=us_result.state.value,
        us_refresh_succeeded=(
            us_result.state is MarketRefreshState.SUCCESS
        ),
        us_verdict_open=us_verdict.open,
        us_verdict_reason=us_verdict.reason,
        us_in_effective_mask=(
            mask is not None and Market.US in mask.markets
        ),
        mask_bound=(mask is not None),
        us_cache_trade_date=cache_trade_date,
    )


def _acquire_lease(root: Path):
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(runtime, 0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(runtime / "cadence.lock", flags, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(descriptor)
        return None
    return descriptor


def _empty_wire_counts() -> dict:
    return {
        "total_started": 0, "group_started": {}, "operation_started": {},
        "state_counts": {},
    }


def _wire_counts(kernel: OpsKernelStore, policy_version: str,
                 slot: datetime) -> dict:
    report = kernel.read_cadence_tick_report(policy_version, slot)
    if report is None:
        return _empty_wire_counts()
    return {
        "total_started": report["total_started"],
        "group_started": dict(report["group_started"]),
        "operation_started": dict(report["operation_started"]),
        "state_counts": dict(report["state_counts"]),
    }


def _day_from_cache_entry(market: Market, entry):
    def restored(kind):
        value = entry.sessions.get(kind)
        if value is None:
            return None
        return CalendarSession(
            datetime.fromisoformat(value.start.replace("Z", "+00:00")),
            datetime.fromisoformat(value.end.replace("Z", "+00:00")),
        )
    trade_date = date.fromisoformat(entry.trade_date)
    if market is Market.KR:
        return KrMarketDay(trade_date, IntegratedHour(
            pre_market=restored("preMarket"),
            regular_market=restored("regularMarket"),
            after_market=restored("afterMarket"),
        ))
    return UsMarketDay(
        trade_date, day_market=restored("dayMarket"),
        pre_market=restored("preMarket"),
        regular_market=restored("regularMarket"),
        after_market=restored("afterMarket"),
    )


_FAIL_CLOSED_REASON = {
    MarketRefreshState.PROVIDER_FAILED: "stale_cache",
    MarketRefreshState.INVALID_IDENTITY: "stale_cache",
    MarketRefreshState.INVALID_DATE: "stale_cache",
    MarketRefreshState.INVALID_BOUNDARY: "stale_cache",
    MarketRefreshState.CACHE_CONFLICT: "stale_cache",
    MarketRefreshState.CACHE_UPDATE_FAILED: "cache_corrupt",
}


def _write_native_refs_after_holdings(
    *, root, accounts, policy_version, tick_id, clock,
):
    """G2.5: persist eligible Toss account native refs after a COMPLETE
    holdings stage. Pension/manual scopes are excluded by the writer.
    The receipt_binding is the holdings wire receipt identity.

    A writer failure is evidence-only — caught by the caller, never
    changes the cadence outcome. No secret/path exposure.
    """
    ref_store = AccountNativeRefStoreV1(
        root / "ops" / "account_native_refs.sqlite3", clock=clock,
    )
    receipt_binding = f"{policy_version}:{tick_id}"
    write_native_refs_from_accounts(
        accounts=accounts,
        receipt_binding=receipt_binding,
        store=ref_store,
    )


def _write_lookahead_after_refresh(
    *, root, results, receipts, responses, cache_store, deps, log=None,
):
    """G2.5: after a successful calendar refresh, write the provider's exact
    today + next_business_day into CalendarLookaheadStoreV1.

    Uses the calendar receipt's native_identity as provenance. Only markets
    whose wire result is MarketRefreshState.SUCCESS AND whose full response
    was captured by the fetcher are written. CACHE_CONFLICT /
    CACHE_UPDATE_FAILED are NOT sufficient — the cache entry may be stale or
    absent and the provider response must not be silently dropped into a
    conflicting store. No extra API call — the data comes from the
    already-completed response.

    A LookaheadWriterError or CalendarLookaheadConflictError is caught per
    market and recorded as bounded stage evidence via ``log`` (m1): the
    reason is sanitized to the exception class name only (no secret/path/
    raw ref). An unexpected (non-typed) exception is NOT swallowed — it
    propagates to the caller, which decides whether to record or re-raise.
    """
    try:
        cache = cache_store.read()
    except CalendarCacheCorruptError:
        return

    lookahead_store = CalendarLookaheadStoreV1(
        root / "ops" / "calendar_lookahead.sqlite3", clock=deps.clock,
    )
    fetched_at = deps.clock()

    for market in Market:
        if market not in receipts or market not in responses:
            continue
        result = results.get(market)
        # M1: gate on SUCCESS ONLY — not _CALENDAR_WIRE_SETTLED_SUCCEEDED
        # which includes CACHE_CONFLICT / CACHE_UPDATE_FAILED.
        if result is None or result.state is not MarketRefreshState.SUCCESS:
            continue
        receipt = receipts[market]
        response = responses[market]
        cache_entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        if cache_entry is None:
            continue

        # m2: validate response shape and access next_business_day inside the
        # per-market branch so a legacy replay/day wrapper or malformed market
        # cannot suppress another market's lookahead write.
        if not (hasattr(response, "today")
                and hasattr(response, "next_business_day")):
            if log is not None:
                log.emit(
                    "stage_finished", stage="calendar", status="ok",
                    reason="lookahead_skipped_invalid_response",
                )
            continue
        try:
            write_lookahead_from_calendar(
                market=market,
                response_today=response.today,
                response_next=response.next_business_day,
                receipt_identity=receipt.native_identity,
                store=lookahead_store,
                current_cache_entry=cache_entry,
                fetched_at=fetched_at,
            )
        except (
            LookaheadWriterError,
            CalendarLookaheadConflictError,
            CalendarLookaheadCorruptError,
            ValueError,
        ) as error:
            # m1: typed writer/store exceptions are evidence-only.
            # Record a sanitized bounded reason (class name only — no
            # secret/path/raw ref) if a log sink is available.
            if log is not None:
                log.emit(
                    "stage_finished", stage="calendar",
                    status="ok",
                    reason=f"lookahead_skipped_{type(error).__name__}",
                )
            continue


def _refresh_calendar(*, root, kernel, cache_store, deps, policy, tick, executor,
                      token, log):
    """Independent per-market refresh through admitted wires (SDS §4, §8).

    Returns ``(results_by_market, receipts_by_market)``. Shared-cache
    corruption that prevents trustworthy market separation performs zero
    fetches and closes both markets.
    """
    slot = tick.scheduled_for_utc
    local_dates = {
        market: market_local_date(market, slot) for market in Market
    }
    try:
        cache = cache_store.read()
    except CalendarCacheCorruptError:
        results = {
            market: MarketRefreshResult(
                market, local_dates[market],
                MarketRefreshState.CACHE_UPDATE_FAILED, False,
            ) for market in Market
        }
        return results, {}

    receipts = {}
    for market in Market:
        entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        if entry is not None and entry.trade_date == local_dates[market].isoformat():
            continue  # current-date entry is reused; no wire, no token
        try:
            kernel.plan_calendar_slot(
                ProducerRole.COLLECTOR, market, local_dates[market], slot,
                now=deps.clock(),
            )
            receipts[market] = kernel.start_calendar_attempt(
                ProducerRole.COLLECTOR, policy, scheduled_for_utc=slot,
                market=market, market_local_date=local_dates[market],
                eligible_slot_utc=slot, now=deps.clock(),
            )
        except OpsKernelError:
            # Backoff/exhausted/duplicate admission: this market stays
            # fail-closed for the tick with wire zero; the other market is
            # still attempted independently.
            continue

    # G2.5: capture full CalendarResponse objects for the lookahead writer.
    # These are the exact provider responses — no extra API call.
    _captured_responses: dict = {}

    def fetcher(*, market, local_trade_date, receipt):
        del receipt
        response = deps.calendar_fetch(
            market=market, local_trade_date=local_trade_date,
            receipt=receipts[market], bearer=token.bearer(),
        )
        # G2.5: calendar_fetch now returns the full CalendarResponse to
        # preserve next_business_day. Capture it for the lookahead writer.
        _captured_responses[market] = response
        return response

    results = {
        result.market: result for result in refresh_markets(
            store=cache_store, fetchers={market: fetcher for market in Market},
            now=slot, receipts=receipts, kernel=kernel,
        )
    }
    for market, receipt in receipts.items():
        outcome = (
            "SUCCEEDED"
            if results[market].state in _CALENDAR_WIRE_SETTLED_SUCCEEDED
            else "FAILED"
        )
        kernel.settle_calendar_attempt(
            ProducerRole.COLLECTOR, market, receipt.market_local_date,
            receipt.attempt_ordinal, outcome, now=deps.clock(),
        )

    # G2.5: on successful calendar refresh, preserve the receipt's exact
    # calendar response (today + next_business_day) in CalendarLookaheadStoreV1
    # through the writer seam. A writer failure is evidence-only — it must
    # not change the tick outcome. No extra API call or recalculation.
    # m1: catch only typed writer/store exceptions; unexpected programmer
    # errors are NOT silently swallowed but propagated so the caller can
    # record truthful stage evidence.
    if root is not None:
        try:
            _write_lookahead_after_refresh(
                root=root, results=results, receipts=receipts,
                responses=_captured_responses,
                cache_store=cache_store, deps=deps, log=log,
            )
        except (
            LookaheadWriterError,
            CalendarLookaheadConflictError,
            CalendarLookaheadCorruptError,
            ValueError,
        ) as error:
            log.emit(
                "stage_finished", stage="calendar", status="ok",
                reason=f"lookahead_skipped_{type(error).__name__}",
            )

    return results, receipts


def _history_capacity(kernel, policy, slot):
    """Remaining pinned CALENDAR-group and total capacity for this tick."""
    report = kernel.read_cadence_tick_report(policy.policy_version, slot)
    limits = report["snapshot"]["request_limits"]
    return (
        limits["calendar_starts"] - report["group_started"].get("CALENDAR", 0),
        limits["total_wire_starts"] - report["total_started"],
    )


def _sync_session_history(*, root, kernel, cache_store, deps, policy, tick,
                          token, log):
    """Accumulate the durable official regular-session history (M2).

    First the current-date fact of each market is appended from its already
    admitted and settled calendar wire (a cache entry without durable kernel
    provenance is never promoted — do not forge). Then, strictly within the
    tick's remaining pinned calendar/total capacity, at most one missing
    historical local date per market is fetched through the SAME kernel
    calendar plan/start/claim/settle authority and appended before the
    attempt settles SUCCEEDED. Failures keep the existing six-attempt
    identity/backoff ledger and never advance the walk.
    """
    started = deps.clock()
    slot = tick.scheduled_for_utc
    history = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3", clock=deps.clock,
    )
    try:
        cache = cache_store.read()
    except CalendarCacheCorruptError:
        # untrustworthy market separation: no append, no history wire
        log.record("history", "skipped", "cache_corrupt", started)
        return

    for market in Market:
        local = market_local_date(market, slot)
        entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        if entry is None or entry.trade_date != local.isoformat():
            continue
        if history.read(market, local) is not None:
            continue
        session = "regular" if "regularMarket" in entry.sessions else "no_regular"
        try:
            history.append_settled(market, local, session, authority=kernel)
        except RegularSessionHistoryProvenanceError:
            continue  # no validated official admission behind this entry

    deferred = False
    for market in Market:
        calendar_remaining, total_remaining = _history_capacity(
            kernel, policy, slot,
        )
        token_need = 0 if token.acquired else 1
        if calendar_remaining < 1 or total_remaining < 1 + token_need:
            break
        anchor = market_local_date(market, slot)
        target = history.next_backfill_target(market, anchor)
        if target is None:
            continue
        try:
            kernel.plan_calendar_slot(
                ProducerRole.COLLECTOR, market, target, slot, now=deps.clock(),
            )
            receipt = kernel.start_calendar_attempt(
                ProducerRole.COLLECTOR, policy, scheduled_for_utc=slot,
                market=market, market_local_date=target,
                eligible_slot_utc=slot, now=deps.clock(),
            )
        except OpsKernelError:
            # backoff active, six attempts consumed, or a closed identity:
            # this market's backfill performs wire zero this tick.
            deferred = True
            continue
        try:
            kernel.claim_calendar_provider(receipt, now=deps.clock())
            day = deps.calendar_fetch(
                market=market, local_trade_date=target, receipt=receipt,
                bearer=token.bearer(),
            )
            # G2.5: calendar_fetch now returns the full CalendarResponse.
            if hasattr(day, "today") and hasattr(day, "next_business_day"):
                day = day.today
            fetch = CalendarFetchResult(market, target, day, receipt)
            session = (
                "regular"
                if regular_session_for(fetch.day, market, slot) is not None
                else "no_regular"
            )
            # the durable fact lands before the attempt settles, so a crash
            # between the two never loses an already-paid official answer
            history.append_backfilled(
                market, target, session, receipt=receipt, authority=kernel,
            )
        except Exception:
            deferred = True
            kernel.settle_calendar_attempt(
                ProducerRole.COLLECTOR, market, target,
                receipt.attempt_ordinal, "FAILED", now=deps.clock(),
            )
            continue
        kernel.settle_calendar_attempt(
            ProducerRole.COLLECTOR, market, target, receipt.attempt_ordinal,
            "SUCCEEDED", now=deps.clock(),
        )
    log.record(
        "history", "ok", "backfill_deferred" if deferred else None, started,
    )


def _judge_verdicts(cache_store, results, slot):
    """Session verdicts from the refreshed cache; failed markets fail closed."""
    try:
        cache = cache_store.read()
    except CalendarCacheCorruptError:
        cache = None
    days = {}
    for market in Market:
        entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        if (results[market].state is MarketRefreshState.SUCCESS
                and entry is not None):
            days[market] = _day_from_cache_entry(market, entry)
        else:
            days[market] = None
    judged = {
        verdict.market: verdict
        for verdict in judge_sessions(days[Market.KR], days[Market.US], slot)
    }
    verdicts = []
    for market in Market:
        if results[market].state is MarketRefreshState.SUCCESS:
            verdicts.append(judged[market])
        else:
            verdicts.append(SessionVerdict(
                market, False, _FAIL_CLOSED_REASON[results[market].state], None,
            ))
    return tuple(verdicts)


def _detail_native_identity(order_id: str) -> str:
    text = str(order_id)
    if _DETAIL_IDENTITY.fullmatch(text):
        return f"detail:{text}"
    digest = hashlib.sha256(text.encode()).hexdigest()[:32]
    return f"detail:{digest}"


def _run_open_stages(*, root, tick, mask, deps, executor, token, kernel, log,
                     pension_revision=None, pension_revision_failed=False):
    """Masked downstream stages; the exact tick/mask binding is re-proven at
    this boundary before any provider I/O (E7: mismatch => downstream zero)."""
    require_tick_mask_binding(mask, tick)

    clock = deps.clock
    holdings_store = CurrentHoldingsStore(
        root / "stores" / "current_holdings.json", clock=clock,
    )
    quotes_store = LatestQuoteStore(
        root / "stores" / "latest_quotes.sqlite3", clock=clock,
    )
    trades_store = TradeLedgerStore(
        root / "stores" / "trade_ledger.sqlite3", clock=clock,
        id_factory=lambda: secrets.token_hex(16),
    )
    scan_state = OrderScanStateStore(
        root / "stores" / "order_scan_state.sqlite3", clock=clock,
    )
    packs = MaterialPackWriter(root / "ops", clock=clock)
    receipts = {
        job: ReceiptStore(root / "ops" / "receipts" / f"{job.value}.json",
                          clock=clock)
        for job in JobKind
    }
    locks = root / "ops" / "locks"
    accounts_holder: dict = {}
    # M6: pension_revision is now pinned at Step 0 (pre-calendar) and passed in.
    if pension_revision_failed:
        log.record("holdings", "failed", "failed_pension_source")
        for stage in ("snapshot", "projection", "quotes", "trades",
                      "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
            log.record(stage, "blocked", "holdings_required_fact_failed")
        return

    def holdings_provider():
        accounts = executor.execute(
            "ACCOUNTS", "accounts:tick0",
            lambda claim: deps.accounts_fetch(claim=claim, bearer=token.bearer()),
        )
        count = len(accounts.identities)
        if count > 5:
            return TossCampaignOutcome(
                "account_count_unexpected", "accounts", 0, (), (),
            )
        facts = []
        for ordinal, identity in enumerate(accounts.identities, start=1):
            facts.append(executor.execute(
                "ACCOUNT_HOLDINGS", f"holdings:{ordinal}",
                lambda claim, identity=identity, ordinal=ordinal:
                    deps.account_holdings_fetch(
                        claim=claim, bearer=token.bearer(), identity=identity,
                        native_ref=accounts.native_refs[identity.account_scope],
                        ordinal=ordinal, count=count,
                    ),
            ))
        accounts_holder["accounts"] = accounts
        return TossCampaignOutcome(
            "success", "holdings", count, tuple(facts), (),
            account_identities=accounts.identities,
            _identity_fingerprints=dict(accounts.fingerprints),
        )

    started = clock()
    try:
        holdings_result = run_holdings_sync(
            tick=tick, mask=mask, holdings_provider=holdings_provider,
            store=holdings_store, pack_writer=packs,
            receipt_store=receipts[JobKind.HOLDINGS],
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=clock, now=tick.scheduled_for_utc,
            job_lock_path=locks / "holdings.lock",
            pension_revision=pension_revision,
        )
    except Exception as error:
        # M1: a collector/store exception is a truthful holdings failure,
        # never a stranded STARTED tick.
        log.record("holdings", "failed", f"exception_{type(error).__name__}",
                   started)
        for stage in ("snapshot", "projection", "quotes", "trades",
                      "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
            log.record(stage, "blocked", "holdings_required_fact_failed")
        return
    if holdings_result.status == "saved":
        log.record("holdings", "ok", None, started)
        log.record("snapshot", "ok")
        log.record(
            "projection",
            "ok" if holdings_result.projection is not None else "failed",
        )
        # G2.5: on successful holdings sync, persist eligible Toss account
        # native refs through the native ref writer seam. A writer failure
        # is evidence-only — it must not change the tick outcome.
        # m1: catch only typed writer/store exceptions; unexpected programmer
        # errors are NOT silently swallowed.
        if accounts_holder.get("accounts") is not None:
            try:
                _write_native_refs_after_holdings(
                    root=root, accounts=accounts_holder["accounts"],
                    policy_version=tick.policy_version,
                    tick_id=tick.tick_id, clock=clock,
                )
            except (NativeRefWriterError, NativeRefCorruptError, ValueError) as error:
                log.emit(
                    "stage_finished", stage="holdings", status="ok",
                    reason=f"native_refs_skipped_{type(error).__name__}",
                )
    else:
        log.record("holdings", "failed", holdings_result.reason, started)
        for stage in ("snapshot", "projection", "quotes", "trades",
                      "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
            log.record(stage, "blocked", "holdings_required_fact_failed")
        return

    canonical = holdings_store.read()
    epoch = int(tick.scheduled_for_utc.timestamp())
    quote_stage_mode = (
        "provider" if active_owner(
            OPERATION_LANE_REGISTRY_V1.entries["PRICE_BATCH"], epoch=epoch)
        is OperationLane.CADENCE_300S else "consumer")
    trade_stage_mode = (
        "provider" if active_owner(
            OPERATION_LANE_REGISTRY_V1.entries["ORDERS_PAGE"], epoch=epoch)
        is OperationLane.CADENCE_300S else "consumer")

    quote_ordinals = {"n": 0}

    def quotes_provider(symbols):
        batch = tuple(symbols)
        ordinal = quote_ordinals["n"]
        quote_ordinals["n"] += 1
        return executor.execute(
            "PRICE_BATCH", f"quotes:{ordinal}",
            lambda claim: deps.quotes_fetch(
                claim=claim, bearer=token.bearer(), symbols=batch,
            ),
        )

    started = clock()
    try:
        quotes_result = run_quotes_sync(
            tick=tick, mask=mask, holdings=canonical,
            quotes_provider=quotes_provider,
            quote_store=lambda outcome: quotes_store.save_batch(outcome.facts),
            pack_writer=packs, receipt_store=receipts[JobKind.QUOTES],
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            clock=clock, now=tick.scheduled_for_utc,
            job_lock_path=locks / "quotes.lock",
            watchlist_symbols=deps.watchlist_symbols,
            pension_unquotable_symbols=deps.pension_unquotable_symbols,
            stage_mode=quote_stage_mode,
            latest_consumer_reader=lambda market, symbols: (
                quotes_store.read_latest_with_stored_at(
                    source="toss-current-price-v1", symbols=tuple(symbols))),
        )
    except Exception as error:
        # M1: quotes is not a required fact for trades; the exception is a
        # truthful quotes failure and the tick continues.
        log.record("quotes", "failed", f"exception_{type(error).__name__}",
                   started)
    else:
        if quotes_result.status == "saved":
            log.record("quotes", "ok", None, started)
        elif quotes_result.status == "skipped":
            log.record("quotes", "skipped", quotes_result.reason, started)
        else:
            log.record("quotes", "failed", quotes_result.reason, started)

    page_ordinals = {"n": 0}

    def orders_provider(*, account_scope, status, from_date, to_date, cursor,
                        limit):
        ordinal = page_ordinals["n"]
        if ordinal > 9:
            raise OpsKernelBudgetExceeded("orders page identities exhausted")
        page_ordinals["n"] += 1
        refs = accounts_holder["accounts"].native_refs
        return executor.execute(
            "ORDERS_PAGE", f"orders:{ordinal}",
            lambda claim: deps.orders_page_fetch(
                claim=claim, bearer=token.bearer(),
                account_scope=account_scope,
                native_ref=refs.get(account_scope), status=status,
                from_date=from_date, to_date=to_date, cursor=cursor,
                limit=limit,
            ),
        )

    def detail_provider(*, account_scope, order_id):
        refs = accounts_holder["accounts"].native_refs
        return executor.execute(
            "ORDER_DETAIL", _detail_native_identity(order_id),
            lambda claim: deps.order_detail_fetch(
                claim=claim, bearer=token.bearer(),
                account_scope=account_scope,
                native_ref=refs.get(account_scope), order_id=order_id,
            ),
        )

    started = clock()
    try:
        watermark = None
        scopes = ()
        starts_by_scope = {}
        consumer_from = tick.scheduled_for_utc
        if trade_stage_mode == "consumer":
            watermark = ConsumeWatermarkStoreV1(
                root / "ops" / "consume_watermark.sqlite3", clock=clock)
            scopes = tuple(sorted(
                account.account_scope for account in canonical.accounts
                if account.account_scope.startswith("toss-")
            )) if canonical is not None else ()
            cutover_epoch = OPERATION_LANE_REGISTRY_V1.entries[
                "ORDERS_PAGE"].cutover_epoch_utc
            cutover_at = datetime.fromtimestamp(cutover_epoch, timezone.utc)
            for scope in scopes:
                if watermark.read(source=LEDGER_SOURCE, account_scope=scope) is None:
                    watermark.bootstrap(
                        source=LEDGER_SOURCE, account_scope=scope,
                        cutover_epoch_utc=cutover_at)
            starts_by_scope = {
                scope: watermark.read(
                    source=LEDGER_SOURCE,
                    account_scope=scope).last_consumed_stored_at
                for scope in scopes
            }
            consumer_from = (min(starts_by_scope.values())
                             if starts_by_scope else cutover_at)

        def audit_reader():
            result = []
            for scope in scopes:
                for row in trades_store.read_stored_window(
                        source=LEDGER_SOURCE, account_scope=scope,
                        from_stored_at=starts_by_scope[scope],
                        to_stored_at=tick.scheduled_for_utc):
                    result.append({
                        "source": row.event.source,
                        "account_scope": row.event.account_scope,
                        "source_record_key": row.event.source_record_key,
                        "stored_at": row.stored_at.isoformat(),
                    })
            return tuple(result)

        def advance_all(last, advanced, tick_id):
            if watermark is None:
                return
            for scope in scopes:
                watermark.advance(
                    source=LEDGER_SOURCE, account_scope=scope,
                    last_consumed_stored_at=last,
                    advanced_at=advanced, tick_id=tick_id)

        trades_result = run_trades_sync(
            tick=tick, mask=mask, holdings=canonical,
            orders_provider=orders_provider, detail_provider=detail_provider,
            store=trades_store, scan_state=scan_state, pack_writer=packs,
            receipt_store=receipts[JobKind.TRADES],
            reservation_seal=lambda *args: None,
            incident_writer=lambda *args: None,
            regular_session_dates=deps.regular_session_dates, clock=clock,
            now=tick.scheduled_for_utc,
            job_lock_path=locks / "trades.lock",
            ops_kernel=None, invocation_id=None,
            stage_mode=trade_stage_mode,
            consumer_audit_reader=audit_reader,
            consumer_window_from=consumer_from,
            watermark_advance=advance_all,
        )
    except Exception as error:
        log.record("trades", "failed", f"exception_{type(error).__name__}",
                   started)
    else:
        if trades_result.status in ("saved", "partial"):
            # A truthful partial (quarantine/market-pending/pagination) is the
            # contract outcome for unclassifiable records, not a stage failure.
            log.record(
                "trades", "ok",
                None if trades_result.status == "saved" else trades_result.reason,
                started,
            )
        elif trades_result.status == "skipped":
            log.record("trades", "skipped", trades_result.reason, started)
        else:
            log.record("trades", "failed", trades_result.reason, started)

    started = clock()
    try:
        barrier = assemble_analysis_materials(
            tick=tick, mask=mask,
            receipt_readers={job: receipts[job].read for job in JobKind},
            pack_reader=lambda path, digest: packs.read_and_verify(
                Path(path), digest,
            ),
            now=tick.scheduled_for_utc,
        )
    except Exception as error:
        log.record("barrier", "failed", f"exception_{type(error).__name__}",
                   started)
        for stage in ("deltas", "admission", "analysis_shadow", "candidates"):
            log.record(stage, "blocked", "barrier_required_fact_failed")
        return
    log.record("barrier", "ok", None, started)

    started = clock()
    if deps.analysis_execution is None:
        log.record("deltas", "skipped", "analysis_disabled", started)
        log.record("admission", "skipped", "analysis_disabled")
        log.record("analysis_shadow", "skipped", "analysis_disabled")
        log.record("candidates", "skipped", "request_path_disabled")
        return
    try:
        outcome = deps.analysis_execution(
            tick=tick, barrier=barrier, store=kernel,
            now=tick.scheduled_for_utc,
        )
    except Exception as error:
        reason = f"exception_{type(error).__name__}"
        log.record("deltas", "failed", reason, started)
        log.record("admission", "blocked", reason)
        log.record("analysis_shadow", "blocked", reason)
        log.record("candidates", "skipped", "request_path_only")
    else:
        log.record("deltas", "ok", f"count_{outcome.delta_count}", started)
        log.record("admission", "ok", f"admitted_{outcome.admitted_count}")
        analysis_status = (
            "ok" if outcome.failed == 0 and outcome.unknown == 0 else "failed"
        )
        log.record(
            "analysis_shadow", analysis_status,
            f"succeeded_{outcome.succeeded}_failed_{outcome.failed}_unknown_{outcome.unknown}",
        )
        log.record("candidates", "skipped", "request_path_only")


def _confine_runtime_root(root: Path, mode: str) -> Path:
    resolved = Path(root).resolve()
    production = _production_canonical_root().resolve()
    if mode == CADENCE_MODE_PRODUCTION:
        if resolved != production:
            raise ProductionCadenceRefusal(
                "production mode requires canonical root"
            )
        return resolved
    if (resolved == production or production in resolved.parents
            or resolved in production.parents):
        raise ProductionCadenceRefusal(
            "production canonical root refused without an activation contract"
        )
    return resolved


def run_production_cadence_once(
    *, root: Path, policy_version: str, scheduled_for_utc: datetime,
    observed_now_utc: datetime, deps: ProductionCadenceDepsV1,
    replay: bool = False,
) -> ProductionCadenceResultV1:
    """Execute exactly one aligned versioned cadence tick in live shadow."""
    if not isinstance(deps, ProductionCadenceDepsV1):
        raise ProductionCadenceRefusal("typed production cadence deps required")
    policy = CADENCE_POLICY_REGISTRY.get(policy_version)
    if policy is None:
        raise ProductionCadenceRefusal("unknown cadence policy version")
    try:
        tick = cadence_tick_v1(policy_version, scheduled_for_utc)
    except ValueError as error:
        raise ProductionCadenceRefusal(
            "scheduled slot is not on the policy cadence grid"
        ) from error
    if (not isinstance(observed_now_utc, datetime)
            or observed_now_utc.tzinfo is None
            or observed_now_utc.utcoffset() is None):
        raise ProductionCadenceRefusal("observed now must be aware UTC")
    root = _confine_runtime_root(root, deps.mode)
    if deps.pension_registry_path is not None:
        registry_resolved = deps.pension_registry_path.resolve()
        root_resolved = root.resolve()
        # M8: refuse if the pension_registry_path is outside the cadence root.
        if registry_resolved != root_resolved and root_resolved not in registry_resolved.parents:
            raise ProductionCadenceRefusal(
                "pension_registry_path must be under the cadence root"
            )
        production_root = _production_canonical_root().resolve()
        if (deps.mode != CADENCE_MODE_PRODUCTION
                and (registry_resolved == production_root
                     or production_root in registry_resolved.parents)):
            raise ProductionCadenceRefusal(
                "pension_registry_path must not point into production canonical root "
                "in non-production mode"
            )
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(root, 0o700)

    lag_seconds = max(
        0.0, (observed_now_utc - scheduled_for_utc).total_seconds()
    )
    receipts = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3", clock=deps.clock,
    )
    log = _StageLog(deps, policy_version, tick.tick_id)
    local_dates_iso = {
        market.value: market_local_date(market, scheduled_for_utc).isoformat()
        for market in Market
    }

    def provider0(outcome: CadenceOutcome, reason: str) -> ProductionCadenceResultV1:
        log.emit("tick_finished", status=outcome.value, reason=reason)
        return ProductionCadenceResultV1(
            outcome, reason, OUTCOME_EXIT_CODES[outcome], policy_version,
            scheduled_for_utc, tick.tick_id, lag_seconds, None, (),
            MappingProxyType(dict(local_dates_iso)), MappingProxyType({}),
            log.statuses(), MappingProxyType(_empty_wire_counts()),
        )

    lease = _acquire_lease(root)
    if lease is None:
        receipts.record_event(policy_version, scheduled_for_utc, "OVERLAP")
        log.record("lease", "failed", "lock_contention")
        return provider0(CadenceOutcome.OVERLAP, "lock_contention")
    try:
        log.record("lease", "ok")
        kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        # Reconcile every stale STARTED request/receipt from a crashed prior
        # invocation before any new provider start (SDS §9).
        kernel.reconcile_cadence(ProducerRole.COLLECTOR, now=observed_now_utc)
        quant_receipts_path = root / "ops" / "quant_recurring.sqlite3"
        quant_receipts = None
        quant_receipts_loaded = False

        def load_quant_receipts():
            nonlocal quant_receipts, quant_receipts_loaded
            if not quant_receipts_loaded:
                quant_receipts = _existing_quant_receipt_store(
                    quant_receipts_path
                )
                quant_receipts_loaded = True
            return quant_receipts

        receipts.reconcile_stale(
            now=observed_now_utc, exclude=(policy_version, scheduled_for_utc),
        )

        if receipts.read_tick(policy_version, scheduled_for_utc) is not None:
            receipts.record_event(policy_version, scheduled_for_utc, "DUPLICATE")
            log.record("tick", "failed", "duplicate_slot")
            return provider0(CadenceOutcome.DUPLICATE, "duplicate_slot")
        if lag_seconds > policy.max_start_lag_seconds and not replay:
            receipts.record_missed(
                policy_version, scheduled_for_utc, tick.tick_id,
                observed_start=observed_now_utc, lag_seconds=lag_seconds,
                max_lag_seconds=policy.max_start_lag_seconds,
            )
            log.record("tick", "failed", "excess_start_lag")
            return provider0(CadenceOutcome.MISSED, "excess_start_lag")

        receipts.begin_tick(
            policy_version, scheduled_for_utc, tick.tick_id,
            observed_start=observed_now_utc, lag_seconds=lag_seconds,
            max_lag_seconds=policy.max_start_lag_seconds,
        )
        log.emit("tick_started")
        child_limits = TradesChildLimitsV1()
        kernel.pin_cadence_tick(
            ProducerRole.COLLECTOR, policy, scheduled_for_utc=scheduled_for_utc,
            now=deps.clock(), child_limits=child_limits,
        )
        log.record("tick", "ok")

        # M6: Step 0 pre-calendar — pin the PENSION registry revision BEFORE
        # calendar refresh so a missing/corrupt registry produces evidence on
        # every tick (closed-market included), not just open ticks.
        pension_revision_pinned = None
        pension_revision_failed = False
        if deps.pension_composition_enabled:
            try:
                registry_path = (
                    deps.pension_registry_path
                    if deps.pension_registry_path is not None
                    else root / "stores" / "pension_registry.sqlite3"
                )
                pension_revision_pinned = PensionRegistryStore(
                    registry_path, clock=deps.clock,
                ).read_active_revision()
                if pension_revision_pinned is None:
                    pension_revision_failed = True
                    # M5: publish a real ReceiptV1(FAILED, "failed_pension_source").
                    from tokmo.records.receipt_store import ReceiptStore as _RS
                    from tokmo.values.receipt_v1 import (
                        AttemptStatus as _AS, AttemptV1, ReceiptV1,
                    )
                    _holdings_receipt_store = _RS(
                        root / "ops" / "receipts" / "holdings.json",
                        clock=deps.clock,
                    )
                    _attempt = AttemptV1(
                        tick.tick_id, _AS.FAILED, "failed_pension_source",
                        deps.clock(),
                    )
                    _holdings_receipt_store.replace(
                        ReceiptV1(1, JobKind.HOLDINGS, _attempt, None, {}, None)
                    )
                else:
                    # Validate the pinned ACTIVE revision without opening
                    # any provider wire.
                    materialize_pension_holdings(
                        pension_revision_pinned, pension_ordinal=1)
            except Exception as error:
                # M5: record stage failure and publish a terminal receipt.
                log.record(
                    "holdings", "failed",
                    f"exception_{type(error).__name__}",
                )
                pension_revision_failed = True
                # M5: publish ReceiptV1(FAILED, "failed_pension_source").
                from tokmo.records.receipt_store import ReceiptStore as _RS2
                from tokmo.values.receipt_v1 import (
                    AttemptStatus as _AS2, AttemptV1 as _A2, ReceiptV1 as _R2,
                )
                _hrs2 = _RS2(
                    root / "ops" / "receipts" / "holdings.json",
                    clock=deps.clock,
                )
                _hrs2.replace(_R2(
                    1, JobKind.HOLDINGS,
                    _A2(tick.tick_id, _AS2.FAILED, "failed_pension_source",
                        deps.clock()),
                    None, {}, None,
                ))

        # N7 Step 0a/0b/0c: reconcile, naturally evaluate, then deliver.
        # M5: wrap in a typed handler that records stage failure and
        # publishes a terminal receipt instead of escaping.
        try:
            kernel.promote_stale_attempting(ProducerRole.HERMES, observed_now_utc)
            if deps.pension_composition_enabled:
                registry_path = (
                    deps.pension_registry_path
                    if deps.pension_registry_path is not None
                    else root / "stores" / "pension_registry.sqlite3"
                )
                registry = PensionRegistryStore(registry_path, clock=deps.clock)
                evaluate_reminder(
                    registry, kernel, tick_now=scheduled_for_utc,
                    tick_id=tick.tick_id,
                    production_mode=deps.mode == CADENCE_MODE_PRODUCTION,
                    replay=replay, send_enabled=deps.pension_reminder_send_enabled,
                    role=ProducerRole.COLLECTOR,
                )
                telegram_send = getattr(deps, "telegram_send", None)
                if (deps.mode == CADENCE_MODE_PRODUCTION and not replay
                        and deps.pension_reminder_send_enabled):
                    if not callable(telegram_send):
                        raise ProductionCadenceRefusal("telegram sender absent")
                    run_pension_reminder_delivery(
                        ops_kernel=kernel, role=ProducerRole.HERMES,
                        telegram_send=telegram_send, now=observed_now_utc,
                    )
        except ProductionCadenceRefusal:
            raise
        except Exception as error:
            # M5: a failure in Step 0a/0b/0c records a stage failure
            # instead of escaping and bricking the cadence.
            log.record(
                "pension_reminder", "failed",
                f"exception_{type(error).__name__}",
            )

        executor = CadenceWireExecutorV1(
            ops_kernel=kernel, role=ProducerRole.COLLECTOR, policy=policy,
            scheduled_for_utc=scheduled_for_utc, clock=deps.clock,
            child_limits=child_limits,
            lane_registry=OPERATION_LANE_REGISTRY_V1,
            lane=OperationLane.CADENCE_300S,
        )
        token = LazyWireTokenV1(executor=executor, token_fetch=deps.token_fetch)
        cache_store = CalendarCacheStore(
            root / "ops" / "calendar_cache.json", clock=deps.clock,
        )

        started = deps.clock()
        results, calendar_receipts = _refresh_calendar(
            root=root, kernel=kernel, cache_store=cache_store, deps=deps,
            policy=policy, tick=tick, executor=executor, token=token, log=log,
        )
        calendar_states = {
            market.value: results[market].state.value for market in Market
        }
        calendar_ok = all(
            results[market].state is MarketRefreshState.SUCCESS
            for market in Market
        )
        log.record(
            "calendar", "ok" if calendar_ok else "failed",
            None if calendar_ok else "market_refresh_failed", started,
        )

        try:
            _sync_session_history(
                root=root, kernel=kernel, cache_store=cache_store, deps=deps,
                policy=policy, tick=tick, token=token, log=log,
            )
        except Exception as error:
            # History accumulation is incremental background evidence; an
            # unexpected exception is recorded truthfully and never blocks
            # the tick's session judgment or strands the receipt STARTED.
            log.record(
                "history", "failed", f"exception_{type(error).__name__}",
            )

        # Default V1 remains wire-closed. This stage is deliberately outside
        # the open-market mask so closed sessions can reuse a completed result.
        if policy.request_limits.daily_candle_starts <= 0:
            log.record("market_regime_shadow", "skipped", "policy_not_pinned")
        else:
            started = deps.clock()
            try:
                status, reason = run_market_regime_shadow(
                    root=root, deps=deps, policy=policy, tick=tick,
                    executor=executor, token=token, cache_store=cache_store,
                    invocation_id=secrets.token_hex(16),
                )
                log.record("market_regime_shadow", status, reason, started)
            except Exception as error:
                log.record(
                    "market_regime_shadow", "failed",
                    f"exception_{type(error).__name__}", started,
                )

        verdicts = _judge_verdicts(cache_store, results, scheduled_for_utc)
        mask_error = None
        started = deps.clock()
        try:
            mask = build_open_market_mask(
                policy_version=policy_version,
                scheduled_for_utc=scheduled_for_utc, tick_id=tick.tick_id,
                verdicts=verdicts,
                local_dates={
                    market: market_local_date(market, scheduled_for_utc)
                    for market in Market
                },
            )
            require_tick_mask_binding(mask, tick)
        except (MaskBindingError, ValueError):
            mask = None
            mask_error = "mask_binding"
        if mask is None:
            log.record("mask", "failed", mask_error, started)
        else:
            log.record("mask", "ok", None, started)

        membership = None
        profiles_execution = None
        # KR/US market-profiles dispatch is keyed on the exact reviewed
        # policy version (Plan/SDS 2026-08-22 §6); every other policy keeps
        # the legacy V1 US lane path byte-identical below.
        quant_profiles_policy = (
            policy.policy_version
            == CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1.policy_version
        )
        quant_skip_reason = "policy_not_pinned"
        if policy.request_limits.membership_starts <= 0:
            log.record("quant_membership", "skipped", quant_skip_reason)
        elif quant_profiles_policy:
            started = deps.clock()
            try:
                profiles_execution = run_profiles_membership(
                    root=root,
                    deps=deps,
                    policy=policy,
                    tick=tick,
                    executor=executor,
                    token=token,
                    results=results,
                    mask=mask,
                    cache_store=cache_store,
                    now=observed_now_utc,
                )
                quant_skip_reason = profiles_execution.reason
                log.record(
                    "quant_membership",
                    profiles_execution.status,
                    profiles_execution.reason,
                    started,
                )
            except Exception as error:
                quant_skip_reason = f"exception_{type(error).__name__}"
                log.record(
                    "quant_membership", "failed", quant_skip_reason, started
                )
        else:
            started = deps.clock()
            try:
                quant_calendar = _quant_tick_calendar_input(
                    results=results,
                    verdicts=verdicts,
                    mask=mask,
                    cache_store=cache_store,
                )
            except ValueError:
                quant_skip_reason = (
                    QuantDueDecisionV1.CALENDAR_BINDING_CONFLICT.value
                )
                log.record(
                    "quant_membership", "skipped", quant_skip_reason, started
                )
            else:
                try:
                    context = evaluate_quant_due(
                        root=root,
                        deps=deps,
                        policy=policy,
                        tick=tick,
                        calendar=quant_calendar,
                        cache_store=cache_store,
                        receipts=load_quant_receipts,
                    )
                    if context.decision is not QuantDueDecisionV1.DUE:
                        quant_skip_reason = context.decision.value.lower()
                        log.record(
                            "quant_membership",
                            "skipped",
                            quant_skip_reason,
                            started,
                        )
                    else:
                        quant_receipts = _prepare_quant_receipts_for_due(
                            path=quant_receipts_path,
                            existing=load_quant_receipts(),
                            context=context,
                            now=observed_now_utc,
                            policy=policy,
                        )
                        membership = run_quant_membership_acquisition(
                            root=root,
                            deps=deps,
                            policy=policy,
                            tick=tick,
                            executor=executor,
                            context=context,
                            receipts=quant_receipts,
                        )
                        quant_skip_reason = membership.reason
                        log.record(
                            "quant_membership",
                            membership.status,
                            membership.reason,
                            started,
                        )
                except Exception as error:
                    quant_skip_reason = f"exception_{type(error).__name__}"
                    log.record(
                        "quant_membership", "failed", quant_skip_reason, started
                    )
        if mask is None:
            for stage in ("holdings", "snapshot", "projection", "quotes",
                          "trades", "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
                log.record(stage, "blocked", mask_error)
        else:
            if not mask.markets:
                truthful = calendar_ok and all(
                    entry.reason in _TRUTHFUL_CLOSED_REASONS
                    for entry in mask.entries.values()
                )
                skip_reason = (
                    "both_markets_closed" if truthful else "market_fail_closed"
                )
                for stage in ("holdings", "snapshot", "projection", "quotes",
                              "trades", "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
                    log.record(
                        stage, "skipped" if truthful else "blocked", skip_reason,
                    )
            else:
                try:
                    _run_open_stages(
                        root=root, tick=tick, mask=mask, deps=deps,
                        executor=executor, token=token, kernel=kernel, log=log,
                        pension_revision=pension_revision_pinned,
                        pension_revision_failed=pension_revision_failed,
                    )
                except Exception as error:
                    # M1: any unexpected open-stage escape terminates the tick
                    # truthfully — the first unreached contract stage failed
                    # with the exception type, everything after it never ran.
                    reason = f"exception_{type(error).__name__}"
                    contract_stages = ("holdings", "quotes", "trades",
                                       "barrier", "deltas", "admission", "analysis_shadow", "candidates")
                    failed_stage = next(
                        (stage for stage in contract_stages
                         if log.status_of(stage) == "not_reached"), None,
                    )
                    if failed_stage is not None:
                        log.record(failed_stage, "failed", reason)
                    for stage in ("holdings", "snapshot", "projection",
                                  "quotes", "trades", "barrier", "deltas", "admission", "analysis_shadow", "candidates"):
                        if log.status_of(stage) == "not_reached":
                            log.record(stage, "blocked", reason)

        if policy.request_limits.membership_starts <= 0:
            log.record("quant_shadow", "skipped", "policy_not_pinned")
        elif quant_profiles_policy:
            if profiles_execution is None or profiles_execution.status != "ok":
                log.record("quant_shadow", "skipped", quant_skip_reason)
            else:
                started = deps.clock()
                try:
                    status, reason = run_profiles_shadow(
                        root=root,
                        deps=deps,
                        policy=policy,
                        tick=tick,
                        executor=executor,
                        token=token,
                        execution=profiles_execution,
                    )
                    log.record("quant_shadow", status, reason, started)
                except Exception as error:
                    log.record(
                        "quant_shadow", "failed",
                        f"exception_{type(error).__name__}", started,
                    )
        elif membership is None or membership.status != "ok":
            log.record("quant_shadow", "skipped", quant_skip_reason)
        else:
            started = deps.clock()
            try:
                status, reason = run_quant_recurring_shadow(
                    root=root,
                    deps=deps,
                    policy=policy,
                    tick=tick,
                    executor=executor,
                    token=token,
                    membership=membership,
                    receipts=quant_receipts,
                )
                log.record("quant_shadow", status, reason, started)
            except Exception as error:
                log.record(
                    "quant_shadow", "failed",
                    f"exception_{type(error).__name__}", started,
                )

        log.record("sink", "ok")
        outcome, reason = _terminal_outcome(
            mask, mask_error, calendar_ok, log,
            quant_profile_stages=quant_profiles_policy,
        )
        wire = _wire_counts(kernel, policy_version, scheduled_for_utc)
        finished_at = deps.clock()
        receipts.finish_tick(
            policy_version, scheduled_for_utc, status=outcome.value,
            reason=reason,
            mask_digest=None if mask is None else mask.digest,
            open_markets=() if mask is None else tuple(sorted(
                market.value for market in mask.markets
            )),
            local_dates=local_dates_iso,
            stages=[{
                "stage": status.stage, "status": status.status,
                "reason": status.reason, "latency_ms": status.latency_ms,
            } for status in log.statuses()],
            wire_counts=wire,
            calendar_attempts=tuple(
                receipt.native_identity
                for receipt in calendar_receipts.values()
            ),
            finished_at=finished_at,
        )
        log.emit("tick_finished", status=outcome.value, reason=reason)
        return ProductionCadenceResultV1(
            outcome, reason, OUTCOME_EXIT_CODES[outcome], policy_version,
            scheduled_for_utc, tick.tick_id, lag_seconds,
            None if mask is None else mask.digest,
            () if mask is None else tuple(sorted(
                market.value for market in mask.markets
            )),
            MappingProxyType(dict(local_dates_iso)),
            MappingProxyType(dict(calendar_states)),
            log.statuses(), MappingProxyType(wire),
        )
    finally:
        fcntl.flock(lease, fcntl.LOCK_UN)
        os.close(lease)


def _terminal_outcome(mask, mask_error, calendar_ok, log, *,
                      quant_profile_stages=False):
    """Truthful terminal outcome for the fixed state machine (SDS §8)."""
    if mask_error is not None:
        return CadenceOutcome.FAILED, mask_error
    if log.status_of("holdings") == "failed":
        return CadenceOutcome.FAILED, "holdings_required_fact_failed"
    if quant_profile_stages:
        # Profiles policy only (Plan/SDS 2026-08-22 §8): a failed executed
        # profile makes the tick truthfully PARTIAL. Legacy policies keep
        # their existing terminal semantics byte-identically.
        quant_failed = [
            stage for stage in ("quant_membership", "quant_shadow")
            if log.status_of(stage) == "failed"
        ]
        if quant_failed:
            return CadenceOutcome.PARTIAL, f"stage_failed_{quant_failed[0]}"
    if not mask.markets:
        if calendar_ok and all(
                entry.reason in _TRUTHFUL_CLOSED_REASONS
                for entry in mask.entries.values()):
            return CadenceOutcome.SKIPPED_CLOSED, "both_markets_closed"
        return CadenceOutcome.FAILED, "market_fail_closed"
    failed_stages = [
        stage for stage in ("quotes", "trades", "barrier", "deltas", "admission", "analysis_shadow", "candidates")
        if log.status_of(stage) == "failed"
    ]
    if failed_stages or not calendar_ok:
        return CadenceOutcome.PARTIAL, (
            f"stage_failed_{failed_stages[0]}" if failed_stages
            else "market_refresh_failed"
        )
    return CadenceOutcome.COMPLETE, None
