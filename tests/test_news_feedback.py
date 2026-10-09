import ast
from pathlib import Path

from tokmo.values.callback_registry import (
    CallbackRegistry,
    CallbackRegistryError,
    news_feedback_callback,
)


def test_prefix_registry_and_exact_callback_contract():
    registry = CallbackRegistry(("q1:", "p1:", "n1:"))
    ref = "a" * 32
    assert registry.owner(news_feedback_callback(ref, useful=True)) == "n1:"
    try:
        news_feedback_callback("NOTHEX", useful=True)
    except CallbackRegistryError:
        pass
    else:
        raise AssertionError("non-hex callback accepted")


def test_feedback_program_has_no_sql_or_policy_import_surface():
    path = Path(__file__).parents[1] / "src/tokmo/programs/news_feedback.py"
    tree = ast.parse(path.read_text())
    imports = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        token in item
        for item in imports
        for token in ("policy", "preference", "playbook", "watchlist", "sqlite")
    )
    assert not any(
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and any(
            sql in node.value.upper()
            for sql in ("SELECT ", "INSERT ", "UPDATE ", "DELETE ")
        )
        for node in ast.walk(tree)
    )
