"""Deterministic NEWS taxonomy values (stages 1–6, 2026-08-22).

Typed registration vocabulary: kinds, lifecycle states, reason codes, the
bounded catalyst controlled vocabulary, and the provider-neutral pending
adjudication request. No provider, model, or transport identifiers may
ever appear here — stage 7 injects an adjudicator callable at composition.
"""

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass

TAXONOMY_KINDS_V1 = ("ENTITY_ALIAS", "TOPIC", "METRIC", "CATALYST", "EFFECT")

# instance -> candidate -> VERIFIED_AUTO | PENDING_MODEL is the automatic
# lifecycle; VERIFIED_MODEL/REJECTED_MODEL are reserved for the stage-7
# adjudicator and TOMBSTONED is the only deactivation (never deletion).
TAXONOMY_STATES_V1 = (
    "CANDIDATE",
    "VERIFIED_AUTO",
    "PENDING_MODEL",
    "VERIFIED_MODEL",
    "REJECTED_MODEL",
    "TOMBSTONED",
)

# Bounded controlled catalyst vocabulary: markers are matched as substrings
# of the NFKC-casefolded title. Unknown or ambiguous event types stay
# PENDING_MODEL — the vocabulary itself never grows automatically.
CATALYST_TYPES_V1: Mapping = {
    "EARNINGS_RELEASE": ("실적", "어닝", "영업이익", "earnings"),
    "GUIDANCE_CHANGE": ("가이던스", "guidance"),
    "MERGER_ACQUISITION": ("인수", "합병", "acquisition", "merger", "m&a"),
    "REGULATORY_ACTION": ("규제", "반독점", "antitrust", "regulation"),
    "LITIGATION": ("소송", "판결", "배상", "lawsuit"),
    "PRODUCT_LAUNCH": ("출시", "공개", "launch", "unveil"),
    "SUPPLY_DISRUPTION": ("파업", "리콜", "공급 차질", "shortage", "recall"),
    "RATE_DECISION": ("금리", "fomc", "rate cut", "rate hike"),
    "SANCTIONS_EXPORT_CONTROL": (
        "제재",
        "수출 통제",
        "수출통제",
        "sanction",
        "export control",
    ),
    "MANAGEMENT_CHANGE": ("사임", "선임", "경영진 교체", "resign"),
    "CAPACITY_INVESTMENT": ("증설", "설비투자", "공장 건설", "capex"),
    "SHAREHOLDER_RETURN": ("배당", "자사주", "buyback", "dividend"),
}

# Event-shaped language without a controlled type: catalyst candidate with
# unknown type (PENDING_MODEL), never a silent new type.
GENERIC_EVENT_MARKERS_V1 = ("발표", "결정", "체결", "공시", "announce")

# Observed (realized) market-move markers with deterministic direction.
OBSERVED_EFFECT_MARKERS_V1: Mapping = {
    "급등": "UP",
    "폭등": "UP",
    "상승": "UP",
    "반등": "UP",
    "돌파": "UP",
    "경신": "UP",
    "급락": "DOWN",
    "폭락": "DOWN",
    "하락": "DOWN",
    "rose": "UP",
    "jumped": "UP",
    "surged": "UP",
    "gained": "UP",
    "fell": "DOWN",
    "plunged": "DOWN",
    "dropped": "DOWN",
    "declined": "DOWN",
}

# Causal/predictive language: the effect stays an unresolved prediction.
PREDICTIVE_MARKERS_V1 = (
    "전망",
    "예상",
    "기대",
    "우려",
    "가능성",
    "임박",
    "could",
    "may",
    "might",
    "will",
    "expected",
    "forecast",
    "outlook",
)


@dataclass(frozen=True)
class TaxonomyObservationV1:
    """One deterministic admission proposal for the registration ledger."""

    kind: str
    unit_key: str
    canonical_key: "str | None"
    proposed_state: str
    reason: str
    display_text: str
    evidence: Mapping


@dataclass(frozen=True)
class TaxonomyAdmissionResultV1:
    observations: int
    admitted_auto: int
    pending: int
    candidates: int


def taxonomy_unit_id(kind: str, unit_key: str) -> str:
    return hashlib.sha256(f"{kind}\0{unit_key}".encode()).hexdigest()


def taxonomy_evidence_digest(evidence: Mapping) -> str:
    raw = json.dumps(
        dict(evidence), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    )
    return hashlib.sha256(raw.encode()).hexdigest()


def build_adjudication_request(unit_row: Mapping) -> dict:
    """Neutral pending-queue item for an injected adjudicator callable.

    Deliberately carries no route, no vendor, and no transport field: the
    stage-7 owner decides how (and whether) to answer each question."""
    try:
        evidence = json.loads(unit_row["payload_json"])
    except (KeyError, TypeError, ValueError):
        evidence = {}
    return {
        "v": 1,
        "kind": unit_row["kind"],
        "unit_key": unit_row["unit_key"],
        "canonical_key": unit_row.get("canonical_key"),
        "reason": unit_row["reason"],
        "display_text": unit_row.get("display_text", ""),
        "evidence": evidence,
        "question": (
            "registration verdict requested: VERIFIED or REJECTED"
            " for this unit, based only on the evidence above"
        ),
    }


# ------------------------------------------------- stage 7: batch protocol

