"""Durable 48-hour live-shadow runner core (Stage 4 B, SDS §11.3).

One runner invokes the exact production cadence CLI seam every aligned
five-minute slot for exactly 48 hours from the recorded ``actual_start``.
Every ambient dependency — process environment, wall clock, real sleep,
git subprocess, the composition tick seam, and any transport — is injected
through :class:`ShadowRunnerDepsV1`; the production wiring lives at the
ambient edge in ``tools/run_48h_shadow.py``, so this module names no
composition, network, environment, or clock source.

Launched early the runner waits with provider I/O zero. ``actual_start``
is recorded exactly once; crash resume reads the durable state and
continues future slots, records slots that elapsed while down as MISSED,
and never replays an elapsed live slot. The terminal state is COMPLETE
with exit 0; relaunch after completion is inert.

Every preflight gate fails closed before any provider work: exact expected
repo HEAD commit, verified full-suite and pilot-acceptance PASS markers,
a safe non-production shadow root, order-write capability absent, read-only
Toss credentials present (names only, values never logged), a sane clock,
and one live instance (flock). Durable state is written atomically with
fsync, kept private (0600), refuses symlinks/hardlinks, and records only
allowlisted token-shaped fields — never a secret.

Lifecycle is the bounded harness channel only (START once, FATAL/STALLED
deduplicated with cooldown, FINISH once); stock signal and analysis alerts
are zero, and no model or order API is reachable from this module.
"""

import fcntl
import importlib.util
import json
import os
import re
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.prelaunch_markers import MarkerError
from tokmo.values.cadence_policy import CADENCE_OPERATIONS

RUNNER_SCHEMA = "tokmo-48h-shadow-state-v1"
RUNNER_MODE = "live-shadow"
LIFECYCLE_MODES = frozenset({"dry-run", "telegram"})

_COMMIT_SHA = re.compile(r"[0-9a-f]{40}")
_CLOCK_SANITY_WINDOW = timedelta(days=30)
_WAIT_CHUNK_SECONDS = 30.0

# Order-write capability must not even be importable (fail closed on any
# future appearance); the environment flag is refused outright.
_ORDER_WRITE_ENV_FLAG = "TOKMO_ENABLE_ORDER_WRITES"
_ORDER_WRITE_MODULES = (
    "tokmo.facts.toss_orders_write",
    "tokmo.facts.toss_write",
    "tokmo.programs.order_writer",
    "tokmo.programs.order_submit",
)

# The only summary fields a tick record may carry into durable state.
_TICK_RECORD_ALLOWLIST = (
    "outcome",
    "reason",
    "exit_code",
    "lag_seconds",
    "mask_digest",
    "open_markets",
)


class ShadowRunnerRefusal(ValueError):
    """Configuration/preflight refusal before any provider work (exit 1)."""


