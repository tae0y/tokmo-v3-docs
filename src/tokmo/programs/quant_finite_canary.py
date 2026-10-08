"""Quant finite live analyzer + Telegram execution canary (2026-09-04): one
approved run, pure orchestration helpers for the controller's
``canary-preview`` / ``canary-send`` subcommands.

Nothing here opens a file, a socket or a store: the controller injects the
durable analysis-receipt read/write, the model transport and the Telegram
sender. Approved budget for exactly one activation id:

- located KR+US cards ≤ ``MODEL_CALL_CAP`` (4), one model call per card,
  retry 0, and the reservation persisted BEFORE every call;
- Telegram root messages ≤ ``TELEGRAM_ROOT_CAP`` (2): exactly one part per
  market, both markets present;
- a replay of the same activation id makes zero model calls (analysis
  receipt present) and zero sends (send receipt present);
- any card whose analysis fails or is rejected stops the run before
  Telegram — a finite canary has no deterministic-card fallback.

The analysis receipt is bounded and sanitized: validated analyses (the very
statements the card renders, already privacy/number/order-instruction
checked), report/message digests, counts, typed reasons and route-authority
digests only — never the prompt, the raw model output, the evidence pack,
credentials or private paths. An explicit controller-only observation writes
minimal privacy-filtered failing fields through a separate private-store seam;
the analysis receipt carries only its digest and bounded metadata.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re

# canonical quota-authority bounds and parser (single source of truth with the
# NEWS executor); the routing-side markers below are the quant route's own
from tokmo.programs.news_classifier_executor import (
    ROUTE_AUTHORITY_MAX_AGE_SECONDS, ROUTE_AUTHORITY_MAX_FUTURE_SECONDS, _QUOTA_PROVIDER_KEY,
    _QUOTA_REQUIRED_WINDOWS, _QUOTA_SCHEMA_ID, _parse_observed_at,
)
from tokmo.programs.quant_contextual_analysis import (
    ANALYZER_MODEL_PURPOSE, LIST_SECTIONS, MAX_STATEMENT_CHARS, AnalysisValidationError,
    ContextualAnalysisV1, analyzer_adapter, build_analyzer_request, validate_analysis,
)
from tokmo.programs.quant_telegram_card import MARKETS, _private

MODEL_CALL_CAP = 4
TELEGRAM_ROOT_CAP = 2
RETRIES = 0
# exact sanctioned route (MODEL_ROUTING_STATE: direct Z.AI GLM, provider=zai,
# fallback 0). ``glm-5.3-flash`` is reserved there for the NEWS probe worker,
# so the finite canary binds the general direct GLM model id and nothing else.
FINITE_CANARY_PROVIDER = "zai"
FINITE_CANARY_MODEL_ID = "glm-5.3"
# B1: admission binds the routing authority's own exact line for THIS route —
# backtick-delimited so ``model=glm-5.3-flash`` can never satisfy ``model=glm-5.3``
ROUTE_MARKERS = ("direct Z.AI GLM route", f"`provider={FINITE_CANARY_PROVIDER}`", f"`model={FINITE_CANARY_MODEL_ID}`")
FINITE_CANARY_MODEL_PURPOSE = ANALYZER_MODEL_PURPOSE
FINITE_CANARY_MAX_TOKENS = 4096
FINITE_CANARY_CALL_TIMEOUT_SECONDS = 180
ANALYSIS_RECEIPT_SCHEMA = "quant-finite-canary-analysis-receipt-v1"
# RCA 2026-09-04 (first live activation: 4 calls, 4 validator rejections —
# statement_shape / free_text_number ×2 / statement_text): the prompt carries a
# deterministic skeleton and numbered rules that spell out every validator
# constraint. The placeholder deliberately carries digits so a literal echo of
# the skeleton is rejected by the untouched validator.
STATEMENT_PLACEHOLDER = f"<문장: 아라비아 숫자 없이 {MAX_STATEMENT_CHARS}자 이하>"
PROMPT_MAX_CHARS = 20000


class PromptRejected(ValueError):
    """The prompt for one card exceeds the bound or trips the privacy scan; no call."""
_AUTHORITY_KEYS = ("sha256", "mtime_ns", "observed_at")
_RECORD_KEYS = frozenset(("symbol", "summary", "conditional_order_assessment", "confidence", "labels")
                         + LIST_SECTIONS)
_MAX_REASON_CHARS = 120
_JSON = dict(ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ModelCapExhausted(RuntimeError):
    """Raised instead of a transport call once the durable cumulative cap is reached."""


class RouteNotAdmitted(RuntimeError):
    """Exact-route admission refusal; ``exit_code`` names the typed cause."""

    def __init__(self, exit_code: str):
        self.exit_code = exit_code
        super().__init__(exit_code)


def _iso(now: datetime) -> str:
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("now must be aware")
    return now.astimezone(timezone.utc).isoformat()


# --- admission -----------------------------------------------------------------------

def admit_route(*, routing_text, quota_payload, now: datetime) -> str:
    """Fail-closed exact-route admission over the two authority documents'
    CONTENT (the caller reads the files): the routing state must carry this
    route's own line (``ROUTE_MARKERS``) — the NEWS ``glm-5.3-flash`` probe
    exception never admits it — and the quota authority must be the
    canonical schema, fresh (≤45 min, ≤5 min future skew), provider ok, with
    both required Z.AI windows numeric and positive. Returns the quota
    ``observed_at`` (UTC ISO) for the receipt."""
    if not isinstance(routing_text, str) or any(marker not in routing_text for marker in ROUTE_MARKERS):
        raise RouteNotAdmitted("ROUTE_NOT_ADMITTED")
    if type(quota_payload) is not dict or quota_payload.get("schema") != _QUOTA_SCHEMA_ID:
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    try:
        observed = _parse_observed_at(quota_payload.get("observed_at"))
    except ValueError:
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    now_utc = datetime.fromisoformat(_iso(now))
    if observed - now_utc > timedelta(seconds=ROUTE_AUTHORITY_MAX_FUTURE_SECONDS):
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    if now_utc - observed > timedelta(seconds=ROUTE_AUTHORITY_MAX_AGE_SECONDS):
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    providers = quota_payload.get("providers")
    provider = providers.get(_QUOTA_PROVIDER_KEY) if type(providers) is dict else None
    if type(provider) is not dict or provider.get("ok") is not True or type(provider.get("windows")) is not list:
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    remaining = {window.get("label"): window.get("remaining") for window in provider["windows"]
                 if type(window) is dict and window.get("label") in _QUOTA_REQUIRED_WINDOWS}
    if set(remaining) != set(_QUOTA_REQUIRED_WINDOWS) or any(
            type(value) is bool or not isinstance(value, (int, float)) for value in remaining.values()):
        raise RouteNotAdmitted("MODEL_UNAVAILABLE")
    if any(value <= 0 for value in remaining.values()):
        raise RouteNotAdmitted("QUOTA_EXCEEDED")
    return observed.isoformat()



def card_keys(located: dict) -> list:
    """``market:symbol`` for every located card, MARKETS order then symbol order."""
    keys = []
    for market in MARKETS:
        item = located.get(market)
        if item is None:
            continue
        keys.extend(f"{market}:{card.symbol}" for card in sorted(item.report.cards, key=lambda card: card.symbol))
    return keys


def admit_preview(located: dict) -> str | None:
    """Refusal reason before any reservation: both markets located, 1..cap cards."""
    for market in MARKETS:
        if located.get(market) is None:
            return f"report_missing:{market}"
    keys = card_keys(located)
    if not keys:
        return "no_cards"
    if len(keys) > MODEL_CALL_CAP:
        return f"model_cap_exceeded:{len(keys)}"
    return None


def sanitize_authority(authority) -> dict:
    """Route-authority stamps reduced to digests/mtime/observed_at (no paths)."""
    if type(authority) is not dict:
        return {}
    return {name: ({key: stamp[key] for key in _AUTHORITY_KEYS if key in stamp}
                   if type(stamp) is dict else {})
            for name, stamp in authority.items() if name in ("routing_state", "quota_latest")}


def reserve_analysis_receipt(*, activation_id: str, located: dict, route_authority, now: datetime) -> dict:
    """The durable reservation written before the transport is even built."""
    return {"schema": ANALYSIS_RECEIPT_SCHEMA, "activation_id": activation_id, "status": "RESERVED",
            "reserved_at": _iso(now), "settled_at": None, "off": None,
            "model": {"provider": FINITE_CANARY_PROVIDER, "model_id": FINITE_CANARY_MODEL_ID,
                      "purpose": FINITE_CANARY_MODEL_PURPOSE, "call_cap": MODEL_CALL_CAP, "retry": RETRIES,
                      "timeout_seconds": FINITE_CANARY_CALL_TIMEOUT_SECONDS,
                      "completion_cap": FINITE_CANARY_MAX_TOKENS},
            "telegram_root_cap": TELEGRAM_ROOT_CAP,
            "route_authority": sanitize_authority(route_authority),
            "report_digests": {market: located[market].report_sha256 for market in MARKETS},
            "cards": card_keys(located), "model_calls": 0, "calls": [], "validated": [], "rejected": {}}


# --- bounded model calls ----------------------------------------------------------------

def bind_call_ledger(*, update, cap: int = MODEL_CALL_CAP):
    """(reserve, settle) over an injected ``update(mutate) -> receipt`` that
    applies ``mutate(receipt) -> receipt`` as ONE locked, durable
    read-modify-write. ``reserve(key)`` gates every start on the DURABLE
    count (never an in-memory counter): at ``cap`` it raises
    ``ModelCapExhausted`` inside the critical section without writing, so
    concurrent ledgers, a crashed process's RESERVED row, or a replay can
    never add a fifth start. Otherwise it persists the incremented counter and
    a RESERVED row before the call; ``settle(key, status)`` rewrites that row
    afterwards."""
    def reserve(key: str) -> None:
        def mutate(receipt: dict) -> dict:
            count = int(receipt["model_calls"])
            if count >= cap:
                raise ModelCapExhausted(key)
            return dict(receipt, model_calls=count + 1,
                        calls=list(receipt["calls"]) + [{"key": key, "status": "RESERVED"}])
        update(mutate)

    def settle(key: str, status: str) -> None:
        def mutate(receipt: dict) -> dict:
            calls = [dict(call) for call in receipt["calls"]]
            for call in reversed(calls):
                if call["key"] == key and call["status"] == "RESERVED":
                    call["status"] = status
                    break
            return dict(receipt, calls=calls)
        update(mutate)
    return reserve, settle


def capped_analysis(transport, *, reserve, settle, captured: dict, persist=None, observe=None):
    """The controller's ``analysis(pack)`` seam over the provider-neutral
    adapter: the durable ledger's ``reserve`` gates and persists each start
    before the transport call (refusing at the cap with no call), the typed
    outcome is settled after it, and the validated analysis is stashed in
    ``captured`` so the receipt carries exactly what the card renders.
    Validation failures propagate as the seam's existing ``invalid_output:*``
    reasons (same validator)."""
    adapter = analyzer_adapter(transport)

    def analysis(pack: dict):
        key = f"{pack['market']}:{pack['symbol']}"
        prompt = checked_prompt(pack)  # bounded + privacy-scanned BEFORE any reservation
        reserve(key)
        try:
            output = adapter(pack)
        except Exception as error:
            settle(key, f"FAILED:{type(error).__name__}")
            if observe is not None:
                observe(key, failure_observation(error, None, pack, prompt))
            raise
        settle(key, "RETURNED")
        try:
            captured[key] = validate_analysis(output, pack)
        except AnalysisValidationError as error:
            if observe is not None:
                observe(key, failure_observation(error, output, pack, prompt))
        else:
            if persist is not None:
                persist(key, captured[key])
        return output
    return analysis


def analyzer_prompt_skeleton(request: dict) -> dict:
    """Deterministic compact answer skeleton derived from the request's own
    output contract: exact top-level keys, list items with exactly
    ``statement``/``evidence_ids``, the assessment/confidence key triples,
    real allowed ids (two for a relation), and the pack's conditional
    semantics (order id cited and a non-``해당 없음`` verdict when orders
    exist). Statements are ``STATEMENT_PLACEHOLDER`` so the literal skeleton
    can never pass the validator."""
    contract = request["task"]["output_contract"]
    ids = list(contract["citations"]["allowed_ids"])
    one, two = ids[:1], ids[:2]
    observable = [item['id'] for item in request['evidence_pack']['evidence']
                  if item['kind'] in ('factor', 'zone')]
    def item(cites):
        return {"statement": STATEMENT_PLACEHOLDER, "evidence_ids": list(cites)}
    return {"symbol": contract["symbol"], "summary": STATEMENT_PLACEHOLDER,
            "evidence_relations": [item(two)],
            "conditional_order_assessment": None,
            "alternatives": [item(one)], "counterevidence": [item(one)], "assumptions": [],
            "falsifiers": [item(observable[:1])], "change_conditions": [item(observable[:1])],
            "confidence": {"level": contract["confidence"]["level_enum"][0], "statement": STATEMENT_PLACEHOLDER,
                           "evidence_ids": one}}


def analyzer_prompt_rules(request: dict) -> tuple:
    """Numbered Korean rules, each derived from the request's output contract
    (never a second constant table): one JSON object only; exact key sets;
    item cardinality; verdict/level enums and the pack's conditional
    semantics; citation minimums and uniqueness; non-empty bounded text;
    no digits except the approved metric labels — cite the evidence id that
    holds a number instead of restating it; no probabilities, order
    instructions or private data; and the full allowed-id list."""
    contract = request["task"]["output_contract"]
    text_rules, semantics = contract["statement_text"], contract["conditional_semantics"]
    sections = list(contract["sections"])
    section_contract = contract["sections"][sections[0]]
    section_keys = ", ".join(section_contract["statement_keys"])
    assessment_contract = contract["conditional_order_assessment"]
    confidence_contract = contract["confidence"]
    citations = contract["citations"]
    conditional = (" 기본값은 null이다. 확인된 BUY/SELL 주문이 투자판단이나 실제 제공된 계획과 중요하게 "
                   "일치·상충할 때만 평가하며 해당 주문 id와 관련 지표·구간·계획 id를 함께 인용한다. "
                   "방향만으로 익절·손절 목적을 추론하지 않는다. 해당 주문에 대한 명시적 목적 근거가 있을 때만 사용한다.")
    return (
        "1. 출력은 설명·마크다운·코드펜스·주석·앞뒤 텍스트 없이 JSON 객체 하나뿐이다. 아래 skeleton과 같은 구조로만 답한다.",
        f"2. 최상위 키는 정확히 다음 {len(contract['required_keys'])}개만 쓴다: {', '.join(contract['required_keys'])}. "
        f"다른 키를 추가하지 않으며 symbol은 \"{contract['symbol']}\" 그대로 쓴다.",
        f"3. 목록 섹션({', '.join(sections)})의 각 항목은 정확히 {len(section_contract['statement_keys'])}개 키 "
        f"{section_keys} 만 가진 객체다. 다른 키를 항목에 넣지 않는다. 각 섹션은 항목 "
        f"{section_contract['min_items']}개 이상 {section_contract['max_items']}개 이하이며 "
        "assumptions만 빈 목록 []이 허용된다.",
        f"4. conditional_order_assessment는 null 또는 정확히 {len(assessment_contract['statement_keys'])}개 키 "
        f"{', '.join(assessment_contract['statement_keys'])} 만 가진 객체다. verdict는 "
        f"{', '.join(assessment_contract['verdict_enum'])} 중 하나다.{conditional}",
        f"5. confidence는 정확히 {len(confidence_contract['statement_keys'])}개 키 "
        f"{', '.join(confidence_contract['statement_keys'])} 만 가진 객체다. level은 "
        f"{'/'.join(confidence_contract['level_enum'])} 중 하나다.",
        "6. evidence_ids는 문자열 배열이며 아래 허용 id만 넣는다. 같은 배열 안에서 중복 금지. evidence_relations의 각 항목은 "
        f"서로 다른 id를 {citations['min_per_relation']}개 이상, 그 밖의 모든 statement·assessment·confidence는 "
        f"{citations['min_per_statement']}개 이상 인용한다.",
        f"7. summary와 모든 statement는 비어 있지 않은 문장이며 최대 {text_rules['max_chars']}자다. "
        "모바일 두 카드가 시장 메시지 하나에 들어가도록 짧게 쓴다. summary는 결론 한 문장, "
        "근거 관계는 인과관계 한두 문장, 반대 근거 하나, 선택지는 해당되는 보유·축소 두 문장으로 쓴다. "
        "구간 가격과 위치별 매수 대응은 렌더러가 추가하므로 중복하지 않는다. "
        "falsifiers와 change_conditions는 같은 핵심 반전 조건으로 통합하고 가정은 필요할 때만 쓴다.",
        "8. 자유서술에 아라비아 숫자·%·소수점·분수·배수·날짜 표기를 쓰지 않는다. 유일한 예외는 지표명 "
        f"{', '.join(text_rules['no_digits_except_labels'])}이며 그대로 붙여 쓴다. 가격·수량·비중·수익률·낙폭·기간 같은 "
        "수치는 문장에 다시 적지 말고 그 값을 가진 evidence id를 evidence_ids에 인용해 가리킨다. "
        "입력 label·필드명은 서술 허용 지표명 목록이 아니다. 승인 목록 밖의 숫자가 붙은 내부 지표명을 그대로 복사하지 않는다. "
        "해당 지표의 의미를 숫자 없는 정성 표현으로 설명하고 evidence_ids에 인용한다.",
        "9. 계산되지 않은 도달 확률·가능성 퍼센트를 쓰지 않고 정성 표현만 쓴다. 주문 실행·제출·변경·취소를 지시하는 문장을 "
        "쓰지 않는다. evidence pack 밖의 사실, 계정·보유 세부·경로·비밀 정보를 출력하지 않는다. "
        "허용 id 인용만으로 문장의 의미가 입증되지는 않는다. 인용한 값·지표의 의미와 주장 방향을 대조하고, "
        "근거가 없는 해석은 불확실성으로 명시한다. 중립 RSI를 과매도 근거로 바꾸지 않는다. "
        "MACD의 음수는 부채가 아니라 지표의 부호로 설명한다. 모멘텀은 저평가 근거가 아니며 "
        "재무·가치평가 근거 없이 저평가나 적정가치를 주장하지 않는다. 매수·유지·축소 검토는 이유와 조건을 붙여 제시한다. "
        "change_conditions 하나 이상은 제공된 지표의 기존 부호 경계(양수 수익률·이동평균 대비가 양수가 아니게 됨) "
        "또는 실제 구간 무효화·만료를 인용하고 관측 가능한 판단 반전을 설명한다. 새 수치 임계값은 만들지 않는다. "
        "지표·가격의 기록시점을 구분하며 저장 리포트 검증과 가격 나이 허용은 실시간 시세를 뜻하지 않는다. "
        "age_valid가 false인 가격과 그 가격의 관계값은 참고 전용이며 현재 행동의 근거로 쓰지 않는다. 제공된 완료 세션 종가와 구간은 그 세션 기준으로만 해석한다. 가격 확인 신호가 구현된 것처럼 대기한다고 쓰지 않는다.",
        f"10. 허용 evidence id 전체: {', '.join(contract['citations']['allowed_ids'])}",
    )


def analyzer_prompt_text(request: dict) -> str:
    """One deterministic user message for the direct route: the fixed system
    contract, the task, the numbered rules, the answer skeleton, the derived
    output contract and the evidence pack — nothing ambient, and a bare
    single JSON object demanded."""
    citations = request["task"]["output_contract"]["citations"]
    return "\n".join((
        request["system"],
        f"purpose: {request['model_purpose']} (schema_version {request['schema_version']})",
        request["task"]["instruction"],
        "규칙:",
        *analyzer_prompt_rules(request),
        "skeleton: " + json.dumps(analyzer_prompt_skeleton(request), **_JSON),
        "output_contract: " + json.dumps(request["task"]["output_contract"], **_JSON),
        "evidence_pack: " + json.dumps(request["evidence_pack"], **_JSON),
        "skeleton은 객체마다 키 집합을 그대로 보여준다. 각 객체의 키 집합은 skeleton과 똑같이 유지하고 <placeholder>를 실제 서술로 바꾼다. "
        "항목 수는 규칙 3, level은 규칙 5 안에서 고른다. skeleton의 evidence_ids가 완성 문장을 직접 지지하면 유지하고, 아니면 관련 허용 id로 교체한다. 그 JSON 객체 하나만 출력하라.",
        "출력 직전 자체검사: 모든 evidence_ids에 대해 순서대로 중복 제거, 허용 id 전체와 대조, 최소 개수 확인을 "
        "수행한다. evidence_relations의 각 evidence_ids는 "
        f"서로 다른 관련 허용 id를 최소 {citations['min_per_relation']}개, 그 밖의 모든 statement·assessment·confidence는 "
        f"관련 허용 id를 최소 {citations['min_per_statement']}개 포함해야 한다. 중복·빈 배열·허용 목록 밖 id·최소 개수 미달이 "
        "하나라도 있으면 출력하지 말고 스스로 고친다. summary와 모든 statement에서 승인 지표명을 잠시 제거한 뒤 "
        "아라비아 숫자를 검사한다. 숫자가 남으면 숫자 주장을 정성 문장으로 바꾸고 해당 값을 가진 evidence id를 인용한 뒤 "
        "JSON 객체 하나만 출력하라.",
    ))


def checked_prompt(pack: dict) -> str:
    """The exact prompt the transport will send, refused (no reservation, no
    call) when it exceeds ``PROMPT_MAX_CHARS`` or trips the privacy scan."""
    prompt = analyzer_prompt_text(build_analyzer_request(pack))
    if len(prompt) > PROMPT_MAX_CHARS:
        raise PromptRejected("prompt_too_large")
    if _private(prompt):
        raise PromptRejected("prompt_privacy")
    return prompt


# --- analysis record round trip ----------------------------------------------------------

def analysis_record(analysis: ContextualAnalysisV1) -> dict:
    record = {"symbol": analysis.symbol, "summary": analysis.summary,
              **{name: [dict(entry) for entry in getattr(analysis, name)] for name in LIST_SECTIONS},
              "conditional_order_assessment": (None if analysis.conditional_order_assessment is None
                                                else dict(analysis.conditional_order_assessment)),
              "confidence": dict(analysis.confidence), "labels": dict(analysis.labels)}
    if analysis.display_version == 2:
        record["display_version"] = 2
    return record


def analysis_from_record(record) -> ContextualAnalysisV1:
    if type(record) is not dict:
        raise ValueError("analysis_record_shape")
    version = record.get("display_version", 1)
    keys = _RECORD_KEYS | ({"display_version"} if version == 2 else set())
    if version not in (1, 2) or set(record) != keys:
        raise ValueError("analysis_record_shape")
    sections = {}
    for name in LIST_SECTIONS:
        entries = record[name]
        if type(entries) is not list or any(type(entry) is not dict for entry in entries):
            raise ValueError("analysis_record_shape")
        sections[name] = tuple(dict(entry) for entry in entries)
    assessment = record["conditional_order_assessment"]
    if (assessment is None and version != 2) or (assessment is not None and type(assessment) is not dict):
        raise ValueError("analysis_record_shape")
    if any(type(record[name]) is not dict for name in ("confidence", "labels")):
        raise ValueError("analysis_record_shape")
    for value in record['labels'].values():
        if version == 1 and type(value) is not str:
            raise ValueError("analysis_record_shape")
        if version == 2 and (type(value) is not dict or set(value) != {'label', 'display', 'asof'}
                             or any(type(item) is not str for item in value.values())):
            raise ValueError("analysis_record_shape")
    return ContextualAnalysisV1(symbol=record["symbol"], summary=record["summary"],
                                conditional_order_assessment=None if assessment is None else dict(assessment),
                                confidence=dict(record["confidence"]), labels=dict(record["labels"]),
                                display_version=version, **sections)


def analyses_from_receipt(receipt: dict) -> dict:
    """{market: {symbol: ContextualAnalysisV1}} from a receipt; shape-checked."""
    analyses = receipt.get("analyses")
    if type(analyses) is not dict:
        raise ValueError("analysis_record_shape")
    result: dict = {}
    for market, by_symbol in analyses.items():
        if market not in MARKETS or type(by_symbol) is not dict:
            raise ValueError("analysis_record_shape")
        result[market] = {}
        for symbol, record in by_symbol.items():
            analysis = analysis_from_record(record)
            if analysis.symbol != symbol:
                raise ValueError("analysis_record_symbol")
            result[market][symbol] = analysis
    return result


# --- messages ----------------------------------------------------------------------------

def message_digest(message) -> str:
    return hashlib.sha256("\n\x1e\n".join(message.parts).encode("utf-8")).hexdigest()


def check_messages(messages) -> str | None:
    """Telegram cap: both markets, exactly one root message each."""
    if [message.market for message in messages] != list(MARKETS):
        return "markets_incomplete"
    for message in messages:
        if len(message.parts) != 1:
            return f"telegram_cap_exceeded:{message.market}:{len(message.parts)}"
    if sum(len(message.parts) for message in messages) > TELEGRAM_ROOT_CAP:
        return "telegram_cap_exceeded"
    return None


def check_message_digests(messages, receipt: dict) -> str | None:
    """M1: every message about to be sent must be byte-identical to the one the
    validated receipt previewed; the first drifted market is the refusal."""
    digests = dict(receipt.get("message_digests", {}))
    for message in messages:
        if digests.get(message.market) != message_digest(message):
            return f"message_digest_drift:{message.market}"
    return None


# --- settlement and delivery admission ---------------------------------------------------

def settle_preview(receipt: dict, *, analysis_evidence: dict, captured: dict, messages, now: datetime) -> dict:
    """ANALYZED only when every located card validated with exactly one call
    each, zero external calls, and one root message per market; otherwise
    FAILED with the first typed reason. Individually validated analyses survive
    failure; all-pass message digests are stored only on ANALYZED."""
    settled = dict(receipt, settled_at=_iso(now))
    for key, analysis in captured.items():
        settled = retain_analysis(settled, key, analysis)
    cards = list(receipt["cards"])
    validated = list(analysis_evidence.get("validated", ()))
    rejected = {str(key): str(reason)[:_MAX_REASON_CHARS]
                for key, reason in dict(analysis_evidence.get("rejected", {})).items()}
    external = dict(analysis_evidence.get("external", {}))
    settled.update(validated=validated, rejected=rejected,
                   external={"queries": int(external.get("queries", 0)), "fetches": int(external.get("fetches", 0))})
    reason = None
    if not analysis_evidence.get("enabled"):
        reason = "analysis_disabled"
    elif rejected:
        reason = "analysis_rejected:" + ",".join(sorted(rejected))
    elif sorted(validated) != sorted(cards) or sorted(captured) != sorted(cards):
        reason = "analysis_coverage_mismatch"
    elif settled["model_calls"] != len(cards) or analysis_evidence.get("model_calls") != len(cards):
        reason = "model_call_count_mismatch"
    elif settled["external"]["queries"] or settled["external"]["fetches"]:
        reason = "external_calls_forbidden"
    else:
        reason = check_messages(messages)
    if reason is None:
        analyses: dict = {}
        for key, analysis in captured.items():
            market, _separator, symbol = key.partition(":")
            analyses.setdefault(market, {})[symbol] = analysis_record(analysis)
        settled["analyses"] = analyses
        settled["message_digests"] = {message.market: message_digest(message) for message in messages}
        settled["message_chars"] = {message.market: len(message.parts[0].encode("utf-16-le")) // 2
                                    for message in messages}
        if _private(json.dumps(settled, **_JSON)):
            for name in ("analyses", "message_digests", "message_chars"):
                settled.pop(name, None)
            reason = "receipt_privacy"
    settled["status"] = "ANALYZED" if reason is None else "FAILED"
    settled["off"] = reason
    return settled


def admit_send(*, receipt, located: dict, expected: dict) -> str | None:
    """Delivery precondition: a validated receipt whose digests equal both the
    operator's expectation and the currently located reports, covering every
    located card with a well-formed stored analysis."""
    if receipt is None:
        return "analysis_receipt_missing"
    if type(receipt) is not dict or receipt.get("schema") != ANALYSIS_RECEIPT_SCHEMA:
        return "analysis_receipt_shape"
    if receipt.get("status") != "ANALYZED":
        return f"analysis_not_validated:{receipt.get('status')}"
    for market in MARKETS:
        if market not in expected:
            return f"expected_digest_missing:{market}"
        item = located.get(market)
        if item is None:
            return f"report_missing:{market}"
        if dict(receipt.get("report_digests", {})).get(market) != item.report_sha256:
            return f"report_digest_drift:{market}"
        if expected[market] != item.report_sha256:
            return f"report_digest_mismatch:{market}"
    if sorted(receipt.get("validated", ())) != sorted(card_keys(located)):
        return "analysis_coverage_mismatch"
    try:
        analyses = analyses_from_receipt(receipt)
    except ValueError as error:
        return f"analysis_record_invalid:{error.args[0]}"
    stored = sorted(f"{market}:{symbol}" for market, by_symbol in analyses.items() for symbol in by_symbol)
    if stored != sorted(card_keys(located)):
        return "analysis_coverage_mismatch"
    return None


# One-off owner observation. No provider, file or transport effects here.
def retain_analysis(receipt: dict, key: str, analysis: ContextualAnalysisV1) -> dict:
    if receipt['status'] != 'RESERVED' or key not in receipt['cards'] or key in receipt['rejected']:
        raise ValueError('observation_not_reserved')
    record = analysis_record(analysis)
    if _private(json.dumps(record, **_JSON)):
        raise ValueError('receipt_privacy')
    market, _, symbol = key.partition(':')
    if symbol != analysis.symbol:
        raise ValueError('analysis_record_symbol')
    analyses = {market: dict(rows) for market, rows in receipt.get('analyses', {}).items()}
    analyses.setdefault(market, {})[symbol] = record
    return dict(receipt, analyses=analyses, validated=list(dict.fromkeys([*receipt['validated'], key])))


def _observation_text(value):
    # Omit the complete suspect field, never truncate a credential into apparent safe prose.
    if type(value) is not str or not value or len(value) > MAX_STATEMENT_CHARS:
        return None
    if _private(value) or re.search(
            r'account|credential|secret|token|password|api.?key|bearer|계좌|계정|비밀|보유|잔고|'
            r'[/\\\\]|https?:|[A-Za-z0-9_-]{24,}|\d{7,}|[\x00-\x1f]', value, re.I):
        return None
    return value


def _observation_original(value, pack):
    """Owner-only bounded field; pattern/known-value redaction is not perfect secrecy.
    Preserve all other bytes (including citation case/whitespace), never normalize.
    Evidence projection keeps its separate conservative scalar allowlist below.
    """
    if type(value) is not str:
        return None, 'nontext'
    if len(value) > MAX_STATEMENT_CHARS:
        return None, 'oversize'
    sensitive = r'account|credential|secret|token|password|api.?key|scope|source_ref|path|계좌|계정'
    known = set()

    def collect(item, private=False):
        if type(item) is dict:
            for key, child in item.items():
                collect(child, private or bool(re.search(sensitive, key, re.I)) or
                        key in ('quantity', 'avg_price', 'market_weight_pct', 'total_weight_pct'))
        elif type(item) is list:
            for child in item:
                collect(child, private)
        elif private and type(item) in (str, int, float) and str(item):
            known.add(str(item))

    collect(pack)
    for token in sorted(known, key=lambda item: (-len(item), item)):
        boundary = r'[A-Za-z0-9_.]' if re.fullmatch(r'[+-]?[0-9]+(?:\.[0-9]+)?', token) else r'\w'
        value = re.sub(r'(?<!' + boundary + ')' + re.escape(token) + r'(?!' + boundary + ')',
                       '[REDACTED]', value)
    patterns = (
        r'(?i)\b(?:api[_-]?key|account(?:[_-]?(?:id|ref|number))?|credential|secret|token|password|계좌|계정)'
        r'\s*[:=]\s*(?:"[^"]*"|\x27[^\x27]*\x27|[^\s,;]+)',
        r'(?i)\b(?:Bearer|account[_-](?:id|ref|number)|api[_-]?key|password)\s+[^\s,;]+',
        r'\b(?:sk-[A-Za-z0-9_-]+|AKIA[A-Z0-9]{16}|gh[pousr]_[A-Za-z0-9_]+|'
        r'eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)\b',
        r'(?i)(?:https?://|(?:[A-Z]:)?[/\\]|~[/\\]|\.{1,2}[/\\]|[\w.-]+[/\\])[^\s,;]+',
        r'(?i)(?:보유|잔고|holdings?|balance)(?:\s*[:=]\s*|\s+(?=\d))[^\n;]+',
        r'(?<![A-Za-z0-9_.])\d{7,}(?![A-Za-z0-9_.])|[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]',
    )
    for pattern in patterns:
        value = re.sub(pattern, '[REDACTED]', value)
    return value, None


def _observation_type(value) -> str:
    return {type(None): 'null', bool: 'bool', int: 'int', float: 'float',
            str: 'str', list: 'list', dict: 'dict'}.get(type(value), 'other')


def failure_observation(error, output, pack: dict, prompt: str) -> dict:
    """Actual first validator frame only; never retain frames, containers or raw JSON.
    Unknown citations are losslessly unwrapped from the adapter's injective prefix.
    Malformed JSON/objects have no safely separable field and retain metadata only.
    """
    from tokmo.programs import quant_contextual_analysis as q
    missing = object()
    entry, text, path = missing, missing, '$'
    root, validator_frame = output, False
    tb = error.__traceback__
    while tb:
        if tb.tb_frame.f_code is q.validate_analysis.__code__:
            root, validator_frame = tb.tb_frame.f_locals['output'], True
        if tb.tb_frame.f_code is q._statement.__code__:
            entry = tb.tb_frame.f_locals['entry']
        if tb.tb_frame.f_code is q._check_text.__code__:
            text = tb.tb_frame.f_locals['text']
        tb = tb.tb_next
    reason = error.args[0] if isinstance(error, AnalysisValidationError) else type(error).__name__
    if type(root) is dict:
        for name in LIST_SECTIONS:
            entries = root.get(name)
            if type(entries) is list:
                for index, item in enumerate(entries[:q.MAX_ITEMS]):
                    if item is entry:
                        path = f'{name}[{index}]'
        for name in ('conditional_order_assessment', 'confidence'):
            if root.get(name) is entry:
                path = name
    ids = q._citation_map(pack)
    cited = entry.get('evidence_ids') if type(entry) is dict else None
    original = missing
    if text is not missing:
        path = 'summary' if entry is missing else path + '.statement'
        original = text
    if reason == 'unknown_evidence_id':
        index, unknown = next((i, item) for i, item in enumerate(cited) if item not in ids)
        path += f'.evidence_ids[{index}]'
        prefix = '!unknown:'
        while any(item.startswith(prefix) for item in ids):
            prefix = '!' + prefix
        original = unknown[len(prefix):] if unknown.startswith(prefix) else unknown
    elif reason == 'unsupported_claim':
        path += '.evidence_ids'
    elif reason.startswith('section_') and reason[8:] in LIST_SECTIONS:
        path = reason[8:]
    elif reason in ('conditional_shape', 'conditional_uncited', 'conditional_invented'):
        path = 'conditional_order_assessment'
    elif reason == 'confidence_shape':
        path = 'confidence'
    elif reason == 'symbol_mismatch':
        path = 'symbol'
    elif reason == 'conditional_side_inferred' and entry is missing:
        path = 'summary'
        original = root['summary']
    if original is missing:
        original, omitted, state = None, 'no_extracted_field', 'shape_only'
    else:
        extracted = original
        original, omitted = _observation_original(extracted, pack)
        state = 'shape_only' if omitted else 'exact' if original == extracted else 'redacted'
    related = {item for item in cited if type(item) is str and item in ids} if type(cited) is list else set()
    if type(text) is str:
        related.update(canonical for canonical, wire in ids.items()
                       if re.search(r'(?<![A-Za-z0-9_])' + re.escape(wire) + r'(?![A-Za-z0-9_])', text))
    # No source/account/scope references or unrelated evidence; explicit scalar projection.
    fields = {'id', 'kind', 'label', 'value', 'currency', 'lower', 'upper', 'invalidation',
              'side', 'trigger_price', 'order_price', 'summary', 'detail', 'orientation',
              'trigger_vs_price_pct', 'trigger_minus_price', 'order_vs_trigger_pct'}
    evidence = [{key: value for key, value in item.items() if key in fields and
                 (value is None or type(value) in (bool, int, float) or _observation_text(value) is not None)}
                for item in [*pack['evidence'], *pack['gaps']] if item['id'] in related]
    shape = {}
    if validator_frame and type(root) is dict and (reason == 'confidence_shape' or
            reason == 'required_sections' and 'confidence' not in root):
        confidence = root.get('confidence', missing)
        is_dict = type(confidence) is dict
        keys = ('level', 'statement', 'evidence_ids')
        level = confidence.get('level', missing) if is_dict else missing
        shape['confidence_shape'] = {
            'actual_type': 'missing' if confidence is missing else _observation_type(confidence),
            'expected_keys_present': {key: key in confidence if is_dict else None for key in keys},
            'unexpected_key_count': sum(key not in keys for key in confidence) if is_dict else None,
            'level_present': level is not missing if is_dict else None,
            'level_type': ('missing' if level is missing else _observation_type(level)) if is_dict else None,
            'level_in_enum': type(level) is str and level in q.CONFIDENCE_LEVELS if level is not missing else None}
    if validator_frame and reason == 'statement_text' and text is not missing and state != 'exact':
        shape['text_shape'] = {'actual_type': _observation_type(text),
                               'length': len(text) if type(text) is str else None,
                               'blank': not text.strip() if type(text) is str else None}
    return {'class': reason, 'path': path, 'original': original,
            'original_state': state, 'original_omitted': omitted,
            'allowed_ids': sorted(ids), 'related_evidence': evidence,
            'prompt_sha256': hashlib.sha256(prompt.encode()).hexdigest(), **shape}


def partial_card_snapshot(*, key, analysis, card, context, standings, as_of) -> dict:
    """Existing renderer, original report standings/context, no overflow fallback."""
    from tokmo.programs.quant_telegram_card import _card_block, MESSAGE_CHAR_CAP
    block = _card_block(card, context, standings, set(), [], analysis, asof=as_of)
    text = f'TEST / PARTIAL / as-of {as_of}\n{key}\n\n{block}'
    if _private(text) or len(text.encode('utf-16-le')) // 2 > MESSAGE_CHAR_CAP:
        raise ValueError('partial_card_unrenderable')
    return {'text': text, 'sha256': hashlib.sha256(text.encode()).hexdigest(),
            'analysis_sha256': hashlib.sha256(json.dumps(analysis_record(analysis), **_JSON).encode()).hexdigest()}


def partial_cards_from_receipt(receipt: dict, *, requested=False) -> tuple:
    """Pure one-off replay of exact existing-renderer snapshots. Never sends.
    Controller alone owns explicit user DM <owner-chat-id> transport, never stock bot/group.
    """
    if not requested:
        return ()
    try:
        if receipt['schema'] != ANALYSIS_RECEIPT_SCHEMA or receipt['status'] not in ('RESERVED', 'FAILED', 'ANALYZED'):
            raise ValueError('partial_receipt_shape')
        analyses = analyses_from_receipt(receipt)
        keys = receipt['validated']
        if type(keys) is not list or len(set(keys)) != len(keys) or len(keys) > MODEL_CALL_CAP:
            raise ValueError('partial_receipt_shape')
        snapshots = receipt['partial_cards']
        if set(snapshots) != set(keys) or any(key not in receipt['cards'] or key in receipt['rejected'] for key in keys):
            raise ValueError('partial_coverage')
        texts = []
        for key in keys:
            market, symbol = key.split(':')
            snapshot, analysis = snapshots[key], analyses[market][symbol]
            if snapshot == {'omitted': 'partial_card_unrenderable'}:
                continue
            text = snapshot['text']
            from tokmo.programs.quant_telegram_card import _analysis_lines, _actionable_analysis_lines, MESSAGE_CHAR_CAP
            analysis_lines = _actionable_analysis_lines(analysis) if analysis.display_version == 2 else _analysis_lines(analysis)
            if (not text.startswith('TEST / PARTIAL / as-of ') or _private(text)
                    or len(text.encode('utf-16-le')) // 2 > MESSAGE_CHAR_CAP
                    or snapshot['sha256'] != hashlib.sha256(text.encode()).hexdigest()
                    or snapshot['analysis_sha256'] != hashlib.sha256(json.dumps(analysis_record(analysis), **_JSON).encode()).hexdigest()
                    or any(line not in text for line in analysis_lines)):
                raise ValueError('partial_content_invalid')
            texts.append(text)
        return tuple(texts)
    except (KeyError, TypeError, AttributeError) as error:
        raise ValueError('partial_receipt_shape') from None
