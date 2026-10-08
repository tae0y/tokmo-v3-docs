import json
from datetime import date, datetime, timedelta, timezone

import pytest

from tokmo.facts.opendart_bulk import BulkContractError, BulkTransportFailure
from tokmo.programs.opendart_pilot import (
    PilotError, run_baseline, run_incremental, run_operate, run_preflight,
)
from tokmo.records.opendart_disclosures import DisclosureStore

from test_opendart_bulk import envelope, row
from tools.run_opendart_pilot import run_five_polls
from tokmo.composition import _seoul_calendar_date


NOW = datetime(2026, 8, 3, tzinfo=timezone.utc)


class Transport:
    def __init__(self, pages): self.pages, self.calls = list(pages), []
    def __call__(self, request, api_key):
        self.calls.append(request)
        item = self.pages.pop(0)
        if isinstance(item, Exception): raise item
        return item


def store(tmp_path): return DisclosureStore.open(tmp_path / "private" / "bulk.sqlite3")


def seed_baseline(db, begin="20260701", end="20260801"):
    run = db.claim("backfill", begin, end, NOW)
    db.set_plan(run.run_id, 1, 0)
    return db.finish(run.run_id, "complete", NOW)


def full_page(total_count, total_page):
    rows = [row(f"2026{number:010d}") for number in range(100)]
    return envelope(rows, total_count=total_count, total_page=total_page)


def migrated_for(db, today):
    """Create day_partitions and mark pre-today seed rows complete so a test
    exercises exactly the current day."""
    db.migrate_partitions(today, NOW)
    db.connection.execute(
        "update day_partitions set status='complete' where part_date<?",
        (today.strftime("%Y%m%d"),))


def operate_tick(db, *, today, transport, api_key="x", now=NOW):
    return run_operate(db, today=today, api_key=api_key, transport=transport,
                       now=now, deadline=now + timedelta(hours=6),
                       clock=lambda: now)


def latest_partition_run(db):
    run_id = db.connection.execute(
        "select run_id from runs where kind='partition' "
        "order by rowid desc limit 1").fetchone()[0]
    return db.receipt(run_id)


def test_attempt_is_committed_before_transport_failure_and_no_retry(tmp_path):
    db, tx = store(tmp_path), Transport([BulkTransportFailure("timeout")])
    with pytest.raises(PilotError, match="timeout"):
        run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key="SECRET", transport=tx, now=NOW)
    assert len(tx.calls) == 1
    assert db.connection.execute("select outcome from attempts").fetchone() == ("timeout",)
    assert db.connection.execute("select status from runs").fetchone() == ("failed",)


@pytest.mark.parametrize("code", ["010", "011", "012", "020", "021", "101"])
def test_provider_stop_class_is_durable(code, tmp_path):
    db = store(tmp_path)
    body = json.dumps({"status": code, "message": "RAW PROVIDER TEXT"}).encode()
    with pytest.raises(PilotError, match=f"provider_{code}"):
        run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key="x",
                      transport=Transport([body]), now=NOW)
    assert db.connection.execute("select outcome from attempts").fetchone() == (code,)
    assert b"RAW PROVIDER TEXT" not in (tmp_path / "private" / "bulk.sqlite3").read_bytes()


def test_preflight_is_one_request_and_does_not_continue(tmp_path):
    db, tx = store(tmp_path), Transport([full_page(201, 3)])
    receipt = run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key="SECRET", transport=tx, now=NOW)
    assert receipt.status == "complete" and receipt.planned_pages == 3
    assert len(tx.calls) == 1


def test_baseline_requires_exact_preflight_identity_and_planned_pages(tmp_path):
    db, tx = store(tmp_path), Transport([envelope([row()])])
    pre = run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key="x", transport=tx, now=NOW)
    with pytest.raises(PilotError, match="preflight_identity"):
        run_baseline(db, expected_preflight_id="wrong", expected_planned_pages=1,
                     api_key="x", transport=Transport([]), now=NOW)
    with pytest.raises(PilotError, match="planned_pages"):
        run_baseline(db, expected_preflight_id=pre.run_id, expected_planned_pages=2,
                     api_key="x", transport=Transport([]), now=NOW)


