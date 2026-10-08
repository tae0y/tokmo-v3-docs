from datetime import date, datetime, timedelta, timezone
from pathlib import Path
import socket
import sqlite3

import pytest

SOCKET_TYPE = socket.socket

from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.request_parity import REQUEST_OPERATION_SPECS, canonical_params_digest


def test_f12_runs_exactly_one_utc_day_in_closed_production_stage_order(tmp_path):
    from tokmo.programs.integrated_fake_cadence import (
        _IntegratedStageDepsV1,
        _run_integrated_fake_day_range,
    )

    events = []

    def stage(name):
        def run(*, tick, **_kwargs):
            events.append((tick.tick_id, name))
            return None
        return run

    deps = _IntegratedStageDepsV1(
        calendar_provider=stage("calendar"),
        session_gate=stage("session"),
        holdings_runner=stage("holdings"),
        quotes_runner=stage("quotes"),
        trades_runner=stage("trades"),
        barrier_runner=stage("barrier"),
        analysis_runner=stage("analysis"),
        clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    report = _run_integrated_fake_day_range(
        utc_day=date(2026, 7, 30), root=tmp_path / "tokmo-v3-f", deps=deps,
    )

    assert report.tick_count == 288
    assert len({tick for tick, _stage in events}) == 288
    assert [name for _tick, name in events[:7]] == [
        "calendar", "session", "holdings", "quotes", "trades", "barrier", "analysis",
    ]
    assert all(
        [name for tick, name in events if tick == tick_id] ==
        ["calendar", "session", "holdings", "quotes", "trades", "barrier", "analysis"]
        for tick_id in {tick for tick, _stage in events}
    )
    assert report.utc_day == "2026-07-30"


def test_f12_denies_production_root_before_stage_calls(tmp_path, monkeypatch):
    from tokmo.programs.integrated_fake_cadence import (
        _IntegratedStageDepsV1,
        _run_integrated_fake_day_range,
    )

    called = []
    runner = lambda **kwargs: called.append(kwargs)
    deps = _IntegratedStageDepsV1(
        calendar_provider=runner, session_gate=runner, holdings_runner=runner,
        quotes_runner=runner, trades_runner=runner, barrier_runner=runner,
        analysis_runner=runner,
        clock=lambda: datetime(2026, 7, 30, tzinfo=timezone.utc),
    )
    production = tmp_path / "production"
    production.mkdir()
    monkeypatch.setattr(
        "tokmo.programs.integrated_fake_cadence._production_root",
        lambda: production,
    )

    with pytest.raises(ValueError, match="production root denied"):
        _run_integrated_fake_day_range(utc_day=date(2026, 7, 30), root=production / "child", deps=deps)
    assert called == []


def test_f12_288_ticks_call_real_collectors_and_write_durable_skip_receipts(tmp_path):
    from tokmo.programs.legacy_g2 import (
        legacy_run_holdings_sync as run_holdings_sync,
        legacy_run_quotes_sync as run_quotes_sync,
        legacy_run_trades_sync as run_trades_sync,
    )
    from tokmo.programs.integrated_fake_cadence import _IntegratedStageDepsV1, _run_integrated_fake_day_range
    from tokmo.programs.session_gate import SessionGateResult
    from tokmo.records.receipt_store import ReceiptStore
    from tokmo.records.material_pack_store import MaterialPackWriter
    from tokmo.values.session_gate import Market, SessionVerdict

    root = tmp_path / "tokmo-v3-f"
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    closed = SessionGateResult(now, (
        SessionVerdict(Market.KR, False, "after_close", None),
        SessionVerdict(Market.US, False, "after_close", None),
    ), False, False)
    packs = MaterialPackWriter(root / "packs", clock=lambda: now)
    receipts = {job: ReceiptStore(root / "receipts" / f"{job}.json", clock=lambda: now)
                for job in ("holdings", "quotes", "trades")}
    calls = {job: 0 for job in receipts}

    def holdings(*, tick, **_):
        calls["holdings"] += 1
        return run_holdings_sync(
            tick=tick, session_result=closed, holdings_provider=lambda: pytest.fail("closed"),
            store=object(), pack_writer=packs, receipt_store=receipts["holdings"],
            reservation_seal=lambda *a: None, incident_writer=lambda *a: None,
            clock=lambda: now, now=now, job_lock_path=root / "locks" / f"h-{tick.tick_id}")

    def quotes(*, tick, **_):
        calls["quotes"] += 1
        return run_quotes_sync(
            tick=tick, session_result=closed, holdings=None,
            quotes_provider=lambda *_a: pytest.fail("closed"), quote_store=lambda _v: None,
            pack_writer=packs, receipt_store=receipts["quotes"],
            reservation_seal=lambda *a: None, incident_writer=lambda *a: None,
            clock=lambda: now, now=now, job_lock_path=root / "locks" / f"q-{tick.tick_id}")

    def trades(*, tick, **_):
        calls["trades"] += 1
        return run_trades_sync(
            tick=tick, session_result=closed, holdings=None,
            orders_provider=lambda **_k: pytest.fail("closed"),
            detail_provider=lambda **_k: pytest.fail("closed"), store=object(), scan_state=object(),
            pack_writer=packs, receipt_store=receipts["trades"],
            reservation_seal=lambda *a: None, incident_writer=lambda *a: None,
            regular_session_dates=object(), clock=lambda: now, now=now,
            job_lock_path=root / "locks" / f"t-{tick.tick_id}")

    noop = lambda **_: None
    report = _run_integrated_fake_day_range(
        utc_day=now.date(), root=root,
        deps=_IntegratedStageDepsV1(noop, noop, holdings, quotes, trades, noop, noop, lambda: now))
    assert report.tick_count == 288
    assert calls == {"holdings": 288, "quotes": 288, "trades": 288}
    from tokmo.values.ops_tick import tick_id_from_utc
    last_tick = tick_id_from_utc(now + timedelta(minutes=5 * 287)).tick_id
    assert all(store.read().attempt.tick_id == last_tick for store in receipts.values())
    assert all(store.read().attempt.reason in {"skipped_market_closed", "skipped_holdings_absent"}
               for store in receipts.values())


def test_f12_scheduled_quotes_tick_closes_market_batches_through_durable_production_path(
        tmp_path, monkeypatch):
    from tokmo.facts.toss import derive_account_scope
    from tokmo.facts.toss_prices import TossPriceQueryOutcome
    from tokmo.programs.integrated_fake_cadence import _IntegratedStageDepsV1, _run_integrated_fake_day_range
    from tokmo.programs.legacy_g2 import legacy_run_quotes_sync as run_quotes_sync
    from tokmo.programs.session_gate import SessionGateResult
    from tokmo.records.latest_quotes import LatestQuoteStore
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.records.material_pack_store import MaterialPackWriter
    from tokmo.records.receipt_store import ReceiptStore
    from tokmo.values.current_holdings import (
        AccountHoldingsV1, AccountScopeIdentityV1, CurrentHoldingsV1, HoldingItemV1,
    )
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.material_pack import JobKind
    from tokmo.values.session_gate import Market, SessionVerdict

    root = tmp_path / "tokmo-v3-f"
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    scope = derive_account_scope("fixture-account")
    kr_symbols = tuple(f"KR{index:06d}" for index in range(201))
    us_symbols = ("US000001",)
    items = tuple(
        HoldingItemV1(symbol, "1", "1", "KRW", "KR", "market")
        for symbol in kr_symbols
    ) + tuple(
        HoldingItemV1(symbol, "1", "1", "USD", "US", "market")
        for symbol in us_symbols
    )
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, items),), True, now,
        {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    sessions = SessionGateResult(now, (
        SessionVerdict(Market.KR, True, "open", None),
        SessionVerdict(Market.US, True, "open", None),
    ), True, False)
    packs = MaterialPackWriter(root / "packs", clock=lambda: now)
    receipts = ReceiptStore(root / "receipts" / "quotes.json", clock=lambda: now)
    quote_store = LatestQuoteStore(root / "state" / "quotes.sqlite3", clock=lambda: now)
    provider_calls = []
    quote_results = []
    invocation = "f" * 32

    def provider(*, tick, ops_kernel, symbols):
        batch = tuple(symbols)
        provider_calls.append(batch)
        market = "KR" if batch[0].startswith("KR") else "US"
        ordinal = len(provider_calls) - 1
        params = {"market": market, "sorted_unique_symbols": batch}
        spec = REQUEST_OPERATION_SPECS["PRICE_BATCH"]
        ops_kernel.plan_requests(ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", ({
            "ordinal": ordinal, "operation": "PRICE_BATCH",
            "params_digest": canonical_params_digest("PRICE_BATCH", params),
            "method": spec[0], "host": "openapi.tossinvest.com",
            "normalized_path_template": spec[1], "account_ordinal": None,
            "market": None, "local_trade_date": None, "canonical_params": params,
        },), invocation_id=invocation, now=now)
        ops_kernel.start_request(
            ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", ordinal,
            invocation_id=invocation, now=now)
        ops_kernel.settle_request(
            ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", ordinal, "OK",
            invocation_id=invocation, now=now)
        return TossPriceQueryOutcome("success", tuple(
            TossCurrentPriceFact(
                "toss-current-price-v1", symbol, "10",
                "KRW" if market == "KR" else "USD", now, now,
            ) for symbol in batch
        ))

    first_tick = None
    def quotes(*, tick, ops_kernel, **_):
        nonlocal first_tick
        if first_tick is not None:
            return None
        first_tick = tick
        result = run_quotes_sync(
            tick=tick, session_result=sessions, holdings=holdings,
            quotes_provider=lambda symbols: provider(
                tick=tick, ops_kernel=ops_kernel, symbols=symbols),
            quote_store=lambda outcome: quote_store.save_batch(outcome.facts),
            pack_writer=packs, receipt_store=receipts,
            reservation_seal=lambda *a: None, incident_writer=lambda *a: None,
            clock=lambda: now, now=now, job_lock_path=root / "locks" / "quotes.lock")
        quote_results.append(result)
        return result

    network = []
    def forbidden(*args, **kwargs):
        network.append((args, kwargs))
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(SOCKET_TYPE, "connect", forbidden)
    noop = lambda **_: None
    report = _run_integrated_fake_day_range(
        utc_day=now.date(), root=root,
        deps=_IntegratedStageDepsV1(noop, noop, noop, quotes, noop, noop, noop, lambda: now))

    expected = (kr_symbols[:200], kr_symbols[200:], us_symbols)
    assert tuple(provider_calls) == expected
    assert [len(batch) for batch in provider_calls] == [200, 1, 1]
    rows = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3").read_planned_requests(
        first_tick.tick_id, "QUOTES")
    assert [(row["operation"], row["ordinal"], row["state"]) for row in rows] == [
        ("PRICE_BATCH", 0, "SETTLED"),
        ("PRICE_BATCH", 1, "SETTLED"),
        ("PRICE_BATCH", 2, "SETTLED"),
    ]
    assert report.per_operation == {"PRICE_BATCH": 3}
    assert report.planned_request_count == report.started_request_count == report.settled_request_count == 3
    assert len(provider_calls) == 3
    assert len(quote_results) == 1 and quote_results[0].status == "saved"
    receipt = receipts.read()
    assert receipt.attempt.tick_id == first_tick.tick_id
    assert receipt.attempt.reason == "saved"
    assert receipt.last_success.pack_path == quote_results[0].pack_path
    assert len(packs.list_packs(JobKind.QUOTES)) == 1
    assert network == [] and report.socket_count == report.dns_count == 0


