"""Standalone typed vocabulary for the routine FRED series query boundary.

Delta SDS §2/§3: this module is domain-separated from the R1-R4 campaign
vocabulary. It imports nothing from the repository — the canonical
serializer and the digests below are built from the standard library only —
so no campaign constant, manifest revision or ordinal can enter the routine
query value graph.

Every identity here is domain-separated. ``query_identity`` addresses the
question (series, closed observation range, revision policy) and therefore
excludes the decision time, so repeated decisions address one query.
``revision_identity`` addresses the answered material (provider realtime
ranges, normalized observations, release/vintage metadata) and therefore
excludes ``fetched_at``, so the same material fetched later is the same
revision.
"""

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from enum import Enum

QUERY_DOMAIN = "tokmo.fred-query.query.r1"
REVISION_DOMAIN = "tokmo.fred-query.revision.r1"
PROVENANCE_DOMAIN = "tokmo.fred-query.provenance.r1"
# G2 Delta SDS §4.1. The ``.r2`` suffix is load-bearing: revision B widened
# the digested provenance field set, so a digest taken over an earlier shape
# can never compare equal to a current one. Any later change to that field
# set must bump this suffix again.
SCHEDULE_DOMAIN = "tokmo.fred-query.schedule.r2"

SERIES_PAYEMS = "PAYEMS"

# Delta SDS §4 PAYEMS G1 policy constants. These are internal initial policy
# values owned by the Macro program, not provider facts.
FRESH_FOR = timedelta(hours=24)
RELEASE_GRACE = timedelta(minutes=15)
FAILURE_COOLDOWN = timedelta(hours=6)
STARTED_LEASE = timedelta(minutes=30)
# G2 Delta SDS §4.1: strictly shorter than the ~28 day minimum interval
# between consecutive Employment Situation releases, so a persisted schedule
# can never outlive the release it describes.
SCHEDULE_VALID_FOR = timedelta(days=21)
# Pure cross-authority sanity bound between the FRED release date and the
# BLS instant; it catches a gross join error without needing timezone data
# in the value layer.
MAX_SCHEDULE_JOIN_GAP_DAYS = 1
MAX_TRANSPORT_CALLS = 1
LOCAL_STATE_UNKNOWN_START = "LOCAL_STATE_UNKNOWN_START"


class FredQueryCacheCorruption(ValueError):
    """Typed local-state failure raised by the Records cache.

    It lives in the value vocabulary so the Macro program can close over it
    without importing the Records adapter it is handed as a dependency.
    Corruption never triggers provider access.
    """


class FredRevisionPolicyV1(str, Enum):
    INITIAL_RELEASE = "INITIAL_RELEASE"
    LATEST_AVAILABLE = "LATEST_AVAILABLE"


class FredValueStateV1(str, Enum):
    PRESENT = "PRESENT"
    MISSING = "MISSING"


class FredCompletenessV1(str, Enum):
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"


class FredQueryFailureCodeV1(str, Enum):
    AUTH = "AUTH"
    TRANSIENT_TRANSPORT = "TRANSIENT_TRANSPORT"
    PROVIDER_CONTRACT = "PROVIDER_CONTRACT"
    RESPONSE_CONTRACT = "RESPONSE_CONTRACT"
    INCOMPLETE = "INCOMPLETE"
    NOT_YET_RELEASED = "NOT_YET_RELEASED"
    LOCAL_STATE = "LOCAL_STATE"


class FredDueStateV1(str, Enum):
    DUE_MISSING = "DUE_MISSING"
    DUE_QUERY_CHANGED = "DUE_QUERY_CHANGED"
    DUE_RELEASE = "DUE_RELEASE"
    DUE_STALE = "DUE_STALE"
    NOT_DUE_FRESH = "NOT_DUE_FRESH"
    NOT_DUE_REVISION_CURRENT = "NOT_DUE_REVISION_CURRENT"
    NOT_DUE_BEFORE_RELEASE = "NOT_DUE_BEFORE_RELEASE"
    NOT_DUE_COOLDOWN = "NOT_DUE_COOLDOWN"
    UNAVAILABLE_RELEASE_SCHEDULE = "UNAVAILABLE_RELEASE_SCHEDULE"
    UNAVAILABLE_LOCAL_STATE = "UNAVAILABLE_LOCAL_STATE"


