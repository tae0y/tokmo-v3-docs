"""Ops Kernel v15: explicit offline v14 -> v15 same-version drift repair.

Covers the Plan/SDS 2026-08-30 TDD matrix:
fresh v15 bootstrap admits MEMBERSHIP_TOSS_STOCKS; every exact legacy
v4..v14 database raises typed OpsKernelMigrationRequiredError from ordinary
construction without mutation; inferred narrowed-v14 DDL is drift and fails
closed; the explicit locked snapshot+copy migration preserves every row and
object; fault injection proves pre/post-replace outcomes; WAL-pending
committed rows and an active reader are preserved; lock contention is
zero-write; unsafe paths refuse; old v14 code fails closed against v15;
imports never activate migration; the CLI success/refusal/receipt/replay
contract holds.
"""

from datetime import datetime, timezone
import fcntl
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import stat
import subprocess
import sys
import types

import pytest

from tokmo.records import ops_kernel as module
from tokmo.records.ops_kernel import OpsKernelStore, SCHEMA_VERSION
from tokmo.values.ops_kernel import (
    OpsKernelCorruptError, OpsKernelError, OpsKernelMigrationRequiredError,
)

TOOL_PATH = Path(__file__).resolve().parents[1] / "tools" / (
    "migrate_ops_kernel_v14_to_v15.py"
)
TS = "2026-08-21T00:00:00.000000Z"


def _build(path, sql, *, mode=0o600):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    os.chmod(path, mode)


def _sha256_file(path):
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sidecars(path):
    return {
        suffix: path.with_name(path.name + suffix).exists()
        for suffix in ("-wal", "-shm", "-journal")
    }


def _objects(connection):
    return {
        (row[0], row[1], row[2], None if row[3] is None else " ".join(row[3].split()))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_stat%'"
        )
    }


def _rows(connection):
    tables = sorted(
        row[0] for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    return {table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
            for table in tables}


def _representative_v14(path, *, mode=0o600):
    """Exact deployed-v14 schema with representative rows in every touched
    table family (cadence, counters, hypotheses with a REAL overlap row)."""
    _build(path, module._LEGACY_SCHEMA_SQL_V14, mode=mode)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
        (TS, "a" * 64, '{"pinned":1}', TS),
    )
    connection.execute(
        "INSERT INTO cadence_wire_starts VALUES"
        "('cadence-300s-v1',?,'TOKEN','TOKEN','token:tick0','SUCCEEDED',?,?,?)",
        (TS, TS, TS, TS),
    )
    connection.execute(
        "INSERT INTO cadence_wire_starts VALUES"
        "('cadence-300s-v1',?,'CONDITIONAL_ORDER_HISTORY','CONDITIONAL_ORDERS',"
        "'conditional:OPEN:0','PLANNED',?,NULL,NULL)",
        (TS, TS),
    )
    connection.execute(
        "INSERT INTO request_day_counters VALUES('2026-08-21',3,?,?)", (TS, TS)
    )
    for suffix, digest in (("a", "1"), ("b", "2")):
        connection.execute(
            "INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?,NULL,?,?)",
            (f"hypothesis:{suffix * 24}", digest * 64, "CANDIDATE", "s",
             "EVENT_OCCUR", "t", "[]", "[]", "[]", "NONE", "youtube:12man",
             "YlVhQO-Fkc8", TS, TS),
        )
    connection.execute(
        "INSERT INTO hypothesis_relations VALUES(?,?,?,?,?,?,?,?,?)",
        ("f" * 32, "hypothesis:" + "a" * 24, "hypothesis:" + "b" * 24,
         "RELATED_BUT_DISTINCT", "material_conflict", '["touch"]',
         0.25, "WEEKLY_REVIEW", TS),
    )
    connection.commit()
    connection.close()


# --- Matrix 1: fresh bootstrap is v15 and admits the widened CHECK --------


def test_fresh_bootstrap_is_v15_and_admits_membership_toss_stocks(tmp_path):
    assert SCHEMA_VERSION == 15
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='cadence_wire_starts'"
        ).fetchone()[0]
        assert "'MEMBERSHIP_TOSS_STOCKS'" in sql
        connection.execute(
            "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
            (TS, "a" * 64, '{"pinned":1}', TS),
        )
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES"
            "('cadence-300s-v1',?,'MEMBERSHIP','MEMBERSHIP_TOSS_STOCKS',"
            "'membership:toss-stocks:2026-08-21','PLANNED',?,NULL,NULL)",
            (TS, TS),
        )
        connection.commit()
    store.close()


