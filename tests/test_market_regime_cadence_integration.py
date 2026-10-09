import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
from tokmo.programs.production_cadence import run_production_cadence_once
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.cadence_policy import (
    CADENCE_CORE_SHADOW_POLICY_V1,
    CADENCE_MARKET_PORTFOLIO_POLICY_V1,
    CADENCE_POLICY_REGISTRY,
    CADENCE_POLICY_V1,
    CADENCE_TARGETED_ONESHOT_POLICY_V1,
    GROUP_LIMIT_FIELDS,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
    policy_snapshot,
    validate_cadence_operation,
)
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
)
from tokmo.values.ops_kernel import (
    CadenceBindingError,
    CadenceBudgetExceeded,
    CadenceDuplicateError,
    CadencePolicyMismatchError,
    CadenceTransitionError,
    ProducerRole,
)
from tokmo.values.production_cadence import CADENCE_STAGE_ORDER
from tokmo.values.request_parity import REQUEST_OPERATION_SPECS
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_data import Candle, CandlePageResponse


def test_core_shadow_declares_17_17_0_and_shared_stage_contract():
    assert TickRequestLimitsV1().daily_candle_starts == 0
    assert TickRequestLimitsV1().market_daily_candle_starts == 0
    assert TickRequestLimitsV1().quant_daily_candle_starts == 0
    shared = CADENCE_POLICY_REGISTRY["cadence-300s-core-shadow-v1"]
    assert shared.request_limits.total_wire_starts == 44
    assert shared.request_limits.daily_candle_starts == 17
    assert shared.request_limits.market_daily_candle_starts == 17
    assert shared.request_limits.quant_daily_candle_starts == 0
    assert GROUP_LIMIT_FIELDS["DAILY_CANDLES"] == "daily_candle_starts"
    assert REQUEST_OPERATION_SPECS["STOCK_CANDLES_DAILY"] == (
        "GET",
        "/api/v1/candles",
        False,
    )
    assert (
        validate_cadence_operation(
            "STOCK_CANDLES_DAILY",
            "candles:mr:KR:005930:2026-08-08:1",
        )
        == "DAILY_CANDLES"
    )
    assert CADENCE_STAGE_ORDER[4] == "market_regime_shadow"
    assert (
        policy_snapshot(shared, TradesChildLimitsV1())["request_limits"][
            "daily_candle_starts"
        ]
        == 17
    )


def test_source_declared_daily_gate_is_equivalent_to_market_lane_gate():
    from tokmo.values.pulse_policy import PULSE_POLICY_V1

    policies = (
        CADENCE_POLICY_V1,
        CADENCE_CORE_SHADOW_POLICY_V1,
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_TARGETED_ONESHOT_POLICY_V1,
        PULSE_POLICY_V1,
    )
    assert all(
        (policy.request_limits.daily_candle_starts > 0)
        == (policy.request_limits.market_daily_candle_starts > 0)
        for policy in policies
    )
    assert all(
        policy.request_limits.quant_daily_candle_starts == 0 for policy in policies
    )


def test_real_executor_daily_budget_exhaustion_precedes_callback(tmp_path):
    from tokmo.programs.cadence_wire import CadenceWireExecutorV1
    from tokmo.records.ops_kernel import OpsKernelStore

    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    policy = CADENCE_POLICY_REGISTRY["cadence-300s-v1"]
    executor = CadenceWireExecutorV1(
        ops_kernel=OpsKernelStore(tmp_path / "ops.sqlite3"),
        role=ProducerRole.COLLECTOR,
        policy=policy,
        scheduled_for_utc=slot,
        clock=lambda: slot + timedelta(seconds=1),
        child_limits=TradesChildLimitsV1(),
        lane_registry=OPERATION_LANE_REGISTRY_V1,
        lane=OperationLane.CADENCE_300S,
    )
    calls = []
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute(
            "STOCK_CANDLES_DAILY",
            "candles:mr:KR:005930:2026-08-01:1",
            lambda claim: calls.append(claim),
        )
    assert calls == []


class _HistoryAuthority:
    def read_settled_calendar_provenance(self, market, local_date):
        return {
            "native_identity": f"calendar:{market.value}:{local_date.isoformat()}:0"
        }


def _fixture(candles):
    return {
        "calendar": {
            "KR": {
                "regular_start": "2026-08-03T00:00:00Z",
                "regular_end": "2026-08-03T06:30:00Z",
            },
            "US": {
                "regular_start": "2026-08-03T13:30:00Z",
                "regular_end": "2026-08-03T20:00:00Z",
            },
        },
        "accounts": (),
        "watchlist_symbols": {"KR": ("005930",)},
        "stock_candles": TossReadOutcome("success", CandlePageResponse(candles, None)),
    }


