"""Automatic history policy through real capture/reader seams; no network."""

import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest
from test_quant_history_window import NOW, source

from tokmo.facts.toss_reads import TossReadOperation as Op
from tokmo.programs import quant_input_capture as capture
from tokmo.programs import quant_input_refresh as refresh


def setup(tmp_path, monkeypatch, symbols=("AAA", "BBB", "CCC")):
    data = dict(
        positions=[],
        instruments=[
            dict(
                market="US",
                symbol=s,
                currency="USD",
                price="105",
                price_at=NOW.isoformat(),
            )
            for s in symbols
        ],
    )
    cal = dict(days=[], history={"US": {"2026-09-25": True}, "KR": {}})
    monkeypatch.setattr(refresh, "read_calendar", lambda *a, **kw: cal)
    monkeypatch.setattr(
        "tokmo.programs.quant_decision_inputs.load_local_snapshot",
        lambda *a, **kw: deepcopy(data),
    )
    fetch, _ = source()
    calls = []

    def transport(request):
        calls.append(request)
        if request.operation is Op.STOCKS:
            value = [
                dict(
                    symbol=s,
                    name=s,
                    englishName=s,
                    isinCode="TEST-" + s,
                    market="NASDAQ",
                    securityType="STOCK",
                    isCommonShare=True,
                    status="ACTIVE",
                    currency="USD",
                    sharesOutstanding="1000",
                )
                for s in request.symbols
            ]
        else:
            page = fetch(Op.STOCK_CANDLES, before=request.before, count=request.count)
            value = dict(
                candles=[
                    dict(
                        timestamp=c.timestamp.isoformat(),
                        openPrice=c.open_price,
                        highPrice=c.high_price,
                        lowPrice=c.low_price,
                        closePrice=c.close_price,
                        volume=c.volume,
                        currency="USD",
                    )
                    for c in page.candles
                ],
                nextBefore=page.next_before.isoformat() if page.next_before else None,
            )
        return NS(body=json.dumps(dict(result=value)).encode())

    monkeypatch.setattr(
        "tokmo.facts.toss_reads_live.TossReadsLiveTransport", lambda: transport
    )

    def run():
        return refresh.refresh_due_inputs(
            project_root=tmp_path,
            data_root=tmp_path,
            existing_token="fixture-only",
            now=NOW,
            clock=lambda: NOW,
            deadline=NOW + timedelta(seconds=35),
        )

    return data, cal, calls, run


def test_automatic_year_is_one_symbol_per_tick_then_reused(tmp_path, monkeypatch):
    _, _, calls, run = setup(tmp_path, monkeypatch)
    for _ in range(3):
        result = run()
        assert result["job"] == "candles" and not result["failures"]
        assert (
            len(result["calls"]) == 3
        )  # metadata + two pages, within the old four-call ceiling
    rows = json.loads((tmp_path / "ops/quant-inputs/latest.json").read_text())[
        "instruments"
    ]
    assert {r["symbol"] for r in rows} == {"AAA", "BBB", "CCC"}
    for row in rows:
        history = row["metrics"]["price_history"]
        assert history["requested_start"] == "2025-09-25"
        assert history["status"] == "COVERED" and history["actual_count"] > 200
    count = len(calls)
    assert run()["skipped"] and len(calls) == count


def test_source_exhaustion_is_partial_but_not_repeated_for_same_session(
    tmp_path, monkeypatch
):
    data, cal, _, _ = setup(tmp_path, monkeypatch, ("AAA",))
    jobs, _ = refresh.inventory_jobs(data, cal, NOW)
    history = dict(
        requested_start="2025-09-25",
        requested_end="2026-09-25",
        requested_sessions=None,
        actual_count=1,
        actual_start="2026-09-25",
        actual_end="2026-09-25",
        stop_reason="SOURCE_EXHAUSTED",
        status="PARTIAL",
    )
    row = dict(
        market="US",
        symbol="AAA",
        bars=[dict(date="2026-09-25", adjusted=True)],
        metrics=dict(price_history=history),
    )
    assert not refresh.select_due(jobs, {}, dict(instruments=[row]), NOW)
    history["requested_start"] = None
    history["requested_sessions"] = 200
    assert refresh.select_due(jobs, {}, dict(instruments=[row]), NOW)


