import ast
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).parents[1]


def test_live_transport_is_only_imported_from_composition_boundary():
    hits = []
    for path in (ROOT / "src" / "tokmo").rglob("*.py"):
        if path.name == "opendart_bulk_live.py": continue
        tree = ast.parse(path.read_text())
        if any(isinstance(n, (ast.Import, ast.ImportFrom)) and
               "opendart_bulk_live" in ast.unparse(n) for n in ast.walk(tree)):
            hits.append(path.relative_to(ROOT).as_posix())
    assert hits == ["src/tokmo/opendart_bulk_composition.py"]


def test_bulk_lane_has_only_get_and_no_forbidden_scope():
    paths = [ROOT / "src/tokmo/facts/opendart_bulk_live.py",
             ROOT / "src/tokmo/programs/opendart_pilot.py",
             ROOT / "tools/opendart_bulk.py"]
    text = "\n".join(path.read_text() for path in paths)
    assert 'METHOD = "GET"' in text
    assert "api-key" not in text.lower()
    for word in ("POST", "PUT", "PATCH", "DELETE", "telegram", "openai", "sec.gov", "cron"):
        assert word.lower() not in text.lower()


def test_operational_program_seam_has_no_ai_telegram_sec_or_write_api_imports():
    paths = [ROOT / "src/tokmo/programs/opendart_pilot.py",
             ROOT / "src/tokmo/opendart_bulk_composition.py",
             ROOT / "src/tokmo/programs/opendart_fundamentals_enrichment.py",
             ROOT / "src/tokmo/records/opendart_fundamentals_store.py",
             ROOT / "tools/opendart_bulk.py",
             ROOT / "tools/run_opendart_operation.py"]
    imports = []
    for path in paths:
        tree = ast.parse(path.read_text())
        imports.extend(ast.unparse(node).lower() for node in ast.walk(tree)
                       if isinstance(node, (ast.Import, ast.ImportFrom)))
    joined = "\n".join(imports)
    assert all(forbidden not in joined
               for forbidden in ("openai", "anthropic", "telegram", "sec", "write"))
    assert "from tokmo.opendart_bulk_composition" in (
        ROOT / "tools/opendart_bulk.py").read_text()


def test_single_target_sources_match_reviewed_contract_bytes():
    # opendart_live carries the reviewed EP-1 tracer seam; its pin was
    # refreshed exactly once for the SDS §11 (m-4) docstring correction.
    import hashlib
    expected = {
        "src/tokmo/facts/opendart.py": "2263a9ccd6fd45cf7457646210761fbe799a1792dbba99e84233df6d3de7a661",
        "src/tokmo/facts/opendart_live.py": "e97d836f92a9ca64dd8eca3e4743653bdd947887d972ce30b817871ee1d1ad59",
    }
    for name, digest in expected.items():
        assert hashlib.sha256((ROOT / name).read_bytes()).hexdigest() == digest


def test_enrichment_lane_modules_have_no_forbidden_scope():
    # The §6 tick outcome literal refused_inputs_unavailable contains the
    # letters "put", so the bulk-lane word sweep above cannot absorb these
    # modules verbatim; this sweep bans the remaining vocabulary exactly.
    paths = [ROOT / "src/tokmo/programs/opendart_fundamentals_enrichment.py",
             ROOT / "src/tokmo/records/opendart_fundamentals_store.py"]
    text = "\n".join(path.read_text() for path in paths)
    assert "api-key" not in text.lower()
    for word in ("POST", "PATCH", "DELETE", "telegram", "openai", "sec.gov",
                 "cron", "urllib", "http.client", "socket"):
        assert word.lower() not in text.lower(), word
    for path in paths:
        tree = ast.parse(path.read_text())
        imports = [ast.unparse(node).lower() for node in ast.walk(tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom))]
        joined = "\n".join(imports)
        assert all(banned not in joined for banned in (
            "openai", "anthropic", "telegram", "sec", "write", "urllib",
            "socket", "ssl", "http"))
    # The program stays a pure orchestration seam: no live transport import.
    enrichment_tree = ast.parse(paths[0].read_text())
    assert not any("opendart_live" in ast.unparse(node)
                   for node in ast.walk(enrichment_tree)
                   if isinstance(node, (ast.Import, ast.ImportFrom)))


def test_five_run_wrapper_has_one_internal_five_bound_and_no_secret_argument():
    source = (ROOT / "tools/run_opendart_pilot.py").read_text()
    tree = ast.parse(source)
    ranges = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
              and isinstance(node.func, ast.Name) and node.func.id == "range"]
    assert [tuple(ast.literal_eval(arg) for arg in node.args) for node in ranges] == [(1, 6)]
    assert "api-key" not in source.lower()


def test_tools_bootstrap_src_for_direct_invocation(tmp_path):
    for tool in ("opendart_bulk.py", "run_opendart_pilot.py", "run_opendart_operation.py"):
        result = subprocess.run(
            [sys.executable, str(ROOT / "tools" / tool), "--help"],
            cwd=ROOT, env={"PATH": ""}, capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert "ModuleNotFoundError" not in result.stderr
    status = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "opendart_bulk.py"),
         "status", "--home", str(tmp_path / "home")],
        cwd=ROOT, env={"PATH": ""}, capture_output=True, text=True)
    assert status.returncode == 0, status.stderr
    # status is strictly read-only: an absent store is reported, never created.
    assert '"integrity":"absent"' in status.stdout
    assert not (tmp_path / "home" / ".local").exists()
