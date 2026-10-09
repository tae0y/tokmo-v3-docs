"""Stage 4 A: 576-tick virtual production replay harness (SDS §11.2).

The harness drives the identical production ``run_production_cadence_once``
function — never a duplicate orchestrator, never the fake-day runner — at the
active policy cadence across a controlled 48-hour timeline. Calendar facts
are explicit official fixture entries per market and local date (weekends,
one weekday holiday per market, independent rollovers, a non-grid-aligned
special session with a both-open overlap); production code receives only
those facts and never a weekday rule. Injected faults exercise cold, corrupt
and stale cache, provider failure with the kernel retry/backoff ladder,
duplicate slot, lock overlap, crash/reconcile, excess start lag,
unknown-trade quarantine, and pinned-policy admission boundaries.

The run produces one durable machine-readable JSON report — planned versus
invoked ticks, terminal outcomes, per-stage status/latency, wire starts by
operation/group/child, receipt reconciliation, provider-zero proofs, and an
explicit unverified-path list — whose invariant assertions fail the replay
(nonzero CLI exit) on any contract violation. The CLI writes only to
explicit shadow-only paths; the production canonical data root is refused.
"""

import argparse
import copy
import dataclasses
import fcntl
import json
import os
import sys
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.production_cadence import run_production_cadence_once
from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.durability import fsync_directory
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values import cadence_policy as _policy_values
from tokmo.values.cadence_policy import CALENDAR_ATTEMPT_MAX, GROUP_LIMIT_FIELDS
from tokmo.values.calendar_refresh import market_local_date
from tokmo.values.session_gate import Market

REPLAY_REPORT_SCHEMA = "tokmo-virtual-replay-report-v1"

DEFAULT_48H_START = datetime(2026, 8, 2, 14, 30, tzinfo=UTC)

FAULT_TOKENS = frozenset(
    {
        "corrupt_cache",
        "duplicate_after",
        "overlap",
        "excess_lag",
        "crash_quotes",
    }
)

_DOWNSTREAM_OPERATIONS = (
    "ACCOUNTS",
    "ACCOUNT_HOLDINGS",
    "PRICE_BATCH",
    "ORDERS_PAGE",
    "ORDER_DETAIL",
)

# Paths the virtual replay knowingly does not exercise; reported honestly
# instead of silently claiming coverage (SDS §11.2/§11.3).
_UNVERIFIED_PATHS = (
    "us_dst_spring_forward",
    "us_dst_fall_back",
    "policy_60s_cutover",
    "live_provider_transport",
    "real_scheduler_wrapper_timeout",
)


class ReplayReportPathError(ValueError):
    """The replay report may land only on an explicit shadow-only path."""


class _InjectedCrash(BaseException):
    """Hard mid-tick abort (not an ``Exception``): the production contract
    leaves STARTED rows for the next invocation's reconcile."""


def _registry():
    return _policy_values.CADENCE_POLICY_REGISTRY


@dataclass(frozen=True)
class VirtualReplayScenarioV1:
    """One deterministic replay timeline over explicit official facts."""

    name: str
    policy_version: str
    start_slot_utc: datetime
    duration_seconds: int
    cadence_seconds: int
    fixture: Mapping
    faults: Mapping[int, str] = field(default_factory=dict)
    # market value -> {"local_date": ISO date, "fail_attempts": int}: the
    # first N admitted wire attempts for that market/local-date fail, so the
    # kernel's retry/backoff ladder decides the actual attempt slots.
    calendar_failures: Mapping[str, Mapping] = field(default_factory=dict)
    expect: Mapping = field(default_factory=dict)

    def __post_init__(self):
        slot = self.start_slot_utc
        if (
            not isinstance(slot, datetime)
            or slot.tzinfo is None
            or slot.utcoffset() != timedelta(0)
            or slot.microsecond != 0
        ):
            raise ValueError("scenario start must be aware UTC")
        if int(slot.timestamp()) % self.cadence_seconds != 0:
            raise ValueError("scenario start is not on the policy cadence grid")
        if self.duration_seconds % self.cadence_seconds != 0:
            raise ValueError("scenario duration must be whole cadence slots")
        for index, token in self.faults.items():
            if type(index) is not int or not 0 <= index < self.tick_count:
                raise ValueError("fault slot index out of range")
            if token not in FAULT_TOKENS:
                raise ValueError("unknown fault token")
        for market_value, config in self.calendar_failures.items():
            if market_value not in ("KR", "US"):
                raise ValueError("unknown calendar failure market")
            date.fromisoformat(config["local_date"])
            if type(config["fail_attempts"]) is not int or not (
                1 <= config["fail_attempts"] < CALENDAR_ATTEMPT_MAX
            ):
                raise ValueError("invalid calendar failure attempt count")

    @property
    def tick_count(self) -> int:
        return self.duration_seconds // self.cadence_seconds


