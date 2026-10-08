"""Private, terminal-by-D Quant recurring receipt state machine."""

from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
import os
import sqlite3

import pytest


NOW = datetime(2026, 8, 14, 20, 0, tzinfo=timezone.utc)


def _identity():
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_multifactor_core import MarketV1
    from tokmo.values.quant_recurring import (
        QUANT_RECURRING_PROGRAM_ID,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
        QuantDueIdentityV1,
    )

    return QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        CADENCE_QUANT_SHADOW_POLICY_V1.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        date(2026, 8, 14),
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )


def _result(identity_digest):
    from tokmo.records.quant_recurring_receipts import QuantRecurringResultV1

    return QuantRecurringResultV1(
        identity_digest=identity_digest,
        acquisition_id="official-membership-recurring-v1:2026-08-14:" + "a" * 32,
        membership_holdings_digest="b" * 64,
        holdings_captured_at=NOW,
        membership_starts=7,
        candle_page1_starts=0,
        candle_page2_starts=0,
        cohort_size=4,
        admissible_count=0,
        provenance_digest="c" * 64,
        pack_digest="",
        degraded=False,
        limitations=("COHORT_MISMATCH",),
        symbols=("AAPL", "MSFT", "NVDA", "TSLA"),
    )


def _claimed_store(tmp_path):
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore

    store = QuantRecurringReceiptStore(tmp_path / "ops" / "quant_recurring.sqlite3")
    identity = _identity()
    lease = store.acquire_identity_lease(identity.identity_digest)
    assert lease is not None
    store.claim(
        identity,
        owner_invocation_id="invocation-1",
        owner_lease_key=lease.key,
        claimed_at=NOW,
        identity_lease=lease,
    )
    return store, identity, lease


def test_receipt_store_has_exact_strict_private_schema_and_nonblocking_identity_lock(tmp_path):
    from tokmo.records.private_sqlite import assert_no_sidecars_after_close
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore

    path = tmp_path / "ops" / "quant_recurring.sqlite3"
    store = QuantRecurringReceiptStore(path)
    identity = _identity()
    first = store.acquire_identity_lease(identity.identity_digest)
    assert first is not None
    assert store.acquire_identity_lease(identity.identity_digest) is None
    first.close()
    second = store.acquire_identity_lease(identity.identity_digest)
    assert second is not None
    second.close()

    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (1,)
        strict = {
            row[1]: row[5]
            for row in connection.execute("PRAGMA table_list")
            if row[1].startswith("quant_recurring_")
        }
        assert strict == {
            "quant_recurring_due": 1,
            "quant_recurring_result": 1,
        }
    finally:
        connection.close()
    assert (os.stat(path).st_mode & 0o777) == 0o600
    assert (os.stat(path.parent).st_mode & 0o777) == 0o700
    assert assert_no_sidecars_after_close(path) == []


def test_terminal_transition_is_single_atomic_and_same_identity_is_never_reclaimed(tmp_path):
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store, identity, lease = _claimed_store(tmp_path)
    try:
        store.finish(
            identity.identity_digest,
            state=QuantTerminalStateV1.NO_PACK,
            reason_code="cohort_mismatch",
            finished_at=NOW + timedelta(seconds=1),
            result=_result(identity.identity_digest),
        )
        due = store.read_due(identity.identity_digest)
        assert due.state is QuantTerminalStateV1.NO_PACK
        assert store.read_result(identity.identity_digest).symbols == (
            "AAPL", "MSFT", "NVDA", "TSLA",
        )
        with pytest.raises(ValueError, match="terminal"):
            store.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.FAILED,
                reason_code="again",
                finished_at=NOW + timedelta(seconds=2),
            )
        assert store.claim(
            identity,
            owner_invocation_id="invocation-1",
            owner_lease_key=lease.key,
            claimed_at=NOW + timedelta(seconds=3),
            identity_lease=lease,
        ).state is QuantTerminalStateV1.NO_PACK
    finally:
        lease.close()


def test_result_constraint_failure_rolls_back_result_and_terminal_update(tmp_path):
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store, identity, lease = _claimed_store(tmp_path)
    try:
        invalid = replace(_result(identity.identity_digest), membership_starts=8)
        with pytest.raises(sqlite3.IntegrityError):
            store.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.NO_PACK,
                reason_code="invalid",
                finished_at=NOW + timedelta(seconds=1),
                result=invalid,
            )
        assert store.read_due(identity.identity_digest).state is QuantTerminalStateV1.CLAIMED
        assert store.read_result(identity.identity_digest) is None
    finally:
        lease.close()


def test_membership_plan_receipt_constraint_rolls_back_whole_transition(tmp_path):
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store, identity, lease = _claimed_store(tmp_path)
    try:
        invalid = replace(
            _result(identity.identity_digest),
            cohort_size=3,
            symbols=("AAPL", "MSFT", "NVDA"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            store.finish(
                identity.identity_digest,
                state=QuantTerminalStateV1.NO_PACK,
                reason_code="invalid_plan_count",
                finished_at=NOW + timedelta(seconds=1),
                result=invalid,
            )
        assert store.read_due(identity.identity_digest).state is (
            QuantTerminalStateV1.CLAIMED
        )
        assert store.read_result(identity.identity_digest) is None
    finally:
        lease.close()


def test_stale_claim_reconciles_once_to_unknown_and_schema_conflict_fails_closed(tmp_path):
    from tokmo.records.quant_recurring_receipts import QuantRecurringReceiptStore
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantTerminalStateV1

    store, identity, lease = _claimed_store(tmp_path)
    lease.close()
    assert store.reconcile_stale(
        now=NOW + timedelta(seconds=301),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
    ) == 1
    assert store.read_due(identity.identity_digest).state is QuantTerminalStateV1.UNKNOWN
    assert store.reconcile_stale(
        now=NOW + timedelta(seconds=602),
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
    ) == 0

    connection = sqlite3.connect(store.path)
    try:
        connection.execute("PRAGMA user_version=2")
    finally:
        connection.close()
    with pytest.raises(ValueError, match="schema_mismatch"):
        QuantRecurringReceiptStore(store.path)