def test_f12_day_total_above_3744_is_telemetry_and_operation_ceiling_still_fails_closed(tmp_path, monkeypatch):
    """G-10: the UTC-day counter survives reopen as monotonic telemetry with no
    aggregate rejection; per-operation ceilings still fail closed pre-network."""
    from tokmo.programs.legacy_g2 import legacy_run_holdings_sync as run_holdings_sync
    from tokmo.programs.session_gate import SessionGateResult
    from tokmo.records.current_holdings_store import CurrentHoldingsStore
    from tokmo.records.material_pack_store import MaterialPackWriter
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.records.receipt_store import ReceiptStore
    from tokmo.values.ops_kernel import OpsKernelBudgetExceeded, RequestEndpointV1
    from tokmo.values.ops_tick import tick_id_from_utc
    from tokmo.values.session_gate import Market, SessionVerdict

    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    endpoint = RequestEndpointV1("toss", "GET", "openapi.tossinvest.com", "/api/v1/orders")
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    kernel = OpsKernelStore(path)
    tick = tick_id_from_utc(now)
    invocation = "1" * 32
    kernel.plan_requests(ProducerRole.COLLECTOR, tick.tick_id, "HOLDINGS", ({
        "ordinal": 0, "operation": "TOKEN",
        "params_digest": canonical_params_digest("TOKEN", {}),
        "method": "POST", "host": "openapi.tossinvest.com",
        "normalized_path_template": "/oauth2/token", "account_ordinal": None,
        "market": None, "local_trade_date": None, "canonical_params": {},
    },), invocation_id=invocation, now=now)
    kernel.start_request(ProducerRole.COLLECTOR, tick.tick_id, "HOLDINGS", 0,
                         invocation_id=invocation, now=now)
    kernel.settle_request(ProducerRole.COLLECTOR, tick.tick_id, "HOLDINGS", 0,
                          "EMPTY_OK", invocation_id=invocation, now=now)
    for _ in range(3743):
        kernel.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, now)
    del kernel
    reopened = OpsKernelStore(path)
    assert reopened.read_reservation_counts(None, endpoint, now).runtime == 3744
    # Crossing the historical 3,744 total is admitted: telemetry only.
    reopened.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, now)
    assert reopened.read_reservation_counts(None, endpoint, now).runtime == 3745

    network = []
    def forbidden(*args, **kwargs):
        network.append((args, kwargs))
        raise AssertionError("network forbidden")
    monkeypatch.setattr(socket, "getaddrinfo", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(SOCKET_TYPE, "connect", forbidden)
    sessions = SessionGateResult(now, (
        SessionVerdict(Market.KR, True, "open", None),
        SessionVerdict(Market.US, False, "after_close", None),
    ), True, False)
    receipts = ReceiptStore(tmp_path / "receipts" / "holdings.json", clock=lambda: now)

    def ceiling_denied_provider():
        # A per-operation/tick structural ceiling (never the UTC-day total).
        raise OpsKernelBudgetExceeded("operation ceiling exceeded")

    result = run_holdings_sync(
        tick=tick_id_from_utc(now), session_result=sessions,
        holdings_provider=ceiling_denied_provider,
        store=CurrentHoldingsStore(tmp_path / "state" / "holdings.json", clock=lambda: now),
        pack_writer=MaterialPackWriter(tmp_path / "packs", clock=lambda: now),
        receipt_store=receipts, reservation_seal=lambda *a: None,
        incident_writer=lambda *a: None, clock=lambda: now, now=now,
        job_lock_path=tmp_path / "locks" / "holdings.lock")
    assert result.reason == "failed_ceiling"
    assert receipts.read().attempt.reason == "failed_ceiling"
    assert network == []
    assert reopened.read_reservation_counts(None, endpoint, now).runtime == 3745
    connection = sqlite3.connect(path)
    try:
        get_head_audit = connection.execute(
            "SELECT COALESCE(SUM(started_count),0) FROM runtime_request_reservations"
        ).fetchone()[0]
        starts = connection.execute(
            "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
            (now.date().isoformat(),)).fetchone()[0]
    finally:
        connection.close()
    assert starts == 3745
    assert get_head_audit == starts - 1  # TOKEN is a start, never a GET/HEAD audit row.


def test_f13_split_reopen_has_exact_durable_equivalence_and_replay_is_zero(tmp_path):
    from tokmo.facts.toss import derive_account_scope
    from tokmo.facts.toss_prices import TossPriceQueryOutcome
    from tokmo.programs.integrated_fake_cadence import _IntegratedStageDepsV1, _run_integrated_fake_day_range
    from tokmo.programs.legacy_g2 import legacy_run_quotes_sync as run_quotes_sync
    from tokmo.programs.session_gate import SessionGateResult
    from tokmo.records.latest_quotes import LatestQuoteStore
    from tokmo.records.material_pack_store import MaterialPackWriter
    from tokmo.records.receipt_store import ReceiptStore
    from tokmo.values.current_holdings import (
        AccountHoldingsV1, AccountScopeIdentityV1, CurrentHoldingsV1, HoldingItemV1,
    )
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.material_pack import JobKind
    from tokmo.values.ops_kernel import ReplayResult
    from tokmo.values.session_gate import Market, SessionVerdict

    day = date(2026, 7, 30)
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    scope = derive_account_scope("f13-fixture-account")
    holdings = CurrentHoldingsV1(
        1, (AccountHoldingsV1(scope, 1, (
            HoldingItemV1("KR000001", "1", "1", "KRW", "KR", "market"),
        )),), True, now, {scope: AccountScopeIdentityV1(scope, None, 1, None)},
    )
    sessions = SessionGateResult(now, (
        SessionVerdict(Market.KR, True, "open", None),
        SessionVerdict(Market.US, False, "after_close", None),
    ), True, False)

    def fresh_deps(root):
        provider_calls, storage_calls = [], []
        packs = MaterialPackWriter(root / "materials", clock=lambda: now)
        receipt = ReceiptStore(root / "receipts" / "quotes.json", clock=lambda: now)
        quote_store = LatestQuoteStore(root / "state" / "quotes.sqlite3", clock=lambda: now)

        def calendar(*, tick, ops_kernel, **_):
            if tick.tick_id != int(datetime(2026, 7, 30, tzinfo=timezone.utc).timestamp() // 300):
                return
            params = {"market": "KR", "local_trade_date": day.isoformat()}
            spec = REQUEST_OPERATION_SPECS["CALENDAR_DAY_KR"]
            row = {"ordinal": 0, "operation": "CALENDAR_DAY_KR",
                   "params_digest": canonical_params_digest("CALENDAR_DAY_KR", params),
                   "method": spec[0], "host": "openapi.tossinvest.com",
                   "normalized_path_template": spec[1], "account_ordinal": None,
                   "market": "KR", "local_trade_date": day.isoformat(),
                   "canonical_params": params}
            invocation = f"{tick.tick_id:032x}"
            assert ops_kernel.plan_requests(ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", (row,),
                                            invocation_id=invocation, now=now) is ReplayResult.CREATED
            ops_kernel.start_request(ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", 0,
                                     invocation_id=invocation, now=now)
            ops_kernel.settle_request(ProducerRole.COLLECTOR, tick.tick_id, "CALENDAR", 0, "OK",
                                      invocation_id=invocation, now=now)

        def quotes(*, tick, ops_kernel, counters, **_):
            invocation = f"{tick.tick_id + 1:032x}"
            params = {"market": "KR", "sorted_unique_symbols": ("KR000001",)}
            spec = REQUEST_OPERATION_SPECS["PRICE_BATCH"]

            def provider(symbols):
                identity = (tick.tick_id, tuple(symbols))
                provider_calls.append(identity)
                row = {"ordinal": 0, "operation": "PRICE_BATCH",
                       "params_digest": canonical_params_digest("PRICE_BATCH", params),
                       "method": spec[0], "host": "openapi.tossinvest.com",
                       "normalized_path_template": spec[1], "account_ordinal": None,
                       "market": None, "local_trade_date": None, "canonical_params": params}
                ops_kernel.plan_requests(ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", (row,),
                                         invocation_id=invocation, now=now)
                ops_kernel.start_request(ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", 0,
                                         invocation_id=invocation, now=now)
                ops_kernel.settle_request(ProducerRole.COLLECTOR, tick.tick_id, "QUOTES", 0, "OK",
                                          invocation_id=invocation, now=now)
                return TossPriceQueryOutcome("success", (
                    TossCurrentPriceFact("toss-current-price-v1", "KR000001", "10", "KRW", now, now),
                ))

            def save(outcome):
                storage_calls.append((tick.tick_id, tuple(f.symbol for f in outcome.facts)))
                return quote_store.save_batch(outcome.facts)

            result = run_quotes_sync(
                tick=tick, session_result=sessions, holdings=holdings, quotes_provider=provider,
                quote_store=save, pack_writer=packs, receipt_store=receipt,
                reservation_seal=lambda *a: None, incident_writer=lambda *a: None,
                clock=lambda: now, now=now, job_lock_path=root / "locks" / "quotes.lock")
            assert result.status == "saved", result
            counters.add_logical_slots(1)

        noop = lambda **_: None
        return (_IntegratedStageDepsV1(calendar, noop, noop, quotes, noop, noop, noop, lambda: now),
                provider_calls, storage_calls)

    def snapshot(root, provider_calls, storage_calls):
        db = sqlite3.connect(root / "ops" / "ops_kernel.sqlite3")
        db.row_factory = sqlite3.Row
        def rows(table):
            return tuple(tuple(row) for row in db.execute(f"SELECT * FROM {table} ORDER BY 1,2,3"))
        planned = tuple(dict(row) for row in db.execute(
            "SELECT tick_id,job_scope,ordinal,operation,params_digest,state,exit "
            "FROM planned_requests ORDER BY tick_id,job_scope,ordinal"))
        result = {
            "request_day_counters": rows("request_day_counters"),
            "calendar_request_counters": rows("calendar_request_counters"),
            "planned_identity_state_exit": tuple(
                (r["tick_id"], r["job_scope"], r["ordinal"], r["operation"],
                 r["params_digest"], r["state"], r["exit"]) for r in planned),
            "planned_state_exit_identity_counts": tuple(db.execute(
                "SELECT state,exit,operation,count(*) FROM planned_requests "
                "GROUP BY state,exit,operation ORDER BY state,exit,operation")),
            "planned_request_pages": rows("planned_request_pages"),
            "planned_request_pages_count": db.execute(
                "SELECT count(*) FROM planned_request_pages").fetchone()[0],
            "runtime_audit_counts": tuple(db.execute(
                "SELECT runtime_day,source,method,host,normalized_path_template,started_count "
                "FROM runtime_request_reservations ORDER BY 1,2,3,4,5")),
        }
        db.close()
        receipt = ReceiptStore(root / "receipts" / "quotes.json", clock=lambda: now).read()
        assert receipt is not None
        result["receipt"] = (
            receipt.attempt.tick_id, receipt.attempt.status.value, receipt.attempt.reason,
            None if receipt.last_success is None else (
                receipt.last_success.tick_id,
                Path(receipt.last_success.pack_path).relative_to(root).as_posix(),
                receipt.last_success.pack_sha256,
            ),
        )
        writer = MaterialPackWriter(root / "materials", clock=lambda: now)
        result["packs"] = tuple(
            (tick, path.relative_to(root).as_posix(), digest)
            for tick, path, digest in writer.list_packs(JobKind.QUOTES))
        result["provider_calls"] = tuple(provider_calls)
        result["provider_call_count"] = len(provider_calls)
        result["storage_calls"] = tuple(storage_calls)
        result["storage_call_count"] = len(storage_calls)
        return result

    full_root, split_root = tmp_path / "full", tmp_path / "split"
    deps, full_provider, full_storage = fresh_deps(full_root)
    _run_integrated_fake_day_range(utc_day=day, root=full_root, deps=deps, start_tick=0, end_tick=288)

    deps, first_provider, first_storage = fresh_deps(split_root)
    _run_integrated_fake_day_range(utc_day=day, root=split_root, deps=deps, start_tick=0, end_tick=144)
    del deps
    deps, second_provider, second_storage = fresh_deps(split_root)
    _run_integrated_fake_day_range(utc_day=day, root=split_root, deps=deps, start_tick=144, end_tick=288)
    provider_calls = first_provider + second_provider
    storage_calls = first_storage + second_storage
    before = snapshot(split_root, provider_calls, storage_calls)

    assert snapshot(full_root, full_provider, full_storage) == before
    deps, replay_provider, replay_storage = fresh_deps(split_root)
    replay = _run_integrated_fake_day_range(
        utc_day=day, root=split_root, deps=deps, start_tick=0, end_tick=144)
    assert replay.tick_count == replay.logical_slots == 0
    assert replay_provider == replay_storage == []
    assert snapshot(split_root, provider_calls, storage_calls) == before


def test_f13_day_lock_denies_second_runner_before_stage_or_store_mutation(tmp_path):
    from tokmo.programs.integrated_fake_cadence import (
        _IntegratedStageDepsV1, IntegratedCadenceLockedError, _run_integrated_fake_day_range,
    )

    root = tmp_path / "locked"
    day = date(2026, 7, 30)
    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    inner_calls = []
    noop = lambda **_: None
    inner = _IntegratedStageDepsV1(
        lambda **kw: inner_calls.append(kw), noop, noop, noop, noop, noop, noop, lambda: now)

    def contend(**_):
        with pytest.raises(IntegratedCadenceLockedError, match="integrated cadence locked"):
            _run_integrated_fake_day_range(
                utc_day=day, root=root, deps=inner, start_tick=0, end_tick=1)
        assert inner_calls == []

    outer = _IntegratedStageDepsV1(contend, noop, noop, noop, noop, noop, noop, lambda: now)
    _run_integrated_fake_day_range(utc_day=day, root=root, deps=outer, start_tick=0, end_tick=1)
    assert inner_calls == []
    assert (root / "ops" / "locks" / "integrated.lock").exists()


def test_f13_reports_instrumented_logical_socket_and_dns_counters(tmp_path):
    from tokmo.programs.integrated_fake_cadence import _IntegratedStageDepsV1, _run_integrated_fake_day_range

    now = datetime(2026, 7, 30, tzinfo=timezone.utc)
    def measured(*, counters, **_):
        counters.add_logical_slots(3)
        counters.add_dns_count()
        counters.add_socket_count()

    noop = lambda **_: None
    deps = _IntegratedStageDepsV1(measured, noop, noop, noop, noop, noop, noop, lambda: now)
    report = _run_integrated_fake_day_range(
        utc_day=now.date(), root=tmp_path / "counts", deps=deps, start_tick=0, end_tick=1)
    assert (report.logical_slots, report.dns_count, report.socket_count) == (3, 1, 1)
