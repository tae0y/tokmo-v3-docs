"""Ops Kernel v14 same-version drift history + v15 explicit migration."""

import os
import sqlite3
import stat
from datetime import UTC, datetime

import pytest

from tokmo.records import ops_kernel as module
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CADENCE_QUANT_SHADOW_POLICY_V1,
    TradesChildLimitsV1,
)
from tokmo.values.ops_kernel import OpsKernelCorruptError, ProducerRole

TS = "2026-08-21T00:00:00.000000Z"
SLOT = "2026-08-21T00:00:00.000000Z"
NOW = datetime(2026, 8, 21, tzinfo=UTC)


def _build(path, sql):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)


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
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'"
        )
    )
    return {
        table: connection.execute(f'SELECT * FROM "{table}"').fetchall()
        for table in tables
    }


def _representative_v13(path):
    _build(path, module._LEGACY_SCHEMA_SQL_V13)
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
        "INSERT INTO request_day_counters VALUES('2026-08-21',3,?,?)", (TS, TS)
    )
    connection.commit()
    connection.close()


def test_same_version_candidate_schema_is_refused_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    candidate_v13 = module.SCHEMA_SQL.replace(
        f"PRAGMA user_version={module.SCHEMA_VERSION}", "PRAGMA user_version=13"
    )
    _build(path, candidate_v13)
    before = path.read_bytes()

    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(path)

    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)


def test_v13_requires_migration_and_v14_to_v15_changes_only_version(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v13(path)
    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before

    # The approved v14 -> v15 path is the explicit offline command; it
    # preserves every row/object and changes only user_version.
    v14 = tmp_path / "ops14" / "ops_kernel.sqlite3"
    _build(v14, module._LEGACY_SCHEMA_SQL_V14)
    with sqlite3.connect(v14) as connection:
        connection.execute(
            "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
            (TS, "a" * 64, '{"pinned":1}', TS),
        )
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES"
            "('cadence-300s-v1',?,'TOKEN','TOKEN','token:tick0','SUCCEEDED',?,?,?)",
            (TS, TS, TS, TS),
        )
    snapshot = tmp_path / "ops14" / "snap.sqlite3"
    OpsKernelStore.migrate_v14_to_v15(
        v14,
        snapshot_path=snapshot,
        writers_paused=True,
    )
    fresh = OpsKernelStore(tmp_path / "fresh" / "ops_kernel.sqlite3")
    with sqlite3.connect(v14) as migrated, sqlite3.connect(fresh.path) as bootstrap:
        assert migrated.execute("PRAGMA user_version").fetchone() == (15,)
        after_wire_sql = migrated.execute(
            "SELECT sql FROM sqlite_schema WHERE name='cadence_wire_starts'"
        ).fetchone()[0]
        assert "'MEMBERSHIP_TOSS_STOCKS'" in after_wire_sql
        assert _objects(migrated) == _objects(bootstrap)
    fresh.close()


def test_fresh_v15_accepts_membership_toss_stocks(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
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


def test_v13_refusal_is_atomic_fail_closed_and_private(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _representative_v13(path)
    before = path.read_bytes()

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)

    assert path.read_bytes() == before
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert not list(path.parent.glob(f".{path.name}.migrate-v*"))
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (13,)
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT count(*) FROM cadence_wire_starts"
        ).fetchone() == (1,)


def test_pre_quant_closed_operations_remain_accepted_on_v15(tmp_path):
    fresh = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    with sqlite3.connect(fresh.path) as connection:
        connection.execute(
            "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
            (TS, "a" * 64, '{"pinned":1}', TS),
        )
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES"
            "('cadence-300s-v1',?,'CONDITIONAL_ORDER_HISTORY','CONDITIONAL_ORDERS',"
            "'conditional:OPEN:0','PLANNED',?,NULL,NULL)",
            (TS, TS),
        )
        connection.commit()
        assert connection.execute(
            "SELECT operation FROM cadence_wire_starts ORDER BY operation"
        ).fetchall() == [("CONDITIONAL_ORDERS",)]
    store = OpsKernelStore(fresh.path)
    store.plan_cadence_requests(
        ProducerRole.COLLECTOR,
        CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=NOW,
        requests=(("MEMBERSHIP_SEC_ISSUER", "membership:issuer:2026-08-14:AAPL"),),
        now=NOW,
        child_limits=TradesChildLimitsV1(),
    )
    row = store.start_cadence_request(
        ProducerRole.COLLECTOR,
        CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=NOW,
        operation="MEMBERSHIP_SEC_ISSUER",
        native_identity="membership:issuer:2026-08-14:AAPL",
        now=NOW,
        child_limits=TradesChildLimitsV1(),
    )
    assert row["operation_group"] == "MEMBERSHIP"
