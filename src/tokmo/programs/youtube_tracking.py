"""Deterministic TRACKING_CANDIDATE adapter for the YouTube 12man report.

The report body, transcript, and metadata are UNTRUSTED data. This module
carries no network, file-write, or delivery capability: it only parses a
closed block grammar into typed values and composes existing ops-kernel
admission calls. Any instruction-shaped text inside the untrusted input is
kept as inert data or rejected by shape — never interpreted or executed.

M2 quarantine: a malformed or THESIS_UNARTICULATED block never aborts its
siblings. ``parse_report_tracking_blocks`` isolates each block, returns the
valid candidates, and returns the malformed ones as typed
``CandidateFailureV1`` records the caller persists for the later user
question. The system never articulates an unarticulated thesis.
"""

from dataclasses import dataclass
from datetime import datetime
import hashlib
import re

from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import (
    AdmissionDecisionV1,
    CandidateFailureV1,
    HypothesisIndexKeyV1,
    IndexKeyKind,
    ProducerRole,
    ReplayResult,
    SemanticClassification,
    SemanticProfileV1,
    TrackingCandidateV1,
    TriggerKind,
    hypothesis_identity_digest,
)
from tokmo.values.ops_kernel import HypothesisState


BLOCK_BEGIN = "=== TRACKING_CANDIDATE BEGIN ==="
BLOCK_END = "=== TRACKING_CANDIDATE END ==="

_TRIGGERS = {kind.value: kind for kind in TriggerKind}
_TOUCHES = {"PERSONAL", "PENSION", "PORTFOLIO_ALL", "NONE"}
_KEY_KINDS = {
    "metric": IndexKeyKind.METRIC,
    "entity": IndexKeyKind.ENTITY,
    "effect": IndexKeyKind.EFFECT,
    "source-event": IndexKeyKind.SOURCE_EVENT,
}
_UNARTICULATED = "THESIS_UNARTICULATED"
_MAX_STATEMENT = 300
# Redaction aligned with values/ops_kernel.py: private-path/identity markers
# block actual secrets and absolute host paths; the ordinary word 'secret'
# alone is legitimate vocabulary and NOT blocked. Key-shaped tokens are
# blocked by shape.
_FORBIDDEN_STATEMENT_MARKERS = (
    "/users/", "~/.local", "tokmo", "/hermes",
)
_SECRET_SHAPES = (
    re.compile(r"\bsk-[A-Za-z0-9_-]{16,}\b"),
    re.compile(r"\bghp_[A-Za-z0-9]{16,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{16,}\b"),
)
# Token-ish evidence ids: no spaces, bounded length (matches values rules).
_EVIDENCE_TOKEN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:\-]{0,127}$")


class TrackingCandidateParseError(ValueError):
    """A TRACKING_CANDIDATE block violated the closed admission grammar."""


@dataclass(frozen=True)
class ArchiveProvenanceV1:
    source_ref: str
    revision: str
    payload_digest: str


@dataclass(frozen=True)
class ParsedReportV1:
    """Per-block isolation result (M2): survivors + quarantined failures."""

    candidates: tuple
    failures: tuple


def _clean_statement(raw: str) -> str:
    if type(raw) is not str:
        raise TrackingCandidateParseError("STATEMENT must be text")
    stripped = raw.strip()
    if not stripped or len(stripped) > _MAX_STATEMENT:
        raise TrackingCandidateParseError("STATEMENT length out of bounds")
    if stripped == _UNARTICULATED:
        # User intent gap: the system never articulates it automatically.
        raise TrackingCandidateParseError(
            "STATEMENT THESIS_UNARTICULATED is not admissible",
        )
    lowered = stripped.lower()
    for marker in _FORBIDDEN_STATEMENT_MARKERS:
        if marker in lowered:
            raise TrackingCandidateParseError(
                "STATEMENT contains forbidden marker",
            )
    for shape in _SECRET_SHAPES:
        if shape.search(stripped):
            raise TrackingCandidateParseError(
                "STATEMENT contains secret-shaped token",
            )
    return stripped


def _split_tokens(raw: str, name: str, *, minimum: int) -> tuple:
    if type(raw) is not str:
        raise TrackingCandidateParseError(f"{name} must be text")
    parts = [item.strip() for item in raw.split(";") if item.strip()]
    if len(parts) < minimum or len(parts) > 8:
        raise TrackingCandidateParseError(f"{name} count out of bounds")
    for item in parts:
        if not _EVIDENCE_TOKEN.fullmatch(item):
            raise TrackingCandidateParseError(f"{name} contains invalid token")
    return tuple(parts)


