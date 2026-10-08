"""Primitive closed request-parity values; owns provider operation semantics."""

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
import hashlib
import json
from types import MappingProxyType
from typing import Literal, Mapping

from tokmo.values._primitives import MISSING, Opt, _Decimal30
from tokmo.values.toss_orders import Order, OrderExecution


MAX_ORDERS_JSON_BYTES = 2 * 1024 * 1024


@dataclass(frozen=True)
class OrdersPageCheckpointV1:
    schema_version: Literal[1]
    tick_id: int
    request_ordinal: int
    account_scope: str
    window_from: str
    window_to: str
    request_cursor: str | None
    orders: tuple[Order, ...]
    has_next: bool
    next_cursor: str | None


def _nonempty_string(value: object) -> bool:
    return type(value) is str and bool(value)


def _decimal(value: object) -> bool:
    return _Decimal30.parse(value) is not None


def _canonical_date(value: object) -> bool:
    if type(value) is not str:
        return False
    try:
        return date.fromisoformat(value).isoformat() == value
    except ValueError:
        return False


def _page_contract(page: object) -> OrdersPageCheckpointV1:
    if not isinstance(page, OrdersPageCheckpointV1):
        raise ValueError("invalid orders page checkpoint")
    valid_scope = (
        type(page.account_scope) is str and len(page.account_scope) == 34
        and page.account_scope.startswith("toss-acct-")
        and all(character in "0123456789abcdef" for character in page.account_scope[10:])
    )
    valid_cursor = (
        (page.request_cursor is None or
         (_nonempty_string(page.request_cursor) and len(page.request_cursor) <= 4096))
        and type(page.has_next) is bool
        and ((not page.has_next and page.next_cursor is None) or
             (page.has_next and _nonempty_string(page.next_cursor)
              and len(page.next_cursor) <= 4096
              and page.next_cursor != page.request_cursor))
    )
    if not (
        page.schema_version == 1 and type(page.schema_version) is int
        and type(page.tick_id) is int and 0 <= page.tick_id <= 2147483647
        and type(page.request_ordinal) is int and 0 <= page.request_ordinal <= 9
        and valid_scope and _canonical_date(page.window_from)
        and _canonical_date(page.window_to) and page.window_from <= page.window_to
        and type(page.orders) is tuple and valid_cursor
    ):
        raise ValueError("invalid orders page checkpoint")
    return page


