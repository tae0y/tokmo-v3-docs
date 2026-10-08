"""Stage 1-F: versioned tick binding and atomic wire-start admission (SDS §7.3).

The cadence admission authority is the production ``OpsKernelStore`` itself:
one SQLite DB, one ``BEGIN IMMEDIATE`` transaction per start, no parallel
ledger. Every expectation below is derived from an injected policy object —
never from copied production limit literals.
"""

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)
from tokmo.values.ops_kernel import ProducerRole, ReplayResult

SLOT = datetime(2026, 8, 1, 12, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 1, 12, 0, 3, tzinfo=timezone.utc)
ROLE = ProducerRole.COLLECTOR
NO_CHILDREN = TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0)


def custom_policy(*, version="cadence-test-v1", **limits):
    return CadencePolicyV1(
        policy_version=version, cadence_seconds=60, max_start_lag_seconds=10,
        child_timeout_seconds=40, termination_margin_seconds=10,
        request_limits=TickRequestLimitsV1(
            **{"total_wire_starts": 27, **limits},
        ),
    )


def make_store(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")


def plan(store, policy, operation, identity, *, slot=SLOT, now=NOW,
         child_limits=NO_CHILDREN):
    return store.plan_cadence_requests(
        ROLE, policy, scheduled_for_utc=slot,
        requests=((operation, identity),), now=now, child_limits=child_limits,
    )


def start(store, policy, operation, identity, *, slot=SLOT, now=NOW,
          child_limits=NO_CHILDREN):
    return store.start_cadence_request(
        ROLE, policy, scheduled_for_utc=slot, operation=operation,
        native_identity=identity, now=now, child_limits=child_limits,
    )


def admit(store, policy, operation, identity, *, slot=SLOT, now=NOW,
          child_limits=NO_CHILDREN):
    plan(store, policy, operation, identity, slot=slot, now=now,
         child_limits=child_limits)
    return start(store, policy, operation, identity, slot=slot, now=now,
                 child_limits=child_limits)


def raw_counts(store):
    connection = sqlite3.connect(store.path)
    try:
        ticks = connection.execute("SELECT count(*) FROM cadence_ticks").fetchone()[0]
        wires = connection.execute(
            "SELECT count(*) FROM cadence_wire_starts WHERE state<>'PLANNED'"
        ).fetchone()[0]
    finally:
        connection.close()
    return ticks, wires


def test_first_admission_pins_snapshot_and_records_versioned_wire_start(tmp_path):
    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1, calendar_starts=1, holdings_starts=1,
                           quote_starts=0, trades_starts=0)
    record = admit(store, policy, "TOKEN", "token:tick0")
    assert record["operation_group"] == "TOKEN"
    assert record["operation"] == "TOKEN"
    assert record["state"] == "STARTED"
    assert record["policy_version"] == policy.policy_version

    report = store.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["snapshot"]["request_limits"]["token_starts"] == (
        policy.request_limits.token_starts
    )
    assert report["total_started"] == 1
    assert report["group_started"]["TOKEN"] == 1


def test_missing_or_invalid_versioned_binding_admits_nothing(tmp_path):
    from tokmo.values.ops_kernel import CadenceBindingError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1, calendar_starts=1)
    cases = (
        dict(scheduled_for_utc=None),
        dict(scheduled_for_utc=SLOT.replace(tzinfo=None)),
        dict(scheduled_for_utc=SLOT + timedelta(seconds=7)),  # off the policy grid
        dict(operation="EMERGENCY"),  # outside the closed operation enum
        dict(operation="CALENDAR"),   # a group name is not an operation
        dict(operation=None),
        dict(policy=None),
        dict(native_identity=""),
        dict(native_identity="token:whatever"),  # non-canonical for TOKEN
        dict(operation="CALENDAR_DAY_KR", native_identity="calendar:KR:not-a-date:0"),
        dict(operation="CALENDAR_DAY_KR", native_identity="calendar:US:2026-08-01:0"),
        dict(operation="CALENDAR_DAY_KR", native_identity="calendar:KR:2026-08-01:9"),
    )
    for changes in cases:
        arguments = dict(
            policy=policy, scheduled_for_utc=SLOT, operation="TOKEN",
            native_identity="token:tick0", now=NOW,
            child_limits=TradesChildLimitsV1(),
        )
        arguments.update(changes)
        used_policy = arguments.pop("policy")
        operation = arguments.pop("operation")
        identity = arguments.pop("native_identity")
        with pytest.raises(CadenceBindingError):
            store.plan_cadence_requests(
                ROLE, used_policy, requests=((operation, identity),), **arguments,
            )
        with pytest.raises(CadenceBindingError):
            store.start_cadence_request(
                ROLE, used_policy, operation=operation, native_identity=identity,
                **arguments,
            )
    assert raw_counts(store) == (0, 0)


