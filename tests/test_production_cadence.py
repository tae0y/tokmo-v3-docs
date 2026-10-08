"""Stage 3-A/C/D: one production-shaped cadence tick in read-only shadow.

``run_production_cadence_once`` executes the fixed one-tick state machine —
lease -> canonical tick -> independent calendar refresh through admitted
wires -> one mask -> truthful skip or masked downstream -> durable terminal
receipt — with fixture dependencies, zero live I/O, and exact wire
accounting: every provider callback equals one STARTED kernel receipt.
"""

from datetime import date, datetime, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path

import pytest

from tokmo.programs import production_cadence
from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.production_cadence import (
    ProductionCadenceRefusal,
    run_production_cadence_once,
)
from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.order_scan_state import OrderScanStateStore
from tokmo.values.cadence_policy import CADENCE_POLICY_V1, cadence_tick_v1
from tokmo.values.open_market_mask import build_open_market_mask
from tokmo.values.production_cadence import CadenceOutcome
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market

POLICY = CADENCE_POLICY_V1.policy_version

# Monday 2026-08-03. KR regular 00:00-06:30Z (09:00-15:30 KST); US regular
# 13:30-20:00Z (09:30-16:00 New York, EDT).
KR_SESSION = ("2026-08-03T00:00:00Z", "2026-08-03T06:30:00Z")
US_SESSION = ("2026-08-03T13:30:00Z", "2026-08-03T20:00:00Z")

SLOT_BOTH_CLOSED = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
SLOT_US_OPEN = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
SLOT_KR_OPEN = datetime(2026, 8, 3, 5, 0, tzinfo=timezone.utc)


def fixture(**overrides):
    base = {
        "token": "fixture-secret-bearer",
        "calendar": {
            "KR": {"regular_start": KR_SESSION[0], "regular_end": KR_SESSION[1]},
            "US": {"regular_start": US_SESSION[0], "regular_end": US_SESSION[1]},
        },
        "accounts": [{
            "seed": "123", "label": "brokerage",
            "holdings": [
                {"symbol": "005930", "quantity": "5", "avg_price": "70000",
                 "currency": "KRW", "market_raw": "KR"},
                {"symbol": "AAPL", "quantity": "2", "avg_price": "180",
                 "currency": "USD", "market_raw": "US"},
            ],
        }],
        "quotes": {
            "AAPL": {"price": "181.25", "currency": "USD"},
            "005930": {"price": "70100", "currency": "KRW"},
        },
        "orders": {},
    }
    base.update(overrides)
    return base


def run_once(tmp_path, slot, *, fixture_data=None, observed=None, replay=False,
             lifecycle_sink=None):
    root = tmp_path / "shadow-root"
    observed = slot + timedelta(seconds=1) if observed is None else observed
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture() if fixture_data is None else fixture_data,
        clock=lambda: observed, lifecycle_sink=lifecycle_sink,
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=slot,
        observed_now_utc=observed, deps=deps, replay=replay,
    )
    return root, result, probe


def kernel_report(root, slot):
    store = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    return store.read_cadence_tick_report(POLICY, slot)


def receipt_row(root, slot):
    store = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3",
        clock=lambda: slot,
    )
    return store.read_tick(POLICY, slot)


def stage_status(result, stage):
    return {status.stage: status for status in result.stages}[stage]


def history_store(root):
    from tokmo.records.regular_session_history import RegularSessionHistoryStore

    return RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=lambda: SLOT_BOTH_CLOSED,
    )


def test_default_policy_keeps_market_regime_wire_closed(tmp_path):
    root, result, probe = run_once(tmp_path, SLOT_BOTH_CLOSED)
    stage = stage_status(result, "market_regime_shadow")
    assert (stage.status, stage.reason) == ("skipped", "policy_not_pinned")
    report = kernel_report(root, SLOT_BOTH_CLOSED)
    assert report["group_started"].get("DAILY_CANDLES", 0) == 0
    assert report["operation_started"].get("STOCK_CANDLES_DAILY", 0) == 0
    assert probe.calls.get("STOCK_CANDLES_DAILY", 0) == 0


