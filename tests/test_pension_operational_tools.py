"""Tool-level proofs for the independent-review blockers.

B-01 the real CLI activate path goes through the snapshot-first protocol
B-02 the snapshot migration tool actually migrates, and restores
B-03 the registry migration tool exists, orders both DBs, and restores
B-04 transaction detail runs over the real ledger through a real entrypoint
B-05 a crash after the snapshot commit converges on exactly one snapshot
M-01 only the current row ever carries a holdings payload

Everything runs against synthetic clones under ``tmp_path``. The migration and
import tools are exercised as real subprocesses so the executable topology --
not just the library -- is what is proved.
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

from tokmo.programs.pension_snapshot_adapter import (
    APPROVED_DETAIL_ACCOUNT_SCOPE,
    APPROVED_DETAIL_IMPORTER_VERSION,
    APPROVED_DETAIL_SOURCE,
    TRANSACTION_DETAIL_PLAN_VERSION,
    build_pension_snapshot,
    classify_pension_batch,
    deterministic_snapshot_id,
    pension_snapshot_identity,
    pension_snapshot_port,
    run_transaction_detail_batch,
)
from tokmo.programs.pension_current_update import (
    BATCH_CONFLICT,
    BATCH_DUPLICATE,
    BATCH_NEW,
    OUTCOME_APPENDED,
    OUTCOME_BLOCKED,
    OUTCOME_CONFLICT_REVIEW,
    OUTCOME_DUPLICATE_NOOP,
    batch_identity,
)
from tokmo.records.holdings_snapshots import (
    SCHEMA_GENERATION_TWO,
    HoldingsSnapshotStore,
)
from tokmo.records.holdings_snapshots import holdings_snapshot_store_path
from tokmo.records.pension_revisions import (
    EMPTY_HOLDINGS_PAYLOAD,
    PensionRegistryCorruptError,
    PensionRegistryStore,
)
from tokmo.records.trade_ledger_store import TradeLedgerStore
from tokmo.values.trade_ledger import TradeLedgerEvent

from tests.test_pension_account_status_parser import (  # noqa: E402
    build_status_workbook,
    etf_symbol,
    landing,
)
from tests.test_pension_current_read_model import (  # noqa: E402
    NOW,
    SEVEN_AS_OF,
    TEN_AS_OF,
    clock,
    current_read_model_registry,
    migrated_snapshot_store,
    seven,
)
from tests.test_pension_snapshot_generation_migration import (  # noqa: E402
    generation_one_store,
)
from tests.test_pension_account_status_import_tool import (  # noqa: E402
    TOOL_PATH,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SNAPSHOT_TOOL = REPO_ROOT / "tools" / "migrate_holdings_snapshots_v1_to_v2.py"
REGISTRY_TOOL = (
    REPO_ROOT / "tools" / "migrate_pension_registry_current_model.py")
SOURCE_AS_OF = "2026-08-13"


def run_tool(tool: Path, *args: str) -> subprocess.CompletedProcess:
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True, text=True, env=environment, cwd=str(REPO_ROOT))


def file_state(path: Path) -> tuple:
    info = os.stat(path)
    return (hashlib.sha256(path.read_bytes()).hexdigest(),
            info.st_size, info.st_ino)


# ---------------------------------------------------------------------------
# B-02 — the snapshot migration tool
# ---------------------------------------------------------------------------

def authorization(tmp_path: Path, digest: str) -> Path:
    record = tmp_path / "authorization.txt"
    record.write_text(f"approved-source-sha256={digest}\n", encoding="utf-8")
    return record


def test_snapshot_tool_migrates_a_synthetic_clone(tmp_path):
    _store, path = generation_one_store(tmp_path)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "backup" / "snap.bak"),
        "--manifest", str(tmp_path / "backup" / "snap.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)))
    assert result.returncode == 0, result.stderr
    assert "Generation 2 is installed" in result.stdout
    connection, generation = HoldingsSnapshotStore(
        path, clock=clock)._open_read_generation()
    connection.close()
    assert generation == SCHEMA_GENERATION_TWO


def test_snapshot_tool_dry_run_writes_nothing(tmp_path):
    _store, path = generation_one_store(tmp_path)
    before = file_state(path)
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "backup" / "snap.bak"),
        "--manifest", str(tmp_path / "backup" / "snap.manifest"),
        "--dry-run")
    assert result.returncode == 0, result.stderr
    assert file_state(path) == before
    assert not (tmp_path / "backup").exists()


def test_snapshot_tool_refuses_without_matching_authorization(tmp_path):
    _store, path = generation_one_store(tmp_path)
    before = file_state(path)
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "backup" / "snap.bak"),
        "--manifest", str(tmp_path / "backup" / "snap.manifest"),
        "--authorization-record", str(authorization(tmp_path, "0" * 64)))
    assert result.returncode == 1
    assert file_state(path) == before


def test_snapshot_tool_refuses_a_mutated_schema_before_any_write(tmp_path):
    """M-02: a CHECK-only mutation is caught at inspect, before backup."""
    _store, path = generation_one_store(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE INDEX synthetic_extra ON holdings_snapshots(source)")
        connection.commit()
    finally:
        connection.close()
    before = file_state(path)
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "backup" / "snap.bak"),
        "--manifest", str(tmp_path / "backup" / "snap.manifest"),
        "--authorization-record", str(authorization(tmp_path, before[0])))
    assert result.returncode == 1
    assert "object_inventory_mismatch" in result.stderr
    assert file_state(path) == before
    assert not (tmp_path / "backup").exists()


def test_snapshot_tool_refuses_a_shifted_user_version(tmp_path):
    _store, path = generation_one_store(tmp_path)
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA user_version = 7")
    finally:
        connection.close()
    before = file_state(path)
    result = run_tool(
        SNAPSHOT_TOOL, "--source", str(path),
        "--backup", str(tmp_path / "backup" / "snap.bak"),
        "--manifest", str(tmp_path / "backup" / "snap.manifest"),
        "--authorization-record", str(authorization(tmp_path, before[0])))
    assert result.returncode == 1
    assert "user_version_mismatch" in result.stderr
    assert file_state(path) == before


def test_snapshot_tool_restore_puts_the_backup_back(tmp_path):
    _store, path = generation_one_store(tmp_path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "snap.bak"
    manifest = tmp_path / "backup" / "snap.manifest"
    assert run_tool(
        SNAPSHOT_TOOL, "--source", str(path), "--backup", str(backup),
        "--manifest", str(manifest),
        "--authorization-record", str(authorization(tmp_path, original)),
    ).returncode == 0

    result = run_tool(
        SNAPSHOT_TOOL, "--restore", "--source", str(path),
        "--backup", str(backup), "--manifest", str(manifest))
    assert result.returncode == 3, result.stderr
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original


def test_snapshot_tool_refuses_to_restore_a_tampered_backup(tmp_path):
    _store, path = generation_one_store(tmp_path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "snap.bak"
    manifest = tmp_path / "backup" / "snap.manifest"
    run_tool(SNAPSHOT_TOOL, "--source", str(path), "--backup", str(backup),
             "--manifest", str(manifest),
             "--authorization-record", str(authorization(tmp_path, original)))
    backup.write_bytes(backup.read_bytes() + b"\x00")
    result = run_tool(
        SNAPSHOT_TOOL, "--restore", "--source", str(path),
        "--backup", str(backup), "--manifest", str(manifest))
    assert result.returncode == 4
    assert "backup_does_not_match_manifest" in result.stderr


# ---------------------------------------------------------------------------
# B-03 — the registry migration tool
# ---------------------------------------------------------------------------

def generation_one_registry(root: Path) -> Path:
    path = root / "stores" / "pension_registry.sqlite3"
    store = PensionRegistryStore(path, clock=clock)
    old = seven()
    store.create_candidate(old)
    store.promote_to_pending(old.revision_id)
    store.activate(old.revision_id, approval_reason="synthetic-existing")
    return path


def migrated_snapshot_in_root(root: Path) -> Path:
    """A generation-2 snapshot store living under the operator root."""
    from tests.test_pension_snapshot_generation_migration import (
        migrate_to_generation_two, toss_capture, toss_permit,
    )
    path = holdings_snapshot_store_path(root)
    store = HoldingsSnapshotStore(path, clock=clock)
    capture = toss_capture()
    store.append(capture, toss_permit(capture.snapshot_id))
    migrate_to_generation_two(path, root)
    return path


def test_registry_tool_requires_the_snapshot_store_first(tmp_path):
    """The snapshot store must already admit the pension source, or the
    registry would become unable to install current state at all."""
    from tests.test_pension_snapshot_generation_migration import (
        toss_capture, toss_permit,
    )
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    # A generation-1 snapshot store: present, readable, but not yet widened.
    snapshot_path = holdings_snapshot_store_path(root)
    snapshot_store = HoldingsSnapshotStore(snapshot_path, clock=clock)
    capture = toss_capture()
    snapshot_store.append(capture, toss_permit(capture.snapshot_id))

    before = file_state(registry_path)
    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, before[0])))
    assert result.returncode == 1
    assert "migrate the snapshot store first" in result.stderr
    assert file_state(registry_path) == before
    assert not (tmp_path / "backup").exists()


def test_registry_tool_refuses_when_the_snapshot_store_is_absent(tmp_path):
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    before = file_state(registry_path)
    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, before[0])))
    assert result.returncode == 1
    assert "snapshot store unreadable" in result.stderr
    assert file_state(registry_path) == before


def test_registry_tool_migrates_and_preserves_the_current_seven(tmp_path):
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()

    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)))
    assert result.returncode == 0, result.stderr
    assert "current holdings preserved: 7" in result.stdout

    store = PensionRegistryStore(registry_path, clock=clock)
    assert store.read_active_revision().holding_count == 7
    assert store.count_payload_bearing_rows() == 1
    assert store.count_superseded_payload_rows() == 0


def test_registry_tool_dry_run_writes_nothing(tmp_path):
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    before = file_state(registry_path)
    result = run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert file_state(registry_path) == before
    assert not (tmp_path / "backup").exists()


def test_registry_tool_restore_puts_the_backup_back(tmp_path):
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    original = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "reg.bak"
    manifest = tmp_path / "backup" / "reg.manifest"
    assert run_tool(
        REGISTRY_TOOL, "--root", str(root), "--backup", str(backup),
        "--manifest", str(manifest),
        "--authorization-record", str(authorization(tmp_path, original)),
    ).returncode == 0

    result = run_tool(
        REGISTRY_TOOL, "--restore", "--root", str(root),
        "--backup", str(backup), "--manifest", str(manifest))
    assert result.returncode == 3, result.stderr
    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == original
    assert PensionRegistryStore(
        registry_path, clock=clock).schema_generation() == 1


def test_neither_tool_has_a_production_path_default():
    for tool in (SNAPSHOT_TOOL, REGISTRY_TOOL):
        text = tool.read_text(encoding="utf-8")
        assert ".local/share" not in text
        assert "default=Path(" not in text


# ---------------------------------------------------------------------------
# B-01 — the CLI activate path
# ---------------------------------------------------------------------------

def plan_file(tmp_path: Path) -> Path:
    from tokmo.values.pension_account_status import (
        CLASSIFICATION_PLAN_VERSION,
        DISPOSITION_EXCLUDED_NON_ETF,
        DISPOSITION_VERIFIED_KRX_ETF,
        EXCLUSION_CASH_LIKE,
        EXCLUSION_CORPORATE_BOND,
        EXCLUSION_MUTUAL_FUND,
    )
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
    path = tmp_path / "plan.json"
    path.write_text(json.dumps({
        "plan_version": CLASSIFICATION_PLAN_VERSION,
        "expected_row_count": 14, "expected_etf_count": 10,
        "expected_excluded_count": 4, "rows": rows}), encoding="utf-8")
    return path


def prepared_root(tmp_path) -> tuple[Path, Path, Path, str]:
    """A migrated root holding the current 7, with a stored ETF-10 candidate."""
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    assert run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)),
    ).returncode == 0

    workbook = landing(tmp_path, build_status_workbook(), "status.xlsx")
    plan = plan_file(tmp_path)
    created = run_tool(
        TOOL_PATH, "candidate", "--file", str(workbook), "--plan", str(plan),
        "--root", str(root), "--source-as-of", SOURCE_AS_OF)
    assert created.returncode == 0, created.stderr
    revision_id = next(
        line.split(":", 1)[1].strip()
        for line in created.stdout.splitlines()
        if line.strip().startswith("revision_id:"))
    return root, workbook, plan, revision_id


def test_cli_activate_runs_the_snapshot_first_order(tmp_path):
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF)
    assert result.returncode == 0, result.stderr
    assert ("order:                 capture_current -> capture_candidate "
            "-> replace_current") in result.stdout

    store = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert store.read_active_revision().holding_count == 10
    assert store.count_payload_bearing_rows() == 1
    assert store.count_superseded_payload_rows() == 0

    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    captures = snapshots.list_pension_capture_headers()
    assert len(captures) == 2


def test_cli_activate_blocked_by_snapshot_failure_changes_nothing(tmp_path):
    """A snapshot store that cannot be written leaves the registry untouched:
    same bytes, same rows, same pointer, same activation events."""
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    registry_path = root / "stores" / "pension_registry.sqlite3"
    snapshot_path = holdings_snapshot_store_path(root)

    def registry_state():
        connection = sqlite3.connect(registry_path)
        try:
            return (
                connection.execute(
                    "SELECT revision_id, state, holdings_json "
                    "FROM pension_revisions ORDER BY revision_id").fetchall(),
                connection.execute(
                    "SELECT * FROM pension_active_pointer").fetchall(),
                connection.execute(
                    "SELECT count(*) FROM pension_activation_events"
                ).fetchone()[0],
            )
        finally:
            connection.close()

    before_bytes = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    before_state = registry_state()

    # Corrupt the snapshot store so every capture fails.
    snapshot_path.write_bytes(b"not a database at all")

    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF)
    assert result.returncode == 1
    assert "activation blocked" in result.stderr
    assert "unchanged" in result.stderr

    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == before_bytes
    assert registry_state() == before_state
    store = PensionRegistryStore(registry_path, clock=clock)
    assert store.read_active_revision().holding_count == 7


def test_cli_activate_refuses_a_mismatched_source(tmp_path):
    root, _workbook, plan, revision_id = prepared_root(tmp_path)
    other = landing(tmp_path, build_status_workbook(
        rows=[["SYNTH-OTHER-01", 1, 100, 100, 0, 0, 1]]), "other.xlsx")
    other_plan = tmp_path / "other_plan.json"
    payload = json.loads(plan.read_text())
    payload["expected_row_count"] = 1
    payload["expected_etf_count"] = 1
    payload["expected_excluded_count"] = 0
    payload["rows"] = [payload["rows"][0]]
    other_plan.write_text(json.dumps(payload), encoding="utf-8")

    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(other), "--plan", str(other_plan),
        "--source-as-of", SOURCE_AS_OF)
    assert result.returncode == 1
    store = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert store.read_active_revision().holding_count == 7


def test_the_cli_has_no_direct_activate_bypass():
    text = TOOL_PATH.read_text(encoding="utf-8")
    assert "store.activate(" not in text
    assert "replace_current_holdings" in text


def test_the_helper_has_a_real_non_test_caller():
    """B-01: the protocol must be reachable from production code."""
    callers = []
    for path in sorted((REPO_ROOT / "src").rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        if "replace_current_holdings" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    for path in sorted((REPO_ROOT / "tools").rglob("*.py")):
        if "replace_current_holdings" in path.read_text(encoding="utf-8"):
            callers.append(path.name)
    assert "import_pension_account_status.py" in callers


# ---------------------------------------------------------------------------
# M-01 — exactly one payload at every committed and reopened state
# ---------------------------------------------------------------------------

def test_candidate_creation_does_not_add_a_second_payload(tmp_path):
    root, _workbook, _plan, revision_id = prepared_root(tmp_path)
    store = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert store.count_payload_bearing_rows() == 1
    assert store.read_revision_state(revision_id) == "CANDIDATE_VALID"
    connection = sqlite3.connect(root / "stores" / "pension_registry.sqlite3")
    try:
        payload = connection.execute(
            "SELECT holdings_json FROM pension_revisions WHERE revision_id=?",
            (revision_id,)).fetchone()[0]
    finally:
        connection.close()
    assert payload == EMPTY_HOLDINGS_PAYLOAD


def test_reopening_after_activation_still_shows_one_payload(tmp_path):
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    run_tool(TOOL_PATH, "activate", "--root", str(root),
             "--revision-id", revision_id, "--approval-reason", "synthetic",
             "--file", str(workbook), "--plan", str(plan),
             "--source-as-of", SOURCE_AS_OF)
    reopened = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert reopened.count_payload_bearing_rows() == 1
    assert reopened.read_active_revision().holding_count == 10


def test_bare_activate_is_refused_on_the_current_read_model(tmp_path):
    """A candidate carries no payload on the current read model, so a bare
    activation could only install an empty current state. It is refused."""
    from tests.test_pension_current_read_model import ten
    registry = current_read_model_registry(tmp_path)
    candidate = ten()
    registry.create_candidate(candidate)
    with pytest.raises(PensionRegistryCorruptError) as error:
        registry.activate(candidate.revision_id, approval_reason="synthetic")
    assert error.value.reason == "activation_requires_verified_snapshot"
    assert registry.read_active_revision().holding_count == 7
    assert registry.count_payload_bearing_rows() == 1


# ---------------------------------------------------------------------------
# B-04 / B-05 — real transaction detail over the real ledger
# ---------------------------------------------------------------------------

LEDGER_SOURCE = "retirement_excel_v1"
LEDGER_SCOPE = "retirement_pension_1"


def ledger_events(count: int, *, start: int = 1, price: str = "1000"):
    return tuple(
        TradeLedgerEvent(
            source=LEDGER_SOURCE, account_scope=LEDGER_SCOPE,
            source_record_key=f"row-{n:06d}", event_kind="EXECUTION",
            supersedes_source_record_key=None, trade_date=date(2026, 7, 1),
            transaction_type_raw="synthetic-buy",
            instrument_raw=f"SYNTH-LEGACY-{n:02d}", quantity="10",
            unit_price=price, amount="10000", medium_raw="SYNTH",
            currency_raw="KRW", settlement_date=None, fee=None, tax=None)
        for n in range(start, start + count))


def ledger_store(tmp_path: Path):
    import itertools
    counter = itertools.count(1)
    return TradeLedgerStore(
        tmp_path / "stores" / "trade_ledger.sqlite3", clock=clock,
        id_factory=lambda: f"{next(counter):032x}")


def batch_environment(tmp_path):
    registry = current_read_model_registry(tmp_path)
    snapshots = migrated_snapshot_store(tmp_path)
    (tmp_path / "stores").mkdir(exist_ok=True)
    return registry, snapshots, ledger_store(tmp_path), (
        tmp_path / "stores" / "trade_ledger.sqlite3")


def admit(registry, snapshots, ledger, ledger_path, events, trace=None,
          file_sha256="f" * 64):
    return run_transaction_detail_batch(
        registry=registry, snapshot_store=snapshots, ledger_store=ledger,
        ledger_path=ledger_path, events=events, file_sha256=file_sha256,
        importer_version="synthetic-importer-v1", source=LEDGER_SOURCE,
        account_scope=LEDGER_SCOPE, clock=clock, trace=trace)


def test_a_new_batch_pre_snapshots_then_appends_to_the_real_ledger(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path,
                    ledger_events(3), trace)
    assert receipt.outcome == OUTCOME_APPENDED
    assert receipt.appended_count == 3
    assert trace == ["classify", "reserve_link", "pre_snapshot",
                     "mark_captured", "ledger_append"]
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()
    assert registry.read_batch_pre_snapshot(
        batch_identity(ledger_events(3), file_sha256="f" * 64)
    )["link_state"] == "APPENDED"


def test_an_exact_duplicate_batch_is_a_total_no_op(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    snapshots_before = len(snapshots.list_pension_capture_headers())

    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path,
                    ledger_events(3), trace)
    assert receipt.outcome == OUTCOME_DUPLICATE_NOOP
    # RI-02: the link is READ (to see whether an earlier attempt left it
    # unfinalized) but nothing is written and nothing is captured.
    assert trace == ["classify", "read_link"]
    assert len(snapshots.list_pension_capture_headers()) == snapshots_before
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()


def test_an_overlapping_batch_is_a_conflict_with_no_write(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    before = len(snapshots.list_pension_capture_headers())

    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path,
                    ledger_events(5), trace)   # rows 1-3 already present
    assert receipt.outcome == OUTCOME_CONFLICT_REVIEW
    assert trace == ["classify"]
    assert len(snapshots.list_pension_capture_headers()) == before
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()


def test_a_same_key_different_content_batch_is_a_conflict(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    receipt = admit(registry, snapshots, ledger, ledger_path,
                    ledger_events(3, price="9999"))
    assert receipt.outcome == OUTCOME_CONFLICT_REVIEW


def test_classification_reads_the_ledger_without_writing(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    before = file_state(ledger_path)
    assert classify_pension_batch(
        ledger_path, ledger_events(3), source=LEDGER_SOURCE,
        account_scope=LEDGER_SCOPE) == BATCH_DUPLICATE
    assert classify_pension_batch(
        ledger_path, ledger_events(2, start=90), source=LEDGER_SOURCE,
        account_scope=LEDGER_SCOPE) == BATCH_NEW
    assert file_state(ledger_path) == before


def test_transaction_detail_never_mutates_holdings(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    before = registry.read_active_revision()
    admit(registry, snapshots, ledger, ledger_path, ledger_events(4))
    after = registry.read_active_revision()
    assert after.revision_id == before.revision_id
    assert after.holdings == before.holdings
    assert after.source_as_of == before.source_as_of == SEVEN_AS_OF
    assert registry.count_payload_bearing_rows() == 1


def test_snapshot_failure_blocks_the_ledger_append(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    # Break the snapshot store after the registry is ready.
    (tmp_path / "toss" / "holdings_snapshots.sqlite3").write_bytes(b"broken")
    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path,
                    ledger_events(3), trace)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "ledger_append" not in trace
    assert not ledger_path.exists() or sqlite3.connect(
        ledger_path).execute(
        "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 0


# --- B-05: crash after the snapshot commit, before the read-back -----------

def test_deterministic_identity_is_stable_and_uuid4_shaped():
    from tokmo.records.holdings_persistence import is_valid_uuid4_hex
    first = deterministic_snapshot_id(binding="batch-pre:x", content_hash="a" * 64)
    assert first == deterministic_snapshot_id(
        binding="batch-pre:x", content_hash="a" * 64)
    assert first != deterministic_snapshot_id(
        binding="batch-pre:y", content_hash="a" * 64)
    assert first != deterministic_snapshot_id(
        binding="batch-pre:x", content_hash="b" * 64)
    assert is_valid_uuid4_hex(first)


def test_crash_after_snapshot_commit_before_readback_converges(tmp_path):
    """The reservation is durable BEFORE the capture and the snapshot id is
    deterministic, so a crash between the snapshot commit and its read-back
    cannot fork into a second snapshot."""
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    current = registry.read_active_revision()

    # Simulate the crashed attempt: reservation written, snapshot appended,
    # process died before the read-back and before the ledger append.
    reserved_id = pension_snapshot_identity(
        current, binding=f"batch-pre:{identity}")
    registry.reserve_batch_pre_snapshot(
        identity, reserved_id, current.semantic_digest, current.source_as_of)
    port = pension_snapshot_port(snapshots, binding=f"batch-pre:{identity}")
    assert port.capture(current).ok
    assert len(snapshots.list_pension_capture_headers()) == 1
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "RESERVED"

    # Reopen and retry.
    reopened_registry = PensionRegistryStore(
        tmp_path / "stores" / "pension_registry.sqlite3", clock=clock)
    receipt = admit(reopened_registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_APPENDED
    assert receipt.pre_snapshot_id == reserved_id
    assert len(snapshots.list_pension_capture_headers()) == 1
    assert reopened_registry.count_batch_pre_snapshots() == 1
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()


def test_repeated_crashes_never_multiply_snapshots(tmp_path):
    registry, snapshots, _ledger, _ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    current = registry.read_active_revision()
    reserved_id = pension_snapshot_identity(
        current, binding=f"batch-pre:{identity}")
    port = pension_snapshot_port(snapshots, binding=f"batch-pre:{identity}")
    for _ in range(5):
        registry.reserve_batch_pre_snapshot(
            identity, reserved_id, current.semantic_digest,
            current.source_as_of)
        assert port.capture(current).ok
    assert len(snapshots.list_pension_capture_headers()) == 1
    assert registry.count_batch_pre_snapshots() == 1


def test_a_reservation_conflict_fails_closed(tmp_path):
    registry, _snapshots, _ledger, _path = batch_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        "a" * 64, "b" * 32, "c" * 64, SEVEN_AS_OF)
    # A differing snapshot id, digest OR reservation source_as_of all conflict.
    for snapshot_id, digest, as_of in (
        ("d" * 32, "c" * 64, SEVEN_AS_OF),
        ("b" * 32, "e" * 64, SEVEN_AS_OF),
        ("b" * 32, "c" * 64, TEN_AS_OF),
    ):
        with pytest.raises(PensionRegistryCorruptError) as error:
            registry.reserve_batch_pre_snapshot(
                "a" * 64, snapshot_id, digest, as_of)
        assert error.value.reason == "batch_pre_snapshot_conflict"


def test_link_metadata_holds_no_holdings_content(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(pension_batch_pre_snapshots)")}
        rows = connection.execute(
            "SELECT * FROM pension_batch_pre_snapshots").fetchall()
    finally:
        connection.close()
    assert columns == {
        "batch_identity", "snapshot_id", "holdings_semantic_digest",
        "reservation_source_as_of",
        "link_state", "reserved_at", "captured_at", "appended_at"}
    rendered = repr(rows)
    for banned in ("80000", "SYNTH", "/", "quantity", "avg_price"):
        assert banned not in rendered


# ---------------------------------------------------------------------------
# raw-landing lifecycle, preserved from the earlier import-tool coverage
# ---------------------------------------------------------------------------

def test_activate_with_the_flag_removes_the_landing_after_verification(tmp_path):
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF, "--delete-landing", str(workbook))
    assert result.returncode == 0, result.stderr
    assert "read-back, hash equality" in result.stdout
    assert not workbook.exists()
    store = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert store.read_active_revision().holding_count == 10


def test_activate_without_the_flag_keeps_the_landing(tmp_path):
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF)
    assert result.returncode == 0, result.stderr
    assert "left in place" in result.stdout
    assert workbook.exists()


def test_a_mismatched_landing_file_is_never_removed(tmp_path):
    root, workbook, plan, revision_id = prepared_root(tmp_path)
    other = landing(tmp_path, build_status_workbook(
        rows=[["SYNTH-OTHER-01", 1, 100, 100, 0, 0, 1]]), "other.xlsx")
    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", revision_id, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF, "--delete-landing", str(other))
    assert result.returncode == 1
    assert "nothing removed" in result.stderr
    assert other.exists()
    assert workbook.exists()


def test_activating_an_unknown_revision_removes_nothing(tmp_path):
    root, workbook, plan, _revision_id = prepared_root(tmp_path)
    result = run_tool(
        TOOL_PATH, "activate", "--root", str(root),
        "--revision-id", "f" * 32, "--approval-reason", "synthetic",
        "--file", str(workbook), "--plan", str(plan),
        "--source-as-of", SOURCE_AS_OF, "--delete-landing", str(workbook))
    assert result.returncode == 1
    assert workbook.exists()
    store = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert store.read_active_revision().holding_count == 7


# ---------------------------------------------------------------------------
# RI-01 — the deterministic id binds the full captured semantics
# ---------------------------------------------------------------------------

def test_identity_changes_when_source_as_of_changes(tmp_path):
    from dataclasses import replace as _replace
    from tests.test_pension_current_read_model import TEN_AS_OF
    current = seven()
    later = _replace(current, source_as_of=TEN_AS_OF)
    assert current.semantic_digest == later.semantic_digest   # holdings equal
    assert pension_snapshot_identity(current, binding="b") != \
        pension_snapshot_identity(later, binding="b")


def test_identity_changes_when_any_holdings_field_changes(tmp_path):
    from dataclasses import replace as _replace
    from tokmo.values.current_holdings import HoldingItemV1
    from tokmo.values.pension_holdings import pension_semantic_digest
    base = seven()

    def variant(**changes):
        items = list(base.holdings)
        items[0] = _replace(items[0], **changes)
        frozen = tuple(items)
        return _replace(base, holdings=frozen, holding_count=len(frozen),
                        semantic_digest=pension_semantic_digest(frozen))

    baseline = pension_snapshot_identity(base, binding="b")
    for changes in ({"quantity": "101"}, {"avg_price": "1001"},
                    {"symbol": "800099"}):
        assert pension_snapshot_identity(
            variant(**changes), binding="b") != baseline


def test_identity_is_stable_under_normalization_equivalent_content(tmp_path):
    """Canonical-equal decimals are the same capture, so the same id."""
    from dataclasses import replace as _replace
    from tokmo.values.pension_holdings import pension_semantic_digest
    base = seven()
    items = list(base.holdings)
    items[0] = _replace(items[0], quantity="100.000", avg_price="1000.0")
    frozen = tuple(items)
    equivalent = _replace(base, holdings=frozen,
                          semantic_digest=pension_semantic_digest(frozen))
    assert pension_snapshot_identity(equivalent, binding="b") == \
        pension_snapshot_identity(base, binding="b")


def test_identity_matches_the_stores_own_content_hash(tmp_path):
    built = build_pension_snapshot(seven(), binding="b")
    from tokmo.records.holdings_snapshots import snapshot_content_hash
    assert built.content_hash == snapshot_content_hash(built)
    assert built.snapshot_id == deterministic_snapshot_id(
        binding="b", content_hash=built.content_hash)


# ---------------------------------------------------------------------------
# RI-02 — a ledger commit followed by a crash finalizes on retry
# ---------------------------------------------------------------------------

def test_crash_after_ledger_commit_finalizes_the_link_on_retry(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    current = registry.read_active_revision()

    # Reproduce the crashed attempt: reserve, capture, mark CAPTURED, commit
    # the ledger, then die before the link is finalized.
    binding = f"batch-pre:{identity}"
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    assert pension_snapshot_port(snapshots, binding=binding).capture(current).ok
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    assert ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"),
    ).status == "SAVED"
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"

    # Reopen and retry.
    reopened = PensionRegistryStore(
        tmp_path / "stores" / "pension_registry.sqlite3", clock=clock)
    trace: list[str] = []
    receipt = admit(reopened, snapshots, ledger, ledger_path, events, trace)
    assert receipt.outcome == OUTCOME_DUPLICATE_NOOP
    assert trace == ["classify", "read_link", "verify_link", "finalize_link"]
    assert reopened.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"
    assert len(snapshots.list_pension_capture_headers()) == 1
    assert reopened.count_batch_pre_snapshots() == 1
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()

    # Repeating the retry changes nothing further.
    again = admit(reopened, snapshots, ledger, ledger_path, events)
    assert again.outcome == OUTCOME_DUPLICATE_NOOP
    assert reopened.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"
    assert len(snapshots.list_pension_capture_headers()) == 1


def test_a_plain_duplicate_without_a_link_creates_nothing(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path, events, trace)
    assert receipt.outcome == OUTCOME_DUPLICATE_NOOP
    assert trace == ["classify", "read_link"]
    assert receipt.pre_snapshot_id == ""
    assert registry.count_batch_pre_snapshots() == 0
    assert len(snapshots.list_pension_capture_headers()) == 0


def test_a_captured_link_with_an_unreadable_snapshot_stays_captured(tmp_path):
    """Fail closed: an unverifiable binding is left for review, not finalized."""
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    current = registry.read_active_revision()
    binding = f"batch-pre:{identity}"
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    # The link points at a snapshot that was never captured.
    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"


def test_same_keys_different_content_never_finalizes_a_link(tmp_path):
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    admit(registry, snapshots, ledger, ledger_path, events)
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"

    # RI-02 case 3: the altered batch carries the SAME record keys and the same
    # file digest, so it shares the batch identity. It must still be refused,
    # and it must neither finalize nor disturb the existing link.
    altered = ledger_events(3, price="9999")
    assert batch_identity(altered, file_sha256="f" * 64) == identity
    link_before = registry.read_batch_pre_snapshot(identity)
    snapshots_before = len(snapshots.list_pension_capture_headers())

    trace: list[str] = []
    receipt = admit(registry, snapshots, ledger, ledger_path, altered, trace)
    assert receipt.outcome == OUTCOME_CONFLICT_REVIEW
    assert trace == ["classify"]
    assert registry.read_batch_pre_snapshot(identity) == link_before
    assert registry.count_batch_pre_snapshots() == 1
    assert len(snapshots.list_pension_capture_headers()) == snapshots_before
    connection = sqlite3.connect(ledger_path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0] == 3
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# B-02 / B-03 — the install recovery boundary, proved by fault injection
# ---------------------------------------------------------------------------

def run_tool_with_fault(tool: Path, fault: str, *args: str):
    environment = dict(os.environ)
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")
    environment["TOKMO_MIGRATION_FAULT"] = fault
    return subprocess.run(
        [sys.executable, str(tool), *args],
        capture_output=True, text=True, env=environment, cwd=str(REPO_ROOT))


@pytest.mark.parametrize("fault", ["after_replace", "at_fsync", "post_verify"])
def test_snapshot_tool_restores_when_the_install_stage_fails(tmp_path, fault):
    _store, path = generation_one_store(tmp_path)
    original = hashlib.sha256(path.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "snap.bak"
    manifest = tmp_path / "backup" / "snap.manifest"

    result = run_tool_with_fault(
        SNAPSHOT_TOOL, fault, "--source", str(path), "--backup", str(backup),
        "--manifest", str(manifest),
        "--authorization-record", str(authorization(tmp_path, original)))
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "install stage failed" in result.stderr

    # Restored exactly, and still the original generation.
    assert hashlib.sha256(path.read_bytes()).hexdigest() == original
    connection, generation = HoldingsSnapshotStore(
        path, clock=clock)._open_read_generation()
    connection.close()
    assert generation == 1
    # Backup and manifest survive and were not overwritten.
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original
    assert manifest.exists()


@pytest.mark.parametrize("fault", ["after_replace", "at_fsync", "post_verify"])
def test_registry_tool_restores_when_the_install_stage_fails(tmp_path, fault):
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    original = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "reg.bak"
    manifest = tmp_path / "backup" / "reg.manifest"

    result = run_tool_with_fault(
        REGISTRY_TOOL, fault, "--root", str(root), "--backup", str(backup),
        "--manifest", str(manifest),
        "--authorization-record", str(authorization(tmp_path, original)))
    assert result.returncode == 3, (result.stdout, result.stderr)
    assert "install stage failed" in result.stderr

    assert hashlib.sha256(registry_path.read_bytes()).hexdigest() == original
    store = PensionRegistryStore(registry_path, clock=clock)
    assert store.schema_generation() == 1
    assert store.read_active_revision().holding_count == 7
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original
    assert manifest.exists()


@pytest.mark.parametrize(
    "tool_kind", ["snapshot", "registry"])
def test_a_failed_restore_is_distinct_and_preserves_the_backup(
        tmp_path, tool_kind):
    """Restore failure must be its own outcome and must not consume the
    backup or its manifest."""
    if tool_kind == "snapshot":
        _store, target = generation_one_store(tmp_path)
        tool, extra = SNAPSHOT_TOOL, ("--source", str(target))
    else:
        root = tmp_path / "root"
        target = generation_one_registry(root)
        migrated_snapshot_in_root(root)
        tool, extra = REGISTRY_TOOL, ("--root", str(root))
    original = hashlib.sha256(target.read_bytes()).hexdigest()
    backup = tmp_path / "backup" / "b.bak"
    manifest = tmp_path / "backup" / "b.manifest"

    result = run_tool_with_fault(
        tool, "after_replace,restore:replace", *extra, "--backup", str(backup),
        "--manifest", str(manifest),
        "--authorization-record", str(authorization(tmp_path, original)))
    assert result.returncode == 4, (result.stdout, result.stderr)
    assert "UNCERTAIN" in result.stderr
    # The backup and manifest are intact and were never overwritten.
    assert hashlib.sha256(backup.read_bytes()).hexdigest() == original
    assert manifest.exists()


@pytest.mark.parametrize("family,mutation", [
    ("pension_activation_events",
     "UPDATE pension_activation_events SET approval_reason='tampered'"),
    ("pension_active_pointer",
     "UPDATE pension_active_pointer SET activation_generation=99"),
    ("pension_reminder_events",
     "UPDATE pension_reminder_events SET alert_id='tampered'"),
    ("pension_revisions",
     "UPDATE pension_revisions SET approval_reason='tampered'"),
])
def test_registry_verifier_catches_content_mutation_in_each_family(
        tmp_path, family, mutation):
    """B-03: the verifier compares every column of every preserved family."""
    from tokmo.records import pension_registry_migration as migration
    root = tmp_path / "root"
    path = generation_one_registry(root)
    store = PensionRegistryStore(path, clock=clock)
    store.reserve_reminder("c" * 64, "a" * 32, 31, NOW, alert_id="synthetic")

    inventory = migration.inspect_registry(path)
    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    migration.build_current_read_model_sidecar(path, sidecar)

    connection = sqlite3.connect(sidecar)
    try:
        connection.execute(mutation)
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(migration.RegistryMigrationError) as error:
        migration.verify_current_read_model_sidecar(inventory, sidecar)
    assert family in error.value.reason or error.value.reason in (
        "audit_rows_altered", "payload_transformation_incorrect")


def test_registry_verifier_catches_a_wrongly_preserved_payload(tmp_path):
    from tokmo.records import pension_registry_migration as migration
    root = tmp_path / "root"
    path = generation_one_registry(root)
    inventory = migration.inspect_registry(path)
    sidecar = tmp_path / "sidecar" / "pension_registry.sqlite3"
    migration.build_current_read_model_sidecar(path, sidecar)

    connection = sqlite3.connect(sidecar)
    try:
        # Give the current row a payload it did not have in the source.
        connection.execute(
            "UPDATE pension_revisions SET holdings_json='[{\"symbol\":\"9\"}]' "
            "WHERE state='ACTIVE'")
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(migration.RegistryMigrationError) as error:
        migration.verify_current_read_model_sidecar(inventory, sidecar)
    assert error.value.reason == "payload_transformation_incorrect"


# ---------------------------------------------------------------------------
# B-04 — the real transaction-detail operator CLI
# ---------------------------------------------------------------------------

DETAIL_TOOL = REPO_ROOT / "tools" / "import_pension_transaction_detail.py"


def detail_document(count: int, *, kind: str = "TRANSACTION_DETAIL",
                    price: str = "1000", start: int = 1) -> dict:
    return {
        "record_kind": kind,
        "records": [
            {"source_record_key": f"row-{n:06d}", "trade_date": "2026-07-01",
             "transaction_type_raw": "synthetic-buy",
             "instrument_raw": f"SYNTH-LEGACY-{n:02d}", "quantity": "10",
             "unit_price": price, "amount": "10000", "medium_raw": "SYNTH",
             "currency_raw": "KRW"}
            for n in range(start, start + count)
        ],
    }


def write_detail_batch(tmp_path: Path, document: dict, name="batch.json",
                       plan_overrides: dict | None = None):
    path = tmp_path / name
    path.write_text(json.dumps(document), encoding="utf-8")
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    plan = tmp_path / f"{name}.plan.json"
    plan.write_text(json.dumps(dict({
        "plan_version": TRANSACTION_DETAIL_PLAN_VERSION,
        "source": APPROVED_DETAIL_SOURCE,
        "account_scope": APPROVED_DETAIL_ACCOUNT_SCOPE,
        "importer_version": APPROVED_DETAIL_IMPORTER_VERSION,
        "expected_record_count": len(document.get("records", [])) or 1,
        "source_sha256": digest,
    }, **(plan_overrides or {}))), encoding="utf-8")
    return path, plan


def detail_root(tmp_path) -> Path:
    """A migrated root with the current 7, ready for a detail batch."""
    root = tmp_path / "root"
    registry_path = generation_one_registry(root)
    migrated_snapshot_in_root(root)
    digest = hashlib.sha256(registry_path.read_bytes()).hexdigest()
    assert run_tool(
        REGISTRY_TOOL, "--root", str(root),
        "--backup", str(tmp_path / "backup" / "reg.bak"),
        "--manifest", str(tmp_path / "backup" / "reg.manifest"),
        "--authorization-record", str(authorization(tmp_path, digest)),
    ).returncode == 0
    return root


def ledger_rows(root: Path) -> int:
    path = root / "stores" / "trade_ledger.sqlite3"
    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0]
    finally:
        connection.close()


def test_detail_cli_appends_a_new_batch_after_the_pre_snapshot(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 0, result.stderr
    assert ("order:          classify -> reserve_link -> pre_snapshot -> "
            "mark_captured -> ledger_append") in result.stdout
    assert "Appended 3 events" in result.stdout
    assert "holdings:       unchanged (7 items, source_as_of preserved)" \
        in result.stdout
    assert ledger_rows(root) == 3


def test_detail_cli_rejects_aggregate_input_before_any_write(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(
        tmp_path, detail_document(5, kind="AGGREGATE_SUMMARY"))
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    before = len(snapshots.list_pension_capture_headers())

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert "aggregate_input_rejected" in result.stderr
    assert "Nothing was captured, linked or appended" in result.stderr
    assert ledger_rows(root) == 0
    assert len(snapshots.list_pension_capture_headers()) == before
    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert registry.count_batch_pre_snapshots() == 0


def test_detail_cli_rejects_a_row_without_execution_identity(tmp_path):
    root = detail_root(tmp_path)
    document = detail_document(2)
    del document["records"][0]["trade_date"]
    batch, plan = write_detail_batch(tmp_path, document)
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert "detail_row_missing:trade_date" in result.stderr
    assert ledger_rows(root) == 0


def test_detail_cli_duplicate_is_a_no_op(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    assert run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                    "--root", str(root)).returncode == 0
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    before = len(snapshots.list_pension_capture_headers())

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 0, result.stderr
    assert "Exact duplicate" in result.stdout
    assert ledger_rows(root) == 3
    assert len(snapshots.list_pension_capture_headers()) == before


def test_detail_cli_conflict_writes_nothing(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
             "--root", str(root))
    overlapping, overlap_plan = write_detail_batch(
        tmp_path, detail_document(5), name="overlap.json")

    result = run_tool(DETAIL_TOOL, "--file", str(overlapping),
                      "--plan", str(overlap_plan), "--root", str(root))
    assert result.returncode == 1
    assert "conflict" in result.stderr
    assert ledger_rows(root) == 3


def test_detail_cli_blocks_when_the_snapshot_store_is_broken(tmp_path):
    root = detail_root(tmp_path)
    (holdings_snapshot_store_path(root)).write_bytes(b"broken")
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 2
    assert "blocked" in result.stderr
    assert ledger_rows(root) == 0


def test_detail_cli_dry_run_writes_nothing(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root), "--dry-run")
    assert result.returncode == 0, result.stderr
    assert "classification: NEW" in result.stdout
    assert ledger_rows(root) == 0


def test_detail_cli_refuses_a_digest_mismatch(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(tmp_path, detail_document(3))
    batch.write_text(json.dumps(detail_document(3, price="9999")),
                     encoding="utf-8")
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert "source_digest_mismatch" in result.stderr
    assert ledger_rows(root) == 0


def crashed_after_pre_snapshot(root: Path, events) -> str:
    """Leave exactly the durable state a crash after the pre-snapshot leaves:
    a reserved+captured link and one snapshot, and no ledger row."""
    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    identity = batch_identity(
        events, file_sha256=hashlib.sha256(b"unused").hexdigest())
    return identity, registry, snapshots


def test_detail_cli_converges_after_a_crash_between_snapshot_and_ledger(tmp_path):
    """Reopen through the REAL CLI: one snapshot, one complete ledger batch."""
    root = detail_root(tmp_path)
    document = detail_document(3)
    batch, plan = write_detail_batch(tmp_path, document)
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)

    # Reproduce the crashed attempt exactly: reserve, capture, mark CAPTURED,
    # then die before the ledger append.
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256=digest)
    current = registry.read_active_revision()
    binding = f"batch-pre:{identity}"
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    assert pension_snapshot_port(snapshots, binding=binding).capture(current).ok
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    assert len(snapshots.list_pension_capture_headers()) == 1
    assert ledger_rows(root) == 0

    # Now the operator reruns the real CLI.
    second = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert second.returncode == 0, second.stderr
    assert ledger_rows(root) == 3
    assert len(snapshots.list_pension_capture_headers()) == 1

    reopened = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert reopened.count_batch_pre_snapshots() == 1
    assert reopened.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"

    # Repeating changes nothing.
    third = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                     "--root", str(root))
    assert third.returncode == 0
    assert "Exact duplicate" in third.stdout
    assert ledger_rows(root) == 3
    assert len(snapshots.list_pension_capture_headers()) == 1


def test_detail_cli_finalizes_a_link_left_captured_by_a_ledger_crash(tmp_path):
    """RI-02 through the real CLI: the ledger committed, the link did not."""
    root = detail_root(tmp_path)
    document = detail_document(3)
    batch, plan = write_detail_batch(tmp_path, document)
    digest = hashlib.sha256(batch.read_bytes()).hexdigest()

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256=digest)
    current = registry.read_active_revision()
    binding = f"batch-pre:{identity}"
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    pension_snapshot_port(snapshots, binding=binding).capture(current)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")

    # The ledger committed; the process died before finalizing the link.
    ledger = ledger_store_at(root)
    from tokmo.values.trade_ledger import ImportAuditMeta
    assert ledger.append_batch(
        events, import_audit=ImportAuditMeta(digest, "synthetic-importer-v1"),
    ).status == "SAVED"
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 0, result.stderr
    assert "Exact duplicate" in result.stdout
    reopened = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    assert reopened.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"
    assert ledger_rows(root) == 3
    assert len(snapshots.list_pension_capture_headers()) == 1


def ledger_store_at(root: Path):
    import itertools
    counter = itertools.count(1)
    return TradeLedgerStore(
        root / "stores" / "trade_ledger.sqlite3", clock=clock,
        id_factory=lambda: f"{next(counter):032x}")


def test_the_detail_entrypoint_has_no_production_default():
    text = DETAIL_TOOL.read_text(encoding="utf-8")
    assert ".local/share" not in text
    assert "default=Path(" not in text


def test_the_detail_entrypoint_calls_the_real_adapter():
    text = DETAIL_TOOL.read_text(encoding="utf-8")
    assert "run_transaction_detail_batch" in text
    assert "TradeLedgerStore" in text
    # The superseded abstract protocol must be gone from the tree entirely.
    assert "admit_transaction_batch" not in text
    for module in ("src/tokmo/programs/pension_current_update.py",
                   "src/tokmo/programs/pension_snapshot_adapter.py"):
        assert "def admit_transaction_batch" not in (
            REPO_ROOT / module).read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# B-04 — authorization precedes every durable write
# ---------------------------------------------------------------------------

def store_state(root: Path) -> dict:
    """Bytes and row counts of every store the batch path could touch."""
    def snapshot_of(relative: str) -> tuple:
        path = root / "stores" / relative
        if not path.exists():
            return ("absent",)
        return (hashlib.sha256(path.read_bytes()).hexdigest(),
                os.stat(path).st_size)

    registry = PensionRegistryStore(
        root / "stores" / "pension_registry.sqlite3", clock=clock)
    snapshots = HoldingsSnapshotStore(
        holdings_snapshot_store_path(root), clock=clock)
    return {
        "registry_bytes": snapshot_of("pension_registry.sqlite3"),
        "snapshot_bytes": snapshot_of("holdings_snapshots.sqlite3"),
        "ledger_bytes": snapshot_of("trade_ledger.sqlite3"),
        "ledger_rows": ledger_rows(root),
        "links": registry.count_batch_pre_snapshots(),
        "pension_captures": len(snapshots.list_pension_capture_headers()),
        "payload_rows": registry.count_payload_bearing_rows(),
    }


@pytest.mark.parametrize("overrides,expected", [
    ({"source": "some-other-source"}, "unauthorized_source"),
    ({"account_scope": "toss-acct-000000000000000000000000"},
     "unauthorized_account_scope"),
    ({"importer_version": "synthetic-importer-v1"},
     "unauthorized_importer_version"),
    ({"plan_version": "pension-transaction-detail-plan-vNEXT"},
     "unauthorized_plan_version"),
    ({"expected_record_count": 0}, "invalid_expected_record_count"),
    ({"source_sha256": "z" * 64}, "invalid_source_sha256"),
])
def test_detail_cli_refuses_an_unauthorized_plan_before_any_write(
        tmp_path, overrides, expected):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(
        tmp_path, detail_document(3), plan_overrides=overrides)
    before = store_state(root)

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert expected in result.stderr
    assert "Nothing was captured, linked or appended" in result.stderr
    assert store_state(root) == before


def test_detail_cli_refuses_an_unauthorized_event_kind(tmp_path):
    root = detail_root(tmp_path)
    document = detail_document(3)
    document["records"][1]["event_kind"] = "ADJUSTMENT"
    batch, plan = write_detail_batch(tmp_path, document)
    before = store_state(root)

    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert "unauthorized_event_kind" in result.stderr
    assert store_state(root) == before


def test_detail_cli_refuses_aggregate_input_with_stores_invariant(tmp_path):
    root = detail_root(tmp_path)
    batch, plan = write_detail_batch(
        tmp_path, detail_document(5, kind="AGGREGATE_SUMMARY"))
    before = store_state(root)
    result = run_tool(DETAIL_TOOL, "--file", str(batch), "--plan", str(plan),
                      "--root", str(root))
    assert result.returncode == 1
    assert "aggregate_input_rejected" in result.stderr
    assert store_state(root) == before


def test_the_approved_contract_reuses_the_canonical_constants():
    from tokmo.programs.pension_trade_ingestion import (
        PENSION_TRADE_SCOPE, PENSION_TRADE_SOURCE,
    )
    assert APPROVED_DETAIL_SOURCE == PENSION_TRADE_SOURCE
    assert APPROVED_DETAIL_ACCOUNT_SCOPE == PENSION_TRADE_SCOPE
    from tokmo.programs.pension_snapshot_adapter import (
        APPROVED_DETAIL_EVENT_KINDS,
    )
    assert APPROVED_DETAIL_EVENT_KINDS == frozenset({"EXECUTION", "CORRECTION"})


# ---------------------------------------------------------------------------
# RI-02 — exact linked-snapshot verification
# ---------------------------------------------------------------------------

def captured_link_environment(tmp_path):
    """A CAPTURED link whose ledger already holds the exact batch."""
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    current = registry.read_active_revision()
    binding = f"batch-pre:{identity}"
    return registry, snapshots, ledger, ledger_path, events, identity, \
        current, binding


def test_a_link_pointing_at_a_toss_capture_is_refused(tmp_path):
    from tests.test_pension_snapshot_generation_migration import (
        toss_capture, toss_permit,
    )
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    toss = toss_capture(("069500",))
    snapshots.append(toss, toss_permit(toss.snapshot_id))
    registry.reserve_batch_pre_snapshot(
        identity, toss.snapshot_id, current.semantic_digest,
        current.source_as_of)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "wrong_source" in receipt.reason
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"


def test_a_link_with_a_wrong_holdings_digest_is_refused(tmp_path):
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        "d" * 64,                                   # not the real digest
        current.source_as_of)
    assert pension_snapshot_port(snapshots, binding=binding).capture(current).ok
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "holdings_digest_mismatch" in receipt.reason
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"


def test_a_link_with_a_wrong_deterministic_identity_is_refused(tmp_path):
    """A readable pension capture that this batch binding could not produce."""
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    # Capture under a DIFFERENT binding, then point the link at it.
    other = pension_snapshot_port(snapshots, binding="batch-pre:other")
    assert other.capture(current).ok
    foreign_id = pension_snapshot_identity(current, binding="batch-pre:other")
    registry.reserve_batch_pre_snapshot(
        identity, foreign_id, current.semantic_digest, current.source_as_of)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "identity_mismatch" in receipt.reason
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"


def test_a_link_pointing_at_nothing_is_refused(tmp_path):
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "unreadable" in receipt.reason
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"


def test_a_refused_link_leaves_every_store_untouched(tmp_path):
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        identity, "e" * 32, "d" * 64, current.source_as_of)
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    from tokmo.values.trade_ledger import ImportAuditMeta
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))
    rows_before = ledger_rows_at(ledger_path)
    captures_before = len(snapshots.list_pension_capture_headers())

    admit(registry, snapshots, ledger, ledger_path, events)
    assert ledger_rows_at(ledger_path) == rows_before
    assert len(snapshots.list_pension_capture_headers()) == captures_before
    assert registry.count_batch_pre_snapshots() == 1
    assert registry.read_active_revision().holding_count == 7


def ledger_rows_at(path: Path) -> int:
    if not path.exists():
        return 0
    connection = sqlite3.connect(path)
    try:
        return connection.execute(
            "SELECT count(*) FROM trade_ledger_events").fetchone()[0]
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# RI-02 final — recovery is judged against the RESERVATION, never against
# retry-time current, which may legitimately have moved on.
# ---------------------------------------------------------------------------

def test_recovery_survives_a_legitimate_current_replacement(tmp_path):
    """The exact scenario the reviewer named.

    A batch pre-snapshots and commits to the ledger; the process dies before
    the link is finalized; the operator then legitimately replaces current
    holdings with a NEW source_as_of; the batch is retried. The correct
    historical pre-snapshot must still be accepted.
    """
    from dataclasses import replace as _replace
    from tokmo.programs.pension_current_update import replace_current_holdings
    from tokmo.programs.pension_snapshot_adapter import (
        BINDING_CANDIDATE, BINDING_CURRENT,
    )
    from tokmo.programs.pension_current_update import PensionSnapshotPortV1
    from tokmo.values.trade_ledger import ImportAuditMeta
    from tests.test_pension_current_read_model import ten

    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    events = ledger_events(3)
    identity = batch_identity(events, file_sha256="f" * 64)
    original = registry.read_active_revision()
    assert original.source_as_of == SEVEN_AS_OF

    # 1. pre-snapshot + link, ledger commits, crash before APPENDED.
    binding = f"batch-pre:{identity}"
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(original, binding=binding),
        original.semantic_digest, original.source_as_of)
    assert pension_snapshot_port(snapshots, binding=binding).capture(original).ok
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    assert ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"),
    ).status == "SAVED"

    # 2. A legitimate current replacement with a DIFFERENT source_as_of.
    current_port = pension_snapshot_port(snapshots, binding=BINDING_CURRENT)
    candidate_port = pension_snapshot_port(snapshots, binding=BINDING_CANDIDATE)
    new_current = ten()
    assert new_current.source_as_of == TEN_AS_OF != SEVEN_AS_OF

    def capture(revision):
        port = (current_port if revision.revision_id == original.revision_id
                else candidate_port)
        return port.capture(revision)

    receipt = replace_current_holdings(
        registry=registry, snapshot_port=PensionSnapshotPortV1(capture=capture),
        current_revision=original, candidate_revision=new_current,
        approval_reason="synthetic-legitimate-replacement")
    assert receipt.outcome == "REPLACED"
    assert registry.read_active_revision().source_as_of == TEN_AS_OF

    captures_before = len(snapshots.list_pension_capture_headers())
    ledger_before = ledger_rows_at(ledger_path)

    # 3. Reopen and retry the exact batch.
    reopened = PensionRegistryStore(
        tmp_path / "stores" / "pension_registry.sqlite3", clock=clock)
    trace: list[str] = []
    result = admit(reopened, snapshots, ledger, ledger_path, events, trace)

    assert result.outcome == OUTCOME_DUPLICATE_NOOP
    assert trace == ["classify", "read_link", "verify_link", "finalize_link"]
    assert reopened.read_batch_pre_snapshot(identity)["link_state"] == "APPENDED"
    assert reopened.count_batch_pre_snapshots() == 1
    # The batch's own pre-snapshot is still the single one for that batch, and
    # nothing else changed.
    assert len(snapshots.list_pension_capture_headers()) == captures_before
    assert ledger_rows_at(ledger_path) == ledger_before
    # The new current is untouched by the recovery.
    assert reopened.read_active_revision().source_as_of == TEN_AS_OF
    assert reopened.read_active_revision().holding_count == 10
    assert reopened.count_payload_bearing_rows() == 1


def test_a_tampered_reservation_source_as_of_keeps_the_link_captured(tmp_path):
    """Negative: the reservation binding must actually be checked."""
    from tokmo.values.trade_ledger import ImportAuditMeta
    registry, snapshots, ledger, ledger_path, events, identity, current, \
        binding = captured_link_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        identity, pension_snapshot_identity(current, binding=binding),
        current.semantic_digest, current.source_as_of)
    assert pension_snapshot_port(snapshots, binding=binding).capture(current).ok
    registry.advance_batch_pre_snapshot(identity, "CAPTURED")
    ledger.append_batch(
        events, import_audit=ImportAuditMeta("f" * 64, "synthetic-importer-v1"))

    # Tamper only the reservation's source_as_of.
    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        connection.execute(
            "UPDATE pension_batch_pre_snapshots "
            "SET reservation_source_as_of = ? WHERE batch_identity = ?",
            ("2020-01-01T00:00:00.000000+00:00", identity))
        connection.commit()
    finally:
        connection.close()

    captures_before = len(snapshots.list_pension_capture_headers())
    rows_before = ledger_rows_at(ledger_path)

    receipt = admit(registry, snapshots, ledger, ledger_path, events)
    assert receipt.outcome == OUTCOME_BLOCKED
    assert "captured_at_mismatch" in receipt.reason
    assert registry.read_batch_pre_snapshot(identity)["link_state"] == "CAPTURED"
    assert len(snapshots.list_pension_capture_headers()) == captures_before
    assert ledger_rows_at(ledger_path) == rows_before
    assert registry.read_active_revision().holding_count == 7


def test_the_reservation_timestamp_must_be_canonical(tmp_path):
    """The schema itself rejects a non-canonical reservation timestamp."""
    registry, _snapshots, _ledger, _path = batch_environment(tmp_path)
    registry.reserve_batch_pre_snapshot(
        "a" * 64, "b" * 32, "c" * 64, SEVEN_AS_OF)
    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        for bad in ("2026-08-13", "2026-08-13T00:00:00+00:00",
                    "2026-08-13T00:00:00.000000Z"):
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(
                    "UPDATE pension_batch_pre_snapshots "
                    "SET reservation_source_as_of = ?", (bad,))
    finally:
        connection.close()


def test_the_verifier_never_reads_current_state():
    """Structural guard: recovery validity must not depend on retry-time
    current, which is exactly the defect this closes."""
    import inspect
    from tokmo.programs.pension_snapshot_adapter import (
        verify_linked_pre_snapshot,
    )
    source = inspect.getsource(verify_linked_pre_snapshot)
    assert "read_active_revision" not in source
    assert "current_source_as_of" not in source
    assert "reservation_source_as_of" in source


def test_the_reservation_binding_is_privacy_safe(tmp_path):
    """A timestamp is not holdings content: no symbols, quantities or prices."""
    registry, snapshots, ledger, ledger_path = batch_environment(tmp_path)
    admit(registry, snapshots, ledger, ledger_path, ledger_events(3))
    connection = sqlite3.connect(
        tmp_path / "stores" / "pension_registry.sqlite3")
    try:
        rows = connection.execute(
            "SELECT * FROM pension_batch_pre_snapshots").fetchall()
    finally:
        connection.close()
    rendered = repr(rows)
    for banned in ("80000", "SYNTH", "quantity", "avg_price", "/"):
        assert banned not in rendered
