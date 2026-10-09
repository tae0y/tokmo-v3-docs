"""RED tests for the narrow DART rm provider-mutable annotation contract.

These tests are written RED and drive the implementation:

  1. null→정 REVISION on first observation, canonical latest updated
  2. exact repeat of same revision is DUPLICATE (idempotent)
  3. immutable field drift remains CONFLICT and fails the run
  4. existing-schema migration with rows (additive, idempotent)
  5. evidence idempotency: same transition appended once
  6. A→B→C: each transition appended once; stale A does not oscillate
  7. transaction rollback: partial failure leaves no partial evidence
  8. status/receipt revision_count in aggregates and CLI output
"""

import sqlite3
import textwrap
from datetime import UTC, date, datetime

import pytest
from test_opendart_bulk import envelope
from test_opendart_pilot import (
    Transport,
    latest_partition_run,
    migrated_for,
    operate_tick,
    seed_baseline,
    store,
)

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.records.opendart_disclosures import DisclosureStore

NOW = datetime(2026, 8, 3, tzinfo=UTC)
RCEPT_NO = "20260803000117"
CORP_CODE = "00126380"
CORP_NAME = "Samsung"
STOCK_CODE = "005930"
CORP_CLS = "Y"
REPORT_NM = "report"
FLR_NM = "filer"
RCEPT_DT = "20260701"


def disclosure_with_rm(rm):
    return Disclosure(
        CORP_CODE,
        CORP_NAME,
        STOCK_CODE,
        CORP_CLS,
        REPORT_NM,
        RCEPT_NO,
        FLR_NM,
        RCEPT_DT,
        rm,
    )


# ---------------------------------------------------------------------------
# 1. null→정 REVISION
# ---------------------------------------------------------------------------


