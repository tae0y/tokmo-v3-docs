"""Stage 4 A: 576-tick virtual production replay (SDS §11.2).

The replay harness drives the identical ``run_production_cadence_once``
production function — never a duplicate orchestrator — across a controlled
48-hour timeline at the active policy cadence, with explicit official
KR/US calendar fixture facts (weekend, weekday, one per-market holiday,
market-local rollovers, independent sessions, a non-grid-aligned special
session, a both-open overlap) and injected faults (cold/corrupt cache,
provider failure/retry/backoff, duplicate slot, overlap, crash/reconcile,
excess lag, unknown-trade quarantine). It produces one durable
machine-readable report whose invariant assertions fail the run on any
contract violation.
"""

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.programs.virtual_replay import (
    DEFAULT_48H_START,
    SCENARIO_BUILDERS,
    ReplayReportPathError,
    build_default_48h_scenario,
    build_smoke_scenario,
    run_virtual_replay,
    write_replay_report,
)
from tokmo.values.cadence_policy import CADENCE_POLICY_V1

UTC = UTC
POLICY = "cadence-300s-v1"


def slot_at(index: int) -> datetime:
    return DEFAULT_48H_START + timedelta(seconds=300 * index)


# -- scenario shape ---------------------------------------------------------


def test_default_scenario_is_policy_derived_576_ticks():
    scenario = build_default_48h_scenario()
    assert scenario.name == "default-48h"
    assert scenario.policy_version == POLICY
    assert scenario.duration_seconds == 48 * 3600
    assert scenario.tick_count == (48 * 3600) // CADENCE_POLICY_V1.cadence_seconds
    assert scenario.tick_count == 576
    # the start slot is aligned to the policy grid and aware UTC
    assert scenario.start_slot_utc.utcoffset() == timedelta(0)
    assert int(scenario.start_slot_utc.timestamp()) % 300 == 0


def test_default_scenario_fixture_facts_are_explicit_and_independent():
    scenario = build_default_48h_scenario()
    kr_days = scenario.fixture["calendar"]["KR"]["days"]
    us_days = scenario.fixture["calendar"]["US"]["days"]
    # weekend: both markets officially no_regular on Sunday 2026-08-02
    assert kr_days["2026-08-02"] == {}
    assert us_days["2026-08-02"] == {}
    # weekday regular sessions
    assert kr_days["2026-08-03"]["regular_start"] == "2026-08-03T00:00:00Z"
    assert us_days["2026-08-04"]["regular_start"] == "2026-08-04T13:30:00Z"
    # one per-market weekday holiday: US Monday is officially no_regular
    assert us_days["2026-08-03"] == {}
    # non-grid-aligned official special session with a both-open overlap
    assert kr_days["2026-08-04"]["regular_start"] == "2026-08-04T01:07:00Z"
    assert kr_days["2026-08-04"]["regular_end"] == "2026-08-04T14:07:00Z"
    # canonical fault plan covers every injected path exactly once each
    assert scenario.faults[30] == "corrupt_cache"
    assert scenario.faults[40] == "duplicate_after"
    assert scenario.faults[50] == "overlap"
    assert scenario.faults[60] == "excess_lag"
    assert scenario.faults[150] == "crash_quotes"
    # KR 08-04 calendar provider fails its first two admitted wire attempts;
    # the kernel retry/backoff ladder owns the actual retry slots
    assert scenario.calendar_failures["KR"] == {
        "local_date": "2026-08-04",
        "fail_attempts": 2,
    }
    # the unknown-market order that must quarantine (symbol not in holdings)
    orders = scenario.fixture["orders"]
    assert any(
        entry["symbol"] == "MYST01" for rows in orders.values() for entry in rows
    )


def test_scenario_registry_names_the_canonical_builders():
    assert set(SCENARIO_BUILDERS) == {"default-48h", "smoke-12"}


# -- smoke replay -----------------------------------------------------------


