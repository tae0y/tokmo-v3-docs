"""G2-B bounded read-only US-open one-tick canary runner.

G2-B is the first bounded live run of the *data* path (holdings, US holding
quotes, completed CLOSED-order trades) for exactly **one** production cadence
tick, executed while the US regular session is open and the KR session is
closed.  It reuses — never duplicates — the G2-A executor
(``g2a_shadow_runner._run_one_tick`` and the production A-E stage composition)
and the G2-A shadow root and durable ops kernel, so the same non-rearmable
UTC-day provider counter that G2-A charged carries forward: the canary adds to
it, it never resets it.

Design invariants (owner: G2-B):

- **Same durable root/kernel as G2-A.**  The run id and shadow root are the
  G2-A ones (``shadow/g2a-a-<utc-day>``); the ops kernel and its day counter
  are reused.  No new counter or root is minted, so the cumulative cap cannot
  be re-armed.
- **US open, KR closed, or STOP before any data provider.**  The session gate
  runs before the holdings/quotes/trades stages; ``_b_gate_policy`` STOPs the
  tick before a single data GET if the session is not exactly US-open/KR-closed.
- **Production stays untouched.**  A fingerprint of the production kernel plus
  its WAL/SHM/journal/lock sidecars is taken before and after the tick; any
  difference (including a sidecar appearing) STOPs the run.
- **Exactly one tick.**  A durable progress row makes a second invocation refuse
  rather than run a second tick.

This module owns no HTTP.  The live Toss adapters and credential loader are
constructed only by ``tokmo.composition`` and injected as production deps.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import secrets
import sqlite3
import stat
from types import MappingProxyType
from typing import Callable, Mapping

from tokmo.programs.g2a_shadow_runner import (
    G2APreflightError, G2AStopError, TICK_COUNT, TICK_SECONDS, _aware_utc,
    _confine_shadow_root, _data_root, _grid_base, _private_dir,
    _production_kernel_path, _run_one_tick, preflight,
    shadow_root, stable_run_id,
)
from tokmo.programs.integrated_fake_cadence import (
    IntegratedCadenceCountersV1, IntegratedCadenceDepsV1, _acquire_day_lock,
    _production_stage_deps,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.session_gate import Market

# Operations whose durable started row is truthful evidence of one outbound
# HTTPS open on the live path (the live socket/DNS counts themselves are not
# instrumented here, exactly as in G2-A).
_WIRE_OPERATIONS = (
    "TOKEN", "ACCOUNTS", "ACCOUNT_HOLDINGS", "PRICE_BATCH", "ORDERS_PAGE",
    "ORDER_DETAIL", "CALENDAR_DAY_KR", "CALENDAR_DAY_US",
)

# G2-B exercises only the live *data* path (holdings, US quotes, completed
# CLOSED-order trades).  The AI barrier/analysis stages are Slice-E scope and
# call the GLM/SOL models, which a bounded live data canary must never touch, so
# B truncates the production stage order after trades.
_B_STAGE_ORDER = (
    "calendar_provider", "session_gate", "holdings_runner", "quotes_runner",
    "trades_runner",
)

# The three data-path receipts a green B tick must all have durably saved.
_B_GREEN_JOBS = ("holdings", "quotes", "trades")


def _require_b_green_receipts(metric) -> None:
    """STOP ``data_tick_not_green`` unless all three data receipts read ``saved``.

    A green tick must have durably saved every data job — holdings, US quotes,
    and completed CLOSED trades.  A missing receipt (the stage never wrote one),
    a skipped one, or any ``failed_*`` reason is not green: the canary refuses to
    progress or continue the hour on unproven data rather than committing a tick
    whose data path did not actually succeed.
    """
    receipts = metric.receipts
    if receipts.get("holdings") != "saved" or receipts.get("quotes") != "saved":
        raise G2AStopError("data_tick_not_green")
    if receipts.get("trades") not in ("saved", "partial_incomplete"):
        raise G2AStopError("data_tick_not_green")


# ---------------------------------------------------------------------------
# Session policy: this canary demands US open and KR closed.
# ---------------------------------------------------------------------------

def _b_gate_policy(session) -> None:
    """STOP before any data provider unless US is open and KR is closed."""
    verdicts = {verdict.market: verdict for verdict in session.verdicts}
    kr = verdicts.get(Market.KR)
    us = verdicts.get(Market.US)
    if kr is None or us is None:
        raise G2AStopError("session_shape_unexpected")
    if kr.open:
        raise G2AStopError("kr_must_be_closed")
    if not us.open:
        raise G2AStopError("us_not_open")


# ---------------------------------------------------------------------------
# Production-state fingerprint (existence, kind, mode, size, mtime_ns, sha256).
# ---------------------------------------------------------------------------

def _fingerprint(path: Path) -> tuple:
    """A privacy-safe fingerprint of one production path.

    Absence is a first-class value, so a sidecar that appears mid-run compares
    unequal to its prior absence.  A symlink is distinguished from a regular
    file by kind, and only a genuine regular file is hashed.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return ("absent",)
    except OSError:
        return ("unsafe",)
    is_link = stat.S_ISLNK(info.st_mode)
    is_regular = stat.S_ISREG(info.st_mode)
    digest = ""
    if is_regular and not is_link:
        try:
            with open(path, "rb") as handle:
                digest = hashlib.sha256(handle.read()).hexdigest()
        except OSError:
            return ("unsafe",)
    kind = "symlink" if is_link else ("regular" if is_regular else "other")
    return ("present", kind, stat.S_IMODE(info.st_mode), info.st_size,
            info.st_mtime_ns, digest)


