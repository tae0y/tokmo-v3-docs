"""OWN-1: exact, atomic, row-preserving v8/v9 -> v10 kernel migration."""

from datetime import datetime, timezone
import os
import sqlite3

import pytest

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CADENCE_QUANT_SHADOW_POLICY_V1,
    TradesChildLimitsV1,
)
from tokmo.values.ops_kernel import OpsKernelCorruptError, ProducerRole

SLOT = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 14, 20, 0, 1, tzinfo=timezone.utc)
TS = "2026-08-14T20:00:01.000000Z"


def _build_v8(path, *, mutate=None):
    from tokmo.records.ops_kernel import _LEGACY_SCHEMA_SQL_V8

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    try:
        sql = _LEGACY_SCHEMA_SQL_V8
        connection.executescript(sql if mutate is None else mutate(sql))
        connection.execute(
            "INSERT INTO cadence_ticks VALUES(?,?,?,?,?)",
            ("legacy-v8", "2026-08-14T20:00:00.000000Z", "a" * 64,
             '{"legacy":true}', TS),
        )
        connection.execute(
            "INSERT INTO cadence_wire_starts VALUES(?,?,?,?,?,'SUCCEEDED',?,?,?)",
            ("legacy-v8", "2026-08-14T20:00:00.000000Z", "TOKEN", "TOKEN",
             "token:tick0", TS, TS, TS),
        )
        connection.execute(
            "INSERT INTO request_day_counters VALUES(?,?,?,?)",
            ("2026-08-14", 19, TS, TS),
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def _content(path, tables):
    connection = sqlite3.connect(path)
    try:
        return {
            table: tuple(connection.execute(f'SELECT * FROM "{table}"'))
            for table in sorted(tables)
        }
    finally:
        connection.close()


def test_v8_rejects_membership_wire_and_refuses_migration(tmp_path):
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_v8(path)
    before = path.read_bytes()
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO cadence_wire_starts VALUES(?,?,?,?,?,'PLANNED',?,NULL,NULL)",
                ("legacy-v8", "2026-08-14T20:00:00.000000Z", "MEMBERSHIP",
                 "MEMBERSHIP_SEC_ISSUER",
                 "membership:issuer:2026-08-14:AAPL", TS),
            )
    finally:
        connection.close()

    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    assert path.read_bytes() == before

    # The widened vocabulary stays reachable on a fresh v15 store.
    store = OpsKernelStore(tmp_path / "fresh" / "ops_kernel.sqlite3")
    store.pin_cadence_tick(
        ProducerRole.COLLECTOR, CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=SLOT, now=NOW, child_limits=TradesChildLimitsV1(),
    )
    store.plan_cadence_requests(
        ProducerRole.COLLECTOR, CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=SLOT,
        requests=(("MEMBERSHIP_SEC_ISSUER",
                   "membership:issuer:2026-08-14:AAPL"),),
        now=NOW, child_limits=TradesChildLimitsV1(),
    )
    row = store.start_cadence_request(
        ProducerRole.COLLECTOR, CADENCE_QUANT_SHADOW_POLICY_V1,
        scheduled_for_utc=SLOT, operation="MEMBERSHIP_SEC_ISSUER",
        native_identity="membership:issuer:2026-08-14:AAPL",
        now=NOW, child_limits=TradesChildLimitsV1(),
    )
    assert row["operation_group"] == "MEMBERSHIP"

def test_v8_refusal_preserves_legacy_rows_without_migration(tmp_path):
    from tokmo.records.ops_kernel import _LEGACY_TABLES_V8
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    _build_v8(path)
    before = _content(path, _LEGACY_TABLES_V8)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    after = _content(path, _LEGACY_TABLES_V8)

    assert before == after
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (8,)

def test_v9_legacy_toss_request_table_stays_narrow(tmp_path):
    from tokmo.values.request_parity import REQUEST_OPERATION_SPECS, REQUEST_PATHS

    path = tmp_path / "ops_kernel.sqlite3"
    OpsKernelStore(path)
    assert len(REQUEST_OPERATION_SPECS) == len(REQUEST_PATHS) == 9
    assert not any(value is None for value in REQUEST_PATHS)
    assert "MEMBERSHIP_SEC_ISSUER" not in REQUEST_OPERATION_SPECS
    with sqlite3.connect(path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO planned_requests"
                "(tick_id,job_scope,ordinal,operation,params_digest,market,"
                "local_trade_date,method,host,normalized_path_template,state,"
                "planned_at,planned_invocation_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (1, "DAILY_CANDLES", 0, "MEMBERSHIP_SEC_ISSUER", "b" * 64,
                 None, None, "GET", "openapi.tossinvest.com", "/api/v1/candles",
                 "PLANNED", TS, "c" * 32),
            )


def test_tampered_v8_fails_closed_atomically(tmp_path):
    tampered = tmp_path / "tampered" / "ops_kernel.sqlite3"
    _build_v8(tampered, mutate=lambda sql: sql.replace(
        "('TOKEN','CALENDAR','HOLDINGS','QUOTES','TRADES','DAILY_CANDLES')",
        "('TOKEN','CALENDAR','HOLDINGS','QUOTES','TRADES','DAILY_CANDLES','MEMBERSHIP')",
    ))
    tampered_before = tampered.read_bytes()
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(tampered)
    assert tampered.read_bytes() == tampered_before

    drifted = tmp_path / "drifted" / "ops_kernel.sqlite3"
    _build_v8(drifted)
    with sqlite3.connect(drifted) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (8,)
        assert connection.execute(
            "SELECT provider_started_count FROM request_day_counters"
        ).fetchone() == (19,)