def test_smoke_scenario_replays_and_reports(tmp_path):
    scenario = build_smoke_scenario()
    assert scenario.tick_count == 12
    report = run_virtual_replay(tmp_path / "shadow", scenario)
    assert report["schema"] == "tokmo-virtual-replay-report-v1"
    assert report["scenario"] == "smoke-12"
    assert report["policy_version"] == POLICY
    assert report["cadence_seconds"] == 300
    assert report["planned_ticks"] == 12
    assert report["invoked"] == 12
    assert sum(report["outcomes"].values()) == 12
    # Sunday closed morning: every tick is a truthful skip
    assert report["outcomes"]["SKIPPED_CLOSED"] == 12
    assert report["per_stage"]["holdings"].get("skipped") == 12
    # closed ticks perform zero downstream provider starts
    assert report["proofs"]["closed_downstream_starts_zero"] is True
    assert report["wire"]["operation_started"].get("ACCOUNTS", 0) == 0
    assert report["failures"] == []
    assert all(entry["passed"] for entry in report["assertions"])
    # the fixture bearer secret never reaches the report
    assert scenario.fixture["token"] not in json.dumps(report)


# -- full 48h replay --------------------------------------------------------


@pytest.fixture(scope="module")
def full_replay(tmp_path_factory):
    root = tmp_path_factory.mktemp("replay48") / "shadow"
    scenario = build_default_48h_scenario()
    report = run_virtual_replay(root, scenario)
    return root, scenario, report


def test_full_replay_counts_are_policy_derived(full_replay):
    _root, scenario, report = full_replay
    assert report["planned_ticks"] == 576
    # one extra invocation: the injected duplicate of slot 40
    assert report["invoked"] == 577
    assert sum(report["outcomes"].values()) == 577
    assert report["started_slot_utc"] == "2026-08-02T14:30:00Z"
    assert report["runtime_seconds"] < 300  # fast enough for prelaunch


def kr_ladder(report):
    return [
        fetch["slot_index"]
        for fetch in report["calendar_current_date_fetches"]["KR"]
        if fetch["local_date"] == "2026-08-04"
    ]


def test_full_replay_outcome_ledger_matches_the_fault_plan(full_replay):
    _root, _scenario, report = full_replay
    outcomes = report["outcomes"]
    assert outcomes["MISSED"] == 1
    assert outcomes["DUPLICATE"] == 1
    assert outcomes["OVERLAP"] == 1
    assert outcomes["CRASHED"] == 1
    # corrupt cache (1) + every KR fail-closed tick from the first failed
    # 08-04 attempt until the ladder's success: never rewritten as success
    attempts = kr_ladder(report)
    assert outcomes["FAILED"] == 1 + (attempts[-1] - attempts[0])
    assert outcomes["COMPLETE"] > 200  # KR Monday + KR special Tuesday + US
    assert outcomes["SKIPPED_CLOSED"] > 250
    failed_reasons = {
        entry["slot_utc"]: entry["reason"]
        for entry in report["failures"]
        if entry["outcome"] == "FAILED"
    }
    iso = lambda index: slot_at(index).isoformat().replace("+00:00", "Z")
    for index in (30, *range(attempts[0], attempts[-1])):
        assert failed_reasons[iso(index)] == "market_fail_closed"


def test_full_replay_calendar_refresh_is_once_per_changed_local_date(full_replay):
    _root, _scenario, report = full_replay
    current = report["calendar_current_date_fetches"]
    # KR: cold 08-02 at slot 0, one rollover fetch at KR midnight (15:00Z)
    kr = [(f["local_date"], f["slot_index"]) for f in current["KR"]]
    assert kr[:2] == [("2026-08-02", 0), ("2026-08-03", 6)]
    # then 08-04: two injected failed attempts and one success, spaced by
    # the kernel's 5/15-minute ladder (one slot of settle-clock drift allowed)
    attempts = kr_ladder(report)
    assert len(attempts) == 3
    assert attempts[0] == 294  # KR local midnight 2026-08-03T15:00:00Z
    assert 1 <= attempts[1] - attempts[0] <= 2  # ~5 minutes
    assert 3 <= attempts[2] - attempts[1] <= 4  # ~15 minutes
    # US: cold 08-02, then exactly one fetch per market-local midnight
    assert [(f["local_date"], f["slot_index"]) for f in current["US"]] == [
        ("2026-08-02", 0),
        ("2026-08-03", 162),
        ("2026-08-04", 450),
    ]
    # every in-between slot performed KR calendar wire zero
    assert report["proofs"]["backoff_wire_zero_slots"] == [
        index for index in range(attempts[0], attempts[-1]) if index not in attempts
    ]


