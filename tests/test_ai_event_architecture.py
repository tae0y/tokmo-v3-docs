import ast
from pathlib import Path


from tokmo.values.production_cadence import CADENCE_STAGE_ORDER, StageStatusV1

ROOT = Path(__file__).parents[1]
CADENCE = ROOT / "src/tokmo/programs/production_cadence.py"
COMPOSITION = ROOT / "src/tokmo/composition.py"


def imported_modules(path):
    tree = ast.parse(path.read_text())
    return {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }


def test_cadence_stage_order_has_closed_event_analysis_vocabulary():
    assert CADENCE_STAGE_ORDER[-6:] == (
        "deltas", "admission", "analysis_shadow", "candidates",
        "quant_shadow", "sink",
    )
    for stage in ("deltas", "admission", "analysis_shadow"):
        assert StageStatusV1(stage, "skipped").stage == stage


def test_cadence_and_composition_have_no_ai_provider_transport_or_secret_loading_for_analysis():
    source = CADENCE.read_text() + COMPOSITION.read_text()
    assert "glm_call" not in CADENCE.read_text()
    assert "sol_call" not in CADENCE.read_text()
    assert "AI_API_KEY" not in source
    assert "OPENAI_API_KEY" not in source
    assert "ANTHROPIC_API_KEY" not in source
    assert "tokmo.programs.prompt_builder" not in imported_modules(CADENCE)
    assert "socket" not in imported_modules(CADENCE)
    assert "requests" not in imported_modules(CADENCE)


def test_analysis_execution_dependency_is_fixture_injection_only():
    tree = ast.parse(CADENCE.read_text())
    deps = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "ProductionCadenceDepsV1"
    )
    fields = {
        node.target.id for node in deps.body
        if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)
    }
    assert "analysis_execution" in fields
    annotation = next(
        node.annotation for node in deps.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "analysis_execution"
    )
    assert "AnalysisExecutionV1" in ast.unparse(annotation)
    cadence_source = CADENCE.read_text()
    assert "build_candidates(barrier" not in cadence_source
    assert "built=built" not in cadence_source


def test_portfolio_ai_payload_architecture_and_privacy_boundaries():
    values = (ROOT / "src/tokmo/values/portfolio_ai_payload.py").read_text()
    program = (ROOT / "src/tokmo/programs/portfolio_ai_payload.py").read_text()
    execution = (ROOT / "src/tokmo/programs/event_analysis_execution.py").read_text()
    assert "project_for_glm(candidate) if payload is None" in execution
    assert "normalize_portfolio_payload_v1(payload)" in execution
    assert execution.index("read_due_deferred") < execution.index("evaluate_event_admission", execution.index("def _call_ai_safe"))
    assert "PromptV1((" in execution
    assert "MODEL_REQUEST_MAX_BYTES" in execution
    assert "combined" not in execution.lower()
    assert "class AnalysisRequestV1" not in values + program
    assert "schema_version = 8" not in values + program + execution
    for forbidden in (
        "account_type_label", "source_record_key", "private_order_id",
        "authorization", "internal_total_cost_basis_krw", "\n    realized_pnl",
    ):
        assert forbidden not in values
    assert "classify_market" in (ROOT / "src/tokmo/programs/material_delta.py").read_text()


def test_new_payload_modules_have_closed_import_graph_and_no_financial_recomputation():
    values_path = ROOT / "src/tokmo/values/portfolio_ai_payload.py"
    projection_path = ROOT / "src/tokmo/programs/portfolio_ai_payload.py"
    values_imports = imported_modules(values_path)
    projection_imports = imported_modules(projection_path)
    assert not any(name.startswith("tokmo.records") or name == "tokmo.composition"
                   for name in values_imports | projection_imports)
    assert not ({"socket", "requests", "urllib", "httpx"} & (values_imports | projection_imports))
    for path in (values_path, projection_path):
        tree = ast.parse(path.read_text())
        forbidden_calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)
            and ((isinstance(node.func, ast.Name) and node.func.id == "float")
                 or (isinstance(node.func, ast.Attribute) and node.func.attr == "quantize"))]
        assert forbidden_calls == []
        financial_names = {"quantity", "price", "cost", "value", "pnl", "return_rate", "weight", "amount"}
        arithmetic = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.BinOp) or not isinstance(node.op, (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.Pow)):
                continue
            names = {part.id for part in ast.walk(node) if isinstance(part, ast.Name)} | {
                part.attr for part in ast.walk(node) if isinstance(part, ast.Attribute)}
            if any(any(token in name.lower() for token in financial_names) for name in names):
                arithmetic.append(node)
        assert arithmetic == []


def test_market_classifier_and_per_candidate_prompt_shape_are_ast_bound():
    material_tree = ast.parse((ROOT / "src/tokmo/programs/material_delta.py").read_text())
    classifier_calls = [node for node in ast.walk(material_tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "classify_market"]
    assert len(classifier_calls) == 1
    execution_tree = ast.parse((ROOT / "src/tokmo/programs/event_analysis_execution.py").read_text())
    prompt_calls = [node for node in ast.walk(execution_tree) if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name) and node.func.id == "PromptV1"]
    assert prompt_calls
    for call in prompt_calls:
        assert len([node for node in ast.walk(call)
                    if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "CandidateDataSegmentV1"]) <= 1
    projection_tree = ast.parse((ROOT / "src/tokmo/programs/portfolio_ai_payload.py").read_text())
    selection_cap_guards = [
        node for node in ast.walk(projection_tree)
        if isinstance(node, ast.Compare)
        and isinstance(node.left, ast.Call)
        and isinstance(node.left.func, ast.Name)
        and node.left.func.id == "len"
        and len(node.left.args) == 1
        and isinstance(node.left.args[0], ast.Name)
        and node.left.args[0].id == "selected"
        and len(node.ops) == 1
        and isinstance(node.ops[0], ast.Eq)
        and len(node.comparators) == 1
        and isinstance(node.comparators[0], ast.Constant)
        and node.comparators[0].value == 2
    ]
    assert len(selection_cap_guards) == 1
