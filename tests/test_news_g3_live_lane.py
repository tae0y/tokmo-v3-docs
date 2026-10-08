"""NEWS G3 live-lane RED suite (2026-08-21 follow-up direction).

Covers (A) the durable bounded asynchronous Spark classifier path — pulse
enqueues and stays nonblocking inside its 40-second wrapper, a separately
invoked worker owns the exact-route `glm-5.3-flash` / `zai`
executor with route-authority preflight, strict news-classifier-output-v1
and redaction validation, and typed deferred failure states — and (B) the
recurring `delivery_mode=TELEGRAM_LIVE` lane: durable predecessor canary
proof that a gate rewrite alone cannot forge, canonical outbox/sender
reuse, cutoff/backlog exclusion, morning hold, same-cause dedupe, durable
delivery-only trips and manual rollback, and a bounded activation window.
External network, Telegram, and model transport are all zero: every
transport is a production-shaped fake injected through the real seams.
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from tokmo.programs.news_event_admission import (
    admit_news_event,
    AdmissionInput,
    run_news_event_admission,
)
from tokmo.programs.news_investigation import run_news_investigation
from tokmo.records.news_episode_store import (
    NewsEpisodeStore,
    NewsEpisodeStoreError,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NewsClassifierFailure,
    NewsClassifierRequestV1,
)
from tokmo.values.news_config import (
    NEWS_DAILY_MODEL_CALL_CAP,
    PRODUCER_CANDIDATE_ATTEMPT_MAX,
)
from tokmo.values.ops_kernel import ProducerRole, ReplayResult
from tokmo.values.session_gate import Market

UTC = timezone.utc
KST = ZoneInfo("Asia/Seoul")
NOW = datetime(2026, 8, 21, 9, 0, tzinfo=UTC)  # 18:00 KST — outside hold
LIVE_CUTOFF = NOW - timedelta(minutes=30)
LIVE_WINDOW_END = LIVE_CUTOFF + timedelta(hours=24)
LIVE_ID = "news-g3-live-20260821"
PRED_ID = "news-g3-canary-20260821"
CANARY_CUTOFF = NOW - timedelta(hours=3)
CANARY_WINDOW_END = CANARY_CUTOFF + timedelta(hours=24)
CATALOG = {DEFAULT_NEWS_CLASSIFIER_MODEL_ID: "zai"}
# The current production prompt-policy identity (SDS 2026-09-01 §H.1-2):
# worker admission settles any other frozen policy STALE/POLICY_CHANGED.
from tokmo.programs.news_production import (  # noqa: E402
    _PROMPT_POLICY_DIGEST as POLICY_DIGEST,
)
CONTRACT_DIGEST = "b" * 64
EVIDENCE = {"candidate": "삼성전자", "story_keys": "d-0,d-1",
            "stories": "3", "sources": "2"}


def _stores(tmp_path):
    news = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(tmp_path / "ops" / "ops_kernel.sqlite3",
                         stable_phase_ids=frozenset())
    return news, ops


def _write_gate(root, payload, mode=0o600):
    path = Path(root) / "news_activation.json"
    path.write_text(payload if isinstance(payload, str)
                    else json.dumps(payload))
    os.chmod(path, mode)
    return path


def _live_body(**extra):
    body = {
        "enabled": True, "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_LIVE",
        "activation_id": LIVE_ID,
        "predecessor_activation_id": PRED_ID,
        "cutoff": LIVE_CUTOFF.isoformat(),
        "window_end": LIVE_WINDOW_END.isoformat(),
        "approved_by": "woong",
    }
    body.update(extra)
    return {key: value for key, value in body.items() if value is not ...}


def _canary_body(**extra):
    body = {
        "enabled": True, "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_CANARY",
        "activation_id": PRED_ID,
        "cutoff": CANARY_CUTOFF.isoformat(),
        "window_end": CANARY_WINDOW_END.isoformat(),
        "telegram_attempt_cap": 1,
        "approved_by": "woong",
    }
    body.update(extra)
    return {key: value for key, value in body.items() if value is not ...}


def _seed_predecessor(news, *, outcome="SENT", proof=True,
                      activation_id=PRED_ID):
    earlier = NOW - timedelta(hours=2)
    news.reserve_canary_attempt(activation_id, "nal-pred", now=earlier)
    news.settle_canary_attempt(activation_id, outcome, now=earlier)
    if proof:
        news.record_canary_slot_proof(
            activation_id,
            ("2026-08-21T07:30:00+00:00", "2026-08-21T08:00:00+00:00"),
            1, now=NOW - timedelta(hours=1))


def _live_delivery(activation_id=LIVE_ID, predecessor=PRED_ID,
                   cutoff=LIVE_CUTOFF, window_end=LIVE_WINDOW_END):
    from tokmo.programs.pulse_production_deps import NewsDeliveryV1
    return NewsDeliveryV1("TELEGRAM_LIVE", activation_id, cutoff, window_end,
                          None, predecessor)


def _draft(episode_id, revision_no, *, observed_at, body="본문"):
    return {"episode_id": episode_id, "revision_no": revision_no,
            "revision_kind": "INITIAL" if revision_no == 1
            else "MATERIAL_UPDATE",
            "body_text": body, "portfolio_context_digest": "0" * 64,
            "confidence": 50, "material_change_kind":
                "FIRST_ADMISSION" if revision_no == 1 else "SCALE_CHANGE",
            "material_change_observed_at": observed_at, "links": ()}


def _admit_pending_telegram(news, ops, *, term="라이브사건", revision_no=1,
                            observed_at=None, now=NOW, body="본문"):
    observed = observed_at or (NOW - timedelta(minutes=5))
    episode = news.upsert(term, (), now)
    return episode.episode_id, run_news_event_admission(
        store=news, ops_kernel=ops, role=ProducerRole.HERMES,
        draft=_draft(episode.episode_id, revision_no, observed_at=observed,
                     body=body),
        activation_enabled=True, telegram_cutoff=LIVE_CUTOFF, now=now)


def _sender_recorder(calls, provider_message_id="777"):
    def sender(text, reply_markup=None):
        calls.append((text, reply_markup))
        return provider_message_id
    return sender


def _run_live(news, ops, sender, *, now=NOW, delivery=None, body_guard=None):
    from tokmo.programs.news_alert_delivery import run_live_alert_delivery
    kwargs = {}
    if body_guard is not None:
        kwargs["body_guard"] = body_guard
    return run_live_alert_delivery(
        ops_kernel=ops, role=ProducerRole.HERMES, ledger=news,
        delivery=delivery if delivery is not None else _live_delivery(),
        telegram_send=sender, keyboard_resolver=lambda alert_id: None,
        delivery_binder=news.bind_delivery_by_ops_alert, now=now, **kwargs)


def _request(evidence=None, *, model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
             provider="zai"):
    import hashlib
    evidence = EVIDENCE if evidence is None else evidence
    encoded = json.dumps(evidence, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=False).encode()
    return NewsClassifierRequestV1(
        "EVENT_CLASS", evidence, hashlib.sha256(encoded).hexdigest(),
        POLICY_DIGEST, "news-classifier-output-v1", provider, model_id,
        CONTRACT_DIGEST)


def _investigate(news, ops, *, classifier_call=None, async_queue=None,
                 candidate_id="ndc-cand-1", now=NOW, evidence=None,
                 config_path=None, expected_digest=None):
    def _never(request):
        raise AssertionError("classifier transport must not start in pulse")
    kwargs = {}
    if async_queue is not None:
        kwargs["async_queue"] = async_queue
    if expected_digest is not None:
        kwargs["expected_input_evidence_digest"] = expected_digest
    return run_news_investigation(
        store=news, episode_id=None, candidate_id=candidate_id,
        purpose="EVENT_CLASS",
        evidence=EVIDENCE if evidence is None else evidence,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog=CATALOG,
        classifier_call=classifier_call if classifier_call is not None
        else _never,
        now=now, retry_max=0, ops_kernel=ops, role=ProducerRole.HERMES,
        activation_enabled=False, config_path=config_path, **kwargs)


def _call_rows(news):
    return news.connection.execute(
        "SELECT state,exit,classification_identity FROM classifier_calls"
        " ORDER BY requested_at").fetchall()


def _ok_output(request, **extra):
    payload = {"provider": request.provider, "model_id": request.model_id,
               "event_class": "ORDINARY", "citations": ["candidate"]}
    payload.update(extra)
    return payload


def _ok_classifier(calls=None, event_class="ORDINARY"):
    def call(request):
        if calls is not None:
            calls.append(request)
        return json.dumps({
            "provider": request.provider, "model_id": request.model_id,
            "citations": [], "event_class": event_class})
    return call


def _worker(news, ops, *, classifier_call, now=NOW, config_path=None,
            max_items=2, attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
            lease_seconds=900):
    from tokmo.programs.news_classifier_worker import run_classifier_worker
    return run_classifier_worker(
        store=news, ops_kernel=ops, role=ProducerRole.HERMES,
        classifier_call=classifier_call,
        config_path=config_path, admitted_catalog=CATALOG, now=now,
        lease_owner="worker-test", lease_seconds=lease_seconds,
        max_items=max_items, attempt_max=attempt_max)


def _quota_payload(*, observed_at=None, ok=True, remaining=59.0,
                   five_hours=None, weekly=None):
    if five_hours is None and weekly is None:
        five_hours = weekly = remaining
    return {"schema": "model-quota-latest-v1",
            "observed_at": (observed_at or (NOW - timedelta(minutes=5)))
            .isoformat(),
            "providers": {"zai": {
                "name": "GLM / Z.AI", "ok": ok,
                "windows": [
                    {"label": "5시간", "remaining": five_hours},
                    {"label": "주간", "remaining": weekly},
                    {"label": "월간 MCP", "remaining": 90.0}]}}}


def _authority_paths(tmp_path, *, quota=None, write_routing=True,
                     write_quota=True):
    routing = tmp_path / "MODEL_ROUTING_STATE.md"
    latest = tmp_path / "model-quota-latest.json"
    if write_routing:
        routing.write_text(
            "# Model Routing State\n"
            "- quota authority: `model-quota-latest.json`\n"
            "- invocation freshness: 45분\n"
            f"- direct Z.AI GLM route: {DEFAULT_NEWS_CLASSIFIER_MODEL_ID}, fallback 0\n")
    if write_quota:
        latest.write_text(json.dumps(quota if quota is not None
                                     else _quota_payload()))
    return routing, latest


def _authority(tmp_path, **kwargs):
    from tokmo.programs.news_classifier_executor import (
        build_route_authority_reader,
    )
    tmp_path.mkdir(parents=True, exist_ok=True)
    routing, latest = _authority_paths(tmp_path, **kwargs)
    return build_route_authority_reader(
        routing_state_path=routing, quota_latest_path=latest, now=NOW)


def _executor(tmp_path, runner, *, receipts=None, timeout_seconds=600):
    from tokmo.programs.news_classifier_executor import (
        build_glm_classifier_call,
    )
    sink = receipts.append if receipts is not None else (lambda receipt: None)
    ticks = iter(range(1_000_000))
    return build_glm_classifier_call(
        glm_runner=runner, authority_reader=_authority(tmp_path),
        receipt_sink=sink, timeout_seconds=timeout_seconds,
        work_dir=tmp_path / "work",
        clock=lambda: NOW + timedelta(seconds=next(ticks)))


def _fake_glm_runner(payload, calls=None):
    def runner(prompt_text, timeout_seconds):
        if calls is not None:
            calls.append({"prompt": prompt_text, "timeout": timeout_seconds})
        if payload is None:
            return None
        return payload if isinstance(payload, str) else json.dumps(payload)
    return runner


# =============================================================== A. queue seam


def test_producer_deps_carry_classifier_queue_field():
    import dataclasses
    from tokmo.programs.news_production import NewsProducerDepsV1
    fields = {field.name: field for field in
              dataclasses.fields(NewsProducerDepsV1)}
    assert "classifier_queue" in fields
    assert fields["classifier_queue"].default is None


def test_async_queue_defers_without_call_rows_or_outages(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    assert outcome["status"] == "CLASSIFICATION_DEFERRED"
    assert outcome["exit"] == "ASYNC_PENDING"
    assert _call_rows(news) == []
    assert news.open_outages() == ()
    work = news.classifier_work(outcome["classification_identity"])
    assert work is not None
    assert work["state"] == "PENDING"
    assert work["model_id"] == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    assert work["provider"] == "zai"
    assert json.loads(work["evidence_json"]) == EVIDENCE
    assert work["input_evidence_digest"] == _request().input_evidence_digest
    assert work["candidate_id"] == "ndc-cand-1"


def test_async_defer_does_not_consume_daily_cap(tmp_path):
    news, ops = _stores(tmp_path)
    _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    assert news.classifier_calls_started_on(NOW.date()) == 0


def test_async_enqueue_is_idempotent(tmp_path):
    news, ops = _stores(tmp_path)
    first = _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    second = _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    assert second["exit"] == "ASYNC_PENDING"
    rows = news.connection.execute(
        "SELECT count(*) FROM classifier_work_queue").fetchone()[0]
    assert rows == 1
    work = news.classifier_work(first["classification_identity"])
    assert work["attempts"] == 0


def test_settled_replay_wins_over_queue(tmp_path):
    news, ops = _stores(tmp_path)
    _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    report = _worker(news, ops, classifier_call=_ok_classifier())
    assert report["settled_ok"] == 1
    replayed = _investigate(news, ops,
                            async_queue=news.enqueue_classifier_work)
    assert replayed["status"] == "OK"
    assert replayed["event_class"] == "ORDINARY"


def test_config_invalid_still_fails_closed_before_queue(tmp_path):
    news, ops = _stores(tmp_path)
    config = tmp_path / "news_classifier_model.json"
    config.write_text("{broken")
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work,
                           config_path=config)
    assert outcome == {"status": "CLASSIFICATION_DEFERRED",
                       "exit": "CONFIG_INVALID",
                       "classification_identity": None}
    assert news.connection.execute(
        "SELECT count(*) FROM classifier_work_queue").fetchone()[0] == 0


# ============================================================ B. queue ledger


def test_lease_is_single_owner(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    first = news.lease_classifier_work(
        "owner-a", now=NOW, lease_seconds=900, limit=2,
        attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX)
    assert len(first) == 1 and first[0]["classification_identity"] == identity
    assert news.classifier_work(identity)["state"] == "LEASED"
    assert news.classifier_work(identity)["attempts"] == 1
    second = news.lease_classifier_work(
        "owner-b", now=NOW + timedelta(seconds=1), lease_seconds=900, limit=2,
        attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX)
    assert second == ()


def test_expired_lease_releases_and_attempt_cap_exhausts(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    later = NOW
    for _ in range(PRODUCER_CANDIDATE_ATTEMPT_MAX):
        leased = news.lease_classifier_work(
            "owner", now=later, lease_seconds=60, limit=2,
            attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX)
        assert len(leased) == 1
        later += timedelta(seconds=120)  # lease expires, worker crashed
    exhausted = news.lease_classifier_work(
        "owner", now=later, lease_seconds=60, limit=2,
        attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX)
    assert exhausted == ()
    assert news.classifier_work(identity)["state"] == "EXHAUSTED"


def test_settle_work_transitions(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    news.lease_classifier_work("owner", now=NOW, lease_seconds=900, limit=1,
                               attempt_max=3)
    news.settle_classifier_work(identity, "PENDING", now=NOW,
                                last_exit="TIMEOUT", refund_attempt=True)
    work = news.classifier_work(identity)
    assert work["state"] == "PENDING" and work["attempts"] == 0
    assert work["last_exit"] == "TIMEOUT"
    news.lease_classifier_work("owner", now=NOW, lease_seconds=900, limit=1,
                               attempt_max=3)
    news.settle_classifier_work(identity, "DONE", now=NOW, last_exit="OK")
    assert news.classifier_work(identity)["state"] == "DONE"
    with pytest.raises(NewsEpisodeStoreError):
        news.settle_classifier_work(identity, "PENDING", now=NOW)


def test_reconcile_skips_live_leased_started_calls(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    news.lease_classifier_work("owner", now=NOW, lease_seconds=900, limit=1,
                               attempt_max=3)
    call = {"call_id": "ncl-live-1", "classification_identity": identity,
            "attempt_no": 1, "purpose": "EVENT_CLASS",
            "candidate_id": "ndc-cand-1", "episode_id": None,
            "provider": "zai",
            "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
            "model_contract_digest": CONTRACT_DIGEST,
            "prompt_policy_digest": POLICY_DIGEST,
            "input_evidence_digest": _request().input_evidence_digest,
            "output_schema_id": "news-classifier-output-v1"}
    news.plan_classifier_call(call, now=NOW)
    news.start_classifier_call("ncl-live-1", now=NOW)
    assert news.reconcile_classifier_calls(now=NOW + timedelta(seconds=60)) \
        == 0
    assert news.reconcile_classifier_calls(
        now=NOW + timedelta(seconds=1200)) == 1  # lease expired: crash


def test_settle_classifier_call_reports_lost_race(tmp_path):
    news, ops = _stores(tmp_path)
    call = {"call_id": "ncl-race-1", "classification_identity": "c" * 64,
            "attempt_no": 1, "purpose": "EVENT_CLASS",
            "candidate_id": "ndc-cand-1", "episode_id": None,
            "provider": "zai",
            "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
            "model_contract_digest": CONTRACT_DIGEST,
            "prompt_policy_digest": POLICY_DIGEST,
            "input_evidence_digest": "d" * 64,
            "output_schema_id": "news-classifier-output-v1"}
    news.plan_classifier_call(call, now=NOW)
    news.start_classifier_call("ncl-race-1", now=NOW)
    news.reconcile_classifier_calls(now=NOW)  # no lease: goes UNKNOWN
    assert news.settle_classifier_call(
        "ncl-race-1", "OK", "{}", now=NOW) == 0


# ================================================================= C. worker


def test_worker_settles_ok_and_marks_done(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    calls = []
    report = _worker(news, ops, classifier_call=_ok_classifier(calls))
    assert report["settled_ok"] == 1 and len(calls) == 1
    assert news.classifier_work(identity)["state"] == "DONE"
    settled = news.find_settled_classification(identity)
    assert settled is not None
    assert settled["model_id"] == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    assert news.classifier_calls_started_on(NOW.date()) == 1


def test_worker_stale_route_item_never_calls_model(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    news.connection.execute(
        "UPDATE classifier_work_queue SET model_id='ghost-model'"
        " WHERE classification_identity=?", (identity,))
    news.connection.commit()

    def _never(request):
        raise AssertionError("stale route must not start transport")
    report = _worker(news, ops, classifier_call=_never)
    assert report["stale"] == 1
    assert news.classifier_work(identity)["state"] == "STALE"
    assert _call_rows(news) == []


def test_worker_stale_policy_item_never_calls_model(tmp_path):
    """J-11 (SDS §H.1-2): an item frozen under an older prompt policy must
    never mix with the current prompt — the worker settles it STALE with
    POLICY_CHANGED and starts zero transport; the pulse re-freezes under the
    current policy identity."""
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    news.connection.execute(
        "UPDATE classifier_work_queue SET prompt_policy_digest=?"
        " WHERE classification_identity=?", ("0" * 64, identity))
    news.connection.commit()

    def _never(request):
        raise AssertionError("stale policy must not start transport")
    report = _worker(news, ops, classifier_call=_never)
    assert report["stale"] == 1
    work = news.classifier_work(identity)
    assert work["state"] == "STALE"
    assert work["last_exit"] == "POLICY_CHANGED"
    assert _call_rows(news) == []


def test_worker_config_invalid_is_typed_deferral_without_transport(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    config = tmp_path / "news_classifier_model.json"
    config.write_text(json.dumps({"model_id": "gpt-other"}))

    def _never(request):
        raise AssertionError("invalid config must not start transport")
    report = _worker(news, ops, classifier_call=_never, config_path=config)
    assert report["config_invalid"] is True
    work = news.classifier_work(identity)
    assert work["state"] == "PENDING" and work["attempts"] == 0
    assert any(outage.failure_class == "CONFIG_INVALID"
               for outage in news.open_outages())


def test_worker_timeout_releases_then_exhausts(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]

    def timeout_call(request):
        raise TimeoutError("model call timed out")
    for attempt in range(PRODUCER_CANDIDATE_ATTEMPT_MAX):
        report = _worker(news, ops, classifier_call=timeout_call)
    assert news.classifier_work(identity)["state"] == "EXHAUSTED"
    assert news.classifier_work(identity)["last_exit"] == "TIMEOUT"
    exits = [row[1] for row in _call_rows(news)]
    assert exits == ["TIMEOUT"] * PRODUCER_CANDIDATE_ATTEMPT_MAX
    idle = _worker(news, ops, classifier_call=timeout_call)
    assert idle["leased"] == 0


def test_worker_invalid_json_settles_parse_fail(tmp_path):
    news, ops = _stores(tmp_path)
    _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    report = _worker(news, ops, classifier_call=lambda request: "{broken")
    assert report["released"] == 1
    assert [row[1] for row in _call_rows(news)] == ["PARSE_FAIL"]


def test_worker_model_echo_mismatch_settles_model_rejected(tmp_path):
    news, ops = _stores(tmp_path)
    _investigate(news, ops, async_queue=news.enqueue_classifier_work)

    def wrong_echo(request):
        return json.dumps({"provider": request.provider,
                           "model_id": "gpt-other",
                           "event_class": "ORDINARY", "citations": []})
    _worker(news, ops, classifier_call=wrong_echo)
    assert [row[1] for row in _call_rows(news)] == ["MODEL_REJECTED"]
    assert any(outage.failure_class == "MODEL_REJECTED"
               for outage in news.open_outages())


def test_worker_evidence_tamper_is_stale_without_transport(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    news.connection.execute(
        "UPDATE classifier_work_queue SET evidence_json=?"
        " WHERE classification_identity=?",
        (json.dumps({"candidate": "위조"}), identity))
    news.connection.commit()

    def _never(request):
        raise AssertionError("tampered evidence must not start transport")
    report = _worker(news, ops, classifier_call=_never)
    assert report["stale"] == 1
    assert news.classifier_work(identity)["state"] == "STALE"
    assert _call_rows(news) == []


def test_worker_budget_exceeded_refunds_and_stops(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _investigate(news, ops,
                           async_queue=news.enqueue_classifier_work)
    identity = outcome["classification_identity"]
    for index in range(NEWS_DAILY_MODEL_CALL_CAP):
        news.plan_classifier_call(
            {"call_id": f"ncl-burn-{index:04d}",
             "classification_identity": "e" * 64,
             "attempt_no": index + 1, "purpose": "EVENT_CLASS",
             "candidate_id": "ndc-burn", "episode_id": None,
             "provider": "zai",
             "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
             "model_contract_digest": CONTRACT_DIGEST,
             "prompt_policy_digest": POLICY_DIGEST,
             "input_evidence_digest": "f" * 64,
             "output_schema_id": "news-classifier-output-v1"}, now=NOW)

    def _never(request):
        raise AssertionError("exhausted budget must not start transport")
    report = _worker(news, ops, classifier_call=_never)
    assert report["stopped_budget"] is True
    work = news.classifier_work(identity)
    assert work["state"] == "PENDING" and work["attempts"] == 0


# ========================================================== D. Spark executor




















def _fake_wrapper(tmp_path, body):
    script = tmp_path / "fake-completion-safe-cli.sh"
    script.write_text("#!/bin/sh\n" + body)
    os.chmod(script, 0o700)
    return str(script)










def _failing_runner(exit_code="TRANSPORT_FAIL", *, manifest=True,
                    stderr_text=None, diagnostics=None):
    def runner(argv, prompt_text, timeout_seconds):
        argv = list(argv)
        call_dir = Path(argv[argv.index("--cwd") + 1])
        assert call_dir.is_dir()
        if manifest:
            Path(argv[argv.index("--manifest") + 1]).write_text(json.dumps(
                {"schema_version": 2, "status": "FAILED",
                 "child_exit_code": 3, "stderr_bytes": 42}))
        if stderr_text is not None:
            Path(argv[argv.index("--stderr") + 1]).write_text(stderr_text)
        failure = NewsClassifierFailure(exit_code)
        if diagnostics is not None:
            failure.failure_diagnostics = diagnostics
        raise failure
    return runner










def test_route_authority_fail_closed_matrix(tmp_path):
    from tokmo.programs.news_classifier_executor import (
        build_route_authority_reader,
    )
    valid = _authority(tmp_path / "ok")(_request())
    assert valid["quota_latest"]["observed_at"]

    def exit_code(reader):
        with pytest.raises(NewsClassifierFailure) as failure:
            reader(_request())
        return failure.value.exit_code
    assert exit_code(_authority(tmp_path / "a", write_quota=False)) \
        == "MODEL_UNAVAILABLE"
    assert exit_code(_authority(tmp_path / "b", write_routing=False)) \
        == "MODEL_UNAVAILABLE"
    assert exit_code(_authority(tmp_path / "c", quota=_quota_payload(
        observed_at=NOW - timedelta(minutes=50)))) == "MODEL_UNAVAILABLE"
    assert exit_code(_authority(tmp_path / "d", quota=_quota_payload(
        ok=False))) == "MODEL_UNAVAILABLE"
    assert exit_code(_authority(tmp_path / "e", quota=_quota_payload(
        remaining=0.0))) == "QUOTA_EXCEEDED"
    assert exit_code(_authority(tmp_path / "f", quota={
        "schema": "other", "observed_at": NOW.isoformat(),
        "providers": {}})) == "MODEL_UNAVAILABLE"


def test_output_contract_validator_strictness():
    from tokmo.values.news_classifier import validate_classifier_output_v1
    request = _request()

    def error_for(output):
        with pytest.raises(ValueError) as failure:
            validate_classifier_output_v1(
                output, provider=request.provider,
                model_id=request.model_id, evidence=request.evidence)
        return str(failure.value)
    good = validate_classifier_output_v1(
        _ok_output(request), provider=request.provider,
        model_id=request.model_id, evidence=request.evidence)
    assert json.loads(good)["event_class"] == "ORDINARY"
    assert error_for("not a dict") == "SCHEMA_INVALID"
    assert error_for(_ok_output(request, holdings=[])) == "SCHEMA_INVALID"
    assert error_for(_ok_output(request, event_class="MAYBE")) \
        == "SCHEMA_INVALID"
    assert error_for(_ok_output(request, citations="candidate")) \
        == "SCHEMA_INVALID"
    assert error_for({"provider": request.provider,
                      "model_id": request.model_id}) == "SCHEMA_INVALID"
    assert error_for(_ok_output(request) | {"model_id": "gpt-other"}) \
        == "MODEL_REJECTED"
    assert error_for(_ok_output(request, citations=["ghost"])) \
        == "CITATION_UNRESOLVED"


# ============================================================ E. live gate


def test_gate_valid_live_payload_parses(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate
    _write_gate(tmp_path, _live_body())
    snapshot = read_news_gate(tmp_path)
    activation, producer_mode = snapshot
    assert activation.enabled is True and producer_mode == "SHADOW"
    delivery = snapshot.delivery
    assert delivery.mode == "TELEGRAM_LIVE"
    assert delivery.activation_id == LIVE_ID
    assert delivery.predecessor_activation_id == PRED_ID
    assert delivery.cutoff == LIVE_CUTOFF
    assert delivery.window_end == LIVE_WINDOW_END
    assert delivery.attempt_cap is None
    assert delivery.live_active(NOW) is True
    assert delivery.live_active(LIVE_WINDOW_END) is False


def test_gate_live_fail_closed_matrix(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate
    degenerate = (
        _live_body(predecessor_activation_id=...),
        _live_body(predecessor_activation_id=LIVE_ID),
        _live_body(predecessor_activation_id=".bad"),
        _live_body(telegram_attempt_cap=1),
        _live_body(telegram_attempt_cap=0),
        _live_body(window_end=...),
        _live_body(window_end=(LIVE_CUTOFF
                               + timedelta(days=8)).isoformat()),
        _live_body(window_end=LIVE_CUTOFF.isoformat()),
        _live_body(window_end="2026-08-22T09:00:00"),  # naive
        _live_body(delivery_mode="TELEGRAM_LIVE!"),
        _live_body(producer_mode="LIVE"),
        _live_body(activation_id=...),
        _live_body(cutoff=...),
    )
    for payload in degenerate:
        _write_gate(tmp_path, payload)
        snapshot = read_news_gate(tmp_path)
        assert snapshot.delivery.mode == "OFF", payload
        # producer independence: only the authorized SHADOW token stays on
        expected = "SHADOW" if payload.get("producer_mode") == "SHADOW" \
            and payload.get("enabled") is True \
            and isinstance(payload.get("cutoff"), str) else "OFF"
        assert snapshot[1] == expected, payload


def test_gate_max_live_window_is_seven_days(tmp_path):
    from tokmo.programs.pulse_production_deps import (
        NEWS_LIVE_WINDOW_MAX_SECONDS, read_news_gate,
    )
    assert NEWS_LIVE_WINDOW_MAX_SECONDS == 7 * 24 * 3600
    _write_gate(tmp_path, _live_body(
        window_end=(LIVE_CUTOFF + timedelta(days=7)).isoformat()))
    assert read_news_gate(tmp_path).delivery.mode == "TELEGRAM_LIVE"


def test_gate_canary_payload_still_parses(tmp_path):
    from tokmo.programs.pulse_production_deps import read_news_gate
    _write_gate(tmp_path, {
        "enabled": True, "producer_mode": "SHADOW",
        "delivery_mode": "TELEGRAM_CANARY", "activation_id": PRED_ID,
        "cutoff": LIVE_CUTOFF.isoformat(),
        "window_end": (LIVE_CUTOFF + timedelta(minutes=60)).isoformat(),
        "telegram_attempt_cap": 1, "approved_by": "woong"})
    delivery = read_news_gate(tmp_path).delivery
    assert delivery.mode == "TELEGRAM_CANARY"
    assert delivery.attempt_cap == 1
    assert delivery.predecessor_activation_id is None


# ================================================= F. predecessor authority


def test_live_unauthorized_without_predecessor_evidence(tmp_path):
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    news, ops = _stores(tmp_path)
    assert live_delivery_authorized(news, _live_delivery(), NOW) \
        == (False, "predecessor_unproven")


def test_live_unauthorized_for_failed_or_unknown_canary(tmp_path):
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    for outcome in ("FAILED", "UNKNOWN"):
        news = NewsEpisodeStore(
            tmp_path / outcome / "stores" / "news_episodes.sqlite3")
        _seed_predecessor(news, outcome=outcome, proof=False)
        assert live_delivery_authorized(news, _live_delivery(), NOW) \
            == (False, "predecessor_unproven")


def test_live_unauthorized_without_slot_proof(tmp_path):
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    news, ops = _stores(tmp_path)
    _seed_predecessor(news, proof=False)
    assert live_delivery_authorized(news, _live_delivery(), NOW) \
        == (False, "proof_missing")


def test_live_authorized_with_sent_and_proof(tmp_path):
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    assert live_delivery_authorized(news, _live_delivery(), NOW) \
        == (True, None)
    assert live_delivery_authorized(
        news, _live_delivery(), LIVE_CUTOFF - timedelta(minutes=1)) \
        == (False, "before_cutoff")
    assert live_delivery_authorized(news, _live_delivery(), LIVE_WINDOW_END) \
        == (False, "window_expired")


def test_live_trip_blocks_delivery(tmp_path):
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    assert news.record_live_trip(LIVE_ID, "transport_failed", now=NOW) is True
    assert news.record_live_trip(LIVE_ID, "other", now=NOW) is False
    trip = news.live_trip(LIVE_ID)
    assert trip["reason"] == "transport_failed"  # first trip is durable
    assert live_delivery_authorized(news, _live_delivery(), NOW) \
        == (False, "live_tripped")


def test_slot_proof_requires_sent_canary_and_is_durable(tmp_path):
    news, ops = _stores(tmp_path)
    with pytest.raises(NewsEpisodeStoreError):
        news.record_canary_slot_proof(
            PRED_ID, ("s1", "s2"), 1, now=NOW)
    _seed_predecessor(news, proof=False)
    created = news.record_canary_slot_proof(PRED_ID, ("s1", "s2"), 1, now=NOW)
    assert created is ReplayResult.CREATED
    assert news.record_canary_slot_proof(
        PRED_ID, ("s1", "s2"), 1, now=NOW) is ReplayResult.DUPLICATE
    with pytest.raises(NewsEpisodeStoreError):
        news.record_canary_slot_proof(PRED_ID, ("s3", "s4"), 1, now=NOW)
    with pytest.raises((NewsEpisodeStoreError, sqlite3.IntegrityError)):
        news.record_canary_slot_proof(PRED_ID + "-x", ("s1",), 1, now=NOW)


# ============================================================ G. live runner


def test_live_runner_sends_through_canonical_outbox(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    episode_id, revision_id = _admit_pending_telegram(news, ops)
    calls = []
    result = _run_live(news, ops, _sender_recorder(calls))
    assert result["sent"] == 1 and len(calls) == 1
    assert news.live_trip(LIVE_ID) is None
    row = news.connection.execute(
        "SELECT delivery_state,provider_message_id FROM alert_revisions"
        " WHERE alert_revision_id=?", (revision_id,)).fetchone()
    assert row == ("DELIVERY_BOUND", "777")
    second = _run_live(news, ops, _sender_recorder(calls))
    assert second["claimed"] == 0 and len(calls) == 1


def test_live_runner_claims_nothing_without_authority(tmp_path):
    news, ops = _stores(tmp_path)
    _admit_pending_telegram(news, ops)

    def _never(text, reply_markup=None):
        raise AssertionError("unauthorized live lane must not send")
    result = _run_live(news, ops, _never)
    assert result["off"] == "predecessor_unproven"
    assert result["claimed"] == 0


def test_live_runner_failed_send_trips_delivery_only(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news, ops)

    def broken(text, reply_markup=None):
        raise RuntimeError("transport down")
    result = _run_live(news, ops, broken)
    assert result["failed"] == 1
    assert result["tripped"] == "transport_failed"
    assert news.live_trip(LIVE_ID)["reason"] == "transport_failed"
    after = _run_live(news, ops, broken)
    assert after["off"] == "live_tripped" and after["claimed"] == 0
    # producer/classifier stay safe: admission still lands in SHADOW
    episode = news.upsert("후속사건", (), NOW)
    run_news_event_admission(
        store=news, ops_kernel=ops, role=ProducerRole.HERMES,
        draft=_draft(episode.episode_id, 1, observed_at=NOW),
        activation_enabled=False, telegram_cutoff=None, now=NOW)
    shadow = ops._connect().execute(
        "SELECT count(*) FROM alert_outbox WHERE channel='SHADOW'"
    ).fetchone()[0]
    assert shadow >= 1


def test_live_runner_unknown_send_trips_and_never_requeues(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news, ops)

    def hanging(text, reply_markup=None):
        raise TimeoutError("no settle")
    result = _run_live(news, ops, hanging)
    assert result["unknown"] == 1
    assert result["tripped"] == "transport_unknown"
    unknown = [tuple(row) for row in ops._connect().execute(
        "SELECT status FROM alert_outbox WHERE channel='TELEGRAM'"
    ).fetchall()]
    assert unknown == [("UNKNOWN",)]
    calls = []
    after = _run_live(news, ops, _sender_recorder(calls))
    assert after["off"] == "live_tripped" and calls == []


@pytest.mark.parametrize('code',[429,500,502,503,504])
def test_temporary_http_rejection_does_not_disable_all_future_news(tmp_path,code):
    from urllib.error import HTTPError
    news,ops=_stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news,ops)
    def temporary(text,reply_markup=None):
        raise HTTPError('https://invalid/botSECRET',code,'secret provider body',{},None)
    result=_run_live(news,ops,temporary)
    assert result['failed']==1 and result['tripped'] is None
    assert result['degraded']=='temporary_transport_rejection'
    assert result['failure_reason']==f'provider_failure:HTTPError:{code}'
    assert news.live_trip(LIVE_ID) is None
    stored=ops._connect().execute("SELECT resolution_reason FROM alert_outbox WHERE status='FAILED'").fetchone()[0]
    assert stored==result['failure_reason'] and 'SECRET' not in stored
    after=_run_live(news,ops,lambda *args,**kw:pytest.fail('failed message resend'))
    assert after['claimed']==0 and after['off'] is None


def test_live_runner_redaction_guard_blocks_private_body(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news, ops,
                            body="계좌 노출 /home/tokmo/secret")
    calls = []
    result = _run_live(news, ops, _sender_recorder(calls))
    assert calls == []  # transport never started
    assert result["rejected"] == 1
    assert result["tripped"] == "redaction_failure"
    assert news.live_trip(LIVE_ID)["reason"] == "redaction_failure"
    settled = [tuple(row) for row in ops._connect().execute(
        "SELECT status,resolution_reason FROM alert_outbox"
        " WHERE channel='TELEGRAM'").fetchall()]
    assert settled == [("FAILED", "redaction_rejected")]


def test_live_runner_invariant_failure_trips(tmp_path):
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news, ops)

    def exploding_binder(ops_alert_id, provider_message_id, *, now):
        raise NewsEpisodeStoreError("duplicate binding")
    from tokmo.programs.news_alert_delivery import run_live_alert_delivery
    result = run_live_alert_delivery(
        ops_kernel=ops, role=ProducerRole.HERMES, ledger=news,
        delivery=_live_delivery(), telegram_send=_sender_recorder([]),
        keyboard_resolver=lambda alert_id: None,
        delivery_binder=exploding_binder, now=NOW)
    assert result["tripped"] == "delivery_invariant_failure"
    assert news.live_trip(LIVE_ID)["reason"] == "delivery_invariant_failure"


def test_admission_semantics_preserved_under_live():
    # pre-cutoff backlog is never TELEGRAM; ordinary night events hold;
    # unchanged material state dedupes.
    backlog = admit_news_event(AdmissionInput(
        "nep-1", 1, "ORDINARY", "FIRST_ADMISSION",
        LIVE_CUTOFF - timedelta(minutes=1), LIVE_CUTOFF, False, True,
        admitted_at=NOW))
    assert backlog["status"] == "REJECTED_CUTOFF"
    night = admit_news_event(AdmissionInput(
        "nep-1", 1, "ORDINARY", "FIRST_ADMISSION", NOW, LIVE_CUTOFF, False,
        True, admitted_at=datetime(2026, 8, 21, 14, 30, tzinfo=UTC)))
    assert night["status"] == "HELD_MORNING"  # 23:30 KST
    breaking = admit_news_event(AdmissionInput(
        "nep-1", 1, "CORE_BREAKING", "FIRST_ADMISSION", NOW, LIVE_CUTOFF,
        False, True, admitted_at=datetime(2026, 8, 21, 14, 30, tzinfo=UTC)))
    assert breaking["status"] == "PENDING"  # breaking: 24h immediate
    unchanged = admit_news_event(AdmissionInput(
        "nep-1", 2, "ORDINARY", "SCALE_CHANGE", NOW, LIVE_CUTOFF, False,
        True, admitted_at=NOW, prior_material_change="SCALE_CHANGE"))
    assert unchanged["status"] == "REJECTED_NO_CHANGE"


def test_reconcile_replay_is_idempotent_under_live(tmp_path):
    from tokmo.programs.news_event_admission import reconcile_news_outbox
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _admit_pending_telegram(news, ops)
    again = reconcile_news_outbox(
        store=news, ops_kernel=ops, role=ProducerRole.HERMES,
        activation_enabled=True, telegram_cutoff=LIVE_CUTOFF, now=NOW)
    assert again == 0  # nothing pending: replay-safe
    rows = ops._connect().execute(
        "SELECT count(*) FROM alert_outbox").fetchone()[0]
    assert rows == 1


# =============================================== H. composition & rollback


def _build(root, *, telegram_sender=None):
    from tokmo.programs.pulse_production_deps import (
        build_pulse_production_deps,
    )
    return build_pulse_production_deps(
        root=root, clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW, telegram_sender=telegram_sender)


def test_composition_live_lane_sends_with_fake_sender(tmp_path):
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    episode = news.upsert("컴포지션사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)),
        now=NOW)
    calls = []
    deps = _build(tmp_path, telegram_sender=_sender_recorder(calls))
    deps.news_outbox_reconcile(NOW)
    result = deps.news_delivery_runner(NOW)
    assert result["sent"] == 1 and len(calls) == 1
    assert news.live_trip(LIVE_ID) is None
    telegram = [tuple(row) for row in ops._connect().execute(
        "SELECT status,provider_message_id FROM alert_outbox"
        " WHERE channel='TELEGRAM'").fetchall()]
    assert telegram == [("SENT", "777")]


def test_composition_gate_rewrite_cannot_forge_predecessor(tmp_path):
    _write_gate(tmp_path, _live_body(
        predecessor_activation_id="forged-canary-id"))
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)  # real proof exists only for PRED_ID
    episode = news.upsert("위조사건", (), NOW)
    news.admit_alert_revision(
        _draft(episode.episode_id, 1, observed_at=NOW - timedelta(minutes=5)),
        now=NOW)

    def _never(text, reply_markup=None):
        raise AssertionError("forged gate must never send")
    deps = _build(tmp_path, telegram_sender=_never)
    deps.news_outbox_reconcile(NOW)
    result = deps.news_delivery_runner(NOW)
    assert result["off"] == "predecessor_unproven"
    channels = [tuple(row) for row in ops._connect().execute(
        "SELECT channel FROM alert_outbox").fetchall()]
    assert channels == [("SHADOW",)]  # admission fell closed to SHADOW


def test_composition_live_without_sender_is_off(tmp_path):
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    deps = _build(tmp_path, telegram_sender=None)
    from tokmo.programs.pulse_production_deps import NEWS_DELIVERY_OFF
    result = deps.news_delivery_runner(NOW)
    assert result.get("sent", 0) == 0 and result.get("claimed", 0) == 0


def test_composition_wires_classifier_queue_not_model(tmp_path):
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    deps = _build(tmp_path, telegram_sender=_sender_recorder([]))
    producer = deps.news_producer_deps
    assert producer is not None
    assert producer.classifier_queue is not None
    with pytest.raises(NewsClassifierFailure):
        producer.classifier_call(_request())  # backstop stays fail-closed
    state = producer.classifier_queue(
        {"classification_identity": "1" * 64, "purpose": "EVENT_CLASS",
         "candidate_id": "ndc-wired", "episode_id": None,
         "provider": "zai",
         "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
         "model_contract_digest": CONTRACT_DIGEST,
         "prompt_policy_digest": POLICY_DIGEST,
         "input_evidence_digest": "2" * 64,
         "output_schema_id": "news-classifier-output-v1",
         "evidence_json": json.dumps(EVIDENCE)}, now=NOW)
    assert state == "PENDING"
    row = news.connection.execute(
        "SELECT state FROM classifier_work_queue").fetchone()
    assert row == ("PENDING",)


def test_manual_rollback_is_delivery_only(tmp_path):
    from tokmo.programs.news_live_control import record_manual_live_rollback
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    report = record_manual_live_rollback(
        root=tmp_path, activation_id=LIVE_ID, reason="manual_rollback",
        now=NOW, store=news)
    assert report["tripped"] is True
    assert news.live_trip(LIVE_ID)["reason"] == "manual_rollback"

    def _never(text, reply_markup=None):
        raise AssertionError("rolled-back lane must not send")
    result = _run_live(news, ops, _never)
    assert result["off"] == "live_tripped"
    # gate file untouched: producer stays SHADOW-enabled
    from tokmo.programs.pulse_production_deps import read_news_gate
    snapshot = read_news_gate(tmp_path)
    assert snapshot[1] == "SHADOW"
    assert snapshot.delivery.mode == "TELEGRAM_LIVE"


def _sent_telegram_row(ops, alert_id, *, created_at, sent_at,
                       created_not_before=None):
    """One canonical-outbox NEWS Telegram transport start settled SENT."""
    from tokmo.values.ops_kernel import (
        AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus,
    )
    outbox = AlertOutboxV1(
        alert_id, AlertOrigin.NEWS_EVENT, None, None, None, None, 0,
        f"nep-{alert_id}", f"news:{alert_id}:1", AlertChannel.TELEGRAM,
        "ORDINARY", ("본문",), created_at, status=AlertStatus.PENDING)
    ops.record_news_alert_outbox(ProducerRole.HERMES, outbox,
                                 activation_enabled=True)
    ops.claim_pending_telegram(
        ProducerRole.HERMES, sent_at, origins=(AlertOrigin.NEWS_EVENT.value,),
        created_not_before=created_not_before)
    ops.mark_sent(ProducerRole.HERMES, alert_id, sent_at, "777")


def _complete_slots(tmp_path, offsets=(90, 60)):
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    receipts = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: NOW)
    for index, offset in enumerate(offsets):
        slot = NOW - timedelta(minutes=offset)
        receipts.begin_tick("pulse-60s-1", slot, index + 1,
                            observed_start=slot, lag_seconds=0.0,
                            max_lag_seconds=30)
        receipts.finish_tick("pulse-60s-1", slot, status="SAVED",
                             reason=None, stages=(), wire_counts={},
                             finished_at=slot)


def _seed_bound_canary(news, ops, *, outbox_alert_id="nal-canary",
                       ledger_alert_id="nal-canary"):
    """Post-cutoff canary: outbox row + durable SENT ledger attempt."""
    reserved_at = CANARY_CUTOFF + timedelta(minutes=5)
    settled_at = CANARY_CUTOFF + timedelta(minutes=10)
    _sent_telegram_row(ops, outbox_alert_id, created_at=reserved_at,
                       sent_at=settled_at,
                       created_not_before=CANARY_CUTOFF)
    news.reserve_canary_attempt(PRED_ID, ledger_alert_id, now=reserved_at)
    news.settle_canary_attempt(PRED_ID, "SENT", now=settled_at)
    return settled_at


def test_canary_duplicate_proof_recorder(tmp_path):
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    settled_at = NOW - timedelta(hours=2)
    news.reserve_canary_attempt(PRED_ID, "nal-pred",
                                now=settled_at - timedelta(minutes=1))
    news.settle_canary_attempt(PRED_ID, "SENT", now=settled_at)
    # the one canary transport start in the canonical outbox
    from tokmo.values.ops_kernel import (
        AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus,
    )
    outbox = AlertOutboxV1(
        "nal-pred", AlertOrigin.NEWS_EVENT, None, None, None, None, 0,
        "nep-pred", "news:pred:1", AlertChannel.TELEGRAM, "ORDINARY",
        ("본문",), settled_at - timedelta(minutes=2),
        status=AlertStatus.PENDING)
    ops.record_news_alert_outbox(ProducerRole.HERMES, outbox,
                                 activation_enabled=True)
    ops.claim_pending_telegram(ProducerRole.HERMES,
                               settled_at - timedelta(minutes=1),
                               origins=(AlertOrigin.NEWS_EVENT.value,))
    ops.mark_sent(ProducerRole.HERMES, "nal-pred", settled_at, "777")
    receipts = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3",
        clock=lambda: NOW)
    with pytest.raises(NewsLiveControlError):
        record_canary_duplicate_proof(root=tmp_path, activation_id=PRED_ID,
                                      now=NOW, store=news)  # zero slots yet
    for index, offset in enumerate((90, 60)):
        slot = NOW - timedelta(minutes=offset)
        receipts.begin_tick("pulse-60s-1", slot, index + 1,
                            observed_start=slot, lag_seconds=0.0,
                            max_lag_seconds=30)
        receipts.finish_tick("pulse-60s-1", slot, status="SAVED",
                             reason=None, stages=(), wire_counts={},
                             finished_at=slot)
    report = record_canary_duplicate_proof(
        root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
    assert report["transport_starts"] == 1
    assert len(report["slots"]) == 2
    proof = news.canary_slot_proof(PRED_ID)
    assert proof is not None and proof["transport_starts"] == 1
    again = record_canary_duplicate_proof(
        root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
    assert again["replay"] is True


def test_canary_proof_refuses_duplicate_transport_starts(tmp_path):
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    from tokmo.records.pulse_tick_receipts import PulseTickReceiptStore
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    settled_at = NOW - timedelta(hours=2)
    news.reserve_canary_attempt(PRED_ID, "nal-a",
                                now=settled_at - timedelta(minutes=1))
    news.settle_canary_attempt(PRED_ID, "SENT", now=settled_at)
    for index in ("a", "b"):  # two transport starts observed: refuse
        episode_id, _ = _admit_pending_telegram(
            news, ops, term=f"사건{index}", now=NOW)
        ops.claim_pending_telegram(ProducerRole.HERMES, NOW)
    receipts = PulseTickReceiptStore(
        tmp_path / "ops" / "pulse_tick_receipts.sqlite3", clock=lambda: NOW)
    for index, offset in enumerate((90, 60)):
        slot = NOW - timedelta(minutes=offset)
        receipts.begin_tick("pulse-60s-1", slot, index + 1,
                            observed_start=slot, lag_seconds=0.0,
                            max_lag_seconds=30)
        receipts.finish_tick("pulse-60s-1", slot, status="SAVED",
                             reason=None, stages=(), wire_counts={},
                             finished_at=slot)
    with pytest.raises(NewsLiveControlError):
        record_canary_duplicate_proof(root=tmp_path, activation_id=PRED_ID,
                                      now=NOW, store=news)
    assert news.canary_slot_proof(PRED_ID) is None


def test_canary_proof_bounded_to_cutoff_ignores_history(tmp_path):
    """Incident 2026-08-22: 26 historical NEWS transport starts plus one
    pre-cutoff PENDING bond must never veto a valid post-cutoff canary."""
    from tokmo.programs.news_live_control import record_canary_duplicate_proof
    from tokmo.values.ops_kernel import (
        AlertChannel, AlertOrigin, AlertOutboxV1, AlertStatus,
    )
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    historical = CANARY_CUTOFF - timedelta(days=2)
    for index in range(26):
        _sent_telegram_row(
            ops, f"nal-hist-{index:02d}",
            created_at=historical + timedelta(minutes=index),
            sent_at=historical + timedelta(minutes=index, seconds=30))
    ops.record_news_alert_outbox(ProducerRole.HERMES, AlertOutboxV1(
        "nal-bond", AlertOrigin.NEWS_EVENT, None, None, None, None, 0,
        "nep-bond", "news:bond:1", AlertChannel.TELEGRAM, "ORDINARY",
        ("본문",), CANARY_CUTOFF - timedelta(hours=1),
        status=AlertStatus.PENDING), activation_enabled=True)
    _seed_bound_canary(news, ops)
    _complete_slots(tmp_path)
    report = record_canary_duplicate_proof(
        root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
    assert report["transport_starts"] == 1
    assert len(report["slots"]) == 2
    proof = news.canary_slot_proof(PRED_ID)
    assert proof is not None and proof["transport_starts"] == 1
    bond = ops._connect().execute(
        "SELECT status,attempted_at FROM alert_outbox"
        " WHERE alert_id='nal-bond'").fetchone()
    assert tuple(bond) == ("PENDING", None)  # bond untouched, uncounted
    again = record_canary_duplicate_proof(
        root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
    assert again["replay"] is True and again["transport_starts"] == 1


def test_canary_proof_refuses_second_post_cutoff_transport(tmp_path):
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    settled_at = _seed_bound_canary(news, ops)
    _sent_telegram_row(ops, "nal-extra",
                       created_at=settled_at + timedelta(minutes=1),
                       sent_at=settled_at + timedelta(minutes=2))
    _complete_slots(tmp_path)
    with pytest.raises(NewsLiveControlError):
        record_canary_duplicate_proof(root=tmp_path, activation_id=PRED_ID,
                                      now=NOW, store=news)
    assert news.canary_slot_proof(PRED_ID) is None


def test_canary_proof_refuses_foreign_post_cutoff_row(tmp_path):
    # the sole post-cutoff transport start is not the canary attempt's
    # own ops_alert_id row: some other transport started after cutoff
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    _seed_bound_canary(news, ops, outbox_alert_id="nal-other",
                       ledger_alert_id="nal-canary")
    _complete_slots(tmp_path)
    with pytest.raises(NewsLiveControlError):
        record_canary_duplicate_proof(root=tmp_path, activation_id=PRED_ID,
                                      now=NOW, store=news)
    assert news.canary_slot_proof(PRED_ID) is None


def test_canary_proof_requires_bound_canary_gate(tmp_path):
    # a fully provable state, vetoed only by the gate binding
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    news, ops = _stores(tmp_path)
    _seed_bound_canary(news, ops)
    _complete_slots(tmp_path)
    degenerate = (None,                                   # gate absent
                  _canary_body(enabled=False),            # delivery OFF
                  _live_body(),                           # wrong mode
                  _canary_body(activation_id="other-activation-id"),
                  "{broken")                              # malformed
    for payload in degenerate:
        gate_path = tmp_path / "news_activation.json"
        gate_path.unlink(missing_ok=True)
        if payload is not None:
            _write_gate(tmp_path, payload)
        with pytest.raises(NewsLiveControlError):
            record_canary_duplicate_proof(
                root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
        assert news.canary_slot_proof(PRED_ID) is None, payload
    _write_gate(tmp_path, _canary_body())  # bound gate: same state proves
    report = record_canary_duplicate_proof(
        root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
    assert report["transport_starts"] == 1


def test_canary_proof_fails_closed_on_malformed_attempt_created_at(tmp_path):
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    _seed_bound_canary(news, ops)
    _complete_slots(tmp_path)
    for bad in ("not-a-timestamp", "2026-08-21T06:05:00"):  # naive too
        connection = sqlite3.connect(tmp_path / "ops" / "ops_kernel.sqlite3")
        connection.execute(
            "UPDATE alert_outbox SET created_at=?"
            " WHERE alert_id='nal-canary'", (bad,))
        connection.commit()
        connection.close()
        with pytest.raises(NewsLiveControlError):
            record_canary_duplicate_proof(
                root=tmp_path, activation_id=PRED_ID, now=NOW, store=news)
        assert news.canary_slot_proof(PRED_ID) is None, bad


def test_canary_proof_requires_two_completed_slots(tmp_path):
    from tokmo.programs.news_live_control import (
        NewsLiveControlError, record_canary_duplicate_proof,
    )
    _write_gate(tmp_path, _canary_body())
    news, ops = _stores(tmp_path)
    _seed_bound_canary(news, ops)
    _complete_slots(tmp_path, offsets=(60,))  # one slot only
    with pytest.raises(NewsLiveControlError):
        record_canary_duplicate_proof(root=tmp_path, activation_id=PRED_ID,
                                      now=NOW, store=news)
    assert news.canary_slot_proof(PRED_ID) is None


# ============================================================ I. preflight


def test_preflight_receipt_shape_and_counts(tmp_path):
    from tokmo.programs.news_g3_preflight import read_news_preflight
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    _investigate(news, ops, async_queue=news.enqueue_classifier_work)
    routing, latest = _authority_paths(tmp_path)
    receipt = read_news_preflight(
        tmp_path, now=NOW, routing_state_path=routing,
        quota_latest_path=latest)
    classifier = receipt["classifier"]
    assert classifier["configured_model"] == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    assert classifier["provider"] == "zai"
    assert classifier["retry_max"] == 0
    assert classifier["daily_call_cap"] == NEWS_DAILY_MODEL_CALL_CAP
    assert classifier["timeout_seconds"] > 0
    assert receipt["route_authority"]["state"] == "OK"
    assert receipt["work_queue"] == {"PENDING": 1}
    assert receipt["classifier_calls"] == {}
    assert receipt["telegram_transport_starts"] == 0
    assert receipt["gate"]["delivery_mode"] == "TELEGRAM_LIVE"
    assert receipt["gate"]["predecessor_activation_id"] == PRED_ID
    assert receipt["delivery_enabled_now"] is True
    assert receipt["predecessor"]["attempt"]["outcome"] == "SENT"
    assert receipt["predecessor"]["slot_proof"] is not None
    assert receipt["live_trip"] is None


def test_preflight_is_strictly_read_only(tmp_path):
    from tokmo.programs.news_g3_preflight import read_news_preflight
    receipt = read_news_preflight(tmp_path, now=NOW)
    assert receipt["gate"]["delivery_mode"] == "OFF"
    assert receipt["delivery_enabled_now"] is False
    assert receipt["route_authority"] is None
    assert not (tmp_path / "stores").exists()
    assert not (tmp_path / "ops").exists()


def test_preflight_reports_stale_authority(tmp_path):
    from tokmo.programs.news_g3_preflight import read_news_preflight
    routing, latest = _authority_paths(tmp_path, quota=_quota_payload(
        observed_at=NOW - timedelta(hours=2)))
    receipt = read_news_preflight(
        tmp_path, now=NOW, routing_state_path=routing,
        quota_latest_path=latest)
    assert receipt["route_authority"]["state"] == "MODEL_UNAVAILABLE"


# ==================================================== J. golden async path


def test_golden_path_enqueue_work_settle_replay_deliver(tmp_path):
    """Producer tick defers to the queue; the worker (fake codex through the
    real executor) settles; the next tick replays into an episode and the
    live lane delivers through the canonical outbox with a fake sender."""
    import hashlib
    from tokmo.programs.news_production import (
        NewsProducerDepsV1, run_news_production,
    )
    from tokmo.values.news_config import (
        PRODUCER_CANDIDATES_PER_TICK, PRODUCER_CANDIDATE_TTL_SECONDS,
        PRODUCER_INTERVAL_SECONDS, PRODUCER_LEASE_SECONDS,
        PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
    )
    news, ops = _stores(tmp_path)
    _seed_predecessor(news)
    run_id = "ndr-golden"
    news.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (run_id, NOW.isoformat(), NOW.isoformat(), "1" * 64, "2" * 64, 3, 3,
         0, "OK", NOW.isoformat(), NOW.isoformat()))
    evidence = json.dumps(
        {"v": 1, "document_ids": ["골든-0", "골든-1"],
         "latest_observed_at": (NOW - timedelta(minutes=5)).isoformat(),
         "headline": "골든테마 수주 확대 공시"},
        separators=(",", ":"))
    news.connection.execute(
        "INSERT INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        ("ndc-golden", run_id, "WHOLE_CORPUS_FREQUENCY", None, "골든테마",
         3, 3, evidence, "SURFACED", None, NOW.isoformat(), NOW.isoformat()))
    news.connection.commit()

    def _never(request):
        raise AssertionError("pulse must never start model transport")

    def reader(window_from):
        raise AssertionError("discovery not due in this test")
    delivery = _live_delivery()
    from tokmo.programs.news_alert_delivery import live_delivery_authorized
    deps = NewsProducerDepsV1(
        episode_store=news, ops_kernel=ops, role=ProducerRole.HERMES,
        documents_reader=reader, holding_terms=lambda: frozenset(),
        classifier_call=_never,
        classifier_config_path=tmp_path / "news_classifier_model.json",
        admitted_catalog=CATALOG, gate_cutoff=LIVE_CUTOFF,
        slot_seconds=PRODUCER_INTERVAL_SECONDS,
        candidates_per_tick=PRODUCER_CANDIDATES_PER_TICK,
        lease_seconds=PRODUCER_LEASE_SECONDS,
        backoff_cap_seconds=PRODUCER_RECOVERY_BACKOFF_CAP_SECONDS,
        ttl_seconds=PRODUCER_CANDIDATE_TTL_SECONDS,
        attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
        delivery_activation=lambda now: live_delivery_authorized(
            news, delivery, now)[0],
        classifier_queue=news.enqueue_classifier_work)
    # mark the discovery slot done so the tick only drains
    slot = (int(NOW.timestamp()) // PRODUCER_INTERVAL_SECONDS) \
        * PRODUCER_INTERVAL_SECONDS
    window_to = datetime.fromtimestamp(slot, UTC)
    news.connection.execute(
        "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        ("ndr-slotmark", window_to.isoformat(), window_to.isoformat(),
         "0" * 64, "0" * 64, 0, 0, 0, "OK", NOW.isoformat(),
         NOW.isoformat()))
    news.connection.commit()

    first = run_news_production(producer_deps=deps, now=NOW)
    assert first.drained == 1 and first.candidate_failures == 0
    pending = news.connection.execute(
        "SELECT state FROM classifier_work_queue").fetchall()
    assert pending == [("PENDING",)]
    assert _call_rows(news) == []  # pulse made zero model attempts

    runner_calls = []

    def runner(prompt_text, timeout_seconds):
        runner_calls.append(prompt_text)
        return json.dumps({
            "provider": "zai",
            "model_id": DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
            "event_type": "OTHER",
            "confirmation_claim": "MULTI_MEDIA_UNCONFIRMED",
            "surprise_claim": "UNKNOWN", "actor": "골든테마",
            "citations": ["candidate"],
            "relevance_claim": "NONE", "relevance_citations": []})
    executor = _executor(tmp_path, runner)
    report = _worker(news, ops, classifier_call=executor,
                     now=NOW + timedelta(minutes=2))
    assert report["settled_ok"] == 1 and len(runner_calls) == 1

    later = NOW + timedelta(minutes=6)  # candidate lease elapsed
    second = run_news_production(producer_deps=deps, now=later)
    assert second.drained == 1
    assert second.finalized.get("MERGED_INTO_EPISODE") == 1
    calls = []
    result = _run_live(news, ops, _sender_recorder(calls), now=later)
    assert result["sent"] == 1 and len(calls) == 1
    assert news.live_trip(LIVE_ID) is None


# ============================================================= K. readback


def test_readback_reports_live_state(tmp_path):
    from tokmo.programs.news_canary_readback import read_news_canary_state
    _write_gate(tmp_path, _live_body())
    news, ops = _stores(tmp_path)
    state = read_news_canary_state(tmp_path, now=NOW)
    assert state["delivery_mode"] == "TELEGRAM_LIVE"
    assert state["predecessor_activation_id"] == PRED_ID
    assert state["delivery_enabled_now"] is False
    assert "predecessor_unproven" in state["off_reasons"]
    _seed_predecessor(news)
    proven = read_news_canary_state(tmp_path, now=NOW)
    assert proven["delivery_enabled_now"] is True
    assert proven["off_reasons"] == []
    news.record_live_trip(LIVE_ID, "manual_rollback", now=NOW)
    tripped = read_news_canary_state(tmp_path, now=NOW)
    assert tripped["delivery_enabled_now"] is False
    assert "live_tripped" in tripped["off_reasons"]
    assert tripped["live_trip"]["reason"] == "manual_rollback"
