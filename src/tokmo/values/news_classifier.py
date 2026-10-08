"""Single-config, exact-route NEWS classifier contract (never fallback)."""
from dataclasses import dataclass
from collections.abc import Mapping
import json
from pathlib import Path
import re

DEFAULT_NEWS_CLASSIFIER_MODEL_ID = "glm-5.3-flash"
NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID = "news-classifier-output-v1"
NEWS_CLASSIFIER_PROVIDER = "zai"
# Single exact route only (never fallback): the built-in catalog admits
# the GLM model exclusively under the direct zai provider.
DEFAULT_NEWS_CLASSIFIER_CATALOG = {DEFAULT_NEWS_CLASSIFIER_MODEL_ID:
                                   NEWS_CLASSIFIER_PROVIDER}
NEWS_CLASSIFIER_EVENT_CLASSES = frozenset(
    {"CORE_BREAKING", "ORDINARY", "UNDECIDED"})
# Bounded model-output cap (bytes) enforced by the direct transport
# before any parse work and re-checked by the executor's strict parser:
# the classifier answer is a tiny JSON object, anything larger fails
# closed as PARSE_FAIL. Lives in the values layer so the executor
# composes the contract without importing a live transport module.
NEWS_CLASSIFIER_OUTPUT_MAX_BYTES = 64 * 1024
# The exact-key projection IS the redaction policy: an output may carry
# nothing but the route echo, the typed event class, and citations that
# resolve into the public evidence keys. Any other key — holdings, account,
# price, free-form prose — fails closed as SCHEMA_INVALID.
NEWS_CLASSIFIER_OUTPUT_REQUIRED_KEYS = frozenset(
    {"provider", "model_id", "event_class", "citations"})
NEWS_CLASSIFIER_OUTPUT_ALLOWED_KEYS = NEWS_CLASSIFIER_OUTPUT_REQUIRED_KEYS
# Output contract r2 (SDS 2026-09-01 §D.4 "출력 계약" second revision —
# spelled r2 because the N2 architecture seal reserves the other version
# token for the frozen legacy-path scan): the model extracts bounded
# structured facts only — no event_class key exists, so a model label can
# never re-enter the event-class axis. This is a parsing/validation-layer
# contract: the persisted output_schema_id stays the v1 literal (M-1).
NEWS_CLASSIFIER_OUTPUT_R2_KEYS = frozenset(
    {"provider", "model_id", "event_type", "confirmation_claim",
     "surprise_claim", "actor", "citations"})
# Contract r3 (A1 §Q.2 + A3 §V1): the relevance axis joins the exact
# projection; official evidence leaves the classifier input entirely.
NEWS_CLASSIFIER_OUTPUT_R3_KEYS = NEWS_CLASSIFIER_OUTPUT_R2_KEYS | frozenset(
    {"relevance_claim", "relevance_citations"})
NEWS_CLASSIFIER_ACTOR_MAX_BYTES = 120
_SLUG = re.compile(r"[a-z0-9][a-z0-9.-]{0,127}")

class NewsClassifierRouteError(ValueError): pass
NewsClassifierConfigError = NewsClassifierRouteError

class NewsClassifierFailure(RuntimeError):
    def __init__(self, exit_code):
        self.exit_code=exit_code
        super().__init__(exit_code)

@dataclass(frozen=True)
class ResolvedRoute:
    provider: str
    model_id: str

@dataclass(frozen=True)
class NewsClassifierRequestV1:
    purpose: str
    evidence: dict[str,str]
    input_evidence_digest: str
    prompt_policy_digest: str
    output_schema_id: str
    provider: str
    model_id: str
    model_contract_digest: str

def validate_classifier_route(configured: str, admitted_catalog):
    if type(configured) is not str or not _SLUG.fullmatch(configured) or configured == "latest": raise NewsClassifierRouteError("non-canonical classifier route")
    if not isinstance(admitted_catalog,Mapping):raise NewsClassifierRouteError("classifier catalog must map model to route")
    if configured not in admitted_catalog: raise NewsClassifierRouteError("classifier model is not approved")
    route=admitted_catalog[configured]
    if isinstance(route,str): return ResolvedRoute(route,configured)
    if route.model_id != configured: raise NewsClassifierRouteError("classifier route mismatch")
    return route

def validate_classifier_output_v1(output, *, provider, model_id, evidence):
    """Strict news-classifier-output-v1 judgment; returns canonical JSON.

    Raises ValueError("SCHEMA_INVALID") for any shape outside the exact
    allowed projection (the redaction policy), ValueError("MODEL_REJECTED")
    on a provider/model echo mismatch, and
    ValueError("CITATION_UNRESOLVED") for citations that do not resolve
    into the supplied public evidence keys."""
    if not isinstance(output, dict) \
            or not NEWS_CLASSIFIER_OUTPUT_REQUIRED_KEYS <= set(output) \
            or not set(output) <= NEWS_CLASSIFIER_OUTPUT_ALLOWED_KEYS:
        raise ValueError("SCHEMA_INVALID")
    if type(output["provider"]) is not str or type(output["model_id"]) is not str:
        raise ValueError("SCHEMA_INVALID")
    if output["event_class"] not in NEWS_CLASSIFIER_EVENT_CLASSES:
        raise ValueError("SCHEMA_INVALID")
    citations = output["citations"]
    if type(citations) is not list \
            or any(type(item) is not str for item in citations):
        raise ValueError("SCHEMA_INVALID")
    if output["provider"] != provider or output["model_id"] != model_id:
        raise ValueError("MODEL_REJECTED")
    if not set(citations) <= set(evidence):
        raise ValueError("CITATION_UNRESOLVED")
    return json.dumps(output, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)

