"""Second-review regressions: synthetic data, real local product paths only."""

import json
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal

import pytest
from test_opportunity_rotation import at, bar, market, paper, proposal, session
from test_opportunity_rotation_autonomy import ready

from tokmo.programs.opportunity_rotation import Paper
from tokmo.programs.opportunity_rotation_schedule import run_slot
from tokmo.values.opportunity_rotation import instant


def protected(root):
    p = paper(root)
    p.propose(
        [
            proposal(
                time="10:00:00",
                protection={"target_price": "10300", "stop_price": "9500"},
            )
        ],
        session(),
    )
    p.advance(market([bar("10:00:00")], time="10:01:00"))
    p.advance(market(time="10:05:00"))
    return p


class ExitModel:
    def __init__(self, quantity=10):
        self.calls = 0
        self.quantity = quantity

    def decide(self, packet, call_id, at):
        self.calls += 1
        sell = proposal(
            "exit", side="SELL", price="9900", quantity=self.quantity, time="10:05:00"
        )
        sell.pop("proposed_at")
        return dict(
            id=call_id,
            packet_id=packet["id"],
            author="synthetic",
            verified_by="synthetic",
            verified_at=at,
            cash_reason="SYNTHETIC replacement",
            assessments=[
                dict(
                    symbol="111111",
                    action="EXIT",
                    reason="SYNTHETIC replacement",
                    proposal=sell,
                    cancel_ids=[p["id"] for p in packet["portfolio"]["pending"]],
                )
            ],
        )


def respond(p, model, response="10:06:10", recorded=None, committed=None):
    ticks = iter(
        [
            instant(at("10:05:20")),
            instant(at(response)),
            instant(at(recorded or response)),
            instant(at(committed or recorded or response)),
        ]
    )
    return run_slot(p, ready(), clock=lambda: next(ticks), model=model)


@pytest.mark.parametrize(
    "failure", ["invalid", "write", "close", "recording_delay", "slow_write"]
)
def test_invalid_replacement_does_not_commit_cancellation(
    tmp_path, monkeypatch, failure
):
    import tokmo.records.opportunity_rotation as records

    p = protected(tmp_path)
    model = ExitModel(quantity=999 if failure == "invalid" else 10)
    before = p.state()
    write = records.atomic_private

    def refuse_intent(path, state):
        if any(s["status"] == "WAITING_DATA" for s in state.get("slots", {}).values()):
            raise OSError("synthetic intent write failure")
        return write(path, state)

    if failure == "write":
        monkeypatch.setattr(records, "atomic_private", refuse_intent)
    assert (
        respond(
            p,
            model,
            response="15:29:10" if failure == "close" else "10:06:10",
            recorded="10:07:00" if failure == "recording_delay" else None,
            committed="10:07:00" if failure == "slow_write" else None,
        )["status"]
        == "FAILED"
    )
    after = p.state()
    assert after["proposals"] == before["proposals"]
    assert after["positions"] == before["positions"] and after["cash"] == before["cash"]
    assert model.calls == 1 and "exit" not in after["proposals"]


class CashModel(ExitModel):
    def decide(self, packet, call_id, at):
        result = super().decide(packet, call_id, at)
        buy = proposal(
            "replacement",
            time="10:07:00",
            protection={"target_price": "10300", "stop_price": "9500"},
        )
        buy.pop("proposed_at")
        result["assessments"][0].update(action="ENTER", proposal=buy)
        return result


