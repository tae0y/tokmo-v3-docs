"""Slice 1 — quant profile values: identities, registry, budget formulas.

All symbols in this file are synthetic / RECONSTRUCTED_CONTRACT shapes only;
no live cohort symbol appears here (Plan/SDS §13).
"""

from datetime import date
from hashlib import sha256

import pytest

POLICY_VERSION = "cadence-300s-quant-profiles-shadow-v1"
SESSION = date(2026, 8, 21)


def test_kr_and_us_profile_identities_are_fully_independent_for_same_date():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        US_PROFILE_ID,
        US_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    kr = QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        POLICY_VERSION,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )
    us = QuantProfileDueIdentityV1(
        US_PROFILE_ID,
        POLICY_VERSION,
        Market.US,
        SESSION,
        US_UNIVERSE_POLICY_VERSION,
    )
    assert kr.identity_digest != us.identity_digest
    expected_kr = sha256(
        "|".join(
            (
                "quant-kr-toss-membership-v1",
                POLICY_VERSION,
                "KR",
                "2026-08-21",
                "quant-universe-kr-personal-v1",
            )
        ).encode("ascii")
    ).hexdigest()
    assert kr.identity_digest == expected_kr
    assert kr.acquisition_id == (
        f"quant-profiles-v1:quant-kr-toss-membership-v1:2026-08-21:{expected_kr[:32]}"
    )
    assert us.acquisition_id.startswith(
        "quant-profiles-v1:quant-us-nyse-nasdaq-v1:2026-08-21:"
    )


def test_identity_refuses_non_registry_profile_and_mismatched_bindings():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        US_UNIVERSE_POLICY_VERSION,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    with pytest.raises(ValueError):
        QuantProfileDueIdentityV1(
            "quant-xx-unreviewed-v1",
            POLICY_VERSION,
            Market.KR,
            SESSION,
            KR_UNIVERSE_POLICY_VERSION,
        )
    with pytest.raises(ValueError):  # market must match the profile's market
        QuantProfileDueIdentityV1(
            KR_PROFILE_ID,
            POLICY_VERSION,
            Market.US,
            SESSION,
            KR_UNIVERSE_POLICY_VERSION,
        )
    with pytest.raises(ValueError):  # universe must match the profile's universe
        QuantProfileDueIdentityV1(
            KR_PROFILE_ID,
            POLICY_VERSION,
            Market.KR,
            SESSION,
            US_UNIVERSE_POLICY_VERSION,
        )


def test_registry_is_closed_with_exact_budget_formulas_and_maxima():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        QUANT_PROFILE_EXECUTION_ORDER,
        QUANT_PROFILE_REGISTRY,
        US_PROFILE_ID,
    )
    from tokmo.values.session_gate import Market

    assert set(QUANT_PROFILE_REGISTRY) == {US_PROFILE_ID, KR_PROFILE_ID}
    assert QUANT_PROFILE_EXECUTION_ORDER == (KR_PROFILE_ID, US_PROFILE_ID)
    with pytest.raises(TypeError):
        QUANT_PROFILE_REGISTRY["quant-xx-unreviewed-v1"] = object()

    kr = QUANT_PROFILE_REGISTRY[KR_PROFILE_ID]
    us = QUANT_PROFILE_REGISTRY[US_PROFILE_ID]
    assert (kr.market, us.market) == (Market.KR, Market.US)
    assert (kr.cohort_max, us.cohort_max) == (5, 4)
    # KR: exactly one STOCKS batch regardless of cohort size (§7)
    assert [kr.membership_plan_starts(n) for n in (1, 3, 5)] == [1, 1, 1]
    assert [kr.quant_due_max_increment(n) for n in (1, 5)] == [3, 11]
    # US: existing 3+N / 3+3N formulas byte-identical (§7)
    assert [us.membership_plan_starts(n) for n in (1, 4)] == [4, 7]
    assert [us.quant_due_max_increment(n) for n in (1, 4)] == [6, 15]
    # KR fundamentals are PAUSED, never SEC parity (§5.3)
    assert kr.fundamental_binding.value == "PAUSED"
    assert us.fundamental_binding.value == "SEC_FUNDAMENTALS"
    assert (kr.currency, us.currency) == ("KRW", "USD")


