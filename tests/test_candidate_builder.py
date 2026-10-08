from datetime import datetime, timedelta, timezone

from tokmo.values.analysis_barrier import AnalysisBarrierResultV1, MarketReadiness, VerifiedMaterialV1
from tokmo.values.analysis_contracts import MaterialKindV1, subject_id_for
from tokmo.values.material_pack import JobKind
from tokmo.values.ops_kernel import CandidateAssignment
from tokmo.values.session_gate import Market
from tokmo.programs.candidate_builder import (
    BaselineAdvanceV1,
    BuildResultV1,
    build_candidates,
    material_binding_digest,
)
from tokmo.values.portfolio_scope import resolve_account_scope_pattern  # N5

NOW = datetime(2026, 7, 30, 0, 0, tzinfo=timezone.utc)
H64 = "1" * 64
QUOTE_SUBJECT = subject_id_for(MaterialKindV1.QUOTE_STATE, ["KR", "005930"])


def _barrier(tick=10, quote_hash=H64, price="11", quote_time=None,
             include_holdings=True, holdings_quantity="2"):
    holdings = VerifiedMaterialV1(
        JobKind.HOLDINGS, tick, NOW, "2" * 64, "3" * 64,
        {"accounts": [{
            "account_scope": "toss-acct-" + "a" * 24,
            "account_type_label": "일반", "ordinal": 1,
            "holdings": [{
                "symbol": "005930", "quantity": holdings_quantity,
                "avg_price": "10",
                "currency": "KRW", "market_raw": "KR", "market_field": "KR",
            }],
        }], "global_inventory": True}, ("KR",),
    )
    quotes = VerifiedMaterialV1(
        JobKind.QUOTES, tick, NOW, "4" * 64, quote_hash,
        {"markets": {"KR": [{
            "symbol": "005930", "last_price": price, "currency_raw": "KRW",
            "provider_timestamp": quote_time, "fetched_at": NOW.isoformat(),
            "stored_at": NOW.isoformat(),
        }]}}, ("KR",),
    )
    trades = VerifiedMaterialV1(
        JobKind.TRADES, tick, NOW, "5" * 64, "6" * 64,
        {"accounts": [{
            "account_scope": "toss-acct-" + "a" * 24,
            "inserted": [{
                "source_record_key": "trade-1", "trade_date": "2026-07-30",
                "transaction_type_raw": "BUY", "instrument_raw": "005930",
                "quantity": "1", "unit_price": "11", "amount": "11",
                "medium_raw": "M", "currency_raw": "KRW",
                "classified_market": "KR", "filled_at": NOW.isoformat(),
            }],
            "duplicates": ["trade-duplicate"], "quarantine": [], "incomplete": [],
        }], "pagination_pending": False}, ("KR",),
    )
    materials = {JobKind.QUOTES: quotes, JobKind.TRADES: trades}
    if include_holdings:
        materials[JobKind.HOLDINGS] = holdings
    return AnalysisBarrierResultV1(
        tick_id=tick, materials=materials,
        market_readiness={Market.KR: MarketReadiness.READY,
                          Market.US: MarketReadiness.SKIP_CLOSED},
        stale_jobs={Market.KR: frozenset(), Market.US: frozenset()},
        material_ticks={
            JobKind.HOLDINGS: tick if include_holdings else None,
            JobKind.QUOTES: tick, JobKind.TRADES: tick,
        },
        incidents=(), proceed_markets=frozenset({Market.KR}),
    )


def _hist(tick_id, minutes_ago, price):
    return {
        "tick_id": tick_id,
        "observed_at": NOW - timedelta(minutes=minutes_ago),
        "last_price": price,
    }


def _quote_baseline(price, tick=9):
    return BaselineAdvanceV1(
        MaterialKindV1.QUOTE_STATE, QUOTE_SUBJECT,
        {"market": "KR", "symbol": "005930", "last_price": price},
        None, tick, "0" * 64,
    )