def _z(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


@dataclass(frozen=True)
class Shadow48hConfigV1:
    """The durable 48-hour campaign contract."""

    start_at_utc: datetime
    shadow_root: Path
    expected_commit: str
    full_suite_marker: Path
    review_marker: Path
    duration_seconds: int = 172800
    interval_seconds: int = 300
    mode: str = RUNNER_MODE
    policy_version: str = "cadence-300s-v1"
    lifecycle: str = "dry-run"
    fixture_path: Path | None = None

    def __post_init__(self):
        if self.mode != RUNNER_MODE:
            raise ValueError("live-shadow is the only enabled runner mode")
        start = self.start_at_utc
        if (
            not isinstance(start, datetime)
            or start.tzinfo is None
            or start.utcoffset() is None
            or start.microsecond != 0
        ):
            raise ValueError("start_at_utc must be aware")
        if (
            type(self.interval_seconds) is not int
            or self.interval_seconds <= 0
            or type(self.duration_seconds) is not int
            or self.duration_seconds <= 0
            or self.duration_seconds % self.interval_seconds != 0
        ):
            raise ValueError("duration must be whole positive intervals")
        if int(start.timestamp()) % self.interval_seconds != 0:
            raise ValueError("start_at_utc must align to the interval grid")
        if type(self.expected_commit) is not str or not _COMMIT_SHA.fullmatch(
            self.expected_commit
        ):
            raise ValueError("expected commit must be the full 40-hex identity")
        if self.lifecycle not in LIFECYCLE_MODES:
            raise ValueError("unknown lifecycle mode")
        for name in ("shadow_root", "full_suite_marker", "review_marker"):
            object.__setattr__(self, name, Path(getattr(self, name)))
        if self.fixture_path is not None:
            object.__setattr__(self, "fixture_path", Path(self.fixture_path))

    def snapshot(self) -> dict:
        """Campaign identity pinned into durable state on first write."""
        return {
            "start_at_utc": _z(self.start_at_utc),
            "duration_seconds": self.duration_seconds,
            "interval_seconds": self.interval_seconds,
            "expected_commit": self.expected_commit,
            "mode": self.mode,
            "policy_version": self.policy_version,
        }


def load_config(path: Path) -> Shadow48hConfigV1:
    data = json.loads(Path(path).read_text())
    return Shadow48hConfigV1(
        start_at_utc=datetime.fromisoformat(data["start_at"]),
        shadow_root=Path(data["shadow_root"]),
        expected_commit=data["expected_commit"],
        full_suite_marker=Path(data["full_suite_marker"]),
        review_marker=Path(data["review_marker"]),
        duration_seconds=data.get("duration_seconds", 172800),
        interval_seconds=data.get("interval_seconds", 300),
        mode=data.get("mode", RUNNER_MODE),
        policy_version=data.get("policy_version", "cadence-300s-v1"),
        lifecycle=data.get("lifecycle", "dry-run"),
        fixture_path=(
            Path(data["fixture"]) if data.get("fixture") is not None else None
        ),
    )


@dataclass(frozen=True)
class ShadowRunnerDepsV1:
    """Injected runner seams; production wiring is built in ``main``."""

    clock: Callable[[], datetime]
    sleep: Callable[[float], None]
    invoke_tick: Callable
    git_head: Callable[[], str]
    credentials_check: Callable[[], bool]
    marker_verify: Callable
    environ: Mapping
    lifecycle_emit: Callable


# -- durable state ----------------------------------------------------------


def _atomic_write_private(path: Path, payload: bytes) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(temporary, flags, 0o600)
    try:
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)
    fsync_directory(path.parent)


class Shadow48hStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)

    def read(self) -> dict | None:
        if self.path.is_symlink():
            raise ShadowRunnerRefusal("state_unsafe")
        if not self.path.exists():
            return None
        status = os.lstat(self.path)
        if status.st_nlink != 1:
            raise ShadowRunnerRefusal("state_unsafe")
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError) as error:
            raise ShadowRunnerRefusal("state_unsafe") from error
        if not isinstance(state, dict) or state.get("schema") != RUNNER_SCHEMA:
            raise ShadowRunnerRefusal("state_unsafe")
        return state

    def write(self, state: Mapping) -> None:
        _atomic_write_private(
            self.path,
            json.dumps(dict(state), sort_keys=True).encode(),
        )


# -- preflight --------------------------------------------------------------


def _production_canonical_root() -> Path:
    return Path.home() / ".local" / "share" / "tokmo-v3"


def _refuse_unsafe_root(root: Path) -> Path:
    resolved = Path(root).absolute()
    production = _production_canonical_root().absolute()
    if (
        resolved == production
        or production in resolved.parents
        or resolved in production.parents
    ):
        raise ShadowRunnerRefusal("unsafe_shadow_root")
    return resolved


def _order_write_capability_present(environ: Mapping) -> bool:
    if environ.get(_ORDER_WRITE_ENV_FLAG):
        return True
    return any(
        importlib.util.find_spec(name) is not None for name in _ORDER_WRITE_MODULES
    )


def _preflight(config: Shadow48hConfigV1, deps: ShadowRunnerDepsV1) -> None:
    if config.mode != RUNNER_MODE:
        raise ShadowRunnerRefusal("mode_refused")
    if deps.git_head() != config.expected_commit:
        raise ShadowRunnerRefusal("commit_mismatch")
    try:
        deps.marker_verify(
            config.full_suite_marker,
            kind="full-suite",
            expected_commit=config.expected_commit,
        )
    except MarkerError:
        raise ShadowRunnerRefusal("marker_full_suite_invalid") from None
    try:
        deps.marker_verify(
            config.review_marker,
            kind="pilot-acceptance",
            expected_commit=config.expected_commit,
        )
    except MarkerError:
        raise ShadowRunnerRefusal("marker_independent_review_invalid") from None
    if _order_write_capability_present(deps.environ):
        raise ShadowRunnerRefusal("order_write_capability_enabled")
    if not deps.credentials_check():
        raise ShadowRunnerRefusal("credentials_unavailable")
    now = deps.clock()
    window_start = config.start_at_utc - _CLOCK_SANITY_WINDOW
    window_end = (
        config.start_at_utc
        + timedelta(seconds=config.duration_seconds)
        + _CLOCK_SANITY_WINDOW
    )
    if not window_start <= now <= window_end:
        raise ShadowRunnerRefusal("clock_out_of_range")