# -- official fixture facts (explicit per-market, per-local-date) -----------

_FIXTURE_FIRST_DAY = date(2026, 6, 20)
_FIXTURE_LAST_DAY = date(2026, 8, 7)

# Official no-regular fixture facts. The enumeration below authors explicit
# per-date fixture entries; production only ever sees the resulting facts.
_KR_HOLIDAYS = frozenset({date(2026, 7, 15)})
_US_HOLIDAYS = frozenset({date(2026, 7, 24), date(2026, 8, 3)})

# Official KR special session on Tuesday 2026-08-04: boundaries that do not
# align to the 300-second grid and overlap the US regular open (both-open).
_KR_SPECIAL_SESSIONS = {
    date(2026, 8, 4): ("T01:07:00Z", "T14:07:00Z"),
}


def _official_day_entry(market: Market, day: date) -> dict:
    if market is Market.KR and day in _KR_SPECIAL_SESSIONS:
        start_suffix, end_suffix = _KR_SPECIAL_SESSIONS[day]
        return {
            "regular_start": f"{day.isoformat()}{start_suffix}",
            "regular_end": f"{day.isoformat()}{end_suffix}",
        }
    holidays = _KR_HOLIDAYS if market is Market.KR else _US_HOLIDAYS
    if day.weekday() >= 5 or day in holidays:
        return {}
    if market is Market.KR:  # 09:00-15:30 KST
        return {
            "regular_start": f"{day.isoformat()}T00:00:00Z",
            "regular_end": f"{day.isoformat()}T06:30:00Z",
        }
    return {  # 09:30-16:00 New York, EDT across the fixture window
        "regular_start": f"{day.isoformat()}T13:30:00Z",
        "regular_end": f"{day.isoformat()}T20:00:00Z",
    }


def _official_days(market: Market) -> dict:
    days = {}
    cursor = _FIXTURE_FIRST_DAY
    while cursor <= _FIXTURE_LAST_DAY:
        days[cursor.isoformat()] = _official_day_entry(market, cursor)
        cursor += timedelta(days=1)
    return days


def _base_fixture() -> dict:
    return {
        "token": "fixture-bearer-virtual-replay",
        "calendar": {
            "KR": {"days": _official_days(Market.KR)},
            "US": {"days": _official_days(Market.US)},
        },
        "accounts": [
            {
                "seed": "42",
                "label": "brokerage",
                "holdings": [
                    {
                        "symbol": "005930",
                        "quantity": "5",
                        "avg_price": "70000",
                        "currency": "KRW",
                        "market_raw": "KR",
                    },
                    {
                        "symbol": "AAPL",
                        "quantity": "2",
                        "avg_price": "180",
                        "currency": "USD",
                        "market_raw": "US",
                    },
                ],
            }
        ],
        "quotes": {
            "005930": {"price": "70100", "currency": "KRW"},
            "AAPL": {"price": "181.25", "currency": "USD"},
        },
        "orders": {},
        "order_details": {},
    }


def build_default_48h_scenario() -> VirtualReplayScenarioV1:
    """The canonical prelaunch 48-hour replay: 576 policy-derived ticks."""
    policy = _registry()["cadence-300s-v1"]
    fixture = _base_fixture()
    fixture["orders"] = {
        "42": [
            {
                "order_id": "KR-ORD-1",
                "symbol": "005930",
                "side": "SELL",
                "status": "FILLED",
                "quantity": "1",
                "currency": "KRW",
                "filled_price": "70100",
                "filled_amount": "70100",
            },
            # Unsupported exact currency must remain durably market-pending and
            # never fall back to KR (SDS §3.6).
            {
                "order_id": "MYST-ORD-1",
                "symbol": "MYST01",
                "side": "SELL",
                "status": "FILLED",
                "quantity": "1",
                "currency": "EUR",
            },
        ]
    }
    fixture["order_details"] = {
        "KR-ORD-1": {
            "order_id": "KR-ORD-1",
            "symbol": "005930",
            "side": "SELL",
            "status": "FILLED",
            "quantity": "1",
            "currency": "KRW",
        },
    }
    return VirtualReplayScenarioV1(
        name="default-48h",
        policy_version=policy.policy_version,
        start_slot_utc=DEFAULT_48H_START,
        duration_seconds=48 * 3600,
        cadence_seconds=policy.cadence_seconds,
        fixture=fixture,
        faults={
            30: "corrupt_cache",
            40: "duplicate_after",
            50: "overlap",
            60: "excess_lag",
            150: "crash_quotes",
        },
        calendar_failures={
            "KR": {"local_date": "2026-08-04", "fail_attempts": 2},
        },
        expect={
            "outcomes": {"MISSED": 1, "DUPLICATE": 1, "OVERLAP": 1, "CRASHED": 1},
            "max_lag_seconds": 120.0,
            "quarantined_order_ids": ["MYST-ORD-1"],
            "history_min_sessions": 20,
            "cached_closed_zero_wire": True,
        },
    )


