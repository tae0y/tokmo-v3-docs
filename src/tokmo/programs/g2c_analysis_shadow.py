"""G2-C one-shot scripted analysis shadow over the stored G2 MaterialPacks.

G2-C connects the existing Slice E analysis composition to the material the
G2 shadow root already holds: it runs exactly one barrier_runner→
analysis_runner pass (the same closures ``_production_stage_deps`` wires for
the production cadence) against the packs, receipts, and ops kernel that the
G2-A/G2-B runs stored, with *injected* scripted GLM/SOL byte providers.

Design invariants (owner: G2-C):

- **Existing stages only.**  The barrier and analysis stages are the untouched
  ``_production_stage_deps`` closures, so the Slice E durable batch caps,
  provider redaction projections, and SHADOW-channel enforcement apply
  unchanged.  G2-B's data-only stage order is not referenced or modified.
- **No live surface.**  Every live data/calendar dependency slot is filled
  with a refuse-on-touch callable; the GLM/SOL calls are caller-injected
  scripted bytes.  This module opens no socket, sends no Telegram message,
  writes no order, and schedules nothing.
- **Shadow root confinement.**  All reads and writes stay inside the same
  confined ``shadow/g2a-a-<utc-day>`` root the G2 runs own; the production
  kernel is never opened.
"""

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping

from tokmo.programs.g2a_shadow_runner import (
    _aware_utc, _confine_shadow_root, shadow_root, stable_run_id,
)
from tokmo.programs.integrated_fake_cadence import (
    IntegratedCadenceDepsV1, _production_stage_deps,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.session_gate import Market, SessionVerdict


class G2CForbiddenLiveCallError(RuntimeError):
    """A G2-C analysis shadow run reached a live data/calendar dependency."""


def _forbidden(*_args, **_kwargs):
    raise G2CForbiddenLiveCallError(
        "g2c analysis shadow must not touch live providers"
    )


@dataclass(frozen=True)
class _ScriptedSessionV1:
    """The barrier stage's session carrier for a caller-declared market set."""

    verdicts: tuple[SessionVerdict, ...]


class _TimedCall:
    """Count and time one injected model provider with the injected clock."""

    def __init__(self, call, clock):
        self.call = call
        self.clock = clock
        self.count = 0
        self.seconds = 0.0

    def __call__(self, body):
        started = _aware_utc(self.clock())
        try:
            return self.call(body)
        finally:
            self.count += 1
            self.seconds += (
                _aware_utc(self.clock()) - started
            ).total_seconds()


@dataclass(frozen=True)
class G2CReport:
    run_id: str
    utc_day: str
    tick_id: int
    run_state: str
    candidate_count: int
    glm_calls: int
    sol_calls: int
    disposition_counts: Mapping[str, int]
    total_seconds: float
    glm_seconds: float
    sol_seconds: float


def run_c_analysis_shadow(
    *, home: Path, utc_day: date, open_markets: frozenset[Market],
    glm_call: Callable, sol_call: Callable, clock: Callable,
) -> G2CReport:
    """Run one barrier→analysis pass over the stored shadow materials."""
    if type(utc_day) is not date:
        raise ValueError("invalid utc day")
    if (type(open_markets) is not frozenset or not open_markets
            or not open_markets <= frozenset(Market)):
        raise ValueError("invalid open markets")

    root = _confine_shadow_root(home, shadow_root(home, utc_day))
    glm = _TimedCall(glm_call, clock)
    sol = _TimedCall(sol_call, clock)
    stages = _production_stage_deps(root, IntegratedCadenceDepsV1(
        calendar_provider=_forbidden, holdings_provider_factory=_forbidden,
        quotes_provider_factory=_forbidden, orders_provider_factory=_forbidden,
        detail_provider_factory=_forbidden, glm_call=glm, sol_call=sol,
        regular_session_dates=frozenset(), clock=clock,
    ))
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")

    started = _aware_utc(clock())
    tick = tick_id_from_utc(started)
    session = _ScriptedSessionV1(tuple(
        SessionVerdict(
            market, market in open_markets,
            "open" if market in open_markets else "after_close", None,
        )
        for market in Market
    ))
    context = stages.barrier_runner(tick=tick, prior={"session": session})
    outcome = stages.analysis_runner(tick=tick, prior=context, ops_kernel=store)

    counts: dict[str, int] = {}
    for disposition in store.read_dispositions_for_tick(tick.tick_id):
        action = disposition.action.value
        counts[action] = counts.get(action, 0) + 1
    ended = _aware_utc(clock())
    return G2CReport(
        run_id=stable_run_id(utc_day), utc_day=utc_day.isoformat(),
        tick_id=tick.tick_id, run_state=outcome.state.value,
        candidate_count=outcome.candidate_count,
        glm_calls=glm.count, sol_calls=sol.count,
        disposition_counts=MappingProxyType(dict(sorted(counts.items()))),
        total_seconds=(ended - started).total_seconds(),
        glm_seconds=glm.seconds, sol_seconds=sol.seconds,
    )
