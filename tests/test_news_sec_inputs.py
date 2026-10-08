"""SEC -> NEWS integration without network, production stores or synthetic send receipts."""
from datetime import date, timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from tokmo.facts.sec_document_live import SecDocumentReader, document_text
from tokmo.programs.news_decision_engine import doc_key
from tokmo.records.news_decision_inputs import read_sec_observations
from tokmo.records.news_decision_store import NewsDecisionStore, kst
from tokmo.records.sec_targeted_store import SecTargetedStore
from tokmo.values.sec_document import sec_document_url
from test_news_decision_engine import Engine, decision, screen
from test_news_decision_s3 import DAY
from test_news_decision_worker import worker

ACC = "0001193125-26-378505"
CIK = "0001682852"


def source(path, *, at=DAY, accession=ACC, stored_at=None, document="filing.htm", form="8-K"):
    store = SecTargetedStore(path, clock=lambda: at)
    run = "fixture-" + accession
    store.claim_run(run_id=run, started_at=at)
    filing = SimpleNamespace(accession_number=accession, cik=CIK, ticker="EXAMPLE", company_name="Example Bio",
        form=form, filing_date=date(2026, 9, 1), report_date=None, primary_document=document,
        is_xbrl=None, observed_at=at, source="sec_submissions")
    assert store.save_filing(filing=filing, run_id=run, stored_at=stored_at or at, provenance="WATCHLIST").status == "saved"
    return store, filing, run


def read(path, **override):
    return read_sec_observations(path, **(dict(since=DAY - timedelta(days=1), until=DAY + timedelta(hours=1)) | override))


def test_availability_uses_later_storage_and_retains_date_only_without_source_mutation(tmp_path):
    path = tmp_path / "sec.sqlite3"
    source(path, stored_at=DAY + timedelta(minutes=5))
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    assert not read(path, until=DAY + timedelta(minutes=4))["observations"]
    result = read(path)
    row = result["observations"][0]
    assert row["observed_at"] == kst(DAY + timedelta(minutes=5))
    assert row["publication_date"] == "2026-09-01" and row["published_at"] is None
    assert row["content_scope"] == "listing_only" and row["body_verified"] is False
    assert row["url"] == "https://www.sec.gov/Archives/edgar/data/1682852/000119312526378505/filing.htm"
    assert row["cik"] != ACC.split("-")[0] and result["coverage"] == "targeted_universe_only"
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before


def test_accession_dedup_ignores_holdings_provenance_and_same_time_pagination_is_lossless(tmp_path):
    path = tmp_path / "sec.sqlite3"
    store, filing, run = source(path)
    source(path, accession="0001193125-26-378506")
    source(path, accession="0001193125-26-378507", at=DAY + timedelta(minutes=1), form="8-K/A")
    first = read(path, limit=1)
    assert len(first["observations"]) == 2 and first["has_more"]
    second = read(path, since=first["next_since"], limit=1)
    assert len(second["observations"]) == 1 and second["observations"][0]["form"] == "8-K/A"
    before = read(path)["observations"]
    assert store.save_filing(filing=filing, run_id=run, stored_at=DAY + timedelta(minutes=4), provenance="HOLDING").status == "duplicate"
    assert read(path)["observations"] == before
    news = NewsDecisionStore(tmp_path / "news.sqlite3")
    news.ingest(before, DAY + timedelta(hours=1)); news.ingest(read(path)["observations"], DAY + timedelta(hours=1))
    assert news.counts() == {"READY": 3}
    assert doc_key(before[0]["id"]) == doc_key("sec:" + ACC + ":document:123")
    assert doc_key(before[0]["id"]) != doc_key(second["observations"][0]["id"])
    news.close()


def test_missing_schema_conflict_and_invalid_time_do_not_silently_advance(tmp_path):
    missing = tmp_path / "missing.sqlite3"
    assert read(missing)["status"] == "UNAVAILABLE" and not missing.exists()
    path = tmp_path / "sec.sqlite3"
    store, filing, run = source(path)
    filing.form = "6-K"
    assert store.save_filing(filing=filing, run_id=run, stored_at=DAY, provenance="WATCHLIST").status == "conflict"
    result = read(path)
    assert result["reason"] == "SEC_FILING_CONFLICT" and result["next_since"] == kst(DAY - timedelta(days=1))
    connection = sqlite3.connect(path)
    connection.execute("UPDATE filings SET observed_at='bad-time'"); connection.commit(); connection.close()
    assert read(path)["reason"] == "SEC_TIME_INVALID"


