import hashlib
import inspect
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from typing import get_type_hints

import pytest

import tokmo.records.ops_kernel as ops_kernel_module
from tokmo.records.ops_kernel import SCHEMA_VERSION, OpsKernelStore
from tokmo.values.ops_kernel import (
    OpsKernelBudgetExceeded,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
    OpsKernelCorruptError,
    OpsKernelRoleError,
    OpsKernelTransitionError,
    ProducerRole,
    ReplayResult,
    RequestEndpointV1,
)
from tokmo.values.ops_tick import tick_id_from_utc
from tokmo.values.request_parity import PlannedRequestInputV1, RequestOperation

NOW = datetime(2026, 7, 31, 0, 0, tzinfo=UTC)
INVOCATION = "1" * 32
DIGEST = "a" * 64


def make_store(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")


def plan_row(**changes):
    row = {
        "ordinal": 0,
        "operation": "PRICE_BATCH",
        "params_digest": DIGEST,
        "method": "GET",
        "host": "openapi.tossinvest.com",
        "normalized_path_template": "/api/v1/prices",
        "account_ordinal": None,
        "market": None,
        "local_trade_date": None,
        "canonical_params": {"market": "KR", "sorted_unique_symbols": ["005930"]},
    }
    row.update(changes)
    if "canonical_params" not in changes:
        if row["operation"] in ("TOKEN", "ACCOUNTS"):
            row["canonical_params"] = {}
        elif row["operation"] == "ACCOUNT_HOLDINGS":
            row["canonical_params"] = {"account_ordinal": row["account_ordinal"]}
    if "params_digest" not in changes:
        row["params_digest"] = params_digest(row["operation"], row["canonical_params"])
    return row


def params_digest(operation, params):
    projection = {"operation": operation, **params}
    parts = []
    for key in sorted(projection):
        key_bytes = key.encode()
        value_bytes = json.dumps(
            projection[key], sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode()
        parts.extend(
            (
                str(len(key_bytes)).encode(),
                b":",
                key_bytes,
                str(len(value_bytes)).encode(),
                b":",
                value_bytes,
            )
        )
    return hashlib.sha256(b"".join(parts)).hexdigest()


def valid_row(**changes):
    row = plan_row()
    row.update(changes)
    row["params_digest"] = params_digest(row["operation"], row["canonical_params"])
    return row


def account_rows():
    return tuple(
        valid_row(
            ordinal=account_ordinal + 1,
            operation="ACCOUNT_HOLDINGS",
            normalized_path_template="/api/v1/holdings",
            account_ordinal=account_ordinal,
            canonical_params={"account_ordinal": account_ordinal},
        )
        for account_ordinal in range(1, 6)
    )


def calendar_row(market="KR", local_trade_date="2026-07-31", **changes):
    operation = f"CALENDAR_DAY_{market}"
    params = {"market": market, "local_trade_date": local_trade_date}
    return valid_row(
        operation=operation,
        normalized_path_template=f"/api/v1/market-calendar/{market}",
        market=market,
        local_trade_date=local_trade_date,
        canonical_params=params,
        **changes,
    )


def test_schema_v9_contains_exact_f1_planned_request_identity(tmp_path):
    store = make_store(tmp_path)
    assert SCHEMA_VERSION == 15

    connection = sqlite3.connect(store.path)
    try:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='planned_requests'"
        ).fetchone()[0]
        index_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='index' AND name='planned_requests_open'"
        ).fetchone()[0]
        columns = {
            row[1]: row
            for row in connection.execute("PRAGMA table_xinfo(planned_requests)")
        }
        strict = connection.execute(
            "SELECT strict FROM pragma_table_list WHERE name='planned_requests'"
        ).fetchone()[0]
        identity = connection.execute(
            "SELECT group_concat(name, ',') FROM ("
            "SELECT ii.name FROM pragma_index_list('planned_requests') il "
            "JOIN pragma_index_info(il.name) ii WHERE il.origin='pk' ORDER BY ii.seqno)"
        ).fetchone()[0]
    finally:
        connection.close()

    assert strict == 1
    assert identity == "tick_id,job_scope,ordinal"
    assert "CHECK" in table_sql
    assert "'DAILY_CANDLES'" in table_sql
    assert (
        "WHERE state IN ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING')"
        in index_sql
    )
    assert columns["state"][3] == 1
    assert columns["planned_at"][3] == 1
    assert columns["planned_invocation_id"][3] == 1
    assert columns["started_invocation_id"][3] == 0


def test_plan_requests_creates_defaults_and_identical_replay_is_duplicate(tmp_path):
    store = make_store(tmp_path)
    rows = (plan_row(),)

    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            rows,
            invocation_id=INVOCATION,
            now=NOW,
        )
        is ReplayResult.CREATED
    )
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            rows,
            invocation_id="2" * 32,
            now=NOW,
        )
        is ReplayResult.DUPLICATE
    )

    projected = plan_row()
    projected.pop("canonical_params")
    assert store.read_planned_requests(42, "QUOTES") == (
        {
            "tick_id": 42,
            "job_scope": "QUOTES",
            **projected,
            "state": "PLANNED",
            "planned_at": NOW,
            "armed_at": None,
            "started_at": None,
            "checkpointed_at": None,
            "applying_at": None,
            "settled_at": None,
            "planned_invocation_id": INVOCATION,
            "started_invocation_id": None,
            "exit": None,
            "checkpoint_digest": None,
        },
    )


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("operation", "ORDERS_PAGE"),
        ("params_digest", "b" * 64),
        ("method", "POST"),
        ("host", "other.invalid"),
        ("normalized_path_template", "/api/v1/orders"),
    ),
)
def test_same_identity_changed_immutable_plan_conflicts_without_mutation(
    tmp_path,
    field,
    replacement,
):
    store = make_store(tmp_path)
    original = (plan_row(),)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        original,
        invocation_id=INVOCATION,
        now=NOW,
    )
    before = store.read_planned_requests(42, "QUOTES")

    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(**{field: replacement}),),
            invocation_id="2" * 32,
            now=NOW,
        )

    assert store.read_planned_requests(42, "QUOTES") == before


def test_plan_requests_is_collector_only_and_requires_utc_aware_now(tmp_path):
    store = make_store(tmp_path)
    for role in (ProducerRole.ANALYSIS, ProducerRole.HERMES, ProducerRole.FEEDBACK):
        with pytest.raises(OpsKernelRoleError):
            store.plan_requests(
                role,
                42,
                "QUOTES",
                (plan_row(),),
                invocation_id=INVOCATION,
                now=NOW,
            )
    assert store.read_planned_requests(42) == ()

    with pytest.raises(ValueError, match="timezone-aware"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(),),
            invocation_id=INVOCATION,
            now=NOW.replace(tzinfo=None),
        )
    assert store.read_planned_requests(42) == ()


# Owner-repair regressions: batch atomicity and typed fail-closed planning.
def test_multi_row_conflict_rolls_back_earlier_insert(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (plan_row(ordinal=1),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(ordinal=0), plan_row(ordinal=1, params_digest="b" * 64)),
            invocation_id="2" * 32,
            now=NOW,
        )
    assert tuple(
        row["ordinal"] for row in store.read_planned_requests(42, "QUOTES")
    ) == (1,)


def test_intra_batch_duplicate_identity_is_typed_conflict(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError, match="duplicate"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(), plan_row()),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_empty_request_batch_fails_closed(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError, match="empty"):
        store.plan_requests(
            ProducerRole.COLLECTOR, 42, "QUOTES", (), invocation_id=INVOCATION, now=NOW
        )