@pytest.mark.parametrize(
    "observed,legacy",
    [
        ("10:09:00", False),
        ("16:01:00", False),
        ("2026-09-29T09:02:00+09:00", False),
        ("10:09:00", True),
    ],
)
def test_saved_cash_transition_missing_boundary_and_late_catchup(
    tmp_path, observed, legacy
):
    p = paper(tmp_path)
    p.propose([proposal(time="10:00:00", price="9000")], session())
    p.advance(market(time="10:05:00"))
    model = CashModel()
    result = respond(p, model)
    key = result["slot"]
    assert result["status"] == "WAITING_DATA" and p.report()["reserved_cash"] == "90000"
    if legacy:

        def remove_intent(state):
            for name in ("effective_at", "recorded_at", "changes"):
                state["slots"][key].pop(name, None)
            return state

        p.store.transaction(remove_intent)
    # END10:07 is preceding; END10:08 is post-boundary. Arrival order is reversed.
    m = market([bar("10:07:00", low="9500", high="10400")], time="10:08:00")
    missing = deepcopy(m)
    missing["bars"] = [b for b in reversed(m["bars"]) if b["at"] != at("10:07:00")]
    p.advance(missing)
    assert p.report()["reserved_cash"] == "90000" and not p.state()["fills"]
    m["as_of"] = observed if "T" in observed else at(observed)
    p = Paper(tmp_path)
    p.advance(m)
    state = p.state()
    saved = state["slots"][key]
    assert (
        saved["status"] == ("SUPERSEDED" if legacy else "DECIDED") and model.calls == 1
    )
    if legacy:
        assert "effective_at" not in saved and "replacement" not in state["proposals"]
    else:
        assert (
            saved["effective_at"] == at("10:07:00")
            and saved["applied_at"] == m["as_of"]
        )
        assert state["cash"] == "1900000" and len(state["fills"]) == 1
        fill = state["fills"][0]
        assert (
            fill["id"] == "replacement"
            and fill["bar_end_at"] == at("10:08:00")
            and fill["observed_at"] == m["as_of"]
        )
        assert (
            p.report()["pending"][0]["id"] == "replacement:protection"
        )  # no same-bar exit
        with pytest.raises(ValueError, match="backdated"):
            p.cancel(
                "replacement:protection", at("10:07:00"), "not a recorded transition"
            )
    assert p.advance(m) == p.report() and p.state() == state
    revised = deepcopy(m)
    revised["source"] = "SYNTHETIC revised capture"
    with pytest.raises(ValueError, match="out-of-order"):
        p.advance(revised, observed_at=at("16:01:00", "2026-09-29"))


def closing_market(*, final=True, price="10000", observed="16:00:00", day="2026-09-28"):
    m = market(
        [bar("09:00:00", symbol="069500", day=day)],
        time=observed,
        symbols=("111111", "069500"),
        day=day,
    )
    if final:
        m["bars"] += [
            bar(
                "15:30:00",
                symbol=c,
                opening=price,
                close=price,
                low=price,
                high=price,
                day=day,
            )
            for c in ("111111", "069500")
        ]
        m["marks"] = {
            c: dict(price=price, at=at("15:31:00", day)) for c in ("111111", "069500")
        }
    m["valuation_at"] = at("15:31:00", day)
    return m


@pytest.mark.parametrize("legacy", [False, True])
def test_eod_retained_protection_final_data_once_and_overnight(tmp_path, legacy):
    p = protected(tmp_path)
    missing = p.advance(closing_market(final=False))
    samples = missing["samples"]
    assert missing["settlement"]["status"] == "PENDING_DATA"
    final = closing_market(observed="16:01:00")
    r = p.advance(final)
    assert r["settlement"]["status"] == "SETTLED"
    assert r["reconciliation"]["status"] == "COMPLETE" and r["samples"] == samples + 1
    assert r["pending"][0]["id"] == "b1:protection"
    assert Paper(tmp_path).advance(final) == r
    final["as_of"] = at("16:02:00")
    assert p.advance(final)["samples"] == samples + 1
    if legacy:
        p.store.transaction(
            lambda s: {k: v for k, v in s.items() if k != "daily_closes"}
        )
    next_day = closing_market(observed="16:01:00", day="2026-09-29")
    r = Paper(tmp_path).advance(next_day)
    assert r["settlement"]["status"] == "SETTLED" and r["samples"] == samples + 2
    assert len(p.state()["fills"]) == 1 and r["pending"][0]["quantity"] == 10