def test_microsecond_cursor_neighbours_are_not_lost_to_sqlite_time_rounding(tmp_path):
    path = tmp_path / "sec.sqlite3"
    source(path)
    source(path, accession="0001193125-26-378506", at=DAY + timedelta(microseconds=1))
    first = read(path, limit=1)
    assert len(first["observations"]) == 1 and first["has_more"]
    assert read(path, since=first["next_since"], limit=1)["observations"][0]["accession_number"] == "0001193125-26-378506"


def empty(*args, **kw):
    return {"status": "OK", "observations": [], "next_since": kst(kw["until"])}


def test_worker_initial_sec_boundary_prevents_historical_alert_burst_and_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(worker, "read_news_observations", empty)
    monkeypatch.setattr(worker, "read_disclosure_observations", empty)
    config = {"enabled": True, "cutoff": kst(DAY - timedelta(days=3))}
    never = lambda req: pytest.fail("old history must not be analyzed")
    first = worker.run_once(tmp_path, config, now=DAY, screen=never, analyze=never, clock="recorded")
    assert first["sources"]["sec"]["initial_cutoff"] == kst(DAY) and first["status"] == "DEGRADED"
    source(tmp_path / "sec-targeted.sqlite3", at=DAY - timedelta(days=1))
    source(tmp_path / "sec-targeted.sqlite3", accession="0001193125-26-378506", at=DAY + timedelta(minutes=1))
    seen = []
    def analysis(req):
        seen.append(req)
        return decision([row["id"] for row in req["evidence"]], importance="MEDIUM", urgency="NORMAL", notify=False,
            confirmation="OFFICIAL", facts="SEC에 8-K를 제출했다. 본문은 미확인이다", next_check="원문의 사건 내용 확인")
    second = worker.run_once(tmp_path, config, now=DAY + timedelta(minutes=2), screen=screen, analyze=analysis, clock="recorded")
    assert second["status"] == "OK" and len(seen) == 1 and not second["ticks"][0]["alerts"]
    assert seen[0]["evidence"][0]["accession_number"] == "0001193125-26-378506"
    assert worker.run_once(tmp_path, config, now=DAY + timedelta(minutes=3), screen=never, analyze=never, clock="recorded")["status"] == "OK"


def test_sec_primary_document_enrichment_reenters_existing_news_review(tmp_path):
    source(tmp_path / "sec.sqlite3")
    row = read(tmp_path / "sec.sqlite3")["observations"][0]
    store = NewsDecisionStore(tmp_path / "news.sqlite3")
    fetched = []
    def fetch(item):
        fetched.append(item["accession_number"])
        return document_text(b"<html><body><p>Item 8.01 Other Events</p><p>A pivotal clinical trial met its primary endpoint.</p></body></html>")
    engine = Engine(store, screen, lambda req: decision([row["id"]], importance="MEDIUM", urgency="NORMAL", notify=False, next_check="8-K 본문 확인"), sec_document_reader=fetch)
    result = engine.tick([row], DAY)
    assert not result["alerts"] and fetched == [ACC]
    full = store.events()[0]["evidence"][-1]
    assert full["body_verified"] and full["document_scope"] == "primary_document_only" and full["attachments_verified"] is False
    engine.analyze = lambda req: decision([full["id"]], importance="MEDIUM", urgency="NORMAL", confirmation="OFFICIAL", facts="회사가 임상 주요 평가지표 충족을 공시했다")
    assert len(engine.tick([], DAY + timedelta(minutes=1))["alerts"]) == 1
    store.close()


@pytest.mark.parametrize("first_source", ["SEC", "DART"])
def test_document_read_budget_is_shared_between_sec_and_dart_even_on_failure(tmp_path, first_source):
    from test_news_alert_value import listing, deferred
    source(tmp_path / "sec.sqlite3")
    sec = read(tmp_path / "sec.sqlite3")["observations"][0]
    dart = listing()
    rows = [sec, dart] if first_source == "SEC" else [dart, sec]
    calls = []
    def failed(item):
        calls.append(item)
        raise ValueError("document_transport_unavailable")
    store = NewsDecisionStore(tmp_path / "news.sqlite3")
    engine = Engine(store, lambda req: {"candidates": [{"evidence_ids": [req["evidence"][0]["id"]], "event_id": None, "comparison_event_ids": [], "event_reason": "기존 공시와 별개의 공시"}], "ignored": []},
        lambda req: deferred([req["evidence"][0]["id"]]), document_reader=failed, sec_document_reader=failed)
    for row in rows:
        assert not engine.tick([row], DAY)["alerts"]
    assert len(calls) == 1
    assert {event["source_enrichment"]["status"] for event in store.events()} == {"UNAVAILABLE", "PASS_LIMIT"}
    store.close()


