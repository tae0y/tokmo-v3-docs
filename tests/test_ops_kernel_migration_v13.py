"""Schema v13: CONDITIONAL_ORDERS in the closed cadence-wire CHECKs.

Conditional-order SDS v0.4 §4.4 registers CONDITIONAL_ORDERS (group
CONDITIONAL_ORDER_HISTORY) in the closed registries; the ops-kernel wire
CHECKs are generated from those registries, so the sealed schema advances
v12 -> v13 through the same locked, copy-based atomic migration as every
prior version. The 300s lane's budget is unchanged: the pinned snapshot
format is frozen, so a conditional wire start in the 300s lane fails
closed on a structural zero group budget.
"""

from datetime import datetime, timezone
import sqlite3

import pytest

from tokmo.records import ops_kernel as module
from tokmo.records.ops_kernel import OpsKernelStore, SCHEMA_VERSION
from tokmo.values.cadence_policy import CADENCE_POLICY_V1
from tokmo.values.ops_kernel import OpsKernelCorruptError, ProducerRole

NOW = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
SLOT = datetime(2026, 8, 18, 1, 0, tzinfo=timezone.utc)
TS = "2026-08-18T00:00:00.000000Z"


def _build_legacy(path, sql):
    import os

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    os.chmod(path, 0o600)


def test_schema_version_is_v15_and_wire_checks_admit_conditional(tmp_path):
    assert SCHEMA_VERSION == 15
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    with store._read() as connection:
        assert connection.execute(
            "PRAGMA user_version").fetchone()[0] == 15
        sql = connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='cadence_wire_starts'"
        ).fetchone()[0]
    assert "'CONDITIONAL_ORDERS'" in sql
    assert "'CONDITIONAL_ORDER_HISTORY'" in sql
    store.close()


def test_v12_requires_migration_and_v15_admits_conditional(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V12)
    connection = sqlite3.connect(path)
    connection.execute(
        "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
        (TS, "a" * 64, '{"pinned":1}', TS))
    connection.execute(
        "INSERT INTO cadence_wire_starts VALUES"
        "('cadence-300s-v1',?, 'TOKEN','TOKEN','token:tick0','SUCCEEDED',?,?,?)",
        (TS, TS, TS, TS))
    connection.commit()
    connection.close()
    before = path.read_bytes()

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before

    fresh = OpsKernelStore(tmp_path / "fresh" / "ops_kernel.sqlite3")
    connection = sqlite3.connect(fresh.path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        # the widened CHECK admits the conditional operation/group pair
        connection.execute(
            "INSERT INTO cadence_ticks VALUES('cadence-300s-v1',?,?,?,?)",
            (TS, "a" * 64, '{"pinned":1}', TS))
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES"
            "('cadence-300s-v1',?, 'CONDITIONAL_ORDER_HISTORY',"
            "'CONDITIONAL_ORDERS','conditional:OPEN:0','PLANNED',?,NULL,NULL)",
            (TS, TS))
        connection.commit()
    finally:
        connection.close()

def test_v12_float_overlap_rows_survive_refusal(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V12)
    ts = "2026-08-16T10:35:00.000000Z"
    connection = sqlite3.connect(path)
    for suffix in ("a" * 24, "b" * 24):
        connection.execute(
            "INSERT INTO hypotheses VALUES(?,?,?,?,?,?,?,?,?,NULL,?,?,?,"
            "NULL,?,?)",
            (f"hypothesis:{suffix}", suffix[0] * 64, "CANDIDATE",
             "s", "EVENT_OCCUR", "t", "[]", "[]", "[]", "NONE",
             "youtube:12man", "YlVhQO-Fkc8", ts, ts))
    connection.execute(
        "INSERT INTO hypothesis_relations VALUES(?,?,?,?,?,?,?,?,?)",
        ("f" * 32, "hypothesis:" + "a" * 24, "hypothesis:" + "b" * 24,
         "RELATED_BUT_DISTINCT", "material_conflict", '["touch"]',
         0.25, "WEEKLY_REVIEW", ts))
    connection.commit()
    connection.close()

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (12,)
        assert connection.execute(
            "SELECT overlap, typeof(overlap) FROM hypothesis_relations"
        ).fetchone() == (0.25, "real")
    finally:
        connection.close()

def test_every_legacy_schema_version_through_v12_requires_migration(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    for version, sql in (
        (4, module._LEGACY_SCHEMA_SQL_V4),
        (6, module._LEGACY_SCHEMA_SQL_V6),
        (7, module._LEGACY_SCHEMA_SQL_V7),
        (8, module._LEGACY_SCHEMA_SQL_V8),
        (9, module._LEGACY_SCHEMA_SQL_V9),
        (10, module._LEGACY_SCHEMA_SQL_V10),
        (11, module._LEGACY_SCHEMA_SQL_V11),
        (12, module._LEGACY_SCHEMA_SQL_V12),
    ):
        path = tmp_path / f"v{version}" / "ops_kernel.sqlite3"
        _build_legacy(path, sql)
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "PRAGMA user_version"
            ).fetchone() == (version,)

def test_legacy_v12_bytes_are_unmutated_by_refusal(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    migrated_path = tmp_path / "migrated" / "ops_kernel.sqlite3"
    _build_legacy(migrated_path, module._LEGACY_SCHEMA_SQL_V12)
    before = migrated_path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(migrated_path)
    assert migrated_path.read_bytes() == before

def test_malformed_v12_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V12)
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX cadence_wire_starts_open")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


def test_300s_lane_conditional_start_fails_closed_on_zero_group_budget(
        tmp_path):
    """The frozen pinned-snapshot format carries no conditional budget, so
    the 300s lane structurally cannot start a CONDITIONAL_ORDERS wire."""
    from tokmo.values.ops_kernel import OpsKernelBudgetExceeded

    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    store.plan_cadence_requests(
        ProducerRole.COLLECTOR, CADENCE_POLICY_V1, scheduled_for_utc=SLOT,
        requests=(("CONDITIONAL_ORDERS", "conditional:OPEN:0"),), now=NOW)
    with pytest.raises(OpsKernelBudgetExceeded):
        store.start_cadence_request(
            ProducerRole.COLLECTOR, CADENCE_POLICY_V1,
            scheduled_for_utc=SLOT, operation="CONDITIONAL_ORDERS",
            native_identity="conditional:OPEN:0", now=NOW)
    store.close()
