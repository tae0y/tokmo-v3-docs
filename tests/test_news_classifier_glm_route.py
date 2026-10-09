"""Exact GLM/Z.AI route cutover for the NEWS event classifier (2026-08-22).

Route authority is provider ``zai`` / model ``glm-5.3-flash`` with a direct,
non-agent, no-session transport: the official Z.AI chat-completions
endpoint called once over HTTPS (retry 0, no fallback, no tools, no
ambient context, no Hermes session). Every existing invariant is
preserved: single exact route, durable queue/leases, strict
news-classifier-output-v1 with citation/redaction enforcement, daily
budget, route-authority freshness + routing-marker admission,
prompt/evidence/credential non-leakage, and collection/delivery
independence. Legacy queue rows frozen under the old Spark identity
settle STALE/ROUTE_CHANGED without transport; the producer re-freezes
under the GLM identity. Network and model transport stay zero in tests:
every transport is a production-shaped fake injected through the real
seams (fake runner callables and a fake urlopen).
"""

import json
import os
import urllib.error
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tokmo.records.news_episode_store import NewsEpisodeStore
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.news_classifier import (
    DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    NEWS_CLASSIFIER_PROVIDER,
    NewsClassifierFailure,
    NewsClassifierRouteError,
    validate_classifier_route,
)
from tokmo.values.news_config import (
    PRODUCER_CANDIDATE_ATTEMPT_MAX,
)
from tokmo.values.ops_kernel import ProducerRole

UTC = UTC
NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)
LEGACY_MODEL_ID = "gpt-5.3-codex-spark"
LEGACY_PROVIDER = "openai-codex"
# The current production prompt-policy identity axis (SDS 2026-09-01 §D.4):
# worker admission settles any other frozen policy STALE/POLICY_CHANGED.
from tokmo.programs.news_production import (
    _PROMPT_POLICY_DIGEST as POLICY_DIGEST,
)

CONTRACT_DIGEST = "b" * 64
EVIDENCE = {
    "candidate": "삼성전자",
    "story_keys": "d-0,d-1",
    "stories": "3",
    "sources": "2",
}
# mirrors the real MODEL_ROUTING_STATE.md admission markers the
# completion-safe launcher demands for this exact route
ROUTING_MARKERS = (
    "# Model Routing State\n"
    "- quota authority: `model-quota-latest.json`\n"
    "- invocation freshness: 45분\n"
    "- direct Z.AI GLM route: glm-5.3-flash, fallback 0\n"
)


# ------------------------------------------------------------ 0. exact route


def test_route_constants_are_direct_zai_glm():
    assert NEWS_CLASSIFIER_PROVIDER == "zai"
    assert DEFAULT_NEWS_CLASSIFIER_MODEL_ID == "glm-5.3-flash"


