"""Durable-store contract for the KR fundamentals enrichment ledger (SDS §6/§7)."""

import sqlite3
import stat
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.records.opendart_fundamentals_store import (
    NON_TERMINAL_STATUSES,
    SCHEMA,
    STATUS_FAILED_EXHAUSTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_FAILED_ROW_BOUND,
    STATUS_IN_ATTEMPT,
    STATUS_NO_DATA,
    STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR,
    STATUS_SKIPPED_YEAR_UNPARSEABLE,
    STATUS_SUCCESS,
    TERMINAL_STATUSES,
    CapReachedError,
    FundamentalsStoreError,
    OpenDartFundamentalsStore,
)

NOW = datetime(2026, 9, 3, 4, 0, tzinfo=UTC)

REPORT = dict(
    rcept_no="20260903000001",
    corp_code="00126380",
    stock_code="005930",
    corp_name="Samsung",
    bsns_year="2025",
    provenance="HOLDING",
)


def make_store(tmp_path):
    return OpenDartFundamentalsStore.init_create(
        tmp_path / "private" / "opendart-fundamentals.sqlite3", now=NOW
    )


def begin(store, *, rcept_no=REPORT["rcept_no"], now=NOW):
    fields = dict(REPORT, rcept_no=rcept_no)
    return store.begin_attempt(
        endpoint="fnlttSinglAcnt", request_identity="identity-1", now=now, **fields
    )


def test_init_create_private_meta_and_absence_gate(tmp_path):
    path = tmp_path / "private" / "opendart-fundamentals.sqlite3"
    store = OpenDartFundamentalsStore.init_create(path, now=NOW)
    try:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert store.meta("schema_version") == "1"
        assert store.meta("activation_epoch") == NOW.isoformat()
        assert store.meta("cumulative_start_cap") == "10"
        assert store.meta("enabled") == "1"
        assert store.meta("created_at") == NOW.isoformat()
    finally:
        store.close()
    with pytest.raises(FundamentalsStoreError, match="store_exists"):
        OpenDartFundamentalsStore.init_create(path, now=NOW)
    with pytest.raises(FundamentalsStoreError, match="store_absent"):
        OpenDartFundamentalsStore.open(tmp_path / "private" / "missing.sqlite3")
    reopened = OpenDartFundamentalsStore.open(path)
    reopened.close()
    reader = OpenDartFundamentalsStore.open_readonly(path)
    try:
        with pytest.raises(sqlite3.Error):
            reader.set_meta("enabled", "0")
    finally:
        reader.close()


def test_terminal_and_non_terminal_sets_are_the_normative_closed_sets():
    assert TERMINAL_STATUSES == frozenset(
        {
            STATUS_SUCCESS,
            STATUS_FAILED_EXHAUSTED,
            STATUS_FAILED_ROW_BOUND,
            STATUS_SKIPPED_YEAR_UNPARSEABLE,
            STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR,
        }
    )
    assert NON_TERMINAL_STATUSES == frozenset(
        {
            STATUS_NO_DATA,
            STATUS_FAILED_RETRYABLE,
            STATUS_IN_ATTEMPT,
        }
    )
    assert not TERMINAL_STATUSES & NON_TERMINAL_STATUSES


def test_schema_records_bsns_year_text_and_scan_precondition_comments():
    # m-6: the preconditions live as SQL comments next to the columns.
    assert "bsns_year TEXT NOT NULL" in SCHEMA
    assert "stock_code IS NOT NULL" in SCHEMA
    assert "4-digit" in SCHEMA


def test_begin_attempt_is_write_ahead_reserved_before_any_wire_io(tmp_path):
    store = make_store(tmp_path)
    try:
        start_id = begin(store)
        # A crash right after Txn A leaves both durable rows behind.
        row = store.report(REPORT["rcept_no"])
        assert row["status"] == STATUS_IN_ATTEMPT
        assert row["attempts"] == 1
        assert row["last_attempt_at"] == NOW.isoformat()
        starts = store.connection.execute(
            "select rcept_no,endpoint,request_identity,outcome "
            "from provider_starts where start_id=?",
            (start_id,),
        ).fetchone()
        assert starts == (
            REPORT["rcept_no"],
            "fnlttSinglAcnt",
            "identity-1",
            "RESERVED",
        )
        assert store.cumulative_starts() == 1
    finally:
        store.close()


