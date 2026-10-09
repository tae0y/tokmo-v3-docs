"""Deterministic reverse-index recall over new material deltas.

New Facts/Programs material (CPI, FOMC, disclosures, supply/demand, quote
state…) is mapped to closed index keys, matched against durable hypothesis
index rows, and recorded as a would-have-sent shadow. This phase creates
deterministic recall candidates only — no alert, no promotion, no order
semantics, and no delivery path exists in this module.
"""

import hashlib
from dataclasses import dataclass

from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import (
    HypothesisIndexKeyV1,
    HypothesisState,
    IndexKeyKind,
    ProducerRole,
    RecallCandidateV1,
    ReplayResult,
    ShadowRecallRecordV1,
)

# Long subject/symbol identities are bounded deterministically: the key keeps
# a readable head and a full-content hash tail, so index construction can
# never exceed the 128-char key bound and never throws. The separator is
# ".", which is legal under the closed token charset [A-Za-z0-9_.:-].
_MAX_KEY_IDENTITY = 96


def _bounded_identity(raw: str) -> str:
    if len(raw) <= _MAX_KEY_IDENTITY:
        return raw
    tail = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    head = raw[: _MAX_KEY_IDENTITY - 17]
    return f"{head}.{tail}"


def index_keys_from_delta(delta) -> tuple:
    """Closed, deterministic key set for one MaterialDeltaV1."""
    target = _bounded_identity(
        delta.symbol if delta.symbol else delta.subject_id,
    )
    first_evidence = _bounded_identity(delta.evidence_refs[0].evidence_id)
    return (
        HypothesisIndexKeyV1(
            IndexKeyKind.METRIC,
            f"metric:{delta.material_kind.value}:{target}",
        ),
        HypothesisIndexKeyV1(IndexKeyKind.ENTITY, f"entity:{target}"),
        HypothesisIndexKeyV1(
            IndexKeyKind.EFFECT,
            f"effect:{delta.change_basis.value}",
        ),
        HypothesisIndexKeyV1(
            IndexKeyKind.SOURCE_EVENT,
            f"source-event:{first_evidence}",
        ),
    )


def recall_for_deltas(store, deltas, *, now) -> tuple:
    """Read-only recall: match open hypotheses by index keys."""
    recalls = []
    for delta in deltas:
        keys = index_keys_from_delta(delta)
        matched_by_hypothesis = {}
        for key in keys:
            for hypothesis_id in store.read_hypotheses_by_index(
                key.kind,
                key.value,
            ):
                matched_by_hypothesis.setdefault(hypothesis_id, set()).add(key)
        for hypothesis_id in sorted(matched_by_hypothesis):
            row = store.read_hypothesis(hypothesis_id)
            if row.state not in (HypothesisState.CANDIDATE, HypothesisState.WATCHING):
                continue
            matched = sorted(
                matched_by_hypothesis[hypothesis_id],
                key=lambda key: (key.kind.value, key.value),
            )
            # match_kind computed BEFORE the frozen construction — no
            # post-hoc mutation of the frozen dataclass.
            match_kind = "EXACT" if len(matched) == len(keys) else "PARTIAL"
            recalls.append(
                RecallCandidateV1(
                    hypothesis_id=hypothesis_id,
                    statement=row.statement,
                    state=row.state,
                    matched_keys=tuple(matched),
                    match_kind=match_kind,
                    material_delta_ids=(delta.delta_id,),
                )
            )
    return tuple(recalls)


@dataclass(frozen=True)
class ShadowOutcomeV1:
    new_records: int
    duplicates: int


def _shadow_payload(recall) -> str:
    """Redaction-checked payload: only already-validated token fields."""
    payload = canonical_encode(
        {
            "v": 1,
            "hypothesis_id": recall.hypothesis_id,
            "statement": recall.statement,
            "state": recall.state.value,
            "match_kind": recall.match_kind,
            "matched_keys": [
                [key.kind.value, key.value] for key in recall.matched_keys
            ],
            "material_delta_ids": list(recall.material_delta_ids),
            "would_have_sent": False,
        }
    ).decode("utf-8")
    lowered = payload.lower()
    for marker in ("/users/", "tokmo", ".local", "/hermes"):
        if marker in lowered:
            raise ValueError("shadow payload contains private marker")
    return payload


def record_would_have_sent_shadow(store, recalls, deltas, *, now) -> ShadowOutcomeV1:
    """Idempotent would-have-sent writes; replay is a duplicate, not a send."""
    new_records = duplicates = 0
    for recall in recalls:
        record = ShadowRecallRecordV1(
            hypothesis_id=recall.hypothesis_id,
            material_delta_ids=recall.material_delta_ids,
            matched_keys=recall.matched_keys,
            match_kind=recall.match_kind,
            would_have_sent=False,
            payload_json=_shadow_payload(recall),
            recorded_at=now,
        )
        result = store.record_shadow_recall(ProducerRole.ANALYSIS, record)
        if result is ReplayResult.CREATED:
            new_records += 1
        else:
            duplicates += 1
    return ShadowOutcomeV1(
        new_records=new_records,
        duplicates=duplicates,
    )
