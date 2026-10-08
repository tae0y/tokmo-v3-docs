"""Shared production orchestrator for the KR/US quant market profiles (§6).

Consumes only the closed profile registry's typed contracts: due evaluation
per profile in fixed order (KR, US), at most one DUE profile per tick, the
same claim/finish/reconcile discipline as the V1 lane against the fresh
store, and profile-prefixed truthful stage reasons. This module imports no
live transports and no source parsers — acquisition goes through the
injected deps seams via the profile runtimes.
"""

from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from tokmo.facts.quant_kr_membership import predict_kr_personal_cohort
from tokmo.facts.quant_recurring_membership import (
    DEADLINE_MARGIN_SECONDS,
    PER_START_BUDGET_SECONDS,
    predict_us_personal_cohort,
    sufficient_deadline,
)
from tokmo.programs.calendar_refresh import MarketRefreshState
from tokmo.programs.market_regime_shadow import (
    resolve_completed_session_by_market,
)
from tokmo.programs.quant_profile_kr import (
    KrProfileMembershipOutcomeV1,
    run_kr_profile_membership,
    run_kr_profile_shadow,
)
from tokmo.programs.quant_profile_us import (
    UsProfileExecutionV1,
    run_us_profile_membership,
    run_us_profile_shadow,
)
from tokmo.records.current_holdings_store import CurrentHoldingsStore
from tokmo.records.quant_profile_receipts import QuantProfileReceiptStore
from tokmo.records.regular_session_history import RegularSessionHistoryStore
from tokmo.values.quant_profiles import (
    KR_PROFILE_ID,
    QUANT_PROFILE_EXECUTION_ORDER,
    QUANT_PROFILE_REGISTRY,
    QuantProfileDueContextV1,
    QuantProfileDueDecisionV1,
    QuantProfileDueIdentityV1,
)
from tokmo.values.quant_recurring import QuantTerminalStateV1
from tokmo.values.session_gate import MARKET_TIMEZONE, Market


@dataclass(frozen=True, slots=True)
class ProfileCalendarInputV1:
    """One market's calendar posture for profile due evaluation."""

    refresh_succeeded: bool
    in_effective_mask: bool
    mask_bound: bool
    cache_trade_date: date | None
    official_close: datetime | None


@dataclass(frozen=True, slots=True)
class ProfilesMembershipOutcomeV1:
    status: str
    reason: str
    executed_profile_id: str | None
    kr: KrProfileMembershipOutcomeV1 | None
    us: UsProfileExecutionV1 | None
    receipts: QuantProfileReceiptStore | None