def test_full_replay_masks_admit_only_open_market_subjects(full_replay):
    _root, _scenario, report = full_replay
    assert report["proofs"]["quotes_open_market_only"] is True
    assert report["proofs"]["holdings_single_scan_per_tick"] is True
    assert report["proofs"]["canonical_snapshot_all_markets"] is True
    assert report["proofs"]["closed_downstream_starts_zero"] is True
    batches = report["quote_batch_samples"]
    # KR-only Monday session, both-open overlap, then US-only after the
    # non-grid KR special close at 14:07Z
    assert batches["kr_only"] == ["005930"]
    assert sorted(batches["both_open"]) == ["005930", "AAPL"]
    assert batches["us_only"] == ["AAPL"]


def test_full_replay_quarantines_unknown_trades_without_kr_fallback(full_replay):
    root, _scenario, report = full_replay
    assert report["proofs"]["unknown_trades_quarantined"] >= 1
    assert report["proofs"]["quarantined_detail_starts"] == 0

    from tokmo.facts.toss import derive_account_scope
    from tokmo.records.order_scan_state import OrderScanStateStore

    scan = OrderScanStateStore(
        root / "stores" / "order_scan_state.sqlite3",
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )
    pending = scan.read_market_pending(derive_account_scope("42"))
    assert any(row.order_id == "MYST-ORD-1" for row in pending)


def test_full_replay_duplicate_and_closed_ticks_perform_wire_zero(full_replay):
    _root, _scenario, report = full_replay
    assert report["proofs"]["duplicate_wire_delta_zero"] is True
    assert report["proofs"]["group_limits_respected"] is True
    # lazy token: at least one fully cached closed tick acquired no token
    assert report["proofs"]["cached_closed_tick_token_starts_zero"] is True


def test_full_replay_reconciles_the_crashed_slot_and_settles_all_wires(full_replay):
    root, _scenario, report = full_replay
    crash_iso = slot_at(150).isoformat().replace("+00:00", "Z")
    assert report["receipts"]["reconciled_unknown_slots"] == [crash_iso]
    assert report["proofs"]["no_started_wire_rows_at_end"] is True

    from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore

    receipts = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3",
        clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
    )
    row = receipts.read_tick(POLICY, slot_at(150))
    assert row["status"] == "UNKNOWN"
    assert row["reason"] == "reconciled_stale_started"
    # the overlap slot never started: events only, no receipt row
    assert receipts.read_tick(POLICY, slot_at(50)) is None
    assert [e["kind"] for e in receipts.read_events(POLICY, slot_at(50))] == [
        "OVERLAP",
    ]
    assert receipts.read_tick(POLICY, slot_at(60))["status"] == "MISSED"


def test_full_replay_accumulates_official_history_and_reports_lag(full_replay):
    root, _scenario, report = full_replay
    assert report["max_lag_seconds"] == 120.0
    assert report["history"]["KR"] >= 20
    assert report["history"]["US"] >= 20

    from tokmo.records.regular_session_history import (
        HistorySessionDatesV1,
        RegularSessionHistoryStore,
    )
    from tokmo.values.session_gate import Market

    dates = HistorySessionDatesV1(
        RegularSessionHistoryStore(
            root / "ops" / "regular_session_history.sqlite3",
            clock=lambda: datetime(2026, 8, 5, tzinfo=UTC),
        )
    )
    served = dates.regular_session_dates(Market.KR, date(2026, 8, 3), 20)
    assert len(served) == 20
    # official facts only: the US holiday Monday never appears as a US session
    us_served = dates.regular_session_dates(Market.US, date(2026, 8, 4), 20)
    assert date(2026, 8, 3) not in us_served


def test_full_replay_every_invariant_assertion_passed(full_replay):
    _root, _scenario, report = full_replay
    failed = [entry for entry in report["assertions"] if not entry["passed"]]
    assert failed == [], failed
    assert report["unverified"], "unverified paths must be reported honestly"
    assert "us_dst_spring_forward" in report["unverified"]
    assert "live_provider_transport" in report["unverified"]


# -- custom tiny policy boundary (SDS §11.1 injected values) ----------------


