"""Macro-owned FRED due/admission policy and optional employment projection.

Delta SDS §4/§7. This module owns the whole admission decision and imports
only the routine query vocabulary: the query port and the query cache are
injected, so no Facts, Records, live transport or R1-R4 campaign symbol is
reachable from here.

Decision order (normative):

1. corrupt/unreadable local state → ``UNAVAILABLE_LOCAL_STATE``, wire 0;
2. absent/invalid/expired schedule → ``UNAVAILABLE_RELEASE_SCHEDULE``, wire 0;
3. before release+grace → ``NOT_DUE_BEFORE_RELEASE``, wire 0, whether or not
   an older success exists;
4. an active claim or a failure settled less than the cooldown ago →
   ``NOT_DUE_COOLDOWN``, wire 0;
5. no success at all → ``DUE_MISSING``;
6. cached query identity mismatch in an eligible slot → ``DUE_QUERY_CHANGED``;
7. a success that does not cover this release slot → ``DUE_RELEASE``, which
   prior-slot freshness must not suppress;
8. a current-slot success younger than ``FRESH_FOR`` → ``NOT_DUE_FRESH``;
9. a current-slot success at least ``FRESH_FOR`` old whose release schedule
   is still valid → ``NOT_DUE_REVISION_CURRENT``, wire 0; the stored success
   is the current revision for as long as the release schedule that described
   it remains valid. ``DUE_STALE`` is retained in the enum and ``DUE_STATES``
   for compatibility but is unreachable from ``_admit`` under the current
   caller contract.

There is no pre-query same-revision branch: ``revision_identity`` is only
observed after the Facts adapter answers, so using it to suppress the call
that would produce it would be circular.

Coverage is decided separately from the wire decision, and never presents a
prior-slot or expired fact as current: the enrichment is ``USED`` only for a
fact just fetched, or a stored success that covers this exact release slot,
carries this exact query identity and is still inside the freshness
interval, or is a revision-current success whose release schedule is still
valid. Every other outcome omits the FRED contribution alone; absence is
never mapped to a direction, and a MISSING observation is never read as
zero.
"""

from dataclasses import dataclass
from datetime import date, datetime, timedelta

from tokmo.values.fred_query import (
    DUE_STATES,
    FAILURE_COOLDOWN,
    FRESH_FOR,
    FredCoverageV1,
    FredDueDecisionV1,
    FredDueStateV1,
    FredQueryCacheCorruption,
    FredQueryFailureCodeV1,
    FredQueryFailureV1,
    FredReleaseScheduleV1,
    FredSeriesFactV1,
    FredSeriesQueryV1,
    MacroFredInputV1,
    SERIES_PAYEMS,
    compute_macro_provenance_digest,
    observation_direction,
)

WINDOW_MONTHS_BEFORE_TERMINAL = 2


def previous_calendar_month(year: int, month: int) -> tuple[int, int]:
    """Explicit, year-aware. A bare ``month - 1`` underflows every January."""
    if type(year) is not int or type(month) is not int or not 1 <= month <= 12:
        raise ValueError("invalid_calendar_month")
    if month == 1:
        return year - 1, 12
    return year, month - 1


def last_day_of_month(year: int, month: int) -> date:
    following_year = year + 1 if month == 12 else year
    following_month = 1 if month == 12 else month + 1
    return date(following_year, following_month, 1) - timedelta(days=1)


def derive_query_window_v1(*, release_date: date,
                           as_of: datetime) -> tuple[FredSeriesQueryV1, tuple]:
    """Derive the one query window and the terminal month it is bounded by.

    ``expected_terminal_month`` is computed here exactly once and is consumed
    both by the window and — through ``observation_end``, which is by
    construction its last day — by the §4.7 guard. The wall clock never enters
    the window, so one release slot always yields one ``query_identity``.
    """
    if type(release_date) is not date:
        raise ValueError("invalid_release_date")
    if (type(as_of) is not datetime or as_of.tzinfo is None
            or as_of.utcoffset() is None
            or as_of.utcoffset().total_seconds() != 0):
        raise ValueError("aware_utc_required")
    expected_terminal_month = previous_calendar_month(release_date.year,
                                                      release_date.month)
    window = expected_terminal_month
    for _step in range(WINDOW_MONTHS_BEFORE_TERMINAL):
        window = previous_calendar_month(*window)
    query = FredSeriesQueryV1(SERIES_PAYEMS, date(window[0], window[1], 1),
                              last_day_of_month(*expected_terminal_month), as_of)
    return query, expected_terminal_month


@dataclass(frozen=True)
class _Admission:
    decision: FredDueDecisionV1
    cached: object
    usable: bool


def _require_aware_utc(value: object) -> datetime:
    if (type(value) is not datetime or value.tzinfo is None
            or value.utcoffset() is None
            or value.utcoffset().total_seconds() != 0):
        raise ValueError("aware_utc_required")
    return value


def _schedule_is_valid(query: FredSeriesQueryV1, schedule: object,
                       now: datetime) -> bool:
    return (isinstance(schedule, FredReleaseScheduleV1)
            and schedule.series_id == query.series_id
            and now <= schedule.valid_until)


