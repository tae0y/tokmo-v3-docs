"""Adversarial migration + provenance tests for the SEC targeted store.

Covers the review-gate scenarios left open after the GLM repairs:

A. Real legacy schema-version-1 DB fixture (prior production DDL) migrates
   transactionally on open: members backfilled HOLDING, filing preserved,
   widened filing CHECK accepts WATCHLIST, metadata current, integrity/FK
   clean, index recreated, idempotent reopen.
B. Legacy DB with malformed target_universe symbols JSON rolls back
   atomically: metadata stays 1, no partial members / replacement table,
   truthful store error.
C. store_universe mixed provenance exact rows; monotonic widening on the
   same universe; combined never narrows; key mismatch / invalid
   provenance leaves zero partial rows.
D. save_filing WATCHLIST insert; identical payload through the other
   provenance widens to combined in either order; combined never narrows;
   immutable payload drift remains a conflict.
E. run_sec_targeted watchlist-only; holdings+watchlist overlap dedup (one
   registry + one submissions call); fallback preserves prior per-symbol
   provenance (a stored WATCHLIST symbol never gains a HOLDING claim);
   target cap applies to the deduped union with network 0 on overflow.
"""

import json
import os
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.composition import (
    SEC_TARGETED_DB_NAME,
    run_sec_targeted,
    setup_sec_targeted,
)
from tokmo.facts.sec import _SecTransportSuccess
from tokmo.facts.sec_targeted import merge_fallback_target_provenance
from tokmo.records.sec_targeted_store import (
    SecTargetedStore,
    SecTargetedStoreError,
)
from tokmo.values import SecFilingFact
from tokmo.values.sec_provenance import (
    COMBINED_PROVENANCE,
    TARGET_PROVENANCE,
    WATCHLIST_PROVENANCE,
)

NOW = datetime(2026, 8, 3, 12, 0, 0, tzinfo=UTC)
UA_SECRET = "TokmoTest/0.0 (fixture-contact@example.invalid)"
_TEST_SNAPSHOT_ID = "16fd27068baf433b82eb8c7fada847da"

# Exact prior production v1 DDL (commit 61627e7) — the fixture must match
# what real deployed stores contain, not a synthetic approximation.
_V1_DDL = (
    """CREATE TABLE storage_metadata (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
) STRICT""",
    """CREATE TABLE runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    code          TEXT NOT NULL,
    started_count INTEGER NOT NULL,
    CHECK (length(run_id) BETWEEN 1 AND 128),
    CHECK (started_count >= 0)
) STRICT""",
    """CREATE TABLE attempts (
    run_id       TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    seq          INTEGER NOT NULL,
    endpoint     TEXT NOT NULL,
    cik          TEXT,
    ticker       TEXT,
    started_at   TEXT NOT NULL,
    outcome_code TEXT NOT NULL,
    PRIMARY KEY (run_id, seq),
    CHECK (seq >= 1),
    CHECK (endpoint IN ('sec_company_tickers', 'sec_submissions')),
    CHECK (cik IS NULL OR (length(cik) = 10 AND cik GLOB '[0-9]*'))
) STRICT""",
    """CREATE TABLE target_universe (
    universe_id      TEXT PRIMARY KEY,
    captured_at      TEXT NOT NULL,
    symbols          TEXT NOT NULL,
    source_snapshot  TEXT NOT NULL,
    stored_at        TEXT NOT NULL,
    CHECK (length(universe_id) BETWEEN 1 AND 128),
    CHECK (length(source_snapshot) BETWEEN 1 AND 256)
) STRICT""",
    """CREATE TABLE registry (
    ticker       TEXT PRIMARY KEY,
    cik          TEXT NOT NULL,
    company_name TEXT NOT NULL,
    refreshed_at TEXT NOT NULL,
    CHECK (length(ticker) BETWEEN 1 AND 32)
) STRICT""",
    """CREATE TABLE filings (
    accession_number  TEXT NOT NULL,
    cik                TEXT NOT NULL,
    ticker             TEXT NOT NULL,
    company_name       TEXT NOT NULL,
    form               TEXT NOT NULL,
    filing_date        TEXT NOT NULL,
    report_date        TEXT,
    primary_document   TEXT NOT NULL,
    is_xbrl            INTEGER,
    observed_at        TEXT NOT NULL,
    stored_at          TEXT NOT NULL,
    provenance         TEXT NOT NULL,
    source             TEXT NOT NULL,
    run_id             TEXT NOT NULL REFERENCES runs(run_id) ON DELETE RESTRICT,
    PRIMARY KEY (accession_number),
    CHECK (provenance = 'HOLDING'),
    CHECK (source = 'sec_submissions')
) STRICT""",
    """CREATE TABLE conflicts (
    cik               TEXT NOT NULL,
    accession_number  TEXT NOT NULL,
    existing_form     TEXT NOT NULL,
    new_form          TEXT NOT NULL,
    detected_at       TEXT NOT NULL,
    run_id            TEXT NOT NULL,
    PRIMARY KEY (accession_number)
) STRICT""",
    """CREATE INDEX filings_filing_date
    ON filings (filing_date DESC, cik, accession_number)""",
)

