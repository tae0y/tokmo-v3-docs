"""Architecture guards for the official Toss order-currency contract."""

import ast
from pathlib import Path
from types import MappingProxyType

import tokmo.composition as composition
from tokmo.programs import (
    cadence_fixture_deps,
    integrated_fake_cadence,
    legacy_g2,
    production_cadence,
    trades_sync,
)


def test_no_market_resolver_production_dependency_or_wiring():
    assert not hasattr(trades_sync, "MarketResolver")
    scanned = (
        trades_sync,
        production_cadence,
        legacy_g2,
        integrated_fake_cadence,
        cadence_fixture_deps,
        composition,
    )
    assert all(
        "market_resolver" not in Path(module.__file__).read_text()
        for module in scanned
    )


def test_currency_market_contract_tables_are_immutable():
    assert isinstance(trades_sync._CURRENCY_MARKET, MappingProxyType)
    assert isinstance(trades_sync._MARKET_CURRENCY, MappingProxyType)


def test_trade_path_has_no_symbol_or_krx_registry_dependency():
    tree = ast.parse(Path(trades_sync.__file__).read_text())
    imported = {
        alias.name.lower()
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert not any("krx" in name or "registry" in name for name in imported)
    source = Path(trades_sync.__file__).read_text().lower()
    assert "symbol_exists" not in source
    assert "symbol_registry" not in source
