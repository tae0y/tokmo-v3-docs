"""Right-sized date-partition collection + 18-month retention acceptance.

Contract: .ai/plans/2026-08-14_opendart-date-partition-retention-sds-v0.1.md
(v1.0). Fixture transport + temp DB only; zero network, zero production paths.
"""

import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tokmo.facts.opendart_bulk import Disclosure
from tokmo.opendart_bulk_composition import execute_opendart_bulk
from tokmo.programs.opendart_pilot import (
    PilotError, retention_floor, run_operate,
)
from tokmo.records.opendart_disclosures import DisclosureStore

NOW = datetime(2026, 7, 10, 1, 0, tzinfo=timezone.utc)  # 10:00 KST

EMPTY_BODY = json.dumps({"status": "013"}).encode()
STOP_020 = json.dumps({"status": "020"}).encode()


def open_store(tmp_path):
    db = DisclosureStore.open(tmp_path / "private" / "bulk.sqlite3")
    # Control-flow tests observe thousands of rows; durability is not under test.
    db.connection.execute("PRAGMA synchronous=OFF")
    return db


def day_text(value):
    return value.strftime("%Y%m%d")


def wire_row(day, index):
    return {"corp_code": "00126380", "corp_name": "Corp", "stock_code": "005930",
            "corp_cls": "Y", "report_nm": "r", "rcept_no": f"{day}{index:06d}",
            "flr_nm": "f", "rcept_dt": day, "rm": None}


def day_pages(day, total_pages, rows=None):
    if rows is None:
        rows = [wire_row(day, index) for index in range(total_pages * 100)]
    pages = []
    for page_no in range(1, total_pages + 1):
        chunk = rows[(page_no - 1) * 100:page_no * 100]
        pages.append((page_no, json.dumps(
            {"status": "000", "page_no": page_no, "page_count": 100,
             "total_count": len(rows), "total_page": total_pages,
             "list": chunk}).encode()))
    return pages


class FakeTime:
    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


class DayTransport:
    """Fixture transport keyed by (single-day window, page_no)."""

    def __init__(self, time=None, seconds_per_call=0):
        self.queues, self.calls = {}, []
        self.time, self.step = time, timedelta(seconds=seconds_per_call)

    def put(self, day, page_no, body):
        self.queues.setdefault((day, page_no), []).append(body)

    def put_day(self, day, total_pages, rows=None):
        for page_no, body in day_pages(day, total_pages, rows):
            self.put(day, page_no, body)

    def __call__(self, request, api_key):
        if request.begin != request.end:
            raise AssertionError("multi_day_window")
        key = (request.begin.strftime("%Y%m%d"), request.page_no)
        self.calls.append(key)
        if self.time is not None:
            self.time.now += self.step
        queue = self.queues.get(key)
        if not queue:
            raise AssertionError(f"unexpected_request {key}")
        return queue.pop(0) if len(queue) > 1 else queue[0]


def tick(db, *, today, transport, now=NOW, deadline=None, clock=None):
    return run_operate(
        db, today=today, api_key="x", transport=transport, now=now,
        deadline=deadline or now + timedelta(hours=6),
        clock=clock or (lambda: now))


def partition(db, day):
    return db.connection.execute(
        "select status,next_page,plan_total_page,plan_total_count "
        "from day_partitions where part_date=?", (day,)).fetchone()


# ---------------------------------------------------------------------------
# 1. No fixed page cap: multi-day shapes and one huge day
# ---------------------------------------------------------------------------

def test_two_days_of_66_pages_complete_without_any_fixed_cap(tmp_path):
    today = date(2026, 7, 3)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    db.connection.execute(
        "update day_partitions set status='complete' where part_date='20260701'")
    tx = DayTransport()
    tx.put_day("20260703", 33)
    tx.put_day("20260702", 33)

    result = tick(db, today=today, transport=tx)

    assert result["status"] == "complete"
    assert partition(db, "20260702")[0] == "complete"
    assert partition(db, "20260703")[0] == "open"  # today is never final same-day
    assert db.connection.execute(
        "select count(*) from disclosures").fetchone() == (6600,)
    assert db.connection.execute(
        "select count(*) from runs where status!='complete'").fetchone() == (0,)


def test_single_150_page_day_completes_without_page_cap(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    tx = DayTransport()
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put_day("20260701", 150)

    tick(db, today=today, transport=tx)

    assert partition(db, "20260701")[0] == "complete"
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_dt='20260701'"
    ).fetchone() == (15000,)


