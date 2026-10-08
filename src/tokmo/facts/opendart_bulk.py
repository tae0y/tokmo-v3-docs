"""Pure contracts for the bounded all-disclosures OpenDART lane."""

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date


FIXED_QUERY_KEYS = ("bgn_de", "end_de", "sort", "sort_mth", "page_count",
                    "last_reprt_at", "page_no")
MAX_WINDOW_DAYS = 90
PAGE_COUNT = 100


class BulkContractError(ValueError):
    pass


class BulkTransportFailure(Exception):
    """Closed transport outcome; never contains wire/provider text."""

    ALLOWED = frozenset({"timeout", "connection_failure", "http_5xx", "http_429",
                         "auth_rejection", "bad_request", "redirect", "body_too_large",
                         "unsupported_content_encoding", "protocol_failure"})

    def __init__(self, code: str):
        if code not in self.ALLOWED:
            raise ValueError("transport_code")
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class BulkRequest:
    begin: date
    end: date
    page_no: int
    query_pairs: tuple[tuple[str, str], ...]

    @classmethod
    def create(cls, begin: date, end: date, page_no: int):
        if type(begin) is not date or type(end) is not date or end < begin:
            raise BulkContractError("invalid_window")
        if (end - begin).days >= MAX_WINDOW_DAYS:
            raise BulkContractError("window_too_large")
        if type(page_no) is not int or page_no < 1:
            raise BulkContractError("invalid_page")
        pairs = (("bgn_de", begin.strftime("%Y%m%d")),
                 ("end_de", end.strftime("%Y%m%d")), ("sort", "date"),
                 ("sort_mth", "asc"), ("page_count", "100"),
                 ("last_reprt_at", "N"), ("page_no", str(page_no)))
        if tuple(key for key, _ in pairs) != FIXED_QUERY_KEYS:
            raise BulkContractError("query_contract")
        return cls(begin, end, page_no, pairs)


@dataclass(frozen=True)
class Disclosure:
    corp_code: str
    corp_name: str
    stock_code: str | None
    corp_cls: str
    report_nm: str
    rcept_no: str
    flr_nm: str
    rcept_dt: str
    rm: str | None

    def canonical_json(self) -> str:
        return json.dumps(asdict(self), ensure_ascii=False, sort_keys=True,
                          separators=(",", ":"))

    def payload_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode()).hexdigest()


@dataclass(frozen=True)
class BulkPage:
    page_no: int
    total_count: int
    total_page: int
    rows: tuple[Disclosure, ...]


def _integer(value, reason):
    if type(value) is not int or value < 0:
        raise BulkContractError(reason)
    return value


def _required(row, name):
    value = row.get(name)
    if not isinstance(value, str) or not value.strip():
        raise BulkContractError(f"invalid_{name}")
    return value.strip()


def _optional(row, name):
    value = row.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise BulkContractError(f"invalid_{name}")
    return value.strip() or None


def _row(value) -> Disclosure:
    if not isinstance(value, dict):
        raise BulkContractError("invalid_row")
    stock = _optional(value, "stock_code")
    if stock is not None and (len(stock) != 6 or
                              any(character not in "0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ"
                                  for character in stock)):
        raise BulkContractError("invalid_stock_code")
    corp_code = _required(value, "corp_code")
    receipt = _required(value, "rcept_no")
    receipt_date = _required(value, "rcept_dt")
    if len(corp_code) != 8 or not corp_code.isdigit():
        raise BulkContractError("invalid_corp_code")
    if len(receipt) != 14 or not receipt.isdigit():
        raise BulkContractError("invalid_rcept_no")
    if len(receipt_date) != 8 or not receipt_date.isdigit():
        raise BulkContractError("invalid_rcept_dt")
    corp_cls = _required(value, "corp_cls")
    if corp_cls not in {"Y", "K", "N", "E"}:
        raise BulkContractError("invalid_corp_cls")
    return Disclosure(corp_code, _required(value, "corp_name"), stock, corp_cls,
                      _required(value, "report_nm"), receipt,
                      _required(value, "flr_nm"), receipt_date,
                      _optional(value, "rm"))


def parse_page(body: bytes, requested_page: int) -> BulkPage:
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError, TypeError):
        raise BulkContractError("invalid_json") from None
    if not isinstance(payload, dict) or payload.get("status") != "000":
        code = payload.get("status") if isinstance(payload, dict) else None
        if code == "013":
            return BulkPage(requested_page, 0, 0, ())
        stop = {"010", "011", "012", "020", "021", "101"}
        raise BulkContractError(f"provider_{code}" if code in stop else "provider_contract")
    if payload.get("page_no") != requested_page:
        raise BulkContractError("page_echo")
    if payload.get("page_count") != PAGE_COUNT:
        raise BulkContractError("page_count")
    total_count = _integer(payload.get("total_count"), "total_count")
    total_page = _integer(payload.get("total_page"), "total_page")
    expected_pages = (total_count + PAGE_COUNT - 1) // PAGE_COUNT
    if total_page != expected_pages:
        raise BulkContractError("total_page")
    rows = payload.get("list")
    if not isinstance(rows, list) or len(rows) > PAGE_COUNT:
        raise BulkContractError("list_cardinality")
    if requested_page < total_page and len(rows) != PAGE_COUNT:
        raise BulkContractError("nonfinal_cardinality")
    return BulkPage(requested_page, total_count, total_page,
                    tuple(_row(item) for item in rows))
