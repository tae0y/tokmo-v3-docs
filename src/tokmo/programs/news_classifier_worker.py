"""Separately invoked NEWS classifier worker over the durable work queue.

The pulse producer freezes classification requests into
``classifier_work_queue`` and never blocks on a model; this worker — run
by an operator or an explicitly approved job, never created here — leases
a bounded batch, re-derives the exact single route from the same config
authority, and drives ``run_news_investigation`` with the production
GLM executor so plan/start/settle, daily budget, outage recording, and
the news-classifier-output-v1 validation stack stay the one shared
authority. Crash safety: a leased item whose lease expires falls back to
PENDING (attempt already consumed) and its STARTED call row reconciles to
UNKNOWN on the next pulse tick; no false durable state can survive.

Production invocation (fail-closed on every missing authority)::

    PYTHONPATH=src python -m tokmo.programs.news_classifier_worker \
        --root <runtime-root> --now <aware-ISO> \
        --routing-state ~/.hermes/MODEL_ROUTING_STATE.md \
        --quota-latest ~/.hermes/cron/state/model-quota-latest.json
"""

import argparse
import json
import os
import tempfile
from datetime import datetime
from pathlib import Path

from tokmo.programs.news_investigation import run_news_investigation
from tokmo.programs.news_production import (
    _PROMPT_POLICY_DIGEST as PRODUCTION_PROMPT_POLICY_DIGEST,
)
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_PROVIDER,
    NewsClassifierRouteError,
    read_classifier_model,
    validate_classifier_route,
)
from tokmo.values.news_config import PRODUCER_CANDIDATE_ATTEMPT_MAX

WORKER_CALL_TIMEOUT_SECONDS = 600
WORKER_LEASE_SECONDS = 900  # strictly above the call timeout: no mid-call
WORKER_MAX_ITEMS = 2  # matches the approved per-slot start bound

_DEFERRED_STOP_EXITS = {"MODEL_BUDGET_EXCEEDED", "CONFIG_INVALID"}


def run_classifier_worker(
    *,
    store,
    ops_kernel,
    role,
    classifier_call,
    config_path,
    admitted_catalog,
    now,
    lease_owner,
    lease_seconds=WORKER_LEASE_SECONDS,
    max_items=WORKER_MAX_ITEMS,
    attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
    policy_digest=None,
):
    """Drain a bounded batch of frozen classifier work items.

    Route discipline: an item frozen for a model that no longer equals the
    single configured route is settled STALE without transport — the pulse
    re-freezes under the current route's identity. Policy discipline (SDS
    2026-09-01 §H.1-2): an item frozen under a prompt policy other than
    ``policy_digest`` (default: the production producer policy) is settled
    STALE/POLICY_CHANGED the same way, so old frozen evidence never mixes
    with the current prompt. Config invalidity and an exhausted daily
    budget are typed deferrals that refund the item's attempt and stop the
    batch."""
    if policy_digest is None:
        policy_digest = PRODUCTION_PROMPT_POLICY_DIGEST
    report = {
        "leased": 0,
        "settled_ok": 0,
        "released": 0,
        "exhausted": 0,
        "stale": 0,
        "config_invalid": False,
        "stopped_budget": False,
    }
    items = store.lease_classifier_work(
        lease_owner,
        now=now,
        lease_seconds=lease_seconds,
        limit=max_items,
        attempt_max=attempt_max,
    )
    report["leased"] = len(items)
    for item in items:
        identity = item["classification_identity"]
        try:
            if config_path is None:
                configured = validate_classifier_route(
                    DEFAULT_NEWS_CLASSIFIER_MODEL_ID, admitted_catalog
                ).model_id
            else:
                configured = read_classifier_model(
                    Path(config_path), approved=admitted_catalog
                )
        except NewsClassifierRouteError:
            configured = None
        if configured is None:
            # invalid single-model config: record the typed outage through
            # the shared investigation path (no transport), refund, stop.
            if config_path is not None:
                run_news_investigation(
                    store=store,
                    episode_id=item["episode_id"],
                    candidate_id=item["candidate_id"],
                    purpose=item["purpose"],
                    evidence=json.loads(item["evidence_json"]),
                    prompt_policy_digest=item["prompt_policy_digest"],
                    model_contract_digest=item["model_contract_digest"],
                    configured_model_id=item["model_id"],
                    admitted_catalog=admitted_catalog,
                    classifier_call=classifier_call,
                    now=now,
                    retry_max=0,
                    ops_kernel=ops_kernel,
                    role=role,
                    activation_enabled=False,
                    config_path=config_path,
                )
            store.settle_classifier_work(
                identity,
                "PENDING",
                now=now,
                last_exit="CONFIG_INVALID",
                refund_attempt=True,
            )
            report["config_invalid"] = True
            break
        evidence = json.loads(item["evidence_json"])
        if item["model_id"] != configured:
            store.settle_classifier_work(
                identity, "STALE", now=now, last_exit="ROUTE_CHANGED"
            )
            report["stale"] += 1
            continue
        if item["prompt_policy_digest"] != policy_digest:
            # old-policy frozen evidence must never mix with the current
            # prompt: no transport; the pulse re-freezes under the current
            # policy identity.
            store.settle_classifier_work(
                identity, "STALE", now=now, last_exit="POLICY_CHANGED"
            )
            report["stale"] += 1
            continue
        outcome = run_news_investigation(
            store=store,
            episode_id=item["episode_id"],
            candidate_id=item["candidate_id"],
            purpose=item["purpose"],
            evidence=evidence,
            prompt_policy_digest=item["prompt_policy_digest"],
            model_contract_digest=item["model_contract_digest"],
            configured_model_id=configured,
            admitted_catalog=admitted_catalog,
            classifier_call=classifier_call,
            now=now,
            retry_max=0,
            ops_kernel=ops_kernel,
            role=role,
            activation_enabled=False,
            expected_input_evidence_digest=item["input_evidence_digest"],
        )
        if outcome.get("status") == "OK":
            store.settle_classifier_work(identity, "DONE", now=now, last_exit="OK")
            report["settled_ok"] += 1
            continue
        exit_code = outcome.get("exit")
        if exit_code == "INPUT_DIGEST_MISMATCH":
            # frozen evidence no longer matches its digest: integrity
            # broken, never call a model over it.
            store.settle_classifier_work(
                identity, "STALE", now=now, last_exit=exit_code
            )
            report["stale"] += 1
            continue
        if exit_code in _DEFERRED_STOP_EXITS:
            store.settle_classifier_work(
                identity, "PENDING", now=now, last_exit=exit_code, refund_attempt=True
            )
            report["stopped_budget"] = exit_code == "MODEL_BUDGET_EXCEEDED"
            report["config_invalid"] = (
                report["config_invalid"] or exit_code == "CONFIG_INVALID"
            )
            break
        terminal = item["attempts"] >= attempt_max
        store.settle_classifier_work(
            identity,
            "EXHAUSTED" if terminal else "PENDING",
            now=now,
            last_exit=exit_code,
        )
        report["exhausted" if terminal else "released"] += 1
    return report


