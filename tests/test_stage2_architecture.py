"""Stage 2 repair M1/M2: production APIs are mask-mandatory; legacy is sealed.

Production-facing Stage 2 seams (holdings/quotes/trades collectors and the
analysis barrier) require the open-market mask and the canonical cadence tick
identity: no ``mask=None`` default, no ``session_result``/``open_markets``
verdict reinterpretation parameter. Historical G2 compatibility lives only in
the explicitly named sealed legacy module ``tokmo.programs.legacy_g2`` (every
wrapper docstring says "legacy"), and an AST scan of the whole recursive
production graph rejects legacy wrapper imports/calls and mask omission/None
outside a closed historical allowlist. The masked barrier owns no hand-coded
DST/offset arithmetic — that stays only in the sealed legacy module (M2).
"""

import ast
from pathlib import Path

ROOT = Path(__file__).parents[1]
SRC = ROOT / "src" / "tokmo"

# Production-facing Stage 2 mask seams: (module path, function name).
PRODUCTION_MASK_FUNCTIONS = {
    "run_holdings_sync": SRC / "programs" / "holdings_sync.py",
    "run_quotes_sync": SRC / "programs" / "quotes_sync.py",
    "run_trades_sync": SRC / "programs" / "trades_sync.py",
    "assemble_analysis_materials": SRC / "programs" / "analysis_barrier.py",
}

# Private legacy-capable seams (M1): the locked implementations that still
# understand the mask-less legacy mode. Each name may be referenced only by
# its owning module (its own definition and the public masked wrapper) and by
# the sealed legacy module; reaching one from anywhere else in the recursive
# production graph — import, attribute, or bare name — is a seal violation.
PRIVATE_LEGACY_SEAMS = {
    "_locked_holdings_sync": "src/tokmo/programs/holdings_sync.py",
    "_locked_quotes_sync": "src/tokmo/programs/quotes_sync.py",
    "_locked_trades_sync": "src/tokmo/programs/trades_sync.py",
    "_assemble": "src/tokmo/programs/analysis_barrier.py",
}
LEGACY_CAPABILITY_NAME = "_LEGACY_G2_CAPABILITY"

# Every seam name — public or private — whose calls must carry a real mask.
MASK_SEAM_NAMES = frozenset(PRODUCTION_MASK_FUNCTIONS) | frozenset(PRIVATE_LEGACY_SEAMS)

LEGACY_MODULE = SRC / "programs" / "legacy_g2.py"
LEGACY_WRAPPER_NAMES = frozenset({
    "legacy_run_holdings_sync",
    "legacy_run_quotes_sync",
    "legacy_run_trades_sync",
    "legacy_assemble_analysis_materials",
})

# Closed historical allowlist (ROOT-relative): the sealed legacy module
# itself plus the named historical G2 acceptance runner that predates the
# mask. Nothing else in the recursive production graph may import or call
# the legacy wrappers.
LEGACY_CALLER_ALLOWLIST = frozenset({
    "src/tokmo/programs/legacy_g2.py",
    "src/tokmo/programs/integrated_fake_cadence.py",
})


def production_graph_files():
    """The whole recursive production source graph plus repository tools."""
    files = sorted((SRC).rglob("*.py"))
    files.extend(sorted((ROOT / "tools").rglob("*.py")))
    assert files, "production graph missing"
    return files


def function_def(tree, name):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None


def _kwonly_default(node, arg_name):
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if arg.arg == arg_name:
            return True, default
    return False, None


def all_param_names(node):
    args = node.args
    return {
        arg.arg
        for arg in (args.posonlyargs + args.args + args.kwonlyargs)
    }


# -- production signatures: mask + full tick identity are mandatory ---------------


def test_production_mask_functions_require_mask_and_tick_without_defaults():
    for name, path in PRODUCTION_MASK_FUNCTIONS.items():
        node = function_def(ast.parse(path.read_text()), name)
        assert node is not None, f"{name} missing from {path.name}"
        for required in ("mask", "tick"):
            present, default = _kwonly_default(node, required)
            assert present, f"{name} must take keyword-only '{required}'"
            assert default is None, (
                f"{name} '{required}' must have no default (mask omission "
                "and mask=None are production contract failures)"
            )


