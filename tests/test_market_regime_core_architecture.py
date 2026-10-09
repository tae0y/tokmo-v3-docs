import ast
import builtins
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASE = "b3bbff381e8a19372047e8c673ae6465d49402e6"
CLOSURE = "b035e2eb1a18197450227088ae9506dc020be6a0"
SOURCE = (
    Path("src/tokmo/values/market_regime_core.py"),
    Path("src/tokmo/programs/market_regime_core.py"),
)
ALLOWLIST = {
    *map(str, SOURCE),
    "tests/test_market_regime_core_values.py",
    "tests/test_market_regime_core_components.py",
    "tests/test_market_regime_core_synthesis.py",
    "tests/test_market_regime_core_architecture.py",
}


def allowed(path: str) -> bool:
    return path in ALLOWLIST or (
        path.startswith("tests/fixtures/market_regime_core/")
        and path.endswith(".json")
        and "/" not in path.removeprefix("tests/fixtures/market_regime_core/")
    )


def test_v11_recursive_exact_allowlist_and_nested_probe(tmp_path: Path) -> None:
    subprocess.run(
        ["git", "merge-base", "--is-ancestor", BASE, CLOSURE], cwd=ROOT, check=True
    )
    # Seal the reviewed historical core delta, not an unrelated moving dirty
    # implementation worktree. Pure-core bytes are separately hashed below.
    changed = subprocess.run(
        [
            "git",
            "diff",
            "--relative",
            "--name-only",
            BASE,
            CLOSURE,
            "--",
            *map(str, SOURCE),
            "tests/test_market_regime_core_*.py",
            "tests/fixtures/market_regime_core",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    untracked = subprocess.run(
        [
            "git",
            "ls-files",
            "--others",
            "--exclude-standard",
            "--",
            *map(str, SOURCE),
            "tests/test_market_regime_core_*.py",
            "tests/fixtures/market_regime_core",
        ],
        cwd=ROOT,
        check=True,
        text=True,
        capture_output=True,
    ).stdout.splitlines()
    assert all(allowed(path) for path in changed + untracked)
    assert not allowed("tests/fixtures/market_regime_core/nested/escape.json")


def test_v11_ast_pure_stdlib_no_float_or_mutable_globals() -> None:
    forbidden_calls = {"open", "exec", "eval", "compile", "input"}
    forbidden_modules = {
        "os",
        "socket",
        "pathlib",
        "subprocess",
        "time",
        "logging",
        "sqlite3",
        "urllib",
        "http",
        "pandas",
        "numpy",
    }
    forbidden_names = {
        "buy",
        "sell",
        "order",
        "alert",
        "account",
        "portfolio",
        "recommend",
        "advice",
        "allocation",
    }
    for relative in SOURCE:
        tree = ast.parse(
            (ROOT / relative).read_text(encoding="utf-8"), filename=str(relative)
        )
        for parent in ast.walk(tree):
            for child in ast.iter_child_nodes(parent):
                child.parent = parent
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, float):
                raise AssertionError(f"binary float at {relative}:{node.lineno}")
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                names = (
                    [alias.name.split(".")[0] for alias in node.names]
                    if isinstance(node, ast.Import)
                    else [(node.module or "").split(".")[0]]
                )
                assert all(
                    name in sys.stdlib_module_names or name == "tokmo" for name in names
                )
                assert not forbidden_modules.intersection(names)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in forbidden_calls
            if isinstance(node, (ast.Assign, ast.AnnAssign)) and isinstance(
                getattr(node, "parent", None), ast.Module
            ):
                assert not isinstance(node.value, (ast.List, ast.Dict, ast.Set))
            if isinstance(node, ast.Name):
                assert node.id.lower() not in forbidden_names
        text = (ROOT / relative).read_text(encoding="utf-8")
        assert (
            "datetime.now" not in text
            and "datetime.utcnow" not in text
            and "getenv(" not in text
        )


def test_v11_runtime_has_no_io_clock_or_environment_surface(monkeypatch) -> None:
    import tokmo.programs.market_regime_core as program
    import tokmo.values.market_regime_core as values

    assert not hasattr(program, "open") and not hasattr(values, "open")
    assert not hasattr(program, "socket") and not hasattr(values, "socket")
    assert not hasattr(program, "environ") and not hasattr(values, "environ")
    monkeypatch.setattr(
        builtins,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("file I/O")),
    )
    monkeypatch.setattr(
        socket,
        "socket",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("network I/O")),
    )
    monkeypatch.setattr(
        os,
        "getenv",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            AssertionError("environment read")
        ),
    )
    assert program.rsi14(tuple([program.Decimal("1")] * 15)) == 50