_LEGACY_FILING_ROW = (
    "0000320193-26-000001",
    "0000320193",
    "AAA",
    "Company A",
    "10-Q",
    "2026-08-03",
    "2026-07-01",
    "doc.htm",
    1,
    "2026-08-03T11:00:00+00:00",
    "2026-08-03T11:00:01+00:00",
    "HOLDING",
    "sec_submissions",
    "legacy-run",
)


def make_legacy_v1_db(path: Path, *, symbols_json='["AAA", "BBB"]') -> None:
    """Build a real schema-version-1 store as prior production wrote it."""
    connection = sqlite3.connect(path)
    try:
        for ddl in _V1_DDL:
            connection.execute(ddl)
        connection.executemany(
            "INSERT INTO storage_metadata(key, value) VALUES (?, ?)",
            [
                ("sec_targeted_adapter", "tokmo-sec-targeted-v1"),
                ("sec_targeted_schema_version", "1"),
            ],
        )
        connection.execute(
            "INSERT INTO runs(run_id, started_at, code, started_count) "
            "VALUES ('legacy-run', '2026-08-03T11:00:00+00:00', "
            "'complete', 2)",
        )
        connection.execute(
            "INSERT INTO target_universe(universe_id, captured_at, symbols, "
            "source_snapshot, stored_at) VALUES ('legacy-u1', "
            "'2026-08-03T10:00:00+00:00', ?, "
            "'toss-holdings-v1:2026-08-03T10:00:00+00:00', "
            "'2026-08-03T11:00:00+00:00')",
            (symbols_json,),
        )
        connection.execute(
            "INSERT INTO filings(accession_number, cik, ticker, company_name, "
            "form, filing_date, report_date, primary_document, is_xbrl, "
            "observed_at, stored_at, provenance, source, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            _LEGACY_FILING_ROW,
        )
        connection.commit()
    finally:
        connection.close()
    os.chmod(path, 0o600)


def filing_fact(accession, *, form="10-Q", ticker="AAA", cik="0000320193"):
    return SecFilingFact(
        source="sec_submissions",
        ticker=ticker,
        cik=cik,
        company_name="Company A",
        accession_number=accession,
        form=form,
        filing_date=date(2026, 8, 3),
        report_date=date(2026, 7, 1),
        primary_document="doc.htm",
        is_xbrl=True,
        observed_at=NOW,
    )


def read_rows(path, sql, params=()):
    connection = sqlite3.connect(path)
    try:
        return connection.execute(sql, params).fetchall()
    finally:
        connection.close()


# ---------------------------------------------------------------------------
# A. Legacy v1 fixture migrates transactionally on open
# ---------------------------------------------------------------------------