def _build(price, history_rows, *, tick=10, baseline_price=None,
           baseline_tick=9, include_holdings=True, holdings_quantity="2"):
    baselines = {}
    if baseline_price is not None:
        baselines[(MaterialKindV1.QUOTE_STATE, QUOTE_SUBJECT)] = _quote_baseline(
            baseline_price, tick=baseline_tick,
        )
    barrier = _barrier(
        tick=tick, price=price, quote_time=NOW.isoformat(),
        include_holdings=include_holdings, holdings_quantity=holdings_quantity,
    )
    return build_candidates(
        barrier, baselines=baselines,
        history={QUOTE_SUBJECT: tuple(history_rows)}, now=NOW,
        resolve_scope=resolve_account_scope_pattern,
    )


def _quotes(result):
    return [item for item in result.candidates
            if item.kind is MaterialKindV1.QUOTE_STATE]


def test_material_digest_is_candidate_independent_and_pack_bound():
    barrier = _barrier()
    digest = material_binding_digest(barrier)
    assert len(digest) == 64
    assert digest == material_binding_digest(barrier)
    assert digest != material_binding_digest(_barrier(quote_hash="9" * 64))


def test_exhaustive_deterministic_enumeration_and_first_baselines():
    results = [build_candidates(_barrier(), baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern) for _ in range(60)]
    assert all(item == results[0] for item in results)
    result = results[0]
    assert isinstance(result, BuildResultV1)
    assert [item.kind.value for item in result.candidates] == [
        "COMPLETED_EXECUTION", "QUOTE_STATE"
    ]
    assert result.candidates[0].assignment is CandidateAssignment.CRITICAL
    quote = result.candidates[1]
    assert quote.fields["change_basis"] == "CURRENT_FACT_ONLY"
    assert quote.fields["portfolio_relation"] == "HELD"
    assert quote.fields["trigger_kind"] == "FIRST_OBSERVATION"
    assert quote.fields["trigger_horizon"] == "NONE"
    assert quote.fields["trigger_reference_price"] is None
    assert quote.fields["numeric_state"] == "DATA_INCOMPLETE"
    assert quote.fields["money_impact"] is None
    assert len(result.baseline_advances) == 2
    assert [(item.subject_id, item.tick_id, item.last_price)
            for item in result.history_advances] == [(QUOTE_SUBJECT, 10, "11")]
    assert all("account_scope" not in candidate.fields for candidate in result.candidates)


def test_holding_change_uses_prior_baseline_and_duplicate_trade_is_not_candidate():
    first = build_candidates(_barrier(), baselines={}, now=NOW, resolve_scope=resolve_account_scope_pattern)
    baselines = {(item.kind, item.subject_id): item for item in first.baseline_advances}
    history = {QUOTE_SUBJECT: tuple(
        _hist(item.tick_id, 0, item.last_price) for item in first.history_advances
    )}
    second = build_candidates(
        _barrier(tick=11), baselines=baselines, history=history, now=NOW,
        resolve_scope=resolve_account_scope_pattern,
    )
    kinds = [item.kind.value for item in second.candidates]
    assert kinds.count("COMPLETED_EXECUTION") == 1
    assert kinds.count("HOLDINGS_CHANGE") == 0
    quote = next(item for item in second.candidates if item.kind.value == "QUOTE_STATE")
    assert quote.fields["change_basis"] == "BASELINE_DELTA"
    assert quote.fields["price_delta_ratio"] == "0"
    assert quote.fields["portfolio_relation"] == "HELD"
    # warm-up edge: DATA_INCOMPLETE -> NORMAL is the only reason a candidate exists
    assert quote.fields["trigger_kind"] == "STATE_TRANSITION"
    assert quote.fields["numeric_state"] == "NORMAL"
    # third tick with an unchanged state produces no QUOTE_STATE candidate at all
    third_history = {QUOTE_SUBJECT: (
        _hist(11, 0, "11"), _hist(10, 0, "11"),
    )}
    baselines = {
        **baselines,
        (MaterialKindV1.QUOTE_STATE, QUOTE_SUBJECT): _quote_baseline("11", tick=11),
    }
    third = build_candidates(
        _barrier(tick=12), baselines=baselines, history=third_history, now=NOW,
        resolve_scope=resolve_account_scope_pattern,
    )
    assert [item.kind.value for item in third.candidates] == ["COMPLETED_EXECUTION"]