def build_smoke_scenario(
    *,
    policy_version: str = "cadence-300s-v1",
    open_kr_symbols: int | None = None,
) -> VirtualReplayScenarioV1:
    """A fast 12-tick scenario: closed Sunday morning by default, or an open
    KR Monday hour with ``open_kr_symbols`` KR holdings for injected-policy
    boundary runs."""
    policy = _registry()[policy_version]
    fixture = _base_fixture()
    if open_kr_symbols is None:
        start = datetime(2026, 8, 2, 5, 0, tzinfo=UTC)
        expect = {"outcomes": {"SKIPPED_CLOSED": 12}}
    else:
        start = datetime(2026, 8, 3, 5, 0, tzinfo=UTC)
        symbols = [f"9{index:05d}" for index in range(open_kr_symbols)]
        fixture["accounts"][0]["holdings"] = [
            {
                "symbol": symbol,
                "quantity": "1",
                "avg_price": "1000",
                "currency": "KRW",
                "market_raw": "KR",
            }
            for symbol in symbols
        ]
        fixture["quotes"] = {
            symbol: {"price": "1001", "currency": "KRW"} for symbol in symbols
        }
        expect = {}
    return VirtualReplayScenarioV1(
        name="smoke-12",
        policy_version=policy.policy_version,
        start_slot_utc=start,
        duration_seconds=12 * policy.cadence_seconds,
        cadence_seconds=policy.cadence_seconds,
        fixture=fixture,
        faults={},
        expect=expect,
    )


SCENARIO_BUILDERS = {
    "default-48h": build_default_48h_scenario,
    "smoke-12": build_smoke_scenario,
}


# -- replay execution -------------------------------------------------------


class _TickClock:
    """Injected per-invocation clock advancing one millisecond per reading,
    so stage latencies and store timestamps are simulated but monotonic."""

    def __init__(self, observed: datetime):
        self._now = observed

    def __call__(self) -> datetime:
        self._now += timedelta(milliseconds=1)
        return self._now


def _wrap_recording_deps(
    deps, index, recorders, *, crash_quotes: bool, calendar_failures: Mapping
):
    calendar_fetch = deps.calendar_fetch
    detail_fetch = deps.order_detail_fetch

    def recording_calendar(*, market, local_trade_date, receipt, bearer):
        # the attempt is recorded before the provider outcome so failed
        # wire attempts remain visible evidence
        recorders["calendar"].append(
            {
                "market": market.value,
                "local_date": local_trade_date.isoformat(),
                "slot_index": index,
            }
        )
        config = calendar_failures.get(market.value)
        if config is not None and config["local_date"] == local_trade_date.isoformat():
            key = (market.value, config["local_date"])
            attempt = recorders["fail_counts"].get(key, 0)
            recorders["fail_counts"][key] = attempt + 1
            if attempt < config["fail_attempts"]:
                raise RuntimeError("injected calendar provider failure")
        response = calendar_fetch(
            market=market,
            local_trade_date=local_trade_date,
            receipt=receipt,
            bearer=bearer,
        )
        # G2.5: calendar_fetch now returns the full CalendarResponse.
        # The refresh program expects a day object — extract .today.
        return response.today if hasattr(response, "today") else response

    def recording_detail(*, claim, bearer, account_scope, native_ref, order_id):
        recorders["details"].append(
            {
                "order_id": str(order_id),
                "slot_index": index,
            }
        )
        return detail_fetch(
            claim=claim,
            bearer=bearer,
            account_scope=account_scope,
            native_ref=native_ref,
            order_id=order_id,
        )

    replacements = {
        "calendar_fetch": recording_calendar,
        "order_detail_fetch": recording_detail,
    }
    if crash_quotes:

        def crashing_quotes(*, claim, bearer, symbols):
            del claim, bearer, symbols
            raise _InjectedCrash("injected mid-tick crash")

        replacements["quotes_fetch"] = crashing_quotes
    return dataclasses.replace(deps, **replacements)


def _plain_wire(wire) -> dict:
    return {
        key: (dict(value) if isinstance(value, Mapping) else value)
        for key, value in dict(wire).items()
    }


def _kernel_wire(root: Path, policy_version: str, slot: datetime) -> dict:
    report = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3"
    ).read_cadence_tick_report(policy_version, slot)
    if report is None:
        return {
            "total_started": 0,
            "group_started": {},
            "operation_started": {},
            "state_counts": {},
        }
    return {
        "total_started": report["total_started"],
        "group_started": dict(report["group_started"]),
        "operation_started": dict(report["operation_started"]),
        "state_counts": dict(report["state_counts"]),
    }