def test_custom_tiny_policy_rejects_quotes_n_plus_1_before_wire(tmp_path, monkeypatch):
    from types import MappingProxyType

    from tokmo.programs import production_cadence as cadence_module
    from tokmo.values import cadence_policy as policy_module
    from tokmo.values.cadence_policy import CadencePolicyV1, TickRequestLimitsV1

    tiny = CadencePolicyV1(
        policy_version="cadence-300s-tinytest",
        request_limits=TickRequestLimitsV1(quote_starts=1),
    )
    registry = MappingProxyType(
        {
            POLICY: policy_module.CADENCE_POLICY_V1,
            tiny.policy_version: tiny,
        }
    )
    monkeypatch.setattr(policy_module, "CADENCE_POLICY_REGISTRY", registry)
    monkeypatch.setattr(cadence_module, "CADENCE_POLICY_REGISTRY", registry)

    scenario = build_smoke_scenario(
        policy_version=tiny.policy_version,
        open_kr_symbols=201,
    )
    report = run_virtual_replay(tmp_path / "shadow", scenario)
    # 201 open KR symbols need two PRICE_BATCH wires per tick; the tiny
    # policy admits exactly one, so every tick's N+1 batch is rejected
    # before I/O: one wire per tick, quotes failed, tick truthfully PARTIAL.
    assert report["wire"]["operation_started"]["PRICE_BATCH"] == 12
    assert report["per_stage"]["quotes"].get("failed") == 12
    assert report["outcomes"].get("PARTIAL") == 12
    assert all(entry["passed"] for entry in report["assertions"])


# -- durable report ---------------------------------------------------------


def test_report_write_is_atomic_private_and_shadow_only(tmp_path):
    report = {"schema": "tokmo-virtual-replay-report-v1", "planned_ticks": 12}
    target = tmp_path / "reports" / "replay.json"
    write_replay_report(report, target)
    assert json.loads(target.read_text())["planned_ticks"] == 12
    assert oct(target.stat().st_mode & 0o777) == "0o600"
    assert [p.name for p in target.parent.iterdir()] == ["replay.json"]

    production = Path.home() / ".local" / "share" / "tokmo-v3" / "replay.json"
    with pytest.raises(ReplayReportPathError):
        write_replay_report(report, production)

    victim = tmp_path / "victim.json"
    victim.write_text("{}")
    link = tmp_path / "reports" / "link.json"
    link.symlink_to(victim)
    with pytest.raises(ReplayReportPathError):
        write_replay_report(report, link)
    assert victim.read_text() == "{}"


# -- exact production symbol / CLI invariant failure ------------------------


def test_replay_invokes_the_exact_production_symbol_once_per_slot(
    tmp_path, monkeypatch
):
    from tokmo.programs import production_cadence, virtual_replay

    # the replay's bound name IS the production function, not a duplicate
    assert (
        virtual_replay.run_production_cadence_once
        is production_cadence.run_production_cadence_once
    )

    calls = []
    real = virtual_replay.run_production_cadence_once

    def spy(**kwargs):
        calls.append(kwargs["scheduled_for_utc"])
        return real(**kwargs)

    monkeypatch.setattr(virtual_replay, "run_production_cadence_once", spy)
    scenario = build_smoke_scenario()
    report = run_virtual_replay(tmp_path / "shadow", scenario)
    assert report["invoked"] == 12
    assert calls == [
        scenario.start_slot_utc + timedelta(seconds=300 * index) for index in range(12)
    ]


def test_cli_exits_2_when_an_invariant_fails(tmp_path, monkeypatch, capsys):
    from tokmo.programs import virtual_replay

    failing = {
        "scenario": "smoke-12",
        "planned_ticks": 12,
        "invoked": 12,
        "outcomes": {"COMPLETE": 12},
        "assertions": [{"name": "broken", "passed": False, "detail": "boom"}],
    }
    monkeypatch.setattr(
        virtual_replay,
        "run_virtual_replay",
        lambda root, scenario, progress=None: failing,
    )
    report_path = tmp_path / "reports" / "replay.json"
    exit_code = virtual_replay.main(
        [
            "--scenario",
            "smoke-12",
            "--root",
            str(tmp_path / "shadow"),
            "--report",
            str(report_path),
        ]
    )
    assert exit_code == 2
    summary = json.loads(capsys.readouterr().out)
    assert summary["assertions_failed"] == 1
    assert json.loads(report_path.read_text())["assertions"][0]["name"] == "broken"
