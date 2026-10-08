"""Failure-injection and privacy tests for holdings snapshot SQLite storage."""

import dataclasses
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import time

import pytest

from tokmo.records.holdings_persistence import authorize_user_request
from tokmo.records.holdings_persistence import (
    MIGRATION_CRITERIA_VERSION,
    MIGRATION_PROGRAM_TOKEN,
    HoldingsPersistencePermitV1,
    PurposeKind,
)
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
    snapshot_content_hash,
    canonical_snapshot_bytes,
    legacy_snapshot_id,
)
from tokmo.records.private_sqlite import (
    acquire_immediate_transaction,
    assert_private_sidecars_while_open,
    connect_private,
    prepare_private_path,
    sqlite_locked,
)
from tokmo.values import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)

NOW = datetime(2026, 7, 28, tzinfo=timezone.utc)
UUID4_HEX = "16fd27068baf433b82eb8c7fada847da"


def value(snapshot_id=UUID4_HEX):
    result = CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source="toss-holdings-v1",
        captured_at=NOW,
        discovered_account_count=2,
        accounts=(
            HoldingsSnapshotAccount(1, (TossHoldingFact(
                "AAPL", "10", "150.5", "averagePurchasePrice",
                "USD", "US", "market", "Apple Inc.", "STOCK"),)),
            HoldingsSnapshotAccount(2, ()),
        ),
        content_hash="0" * 64,
    )
    return dataclasses.replace(
        result, content_hash=snapshot_content_hash(result))


def store_with_row(tmp_path):
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    store = HoldingsSnapshotStore(path, clock=lambda: NOW)
    current = value()
    permit = authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW)
    assert store.append(current, permit).status == "SAVED"
    return path, store, current


def test_schema_mismatch_foreign_well_formed(tmp_path):
    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE foreign_table(value TEXT) STRICT")
    connection.close()
    os.chmod(path, 0o600)

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        HoldingsSnapshotStore(path, clock=lambda: NOW).read_latest()
    assert raised.value.reason == "schema_mismatch"


def test_semantic_tamper_is_store_corrupt(tmp_path):
    path, store, current = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        "UPDATE holdings_snapshot_items SET quantity='11'")
    connection.commit()
    connection.close()

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(current.snapshot_id)
    assert raised.value.reason == "store_corrupt"


@pytest.mark.parametrize("column", ["authorized_at", "stored_at"])
def test_each_persisted_timestamp_tamper_is_store_corrupt(tmp_path, column):
    path, store, current = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(
        f"UPDATE holdings_snapshots SET {column}='2026-07-28T00:00:00+00:00'")
    connection.commit()
    connection.close()

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(current.snapshot_id)
    assert raised.value.reason == "store_corrupt"


def test_analyze_extra_internal_table_is_schema_mismatch(tmp_path):
    path, store, _ = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("ANALYZE")
    connection.close()

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_latest()
    assert raised.value.reason == "schema_mismatch"


def test_table_xinfo_manifest_detects_introspection_only_mutation(
    tmp_path, monkeypatch,
):
    import tokmo.records.holdings_snapshots as module

    _, store, _ = store_with_row(tmp_path)
    real_connect = module.connect_private

    class MutatedXinfo:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql == "PRAGMA table_xinfo(holdings_snapshot_accounts)":
                rows = self.connection.execute(sql, parameters).fetchall()
                rows[1] = (*rows[1][:-1], 2)
                return iter(rows)
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: MutatedXinfo(real_connect(path, **kwargs)))
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_latest()
    assert raised.value.reason == "schema_mismatch"


@pytest.mark.parametrize("statement", [
    "UPDATE holdings_snapshots SET content_hash='" + "1" * 64 + "'",
    "DELETE FROM holdings_snapshot_accounts WHERE account_ordinal=2",
    "DELETE FROM holdings_snapshot_items",
])
def test_tampered_relational_content_is_store_corrupt(tmp_path, statement):
    path, store, current = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(statement)
    connection.commit()
    connection.close()

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(current.snapshot_id)
    assert raised.value.reason == "store_corrupt"


