"""R1-R7 contract tests for the explicit-target market-regime seam (G1).

Per the exact-hash-approved delta Plan/SDS v0.1R1: a targeted entry accepts
explicit ``(market, symbol)`` authority and never consults holdings,
watchlist, union, sort, or cap-one selection. Fakes only; no live API.
"""

import ast
import fcntl
import inspect
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

import tokmo.programs.market_regime_shadow as shadow_module
import tokmo.programs.market_regime_targeted_oneshot as oneshot_module
import tokmo.programs.production_cadence as production_cadence_module
from tokmo.facts.toss import derive_account_scope
from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.market_regime_shadow import (
    TargetedRegimeTargetV1,
    run_market_regime_shadow,
    run_market_regime_targeted,
)
from tokmo.programs.market_regime_targeted_oneshot import (
    run_market_regime_targeted_oneshot,
)
from tokmo.programs.production_cadence import (
    ProductionCadenceDepsV1,
    ProductionCadenceRefusal,
    run_production_cadence_once,
)
from tokmo.programs.quotes_sync import derive_analysis_targets
from tokmo.records.calendar_cache import CalendarCacheStore
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.cadence_policy import (
    CADENCE_POLICY_REGISTRY,
    CADENCE_TARGETED_ONESHOT_POLICY_V1,
    TARGETED_ONESHOT_CHILD_LIMITS_V1,
    TARGETED_ONESHOT_POLICY_REGISTRY,
    TargetedOneShotSlotV1,
    cadence_tick_v1,
    policy_snapshot,
    targeted_oneshot_slot,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
)
from tokmo.values.production_cadence import CADENCE_MODE_LIVE_SHADOW
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_calendar import (
    CalendarSession,
    IntegratedHour,
    KrMarketDay,
)
from tokmo.values.toss_market_data import Candle, CandlePageResponse

SLOT = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)  # 19:00 KST Monday
_SOURCE = Path(shadow_module.__file__)


def _clock():
    return SLOT + timedelta(seconds=1)


def _target(symbol="329180", market=Market.KR, requested_by="user:2026-08-10-329180"):
    return TargetedRegimeTargetV1(market, symbol, requested_by)


def _function(name):
    for node in ast.parse(_SOURCE.read_text()).body:
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name} not defined at module top level")


def _referenced_names(node):
    return {child.id for child in ast.walk(node) if isinstance(child, ast.Name)} | {
        child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
    }


class _ForbiddenAccess:
    """Sentinel: any attribute access proves work before target validation."""

    def __init__(self, label):
        self._label = label

    def __getattr__(self, name):
        raise AssertionError(f"{self._label}.{name} accessed")


class _NoSelectionDeps:
    """Clock and provider only; any selection-source access fails the test."""

    def __init__(self, clock, stock_candles_fetch):
        self.clock = clock
        self.stock_candles_fetch = stock_candles_fetch

    def __getattr__(self, name):
        raise AssertionError(f"targeted seam read deps.{name}")


class _CandleProvider:
    def __init__(self, outcome):
        self._outcome = outcome
        self.requests = []

    def __call__(self, *, claim, bearer, symbol, before, adjusted):
        self.requests.append(
            {"symbol": symbol, "bearer": bearer, "before": before, "adjusted": adjusted}
        )
        return self._outcome


class _Token:
    def __init__(self):
        self.calls = 0

    def bearer(self):
        self.calls += 1
        return "test-bearer"


class _Executor:
    def __init__(self):
        self.calls = []

    def execute(self, operation, native_identity, callback):
        self.calls.append((operation, native_identity))
        return callback(object())


class _HistoryAuthority:
    def read_settled_calendar_provenance(self, market, local_date):
        return {
            "native_identity": f"calendar:{market.value}:{local_date.isoformat()}:0"
        }


def _seed_history(root):
    store = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=_clock,
    )
    authority = _HistoryAuthority()
    for offset in range(21):
        store.append_settled(
            Market.KR,
            SLOT.date() - timedelta(days=offset),
            "regular",
            authority=authority,
        )


def _candles():
    return tuple(
        Candle(
            SLOT - timedelta(days=offset),
            "10",
            "12",
            "9",
            "11",
            "100",
            "KRW",
        )
        for offset in range(200)
    )


def _success_outcome():
    return TossReadOutcome("success", CandlePageResponse(_candles(), None))


def _cache_store(present=True):
    if not present:
        return SimpleNamespace(read=lambda: None)
    entry = SimpleNamespace(
        trade_date="2026-08-03",
        sessions={"regularMarket": SimpleNamespace(end="2026-08-03T06:30:00Z")},
    )
    return SimpleNamespace(read=lambda: SimpleNamespace(kr=entry, us=None))


