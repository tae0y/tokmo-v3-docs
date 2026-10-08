"""Stage 3-B: the one mandatory production cadence wire executor.

Every provider callback runs only under a durably PLANNED -> STARTED cadence
wire row admitted against the exact pinned ``CadenceTickV1`` policy, and every
started row settles exactly once (SUCCEEDED/FAILED/UNKNOWN). Over-limit,
duplicate, unplanned, or mismatched requests perform callback zero. Claims are
executor-issued only, live only for the duration of the callback, and the
in-memory bearer is acquired lazily as an admitted TOKEN wire.
"""

from datetime import datetime, timezone

import pytest

from tokmo.programs.cadence_wire import (
    CadenceWireExecutorV1,
    LazyWireTokenV1,
    WireClaimV1,
    WireOutcomeUnknownError,
    require_started_claim,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CADENCE_POLICY_V1,
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)
from tokmo.values.ops_kernel import (
    CadenceBudgetExceeded,
    CadenceDuplicateError,
    CadenceTransitionError,
    ProducerRole,
)
from tokmo.values.operation_lane_registry import (
    LANE_GOVERNED_OPERATIONS,
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
    OperationLaneRegistryV1,
    OperationLaneV1,
    OperationOwnershipError,
)

SLOT = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
NOW = datetime(2026, 8, 3, 14, 0, 1, tzinfo=timezone.utc)


def make_executor(tmp_path, policy=CADENCE_POLICY_V1, child_limits=None):
    store = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")
    executor = CadenceWireExecutorV1(
        ops_kernel=store, role=ProducerRole.COLLECTOR, policy=policy,
        scheduled_for_utc=SLOT, clock=lambda: NOW,
        child_limits=TradesChildLimitsV1() if child_limits is None else child_limits,
        lane_registry=OPERATION_LANE_REGISTRY_V1,
        lane=OperationLane.CADENCE_300S,
    )
    return store, executor


def test_execute_rejects_post_cutover_provider_before_kernel_or_callback(tmp_path):
    cutover = int(SLOT.timestamp()) - int(SLOT.timestamp()) % 300
    governed = {"PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"}
    registry = OperationLaneRegistryV1(entries={
        operation: OperationLaneV1(
            operation,
            OperationLane.PULSE_60S if operation in governed
            else OperationLane.CADENCE_300S,
            OperationLane.CADENCE_300S if operation in governed else None,
            cutover if operation in governed else None,
        )
        for operation in LANE_GOVERNED_OPERATIONS
    })
    store = OpsKernelStore(tmp_path / "ops_kernel.sqlite3")
    executor = CadenceWireExecutorV1(
        ops_kernel=store, role=ProducerRole.COLLECTOR,
        policy=CADENCE_POLICY_V1, scheduled_for_utc=SLOT,
        clock=lambda: NOW, child_limits=TradesChildLimitsV1(),
        lane_registry=registry, lane=OperationLane.CADENCE_300S,
    )
    calls = []

    with pytest.raises(OperationOwnershipError):
        executor.execute(
            "PRICE_BATCH", "quotes:0", lambda claim: calls.append(claim))

    assert calls == []
    assert report(store) is None


def report(store, policy_version=CADENCE_POLICY_V1.policy_version):
    return store.read_cadence_tick_report(policy_version, SLOT)


# -- claims are executor-issued, operation-exact, and one-time ---------------------


def test_wire_claim_cannot_be_constructed_outside_the_executor():
    with pytest.raises(PermissionError):
        WireClaimV1(
            CADENCE_POLICY_V1.policy_version, SLOT, "TOKEN", "TOKEN", "token:tick0",
        )


def test_callback_receives_a_live_claim_that_dies_after_settlement(tmp_path):
    _store, executor = make_executor(tmp_path)
    seen = {}

    def callback(claim):
        require_started_claim(claim, "TOKEN")
        seen["claim"] = claim
        return "bearer"

    assert executor.execute("TOKEN", "token:tick0", callback) == "bearer"
    with pytest.raises(PermissionError):
        require_started_claim(seen["claim"], "TOKEN")