@pytest.mark.parametrize("tick_id", ("42", True, 42.0, -1, 2147483648))
def test_invalid_tick_id_is_typed_and_does_not_mutate(tmp_path, tick_id):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError, match="invalid request plan"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick_id,
            "QUOTES",
            (plan_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_exact_int_tick_id_replay_is_duplicate(tmp_path):
    store = make_store(tmp_path)
    rows = (plan_row(),)
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            2147483647,
            "QUOTES",
            rows,
            invocation_id=INVOCATION,
            now=NOW,
        )
        is ReplayResult.CREATED
    )
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            2147483647,
            "QUOTES",
            rows,
            invocation_id="2" * 32,
            now=NOW,
        )
        is ReplayResult.DUPLICATE
    )


@pytest.mark.parametrize(
    "changes",
    (
        {"operation": "NOPE"},
        {"method": "DELETE"},
        {"host": "other.invalid"},
        {"normalized_path_template": "/api/v1/conditional-orders"},
        {"params_digest": "not-a-digest"},
        {"ordinal": 99},
        {"operation": "ACCOUNT_HOLDINGS", "account_ordinal": None},
    ),
)
def test_invalid_planner_values_raise_typed_planner_error(tmp_path, changes):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(**changes),),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_missing_planner_field_never_leaks_raw_key_error(tmp_path):
    store = make_store(tmp_path)
    row = plan_row()
    del row["operation"]
    with pytest.raises(OpsKernelConflictError, match="missing field"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )


@pytest.mark.parametrize("operation", ([], {}, set()))
def test_unhashable_operation_is_typed_invalid_plan(tmp_path, operation):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError, match="invalid request plan"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (plan_row(operation=operation, params_digest=DIGEST),),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def _seed_day_count(store, count):
    timestamp = "2026-07-31T00:00:00.000000Z"
    connection = sqlite3.connect(store.path)
    try:
        connection.execute(
            "INSERT INTO request_day_counters VALUES(?,?,?,?)",
            ("2026-07-31", count, timestamp, timestamp),
        )
        connection.commit()
    finally:
        connection.close()


def _audit_count(store, method, path):
    connection = sqlite3.connect(store.path)
    try:
        row = connection.execute(
            "SELECT started_count FROM runtime_request_reservations "
            "WHERE runtime_day=? AND source=? AND method=? AND host=? "
            "AND normalized_path_template=?",
            ("2026-07-31", "toss", method, "openapi.tossinvest.com", path),
        ).fetchone()
        return row[0] if row else 0
    finally:
        connection.close()


def test_start_request_admits_above_legacy_3744_day_total_with_monotonic_telemetry(
    tmp_path,
):
    """G-10: the UTC-day total is telemetry, never an admission ceiling."""
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (plan_row(ordinal=0), plan_row(ordinal=1)),
        invocation_id=INVOCATION,
        now=NOW,
    )
    _seed_day_count(store, 5000)

    for ordinal in (0, 1):
        started = store.start_request(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            ordinal,
            invocation_id="2" * 32,
            now=NOW,
        )
        assert started["state"] == "STARTED"
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/prices"
    )
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 5002
    assert _audit_count(store, "GET", "/api/v1/prices") == 2


def test_reserve_request_admits_above_legacy_3744_day_total(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/accounts"
    )
    _seed_day_count(store, 4000)
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, NOW)
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 4001


def test_legacy_and_planned_starts_share_the_same_day_counter(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/accounts"
    )
    _seed_day_count(store, 3742)
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, NOW)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        (plan_row(operation="ACCOUNTS", normalized_path_template="/api/v1/accounts"),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "HOLDINGS", 0, invocation_id="2" * 32, now=NOW
    )
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 3744


def test_token_counts_toward_day_but_not_get_endpoint_audit(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        (
            plan_row(
                operation="TOKEN",
                method="POST",
                normalized_path_template="/oauth2/token",
            ),
        ),
        invocation_id=INVOCATION,
        now=NOW,
    )
    row = store.start_request(
        ProducerRole.COLLECTOR, 42, "HOLDINGS", 0, invocation_id="2" * 32, now=NOW
    )
    assert row["state"] == "STARTED"
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/accounts"
    )
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 1
    assert _audit_count(store, "POST", "/oauth2/token") == 0


def test_get_cooldown_rejects_before_any_start_mutation(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/prices"
    )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (plan_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.record_cooldown(
        ProducerRole.COLLECTOR,
        endpoint,
        NOW + timedelta(minutes=5),
        "rate_limited",
        NOW,
    )

    with pytest.raises(OpsKernelCooldownActive):
        store.start_request(
            ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id="2" * 32, now=NOW
        )
    assert store.read_planned_requests(42, "QUOTES")[0]["state"] == "PLANNED"
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 0
    assert _audit_count(store, "GET", "/api/v1/prices") == 0


def test_start_reopen_is_non_rearming_and_replay_is_typed_pre_io(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (plan_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id="2" * 32, now=NOW
    )
    reopened = OpsKernelStore(store.path)

    with pytest.raises(OpsKernelTransitionError):
        reopened.start_request(
            ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id="3" * 32, now=NOW
        )
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/prices"
    )
    assert reopened.read_reservation_counts(None, endpoint, NOW).runtime == 1
    assert _audit_count(reopened, "GET", "/api/v1/prices") == 1


def test_start_request_validates_role_identity_and_utc_now(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (plan_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelRoleError):
        store.start_request(
            ProducerRole.ANALYSIS, 42, "QUOTES", 0, invocation_id="2" * 32, now=NOW
        )
    with pytest.raises(OpsKernelTransitionError):
        store.start_request(
            ProducerRole.COLLECTOR, 42, "TRADES", 0, invocation_id="2" * 32, now=NOW
        )
    with pytest.raises(ValueError, match="timezone-aware"):
        store.start_request(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            0,
            invocation_id="2" * 32,
            now=NOW.replace(tzinfo=None),
        )
    assert store.read_planned_requests(42, "QUOTES")[0]["state"] == "PLANNED"


# F3 strict vertical: raw projection, canonical planning, and durable settlement.
def test_f3_schema_and_positive_price_batch_raw_projection(tmp_path):
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        columns = {
            row[1] for row in connection.execute("PRAGMA table_info(planned_requests)")
        }
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert {"market", "local_trade_date"} <= columns
    assert "calendar_request_counters" in tables
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (valid_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        is ReplayResult.CREATED
    )
    planned = store.read_planned_requests(42, "QUOTES")[0]
    assert (planned["market"], planned["local_trade_date"]) == (None, None)


@pytest.mark.parametrize(
    "row",
    (
        valid_row(canonical_params={"market": "KR"}),
        valid_row(
            canonical_params={"market": "KR", "sorted_unique_symbols": ["B", "A"]}
        ),
        valid_row(
            canonical_params={"market": "KR", "sorted_unique_symbols": ["A", "A"]}
        ),
        valid_row(market="KR"),
        calendar_row(local_trade_date="2026-02-30"),
        calendar_row()
        | {
            "operation": "CALENDAR_DAY_US",
            "normalized_path_template": "/api/v1/market-calendar/US",
        },
    ),
)
def test_f3_invalid_canonical_or_spoofed_plan_is_pretransaction_conflict(tmp_path, row):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "CALENDAR",
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_f3_persisted_raw_corruption_is_corrupt_on_read_and_replay(tmp_path):
    store = make_store(tmp_path)
    row = calendar_row()
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "CALENDAR",
        (row,),
        invocation_id=INVOCATION,
        now=NOW,
    )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute("UPDATE planned_requests SET market='US' WHERE tick_id=42")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError):
        store.read_planned_requests(42)
    with pytest.raises(OpsKernelCorruptError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "CALENDAR",
            (row,),
            invocation_id="2" * 32,
            now=NOW,
        )


def _calendar_count(store):
    connection = sqlite3.connect(store.path)
    try:
        return connection.execute(
            "SELECT count(*) FROM calendar_request_counters"
        ).fetchone()[0]
    finally:
        connection.close()