def _run(root, *, target, deps, executor, token, cache_store):
    return run_market_regime_targeted(
        root=root,
        deps=deps,
        policy=SimpleNamespace(policy_version="cadence-300s-core-shadow-v1"),
        tick=SimpleNamespace(scheduled_for_utc=SLOT),
        executor=executor,
        token=token,
        cache_store=cache_store,
        invocation_id="targeted-test",
        target=target,
    )


def _recurring_shaped_deps(provider, *, watchlist):
    return SimpleNamespace(
        clock=_clock,
        stock_candles_fetch=provider,
        watchlist_symbols=watchlist,
        pension_unquotable_symbols=frozenset(),
    )


def _write_holdings(root):
    scope = derive_account_scope("11111")
    identity = AccountScopeIdentityV1(scope, "brokerage", 1, None)
    state = CurrentHoldingsV1(
        1,
        (
            AccountHoldingsV1(
                scope, 1, (HoldingItemV1("000660", "1", "100", "KRW", "KR", None),)
            ),
        ),
        True,
        SLOT,
        {scope: identity},
    )
    CurrentHoldingsStore(
        root / "stores" / "current_holdings.json",
        clock=_clock,
    ).replace(state, identity_fingerprints={scope: "a" * 64})


def _shadow_rows(root):
    path = root / "ops" / "market_regime_shadow.sqlite3"
    with closing(sqlite3.connect(path)) as connection:
        claims = connection.execute(
            "SELECT market,symbol,state,failure_code,completed_session_date "
            "FROM market_regime_shadow_claim"
        ).fetchall()
        results = connection.execute(
            "SELECT result_payload FROM market_regime_shadow_result",
        ).fetchall()
    return claims, results