def _slot_iso(slot: datetime) -> str:
    return slot.astimezone(UTC).isoformat().replace("+00:00", "Z")


def run_virtual_replay(
    root: Path,
    scenario: VirtualReplayScenarioV1,
    *,
    progress: Callable[[str], None] | None = None,
) -> dict:
    """Replay the scenario through the exact production tick function and
    return the machine-readable report with evaluated invariant assertions."""
    started_wall = time.monotonic()
    root = Path(root)
    policy = _registry()[scenario.policy_version]
    fixture = copy.deepcopy(dict(scenario.fixture))
    cadence = timedelta(seconds=scenario.cadence_seconds)
    cache_path = root / "ops" / "calendar_cache.json"

    recorders = {"calendar": [], "details": [], "fail_counts": {}}
    ticks: list[dict] = []
    extra_invocations: list[dict] = []
    duplicate_wire_deltas: list[int] = []
    quote_batches: dict[int, list[tuple[str, ...]]] = {}

    def invoke(index, slot, observed, *, crash_quotes=False):
        deps, probe = build_fixture_cadence_deps(
            fixture=fixture,
            clock=_TickClock(observed),
        )
        deps = _wrap_recording_deps(
            deps,
            index,
            recorders,
            crash_quotes=crash_quotes,
            calendar_failures=scenario.calendar_failures,
        )
        result = run_production_cadence_once(
            root=root,
            policy_version=scenario.policy_version,
            scheduled_for_utc=slot,
            observed_now_utc=observed,
            deps=deps,
        )
        return result, probe

    for index in range(scenario.tick_count):
        slot = scenario.start_slot_utc + cadence * index
        fault = scenario.faults.get(index)
        observed = slot + timedelta(
            seconds=120 if fault == "excess_lag" else 1,
        )

        saved_cache = None
        if fault == "corrupt_cache":
            saved_cache = cache_path.read_bytes()
            cache_path.write_text("virtual-replay-injected-corruption")

        overlap_descriptor = None
        if fault == "overlap":
            runtime = root / "runtime"
            runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
            overlap_descriptor = os.open(
                runtime / "cadence.lock",
                os.O_RDWR | os.O_CREAT,
                0o600,
            )
            fcntl.flock(overlap_descriptor, fcntl.LOCK_EX)

        record = {"index": index, "slot_iso": _slot_iso(slot)}
        try:
            if fault == "crash_quotes":
                try:
                    invoke(index, slot, observed, crash_quotes=True)
                except _InjectedCrash:
                    record.update(
                        outcome="CRASHED",
                        reason="injected_crash",
                        lag=1.0,
                        wire=None,
                        stages=(),
                        open_markets=(),
                    )
                else:  # pragma: no cover - the injected crash must escape
                    raise AssertionError("injected crash did not propagate")
            else:
                result, probe = invoke(index, slot, observed)
                record.update(
                    outcome=result.outcome.value,
                    reason=result.reason,
                    lag=result.lag_seconds,
                    wire=_plain_wire(result.wire),
                    stages=result.stages,
                    open_markets=result.open_markets,
                )
                if probe.quote_batches:
                    quote_batches[index] = list(probe.quote_batches)
        finally:
            if overlap_descriptor is not None:
                fcntl.flock(overlap_descriptor, fcntl.LOCK_UN)
                os.close(overlap_descriptor)
            if saved_cache is not None:
                cache_path.write_bytes(saved_cache)
        ticks.append(record)

        if fault == "duplicate_after":
            before = _kernel_wire(root, scenario.policy_version, slot)
            duplicate_result, _probe = invoke(
                index,
                slot,
                slot + timedelta(seconds=2),
            )
            after = _kernel_wire(root, scenario.policy_version, slot)
            duplicate_wire_deltas.append(
                after["total_started"] - before["total_started"],
            )
            extra_invocations.append(
                {
                    "index": index,
                    "slot_iso": _slot_iso(slot),
                    "outcome": duplicate_result.outcome.value,
                    "reason": duplicate_result.reason,
                }
            )
        if progress is not None and index % 96 == 0:
            progress(f"replay {scenario.name} tick {index}/{scenario.tick_count}")

    report = _aggregate(
        root=root,
        scenario=scenario,
        policy=policy,
        ticks=ticks,
        extra_invocations=extra_invocations,
        duplicate_wire_deltas=duplicate_wire_deltas,
        quote_batches=quote_batches,
        recorders=recorders,
        runtime_seconds=time.monotonic() - started_wall,
    )
    return report


# -- aggregation and invariant evaluation -----------------------------------


def _symbol_markets(fixture: Mapping) -> dict:
    markets = {}
    for account in fixture.get("accounts", ()):
        for item in account.get("holdings", ()):
            markets[item["symbol"]] = item.get("market_raw")
    return markets