class TestLegacyV1Migration:
    def test_open_migrates_members_filing_check_metadata(self, tmp_path):
        db = tmp_path / "sec-targeted.sqlite3"
        make_legacy_v1_db(db)

        store = SecTargetedStore(db, clock=lambda: NOW)
        claim = store.claim_run(run_id="mig-run", started_at=NOW)
        assert claim.status == "claimed"

        # Members backfilled from the stored universe, all HOLDING
        members = read_rows(
            db,
            "SELECT universe_id, symbol, provenance FROM "
            "target_universe_members ORDER BY symbol",
        )
        assert members == [
            ("legacy-u1", "AAA", "HOLDING"),
            ("legacy-u1", "BBB", "HOLDING"),
        ]

        # Legacy filing row preserved byte-for-byte
        filings = read_rows(
            db,
            "SELECT accession_number, cik, ticker, company_name, form, "
            "filing_date, report_date, primary_document, is_xbrl, "
            "observed_at, stored_at, provenance, source, run_id "
            "FROM filings",
        )
        assert filings == [_LEGACY_FILING_ROW]

        # Metadata is fully current
        meta = dict(read_rows(db, "SELECT key, value FROM storage_metadata"))
        assert meta == {
            "sec_targeted_schema_version": "2",
            "sec_targeted_adapter": "tokmo-sec-targeted-next",
        }

        # Widened CHECK accepts WATCHLIST (and combined) after migration
        result = store.save_filing(
            filing=filing_fact("0001403161-26-000010", ticker="V", cik="0001403161"),
            run_id="mig-run",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        assert result.status == "saved"
        connection = sqlite3.connect(db)
        try:
            connection.execute(
                "INSERT INTO filings(accession_number, cik, ticker, "
                "company_name, form, filing_date, report_date, "
                "primary_document, is_xbrl, observed_at, stored_at, "
                "provenance, source, run_id) VALUES ('acc-combined', "
                "'0000000001', 'CCC', 'C', '8-K', '2026-08-03', NULL, "
                "'c.htm', 1, 'o', 's', ?, 'sec_submissions', 'mig-run')",
                (COMBINED_PROVENANCE,),
            )
            connection.rollback()
            # v1 CHECK is gone: narrowed enum would have rejected both
        finally:
            connection.close()

        # Integrity and FK are clean, filings index recreated
        connection = sqlite3.connect(db)
        try:
            assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
            assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
            indexes = {
                row[0]
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' "
                    "AND name='filings_filing_date'"
                )
            }
            assert indexes == {"filings_filing_date"}
        finally:
            connection.close()

    def test_migration_is_idempotent_on_reopen(self, tmp_path):
        db = tmp_path / "sec-targeted.sqlite3"
        make_legacy_v1_db(db)
        store = SecTargetedStore(db, clock=lambda: NOW)
        store.claim_run(run_id="mig-run", started_at=NOW)
        # Second open re-validates without touching the schema
        assert store.claim_run(run_id="mig-run-2", started_at=NOW).status == "claimed"
        members = read_rows(db, "SELECT COUNT(*) FROM target_universe_members")
        assert members == [(2,)]

    def test_migrated_store_widens_legacy_filing_provenance(self, tmp_path):
        """The preserved HOLDING filing participates in monotonic widening."""
        db = tmp_path / "sec-targeted.sqlite3"
        make_legacy_v1_db(db)
        store = SecTargetedStore(db, clock=lambda: NOW)
        store.claim_run(run_id="mig-run", started_at=NOW)
        result = store.save_filing(
            filing=filing_fact("0000320193-26-000001"),
            run_id="mig-run",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        assert result.status == "duplicate"
        rows = read_rows(
            db,
            "SELECT provenance FROM filings WHERE accession_number=?",
            ("0000320193-26-000001",),
        )
        assert rows == [(COMBINED_PROVENANCE,)]


# ---------------------------------------------------------------------------
# B. Malformed legacy universe JSON: atomic rollback, truthful error
# ---------------------------------------------------------------------------


class TestLegacyV1MigrationRollback:
    @pytest.mark.parametrize(
        "bad_symbols",
        [
            "not json at all",
            '{"oops": 1}',
            '["AAA", ""]',
            '["AAA", 7]',
        ],
    )
    def test_malformed_symbols_rolls_back_atomically(self, tmp_path, bad_symbols):
        db = tmp_path / "sec-targeted.sqlite3"
        make_legacy_v1_db(db, symbols_json=bad_symbols)

        store = SecTargetedStore(db, clock=lambda: NOW)
        with pytest.raises(SecTargetedStoreError) as excinfo:
            store.claim_run(run_id="mig-run", started_at=NOW)
        assert excinfo.value.reason == "schema_mismatch"

        # Metadata untouched: still schema 1 with the v1 adapter
        meta = dict(read_rows(db, "SELECT key, value FROM storage_metadata"))
        assert meta == {
            "sec_targeted_schema_version": "1",
            "sec_targeted_adapter": "tokmo-sec-targeted-v1",
        }

        # No partial members table, no replacement filings table left behind
        tables = {
            row[0]
            for row in read_rows(
                db,
                "SELECT name FROM sqlite_master WHERE type='table'",
            )
        }
        assert "target_universe_members" not in tables
        assert "filings_next" not in tables

        # Legacy filing row untouched
        filings = read_rows(
            db,
            "SELECT accession_number, cik, ticker, company_name, form, "
            "filing_date, report_date, primary_document, is_xbrl, "
            "observed_at, stored_at, provenance, source, run_id "
            "FROM filings",
        )
        assert filings == [_LEGACY_FILING_ROW]

        # Every subsequent open keeps failing closed the same way
        with pytest.raises(SecTargetedStoreError) as again:
            store.claim_run(run_id="mig-run", started_at=NOW)
        assert again.value.reason == "schema_mismatch"


# ---------------------------------------------------------------------------
# C. store_universe mixed provenance / widening / fail-closed
# ---------------------------------------------------------------------------


class TestStoreUniverseProvenance:
    def _store(self, tmp_path):
        return SecTargetedStore(tmp_path / "test.sqlite3", clock=lambda: NOW)

    def test_mixed_provenance_persists_exact_rows(self, tmp_path):
        store = self._store(tmp_path)
        result = store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=frozenset({"AAA", "BBB", "V"}),
            source_snapshot="toss-holdings-v1:2026-08-03T12:00:00+00:00",
            provenance={
                "AAA": TARGET_PROVENANCE,
                "BBB": COMBINED_PROVENANCE,
                "V": WATCHLIST_PROVENANCE,
            },
        )
        assert result.status == "saved"
        rows = read_rows(
            tmp_path / "test.sqlite3",
            "SELECT symbol, provenance FROM target_universe_members "
            "WHERE universe_id='u1' ORDER BY symbol",
        )
        assert rows == [
            ("AAA", "HOLDING"),
            ("BBB", "HOLDING + WATCHLIST"),
            ("V", "WATCHLIST"),
        ]

    def test_same_universe_widens_monotonically_both_directions(self, tmp_path):
        store = self._store(tmp_path)
        symbols = frozenset({"AAA", "V"})
        store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance={"AAA": TARGET_PROVENANCE, "V": WATCHLIST_PROVENANCE},
        )
        result = store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance={"AAA": WATCHLIST_PROVENANCE, "V": TARGET_PROVENANCE},
        )
        assert result.status == "saved"
        rows = read_rows(
            tmp_path / "test.sqlite3",
            "SELECT symbol, provenance FROM target_universe_members "
            "WHERE universe_id='u1' ORDER BY symbol",
        )
        assert rows == [
            ("AAA", COMBINED_PROVENANCE),
            ("V", COMBINED_PROVENANCE),
        ]

    def test_combined_never_narrows(self, tmp_path):
        store = self._store(tmp_path)
        symbols = frozenset({"AAA"})
        store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance={"AAA": COMBINED_PROVENANCE},
        )
        for narrower in (TARGET_PROVENANCE, WATCHLIST_PROVENANCE, COMBINED_PROVENANCE):
            result = store.store_universe(
                universe_id="u1",
                captured_at=NOW,
                symbols=symbols,
                source_snapshot="snap",
                provenance={"AAA": narrower},
            )
            assert result.status == "unchanged"
            rows = read_rows(
                tmp_path / "test.sqlite3",
                "SELECT provenance FROM target_universe_members WHERE universe_id='u1'",
            )
            assert rows == [(COMBINED_PROVENANCE,)]

    def test_identical_provenance_is_unchanged(self, tmp_path):
        store = self._store(tmp_path)
        symbols = frozenset({"AAA", "V"})
        prov = {"AAA": TARGET_PROVENANCE, "V": WATCHLIST_PROVENANCE}
        store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance=dict(prov),
        )
        result = store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance=dict(prov),
        )
        assert result.status == "unchanged"

    def test_key_mismatch_leaves_zero_partial_rows(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        store = SecTargetedStore(db, clock=lambda: NOW)
        with pytest.raises(SecTargetedStoreError) as excinfo:
            store.store_universe(
                universe_id="u1",
                captured_at=NOW,
                symbols=frozenset({"AAA", "V"}),
                source_snapshot="snap",
                provenance={"AAA": TARGET_PROVENANCE},
            )
        assert excinfo.value.reason == "provenance_key_mismatch"
        if db.exists():
            assert read_rows(db, "SELECT COUNT(*) FROM target_universe") == [(0,)]
            assert read_rows(db, "SELECT COUNT(*) FROM target_universe_members") == [
                (0,)
            ]

    def test_extra_key_is_a_mismatch(self, tmp_path):
        store = self._store(tmp_path)
        with pytest.raises(SecTargetedStoreError) as excinfo:
            store.store_universe(
                universe_id="u1",
                captured_at=NOW,
                symbols=frozenset({"AAA"}),
                source_snapshot="snap",
                provenance={"AAA": TARGET_PROVENANCE, "V": WATCHLIST_PROVENANCE},
            )
        assert excinfo.value.reason == "provenance_key_mismatch"

    def test_invalid_provenance_leaves_zero_partial_rows(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        store = SecTargetedStore(db, clock=lambda: NOW)
        with pytest.raises(SecTargetedStoreError) as excinfo:
            store.store_universe(
                universe_id="u1",
                captured_at=NOW,
                symbols=frozenset({"AAA"}),
                source_snapshot="snap",
                provenance={"AAA": "BOTH"},
            )
        assert excinfo.value.reason == "invalid_provenance"
        if db.exists():
            assert read_rows(db, "SELECT COUNT(*) FROM target_universe") == [(0,)]
            assert read_rows(db, "SELECT COUNT(*) FROM target_universe_members") == [
                (0,)
            ]

    def test_invalid_provenance_never_partially_updates_existing(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        store = SecTargetedStore(db, clock=lambda: NOW)
        symbols = frozenset({"AAA", "V"})
        store.store_universe(
            universe_id="u1",
            captured_at=NOW,
            symbols=symbols,
            source_snapshot="snap",
            provenance={"AAA": TARGET_PROVENANCE, "V": WATCHLIST_PROVENANCE},
        )
        with pytest.raises(SecTargetedStoreError):
            store.store_universe(
                universe_id="u1",
                captured_at=NOW,
                symbols=symbols,
                source_snapshot="snap",
                provenance={"AAA": WATCHLIST_PROVENANCE, "V": "BOTH"},
            )
        rows = read_rows(
            db,
            "SELECT symbol, provenance FROM target_universe_members "
            "WHERE universe_id='u1' ORDER BY symbol",
        )
        assert rows == [("AAA", "HOLDING"), ("V", "WATCHLIST")]


# ---------------------------------------------------------------------------
# D. save_filing provenance widening
# ---------------------------------------------------------------------------


class TestSaveFilingProvenance:
    def _claimed(self, tmp_path):
        db = tmp_path / "test.sqlite3"
        store = SecTargetedStore(db, clock=lambda: NOW)
        store.claim_run(run_id="run1", started_at=NOW)
        return db, store

    def _provenance(self, db, accession):
        rows = read_rows(
            db,
            "SELECT provenance FROM filings WHERE accession_number=?",
            (accession,),
        )
        assert len(rows) == 1
        return rows[0][0]

    def test_watchlist_insert_persists_watchlist(self, tmp_path):
        db, store = self._claimed(tmp_path)
        result = store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        assert result.status == "saved"
        assert self._provenance(db, "acc1") == WATCHLIST_PROVENANCE

    def test_watchlist_then_holding_widens_to_combined(self, tmp_path):
        db, store = self._claimed(tmp_path)
        store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        result = store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=TARGET_PROVENANCE,
        )
        assert result.status == "duplicate"
        assert self._provenance(db, "acc1") == COMBINED_PROVENANCE

    def test_holding_then_watchlist_widens_to_combined(self, tmp_path):
        db, store = self._claimed(tmp_path)
        store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=TARGET_PROVENANCE,
        )
        result = store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        assert result.status == "duplicate"
        assert self._provenance(db, "acc1") == COMBINED_PROVENANCE

    def test_combined_never_narrows(self, tmp_path):
        db, store = self._claimed(tmp_path)
        store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=COMBINED_PROVENANCE,
        )
        for narrower in (TARGET_PROVENANCE, WATCHLIST_PROVENANCE, COMBINED_PROVENANCE):
            result = store.save_filing(
                filing=filing_fact("acc1"),
                run_id="run1",
                stored_at=NOW,
                provenance=narrower,
            )
            assert result.status == "duplicate"
            assert self._provenance(db, "acc1") == COMBINED_PROVENANCE

    def test_changed_payload_is_conflict_and_keeps_provenance(self, tmp_path):
        db, store = self._claimed(tmp_path)
        store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=WATCHLIST_PROVENANCE,
        )
        store.save_filing(
            filing=filing_fact("acc1"),
            run_id="run1",
            stored_at=NOW,
            provenance=TARGET_PROVENANCE,
        )
        result = store.save_filing(
            filing=filing_fact("acc1", form="10-K"),
            run_id="run1",
            stored_at=NOW,
            provenance=TARGET_PROVENANCE,
        )
        assert result.status == "conflict"
        assert self._provenance(db, "acc1") == COMBINED_PROVENANCE
        conflicts = read_rows(db, "SELECT accession_number, new_form FROM conflicts")
        assert conflicts == [("acc1", "10-K")]

    def test_invalid_provenance_rejected_before_sql(self, tmp_path):
        db, store = self._claimed(tmp_path)
        with pytest.raises(SecTargetedStoreError) as excinfo:
            store.save_filing(
                filing=filing_fact("acc1"),
                run_id="run1",
                stored_at=NOW,
                provenance="BOTH",
            )
        assert excinfo.value.reason == "invalid_provenance"
        assert read_rows(db, "SELECT COUNT(*) FROM filings") == [(0,)]


