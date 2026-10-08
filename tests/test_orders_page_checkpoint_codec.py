import ast
from dataclasses import FrozenInstanceError, fields, is_dataclass
from datetime import date, datetime, timezone
import hashlib
import json
from pathlib import Path
from typing import get_type_hints

import pytest

import tokmo.values.request_parity as request_parity
from tokmo.values._primitives import MISSING
from tokmo.values.toss_orders import Order, OrderExecution

ACCOUNT_SCOPE = "toss-acct-" + "a" * 24
ROOT = Path(__file__).parents[1]


def execution(**changes):
    values = dict(
        filled_quantity="1.00", average_filled_price="70000.0",
        filled_amount="70000.00", commission="10.0", tax="2.0",
        filled_at=datetime(2026, 7, 30, 6, tzinfo=timezone.utc),
        settlement_date=date(2026, 8, 3),
    )
    values.update(changes)
    return OrderExecution(**values)


def order(order_id="주문-2", **changes):
    values = dict(
        order_id=order_id, symbol="005930", side="BUY", order_type="LIMIT",
        time_in_force="DAY", status="FILLED", quantity="1", currency="KRW",
        ordered_at=datetime(2026, 7, 30, 5, tzinfo=timezone.utc),
        execution=execution(), price=MISSING, order_amount=MISSING,
        canceled_at=MISSING,
    )
    values.update(changes)
    return Order(**values)


def page(**changes):
    values = dict(
        schema_version=1, tick_id=42, request_ordinal=3,
        account_scope=ACCOUNT_SCOPE, window_from="2026-07-01",
        window_to="2026-07-31", request_cursor=None,
        orders=(order(), order("order-1", price=None, order_amount="70000.00",
                               canceled_at=None)),
        has_next=True, next_cursor="cursor-2",
    )
    values.update(changes)
    return request_parity.OrdersPageCheckpointV1(**values)


def decode(raw, source=None):
    source = source or page()
    return request_parity.decode_orders_page_checkpoint_v1(
        raw, schema_version=source.schema_version, tick_id=source.tick_id,
        request_ordinal=source.request_ordinal, account_scope=source.account_scope,
        window_from=source.window_from, window_to=source.window_to,
        request_cursor=source.request_cursor, has_next=source.has_next,
        next_cursor=source.next_cursor,
    )


def test_orders_page_checkpoint_v1_has_exact_frozen_typed_shape():
    cls = getattr(request_parity, "OrdersPageCheckpointV1", None)
    assert cls is not None, "behavioral RED: typed checkpoint value is absent"
    assert is_dataclass(cls)
    assert cls.__dataclass_params__.frozen is True
    assert tuple(field.name for field in fields(cls)) == (
        "schema_version", "tick_id", "request_ordinal", "account_scope",
        "window_from", "window_to", "request_cursor", "orders", "has_next",
        "next_cursor",
    )
    hints = get_type_hints(cls)
    assert hints["orders"] == tuple[Order, ...]
    candidate = page(orders=())
    assert candidate.schema_version == 1
    with pytest.raises(FrozenInstanceError):
        candidate.tick_id = 4


def test_canonical_utf8_json_digest_and_decode_reencode_are_stable():
    candidate = page()
    encoded = request_parity.encode_orders_page_checkpoint_v1(candidate)
    assert isinstance(encoded, bytes)
    assert "주문-2".encode() in encoded
    assert encoded == json.dumps(json.loads(encoded), sort_keys=True,
                                 separators=(",", ":"), ensure_ascii=False).encode()
    assert request_parity.orders_page_digest_v1(encoded) == hashlib.sha256(encoded).hexdigest()
    restored = decode(encoded, candidate)
    assert request_parity.encode_orders_page_checkpoint_v1(restored) == encoded
    assert request_parity.orders_page_digest_v1(
        request_parity.encode_orders_page_checkpoint_v1(restored)) == hashlib.sha256(encoded).hexdigest()


