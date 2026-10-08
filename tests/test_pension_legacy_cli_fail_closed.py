"""The retired legacy pension CLI refuses everything and changes nothing.

`tools/import_pension_excel.py` used to activate a pension revision and only
afterwards write the canonical holdings snapshot -- resolving that snapshot at
`<root>/stores/`, where no canonical database lives. On a real root the
snapshot step failed AFTER current state had already been mutated. These tests
reproduce that invocation on a production-SHAPED generation-1 root and prove
the tool now refuses before touching anything.

Every fixture is synthetic and lives under ``tmp_path``.
"""
from __future__ import annotations

import ast
import hashlib
import importlib.abc
import importlib.util
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

from tokmo.records.holdings_snapshots import holdings_snapshot_store_path

REPO_ROOT = Path(__file__).resolve().parents[1]
LEGACY_TOOL = REPO_ROOT / "tools" / "import_pension_excel.py"

# The exact invocation the reviewer reproduced.
REPRODUCED_ARGUMENTS = ("--file", "{workbook}", "--root", "{root}", "--approve")


def load_legacy_tool():
    spec = importlib.util.spec_from_file_location(
        "synthetic_legacy_pension_tool", LEGACY_TOOL)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# A production-SHAPED generation-1 root
# ---------------------------------------------------------------------------

@pytest.fixture()
def generation_one_root(tmp_path) -> Path:
    """The real topology, at the schema generation production is on today.

    ``toss/holdings_snapshots.sqlite3`` exists and already holds a Toss
    capture; ``stores/pension_registry.sqlite3`` holds one ACTIVE revision
    with its pointer and activation event. Neither store is migrated, so this
    is generation 1 on both sides -- the state the legacy tool would meet.
    """
    from tests.test_pension_current_read_model import clock, seven
    from tests.test_pension_snapshot_generation_migration import (
        toss_capture, toss_permit,
    )
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    from tokmo.records.pension_revisions import PensionRegistryStore

    root = tmp_path / "data-root"
    root.mkdir(mode=0o700, parents=True)
    (root / "toss").mkdir(mode=0o700)
    (root / "stores").mkdir(mode=0o700)

    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    capture = toss_capture()
    receipt = snapshots.append(capture, toss_permit(capture.snapshot_id))
    assert receipt.status == "SAVED", receipt.reason
    assert stored_generation(holdings_snapshot_store_path(root)) == "1"

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    revision = seven()
    registry.create_candidate(revision)
    registry.promote_to_pending(revision.revision_id)
    registry.activate(revision.revision_id, approval_reason="synthetic-existing")
    assert registry.schema_generation() == 1
    return root


@pytest.fixture()
def landing_workbook(tmp_path) -> Path:
    """A synthetic landing file in the LEGACY workbook form.

    It has to be the legacy form, not the new account-status form: otherwise
    the base-commit tool would stop at ``parse failed`` and the RED run would
    never reach the mutation this test exists to forbid. With this fixture the
    base tool parses successfully, activates the revision, and only then dies
    on the wrong snapshot path -- which is exactly the reported defect.
    """
    from tests.test_pension_excel_parser import _make_workbook

    rows = [
        {
            "instrument_name": f"SYNTH LEGACY ETF {n}",
            "symbol": f"90000{n}",
            "market": "KR",
            "currency": "KRW",
            "quantity": 10,
            "average_cost": 1000,
            "source_as_of": datetime(2026, 8, 13),
        }
        for n in range(1, 4)
    ]
    path = tmp_path / "synthetic-legacy-landing.xlsx"
    path.write_bytes(_make_workbook(rows))
    os.chmod(path, 0o600)
    return path


def stored_generation(path: Path) -> str:
    """Read the snapshot store's declared schema generation, read-only."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return connection.execute(
            "SELECT value FROM storage_metadata"
            " WHERE key='holdings_snapshot_schema_version'").fetchone()[0]
    finally:
        connection.close()


def registry_path(root: Path) -> Path:
    return root / "stores" / "pension_registry.sqlite3"


def digest_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def registry_state(root: Path) -> dict[str, object]:
    """Everything the legacy tool used to mutate: rows AND raw bytes."""
    connection = sqlite3.connect(f"file:{registry_path(root)}?mode=ro", uri=True)
    try:
        return {
            "bytes": digest_of(registry_path(root)),
            "size": registry_path(root).stat().st_size,
            "revisions": connection.execute(
                "SELECT revision_id, state FROM pension_revisions"
                " ORDER BY revision_id").fetchall(),
            "pointer": connection.execute(
                "SELECT * FROM pension_active_pointer").fetchall(),
            "activations": connection.execute(
                "SELECT count(*) FROM pension_activation_events").fetchone()[0],
        }
    finally:
        connection.close()


def snapshot_state(root: Path) -> dict[str, object]:
    path = holdings_snapshot_store_path(root)
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        counts = {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("holdings_snapshots", "holdings_snapshot_accounts",
                          "holdings_snapshot_items")
        }
    finally:
        connection.close()
    return {"bytes": digest_of(path), "size": path.stat().st_size, **counts}


def snapshot_databases_under(root: Path) -> list[str]:
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("holdings_snapshots.sqlite3*")
    )


# ---------------------------------------------------------------------------
# The reproduced invocation, as a real subprocess
# ---------------------------------------------------------------------------

def run_legacy(root: Path, *arguments: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT / "src"), str(REPO_ROOT), environment.get("PYTHONPATH", "")])
    return subprocess.run(
        [sys.executable, str(LEGACY_TOOL), *arguments],
        capture_output=True, text=True, cwd=REPO_ROOT, env=environment)


def test_the_reproduced_approve_invocation_is_refused_and_changes_nothing(
        generation_one_root, landing_workbook):
    """The reviewer's exact repro: RC nonzero, and nothing moved."""
    root = generation_one_root
    registry_before = registry_state(root)
    snapshot_before = snapshot_state(root)
    workbook_before = digest_of(landing_workbook)

    result = run_legacy(
        root, "--file", str(landing_workbook), "--root", str(root), "--approve")

    # 1. Nonzero exit with a clear deprecation message naming both successors.
    assert result.returncode != 0
    assert result.returncode == 2
    message = result.stdout + result.stderr
    assert "DEPRECATED" in message
    assert "REFUSED" in message
    assert "tools/import_pension_account_status.py" in message
    assert "tools/import_pension_transaction_detail.py" in message

    # 2. Registry bytes, revision rows, pointer rows, activation events.
    assert registry_state(root) == registry_before

    # 3. No second snapshot authority was created anywhere under the root.
    assert not (root / "stores" / "holdings_snapshots.sqlite3").exists()
    assert snapshot_databases_under(root) == ["toss/holdings_snapshots.sqlite3"]

    # 4. The canonical snapshot database is untouched, bytes and rows alike.
    assert snapshot_state(root) == snapshot_before

    # 5. The landing workbook survives. The legacy tool used to unlink it.
    assert landing_workbook.exists()
    assert digest_of(landing_workbook) == workbook_before