def _parse_keys(raw: str) -> tuple:
    if type(raw) is not str:
        raise TrackingCandidateParseError("KEYS must be text")
    parts = [item.strip() for item in raw.split(",") if item.strip()]
    if not 1 <= len(parts) <= 8:
        raise TrackingCandidateParseError("KEYS count out of bounds")
    keys = []
    for item in parts:
        matched = False
        for prefix, kind in _KEY_KINDS.items():
            head = prefix + ":"
            if item.startswith(head) and len(item) > len(head):
                keys.append(HypothesisIndexKeyV1(kind, item))
                matched = True
                break
        if not matched:
            raise TrackingCandidateParseError(f"KEYS entry not recognized: {item}")
    return tuple(keys)


def _parse_next_check(raw: str):
    stripped = raw.strip()
    if stripped == "none":
        return None
    try:
        parsed = datetime.fromisoformat(stripped.replace("Z", "+00:00"))
    except ValueError:
        raise TrackingCandidateParseError("NEXT_CHECK not ISO8601 or none")
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise TrackingCandidateParseError("NEXT_CHECK must be timezone-aware")
    return parsed


def _block_digest(block_text: str) -> str:
    return hashlib.sha256(
        canonical_encode({
            "v": 1,
            "domain": "tokmo-v3:youtube-tracking-block:v1",
            "block": block_text,
        }),
    ).hexdigest()


def _parse_one_block(block_text, *, source_ref, revision, observed_at):
    """Parse one block or raise; caller isolates the failure per block."""
    fields = {}
    for line in block_text.splitlines():
        if not line.strip():
            continue
        key, separator, value = line.partition(":")
        if not separator:
            raise TrackingCandidateParseError("candidate line is not KEY: value")
        key = key.strip()
        if key in fields:
            raise TrackingCandidateParseError(f"duplicate field {key}")
        fields[key] = value.strip()
    required = ("STATEMENT", "TRIGGER", "SUPPORT", "INVALIDATE",
                "NEXT_CHECK", "TOUCH", "KEYS")
    missing = [name for name in required if name not in fields]
    if missing:
        raise TrackingCandidateParseError(
            f"missing required fields: {','.join(missing)}",
        )
    statement = _clean_statement(fields["STATEMENT"])
    trigger_parts = fields["TRIGGER"].split("|", 1)
    if len(trigger_parts) != 2:
        raise TrackingCandidateParseError("TRIGGER needs kind | expression")
    trigger_name = trigger_parts[0].strip()
    if trigger_name not in _TRIGGERS:
        raise TrackingCandidateParseError("TRIGGER kind not recognized")
    expression = trigger_parts[1].strip()
    if not expression or len(expression) > 200:
        raise TrackingCandidateParseError("TRIGGER expression out of bounds")
    lowered = expression.lower()
    if "telegram" in lowered or "ignore previous" in lowered:
        raise TrackingCandidateParseError(
            "TRIGGER expression contains instruction-shaped text",
        )
    touch = fields["TOUCH"].strip()
    if touch not in _TOUCHES:
        raise TrackingCandidateParseError("TOUCH not recognized")
    support = _split_tokens(fields.get("SUPPORT", ""), "SUPPORT", minimum=1)
    counter = _split_tokens(fields.get("COUNTER", ""), "COUNTER", minimum=0)
    invalidate = _split_tokens(
        fields["INVALIDATE"], "INVALIDATE", minimum=1,
    )
    keys = _parse_keys(fields["KEYS"])
    return TrackingCandidateV1(
        statement=statement,
        trigger_kind=_TRIGGERS[trigger_name],
        trigger_expression=expression,
        supporting_evidence=support,
        counter_evidence=counter,
        invalidation_conditions=invalidate,
        next_check_at=_parse_next_check(fields["NEXT_CHECK"]),
        portfolio_touch=touch,
        index_keys=keys,
        source_ref=source_ref,
        revision=revision,
        payload_digest=_block_digest(block_text),
        observed_at=observed_at,
    )


def _failure_kind(message: str) -> str:
    lowered = message.lower()
    if "missing required fields" in lowered:
        return "missing_fields"
    if "unarticulated" in lowered:
        return "unarticulated_statement"
    if "duplicate field" in lowered:
        return "duplicate_field"
    if "statement" in lowered:
        return "bad_statement"
    if "trigger" in lowered:
        return "bad_trigger"
    if "keys" in lowered:
        return "bad_keys"
    return "bad_field"