def test_default_catalog_admits_only_the_glm_route():
    from tokmo.values.news_classifier import read_classifier_model

    route = validate_classifier_route(
        DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        {DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
    )
    assert (route.provider, route.model_id) == ("zai", "glm-5.3-flash")
    # the built-in fallback catalog (no config file, no explicit approval)
    # resolves the GLM model id
    assert (
        read_classifier_model(Path("/nonexistent-route-file.json"))
        == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    )
    with pytest.raises(NewsClassifierRouteError):
        read_classifier_model(
            Path("/nonexistent-route-file.json"),
            approved={"gpt-5.3-codex-spark": "openai-codex"},
        )


def test_builtin_fallback_catalog_resolves_direct_zai_provider():
    # m1: the built-in fallback catalog must map glm-5.3-flash to the direct
    # zai provider (not a stale openai-codex literal), observable through
    # the imported catalog object's route-resolution behavior.
    from tokmo.values.news_classifier import DEFAULT_NEWS_CLASSIFIER_CATALOG

    route = validate_classifier_route(
        DEFAULT_NEWS_CLASSIFIER_MODEL_ID, DEFAULT_NEWS_CLASSIFIER_CATALOG
    )
    assert route.provider == NEWS_CLASSIFIER_PROVIDER
    assert route.model_id == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    # every admitted route in the built-in catalog is direct zai
    for model_id in DEFAULT_NEWS_CLASSIFIER_CATALOG:
        resolved = validate_classifier_route(model_id, DEFAULT_NEWS_CLASSIFIER_CATALOG)
        assert resolved.provider == NEWS_CLASSIFIER_PROVIDER


def test_legacy_spark_route_is_no_longer_admitted_by_default(tmp_path):
    from tokmo.values.news_classifier import read_classifier_model

    # a config file naming the legacy Spark model fails closed against the
    # built-in (GLM-only) fallback catalog
    config = tmp_path / "news_classifier_model.json"
    config.write_text(json.dumps({"model_id": LEGACY_MODEL_ID}))
    with pytest.raises(NewsClassifierRouteError):
        read_classifier_model(config)


# -------------------------------------------------- 1. quota authority (zai)


def _zai_quota(
    *,
    observed_at=None,
    ok=True,
    five_hours=99.0,
    weekly=99.0,
    monthly=96.0,
    providers=None,
):
    payload = {
        "schema": "model-quota-latest-v1",
        "observed_at": (observed_at or (NOW - timedelta(minutes=5))).isoformat(),
        "providers": providers
        if providers is not None
        else {
            "zai": {
                "ok": ok,
                "windows": [
                    {"label": "5시간", "remaining": five_hours},
                    {"label": "주간", "remaining": weekly},
                    {"label": "월간 MCP", "remaining": monthly},
                ],
            }
        },
    }
    return payload


def _authority(
    tmp_path, *, quota=None, write_routing=True, write_quota=True, routing_text=None
):
    from tokmo.programs.news_classifier_executor import (
        build_route_authority_reader,
    )

    directory = Path(tmp_path)
    directory.mkdir(parents=True, exist_ok=True)
    routing = directory / "MODEL_ROUTING_STATE.md"
    latest = directory / "model-quota-latest.json"
    if write_routing:
        routing.write_text(
            routing_text if routing_text is not None else ROUTING_MARKERS
        )
    if write_quota:
        latest.write_text(json.dumps(quota if quota is not None else _zai_quota()))
    return build_route_authority_reader(
        routing_state_path=routing, quota_latest_path=latest, now=NOW
    )


def _request(
    evidence=None,
    *,
    model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
    provider=NEWS_CLASSIFIER_PROVIDER,
):
    import hashlib

    evidence = EVIDENCE if evidence is None else evidence
    encoded = json.dumps(
        evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode()
    from tokmo.values.news_classifier import NewsClassifierRequestV1

    return NewsClassifierRequestV1(
        "EVENT_CLASS",
        evidence,
        hashlib.sha256(encoded).hexdigest(),
        POLICY_DIGEST,
        "news-classifier-output-v1",
        provider,
        model_id,
        CONTRACT_DIGEST,
    )


def _exit_code(reader):
    with pytest.raises(NewsClassifierFailure) as failure:
        reader(_request())
    return failure.value.exit_code


def test_zai_quota_authority_requires_5h_and_weekly_windows(tmp_path):
    reader = _authority(tmp_path / "ok")
    authority = reader(_request())
    assert authority["quota_latest"]["observed_at"]


def test_zai_quota_authority_fails_closed_matrix(tmp_path):
    # stale observed_at (older than 45 minutes)
    assert (
        _exit_code(
            _authority(
                tmp_path / "stale",
                quota=_zai_quota(observed_at=NOW - timedelta(minutes=50)),
            )
        )
        == "MODEL_UNAVAILABLE"
    )
    # quota file missing entirely
    assert (
        _exit_code(_authority(tmp_path / "missing-quota", write_quota=False))
        == "MODEL_UNAVAILABLE"
    )
    # routing-state file missing
    assert (
        _exit_code(_authority(tmp_path / "missing-routing", write_routing=False))
        == "MODEL_UNAVAILABLE"
    )
    # provider ok false
    assert (
        _exit_code(_authority(tmp_path / "not-ok", quota=_zai_quota(ok=False)))
        == "MODEL_UNAVAILABLE"
    )
    # wrong schema id
    assert (
        _exit_code(
            _authority(
                tmp_path / "schema",
                quota={
                    "schema": "other-v1",
                    "observed_at": NOW.isoformat(),
                    "providers": _zai_quota()["providers"],
                },
            )
        )
        == "MODEL_UNAVAILABLE"
    )
    # malformed json
    bad = Path(tmp_path / "broken")
    bad.mkdir()
    (bad / "MODEL_ROUTING_STATE.md").write_text(ROUTING_MARKERS)
    (bad / "model-quota-latest.json").write_text("{broken")
    assert (
        _exit_code(_authority(bad, write_routing=False, write_quota=False))
        == "MODEL_UNAVAILABLE"
    )
    # zai provider absent (only codex/claude present)
    codex_only = {
        "schema": "model-quota-latest-v1",
        "observed_at": NOW.isoformat(),
        "providers": {
            "codex": {"ok": True, "windows": [{"label": "Session", "remaining": 59.0}]}
        },
    }
    assert (
        _exit_code(_authority(tmp_path / "codex-only", quota=codex_only))
        == "MODEL_UNAVAILABLE"
    )
    # 5시간 window missing (only 주간 present) → fail closed
    weekly_only = {
        "schema": "model-quota-latest-v1",
        "observed_at": NOW.isoformat(),
        "providers": {
            "zai": {
                "ok": True,
                "windows": [
                    {"label": "주간", "remaining": 50.0},
                    {"label": "월간 MCP", "remaining": 90.0},
                ],
            }
        },
    }
    assert (
        _exit_code(_authority(tmp_path / "weekly-only", quota=weekly_only))
        == "MODEL_UNAVAILABLE"
    )
    # 주간 window missing → fail closed
    five_hour_only = {
        "schema": "model-quota-latest-v1",
        "observed_at": NOW.isoformat(),
        "providers": {
            "zai": {
                "ok": True,
                "windows": [
                    {"label": "5시간", "remaining": 50.0},
                    {"label": "월간 MCP", "remaining": 90.0},
                ],
            }
        },
    }
    assert (
        _exit_code(_authority(tmp_path / "5h-only", quota=five_hour_only))
        == "MODEL_UNAVAILABLE"
    )
    # required window present but malformed (no remaining) → fail closed
    malformed_window = {
        "schema": "model-quota-latest-v1",
        "observed_at": NOW.isoformat(),
        "providers": {
            "zai": {
                "ok": True,
                "windows": [{"label": "5시간"}, {"label": "주간", "remaining": 50.0}],
            }
        },
    }
    assert (
        _exit_code(_authority(tmp_path / "malformed-window", quota=malformed_window))
        == "MODEL_UNAVAILABLE"
    )
    # 5시간 exhausted → QUOTA_EXCEEDED
    assert (
        _exit_code(
            _authority(tmp_path / "exhausted-5h", quota=_zai_quota(five_hours=0.0))
        )
        == "QUOTA_EXCEEDED"
    )
    # 주간 exhausted → QUOTA_EXCEEDED
    assert (
        _exit_code(
            _authority(tmp_path / "exhausted-weekly", quota=_zai_quota(weekly=0.0))
        )
        == "QUOTA_EXCEEDED"
    )
    # monthly (optional window) exhausted alone does NOT block
    reader = _authority(tmp_path / "monthly-zero", quota=_zai_quota(monthly=0.0))
    assert reader(_request())["quota_latest"]["observed_at"]
    # monthly missing entirely is fine (optional window)
    no_monthly = {
        "schema": "model-quota-latest-v1",
        "observed_at": NOW.isoformat(),
        "providers": {
            "zai": {
                "ok": True,
                "windows": [
                    {"label": "5시간", "remaining": 50.0},
                    {"label": "주간", "remaining": 50.0},
                ],
            }
        },
    }
    assert _authority(tmp_path / "no-monthly", quota=no_monthly)(_request()) is not None


def test_zai_quota_authority_rejects_naive_observed_at(tmp_path):
    naive = {
        "schema": "model-quota-latest-v1",
        "observed_at": "2026-08-22T09:00:00",
        "providers": _zai_quota()["providers"],
    }
    assert (
        _exit_code(_authority(tmp_path / "naive", quota=naive)) == "MODEL_UNAVAILABLE"
    )


def test_zai_quota_authority_rejects_future_clock_beyond_skew(tmp_path):
    # m4: a quota authority observed strictly later than the composing
    # worker's judgment instant is implausible; mirror the completion-safe
    # launcher's >5-minute future rejection so both admissions fail closed
    # on the same clock-skew bound.
    assert (
        _exit_code(
            _authority(
                tmp_path / "future-10min",
                quota=_zai_quota(observed_at=NOW + timedelta(minutes=10)),
            )
        )
        == "MODEL_UNAVAILABLE"
    )
    assert (
        _exit_code(
            _authority(
                tmp_path / "future-30min",
                quota=_zai_quota(observed_at=NOW + timedelta(minutes=30)),
            )
        )
        == "MODEL_UNAVAILABLE"
    )
    # small positive skew (≤5 minutes) stays admissible: the snapshot may
    # have been written by a clock marginally ahead of --now.
    reader = _authority(
        tmp_path / "future-2min",
        quota=_zai_quota(observed_at=NOW + timedelta(minutes=2)),
    )
    assert reader(_request())["quota_latest"]["observed_at"]


def test_route_authority_requires_routing_state_admission_markers(tmp_path):
    # the routing authority must itself admit this exact route: the same
    # marker set the completion-safe launcher demands (quota authority
    # pointer, invocation freshness, exact glm-5.3-flash model). A routing
    # state that does not name them fails closed before any transport.
    markerless = "# Model Routing State\n- some other route\n"
    assert (
        _exit_code(_authority(tmp_path / "markerless", routing_text=markerless))
        == "MODEL_UNAVAILABLE"
    )
    missing_model = ROUTING_MARKERS.replace("glm-5.3-flash", "glm-x")
    assert (
        _exit_code(_authority(tmp_path / "wrong-model", routing_text=missing_model))
        == "MODEL_UNAVAILABLE"
    )


# ------------------------------------------- 2. direct non-agent transport


class _FakeResponse:
    def __init__(self, body, status=200):
        self._body = body
        self.status = status

    def read(self, amount=-1):
        return self._body if amount is None or amount < 0 else self._body[:amount]

    def getcode(self):
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _ok_completion(payload):
    return json.dumps(
        {
            "id": "c1",
            "model": "glm-5.3-flash",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": payload},
                }
            ],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    ).encode("utf-8")


def _CaptureOpener(captures, *, response=None, error=None):
    class _opener:
        def open(self, request, timeout=None):
            captures.append(
                {
                    "url": request.full_url,
                    "authorization": request.get_header("Authorization"),
                    "content_type": request.get_header("Content-type"),
                    "body": json.loads(request.data.decode("utf-8")),
                    "bytes": request.data,
                    "timeout": timeout,
                }
            )
            if error is not None:
                raise error
            return _FakeResponse(response if response is not None else b"{}")

    return _opener()


def test_zai_chat_once_sends_single_bounded_public_only_request():
    # B1/M4 closure: the transport is ONE non-agent HTTPS POST whose body
    # is exactly the public-evidence prompt — no tools, no system prompt,
    # no private/ambient context, no second provider, no session. The
    # credential rides only the Authorization header, never argv.
    from tokmo.facts.news_http import MAX_TOKENS, zai_chat_once

    prompt = "역할: 분류기\n규칙: JSON 하나\n"
    captures = []
    message = zai_chat_once(
        prompt,
        timeout_seconds=123,
        api_key="test-secret-key",
        opener=_CaptureOpener(captures, response=_ok_completion('{"ok": true}')),
    )
    assert message == '{"ok": true}'
    assert len(captures) == 1  # exactly one request, retry 0
    call = captures[0]
    assert call["url"] == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert call["authorization"] == "Bearer test-secret-key"
    assert call["timeout"] == 123  # m3: timeout propagation
    body = call["body"]
    assert body["model"] == "glm-5.3-flash"
    assert body["stream"] is False
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "low"
    assert body["max_tokens"] == MAX_TOKENS
    assert body["messages"] == [{"role": "user", "content": prompt}]
    assert set(body) == {
        "model",
        "messages",
        "stream",
        "thinking",
        "reasoning_effort",
        "max_tokens",
    }  # no tools/tool_choice/aux routes
    baseline = captures[0]["bytes"]
    for count, flag in enumerate((False, True), 2):
        assert (
            zai_chat_once(
                prompt,
                timeout_seconds=123,
                api_key="test-secret-key",
                json_mode=flag,
                opener=_CaptureOpener(
                    captures, response=_ok_completion('{"ok": true}')
                ),
            )
            == message
        )
        assert captures[-1]["bytes"] == (
            baseline[:-1] + b', "response_format": {"type": "json_object"}}'
            if flag
            else baseline
        )
        assert len(captures) == count


@pytest.mark.parametrize("json_mode", [False, True])
def test_zai_chat_once_http_failure_mapping_matrix(json_mode):
    from tokmo.facts.news_http import zai_chat_once

    cases = [
        (429, "QUOTA_EXCEEDED"),
        (401, "MODEL_UNAVAILABLE"),
        (403, "MODEL_UNAVAILABLE"),
        (500, "MODEL_UNAVAILABLE"),
        (503, "MODEL_UNAVAILABLE"),
        (404, "TRANSPORT_FAIL"),
    ]
    for status, expected in cases:
        captures = []
        with pytest.raises(NewsClassifierFailure) as failure:
            zai_chat_once(
                "p",
                timeout_seconds=5,
                api_key="k",
                json_mode=json_mode,
                opener=_CaptureOpener(
                    captures,
                    error=urllib.error.HTTPError(
                        "url", status, "err", hdrs=None, fp=None
                    ),
                ),
            )
        assert failure.value.exit_code == expected, status
        # retry 0: the failing request is never repeated
        assert len(captures) == 1, status


def test_zai_chat_once_connection_errors_map_transport_and_timeout():
    from tokmo.facts.news_http import zai_chat_once

    captures = []
    with pytest.raises(NewsClassifierFailure) as failure:
        zai_chat_once(
            "p",
            timeout_seconds=5,
            api_key="k",
            opener=_CaptureOpener(
                captures, error=urllib.error.URLError(ConnectionRefusedError())
            ),
        )
    assert failure.value.exit_code == "TRANSPORT_FAIL"
    assert len(captures) == 1
    with pytest.raises(TimeoutError):
        zai_chat_once(
            "p",
            timeout_seconds=5,
            api_key="k",
            opener=_CaptureOpener([], error=urllib.error.URLError(TimeoutError())),
        )
    with pytest.raises(TimeoutError):
        zai_chat_once(
            "p",
            timeout_seconds=5,
            api_key="k",
            opener=_CaptureOpener([], error=TimeoutError()),
        )


@pytest.mark.parametrize("json_mode", [False, True])
def test_zai_chat_once_malformed_completion_is_model_unavailable(json_mode):
    from tokmo.facts.news_http import zai_chat_once

    for bad in (
        b"{}",
        b'{"choices": []}',
        b'{"choices": [{"message": {}}]}',
        b'{"choices": [{"message": {"content": 7}}]}',
        b"not-json",
    ):
        captures = []
        with pytest.raises(NewsClassifierFailure) as failure:
            zai_chat_once(
                "p",
                timeout_seconds=5,
                api_key="k",
                json_mode=json_mode,
                opener=_CaptureOpener(captures, response=bad),
            )
        assert failure.value.exit_code == "MODEL_UNAVAILABLE", bad
        assert failure.value.failure_diagnostics["error_class"] == "malformed_response"


def test_zai_chat_once_empty_credential_fails_closed_before_network():
    from tokmo.facts.news_http import zai_chat_once

    class _NeverOpener:
        def open(self, request, timeout=None):
            raise AssertionError("no network contact allowed")

    with pytest.raises(NewsClassifierFailure) as failure:
        zai_chat_once("p", timeout_seconds=5, api_key="  ", opener=_NeverOpener())
    assert failure.value.exit_code == "MODEL_UNAVAILABLE"


def test_zai_chat_once_rejects_oversize_completion_body():
    from tokmo.facts.news_http import OUTPUT_MAX_BYTES, zai_chat_once

    oversized = b"x" * (OUTPUT_MAX_BYTES + 1)

    class _HugeOpener:
        def open(self, request, timeout=None):
            return _FakeResponse(oversized)

    with pytest.raises(json.JSONDecodeError):
        zai_chat_once("p", timeout_seconds=5, api_key="k", opener=_HugeOpener())


def test_zai_chat_endpoint_honors_bigmodel_base_url():
    from tokmo.facts.news_http import zai_chat_endpoint

    assert zai_chat_endpoint() == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    assert (
        zai_chat_endpoint("https://open.bigmodel.cn/x")
        == "https://open.bigmodel.cn/api/coding/paas/v4/chat/completions"
    )
    assert (
        zai_chat_endpoint("https://api.z.ai")
        == "https://api.z.ai/api/coding/paas/v4/chat/completions"
    )


def test_read_zai_credential_file_preflight_matrix(tmp_path):
    # M2 closure: the launcher's fail-closed conditions (missing or
    # insecure credential file) are typed MODEL_UNAVAILABLE here too, so
    # outage recording and ops alerts fire instead of TRANSPORT_FAIL.
    from tokmo.programs.news_classifier_executor import (
        read_zai_credential_file,
    )

    # secure owner-only env file resolves the key
    secure = tmp_path / "secure.env"
    secure.write_text("GLM_API_KEY=file-key\n", encoding="utf-8")
    os.chmod(secure, 0o600)
    assert read_zai_credential_file(secure) == "file-key"
    # export-prefixed and quoted forms resolve too
    quoted = tmp_path / "quoted.env"
    quoted.write_text('export ZAI_API_KEY="quoted-key"\n', encoding="utf-8")
    os.chmod(quoted, 0o600)
    assert read_zai_credential_file(quoted) == "quoted-key"
    # missing file → the launcher's fail-closed condition
    with pytest.raises(NewsClassifierFailure) as missing:
        read_zai_credential_file(tmp_path / "absent.env")
    assert missing.value.exit_code == "MODEL_UNAVAILABLE"
    # insecure permissions → fail closed exactly like run-glm-zai
    insecure = tmp_path / "insecure.env"
    insecure.write_text("GLM_API_KEY=file-key\n", encoding="utf-8")
    os.chmod(insecure, 0o644)
    with pytest.raises(NewsClassifierFailure) as perms:
        read_zai_credential_file(insecure)
    assert perms.value.exit_code == "MODEL_UNAVAILABLE"
    # symlinked credential file → fail closed
    linked = tmp_path / "linked.env"
    linked.symlink_to(secure)
    with pytest.raises(NewsClassifierFailure) as link:
        read_zai_credential_file(linked)
    assert link.value.exit_code == "MODEL_UNAVAILABLE"
    # file present but the key is absent → fail closed
    keyless = tmp_path / "keyless.env"
    keyless.write_text("OTHER=x\n", encoding="utf-8")
    os.chmod(keyless, 0o600)
    with pytest.raises(NewsClassifierFailure) as keyless_failure:
        read_zai_credential_file(keyless)
    assert keyless_failure.value.exit_code == "MODEL_UNAVAILABLE"


# ---------------------------------------------- 3. GLM executor composition


def _glm_output(request, **extra):
    """Contract-r3 fact extraction payload (SDS §D.4 + A1 §Q.2)."""
    payload = {
        "provider": request.provider,
        "model_id": request.model_id,
        "event_type": "CORPORATE_ACTION_DECIDED",
        "confirmation_claim": "MULTI_MEDIA_UNCONFIRMED",
        "surprise_claim": "UNKNOWN",
        "actor": "삼성전자",
        "citations": ["candidate"],
        "relevance_claim": "NONE",
        "relevance_citations": [],
    }
    payload.update(extra)
    return payload


def _fake_glm_runner(payload, calls=None):
    """Production-shaped fake: the transport returns the model's final
    message text (direct chat-completions content contract)."""

    def runner(prompt_text, timeout_seconds):
        if calls is not None:
            calls.append({"prompt": prompt_text, "timeout": timeout_seconds})
        if payload is None:
            return None
        return payload if isinstance(payload, str) else json.dumps(payload)

    return runner


def _executor(tmp_path, runner, *, receipts=None, timeout_seconds=600):
    from tokmo.programs.news_classifier_executor import (
        build_glm_classifier_call,
    )

    sink = receipts.append if receipts is not None else (lambda r: None)
    # advance the injected judgment clock one second per call: real
    # queue items are judged at distinct instants, and a static fake
    # clock would collapse every failure bundle onto one name
    ticks = iter(range(1_000_000))
    return build_glm_classifier_call(
        glm_runner=runner,
        authority_reader=_authority(tmp_path),
        receipt_sink=sink,
        timeout_seconds=timeout_seconds,
        work_dir=tmp_path / "work",
        clock=lambda: NOW + timedelta(seconds=next(ticks)),
    )


def test_executor_composes_direct_transport_call(tmp_path):
    calls = []
    executor = _executor(
        tmp_path, _fake_glm_runner(json.dumps(_glm_output(_request())), calls)
    )
    output = executor(_request())
    assert json.loads(output)["event_type"] == "CORPORATE_ACTION_DECIDED"
    # the runner seam carries only prompt + timeout: no argv, no
    # subprocess, no wrapper artifacts exist anywhere in the contract
    assert calls[0]["timeout"] == 600
    assert "삼성전자" in calls[0]["prompt"]
    assert set(calls[0]) == {"prompt", "timeout"}


def test_executor_transport_is_in_process_not_a_child_process():
    # M3/M4: the executor module has no process-spawning surface and no
    # Hermes/agent imports — the model is reached through one bounded
    # HTTPS call, so approval/compression/fallback/auxiliary-provider
    # routes cannot exist for this classifier.
    import inspect

    import tokmo.programs.news_classifier_executor as executor_module

    module_vars = vars(executor_module)
    assert "subprocess" not in module_vars
    assert "run_glm_child" not in module_vars
    assert "build_glm_argv" not in module_vars
    # every public function defined here belongs to this project only
    for name, value in module_vars.items():
        if inspect.isfunction(value) and value.__module__:
            assert value.__module__.startswith("tokmo."), name


def test_executor_prompt_demands_single_json_object_public_only(tmp_path):
    calls = []
    executor = _executor(
        tmp_path, _fake_glm_runner(json.dumps(_glm_output(_request())), calls)
    )
    executor(_request())
    prompt = calls[0]["prompt"]
    assert "JSON" in prompt
    assert "glm-5.3-flash" in prompt
    assert "zai" in prompt
    # Contract v2 (SDS §D.2/§D.4): the model extracts bounded facts only —
    # the closed event-type/claim vocabularies replace the v1 event-class
    # demand, and the legacy label vocabulary is gone from the prompt.
    assert (
        "event_type" in prompt
        and "confirmation_claim" in prompt
        and "surprise_claim" in prompt
        and "actor" in prompt
    )
    # r3 relevance axis rides the same prompt (SDS A1 §Q.2).
    assert "relevance_claim" in prompt and "relevance_citations" in prompt
    assert "INDIRECT_MATERIAL" in prompt and "THEMATIC_MATERIAL" in prompt
    assert (
        "CORPORATE_ACTION_DECIDED" in prompt
        and "OTHER" in prompt
        and "UNKNOWN" in prompt
    )
    assert "MULTI_MEDIA_UNCONFIRMED" in prompt
    assert (
        "CORE_BREAKING" not in prompt
        and "event_class" not in prompt
        and "UNDECIDED" not in prompt
    )
    assert "삼성전자" in prompt


def test_executor_receipt_carries_route_and_no_evidence(tmp_path):
    receipts = []
    executor = _executor(
        tmp_path,
        _fake_glm_runner(json.dumps(_glm_output(_request()))),
        receipts=receipts,
    )
    executor(_request())
    receipt = receipts[0]
    assert receipt["provider"] == "zai"
    assert receipt["model_id"] == "glm-5.3-flash"
    assert receipt["timeout_seconds"] == 600
    assert receipt["retry_max"] == 0
    assert receipt["transport"] == "direct-zai-chat-completions"
    authority = receipt["route_authority"]
    assert len(authority["routing_state"]["sha256"]) == 64
    assert len(authority["quota_latest"]["sha256"]) == 64
    flattened = json.dumps(receipt, ensure_ascii=False)
    assert "삼성전자" not in flattened  # no evidence/prompt in receipts


def test_executor_rejects_wrong_route_without_transport(tmp_path):
    def _never(prompt_text, timeout_seconds):
        raise AssertionError("wrong route must not start transport")

    executor = _executor(tmp_path, _never)
    with pytest.raises(NewsClassifierFailure) as rejected:
        executor(_request(model_id="gpt-other"))
    assert rejected.value.exit_code == "MODEL_REJECTED"
    with pytest.raises(NewsClassifierFailure) as foreign:
        executor(_request(provider="openai-codex"))
    assert foreign.value.exit_code == "MODEL_REJECTED"
    # legacy Spark identity is explicitly rejected
    with pytest.raises(NewsClassifierFailure) as legacy:
        executor(_request(model_id=LEGACY_MODEL_ID, provider=LEGACY_PROVIDER))
    assert legacy.value.exit_code == "MODEL_REJECTED"


def test_executor_missing_output_is_empty_success(tmp_path):
    executor = _executor(tmp_path, _fake_glm_runner(None))
    with pytest.raises(NewsClassifierFailure) as failure:
        executor(_request())
    assert failure.value.exit_code == "EMPTY_SUCCESS"


def test_executor_invalid_json_is_parse_fail(tmp_path):
    executor = _executor(tmp_path, _fake_glm_runner("{broken"))
    with pytest.raises(json.JSONDecodeError):
        executor(_request())


def test_executor_schema_and_citation_validation_stay_strict(tmp_path):
    private = _glm_output(_request())
    private["account_scope"] = "isa-1234"
    executor = _executor(tmp_path, _fake_glm_runner(json.dumps(private)))
    with pytest.raises(ValueError) as schema_error:
        executor(_request())
    assert str(schema_error.value) == "SCHEMA_INVALID"
    unresolved = _glm_output(_request(), citations=["ghost-doc"])
    executor = _executor(tmp_path, _fake_glm_runner(json.dumps(unresolved)))
    with pytest.raises(ValueError) as citation_error:
        executor(_request())
    assert str(citation_error.value) == "CITATION_UNRESOLVED"


def test_executor_wrapped_json_object_is_extracted_still_strict(tmp_path):
    # GLM may wrap the JSON object in prose/code fences; the executor must
    # locate the object without weakening the strict validator.
    wrapped = (
        "분류 결과입니다.\n```json\n"
        + json.dumps(_glm_output(_request()))
        + "\n```\n감사합니다."
    )
    executor = _executor(tmp_path, _fake_glm_runner(wrapped))
    output = executor(_request())
    assert json.loads(output)["event_type"] == "CORPORATE_ACTION_DECIDED"


def test_extract_prefers_last_object_over_decoy_first():
    # M1: a leading decoy fragment (the model quoting the rules or echoing
    # a fragment) must never win; the answer is the last valid top-level
    # JSON object in the message.
    from tokmo.programs.news_classifier_executor import extract_json_object

    decoy = {"provider": "zai", "model_id": "glm-5.3-flash", "event_class": "UNDECIDED"}
    answer = _glm_output(_request())
    text = (
        "규칙을 인용하면 이런 형태다: "
        + json.dumps(decoy)
        + "\n최종 답변:\n"
        + json.dumps(answer)
    )
    assert extract_json_object(text) == answer


def test_extract_prefers_last_fenced_object():
    from tokmo.programs.news_classifier_executor import extract_json_object

    first = {"provider": "zai", "model_id": "glm-5.3-flash", "event_class": "UNDECIDED"}
    answer = _glm_output(_request())
    text = (
        "```json\n" + json.dumps(first) + "\n```\n"
        "다시 정리하면:\n```json\n" + json.dumps(answer) + "\n```"
    )
    assert extract_json_object(text) == answer


def test_extract_multiple_bare_objects_takes_the_last():
    from tokmo.programs.news_classifier_executor import extract_json_object

    first = {"provider": "zai", "model_id": "glm-5.3-flash", "event_class": "UNDECIDED"}
    answer = _glm_output(_request())
    text = json.dumps(first) + "\n" + json.dumps(answer)
    assert extract_json_object(text) == answer


def test_extract_trailing_prose_after_last_object():
    from tokmo.programs.news_classifier_executor import extract_json_object

    answer = _glm_output(_request())
    text = json.dumps(answer) + "\n위 결과에 따라 ORDINARY로 확정합니다."
    assert extract_json_object(text) == answer


def test_extract_malformed_then_valid_object():
    # a truncated object followed by the real answer: the malformed
    # prefix is skipped, the last valid object wins.
    from tokmo.programs.news_classifier_executor import extract_json_object

    answer = _glm_output(_request())
    text = (
        '{"provider": "zai", "model_id": "glm-5.3-flash", "event_cl'
        + "\n"
        + json.dumps(answer)
    )
    assert extract_json_object(text) == answer


def test_extract_oversize_output_is_rejected_before_scan():
    # m5: an output beyond the byte cap must fail before any parse/scan
    # work, as PARSE_FAIL, without O(n^2) behavior.
    from tokmo.programs.news_classifier_executor import (
        GLM_OUTPUT_MAX_BYTES,
        extract_json_object,
    )

    assert GLM_OUTPUT_MAX_BYTES > 0
    huge = ("x" * GLM_OUTPUT_MAX_BYTES) + json.dumps(_glm_output(_request()))
    with pytest.raises(json.JSONDecodeError):
        extract_json_object(huge)


def test_extract_scan_is_linear_on_decoy_heavy_messages():
    # m5: single-pass string-aware span scan — a message dense with
    # unmatched openers must not trigger a suffix rescan for each opener.
    from tokmo.programs.news_classifier_executor import (
        GLM_OUTPUT_MAX_BYTES,
        extract_json_object,
    )

    answer = _glm_output(_request())
    encoded = json.dumps(answer)
    text = "{" * (GLM_OUTPUT_MAX_BYTES - len(encoded) - 1) + "\n" + encoded
    assert extract_json_object(text) == answer


def test_strict_output_requires_explicit_citations(tmp_path):
    missing = _glm_output(_request())
    del missing["citations"]
    executor = _executor(tmp_path, _fake_glm_runner(json.dumps(missing)))
    with pytest.raises(ValueError) as failure:
        executor(_request())
    assert str(failure.value) == "SCHEMA_INVALID"


def test_executor_rejects_legacy_v1_event_class_output(tmp_path):
    """J-12 부속/J-14: a v1-shaped answer (event_class label) is outside the
    exact v2 projection — SCHEMA_INVALID, so a model label can never re-enter
    the event-class axis through the parsing layer."""
    legacy = {
        "provider": "zai",
        "model_id": "glm-5.3-flash",
        "event_class": "CORE_BREAKING",
        "citations": ["candidate"],
    }
    executor = _executor(tmp_path, _fake_glm_runner(json.dumps(legacy)))
    with pytest.raises(ValueError) as failure:
        executor(_request())
    assert str(failure.value) == "SCHEMA_INVALID"


def test_validate_classifier_output_r3_strictness():
    from tokmo.values.news_classifier import validate_classifier_output_r3

    base = _glm_output(_request())

    def check(error, **mutation):
        payload = dict(base) | mutation
        for key, value in list(mutation.items()):
            if value is None:
                del payload[key]
        with pytest.raises(ValueError) as failure:
            validate_classifier_output_r3(
                payload, provider="zai", model_id="glm-5.3-flash", evidence=EVIDENCE
            )
        assert str(failure.value) == error

    canonical = validate_classifier_output_r3(
        dict(base), provider="zai", model_id="glm-5.3-flash", evidence=EVIDENCE
    )
    assert json.loads(canonical) == base
    check("SCHEMA_INVALID", event_class="CORE_BREAKING")  # extra v1 key
    check("SCHEMA_INVALID", event_type="MOON_LANDING")  # open vocab
    check("SCHEMA_INVALID", event_type=None)  # missing key
    check("SCHEMA_INVALID", confirmation_claim="CONFIRMED")
    check("SCHEMA_INVALID", confirmation_claim=True)  # bool excluded
    check("SCHEMA_INVALID", surprise_claim="MAYBE")
    check("SCHEMA_INVALID", actor="가" * 41)  # >120 bytes
    check("SCHEMA_INVALID", actor=7)
    check("SCHEMA_INVALID", citations="candidate")  # str, not list
    check("SCHEMA_INVALID", citations=[True])
    check("MODEL_REJECTED", model_id="gpt-other")
    check("CITATION_UNRESOLVED", citations=["ghost-doc"])
    # r3 relevance axis (SDS A1 §Q.2)
    check("SCHEMA_INVALID", relevance_claim=None)  # missing key
    check("SCHEMA_INVALID", relevance_claim="DIRECT")  # open vocab
    check("SCHEMA_INVALID", relevance_claim=True)
    check("SCHEMA_INVALID", relevance_citations=None)
    check("SCHEMA_INVALID", relevance_citations="candidate")
    check("SCHEMA_INVALID", relevance_citations=[False])
    # a promoted claim REQUIRES at least one citation…
    check("SCHEMA_INVALID", relevance_claim="INDIRECT_MATERIAL", relevance_citations=[])
    # …and citations must resolve into the public evidence keys.
    check(
        "CITATION_UNRESOLVED",
        relevance_claim="THEMATIC_MATERIAL",
        relevance_citations=["ghost-doc"],
    )
    cited = validate_classifier_output_r3(
        dict(base)
        | {
            "relevance_claim": "INDIRECT_MATERIAL",
            "relevance_citations": ["candidate"],
        },
        provider="zai",
        model_id="glm-5.3-flash",
        evidence=EVIDENCE,
    )
    assert json.loads(cited)["relevance_claim"] == "INDIRECT_MATERIAL"


def test_extract_rejects_nested_fragment_not_top_level():
    # M1: extraction must not accept an arbitrary nested fragment —
    # only complete top-level objects qualify as candidates.
    from tokmo.programs.news_classifier_executor import extract_json_object

    payload = {
        "provider": "zai",
        "model_id": "glm-5.3-flash",
        "event_class": "ORDINARY",
        "citations": ["candidate"],
    }
    assert extract_json_object(json.dumps(payload)) == payload
    # a bare fragment with no complete top-level object fails closed
    with pytest.raises(json.JSONDecodeError):
        extract_json_object('설명: "provider": "zai" 는 key다')
    # braces inside string values do not open nested candidates
    tricky = {
        "provider": "zai",
        "model_id": "glm-5.3-flash",
        "event_class": "ORDINARY",
        "citations": ["{not an object}"],
    }
    assert extract_json_object(json.dumps(tricky)) == tricky


def _failing_runner(exit_code="TRANSPORT_FAIL", *, diagnostics=None):
    def runner(prompt_text, timeout_seconds):
        failure = NewsClassifierFailure(exit_code)
        if diagnostics is not None:
            failure.failure_diagnostics = diagnostics
        raise failure

    return runner


def test_executor_preserves_owner_only_diagnostics_on_failure(tmp_path):
    executor = _executor(
        tmp_path,
        _failing_runner(diagnostics={"http_status": 429, "error_class": "http_error"}),
    )
    with pytest.raises(NewsClassifierFailure):
        executor(_request())
    root = tmp_path / "work" / "failure_diagnostics"
    bundles = list(root.iterdir())
    assert len(bundles) == 1
    bundle = bundles[0]
    # m6: control-plane facts only — no manifest copy, no model-derived
    # transcript stream, nothing the transport never produced
    assert {path.name for path in bundle.iterdir()} == {"failure.json"}
    record = json.loads((bundle / "failure.json").read_text())
    assert record["exit_code"] == "TRANSPORT_FAIL"
    assert record["provider"] == "zai"
    assert record["model_id"] == "glm-5.3-flash"
    assert record["http_status"] == 429
    assert record["error_class"] == "http_error"
    assert record["input_evidence_digest"] == _request().input_evidence_digest
    assert set(record) == {
        "record",
        "exit_code",
        "provider",
        "model_id",
        "purpose",
        "input_evidence_digest",
        "prompt_policy_digest",
        "http_status",
        "error_class",
    }
    flattened = json.dumps(record, ensure_ascii=False)
    assert "삼성전자" not in flattened  # evidence never lands on disk
    assert "역할" not in flattened  # nor any prompt fragment
    assert os.stat(root).st_mode & 0o077 == 0
    assert os.stat(bundle / "failure.json").st_mode & 0o077 == 0
    # the prompt never touched disk anywhere in the work tree
    assert not [path for path in (tmp_path / "work").rglob("prompt*")]
    assert [path.name for path in (tmp_path / "work").iterdir()] == [
        "failure_diagnostics"
    ]


def test_executor_timeout_failure_is_preserved_too(tmp_path):
    def hanging(prompt_text, timeout_seconds):
        raise TimeoutError("glm call exceeded the bounded call budget")

    executor = _executor(tmp_path, hanging)
    with pytest.raises(TimeoutError):
        executor(_request())
    bundles = list((tmp_path / "work" / "failure_diagnostics").iterdir())
    assert len(bundles) == 1
    record = json.loads((bundles[0] / "failure.json").read_text())
    assert record["exit_code"] == "TIMEOUT"


def test_executor_empty_success_still_preserves_failure_record(tmp_path):
    executor = _executor(tmp_path, _fake_glm_runner(None))
    with pytest.raises(NewsClassifierFailure) as failure:
        executor(_request())
    assert failure.value.exit_code == "EMPTY_SUCCESS"
    bundles = list((tmp_path / "work" / "failure_diagnostics").iterdir())
    assert len(bundles) == 1
    record = json.loads((bundles[0] / "failure.json").read_text())
    assert record["exit_code"] == "EMPTY_SUCCESS"


def test_executor_retry_is_zero_by_contract(tmp_path):
    receipts = []
    executor = _executor(
        tmp_path,
        _fake_glm_runner(json.dumps(_glm_output(_request()))),
        receipts=receipts,
    )
    executor(_request())
    assert receipts[0]["retry_max"] == 0


def test_executor_failure_diagnostics_retention_is_bounded(tmp_path):
    from tokmo.programs.news_classifier_executor import (
        FAILURE_DIAGNOSTICS_MAX_BUNDLES,
    )

    executor = _executor(tmp_path, _failing_runner())
    for _ in range(FAILURE_DIAGNOSTICS_MAX_BUNDLES + 2):
        with pytest.raises(NewsClassifierFailure):
            executor(_request())
    bundles = list((tmp_path / "work" / "failure_diagnostics").iterdir())
    assert len(bundles) == FAILURE_DIAGNOSTICS_MAX_BUNDLES


# ---------------------------------------- 4. worker wiring + legacy stale


def _stores(tmp_path):
    news = NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")
    ops = OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3", stable_phase_ids=frozenset()
    )
    return news, ops