def test_require_started_claim_rejects_forged_and_wrong_operation_claims(tmp_path):
    _store, executor = make_executor(tmp_path)

    class Forged:
        operation = "TOKEN"
        native_identity = "token:tick0"

    with pytest.raises(PermissionError):
        require_started_claim(Forged(), "TOKEN")
    with pytest.raises(PermissionError):
        require_started_claim(None, "TOKEN")

    def callback(claim):
        with pytest.raises(PermissionError):
            require_started_claim(claim, "ACCOUNTS")
        return require_started_claim(claim, "TOKEN")

    executor.execute("TOKEN", "token:tick0", callback)


# -- plan/start precede the callback; settlement is exactly once -------------------


def test_execute_plans_starts_settles_succeeded_around_the_callback(tmp_path):
    store, executor = make_executor(tmp_path)
    value = executor.execute(
        "ACCOUNTS", "accounts:tick0", lambda claim: "accounts-page",
    )
    assert value == "accounts-page"
    result = report(store)
    assert result["operation_started"] == {"ACCOUNTS": 1}
    assert result["state_counts"] == {"SUCCEEDED": 1}


def test_callback_exception_settles_failed_and_reraises(tmp_path):
    store, executor = make_executor(tmp_path)
    with pytest.raises(RuntimeError):
        executor.execute(
            "PRICE_BATCH", "quotes:0",
            lambda claim: (_ for _ in ()).throw(RuntimeError("boom")),
        )
    assert report(store)["state_counts"] == {"FAILED": 1}


def test_unknown_outcome_settles_unknown_and_reraises(tmp_path):
    store, executor = make_executor(tmp_path)
    with pytest.raises(WireOutcomeUnknownError):
        executor.execute(
            "ORDERS_PAGE", "orders:0",
            lambda claim: (_ for _ in ()).throw(WireOutcomeUnknownError()),
        )
    assert report(store)["state_counts"] == {"UNKNOWN": 1}


def test_duplicate_identity_performs_callback_zero(tmp_path):
    store, executor = make_executor(tmp_path)
    executor.execute("TOKEN", "token:tick0", lambda claim: "bearer")
    calls = []
    with pytest.raises(CadenceDuplicateError):
        executor.execute("TOKEN", "token:tick0", lambda claim: calls.append(1))
    assert calls == []
    assert report(store)["operation_started"] == {"TOKEN": 1}


def test_unknown_operation_and_noncanonical_identity_reject_before_callback(tmp_path):
    store, executor = make_executor(tmp_path)
    calls = []
    with pytest.raises(ValueError):
        executor.execute("ORDER_WRITE", "orders:0", lambda claim: calls.append(1))
    with pytest.raises(ValueError):
        executor.execute("ORDERS_PAGE", "orders:99", lambda claim: calls.append(1))
    assert calls == []
    assert report(store) is None or report(store)["total_started"] == 0


# -- E4: custom tiny policy rejects over-limit before the callback -----------------


TINY = CadencePolicyV1(
    request_limits=TickRequestLimitsV1(
        total_wire_starts=3, token_starts=1, calendar_starts=0,
        holdings_starts=1, quote_starts=1, trades_starts=0,
    ),
)


def test_group_ceiling_from_injected_tiny_policy_rejects_before_callback(tmp_path):
    store, executor = make_executor(
        tmp_path, policy=TINY,
        child_limits=TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0),
    )
    executor.execute("TOKEN", "token:tick0", lambda claim: "bearer")
    calls = []
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute(
            "CALENDAR_DAY_KR", "calendar:KR:2026-08-03:0",
            lambda claim: calls.append(1),
        )
    assert calls == []
    result = report(store)
    assert result["operation_started"] == {"TOKEN": 1}


def test_total_ceiling_rejects_nplus1_even_when_groups_have_room(tmp_path):
    limits = TickRequestLimitsV1(
        total_wire_starts=2, token_starts=0, calendar_starts=0,
        holdings_starts=2, quote_starts=0, trades_starts=0,
    )
    # Simulate a bypassed declaration invariant (frozen-dataclass backdoor)
    # exactly as the kernel admission tests do: the quotes group gains room
    # the total does not cover, and the durable total backstop still stops
    # before the callback.
    object.__setattr__(limits, "quote_starts", 4)
    tiny = CadencePolicyV1()
    object.__setattr__(tiny, "request_limits", limits)
    store, executor = make_executor(
        tmp_path, policy=tiny,
        child_limits=TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0),
    )
    executor.execute("ACCOUNTS", "accounts:tick0", lambda claim: None)
    executor.execute("ACCOUNT_HOLDINGS", "holdings:1", lambda claim: None)
    calls = []
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute("PRICE_BATCH", "quotes:0", lambda claim: calls.append(1))
    assert calls == []
    assert report(store)["total_started"] == 2