def parse_report_tracking_blocks(report_text, *, source_ref, revision,
                                 observed_at) -> ParsedReportV1:
    """Parse every block with per-block isolation (M2).

    Valid blocks become candidates; malformed blocks become typed failures
    quarantined for the later user question. Identical duplicate blocks
    collapse to one candidate. An unterminated block is a failure of the
    whole remaining report tail (nothing after it can be trusted).
    """
    if type(report_text) is not str:
        raise TrackingCandidateParseError("report must be text")
    candidates = []
    failures = []
    seen_blocks = set()
    position = 0
    while True:
        begin = report_text.find(BLOCK_BEGIN, position)
        if begin < 0:
            break
        end = report_text.find(BLOCK_END, begin)
        if end < 0:
            failures.append(CandidateFailureV1(
                source_ref=source_ref, revision=revision,
                error_kind="unterminated_block",
                block_digest=_block_digest(report_text[begin:]),
                recorded_at=observed_at,
            ))
            break
        block_text = report_text[begin + len(BLOCK_BEGIN):end].strip("\n")
        position = end + len(BLOCK_END)
        try:
            candidate = _parse_one_block(
                block_text, source_ref=source_ref, revision=revision,
                observed_at=observed_at,
            )
        except TrackingCandidateParseError as error:
            failures.append(CandidateFailureV1(
                source_ref=source_ref, revision=revision,
                error_kind=_failure_kind(str(error)),
                block_digest=_block_digest(block_text),
                recorded_at=observed_at,
            ))
            continue
        block_key = (
            candidate.statement, candidate.trigger_kind,
            candidate.trigger_expression, candidate.index_keys,
            candidate.portfolio_touch,
        )
        if block_key in seen_blocks:
            continue  # identical repeated block: one candidate
        seen_blocks.add(block_key)
        candidates.append(candidate)
    return ParsedReportV1(candidates=tuple(candidates), failures=tuple(failures))


def parse_tracking_candidates(report_text, *, source_ref, revision,
                              observed_at) -> tuple:
    """Strict whole-report parse: any malformed block fails loudly.

    Kept for callers that want all-or-nothing semantics; the isolated
    admission path is ``parse_report_tracking_blocks``.
    """
    parsed = parse_report_tracking_blocks(
        report_text, source_ref=source_ref, revision=revision,
        observed_at=observed_at,
    )
    if parsed.failures:
        raise TrackingCandidateParseError(
            f"{len(parsed.failures)} malformed candidate block(s)",
        )
    return parsed.candidates


def provenance_from_archive_meta(meta) -> ArchiveProvenanceV1:
    """Deterministic provenance from one archived transcript meta mapping."""
    if not isinstance(meta, dict):
        raise TrackingCandidateParseError("archive meta must be a mapping")
    video_id = meta.get("video_id")
    published = meta.get("published")
    digest = meta.get("transcript_sha256")
    if not isinstance(video_id, str) or not video_id:
        raise TrackingCandidateParseError("archive meta missing video_id")
    if not isinstance(published, str) or not published:
        raise TrackingCandidateParseError("archive meta missing published")
    if not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise TrackingCandidateParseError(
            "archive meta missing transcript_sha256",
        )
    return ArchiveProvenanceV1(
        source_ref=video_id,
        revision=f"{published}:{digest[:12]}",
        payload_digest=digest,
    )


@dataclass(frozen=True)
class AdmitOutcomeV1:
    created: int
    duplicates: int
    revisions: int
    failures: int
    semantic_sightings: int = 0
    related_edges: int = 0
    merge_reviews: int = 0


def _candidate_audit_digest(candidate) -> str:
    """Deterministic audit digest of the admitted candidate content."""
    import hashlib
    return hashlib.sha256(canonical_encode({
        "v": 1,
        "domain": "tokmo-v3:youtube-admission-candidate:v1",
        "statement": candidate.statement,
        "trigger_kind": candidate.trigger_kind.value,
        "trigger_expression": candidate.trigger_expression,
        "invalidation": list(candidate.invalidation_conditions),
        "touch": candidate.portfolio_touch,
        "keys": sorted(f"{key.kind.value}:{key.value}"
                       for key in candidate.index_keys),
    })).hexdigest()


