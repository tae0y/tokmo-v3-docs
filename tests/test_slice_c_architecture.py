import ast
from pathlib import Path
import re

import pytest


NEW = (
    Path("src/tokmo/values/completed_execution.py"),
    Path("src/tokmo/values/decimal_canon.py"),
    Path("src/tokmo/records/order_scan_state.py"),
    Path("src/tokmo/programs/trades_sync.py"),
)

TRANSPORT_ROOTS = frozenset({
    "urllib", "http", "httpx", "requests", "socket", "ssl",
})
WRITE_VERBS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
DYNAMIC_BUILTINS = frozenset({"__import__", "getattr", "eval", "exec"})


def _static_text(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left, right = _static_text(node.left), _static_text(node.right)
        return None if left is None or right is None else left + right
    if isinstance(node, ast.JoinedStr):
        parts = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                parts.append(value.value)
            elif isinstance(value, ast.FormattedValue):
                folded = _static_text(value.value)
                if folded is None:
                    return None
                parts.append(folded)
            else:
                return None
        return "".join(parts)
    if isinstance(node, (ast.Tuple, ast.List)):
        parts = [_static_text(item) for item in node.elts]
        return None if any(item is None for item in parts) else "".join(parts)
    if (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
            and node.func.attr == "join" and len(node.args) == 1):
        separator, values = _static_text(node.func.value), _static_text(node.args[0])
        if separator is not None and isinstance(node.args[0], (ast.Tuple, ast.List)):
            items = [_static_text(item) for item in node.args[0].elts]
            if all(item is not None for item in items):
                return separator.join(items)
        return values
    return None


def _architecture_violations(source, *, programs=False):
    tree = ast.parse(source)
    violations = []
    aliases = set()
    importlib_aliases = {"importlib"}
    import_module_aliases = {"import_module"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".", 1)[0]
                if root in TRANSPORT_ROOTS:
                    violations.append("transport_import")
                if alias.name == "importlib":
                    importlib_aliases.add(alias.asname or alias.name)
                if programs and alias.name.startswith((
                        "tokmo.facts", "tokmo.live", "tokmo.records",
                        "tokmo.composition")):
                    violations.append("program_layer_import")
        elif isinstance(node, ast.ImportFrom):
            module = node.module or ""
            if module.split(".", 1)[0] in TRANSPORT_ROOTS:
                violations.append("transport_from_import")
            if module == "importlib":
                import_module_aliases.update(
                    alias.asname or alias.name for alias in node.names
                    if alias.name == "import_module"
                )
            if programs and (
                    module.startswith((
                        "tokmo.facts", "tokmo.live", "tokmo.records",
                        "tokmo.composition"))
                    or (
                        module == "tokmo"
                        and any(alias.name in {
                            "facts", "live", "records", "composition",
                        } for alias in node.names)
                    )):
                violations.append("program_layer_from_import")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names = {
                arg.arg for arg in (
                    tuple(node.args.posonlyargs) + tuple(node.args.args)
                    + tuple(node.args.kwonlyargs)
                )
            }
            if names & {"method", "body", "url"}:
                violations.append("generic_http_parameter")
        elif isinstance(node, ast.Assign):
            if isinstance(node.value, ast.Name) and node.value.id in DYNAMIC_BUILTINS:
                aliases.update(
                    target.id for target in node.targets
                    if isinstance(target, ast.Name)
                )
        elif isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else None
            dynamic_attribute = (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id in importlib_aliases
                and node.func.attr == "import_module"
            )
            if name in DYNAMIC_BUILTINS or name in aliases:
                violations.append("dynamic_execution")
            if name in import_module_aliases or dynamic_attribute:
                violations.append("dynamic_import")
            if {keyword.arg for keyword in node.keywords} & {"method", "body", "url"}:
                violations.append("generic_http_call")
        text = _static_text(node)
        if text is not None and text.upper() in WRITE_VERBS:
            violations.append("write_method")
    return tuple(violations)


def test_slice_c_has_no_order_write_or_private_live_surface():
    joined = "\n".join(path.read_text() for path in NEW)
    for forbidden in (
        "account" + "Seq", "/api/", "/oauth", "openapi.tossinvest.com",
        '"POST"', '"PUT"', '"PATCH"', '"DELETE"',
        "tokmo.facts.toss_reads_live", "tokmo.composition",
    ):
        assert forbidden not in joined
    program = NEW[-1].read_text()
    assert "tokmo.records" not in program
    tree = ast.parse(program)
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in {"getattr", "__import__"}
        for node in ast.walk(tree)
    )
    assert not Path("src/tokmo/records/trades_store_seam.py").exists()


def test_slice_c_all_new_modules_reject_dynamic_transport_and_write_indirection():
    for path in NEW:
        assert _architecture_violations(
            path.read_text(), programs=path.parts[-2] == "programs",
        ) == ()
        tree = ast.parse(path.read_text())
        if path.parts[-2] == "values":
            assert not any(
                isinstance(node, ast.ImportFrom)
                and (node.module or "").startswith(
                    ("tokmo.facts", "tokmo.records", "tokmo.programs")
                )
                for node in ast.walk(tree)
            )
        assert not any(
            isinstance(node, ast.Constant) and isinstance(node.value, str)
            and re.search(r"https?://|/api/|/oauth", node.value)
            for node in ast.walk(tree)
        )


@pytest.mark.parametrize("snippet", (
    "import urllib.request",
    "from http.client import HTTPSConnection",
    "import httpx",
    "import requests.sessions",
    "import socket",
    "from ssl import SSLContext",
    "import importlib\nimportlib.import_module('ur' + 'llib.request')",
    "from importlib import import_module as load\nload('http' + 'x')",
    "__import__('requests')",
    "g = getattr\ng(object(), 'send')",
    "eval('1 + 1')",
    "exec('pass')",
    "verb = 'PO' + 'ST'",
    "verb = ''.join(('DE', 'LETE'))",
    "verb = f\"{'PA'}TCH\"",
    "def request(method=None): pass",
    "client(method='GET')",
))
def test_architecture_guard_rejects_bypass_snippets(snippet):
    assert _architecture_violations(snippet)


@pytest.mark.parametrize("snippet", (
    "import tokmo.facts.toss_reads",
    "import tokmo.live",
    "from tokmo.records import order_scan_state",
    "from tokmo import composition",
))
def test_program_architecture_guard_rejects_forbidden_tokmo_layers(snippet):
    assert _architecture_violations(snippet, programs=True)


def test_architecture_guard_allows_legitimate_local_sql_delete():
    assert _architecture_violations(
        "db.execute('DELETE FROM execution_quarantine WHERE account_scope=?')"
    ) == ()


def test_toss_orders_source_registry_uses_plain_literal():
    source = Path("src/tokmo/records/trade_ledger_preflight.py").read_text()
    tree = ast.parse(source)
    literals = [
        node.value for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and node.value == "toss-orders-v1"
    ]
    assert len(literals) >= 2
    assert '"to" "ss-orders-v1"' not in source
