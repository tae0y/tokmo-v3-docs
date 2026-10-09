"""Pure W1 Quant recurring identities, planning, and deadline gates."""

from dataclasses import FrozenInstanceError
from datetime import UTC, date, datetime, timedelta

import pytest


def _calendar_input(**overrides):
    from tokmo.values.quant_recurring import QuantTickCalendarInputV1

    values = {
        "us_refresh_state": "success",
        "us_refresh_succeeded": True,
        "us_verdict_open": True,
        "us_verdict_reason": "open",
        "us_in_effective_mask": True,
        "mask_bound": True,
        "us_cache_trade_date": date(2026, 8, 14),
    }
    values.update(overrides)
    return QuantTickCalendarInputV1(**values)


def _identity(session_date=date(2026, 8, 14)):
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.quant_multifactor_core import MarketV1
    from tokmo.values.quant_recurring import (
        QUANT_RECURRING_PROGRAM_ID,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
        QuantDueIdentityV1,
    )

    return QuantDueIdentityV1(
        QUANT_RECURRING_PROGRAM_ID,
        CADENCE_QUANT_SHADOW_POLICY_V1.policy_version,
        MarketV1.US_NYSE_NASDAQ,
        session_date,
        QUANT_RECURRING_UNIVERSE_POLICY_VERSION,
    )


def test_membership_plan_formulas_are_single_source_pure_and_strictly_total():
    from tokmo.values.quant_recurring import (
        membership_plan_starts,
        quant_due_max_increment,
    )

    assert tuple(membership_plan_starts(size) for size in (0, 1, 4, 8)) == (
        3,
        4,
        7,
        11,
    )
    assert tuple(quant_due_max_increment(size) for size in (0, 1, 4, 8)) == (
        3,
        6,
        15,
        27,
    )
    for invalid in (True, -1, 1.0, "4", None):
        with pytest.raises(ValueError, match="^cohort_size$"):
            membership_plan_starts(invalid)
        with pytest.raises(ValueError, match="^cohort_size$"):
            quant_due_max_increment(invalid)


def test_quant_tick_calendar_input_is_frozen_slotted_and_declares_typed_conflict():
    from tokmo.values.quant_recurring import QuantDueDecisionV1

    calendar = _calendar_input()

    assert calendar.us_refresh_state == "success"
    assert calendar.us_cache_trade_date == date(2026, 8, 14)
    assert QuantDueDecisionV1.CALENDAR_BINDING_CONFLICT.value == (
        "CALENDAR_BINDING_CONFLICT"
    )
    assert not hasattr(calendar, "__dict__")
    with pytest.raises(FrozenInstanceError):
        calendar.mask_bound = False


def test_quant_tick_calendar_input_rejects_mask_verdict_divergence():
    with pytest.raises(ValueError, match="^quant_calendar_mask_verdict_conflict$"):
        _calendar_input(us_in_effective_mask=False)


@pytest.mark.parametrize(
    "overrides",
    (
        {"us_verdict_open": True, "us_verdict_reason": "open"},
        {
            "us_verdict_open": False,
            "us_verdict_reason": "stale_cache",
            "us_in_effective_mask": True,
        },
    ),
)
def test_quant_tick_calendar_input_rejects_failed_refresh_presenting_open(overrides):
    with pytest.raises(
        ValueError, match="^quant_calendar_failed_refresh_open_conflict$"
    ):
        _calendar_input(
            **{
                "us_refresh_state": "provider_failed",
                "us_refresh_succeeded": False,
                "mask_bound": False,
                "us_in_effective_mask": False,
                **overrides,
            }
        )


@pytest.mark.parametrize(
    "overrides,code",
    (
        (
            {"us_refresh_succeeded": False},
            "quant_calendar_refresh_flag_conflict",
        ),
        (
            {"us_verdict_open": True, "us_verdict_reason": "after_close"},
            "quant_calendar_verdict_flag_conflict",
        ),
        (
            {
                "mask_bound": False,
                "us_verdict_open": False,
                "us_verdict_reason": "after_close",
                "us_in_effective_mask": True,
            },
            "quant_calendar_unbound_mask_conflict",
        ),
    ),
)
def test_quant_tick_calendar_input_rejects_self_inconsistent_flags(overrides, code):
    with pytest.raises(ValueError, match=f"^{code}$"):
        _calendar_input(**overrides)


