import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "tokmo"


def _imports(relative: str) -> set[str]:
    tree = ast.parse((SRC / relative).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_canonical_registry_and_store_import_no_quant_modules():
    for relative in (
        "values/official_membership_observation.py",
        "records/canonical_observation_registry.py",
        "records/official_membership_observation_store.py",
    ):
        imports = _imports(relative)
        assert not any("quant_" in name for name in imports), (relative, imports)


def test_official_membership_layers_have_no_live_network_or_program_private_store():
    for relative in (
        "facts/quant_membership.py",
        "programs/quant_membership.py",
        "records/official_membership_observation_store.py",
    ):
        text = (SRC / relative).read_text()
        imports = _imports(relative)
        assert not {"requests", "httpx", "socket", "telegram", "openai"}.intersection(
            {name.split(".")[0] for name in imports}
        )
        assert "api_key" not in text
    assert not (SRC / "records/quant_membership_store.py").exists()


def test_canonical_family_declaration_is_closed_and_exact():
    text = (SRC / "records/canonical_observation_registry.py").read_text()
    assert "OfficialMembershipObservationV1" in text
    assert "OfficialMembershipObservationStore" in text


def test_fixture_canary_is_detected_before_fixture_tree_exclusion():
    fixture_text = (
        ROOT / "tests/fixtures/quant_membership/review_canary.txt"
    ).read_text()
    for token in (
        "client_secret",
        "api_key",
        "/Users/fake/path",
        "raw_body",
        "response_body",
    ):
        assert token in fixture_text
    production_texts = [
        (SRC / "values/official_membership_observation.py").read_text(),
        (SRC / "facts/quant_membership.py").read_text(),
        (SRC / "programs/quant_membership.py").read_text(),
        (SRC / "records/canonical_observation_registry.py").read_text(),
        (SRC / "records/official_membership_observation_store.py").read_text(),
        (SRC / "values/quant_multifactor_provenance.py").read_text(),
        (SRC / "programs/quant_multifactor_inputs.py").read_text(),
    ]
    combined = "\n".join(production_texts)
    for token in ("raw_body", "response_body", "/Users/"):
        assert token not in combined
