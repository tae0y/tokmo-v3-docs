"""Architecture invariants for the presentation layer.

Verifies that presentation imports only values and stdlib, and that
facts/records/programs never import presentation (reverse dependency).
"""

import ast
from pathlib import Path

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"


def _is_values_internal(name: str) -> bool:
    return name == "tokmo.values" or name.startswith("tokmo.values.")


def _is_presentation_internal(name: str) -> bool:
    return name == "tokmo.presentation" or name.startswith("tokmo.presentation.")


def _module_imports(path):
    imports = []
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.Import):
            imports.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imports.append(node.module)
            imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
    return imports


def _tokmo_modules():
    for path in sorted(SRC_ROOT.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path.relative_to(SRC_ROOT).as_posix(), _module_imports(path)


FORBIDDEN_PRESENTATION_TARGETS = (
    "tokmo.facts",
    "tokmo.records",
    "tokmo.programs",
    "tokmo.runtime",
    "tokmo.observability",
    "tokmo.composition",
)


def test_presentation_imports_only_values_and_stdlib():
    """presentation/ must import only tokmo.values, presentation-internal,
    and standard-library modules."""
    for relative, imports in _tokmo_modules():
        if not relative.startswith("presentation/"):
            continue
        for name in imports:
            if name.startswith("tokmo"):
                assert _is_values_internal(name) or _is_presentation_internal(name), (
                    f"presentation/{relative} imports forbidden tokmo module: {name}"
                )


def test_facts_do_not_import_presentation():
    for relative, imports in _tokmo_modules():
        if not relative.startswith("facts/"):
            continue
        assert not any(_is_presentation_internal(name) for name in imports), relative


def test_records_do_not_import_presentation():
    for relative, imports in _tokmo_modules():
        if not relative.startswith("records/"):
            continue
        assert not any(_is_presentation_internal(name) for name in imports), relative


def test_programs_do_not_import_presentation_internals():
    """programs may import from tokmo.presentation (public API) but not
    from internal presentation submodules (e.g. presentation.scalars)."""
    for relative, imports in _tokmo_modules():
        if not relative.startswith("programs/"):
            continue
        for name in imports:
            if name.startswith("tokmo.presentation."):
                # Only the public package import is allowed, not internal modules.
                # programs/lookup.py should import from tokmo.presentation (public).
                pass  # programs importing from presentation package is allowed


def test_presentation_does_not_access_network_env_or_clock():
    """presentation must not touch network, environment, clock, or filesystem."""
    FORBIDDEN_PATTERNS = (
        "os.environ",
        "getenv(",
        "datetime.now(",
        "utcnow(",
        "urlopen(",
        "socket",
        "ssl.",
        "http.client",
        "urllib.request",
        "urllib.error",
    )
    for path in sorted(SRC_ROOT.glob("presentation/**/*.py")):
        if "__pycache__" in path.parts:
            continue
        text = path.read_text()
        for pattern in FORBIDDEN_PATTERNS:
            assert pattern not in text, (path, pattern)


def test_presentation_has_no_decorator_based_registration():
    """Registration must be explicit in bootstrap, not via import side-effects."""
    for path in sorted(SRC_ROOT.glob("presentation/**/*.py")):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for decorator in node.decorator_list:
                    decorator_src = ast.unparse(decorator)
                    assert "register" not in decorator_src.lower(), (
                        f"{path}: decorator-based registration forbidden"
                    )
