"""Source-health private store: sealed schema, durable receipts, re-arm CHECKs."""

import os
import sqlite3
from datetime import UTC, datetime

import pytest

from tokmo.records import source_health_store
from tokmo.records.source_health_store import SourceHealthStore

NOW = datetime(2026, 8, 31, 12, 0, tzinfo=UTC).isoformat(timespec="microseconds")
LATER = datetime(2026, 8, 31, 12, 15, tzinfo=UTC).isoformat(timespec="microseconds")


def health_row(source="finnhub-company", **overrides):
    row = {
        "source": source,
        "state": "HEALTHY",
        "consecutive_failures": 0,
        "consecutive_successes": 0,
        "last_tick_id": "2026-08-31T12:00+00:00/B",
        "last_outcome": "SUCCESS",
        "pending_failure_pairs": None,
        "pending_fingerprint": None,
        "pending_fingerprint_ticks": 0,
        "first_failure_at": None,
        "incident_id": None,
        "incident_started_at": None,
        "incident_confirmed_at": None,
        "incident_fingerprint": None,
        "incident_last_error": None,
        "incident_failing_ticks": 0,
        "incident_alert_delivered_at": None,
        "last_processed_slot_utc": None,
        "updated_at": NOW,
    }
    row.update(overrides)
    return row


def degraded_row(source="finnhub-company", **overrides):
    return health_row(
        source,
        state="DEGRADED",
        incident_id="inc-1",
        incident_started_at=NOW,
        incident_confirmed_at=NOW,
        incident_fingerprint="f" * 64,
        incident_last_error="TRANSPORT_FAILURE HTTP_503",
        incident_failing_ticks=2,
        **overrides,
    )


def mint(dedupe_key, kind="DEGRADED", incident_id="inc-1", source="finnhub-company"):
    return {
        "notification_id": f"nid-{dedupe_key}",
        "incident_id": incident_id,
        "source": source,
        "kind": kind,
        "dedupe_key": dedupe_key,
        "body_text": "[NEWS] bounded body",
        "created_at": NOW,
    }


def test_bootstrap_creates_sealed_private_store(tmp_path):
    path = tmp_path / "source_health.sqlite3"
    store = SourceHealthStore(path)
    assert store.read_health("finnhub-company", now=NOW) is None
    assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    connection = sqlite3.connect(path)
    try:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        assert tables == {"schema_meta", "source_health", "notifications"}
        meta = connection.execute(
            "SELECT singleton,schema_id,schema_version FROM schema_meta"
        ).fetchone()
        assert meta == (1, "news-source-health-v1", 1)
        assert connection.execute("PRAGMA journal_mode").fetchone() != ("wal",)
    finally:
        connection.close()
    # A second open validates against the sealed digest without error.
    assert SourceHealthStore(path).read_health("bbc", now=NOW) is None


@pytest.mark.parametrize(
    "mutation",
    [
        "UPDATE schema_meta SET ddl_sha256='0'||substr(ddl_sha256,2)",
        "CREATE TABLE drifted (x INTEGER)",
        "DROP INDEX notifications_dedupe_uq",
    ],
)
def test_mutated_ddl_fails_closed_as_schema_mismatch(tmp_path, mutation):
    path = tmp_path / "source_health.sqlite3"
    SourceHealthStore(path).read_health("bbc", now=NOW)
    connection = sqlite3.connect(path)
    connection.execute(mutation)
    connection.commit()
    connection.close()
    with pytest.raises(RuntimeError, match="schema_mismatch"):
        SourceHealthStore(path).read_health("bbc", now=NOW)


def test_commit_tick_persists_row_and_mints_with_read_back(tmp_path):
    store = SourceHealthStore(tmp_path / "source_health.sqlite3")
    row = degraded_row()
    store.commit_tick(row, mints=(mint("inc-1:DEGRADED:1"),), supersede_ids=(), now=NOW)
    stored = store.read_health("finnhub-company", now=NOW)
    assert stored == row
    rows = store.read_notifications("inc-1", now=NOW)
    assert [r["dedupe_key"] for r in rows] == ["inc-1:DEGRADED:1"]
    assert rows[0]["status"] == "PENDING"
    assert rows[0]["attempt_count"] == 0
    # Replaying the same mint is a dedupe-key no-op, never a double mint.
    store.commit_tick(row, mints=(mint("inc-1:DEGRADED:1"),), supersede_ids=(), now=NOW)
    assert len(store.read_notifications("inc-1", now=NOW)) == 1


def test_crash_before_commit_leaves_no_partial_rows(tmp_path, monkeypatch):
    path = tmp_path / "source_health.sqlite3"
    store = SourceHealthStore(path)

    def explode(connection, row):
        raise RuntimeError("simulated_crash_before_commit")

    monkeypatch.setattr(source_health_store, "_verify_health_row", explode)
    with pytest.raises(RuntimeError, match="simulated_crash_before_commit"):
        store.commit_tick(
            degraded_row(), mints=(mint("inc-1:DEGRADED:1"),), supersede_ids=(), now=NOW
        )
    monkeypatch.undo()
    fresh = SourceHealthStore(path)
    assert fresh.read_health("finnhub-company", now=NOW) is None
    assert fresh.read_notifications("inc-1", now=NOW) == ()


