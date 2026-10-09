"""Offline OpenDART tracer tests: typed value, ZIP/XML mapping parser,
disclosure-list JSON parser, query wiring, store identity, tracer path."""

import io
import json
import stat
import zipfile
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from tokmo.facts.opendart import (
    CORP_CODE_XML_NAME,
    SOURCE,
    ZIP_MAX_ENTRIES,
    ZIP_MAX_UNCOMPRESSED_BYTES,
    _OpenDartTransportFailure,
    _OpenDartTransportSuccess,
    build_corp_code_request,
    build_list_request,
    parse_corp_code_zip,
    parse_disclosure_list,
    query_corp_code,
    query_disclosure_list,
)
from tokmo.programs.tracer import run_tracer
from tokmo.records.decision_journal import DecisionJournal
from tokmo.records.fact_store import FactStore
from tokmo.values import OpenDartDisclosureFact, SaveStatus, UnavailableFact

FIXTURES = Path(__file__).parent / "fixtures"
CORPCODE_XML = (FIXTURES / "opendart_corpcode_success.xml").read_bytes()
LIST_BODY = (FIXTURES / "opendart_list_success.json").read_bytes()
NOW = datetime(2026, 7, 27, 12, 0, 0, tzinfo=UTC)
KEY_SECRET = "fixture-opendart-key-0000000000000000000"
STOCK = "000660"
CORP = "00164779"
RCEPT = "20260727000169"


def zip_body(entries=((CORP_CODE_XML_NAME, None),)):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, CORPCODE_XML if data is None else data)
    return buffer.getvalue()


def dart_fact(report_name="동일인등출자계열회사와의상품ㆍ용역거래변경", rcept_no=RCEPT):
    return OpenDartDisclosureFact(
        source=SOURCE,
        stock_code=STOCK,
        corp_code=CORP,
        corp_name="SK하이닉스",
        corp_cls="Y",
        report_name=report_name,
        receipt_number=rcept_no,
        filer_name="SK하이닉스",
        receipt_date=date(2026, 7, 27),
        remark="공",
        observed_at=NOW,
    )


def error_xml(status):
    return (
        f'<?xml version="1.0" encoding="UTF-8"?>'
        f"<result><status>{status}</status>"
        f"<message>masked</message></result>"
    ).encode()


def list_payload(**row_changes):
    payload = json.loads(LIST_BODY)
    payload["list"][0].update(row_changes)
    return payload


# --- corpCode ZIP/XML mapping parser ----------------------------------------


def test_corp_code_zip_maps_000660_row():
    mapping = parse_corp_code_zip(zip_body(), stock_code=STOCK, observed_at=NOW)
    assert (mapping.stock_code, mapping.corp_code, mapping.corp_name) == (
        STOCK,
        CORP,
        "SK하이닉스",
    )
    assert mapping.modify_date == date(2024, 3, 28)  # actual probe-observed row