def test_queued_legacy_batches_keep_all_symbols_and_period_override(
    tmp_path, monkeypatch
):
    _, _, calls, _ = setup(tmp_path, monkeypatch, ("AAA", "BBB", "CCC", "DDD", "EEE"))
    folder = tmp_path / "ops/quant-inputs"
    folder.mkdir(parents=True)
    request_path = folder / "request.json"
    request_path.write_text(
        json.dumps(
            dict(
                jobs=[
                    dict(kind="candles", offset=0),
                    dict(
                        kind="candles",
                        offset=3,
                        history_sessions=60,
                        history_reason="short-check",
                    ),
                ]
            )
        )
    )
    consumer = capture.requested_capture_consumer(
        project_root=tmp_path, data_root=tmp_path, clock=lambda: NOW
    )
    for _ in range(5):
        result = consumer(
            existing_token="fixture-only", now=NOW, deadline=NOW + timedelta(seconds=35)
        )
        assert not result["failures"] and len(result["calls"]) <= 4
    request = json.loads(request_path.read_text())
    assert not request["jobs"] and not request.get("failed")
    rows = {
        r["symbol"]: r
        for r in json.loads((folder / "latest.json").read_text())["instruments"]
    }
    assert set(rows) == {"AAA", "BBB", "CCC", "DDD", "EEE"}
    assert all(len(rows[s]["bars"]) > 200 for s in ("AAA", "BBB", "CCC"))
    assert all(len(rows[s]["bars"]) == 60 for s in ("DDD", "EEE"))
    assert rows["EEE"]["metrics"]["price_history"]["reason"] == "short-check"
    assert {r.symbol for r in calls if r.operation is Op.STOCK_CANDLES} == set(rows)


@pytest.mark.parametrize("failure_after", [1, 2])
def test_legacy_cache_refresh_failure_preserves_bars_and_cooldown(
    tmp_path, monkeypatch, failure_after
):
    _, _, calls, run = setup(tmp_path, monkeypatch, ("AAA",))
    from tokmo.facts.toss_reads_live import TossReadsLiveTransport

    transport = TossReadsLiveTransport()

    def limited(request):
        if len(calls) == failure_after:
            calls.append(request)
            return NS(code="http_429", retry_after=180)
        return transport(request)

    monkeypatch.setattr(
        "tokmo.facts.toss_reads_live.TossReadsLiveTransport", lambda: limited
    )
    folder = tmp_path / "ops/quant-inputs"
    folder.mkdir(parents=True)
    old = dict(
        market="US",
        symbol="AAA",
        bars=[dict(date="2026-09-25", adjusted=True, close="100")],
    )
    (folder / "latest.json").write_text(json.dumps(dict(instruments=[old])))
    assert run()["failures"] and len(calls) == failure_after + 1
    assert json.loads((folder / "latest.json").read_text())["instruments"][0] == old
    assert run()["skipped"] and len(calls) == failure_after + 1


def test_queued_symbol_identity_survives_holdings_order_change(tmp_path, monkeypatch):
    data, _, _, _ = setup(tmp_path, monkeypatch)
    folder = tmp_path / "ops/quant-inputs"
    folder.mkdir(parents=True)
    (folder / "request.json").write_text(json.dumps(dict(jobs=[dict(kind="candles")])))
    consumer = capture.requested_capture_consumer(
        project_root=tmp_path, data_root=tmp_path, clock=lambda: NOW
    )
    for _ in range(3):
        assert not consumer(
            existing_token="fixture-only", now=NOW, deadline=NOW + timedelta(seconds=35)
        )["failures"]
        data["instruments"].reverse()
    rows = json.loads((folder / "latest.json").read_text())["instruments"]
    assert {row["symbol"] for row in rows} == {"AAA", "BBB", "CCC"}