def test_f3_calendar_success_atomic_write_once_idempotent_and_conflict(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "CALENDAR",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
    )
    store = OpsKernelStore(store.path)
    settled = store.settle_request(
        ProducerRole.COLLECTOR, 42, "CALENDAR", 0, "OK", invocation_id="2" * 32, now=NOW
    )
    assert settled["state"] == "SETTLED" and _calendar_count(store) == 1
    assert (
        store.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "CALENDAR",
            0,
            "OK",
            invocation_id="2" * 32,
            now=NOW + timedelta(minutes=5),
        )
        is ReplayResult.DUPLICATE
    )
    assert _calendar_count(store) == 1
    with pytest.raises(OpsKernelConflictError):
        store.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "CALENDAR",
            0,
            "PROVIDER_ERROR",
            invocation_id="2" * 32,
            now=NOW,
        )


def test_f3_missing_counter_is_corrupt_and_transient_retries_next_tick(tmp_path):
    store = make_store(tmp_path)
    for tick, exit in ((42, "TRANSPORT_FAIL"), (43, "OK")):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            (calendar_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        store.start_request(
            ProducerRole.COLLECTOR, tick, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
        )
        store.settle_request(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            0,
            exit,
            invocation_id="2" * 32,
            now=NOW,
        )
        assert _calendar_count(store) == (0 if tick == 42 else 1)
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("DELETE FROM calendar_request_counters")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError):
        store.settle_request(
            ProducerRole.COLLECTOR,
            43,
            "CALENDAR",
            0,
            "OK",
            invocation_id="2" * 32,
            now=NOW,
        )


def test_f3_calendar_start_uses_separate_budget_and_get_audit(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "CALENDAR",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
    )
    endpoint = RequestEndpointV1(
        "toss", "GET", "openapi.tossinvest.com", "/api/v1/market-calendar/KR"
    )
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 0
    assert _audit_count(store, "GET", "/api/v1/market-calendar/KR") == 1


@pytest.mark.parametrize(
    "row",
    (
        valid_row(
            operation="TOKEN",
            method="POST",
            normalized_path_template="/oauth2/token",
            canonical_params={},
        ),
        valid_row(
            operation="ACCOUNTS",
            normalized_path_template="/api/v1/accounts",
            canonical_params={},
        ),
        valid_row(
            operation="ACCOUNT_HOLDINGS",
            ordinal=2,
            normalized_path_template="/api/v1/holdings",
            account_ordinal=1,
            canonical_params={"account_ordinal": 1},
        ),
        valid_row(),
        valid_row(
            operation="ORDERS_PAGE",
            normalized_path_template="/api/v1/orders",
            canonical_params={
                "account_scope": "toss-acct-" + "a" * 24,
                "window_from": "2026-07-01",
                "window_to": "2026-07-31",
                "request_cursor": None,
            },
        ),
        valid_row(
            operation="ORDER_DETAIL",
            normalized_path_template="/api/v1/orders/{orderId}",
            canonical_params={
                "account_scope": "toss-acct-" + "a" * 24,
                "order_id": "order-1",
            },
        ),
        calendar_row(),
        calendar_row(market="US"),
    ),
)
def test_f3_all_eight_operations_accept_exact_canonical_projection(tmp_path, row):
    store = make_store(tmp_path)
    job_scope = (
        "HOLDINGS"
        if row["operation"] == "ACCOUNT_HOLDINGS"
        else "TRADES"
        if row["operation"] == "ORDERS_PAGE"
        else "CALENDAR"
    )
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            1,
            job_scope,
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )
        is ReplayResult.CREATED
    )


