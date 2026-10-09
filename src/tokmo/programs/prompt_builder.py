"""Typed prompt assembly into one canonical JSON document."""

from dataclasses import dataclass

from tokmo.values.analysis_contracts import (
    MODEL_REQUEST_MAX_BYTES,
    CandidateDataSegmentV1,
    UntrustedTextV1,
)
from tokmo.values.material_pack import canonical_encode

GLM_ANALYSIS_V1 = "glm-analysis-v1"
SOL_ANALYSIS_V1 = "sol-analysis-v1"
INSTRUCTION_IDS_V1 = frozenset({GLM_ANALYSIS_V1, SOL_ANALYSIS_V1})


@dataclass(frozen=True)
class InstructionSegmentV1:
    instruction_id: str

    def __post_init__(self):
        if self.instruction_id not in INSTRUCTION_IDS_V1:
            raise ValueError("unregistered instruction")


@dataclass(frozen=True)
class PromptV1:
    segments: tuple[
        InstructionSegmentV1 | UntrustedTextV1 | CandidateDataSegmentV1, ...
    ]

    def __post_init__(self):
        if type(self.segments) is not tuple or any(
            not isinstance(
                item, (InstructionSegmentV1, UntrustedTextV1, CandidateDataSegmentV1)
            )
            for item in self.segments
        ):
            raise ValueError("invalid prompt segments")


def render_candidate_segment(segment: CandidateDataSegmentV1) -> bytes:
    return canonical_encode(
        {"candidate_id": segment.candidate_id, "payload": segment.payload}
    )


def build_prompt_body(prompt: PromptV1) -> bytes:
    instructions = []
    untrusted = []
    candidates = []
    for segment in prompt.segments:
        if isinstance(segment, InstructionSegmentV1):
            instructions.append(segment.instruction_id)
        elif isinstance(segment, UntrustedTextV1):
            untrusted.append(
                {
                    "provenance": segment.provenance,
                    "text": segment.text,
                    "trust_class": segment.trust_class.value,
                    "instruction_authority": segment.instruction_authority,
                }
            )
        else:
            candidates.append(
                {
                    "candidate_id": segment.candidate_id,
                    "payload": segment.payload,
                }
            )
    body = canonical_encode(
        {
            "schema_version": 1,
            "instructions": instructions,
            "untrusted": untrusted,
            "candidates": candidates,
        }
    )
    if len(body) > MODEL_REQUEST_MAX_BYTES:
        raise ValueError("prompt oversize")
    return body