# -- runner core ------------------------------------------------------------


def _sanitized_stage(stage: Mapping) -> dict:
    """Bounded per-stage evidence: exactly four typed fields, nothing else."""

    def token(value):
        return value if isinstance(value, str) or value is None else None

    latency = stage.get("latency_ms")
    if isinstance(latency, bool) or not isinstance(latency, int):
        latency = None
    return {
        "stage": token(stage.get("stage")),
        "status": token(stage.get("status")),
        "reason": token(stage.get("reason")),
        "latency_ms": latency,
    }


def _tick_record(summary: Mapping, *, duration_ms: int, config, slot_iso: str) -> dict:
    record = {"status": summary.get("outcome"), "duration_ms": duration_ms}
    for key in _TICK_RECORD_ALLOWLIST:
        if key == "outcome":
            continue
        record[key] = summary.get(key)
    wire = summary.get("wire")
    record["wire"] = dict(wire) if isinstance(wire, Mapping) else None
    stages = summary.get("stages")
    record["stages"] = (
        [_sanitized_stage(stage) for stage in stages if isinstance(stage, Mapping)]
        if isinstance(stages, list)
        else None
    )
    record["receipt"] = {
        "policy_version": config.policy_version,
        "scheduled_for_utc": slot_iso,
    }
    return record


def _terminal_summary(state: dict, finished_at: datetime) -> dict:
    counts: dict[str, int] = {}
    lags = []
    deferred_ticks = history_ok_ticks = 0
    exercised_routes: set[str] = set()
    for record in state["ticks"].values():
        status = record.get("status") or "UNKNOWN"
        counts[status] = counts.get(status, 0) + 1
        lag = record.get("lag_seconds")
        if isinstance(lag, (int, float)):
            lags.append(float(lag))
        for stage in record.get("stages") or []:
            if isinstance(stage, Mapping) and stage.get("stage") == "history":
                if stage.get("status") == "ok":
                    history_ok_ticks += 1
                if stage.get("reason") == "backfill_deferred":
                    deferred_ticks += 1
        wire = record.get("wire")
        started = wire.get("operation_started") if isinstance(wire, Mapping) else None
        for operation, count in (started or {}).items():
            if isinstance(count, int) and count > 0:
                exercised_routes.add(operation)
    return {
        "status": "COMPLETE",
        "finished_at": _z(finished_at),
        "counts": counts,
        "lag_max_seconds": max(lags) if lags else 0.0,
        "lag_avg_seconds": (round(sum(lags) / len(lags), 3) if lags else 0.0),
        "history_bootstrap": {
            "backfill_deferred_ticks": deferred_ticks,
            "history_ok_ticks": history_ok_ticks,
        },
        "unverified_routes": sorted(set(CADENCE_OPERATIONS) - exercised_routes),
    }


def _sleep_until(deps: ShadowRunnerDepsV1, target: datetime) -> None:
    while True:
        remaining = (target - deps.clock()).total_seconds()
        if remaining <= 0:
            return
        deps.sleep(min(remaining, _WAIT_CHUNK_SECONDS))