@pytest.mark.parametrize(
    "missing,next_price",
    [
        (None, "9000"),
        (None, "12000"),
        ("111111", "9000"),
        ("069500", "12000"),
        ("opening", "9000"),
    ],
)
def test_true_next_day_settlement_uses_prior_close(tmp_path, missing, next_price):
    p = paper(tmp_path)
    p.propose([proposal(time="10:00:00", price="9000")], session())
    p.advance(market(time="10:05:00"))
    model = CashModel()
    key = respond(p, model)["slot"]
    close = closing_market(price="10200", observed="16:01:00")
    close["bars"] = [b for b in close["bars"] if b["at"] != at("10:08:00")] + [
        bar("10:07:00", high="10000")
    ]
    withheld = [
        b
        for b in close["bars"]
        if (b["symbol"] == missing and b["at"] == at("15:31:00"))
        or (
            missing == "opening"
            and b["symbol"] == "069500"
            and b["at"] == at("09:01:00")
        )
    ]
    close["bars"] = [
        b for b in close["bars"] if b not in withheld and b["at"] != at("10:07:00")
    ]
    close["marks"].pop(missing, None)
    assert p.advance(close)["settlement"]["status"] == "PENDING_DATA"
    assert p.report()["reserved_cash"] == "90000" and not p.state()["fills"]
    day = "2026-09-29"
    observed = at("09:02:00", day)
    nxt = market(
        [
            bar("10:06:00"),
            bar(
                "09:00:00",
                day=day,
                opening=next_price,
                low=next_price,
                high=next_price,
                close=next_price,
            ),
        ],
        time="09:02:00",
        day=day,
        symbols=("111111", "069500"),
        marks={
            c: dict(price="30000", at=at("09:01:00", day)) for c in ("111111", "069500")
        },
    )
    nxt["sessions"].append(session())
    p = Paper(tmp_path)
    r = p.advance(nxt)
    assert p.state()["slots"][key]["status"] == "DECIDED" and model.calls == 1
    assert p.state()["slots"][key]["effective_at"] == at("10:07:00")
    if missing:
        assert r["settlement"]["status"] == "PENDING_DATA"
        assert not any(s["at"] == at("15:31:00") for s in p.state()["samples"])
        nxt["bars"] = withheld
        nxt["as_of"] = at("09:03:00", day)
        r = Paper(tmp_path).advance(nxt)
    state = p.state()
    fills = state["fills"]
    assert r["settlement"] == dict(
        date="2026-09-28",
        status="SETTLED",
        observed_at=nxt["as_of"],
        valuation_at=at("15:31:00"),
    )
    assert r["valuation_at"] == observed and r["reconciliation"]["status"] == "COMPLETE"
    assert [(f["at"], f["bar_end_at"]) for f in fills] == [
        (at("10:08:00"), at("10:08:00")),
        (at("09:01:00", day), at("09:01:00", day)),
    ]
    assert all(f["observed_at"] in (observed, nxt["as_of"]) for f in fills)
    expected_cash = "1990000" if next_price == "9000" else "2003000"
    assert (
        r["cash"] == r["available_cash"] == expected_cash
        and r["reserved_cash"] == "0"
        and not r["positions"]
    )
    assert (
        state["benchmark"]["open_price"] == "10000"
        and state["benchmark"]["quantity"] == 200
    )
    assert [s for s in state["samples"] if s["at"] == at("15:31:00")] == [
        dict(at=at("15:31:00"), nav="2002000", benchmark_nav="2040000")
    ]
    assert [s["at"] for s in state["samples"]] == sorted(
        s["at"] for s in state["samples"]
    )
    assert state["peak_nav"] == ("2002000" if next_price == "9000" else "2003000")
    expected_dd = (
        Decimal(12000) / Decimal(2002000) if next_price == "9000" else Decimal(0)
    )
    assert abs(Decimal(r["observed_max_drawdown"]) - expected_dd) < Decimal("1e-12")
    assert Paper(tmp_path).advance(nxt) == r and p.state() == state
    nxt["as_of"] = at("09:04:00", day)
    p.advance(nxt)
    assert p.state()["samples"] == state["samples"] and p.state()["fills"] == fills
    print(
        json.dumps(
            dict(
                true_next_day=True,
                missing=missing,
                next_price=next_price,
                settlement=r["settlement"],
                samples=state["samples"],
                fills=fills,
                peak=state["peak_nav"],
                mdd=r["observed_max_drawdown"],
            ),
            ensure_ascii=False,
        )
    )


