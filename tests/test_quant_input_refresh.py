import json
from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace

from test_price_reference import calendar
from test_quant_decisions import sample_input

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.programs.quant_input_capture import requested_capture_consumer
from tokmo.programs.quant_input_refresh import (
    completed_session,
    refresh_due_inputs,
)

NOW = datetime.fromisoformat("2026-09-09T12:00:00+09:00")


def harness(tmp_path, monkeypatch):
    data = sample_input()
    for i in data["instruments"]:
        i["price_at"] = NOW.isoformat()
    data["positions"].append(
        dict(account="pension-a", scope="retirement", key="US:SAMPLE", quantity="5")
    )
    cal = calendar()
    cal["days"].append(
        dict(
            market="KR",
            trade_date="2026-09-09",
            sessions=[
                dict(
                    kind="regularMarket",
                    start="2026-09-09T09:00:00+09:00",
                    end="2026-09-09T15:30:00+09:00",
                )
            ],
        )
    )
    cal["history"]["KR"] = {"2026-09-08": True, "2026-09-09": True}
    monkeypatch.setattr(
        "tokmo.programs.quant_input_refresh.read_calendar", lambda *a, **kw: cal
    )
    seen = []
    loads = []

    def load(*args, **kwargs):
        loads.append(kwargs)
        return deepcopy(data)

    monkeypatch.setattr(
        "tokmo.programs.quant_decision_inputs.load_local_snapshot", load
    )
    monkeypatch.setattr(
        "tokmo.programs.quant_input_capture.read_existing_account_refs",
        lambda *a, **kw: SimpleNamespace(reveal=lambda a: "123"),
    )

    def transport(request):
        seen.append(request)
        assert request.token == "pulse-token"
        if request.operation is Op.BUYING_POWER:
            result = dict(currency=request.currency, cashBuyingPower="1000")
        elif request.operation is Op.EXCHANGE_RATE:
            result = dict(
                baseCurrency="USD",
                quoteCurrency="KRW",
                rate="1401",
                midRate="1400",
                basisPoint="7",
                rateChangeType="UP",
                validFrom=NOW.isoformat(),
                validUntil=(NOW + timedelta(days=1)).isoformat(),
            )
        elif request.operation is Op.COMMISSIONS:
            result = []
        elif request.operation is Op.ORDERS:
            result = dict(orders=[], hasNext=False, nextCursor=None)
        elif request.operation is Op.CONDITIONAL_ORDERS:
            result = dict(conditionalOrders=[], hasNext=False, nextCursor=None)
        elif request.operation is Op.SELLABLE_QUANTITY:
            assert (
                request.symbol == "000001"
            )  # Pension quantities are never queried/rewritten.
            result = dict(sellableQuantity="100")
        elif request.operation is Op.STOCKS:
            result = [
                dict(
                    symbol=s,
                    name=s,
                    englishName=s,
                    isinCode="TEST-" + s,
                    market="KOSPI" if s == "000001" else "NASDAQ",
                    securityType="STOCK",
                    isCommonShare=True,
                    status="ACTIVE",
                    currency="KRW" if s == "000001" else "USD",
                    sharesOutstanding="1000",
                )
                for s in request.symbols
            ]
        else:
            assert request.operation is Op.STOCK_CANDLES and request.count == 200
            zone = "+09:00" if request.symbol == "000001" else "-04:00"
            result = dict(
                candles=[
                    dict(
                        timestamp="2026-09-08T00:00:00" + zone,
                        openPrice="100",
                        highPrice="110",
                        lowPrice="90",
                        closePrice="105",
                        volume="1000",
                        currency="KRW" if zone == "+09:00" else "USD",
                    )
                ],
                nextBefore=None,
            )
        return SimpleNamespace(body=json.dumps(dict(result=result)).encode())

    monkeypatch.setattr(
        "tokmo.facts.toss_reads_live.TossReadsLiveTransport", lambda: transport
    )

    def run(at=NOW, budget=35):
        return refresh_due_inputs(
            project_root=tmp_path,
            data_root=tmp_path,
            existing_token="pulse-token",
            clock=lambda: at,
            now=at,
            deadline=at + timedelta(seconds=budget),
        )

    return data, cal, seen, loads, run


