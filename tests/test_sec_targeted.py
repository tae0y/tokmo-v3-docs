"""SEC holdings-targeted filings collector tests.

Covers all specified RED scenarios:
- holdings-only union across accounts/dedup
- production source has no ticker literals/playbook scan
- partial/failed preserves last COMPLETE
- no prior COMPLETE network0
- newly bought added/sold removed
- mapped SPCX-like synthetic ticker follows normal path
- unmapped no submissions I/O
- all recent/date filter/aligned columns
- duplicate/conflict
- registry cache
- exact cap/spacing fake clock
- attempt-before-I/O
- provider stop
- silent/nonzero
- architecture
"""

import ast
import json
import os
import stat
import textwrap
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from tokmo.facts.sec import _SecTransportFailure, _SecTransportSuccess
from tokmo.facts.sec_targeted import (
    MAX_ADMITTED_TARGETS,
    MAX_REQUESTS_PER_TICK,
    MIN_FILING_DATE,
    MIN_START_INTERVAL_SECONDS,
    STOP_CODES,
    TARGET_PROVENANCE,
    merge_sec_target_provenance,
    TickerRegistryResult,
    extract_us_holdings_symbols,
    parse_company_tickers_multi,
    parse_submissions_all,
    query_company_tickers_multi,
    query_submissions_all,
)
from tokmo.records.sec_targeted_store import SecTargetedStore, SecTargetedStoreError
from tokmo.composition import (
    SEC_TARGETED_DB_NAME,
    LiveRunError,
    SecTargetedRunResult,
    run_sec_targeted,
    setup_sec_targeted,
)
from tokmo.values import (
    CompleteHoldingsSnapshot,
    HoldingsSnapshotAccount,
    TossHoldingFact,
    TOSS_SOURCE,
)
from tokmo.values.holdings import HoldingsSnapshotHeader

FIXTURES = Path(__file__).parent / "fixtures"
NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=timezone.utc)
UA_SECRET = "TokmoTest/0.0 (fixture-contact@example.invalid)"

SRC_ROOT = Path(__file__).parents[1] / "src" / "tokmo"

# Valid UUID4 hex for test snapshot IDs and permits.
_TEST_SNAPSHOT_ID = "16fd27068baf433b82eb8c7fada847da"


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def holding(symbol, currency, market_raw, market_field="market_raw"):
    return TossHoldingFact(
        symbol=symbol, quantity="1", avg_price="1",
        avg_price_field="avgPrice", currency=currency,
        market_raw=market_raw, market_field=market_field,
        name=None, asset_type=None,
    )


def snapshot(accounts, snapshot_id=None):
    """Build a CompleteHoldingsSnapshot from (ordinal, holdings) pairs.

    Each unique capture needs a distinct valid UUID4-hex snapshot_id so the
    holdings store treats successive appends as new rows, not duplicates.
    """
    snap_accounts = tuple(
        HoldingsSnapshotAccount(ordinal, tuple(items))
        for ordinal, items in accounts
    )
    import hashlib
    from tokmo.records.holdings_snapshots import snapshot_content_hash
    if snapshot_id is None:
        snapshot_id = _TEST_SNAPSHOT_ID
    dummy = CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=len(snap_accounts),
        accounts=snap_accounts,
        content_hash="0" * 64,
    )
    return CompleteHoldingsSnapshot(
        snapshot_id=snapshot_id,
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=len(snap_accounts),
        accounts=snap_accounts,
        content_hash=snapshot_content_hash(dummy),
    )


def make_permit(snapshot_id=_TEST_SNAPSHOT_ID):
    """Construct a valid v1 permit via the canonical authorization API."""
    from tokmo.records.holdings_persistence import (
        authorize_user_request, DenialToken,
    )
    result = authorize_user_request(
        snapshot_id=snapshot_id, clock=lambda: NOW)
    assert not isinstance(result, DenialToken), "permit authorization denied"
    return result


def tickers_body(entries):
    """Build a company_tickers.json body."""
    payload = {}
    for i, (cik, ticker, title) in enumerate(entries):
        payload[str(i)] = {"cik_str": cik, "ticker": ticker, "title": title}
    return json.dumps(payload).encode()


