"""F14 narrow architecture closure for integrated cadence composition.

Broad provider-path/network ownership remains in test_architecture.py and the
Slice B/C/D/E/F8/F9 guards.  This file only closes the F composition edges.
"""

import ast
from dataclasses import fields
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokmo.programs.integrated_fake_cadence import (
    IntegratedCadenceDepsV1,
    run_integrated_fake_day,
)

ROOT = Path(__file__).parents[1]
INTEGRATED = ROOT / "src/tokmo/programs/integrated_fake_cadence.py"
COMPOSITION_SURFACE = (
    INTEGRATED,
    ROOT / "src/tokmo/programs/holdings_sync.py",
    ROOT / "src/tokmo/programs/quotes_sync.py",
    ROOT / "src/tokmo/programs/trades_sync.py",
    ROOT / "src/tokmo/programs/analysis_barrier.py",
    ROOT / "src/tokmo/programs/analysis_pipeline.py",
)
EXACT_DEPS = (
    "calendar_provider",
    "holdings_provider_factory",
    "quotes_provider_factory",
    "orders_provider_factory",
    "detail_provider_factory",
    "glm_call",
    "sol_call",
    "regular_session_dates",
    "clock",
    # numeric wave (G2-D §10.9) SDS amendment: optional masked per-tick
    # numeric-evidence hook, default None (all pre-G2-D composers unchanged).
    "numeric_evidence_sink",
)
FORBIDDEN_IMPORT_ROOTS = {
    "socket",
    "ssl",
    "subprocess",
    "requests",
    "httpx",
    "aiohttp",
    "telegram",
    "telebot",
    "aiogram",
    "openai",
    "anthropic",
    "croniter",
}
FORBIDDEN_TOKMO_PREFIXES = (
    "tokmo.composition",
    "tokmo.runtime",
    "tokmo.facts.toss_auth_live",
    "tokmo.facts.toss_live",
    "tokmo.facts.toss_prices_live",
    "tokmo.facts.toss_reads_live",
)
ORDER_WRITE_TOKENS = {
    "place_order",
    "create_order",
    "submit_order",
    "modify_order",
    "amend_order",
    "cancel_order",
    "conditional_order_create",
}


def _imports(source: str) -> set[str]:
    result = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            result.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            result.add(node.module)
    return result


def _composition_violations(source: str) -> set[str]:
    tree = ast.parse(source)
    violations = set()
    for name in _imports(source):
        if name.split(".", 1)[0] in FORBIDDEN_IMPORT_ROOTS:
            violations.add("external-import")
        if name.startswith(FORBIDDEN_TOKMO_PREFIXES):
            violations.add("live-or-private-import")
        if any(token in name.lower() for token in ORDER_WRITE_TOKENS):
            violations.add("order-write-import")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and any(
            alias.name.lower() in ORDER_WRITE_TOKENS for alias in node.names
        ):
            violations.add("order-write-import")
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = (
                node.func.id
                if isinstance(node.func, ast.Name)
                else node.func.attr
                if isinstance(node.func, ast.Attribute)
                else ""
            ).lower()
            if name in ORDER_WRITE_TOKENS:
                violations.add("order-write-call")
            generic_wire_keywords = {keyword.arg for keyword in node.keywords}
            if name in {"urlopen", "getaddrinfo", "popen"} or (
                name == "request"
                and generic_wire_keywords & {"url", "method", "headers", "body"}
            ):
                violations.add("direct-transport-call")
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            parameters = {
                arg.arg
                for arg in (
                    *node.args.posonlyargs,
                    *node.args.args,
                    *node.args.kwonlyargs,
                )
            }
            if {"url", "method", "headers", "body"} <= parameters:
                violations.add("generic-network-surface")
    return violations


def test_f14_integrated_dependency_contract_exactly_matches_sds():
    assert tuple(field.name for field in fields(IntegratedCadenceDepsV1)) == EXACT_DEPS
    function = next(
        node
        for node in ast.parse(INTEGRATED.read_text()).body
        if isinstance(node, ast.FunctionDef) and node.name == "run_integrated_fake_day"
    )
    assert [arg.arg for arg in function.args.kwonlyargs] == [
        "utc_day",
        "root",
        "deps",
        "compressed",
    ]


def test_f14_composition_has_no_live_private_external_or_order_write_reachability():
    for path in COMPOSITION_SURFACE:
        assert _composition_violations(path.read_text()) == set(), path


@pytest.mark.parametrize(
    "mutation, expected",
    (
        ("from tokmo.orders.write import cancel_order", "order-write-import"),
        (
            "import requests\ndef leak(): requests.get('https://example.invalid')",
            "external-import",
        ),
        (
            "def leak():\n    client.request(url='x', method='GET', headers={}, body=None)",
            "direct-transport-call",
        ),
    ),
)
def test_f14_guard_mutation_kills_order_write_and_unmetered_transport(
    mutation, expected
):
    assert expected in _composition_violations(mutation)