# ---------------------------------------------------------------------------
# E. run_sec_targeted watchlist union / fallback / cap
# ---------------------------------------------------------------------------


def _holding(symbol):
    from tokmo.values import TossHoldingFact

    return TossHoldingFact(
        symbol=symbol,
        quantity="1",
        avg_price="1",
        avg_price_field="avgPrice",
        currency="USD",
        market_raw="US",
        market_field="market_raw",
        name=None,
        asset_type=None,
    )


def _write_snapshot_store(path, symbols):
    from tokmo.records.holdings_persistence import authorize_user_request
    from tokmo.records.holdings_snapshots import (
        HoldingsSnapshotStore,
        snapshot_content_hash,
    )
    from tokmo.values import (
        TOSS_SOURCE,
        CompleteHoldingsSnapshot,
        HoldingsSnapshotAccount,
    )

    account = HoldingsSnapshotAccount(
        1, tuple(_holding(symbol) for symbol in sorted(symbols))
    )
    snap = CompleteHoldingsSnapshot(
        snapshot_id=_TEST_SNAPSHOT_ID,
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=1,
        accounts=(account,),
        content_hash="0" * 64,
    )
    snap = CompleteHoldingsSnapshot(
        snapshot_id=_TEST_SNAPSHOT_ID,
        source=TOSS_SOURCE,
        captured_at=NOW,
        discovered_account_count=1,
        accounts=(account,),
        content_hash=snapshot_content_hash(snap),
    )
    permit = authorize_user_request(snapshot_id=_TEST_SNAPSHOT_ID, clock=lambda: NOW)
    HoldingsSnapshotStore(path, clock=lambda: NOW).append(snap, permit)


