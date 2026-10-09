"""Real Toss parsers exercised over synthetic transport; zero external calls."""

import json
from datetime import datetime
from pathlib import Path

import pytest

from tokmo.facts.opportunity_rotation import TossOpportunitySource
from tokmo.facts.toss import _TossTransportFailure, _TossTransportSuccess
from tokmo.facts.toss_reads import TossReadOperation as Op


def instant(t="09:05:00"):
    return datetime.fromisoformat("2026-09-28T" + t + "+09:00")


def candle(t, price="10000"):
    return dict(
        timestamp=instant(t).isoformat(),
        openPrice=price,
        highPrice=price,
        lowPrice=price,
        closePrice=price,
        volume="100",
        currency="KRW",
    )


def wire(value):
    return _TossTransportSuccess(body=json.dumps({"result": value}).encode())


def source(transport, max_calls=20):
    return TossOpportunitySource(
        token="synthetic",
        transport=transport,
        price_transport=lambda r: wire(
            [
                dict(
                    symbol=s,
                    lastPrice="10000",
                    currency="KRW",
                    timestamp=instant().isoformat(),
                )
                for s in r.symbols
            ]
        ),
        clock=lambda: instant(),
        max_calls=max_calls,
        pace=lambda: None,
    )


def test_minute_pagination_inclusive_cursor_dedup_and_poll_crossing():
    calls = []

    def transport(req):
        calls.append(req)
        if len(calls) == 1:
            return wire(
                dict(
                    candles=[candle("09:04:00"), candle("09:03:00")],
                    nextBefore=instant("09:03:00").isoformat(),
                )
            )
        return wire(
            dict(
                candles=[candle("09:03:00"), candle("09:02:00"), candle("09:01:00")],
                nextBefore=None,
            )
        )

    rows = source(transport).minutes("111111", instant("09:01:00"), instant())
    assert [r["at"] for r in rows] == [
        instant(t).isoformat() for t in ("09:02:00", "09:03:00", "09:04:00")
    ]
    assert calls[1].before == instant("09:03:00")
    assert all(
        r.adjusted is False
        and r.interval == "1m"
        and r.count == 200
        and r.account_seq is None
        for r in calls
    )


def test_stalled_pagination_budget_and_auth_failure_are_honest():
    calls = []

    def stuck(req):
        calls.append(req)
        return wire(
            dict(
                candles=[candle("09:03:00")], nextBefore=instant("09:03:00").isoformat()
            )
        )

    with pytest.raises(ValueError, match="pagination"):
        source(stuck).minutes("111111", instant("09:00:00"), instant())
    with pytest.raises(ValueError, match="budget"):
        source(stuck, max_calls=1).minutes("111111", instant("09:00:00"), instant())
    bad = source(lambda req: _TossTransportFailure(code="auth_rejection"))
    with pytest.raises(ValueError, match="auth_rejection"):
        bad.minutes("111111", instant("09:00:00"), instant())
    with pytest.raises(ValueError):
        bad.minutes("111111", instant("09:00:00"), instant())
    assert len(bad.calls) == 1