def _enqueue_legacy_spark_work(news, ops):
    from tokmo.programs.news_investigation import run_news_investigation

    return run_news_investigation(
        store=news,
        episode_id=None,
        candidate_id="ndc-legacy",
        purpose="EVENT_CLASS",
        evidence=EVIDENCE,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=LEGACY_MODEL_ID,
        admitted_catalog={LEGACY_MODEL_ID: LEGACY_PROVIDER},
        classifier_call=_never_classifier,
        now=NOW,
        retry_max=0,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        async_queue=news.enqueue_classifier_work,
    )


def _never_classifier(request):
    raise AssertionError("transport must never start in this test")


def _worker(
    news,
    ops,
    *,
    classifier_call,
    now=NOW,
    config_path=None,
    max_items=2,
    attempt_max=PRODUCER_CANDIDATE_ATTEMPT_MAX,
):
    from tokmo.programs.news_classifier_worker import run_classifier_worker

    return run_classifier_worker(
        store=news,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        classifier_call=classifier_call,
        config_path=config_path,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        now=now,
        lease_owner="glm-worker-test",
        lease_seconds=900,
        max_items=max_items,
        attempt_max=attempt_max,
    )


def test_legacy_spark_queue_item_settles_stale_without_transport(tmp_path):
    news, ops = _stores(tmp_path)
    outcome = _enqueue_legacy_spark_work(news, ops)
    identity = outcome["classification_identity"]
    assert news.classifier_work(identity)["model_id"] == LEGACY_MODEL_ID
    report = _worker(news, ops, classifier_call=_never_classifier)
    assert report["stale"] == 1
    assert news.classifier_work(identity)["state"] == "STALE"
    assert news.classifier_work(identity)["last_exit"] == "ROUTE_CHANGED"
    # zero model transport: no classifier_calls rows were created
    assert (
        news.connection.execute("SELECT count(*) FROM classifier_calls").fetchone()[0]
        == 0
    )