def _production_paths(home: Path) -> tuple[Path, ...]:
    kernel = _production_kernel_path(home)
    return (
        kernel,
        kernel.with_name(kernel.name + "-wal"),
        kernel.with_name(kernel.name + "-shm"),
        kernel.with_name(kernel.name + "-journal"),
        _data_root(home) / "ops" / "locks" / "integrated.lock",
    )


def _snapshot_production(paths) -> tuple:
    return tuple((str(path), _fingerprint(path)) for path in paths)


# ---------------------------------------------------------------------------
# Report (masked aggregate counts only).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class G2BReport:
    run_id: str
    utc_day: str
    tick_count: int
    provider_started_count: int
    token_started_count: int
    accounts_started_count: int
    account_holdings_started_count: int
    us_quote_batch_count: int
    kr_quote_count: int
    orders_page_count: int
    order_detail_count: int
    calendar_started_count: int
    wire_attempt_count: int
    production_invariant_ok: bool
    duration_seconds: float
    per_operation: Mapping[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Durable one-tick progress + B run state.
# ---------------------------------------------------------------------------

def _open_progress(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS g2b_ticks("
        "run_id TEXT NOT NULL, tick_id INTEGER NOT NULL, "
        "scheduled_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "PRIMARY KEY(run_id)) STRICT"
    )
    return connection


def _write_b_state(root: Path, status: str, stop_code: str | None,
                   now: datetime) -> None:
    payload = {"status": status, "stop_code": stop_code, "updated_at": now.isoformat()}
    path = root / "g2b_run_state.json"
    temp = path.with_name(".g2b_run_state.json.tmp")
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", closefd=False) as handle:
            handle.write(json.dumps(payload, sort_keys=True))
            handle.flush()
            os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temp, path)
    os.chmod(path, 0o600)


# ---------------------------------------------------------------------------
# One-tick driver.
# ---------------------------------------------------------------------------