@pytest.mark.parametrize(
    "invalid",
    [
        dict(history_start="2025/09/25"),
        dict(history_start="2099-01-01"),
        dict(history_sessions=0),
        dict(history_sessions="60"),
        dict(history_sessions=True),
        dict(history_start="2025-09-25", history_sessions=60),
    ],
)
def test_invalid_history_job_retires_without_wire_and_next_job_runs(
    tmp_path, monkeypatch, invalid
):
    _, _, calls, _ = setup(tmp_path, monkeypatch, ("AAA",))
    folder = tmp_path / "ops/quant-inputs"
    folder.mkdir(parents=True)
    cache = folder / "latest.json"
    cache.write_text('{"instruments": []}')
    before = cache.read_bytes()
    request_path = folder / "request.json"
    request_path.write_text(
        json.dumps(
            dict(
                jobs=[
                    dict(kind="candles", **invalid),
                    dict(
                        kind="candles",
                        instrument=dict(market="US", symbol="AAA"),
                        offset=3,
                        history_sessions=60,
                    ),
                ]
            )
        )
    )
    consumer = capture.requested_capture_consumer(
        project_root=tmp_path, data_root=tmp_path, clock=lambda: NOW
    )
    result = consumer(
        existing_token="fixture-only", now=NOW, deadline=NOW + timedelta(seconds=35)
    )
    assert result["failures"] and not calls and cache.read_bytes() == before
    request = json.loads(request_path.read_text())
    assert len(request["failed"]) == len(request["jobs"]) == 1
    assert result["failures"][0]["reason"].startswith("invalid_history_")
    assert not consumer(
        existing_token="fixture-only", now=NOW, deadline=NOW + timedelta(seconds=35)
    )["failures"]
    assert len(json.loads(cache.read_text())["instruments"][0]["bars"]) == 60
    assert not json.loads(request_path.read_text())["jobs"]


def test_unexpected_validation_failure_is_not_retired_as_a_bad_request(
    tmp_path, monkeypatch
):
    _, _, calls, _ = setup(tmp_path, monkeypatch, ("AAA",))
    path = tmp_path / "ops/quant-inputs/request.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(jobs=[dict(kind="candles")])))
    before = path.read_bytes()

    def unexpected(**kw):
        raise ValueError("fixture_internal_failure")

    monkeypatch.setattr(capture, "validate_history_request", unexpected)
    consumer = capture.requested_capture_consumer(
        project_root=tmp_path, data_root=tmp_path, clock=lambda: NOW
    )
    with pytest.raises(ValueError, match="fixture_internal_failure"):
        consumer(
            existing_token="fixture-only", now=NOW, deadline=NOW + timedelta(seconds=35)
        )
    assert not calls and path.read_bytes() == before