def test_cohort_overflow_context_is_typed_not_due_with_zero_planned_starts():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        US_PROFILE_ID,
        QuantProfileDueContextV1,
        QuantProfileDueDecisionV1,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    # RECONSTRUCTED_CONTRACT synthetic symbols only.
    six = tuple(f"RC000{index}" for index in range(1, 7))

    overflow = QuantProfileDueContextV1(
        profile_id=KR_PROFILE_ID,
        decision=QuantProfileDueDecisionV1.PROFILE_COHORT_EXCEEDS_POLICY,
        identity=None,
        completed_session_date=SESSION,
        local_session_date=SESSION,
        predicted_symbols=(),
        deadline_margin_seconds=0,
        membership_plan_starts=0,
        membership_starts_ceiling=7,
    )
    assert overflow.membership_plan_starts == 0
    assert overflow.predicted_symbols == ()

    # An overflow context may never carry symbols or planned starts.
    with pytest.raises(ValueError):
        QuantProfileDueContextV1(
            profile_id=KR_PROFILE_ID,
            decision=QuantProfileDueDecisionV1.PROFILE_COHORT_EXCEEDS_POLICY,
            identity=None,
            completed_session_date=SESSION,
            local_session_date=SESSION,
            predicted_symbols=six,
            deadline_margin_seconds=0,
            membership_plan_starts=1,
            membership_starts_ceiling=7,
        )

    # DUE with a 6th KR member (or a 5th US member) is unrepresentable.
    kr_identity = QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        POLICY_VERSION,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )
    with pytest.raises(ValueError):
        QuantProfileDueContextV1(
            profile_id=KR_PROFILE_ID,
            decision=QuantProfileDueDecisionV1.DUE,
            identity=kr_identity,
            completed_session_date=SESSION,
            local_session_date=SESSION,
            predicted_symbols=six,
            deadline_margin_seconds=60,
            membership_plan_starts=1,
            membership_starts_ceiling=7,
        )
    from tokmo.values.quant_profiles import (
        US_UNIVERSE_POLICY_VERSION,
    )

    us_identity = QuantProfileDueIdentityV1(
        US_PROFILE_ID,
        POLICY_VERSION,
        Market.US,
        SESSION,
        US_UNIVERSE_POLICY_VERSION,
    )
    with pytest.raises(ValueError):
        QuantProfileDueContextV1(
            profile_id=US_PROFILE_ID,
            decision=QuantProfileDueDecisionV1.DUE,
            identity=us_identity,
            completed_session_date=SESSION,
            local_session_date=SESSION,
            predicted_symbols=tuple(f"RCU{index}" for index in range(5)),
            deadline_margin_seconds=60,
            membership_plan_starts=8,
            membership_starts_ceiling=8,
        )


def test_due_context_requires_identity_binding_and_formula_consistency():
    from tokmo.values.quant_profiles import (
        KR_PROFILE_ID,
        KR_UNIVERSE_POLICY_VERSION,
        QuantProfileDueContextV1,
        QuantProfileDueDecisionV1,
        QuantProfileDueIdentityV1,
    )
    from tokmo.values.session_gate import Market

    identity = QuantProfileDueIdentityV1(
        KR_PROFILE_ID,
        POLICY_VERSION,
        Market.KR,
        SESSION,
        KR_UNIVERSE_POLICY_VERSION,
    )
    context = QuantProfileDueContextV1(
        profile_id=KR_PROFILE_ID,
        decision=QuantProfileDueDecisionV1.DUE,
        identity=identity,
        completed_session_date=SESSION,
        local_session_date=SESSION,
        predicted_symbols=("RC0001", "RC0002"),
        deadline_margin_seconds=60,
        membership_plan_starts=1,
        membership_starts_ceiling=7,
    )
    assert context.identity is identity

    with pytest.raises(ValueError):  # wrong plan-starts for the KR formula
        QuantProfileDueContextV1(
            profile_id=KR_PROFILE_ID,
            decision=QuantProfileDueDecisionV1.DUE,
            identity=identity,
            completed_session_date=SESSION,
            local_session_date=SESSION,
            predicted_symbols=("RC0001", "RC0002"),
            deadline_margin_seconds=60,
            membership_plan_starts=5,
            membership_starts_ceiling=7,
        )
    with pytest.raises(ValueError):  # a non-DUE context may not carry identity
        QuantProfileDueContextV1(
            profile_id=KR_PROFILE_ID,
            decision=QuantProfileDueDecisionV1.NO_COMPLETED_SESSION,
            identity=identity,
            completed_session_date=SESSION,
            local_session_date=SESSION,
            predicted_symbols=(),
            deadline_margin_seconds=0,
            membership_plan_starts=1,
            membership_starts_ceiling=7,
        )