def test_producer_refreezes_under_glm_identity_after_route_change(tmp_path):
    news, ops = _stores(tmp_path)
    legacy = _enqueue_legacy_spark_work(news, ops)
    _worker(news, ops, classifier_call=_never_classifier)
    from tokmo.programs.news_investigation import run_news_investigation

    outcome = run_news_investigation(
        store=news,
        episode_id=None,
        candidate_id="ndc-legacy",
        purpose="EVENT_CLASS",
        evidence=EVIDENCE,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        classifier_call=_never_classifier,
        now=NOW,
        retry_max=0,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        async_queue=news.enqueue_classifier_work,
    )
    assert outcome["status"] == "CLASSIFICATION_DEFERRED"
    assert outcome["exit"] == "ASYNC_PENDING"
    assert outcome["classification_identity"] != legacy["classification_identity"]
    work = news.classifier_work(outcome["classification_identity"])
    assert work["model_id"] == DEFAULT_NEWS_CLASSIFIER_MODEL_ID
    assert work["provider"] == "zai"


def test_worker_config_default_routes_to_glm(tmp_path):
    news, ops = _stores(tmp_path)
    from tokmo.programs.news_investigation import run_news_investigation

    outcome = run_news_investigation(
        store=news,
        episode_id=None,
        candidate_id="ndc-fresh",
        purpose="EVENT_CLASS",
        evidence=EVIDENCE,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        classifier_call=_never_classifier,
        now=NOW,
        retry_max=0,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        async_queue=news.enqueue_classifier_work,
    )
    identity = outcome["classification_identity"]
    calls = []

    def _ok(request):
        calls.append(request)
        return json.dumps(
            {
                "provider": request.provider,
                "model_id": request.model_id,
                "event_class": "ORDINARY",
                "citations": [],
            }
        )

    report = _worker(news, ops, classifier_call=_ok)
    assert report["settled_ok"] == 1 and len(calls) == 1
    assert calls[0].provider == "zai"
    assert calls[0].model_id == "glm-5.3-flash"
    assert news.classifier_work(identity)["state"] == "DONE"


