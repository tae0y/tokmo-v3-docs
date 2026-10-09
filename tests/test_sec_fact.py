"""Offline SEC tracer tests: typed value, parsers, query, store, tracer path."""

import json
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokmo.facts.sec import (
    SOURCE,
    _SecTransportFailure,
    _SecTransportSuccess,
    build_submissions_request,
    build_tickers_request,
    parse_company_tickers,
    parse_submissions,
    query_company_tickers,
    query_submissions,
)
from tokmo.programs.tracer import run_tracer
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.values import SaveStatus, SecFilingFact, UnavailableFact

FIXTURES = Path(__file__).parent / "fixtures"
TICKERS_BODY = (FIXTURES / "sec_company_tickers_success.json").read_bytes()
SUBMISSIONS_BODY = (FIXTURES / "sec_submissions_success.json").read_bytes()
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
UA_SECRET = "TokmoTest/0.0 (fixture-contact@example.invalid)"
AAPL_CIK = "0000320193"


def sec_fact(accession="0000320193-26-000077", form="10-Q"):
    return SecFilingFact(
        source=SOURCE,
        ticker="AAPL",
        cik=AAPL_CIK,
        company_name="Apple Inc.",
        accession_number=accession,
        form=form,
        filing_date=date(2026, 7, 20),
        report_date=date(2026, 6, 27),
        primary_document="aapl-20260627.htm",
        is_xbrl=True,
        observed_at=NOW,
    )


def test_ticker_map_parses_aapl_with_zero_padded_cik():
    mapping = parse_company_tickers(TICKERS_BODY, ticker="AAPL", observed_at=NOW)
    assert (mapping.ticker, mapping.cik, mapping.company_name) == (
        "AAPL",
        AAPL_CIK,
        "Apple Inc.",
    )


