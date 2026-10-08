"""Exact-route GLM classifier executor over the direct Z.AI fixed plan.

Single approved route only: provider ``zai``, model ``glm-5.3-flash``, retry 0,
no fallback. The executor never runs inside the pulse lifecycle — the
separately invoked classifier worker owns it. Before every call it reads
the two canonical route authorities (``MODEL_ROUTING_STATE.md`` and
``model-quota-latest.json``) and fails closed on absence, schema
mismatch, missing routing markers, missing/malformed required Z.AI quota
windows, provider outage, exhausted quota, staleness beyond 45 minutes,
or an implausible future observation beyond 5 minutes, then records a
routing receipt that carries authority digests and call metadata only —
never evidence, prompts, or secrets.

Transport is the registered non-agent, no-session adapter
``tokmo.facts.zai_glm_live``: one bounded HTTPS POST whose body is
exactly the public-evidence prompt, credential in the Authorization
header only, never in any argv. There is no Hermes child, no agent
toolset, no session persistence, and no auxiliary
approval/compression/fallback route — those cannot be constructed from
this seam. GLM has no Codex ``--output-schema``: the prompt demands one
bare JSON object and the strict post-parse validator (exact allowed
keys, provider/model echo, evidence-resolving citations) is the only
schema authority. A failed call preserves an owner-only bounded
diagnostics record carrying control-plane facts only (typed error
class, HTTP status, digests); model-derived transcripts cannot survive
because the direct transport never produces any.
"""
import hashlib
import json
import os
import shutil
import stat
from datetime import timedelta
from pathlib import Path

from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_OUTPUT_MAX_BYTES as GLM_OUTPUT_MAX_BYTES,
    NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
    NEWS_CLASSIFIER_PROVIDER,
    NewsClassifierFailure,
    validate_classifier_output_r3,
)
from tokmo.values.news_event_confirmation import EVENT_TYPES_V1

ROUTE_AUTHORITY_MAX_AGE_SECONDS = 45 * 60
# mirror the completion-safe launcher: a quota authority observed more
# than 5 minutes in the future is implausible clock skew — fail closed
ROUTE_AUTHORITY_MAX_FUTURE_SECONDS = 5 * 60
FAILURE_DIAGNOSTICS_MAX_BUNDLES = 20
_FAILURE_EXCERPT_MAX_CHARS = 2000
_REDACTED = "[REDACTED]"
_QUOTA_SCHEMA_ID = "model-quota-latest-v1"
_QUOTA_PROVIDER_KEY = "zai"
# both bounded-rate windows must be present and positive; the monthly MCP
# window is informational and never gates this worker's admission
_QUOTA_REQUIRED_WINDOWS = frozenset({"5시간", "주간"})
# routing-state markers the completion-safe launcher itself demands for
# this exact route: the executor's own admission binds the same authority
# text, retaining completion-safe routing admission without a child
_ROUTE_MARKERS = ("model-quota-latest.json", "invocation freshness",
                  "glm-5.3-flash")


def _file_stamp(path):
    info = os.stat(path)
    return {"path": str(path),
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "mtime_ns": info.st_mtime_ns}


def build_route_authority_reader(*, routing_state_path, quota_latest_path,
                                 now):
    """Fail-closed reader over the two canonical route authorities.

    ``now`` is the composing CLI's explicit judgment instant (no ambient
    clock exists in this tree). Returns a callable(request) -> receipt
    fragment. Every degenerate state raises the typed deferral the
    classifier-call contract maps to DEFERRED: MODEL_UNAVAILABLE for
    absent/malformed/stale/future authority, a routing state that does
    not admit the exact glm-5.3-flash route, a not-ok provider, or a missing or
    malformed required Z.AI window, QUOTA_EXCEEDED for an exhausted
    required window."""
    routing_state_path = Path(routing_state_path)
    quota_latest_path = Path(quota_latest_path)

    def read_authority(request):
        try:
            routing_bytes = routing_state_path.read_bytes()
            routing_stamp = _file_stamp(routing_state_path)
            quota_bytes = quota_latest_path.read_bytes()
            quota_stamp = _file_stamp(quota_latest_path)
            payload = json.loads(quota_bytes)
        except (OSError, ValueError):
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if not isinstance(payload, dict) \
                or payload.get("schema") != _QUOTA_SCHEMA_ID:
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        try:
            routing_text = routing_bytes.decode("utf-8")
        except UnicodeDecodeError:
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if any(marker not in routing_text for marker in _ROUTE_MARKERS):
            # the routing authority does not admit this exact route
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        try:
            observed_at = _parse_observed_at(payload.get("observed_at"))
        except ValueError:
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if observed_at - now \
                > timedelta(seconds=ROUTE_AUTHORITY_MAX_FUTURE_SECONDS):
            # implausibly in the future: same skew bound as the
            # completion-safe launcher's admission
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if now - observed_at \
                > timedelta(seconds=ROUTE_AUTHORITY_MAX_AGE_SECONDS):
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        provider = payload.get("providers", {}).get(_QUOTA_PROVIDER_KEY)
        if not isinstance(provider, dict) or provider.get("ok") is not True:
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        windows = provider.get("windows")
        if not isinstance(windows, list):
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        remaining = {}
        for window in windows:
            if not isinstance(window, dict):
                continue
            label = window.get("label")
            if label in _QUOTA_REQUIRED_WINDOWS:
                remaining[label] = window.get("remaining")
        if set(remaining) != _QUOTA_REQUIRED_WINDOWS:
            # a required 5h/weekly window is missing entirely
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if any(not isinstance(value, (int, float)) or isinstance(value, bool)
               for value in remaining.values()):
            # a required window exists but carries no usable remaining
            raise NewsClassifierFailure("MODEL_UNAVAILABLE")
        if any(value <= 0 for value in remaining.values()):
            raise NewsClassifierFailure("QUOTA_EXCEEDED")
        return {"routing_state": routing_stamp,
                "quota_latest": quota_stamp
                | {"observed_at": observed_at.isoformat()}}

    return read_authority


