"""Narrow history readiness acceptance, using existing offline capture fixtures."""

import json
from copy import deepcopy
from datetime import date, timedelta

import pytest
from opportunity_rotation_model import decision_input
from test_opportunity_rotation import (
    at,
    bar,
    decision,
    market,
    paper,
    proposal,
    session,
)
from test_opportunity_rotation_autonomy import ready
from test_opportunity_rotation_controller_readiness import capture_clocked
from test_opportunity_rotation_source import source, wire

from tokmo.programs.opportunity_rotation import build_packet
from tokmo.programs.opportunity_rotation_capture import capture_consumer, valid_daily
from tokmo.programs.opportunity_rotation_schedule import run_slot
from tokmo.values.opportunity_rotation import instant


@pytest.mark.parametrize(
    "damage", [None, "unknown", "nonterminal", "malformed", "failed"]
)
def test_terminal_empty_capture_retained_and_reused(tmp_path, damage):
    _, clock, tick, codes, _ = capture_clocked(tmp_path, "10:05:00", 2)
    public, prices = tick.transports
    requests = []

    def transport(req):
        if req.interval == "1d" and req.symbol == codes[1]:
            requests.append(req.symbol)
            if damage == "failed":
                from tokmo.facts.toss import _TossTransportFailure

                return _TossTransportFailure(code="timeout")
            body = dict(candles=[])
            if damage != "unknown":
                body["nextBefore"] = at("00:00:00") if damage == "nonterminal" else None
            if damage == "malformed":
                body["candles"] = [{}]
            return wire(body)
        return public(req)

    consume = capture_consumer(
        data_root=tmp_path / "data",
        transport=transport,
        price_transport=prices,
        clock=lambda: clock[0],
        sleeper=lambda _: None,
    )
    for _ in range(12):
        consume(
            existing_token="synthetic",
            now=clock[0],
            deadline=clock[0] + timedelta(seconds=35),
            wire_recorder=lambda *a: None,
            remaining_budget=6,
        )
    saved = json.loads(
        (tmp_path / "data/opportunity-rotation/captured.json").read_text()
    )
    if damage:
        assert codes[1] not in saved.get("daily", {})
    else:
        assert saved["daily"][codes[1]]["bars"] == []
        assert requests == [codes[1]]
        c = next(c for c in saved["discovery"]["candidates"] if c["symbol"] == codes[1])
        assert len(saved["discovery"]["candidates"]) == 2
        assert c["history_provenance"]["exhausted"] is True
        assert "completed_ohlcv_source_exhausted" in c["missing"]


@pytest.mark.parametrize(
    "change",
    [
        {},
        {"symbol": "222222"},
        {"currency": "USD"},
        {"market": "US"},
        {"adjusted": False},
        {"source": "unknown"},
        {"observed_at": "2026-09-22T00:00:00+09:00"},
        {"observed_at": "2027-01-01T00:00:00+09:00"},
        {"exhausted": False},
        {"exhausted": None},
        {"bars": None},
    ],
)
def test_empty_daily_requires_complete_source_provenance(change):
    row = source(lambda r: wire(dict(candles=[], nextBefore=None))).daily(
        "111111", date(2026, 9, 23)
    )
    row.update(change)
    assert valid_daily(row, "111111", at("10:05:00")) is (not change)


def test_nonempty_cache_keeps_existing_observation_semantics():
    row = source(lambda r: wire(dict(candles=[], nextBefore=None))).daily(
        "111111", date(2026, 9, 23)
    )
    row.update(
        bars=ready()["candidates"][0]["bars"], observed_at="2026-09-22T00:00:00+09:00"
    )
    assert valid_daily(row, "111111", at("10:05:00"))


@pytest.mark.parametrize(
    "kind", ["full", "terminal_empty", "missing_coverage", "corrupt"]
)
def test_newer_cache_selected_through_completed_in_capture(tmp_path, kind):
    _, clock, tick, codes, _ = capture_clocked(tmp_path, "10:05:00", 1)
    public, prices = tick.transports
    path = tmp_path / "data/opportunity-rotation/captured.json"
    b = ready()["candidates"][0]["bars"][0]
    bars = [
        dict(b, date=(date(2026, 9, 24) - timedelta(days=i)).isoformat())
        for i in reversed(range(200))
    ]
    row = dict(
        symbol=codes[0],
        market="KR",
        currency="KRW",
        adjusted=True,
        completed_session="2026-09-24",
        bars=bars,
        exhausted=False,
        source="Toss STOCK_CANDLES 1d adjusted",
        observed_at=at("09:00:00"),
    )
    if kind == "terminal_empty":
        row.update(bars=bars[-1:], exhausted=True)
    if kind == "missing_coverage":
        row.update(bars=bars[-1:], exhausted=False)
    if kind == "corrupt":
        row["bars"][0]["low"] = "-1"
    original = deepcopy(row)
    path.write_text(json.dumps(dict(daily={codes[0]: row})))
    requests = []

    def transport(req):
        if req.interval == "1d":
            requests.append(req.symbol)
        return public(req)

    consume = capture_consumer(
        data_root=tmp_path / "data",
        transport=transport,
        price_transport=prices,
        clock=lambda: clock[0],
        sleeper=lambda _: None,
    )
    for _ in range(8):
        consume(
            existing_token="synthetic",
            now=clock[0],
            deadline=clock[0] + timedelta(seconds=35),
            wire_recorder=lambda *a: None,
            remaining_budget=6,
        )
    saved = json.loads(path.read_text())
    c = saved["discovery"]["candidates"][0]
    assert all(b["date"] <= "2026-09-23" for b in c["bars"])
    if kind in ("full", "terminal_empty"):
        assert requests == []
        assert c["bars"] == original["bars"][:-1]
        assert saved["daily"][codes[0]] == original
        assert c["history_observed_at"] == original["observed_at"]
    else:
        assert requests == [codes[0]]


