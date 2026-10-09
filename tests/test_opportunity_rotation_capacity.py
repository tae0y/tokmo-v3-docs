"""Offline real composition/CLI: normal lanes plus spare public acquisition."""

import json
import os
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import replace
from datetime import UTC, timedelta
from pathlib import Path

import pytest
from test_conditional_detection import _cutover_registry
from test_integrated_pulse import TOSS_SCOPE, _holdings
from test_opportunity_rotation import at, proposal, session
from test_opportunity_rotation_controller_readiness import capture_clocked
from test_opportunity_rotation_source import wire

from tokmo.facts.toss import _TossTransportFailure, _TossTransportSuccess
from tokmo.programs.integrated_pulse import run_pulse_once
from tokmo.records.calendar_lookahead import LookaheadDayV1, LookaheadSessionV1
from tokmo.values.opportunity_rotation import instant
from tokmo.values.session_gate import Market

ROOT = Path(__file__).resolve().parents[1]


def composed(
    tmp_path,
    monkeypatch,
    *,
    start="08:00:00",
    count=159,
    latency=1,
    fail_at=None,
    late_bar=False,
    closing_price=None,
    candle_transform=None,
    quant_inventory=0,
):
    from tokmo import composition

    p, clock, small_tick, codes, _ = capture_clocked(
        tmp_path, start, count, paginated=True
    )
    public, prices = small_tick.transports
    root = tmp_path / "data"
    project = tmp_path / "project"
    (project / "watchlists").mkdir(parents=True)
    (project / ".env.toss").write_text(
        "TOSS_CLIENT_ID=synthetic\nTOSS_CLIENT_SECRET=synthetic\n"
    )
    validator = project / "scripts/playbooks/validate_playbooks.py"
    validator.parent.mkdir(parents=True)
    validator.write_text("def validate_tree(root):\n    return []\n")
    for market in ("KR", "US"):
        (project / "watchlists" / f"{market}.json").write_text(
            json.dumps({"schema_version": 1, "market": market, "entries": []})
        )
    events = []
    token = [None]
    tick_events = []

    def utc():
        return clock[0].astimezone(UTC)

    def sleep(seconds):
        clock[0] += timedelta(seconds=seconds)

    def record(op, req=None):
        with sqlite3.connect(root / "ops/pulse_tick_receipts.sqlite3") as db:
            row = db.execute(
                "SELECT operation,status FROM pulse_wire_attempts ORDER BY tick_id DESC,seq DESC LIMIT 1"
            ).fetchone()
        assert row is not None and row[1] == "STARTED"  # durable BEFORE physical I/O
        expected = {
            "CURRENT_PRICES": "PRICE_BATCH",
            "AUTH": "TOKEN",
            "ORDERS": "ORDERS_PAGE",
        }.get(op, op)
        assert row[0] in (expected, "OPPORTUNITY_READ", "QUANT_READ")
        if req is not None and op != "AUTH":
            assert req.token == token[0]
        event = dict(
            op=op,
            at=utc().isoformat(),
            lane=row[0],
            symbol=getattr(req, "symbol", None),
            interval=getattr(req, "interval", None),
            before=str(getattr(req, "before", None)),
        )
        events.append(event)
        tick_events.append(event)
        sleep(latency)

    class Auth:
        def __init__(self, **kw):
            pass

        def __call__(self, req):
            record("AUTH")
            token[0] = "synthetic-bearer-" + str(len(events))
            return _TossTransportSuccess(
                body=json.dumps(dict(access_token=token[0], expires_in=60)).encode()
            )

    class Reads:
        def __init__(self, **kw):
            pass

        def __call__(self, req):
            record(req.operation.value, req)
            if fail_at and len(events) == fail_at:
                return _TossTransportFailure(code="http_429")
            if req.operation.value == "BUYING_POWER":
                return wire(dict(currency=req.currency, cashBuyingPower="10000"))
            if req.operation.value == "COMMISSIONS":
                return wire([])
            if req.operation.value == "SELLABLE_QUANTITY":
                return wire(dict(sellableQuantity="10"))
            if req.operation.value == "EXCHANGE_RATE":
                return wire(
                    dict(
                        baseCurrency="USD",
                        quoteCurrency="KRW",
                        rate="1400",
                        midRate="1400",
                        basisPoint="0",
                        rateChangeType="UP",
                        validFrom=utc().isoformat(),
                        validUntil=(utc() + timedelta(days=1)).isoformat(),
                    )
                )
            if req.operation.value == "ORDERS":
                return wire(dict(orders=[], hasNext=False, nextCursor=None))
            if req.operation.value == "CONDITIONAL_ORDERS":
                return wire(dict(conditionalOrders=[], hasNext=False, nextCursor=None))
            result = public(req)
            if candle_transform and req.interval == "1m":
                body = json.loads(result.body)["result"]
                body["candles"] = candle_transform(
                    req.symbol, body["candles"], clock[0]
                )
                result = wire(body)
            if closing_price and req.interval == "1m":
                body = json.loads(result.body)["result"]
                for c in body["candles"]:
                    if c["timestamp"][11:16] == "15:31" and req.symbol == codes[0]:
                        c.update(
                            {
                                k: closing_price
                                for k in (
                                    "openPrice",
                                    "highPrice",
                                    "lowPrice",
                                    "closePrice",
                                )
                            }
                        )
                    if "15:21" <= c["timestamp"][11:16] <= "15:30":
                        c["volume"] = "0"
                result = wire(body)
            if (
                late_bar
                and req.interval == "1m"
                and req.symbol == codes[0]
                and clock[0] < instant(at("13:18:00"))
            ):
                body = json.loads(result.body)["result"]
                body["candles"] = [
                    c
                    for c in body["candles"]
                    if instant(c["timestamp"]) != instant(at("13:11:00"))
                ]
                return wire(body)
            return result

    class Prices:
        def __init__(self, **kw):
            pass

        def __call__(self, req):
            record("CURRENT_PRICES", req)
            return prices(req)

    monkeypatch.setattr(composition, "TossAuthLiveTransport", Auth)
    monkeypatch.setattr(composition, "TossReadsLiveTransport", Reads)
    monkeypatch.setattr(composition, "TossPricesLiveTransport", Prices)
    monkeypatch.setattr("tokmo.facts.toss_reads_live.TossReadsLiveTransport", Reads)
    monkeypatch.setattr("tokmo.facts.toss_prices_live.TossPricesLiveTransport", Prices)
    configured = composition.build_pulse_production_live_deps(
        project_root=project,
        runtime_root=root,
        clock=utc,
        dialogue_cutoff=utc(),
        environ={
            "TOKMO_OPPORTUNITY_CAPTURE": "1",
            "TOKMO_QUANT_INPUT_CAPTURE": "1" if quant_inventory else "0",
        },
        capture_sleeper=sleep,
    )

    class Refs:
        def scopes(self):
            return frozenset({TOSS_SCOPE})

        def reveal(self, scope):
            return "123"

    lookahead = lambda market: (
        (
            LookaheadDayV1(
                Market.KR,
                "2026-09-28",
                "2026-09-28",
                (
                    LookaheadSessionV1(
                        "regularMarket",
                        instant(at("09:00:00")).astimezone(UTC),
                        instant(at("15:30:00")).astimezone(UTC),
                    ),
                ),
                utc(),
                "calendar:KR:2026-09-28:0",
            ),
        )
        if market is Market.KR
        else ()
    )
    deps = replace(
        configured,
        holdings_reader=lambda: replace(_holdings(), captured_at=utc()),
        native_refs_reader=lambda: Refs(),
        lookahead_reader=lookahead,
        lane_registry=_cutover_registry(),
        cadence_boundary_gate=lambda slot: True,
    )
    if quant_inventory:
        # Real automatic inventory/15-minute refresh; only local inputs are fixtures.
        data = dict(
            instruments=[
                dict(
                    symbol=f"{333000 + i:06d}",
                    market="KR",
                    currency="KRW",
                    price="10000",
                    price_at=utc().isoformat(),
                )
                for i in range(quant_inventory)
            ],
            positions=[],
            accounts=[
                dict(account=f"synthetic-{i}", scope="personal") for i in range(5)
            ],
        )
        monkeypatch.setattr(
            "tokmo.programs.quant_decision_inputs.load_local_snapshot",
            lambda *a, **kw: data,
        )
        monkeypatch.setattr(
            "tokmo.programs.quant_input_capture.read_existing_account_refs",
            lambda *a, **kw: Refs(),
        )
        calendar = dict(
            days=[
                dict(
                    market="KR",
                    trade_date="2026-09-28",
                    sessions=[
                        dict(
                            kind="regularMarket",
                            start=at("09:00:00"),
                            end=at("15:30:00"),
                        )
                    ],
                )
            ],
            history=dict(
                KR={f"2026-09-{d}": d in (23, 28) for d in range(23, 29)}, US={}
            ),
        )
        monkeypatch.setattr(
            "tokmo.programs.quant_input_refresh.read_calendar",
            lambda *a, **kw: calendar,
        )
    measurements = []

    def tick(quant_reads=0):
        slot = utc()
        tick_events.clear()

        def quant(**kw):
            from tokmo.facts.toss_reads import TossReadOperation as Op
            from tokmo.programs.quant_input_capture import ExistingTokenReader

            reader = ExistingTokenReader(
                token=kw["existing_token"],
                refs=None,
                transport=Reads(),
                clock=utc,
                deadline=kw["deadline"],
                wire_recorder=kw["wire_recorder"],
            )
            for _ in range(quant_reads):
                reader(Op.STOCKS, symbols=(codes[0],))
            return dict(calls=reader.calls)

        result = run_pulse_once(
            root=root,
            policy_version="pulse-60s-v1",
            scheduled_for_utc=slot,
            observed_now_utc=slot,
            deps=replace(
                deps,
                quant_input_capture=quant
                if quant_reads
                else configured.quant_input_capture,
            ),
        )
        assert result.outcome in ("SAVED", "SKIPPED_CLOSED"), result
        assert result.wire_counts["total"] == len(tick_events)
        assert len(tick_events) <= 17 and result.rolling_wire_starts <= 85
        assert (utc() - slot).total_seconds() <= 35
        optional = [
            i for i, e in enumerate(tick_events) if e["lane"] == "OPPORTUNITY_READ"
        ]
        if optional:
            assert all(
                e["lane"] == "OPPORTUNITY_READ" for e in tick_events[optional[0] :]
            )
        for i in optional:
            if i:
                assert (
                    instant(tick_events[i]["at"]) - instant(tick_events[i - 1]["at"])
                ).total_seconds() >= 2
        if instant(at("09:00:00")) <= slot < instant(at("15:30:00")):
            assert [e["op"] for e in tick_events[:4]] == [
                "AUTH",
                "CURRENT_PRICES",
                "ORDERS",
                "CONDITIONAL_ORDERS",
            ]
        measurements.append(
            dict(
                at=slot.isoformat(),
                wires=len(tick_events),
                elapsed=(utc() - slot).total_seconds(),
            )
        )
        path = root / "opportunity-rotation/captured.json"
        snapshot = json.loads(path.read_text()) if path.exists() else {}
        clock[0] = (slot + timedelta(minutes=1)).astimezone(
            instant(at("08:00:00")).tzinfo
        )
        return snapshot

    return p, clock, tick, codes, events, measurements