class _SeedAuthority:
    """Test-only provenance stub for pre-populating history fixtures."""

    def verify_calendar_history_receipt(self, receipt, *, now):
        return None

    def read_settled_calendar_provenance(self, market, market_local_date):
        return {
            "native_identity": (
                f"calendar:{market.value}:{market_local_date.isoformat()}:0"
            ),
        }


def seed_history(store, market, local_date, session):
    store.append_settled(market, local_date, session, authority=_SeedAuthority())


# -- E1: both closed => only admitted calendar+token wires, truthful skip ----------


def test_both_closed_cold_cache_uses_only_admitted_calendar_wires(tmp_path):
    root, result, probe = run_once(tmp_path, SLOT_BOTH_CLOSED)
    assert result.outcome is CadenceOutcome.SKIPPED_CLOSED
    assert result.exit_code == 0
    assert result.open_markets == ()
    assert result.mask_digest is not None
    report = kernel_report(root, SLOT_BOTH_CLOSED)
    assert report["operation_started"] == {
        "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
    }
    assert probe.calls == {"TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1}
    for stage in ("holdings", "quotes", "trades", "barrier", "candidates"):
        assert stage_status(result, stage).status == "skipped"
    row = receipt_row(root, SLOT_BOTH_CLOSED)
    assert row["status"] == "SKIPPED_CLOSED"
    assert row["mask_digest"] == result.mask_digest
    assert row["local_dates"] == {"KR": "2026-08-03", "US": "2026-08-03"}


def test_duplicate_same_slot_is_truthful_duplicate_with_provider_zero(tmp_path):
    root = tmp_path / "shadow-root"
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    first = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_BOTH_CLOSED,
        observed_now_utc=observed, deps=deps,
    )
    assert first.outcome is CadenceOutcome.SKIPPED_CLOSED
    before = dict(probe.calls)
    second = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_BOTH_CLOSED,
        observed_now_utc=observed, deps=deps,
    )
    assert second.outcome is CadenceOutcome.DUPLICATE
    assert second.exit_code == 2
    assert probe.calls == before
    store = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3", clock=lambda: observed,
    )
    events = store.read_events(POLICY, SLOT_BOTH_CLOSED)
    assert [event["kind"] for event in events] == ["DUPLICATE"]
    # the first terminal receipt is preserved, not rewritten
    assert receipt_row(root, SLOT_BOTH_CLOSED)["status"] == "SKIPPED_CLOSED"


def test_warm_cache_closed_slot_spends_only_the_bounded_history_backfill(tmp_path):
    """M2: a warm-cache closed tick performs zero current-calendar wires; the
    remaining pinned calendar capacity funds at most one historical local
    date per market (TOKEN admitted once lazily for the history wires)."""
    root = tmp_path / "shadow-root"
    slot_b = SLOT_BOTH_CLOSED + timedelta(seconds=300)
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_BOTH_CLOSED,
        observed_now_utc=observed, deps=deps,
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=slot_b,
        observed_now_utc=slot_b + timedelta(seconds=1), deps=deps,
    )
    assert result.outcome is CadenceOutcome.SKIPPED_CLOSED
    report = kernel_report(root, slot_b)
    assert report["operation_started"] == {
        "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
    }
    history = history_store(root)
    assert history.read(Market.KR, date(2026, 8, 2))["session"] == "regular"
    assert history.read(Market.US, date(2026, 8, 2))["session"] == "regular"


def test_warm_cache_closed_slot_with_complete_history_performs_zero_wires(tmp_path):
    """The SDS §7 zero-wire contract in its M2 form: once twenty regular
    sessions are durably known for each market, a warm-cache closed tick
    performs zero wires and never acquires a token."""
    root = tmp_path / "shadow-root"
    slot_b = SLOT_BOTH_CLOSED + timedelta(seconds=300)
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_BOTH_CLOSED,
        observed_now_utc=observed, deps=deps,
    )
    history = history_store(root)
    for market in (Market.KR, Market.US):
        for offset in range(1, 21):
            seed_history(
                history, market, date(2026, 8, 3) - timedelta(days=offset),
                "regular",
            )
    warmed = dict(probe.calls)
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=slot_b,
        observed_now_utc=slot_b + timedelta(seconds=1), deps=deps,
    )
    assert result.outcome is CadenceOutcome.SKIPPED_CLOSED
    assert probe.calls == warmed  # cache hit: no calendar, and no token at all
    report = kernel_report(root, slot_b)
    assert report["total_started"] == 0


