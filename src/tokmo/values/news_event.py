"""Typed event episode values."""
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class EpisodeState(str, Enum):
    DISCOVERED = "DISCOVERED"
    INVESTIGATING = "INVESTIGATING"
    EARLY_ALERTED = "EARLY_ALERTED"
    ACTIVE = "ACTIVE"
    ESCALATED = "ESCALATED"
    DEESCALATED = "DEESCALATED"
    CONFLICTED = "CONFLICTED"
    DISCONFIRMED = "DISCONFIRMED"
    RESOLVED = "RESOLVED"
    MUTED = "MUTED"
    CLOSED = "CLOSED"


class NewsClaimStatusV1(str, Enum):
    CONFIRMED_FACT = "CONFIRMED_FACT"
    CLAIM = "CLAIM"
    INTERPRETATION = "INTERPRETATION"
    CAUSAL_HYPOTHESIS = "CAUSAL_HYPOTHESIS"
    RETRACTED = "RETRACTED"
    CORRECTED = "CORRECTED"


class NewsReactionV1(str, Enum):
    HELPFUL = "HELPFUL"
    NOT_HELPFUL = "NOT_HELPFUL"


class NewsEpisodeTransitionError(ValueError):
    pass


@dataclass(frozen=True)
class Evidence:
    evidence_id: str
    claim_status: str
    conflict_group: str | None = None


@dataclass(frozen=True)
class NewsEpisode:
    episode_id: str
    episode_key: str
    state: EpisodeState
    revision: int
    evidence: tuple[Evidence, ...]
    observed_at: datetime