def test_unplanned_identity_start_is_refused_with_mutation_zero(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1)
    with pytest.raises(CadenceTransitionError):
        start(store, policy, "TOKEN", "token:tick0")
    assert raw_counts(store) == (0, 0)
    connection = sqlite3.connect(store.path)
    try:
        assert connection.execute(
            "SELECT count(*) FROM cadence_wire_starts"
        ).fetchone()[0] == 0
    finally:
        connection.close()


def test_start_requires_the_exact_planned_operation_row(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1, holdings_starts=2)
    plan(store, policy, "ACCOUNT_HOLDINGS", "holdings:1")
    # Same identity presented under a different closed operation must not start.
    with pytest.raises(CadenceTransitionError):
        start(store, policy, "ACCOUNTS", "accounts:tick0")
    assert raw_counts(store)[1] == 0
    started = start(store, policy, "ACCOUNT_HOLDINGS", "holdings:1")
    assert started["state"] == "STARTED"


def test_settlement_is_terminal_and_exactly_once(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1)
    admit(store, policy, "TOKEN", "token:tick0")
    settled = store.settle_cadence_request(
        ROLE, policy_version=policy.policy_version, scheduled_for_utc=SLOT,
        native_identity="token:tick0", outcome="SUCCEEDED", now=NOW,
    )
    assert settled["state"] == "SUCCEEDED"
    assert store.settle_cadence_request(
        ROLE, policy_version=policy.policy_version, scheduled_for_utc=SLOT,
        native_identity="token:tick0", outcome="SUCCEEDED", now=NOW,
    ) is ReplayResult.DUPLICATE
    with pytest.raises(CadenceTransitionError):
        store.settle_cadence_request(
            ROLE, policy_version=policy.policy_version, scheduled_for_utc=SLOT,
            native_identity="token:tick0", outcome="FAILED", now=NOW,
        )
    # A settled row still consumed its wire start for admission counting.
    assert store.read_cadence_tick_report(
        policy.policy_version, SLOT
    )["total_started"] == 1


def test_settlement_outcomes_are_closed_and_require_started(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1)
    plan(store, policy, "TOKEN", "token:tick0")
    with pytest.raises(CadenceTransitionError):
        store.settle_cadence_request(
            ROLE, policy_version=policy.policy_version, scheduled_for_utc=SLOT,
            native_identity="token:tick0", outcome="SUCCEEDED", now=NOW,
        )
    start(store, policy, "TOKEN", "token:tick0")
    with pytest.raises(CadenceTransitionError):
        store.settle_cadence_request(
            ROLE, policy_version=policy.policy_version, scheduled_for_utc=SLOT,
            native_identity="token:tick0", outcome="OK", now=NOW,
        )


def test_reopen_reconciliation_settles_stale_started_to_unknown(tmp_path):
    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1, holdings_starts=1)
    admit(store, policy, "TOKEN", "token:tick0")

    reopened = OpsKernelStore(store.path)
    result = reopened.reconcile_cadence(ROLE, now=NOW + timedelta(seconds=30))
    assert [row["native_identity"] for row in result["wire_starts"]] == ["token:tick0"]
    assert all(row["state"] == "UNKNOWN" for row in result["wire_starts"])
    report = reopened.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["total_started"] == 1  # the consumed start never un-counts