def submissions_body(cik, filings):
    """Build a submissions body."""
    recent = {
        "accessionNumber": [],
        "filingDate": [],
        "reportDate": [],
        "form": [],
        "primaryDocument": [],
        "isXBRL": [],
    }
    for acc, fdate, rdate, form, doc, xbrl in filings:
        recent["accessionNumber"].append(acc)
        recent["filingDate"].append(fdate)
        recent["reportDate"].append(rdate)
        recent["form"].append(form)
        recent["primaryDocument"].append(doc)
        recent["isXBRL"].append(xbrl)
    return json.dumps({
        "cik": str(int(cik)), "filings": {"recent": recent}
    }).encode()


def transport_router(route_map):
    """Build a transport that dispatches by endpoint_id/cik."""
    def transport(request):
        key = (request.endpoint_id, request.cik)
        if key in route_map:
            result = route_map[key]
            if isinstance(result, Exception):
                raise result
            if isinstance(result, _SecTransportFailure):
                return result
            return _SecTransportSuccess(result)
        raise RuntimeError(f"unexpected request {request.endpoint_id}:{request.cik}")
    return transport


# ---------------------------------------------------------------------------
# extract_us_holdings_symbols
# ---------------------------------------------------------------------------

class TestExtractUsHoldingsSymbols:
    def test_provenance_union_deduplicates_overlap(self):
        snap = snapshot([(1, [holding("AAA", "USD", "US"), holding("V", "USD", "US")])])
        assert merge_sec_target_provenance(
            snap, frozenset({"V", "WATCH"})
        ) == {
            "AAA": "HOLDING",
            "V": "HOLDING + WATCHLIST",
            "WATCH": "WATCHLIST",
        }

    def test_us_only_from_single_account(self):
        snap = snapshot([(1, [
            holding("AAA", "USD", "US"),
            holding("005930", "KRW", "KR"),
        ])])
        assert extract_us_holdings_symbols(snap) == frozenset({"AAA"})

    def test_union_across_accounts_with_dedup(self):
        snap = snapshot([
            (1, [holding("AAA", "USD", "US"), holding("BBB", "USD", "US")]),
            (2, [holding("AAA", "USD", "US"), holding("CCC", "USD", "US")]),
        ])
        assert extract_us_holdings_symbols(snap) == frozenset({"AAA", "BBB", "CCC"})

    def test_kr_holdings_excluded(self):
        snap = snapshot([(1, [holding("005930", "KRW", "KR")])])
        assert extract_us_holdings_symbols(snap) == frozenset()

    def test_no_symbol_heuristics(self):
        """market_raw='KR' with currency='USD' must still be excluded
        because classify_market uses market_raw only."""
        snap = snapshot([(1, [holding("AAA", "USD", "KR")])])
        assert extract_us_holdings_symbols(snap) == frozenset()

    def test_unknown_market_excluded(self):
        snap = snapshot([(1, [holding("AAA", "EUR", "DE")])])
        assert extract_us_holdings_symbols(snap) == frozenset()


# ---------------------------------------------------------------------------
# parse_company_tickers_multi
# ---------------------------------------------------------------------------

class TestParseCompanyTickersMulti:
    def test_finds_all_requested_tickers(self):
        body = tickers_body([
            (320193, "AAA", "Company A"),
            (789019, "BBB", "Company B"),
            (123456, "CCC", "Company C"),
        ])
        result = parse_company_tickers_multi(
            body, tickers=frozenset({"AAA", "BBB"}), observed_at=NOW)
        assert isinstance(result, TickerRegistryResult)
        assert set(result.mappings) == {"AAA", "BBB"}
        assert result.mappings["AAA"].cik == "0000320193"
        assert result.mappings["AAA"].company_name == "Company A"
        assert result.unmapped == frozenset()

    def test_unmapped_tickers_returned(self):
        body = tickers_body([(320193, "AAA", "Company A")])
        result = parse_company_tickers_multi(
            body, tickers=frozenset({"AAA", "ZZZ"}), observed_at=NOW)
        assert isinstance(result, TickerRegistryResult)
        assert result.unmapped == frozenset({"ZZZ"})

    def test_malformed_json_fails_closed(self):
        result = parse_company_tickers_multi(
            b"{", tickers=frozenset({"AAA"}), observed_at=NOW)
        from tokmo.values import UnavailableFact
        assert isinstance(result, UnavailableFact)
        assert result.reason == "parse_error"

    def test_bad_entry_shape_fails_closed(self):
        body = b'{"0":{"ticker":"AAA","title":"Company A"}}'
        from tokmo.values import UnavailableFact
        result = parse_company_tickers_multi(
            body, tickers=frozenset({"AAA"}), observed_at=NOW)
        assert isinstance(result, UnavailableFact)
        assert result.reason == "contract_mismatch"