def test_horizon_deltas_are_null_until_ring_covers_then_round_like_ratio():
    quote, = _quotes(_build("10000", ()))
    assert quote.fields["price_delta_ratio_5m"] is None
    assert quote.fields["price_delta_ratio_15m"] is None
    assert quote.fields["price_delta_ratio_30m"] is None

    prices = ["10050", "10040", "10030", "10020", "10010", "10000", "9990"]
    history = [
        _hist(9 - index, 5 * (index + 1), price)
        for index, price in enumerate(prices)
    ]
    quote, = _quotes(_build("10100", history, baseline_price="10050"))
    assert quote.fields["price_delta_ratio_5m"] == "0.004975"
    assert quote.fields["price_delta_ratio_15m"] == "0.006979"
    assert quote.fields["price_delta_ratio_30m"] == "0.01"
    assert quote.fields["trigger_kind"] == "CUMULATIVE_DRIFT"
    assert quote.fields["trigger_horizon"] == "HORIZON_30M"
    assert quote.fields["trigger_reference_price"] == "10000"
    assert quote.fields["money_impact"] == "200"


def test_velocity_and_acceleration_are_elapsed_normalized_and_fail_closed():
    history = [_hist(9, 5, "10000"), _hist(8, 10, "10000")]
    quote, = _quotes(_build("10050", history, baseline_price="10000"))
    assert quote.fields["velocity_per_min"] == "0.001"
    assert quote.fields["acceleration_per_min2"] == "0.0002"

    zero_elapsed = _build(
        "10050", [_hist(9, 0, "10000")], baseline_price="10000",
    )
    quote, = _quotes(zero_elapsed)
    assert quote.fields["velocity_per_min"] is None
    assert quote.fields["acceleration_per_min2"] is None
    assert [(item.subject_id, item.tick_id)
            for item in zero_elapsed.history_advances] == [(QUOTE_SUBJECT, 10)]


def test_reversal_requires_noise_floor_and_sign_flip():
    noise = _build(
        "10000", [_hist(9, 5, "10005"), _hist(8, 10, "10000")],
        baseline_price="10005",
    )
    assert _quotes(noise) == []

    flip = _build(
        "10000", [_hist(9, 5, "10015"), _hist(8, 10, "10000")],
        baseline_price="10015",
    )
    quote, = _quotes(flip)
    assert quote.fields["reversal"] is True
    assert quote.fields["trigger_kind"] == "REVERSAL"
    assert quote.fields["numeric_state"] == "REVERSAL"
    assert quote.fields["trigger_horizon"] == "TICK"
    assert quote.fields["trigger_reference_price"] == "10015"
    assert quote.fields["money_impact"] == "-30"


def test_nonmonotonic_observed_at_fails_closed_and_keeps_ring_usable():
    history = [_hist(9, -5, "9000"), _hist(8, 20, "10000")]
    result = _build("10100", history, baseline_price="9000")
    quote, = _quotes(result)
    # the adjacent pair is untrustworthy: no reversal, no streak, no velocity
    assert quote.fields["velocity_per_min"] is None
    assert quote.fields["acceleration_per_min2"] is None
    assert quote.fields["reversal"] is False
    assert quote.fields["persistent_drift_ticks"] == 0
    # yet this is provably not a flat tick, and older valid rows keep working
    assert quote.fields["price_delta_ratio"] == "0.122222"
    assert quote.fields["price_delta_ratio_15m"] == "0.01"
    assert quote.fields["price_delta_ratio_30m"] is None
    assert [(item.subject_id, item.tick_id)
            for item in result.history_advances] == [(QUOTE_SUBJECT, 10)]


def test_flat_tick_resets_drift_streak():
    history = [_hist(9, 5, "10100"), _hist(8, 10, "10100"), _hist(7, 15, "10000")]
    quote, = _quotes(_build("10200", history, baseline_price="10100"))
    assert quote.fields["persistent_drift_ticks"] == 1

    flat_now = _build(
        "10000", [_hist(9, 5, "10000"), _hist(8, 10, "9900")],
        baseline_price="10000",
    )
    quote, = _quotes(flat_now)
    assert quote.fields["persistent_drift_ticks"] == 0
    assert quote.fields["trigger_kind"] == "STATE_TRANSITION"
    assert quote.fields["numeric_state"] == "NORMAL"


