"""Personalized event value contracts without account identifiers."""
from dataclasses import dataclass


@dataclass(frozen=True)
class PortfolioContext:
    holdings: tuple[str, ...]
    playbook_subjects: tuple[str, ...]
    thematic_budget: bool


@dataclass(frozen=True)
class ScenarioNode:
    node_id: str
    state: str
    material_action_condition: bool = False
    parent_id: str | None = None


@dataclass(frozen=True)
class PersonalizedEvent:
    episode_id: str
    materiality: str
    scenarios: tuple[ScenarioNode, ...]
    action_draft: str | None
    acknowledges_prior_reaction: bool
