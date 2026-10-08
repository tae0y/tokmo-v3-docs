"""Signal Program lifecycle types (SDS §3).

Pure, immutable value objects defining program identity, lifecycle status,
and the deterministic status-evaluation function. No I/O, no wall-clock —
all datetimes are caller-supplied.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

__all__ = [
    "ProgramKindV1",
    "ProgramLifecycleStatusV1",
    "LifecycleReasonCode",
    "ProgramLifecycleSpecV1",
    "ProgramLifecycleStateV1",
    "evaluate_lifecycle_status",
]


# ==========================================================================
# §3.1  Program identity
# ==========================================================================

class ProgramKindV1(Enum):
    CORE_ANALYZER = "CORE_ANALYZER"
    HYPOTHESIS_SENSOR = "HYPOTHESIS_SENSOR"


# ==========================================================================
# §3.2  Lifecycle status — closed set of 6 members
# ==========================================================================

class ProgramLifecycleStatusV1(Enum):
    SHADOW = "SHADOW"
    ACTIVE = "ACTIVE"
    REVIEW_DUE = "REVIEW_DUE"
    DORMANT = "DORMANT"
    RETIRED = "RETIRED"
    EXPIRED = "EXPIRED"


# ==========================================================================
# §3.7  Lifecycle reason codes
# ==========================================================================

class LifecycleReasonCode(Enum):
    OK = "OK"
    NOT_YET_ACTIVE = "NOT_YET_ACTIVE"
    REVIEW_DUE = "REVIEW_DUE"
    EXPIRED = "EXPIRED"
    DORMANT = "DORMANT"
    RETIRED = "RETIRED"


# ==========================================================================
# Helpers
# ==========================================================================

import re  # noqa: E402

_PROGRAM_ID_RE: re.Pattern[str] = re.compile(r"^[A-Za-z0-9_-]+$")
_VERSION_RE: re.Pattern[str] = re.compile(r"^\d+\.\d+\.\d+$")
_DIGEST_RE: re.Pattern[str] = re.compile(r"^[0-9a-f]{64}$")


def _require_aware(dt: datetime, field_name: str) -> None:
    """Reject naive datetimes per the aware-datetime validation pattern."""
    if not isinstance(dt, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    if dt.tzinfo is None or dt.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


# ==========================================================================
# §3.3  Lifecycle spec — immutable policy
# ==========================================================================

@dataclass(frozen=True)
class ProgramLifecycleSpecV1:
    program_id: str
    version: str
    kind: ProgramKindV1
    activated_at: datetime
    review_due_at: datetime
    expires_at: datetime | None
    review_cadence_days: int
    spec_digest: str

    def __post_init__(self) -> None:
        # program_id
        if not isinstance(self.program_id, str) or not self.program_id:
            raise ValueError("program_id must be non-empty str")
        if len(self.program_id) > 128:
            raise ValueError("program_id must be <= 128 chars")
        if not _PROGRAM_ID_RE.match(self.program_id):
            raise ValueError("program_id must match ^[A-Za-z0-9_-]+$")

        # version
        if not isinstance(self.version, str) or not _VERSION_RE.match(self.version):
            raise ValueError("version must match MAJOR.MINOR.PATCH")

        # aware datetimes
        _require_aware(self.activated_at, "activated_at")
        _require_aware(self.review_due_at, "review_due_at")

        # kind-specific rules
        if self.kind is ProgramKindV1.CORE_ANALYZER:
            if self.expires_at is not None:
                raise ValueError("CORE_ANALYZER must have expires_at=None")
        elif self.kind is ProgramKindV1.HYPOTHESIS_SENSOR:
            if self.expires_at is None:
                raise ValueError("HYPOTHESIS_SENSOR requires expires_at")
            _require_aware(self.expires_at, "expires_at")
            if not (self.activated_at < self.review_due_at < self.expires_at):
                raise ValueError(
                    "HYPOTHESIS_SENSOR requires activated_at < review_due_at < expires_at"
                )

        # review_cadence_days
        if not isinstance(self.review_cadence_days, int) or isinstance(
            self.review_cadence_days, bool
        ):
            raise ValueError("review_cadence_days must be int")
        if not 1 <= self.review_cadence_days <= 365:
            raise ValueError("review_cadence_days must be 1..365")

        # spec_digest
        if not isinstance(self.spec_digest, str) or not _DIGEST_RE.match(self.spec_digest):
            raise ValueError("spec_digest must be 64-char lowercase hex")


# ==========================================================================
# §3.4  Lifecycle state — effective state at evaluation time
# ==========================================================================

@dataclass(frozen=True)
class ProgramLifecycleStateV1:
    spec: ProgramLifecycleSpecV1
    status: ProgramLifecycleStatusV1
    last_evidence_at: datetime | None
    evidence_count: int
    renewal_count: int
    as_of: datetime

    def __post_init__(self) -> None:
        spec = self.spec

        # as_of aware
        _require_aware(self.as_of, "as_of")

        # last_evidence_at
        if self.last_evidence_at is not None:
            _require_aware(self.last_evidence_at, "last_evidence_at")
            if self.last_evidence_at > self.as_of:
                raise ValueError("last_evidence_at must be <= as_of")

        # evidence_count, renewal_count
        if not isinstance(self.evidence_count, int) or isinstance(
            self.evidence_count, bool
        ) or self.evidence_count < 0:
            raise ValueError("evidence_count must be int >= 0")
        if not isinstance(self.renewal_count, int) or isinstance(
            self.renewal_count, bool
        ) or self.renewal_count < 0:
            raise ValueError("renewal_count must be int >= 0")

        kind = spec.kind
        status = self.status

        # CORE invariant
        if kind is ProgramKindV1.CORE_ANALYZER:
            if self.renewal_count != 0:
                raise ValueError("CORE_ANALYZER renewal_count must be 0")
            if spec.expires_at is not None:
                raise ValueError("CORE_ANALYZER spec.expires_at must be None")
            if status is ProgramLifecycleStatusV1.EXPIRED:
                raise ValueError("EXPIRED is unreachable for CORE_ANALYZER")

        # HYPOTHESIS expiry consistency
        if kind is ProgramKindV1.HYPOTHESIS_SENSOR:
            if status is ProgramLifecycleStatusV1.EXPIRED:
                if self.as_of < spec.expires_at:  # type: ignore[operator]
                    raise ValueError(
                        "EXPIRED requires as_of >= expires_at for HYPOTHESIS_SENSOR"
                    )
            else:
                if self.as_of >= spec.expires_at:  # type: ignore[operator]
                    raise ValueError(
                        "non-EXPIRED HYPOTHESIS_SENSOR requires as_of < expires_at"
                    )

        # SHADOW vs activated
        if status is ProgramLifecycleStatusV1.SHADOW:
            if self.as_of >= spec.activated_at:
                raise ValueError("SHADOW requires as_of < activated_at")
        else:
            if self.as_of < spec.activated_at:
                raise ValueError(
                    f"{status.value} requires as_of >= activated_at"
                )

        # REVIEW_DUE consistency
        if status is ProgramLifecycleStatusV1.REVIEW_DUE:
            if self.as_of < spec.review_due_at:
                raise ValueError("REVIEW_DUE requires as_of >= review_due_at")
        if status is ProgramLifecycleStatusV1.ACTIVE:
            if self.as_of >= spec.review_due_at:
                raise ValueError("ACTIVE requires as_of < review_due_at")

        # DORMANT / RETIRED — explicit injected states; no time-inconsistency
        # check (they may occur regardless of current due/expiry time per §3.4).


# ==========================================================================
# §3.5  Status evaluation — pure deterministic function
# ==========================================================================

_BLOCKING_REASON: dict[ProgramLifecycleStatusV1, LifecycleReasonCode] = {
    ProgramLifecycleStatusV1.SHADOW: LifecycleReasonCode.NOT_YET_ACTIVE,
    ProgramLifecycleStatusV1.DORMANT: LifecycleReasonCode.DORMANT,
    ProgramLifecycleStatusV1.RETIRED: LifecycleReasonCode.RETIRED,
    ProgramLifecycleStatusV1.EXPIRED: LifecycleReasonCode.EXPIRED,
}


def evaluate_lifecycle_status(
    spec: ProgramLifecycleSpecV1,
    *,
    as_of: datetime,
    last_evidence_at: datetime | None,
    evidence_count: int,
    renewal_count: int,
) -> ProgramLifecycleStateV1:
    """Deterministic lifecycle status (§3.5, first-match-wins ordering).

    1. as_of < activated_at → SHADOW
    2. HYPOTHESIS and as_of >= expires_at → EXPIRED
    3. as_of >= review_due_at → REVIEW_DUE
    4. otherwise → ACTIVE
    """
    _require_aware(as_of, "as_of")
    if last_evidence_at is not None:
        _require_aware(last_evidence_at, "last_evidence_at")
        if last_evidence_at > as_of:
            raise ValueError("last_evidence_at must be <= as_of")

    if as_of < spec.activated_at:
        status = ProgramLifecycleStatusV1.SHADOW
    elif (
        spec.kind is ProgramKindV1.HYPOTHESIS_SENSOR
        and spec.expires_at is not None
        and as_of >= spec.expires_at
    ):
        status = ProgramLifecycleStatusV1.EXPIRED
    elif as_of >= spec.review_due_at:
        status = ProgramLifecycleStatusV1.REVIEW_DUE
    else:
        status = ProgramLifecycleStatusV1.ACTIVE

    return ProgramLifecycleStateV1(
        spec=spec,
        status=status,
        last_evidence_at=last_evidence_at,
        evidence_count=evidence_count,
        renewal_count=renewal_count,
        as_of=as_of,
    )