# ---------------------------------------------------------------------------
# 2. Deadline partial + cursor resume (exact-equal plan identity)
# ---------------------------------------------------------------------------

def test_deadline_saves_cursor_and_next_tick_resumes_to_completion(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    time = FakeTime(NOW)
    tx = DayTransport(time=time, seconds_per_call=10)
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put_day("20260701", 150)

    result = tick(db, today=today, transport=tx,
                  deadline=NOW + timedelta(seconds=10 * 41), clock=time)
    assert result["status"] == "complete"  # successful tick; durable partial resumes

    status, next_page, plan_page, plan_count = partition(db, "20260701")
    assert status == "open" and 1 < next_page <= 150
    assert (plan_page, plan_count) == (150, 15000)
    assert db.connection.execute(
        "select count(*) from runs where kind='partition' and status='partial'"
    ).fetchone() == (1,)
    first_tick_calls = len(tx.calls)

    later = NOW + timedelta(minutes=30)
    time2 = FakeTime(later)
    tx.time = time2
    result = tick(db, today=today, transport=tx, now=later,
                  deadline=later + timedelta(hours=6), clock=time2)
    assert result["status"] == "complete"

    assert partition(db, "20260701")[0] == "complete"
    resumed = [key for key in tx.calls[first_tick_calls:] if key[0] == "20260701"]
    assert resumed == [("20260701", page) for page in range(next_page, 151)]
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_dt='20260701'"
    ).fetchone() == (15000,)


def test_resume_with_changed_metadata_restarts_from_page_one_without_silent_skip(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    original = [wire_row("20260701", index) for index in range(300)]
    inserted = dict(wire_row("20260701", 999999), report_nm="inserted-mid")
    grown = original[:50] + [inserted] + original[50:]

    time = FakeTime(NOW)
    tx = DayTransport(time=time, seconds_per_call=10)
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put_day("20260701", 3, rows=original)
    tick(db, today=today, transport=tx,
         deadline=NOW + timedelta(seconds=10 * 3), clock=time)
    assert partition(db, "20260701")[:2] == ("open", 3)
    first_tick_calls = len(tx.calls)

    # One row was inserted mid-page-1 between ticks: cursor offset is invalid.
    tx.queues.clear()
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put_day("20260701", 4, rows=grown)
    later = NOW + timedelta(minutes=30)
    time2 = FakeTime(later)
    tx.time = time2
    tick(db, today=today, transport=tx, now=later,
         deadline=later + timedelta(hours=6), clock=time2)

    assert partition(db, "20260701")[0] == "complete"
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_dt='20260701'"
    ).fetchone() == (301,)
    assert db.connection.execute(
        "select count(*) from disclosures where rcept_no=?",
        (inserted["rcept_no"],)).fetchone() == (1,)
    restarted = [key for key in tx.calls[first_tick_calls:] if key[0] == "20260701"]
    assert restarted[0] == ("20260701", 3)  # probe at the stored cursor
    assert restarted[1:] == [("20260701", page) for page in range(1, 5)]


# ---------------------------------------------------------------------------
# 3. today first, seed-forward gap fill, oldest backlog order
# ---------------------------------------------------------------------------

def test_seed_forward_today_first_and_oldest_backlog_order(tmp_path):
    db = open_store(tmp_path)
    db.migrate_partitions(date(2026, 7, 1), NOW)
    today = date(2026, 7, 4)
    tx = DayTransport()
    for day in ("20260704", "20260701", "20260702", "20260703"):
        tx.put_day(day, 1)

    tick(db, today=today, transport=tx)

    assert tx.calls == [("20260704", 1), ("20260701", 1),
                        ("20260702", 1), ("20260703", 1)]
    for day in ("20260701", "20260702", "20260703"):
        assert partition(db, day)[0] == "complete"
    assert partition(db, "20260704")[0] == "open"


def test_empty_day_finalizes_only_after_settle_window(tmp_path):
    today = date(2026, 7, 6)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    tx = DayTransport()
    for day in ("20260701", "20260702", "20260703", "20260704",
                "20260705", "20260706"):
        tx.put(day, 1, EMPTY_BODY)

    tick(db, today=today, transport=tx)

    assert partition(db, "20260701")[0] == "complete"
    assert partition(db, "20260703")[0] == "complete"  # boundary: today-3
    assert partition(db, "20260704")[0] == "open"      # D-2 reconfirms later
    assert partition(db, "20260705")[0] == "open"      # D-1
    assert partition(db, "20260706")[0] == "open"      # today
    assert db.connection.execute(
        "select count(*) from runs where status!='complete'").fetchone() == (0,)


