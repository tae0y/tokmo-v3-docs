"""The 2026-08-07 OpenDART collection stall and the contract that replaces it.

Plan: plans/2026-08-07_opendart-revision-model.md

The defect, reproduced as Phase 0 before any code moved:

  1. DART served rcept_dt=20260807 for rcept_no 20260806000555 (핑거) at
     2026-08-06T22:30:42Z, then rcept_dt=20260806 from 2026-08-07T00:30Z.
     Only that one field differed.
  2. rcept_dt sat in IMMUTABLE_FIELDS, so the drift classified as CONFLICT.
  3. _observe_page raised on the first conflicting row, so every later page of
     the run went unrequested. The query sorts ascending by date, which puts
     new disclosures on the LAST pages — new_count was structurally 0.
  4. The conflict path never updated `disclosures`, so the stall repeated on
     every run: 4 consecutive failures, 120 disclosures behind the provider.

What these tests now pin instead:

  - rcept_dt drift is a REVISION; the provider's newer value is adopted on both
    canonical_json and the denormalised column, and the next sighting is a
    plain duplicate (§1, §2).
  - stable identity drift (corp_code and stock_code, the only two guards) is
    still a CONFLICT (§2).
  - a conflict downgrades the run to `incomplete` without discarding the rest
    of the page or the pages behind it (§3).
  - revision evidence keeps the superseded payload, not just its hash (§4).
"""

import os
import sqlite3
from datetime import date, datetime, timezone

import pytest

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.programs.opendart_pilot import run_preflight
from tokmo.records.opendart_disclosures import SCHEMA, DisclosureStore

from test_opendart_bulk import envelope
from test_opendart_pilot import (
    Transport, latest_partition_run, migrated_for, operate_tick, seed_baseline,
    store,
)


NOW = datetime(2026, 8, 7, tzinfo=timezone.utc)
TODAY = date(2026, 8, 7)

# The exact production identity that stalled the lane.
DRIFT_NO = "20260806000555"
CORP_CODE = "01142075"
CORP_NAME = "핑거"
STOCK_CODE = "163730"
CORP_CLS = "K"
REPORT_NM = "주요사항보고서(타법인주식및출자증권양수결정)"
FLR_NM = "핑거"
FIRST_SEEN_DT = "20260807"   # what DART served at 22:30Z
CORRECTED_DT = "20260806"    # what DART serves now


def drifting(rcept_dt):
    return Disclosure(CORP_CODE, CORP_NAME, STOCK_CODE, CORP_CLS, REPORT_NM,
                      DRIFT_NO, FLR_NM, rcept_dt, None)


def wire_row(receipt, rcept_dt="20260806"):
    """One provider row as it arrives on the wire."""
    return {"corp_code": CORP_CODE, "corp_name": CORP_NAME,
            "stock_code": STOCK_CODE, "corp_cls": CORP_CLS,
            "report_nm": REPORT_NM, "rcept_no": receipt, "flr_nm": FLR_NM,
            "rcept_dt": rcept_dt, "rm": None}


def filler(start, count):
    return [wire_row(f"2026{number:010d}") for number in range(start, start + count)]


# ---------------------------------------------------------------------------
# 1. rcept_dt drift is a REVISION and the provider value is adopted
# ---------------------------------------------------------------------------

def test_rcept_dt_drift_is_a_revision_and_adopts_the_provider_value(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)

    assert db.observe(run, drifting(FIRST_SEEN_DT), NOW) == "new"

    # DART corrects the receipt date. Nothing else about the disclosure moved.
    result = db.observe(run, drifting(CORRECTED_DT), NOW)

    assert result == "revision"
    assert db.receipt(run.run_id).conflict_count == 0
    assert db.receipt(run.run_id).revision_count == 1

    # The canonical row adopts the provider's newer value, so the next run sees
    # a duplicate instead of re-hitting the same conflict.
    canonical, rcept_dt = db.connection.execute(
        "select canonical_json,rcept_dt from disclosures where rcept_no=?",
        (DRIFT_NO,)).fetchone()
    assert rcept_dt == CORRECTED_DT
    # The denormalised column and the canonical payload must not drift apart.
    assert f'"rcept_dt":"{CORRECTED_DT}"' in canonical

    assert db.connection.execute(
        "select count(*) from conflicts where rcept_no=?",
        (DRIFT_NO,)).fetchone() == (0,)
    assert db.connection.execute(
        "select count(*) from dart_revisions where rcept_no=?",
        (DRIFT_NO,)).fetchone() == (1,)
    db.close()


