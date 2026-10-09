"""Toss order command contract pinned to OpenAPI 1.2.15 (2026-09-11).

Closed routes, strict payload validation, a single injected transport call.
No credentials acquisition, persistence, retries or investment decisions.
"""

import json
import re
from datetime import date
from decimal import Decimal
from types import MappingProxyType

from tokmo.facts.toss import valid_token
from tokmo.facts.toss_reads import valid_account_seq_int64
from tokmo.values.order_commands import (
    CancelOrder,
    Condition,
    ConditionalOrder,
    OrderCommand,
    OrderCommandResult,
    OrderOperation,
    OrderTransportDisabled,
    OrderWireRequest,
    OrderWireResponse,
    RegularOrder,
    ReplaceOrder,
)

ORDER_COMMAND_ROUTES = MappingProxyType(
    {
        OrderOperation.CREATE: ("POST", "/api/v1/orders"),
        OrderOperation.MODIFY: ("POST", "/api/v1/orders/{id}/modify"),
        OrderOperation.CANCEL: ("POST", "/api/v1/orders/{id}/cancel"),
        OrderOperation.CONDITIONAL_CREATE: ("POST", "/api/v1/conditional-orders"),
        OrderOperation.CONDITIONAL_MODIFY: (
            "POST",
            "/api/v1/conditional-orders/{id}/modify",
        ),
        OrderOperation.CONDITIONAL_CANCEL: (
            "DELETE",
            "/api/v1/conditional-orders/{id}",
        ),
    }
)
CREATE_OPERATIONS = frozenset(
    {OrderOperation.CREATE, OrderOperation.CONDITIONAL_CREATE}
)
_NUMBER = re.compile(r"[0-9]+(?:\.[0-9]+)?\Z", re.ASCII)
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{1,512}\Z", re.ASCII)
_KEY = re.compile(r"[A-Za-z0-9_-]{1,36}\Z", re.ASCII)


def _positive(value, name, *, integer=False):
    if not isinstance(value, str) or len(value) > 30 or not _NUMBER.fullmatch(value):
        raise ValueError(name)
    number = Decimal(value)
    if number <= 0 or (
        integer and ("." in value or number != number.to_integral_value())
    ):
        raise ValueError(name)
    return number


def _price(value, market, name):
    number = _positive(value, name, integer=market == "KR")
    if market == "US":
        precision = 4 if number < 1 else 2
        if number != number.quantize(Decimal(1).scaleb(-precision)):
            raise ValueError(name)
    return value


def _order_price(order_type, price, market, key):
    if order_type not in {"LIMIT", "MARKET"}:
        raise ValueError("order_type")
    if order_type == "MARKET":
        if price is not None:
            raise ValueError("market_price_forbidden")
        return {}
    return {key: _price(price, market, key)}


def _key(value):
    if not isinstance(value, str) or not _KEY.fullmatch(value):
        raise ValueError("client_order_id_required")
    return value


def _bool(value):
    if type(value) is not bool:
        raise ValueError("confirm_high_value")
    return value


def _regular(order, market, symbol):
    if type(order) is not RegularOrder or order.side not in {"BUY", "SELL"}:
        raise ValueError("regular_order")
    if order.time_in_force not in {"DAY", "CLS", "OPG"}:
        raise ValueError("time_in_force")
    if order.time_in_force == "CLS" and (market != "US" or order.order_type != "LIMIT"):
        raise ValueError("close_order_market")
    if order.time_in_force == "OPG" and market != "KR":
        raise ValueError("opening_order_market")
    data = {
        "symbol": symbol,
        "side": order.side,
        "orderType": order.order_type,
        "clientOrderId": _key(order.client_order_id),
        "confirmHighValueOrder": _bool(order.confirm_high_value),
    }
    data.update(_order_price(order.order_type, order.price, market, "price"))
    if (order.quantity is None) == (order.order_amount is None):
        raise ValueError("quantity_xor_amount")
    if order.order_amount is not None:
        if (
            market != "US"
            or order.order_type != "MARKET"
            or order.time_in_force != "DAY"
        ):
            raise ValueError("amount_order_type")
        _positive(order.order_amount, "order_amount")
        data["orderAmount"] = order.order_amount
    else:
        fractional = (
            market == "US" and order.order_type == "MARKET" and order.side == "SELL"
        )
        number = _positive(order.quantity, "quantity", integer=not fractional)
        if fractional and number.as_tuple().exponent < -6:
            raise ValueError("fractional_quantity_precision")
        data["quantity"] = order.quantity
        data["timeInForce"] = order.time_in_force
    return data


