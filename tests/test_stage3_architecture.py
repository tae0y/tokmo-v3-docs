"""Stage 3 architecture seals: read-only production graph, mandatory executor.

The production cadence graph (orchestrator, wire executor, fixture deps, tick
receipt store, cadence values) must be transitively free of live transports,
the composition root, sealed legacy G2 surfaces, the G2 acceptance runners,
the model pipeline, and every order-write capability; the CLI seam is owned
by ``tokmo.composition:cadence_main``; and no provider callback is reachable
without an executor-issued claim.
"""

import ast
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "tokmo"

STAGE3_MODULES = (
    "programs/production_cadence.py",
    "programs/cadence_wire.py",
    "programs/cadence_fixture_deps.py",
    "facts/market_regime_toss_input.py",
    "programs/market_regime_shadow.py",
    "programs/quant_recurring_shadow.py",
    "facts/quant_recurring_membership.py",
    "records/quant_recurring_receipts.py",
    "values/quant_recurring.py",
    "records/market_regime_shadow_state.py",
    "records/cadence_tick_receipts.py",
    "records/regular_session_history.py",
    "values/production_cadence.py",
)

# Modules the production cadence graph must never reach, directly or
# transitively: live transports and their composition owner, ambient clock,
# sealed legacy G2 wrappers, the bounded G2 acceptance runners, and the
# model-calling analysis pipeline.
FORBIDDEN_GRAPH_MODULES = frozenset({
    "tokmo.composition",
    "tokmo.runtime.wallclock",
    "tokmo.runtime.config",
    "tokmo.facts.eia_live", "tokmo.facts.sec_live", "tokmo.facts.opendart_live",
    "tokmo.facts.krx_live",
    "tokmo.facts.toss_auth_live", "tokmo.facts.toss_live",
    "tokmo.facts.toss_prices_live", "tokmo.facts.toss_reads_live",
    "tokmo.programs.legacy_g2",
    "tokmo.programs.g2a_shadow_runner", "tokmo.programs.g2b_canary_runner",
    "tokmo.programs.g2c_analysis_shadow", "tokmo.programs.g2d_full_session_runner",
    "tokmo.programs.integrated_fake_cadence",
    "tokmo.programs.analysis_pipeline",
})

LEGACY_ADMISSION_NAMES = frozenset({
    "plan_requests", "arm_account_rows", "start_request", "settle_request",
    "reserve_request",
})

DEPS_PROVIDER_FIELDS = (
    "token_fetch", "calendar_fetch", "accounts_fetch",
    "account_holdings_fetch", "quotes_fetch", "orders_page_fetch",
    "order_detail_fetch", "stock_candles_fetch",
)


def _module_name(relative: str) -> str:
    return "tokmo." + relative.removesuffix(".py").replace("/", ".")


def _tokmo_imports(module: str) -> set[str]:
    path = SRC / (module.removeprefix("tokmo.").replace(".", "/") + ".py")
    if not path.is_file():
        return set()
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("tokmo"):
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(
                alias.name for alias in node.names
                if alias.name.startswith("tokmo")
            )
    return names


def transitive_closure(module: str) -> set[str]:
    seen: set[str] = set()
    frontier = [module]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        frontier.extend(_tokmo_imports(current))
    return seen


def test_stage3_graph_is_transitively_free_of_forbidden_modules():
    for relative in STAGE3_MODULES:
        closure = transitive_closure(_module_name(relative))
        leaked = closure & FORBIDDEN_GRAPH_MODULES
        assert not leaked, f"{relative} transitively reaches {sorted(leaked)}"


def test_stage3_modules_never_name_the_legacy_admission_surface():
    for relative in STAGE3_MODULES:
        tree = ast.parse((SRC / relative).read_text())
        used = {
            node.attr for node in ast.walk(tree) if isinstance(node, ast.Attribute)
        } | {
            node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
        }
        leaked = used & LEGACY_ADMISSION_NAMES
        assert not leaked, f"{relative} names legacy admission surface {leaked}"


