"""Delta SDS §2/§9 acceptance 12, 13, 14: the routine-query architecture seal.

Seals the sealed routine paths, the independence of the value graph from the
R1-R4 campaign vocabulary, the layer import directions, the fixture/live
transport type parity, and zero network, credential-file and production-root
access on the whole G1 fixture path.
"""

import ast
import builtins
import os
import socket
import sqlite3
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

SOURCE_PATHS = (
    "src/tokmo/values/fred_query.py",
    "src/tokmo/facts/fred_query.py",
    "src/tokmo/records/fred_query_cache.py",
    "src/tokmo/programs/macro_fred_inputs.py",
    "src/tokmo/fred_query_composition.py",
    "src/tokmo/records/fred_schedule_sidecar.py",
)
TEST_PATHS = (
    "tests/test_fred_query_values.py",
    "tests/test_fred_query_fact.py",
    "tests/test_fred_query_cache.py",
    "tests/test_macro_fred_inputs.py",
    "tests/test_fred_query_architecture.py",
    "tests/test_macro_fred_caller.py",
    "tests/test_fred_schedule_sidecar.py",
)

# R1-R4 campaign history must stay unreachable from the routine query path.
CAMPAIGN_TOKENS = (
    "campaign", "r2_g_live", "r3_g_live", "r4_g_live", "g_live_manifest",
    "execute_r4", "manifest_digest", "manifest_revision", "manifest_code",
    "quarantine", "invocation_identity", "high_water", "fredmaterialstore",
    "fred_material_store", "reserve_campaign", "ordinal", "campaign_cap",
    "purpose_cumulative_ceiling", "store_material", "store_page",
)
# No scheduler, alert or order effect may be named anywhere in the new paths.
EFFECT_TOKENS = (
    "cron", "launchagent", "launchd", "plist", "telegram", "webhook", "smtp",
    "place_order", "submit_order", "order_id", "orderid", "send_alert",
)


def _imports(relative):
    names = []
    for node in ast.walk(ast.parse((ROOT / relative).read_text())):
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
            names.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _tokmo_imports(relative):
    return {name for name in _imports(relative) if name.startswith("tokmo")}


def _symbols(relative):
    """Every identifier, import name and non-docstring literal, lowercased.

    Prose in a module docstring may name the boundary it is separated from;
    a reachable symbol may not.
    """
    tree = ast.parse((ROOT / relative).read_text())
    docstrings = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef,
                                 ast.AsyncFunctionDef)) or not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            docstrings.add(id(first.value))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            found.add(node.id)
        elif isinstance(node, ast.Attribute):
            found.add(node.attr)
        elif isinstance(node, ast.arg):
            found.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            found.add(node.name)
        elif isinstance(node, ast.alias):
            found.update((node.name, node.asname or ""))
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
        elif isinstance(node, ast.keyword) and node.arg:
            found.add(node.arg)
        elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                and id(node) not in docstrings):
            found.add(node.value)
    return {item.lower() for item in found if item}


def test_the_sealed_routine_paths_exist_and_nothing_else_was_added():
    for relative in SOURCE_PATHS + TEST_PATHS:
        assert (ROOT / relative).is_file(), relative
    discovered = {
        path.relative_to(ROOT).as_posix()
        for base in (ROOT / "src", ROOT / "tools", ROOT / "tests")
        for path in base.rglob("*.py")
        if "__pycache__" not in path.parts
        and ("fred_query" in path.name or "macro_fred" in path.name
             or "fred_schedule" in path.name)
    }
    assert discovered == set(SOURCE_PATHS) | set(TEST_PATHS)


def test_values_fred_query_is_standalone_and_never_imports_the_campaign_values():
    names = _imports("src/tokmo/values/fred_query.py")
    assert "tokmo.values.fred_macro" not in names
    assert not any(name.startswith("tokmo") for name in names), names
    assert set(names) <= {"dataclasses", "dataclasses.dataclass", "datetime",
                          "datetime.date", "datetime.datetime",
                          "datetime.timedelta", "datetime.timezone", "decimal",
                          "decimal.Decimal", "enum", "enum.Enum", "hashlib",
                          "json"}, names


