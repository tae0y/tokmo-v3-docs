"""Independent FRED series query adapter: typed query in, typed outcome out.

Delta SDS §5: exactly one `SERIES_OBSERVATIONS` request per invocation for
the G1 PAYEMS vertical, retry 0, using the realtime bounds and
``output_type=4`` shape already verified by R4. The adapter reuses the
existing request builder and strict parser, holds no cache, owns no due
policy, and returns no Macro judgment. It imports no Records or Programs
symbol and no live transport module: the transport, API key provider and
clock are injected, so the same adapter type serves the G1 fixture
assembly and any later production assembly.
"""

from datetime import date

from tokmo.facts.fred import parse_fred_response
from tokmo.values.fred_macro import FredOperation, FredRequest, PageCompleteness
from tokmo.values.fred_query import (
    FredCompletenessV1,
    FredObservationV1,
    FredQueryFailureCodeV1,
    FredQueryFailureV1,
    FredRevisionPolicyV1,
    FredSeriesFactV1,
    FredSeriesQueryV1,
    FredValueStateV1,
    build_fred_series_fact_v1,
)

# Broad historical realtime bounds already verified by R4 for output_type=4.
REALTIME_START = date(1776, 7, 4)
REALTIME_END = date(9999, 12, 31)
MAX_OBSERVATION_LIMIT = 1200

# G2 Delta SDS §4.2: the newest already-occurred release date plus one row of
# margin is all this lane ever needs from the release-date endpoint.
RELEASE_DATES_LIMIT = 2
# The narrow completeness judgement that belongs to RELEASE_DATES alone.
DESC_PREFIX_COMPLETE = "DESC_PREFIX_COMPLETE"

# Delta SDS §8: every current live transport failure code, mapped exactly.
TRANSPORT_FAILURE_MAP = {
    "auth_rejection": FredQueryFailureCodeV1.AUTH,
    "api_key_not_registered": FredQueryFailureCodeV1.AUTH,
    "timeout": FredQueryFailureCodeV1.TRANSIENT_TRANSPORT,
    "connection_failure": FredQueryFailureCodeV1.TRANSIENT_TRANSPORT,
    "http_429": FredQueryFailureCodeV1.TRANSIENT_TRANSPORT,
    "http_5xx": FredQueryFailureCodeV1.TRANSIENT_TRANSPORT,
    "redirect": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
    "body_too_large": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
    "unsupported_content_encoding": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
    "protocol_failure": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
    "bad_request": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
    "provider_bad_request_unknown": FredQueryFailureCodeV1.PROVIDER_CONTRACT,
}


def observation_limit(query: FredSeriesQueryV1) -> int:
    """A bounded limit sufficient for the requested closed monthly window."""
    months = (
        (query.observation_end.year - query.observation_start.year) * 12
        + query.observation_end.month
        - query.observation_start.month
        + 1
    )
    return max(1, min(months, MAX_OBSERVATION_LIMIT))


def build_series_observations_request(query: FredSeriesQueryV1) -> FredRequest:
    return FredRequest(
        FredOperation.SERIES_OBSERVATIONS,
        series_id=query.series_id,
        realtime_start=REALTIME_START,
        realtime_end=REALTIME_END,
        observation_start=query.observation_start,
        observation_end=query.observation_end,
        output_type=4,
        limit=observation_limit(query),
        offset=0,
    )


class FredQueryAdapterV1:
    """One bounded provider question per call; never stores what it learns."""

    def __init__(self, *, transport, api_key_provider, clock):
        self._transport = transport
        self._api_key_provider = api_key_provider
        self._clock = clock

    def query_series(self, query) -> FredSeriesFactV1 | FredQueryFailureV1:
        if not isinstance(query, FredSeriesQueryV1):
            raise ValueError("query_required")
        if query.revision_policy is not FredRevisionPolicyV1.INITIAL_RELEASE:
            raise ValueError("unregistered_revision_policy")
        request = build_series_observations_request(query)
        try:
            api_key = self._api_key_provider()
        except ValueError:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.AUTH)
        # One call, retry 0 inside one invocation. The outcome is read
        # structurally so the live and fixture transports stay interchangeable
        # without importing either module here.
        outcome = self._transport(request, api_key)
        code = getattr(outcome, "code", None)
        if code != "success":
            return FredQueryFailureV1.for_code(
                TRANSPORT_FAILURE_MAP.get(
                    code, FredQueryFailureCodeV1.PROVIDER_CONTRACT
                )
            )
        try:
            page = parse_fred_response(request, getattr(outcome, "body", None))
        except ValueError:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)
        if page.completeness is not PageCompleteness.COMPLETE or not page.rows:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.INCOMPLETE)
        try:
            observations = tuple(_observation(row) for row in page.rows)
        except ValueError:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)
        return build_fred_series_fact_v1(
            query_identity=query.query_identity,
            series_id=query.series_id,
            observations=observations,
            fetched_at=self._clock(),
            release_id=None,
            release_date=None,
            vintage_date=None,
            completeness=FredCompletenessV1.COMPLETE,
        )


