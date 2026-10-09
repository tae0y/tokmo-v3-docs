from tokmo.programs.personalized_event_analysis import analyze_event
from tokmo.values.personalized_event import PortfolioContext, ScenarioNode


def test_dynamic_scenario_shape_and_action_condition_not_ordinal():
    scenarios = (
        ScenarioNode("action-now", "MET", material_action_condition=True),
        ScenarioNode("watch", "OPEN"),
        ScenarioNode("branch-a", "OPEN", parent_id="watch"),
        ScenarioNode("branch-b", "OPEN", parent_id="watch"),
    )
    result = analyze_event(
        episode_id="nep-1",
        entities=("SK하이닉스",),
        scenarios=scenarios,
        context=PortfolioContext(
            holdings=("SK하이닉스",), playbook_subjects=(), thematic_budget=False
        ),
        prior_reaction=None,
    )
    assert result.materiality == "DIRECT_HOLDING"
    assert result.action_draft is not None
    assert len(result.scenarios) == 4


def test_nonholding_theme_is_relevant_but_weak_sector_alone_is_not():
    h200 = analyze_event(
        episode_id="nep-2",
        entities=("H200",),
        scenarios=(),
        context=PortfolioContext(
            holdings=(), playbook_subjects=(), thematic_budget=True
        ),
        prior_reaction="NOT_HELPFUL",
    )
    weak = analyze_event(
        episode_id="nep-3",
        entities=("AI",),
        scenarios=(),
        context=PortfolioContext(
            holdings=(), playbook_subjects=(), thematic_budget=False
        ),
        prior_reaction=None,
    )
    assert h200.materiality == "THEMATIC_CAPACITY"
    assert h200.acknowledges_prior_reaction is True
    assert weak.materiality == "WEAK"


def test_action_draft_when_met_node_is_last_ordinal():
    scenarios = (
        ScenarioNode("watch-1", "PENDING"),
        ScenarioNode("watch-2", "ACTIVE"),
        ScenarioNode("last-action", "MET", material_action_condition=True),
    )
    result = analyze_event(
        episode_id="nep-last",
        entities=("SK하이닉스",),
        scenarios=scenarios,
        context=PortfolioContext(("SK하이닉스",), (), False),
        prior_reaction=None,
    )
    assert result.action_draft and scenarios[-1].material_action_condition