def test_cap_refusal_is_write_ahead_and_leaves_no_report_row(tmp_path):
    store = make_store(tmp_path)
    try:
        store.set_meta("cumulative_start_cap", "1")
        begin(store)
        with pytest.raises(CapReachedError):
            begin(store, rcept_no="20260903000002")
        # m-2: the refused candidate has no per-report ledger row at all.
        assert store.report("20260903000002") is None
        assert store.cumulative_starts() == 1
    finally:
        store.close()


def test_settle_start_disagreement_and_double_settle_refuse(tmp_path):
    store = make_store(tmp_path)
    try:
        start_id = begin(store)
        store.settle_start(start_id, "success", body_sha256="ab" * 32)
        with pytest.raises(FundamentalsStoreError, match="reservation_disagreement"):
            store.settle_start(start_id, "success")
        with pytest.raises(FundamentalsStoreError, match="reservation_disagreement"):
            store.settle_start(start_id + 99, "success")
    finally:
        store.close()


def test_settle_start_with_reserve_next_is_one_settlement_and_one_reservation(tmp_path):
    store = make_store(tmp_path)
    try:
        first = begin(store)
        second = store.settle_start(
            first,
            "success",
            body_sha256="cd" * 32,
            reserve_next=(REPORT["rcept_no"], "fnlttSinglAcntAll", "identity-2"),
            now=NOW,
        )
        assert second is not None and second != first
        outcomes = dict(
            store.connection.execute(
                "select start_id,outcome from provider_starts"
            ).fetchall()
        )
        assert outcomes == {first: "success", second: "RESERVED"}
        assert store.cumulative_starts() == 2
    finally:
        store.close()


def test_cap_trip_on_reserve_next_still_settles_the_first_start(tmp_path):
    store = make_store(tmp_path)
    try:
        store.set_meta("cumulative_start_cap", "1")
        first = begin(store)
        with pytest.raises(CapReachedError):
            store.settle_start(
                first,
                "success",
                body_sha256="ef" * 32,
                reserve_next=(REPORT["rcept_no"], "fnlttSinglAcntAll", "identity-2"),
                now=NOW,
            )
        outcomes = dict(
            store.connection.execute(
                "select start_id,outcome from provider_starts"
            ).fetchall()
        )
        assert outcomes == {first: "success"}
    finally:
        store.close()


def test_success_settlement_persists_payload_rows_in_the_same_transaction(tmp_path):
    store = make_store(tmp_path)
    try:
        start_id = begin(store)
        store.settle_start(start_id, "success", body_sha256="12" * 32)
        rows = ('{"account_nm":"a"}', '{"account_nm":"b"}')
        store.settle_report_terminal(
            rcept_no=REPORT["rcept_no"],
            status=STATUS_SUCCESS,
            last_outcome="success",
            now=NOW,
            endpoint="fnlttSinglAcnt",
            payload_rows=rows,
        )
        assert store.is_settled(REPORT["rcept_no"]) is True
        stored = store.connection.execute(
            "select endpoint,ordinal,row_json from fundamental_payload_rows "
            "order by ordinal"
        ).fetchall()
        assert stored == [
            ("fnlttSinglAcnt", 1, rows[0]),
            ("fnlttSinglAcnt", 2, rows[1]),
        ]
        # §7: a second terminal settlement refuses loudly.
        with pytest.raises(FundamentalsStoreError, match="report_settled"):
            store.settle_report_terminal(
                rcept_no=REPORT["rcept_no"],
                status=STATUS_SUCCESS,
                last_outcome="success",
                now=NOW,
                endpoint="fnlttSinglAcnt",
                payload_rows=rows,
            )
        # And the payload primary key refuses silent duplication.
        with pytest.raises(sqlite3.IntegrityError):
            store.insert_payload_rows(REPORT["rcept_no"], "fnlttSinglAcnt", rows)
    finally:
        store.close()


def test_payload_rows_require_success_status(tmp_path):
    store = make_store(tmp_path)
    try:
        begin(store)
        with pytest.raises(FundamentalsStoreError, match="payload_requires_success"):
            store.settle_report_terminal(
                rcept_no=REPORT["rcept_no"],
                status=STATUS_FAILED_RETRYABLE,
                last_outcome="timeout",
                now=NOW,
                endpoint="fnlttSinglAcnt",
                payload_rows=('{"account_nm":"a"}',),
            )
        with pytest.raises(FundamentalsStoreError, match="payload_rows_required"):
            store.settle_report_terminal(
                rcept_no=REPORT["rcept_no"],
                status=STATUS_SUCCESS,
                last_outcome="success",
                now=NOW,
                endpoint="fnlttSinglAcnt",
            )
    finally:
        store.close()


