"""Pure, closed value vocabulary for the Stage3 FRED source."""

from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
import hashlib
import json
import re
import unicodedata

from tokmo.values.decimal_canon import (
    Decimal30CanonicalizationViolation, canonicalize_decimal30,
)

SOURCE = "FRED"
PURPOSE = "CANONICAL_SOURCE_STAGE3"
MANIFEST_REVISION = "fred-canonical-stage3-r2"
SERIES_ID = "PAYEMS"
CAMPAIGN_CAP = 10
PURPOSE_CUMULATIVE_CEILING = 20
CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R1"
MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R1"
CODE_REVISION = "fred-g-live-controller-r1"
R2_CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R2"
R2_MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R2"
R2_MANIFEST_REVISION = "fred-canonical-stage3-g-live-r2-diagnostic-v1"
R2_CODE_REVISION = "fred-g-live-controller-r2-diagnostic-v1"
R3_CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R3"
R3_MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R3"
R3_MANIFEST_REVISION = "fred-canonical-stage3-g-live-r3-provider-date-v1"
R3_CODE_REVISION = "fred-g-live-controller-r3-provider-date-v1"
R4_CAMPAIGN_CODE = "FRED_G_LIVE_PAYEMS_R4"
R4_MANIFEST_CODE = "FRED_G_LIVE_PAYEMS_FIVE_R4"
R4_MANIFEST_REVISION = "fred-canonical-stage3-g-live-r4-output4-realtime-v1"
R4_CODE_REVISION = "fred-g-live-controller-r4-output4-realtime-v1"


class FredOperation(str, Enum):
    SERIES = "SERIES"
    SERIES_OBSERVATIONS = "SERIES_OBSERVATIONS"
    SERIES_RELEASE = "SERIES_RELEASE"
    RELEASE_DATES = "RELEASE_DATES"
    SERIES_VINTAGE_DATES = "SERIES_VINTAGE_DATES"


class ExecutionMode(str, Enum):
    FIXTURE = "FIXTURE"
    LIVE = "LIVE"


class IdentityProvenance(str, Enum):
    PROVIDER = "PROVIDER"
    REQUEST = "REQUEST"


class PageCompleteness(str, Enum):
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"

    @classmethod
    def from_counts(cls, offset: int, returned: int, count: int):
        return cls.COMPLETE if offset == 0 and returned >= count else cls.TRUNCATED


class ReleaseClassification(str, Enum):
    INITIAL = "INITIAL"
    REVISION = "REVISION"
    UNKNOWN = "UNKNOWN"


def classification_flags(value: ReleaseClassification) -> tuple[bool | None, bool | None]:
    return {
        ReleaseClassification.INITIAL: (True, False),
        ReleaseClassification.REVISION: (False, True),
        ReleaseClassification.UNKNOWN: (None, None),
    }[value]


def _date(value: object, name: str) -> None:
    if value is not None and type(value) is not date:
        raise ValueError(f"invalid_{name}")


def canonical_json(value: object) -> bytes:
    def clean(item):
        if isinstance(item, Enum):
            return item.value
        if type(item) is date:
            return item.isoformat()
        if type(item) is datetime:
            if item.tzinfo is None or item.utcoffset() is None:
                raise ValueError("aware_utc_required")
            return item.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")
        if isinstance(item, Decimal):
            result = canonicalize_decimal30(str(item))
            if isinstance(result, Decimal30CanonicalizationViolation):
                raise ValueError(result.reason)
            return result
        if isinstance(item, str):
            return unicodedata.normalize("NFC", item)
        if isinstance(item, dict):
            return {str(key): clean(val) for key, val in item.items()}
        if isinstance(item, (tuple, list)):
            return [clean(val) for val in item]
        if item is None or type(item) in (bool, int):
            return item
        raise ValueError("noncanonical_value")
    return (json.dumps(clean(value), sort_keys=True, separators=(",", ":"),
                       ensure_ascii=False) + "\n").encode("utf-8")