def validate_classifier_output_r2(output, *, provider, model_id, evidence):
    """Strict r2 fact-extraction judgment; returns canonical JSON.

    Isomorphic to :func:`validate_classifier_output_v1`: exact allowed key
    projection (the redaction policy), closed vocabularies, bool exclusion,
    provider/model echo (MODEL_REJECTED), and citations that resolve into
    the supplied public evidence keys (CITATION_UNRESOLVED). Parsing layer
    only — no store row ever carries a new schema id (§D.4 M-1)."""
    from tokmo.values.news_event_confirmation import (
        CONFIRMATION_STATES_V1,
        EVENT_TYPES_V1,
        SURPRISE_CLAIMS_V1,
    )
    if not isinstance(output, dict) \
            or set(output) != NEWS_CLASSIFIER_OUTPUT_R2_KEYS:
        raise ValueError("SCHEMA_INVALID")
    for key in ("provider", "model_id", "event_type", "confirmation_claim",
                "surprise_claim", "actor"):
        if type(output[key]) is not str:
            raise ValueError("SCHEMA_INVALID")
    if output["event_type"] not in EVENT_TYPES_V1:
        raise ValueError("SCHEMA_INVALID")
    if output["confirmation_claim"] not in CONFIRMATION_STATES_V1:
        raise ValueError("SCHEMA_INVALID")
    if output["surprise_claim"] not in SURPRISE_CLAIMS_V1:
        raise ValueError("SCHEMA_INVALID")
    if len(output["actor"].encode("utf-8")) > NEWS_CLASSIFIER_ACTOR_MAX_BYTES:
        raise ValueError("SCHEMA_INVALID")
    citations = output["citations"]
    if type(citations) is not list \
            or any(type(item) is not str for item in citations):
        raise ValueError("SCHEMA_INVALID")
    if output["provider"] != provider or output["model_id"] != model_id:
        raise ValueError("MODEL_REJECTED")
    if not set(citations) <= set(evidence):
        raise ValueError("CITATION_UNRESOLVED")
    return json.dumps(output, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)

def validate_classifier_output_r3(output, *, provider, model_id, evidence):
    """Strict r3 fact-extraction judgment; returns canonical JSON.

    r2 plus the bounded relevance axis (§Q.2): closed relevance vocabulary,
    citation-required promotion, and BOTH citation lists resolving into the
    supplied public evidence keys. Parsing layer only — the persisted
    output_schema_id stays the v1 literal (§D.4 M-1)."""
    from tokmo.values.news_event_confirmation import (
        CONFIRMATION_STATES_V1,
        EVENT_TYPES_V1,
        RELEVANCE_CLAIMS_V1,
        SURPRISE_CLAIMS_V1,
    )
    if not isinstance(output, dict) \
            or set(output) != NEWS_CLASSIFIER_OUTPUT_R3_KEYS:
        raise ValueError("SCHEMA_INVALID")
    for key in ("provider", "model_id", "event_type", "confirmation_claim",
                "surprise_claim", "actor", "relevance_claim"):
        if type(output[key]) is not str:
            raise ValueError("SCHEMA_INVALID")
    if output["event_type"] not in EVENT_TYPES_V1:
        raise ValueError("SCHEMA_INVALID")
    if output["confirmation_claim"] not in CONFIRMATION_STATES_V1:
        raise ValueError("SCHEMA_INVALID")
    if output["surprise_claim"] not in SURPRISE_CLAIMS_V1:
        raise ValueError("SCHEMA_INVALID")
    if output["relevance_claim"] not in RELEVANCE_CLAIMS_V1:
        raise ValueError("SCHEMA_INVALID")
    if len(output["actor"].encode("utf-8")) > NEWS_CLASSIFIER_ACTOR_MAX_BYTES:
        raise ValueError("SCHEMA_INVALID")
    for key in ("citations", "relevance_citations"):
        if type(output[key]) is not list \
                or any(type(item) is not str for item in output[key]):
            raise ValueError("SCHEMA_INVALID")
    if output["relevance_claim"] != "NONE" \
            and not output["relevance_citations"]:
        raise ValueError("SCHEMA_INVALID")
    if output["provider"] != provider or output["model_id"] != model_id:
        raise ValueError("MODEL_REJECTED")
    if not (set(output["citations"])
            | set(output["relevance_citations"])) <= set(evidence):
        raise ValueError("CITATION_UNRESOLVED")
    return json.dumps(output, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False)

def read_classifier_model(path: Path, *, approved=None):
    path=Path(path)
    if not path.exists():
        model=DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    else:
        try:
            payload=json.loads(path.read_bytes()); model=payload["model_id"]
            if set(payload)!={"model_id"} or type(model) is not str: raise ValueError
        except (OSError,ValueError,KeyError,json.JSONDecodeError) as exc: raise NewsClassifierRouteError("invalid classifier configuration") from exc
    validate_classifier_route(model, approved if approved is not None else DEFAULT_NEWS_CLASSIFIER_CATALOG)
    return model
