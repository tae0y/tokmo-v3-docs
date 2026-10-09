"""Plan §10 TDD matrix for the recurring Market Regime portfolio drain.

T-E1..T-E9 against the real operations kernel, wire executor, lazy token,
and claim store — fakes only at the provider boundary; no live API. The
19-symbol fixture mirrors production shape: KR 12 / US 7 with one
cross-account duplicate and two active watchlist symbols.
"""

import sqlite3
from contextlib import closing
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

import tokmo.programs.market_regime_shadow as shadow_module
from tokmo.facts.toss import derive_account_scope
from tokmo.facts.toss_reads import TossReadOutcome
from tokmo.programs.cadence_wire import CadenceWireExecutorV1, LazyWireTokenV1
from tokmo.programs.market_regime_shadow import (
    read_market_regime_coverage,
    run_market_regime_shadow,
)
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.cadence_policy import (
    CADENCE_CORE_SHADOW_POLICY_V1,
    CADENCE_MARKET_PORTFOLIO_POLICY_V1,
    CADENCE_POLICY_REGISTRY,
    CADENCE_POLICY_V1,
    CADENCE_TARGETED_ONESHOT_POLICY_V1,
    TARGETED_ONESHOT_POLICY_REGISTRY,
    TradesChildLimitsV1,
    cadence_tick_v1,
    policy_digest,
    policy_snapshot,
)
from tokmo.values.current_holdings import (
    AccountHoldingsV1,
    AccountScopeIdentityV1,
    CurrentHoldingsV1,
    HoldingItemV1,
)
from tokmo.values.operation_lane_registry import (
    OPERATION_LANE_REGISTRY_V1,
    OperationLane,
)
from tokmo.values.ops_kernel import ProducerRole
from tokmo.values.session_gate import Market
from tokmo.values.toss_market_data import Candle, CandlePageResponse

POLICY = CADENCE_MARKET_PORTFOLIO_POLICY_V1
POLICY_ID = "cadence-300s-market-portfolio-v1"
SLOT = datetime(2026, 8, 3, 10, 0, tzinfo=UTC)  # 19:00 KST Monday

KR_PERSONAL = ("000660", "005380", "069500", "329180", "442580")
KR_PENSION = (
    "0008T0",
    "0162Z0",
    "0167A0",
    "0204D0",
    "269540",
    "490090",
    "069500",
)  # 069500 duplicated across accounts
US_PERSONAL = ("GOOGL", "MSFT", "NVDA", "PEP", "SPCX", "TSLA")
WATCHLIST = {Market.KR: frozenset({"005930"}), Market.US: frozenset({"V"})}

KR_ALL = tuple(sorted(set(KR_PERSONAL) | set(KR_PENSION) | {"005930"}))
US_ALL = tuple(sorted(set(US_PERSONAL) | {"V"}))
ORDERED = tuple((Market.KR, s) for s in KR_ALL) + tuple((Market.US, s) for s in US_ALL)
assert (len(KR_ALL), len(US_ALL)) == (12, 7)

KR_SESSION = date(2026, 8, 3)
US_SESSION = date(2026, 7, 31)  # 08-03 US regular closes after SLOT; walk back


def _clock():
    return SLOT + timedelta(seconds=1)


class _HistoryAuthority:
    def read_settled_calendar_provenance(self, market, local_date):
        return {
            "native_identity": f"calendar:{market.value}:{local_date.isoformat()}:0"
        }


def _seed_history(root):
    store = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=_clock,
    )
    authority = _HistoryAuthority()
    for market in Market:
        for offset in range(21):
            day = SLOT.date() - timedelta(days=offset)
            session = "regular" if day.weekday() < 5 else "no_regular"
            store.append_settled(market, day, session, authority=authority)


