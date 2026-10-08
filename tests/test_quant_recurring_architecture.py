import ast
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from hashlib import sha256
import json
from pathlib import Path
import sqlite3

import pytest


ROOT = Path(__file__).parents[1]
WORKTREE_ROOT = ROOT.parents[1]


def _calendar_inputs(*, us_state, us_open):
    from tokmo.programs.calendar_refresh import MarketRefreshResult, MarketRefreshState
    from tokmo.values.session_gate import Market, SessionVerdict
    from tokmo.values.toss_market_calendar import CalendarSession

    slot = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
    session = CalendarSession(slot - timedelta(hours=1), slot + timedelta(hours=1))
    results = {
        Market.KR: MarketRefreshResult(
            Market.KR,
            date(2026, 8, 15),
            MarketRefreshState.PROVIDER_FAILED,
            False,
        ),
        Market.US: MarketRefreshResult(
            Market.US, date(2026, 8, 14), us_state, False,
        ),
    }
    verdicts = (
        SessionVerdict(Market.KR, False, "stale_cache", None),
        SessionVerdict(
            Market.US,
            us_open,
            "open" if us_open else "stale_cache",
            session if us_open else None,
        ),
    )
    return slot, results, verdicts


def _mask(*, slot, verdicts):
    from tokmo.values.cadence_policy import CADENCE_MARKET_PORTFOLIO_POLICY_V1
    from tokmo.values.open_market_mask import build_open_market_mask
    from tokmo.values.session_gate import Market

    return build_open_market_mask(
        policy_version=CADENCE_MARKET_PORTFOLIO_POLICY_V1.policy_version,
        scheduled_for_utc=slot,
        tick_id=int(slot.timestamp()) // 300,
        verdicts=verdicts,
        local_dates={Market.KR: date(2026, 8, 15), Market.US: date(2026, 8, 14)},
    )


def _quant_seam_fixture_deps(slot):
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps

    return build_fixture_cadence_deps(
        fixture={
            "token": "fixture-bearer",
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
            "accounts": [{
                "seed": "1",
                "holdings": [{
                    "symbol": "AAPL",
                    "quantity": "1",
                    "avg_price": "100",
                    "currency": "USD",
                    "market_raw": "US",
                }],
            }],
            "quotes": {"AAPL": {"price": "101", "currency": "USD"}},
            "orders": {},
        },
        clock=lambda: slot + timedelta(seconds=1),
    )


def _inject_quant_limits(monkeypatch, module):
    from tokmo.values.cadence_policy import (
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_QUANT_SHADOW_POLICY_V1,
    )

    policy = replace(
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        request_limits=CADENCE_QUANT_SHADOW_POLICY_V1.request_limits,
    )
    monkeypatch.setattr(module, "CADENCE_POLICY_REGISTRY", {
        policy.policy_version: policy,
    })
    monkeypatch.setattr(
        module,
        "run_market_regime_shadow",
        lambda **kwargs: ("skipped", "test_isolation"),
    )
    return policy


