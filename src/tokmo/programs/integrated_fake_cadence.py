"""External-I/O-free, restartable one-day cadence composition for Slice F fixtures."""

import fcntl
import os
import secrets
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from tokmo.programs.analysis_pipeline import run_analysis_pipeline
from tokmo.programs.legacy_g2 import (
    legacy_assemble_analysis_materials,
    legacy_run_holdings_sync,
    legacy_run_quotes_sync,
    legacy_run_trades_sync,
)
from tokmo.programs.session_gate import run_session_gate
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.records.material_pack_store import MaterialPackWriter
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.calendar_refresh import market_local_date
from tokmo.values.material_pack import JobKind
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.portfolio_scope import resolve_account_scope_pattern
from tokmo.values.session_gate import Market


class IntegratedCadenceLockedError(RuntimeError):
    """Another integrated runner owns this root's day lock."""


@dataclass(frozen=True)
class RequestGateV1:
    """Factory binding that exposes the cadence-owned request authority."""

    ops_kernel: OpsKernelStore


@dataclass(frozen=True)
class IntegratedCadenceDepsV1:
    calendar_provider: Callable
    holdings_provider_factory: Callable
    quotes_provider_factory: Callable
    orders_provider_factory: Callable
    detail_provider_factory: Callable
    glm_call: Callable
    sol_call: Callable
    regular_session_dates: object
    clock: Callable[[], datetime]
    # Optional masked per-tick numeric-evidence hook (§10.9): the analysis
    # stage forwards it to ``run_analysis_pipeline`` so a shadow runner can
    # aggregate subject-tick numeric state without knowing the gate logic.
    numeric_evidence_sink: Callable | None = None


@dataclass(frozen=True)
class _IntegratedStageDepsV1:
    calendar_provider: Callable
    session_gate: Callable
    holdings_runner: Callable
    quotes_runner: Callable
    trades_runner: Callable
    barrier_runner: Callable
    analysis_runner: Callable
    clock: Callable[[], datetime]


@dataclass(frozen=True)
class IntegratedCadenceReportV1:
    utc_day: str
    tick_count: int
    logical_slots: int
    planned_request_count: int
    started_request_count: int
    settled_request_count: int
    unknown_request_count: int
    per_operation: Mapping[str, int]
    emergency_started_count: int
    calendar_started_count: int
    socket_count: int
    dns_count: int


class IntegratedCadenceCountersV1:
    """Run-local measured counters exposed to fixture stages."""

    def __init__(self):
        self.logical_slots = 0
        self.socket_count = 0
        self.dns_count = 0

    def add_logical_slots(self, count: int) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("invalid logical slot count")
        self.logical_slots += count

    def add_socket_count(self, count: int = 1) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("invalid socket count")
        self.socket_count += count

    def add_dns_count(self, count: int = 1) -> None:
        if type(count) is not int or count < 0:
            raise ValueError("invalid dns count")
        self.dns_count += count


def _production_root() -> Path:
    return Path.home() / ".local" / "share" / "tokmo-v3"


def _confined_fake_root(root: Path) -> Path:
    candidate = Path(root).resolve()
    production = _production_root().resolve()
    if (
        candidate == production
        or candidate in production.parents
        or production in candidate.parents
    ):
        raise ValueError("production root denied")
    return candidate


def _request_report(
    store: OpsKernelStore, ticks
) -> tuple[int, int, int, int, dict[str, int]]:
    rows = tuple(
        row for tick in ticks for row in store.read_planned_requests(tick.tick_id)
    )
    operations: dict[str, int] = {}
    for row in rows:
        if row["started_at"] is not None:
            operations[row["operation"]] = operations.get(row["operation"], 0) + 1
    return (
        len(rows),
        sum(row["started_at"] is not None for row in rows),
        sum(row["state"] == "SETTLED" for row in rows),
        sum(row["state"] == "UNKNOWN" for row in rows),
        operations,
    )


def _acquire_day_lock(root: Path) -> int:
    ops_dir = root / "ops"
    ops_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(ops_dir, 0o700)
    lock_dir = ops_dir / "locks"
    lock_dir.mkdir(mode=0o700, exist_ok=True)
    os.chmod(lock_dir, 0o700)
    path = lock_dir / "integrated.lock"
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError as error:
        os.close(descriptor)
        raise IntegratedCadenceLockedError("integrated cadence locked") from error
    return descriptor