def test_null_to_nonnull_rm_is_revision_not_conflict(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    # First observation: rm=None (as stored from baseline)
    assert db.observe(run, disclosure_with_rm(None), NOW) == "new"
    # Provider mutates rm to "정"
    result = db.observe(run, disclosure_with_rm("정"), NOW)
    assert result == "revision"
    # Canonical latest is now updated to observed
    stored_rm = db.connection.execute(
        "select rm from disclosures where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert stored_rm == ("정",)
    # revision evidence was appended
    count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert count == (1,)
    # run revision_count incremented
    assert db.receipt(run.run_id).revision_count == 1
    db.close()


# ---------------------------------------------------------------------------
# 2. Exact repeat of same revision is DUPLICATE
# ---------------------------------------------------------------------------


def test_exact_repeat_of_revision_is_duplicate(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)  # new
    db.observe(run, disclosure_with_rm("정"), NOW)  # revision
    # Now observed "정" again — should be duplicate
    result = db.observe(run, disclosure_with_rm("정"), NOW)
    assert result == "duplicate"
    # No new revision evidence
    count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert count == (1,)
    db.close()


# ---------------------------------------------------------------------------
# 3. Immutable field drift remains CONFLICT
# ---------------------------------------------------------------------------


def test_immutable_field_drift_is_still_conflict(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)  # new
    # Change a stable provider/company identity field.
    changed = Disclosure(
        "99999999",
        CORP_NAME,
        STOCK_CODE,
        CORP_CLS,
        REPORT_NM,
        RCEPT_NO,
        FLR_NM,
        RCEPT_DT,
        None,
    )
    result = db.observe(run, changed, NOW)
    assert result == "conflict"
    assert db.receipt(run.run_id).conflict_count == 1
    # No revision evidence for conflict
    rev_count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert rev_count == (0,)
    db.close()


# ---------------------------------------------------------------------------
# 4. Existing-schema migration with rows
# ---------------------------------------------------------------------------


def test_existing_schema_migrates_additively(tmp_path):
    """Opening a DB created with the old schema (no dart_revisions table,
    no runs.revision_count column) must migrate in-place and preserve rows."""
    db_path = tmp_path / "private" / "bulk.sqlite3"
    db_path.parent.mkdir(mode=0o700, exist_ok=True)

    # Manually create old-schema DB with rows
    old_schema = textwrap.dedent("""\
        CREATE TABLE IF NOT EXISTS runs(run_id TEXT PRIMARY KEY, kind TEXT NOT NULL,
         bgn_de TEXT NOT NULL, end_de TEXT NOT NULL, planned_pages INTEGER,
         fetched_pages INTEGER NOT NULL DEFAULT 0, first_total_count INTEGER,
         first_total_page INTEGER, status TEXT NOT NULL, new_count INTEGER NOT NULL DEFAULT 0,
         duplicate_count INTEGER NOT NULL DEFAULT 0, conflict_count INTEGER NOT NULL DEFAULT 0,
         started_at TEXT NOT NULL, ended_at TEXT);
        CREATE TABLE IF NOT EXISTS attempts(attempt_id INTEGER PRIMARY KEY, run_id TEXT NOT NULL,
         page_no INTEGER NOT NULL, started_at TEXT NOT NULL, outcome TEXT NOT NULL,
         FOREIGN KEY(run_id) REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS pilot_invocations(
         ordinal INTEGER PRIMARY KEY CHECK(ordinal BETWEEN 1 AND 5),
         run_id TEXT NOT NULL UNIQUE, claimed_at TEXT NOT NULL,
         FOREIGN KEY(run_id) REFERENCES runs(run_id));
        CREATE TABLE IF NOT EXISTS disclosures(rcept_no TEXT PRIMARY KEY, corp_code TEXT NOT NULL,
         corp_name TEXT NOT NULL, stock_code TEXT, corp_cls TEXT NOT NULL, report_nm TEXT NOT NULL,
         flr_nm TEXT NOT NULL, rcept_dt TEXT NOT NULL, rm TEXT, canonical_json TEXT NOT NULL,
         payload_hash TEXT NOT NULL, first_observed_at TEXT NOT NULL, first_run_id TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS conflicts(conflict_id INTEGER PRIMARY KEY, rcept_no TEXT NOT NULL,
         stored_json TEXT NOT NULL, observed_json TEXT NOT NULL, observed_at TEXT NOT NULL,
         run_id TEXT NOT NULL);
    """)
    conn = sqlite3.connect(str(db_path))
    conn.executescript(old_schema)
    conn.execute(
        "insert into runs(run_id,kind,bgn_de,end_de,status,started_at,new_count,conflict_count) "
        "values('oldrun1','backfill','20260701','20260801','complete','2026-08-01T00:00:00+00:00',5,0)"
    )
    conn.execute(
        "insert into disclosures values(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            RCEPT_NO,
            CORP_CODE,
            CORP_NAME,
            STOCK_CODE,
            CORP_CLS,
            REPORT_NM,
            FLR_NM,
            RCEPT_DT,
            None,
            '{"rm":null}',
            "abc123hash",
            "2026-08-01T00:00:00+00:00",
            "oldrun1",
        ),
    )
    conn.commit()
    conn.close()
    import os

    os.chmod(db_path, 0o600)

    # Open with DisclosureStore — triggers migration
    db = DisclosureStore.open(db_path)
    # Old rows preserved
    rows = db.connection.execute("select count(*) from runs").fetchone()
    assert rows == (1,)
    disc = db.connection.execute("select rcept_no,rm from disclosures").fetchone()
    assert disc == (RCEPT_NO, None)
    # New revision table exists
    rev_table = db.connection.execute(
        "select count(*) from sqlite_master where type='table' and name='dart_revisions'"
    ).fetchone()
    assert rev_table == (1,)
    # runs.revision_count column exists
    cols = [
        row[1] for row in db.connection.execute("pragma table_info(runs)").fetchall()
    ]
    assert "revision_count" in cols
    # Old run has revision_count=0 (default)
    rc = db.connection.execute(
        "select revision_count from runs where run_id='oldrun1'"
    ).fetchone()
    assert rc == (0,)
    # Idempotent: re-open should not fail or duplicate
    db.close()
    db2 = DisclosureStore.open(db_path)
    assert db2.connection.execute("select count(*) from dart_revisions").fetchone() == (
        0,
    )
    assert db2.connection.execute("select count(*) from runs").fetchone() == (1,)
    db2.close()


# ---------------------------------------------------------------------------
# 5. Evidence idempotency: same transition appended once
# ---------------------------------------------------------------------------


def test_evidence_idempotency_same_transition_appended_once(tmp_path):
    db = store(tmp_path)
    run1 = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run1, disclosure_with_rm(None), NOW)  # new
    db.observe(run1, disclosure_with_rm("정"), NOW)  # revision

    run2 = db.claim("incremental", "20260803", "20260803", NOW)
    db.finish(run2.run_id, "complete", NOW)
    run2 = db.claim("incremental", "20260803", "20260803", NOW)
    # Observe "정" again from a different run — still duplicate, no new revision
    result = db.observe(run2, disclosure_with_rm("정"), NOW)
    assert result == "duplicate"
    count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert count == (1,)
    db.close()


