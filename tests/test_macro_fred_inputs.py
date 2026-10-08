"""Delta SDS §4/§7 acceptance 4-11: Macro-owned due order and projection.

The Program owns the whole admission decision, claims the due slot in
Records before calling the injected port, and treats FRED as optional
employment enrichment: failure, missing, not-yet-released or incomplete
data omits only this contribution and never blocks or changes the existing
Market Regime core.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tokmo.fred_query_composition import (
    FredFixtureTransportOutcomeV1,
    build_g1_fixture_fred_query_deps_v1,
)
from tokmo.programs.macro_fred_inputs import (
    evaluate_fred_due_v1,
    run_macro_fred_enrichment_v1,
)
from tokmo.values.fred_query import (
    FAILURE_COOLDOWN,
    FRESH_FOR,
    LOCAL_STATE_UNKNOWN_START,
    SERIES_PAYEMS,
    STARTED_LEASE,
    FredCoverageV1,
    FredDirectionV1,
    FredDueStateV1,
    FredQueryCacheCorruption,
    FredReleaseScheduleV1,
    FredSeriesQueryV1,
    MacroFredInputV1,
)

UTC = timezone.utc
RELEASE_AT = datetime(2026, 8, 7, 12, 30, tzinfo=UTC)
ELIGIBLE = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
SLOT = "PAYEMS:2026-08-07"
NEXT_RELEASE_AT = datetime(2026, 9, 4, 12, 30, tzinfo=UTC)
NEXT_SLOT = "PAYEMS:2026-09-04"

SCHEDULE = FredReleaseScheduleV1(SERIES_PAYEMS, RELEASE_AT, SLOT,
                                 NEXT_RELEASE_AT, "b" * 64)
NEXT_SCHEDULE = FredReleaseScheduleV1(
    SERIES_PAYEMS, NEXT_RELEASE_AT, NEXT_SLOT,
    datetime(2026, 10, 2, 12, 30, tzinfo=UTC), "c" * 64)

JUNE = {"realtime_start": "2026-07-02", "realtime_end": "9999-12-31",
        "date": "2026-06-01", "value": "150900"}
JULY = {"realtime_start": "2026-08-07", "realtime_end": "9999-12-31",
        "date": "2026-07-01", "value": "151000"}


class _Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


def _query(**overrides):
    fields = dict(series_id=SERIES_PAYEMS, observation_start=date(2026, 6, 1),
                  observation_end=date(2026, 7, 31), as_of=ELIGIBLE)
    fields.update(overrides)
    return FredSeriesQueryV1(**fields)


def _body(rows, *, count=None, limit=2, start="2026-06-01", end="2026-07-31"):
    return json.dumps({
        "realtime_start": "1776-07-04", "realtime_end": "9999-12-31",
        "observation_start": start, "observation_end": end,
        "units": "lin", "output_type": 4, "file_type": "json",
        "order_by": "observation_date", "sort_order": "asc",
        "count": len(rows) if count is None else count,
        "offset": 0, "limit": limit, "observations": list(rows)}).encode()


def _success(rows=(JUNE, JULY), **kwargs):
    return FredFixtureTransportOutcomeV1("success", _body(list(rows), **kwargs))


def _deps(root, clock, *outcomes):
    return build_g1_fixture_fred_query_deps_v1(
        root=root, outcomes=tuple(outcomes), clock=clock)


def _run(deps, clock, *, query=None, schedule=SCHEDULE):
    return run_macro_fred_enrichment_v1(
        query=query or _query(), schedule=schedule, cache=deps.cache,
        query_port=deps.port, clock=clock)


def _attempts(root):
    connection = sqlite3.connect(root / "fred-query-cache.sqlite3")
    try:
        return connection.execute(
            "SELECT query_identity,due_slot,attempt_seq,state,failure_code "
            "FROM query_attempt ORDER BY due_slot,attempt_seq").fetchall()
    finally:
        connection.close()


def test_before_release_grace_starts_zero_wires_even_with_an_older_success(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    assert _run(deps, clock).coverage is FredCoverageV1.USED

    clock.value = NEXT_RELEASE_AT - timedelta(minutes=1)
    later = _deps(root, clock)
    result = _run(later, clock, schedule=NEXT_SCHEDULE)
    assert result.due_state is FredDueStateV1.NOT_DUE_BEFORE_RELEASE
    assert result.coverage is FredCoverageV1.OMITTED_NOT_DUE
    assert result.provider_starts == 0 and later.transport.requests == []
    assert result.current_value is None and result.direction is None


def test_missing_success_after_release_grace_starts_exactly_one_wire(tmp_path):
    clock = _Clock(ELIGIBLE)
    deps = _deps(tmp_path / "fred-query", clock, _success())
    assert evaluate_fred_due_v1(query=_query(), schedule=SCHEDULE,
                                cache=deps.cache, now=ELIGIBLE
                                ).state is FredDueStateV1.DUE_MISSING
    result = _run(deps, clock)
    assert isinstance(result, MacroFredInputV1)
    assert result.due_state is FredDueStateV1.DUE_MISSING
    assert result.coverage is FredCoverageV1.USED
    assert result.provider_starts == 1 and len(deps.transport.requests) == 1
    assert result.current_value == Decimal("151000")
    assert result.current_date == date(2026, 7, 1)
    assert result.prior_value == Decimal("150900")
    assert result.direction is FredDirectionV1.EXPANDING
    assert result.series_id == SERIES_PAYEMS
    assert len(result.revision_identity) == 64


def test_repeated_tick_and_restart_inside_freshness_start_zero_wires(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    first = _run(deps, clock)

    clock.value = ELIGIBLE + timedelta(hours=1)
    repeated = _run(deps, clock)
    assert repeated.due_state is FredDueStateV1.NOT_DUE_FRESH
    assert repeated.coverage is FredCoverageV1.USED
    assert repeated.provider_starts == 0 and len(deps.transport.requests) == 1
    assert repeated.revision_identity == first.revision_identity

    clock.value = ELIGIBLE + FRESH_FOR - timedelta(seconds=1)
    restarted = _deps(root, clock)
    result = _run(restarted, clock)
    assert result.due_state is FredDueStateV1.NOT_DUE_FRESH
    assert result.provider_starts == 0 and restarted.transport.requests == []
    assert [row[2] for row in _attempts(root)] == [1]


def test_expired_freshness_with_valid_schedule_is_revision_current_not_stale(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    first = _run(deps, clock)

    clock.value = ELIGIBLE + FRESH_FOR
    refresh = _deps(root, clock, _success())
    result = _run(refresh, clock)
    assert result.due_state is FredDueStateV1.NOT_DUE_REVISION_CURRENT
    assert result.provider_starts == 0 and refresh.transport.requests == []
    assert result.coverage is FredCoverageV1.USED
    assert result.revision_identity == first.revision_identity
    stored = refresh.cache.read_success_for_series(SERIES_PAYEMS, now=clock.value)
    assert stored.fetched_at == ELIGIBLE
    assert stored.due_slot == SLOT
    assert [(row[2], row[3]) for row in _attempts(root)] == [(1, "SUCCEEDED")]


def test_new_release_slot_is_due_even_when_the_prior_slot_is_still_fresh(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    _run(deps, clock)

    clock.value = NEXT_RELEASE_AT - timedelta(hours=2)
    refresh = _deps(root, clock, _success())
    assert _run(refresh, clock).due_state is FredDueStateV1.NOT_DUE_REVISION_CURRENT

    clock.value = NEXT_RELEASE_AT + timedelta(minutes=15)
    released = _deps(root, clock, _success())
    result = _run(released, clock, schedule=NEXT_SCHEDULE)
    assert result.due_state is FredDueStateV1.DUE_RELEASE
    assert result.provider_starts == 1 and len(released.transport.requests) == 1
    slots = {row[1] for row in _attempts(root)}
    assert slots == {SLOT, NEXT_SLOT}


def test_changed_query_identity_in_an_eligible_slot_is_due(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    _run(deps, clock)

    widened = _query(observation_start=date(2026, 5, 1))
    clock.value = ELIGIBLE + timedelta(hours=1)
    changed = _deps(root, clock, _success(limit=3, start="2026-05-01"))
    result = _run(changed, clock, query=widened)
    assert result.due_state is FredDueStateV1.DUE_QUERY_CHANGED
    assert result.provider_starts == 1
    assert result.query_identity == widened.query_identity


def test_old_success_is_retained_and_reused_as_revision_current(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    first = _run(deps, clock)

    clock.value = ELIGIBLE + FRESH_FOR
    failing = _deps(root, clock)
    failed = _run(failing, clock)
    assert failed.due_state is FredDueStateV1.NOT_DUE_REVISION_CURRENT
    assert failed.coverage is FredCoverageV1.USED
    assert failed.provider_starts == 0
    assert failed.current_value is not None
    stored = failing.cache.read_success_for_series(SERIES_PAYEMS, now=clock.value)
    assert stored.revision_identity == first.revision_identity
    assert stored.fetched_at == ELIGIBLE

    # After revision-aware reuse, no wire was attempted, so no failure was
    # recorded and the cooldown path is unreachable for a same-slot success.
    # The direct reopened-after-cooldown test covers the synthetic cooldown
    # guard and its OMITTED_NOT_DUE projection.


def test_crash_after_claim_leases_then_reconciles_then_cools_down(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    crashed = _deps(root, clock)
    crashed.cache.claim(_query().query_identity, SLOT, now=ELIGIBLE)

    clock.value = ELIGIBLE + STARTED_LEASE
    inside = _deps(root, clock, _success())
    held = _run(inside, clock)
    assert held.due_state is FredDueStateV1.NOT_DUE_COOLDOWN
    assert held.provider_starts == 0 and inside.transport.requests == []
    assert [(row[2], row[3]) for row in _attempts(root)] == [(1, "STARTED")]

    clock.value = ELIGIBLE + STARTED_LEASE + timedelta(seconds=1)
    reconciled = _deps(root, clock, _success())
    result = _run(reconciled, clock)
    assert result.due_state is FredDueStateV1.NOT_DUE_COOLDOWN
    assert result.provider_starts == 0 and reconciled.transport.requests == []
    assert [(row[2], row[3], row[4]) for row in _attempts(root)] == [
        (1, "FAILED", LOCAL_STATE_UNKNOWN_START)]

    clock.value = clock.value + FAILURE_COOLDOWN
    released = _deps(root, clock, _success())
    admitted = _run(released, clock)
    assert admitted.due_state is FredDueStateV1.DUE_MISSING
    assert admitted.provider_starts == 1 and len(released.transport.requests) == 1
    assert [(row[2], row[3], row[4]) for row in _attempts(root)] == [
        (1, "FAILED", LOCAL_STATE_UNKNOWN_START), (2, "SUCCEEDED", None)]


def test_an_active_claim_from_a_concurrent_invocation_starts_zero_wires(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    deps.cache.claim(_query().query_identity, SLOT, now=ELIGIBLE)
    result = _run(deps, clock)
    assert result.due_state is FredDueStateV1.NOT_DUE_COOLDOWN
    assert result.provider_starts == 0 and deps.transport.requests == []
    assert len(_attempts(root)) == 1


def test_corrupt_local_state_omits_fred_and_starts_zero_wires(tmp_path):
    root = tmp_path / "fred-query"
    clock = _Clock(ELIGIBLE)
    deps = _deps(root, clock, _success())
    _run(deps, clock)
    connection = sqlite3.connect(root / "fred-query-cache.sqlite3")
    try:
        connection.execute("UPDATE query_success SET fact_json=?", (b'{"x":1}\n',))
        connection.commit()
    finally:
        connection.close()
    clock.value = ELIGIBLE + FRESH_FOR
    before = len(deps.transport.requests)
    result = _run(deps, clock)
    assert result.due_state is FredDueStateV1.UNAVAILABLE_LOCAL_STATE
    assert result.coverage is FredCoverageV1.OMITTED_UNAVAILABLE
    assert result.provider_starts == 0
    assert len(deps.transport.requests) == before
    assert result.revision_identity is None

    # A fresh reopen validates stored bytes and fails closed at construction,
    # so no port is ever built and no provider access can hide the corruption.
    with pytest.raises(FredQueryCacheCorruption):
        _deps(root, clock, _success())


@pytest.mark.parametrize("schedule", [
    None,
    FredReleaseScheduleV1(SERIES_PAYEMS, RELEASE_AT, SLOT,
                          datetime(2026, 8, 8, tzinfo=UTC), "b" * 64),
])
def test_absent_or_expired_schedule_is_unavailable_with_zero_wires(tmp_path, schedule):
    clock = _Clock(datetime(2026, 8, 20, tzinfo=UTC))
    deps = _deps(tmp_path / "fred-query", clock, _success())
    result = _run(deps, clock, schedule=schedule)
    assert result.due_state is FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE
    assert result.coverage is FredCoverageV1.OMITTED_UNAVAILABLE
    assert result.provider_starts == 0 and deps.transport.requests == []


def test_incomplete_and_truncated_pages_omit_only_the_fred_contribution(tmp_path):
    clock = _Clock(ELIGIBLE)
    deps = _deps(tmp_path / "fred-query", clock, _success(count=9))
    result = _run(deps, clock)
    assert result.coverage is FredCoverageV1.OMITTED_INCOMPLETE
    assert result.due_state is FredDueStateV1.DUE_MISSING
    assert result.provider_starts == 1
    assert (result.current_value, result.prior_value, result.direction) == (
        None, None, None)
    assert not any(name in result.__dataclass_fields__
                   for name in ("regime", "pack_digest", "bars"))


@pytest.mark.parametrize("rows,direction,current", [
    ((JUNE, JULY), FredDirectionV1.EXPANDING, Decimal("151000")),
    ((dict(JULY, date="2026-06-01", value="151500"), dict(JULY, value="151000")),
     FredDirectionV1.CONTRACTING, Decimal("151000")),
    ((dict(JULY, date="2026-06-01"), JULY), FredDirectionV1.FLAT, Decimal("151000")),
])
def test_direction_uses_the_two_latest_present_observations(tmp_path, rows,
                                                            direction, current):
    clock = _Clock(ELIGIBLE)
    deps = _deps(tmp_path / "fred-query", clock, _success(rows))
    result = _run(deps, clock)
    assert result.direction is direction
    assert result.current_value == current


def test_a_missing_latest_observation_never_becomes_zero(tmp_path):
    clock = _Clock(ELIGIBLE)
    deps = _deps(tmp_path / "fred-query", clock,
                 _success((JUNE, dict(JULY, value="."))))
    result = _run(deps, clock)
    assert result.coverage is FredCoverageV1.USED
    assert result.current_value == Decimal("150900")
    assert result.current_date == date(2026, 6, 1)
    assert result.prior_value is None and result.direction is None


# --- AR-3/m-2: window helpers tested directly from macro_fred_inputs (T5) ----

from tokmo.programs.macro_fred_inputs import (
    previous_calendar_month, last_day_of_month, derive_query_window_v1,
    WINDOW_MONTHS_BEFORE_TERMINAL,
)


@pytest.mark.parametrize("year,month,expected", [
    (2026, 1, (2025, 12)), (2026, 2, (2026, 1)), (2026, 12, (2026, 11)),
    (2025, 1, (2024, 12)),
])
def test_previous_calendar_month_is_year_aware(year, month, expected):
    assert previous_calendar_month(year, month) == expected


def test_previous_calendar_month_rejects_invalid():
    with pytest.raises(ValueError, match="invalid_calendar_month"):
        previous_calendar_month(2026, 0)


@pytest.mark.parametrize("release_date,end", [
    (date(2026, 3, 6), date(2026, 2, 28)),
    (date(2028, 3, 10), date(2028, 2, 29)),
    (date(2026, 1, 9), date(2025, 12, 31)),
    (date(2026, 5, 8), date(2026, 4, 30)),
])
def test_window_end_is_last_day_of_terminal_month(release_date, end):
    query, terminal = derive_query_window_v1(
        release_date=release_date, as_of=datetime(2026, 8, 12, tzinfo=UTC))
    assert query.observation_end == end
    assert (query.observation_end.year, query.observation_end.month) == terminal


def test_january_release_rolls_over_to_december():
    query, terminal = derive_query_window_v1(
        release_date=date(2026, 1, 9), as_of=datetime(2026, 1, 9, 13, 45, tzinfo=UTC))
    assert terminal == (2025, 12)
    assert query.observation_end == date(2025, 12, 31)
    assert query.observation_start == date(2025, 10, 1)


def test_normal_release_window():
    query, terminal = derive_query_window_v1(
        release_date=date(2026, 8, 7),
        as_of=datetime(2026, 8, 7, 12, 45, tzinfo=UTC))
    assert terminal == (2026, 7)
    assert query.observation_end == date(2026, 7, 31)
    assert query.observation_start == date(2026, 5, 1)


def test_window_derivation_rejects_untyped_input():
    with pytest.raises(ValueError, match="invalid_release_date"):
        derive_query_window_v1(release_date="2026-08-07",
                               as_of=datetime(2026, 8, 7, tzinfo=UTC))
    with pytest.raises(ValueError, match="aware_utc_required"):
        derive_query_window_v1(release_date=date(2026, 8, 7),
                               as_of=datetime(2026, 8, 7))


def test_window_months_before_terminal_is_two():
    assert WINDOW_MONTHS_BEFORE_TERMINAL == 2


# --- Revision-aware reuse (v0.3): M1 guard, M3 unreachability seal ---


def test_reopened_after_cooldown_is_revision_current():
    """A synthetic cooldown omits the old success, then revision-current
    reuse reopens exactly at the cooldown boundary."""
    from tokmo.programs.macro_fred_inputs import _admit, _project
    from tokmo.values.fred_query import FAILURE_COOLDOWN

    class _Attempts:
        highest_attempt_seq = 1
        active_started_at = None
        last_failure_at = ELIGIBLE + FRESH_FOR  # inside cooldown
        last_failure_code = "TRANSIENT_TRANSPORT"

    class _Cache:
        def read_success_for_series(self, series_id, *, now):
            class _Stored:
                query_identity = _query().query_identity
                due_slot = SLOT
                fetched_at = ELIGIBLE  # old: ELIGIBLE + FRESH_FOR > FRESH_FOR
                revision_identity = "d" * 64
            return _Stored()
        def read_attempt_state(self, *a, **kw):
            return _Attempts()

    admission = _admit(query=_query(), schedule=SCHEDULE, cache=_Cache(),
                       now=ELIGIBLE + FRESH_FOR + FAILURE_COOLDOWN - timedelta(seconds=1))
    assert admission.decision.state is FredDueStateV1.NOT_DUE_COOLDOWN
    assert admission.usable is False
    projected = _project(query=_query(), schedule=SCHEDULE,
                         decision=admission.decision, admission=admission,
                         fetched=None, failure=None, starts=0)
    assert projected.coverage is FredCoverageV1.OMITTED_NOT_DUE

    reopened = _admit(query=_query(), schedule=SCHEDULE, cache=_Cache(),
                      now=ELIGIBLE + FRESH_FOR + FAILURE_COOLDOWN)
    assert reopened.decision.state is FredDueStateV1.NOT_DUE_REVISION_CURRENT
    assert reopened.usable is True


def test_due_stale_unreachable_from_admit_for_previous_stale_branch():
    """The previous stale-success branch now returns revision-current."""
    from tokmo.programs.macro_fred_inputs import _admit

    class _EmptyAttempts:
        highest_attempt_seq = 0
        active_started_at = None
        last_failure_at = None
        last_failure_code = None

    class _Cache:
        def __init__(self, success=None):
            self._success = success
        def read_success_for_series(self, series_id, *, now):
            return self._success
        def read_attempt_state(self, *a, **kw):
            return _EmptyAttempts()

    class _Stored:
        query_identity = _query().query_identity
        due_slot = SLOT
        fetched_at = ELIGIBLE  # old success, > FRESH_FOR
        revision_identity = "d" * 64

    # The only input that previously reached DUE_STALE:
    admission = _admit(query=_query(), schedule=SCHEDULE,
                       cache=_Cache(success=_Stored()),
                       now=ELIGIBLE + FRESH_FOR)
    assert admission.decision.state is not FredDueStateV1.DUE_STALE
    assert admission.decision.state is FredDueStateV1.NOT_DUE_REVISION_CURRENT