# -- E2/E3/E9: masked open flows with exact wire accounting ------------------------


def test_us_only_open_full_flow_is_masked_and_exactly_accounted(tmp_path):
    root, result, probe = run_once(tmp_path, SLOT_US_OPEN)
    assert result.outcome is CadenceOutcome.COMPLETE
    assert result.exit_code == 0
    assert result.open_markets == ("US",)
    for stage in ("calendar", "mask", "holdings", "snapshot", "projection",
                  "quotes", "trades", "barrier", "sink"):
        assert stage_status(result, stage).status == "ok", stage
    assert stage_status(result, "candidates").status == "skipped"
    assert stage_status(result, "candidates").reason == "request_path_disabled"

    # canonical snapshot stays account-wide; projection/quotes are mask-only
    canonical = CurrentHoldingsStore(
        root / "stores" / "current_holdings.json",
        clock=lambda: SLOT_US_OPEN,
    ).read()
    symbols = {
        item.symbol for account in canonical.accounts for item in account.holdings
    }
    assert symbols == {"005930", "AAPL"}
    assert probe.quote_batches == [("AAPL",)]

    # E3: exact wire call counts equal STARTED receipts
    report = kernel_report(root, SLOT_US_OPEN)
    assert report["operation_started"] == probe.calls == {
        "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
        "ACCOUNTS": 1, "ACCOUNT_HOLDINGS": 1, "PRICE_BATCH": 1,
        "ORDERS_PAGE": 1,
    }
    assert report["state_counts"] == {"SUCCEEDED": report["total_started"]}

    # E9: the one mask digest is bound end to end
    row = receipt_row(root, SLOT_US_OPEN)
    assert row["status"] == "COMPLETE"
    assert row["mask_digest"] == result.mask_digest
    assert row["open_markets"] == ["US"]
    assert row["wire_counts"]["operation_started"] == dict(report["operation_started"])


def test_kr_only_and_both_open_masks_admit_exactly_their_markets(tmp_path):
    _root, result, probe = run_once(tmp_path, SLOT_KR_OPEN)
    assert result.outcome is CadenceOutcome.COMPLETE
    assert result.open_markets == ("KR",)
    assert probe.quote_batches == [("005930",)]

    both = fixture()
    both["calendar"]["US"] = {
        "regular_start": "2026-08-03T04:00:00Z",
        "regular_end": "2026-08-03T11:00:00Z",
    }
    _root, result, probe = run_once(
        tmp_path / "both", SLOT_KR_OPEN, fixture_data=both,
    )
    assert result.outcome is CadenceOutcome.COMPLETE
    assert result.open_markets == ("KR", "US")
    assert sorted(probe.quote_batches) == [("005930",), ("AAPL",)]
    assert probe.calls["ACCOUNTS"] == 1  # shared account providers execute once


def test_extended_session_fixture_reaches_unchanged_downstream_and_material(tmp_path):
    data = fixture()
    data["calendar"]["US"] = {
        "day_start": "2026-08-03T09:00:00Z",
        "day_end": "2026-08-03T11:00:00Z",
        "regular_start": US_SESSION[0],
        "regular_end": US_SESSION[1],
    }
    root, result, probe = run_once(tmp_path, SLOT_BOTH_CLOSED, fixture_data=data)
    assert result.outcome is CadenceOutcome.COMPLETE
    assert result.open_markets == ("US",)
    assert probe.calls["ACCOUNTS"] == 1
    assert probe.calls["PRICE_BATCH"] == 1
    assert probe.calls["ORDERS_PAGE"] == 1
    for stage in ("holdings", "quotes", "trades", "barrier"):
        assert stage_status(result, stage).status == "ok"
    assert stage_status(result, "candidates").status == "skipped"
    assert stage_status(result, "candidates").reason == "request_path_disabled"
    assert len(tuple((root / "ops" / "packs").rglob("*.json"))) == 3