def test_closing_print_capture_parser_and_ledger_time_domains(tmp_path):
    from test_opportunity_rotation_source import candle, source, wire

    calls = []

    def transport(req):
        if req.operation.value == "KR_MARKET_CALENDAR":

            def day(d):
                return dict(
                    date=d,
                    integrated=dict(
                        regularMarket=dict(
                            startTime=at("09:00:00", d), endTime=at("15:30:00", d)
                        )
                    ),
                )

            return wire(
                dict(
                    today=day("2026-09-28"),
                    previousBusinessDay=day("2026-09-25"),
                    nextBusinessDay=day("2026-09-29"),
                )
            )
        calls.append(req.before)
        rows = [
            dict(
                candle(t, "10500"),
                volume="100" if t in ("15:31:00", "15:32:00") else "0",
            )
            for t in ("15:28:00", "15:29:00", "15:30:00", "15:31:00", "15:32:00")
        ]
        return wire(
            dict(
                candles=[c for c in rows if instant(c["timestamp"]) <= req.before],
                nextBefore=None,
            )
        )

    captured = source(transport).capture(
        symbols={"111111"}, start=at("15:27:00"), end=at("16:00:00")
    )
    assert calls == [instant(at("15:31:00"))]
    assert captured["valuation_at"] == at("15:31:00")
    assert (
        captured["marks"]["111111"]["price"] == "10500"
        and captured["coverage"][0]["complete"]
    )
    p = protected(tmp_path)
    m = closing_market(price="10500")
    m["bars"] += [
        bar("15:31:00", opening="9000", close="9000", low="9000", high="9000")
    ]
    p.advance(m)
    fill = p.state()["fills"][-1]
    assert fill["side"] == "SELL" and fill["at"] == at("15:30:00")
    assert fill["bar_end_at"] == at("15:31:00") and fill["observed_at"] == at(
        "16:00:00"
    )
    assert fill["event_start_at"] == fill["event_end_at"] == at("15:30:00")
    assert p.report()["settlement"]["status"] == "SETTLED"
    assert Paper(tmp_path).advance(m)["cash"] == "2003000"


@pytest.mark.parametrize("created", ["15:29:00", "15:29:30"])
def test_close_order_validity_missing_late_and_no_same_bar_exit(tmp_path, created):
    p = paper(tmp_path)
    p.propose(
        [
            proposal(
                time=created,
                review_at=at("16:00:00"),
                protection={"target_price": "10300", "stop_price": "9500"},
            )
        ],
        session(),
    )
    m = market(time="15:30:00")
    # A touch before a mid-minute proposal must not be inferred.
    m["bars"] = [b for b in m["bars"] if b["at"] != at("15:30:00")] + [bar("15:29:00")]
    m["bars"][-1]["volume"] = "100" if created.endswith("30") else "0"
    assert p.advance(m)["reserved_cash"] == "100000"
    with pytest.raises(ValueError, match="reconcil"):
        p.cancel("b1", at("15:30:00"), "close print not observed")
    assert p.advance(closing_market(final=False))["reserved_cash"] == "100000"
    m = closing_market(observed="16:01:00")
    m["bars"][-2].update(low="9000", high="11000")
    r = p.advance(m)
    assert len(p.state()["fills"]) == 1 and p.state()["fills"][0]["at"] == at(
        "15:30:00"
    )
    assert (
        r["pending"][0]["id"] == "b1:protection"
        and r["settlement"]["status"] == "SETTLED"
    )
    assert Paper(tmp_path).advance(m) == r
    with pytest.raises(ValueError, match="outside"):
        p.propose(
            [proposal("after", time="16:02:00", review_at=at("17:00:00"))], session()
        )