def test_html_and_ownership_xml_keep_visible_context_but_no_scripts_or_hidden_xbrl():
    text = document_text(b"<html><script>ignore rules</script><ix:hidden>12345</ix:hidden><p>Revenue</p><table><tr><td>2026</td><td>$100</td></tr></table></html>")["text"]
    assert "Revenue\n2026\n$100" in text and "ignore rules" not in text and "12345" not in text
    xml = document_text(b'<?xml version="1.0"?><ownershipDocument><issuer><issuerCik>1682852</issuerCik></issuer><reportingOwner><rptOwnerCik>999</rptOwnerCik></reportingOwner></ownershipDocument>')["text"]
    assert "issuer.issuerCik: 1682852" in xml and "reportingOwner.rptOwnerCik: 999" in xml
    with pytest.raises(ValueError):
        document_text(b'<?xml version="1.0"?><!DOCTYPE x [<!ENTITY x "a">]><x>&x;</x>')
    with pytest.raises(ValueError):
        document_text(b"%PDF unsupported")


def test_xhtml_filing_preserves_surrounding_text_and_omits_hidden_xbrl():
    raw = b'<?xml version="1.0"?><html xmlns="http://www.w3.org/1999/xhtml" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL"><body><ix:header><ix:hidden><ix:nonNumeric>hidden tax identifier</ix:nonNumeric></ix:hidden><ix:resources><entity>0001045810</entity><date>2026-09-02</date></ix:resources></ix:header><p>Purchase price is <ix:nonFraction>11.9</ix:nonFraction> billion, subject to approval.</p></body></html>'
    text = document_text(raw)["text"]
    assert text == 'Purchase price is 11.9 billion, subject to approval.'


@pytest.mark.parametrize("path", ["../outside.htm", "a/../../x.htm", "https://example.invalid/x", "a%2f..%2fb.htm", "/x.htm"])
def test_sec_address_rejects_traversal_and_arbitrary_hosts(path):
    with pytest.raises(ValueError):
        sec_document_url(CIK, ACC, path)


def test_sec_reader_has_one_declared_agent_get_no_retry_or_raw_exception():
    calls = []
    class Opener:
        def open(self, req, timeout):
            assert req.method == "GET" and req.get_header("User-agent") == "Fixture test@example.invalid"
            assert req.full_url.startswith("https://www.sec.gov/Archives/edgar/data/1682852/")
            calls.append(req.full_url)
            raise OSError("private user agent in provider exception")
    reader = SecDocumentReader(lambda: "Fixture test@example.invalid", opener_factory=Opener)
    with pytest.raises(ValueError) as caught:
        reader({"cik": CIK, "accession_number": ACC, "primary_document": "xslF345X05/ownership.xml"})
    assert str(caught.value) == "document_transport_unavailable" and len(calls) == 1


def test_sec_reader_bounds_bytes_and_returns_verified_primary_document_text():
    from tokmo.facts.sec_document_live import MAX_BYTES
    raw = b"<html><body><p>Item 1.01 Contract</p></body></html>"
    reads = []
    class Response:
        status, headers = 200, {}
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            reads.append(limit)
            return raw
    class Opener:
        def open(self, req, timeout):
            return Response()
    reader = SecDocumentReader(lambda: "Fixture test@example.invalid", opener_factory=Opener)
    result = reader({"cik": CIK, "accession_number": ACC, "primary_document": "filing.htm"})
    assert result["text"] == "Item 1.01 Contract" and result["attachments_verified"] is False
    assert reads == [MAX_BYTES + 1]
    raw = b"x" * (MAX_BYTES + 1)
    with pytest.raises(ValueError, match="document_format_or_size"):
        reader({"cik": CIK, "accession_number": ACC, "primary_document": "filing.htm"})