def run_b_canary(
    *, home: Path, utc_day: date, deps: IntegratedCadenceDepsV1,
    credential_path: Path | None, clock: Callable[[], datetime],
    open_handle_probe: Callable[[Path], int],
    metrics_sink: Callable[[dict], None] | None = None,
) -> G2BReport:
    """Run exactly one production A-E cadence tick, live, US-open/KR-closed."""
    if not isinstance(deps, IntegratedCadenceDepsV1):
        raise ValueError("invalid integrated cadence deps")

    # Preflight reuses the G2-A guards: production kernel absent, zero open
    # handles under the data root, private-mode credential, shadow schema, clean
    # run state.  It opens no socket and mutates nothing.
    preflight(home=home, utc_day=utc_day, credential_path=credential_path,
              clock=clock, open_handle_probe=open_handle_probe)

    root = _confine_shadow_root(home, shadow_root(home, utc_day))
    _private_dir(_data_root(home) / "shadow")
    _private_dir(root)
    run_id = stable_run_id(utc_day)

    # A prior STOPPED canary preserves its evidence and refuses to re-run.
    state_path = root / "g2b_run_state.json"
    if state_path.exists():
        try:
            prior = json.loads(state_path.read_text())
        except (OSError, ValueError):
            raise G2AStopError("dirty_canary_state") from None
        if not isinstance(prior, dict) or prior.get("status") == "STOPPED":
            raise G2AStopError("dirty_canary_state")

    production_paths = _production_paths(home)
    before = _snapshot_production(production_paths)

    lock_descriptor = _acquire_day_lock(root)
    progress = None
    counters = IntegratedCadenceCountersV1()
    start_wall = _aware_utc(clock())
    try:
        _write_b_state(root, "RUNNING", None, start_wall)
        store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        progress = _open_progress(root / "ops" / "g2b_progress.sqlite3")
        if progress.execute("SELECT COUNT(*) FROM g2b_ticks WHERE run_id=?",
                            (run_id,)).fetchone()[0]:
            raise G2AStopError("canary_already_ran")

        store.reconcile_requests(ProducerRole.COLLECTOR,
                                 invocation_id=secrets.token_hex(16), now=start_wall)
        stages = _production_stage_deps(root, deps)
        stage_callables = tuple(getattr(stages, name) for name in _B_STAGE_ORDER)

        # The current cadence tick, floored to its 300s grid boundary: providers
        # derive the same tick from the shared clock, so no wall-clock sleep and
        # no grid wait is needed for a single immediate canary tick.
        scheduled = tick_id_from_utc(start_wall).start_utc
        try:
            metric = _run_one_tick(
                index=0, scheduled=scheduled, store=store,
                stage_callables=stage_callables, counters=counters, root=root,
                clock=clock, metrics_sink=metrics_sink, gate_policy=_b_gate_policy)
            # The tick only counts if the whole data path proved green; an
            # unproven tick STOPs before it is durably committed.
            _require_b_green_receipts(metric)
            progress.execute(
                "INSERT INTO g2b_ticks(run_id,tick_id,scheduled_at,completed_at) "
                "VALUES(?,?,?,?)",
                (run_id, metric.tick_id, metric.scheduled_at, metric.ended_at))
            progress.commit()
        except G2AStopError as stop:
            code = _reconcile_production(production_paths, before, stop.code)
            _write_b_state(root, "STOPPED", code, _aware_utc(clock()))
            raise G2AStopError(code) from stop

        code = _reconcile_production(production_paths, before, None)
        if code is not None:
            _write_b_state(root, "STOPPED", code, _aware_utc(clock()))
            raise G2AStopError(code)
        end_wall = _aware_utc(clock())
        _write_b_state(root, "COMPLETED", None, end_wall)
        return _build_report(run_id, utc_day, root,
                             (end_wall - start_wall).total_seconds())
    finally:
        if progress is not None:
            progress.close()
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _reconcile_production(paths, before, prior_code: str | None) -> str | None:
    """Return the STOP code once the production snapshot is re-compared.

    A production mutation dominates any other STOP code: it is the most severe
    invariant this canary defends, so it is surfaced even if the tick was
    already stopping for another reason.
    """
    if _snapshot_production(paths) != before:
        return "production_mutated"
    return prior_code


# ---------------------------------------------------------------------------
# Report assembly from durable started rows + the day counter.
# ---------------------------------------------------------------------------

def _started_operations(root: Path) -> dict[str, int]:
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        rows = connection.execute(
            "SELECT operation, COUNT(*) FROM planned_requests "
            "WHERE started_at IS NOT NULL GROUP BY operation").fetchall()
    finally:
        connection.close()
    return {operation: count for operation, count in rows}


def _provider_started_count(root: Path, utc_day: date) -> int:
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        row = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (utc_day.isoformat(),)).fetchone()
    finally:
        connection.close()
    return 0 if row is None else row[0]


def _build_report(run_id: str, utc_day: date, root: Path,
                  duration_seconds: float, tick_count: int = 1) -> G2BReport:
    started = _started_operations(root)
    calendar = sum(count for operation, count in started.items()
                   if operation.startswith("CALENDAR_DAY_"))
    wire = sum(count for operation, count in started.items()
               if operation in _WIRE_OPERATIONS)
    return G2BReport(
        run_id=run_id, utc_day=utc_day.isoformat(), tick_count=tick_count,
        provider_started_count=_provider_started_count(root, utc_day),
        token_started_count=started.get("TOKEN", 0),
        accounts_started_count=started.get("ACCOUNTS", 0),
        account_holdings_started_count=started.get("ACCOUNT_HOLDINGS", 0),
        us_quote_batch_count=started.get("PRICE_BATCH", 0),
        kr_quote_count=0,
        orders_page_count=started.get("ORDERS_PAGE", 0),
        order_detail_count=started.get("ORDER_DETAIL", 0),
        calendar_started_count=calendar,
        wire_attempt_count=wire,
        production_invariant_ok=True,
        duration_seconds=duration_seconds,
        per_operation=MappingProxyType(dict(sorted(started.items()))),
    )


# ---------------------------------------------------------------------------
# One-hour twelve-tick canary campaign.
#
# The first tick IS the canary: if it STOPs (session shape wrong, production
# touched, ceiling, kernel), the run halts before any further sleep, so the
# same process either proves the live data path green and continues the hour or
# stops with its cumulative evidence intact.  Every tick reuses the same shadow
# root, ops kernel, providers, and non-rearmable day counter as G2-A/the canary.
# ---------------------------------------------------------------------------

_HOUR_METRICS_LEAF = "g2b_metrics.jsonl"


def _open_hour_progress(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS g2b_hour_ticks("
        "run_id TEXT NOT NULL, tick_index INTEGER NOT NULL "
        "CHECK(tick_index BETWEEN 0 AND 11), tick_id INTEGER NOT NULL, "
        "scheduled_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "PRIMARY KEY(run_id, tick_index)) STRICT"
    )
    return connection


