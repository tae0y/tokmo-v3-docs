import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
BARRIER = ROOT / "src/tokmo/programs/analysis_barrier.py"
KERNEL = ROOT / "src/tokmo/records/ops_kernel.py"


def imports(path):
    tree = ast.parse(path.read_text())
    return {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }


def validate_slice_d_source(source, *, allowed_imports, allow_filesystem):
    tree = ast.parse(source)
    forbidden_modules = {
        "importlib", "socket", "ssl", "subprocess", "urllib", "http",
        "tokmo.facts", "tokmo.composition", "tokmo.records.order_scan_state",
        "tokmo.records.current_holdings_store", "tokmo.records.latest_quotes",
        "tokmo.records.trade_ledger_store", "tokmo.records.holdings_snapshots",
    }
    forbidden_calls = {"__import__", "eval", "exec", "getattr"}
    if not allow_filesystem:
        forbidden_calls |= {"open"}
    aliases = set()

    def static_string(node):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left, right = static_string(node.left), static_string(node.right)
            return None if left is None or right is None else left + right
        if isinstance(node, ast.JoinedStr):
            parts = [static_string(value) for value in node.values]
            return None if any(part is None for part in parts) else "".join(parts)
        if isinstance(node, ast.FormattedValue):
            value = static_string(node.value)
            return None if value is None else value
        return None

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules = [alias.name for alias in node.names]
            if any(module not in allowed_imports for module in modules):
                raise AssertionError("import denied")
        elif isinstance(node, ast.ImportFrom):
            if not node.module or node.level or node.module not in allowed_imports:
                raise AssertionError("from import denied")
        elif isinstance(node, ast.Assign) and isinstance(node.value, ast.Name):
            if node.value.id in forbidden_calls | {"open"}:
                aliases.update(
                    target.id for target in node.targets if isinstance(target, ast.Name)
                )
        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in forbidden_calls | aliases:
                raise AssertionError("call denied")
            if (isinstance(node.func, ast.Attribute)
                    and isinstance(node.func.value, ast.Name)
                    and node.func.value.id in {"importlib", "subprocess", "socket"}):
                raise AssertionError("module call denied")
        text = static_string(node)
        if text and any(token in text.lower() for token in forbidden_modules):
            raise AssertionError("forbidden composed token")


def test_barrier_import_allowlist_and_no_direct_io():
    allowed = {
        "tokmo.values.analysis_barrier", "tokmo.values.material_pack",
        "tokmo.values.receipt_v1", "tokmo.values.session_gate",
        "tokmo.values.ops_tick", "datetime", "typing", "__future__",
        # Stage 2 (SDS §6): the barrier binds the immutable open-market mask;
        # the mask value module is pure (no I/O) like every other value import.
        "tokmo.values.open_market_mask",
        # Stage 2 repair B1/M2: the canonical cadence tick carrier is a pure
        # value, and masked market-local dates come from IANA ZoneInfo only.
        "tokmo.values.cadence_policy",
        "zoneinfo",
    }
    assert imports(BARRIER) <= allowed
    tree = ast.parse(BARRIER.read_text())
    forbidden_calls = {"open", "sleep", "urlopen", "connect", "run", "Popen"}
    assert not {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
        and node.func.id in forbidden_calls
    }
    validate_slice_d_source(
        BARRIER.read_text(), allowed_imports=allowed, allow_filesystem=False
    )


def test_slice_d_has_no_c_store_or_live_private_bypass_tokens():
    text = (BARRIER.read_text() + KERNEL.read_text()).lower()
    forbidden = {
        "order_scan_state", "current_holdings_store", "latest_quotes",
        "trade_ledger_store", "holdings_snapshots", "tokmo.facts",
        "tokmo.composition", "socket", "subprocess",
        "accountseq", "authorization", "api_key",
    }
    assert not (forbidden & set(text.replace("(", " ").replace(".", " ").split()))


def test_kernel_exposes_no_generic_sql_surface():
    tree = ast.parse(KERNEL.read_text())
    public = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not node.name.startswith("_")
    }
    assert not (public & {"execute", "executemany", "query", "raw_sql", "connection"})
    allowed = {
        # fcntl/re: the locked atomic migrations own an flock and the
        # deterministic legacy table-order derivation.
        "contextlib", "collections", "datetime", "fcntl", "hashlib", "json", "os",
        "pathlib", "re", "sqlite3", "stat", "typing", "uuid",
        "tokmo.records.durability", "tokmo.records.private_sqlite",
        "tokmo.values.cadence_policy", "tokmo.values.material_pack",
        "tokmo.values.ops_kernel", "tokmo.values.request_parity",
    }
    validate_slice_d_source(
        KERNEL.read_text(), allowed_imports=allowed, allow_filesystem=True
    )


def test_actual_architecture_validator_rejects_bypass_snippets():
    allowed = {"datetime", "typing"}
    bad = (
        "import subprocess",
        "from socket import socket",
        "import importlib\nimportlib.import_module('sock' + 'et')",
        "__import__('socket')",
        "eval('1+1')",
        "exec('pass')",
        "getattr(object(), 'x')",
        "fn = open\nfn('/tmp/x')",
        "open('/tmp/x')",
        "name = f\"tokmo.{'facts'}\"",
    )
    for source in bad:
        with pytest.raises(AssertionError):
            validate_slice_d_source(
                source, allowed_imports=allowed, allow_filesystem=False
            )