def _batch_samples(ticks, quote_batches) -> dict:
    samples = {}
    keys = {("KR",): "kr_only", ("KR", "US"): "both_open", ("US",): "us_only"}
    for record in ticks:
        open_markets = tuple(record.get("open_markets") or ())
        key = keys.get(open_markets)
        batches = quote_batches.get(record["index"])
        if key is None or key in samples or not batches:
            continue
        samples[key] = sorted({symbol for batch in batches for symbol in batch})
    return samples


def _history_counts(root: Path, fixture: Mapping) -> dict:
    counts = {market.value: 0 for market in Market}
    path = root / "ops" / "regular_session_history.sqlite3"
    if not path.exists():
        return counts
    store = RegularSessionHistoryStore(
        path,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    for market in Market:
        days = fixture["calendar"][market.value].get("days", {})
        for day_iso in days:
            row = store.read(market, date.fromisoformat(day_iso))
            if row is not None and row["session"] == "regular":
                counts[market.value] += 1
    return counts


def _market_pending_rows(root: Path):
    path = root / "stores" / "order_scan_state.sqlite3"
    if not path.exists():
        return ()
    store = OrderScanStateStore(
        path,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    return tuple(store.read_all_market_pending())


def _canonical_snapshot_markets(root: Path, symbol_markets: Mapping) -> set:
    path = root / "stores" / "current_holdings.json"
    if not path.exists():
        return set()
    state = CurrentHoldingsStore(
        path,
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    ).read()
    if state is None:
        return set()
    return {
        symbol_markets.get(item.symbol)
        for account in state.accounts
        for item in account.holdings
    }


def _aggregate(
    *,
    root,
    scenario,
    policy,
    ticks,
    extra_invocations,
    duplicate_wire_deltas,
    quote_batches,
    recorders,
    runtime_seconds,
) -> dict:
    outcomes: dict[str, int] = {}
    per_stage: dict[str, dict[str, int]] = {}
    stage_latency: dict[str, int] = {}
    wire_totals = {
        "total_started": 0,
        "group_started": {},
        "operation_started": {},
        "state_counts": {},
    }
    max_lag = 0.0
    failures = []
    crash_slots = []

    def add_counts(target: dict, source: Mapping) -> None:
        for key, value in source.items():
            target[key] = target.get(key, 0) + value

    for record in ticks:
        outcome = record["outcome"]
        outcomes[outcome] = outcomes.get(outcome, 0) + 1
        max_lag = max(max_lag, record["lag"])
        if outcome == "CRASHED":
            crash_slots.append(record)
            record["wire"] = _kernel_wire(
                root,
                scenario.policy_version,
                datetime.fromisoformat(record["slot_iso"].replace("Z", "+00:00")),
            )
        wire = record.get("wire")
        if wire is not None:
            wire_totals["total_started"] += wire["total_started"]
            add_counts(wire_totals["group_started"], wire["group_started"])
            add_counts(
                wire_totals["operation_started"],
                wire["operation_started"],
            )
            add_counts(wire_totals["state_counts"], wire["state_counts"])
        for status in record.get("stages") or ():
            per_stage.setdefault(status.stage, {})
            per_stage[status.stage][status.status] = (
                per_stage[status.stage].get(status.status, 0) + 1
            )
            stage_latency[status.stage] = max(
                stage_latency.get(status.stage, 0),
                status.latency_ms,
            )
        if outcome in ("FAILED", "PARTIAL", "CRASHED", "UNKNOWN"):
            failures.append(
                {
                    "slot_utc": record["slot_iso"],
                    "outcome": outcome,
                    "reason": record["reason"],
                }
            )
    for extra in extra_invocations:
        outcomes[extra["outcome"]] = outcomes.get(extra["outcome"], 0) + 1

    wire_totals["child_started"] = {
        operation: wire_totals["operation_started"].get(operation, 0)
        for operation in ("ORDERS_PAGE", "ORDER_DETAIL")
    }

    # current-date calendar attempts per market, in wire order
    current_fetches = {market.value: [] for market in Market}
    slot_of = {
        record["index"]: datetime.fromisoformat(
            record["slot_iso"].replace("Z", "+00:00"),
        )
        for record in ticks
    }
    for fetch in recorders["calendar"]:
        market = Market(fetch["market"])
        slot = slot_of[fetch["slot_index"]]
        if fetch["local_date"] == market_local_date(market, slot).isoformat():
            current_fetches[market.value].append(dict(fetch))

    # receipts and events, slot by slot
    receipts_store = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3",
        clock=lambda: datetime(2026, 1, 1, tzinfo=UTC),
    )
    terminal_by_status: dict[str, int] = {}
    event_counts = {"MISSED": 0, "DUPLICATE": 0, "OVERLAP": 0}
    reconciled_unknown = []
    unaccounted = []
    for record in ticks:
        slot = slot_of[record["index"]]
        row = receipts_store.read_tick(scenario.policy_version, slot)
        if row is not None:
            terminal_by_status[row["status"]] = (
                terminal_by_status.get(row["status"], 0) + 1
            )
            if (
                row["status"] == "UNKNOWN"
                and row["reason"] == "reconciled_stale_started"
            ):
                reconciled_unknown.append(record["slot_iso"])
        events = ()
        if row is None or record["index"] in scenario.faults:
            events = receipts_store.read_events(scenario.policy_version, slot)
            for event in events:
                event_counts[event["kind"]] += 1
        if row is None and not events:
            unaccounted.append(record["slot_iso"])

    symbol_markets = _symbol_markets(scenario.fixture)
    pending_rows = _market_pending_rows(root)
    pending_ids = {row.order_id for row in pending_rows}
    quarantined_detail_starts = sum(
        1 for detail in recorders["details"] if detail["order_id"] in pending_ids
    )

    # per-tick provider-zero and admission proofs
    closed_downstream_ok = True
    quotes_open_only = True
    holdings_single_scan = True
    group_limits_ok = True
    cached_closed_zero_wire = False
    limits = policy.request_limits
    for record in ticks:
        wire = record.get("wire")
        if wire is None:
            continue
        operations = wire["operation_started"]
        if record["outcome"] == "SKIPPED_CLOSED":
            if any(operations.get(op, 0) for op in _DOWNSTREAM_OPERATIONS):
                closed_downstream_ok = False
            if wire["total_started"] == 0:
                cached_closed_zero_wire = True
        if operations.get("ACCOUNTS", 0) > 1:
            holdings_single_scan = False
        if wire["total_started"] > limits.total_wire_starts:
            group_limits_ok = False
        for group, field_name in GROUP_LIMIT_FIELDS.items():
            if wire["group_started"].get(group, 0) > getattr(limits, field_name):
                group_limits_ok = False
        open_markets = set(record.get("open_markets") or ())
        for batch in quote_batches.get(record["index"], ()):
            for symbol in batch:
                if symbol_markets.get(symbol) not in open_markets:
                    quotes_open_only = False

    # backoff evidence: for injected calendar failures, wire attempts happen
    # only at kernel-eligible slots; blocked fail-closed slots between the
    # first attempt and the eventual success perform calendar wire zero
    backoff_blocked = []
    ladder_attempts = {}
    outcome_by_index = {record["index"]: record["outcome"] for record in ticks}
    for market_value, config in sorted(scenario.calendar_failures.items()):
        attempt_indices = [
            fetch["slot_index"]
            for fetch in current_fetches[market_value]
            if fetch["local_date"] == config["local_date"]
        ]
        ladder_attempts[market_value] = attempt_indices
        if not attempt_indices:
            continue
        for index in range(min(attempt_indices), max(attempt_indices)):
            if index not in attempt_indices and outcome_by_index.get(index) == "FAILED":
                backoff_blocked.append(index)

    # a crashed slot's rows must settle via the next invocation's reconcile
    no_started_left = True
    for record in crash_slots:
        final = _kernel_wire(
            root,
            scenario.policy_version,
            datetime.fromisoformat(record["slot_iso"].replace("Z", "+00:00")),
        )
        if final["state_counts"].get("STARTED", 0):
            no_started_left = False
    if wire_totals["state_counts"].get("STARTED", 0) and not crash_slots:
        no_started_left = False

    snapshot_markets = _canonical_snapshot_markets(root, symbol_markets)
    open_ticks = [rec for rec in ticks if rec.get("open_markets")]
    canonical_all_markets = (
        snapshot_markets >= set(symbol_markets.values()) if open_ticks else True
    )

    report = {
        "schema": REPLAY_REPORT_SCHEMA,
        "scenario": scenario.name,
        "policy_version": scenario.policy_version,
        "cadence_seconds": scenario.cadence_seconds,
        "planned_ticks": scenario.tick_count,
        "invoked": len(ticks) + len(extra_invocations),
        "started_slot_utc": _slot_iso(scenario.start_slot_utc),
        "ended_slot_utc": _slot_iso(
            scenario.start_slot_utc + timedelta(seconds=scenario.duration_seconds)
        ),
        "runtime_seconds": round(runtime_seconds, 3),
        "outcomes": outcomes,
        "per_stage": per_stage,
        "stage_latency_ms": stage_latency,
        "max_lag_seconds": max_lag,
        "wire": wire_totals,
        "calendar_current_date_fetches": current_fetches,
        "quote_batch_samples": _batch_samples(ticks, quote_batches),
        "receipts": {
            "terminal_by_status": terminal_by_status,
            "events": event_counts,
            "reconciled_unknown_slots": reconciled_unknown,
            "unaccounted_slots": unaccounted,
        },
        "history": _history_counts(root, scenario.fixture),
        "proofs": {
            "closed_downstream_starts_zero": closed_downstream_ok,
            "quotes_open_market_only": quotes_open_only,
            "holdings_single_scan_per_tick": holdings_single_scan,
            "canonical_snapshot_all_markets": canonical_all_markets,
            "group_limits_respected": group_limits_ok,
            "cached_closed_tick_token_starts_zero": cached_closed_zero_wire,
            "duplicate_wire_delta_zero": (
                all(delta == 0 for delta in duplicate_wire_deltas)
                if duplicate_wire_deltas
                else True
            ),
            "unknown_trades_quarantined": len(pending_rows),
            # Legacy report key retained as a pinned serialized shape. Its
            # value is the durable market-pending order-id set.
            "quarantined_order_ids": sorted(pending_ids),
            "quarantined_detail_starts": quarantined_detail_starts,
            "backoff_wire_zero_slots": backoff_blocked,
            "calendar_ladder_attempts": ladder_attempts,
            "no_started_wire_rows_at_end": no_started_left,
        },
        "failures": failures,
        "extra_invocations": extra_invocations,
        "fault_slots": {
            str(index): token for index, token in sorted(scenario.faults.items())
        },
        "unverified": list(_UNVERIFIED_PATHS),
    }
    report["assertions"] = _evaluate_invariants(report, scenario)
    return report


def _evaluate_invariants(report: Mapping, scenario) -> list:
    checks = []

    def check(name: str, passed: bool, detail: str) -> None:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})

    proofs = report["proofs"]
    check(
        "planned_ticks_policy_derived",
        report["planned_ticks"]
        == scenario.duration_seconds // scenario.cadence_seconds,
        f"planned={report['planned_ticks']}",
    )
    check(
        "no_replay_catch_up",
        report["invoked"] == report["planned_ticks"] + len(report["extra_invocations"]),
        f"invoked={report['invoked']}",
    )
    check(
        "all_planned_slots_accounted",
        not report["receipts"]["unaccounted_slots"],
        f"unaccounted={report['receipts']['unaccounted_slots']}",
    )
    fetch_counts: dict[tuple, int] = {}
    for market_value, fetches in report["calendar_current_date_fetches"].items():
        for fetch in fetches:
            key = (market_value, fetch["local_date"])
            fetch_counts[key] = fetch_counts.get(key, 0) + 1
    injected_dates = {
        (market_value, config["local_date"])
        for market_value, config in scenario.calendar_failures.items()
    }
    check(
        "calendar_refresh_once_per_changed_local_date",
        all(
            count <= CALENDAR_ATTEMPT_MAX if key in injected_dates else count == 1
            for key, count in fetch_counts.items()
        ),
        f"current_date_fetch_counts={sorted(fetch_counts.items())}",
    )
    for name in (
        "closed_downstream_starts_zero",
        "quotes_open_market_only",
        "holdings_single_scan_per_tick",
        "canonical_snapshot_all_markets",
        "group_limits_respected",
        "duplicate_wire_delta_zero",
        "no_started_wire_rows_at_end",
    ):
        check(name, proofs[name] is True, f"{name}={proofs[name]}")
    check(
        "quarantined_detail_starts_zero",
        proofs["quarantined_detail_starts"] == 0,
        f"quarantined_detail_starts={proofs['quarantined_detail_starts']}",
    )

    expect = dict(scenario.expect)
    for outcome, count in expect.get("outcomes", {}).items():
        check(
            f"expected_outcome_{outcome}",
            report["outcomes"].get(outcome, 0) == count,
            f"{outcome}={report['outcomes'].get(outcome, 0)} expected={count}",
        )
    if "max_lag_seconds" in expect:
        check(
            "expected_max_lag",
            report["max_lag_seconds"] == expect["max_lag_seconds"],
            f"max_lag={report['max_lag_seconds']}",
        )

    # kernel retry ladder: injected failures consume exactly fail_attempts
    # ordinals plus one success, spaced by the policy ladder (one slot of
    # settle-clock drift allowed), with every in-between slot wire zero
    slot_index_of = {}
    start_epoch = int(scenario.start_slot_utc.timestamp())
    for entry in report["failures"]:
        slot = datetime.fromisoformat(entry["slot_utc"].replace("Z", "+00:00"))
        index = (int(slot.timestamp()) - start_epoch) // scenario.cadence_seconds
        slot_index_of.setdefault(entry["outcome"], set()).add(index)
    ladder_window = set()
    for market_value, config in sorted(scenario.calendar_failures.items()):
        attempts = proofs["calendar_ladder_attempts"].get(market_value, [])
        ladder_ok = len(attempts) == config["fail_attempts"] + 1 and attempts == sorted(
            set(attempts)
        )
        for ordinal in range(1, len(attempts)):
            delay_slots = (
                _policy_values.CALENDAR_RETRY_DELAY_MINUTES[ordinal - 1]
                * 60
                // scenario.cadence_seconds
            )
            gap = attempts[ordinal] - attempts[ordinal - 1]
            if not delay_slots <= gap <= delay_slots + 1:
                ladder_ok = False
        check(
            f"calendar_retry_ladder_{market_value}",
            ladder_ok,
            f"attempts={attempts}",
        )
        if attempts:
            ladder_window.update(range(attempts[0], attempts[-1]))
    if scenario.calendar_failures or any(
        token == "corrupt_cache" for token in scenario.faults.values()
    ):
        corrupt_slots = {
            index
            for index, token in scenario.faults.items()
            if token == "corrupt_cache"
        }
        failed_slots = slot_index_of.get("FAILED", set())
        check(
            "failed_ticks_truthful",
            failed_slots == corrupt_slots | ladder_window,
            f"failed={sorted(failed_slots)} "
            f"expected={sorted(corrupt_slots | ladder_window)}",
        )
    if "quarantined_order_ids" in expect:
        pending = set(proofs["quarantined_order_ids"])
        check(
            "unknown_trades_quarantined_no_kr_fallback",
            proofs["unknown_trades_quarantined"] >= 1
            and set(expect["quarantined_order_ids"]) <= pending,
            f"pending={sorted(pending)}",
        )
    if "history_min_sessions" in expect:
        minimum = expect["history_min_sessions"]
        check(
            "history_backfill_reached_target",
            all(count >= minimum for count in report["history"].values()),
            f"history={report['history']}",
        )
    if expect.get("cached_closed_zero_wire"):
        check(
            "cached_closed_tick_token_starts_zero",
            proofs["cached_closed_tick_token_starts_zero"] is True,
            "lazy token: a fully cached closed tick performed wire zero",
        )
    crash_slots = [
        _slot_iso(
            scenario.start_slot_utc
            + timedelta(seconds=scenario.cadence_seconds * index)
        )
        for index, token in sorted(scenario.faults.items())
        if token == "crash_quotes"
    ]
    check(
        "crashed_slots_reconciled_unknown",
        report["receipts"]["reconciled_unknown_slots"] == crash_slots,
        f"reconciled={report['receipts']['reconciled_unknown_slots']}",
    )
    return checks