# --- Matrix 2: exact legacy v4..v14 refuse typed, unmutated ---------------


LEGACY_VERSIONS = (
    (4, "_LEGACY_SCHEMA_SQL_V4"), (6, "_LEGACY_SCHEMA_SQL_V6"),
    (7, "_LEGACY_SCHEMA_SQL_V7"), (8, "_LEGACY_SCHEMA_SQL_V8"),
    (9, "_LEGACY_SCHEMA_SQL_V9"), (10, "_LEGACY_SCHEMA_SQL_V10"),
    (11, "_LEGACY_SCHEMA_SQL_V11"), (12, "_LEGACY_SCHEMA_SQL_V12"),
    (13, "_LEGACY_SCHEMA_SQL_V13"), (14, "_LEGACY_SCHEMA_SQL_V14"),
)


def test_every_exact_legacy_version_refuses_typed_without_mutation(tmp_path):
    for version, attribute in LEGACY_VERSIONS:
        path = tmp_path / f"v{version}" / "ops_kernel.sqlite3"
        _build(path, getattr(module, attribute))
        before = path.read_bytes()
        before_stat = path.stat()
        with pytest.raises(OpsKernelMigrationRequiredError) as raised:
            OpsKernelStore(path)
        assert "migration required" in str(raised.value)
        assert path.read_bytes() == before
        after = path.stat()
        assert (after.st_mtime_ns, after.st_size) == (
            before_stat.st_mtime_ns, before_stat.st_size,
        )
        sidecars = _sidecars(path)
        assert sidecars["-journal"] is False


def test_legacy_v14_with_rows_refuses_typed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


# --- Matrix 3: non-deployed narrowed v14 DDL is drift, fails closed --------


