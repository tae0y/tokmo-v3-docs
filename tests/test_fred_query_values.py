"""Delta SDS §3/§4 acceptance 1: the standalone routine-query vocabulary.

The value graph is domain-separated from the R1-R4 campaign vocabulary: it
registers exactly one series/policy pair, refuses naive decision times and
inverted ranges, and refuses a fact whose declared identities do not match
its own material.
"""

from datetime import UTC, date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from tokmo.values.fred_query import (
    FAILURE_COOLDOWN,
    FRESH_FOR,
    LOCAL_STATE_UNKNOWN_START,
    MAX_TRANSPORT_CALLS,
    RELEASE_GRACE,
    SERIES_PAYEMS,
    SERIES_REGISTRY,
    STARTED_LEASE,
    FredCompletenessV1,
    FredCoverageV1,
    FredDirectionV1,
    FredDueDecisionV1,
    FredDueStateV1,
    FredObservationV1,
    FredQueryFailureCodeV1,
    FredQueryFailureV1,
    FredReleaseScheduleV1,
    FredRevisionPolicyV1,
    FredSeriesFactV1,
    FredSeriesQueryV1,
    FredValueStateV1,
    build_fred_series_fact_v1,
    compute_revision_identity,
)

UTC = UTC
AS_OF = datetime(2026, 8, 12, 13, 0, tzinfo=UTC)
FETCHED_AT = datetime(2026, 8, 12, 13, 0, 1, tzinfo=UTC)


def _query(**overrides):
    fields = dict(
        series_id=SERIES_PAYEMS,
        observation_start=date(2026, 6, 1),
        observation_end=date(2026, 7, 31),
        as_of=AS_OF,
    )
    fields.update(overrides)
    return FredSeriesQueryV1(**fields)


def _observations():
    return (
        FredObservationV1(
            date(2026, 6, 1),
            Decimal(150900),
            FredValueStateV1.PRESENT,
            date(2026, 7, 2),
            date(9999, 12, 31),
        ),
        FredObservationV1(
            date(2026, 7, 1),
            Decimal(151000),
            FredValueStateV1.PRESENT,
            date(2026, 8, 7),
            date(9999, 12, 31),
        ),
    )


def _fact(**overrides):
    fields = dict(
        query_identity=_query().query_identity,
        series_id=SERIES_PAYEMS,
        observations=_observations(),
        fetched_at=FETCHED_AT,
        release_id=None,
        release_date=None,
        vintage_date=None,
        completeness=FredCompletenessV1.COMPLETE,
    )
    fields.update(overrides)
    return build_fred_series_fact_v1(**fields)


def test_registry_admits_exactly_payems_initial_release():
    assert set(SERIES_REGISTRY) == {"PAYEMS"}
    assert SERIES_REGISTRY["PAYEMS"] == frozenset(
        {FredRevisionPolicyV1.INITIAL_RELEASE}
    )
    assert _query().revision_policy is FredRevisionPolicyV1.INITIAL_RELEASE
    with pytest.raises(ValueError, match="unregistered_series"):
        _query(series_id="UNRATE")
    with pytest.raises(ValueError, match="unregistered_revision_policy"):
        _query(revision_policy=FredRevisionPolicyV1.LATEST_AVAILABLE)


def test_query_rejects_naive_as_of_inverted_range_and_wrong_types():
    with pytest.raises(ValueError, match="aware_utc_required"):
        _query(as_of=datetime(2026, 8, 12, 13, 0))
    with pytest.raises(ValueError, match="aware_utc_required"):
        _query(as_of=datetime(2026, 8, 12, 13, 0, tzinfo=timezone(timedelta(hours=9))))
    with pytest.raises(ValueError, match="ordered_observation_range"):
        _query(observation_start=date(2026, 8, 1), observation_end=date(2026, 7, 31))
    with pytest.raises(ValueError, match="invalid_observation_start"):
        _query(observation_start=datetime(2026, 6, 1, tzinfo=UTC))