# -- E5: lag/overlap/crash-reconcile/provider-exception/cache paths ----------------


def test_excess_lag_records_missed_with_provider_zero(tmp_path):
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=120)
    root, result, probe = run_once(
        tmp_path, SLOT_BOTH_CLOSED, observed=observed,
    )
    assert result.outcome is CadenceOutcome.MISSED
    assert result.exit_code == 2
    assert probe.calls == {}
    assert kernel_report(root, SLOT_BOTH_CLOSED) is None
    assert receipt_row(root, SLOT_BOTH_CLOSED)["status"] == "MISSED"


def test_explicit_replay_mode_admits_a_lagged_slot(tmp_path):
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=120)
    _root, result, _probe = run_once(
        tmp_path, SLOT_BOTH_CLOSED, observed=observed, replay=True,
    )
    assert result.outcome is CadenceOutcome.SKIPPED_CLOSED


def test_lock_contention_records_overlap_with_provider_zero(tmp_path):
    root = tmp_path / "shadow-root"
    lock_path = root / "runtime" / "cadence.lock"
    lock_path.parent.mkdir(parents=True)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, fcntl.LOCK_EX)
    try:
        observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
        deps, probe = build_fixture_cadence_deps(
            fixture=fixture(), clock=lambda: observed,
        )
        result = run_production_cadence_once(
            root=root, policy_version=POLICY,
            scheduled_for_utc=SLOT_BOTH_CLOSED, observed_now_utc=observed,
            deps=deps,
        )
        assert result.outcome is CadenceOutcome.OVERLAP
        assert result.exit_code == 2
        assert probe.calls == {}
        store = CadenceTickReceiptStore(
            root / "ops" / "cadence_ticks.sqlite3", clock=lambda: observed,
        )
        events = store.read_events(POLICY, SLOT_BOTH_CLOSED)
        assert [event["kind"] for event in events] == ["OVERLAP"]
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def test_next_invocation_reconciles_stale_started_state_without_new_wire(tmp_path):
    root = tmp_path / "shadow-root"
    slot_a = SLOT_BOTH_CLOSED
    slot_b = slot_a + timedelta(seconds=300)
    observed_a = slot_a + timedelta(seconds=1)
    # Simulate a crashed prior invocation: a STARTED kernel wire row and a
    # STARTED tick receipt row that never settled.
    kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    kernel.plan_cadence_requests(
        ProducerRole.COLLECTOR, CADENCE_POLICY_V1, scheduled_for_utc=slot_a,
        requests=(("ACCOUNTS", "accounts:tick0"),), now=observed_a,
    )
    kernel.start_cadence_request(
        ProducerRole.COLLECTOR, CADENCE_POLICY_V1, scheduled_for_utc=slot_a,
        operation="ACCOUNTS", native_identity="accounts:tick0", now=observed_a,
    )
    receipts = CadenceTickReceiptStore(
        root / "ops" / "cadence_ticks.sqlite3", clock=lambda: observed_a,
    )
    tick_a = cadence_tick_v1(POLICY, slot_a)
    receipts.begin_tick(
        POLICY, slot_a, tick_a.tick_id, observed_start=observed_a,
        lag_seconds=1.0, max_lag_seconds=60,
    )

    observed_b = slot_b + timedelta(seconds=1)
    deps, _probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed_b,
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=slot_b,
        observed_now_utc=observed_b, deps=deps,
    )
    assert result.outcome is CadenceOutcome.SKIPPED_CLOSED
    assert kernel_report(root, slot_a)["state_counts"] == {"UNKNOWN": 1}
    assert receipt_row(root, slot_a)["status"] == "UNKNOWN"