class FredCoverageV1(str, Enum):
    USED = "USED"
    OMITTED_NOT_DUE = "OMITTED_NOT_DUE"
    OMITTED_UNAVAILABLE = "OMITTED_UNAVAILABLE"
    OMITTED_INCOMPLETE = "OMITTED_INCOMPLETE"


class FredDirectionV1(str, Enum):
    """Factual period-over-period direction, never an investment judgment."""

    EXPANDING = "EXPANDING"
    CONTRACTING = "CONTRACTING"
    FLAT = "FLAT"


DUE_STATES = frozenset(
    {
        FredDueStateV1.DUE_MISSING,
        FredDueStateV1.DUE_QUERY_CHANGED,
        FredDueStateV1.DUE_RELEASE,
        FredDueStateV1.DUE_STALE,
    }
)

RETRYABLE_FAILURE_CODES = frozenset({FredQueryFailureCodeV1.TRANSIENT_TRANSPORT})

# The closed G1 registry: only the PAYEMS initial-release vertical has a
# verified provider contract. ``output_type=4`` returns initial-release
# observations, so LATEST_AVAILABLE is deliberately not admitted.
SERIES_REGISTRY = {
    SERIES_PAYEMS: frozenset({FredRevisionPolicyV1.INITIAL_RELEASE}),
}


def canonical_decimal(value: Decimal) -> str:
    """Scale-independent canonical spelling of a finite Decimal."""
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError("noncanonical_decimal")
    if value.is_zero():
        return "0"
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    sign, digits = ("-", text[1:]) if text.startswith("-") else ("", text)
    if len(digits) > 1 and digits[0] == "0" and digits[1] != ".":
        digits = digits.lstrip("0") or "0"
    return sign + digits