def _parse_boundary(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("calendar_boundary")
    return parsed


def profile_calendar_inputs(*, results, mask, cache_store) -> dict:
    """Both markets' calendar inputs from one cache read; fail-closed."""
    try:
        cache = cache_store.read()
    except Exception:
        cache = None
    inputs = {}
    for market in Market:
        result = results.get(market)
        if result is None:
            raise ValueError("profiles_calendar_input_missing")
        entry = None if cache is None else (
            cache.kr if market is Market.KR else cache.us
        )
        trade_date = None
        close = None
        if entry is not None:
            try:
                trade_date = date.fromisoformat(entry.trade_date)
            except (TypeError, ValueError):
                trade_date = None
            regular = entry.sessions.get("regularMarket")
            if regular is not None:
                try:
                    close = _parse_boundary(regular.end)
                except (TypeError, ValueError):
                    close = None
        inputs[market] = ProfileCalendarInputV1(
            refresh_succeeded=result.state is MarketRefreshState.SUCCESS,
            in_effective_mask=mask is not None and market in mask.markets,
            mask_bound=mask is not None,
            cache_trade_date=trade_date,
            official_close=close,
        )
    return inputs


def _context(
    profile, decision, *, ceiling, completed=None, local=None, predicted=(),
    margin=0, plan=None,
) -> QuantProfileDueContextV1:
    return QuantProfileDueContextV1(
        profile_id=profile.profile_id,
        decision=decision,
        identity=None,
        completed_session_date=completed,
        local_session_date=local,
        predicted_symbols=predicted,
        deadline_margin_seconds=margin,
        membership_plan_starts=(
            profile.membership_plan_starts(len(predicted))
            if plan is None else plan
        ),
        membership_starts_ceiling=ceiling,
    )


def evaluate_profile_due(
    *, root, deps, policy, tick, profile,
    calendar: ProfileCalendarInputV1, receipts_loader,
) -> QuantProfileDueContextV1:
    """Generalized due evaluation (§6.1); zero wires, zero claims."""
    limits = policy.request_limits
    ceiling = limits.membership_starts
    if limits.membership_starts <= 0 or limits.quant_daily_candle_starts <= 0:
        return _context(
            profile, QuantProfileDueDecisionV1.POLICY_ZERO, ceiling=ceiling,
        )
    slot = tick.scheduled_for_utc
    local_date = slot.astimezone(
        ZoneInfo(MARKET_TIMEZONE[profile.market])
    ).date()
    if (
        not calendar.refresh_succeeded
        or not calendar.mask_bound
        or calendar.cache_trade_date != local_date
        or calendar.official_close is None
        or slot < calendar.official_close
    ):
        return _context(
            profile, QuantProfileDueDecisionV1.NO_COMPLETED_SESSION,
            ceiling=ceiling, local=local_date,
        )
    try:
        history = RegularSessionHistoryStore(
            Path(root) / "ops" / "regular_session_history.sqlite3",
            clock=deps.clock,
        )
        today_row = history.read(profile.market, local_date)
        completed = resolve_completed_session_by_market(
            scheduled_for_utc=slot,
            market=profile.market,
            today_row=today_row,
            history_store=history,
            official_close=calendar.official_close,
        )
    except Exception:
        completed = None
    if completed is None:
        return _context(
            profile, QuantProfileDueDecisionV1.NO_COMPLETED_SESSION,
            ceiling=ceiling, local=local_date,
        )
    if completed != local_date:
        return _context(
            profile, QuantProfileDueDecisionV1.LOCAL_DATE_MISMATCH,
            ceiling=ceiling, completed=completed, local=local_date,
        )
    if not calendar.in_effective_mask:
        return _context(
            profile, QuantProfileDueDecisionV1.NO_POST_CLOSE_OPEN_WINDOW,
            ceiling=ceiling, completed=completed, local=local_date,
        )
    identity = QuantProfileDueIdentityV1(
        profile.profile_id,
        policy.policy_version,
        profile.market,
        completed,
        profile.universe_policy_version,
    )
    store = receipts_loader()
    row = None if store is None else store.read_due(identity.identity_digest)
    if row is not None:
        # B1: stale CLAIMED rows were already reconciled once this tick
        # before due evaluation, so a surviving CLAIMED row is genuinely
        # in flight — neither re-claimed nor called terminal, zero wire.
        if row.state is QuantTerminalStateV1.CLAIMED:
            return _context(
                profile, QuantProfileDueDecisionV1.CLAIM_IN_FLIGHT,
                ceiling=ceiling, completed=completed, local=local_date,
            )
        return _context(
            profile, QuantProfileDueDecisionV1.ALREADY_TERMINAL,
            ceiling=ceiling, completed=completed, local=local_date,
        )
    try:
        prior = CurrentHoldingsStore(
            Path(root) / "stores" / "current_holdings.json",
            clock=deps.clock,
        ).read()
    except Exception:
        prior = None
    predicted = (
        predict_kr_personal_cohort(prior)
        if profile.profile_id == KR_PROFILE_ID
        else predict_us_personal_cohort(prior)
    )
    if len(predicted) < 1:
        return _context(
            profile, QuantProfileDueDecisionV1.COHORT_UNPREDICTABLE,
            ceiling=ceiling, completed=completed, local=local_date,
        )
    if len(predicted) > profile.cohort_max:
        # Typed fail-closed overflow: zero planned starts, no cohort carried,
        # no claim, no wire (§7).
        return _context(
            profile,
            QuantProfileDueDecisionV1.PROFILE_COHORT_EXCEEDS_POLICY,
            ceiling=ceiling, completed=completed, local=local_date, plan=0,
        )
    plan_starts = profile.membership_plan_starts(len(predicted))
    if plan_starts > ceiling:
        return _context(
            profile,
            QuantProfileDueDecisionV1.MEMBERSHIP_BUDGET_UNSATISFIABLE,
            ceiling=ceiling, completed=completed, local=local_date,
            predicted=predicted,
        )
    planned_starts = profile.quant_due_max_increment(len(predicted))
    observed_now = deps.clock()
    remaining = policy.child_timeout_seconds - (
        observed_now - slot
    ).total_seconds()
    required = planned_starts * PER_START_BUDGET_SECONDS + DEADLINE_MARGIN_SECONDS
    margin = int(remaining - required)
    if not sufficient_deadline(
        policy=policy,
        scheduled_for_utc=slot,
        observed_now_utc=observed_now,
        planned_starts=planned_starts,
    ):
        return _context(
            profile, QuantProfileDueDecisionV1.INSUFFICIENT_DEADLINE,
            ceiling=ceiling, completed=completed, local=local_date,
            predicted=predicted, margin=margin,
        )
    return QuantProfileDueContextV1(
        profile_id=profile.profile_id,
        decision=QuantProfileDueDecisionV1.DUE,
        identity=identity,
        completed_session_date=completed,
        local_session_date=local_date,
        predicted_symbols=predicted,
        deadline_margin_seconds=margin,
        membership_plan_starts=plan_starts,
        membership_starts_ceiling=ceiling,
    )


def _existing_profile_receipt_store(path: Path):
    if not path.is_file():
        return None
    return QuantProfileReceiptStore(path)


def run_profiles_membership(
    *, root, deps, policy, tick, executor, token, results, mask,
    cache_store, now,
) -> ProfilesMembershipOutcomeV1:
    """Evaluate KR then US; execute at most the first DUE profile (§6.2)."""
    path = Path(root) / "ops" / "quant_profile_receipts.sqlite3"
    # B1: reconcile stale CLAIMED rows exactly once per tick, before any
    # profile due evaluation, so crashed prior claims become terminal
    # UNKNOWN/stale_claim and are then read truthfully below.
    existing = _existing_profile_receipt_store(path)
    if existing is not None:
        existing.reconcile_stale(now=now, policy=policy)
    holder = {"store": existing}

    def loader():
        return holder["store"]

    try:
        calendars = profile_calendar_inputs(
            results=results, mask=mask, cache_store=cache_store,
        )
    except ValueError:
        return ProfilesMembershipOutcomeV1(
            "skipped", "calendar_binding_conflict", None, None, None, None,
        )
    reasons = []
    for profile_id in QUANT_PROFILE_EXECUTION_ORDER:
        profile = QUANT_PROFILE_REGISTRY[profile_id]
        prefix = profile.market.value.lower()
        context = evaluate_profile_due(
            root=root,
            deps=deps,
            policy=policy,
            tick=tick,
            profile=profile,
            calendar=calendars[profile.market],
            receipts_loader=loader,
        )
        if context.decision is not QuantProfileDueDecisionV1.DUE:
            reasons.append(f"{prefix}:{context.decision.value.lower()}")
            continue
        store = loader()
        if store is None:
            # fresh store: created only on the first DUE claim; it has no
            # rows, so the once-per-tick reconcile above stays exact.
            store = QuantProfileReceiptStore(path)
            holder["store"] = store
        if profile_id == KR_PROFILE_ID:
            outcome = run_kr_profile_membership(
                deps=deps,
                policy=policy,
                tick=tick,
                executor=executor,
                token=token,
                context=context,
                receipts=store,
            )
            return ProfilesMembershipOutcomeV1(
                outcome.status, f"{prefix}:{outcome.reason}",
                profile_id, outcome, None, store,
            )
        execution = run_us_profile_membership(
            root=root,
            deps=deps,
            policy=policy,
            tick=tick,
            executor=executor,
            context=context,
            receipts=store,
        )
        return ProfilesMembershipOutcomeV1(
            execution.membership.status,
            f"{prefix}:{execution.membership.reason}",
            profile_id, None, execution, store,
        )
    return ProfilesMembershipOutcomeV1(
        "skipped", ",".join(reasons), None, None, None, None,
    )


def run_profiles_shadow(
    *, root, deps, policy, tick, executor, token,
    execution: ProfilesMembershipOutcomeV1,
) -> tuple[str, str]:
    """Complete the tick's one executed profile; profile-prefixed reasons."""
    if execution.executed_profile_id is None or execution.status != "ok":
        return "skipped", execution.reason
    profile = QUANT_PROFILE_REGISTRY[execution.executed_profile_id]
    prefix = profile.market.value.lower()
    if execution.executed_profile_id == KR_PROFILE_ID:
        status, reason = run_kr_profile_shadow(
            root=Path(root),
            deps=deps,
            policy=policy,
            tick=tick,
            executor=executor,
            token=token,
            membership=execution.kr,
            receipts=execution.receipts,
        )
    else:
        status, reason = run_us_profile_shadow(
            root=Path(root),
            deps=deps,
            policy=policy,
            tick=tick,
            executor=executor,
            token=token,
            execution=execution.us,
        )
    return status, f"{prefix}:{reason}"
