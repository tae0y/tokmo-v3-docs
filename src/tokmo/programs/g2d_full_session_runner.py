"""G2-D full-session shadow runner (KR regular session, one-shot Mac pilot).

G2-D is **G2-B (live data path) + G2-C (injected scripted analysis) extended
across the whole KR regular session**.  It builds no new executor: it loops the
existing production seven-stage cadence
(``integrated_fake_cadence._production_stage_deps``) tick by tick and reuses the
G2-A per-tick driver (``g2a_shadow_runner._run_one_tick``), whose position-1
``gate_policy`` hook fires the KR self-termination *before* any data provider.

Design invariants (owner: G2-D):

- **KR-open only, real-time self-terminating (§10.3).**  The first tick STOPs
  ``kr_not_open_<reason>`` if KR is not open; each subsequent tick re-reads the
  session verdict and continues only while KR is open; the first ``after_close``
  tick leaves the loop as a *success* terminus (``session_ended``) before any
  data provider.  A zero-tick ``kr_not_open_*`` refusal is durably recorded as
  ``REFUSED`` and never seals the day — the real open-hours attempt still runs
  without an approval flag.  A ``G2D_MAX_TICKS`` hard cap defends a mis-judged
  calendar.
- **One-shot pilot, no restart infra (§10.4, M-3).**  There is **no**
  ``g2d_ticks`` durable per-tick table, the whole session runs in one process,
  and any crash ends the run ``incomplete=True`` with the production invariant
  re-measured and the masked exception type recorded durably — never a resume,
  replay, or restart.  Within the one process an expired memory bearer is
  re-acquired by composition up to ten TOTAL durable TOKEN calls; the eleventh,
  or any acquisition failure, is the terminal ``token_lost_incomplete``.  A
  same-day re-run of a non-REFUSED state refuses without an explicit
  user-approval flag.  ``KeyboardInterrupt``/``SystemExit`` are recorded but
  never swallowed.
- **Per-tick green-receipt gate (B-2).**  Every executed tick must prove the
  same three data receipts G2-B demands (holdings/quotes saved, trades
  saved-or-partial); a non-green tick is an explicit ``data_tick_not_green``
  STOP, never a success terminus, and the report carries the receipt counts
  and green-tick count.
- **Data ceiling dedicated, campaign total scaled (§10.5, M-4/B-1).**  Each
  tick's data upper bound is G2-B's 23; the campaign ledger composition builds
  for G2-D is dedicated to 2310 (not G2-B's 286); the calendar wire (<=70) is a
  separate budget the UTC-day telemetry counter (historically 3744-capped;
  now uncapped, G-10) never touches.  The pre-tick gate on
  the shared durable day counter (which still carries any prior same-day
  consumption) STOPs before a tick could cross the campaign total.
- **Real GLM/SOL calls = 0 (§10.7).**  The barrier/analysis stages run with
  caller-injected scripted stubs only; the runner refuses to start unless both
  model slots carry the injection marker.  The runner knows nothing of the
  numeric gate logic (§10.8) — the report's numeric aggregates are the analysis
  stage's explicit masked per-tick metric (subject-tick numeric state and
  trigger fires), collected by composition and read back here; a tick whose
  metric is missing is counted separately and marks the run INCOMPLETE, so an
  instrumentation gap can never read as "no signal" (M-3).

This module owns no HTTP and imports no live transport or model provider: the
live Toss adapters and the wrapped model stubs are constructed only by
``tokmo.composition`` (the single sanctioned live importer) and injected.
"""

import fcntl
import json
import os
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from types import MappingProxyType