def test_f14_integrated_owns_one_kernel_and_one_day_lock_authority():
    tree = ast.parse(INTEGRATED.read_text())
    calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    ]
    assert calls.count("OpsKernelStore") == 1
    assert calls.count("_acquire_day_lock") == 1


def test_f14_production_root_escape_is_denied_before_dependency_use(
    tmp_path, monkeypatch
):
    called = []
    production = tmp_path / "private-production"
    production.mkdir()
    monkeypatch.setattr(
        "tokmo.programs.integrated_fake_cadence._production_root", lambda: production
    )
    # A deliberately invalid dependency object is sufficient: root rejection must
    # happen before any factory/provider can be reached.
    deps = object.__new__(IntegratedCadenceDepsV1)
    for escaped in (production, production / "child", production.parent):
        with pytest.raises(ValueError, match="production root denied"):
            run_integrated_fake_day(utc_day=date(2026, 7, 30), root=escaped, deps=deps)
    assert called == []


def test_f14_guard_mutation_kills_dependency_field_drift():
    actual = tuple(field.name for field in fields(IntegratedCadenceDepsV1))
    assert actual == EXACT_DEPS
    assert actual != EXACT_DEPS[:-1]
    assert actual != (EXACT_DEPS[1], EXACT_DEPS[0], *EXACT_DEPS[2:])


def test_f14_public_entrypoint_composes_production_stores_and_gate_bound_factories(
    tmp_path, monkeypatch
):
    import socket

    from tokmo.values.toss_market_calendar import (
        IntegratedHour,
        KrMarketDay,
        UsMarketDay,
    )

    root = tmp_path / "public-composition"
    now = datetime(2026, 7, 30, tzinfo=UTC)
    calendar_calls = []
    factory_gates = {name: [] for name in ("holdings", "quotes", "orders", "detail")}
    provider_calls = []
    network = []

    def forbidden(*args, **kwargs):
        network.append((args, kwargs))
        raise AssertionError("network forbidden")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)

    def calendar_provider(*, market, local_trade_date, tick, request_gate):
        calendar_calls.append(
            (market.value, local_trade_date, tick.tick_id, id(request_gate))
        )
        if market.value == "KR":
            return KrMarketDay(local_trade_date, IntegratedHour(regular_market=None))
        return UsMarketDay(local_trade_date, regular_market=None)

    def factory(name):
        def bind(gate):
            factory_gates[name].append(gate)

            def provider(*args, **kwargs):
                provider_calls.append((name, args, kwargs))
                raise AssertionError("closed market provider must not run")

            return provider

        return bind

    class Dates:
        def regular_session_dates(self, market, anchor_date, count):
            return tuple(anchor_date for _ in range(count))

    deps = IntegratedCadenceDepsV1(
        calendar_provider=calendar_provider,
        holdings_provider_factory=factory("holdings"),
        quotes_provider_factory=factory("quotes"),
        orders_provider_factory=factory("orders"),
        detail_provider_factory=factory("detail"),
        glm_call=lambda _body: pytest.fail("no analysis candidates"),
        sol_call=lambda _body: pytest.fail("no analysis candidates"),
        regular_session_dates=Dates(),
        clock=lambda: now,
    )
    report = run_integrated_fake_day(utc_day=now.date(), root=root, deps=deps)

    assert report.tick_count == 288
    assert calendar_calls
    assert all(len(factory_gates[name]) == 1 for name in factory_gates)
    gate_ids = {id(gate) for gates in factory_gates.values() for gate in gates}
    assert len(gate_ids) == 1
    assert provider_calls == []
    assert network == [] and report.socket_count == report.dns_count == 0
    assert (root / "ops" / "ops_kernel.sqlite3").is_file()
    assert (root / "ops" / "integrated_cadence.sqlite3").is_file()
    assert all(
        (root / "ops" / "receipts" / f"{job}.json").is_file()
        for job in ("holdings", "quotes", "trades")
    )


def test_f14_mutation_kills_direct_stage_bypass_or_reordering():
    tree = ast.parse(INTEGRATED.read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_production_stage_deps"
    )
    returned = next(
        node
        for node in ast.walk(helper)
        if isinstance(node, ast.Return)
        and isinstance(node.value, ast.Call)
        and getattr(node.value.func, "id", "") == "_IntegratedStageDepsV1"
    )
    order = tuple(arg.id for arg in returned.value.args[:-1])
    expected = (
        "calendar",
        "session",
        "holdings",
        "quotes",
        "trades",
        "barrier",
        "analysis",
    )
    assert order == expected
    assert order != expected[:2] + expected[3:]
    assert order != expected[:2] + ("quotes", "holdings") + expected[4:]
