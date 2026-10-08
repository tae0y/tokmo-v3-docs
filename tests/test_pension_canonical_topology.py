"""PT-01: pension operator paths must resolve the CANONICAL snapshot database.

The canonical holdings snapshot store has always lived at
``<data root>/toss/holdings_snapshots.sqlite3`` — that is where
``composition._holdings_snapshot_store_path`` resolves, where the real
production database is, and what the canary protected-path list names.

A production preflight found the pension operator tools resolving
``<root>/stores/holdings_snapshots.sqlite3`` instead, which would either fail or
create a SECOND parallel snapshot authority. The synthetic tests had built that
same wrong topology themselves, so they passed while being wrong.

These tests therefore pin the topology against the canonical resolver rather
than against anything this slice declares, and they build a
production-SHAPED root:

    <root>/toss/holdings_snapshots.sqlite3     the one snapshot authority
    <root>/stores/pension_registry.sqlite3     current read model
    <root>/stores/trade_ledger.sqlite3         execution ledger

Everything here is synthetic and lives under ``tmp_path``.
"""
from __future__ import annotations

import ast
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tokmo.records.holdings_snapshots import (
    HOLDINGS_SNAPSHOT_RELATIVE_PATH,
    holdings_snapshot_store_path,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
DATA_LEAF = (".local", "share", "tokmo-v3")

IMPORT_TOOL = REPO_ROOT / "tools" / "import_pension_account_status.py"
DETAIL_TOOL = REPO_ROOT / "tools" / "import_pension_transaction_detail.py"
REGISTRY_TOOL = (
    REPO_ROOT / "tools" / "migrate_pension_registry_current_model.py")
SNAPSHOT_TOOL = REPO_ROOT / "tools" / "migrate_holdings_snapshots_v1_to_v2.py"

OPERATOR_TOOLS = (IMPORT_TOOL, DETAIL_TOOL, REGISTRY_TOOL)

# Retired. It resolved the wrong `stores/` snapshot path AFTER activating
# the revision, so it is fail-closed rather than repointed. It is no longer
# an exception to the guards below; it must satisfy them like anything else.
LEGACY_TOOL = REPO_ROOT / "tools" / "import_pension_excel.py"


# ---------------------------------------------------------------------------
# Parity with the canonical resolver
# ---------------------------------------------------------------------------

def test_both_composition_resolvers_agree_with_the_shared_declaration():
    """`composition.py` is SEALED byte-identical by another slice's contract
    (`tests/test_ai_infra_capex_architecture.py`), so it cannot be made to
    delegate. It keeps its own two literal spellings. Parity is therefore not
    structural and must be pinned here, executably: both composition resolvers
    are called for real and compared against the shared declaration. If either
    side ever moves, this fails.
    """
    from tokmo import composition

    home = Path("/synthetic/home")
    expected = holdings_snapshot_store_path(home.joinpath(*DATA_LEAF))

    assert composition._holdings_snapshot_store_path(home) == expected
    # _holdings_route_paths returns (cutover, snapshots, store, lock).
    assert composition._holdings_route_paths(home)[1] == expected


def test_the_shared_resolver_matches_the_canonical_composition_resolver():
    """The one source of truth, compared against the resolver production uses."""
    from tokmo.composition import _holdings_snapshot_store_path

    home = Path("/synthetic-home")
    data_root = home.joinpath(*DATA_LEAF)
    assert holdings_snapshot_store_path(data_root) == \
        _holdings_snapshot_store_path(home)


def test_the_relative_path_matches_the_canary_protected_path():
    """The canary protected-path list names the same relative location."""
    from tokmo.programs.canary_safety_gate import DEFAULT_PROTECTED_PATHS

    assert "/".join(HOLDINGS_SNAPSHOT_RELATIVE_PATH) in DEFAULT_PROTECTED_PATHS
    # And the registry/ledger stay under stores/, as that same list says.
    assert "stores/pension_registry.sqlite3" in DEFAULT_PROTECTED_PATHS
    assert "stores/trade_ledger.sqlite3" in DEFAULT_PROTECTED_PATHS


def test_the_relative_path_is_the_toss_leaf():
    assert HOLDINGS_SNAPSHOT_RELATIVE_PATH == (
        "toss", "holdings_snapshots.sqlite3")


@pytest.mark.parametrize("tool", OPERATOR_TOOLS, ids=lambda p: p.name)
def test_no_operator_tool_declares_a_stores_snapshot_path(tool):
    """The exact defect: a snapshot database under ``stores/``."""
    text = tool.read_text(encoding="utf-8")
    assert '"stores", "holdings_snapshots.sqlite3"' not in text
    assert "stores/holdings_snapshots.sqlite3" not in text
    assert 'SNAPSHOT_FILENAME = "holdings_snapshots.sqlite3"' not in text


@pytest.mark.parametrize("tool", OPERATOR_TOOLS, ids=lambda p: p.name)
def test_every_operator_tool_uses_the_shared_resolver(tool):
    text = tool.read_text(encoding="utf-8")
    assert "holdings_snapshot_store_path" in text


def test_the_retired_legacy_tool_declares_no_store_and_refuses():
    """The legacy CLI is a must-fail-closed surface, not an allowlisted one.

    Behaviour is proved in tests/test_pension_legacy_cli_fail_closed.py; this
    guard exists so the topology suite itself fails if the tool ever regains a
    store path or a write-capable branch.
    """
    text = LEGACY_TOOL.read_text(encoding="utf-8")
    assert ".sqlite3" not in text
    assert "stores/holdings_snapshots" not in text
    assert "REFUSED" in text
    assert "DEPRECATED" in text
    assert "import_pension_account_status.py" in text

    tree = ast.parse(text)
    imported = [
        node.module or "" for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ] + [
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    ]
    assert not any(name.startswith("tokmo") for name in imported), imported


def test_no_module_in_the_delta_redeclares_the_snapshot_filename():
    """Only the shared leaf may spell the canonical snapshot filename."""
    allowed = {
        "holdings_snapshots.py",     # the one shared declaration
        # SEALED byte-identical at the base commit by another slice; it
        # keeps its two pre-existing literals, pinned for parity above.
        "composition.py",
        "canary_safety_gate.py",     # the pre-existing protected-path list
        "holdings_snapshot_migration.py",   # frozen migration DDL/table names
    }
    offenders = []
    for root in ("src", "tools"):
        for path in sorted((REPO_ROOT / root).rglob("*.py")):
            if "__pycache__" in path.parts or path.name in allowed:
                continue
            if "holdings_snapshots.sqlite3" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
    assert offenders == [], offenders


# ---------------------------------------------------------------------------
# A production-SHAPED root
# ---------------------------------------------------------------------------

def run_tool(tool: Path, *args: str, fault: str | None = None):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    if fault is not None:
        environment["TOKMO_MIGRATION_FAULT"] = fault
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True, text=True, env=environment, cwd=str(REPO_ROOT))


