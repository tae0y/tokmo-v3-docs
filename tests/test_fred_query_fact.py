"""Delta SDS §5/§8 acceptance 2, 3, 12: the independent Facts adapter.

One `SERIES_OBSERVATIONS` request per invocation, retry 0, verified
`output_type=4` rows mapped to typed observations, and every transport /
parser / truncation outcome closed onto the typed failure vocabulary. The
adapter stores nothing and never constructs a socket.
"""

import json
import socket
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest

from tokmo.facts.fred import build_query_pairs
from tokmo.facts.fred_live import FAILURE_CODES
from tokmo.facts.fred_query import FredQueryAdapterV1
from tokmo.fred_query_composition import (
    FredFixtureTransportOutcomeV1,
    FredFixtureTransportV1,
    build_fixture_api_key_provider_v1,
    build_fixture_fred_query_port_v1,
)
from tokmo.values.fred_macro import FredOperation
from tokmo.values.fred_query import (
    SERIES_PAYEMS,
    FredCompletenessV1,
    FredQueryFailureCodeV1,
    FredQueryFailureV1,
    FredRevisionPolicyV1,
    FredSeriesFactV1,
    FredSeriesQueryV1,
    FredValueStateV1,
)

UTC = UTC
AS_OF = datetime(2026, 8, 12, 13, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 12, 13, 0, 1, tzinfo=UTC)

JUNE = {
    "realtime_start": "2026-07-02",
    "realtime_end": "9999-12-31",
    "date": "2026-06-01",
    "value": "150900",
}
JULY = {
    "realtime_start": "2026-08-07",
    "realtime_end": "9999-12-31",
    "date": "2026-07-01",
    "value": "151000",
}
JULY_MISSING = dict(JULY, value=".")


def _query(**overrides):
    fields = dict(
        series_id=SERIES_PAYEMS,
        observation_start=date(2026, 6, 1),
        observation_end=date(2026, 7, 31),
        as_of=AS_OF,
    )
    fields.update(overrides)
    return FredSeriesQueryV1(**fields)