def test_trades_child_ceiling_rejects_before_callback(tmp_path):
    store, executor = make_executor(
        tmp_path,
        child_limits=TradesChildLimitsV1(orders_page_starts=1, order_detail_starts=1),
    )
    executor.execute("ORDERS_PAGE", "orders:0", lambda claim: None)
    calls = []
    with pytest.raises(CadenceBudgetExceeded):
        executor.execute("ORDERS_PAGE", "orders:1", lambda claim: calls.append(1))
    assert calls == []
    assert report(store)["operation_started"] == {"ORDERS_PAGE": 1}


# -- E3: exact provider wire call counts equal STARTED receipts --------------------


def test_started_receipts_equal_actual_callback_invocations_exactly(tmp_path):
    store, executor = make_executor(tmp_path)
    invocations = {"n": 0}

    def counted(claim):
        invocations["n"] += 1
        return invocations["n"]

    executor.execute("TOKEN", "token:tick0", counted)
    executor.execute("ACCOUNTS", "accounts:tick0", counted)
    executor.execute("ACCOUNT_HOLDINGS", "holdings:1", counted)
    executor.execute("PRICE_BATCH", "quotes:0", counted)
    executor.execute("ORDERS_PAGE", "orders:0", counted)
    executor.execute("ORDER_DETAIL", "detail:ord-1", counted)
    result = report(store)
    assert result["total_started"] == invocations["n"] == 6
    assert result["state_counts"] == {"SUCCEEDED": 6}


# -- crash recovery: stale STARTED rows reconcile to UNKNOWN, never restart --------


def test_reconcile_settles_stale_started_rows_without_restart(tmp_path):
    store, executor = make_executor(tmp_path)

    class Crash(BaseException):
        pass

    with pytest.raises(Crash):
        executor.execute(
            "ACCOUNTS", "accounts:tick0",
            lambda claim: (_ for _ in ()).throw(Crash()),
        )
    # A BaseException that is not an Exception models a hard kill between
    # start and settle: the executor must NOT have settled it as FAILED.
    # (KeyboardInterrupt-style aborts leave the durable row STARTED.)
    assert report(store)["state_counts"] == {"STARTED": 1}
    reconciled = store.reconcile_cadence(ProducerRole.COLLECTOR, now=NOW)
    assert [row["state"] for row in reconciled["wire_starts"]] == ["UNKNOWN"]
    calls = []
    with pytest.raises(CadenceDuplicateError):
        executor.execute("ACCOUNTS", "accounts:tick0", lambda claim: calls.append(1))
    assert calls == []


# -- lazy TOKEN wire ---------------------------------------------------------------


def test_lazy_token_acquires_once_through_the_executor_and_masks_repr(tmp_path):
    store, executor = make_executor(tmp_path)
    fetches = []

    def token_fetch(claim):
        require_started_claim(claim, "TOKEN")
        fetches.append(1)
        return "secret-bearer-string"

    token = LazyWireTokenV1(executor=executor, token_fetch=token_fetch)
    assert report(store) is None or report(store).get("total_started", 0) == 0
    assert token.bearer() == "secret-bearer-string"
    assert token.bearer() == "secret-bearer-string"
    assert fetches == [1]
    assert report(store)["operation_started"] == {"TOKEN": 1}
    assert "secret-bearer-string" not in repr(token)
    assert "secret-bearer-string" not in str(token)


def test_lazy_token_rejects_a_non_string_bearer(tmp_path):
    _store, executor = make_executor(tmp_path)
    token = LazyWireTokenV1(executor=executor, token_fetch=lambda claim: None)
    with pytest.raises(ValueError):
        token.bearer()


def test_lazy_token_failure_settles_failed_and_is_not_cached(tmp_path):
    store, executor = make_executor(tmp_path)
    token = LazyWireTokenV1(
        executor=executor,
        token_fetch=lambda claim: (_ for _ in ()).throw(RuntimeError("down")),
    )
    with pytest.raises(RuntimeError):
        token.bearer()
    assert report(store)["state_counts"] == {"FAILED": 1}