def _seed_history(root, clock):
    store = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=clock,
    )
    authority = _HistoryAuthority()
    for market in Market:
        for offset in range(1, 21):
            day = datetime(2026, 8, 3).date() - timedelta(days=offset)
            store.append_settled(market, day, "regular", authority=authority)


def test_active_production_path_runs_once_then_next_slot_reuses(monkeypatch, tmp_path):
    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    root = tmp_path / "shadow"
    candles = tuple(
        Candle(
            slot - timedelta(days=offset),
            "10",
            "12",
            "9",
            "11",
            "100",
            "KRW",
        )
        for offset in range(200)
    )
    _seed_history(root, lambda: slot)
    counts = {"normalize": 0, "core": 0}
    import tokmo.programs.market_regime_shadow as shadow

    real_normalize = shadow.normalize_daily_candles
    real_core = shadow.evaluate_market_regime_core_v1

    def normalize(*args, **kwargs):
        counts["normalize"] += 1
        return real_normalize(*args, **kwargs)

    def core(series):
        counts["core"] += 1
        return real_core(series)

    monkeypatch.setattr(shadow, "normalize_daily_candles", normalize)
    monkeypatch.setattr(shadow, "evaluate_market_regime_core_v1", core)
    policy = "cadence-300s-core-shadow-v1"
    deps1, probe1 = build_fixture_cadence_deps(
        fixture=_fixture(candles),
        clock=lambda: slot + timedelta(seconds=1),
    )
    first = run_production_cadence_once(
        root=root,
        policy_version=policy,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps1,
    )
    first_stage = {item.stage: item for item in first.stages}["market_regime_shadow"]
    assert (first_stage.status, first_stage.reason) == (
        "ok",
        "kr_1_of_1_us_0_of_0_pending_0",
    )
    assert probe1.calls["STOCK_CANDLES_DAILY"] == 1
    assert (counts["normalize"], counts["core"]) == (1, 1)
    next_slot = slot + timedelta(minutes=5)
    deps2, probe2 = build_fixture_cadence_deps(
        fixture=_fixture(candles),
        clock=lambda: next_slot + timedelta(seconds=1),
    )
    second = run_production_cadence_once(
        root=root,
        policy_version=policy,
        scheduled_for_utc=next_slot,
        observed_now_utc=next_slot + timedelta(seconds=1),
        deps=deps2,
    )
    second_stage = {item.stage: item for item in second.stages}["market_regime_shadow"]
    assert (second_stage.status, second_stage.reason) == (
        "skipped",
        "same_completed_session",
    )
    assert probe2.calls == {}
    assert (counts["normalize"], counts["core"]) == (1, 1)


def test_failed_identity_persists_and_next_invocation_skips_without_wire(tmp_path):
    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    root = tmp_path / "shadow"
    _seed_history(root, lambda: slot)
    policy = "cadence-300s-core-shadow-v1"
    deps1, probe1 = build_fixture_cadence_deps(
        fixture=_fixture(()),
        clock=lambda: slot + timedelta(seconds=1),
    )
    first = run_production_cadence_once(
        root=root,
        policy_version=policy,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps1,
    )
    first_stage = {item.stage: item for item in first.stages}["market_regime_shadow"]
    # Portfolio-drain semantics: a new per-symbol FAILED downgrades the stage
    # with the bounded numeric coverage reason, never the exception text.
    assert (first_stage.status, first_stage.reason) == (
        "failed",
        "kr_1_of_1_us_0_of_0_pending_0",
    )
    assert probe1.calls["STOCK_CANDLES_DAILY"] == 1
    shadow_db = root / "ops" / "market_regime_shadow.sqlite3"
    with closing(sqlite3.connect(shadow_db)) as connection:
        identity, state, failure_code = connection.execute(
            "SELECT identity_digest,state,failure_code FROM market_regime_shadow_claim"
        ).fetchone()
    assert (state, failure_code) == ("FAILED", "ValueError")

    next_slot = slot + timedelta(minutes=5)
    deps2, probe2 = build_fixture_cadence_deps(
        fixture=_fixture(()),
        clock=lambda: next_slot + timedelta(seconds=1),
    )
    second = run_production_cadence_once(
        root=root,
        policy_version=policy,
        scheduled_for_utc=next_slot,
        observed_now_utc=next_slot + timedelta(seconds=1),
        deps=deps2,
    )
    second_stage = {item.stage: item for item in second.stages}["market_regime_shadow"]
    # FAILED is terminal coverage for the session: pending drops to zero and
    # the next tick is a zero-wire same-session skip.
    assert (second_stage.status, second_stage.reason) == (
        "skipped",
        "same_completed_session",
    )
    assert probe2.calls.get("STOCK_CANDLES_DAILY", 0) == 0
    with closing(sqlite3.connect(shadow_db)) as connection:
        assert connection.execute(
            "SELECT state,failure_code FROM market_regime_shadow_claim "
            "WHERE identity_digest=?",
            (identity,),
        ).fetchone() == ("FAILED", "ValueError")


