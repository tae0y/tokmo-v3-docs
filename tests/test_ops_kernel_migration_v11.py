"""Schema v11: semantic relationship/review/weekly tables + v10 -> v11 migration.

Vertical 9 — exact migration from every legacy version preserving all rows;
clean-DB integrity/FK checks; new v11 tables start empty (no invented state).
"""

import sqlite3
from datetime import UTC, datetime

import pytest

from tokmo.records import ops_kernel as module
from tokmo.records.ops_kernel import SCHEMA_VERSION, OpsKernelStore
from tokmo.values.ops_kernel import OpsKernelCorruptError, ProducerRole

NOW = datetime(2026, 8, 16, 1, 0, tzinfo=UTC)
TS = "2026-08-01T00:00:00.000000Z"

V11_TABLES = {
    "hypothesis_relations",
    "merge_review_queue",
    "weekly_review_runs",
    "weekly_review_pairs",
    "hypothesis_admission_decisions",
}


def _build_legacy(path, sql):
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(sql)
    connection.commit()
    connection.close()
    import os

    os.chmod(path, 0o600)


def _seed_v10_hypothesis_rows(path):
    """Admit two distinct hypotheses through the real store seam."""
    from tests.test_youtube_semantic import make_candidate

    store = OpsKernelStore(path)
    first, one = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        make_candidate(),
        now=NOW,
    )
    assert first.name == "CREATED"
    second, two = store.upsert_hypothesis(
        ProducerRole.ANALYSIS,
        make_candidate(
            statement="다른 인과 관계 가설",
            trigger_expression="entity:005980:other_causal_signal",
        ),
        now=NOW,
    )
    assert second.name == "CREATED"
    store.close()
    return one, two


def _downgrade_to_v10(path):
    """Take a current store back to the exact committed v10 schema with rows.

    v13 widened the cadence-wire CHECK lists with CONDITIONAL_ORDERS /
    CONDITIONAL_ORDER_HISTORY; a genuine historical v10 database predates
    both, so the downgrade also rebuilds ``cadence_wire_starts`` with the
    pre-conditional CHECK bytes (copy -> drop -> rename, transactional).
    """
    import re as _re

    from tokmo.records.ops_kernel import _LEGACY_SCHEMA_SQL_V10

    legacy_sql = _re.search(
        r"CREATE TABLE cadence_wire_starts\(.*?\) STRICT;",
        _LEGACY_SCHEMA_SQL_V10,
        _re.DOTALL,
    ).group(0)
    connection = sqlite3.connect(path)
    version = connection.execute("PRAGMA user_version").fetchone()[0]
    connection.close()
    assert version == 15
    for table in sorted(V11_TABLES):
        connection = sqlite3.connect(path)
        connection.execute(f"DROP TABLE {table}")
        connection.execute("PRAGMA user_version=10")
        connection.commit()
        connection.close()
    connection = sqlite3.connect(path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE cadence_wire_starts_v10 AS SELECT * FROM cadence_wire_starts"
    )
    connection.execute("DROP TABLE cadence_wire_starts")
    connection.executescript(legacy_sql)
    connection.execute(
        "INSERT INTO cadence_wire_starts SELECT * FROM cadence_wire_starts_v10"
    )
    connection.execute("DROP TABLE cadence_wire_starts_v10")
    connection.execute("DROP INDEX IF EXISTS cadence_wire_starts_open")
    legacy_index = _re.search(
        r"CREATE INDEX cadence_wire_starts_open.*?;",
        _LEGACY_SCHEMA_SQL_V10,
        _re.DOTALL,
    ).group(0)
    connection.executescript(legacy_index)
    connection.close()


def test_current_schema_version_bootstrap_creates_v11_tables(tmp_path):
    assert SCHEMA_VERSION == 15
    store = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    with store._read() as connection:
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        version = connection.execute("PRAGMA user_version").fetchone()[0]
    assert version == 15
    assert V11_TABLES <= names
    # every new table starts empty on a clean bootstrap
    with store._read() as connection:
        for table in sorted(V11_TABLES):
            assert (
                connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
            )
    store.close()


def test_v10_requires_migration_preserving_hypothesis_rows(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    one, two = _seed_v10_hypothesis_rows(path)
    _downgrade_to_v10(path)

    before = path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (10,)
        assert connection.execute("SELECT count(*) FROM hypotheses").fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM hypothesis_sightings"
        ).fetchone() == (2,)
        assert connection.execute(
            "SELECT count(*) FROM hypothesis_index"
        ).fetchone() > (0,)
    finally:
        connection.close()


