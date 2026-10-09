"""Bounded, auditable NEWS classifier orchestration with no automatic fallback."""

import hashlib
import json
from datetime import datetime

from tokmo.values.news_classifier import (
    NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
    NewsClassifierFailure,
    NewsClassifierRequestV1,
    NewsClassifierRouteError,
    read_classifier_model,
    validate_classifier_route,
)
from tokmo.values.news_config import (
    INVESTIGATION_MODEL_CALLS_MAX,
    INVESTIGATION_RETRY_MAX,
    NEWS_DAILY_MODEL_CALL_CAP,
)
from tokmo.values.ops_kernel import AlertChannel, AlertOrigin, AlertOutboxV1

_OUTAGE_EXITS = {
    "RATE_LIMITED",
    "QUOTA_EXCEEDED",
    "MODEL_UNAVAILABLE",
    "MODEL_REJECTED",
}


def investigate(episode_key, evidence, *, store, now):
    return store.upsert(episode_key, tuple(evidence), now)


def classify_event_guarded(
    *,
    direct_holding,
    official_primary,
    irreversible,
    single_source_claim,
    systemic_transition=False,
    propagation_high=False,
    classification_status="OK",
    classifier_event_class="UNDECIDED",
):
    """Deterministic event-class judgment r2 (SDS 2026-09-01 §E.2).

    The old ``classifier_event_class == "CORE_BREAKING"`` promotion branch
    is removed (m-4): the model argument is accepted for signature
    compatibility but can never promote — the two deterministic rules are
    the only CORE_BREAKING paths. v1 compatibility lives solely in settled
    replay rows answering their old identities."""
    if classification_status != "OK":
        return "CLASSIFICATION_DEFERRED"
    if direct_holding and official_primary and irreversible:
        return "CORE_BREAKING"
    if systemic_transition and propagation_high:
        return "CORE_BREAKING"
    return "ORDINARY"


def _identity(route, purpose, prompt_policy_digest, evidence_digest):
    value = {
        "v": 1,
        "domain": "tokmo-v3:news-classifier-call:v1",
        "provider": route.provider,
        "model_id": route.model_id,
        "purpose": purpose,
        "prompt_policy_digest": prompt_policy_digest,
        "input_evidence_digest": evidence_digest,
        "output_schema_id": NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
    }
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _exit(error):
    if isinstance(error, NewsClassifierFailure):
        return error.exit_code
    if isinstance(error, TimeoutError):
        return "TIMEOUT"
    if isinstance(error, json.JSONDecodeError):
        return "PARSE_FAIL"
    if isinstance(error, ValueError) and str(error) in {
        "MODEL_REJECTED",
        "CITATION_UNRESOLVED",
        "SCHEMA_INVALID",
    }:
        return str(error)
    return "TRANSPORT_FAIL"


def _enqueue_outages(store, ops_kernel, role, *, activation_enabled, now):
    if ops_kernel is None:
        return 0
    count = 0
    for (
        outage_id,
        model_id,
        failure_class,
        first_observed_at,
    ) in store.list_unalerted_open_outages():
        body = f"NEWS 분류가 동작하지 않음: 구성 모델 {model_id}, 실패 클래스 {failure_class}, 최초 관측 {first_observed_at}"
        channel = AlertChannel.TELEGRAM if activation_enabled else AlertChannel.SHADOW
        outbox = AlertOutboxV1(
            "nop-" + outage_id.removeprefix("nout-")[:24],
            AlertOrigin.NEWS_OPS,
            None,
            None,
            None,
            None,
            0,
            outage_id,
            "newsops:" + outage_id,
            channel,
            "OPS_FAILURE",
            (body,),
            datetime.fromisoformat(first_observed_at),
        )
        ops_kernel.record_news_ops_failure_outbox(
            role, outbox, activation_enabled=activation_enabled
        )
        store.bind_outage_alert(outage_id, outbox.alert_id, now=now)
        count += 1
    return count