# Strict per-unit verdicts an injected batch transport may return. DEFERRED
# keeps the unit pending (durably marked for its current evidence) without
# deleting history; VERIFIED/REJECTED map to the model-adjudicated states.
ADJUDICATION_VERDICTS_V1 = ("VERIFIED", "REJECTED", "DEFERRED")

_ADJUDICATION_RESULT_REQUIRED = frozenset({"unit_id", "verdict"})
_ADJUDICATION_RESULT_ALLOWED = _ADJUDICATION_RESULT_REQUIRED | {
    "reason",
    "canonical_key",
}


def _canonical_json(payload) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def adjudication_job_id(unit_rows) -> str:
    """Durable identity of one pending batch: the digest over the exact
    (unit_id, evidence_digest) pairs. Order-independent, so the same
    pending evidence can never start twice under a reshuffled batch."""
    pairs = sorted((row["unit_id"], row["evidence_digest"]) for row in unit_rows)
    raw = "\n".join(f"{unit_id}\0{digest}" for unit_id, digest in pairs)
    return hashlib.sha256(raw.encode()).hexdigest()


def adjudication_response_digest(response) -> str:
    return hashlib.sha256(_canonical_json(response).encode()).hexdigest()


def build_adjudication_batch_request(unit_rows) -> dict:
    """One provider-neutral batch request over a bounded, deterministically
    ordered pending selection. Carries no route, vendor, or transport
    field; per-unit payloads are the single-unit neutral requests plus the
    correlation ``unit_id``."""
    units = []
    for row in unit_rows:
        item = build_adjudication_request(row)
        item["unit_id"] = row["unit_id"]
        units.append(item)
    return {
        "v": 1,
        "units": units,
        "unit_ids": tuple(item["unit_id"] for item in units),
        "request_digest": hashlib.sha256(_canonical_json(units).encode()).hexdigest(),
        "question": (
            "batch registration verdicts requested: exactly one "
            "VERIFIED, REJECTED, or DEFERRED result per unit_id, "
            "based only on each unit's evidence"
        ),
    }


def _allowed_canonicals(unit: Mapping):
    """Canonical/type assignment is only ever a choice among evidence the
    unit already carries — an ENTITY_ALIAS may pick one of its observed
    canonicals, a CATALYST one controlled-vocabulary type. Every other
    kind (and every other value) is an invalid canonical transition."""
    evidence = unit.get("evidence")
    evidence = evidence if isinstance(evidence, Mapping) else {}
    if unit["kind"] == "ENTITY_ALIAS":
        allowed = set()
        by_canonical = evidence.get("by_canonical")
        if isinstance(by_canonical, Mapping):
            allowed.update(str(key) for key in by_canonical)
        canonical = evidence.get("canonical")
        if isinstance(canonical, str):
            allowed.add(canonical)
        stored = unit.get("canonical_key")
        if isinstance(stored, str):
            allowed.add(stored)
        return allowed
    if unit["kind"] == "CATALYST":
        return set(CATALYST_TYPES_V1)
    return None


def validate_adjudication_batch_response(response, request) -> tuple:
    """Strict all-or-nothing batch verdict validation (fail closed).

    The response must cover all and only the requested unit ids — any
    duplicate, missing, or extra id rejects the whole response; so does an
    unknown verdict, an unexpected key, or a canonical/type value outside
    what the unit's own evidence allows. Returns normalized results in
    request order; raises ValueError otherwise (never a partial result)."""
    units_by_id = {unit["unit_id"]: unit for unit in request["units"]}
    if (
        not isinstance(response, Mapping)
        or response.get("v") != 1
        or not set(response) <= {"v", "results"}
    ):
        raise ValueError("ADJUDICATION_RESPONSE_INVALID")
    results = response.get("results")
    if not isinstance(results, (list, tuple)):
        raise ValueError("ADJUDICATION_RESPONSE_INVALID")
    normalized = {}
    for result in results:
        if (
            not isinstance(result, Mapping)
            or not _ADJUDICATION_RESULT_REQUIRED <= set(result)
            or not set(result) <= _ADJUDICATION_RESULT_ALLOWED
        ):
            raise ValueError("ADJUDICATION_RESULT_INVALID")
        unit_id = result["unit_id"]
        if unit_id not in units_by_id:
            raise ValueError("ADJUDICATION_UNIT_EXTRA")
        if unit_id in normalized:
            raise ValueError("ADJUDICATION_UNIT_DUPLICATE")
        verdict = result["verdict"]
        if verdict not in ADJUDICATION_VERDICTS_V1:
            raise ValueError("ADJUDICATION_VERDICT_INVALID")
        reason = result.get("reason", "")
        if type(reason) is not str:
            raise ValueError("ADJUDICATION_RESULT_INVALID")
        canonical = result.get("canonical_key")
        if canonical is not None:
            allowed = _allowed_canonicals(units_by_id[unit_id])
            if (
                verdict != "VERIFIED"
                or type(canonical) is not str
                or allowed is None
                or canonical not in allowed
            ):
                raise ValueError("ADJUDICATION_CANONICAL_INVALID")
        normalized[unit_id] = {
            "unit_id": unit_id,
            "verdict": verdict,
            "reason": reason or "adjudicated",
            "canonical_key": canonical,
        }
    if set(normalized) != set(units_by_id):
        raise ValueError("ADJUDICATION_UNIT_MISSING")
    return tuple(normalized[unit_id] for unit_id in request["unit_ids"])
