"""Controller acceptance: non-toy discovery volume and active-position latency.
All transports are synthetic; no provider/model/operational writes.
"""

import json
from datetime import timedelta

from test_opportunity_rotation import at, paper, proposal, session
from test_opportunity_rotation_source import candle, wire

from tokmo.programs.opportunity_rotation_capture import capture_consumer
from tokmo.values.opportunity_rotation import instant


def capture_clocked(tmp_path, start, count, *, paginated=False):
    clock = [instant(at(start))]
    root = tmp_path / "data"
    p = paper(root / "opportunity-rotation/paper")
    codes = [f"{111111 + i:06d}" for i in range(count)]
    wires = []

    def transport(req):
        op = req.operation.value
        wires.append((op, clock[0].isoformat()))
        if op == "KR_MARKET_CALENDAR":

            def day(d):
                return dict(
                    date=d,
                    integrated=dict(
                        regularMarket=dict(
                            startTime=d + "T09:00:00+09:00",
                            endTime=d + "T15:30:00+09:00",
                        )
                    ),
                )

            return wire(
                dict(
                    today=day(req.date),
                    previousBusinessDay=day("2026-09-23"),
                    nextBusinessDay=day("2026-09-30"),
                )
            )
        if op == "RANKINGS":
            selected = (
                codes[:100]
                if req.ranking_type == "MARKET_TRADING_AMOUNT"
                else codes[-100:]
            )
            return wire(
                dict(
                    rankedAt=clock[0].isoformat(),
                    rankings=[
                        dict(
                            rank=i + 1,
                            symbol=s,
                            currency="KRW",
                            price=dict(lastPrice="10000", basePrice="9000"),
                            tradingVolume="100",
                            tradingAmount="1000000",
                        )
                        for i, s in enumerate(selected)
                    ],
                )
            )
        if op == "STOCKS":
            return wire(
                [
                    dict(
                        symbol=s,
                        name="합성",
                        englishName="Synthetic",
                        isinCode="KR" + s,
                        market="KOSPI",
                        securityType="STOCK",
                        isCommonShare=True,
                        status="ACTIVE",
                        currency="KRW",
                        sharesOutstanding="10000",
                        koreanMarketDetail=dict(
                            liquidationTrading=False,
                            nxtSupported=False,
                            krxTradingSuspended=False,
                        ),
                    )
                    for s in req.symbols
                ]
            )
        if req.interval == "1d":
            c = candle("09:00:00")
            c["timestamp"] = "2026-09-23T00:00:00+09:00"
            return wire(dict(candles=[c], nextBefore=None))
        if paginated:
            day = req.before.date().isoformat()
            end = min(req.before, clock[0], instant(at("15:31:00", day))).replace(
                second=0, microsecond=0
            )
            dates = [
                end - timedelta(minutes=i)
                for i in range(200)
                if end - timedelta(minutes=i) > instant(at("09:00:00", day))
            ]
            rows = [
                dict(candle("09:01:00", "50000"), timestamp=d.isoformat())
                for d in dates
            ]
            cursor = (
                dates[-1].isoformat()
                if dates and dates[-1] > instant(at("09:01:00", day))
                else None
            )
            return wire(dict(candles=rows, nextBefore=cursor))
        c = candle("09:01:00", "50000")
        c["timestamp"] = req.before.isoformat()
        return wire(dict(candles=[c], nextBefore=None))

    def prices(req):
        wires.append(("CURRENT_PRICES", clock[0].isoformat()))
        return wire(
            [
                dict(
                    symbol=s,
                    lastPrice="10000",
                    currency="KRW",
                    timestamp=clock[0].isoformat(),
                )
                for s in req.symbols
            ]
        )

    consumer = capture_consumer(
        data_root=root,
        transport=transport,
        price_transport=prices,
        clock=lambda: clock[0],
        sleeper=lambda seconds: None,
    )

    def tick():
        result = consumer(
            existing_token="synthetic-controller-token",
            now=clock[0],
            deadline=clock[0] + timedelta(seconds=35),
            wire_recorder=lambda *args: None,
            remaining_budget=3,
        )
        assert result["status"] != "FAILED", json.loads(
            (root / "opportunity-rotation/captured.json").read_text()
        ).get("blocked")
        clock[0] += timedelta(minutes=1)
        return json.loads((root / "opportunity-rotation/captured.json").read_text())

    tick.transports = (transport, prices)
    return p, clock, tick, codes, wires


def test_observed_scale_union_ready_for_morning_after_two_hour_warmup(tmp_path):
    # 159 is a previously observed ranking-union size, not a product cutoff.
    p, clock, tick, codes, wires = capture_clocked(tmp_path, "08:00:00", 159)
    snapshot = {}
    while clock[0] <= instant(at("10:05:00")):
        snapshot = tick()
    completed = snapshot.get("discovery", {})
    print(
        json.dumps(
            dict(
                candidates=len(completed.get("candidates", [])),
                expected=len(codes),
                wires=len(wires),
                at=clock[0].isoformat(),
            )
        )
    )
    assert len(completed.get("candidates", [])) == len(codes), (
        "morning discovery cannot finish with one alternate-tick read per candidate"
    )


def test_five_pending_symbols_have_fresh_reconciliation_for_afternoon(tmp_path):
    p, clock, tick, codes, wires = capture_clocked(tmp_path, "13:10:00", 5)
    for i, code in enumerate(codes):
        p.propose(
            [proposal(id=f"controller-{i}", symbol=code, time="13:09:00", quantity=1)],
            session(),
        )
    snapshot = {}
    while clock[0] <= instant(at("13:30:00")):
        snapshot = tick()
    market = snapshot.get("market", {})
    age = (instant(at("13:30:00")) - instant(market["valuation_at"])).total_seconds()
    print(
        json.dumps(
            dict(
                valuation_at=market["valuation_at"],
                decision_at=at("13:30:00"),
                age_seconds=age,
                wire_starts=len(wires),
            )
        )
    )
    assert age <= 300, (
        "market capture end pinned across alternate ticks exceeds slot freshness"
    )


def test_paginated_selected_market_reaches_common_fresh_cutoff(tmp_path):
    p, clock, tick, codes, wires = capture_clocked(
        tmp_path, "13:10:00", 5, paginated=True
    )
    for i, code in enumerate(codes):
        p.propose(
            [proposal(id=f"paged-{i}", symbol=code, time="13:09:00", quantity=1)],
            session(),
        )
    snapshot = {}
    while clock[0] <= instant(at("13:30:00")):
        snapshot = tick()
    market = snapshot["market"]
    assert (
        instant(at("13:30:00")) - instant(market["valuation_at"])
    ).total_seconds() <= 300
    assert set(market["marks"]) == set(codes) | {"069500"}
    assert all(c["complete"] for c in market["coverage"])
    assert all(m["at"] == market["valuation_at"] for m in market["marks"].values())
    p.advance(market)
    assert all(
        instant(order["reconciled_until"]) >= instant(market["valuation_at"])
        for order in p.state()["proposals"].values()
    )
    print(
        json.dumps(
            dict(
                paginated=True,
                valuation_at=market["valuation_at"],
                bars=len(market["bars"]),
                wires=len(wires),
            )
        )
    )
