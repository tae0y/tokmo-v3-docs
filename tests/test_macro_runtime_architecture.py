import ast
from pathlib import Path

BASE_COMMIT="43dd223eaa13d4b19a90bdd76019612efad643a5"
IMPLEMENTATION_COMMIT="1760f8531bada2b336f5f5f2bc3bc40e45109553"

def _historical_implementation_paths(root,git):
    prefix=git(["git","rev-parse","--show-prefix"],cwd=root,text=True).strip()
    for commit in (BASE_COMMIT,IMPLEMENTATION_COMMIT):
        git(["git","cat-file","-e",f"{commit}^{{commit}}"],cwd=root,text=True)
    output=git(["git","diff","--name-only",f"{BASE_COMMIT}..{IMPLEMENTATION_COMMIT}"],cwd=root,text=True)
    normalized={path.removeprefix(prefix) for path in output.splitlines()}
    return {path for path in normalized if not path.startswith(".ai/reviews/")}

def test_historical_lane_does_not_consult_head_status_or_later_paths():
    calls=[]
    def git(command,**kwargs):
        calls.append(command)
        if command==["git","rev-parse","--show-prefix"]: return "projects/tokmo-v3/\n"
        if command==["git","cat-file","-e","43dd223eaa13d4b19a90bdd76019612efad643a5^{commit}"]: return ""
        if command==["git","cat-file","-e","1760f8531bada2b336f5f5f2bc3bc40e45109553^{commit}"]: return ""
        if command==["git","diff","--name-only","43dd223eaa13d4b19a90bdd76019612efad643a5..1760f8531bada2b336f5f5f2bc3bc40e45109553"]: return "projects/tokmo-v3/tools/macro_canary.py\n"
        raise AssertionError(f"historical assertion consulted mutable state: {command}")
    assert _historical_implementation_paths(Path("/repo"),git)=={"tools/macro_canary.py"}
    assert all("HEAD" not in command and "status" not in command and ".ai/HANDOFF.md" not in command for command in calls)

def test_allowlist_and_forbidden_graph():
    root=Path(__file__).parents[1]
    expected={
      "src/tokmo/programs/macro_canary_adapter.py":{"normalize_macro_pages","merge_macro_pages","validate_page_boundary","build_macro_bundle","compute_firing_rate_status","resolve_completed_session"},
      "src/tokmo/programs/macro_runtime.py":{"MacroRuntimePolicyV1","MacroRuntimeTriggerV1","MacroRuntimeResultV1","eligible_completed_session","macro_dedupe_identity","run_macro_runtime_once"},
      "src/tokmo/records/macro_runtime_receipts.py":{"MacroRuntimeReceiptStore"},
      "tools/run_macro_runtime.py":{"build_runtime_deps","invoke_macro_runtime","main"},
      "tools/install_macro_runtime_agent.py":{"AgentPaths","resolve_paths","generate_wrapper","generate_config","generate_plist","validate_activation_preconditions","install","doctor","uninstall","main"},
    }
    for rel,names in expected.items():
        text=(root/rel).read_text(); tree=ast.parse(text)
        got={n.name for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
        assert names == got
        assert not any(word in text for word in ("telegram", "create_order", "account_seq"))
    receipt=ast.parse((root/"src/tokmo/records/macro_runtime_receipts.py").read_text()).body
    klass=next(node for node in receipt if isinstance(node,ast.ClassDef))
    assert {node.name for node in klass.body if isinstance(node,(ast.FunctionDef,ast.AsyncFunctionDef)) and node.name!="__init__"}=={"reserve","settle","read_receipt","reconcile_unknown"}

def test_changed_file_set_is_exact_delta_allowlist():
    import subprocess
    root=Path(__file__).parents[1]
    fixtures={f"tests/fixtures/macro_canary/{name}" for name in ("kospi_page_current_v1.json","investor_page_current_v1.json","proxy_page_current_v1.json","kospi_page_older_v1.json","investor_page_older_v1.json","proxy_page_older_v1.json","page_overlap_v1.json","page_duplicate_v1.json","page_bad_cursor_v1.json")}
    expected={"src/tokmo/programs/macro_canary_adapter.py","src/tokmo/programs/macro_runtime.py","src/tokmo/records/macro_runtime_receipts.py","tools/macro_canary.py","tools/run_macro_runtime.py","tools/install_macro_runtime_agent.py","src/tokmo/composition.py","tests/test_macro_canary_adapter.py","tests/test_macro_canary_contract.py","tests/test_macro_runtime.py","tests/test_macro_runtime_receipts.py","tests/test_macro_runtime_architecture.py","tests/test_macro_runtime_cli.py","tests/test_macro_runtime_installer.py"}|fixtures
    assert _historical_implementation_paths(root,subprocess.check_output)==expected