def _admit(*, query: FredSeriesQueryV1, schedule: object, cache,
           now: datetime) -> _Admission:
    if not isinstance(query, FredSeriesQueryV1):
        raise ValueError("query_required")
    _require_aware_utc(now)
    identity = query.query_identity
    if not _schedule_is_valid(query, schedule, now):
        try:
            cache.read_success_for_series(query.series_id, now=now)
        except FredQueryCacheCorruption:
            return _Admission(FredDueDecisionV1(
                FredDueStateV1.UNAVAILABLE_LOCAL_STATE, identity, None),
                None, False)
        return _Admission(FredDueDecisionV1(
            FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE, identity, None),
            None, False)
    slot = schedule.release_slot
    try:
        cached = cache.read_success_for_series(query.series_id, now=now)
        attempts = cache.read_attempt_state(identity, slot, now=now)
    except FredQueryCacheCorruption:
        return _Admission(FredDueDecisionV1(
            FredDueStateV1.UNAVAILABLE_LOCAL_STATE, identity, slot), None, False)

    def decided(state: FredDueStateV1) -> _Admission:
        usable = (cached is not None
                  and cached.query_identity == identity
                  and cached.due_slot == slot
                  and (now - cached.fetched_at < FRESH_FOR
                       or state is FredDueStateV1.NOT_DUE_REVISION_CURRENT))
        return _Admission(FredDueDecisionV1(state, identity, slot), cached, usable)

    if now < schedule.eligible_from:
        return _Admission(FredDueDecisionV1(
            FredDueStateV1.NOT_DUE_BEFORE_RELEASE, identity, slot), cached, False)
    if attempts.active_started_at is not None:
        return decided(FredDueStateV1.NOT_DUE_COOLDOWN)
    if (attempts.last_failure_at is not None
            and now - attempts.last_failure_at < FAILURE_COOLDOWN):
        return decided(FredDueStateV1.NOT_DUE_COOLDOWN)
    if cached is None:
        return decided(FredDueStateV1.DUE_MISSING)
    if cached.query_identity != identity:
        return decided(FredDueStateV1.DUE_QUERY_CHANGED)
    if cached.due_slot != slot:
        return decided(FredDueStateV1.DUE_RELEASE)
    if now - cached.fetched_at < FRESH_FOR:
        return decided(FredDueStateV1.NOT_DUE_FRESH)
    return decided(FredDueStateV1.NOT_DUE_REVISION_CURRENT)


def evaluate_fred_due_v1(*, query, schedule, cache, now) -> FredDueDecisionV1:
    """Decide query/reuse/not-yet-released/fail-closed from passed state."""
    return _admit(query=query, schedule=schedule, cache=cache, now=now).decision


def run_macro_fred_enrichment_v1(*, query, schedule, cache, query_port,
                                 clock) -> MacroFredInputV1:
    now = _require_aware_utc(clock())
    admission = _admit(query=query, schedule=schedule, cache=cache, now=now)
    decision = admission.decision
    fetched = None
    failure = None
    starts = 0
    if decision.state in DUE_STATES:
        claim = cache.claim(decision.query_identity, decision.due_slot, now=now)
        if claim is None:
            # A concurrent invocation holds the single-flight claim.
            decision = FredDueDecisionV1(FredDueStateV1.NOT_DUE_COOLDOWN,
                                         decision.query_identity, decision.due_slot)
        else:
            starts = 1
            outcome = query_port.query_series(query)
            if isinstance(outcome, FredSeriesFactV1):
                cache.settle_success(claim, outcome, now=now)
                fetched = outcome
            elif isinstance(outcome, FredQueryFailureV1):
                cache.settle_failure(claim, outcome.code, now=now)
                failure = outcome
            else:
                raise ValueError("closed_query_outcome_required")
    return _project(query=query, schedule=schedule, decision=decision,
                    admission=admission, fetched=fetched, failure=failure,
                    starts=starts)


def _project(*, query, schedule, decision, admission, fetched, failure,
             starts) -> MacroFredInputV1:
    fact = fetched
    if fact is None and admission.usable:
        fact = admission.cached.fact
    if fact is not None:
        coverage = FredCoverageV1.USED
    elif failure is not None:
        coverage = (FredCoverageV1.OMITTED_INCOMPLETE
                    if failure.code is FredQueryFailureCodeV1.INCOMPLETE
                    else FredCoverageV1.OMITTED_UNAVAILABLE)
    elif decision.state in (FredDueStateV1.UNAVAILABLE_LOCAL_STATE,
                            FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE):
        coverage = FredCoverageV1.OMITTED_UNAVAILABLE
    else:
        coverage = FredCoverageV1.OMITTED_NOT_DUE
    current = prior = direction = None
    revision_identity = None
    if fact is not None:
        revision_identity = fact.revision_identity
        current, prior, direction = observation_direction(fact.observations)
    schedule_digest = (schedule.provenance_digest
                       if isinstance(schedule, FredReleaseScheduleV1) else None)
    return MacroFredInputV1(
        query.series_id, coverage, decision.state, starts,
        None if current is None else current.value,
        None if current is None else current.observation_date,
        None if prior is None else prior.value,
        None if prior is None else prior.observation_date,
        direction, decision.query_identity, revision_identity,
        compute_macro_provenance_digest(
            query_identity=decision.query_identity,
            revision_identity=revision_identity, due_slot=decision.due_slot,
            due_state=decision.state, coverage=coverage,
            schedule_provenance_digest=schedule_digest))
