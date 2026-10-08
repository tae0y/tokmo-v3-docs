"""Rollout/rollback gates (Plan §23–§25): paused-first, atomic cutover."""

import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.programs.integrated_pulse import PulseDepsV1, pulse_main, run_pulse_once
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.values.operation_lane_registry import (
    LANE_GOVERNED_OPERATIONS,
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
    active_owner,
)
from tokmo.values.pulse_policy import PULSE_POLICY_V1

PROJECT_ROOT = Path(__file__).parents[1]


def _utc(text):
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


# --- paused-first ops artifacts (§9.1) ------------------------------------


def test_pulse_cron_json_is_a_paused_uninstalled_artifact():
    payload = json.loads(
        (PROJECT_ROOT / "ops" / "hermes-cron" / "tokmo-v3-pulse.json")
        .read_text())
    assert payload["enabled"] is False
    assert payload["paused"] is True
    assert payload["schedule"] == "* * * * *"
    assert payload["workdir"] is None
    assert payload["ai_analysis"] is False
    assert payload["telegram_alerts"] is False
    assert payload["order_write"] is False


def test_pulse_cron_json_policy_version_matches_the_active_runner():
    # §9.1: the metadata drift of the cadence JSON is not repeated — the
    # pulse cron JSON names exactly the policy the tools runner activates.
    payload = json.loads(
        (PROJECT_ROOT / "ops" / "hermes-cron" / "tokmo-v3-pulse.json")
        .read_text())
    spec = importlib.util.spec_from_file_location(
        "run_tokmo_pulse", PROJECT_ROOT / "tools" / "run_tokmo_pulse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.ACTIVE_PULSE_POLICY is PULSE_POLICY_V1
    assert payload["policy_version"] == module.ACTIVE_PULSE_POLICY.policy_version


def test_pulse_wrapper_uses_the_40s_child_timeout_envelope():
    text = (PROJECT_ROOT / "ops" / "hermes-cron" / "tokmo_v3_pulse.sh")
    content = text.read_text()
    assert "--timeout 40" in content
    assert "run_tokmo_pulse.py" in content
    assert str(PULSE_POLICY_V1.child_timeout_seconds) == "40"


def test_cadence_cron_artifacts_are_untouched():
    payload = json.loads(
        (PROJECT_ROOT / "ops" / "hermes-cron" / "tokmo-v3-cadence.json")
        .read_text())
    assert payload["name"] == "tokmo-v3-cadence-5m"
    assert payload["schedule"] == "*/5 * * * *"
    assert payload["script"] == "tokmo_v3_cadence.sh"
    wrapper = (PROJECT_ROOT / "ops" / "hermes-cron" / "tokmo_v3_cadence.sh")
    assert "--timeout 270" in wrapper.read_text()


# --- production refusal / fixture CLI (§9.1, G1) --------------------------


def test_production_pulse_main_requires_canonical_root_before_any_io(tmp_path, capsys):
    code = pulse_main([
        "--once", "--mode", "production",
        "--policy-version", "pulse-60s-v1",
        "--scheduled-for", "2026-08-10T01:00:00Z",
        "--root", str(tmp_path / "never-created"),
    ])
    captured = capsys.readouterr()
    assert code == 1
    assert "refusal=production_requires_canonical_root" in captured.err
    assert not (tmp_path / "never-created").exists()


def test_fixture_pulse_cli_runs_one_disabled_tick(tmp_path, capsys):
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "lookahead": {}, "holdings": None, "quotes": {}, "orders": {},
    }))
    root = tmp_path / "root"
    code = pulse_main([
        "--once", "--mode", "fixture",
        "--policy-version", "pulse-60s-v1",
        "--scheduled-for", "2026-08-10T01:00:00Z",
        "--fixture", str(fixture_path),
        "--root", str(root),
    ])
    captured = capsys.readouterr()
    assert code == 0
    summary = json.loads(captured.out.strip())
    assert summary["outcome"] == "SAVED"
    assert summary["wire_counts"].get("total", 0) == 0
    assert (root / "ops" / "pulse_tick_receipts.sqlite3").exists()


def test_fixture_cli_with_provider_flags_fails_closed_on_ownership(
        tmp_path, capsys):
    # Even through the CLI, claiming provider mode before the reviewed
    # registry cutover is a fail-closed owner conflict with wire zero.
    fixture_path = tmp_path / "fixture.json"
    fixture_path.write_text(json.dumps({
        "lookahead": {}, "holdings": None, "quotes": {}, "orders": {},
        "quotes_provider_enabled": True,
    }))
    code = pulse_main([
        "--once", "--mode", "fixture",
        "--policy-version", "pulse-60s-v1",
        "--scheduled-for", "2026-08-10T01:00:00Z",
        "--fixture", str(fixture_path),
        "--root", str(tmp_path / "root"),
    ])
    captured = capsys.readouterr()
    assert code == 2
    summary = json.loads(captured.out.strip())
    assert summary["outcome"] == "FAILED"
    assert summary["reason"] == "owner_conflict"
    assert summary["wire_counts"] == {}


# --- cutover boundary semantics (§8.3, §24) -------------------------------


CUTOVER_SLOT = _utc("2026-08-10T01:05:00Z")  # aligned to the 300s grid