def absent_discovery(tmp_path):
    _, clock, tick, codes, _ = capture_clocked(tmp_path, "10:05:00", 2)
    public, prices = tick.transports
    s = source(
        lambda r: (
            wire(dict(candles=[], nextBefore=None))
            if r.interval == "1d" and r.symbol == codes[1]
            else public(r)
        )
    )
    s.clock, s.price_transport = lambda: clock[0], prices
    return s.discover(held_symbols=())


@pytest.mark.parametrize(
    "case",
    [
        "watch",
        "buy_ready",
        "buy_empty",
        "held",
        "pending",
        "unfetched",
        "nonterminal",
        "stale_history",
        "currency",
        "source",
        "identity",
        "malformed",
        "quote_stale",
        "discovery_stale",
    ],
)
def test_source_packet_scheduler_and_entry_boundary(tmp_path, case):
    d = absent_discovery(tmp_path / "source")
    c = d["candidates"][1]
    p = paper(tmp_path / "paper")
    proof = c["history_provenance"]
    if case == "unfetched":
        c.pop("history_provenance")
    if case == "nonterminal":
        proof["exhausted"] = None
    if case == "stale_history":
        proof["completed_session"] = "2026-09-22"
    if case == "currency":
        proof["currency"] = "USD"
    if case == "source":
        proof["source"] = "unverified"
    if case == "identity":
        proof["symbol"] = "999999"
    if case == "malformed":
        c["bars"] = None
    if case == "quote_stale":
        c["quote"]["at"] = at("09:00:00")
    if case == "discovery_stale":
        d["as_of"] = at("09:00:00")
    if case in ("held", "pending"):
        p.propose([proposal(symbol=c["symbol"], time="09:00:00")], session())
        p.advance(
            market(
                [bar("09:01:00", symbol=c["symbol"])] if case == "held" else [],
                time="10:05:00",
                symbols=(c["symbol"],),
            )
        )
    before = p.state()
    calls = []

    class Model:
        def decide(self, packet, *, call_id, at):
            calls.append(packet)
            view = decision_input(packet)
            assert len(view["candidates"]) == 2
            absent = view["candidates"][1]
            assert (
                absent["history_provenance"] == proof and absent["trade_ready"] is False
            )
            assert "completed_ohlcv_source_exhausted" in absent["missing"]
            a = [] if case == "watch" else decision(packet)["assessments"]
            if case == "buy_empty":
                a[0]["symbol"] = a[0]["proposal"]["symbol"] = c["symbol"]
            return decision(packet, id=call_id, verified_at=at, assessments=a)

    result = run_slot(p, d, clock=lambda: instant(at("10:05:00")), model=Model())
    expected = (
        "WAITING_DATA"
        if case in ("watch", "buy_ready")
        else "FAILED"
        if case == "buy_empty"
        else "DATA_NOT_READY"
    )
    assert result["status"] == expected
    assert len(calls) == (1 if case in ("watch", "buy_ready", "buy_empty") else 0)
    if expected == "DATA_NOT_READY":
        assert p.state() == before
    if case == "buy_empty":
        assert (
            result["reason"] == "candidate missing entry facts"
            and p.state()["proposals"] == {}
        )
    if case == "buy_ready":
        p.advance(market(time="10:06:00"))
        assert p.state()["proposals"]["b1"]["symbol"] == "111111"


@pytest.mark.parametrize("damage", ["empty", "missing_bars", "stale_bars", "digest"])
def test_direct_decide_revalidates_entry_lineage_not_derived_flag(tmp_path, damage):
    from tokmo.values.opportunity_rotation import digest

    p = paper(tmp_path)
    packet = build_packet(ready(), p.state())
    c = packet["candidates"][0]
    if damage == "empty":
        c["bars"] = []
    if damage == "missing_bars":
        c.pop("bars")
    if damage == "stale_bars":
        c["bars"][-1]["date"] = "2026-09-22"
    if damage == "digest":
        c["history"]["sha256"] = "wrong"
    c["trade_ready"] = True
    packet["id"] = digest({k: v for k, v in packet.items() if k != "id"})
    with pytest.raises(ValueError, match="candidate missing entry facts"):
        p.decide(packet, decision(packet, verified_at=at("10:05:00")), at("10:05:00"))
    assert p.state()["proposals"] == {}