# ---------------------------------------------------------------------------
# 6. A→B→C each transition once; stale A does not oscillate
# ---------------------------------------------------------------------------


def test_a_to_b_to_c_each_transition_once(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)  # new (A=null)
    db.observe(run, disclosure_with_rm("A"), NOW)  # revision null→A
    db.observe(run, disclosure_with_rm("B"), NOW)  # revision A→B
    db.observe(run, disclosure_with_rm("C"), NOW)  # revision B→C
    count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert count == (3,)
    # Canonical latest is C
    stored_rm = db.connection.execute(
        "select rm from disclosures where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert stored_rm == ("C",)
    db.close()


def test_stale_observation_after_revision_is_safe(tmp_path):
    """After A→B, a stale A observation must not silently oscillate
    canonical latest back to A. It should be treated as a revision
    B→A (appending evidence) rather than silently reverting, because
    we cannot establish chronology. The canonical latest updates to
    the newly observed value so the next identical observation is
    duplicate — latest-observed semantics."""
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)  # new
    db.observe(run, disclosure_with_rm("A"), NOW)  # revision null→A
    db.observe(run, disclosure_with_rm("B"), NOW)  # revision A→B
    # Stale A arrives (e.g., provider cache lag)
    result = db.observe(run, disclosure_with_rm("A"), NOW)
    assert result == "revision"
    # Evidence appended (B→A transition recorded)
    count = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert count == (3,)
    # Canonical latest is now A (latest-observed)
    stored_rm = db.connection.execute(
        "select rm from disclosures where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert stored_rm == ("A",)
    db.close()


def test_repeated_transition_keeps_truthful_evidence_per_run(tmp_path):
    db = store(tmp_path)
    run1 = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run1, disclosure_with_rm(None), NOW)
    db.observe(run1, disclosure_with_rm("A"), NOW)
    db.observe(run1, disclosure_with_rm("B"), NOW)
    db.finish(run1.run_id, "complete", NOW)

    run2 = db.claim("incremental", "20260803", "20260803", NOW)
    assert db.observe(run2, disclosure_with_rm("A"), NOW) == "revision"
    assert db.observe(run2, disclosure_with_rm("B"), NOW) == "revision"

    rows = db.connection.execute(
        "select run_id from dart_revisions where rcept_no=? order by revision_id",
        (RCEPT_NO,),
    ).fetchall()
    assert len(rows) == 4
    assert db.receipt(run1.run_id).revision_count == 2
    assert db.receipt(run2.run_id).revision_count == 2
    assert sum(row[0] == run1.run_id for row in rows) == 2
    assert sum(row[0] == run2.run_id for row in rows) == 2
    db.close()


# ---------------------------------------------------------------------------
# 7. Transaction rollback: rm update + revision evidence + run counters
#    together or rollback
# ---------------------------------------------------------------------------


def test_revision_evidence_has_unique_fingerprint(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)
    db.observe(run, disclosure_with_rm("A"), NOW)
    db.observe(run, disclosure_with_rm("B"), NOW)
    rows = db.connection.execute(
        "select evidence_fingerprint from dart_revisions where rcept_no=? "
        "order by evidence_fingerprint",
        (RCEPT_NO,),
    ).fetchall()
    fingerprints = [r[0] for r in rows]
    assert len(fingerprints) == len(set(fingerprints)), "fingerprints must be unique"
    db.close()


# ---------------------------------------------------------------------------
# 8. Revision evidence contains required fields
# ---------------------------------------------------------------------------


def test_revision_evidence_contains_required_fields(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)
    db.observe(run, disclosure_with_rm("정"), NOW)
    row = db.connection.execute(
        """select rcept_no, previous_rm, observed_rm, previous_payload_hash,
                  observed_payload_hash, observed_at, run_id, evidence_fingerprint
           from dart_revisions where rcept_no=?""",
        (RCEPT_NO,),
    ).fetchone()
    assert row[0] == RCEPT_NO
    assert row[1] is None  # previous_rm was null
    assert row[2] == "정"  # observed_rm
    assert row[3] is not None  # previous_payload_hash
    assert row[4] is not None  # observed_payload_hash
    assert row[5] is not None  # observed_at
    assert row[6] == run.run_id  # run_id
    assert row[7] is not None  # evidence_fingerprint
    db.close()