def _parse_observed_at(value):
    from datetime import datetime, timezone
    observed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("naive observed_at")
    return observed.astimezone(timezone.utc)


def build_glm_prompt(request):
    """Deterministic public-evidence prompt (contract r2); no ambient or
    private state.

    SDS 2026-09-01 §D.2/§D.4 (decision 9): the model extracts bounded
    structured facts only — event_type/confirmation_claim/surprise_claim/
    actor/citations. The final event class, materiality, timing, and
    delivery are owned by deterministic code, so no event-class vocabulary
    exists in this prompt. GLM receives no ``--output-schema`` enforcement,
    so the prompt itself demands the exact output contract: one bare JSON
    object — no prose, no code fences."""
    evidence = json.dumps(request.evidence, sort_keys=True,
                          separators=(",", ":"), ensure_ascii=False)
    event_types = "/".join(sorted(EVENT_TYPES_V1))
    return (
        "역할: Tokmo NEWS 구조화 사실 추출기. 아래 공개 NEWS 근거에서 "
        "유계 구조화 사실만 추출하라. 사건 등급·중요도·발송 판단은 "
        "결정론 코드가 소유한다.\n"
        f"purpose: {request.purpose}\n"
        f"output_schema_id: {request.output_schema_id}\n"
        "규칙: 최종 답변은 설명·코드펜스 없이 JSON 객체 하나뿐이다. "
        "JSON의 key는 provider, model_id, event_type, confirmation_claim, "
        "surprise_claim, actor, citations, relevance_claim, "
        "relevance_citations만 가능하다. "
        f'provider는 정확히 "{request.provider}", model_id는 정확히 '
        f'"{request.model_id}"를 echo한다. '
        f"event_type은 {event_types} 중 하나이며 불확실하면 OTHER를 쓴다. "
        "confirmation_claim은 OFFICIAL_CONFIRMED/MULTI_MEDIA_UNCONFIRMED/"
        "SINGLE_MEDIA_UNCONFIRMED/ANALYSIS_OPINION 중 하나 — 근거 제목에 "
        "보이는 주장 수준일 뿐이며 코드는 이를 하향 방향으로만 반영한다. "
        "surprise_claim은 ROUTINE/SURPRISE/UNKNOWN 중 하나이며 불확실하면 "
        "UNKNOWN을 쓴다. actor는 사건 주체의 짧은 표시명(120바이트 이내)"
        "이다. relevance_claim은 NONE/INDIRECT_MATERIAL/THEMATIC_MATERIAL "
        "중 하나 — 보유 여부 판단이 아니라 evidence 안에서 발견한 간접/"
        "테마 연관 주장이며 확실하지 않으면 NONE을 쓴다. relevance_claim이 "
        "NONE이 아니면 relevance_citations에 근거 evidence key를 1개 이상 "
        "넣고, NONE이면 빈 배열을 쓴다. citations와 relevance_citations"
        "에는 아래 evidence의 key만 넣을 수 있다. evidence "
        "밖의 사실, 계정·보유·가격·경로·비밀 정보는 절대 출력하지 "
        "않는다.\n"
        f"evidence: {evidence}\n")