def test_production_mask_functions_take_no_session_reinterpretation_params():
    for name, path in PRODUCTION_MASK_FUNCTIONS.items():
        node = function_def(ast.parse(path.read_text()), name)
        params = all_param_names(node)
        assert "session_result" not in params, (
            f"{name} must not reinterpret session verdicts"
        )
        assert "open_markets" not in params, (
            f"{name} must not accept a caller-chosen open set"
        )


# -- sealed legacy module ----------------------------------------------------------


def test_legacy_module_exists_and_every_wrapper_is_marked_legacy():
    assert LEGACY_MODULE.is_file(), "sealed legacy module missing"
    tree = ast.parse(LEGACY_MODULE.read_text())
    module_doc = ast.get_docstring(tree) or ""
    assert "legacy" in module_doc.lower()
    defined = {
        node.name: node for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    for name in sorted(LEGACY_WRAPPER_NAMES):
        assert name in defined, f"sealed legacy wrapper {name} missing"
        doc = ast.get_docstring(defined[name]) or ""
        assert "legacy" in doc.lower(), f"{name} docstring must say legacy"


def legacy_reachable(tree) -> set:
    """Legacy wrapper names reachable as imports, attributes, or bare names."""
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module and "legacy_g2" in node.module:
                reached.add(node.module)
            reached.update(
                alias.name for alias in node.names
                if alias.name in LEGACY_WRAPPER_NAMES
            )
        elif isinstance(node, ast.Import):
            reached.update(
                alias.name for alias in node.names if "legacy_g2" in alias.name
            )
        elif isinstance(node, ast.Attribute) and node.attr in LEGACY_WRAPPER_NAMES:
            reached.add(node.attr)
        elif isinstance(node, ast.Name) and node.id in LEGACY_WRAPPER_NAMES:
            reached.add(node.id)
    return reached


def test_whole_production_graph_cannot_reach_legacy_wrappers():
    for path in production_graph_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in LEGACY_CALLER_ALLOWLIST:
            continue
        reached = legacy_reachable(ast.parse(path.read_text()))
        assert not reached, f"{relative} reaches sealed legacy surface: {reached}"


def test_legacy_caller_allowlist_is_closed_and_real():
    for relative in LEGACY_CALLER_ALLOWLIST:
        assert (ROOT / relative).is_file(), f"stale allowlist entry: {relative}"
    assert LEGACY_CALLER_ALLOWLIST == frozenset({
        "src/tokmo/programs/legacy_g2.py",
        "src/tokmo/programs/integrated_fake_cadence.py",
    })


# -- call sites: mask can be neither omitted nor None ------------------------------


def masked_call_violations(tree) -> list:
    """Calls to production mask seams that omit mask or pass mask=None."""
    violations = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else None
        )
        if name not in MASK_SEAM_NAMES:
            continue
        keywords = {keyword.arg: keyword.value for keyword in node.keywords}
        if "mask" not in keywords and None not in keywords:  # **kwargs escapes via None key
            violations.append((node.lineno, name, "mask omitted"))
        mask_value = keywords.get("mask")
        if (isinstance(mask_value, ast.Constant) and mask_value.value is None):
            violations.append((node.lineno, name, "mask=None"))
    return violations


def test_whole_production_graph_never_calls_mask_seams_without_mask():
    for path in production_graph_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative in LEGACY_CALLER_ALLOWLIST:
            continue
        violations = masked_call_violations(ast.parse(path.read_text()))
        assert not violations, f"{relative}: {violations}"


def test_masked_call_detector_catches_omission_and_none():
    offending = ast.parse(
        "def tick():\n"
        "    run_quotes_sync(tick=t, mask=None)\n"
        "    run_holdings_sync(tick=t)\n"
        "    module.run_trades_sync(tick=t, mask=None)\n"
    )
    kinds = {(name, kind) for _line, name, kind in masked_call_violations(offending)}
    assert kinds == {
        ("run_quotes_sync", "mask=None"),
        ("run_holdings_sync", "mask omitted"),
        ("run_trades_sync", "mask=None"),
    }
    clean = ast.parse("run_quotes_sync(tick=t, mask=mask)")
    assert masked_call_violations(clean) == []