def test_quant_recurring_program_imports_no_live_membership_transport_symbol():
    path = ROOT / "src" / "tokmo" / "programs" / "quant_recurring_shadow.py"
    imports = {
        node.module
        for node in ast.walk(ast.parse(path.read_text()))
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "tokmo.facts.official_membership_live" not in imports


def test_calendar_carrier_construction_reads_current_tick_inputs_and_cache_once():
    import tokmo.programs.production_cadence as module
    from tokmo.programs.calendar_refresh import MarketRefreshState
    from tokmo.values.session_gate import Market

    slot, results, verdicts = _calendar_inputs(
        us_state=MarketRefreshState.SUCCESS,
        us_open=True,
    )
    mask = _mask(slot=slot, verdicts=verdicts)

    class Cache:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            return type("CacheValue", (), {
                "us": type("UsEntry", (), {"trade_date": "2026-08-14"})(),
            })()

    cache = Cache()
    calendar = module._quant_tick_calendar_input(
        results=results,
        verdicts=verdicts,
        mask=mask,
        cache_store=cache,
    )

    assert cache.reads == 1
    assert calendar.us_refresh_state == "success"
    assert calendar.us_refresh_succeeded is True
    assert calendar.us_verdict_open is True
    assert calendar.us_in_effective_mask is True
    assert calendar.mask_bound is True
    assert calendar.us_cache_trade_date == date(2026, 8, 14)
    assert results[Market.KR].state is MarketRefreshState.PROVIDER_FAILED


def test_carrier_construction_then_failed_refresh_due_gate_never_rereads_cache(
    tmp_path,
):
    import tokmo.programs.production_cadence as production
    import tokmo.programs.quant_recurring_shadow as recurring
    from tokmo.programs.calendar_refresh import MarketRefreshState
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    slot, results, verdicts = _calendar_inputs(
        us_state=MarketRefreshState.PROVIDER_FAILED,
        us_open=False,
    )
    mask = _mask(slot=slot, verdicts=verdicts)

    class ConstructionCache:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            return type("CacheValue", (), {
                "us": type("UsEntry", (), {"trade_date": "2026-08-14"})(),
            })()

    class BombCache:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            raise AssertionError("evaluate_quant_due reread cache for D2")

    construction_cache = ConstructionCache()
    calendar = production._quant_tick_calendar_input(
        results=results,
        verdicts=verdicts,
        mask=mask,
        cache_store=construction_cache,
    )
    due_cache = BombCache()
    context = recurring.evaluate_quant_due(
        root=tmp_path,
        deps=type("Deps", (), {"clock": staticmethod(lambda: slot)})(),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        tick=type("Tick", (), {"scheduled_for_utc": slot})(),
        calendar=calendar,
        cache_store=due_cache,
        receipts=type("Receipts", (), {
            "read_due": lambda self, identity: (_ for _ in ()).throw(
                AssertionError(identity)
            ),
        })(),
    )

    assert construction_cache.reads == 1
    assert due_cache.reads == 0
    assert context.decision is QuantDueDecisionV1.NO_COMPLETED_SESSION
    assert context.identity is None


def test_calendar_binding_conflict_is_typed_zero_quant_effect_at_callsite(
    monkeypatch, tmp_path,
):
    import tokmo.programs.production_cadence as module

    slot = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    policy = _inject_quant_limits(monkeypatch, module)
    deps, probe = _quant_seam_fixture_deps(slot)
    quant_calls = []

    def conflict(**kwargs):
        quant_calls.append(("carrier", kwargs["mask"]))
        raise ValueError("quant_calendar_mask_verdict_conflict")

    def forbidden_quant_seam(**kwargs):
        raise AssertionError(f"quant effect reached: {sorted(kwargs)}")

    monkeypatch.setattr(module, "_quant_tick_calendar_input", conflict)
    monkeypatch.setattr(
        module, "run_quant_membership_acquisition", forbidden_quant_seam
    )
    monkeypatch.setattr(module, "run_quant_recurring_shadow", forbidden_quant_seam)
    result = module.run_production_cadence_once(
        root=tmp_path / "shadow",
        policy_version=policy.policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )

    statuses = {item.stage: item for item in result.stages}
    assert len(quant_calls) == 1
    assert statuses["quant_membership"].status == "skipped"
    assert statuses["quant_membership"].reason == "CALENDAR_BINDING_CONFLICT"
    assert statuses["quant_shadow"].status == "skipped"
    assert statuses["quant_shadow"].reason == "CALENDAR_BINDING_CONFLICT"
    assert "source_failure" not in {
        statuses["quant_membership"].reason,
        statuses["quant_shadow"].reason,
    }
    assert not any(name.startswith("MEMBERSHIP_") for name in probe.calls)
    assert probe.calls.get("STOCK_CANDLES_DAILY", 0) == 0
    assert not (tmp_path / "shadow" / "ops" / "quant_recurring.sqlite3").exists()
    assert not (
        tmp_path / "shadow" / "official-membership" / "observations.sqlite3"
    ).exists()


def test_quant_seam_reads_the_same_mask_object_before_open_stages(
    monkeypatch, tmp_path,
):
    import tokmo.programs.production_cadence as module
    from tokmo.values.quant_recurring import (
        QuantDueContextV1,
        QuantDueDecisionV1,
    )

    slot = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    policy = _inject_quant_limits(monkeypatch, module)
    deps, probe = _quant_seam_fixture_deps(slot)
    actual_carrier = module._quant_tick_calendar_input
    events = []

    def capture_carrier(**kwargs):
        events.append(("carrier", kwargs["mask"]))
        return actual_carrier(**kwargs)

    def not_due(**kwargs):
        del kwargs
        return QuantDueContextV1(
            QuantDueDecisionV1.NO_COMPLETED_SESSION,
            None,
            None,
            date(2026, 8, 3),
            (),
            0,
            3,
            7,
        )

    def capture_open(*, mask, **kwargs):
        del kwargs
        events.append(("open", mask))

    monkeypatch.setattr(module, "_quant_tick_calendar_input", capture_carrier)
    monkeypatch.setattr(module, "evaluate_quant_due", not_due)
    monkeypatch.setattr(module, "_run_open_stages", capture_open)
    module.run_production_cadence_once(
        root=tmp_path / "shadow",
        policy_version=policy.policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )

    assert [name for name, _ in events] == ["carrier", "open"]
    assert events[0][1] is events[1][1]
    assert events[0][1].digest == events[1][1].digest


def test_d2_refusal_does_not_open_existing_quant_receipt_store(
    monkeypatch, tmp_path,
):
    import tokmo.programs.production_cadence as module
    from tokmo.values.quant_recurring import QuantTickCalendarInputV1

    slot = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
    policy = _inject_quant_limits(monkeypatch, module)
    deps, probe = _quant_seam_fixture_deps(slot)

    def bomb(path):
        raise AssertionError(f"receipt store opened before D2: {path}")

    monkeypatch.setattr(module, "_existing_quant_receipt_store", bomb)
    monkeypatch.setattr(
        module,
        "run_quant_membership_acquisition",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"membership effect reached: {sorted(kwargs)}")
        ),
    )
    monkeypatch.setattr(
        module,
        "run_quant_recurring_shadow",
        lambda **kwargs: (_ for _ in ()).throw(
            AssertionError(f"shadow effect reached: {sorted(kwargs)}")
        ),
    )
    monkeypatch.setattr(
        module,
        "_quant_tick_calendar_input",
        lambda **kwargs: QuantTickCalendarInputV1(
            us_refresh_state="provider_failed",
            us_refresh_succeeded=False,
            us_verdict_open=False,
            us_verdict_reason="stale_cache",
            us_in_effective_mask=False,
            mask_bound=True,
            us_cache_trade_date=date(2026, 8, 3),
        ),
    )
    result = module.run_production_cadence_once(
        root=tmp_path / "shadow",
        policy_version=policy.policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )

    status = {item.stage: item for item in result.stages}["quant_membership"]
    assert (status.status, status.reason) == (
        "skipped",
        "no_completed_session",
    )
    assert not any(name.startswith("MEMBERSHIP_") for name in probe.calls)
    assert probe.calls.get("ACCOUNTS", 0) == 0
    with sqlite3.connect(
        tmp_path / "shadow" / "ops" / "ops_kernel.sqlite3"
    ) as connection:
        membership_rows = connection.execute(
            "SELECT COUNT(*) FROM cadence_wire_starts "
            "WHERE operation_group='MEMBERSHIP'"
        ).fetchone()[0]
    assert membership_rows == 0
    assert not (tmp_path / "shadow" / "ops" / "quant_recurring.sqlite3").exists()