def test_rm_revision_still_updates_its_denormalised_column(tmp_path):
    """Generalising the UPDATE must not regress the original rm contract."""
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    db.observe(run, drifting(CORRECTED_DT), NOW)

    revised = Disclosure(CORP_CODE, CORP_NAME, STOCK_CODE, CORP_CLS, REPORT_NM,
                         DRIFT_NO, FLR_NM, CORRECTED_DT, "정")
    assert db.observe(run, revised, NOW) == "revision"

    assert db.connection.execute(
        "select rm,rcept_dt from disclosures where rcept_no=?",
        (DRIFT_NO,)).fetchone() == ("정", CORRECTED_DT)
    db.close()


# ---------------------------------------------------------------------------
# 2. The stall is self-healing — a repeat observation is now a duplicate
# ---------------------------------------------------------------------------

def test_rcept_dt_correction_settles_instead_of_repeating(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    db.observe(run, drifting(FIRST_SEEN_DT), NOW)

    assert db.observe(run, drifting(CORRECTED_DT), NOW) == "revision"
    for _ in range(3):
        assert db.observe(run, drifting(CORRECTED_DT), NOW) == "duplicate"

    assert db.receipt(run.run_id).conflict_count == 0
    assert db.receipt(run.run_id).revision_count == 1
    db.close()


def test_corp_name_correction_is_a_revision_and_settles(tmp_path):
    """The exact observed company-name correction is provider revision data."""
    db = store(tmp_path)
    run = db.claim("incremental", "20260811", "20260811", NOW)
    original = Disclosure(
        "01234567", "압타머사이언스", "291650", CORP_CLS, REPORT_NM,
        "20260811900691", "압타머사이언스", "20260811", None)
    corrected = Disclosure(
        "01234567", "츌립앤사이언스", "291650", CORP_CLS, REPORT_NM,
        "20260811900691", "압타머사이언스", "20260811", None)

    assert db.observe(run, original, NOW) == "new"
    assert db.observe(run, corrected, NOW) == "revision"
    assert db.observe(run, corrected, NOW) == "duplicate"

    receipt = db.receipt(run.run_id)
    assert receipt.conflict_count == 0
    assert receipt.revision_count == 1
    canonical, corp_name = db.connection.execute(
        "select canonical_json,corp_name from disclosures where rcept_no=?",
        ("20260811900691",)).fetchone()
    assert corp_name == "츌립앤사이언스"
    assert '"corp_name":"츌립앤사이언스"' in canonical

    previous, observed, changed = db.connection.execute(
        "select previous_canonical_json,observed_canonical_json,changed_fields "
        "from dart_revisions where rcept_no=?",
        ("20260811900691",)).fetchone()
    assert previous == original.canonical_json()
    assert observed == corrected.canonical_json()
    assert changed == "corp_name"
    db.close()


def test_corp_code_drift_is_still_a_conflict(tmp_path):
    """Widening the mutable set must not weaken the identity guard."""
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    db.observe(run, drifting(CORRECTED_DT), NOW)

    impostor = Disclosure("99999999", CORP_NAME, STOCK_CODE, CORP_CLS, REPORT_NM,
                          DRIFT_NO, FLR_NM, CORRECTED_DT, None)
    assert db.observe(run, impostor, NOW) == "conflict"
    assert db.receipt(run.run_id).conflict_count == 1
    db.close()


def test_stock_code_drift_is_still_a_conflict(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    db.observe(run, drifting(CORRECTED_DT), NOW)

    impostor = Disclosure(CORP_CODE, CORP_NAME, "999999", CORP_CLS, REPORT_NM,
                          DRIFT_NO, FLR_NM, CORRECTED_DT, None)
    assert db.observe(run, impostor, NOW) == "conflict"
    assert db.receipt(run.run_id).conflict_count == 1
    db.close()


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("corp_cls", "Y"),
        ("report_nm", "[첨부추가]주요사항보고서(타법인주식및출자증권양수결정)"),
        ("flr_nm", "정정 제출인"),
    ),
)
def test_non_identity_field_correction_is_a_revision_and_settles(
        tmp_path, field, replacement):
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    original = drifting(CORRECTED_DT)
    db.observe(run, original, NOW)

    values = {
        "corp_code": original.corp_code,
        "corp_name": original.corp_name,
        "stock_code": original.stock_code,
        "corp_cls": original.corp_cls,
        "report_nm": original.report_nm,
        "rcept_no": original.rcept_no,
        "flr_nm": original.flr_nm,
        "rcept_dt": original.rcept_dt,
        "rm": original.rm,
    }
    values[field] = replacement
    corrected = Disclosure(**values)

    assert db.observe(run, corrected, NOW) == "revision"
    assert db.observe(run, corrected, NOW) == "duplicate"
    assert db.receipt(run.run_id).conflict_count == 0
    assert db.receipt(run.run_id).revision_count == 1

    columns = db.connection.execute(
        "select corp_cls,report_nm,flr_nm,canonical_json from disclosures "
        "where rcept_no=?", (DRIFT_NO,)).fetchone()
    indices = {"corp_cls": 0, "report_nm": 1, "flr_nm": 2}
    assert columns[indices[field]] == replacement
    assert f'"{field}":"{replacement}"' in columns[3]
    evidence = db.connection.execute(
        "select previous_canonical_json,observed_canonical_json,changed_fields "
        "from dart_revisions where rcept_no=?", (DRIFT_NO,)).fetchone()
    assert evidence == (original.canonical_json(), corrected.canonical_json(), field)
    db.close()


