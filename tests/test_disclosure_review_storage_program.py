"""Programs seam tests: disclosure review storage.

Verifies the thin seam forwards fact and reviewed_at exactly once, rejects
wrong source/type, and never calls facts/API/clock/Telegram.
"""

from datetime import date, datetime, timezone, timedelta

import pytest

from tokmo.programs.disclosure_review_storage import (
    record_completed_disclosure_review,
)
from tokmo.records.disclosure_reviews import DisclosureReviewStore
from tokmo.values import (
    OPENDART_SOURCE,
    SEC_SOURCE,
    OpenDartDisclosureFact,
    SaveStatus,
    SecFilingFact,
)

NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=timezone.utc)
REVIEWED = datetime(2026, 7, 27, 12, 30, 0, tzinfo=timezone.utc)

AAPL_CIK = "0000320193"
STOCK = "000660"
CORP = "00164779"
RCEPT = "20260727000169"


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


class TestProgramsSeamForwarding:
    def test_sec_fact_forwarded_once(self, tmp_path):
        store = DisclosureReviewStore(
            tmp_path / "sec_disclosure_reviews.json", source=SEC_SOURCE)
        receipt = record_completed_disclosure_review(
            sec_fact(), reviewed_at=REVIEWED, store=store)
        assert receipt.status is SaveStatus.SAVED
        assert receipt.effective_reviewed_at == REVIEWED

        # Second call with same fact/identity is DUPLICATE.
        receipt2 = record_completed_disclosure_review(
            sec_fact(), reviewed_at=REVIEWED, store=store)
        assert receipt2.status is SaveStatus.DUPLICATE

    def test_opendart_fact_forwarded_once(self, tmp_path):
        store = DisclosureReviewStore(
            tmp_path / "opendart_disclosure_reviews.json",
            source=OPENDART_SOURCE)
        receipt = record_completed_disclosure_review(
            dart_fact(), reviewed_at=REVIEWED, store=store)
        assert receipt.status is SaveStatus.SAVED

    def test_reviewed_at_not_inferred(self, tmp_path):
        """The seam must receive reviewed_at as an explicit argument; it has
        no clock, no default, and no inference."""
        import inspect
        from tokmo.programs import disclosure_review_storage as mod
        sig = inspect.signature(mod.record_completed_disclosure_review)
        assert "reviewed_at" in sig.parameters
        param = sig.parameters["reviewed_at"]
        assert param.default is inspect.Parameter.empty
        assert param.kind == inspect.Parameter.KEYWORD_ONLY


class TestProgramsSeamRejections:
    def test_wrong_source_rejected(self, tmp_path):
        store = DisclosureReviewStore(
            tmp_path / "sec_disclosure_reviews.json", source=SEC_SOURCE)
        receipt = record_completed_disclosure_review(
            dart_fact(), reviewed_at=REVIEWED, store=store)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_fact"

    def test_naive_reviewed_at_rejected(self, tmp_path):
        store = DisclosureReviewStore(
            tmp_path / "sec_disclosure_reviews.json", source=SEC_SOURCE)
        receipt = record_completed_disclosure_review(
            sec_fact(), reviewed_at=datetime(2026, 7, 27, 12, 30, 0),
            store=store)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_review"

    def test_reviewed_before_observed_rejected(self, tmp_path):
        store = DisclosureReviewStore(
            tmp_path / "sec_disclosure_reviews.json", source=SEC_SOURCE)
        before = NOW - timedelta(minutes=1)
        receipt = record_completed_disclosure_review(
            sec_fact(), reviewed_at=before, store=store)
        assert receipt.status is SaveStatus.FAILED
        assert receipt.reason == "invalid_review"