def test_membership_toss_stocks_operation_is_closed_and_date_validated():
    from tokmo.values.cadence_policy import (
        CADENCE_OPERATIONS,
        validate_cadence_operation,
    )
    from tokmo.values.operation_lane_registry import (
        OPERATION_LANE_REGISTRY_V1,
        OperationLane,
    )

    assert CADENCE_OPERATIONS["MEMBERSHIP_TOSS_STOCKS"] == "MEMBERSHIP"
    assert (
        validate_cadence_operation(
            "MEMBERSHIP_TOSS_STOCKS", "membership:toss-stocks:2026-08-21"
        )
        == "MEMBERSHIP"
    )
    for bad in (
        "membership:toss-stocks:2026-02-30",  # non-existent calendar date
        "membership:toss-stocks:2026-8-21",
        "membership:toss-stocks:2026-08-21:RC0001",
        "membership:stocks:2026-08-21",
    ):
        with pytest.raises(ValueError, match="non-canonical native identity"):
            validate_cadence_operation("MEMBERSHIP_TOSS_STOCKS", bad)
    entry = OPERATION_LANE_REGISTRY_V1.entries["MEMBERSHIP_TOSS_STOCKS"]
    assert entry.recurring_owner is OperationLane.CADENCE_300S
    assert entry.fallback_owner is None
    assert entry.cutover_epoch_utc is None


def test_quant_profiles_policy_is_registered_with_exact_47_start_limits():
    from tokmo.values.cadence_policy import (
        CADENCE_POLICY_REGISTRY,
        CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1,
    )

    policy = CADENCE_QUANT_PROFILES_SHADOW_POLICY_V1
    assert policy.policy_version == "cadence-300s-quant-profiles-shadow-v1"
    assert CADENCE_POLICY_REGISTRY[policy.policy_version] is policy
    limits = policy.request_limits
    assert limits.total_wire_starts == 47
    assert (
        limits.token_starts,
        limits.calendar_starts,
        limits.holdings_starts,
        limits.quote_starts,
        limits.trades_starts,
    ) == (1, 2, 8, 4, 12)
    assert limits.daily_candle_starts == 13
    assert limits.market_daily_candle_starts == 3
    assert limits.quant_daily_candle_starts == 10
    assert limits.membership_starts == 7
    assert limits.conditional_order_starts == 0
    # the group invariant is exact, not merely satisfied with slack
    assert (
        limits.token_starts
        + limits.calendar_starts
        + limits.holdings_starts
        + limits.quote_starts
        + limits.trades_starts
        + limits.daily_candle_starts
        + limits.membership_starts
        + limits.conditional_order_starts
    ) == limits.total_wire_starts


def test_existing_policy_snapshots_and_digests_are_byte_stable():
    from tokmo.values.cadence_policy import (
        CADENCE_CORE_SHADOW_POLICY_V1,
        CADENCE_MARKET_PORTFOLIO_POLICY_V1,
        CADENCE_POLICY_V1,
        CADENCE_QUANT_SHADOW_POLICY_V1,
        TradesChildLimitsV1,
        policy_digest,
    )

    child = TradesChildLimitsV1()
    assert {
        policy.policy_version: policy_digest(policy, child)
        for policy in (
            CADENCE_POLICY_V1,
            CADENCE_CORE_SHADOW_POLICY_V1,
            CADENCE_MARKET_PORTFOLIO_POLICY_V1,
            CADENCE_QUANT_SHADOW_POLICY_V1,
        )
    } == {
        "cadence-300s-v1": "571f8aa25368c595949911868733727166e51a9591538c61a05672c9b7910564",
        "cadence-300s-core-shadow-v1": "d81f2b754027ff13126be5294c708a07078fdf7026e1db9e74447e4e0361d571",
        "cadence-300s-market-portfolio-v1": "5093db5a9c75ca5817e56d099b4cc7e98a579cf37c8b0fd3e45e87cd4e849546",
        "cadence-300s-market-quant4-shadow-v1": "de64f5fe5e4e9dc5fe88a34c15d3d5f48ff8c9712c1e56a0ed51852963004d36",
    }
