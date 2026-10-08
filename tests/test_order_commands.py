"""Order writes are tested with injected responses, never brokerage I/O."""
import json
from dataclasses import replace
from datetime import date, datetime, timezone

import pytest

from tokmo.values.order_commands import (
    CancelOrder, ConditionalOrder, Condition, OrderCommand, OrderOperation,
    RegularOrder, ReplaceOrder, OrderWireResponse,
)
from tokmo.facts.toss_order_commands import encode_order_command, execute_order_command


NOW = datetime(2026, 9, 11, 1, tzinfo=timezone.utc)


def buy(key="test-buy"):
    return OrderCommand(OrderOperation.CREATE, "KR", "005380",
                        RegularOrder("BUY", "LIMIT", quantity="1", price="380000",
                                     client_order_id=key))


def oto(key="test-oto"):
    return OrderCommand(OrderOperation.CONDITIONAL_CREATE, "KR", "005380",
                        ConditionalOrder("OTO", "1", "LIMIT", date(2026, 9, 18),
                                         Condition("BUY", "380000", "380000"),
                                         Condition("SELL", "390000", "390000"), key))


def test_regular_and_conditional_payloads_are_distinct():
    method, path, body = encode_order_command(buy())
    assert (method, path) == ("POST", "/api/v1/orders")
    assert json.loads(body) == {"symbol": "005380", "side": "BUY", "orderType": "LIMIT",
                                "quantity": "1", "price": "380000", "timeInForce": "DAY",
                                "clientOrderId": "test-buy", "confirmHighValueOrder": False}
    _, _, raw = encode_order_command(oto())
    data = json.loads(raw)
    assert data["first"] == {"orderSide": "BUY", "triggerPrice": "380000", "orderPrice": "380000"}
    assert data["quantity"] == "1" and "quantity" not in data["first"]
    assert data["expireDate"] == "2026-09-18"


@pytest.mark.parametrize("operation,payload,target,method,path,body", [
    (OrderOperation.MODIFY, ReplaceOrder("LIMIT", "1", "370000"), "old-1", "POST",
     "/api/v1/orders/old-1/modify", {"orderType": "LIMIT", "quantity": "1", "price": "370000", "confirmHighValueOrder": False}),
    (OrderOperation.CANCEL, CancelOrder(), "old-1", "POST", "/api/v1/orders/old-1/cancel", {}),
    (OrderOperation.CONDITIONAL_CANCEL, CancelOrder(), "old-1", "DELETE", "/api/v1/conditional-orders/old-1", None),
])
def test_mutation_routes(operation, payload, target, method, path, body):
    result = encode_order_command(OrderCommand(operation, "KR", "005380", payload, target))
    assert result[:2] == (method, path)
    assert (None if result[2] is None else json.loads(result[2])) == body


def test_conditional_modify_has_group_quantity_and_no_create_key_or_symbol():
    cmd = replace(oto(), operation=OrderOperation.CONDITIONAL_MODIFY, target_id="old",
                  payload=replace(oto().payload, client_order_id=None))
    data = json.loads(encode_order_command(cmd)[2])
    assert data["quantity"] == "1"
    assert not {"symbol", "clientOrderId"} & data.keys()


@pytest.mark.parametrize("patch", [
    {"quantity": "2.5"}, {"quantity": "0"}, {"quantity": "NaN"},
    {"price": "Infinity"}, {"price": "-1"}, {"price": "1e5"},
    {"order_type": "MARKET"}, {"client_order_id": ""}, {"side": "buy"},
    {"quantity": "1", "order_amount": "20"}, {"time_in_force": "CLS"},
    {"confirm_high_value": "true"},
])
def test_invalid_orders_never_reach_transport(patch):
    cmd = replace(buy(), payload=replace(buy().payload, **patch))
    calls = []
    result = execute_order_command(cmd, token="token", account_ref="123", transport=lambda r: calls.append(r))
    assert result.status == "NOT_SENT" and not calls


