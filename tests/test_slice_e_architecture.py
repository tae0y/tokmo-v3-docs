import ast
from pathlib import Path

PIPELINE = Path("src/tokmo/programs/analysis_pipeline.py")
E_MODULES = (
    PIPELINE,
    Path("src/tokmo/programs/candidate_builder.py"),
    Path("src/tokmo/programs/analysis_redaction.py"),
    Path("src/tokmo/programs/prompt_builder.py"),
    Path("src/tokmo/values/analysis_contracts.py"),
)


def test_analysis_contracts_reexports_kernel_audit_types_without_records_dependency():
    contracts_path = Path("src/tokmo/values/analysis_contracts.py")
    tree = ast.parse(contracts_path.read_text())
    imports = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    imports.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        name == "tokmo.records" or name.startswith("tokmo.records.") for name in imports
    )


def test_slice_e_pipeline_has_no_external_or_forbidden_operational_imports():
    tree = ast.parse(PIPELINE.read_text())
    imports = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        (node.module or "").split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    assert not imports & {
        "socket",
        "ssl",
        "http",
        "urllib",
        "requests",
        "subprocess",
    }
    source = PIPELINE.read_text()
    assert "tokmo.composition" not in source
    assert "reserve_request" not in source
    assert "record_cooldown" not in source
    assert "AlertChannel.TELEGRAM" not in source
    assert "logging" not in source and "print(" not in source


def test_run_lock_is_the_only_pipeline_filesystem_open():
    tree = ast.parse(PIPELINE.read_text())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name)
            and node.func.id == "open"
            or isinstance(node.func, ast.Attribute)
            and node.func.attr == "open"
        )
    ]
    assert len(calls) == 1
    assert isinstance(calls[0].func, ast.Attribute)
    assert calls[0].func.attr == "open"


def test_all_slice_e_modules_have_no_logging_print_or_external_imports():
    forbidden = {"socket", "ssl", "http", "urllib", "requests", "subprocess", "logging"}
    for path in E_MODULES:
        source = path.read_text()
        tree = ast.parse(source)
        imports = {
            alias.name.split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            (node.module or "").split(".")[0]
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not imports & forbidden, path
        assert "print(" not in source, path


def test_core_slice_e_tests_use_public_entrypoint_not_kernel_writers():
    forbidden = {
        "create_manifest_with_baselines",
        "plan_analysis_batches",
        "start_analysis_batch",
        "settle_analysis_batch",
        "reconcile_analysis_batches",
        "upsert_deferred",
        "record_disposition_with_outbox",
    }
    for name in ("test_analysis_pipeline.py", "test_analysis_cadence.py"):
        tree = ast.parse(Path("tests", name).read_text())
        called = {
            node.func.attr
            for node in ast.walk(tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        assert not called & forbidden
        core = [
            node
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and node.name.startswith("test_")
            and any(
                token in node.name
                for token in (
                    "te7_",
                    "te8b_",
                    "te12_",
                    "crash",
                    "replay",
                    "cadence",
                    "rate_limited",
                    "auth_fail",
                    "failure_streak",
                )
            )
        ]
        assert core, name
        wrappers = {
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and any(
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "run_analysis_pipeline"
                for call in ast.walk(node)
            )
        }
        for function in core:
            public_calls = [
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and (
                    node.func.id == "run_analysis_pipeline" or node.func.id in wrappers
                )
            ]
            assert public_calls, f"{name}:{function.name}"
            private_calls = {
                node.func.id
                for node in ast.walk(function)
                if isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id.startswith("_")
                and node.func.id
                in {
                    "_decision",
                    "_settle_unknown_candidates",
                    "_settle_carryover",
                    "_reconstruct_deferred",
                    "_record_failure_aggregates",
                }
            }
            assert not private_calls, f"{name}:{function.name}:{private_calls}"