def test_metadata_drift_stops_and_marks_incomplete(tmp_path):
    db = store(tmp_path)
    pre = run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key="x",
                        transport=Transport([full_page(101, 2)]), now=NOW)
    tx = Transport([full_page(101, 2),
                    envelope([row(f"2027{number:010d}") for number in range(100)],
                             page=2, total_count=201, total_page=3)])
    with pytest.raises(PilotError, match="metadata_drift"):
        run_baseline(db, expected_preflight_id=pre.run_id, expected_planned_pages=2,
                     api_key="x", transport=tx, now=NOW)
    assert len(tx.calls) == 2
    assert db.connection.execute("select status from runs where kind='backfill'").fetchone() == ("incomplete",)


def test_incremental_requires_baseline_complete_and_overflow_is_not_complete(tmp_path):
    db = store(tmp_path)
    with pytest.raises(PilotError, match="baseline_required"):
        run_incremental(db, today=date(2026, 8, 3), api_key="x", transport=Transport([]), now=NOW)
    seed_baseline(db)
    receipt = run_incremental(db, today=date(2026, 8, 3), api_key="x",
                              transport=Transport([full_page(4001, 41)]), now=NOW)
    assert receipt.status == "overflow" and receipt.end_de == "20260803"


def test_sixth_pilot_tick_rejects_before_transport(tmp_path):
    db = store(tmp_path)
    seed_baseline(db)
    tx = Transport([envelope([]) for _ in range(5)])
    for _ in range(5):
        assert run_incremental(db, today=date(2026, 8, 3), api_key="x",
                               transport=tx, now=NOW).status == "complete"
    with pytest.raises(PilotError, match="pilot_cap"):
        run_incremental(db, today=date(2026, 8, 3), api_key="x",
                        transport=tx, now=NOW)
    assert len(tx.calls) == 5
    assert db.connection.execute("select count(*) from runs where kind='incremental'").fetchone() == (5,)
    assert db.connection.execute(
        "select group_concat(ordinal) from (select ordinal from pilot_invocations order by ordinal)"
    ).fetchone() == ("1,2,3,4,5",)


def test_operate_ignores_exhausted_pilot_ledger(tmp_path):
    db = store(tmp_path)
    for _ in range(5):
        run = db.claim_pilot_incremental("20260803", "20260803", NOW)
        assert run is not None
        db.finish(run.run_id, "complete", NOW)
    before = db.connection.execute("select count(*) from pilot_invocations").fetchone()
    today = date(2026, 8, 3)
    migrated_for(db, today)
    tx = Transport([envelope([])])

    result = operate_tick(db, today=today, transport=tx)

    assert result["status"] == "complete"
    assert latest_partition_run(db).status == "complete"
    assert tx.calls[0].begin == tx.calls[0].end == today
    assert db.connection.execute("select count(*) from pilot_invocations").fetchone() == before


def test_operate_accepts_append_only_growth_and_extends_the_plan_without_cap(tmp_path):
    db = store(tmp_path)
    db.connection.execute("PRAGMA synchronous=OFF")
    today = date(2026, 8, 14)
    migrated_for(db, today)
    tx = Transport([
        full_page(2200, 22),
        *[
            envelope([row(f"2027{number:010d}") for number in range(100)],
                     page=page_no, total_count=2500, total_page=25)
            for page_no in range(2, 26)
        ],
    ])

    result = operate_tick(db, today=today, transport=tx)

    assert result["status"] == "complete"
    receipt = latest_partition_run(db)
    assert receipt.status == "complete"
    assert receipt.planned_pages == 25
    assert receipt.fetched_pages == 25
    assert [request.page_no for request in tx.calls] == list(range(1, 26))


@pytest.mark.parametrize(
    "changed",
    [
        envelope([row(f"2027{number:010d}") for number in range(100)],
                 page=2, total_count=2100, total_page=21),
        envelope([row(f"2027{number:010d}") for number in range(100)],
                 page=2, total_count=2199, total_page=22),
    ],
)
def test_operate_still_fails_closed_on_metadata_shrinkage(tmp_path, changed):
    db = store(tmp_path)
    today = date(2026, 8, 14)
    migrated_for(db, today)
    tx = Transport([full_page(2200, 22), changed])

    with pytest.raises(PilotError, match="metadata_drift"):
        operate_tick(db, today=today, transport=tx)

    receipt = latest_partition_run(db)
    assert (receipt.status, receipt.fetched_pages) == ("incomplete", 1)
    assert db.connection.execute(
        "select status from day_partitions where part_date='20260814'"
    ).fetchone() == ("failed",)


