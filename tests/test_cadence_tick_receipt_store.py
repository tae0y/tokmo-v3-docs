"""Stage 3: durable scheduler tick receipts (`ops/cadence_ticks.sqlite3`).

One canonical row per `(policy_version, scheduled_for_utc)` slot with the full
terminal evidence (times, lag, per-stage status, wire counts, mask identity,
reason), an events log for MISSED/DUPLICATE/OVERLAP occurrences, private file
modes, and stale-STARTED reconciliation for crashed invocations.
"""

from datetime import datetime, timedelta, timezone
import os
import stat

import pytest

from tokmo.records.cadence_tick_receipts import CadenceTickReceiptStore

POLICY = "cadence-300s-v1"
SLOT = datetime(2026, 8, 3, 10, 0, tzinfo=timezone.utc)
NOW = SLOT + timedelta(seconds=1)


def make_store(tmp_path):
    return CadenceTickReceiptStore(
        tmp_path / "ops" / "cadence_ticks.sqlite3", clock=lambda: NOW,
    )


def begin(store, slot=SLOT):
    return store.begin_tick(
        POLICY, slot, 5952914, observed_start=NOW, lag_seconds=1.0,
        max_lag_seconds=60,
    )


def finish(store, slot=SLOT, status="COMPLETE"):
    store.finish_tick(
        POLICY, slot, status=status, reason=None, mask_digest="a" * 64,
        open_markets=("US",), local_dates={"KR": "2026-08-03", "US": "2026-08-03"},
        stages=({"stage": "calendar", "status": "ok", "reason": None,
                 "latency_ms": 3},),
        wire_counts={"total_started": 3, "operation_started": {"TOKEN": 1}},
        calendar_attempts=("calendar:US:2026-08-03:0",),
        finished_at=NOW,
    )


def test_begin_finish_roundtrip_and_duplicate_detection(tmp_path):
    store = make_store(tmp_path)
    assert begin(store) == "created"
    row = store.read_tick(POLICY, SLOT)
    assert row["status"] == "STARTED"
    assert begin(store) == "duplicate"
    finish(store)
    row = store.read_tick(POLICY, SLOT)
    assert row["status"] == "COMPLETE"
    assert row["mask_digest"] == "a" * 64
    assert row["open_markets"] == ["US"]
    assert row["wire_counts"]["operation_started"] == {"TOKEN": 1}
    assert row["stages"][0]["stage"] == "calendar"
    assert row["calendar_attempts"] == ["calendar:US:2026-08-03:0"]
    # a terminal row never restarts
    assert begin(store) == "duplicate"


def test_record_missed_is_a_terminal_row_without_start(tmp_path):
    store = make_store(tmp_path)
    store.record_missed(
        POLICY, SLOT, 5952914, observed_start=NOW, lag_seconds=120.0,
        max_lag_seconds=60,
    )
    row = store.read_tick(POLICY, SLOT)
    assert row["status"] == "MISSED"
    assert row["lag_seconds"] == 120.0


def test_events_log_missed_duplicate_overlap(tmp_path):
    store = make_store(tmp_path)
    store.record_event(POLICY, SLOT, "OVERLAP")
    store.record_event(POLICY, SLOT, "DUPLICATE")
    kinds = [event["kind"] for event in store.read_events(POLICY, SLOT)]
    assert kinds == ["OVERLAP", "DUPLICATE"]
    with pytest.raises(ValueError):
        store.record_event(POLICY, SLOT, "SOMETHING_ELSE")


def test_reconcile_marks_stale_started_rows_unknown_except_current(tmp_path):
    store = make_store(tmp_path)
    other = SLOT - timedelta(seconds=300)
    store.begin_tick(POLICY, other, 5952913, observed_start=NOW,
                     lag_seconds=1.0, max_lag_seconds=60)
    begin(store)
    reconciled = store.reconcile_stale(now=NOW, exclude=(POLICY, SLOT))
    assert [(item["policy_version"], item["scheduled_for_utc"]) for item in reconciled] == [
        (POLICY, other)
    ]
    assert store.read_tick(POLICY, other)["status"] == "UNKNOWN"
    assert store.read_tick(POLICY, SLOT)["status"] == "STARTED"


def test_store_files_are_private(tmp_path):
    store = make_store(tmp_path)
    begin(store)
    mode = stat.S_IMODE(os.lstat(store.path).st_mode)
    assert mode == 0o600


def test_finish_requires_an_existing_started_row(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(ValueError):
        finish(store)