def test_membership_kernel_rows_settle_before_first_holdings_start(
    monkeypatch, tmp_path,
):
    import tokmo.programs.production_cadence as module
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.programs.quant_recurring_shadow import QuantMembershipOutcomeV1
    from tokmo.values.quant_multifactor_core import MarketV1
    from tokmo.values.quant_recurring import (
        QUANT_RECURRING_PROGRAM_ID,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
        QuantDueContextV1,
        QuantDueDecisionV1,
        QuantDueIdentityV1,
    )

    slot = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    policy = _inject_quant_limits(monkeypatch, module)
    counter = {"value": 0}

    def clock():
        counter["value"] += 1
        return slot + timedelta(seconds=1, microseconds=counter["value"])

    deps, _ = _quant_seam_fixture_deps(slot)
    deps = replace(deps, clock=clock)
    symbols = ("AAPL", "MSFT", "NVDA", "TSLA")
    identity = QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        policy.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        date(2026, 8, 3),
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )
    context = QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        date(2026, 8, 3),
        date(2026, 8, 3),
        symbols,
        90,
        7,
        7,
    )

    monkeypatch.setattr(module, "evaluate_quant_due", lambda **kwargs: context)

    def membership(**kwargs):
        executor = kwargs["executor"]
        for operation, native_identity in membership_operation_plan(
            symbols, identity.completed_session_date
        ):
            executor.execute(operation, native_identity, lambda claim: None)
        return QuantMembershipOutcomeV1(
            identity,
            None,
            (),
            symbols,
            7,
            "ok",
            "ok",
        )

    monkeypatch.setattr(module, "run_quant_membership_acquisition", membership)
    monkeypatch.setattr(
        module,
        "run_quant_recurring_shadow",
        lambda **kwargs: ("skipped", "test_isolation"),
    )
    root = tmp_path / "shadow"
    module.run_production_cadence_once(
        root=root,
        policy_version=policy.policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=slot + timedelta(seconds=1),
        deps=deps,
    )

    with sqlite3.connect(root / "ops" / "ops_kernel.sqlite3") as connection:
        rows = connection.execute(
            "SELECT operation,started_at,state FROM cadence_wire_starts "
            "WHERE operation LIKE 'MEMBERSHIP_%' "
            "OR operation IN ('ACCOUNTS','ACCOUNT_HOLDINGS') "
            "ORDER BY started_at"
        ).fetchall()
    membership_rows = [row for row in rows if row[0].startswith("MEMBERSHIP_")]
    holdings_rows = [row for row in rows if row[0] in {"ACCOUNTS", "ACCOUNT_HOLDINGS"}]
    assert len(membership_rows) == 7
    assert holdings_rows
    assert all(row[2] == "SUCCEEDED" for row in membership_rows)
    assert max(row[1] for row in membership_rows) < min(
        row[1] for row in holdings_rows
    )


