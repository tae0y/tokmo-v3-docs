import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_membership_proof_construction_boundary_and_no_forbidden_capabilities():
    allowed = {"programs/quant_membership.py"}
    for path in (ROOT / "src/tokmo").rglob("*.py"):
        relative = path.relative_to(ROOT / "src/tokmo").as_posix()
        tree = ast.parse(path.read_text())
        constructors = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and (
                getattr(node.func, "id", None) == "MembershipProofV1"
                or getattr(node.func, "attr", None) == "MembershipProofV1"
            )
        ]
        assert not constructors or relative in allowed
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.ImportFrom)
                and node.module == "tokmo.values.quant_multifactor_provenance"
            ):
                assert all(
                    alias.name != "MembershipProofV1" or relative in allowed
                    for alias in node.names
                )


def test_values_layer_and_assembler_have_no_io_or_delivery_imports():
    for relative in (
        "values/quant_multifactor_provenance.py",
        "programs/quant_multifactor_inputs.py",
        "programs/quant_membership.py",
    ):
        text = (ROOT / "src/tokmo" / relative).read_text()
        tree = ast.parse(text)
        imported = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        }
        imported.update(
            node.module.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module
        )
        assert not {
            "sqlite3",
            "socket",
            "requests",
            "httpx",
            "telegram",
            "openai",
        }.intersection(imported)