def test_us_fractional_and_modification_rules():
    valid = OrderCommand(OrderOperation.CREATE, "US", "LMT",
                         RegularOrder("SELL", "MARKET", quantity="0.5", client_order_id="fraction"))
    assert json.loads(encode_order_command(valid)[2])["quantity"] == "0.5"
    for payload in (replace(valid.payload, side="BUY"), replace(valid.payload, quantity="0.1234567")):
        with pytest.raises(ValueError): encode_order_command(replace(valid, payload=payload))
    modify = OrderCommand(OrderOperation.MODIFY, "US", "LMT", ReplaceOrder("LIMIT", price="524"), "old")
    assert "quantity" not in json.loads(encode_order_command(modify)[2])
    with pytest.raises(ValueError): encode_order_command(replace(modify, payload=ReplaceOrder("LIMIT", "1", "524")))


@pytest.mark.parametrize("payload", [
    ConditionalOrder("OTO", "1", "LIMIT", date(2026, 9, 18), Condition("SELL", "380000", "380000"), Condition("SELL", "390000", "390000"), "key"),
    ConditionalOrder("OCO", "1", "LIMIT", date(2026, 9, 18), Condition("SELL", "370000", "370000"), Condition("SELL", "390000", "390000"), "key"),
    ConditionalOrder("OTO", "1", "MARKET", date(2026, 9, 18), Condition("BUY", "380000"), Condition("SELL", "390000"), "key"),
    ConditionalOrder("SINGLE", "1", "LIMIT", date(2026, 9, 18), Condition("BUY", "380000", "380000"), Condition("SELL", "390000", "390000"), "key"),
])
def test_invalid_conditional_shapes(payload):
    with pytest.raises(ValueError): encode_order_command(replace(oto(), payload=payload))


@pytest.mark.parametrize("target", ["../orders", "x?secret=1", "x\r\nHeader:bad", "", "x/y"])
def test_path_injection_rejected(target):
    with pytest.raises(ValueError): encode_order_command(OrderCommand(OrderOperation.CANCEL, "KR", "005380", CancelOrder(), target))


def respond(cmd, status, obj):
    raw = b"" if obj is None else json.dumps(obj).encode()
    return execute_order_command(cmd, token="token", account_ref="123", transport=lambda r: OrderWireResponse(status, raw))


def test_acceptance_is_not_fill_and_new_ids_are_preserved():
    r = respond(buy(), 200, {"result": {"orderId": "new", "clientOrderId": "test-buy"}})
    assert (r.status, r.order_id, r.previous_order_id) == ("ACCEPTED", "new", None)
    cmd = OrderCommand(OrderOperation.MODIFY, "KR", "005380", ReplaceOrder("LIMIT", "1", "370000"), "old")
    r = respond(cmd, 200, {"result": {"orderId": "replacement"}})
    assert (r.order_id, r.previous_order_id) == ("replacement", "old")
    cmd = OrderCommand(OrderOperation.CONDITIONAL_CANCEL, "KR", "005380", CancelOrder(), "old")
    assert respond(cmd, 204, None).status == "ACCEPTED"


@pytest.mark.parametrize("status,obj,expected", [
    (422, {"error": {"code": "insufficient-buying-power"}}, "REJECTED"),
    (409, {"error": {"code": "request-in-progress"}}, "UNKNOWN"),
    (500, {"error": {"code": "internal-error"}}, "UNKNOWN"),
    (200, {"result": {}}, "UNKNOWN"),
    (200, {"result": {"orderId": "new", "clientOrderId": "wrong"}}, "UNKNOWN"),
    (200, {"result": {"orderId": "new"}, "error": {}}, "UNKNOWN"),
    (302, None, "UNKNOWN"),
])
def test_ambiguous_results_are_not_success_or_retryable(status, obj, expected):
    assert respond(buy(), status, obj).status == expected


def test_timeout_has_one_attempt_and_redacts_credentials():
    calls = []
    def fail(req):
        calls.append(req)
        assert "secret-token" not in repr(req) and "999888" not in repr(req)
        raise TimeoutError("secret-token")
    result = execute_order_command(buy(), token="secret-token", account_ref="999888", transport=fail)
    assert result.status == "UNKNOWN" and len(calls) == 1
    assert "secret-token" not in repr(result)