def _write_holdings(root, *, extra_kr_raw=None):
    def items(symbols, market_raw):
        return tuple(
            HoldingItemV1(symbol, "1", "100", "KRW", market_raw, None)
            for symbol in symbols
        )

    personal_scope = derive_account_scope("11111")
    pension_scope = derive_account_scope("22222")
    kr_items = items(KR_PERSONAL, "KR") + items(US_PERSONAL, "US")
    if extra_kr_raw is not None:
        kr_items += (HoldingItemV1("999999", "1", "100", "KRW", extra_kr_raw, None),)
    accounts = (
        AccountHoldingsV1(personal_scope, 1, kr_items),
        AccountHoldingsV1(pension_scope, 2, items(KR_PENSION, "KR")),
    )
    identity_map = {
        personal_scope: AccountScopeIdentityV1(personal_scope, "brokerage", 1, None),
        pension_scope: AccountScopeIdentityV1(pension_scope, "pension", 2, None),
    }
    CurrentHoldingsStore(
        root / "stores" / "current_holdings.json",
        clock=_clock,
    ).replace(
        CurrentHoldingsV1(1, accounts, True, SLOT, identity_map),
        identity_fingerprints={personal_scope: "a" * 64, pension_scope: "b" * 64},
    )


def _cache_store(*, kr=True, us=True, us_trade_date="2026-08-03"):
    kr_entry = (
        SimpleNamespace(
            trade_date="2026-08-03",
            sessions={"regularMarket": SimpleNamespace(end="2026-08-03T06:30:00Z")},
        )
        if kr
        else None
    )
    us_entry = (
        SimpleNamespace(
            trade_date=us_trade_date,
            sessions={"regularMarket": SimpleNamespace(end="2026-08-03T20:00:00Z")},
        )
        if us
        else None
    )
    return SimpleNamespace(read=lambda: SimpleNamespace(kr=kr_entry, us=us_entry))


def _candles():
    return tuple(
        Candle(
            SLOT - timedelta(days=offset),
            "10",
            "12",
            "9",
            "11",
            "100",
            "KRW",
        )
        for offset in range(200)
    )


class _CandleProvider:
    def __init__(self, failing_symbols=frozenset()):
        self.failing = frozenset(failing_symbols)
        self.requests = []

    def __call__(self, *, claim, bearer, symbol, before, adjusted):
        self.requests.append(symbol)
        if symbol in self.failing:
            return TossReadOutcome("rate_limited")
        candles = tuple(
            Candle(
                before - timedelta(days=offset),
                "10",
                "12",
                "9",
                "11",
                "100",
                "KRW",
            )
            for offset in range(200)
        )
        return TossReadOutcome("success", CandlePageResponse(candles, None))


class _TokenFetch:
    def __init__(self, fail=False):
        self.calls = 0
        self.fail = fail

    def __call__(self, claim):
        self.calls += 1
        if self.fail:
            raise ValueError("token backend down")
        return "test-bearer"


class _SpyExecutor:
    """Delegating spy: records every admission *attempt* by operation."""

    def __init__(self, real):
        self._real = real
        self.attempts = []

    def execute(self, operation, native_identity, callback):
        self.attempts.append((operation, native_identity))
        return self._real.execute(operation, native_identity, callback)


def _deps(provider):
    return SimpleNamespace(
        clock=_clock,
        stock_candles_fetch=provider,
        watchlist_symbols=WATCHLIST,
        pension_unquotable_symbols=frozenset(),
    )


def _tick(
    root,
    *,
    slot,
    provider,
    token_fetch=None,
    cache=None,
    invocation_id=None,
    prepare=None,
):
    kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    executor = _SpyExecutor(
        CadenceWireExecutorV1(
            ops_kernel=kernel,
            role=ProducerRole.COLLECTOR,
            policy=POLICY,
            scheduled_for_utc=slot,
            clock=lambda: slot + timedelta(seconds=1),
            child_limits=TradesChildLimitsV1(),
            lane_registry=OPERATION_LANE_REGISTRY_V1,
            lane=OperationLane.CADENCE_300S,
        )
    )
    token_fetch = token_fetch if token_fetch is not None else _TokenFetch()
    token = LazyWireTokenV1(executor=executor._real, token_fetch=token_fetch)
    if prepare is not None:
        prepare(executor._real, token)
    status, reason = run_market_regime_shadow(
        root=root,
        deps=_deps(provider),
        policy=POLICY,
        tick=SimpleNamespace(scheduled_for_utc=slot),
        executor=executor,
        token=token,
        cache_store=cache or _cache_store(),
        invocation_id=invocation_id or f"drain-{slot.isoformat()}",
    )
    return status, reason, kernel, executor, token_fetch


def _root(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    _write_holdings(root)
    return root


def _claim_rows(root):
    path = root / "ops" / "market_regime_shadow.sqlite3"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path)) as connection:
        return connection.execute(
            "SELECT market,symbol,state,failure_code,completed_session_date "
            "FROM market_regime_shadow_claim ORDER BY market,symbol",
        ).fetchall()