@pytest.mark.parametrize("arguments", [
    pytest.param(("--file", "{workbook}", "--root", "{root}"), id="default"),
    pytest.param(("--file", "{workbook}", "--preview-only"), id="preview-only"),
    pytest.param(("--file", "{workbook}", "--root", "{root}", "--approve",
                  "--source-as-of", "2026-08-13"), id="approve-with-as-of"),
    pytest.param((), id="no-arguments"),
    pytest.param(("--unknown-flag",), id="unknown-flag"),
])
def test_every_other_invocation_is_refused_too(
        generation_one_root, landing_workbook, arguments):
    """Including --preview-only: the refusal is total, not approve-only."""
    root = generation_one_root
    registry_before = registry_state(root)
    snapshot_before = snapshot_state(root)

    result = run_legacy(root, *(
        argument.format(workbook=landing_workbook, root=root)
        for argument in arguments))

    assert result.returncode == 2
    assert "REFUSED" in result.stderr
    assert registry_state(root) == registry_before
    assert snapshot_state(root) == snapshot_before
    assert landing_workbook.exists()
    assert snapshot_databases_under(root) == ["toss/holdings_snapshots.sqlite3"]


def test_only_help_succeeds_and_it_still_says_deprecated(generation_one_root):
    result = run_legacy(generation_one_root, "--help")
    assert result.returncode == 0
    assert "DEPRECATED" in result.stdout
    assert "tools/import_pension_account_status.py" in result.stdout
    assert snapshot_state(generation_one_root)  # store still readable
    assert not (generation_one_root / "stores" / "holdings_snapshots.sqlite3").exists()


# ---------------------------------------------------------------------------
# The refusal precedes imports and store constructors -- structurally
# ---------------------------------------------------------------------------

def test_the_module_imports_nothing_from_tokmo_at_all():
    """The strongest available proof that no store can be constructed: the
    file has no way to reach one. This is a property of the module, not a
    claim about which branch runs."""
    source = LEGACY_TOOL.read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")
    assert imported == ["__future__", "sys"], imported

    # Prose is allowed to name what the tool no longer does, so scan the
    # EXECUTABLE identifiers rather than the file text: every name and
    # attribute the module can actually evaluate.
    used = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert used <= {
        "sys", "argv", "arguments", "argument", "list", "print", "stderr",
        "main", "HELP", "REFUSAL", "REFUSED_EXIT_CODE",
        "NEW_HOLDINGS_ENTRYPOINT", "NEW_LEDGER_ENTRYPOINT",
        "SystemExit", "__name__",
        "list", "str", "int",   # the type annotations on main()
    }, sorted(used)


def test_the_module_declares_no_database_path():
    """It must not name any store, canonical or otherwise -- including the
    wrong `stores/` snapshot path it used to carry."""
    source = LEGACY_TOOL.read_text(encoding="utf-8")
    assert ".sqlite3" not in source
    assert "stores/holdings_snapshots" not in source


def test_the_refusal_happens_before_any_tokmo_import_could_occur(monkeypatch):
    """Behavioural companion to the structural test: with any fresh ``tokmo``
    import made fatal, the refusal still returns cleanly."""

    class Tripwire(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            if fullname == "tokmo" or fullname.startswith("tokmo."):
                raise AssertionError(f"legacy tool reached {fullname}")
            return None

    module = load_legacy_tool()
    monkeypatch.setattr(sys, "meta_path", [Tripwire(), *sys.meta_path])
    assert module.main(["--file", "x.xlsx", "--root", "r", "--approve"]) == 2
    assert module.main([]) == 2
    assert module.main(["--help"]) == 0


def test_the_tool_has_no_write_capable_code_path():
    """No call in the module can create, open, or remove anything."""
    tree = ast.parse(LEGACY_TOOL.read_text(encoding="utf-8"))
    called = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            function = node.func
            if isinstance(function, ast.Name):
                called.add(function.id)
            elif isinstance(function, ast.Attribute):
                called.add(function.attr)
    assert called <= {"print", "list", "main", "SystemExit"}, sorted(called)