@pytest.mark.parametrize(
    "code",
    [
        "http_429",
        "http_5xx",
        "timeout",
        "connection_failure",
        "auth_rejection",
        "forbidden",
        "contract_mismatch",
    ],
)
def test_capture_next_permitted_tick_recovers_transient_only(tmp_path, code):
    from test_opportunity_rotation_controller_readiness import capture_clocked

    from tokmo.facts.toss import _TossTransportFailure
    from tokmo.programs.opportunity_rotation_capture import capture_consumer

    p, clock, tick, _, _ = capture_clocked(tmp_path, "09:01:00", 1, paginated=True)
    public, prices = tick.transports
    requests = []

    def transport(req):
        requests.append(req.operation.value)
        if len(requests) == 2:
            if code == "contract_mismatch":
                from test_opportunity_rotation_source import wire

                return wire({"invalid": "SYNTHETIC malformed response"})
            return _TossTransportFailure(
                code=code, retry_after=120 if code == "http_429" else None
            )
        return public(req)

    def consumer():
        return capture_consumer(
            data_root=tmp_path / "data",
            transport=transport,
            price_transport=prices,
            clock=lambda: clock[0],
            sleeper=lambda _: None,
        )

    def consume(c, budget=3):
        return c(
            existing_token="SYNTHETIC-bearer",
            now=clock[0],
            deadline=clock[0] + timedelta(seconds=35),
            wire_recorder=lambda *a: None,
            remaining_budget=budget,
        )

    c = consumer()
    first = consume(c)
    assert first == dict(status="FAILED", calls=2) and len(requests) == 2
    path = tmp_path / "data/opportunity-rotation/captured.json"
    before = json.loads(path.read_text())
    assert before["work"]["discovery"]["cache"]  # first completed calendar survived
    transient = code in ("http_429", "http_5xx", "timeout", "connection_failure")
    assert c.due(instant(at("09:01:30"))) is False
    assert c.due(instant(at("09:01:00", "2026-09-29"))) is transient
    clock[0] += timedelta(minutes=1)
    if code == "http_429":
        assert c.due(clock[0]) is False and consume(c)["calls"] == 0
        clock[0] += timedelta(minutes=1)
    c = consumer()  # restart reads durable progress and error
    assert c.due(clock[0]) is transient
    if transient:
        assert consume(c, 0)["calls"] == 0
        assert len(requests) == 2
        result = consume(c)
        assert result["status"] != "FAILED" and result["calls"] <= 3
        assert requests[2] == "RANKINGS"  # discovery resumes after its saved calendar
        saved = json.loads(path.read_text())
        assert saved["last_error"]["recovered_at"] == clock[0].isoformat()
        assert saved["last_error"]["code"] == code and saved["last_error"]["at"] == at(
            "09:01:00"
        )
        assert saved["market"]["bars"]
    else:
        assert consume(c) == dict(status="BLOCKED", calls=0) and len(requests) == 2
    assert "SYNTHETIC-bearer" not in path.read_text()


def large_discovery():
    from datetime import date

    d = ready()
    d["completed_session"] = "2026-09-25"
    days = []
    day = date(2026, 9, 25)
    while len(days) < 200:
        if day.weekday() < 5:
            days.append(day.isoformat())
        day -= timedelta(days=1)
    template = d["candidates"][0]
    template["bars"] = [
        dict(
            date=day,
            open="10000",
            high="10500",
            low="9500",
            close="10000",
            volume="1000",
        )
        for day in reversed(days)
    ]
    d["candidates"] = [
        dict(deepcopy(template), symbol=f"{111111 + i:06}") for i in range(159)
    ]
    return d


def model_prompt(packet, root):
    from types import SimpleNamespace

    from opportunity_rotation_model import MODEL, PROVIDER, ProductModel

    captured = []

    def execute(prompt, **kwargs):
        captured.append(prompt)
        return json.dumps(
            dict(cash_reason="SYNTHETIC size only", assessments=[])
        ), dict(_model=MODEL, _provider=PROVIDER)

    model = ProductModel(
        manifest=root / "absent",
        receipt_root=root / "receipts",
        synthetic=True,
        gate=SimpleNamespace(
            admit=lambda *a, **kw: dict(scope={}, route={}),
            recheck=lambda *a, **kw: None,
        ),
        executor=execute,
    )
    model.decide(packet, call_id="synthetic-size", at=at("10:05:00"))
    receipt = json.loads(next((root / "receipts").glob("*-RESULT.json")).read_text())
    assert receipt["response"] == dict(
        cash_reason="SYNTHETIC size only", assessments=[]
    )
    assert receipt["prompt_utf8_bytes"] == len(captured[0].encode())
    return captured[0]