def _wire_report(kernel, slot):
    report = kernel.read_cadence_tick_report(POLICY_ID, slot)
    if report is None:
        return None
    return (report["total_started"], dict(report["operation_started"]))


def _identity_for(market, symbol, completed):
    from tokmo.facts.market_regime_toss_input import candle_request_identity
    from tokmo.values.market_regime_core import AdjustmentMode

    request_identity = candle_request_identity(
        lane="mr",
        market=market,
        symbol=symbol,
        completed_session=completed,
        adjusted=True,
    )
    return shadow_module._identity_digest(
        policy_version=POLICY_ID,
        market=market,
        symbol=symbol,
        completed_session=completed,
        adjustment_mode=AdjustmentMode.ADJUSTED,
        request_identity=request_identity,
    )


# --- T-E1: cap removal — tick 1 admits exactly k=3, deterministic order ----


def test_te1_first_tick_admits_exactly_k_in_deterministic_order(tmp_path):
    assert (
        POLICY.request_limits.market_daily_candle_starts,
        POLICY.request_limits.quant_daily_candle_starts,
    ) == (3, 0)
    root = _root(tmp_path)
    provider = _CandleProvider()
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=SLOT, provider=provider
    )
    assert (status, reason) == ("ok", "kr_3_of_12_us_0_of_7_pending_16")
    assert provider.requests == list(KR_ALL[:3])
    assert token_fetch.calls == 1
    # Exactly three candle attempts through the stage's executor: the k
    # ceiling stops the drain before a fourth admission is even tried. (The
    # shared TOKEN wire is admitted through the lazy token's own executor
    # binding and shows up in the kernel report below.)
    assert executor.attempts == [
        ("STOCK_CANDLES_DAILY", f"candles:mr:KR:{symbol}:2026-08-03:1")
        for symbol in KR_ALL[:3]
    ]
    assert _wire_report(kernel, SLOT) == (4, {"TOKEN": 1, "STOCK_CANDLES_DAILY": 3})
    assert [row[:3] for row in _claim_rows(root)] == [
        ("KR", symbol, "SUCCEEDED") for symbol in KR_ALL[:3]
    ]


# --- T-E2: 7-tick full drain, 19 candles total, TOKEN <=1 per tick --------


def test_te2_consecutive_ticks_drain_all_nineteen_symbols(tmp_path):
    root = _root(tmp_path)
    outcomes = []
    for index in range(7):
        slot = SLOT + timedelta(minutes=5 * index)
        provider = _CandleProvider()
        status, reason, kernel, executor, token_fetch = _tick(
            root, slot=slot, provider=provider
        )
        outcomes.append((status, len(provider.requests), token_fetch.calls))
    assert outcomes == [("ok", 3, 1)] * 6 + [("ok", 1, 1)]
    rows = _claim_rows(root)
    assert len(rows) == 19
    assert all(row[2] == "SUCCEEDED" for row in rows)
    assert {row[0] for row in rows} == {"KR", "US"}
    kr_rows = [row for row in rows if row[0] == "KR"]
    us_rows = [row for row in rows if row[0] == "US"]
    assert [row[1] for row in kr_rows] == list(KR_ALL)
    assert [row[1] for row in us_rows] == list(US_ALL)
    assert all(row[4] == "2026-08-03" for row in kr_rows)
    assert all(row[4] == "2026-07-31" for row in us_rows)
    # KR drains fully before any US symbol: per-market completed sessions.
    kernel = OpsKernelStore(root / "ops" / "ops_kernel.sqlite3")
    total = 0
    for index in range(7):
        report = kernel.read_cadence_tick_report(
            POLICY_ID, SLOT + timedelta(minutes=5 * index)
        )
        assert report["operation_started"]["TOKEN"] == 1
        total += report["operation_started"]["STOCK_CANDLES_DAILY"]
    assert total == 19


# --- T-E3: per-symbol provider failure isolates; drain continues ----------