def test_query_identity_binds_range_and_policy_but_excludes_as_of():
    identity = _query().query_identity
    assert len(identity) == 64 and int(identity, 16) >= 0
    later = _query(as_of=AS_OF + timedelta(days=9))
    assert later.query_identity == identity
    assert _query(observation_end=date(2026, 8, 31)).query_identity != identity
    assert _query(observation_start=date(2026, 5, 1)).query_identity != identity


def test_observation_value_state_and_missing_never_becomes_zero():
    with pytest.raises(ValueError, match="value_state_contract"):
        FredObservationV1(
            date(2026, 7, 1),
            None,
            FredValueStateV1.PRESENT,
            date(2026, 8, 7),
            date(9999, 12, 31),
        )
    with pytest.raises(ValueError, match="value_state_contract"):
        FredObservationV1(
            date(2026, 7, 1),
            Decimal(1),
            FredValueStateV1.MISSING,
            date(2026, 8, 7),
            date(9999, 12, 31),
        )
    missing = FredObservationV1(
        date(2026, 7, 1),
        None,
        FredValueStateV1.MISSING,
        date(2026, 8, 7),
        date(9999, 12, 31),
    )
    assert missing.value is None
    with pytest.raises(ValueError, match="ordered_realtime_range"):
        FredObservationV1(
            date(2026, 7, 1),
            None,
            FredValueStateV1.MISSING,
            date(9999, 12, 31),
            date(2026, 8, 7),
        )


def test_fact_rejects_empty_unordered_and_mismatched_identity():
    with pytest.raises(ValueError, match="empty_observations"):
        _fact(observations=())
    with pytest.raises(ValueError, match="unordered_or_duplicate_observations"):
        _fact(observations=tuple(reversed(_observations())))
    with pytest.raises(ValueError, match="aware_utc_required"):
        _fact(fetched_at=datetime(2026, 8, 12, 13, 0, 1))
    with pytest.raises(ValueError, match="unregistered_series"):
        _fact(series_id="UNRATE")
    good = _fact()
    with pytest.raises(ValueError, match="revision_identity_mismatch"):
        FredSeriesFactV1(
            good.query_identity,
            good.series_id,
            good.observations,
            good.fetched_at,
            None,
            None,
            None,
            "0" * 64,
            FredCompletenessV1.COMPLETE,
        )
    with pytest.raises(ValueError, match="invalid_query_identity"):
        _fact(query_identity="short")


def test_revision_identity_excludes_fetched_at_and_binds_material():
    base = _fact()
    later = _fact(fetched_at=FETCHED_AT + timedelta(days=3))
    assert later.revision_identity == base.revision_identity
    assert later.fetched_at != base.fetched_at
    revised = _observations()[:1] + (
        FredObservationV1(
            date(2026, 7, 1),
            Decimal(151300),
            FredValueStateV1.PRESENT,
            date(2026, 8, 7),
            date(9999, 12, 31),
        ),
    )
    assert _fact(observations=revised).revision_identity != base.revision_identity
    realtime_moved = _observations()[:1] + (
        FredObservationV1(
            date(2026, 7, 1),
            Decimal(151000),
            FredValueStateV1.PRESENT,
            date(2026, 8, 8),
            date(9999, 12, 31),
        ),
    )
    assert (
        _fact(observations=realtime_moved).revision_identity != base.revision_identity
    )
    assert _fact(release_id=50).revision_identity != base.revision_identity
    assert base.revision_identity == compute_revision_identity(
        query_identity=base.query_identity,
        series_id=base.series_id,
        observations=base.observations,
        release_id=None,
        release_date=None,
        vintage_date=None,
        completeness=FredCompletenessV1.COMPLETE,
    )


