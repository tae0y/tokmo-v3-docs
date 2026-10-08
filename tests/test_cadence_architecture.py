"""B2/13/14/m3: cadence authority sealing and central policy derivation.

The historical G2 plan/start APIs stay only as sealed legacy acceptance
surfaces. The seal covers the whole relevant production source graph — every
program module regardless of filename, the composition/transport owner, and
every repository tool — minus a closed allowlist of named historical G2
modules and named historical G2 wiring scopes inside composition. Any new
module that calls the legacy admission surface fails, whatever it is named.
Every new cadence transport closure requires an OpsKernel STARTED receipt,
and active production-path numeric gates derive from the one central policy
declaration.
"""

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]

LEGACY_ADMISSION_NAMES = frozenset({
    "plan_requests", "arm_account_rows", "start_request", "settle_request",
    "reserve_request",
})

# Closed allowlist of named historical G2 acceptance modules (m3). These are
# the bounded, already-reviewed G2 collectors/runners; nothing may be added
# here without review, and any other module in the sealed graph fails on any
# legacy admission name regardless of its filename.
HISTORICAL_G2_PROGRAM_MODULES = frozenset({
    "trades_sync.py",  # G2 trades collector: plans/starts/settles order pages
})

# Closed allowlist of named historical G2 wiring scopes inside the
# composition/transport owner. Composition itself stays sealed: legacy names
# anywhere outside exactly these top-level scopes fail the test.
HISTORICAL_G2_COMPOSITION_SCOPES = frozenset({
    "_g2a_account_calendar_start",
    "_g2a_account_calendar_settle",
    "_g2a_plan_start_token",
    "_g2a_settle_token",
    "_g2b_plan_start_session_calendar",
    "_g2b_settle_session_calendar",
    "_g2b_plan_start_token",
    "_g2b_settle_token",
    "_g2b_plan_start",
    "_g2b_settle",
    "_g2b_plan_arm_account_holdings",
    "build_g2b_live_deps",
    "build_g2d_live_deps",
})

COMPOSITION = ROOT / "src" / "tokmo" / "composition.py"


def legacy_names_used(node) -> set:
    """Every legacy admission name reachable as an attribute or bare name."""
    used = {
        child.attr for child in ast.walk(node) if isinstance(child, ast.Attribute)
    } | {
        child.id for child in ast.walk(node) if isinstance(child, ast.Name)
    }
    return used & LEGACY_ADMISSION_NAMES


def sealed_program_files(*, programs_root=None, tools_root=None):
    """Recursively discover the whole program/tool graph by directory
    membership, never by filename convention. Historical exclusions are
    ROOT-relative program paths, so a same-basename nested file is not exempt."""
    programs_root = programs_root or (ROOT / "src" / "tokmo" / "programs")
    tools_root = tools_root or (ROOT / "tools")
    files = sorted(
        path for path in programs_root.rglob("*.py")
        if path.relative_to(programs_root).as_posix()
        not in HISTORICAL_G2_PROGRAM_MODULES
    )
    files.extend(sorted(tools_root.rglob("*.py")))
    assert files, "production program graph missing"
    return files


def test_whole_program_graph_cannot_reach_legacy_admission_surface():
    for path in sealed_program_files():
        leaked = legacy_names_used(ast.parse(path.read_text()))
        assert not leaked, (
            f"{path.relative_to(ROOT)} reaches legacy admission surface: {leaked}"
        )


def test_market_regime_uses_static_imports_and_shared_target_authority():
    shadow = ROOT / "src" / "tokmo" / "programs" / "market_regime_shadow.py"
    quotes = ROOT / "src" / "tokmo" / "programs" / "quotes_sync.py"
    tree = ast.parse(shadow.read_text())
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "__import__"
        for node in ast.walk(tree)
    )
    imports = {
        alias.name
        for node in tree.body if isinstance(node, ast.ImportFrom)
        and node.module == "tokmo.programs.quotes_sync"
        for alias in node.names
    }
    assert "derive_analysis_targets" in imports
    assert any(
        isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == "derive_analysis_targets"
        for node in ast.parse(quotes.read_text()).body
    )


def test_composition_outside_named_g2_scopes_cannot_reach_legacy_surface():
    tree = ast.parse(COMPOSITION.read_text())
    for node in tree.body:
        if (isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
                and node.name in HISTORICAL_G2_COMPOSITION_SCOPES):
            continue
        leaked = legacy_names_used(node)
        assert not leaked, (
            "composition.py reaches the legacy admission surface outside the "
            f"named historical G2 scopes (line {node.lineno}): {leaked}"
        )


def test_g2_allowlists_are_closed_and_still_real():
    """Every allowlisted module/scope must still exist: a removed historical
    module may not linger as a dormant hole in the seal."""
    programs = ROOT / "src" / "tokmo" / "programs"
    for name in HISTORICAL_G2_PROGRAM_MODULES:
        assert (programs / name).is_file(), f"stale allowlist module: {name}"
    tree = ast.parse(COMPOSITION.read_text())
    top_level = {
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
    }
    stale = HISTORICAL_G2_COMPOSITION_SCOPES - top_level
    assert not stale, f"stale allowlist composition scopes: {stale}"


def test_seal_detector_catches_a_new_module_with_any_filename():
    """The seal is graph-membership plus AST names, not a filename glob: a
    synthetic new transport module under any name is detected."""
    offending = ast.parse(
        "def pump(kernel, tick_id, rows, now):\n"
        "    kernel.plan_requests(None, tick_id, 'QUOTES', rows, now=now)\n"
        "    return start_request\n"
    )
    assert legacy_names_used(offending) == {"plan_requests", "start_request"}
    clean = ast.parse("def refresh(kernel, receipt):\n    return receipt\n")
    assert legacy_names_used(clean) == set()