def _tickers_body(entries):
    payload = {}
    for i, (cik, ticker, title) in enumerate(entries):
        payload[str(i)] = {"cik_str": cik, "ticker": ticker, "title": title}
    return json.dumps(payload).encode()


def _submissions_body(cik, filings):
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
    return json.dumps({"cik": str(int(cik)), "filings": {"recent": recent}}).encode()


class TestRunSecTargetedWatchlist:
    def _prepare(self, tmp_path):
        setup_sec_targeted(home=tmp_path)
        return tmp_path / ".local" / "share" / "tokmo-v3" / SEC_TARGETED_DB_NAME

    def _v_transport(self, calls):
        tb = _tickers_body([(1403161, "V", "Visa Inc.")])
        sb = _submissions_body(
            "0001403161",
            [
                (
                    "0001403161-26-000010",
                    "2026-08-03",
                    "2026-06-30",
                    "10-Q",
                    "v-10q.htm",
                    1,
                ),
            ],
        )

        def transport(request):
            calls.append((request.endpoint_id, request.cik))
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(sb)

        return transport

    def test_watchlist_only_run_saves_watchlist_provenance(self, tmp_path):
        db = self._prepare(tmp_path)
        calls = []
        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=None,
            transport=self._v_transport(calls),
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"V"}),
        )
        assert result.code == "complete"
        assert result.target_count == 1
        assert result.filings_saved == 1
        assert calls == [
            ("sec_company_tickers", None),
            ("sec_submissions", "0001403161"),
        ]
        members = read_rows(
            db, "SELECT symbol, provenance FROM target_universe_members"
        )
        assert members == [("V", WATCHLIST_PROVENANCE)]
        assert read_rows(db, "SELECT provenance FROM filings") == [
            (WATCHLIST_PROVENANCE,)
        ]
        assert read_rows(db, "SELECT source_snapshot FROM target_universe") == [
            ("watchlist_only",)
        ]

    def test_holdings_watchlist_overlap_dedups_to_single_target(self, tmp_path):
        db = self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        _write_snapshot_store(snap_path, {"V"})
        calls = []
        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=snap_path,
            transport=self._v_transport(calls),
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"V"}),
        )
        assert result.code == "complete"
        assert result.target_count == 1
        assert result.started == 2
        # Exactly one registry call and one submissions call for the overlap
        assert calls == [
            ("sec_company_tickers", None),
            ("sec_submissions", "0001403161"),
        ]
        members = read_rows(
            db, "SELECT symbol, provenance FROM target_universe_members"
        )
        assert members == [("V", COMBINED_PROVENANCE)]
        assert read_rows(db, "SELECT provenance FROM filings") == [
            (COMBINED_PROVENANCE,)
        ]

    def test_fallback_preserves_watchlist_provenance(self, tmp_path):
        """A stored WATCHLIST-only symbol still on the watchlist must stay
        WATCHLIST on the fallback path — union membership in the stored
        universe is not a HOLDING claim."""
        db = self._prepare(tmp_path)
        calls1 = []
        run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=None,
            transport=self._v_transport(calls1),
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"V"}),
        )

        # Fallback branch 1: snapshot store exists but is unreadable
        garbage = tmp_path / "corrupt_snapshots.sqlite3"
        garbage.write_bytes(b"this is not a sqlite database " * 8)
        calls2 = []
        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=garbage,
            transport=self._v_transport(calls2),
            clock=lambda: NOW + timedelta(minutes=30),
            run_id="run2",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"V"}),
        )
        assert result.code == "complete"
        members = read_rows(
            db, "SELECT symbol, provenance FROM target_universe_members"
        )
        assert members == [("V", WATCHLIST_PROVENANCE)]
        assert read_rows(db, "SELECT provenance FROM filings") == [
            (WATCHLIST_PROVENANCE,)
        ]

        # Fallback branch 2: no snapshot store path at all
        calls3 = []
        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=None,
            transport=self._v_transport(calls3),
            clock=lambda: NOW + timedelta(minutes=60),
            run_id="run3",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"V"}),
        )
        assert result.code == "complete"
        members = read_rows(
            db, "SELECT symbol, provenance FROM target_universe_members"
        )
        assert members == [("V", WATCHLIST_PROVENANCE)]
        assert read_rows(db, "SELECT provenance FROM filings") == [
            (WATCHLIST_PROVENANCE,)
        ]

    def test_fallback_preserves_holding_and_widens_only_watchlisted(self, tmp_path):
        """Prior HOLDING stays HOLDING off-watchlist; a held symbol newly
        watchlisted widens to COMBINED on fallback."""
        db = self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        _write_snapshot_store(snap_path, {"AAA", "BBB"})
        tb = _tickers_body(
            [
                (320193, "AAA", "Company A"),
                (320194, "BBB", "Company B"),
            ]
        )

        def transport(request):
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(tb)
            return _SecTransportSuccess(_submissions_body(request.cik, []))

        run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=snap_path,
            transport=transport,
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
        )
        members = dict(
            read_rows(db, "SELECT symbol, provenance FROM target_universe_members")
        )
        assert members == {"AAA": "HOLDING", "BBB": "HOLDING"}

        # Fallback run: AAA is now also watchlisted, BBB is not
        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=None,
            transport=transport,
            clock=lambda: NOW + timedelta(minutes=30),
            run_id="run2",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset({"AAA"}),
        )
        assert result.code == "complete"
        rows = read_rows(
            db,
            "SELECT universe_id, symbol, provenance FROM "
            "target_universe_members ORDER BY universe_id, symbol",
        )
        latest = {(symbol, provenance) for universe_id, symbol, provenance in rows}
        assert ("AAA", COMBINED_PROVENANCE) in latest or (
            "AAA",
            "HOLDING + WATCHLIST",
        ) in latest
        # BBB never gains a watchlist claim in any universe row
        assert all(
            provenance == "HOLDING"
            for universe_id, symbol, provenance in rows
            if symbol == "BBB"
        )

    def test_cap_applies_after_deduped_union(self, tmp_path):
        """20 deduped union targets proceed; 21 fail closed with network 0."""
        self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        holdings = {f"H{i:02d}" for i in range(12)}
        _write_snapshot_store(snap_path, holdings)
        # 12 watchlist symbols, 4 overlapping: union = 12 + 8 = 20
        watchlist = {f"H{i:02d}" for i in range(4)} | {f"W{i:02d}" for i in range(8)}
        calls = []

        def transport(request):
            calls.append(request.endpoint_id)
            if request.endpoint_id == "sec_company_tickers":
                return _SecTransportSuccess(_tickers_body([]))
            raise RuntimeError("no submissions expected")

        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=snap_path,
            transport=transport,
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset(watchlist),
        )
        assert result.code == "complete_with_unresolved"
        assert result.target_count == 20
        assert calls == ["sec_company_tickers"]

    def test_union_overflow_fails_closed_network_zero(self, tmp_path):
        self._prepare(tmp_path)
        snap_path = tmp_path / "holdings_snapshots.sqlite3"
        _write_snapshot_store(snap_path, {f"H{i:02d}" for i in range(12)})
        watchlist = {f"H{i:02d}" for i in range(3)} | {
            f"W{i:02d}" for i in range(9)
        }  # union = 21
        calls = []

        def transport(request):
            calls.append(request.endpoint_id)
            raise RuntimeError("must not be called")

        result = run_sec_targeted(
            home=tmp_path,
            snapshot_store_path=snap_path,
            transport=transport,
            clock=lambda: NOW,
            run_id="run1",
            user_agent=UA_SECRET,
            sleeper=lambda _: None,
            us_watchlist_symbols=frozenset(watchlist),
        )
        assert result.code == "failed_target_cap"
        assert result.target_count == 21
        assert result.started == 0
        assert calls == []