def test_failure_is_closed_code_plus_declared_retryability_only():
    assert {item.value for item in FredQueryFailureCodeV1} == {
        "AUTH",
        "TRANSIENT_TRANSPORT",
        "PROVIDER_CONTRACT",
        "RESPONSE_CONTRACT",
        "INCOMPLETE",
        "NOT_YET_RELEASED",
        "LOCAL_STATE",
    }
    failure = FredQueryFailureV1(FredQueryFailureCodeV1.TRANSIENT_TRANSPORT, True)
    assert set(failure.__dataclass_fields__) == {"code", "retryable"}
    with pytest.raises(ValueError, match="retryable_contract"):
        FredQueryFailureV1(FredQueryFailureCodeV1.AUTH, True)
    with pytest.raises(ValueError, match="retryable_contract"):
        FredQueryFailureV1(FredQueryFailureCodeV1.TRANSIENT_TRANSPORT, False)
    assert FredQueryFailureV1.for_code(FredQueryFailureCodeV1.AUTH).retryable is False


def test_release_schedule_requires_aware_utc_slot_and_ordered_validity():
    schedule = FredReleaseScheduleV1(
        SERIES_PAYEMS,
        datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
        "PAYEMS:2026-08-07",
        datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
        "a" * 64,
    )
    assert schedule.release_slot == "PAYEMS:2026-08-07"
    with pytest.raises(ValueError, match="aware_utc_required"):
        FredReleaseScheduleV1(
            SERIES_PAYEMS,
            datetime(2026, 8, 7, 12, 30),
            "PAYEMS:2026-08-07",
            datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
            "a" * 64,
        )
    with pytest.raises(ValueError, match="ordered_release_validity"):
        FredReleaseScheduleV1(
            SERIES_PAYEMS,
            datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
            "PAYEMS:2026-08-07",
            datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
            "a" * 64,
        )
    with pytest.raises(ValueError, match="invalid_provenance_digest"):
        FredReleaseScheduleV1(
            SERIES_PAYEMS,
            datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
            "PAYEMS:2026-08-07",
            datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
            "nope",
        )
    with pytest.raises(ValueError, match="unregistered_series"):
        FredReleaseScheduleV1(
            "UNRATE",
            datetime(2026, 8, 7, 12, 30, tzinfo=UTC),
            "UNRATE:2026-08-07",
            datetime(2026, 9, 4, 12, 30, tzinfo=UTC),
            "a" * 64,
        )


def test_due_decision_vocabulary_is_closed_and_carries_no_callable():
    assert {item.value for item in FredDueStateV1} == {
        "DUE_MISSING",
        "DUE_QUERY_CHANGED",
        "DUE_RELEASE",
        "DUE_STALE",
        "NOT_DUE_FRESH",
        "NOT_DUE_REVISION_CURRENT",
        "NOT_DUE_BEFORE_RELEASE",
        "NOT_DUE_COOLDOWN",
        "UNAVAILABLE_RELEASE_SCHEDULE",
        "UNAVAILABLE_LOCAL_STATE",
    }
    assert {item.value for item in FredCoverageV1} == {
        "USED",
        "OMITTED_NOT_DUE",
        "OMITTED_UNAVAILABLE",
        "OMITTED_INCOMPLETE",
    }
    assert {item.value for item in FredDirectionV1} == {
        "EXPANDING",
        "CONTRACTING",
        "FLAT",
    }
    decision = FredDueDecisionV1(
        FredDueStateV1.DUE_MISSING, _query().query_identity, "PAYEMS:2026-08-07"
    )
    assert set(decision.__dataclass_fields__) == {"state", "query_identity", "due_slot"}
    assert not any(
        callable(getattr(decision, name)) for name in decision.__dataclass_fields__
    )
    with pytest.raises(ValueError, match="invalid_query_identity"):
        FredDueDecisionV1(FredDueStateV1.DUE_MISSING, "short", None)


def test_g1_policy_constants_are_the_declared_values():
    assert FRESH_FOR == timedelta(hours=24)
    assert RELEASE_GRACE == timedelta(minutes=15)
    assert FAILURE_COOLDOWN == timedelta(hours=6)
    assert STARTED_LEASE == timedelta(minutes=30)
    assert MAX_TRANSPORT_CALLS == 1
    assert LOCAL_STATE_UNKNOWN_START == "LOCAL_STATE_UNKNOWN_START"