def production_shaped_root(tmp_path) -> Path:
    """A root laid out exactly like the real data root.

    ``toss/holdings_snapshots.sqlite3`` already exists and already holds Toss
    captures, exactly as production does; the pension lane must join it, never
    create a second database.
    """
    from tests.test_pension_current_read_model import clock, seven
    from tests.test_pension_snapshot_generation_migration import (
        migrate_to_generation_two, toss_capture, toss_permit,
    )
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    from tokmo.records.pension_revisions import PensionRegistryStore

    root = tmp_path / "data-root"
    root.mkdir(mode=0o700, parents=True)
    (root / "toss").mkdir(mode=0o700)
    (root / "stores").mkdir(mode=0o700)

    snapshot_path = holdings_snapshot_store_path(root)
    store = HoldingsSnapshotStore(snapshot_path, clock=clock)
    capture = toss_capture()
    receipt = store.append(capture, toss_permit(capture.snapshot_id))
    assert receipt.status == "SAVED", receipt.reason
    migrate_to_generation_two(snapshot_path, tmp_path)

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    old = seven()
    registry.create_candidate(old)
    registry.promote_to_pending(old.revision_id)
    registry.activate(old.revision_id, approval_reason="synthetic-existing")
    return root


def authorization(tmp_path: Path, digest: str) -> Path:
    record = tmp_path / "pt01_authorization.txt"
    record.write_text(f"approved-source-sha256={digest}\n", encoding="utf-8")
    return record