def test_legacy_reach_detector_catches_import_and_call_forms():
    offending = ast.parse(
        "from tokmo.programs.legacy_g2 import legacy_run_trades_sync\n"
        "import tokmo.programs.legacy_g2 as lg\n"
        "lg.legacy_run_holdings_sync()\n"
    )
    reached = legacy_reachable(offending)
    assert "legacy_run_trades_sync" in reached
    assert "legacy_run_holdings_sync" in reached
    assert any("legacy_g2" in item for item in reached)


# -- M1: private legacy-capable seams are sealed too -------------------------------


def private_seam_reachable(tree) -> set:
    """Private seam/capability names reachable as (kind, name) tuples: any
    import, attribute access, or bare-name use of a locked implementation or
    of the module-private legacy capability."""
    watched = frozenset(PRIVATE_LEGACY_SEAMS) | {LEGACY_CAPABILITY_NAME}
    reached = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            reached.update(
                ("import", alias.name) for alias in node.names
                if alias.name in watched
            )
        elif isinstance(node, ast.Attribute) and node.attr in watched:
            reached.add(("attribute", node.attr))
        elif isinstance(node, ast.Name) and node.id in watched:
            reached.add(("name", node.id))
    return reached


def test_whole_production_graph_cannot_reach_private_legacy_seams():
    owners = {path: name for name, path in PRIVATE_LEGACY_SEAMS.items()}
    for path in production_graph_files():
        relative = path.relative_to(ROOT).as_posix()
        if relative == "src/tokmo/programs/legacy_g2.py":
            continue
        reached = private_seam_reachable(ast.parse(path.read_text()))
        allowed = set()
        if relative in owners:
            # The owning module may use its own seam and its own capability
            # as bare names (definition + the masked public wrapper's call);
            # it may never import another module's seam or capability.
            allowed = {
                ("name", owners[relative]), ("name", LEGACY_CAPABILITY_NAME),
            }
        assert reached <= allowed, (
            f"{relative} reaches private legacy seam surface: {reached - allowed}"
        )


def test_private_seam_detector_catches_import_attribute_and_bare_forms():
    offending = ast.parse(
        "from tokmo.programs.holdings_sync import _locked_holdings_sync\n"
        "import tokmo.programs.quotes_sync as qs\n"
        "qs._locked_quotes_sync(tick=t, mask=None)\n"
        "_assemble(tick=t, mask=None)\n"
        "_locked_trades_sync(tick=t, mask=None)\n"
        "from tokmo.programs.trades_sync import _LEGACY_G2_CAPABILITY\n"
    )
    reached = private_seam_reachable(offending)
    assert ("import", "_locked_holdings_sync") in reached
    assert ("attribute", "_locked_quotes_sync") in reached
    assert ("name", "_assemble") in reached
    assert ("name", "_locked_trades_sync") in reached
    assert ("import", LEGACY_CAPABILITY_NAME) in reached
    clean = ast.parse("run_quotes_sync(tick=t, mask=mask)")
    assert private_seam_reachable(clean) == set()


def test_masked_call_detector_covers_private_seams():
    offending = ast.parse(
        "def tick():\n"
        "    _locked_trades_sync(tick=t, mask=None)\n"
        "    _locked_holdings_sync(tick=t)\n"
        "    module._locked_quotes_sync(tick=t, mask=None)\n"
        "    _assemble(tick=t, mask=None)\n"
    )
    kinds = {(name, kind) for _line, name, kind in masked_call_violations(offending)}
    assert kinds == {
        ("_locked_trades_sync", "mask=None"),
        ("_locked_holdings_sync", "mask omitted"),
        ("_locked_quotes_sync", "mask=None"),
        ("_assemble", "mask=None"),
    }


# -- M1: runtime capability — mask=None is unreachable without the sealed token ----