def test_compact_model_contract_keeps_all_candidates_full_history_and_quality(tmp_path):
    from tokmo.programs.opportunity_rotation import build_packet
    from tokmo.values.opportunity_rotation import digest

    d = large_discovery()
    raw = deepcopy(d)
    p = protected(tmp_path / "paper")
    packet = build_packet(d, p.state())
    original = deepcopy(packet)
    prompt = model_prompt(packet, tmp_path / "before")
    sent = json.loads(prompt.split("\n", 1)[1])
    assert len(sent["candidates"]) == 159
    assert {c["symbol"] for c in sent["candidates"]} == {
        c["symbol"] for c in raw["candidates"]
    }
    assert all("bars" not in c for c in sent["candidates"])
    assert (
        packet == original
        and d == raw
        and all(len(c["bars"]) == 200 for c in packet["candidates"])
    )
    assert sent["portfolio"] == packet["portfolio"]
    assert sent["decision_contract"]["model_response_fields"] == [
        "cash_reason",
        "assessments",
    ]
    # Far-history extreme: full-history drawdown/range must affect what the
    # model receives, even though the last 199 candles are identical.
    d["candidates"][0]["bars"][0].update(open="20000", high="21000", close="20000")
    changed = build_packet(d, p.state())
    next_sent = json.loads(model_prompt(changed, tmp_path / "after").split("\n", 1)[1])
    first = sent["candidates"][0]
    far = next_sent["candidates"][0]
    assert first["features"]["max_drawdown"] != far["features"]["max_drawdown"]
    assert first["range_reference"]["high"] != far["range_reference"]["high"]
    assert first["history"]["sha256"] == digest(raw["candidates"][0]["bars"])
    d["candidates"][1]["bars"] = d["candidates"][1]["bars"][-1:]
    d["candidates"][2]["bars"] = []
    d["candidates"][2]["missing"] = ["completed_ohlcv", "news"]
    quality = json.loads(
        model_prompt(build_packet(d, p.state()), tmp_path / "quality").split("\n", 1)[1]
    )
    assert quality["candidates"][1]["features"]["ret_120"]["status"] == "INSUFFICIENT"
    assert quality["candidates"][2]["history"]["observations"] == 0
    assert quality["candidates"][2]["missing"] == ["completed_ohlcv", "news"]
    assert quality["candidates"][2]["trade_ready"] is False
    assert (
        len(prompt.encode()) < len(json.dumps(packet, ensure_ascii=False).encode()) / 3
    )