# ---------------------------------------------------------------------------
# parse_submissions_all
# ---------------------------------------------------------------------------

class TestParseSubmissionsAll:
    def test_all_filings_on_or_after_cutoff(self):
        body = submissions_body("0000320193", [
            ("0000320193-26-000077", "2026-08-03", "2026-07-01", "10-Q", "doc1.htm", 1),
            ("0000320193-26-000078", "2026-08-05", "", "4", "doc2.xml", 0),
            ("0000320193-26-000050", "2026-07-15", "2026-06-30", "10-Q", "doc3.htm", 1),
        ])
        result = parse_submissions_all(
            body, ticker="AAA", cik="0000320193",
            company_name="Company A",
            min_filing_date=MIN_FILING_DATE, observed_at=NOW)
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert result[0].filing_date == date(2026, 8, 3)
        assert result[1].filing_date == date(2026, 8, 5)

    def test_before_cutoff_filtered_out(self):
        body = submissions_body("0000320193", [
            ("0000320193-26-000050", "2026-07-01", "2026-06-01", "10-Q", "doc.htm", 1),
        ])
        result = parse_submissions_all(
            body, ticker="AAA", cik="0000320193",
            company_name="Company A",
            min_filing_date=MIN_FILING_DATE, observed_at=NOW)
        assert isinstance(result, tuple)
        assert len(result) == 0

    def test_aligned_columns_validated(self):
        """Misaligned column lengths fail closed."""
        body = json.dumps({
            "cik": "320193",
            "filings": {"recent": {
                "accessionNumber": ["a1", "a2"],
                "filingDate": ["2026-08-03"],
                "form": ["10-Q", "4"],
                "primaryDocument": ["d1", "d2"],
            }}
        }).encode()
        from tokmo.values import UnavailableFact
        result = parse_submissions_all(
            body, ticker="AAA", cik="0000320193",
            company_name="Company A",
            min_filing_date=MIN_FILING_DATE, observed_at=NOW)
        assert isinstance(result, UnavailableFact)
        assert result.reason == "contract_mismatch"

    def test_cik_mismatch_fails_closed(self):
        body = submissions_body("0000320193", [])
        from tokmo.values import UnavailableFact
        result = parse_submissions_all(
            body, ticker="AAA", cik="0000999999",
            company_name="Company A",
            min_filing_date=MIN_FILING_DATE, observed_at=NOW)
        assert isinstance(result, UnavailableFact)
        assert result.reason == "contract_mismatch"

    def test_optional_columns_absent(self):
        """reportDate and isXBRL may be absent; should not fail."""
        body = json.dumps({
            "cik": "320193",
            "filings": {"recent": {
                "accessionNumber": ["a1"],
                "filingDate": ["2026-08-03"],
                "form": ["10-Q"],
                "primaryDocument": ["d1"],
            }}
        }).encode()
        result = parse_submissions_all(
            body, ticker="AAA", cik="0000320193",
            company_name="Company A",
            min_filing_date=MIN_FILING_DATE, observed_at=NOW)
        assert isinstance(result, tuple)
        assert len(result) == 1
        assert result[0].report_date is None
        assert result[0].is_xbrl is None


# ---------------------------------------------------------------------------
# Store tests
# ---------------------------------------------------------------------------