def test_quotes_provider_exception_is_truthful_partial(tmp_path):
    root, result, probe = run_once(
        tmp_path, SLOT_US_OPEN, fixture_data=fixture(quotes_fail=True),
    )
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.exit_code == 2
    assert stage_status(result, "quotes").status == "failed"
    assert stage_status(result, "trades").status == "ok"
    report = kernel_report(root, SLOT_US_OPEN)
    assert report["state_counts"]["FAILED"] == 1
    assert receipt_row(root, SLOT_US_OPEN)["status"] == "PARTIAL"


def test_holdings_failure_blocks_required_fact_downstream(tmp_path):
    root, result, probe = run_once(
        tmp_path, SLOT_US_OPEN, fixture_data=fixture(accounts_fail=True),
    )
    assert result.outcome is CadenceOutcome.FAILED
    assert stage_status(result, "holdings").status == "failed"
    for stage in ("quotes", "trades", "barrier", "candidates"):
        assert stage_status(result, stage).status == "blocked", stage
    report = kernel_report(root, SLOT_US_OPEN)
    assert "PRICE_BATCH" not in report["operation_started"]
    assert "ORDERS_PAGE" not in report["operation_started"]


def test_corrupt_calendar_cache_closes_both_markets_with_wire_zero(tmp_path):
    root = tmp_path / "shadow-root"
    cache_path = root / "ops" / "calendar_cache.json"
    cache_path.parent.mkdir(parents=True)
    for directory in (root, cache_path.parent):
        os.chmod(directory, 0o700)
    cache_path.write_text("{corrupt")
    observed = SLOT_US_OPEN + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_US_OPEN,
        observed_now_utc=observed, deps=deps,
    )
    assert result.outcome is CadenceOutcome.FAILED
    assert result.open_markets == ()
    assert probe.calls == {}  # untrustworthy separation: zero fetches
    assert kernel_report(root, SLOT_US_OPEN)["total_started"] == 0


# -- E6: independent market failure isolation --------------------------------------


def test_kr_calendar_failure_does_not_poison_valid_us_judgment(tmp_path):
    broken = fixture()
    broken["calendar"]["KR"] = {"fail": True}
    root, result, probe = run_once(
        tmp_path, SLOT_US_OPEN, fixture_data=broken,
    )
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.exit_code == 2
    assert result.open_markets == ("US",)
    assert result.calendar == {"KR": "provider_failed", "US": "success"}
    # downstream ran for US only
    assert probe.quote_batches == [("AAPL",)]
    report = kernel_report(root, SLOT_US_OPEN)
    assert report["operation_started"]["CALENDAR_DAY_KR"] == 1
    assert report["state_counts"]["FAILED"] == 1  # the KR calendar wire


# -- E7: mask/tick mismatch at the orchestrator boundary => downstream zero --------


def test_mask_tick_mismatch_at_orchestrator_boundary_is_downstream_zero(
        tmp_path, monkeypatch):
    foreign_slot = SLOT_US_OPEN + timedelta(seconds=300)
    foreign_tick = cadence_tick_v1(POLICY, foreign_slot)

    def foreign_mask(*, policy_version, scheduled_for_utc, tick_id, verdicts,
                     local_dates):
        return build_open_market_mask(
            policy_version=policy_version, scheduled_for_utc=foreign_slot,
            tick_id=foreign_tick.tick_id, verdicts=verdicts,
            local_dates=local_dates,
        )

    monkeypatch.setattr(production_cadence, "build_open_market_mask", foreign_mask)
    root, result, probe = run_once(tmp_path, SLOT_US_OPEN)
    assert result.outcome is CadenceOutcome.FAILED
    assert result.reason == "mask_binding"
    downstream = {"ACCOUNTS", "ACCOUNT_HOLDINGS", "PRICE_BATCH",
                  "ORDERS_PAGE", "ORDER_DETAIL"}
    assert not downstream & set(probe.calls)


# -- E8: unknown / closed-market trade records pend with detail zero ---------------