@pytest.mark.parametrize(
    ("error", "expected"),
    (
        (CadenceBudgetExceeded("x"), ("skipped", "admission_budget_exhausted")),
        (CadenceDuplicateError("x"), ("skipped", "admission_duplicate")),
        (CadencePolicyMismatchError("x"), ("skipped", "admission_policy_mismatch")),
        (CadenceBindingError("x"), ("failed", "admission_internal_error")),
        (CadenceTransitionError("x"), ("failed", "admission_internal_error")),
        (ValueError("validation"), ("failed", "admission_internal_error")),
    ),
)
def test_market_admission_failure_precedes_shadow_claim_and_provider(
    monkeypatch, tmp_path, error, expected
):
    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    root = tmp_path / "shadow"
    _seed_history(root, lambda: slot)
    deps, probe = build_fixture_cadence_deps(
        fixture=_fixture(()),
        clock=lambda: slot + timedelta(seconds=1),
    )
    from tokmo.programs.cadence_wire import CadenceWireExecutorV1

    original = CadenceWireExecutorV1.execute

    def reject(self, operation, native_identity, callback):
        if operation == "STOCK_CANDLES_DAILY":
            raise error
        return original(self, operation, native_identity, callback)

    monkeypatch.setattr(CadenceWireExecutorV1, "execute", reject)
    result = run_production_cadence_once(
        root=root,
        policy_version="cadence-300s-core-shadow-v1",
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )
    stage = {item.stage: item for item in result.stages}["market_regime_shadow"]
    assert (stage.status, stage.reason) == expected
    assert probe.calls.get("STOCK_CANDLES_DAILY", 0) == 0
    shadow_db = root / "ops" / "market_regime_shadow.sqlite3"
    with sqlite3.connect(shadow_db) as connection:
        assert connection.execute(
            "SELECT count(*) FROM market_regime_shadow_claim"
        ).fetchone() == (0,)


def test_after_admission_contention_consumes_failed_wire_without_provider(
    monkeypatch, tmp_path
):
    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    root = tmp_path / "shadow"
    _seed_history(root, lambda: slot)
    deps, probe = build_fixture_cadence_deps(
        fixture=_fixture(()),
        clock=lambda: slot + timedelta(seconds=1),
    )
    from tokmo.records.market_regime_shadow_state import MarketRegimeShadowStateStore

    monkeypatch.setattr(
        MarketRegimeShadowStateStore,
        "acquire_identity_lease",
        lambda self, identity: None,
    )
    result = run_production_cadence_once(
        root=root,
        policy_version="cadence-300s-core-shadow-v1",
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )
    stage = {item.stage: item for item in result.stages}["market_regime_shadow"]
    assert (stage.status, stage.reason) == (
        "skipped",
        "in_progress_after_admission",
    )
    assert probe.calls.get("STOCK_CANDLES_DAILY", 0) == 0
    from tokmo.records.ops_kernel import OpsKernelStore

    report = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3"
    ).read_cadence_tick_report(
        "cadence-300s-core-shadow-v1",
        slot,
    )
    assert report["operation_started"]["STOCK_CANDLES_DAILY"] == 1
    assert report["state_counts"]["FAILED"] >= 1


def test_after_admission_recovery_aborts_without_replay_or_provider(
    monkeypatch, tmp_path
):
    slot = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)
    root = tmp_path / "shadow"
    _seed_history(root, lambda: slot)
    deps, probe = build_fixture_cadence_deps(
        fixture=_fixture(()),
        clock=lambda: slot + timedelta(seconds=1),
    )
    from tokmo.records.market_regime_shadow_state import (
        MarketRegimeShadowStateStore,
        ShadowClaimV1,
    )

    monkeypatch.setattr(
        MarketRegimeShadowStateStore,
        "claim",
        lambda self, **kwargs: ShadowClaimV1(
            "recovery_required", kwargs["identity_digest"]
        ),
    )
    result = run_production_cadence_once(
        root=root,
        policy_version="cadence-300s-core-shadow-v1",
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )
    stage = {item.stage: item for item in result.stages}["market_regime_shadow"]
    assert (stage.status, stage.reason) == (
        "skipped",
        "recovery_required_after_admission",
    )
    assert probe.calls.get("STOCK_CANDLES_DAILY", 0) == 0
    from tokmo.records.ops_kernel import OpsKernelStore

    report = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3"
    ).read_cadence_tick_report(
        "cadence-300s-core-shadow-v1",
        slot,
    )
    assert report["operation_started"]["STOCK_CANDLES_DAILY"] == 1
    assert report["state_counts"]["FAILED"] >= 1