def run_news_investigation(
    *,
    store,
    episode_id,
    purpose,
    evidence,
    prompt_policy_digest,
    model_contract_digest,
    configured_model_id,
    admitted_catalog,
    classifier_call,
    now,
    retry_max=0,
    call_budget=None,
    ops_kernel=None,
    role=None,
    activation_enabled=False,
    config_path=None,
    expected_input_evidence_digest=None,
    candidate_id=None,
    async_queue=None,
):
    if config_path is not None:
        try:
            configured_model_id = read_classifier_model(
                config_path, approved=admitted_catalog
            )
        except NewsClassifierRouteError:
            store.record_outage("invalid-config", "CONFIG_INVALID", now, None)
            _enqueue_outages(
                store, ops_kernel, role, activation_enabled=activation_enabled, now=now
            )
            return {
                "status": "CLASSIFICATION_DEFERRED",
                "exit": "CONFIG_INVALID",
                "classification_identity": None,
            }
    route = validate_classifier_route(configured_model_id, admitted_catalog)
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    evidence_digest = hashlib.sha256(encoded).hexdigest()
    identity = _identity(route, purpose, prompt_policy_digest, evidence_digest)
    if (
        expected_input_evidence_digest is not None
        and expected_input_evidence_digest != evidence_digest
    ):
        return {
            "status": "CLASSIFICATION_DEFERRED",
            "exit": "INPUT_DIGEST_MISMATCH",
            "classification_identity": identity,
        }
    replay = store.find_settled_classification(identity)
    if replay:
        return json.loads(replay["output_json"]) | {
            "classification_identity": identity,
            "status": "OK",
        }
    if async_queue is not None:
        # Durable bounded asynchronous handoff: pulse freezes the request
        # once per identity and stays nonblocking — no call row, no daily
        # budget consumption, no synthetic outage. Only the separately
        # invoked worker starts model transport for this identity.
        queue_state = async_queue(
            {
                "classification_identity": identity,
                "purpose": purpose,
                "candidate_id": candidate_id,
                "episode_id": episode_id,
                "provider": route.provider,
                "model_id": route.model_id,
                "model_contract_digest": model_contract_digest,
                "prompt_policy_digest": prompt_policy_digest,
                "input_evidence_digest": evidence_digest,
                "output_schema_id": NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
                "evidence_json": encoded.decode(),
            },
            now=now,
        )
        return {
            "status": "CLASSIFICATION_DEFERRED",
            "exit": f"ASYNC_{queue_state}",
            "classification_identity": identity,
        }
    attempts = min(retry_max, INVESTIGATION_RETRY_MAX) + 1
    attempts = min(attempts, INVESTIGATION_MODEL_CALLS_MAX)
    last_exit = None
    for _ in range(attempts):
        if store.classifier_calls_started_on(now.date()) >= NEWS_DAILY_MODEL_CALL_CAP:
            return {
                "status": "CLASSIFICATION_DEFERRED",
                "exit": "MODEL_BUDGET_EXCEEDED",
                "classification_identity": identity,
            }
        if (
            call_budget is not None
            and call_budget.get("started", 0) >= call_budget["limit"]
        ):
            return {
                "status": "CLASSIFICATION_DEFERRED",
                "exit": "MODEL_BUDGET_EXCEEDED",
                "classification_identity": identity,
            }
        attempt_no = store.next_classifier_attempt(identity)
        call_id = (
            "ncl-"
            + hashlib.sha256(f"{identity}:{attempt_no}".encode()).hexdigest()[:24]
        )
        call = {
            "call_id": call_id,
            "classification_identity": identity,
            "attempt_no": attempt_no,
            "purpose": purpose,
            "candidate_id": candidate_id,
            "episode_id": episode_id,
            "provider": route.provider,
            "model_id": route.model_id,
            "model_contract_digest": model_contract_digest,
            "prompt_policy_digest": prompt_policy_digest,
            "input_evidence_digest": evidence_digest,
            "output_schema_id": NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
        }
        store.plan_classifier_call(call, now=now)
        store.start_classifier_call(call_id, now=now)
        if call_budget is not None:
            call_budget["started"] = call_budget.get("started", 0) + 1
        request = NewsClassifierRequestV1(
            purpose,
            evidence,
            evidence_digest,
            prompt_policy_digest,
            NEWS_CLASSIFIER_OUTPUT_SCHEMA_ID,
            route.provider,
            route.model_id,
            model_contract_digest,
        )
        try:
            output = json.loads(classifier_call(request))
            if (
                not isinstance(output, dict)
                or "provider" not in output
                or "model_id" not in output
            ):
                raise ValueError("SCHEMA_INVALID")
            if (
                output["provider"] != route.provider
                or output["model_id"] != route.model_id
            ):
                raise ValueError("MODEL_REJECTED")
            if not set(output.get("citations", ())) <= set(evidence):
                raise ValueError("CITATION_UNRESOLVED")
        except Exception as error:
            last_exit = _exit(error)
            store.settle_classifier_call(call_id, last_exit, None, now=now)
            if last_exit in _OUTAGE_EXITS:
                store.record_outage(
                    route.model_id, last_exit, now, model_contract_digest
                )
                _enqueue_outages(
                    store,
                    ops_kernel,
                    role,
                    activation_enabled=activation_enabled,
                    now=now,
                )
                break
            continue
        output_json = json.dumps(output, sort_keys=True, separators=(",", ":"))
        if not store.settle_classifier_call(call_id, "OK", output_json, now=now):
            # crash reconcile won the STARTED row: nothing durable was
            # written, so report a truthful deferral instead of success.
            last_exit = "SETTLE_LOST"
            continue
        store.clear_open_outages(call_id, now=now)
        store.clear_config_invalid_outages(now=now)
        return output | {"classification_identity": identity, "status": "OK"}
    return {
        "status": "CLASSIFICATION_DEFERRED",
        "exit": last_exit,
        "classification_identity": identity,
    }