def _open_progress(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS completed_ticks("
        "utc_day TEXT NOT NULL, tick_index INTEGER NOT NULL CHECK(tick_index BETWEEN 0 AND 287),"
        "tick_id INTEGER NOT NULL, PRIMARY KEY(utc_day,tick_index)) STRICT"
    )
    return connection


def _run_integrated_fake_day_range(
    *,
    utc_day: date,
    root: Path,
    deps: _IntegratedStageDepsV1,
    compressed: bool = True,
    start_tick: int = 0,
    end_tick: int = 288,
) -> IntegratedCadenceReportV1:
    """Private bounded restart/test seam for ``[start_tick, end_tick)``."""
    if type(utc_day) is not date or not isinstance(deps, _IntegratedStageDepsV1):
        raise ValueError("invalid integrated cadence input")
    if compressed is not True:
        raise ValueError("fake cadence must be compressed")
    if (
        type(start_tick) is not int
        or type(end_tick) is not int
        or not 0 <= start_tick <= end_tick <= 288
    ):
        raise ValueError("invalid integrated cadence range")

    confined = _confined_fake_root(root)
    confined.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock_descriptor = _acquire_day_lock(confined)
    progress = None
    counters = IntegratedCadenceCountersV1()
    executed_ticks = []
    try:
        store = OpsKernelStore(confined / "ops" / "ops_kernel.sqlite3")
        progress = _open_progress(confined / "ops" / "integrated_cadence.sqlite3")
        completed = {
            row[0]
            for row in progress.execute(
                "SELECT tick_index FROM completed_ticks WHERE utc_day=?",
                (utc_day.isoformat(),),
            )
        }
        store.reconcile_requests(
            ProducerRole.COLLECTOR,
            invocation_id=secrets.token_hex(16),
            now=deps.clock(),
        )

        start = datetime.combine(utc_day, datetime.min.time(), tzinfo=UTC)
        stages = (
            deps.calendar_provider,
            deps.session_gate,
            deps.holdings_runner,
            deps.quotes_runner,
            deps.trades_runner,
            deps.barrier_runner,
            deps.analysis_runner,
        )
        for index in range(start_tick, end_tick):
            if index in completed:
                continue
            tick = tick_id_from_utc(start + timedelta(minutes=5 * index))
            context = None
            for stage in stages:
                value = stage(
                    tick=tick,
                    root=confined,
                    ops_kernel=store,
                    prior=context,
                    counters=counters,
                )
                if value is not None:
                    context = value
            progress.execute(
                "INSERT INTO completed_ticks(utc_day,tick_index,tick_id) VALUES(?,?,?)",
                (utc_day.isoformat(), index, tick.tick_id),
            )
            progress.commit()
            executed_ticks.append(tick)

        planned, started, settled, unknown, operations = _request_report(
            store, executed_ticks
        )
        calendar_started = sum(
            count
            for operation, count in operations.items()
            if operation.startswith("CALENDAR_DAY_")
        )
        return IntegratedCadenceReportV1(
            utc_day.isoformat(),
            len(executed_ticks),
            counters.logical_slots,
            planned,
            started,
            settled,
            unknown,
            MappingProxyType(dict(sorted(operations.items()))),
            started - calendar_started,
            calendar_started,
            counters.socket_count,
            counters.dns_count,
        )
    finally:
        if progress is not None:
            progress.close()
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _production_stage_deps(
    root: Path, deps: IntegratedCadenceDepsV1
) -> _IntegratedStageDepsV1:
    """Construct the existing A-E production stores and their seven stage runners."""
    clock = deps.clock
    calendar_cache = CalendarCacheStore(
        root / "ops" / "calendar_cache.json", clock=clock
    )
    holdings_store = CurrentHoldingsStore(
        root / "stores" / "current_holdings.json", clock=clock
    )
    quotes_store = LatestQuoteStore(
        root / "stores" / "latest_quotes.sqlite3", clock=clock
    )
    trades_store = TradeLedgerStore(
        root / "stores" / "trade_ledger.sqlite3",
        clock=clock,
        id_factory=lambda: secrets.token_hex(16),
    )
    scan_state = OrderScanStateStore(
        root / "stores" / "order_scan_state.sqlite3", clock=clock
    )
    packs = MaterialPackWriter(root / "ops", clock=clock)
    receipts = {
        job: ReceiptStore(root / "ops" / "receipts" / f"{job.value}.json", clock=clock)
        for job in JobKind
    }
    gate_holder = {}

    def calendar(*, tick, ops_kernel, **_):
        gate = gate_holder.setdefault("gate", RequestGateV1(ops_kernel))
        if "providers" not in gate_holder:
            gate_holder["providers"] = {
                "holdings": deps.holdings_provider_factory(gate),
                "quotes": deps.quotes_provider_factory(gate),
                "orders": deps.orders_provider_factory(gate),
                "detail": deps.detail_provider_factory(gate),
            }
        now = tick.start_utc
        providers = {
            market: lambda market=market: deps.calendar_provider(
                market=market,
                local_trade_date=market_local_date(market, now),
                tick=tick,
                request_gate=gate,
            )
            for market in Market
        }
        return providers

    def session(*, tick, prior, counters, **_):
        result = run_session_gate(
            kr_calendar_provider=prior[Market.KR],
            us_calendar_provider=prior[Market.US],
            cache_read=calendar_cache.read,
            cache_write=calendar_cache.write,
            now=tick.start_utc,
        )
        counters.add_logical_slots(sum(item.open for item in result.verdicts) * 3)
        return {"session": result}

    def holdings(*, tick, prior, ops_kernel, **_):
        legacy_run_holdings_sync(
            tick=tick,
            session_result=prior["session"],
            holdings_provider=gate_holder["providers"]["holdings"],
            store=holdings_store,
            pack_writer=packs,
            receipt_store=receipts[JobKind.HOLDINGS],
            reservation_seal=lambda *_: None,
            incident_writer=lambda *_: None,
            clock=clock,
            now=tick.start_utc,
            job_lock_path=root / "ops" / "locks" / "holdings.lock",
        )
        return prior

    def quotes(*, tick, prior, ops_kernel, **_):
        legacy_run_quotes_sync(
            tick=tick,
            session_result=prior["session"],
            holdings=holdings_store.read(),
            quotes_provider=gate_holder["providers"]["quotes"],
            quote_store=lambda outcome: quotes_store.save_batch(outcome.facts),
            pack_writer=packs,
            receipt_store=receipts[JobKind.QUOTES],
            reservation_seal=lambda *_: None,
            incident_writer=lambda *_: None,
            clock=clock,
            now=tick.start_utc,
            job_lock_path=root / "ops" / "locks" / "quotes.lock",
        )
        return prior

    def trades(*, tick, prior, ops_kernel, **_):
        legacy_run_trades_sync(
            tick=tick,
            session_result=prior["session"],
            holdings=holdings_store.read(),
            orders_provider=gate_holder["providers"]["orders"],
            detail_provider=gate_holder["providers"]["detail"],
            store=trades_store,
            scan_state=scan_state,
            pack_writer=packs,
            receipt_store=receipts[JobKind.TRADES],
            reservation_seal=lambda *_: None,
            incident_writer=lambda *_: None,
            regular_session_dates=deps.regular_session_dates,
            clock=clock,
            now=tick.start_utc,
            job_lock_path=root / "ops" / "locks" / "trades.lock",
            ops_kernel=ops_kernel,
            invocation_id=secrets.token_hex(16),
        )
        return prior

    def barrier(*, tick, prior, **_):
        open_markets = frozenset(
            item.market for item in prior["session"].verdicts if item.open
        )
        result = legacy_assemble_analysis_materials(
            tick=tick,
            open_markets=open_markets,
            receipt_readers={job: receipts[job].read for job in JobKind},
            pack_reader=lambda path, digest: packs.read_and_verify(Path(path), digest),
            now=tick.start_utc,
        )
        return {"session": prior["session"], "barrier": result}

    def analysis(*, tick, prior, ops_kernel, **_):
        return run_analysis_pipeline(
            tick=tick,
            barrier=prior["barrier"],
            store=ops_kernel,
            run_lock_path=root / "ops" / "locks" / "analysis.lock",
            glm_call=deps.glm_call,
            sol_call=deps.sol_call,
            now=tick.start_utc,
            clock=clock,
            numeric_evidence_sink=deps.numeric_evidence_sink,
            resolve_scope=resolve_account_scope_pattern,
        )

    return _IntegratedStageDepsV1(
        calendar, session, holdings, quotes, trades, barrier, analysis, clock
    )


def run_integrated_fake_day(
    *,
    utc_day: date,
    root: Path,
    deps: IntegratedCadenceDepsV1,
    compressed: bool = True,
) -> IntegratedCadenceReportV1:
    """Run one isolated 288-tick UTC fake day through the production A-E composition."""
    confined = _confined_fake_root(root)
    if type(utc_day) is not date or not isinstance(deps, IntegratedCadenceDepsV1):
        raise ValueError("invalid integrated cadence input")
    stages = _production_stage_deps(confined, deps)
    return _run_integrated_fake_day_range(
        utc_day=utc_day,
        root=confined,
        deps=stages,
        compressed=compressed,
    )