def test_te3_provider_failure_is_isolated_and_next_symbols_continue(tmp_path):
    root = _root(tmp_path)
    provider = _CandleProvider(failing_symbols={KR_ALL[1]})
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=SLOT, provider=provider
    )
    assert status == "failed"
    assert reason == "kr_3_of_12_us_0_of_7_pending_16"
    # The symbol after the failure was still requested in the same tick.
    assert provider.requests == list(KR_ALL[:3])
    rows = {row[1]: row for row in _claim_rows(root)}
    assert rows[KR_ALL[0]][2] == "SUCCEEDED"
    assert rows[KR_ALL[1]][2:4] == ("FAILED", "ValueError")
    assert rows[KR_ALL[2]][2] == "SUCCEEDED"
    # No symbol appears in the bounded stage reason.
    assert all(symbol not in reason for symbol in KR_ALL + US_ALL)
    # Next tick resumes at the fourth symbol; the FAILED identity stays
    # terminal with zero wire.
    provider_2 = _CandleProvider()
    status_2, reason_2, *_ = _tick(
        root, slot=SLOT + timedelta(minutes=5), provider=provider_2
    )
    assert (status_2, reason_2) == ("ok", "kr_6_of_12_us_0_of_7_pending_13")
    assert provider_2.requests == list(KR_ALL[3:6])


# --- T-E4: token failure contaminates no symbol identity (F6) -------------


def test_te4_token_failure_fails_stage_with_zero_symbol_identities(tmp_path):
    root = _root(tmp_path)
    provider = _CandleProvider()
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=SLOT, provider=provider, token_fetch=_TokenFetch(fail=True)
    )
    assert (status, reason) == ("failed", "token_unavailable")
    assert provider.requests == []
    assert token_fetch.calls == 1
    assert _claim_rows(root) == []
    # The TOKEN wire itself settled FAILED; no candle wire ever started.
    report = kernel.read_cadence_tick_report(POLICY_ID, SLOT)
    assert report["operation_started"] == {"TOKEN": 1}
    assert report["state_counts"] == {"FAILED": 1}


# --- T-E5: full coverage -> all-wire-0 same-session skip, no recompute ----


def test_te5_completed_session_reuse_is_zero_wire_and_zero_recompute(
    tmp_path, monkeypatch
):
    root = _root(tmp_path)
    for index in range(7):
        _tick(
            root, slot=SLOT + timedelta(minutes=5 * index), provider=_CandleProvider()
        )
    counts = {"normalize": 0, "core": 0}
    real_normalize = shadow_module.normalize_daily_candles
    real_core = shadow_module.evaluate_market_regime_core_v1
    monkeypatch.setattr(
        shadow_module,
        "normalize_daily_candles",
        lambda *a, **k: (
            counts.__setitem__("normalize", counts["normalize"] + 1)
            or real_normalize(*a, **k)
        ),
    )
    monkeypatch.setattr(
        shadow_module,
        "evaluate_market_regime_core_v1",
        lambda series: (
            counts.__setitem__("core", counts["core"] + 1) or real_core(series)
        ),
    )
    provider = _CandleProvider()
    reuse_slot = SLOT + timedelta(minutes=35)
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=reuse_slot, provider=provider
    )
    assert (status, reason) == ("skipped", "same_completed_session")
    assert provider.requests == []
    assert token_fetch.calls == 0
    assert executor.attempts == []
    assert _wire_report(kernel, reuse_slot) is None
    assert counts == {"normalize": 0, "core": 0}
    assert len(_claim_rows(root)) == 19


# --- T-E6: one unknown market_raw fails the whole stage closed, wire 0 ----


def test_te6_unknown_market_raw_fails_whole_stage_with_zero_wire(tmp_path):
    root = tmp_path / "root"
    _seed_history(root)
    _write_holdings(root, extra_kr_raw="XX")
    provider = _CandleProvider()
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=SLOT, provider=provider
    )
    assert (status, reason) == ("failed", "unknown_target")
    assert provider.requests == []
    assert token_fetch.calls == 0
    assert executor.attempts == []
    assert _wire_report(kernel, SLOT) is None
    assert _claim_rows(root) == []


# --- T-E7: per-market session isolation ----------------------------------