def _replace(order, market):
    if type(order) is not ReplaceOrder:
        raise ValueError("replace_order")
    data = {
        "orderType": order.order_type,
        "confirmHighValueOrder": _bool(order.confirm_high_value),
    }
    data.update(_order_price(order.order_type, order.price, market, "price"))
    if market == "KR":
        _positive(order.quantity, "quantity", integer=True)
        data["quantity"] = order.quantity
    elif order.quantity is not None:
        raise ValueError("us_modify_quantity_not_supported")
    return data


def _condition(leg, market, order_type):
    if type(leg) is not Condition or leg.side not in {"BUY", "SELL"}:
        raise ValueError("condition")
    return {
        "orderSide": leg.side,
        "triggerPrice": _price(leg.trigger_price, market, "trigger_price"),
        **_order_price(order_type, leg.order_price, market, "orderPrice"),
    }


def _conditional(order, market, symbol, *, creating):
    if type(order) is not ConditionalOrder or order.kind not in {
        "SINGLE",
        "OTO",
        "OCO",
    }:
        raise ValueError("conditional_order")
    # Integer quantities only until fractional conditional execution is documented.
    _positive(order.quantity, "quantity", integer=True)
    if type(order.expire_date) is not date:
        raise ValueError("expire_date")
    first = _condition(order.first, market, order.order_type)
    data = {
        "type": order.kind,
        "quantity": order.quantity,
        "orderType": order.order_type,
        "expireDate": order.expire_date.isoformat(),
        "first": first,
        "confirmHighValueOrder": _bool(order.confirm_high_value),
    }
    if order.kind == "SINGLE":
        if order.second is not None:
            raise ValueError("single_second_forbidden")
    else:
        if order.order_type != "LIMIT":
            raise ValueError("group_requires_limit")
        second = _condition(order.second, market, order.order_type)
        sides = (first["orderSide"], second["orderSide"])
        if sides != (("BUY", "SELL") if order.kind == "OTO" else ("SELL", "SELL")):
            raise ValueError("group_sides")
        if order.kind == "OCO" and Decimal(first["triggerPrice"]) <= Decimal(
            second["triggerPrice"]
        ):
            raise ValueError("oco_trigger_order")
        data["second"] = second
    if creating:
        data.update(symbol=symbol, clientOrderId=_key(order.client_order_id))
    elif order.client_order_id is not None:
        raise ValueError("modify_client_key_not_supported")
    return data


