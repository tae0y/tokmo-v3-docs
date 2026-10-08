"""Architecture and release-blocking privacy contracts for portfolio calculations."""

import ast
from dataclasses import asdict, fields, is_dataclass
from pathlib import Path

from tokmo.values.portfolio_calculations import (
    PartitionPresentationV1,
    PortfolioPresentationV1,
    PositionPresentationV1,
    ScopePresentationV1,
)

ROOT = Path(__file__).parents[1]
VALUES = ROOT / "src/tokmo/values/portfolio_calculations.py"
PROGRAM = ROOT / "src/tokmo/programs/portfolio_calculations.py"
PORTFOLIO_TEST = ROOT / "tests/test_portfolio_calculations.py"


def _imports(path):
    tree = ast.parse(path.read_text())
    result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            result.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            result.append(node.module or "")
    return result


def test_pc13_values_module_is_frozen_dto_only():
    source = VALUES.read_text()
    tree = ast.parse(source)
    imports = _imports(VALUES)
    assert not any(name.startswith(("tokmo.records", "tokmo.facts", "tokmo.programs"))
                   for name in imports)
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            decorators = {ast.unparse(item) for item in node.decorator_list}
            if any(item.startswith("dataclass") for item in decorators):
                assert any("frozen=True" in item for item in decorators)
    assert "open(" not in source and "os.environ" not in source


def test_pc13_program_is_pure_and_has_no_forbidden_imports_or_float_math():
    source = PROGRAM.read_text()
    imports = _imports(PROGRAM)
    assert not any(name.startswith((
        "tokmo.records", "tokmo.facts", "tokmo.composition", "requests", "httpx",
        "urllib", "socket", "logging", "sqlite3",
    )) for name in imports)
    tree = ast.parse(source)
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                   and node.func.id == "float" for node in ast.walk(tree))
    assert not any(isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                   and node.func.attr == "quantize" for node in ast.walk(tree))
    assert "latest" not in source.lower()


def test_pc13_presentation_schema_blocks_private_and_converted_principal_fields():
    projection_classes = (
        PortfolioPresentationV1, PositionPresentationV1,
        PartitionPresentationV1, ScopePresentationV1,
    )
    forbidden_exact = {
        "account_scope", "account_id", "account_number", "source_record_key",
        "average_cost_krw", "current_price_krw", "total_cost_basis_krw",
        "priced_cost_basis_krw", "converted_krw",
        "internal_total_cost_basis_krw", "internal_priced_cost_basis_krw",
    }
    allowed_krw = {
        "evaluated_market_value_krw", "unrealized_pnl_krw",
    }
    for cls in projection_classes:
        assert is_dataclass(cls)
        names = {field.name for field in fields(cls)}
        assert not names & forbidden_exact
        for name in names:
            if name.endswith("_krw"):
                assert name in allowed_krw
        assert not any(token in name.lower() for name in names
                       for token in ("secret", "token", "auth", "path", "exception"))


def test_pc13_canonical_calculation_dto_has_no_direct_identity_fields():
    tree = ast.parse(VALUES.read_text())
    forbidden = {"account_scope", "account_id", "account_number", "source_record_key"}
    for node in tree.body:
        if not isinstance(node, ast.ClassDef):
            continue
        annotated = {
            child.target.id for child in node.body
            if isinstance(child, ast.AnnAssign) and isinstance(child.target, ast.Name)
        }
        assert not annotated & forbidden, node.name


def test_pc13_rejects_dynamic_import_calls_in_program_and_contract_tests():
    for path in (PROGRAM, PORTFOLIO_TEST, Path(__file__)):
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            assert not (isinstance(node.func, ast.Name)
                        and node.func.id == "__import__"), path
            assert not (isinstance(node.func, ast.Attribute)
                        and node.func.attr == "import_module"), path


def test_pc13_decimal_context_explicitly_traps_division_by_zero():
    tree = ast.parse(PROGRAM.read_text())
    assignments = {
        ast.unparse(node.targets[0]): ast.unparse(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign) and len(node.targets) == 1
    }
    assert assignments.get("context.traps[DivisionByZero]") == "True"