@dataclass(frozen=True)
class FredRequest:
    operation: FredOperation
    series_id: str | None = None
    release_id: int | None = None
    realtime_start: date | None = None
    realtime_end: date | None = None
    observation_start: date | None = None
    observation_end: date | None = None
    output_type: int | None = None
    vintage_dates: tuple[date, ...] = ()
    limit: int | None = None
    offset: int = 0
    include_release_dates_with_no_data: bool = False

    def __post_init__(self):
        if not isinstance(self.operation, FredOperation):
            raise ValueError("closed_operation")
        if self.series_id is not None and not re.fullmatch(r"[A-Za-z0-9._-]{1,120}", self.series_id):
            raise ValueError("invalid_series_id")
        if self.release_id is not None and (type(self.release_id) is not int or self.release_id <= 0):
            raise ValueError("invalid_release_id")
        for name in ("realtime_start", "realtime_end", "observation_start", "observation_end"):
            _date(getattr(self, name), name)
        for left, right, name in ((self.realtime_start, self.realtime_end, "realtime"),
                                  (self.observation_start, self.observation_end, "observation")):
            if (left is None) != (right is None):
                raise ValueError(f"complete_{name}_pair")
            if left is not None and left > right:
                raise ValueError(f"ordered_{name}_pair")
        if type(self.offset) is not int or self.offset < 0:
            raise ValueError("invalid_offset")
        if self.output_type is not None and self.output_type not in (1, 2, 3, 4):
            raise ValueError("invalid_output_type")
        if len(self.vintage_dates) > 1000 or len(set(self.vintage_dates)) != len(self.vintage_dates) or tuple(sorted(self.vintage_dates)) != self.vintage_dates:
            raise ValueError("invalid_vintage_dates")
        if any(type(value) is not date for value in self.vintage_dates):
            raise ValueError("invalid_vintage_dates")
        if self.vintage_dates and self.output_type not in (2, 3):
            raise ValueError("vintage_dates_output_policy")
        if self.vintage_dates and self.realtime_start is not None:
            raise ValueError("realtime_vintage_exclusive")
        maxima = {FredOperation.SERIES_OBSERVATIONS: 100000,
                  FredOperation.SERIES_VINTAGE_DATES: 10000,
                  FredOperation.RELEASE_DATES: 10000}
        if self.limit is not None and (type(self.limit) is not int or
                                       not 1 <= self.limit <= maxima.get(self.operation, 10000)):
            raise ValueError("invalid_limit")
        if type(self.include_release_dates_with_no_data) is not bool:
            raise ValueError("invalid_include_release_dates_with_no_data")
        needs_series = self.operation is not FredOperation.RELEASE_DATES
        if needs_series != (self.series_id is not None):
            raise ValueError("operation_series_identity")
        if (self.operation is FredOperation.RELEASE_DATES) != (self.release_id is not None):
            raise ValueError("operation_release_identity")

    @property
    def request_identity(self) -> str:
        payload = {name: getattr(self, name) for name in self.__dataclass_fields__}
        return hashlib.sha256(canonical_json(payload)).hexdigest()


def canonical_fred_value(value: str) -> tuple[Decimal | None, str]:
    if value == ".":
        return None, "MISSING"
    result = canonicalize_decimal30(value)
    if isinstance(result, Decimal30CanonicalizationViolation):
        raise ValueError("invalid_fred_decimal")
    return Decimal(result), "PRESENT"


def invocation_identity(request: FredRequest, execution_mode: ExecutionMode) -> str:
    if not isinstance(execution_mode, ExecutionMode):
        raise ValueError("execution_mode_required")
    return hashlib.sha256(canonical_json({
        "source": SOURCE, "purpose": PURPOSE,
        "manifest_revision": MANIFEST_REVISION,
        "execution_mode": execution_mode.value,
        "operation": request.operation.value,
        "request_identity": request.request_identity,
    })).hexdigest()


