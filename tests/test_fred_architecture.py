import ast
import importlib.util
import json
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).parents[1]


def _tool_module():
    spec = importlib.util.spec_from_file_location("fred_canonical_tool", ROOT / "tools/fred_canonical.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _series_fixture(path):
    path.write_text(json.dumps({"realtime_start":"2026-01-01","realtime_end":"2026-01-02",
        "seriess":[{"id":"PAYEMS","realtime_start":"2026-01-01","realtime_end":"2026-01-02",
        "title":"All Employees","observation_start":"1939-01-01","observation_end":"2025-12-01",
        "frequency":"Monthly","frequency_short":"M","units":"Thousands","units_short":"Thous.",
        "seasonal_adjustment":"Seasonally Adjusted","seasonal_adjustment_short":"SA",
        "last_updated":"2026-01-01 00:00:00-06","popularity":90}]}))


def test_fred_layer_seals_and_cli_exist():
    values = (ROOT / "src/tokmo/values/fred_macro.py").read_text()
    live = (ROOT / "src/tokmo/facts/fred_live.py").read_text()
    composition = (ROOT / "src/tokmo/fred_composition.py").read_text()
    tool = ROOT / "tools/fred_canonical.py"
    assert tool.exists()
    assert "urlopen(" not in live and "os.environ" not in live
    assert "datetime.now(" not in "\n".join((values, live, composition))
    assert "Path.home()" not in composition
    tree = ast.parse(composition)
    function = next(node for node in ast.walk(tree)
                    if isinstance(node, ast.FunctionDef) and node.name == "run_fred_operation")
    defaults = {arg.arg for arg, default in zip(function.args.kwonlyargs[-len(function.args.kw_defaults):],
                                                 function.args.kw_defaults) if default is not None}
    assert not {"transport", "clock"} & defaults


def test_fred_live_architecture_delta_has_one_registered_importer():
    spec = importlib.util.spec_from_file_location("repository_architecture", ROOT / "tests/test_architecture.py")
    architecture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(architecture)
    assert {item for item in architecture.LIVE_TRANSPORT_MODULES if "fred" in item} == {"facts/fred_live.py"}
    assert {item for item in architecture.LIVE_TRANSPORT_IMPORT_PREFIXES if "fred" in item} == {"tokmo.facts.fred_live"}
    importers = []
    for path in (ROOT / "src/tokmo").rglob("*.py"):
        tree = ast.parse(path.read_text())
        if any(isinstance(node, ast.ImportFrom) and node.module == "tokmo.facts.fred_live"
               for node in ast.walk(tree)):
            importers.append(path.relative_to(ROOT / "src/tokmo").as_posix())
    assert importers == ["fred_composition.py"]


def test_only_closed_fred_source_paths_exist():
    actual = {path.relative_to(ROOT).as_posix() for base in (ROOT / "src", ROOT / "tools", ROOT / "tests")
              for path in base.rglob("*fred*") if path.is_file() and "__pycache__" not in path.parts}
    discovered = actual - {path for path in actual if path.startswith("tests/fixtures/fred/")}
    expected_source = {
        "src/tokmo/values/fred_macro.py", "src/tokmo/facts/fred.py",
        "src/tokmo/facts/fred_live.py", "src/tokmo/records/fred_material_store.py",
        "src/tokmo/fred_composition.py", "tools/fred_canonical.py",
        "tools/fred_canonical_live.py",
        # G2 on-demand connection delta: one new tool path.
        "tools/fred_ondemand_canary.py",
        # Routine on-demand query seam (Delta SDS §9): five new source paths.
        "src/tokmo/values/fred_query.py", "src/tokmo/facts/fred_query.py",
        "src/tokmo/records/fred_query_cache.py",
        "src/tokmo/programs/macro_fred_inputs.py",
        "src/tokmo/fred_query_composition.py",
        # FRED on-demand → Macro caller connection: moved sidecar module.
        "src/tokmo/records/fred_schedule_sidecar.py",
    }
    # Test paths keep their own discovery allowlist and are never mixed into
    # the closed source set above.
    expected_tests = {
        "tests/test_fred_values.py", "tests/test_fred_fact.py", "tests/test_fred_live.py",
        "tests/test_fred_material_store.py", "tests/test_fred_composition.py",
        "tests/test_fred_architecture.py", "tests/test_fred_canonical_live.py",
        "tests/test_fred_r3.py", "tests/test_fred_r4.py",
        "tests/test_fred_query_values.py", "tests/test_fred_query_fact.py",
        "tests/test_fred_query_cache.py", "tests/test_macro_fred_inputs.py",
        "tests/test_fred_query_architecture.py",
        # G2 on-demand connection delta: one new test path.
        "tests/test_fred_ondemand_canary.py",
        # FRED on-demand → Macro caller connection: two new test paths.
        "tests/test_fred_schedule_sidecar.py",
        "tests/test_macro_fred_caller.py",
    }
    assert {path for path in discovered if not path.startswith("tests/")} == expected_source
    assert {path for path in discovered if path.startswith("tests/")} == expected_tests


def test_g_live_changed_controller_layers_do_not_catch_baseexception():
    for relative in ("src/tokmo/fred_composition.py", "tools/fred_canonical_live.py"):
        path = ROOT / relative
        assert path.exists(), relative
        tree = ast.parse(path.read_text())
        assert not any(isinstance(node, ast.ExceptHandler) and
                       isinstance(node.type, ast.Name) and node.type.id == "BaseException"
                       for node in ast.walk(tree))


def test_fred_changed_files_have_no_secret_url_or_private_path_literal():
    changed = (
        "src/tokmo/facts/fred.py", "src/tokmo/facts/fred_live.py",
        "src/tokmo/values/fred_macro.py", "src/tokmo/fred_composition.py",
        "src/tokmo/records/fred_material_store.py", "tools/fred_canonical_live.py",
        "tests/test_fred_canonical_live.py", "tests/test_fred_architecture.py",
        "tests/test_fred_material_store.py", "tests/test_fred_composition.py",
        "tests/test_fred_live.py", "tests/test_fred_r4.py",
    )
    credential = "a" * 32
    for relative in changed:
        text = (ROOT / relative).read_text()
        assert f"api_key={credential}" not in text
        assert "/" + "Users/" not in text and "/" + "home/" not in text
        assert ".hermes/" + "credentials/fred.env" not in text


def test_fixture_cli_requires_root_and_live_is_unreachable(tmp_path, capsys):
    module = _tool_module()
    missing = tmp_path / "missing.json"
    kwargs = dict(wall_clock=lambda: datetime(2026,1,1,tzinfo=timezone.utc),
                  monotonic_clock=lambda: 1.0, api_key_loader=lambda: "a" * 32)
    assert module.run_cli(["--operation","SERIES","--fixture",str(missing)], **kwargs) == 2
    assert not missing.exists()
    assert "explicit_root_required" in capsys.readouterr().out
    assert module.run_cli(["--live"], **kwargs) == 2
    assert "g_live_forbidden" in capsys.readouterr().out


def test_fixture_cli_one_start_and_honest_zero_start_rerun(tmp_path, capsys):
    module = _tool_module()
    fixture, root = tmp_path / "series.json", tmp_path / "store"
    _series_fixture(fixture)
    args = ["--operation","SERIES","--fixture",str(fixture),"--root",str(root)]
    kwargs = dict(wall_clock=lambda: datetime(2026,1,1,tzinfo=timezone.utc),
                  monotonic_clock=lambda: 1.0, api_key_loader=lambda: "a" * 32)
    assert module.run_cli(args, **kwargs) == 0
    assert module.run_cli(args, **kwargs) == 0
    lines = capsys.readouterr().out.splitlines()
    assert [json.loads(line)["code"] for line in lines] == ["durable_success", "already_attempted_fixture"]
    assert all("api_key" not in line and "https://" not in line for line in lines)