def test_f3_calendar_577th_start_is_admitted_without_daily_cap(tmp_path):
    store = make_store(tmp_path)
    for tick in range(576):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            (calendar_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        store.start_request(
            ProducerRole.COLLECTOR, tick, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
        )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        576,
        "CALENDAR",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    started = store.start_request(
        ProducerRole.COLLECTOR,
        576,
        "CALENDAR",
        0,
        invocation_id="2" * 32,
        now=NOW,
    )
    assert started["state"] == "STARTED"


def test_f3_calendar_operation_with_quotes_scope_has_no_daily_cap(tmp_path):
    store = make_store(tmp_path)
    for tick in range(576):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick,
            "QUOTES",
            (calendar_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        store.start_request(
            ProducerRole.COLLECTOR, tick, "QUOTES", 0, invocation_id="2" * 32, now=NOW
        )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        576,
        "QUOTES",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    started = store.start_request(
        ProducerRole.COLLECTOR,
        576,
        "QUOTES",
        0,
        invocation_id="2" * 32,
        now=NOW,
    )
    assert started["state"] == "STARTED"


def test_f3_calendar_operation_with_quotes_scope_blocks_third_start_in_tick(tmp_path):
    store = make_store(tmp_path)
    rows = tuple(calendar_row(ordinal=ordinal) for ordinal in range(3))
    store.plan_requests(
        ProducerRole.COLLECTOR, 42, "QUOTES", rows, invocation_id=INVOCATION, now=NOW
    )
    for ordinal in range(2):
        store.start_request(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            ordinal,
            invocation_id="2" * 32,
            now=NOW,
        )
    with pytest.raises(OpsKernelBudgetExceeded):
        store.start_request(
            ProducerRole.COLLECTOR, 42, "QUOTES", 2, invocation_id="2" * 32, now=NOW
        )


def test_f3_price_batch_with_calendar_scope_does_not_consume_calendar_budget(tmp_path):
    store = make_store(tmp_path)
    for tick in range(576):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            (valid_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        store.start_request(
            ProducerRole.COLLECTOR, tick, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
        )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        576,
        "QUOTES",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    started = store.start_request(
        ProducerRole.COLLECTOR, 576, "QUOTES", 0, invocation_id="2" * 32, now=NOW
    )
    assert started["state"] == "STARTED"


def test_f3_planned_request_dataclass_uses_actual_attribute_input_path(tmp_path):
    store = make_store(tmp_path)
    raw = valid_row()
    row = PlannedRequestInputV1(
        ordinal=raw["ordinal"],
        operation=RequestOperation.PRICE_BATCH,
        params_digest=raw["params_digest"],
        method=raw["method"],
        host=raw["host"],
        normalized_path_template=raw["normalized_path_template"],
        account_ordinal=raw["account_ordinal"],
        market=raw["market"],
        local_trade_date=raw["local_trade_date"],
        canonical_params=raw["canonical_params"],
    )
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )
        is ReplayResult.CREATED
    )
    assert store.read_planned_requests(42, "QUOTES")[0]["operation"] == "PRICE_BATCH"


def test_f3_calendar_empty_ok_writes_counter_once_across_later_request(tmp_path):
    store = make_store(tmp_path)
    for tick in (42, 43):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            (calendar_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
        store.start_request(
            ProducerRole.COLLECTOR, tick, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
        )
        store.settle_request(
            ProducerRole.COLLECTOR,
            tick,
            "CALENDAR",
            0,
            "EMPTY_OK",
            invocation_id="2" * 32,
            now=NOW + timedelta(minutes=tick - 42),
        )
    connection = sqlite3.connect(store.path)
    try:
        counter = connection.execute(
            "SELECT successful_count,first_tick_id,updated_at FROM calendar_request_counters "
            "WHERE market='KR' AND local_trade_date='2026-07-31'"
        ).fetchone()
    finally:
        connection.close()
    assert counter == (1, 42, "2026-07-31T00:00:00.000000Z")


def test_f3_calendar_settle_rejects_foreign_invocation(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "CALENDAR",
        (calendar_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "CALENDAR", 0, invocation_id="2" * 32, now=NOW
    )
    with pytest.raises(OpsKernelTransitionError):
        store.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "CALENDAR",
            0,
            "OK",
            invocation_id="3" * 32,
            now=NOW,
        )
    assert _calendar_count(store) == 0


def test_f3_canonical_params_extra_key_is_rejected(tmp_path):
    store = make_store(tmp_path)
    row = valid_row(
        canonical_params={
            "market": "KR",
            "sorted_unique_symbols": ["005930"],
            "extra": "forbidden",
        }
    )
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_f3_settle_return_annotation_includes_duplicate_replay_result():
    assert (
        get_type_hints(OpsKernelStore.settle_request)["return"] == dict | ReplayResult
    )


# F4 strict vertical: five durable ordinal slots, atomic arm, and early-stop closure.
def test_f4_arm_count_three_arms_exact_ordinals_and_cancels_slots_four_five(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    armed = store.arm_account_rows(
        ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
    )
    assert tuple(
        (row["account_ordinal"], row["ordinal"], row["state"]) for row in armed
    ) == (
        (1, 2, "ARMED"),
        (2, 3, "ARMED"),
        (3, 4, "ARMED"),
        (4, 5, "CANCELLED_UNSTARTED"),
        (5, 6, "CANCELLED_UNSTARTED"),
    )
    assert tuple(row["exit"] for row in armed) == (
        None,
        None,
        None,
        "EARLY_STOP",
        "EARLY_STOP",
    )
    assert tuple(row["armed_at"] for row in armed) == (NOW, NOW, NOW, None, None)
    assert tuple(row["settled_at"] for row in armed) == (None, None, None, NOW, NOW)
    assert all(row["started_at"] is None for row in armed)


@pytest.mark.parametrize("job_scope,ordinal", (("QUOTES", 2), ("HOLDINGS", 9)))
def test_f4_account_plan_identity_mismatch_is_pretransaction_conflict(
    tmp_path,
    job_scope,
    ordinal,
):
    store = make_store(tmp_path)
    row = account_rows()[0] | {"ordinal": ordinal}
    with pytest.raises(OpsKernelConflictError, match="invalid request plan"):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            job_scope,
            (row,),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


@pytest.mark.parametrize("tick_id", ("42", True, 42.0, -1, 2147483648))
@pytest.mark.parametrize("method", ("arm", "settle"))
def test_f4_account_api_tick_id_is_pretransaction_typed_conflict(
    tmp_path, tick_id, method
):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError, match="invalid tick id"):
        if method == "arm":
            store.arm_account_rows(
                ProducerRole.COLLECTOR,
                tick_id,
                3,
                invocation_id=INVOCATION,
                now=NOW,
            )
        else:
            store.settle_unstarted_account_rows(
                ProducerRole.COLLECTOR,
                tick_id,
                invocation_id=INVOCATION,
                now=NOW,
            )


@pytest.mark.parametrize("count", (0, 3))
def test_f4_well_formed_incomplete_account_set_is_unavailable(tmp_path, count):
    store = make_store(tmp_path)
    if count:
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "HOLDINGS",
            account_rows()[:count],
            invocation_id=INVOCATION,
            now=NOW,
        )
    with pytest.raises(OpsKernelTransitionError, match="account rows unavailable"):
        store.settle_unstarted_account_rows(
            ProducerRole.COLLECTOR,
            42,
            invocation_id="2" * 32,
            now=NOW,
        )


def test_f4_actual_malformed_account_row_is_corrupt(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE planned_requests SET ordinal=9 WHERE tick_id=42 AND ordinal=6"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError, match="shape corrupt"):
        store.settle_unstarted_account_rows(
            ProducerRole.COLLECTOR,
            42,
            invocation_id="2" * 32,
            now=NOW,
        )


def test_f4_account_failure_then_finalizer_settles_later_armed_rows(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.arm_account_rows(
        ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "HOLDINGS", 3, invocation_id=INVOCATION, now=NOW
    )
    store.settle_request(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        3,
        "PROVIDER_ERROR",
        invocation_id=INVOCATION,
        now=NOW,
    )
    settled = store.settle_unstarted_account_rows(
        ProducerRole.COLLECTOR, 42, invocation_id=INVOCATION, now=NOW
    )
    assert tuple(row["account_ordinal"] for row in settled) == (1, 3)
    rows = store.read_planned_requests(42, "HOLDINGS")
    assert tuple(
        (row["account_ordinal"], row["state"], row["exit"]) for row in rows
    ) == (
        (1, "CANCELLED_UNSTARTED", "EARLY_STOP"),
        (2, "SETTLED", "PROVIDER_ERROR"),
        (3, "CANCELLED_UNSTARTED", "EARLY_STOP"),
        (4, "CANCELLED_UNSTARTED", "EARLY_STOP"),
        (5, "CANCELLED_UNSTARTED", "EARLY_STOP"),
    )
    assert rows[1]["started_at"] == NOW
    assert rows[1]["settled_at"] == NOW


def test_f4_arm_transaction_crash_rolls_back_every_slot(tmp_path, monkeypatch):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    original = store._planned_request
    calls = 0

    def crash_on_projection(row):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("injected")
        return original(row)

    monkeypatch.setattr(store, "_planned_request", crash_on_projection)
    with pytest.raises(RuntimeError, match="injected"):
        store.arm_account_rows(
            ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
        )
    monkeypatch.setattr(store, "_planned_request", original)
    assert {row["state"] for row in store.read_planned_requests(42, "HOLDINGS")} == {
        "PLANNED"
    }


def test_f4_fresh_invocation_reopen_closes_open_slots_without_rearming(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.arm_account_rows(
        ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
    )
    reopened = OpsKernelStore(store.path)
    with pytest.raises(OpsKernelTransitionError):
        reopened.arm_account_rows(
            ProducerRole.COLLECTOR, 42, 3, invocation_id="2" * 32, now=NOW
        )
    settled = reopened.settle_unstarted_account_rows(
        ProducerRole.COLLECTOR,
        42,
        invocation_id="2" * 32,
        now=NOW + timedelta(minutes=1),
    )
    assert tuple(row["account_ordinal"] for row in settled) == (1, 2, 3)
    assert all(row["settled_at"] == NOW + timedelta(minutes=1) for row in settled)
    assert {row["state"] for row in reopened.read_planned_requests(42, "HOLDINGS")} == {
        "CANCELLED_UNSTARTED"
    }
    assert (
        reopened.settle_unstarted_account_rows(
            ProducerRole.COLLECTOR,
            42,
            invocation_id="3" * 32,
            now=NOW,
        )
        == ()
    )
    assert (
        reopened.plan_requests(
            ProducerRole.COLLECTOR,
            43,
            "HOLDINGS",
            account_rows(),
            invocation_id="3" * 32,
            now=NOW,
        )
        is ReplayResult.CREATED
    )


def test_f4_auth_prearm_failure_cancels_all_five_and_next_tick_has_new_identity(
    tmp_path,
):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    cancelled = store.settle_unstarted_account_rows(
        ProducerRole.COLLECTOR, 42, invocation_id=INVOCATION, now=NOW
    )
    assert len(cancelled) == 5
    assert {(row["state"], row["exit"]) for row in cancelled} == {
        ("CANCELLED_UNSTARTED", "EARLY_STOP")
    }
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            43,
            "HOLDINGS",
            account_rows(),
            invocation_id="2" * 32,
            now=NOW,
        )
        is ReplayResult.CREATED
    )


@pytest.mark.parametrize("account_count", (True, 1.0, "1", 0, 6))
def test_f4_account_count_is_exact_int_one_through_five(tmp_path, account_count):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelConflictError):
        store.arm_account_rows(
            ProducerRole.COLLECTOR, 42, account_count, invocation_id=INVOCATION, now=NOW
        )
    assert {row["state"] for row in store.read_planned_requests(42, "HOLDINGS")} == {
        "PLANNED"
    }


def test_f4_account_surface_and_ddl_have_exact_supported_behavior(tmp_path):
    assert "BOUND" not in inspect.getsource(ops_kernel_module)
    assert (
        "account_scope"
        not in inspect.signature(OpsKernelStore.arm_account_rows).parameters
    )
    assert (
        "account_scope"
        not in inspect.signature(
            OpsKernelStore.settle_unstarted_account_rows
        ).parameters
    )
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        table_sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='planned_requests'"
        ).fetchone()[0]
    finally:
        connection.close()
    assert "'BOUND'" not in table_sql
    assert (
        "state TEXT NOT NULL CHECK(state IN\n    ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING',\n     'SETTLED','UNKNOWN','CANCELLED_UNSTARTED'))"
        in table_sql
    )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    assert (
        store.start_request(
            ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id=INVOCATION, now=NOW
        )["state"]
        == "STARTED"
    )


