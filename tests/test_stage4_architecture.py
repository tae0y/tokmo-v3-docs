"""Stage 4 architecture seals: harness graph stays read-only and bounded.

The virtual replay, 48-hour shadow runner, lifecycle channel, and PASS
marker modules must be transitively free of live transports, the model
pipeline, the sealed G2 runners, every order-write capability, environment
reads, and ambient clocks (the repo-wide contract in test_architecture
enforces the latter two). The composition CLI seam and every ambient input
belong exclusively to the edge script ``tools/run_48h_shadow.py``. The
lifecycle sender is the single bounded outbound channel (typed harness
events only, Telegram sendMessage only, injected opener); no raw stock
alert path exists. Production provider calls continue to flow through the
claim-enforcing wire executor because the replay drives the identical
production tick function with claim-validated fixture deps.
"""

import ast
import inspect
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "tokmo"

STAGE4_MODULES = (
    "programs/virtual_replay.py",
    "programs/shadow_48h_runner.py",
    "observability/shadow_lifecycle.py",
    "records/prelaunch_markers.py",
)

# Never reachable from the Stage 4 graph, directly or transitively.
FORBIDDEN_GRAPH_MODULES = frozenset({
    "tokmo.composition",
    "tokmo.runtime.wallclock",
    "tokmo.runtime.config",
    "tokmo.facts.eia_live", "tokmo.facts.sec_live", "tokmo.facts.opendart_live",
    "tokmo.facts.krx_live",
    "tokmo.facts.toss_auth_live", "tokmo.facts.toss_live",
    "tokmo.facts.toss_prices_live", "tokmo.facts.toss_reads_live",
    "tokmo.programs.legacy_g2",
    "tokmo.programs.g2a_shadow_runner", "tokmo.programs.g2b_canary_runner",
    "tokmo.programs.g2c_analysis_shadow",
    "tokmo.programs.g2d_full_session_runner",
    "tokmo.programs.integrated_fake_cadence",
    "tokmo.programs.analysis_pipeline",
})



def _module_name(relative: str) -> str:
    return "tokmo." + relative.removesuffix(".py").replace("/", ".")


def _top_level_tokmo_imports(path: Path) -> set:
    names = set()
    for node in ast.parse(path.read_text()).body:
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("tokmo"):
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(
                alias.name for alias in node.names
                if alias.name.startswith("tokmo")
            )
    return names


def _all_tokmo_imports(path: Path) -> set:
    names = set()
    for node in ast.walk(ast.parse(path.read_text())):
        if isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith("tokmo"):
                names.add(node.module)
        elif isinstance(node, ast.Import):
            names.update(
                alias.name for alias in node.names
                if alias.name.startswith("tokmo")
            )
    return names


def _transitive_top_level_closure(module: str) -> set:
    seen: set = set()
    frontier = [module]
    while frontier:
        current = frontier.pop()
        if current in seen:
            continue
        seen.add(current)
        path = SRC / (current.removeprefix("tokmo.").replace(".", "/") + ".py")
        if path.is_file():
            frontier.extend(_top_level_tokmo_imports(path))
    return seen


def test_stage4_graph_is_transitively_free_of_forbidden_modules():
    for relative in STAGE4_MODULES:
        closure = _transitive_top_level_closure(_module_name(relative))
        leaked = closure & FORBIDDEN_GRAPH_MODULES
        assert not leaked, f"{relative} transitively reaches {sorted(leaked)}"


def test_stage4_src_modules_never_name_composition_at_all():
    for relative in STAGE4_MODULES:
        assert "tokmo.composition" not in _all_tokmo_imports(SRC / relative), (
            f"{relative} must never import the composition root"
        )


def test_the_ambient_edge_tool_owns_the_production_seam():
    """Only tools/run_48h_shadow.py (outside the sealed src graph) touches
    the composition CLI seam, process environment, wall clock, and HTTPS
    opener for the runner."""
    edge = (ROOT / "tools" / "run_48h_shadow.py").read_text()
    assert "from tokmo.composition import cadence_main" in edge
    assert "run_48h_shadow" in edge
    assert "os.environ" in edge
    assert "urllib.request" in edge
    # the src runner core names none of these
    core = (SRC / "programs/shadow_48h_runner.py").read_text()
    for token in ("os.environ", "datetime.now(", "urllib", "cadence_main"):
        assert token not in core, token


def test_stage4_modules_carry_no_order_write_or_model_vocabulary():
    # the lifecycle sender is the single POSTing module and is checked
    # separately below
    for relative in ("programs/virtual_replay.py",
                     "programs/shadow_48h_runner.py",
                     "records/prelaunch_markers.py"):
        text = (SRC / relative).read_text()
        for method in ("POST", "PUT", "PATCH", "DELETE"):
            assert not re.search(rf"\b{method}\b", text), (relative, method)
        for token in ("api.telegram.org", "sendMessage", "glm", "GLM",
                      "run_analysis_pipeline"):
            assert token not in text, (relative, token)


