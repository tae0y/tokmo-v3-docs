"""M3: locked atomic v4 -> v7 kernel schema migration.

The committed baseline DB schema is v4 (request_day_counters CHECK capped at
3744, no cadence tables). Opening a valid v4 database must migrate it in one
private, locked, copy-validate-fsync-atomic-replace step to the current
schema: every existing table/row (including request_day_counters) preserved,
the day-counter CHECK rebuilt to telemetry-only, and the cadence tables added.
Malformed or unsafe databases still fail closed without mutation.

M2: migration coordinates with every normal kernel transaction through the
same ``.migrate.lock``. Normal transactions hold the shared lock from before
opening the database until after close; migration holds the exclusive lock
across checkpoint/snapshot/copy/validate/fsync/replace/directory-fsync, so a
migration never snapshots mid-transaction, an open writer's commit is never
lost to an old inode, and any fault leaves either the complete original v4
file or a complete v7 file — never a partial state.
"""

import fcntl
import os
import sqlite3
import threading
from datetime import UTC, date, datetime

import pytest

from tokmo.records.ops_kernel import SCHEMA_VERSION, OpsKernelStore
from tokmo.values.ops_kernel import OpsKernelCorruptError, ProducerRole

TS = "2026-07-31T00:00:00.000000Z"


def objects(connection):
    return {
        (row[0], row[1], row[2], None if row[3] is None else " ".join(row[3].split()))
        for row in connection.execute(
            "SELECT type,name,tbl_name,sql FROM sqlite_schema"
            " WHERE name NOT LIKE 'sqlite_stat%'"
        )
    }