def test_unknown_and_closed_market_orders_pend_without_detail_wires(tmp_path):
    data = fixture()
    data["orders"] = {"123": [
        {"order_id": "mystery-1", "symbol": "MYSTERY", "side": "SELL",
         "status": "FILLED", "quantity": "1", "currency": "EUR"},
        {"order_id": "kr-closed-1", "symbol": "005930", "side": "SELL",
         "status": "FILLED", "quantity": "1", "currency": "KRW"},
    ]}
    root, result, _probe = run_once(tmp_path, SLOT_US_OPEN, fixture_data=data)
    assert result.outcome is CadenceOutcome.COMPLETE
    report = kernel_report(root, SLOT_US_OPEN)
    assert "ORDER_DETAIL" not in report["operation_started"]
    scan = OrderScanStateStore(
        root / "stores" / "order_scan_state.sqlite3",
        clock=lambda: SLOT_US_OPEN,
    )
    scope = next(iter(
        CurrentHoldingsStore(
            root / "stores" / "current_holdings.json",
            clock=lambda: SLOT_US_OPEN,
        ).read().identity_map
    ))
    pending = {row.order_id for row in scan.read_market_pending(scope)}
    assert "mystery-1" in pending  # never falls back to KR


# -- refusals (configuration/preflight, before any provider work) ------------------


def test_unknown_policy_and_noncanonical_slot_are_refusals(tmp_path):
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    with pytest.raises(ProductionCadenceRefusal):
        run_production_cadence_once(
            root=tmp_path / "shadow", policy_version="cadence-60s-vX",
            scheduled_for_utc=SLOT_BOTH_CLOSED, observed_now_utc=observed,
            deps=deps,
        )
    with pytest.raises(ProductionCadenceRefusal):
        run_production_cadence_once(
            root=tmp_path / "shadow", policy_version=POLICY,
            scheduled_for_utc=SLOT_BOTH_CLOSED + timedelta(seconds=7),
            observed_now_utc=observed, deps=deps,
        )
    assert probe.calls == {}


def test_production_canonical_root_is_refused(tmp_path):
    observed = SLOT_BOTH_CLOSED + timedelta(seconds=1)
    deps, _probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    with pytest.raises(ProductionCadenceRefusal):
        run_production_cadence_once(
            root=Path.home() / ".local" / "share" / "tokmo-v3",
            policy_version=POLICY, scheduled_for_utc=SLOT_BOTH_CLOSED,
            observed_now_utc=observed, deps=deps,
        )


# -- lifecycle sink: typed events only, default no-op, no secrets ------------------


def test_lifecycle_sink_receives_typed_events_without_secrets(tmp_path):
    events = []
    _root, result, _probe = run_once(
        tmp_path, SLOT_US_OPEN, lifecycle_sink=events.append,
    )
    assert result.outcome is CadenceOutcome.COMPLETE
    assert events, "lifecycle events expected"
    kinds = {event.kind for event in events}
    assert kinds <= {"tick_started", "stage_finished", "tick_finished"}
    for event in events:
        assert event.policy_version == POLICY
        assert "fixture-secret-bearer" not in repr(event)
    assert events[-1].kind == "tick_finished"


# -- M1: truthful terminal exceptions (GLM review repair) ---------------------------
# A normal Exception from any open stage may never strand the tick STARTED;
# the durable receipt terminates FAILED/PARTIAL per the required-stage
# contract, downstream is truthfully not run, and the lease is released.