def test_foreign_key_check_failure_is_store_corrupt(tmp_path):
    path, store, current = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute(
        "INSERT INTO holdings_snapshot_accounts VALUES (?, 3)",
        ("f" * 64,))
    connection.commit()
    connection.close()

    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(current.snapshot_id)
    assert raised.value.reason == "store_corrupt"


def test_malformed_db_is_store_corrupt(tmp_path):
    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    path.write_bytes(b"not a sqlite database")
    os.chmod(path, 0o600)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        HoldingsSnapshotStore(path, clock=lambda: NOW).read_latest()
    assert raised.value.reason == "store_corrupt"


def test_read_leaves_no_journal_or_mtime_change(tmp_path):
    path, store, current = store_with_row(tmp_path)
    before = path.stat().st_mtime_ns
    assert store.read_snapshot(current.snapshot_id) == current
    after = path.stat().st_mtime_ns
    assert after == before
    assert not any(Path(f"{path}{suffix}").exists()
                   for suffix in ("-journal", "-wal", "-shm"))


def test_unsafe_path_symlink_rejected(tmp_path):
    path, _, current = store_with_row(tmp_path)
    other = tmp_path / "other.sqlite3"
    path.rename(other)
    path.symlink_to(other)
    permit = authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW)
    receipt = HoldingsSnapshotStore(
        path, clock=lambda: NOW).append(current, permit)
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"


def test_dangling_symlink_and_unsafe_modes_rejected(tmp_path):
    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    path.symlink_to(parent / "missing.sqlite3")
    current = value()
    permit = authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW)
    receipt = HoldingsSnapshotStore(path, clock=lambda: NOW).append(
        current, permit)
    assert receipt.reason == "store_write_failed"
    path.unlink()
    path.write_bytes(b"")
    os.chmod(path, 0o644)
    receipt = HoldingsSnapshotStore(path, clock=lambda: NOW).append(
        current, permit)
    assert receipt.reason == "store_write_failed"


def test_read_dangling_symlink_is_store_corrupt(tmp_path):
    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    path.symlink_to(parent / "missing.sqlite3")
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        HoldingsSnapshotStore(path, clock=lambda: NOW).read_latest()
    assert raised.value.reason == "store_corrupt"


def test_dir_fd_relative_open_works(tmp_path, monkeypatch):
    path, _, _ = store_with_row(tmp_path)
    real_open = os.open
    calls = []

    def recording_open(candidate, flags, mode=0o777, *, dir_fd=None):
        calls.append((candidate, flags, dir_fd))
        return real_open(candidate, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr("tokmo.records.private_sqlite.os.open", recording_open)
    assert not prepare_private_path(path).unsafe
    assert any(candidate == path.name and dir_fd is not None
               and flags & os.O_NOFOLLOW for candidate, flags, dir_fd in calls)

def test_fifo_and_unsafe_parent_mode_rejected(tmp_path):
    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    os.mkfifo(path, mode=0o600)
    current = value()
    permit = authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW)
    assert HoldingsSnapshotStore(path, clock=lambda: NOW).append(
        current, permit).reason == "store_write_failed"
    path.unlink()
    os.chmod(parent, 0o755)
    assert HoldingsSnapshotStore(path, clock=lambda: NOW).append(
        current, permit).reason == "store_write_failed"