def test_report_name_incident_is_revision_not_conflict(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260813", "20260813", NOW)
    original = Disclosure("00689143", "아하", "102950", "K",
                          "주주명부폐쇄기간또는기준일설정",
                          "20260813601444", "아하", "20260813", None)
    corrected = Disclosure("00689143", "아하", "102950", "K",
                           "[첨부추가]주주명부폐쇄기간또는기준일설정",
                           "20260813601444", "아하", "20260813", None)

    assert db.observe(run, original, NOW) == "new"
    assert db.observe(run, corrected, NOW) == "revision"
    assert db.observe(run, corrected, NOW) == "duplicate"
    receipt = db.receipt(run.run_id)
    assert receipt.conflict_count == 0
    assert receipt.revision_count == 1
    report_name, canonical = db.connection.execute(
        "select report_nm,canonical_json from disclosures where rcept_no=?",
        ("20260813601444",)).fetchone()
    assert report_name == corrected.report_nm
    assert f'"report_nm":"{corrected.report_nm}"' in canonical
    assert db.connection.execute(
        "select changed_fields from dart_revisions where rcept_no=?",
        ("20260813601444",)).fetchone() == ("report_nm",)
    db.close()


# ---------------------------------------------------------------------------
# 3. A conflict no longer discards the remaining pages
# ---------------------------------------------------------------------------

def conflicting_pages(db):
    """Three pages where page 2 carries a genuine identity conflict.

    corp_code drift stays a conflict after Phase 2, so this exercises the abort
    policy itself rather than the rcept_dt classification.
    """
    seed = seed_baseline(db)
    db.observe(seed.run_id, drifting(CORRECTED_DT), NOW)
    impostor = dict(wire_row(DRIFT_NO, CORRECTED_DT), corp_code="99999999")
    return [
        envelope(filler(100, 100), page=1, total_count=201, total_page=3),
        envelope(filler(200, 99) + [impostor],
                 page=2, total_count=201, total_page=3),
        # Page 3 is where the genuinely new disclosures live under ascending sort.
        envelope([wire_row("20260807000999")],
                 page=3, total_count=201, total_page=3),
    ]


def test_conflict_no_longer_aborts_the_remaining_pages(tmp_path):
    db = store(tmp_path)
    tx = Transport(conflicting_pages(db))
    migrated_for(db, TODAY)

    operate_tick(db, today=TODAY, transport=tx, now=NOW)
    receipt = latest_partition_run(db)

    # Every page was requested, and the new disclosure on the last page landed.
    assert len(tx.calls) == 3
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_no=?",
        ("20260807000999",)).fetchone() == (1,)

    # The conflict is still reported — it is downgraded, not silenced.
    assert receipt.status == "incomplete"
    assert receipt.conflict_count == 1
    assert receipt.fetched_pages == 3
    db.close()


def test_page_counter_matches_successful_attempts(tmp_path):
    """fetched_pages under-reporting was the fingerprint of the abort path."""
    db = store(tmp_path)
    tx = Transport(conflicting_pages(db))
    migrated_for(db, TODAY)
    operate_tick(db, today=TODAY, transport=tx, now=NOW)
    receipt = latest_partition_run(db)

    successes = db.connection.execute(
        "select count(*) from attempts where run_id=? and outcome='success'",
        (receipt.run_id,)).fetchone()
    assert successes == (receipt.fetched_pages,) == ((3,))
    db.close()