def test_f4_account_finalizer_leaves_started_and_nonaccount_rows_untouched(tmp_path):
    store = make_store(tmp_path)
    rows = (
        valid_row(
            ordinal=0,
            operation="TOKEN",
            method="POST",
            normalized_path_template="/oauth2/token",
            canonical_params={},
        ),
        valid_row(
            ordinal=1,
            operation="ACCOUNTS",
            normalized_path_template="/api/v1/accounts",
            canonical_params={},
        ),
        *account_rows(),
    )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        rows,
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.arm_account_rows(
        ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
    )
    started = store.start_request(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        2,
        invocation_id="2" * 32,
        now=NOW,
    )
    store.settle_unstarted_account_rows(
        ProducerRole.COLLECTOR,
        42,
        invocation_id="3" * 32,
        now=NOW + timedelta(minutes=1),
    )
    final = store.read_planned_requests(42, "HOLDINGS")
    assert tuple((row["operation"], row["state"]) for row in final[:2]) == (
        ("TOKEN", "PLANNED"),
        ("ACCOUNTS", "PLANNED"),
    )
    assert final[2] == started


def test_f4_account_start_requires_armed_row_and_exact_shape(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelTransitionError):
        store.start_request(
            ProducerRole.COLLECTOR, 42, "HOLDINGS", 2, invocation_id=INVOCATION, now=NOW
        )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE planned_requests SET ordinal=7 WHERE tick_id=42 AND ordinal=6"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError):
        store.arm_account_rows(
            ProducerRole.COLLECTOR, 42, 3, invocation_id=INVOCATION, now=NOW
        )


def test_f4_role_and_invocation_fail_closed_without_mutation(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "HOLDINGS",
        account_rows(),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelRoleError):
        store.arm_account_rows(
            ProducerRole.ANALYSIS, 42, 3, invocation_id=INVOCATION, now=NOW
        )
    with pytest.raises(ValueError):
        store.settle_unstarted_account_rows(
            ProducerRole.COLLECTOR, 42, invocation_id="bad", now=NOW
        )
    with pytest.raises(OpsKernelTransitionError):
        store.arm_account_rows(
            ProducerRole.COLLECTOR, 42, 3, invocation_id="2" * 32, now=NOW
        )
    assert {row["state"] for row in store.read_planned_requests(42, "HOLDINGS")} == {
        "PLANNED"
    }


# F5 strict vertical: durable ORDERS_PAGE checkpoint-before-apply only.
ACCOUNT_SCOPE = "toss-acct-" + "a" * 24


def orders_row(**changes):
    params = changes.pop(
        "canonical_params",
        {
            "account_scope": ACCOUNT_SCOPE,
            "window_from": "2026-07-01",
            "window_to": "2026-07-31",
            "request_cursor": None,
        },
    )
    return valid_row(
        operation="ORDERS_PAGE",
        normalized_path_template="/api/v1/orders",
        canonical_params=params,
        **changes,
    )


def child_orders_row(ordinal=1, cursor="next", **changes):
    params = changes.pop(
        "canonical_params",
        {
            "account_scope": ACCOUNT_SCOPE,
            "window_from": "2026-07-01",
            "window_to": "2026-07-31",
            "request_cursor": cursor,
        },
    )
    return valid_row(
        ordinal=ordinal,
        operation="ORDERS_PAGE",
        normalized_path_template="/api/v1/orders",
        canonical_params=params,
        **changes,
    )


def started_orders(store, *, tick_id=42, invocation_id=INVOCATION):
    store.plan_requests(
        ProducerRole.COLLECTOR,
        tick_id,
        "TRADES",
        (orders_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    return store.start_request(
        ProducerRole.COLLECTOR,
        tick_id,
        "TRADES",
        0,
        invocation_id=invocation_id,
        now=NOW,
    )


def checkpoint_kwargs(**changes):
    orders_json = changes.pop("orders_json", "[]")
    values = {
        "account_scope": ACCOUNT_SCOPE,
        "window_from": "2026-07-01",
        "window_to": "2026-07-31",
        "request_cursor": None,
        "orders_json": orders_json,
        "has_next": False,
        "next_cursor": None,
        "page_digest": hashlib.sha256(orders_json.encode()).hexdigest(),
        "invocation_id": INVOCATION,
        "now": NOW,
    }
    values.update(changes)
    return values


@pytest.mark.parametrize("parent_exit", ("OK", "EMPTY_OK"))
def test_f7_response_derived_child_requires_eligible_durable_parent_and_exact_binding(
    tmp_path, parent_exit
):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )
    store.begin_page_apply(
        ProducerRole.COLLECTOR, 42, 0, invocation_id=INVOCATION, now=NOW
    )
    store.settle_request(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        0,
        parent_exit,
        invocation_id=INVOCATION,
        now=NOW,
    )
    other = {
        "account_scope": "toss-acct-" + "b" * 24,
        "window_from": "2026-07-01",
        "window_to": "2026-07-31",
        "request_cursor": None,
    }
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        (orders_row(ordinal=1, canonical_params=other),),
        invocation_id="2" * 32,
        now=NOW,
    )

    child = child_orders_row(ordinal=2)
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child,),
            invocation_id="3" * 32,
            now=NOW,
        )
        is ReplayResult.CREATED
    )
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child,),
            invocation_id="4" * 32,
            now=NOW,
        )
        is ReplayResult.DUPLICATE
    )
    assert [row["ordinal"] for row in store.read_planned_requests(42, "TRADES")] == [
        0,
        1,
        2,
    ]


@pytest.mark.parametrize(
    "drift",
    (
        {"cursor": "guessed"},
        {
            "canonical_params": {
                "account_scope": ACCOUNT_SCOPE,
                "window_from": "2026-07-02",
                "window_to": "2026-07-31",
                "request_cursor": "next",
            }
        },
    ),
)
def test_f7_guessed_or_drifted_child_conflicts_without_mutation(tmp_path, drift):
    store = make_store(tmp_path)
    started_orders(store)
    before = (store.read_planned_requests(42, "TRADES"), raw_pages(store))
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(**drift),),
            invocation_id="2" * 32,
            now=NOW,
        )
    assert (store.read_planned_requests(42, "TRADES"), raw_pages(store)) == before


@pytest.mark.parametrize(
    "changed_window",
    (
        {"window_from": "2026-07-02", "window_to": "2026-07-31"},
        {"window_from": "2026-07-01", "window_to": "2026-07-30"},
    ),
)
def test_f7_checkpointed_parent_rejects_correct_cursor_with_window_drift_mutation_zero(
    tmp_path, changed_window
):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )

    def durable_state():
        with sqlite3.connect(store.path) as connection:
            return tuple(
                tuple(connection.execute(f"SELECT * FROM {table} ORDER BY 1,2,3"))
                for table in (
                    "planned_requests",
                    "planned_request_pages",
                    "request_day_counters",
                )
            )

    before = durable_state()
    params = {
        "account_scope": ACCOUNT_SCOPE,
        "request_cursor": "next",
        **changed_window,
    }
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(canonical_params=params),),
            invocation_id="2" * 32,
            now=NOW,
        )
    assert durable_state() == before


