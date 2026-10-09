"""Local-only portfolio linkage and scenario admission."""

from tokmo.values.personalized_event import PersonalizedEvent


def analyze_event(*, episode_id, entities, scenarios, context, prior_reaction):
    entity_set = {entity.casefold() for entity in entities}
    direct = entity_set & {
        item.casefold() for item in context.holdings + context.playbook_subjects
    }
    if direct:
        materiality = "DIRECT_HOLDING"
    elif context.thematic_budget and entity_set - {"ai"}:
        materiality = "THEMATIC_CAPACITY"
    else:
        materiality = "WEAK"
    action = None
    if any(
        node.material_action_condition and node.state == "MET" for node in scenarios
    ):
        action = "사용자 확인 전용 행동 초안; 자동 주문·원장·플레이북 변경 없음"
    return PersonalizedEvent(
        episode_id,
        materiality,
        tuple(scenarios),
        action,
        getattr(prior_reaction, "value", prior_reaction) == "NOT_HELPFUL",
    )