# ---------------------------------------------------------------------------
# 4. Completion gates and fail-closed stops
# ---------------------------------------------------------------------------

def test_completion_requires_conflict_zero(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    seeded = db.claim("partition", "20260701", "20260701", NOW)
    db.observe(seeded, Disclosure("00126380", "Corp", "005930", "Y", "r",
                                  "20260701000001", "f", "20260701", None), NOW)
    db.finish(seeded.run_id, "complete", NOW)
    impostor = dict(wire_row("20260701", 1), corp_code="99999999")
    tx = DayTransport()
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put("20260701", 1, json.dumps(
        {"status": "000", "page_no": 1, "page_count": 100, "total_count": 1,
         "total_page": 1, "list": [impostor]}).encode())

    tick(db, today=today, transport=tx)

    assert partition(db, "20260701")[0] == "failed"
    assert db.connection.execute(
        "select status,conflict_count from runs where kind='partition' "
        "and bgn_de='20260701' order by rowid desc limit 1"
    ).fetchone() == ("incomplete", 1)


def test_provider_stop_fails_closed_and_failed_dates_retry_after_open_backlog(tmp_path):
    today = date(2026, 7, 3)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    tx = DayTransport()
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put("20260701", 1, STOP_020)

    with pytest.raises(PilotError, match="provider_020"):
        tick(db, today=today, transport=tx)

    assert partition(db, "20260701")[0] == "failed"
    assert partition(db, "20260702")[0] == "open"
    assert ("20260702", 1) not in tx.calls  # quota stop aborts the whole tick

    tx2 = DayTransport()
    tx2.put(day_text(today), 1, EMPTY_BODY)
    tx2.put_day("20260702", 1)
    tx2.put_day("20260701", 1)
    tick(db, today=today, transport=tx2)

    backlog = [key for key in tx2.calls if key[0] != day_text(today)]
    assert backlog[0] == ("20260702", 1)  # open dates outrank failed ones
    assert backlog[1] == ("20260701", 1)  # failed date retries afterwards
    assert partition(db, "20260701")[0] == "complete"


# ---------------------------------------------------------------------------
# 5. Retention floor + double-guard prune
# ---------------------------------------------------------------------------

def test_retention_floor_is_first_day_of_month_18_months_back():
    assert retention_floor(date(2026, 8, 14)) == date(2025, 2, 1)
    assert retention_floor(date(2027, 3, 31)) == date(2025, 9, 1)
    assert retention_floor(date(2028, 2, 29)) == date(2026, 8, 1)
    assert retention_floor(date(2026, 1, 15)) == date(2024, 7, 1)


def make_disclosure(rcept_no, rcept_dt):
    return Disclosure("00126380", "Corp", "005930", "Y", "r", rcept_no, "f",
                      rcept_dt, None)


def test_prune_uses_double_guard_and_preserves_ledger(tmp_path):
    db = open_store(tmp_path)
    db.migrate_partitions(date(2026, 7, 1), NOW)
    floor = date(2025, 2, 1)
    old_now = datetime(2025, 1, 1, tzinfo=timezone.utc)
    old_run = db.claim("partition", "20250101", "20250101", old_now)
    db.start_attempt(old_run.run_id, 1, old_now)
    for rcept_no, rcept_dt in (
        ("20250101000001", "20250101"),  # both guards below floor -> pruned
        ("20250215000001", "20250115"),  # mutable rcept_dt alone below -> kept
        ("20250115000002", "20250301"),  # prefix below, rcept_dt current -> kept
        ("20250101000003", "20250101"),  # below floor but conflicted -> kept
    ):
        db.observe(old_run, make_disclosure(rcept_no, rcept_dt), NOW)
    db.observe(old_run, Disclosure("99999999", "X", None, "Y", "r",
                                   "20250101000003", "f", "20250101", None), NOW)
    db.finish(old_run.run_id, "complete", old_now)
    db.connection.execute(
        "insert into dart_revisions(rcept_no,previous_payload_hash,"
        "observed_payload_hash,observed_at,run_id,evidence_fingerprint) "
        "values('20250101000001','a','b','t',?,'fp1')", (old_run.run_id,))
    recent_run = db.claim("partition", "20260701", "20260701", NOW)
    db.start_attempt(recent_run.run_id, 1, NOW)
    db.finish(recent_run.run_id, "complete", NOW)
    db.connection.execute(
        "insert into day_partitions(part_date,status,next_page,updated_at) "
        "values('20250101','complete',1,'t')")

    summary = db.prune(floor, NOW)

    remaining = {value for (value,) in db.connection.execute(
        "select rcept_no from disclosures")}
    assert remaining == {"20250215000001", "20250115000002", "20250101000003"}
    assert (summary["disclosures"], summary["revisions"]) == (1, 1)
    assert db.connection.execute(
        "select count(*) from dart_revisions").fetchone() == (0,)
    assert db.connection.execute(
        "select count(*) from conflicts").fetchone() == (1,)
    assert db.connection.execute("select count(*) from runs").fetchone() == (2,)
    assert db.connection.execute(
        "select count(*) from attempts where run_id=?",
        (old_run.run_id,)).fetchone() == (0,)
    assert db.connection.execute(
        "select count(*) from attempts where run_id=?",
        (recent_run.run_id,)).fetchone() == (1,)
    assert db.connection.execute(
        "select count(*) from day_partitions where part_date<'20250201'"
    ).fetchone() == (0,)
    assert summary["foreign_key_violations"] == 0
    assert summary["integrity"] == "ok"

    again = db.prune(floor, NOW)
    assert (again["disclosures"], again["revisions"], again["attempts"],
            again["partitions"]) == (0, 0, 0, 0)


# ---------------------------------------------------------------------------
# 6. Read-only status, explicit migrate/backfill-seed, stale recovery
# ---------------------------------------------------------------------------

def test_status_and_integrity_are_read_only_and_absent_safe(tmp_path):
    home = tmp_path / "home"
    absent = execute_opendart_bulk(home=home, command="status", now=NOW)
    assert absent == {"integrity": "absent", "runs": {}, "partitions": {}}
    db_path = home / ".local" / "share" / "tokmo-v3" / "opendart-bulk.sqlite3"
    assert not db_path.exists()

    migrated = execute_opendart_bulk(home=home, command="migrate", now=NOW)
    assert migrated["status"] == "migrated" and migrated["seeded"] == 10
    before_bytes = db_path.read_bytes()
    before_mtime = db_path.stat().st_mtime_ns

    result = execute_opendart_bulk(home=home, command="status", now=NOW)

    assert result["integrity"] == "ok"
    assert result["partitions"] == {"open": 10}
    assert db_path.read_bytes() == before_bytes
    assert db_path.stat().st_mtime_ns == before_mtime


def test_migrate_and_backfill_seed_are_idempotent_and_operate_fails_closed_before_migrate(tmp_path):
    db = open_store(tmp_path)
    tx = DayTransport()
    with pytest.raises(PilotError, match="migration_required"):
        tick(db, today=date(2026, 7, 2), transport=tx)
    assert tx.calls == []

    today = date(2026, 7, 10)
    first = db.migrate_partitions(today, NOW)
    assert first == 10
    assert db.migrate_partitions(today, NOW) == 0
    floor = retention_floor(today)
    seeded = db.backfill_seed(floor, NOW)
    assert seeded == (date(2026, 7, 1) - floor).days
    assert db.backfill_seed(floor, NOW) == 0
    statuses = dict(db.connection.execute(
        "select status,count(*) from day_partitions group by status"))
    assert statuses == {"open": first + seeded}


def test_stale_claimed_run_is_recovered_and_cursor_preserved(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    stale_now = NOW - timedelta(hours=2)
    stale = db.claim("partition", "20260701", "20260701", stale_now)
    db.set_partition("20260701", status="in_progress", next_page=3,
                     plan=(5, 500), run_id=stale.run_id, now=stale_now)

    tx = DayTransport()
    tx.put(day_text(today), 1, EMPTY_BODY)
    tx.put_day("20260701", 5, rows=[wire_row("20260701", index)
                                    for index in range(500)])
    tick(db, today=today, transport=tx)

    assert db.receipt(stale.run_id).status == "failed"
    assert partition(db, "20260701")[0] == "complete"
    backlog = [key for key in tx.calls if key[0] == "20260701"]
    assert backlog == [("20260701", page) for page in (3, 4, 5)]


def test_live_claim_blocks_tick_with_single_skip_row_and_no_transport(tmp_path):
    today = date(2026, 7, 2)
    db = open_store(tmp_path)
    db.migrate_partitions(today, NOW)
    live = db.claim("partition", "20260701", "20260701",
                    NOW - timedelta(minutes=10))
    tx = DayTransport()

    result = tick(db, today=today, transport=tx)

    assert result["status"] == "skipped_overlap"
    assert tx.calls == []
    assert db.connection.execute("select count(*) from runs").fetchone() == (2,)
    assert db.receipt(live.run_id).status == "claimed"