@pytest.mark.parametrize(
    "operation,ordinal", (("ORDERS_PAGE", 10), ("ORDER_DETAIL", 9))
)
def test_f7_trades_ordinal_partitions_are_exact_and_pretransaction(
    operation, ordinal, tmp_path
):
    store = make_store(tmp_path)
    params = (
        {
            "account_scope": ACCOUNT_SCOPE,
            "window_from": "2026-07-01",
            "window_to": "2026-07-31",
            "request_cursor": None,
        }
        if operation == "ORDERS_PAGE"
        else {"account_scope": ACCOUNT_SCOPE, "order_id": "order-1"}
    )
    path = (
        "/api/v1/orders" if operation == "ORDERS_PAGE" else "/api/v1/orders/{orderId}"
    )
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (
                valid_row(
                    ordinal=ordinal,
                    operation=operation,
                    normalized_path_template=path,
                    canonical_params=params,
                ),
            ),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_f7_one_parent_cannot_have_two_live_children(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        (child_orders_row(),),
        invocation_id="2" * 32,
        now=NOW,
    )
    before = store.read_planned_requests(42, "TRADES")
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(ordinal=2),),
            invocation_id="3" * 32,
            now=NOW,
        )
    assert store.read_planned_requests(42, "TRADES") == before


def test_f7_checkpointed_first_page_exact_plan_replay_is_duplicate(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )
    before = (store.read_planned_requests(42, "TRADES"), raw_pages(store))
    assert (
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (orders_row(),),
            invocation_id="2" * 32,
            now=NOW,
        )
        is ReplayResult.DUPLICATE
    )
    assert (store.read_planned_requests(42, "TRADES"), raw_pages(store)) == before


def test_f7_second_first_page_for_scope_conflicts_without_mutation(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        (orders_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    before = store.read_planned_requests(42, "TRADES")
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(ordinal=1, cursor=None),),
            invocation_id="2" * 32,
            now=NOW,
        )
    assert store.read_planned_requests(42, "TRADES") == before


def test_f7_orders_page_is_bound_to_trades_scope_pretransaction(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelConflictError):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            (orders_row(),),
            invocation_id=INVOCATION,
            now=NOW,
        )
    assert store.read_planned_requests(42) == ()


def test_f7_third_orders_page_for_scope_is_typed_denial_and_mutation_zero_after_reopen(
    tmp_path,
):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )
    store.begin_page_apply(
        ProducerRole.COLLECTOR, 42, 0, invocation_id=INVOCATION, now=NOW
    )
    store.settle_request(
        ProducerRole.COLLECTOR, 42, "TRADES", 0, "OK", invocation_id=INVOCATION, now=NOW
    )
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        (child_orders_row(),),
        invocation_id="2" * 32,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "TRADES", 1, invocation_id="2" * 32, now=NOW
    )
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        1,
        **checkpoint_kwargs(
            request_cursor="next",
            has_next=True,
            next_cursor="third",
            invocation_id="2" * 32,
        ),
    )
    reopened = OpsKernelStore(store.path)
    before = (reopened.read_planned_requests(42, "TRADES"), raw_pages(reopened))
    with pytest.raises(OpsKernelBudgetExceeded):
        reopened.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(ordinal=2, cursor="third"),),
            invocation_id="3" * 32,
            now=NOW,
        )
    assert (reopened.read_planned_requests(42, "TRADES"), raw_pages(reopened)) == before


@pytest.mark.parametrize("parent_state", ("APPLYING", "SETTLED_BAD", "MISSING_CURSOR"))
def test_f7_ineligible_parent_variants_reject_child_without_mutation(
    tmp_path, parent_state
):
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR,
        42,
        0,
        **checkpoint_kwargs(has_next=True, next_cursor="next"),
    )
    if parent_state == "APPLYING":
        store.begin_page_apply(
            ProducerRole.COLLECTOR, 42, 0, invocation_id=INVOCATION, now=NOW
        )
    elif parent_state == "SETTLED_BAD":
        store.begin_page_apply(
            ProducerRole.COLLECTOR, 42, 0, invocation_id=INVOCATION, now=NOW
        )
        store.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            0,
            "STORE_APPLY_FAILED",
            invocation_id=INVOCATION,
            now=NOW,
        )
    else:
        with sqlite3.connect(store.path) as db:
            db.execute("PRAGMA ignore_check_constraints=ON")
            db.execute(
                "UPDATE planned_request_pages SET next_cursor=NULL WHERE tick_id=42"
            )
    before = store.read_planned_requests(42, "TRADES")
    expected = (
        OpsKernelCorruptError
        if parent_state == "MISSING_CURSOR"
        else OpsKernelConflictError
    )
    with pytest.raises(expected):
        store.plan_requests(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            (child_orders_row(),),
            invocation_id="2" * 32,
            now=NOW,
        )
    assert store.read_planned_requests(42, "TRADES") == before


def raw_pages(store):
    connection = sqlite3.connect(store.path)
    connection.row_factory = sqlite3.Row
    try:
        return tuple(
            dict(row)
            for row in connection.execute(
                "SELECT * FROM planned_request_pages ORDER BY tick_id,ordinal"
            )
        )
    finally:
        connection.close()


def test_f5_public_checkpoint_signature_and_exact_page_ddl(tmp_path):
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    try:
        table = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE type='table' AND name='planned_request_pages'"
        ).fetchone()
        assert table is not None, "behavioral RED: durable page table is absent"
        sql = table[0]
        identity = connection.execute(
            "SELECT group_concat(name, ',') FROM (SELECT ii.name FROM pragma_index_list('planned_request_pages') il "
            "JOIN pragma_index_info(il.name) ii WHERE il.origin='pk' ORDER BY ii.seqno)"
        ).fetchone()[0]
        foreign = tuple(
            connection.execute("PRAGMA foreign_key_list(planned_request_pages)")
        )
    finally:
        connection.close()
    assert identity == "tick_id,job_scope,ordinal" and len(foreign) == 3
    assert tuple(
        inspect.signature(OpsKernelStore.checkpoint_orders_page).parameters
    ) == (
        "self",
        "role",
        "tick_id",
        "ordinal",
        "account_scope",
        "window_from",
        "window_to",
        "request_cursor",
        "orders_json",
        "has_next",
        "next_cursor",
        "page_digest",
        "invocation_id",
        "now",
    )
    for clause in (
        "job_scope TEXT NOT NULL CHECK(job_scope='TRADES')",
        "ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9)",
        "schema_version INTEGER NOT NULL CHECK(schema_version=1)",
        "length(CAST(orders_json AS BLOB)) BETWEEN 2 AND 2097152",
        "length(page_digest)=64",
        "has_next IN (0,1)",
        "next_cursor<>request_cursor",
        "created_at TEXT NOT NULL CHECK(",
    ):
        assert clause in sql