def cli_step(root, time):
    command = [
        sys.executable,
        str(ROOT / "tools/run_opportunity_rotation.py"),
        "--state-root",
        str(root / "opportunity-rotation/paper"),
        "step",
        "--captured",
        str(root / "opportunity-rotation/captured.json"),
        "--at",
        time if "T" in time else at(time),
        "--context-root",
        str(root / "empty-context"),
        "--manifest",
        str(root / "absent-manifest"),
    ]
    result = subprocess.run(
        command,
        cwd=root,
        env={
            **os.environ,
            "PYTHONPATH": str(ROOT / "src")
            + os.pathsep
            + os.environ.get("PYTHONPATH", ""),
        },
        text=True,
        capture_output=True,
    )
    assert result.stdout, result.stderr
    return result, json.loads(result.stdout)


def test_real_normal_workload_meets_both_slots_and_cli(tmp_path, monkeypatch):
    from opportunity_rotation_model import ProductModel

    from tokmo.programs.opportunity_rotation_schedule import run_slot

    p, clock, tick, codes, events, measurements = composed(tmp_path, monkeypatch)
    snapshot = {}
    first_ready = None
    while clock[0] <= instant(at("13:30:00")):
        time = clock[0].strftime("%H:%M:%S")
        if time == "13:10:00":
            for i, code in enumerate(codes[:5]):
                p.propose(
                    [
                        proposal(
                            id=f"normal-{i}", symbol=code, time="13:09:00", quantity=1
                        )
                    ],
                    session(),
                )
        snapshot = tick()
        if first_ready is None and snapshot.get("discovery"):
            first_ready = time
        if time not in ("10:05:00", "13:30:00"):
            continue
        d = snapshot["discovery"]
        m = snapshot["market"]
        assert {c["symbol"] for c in d["candidates"]} == set(codes)
        assert all(c["bars"] for c in d["candidates"])
        assert (instant(at(time)) - instant(m["valuation_at"])).total_seconds() <= 300
        assert all(c["complete"] for c in m["coverage"])
        # Same actual CLI with captured provider facts must reach the missing
        # model gate, proving data acceptance without any external model call.
        decision_time = instant(at(time)) + timedelta(
            seconds=measurements[-1]["elapsed"]
        )
        result, output = cli_step(tmp_path / "data", decision_time.strftime("%H:%M:%S"))
        assert result.returncode == 2 and output["decision"]["status"] == "FAILED", (
            output
        )
        assert (
            "GATE_NOT_INSTALLED" in output["decision"]["reason"]
            or "MANIFEST" in output["decision"]["reason"]
        )
        assert not list((p.store.path.parent / "receipts").glob("*CALL.json"))
        # Separate temporary PAPER copy exercises the real adapter with an
        # explicitly synthetic gate/executor and no installed product approval.
        from tokmo.programs.opportunity_rotation import Paper
        from tokmo.records.quant_decision_reports import atomic_private

        proof = Paper(tmp_path / ("synthetic-model-" + time.replace(":", "")))
        state = p.state()
        state["slots"] = {}
        atomic_private(proof.store.path, state)

        class Gate:
            def admit(self, *a, **kw):
                return dict(
                    scope=dict(identity_synthetic=False),
                    route=dict(provider="codex", model="gpt-6-astra"),
                )

            def recheck(self, *a, **kw):
                assert list((proof.store.path.parent / "receipts").glob("*CALL.json"))

        def execute(prompt, **kw):
            assert list((proof.store.path.parent / "receipts").glob("*CALL.json"))
            packet = json.loads(prompt.split("\n", 1)[1])
            assert len(packet["candidates"]) == 159
            return json.dumps(
                dict(
                    cash_reason="합성 예약 유지",
                    assessments=[
                        dict(symbol=s, action="WATCH", reason="합성 검토")
                        for s in codes
                    ],
                )
            ), dict(_provider="codex", _model="gpt-6-astra")

        model = ProductModel(
            manifest=tmp_path / "absent",
            receipt_root=proof.store.path.parent / "receipts",
            gate=Gate(),
            executor=execute,
            synthetic=True,
        )
        result = run_slot(proof, d, clock=lambda: decision_time, model=model)
        assert result["status"] == "WAITING_DATA"
        while proof.state()["slots"][result["slot"]]["status"] == "WAITING_DATA":
            proof.advance(tick()["market"])
        assert proof.state()["slots"][result["slot"]]["status"] == "DECIDED"
        print(
            json.dumps(
                dict(
                    synthetic=True,
                    slot=time,
                    candidates=len(d["candidates"]),
                    valuation_at=m["valuation_at"],
                    first_ready=first_ready,
                    model="synthetic executor only",
                    cli="missing gate; zero external calls",
                )
            )
        )
    assert all(
        sum(m["wires"] for m in measurements[i : i + 5]) <= 85
        for i in range(len(measurements))
    )
    histories = Counter(e["symbol"] for e in events if e["interval"] == "1d")
    assert set(histories) == set(codes) and set(histories.values()) == {1}
    print(
        json.dumps(
            dict(
                ticks=len(measurements),
                physical_wires=len(events),
                max_tick=max(m["wires"] for m in measurements),
                max_elapsed=max(m["elapsed"] for m in measurements),
                daily_histories=len(histories),
            )
        )
    )


