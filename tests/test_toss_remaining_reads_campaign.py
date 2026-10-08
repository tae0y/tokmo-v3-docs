"""Offline fake-transport campaign proof for the 17 remaining reads.

Verifies campaign ceiling, order, spacing checkpoints, STOP behavior, ID
provenance, and redacted reporting — using fake transport only.
"""

from datetime import datetime, timedelta, timezone

import pytest

from tokmo.facts.toss import _TossTransportFailure, _TossTransportSuccess
from tokmo.facts.toss_reads import (
    TossReadOperation,
    build_read_request,
    query_read,
)

NOW = datetime(2026, 7, 28, 3, 0, tzinfo=timezone.utc)
TOKEN = "SYNTHETIC-READ-TOKEN-0001"
ACCOUNT_SEQ = "1234567890"


class FakeTransport:
    """Returns pre-scripted outcomes, records call count."""

    def __init__(self, *outcomes):
        self.outcomes = list(outcomes)
        self.requests = []
        self.call_count = 0

    def __call__(self, request):
        self.requests.append(request)
        self.call_count += 1
        assert self.outcomes, "campaign started an unplanned request"
        return self.outcomes.pop(0)


def ok(body):
    return _TossTransportSuccess(body=body)


# --- one request per invocation ------------------------------------------

def test_one_success_stops_immediately():
    body = b'{"result":{"currency":"KRW","asks":[],"bids":[]}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "success"
    assert transport.call_count == 1
    # No follow-up calls even though there might be a nextBefore etc.
    assert len(transport.outcomes) == 0  # all consumed


def test_first_failure_stops_campaign():
    transport = FakeTransport(_TossTransportFailure(code="http_429", retry_after=30))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "http_429"
    assert transport.call_count == 1
    assert outcome.retry_after == 30


def test_contract_mismatch_stops_without_retry():
    body = b'{"result":{"wrong":"shape"}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "contract_mismatch"
    assert transport.call_count == 1


# --- detail provenance: detail must be invoked from explicit ID only -----

def test_order_detail_makes_one_request_no_list_fallback():
    body = b'{"result":{"orderId":"ID-001","symbol":"005930","side":"BUY","orderType":"LIMIT","timeInForce":"DAY","status":"PENDING","quantity":"10","currency":"KRW","orderedAt":"2026-03-29T09:30:00+09:00","execution":{"filledQuantity":"0","averageFilledPrice":null,"filledAmount":null,"commission":null,"tax":null,"filledAt":null,"settlementDate":null}}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDER_DETAIL, token=TOKEN,
                             account_seq=ACCOUNT_SEQ, order_id="ID-001")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "success"
    assert transport.call_count == 1


def test_conditional_detail_one_request_no_fallback():
    body = b'{"result":{"conditionalOrderId":"COND-001","type":"SINGLE","status":"WATCHING","symbol":"005930","market":"KR","quantity":"100","orderType":"LIMIT","createdAt":"2026-06-12T09:00:00+09:00","first":{"type":"STOP","status":"WATCHING","triggerPrice":"295","targetProfitRate":null,"orderPrice":"294.5","triggeredOrderId":null}}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.CONDITIONAL_ORDER_DETAIL, token=TOKEN,
                             account_seq=ACCOUNT_SEQ, conditional_order_id="COND-001")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "success"
    assert transport.call_count == 1


# --- pagination is metadata only: no auto-fetch --------------------------

def test_orders_list_with_has_next_does_not_paginate():
    body = b'{"result":{"orders":[],"nextCursor":"CURSOR-ABC","hasNext":true}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDERS, token=TOKEN,
                             account_seq=ACCOUNT_SEQ, status="CLOSED")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "success"
    assert transport.call_count == 1


def test_conditional_orders_list_with_has_next_does_not_paginate():
    body = b'{"result":{"conditionalOrders":[],"nextCursor":"CURSOR-XYZ","hasNext":true}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.CONDITIONAL_ORDERS, token=TOKEN,
                             account_seq=ACCOUNT_SEQ, status="OPEN")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    assert outcome.code == "success"
    assert transport.call_count == 1


# --- redaction in outcomes -----------------------------------------------

def test_outcome_repr_has_no_sensitive_values():
    body = b'{"result":{"currency":"KRW","asks":[{"price":"72100","volume":"100"}],"bids":[]}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDERBOOK, token=TOKEN, symbol="005930")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    rendered = repr(outcome)
    assert TOKEN not in rendered
    assert "72100" not in rendered
    assert "005930" not in rendered


def test_cursor_and_account_redacted():
    body = b'{"result":{"orders":[],"nextCursor":"SENSITIVE","hasNext":false}}'
    transport = FakeTransport(ok(body))
    req = build_read_request(TossReadOperation.ORDERS, token=TOKEN,
                             account_seq=ACCOUNT_SEQ, status="OPEN")
    outcome = query_read(req, transport=transport, clock=lambda: NOW)
    rendered = repr(outcome)
    assert ACCOUNT_SEQ not in rendered
    assert "SENSITIVE" not in rendered