def test_te7_one_market_resolver_failure_skips_only_that_market(tmp_path):
    root = _root(tmp_path)
    # US calendar cache is stale: US session unresolvable, KR drains.
    stale = _cache_store(us_trade_date="2026-08-01")
    provider = _CandleProvider()
    status, reason, kernel, executor, token_fetch = _tick(
        root, slot=SLOT, provider=provider, cache=stale
    )
    assert (status, reason) == ("ok", "kr_3_of_12_us_0_of_7_pending_9")
    assert provider.requests == list(KR_ALL[:3])
    assert all(row[0] == "KR" for row in _claim_rows(root))
    # Both markets unresolvable: zero-wire skip.
    both = _cache_store(kr=False, us_trade_date="2026-08-01")
    provider_2 = _CandleProvider()
    status_2, reason_2, kernel_2, executor_2, token_2 = _tick(
        root, slot=SLOT + timedelta(minutes=5), provider=provider_2, cache=both
    )
    assert (status_2, reason_2) == ("skipped", "completed_session_unavailable")
    assert provider_2.requests == []
    assert token_2.calls == 0
    assert executor_2.attempts == []


# --- T-E8: registry/digest reflection; targeted seam stays separate -------


def test_te8_policy_is_additive_and_recurring_rejects_targeted(tmp_path):
    limits = POLICY.request_limits
    assert POLICY.policy_version == POLICY_ID
    assert (
        limits.total_wire_starts,
        limits.daily_candle_starts,
        limits.market_daily_candle_starts,
        limits.quant_daily_candle_starts,
    ) == (30, 3, 3, 0)
    v1 = CADENCE_POLICY_V1.request_limits
    assert (
        limits.token_starts,
        limits.calendar_starts,
        limits.holdings_starts,
        limits.quote_starts,
        limits.trades_starts,
    ) == (
        v1.token_starts,
        v1.calendar_starts,
        v1.holdings_starts,
        v1.quote_starts,
        v1.trades_starts,
    )
    assert (v1.total_wire_starts, v1.daily_candle_starts) == (27, 0)
    shadow = CADENCE_CORE_SHADOW_POLICY_V1.request_limits
    assert (
        shadow.total_wire_starts,
        shadow.daily_candle_starts,
        shadow.market_daily_candle_starts,
        shadow.quant_daily_candle_starts,
    ) == (44, 17, 17, 0)
    assert set(CADENCE_POLICY_REGISTRY) == {
        "cadence-300s-v1",
        "cadence-300s-core-shadow-v1",
        POLICY_ID,
        "cadence-300s-market-quant4-shadow-v1",
        "cadence-300s-quant-profiles-shadow-v1",
    }
    assert CADENCE_POLICY_REGISTRY[POLICY_ID] is POLICY
    snapshot = policy_snapshot(POLICY, TradesChildLimitsV1())
    assert snapshot["request_limits"]["daily_candle_starts"] == 3
    assert snapshot["request_limits"]["total_wire_starts"] == 30
    digests = {
        policy_digest(entry, TradesChildLimitsV1())
        for entry in (CADENCE_POLICY_V1, CADENCE_CORE_SHADOW_POLICY_V1, POLICY)
    }
    assert len(digests) == 3
    tick = cadence_tick_v1(POLICY_ID, SLOT)
    assert tick.policy_version == POLICY_ID
    # Targeted one-shot registry unchanged and still mutually exclusive.
    assert dict(TARGETED_ONESHOT_POLICY_REGISTRY) == {
        "cadence-300s-targeted-oneshot-v1": CADENCE_TARGETED_ONESHOT_POLICY_V1
    }
    with pytest.raises(ValueError, match="unknown cadence policy version"):
        cadence_tick_v1("cadence-300s-targeted-oneshot-v1", SLOT)


# --- T-E9: budget exhaustion is a normal termination ----------------------


def test_te9_budget_exhaustion_terminates_normally_and_next_tick_inherits(tmp_path):
    assert POLICY.request_limits.market_daily_candle_starts == 3
    root = _root(tmp_path)

    def consume_candles(executor, token, count):
        for index in range(count):
            executor.execute(
                "STOCK_CANDLES_DAILY",
                f"candles:mr:KR:PRE{index}:2026-08-03:1",
                lambda claim: None,
            )

    # Whole daily budget pre-consumed in-tick: zero drain progress, clean skip.
    provider = _CandleProvider()
    status, reason, kernel, executor, token_fetch = _tick(
        root,
        slot=SLOT,
        provider=provider,
        prepare=lambda executor, token: consume_candles(executor, token, 3),
    )
    assert (status, reason) == ("skipped", "admission_budget_exhausted")
    assert provider.requests == []
    assert _claim_rows(root) == []
    # Partially consumed budget: one symbol progresses, then normal stop.
    provider_2 = _CandleProvider()
    status_2, reason_2, *_ = _tick(
        root,
        slot=SLOT + timedelta(minutes=5),
        provider=provider_2,
        prepare=lambda executor, token: consume_candles(executor, token, 2),
    )
    assert (status_2, reason_2) == ("ok", "kr_1_of_12_us_0_of_7_pending_18")
    assert provider_2.requests == [KR_ALL[0]]
    # Next tick inherits the remainder with the full budget.
    provider_3 = _CandleProvider()
    status_3, reason_3, *_ = _tick(
        root, slot=SLOT + timedelta(minutes=10), provider=provider_3
    )
    assert (status_3, reason_3) == ("ok", "kr_4_of_12_us_0_of_7_pending_15")
    assert provider_3.requests == list(KR_ALL[1:4])


