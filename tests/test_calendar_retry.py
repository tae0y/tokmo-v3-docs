"""Stage 1-D: canonical calendar retry identity and idempotency (SDS §4).

The retry ledger lives inside the production ``OpsKernelStore`` — the same
SQLite DB and transactional authority as every other wire start. Planned slot
identity is (market, market_local_date, eligible_slot_utc); the wire identity
is (market, market_local_date, ordinal 0..5) with the ordinal allocated
atomically only at STARTED. Expectations for delays and attempt counts are
derived from the central policy declaration, never re-typed.
"""

from datetime import date, datetime, timedelta, timezone
import sqlite3

import pytest

from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.cadence_policy import (
    CALENDAR_ATTEMPT_MAX,
    CALENDAR_RETRY_DELAY_MINUTES,
    CadencePolicyV1,
    TickRequestLimitsV1,
    TradesChildLimitsV1,
)
from tokmo.values.ops_kernel import (
    CalendarStartReceiptV1,
    ProducerRole,
    ReplayResult,
)
from tokmo.values.session_gate import Market

LOCAL_DATE = date(2026, 8, 3)
BASE = datetime(2026, 8, 2, 20, 0, tzinfo=timezone.utc)
ROLE = ProducerRole.COLLECTOR
CHILD_LIMITS = TradesChildLimitsV1(orders_page_starts=0, order_detail_starts=0)


def policy():
    return CadencePolicyV1(
        policy_version="cadence-test-v1", cadence_seconds=60,
        max_start_lag_seconds=10, child_timeout_seconds=40,
        termination_margin_seconds=10,
        request_limits=TickRequestLimitsV1(
            total_wire_starts=27, token_starts=0, calendar_starts=2,
            holdings_starts=0, quote_starts=0, trades_starts=0,
        ),
    )