def test_old_sqlite_version_rejected(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    current = value()
    monkeypatch.setattr(module.sqlite3, "sqlite_version_info", (3, 36, 0))
    receipt = HoldingsSnapshotStore(
        tmp_path / "toss" / "holdings_snapshots.sqlite3",
        clock=lambda: NOW,
    ).append(current, authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"


def test_invalid_runtime_uuid_and_fabricated_pairing_rejected(tmp_path):
    current = value(snapshot_id="16fd27068baf433b12eb8c7fada847da")
    fabricated = HoldingsPersistencePermitV1(
        criteria_version="holdings-persistence-v1",
        snapshot_id=current.snapshot_id,
        purpose_kind=PurposeKind.APPROVED_MIGRATION,
        program_name=MIGRATION_PROGRAM_TOKEN,
        authorized_at=NOW,
    )
    receipt = HoldingsSnapshotStore(
        tmp_path / "toss" / "holdings_snapshots.sqlite3",
        clock=lambda: NOW,
    ).append(current, fabricated)
    assert receipt.reason == "invalid_permit"


def test_canonical_utc_different_tzinfo_same_bytes():
    first = value()
    second = dataclasses.replace(
        first, captured_at=first.captured_at.astimezone(
            timezone(timedelta(hours=9))))
    assert canonical_snapshot_bytes(first) == canonical_snapshot_bytes(second)


def test_closed_redaction_surfaces_and_account_repr(tmp_path):
    current = value()
    receipt = HoldingsSnapshotStore(
        tmp_path / "toss" / "holdings_snapshots.sqlite3",
        clock=lambda: NOW,
    ).append(current, authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW))
    rendered = repr(receipt)
    assert current.snapshot_id not in rendered
    assert current.content_hash not in rendered
    assert "AAPL" not in rendered
    account_rendered = repr(current.accounts[0])
    assert "account_ordinal" not in account_rendered
    assert "holdings" not in account_rendered
    assert "AAPL" not in repr(current)


def test_read_uses_mode_ro(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    _, store, current = store_with_row(tmp_path)
    real_connect = module.connect_private
    modes = []

    def recording_connect(path, **kwargs):
        modes.append(kwargs.get("mode", "rwc"))
        return real_connect(path, **kwargs)

    monkeypatch.setattr(module, "connect_private", recording_connect)
    assert store.read_snapshot(current.snapshot_id) == current
    assert modes == ["ro"]


def test_private_journal_owner_only_and_sidecar_closure(tmp_path):
    path, _, current = store_with_row(tmp_path)
    connection = connect_private(path)
    try:
        acquire_immediate_transaction(connection)
        connection.execute(
            "UPDATE holdings_snapshots SET stored_at=? "
            "WHERE snapshot_id=?",
            ("2026-07-28T00:00:01.000000+00:00", current.snapshot_id))
        sidecars = assert_private_sidecars_while_open(path)
        assert sidecars
        assert all(stat.S_IMODE(item.stat().st_mode) == 0o600
                   for item in sidecars)
        connection.execute("COMMIT")
    finally:
        connection.close()
    assert not any(Path(f"{path}{suffix}").exists()
                   for suffix in ("-journal", "-wal", "-shm"))


def test_legacy_digest_admission_and_read_back(tmp_path):
    provisional = value()
    digest = legacy_snapshot_id(canonical_snapshot_bytes(provisional))
    legacy = dataclasses.replace(provisional, snapshot_id=digest)
    permit = HoldingsPersistencePermitV1(
        criteria_version=MIGRATION_CRITERIA_VERSION,
        snapshot_id=digest,
        purpose_kind=PurposeKind.APPROVED_MIGRATION,
        program_name=MIGRATION_PROGRAM_TOKEN,
        authorized_at=NOW,
    )
    store = HoldingsSnapshotStore(
        tmp_path / "toss" / "holdings_snapshots.sqlite3",
        clock=lambda: NOW)
    assert store.import_legacy(legacy, permit).status == "SAVED"
    assert store.read_snapshot(digest) == legacy


def test_injected_commit_failure_zero_partial(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    path, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    real_connect = module.connect_private

    class FailingCommit:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("injected commit failure")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: FailingCommit(real_connect(path, **kwargs)))
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"

    monkeypatch.setattr(module, "connect_private", real_connect)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"
    assert store.read_snapshot(first.snapshot_id) == first


def test_commit_and_rollback_failure_returns_closed_receipt_zero_partial(
    tmp_path, monkeypatch,
):
    import tokmo.records.holdings_snapshots as module

    _, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    real_connect = module.connect_private

    class DoubleFailure:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql in {"COMMIT", "ROLLBACK"}:
                raise sqlite3.OperationalError(f"injected {sql}")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: DoubleFailure(real_connect(path, **kwargs)))
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"

    monkeypatch.setattr(module, "connect_private", real_connect)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"


def test_fsync_failure_returns_closed_receipt_and_zero_partial(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    current = value()
    monkeypatch.setattr(
        module, "fsync_directory",
        lambda path: (_ for _ in ()).throw(OSError("injected fsync failure")))
    path = tmp_path / "toss" / "holdings_snapshots.sqlite3"
    receipt = HoldingsSnapshotStore(
        path,
        clock=lambda: NOW,
    ).append(current, authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"
    assert not path.exists()


def test_precreated_fsync_failure_retains_caller_owned_artifact(
    tmp_path, monkeypatch,
):
    import tokmo.records.holdings_snapshots as module

    parent = tmp_path / "toss"
    parent.mkdir(mode=0o700)
    path = parent / "holdings_snapshots.sqlite3"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(fd)
    monkeypatch.setattr(
        module, "fsync_directory",
        lambda _path: (_ for _ in ()).throw(OSError("injected")))
    current = value()
    receipt = HoldingsSnapshotStore(
        path, clock=lambda: NOW, initialize_precreated=True,
    ).append(current, authorize_user_request(
        snapshot_id=current.snapshot_id, clock=lambda: NOW))
    assert (receipt.status, receipt.reason) == (
        "FAILED", "store_write_failed")
    assert path.exists()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not Path(f"{path}-journal").exists()


def test_commit_failure_and_close_failure_stay_closed_zero_partial(
    tmp_path, monkeypatch,
):
    import tokmo.records.holdings_snapshots as module

    _, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    real_connect = module.connect_private

    class CommitAndCloseFailure:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql == "COMMIT":
                raise sqlite3.OperationalError("injected commit failure")
            return self.connection.execute(sql, parameters)

        def close(self):
            self.connection.close()
            raise sqlite3.OperationalError("injected close failure")

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: CommitAndCloseFailure(
            real_connect(path, **kwargs)))
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"

    monkeypatch.setattr(module, "connect_private", real_connect)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"


def test_injected_pre_commit_failure_zero_partial(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    path, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    real_connect = module.connect_private

    class FailingInsert:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql.startswith("INSERT INTO holdings_snapshot_items"):
                raise sqlite3.OperationalError("injected pre-commit failure")
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: FailingInsert(real_connect(path, **kwargs)))
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.reason == "store_write_failed"

    monkeypatch.setattr(module, "connect_private", real_connect)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"
    assert store.read_snapshot(first.snapshot_id) == first


def test_path_swap_detected_write_path_has_zero_partial(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    _, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    real_identity = module.path_identity
    calls = 0

    def swapped_after_commit(path):
        nonlocal calls
        calls += 1
        device, inode = real_identity(path)
        return (device, inode + 1) if calls == 2 else (device, inode)

    monkeypatch.setattr(module, "path_identity", swapped_after_commit)
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_write_failed"

    monkeypatch.setattr(module, "path_identity", real_identity)
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"
    assert store.read_snapshot(first.snapshot_id) == first


@pytest.mark.parametrize("mutation,reason", [
    ("CREATE TABLE unexpected(value TEXT) STRICT", "schema_mismatch"),
    ("UPDATE holdings_snapshot_items SET quantity='11'", "store_corrupt"),
])
def test_append_existing_invalid_store_returns_closed_reason(
    tmp_path, mutation, reason,
):
    path, store, first = store_with_row(tmp_path)
    connection = sqlite3.connect(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    receipt = store.append(second, authorize_user_request(
        snapshot_id=second.snapshot_id, clock=lambda: NOW))
    assert receipt.status == "FAILED"
    assert receipt.reason == reason


def test_quick_check_failure_is_store_corrupt(tmp_path, monkeypatch):
    import tokmo.records.holdings_snapshots as module

    _, store, current = store_with_row(tmp_path)
    real_connect = module.connect_private

    class CorruptResult:
        def fetchone(self):
            return ("corrupt",)

    class FailedQuickCheck:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, parameters=()):
            if sql == "PRAGMA quick_check":
                return CorruptResult()
            return self.connection.execute(sql, parameters)

        def __getattr__(self, name):
            return getattr(self.connection, name)

    monkeypatch.setattr(
        module, "connect_private",
        lambda path, **kwargs: FailedQuickCheck(
            real_connect(path, **kwargs)))
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(current.snapshot_id)
    assert raised.value.reason == "store_corrupt"


def test_bounded_database_lock(tmp_path):
    path, store, first = store_with_row(tmp_path)
    second = dataclasses.replace(
        first, snapshot_id="a6e4c9a06b1743188cb2dd8e5a06a3d4")
    locker = sqlite3.connect(path, isolation_level=None)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        receipt = store.append(second, authorize_user_request(
            snapshot_id=second.snapshot_id, clock=lambda: NOW))
    finally:
        locker.execute("ROLLBACK")
        locker.close()
    elapsed = time.monotonic() - started
    assert receipt.status == "FAILED"
    assert receipt.reason == "store_locked"
    assert 0.9 <= elapsed <= 1.2
    with pytest.raises(HoldingsSnapshotReadError) as raised:
        store.read_snapshot(second.snapshot_id)
    assert raised.value.reason == "not_found"
    assert store.read_snapshot(first.snapshot_id) == first


def test_immediate_transaction_acquisition_succeeds_and_restores_timeout(
    tmp_path,
):
    path, _, _ = store_with_row(tmp_path)
    connection = connect_private(path)
    try:
        acquire_immediate_transaction(connection)
        assert connection.in_transaction
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (1000,)
        connection.execute("ROLLBACK")
    finally:
        connection.close()


def test_immediate_transaction_acquisition_times_out_without_transaction_and_restores(
    tmp_path,
):
    path, _, _ = store_with_row(tmp_path)
    locker = sqlite3.connect(path, isolation_level=None)
    connection = connect_private(path)
    locker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(sqlite3.OperationalError) as raised:
            acquire_immediate_transaction(
                connection, timeout_seconds=0.05, poll_interval=0.01)
        elapsed = time.monotonic() - started
        assert sqlite_locked(raised.value)
        assert 0.04 <= elapsed <= 0.15
        assert not connection.in_transaction
        assert connection.execute("PRAGMA busy_timeout").fetchone() == (1000,)
    finally:
        locker.execute("ROLLBACK")
        locker.close()
        connection.close()


def test_begin_success_after_deadline_rolls_back_and_restores_timeout(monkeypatch):
    import tokmo.records.private_sqlite as module

    class Connection:
        in_transaction = False

        def __init__(self):
            self.commands = []

        def execute(self, sql):
            self.commands.append(sql)
            if sql == "BEGIN IMMEDIATE":
                self.in_transaction = True
            elif sql == "ROLLBACK":
                self.in_transaction = False

    times = iter((10.0, 11.1))
    monkeypatch.setattr(module.time, "monotonic", lambda: next(times))
    connection = Connection()
    with pytest.raises(sqlite3.OperationalError):
        acquire_immediate_transaction(connection, timeout_seconds=1.0)
    assert connection.commands == [
        "PRAGMA busy_timeout=0", "BEGIN IMMEDIATE", "ROLLBACK",
        "PRAGMA busy_timeout=1000",
    ]
    assert not connection.in_transaction


def test_lock_classifier_uses_only_sqlite_busy_or_locked_codes():
    message_only = sqlite3.OperationalError("database is locked")
    assert not sqlite_locked(message_only)
    busy = sqlite3.OperationalError("closed")
    busy.sqlite_errorcode = sqlite3.SQLITE_BUSY
    locked = sqlite3.OperationalError("closed")
    locked.sqlite_errorcode = sqlite3.SQLITE_LOCKED
    assert sqlite_locked(busy)
    assert sqlite_locked(locked)


def test_path_swap_detected_pre_post_connect(tmp_path, monkeypatch):
    import tokmo.records.private_sqlite as module

    path, _, _ = store_with_row(tmp_path)
    real_lstat = module.os.lstat
    real_connect = module.sqlite3.connect
    connected = False

    def marked_connect(*args, **kwargs):
        nonlocal connected
        result = real_connect(*args, **kwargs)
        connected = True
        return result

    def swapped_lstat(candidate, *args, **kwargs):
        result = real_lstat(candidate, *args, **kwargs)
        if connected and Path(candidate) == path:
            fields = list(result)
            fields[1] += 1
            return os.stat_result(fields)
        return result

    monkeypatch.setattr(module.sqlite3, "connect", marked_connect)
    monkeypatch.setattr(module.os, "lstat", swapped_lstat)
    with pytest.raises(OSError, match="unsafe_path_swap"):
        connect_private(path)