def _datetime_text(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("invalid order datetime")
    return value.isoformat()


def _execution_json(value: object) -> dict[str, object]:
    if not isinstance(value, OrderExecution) or not _decimal(value.filled_quantity):
        raise ValueError("invalid order execution")
    result: dict[str, object] = {"filledQuantity": value.filled_quantity}
    for key, item in (("averageFilledPrice", value.average_filled_price),
                      ("filledAmount", value.filled_amount),
                      ("commission", value.commission), ("tax", value.tax)):
        if item is not None and not _decimal(item):
            raise ValueError("invalid order execution")
        result[key] = item
    result["filledAt"] = None if value.filled_at is None else _datetime_text(value.filled_at)
    if value.settlement_date is not None and (
        not isinstance(value.settlement_date, date)
        or isinstance(value.settlement_date, datetime)
    ):
        raise ValueError("invalid order execution")
    result["settlementDate"] = (None if value.settlement_date is None
                                else value.settlement_date.isoformat())
    return result


def _order_json(value: object) -> dict[str, object]:
    if not isinstance(value, Order):
        raise ValueError("invalid order")
    strings = (value.order_id, value.symbol, value.side, value.order_type,
               value.time_in_force, value.status, value.currency)
    if not all(_nonempty_string(item) for item in strings) or not _decimal(value.quantity):
        raise ValueError("invalid order")
    result: dict[str, object] = {
        "orderId": value.order_id, "symbol": value.symbol, "side": value.side,
        "orderType": value.order_type, "timeInForce": value.time_in_force,
        "status": value.status, "quantity": value.quantity, "currency": value.currency,
        "orderedAt": _datetime_text(value.ordered_at), "execution": _execution_json(value.execution),
    }
    for key, item in (("price", value.price), ("orderAmount", value.order_amount)):
        if item is not MISSING:
            if item is not None and not _decimal(item):
                raise ValueError("invalid order")
            result[key] = item
    if value.canceled_at is not MISSING:
        result["canceledAt"] = (None if value.canceled_at is None
                                else _datetime_text(value.canceled_at))
    return result


def encode_orders_page_checkpoint_v1(page: OrdersPageCheckpointV1) -> bytes:
    page = _page_contract(page)
    encoded = json.dumps([_order_json(order) for order in page.orders], sort_keys=True,
                         separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    if not 2 <= len(encoded) <= MAX_ORDERS_JSON_BYTES:
        raise ValueError("orders page payload exceeds bound")
    return encoded


def orders_page_digest_v1(orders_json: bytes) -> str:
    if type(orders_json) is not bytes or not 2 <= len(orders_json) <= MAX_ORDERS_JSON_BYTES:
        raise ValueError("invalid orders page bytes")
    return hashlib.sha256(orders_json).hexdigest()


def _exact_object(value: object, required: set[str], optional: set[str] = set()) -> dict:
    if type(value) is not dict or not required <= set(value) or not set(value) <= required | optional:
        raise ValueError("invalid orders page payload")
    return value


def _parse_datetime(value: object, *, nullable: bool = False):
    if value is None and nullable:
        return None
    if type(value) is not str:
        raise ValueError("invalid order datetime")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise ValueError("invalid order datetime") from None
    if parsed.tzinfo is None or parsed.utcoffset() is None or parsed.isoformat() != value:
        raise ValueError("invalid order datetime")
    return parsed


def _parse_nullable_decimal(value: object):
    if value is not None and not _decimal(value):
        raise ValueError("invalid order decimal")
    return value


def _parse_execution(value: object) -> OrderExecution:
    raw = _exact_object(value, {"filledQuantity", "averageFilledPrice", "filledAmount",
                                "commission", "tax", "filledAt", "settlementDate"})
    if not _decimal(raw["filledQuantity"]):
        raise ValueError("invalid order execution")
    settlement = raw["settlementDate"]
    if settlement is not None:
        if not _canonical_date(settlement):
            raise ValueError("invalid order settlement date")
        settlement = date.fromisoformat(settlement)
    return OrderExecution(
        filled_quantity=raw["filledQuantity"],
        average_filled_price=_parse_nullable_decimal(raw["averageFilledPrice"]),
        filled_amount=_parse_nullable_decimal(raw["filledAmount"]),
        commission=_parse_nullable_decimal(raw["commission"]),
        tax=_parse_nullable_decimal(raw["tax"]),
        filled_at=_parse_datetime(raw["filledAt"], nullable=True),
        settlement_date=settlement,
    )


def _parse_order(value: object) -> Order:
    required = {"orderId", "symbol", "side", "orderType", "timeInForce", "status",
                "quantity", "currency", "orderedAt", "execution"}
    raw = _exact_object(value, required, {"price", "orderAmount", "canceledAt"})
    if not all(_nonempty_string(raw[key]) for key in
               ("orderId", "symbol", "side", "orderType", "timeInForce", "status", "currency")):
        raise ValueError("invalid order")
    if not _decimal(raw["quantity"]):
        raise ValueError("invalid order")
    price = Opt.from_json(raw, "price")
    amount = Opt.from_json(raw, "orderAmount")
    canceled = Opt.from_json(raw, "canceledAt")
    if price is not MISSING:
        price = _parse_nullable_decimal(price)
    if amount is not MISSING:
        amount = _parse_nullable_decimal(amount)
    if canceled is not MISSING:
        canceled = _parse_datetime(canceled, nullable=True)
    return Order(
        order_id=raw["orderId"], symbol=raw["symbol"], side=raw["side"],
        order_type=raw["orderType"], time_in_force=raw["timeInForce"],
        status=raw["status"], quantity=raw["quantity"], currency=raw["currency"],
        ordered_at=_parse_datetime(raw["orderedAt"]), execution=_parse_execution(raw["execution"]),
        price=price, order_amount=amount, canceled_at=canceled,
    )


def decode_orders_page_checkpoint_v1(
    orders_json: bytes, *, schema_version: int, tick_id: int, request_ordinal: int,
    account_scope: str, window_from: str, window_to: str, request_cursor: str | None,
    has_next: bool, next_cursor: str | None,
) -> OrdersPageCheckpointV1:
    if type(orders_json) is not bytes or not 2 <= len(orders_json) <= MAX_ORDERS_JSON_BYTES:
        raise ValueError("invalid orders page bytes")
    try:
        text = orders_json.decode("utf-8")
        raw = json.loads(text)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ValueError("invalid orders page JSON") from None
    if type(raw) is not list:
        raise ValueError("invalid orders page payload")
    canonical = json.dumps(raw, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    if canonical != orders_json:
        raise ValueError("non-canonical orders page JSON")
    page = OrdersPageCheckpointV1(
        schema_version=schema_version, tick_id=tick_id, request_ordinal=request_ordinal,
        account_scope=account_scope, window_from=window_from, window_to=window_to,
        request_cursor=request_cursor, orders=tuple(_parse_order(item) for item in raw),
        has_next=has_next, next_cursor=next_cursor,
    )
    _page_contract(page)
    return page


PROVIDER_HOST = "openapi.tossinvest.com"


class RequestOperation(str, Enum):
    TOKEN = "TOKEN"
    ACCOUNTS = "ACCOUNTS"
    ACCOUNT_HOLDINGS = "ACCOUNT_HOLDINGS"
    PRICE_BATCH = "PRICE_BATCH"
    ORDERS_PAGE = "ORDERS_PAGE"
    ORDER_DETAIL = "ORDER_DETAIL"
    CALENDAR_DAY_KR = "CALENDAR_DAY_KR"
    CALENDAR_DAY_US = "CALENDAR_DAY_US"
    STOCK_CANDLES_DAILY = "STOCK_CANDLES_DAILY"


REQUEST_OPERATION_SPECS = MappingProxyType({
    "TOKEN": ("POST", "/oauth2/token", False),
    "ACCOUNTS": ("GET", "/api/v1/accounts", False),
    "ACCOUNT_HOLDINGS": ("GET", "/api/v1/holdings", True),
    "PRICE_BATCH": ("GET", "/api/v1/prices", False),
    "ORDERS_PAGE": ("GET", "/api/v1/orders", False),
    "ORDER_DETAIL": ("GET", "/api/v1/orders/{orderId}", False),
    "CALENDAR_DAY_KR": ("GET", "/api/v1/market-calendar/KR", False),
    "CALENDAR_DAY_US": ("GET", "/api/v1/market-calendar/US", False),
    "STOCK_CANDLES_DAILY": ("GET", "/api/v1/candles", False),
})
REQUEST_JOB_SCOPES = frozenset({"CALENDAR", "HOLDINGS", "QUOTES", "TRADES", "DAILY_CANDLES"})
REQUEST_PATHS = tuple(spec[1] for spec in REQUEST_OPERATION_SPECS.values())


@dataclass(frozen=True)
class PlannedRequestInputV1:
    ordinal: int
    operation: RequestOperation
    params_digest: str
    method: str
    host: str
    normalized_path_template: str
    account_ordinal: int | None
    market: str | None
    local_trade_date: str | None
    canonical_params: Mapping[str, object]


def canonical_params_digest(operation: str, params: Mapping[str, object]) -> str:
    fields = {"operation": operation, **params}
    parts = []
    for key in sorted(fields):
        key_bytes = key.encode()
        value_bytes = json.dumps(
            fields[key], sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
        parts.extend((str(len(key_bytes)).encode(), b":", key_bytes,
                      str(len(value_bytes)).encode(), b":", value_bytes))
    raw = b"".join(parts)
    return hashlib.sha256(raw).hexdigest()


def validate_canonical_params(operation: str, params: object) -> dict[str, object]:
    if not isinstance(params, Mapping):
        raise ValueError("invalid canonical params")
    value = dict(params)
    keys = dict((
        ("TOKEN", set()), ("ACCOUNTS", set()),
        ("ACCOUNT_HOLDINGS", {"account_ordinal"}),
        ("PRICE_BATCH", {"market", "sorted_unique_symbols"}),
        ("ORDERS_PAGE", {"account_scope", "window_from", "window_to", "request_cursor"}),
        ("ORDER_DETAIL", {"account_scope", "order_id"}),
        ("CALENDAR_DAY_KR", {"market", "local_trade_date"}),
        ("CALENDAR_DAY_US", {"market", "local_trade_date"}),
    ))
    if operation not in keys or set(value) != keys[operation]:
        raise ValueError("invalid canonical params")
    if operation == "ACCOUNT_HOLDINGS":
        ordinal = value["account_ordinal"]
        if type(ordinal) is not int or not 1 <= ordinal <= 5:
            raise ValueError("invalid canonical params")
    elif operation == "PRICE_BATCH":
        symbols = value["sorted_unique_symbols"]
        if value["market"] not in ("KR", "US") or not isinstance(symbols, (list, tuple)):
            raise ValueError("invalid canonical params")
        if not symbols or any(type(item) is not str or not item for item in symbols):
            raise ValueError("invalid canonical params")
        if list(symbols) != sorted(set(symbols)):
            raise ValueError("invalid canonical params")
        value["sorted_unique_symbols"] = list(symbols)
    elif operation in ("ORDERS_PAGE", "ORDER_DETAIL"):
        required_strings = ("account_scope", "order_id") if operation == "ORDER_DETAIL" else (
            "account_scope", "window_from", "window_to",
        )
        if any(type(value[name]) is not str or not value[name] for name in required_strings):
            raise ValueError("invalid canonical params")
        if operation == "ORDERS_PAGE" and value["request_cursor"] is not None and (
            type(value["request_cursor"]) is not str or not value["request_cursor"]
        ):
            raise ValueError("invalid canonical params")
    elif operation.startswith("CALENDAR_DAY_"):
        market = operation.rsplit("_", 1)[1]
        raw_date = value["local_trade_date"]
        try:
            canonical_date = date.fromisoformat(raw_date).isoformat()
        except (TypeError, ValueError):
            raise ValueError("invalid canonical params") from None
        if value["market"] != market or raw_date != canonical_date:
            raise ValueError("invalid canonical params")
    return value