def test_long_cache_reuse_short_request_and_new_session_preserve_verified_history(
    tmp_path, monkeypatch
):
    from test_quant_history_window import capture as collect

    data, cal, calls, run = setup(tmp_path, monkeypatch, ("AAA",))
    folder = tmp_path / "ops/quant-inputs"
    folder.mkdir(parents=True)
    path = folder / "latest.json"
    fetch, _ = source()
    long = collect(fetch, history_start="2024-09-25")["instruments"][0] | dict(
        symbol="AAA"
    )
    path.write_text(json.dumps(dict(instruments=[long])))
    assert run()["skipped"] and not calls
    for sessions in (60, 1):
        fetch, _ = source()
        assert not capture.capture_job(
            fetch,
            data=data,
            path=path,
            job="candles",
            now=NOW,
            history_sessions=sessions,
            history_reason="short-analysis",
        )["failures"]
        row = json.loads(path.read_text())["instruments"][0]
        assert row["bars"] == long["bars"]
        assert row["metrics"]["price_history"]["requested_sessions"] == sessions
        assert row["metrics"]["price_history"]["actual_count"] == sessions
        assert run()["skipped"] and not calls
    # A new completed session extends the cache without discarding its older range.
    fetch, _ = source()

    def next_session(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES:
            page.candles.insert(
                0,
                NS(**(vars(page.candles[0]) | dict(timestamp=NOW + timedelta(days=3)))),
            )
        return page

    later = NOW + timedelta(days=3)
    assert not capture.capture_job(
        next_session,
        data=data,
        path=path,
        job="candles",
        now=later,
        history_sessions=60,
    )["failures"]
    row = json.loads(path.read_text())["instruments"][0]
    assert (
        row["bars"][:-1] == long["bars"] and len(row["bars"]) == len(long["bars"]) + 1
    )
    jobs, _ = refresh.inventory_jobs(data, cal, NOW)
    for broken in (
        dict(actual_count=1),
        dict(requested_start="bad"),
        dict(actual_end="2026-09-24"),
    ):
        damaged = deepcopy(long)
        damaged["metrics"]["price_history"].update(broken)
        assert refresh.select_due(jobs, {}, dict(instruments=[damaged]), NOW)
    for malformed in (
        None,
        [],
        dict(
            long["metrics"]["price_history"],
            available_at=(NOW + timedelta(days=1)).isoformat(),
        ),
    ):
        damaged = deepcopy(long)
        damaged["metrics"]["price_history"] = malformed
        assert refresh.select_due(jobs, {}, dict(instruments=[damaged]), NOW)
    # Repeated short captures missing the latest session never erase retained bars.
    retained = row["bars"]
    for _ in range(2):
        fetch, _ = source()
        capture.capture_job(
            fetch, data=data, path=path, job="candles", now=later, history_sessions=60
        )
        saved = json.loads(path.read_text())["instruments"][0]
        assert saved["bars"] == retained
        assert saved["metrics"]["price_history"]["cache_coverage"][
            "actual_count"
        ] == len(retained)
    # A successful provider correction replaces that date, not the older range.
    fetch, _ = source()

    def revised(op, **kw):
        page = fetch(op, **kw)
        if op is Op.STOCK_CANDLES:
            page.candles[0].close_price = "106"
        return page

    result = capture.capture_job(
        revised, data=data, path=path, job="candles", now=NOW, history_sessions=60
    )
    assert not result["failures"]
    updated = json.loads(path.read_text())["instruments"][0]["bars"]
    assert len(updated) == len(retained) and updated[0] == retained[0]
    assert next(b for b in updated if b["date"] == "2026-09-25")["close"] == "106"


@pytest.mark.parametrize("field,value", [("volume", "900"), ("close", "105.0")])
def test_automatic_refresh_accepts_updated_provider_values(
    tmp_path, monkeypatch, field, value
):
    from test_quant_history_window import capture as collect

    _, _, calls, run = setup(tmp_path, monkeypatch, ("AAA",))
    fetch, _ = source()
    old = collect(fetch, history_start="2024-09-25")["instruments"][0] | dict(
        symbol="AAA"
    )
    old["bars"].pop()
    old["bars"][-1][field] = value
    old["metrics"]["price_history"].update(
        actual_count=len(old["bars"]),
        actual_end="2026-09-24",
        requested_end="2026-09-24",
    )
    path = tmp_path / "ops/quant-inputs/latest.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(dict(instruments=[old])))
    result = run()
    assert not result["failures"] and len(calls) <= 4
    saved = json.loads(path.read_text())["instruments"][0]
    assert (
        saved["bars"][0]["date"] == "2024-09-25"
        and saved["bars"][-1]["date"] == "2026-09-25"
    )
    assert next(b for b in saved["bars"] if b["date"] == "2026-09-24")[field] == (
        "1000" if field == "volume" else "105"
    )
    count = len(calls)
    assert run()["skipped"] and len(calls) == count