def test_due_context_rejects_formula_conflict_and_impossible_due_budget():
    from tokmo.values.quant_recurring import (
        QuantDueContextV1,
        QuantDueDecisionV1,
    )

    identity = _identity()
    symbols = ("AAPL", "MSFT", "NVDA", "TSLA")
    due = QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        identity.completed_session_date,
        identity.completed_session_date,
        symbols,
        90,
        7,
        7,
    )
    assert (due.membership_plan_starts, due.membership_starts_ceiling) == (7, 7)

    with pytest.raises(ValueError, match="^due_context_plan_starts_conflict$"):
        QuantDueContextV1(
            QuantDueDecisionV1.DUE,
            identity,
            identity.completed_session_date,
            identity.completed_session_date,
            symbols,
            90,
            6,
            7,
        )
    with pytest.raises(ValueError, match="^due_context_membership_budget$"):
        QuantDueContextV1(
            QuantDueDecisionV1.DUE,
            identity,
            identity.completed_session_date,
            identity.completed_session_date,
            symbols + ("VOO",),
            90,
            8,
            7,
        )

    refused = QuantDueContextV1(
        QuantDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE,
        None,
        identity.completed_session_date,
        identity.completed_session_date,
        symbols + ("VOO",),
        0,
        8,
        7,
    )
    paused = QuantDueContextV1(
        QuantDueDecisionV1.POLICY_ZERO,
        None,
        None,
        None,
        (),
        0,
        3,
        0,
    )
    assert refused.identity is paused.identity is None


def test_due_identity_binding_and_context_enforce_one_session_date_only_when_due():
    from tokmo.values.quant_recurring import (
        QUANT_ACQUISITION_NAMESPACE,
        QuantDueContextV1,
        QuantDueDecisionV1,
        QuantSessionBindingV1,
    )

    session_date = date(2026, 8, 14)
    identity = _identity(session_date)
    assert len(identity.identity_digest) == 64
    assert identity.acquisition_id.startswith(
        f"{QUANT_ACQUISITION_NAMESPACE}:{session_date.isoformat()}:"
    )
    assert QuantSessionBindingV1(identity).session_date == session_date

    due = QuantDueContextV1(
        QuantDueDecisionV1.DUE,
        identity,
        session_date,
        session_date,
        ("AAPL", "MSFT", "NVDA", "TSLA"),
        90,
        7,
        7,
    )
    assert due.identity is identity

    with pytest.raises(ValueError, match="due_context_session_date_conflict"):
        QuantDueContextV1(
            QuantDueDecisionV1.DUE,
            identity,
            date(2026, 8, 13),
            date(2026, 8, 13),
            ("AAPL",),
            90,
            4,
            7,
        )
    with pytest.raises(ValueError, match="due_context_ny_date_conflict"):
        QuantDueContextV1(
            QuantDueDecisionV1.DUE,
            identity,
            session_date,
            date(2026, 8, 13),
            ("AAPL",),
            90,
            4,
            7,
        )
    with pytest.raises(ValueError, match="non_due_context_must_not_carry_identity"):
        QuantDueContextV1(
            QuantDueDecisionV1.POLICY_ZERO,
            identity,
            session_date,
            session_date,
            (),
            0,
            3,
            0,
        )

    non_due = QuantDueContextV1(
        QuantDueDecisionV1.COHORT_UNPREDICTABLE,
        None,
        date(2001, 1, 1),
        date(2002, 1, 1),
        tuple(f"S{index}" for index in range(12)),
        -1,
        15,
        7,
    )
    assert non_due.identity is None


