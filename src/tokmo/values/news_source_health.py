"""Closed vocabularies, bounds, and templates for NEWS source health."""

from dataclasses import dataclass
from enum import StrEnum


class SourceHealthState(StrEnum):
    HEALTHY = "HEALTHY"
    DEGRADED = "DEGRADED"


class NotificationKind(StrEnum):
    DEGRADED = "DEGRADED"
    FINGERPRINT_CHANGED = "FINGERPRINT_CHANGED"
    REMINDER = "REMINDER"
    RECOVERED = "RECOVERED"


class NotificationStatus(StrEnum):
    PENDING = "PENDING"
    ATTEMPTING = "ATTEMPTING"
    SENT = "SENT"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"
    SUPERSEDED = "SUPERSEDED"


DEGRADE_CONFIRM_TICKS = 2
RECOVER_CONFIRM_TICKS = 2
REMINDER_SLOT_HOURS_KST = (8, 20)
REMINDER_MIN_GAP_SECONDS = 7200
ATTEMPTING_STALE_SECONDS = 600
MAX_ATTEMPTS_PER_NOTIFICATION = 8
MAX_DEGRADED_ROWS_PER_INCIDENT = 3
HEALTH_TZ_KEY = "Asia/Seoul"

DELIVERY_FAILURE_REASONS = frozenset(
    {
        "provider_timeout",
        "provider_failure",
        "attempt_stale_crash",
    }
)

# Static sanitized coverage-impact lines keyed by CLI source slug. The
# finnhub-company wording reflects the 2-calendar-day request window that
# self-heals on recovery; rotating feeds may lose depth-rotated items.
COVERAGE_IMPACT = {
    "finnhub-company": "미국 종목 회사 뉴스 지연 — 복구 시 최근 2일 창 자동 재수집",
    "finnhub-market": "미국 시장 뉴스 지연 — 회전 피드 특성상 일부 항목 유실 가능",
    "naver": "국내 종목 뉴스 지연 — 복구 후 다음 tick부터 정상 수집",
    "bbc": "해외 일반 뉴스 지연 — 피드 회전으로 일부 항목 유실 가능",
    "yna-market": "국내 증시 뉴스 지연 — 피드 회전으로 일부 항목 유실 가능",
    "yna-industry": "국내 산업 뉴스 지연 — 피드 회전으로 일부 항목 유실 가능",
    "jtbc-breaking": "속보 뉴스 지연 — 피드 회전으로 일부 항목 유실 가능",
    "investing-analyst": "분석 뉴스 지연 — 피드 회전으로 일부 항목 유실 가능",
}
DEFAULT_COVERAGE_IMPACT = "뉴스 수집 지연 — 복구 후 다음 tick부터 정상 수집"


@dataclass(frozen=True)
class SourceTickObservation:
    tick_id: str
    source: str
    observed_at: str
    outcome: str
    failure_pairs: tuple[tuple[str, str | None], ...]
    targets_total: int
    targets_failed: int
    diagnostic_summary: str = ""


@dataclass(frozen=True)
class HealthDecision:
    source: str
    outcome: str
    state: str
    minted: tuple[str, ...]


@dataclass(frozen=True)
class DeliveryOutcome:
    attempted: int
    sent: int
    failed: int
    unknown: int