def test_automatic_refresh_finishes_then_reuses_bars_and_refreshes_account_inputs(
    tmp_path, monkeypatch
):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    (tmp_path / "investment_followup.json").write_text(
        json.dumps(dict(pension_analysis="/configured/manifest.json"))
    )
    results = [run(NOW + timedelta(minutes=n)) for n in range(8)]
    assert all(not r["failures"] and len(r["calls"]) <= 4 for r in results)
    assert {r["job"] for r in results} >= {"funds", "orders", "sellable", "candles"}
    assert not loads[0].get("pension_analysis")
    cache = json.loads((tmp_path / "ops/quant-inputs/latest.json").read_text())
    assert len(cache["cash"]) == 2 and len(cache["positions"]) == 1
    assert all(i["bars"][-1]["date"] == "2026-09-08" for i in cache["instruments"])
    n = len(seen)
    assert run(NOW + timedelta(minutes=10))["skipped"] and len(seen) == n
    assert run(NOW + timedelta(minutes=15))["job"] == "funds" and len(seen) == n + 3
    assert sum(r.operation is Op.STOCK_CANDLES for r in seen) == 2
    assert not (tmp_path / "ops/quant-inputs/request.json").exists()


def test_holdings_change_becomes_due_before_regular_interval(tmp_path, monkeypatch):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    for n in range(5):
        run(NOW + timedelta(minutes=n))
    data["positions"][0]["quantity"] = "120"
    result = run(NOW + timedelta(minutes=6))
    assert result["job"] == "funds" and not result["failures"]


def test_missing_pension_quote_uses_existing_prices_reader_and_does_not_mutate_holdings(
    tmp_path, monkeypatch
):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    data["instruments"][1]["price"] = None
    data["instruments"][1]["price_at"] = None

    def prices(request):
        seen.append(request)
        assert request.token == "pulse-token" and request.symbols == ("SAMPLE",)
        return SimpleNamespace(
            body=json.dumps(
                dict(
                    result=[
                        dict(
                            symbol="SAMPLE",
                            lastPrice="123",
                            currency="USD",
                            timestamp=NOW.isoformat(),
                        )
                    ]
                )
            ).encode()
        )

    monkeypatch.setattr(
        "tokmo.facts.toss_prices_live.TossPricesLiveTransport", lambda: prices
    )
    result = run()
    assert (
        result["job"] == "quotes"
        and not result["failures"]
        and len(result["calls"]) == 1
    )
    saved = json.loads((tmp_path / "ops/quant-inputs/latest.json").read_text())
    assert (
        saved["instruments"][0]["price"] == "123"
        and saved["instruments"][0]["price_at"] == NOW.isoformat()
    )
    assert saved["instruments"][0]["sources"] == [
        "Toss GET /api/v1/prices · 퀀트 시세 보충"
    ]
    assert data["positions"][-1]["quantity"] == "5"
    assert not (tmp_path / "stores/current_holdings.json").exists()
    assert not (tmp_path / "stores/latest_quotes.sqlite3").exists()
    assert run(NOW + timedelta(minutes=1))["job"] != "quotes"


def test_closed_account_retains_cash_and_calendar_handles_holiday_and_close_delay(
    tmp_path, monkeypatch
):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    assert (
        completed_session(
            cal, "US", datetime.fromisoformat("2026-09-08T14:00:00+09:00")
        )
        == "2026-09-04"
    )
    assert completed_session(cal, "KR", NOW.replace(hour=15, minute=34)) == "2026-09-08"
    assert completed_session(cal, "KR", NOW.replace(hour=15, minute=35)) == "2026-09-09"
    data["positions"] = []
    data["accounts"] = [dict(account="personal-a", scope="personal")]
    assert run()["job"] == "funds" and len(seen) == 3


def test_missing_calendar_day_is_not_treated_as_a_holiday(tmp_path, monkeypatch):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    del cal["history"]["US"]["2026-09-07"]
    assert (
        completed_session(
            cal, "US", datetime.fromisoformat("2026-09-08T14:00:00+09:00")
        )
        is None
    )


def test_early_close_is_captured_after_the_actual_close_not_normal_hours(
    tmp_path, monkeypatch
):
    from tokmo.programs.quant_decision_inputs import capture_market_data

    at = datetime.fromisoformat("2026-09-09T13:05:00-04:00")
    seen = []

    def fetch(op, **kw):
        seen.append((op, kw))
        if op is Op.STOCKS:
            return []
        return SimpleNamespace(
            candles=[
                SimpleNamespace(
                    timestamp=datetime.fromisoformat("2026-09-09T00:00:00-04:00"),
                    open_price="100",
                    high_price="110",
                    low_price="90",
                    close_price="105",
                    volume="1000",
                )
            ]
        )

    result = capture_market_data(
        fetch,
        instruments=[dict(symbol="V", market="US")],
        now=at,
        completed_session="2026-09-09",
        session_closes={"US:2026-09-09": "2026-09-09T13:00:00-04:00"},
    )
    assert (
        not result["failures"]
        and result["instruments"][0]["bars"][0]["date"] == "2026-09-09"
    )
    assert seen[-1][1]["before"].date().isoformat() == "2026-09-10"