def test_quant_receipt_store_is_not_created_by_absent_read_probe(monkeypatch, tmp_path):
    import tokmo.programs.production_cadence as module

    class Bomb:
        def __init__(self, path):
            raise AssertionError(f"receipt store created: {path}")

    path = tmp_path / "ops" / "quant_recurring.sqlite3"
    monkeypatch.setattr(module, "QuantRecurringReceiptStore", Bomb)
    assert module._existing_quant_receipt_store(path) is None
    assert not path.exists()


def test_budget_refusal_does_not_create_or_reconcile_quant_receipts(
    monkeypatch, tmp_path,
):
    import tokmo.programs.production_cadence as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    calls = []
    existing = type("Existing", (), {
        "reconcile_stale": lambda self, **kwargs: calls.append(kwargs),
    })()
    context = type("Context", (), {
        "decision": QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE,
    })()

    class Bomb:
        def __init__(self, path):
            raise AssertionError(f"receipt store created: {path}")

    monkeypatch.setattr(module, "QuantRecurringReceiptStore", Bomb)
    path = tmp_path / "ops" / "quant_recurring.sqlite3"
    result = module._prepare_quant_receipts_for_due(
        path=path,
        existing=existing,
        context=context,
        now=datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
    )
    assert result is existing
    assert calls == []
    assert not path.exists()


def test_m3_formula_is_single_sourced_without_fixed_plan_semantics():
    values = (ROOT / "src" / "tokmo" / "values" / "quant_recurring.py").read_text()
    runtime = (
        ROOT / "src" / "tokmo" / "programs" / "quant_recurring_shadow.py"
    ).read_text()

    assert "MEMBERSHIP_START_PLAN" not in values + runtime
    assert "QUANT_DUE_MAX_INCREMENT" not in values + runtime
    assert "def membership_plan_starts(" in values
    assert "def quant_due_max_increment(" in values
    assert "3 + len(predicted_symbols)" not in runtime
    assert "planned_starts=15" not in runtime


def test_implementation_is_bound_to_exact_pass_four_design_hashes():
    paths = {
        "analysis": WORKTREE_ROOT
        / ".ai"
        / "analysis"
        / "2026-08-14_quant-recurring-composition-impact.md",
        "sds": WORKTREE_ROOT
        / ".ai"
        / "plans"
        / "2026-08-14_quant-recurring-composition-plan-sds-v0.1.md",
    }
    assert {name: sha256(path.read_bytes()).hexdigest() for name, path in paths.items()} == {
        "analysis": "44e1c5bd0260c4f296461a5f8aa38c5dee4a44b82c3ca7ea14c5cb9cc484950c",
        "sds": "5efb1a285e850de7733b98174d990fa6b8059a2bd76468cb09ae1d24056fa584",
    }