def test_failed_rearm_resets_timestamps_and_naive_flip_violates_check(tmp_path):
    """R-DLV-5: PENDING <=> both timestamps NULL; re-arm is the explicit reset."""
    path = tmp_path / "source_health.sqlite3"
    store = SourceHealthStore(path)
    store.commit_tick(
        degraded_row(), mints=(mint("inc-1:DEGRADED:1"),), supersede_ids=(), now=NOW
    )
    claimed = store.claim_next("finnhub-company", now=NOW)
    assert claimed["attempt_count"] == 1
    store.settle(
        claimed["notification_id"], "FAILED", now=NOW, failure_reason="provider_failure"
    )
    # RED core: a naive re-arm that flips status alone must fail the CHECKs.
    connection = sqlite3.connect(path)
    try:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE notifications SET status='PENDING' WHERE notification_id=?",
                (claimed["notification_id"],),
            )
    finally:
        connection.close()
    store.reconcile_delivery(stale_before=NOW, now=LATER)
    row = store.read_notifications("inc-1", now=LATER)[0]
    assert row["status"] == "PENDING"
    assert row["attempted_at"] is None and row["settled_at"] is None
    assert row["attempt_count"] == 1
    # Full cycle PENDING->ATTEMPTING->SENT commits without CHECK violation.
    claimed = store.claim_next("finnhub-company", now=LATER)
    assert claimed["attempt_count"] == 2
    store.settle(
        claimed["notification_id"], "SENT", now=LATER, provider_message_id="42"
    )
    row = store.read_notifications("inc-1", now=LATER)[0]
    assert (row["status"], row["provider_message_id"]) == ("SENT", "42")


def test_rearm_stops_at_attempt_cap_and_stale_attempting_goes_unknown(tmp_path):
    store = SourceHealthStore(tmp_path / "source_health.sqlite3")
    store.commit_tick(
        degraded_row(), mints=(mint("inc-1:DEGRADED:1"),), supersede_ids=(), now=NOW
    )
    for _ in range(8):
        claimed = store.claim_next("finnhub-company", now=NOW)
        store.settle(
            claimed["notification_id"],
            "FAILED",
            now=NOW,
            failure_reason="provider_failure",
        )
        store.reconcile_delivery(stale_before=NOW, now=NOW)
    row = store.read_notifications("inc-1", now=NOW)[0]
    assert (row["status"], row["attempt_count"]) == ("FAILED", 8)
    assert store.claim_next("finnhub-company", now=NOW) is None
    # Stale ATTEMPTING reconciles to terminal UNKNOWN.
    store.commit_tick(
        degraded_row(), mints=(mint("inc-1:DEGRADED:2"),), supersede_ids=(), now=NOW
    )
    claimed = store.claim_next("finnhub-company", now=NOW)
    store.reconcile_delivery(stale_before=LATER, now=LATER)
    rows = {r["dedupe_key"]: r for r in store.read_notifications("inc-1", now=LATER)}
    settled = rows["inc-1:DEGRADED:2"]
    assert settled["status"] == "UNKNOWN"
    assert settled["failure_reason"] == "attempt_stale_crash"


def test_unattempted_supersede_keeps_null_attempted_and_zero_attempts(tmp_path):
    """R-DLV-6 (store contract): SUPERSEDED from PENDING settles without attempt."""
    store = SourceHealthStore(tmp_path / "source_health.sqlite3")
    reminder = mint("inc-1:REM:2026-08-31T23:00:00.000000+00:00", kind="REMINDER")
    store.commit_tick(degraded_row(), mints=(reminder,), supersede_ids=(), now=NOW)
    store.commit_tick(
        degraded_row(),
        mints=(),
        supersede_ids=(reminder["notification_id"],),
        now=LATER,
    )
    row = store.read_notifications("inc-1", now=LATER)[0]
    assert row["status"] == "SUPERSEDED"
    assert row["attempted_at"] is None
    assert row["settled_at"] == LATER
    assert row["attempt_count"] == 0
    assert store.claim_next("finnhub-company", now=LATER) is None


def test_claim_orders_transition_kinds_ahead_of_reminders(tmp_path):
    store = SourceHealthStore(tmp_path / "source_health.sqlite3")
    store.commit_tick(
        degraded_row(),
        mints=(
            mint("inc-1:REM:slot", kind="REMINDER"),
            mint("inc-1:DEGRADED:1", kind="DEGRADED"),
        ),
        supersede_ids=(),
        now=NOW,
    )
    assert store.pending_sources(now=NOW) == ("finnhub-company",)
    claimed = store.claim_next("finnhub-company", now=NOW)
    assert claimed["kind"] == "DEGRADED"