def test_stage3_modules_carry_no_order_write_vocabulary():
    import re
    for relative in STAGE3_MODULES:
        text = (SRC / relative).read_text()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not re.search(rf"\b{method}\b", text), (relative, method)
        assert "oauth" not in text.lower(), relative


def test_production_cadence_deps_fields_are_exactly_the_read_only_contract():
    from tokmo.programs.production_cadence import ProductionCadenceDepsV1

    assert tuple(ProductionCadenceDepsV1.__dataclass_fields__) == (
        "mode", "clock", *DEPS_PROVIDER_FIELDS,
        "regular_session_dates", "lifecycle_sink", "watchlist_symbols",
        "analysis_execution",
        "pension_registry_path", "pension_composition_enabled",
        "pension_reminder_send_enabled", "pension_unquotable_symbols",
        "membership_fetch", "sec_user_agent", "sic_snapshot_loader",
        "stocks_membership_fetch",
    )


def test_fixture_providers_reject_claimless_calls_before_any_data():
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps

    deps, _probe = build_fixture_cadence_deps(
        fixture={"accounts": [{"seed": "1", "holdings": []}]},
        clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
    )
    with pytest.raises(PermissionError):
        deps.token_fetch(None)
    with pytest.raises(PermissionError):
        deps.accounts_fetch(claim=None, bearer="fixture-bearer")
    with pytest.raises(PermissionError):
        deps.quotes_fetch(claim=object(), bearer="fixture-bearer", symbols=("A",))
    with pytest.raises(PermissionError):
        deps.orders_page_fetch(
            claim=None, bearer="fixture-bearer", account_scope="s",
            native_ref="1", status="CLOSED", from_date="2026-08-01",
            to_date="2026-08-03", cursor=None, limit=100,
        )
    with pytest.raises(PermissionError):
        deps.order_detail_fetch(
            claim=None, bearer="fixture-bearer", account_scope="s",
            native_ref="1", order_id="o",
        )
    with pytest.raises(PermissionError):
        deps.stock_candles_fetch(
            claim=None, bearer="fixture-bearer", symbol="005930",
            before=datetime(2026, 8, 3, tzinfo=timezone.utc), adjusted=True,
        )
    assert _probe.calls == {}


def test_every_executed_provider_callback_carried_a_genuine_live_claim(tmp_path):
    """Runtime proof of B: one full open tick's non-calendar callbacks each
    received an executor-issued claim, and their count equals the kernel's
    STARTED receipts for those operations (no unreceipted callback)."""
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
    from tokmo.programs.cadence_wire import WireClaimV1
    from tokmo.programs.production_cadence import run_production_cadence_once
    from tokmo.records.ops_kernel import OpsKernelStore

    slot = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    observed = slot + timedelta(seconds=1)
    deps, probe = build_fixture_cadence_deps(
        fixture={
            "token": "claim-proof-bearer",
            "calendar": {
                "KR": {"regular_start": "2026-08-03T00:00:00Z",
                       "regular_end": "2026-08-03T06:30:00Z"},
                "US": {"regular_start": "2026-08-03T13:30:00Z",
                       "regular_end": "2026-08-03T20:00:00Z"},
            },
            "accounts": [{"seed": "9", "holdings": [
                {"symbol": "AAPL", "quantity": "1", "avg_price": "100",
                 "currency": "USD", "market_raw": "US"},
            ]}],
            "quotes": {"AAPL": {"price": "101", "currency": "USD"}},
        },
        clock=lambda: observed,
    )
    root = tmp_path / "shadow"
    result = run_production_cadence_once(
        root=root, policy_version="cadence-300s-v1", scheduled_for_utc=slot,
        observed_now_utc=observed, deps=deps,
    )
    assert result.outcome.value == "COMPLETE"
    assert all(type(claim) is WireClaimV1 for claim in probe.claims)
    report = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3"
    ).read_cadence_tick_report("cadence-300s-v1", slot)
    non_calendar_started = sum(
        count for operation, count in report["operation_started"].items()
        if not operation.startswith("CALENDAR_DAY_")
    )
    assert len(probe.claims) == non_calendar_started
    assert report["operation_started"]["TOKEN"] == 1  # no hidden token path