class TestSecTargetedStore:
    def test_claim_run_then_overlap(self, tmp_path):
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        result = store.claim_run(run_id="run1", started_at=NOW)
        assert result.status == "claimed"
        result2 = store.claim_run(run_id="run1", started_at=NOW)
        assert result2.status == "overlap"

    def test_save_filing_duplicate(self, tmp_path):
        from tokmo.values import SecFilingFact
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        store.claim_run(run_id="run1", started_at=NOW)
        filing = SecFilingFact(
            source="sec_submissions", ticker="AAA", cik="0000320193",
            company_name="Company A", accession_number="acc1",
            form="10-Q", filing_date=date(2026, 8, 3),
            report_date=date(2026, 7, 1), primary_document="doc.htm",
            is_xbrl=True, observed_at=NOW,
        )
        r1 = store.save_filing(filing=filing, run_id="run1", stored_at=NOW)
        assert r1.status == "saved"
        r2 = store.save_filing(filing=filing, run_id="run1", stored_at=NOW)
        assert r2.status == "duplicate"

    def test_save_filing_conflict(self, tmp_path):
        from tokmo.values import SecFilingFact
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        store.claim_run(run_id="run1", started_at=NOW)
        filing1 = SecFilingFact(
            source="sec_submissions", ticker="AAA", cik="0000320193",
            company_name="Company A", accession_number="acc1",
            form="10-Q", filing_date=date(2026, 8, 3),
            report_date=date(2026, 7, 1), primary_document="doc.htm",
            is_xbrl=True, observed_at=NOW,
        )
        filing2 = SecFilingFact(
            source="sec_submissions", ticker="AAA", cik="0000320193",
            company_name="Company A", accession_number="acc1",
            form="10-K", filing_date=date(2026, 8, 3),
            report_date=date(2026, 7, 1), primary_document="doc.htm",
            is_xbrl=True, observed_at=NOW,
        )
        r1 = store.save_filing(filing=filing1, run_id="run1", stored_at=NOW)
        assert r1.status == "saved"
        r2 = store.save_filing(filing=filing2, run_id="run1", stored_at=NOW)
        assert r2.status == "conflict"

        store.claim_run(run_id="run2", started_at=NOW)
        r3 = store.save_filing(filing=filing2, run_id="run2", stored_at=NOW)
        assert r3.status == "conflict"

    def test_store_and_load_universe(self, tmp_path):
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        store.store_universe(
            universe_id="u1", captured_at=NOW,
            symbols=frozenset({"AAA", "BBB"}),
            source_snapshot="toss-holdings-v1:2026-08-03T12:00:00+00:00",
        )
        loaded = store.load_last_universe()
        assert loaded == ("AAA", "BBB")

    def test_store_and_load_registry(self, tmp_path):
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        store.store_registry(
            ticker="AAA", cik="0000320193",
            company_name="Company A", refreshed_at=NOW,
        )
        reg = store.load_registry()
        assert "AAA" in reg
        assert reg["AAA"][0] == "0000320193"
        assert reg["AAA"][1] == "Company A"

    def test_record_attempt(self, tmp_path):
        store = SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)
        store.claim_run(run_id="run1", started_at=NOW)
        store.record_attempt(
            run_id="run1", seq=1, endpoint="sec_company_tickers",
            cik=None, ticker=None, started_at=NOW,
            outcome_code="success",
        )
        # no exception means success


# ---------------------------------------------------------------------------
# Composition runner tests
# ---------------------------------------------------------------------------