def test_worker_main_wiring_drains_via_direct_glm_transport(tmp_path, monkeypatch):
    # m2 (DI replacement for the source-snapshot test): drive the real
    # worker CLI composition end-to-end with a fake registered transport
    # adapter and a temporary owner-checked --glm-env-file. Proves: the
    # worker admits only glm-5.3-flash/zai, resolves the credential through
    # the real env-file preflight, reaches the model via the in-process
    # direct transport (prompt + timeout only), settles the queue item,
    # and writes no argv/subprocess anywhere.
    from tokmo.programs import news_classifier_worker as worker_module
    from tokmo.programs.news_investigation import run_news_investigation

    news, ops = _stores(tmp_path)
    run_news_investigation(
        store=news,
        episode_id=None,
        candidate_id="ndc-main",
        purpose="EVENT_CLASS",
        evidence=EVIDENCE,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        classifier_call=_never_classifier,
        now=NOW,
        retry_max=0,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        async_queue=news.enqueue_classifier_work,
    )
    routing = tmp_path / "MODEL_ROUTING_STATE.md"
    routing.write_text(ROUTING_MARKERS)
    quota = tmp_path / "model-quota-latest.json"
    quota.write_text(json.dumps(_zai_quota()))
    env_file = tmp_path / "glm.env"
    env_file.write_text("GLM_API_KEY=worker-test-key\n")
    os.chmod(env_file, 0o600)
    transports = []

    def fake_chat(prompt_text, *, timeout_seconds, api_key, **kwargs):
        transports.append(
            {"prompt": prompt_text, "timeout": timeout_seconds, "api_key": api_key}
        )
        return json.dumps(
            {
                "provider": "zai",
                "model_id": "glm-5.3-flash",
                "event_type": "OTHER",
                "confirmation_claim": "MULTI_MEDIA_UNCONFIRMED",
                "surprise_claim": "UNKNOWN",
                "actor": "삼성전자",
                "citations": ["candidate"],
                "relevance_claim": "NONE",
                "relevance_citations": [],
            }
        )

    exit_code = worker_module.main(
        [
            "--root",
            str(tmp_path),
            "--now",
            NOW.isoformat(),
            "--routing-state",
            str(routing),
            "--quota-latest",
            str(quota),
            "--glm-env-file",
            str(env_file),
            "--max-items",
            "1",
        ],
        glm_transport=fake_chat,
    )
    assert exit_code == 0
    assert len(transports) == 1
    assert transports[0]["timeout"] == 600  # WORKER_CALL_TIMEOUT_SECONDS
    # the real env-file preflight fed the transport's api_key seam
    assert transports[0]["api_key"] == "worker-test-key"
    assert "삼성전자" in transports[0]["prompt"]
    assert news.connection.execute(
        "SELECT provider,model_id,state FROM classifier_calls"
    ).fetchall() == [("zai", "glm-5.3-flash", "SETTLED")]