def _recall_open_profiles(store, candidate):
    """Recall open hypotheses by index-key overlap (read-only)."""
    seen = {}
    for key in candidate.index_keys:
        for hypothesis_id in store.read_hypotheses_by_index(key.kind, key.value):
            seen.setdefault(hypothesis_id, None)
    profiles = {}
    for hypothesis_id in sorted(seen):
        row = store.read_hypothesis(hypothesis_id)
        if row.state not in (HypothesisState.CANDIDATE,
                             HypothesisState.WATCHING):
            continue
        index_keys = store.read_hypothesis_index(hypothesis_id)
        profiles[hypothesis_id] = SemanticProfileV1.from_hypothesis(
            row, index_keys=index_keys,
        )
    return profiles


def _semantic_pre_admit(store, candidate, *, now):
    """Deterministic conservative semantic comparison before creation.

    Returns ``(outcome, hypothesis_id)`` where outcome is one of
    ``exact_replay``, ``semantic_sighting``, ``proceed``. On
    ``semantic_sighting`` the caller must NOT create a second hypothesis;
    the sighting is recorded against the kept hypothesis.
    """
    from tokmo.programs.youtube_semantic import classify_semantic_profiles

    profile = SemanticProfileV1.from_candidate(candidate)
    canonical = hypothesis_identity_digest(
        candidate.statement, candidate.trigger_kind,
        candidate.trigger_expression,
    )
    recalled = _recall_open_profiles(store, candidate)
    # Fable M1 repair: a FULL canonical-identity pass runs before any
    # semantic SAME/RELATED/MERGE scan. With two structural twins in the
    # store, an exact replay of the later twin must hit its own canonical
    # identity — never be misattributed as a new sighting on the earlier
    # twin just because the scan reached it first.
    for hypothesis_id in sorted(recalled):
        row = store.read_hypothesis(hypothesis_id)
        other_canonical = hypothesis_identity_digest(
            row.statement, row.trigger_kind, row.trigger_expression,
        )
        if canonical == other_canonical:
            decision = classify_semantic_profiles(
                profile, recalled[hypothesis_id],
                same_canonical_identity=True,
            )
            return "exact_replay", hypothesis_id, decision
    for hypothesis_id in sorted(recalled):
        decision = classify_semantic_profiles(
            profile, recalled[hypothesis_id],
            same_canonical_identity=False,
        )
        if decision.classification is (
            SemanticClassification.SAME_HYPOTHESIS_NEW_SIGHTING
        ):
            return "semantic_sighting", hypothesis_id, decision
    return "proceed", None, None


def _record_semantic_sighting(store, candidate, hypothesis_id, *, now):
    """Append the candidate's observation as a new sighting on the kept
    hypothesis. The sighting keeps the candidate's own source provenance;
    nothing is fabricated beyond what the report itself states."""
    from tokmo.values.ops_kernel import HypothesisSightingV1

    # Fable minor 3: the sighting keeps the candidate's own supporting
    # evidence — the report's evidence is real observed material, never an
    # empty delta that silently discards what the source stated.
    sighting = HypothesisSightingV1(
        hypothesis_id=hypothesis_id,
        source_kind="REPORT",
        source_ref=candidate.source_ref,
        revision=candidate.revision,
        payload_digest=candidate.payload_digest,
        evidence_delta=tuple(candidate.supporting_evidence),
        observed_at=candidate.observed_at,
    )
    return store.record_sighting(ProducerRole.ANALYSIS, sighting, now=now)