def test_money_impact_requires_same_tick_holdings_material():
    history = [_hist(9, 5, "10000")]
    quote, = _quotes(_build(
        "10100", history, baseline_price="10000", include_holdings=False,
    ))
    assert quote.fields["money_impact"] is None
    assert quote.fields["portfolio_relation"] == "UNHELD"

    quote, = _quotes(_build("10100", history, baseline_price="10000"))
    assert quote.fields["trigger_kind"] == "THRESHOLD_MOVE"
    assert quote.fields["trigger_reference_price"] == "10000"
    assert quote.fields["money_impact"] == "200"


def test_money_impact_binds_to_trigger_reference_price():
    drift_history = [
        _hist(9, 5, "10020"), _hist(8, 10, "10010"), _hist(7, 15, "10000"),
    ]
    quote, = _quotes(_build("10030", drift_history, baseline_price="10020"))
    assert quote.fields["trigger_kind"] == "PERSISTENT_DRIFT"
    assert quote.fields["trigger_horizon"] == "STREAK"
    assert quote.fields["persistent_drift_ticks"] == 3
    assert quote.fields["trigger_reference_price"] == "10000"
    assert quote.fields["money_impact"] == "60"

    both_horizons = [
        _hist(9, 5, "10000"), _hist(8, 10, "10000"), _hist(7, 15, "10001"),
        _hist(6, 20, "10000"), _hist(5, 25, "10000"), _hist(4, 30, "10000"),
        _hist(3, 35, "10000"),
    ]
    quote, = _quotes(_build("10150", both_horizons, baseline_price="10000"))
    assert quote.fields["trigger_kind"] == "CUMULATIVE_DRIFT"
    assert quote.fields["trigger_horizon"] == "HORIZON_15M"
    assert quote.fields["trigger_reference_price"] == "10001"
    assert quote.fields["money_impact"] == "298"


def test_trigger_priority_is_deterministic_under_concurrent_conditions():
    first, = _quotes(_build("10000", ()))
    assert first.fields["trigger_kind"] == "FIRST_OBSERVATION"

    reversal_and_threshold = _build(
        "10000", [_hist(9, 5, "10200"), _hist(8, 10, "10000")],
        baseline_price="10200",
    )
    quote, = _quotes(reversal_and_threshold)
    assert quote.fields["trigger_kind"] == "REVERSAL"


def test_numeric_state_covers_six_values_and_drift_state_outlives_edge():
    first, = _quotes(_build("10000", ()))
    assert first.fields["numeric_state"] == "DATA_INCOMPLETE"

    watch, = _quotes(_build(
        "10030", [_hist(9, 5, "10000")], baseline_price="10000",
    ))
    assert watch.fields["numeric_state"] == "WATCH"
    assert watch.fields["trigger_kind"] == "STATE_TRANSITION"

    material, = _quotes(_build(
        "10060", [_hist(9, 5, "10000")], baseline_price="10000",
    ))
    assert material.fields["numeric_state"] == "MATERIAL_MOVE"
    assert material.fields["trigger_kind"] == "THRESHOLD_MOVE"

    # streak 4 with a large fourth move: state stays PERSISTENT_DRIFT while the
    # trigger edge (==3) has already passed
    streak_four = _build(
        "10090",
        [_hist(9, 5, "10030"), _hist(8, 10, "10020"),
         _hist(7, 15, "10010"), _hist(6, 20, "10000")],
        baseline_price="10030",
    )
    quote, = _quotes(streak_four)
    assert quote.fields["numeric_state"] == "PERSISTENT_DRIFT"
    assert quote.fields["persistent_drift_ticks"] == 4
    assert quote.fields["trigger_kind"] == "THRESHOLD_MOVE"


