"""Architecture/scope tests for disclosure review persistence.

Enforces durable import-direction invariants for the new records and programs
modules. Does not add broad negative string scans for future field names or
technologies (plan Task 6).
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"

_RECORDS_MODULE = "records/disclosure_reviews.py"
_PROGRAMS_MODULE = "programs/disclosure_review_storage.py"


def _module_imports(path: Path) -> list[str]:
    imports = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
    return imports


def test_disclosure_review_records_imports_values_stdlib_durability_only():
    """records/disclosure_reviews.py may import only:
    - stdlib (json, os, contextlib, dataclasses, datetime, pathlib)
    - tokmo.values
    - tokmo.records.durability
    """
    path = SRC_ROOT / _RECORDS_MODULE
    imports = _module_imports(path)
    # Whitelist: any tokmo.* import outside these is forbidden.
    _ALLOWED = (
        "tokmo.values",
        "tokmo.records.durability",
    )
    for name in imports:
        if name.startswith("tokmo"):
            assert any(name == a or name.startswith(a + ".")
                       for a in _ALLOWED), (
                f"{_RECORDS_MODULE} imports tokmo dependency "
                f"outside allowed whitelist: {name}")


def test_disclosure_review_records_no_network_no_env_no_wall_clock():
    """records must not import network, read environment, or call wall clock."""
    path = SRC_ROOT / _RECORDS_MODULE
    text = path.read_text()
    forbidden_patterns = (
        "os.environ", "getenv(", "datetime.now(", "utcnow(",
        "urlopen(", "socket", "ssl",
    )
    for pattern in forbidden_patterns:
        assert pattern not in text, (
            f"{_RECORDS_MODULE} contains forbidden pattern {pattern!r}")


def test_disclosure_review_programs_imports_values_and_records_only():
    """programs/disclosure_review_storage.py may import only:
    - tokmo.values
    - tokmo.records.disclosure_reviews
    No facts, no network, no clock, no Telegram.
    """
    path = SRC_ROOT / _PROGRAMS_MODULE
    imports = _module_imports(path)
    allowed = (
        "tokmo.values",
        "tokmo.records.disclosure_reviews",
    )
    for name in imports:
        if name.startswith("tokmo"):
            assert any(name == a or name.startswith(a + ".")
                       for a in allowed), (
                f"{_PROGRAMS_MODULE} imports {name} outside allowed set")
    text = path.read_text()
    forbidden_patterns = (
        "os.environ", "getenv(", "datetime.now(", "utcnow(",
        "urlopen(", "socket", "ssl", "telegram",
    )
    for pattern in forbidden_patterns:
        assert pattern not in text, (
            f"{_PROGRAMS_MODULE} contains forbidden pattern {pattern!r}")


def test_disclosure_review_programs_does_not_import_facts():
    """The programs seam must not import from tokmo.facts."""
    path = SRC_ROOT / _PROGRAMS_MODULE
    imports = _module_imports(path)
    for name in imports:
        assert not name.startswith("tokmo.facts"), (
            f"{_PROGRAMS_MODULE} must not import {name}")