def _atomic_receipt_writer(directory):
    directory = Path(directory)

    def write(receipt):
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        name = f"routing-receipt-{receipt['output_digest'][:16]}.json"
        fd, tmp_name = tempfile.mkstemp(prefix=".receipt.", dir=directory)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(receipt, handle, ensure_ascii=False, sort_keys=True)
            os.replace(tmp_name, directory / name)
        finally:
            if Path(tmp_name).exists():
                os.unlink(tmp_name)

    return write


def main(argv=None, *, glm_transport):
    from tokmo.programs.news_classifier_executor import (
        build_glm_classifier_call,
        build_route_authority_reader,
        read_zai_credential_file,
    )
    from tokmo.records.news_episode_store import NewsEpisodeStore
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.ops_kernel import ProducerRole

    parser = argparse.ArgumentParser(
        description="Drain the durable NEWS classifier work queue once."
    )
    parser.add_argument("--root", required=True)
    parser.add_argument("--now", required=True, help="aware ISO-8601 judgment instant")
    parser.add_argument("--routing-state", required=True)
    parser.add_argument("--quota-latest", required=True)
    parser.add_argument("--timeout", type=float, default=WORKER_CALL_TIMEOUT_SECONDS)
    parser.add_argument("--max-items", type=int, default=WORKER_MAX_ITEMS)
    parser.add_argument("--lease-owner", default="news-classifier-worker")
    parser.add_argument(
        "--glm-env-file",
        default=None,
        help="owner-checked private env file holding "
        "GLM_API_KEY/ZAI_API_KEY (default "
        "~/.hermes/.env); the launcher's fallback "
        "credential source",
    )
    arguments = parser.parse_args(argv)
    try:
        now = datetime.fromisoformat(arguments.now.replace("Z", "+00:00"))
    except ValueError:
        parser.error("--now must be ISO-8601")
    if now.tzinfo is None or now.utcoffset() is None:
        parser.error("--now must be timezone-aware")
    if not arguments.timeout > 0:
        parser.error("--timeout must be positive")
    root = Path(arguments.root)
    store = NewsEpisodeStore(root / "stores" / "news_episodes.sqlite3")
    ops_kernel = OpsKernelStore(
        root / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    authority = build_route_authority_reader(
        routing_state_path=Path(arguments.routing_state),
        quota_latest_path=Path(arguments.quota_latest),
        now=now,
    )
    env_file = (
        Path(arguments.glm_env_file)
        if arguments.glm_env_file is not None
        else Path.home() / ".hermes" / ".env"
    )

    def _production_glm_runner(prompt_text, timeout_seconds):
        # the key is resolved per call inside the process (fail-closed
        # like the fixed-plan launcher) and rides only the adapter's
        # Authorization header — never argv, never disk
        if glm_transport is None:
            raise NewsClassifierRouteError("GLM transport is not injected")
        return glm_transport(
            prompt_text,
            timeout_seconds=timeout_seconds,
            api_key=read_zai_credential_file(env_file),
        )

    classifier_call = build_glm_classifier_call(
        glm_runner=_production_glm_runner,
        authority_reader=authority,
        receipt_sink=_atomic_receipt_writer(root / "news" / "classifier_receipts"),
        timeout_seconds=arguments.timeout,
        work_dir=root / "news" / "classifier_work",
        clock=lambda: now,
    )
    lease_seconds = max(WORKER_LEASE_SECONDS, int(arguments.timeout) + 300)
    report = run_classifier_worker(
        store=store,
        ops_kernel=ops_kernel,
        role=ProducerRole.HERMES,
        classifier_call=classifier_call,
        config_path=root / "news_classifier_model.json",
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        now=now,
        lease_owner=arguments.lease_owner,
        lease_seconds=lease_seconds,
        max_items=arguments.max_items,
    )
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0