def canonical_json(value: object) -> bytes:
    """Deterministic canonical JSON for this domain, stdlib only."""

    def clean(item):
        if isinstance(item, Enum):
            return item.value
        if type(item) is date:
            return item.isoformat()
        if type(item) is datetime:
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("aware_utc_required")
            return (
                item.astimezone(UTC)
                .isoformat(timespec="microseconds")
                .replace("+00:00", "Z")
            )
        if isinstance(item, Decimal):
            return canonical_decimal(item)
        if isinstance(item, str):
            return item
        if isinstance(item, dict):
            return {str(key): clean(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [clean(val) for val in item]
        if item is None or type(item) in (bool, int):
            return item
        raise ValueError("noncanonical_value")

    return (
        json.dumps(
            clean(value), sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        + "\n"
    ).encode("utf-8")


def domain_digest(domain: str, payload: object) -> str:
    if type(domain) is not str or not domain:
        raise ValueError("invalid_domain")
    return hashlib.sha256(
        canonical_json({"domain": domain, "payload": payload})
    ).hexdigest()


def _require_date(value: object, name: str) -> None:
    if type(value) is not date:
        raise ValueError(f"invalid_{name}")


def _require_optional_date(value: object, name: str) -> None:
    if value is not None:
        _require_date(value, name)


def _require_aware_utc(value: object) -> None:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError("aware_utc_required")


def _require_digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or any(character not in "0123456789abcdef" for character in value)
    ):
        raise ValueError(f"invalid_{name}")


def _require_registered_series(series_id: object) -> None:
    if type(series_id) is not str or series_id not in SERIES_REGISTRY:
        raise ValueError("unregistered_series")


def _require_slot(value: object, name: str) -> None:
    if type(value) is not str or not 1 <= len(value) <= 120:
        raise ValueError(f"invalid_{name}")


@dataclass(frozen=True)
class FredSeriesQueryV1:
    series_id: str
    observation_start: date
    observation_end: date
    as_of: datetime
    revision_policy: FredRevisionPolicyV1 = FredRevisionPolicyV1.INITIAL_RELEASE

    def __post_init__(self):
        _require_registered_series(self.series_id)
        if not isinstance(self.revision_policy, FredRevisionPolicyV1):
            raise ValueError("unregistered_revision_policy")
        if self.revision_policy not in SERIES_REGISTRY[self.series_id]:
            raise ValueError("unregistered_revision_policy")
        _require_date(self.observation_start, "observation_start")
        _require_date(self.observation_end, "observation_end")
        if self.observation_start > self.observation_end:
            raise ValueError("ordered_observation_range")
        _require_aware_utc(self.as_of)

    @property
    def query_identity(self) -> str:
        return domain_digest(
            QUERY_DOMAIN,
            {
                "series_id": self.series_id,
                "observation_start": self.observation_start,
                "observation_end": self.observation_end,
                "revision_policy": self.revision_policy,
            },
        )


@dataclass(frozen=True)
class FredObservationV1:
    observation_date: date
    value: Decimal | None
    value_state: FredValueStateV1
    realtime_start: date
    realtime_end: date

    def __post_init__(self):
        _require_date(self.observation_date, "observation_date")
        _require_date(self.realtime_start, "realtime_start")
        _require_date(self.realtime_end, "realtime_end")
        if not isinstance(self.value_state, FredValueStateV1):
            raise ValueError("invalid_value_state")
        present = self.value_state is FredValueStateV1.PRESENT
        if present != (type(self.value) is Decimal):
            raise ValueError("value_state_contract")
        if present and not self.value.is_finite():
            raise ValueError("value_state_contract")
        if self.realtime_start > self.realtime_end:
            raise ValueError("ordered_realtime_range")

    def normalized(self) -> dict:
        return {
            "observation_date": self.observation_date,
            "value": None if self.value is None else canonical_decimal(self.value),
            "value_state": self.value_state,
            "realtime_start": self.realtime_start,
            "realtime_end": self.realtime_end,
        }


def compute_revision_identity(
    *,
    query_identity: str,
    series_id: str,
    observations: tuple,
    release_id: int | None,
    release_date: date | None,
    vintage_date: date | None,
    completeness: FredCompletenessV1,
) -> str:
    """Digest the answered material; ``fetched_at`` is deliberately excluded."""
    return domain_digest(
        REVISION_DOMAIN,
        {
            "query_identity": query_identity,
            "series_id": series_id,
            "release_id": release_id,
            "release_date": release_date,
            "vintage_date": vintage_date,
            "completeness": completeness,
            "observations": [item.normalized() for item in observations],
        },
    )


@dataclass(frozen=True)
class FredSeriesFactV1:
    query_identity: str
    series_id: str
    observations: tuple
    fetched_at: datetime
    release_id: int | None
    release_date: date | None
    vintage_date: date | None
    revision_identity: str
    completeness: FredCompletenessV1

    def __post_init__(self):
        _require_registered_series(self.series_id)
        _require_digest(self.query_identity, "query_identity")
        if type(self.observations) is not tuple:
            raise ValueError("invalid_observations")
        if not self.observations:
            raise ValueError("empty_observations")
        if any(not isinstance(item, FredObservationV1) for item in self.observations):
            raise ValueError("invalid_observations")
        days = [item.observation_date for item in self.observations]
        if days != sorted(days) or len(set(days)) != len(days):
            raise ValueError("unordered_or_duplicate_observations")
        _require_aware_utc(self.fetched_at)
        if self.release_id is not None and (
            type(self.release_id) is not int or self.release_id <= 0
        ):
            raise ValueError("invalid_release_id")
        _require_optional_date(self.release_date, "release_date")
        _require_optional_date(self.vintage_date, "vintage_date")
        if not isinstance(self.completeness, FredCompletenessV1):
            raise ValueError("invalid_completeness")
        _require_digest(self.revision_identity, "revision_identity")
        if self.revision_identity != compute_revision_identity(
            query_identity=self.query_identity,
            series_id=self.series_id,
            observations=self.observations,
            release_id=self.release_id,
            release_date=self.release_date,
            vintage_date=self.vintage_date,
            completeness=self.completeness,
        ):
            raise ValueError("revision_identity_mismatch")

    @property
    def present_observations(self) -> tuple:
        return tuple(
            item
            for item in self.observations
            if item.value_state is FredValueStateV1.PRESENT
        )


def build_fred_series_fact_v1(
    *,
    query_identity: str,
    series_id: str,
    observations: tuple,
    fetched_at: datetime,
    release_id: int | None,
    release_date: date | None,
    vintage_date: date | None,
    completeness: FredCompletenessV1,
) -> FredSeriesFactV1:
    return FredSeriesFactV1(
        query_identity,
        series_id,
        observations,
        fetched_at,
        release_id,
        release_date,
        vintage_date,
        compute_revision_identity(
            query_identity=query_identity,
            series_id=series_id,
            observations=observations,
            release_id=release_id,
            release_date=release_date,
            vintage_date=vintage_date,
            completeness=completeness,
        ),
        completeness,
    )


@dataclass(frozen=True)
class FredQueryFailureV1:
    """A closed failure: no raw body, URL, credential or provider message."""

    code: FredQueryFailureCodeV1
    retryable: bool

    def __post_init__(self):
        if not isinstance(self.code, FredQueryFailureCodeV1):
            raise ValueError("invalid_failure_code")
        if type(self.retryable) is not bool:
            raise ValueError("retryable_contract")
        if self.retryable != (self.code in RETRYABLE_FAILURE_CODES):
            raise ValueError("retryable_contract")

    @classmethod
    def for_code(cls, code: FredQueryFailureCodeV1) -> "FredQueryFailureV1":
        if not isinstance(code, FredQueryFailureCodeV1):
            raise ValueError("invalid_failure_code")
        return cls(code, code in RETRYABLE_FAILURE_CODES)


@dataclass(frozen=True)
class FredReleaseScheduleV1:
    series_id: str
    release_at: datetime
    release_slot: str
    valid_until: datetime
    provenance_digest: str

    def __post_init__(self):
        _require_registered_series(self.series_id)
        _require_aware_utc(self.release_at)
        _require_aware_utc(self.valid_until)
        _require_slot(self.release_slot, "release_slot")
        _require_digest(self.provenance_digest, "provenance_digest")
        if self.release_at > self.valid_until:
            raise ValueError("ordered_release_validity")

    @property
    def eligible_from(self) -> datetime:
        return self.release_at + RELEASE_GRACE


def release_slot_for(series_id: str, release_date: date) -> str:
    """The canonical slot identity for one release of one registered series."""
    _require_registered_series(series_id)
    _require_date(release_date, "release_date")
    return f"{series_id}:{release_date.isoformat()}"


def build_fred_release_schedule_v1(
    *,
    series_id: str,
    release_id: int,
    release_date: date,
    official_release_at: datetime,
    schedule_provenance: object,
) -> FredReleaseScheduleV1:
    """Build the dual-authority schedule from an already-resolved instant.

    ``official_release_at`` is the BLS publication instant, already resolved
    to aware UTC by the Facts layer; this module stays stdlib-only and never
    resolves a timezone itself. The FRED release date and the BLS instant are
    two different authorities, so they are cross-checked here.
    """
    _require_registered_series(series_id)
    if type(release_id) is not int or type(release_id) is bool or release_id <= 0:
        raise ValueError("invalid_release_id")
    _require_date(release_date, "release_date")
    _require_aware_utc(official_release_at)
    if (
        abs((official_release_at.date() - release_date).days)
        > MAX_SCHEDULE_JOIN_GAP_DAYS
    ):
        raise ValueError("cross_authority_gap")
    return FredReleaseScheduleV1(
        series_id,
        official_release_at,
        release_slot_for(series_id, release_date),
        official_release_at + SCHEDULE_VALID_FOR,
        domain_digest(SCHEDULE_DOMAIN, schedule_provenance),
    )


@dataclass(frozen=True)
class FredDueDecisionV1:
    state: FredDueStateV1
    query_identity: str
    due_slot: str | None

    def __post_init__(self):
        if not isinstance(self.state, FredDueStateV1):
            raise ValueError("invalid_due_state")
        _require_digest(self.query_identity, "query_identity")
        if self.due_slot is not None:
            _require_slot(self.due_slot, "due_slot")


@dataclass(frozen=True)
class MacroFredInputV1:
    """Optional Macro employment enrichment plus exact coverage/provenance."""

    series_id: str
    coverage: FredCoverageV1
    due_state: FredDueStateV1
    provider_starts: int
    current_value: Decimal | None
    current_date: date | None
    prior_value: Decimal | None
    prior_date: date | None
    direction: FredDirectionV1 | None
    query_identity: str
    revision_identity: str | None
    provenance_digest: str

    def __post_init__(self):
        _require_registered_series(self.series_id)
        if not isinstance(self.coverage, FredCoverageV1):
            raise ValueError("invalid_coverage")
        if not isinstance(self.due_state, FredDueStateV1):
            raise ValueError("invalid_due_state")
        if self.provider_starts not in (0, 1) or type(self.provider_starts) is not int:
            raise ValueError("invalid_provider_starts")
        _require_digest(self.query_identity, "query_identity")
        _require_digest(self.provenance_digest, "provenance_digest")
        used = self.coverage is FredCoverageV1.USED
        if used:
            _require_digest(self.revision_identity, "revision_identity")
        elif (
            self.revision_identity is not None
            or self.current_value is not None
            or self.current_date is not None
            or self.prior_value is not None
            or self.prior_date is not None
            or self.direction is not None
        ):
            raise ValueError("omitted_coverage_carries_no_value")
        for value, day in (
            (self.current_value, self.current_date),
            (self.prior_value, self.prior_date),
        ):
            if value is not None and type(value) is not Decimal:
                raise ValueError("invalid_observation_value")
            _require_optional_date(day, "observation_date")
            if (value is None) != (day is None):
                raise ValueError("value_date_contract")
        if self.direction is not None:
            if not isinstance(self.direction, FredDirectionV1):
                raise ValueError("invalid_direction")
            if self.current_value is None or self.prior_value is None:
                raise ValueError("direction_requires_two_present_values")


def compute_macro_provenance_digest(
    *,
    query_identity: str,
    revision_identity: str | None,
    due_slot: str | None,
    due_state: FredDueStateV1,
    coverage: FredCoverageV1,
    schedule_provenance_digest: str | None,
) -> str:
    return domain_digest(
        PROVENANCE_DOMAIN,
        {
            "query_identity": query_identity,
            "revision_identity": revision_identity,
            "due_slot": due_slot,
            "due_state": due_state,
            "coverage": coverage,
            "schedule_provenance_digest": schedule_provenance_digest,
        },
    )


def observation_direction(observations: tuple) -> tuple:
    """Return (current, prior, direction) from the two latest PRESENT rows.

    A MISSING observation is skipped, never read as zero.
    """
    present = tuple(
        item for item in observations if item.value_state is FredValueStateV1.PRESENT
    )
    if not present:
        return None, None, None
    current = present[-1]
    if len(present) == 1:
        return current, None, None
    prior = present[-2]
    if current.value > prior.value:
        direction = FredDirectionV1.EXPANDING
    elif current.value < prior.value:
        direction = FredDirectionV1.CONTRACTING
    else:
        direction = FredDirectionV1.FLAT
    return current, prior, direction