def build_series_release_request(*, series_id: str) -> FredRequest:
    """The R3/R4 verified shape: realtime keys omitted, one singleton row."""
    return FredRequest(FredOperation.SERIES_RELEASE, series_id=series_id)


def build_release_dates_request(*, release_id: int) -> FredRequest:
    return FredRequest(
        FredOperation.RELEASE_DATES,
        release_id=release_id,
        include_release_dates_with_no_data=False,
        limit=RELEASE_DATES_LIMIT,
        offset=0,
    )


def judge_release_dates_page_v1(page) -> str | None:
    """Judge a RELEASE_DATES page by the narrow DESC-prefix rule (B-1).

    ``sort_order=desc`` is fixed by the request builder and re-checked against
    the provider echo by ``parse_fred_response``, which also rejects unordered
    or duplicate rows; a page that reaches this function has therefore already
    passed those two preconditions. What remains to judge here is that the
    page is the *head* of the descending sequence: offset 0 with at least one
    row. Under those conditions a ``TRUNCATED`` page is still authoritative
    for its newest row, so it is never mapped to ``INCOMPLETE`` — that mapping
    belongs to ``SERIES_OBSERVATIONS``, which keeps its strict ``COMPLETE``
    requirement unchanged.
    """
    if getattr(page, "operation", None) is not FredOperation.RELEASE_DATES:
        return None
    if page.offset != 0 or not page.rows:
        return None
    days = [row["date"] for row in page.rows]
    if days != sorted(days, reverse=True) or len(set(days)) != len(days):
        return None
    return DESC_PREFIX_COMPLETE


class FredScheduleAdapterV1:
    """Bounded release-schedule discovery: one request per call, retry 0.

    It answers only the two questions the schedule join needs — which release
    a series belongs to, and which release date has most recently occurred —
    and stores nothing it learns.
    """

    def __init__(self, *, transport, api_key_provider):
        self._transport = transport
        self._api_key_provider = api_key_provider

    def query_series_release_v1(self, *, series_id: str) -> int | FredQueryFailureV1:
        page = self._page(build_series_release_request(series_id=series_id))
        if isinstance(page, FredQueryFailureV1):
            return page
        release_id = page.rows[0].get("id")
        if type(release_id) is not int or type(release_id) is bool or release_id <= 0:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)
        return release_id

    def query_release_dates_v1(self, *, release_id: int) -> date | FredQueryFailureV1:
        page = self._page(build_release_dates_request(release_id=release_id))
        if isinstance(page, FredQueryFailureV1):
            return page
        if not page.rows:
            # Nothing has been released yet for this release identity; this
            # closes the schedule with no further wire.
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.NOT_YET_RELEASED)
        if judge_release_dates_page_v1(page) != DESC_PREFIX_COMPLETE:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)
        try:
            return date.fromisoformat(page.rows[0]["date"])
        except (KeyError, TypeError, ValueError):
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)

    def _page(self, request: FredRequest):
        try:
            api_key = self._api_key_provider()
        except ValueError:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.AUTH)
        outcome = self._transport(request, api_key)
        code = getattr(outcome, "code", None)
        if code != "success":
            return FredQueryFailureV1.for_code(
                TRANSPORT_FAILURE_MAP.get(
                    code, FredQueryFailureCodeV1.PROVIDER_CONTRACT
                )
            )
        try:
            return parse_fred_response(request, getattr(outcome, "body", None))
        except ValueError:
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)


class TerminalMonthGuardedPortV1:
    """Refuse to settle a success whose newest month is not the expected one.

    G2 Delta SDS §4.7. A released-but-not-yet-ingested provider answer is
    truthful about older months and silent about the new one, so it would
    otherwise settle as a success and cover the new release slot with stale
    material. The expected reference month is derived once by the caller and
    is carried here by ``query.observation_end``, which is by construction the
    last day of that month; comparison is on the ordered ``(year, month)``
    pair, never the month number alone.

    This wrapper judges an already-parsed fact. It is therefore a distinct
    path from the observation adapter's completeness mapping: a page can be
    perfectly ``COMPLETE`` for the requested window and still be rejected
    here, and the codes differ — ``NOT_YET_RELEASED`` when the newest month is
    earlier, ``RESPONSE_CONTRACT`` when it is later than the window the
    request itself bounded.
    """

    def __init__(self, inner):
        self._inner = inner

    def query_series(self, query) -> FredSeriesFactV1 | FredQueryFailureV1:
        outcome = self._inner.query_series(query)
        if not isinstance(outcome, FredSeriesFactV1):
            return outcome
        newest = outcome.observations[-1].observation_date
        if (newest.year, newest.month) < (
            query.observation_end.year,
            query.observation_end.month,
        ):
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.NOT_YET_RELEASED)
        if (newest.year, newest.month) > (
            query.observation_end.year,
            query.observation_end.month,
        ):
            return FredQueryFailureV1.for_code(FredQueryFailureCodeV1.RESPONSE_CONTRACT)
        return outcome


def _observation(row: dict) -> FredObservationV1:
    return FredObservationV1(
        date.fromisoformat(row["date"]),
        row["value"],
        FredValueStateV1(row["value_state"]),
        date.fromisoformat(row["realtime_start"]),
        date.fromisoformat(row["realtime_end"]),
    )