def test_v2_policy_rows_persist_v1_output_schema_id(tmp_path):
    """J-14 부속 (SDS §D.4 M-1): work frozen under the v2 policy digest and
    its settled call rows keep the literal ``news-classifier-output-v1``
    output_schema_id — the STRICT DDL CHECK passes and no IntegrityError
    interrupts enqueue/settle. The version axis is the policy digest, never
    the persisted schema id."""
    from tokmo.programs.news_investigation import run_news_investigation

    news, ops = _stores(tmp_path)
    outcome = run_news_investigation(
        store=news,
        episode_id=None,
        candidate_id="ndc-v2-schema",
        purpose="EVENT_CLASS",
        evidence=EVIDENCE,
        prompt_policy_digest=POLICY_DIGEST,
        model_contract_digest=CONTRACT_DIGEST,
        configured_model_id=DEFAULT_NEWS_CLASSIFIER_MODEL_ID,
        admitted_catalog={DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER},
        classifier_call=_never_classifier,
        now=NOW,
        retry_max=0,
        ops_kernel=ops,
        role=ProducerRole.HERMES,
        activation_enabled=False,
        async_queue=news.enqueue_classifier_work,
    )
    identity = outcome["classification_identity"]
    work = news.classifier_work(identity)
    assert work["prompt_policy_digest"] == POLICY_DIGEST
    assert work["output_schema_id"] == "news-classifier-output-v1"

    def _ok_v2(request):
        return json.dumps(_glm_output(request, citations=[]))

    report = _worker(news, ops, classifier_call=_ok_v2)
    assert report["settled_ok"] == 1
    assert news.classifier_work(identity)["state"] == "DONE"
    rows = news.connection.execute(
        "SELECT output_schema_id,state,exit FROM classifier_calls"
    ).fetchall()
    assert rows == [("news-classifier-output-v1", "SETTLED", "OK")]


