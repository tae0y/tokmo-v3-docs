"""Deterministic, conservative semantic dedupe decision engine.

The engine compares two bounded ``SemanticProfileV1`` values and returns a
typed ``SemanticDecisionV1``. It is pure: no network, no model, no store. A
later bounded classifier may only *escalate* a NO_RELATION verdict into
MERGE_REVIEW through ``ClassifierHintV1`` — it can never override the
deterministic material-conflict guard, and can never turn any verdict into
an automatic merge or a fabricated sighting.

Decision order (deterministic):
1. canonical identity replay flag -> EXACT_DUPLICATE;
2. material conflict (trigger/invalidation/horizon/touch) -> RELATED_BUT_DISTINCT
   with reason ``material_conflict`` and the compared conflict fields — this
   guard is absolute: no hint overrides it;
3. complete structural equality (same trigger kind, normalized trigger,
   invalidation set, horizon bucket, touch, and identical key sets) ->
   SAME_HYPOTHESIS_NEW_SIGHTING;
4. key-set overlap >= 1/2 with no conflict -> MERGE_REVIEW (ambiguous high
   overlap is preserved, never destructively merged);
5. any lesser overlap without conflict -> RELATED_BUT_DISTINCT with reason
   ``partial_overlap_distinct``;
6. zero shared keys and no shared semantic core -> NO_RELATION; a classifier
   hint may escalate exactly this verdict to MERGE_REVIEW.
"""

from tokmo.values.ops_kernel import (
    ClassifierHintV1,
    SemanticClassification,
    SemanticDecisionV1,
    SemanticProfileV1,
    _SEMANTIC_HIGH_OVERLAP,
)


def _material_conflicts(left: SemanticProfileV1,
                        right: SemanticProfileV1) -> tuple:
    conflicts = []
    if (left.trigger_kind is not right.trigger_kind
            or left.trigger_norm != right.trigger_norm):
        conflicts.append("trigger")
    if left.invalidation != right.invalidation:
        conflicts.append("invalidation")
    if left.horizon != right.horizon:
        conflicts.append("horizon")
    if left.portfolio_touch != right.portfolio_touch:
        conflicts.append("touch")
    return tuple(conflicts)


def classify_semantic_profiles(left: SemanticProfileV1,
                               right: SemanticProfileV1, *,
                               same_canonical_identity: bool = False,
                               hint: ClassifierHintV1 | None = None) -> SemanticDecisionV1:
    if not isinstance(left, SemanticProfileV1) or not isinstance(
        right, SemanticProfileV1,
    ):
        raise ValueError("invalid semantic profile")
    if hint is not None and not isinstance(hint, ClassifierHintV1):
        raise ValueError("invalid classifier hint")
    overlap = left.key_overlap(right)
    # 1. exact canonical identity/replay keeps the existing exact behavior
    if same_canonical_identity:
        return SemanticDecisionV1(
            classification=SemanticClassification.EXACT_DUPLICATE,
            reason_code="canonical_identity_replay",
            conflicts=(),
            overlap=overlap,
        )
    # 2. deterministic material-conflict guard (never hint-overridable)
    conflicts = _material_conflicts(left, right)
    if conflicts:
        return SemanticDecisionV1(
            classification=SemanticClassification.RELATED_BUT_DISTINCT,
            reason_code="material_conflict",
            conflicts=conflicts,
            overlap=overlap,
        )
    # 3. complete structural equality -> same hypothesis, new sighting
    left_keys = left.entities | left.metrics | left.effects | left.source_events
    right_keys = (
        right.entities | right.metrics | right.effects | right.source_events
    )
    if left_keys == right_keys:
        return SemanticDecisionV1(
            classification=SemanticClassification.SAME_HYPOTHESIS_NEW_SIGHTING,
            reason_code="structural_equality_new_source",
            conflicts=(),
            overlap=overlap,
        )
    # 4-6. overlap bands; hint escalates only NO_RELATION
    if overlap >= _SEMANTIC_HIGH_OVERLAP:
        return SemanticDecisionV1(
            classification=SemanticClassification.MERGE_REVIEW,
            reason_code="high_overlap_ambiguous",
            conflicts=(),
            overlap=overlap,
        )
    if overlap > 0.0:
        return SemanticDecisionV1(
            classification=SemanticClassification.RELATED_BUT_DISTINCT,
            reason_code="partial_overlap_distinct",
            conflicts=(),
            overlap=overlap,
        )
    if hint is not None and hint.asserts_high_overlap:
        return SemanticDecisionV1(
            classification=SemanticClassification.MERGE_REVIEW,
            reason_code="classifier_overlap_hint",
            conflicts=(),
            overlap=overlap,
            hint_used=True,
        )
    return SemanticDecisionV1(
        classification=SemanticClassification.NO_RELATION,
        reason_code="no_overlap",
        conflicts=(),
        overlap=overlap,
    )