def test_cold_capture_still_is_data_not_ready_in_actual_cli(tmp_path, monkeypatch):
    p, clock, tick, codes, events, measurements = composed(
        tmp_path, monkeypatch, start="10:05:00", count=5
    )
    assert tick(quant_reads=4).get("discovery") is None
    assert any(e["lane"] == "OPPORTUNITY_READ" for e in events)
    result, output = cli_step(tmp_path / "data", "10:05:20")
    assert result.returncode == 0 and output["decision"]["status"] == "DATA_NOT_READY"
    assert p.state()["proposals"] == {} and not list(
        (p.store.path.parent / "receipts").glob("*CALL.json")
    )


@pytest.mark.parametrize(
    "latency,fail_at,public_count", [(4, None, 2), (10, None, 0), (1, 5, 2)]
)
def test_deadline_and_failed_physical_wires_are_bounded(
    tmp_path, monkeypatch, latency, fail_at, public_count
):
    p, clock, tick, codes, events, measurements = composed(
        tmp_path,
        monkeypatch,
        start="08:00:00",
        count=5,
        latency=latency,
        fail_at=fail_at,
    )
    snapshot = tick()
    assert sum(e["lane"] == "OPPORTUNITY_READ" for e in events) == public_count
    if fail_at:
        assert snapshot["receipt"]["status"] == "FAILED"
        assert snapshot["receipt"]["operations"] == [
            e["op"] for e in events if e["lane"] == "OPPORTUNITY_READ"
        ]
        assert not any("synthetic-bearer" in str(v) for v in snapshot.values())


def test_checkpoint_revisits_missing_provider_minute_after_restart(
    tmp_path, monkeypatch
):
    p, clock, tick, codes, events, measurements = composed(
        tmp_path, monkeypatch, start="13:10:00", count=5, late_bar=True
    )
    p.propose([proposal(time="13:09:00", quantity=1)], session())
    saw_gap = False
    snapshot = {}
    while clock[0] <= instant(at("13:25:00")):
        snapshot = tick()
        market = snapshot.get("market")
        if not market:
            continue
        saw_gap |= any(not c["complete"] for c in market["coverage"])
        p.advance(market)
    assert saw_gap and all(c["complete"] for c in snapshot["market"]["coverage"])
    assert instant(p.state()["proposals"]["b1"]["reconciled_until"]) >= instant(
        at("13:20:00")
    )
    assert any(
        b["symbol"] == codes[0] and b["at"] == at("13:11:00")
        for b in snapshot["market"]["bars"]
    )