def test_policy_digest_mismatch_is_rejected_and_pinned_snapshot_survives(tmp_path):
    from tokmo.values.ops_kernel import CadencePolicyMismatchError

    store = make_store(tmp_path)
    first = custom_policy(token_starts=1, calendar_starts=2)
    admit(store, first, "TOKEN", "token:tick0")

    drifted = replace(
        first, request_limits=replace(first.request_limits, calendar_starts=1),
    )
    with pytest.raises(CadencePolicyMismatchError):
        admit(store, drifted, "CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0")

    report = store.read_cadence_tick_report(first.policy_version, SLOT)
    assert report["snapshot"]["request_limits"]["calendar_starts"] == (
        first.request_limits.calendar_starts
    )
    assert report["total_started"] == 1  # the drifted process admitted nothing

    next_slot = SLOT + timedelta(minutes=5)
    assert admit(
        store, drifted, "TOKEN", "token:tick0", slot=next_slot,
        now=next_slot + timedelta(seconds=3),
    )["state"] == "STARTED"
    next_report = store.read_cadence_tick_report(first.policy_version, next_slot)
    assert next_report["snapshot"]["request_limits"]["calendar_starts"] == 1


def test_group_exact_limit_admits_and_group_n_plus_one_rejects_below_total(tmp_path):
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    store = make_store(tmp_path)
    policy = custom_policy(total_wire_starts=9, token_starts=1, calendar_starts=2,
                           holdings_starts=2, quote_starts=0, trades_starts=0)
    limit = policy.request_limits.calendar_starts
    identities = (
        ("CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0"),
        ("CALENDAR_DAY_US", "calendar:US:2026-08-01:0"),
        ("CALENDAR_DAY_KR", "calendar:KR:2026-08-01:1"),
    )
    for operation, identity in identities[:limit]:
        admit(store, policy, operation, identity)
    report = store.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["group_started"]["CALENDAR"] == limit
    assert report["total_started"] < policy.request_limits.total_wire_starts

    with pytest.raises(CadenceBudgetExceeded):
        admit(store, policy, *identities[limit])
    assert store.read_cadence_tick_report(
        policy.policy_version, SLOT
    )["group_started"]["CALENDAR"] == limit


def test_total_backstop_rejects_n_plus_one_even_with_group_room(tmp_path):
    """The total ceiling is a final causal backstop: even if a (mis)declared
    policy leaves group room beyond the total, the durable gate still stops."""
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    store = make_store(tmp_path)
    limits = TickRequestLimitsV1(total_wire_starts=2, token_starts=1,
                                 calendar_starts=1, holdings_starts=0,
                                 quote_starts=0, trades_starts=0)
    # Simulate a bypassed declaration invariant (frozen-dataclass backdoor):
    # holdings gains room the total does not cover.
    object.__setattr__(limits, "holdings_starts", 1)
    policy = custom_policy()
    object.__setattr__(policy, "request_limits", limits)

    admit(store, policy, "TOKEN", "token:tick0")
    admit(store, policy, "CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0")
    with pytest.raises(CadenceBudgetExceeded):
        admit(store, policy, "ACCOUNT_HOLDINGS", "holdings:1")
    assert store.read_cadence_tick_report(
        policy.policy_version, SLOT
    )["total_started"] == policy.request_limits.total_wire_starts


def test_trades_children_checked_atomically_with_parent_and_total(tmp_path):
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    store = make_store(tmp_path)
    child_limits = TradesChildLimitsV1(orders_page_starts=2, order_detail_starts=1)
    policy = custom_policy(total_wire_starts=9, token_starts=0, calendar_starts=0,
                           holdings_starts=0, quote_starts=0, trades_starts=3)

    for page in range(child_limits.orders_page_starts):
        admit(store, policy, "ORDERS_PAGE", f"orders:{page}",
              child_limits=child_limits)
    with pytest.raises(CadenceBudgetExceeded):
        admit(store, policy, "ORDERS_PAGE", "orders:9", child_limits=child_limits)

    admit(store, policy, "ORDER_DETAIL", "detail:0", child_limits=child_limits)
    with pytest.raises(CadenceBudgetExceeded):
        admit(store, policy, "ORDER_DETAIL", "detail:1", child_limits=child_limits)

    report = store.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["group_started"]["TRADES"] == policy.request_limits.trades_starts
    # The parent trades gate is checked in the same transaction: any further
    # trades-group start is rejected at the group ceiling.
    with pytest.raises(CadenceBudgetExceeded):
        admit(store, policy, "ORDERS_PAGE", "orders:8", child_limits=child_limits)