def test_corp_code_zip_without_target_row_is_typed_unavailable():
    result = parse_corp_code_zip(zip_body(), stock_code="999999", observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "no_published_data"


@pytest.mark.parametrize(
    "entries",
    [
        (("../evil.xml", None),),
        (("/CORPCODE.xml", None),),
        (("a\\b.xml", None),),
        (("OTHER.xml", None),),
        (
            (CORP_CODE_XML_NAME, None),
            ("a", b"1"),
            ("b", b"2"),
            ("c", b"3"),
            ("d", b"4"),
        ),
        ((CORP_CODE_XML_NAME, b"0" * (ZIP_MAX_UNCOMPRESSED_BYTES + 1)),),
    ],
)
def test_corp_code_zip_guards_fail_closed(entries):
    assert len(entries) <= ZIP_MAX_ENTRIES + 1
    result = parse_corp_code_zip(zip_body(entries), stock_code=STOCK, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "contract_mismatch"


def test_corp_code_zip_rejects_symlink_like_entry():
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        info = zipfile.ZipInfo(CORP_CODE_XML_NAME)
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, CORPCODE_XML)
    result = parse_corp_code_zip(buffer.getvalue(), stock_code=STOCK, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "contract_mismatch"


@pytest.mark.parametrize(
    "xml,reason",
    [
        (
            b"<result><list><corp_code>164779</corp_code><corp_name>SK</corp_name>"
            b"<stock_code>000660</stock_code><modify_date>20260415</modify_date>"
            b"</list></result>",
            "contract_mismatch",
        ),
        (
            b"<result><list><corp_code>00164779</corp_code><corp_name>SK</corp_name>"
            b"<stock_code>000660</stock_code><modify_date>2026-04</modify_date>"
            b"</list></result>",
            "parse_error",
        ),
        (
            b"<result><list><corp_code>00164779</corp_code><corp_name>SK</corp_name>"
            b"<stock_code>000660</stock_code>",
            "parse_error",
        ),
    ],
)
def test_corp_code_zip_malformed_rows_fail_closed(xml, reason):
    body = zip_body(((CORP_CODE_XML_NAME, xml),))
    result = parse_corp_code_zip(body, stock_code=STOCK, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == reason


def test_non_zip_garbage_body_is_parse_error():
    result = parse_corp_code_zip(b"not-a-zip", stock_code=STOCK, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "parse_error"


def test_non_zip_plain_xml_without_status_is_contract_mismatch():
    result = parse_corp_code_zip(CORPCODE_XML, stock_code=STOCK, observed_at=NOW)
    assert isinstance(result, UnavailableFact)
    assert result.reason == "contract_mismatch"


# --- disclosure-list JSON parser --------------------------------------------


def test_list_latest_row_becomes_typed_disclosure_fact():
    """Fixture mirrors the probe-observed row: the filer is the company
    itself and rm carries a short non-empty marker."""
    fact = parse_disclosure_list(
        LIST_BODY, corp_code=CORP, stock_code=STOCK, observed_at=NOW
    )
    assert isinstance(fact, OpenDartDisclosureFact)
    assert (fact.corp_code, fact.stock_code, fact.corp_cls) == (CORP, STOCK, "Y")
    assert fact.receipt_number == RCEPT
    assert fact.receipt_date == date(2026, 7, 27)
    assert fact.filer_name == "SK하이닉스"
    assert fact.remark == "공"
    assert fact.source_ref == f"{SOURCE}:{CORP}:{RCEPT}"


@pytest.mark.parametrize("raw_remark", ["", " ", None])
def test_list_empty_or_absent_remark_normalizes_to_none(raw_remark):
    payload = list_payload(rm=raw_remark)
    if raw_remark is None:
        del payload["list"][0]["rm"]
    fact = parse_disclosure_list(
        json.dumps(payload).encode(), corp_code=CORP, stock_code=STOCK, observed_at=NOW
    )
    assert fact.remark is None


@pytest.mark.parametrize(
    "payload,reason",
    [
        (b"{", "parse_error"),
        (b"[]", "contract_mismatch"),
        (b'{"status":"000","page_no":2,"list":[]}', "contract_mismatch"),
        (
            json.dumps({**json.loads(LIST_BODY), "list": []}).encode(),
            "no_published_data",
        ),
        (
            json.dumps({**json.loads(LIST_BODY), "list": "x"}).encode(),
            "contract_mismatch",
        ),
        (json.dumps(list_payload(corp_code="00000001")).encode(), "contract_mismatch"),
        (json.dumps(list_payload(stock_code="005930")).encode(), "contract_mismatch"),
        (json.dumps(list_payload(corp_cls="Z")).encode(), "contract_mismatch"),
        (json.dumps(list_payload(rcept_no="123")).encode(), "contract_mismatch"),
        (json.dumps(list_payload(rcept_dt="2026-07-21")).encode(), "parse_error"),
        (json.dumps(list_payload(flr_nm="")).encode(), "contract_mismatch"),
        (json.dumps(list_payload(rm=7)).encode(), "contract_mismatch"),
    ],
)
def test_list_malformed_shapes_fail_closed(payload, reason):
    result = parse_disclosure_list(
        payload, corp_code=CORP, stock_code=STOCK, observed_at=NOW
    )
    assert isinstance(result, UnavailableFact)
    assert result.reason == reason


# --- query wiring: provider status classes and transport outcomes -----------


def query_list_with_body(body):
    return query_disclosure_list(
        corp_code=CORP,
        stock_code=STOCK,
        begin_date="20260428",
        end_date="20260727",
        api_key=KEY_SECRET,
        transport=lambda _request: _OpenDartTransportSuccess(body),
        clock=lambda: NOW,
    )


def test_query_corp_code_success_closes_hypothesis():
    result = query_corp_code(
        stock_code=STOCK,
        api_key=KEY_SECRET,
        transport=lambda _request: _OpenDartTransportSuccess(zip_body()),
        clock=lambda: NOW,
    )
    assert result.attempt.code == "success"
    assert result.result.corp_code == CORP


def test_query_corp_code_key_rejection_status_is_distinguished():
    result = query_corp_code(
        stock_code=STOCK,
        api_key=KEY_SECRET,
        transport=lambda _request: _OpenDartTransportSuccess(error_xml("010")),
        clock=lambda: NOW,
    )
    assert result.attempt.code == "provider_key_rejected"
    assert result.result.reason == "contract_mismatch"


@pytest.mark.parametrize(
    "status,attempt_code,reason",
    [
        ("013", "provider_no_data", "no_published_data"),
        ("020", "provider_rate_limited", "rate_limited"),
        ("101", "provider_key_rejected", "contract_mismatch"),
        ("900", "response_contract_failure", "contract_mismatch"),
    ],
)
def test_query_list_provider_statuses_map_to_closed_codes(status, attempt_code, reason):
    body = json.dumps({"status": status, "message": "masked"}).encode()
    result = query_list_with_body(body)
    assert result.attempt.code == attempt_code
    assert isinstance(result.result, UnavailableFact)
    assert result.result.reason == reason


def test_query_list_success_and_transport_failure_mapping():
    ok = query_list_with_body(LIST_BODY)
    assert ok.attempt.code == "success"
    assert ok.result.receipt_number == RCEPT

    failed = query_disclosure_list(
        corp_code=CORP,
        stock_code=STOCK,
        begin_date="20260428",
        end_date="20260727",
        api_key=KEY_SECRET,
        transport=lambda _request: _OpenDartTransportFailure(
            "http_429", retry_after=60
        ),
        clock=lambda: NOW,
    )
    assert failed.attempt.code == "http_429"
    assert failed.attempt.retry_after == 60
    assert failed.result.reason == "rate_limited"


def test_query_sanitizes_raising_transport_and_reprs_exclude_key():
    def transport(_request):
        raise RuntimeError(f"must not escape {KEY_SECRET}")

    result = query_corp_code(
        stock_code=STOCK, api_key=KEY_SECRET, transport=transport, clock=lambda: NOW
    )
    assert result.attempt.code == "protocol_failure"
    assert isinstance(result.result, UnavailableFact)
    assert KEY_SECRET not in repr(result)

    for request in (
        build_corp_code_request(api_key=KEY_SECRET),
        build_list_request(
            corp_code=CORP,
            begin_date="20260428",
            end_date="20260727",
            api_key=KEY_SECRET,
        ),
    ):
        assert KEY_SECRET not in repr(request)
        assert request.api_key == KEY_SECRET


# --- same storage and tracer boundary ---------------------------------------


def test_fact_store_saves_duplicates_and_conflicts_disclosures(tmp_path):
    store = FactStore(
        tmp_path / "opendart_facts.json",
        clock=lambda: NOW,
        id_factory=iter(["r1", "r2", "r3"]).__next__,
    )
    first = store.save(dart_fact(), policy_version="tracer1-v1")
    duplicate = store.save(dart_fact(), policy_version="tracer1-v1")
    conflict = store.save(
        dart_fact(report_name="주요사항보고서"), policy_version="tracer1-v1"
    )

    assert (first.status, duplicate.status, conflict.status) == (
        SaveStatus.SAVED,
        SaveStatus.DUPLICATE,
        SaveStatus.CONFLICT,
    )
    reopened = FactStore(
        tmp_path / "opendart_facts.json", clock=lambda: NOW, id_factory=lambda: "unused"
    )
    assert reopened.records()[0]["receipt_number"] == RCEPT


def test_run_tracer_saves_disclosure_through_same_boundary(tmp_path):
    ids = iter(["record", "decision"])
    result = run_tracer(
        dart_fact(),
        store=FactStore(
            tmp_path / "opendart_facts.json", clock=lambda: NOW, id_factory=ids.__next__
        ),
        journal=DecisionJournal(tmp_path / "opendart_decisions.jsonl"),
        clock=lambda: NOW,
        id_factory=ids.__next__,
        run_id="run",
    )
    assert (result.decision.action, result.decision.reason) == ("SAVE", "saved")
    row = json.loads((tmp_path / "opendart_decisions.jsonl").read_text())
    assert row["source_ref"] == f"{SOURCE}:{CORP}:{RCEPT}"