def run_b_one_hour(
    *, home: Path, utc_day: date, deps: IntegratedCadenceDepsV1,
    credential_path: Path | None, clock: Callable[[], datetime],
    sleeper: Callable[[float], None], open_handle_probe: Callable[[Path], int],
    metrics_sink: Callable[[dict], None] | None = None,
) -> G2BReport:
    """Run exactly twelve 300s live data ticks over one US-open/KR-closed hour.

    The first tick is the canary.  A COMPLETED same-day run refuses to re-run; a
    STOPPED or interrupted run is resumed against the same durable progress and
    day counter (never re-armed).  A production-state mutation before or after
    the whole hour is the highest-priority STOP.
    """
    if not isinstance(deps, IntegratedCadenceDepsV1):
        raise ValueError("invalid integrated cadence deps")

    preflight(home=home, utc_day=utc_day, credential_path=credential_path,
              clock=clock, open_handle_probe=open_handle_probe)

    root = _confine_shadow_root(home, shadow_root(home, utc_day))
    _private_dir(_data_root(home) / "shadow")
    _private_dir(root)
    run_id = stable_run_id(utc_day)

    # A COMPLETED run refuses a same-day rerun; a STOPPED/interrupted run resumes
    # (its cumulative evidence and the non-rearmable counter are preserved, and a
    # repaired invocation is allowed without minting a new root).
    state_path = root / "g2b_run_state.json"
    if state_path.exists():
        try:
            prior = json.loads(state_path.read_text())
        except (OSError, ValueError):
            raise G2AStopError("dirty_canary_state") from None
        if not isinstance(prior, dict):
            raise G2AStopError("dirty_canary_state")
        if prior.get("status") == "COMPLETED":
            raise G2AStopError("canary_already_complete")

    production_paths = _production_paths(home)
    before = _snapshot_production(production_paths)

    lock_descriptor = _acquire_day_lock(root)
    progress = None
    counters = IntegratedCadenceCountersV1()
    start_wall = _aware_utc(clock())
    try:
        _write_b_state(root, "RUNNING", None, start_wall)
        store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        progress = _open_hour_progress(root / "ops" / "g2b_hour_progress.sqlite3")
        completed = {
            row[0] for row in progress.execute(
                "SELECT tick_index FROM g2b_hour_ticks WHERE run_id=?", (run_id,))
        }
        store.reconcile_requests(ProducerRole.COLLECTOR,
                                 invocation_id=secrets.token_hex(16), now=start_wall)
        stages = _production_stage_deps(root, deps)
        stage_callables = tuple(getattr(stages, name) for name in _B_STAGE_ORDER)

        base = _grid_base(start_wall)
        try:
            for index in range(TICK_COUNT):
                if index in completed:
                    continue
                scheduled = base + timedelta(seconds=index * TICK_SECONDS)
                delay = (scheduled - _aware_utc(clock())).total_seconds()
                if delay > 0:
                    sleeper(delay)
                elif delay <= -TICK_SECONDS:
                    # No catch-up: a fully missed tick window STOPs rather than
                    # replaying ticks back to back.
                    raise G2AStopError("tick_missed")
                metric = _run_one_tick(
                    index=index, scheduled=scheduled, store=store,
                    stage_callables=stage_callables, counters=counters, root=root,
                    clock=clock, metrics_sink=metrics_sink,
                    gate_policy=_b_gate_policy, metrics_leaf=_HOUR_METRICS_LEAF)
                # Every tick — the first canary tick and every later one — must
                # prove all three data receipts green before it is committed or
                # the hour is allowed to continue.
                _require_b_green_receipts(metric)
                progress.execute(
                    "INSERT INTO g2b_hour_ticks(run_id,tick_index,tick_id,"
                    "scheduled_at,completed_at) VALUES(?,?,?,?,?)",
                    (run_id, index, metric.tick_id, metric.scheduled_at,
                     metric.ended_at))
                progress.commit()
        except G2AStopError as stop:
            code = _reconcile_production(production_paths, before, stop.code)
            _write_b_state(root, "STOPPED", code, _aware_utc(clock()))
            raise G2AStopError(code) from stop

        code = _reconcile_production(production_paths, before, None)
        if code is not None:
            _write_b_state(root, "STOPPED", code, _aware_utc(clock()))
            raise G2AStopError(code)
        end_wall = _aware_utc(clock())
        _write_b_state(root, "COMPLETED", None, end_wall)
        total_ticks = progress.execute(
            "SELECT COUNT(*) FROM g2b_hour_ticks WHERE run_id=?", (run_id,)).fetchone()[0]
        return _build_report(run_id, utc_day, root,
                             (end_wall - start_wall).total_seconds(), total_ticks)
    finally:
        if progress is not None:
            progress.close()
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)