def test_facts_adapter_reaches_no_records_program_or_live_transport():
    names = _tokmo_imports("src/tokmo/facts/fred_query.py")
    assert names == {"tokmo.facts.fred", "tokmo.facts.fred.parse_fred_response",
                     "tokmo.values.fred_macro", "tokmo.values.fred_query"} | {
        name for name in names if name.startswith(("tokmo.values.fred_macro.",
                                                   "tokmo.values.fred_query."))}
    assert not any(name.startswith(("tokmo.records", "tokmo.programs",
                                    "tokmo.facts.fred_live", "tokmo.runtime",
                                    "tokmo.composition")) for name in names)


def test_records_cache_reaches_no_facts_program_or_live_transport():
    names = _tokmo_imports("src/tokmo/records/fred_query_cache.py")
    assert not any(name.startswith(("tokmo.facts", "tokmo.programs",
                                    "tokmo.runtime", "tokmo.composition"))
                   for name in names)
    assert "tokmo.values.fred_query" in names
    assert "tokmo.values.fred_macro" not in names


def test_macro_program_imports_only_the_routine_query_vocabulary():
    names = _tokmo_imports("src/tokmo/programs/macro_fred_inputs.py")
    assert {name.split(".fred_query")[0] + ".fred_query" for name in names} == {
        "tokmo.values.fred_query"}
    assert not any(name.startswith(("tokmo.facts", "tokmo.records",
                                    "tokmo.runtime", "tokmo.composition"))
                   for name in names)
    assert not any("market_regime" in name or "macro_runtime" in name
                   for name in names)


def test_only_composition_assembles_the_layers_and_it_stays_offline():
    names = _tokmo_imports("src/tokmo/fred_query_composition.py")
    assert "tokmo.facts.fred_query" in names
    assert "tokmo.records.fred_query_cache" in names
    assert not any(name.startswith("tokmo.facts.fred_live") for name in names)
    assert not any(name.startswith("tokmo.runtime") for name in names)
    source = (ROOT / "src/tokmo/fred_query_composition.py").read_text()
    assert "load_fred_api_key" not in source and "FredLiveTransport" not in source
    assert "Path.home()" not in source and "build_live_opener" not in source


def test_the_routine_path_names_no_campaign_or_r1_r4_symbol():
    for relative in SOURCE_PATHS:
        symbols = _symbols(relative)
        for token in CAMPAIGN_TOKENS:
            offenders = {symbol for symbol in symbols if token in symbol}
            assert not offenders, (relative, token, sorted(offenders))


def test_the_routine_path_names_no_scheduler_alert_or_order_effect():
    for relative in SOURCE_PATHS:
        symbols = _symbols(relative)
        for token in EFFECT_TOKENS:
            offenders = {symbol for symbol in symbols if token in symbol}
            assert not offenders, (relative, token, sorted(offenders))


def test_the_new_source_paths_keep_the_repository_layer_directions():
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "repository_architecture", ROOT / "tests/test_architecture.py")
    architecture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(architecture)
    modules = dict(architecture.tokmo_modules())
    for relative in SOURCE_PATHS:
        key = relative.removeprefix("src/tokmo/")
        assert key in modules, relative
        assert not any(name.startswith(architecture.LIVE_TRANSPORT_IMPORT_PREFIXES)
                       for name in modules[key]), relative
    architecture.test_layer_import_directions_match_contract()
    architecture.test_no_forbidden_provider_network_or_order_imports_anywhere()
    architecture.test_no_process_env_or_direct_clock_in_source()
    architecture.test_no_assert_statements_in_src()


def test_the_fixture_transport_has_type_parity_with_the_live_transport():
    from dataclasses import fields

    from tokmo.facts.fred_live import FAILURE_CODES, FredTransportOutcome
    from tokmo.facts.fred_query import TRANSPORT_FAILURE_MAP
    from tokmo.fred_query_composition import (
        FIXTURE_TRANSPORT_FAILURE_CODES, FredFixtureTransportOutcomeV1,
        FredFixtureTransportV1,
    )
    from tokmo.values.fred_query import FredQueryFailureCodeV1

    assert FIXTURE_TRANSPORT_FAILURE_CODES == FAILURE_CODES
    assert set(TRANSPORT_FAILURE_MAP) == set(FAILURE_CODES)
    assert set(TRANSPORT_FAILURE_MAP.values()) <= set(FredQueryFailureCodeV1)
    assert ([field.name for field in fields(FredFixtureTransportOutcomeV1)]
            == [field.name for field in fields(FredTransportOutcome)])
    with pytest.raises(ValueError, match="closed_transport_code"):
        FredFixtureTransportOutcomeV1("teapot")
    with pytest.raises(ValueError, match="transport_body_contract"):
        FredFixtureTransportOutcomeV1("success")
    transport = FredFixtureTransportV1(outcomes=())
    assert transport.__call__.__code__.co_varnames[:3] == (
        "self", "request", "api_key")


