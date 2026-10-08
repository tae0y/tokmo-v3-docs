"""SDS §12 behavior matrix for the bulk-triggered KR fundamentals enrichment.

Every provider start is counted through an injected fake transport; no test
constructs a real opener and no test performs network I/O.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

import tokmo.opendart_bulk_composition as composition
from tokmo.facts.opendart import (
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
)
from tokmo.facts.opendart_bulk import Disclosure
from tokmo.facts.opendart_targeted import KrRelevanceResolution
from tokmo.programs import quant_kr_source_contract
from tokmo.programs.opendart_fundamentals_enrichment import (
    ENRICHMENT_MIN_BUDGET_SECONDS,
    MAX_ATTEMPTS_PER_REPORT,
    MAX_PERSISTED_ROWS,
    MAX_REPORTS_PER_TICK,
    MAX_STARTS_PER_REPORT,
    PERSISTED_FIELD_NAMES,
    REATTEMPT_SPACING_HOURS,
    SCAN_WINDOW_DAYS,
    STALE_ATTEMPT_MINUTES,
    TICK_HARD_CAP_MINUTES,
    classify_periodic_report,
    run_fundamentals_enrichment,
    sanitize_fundamentals_rows,
)
from tokmo.records.opendart_disclosures import DisclosureStore
from tokmo.records.opendart_fundamentals_store import (
    OpenDartFundamentalsStore,
    STATUS_FAILED_EXHAUSTED,
    STATUS_FAILED_RETRYABLE,
    STATUS_FAILED_ROW_BOUND,
    STATUS_IN_ATTEMPT,
    STATUS_NO_DATA,
    STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR,
    STATUS_SKIPPED_YEAR_UNPARSEABLE,
    STATUS_SUCCESS,
    TERMINAL_STATUSES,
)
from tokmo.records.holdings_snapshots import HoldingsSnapshotReadError
from tokmo.values.holdings import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
)

NOW = datetime(2026, 9, 3, 4, 0, tzinfo=timezone.utc)
EPOCH = NOW - timedelta(hours=1)
RELEVANCE = KrRelevanceResolution(members=(
    ("005930", "HOLDING"), ("000660", "WATCHLIST"),
))
ANNUAL = "사업보고서 (2025.12)"


class FakeTransport:
    def __init__(self, outcomes=()):
        self.outcomes = list(outcomes)
        self.requests = []

    def trace_json(self, request):
        self.requests.append(request)
        return self.outcomes.pop(0)


def success(rows, status="000"):
    return _OpenDartTransportSuccess(body=json.dumps(
        {"status": status, "message": "RAW PROVIDER TEXT",
         "list": rows}).encode())


def provider_stop(status):
    return _OpenDartTransportSuccess(body=json.dumps(
        {"status": status, "message": "RAW PROVIDER TEXT"}).encode())


def cfs_row(corp="00126380", **extra):
    row = {"corp_code": corp, "fs_div": "CFS",
           "account_id": "ifrs-full_Assets", "account_nm": "자산총계",
           "thstrm_amount": "1000", "bsns_year": "2025"}
    row.update(extra)
    return row


def make_stores(tmp_path):
    bulk = DisclosureStore.open(tmp_path / "private" / "bulk.sqlite3")
    enrich = OpenDartFundamentalsStore.init_create(
        tmp_path / "private" / "opendart-fundamentals.sqlite3", now=EPOCH)
    return bulk, enrich


def seed(bulk, rcept_no, *, report_nm=ANNUAL, stock="005930",
         corp="00126380", observed=NOW, rcept_dt="20260415"):
    disclosure = Disclosure(corp, "Samsung", stock, "Y", report_nm,
                            rcept_no, "filer", rcept_dt, None)
    assert bulk.observe("seed-run", disclosure, observed) == "new"


def run(bulk, enrich, transport, *, now=NOW, relevance=RELEVANCE,
        deadline=None, clock=None, scope_note=None):
    moment = now
    return run_fundamentals_enrichment(
        bulk_store=bulk, enrichment_store=enrich, relevance=relevance,
        api_key="fixture-key", transport=transport, now=moment,
        clock=clock if clock is not None else (lambda: moment),
        deadline=deadline if deadline is not None
        else moment + timedelta(minutes=5),
        scope_note=scope_note)


def tick_events(enrich):
    return enrich.connection.execute(
        "select outcome,reports_processed,starts from tick_events "
        "order by event_id").fetchall()


def close(*stores):
    for store in stores:
        store.close()


# ---------------------------------------------------------------- §3.1 matcher


def test_classifier_accepts_annual_reports_and_stripped_correction_tags():
    for name in (ANNUAL, "[기재정정]사업보고서 (2025.12)",
                 "[기재정정][첨부추가]사업보고서(2025.12)"):
        decided = classify_periodic_report(name, "20260415")
        assert (decided.kind, decided.bsns_year) == ("annual", "2025")


@pytest.mark.parametrize("name", [
    "반기보고서 (2026.06)", "분기보고서 (2026.03)", "주요사항보고서",
    "제27기 사업보고서 (2025.12)", "사업보고서", "사업보고서 (2025.12) 추가",
])
def test_classifier_never_guesses_non_annual_names(name):
    assert classify_periodic_report(name, "20260415").kind == "not_candidate"


def test_classifier_settles_nonstandard_fiscal_year_and_implausible_years():
    assert classify_periodic_report(
        "사업보고서 (2026.03)", "20260615").kind == "nonstandard_fiscal_year"
    for name, rcept_dt in (("사업보고서 (2030.12)", "20260415"),
                           ("사업보고서 (2023.12)", "20260415"),
                           (ANNUAL, "bad-date")):
        assert classify_periodic_report(name, rcept_dt).kind == "year_unparseable"


def test_bounds_match_the_sds_contract():
    assert (MAX_REPORTS_PER_TICK, MAX_STARTS_PER_REPORT,
            MAX_ATTEMPTS_PER_REPORT, REATTEMPT_SPACING_HOURS,
            SCAN_WINDOW_DAYS, MAX_PERSISTED_ROWS, STALE_ATTEMPT_MINUTES,
            ENRICHMENT_MIN_BUDGET_SECONDS, TICK_HARD_CAP_MINUTES,
            ) == (2, 2, 3, 24, 14, 1024, 60, 120, 25)


# ------------------------------------------------------------ §12 rows 3–12


def test_no_candidates_is_idle_with_no_tick_event(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["status"] == "idle"
        assert summary["starts"] == 0
        assert transport.requests == []
        assert tick_events(enrich) == []
    finally:
        close(bulk, enrich)


def test_held_annual_report_single_resolves_major_and_persists(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([success([cfs_row()])])
    try:
        summary = run(bulk, enrich, transport)
        assert summary == {"status": "processed", "reports_processed": 1,
                           "starts": 1, "scope_note": None}
        request = transport.requests[0]
        assert (request.endpoint, request.corp_code, request.business_year,
                request.report_code, request.fs_div) == (
            "fnlttSinglAcnt", "00126380", "2025", "11011", None)
        assert request.api_key == "fixture-key"
        report = enrich.report("20260903000001")
        assert (report["status"], report["provenance"],
                report["bsns_year"]) == (STATUS_SUCCESS, "HOLDING", "2025")
        persisted = enrich.latest_success_rows("00126380", "2025")
        assert len(persisted) == 1
        stored_row = json.loads(persisted[0])
        assert stored_row["fs_div"] == "CFS"
        assert "message" not in stored_row
        start = enrich.connection.execute(
            "select outcome,body_sha256 from provider_starts").fetchone()
        assert start[0] == "success" and len(start[1]) == 64
        assert tick_events(enrich) == [("processed", 1, 1)]
    finally:
        close(bulk, enrich)


def test_major_unresolved_single_falls_back_to_all_and_admits_missing_fs_div(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    all_row = {"corp_code": "00126380", "account_id": "ifrs-full_Assets",
               "account_nm": "자산총계", "thstrm_amount": "1000",
               "thstrm_add_amount": "10"}
    transport = FakeTransport([
        success([{"corp_code": "00126380", "fs_div": "OFS",
                  "account_nm": "자산총계"}]),
        success([all_row]),
    ])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 2
        assert transport.requests[1].endpoint == "fnlttSinglAcntAll"
        assert transport.requests[1].fs_div == "CFS"
        assert enrich.report("20260903000001")["status"] == STATUS_SUCCESS
        persisted = enrich.latest_success_rows("00126380", "2025")
        assert json.loads(persisted[0])["thstrm_add_amount"] == "10"
        endpoint = enrich.connection.execute(
            "select distinct endpoint from fundamental_payload_rows").fetchall()
        assert endpoint == [("fnlttSinglAcntAll",)]
    finally:
        close(bulk, enrich)


def test_explicit_non_cfs_row_on_all_settles_schema_drift(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([
        success([{"corp_code": "00126380", "fs_div": "OFS"}]),
        success([cfs_row(), {"corp_code": "00126380", "fs_div": "OFS"}]),
    ])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 2
        report = enrich.report("20260903000001")
        assert (report["status"], report["last_outcome"]) == (
            STATUS_FAILED_RETRYABLE, "schema_drift")
        assert enrich.latest_success_rows("00126380", "2025") == ()
    finally:
        close(bulk, enrich)


@pytest.mark.parametrize("status", sorted(TERMINAL_STATUSES))
def test_any_terminal_report_identity_yields_zero_starts_forever(tmp_path, status):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    payload = ('{"account_nm":"a"}',) if status == STATUS_SUCCESS else ()
    enrich.settle_report_terminal(
        rcept_no="20260903000001", status=status, last_outcome="prior",
        now=EPOCH, corp_code="00126380", stock_code="005930",
        corp_name="Samsung", bsns_year="2025", provenance="HOLDING",
        endpoint="fnlttSinglAcnt", payload_rows=payload)
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 0
        assert transport.requests == []
        assert enrich.report("20260903000001")["status"] == status
    finally:
        close(bulk, enrich)


@pytest.mark.parametrize("status", [STATUS_NO_DATA, STATUS_FAILED_RETRYABLE])
def test_non_terminal_report_inside_spacing_window_is_skipped(tmp_path, status):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    enrich.begin_attempt(
        rcept_no="20260903000001", corp_code="00126380", stock_code="005930",
        corp_name="Samsung", bsns_year="2025", provenance="HOLDING",
        endpoint="fnlttSinglAcnt", request_identity="i",
        now=NOW - timedelta(hours=1))
    first = enrich.connection.execute(
        "select max(start_id) from provider_starts").fetchone()[0]
    enrich.settle_start(first, "timeout")
    enrich.settle_report_terminal(
        rcept_no="20260903000001", status=status, last_outcome="timeout",
        now=NOW - timedelta(hours=1))
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 0
        assert transport.requests == []
        assert enrich.report("20260903000001")["status"] == status
    finally:
        close(bulk, enrich)


def test_fresh_in_attempt_row_is_skipped_this_tick(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    enrich.begin_attempt(
        rcept_no="20260903000001", corp_code="00126380", stock_code="005930",
        corp_name="Samsung", bsns_year="2025", provenance="HOLDING",
        endpoint="fnlttSinglAcnt", request_identity="i",
        now=NOW - timedelta(minutes=5))
    transport = FakeTransport()
    try:
        assert run(bulk, enrich, transport)["starts"] == 0
        assert enrich.report("20260903000001")["status"] == STATUS_IN_ATTEMPT
    finally:
        close(bulk, enrich)


def test_non_member_annual_report_leaves_no_ledger_row(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001", stock="999999", corp="00999999")
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["status"] == "idle"
        assert transport.requests == []
        assert enrich.report("20260903000001") is None
    finally:
        close(bulk, enrich)


def test_non_periodic_report_for_member_leaves_no_ledger_row(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001", report_nm="주요사항보고서")
    transport = FakeTransport()
    try:
        assert run(bulk, enrich, transport)["status"] == "idle"
        assert enrich.report("20260903000001") is None
    finally:
        close(bulk, enrich)


def test_correction_filing_is_a_fresh_trigger_and_wins_the_latest_read(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([success([cfs_row(account_nm="원본")])])
    try:
        assert run(bulk, enrich, transport)["starts"] == 1
        seed(bulk, "20260904000009", report_nm="[기재정정]사업보고서 (2025.12)",
             observed=NOW + timedelta(hours=1))
        replay = FakeTransport([success([cfs_row(account_nm="정정")])])
        summary = run(bulk, enrich, replay, now=NOW + timedelta(hours=2))
        assert summary["starts"] == 1
        assert enrich.report("20260904000009")["status"] == STATUS_SUCCESS
        latest = enrich.latest_success_rows("00126380", "2025")
        assert json.loads(latest[0])["account_nm"] == "정정"
    finally:
        close(bulk, enrich)


def test_watchlist_only_symbol_carries_watchlist_provenance(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001", stock="000660", corp="00164779")
    transport = FakeTransport([success([cfs_row(corp="00164779")])])
    try:
        assert run(bulk, enrich, transport)["starts"] == 1
        assert enrich.report("20260903000001")["provenance"] == "WATCHLIST"
    finally:
        close(bulk, enrich)


@pytest.mark.parametrize("report_nm,status", [
    ("사업보고서 (2026.03)", STATUS_SKIPPED_NONSTANDARD_FISCAL_YEAR),
    ("사업보고서 (2030.12)", STATUS_SKIPPED_YEAR_UNPARSEABLE),
])
def test_skip_classes_settle_durably_with_zero_starts(tmp_path, report_nm, status):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001", report_nm=report_nm)
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 0
        assert transport.requests == []
        report = enrich.report("20260903000001")
        assert report["status"] == status
        assert report["attempts"] == 0
        assert enrich.is_settled("20260903000001") is True
        # Replay: settled skip classes are terminal — zero starts forever.
        assert run(bulk, enrich, FakeTransport())["starts"] == 0
    finally:
        close(bulk, enrich)


# ----------------------------------------------------------- §12 rows 13–18


@pytest.mark.parametrize("code", [
    "timeout", "connection_failure", "http_429", "auth_rejection",
    "http_5xx", "bad_request", "redirect", "body_too_large",
])
def test_transport_failure_consumes_the_start_and_settles_retryable(tmp_path, code):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([_OpenDartTransportFailure(code=code)])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 1
        report = enrich.report("20260903000001")
        assert (report["status"], report["last_outcome"]) == (
            STATUS_FAILED_RETRYABLE, code)
        assert enrich.connection.execute(
            "select outcome from provider_starts").fetchone() == (code,)
    finally:
        close(bulk, enrich)


def test_schema_drift_body_consumes_the_start(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([_OpenDartTransportSuccess(body=b"not json")])
    try:
        assert run(bulk, enrich, transport)["starts"] == 1
        assert enrich.report("20260903000001")["last_outcome"] == "schema_drift"
    finally:
        close(bulk, enrich)


def test_provider_013_is_non_terminal_and_exhausts_after_three_attempts(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    try:
        for attempt in range(1, MAX_ATTEMPTS_PER_REPORT + 1):
            moment = NOW + timedelta(hours=(attempt - 1) * 25)
            summary = run(bulk, enrich, FakeTransport([provider_stop("013")]),
                          now=moment)
            assert summary["starts"] == 1
            report = enrich.report("20260903000001")
            assert report["attempts"] == attempt
            assert report["last_outcome"] == "provider_no_data"
            if attempt < MAX_ATTEMPTS_PER_REPORT:
                assert report["status"] == STATUS_NO_DATA
                assert enrich.is_settled("20260903000001") is False
        assert report["status"] == STATUS_FAILED_EXHAUSTED
        assert enrich.is_settled("20260903000001") is True
        follow_up = run(bulk, enrich, FakeTransport(),
                        now=NOW + timedelta(hours=76))
        assert follow_up["starts"] == 0
    finally:
        close(bulk, enrich)


def test_reattempt_before_24h_spacing_is_refused(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    try:
        run(bulk, enrich, FakeTransport([_OpenDartTransportFailure(
            code="timeout")]))
        early = run(bulk, enrich, FakeTransport(),
                    now=NOW + timedelta(hours=23))
        assert early["starts"] == 0
        assert enrich.report("20260903000001")["attempts"] == 1
    finally:
        close(bulk, enrich)


def test_third_failed_attempt_settles_exhausted_then_zero_starts(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    try:
        for attempt in range(1, MAX_ATTEMPTS_PER_REPORT + 1):
            run(bulk, enrich,
                FakeTransport([_OpenDartTransportFailure(code="timeout")]),
                now=NOW + timedelta(hours=(attempt - 1) * 25))
        report = enrich.report("20260903000001")
        assert report["status"] == STATUS_FAILED_EXHAUSTED
        assert report["attempts"] == MAX_ATTEMPTS_PER_REPORT
        assert run(bulk, enrich, FakeTransport(),
                   now=NOW + timedelta(hours=80))["starts"] == 0
    finally:
        close(bulk, enrich)


def test_two_reports_per_tick_and_the_third_is_deferred_to_the_next(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    for ordinal in (1, 2, 3):
        seed(bulk, f"2026090300000{ordinal}",
             observed=NOW - timedelta(minutes=4 - ordinal))
    transport = FakeTransport([
        success([{"corp_code": "00126380", "fs_div": "OFS"}]),
        success([cfs_row()]),
        success([cfs_row()]),
    ])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["reports_processed"] == MAX_REPORTS_PER_TICK
        assert summary["starts"] == 3
        assert enrich.report("20260903000003") is None
        follow_up = run(bulk, enrich, FakeTransport([success([cfs_row()])]),
                        now=NOW + timedelta(minutes=30))
        assert follow_up["starts"] == 1
        assert enrich.report("20260903000003")["status"] == STATUS_SUCCESS
    finally:
        close(bulk, enrich)


def test_exhausted_cumulative_cap_refuses_with_no_per_report_row(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    enrich.set_meta("cumulative_start_cap", "0")
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["status"] == "refused_cap_reached"
        assert summary["starts"] == 0
        assert transport.requests == []
        assert enrich.report("20260903000001") is None
        assert tick_events(enrich) == [("refused_cap_reached", 0, 0)]
    finally:
        close(bulk, enrich)


def test_cap_trip_on_the_fallback_reservation_settles_retryable(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    enrich.set_meta("cumulative_start_cap", "1")
    transport = FakeTransport([
        success([{"corp_code": "00126380", "fs_div": "OFS"}]),
    ])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["status"] == "refused_cap_reached"
        assert summary["starts"] == 1
        report = enrich.report("20260903000001")
        assert (report["status"], report["last_outcome"]) == (
            STATUS_FAILED_RETRYABLE, "cap_reached")
    finally:
        close(bulk, enrich)


# ----------------------------------------------------------- §12 rows 22–26


def test_crash_leftovers_reconcile_and_stale_starts_keep_counting(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001", observed=NOW - timedelta(hours=2))
    enrich.begin_attempt(
        rcept_no="20260903000001", corp_code="00126380", stock_code="005930",
        corp_name="Samsung", bsns_year="2025", provenance="HOLDING",
        endpoint="fnlttSinglAcnt", request_identity="i",
        now=NOW - timedelta(minutes=90))
    transport = FakeTransport()
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 0
        assert transport.requests == []
        report = enrich.report("20260903000001")
        assert (report["status"], report["last_outcome"]) == (
            STATUS_FAILED_RETRYABLE, "stale_crash")
        assert enrich.connection.execute(
            "select outcome from provider_starts").fetchone() == ("stale_crash",)
        assert enrich.cumulative_starts() == 1
    finally:
        close(bulk, enrich)


def test_rows_before_epoch_or_outside_the_scan_window_are_never_candidates(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260801000001", observed=EPOCH - timedelta(minutes=1))
    seed(bulk, "20260810000001",
         observed=NOW - timedelta(days=SCAN_WINDOW_DAYS, minutes=1))
    transport = FakeTransport()
    try:
        assert run(bulk, enrich, transport)["status"] == "idle"
        assert transport.requests == []
    finally:
        close(bulk, enrich)


def test_deferred_when_the_tick_hard_cap_is_already_spent(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport()
    late = NOW + timedelta(minutes=TICK_HARD_CAP_MINUTES)
    try:
        summary = run(bulk, enrich, transport, clock=lambda: late,
                      deadline=late + timedelta(seconds=1))
        assert summary["status"] == "deferred_deadline"
        assert summary["starts"] == 0
        assert transport.requests == []
        assert tick_events(enrich) == [("deferred_deadline", 0, 0)]
        assert enrich.report("20260903000001") is None
    finally:
        close(bulk, enrich)


def test_enrichment_still_runs_inside_the_floor_after_an_exhausted_bulk_deadline(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    started = NOW + timedelta(minutes=20)
    transport = FakeTransport([success([cfs_row()])])
    try:
        summary = run(
            bulk, enrich, transport, clock=lambda: started,
            deadline=composition.enrichment_deadline(NOW, started))
        assert summary["starts"] == 1
        assert enrich.report("20260903000001")["status"] == STATUS_SUCCESS
    finally:
        close(bulk, enrich)


def test_enrichment_deadline_formula_floor_and_hard_cap():
    floor = composition.enrichment_deadline(NOW, NOW + timedelta(minutes=20))
    assert floor == NOW + timedelta(
        minutes=20, seconds=ENRICHMENT_MIN_BUDGET_SECONDS)
    capped = composition.enrichment_deadline(
        NOW, NOW + timedelta(minutes=24, seconds=30))
    assert capped == NOW + timedelta(minutes=TICK_HARD_CAP_MINUTES)
    early = composition.enrichment_deadline(NOW, NOW + timedelta(minutes=1))
    assert early == NOW + timedelta(minutes=20)


def test_row_flood_settles_terminal_row_bound_and_never_retries(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    flood = [cfs_row() for _ in range(MAX_PERSISTED_ROWS + 1)]
    try:
        summary = run(bulk, enrich, FakeTransport([success(flood)]))
        assert summary["starts"] == 1
        report = enrich.report("20260903000001")
        assert report["status"] == STATUS_FAILED_ROW_BOUND
        assert enrich.is_settled("20260903000001") is True
        assert enrich.latest_success_rows("00126380", "2025") == ()
        assert run(bulk, enrich, FakeTransport(),
                   now=NOW + timedelta(hours=25))["starts"] == 0
    finally:
        close(bulk, enrich)


def test_response_row_for_another_corp_fails_closed(tmp_path):
    bulk, enrich = make_stores(tmp_path)
    seed(bulk, "20260903000001")
    transport = FakeTransport([success([cfs_row(corp="99999999")])])
    try:
        summary = run(bulk, enrich, transport)
        assert summary["starts"] == 1
        report = enrich.report("20260903000001")
        assert (report["status"], report["last_outcome"]) == (
            STATUS_FAILED_RETRYABLE, "request_identity_disagreement")
        assert enrich.latest_success_rows("00126380", "2025") == ()
    finally:
        close(bulk, enrich)


# --------------------------------------------------------------- §12 row 27


def test_sanitizer_keeps_only_the_closed_field_set_and_equals_ep1_sets():
    rows = [
        {"corp_code": "00126380", "message": "RAW PROVIDER TEXT",
         "thstrm_amount": "1", "account_id": 123, "unknown_key": "x",
         "account_nm": "자산총계"},
        "not a dict",
    ]
    sanitized = sanitize_fundamentals_rows(rows)
    assert len(sanitized) == 1
    decoded = json.loads(sanitized[0])
    assert set(decoded) <= PERSISTED_FIELD_NAMES
    assert decoded == {"corp_code": "00126380", "thstrm_amount": "1",
                       "account_nm": "자산총계"}
    assert PERSISTED_FIELD_NAMES == quant_kr_source_contract.SAFE_FIELD_NAMES
    assert quant_kr_source_contract.SAFE_ROW_FIELDS < PERSISTED_FIELD_NAMES


# ------------------------------------------- composition seam (§12 rows 1–2,
# 19–21, 24a, 28) and operator commands (§10)


def operate_home(tmp_path):
    home = tmp_path / "home"
    config_dir = home / ".config" / "tokmo-v3"
    config_dir.mkdir(parents=True)
    config_dir.chmod(0o700)
    credential = config_dir / "opendart.env"
    credential.write_text("OPENDART_API_KEY=fixture-key\n")
    credential.chmod(0o600)
    return home


def snapshot_fixture():
    holding = TossHoldingFact(
        symbol="005930", quantity="1", avg_price="1",
        avg_price_field="averagePrice", currency="KRW", market_raw="KR",
        market_field="market", name=None, asset_type=None)
    return CompleteHoldingsSnapshot(
        snapshot_id="fixture", source="toss-holdings-v1", captured_at=NOW,
        discovered_account_count=1,
        accounts=(HoldingsSnapshotAccount(1, (holding,)),),
        content_hash="a" * 64)


class StubHoldingsStore:
    toss_error = None
    pension_error = HoldingsSnapshotReadError("not_found")

    def __init__(self, path, *, clock):
        self.path = path

    def read_latest(self):
        if type(self).toss_error is not None:
            raise type(self).toss_error
        return snapshot_fixture()

    def read_latest_pension_snapshot(self):
        if type(self).pension_error is not None:
            raise type(self).pension_error
        return snapshot_fixture()


def wire_operate(monkeypatch, *, bulk_status="complete", outcomes=()):
    monkeypatch.setattr(composition, "utc_now", lambda: NOW)
    monkeypatch.setattr(composition, "run_operate",
                        lambda *args, **kwargs: {"status": bulk_status,
                                                 "partitions": []})
    monkeypatch.setattr(composition, "OpenDartBulkLiveTransport",
                        lambda: object())
    transport = FakeTransport(list(outcomes))
    monkeypatch.setattr(composition, "OpenDartLiveTransport",
                        lambda: transport)
    monkeypatch.setattr(composition, "HoldingsSnapshotStore",
                        StubHoldingsStore)
    monkeypatch.setattr(StubHoldingsStore, "toss_error", None)
    monkeypatch.setattr(StubHoldingsStore, "pension_error",
                        HoldingsSnapshotReadError("not_found"))
    return transport


def operate(home):
    return composition.execute_opendart_bulk(
        home=home, command="operate", now=NOW)


def test_absent_store_means_inactive_provider_start_zero(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    transport = wire_operate(monkeypatch)
    result = operate(home)
    assert result["status"] == "complete"
    assert result["enrichment"] == {"status": "inactive"}
    assert transport.requests == []
    assert not (home / ".local" / "share" / "tokmo-v3"
                / "opendart-fundamentals.sqlite3").exists()


def test_paused_store_yields_zero_starts_and_zero_writes(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    transport = wire_operate(monkeypatch)
    assert composition.execute_opendart_bulk(
        home=home, command="enrich-init", now=NOW)["status"] == "initialized"
    assert composition.execute_opendart_bulk(
        home=home, command="enrich-pause", now=NOW)["status"] == "paused"
    result = operate(home)
    assert result["enrichment"] == {"status": "paused"}
    assert transport.requests == []
    summary = composition.execute_opendart_bulk(
        home=home, command="enrich-status", now=NOW)
    assert summary["meta"]["enabled"] == "0"
    assert summary["tick_events"] == []
    assert summary["cumulative_starts"] == 0


def test_enrich_command_round_trip_and_set_cap(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    wire_operate(monkeypatch)
    absent = composition.execute_opendart_bulk(
        home=home, command="enrich-status", now=NOW)
    assert absent == {"status": "absent"}
    created = composition.execute_opendart_bulk(
        home=home, command="enrich-init", now=NOW)
    assert created["activation_epoch"] == NOW.isoformat()
    assert created["cumulative_start_cap"] == 10
    with pytest.raises(Exception):
        composition.execute_opendart_bulk(
            home=home, command="enrich-init", now=NOW)
    assert composition.execute_opendart_bulk(
        home=home, command="enrich-resume", now=NOW)["status"] == "resumed"
    capped = composition.execute_opendart_bulk(
        home=home, command="enrich-set-cap", now=NOW,
        expected_planned_pages=20)
    assert capped == {"status": "cap_set", "cumulative_start_cap": 20}
    summary = composition.execute_opendart_bulk(
        home=home, command="enrich-status", now=NOW)
    assert summary["meta"]["cumulative_start_cap"] == "20"
    with pytest.raises(ValueError, match="cap_value_required"):
        composition.execute_opendart_bulk(
            home=home, command="enrich-set-cap", now=NOW)


def test_source_failure_refuses_the_tick_but_bulk_stays_complete(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    transport = wire_operate(monkeypatch)
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)
    monkeypatch.setattr(StubHoldingsStore, "toss_error",
                        HoldingsSnapshotReadError("store_corrupt"))
    result = operate(home)
    assert result["status"] == "complete"
    assert result["enrichment"]["status"] == "refused_inputs_unavailable"
    assert transport.requests == []
    summary = composition.execute_opendart_bulk(
        home=home, command="enrich-status", now=NOW)
    assert [event["outcome"] for event in summary["tick_events"]] == [
        "refused_inputs_unavailable"]


def test_pension_not_found_is_tolerated_absent_scope(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    wire_operate(monkeypatch)
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)
    result = operate(home)
    assert result["enrichment"]["status"] == "idle"
    assert result["enrichment"]["scope_note"] == "pension_scope_absent"


def test_operate_still_defers_after_the_real_deadline(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    transport = wire_operate(monkeypatch)
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)
    monkeypatch.setattr(composition, "utc_now", lambda: NOW + timedelta(days=1))
    result = operate(home)
    assert result["enrichment"]["status"] == "deferred_deadline"
    assert transport.requests == []


def test_pension_corruption_refuses_the_tick(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    wire_operate(monkeypatch)
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)
    monkeypatch.setattr(StubHoldingsStore, "pension_error",
                        HoldingsSnapshotReadError("store_locked"))
    result = operate(home)
    assert result["enrichment"]["status"] == "refused_inputs_unavailable"


def test_bulk_skipped_overlap_never_attempts_enrichment(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    transport = wire_operate(monkeypatch, bulk_status="skipped_overlap")
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)
    result = operate(home)
    assert result["status"] == "skipped_overlap"
    assert result["enrichment"] == {"status": "not_attempted"}
    assert transport.requests == []


def test_unexpected_enrichment_error_never_escapes_into_the_bulk_path(tmp_path, monkeypatch):
    home = operate_home(tmp_path)
    wire_operate(monkeypatch)
    composition.execute_opendart_bulk(home=home, command="enrich-init", now=NOW)

    def explode(**_kwargs):
        raise RuntimeError("RAW INTERNAL DETAIL")

    monkeypatch.setattr(composition, "run_fundamentals_enrichment", explode)
    result = operate(home)
    assert result["status"] == "complete"
    assert result["enrichment"] == {"status": "error"}