def _registry_with_cutover(cutover_epoch):
    entries = {}
    for operation in sorted(LANE_GOVERNED_OPERATIONS):
        if operation in ("PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"):
            entries[operation] = OperationLaneV1(
                operation, OperationLane.PULSE_60S,
                OperationLane.CADENCE_300S, cutover_epoch)
        else:
            entries[operation] = OperationLaneV1(
                operation, OperationLane.CADENCE_300S, None, None)
    return OperationLaneRegistryV1(entries=entries)


def _minimal_deps(tmp_path, registry):
    return PulseDepsV1(
        clock=lambda: _utc("2026-08-10T01:05:01Z"),
        lookahead_reader=lambda market: (),
        holdings_reader=lambda: None,
        native_refs_reader=lambda: None,
        watchlist_symbols={},
        token_fetch=lambda: "token",
        quotes_fetch=lambda market, symbols: None,
        orders_page_fetch=lambda **kwargs: None,
        order_detail_fetch=lambda **kwargs: None,
        quote_store=LatestQuoteStore(
            tmp_path / "stores" / "latest_quotes.sqlite3",
            clock=lambda: _utc("2026-08-10T01:05:01Z")),
        ledger_store=None,
        scan_state_store=None,
        dialogue_store=_DialogueStub(tmp_path),
        dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda root: [],
        dialogue_cutoff=_utc("2026-08-10T01:05:00Z"),
        quotes_provider_enabled=True,
        execution_provider_enabled=True,
        lane_registry=registry,
    )


class _DialogueStub:
    def __init__(self, root): self.path = root / "stores/execution_dialogue.sqlite3"
    def list_outbox(self, *, status=None):
        return ()


def test_pulse_provider_mode_flips_exactly_at_the_cutover_epoch(tmp_path):
    cutover_epoch = int(CUTOVER_SLOT.timestamp())
    registry = _registry_with_cutover(cutover_epoch)
    before = run_pulse_once(
        root=tmp_path / "before", policy_version="pulse-60s-v1",
        scheduled_for_utc=_utc("2026-08-10T01:04:00Z"),
        observed_now_utc=_utc("2026-08-10T01:04:01Z"),
        deps=_minimal_deps(tmp_path / "before", registry))
    assert before.outcome == "FAILED"
    assert before.reason == "owner_conflict"
    after = run_pulse_once(
        root=tmp_path / "after", policy_version="pulse-60s-v1",
        scheduled_for_utc=CUTOVER_SLOT,
        observed_now_utc=_utc("2026-08-10T01:05:01Z"),
        deps=_minimal_deps(tmp_path / "after", registry))
    # Both markets fail closed on the empty lookahead (no provider config),
    # but ownership admits the stages: the refusal reason is gone.
    assert after.reason != "owner_conflict"


def test_rollback_registry_restores_the_300s_fallback_owner():
    cutover_epoch = int(CUTOVER_SLOT.timestamp())
    registry = _registry_with_cutover(cutover_epoch)
    entry = registry.entries["PRICE_BATCH"]
    # §25: rollback is the reviewed constant change back to the fallback —
    # semantically the pre-cutover owner for every governed operation.
    assert entry.fallback_owner is OperationLane.CADENCE_300S
    assert active_owner(
        entry, epoch=cutover_epoch - 300) is OperationLane.CADENCE_300S
    for operation, production_entry in OPERATION_LANE_REGISTRY_V1.entries.items():
        if operation == "CONDITIONAL_ORDERS":
            # Conditional-order SDS v0.4 §4.4: born Pulse-owned — active at any epoch.
            assert active_owner(
                production_entry, epoch=cutover_epoch) is OperationLane.PULSE_60S
            continue
        assert active_owner(
            production_entry, epoch=cutover_epoch) is OperationLane.CADENCE_300S


def test_rollback_preserves_pulse_data_stores(tmp_path):
    # §25.4: valid rows in the shared stores and the pulse receipts survive
    # a rollback untouched — rollback is owner/config only.
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    from tokmo.values.market_observations import TossCurrentPriceFact
    now = _utc("2026-08-10T01:05:01Z")
    store = LatestQuoteStore(
        tmp_path / "stores" / "latest_quotes.sqlite3", clock=lambda: now)
    saved = store.save_batch((TossCurrentPriceFact(
        source="toss-current-price-v1", symbol="005930", last_price="70000",
        currency_raw="KRW", timestamp=now, fetched_at=now),))
    assert saved.status == "SAVED"
    receipts = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: now)
    slot = _utc("2026-08-10T01:05:00Z")
    receipts.begin_tick("pulse-60s-v1", slot, int(slot.timestamp()) // 60,
                        observed_start=now, lag_seconds=1.0,
                        max_lag_seconds=15)
    # Rollback: nothing in the data path is deleted; both reads succeed.
    facts = store.read_latest(
        source="toss-current-price-v1", symbols=("005930",))
    assert facts[0].last_price == "70000"
    receipt = receipts.read_tick("pulse-60s-v1", slot)
    assert receipt is not None and receipt["status"] == "STARTED"


def test_production_registry_preserves_cadence_until_future_cutover():
    for operation, entry in OPERATION_LANE_REGISTRY_V1.entries.items():
        if operation in {"PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"}:
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is not None
        elif operation == "CONDITIONAL_ORDERS":
            # Conditional-order SDS v0.4 §4.4: born Pulse-owned, no fallback.
            assert entry.recurring_owner is OperationLane.PULSE_60S
            assert entry.fallback_owner is None
            assert entry.cutover_epoch_utc is None
        else:
            assert entry.recurring_owner is OperationLane.CADENCE_300S
            assert entry.cutover_epoch_utc is None