def test_ticker_map_without_requested_ticker_is_typed_unavailable():
    result = parse_company_tickers(TICKERS_BODY, ticker="ZZZZ", observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "no_published_data"


@pytest.mark.parametrize(
    "body,reason",
    [
        (b"{", "parse_error"),
        (b"[]", "contract_mismatch"),
        (b'{"0":{"ticker":"AAPL","title":"Apple Inc."}}', "contract_mismatch"),
        (
            b'{"0":{"cik_str":"320193","ticker":"AAPL","title":"Apple Inc."}}',
            "contract_mismatch",
        ),
        (
            b'{"0":{"cik_str":0,"ticker":"AAPL","title":"Apple Inc."}}',
            "contract_mismatch",
        ),
    ],
)
def test_ticker_map_malformed_shapes_fail_closed(body, reason):
    result = parse_company_tickers(body, ticker="AAPL", observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == reason


def test_submissions_latest_filing_uses_index_zero_columns():
    """Fixture mirrors the probe-observed minimal structure, including a
    relative-path primaryDocument and integer isXBRL flags."""
    fact = parse_submissions(
        SUBMISSIONS_BODY,
        ticker="AAPL",
        cik=AAPL_CIK,
        company_name="Apple Inc.",
        observed_at=NOW,
    )
    assert isinstance(fact, SecFilingFact)
    assert fact.accession_number == "0001140361-26-025622"
    assert fact.form == "4"
    assert fact.filing_date == date(2026, 6, 17)
    assert fact.report_date == date(2026, 6, 15)
    assert fact.primary_document == "xslF345X06/form4.xml"
    assert fact.is_xbrl is False
    assert fact.source_ref == f"{SOURCE}:{AAPL_CIK}:0001140361-26-025622"


def test_submissions_empty_report_date_and_missing_isxbrl_are_none():
    payload = json.loads(SUBMISSIONS_BODY)
    recent = payload["filings"]["recent"]
    recent["reportDate"][0] = ""
    del recent["isXBRL"]
    fact = parse_submissions(
        json.dumps(payload).encode(),
        ticker="AAPL",
        cik=AAPL_CIK,
        company_name="Apple Inc.",
        observed_at=NOW,
    )
    assert isinstance(fact, SecFilingFact)
    assert fact.report_date is None
    assert fact.is_xbrl is None


def mutate(**changes):
    payload = json.loads(SUBMISSIONS_BODY)
    recent = payload["filings"]["recent"]
    recent.update(changes)
    return json.dumps(payload).encode()


@pytest.mark.parametrize(
    "body,reason",
    [
        (b"{", "parse_error"),
        (b'{"cik":"320193"}', "contract_mismatch"),
        (mutate(form=["10-Q"]), "contract_mismatch"),  # misaligned column lengths
        (mutate(isXBRL=[1]), "contract_mismatch"),
        (
            mutate(
                accessionNumber=[],
                filingDate=[],
                reportDate=[],
                form=[],
                primaryDocument=[],
                isXBRL=[],
            ),
            "no_published_data",
        ),
        (mutate(accessionNumber="not-a-list"), "contract_mismatch"),
        (mutate(filingDate=["not-a-date", "2026-05-02"]), "parse_error"),
        (
            json.dumps({**json.loads(SUBMISSIONS_BODY), "cik": "999999"}).encode(),
            "contract_mismatch",
        ),
    ],
)
def test_submissions_malformed_structures_fail_closed(body, reason):
    result = parse_submissions(
        body, ticker="AAPL", cik=AAPL_CIK, company_name="Apple Inc.", observed_at=NOW
    )
    assert isinstance(result, UnavailableFact)
    assert result.reason == reason


def test_query_tickers_success_and_transport_failure_mapping():
    ok = query_company_tickers(
        ticker="AAPL",
        user_agent=UA_SECRET,
        transport=lambda _request: _SecTransportSuccess(TICKERS_BODY),
        clock=lambda: NOW,
    )
    assert ok.attempt.code == "success"
    assert ok.result.cik == AAPL_CIK

    failed = query_company_tickers(
        ticker="AAPL",
        user_agent=UA_SECRET,
        transport=lambda _request: _SecTransportFailure("auth_rejection"),
        clock=lambda: NOW,
    )
    assert failed.attempt.code == "auth_rejection"
    assert isinstance(failed.result, UnavailableFact)
    assert failed.result.reason == "contract_mismatch"


def test_query_submissions_sanitizes_raising_transport():
    def transport(_request):
        raise RuntimeError(f"must not escape {UA_SECRET}")

    result = query_submissions(
        ticker="AAPL",
        cik=AAPL_CIK,
        company_name="Apple Inc.",
        user_agent=UA_SECRET,
        transport=transport,
        clock=lambda: NOW,
    )
    assert result.attempt.code == "protocol_failure"
    assert isinstance(result.result, UnavailableFact)
    assert UA_SECRET not in repr(result)


def test_requests_carry_user_agent_out_of_repr():
    for request in (
        build_tickers_request(user_agent=UA_SECRET),
        build_submissions_request(cik=AAPL_CIK, user_agent=UA_SECRET),
    ):
        assert UA_SECRET not in repr(request)
        assert request.user_agent == UA_SECRET


def test_fact_store_saves_duplicates_and_conflicts_sec_filings(tmp_path):
    store = FactStore(
        tmp_path / "sec_facts.json",
        clock=lambda: NOW,
        id_factory=iter(["r1", "r2", "r3"]).__next__,
    )
    first = store.save(sec_fact(), policy_version="tracer1-v1")
    duplicate = store.save(sec_fact(), policy_version="tracer1-v1")
    conflict = store.save(sec_fact(form="10-K"), policy_version="tracer1-v1")

    assert (first.status, duplicate.status, conflict.status) == (
        SaveStatus.SAVED,
        SaveStatus.DUPLICATE,
        SaveStatus.CONFLICT,
    )
    reopened = FactStore(
        tmp_path / "sec_facts.json", clock=lambda: NOW, id_factory=lambda: "unused"
    )
    assert reopened.records()[0]["accession_number"] == "0000320193-26-000077"


def test_run_tracer_saves_sec_fact_through_same_boundary(tmp_path):
    ids = iter(["record", "decision"])
    result = run_tracer(
        sec_fact(),
        store=FactStore(
            tmp_path / "sec_facts.json", clock=lambda: NOW, id_factory=ids.__next__
        ),
        journal=DecisionJournal(tmp_path / "sec_decisions.jsonl"),
        clock=lambda: NOW,
        id_factory=ids.__next__,
        run_id="run",
    )
    assert (result.decision.action, result.decision.reason) == ("SAVE", "saved")
    row = json.loads((tmp_path / "sec_decisions.jsonl").read_text())
    assert row["source_ref"] == f"{SOURCE}:{AAPL_CIK}:0000320193-26-000077"