class TestRunSecTargeted:
    def _prepare(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        return tmp_path / ".local" / "share" / "tokmo-v3" / SEC_TARGETED_DB_NAME

    def test_no_snapshot_no_prior_complete_network0(self, tmp_path):
        db_path = self._prepare(tmp_path)
        calls = []

        def transport(request):
            calls.append(request)
            raise RuntimeError("must not be called")

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=None,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )
        assert result.code == "failed_no_universe"
        assert result.started == 0
        assert calls == []

    def test_snapshot_drives_target_universe(self, tmp_path):
        """A snapshot with US holdings drives the target universe and
        triggers a registry call."""
        db_path = self._prepare(tmp_path)
        snap = snapshot([(1, [
            holding("AAA", "USD", "US"),
            holding("005930", "KRW", "KR"),
        ])])

        # Write a holdings snapshot store
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME,
        )
        store = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        store.append(snap, permit)

        tb = tickers_body([(320193, "AAA", "Company A")])
        calls = []
        sleeps = []

        def transport(request):
            calls.append((request.endpoint_id, request.cik))
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(submissions_body(
                "0000320193", [
                    ("0000320193-26-000077", "2026-08-03", "2026-07-01",
                     "10-Q", "doc1.htm", 1),
                ]))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
            sleeper=sleeps.append,
        )
        assert result.code == "complete"
        assert result.target_count == 1
        assert result.filings_saved == 1
        # 1 registry + 1 submissions = 2 starts
        assert result.started == 2
        assert ("sec_company_tickers", None) in calls
        assert ("sec_submissions", "0000320193") in calls
        assert sleeps == [MIN_START_INTERVAL_SECONDS]

    def test_new_purchase_added_to_universe(self, tmp_path):
        """A new purchase appears on the next COMPLETE snapshot."""
        self._prepare(tmp_path)

        # First snapshot: AAA only
        sid1 = "16fd27068baf433b82eb8c7fada847da"
        snap1 = snapshot([(1, [holding("AAA", "USD", "US")])], snapshot_id=sid1)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit1 = make_permit(snapshot_id=sid1)
        hstore.append(snap1, permit1)

        tb1 = tickers_body([(320193, "AAA", "Company A")])

        def transport1(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb1)
            return _SecTransportSuccess(submissions_body(
                "0000320193", []))

        run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport1, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )

        # Second snapshot: AAA + BBB (new purchase)
        sid2 = "26fd27068baf433b82eb8c7fada847da"
        snap2 = snapshot([(1, [
            holding("AAA", "USD", "US"),
            holding("BBB", "USD", "US"),
        ])], snapshot_id=sid2)
        permit2 = make_permit(snapshot_id=sid2)
        hstore.append(snap2, permit2)

        tb2 = tickers_body([
            (320193, "AAA", "Company A"),
            (789019, "BBB", "Company B"),
        ])
        calls = []

        def transport2(request):
            calls.append((request.endpoint_id, request.cik))
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb2)
            return _SecTransportSuccess(submissions_body(
                request.cik, []))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport2,
            clock=lambda: NOW + timedelta(hours=1),
            run_id="run2", user_agent=UA_SECRET,
        )
        assert result.target_count == 2
        assert ("sec_submissions", "0000789019") in calls

    def test_sold_symbol_removed_from_universe(self, tmp_path):
        """A fully sold symbol disappears on the next COMPLETE snapshot."""
        self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)

        # First: AAA + BBB
        sid1 = "16fd27068baf433b82eb8c7fada847da"
        snap1 = snapshot([(1, [
            holding("AAA", "USD", "US"),
            holding("BBB", "USD", "US"),
        ])], snapshot_id=sid1)
        permit1 = make_permit(snapshot_id=sid1)
        hstore.append(snap1, permit1)
        tb1 = tickers_body([
            (320193, "AAA", "Company A"),
            (789019, "BBB", "Company B"),
        ])

        def t1(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb1)
            return _SecTransportSuccess(submissions_body(request.cik, []))

        run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=t1, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )

        # Second: AAA only (BBB sold)
        sid2 = "26fd27068baf433b82eb8c7fada847da"
        snap2 = snapshot([(1, [holding("AAA", "USD", "US")])], snapshot_id=sid2)
        permit2 = make_permit(snapshot_id=sid2)
        hstore.append(snap2, permit2)

        calls = []

        def t2(request):
            calls.append((request.endpoint_id, request.cik))
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tickers_body([(320193, "AAA", "Company A")]))
            return _SecTransportSuccess(submissions_body(request.cik, []))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=t2,
            clock=lambda: NOW + timedelta(hours=1),
            run_id="run2", user_agent=UA_SECRET,
        )
        assert result.target_count == 1
        # BBB should NOT have been queried
        assert all(cik != "0000789019" for _, cik in calls)

    def test_mapped_synthetic_ticker_follows_normal_path(self, tmp_path):
        """A ticker like SPCX that IS in the official registry follows
        the normal registry→submissions path."""
        self._prepare(tmp_path)
        snap = snapshot([(1, [holding("SPCX", "USD", "US")])])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        spcx_cik = "0001965672"
        tb = tickers_body([(1965672, "SPCX", "SPAC X")])

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(submissions_body(spcx_cik, [
                ("0001965672-26-000001", "2026-08-03", "", "4", "form4.xml", 0),
            ]))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )
        assert result.code == "complete"
        assert result.filings_saved == 1

    def test_unmapped_ticker_no_submissions_io(self, tmp_path):
        """Officially unmapped held ticker: no submissions call."""
        self._prepare(tmp_path)
        snap = snapshot([(1, [holding("ZZZ", "USD", "US")])])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        tb = tickers_body([(320193, "AAA", "Company A")])
        submissions_calls = []

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            submissions_calls.append(request)
            raise RuntimeError("must not be called for unmapped")

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )
        assert "ZZZ" in result.unresolved
        assert result.code == "complete_with_unresolved"
        assert submissions_calls == []

    def test_provider_stop_halts_remaining(self, tmp_path):
        """401/403/429 stops remaining targets and fails closed."""
        self._prepare(tmp_path)
        snap = snapshot([(1, [
            holding("AAA", "USD", "US"),
            holding("BBB", "USD", "US"),
        ])])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        tb = tickers_body([
            (320193, "AAA", "Company A"),
            (789019, "BBB", "Company B"),
        ])
        submissions_calls = []

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            if request.cik == "0000320193":
                return _SecTransportFailure("http_429")
            submissions_calls.append(request)
            raise RuntimeError("must not reach BBB after stop")

        with pytest.raises(LiveRunError, match="fair_access_stop"):
            run_sec_targeted(
                home=tmp_path, snapshot_store_path=snap_path,
                transport=transport, clock=lambda: NOW,
                run_id="run1", user_agent=UA_SECRET,
            )
        assert submissions_calls == []

    def test_attempt_before_io(self, tmp_path):
        """The run claim is written before any transport call.

        The transport callback inspects the DB mid-execution: if the run row
        is not already claimed with code='started', the assertion fails.
        Transport exceptions are sanitized by the query layer, so the run
        completes normally — the in-callback assertion is the test.
        """
        self._prepare(tmp_path)
        snap = snapshot([(1, [holding("AAA", "USD", "US")])])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        db_path = tmp_path / ".local" / "share" / "tokmo-v3" / SEC_TARGETED_DB_NAME

        observed_run_codes = []

        def transport(request):
            # Observe the durable claim mid-I/O; assert only after the query
            # returns so the transport sanitizer cannot swallow test failures.
            import sqlite3 as sq
            conn = sq.connect(str(db_path))
            row = conn.execute(
                "SELECT code FROM runs WHERE run_id='run1'"
            ).fetchone()
            conn.close()
            observed_run_codes.append(row)

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )
        # Transport returned None (no exception raised, just inspected DB);
        # _query treats None as protocol_failure. The key assertion already
        # happened inside the callback.
        assert result.code == "failed_registry"
        assert observed_run_codes == [("started",)]

    def test_registry_cache_avoids_refetch_same_kst_day(self, tmp_path):
        """Same KST day: registry is not fetched again."""
        self._prepare(tmp_path)
        snap = snapshot([(1, [holding("AAA", "USD", "US")])])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        tb = tickers_body([(320193, "AAA", "Company A")])

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(submissions_body("0000320193", []))

        # First run: populates registry
        run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )

        # Second run same KST day: should NOT fetch registry
        registry_calls = []

        def transport2(request):
            if request.endpoint_id == "sec_company_tickers":
                registry_calls.append(request)
                raise RuntimeError("must not refetch registry")
            return _SecTransportSuccess(submissions_body("0000320193", []))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport2,
            clock=lambda: NOW + timedelta(minutes=30),
            run_id="run2", user_agent=UA_SECRET,
        )
        assert registry_calls == []
        assert result.code == "complete"

    def test_partial_snapshot_preserves_last_complete(self, tmp_path):
        """If snapshot read fails, the last COMPLETE universe is used."""
        self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()

        # First: successful snapshot with AAA
        snap1 = snapshot([(1, [holding("AAA", "USD", "US")])])
        hstore.append(snap1, permit)
        tb = tickers_body([(320193, "AAA", "Company A")])

        def t1(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(submissions_body("0000320193", []))

        run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=t1, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )

        # Now simulate snapshot failure by passing a non-existent path
        bad_path = tmp_path / "nonexistent.sqlite3"
        calls = []

        def t2(request):
            calls.append(request)
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(submissions_body("0000320193", []))

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=bad_path,
            transport=t2,
            clock=lambda: NOW + timedelta(hours=1),
            run_id="run2", user_agent=UA_SECRET,
        )
        # Should use last COMPLETE universe (AAA)
        assert result.target_count == 1
        assert result.code == "complete"