def test_discovery_provider_maximum_all_candidates_and_missing_context():
    calls = []

    def transport(req):
        calls.append(req)
        if req.operation == Op.KR_MARKET_CALENDAR:

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
                    today=day("2026-09-28"),
                    previousBusinessDay=day("2026-09-23"),
                    nextBusinessDay=day("2026-09-29"),
                )
            )
        if req.operation == Op.RANKINGS:
            return wire(
                dict(
                    rankedAt=instant().isoformat(),
                    rankings=[
                        dict(
                            rank=i + 1,
                            symbol=f"{i:06}",
                            currency="KRW",
                            price=dict(lastPrice="10000", basePrice="9000"),
                            tradingVolume="100",
                            tradingAmount="1000000",
                        )
                        for i in range(8)
                    ],
                )
            )
        if req.operation == Op.STOCKS:
            return wire(
                [
                    dict(
                        symbol=s,
                        name="합성" + s,
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
        if req.operation == Op.STOCK_CANDLES:
            c = candle("09:00:00")
            c["timestamp"] = "2026-09-23T09:00:00+09:00"
            return wire(dict(candles=[c], nextBefore=None))
        raise AssertionError(req.operation)

    result = source(transport).discover(held_symbols=())
    assert len(result["candidates"]) == 8  # no first-5 or first-6 discovery cutoff
    assert all(c["eligible"] and c["bars"] and c["quote"] for c in result["candidates"])
    assert {r.count for r in calls if r.operation == Op.RANKINGS} == {100}
    assert {r.ranking_type for r in calls if r.operation == Op.RANKINGS} == {
        "MARKET_TRADING_AMOUNT",
        "MARKET_TRADING_VOLUME",
    }
    assert all(c["evidence"] == [] for c in result["candidates"])
    assert all("news" in c["missing"] for c in result["candidates"])
    assert all(r.account_seq is None for r in calls)


def test_runner_has_no_live_order_or_model_route_and_default_is_project():
    root = Path(__file__).resolve().parents[1]
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "opportunity_runner", root / "tools/run_opportunity_rotation.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    assert runner.DEFAULT_STATE_ROOT == root / "var/data/opportunity-rotation/paper"
    for path in [
        root / "tools/run_opportunity_rotation.py",
        *root.glob("src/tokmo/*/opportunity_rotation.py"),
    ]:
        text = path.read_text()
        assert not any(
            term in text
            for term in (
                "toss_order_commands",
                "rotation_policy",
                "QuantModel",
                "subprocess",
                "hyundai",
            )
        )


def test_calendar_missing_is_not_a_holiday():
    from datetime import date

    from tokmo.facts.opportunity_rotation import _session
    from tokmo.values.toss_market_calendar import KrMarketDay

    with pytest.raises(ValueError, match="calendar"):
        _session(KrMarketDay(date(2026, 9, 28)))


def test_capture_paginates_between_polls_only_regular_same_time_marks():
    def transport(req):
        if req.operation == Op.KR_MARKET_CALENDAR:

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
                    today=day("2026-09-28"),
                    previousBusinessDay=day("2026-09-23"),
                    nextBusinessDay=day("2026-09-29"),
                )
            )
        return wire(
            dict(
                candles=[
                    candle(t)
                    for t in (
                        "09:05:00",
                        "09:04:00",
                        "09:02:00",
                        "09:00:00",
                        "08:59:00",
                    )
                ],
                nextBefore=None,
            )
        )

    captured = source(transport).capture(
        symbols={"111111", "069500"}, start=instant("08:00:00"), end=instant()
    )
    assert len(captured["coverage"]) == 2
    assert all(
        b["at"] > instant("09:00:00").isoformat() and b["at"] <= instant().isoformat()
        for b in captured["bars"]
    )
    assert set(captured["marks"]) == {"111111", "069500"}
    assert captured["valuation_at"] == instant().isoformat()


def test_cli_semantic_source_failure_receipt_is_failed(tmp_path, monkeypatch, capsys):
    import importlib.util

    from tokmo.programs.opportunity_rotation import Paper
    from tokmo.values.opportunity_rotation import Costs

    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location(
        "receipt_runner", root / "tools/run_opportunity_rotation.py"
    )
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    Paper(tmp_path).initialize(Costs("test", "0", "0", "0", "0"))

    class FailedSource:
        calls = []
        max_calls = 3
        failed = False

        def discover(self, **kwargs):
            raise ValueError("calendar semantics unavailable")

    monkeypatch.setattr(runner, "connect", lambda args: FailedSource())
    result = runner.main(
        [
            "--state-root",
            str(tmp_path),
            "discover",
            "--toss",
            "--output",
            str(tmp_path / "packet.json"),
        ]
    )
    assert result == 2
    receipt = json.loads(next((tmp_path / "receipts").glob("*.json")).read_text())
    assert receipt["status"] == "FAILED"
    assert not (tmp_path / "packet.json").exists()


def test_official_end_timestamp_includes_last_completed_and_reports_gaps():
    def transport(req):
        if req.operation == Op.KR_MARKET_CALENDAR:

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
                    today=day("2026-09-28"),
                    previousBusinessDay=day("2026-09-23"),
                    nextBusinessDay=day("2026-09-29"),
                )
            )
        return wire(
            dict(
                candles=[
                    candle(t)
                    for t in (
                        "09:05:00",
                        "09:04:00",
                        "09:02:00",
                        "09:01:00",
                        "09:00:00",
                    )
                ],
                nextBefore=None,
            )
        )

    captured = source(transport).capture(
        symbols={"111111"}, start=instant("09:00:00"), end=instant()
    )
    assert [b["at"] for b in captured["bars"]] == [
        instant(t).isoformat() for t in ("09:01:00", "09:02:00", "09:04:00", "09:05:00")
    ]
    assert captured["valuation_at"] == instant().isoformat()
    assert captured["coverage"][0]["complete"] is False