def run_48h_shadow(config: Shadow48hConfigV1, deps: ShadowRunnerDepsV1) -> int:
    """Run (or resume) one 48-hour live-shadow campaign; returns exit code."""
    try:
        root = _refuse_unsafe_root(config.shadow_root)
    except ShadowRunnerRefusal as refusal:
        print(f"tokmo-48h-shadow refusal={refusal}", file=sys.stderr)
        return 1

    harness = root / "harness"
    harness.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(harness, 0o700)
    flags = os.O_RDWR | os.O_CREAT
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    lock_descriptor = os.open(harness / "runner.lock", flags, 0o600)
    try:
        fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock_descriptor)
        print("tokmo-48h-shadow refusal=duplicate_instance", file=sys.stderr)
        return 1
    try:
        return _run_locked(
            config,
            deps,
            Shadow48hStateStore(
                harness / "state.json",
            ),
        )
    except ShadowRunnerRefusal as refusal:
        print(f"tokmo-48h-shadow refusal={refusal}", file=sys.stderr)
        return 1
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _run_locked(config, deps, store: Shadow48hStateStore) -> int:
    state = store.read()
    if state is not None:
        if state["config"] != config.snapshot():
            raise ShadowRunnerRefusal("state_config_mismatch")
        if state.get("terminal") is not None:
            # successful completion is inert: no rerun, no re-emission
            print(
                json.dumps(
                    {"status": "ALREADY_COMPLETE", "terminal": state["terminal"]},
                    sort_keys=True,
                )
            )
            return 0

    _preflight(config, deps)

    # launched before start: wait with provider I/O zero and no state
    if deps.clock() < config.start_at_utc:
        print(
            f"tokmo-48h-shadow waiting_until={_z(config.start_at_utc)}",
            file=sys.stderr,
        )
        _sleep_until(deps, config.start_at_utc)

    if state is None:
        actual_start = deps.clock()
        state = {
            "schema": RUNNER_SCHEMA,
            "config": config.snapshot(),
            "actual_start": _z(actual_start),
            "intended_end": _z(
                actual_start + timedelta(seconds=config.duration_seconds),
            ),
            "ticks": {},
            "terminal": None,
        }
        store.write(state)

    deps.lifecycle_emit("START")

    actual_start = datetime.fromisoformat(
        state["actual_start"].replace("Z", "+00:00"),
    )
    intended_end = datetime.fromisoformat(
        state["intended_end"].replace("Z", "+00:00"),
    )
    interval = config.interval_seconds
    first_epoch = -(-int(actual_start.timestamp()) // interval) * interval
    end_epoch = int(intended_end.timestamp())

    for slot_epoch in range(first_epoch, end_epoch, interval):
        slot = datetime.fromtimestamp(slot_epoch, tz=UTC)
        slot_iso = _z(slot)
        if slot_iso in state["ticks"]:
            continue  # a recorded slot is never replayed
        now = deps.clock()
        if now >= slot + timedelta(seconds=interval):
            # the whole slot elapsed while the runner was down: MISSED, and
            # never invoked retroactively
            state["ticks"][slot_iso] = {
                "status": "MISSED",
                "reason": "elapsed_during_downtime",
                "exit_code": None,
                "duration_ms": 0,
                "receipt": {
                    "policy_version": config.policy_version,
                    "scheduled_for_utc": slot_iso,
                },
            }
            store.write(state)
            continue
        if now < slot:
            _sleep_until(deps, slot)
        observed = deps.clock()
        try:
            summary = deps.invoke_tick(slot, observed)
        except Exception as error:
            duration_ms = max(
                0,
                int((deps.clock() - observed).total_seconds() * 1000),
            )
            reason = f"exception_{type(error).__name__}"
            state["ticks"][slot_iso] = {
                "status": "HARNESS_ERROR",
                "reason": reason,
                "exit_code": None,
                "duration_ms": duration_ms,
                "receipt": {
                    "policy_version": config.policy_version,
                    "scheduled_for_utc": slot_iso,
                },
            }
            store.write(state)
            deps.lifecycle_emit(
                "FATAL",
                signature=f"invoke_{type(error).__name__}",
            )
            continue
        duration_seconds = (deps.clock() - observed).total_seconds()
        state["ticks"][slot_iso] = _tick_record(
            summary,
            duration_ms=max(0, int(duration_seconds * 1000)),
            config=config,
            slot_iso=slot_iso,
        )
        store.write(state)
        if duration_seconds > interval:
            deps.lifecycle_emit(
                "STALLED",
                signature="tick_overrun",
                detail=f"duration_{int(duration_seconds)}s",
            )

    state["terminal"] = _terminal_summary(state, deps.clock())
    store.write(state)
    deps.lifecycle_emit("FINISH")
    print(
        json.dumps(
            {"status": "COMPLETE", "terminal": state["terminal"]}, sort_keys=True
        )
    )
    return 0


# The production wiring — process environment, wall clock, real sleep, git
# subprocess, HTTPS opener, and the composition tick seam — lives at the
# ambient edge in ``tools/run_48h_shadow.py``.