def test_state_transition_fires_on_edges_only():
    warmup = _build("11", [_hist(9, 0, "11")], baseline_price="11")
    quote, = _quotes(warmup)
    assert quote.fields["trigger_kind"] == "STATE_TRANSITION"
    assert quote.fields["numeric_state"] == "NORMAL"

    steady = _build(
        "11", [_hist(9, 0, "11"), _hist(8, 0, "11")], baseline_price="11",
    )
    assert _quotes(steady) == []

    recovery = _build(
        "10030", [_hist(9, 5, "10030"), _hist(8, 10, "10000")],
        baseline_price="10030",
    )
    quote, = _quotes(recovery)
    assert quote.fields["trigger_kind"] == "STATE_TRANSITION"
    assert quote.fields["numeric_state"] == "NORMAL"

    concurrent = _build(
        "10060", [_hist(9, 5, "10000")], baseline_price="10000",
    )
    assert len(_quotes(concurrent)) == 1
    assert _quotes(concurrent)[0].fields["trigger_kind"] == "THRESHOLD_MOVE"


def test_persistent_drift_rearm_requires_streak_reset():
    fires, = _quotes(_build(
        "10030",
        [_hist(9, 5, "10020"), _hist(8, 10, "10010"), _hist(7, 15, "10000")],
        baseline_price="10020",
    ))
    assert fires.fields["trigger_kind"] == "PERSISTENT_DRIFT"

    suppressed = _build(
        "10040",
        [_hist(9, 5, "10030"), _hist(8, 10, "10020"),
         _hist(7, 15, "10010"), _hist(6, 20, "10000")],
        baseline_price="10030",
    )
    assert _quotes(suppressed) == []

    rearmed, = _quotes(_build(
        "10070",
        [_hist(9, 5, "10060"), _hist(8, 10, "10050"),
         _hist(7, 15, "10040"), _hist(6, 20, "10040")],
        baseline_price="10060",
    ))
    assert rearmed.fields["trigger_kind"] == "PERSISTENT_DRIFT"

    downward, = _quotes(_build(
        "9960",
        [_hist(9, 5, "9970"), _hist(8, 10, "9980"), _hist(7, 15, "9990")],
        baseline_price="9970",
    ))
    assert downward.fields["trigger_kind"] == "PERSISTENT_DRIFT"
    assert downward.fields["persistent_drift_ticks"] == -3


def test_cumulative_drift_rearm_uses_prior_ring_including_30m():
    prices = ["10050", "10040", "10030", "10020", "10010", "10000", "9990"]
    history = [
        _hist(9 - index, 5 * (index + 1), price)
        for index, price in enumerate(prices)
    ]
    quote, = _quotes(_build("10100", history, baseline_price="10050"))
    assert quote.fields["trigger_kind"] == "CUMULATIVE_DRIFT"
    assert quote.fields["trigger_horizon"] == "HORIZON_30M"

    # next tick still above threshold: the prior 30m value is only computable
    # because R' retains 7 durable rows — no re-fire while it stays above
    above_prices = ["10100", "10090", "10080", "10060", "10040", "10020", "10000"]
    above = [
        _hist(10 - index, 5 * (index + 1), price)
        for index, price in enumerate(above_prices)
    ]
    assert _quotes(_build(
        "10150", above, tick=11, baseline_price="10100", baseline_tick=10,
    )) == []

    # after dipping below, the next crossing fires again (15m horizon)
    dipped = [
        _hist(9, 5, "10000"), _hist(8, 10, "10000"),
        _hist(7, 15, "10040"), _hist(6, 20, "10040"),
    ]
    quote, = _quotes(_build("10150", dipped, baseline_price="10000"))
    assert quote.fields["trigger_kind"] == "CUMULATIVE_DRIFT"
    assert quote.fields["trigger_horizon"] == "HORIZON_15M"


def test_baseline_and_history_advance_even_when_gate_misses():
    result = _build(
        "10000", [_hist(9, 5, "10000"), _hist(8, 10, "10000")],
        baseline_price="10000",
    )
    assert _quotes(result) == []
    assert [(item.subject_id, item.tick_id, item.last_price)
            for item in result.history_advances] == [(QUOTE_SUBJECT, 10, "10000")]
    assert any(
        item.kind is MaterialKindV1.QUOTE_STATE
        and item.subject_id == QUOTE_SUBJECT
        and item.source_tick_id == 10
        for item in result.baseline_advances
    )