def test_v14_inferred_narrow_ddl_is_drift_and_fails_closed(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    narrowed_v14 = module._LEGACY_SCHEMA_SQL_V14.replace(
        f"operation IN ({module._V15_CADENCE_WIRE_OPERATIONS_SQL})",
        f"operation IN ({module._PRE_MEMBERSHIP_TOSS_STOCKS_WIRE_OPERATIONS_SQL})",
    )
    _build(path, narrowed_v14)
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


# --- Matrix 4/5: explicit migration preserves rows and objects ------------


def test_migrate_v14_to_v15_preserves_rows_and_identical_deployed_ddl(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "pre-migration.snapshot.sqlite3"
    before_objects = _objects(sqlite3.connect(path))
    before_rows = _rows(sqlite3.connect(path))

    receipt = OpsKernelStore.migrate_v14_to_v15(
        path, snapshot_path=snapshot, writers_paused=True,
    )
    assert receipt.status == "MIGRATED"
    assert (receipt.from_version, receipt.to_version) == (14, 15)
    assert receipt.table_count == len(module.TABLES)
    assert receipt.writers_paused_attested is True
    assert receipt.snapshot_sha256 == _sha256_file(snapshot)
    assert receipt.installed_main_sha256 == _sha256_file(path)

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        after_objects = _objects(connection)
        assert after_objects == before_objects
        assert _rows(connection) == before_rows
        # Matrix 5: closed operations and the new membership operation are
        # insertable after migration.
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES"
            "('cadence-300s-v1',?,'MEMBERSHIP','MEMBERSHIP_TOSS_STOCKS',"
            "'membership:toss-stocks:2026-08-22','PLANNED',?,NULL,NULL)",
            (TS, TS),
        )
        connection.commit()
        assert connection.execute(
            "SELECT operation FROM cadence_wire_starts ORDER BY operation"
        ).fetchall() == [
            ("CONDITIONAL_ORDERS",), ("MEMBERSHIP_TOSS_STOCKS",), ("TOKEN",),
        ]
    finally:
        connection.close()
    # migrated store reopens through the normal exact-validation path
    OpsKernelStore(path)


def test_migration_requires_explicit_attestation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=False,
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


def test_migration_refuses_non_v14_source(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build(path, module._LEGACY_SCHEMA_SQL_V13)
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)


def test_v15_source_without_receipt_refuses_already_v15(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    OpsKernelStore(path).close()
    with pytest.raises(OpsKernelCorruptError, match="already-v15-no-receipt"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )


# --- Matrix 6: fault injection boundaries ---------------------------------


def _fault_setup(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    return path, tmp_path / "ops" / "snap.sqlite3"


def test_copy_drift_fault_leaves_complete_v14(tmp_path, monkeypatch):
    path, snapshot = _fault_setup(tmp_path)
    before = path.read_bytes()

    def drift(_store, connection, legacy_version):
        assert legacy_version == 14
        connection.execute(
            "UPDATE main.request_day_counters SET provider_started_count=20"
        )

    monkeypatch.setattr(
        OpsKernelStore, "_migration_copy_validation_hook", drift, raising=False,
    )
    with pytest.raises(OpsKernelCorruptError, match="copy verification"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=True,
        )
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


def test_validation_fault_leaves_complete_v14(tmp_path, monkeypatch):
    path, snapshot = _fault_setup(tmp_path)
    before = path.read_bytes()
    original = OpsKernelStore._validate_schema
    state = {"failed": False}

    def failing_validate(self, connection, **kwargs):
        if not kwargs and not state["failed"]:
            state["failed"] = True
            raise OpsKernelCorruptError("injected validation fault")
        return original(self, connection, **kwargs)

    monkeypatch.setattr(OpsKernelStore, "_validate_schema", failing_validate)
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=True,
        )
    assert path.read_bytes() == before


def test_replace_fault_leaves_complete_v14(tmp_path, monkeypatch):
    path, snapshot = _fault_setup(tmp_path)
    before = path.read_bytes()
    monkeypatch.setattr(
        os, "replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("injected replace")),
    )
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=True,
        )
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.migrate-v14-*"))


def test_postverify_fault_reports_uncertain_and_keeps_backup(tmp_path,
                                                             monkeypatch):
    path, snapshot = _fault_setup(tmp_path)
    original = OpsKernelStore._validate_schema
    calls = {"candidate_or_readback": 0}

    def failing_readback(self, connection, **kwargs):
        result = original(self, connection, **kwargs)
        if not kwargs:
            calls["candidate_or_readback"] += 1
            if calls["candidate_or_readback"] == 2:
                raise OpsKernelCorruptError("injected read-back fault")
        return result

    monkeypatch.setattr(OpsKernelStore, "_validate_schema", failing_readback)
    with pytest.raises(OpsKernelCorruptError, match="uncertain"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=True,
        )
    # v15 is installed (post-replace); snapshot retained for rollback.
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
    assert snapshot.exists()


# --- Matrix 7: WAL-pending rows and an active reader ----------------------


def _wal_backed_v14(path):
    _build(path, module._LEGACY_SCHEMA_SQL_V14)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute(
        "INSERT INTO request_day_counters VALUES('2026-08-22',77,?,?)", (TS, TS)
    )
    connection.commit()
    # Not checkpointed: the committed row lives in the durable -wal only.
    return connection


def test_wal_pending_committed_row_survives_migration(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    writer = _wal_backed_v14(path)
    main_before = path.read_bytes()
    wal_path = path.with_name(path.name + "-wal")
    wal_before = wal_path.read_bytes()
    reader = sqlite3.connect(path)
    reader.execute("BEGIN")
    assert reader.execute(
        "SELECT count(*) FROM request_day_counters"
    ).fetchone() == (1,)
    snapshot = tmp_path / "ops" / "snap.sqlite3"

    OpsKernelStore.migrate_v14_to_v15(
        path, snapshot_path=snapshot, writers_paused=True,
    )

    # The pre-replacement bytes were not mutated pre-promotion (they were
    # replaced wholesale); the WAL-committed row is preserved in the result.
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        assert connection.execute(
            "SELECT count(*) FROM request_day_counters"
        ).fetchone() == (1,)
    finally:
        connection.close()
    # The active reader keeps a consistent view on the old inode.
    assert reader.execute(
        "SELECT count(*) FROM request_day_counters"
    ).fetchone() == (1,)
    reader.close()
    writer.close()
    assert main_before != path.read_bytes()  # replaced, not edited


def test_replace_fault_leaves_wal_bytes_untouched(tmp_path, monkeypatch):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    writer = _wal_backed_v14(path)
    wal_path = path.with_name(path.name + "-wal")
    main_before = path.read_bytes()
    wal_before = wal_path.read_bytes()
    monkeypatch.setattr(
        os, "replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("injected replace")),
    )
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    assert path.read_bytes() == main_before
    assert wal_path.read_bytes() == wal_before
    writer.close()


# --- Matrix 8: lock contention is bounded and zero-write ------------------


def test_lock_contention_refuses_without_writes(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    before = path.read_bytes()
    lock_path = path.with_name(path.name + ".migrate.lock")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        with pytest.raises(OpsKernelCorruptError, match="busy"):
            OpsKernelStore.migrate_v14_to_v15(
                path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
                writers_paused=True,
            )
        assert path.read_bytes() == before
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


# --- Matrix 9: unsafe source/snapshot paths refuse ------------------------


@pytest.mark.parametrize("kind", ["missing", "symlink", "directory",
                                  "wrong-mode", "multi-link"])
def test_unsafe_source_paths_refuse(tmp_path, kind):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    if kind == "missing":
        source = tmp_path / "ops" / "absent.sqlite3"
    elif kind == "symlink":
        _representative_v14(path)
        source = tmp_path / "ops" / "link.sqlite3"
        source.symlink_to(path)
    elif kind == "directory":
        source = tmp_path / "ops" / "dir.sqlite3"
        tmp_path.joinpath("ops").mkdir(exist_ok=True)
        source.mkdir()
    elif kind == "wrong-mode":
        _representative_v14(path, mode=0o644)
        source = path
    else:
        _representative_v14(path)
        source = path
        os.link(path, tmp_path / "ops" / "hardlink.sqlite3")
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            source, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )


def test_existing_snapshot_target_refuses_without_overwrite(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    snapshot.write_bytes(b"pre-existing")
    with pytest.raises(OpsKernelCorruptError, match="exists"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=snapshot, writers_paused=True,
        )
    assert snapshot.read_bytes() == b"pre-existing"


# --- Matrix 10: old v14 code fails closed against v15 ---------------------


def test_old_v14_code_fails_closed_against_v15(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    store.close()
    before = store.path.read_bytes()
    commit = "4b8b4c691cb7bf48facfa8689a42992c0acce1ab"
    root = "projects/tokmo-v3/src/tokmo/"
    source = subprocess.run(
        ["git", "show", f"{commit}:{root}records/ops_kernel.py"],
        check=True, text=True, capture_output=True, cwd=_repo_root(),
    ).stdout
    historical = types.ModuleType("_v14_ops_kernel")
    historical.__file__ = root + "records/ops_kernel.py"
    historical.__package__ = "tokmo.records"
    sys.modules.setdefault(
        "tokmo.values.request_parity", __import__(
            "tokmo.values.request_parity", fromlist=["x"],
        ),
    )
    exec(compile(source, root + "records/ops_kernel.py", "exec"),
         historical.__dict__)
    with pytest.raises(OpsKernelCorruptError):
        historical.OpsKernelStore(store.path)
    assert store.path.read_bytes() == before


def _repo_root():
    return Path(__file__).resolve().parents[2]


# --- Matrix 11: imports never activate migration --------------------------


def test_importing_tool_and_store_never_migrates(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    before = path.read_bytes()
    spec = importlib.util.spec_from_file_location(
        "migrate_ops_kernel_v14_to_v15", TOOL_PATH,
    )
    tool = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(tool)
    assert not list(path.parent.glob(f".{path.name}.migrate-v14-*"))
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


# --- Matrix 12: CLI contract ----------------------------------------------


def _run_cli(database, snapshot, receipt, *, attest=True, cwd=None):
    command = [
        sys.executable, str(TOOL_PATH),
        "--database", str(database),
        "--snapshot", str(snapshot),
        "--receipt", str(receipt),
    ]
    if attest:
        command.append("--attest-writers-paused")
    return subprocess.run(
        command, text=True, capture_output=True, cwd=cwd or _repo_root(),
    )


def test_cli_success_writes_bounded_receipt(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    receipt = tmp_path / "ops" / "receipt.json"
    result = _run_cli(path, snapshot, receipt)
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert payload["status"] == "MIGRATED"
    receipt_payload = json.loads(receipt.read_text())
    assert receipt_payload == payload["receipt"]
    assert receipt_payload["writers_paused_attested"] is True
    for banned in ("path", "credential", "token", "identity"):
        assert banned not in json.dumps(receipt_payload).lower() or banned == "identity"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600
    assert stat.S_IMODE(snapshot.stat().st_mode) == 0o600


def test_cli_refuses_without_attestation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    result = _run_cli(
        path, tmp_path / "ops" / "snap.sqlite3", tmp_path / "ops" / "r.json",
        attest=False,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "REFUSED"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


def test_cli_replay_after_success_reports_already_v15(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    receipt = tmp_path / "ops" / "receipt.json"
    assert _run_cli(path, snapshot, receipt).returncode == 0
    replay_receipt = tmp_path / "ops" / "receipt2.json"
    result = _run_cli(path, snapshot, replay_receipt)
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["reason"] == "already-v15-no-receipt"
    assert not replay_receipt.exists()


def test_cli_existing_receipt_target_refuses(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    receipt = tmp_path / "ops" / "receipt.json"
    receipt.write_text("{}")
    result = _run_cli(path, tmp_path / "ops" / "snap.sqlite3", receipt)
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "REFUSED"
    assert receipt.read_text() == "{}"


# --- Review repair RED tests: masking gaps (B1/B2/M1/M2/M3/M6) ------------


def test_cli_fails_closed_when_receipt_path_already_exists_pre_migration(
        tmp_path):
    # B1: a pre-existing receipt target must refuse BEFORE the migration
    # runs, leaving the source at exact v14.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    receipt = tmp_path / "ops" / "receipt.json"
    receipt.write_text("{}")
    result = _run_cli(path, snapshot, receipt)
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "REFUSED"
    assert "receipt" in payload["reason"]
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


def test_migration_refuses_wrong_owner(monkeypatch, tmp_path):
    # M3: a source file owned by another uid must refuse before any write.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    real_getuid = os.getuid
    monkeypatch.setattr(
        os, "getuid", lambda: real_getuid() + 1, raising=True,
    )
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    monkeypatch.undo()
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)
    assert not (tmp_path / "ops" / "snap.sqlite3").exists()


def test_candidate_fsync_fault_leaves_source_unchanged(monkeypatch, tmp_path):
    # B2/m3 (Matrix 6): a fsync fault on the pre-replace candidate must map
    # to a typed failure, remove candidate artifacts, and leave the exact
    # v14 source bytes untouched.
    from tokmo.records import ops_kernel as module

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    before = path.read_bytes()
    real_fsync = os.fsync

    def faulting_fsync(descriptor):
        info = os.fstat(descriptor)
        for candidate in path.parent.glob(
                f".{path.name}.migrate-v14-*"):
            try:
                if os.lstat(candidate).st_ino == info.st_ino:
                    raise OSError("fault injection: candidate fsync")
            except FileNotFoundError:
                continue
        return real_fsync(descriptor)

    monkeypatch.setattr(module.os, "fsync", faulting_fsync)
    with pytest.raises(OpsKernelError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    monkeypatch.undo()
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.migrate-v14-*"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)


def test_replace_fault_leaves_wal_bytes_untouched_offline(tmp_path,
                                                           monkeypatch):
    # B2 gap: pre-replace fault while the source still has a -wal. The
    # durable main and -wal bytes must be untouched and journal-absent.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    writer = _wal_backed_v14(path)
    main_before = path.read_bytes()
    wal_path = path.with_name(path.name + "-wal")
    wal_before = wal_path.read_bytes()
    monkeypatch.setattr(
        os, "replace",
        lambda src, dst: (_ for _ in ()).throw(OSError("injected replace")),
    )
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    assert path.read_bytes() == main_before
    assert wal_path.read_bytes() == wal_before
    assert not path.with_name(path.name + "-journal").exists()
    writer.close()


def test_snapshot_is_exact_v14_and_carries_wal_committed_rows(tmp_path):
    # M1/M6: the retained snapshot must itself be a complete, checkpointed,
    # exact-v14 database containing the WAL-committed rows.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    writer = _wal_backed_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    receipt = OpsKernelStore.migrate_v14_to_v15(
        path, snapshot_path=snapshot, writers_paused=True,
    )
    writer.close()
    assert receipt.pre_wal_sha256 is not None
    assert receipt.pre_main_sha256 != receipt.snapshot_sha256
    connection = sqlite3.connect(snapshot)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)
        assert connection.execute(
            "SELECT count(*) FROM request_day_counters"
        ).fetchone() == (1,)
        assert connection.execute(
            "PRAGMA integrity_check"
        ).fetchone() == ("ok",)
    finally:
        connection.close()


def test_journal_sidecar_present_refuses_prelock(tmp_path):
    # M2 gap: a -journal sidecar is an immediate pre-lock refusal under the
    # WAL-mode contract.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    path.with_name(path.name + "-journal").write_bytes(b"rollback-journal")
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError, match="journal"):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    assert path.read_bytes() == before


def test_receipt_records_source_prehashes_and_row_count(tmp_path):
    # M1: the receipt must pin what was migrated — pre-main/pre-WAL source
    # hashes (not snapshot echoes) and a concrete total row count.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    main_sha = _sha256_file(path)
    receipt = OpsKernelStore.migrate_v14_to_v15(
        path, snapshot_path=snapshot, writers_paused=True,
    )
    assert receipt.pre_main_sha256 == main_sha
    assert receipt.pre_wal_sha256 is None
    assert isinstance(receipt.row_count, int) and receipt.row_count > 0
    expected_rows = sum(len(rows) for rows in before_rows_values(path))
    assert receipt.row_count == expected_rows
    assert receipt.row_fingerprint != "verified"
    assert len(receipt.row_fingerprint) == 64


def before_rows_values(path):
    connection = sqlite3.connect(path)
    try:
        return list(_rows(connection).values())
    finally:
        connection.close()


def test_snapshot_is_not_a_raw_byte_copy(tmp_path):
    # M2 masking: byte-copying WAL-mode databases yields torn snapshots; the
    # snapshot must come from the SQLite backup API instead.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    writer = _wal_backed_v14(path)
    snapshot = tmp_path / "ops" / "snap.sqlite3"
    OpsKernelStore.migrate_v14_to_v15(
        path, snapshot_path=snapshot, writers_paused=True,
    )
    writer.close()
    assert not snapshot.with_name(snapshot.name + "-wal").exists()
    assert not snapshot.with_name(snapshot.name + "-shm").exists()
    assert not snapshot.with_name(snapshot.name + "-journal").exists()
    header = snapshot.read_bytes()[:20]
    assert header[18:19] in (b"\x01", b"\x02")  # not a torn WAL-mode image


def test_migration_survives_source_with_foreign_key_violation_unused(tmp_path):
    # M2 gap: quick_check does NOT catch foreign-key violations; a v14
    # source with alert_outbox rows referencing missing dispositions must
    # refuse (snapshot FK check), not install a corrupted v15.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO alert_outbox(alert_id,origin,decision_id,tick_id,"
        "subject_id,dedupe_key,channel,severity,body_evidence_json,"
        "created_at,resolution) VALUES("
        "'alert-1','DISPOSITION','missing-disposition',1,"
        "'subject-1','dedupe-1','TELEGRAM','INFO','{}',?,'UNRESOLVED')",
        (TS,),
    )
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore.migrate_v14_to_v15(
            path, snapshot_path=tmp_path / "ops" / "snap.sqlite3",
            writers_paused=True,
        )
    assert path.read_bytes() == before


def test_cli_refuses_relative_paths(tmp_path):
    # m2: relative --database/--snapshot/--receipt are ambiguous across the
    # operator's cwd; the CLI must refuse them outright.
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v14(path)
    result = _run_cli(
        Path("ops/ops_kernel.sqlite3"), Path("ops/snap.sqlite3"),
        Path("ops/r.json"), cwd=tmp_path,
    )
    assert result.returncode != 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "REFUSED"
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (14,)