def _body(rows, *, count=None, limit=2, offset=0, **overrides):
    payload = {
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "observation_start": "2026-06-01",
        "observation_end": "2026-07-31",
        "units": "lin",
        "output_type": 4,
        "file_type": "json",
        "order_by": "observation_date",
        "sort_order": "asc",
        "count": len(rows) if count is None else count,
        "offset": offset,
        "limit": limit,
        "observations": list(rows),
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _port(*outcomes, clock=None):
    transport = FredFixtureTransportV1(outcomes=tuple(outcomes))
    port = build_fixture_fred_query_port_v1(
        transport=transport,
        api_key_provider=build_fixture_api_key_provider_v1(),
        clock=clock or (lambda: FETCHED_AT),
    )
    return port, transport


def _success(rows, **kwargs):
    return FredFixtureTransportOutcomeV1("success", _body(rows, **kwargs))


def test_verified_output4_rows_become_a_typed_fact_and_missing_stays_missing():
    port, transport = _port(_success([JUNE, JULY_MISSING]))
    fact = port.query_series(_query())
    assert isinstance(fact, FredSeriesFactV1)
    assert len(transport.requests) == 1
    assert fact.series_id == SERIES_PAYEMS
    assert fact.completeness is FredCompletenessV1.COMPLETE
    assert fact.query_identity == _query().query_identity
    assert fact.fetched_at == FETCHED_AT
    assert (fact.release_id, fact.release_date, fact.vintage_date) == (None, None, None)
    first, second = fact.observations
    assert (first.observation_date, first.value, first.value_state) == (
        date(2026, 6, 1),
        Decimal(150900),
        FredValueStateV1.PRESENT,
    )
    assert (first.realtime_start, first.realtime_end) == (
        date(2026, 7, 2),
        date(9999, 12, 31),
    )
    assert (second.observation_date, second.value, second.value_state) == (
        date(2026, 7, 1),
        None,
        FredValueStateV1.MISSING,
    )
    assert second.value is not Decimal(0) and second.value is None


def test_revision_identity_ignores_fetched_at_across_two_identical_fetches():
    first, _ = _port(_success([JUNE, JULY]), clock=lambda: FETCHED_AT)
    later, _ = _port(
        _success([JUNE, JULY]), clock=lambda: FETCHED_AT + timedelta(days=2)
    )
    one = first.query_series(_query())
    two = later.query_series(_query())
    assert one.revision_identity == two.revision_identity
    assert one.fetched_at != two.fetched_at


def test_exactly_one_bounded_output4_request_with_broad_historical_realtime():
    port, transport = _port(_success([JUNE, JULY]))
    port.query_series(_query())
    assert len(transport.requests) == 1
    request, api_key = transport.requests[0]
    assert request.operation is FredOperation.SERIES_OBSERVATIONS
    assert request.series_id == SERIES_PAYEMS
    assert request.output_type == 4
    assert (request.realtime_start, request.realtime_end) == (
        date(1776, 7, 4),
        date(9999, 12, 31),
    )
    assert (request.observation_start, request.observation_end) == (
        date(2026, 6, 1),
        date(2026, 7, 31),
    )
    assert (request.limit, request.offset) == (2, 0)
    assert request.release_id is None and request.vintage_dates == ()
    assert dict(build_query_pairs(request))["sort_order"] == "asc"
    assert api_key == build_fixture_api_key_provider_v1()()


def test_requested_window_sets_the_bounded_limit():
    port, transport = _port(
        _success(
            [JUNE, JULY],
            limit=14,
            observation_start="2025-06-01",
            observation_end="2026-07-31",
        )
    )
    port.query_series(_query(observation_start=date(2025, 6, 1)))
    assert transport.requests[0][0].limit == 14


@pytest.mark.parametrize(
    "code,expected",
    [
        ("auth_rejection", FredQueryFailureCodeV1.AUTH),
        ("api_key_not_registered", FredQueryFailureCodeV1.AUTH),
        ("timeout", FredQueryFailureCodeV1.TRANSIENT_TRANSPORT),
        ("connection_failure", FredQueryFailureCodeV1.TRANSIENT_TRANSPORT),
        ("http_429", FredQueryFailureCodeV1.TRANSIENT_TRANSPORT),
        ("http_5xx", FredQueryFailureCodeV1.TRANSIENT_TRANSPORT),
        ("redirect", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
        ("body_too_large", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
        ("unsupported_content_encoding", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
        ("protocol_failure", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
        ("bad_request", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
        ("provider_bad_request_unknown", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
    ],
)
def test_every_live_transport_failure_code_maps_exactly_once(code, expected):
    port, transport = _port(FredFixtureTransportOutcomeV1(code))
    failure = port.query_series(_query())
    assert isinstance(failure, FredQueryFailureV1)
    assert failure.code is expected
    assert failure.retryable is (expected is FredQueryFailureCodeV1.TRANSIENT_TRANSPORT)
    assert len(transport.requests) == 1
    assert not hasattr(failure, "body") and not hasattr(failure, "url")


def test_the_mapping_covers_every_declared_live_transport_failure_code():
    from tokmo.facts.fred_query import TRANSPORT_FAILURE_MAP

    assert set(TRANSPORT_FAILURE_MAP) == set(FAILURE_CODES)


@pytest.mark.parametrize(
    "body",
    [
        b"{",
        json.dumps({"count": 1}).encode(),
        _body([JUNE, JULY], realtime_start="2026-08-12"),
        _body([JUNE, JULY], output_type=1),
        _body([dict(JULY, extra="x")]),
        _body([JULY, JUNE]),
    ],
)
def test_parser_shape_and_echo_drift_map_to_response_contract(body):
    port, transport = _port(FredFixtureTransportOutcomeV1("success", body))
    failure = port.query_series(_query())
    assert isinstance(failure, FredQueryFailureV1)
    assert failure.code is FredQueryFailureCodeV1.RESPONSE_CONTRACT
    assert failure.retryable is False
    assert len(transport.requests) == 1


def test_truncated_and_empty_pages_map_to_incomplete():
    port, _ = _port(_success([JUNE, JULY], count=9))
    truncated = port.query_series(_query())
    assert truncated.code is FredQueryFailureCodeV1.INCOMPLETE
    port, _ = _port(_success([], count=0))
    empty = port.query_series(_query())
    assert empty.code is FredQueryFailureCodeV1.INCOMPLETE


def test_credential_failure_is_auth_and_starts_no_transport_call():
    def broken_provider():
        raise ValueError("credential_unsafe")

    transport = FredFixtureTransportV1(outcomes=(_success([JUNE, JULY]),))
    port = build_fixture_fred_query_port_v1(
        transport=transport, api_key_provider=broken_provider, clock=lambda: FETCHED_AT
    )
    failure = port.query_series(_query())
    assert failure.code is FredQueryFailureCodeV1.AUTH
    assert failure.retryable is False
    assert transport.requests == []


def test_adapter_refuses_a_policy_it_cannot_serve():
    port, transport = _port(_success([JUNE, JULY]))
    with pytest.raises(ValueError, match="query_required"):
        port.query_series("PAYEMS")
    with pytest.raises(ValueError, match="unregistered_revision_policy"):
        FredSeriesQueryV1(
            SERIES_PAYEMS,
            date(2026, 6, 1),
            date(2026, 7, 31),
            AS_OF,
            FredRevisionPolicyV1.LATEST_AVAILABLE,
        )
    assert transport.requests == []


def test_fixture_query_path_creates_no_socket(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("network_forbidden")

    monkeypatch.setattr(socket, "socket", forbidden)
    monkeypatch.setattr(socket, "create_connection", forbidden)
    port, transport = _port(_success([JUNE, JULY]))
    assert isinstance(port.query_series(_query()), FredSeriesFactV1)
    assert len(transport.requests) == 1


def test_adapter_is_the_only_caller_and_never_retries_inside_one_invocation():
    port, transport = _port(FredFixtureTransportOutcomeV1("timeout"))
    assert (
        port.query_series(_query()).code is FredQueryFailureCodeV1.TRANSIENT_TRANSPORT
    )
    assert len(transport.requests) == 1
    assert isinstance(port, FredQueryAdapterV1)


# ---------------------------------------------------------------------------
# G2 Delta SDS §4.2 / §8.1 acceptance A16-A18: the schedule adapters.
# ---------------------------------------------------------------------------

RELEASE_ID = 50


def _release_body(
    rows,
    *,
    count=None,
    limit=2,
    offset=0,
    sort_order="desc",
    order_by="release_date",
    **overrides,
):
    payload = {
        "realtime_start": "1776-07-04",
        "realtime_end": "9999-12-31",
        "order_by": order_by,
        "sort_order": sort_order,
        "count": len(rows) if count is None else count,
        "offset": offset,
        "limit": limit,
        "release_dates": [{"release_id": RELEASE_ID, "date": row} for row in rows],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _series_release_body(release_id=RELEASE_ID, **overrides):
    payload = {
        "realtime_start": "2026-08-12",
        "realtime_end": "2026-08-12",
        "releases": [
            {
                "id": release_id,
                "realtime_start": "2026-08-12",
                "realtime_end": "2026-08-12",
                "name": "Employment Situation",
                "press_release": True,
            }
        ],
    }
    payload.update(overrides)
    return json.dumps(payload).encode()


def _schedule_adapter(*outcomes):
    from tokmo.facts.fred_query import FredScheduleAdapterV1

    transport = FredFixtureTransportV1(outcomes=tuple(outcomes))
    adapter = FredScheduleAdapterV1(
        transport=transport, api_key_provider=build_fixture_api_key_provider_v1()
    )
    return adapter, transport


def test_a16_a_truncated_desc_prefix_page_is_admitted_and_is_not_incomplete():
    from tokmo.facts.fred_query import RELEASE_DATES_LIMIT

    assert RELEASE_DATES_LIMIT == 2
    adapter, transport = _schedule_adapter(
        FredFixtureTransportOutcomeV1(
            "success", _release_body(["2026-08-07", "2026-07-02"], count=311)
        )
    )
    release_date = adapter.query_release_dates_v1(release_id=RELEASE_ID)
    assert release_date == date(2026, 8, 7)
    assert not isinstance(release_date, FredQueryFailureV1)
    assert len(transport.requests) == 1
    request = transport.requests[0][0]
    assert request.operation is FredOperation.RELEASE_DATES
    assert (request.release_id, request.limit, request.offset) == (
        RELEASE_ID,
        RELEASE_DATES_LIMIT,
        0,
    )
    assert request.include_release_dates_with_no_data is False
    assert dict(build_query_pairs(request))["sort_order"] == "desc"


def test_a16_the_same_truncated_page_is_judged_desc_prefix_complete():
    from tokmo.facts.fred import parse_fred_response
    from tokmo.facts.fred_query import (
        DESC_PREFIX_COMPLETE,
        build_release_dates_request,
        judge_release_dates_page_v1,
    )
    from tokmo.values.fred_macro import PageCompleteness

    request = build_release_dates_request(release_id=RELEASE_ID)
    page = parse_fred_response(
        request, _release_body(["2026-08-07", "2026-07-02"], count=311)
    )
    assert page.completeness is PageCompleteness.TRUNCATED
    assert judge_release_dates_page_v1(page) == DESC_PREFIX_COMPLETE
    assert judge_release_dates_page_v1(page) != FredQueryFailureCodeV1.INCOMPLETE


def test_a16_the_observation_adapter_keeps_its_strict_complete_requirement():
    # The narrow prefix rule belongs to RELEASE_DATES alone: an observation
    # page with the same truncation shape still closes INCOMPLETE.
    port, _ = _port(_success([JUNE, JULY], count=9))
    assert port.query_series(_query()).code is FredQueryFailureCodeV1.INCOMPLETE


def test_a17_empty_release_rows_close_as_not_yet_released_with_no_further_wire():
    adapter, transport = _schedule_adapter(
        FredFixtureTransportOutcomeV1("success", _release_body([], count=0))
    )
    outcome = adapter.query_release_dates_v1(release_id=RELEASE_ID)
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is FredQueryFailureCodeV1.NOT_YET_RELEASED
    assert outcome.retryable is False
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "body",
    [
        _release_body(["2026-08-07"], sort_order="asc"),
        _release_body(["2026-08-07"], order_by="release_id"),
        _release_body(["2026-08-07"], offset=3, count=9),
        _release_body(["2026-07-02", "2026-08-07"]),
        _release_body(["2026-08-07", "2026-08-07"]),
        b"{",
    ],
)
def test_a18_a_page_that_breaks_the_prefix_precondition_is_response_contract(body):
    adapter, transport = _schedule_adapter(
        FredFixtureTransportOutcomeV1("success", body)
    )
    outcome = adapter.query_release_dates_v1(release_id=RELEASE_ID)
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is FredQueryFailureCodeV1.RESPONSE_CONTRACT
    assert len(transport.requests) == 1


def test_the_series_release_adapter_returns_one_positive_release_identity():
    adapter, transport = _schedule_adapter(
        FredFixtureTransportOutcomeV1("success", _series_release_body())
    )
    assert adapter.query_series_release_v1(series_id=SERIES_PAYEMS) == RELEASE_ID
    assert len(transport.requests) == 1
    request = transport.requests[0][0]
    assert request.operation is FredOperation.SERIES_RELEASE
    assert request.series_id == SERIES_PAYEMS
    assert (request.realtime_start, request.realtime_end) == (None, None)


@pytest.mark.parametrize(
    "body,expected",
    [
        (_series_release_body(release_id=0), FredQueryFailureCodeV1.RESPONSE_CONTRACT),
        (
            json.dumps(
                {
                    "realtime_start": "2026-08-12",
                    "realtime_end": "2026-08-12",
                    "releases": [],
                }
            ).encode(),
            FredQueryFailureCodeV1.RESPONSE_CONTRACT,
        ),
        (b"not-json", FredQueryFailureCodeV1.RESPONSE_CONTRACT),
    ],
)
def test_the_series_release_adapter_rejects_a_non_singleton_or_invalid_release(
    body, expected
):
    adapter, _ = _schedule_adapter(FredFixtureTransportOutcomeV1("success", body))
    outcome = adapter.query_series_release_v1(series_id=SERIES_PAYEMS)
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is expected


@pytest.mark.parametrize(
    "code,expected",
    [
        ("timeout", FredQueryFailureCodeV1.TRANSIENT_TRANSPORT),
        ("auth_rejection", FredQueryFailureCodeV1.AUTH),
        ("redirect", FredQueryFailureCodeV1.PROVIDER_CONTRACT),
    ],
)
def test_the_schedule_adapters_reuse_the_closed_transport_failure_map(code, expected):
    adapter, transport = _schedule_adapter(
        FredFixtureTransportOutcomeV1(code), FredFixtureTransportOutcomeV1(code)
    )
    assert adapter.query_series_release_v1(series_id=SERIES_PAYEMS).code is expected
    assert adapter.query_release_dates_v1(release_id=RELEASE_ID).code is expected
    assert len(transport.requests) == 2


def test_the_schedule_adapters_never_retry_and_store_nothing():
    from tokmo.facts.fred_query import FredScheduleAdapterV1

    adapter, transport = _schedule_adapter(FredFixtureTransportOutcomeV1("timeout"))
    adapter.query_series_release_v1(series_id=SERIES_PAYEMS)
    assert len(transport.requests) == 1
    assert not any(name for name in vars(adapter) if "cache" in name or "store" in name)
    assert not hasattr(FredScheduleAdapterV1, "settle_success")


def test_a_credential_failure_starts_no_schedule_request():
    from tokmo.facts.fred_query import FredScheduleAdapterV1

    def broken_provider():
        raise ValueError("credential_unsafe")

    transport = FredFixtureTransportV1(outcomes=())
    adapter = FredScheduleAdapterV1(
        transport=transport, api_key_provider=broken_provider
    )
    assert (
        adapter.query_series_release_v1(series_id=SERIES_PAYEMS).code
        is FredQueryFailureCodeV1.AUTH
    )
    assert (
        adapter.query_release_dates_v1(release_id=RELEASE_ID).code
        is FredQueryFailureCodeV1.AUTH
    )
    assert transport.requests == []


# ---------------------------------------------------------------------------
# G2 Delta SDS §4.7 / §8.1 acceptance A22-A25 and the GLM m1 distinctness
# note: the terminal-month availability guard.
# ---------------------------------------------------------------------------


def _guarded(*outcomes, clock=None):
    from tokmo.facts.fred_query import TerminalMonthGuardedPortV1

    port, transport = _port(*outcomes, clock=clock)
    return TerminalMonthGuardedPortV1(port), transport


def _window_body(rows, *, start, end, limit):
    return _body(rows, limit=limit, observation_start=start, observation_end=end)


MAY = {
    "realtime_start": "2026-06-05",
    "realtime_end": "9999-12-31",
    "date": "2026-05-01",
    "value": "150800",
}


def test_a22_a_newest_month_earlier_than_expected_becomes_not_yet_released():
    guarded, transport = _guarded(_success([JUNE], count=1))
    outcome = guarded.query_series(_query())
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is FredQueryFailureCodeV1.NOT_YET_RELEASED
    assert outcome.retryable is False
    assert len(transport.requests) == 1


def test_a22_the_downgrade_settles_no_success_and_preserves_the_prior_one(tmp_path):
    from datetime import timedelta

    from tokmo.programs.macro_fred_inputs import run_macro_fred_enrichment_v1
    from tokmo.records.fred_query_cache import FredQueryCacheV1
    from tokmo.values.fred_query import (
        FAILURE_COOLDOWN,
        FredCoverageV1,
        FredDueStateV1,
        FredReleaseScheduleV1,
    )

    now = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
    cache = FredQueryCacheV1(tmp_path / "cache", opened_at=now)
    prior_query = _query(
        observation_start=date(2026, 4, 1), observation_end=date(2026, 6, 30)
    )
    prior_port, _ = _port(
        _success(
            [JUNE],
            count=1,
            limit=3,
            observation_start="2026-04-01",
            observation_end="2026-06-30",
        ),
        clock=lambda: now - timedelta(days=30),
    )
    prior_schedule = FredReleaseScheduleV1(
        SERIES_PAYEMS,
        now - timedelta(days=31),
        "PAYEMS:2026-07-02",
        now + timedelta(days=1),
        "a" * 64,
    )
    prior = run_macro_fred_enrichment_v1(
        query=prior_query,
        schedule=prior_schedule,
        cache=cache,
        query_port=prior_port,
        clock=lambda: now - timedelta(days=30),
    )
    assert prior.coverage is FredCoverageV1.USED
    stored = cache.read_success_for_series(SERIES_PAYEMS, now=now)
    before = (
        stored.query_identity,
        stored.revision_identity,
        stored.due_slot,
        stored.fetched_at,
    )

    # The new slot's release fired, but FRED has not ingested July yet.
    query = _query(
        observation_start=date(2026, 5, 1), observation_end=date(2026, 7, 31)
    )
    guarded, transport = _guarded(
        _success(
            [MAY, JUNE],
            count=2,
            limit=3,
            observation_start="2026-05-01",
            observation_end="2026-07-31",
        ),
        clock=lambda: now,
    )
    schedule = FredReleaseScheduleV1(
        SERIES_PAYEMS,
        now - timedelta(minutes=15),
        "PAYEMS:2026-08-07",
        now + timedelta(days=21),
        "b" * 64,
    )
    result = run_macro_fred_enrichment_v1(
        query=query,
        schedule=schedule,
        cache=cache,
        query_port=guarded,
        clock=lambda: now,
    )

    from tokmo.values.fred_query import DUE_STATES

    # The new release moved the window, so the stored success no longer
    # carries this query identity; either way the decision is a DUE state and
    # the wire is spent exactly once.
    assert result.due_state is FredDueStateV1.DUE_QUERY_CHANGED
    assert result.due_state in DUE_STATES
    assert result.coverage is FredCoverageV1.OMITTED_UNAVAILABLE
    assert result.provider_starts == 1 and len(transport.requests) == 1
    assert result.revision_identity is None and result.direction is None
    # The prior success row is byte-preserved: no success covers the new slot.
    after = cache.read_success_for_series(SERIES_PAYEMS, now=now)
    assert (
        after.query_identity,
        after.revision_identity,
        after.due_slot,
        after.fetched_at,
    ) == before
    rows = cache.attempt_rows(query.query_identity, "PAYEMS:2026-08-07")
    assert [(row[1], row[4]) for row in rows] == [
        ("FAILED", FredQueryFailureCodeV1.NOT_YET_RELEASED.value)
    ]
    # The 6h cooldown is in force for that slot, so no retry can follow.
    state = cache.read_attempt_state(query.query_identity, "PAYEMS:2026-08-07", now=now)
    assert state.last_failure_at is not None
    assert now - state.last_failure_at < FAILURE_COOLDOWN
    later = run_macro_fred_enrichment_v1(
        query=query,
        schedule=schedule,
        cache=cache,
        query_port=guarded,
        clock=lambda: now + timedelta(hours=1),
    )
    assert later.due_state is FredDueStateV1.NOT_DUE_COOLDOWN
    assert later.provider_starts == 0 and len(transport.requests) == 1


AUGUST = {
    "realtime_start": "2026-09-04",
    "realtime_end": "9999-12-31",
    "date": "2026-08-01",
    "value": "151200",
}


def _late_page():
    """A page that is COMPLETE for the requested window and still too new.

    The window is the real three-month one, so the request limit, the page
    echo and the row count all agree: the only thing wrong with this page is
    that its newest month is later than the window's terminal month. Nothing
    but the guard can reject it.
    """
    return _success(
        [JUNE, JULY, AUGUST],
        count=3,
        limit=3,
        observation_start="2026-05-01",
        observation_end="2026-07-31",
    )


def _late_query():
    return _query(observation_start=date(2026, 5, 1), observation_end=date(2026, 7, 31))


def test_a23_a_newest_month_later_than_expected_is_rejected_as_response_contract():
    plain, _ = _port(_late_page())
    assert isinstance(plain.query_series(_late_query()), FredSeriesFactV1), (
        "the page must be accepted by the adapter, so only the guard can reject"
    )
    guarded, transport = _guarded(_late_page())
    outcome = guarded.query_series(_late_query())
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is FredQueryFailureCodeV1.RESPONSE_CONTRACT
    assert outcome.retryable is False
    assert len(transport.requests) == 1


def test_a23_the_later_material_never_settles_a_success(tmp_path):
    from datetime import timedelta

    from tokmo.programs.macro_fred_inputs import run_macro_fred_enrichment_v1
    from tokmo.records.fred_query_cache import FredQueryCacheV1
    from tokmo.values.fred_query import (
        FredCoverageV1,
        FredDueStateV1,
        FredReleaseScheduleV1,
    )

    now = datetime(2026, 8, 7, 12, 45, tzinfo=UTC)
    cache = FredQueryCacheV1(tmp_path / "cache", opened_at=now)
    query = _late_query()
    guarded, _ = _guarded(_late_page(), clock=lambda: now)
    result = run_macro_fred_enrichment_v1(
        query=query,
        schedule=FredReleaseScheduleV1(
            SERIES_PAYEMS,
            now - timedelta(minutes=15),
            "PAYEMS:2026-08-07",
            now + timedelta(days=21),
            "b" * 64,
        ),
        cache=cache,
        query_port=guarded,
        clock=lambda: now,
    )
    assert result.due_state is FredDueStateV1.DUE_MISSING
    assert result.coverage is FredCoverageV1.OMITTED_UNAVAILABLE
    assert cache.read_success_for_series(SERIES_PAYEMS, now=now) is None
    rows = cache.attempt_rows(query.query_identity, "PAYEMS:2026-08-07")
    assert [(row[1], row[4]) for row in rows] == [
        ("FAILED", FredQueryFailureCodeV1.RESPONSE_CONTRACT.value)
    ]


def test_a24_a_matching_month_passes_the_guard_unchanged():
    guarded, transport = _guarded(_success([JUNE, JULY]))
    plain, _ = _port(_success([JUNE, JULY]))
    guarded_fact = guarded.query_series(_query())
    assert isinstance(guarded_fact, FredSeriesFactV1)
    assert guarded_fact == plain.query_series(_query())
    assert len(transport.requests) == 1


@pytest.mark.parametrize(
    "newest,end,expected",
    [
        ("2025-12-01", date(2026, 1, 31), FredQueryFailureCodeV1.NOT_YET_RELEASED),
        ("2026-01-01", date(2025, 12, 31), FredQueryFailureCodeV1.RESPONSE_CONTRACT),
    ],
)
def test_a25_the_comparison_orders_the_year_month_pair_not_the_month_number(
    newest, end, expected
):
    start = (
        date(end.year - 1, end.month, 1) if end.month != 1 else date(end.year - 1, 1, 1)
    )
    row = {
        "realtime_start": "2026-02-06",
        "realtime_end": "9999-12-31",
        "date": newest,
        "value": "151000",
    }
    guarded, _ = _guarded(
        _success(
            [row],
            count=1,
            limit=13,
            observation_start=start.isoformat(),
            observation_end=end.isoformat(),
        )
    )
    outcome = guarded.query_series(_query(observation_start=start, observation_end=end))
    assert isinstance(outcome, FredQueryFailureV1)
    assert outcome.code is expected


def test_a25_a_december_page_against_a_december_expectation_is_accepted():
    row = {
        "realtime_start": "2026-01-09",
        "realtime_end": "9999-12-31",
        "date": "2025-12-01",
        "value": "151000",
    }
    guarded, _ = _guarded(
        _success(
            [row],
            count=1,
            limit=3,
            observation_start="2025-10-01",
            observation_end="2025-12-31",
        )
    )
    outcome = guarded.query_series(
        _query(observation_start=date(2025, 10, 1), observation_end=date(2025, 12, 31))
    )
    assert isinstance(outcome, FredSeriesFactV1)


def test_glm_m1_the_guard_path_is_distinct_from_the_adapter_incomplete_path():
    """The guard's RESPONSE_CONTRACT is not the adapter's INCOMPLETE.

    A page that is COMPLETE for the requested window still fails the guard,
    and a truncated page still fails the adapter — the two rejections have
    different causes, different codes and different code paths.
    """
    unguarded, _ = _port(_late_page())
    assert isinstance(unguarded.query_series(_late_query()), FredSeriesFactV1)
    guarded, _ = _guarded(_late_page())
    guard_failure = guarded.query_series(_late_query())
    assert guard_failure.code is FredQueryFailureCodeV1.RESPONSE_CONTRACT

    truncated_port, _ = _port(_success([JUNE, JULY], count=9))
    adapter_failure = truncated_port.query_series(_query())
    assert adapter_failure.code is FredQueryFailureCodeV1.INCOMPLETE
    assert guard_failure.code is not adapter_failure.code

    # The guard never emits INCOMPLETE and the adapter never emits the
    # guard's month verdicts from a complete page.
    guarded_complete, _ = _guarded(_success([JUNE, JULY]))
    assert isinstance(guarded_complete.query_series(_query()), FredSeriesFactV1)
    guarded_truncated, _ = _guarded(_success([JUNE, JULY], count=9))
    assert (
        guarded_truncated.query_series(_query()).code
        is FredQueryFailureCodeV1.INCOMPLETE
    )


def test_the_guard_passes_every_non_fact_outcome_through_untouched():
    from tokmo.facts.fred_query import TerminalMonthGuardedPortV1

    failure = FredQueryFailureV1.for_code(FredQueryFailureCodeV1.AUTH)

    class _Fixed:
        def __init__(self, outcome):
            self.outcome = outcome
            self.calls = 0

        def query_series(self, _query):
            self.calls += 1
            return self.outcome

    inner = _Fixed(failure)
    assert TerminalMonthGuardedPortV1(inner).query_series(_query()) is failure
    assert inner.calls == 1