def encode_order_command(command):
    """Return fixed method/path and canonical UTF-8 JSON, without credentials."""
    if (
        type(command) is not OrderCommand
        or type(command.operation) is not OrderOperation
    ):
        raise ValueError("order_command")
    if command.market not in {"KR", "US"}:
        raise ValueError("market")
    pattern = (
        r"[A-Za-z0-9]{6}" if command.market == "KR" else r"[A-Za-z][A-Za-z0-9.\-]{0,19}"
    )
    if not isinstance(command.symbol, str) or not re.fullmatch(
        pattern, command.symbol, re.ASCII
    ):
        raise ValueError("symbol")
    op = command.operation
    method, path = ORDER_COMMAND_ROUTES[op]
    if op in CREATE_OPERATIONS:
        if command.target_id is not None:
            raise ValueError("create_target_forbidden")
    else:
        if not isinstance(command.target_id, str) or not _IDENTIFIER.fullmatch(
            command.target_id
        ):
            raise ValueError("target_id")
        path = path.replace("{id}", command.target_id)
    if op == OrderOperation.CREATE:
        data = _regular(command.payload, command.market, command.symbol)
    elif op == OrderOperation.MODIFY:
        data = _replace(command.payload, command.market)
    elif op in {OrderOperation.CONDITIONAL_CREATE, OrderOperation.CONDITIONAL_MODIFY}:
        data = _conditional(
            command.payload,
            command.market,
            command.symbol,
            creating=op == OrderOperation.CONDITIONAL_CREATE,
        )
    else:
        if type(command.payload) is not CancelOrder:
            raise ValueError("cancel_payload")
        data = None if op == OrderOperation.CONDITIONAL_CANCEL else {}
    body = (
        None
        if data is None
        else json.dumps(
            data, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    )
    return method, path, body


def _identifier(value):
    return value if isinstance(value, str) and _IDENTIFIER.fullmatch(value) else None


def _unique_object(pairs):
    obj = {}
    for key, value in pairs:
        if key in obj:
            raise ValueError("duplicate_json_key")
        obj[key] = value
    return obj


def parse_order_command_response(command, response):
    previous = command.target_id

    def result(status, code, oid=None, request_id=None):
        return OrderCommandResult(status, code, oid, previous, request_id)

    if (
        type(response) is not OrderWireResponse
        or type(response.status) is not int
        or type(response.body) is not bytes
    ):
        return result("UNKNOWN", "response_contract")
    if len(response.body) > 1024 * 1024:
        return result("UNKNOWN", "response_too_large")
    if (
        response.status == 204
        and command.operation == OrderOperation.CONDITIONAL_CANCEL
        and not response.body
    ):
        return result("ACCEPTED", "acknowledged", previous)
    try:
        data = json.loads(response.body, object_pairs_hook=_unique_object)
    except (ValueError, UnicodeError):
        return result("UNKNOWN", "response_malformed")
    if type(data) is not dict or ("result" in data and "error" in data):
        return result("UNKNOWN", "response_contract")
    if response.status == 200:
        item = data.get("result")
        if type(item) is not dict:
            return result("UNKNOWN", "result_missing")
        key = (
            "conditionalOrderId"
            if command.operation.name.startswith("CONDITIONAL")
            else "orderId"
        )
        oid = _identifier(item.get(key))
        if oid is None:
            return result("UNKNOWN", "order_id_missing")
        if command.operation in CREATE_OPERATIONS:
            echoed = item.get("clientOrderId")
            if echoed is not None and echoed != command.payload.client_order_id:
                return result("UNKNOWN", "client_key_mismatch")
        elif oid == previous:
            return result("UNKNOWN", "replacement_id_unchanged")
        return result("ACCEPTED", "acknowledged", oid)
    error = data.get("error")
    if type(error) is not dict:
        return result("UNKNOWN", "error_contract")
    code = error.get("code")
    if not isinstance(code, str) or not re.fullmatch(r"[a-z0-9_-]{1,100}", code):
        return result("UNKNOWN", "error_code_missing")
    status = (
        "REJECTED"
        if 400 <= response.status < 500
        and code
        not in {
            "request-in-progress",
            "already-processing",
            "idempotency-key-conflict",
        }
        else "UNKNOWN"
    )
    return result(status, code, request_id=_identifier(error.get("requestId")))


def execute_order_command(command, *, token, account_ref, transport):
    """One attempt; UNKNOWN is never retried here or interpreted as failure."""
    try:
        encode_order_command(command)
        if not valid_token(token) or not valid_account_seq_int64(account_ref):
            raise ValueError("credentials")
    except (ValueError, TypeError, ArithmeticError):
        return OrderCommandResult("NOT_SENT", "invalid_request")
    try:
        response = transport(OrderWireRequest(command, token, account_ref))
    except OrderTransportDisabled:
        return OrderCommandResult("NOT_SENT", "transport_disabled")
    except Exception:
        return OrderCommandResult(
            "UNKNOWN", "transport_uncertain", previous_order_id=command.target_id
        )
    return parse_order_command_response(command, response)