@pytest.mark.parametrize(
    "count,fill_count,delayed,touch,resume",
    [
        (1, 0, False, "old", None),
        (5, 0, False, "old", None),
        (5, 0, True, "old", None),
        (5, 1, True, None, None),
        (5, 5, True, None, None),
        (5, 0, True, "new", None),
        (5, 0, True, "old", "16:01:00"),
        (5, 0, True, "new", "2026-09-29T09:02:00+09:00"),
        (5, 0, True, "old", "true-next-day"),
        (5, 0, True, "old", "true-next-day-close"),
    ],
)
def test_normal_five_held_saved_replacement(
    tmp_path, monkeypatch, count, fill_count, delayed, touch, resume
):
    from test_opportunity_rotation_capacity import cli_step, composed

    true_next_day = bool(resume and resume.startswith("true-next-day"))

    def candles(code, rows, observed):
        for row in rows:
            if (
                row["timestamp"] == at("13:32:00" if fill_count == 1 else "13:31:00")
                and code in codes[:fill_count]
            ):
                row["lowPrice"] = "44000"
            if row["timestamp"] == at("13:33:00") and code in codes and touch:
                row.update(
                    lowPrice="44000" if touch == "old" else "50000",
                    highPrice="50000" if touch == "old" else "53000",
                )
            if row["timestamp"] == at("15:31:00") and code in codes:
                row.update(
                    openPrice="54000",
                    highPrice="54000",
                    lowPrice="54000",
                    closePrice="54000",
                )
        return [
            r
            for r in rows
            if not (
                delayed
                and observed
                < instant(
                    at("09:02:00", "2026-09-29") if true_next_day else at("13:40:00")
                )
                and r["timestamp"] == at("13:31:00")
                and code in (codes[-1:] if touch else codes)
            )
            and not (
                resume == "true-next-day-close"
                and code == "069500"
                and r["timestamp"] == at("15:31:00")
                and observed < instant(at("09:08:00", "2026-09-29"))
            )
        ]

    p, clock, tick, codes, events, measurements = composed(
        tmp_path,
        monkeypatch,
        start="13:10:00",
        count=count,
        closing_price="54000",
        candle_transform=candles,
    )
    for i, code in enumerate(codes):
        p.propose(
            [
                proposal(
                    f"b{i}",
                    symbol=code,
                    time="13:09:00",
                    price="50000",
                    quantity=1,
                    protection={"target_price": "55000", "stop_price": "45000"},
                )
            ],
            session(),
        )
    while clock[0] <= instant(at("13:30:00")):
        snapshot = tick()
        if snapshot.get("market"):
            p.advance(snapshot["market"])

    class Model(ExitModel):
        def decide(self, packet, call_id, at):
            result = super().decide(packet, call_id, at)
            result["assessments"] = []
            for i, code in enumerate(codes):
                sell = proposal(
                    f"exit{i}",
                    symbol=code,
                    side="SELL",
                    price="52000",
                    quantity=1,
                    time="13:30:00",
                    review_at="2026-09-28T14:30:00+09:00",
                )
                sell.pop("proposed_at")
                result["assessments"].append(
                    dict(
                        symbol=code,
                        action="EXIT",
                        reason="SYNTHETIC replacement",
                        proposal=sell,
                        cancel_ids=[f"b{i}:protection"],
                    )
                )
            clock[0] = instant("2026-09-28T13:31:10+09:00")
            return result

    model = Model(1)
    clock[0] = max(
        instant(at("13:30:20")),
        instant(snapshot["discovery"]["as_of"]),
        instant(snapshot["market"]["as_of"]),
    )
    decision_at = clock[0].isoformat()
    result = run_slot(p, snapshot["discovery"], clock=lambda: clock[0], model=model)
    assert result["status"] == "WAITING_DATA" and len(p.report()["pending"]) == count
    saved = p.state()["slots"][result["slot"]]
    observations = [result]
    p = Paper(p.store.root)
    clock[0] = instant(at("13:32:00"))
    deadline = instant(at("15:40:00" if resume else "14:00:00"))
    while clock[0] <= deadline:
        before = clock[0]
        snapshot = tick()
        if resume and instant(at("13:40:00")) <= before < deadline:
            continue
        observed = (
            resume
            if resume and not true_next_day and before == deadline
            else max(
                before.replace(second=20), instant(snapshot["market"]["as_of"])
            ).isoformat()
        )
        process, output = cli_step(tmp_path / "data", observed)
        assert process.returncode == 0, output
        result = output["decision"]
        observations.append(
            dict(
                status=result["status"],
                at=before.isoformat(),
                valuation=snapshot["market"]["valuation_at"],
            )
        )
        current = p.state()["slots"][saved["decision"]["id"]]
        assert (
            current["decision"] == saved["decision"]
            and current["packet"] == saved["packet"]
        )
        if delayed and before < instant(at("13:40:00")):
            assert (
                result["status"] == "WAITING_DATA"
                and len(p.report()["pending"]) == count
                and len(p.state()["fills"]) == count
            )
            assert (
                p.report()["cash"]
                == p.report()["available_cash"]
                == str(2000000 - count * 50000)
            )
            assert [x["id"] for x in p.report()["pending"]] == [
                f"b{i}:protection" for i in range(count)
            ]
        if current["status"] in ("DECIDED", "SUPERSEDED") and len(
            p.state()["fills"]
        ) == count + fill_count + (count if touch == "new" or resume else 0):
            break
    if true_next_day:
        assert p.report()["settlement"]["status"] == "PENDING_DATA"
        clock[0] = instant(at("09:02:00", "2026-09-29"))
        while clock[0] <= instant(at("09:15:00", "2026-09-29")):
            before = clock[0]
            snapshot = tick()
            observed = before.replace(second=40).isoformat()
            process, output = cli_step(tmp_path / "data", observed)
            assert process.returncode == 0, output
            current = p.state()["slots"][saved["decision"]["id"]]
            observations.append(
                dict(
                    status=current["status"],
                    at=observed,
                    valuation=snapshot["market"]["valuation_at"],
                    settlement=p.report()["settlement"],
                )
            )
            if (
                current["status"] == "DECIDED"
                and p.report()["settlement"]["status"] == "SETTLED"
            ):
                break
        assert snapshot["market"]["valuation_at"].startswith("2026-09-29")
        assert p.report()["settlement"]["status"] == "SETTLED"
        assert [s for s in p.state()["samples"] if s["at"] == at("15:31:00")] == [
            dict(at=at("15:31:00"), nav="2010000", benchmark_nav="2000000")
        ]
    state = p.state()
    pending = p.report()["pending"]
    assert model.calls == 1 and len(state["fills"]) == count + fill_count + (
        count if touch == "new" or resume else 0
    )
    assert p.advance(snapshot["market"]) == p.report() and p.state() == state
    assert current["status"] == ("SUPERSEDED" if fill_count else "DECIDED"), (
        "normal five-held saved replacement must finish"
    )
    assert saved["effective_at"] == current["effective_at"] == at("13:32:00")
    assert saved["at"] == decision_at and saved["recorded_at"] == saved["decision"][
        "verified_at"
    ] == at("13:31:10")
    assert state["cash"] == str(
        2000000
        - count * 50000
        + fill_count * 45000
        + (count * 52000 if touch == "new" or resume else 0)
    )
    if fill_count:
        assert [x["id"] for x in pending] == [
            f"b{i}:protection" for i in range(fill_count, count)
        ]
        assert not any(k.startswith("exit") for k in state["proposals"])
        assert all(
            f["bar_end_at"] == at("13:32:00" if fill_count == 1 else "13:31:00")
            and instant(f["observed_at"]) >= instant(at("13:40:00"))
            for f in state["fills"][count:]
        )
    else:
        applied = current["applied_at"]
        effective = current["effective_at"]
        assert [x["id"] for x in pending] == (
            [] if touch == "new" or resume else [f"exit{i}" for i in range(count)]
        )
        assert all(
            state["proposals"][f"exit{i}"]["proposed_at"] == effective
            and state["proposals"][f"exit{i}"]["quantity"] == 1
            for i in range(count)
        )
        expected_applied = (
            next(o["at"] for o in observations[1:] if o["status"] == "DECIDED")
            if true_next_day
            else observed
            if "T" in observed
            else at(observed)
        )
        assert applied == expected_applied
        assert instant(applied) >= instant(effective) and instant(
            current["recorded_at"]
        ) < instant(effective)
        assert all(
            state["proposals"][f"b{i}:protection"]["cancelled_at"] == effective
            for i in range(count)
        )
        assert all(
            state["proposals"][f"b{i}:protection"]["reconciled_until"] == effective
            for i in range(count)
        )
        while clock[0] <= instant(at("15:40:00")):
            snapshot = tick()
            if snapshot.get("market"):
                p.advance(snapshot["market"])
        final = p.report()
        assert final["settlement"]["status"] == "SETTLED" and final["cash"] == str(
            2000000 + count * 2000
        )
        assert all(
            f["at"] == at("13:33:00" if touch == "new" else "15:30:00")
            and f["bar_end_at"] == at("13:33:00" if touch == "new" else "15:31:00")
            for f in p.state()["fills"][count:]
        )
        process, output = cli_step(
            tmp_path / "data", observed if true_next_day else resume or "15:41:00"
        )
        assert process.returncode == 0 and output["portfolio"] == final
        next_day = closing_market(day="2026-09-29")
        next_day["bars"] = []
        assert Paper(p.store.root).advance(next_day)["cash"] == final["cash"]
        assert len(p.state()["fills"]) == count * 2 and model.calls == 1
    print(
        json.dumps(
            dict(
                case=[count, fill_count, delayed, touch, resume],
                observations=observations,
                slot={
                    k: current[k]
                    for k in (
                        "status",
                        "at",
                        "recorded_at",
                        "effective_at",
                        "checked_at",
                    )
                },
                fills=p.state()["fills"],
                cash=p.state()["cash"],
                ticks=len(measurements),
            ),
            ensure_ascii=False,
        )
    )