def test_f5_started_orders_checkpoint_is_atomic_exact_and_empty_allowed(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    result = store.checkpoint_orders_page(
        ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
    )
    digest = hashlib.sha256(b"[]").hexdigest()
    assert (result["state"], result["checkpointed_at"]) == ("CHECKPOINTED", NOW)
    assert result["checkpoint_digest"] != digest
    assert len(result["checkpoint_digest"]) == 64
    assert raw_pages(store) == (
        {
            "tick_id": 42,
            "job_scope": "TRADES",
            "ordinal": 0,
            "schema_version": 1,
            "account_scope": ACCOUNT_SCOPE,
            "window_from": "2026-07-01",
            "window_to": "2026-07-31",
            "request_cursor": None,
            "orders_json": "[]",
            "has_next": 0,
            "next_cursor": None,
            "page_digest": digest,
            "created_at": "2026-07-31T00:00:00.000000Z",
        },
    )


def test_f5_recomputes_digest_and_binds_every_page_field_to_plan(tmp_path):
    drift_cases = (
        {"account_scope": "toss-acct-" + "b" * 24},
        {"window_from": "2026-07-02"},
        {"window_to": "2026-07-30"},
        {"request_cursor": "cursor"},
        {"orders_json": "[ ]"},
        {"orders_json": "{}"},
        {"has_next": True, "next_cursor": None},
        {"has_next": True, "next_cursor": "x", "request_cursor": "x"},
        {"page_digest": "A" * 64},
        {"page_digest": "b" * 64},
    )
    for index, changes in enumerate(drift_cases):
        store = make_store(tmp_path / str(index))
        started_orders(store)
        kwargs = checkpoint_kwargs(**changes)
        if "orders_json" in changes and "page_digest" not in changes:
            kwargs["page_digest"] = hashlib.sha256(
                changes["orders_json"].encode()
            ).hexdigest()
        with pytest.raises((OpsKernelConflictError, OpsKernelTransitionError)):
            store.checkpoint_orders_page(ProducerRole.COLLECTOR, 42, 0, **kwargs)
        assert store.read_planned_requests(42, "TRADES")[0]["state"] == "STARTED"
        assert raw_pages(store) == ()


def test_f5_checkpoint_admission_owner_and_non_orders_reject_without_mutation(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "TRADES",
        (orders_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    with pytest.raises(OpsKernelTransitionError):
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
        )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "TRADES", 0, invocation_id=INVOCATION, now=NOW
    )
    with pytest.raises(OpsKernelTransitionError):
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs(invocation_id="2" * 32)
        )
    other = make_store(tmp_path / "other")
    other.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    other.start_request(
        ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id=INVOCATION, now=NOW
    )
    with pytest.raises(OpsKernelTransitionError):
        other.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
        )
    assert raw_pages(store) == raw_pages(other) == ()


def test_f5_exact_replay_is_idempotent_and_any_drift_conflicts_mutation_zero(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    first = store.checkpoint_orders_page(
        ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
    )
    before = (store.read_planned_requests(42, "TRADES"), raw_pages(store))
    assert (
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR,
            42,
            0,
            **checkpoint_kwargs(now=NOW + timedelta(minutes=1)),
        )
        == first
    )
    with pytest.raises(OpsKernelTransitionError):
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs(invocation_id="2" * 32)
        )
    assert (store.read_planned_requests(42, "TRADES"), raw_pages(store)) == before
    for changes in (
        {"orders_json": "[{}]"},
        {"next_cursor": "x"},
        {"window_to": "2026-07-30"},
    ):
        kwargs = checkpoint_kwargs(**changes)
        kwargs["page_digest"] = hashlib.sha256(
            kwargs["orders_json"].encode()
        ).hexdigest()
        with pytest.raises(OpsKernelConflictError):
            store.checkpoint_orders_page(ProducerRole.COLLECTOR, 42, 0, **kwargs)
        assert (store.read_planned_requests(42, "TRADES"), raw_pages(store)) == before


def test_f5_checkpoint_projection_crash_rolls_back_page_and_parent(
    tmp_path, monkeypatch
):
    store = make_store(tmp_path)
    started_orders(store)
    original = store._planned_request
    monkeypatch.setattr(
        store,
        "_planned_request",
        lambda row: (_ for _ in ()).throw(RuntimeError("injected")),
    )
    with pytest.raises(RuntimeError, match="injected"):
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
        )
    monkeypatch.setattr(store, "_planned_request", original)
    reopened = OpsKernelStore(store.path)
    assert reopened.read_planned_requests(42, "TRADES")[0]["state"] == "STARTED"
    assert raw_pages(reopened) == ()


def test_f5_checkpoint_survives_reopen_and_replay_needs_no_provider(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    expected = store.checkpoint_orders_page(
        ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
    )
    reopened = OpsKernelStore(store.path)
    assert raw_pages(reopened)[0]["orders_json"] == "[]"
    assert (
        reopened.checkpoint_orders_page(
            ProducerRole.COLLECTOR,
            42,
            0,
            **checkpoint_kwargs(now=NOW + timedelta(hours=1)),
        )
        == expected
    )


def _f11_orders_page(store, tick_id, state, now):
    store.plan_requests(
        ProducerRole.COLLECTOR,
        tick_id,
        "TRADES",
        (orders_row(),),
        invocation_id=INVOCATION,
        now=now,
    )
    store.start_request(
        ProducerRole.COLLECTOR, tick_id, "TRADES", 0, invocation_id=INVOCATION, now=now
    )
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR, tick_id, 0, **checkpoint_kwargs(now=now)
    )
    if state in ("APPLYING", "SETTLED"):
        store.begin_page_apply(
            ProducerRole.COLLECTOR, tick_id, 0, invocation_id=INVOCATION, now=now
        )
    if state == "SETTLED":
        store.settle_request(
            ProducerRole.COLLECTOR,
            tick_id,
            "TRADES",
            0,
            "OK",
            invocation_id=INVOCATION,
            now=now,
        )


def _f11_counts(store):
    with sqlite3.connect(store.path) as connection:
        return {
            table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in (
                "planned_requests",
                "planned_request_pages",
                "request_day_counters",
                "calendar_request_counters",
            )
        }


def test_f11_current_utc_day_rows_pages_and_counters_are_pinned(tmp_path):
    store = make_store(tmp_path)
    base = tick_id_from_utc(NOW).tick_id
    store.plan_requests(
        ProducerRole.COLLECTOR,
        base,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    for offset, state in enumerate(("CHECKPOINTED", "APPLYING", "SETTLED"), 1):
        _f11_orders_page(
            store, base + offset, state, NOW + timedelta(minutes=offset * 5)
        )
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO calendar_request_counters VALUES(?,?,?,?,?)",
            ("KR", "2026-07-31", 1, base, "2026-07-31T00:00:00.000000Z"),
        )
    before = _f11_counts(store)

    assert store.prune_request_parity(
        ProducerRole.COLLECTOR, now=NOW + timedelta(hours=23)
    ) == {
        "planned_request_pages": 0,
        "planned_requests": 0,
    }
    assert _f11_counts(store) == before


def test_f11_corruption_stops_before_deleting_evidence(tmp_path):
    store = make_store(tmp_path)
    old = tick_id_from_utc(NOW - timedelta(days=2)).tick_id
    _f11_orders_page(store, old, "SETTLED", NOW - timedelta(days=2))
    corrupt = old + 1
    _f11_orders_page(store, corrupt, "CHECKPOINTED", NOW - timedelta(days=2))
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "DELETE FROM planned_request_pages WHERE tick_id=?", (corrupt,)
        )
    before = _f11_counts(store)

    with pytest.raises(OpsKernelCorruptError, match="checkpoint"):
        store.prune_request_parity(ProducerRole.COLLECTOR, now=NOW)
    assert _f11_counts(store) == before
    assert store.read_planned_requests(corrupt, "TRADES")[0]["state"] == "CHECKPOINTED"