@pytest.mark.parametrize("field", ["price", "order_amount", "canceled_at"])
@pytest.mark.parametrize("state", [MISSING, None, "value"])
def test_order_optional_fields_preserve_missing_null_and_value(field, state):
    value = state
    if state == "value":
        value = (datetime(2026, 7, 30, tzinfo=timezone.utc)
                 if field == "canceled_at" else "1.25")
    candidate = page(orders=(order(**{field: value}),))
    encoded = request_parity.encode_orders_page_checkpoint_v1(candidate)
    key = {"price": "price", "order_amount": "orderAmount",
           "canceled_at": "canceledAt"}[field]
    raw_order = json.loads(encoded)[0]
    assert (key in raw_order) is (state is not MISSING)
    if state is None:
        assert raw_order[key] is None
    restored = decode(encoded, candidate).orders[0]
    actual = getattr(restored, field)
    assert actual is MISSING if state is MISSING else actual == value


def test_order_tuple_order_and_empty_page_roundtrip():
    candidate = page(orders=(order("z"), order("a")))
    assert tuple(item.order_id for item in decode(
        request_parity.encode_orders_page_checkpoint_v1(candidate), candidate).orders) == ("z", "a")
    empty = page(orders=(), has_next=False, next_cursor=None)
    assert request_parity.encode_orders_page_checkpoint_v1(empty) == b"[]"
    assert decode(b"[]", empty) == empty


@pytest.mark.parametrize("changes", [
    {"schema_version": 2}, {"schema_version": True}, {"tick_id": -1},
    {"tick_id": 2147483648}, {"tick_id": True}, {"request_ordinal": -1},
    {"request_ordinal": 10}, {"request_ordinal": False},
    {"account_scope": "bad"}, {"window_from": "2026-7-1"},
    {"window_to": "2026-02-30"}, {"request_cursor": ""},
    {"request_cursor": 1}, {"has_next": 1},
    {"has_next": False, "next_cursor": "x"},
    {"has_next": True, "next_cursor": None},
    {"request_cursor": "same", "has_next": True, "next_cursor": "same"},
    {"next_cursor": "x" * 4097}, {"orders": []},
])
def test_page_contract_rejects_schema_scope_window_cursor_and_bounds(changes):
    with pytest.raises(ValueError):
        request_parity.encode_orders_page_checkpoint_v1(page(**changes))


@pytest.mark.parametrize("mutation", [
    lambda rows: rows[0].pop("orderId"),
    lambda rows: rows[0].__setitem__("unknown", 1),
    lambda rows: rows[0].__setitem__("quantity", 1),
    lambda rows: rows[0].__setitem__("quantity", True),
    lambda rows: rows[0].__setitem__("execution", []),
    lambda rows: rows[0]["execution"].pop("tax"),
    lambda rows: rows[0]["execution"].__setitem__("filledQuantity", False),
    lambda rows: rows[0]["execution"].__setitem__("unknown", None),
])
def test_decode_rejects_required_unknown_wrong_scalar_container_and_bool_as_int(mutation):
    source = page(orders=(order(),))
    rows = json.loads(request_parity.encode_orders_page_checkpoint_v1(source))
    unmutated = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    assert decode(unmutated, source) == source

    mutation(rows)
    raw = json.dumps(
        rows, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    with pytest.raises(ValueError):
        decode(raw, source)


@pytest.mark.parametrize("raw", [b"\xff", b"{", b"{}", b"[1]", b"[{}]", b"[ ]"])
def test_decode_rejects_malformed_utf8_json_wrong_top_level_and_noncanonical(raw):
    with pytest.raises(ValueError):
        decode(raw)


def test_codec_enforces_two_mib_bound_before_json_decode():
    with pytest.raises(ValueError):
        decode(b" " * (2 * 1024 * 1024 + 1))


def test_kernel_primitive_boundary_has_no_typed_codec_imports():
    path = ROOT / "src/tokmo/records/ops_kernel.py"
    source = path.read_text()
    tree = ast.parse(source)
    imports = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    } | {
        alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert "tokmo.values.toss_orders" not in imports
    assert "tokmo.values._primitives" not in imports
    assert "Order" not in source and "MISSING" not in source
    method = next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
                  and node.name == "checkpoint_orders_page")
    names = {node.id for node in ast.walk(method) if isinstance(node, ast.Name)}
    assert not names & {"Order", "MISSING", "OrdersPageCheckpointV1",
                        "encode_orders_page_checkpoint_v1", "decode_orders_page_checkpoint_v1"}