def make_store(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")


def slot_at(minutes):
    return BASE + timedelta(minutes=minutes)


def plan(store, minutes, *, market=Market.KR, local_date=LOCAL_DATE):
    return store.plan_calendar_slot(
        ROLE, market, local_date, slot_at(minutes), now=slot_at(minutes),
    )


def start(store, minutes, *, market=Market.KR, local_date=LOCAL_DATE, now=None):
    return store.start_calendar_attempt(
        ROLE, policy(), scheduled_for_utc=slot_at(minutes), market=market,
        market_local_date=local_date, eligible_slot_utc=slot_at(minutes),
        now=now or slot_at(minutes), child_limits=CHILD_LIMITS,
    )


def settle(store, ordinal, outcome, minutes, *, market=Market.KR, local_date=LOCAL_DATE):
    return store.settle_calendar_attempt(
        ROLE, market, local_date, ordinal, outcome, now=slot_at(minutes),
    )


def wire_rows(store):
    connection = sqlite3.connect(store.path)
    try:
        return connection.execute(
            "SELECT operation_group,native_identity FROM cadence_wire_starts "
            "WHERE state<>'PLANNED' ORDER BY started_at"
        ).fetchall()
    finally:
        connection.close()


def test_duplicate_planned_slot_resolves_to_same_row_with_wire_zero(tmp_path):
    store = make_store(tmp_path)
    assert plan(store, 0) is ReplayResult.CREATED
    assert plan(store, 0) is ReplayResult.DUPLICATE
    assert wire_rows(store) == []


def test_unstarted_slot_consumes_no_ordinal_and_start_binds_wire_identity(tmp_path):
    store = make_store(tmp_path)
    plan(store, 0)   # cancelled/unstarted slot: consumes nothing
    plan(store, 5)
    started = start(store, 5)
    assert isinstance(started, CalendarStartReceiptV1)
    assert started.attempt_ordinal == 0
    assert started.market is Market.KR
    assert started.state == "STARTED"
    assert started.policy_version == policy().policy_version
    rows = wire_rows(store)
    assert len(rows) == 1
    assert rows[0][0] == "CALENDAR"
    assert rows[0][1] == "calendar:KR:2026-08-03:0"


def test_started_slot_duplicate_invocation_performs_wire_zero(tmp_path):
    from tokmo.values.ops_kernel import CadenceDuplicateError

    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    with pytest.raises(CadenceDuplicateError):
        start(store, 0)
    assert len(wire_rows(store)) == 1


def test_backoff_deadline_gates_next_ordinal_and_delays_come_from_policy(tmp_path):
    from tokmo.values.ops_kernel import CadenceCalendarBackoffError

    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    settle(store, 0, "FAILED", 1)

    first_delay = CALENDAR_RETRY_DELAY_MINUTES[0]
    too_early = 1 + first_delay - 1
    plan(store, too_early)
    with pytest.raises(CadenceCalendarBackoffError):
        start(store, too_early)
    assert len(wire_rows(store)) == 1

    on_time = 1 + first_delay
    plan(store, on_time)
    assert start(store, on_time).attempt_ordinal == 1


def test_utc_day_rollover_never_resets_attempt_state(tmp_path):
    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    settle(store, 0, "FAILED", 1)  # 20:01Z

    # Cross UTC midnight (BASE+241min = 00:01Z next UTC day); the market-local
    # date is unchanged, so the identity and its ordinal sequence persist.
    after_midnight = 241
    assert slot_at(after_midnight).date() > BASE.date()
    plan(store, after_midnight)
    started = start(store, after_midnight)
    assert started.attempt_ordinal == 1  # continued, not reset to 0


def test_success_closes_identity_for_every_later_slot(tmp_path):
    from tokmo.values.ops_kernel import CadenceCalendarClosedError

    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    settle(store, 0, "SUCCEEDED", 1)

    plan(store, 10)
    with pytest.raises(CadenceCalendarClosedError):
        start(store, 10)
    assert len(wire_rows(store)) == 1


def test_unknown_settlement_consumes_ordinal_and_never_restarts_it(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    settle(store, 0, "UNKNOWN", 1)

    # Reconciled UNKNOWN is terminal: replay of the same outcome is a
    # duplicate; a conflicting rewrite is refused.
    assert settle(store, 0, "UNKNOWN", 2) is ReplayResult.DUPLICATE
    with pytest.raises(CadenceTransitionError):
        settle(store, 0, "SUCCEEDED", 3)

    next_minutes = 1 + CALENDAR_RETRY_DELAY_MINUTES[0]
    plan(store, next_minutes)
    assert start(store, next_minutes).attempt_ordinal == 1


def test_crash_after_started_reconciles_same_ordinal_unknown_then_next(tmp_path):
    """M4: crash after STARTED -> reopen -> reconcile -> same ordinal UNKNOWN
    -> normal backoff -> ordinal N+1. The stale ordinal is never restarted."""
    from tokmo.values.ops_kernel import CadenceCalendarBackoffError

    store = make_store(tmp_path)
    plan(store, 0)
    receipt = start(store, 0)
    assert receipt.attempt_ordinal == 0
    del store  # the crashed process never settles

    reopened = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3")
    result = reopened.reconcile_cadence(ROLE, now=slot_at(1))
    settled = result["calendar_attempts"]
    assert len(settled) == 1
    assert settled[0]["market"] is Market.KR
    assert settled[0]["market_local_date"] == LOCAL_DATE
    assert settled[0]["attempt_ordinal"] == 0  # the same consumed ordinal
    assert settled[0]["state"] == "UNKNOWN"
    assert all(row["state"] == "UNKNOWN" for row in result["wire_starts"])

    # A second reconciliation finds nothing: the settlement is terminal.
    again = reopened.reconcile_cadence(ROLE, now=slot_at(2))
    assert again["calendar_attempts"] == ()

    # Backoff still applies before ordinal 1 may start.
    too_early = 2
    plan(reopened, too_early)
    with pytest.raises(CadenceCalendarBackoffError):
        start(reopened, too_early)
    assert len(wire_rows(reopened)) == 1

    on_time = 1 + CALENDAR_RETRY_DELAY_MINUTES[0]
    plan(reopened, on_time)
    next_receipt = start(reopened, on_time)
    assert next_receipt.attempt_ordinal == 1
    connection = sqlite3.connect(reopened.path)
    try:
        states = dict(connection.execute(
            "SELECT attempt_ordinal,state FROM cadence_calendar_attempts "
            "ORDER BY attempt_ordinal"
        ).fetchall())
    finally:
        connection.close()
    assert states == {0: "UNKNOWN", 1: "STARTED"}


def test_sixth_exhaustion_holds_until_next_market_local_date(tmp_path):
    from tokmo.values.ops_kernel import CadenceCalendarExhaustedError

    store = make_store(tmp_path)
    minutes = 0
    for ordinal in range(CALENDAR_ATTEMPT_MAX):
        plan(store, minutes)
        assert start(store, minutes).attempt_ordinal == ordinal
        settle(store, ordinal, "FAILED", minutes + 1)
        if ordinal < len(CALENDAR_RETRY_DELAY_MINUTES):
            minutes += 1 + CALENDAR_RETRY_DELAY_MINUTES[ordinal]

    plan(store, minutes + 500)
    with pytest.raises(CadenceCalendarExhaustedError):
        start(store, minutes + 500)
    assert len(wire_rows(store)) == CALENDAR_ATTEMPT_MAX

    # The next market-local date is a fresh identity with a fresh ordinal 0.
    next_date = date(2026, 8, 4)
    plan(store, minutes + 600, local_date=next_date)
    assert start(store, minutes + 600, local_date=next_date).attempt_ordinal == 0


def test_calendar_start_requires_versioned_tick_binding(tmp_path):
    from tokmo.values.ops_kernel import CadenceBindingError

    store = make_store(tmp_path)
    plan(store, 0)
    with pytest.raises(CadenceBindingError):
        store.start_calendar_attempt(
            ROLE, None, scheduled_for_utc=slot_at(0), market=Market.KR,
            market_local_date=LOCAL_DATE, eligible_slot_utc=slot_at(0),
            now=slot_at(0), child_limits=CHILD_LIMITS,
        )
    with pytest.raises(CadenceBindingError):
        store.start_calendar_attempt(
            ROLE, policy(), scheduled_for_utc=None, market=Market.KR,
            market_local_date=LOCAL_DATE, eligible_slot_utc=slot_at(0),
            now=slot_at(0), child_limits=CHILD_LIMITS,
        )
    assert wire_rows(store) == []
    connection = sqlite3.connect(store.path)
    try:
        state = connection.execute(
            "SELECT state,attempt_ordinal FROM cadence_calendar_slots"
        ).fetchone()
        attempts = connection.execute(
            "SELECT count(*) FROM cadence_calendar_attempts"
        ).fetchone()[0]
    finally:
        connection.close()
    assert tuple(state) == ("PLANNED", None)
    assert attempts == 0


def test_calendar_retry_identity_is_not_an_admission_bypass(tmp_path):
    """B1/B2: the calendar group consumes the same per-tick group/total count
    as every other wire start; native retry identity never bypasses it."""
    from tokmo.values.ops_kernel import CadenceBudgetExceeded

    store = make_store(tmp_path)
    limit = policy().request_limits.calendar_starts
    # All starts in the SAME cadence tick slot: distinct markets/dates supply
    # distinct native identities, yet the CALENDAR group ceiling still gates.
    dates = (LOCAL_DATE, date(2026, 8, 4), date(2026, 8, 5))
    for local_date in dates[:limit]:
        plan(store, 0, local_date=local_date)
        start(store, 0, local_date=local_date)
    plan(store, 0, local_date=dates[limit])
    with pytest.raises(CadenceBudgetExceeded):
        start(store, 0, local_date=dates[limit])
    assert len(wire_rows(store)) == limit


def test_unreconciled_started_attempt_blocks_new_ordinal(tmp_path):
    from tokmo.values.ops_kernel import CadenceTransitionError

    store = make_store(tmp_path)
    plan(store, 0)
    start(store, 0)
    plan(store, 10)
    with pytest.raises(CadenceTransitionError):
        start(store, 10)
    assert len(wire_rows(store)) == 1