from tokmo.programs.g2a_shadow_runner import (
    TICK_SECONDS,
    G2AStopError,
    _aware_utc,
    _confine_shadow_root,
    _data_root,
    _grid_base,
    _private_dir,
    _run_one_tick,
    preflight,
    shadow_root,
    stable_run_id,
)
from tokmo.programs.g2b_canary_runner import (
    _WIRE_OPERATIONS,
    _production_paths,
    _provider_started_count,
    _reconcile_production,
    _require_b_green_receipts,
    _snapshot_production,
    _started_operations,
)
from tokmo.programs.integrated_fake_cadence import (
    IntegratedCadenceCountersV1,
    IntegratedCadenceDepsV1,
    _acquire_day_lock,
    _production_stage_deps,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market

# The masked "state established" sentinel: any observed numeric_state that is not
# this value counts as an observed signal.  Spelled as a bare token so the runner
# stays free of the gate's contract enum (§10.8 concern separation).
_DATA_INCOMPLETE = "DATA_INCOMPLETE"

# Session bounds and ceilings (§10.3, §10.5).
G2D_MAX_TICKS = 100
_G2D_PER_TICK_DATA_CEILING = 23  # 1 ACCOUNTS + 5 holdings + 5 quotes + 12 trades
G2D_CAMPAIGN_DATA_CEILING = 10 + G2D_MAX_TICKS * _G2D_PER_TICK_DATA_CEILING  # 2310

# STOP codes that mean the single process lost its footing rather than reaching a
# clean terminus: these end the run INCOMPLETE (§10.4, M-3).
_INCOMPLETE_STOP_CODES = frozenset({"token_lost_incomplete"})

_D_STAGE_ORDER = (
    "calendar_provider",
    "session_gate",
    "holdings_runner",
    "quotes_runner",
    "trades_runner",
    "barrier_runner",
    "analysis_runner",
)

_METRICS_LEAF = "g2d_metrics.jsonl"


# ---------------------------------------------------------------------------
# Injected model slot: the marker + masked counter for caller-supplied stubs.
# ---------------------------------------------------------------------------


class _G2DInjectedModelCall:
    """Marker proving a model slot is caller-injected (never a live provider).

    Composition wraps each caller-supplied GLM/SOL stub in this class; the runner
    refuses to start if either slot is not an instance, so a smuggled live
    provider is caught before any provider runs (§10.7, T-N23).  The wrapper also
    counts and times its calls.  It deliberately does **not** parse the
    projection body: the report's numeric aggregates come from the analysis
    stage's explicit metric, never from GLM projection rows (M-3).
    """

    def __init__(self, call: Callable, clock: Callable[[], datetime]) -> None:
        self._call = call
        self._clock = clock
        self.count = 0
        self.seconds = 0.0

    def __call__(self, body):
        started = _aware_utc(self._clock())
        try:
            return self._call(body)
        finally:
            self.count += 1
            self.seconds += (_aware_utc(self._clock()) - started).total_seconds()


def _token_acquire_for(deps: IntegratedCadenceDepsV1):
    """Expose the composed bounded campaign-token acquirer for focused tests.

    G2-D's token acquirer (composition-owned) re-acquires an expired in-process
    bearer under a fresh durable TOKEN identity, up to ten TOTAL durable calls;
    the eleventh or any failure STOPs ``token_lost_incomplete`` (§10.4) —
    restart, resume, and replay stay forbidden.
    """
    acquire = getattr(deps, "_g2d_token_acquire", None)
    if acquire is None:
        raise G2AStopError("token_acquire_missing")
    return acquire


# ---------------------------------------------------------------------------
# Report (masked aggregates only — no symbol/account/amount raw; §10.9).
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class G2DReport:
    run_id: str
    utc_day: str
    tick_count: int
    stop_reason: str | None
    provider_started_count: int
    wire_attempt_count: int
    numeric_state_established_count: int
    trigger_counts: Mapping[str, int]
    disposition_counts: Mapping[str, int]
    production_invariant_ok: bool
    incomplete: bool
    duration_seconds: float
    glm_calls: int = 0
    sol_calls: int = 0
    # B-2: per-tick green-receipt evidence — how many executed ticks proved all
    # three data receipts green, and every observed "job:reason" receipt count.
    green_tick_count: int = 0
    receipt_counts: Mapping[str, int] = field(default_factory=dict)
    # M-3: the full masked numeric-state distribution (subject-tick counts by
    # state value) behind numeric_state_established_count, plus the count of
    # executed ticks whose explicit analysis metric never arrived — an
    # instrumentation gap, reported apart from "no signal" and forcing
    # incomplete=True.
    numeric_state_counts: Mapping[str, int] = field(default_factory=dict)
    numeric_evidence_missing_tick_count: int = 0
    # M-1: durable TOKEN calls actually started (<= 10 by construction).
    token_started_count: int = 0
    # B-3: the masked exception type (name only, never a message or value) when
    # the run ended by crash/abort.
    masked_exception_type: str | None = None
    # Masked pilot annotations (owner instruction): a first-session run has no
    # prior-day baseline and its 5/15/30m deltas warm up in-session, so both are
    # explicitly flagged unverified rather than silently presented as sound.
    first_session_baseline_unverified: bool = True
    prior_day_gap_unverified: bool = True
    per_operation: Mapping[str, int] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# KR-open gate policy (§10.6): reused by ``_run_one_tick`` at stage position 1.
# ---------------------------------------------------------------------------


def _d_gate_policy(session) -> None:
    """STOP before any data provider unless KR is open (US is barrier's concern)."""
    verdicts = {verdict.market: verdict for verdict in session.verdicts}
    kr = verdicts.get(Market.KR)
    if kr is not None and kr.reason == "after_close":
        raise G2AStopError("session_ended")
    if kr is None:
        raise G2AStopError("kr_not_open_missing")
    if not kr.open:
        # The refusal carries the verdict's own reason (before_open, no_regular,
        # stale_cache, ...) so an early start stays distinguishable from a
        # holiday in the durable record (M-2).
        raise G2AStopError(f"kr_not_open_{kr.reason}")
    # US is intentionally unconstrained — the barrier absorbs a closed US.


# ---------------------------------------------------------------------------
# Run-state (single JSON; there is deliberately no per-tick progress table).
# ---------------------------------------------------------------------------


def _write_d_state(
    root: Path,
    status: str,
    stop_code: str | None,
    now: datetime,
    exception_type: str | None = None,
) -> None:
    payload = {"status": status, "stop_code": stop_code, "updated_at": now.isoformat()}
    if exception_type is not None:
        payload["exception_type"] = exception_type
    path = root / "g2d_run_state.json"
    temp = path.with_name(".g2d_run_state.json.tmp")
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
# Full-session driver.
# ---------------------------------------------------------------------------


def run_d_full_session(
    *,
    home: Path,
    utc_day: date,
    deps: IntegratedCadenceDepsV1,
    credential_path: Path | None,
    clock: Callable[[], datetime],
    sleeper: Callable[[float], None],
    open_handle_probe: Callable[[Path], int],
    campaign_data_ceiling: int = G2D_CAMPAIGN_DATA_CEILING,
    approved_rerun: bool = False,
    metrics_sink: Callable[[dict], None] | None = None,
) -> G2DReport:
    """Loop the production seven-stage cadence, live-data, across the KR session."""
    if not isinstance(deps, IntegratedCadenceDepsV1):
        raise ValueError("invalid integrated cadence deps")
    # Both model slots must be caller-injected — a smuggled live provider STOPs
    # before any provider or socket (§10.7).
    if not isinstance(deps.glm_call, _G2DInjectedModelCall):
        raise G2AStopError("glm_not_injected")
    if not isinstance(deps.sol_call, _G2DInjectedModelCall):
        raise G2AStopError("sol_not_injected")
    # The composition-owned instrumentation side-channels: the analysis stage's
    # explicit per-tick numeric evidence and the latched bearer-loss flag.  A
    # deps object without them was not built by ``build_g2d_live_deps``.
    numeric_evidence = getattr(deps, "_g2d_numeric_evidence", None)
    token_lost = getattr(deps, "_g2d_token_lost", None)
    if numeric_evidence is None or token_lost is None:
        raise G2AStopError("g2d_instrumentation_not_wired")

    preflight(
        home=home,
        utc_day=utc_day,
        credential_path=credential_path,
        clock=clock,
        open_handle_probe=open_handle_probe,
    )

    root = _confine_shadow_root(home, shadow_root(home, utc_day))
    _private_dir(_data_root(home) / "shadow")
    _private_dir(root)
    run_id = stable_run_id(utc_day)

    # A prior run's state refuses a same-day re-run without explicit approval:
    # the pilot never resumes or replays a completed/incomplete grid (§10.4).
    # A prior zero-tick REFUSED record (kr_not_open_*) is the one exception —
    # it never burned data providers, so it must not seal the day (M-2).
    state_path = root / "g2d_run_state.json"
    if state_path.exists() and not approved_rerun:
        try:
            prior = json.loads(state_path.read_text())
        except (OSError, ValueError):
            raise G2AStopError("rerun_requires_approval") from None
        if not (isinstance(prior, dict) and prior.get("status") == "REFUSED"):
            raise G2AStopError("rerun_requires_approval")

    production_paths = _production_paths(home)
    before = _snapshot_production(production_paths)

    lock_descriptor = _acquire_day_lock(root)
    counters = IntegratedCadenceCountersV1()
    executed_tick_ids: list[int] = []
    receipt_counts: dict[str, int] = {}
    green_tick_count = 0
    start_wall = _aware_utc(clock())
    incomplete = False
    stop_reason: str | None = None
    try:
        _write_d_state(root, "RUNNING", None, start_wall)
        store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
        store.reconcile_requests(
            ProducerRole.COLLECTOR, invocation_id=secrets.token_hex(16), now=start_wall
        )
        stages = _production_stage_deps(root, deps)
        stage_callables = tuple(getattr(stages, name) for name in _D_STAGE_ORDER)
        base = _grid_base(start_wall)

        try:
            for index in range(G2D_MAX_TICKS):
                # Pre-tick campaign ceiling gate (§10.5): STOP before a tick whose
                # data upper bound would cross the campaign total; the shared day
                # counter still carries any prior same-day consumption, and the
                # calendar is a separate budget never counted here (M-4).
                started = _provider_started_count(root, utc_day)
                if started + _G2D_PER_TICK_DATA_CEILING > campaign_data_ceiling:
                    stop_reason = "campaign_ceiling"
                    break
                scheduled = base + timedelta(seconds=index * TICK_SECONDS)
                delay = (scheduled - _aware_utc(clock())).total_seconds()
                if delay > 0:
                    sleeper(delay)
                metric = _run_one_tick(
                    index=index,
                    scheduled=scheduled,
                    store=store,
                    stage_callables=stage_callables,
                    counters=counters,
                    root=root,
                    clock=clock,
                    metrics_sink=metrics_sink,
                    gate_policy=_d_gate_policy,
                    metrics_leaf=_METRICS_LEAF,
                )
                executed_tick_ids.append(metric.tick_id)
                for job, reason in metric.receipts.items():
                    key = f"{job}:{reason}"
                    receipt_counts[key] = receipt_counts.get(key, 0) + 1
                # B-2: the G2-B-isomorphic green gate — every executed tick must
                # prove its three data receipts before the session may continue
                # or ever be reported a success.
                _require_b_green_receipts(metric)
                green_tick_count += 1
            else:
                # The loop ran the full cap without self-terminating.
                stop_reason = "tick_cap_reached"
        except G2AStopError as stop:
            stop_reason = stop.code
            incomplete = stop.code in _INCOMPLETE_STOP_CODES
        if token_lost():
            # The latched in-process bearer loss dominates a degraded receipt
            # STOP: a stage may have absorbed the control signal into a failed
            # receipt, but the true cause is the lost bearer (§10.4, M-1).
            stop_reason = "token_lost_incomplete"
            incomplete = True
        missing_evidence = sum(
            1 for tick_id in executed_tick_ids if tick_id not in numeric_evidence
        )
        if missing_evidence:
            # An executed tick without its explicit numeric metric is an
            # instrumentation gap, not "no signal": the run may not look
            # complete (M-3).
            incomplete = True

        # A production-state mutation dominates any other outcome (§14.4).
        production_code = _reconcile_production(production_paths, before, None)
        production_ok = production_code is None
        if not production_ok:
            stop_reason = production_code
        end_wall = _aware_utc(clock())
        status = _final_status(incomplete, stop_reason, len(executed_tick_ids))
        _write_d_state(root, status, stop_reason, end_wall)
        return _build_report(
            run_id,
            utc_day,
            root,
            deps,
            executed_tick_ids,
            stop_reason,
            incomplete,
            production_ok,
            (end_wall - start_wall).total_seconds(),
            green_tick_count=green_tick_count,
            receipt_counts=receipt_counts,
            missing_evidence=missing_evidence,
        )
    except Exception as error:
        # A crash ends the single process INCOMPLETE, with no resume (§10.4).
        # The production invariant is re-measured even here (B-3) and the
        # masked exception type — the name only, never a message or value — is
        # recorded durably and in the report.
        end_wall = _aware_utc(clock())
        production_code = _reconcile_production(production_paths, before, "crashed")
        production_ok = production_code == "crashed"
        masked_exception = type(error).__name__
        _write_d_state(
            root,
            "INCOMPLETE",
            production_code,
            end_wall,
            exception_type=masked_exception,
        )
        return _build_report(
            run_id,
            utc_day,
            root,
            deps,
            executed_tick_ids,
            production_code,
            True,
            production_ok,
            (end_wall - start_wall).total_seconds(),
            green_tick_count=green_tick_count,
            receipt_counts=receipt_counts,
            missing_evidence=sum(
                1 for tick_id in executed_tick_ids if tick_id not in numeric_evidence
            ),
            masked_exception_type=masked_exception,
        )
    except BaseException as error:
        # KeyboardInterrupt/SystemExit are never swallowed: record the abort
        # durably (guarded, so recording can never mask the abort) and re-raise.
        try:
            production_code = _reconcile_production(production_paths, before, "aborted")
            _write_d_state(
                root,
                "INCOMPLETE",
                production_code,
                _aware_utc(clock()),
                exception_type=type(error).__name__,
            )
        except Exception:
            pass
        raise
    finally:
        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _final_status(incomplete: bool, stop_reason: str | None, tick_count: int) -> str:
    if incomplete:
        return "INCOMPLETE"
    if (
        stop_reason is not None
        and stop_reason.startswith("kr_not_open")
        and tick_count == 0
    ):
        # A zero-tick refusal (before_open/no_regular/...) never seals the day:
        # the rerun gate admits a prior REFUSED record without approval (M-2).
        return "REFUSED"
    if stop_reason in (None, "session_ended"):
        return "COMPLETED"
    return "STOPPED"


def _build_report(
    run_id,
    utc_day,
    root,
    deps,
    executed_tick_ids,
    stop_reason,
    incomplete,
    production_ok,
    duration_seconds,
    *,
    green_tick_count=0,
    receipt_counts=None,
    missing_evidence=0,
    masked_exception_type=None,
) -> G2DReport:
    started = _started_operations(root)
    wire = sum(count for op, count in started.items() if op in _WIRE_OPERATIONS)
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    dispositions: dict[str, int] = {}
    for tick_id in executed_tick_ids:
        for disposition in store.read_dispositions_for_tick(tick_id):
            action = disposition.action.value
            dispositions[action] = dispositions.get(action, 0) + 1
    # M-3: aggregate the analysis stage's explicit per-tick metric — the only
    # numeric evidence source; GLM projection rows are never counted.
    evidence = getattr(deps, "_g2d_numeric_evidence", None) or {}
    state_counts: dict[str, int] = {}
    trigger_counts: dict[str, int] = {}
    for tick_id in executed_tick_ids:
        payload = evidence.get(tick_id)
        if payload is None:
            continue
        for state, count in payload["numeric_state_counts"].items():
            state_counts[state] = state_counts.get(state, 0) + count
        for kind, count in payload["trigger_counts"].items():
            trigger_counts[kind] = trigger_counts.get(kind, 0) + count
    established = sum(
        count for state, count in state_counts.items() if state != _DATA_INCOMPLETE
    )
    return G2DReport(
        run_id=run_id,
        utc_day=utc_day.isoformat(),
        tick_count=len(executed_tick_ids),
        stop_reason=stop_reason,
        provider_started_count=_provider_started_count(root, utc_day),
        wire_attempt_count=wire,
        numeric_state_established_count=established,
        trigger_counts=MappingProxyType(dict(sorted(trigger_counts.items()))),
        disposition_counts=MappingProxyType(dict(sorted(dispositions.items()))),
        production_invariant_ok=production_ok,
        incomplete=incomplete,
        duration_seconds=duration_seconds,
        glm_calls=deps.glm_call.count,
        sol_calls=deps.sol_call.count,
        green_tick_count=green_tick_count,
        receipt_counts=MappingProxyType(dict(sorted((receipt_counts or {}).items()))),
        numeric_state_counts=MappingProxyType(dict(sorted(state_counts.items()))),
        numeric_evidence_missing_tick_count=missing_evidence,
        token_started_count=started.get("TOKEN", 0),
        masked_exception_type=masked_exception_type,
        per_operation=MappingProxyType(dict(sorted(started.items()))),
    )