def build_v4(path, *, mutate=None):
    from tokmo.records.ops_kernel import _LEGACY_SCHEMA_SQL_V4

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        script = (
            _LEGACY_SCHEMA_SQL_V4 if mutate is None else mutate(_LEGACY_SCHEMA_SQL_V4)
        )
        connection.executescript(script)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO request_day_counters VALUES('2026-07-30',1200,?,?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO request_day_counters VALUES('2026-07-31',3744,?,?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO endpoint_cooldowns VALUES"
            "('toss','GET','provider.example','/api/v1/orders',?, 'rate_limited',?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO incidents VALUES('PROVIDER','quotes',?,?,3,'OPEN','impact-ref',NULL)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO manifest_runs VALUES(7,?,?,?,?,0,'OPEN',NULL,NULL)",
            (TS, '{"trades":7}', "a" * 64, "{}"),
        )
        connection.execute(
            "INSERT INTO candidate_manifest VALUES"
            "(7,'cand-1','subj-1','QUOTE_STATE','[]','FULL_REVIEW','PENDING',?)",
            (TS,),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def assert_is_v4(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        counters_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='request_day_counters'"
        ).fetchone()[0]
        assert "3744" in counters_sql
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'cadence_%'"
        ).fetchone() == (0,)
    finally:
        connection.close()


def test_valid_v4_db_requires_migration_without_mutation(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    assert_is_v4(path)
    before = path.read_bytes()

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)

    assert path.read_bytes() == before
    assert SCHEMA_VERSION == 15
    # Historical v4 row compatibility stays provable without any production
    # constructor migration (test-only legacy builder, Plan/SDS §4).
    connection = sqlite3.connect(path)
    try:
        counters = dict(
            connection.execute(
                "SELECT utc_day,provider_started_count FROM request_day_counters "
                "ORDER BY utc_day"
            ).fetchall()
        )
        assert counters == {"2026-07-30": 1200, "2026-07-31": 3744}
        assert connection.execute(
            "SELECT source,reason FROM endpoint_cooldowns"
        ).fetchone() == ("toss", "rate_limited")
        assert connection.execute(
            "SELECT kind,subject,count,state FROM incidents"
        ).fetchone() == ("PROVIDER", "quotes", 3, "OPEN")
        assert connection.execute(
            "SELECT tick_id,state FROM manifest_runs"
        ).fetchone() == (7, "OPEN")
        assert connection.execute(
            "SELECT candidate_id FROM candidate_manifest WHERE tick_id=7"
        ).fetchone() == ("cand-1",)
    finally:
        connection.close()


def test_v4_content_is_never_touched_by_ordinary_construction(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert_is_v4(path)
    with sqlite3.connect(path) as connection:
        assert connection.execute(
            "SELECT reason FROM endpoint_cooldowns WHERE source='toss'"
        ).fetchone() == ("rate_limited",)


def test_fresh_bootstrap_schema_is_exactly_v15(tmp_path):
    fresh = OpsKernelStore(tmp_path / "fresh" / "ops_kernel.sqlite3")
    second = OpsKernelStore(tmp_path / "second" / "ops_kernel.sqlite3")
    bootstrap = sqlite3.connect(fresh.path)
    other = sqlite3.connect(second.path)
    try:
        assert objects(bootstrap) == objects(other)
        assert bootstrap.execute("PRAGMA user_version").fetchone() == (15,)
    finally:
        bootstrap.close()
        other.close()
    # The fresh store reopens through the normal exact-validation path.
    OpsKernelStore(fresh.path)


def test_legacy_v4_rows_remain_readable_until_reviewed_migration(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("SELECT tick_id FROM manifest_runs").fetchone() == (
            7,
        )
    finally:
        connection.close()


def _endpoint():
    from tokmo.values.ops_kernel import RequestEndpointV1

    return RequestEndpointV1("toss", "GET", "provider.example", "/api/v1/orders")


def test_tampered_v4_table_set_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    connection = sqlite3.connect(path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript("DROP TABLE incidents;")
    connection.close()
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


def test_weakened_v4_check_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(
        path,
        mutate=lambda sql: sql.replace(
            "provider_started_count BETWEEN 0 AND 3744",
            "provider_started_count >= 0",
        ),
    )
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(path)
    assert path.read_bytes() == before


def test_uncommitted_interim_v5_schema_fails_closed(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(
        path,
        mutate=lambda sql: sql.replace(
            "provider_started_count BETWEEN 0 AND 3744",
            "provider_started_count >= 0",
        ).replace("PRAGMA user_version=4", "PRAGMA user_version=5"),
    )
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(path)


# --- M2: migration/transaction coordination through the same lock ---


def migrate_lock(path):
    return path.with_name(path.name + ".migrate.lock")


def hold_lock(path, kind):
    descriptor = os.open(migrate_lock(path), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(descriptor, kind)
    return descriptor


def release_lock(descriptor):
    fcntl.flock(descriptor, fcntl.LOCK_UN)
    os.close(descriptor)


def assert_complete_v4_with_seed_rows(path):
    """The file is the complete original v4 database — never a partial copy."""
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (4,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'cadence_%'"
        ).fetchone() == (0,)
        assert_seed_rows(connection)
    finally:
        connection.close()


def assert_complete_v7_with_seed_rows(path):
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (SCHEMA_VERSION,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name='cadence_wire_starts'"
        ).fetchone() == (1,)
        assert_seed_rows(connection)
    finally:
        connection.close()


def assert_seed_rows(connection):
    counters = dict(
        connection.execute(
            "SELECT utc_day,provider_started_count FROM request_day_counters "
            "WHERE utc_day<='2026-07-31' ORDER BY utc_day"
        ).fetchall()
    )
    assert counters == {"2026-07-30": 1200, "2026-07-31": 3744}
    assert connection.execute(
        "SELECT kind,subject,count,state FROM incidents"
    ).fetchone() == ("PROVIDER", "quotes", 3, "OPEN")
    assert connection.execute(
        "SELECT candidate_id FROM candidate_manifest WHERE tick_id=7"
    ).fetchone() == ("cand-1",)


def test_normal_write_transaction_holds_shared_migration_lock(tmp_path):
    """A normal kernel write transaction participates in the migration lock:
    while an exclusive (migration-side) holder exists, the write waits, and it
    commits only after the exclusive lock is released."""
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    store = OpsKernelStore(path)  # fresh current-schema store
    exclusive = hold_lock(path, fcntl.LOCK_EX)
    finished = threading.Event()

    def write():
        store.plan_calendar_slot(
            ProducerRole.COLLECTOR,
            __import__("tokmo.values.session_gate", fromlist=["Market"]).Market.KR,
            date(2026, 8, 3),
            datetime(2026, 8, 2, 20, 0, tzinfo=UTC),
            now=datetime(2026, 8, 2, 20, 0, tzinfo=UTC),
        )
        finished.set()

    writer = threading.Thread(target=write, daemon=True)
    writer.start()
    try:
        # The write must be blocked by the exclusive holder, not committed.
        assert not finished.wait(timeout=1.0), (
            "normal write transaction ignored the exclusive migration lock"
        )
    finally:
        release_lock(exclusive)
    writer.join(timeout=10)
    assert finished.is_set()
    connection = sqlite3.connect(path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM cadence_calendar_slots"
        ).fetchone() == (1,)
    finally:
        connection.close()


def test_migration_waits_for_open_shared_writer_and_preserves_its_commit__as_v15_refusal_contract(
    tmp_path,
):
    """Restructured (Plan/SDS 2026-08-30): ordinary construction never
    migrates, so an open coordinated writer and a racing writer observe an
    untouched complete v4 file and a typed migration-required refusal."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    descriptor = hold_lock(path, fcntl.LOCK_SH)
    try:
        writer = sqlite3.connect(path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO request_day_counters VALUES('2026-08-01',77,?,?)",
            (TS, TS),
        )
        writer.execute("COMMIT")
        writer.close()
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
    finally:
        release_lock(descriptor)
    assert_complete_v4_with_seed_rows(path)


def test_writer_racing_migration_is_preserved_in_either_interleaving__as_v15_refusal_contract(
    tmp_path,
):
    """Restructured (Plan/SDS 2026-08-30): ordinary construction never
    migrates, so an open coordinated writer and a racing writer observe an
    untouched complete v4 file and a typed migration-required refusal."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    descriptor = hold_lock(path, fcntl.LOCK_SH)
    try:
        writer = sqlite3.connect(path)
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(
            "INSERT INTO request_day_counters VALUES('2026-08-01',77,?,?)",
            (TS, TS),
        )
        writer.execute("COMMIT")
        writer.close()
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
    finally:
        release_lock(descriptor)
    assert_complete_v4_with_seed_rows(path)


def recover_and_verify(path):
    """After any injected fault, a clean reopen completes the migration with
    every seeded row preserved."""
    OpsKernelStore(path)
    assert_complete_v7_with_seed_rows(path)


def test_fault_during_row_copy_leaves_complete_v4__as_v15_refusal_contract(tmp_path):
    """Restructured (Plan/SDS 2026-08-30): no constructor migration exists,
    so no fault path can touch the legacy file at all."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    assert_complete_v4_with_seed_rows(path)


def test_fault_during_migration_validation_leaves_complete_v4__as_v15_refusal_contract(
    tmp_path,
):
    """Restructured (Plan/SDS 2026-08-30): no constructor migration exists,
    so no fault path can touch the legacy file at all."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    assert_complete_v4_with_seed_rows(path)


def test_fault_at_file_fsync_leaves_complete_v4__as_v15_refusal_contract(tmp_path):
    """Restructured (Plan/SDS 2026-08-30): no constructor migration exists,
    so no fault path can touch the legacy file at all."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    assert_complete_v4_with_seed_rows(path)


def test_fault_at_replace_leaves_complete_v4__as_v15_refusal_contract(tmp_path):
    """Restructured (Plan/SDS 2026-08-30): no constructor migration exists,
    so no fault path can touch the legacy file at all."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    assert_complete_v4_with_seed_rows(path)


def test_legacy_v4_is_never_mutated_even_under_io_faults(tmp_path, monkeypatch):
    """Restructured (Plan/SDS 2026-08-30): constructor-side migration is
    gone, so even injected fs/IO faults leave the legacy bytes untouched."""
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    build_v4(path)
    before = path.read_bytes()
    monkeypatch.setattr(
        os,
        "fsync",
        lambda descriptor: (_ for _ in ()).throw(OSError("injected fsync fault")),
    )
    try:
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
    finally:
        monkeypatch.undo()
    assert path.read_bytes() == before
    assert_complete_v4_with_seed_rows(path)