def migrated_root(tmp_path) -> Path:
    root = production_shaped_root(tmp_path)
    registry_path = root / "stores" / "pension_registry.sqlite3"
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "pt01" / "reg.bak"),
        "--manifest", str(tmp_path / "pt01" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)))
    assert result.returncode == 0, (result.stdout, result.stderr)
    return root


def assert_no_parallel_snapshot_database(root: Path) -> None:
    """The whole point: exactly one snapshot database, in the canonical place."""
    assert not (root / "stores" / "holdings_snapshots.sqlite3").exists()
    found = sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("holdings_snapshots.sqlite3*")
    )
    assert found == ["toss/holdings_snapshots.sqlite3"], found


def pension_capture_count(root: Path) -> int:
    from tests.test_pension_current_read_model import clock
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
    return len(HoldingsSnapshotStore(
        holdings_snapshot_store_path(root),
        clock=clock).list_pension_capture_headers())


def test_registry_migration_prerequisite_reads_the_canonical_snapshot(tmp_path):
    """The order gate must look at toss/, or it would never see the real store."""
    root = production_shaped_root(tmp_path)
    registry_path = root / "stores" / "pension_registry.sqlite3"
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "pt01" / "reg.bak"),
        "--manifest", str(tmp_path / "pt01" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)))
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert "snapshot store already admits the pension source" in result.stdout
    assert_no_parallel_snapshot_database(root)


# ---------------------------------------------------------------------------
# Candidate + activation against the production shape
# ---------------------------------------------------------------------------

def plan_and_workbook(tmp_path):
    from tests.test_pension_account_status_parser import (
        build_status_workbook, etf_symbol, landing,
    )
    from tokmo.values.pension_account_status import (
        CLASSIFICATION_PLAN_VERSION,
        DISPOSITION_EXCLUDED_NON_ETF,
        DISPOSITION_VERIFIED_KRX_ETF,
        EXCLUSION_CASH_LIKE,
        EXCLUSION_CORPORATE_BOND,
        EXCLUSION_MUTUAL_FUND,
    )
    workbook = landing(tmp_path, build_status_workbook(), "pt01_status.xlsx")
    rows = [
        {"ordinal": n, "disposition": DISPOSITION_VERIFIED_KRX_ETF,
         "symbol": etf_symbol(n)} for n in range(1, 11)
    ] + [
        {"ordinal": 11, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CORPORATE_BOND},
        {"ordinal": 12, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CORPORATE_BOND},
        {"ordinal": 13, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_MUTUAL_FUND},
        {"ordinal": 14, "disposition": DISPOSITION_EXCLUDED_NON_ETF,
         "exclusion_kind": EXCLUSION_CASH_LIKE},
    ]
    plan = tmp_path / "pt01_plan.json"
    plan.write_text(json.dumps({
        "plan_version": CLASSIFICATION_PLAN_VERSION,
        "expected_row_count": 14, "expected_etf_count": 10,
        "expected_excluded_count": 4, "rows": rows}), encoding="utf-8")
    return workbook, plan


