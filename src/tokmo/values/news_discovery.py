"""Deterministic, transport-free NEWS discovery values."""

import hashlib
import json
from dataclasses import dataclass
from enum import Enum
from pathlib import Path


class NewsDiscoveryLaneV1(str, Enum):
    WHOLE_CORPUS_FREQUENCY = "WHOLE_CORPUS_FREQUENCY"
    DIRECT_HOLDING = "DIRECT_HOLDING"
    DIRECT_PLAYBOOK = "DIRECT_PLAYBOOK"
    PRE_CATALYST = "PRE_CATALYST"
    EARLY_PROPAGATION = "EARLY_PROPAGATION"
    PRICE_VOLUME_BACKSTOP = "PRICE_VOLUME_BACKSTOP"
    CONCEPT_EMERGENCE = "CONCEPT_EMERGENCE"
    MARKET_STRUCTURE = "MARKET_STRUCTURE"
    MACRO_GEOPOLITICAL = "MACRO_GEOPOLITICAL"


@dataclass(frozen=True)
class NewsDocument:
    document_id: str
    title: str
    story_fingerprint: str
    source_id: str
    observed_at: str | None = None
    # Already-stored non-title evidence (n5 ``summary_text``): alias
    # discovery may read it; nothing is ever fetched to populate it.
    summary: str | None = None
    # Already-stored n5 provenance (SDS 2026-09-01 W2): the intake-time
    # FACT/OPINION role and publisher identity. Defaults keep every
    # pre-existing positional construction byte-compatible.
    evidence_role: str | None = None
    publisher_name: str | None = None


@dataclass(frozen=True)
class DiscoveryCandidate:
    key: str
    lane: str
    distinct_story_count: int
    document_ids: tuple[str, ...]
    distinct_source_count: int = 0
    phrase_text: str | None = None
    latest_observed_at: str | None = None


@dataclass(frozen=True)
class DiscoveryDecision:
    reason: str
    key: str | None
    phrase_text: str | None
    distinct_story_count: int
    distinct_source_count: int
    document_ids: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[DiscoveryCandidate, ...]
    syndicated_documents: int
    dropped_over_cap: int
    corpus_snapshot_digest: str = ""
    policy_digest: str = ""
    dropped_keys: tuple[str, ...] = ()
    dropped_candidates: tuple[DiscoveryCandidate, ...] = ()
    decisions: tuple[DiscoveryDecision, ...] = ()


@dataclass(frozen=True)
class NewsDiscoveryPolicyV1:
    minimum_stories: int = 3
    minimum_sources: int = 2
    candidate_cap: int = 12

    @property
    def policy_digest(self) -> str:
        raw = json.dumps(self.__dict__, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(raw).hexdigest()


def load_public_aliases(
    path: Path,
) -> tuple[dict[str, tuple[str, ...]], tuple[str, ...]]:
    value = json.loads(path.read_text(encoding="utf-8"))
    aliases = {key: tuple(items) for key, items in value["aliases"].items()}
    return aliases, tuple(value["suppressed"])
