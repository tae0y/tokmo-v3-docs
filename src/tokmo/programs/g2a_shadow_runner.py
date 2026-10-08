"""G2-A bounded read-only live-shadow runner.

A thin real-time driver that runs the *existing* production A-E composition
(``integrated_fake_cadence._production_stage_deps``) for exactly twelve
five-minute ticks over one real wall-clock hour, in an isolated shadow root
under ``~/.local/share/tokmo-v3/shadow/<stable_run_id>``.

Design invariants (owner: G2-A):

- The stable run id is derived from the UTC day, so a caller cannot mint a
  fresh id to escape the same-day cumulative provider ceiling.
- Preflight refuses before any tick if the production ops kernel is present,
  any handle is open under the production tree, the credential file is
  missing/wrong-mode/symlinked, the shadow schema mismatches, or a prior run
  left dirty durable state.  Preflight opens no socket and mutates nothing.
- The session gate decides open/closed.  If any market is unexpectedly open,
  the runner STOPs *before* any holdings/quotes/trades provider is reached, so
  a closed-session G2-A run makes exactly zero data provider calls.
- Every provider start is durably reserved before I/O by the production kernel
  and charged to the shared UTC-day counter (a 3744 admission ceiling at the
  time of this historical G2-A evidence; now monotonic telemetry only, G-10).

This module owns no HTTP.  The live Toss adapters and the credential loader are
constructed only by ``tokmo.composition`` (the single sanctioned importer of
the live transports) and injected as production deps.
"""

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import math
import os
from pathlib import Path
import secrets
import sqlite3
import subprocess
from types import MappingProxyType
from typing import Callable, Mapping

from tokmo.programs.integrated_fake_cadence import (
    IntegratedCadenceCountersV1,
    IntegratedCadenceDepsV1,
    _acquire_day_lock,
    _production_stage_deps,
)
from tokmo.records.ops_kernel import SCHEMA_VERSION, OpsKernelStore
from tokmo.records.receipt_store import ReceiptStore
from tokmo.values.material_pack import JobKind
from tokmo.values.ops_kernel import OpsKernelError, ProducerRole
from tokmo.values.ops_tick import tick_id_from_utc

TICK_COUNT = 12
TICK_SECONDS = 300
STABLE_RUN_PREFIX = "g2a-a-"
_DATA_LEAF = ".local/share/tokmo-v3"

# Operations that each correspond to exactly one outbound HTTPS open.  A durable
# started row for one of these is truthful evidence of a wire attempt; the live
# socket/DNS counts themselves are not instrumented here (see the F13 report),
# so we never claim a measured zero.
_WIRE_OPERATIONS = ("TOKEN", "CALENDAR_DAY_KR", "CALENDAR_DAY_US")

# Closed-session upper bound: cold-start auth once (day-counter-charging) plus one
# calendar start per market.  No data operation may start with both sessions
# closed, so the whole hour's provider budget is bounded by three starts.
_MANIFEST_UPPER_BOUND = MappingProxyType({
    "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
})