@pytest.mark.parametrize(
    "status",
    sorted(TERMINAL_STATUSES - {STATUS_SUCCESS})
    + [STATUS_NO_DATA, STATUS_FAILED_RETRYABLE],
)
def test_settlement_upserts_absent_rows_with_zero_attempts(tmp_path, status):
    store = make_store(tmp_path)
    try:
        store.settle_report_terminal(
            rcept_no=REPORT["rcept_no"],
            status=status,
            last_outcome="classified",
            now=NOW,
            corp_code=REPORT["corp_code"],
            stock_code=REPORT["stock_code"],
            corp_name=REPORT["corp_name"],
            bsns_year=REPORT["bsns_year"],
            provenance=REPORT["provenance"],
        )
        row = store.report(REPORT["rcept_no"])
        assert row["status"] == status
        assert row["attempts"] == 0
        assert store.is_settled(REPORT["rcept_no"]) is (status in TERMINAL_STATUSES)
    finally:
        store.close()


def test_reconcile_stale_settles_crash_leftovers_and_keeps_cap_accounting(tmp_path):
    store = make_store(tmp_path)
    try:
        stale_at = NOW - timedelta(minutes=90)
        begin(store, now=stale_at)
        fresh_id = begin(store, rcept_no="20260903000002", now=NOW)
        result = store.reconcile_stale(
            now=NOW, stale_before=NOW - timedelta(minutes=60)
        )
        assert result == {"stale_starts": 1, "stale_reports": 1}
        outcomes = dict(
            store.connection.execute(
                "select start_id,outcome from provider_starts"
            ).fetchall()
        )
        assert outcomes[fresh_id] == "RESERVED"
        assert "stale_crash" in outcomes.values()
        stale_report = store.report(REPORT["rcept_no"])
        assert stale_report["status"] == STATUS_FAILED_RETRYABLE
        assert stale_report["last_outcome"] == "stale_crash"
        fresh_report = store.report("20260903000002")
        assert fresh_report["status"] == STATUS_IN_ATTEMPT
        # §7: stale starts keep counting toward the cumulative cap.
        assert store.cumulative_starts() == 2
    finally:
        store.close()


def test_tick_events_and_status_summary_are_closed(tmp_path):
    store = make_store(tmp_path)
    try:
        store.append_tick_event(
            at=NOW, outcome="processed", reason=None, reports_processed=1, starts=2
        )
        store.append_tick_event(
            at=NOW,
            outcome="refused_cap_reached",
            reason="cap",
            reports_processed=0,
            starts=0,
        )
        with pytest.raises(FundamentalsStoreError, match="unknown_tick_outcome"):
            store.append_tick_event(
                at=NOW, outcome="surprise", reason=None, reports_processed=0, starts=0
            )
        summary = store.status_summary()
        assert set(summary) == {"meta", "reports", "cumulative_starts", "tick_events"}
        assert summary["meta"]["cumulative_start_cap"] == "10"
        assert summary["cumulative_starts"] == 0
        assert [event["outcome"] for event in summary["tick_events"]] == [
            "refused_cap_reached",
            "processed",
        ]
    finally:
        store.close()


def test_latest_success_rows_prefers_the_maximum_rcept_no(tmp_path):
    store = make_store(tmp_path)
    try:
        for rcept_no, marker in (
            ("20260903000001", "original"),
            ("20260904000009", "correction"),
        ):
            store.begin_attempt(
                endpoint="fnlttSinglAcnt",
                request_identity="i",
                now=NOW,
                **dict(REPORT, rcept_no=rcept_no),
            )
            store.settle_report_terminal(
                rcept_no=rcept_no,
                status=STATUS_SUCCESS,
                last_outcome="success",
                now=NOW,
                endpoint="fnlttSinglAcnt",
                payload_rows=('{"account_nm":"%s"}' % marker,),
            )
        assert store.latest_success_rows("00126380", "2025") == (
            '{"account_nm":"correction"}',
        )
        assert store.latest_success_rows("00126380", "2024") == ()
    finally:
        store.close()