# -- durable report ---------------------------------------------------------


def _production_canonical_root() -> Path:
    return Path.home() / ".local" / "share" / "tokmo-v3"


def _ensure_safe_report_path(path: Path) -> Path:
    path = Path(path).absolute()
    production = _production_canonical_root().absolute()
    if path == production or production in path.parents:
        raise ReplayReportPathError(
            "replay report refuses the production canonical data root"
        )
    if path.is_symlink():
        raise ReplayReportPathError("replay report path may not be a symlink")
    if path.exists():
        status = os.lstat(path)
        if status.st_nlink != 1:
            raise ReplayReportPathError("replay report path may not be hardlinked")
    return path


def write_replay_report(report: Mapping, path: Path) -> None:
    """Atomically persist the replay report: private, fsynced, shadow-only."""
    path = _ensure_safe_report_path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    payload = json.dumps(dict(report), sort_keys=True, indent=1).encode()
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


# -- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """`python -m tokmo.programs.virtual_replay`: one scenario, one durable
    report on an explicit shadow-only path, one JSON summary line."""
    from tokmo.programs.production_cadence import ProductionCadenceRefusal

    parser = argparse.ArgumentParser(prog="tokmo-virtual-replay")
    parser.add_argument("--scenario", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--report", required=True)
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    def refuse(reason: str) -> int:
        print(f"tokmo-virtual-replay refusal={reason}", file=sys.stderr)
        return 1

    builder = SCENARIO_BUILDERS.get(args.scenario)
    if builder is None:
        return refuse("unknown_scenario")
    try:
        report_path = _ensure_safe_report_path(Path(args.report))
    except ReplayReportPathError:
        return refuse("unsafe_report_path")
    scenario = builder()
    try:
        report = run_virtual_replay(
            Path(args.root),
            scenario,
            progress=lambda line: print(line, file=sys.stderr),
        )
    except ProductionCadenceRefusal as error:
        return refuse(str(error).replace(" ", "_"))
    write_replay_report(report, report_path)
    failed = sum(1 for entry in report["assertions"] if not entry["passed"])
    print(
        json.dumps(
            {
                "scenario": report["scenario"],
                "planned_ticks": report["planned_ticks"],
                "invoked": report["invoked"],
                "outcomes": report["outcomes"],
                "assertions_failed": failed,
                "report_path": str(report_path),
            },
            sort_keys=True,
        )
    )
    return 0 if failed == 0 else 2


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