# ---------------------------------------------------------------------------
# 9. Receipt and status include revision_count
# ---------------------------------------------------------------------------


def test_receipt_includes_revision_count(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run, disclosure_with_rm(None), NOW)
    db.observe(run, disclosure_with_rm("정"), NOW)
    receipt = db.receipt(run.run_id)
    assert receipt.revision_count == 1
    assert receipt.new_count == 1
    assert receipt.conflict_count == 0
    db.close()


# ---------------------------------------------------------------------------
# 10. Revision evidence, canonical update, and run counter are atomic
# ---------------------------------------------------------------------------


def test_revision_rolls_back_all_writes_when_counter_update_fails(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260803", "20260803", NOW)
    assert db.observe(run, disclosure_with_rm(None), NOW) == "new"
    db.connection.execute("""
        CREATE TRIGGER fail_revision_counter
        BEFORE UPDATE OF revision_count ON runs
        WHEN NEW.revision_count > OLD.revision_count
        BEGIN SELECT RAISE(ABORT, 'forced_revision_counter_failure'); END
    """)

    with pytest.raises(sqlite3.IntegrityError, match="forced_revision_counter_failure"):
        db.observe(run, disclosure_with_rm("정"), NOW)

    assert db.connection.execute(
        "select rm from disclosures where rcept_no=?", (RCEPT_NO,)
    ).fetchone() == (None,)
    assert db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone() == (0,)
    assert db.receipt(run.run_id).revision_count == 0
    db.close()


# ---------------------------------------------------------------------------
# 11. run_operate does not fail on rm revision (the original bug)
# ---------------------------------------------------------------------------


def test_operate_succeeds_with_rm_revision(tmp_path):
    """The original live failure: a disclosure that was stored with rm=null
    is observed with rm="정". run_operate should succeed, not fail-closed."""
    db = store(tmp_path)
    seed_baseline(db, end="20260803")
    # Seed a disclosure from the baseline with rm=None
    run0 = db.claim("backfill", "20260701", "20260803", NOW)
    db.observe(run0, disclosure_with_rm(None), NOW)
    db.finish(run0.run_id, "complete", NOW)

    # Now run operate — the page returns the same disclosure but rm="정"
    page_body = envelope(
        [
            {
                "corp_code": CORP_CODE,
                "corp_name": CORP_NAME,
                "stock_code": STOCK_CODE,
                "corp_cls": CORP_CLS,
                "report_nm": REPORT_NM,
                "rcept_no": RCEPT_NO,
                "flr_nm": FLR_NM,
                "rcept_dt": RCEPT_DT,
                "rm": "정",
            }
        ]
    )
    tx = Transport([page_body])
    migrated_for(db, date(2026, 8, 3))
    operate_tick(db, today=date(2026, 8, 3), transport=tx, now=NOW)
    receipt = latest_partition_run(db)
    assert receipt.status == "complete"
    assert receipt.revision_count == 1
    db.close()


# ---------------------------------------------------------------------------
# 11. Multiple revisions across different runs accumulate correctly
# ---------------------------------------------------------------------------


def test_revisions_across_runs_accumulate(tmp_path):
    db = store(tmp_path)
    run1 = db.claim("incremental", "20260803", "20260803", NOW)
    db.observe(run1, disclosure_with_rm(None), NOW)  # new
    db.observe(run1, disclosure_with_rm("A"), NOW)  # revision
    db.finish(run1.run_id, "complete", NOW)

    run2 = db.claim("incremental", "20260803", "20260803", NOW)
    result = db.observe(run2, disclosure_with_rm("B"), NOW)  # revision A→B
    assert result == "revision"
    db.finish(run2.run_id, "complete", NOW)

    # Total revision evidence across runs
    total = db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?", (RCEPT_NO,)
    ).fetchone()
    assert total == (2,)
    # Each run has its own revision_count
    assert db.receipt(run1.run_id).revision_count == 1
    assert db.receipt(run2.run_id).revision_count == 1
    db.close()