def test_seal_detector_catches_recursive_subpackage_module(tmp_path):
    programs = tmp_path / "programs"
    tools = tmp_path / "tools"
    nested = programs / "subpackage" / "transport.py"
    nested.parent.mkdir(parents=True)
    tools.mkdir()
    nested.write_text("def run(kernel):\n    return kernel.start_request\n")
    (tools / "clean.py").write_text("def run():\n    return None\n")

    discovered = sealed_program_files(programs_root=programs, tools_root=tools)
    assert nested in discovered
    leaked = legacy_names_used(ast.parse(nested.read_text()))
    assert leaked == {"start_request"}


def test_legacy_g2_plan_start_reserve_apis_are_sealed_legacy_only():
    from tokmo.records.ops_kernel import OpsKernelStore

    for name in sorted(LEGACY_ADMISSION_NAMES):
        method = OpsKernelStore.__dict__[name]
        doc = method.__doc__
        assert doc and "legacy" in doc.lower(), (
            f"OpsKernelStore.{name} must be marked as a sealed legacy-only "
            "G2 acceptance API"
        )


def test_new_cadence_transport_closure_requires_started_receipt_field():
    from dataclasses import MISSING as DATACLASS_MISSING
    from tokmo.values.calendar_refresh import CalendarFetchResult
    from tokmo.values.ops_kernel import CalendarStartReceiptV1

    field = CalendarFetchResult.__dataclass_fields__["receipt"]
    assert field.default is DATACLASS_MISSING  # required, no None default
    assert CalendarStartReceiptV1.__dataclass_fields__["state"]
    with pytest.raises(ValueError):
        CalendarStartReceiptV1(
            "v", __import__("datetime").datetime(2026, 8, 1, tzinfo=__import__("datetime").timezone.utc),
            __import__("tokmo.values.session_gate", fromlist=["Market"]).Market.KR,
            __import__("datetime").date(2026, 8, 1), 0,
            "calendar:KR:2026-08-01:0", "PLANNED",
        )


def test_closed_cadence_operations_match_wire_operation_specs():
    from tokmo.values.cadence_policy import CADENCE_OPERATIONS, GROUP_LIMIT_FIELDS
    from tokmo.values.request_parity import REQUEST_OPERATION_SPECS

    toss_operations = frozenset(REQUEST_OPERATION_SPECS)
    assert toss_operations <= set(CADENCE_OPERATIONS)
    # CONDITIONAL_ORDERS is a Pulse-lane operation (conditional SDS §4.4);
    # the 300s request-parity vocabulary never carries it.
    assert set(CADENCE_OPERATIONS) - toss_operations == {
        "MEMBERSHIP_NASDAQ_LISTED", "MEMBERSHIP_OTHER_LISTED",
        "MEMBERSHIP_SEC_REGISTRY", "MEMBERSHIP_SEC_ISSUER",
        "MEMBERSHIP_TOSS_STOCKS",
        "CONDITIONAL_ORDERS",
    }
    assert set(CADENCE_OPERATIONS.values()) == set(GROUP_LIMIT_FIELDS)


def test_stage2_mask_value_graph_cannot_reach_legacy_admission_surface():
    """Stage 2 mask modules are production cadence surface: the whole values
    tree (including ``open_market_mask``) may never name a legacy G2
    plan/start/reserve API."""
    values_root = ROOT / "src" / "tokmo" / "values"
    files = sorted(values_root.rglob("*.py"))
    assert (values_root / "open_market_mask.py") in files
    for path in files:
        leaked = legacy_names_used(ast.parse(path.read_text()))
        assert not leaked, (
            f"{path.relative_to(ROOT)} reaches legacy admission surface: {leaked}"
        )


def test_stage2_masked_collectors_stay_inside_the_sealed_graph():
    """The mask-admitting collectors/barrier/candidate modules must remain
    sealed program-graph members (no legacy allowlist growth), so any legacy
    plan/start/reserve call added to them fails the whole-graph seal above."""
    assert HISTORICAL_G2_PROGRAM_MODULES == frozenset({"trades_sync.py"})
    sealed = {
        path.relative_to(ROOT / "src" / "tokmo" / "programs").as_posix()
        for path in sealed_program_files()
        if "programs" in path.parts
    }
    for module in ("holdings_sync.py", "quotes_sync.py", "analysis_barrier.py",
                   "candidate_builder.py"):
        assert module in sealed, f"{module} escaped the sealed program graph"


def test_trades_sync_mask_path_never_uses_legacy_names_for_mask_admission():
    """trades_sync stays allowlisted only for its historical G2 ORDERS_PAGE
    parity; the Stage 2 mask boundary itself is enforced through the typed
    mask binding, and the module must import it."""
    source = (ROOT / "src" / "tokmo" / "programs" / "trades_sync.py").read_text()
    tree = ast.parse(source)
    imported = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        and node.module == "tokmo.values.open_market_mask"
        for alias in node.names
    }
    assert "require_tick_mask_binding" in imported


def test_toss_max_starts_derives_from_central_holdings_limit():
    from tokmo import composition
    from tokmo.values.cadence_policy import TICK_REQUEST_LIMITS_V1

    assert composition.TOSS_MAX_STARTS == TICK_REQUEST_LIMITS_V1.holdings_starts

    # Source-level: the active gate is the central declaration, not a copied
    # numeric literal — changing the one policy field changes the gate.
    tree = ast.parse(Path(composition.__file__.replace(".pyc", ".py")).read_text())
    assignment = None
    for node in ast.walk(tree):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == "TOSS_MAX_STARTS"
            for target in node.targets
        ):
            assignment = node
            break
    assert assignment is not None
    assert isinstance(assignment.value, ast.Attribute)
    assert assignment.value.attr == "holdings_starts"
