"""Result types: unavailable fact, save status/result, decision record."""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from tokmo.values import DECISION_ACTIONS, UNAVAILABLE_REASONS

__all__ = ["DecisionRecord", "SaveResult", "SaveStatus", "UnavailableFact"]


@dataclass(frozen=True)
class UnavailableFact:
    source: str
    reason: str
    observed_at: datetime
    retry_after: int | None = None

    def __post_init__(self) -> None:
        if self.reason not in UNAVAILABLE_REASONS:
            raise ValueError(f"unavailable_reason_outside_contract:{self.reason}")


class SaveStatus(Enum):
    SAVED = "SAVED"
    DUPLICATE = "DUPLICATE"
    CONFLICT = "CONFLICT"
    FAILED = "FAILED"


@dataclass(frozen=True)
class SaveResult:
    status: SaveStatus
    record_id: str | None
    reason: str
    stored_at: datetime


@dataclass(frozen=True)
class DecisionRecord:
    decision_id: str
    run_id: str
    source_ref: str
    action: str
    reason: str
    observed_at: datetime
    decided_at: datetime
    policy_version: str

    def __post_init__(self) -> None:
        if self.action not in DECISION_ACTIONS:
            raise ValueError(f"decision_action_outside_contract:{self.action}")