class G2AStopError(RuntimeError):
    """A STOP condition: exit nonzero, preserve durable evidence, no refund."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class G2APreflightError(RuntimeError):
    """Preflight refusal: the environment is not safe for a bounded live run."""

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


# ---------------------------------------------------------------------------
# Stable identity and shadow-root confinement.
# ---------------------------------------------------------------------------

def stable_run_id(utc_day: date) -> str:
    if type(utc_day) is not date:
        raise ValueError("invalid utc day")
    return f"{STABLE_RUN_PREFIX}{utc_day.isoformat()}"


def _data_root(home: Path) -> Path:
    return Path(home) / _DATA_LEAF


def shadow_root(home: Path, utc_day: date) -> Path:
    return _data_root(home) / "shadow" / stable_run_id(utc_day)


def _confine_shadow_root(home: Path, root: Path) -> Path:
    """Deny anything that is not exactly ``<data>/shadow/<id>``.

    The shadow namespace lives *under* the production data dir on purpose, so
    the guard is positive: the resolved parent must be the shadow dir and the
    leaf must not collide with a production store dir.
    """
    resolved = Path(root).resolve()
    shadow_dir = (_data_root(home) / "shadow").resolve()
    if resolved.parent != shadow_dir:
        raise ValueError("shadow root escapes the shadow namespace")
    if resolved.name in {"ops", "stores", "toss"}:
        raise ValueError("shadow root collides with a production store")
    return resolved


def _production_kernel_path(home: Path) -> Path:
    return _data_root(home) / "ops" / "ops_kernel.sqlite3"


# ---------------------------------------------------------------------------
# Manifest (sanitized upper bound for the twelve-tick hour).
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class G2AManifest:
    utc_day: str
    tick_count: int
    tick_seconds: int
    per_operation: Mapping[str, int]
    ceiling_consuming_upper_bound: int
    calendar_budget_upper_bound: int
    auth_exception_upper_bound: int
    total_upper_bound: int


def build_g2a_manifest(utc_day: date) -> G2AManifest:
    if type(utc_day) is not date:
        raise ValueError("invalid utc day")
    per_operation = dict(_MANIFEST_UPPER_BOUND)
    # TOKEN is a durable, metered start: it charges the shared UTC-day
    # counter exactly once (no unmetered auth exception).  Calendar spends its
    # own separate day budget; data starts consume the ceiling too and are zero
    # while both sessions are closed.
    auth_exception = 0
    calendar = sum(count for op, count in per_operation.items()
                   if op.startswith("CALENDAR_DAY_"))
    ceiling = sum(count for op, count in per_operation.items()
                  if not op.startswith("CALENDAR_DAY_"))
    return G2AManifest(
        utc_day=utc_day.isoformat(), tick_count=TICK_COUNT, tick_seconds=TICK_SECONDS,
        per_operation=MappingProxyType(dict(sorted(per_operation.items()))),
        ceiling_consuming_upper_bound=ceiling,
        calendar_budget_upper_bound=calendar,
        auth_exception_upper_bound=auth_exception,
        total_upper_bound=ceiling + calendar + auth_exception,
    )


def _manifest_json(manifest: G2AManifest) -> dict:
    return {
        "utc_day": manifest.utc_day, "tick_count": manifest.tick_count,
        "tick_seconds": manifest.tick_seconds,
        "per_operation": dict(manifest.per_operation),
        "ceiling_consuming_upper_bound": manifest.ceiling_consuming_upper_bound,
        "calendar_budget_upper_bound": manifest.calendar_budget_upper_bound,
        "auth_exception_upper_bound": manifest.auth_exception_upper_bound,
        "total_upper_bound": manifest.total_upper_bound,
    }


# ---------------------------------------------------------------------------
# Preflight.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class G2APreflightReport:
    ok: bool
    run_id: str
    checks: Mapping[str, str]


def _lsof_open_handle_count(path: Path) -> int:
    """Production default: count open handles under ``path`` via ``lsof``.

    A missing directory or a missing ``lsof`` binary both mean zero open
    handles from our perspective; any positive match refuses the run.
    """
    target = Path(path)
    if not target.exists():
        return 0
    try:
        completed = subprocess.run(
            ["lsof", "-t", "+D", str(target)],
            capture_output=True, text=True, check=False, timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return 0
    return len([line for line in completed.stdout.splitlines() if line.strip()])


def _require_private_credential(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise G2APreflightError("credential_mode")
    if (path.stat().st_mode & 0o777) != 0o600:
        raise G2APreflightError("credential_mode")


def preflight(
    *, home: Path, utc_day: date, credential_path: Path | None, clock: Callable[[], datetime],
    open_handle_probe: Callable[[Path], int],
) -> G2APreflightReport:
    """Refuse unless the environment is safe.  Opens no socket, mutates nothing."""
    checks: dict[str, str] = {}

    kernel = _production_kernel_path(home)
    if kernel.exists():
        raise G2APreflightError("production_kernel_present")
    checks["production_kernel_absent"] = "pass"

    data_root = _data_root(home)
    if open_handle_probe(data_root) != 0:
        raise G2APreflightError("open_handles_present")
    checks["open_handles_zero"] = "pass"

    if credential_path is not None:
        _require_private_credential(Path(credential_path))
    checks["credential_mode"] = "pass"

    root = shadow_root(home, utc_day)
    _confine_shadow_root(home, root)
    checks["shadow_confined"] = "pass"

    shadow_kernel = root / "ops" / "ops_kernel.sqlite3"
    if shadow_kernel.exists():
        try:
            connection = sqlite3.connect(shadow_kernel)
            try:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            finally:
                connection.close()
        except sqlite3.Error:
            raise G2APreflightError("shadow_schema_unreadable") from None
        if version != SCHEMA_VERSION:
            raise G2APreflightError("shadow_schema_mismatch")
    checks["shadow_schema"] = "pass"

    state_path = root / "run_state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
        except (OSError, ValueError):
            raise G2APreflightError("dirty_run_state") from None
        if not isinstance(state, dict) or state.get("status") == "STOPPED":
            raise G2APreflightError("dirty_run_state")
    checks["run_state_clean"] = "pass"

    # ``clock`` is read to prove the injected seam is live and aware-UTC.
    _aware_utc(clock())
    return G2APreflightReport(ok=True, run_id=stable_run_id(utc_day),
                             checks=MappingProxyType(dict(checks)))


# ---------------------------------------------------------------------------
# Per-tick metric and run report.
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class G2ATickMetric:
    tick_index: int
    tick_id: int
    scheduled_at: str
    started_at: str
    ended_at: str
    duration_seconds: float
    lag_seconds: float
    planned_per_operation: Mapping[str, int]
    started_per_operation: Mapping[str, int]
    settled_per_operation: Mapping[str, int]
    receipts: Mapping[str, str]
    wire_attempt_count: int


@dataclass(frozen=True)
class G2AReport:
    run_id: str
    utc_day: str
    tick_count: int
    per_operation: Mapping[str, int]
    provider_started_count: int
    calendar_started_count: int
    wire_attempt_count: int
    manifest: G2AManifest
    tick_metrics: tuple = field(default_factory=tuple)


def _metric_json(metric: G2ATickMetric) -> dict:
    # Live socket/DNS are not instrumented on this path, so the JSONL carries
    # only the truthful wire-attempt count (durable started TOKEN/calendar rows)
    # and never a fabricated ``socket_count``/``dns_count`` zero.
    return {
        "tick_index": metric.tick_index, "tick_id": metric.tick_id,
        "scheduled_at": metric.scheduled_at, "started_at": metric.started_at,
        "ended_at": metric.ended_at, "duration_seconds": metric.duration_seconds,
        "lag_seconds": metric.lag_seconds,
        "planned_per_operation": dict(metric.planned_per_operation),
        "started_per_operation": dict(metric.started_per_operation),
        "settled_per_operation": dict(metric.settled_per_operation),
        "receipts": dict(metric.receipts),
        "wire_attempt_count": metric.wire_attempt_count,
    }


# ---------------------------------------------------------------------------
# Real-time twelve-tick driver.
# ---------------------------------------------------------------------------

def _aware_utc(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("clock must return aware UTC")
    return value.astimezone(timezone.utc)


def _grid_base(now: datetime) -> datetime:
    epoch = math.ceil(now.timestamp() / TICK_SECONDS) * TICK_SECONDS
    return datetime.fromtimestamp(epoch, tz=timezone.utc)


def _private_dir(path: Path) -> None:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)


def _write_run_state(root: Path, status: str, stop_code: str | None,
                     now: datetime) -> None:
    payload = {"status": status, "stop_code": stop_code, "updated_at": now.isoformat()}
    path = root / "run_state.json"
    temp = path.with_name(".run_state.json.tmp")
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


def _open_progress(path: Path) -> sqlite3.Connection:
    connection = sqlite3.connect(path)
    os.chmod(path, 0o600)
    connection.execute(
        "CREATE TABLE IF NOT EXISTS g2a_ticks("
        "run_id TEXT NOT NULL, tick_index INTEGER NOT NULL "
        "CHECK(tick_index BETWEEN 0 AND 11), tick_id INTEGER NOT NULL, "
        "scheduled_at TEXT NOT NULL, completed_at TEXT NOT NULL, "
        "PRIMARY KEY(run_id, tick_index)) STRICT"
    )
    return connection


def _append_metric(root: Path, metric: G2ATickMetric,
                   metrics_leaf: str = "g2a_metrics.jsonl") -> None:
    path = root / "ops" / "metrics" / metrics_leaf
    _private_dir(path.parent)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        os.write(descriptor, (json.dumps(_metric_json(metric), sort_keys=True) + "\n").encode())
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _per_operation(rows, predicate) -> dict[str, int]:
    counts: dict[str, int] = {}
    for row in rows:
        if predicate(row):
            counts[row["operation"]] = counts.get(row["operation"], 0) + 1
    return counts


def _tick_receipts(root: Path, tick_id: int, clock: Callable[[], datetime]) -> dict[str, str]:
    receipts: dict[str, str] = {}
    for job in JobKind:
        path = root / "ops" / "receipts" / f"{job.value}.json"
        if not path.exists():
            continue
        record = ReceiptStore(path, clock=clock).read()
        if record is not None and record.attempt.tick_id == tick_id:
            receipts[job.value] = record.attempt.reason
    return receipts


_STAGE_ORDER = (
    "calendar_provider", "session_gate", "holdings_runner", "quotes_runner",
    "trades_runner", "barrier_runner", "analysis_runner",
)


def _a_gate_policy(session) -> None:
    """G2-A closed-session policy: any open market STOPs before data providers."""
    if getattr(session, "any_open", False):
        raise G2AStopError("unexpected_open_session")


def run_a_one_hour(
    *, home: Path, utc_day: date, deps: IntegratedCadenceDepsV1,
    credential_path: Path | None, clock: Callable[[], datetime],
    sleeper: Callable[[float], None], open_handle_probe: Callable[[Path], int],
    metrics_sink: Callable[[dict], None] | None = None,
) -> G2AReport:
    """Run exactly twelve 300s ticks of the production A-E cadence, live-shadowed."""
    if not isinstance(deps, IntegratedCadenceDepsV1):
        raise ValueError("invalid integrated cadence deps")
    preflight(home=home, utc_day=utc_day, credential_path=credential_path,
              clock=clock, open_handle_probe=open_handle_probe)

    root = _confine_shadow_root(home, shadow_root(home, utc_day))
    _private_dir(_data_root(home) / "shadow")
    _private_dir(root)
    run_id = stable_run_id(utc_day)

    lock_descriptor = _acquire_day_lock(root)
    progress = None
    manifest = build_g2a_manifest(utc_day)
    counters = IntegratedCadenceCountersV1()
    metrics: list[G2ATickMetric] = []
    try:
        _write_run_state(root, "RUNNING", None, _aware_utc(clock()))
        _persist_manifest(root, manifest, clock)
        store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        progress = _open_progress(root / "ops" / "g2a_progress.sqlite3")
        completed = {
            row[0] for row in progress.execute(
                "SELECT tick_index FROM g2a_ticks WHERE run_id=?", (run_id,))
        }
        store.reconcile_requests(ProducerRole.COLLECTOR,
                                 invocation_id=secrets.token_hex(16), now=_aware_utc(clock()))
        stages = _production_stage_deps(root, deps)
        stage_callables = tuple(getattr(stages, name) for name in _STAGE_ORDER)

        base = _grid_base(_aware_utc(clock()))
        try:
            for index in range(TICK_COUNT):
                if index in completed:
                    continue
                scheduled = base + timedelta(seconds=index * TICK_SECONDS)
                delay = (scheduled - _aware_utc(clock())).total_seconds()
                if delay > 0:
                    sleeper(delay)
                metric = _run_one_tick(
                    index=index, scheduled=scheduled, store=store,
                    stage_callables=stage_callables, counters=counters,
                    root=root, clock=clock, metrics_sink=metrics_sink,
                    gate_policy=_a_gate_policy)
                progress.execute(
                    "INSERT INTO g2a_ticks(run_id,tick_index,tick_id,scheduled_at,completed_at) "
                    "VALUES(?,?,?,?,?)",
                    (run_id, index, metric.tick_id, metric.scheduled_at, metric.ended_at))
                progress.commit()
                metrics.append(metric)
        except G2AStopError as stop:
            _write_run_state(root, "STOPPED", stop.code, _aware_utc(clock()))
            raise
        _write_run_state(root, "COMPLETED", None, _aware_utc(clock()))
        total_ticks = progress.execute(
            "SELECT COUNT(*) FROM g2a_ticks WHERE run_id=?", (run_id,)).fetchone()[0]
        return _build_report(run_id, utc_day, total_ticks, root, manifest,
                            tuple(metrics))
    finally:
        if progress is not None:
            progress.close()
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _run_one_tick(
    *, index, scheduled, store, stage_callables, counters, root, clock, metrics_sink,
    gate_policy: Callable[[object], None], metrics_leaf: str = "g2a_metrics.jsonl",
) -> G2ATickMetric:
    start_wall = _aware_utc(clock())
    lag = (start_wall - scheduled).total_seconds()
    tick = tick_id_from_utc(scheduled)

    context = None
    for position, stage in enumerate(stage_callables):
        try:
            value = stage(tick=tick, root=root, ops_kernel=store,
                          prior=context, counters=counters)
        except G2AStopError:
            raise
        except OpsKernelError as error:
            raise G2AStopError("kernel_stop") from error
        if value is not None:
            context = value
        # The session gate runs at position 1; its verdict decides whether the
        # downstream data providers (positions 2+) are allowed to run at all.
        # ``gate_policy`` STOPs before any data provider is reached when the
        # session shape is not the one this run demands.
        if position == 1 and isinstance(context, dict):
            session = context.get("session")
            if session is not None:
                gate_policy(session)

    end_wall = _aware_utc(clock())
    duration = (end_wall - start_wall).total_seconds()
    if duration >= TICK_SECONDS:
        raise G2AStopError("tick_overlap")

    rows = store.read_planned_requests(tick.tick_id)
    started = _per_operation(rows, lambda r: r["started_at"] is not None)
    metric = G2ATickMetric(
        tick_index=index, tick_id=tick.tick_id, scheduled_at=scheduled.isoformat(),
        started_at=start_wall.isoformat(), ended_at=end_wall.isoformat(),
        duration_seconds=duration, lag_seconds=lag,
        planned_per_operation=MappingProxyType(_per_operation(rows, lambda r: True)),
        started_per_operation=MappingProxyType(started),
        settled_per_operation=MappingProxyType(
            _per_operation(rows, lambda r: r["state"] == "SETTLED")),
        receipts=MappingProxyType(_tick_receipts(root, tick.tick_id, clock)),
        wire_attempt_count=sum(
            count for op, count in started.items() if op in _WIRE_OPERATIONS),
    )
    _append_metric(root, metric, metrics_leaf)
    if metrics_sink is not None:
        metrics_sink(_metric_json(metric))
    return metric


def _persist_manifest(root: Path, manifest: G2AManifest, clock: Callable[[], datetime]) -> None:
    path = root / "manifest.json"
    payload = _manifest_json(manifest)
    payload["persisted_at"] = _aware_utc(clock()).isoformat()
    temp = path.with_name(".manifest.json.tmp")
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


def _build_report(run_id, utc_day, total_ticks, root, manifest, metrics) -> G2AReport:
    per_operation: dict[str, int] = {}
    calendar_started = 0
    for metric in metrics:
        for operation, count in metric.started_per_operation.items():
            per_operation[operation] = per_operation.get(operation, 0) + count
            if operation.startswith("CALENDAR_DAY_"):
                calendar_started += count
    provider_started = _read_provider_started(root, utc_day)
    return G2AReport(
        run_id=run_id, utc_day=utc_day.isoformat(), tick_count=total_ticks,
        per_operation=MappingProxyType(dict(sorted(per_operation.items()))),
        provider_started_count=provider_started, calendar_started_count=calendar_started,
        wire_attempt_count=_read_wire_attempt_count(root),
        manifest=manifest, tick_metrics=tuple(metrics),
    )


def _read_provider_started(root: Path, utc_day: date) -> int:
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        row = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (utc_day.isoformat(),)).fetchone()
    finally:
        connection.close()
    return 0 if row is None else row[0]


def _read_wire_attempt_count(root: Path) -> int:
    """Truthful outbound-attempt count: durable started rows for the operations
    that each open exactly one HTTPS connection (the cold-start TOKEN plus every
    calendar GET).  This is evidence, not a live socket measurement."""
    placeholders = ",".join("?" for _ in _WIRE_OPERATIONS)
    connection = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
    try:
        row = connection.execute(
            "SELECT COUNT(*) FROM planned_requests WHERE started_at IS NOT NULL "
            f"AND operation IN ({placeholders})", _WIRE_OPERATIONS).fetchone()
    finally:
        connection.close()
    return 0 if row is None else row[0]