def test_not_due_revision_current_is_not_a_wire_state():
    from tokmo.values.fred_query import DUE_STATES

    assert FredDueStateV1.NOT_DUE_REVISION_CURRENT not in DUE_STATES
    assert FredDueStateV1.NOT_DUE_REVISION_CURRENT.value == "NOT_DUE_REVISION_CURRENT"


# ---------------------------------------------------------------------------
# G2 Delta SDS §4.1 / §8.1 acceptance A1-A5: the release-schedule builder.
# ---------------------------------------------------------------------------

OFFICIAL_AT = datetime(2026, 8, 7, 12, 30, tzinfo=timezone.utc)
SCHEDULE_PROVENANCE = {
    "fred_release_id": 50,
    "fred_release_date": "2026-08-07",
    "bls_source_id": "BLS_ICS_V1",
    "bls_source_url": "https://www.bls.gov/schedule/news_release/bls.ics",
    "bls_summary": "Employment Situation",
    "bls_tzid": "US-Eastern",
    "bls_resolved_zone": "America/New_York",
    "official_release_at_utc": "2026-08-07T12:30:00.000000Z",
    "bls_body_sha256": "c" * 64,
    "bls_body_bytes": 1024,
    "grace_seconds": 900,
    "join_rule": "SUMMARY_EXACT+TZID_ALLOWLIST+DST_CROSSCHECK+LOCAL_DATE_EQUAL+SINGLETON",
}


def _schedule(**overrides):
    from tokmo.values.fred_query import build_fred_release_schedule_v1

    fields = dict(
        series_id=SERIES_PAYEMS,
        release_id=50,
        release_date=date(2026, 8, 7),
        official_release_at=OFFICIAL_AT,
        schedule_provenance=SCHEDULE_PROVENANCE,
    )
    fields.update(overrides)
    return build_fred_release_schedule_v1(**fields)


def test_a1_the_builder_carries_the_official_instant_with_only_the_15m_grace():
    from tokmo.values.fred_query import SCHEDULE_DOMAIN, domain_digest

    schedule = _schedule()
    assert schedule.release_at == OFFICIAL_AT
    assert schedule.eligible_from == OFFICIAL_AT + timedelta(minutes=15)
    assert schedule.eligible_from == datetime(2026, 8, 7, 12, 45, tzinfo=timezone.utc)
    assert schedule.provenance_digest == domain_digest(
        SCHEDULE_DOMAIN, SCHEDULE_PROVENANCE
    )


def test_a2_valid_until_is_the_official_instant_plus_twenty_one_days():
    from tokmo.values.fred_query import SCHEDULE_VALID_FOR

    assert SCHEDULE_VALID_FOR == timedelta(days=21)
    schedule = _schedule()
    assert schedule.valid_until == OFFICIAL_AT + timedelta(days=21)


@pytest.mark.parametrize(
    "overrides,reason",
    [
        ({"official_release_at": datetime(2026, 8, 7, 12, 30)}, "aware_utc_required"),
        (
            {
                "official_release_at": datetime(
                    2026, 8, 7, 8, 30, tzinfo=timezone(timedelta(hours=-4))
                )
            },
            "aware_utc_required",
        ),
        ({"release_id": 0}, "invalid_release_id"),
        ({"release_id": -3}, "invalid_release_id"),
        ({"release_id": True}, "invalid_release_id"),
        ({"release_id": "50"}, "invalid_release_id"),
        ({"release_date": "2026-08-07"}, "invalid_release_date"),
        ({"release_date": date(2026, 8, 5)}, "cross_authority_gap"),
        ({"series_id": "UNRATE"}, "unregistered_series"),
    ],
)
def test_a3_the_builder_rejects_every_unsound_authority_input(overrides, reason):
    with pytest.raises(ValueError, match=reason):
        _schedule(**overrides)


def test_a3_a_one_day_cross_authority_gap_is_admitted():
    assert _schedule(release_date=date(2026, 8, 6)).release_slot == "PAYEMS:2026-08-06"
    assert _schedule(release_date=date(2026, 8, 8)).release_slot == "PAYEMS:2026-08-08"