def test_pulse_deps_admitted_catalog_is_glm_only(tmp_path):
    # m2 (DI replacement): build the real production dependency bundle
    # and read its admitted catalog through the constructed object.
    from tokmo.programs.pulse_production_deps import build_pulse_production_deps
    from tokmo.values.session_gate import Market

    gate = tmp_path / "news_activation.json"
    gate.write_text(
        json.dumps(
            {
                "enabled": True,
                "cutoff": NOW.isoformat(),
                "approved_by": "woong",
                "producer_mode": "SHADOW",
            }
        )
    )
    os.chmod(gate, 0o600)
    deps = build_pulse_production_deps(
        root=tmp_path,
        clock=lambda: NOW,
        watchlist_symbols={Market.KR: frozenset(), Market.US: frozenset()},
        dialogue_cutoff=NOW,
    )
    producer = deps.news_producer_deps
    assert producer is not None
    assert dict(producer.admitted_catalog) == {
        DEFAULT_NEWS_CLASSIFIER_MODEL_ID: NEWS_CLASSIFIER_PROVIDER
    }


def test_preflight_reports_glm_route(tmp_path):
    from tokmo.programs.news_g3_preflight import read_news_preflight

    receipt = read_news_preflight(tmp_path, now=NOW)
    classifier = receipt["classifier"]
    assert classifier["configured_model"] == "glm-5.3-flash"
    assert classifier["provider"] == "zai"
    assert classifier["retry_max"] == 0


@pytest.mark.parametrize("flag", [None, 0, 1, "true", {}, []])
def test_zai_chat_once_json_mode_requires_bool_before_request(monkeypatch, flag):
    from tokmo.facts import news_http

    def forbidden(*args, **kwargs):
        pytest.fail("request constructed for invalid json_mode")

    monkeypatch.setattr(news_http, "Request", forbidden)
    with pytest.raises(TypeError, match="json_mode must be bool"):
        news_http.zai_chat_once("p", timeout_seconds=1, api_key="k", json_mode=flag)