def test_whole_page_is_observed_even_after_a_conflicting_row(tmp_path):
    db = store(tmp_path)
    pages = conflicting_pages(db)
    migrated_for(db, TODAY)
    operate_tick(db, today=TODAY, transport=Transport(pages), now=NOW)

    # All 99 rows that share page 2 with the conflicting row are persisted, and
    # so is every row on page 1.
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_no like '202600000002%'"
    ).fetchone() == (99,)
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_no like '202600000001%'"
    ).fetchone() == (100,)
    db.close()


def test_preflight_with_a_conflict_does_not_report_complete(tmp_path):
    """_observe_page no longer raises, so preflight needs its own gate."""
    db = store(tmp_path)
    seed = seed_baseline(db)
    db.observe(seed.run_id, drifting(CORRECTED_DT), NOW)
    impostor = dict(wire_row(DRIFT_NO, CORRECTED_DT), corp_code="99999999")

    receipt = run_preflight(
        db, fixed_cutoff=date(2026, 8, 6), api_key="x",
        transport=Transport([envelope([impostor], page=1)]), now=NOW)

    assert receipt.status == "incomplete"
    assert receipt.conflict_count == 1
    assert db.completed_preflight(receipt.run_id) is None
    db.close()


# ---------------------------------------------------------------------------
# 4. Revision evidence keeps the superseded values, not just hashes
# ---------------------------------------------------------------------------

def _legacy_revision_schema_db(tmp_path):
    """A DB whose dart_revisions predates the evidence columns."""
    db_path = tmp_path / "private" / "bulk.sqlite3"
    db_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    legacy = SCHEMA.replace(
        " previous_canonical_json TEXT,\n"
        " observed_canonical_json TEXT,\n"
        " changed_fields TEXT,\n", "")
    assert "previous_canonical_json" not in legacy
    connection = sqlite3.connect(str(db_path))
    connection.executescript(legacy)
    connection.commit()
    connection.close()
    os.chmod(db_path, 0o600)
    return db_path


def test_migrated_dart_revisions_schema_matches_a_freshly_created_one(tmp_path):
    legacy_path = _legacy_revision_schema_db(tmp_path / "legacy")
    migrated = DisclosureStore.open(legacy_path)
    fresh = DisclosureStore.open(tmp_path / "fresh" / "private" / "bulk.sqlite3")

    def columns(db):
        return [(row[1], row[2]) for row in
                db.connection.execute("pragma table_info(dart_revisions)")]

    assert columns(migrated) == columns(fresh)
    assert [name for name, _type in columns(fresh)][-3:] == [
        "previous_canonical_json", "observed_canonical_json", "changed_fields"]

    # Idempotent: reopening does not re-add or reorder.
    migrated.close()
    reopened = DisclosureStore.open(legacy_path)
    assert columns(reopened) == columns(fresh)
    reopened.close()
    fresh.close()


def test_revision_records_the_superseded_value_not_only_a_hash(tmp_path):
    db = store(tmp_path)
    run = db.claim("incremental", "20260806", "20260807", NOW)
    db.observe(run, drifting(FIRST_SEEN_DT), NOW)
    db.observe(run, drifting(CORRECTED_DT), NOW)

    previous, observed, changed = db.connection.execute(
        "select previous_canonical_json,observed_canonical_json,changed_fields "
        "from dart_revisions where rcept_no=?", (DRIFT_NO,)).fetchone()

    assert changed == "rcept_dt"
    assert f'"rcept_dt":"{FIRST_SEEN_DT}"' in previous
    assert f'"rcept_dt":"{CORRECTED_DT}"' in observed
    db.close()


def test_legacy_revision_rows_keep_null_evidence(tmp_path):
    """Pre-existing rows are not backfilled — the original payloads are gone."""
    legacy_path = _legacy_revision_schema_db(tmp_path / "legacy")
    connection = sqlite3.connect(str(legacy_path))
    connection.execute(
        "insert into runs(run_id,kind,bgn_de,end_de,status,started_at) "
        "values('old','incremental','20260803','20260803','complete','2026-08-03T00:00:00+00:00')")
    connection.execute(
        "insert into dart_revisions(rcept_no,previous_rm,observed_rm,"
        "previous_payload_hash,observed_payload_hash,observed_at,run_id,"
        "evidence_fingerprint) values(?,?,?,?,?,?,?,?)",
        (DRIFT_NO, None, "정", "hash_a", "hash_b",
         "2026-08-03T00:00:00+00:00", "old", "fingerprint_a"))
    connection.commit()
    connection.close()

    db = DisclosureStore.open(legacy_path)
    assert db.connection.execute(
        "select previous_canonical_json,observed_canonical_json,changed_fields "
        "from dart_revisions where rcept_no=?", (DRIFT_NO,)).fetchone() == (
            None, None, None)
    db.close()