# --- restart/in-progress: a live foreign owner is skipped without wire ----


def test_live_foreign_owner_symbol_is_skipped_and_drain_continues(tmp_path):
    from tokmo.records.market_regime_shadow_state import (
        MarketRegimeShadowStateStore,
    )
    from tokmo.values.market_regime_core import AdjustmentMode

    root = _root(tmp_path)
    identity = _identity_for(Market.KR, KR_ALL[0], KR_SESSION)
    store = MarketRegimeShadowStateStore(root / "ops" / "market_regime_shadow.sqlite3")
    lease = store.acquire_identity_lease(identity)
    assert lease is not None
    try:
        store.claim(
            identity_digest=identity,
            program_id="market-regime-core-v1",
            spec_digest="b" * 64,
            policy_version=POLICY_ID,
            market=Market.KR,
            symbol=KR_ALL[0],
            completed_session_date=KR_SESSION,
            adjustment_mode=AdjustmentMode.ADJUSTED,
            request_identity="c" * 64,
            owner_invocation_id="foreign",
            owner_started_at=_clock(),
            owner_lease_key=lease.key,
            identity_lease=lease,
        )
        provider = _CandleProvider()
        status, reason, *_ = _tick(root, slot=SLOT, provider=provider)
    finally:
        lease.close()
    assert (status, reason) == ("ok", "kr_3_of_12_us_0_of_7_pending_15")
    # The in-progress symbol was never admitted; the next three drained.
    assert provider.requests == list(KR_ALL[1:4])


# --- coverage/readiness surface (§8) --------------------------------------


def test_coverage_surface_counts_and_ready_transition(tmp_path):
    root = _root(tmp_path)
    provider = _CandleProvider(failing_symbols={KR_ALL[1]})
    _tick(root, slot=SLOT, provider=provider)
    coverage = read_market_regime_coverage(
        root=root,
        deps=_deps(provider),
        policy_version=POLICY_ID,
        observed_now_utc=SLOT,
        cache_store=_cache_store(),
    )
    assert coverage["policy_version"] == POLICY_ID
    kr = coverage["markets"]["KR"]
    assert kr == {
        "admissible": 12,
        "completed_session": "2026-08-03",
        "terminal": 3,
        "succeeded": 2,
        "failed": 1,
        "in_progress": 0,
        "pending": 9,
        "ready": False,
    }
    us = coverage["markets"]["US"]
    assert us == {
        "admissible": 7,
        "completed_session": "2026-07-31",
        "terminal": 0,
        "succeeded": 0,
        "failed": 0,
        "in_progress": 0,
        "pending": 7,
        "ready": False,
    }
    # Symbols never leak into the coverage surface.
    assert not any(symbol in str(coverage) for symbol in KR_ALL + US_ALL)
    for index in range(1, 7):
        _tick(
            root, slot=SLOT + timedelta(minutes=5 * index), provider=_CandleProvider()
        )
    done = read_market_regime_coverage(
        root=root,
        deps=_deps(provider),
        policy_version=POLICY_ID,
        observed_now_utc=SLOT,
        cache_store=_cache_store(),
    )
    # FAILED symbols are terminal coverage: READY with succeeded >= 1.
    assert done["markets"]["KR"] == {
        "admissible": 12,
        "completed_session": "2026-08-03",
        "terminal": 12,
        "succeeded": 11,
        "failed": 1,
        "in_progress": 0,
        "pending": 0,
        "ready": True,
    }
    assert done["markets"]["US"] == {
        "admissible": 7,
        "completed_session": "2026-07-31",
        "terminal": 7,
        "succeeded": 7,
        "failed": 0,
        "in_progress": 0,
        "pending": 0,
        "ready": True,
    }