def test_rate_limit_stops_remaining_calls_and_honors_shared_cooldown(
    tmp_path, monkeypatch
):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)

    def limited(request):
        seen.append(request)
        return SimpleNamespace(code="http_429", retry_after=180)

    monkeypatch.setattr(
        "tokmo.facts.toss_reads_live.TossReadsLiveTransport", lambda: limited
    )
    assert run()["failures"] and len(seen) == 1
    assert run(NOW + timedelta(minutes=1))["skipped"] and len(seen) == 1
    run(NOW + timedelta(minutes=3))
    assert len(seen) == 2


def test_no_remaining_time_does_not_consume_attempts(tmp_path, monkeypatch):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    result = run(budget=2)
    state = json.loads((tmp_path / "ops/quant-inputs/refresh.json").read_text())
    assert result["skipped"] and not seen
    assert not any(v.get("attempts") for v in state["jobs"].values())
    assert run()["job"] == "funds"


def test_failed_job_retries_three_times_then_waits_one_hour(tmp_path, monkeypatch):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    data["instruments"] = []
    data["positions"] = []
    data["accounts"] = [dict(account="personal-a", scope="personal")]

    def failed(request):
        seen.append(request)
        return SimpleNamespace(code="timeout")

    monkeypatch.setattr(
        "tokmo.facts.toss_reads_live.TossReadsLiveTransport", lambda: failed
    )
    for minute in (0, 1, 5, 6, 20, 21):
        run(NOW + timedelta(minutes=minute))
    state = json.loads((tmp_path / "ops/quant-inputs/refresh.json").read_text())
    assert all(v["attempts"] == 3 for v in state["jobs"].values())
    n = len(seen)
    assert run(NOW + timedelta(minutes=40))["skipped"] and len(seen) == n


def test_explicit_request_keeps_priority_over_automatic_work(tmp_path, monkeypatch):
    data, cal, seen, loads, run = harness(tmp_path, monkeypatch)
    root = tmp_path / "ops/quant-inputs"
    root.mkdir(parents=True)
    (root / "request.json").write_text(json.dumps(dict(jobs=[dict(kind="orders")])))
    consumer = requested_capture_consumer(
        project_root=tmp_path, data_root=tmp_path, clock=lambda: NOW
    )
    result = consumer(
        existing_token="pulse-token", now=NOW, deadline=NOW + timedelta(seconds=35)
    )
    assert result["job"] == "orders" and not (root / "refresh.json").exists()
    assert not json.loads((root / "request.json").read_text())["jobs"]


def test_fresh_quote_and_bars_survive_older_supplements_and_old_quote_uses_new_close(
    tmp_path, monkeypatch
):
    from tokmo.programs import quant_decision_inputs as inputs

    state = SimpleNamespace(captured_at=NOW, accounts=[])
    monkeypatch.setattr(inputs.CurrentHoldingsStore, "read", lambda self: state)
    bar = dict(
        date="2026-09-08",
        available_at="2026-09-08T15:30:00+09:00",
        close="110",
        adjusted=True,
    )
    current = dict(
        market="KR", symbol="000001", price="115", price_at=NOW.isoformat(), bars=[bar]
    )
    old = dict(
        market="KR",
        symbol="000001",
        price="100",
        price_at="2026-09-04T16:00:00+09:00",
        bars=[],
    )
    result = inputs.load_local_snapshot(
        tmp_path,
        tmp_path,
        as_of=NOW,
        include_context=False,
        supplement=[dict(instruments=[current]), dict(instruments=[old])],
    )
    item = result["instruments"][0]
    assert item["price"] == "115" and item["bars"] == [bar]
    current.pop("price")
    current.pop("price_at")
    result = inputs.load_local_snapshot(
        tmp_path,
        tmp_path,
        as_of=NOW,
        include_context=False,
        supplement=[dict(instruments=[current]), dict(instruments=[old])],
    )
    assert result["instruments"][0]["price"] == "110"
    assert any("종가 기준" in text for text in result["limitations"])