# -- CLI ownership (SDS §9): tokmo-cadence -> tokmo.composition:cadence_main -------


def test_pyproject_declares_the_cadence_cli_owner():
    text = (ROOT / "pyproject.toml").read_text()
    assert 'tokmo-cadence = "tokmo.composition:cadence_main"' in text


def test_cadence_main_exists_and_calls_the_production_entrypoint_once():
    tree = ast.parse((SRC / "composition.py").read_text())
    target = None
    for node in tree.body:
        if isinstance(node, ast.FunctionDef) and node.name == "cadence_main":
            target = node
    assert target is not None, "composition.cadence_main missing"
    calls = [
        child for child in ast.walk(target)
        if isinstance(child, ast.Call)
        and (
            (isinstance(child.func, ast.Name)
             and child.func.id == "run_production_cadence_once")
            or (isinstance(child.func, ast.Attribute)
                and child.func.attr == "run_production_cadence_once")
        )
    ]
    assert len(calls) == 1, "cadence_main must call run_production_cadence_once exactly once"
    # G2-D imports/calls are forbidden from this path (SDS §9).
    named = {
        child.attr for child in ast.walk(target) if isinstance(child, ast.Attribute)
    } | {
        child.id for child in ast.walk(target) if isinstance(child, ast.Name)
    }
    forbidden = {
        "run_d_full_session", "build_g2d_live_deps", "G2DReport",
        "run_b_canary", "run_a_one_hour", "run_integrated_fake_day",
    }
    assert not named & forbidden


def test_real_deps_builder_constructs_without_provider_calls(tmp_path):
    """The real read-only builder is constructible from the official adapters
    with zero provider calls and zero build-time I/O: it wires the durable
    regular-session history as the trades session-date source, and an empty
    history is a truthful ``failed_calendar``, never a weekday guess.

    The builder loads canonical watchlists from ``project_root / watchlists``.
    A missing registry must fail closed, so this test creates valid empty
    KR/US fixture registries to isolate from the real project watchlists
    while preserving the production loading path."""
    from tokmo.composition import TOSS_ENV_NAME, build_production_cadence_deps
    from tokmo.programs.production_cadence import ProductionCadenceDepsV1
    from tokmo.records.regular_session_history import HistorySessionDatesV1
    from tokmo.values.session_gate import Market

    (tmp_path / TOSS_ENV_NAME).write_text(
        "TOSS_CLIENT_ID=fixture-id\nTOSS_CLIENT_SECRET=fixture-secret\n"
    )

    # Create valid empty watchlist registries so the builder's production
    # load_watchlists path succeeds with zero targets (not a fail-closed
    # registry_unavailable from a missing directory).
    import json
    watchlists_dir = tmp_path / "watchlists"
    watchlists_dir.mkdir()
    empty_kr = {
        "schema_version": 1, "market": "KR", "entries": [],
    }
    empty_us = {
        "schema_version": 1, "market": "US", "entries": [],
    }
    (watchlists_dir / "KR.json").write_text(json.dumps(empty_kr))
    (watchlists_dir / "US.json").write_text(json.dumps(empty_us))

    deps = build_production_cadence_deps(
        project_root=tmp_path,
        clock=lambda: datetime(2026, 8, 3, tzinfo=timezone.utc),
        shadow_root=tmp_path / "shadow",
        https_handler_factory=None,
    )
    assert isinstance(deps, ProductionCadenceDepsV1)
    assert deps.mode == "live_shadow"
    assert isinstance(deps.regular_session_dates, HistorySessionDatesV1)
    assert not (tmp_path / "shadow").exists()  # zero build-time I/O
    # empty durable history: truthful failed_calendar, no fabricated weekday
    with pytest.raises(ValueError):
        deps.regular_session_dates.regular_session_dates(
            Market.KR, datetime(2026, 8, 3, tzinfo=timezone.utc).date(), 20,
        )
    assert not (tmp_path / "shadow").exists()  # reads never create the store