def test_coverage_all_terminal_failed_is_not_ready(tmp_path):
    root = _root(tmp_path)
    failing = frozenset(KR_ALL + US_ALL)
    for index in range(7):
        _tick(
            root,
            slot=SLOT + timedelta(minutes=5 * index),
            provider=_CandleProvider(failing_symbols=failing),
        )
    coverage = read_market_regime_coverage(
        root=root,
        deps=_deps(_CandleProvider()),
        policy_version=POLICY_ID,
        observed_now_utc=SLOT,
        cache_store=_cache_store(),
    )
    for market, admissible in (("KR", 12), ("US", 7)):
        row = coverage["markets"][market]
        assert row["admissible"] == admissible
        assert row["terminal"] == admissible
        assert row["failed"] == admissible
        assert row["succeeded"] == 0
        assert row["pending"] == 0
        assert row["ready"] is False


def test_coverage_surface_with_unresolvable_market_and_no_receipts(tmp_path):
    root = _root(tmp_path)
    provider = _CandleProvider()
    coverage = read_market_regime_coverage(
        root=root,
        deps=_deps(provider),
        policy_version=POLICY_ID,
        observed_now_utc=SLOT,
        cache_store=_cache_store(us_trade_date="2026-08-01"),
    )
    assert coverage["last_stage"] is None
    assert coverage["markets"]["US"] == {
        "admissible": 7,
        "completed_session": None,
        "terminal": None,
        "succeeded": None,
        "failed": None,
        "in_progress": None,
        "pending": None,
        "ready": False,
    }
    assert coverage["markets"]["KR"]["pending"] == 12
    assert provider.requests == []


# --- integrated cadence: new policy end-to-end + receipt-backed last_stage -


def test_integrated_cadence_runs_drain_and_coverage_reads_receipt(tmp_path):
    from tokmo.programs.cadence_fixture_deps import build_fixture_cadence_deps
    from tokmo.programs.production_cadence import run_production_cadence_once

    root = tmp_path / "integrated"
    store = RegularSessionHistoryStore(
        root / "ops" / "regular_session_history.sqlite3",
        clock=_clock,
    )
    authority = _HistoryAuthority()
    for market in Market:
        for offset in range(1, 21):
            store.append_settled(
                market,
                SLOT.date() - timedelta(days=offset),
                "regular",
                authority=authority,
            )
    fixture = {
        "calendar": {
            "KR": {
                "regular_start": "2026-08-03T00:00:00Z",
                "regular_end": "2026-08-03T06:30:00Z",
            },
            "US": {
                "regular_start": "2026-08-03T13:30:00Z",
                "regular_end": "2026-08-03T20:00:00Z",
            },
        },
        "accounts": (),
        "watchlist_symbols": {"KR": ("005930",)},
        "stock_candles": TossReadOutcome(
            "success", CandlePageResponse(_candles(), None)
        ),
    }
    deps, probe = build_fixture_cadence_deps(
        fixture=fixture,
        clock=lambda: SLOT + timedelta(seconds=1),
    )
    result = run_production_cadence_once(
        root=root,
        policy_version=POLICY_ID,
        scheduled_for_utc=SLOT,
        observed_now_utc=SLOT + timedelta(seconds=1),
        deps=deps,
    )
    stage = {item.stage: item for item in result.stages}["market_regime_shadow"]
    assert POLICY.request_limits.market_daily_candle_starts == 3
    assert (stage.status, stage.reason) == ("ok", "kr_1_of_1_us_0_of_0_pending_0")
    assert probe.calls["STOCK_CANDLES_DAILY"] == 1
    coverage = read_market_regime_coverage(
        root=root,
        deps=SimpleNamespace(
            clock=_clock,
            watchlist_symbols={Market.KR: ("005930",)},
            pension_unquotable_symbols=frozenset(),
        ),
        policy_version=POLICY_ID,
        observed_now_utc=SLOT,
        cache_store=SimpleNamespace(read=lambda: None),
    )
    last = coverage["last_stage"]
    assert (last["status"], last["reason"]) == ("ok", "kr_1_of_1_us_0_of_0_pending_0")
    assert last["scheduled_for_utc"] == "2026-08-03T10:00:00.000000Z"