def test_candidate_and_activation_use_only_the_canonical_snapshot(tmp_path):
    from tests.test_pension_current_read_model import clock
    from tokmo.records.pension_revisions import PensionRegistryStore

    root = migrated_root(tmp_path)
    workbook, plan = plan_and_workbook(tmp_path)
    toss_before = holdings_snapshot_store_path(root).stat().st_size

    created = run_tool(
        IMPORT_TOOL, "candidate", "--file", str(workbook), "--plan", str(plan),
        "--root", str(root), "--source-as-of", "2026-08-13")
    assert created.returncode == 0, created.stderr
    revision_id = next(
        line.split(":", 1)[1].strip()
        for line in created.stdout.splitlines()
        if line.strip().startswith("revision_id:"))
    assert_no_parallel_snapshot_database(root)

    activated = run_tool(
        IMPORT_TOOL, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", "2026-08-13")
    assert activated.returncode == 0, activated.stderr

    assert_no_parallel_snapshot_database(root)
    # The two captures landed in the pre-existing canonical database.
    assert pension_capture_count(root) == 2
    assert holdings_snapshot_store_path(root).stat().st_size >= toss_before

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert registry.read_active_revision().holding_count == 10


def test_the_existing_toss_captures_survive_the_pension_activation(tmp_path):
    from tests.test_pension_current_read_model import clock
    from tokmo.records.holdings_snapshots import HoldingsSnapshotStore

    root = migrated_root(tmp_path)
    store = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    toss_before = store.read_latest()

    workbook, plan = plan_and_workbook(tmp_path)
    created = run_tool(
        IMPORT_TOOL, "candidate", "--file", str(workbook), "--plan", str(plan),
        "--root", str(root), "--source-as-of", "2026-08-13")
    revision_id = next(
        line.split(":", 1)[1].strip()
        for line in created.stdout.splitlines()
        if line.strip().startswith("revision_id:"))
    run_tool(IMPORT_TOOL, "activate", "--root", str(root),
             "--revision-id", revision_id, "--approval-reason", "synthetic",
             "--file", str(workbook), "--plan", str(plan),
             "--source-as-of", "2026-08-13")

    assert store.read_latest() == toss_before
    assert {header.source for header in store.list_capture_headers()} == {
        "toss-holdings-v1"}


# ---------------------------------------------------------------------------
# Transaction detail against the production shape
# ---------------------------------------------------------------------------

def test_transaction_detail_pre_snapshot_uses_only_the_canonical_snapshot(
        tmp_path):
    from tokmo.programs.pension_snapshot_adapter import (
        APPROVED_DETAIL_ACCOUNT_SCOPE,
        APPROVED_DETAIL_IMPORTER_VERSION,
        APPROVED_DETAIL_SOURCE,
        TRANSACTION_DETAIL_PLAN_VERSION,
    )
    root = migrated_root(tmp_path)
    document = {
        "record_kind": "TRANSACTION_DETAIL",
        "records": [
            {"source_record_key": f"row-{n:06d}", "trade_date": "2026-07-01",
             "transaction_type_raw": "synthetic-buy",
             "instrument_raw": f"SYNTH-LEGACY-{n:02d}", "quantity": "10",
             "unit_price": "1000", "amount": "10000", "medium_raw": "SYNTH",
             "currency_raw": "KRW"}
            for n in range(1, 4)
        ],
    }
    batch = tmp_path / "pt01_batch.json"
    batch.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()
    plan = tmp_path / "pt01_batch_plan.json"
    plan.write_text(json.dumps({
        "plan_version": TRANSACTION_DETAIL_PLAN_VERSION,
        "source": APPROVED_DETAIL_SOURCE,
        "account_scope": APPROVED_DETAIL_ACCOUNT_SCOPE,
        "importer_version": APPROVED_DETAIL_IMPORTER_VERSION,
        "expected_record_count": 3, "source_sha256": digest,
    }), encoding="utf-8")

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 0, result.stderr
    assert "Appended 3 events" in result.stdout

    assert_no_parallel_snapshot_database(root)
    assert pension_capture_count(root) == 1
    connection = sqlite3.connect(root / "stores" / "trade_ledger.sqlite3")
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()


def test_the_snapshot_migration_keeps_an_explicit_source(tmp_path):
    """The snapshot migration is pointed at an exact file, never a root."""
    text = SNAPSHOT_TOOL.read_text(encoding="utf-8")
    assert '"--source"' in text
    assert '"--root"' not in text

    root = production_shaped_root(tmp_path)
    # Already generation 2 in this fixture, so the tool must refuse rather than
    # silently touch anything.
    path = holdings_snapshot_store_path(root)
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "pt01" / "snap.bak"),
        "--manifest", str(tmp_path / "pt01" / "snap.manifest"),
        "--dry-run")
    assert result.returncode == 1
    assert "source_not_generation_one" in result.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert_no_parallel_snapshot_database(root)