def test_f11_old_eligible_child_is_deleted_before_parent_and_current_pair_remains(
    tmp_path,
):
    store = make_store(tmp_path)
    old = tick_id_from_utc(NOW - timedelta(days=1)).tick_id
    current = tick_id_from_utc(NOW).tick_id
    _f11_orders_page(store, old, "SETTLED", NOW - timedelta(days=1))
    _f11_orders_page(store, current, "SETTLED", NOW)

    assert store.prune_request_parity(ProducerRole.COLLECTOR, now=NOW) == {
        "planned_request_pages": 1,
        "planned_requests": 1,
    }
    assert store.read_planned_requests(old, "TRADES") == ()
    assert len(store.read_planned_requests(current, "TRADES")) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT tick_id FROM planned_request_pages ORDER BY tick_id"
        ).fetchall() == [(current,)]
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_f5_reconcile_foreign_started_only_to_closed_unknown(tmp_path):
    store = make_store(tmp_path)
    started_orders(store, tick_id=41, invocation_id="2" * 32)
    started_orders(store, tick_id=42, invocation_id=INVOCATION)
    started_orders(store, tick_id=43, invocation_id="2" * 32)
    store.checkpoint_orders_page(
        ProducerRole.COLLECTOR, 43, 0, **checkpoint_kwargs(invocation_id="2" * 32)
    )
    reconciled = store.reconcile_requests(
        ProducerRole.COLLECTOR, invocation_id=INVOCATION, now=NOW + timedelta(minutes=1)
    )
    assert tuple((row["tick_id"], row["state"], row["exit"]) for row in reconciled) == (
        (41, "UNKNOWN", "UNKNOWN_OUTCOME"),
    )
    assert store.read_planned_requests(42, "TRADES")[0]["state"] == "STARTED"
    assert store.read_planned_requests(43, "TRADES")[0]["state"] == "CHECKPOINTED"


def test_f5_settle_unknown_outcome_uses_closed_unknown_state(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    store.start_request(
        ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id=INVOCATION, now=NOW
    )
    result = store.settle_request(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        0,
        "UNKNOWN_OUTCOME",
        invocation_id=INVOCATION,
        now=NOW + timedelta(minutes=1),
    )
    assert (result["state"], result["exit"], result["settled_at"]) == (
        "UNKNOWN",
        "UNKNOWN_OUTCOME",
        NOW + timedelta(minutes=1),
    )


def test_f5_read_rejects_corrupt_job_scope_instead_of_silently_projecting(tmp_path):
    store = make_store(tmp_path)
    store.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(
            "UPDATE planned_requests SET job_scope='BROKEN' WHERE tick_id=42"
        )
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError):
        store.read_planned_requests(42)


def test_f6_begin_page_apply_exact_api_foreign_claim_and_idempotent_reentry(tmp_path):
    assert tuple(inspect.signature(OpsKernelStore.begin_page_apply).parameters) == (
        "self",
        "role",
        "tick_id",
        "ordinal",
        "invocation_id",
        "now",
    )
    store = make_store(tmp_path)
    started_orders(store)
    store.checkpoint_orders_page(ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs())
    applied = store.begin_page_apply(
        ProducerRole.COLLECTOR,
        42,
        0,
        invocation_id="2" * 32,
        now=NOW + timedelta(minutes=1),
    )
    assert (applied["state"], applied["applying_at"]) == (
        "APPLYING",
        NOW + timedelta(minutes=1),
    )
    assert applied["orders_page"] == {
        "schema_version": 1,
        "tick_id": 42,
        "request_ordinal": 0,
        "account_scope": ACCOUNT_SCOPE,
        "window_from": "2026-07-01",
        "window_to": "2026-07-31",
        "request_cursor": None,
        "orders_json": "[]",
        "has_next": False,
        "next_cursor": None,
        "page_digest": hashlib.sha256(b"[]").hexdigest(),
    }
    assert (
        store.begin_page_apply(
            ProducerRole.COLLECTOR,
            42,
            0,
            invocation_id="3" * 32,
            now=NOW + timedelta(minutes=2),
        )
        == applied
    )


def test_f6_begin_page_apply_rejects_missing_torn_and_digest_drift_mutation_zero(
    tmp_path,
):
    for index, mutation in enumerate(
        (
            "DELETE FROM planned_request_pages",
            "UPDATE planned_request_pages SET orders_json='[{}]'",
            "UPDATE planned_request_pages SET account_scope='toss-acct-' || printf('%024d', 1)",
        )
    ):
        store = make_store(tmp_path / str(index))
        started_orders(store)
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
        )
        connection = sqlite3.connect(store.path)
        try:
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("PRAGMA ignore_check_constraints=ON")
            connection.execute(mutation)
            connection.commit()
        finally:
            connection.close()
        before = store.read_planned_requests(42, "TRADES")
        with pytest.raises(OpsKernelCorruptError):
            store.begin_page_apply(
                ProducerRole.COLLECTOR, 42, 0, invocation_id="2" * 32, now=NOW
            )
        assert store.read_planned_requests(42, "TRADES") == before


@pytest.mark.parametrize(
    "column,value",
    (
        ("schema_version", "2"),
        ("tick_id", "43"),
        ("job_scope", "'QUOTES'"),
        ("ordinal", "1"),
        ("account_scope", "'toss-acct-' || printf('%024d', 1)"),
        ("window_from", "'2026-07-02'"),
        ("window_to", "'2026-07-30'"),
        ("request_cursor", "'tampered-request'"),
        ("page_digest", "printf('%064d', 1)"),
        ("has_next", "0"),
        ("next_cursor", "'ATTACKER'"),
    ),
)
def test_f6_checkpoint_binding_rejects_every_primitive_content_tamper_mutation_zero(
    tmp_path, column, value
):
    store = make_store(tmp_path)
    started_orders(store)
    kwargs = checkpoint_kwargs(has_next=True, next_cursor="next")
    store.checkpoint_orders_page(ProducerRole.COLLECTOR, 42, 0, **kwargs)
    before = store.read_planned_requests(42, "TRADES")
    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("PRAGMA ignore_check_constraints=ON")
        connection.execute(f"UPDATE planned_request_pages SET {column}={value}")
        connection.commit()
    finally:
        connection.close()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(store.path).begin_page_apply(
            ProducerRole.COLLECTOR,
            42,
            0,
            invocation_id="2" * 32,
            now=NOW,
        )
    assert store.read_planned_requests(42, "TRADES") == before


def test_f6_begin_rejects_started_without_mutation(tmp_path):
    store = make_store(tmp_path)
    started_orders(store)
    before = store.read_planned_requests(42, "TRADES")
    with pytest.raises(OpsKernelTransitionError):
        store.begin_page_apply(
            ProducerRole.COLLECTOR, 42, 0, invocation_id=INVOCATION, now=NOW
        )
    assert store.read_planned_requests(42, "TRADES") == before


def test_f6_applying_settlement_is_closed_and_scope_bound(tmp_path):
    for exit in ("OK", "EMPTY_OK", "STORE_APPLY_FAILED"):
        store = make_store(tmp_path / exit)
        started_orders(store)
        store.checkpoint_orders_page(
            ProducerRole.COLLECTOR, 42, 0, **checkpoint_kwargs()
        )
        store.begin_page_apply(
            ProducerRole.COLLECTOR, 42, 0, invocation_id="2" * 32, now=NOW
        )
        settled = store.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "TRADES",
            0,
            exit,
            invocation_id="3" * 32,
            now=NOW + timedelta(minutes=1),
        )
        assert (settled["state"], settled["exit"]) == ("SETTLED", exit)
        assert (
            store.settle_request(
                ProducerRole.COLLECTOR,
                42,
                "TRADES",
                0,
                exit,
                invocation_id="4" * 32,
                now=NOW + timedelta(minutes=2),
            )
            is ReplayResult.DUPLICATE
        )
    ordinary = make_store(tmp_path / "ordinary")
    ordinary.plan_requests(
        ProducerRole.COLLECTOR,
        42,
        "QUOTES",
        (valid_row(),),
        invocation_id=INVOCATION,
        now=NOW,
    )
    ordinary.start_request(
        ProducerRole.COLLECTOR, 42, "QUOTES", 0, invocation_id=INVOCATION, now=NOW
    )
    with pytest.raises(OpsKernelTransitionError):
        ordinary.settle_request(
            ProducerRole.COLLECTOR,
            42,
            "QUOTES",
            0,
            "STORE_APPLY_FAILED",
            invocation_id=INVOCATION,
            now=NOW,
        )