def test_operate_fails_closed_on_growth_then_partial_shrinkage(tmp_path):
    db = store(tmp_path)
    today = date(2026, 8, 14)
    migrated_for(db, today)
    tx = Transport([
        full_page(2200, 22),
        envelope([row(f"2027{number:010d}") for number in range(100)],
                 page=2, total_count=2500, total_page=25),
        envelope([row(f"2028{number:010d}") for number in range(100)],
                 page=3, total_count=2300, total_page=23),
    ])

    with pytest.raises(PilotError, match="metadata_drift"):
        operate_tick(db, today=today, transport=tx)

    receipt = latest_partition_run(db)
    assert (receipt.status, receipt.fetched_pages) == ("incomplete", 2)
    assert db.connection.execute(
        "select status from day_partitions where part_date='20260814'"
    ).fetchone() == ("failed",)


def test_pre_transport_contract_error_is_failed_and_does_not_wedge(tmp_path):
    db, tx = store(tmp_path), Transport([])
    with pytest.raises(BulkContractError, match="window_too_large"):
        run_preflight(db, fixed_cutoff=date(2026, 10, 15), api_key="x",
                      transport=tx, now=NOW)
    assert tx.calls == []
    assert db.connection.execute("select status from runs").fetchone() == ("failed",)
    assert db.claim("preflight", "20260701", "20260802", NOW).status == "claimed"


def test_wrapper_uses_monotonic_minute_starts_without_real_wait():
    clock = [10.0]
    starts, sleeps = [], []
    def monotonic(): return clock[0]
    def sleep(seconds):
        sleeps.append(seconds)
        clock[0] += seconds
    def call():
        starts.append(clock[0])
        clock[0] += 2.5
        return 0
    assert run_five_polls(call=call, monotonic=monotonic, sleep=sleep) == 0
    assert starts == [10.0, 70.0, 130.0, 190.0, 250.0]
    assert sleeps == [57.5, 57.5, 57.5, 57.5]


def test_wrapper_stops_on_nonzero_without_compensating():
    calls = []
    def call():
        calls.append(None)
        return 2 if len(calls) == 3 else 0
    assert run_five_polls(call=call, monotonic=lambda: 0, sleep=lambda _: None) == 2
    assert len(calls) == 3


def test_wrapper_preserves_minimum_spacing_after_slow_poll():
    clock = [10.0]
    starts = []
    durations = iter((70.0, 1.0, 1.0, 1.0, 1.0))
    def call():
        starts.append(clock[0])
        clock[0] += next(durations)
        return 0
    run_five_polls(call=call, monotonic=lambda: clock[0],
                   sleep=lambda seconds: clock.__setitem__(0, clock[0] + seconds))
    assert starts == [10.0, 80.0, 140.0, 200.0, 260.0]
    assert all(later - earlier >= 60 for earlier, later in zip(starts, starts[1:]))


def test_failed_and_skipped_pilot_claims_are_not_refunded(tmp_path):
    db = store(tmp_path)
    seed_baseline(db)
    with pytest.raises(PilotError, match="timeout"):
        run_incremental(db, today=date(2026, 8, 3), api_key="x",
                        transport=Transport([BulkTransportFailure("timeout")]), now=NOW)
    active = db.claim("preflight", "20260701", "20260802", NOW)
    assert active.status == "claimed"
    skipped = run_incremental(db, today=date(2026, 8, 3), api_key="x",
                              transport=Transport([]), now=NOW)
    assert skipped.status == "skipped_overlap"
    assert db.connection.execute("select ordinal from pilot_invocations order by ordinal").fetchall() == [(1,), (2,)]


def test_seoul_calendar_date_crosses_utc_midnight_boundary():
    assert _seoul_calendar_date(datetime(2026, 8, 2, 16, tzinfo=timezone.utc)) == date(2026, 8, 3)
    with pytest.raises(ValueError, match="aware_now_required"):
        _seoul_calendar_date(datetime(2026, 8, 3))


def test_secret_never_appears_in_repr_db_or_receipt(tmp_path):
    secret = "VERY_SECRET_SENTINEL"
    db = store(tmp_path)
    receipt = run_preflight(db, fixed_cutoff=date(2026, 8, 2), api_key=secret,
                            transport=Transport([envelope([])]), now=NOW)
    assert secret not in repr(receipt)
    assert secret.encode() not in (tmp_path / "private" / "bulk.sqlite3").read_bytes()