def test_duplicate_native_identity_is_refused_with_wire_zero(tmp_path):
    from tokmo.values.ops_kernel import CadenceDuplicateError

    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1, calendar_starts=2)
    admit(store, policy, "CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0")
    with pytest.raises(CadenceDuplicateError):
        admit(store, policy, "CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0")
    assert raw_counts(store) == (1, 1)


def test_two_concurrent_contenders_for_final_group_slot_admit_exactly_one(tmp_path):
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    policy = custom_policy(token_starts=0, calendar_starts=1)
    seed = OpsKernelStore(path)
    contenders = (
        ("CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0"),
        ("CALENDAR_DAY_US", "calendar:US:2026-08-01:0"),
    )
    for operation, identity in contenders:
        plan(seed, policy, operation, identity)

    def contend(request):
        operation, identity = request
        store = OpsKernelStore(path)
        try:
            return start(store, policy, operation, identity)
        except CadenceBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contend, contenders))
    admitted = [outcome for outcome in outcomes if outcome is not None]
    assert len(admitted) == 1
    report = OpsKernelStore(path).read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["group_started"]["CALENDAR"] == policy.request_limits.calendar_starts


def test_two_concurrent_contenders_for_final_total_slot_admit_exactly_one(tmp_path):
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    limits = TickRequestLimitsV1(total_wire_starts=1, token_starts=0,
                                 calendar_starts=1, holdings_starts=0,
                                 quote_starts=0, trades_starts=0)
    object.__setattr__(limits, "holdings_starts", 1)
    policy = custom_policy()
    object.__setattr__(policy, "request_limits", limits)
    seed = OpsKernelStore(path)
    contenders = (
        ("CALENDAR_DAY_KR", "calendar:KR:2026-08-01:0"),
        ("ACCOUNT_HOLDINGS", "holdings:1"),
    )
    for operation, identity in contenders:
        plan(seed, policy, operation, identity)

    def contend(request):
        operation, identity = request
        store = OpsKernelStore(path)
        try:
            return start(store, policy, operation, identity)
        except CadenceBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(contend, contenders))
    assert len([outcome for outcome in outcomes if outcome is not None]) == 1
    report = OpsKernelStore(path).read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["total_started"] == policy.request_limits.total_wire_starts


def test_pinned_or_planned_closed_tick_reserves_no_capacity_as_calls(tmp_path):
    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1)
    store.pin_cadence_tick(ROLE, policy, scheduled_for_utc=SLOT, now=NOW,
                           child_limits=NO_CHILDREN)
    report = store.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["total_started"] == 0
    assert all(count == 0 for count in report["group_started"].values())

    # A durable PLANNED row is not a call either.
    plan(store, policy, "TOKEN", "token:tick0")
    report = store.read_cadence_tick_report(policy.policy_version, SLOT)
    assert report["total_started"] == 0


def test_distinct_policy_versions_have_non_colliding_tick_keys(tmp_path):
    store = make_store(tmp_path)
    old = custom_policy(version="cadence-test-300s", token_starts=1)
    new = custom_policy(version="cadence-test-60s", token_starts=1)
    admit(store, old, "TOKEN", "token:tick0")
    admit(store, new, "TOKEN", "token:tick0")
    assert store.read_cadence_tick_report(old.policy_version, SLOT)["total_started"] == 1
    assert store.read_cadence_tick_report(new.policy_version, SLOT)["total_started"] == 1


def test_cadence_admission_shares_the_production_kernel_db_and_authority(tmp_path):
    """B1: no parallel authority ledger — the cadence tables live in the same
    SQLite file as production planned requests, under the same store."""
    store = make_store(tmp_path)
    policy = custom_policy(token_starts=1)
    admit(store, policy, "TOKEN", "token:tick0")
    connection = sqlite3.connect(store.path)
    try:
        tables = {
            row[0] for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='table'"
            )
        }
    finally:
        connection.close()
    assert {"cadence_ticks", "cadence_wire_starts", "planned_requests"} <= tables
    # The standalone parallel-store module is gone.
    with pytest.raises(ModuleNotFoundError):
        import tokmo.records.cadence_admission  # noqa: F401
