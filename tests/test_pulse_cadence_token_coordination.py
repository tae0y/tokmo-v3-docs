"""Minimal 5-minute boundary token-collision coordination tests."""

from datetime import datetime, timezone
from pathlib import Path

from tokmo.programs.integrated_pulse import PulseDepsV1, run_pulse_once
from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
from tokmo.records.calendar_lookahead import LookaheadDayV1, LookaheadSessionV1
from tokmo.records.latest_quotes import LatestQuoteStore
from tokmo.values.market_observations import TossCurrentPriceFact
from tokmo.values.operation_lane_registry import (
    LANE_GOVERNED_OPERATIONS, OperationLane, OperationLaneRegistryV1,
    OperationLaneV1,
)
from tokmo.values.session_gate import Market
from tokmo.values.material_pack import JobKind, build_material_pack_v1

T5 = datetime(2026, 8, 11, 1, 5, tzinfo=timezone.utc)
T1 = datetime(2026, 8, 11, 1, 6, tzinfo=timezone.utc)


class _Dialogue:
    def __init__(self, root): self.path = root / "stores/execution_dialogue.sqlite3"
    def list_outbox(self, *, status=None): return ()


def _registry():
    entries = {}
    for op in LANE_GOVERNED_OPERATIONS:
        if op in {"PRICE_BATCH", "ORDERS_PAGE", "ORDER_DETAIL"}:
            entries[op] = OperationLaneV1(
                op, OperationLane.PULSE_60S, OperationLane.CADENCE_300S,
                int(T5.timestamp()))
        else:
            entries[op] = OperationLaneV1(op, OperationLane.CADENCE_300S, None, None)
    return OperationLaneRegistryV1(entries)


def _deps(root, slot, gate, calls):
    def token():
        calls.append("token")
        return "t"
    def quotes(market, symbols):
        calls.append("quotes")
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact("t", s, "1", "KRW", slot, slot)
            for s in symbols), ())
    return PulseDepsV1(
        clock=lambda: slot,
        lookahead_reader=lambda market: (
            (LookaheadDayV1(
                Market.KR, "2026-08-11", "2026-08-11",
                (LookaheadSessionV1(
                    "regularMarket",
                    datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc),
                    datetime(2026, 8, 11, 6, 30, tzinfo=timezone.utc)),),
                slot, "calendar:KR:2026-08-11:0"),)
            if market is Market.KR else ()),
        holdings_reader=lambda: None,
        native_refs_reader=lambda: None,
        watchlist_symbols={Market.KR: frozenset({"005930"})},
        token_fetch=token, quotes_fetch=quotes,
        orders_page_fetch=lambda **kw: None,
        order_detail_fetch=lambda **kw: None,
        quote_store=LatestQuoteStore(root / "stores" / "latest_quotes.sqlite3",
                                    clock=lambda: slot),
        ledger_store=None, scan_state_store=None,
        dialogue_store=_Dialogue(root), dialogue_quote_reader=lambda symbol: None,
        playbook_validator=lambda root: [], dialogue_cutoff=slot,
        quotes_provider_enabled=True, execution_provider_enabled=False,
        lane_registry=_registry(), cadence_boundary_gate=gate,
    )


def test_non_boundary_does_not_call_cadence_gate(tmp_path):
    calls=[]
    result=run_pulse_once(root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=T1, observed_now_utc=T1,
        deps=_deps(tmp_path, T1, lambda slot: calls.append("gate") or False, calls))
    assert "gate" not in calls
    assert "token" in calls


def test_boundary_complete_then_token(tmp_path):
    calls=[]
    result=run_pulse_once(root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=T5, observed_now_utc=T5,
        deps=_deps(tmp_path, T5, lambda slot: calls.append("gate") or True, calls))
    assert calls.index("gate") < calls.index("token")
    assert result.reason is None


def test_boundary_missing_or_started_fails_with_wire_zero(tmp_path):
    calls=[]
    result=run_pulse_once(root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=T5, observed_now_utc=T5,
        deps=_deps(tmp_path, T5, lambda slot: False, calls))
    assert result.reason == "cadence_not_complete"
    assert result.wire_counts == {}
    assert "token" not in calls


def test_boundary_failed_fails_with_wire_zero(tmp_path):
    calls=[]
    result=run_pulse_once(root=tmp_path, policy_version="pulse-60s-v1",
        scheduled_for_utc=T5, observed_now_utc=T5,
        deps=_deps(tmp_path, T5, lambda slot: False, calls))
    assert result.outcome == "FAILED"
    assert "token" not in calls


def test_cadence_consumer_execution_pack_is_schema_valid():
    pack = build_material_pack_v1(
        schema_version=1, job=JobKind.TRADES, tick_id=1,
        market_partition=("KR",),
        payload={"executions": [{
            "source": "toss-orders-v1",
            "account_scope": "toss-account-redacted",
            "source_record_key": "record-redacted",
            "stored_at": T5.isoformat(),
        }]},
        captured_at=T5,
        source_timestamps={"provider": None, "fetched": None,
                           "stored": T5.isoformat()},
        parser_version="toss-orders-v1", mask_digest="a" * 64)
    assert pack.payload["executions"][0]["source"] == "toss-orders-v1"