def _seam_calls(tmp_path):
    """Direct private-seam calls with mask=None and junk collaborators: the
    capability seal must reject them before any collaborator is touched."""
    from tokmo.programs.analysis_barrier import _assemble
    from tokmo.programs.holdings_sync import _locked_holdings_sync
    from tokmo.programs.quotes_sync import _locked_quotes_sync
    from tokmo.programs.trades_sync import _locked_trades_sync

    def seam(target, **base):
        def build(**extra):
            kwargs = dict(base)
            kwargs.update(extra)
            return lambda: target(**kwargs)
        return build

    holdings = seam(
        _locked_holdings_sync,
        tick=object(), session_result=object(), holdings_provider=None,
        store=None, pack_writer=None, receipt_store=None,
        reservation_seal=None, incident_writer=None, clock=None, now=None,
        job_lock_path=tmp_path / "h.lock", mask=None,
    )
    quotes = seam(
        _locked_quotes_sync,
        tick=object(), session_result=object(), holdings=None,
        quotes_provider=None, quote_store=None, pack_writer=None,
        receipt_store=None, reservation_seal=None, incident_writer=None,
        clock=None, now=None, job_lock_path=tmp_path / "q.lock", mask=None,
    )
    trades = seam(
        _locked_trades_sync,
        tick=object(), session_result=object(), holdings=None,
        orders_provider=None, detail_provider=None, store=None,
        scan_state=None, pack_writer=None, receipt_store=None,
        reservation_seal=None, incident_writer=None,
        regular_session_dates=None, clock=None,
        now=None, job_lock_path=tmp_path / "t.lock", ops_kernel=None,
        invocation_id=None, mask=None,
    )
    assemble = seam(
        _assemble,
        tick=object(), open_markets=None, receipt_readers=None,
        pack_reader=None, now=None, mask=None,
        legacy_market_date_ok=lambda *args: True,
    )
    return holdings, quotes, trades, assemble


def test_private_seams_reject_mask_none_without_the_legacy_capability(tmp_path):
    import pytest
    for seam in _seam_calls(tmp_path):
        with pytest.raises(PermissionError):
            seam()()
        with pytest.raises(PermissionError):
            seam(legacy=object())()  # a forged token is not the capability


def test_capability_cannot_launder_a_masked_production_call(tmp_path):
    import pytest
    from tokmo.programs import (
        analysis_barrier, holdings_sync, quotes_sync, trades_sync,
    )
    modules = (holdings_sync, quotes_sync, trades_sync, analysis_barrier)
    for seam, module in zip(_seam_calls(tmp_path), modules):
        capability = getattr(module, LEGACY_CAPABILITY_NAME)
        # mask present together with the legacy capability is a contradiction
        with pytest.raises(PermissionError):
            seam(legacy=capability, mask=object())()


def test_legacy_wrappers_present_the_capability_and_still_run(tmp_path):
    # The sealed legacy module reaches the mask-less mode; a plain production
    # call cannot. The wrapper gets past the capability seal and fails only on
    # its junk collaborators, never with the seal's PermissionError.
    import pytest
    from tokmo.programs.legacy_g2 import legacy_assemble_analysis_materials
    with pytest.raises((TypeError, ValueError)):
        legacy_assemble_analysis_materials(
            tick=object(), open_markets=None, receipt_readers=None,
            pack_reader=None, now=None,
        )


# -- M2: no hand-coded DST/offset arithmetic in the masked production barrier ------


def test_masked_barrier_owns_no_hand_dst_and_uses_zoneinfo():
    tree = ast.parse((SRC / "programs" / "analysis_barrier.py").read_text())
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_us_dst" not in defined
    assert "_local_date" not in defined
    imports = {
        alias.name
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        for alias in node.names
    } | {
        node.module
        for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert "zoneinfo" in imports or "ZoneInfo" in imports


def test_legacy_module_owns_the_hand_dst_calculation():
    tree = ast.parse(LEGACY_MODULE.read_text())
    defined = {
        node.name for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "_legacy_us_dst" in defined
    assert "_legacy_local_date" in defined