def test_membership_operation_plan_is_d_bound_deterministic_and_fixed_order():
    from tokmo.facts.quant_recurring_membership import membership_operation_plan
    from tokmo.values.cadence_policy import validate_cadence_operation

    symbols = ("TSLA", "AAPL", "NVDA", "MSFT")
    first_date = date(2026, 8, 13)
    second_date = date(2026, 8, 14)
    first = membership_operation_plan(symbols, first_date)
    second = membership_operation_plan(symbols, second_date)

    assert first == membership_operation_plan(symbols, first_date)
    assert all(
        membership_operation_plan(symbols, first_date) == first for _ in range(100)
    )
    assert tuple(operation for operation, _ in first) == tuple(
        operation for operation, _ in second
    )
    assert set(identity for _, identity in first).isdisjoint(
        identity for _, identity in second
    )
    assert first == (
        ("MEMBERSHIP_NASDAQ_LISTED", "membership:nasdaqlisted:2026-08-13"),
        ("MEMBERSHIP_OTHER_LISTED", "membership:otherlisted:2026-08-13"),
        ("MEMBERSHIP_SEC_REGISTRY", "membership:registry:2026-08-13"),
        ("MEMBERSHIP_SEC_ISSUER", "membership:issuer:2026-08-13:AAPL"),
        ("MEMBERSHIP_SEC_ISSUER", "membership:issuer:2026-08-13:MSFT"),
        ("MEMBERSHIP_SEC_ISSUER", "membership:issuer:2026-08-13:NVDA"),
        ("MEMBERSHIP_SEC_ISSUER", "membership:issuer:2026-08-13:TSLA"),
    )
    assert all(
        validate_cadence_operation(operation, identity) == "MEMBERSHIP"
        for operation, identity in first
    )
    with pytest.raises(TypeError):
        membership_operation_plan(symbols)


@pytest.mark.parametrize(
    "symbols,session_date,code",
    (
        ([], date(2026, 8, 14), "symbols"),
        ((), date(2026, 8, 14), "symbols"),
        (tuple(f"S{index}" for index in range(9)), date(2026, 8, 14), "symbols"),
        (("AAPL", "AAPL"), date(2026, 8, 14), "symbols"),
        (("aapl",), date(2026, 8, 14), "symbols"),
        (("A" * 33,), date(2026, 8, 14), "symbols"),
        (("AAPL",), datetime(2026, 8, 14), "session_date"),
        (("AAPL",), date(2006, 12, 31), "session_date"),
        (("AAPL",), None, "session_date"),
        (("AAPL",), "2026-08-14", "session_date"),
    ),
)
def test_membership_operation_plan_fails_closed_before_returning_pairs(
    symbols,
    session_date,
    code,
):
    from tokmo.facts.quant_recurring_membership import membership_operation_plan

    with pytest.raises(ValueError, match=f"^{code}$"):
        membership_operation_plan(symbols, session_date)


def test_membership_native_identity_rejects_invalid_endpoint_pairing():
    from tokmo.facts.quant_recurring_membership import membership_native_identity

    session_date = date(2026, 8, 14)
    with pytest.raises(ValueError, match="^endpoint$"):
        membership_native_identity(endpoint="unknown", session_date=session_date)
    with pytest.raises(ValueError, match="^endpoint$"):
        membership_native_identity(endpoint="issuer", session_date=session_date)
    with pytest.raises(ValueError, match="^endpoint$"):
        membership_native_identity(
            endpoint="registry", session_date=session_date, symbol="AAPL"
        )


def test_quant_candle_identity_and_deadline_arithmetic_delegate_to_closed_contracts():
    from tokmo.facts.quant_recurring_membership import (
        quant_candle_native_identity,
        sufficient_deadline,
    )
    from tokmo.values.cadence_policy import CADENCE_QUANT_SHADOW_POLICY_V1
    from tokmo.values.session_gate import Market

    session_date = date(2026, 8, 14)
    assert (
        quant_candle_native_identity(
            market=Market.US,
            symbol="AAPL",
            session_date=session_date,
            page_index=2,
        )
        == "candles:qt:US:AAPL:2026-08-14:2"
    )
    scheduled = datetime(2026, 8, 14, 20, 0, tzinfo=UTC)
    assert (
        sufficient_deadline(
            policy=CADENCE_QUANT_SHADOW_POLICY_V1,
            scheduled_for_utc=scheduled,
            observed_now_utc=scheduled + timedelta(seconds=90),
            planned_starts=15,
        )
        is True
    )
    assert (
        sufficient_deadline(
            policy=CADENCE_QUANT_SHADOW_POLICY_V1,
            scheduled_for_utc=scheduled,
            observed_now_utc=scheduled + timedelta(seconds=91),
            planned_starts=15,
        )
        is False
    )