# ---------------------------------------------------------------------------
# Pure fallback merge unit coverage
# ---------------------------------------------------------------------------


class TestMergeFallbackTargetProvenance:
    def test_prior_watchlist_still_watchlisted_stays_watchlist(self):
        merged = merge_fallback_target_provenance(
            frozenset({"V"}),
            {"V": WATCHLIST_PROVENANCE},
            frozenset({"V"}),
        )
        assert merged == {"V": WATCHLIST_PROVENANCE}

    def test_prior_holding_watchlisted_widens(self):
        merged = merge_fallback_target_provenance(
            frozenset({"AAA"}),
            {"AAA": TARGET_PROVENANCE},
            frozenset({"AAA"}),
        )
        assert merged == {"AAA": COMBINED_PROVENANCE}

    def test_prior_combined_is_terminal(self):
        for watch in (frozenset(), frozenset({"AAA"})):
            merged = merge_fallback_target_provenance(
                frozenset({"AAA"}),
                {"AAA": COMBINED_PROVENANCE},
                watch,
            )
            assert merged == {"AAA": COMBINED_PROVENANCE}

    def test_new_watchlist_symbol_is_watchlist(self):
        merged = merge_fallback_target_provenance(
            frozenset({"AAA"}),
            {"AAA": TARGET_PROVENANCE},
            frozenset({"V"}),
        )
        assert merged == {"AAA": TARGET_PROVENANCE, "V": WATCHLIST_PROVENANCE}

    def test_missing_prior_row_defaults_to_holding(self):
        merged = merge_fallback_target_provenance(
            frozenset({"AAA"}),
            {},
            frozenset(),
        )
        assert merged == {"AAA": TARGET_PROVENANCE}