def test_market_regime_core_is_untouched_by_this_delta():
    for relative in ("src/tokmo/programs/market_regime_core.py",
                     "src/tokmo/values/market_regime_core.py"):
        text = (ROOT / relative).read_text().lower()
        assert "fred" not in text, relative
        assert "macro_fred" not in text, relative
    from tokmo.programs.market_regime_core import evaluate_market_regime_core_v1
    from tokmo.values.fred_query import MacroFredInputV1

    assert evaluate_market_regime_core_v1.__code__.co_varnames[:1] == ("series",)
    assert not {"regime", "pack_digest", "bars"} & set(
        MacroFredInputV1.__dataclass_fields__)


def test_g1_fixture_path_opens_no_socket_credential_or_production_root(
        tmp_path, monkeypatch):
    import json

    from tokmo.fred_query_composition import (
        FredFixtureTransportOutcomeV1, build_g1_fixture_fred_query_deps_v1,
    )
    from tokmo.programs.macro_fred_inputs import run_macro_fred_enrichment_v1
    from tokmo.values.fred_query import (
        SERIES_PAYEMS, FredCoverageV1, FredReleaseScheduleV1, FredSeriesQueryV1,
    )

    utc = timezone.utc
    now = datetime(2026, 8, 7, 12, 45, tzinfo=utc)
    opened, connected = [], []
    real_open, real_os_open = builtins.open, os.open
    real_connect = sqlite3.connect

    def guarded_open(file, *args, **kwargs):
        opened.append(str(file))
        return real_open(file, *args, **kwargs)

    def guarded_os_open(path, *args, **kwargs):
        opened.append(str(path))
        return real_os_open(path, *args, **kwargs)

    def guarded_connect(target, *args, **kwargs):
        connected.append(str(target))
        return real_connect(target, *args, **kwargs)

    def forbidden(*args, **kwargs):
        raise AssertionError("network_forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(os, "open", guarded_os_open)
    monkeypatch.setattr(sqlite3, "connect", guarded_connect)

    body = json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": "2026-06-01", "observation_end": "2026-07-31",
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": 1, "offset": 0, "limit": 2,
        "observations": [{"realtime_start": "2026-08-07",
                          "realtime_end": "9999-12-31",
                          "date": "2026-07-01", "value": "151000"}]}).encode()
    deps = build_g1_fixture_fred_query_deps_v1(
        root=tmp_path / "fred-query",
        outcomes=(FredFixtureTransportOutcomeV1("success", body),),
        clock=lambda: now)
    result = run_macro_fred_enrichment_v1(
        query=FredSeriesQueryV1(SERIES_PAYEMS, date(2026, 6, 1),
                                date(2026, 7, 31), now),
        schedule=FredReleaseScheduleV1(
            SERIES_PAYEMS, now - timedelta(minutes=15), "PAYEMS:2026-08-07",
            now + timedelta(days=28), "b" * 64),
        cache=deps.cache, query_port=deps.port, clock=lambda: now)

    assert result.coverage is FredCoverageV1.USED
    assert result.provider_starts == 1 and len(deps.transport.requests) == 1
    touched = opened + connected
    assert touched, "the guarded run touched no path at all"
    for path in touched:
        lowered = path.lower()
        assert ".hermes" not in lowered, path
        assert "credential" not in lowered and ".env" not in lowered, path
        assert "trade_ledger" not in lowered and "fred.sqlite3" not in lowered, path
        assert "launchagents" not in lowered, path
        resolved = Path(path)
        if resolved.is_absolute() and str(resolved).startswith(str(Path.home())):
            assert str(resolved).startswith(str(ROOT)), path
    for target in connected:
        cleaned = target.removeprefix("file://").split("?", 1)[0]
        assert cleaned.startswith(str(tmp_path)), target
