import json
from datetime import date, datetime, timezone

import pytest

from tokmo.facts.opendart_bulk import (
    BulkContractError, BulkRequest, parse_page,
)


def row(receipt="20260000000001", stock="005930", name=" report "):
    return {"corp_code": "00126380", "corp_name": " Samsung ",
            "stock_code": stock, "corp_cls": "Y", "report_nm": name,
            "rcept_no": receipt, "flr_nm": " filer ",
            "rcept_dt": "20260701", "rm": " "}


def envelope(rows, *, page=1, total_count=None, total_page=None):
    total_count = len(rows) if total_count is None else total_count
    total_page = ((total_count + 99) // 100) if total_page is None else total_page
    return json.dumps({"status": "000", "page_no": page, "page_count": 100,
                       "total_count": total_count, "total_page": total_page,
                       "list": rows}).encode()


def test_request_has_exact_fixed_allowlist_and_no_corp_code():
    request = BulkRequest.create(date(2026, 7, 1), date(2026, 7, 31), 2)
    assert request.query_pairs == (("bgn_de", "20260701"), ("end_de", "20260731"),
                                   ("sort", "date"), ("sort_mth", "asc"),
                                   ("page_count", "100"), ("last_reprt_at", "N"),
                                   ("page_no", "2"))
    assert "corp_code" not in dict(request.query_pairs)
    with pytest.raises(TypeError):
        BulkRequest.create(date(2026, 7, 1), date(2026, 7, 2), 1, extra="x")


def test_91_day_window_rejected_before_transport():
    with pytest.raises(BulkContractError, match="window_too_large"):
        BulkRequest.create(date(2026, 1, 1), date(2026, 4, 1), 1)


def test_multirow_parse_normalizes_blank_stock_and_does_not_assume_receipt_date():
    parsed = parse_page(envelope([row(stock=" "), row("19990000000002")]), 1)
    assert parsed.rows[0].stock_code is None
    assert parsed.rows[0].report_nm == "report"
    assert parsed.rows[1].rcept_no == "19990000000002"
    with pytest.raises(BulkContractError, match="invalid_stock_code"):
        parse_page(envelope([row(stock="123")]), 1)


@pytest.mark.parametrize("stock", ["A05930", "00593K", "005930"])
def test_six_character_ascii_uppercase_alphanumeric_stock_code_is_canonical(stock):
    parsed = parse_page(envelope([row(stock=f" {stock} ")]), 1)
    assert parsed.rows[0].stock_code == stock


@pytest.mark.parametrize("stock", [
    "a05930", "00593k", "0059-0", "0059 0", "Ａ05930", "00593가",
])
def test_noncanonical_six_character_stock_code_is_rejected(stock):
    with pytest.raises(BulkContractError, match="invalid_stock_code"):
        parse_page(envelope([row(stock=stock)]), 1)


@pytest.mark.parametrize("code", ["010", "011", "012", "020", "021", "101"])
def test_provider_stop_code_is_preserved_without_provider_text(code):
    body = json.dumps({"status": code, "message": "RAW SECRET PROVIDER TEXT"}).encode()
    with pytest.raises(BulkContractError, match=f"provider_{code}$") as caught:
        parse_page(body, 1)
    assert "RAW" not in str(caught.value)


@pytest.mark.parametrize("change", [
    {"page_no": 2}, {"page_count": 99}, {"total_page": 2},
    {"total_count": -1}, {"list": [row() for _ in range(101)]},
])
def test_metadata_mismatch_is_closed(change):
    payload = json.loads(envelope([row()]))
    payload.update(change)
    with pytest.raises(BulkContractError):
        parse_page(json.dumps(payload).encode(), 1)


def test_nonfinal_page_requires_100_rows():
    with pytest.raises(BulkContractError, match="nonfinal_cardinality"):
        parse_page(envelope([row()], total_count=101, total_page=2), 1)