def _top_level_object_spans(text):
    """Single-pass string/escape-aware scan for top-level ``{...}`` spans.

    Quotes are only tracked inside an object, so prose before an answer
    is ignored and a fenced or wrapped object is found; the scan visits
    each character at most twice (O(n), no quadratic rescans)."""
    completed = []
    stack = []
    in_string = False
    escaped = False
    for index, char in enumerate(text):
        if not stack:
            if char == "{":
                stack.append((index, None))
            continue
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            elif char in "\r\n":
                # Raw newlines are invalid inside JSON strings. Discard the
                # malformed prefix so a later answer can be found without
                # rescanning any suffix.
                stack.clear()
                in_string = False
                escaped = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            stack.append((index, stack[-1][0]))
        elif char == "}":
            start, parent = stack.pop()
            completed.append((start, index, parent))

    # Balanced parents own their nested objects, which are not top-level
    # candidates. A complete child of an unmatched malformed opener is
    # promoted. Every character is still visited exactly once.
    completed_starts = {start for start, _, _ in completed}
    return [(start, end) for start, end, parent in completed
            if parent is None or parent not in completed_starts]


def extract_json_object(text):
    """Locate the answer JSON object: the LAST valid top-level object.

    A single model message may quote the rules or echo a decoy fragment
    before the final answer, so the first decodable object must never
    win: deterministically prefer the last complete top-level JSON
    object (fenced or bare). Only whole top-level objects are candidates
    — nested fragments inside strings or other values can never be
    returned — and the strict downstream validator stays authoritative.
    Raises JSONDecodeError (settling as PARSE_FAIL) when no object is
    present or the message exceeds the bounded output cap."""
    if len(text) > GLM_OUTPUT_MAX_BYTES:
        raise json.JSONDecodeError(
            "model output exceeds the bounded cap", text, 0)
    chosen = None
    for start, end in _top_level_object_spans(text):
        try:
            value = json.loads(text[start:end + 1])
        except ValueError:
            continue
        if isinstance(value, dict):
            chosen = value
    if chosen is None:
        raise json.JSONDecodeError("no JSON object in model message", text, 0)
    return chosen


def read_zai_credential_file(env_file):
    """Fail-closed GLM credential lookup from an owner-checked file.

    Mirrors the fixed-plan launcher's policy for the ``.env`` fallback:
    regular file, no symlink, no group/other access. Every refusal —
    missing file, symlink, insecure permissions, absent key — is the
    launcher's fail-closed condition and maps to MODEL_UNAVAILABLE so
    outage recording and ops alerts fire. The environment-variable
    lookup itself is composed by the worker CLI (the tree's single
    ambient-dependency seam), keeping this module env-free."""
    path = Path(env_file)
    try:
        info = path.lstat()
    except OSError:
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")
    if info.st_mode & 0o077:
        # insecure credential file: the launcher would fail closed too
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        raise NewsClassifierFailure("MODEL_UNAVAILABLE")
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].lstrip()
        name, separator, value = line.partition("=")
        if separator and name.strip() in ("GLM_API_KEY", "ZAI_API_KEY") \
                and value.strip().strip("'\""):
            return value.strip().strip("'\"")
    raise NewsClassifierFailure("MODEL_UNAVAILABLE")


def _sanitize_failure_excerpt(data, secret_texts,
                              max_chars=_FAILURE_EXCERPT_MAX_CHARS):
    """Bounded diagnostics excerpt with every secret-bearing line removed.

    Direct occurrences of each secret are replaced, and any line sharing a
    whole line of a secret is dropped entirely."""
    if isinstance(data, bytes):
        data = data.decode("utf-8", errors="replace")
    fragments = []
    for secret in secret_texts:
        if not secret or len(secret) < 4:
            continue
        data = data.replace(secret, _REDACTED)
        fragments.extend(line.strip() for line in secret.splitlines()
                         if len(line.strip()) >= 4)
    lines = [_REDACTED if any(fragment in line for fragment in fragments)
             else line for line in data.splitlines()]
    text = "\n".join(lines)
    if len(text) > max_chars:
        text = text[:max_chars] + "…[truncated]"
    return text


def _bundle_identity(request, prompt_text):
    return (request.input_evidence_digest,
            hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()[:16])


_BUNDLE_SEQUENCE = []  # monotonic fallback counter (no ambient clock)


