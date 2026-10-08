"""SEC/OpenDART disclosure review persistence: schema, validation, append behavior.

Covers plan Tasks 1, 3, 4: exact schema/key-sets, identity, timestamp
canonicalisation, naïve/invalid rejection, corrupt-file fail-closed,
SEC/OpenDART append (SAVED/DUPLICATE/CONFLICT/FAILED), reopen readback,
and write-failure preserving prior canonical bytes.
"""

import json
import os
import stat
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

import pytest

from tokmo.records.disclosure_reviews import (
    DisclosureReviewReceipt,
    DisclosureReviewStore,
)
from tokmo.values import (
    OPENDART_SOURCE,
    SEC_SOURCE,
    OpenDartDisclosureFact,
    SaveStatus,
    SecFilingFact,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
NOW_LATER = datetime(2026, 7, 27, 13, 0, 0, tzinfo=timezone.utc)
REVIEWED = datetime(2026, 7, 27, 12, 30, 0, tzinfo=timezone.utc)
REVIEWED_LATER = datetime(2026, 7, 27, 13, 30, 0, tzinfo=timezone.utc)
REVIEWED_WITH_US = datetime(2026, 7, 27, 12, 30, 0, 123456, tzinfo=timezone.utc)

AAPL_CIK = "0000320193"
STOCK = "000660"
CORP = "00164779"
RCEPT = "20260727000169"

MAX_BYTES = 16 * 1024 * 1024


# ---------------------------------------------------------------------------
# Fact helpers
# ---------------------------------------------------------------------------

def sec_fact(**kw):
    defaults = dict(
        source=SEC_SOURCE, ticker="AAPL", cik=AAPL_CIK,
        company_name="Apple Inc.",
        accession_number="0000320193-26-000077", form="10-Q",
        filing_date=date(2026, 7, 20), report_date=date(2026, 6, 27),
        primary_document="aapl-20260627.htm", is_xbrl=True, observed_at=NOW,
    )
    defaults.update(kw)
    return SecFilingFact(**defaults)


def dart_fact(**kw):
    defaults = dict(
        source=OPENDART_SOURCE, stock_code=STOCK, corp_code=CORP,
        corp_name="SK하이닉스", corp_cls="Y",
        report_name="동일인등출자계열회사와의상품ㆍ용역거래변경",
        receipt_number=RCEPT, filer_name="SK하이닉스",
        receipt_date=date(2026, 7, 27), remark="공", observed_at=NOW,
    )
    defaults.update(kw)
    return OpenDartDisclosureFact(**defaults)


def sec_store(tmp_path):
    return DisclosureReviewStore(tmp_path / "sec_disclosure_reviews.json",
                                 source=SEC_SOURCE)


def dart_store(tmp_path):
    return DisclosureReviewStore(tmp_path / "opendart_disclosure_reviews.json",
                                 source=OPENDART_SOURCE)


# ---------------------------------------------------------------------------
# Task 1: schema and validation
# ---------------------------------------------------------------------------

class TestSecSchema:
    def test_new_file_has_exact_top_level_keys(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        assert set(payload) == {"schema_version", "source", "records"}
        assert payload["schema_version"] == 1
        assert payload["source"] == SEC_SOURCE
        assert isinstance(payload["records"], list)
        assert len(payload["records"]) == 1

    def test_sec_record_has_exact_key_set(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert set(record) == {
            "source", "ticker", "cik", "company_name",
            "accession_number", "form", "filing_date", "report_date",
            "primary_document", "is_xbrl", "observed_at", "reviewed_at",
        }

    def test_sec_identity_fields_stored(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert (record["source"], record["cik"],
                record["accession_number"]) == (
                    SEC_SOURCE, AAPL_CIK, "0000320193-26-000077")

    def test_sec_report_date_and_is_xbrl_nullable(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(
            sec_fact(report_date=None, is_xbrl=None), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert record["report_date"] is None
        assert record["is_xbrl"] is None


class TestOpenDartSchema:
    def test_new_file_has_exact_top_level_keys(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        assert set(payload) == {"schema_version", "source", "records"}
        assert payload["schema_version"] == 1
        assert payload["source"] == OPENDART_SOURCE

    def test_opendart_record_has_exact_key_set(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert set(record) == {
            "source", "stock_code", "corp_code", "corp_name",
            "corp_cls", "report_name", "receipt_number", "filer_name",
            "receipt_date", "remark", "observed_at", "reviewed_at",
        }

    def test_opendart_identity_fields_stored(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert (record["source"], record["corp_code"],
                record["receipt_number"]) == (
                    OPENDART_SOURCE, CORP, RCEPT)

    def test_opendart_remark_nullable(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(remark=None), reviewed_at=REVIEWED)
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        assert payload["records"][0]["remark"] is None


class TestTimestamps:
    def test_canonical_utc_round_trip(self, tmp_path):
        store = sec_store(tmp_path)
        # Non-UTC timezone input must be canonicalised to +00:00.
        kst = timezone(timedelta(hours=9))
        reviewed_kst = datetime(2026, 7, 27, 21, 30, 0, tzinfo=kst)
        observed_kst = datetime(2026, 7, 27, 21, 0, 0, tzinfo=kst)
        store.append_review(
            sec_fact(observed_at=observed_kst), reviewed_at=reviewed_kst)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert record["observed_at"] == "2026-07-27T12:00:00.000000+00:00"
        assert record["reviewed_at"] == "2026-07-27T12:30:00.000000+00:00"

    def test_microseconds_preserved(self, tmp_path):
        store = sec_store(tmp_path)
        observed_us = datetime(2026, 7, 27, 12, 0, 0, 500000,
                               tzinfo=timezone.utc)
        store.append_review(
            sec_fact(observed_at=observed_us), reviewed_at=REVIEWED_WITH_US)
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert "500000" in record["observed_at"]
        assert "123456" in record["reviewed_at"]

    def test_naive_reviewed_at_rejected(self, tmp_path):
        store = sec_store(tmp_path)
        naive = datetime(2026, 7, 27, 12, 30, 0)
        receipt = store.append_review(sec_fact(), reviewed_at=naive)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_review"
        assert receipt.effective_reviewed_at is None
        assert not (tmp_path / "sec_disclosure_reviews.json").exists()

    def test_naive_observed_at_rejected(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(observed_at=datetime(2026, 7, 27, 12, 0, 0)),
            reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_reviewed_before_observed_rejected(self, tmp_path):
        store = sec_store(tmp_path)
        before = NOW - timedelta(minutes=1)
        receipt = store.append_review(sec_fact(), reviewed_at=before)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_review"
        assert receipt.effective_reviewed_at is None

    def test_reviewed_equals_observed_allowed(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=NOW)
        assert receipt.status is SaveStatus.SAVED
        assert receipt.effective_reviewed_at == NOW


class TestIdentityValidation:
    def test_sec_short_cik_rejected(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(cik="123"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_sec_non_digit_cik_rejected(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(cik="000000000a"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_opendart_short_stock_code_rejected(self, tmp_path):
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(stock_code="123"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_opendart_short_corp_code_rejected(self, tmp_path):
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(corp_code="0016477"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_opendart_short_receipt_number_rejected(self, tmp_path):
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(receipt_number="123"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"


class TestWrongSourceOrType:
    def test_sec_store_rejects_opendart_fact(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(dart_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_opendart_store_rejects_sec_fact(self, tmp_path):
        store = dart_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_sec_store_rejects_wrong_source_fact(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(source="wrong"), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"


# ---------------------------------------------------------------------------
# Task 1: corrupt / oversized store fail-closed
# ---------------------------------------------------------------------------

class TestCorruptStore:
    def test_malformed_json_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_text("{broken")
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_wrong_schema_version_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_text(json.dumps({
            "schema_version": 99, "source": SEC_SOURCE, "records": [],
        }))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_wrong_source_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_text(json.dumps({
            "schema_version": 1, "source": "wrong", "records": [],
        }))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_extra_top_level_key_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_text(json.dumps({
            "schema_version": 1, "source": SEC_SOURCE, "records": [],
            "extra": True,
        }))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_record_with_wrong_keys_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_text(json.dumps({
            "schema_version": 1, "source": SEC_SOURCE,
            "records": [{"unexpected": True}],
        }))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_oversized_file_not_overwritten(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        path.write_bytes(b"x" * (MAX_BYTES + 1))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_corrupt_record_semantics_not_overwritten(self, tmp_path):
        """A structurally valid record with invalid CIK width is corrupt."""
        path = tmp_path / "sec_disclosure_reviews.json"
        record = {
            "source": SEC_SOURCE, "ticker": "AAPL", "cik": "123",
            "company_name": "Apple Inc.",
            "accession_number": "0000320193-26-000077", "form": "10-Q",
            "filing_date": "2026-07-20", "report_date": "2026-06-27",
            "primary_document": "doc.htm", "is_xbrl": True,
            "observed_at": "2026-07-27T12:00:00+00:00",
            "reviewed_at": "2026-07-27T12:30:00+00:00",
        }
        path.write_text(json.dumps({
            "schema_version": 1, "source": SEC_SOURCE, "records": [record],
        }))
        original = path.read_bytes()
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"
        assert path.read_bytes() == original

    def test_non_utc_timestamp_in_store_is_corrupt(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        record = {
            "source": SEC_SOURCE, "ticker": "AAPL", "cik": AAPL_CIK,
            "company_name": "Apple Inc.",
            "accession_number": "0000320193-26-000077", "form": "10-Q",
            "filing_date": "2026-07-20", "report_date": "2026-06-27",
            "primary_document": "doc.htm", "is_xbrl": True,
            "observed_at": "2026-07-27T12:00:00+09:00",
            "reviewed_at": "2026-07-27T12:30:00+00:00",
        }
        path.write_text(json.dumps({
            "schema_version": 1, "source": SEC_SOURCE, "records": [record],
        }))
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_corrupt"


# ---------------------------------------------------------------------------
# Task 3: SEC append
# ---------------------------------------------------------------------------

class TestSecAppend:
    def test_new_save(self, tmp_path):
        store = sec_store(tmp_path)
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.SAVED
        assert receipt.reason == "saved"
        assert receipt.effective_reviewed_at == REVIEWED

    def test_duplicate_preserves_first_reviewed_at_and_bytes(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        path = tmp_path / "sec_disclosure_reviews.json"
        bytes_after_first = path.read_bytes()
        receipt = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.reason == "duplicate"
        assert receipt.effective_reviewed_at == REVIEWED
        assert path.read_bytes() == bytes_after_first

    def test_duplicate_with_later_timestamps_preserves_first(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        # New fetch produces a new observed_at; caller passes a later reviewed_at.
        receipt = store.append_review(
            sec_fact(observed_at=NOW_LATER), reviewed_at=REVIEWED_LATER)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert record["observed_at"] == "2026-07-27T12:00:00.000000+00:00"
        assert record["reviewed_at"] == "2026-07-27T12:30:00.000000+00:00"

    def test_conflict_no_mutation(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        path = tmp_path / "sec_disclosure_reviews.json"
        bytes_before = path.read_bytes()
        receipt = store.append_review(
            sec_fact(form="10-K"), reviewed_at=REVIEWED_LATER)
        assert receipt.status is SaveStatus.CONFLICT
        assert receipt.reason == "identity_conflict"
        assert receipt.effective_reviewed_at == REVIEWED
        assert path.read_bytes() == bytes_before

    def test_fresh_reopen_exact_readback(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=SEC_SOURCE)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        store2 = DisclosureReviewStore(path, source=SEC_SOURCE)
        receipt = store2.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED

    def test_write_failure_preserves_prior_file(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=SEC_SOURCE)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        canonical = path.read_bytes()
        # Make parent unwritable so temp file creation fails.
        path.parent.chmod(0o500)
        try:
            receipt = store.append_review(
                sec_fact(accession_number="0000320193-26-000078",
                         cik="0000320194"),
                reviewed_at=REVIEWED)
            assert receipt.status is SaveStatus.FAILED
            assert receipt.reason == "store_write_failed"
            assert path.read_bytes() == canonical
        finally:
            path.parent.chmod(0o700)

    def test_multiple_distinct_identities(self, tmp_path):
        store = sec_store(tmp_path)
        r1 = store.append_review(sec_fact(), reviewed_at=REVIEWED)
        r2 = store.append_review(
            sec_fact(accession_number="0000320193-26-000078"),
            reviewed_at=REVIEWED)
        assert r1.status is SaveStatus.SAVED
        assert r2.status is SaveStatus.SAVED
        payload = json.loads(
            (tmp_path / "sec_disclosure_reviews.json").read_text())
        assert len(payload["records"]) == 2


# ---------------------------------------------------------------------------
# Task 4: OpenDART append
# ---------------------------------------------------------------------------

class TestOpenDartAppend:
    def test_new_save(self, tmp_path):
        store = dart_store(tmp_path)
        receipt = store.append_review(dart_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.SAVED
        assert receipt.reason == "saved"
        assert receipt.effective_reviewed_at == REVIEWED

    def test_duplicate_preserves_first_reviewed_at_and_bytes(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        path = tmp_path / "opendart_disclosure_reviews.json"
        bytes_after_first = path.read_bytes()
        receipt = store.append_review(dart_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED
        assert path.read_bytes() == bytes_after_first

    def test_duplicate_with_later_timestamps_preserves_first(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        receipt = store.append_review(
            dart_fact(observed_at=NOW_LATER), reviewed_at=REVIEWED_LATER)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        record = payload["records"][0]
        assert record["observed_at"] == "2026-07-27T12:00:00.000000+00:00"
        assert record["reviewed_at"] == "2026-07-27T12:30:00.000000+00:00"

    def test_conflict_no_mutation(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        path = tmp_path / "opendart_disclosure_reviews.json"
        bytes_before = path.read_bytes()
        receipt = store.append_review(
            dart_fact(report_name="주요사항보고서"), reviewed_at=REVIEWED_LATER)
        assert receipt.status is SaveStatus.CONFLICT
        assert receipt.reason == "identity_conflict"
        assert receipt.effective_reviewed_at == REVIEWED
        assert path.read_bytes() == bytes_before

    def test_nullable_remark_duplicate(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(remark=None), reviewed_at=REVIEWED)
        receipt = store.append_review(
            dart_fact(remark=None), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE

    def test_fresh_reopen_exact_readback(self, tmp_path):
        path = tmp_path / "opendart_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=OPENDART_SOURCE)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        store2 = DisclosureReviewStore(path, source=OPENDART_SOURCE)
        receipt = store2.append_review(dart_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED

    def test_write_failure_preserves_prior_file(self, tmp_path):
        path = tmp_path / "opendart_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=OPENDART_SOURCE)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        canonical = path.read_bytes()
        path.parent.chmod(0o500)
        try:
            receipt = store.append_review(
                dart_fact(receipt_number="20260727000170"),
                reviewed_at=REVIEWED)
            assert receipt.status is SaveStatus.FAILED
            assert receipt.reason == "store_write_failed"
            assert path.read_bytes() == canonical
        finally:
            path.parent.chmod(0o700)

    def test_multiple_distinct_identities(self, tmp_path):
        store = dart_store(tmp_path)
        r1 = store.append_review(dart_fact(), reviewed_at=REVIEWED)
        r2 = store.append_review(
            dart_fact(receipt_number="20260727000170"), reviewed_at=REVIEWED)
        assert r1.status is SaveStatus.SAVED
        assert r2.status is SaveStatus.SAVED
        payload = json.loads(
            (tmp_path / "opendart_disclosure_reviews.json").read_text())
        assert len(payload["records"]) == 2


# ---------------------------------------------------------------------------
# File permissions
# ---------------------------------------------------------------------------

class TestPermissions:
    def test_parent_dir_mode_0700(self, tmp_path):
        """Store creates a non-existent subdirectory with mode 0700."""
        subdir = tmp_path / "data" / "reviews"
        store = DisclosureReviewStore(
            subdir / "sec_disclosure_reviews.json", source=SEC_SOURCE)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        mode = stat.S_IMODE(subdir.stat().st_mode)
        assert mode == 0o700

    def test_file_mode_0600(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        mode = stat.S_IMODE(
            (tmp_path / "sec_disclosure_reviews.json").stat().st_mode)
        assert mode == 0o600

    def test_no_temp_file_left_behind(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        temps = list(tmp_path.glob(".*.tmp"))
        assert temps == []


# ---------------------------------------------------------------------------
# JSON serialisation
# ---------------------------------------------------------------------------

class TestJsonFormat:
    def test_ensure_ascii_false(self, tmp_path):
        store = dart_store(tmp_path)
        store.append_review(dart_fact(), reviewed_at=REVIEWED)
        raw = (tmp_path / "opendart_disclosure_reviews.json").read_text()
        assert "SK하이닉스" in raw

    def test_compact_separators(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        raw = (tmp_path / "sec_disclosure_reviews.json").read_text()
        assert ", " not in raw
        assert ": " not in raw

    def test_no_nan(self, tmp_path):
        store = sec_store(tmp_path)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        raw = (tmp_path / "sec_disclosure_reviews.json").read_text()
        assert "NaN" not in raw
        assert "Infinity" not in raw


# ---------------------------------------------------------------------------
# MAT-1: self-brick prevention — invalid candidates must be rejected before
# the first write, so the canonical file is never created.
# ---------------------------------------------------------------------------

class TestSelfBrickPrevention:
    def test_opendart_empty_remark_rejected_first_call(self, tmp_path):
        """remark='' passes write-side _validate_dart_fact but must fail the
        candidate-record validation (empty non-null string). No file created."""
        path = tmp_path / "opendart_disclosure_reviews.json"
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(remark=""), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert receipt.effective_reviewed_at is None
        assert not path.exists()

    def test_opendart_fullwidth_receipt_rejected_first_call(self, tmp_path):
        """Fullwidth digits pass str.isdigit() but not isascii().
        Candidate-record validation must reject; no file created."""
        path = tmp_path / "opendart_disclosure_reviews.json"
        fullwidth = "\uff11\uff10\uff12\uff16\uff10\uff17\uff12\uff17\uff10\uff10\uff10\uff10\uff11\uff16"  # １０２６０７２７１０００１６
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(receipt_number=fullwidth), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert receipt.effective_reviewed_at is None
        assert not path.exists()

    def test_opendart_fullwidth_stock_rejected_first_call(self, tmp_path):
        path = tmp_path / "opendart_disclosure_reviews.json"
        fullwidth_stock = "\uff10\uff10\uff10\uff16\uff16\uff10"  # ０００６６０
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(stock_code=fullwidth_stock), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert not path.exists()

    def test_opendart_fullwidth_corp_rejected_first_call(self, tmp_path):
        path = tmp_path / "opendart_disclosure_reviews.json"
        fullwidth_corp = "\uff10\uff10\uff11\uff16\uff14\uff17\uff17\uff19"  # ００１６４７７９
        store = dart_store(tmp_path)
        receipt = store.append_review(
            dart_fact(corp_code=fullwidth_corp), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert not path.exists()

    def test_sec_empty_ticker_rejected_first_call(self, tmp_path):
        """SEC empty required string must fail candidate validation.
        No file created."""
        path = tmp_path / "sec_disclosure_reviews.json"
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(ticker=""), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert receipt.effective_reviewed_at is None
        assert not path.exists()

    def test_sec_empty_company_name_rejected_first_call(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(company_name=""), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert not path.exists()

    def test_sec_empty_accession_rejected_first_call(self, tmp_path):
        path = tmp_path / "sec_disclosure_reviews.json"
        store = sec_store(tmp_path)
        receipt = store.append_review(
            sec_fact(accession_number=""), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"
        assert not path.exists()

    def test_saved_record_readable_on_fresh_reopen_and_duplicate(self, tmp_path):
        """A valid SAVED record must always be readable on fresh reopen,
        and a subsequent identical call resolves to DUPLICATE."""
        path = tmp_path / "sec_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=SEC_SOURCE)
        store.append_review(sec_fact(), reviewed_at=REVIEWED)
        store2 = DisclosureReviewStore(path, source=SEC_SOURCE)
        receipt = store2.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.DUPLICATE
        assert receipt.effective_reviewed_at == REVIEWED


# ---------------------------------------------------------------------------
# Atomic commit: fsync_directory failure leaves file visible but recoverable
# ---------------------------------------------------------------------------

class TestFsyncDirectoryFailure:
    def test_post_replace_fsync_failure_is_duplicate_on_retry(self, tmp_path, monkeypatch):
        """If fsync_directory fails after os.replace succeeded, the record
        is on disk. The caller sees FAILED/store_write_failed, but a fresh
        reopen finds the identity and resolves to DUPLICATE."""
        path = tmp_path / "sec_disclosure_reviews.json"
        store = DisclosureReviewStore(path, source=SEC_SOURCE)

        import tokmo.records.disclosure_reviews as dr_mod

        original_fsync = dr_mod.fsync_directory
        call_count = 0

        def fail_once(directory):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise OSError("simulated fsync failure")
            original_fsync(directory)

        monkeypatch.setattr(dr_mod, "fsync_directory", fail_once)
        receipt = store.append_review(
            sec_fact(), reviewed_at=REVIEWED)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "store_write_failed"
        assert receipt.effective_reviewed_at is None
        # File is visible despite the fsync failure.
        assert path.exists()
        payload = json.loads(path.read_text())
        assert len(payload["records"]) == 1
        # Fresh reopen resolves to DUPLICATE, recovering gracefully.
        store2 = DisclosureReviewStore(path, source=SEC_SOURCE)
        receipt2 = store2.append_review(sec_fact(), reviewed_at=REVIEWED)
        assert receipt2.status is SaveStatus.DUPLICATE