def test_r1_targeted_run_binds_requested_symbol_through_every_link(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    _write_holdings(root)
    provider = _CandleProvider(_success_outcome())
    executor, token = _Executor(), _Token()
    status = _run(
        root,
        target=_target(),
        executor=executor,
        token=token,
        cache_store=_cache_store(),
        deps=_recurring_shaped_deps(
            provider, watchlist={Market.KR: frozenset({"005930"})}
        ),
    )
    assert status == ("ok", None)
    assert [request["symbol"] for request in provider.requests] == ["329180"]
    assert executor.calls == [
        ("STOCK_CANDLES_DAILY", "candles:mr:KR:329180:2026-08-03:1"),
    ]
    assert token.calls == 1
    claims, results = _shadow_rows(root)
    assert [row[:3] for row in claims] == [("KR", "329180", "SUCCEEDED")]
    assert claims[0][4] == "2026-08-03"
    assert len(results) == 1
    assert json.loads(results[0][0])["symbol"] == "329180"
    raw = (root / "ops" / "market_regime_shadow.sqlite3").read_bytes()
    assert b"005930" not in raw  # watchlist symbol reached no state row
    assert b"000660" not in raw  # holdings symbol reached no state row
    assert b"user:2026-08-10-329180" not in raw  # requested_by is ephemeral
    # Same target under a different provenance tag maps to the same identity
    # digest: reused without any new token/provider start.
    provider_next = _CandleProvider(_success_outcome())
    executor_next, token_next = _Executor(), _Token()
    status_next = _run(
        root,
        target=_target(requested_by="user:someone-else"),
        executor=executor_next,
        token=token_next,
        cache_store=_cache_store(),
        deps=_recurring_shaped_deps(
            provider_next, watchlist={Market.KR: frozenset({"005930"})}
        ),
    )
    assert status_next == ("skipped", "same_completed_session")
    assert provider_next.requests == []
    assert executor_next.calls == []
    assert token_next.calls == 0


def test_r2_watchlist_and_holdings_sources_are_never_touched(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    provider = _CandleProvider(_success_outcome())
    executor, token = _Executor(), _Token()
    status = _run(
        root,
        target=_target(),
        deps=_NoSelectionDeps(_clock, provider),
        executor=executor,
        token=token,
        cache_store=_cache_store(),
    )
    assert status == ("ok", None)
    assert [request["symbol"] for request in provider.requests] == ["329180"]
    assert not (root / "stores").exists()


def test_r2_targeted_source_references_no_selection_seam():
    targeted = _function("run_market_regime_targeted")
    forbidden = {
        "derive_analysis_targets",
        "CurrentHoldingsStore",
        "classify_market",
        "watchlist_symbols",
        "pension_unquotable_symbols",
        "holdings",
        "sorted",
    }
    assert forbidden & _referenced_names(targeted) == set()


@pytest.mark.parametrize(
    "build_bad_target",
    (
        lambda: TargetedRegimeTargetV1(Market.KR, "a005930", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180 ", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, 329180, "user:x"),
        lambda: TargetedRegimeTargetV1("KR", "329180", "user:x"),
        lambda: (Market.KR, "329180", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180", ""),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180", None),
    ),
    ids=(
        "lowercase_symbol",
        "trailing_space",
        "empty_symbol",
        "non_str_symbol",
        "non_market_market",
        "tuple_not_typed_target",
        "empty_requested_by",
        "none_requested_by",
    ),
)
def test_r3_invalid_targets_fail_closed_with_zero_access(tmp_path, build_bad_target):
    token = _Token()
    status = run_market_regime_targeted(
        root=tmp_path / "root",
        deps=_ForbiddenAccess("deps"),
        policy=_ForbiddenAccess("policy"),
        tick=_ForbiddenAccess("tick"),
        executor=_ForbiddenAccess("executor"),
        token=token,
        cache_store=_ForbiddenAccess("cache_store"),
        invocation_id="targeted-test",
        target=build_bad_target(),
    )
    assert status == ("failed", "invalid_target")
    assert token.calls == 0
    assert not (tmp_path / "root").exists()


def test_r4_post_normalize_identity_mismatch_dies_closed(tmp_path, monkeypatch):
    root = tmp_path / "root"
    _seed_history(root)
    real_normalize = shadow_module.normalize_daily_candles

    def poisoned(rows, *, market, symbol, completed_session, adjustment_mode):
        del symbol
        return real_normalize(
            rows,
            market=market,
            symbol="005930",
            completed_session=completed_session,
            adjustment_mode=adjustment_mode,
        )

    monkeypatch.setattr(shadow_module, "normalize_daily_candles", poisoned)
    provider = _CandleProvider(_success_outcome())
    executor, token = _Executor(), _Token()
    status = _run(
        root,
        target=_target(),
        executor=executor,
        token=token,
        cache_store=_cache_store(),
        deps=SimpleNamespace(clock=_clock, stock_candles_fetch=provider),
    )
    assert status == ("failed", "exception_ValueError")
    assert len(provider.requests) == 1
    claims, results = _shadow_rows(root)
    assert [row[1:4] for row in claims] == [("329180", "FAILED", "ValueError")]
    assert results == []  # nothing presentable was stored under any symbol


def test_r4_identity_assertion_guards_before_evaluation_and_success():
    targeted = _function("run_market_regime_targeted")
    guards = [
        node
        for node in ast.walk(targeted)
        if isinstance(node, ast.If)
        and any(
            isinstance(child, ast.Constant)
            and child.value == "target_identity_mismatch"
            for child in ast.walk(node)
        )
    ]
    assert len(guards) == 1
    guard = guards[0]
    assert isinstance(guard.test, ast.Compare)
    sides = [guard.test.left, *guard.test.comparators]
    assert all(
        isinstance(side, ast.Attribute) and side.attr == "symbol" for side in sides
    )
    evaluation = [
        node.lineno
        for node in ast.walk(targeted)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "evaluate_market_regime_core_v1"
    ]
    succeed = [
        node.lineno
        for node in ast.walk(targeted)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "succeed"
    ]
    assert evaluation and succeed
    assert guard.lineno < min(evaluation) < min(succeed)


def test_r5_provider_failure_settles_once_with_no_retry_or_substitution(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    provider = _CandleProvider(TossReadOutcome("rate_limited"))
    executor, token = _Executor(), _Token()
    status = _run(
        root,
        target=_target(),
        executor=executor,
        token=token,
        cache_store=_cache_store(),
        deps=SimpleNamespace(clock=_clock, stock_candles_fetch=provider),
    )
    assert status == ("failed", "exception_ValueError")
    assert [request["symbol"] for request in provider.requests] == ["329180"]
    assert len(executor.calls) == 1
    claims, results = _shadow_rows(root)
    assert [row[1:4] for row in claims] == [("329180", "FAILED", "ValueError")]
    assert results == []
    # The failed identity settles: the next run skips with zero wire.
    provider_next = _CandleProvider(_success_outcome())
    executor_next, token_next = _Executor(), _Token()
    status_next = _run(
        root,
        target=_target(),
        executor=executor_next,
        token=token_next,
        cache_store=_cache_store(),
        deps=SimpleNamespace(clock=_clock, stock_candles_fetch=provider_next),
    )
    assert status_next == ("skipped", "failed")
    assert provider_next.requests == []
    assert executor_next.calls == []
    assert token_next.calls == 0


@pytest.mark.parametrize(
    "cache_present", (False, True), ids=("no_cache_entry", "no_history")
)
def test_r6_unresolvable_completed_session_skips_with_zero_wire(
    tmp_path, cache_present
):
    root = tmp_path / "root"
    provider = _CandleProvider(_success_outcome())
    executor, token = _Executor(), _Token()
    status = _run(
        root,
        target=_target(),
        executor=executor,
        token=token,
        cache_store=_cache_store(present=cache_present),
        deps=SimpleNamespace(clock=_clock, stock_candles_fetch=provider),
    )
    assert status == ("skipped", "completed_session_unavailable")
    assert provider.requests == []
    assert executor.calls == []
    assert token.calls == 0
    assert not (root / "ops" / "market_regime_shadow.sqlite3").exists()


def test_r7_recurring_selector_remains_sealed_and_shared():
    assert shadow_module.derive_analysis_targets is derive_analysis_targets
    recurring = _function("run_market_regime_shadow")
    forbidden = {
        "TargetedRegimeTargetV1",
        "run_market_regime_targeted",
        "requested_by",
        "target",
    }
    assert forbidden & _referenced_names(recurring) == set()
    assert not any(
        isinstance(node, ast.Constant) and node.value == "target_identity_mismatch"
        for node in ast.walk(recurring)
    )
    recurring_params = inspect.signature(run_market_regime_shadow).parameters
    assert set(recurring_params) == {
        "root",
        "deps",
        "policy",
        "tick",
        "executor",
        "token",
        "cache_store",
        "invocation_id",
    }
    targeted_params = inspect.signature(run_market_regime_targeted).parameters
    assert set(targeted_params) == set(recurring_params) | {"target"}
    assert targeted_params["target"].kind is inspect.Parameter.KEYWORD_ONLY


# ---------------------------------------------------------------------------
# G2 (Plan/SDS v0.2 §§15-19): bounded production one-shot operator seam.
# T0 pins the consumed 2026-08-10T05:57:32Z incident as an executable fossil
# against the real kernel; it must be GREEN before any G2 implementation.
# ---------------------------------------------------------------------------

_INCIDENT_POLICY_ID = "targeted-one-shot-v1-20260810-329180"
OFF_GRID = SLOT + timedelta(seconds=152)  # 152 % 300 != 0, like 05:57:32Z


def _kernel_rows(root, policy_version):
    path = root / "ops" / "ops_kernel.sqlite3"
    if not path.exists():
        return None
    with closing(sqlite3.connect(path)) as connection:
        ticks = connection.execute(
            "SELECT COUNT(*) FROM cadence_ticks WHERE policy_version=?",
            (policy_version,),
        ).fetchone()[0]
        wires = connection.execute(
            "SELECT operation,native_identity,state FROM cadence_wire_starts "
            "WHERE policy_version=? ORDER BY operation",
            (policy_version,),
        ).fetchall()
    return ticks, wires


def test_g2_t0_unregistered_offgrid_incident_dies_with_zero_wire(tmp_path):
    from tokmo.programs.cadence_wire import CadenceWireExecutorV1
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.cadence_policy import (
        CadencePolicyV1,
        TickRequestLimitsV1,
        TradesChildLimitsV1,
    )
    from tokmo.values.ops_kernel import ProducerRole

    root = tmp_path / "root"
    _seed_history(root)
    inline_policy = CadencePolicyV1(
        policy_version=_INCIDENT_POLICY_ID,
        request_limits=TickRequestLimitsV1(
            total_wire_starts=2,
            token_starts=1,
            calendar_starts=0,
            holdings_starts=0,
            quote_starts=0,
            trades_starts=0,
            daily_candle_starts=1,
            market_daily_candle_starts=1,
        ),
    )
    kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    executor = CadenceWireExecutorV1(
        ops_kernel=kernel,
        role=ProducerRole.COLLECTOR,
        policy=inline_policy,
        scheduled_for_utc=OFF_GRID,
        clock=_clock,
        child_limits=TradesChildLimitsV1(),
        lane_registry=OPERATION_LANE_REGISTRY_V1,
        lane=OperationLane.CADENCE_300S,
    )
    provider = _CandleProvider(_success_outcome())
    token = _Token()
    status = run_market_regime_targeted(
        root=root,
        deps=SimpleNamespace(clock=_clock, stock_candles_fetch=provider),
        policy=inline_policy,
        tick=SimpleNamespace(scheduled_for_utc=OFF_GRID),
        executor=executor,
        token=token,
        cache_store=_cache_store(),
        invocation_id="targeted-test",
        target=_target(),
    )
    assert status == ("failed", "admission_internal_error")
    assert kernel.read_cadence_tick_report(_INCIDENT_POLICY_ID, OFF_GRID) is None
    assert _kernel_rows(root, _INCIDENT_POLICY_ID) == (0, [])
    assert provider.requests == []
    assert token.calls == 0


TARGETED_POLICY_ID = "cadence-300s-targeted-oneshot-v1"
OBSERVED = OFF_GRID  # operator invokes at an off-grid instant; slot floors
_EMPTY_WIRE = {
    "total_started": 0,
    "group_started": {},
    "operation_started": {},
    "state_counts": {},
}


class _TokenFetch:
    def __init__(self):
        self.calls = 0

    def __call__(self, claim):
        self.calls += 1
        return "test-bearer"


class _OneshotDeps:
    """clock/token/candles only; any selection-source access fails the test."""

    def __init__(self, clock, token_fetch, stock_candles_fetch):
        self.clock = clock
        self.token_fetch = token_fetch
        self.stock_candles_fetch = stock_candles_fetch

    def __getattr__(self, name):
        raise AssertionError(f"one-shot seam read deps.{name}")


def _seed_calendar_cache(root):
    (root / "ops").mkdir(mode=0o700, parents=True, exist_ok=True)
    CalendarCacheStore(root / "ops" / "calendar_cache.json", clock=_clock).write(
        KrMarketDay(
            SLOT.date(),
            IntegratedHour(
                regular_market=CalendarSession(
                    datetime(2026, 8, 3, 0, 0, tzinfo=UTC),
                    datetime(2026, 8, 3, 6, 30, tzinfo=UTC),
                ),
            ),
        ),
        None,
    )


def _oneshot_root(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    _seed_calendar_cache(root)
    return root


def _oneshot(
    root,
    *,
    provider,
    token_fetch,
    target=None,
    observed=OBSERVED,
    invocation_id="oneshot-test",
):
    return run_market_regime_targeted_oneshot(
        root=root,
        deps=_OneshotDeps(_clock, token_fetch, provider),
        target=_target() if target is None else target,
        observed_now_utc=observed,
        invocation_id=invocation_id,
    )


def test_g2_t1_registered_oneshot_bounded_green_path(tmp_path):
    root = _oneshot_root(tmp_path)
    provider = _CandleProvider(_success_outcome())
    token_fetch = _TokenFetch()
    evidence = _oneshot(root, provider=provider, token_fetch=token_fetch)
    assert (evidence["status"], evidence["reason"]) == ("ok", None)
    assert evidence["policy_version"] == TARGETED_POLICY_ID
    assert evidence["scheduled_for_utc"] == SLOT.isoformat()  # floored
    assert evidence["invocation_id"] == "oneshot-test"
    assert token_fetch.calls == 1
    assert [request["symbol"] for request in provider.requests] == ["329180"]
    assert [request["bearer"] for request in provider.requests] == ["test-bearer"]
    wire = evidence["wire"]
    assert wire["total_started"] == 2
    assert wire["group_started"] == {
        "TOKEN": 1,
        "DAILY_CANDLES": 1,
        "CALENDAR": 0,
        "HOLDINGS": 0,
        "QUOTES": 0,
        "TRADES": 0,
        "MEMBERSHIP": 0,
        "CONDITIONAL_ORDER_HISTORY": 0,
    }
    assert wire["operation_started"] == {"TOKEN": 1, "STOCK_CANDLES_DAILY": 1}
    assert wire["state_counts"] == {"SUCCEEDED": 2}
    state_row = evidence["state_row"]
    assert (
        state_row["state"],
        state_row["market"],
        state_row["symbol"],
        state_row["completed_session"],
        state_row["normalized_bar_count"],
    ) == ("SUCCEEDED", "KR", "329180", "2026-08-03", 200)
    assert len(state_row["pack_digest"]) == 64
    from tokmo.records.ops_kernel import OpsKernelStore

    kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    for recurring_version in CADENCE_POLICY_REGISTRY:
        assert kernel.read_cadence_tick_report(recurring_version, SLOT) is None


def test_g2_t1_slot_derivation_floors_and_rejects_offgrid_construction():
    incident = datetime(2026, 8, 10, 5, 57, 32, tzinfo=UTC)
    slot = targeted_oneshot_slot(TARGETED_POLICY_ID, incident)
    assert type(slot) is TargetedOneShotSlotV1
    assert slot.policy_version == TARGETED_POLICY_ID
    assert slot.scheduled_for_utc == datetime(2026, 8, 10, 5, 55, tzinfo=UTC)
    with pytest.raises(ValueError):
        TargetedOneShotSlotV1(TARGETED_POLICY_ID, incident)  # off grid
    with pytest.raises(ValueError):
        TargetedOneShotSlotV1(TARGETED_POLICY_ID, SLOT.replace(tzinfo=None))
    with pytest.raises(ValueError):
        TargetedOneShotSlotV1(TARGETED_POLICY_ID, SLOT.replace(microsecond=500))


def test_g2_t2_exact_caps_and_registry_separation():
    policy = CADENCE_TARGETED_ONESHOT_POLICY_V1
    limits = policy.request_limits
    assert policy.policy_version == TARGETED_POLICY_ID
    assert policy.cadence_seconds == 300
    assert (
        limits.total_wire_starts,
        limits.token_starts,
        limits.calendar_starts,
        limits.holdings_starts,
        limits.quote_starts,
        limits.trades_starts,
        limits.daily_candle_starts,
    ) == (2, 1, 0, 0, 0, 0, 1)
    assert (limits.market_daily_candle_starts, limits.quant_daily_candle_starts) == (
        1,
        0,
    )
    assert (
        TARGETED_ONESHOT_CHILD_LIMITS_V1.orders_page_starts,
        TARGETED_ONESHOT_CHILD_LIMITS_V1.order_detail_starts,
    ) == (0, 0)
    policy_snapshot(policy, TARGETED_ONESHOT_CHILD_LIMITS_V1)  # feasible
    assert set(CADENCE_POLICY_REGISTRY) == {
        "cadence-300s-v1",
        "cadence-300s-core-shadow-v1",
        "cadence-300s-market-portfolio-v1",
        "cadence-300s-market-quant4-shadow-v1",
        "cadence-300s-quant-profiles-shadow-v1",
    }
    assert TARGETED_POLICY_ID not in CADENCE_POLICY_REGISTRY
    assert dict(TARGETED_ONESHOT_POLICY_REGISTRY) == {TARGETED_POLICY_ID: policy}
    with pytest.raises(TypeError):
        TARGETED_ONESHOT_POLICY_REGISTRY[TARGETED_POLICY_ID] = policy


@pytest.mark.parametrize(
    "build_bad_target",
    (
        lambda: TargetedRegimeTargetV1(Market.KR, "a005930", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180 ", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, 329180, "user:x"),
        lambda: TargetedRegimeTargetV1("KR", "329180", "user:x"),
        lambda: (Market.KR, "329180", "user:x"),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180", ""),
        lambda: TargetedRegimeTargetV1(Market.KR, "329180", None),
    ),
    ids=(
        "lowercase_symbol",
        "trailing_space",
        "empty_symbol",
        "non_str_symbol",
        "non_market_market",
        "tuple_not_typed_target",
        "empty_requested_by",
        "none_requested_by",
    ),
)
def test_g2_t3_invalid_targets_fail_closed_via_operator_seam(
    tmp_path, build_bad_target
):
    root = _oneshot_root(tmp_path)
    provider = _CandleProvider(_success_outcome())
    token_fetch = _TokenFetch()
    evidence = _oneshot(
        root, provider=provider, token_fetch=token_fetch, target=build_bad_target()
    )
    assert (evidence["status"], evidence["reason"]) == ("failed", "invalid_target")
    assert evidence["wire"] == _EMPTY_WIRE
    assert evidence["state_row"] is None
    assert _kernel_rows(root, TARGETED_POLICY_ID) == (0, [])
    assert provider.requests == []
    assert token_fetch.calls == 0
    # the lease was released: an immediate exclusive acquire succeeds
    descriptor = production_cadence_module._acquire_lease(root)
    assert descriptor is not None
    os.close(descriptor)


def test_g2_t4_provider_failure_consumes_attempt_and_reruns_stay_zero(tmp_path):
    root = _oneshot_root(tmp_path)
    provider = _CandleProvider(TossReadOutcome("rate_limited"))
    token_fetch = _TokenFetch()
    first = _oneshot(root, provider=provider, token_fetch=token_fetch)
    assert (first["status"], first["reason"]) == ("failed", "exception_ValueError")
    assert token_fetch.calls == 1
    assert len(provider.requests) == 1
    ticks, wires = _kernel_rows(root, TARGETED_POLICY_ID)
    assert ticks == 1
    assert [(row[0], row[2]) for row in wires] == [
        ("STOCK_CANDLES_DAILY", "FAILED"),
        ("TOKEN", "SUCCEEDED"),
    ]
    assert first["wire"]["state_counts"] == {"SUCCEEDED": 1, "FAILED": 1}
    assert first["state_row"]["state"] == "FAILED"
    # 2nd, same slot: the settled state stops it before any new start.
    provider_2 = _CandleProvider(_success_outcome())
    token_2 = _TokenFetch()
    second = _oneshot(
        root,
        provider=provider_2,
        token_fetch=token_2,
        observed=OBSERVED + timedelta(seconds=10),
    )
    assert (second["status"], second["reason"]) == ("skipped", "failed")
    assert provider_2.requests == []
    assert token_2.calls == 0
    assert second["wire"]["total_started"] == 2  # consumed budget, no growth
    # 3rd, next slot, same completed session: identity chain stays zero-wire.
    provider_3 = _CandleProvider(_success_outcome())
    token_3 = _TokenFetch()
    third = _oneshot(
        root,
        provider=provider_3,
        token_fetch=token_3,
        observed=OBSERVED + timedelta(seconds=300),
    )
    assert (third["status"], third["reason"]) == ("skipped", "failed")
    assert provider_3.requests == []
    assert token_3.calls == 0
    assert _kernel_rows(root, TARGETED_POLICY_ID) == (1, wires)


def test_g2_t5_identity_chain_via_operator_seam(tmp_path):
    root = _oneshot_root(tmp_path)
    provider = _CandleProvider(_success_outcome())
    token_fetch = _TokenFetch()
    evidence = _oneshot(root, provider=provider, token_fetch=token_fetch)
    assert (evidence["status"], evidence["reason"]) == ("ok", None)
    assert [request["symbol"] for request in provider.requests] == ["329180"]
    assert _kernel_rows(root, TARGETED_POLICY_ID) == (
        1,
        [
            ("STOCK_CANDLES_DAILY", "candles:mr:KR:329180:2026-08-03:1", "SUCCEEDED"),
            ("TOKEN", "token:tick0", "SUCCEEDED"),
        ],
    )
    assert (evidence["market"], evidence["symbol"]) == ("KR", "329180")
    assert evidence["requested_by"] == "user:2026-08-10-329180"
    assert (evidence["state_row"]["market"], evidence["state_row"]["symbol"]) == (
        "KR",
        "329180",
    )
    claims, results = _shadow_rows(root)
    assert [row[:3] for row in claims] == [("KR", "329180", "SUCCEEDED")]
    assert json.loads(results[0][0])["symbol"] == "329180"
    raw = (root / "ops" / "market_regime_shadow.sqlite3").read_bytes()
    assert b"005930" not in raw


def test_g2_t6_active_cadence_lock_defers_oneshot_without_consumption(tmp_path):
    root = _oneshot_root(tmp_path)
    # The seam must reuse the exact recurring lease primitive and lock file.
    assert oneshot_module._acquire_lease is production_cadence_module._acquire_lease
    runtime = root / "runtime"
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(runtime / "cadence.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        provider = _CandleProvider(_success_outcome())
        token_fetch = _TokenFetch()
        contended = _oneshot(root, provider=provider, token_fetch=token_fetch)
    finally:
        os.close(descriptor)
    assert (contended["status"], contended["reason"]) == (
        "skipped",
        "overlap_lock_contention",
    )
    assert contended["wire"] == _EMPTY_WIRE
    assert contended["state_row"] is None
    assert not (root / "ops" / "ops_kernel.sqlite3").exists()
    assert provider.requests == []
    assert token_fetch.calls == 0
    # After release the identical call proceeds: the attempt was not consumed.
    provider_retry = _CandleProvider(_success_outcome())
    token_retry = _TokenFetch()
    retry = _oneshot(root, provider=provider_retry, token_fetch=token_retry)
    assert (retry["status"], retry["reason"]) == ("ok", None)


def _refusing_provider(**kwargs):
    raise AssertionError("provider callable must not run")


# ---------------------------------------------------------------------------
# m1 repair (portfolio-expansion Plan §7): ``_state_readback`` must bind the
# validated ``(market, symbol)`` and the invoking identity — never "latest row
# under the policy". No state filter: FAILED/CLAIMED rows surface unchanged,
# with ``failure_code`` and ``owned_by_this_invocation`` as evidence.
# ---------------------------------------------------------------------------

import hashlib
from datetime import date as _date

from tokmo.records.market_regime_shadow_state import MarketRegimeShadowStateStore
from tokmo.values.market_regime_core import AdjustmentMode


def _seed_terminal_row(
    root,
    *,
    market,
    symbol,
    state,
    claimed_at,
    session=_date(2026, 8, 3),
    policy_version=TARGETED_POLICY_ID,
    invocation="seed-invoc",
    failure_code="ValueError",
):
    identity = hashlib.sha256(
        f"{policy_version}|{market.value}|{symbol}|{session}|{claimed_at}".encode()
    ).hexdigest()
    store = MarketRegimeShadowStateStore(root / "ops" / "market_regime_shadow.sqlite3")
    lease = store.acquire_identity_lease(identity)
    assert lease is not None
    with lease:
        assert (
            store.claim(
                identity_digest=identity,
                program_id="market-regime-core-v1",
                spec_digest="b" * 64,
                policy_version=policy_version,
                market=market,
                symbol=symbol,
                completed_session_date=session,
                adjustment_mode=AdjustmentMode.ADJUSTED,
                request_identity="c" * 64,
                owner_invocation_id=invocation,
                owner_started_at=claimed_at,
                owner_lease_key=lease.key,
                identity_lease=lease,
            ).state
            == "owner"
        )
    if state == "SUCCEEDED":
        store.succeed(
            identity,
            input_digest="d" * 64,
            pack_digest="e" * 64,
            fetched_at=claimed_at,
            evaluated_at=claimed_at,
            completed_session_date=session,
            normalized_bar_count=200,
            result_payload={"symbol": symbol},
        )
    else:
        store.fail(identity, failure_code=failure_code, finished_at=claimed_at)
    return identity


def _readback(
    root,
    *,
    market,
    symbol,
    invocation_id="seed-invoc",
    policy_version=TARGETED_POLICY_ID,
):
    return oneshot_module._state_readback(
        root,
        policy_version,
        market=market,
        symbol=symbol,
        invocation_id=invocation_id,
    )


def test_m1_1_readback_binds_symbol_over_recency(tmp_path):
    root = tmp_path / "root"
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="329180",
        state="SUCCEEDED",
        claimed_at=SLOT + timedelta(seconds=10),
    )
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="000660",
        state="SUCCEEDED",
        claimed_at=SLOT + timedelta(seconds=500),
    )
    row = _readback(root, market="KR", symbol="329180")
    assert (row["market"], row["symbol"], row["state"]) == ("KR", "329180", "SUCCEEDED")
    assert row["failure_code"] is None
    assert row["owned_by_this_invocation"] is True
    foreign = _readback(
        root, market="KR", symbol="329180", invocation_id="someone-else"
    )
    assert foreign["owned_by_this_invocation"] is False
    assert _readback(root, market="KR", symbol="005930") is None


def test_m1_2_readback_binds_market_for_same_symbol_string(tmp_path):
    root = tmp_path / "root"
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="069500",
        state="SUCCEEDED",
        claimed_at=SLOT + timedelta(seconds=10),
    )
    _seed_terminal_row(
        root,
        market=Market.US,
        symbol="069500",
        state="FAILED",
        claimed_at=SLOT + timedelta(seconds=500),
        failure_code="ValueError",
    )
    kr = _readback(root, market="KR", symbol="069500")
    assert (kr["market"], kr["state"], kr["failure_code"]) == ("KR", "SUCCEEDED", None)
    us = _readback(root, market="US", symbol="069500")
    assert (us["market"], us["state"], us["failure_code"]) == (
        "US",
        "FAILED",
        "ValueError",
    )


def test_m1_3_readback_exposes_latest_failed_row_without_state_filter(tmp_path):
    root = tmp_path / "root"
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="329180",
        state="SUCCEEDED",
        session=_date(2026, 8, 1),
        claimed_at=SLOT + timedelta(seconds=10),
    )
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="329180",
        state="FAILED",
        session=_date(2026, 8, 3),
        claimed_at=SLOT + timedelta(seconds=500),
        failure_code="ValueError",
    )
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="000660",
        state="SUCCEEDED",
        claimed_at=SLOT + timedelta(seconds=900),
    )
    row = _readback(root, market="KR", symbol="329180")
    # Newest row for the bound identity wins even though it is FAILED and a
    # newer foreign-symbol SUCCEEDED row exists: kills mutations M1 (symbol
    # predicate removed), M3 (state filter added), M4 (ASC ordering).
    assert (row["state"], row["failure_code"], row["completed_session"]) == (
        "FAILED",
        "ValueError",
        "2026-08-03",
    )


def test_m1_oneshot_evidence_ignores_stale_other_symbol_row(tmp_path):
    root = _oneshot_root(tmp_path)
    _seed_terminal_row(
        root,
        market=Market.KR,
        symbol="000660",
        state="SUCCEEDED",
        claimed_at=SLOT + timedelta(seconds=900),
    )
    provider = _CandleProvider(_success_outcome())
    token_fetch = _TokenFetch()
    evidence = _oneshot(root, provider=provider, token_fetch=token_fetch)
    assert (evidence["status"], evidence["reason"]) == ("ok", None)
    state_row = evidence["state_row"]
    assert (state_row["market"], state_row["symbol"], state_row["state"]) == (
        "KR",
        "329180",
        "SUCCEEDED",
    )
    assert state_row["failure_code"] is None
    assert state_row["owned_by_this_invocation"] is True


def test_g2_t7_recurring_entrypoints_structurally_reject_targeted_policy(tmp_path):
    with pytest.raises(ValueError, match="unknown cadence policy version"):
        cadence_tick_v1(TARGETED_POLICY_ID, SLOT)
    deps = ProductionCadenceDepsV1(
        mode=CADENCE_MODE_LIVE_SHADOW,
        clock=_clock,
        token_fetch=_refusing_provider,
        calendar_fetch=_refusing_provider,
        accounts_fetch=_refusing_provider,
        account_holdings_fetch=_refusing_provider,
        quotes_fetch=_refusing_provider,
        orders_page_fetch=_refusing_provider,
        order_detail_fetch=_refusing_provider,
        stock_candles_fetch=_refusing_provider,
        regular_session_dates=object(),
    )
    with pytest.raises(
        ProductionCadenceRefusal, match="unknown cadence policy version"
    ):
        run_production_cadence_once(
            root=tmp_path / "root",
            policy_version=TARGETED_POLICY_ID,
            scheduled_for_utc=SLOT,
            observed_now_utc=OBSERVED,
            deps=deps,
        )
    # Reverse direction: recurring policies cannot enter the one-shot slot.
    with pytest.raises(ValueError, match="unknown targeted one-shot policy"):
        targeted_oneshot_slot("cadence-300s-v1", OBSERVED)
    with pytest.raises(ValueError, match="unknown targeted one-shot policy"):
        TargetedOneShotSlotV1("cadence-300s-core-shadow-v1", SLOT)