def test_a4_the_slot_is_deterministic_and_the_digest_excludes_the_fetch_instant():
    first, second = _schedule(), _schedule()
    assert first.release_slot == "PAYEMS:2026-08-07"
    assert 1 <= len(first.release_slot) <= 120
    assert first.provenance_digest == second.provenance_digest
    with_fetch = _schedule(
        schedule_provenance=dict(SCHEDULE_PROVENANCE, bls_body_bytes=2048)
    )
    assert with_fetch.provenance_digest != first.provenance_digest


def test_a5_the_r2_schedule_domain_is_load_bearing():
    from tokmo.values.fred_query import SCHEDULE_DOMAIN, domain_digest

    assert SCHEDULE_DOMAIN == "tokmo.fred-query.schedule.r2"
    stale = domain_digest("tokmo.fred-query.schedule.r1", SCHEDULE_PROVENANCE)
    assert stale != _schedule().provenance_digest


class _NoLocalStateCache:
    """A cache with no stored success; it records every read it is asked for."""

    class _Attempts:
        highest_attempt_seq = 0
        active_started_at = None
        last_failure_at = None
        last_failure_code = None

    def __init__(self):
        self.reads = []
        self.attempt_reads = []

    def read_success_for_series(self, series_id, *, now):
        self.reads.append((series_id, now))

    def read_attempt_state(self, query_identity, due_slot, *, now):
        self.attempt_reads.append((query_identity, due_slot, now))
        return self._Attempts()

    def claim(self, query_identity, due_slot, *, now):
        raise AssertionError("no_claim_without_a_valid_schedule")


def test_a2_an_expired_schedule_closes_unavailable_with_wire_zero():
    from tokmo.programs.macro_fred_inputs import (
        evaluate_fred_due_v1,
        run_macro_fred_enrichment_v1,
    )

    schedule = _schedule()
    query = FredSeriesQueryV1(
        SERIES_PAYEMS, date(2026, 5, 1), date(2026, 7, 31), OFFICIAL_AT
    )

    class _RefusingPort:
        def query_series(self, _query):
            raise AssertionError("no_wire_without_a_valid_schedule")

    inside = schedule.valid_until
    outside = schedule.valid_until + timedelta(microseconds=1)
    assert (
        evaluate_fred_due_v1(
            query=query,
            schedule=schedule,
            cache=_NoLocalStateCache(),
            now=inside,
        ).state
        is not FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE
    )
    expired_cache = _NoLocalStateCache()
    assert (
        evaluate_fred_due_v1(
            query=query,
            schedule=schedule,
            cache=expired_cache,
            now=outside,
        ).state
        is FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE
    )
    assert expired_cache.attempt_reads == []
    result = run_macro_fred_enrichment_v1(
        query=query,
        schedule=schedule,
        cache=_NoLocalStateCache(),
        query_port=_RefusingPort(),
        clock=lambda: outside,
    )
    assert result.due_state is FredDueStateV1.UNAVAILABLE_RELEASE_SCHEDULE
    assert result.provider_starts == 0
    assert result.coverage is FredCoverageV1.OMITTED_UNAVAILABLE


def test_a1_the_value_layer_holds_no_next_day_or_assumed_time_of_day_rule():
    """The instant comes from the BLS authority alone (D3)."""
    from pathlib import Path

    source = (
        Path(__file__).parents[1].joinpath("src/tokmo/values/fred_query.py").read_text()
    )
    for forbidden in (
        "RELEASE_DAY_CLOSE",
        "next_day",
        "NEXT_DAY",
        "08:30",
        "hours=8",
        "zoneinfo",
        "ZoneInfo",
        "America/",
        "timedelta(days=1)",
    ):
        assert forbidden not in source, forbidden
    # The only interval between the official instant and eligibility is the
    # declared 15-minute grace.
    assert _schedule().eligible_from - _schedule().release_at == RELEASE_GRACE
