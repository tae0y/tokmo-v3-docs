"""W1 Quant recurring completed-session due gate and cohort prediction."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from tokmo.programs.calendar_refresh import MarketRefreshState

SESSION_DATE = date(2026, 8, 14)
SLOT = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
_DEFAULT_RECEIPTS = object()


def _holdings(
    captured_at=SLOT - timedelta(minutes=5),
    symbols=("TSLA", "AAPL", "NVDA", "MSFT"),
):
    from tokmo.values.current_holdings import (
        AccountHoldingsV1,
        AccountScopeIdentityV1,
        CurrentHoldingsV1,
        HoldingItemV1,
    )

    scope = "toss-acct-" + "0" * 24
    items = tuple(
        HoldingItemV1(symbol, "1", "100", "USD", "US", None) for symbol in symbols
    )
    return CurrentHoldingsV1(
        1,
        (AccountHoldingsV1(scope, 1, items),),
        True,
        captured_at,
        {scope: AccountScopeIdentityV1(scope, "personal", 1, None)},
    )


def _cache(
    *,
    slot=SLOT,
    regular_end=SLOT,
    after_end=SLOT + timedelta(hours=2),
    trade_date=SESSION_DATE,
    include_after=True,
):
    from tokmo.records.calendar_cache import (
        CalendarCacheSchema2,
        CalendarDayCacheSchema2,
        CalendarSessionCacheSchema2,
    )

    def stamp(value):
        return value.isoformat().replace("+00:00", "Z")

    sessions = {
        "regularMarket": CalendarSessionCacheSchema2(
            stamp(regular_end - timedelta(hours=6, minutes=30)),
            stamp(regular_end),
        ),
    }
    if include_after:
        sessions["afterMarket"] = CalendarSessionCacheSchema2(
            stamp(regular_end), stamp(after_end)
        )
    us = CalendarDayCacheSchema2(
        trade_date.isoformat(),
        sessions,
        trade_date.isoformat(),
        stamp(slot - timedelta(seconds=1)),
        2,
        "toss-market-calendar-v1",
    )
    return CalendarCacheSchema2(2, None, us, stamp(slot - timedelta(seconds=1)))


class _CacheStore:
    def __init__(self, value):
        self.value = value

    def read(self):
        return self.value


class _Receipts:
    def __init__(self, terminal=False):
        self.terminal = terminal
        self.reads = 0

    def read_due(self, identity_digest):
        self.reads += 1
        return object() if self.terminal else None


def _calendar(
    *,
    state="success",
    verdict_open=True,
    verdict_reason=None,
    in_mask=True,
    mask_bound=True,
    cache_trade_date=SESSION_DATE,
):
    from tokmo.values.quant_recurring import QuantTickCalendarInputV1

    state_value = getattr(state, "value", state)
    succeeded = state_value == "success"
    if verdict_reason is None:
        verdict_reason = "open" if verdict_open else "stale_cache"
    return QuantTickCalendarInputV1(
        us_refresh_state=state_value,
        us_refresh_succeeded=succeeded,
        us_verdict_open=verdict_open,
        us_verdict_reason=verdict_reason,
        us_in_effective_mask=in_mask,
        mask_bound=mask_bound,
        us_cache_trade_date=cache_trade_date,
    )


def _write_holdings(root, symbols=("TSLA", "AAPL", "NVDA", "MSFT")):
    from tokmo.records.current_holdings_store import CurrentHoldingsStore

    state = _holdings(symbols=symbols)
    store = CurrentHoldingsStore(
        root / "stores" / "current_holdings.json", clock=lambda: SLOT
    )
    store.replace(
        state,
        identity_fingerprints={next(iter(state.identity_map)): "a" * 64},
    )


def _evaluate(
    monkeypatch,
    tmp_path,
    *,
    policy,
    slot=SLOT,
    cache=None,
    terminal=False,
    acquisition=False,
    clock=None,
    receipts=_DEFAULT_RECEIPTS,
    calendar=None,
    cache_store=None,
):
    import tokmo.programs.quant_recurring_shadow as module

    class History:
        def __init__(self, path, *, clock):
            del path, clock

        def read(self, market, local_date):
            del market
            if local_date == SESSION_DATE:
                return {"session": "regular"}
            return None

    class Observations:
        def __init__(self, path):
            del path

        def read_acquisition(self, acquisition_id):
            del acquisition_id
            return object() if acquisition else None

    monkeypatch.setattr(module, "RegularSessionHistoryStore", History)
    monkeypatch.setattr(module, "OfficialMembershipObservationStore", Observations)
    if acquisition:
        observation_path = tmp_path / "official-membership" / "observations.sqlite3"
        observation_path.parent.mkdir(parents=True, exist_ok=True)
        observation_path.touch()
    cache_value = _cache(slot=slot) if cache is None else cache
    if calendar is None:
        cache_trade_date = (
            None
            if cache_value is None or cache_value.us is None
            else date.fromisoformat(cache_value.us.trade_date)
        )
        calendar = _calendar(cache_trade_date=cache_trade_date)
    deps = SimpleNamespace(
        clock=(lambda: slot + timedelta(seconds=1)) if clock is None else clock
    )
    return module.evaluate_quant_due(
        root=tmp_path,
        deps=deps,
        policy=policy,
        tick=SimpleNamespace(scheduled_for_utc=slot),
        calendar=calendar,
        cache_store=_CacheStore(cache_value) if cache_store is None else cache_store,
        receipts=(_Receipts(terminal) if receipts is _DEFAULT_RECEIPTS else receipts),
    )


def test_predict_us_personal_cohort_reuses_scope_and_market_rules_without_freshness():
    from tokmo.facts.quant_recurring_membership import predict_us_personal_cohort
    from tokmo.values.current_holdings import HoldingItemV1

    prior = _holdings(captured_at=datetime(2020, 1, 1, tzinfo=UTC))
    assert predict_us_personal_cohort(prior) == ("AAPL", "MSFT", "NVDA", "TSLA")
    assert predict_us_personal_cohort(None) == ()
    bad_item = HoldingItemV1("AAPL", "1", "1", "USD", "UNKNOWN", None)
    account = prior.accounts[0]
    object.__setattr__(account, "holdings", (bad_item,))
    assert predict_us_personal_cohort(prior) == ()


def test_policy_zero_returns_before_any_cache_receipt_or_observation_access(
    monkeypatch, tmp_path
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_MARKET_PORTFOLIO_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    class Bomb:
        def __init__(self, *args, **kwargs):
            raise AssertionError("store opened")

    class CalendarBomb:
        def __getattribute__(self, name):
            raise AssertionError(f"calendar accessed: {name}")

    monkeypatch.setattr(module, "RegularSessionHistoryStore", Bomb)
    monkeypatch.setattr(module, "OfficialMembershipObservationStore", Bomb)
    context = module.evaluate_quant_due(
        root=tmp_path,
        deps=SimpleNamespace(clock=lambda: SLOT),
        policy=CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        tick=SimpleNamespace(scheduled_for_utc=SLOT),
        calendar=CalendarBomb(),
        cache_store=SimpleNamespace(
            read=lambda: (_ for _ in ()).throw(AssertionError("cache read"))
        ),
        receipts=SimpleNamespace(
            read_due=lambda value: (_ for _ in ()).throw(AssertionError("receipt read"))
        ),
    )
    assert context.decision is QuantDueDecisionV1.POLICY_ZERO
    assert (context.membership_plan_starts, context.membership_starts_ceiling) == (
        3,
        0,
    )


@pytest.mark.parametrize(
    "state",
    tuple(
        state for state in MarketRefreshState if state is not MarketRefreshState.SUCCESS
    ),
)
def test_same_day_cache_non_success_us_refresh_is_zero_effect_before_cache_read(
    monkeypatch,
    tmp_path,
    state,
):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    class BombCache:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            raise AssertionError("D2 re-read cache")

    receipts = _Receipts()
    cache_store = BombCache()
    context = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        calendar=_calendar(
            state=state,
            verdict_open=False,
            verdict_reason=(
                "cache_corrupt"
                if state.value == "cache_update_failed"
                else "stale_cache"
            ),
            in_mask=False,
        ),
        cache_store=cache_store,
        receipts=receipts,
    )

    assert context.decision is QuantDueDecisionV1.NO_COMPLETED_SESSION
    assert context.identity is None
    assert cache_store.reads == 0
    assert receipts.reads == 0
    assert not (tmp_path / "stores" / "current_holdings.json").exists()
    assert not (tmp_path / "ops" / "quant_recurring.sqlite3").exists()
    assert not (tmp_path / "official-membership" / "observations.sqlite3").exists()


def test_unbound_mask_refuses_before_cache_read_and_does_not_burn_session(
    monkeypatch, tmp_path
):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    class BombCache:
        def __init__(self):
            self.reads = 0

        def read(self):
            self.reads += 1
            raise AssertionError("unbound D2 re-read cache")

    receipts = _Receipts()
    cache_store = BombCache()
    context = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        calendar=_calendar(in_mask=False, mask_bound=False),
        cache_store=cache_store,
        receipts=receipts,
    )

    assert context.decision is QuantDueDecisionV1.NO_COMPLETED_SESSION
    assert context.identity is None
    assert cache_store.reads == 0
    assert receipts.reads == 0
    assert not (tmp_path / "ops" / "quant_recurring.sqlite3").exists()


def test_failed_refresh_does_not_burn_d_and_next_same_session_success_is_due(
    monkeypatch,
    tmp_path,
):
    from tokmo.programs.calendar_refresh import MarketRefreshState
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    _write_holdings(tmp_path)
    receipts = _Receipts()
    refused = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        calendar=_calendar(
            state=MarketRefreshState.PROVIDER_FAILED,
            verdict_open=False,
            in_mask=False,
        ),
        cache_store=SimpleNamespace(
            read=lambda: (_ for _ in ()).throw(AssertionError("cache reread"))
        ),
        receipts=receipts,
    )
    retried = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        receipts=receipts,
    )

    assert refused.decision is QuantDueDecisionV1.NO_COMPLETED_SESSION
    assert retried.decision is QuantDueDecisionV1.DUE
    assert receipts.reads == 1
    assert not (tmp_path / "ops" / "quant_recurring.sqlite3").exists()


@pytest.mark.parametrize(
    "kr_state",
    (MarketRefreshState.SUCCESS, MarketRefreshState.PROVIDER_FAILED),
    ids=("r16e_all_success", "r16f_kr_failed_us_success"),
)
def test_us_success_matching_cache_and_mask_is_due_independent_of_kr_refresh(
    monkeypatch,
    tmp_path,
    kr_state,
):
    import tokmo.programs.production_cadence as production
    from tokmo.programs.calendar_refresh import MarketRefreshResult
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1
    from tokmo.values.session_gate import Market, SessionVerdict
    from tokmo.values.toss_market_calendar import CalendarSession

    _write_holdings(tmp_path)
    cache = _cache()
    results = {
        Market.KR: MarketRefreshResult(
            Market.KR,
            SESSION_DATE,
            kr_state,
            False,
        ),
        Market.US: MarketRefreshResult(
            Market.US,
            SESSION_DATE,
            MarketRefreshState.SUCCESS,
            False,
        ),
    }
    verdicts = (
        SessionVerdict(Market.KR, False, "stale_cache", None),
        SessionVerdict(
            Market.US,
            True,
            "open",
            CalendarSession(SLOT, SLOT + timedelta(hours=2)),
        ),
    )
    calendar = production._quant_tick_calendar_input(
        results=results,
        verdicts=verdicts,
        mask=SimpleNamespace(markets=frozenset({Market.US})),
        cache_store=_CacheStore(cache),
    )
    context = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        cache=cache,
        calendar=calendar,
    )

    assert context.decision is QuantDueDecisionV1.DUE
    assert context.membership_plan_starts == 7


def test_completed_session_due_gate_and_terminal_acquisition_replay(
    monkeypatch, tmp_path
):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    _write_holdings(tmp_path)
    due = _evaluate(monkeypatch, tmp_path, policy=CADENCE_QUANT_SHADOW_POLICY_V1)
    assert due.decision is QuantDueDecisionV1.DUE
    assert due.completed_session_date == SESSION_DATE
    assert due.ny_date == SESSION_DATE
    assert due.predicted_symbols == ("AAPL", "MSFT", "NVDA", "TSLA")

    terminal = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        terminal=True,
    )
    acquired = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        acquisition=True,
    )
    assert terminal.decision is QuantDueDecisionV1.ALREADY_TERMINAL
    assert acquired.decision is QuantDueDecisionV1.ALREADY_TERMINAL


def test_membership_budget_boundary_and_sweep_are_exactly_three_plus_n(
    monkeypatch,
    tmp_path,
):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    for cohort_size in range(1, 9):
        root = tmp_path / f"n{cohort_size}"
        symbols = tuple(f"S{index}" for index in range(cohort_size))
        _write_holdings(root, symbols)
        context = _evaluate(
            monkeypatch,
            root,
            policy=CADENCE_QUANT_SHADOW_POLICY_V1,
            receipts=None,
        )
        expected = (
            QuantDueDecisionV1.DUE
            if cohort_size <= 4
            else QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE
        )
        assert context.decision is expected
        assert context.membership_plan_starts == 3 + cohort_size
        assert context.membership_starts_ceiling == 7
        assert (context.identity is not None) is (cohort_size <= 4)
        assert not (root / "ops" / "quant_recurring.sqlite3").exists()
        assert not (root / "official-membership" / "observations.sqlite3").exists()
        assert not (root / "official-membership" / "campaign.sqlite3").exists()


def test_custom_lower_ceiling_refuses_at_its_true_boundary(monkeypatch, tmp_path):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    lower = replace(
        CADENCE_QUANT_SHADOW_POLICY_V1,
        request_limits=replace(
            CADENCE_QUANT_SHADOW_POLICY_V1.request_limits,
            membership_starts=5,
        ),
    )
    two_root = tmp_path / "two"
    three_root = tmp_path / "three"
    _write_holdings(two_root, ("AAPL", "MSFT"))
    _write_holdings(three_root, ("AAPL", "MSFT", "NVDA"))

    due = _evaluate(monkeypatch, two_root, policy=lower)
    refused = _evaluate(monkeypatch, three_root, policy=lower)

    assert due.decision is QuantDueDecisionV1.DUE
    assert (due.membership_plan_starts, due.membership_starts_ceiling) == (5, 5)
    assert refused.decision is QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE
    assert (refused.membership_plan_starts, refused.membership_starts_ceiling) == (
        6,
        5,
    )
    assert refused.identity is None


def test_budget_unsatisfiable_context_cannot_claim_execute_or_call_provider(
    monkeypatch,
    tmp_path,
):
    import tokmo.programs.quant_recurring_shadow as module
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    class Bomb:
        def __init__(self):
            self.calls = 0

        def __call__(self, *args, **kwargs):
            self.calls += 1
            raise AssertionError("forbidden effect")

        def __getattr__(self, name):
            del name
            return self

    _write_holdings(tmp_path, ("AAPL", "MSFT", "NVDA", "TSLA", "VOO"))
    context = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        receipts=None,
    )
    assert context.decision is QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE
    deps = Bomb()
    executor = Bomb()
    receipt_store = Bomb()
    with pytest.raises(module.QuantRecurringRefusal, match="^context_not_due$"):
        module.run_quant_membership_acquisition(
            root=tmp_path,
            deps=deps,
            policy=CADENCE_QUANT_SHADOW_POLICY_V1,
            tick=SimpleNamespace(scheduled_for_utc=SLOT),
            executor=executor,
            context=context,
            receipts=receipt_store,
        )
    assert deps.calls == executor.calls == receipt_store.calls == 0
    assert not (tmp_path / "ops" / "quant_recurring.sqlite3").exists()


def test_due_gate_rejects_preclose_cross_date_closed_window_and_deadline(
    monkeypatch, tmp_path
):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    _write_holdings(tmp_path)
    before = SLOT - timedelta(seconds=1)
    preclose = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        slot=before,
        cache=_cache(slot=before, regular_end=SLOT),
    )
    assert preclose.decision is QuantDueDecisionV1.NO_COMPLETED_SESSION

    next_ny_day = datetime(2026, 8, 15, 5, 0, tzinfo=UTC)
    mismatch = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        slot=next_ny_day,
        cache=_cache(
            slot=next_ny_day,
            regular_end=SLOT,
            after_end=next_ny_day + timedelta(hours=1),
            trade_date=date(2026, 8, 15),
        ),
    )
    assert mismatch.decision is QuantDueDecisionV1.NY_DATE_MISMATCH

    closed = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        cache=_cache(include_after=False),
        calendar=_calendar(
            verdict_open=False,
            verdict_reason="after_close",
            in_mask=False,
        ),
    )
    assert closed.decision is QuantDueDecisionV1.NO_POST_CLOSE_OPEN_WINDOW

    deadline = _evaluate(
        monkeypatch,
        tmp_path,
        policy=CADENCE_QUANT_SHADOW_POLICY_V1,
        clock=lambda: SLOT + timedelta(seconds=91),
    )
    assert deadline.decision is QuantDueDecisionV1.INSUFFICIENT_DEADLINE