def test_optional_quant_dependencies_are_callable_or_none():
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
    from tokmo.programs.production_cadence import ProductionCadenceRefusal

    deps, _ = build_fixture_cadence_deps(
        fixture={"accounts": [{"seed": "1", "holdings": []}]},
        clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    for name in ("membership_fetch", "sec_user_agent", "sic_snapshot_loader"):
        with pytest.raises(ProductionCadenceRefusal):
            replace(deps, **{name: object()})


def test_legacy_policy_records_two_inactive_rows_with_zero_quant_effect(tmp_path):
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
    from tokmo.programs.production_cadence import run_production_cadence_once
    from tokmo.values.cadence_policy import CADENCE_POLICY_V1

    class Bomb:
        def __call__(self, *args, **kwargs):
            raise AssertionError("inactive quant dependency called")

    slot = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
    observed = slot + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture={
            "token": "fixture-bearer",
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
            "accounts": [{"seed": "1", "holdings": []}],
        },
        clock=lambda: observed,
    )
    deps = replace(
        deps,
        membership_fetch=Bomb(),
        sec_user_agent=Bomb(),
        sic_snapshot_loader=Bomb(),
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root,
        policy_version=CADENCE_POLICY_V1.policy_version,
        scheduled_for_utc=slot,
        observed_now_utc=observed,
        deps=deps,
    )
    statuses = {item.stage: (item.status, item.reason) for item in result.stages}
    assert len(result.stages) == 19
    assert statuses["quant_membership"] == ("skipped", "policy_not_pinned")
    assert statuses["quant_shadow"] == ("skipped", "policy_not_pinned")
    assert probe.calls == {
        "TOKEN": 1,
        "CALENDAR_DAY_KR": 1,
        "CALENDAR_DAY_US": 1,
    }
    assert not (root / "ops" / "quant_recurring.sqlite3").exists()
    assert not (root / "official-membership" / "observations.sqlite3").exists()
    assert not (root / "official-membership" / "campaign.sqlite3").exists()


def test_real_composition_wires_quant_dependencies_and_loads_sec_config_lazily(
    monkeypatch, tmp_path,
):
    import tokmo.composition as composition

    (tmp_path / composition.TOSS_ENV_NAME).write_text(
        "TOSS_CLIENT_ID=fixture-id\nTOSS_CLIENT_SECRET=fixture-secret\n"
    )
    watchlists = tmp_path / "watchlists"
    watchlists.mkdir()
    for market in ("KR", "US"):
        (watchlists / f"{market}.json").write_text(json.dumps({
            "schema_version": 1,
            "market": market,
            "entries": [],
        }))
    calls = []

    def forbidden_at_build(path):
        calls.append(path)
        raise RuntimeError("lazy sec config proof")

    monkeypatch.setattr(composition, "load_sec_config", forbidden_at_build)
    deps = composition.build_production_cadence_deps(
        project_root=tmp_path,
        shadow_root=tmp_path / "shadow",
        clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    assert calls == []
    assert callable(deps.membership_fetch)
    assert callable(deps.sec_user_agent)
    assert callable(deps.sic_snapshot_loader)
    with pytest.raises(RuntimeError, match="lazy sec config proof"):
        deps.sec_user_agent()
    assert calls == [Path.home() / ".config" / "tokmo-v3" / "sec.env"]


def test_w3_registration_does_not_change_active_market_policy():
    import tools.run_tokmo_cadence as runner
    from tokmo.values.cadence_policy import (
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_POLICY_REGISTRY,
        CADENCE_QUANT_SHADOW_POLICY_V1,
    )

    assert runner.ACTIVE_POLICY is CADENCE_MARKET_PORTFOLIO_POLICY_V1
    assert (CADENCE_POLICY_REGISTRY[CADENCE_QUANT_SHADOW_POLICY_V1.policy_version]
            is CADENCE_QUANT_SHADOW_POLICY_V1)
    assert runner.ACTIVE_POLICY.policy_version == "cadence-300s-market-portfolio-v1"
    assert runner.ACTIVE_POLICY.request_limits.membership_starts == 0
    assert runner.ACTIVE_POLICY.request_limits.quant_daily_candle_starts == 0