def test_lifecycle_sender_is_the_only_bounded_outbound_channel():
    from tokmo.observability.shadow_lifecycle import (
        HARNESS_EVENT_KINDS,
        TelegramStockBotSender,
    )

    # closed harness vocabulary: no stock/analysis alert kind exists
    assert HARNESS_EVENT_KINDS == frozenset(
        {"START", "FATAL", "STALLED", "FINISH"},
    )
    # the sender accepts exactly one typed event — no free-text payload seam
    parameters = list(
        inspect.signature(TelegramStockBotSender.__call__).parameters,
    )
    assert parameters == ["self", "event"]
    text = (SRC / "observability/shadow_lifecycle.py").read_text()
    # sendMessage is the only Telegram method; no photo/document/broadcast
    assert text.count("api.telegram.org") == 1
    for token in ("sendPhoto", "sendDocument", "sendMediaGroup"):
        assert token not in text


def test_replay_drives_the_exact_production_tick_with_claimed_fixtures():
    path = SRC / "programs/virtual_replay.py"
    imports = _all_tokmo_imports(path)
    assert "tokmo.programs.production_cadence" in imports
    assert "tokmo.programs.cadence_fixture_deps" in imports
    tree = ast.parse(path.read_text())
    called = {
        node.func.id for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_production_cadence_once" in called
    # no duplicate orchestrator: the sealed runners stay unreachable
    for name in ("run_d_full_session", "run_integrated_fake_day",
                 "run_b_canary"):
        assert name not in path.read_text()


def test_runner_records_only_allowlisted_tick_fields():
    from tokmo.programs.shadow_48h_runner import _TICK_RECORD_ALLOWLIST

    assert set(_TICK_RECORD_ALLOWLIST) == {
        "outcome", "reason", "exit_code", "lag_seconds", "mask_digest",
        "open_markets",
    }


def test_marker_tool_creates_verifiable_markers(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_stage4_pass_markers.py"),
         "--kind", "full-suite",
         "--commit", "55b65f63359f0d3a97920ea794b9d5b60bc8003e",
         "--command", ".venv/bin/python -m pytest -q",
         "--test-count", "2466",
         "--provenance", "hermes/full-suite",
         "--out", str(tmp_path / "full_suite_pass.json")],
        capture_output=True, text=True, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "PYTHONPATH": str(ROOT / "src")},
    )
    assert completed.returncode == 0, completed.stderr
    from tokmo.records.prelaunch_markers import verify_pass_marker

    payload = verify_pass_marker(
        tmp_path / "full_suite_pass.json", kind="full-suite",
        expected_commit="55b65f63359f0d3a97920ea794b9d5b60bc8003e",
    )
    assert payload["test_count"] == 2466
    assert "sha256" in completed.stdout


def test_marker_tool_signs_with_env_key(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_stage4_pass_markers.py"),
         "--kind", "pilot-acceptance",
         "--commit", "55b65f63359f0d3a97920ea794b9d5b60bc8003e",
         "--command", "direct glm reclosure",
         "--test-count", "1",
         "--provenance", "direct-zai/glm-5.2",
         "--out", str(tmp_path / "review.json"),
         "--hmac-key-env", "TOKMO_MARKER_KEY"],
        capture_output=True, text=True, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "PYTHONPATH": str(ROOT / "src"),
             "TOKMO_MARKER_KEY": "stage4-signing-key"},
    )
    assert completed.returncode == 0, completed.stderr
    from tokmo.records.prelaunch_markers import MarkerError, verify_pass_marker
    import pytest

    payload = verify_pass_marker(
        tmp_path / "review.json", kind="pilot-acceptance",
        expected_commit="55b65f63359f0d3a97920ea794b9d5b60bc8003e",
        hmac_key=b"stage4-signing-key",
    )
    assert payload["provenance"] == "direct-zai/glm-5.2"
    with pytest.raises(MarkerError):
        verify_pass_marker(
            tmp_path / "review.json", kind="pilot-acceptance",
            expected_commit="55b65f63359f0d3a97920ea794b9d5b60bc8003e",
            hmac_key=b"wrong",
        )
    # the key value itself never lands in the marker or stdout
    assert "stage4-signing-key" not in (tmp_path / "review.json").read_text()
    assert "stage4-signing-key" not in completed.stdout


def test_marker_tool_refuses_missing_key_env(tmp_path):
    completed = subprocess.run(
        [sys.executable, str(ROOT / "tools" / "make_stage4_pass_markers.py"),
         "--kind", "full-suite",
         "--commit", "55b65f63359f0d3a97920ea794b9d5b60bc8003e",
         "--command", "pytest -q", "--test-count", "1",
         "--provenance", "hermes",
         "--out", str(tmp_path / "m.json"),
         "--hmac-key-env", "TOKMO_MARKER_KEY"],
        capture_output=True, text=True, cwd=ROOT,
        env={"PATH": "/usr/bin:/bin", "HOME": str(tmp_path),
             "PYTHONPATH": str(ROOT / "src")},
    )
    assert completed.returncode == 1
    assert not (tmp_path / "m.json").exists()