def admit_report_candidates(store, candidates, *, now,
                             failures=()) -> AdmitOutcomeV1:
    """Admit parsed candidates through the durable kernel seam (idempotent).

    M1/M2 isolation: each candidate is admitted independently; one failure
    never kills sibling candidates. Accepts either a candidate tuple or a
    ``ParsedReportV1`` (whose quarantined failures are persisted durably
    and idempotently for the later user question).

    Semantic pre-admission (2026-08-16 wiring): before creating a new
    hypothesis, the candidate's structured profile is compared with open
    hypotheses recalled by index overlap. Complete structural equality with
    a different source becomes a new sighting on the kept hypothesis — no
    second hypothesis is created. Material conflicts and ambiguous high
    overlap preserve the new hypothesis and create, respectively, a
    durable RELATED_BUT_DISTINCT edge or an OPEN MERGE_REVIEW queue item.
    The existing exact identity/replay behavior is unchanged; merging stays
    manual-only.
    """
    from tokmo.programs.youtube_semantic import classify_semantic_profiles
    from tokmo.values.ops_kernel import HypothesisState

    parsed = getattr(candidates, "candidates", None)
    if parsed is not None:  # ParsedReportV1 passed directly
        failures = getattr(candidates, "failures", failures)
        candidates = parsed
    created = duplicates = revisions = failure_count = 0
    semantic_sightings = related_edges = merge_reviews = 0
    for candidate in candidates:
        try:
            outcome, matched_id, decision = _semantic_pre_admit(
                store, candidate, now=now,
            )
            if outcome == "semantic_sighting":
                sighting_result = _record_semantic_sighting(
                    store, candidate, matched_id, now=now,
                )
                store.record_admission_decision(ProducerRole.ANALYSIS, (
                    AdmissionDecisionV1(
                        candidate_digest=_candidate_audit_digest(candidate),
                        hypothesis_id=matched_id,
                        matched_hypothesis_id=matched_id,
                        classification=decision.classification.value,
                        reason_code=decision.reason_code,
                        conflicts=decision.conflicts,
                        decided_at=now,
                    )
                ))
                if sighting_result is ReplayResult.CREATED:
                    semantic_sightings += 1
                else:
                    duplicates += 1
                continue
            exact = outcome == "exact_replay"
            if exact:
                store.record_admission_decision(ProducerRole.ANALYSIS, (
                    AdmissionDecisionV1(
                        candidate_digest=_candidate_audit_digest(candidate),
                        hypothesis_id=matched_id,
                        matched_hypothesis_id=matched_id,
                        classification=decision.classification.value,
                        reason_code=decision.reason_code,
                        conflicts=decision.conflicts,
                        decided_at=now,
                    )
                ))
            result, hypothesis_id = store.upsert_hypothesis(
                ProducerRole.ANALYSIS, candidate, now=now,
            )
        except Exception:
            failure_count += 1
            continue
        if exact and result is ReplayResult.DUPLICATE:
            duplicates += 1
            continue  # replay: no new pair adjudication
        if result is ReplayResult.CREATED:
            sightings = len(store.read_hypothesis_sightings(hypothesis_id))
            if sightings > 1:
                revisions += 1
            else:
                created += 1
        else:
            duplicates += 1
        # post-creation conservative relations for distinct/ambiguous pairs
        try:
            profile = SemanticProfileV1.from_candidate(candidate)
            recalled = _recall_open_profiles(store, candidate)
            compared = 0
            for other_id, other in recalled.items():
                if other_id == hypothesis_id:
                    continue
                compared += 1
                pair = classify_semantic_profiles(profile, other)
                store.record_admission_decision(ProducerRole.ANALYSIS, (
                    AdmissionDecisionV1(
                        candidate_digest=_candidate_audit_digest(candidate),
                        hypothesis_id=hypothesis_id,
                        matched_hypothesis_id=other_id,
                        classification=pair.classification.value,
                        reason_code=pair.reason_code,
                        conflicts=pair.conflicts,
                        decided_at=now,
                    )
                ))
                if pair.classification is (
                    SemanticClassification.RELATED_BUT_DISTINCT
                ):
                    if store.record_hypothesis_relation(
                        ProducerRole.ANALYSIS, hypothesis_id, other_id,
                        pair, now=now,
                    ) == "created":
                        related_edges += 1
                elif pair.classification is SemanticClassification.MERGE_REVIEW:
                    if store.queue_merge_review(
                        ProducerRole.ANALYSIS, hypothesis_id, other_id,
                        pair, now=now,
                    ) == "created":
                        merge_reviews += 1
            if compared == 0:
                # no open hypothesis shared an index key: normal creation
                # with no relation — still auditable.
                store.record_admission_decision(ProducerRole.ANALYSIS, (
                    AdmissionDecisionV1(
                        candidate_digest=_candidate_audit_digest(candidate),
                        hypothesis_id=hypothesis_id,
                        matched_hypothesis_id=None,
                        classification="NO_RELATION",
                        reason_code="no_overlap",
                        conflicts=(),
                        decided_at=now,
                    )
                ))
        except Exception:
            failure_count += 1
    for failure in failures:
        store.record_candidate_failure(ProducerRole.ANALYSIS, failure, now=now)
    return AdmitOutcomeV1(
        created=created, duplicates=duplicates,
        revisions=revisions, failures=failure_count,
        semantic_sightings=semantic_sightings,
        related_edges=related_edges, merge_reviews=merge_reviews,
    )