def _preserve_failure_diagnostics(*, diagnostics_root, error,
                                  request, prompt_text, now=None):
    """Owner-only bounded failure record rescued after a typed failure.

    Retains control-plane facts only — typed exit, provider/model echo,
    digest identifiers, bounded sanitized error classes — never the
    prompt, the evidence, the model output, or any agent transcript
    (the direct-API transport produces none). ``now`` is the caller's
    injected judgment instant (used only for the bundle name; a
    sequence-suffixed name keeps bundles unique without any ambient
    clock). Best-effort: a diagnostics error must never mask the typed
    transport failure."""
    try:
        diagnostics_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        evidence_digest, prompt_digest = _bundle_identity(
            request, prompt_text)
        _BUNDLE_SEQUENCE.append(None)
        prefix = (now.strftime("%Y%m%dT%H%M%S")
                  if now is not None
                  else f"seq{len(_BUNDLE_SEQUENCE):06d}")
        bundle = diagnostics_root / f"{prefix}-{prompt_digest}"
        bundle.mkdir(mode=0o700)
        secrets = [prompt_text]
        for key, value in request.evidence.items():
            secrets.extend(text for text in (str(key), str(value))
                           if len(text) >= 4)
        carried = getattr(error, "failure_diagnostics", None) or {}
        record = {
            "record": "news-classifier-failure-diagnostics-v1",
            "exit_code": getattr(error, "exit_code", "TIMEOUT"),
            "provider": request.provider,
            "model_id": request.model_id,
            "purpose": request.purpose,
            "input_evidence_digest": evidence_digest,
            "prompt_policy_digest": request.prompt_policy_digest,
            "http_status": carried.get("http_status"),
            "error_class": _sanitize_failure_excerpt(
                carried.get("error_class") or "", secrets),
        }
        record_path = bundle / "failure.json"
        record_path.write_text(
            json.dumps(record, ensure_ascii=False, sort_keys=True),
            encoding="utf-8")
        os.chmod(record_path, 0o600)
        bundles = sorted(
            (path for path in diagnostics_root.iterdir() if path.is_dir()),
            key=lambda path: (path.stat().st_mtime_ns, path.name))
        for stale in bundles[:-FAILURE_DIAGNOSTICS_MAX_BUNDLES]:
            shutil.rmtree(stale, ignore_errors=True)
    except OSError:
        pass


def build_glm_classifier_call(*, glm_runner, authority_reader,
                              receipt_sink, timeout_seconds, work_dir,
                              clock):
    """Compose the production classifier_call for the async worker.

    ``glm_runner`` is a ``prompt_text, timeout_seconds -> message text``
    callable owning the credential boundary (production wiring builds it
    from :func:`read_zai_credential_file` and the registered
    ``tokmo.facts.zai_glm_live.zai_chat_once`` adapter). ``clock`` is the
    injected aware-UTC callable used only to stamp failure-diagnostics
    bundle names; no ambient clock is read here. The returned callable
    takes a NewsClassifierRequestV1 and returns the canonical validated
    output JSON string, or raises the typed failures the investigation
    contract settles (MODEL_REJECTED, MODEL_UNAVAILABLE, QUOTA_EXCEEDED,
    TIMEOUT, TRANSPORT_FAIL, EMPTY_SUCCESS, PARSE_FAIL, SCHEMA_INVALID,
    CITATION_UNRESOLVED). The prompt never touches disk: it lives in
    memory for exactly one bounded call."""
    work_dir = Path(work_dir)

    def classifier_call(request):
        if request.provider != NEWS_CLASSIFIER_PROVIDER \
                or request.model_id != DEFAULT_NEWS_CLASSIFIER_MODEL_ID \
                or request.output_schema_id \
                != NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID:
            raise NewsClassifierFailure("MODEL_REJECTED")
        authority = authority_reader(request)
        prompt_text = build_glm_prompt(request)
        try:
            message = glm_runner(prompt_text, timeout_seconds)
            if not isinstance(message, str) or not message.strip():
                raise NewsClassifierFailure("EMPTY_SUCCESS")
        except (NewsClassifierFailure, TimeoutError) as error:
            _preserve_failure_diagnostics(
                diagnostics_root=work_dir / "failure_diagnostics",
                error=error, request=request, prompt_text=prompt_text,
                now=clock() if clock is not None else None)
            raise
        output = extract_json_object(message)  # settles as PARSE_FAIL
        canonical = validate_classifier_output_r3(
            output, provider=request.provider, model_id=request.model_id,
            evidence=request.evidence)
        receipt_sink({
            "receipt": "news-classifier-routing-receipt-v1",
            "provider": request.provider,
            "model_id": request.model_id,
            "purpose": request.purpose,
            "output_schema_id": request.output_schema_id,
            "input_evidence_digest": request.input_evidence_digest,
            "prompt_policy_digest": request.prompt_policy_digest,
            "timeout_seconds": timeout_seconds,
            "retry_max": 0,
            "transport": "direct-zai-chat-completions",
            "route_authority": authority,
            "output_digest": hashlib.sha256(
                canonical.encode("utf-8")).hexdigest(),
        })
        return canonical

    return classifier_call