def g_live_manifest(*, release_id: int) -> tuple[FredRequest, ...]:
    """Return the one closed Stage3 LIVE manifest in provider-call order."""
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid_release_id")
    realtime = date(2026, 8, 12)
    return (
        FredRequest(FredOperation.SERIES, series_id=SERIES_ID,
                    realtime_start=realtime, realtime_end=realtime),
        FredRequest(FredOperation.SERIES_OBSERVATIONS, series_id=SERIES_ID,
                    realtime_start=realtime, realtime_end=realtime,
                    observation_start=date(2026, 7, 1),
                    observation_end=date(2026, 7, 31), output_type=4,
                    limit=10, offset=0),
        FredRequest(FredOperation.SERIES_RELEASE, series_id=SERIES_ID,
                    realtime_start=realtime, realtime_end=realtime),
        FredRequest(FredOperation.RELEASE_DATES, release_id=release_id,
                    include_release_dates_with_no_data=False, limit=10, offset=0),
        FredRequest(FredOperation.SERIES_VINTAGE_DATES, series_id=SERIES_ID,
                    limit=10, offset=0),
    )


def g_live_manifest_digest() -> str:
    manifest = []
    for index, request in enumerate(g_live_manifest(release_id=1), 1):
        fields = {name: getattr(request, name) for name in request.__dataclass_fields__}
        if index == 4:
            fields["release_id"] = {"type": "operation_3_release_id", "ordinal": 3}
        manifest.append(fields)
    return hashlib.sha256(canonical_json({
        "campaign_code": CAMPAIGN_CODE,
        "manifest_code": MANIFEST_CODE,
        "manifest_revision": MANIFEST_REVISION,
        "operations": manifest,
    })).hexdigest()


MANIFEST_DIGEST = g_live_manifest_digest()


R2_MANIFEST_DIGEST = "fa0d82ac80678e74c02e2b05b642ce225a756f383727b814664ff134622fd134"


def r3_g_live_manifest(*, release_id: int) -> tuple[FredRequest, ...]:
    """Return the closed Stage3 LIVE R3 manifest (no realtime pair) in provider-call order."""
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid_release_id")
    return (
        FredRequest(FredOperation.SERIES, series_id=SERIES_ID),
        FredRequest(FredOperation.SERIES_OBSERVATIONS, series_id=SERIES_ID,
                    observation_start=date(2026, 7, 1),
                    observation_end=date(2026, 7, 31), output_type=4,
                    limit=10, offset=0),
        FredRequest(FredOperation.SERIES_RELEASE, series_id=SERIES_ID),
        FredRequest(FredOperation.RELEASE_DATES, release_id=release_id,
                    include_release_dates_with_no_data=False, limit=10, offset=0),
        FredRequest(FredOperation.SERIES_VINTAGE_DATES, series_id=SERIES_ID,
                    limit=10, offset=0),
    )


R3_MANIFEST_DIGEST = "da4d281aa2deae985b818b2d74dadfb922ef259721985c8aa26b348059828d51"


def r4_g_live_manifest(*, release_id: int) -> tuple[FredRequest, ...]:
    """Return the closed Stage3 LIVE R4 manifest in provider-call order.

    Only operation 2 carries the broad historical realtime pair
    (1776-07-04 / 9999-12-31); all other operations omit realtime.
    """
    if type(release_id) is not int or release_id <= 0:
        raise ValueError("invalid_release_id")
    return (
        FredRequest(FredOperation.SERIES, series_id=SERIES_ID),
        FredRequest(FredOperation.SERIES_OBSERVATIONS, series_id=SERIES_ID,
                    realtime_start=date(1776, 7, 4), realtime_end=date(9999, 12, 31),
                    observation_start=date(2026, 7, 1),
                    observation_end=date(2026, 7, 31), output_type=4,
                    limit=10, offset=0),
        FredRequest(FredOperation.SERIES_RELEASE, series_id=SERIES_ID),
        FredRequest(FredOperation.RELEASE_DATES, release_id=release_id,
                    include_release_dates_with_no_data=False, limit=10, offset=0),
        FredRequest(FredOperation.SERIES_VINTAGE_DATES, series_id=SERIES_ID,
                    limit=10, offset=0),
    )


R4_MANIFEST_DIGEST = "4c0d0c3f78382540dbfc7e5a8af21c1ceadeafb77978c85c49bef9588dbd40f6"