def test_every_legacy_schema_version_requires_migration(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    for version, sql in (
        (4, module._LEGACY_SCHEMA_SQL_V4),
        (6, module._LEGACY_SCHEMA_SQL_V6),
        (7, module._LEGACY_SCHEMA_SQL_V7),
        (8, module._LEGACY_SCHEMA_SQL_V8),
        (9, module._LEGACY_SCHEMA_SQL_V9),
        (10, module._LEGACY_SCHEMA_SQL_V10),
    ):
        path = tmp_path / f"v{version}" / "ops_kernel.sqlite3"
        _build_legacy(path, sql)
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (version,)


def test_fresh_bootstrap_schemas_are_identical(tmp_path):
    first = OpsKernelStore(tmp_path / "one" / "ops_kernel.sqlite3")
    second = OpsKernelStore(tmp_path / "two" / "ops_kernel.sqlite3")

    def objects(connection):
        return {
            (
                row[0],
                row[1],
                row[2],
                None if row[3] is None else " ".join(row[3].split()),
            )
            for row in connection.execute(
                "SELECT type,name,tbl_name,sql FROM sqlite_schema"
                " WHERE name NOT LIKE 'sqlite_stat%'"
            )
        }

    left = sqlite3.connect(first.path)
    right = sqlite3.connect(second.path)
    try:
        assert objects(left) == objects(right)
    finally:
        left.close()
        right.close()
    OpsKernelStore(first.path)  # reopens through exact validation


def test_legacy_v10_with_rows_stays_clean_under_refusal(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _seed_v10_hypothesis_rows(path)
    _downgrade_to_v10(path)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
    finally:
        connection.close()


def test_malformed_v10_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_legacy(path, module._LEGACY_SCHEMA_SQL_V10)
    connection = sqlite3.connect(path)
    connection.execute("DROP INDEX hypotheses_open")
    connection.commit()
    connection.close()
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


# --- v12: append-only merge-review history widened uniqueness ---------------


def _build_v11_with_review_history(path):
    """A committed-v11 schema with one OPEN review and one REJECTED event,
    exactly as v11 stored them (unique on pair/status/origin)."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(module._LEGACY_SCHEMA_SQL_V11)
    TS2 = "2026-08-14T00:00:00.000000Z"
    TS3 = "2026-08-15T00:00:00.000000Z"
    connection.execute(
        "INSERT INTO hypotheses(hypothesis_id,identity_digest,state,statement,"
        "trigger_kind,trigger_expression,supporting_evidence_json,"
        "counter_evidence_json,invalidation_conditions_json,next_check_at,"
        "portfolio_touch,origin,provenance_first,superseded_by,created_at,"
        "updated_at) VALUES('hypothesis:"
        + "a"
        * 24
        + "','1111111111111111111111111111111111111111111111111111111111111111','CANDIDATE','s1','EVENT_OCCUR','e1','[\"x\"]','[]','[\"y\"]',"
        "NULL,'PERSONAL','youtube:12man','src',NULL,?,?)",
        (TS2, TS2),
    )
    connection.execute(
        "INSERT INTO hypotheses(hypothesis_id,identity_digest,state,statement,"
        "trigger_kind,trigger_expression,supporting_evidence_json,"
        "counter_evidence_json,invalidation_conditions_json,next_check_at,"
        "portfolio_touch,origin,provenance_first,superseded_by,created_at,"
        "updated_at) VALUES('hypothesis:"
        + "b"
        * 24
        + "','2222222222222222222222222222222222222222222222222222222222222222','CANDIDATE','s2','EVENT_OCCUR','e2','[\"x\"]','[]','[\"y\"]',"
        "NULL,'PERSONAL','youtube:12man','src',NULL,?,?)",
        (TS2, TS2),
    )
    connection.execute(
        "INSERT INTO merge_review_queue(review_id,hypothesis_a,hypothesis_b,"
        "status,reason_code,origin,created_at,resolved_at) "
        "VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','hypothesis:"
        + "a" * 24
        + "','hypothesis:"
        + "b" * 24
        + "','OPEN','high_overlap_ambiguous',"
        "'ADMISSION',?,NULL)",
        (TS2,),
    )
    connection.execute(
        "INSERT INTO merge_review_queue(review_id,hypothesis_a,hypothesis_b,"
        "status,reason_code,origin,created_at,resolved_at) "
        "VALUES('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','hypothesis:"
        + "a" * 24
        + "','hypothesis:"
        + "b" * 24
        + "','REJECTED','high_overlap_ambiguous',"
        "'ADMISSION',?,?)",
        (TS2, TS3),
    )
    connection.commit()
    connection.close()
    import os

    os.chmod(path, 0o600)


def test_v11_requires_migration_and_v15_keeps_widened_uniqueness(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_v11_with_review_history(path)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)

    fresh = OpsKernelStore(tmp_path / "fresh" / "ops_kernel.sqlite3")
    connection = sqlite3.connect(fresh.path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        connection.execute(
            "INSERT INTO hypotheses(hypothesis_id,identity_digest,state,statement,"
            "trigger_kind,trigger_expression,supporting_evidence_json,"
            "counter_evidence_json,invalidation_conditions_json,next_check_at,"
            "portfolio_touch,origin,provenance_first,superseded_by,created_at,"
            "updated_at) VALUES('hypothesis:"
            + "a"
            * 24
            + "','1111111111111111111111111111111111111111111111111111111111111111','CANDIDATE','s1','EVENT_OCCUR','e1','[\"x\"]','[]','[\"y\"]',"
            "NULL,'PERSONAL','youtube:12man','src',NULL,?,?)",
            (TS, TS),
        )
        connection.execute(
            "INSERT INTO hypotheses(hypothesis_id,identity_digest,state,statement,"
            "trigger_kind,trigger_expression,supporting_evidence_json,"
            "counter_evidence_json,invalidation_conditions_json,next_check_at,"
            "portfolio_touch,origin,provenance_first,superseded_by,created_at,"
            "updated_at) VALUES('hypothesis:"
            + "b"
            * 24
            + "','2222222222222222222222222222222222222222222222222222222222222222','CANDIDATE','s2','EVENT_OCCUR','e2','[\"x\"]','[]','[\"y\"]',"
            "NULL,'PERSONAL','youtube:12man','src',NULL,?,?)",
            (TS, TS),
        )
        # the widened uniqueness admits two same-status events on one pair
        connection.execute(
            "INSERT INTO merge_review_queue(review_id,hypothesis_a,"
            "hypothesis_b,status,reason_code,origin,created_at,resolved_at) "
            "VALUES('aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','hypothesis:"
            + "a" * 24
            + "','hypothesis:"
            + "b" * 24
            + "','OPEN','high_overlap_ambiguous',"
            "'ADMISSION',?,NULL)",
            (TS,),
        )
        connection.execute(
            "INSERT INTO merge_review_queue(review_id,hypothesis_a,"
            "hypothesis_b,status,reason_code,origin,created_at,resolved_at) "
            "VALUES('bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb','hypothesis:"
            + "a" * 24
            + "','hypothesis:"
            + "b" * 24
            + "','OPEN','high_overlap_ambiguous',"
            "'WEEKLY_REVIEW',?,NULL)",
            (TS,),
        )
        connection.commit()
        assert connection.execute(
            "SELECT count(*) FROM merge_review_queue"
        ).fetchone() == (2,)
    finally:
        connection.close()


def test_legacy_v11_review_history_rows_survive_refusal(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    migrated_path = tmp_path / "migrated" / "ops_kernel.sqlite3"
    _build_v11_with_review_history(migrated_path)
    before = migrated_path.read_bytes()
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(migrated_path)
    assert migrated_path.read_bytes() == before
    connection = sqlite3.connect(migrated_path)
    try:
        rows = connection.execute(
            "SELECT review_id,status FROM merge_review_queue ORDER BY review_id"
        ).fetchall()
        assert sorted(rows) == [
            ("aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "OPEN"),
            ("bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb", "REJECTED"),
        ]
    finally:
        connection.close()


def test_malformed_v11_fails_closed_without_mutation(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.executescript(module._LEGACY_SCHEMA_SQL_V11)
    connection.execute("DROP INDEX merge_review_queue_open")
    connection.commit()
    connection.close()
    import os

    os.chmod(path, 0o600)
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before


def test_every_legacy_version_through_v11_requires_migration(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    for version, sql in (
        (4, module._LEGACY_SCHEMA_SQL_V4),
        (6, module._LEGACY_SCHEMA_SQL_V6),
        (7, module._LEGACY_SCHEMA_SQL_V7),
        (8, module._LEGACY_SCHEMA_SQL_V8),
        (9, module._LEGACY_SCHEMA_SQL_V9),
        (10, module._LEGACY_SCHEMA_SQL_V10),
        (11, module._LEGACY_SCHEMA_SQL_V11),
    ):
        path = tmp_path / f"v{version}" / "ops_kernel.sqlite3"
        _build_legacy(path, sql)
        with pytest.raises(OpsKernelMigrationRequiredError):
            OpsKernelStore(path)
        with sqlite3.connect(path) as conn:
            assert conn.execute("PRAGMA user_version").fetchone() == (version,)