def test_holdings_stage_exception_finishes_a_truthful_failed_tick(
        tmp_path, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("holdings receipt store exploded")

    monkeypatch.setattr(production_cadence, "run_holdings_sync", explode)
    root, result, probe = run_once(tmp_path, SLOT_US_OPEN)
    assert result.outcome is CadenceOutcome.FAILED
    assert result.reason == "holdings_required_fact_failed"
    assert result.exit_code == 2
    holdings = stage_status(result, "holdings")
    assert holdings.status == "failed"
    assert holdings.reason == "exception_RuntimeError"
    for stage in ("snapshot", "projection", "quotes", "trades",
                  "barrier", "candidates"):
        assert stage_status(result, stage).status == "blocked", stage
    row = receipt_row(root, SLOT_US_OPEN)
    assert row["status"] == "FAILED"  # terminal, never a stranded STARTED
    report = kernel_report(root, SLOT_US_OPEN)
    for downstream in ("ACCOUNTS", "PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
        assert downstream not in report["operation_started"]


def test_quotes_stage_exception_finishes_a_truthful_partial_tick(
        tmp_path, monkeypatch):
    def explode(**kwargs):
        raise RuntimeError("quote store exploded")

    monkeypatch.setattr(production_cadence, "run_quotes_sync", explode)
    root, result, _probe = run_once(tmp_path, SLOT_US_OPEN)
    assert result.outcome is CadenceOutcome.PARTIAL
    assert result.reason == "stage_failed_quotes"
    assert result.exit_code == 2
    quotes = stage_status(result, "quotes")
    assert quotes.status == "failed"
    assert quotes.reason == "exception_RuntimeError"
    # quotes is not a required fact for trades: the tick continues truthfully
    assert stage_status(result, "trades").status == "ok"
    assert receipt_row(root, SLOT_US_OPEN)["status"] == "PARTIAL"


def test_canonical_snapshot_read_exception_never_strands_the_tick(
        tmp_path, monkeypatch):
    from tokmo.records.current_holdings_store import CurrentHoldingsStore

    def explode(self):
        raise RuntimeError("canonical snapshot unreadable")

    monkeypatch.setattr(CurrentHoldingsStore, "read", explode)
    root, result, _probe = run_once(tmp_path, SLOT_US_OPEN)
    assert result.outcome is CadenceOutcome.PARTIAL
    quotes = stage_status(result, "quotes")
    assert quotes.status == "failed"
    assert quotes.reason == "exception_RuntimeError"
    for stage in ("trades", "barrier", "candidates"):
        assert stage_status(result, stage).status == "blocked", stage
    assert receipt_row(root, SLOT_US_OPEN)["status"] == "PARTIAL"


def test_token_fetch_exception_is_a_truthful_market_fail_closed(tmp_path):
    root, result, probe = run_once(
        tmp_path, SLOT_US_OPEN, fixture_data=fixture(token_fail=True),
    )
    assert result.outcome is CadenceOutcome.FAILED
    assert result.reason == "market_fail_closed"
    assert probe.calls == {"TOKEN": 1}
    row = receipt_row(root, SLOT_US_OPEN)
    assert row["status"] == "FAILED"
    report = kernel_report(root, SLOT_US_OPEN)
    assert report["state_counts"].get("STARTED") is None  # everything settled


def test_account_holdings_wire_exception_is_a_truthful_failed_tick(tmp_path):
    root, result, probe = run_once(
        tmp_path, SLOT_US_OPEN, fixture_data=fixture(holdings_fail=True),
    )
    assert result.outcome is CadenceOutcome.FAILED
    assert result.reason == "holdings_required_fact_failed"
    assert stage_status(result, "holdings").status == "failed"
    for stage in ("quotes", "trades", "barrier", "candidates"):
        assert stage_status(result, stage).status == "blocked", stage
    row = receipt_row(root, SLOT_US_OPEN)
    assert row["status"] == "FAILED"
    report = kernel_report(root, SLOT_US_OPEN)
    assert "PRICE_BATCH" not in report["operation_started"]
    assert report["state_counts"].get("STARTED") is None


# -- M2: regular-session history accumulation (GLM review repair) -------------------


def test_current_date_official_fact_is_appended_from_its_admitted_receipt(tmp_path):
    root, _result, _probe = run_once(tmp_path, SLOT_BOTH_CLOSED)
    history = history_store(root)
    for market in (Market.KR, Market.US):
        row = history.read(market, date(2026, 8, 3))
        assert row["session"] == "regular"
        assert row["source_identity"] == f"calendar:{market.value}:2026-08-03:0"
    # cold cache consumed the whole calendar allocation: backfill zero
    report = kernel_report(root, SLOT_BOTH_CLOSED)
    assert report["operation_started"] == {
        "TOKEN": 1, "CALENDAR_DAY_KR": 1, "CALENDAR_DAY_US": 1,
    }


def test_backfill_failure_defers_with_backoff_and_never_advances(tmp_path):
    data = fixture()
    data["calendar"]["KR"] = dict(data["calendar"]["KR"])
    data["calendar"]["KR"]["days"] = {"2026-08-02": {"fail": True}}
    root = tmp_path / "shadow-root"
    slots = [SLOT_BOTH_CLOSED + timedelta(seconds=300 * n) for n in range(3)]
    for index, slot in enumerate(slots):
        # the third tick starts exactly on its slot so it falls strictly
        # inside the failed KR attempt's five-minute backoff window
        observed = slot + timedelta(seconds=1 if index < 2 else 0)
        deps, _probe = build_fixture_cadence_deps(
            fixture=data, clock=lambda observed=observed: observed,
        )
        result = run_production_cadence_once(
            root=root, policy_version=POLICY, scheduled_for_utc=slot,
            observed_now_utc=observed, deps=deps,
        )
        assert result.outcome is CadenceOutcome.SKIPPED_CLOSED

    history = history_store(root)
    assert history.read(Market.KR, date(2026, 8, 2)) is None  # never forged
    assert history.read(Market.US, date(2026, 8, 2))["session"] == "regular"
    # tick 2 consumed KR ordinal 0 (FAILED); tick 3 falls inside the 5-minute
    # backoff so KR performs wire zero while US continues its own walk.
    second = kernel_report(root, slots[1])
    assert second["operation_started"].get("CALENDAR_DAY_KR") == 1
    third = kernel_report(root, slots[2])
    assert "CALENDAR_DAY_KR" not in third["operation_started"]
    assert third["operation_started"].get("CALENDAR_DAY_US") == 1


def test_open_tick_with_bootstrapped_history_serves_real_trade_windows(tmp_path):
    """The production session-date source reads only the durable history."""
    import dataclasses

    from tokmo.records.regular_session_history import HistorySessionDatesV1

    root = tmp_path / "shadow-root"
    history = history_store(root)
    for market in (Market.KR, Market.US):
        for offset in range(1, 30):
            day = date(2026, 8, 3) - timedelta(days=offset)
            seed_history(
                history, market, day,
                "regular" if day.weekday() < 5 else "no_regular",
            )
    observed = SLOT_US_OPEN + timedelta(seconds=1)
    deps, _probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    deps = dataclasses.replace(
        deps, regular_session_dates=HistorySessionDatesV1(history),
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_US_OPEN,
        observed_now_utc=observed, deps=deps,
    )
    assert result.outcome is CadenceOutcome.COMPLETE
    assert stage_status(result, "trades").status == "ok"


def test_corrupt_calendar_cache_skips_history_sync_entirely(tmp_path):
    root = tmp_path / "shadow-root"
    cache_path = root / "ops" / "calendar_cache.json"
    cache_path.parent.mkdir(parents=True)
    for directory in (root, cache_path.parent):
        os.chmod(directory, 0o700)
    cache_path.write_text("{corrupt")
    observed = SLOT_US_OPEN + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture(), clock=lambda: observed,
    )
    result = run_production_cadence_once(
        root=root, policy_version=POLICY, scheduled_for_utc=SLOT_US_OPEN,
        observed_now_utc=observed, deps=deps,
    )
    assert probe.calls == {}  # untrustworthy separation: no history wire either
    history = history_store(root)
    assert not history.path.exists()
    assert stage_status(result, "history").status == "skipped"


def test_no_secret_account_or_symbol_reaches_the_durable_receipt_store(tmp_path):
    root, _result, _probe = run_once(tmp_path, SLOT_US_OPEN)
    raw = (root / "ops" / "cadence_ticks.sqlite3").read_bytes()
    for banned in (b"fixture-secret-bearer", b"AAPL", b"005930", b"toss-acct-"):
        assert banned not in raw
    row = receipt_row(root, SLOT_US_OPEN)
    assert row["local_dates"] == {"KR": "2026-08-03", "US": "2026-08-03"}
    assert json.dumps(row["stages"])  # stage statuses are fixed tokens only