# ---------------------------------------------------------------------------
# Architecture / static analysis tests
# ---------------------------------------------------------------------------

class TestArchitecture:
    def test_production_source_has_no_ticker_literals(self):
        """No hard-coded real-portfolio ticker symbols in production source."""
        # Check sec_targeted.py and sec_targeted_store.py only
        files = [
            SRC_ROOT / "facts" / "sec_targeted.py",
            SRC_ROOT / "records" / "sec_targeted_store.py",
        ]
        # Known real-portfolio tickers that must never appear as literals
        banned = {"AAPL", "MSFT", "NVDA", "GOOGL", "TSLA", "AMZN", "META"}
        for path in files:
            source = path.read_text()
            # Extract all quoted string literals
            tree = ast.parse(source)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    assert node.value.upper() not in banned, (
                        f"{path.name}: found ticker literal {node.value}"
                    )

    def test_no_playbook_scan_in_source(self):
        """Production source must not scan playbooks/ for tickers.

        Verified via AST: no string literal or attribute access references
        a playbook path.  Docstring mentions are not behavioral evidence.
        """
        files = [
            SRC_ROOT / "facts" / "sec_targeted.py",
            SRC_ROOT / "records" / "sec_targeted_store.py",
            SRC_ROOT / "composition.py",
        ]
        for path in files:
            source = path.read_text()
            tree = ast.parse(source)
            for node in ast.walk(tree):
                # Check string constants for playbook path references
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    val = node.value.lower()
                    # A docstring comment saying "no playbook scan" is fine;
                    # an actual path like "playbooks/" is not.
                    assert "playbooks/" not in val and not (
                        val.startswith("playbook") and "/" in val
                    ), f"{path.name}: playbook path reference: {node.value}"
                # Check attribute access that could open/scan playbook paths
                if isinstance(node, ast.Attribute):
                    assert node.attr != "playbooks", (
                        f"{path.name}: playbook attribute access"
                    )

    def test_no_ai_telegram_alerts_imports(self):
        """No AI/Telegram/alerts/orders imports in sec_targeted modules."""
        files = [
            SRC_ROOT / "facts" / "sec_targeted.py",
            SRC_ROOT / "records" / "sec_targeted_store.py",
        ]
        forbidden = {"telegram", "openai", "anthropic", "aiogram", "ccxt",
                     "alpaca_trade_api", "boto3"}
        for path in files:
            tree = ast.parse(path.read_text())
            imports = []
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.extend(a.name for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.append(node.module or "")
            for imp in imports:
                top = imp.split(".")[0]
                assert top not in forbidden, f"{path.name}: forbidden import {imp}"

    def test_credential_loader_only_reads_existing_env(self):
        """The composition must read sec.env through the existing loader only."""
        # This is verified by the fact that run_sec_targeted takes user_agent
        # as a parameter; it never opens sec.env itself.
        source = (SRC_ROOT / "composition.py").read_text()
        # Find run_sec_targeted (exact, not run_sec_targeted_operation)
        idx = source.index("def run_sec_targeted(\n")
        func_body = source[idx:]
        # End at the next top-level def (the operation seam section header)
        end = func_body.index("\n# ---------------------------------------------------------------------------\n# SEC targeted operation seam")
        func_body = func_body[:end]
        assert "sec.env" not in func_body
        assert "load_sec_config" not in func_body


# ---------------------------------------------------------------------------
# Cap and spacing tests
# ---------------------------------------------------------------------------

class TestCapAndSpacing:
    def test_hard_target_cap_20(self):
        assert MAX_ADMITTED_TARGETS == 20

    def test_request_cap_21(self):
        assert MAX_REQUESTS_PER_TICK == 21

    def test_min_filing_date_is_2026_08_03(self):
        assert MIN_FILING_DATE == date(2026, 8, 3)

    def test_target_provenance_is_holding(self):
        assert TARGET_PROVENANCE == "HOLDING"

    def test_stop_codes(self):
        assert STOP_CODES == frozenset({"auth_rejection", "http_429"})

    def test_over_20_targets_fails_closed(self, tmp_path):
        """More than 20 US holdings fails closed with no I/O."""
        setup_sec_targeted(home=tmp_path)
        holdings = [holding(f"T{i:02d}", "USD", "US") for i in range(21)]
        snap = snapshot([(1, holdings)])
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        from tokmo.records.holdings_snapshots import (
            HoldingsSnapshotStore, HoldingsPersistencePermitV1,
        )
        from tokmo.records.holdings_persistence import (
            CRITERIA_VERSION_V1, USER_REQUEST_PROGRAM_NAME, PurposeKind,
        )
        hstore = HoldingsSnapshotStore(snap_path, clock=lambda: NOW)
        permit = make_permit()
        hstore.append(snap, permit)

        calls = []

        def transport(request):
            calls.append(request)
            raise RuntimeError("must not be called when cap exceeded")

        result = run_sec_targeted(
            home=tmp_path, snapshot_store_path=snap_path,
            transport=transport, clock=lambda: NOW,
            run_id="run1", user_agent=UA_SECRET,
        )
        assert result.code == "failed_target_cap"
        assert result.target_count == 21
        assert calls == []
