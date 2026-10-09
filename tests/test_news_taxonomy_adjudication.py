"""NEWS taxonomy stage-7 pending adjudication RED suite (2026-08-22).

Provider-pluggable batched adjudication over the durable PENDING_MODEL
queue: bounded deterministic batches, a durable job/call ledger with
replay-safe idempotency, strict all-or-nothing response validation, and
a provider-neutral transport protocol. Every transport in here is a fake
injected callable — no provider, no model, no network anywhere.
"""

import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from tokmo.programs.news_taxonomy_adjudication import (
    ADJUDICATION_BATCH_DEFAULT,
    ADJUDICATION_BATCH_HARD_MAX,
    ADJUDICATION_EXPIRY_DAYS,
    read_adjudication_config,
    run_taxonomy_adjudication_worker,
)
from tokmo.records.news_episode_store import (
    NewsEpisodeStore,
    NewsEpisodeStoreError,
)
from tokmo.values.news_taxonomy import (
    ADJUDICATION_VERDICTS_V1,
    adjudication_job_id,
    adjudication_response_digest,
    build_adjudication_batch_request,
    validate_adjudication_batch_response,
)

UTC = UTC
NOW = datetime(2026, 8, 22, 9, 0, tzinfo=UTC)


def _store(tmp_path):
    return NewsEpisodeStore(tmp_path / "stores" / "news_episodes.sqlite3")


def _pend(
    store, kind, unit_key, reason, evidence, *, display=None, canonical=None, at=NOW
):
    store.record_taxonomy_observation(
        {
            "kind": kind,
            "unit_key": unit_key,
            "canonical_key": canonical,
            "proposed_state": "PENDING_MODEL",
            "reason": reason,
            "display_text": display or unit_key,
            "evidence": evidence,
        },
        now=at,
    )


def _collision(store, alias, *, at=NOW):
    _pend(
        store,
        "ENTITY_ALIAS",
        alias,
        "ALIAS_COLLISION",
        {
            "by_canonical": {
                "ent:alpha": {"stories": ["s1"]},
                "ent:beta": {"stories": ["s2"]},
            }
        },
        display=alias,
        at=at,
    )


def _scoped_catalyst(store, i, *, at=NOW):
    _pend(
        store,
        "CATALYST",
        f"ent:nvidia|UNKNOWN|story-c{i}",
        "CATALYST_UNKNOWN_TYPE",
        {"subject": "ent:nvidia", "types": [], "story": f"story-c{i}"},
        display=f"엔비디아 신제품 발표 {i}차",
        at=at,
    )


def _predicted_effect(store, i, *, at=NOW):
    _pend(
        store,
        "EFFECT",
        f"ent:zeta|PREDICTED|story-e{i}",
        "EFFECT_PREDICTED_UNRESOLVED",
        {"subject": "ent:zeta", "predicted": True, "story": f"story-e{i}"},
        display=f"제타 급등 전망 {i}보",
        at=at,
    )


def _unscoped_topic(store, i, *, at=NOW):
    _pend(
        store,
        "TOPIC",
        f"무관 화제{i:03d}",
        "TOPIC_UNSCOPED_REPEATED",
        {"stories": [f"story-t{i}"]},
        at=at,
    )


def _unresolved_metric(store, i, *, at=NOW):
    _pend(
        store,
        "METRIC",
        f"|3.{i}|%||story-m{i}",
        "METRIC_SUBJECT_UNRESOLVED",
        {"subject": None, "value": f"3.{i}", "unit": "%", "story": f"story-m{i}"},
        display=f"무관 수치 {i} 기사",
        at=at,
    )


def _route_config(tmp_path, route_id="route-alpha", **overrides):
    path = tmp_path / "news_taxonomy_adjudication.json"
    payload = {"mode": "ROUTE", "route_id": route_id, **overrides}
    path.write_text(json.dumps(payload), encoding="utf-8")
    return read_adjudication_config(path)


class FakeTransport:
    """Injected fake adjudicator; records requests, returns per-unit
    verdicts, optionally rewritten per test."""

    def __init__(self, verdict="REJECTED", rewrite=None):
        self.requests = []
        self.verdict = verdict
        self.rewrite = rewrite

    def __call__(self, request):
        self.requests.append(request)
        results = [
            {
                "unit_id": unit["unit_id"],
                "verdict": self.verdict,
                "reason": "fake-adjudicated",
            }
            for unit in request["units"]
        ]
        response = {"v": 1, "results": results}
        return self.rewrite(request, response) if self.rewrite else response


def _run(store, config, transport, *, now=NOW, scope=("엔비디아",), **kwargs):
    return run_taxonomy_adjudication_worker(
        store=store,
        config=config,
        transport=transport,
        now=now,
        scope_terms=frozenset(scope),
        **kwargs,
    )


# ------------------------------------------------------------ schema/migration


def test_adjudication_ledger_migration_upgrades_existing_store(tmp_path):
    from tokmo.records.news_episode_store import _actual_ddl_digest

    path = tmp_path / "stores" / "news_episodes.sqlite3"
    store = _store(tmp_path)
    store.upsert("보존사건", (), NOW)
    _collision(store, "겹칭")
    store.close()
    connection = sqlite3.connect(path)
    connection.execute("DROP TABLE taxonomy_adjudication_units")
    connection.execute("DROP TABLE taxonomy_adjudication_jobs")
    connection.execute(
        "UPDATE schema_meta SET ddl_sha256=?", (_actual_ddl_digest(connection),)
    )
    connection.commit()
    connection.close()
    upgraded = NewsEpisodeStore(path)
    rows = upgraded.list_taxonomy_adjudication_eligible(limit=8)
    assert [row["unit_key"] for row in rows] == ["겹칭"]
    assert upgraded.upsert("보존사건", (), NOW).revision == 2
    upgraded.close()


# ------------------------------------------------- batch request / protocol


def test_batch_request_is_bounded_ordered_digested_and_neutral(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        _scoped_catalyst(store, i, at=NOW + timedelta(seconds=i))
    _collision(store, "겹칭")
    rows = store.list_taxonomy_adjudication_eligible(limit=8)
    request = build_adjudication_batch_request(rows)
    again = build_adjudication_batch_request(rows)
    assert request == again  # stable bytes for a stable row order
    assert request["v"] == 1
    assert len(request["request_digest"]) == 64
    assert list(request["unit_ids"]) == [row["unit_id"] for row in rows]
    assert [unit["unit_id"] for unit in request["units"]] == list(request["unit_ids"])
    job_id = adjudication_job_id(rows)
    assert len(job_id) == 64
    assert job_id == adjudication_job_id(tuple(reversed(rows)))
    flat = json.dumps(request, ensure_ascii=False).casefold()
    for forbidden in (
        "provider",
        "model",
        "gpt",
        "glm",
        "gemini",
        "openai",
        "anthropic",
    ):
        assert forbidden not in flat
    assert ADJUDICATION_VERDICTS_V1 == ("VERIFIED", "REJECTED", "DEFERRED")


def test_response_validation_rejects_whole_response_fail_closed(tmp_path):
    store = _store(tmp_path)
    _collision(store, "겹칭")
    _scoped_catalyst(store, 1)
    _unscoped_topic(store, 1)
    rows = store.list_taxonomy_adjudication_eligible(limit=8)
    request = build_adjudication_batch_request(rows)
    ids = list(request["unit_ids"])

    def ok(unit_id, **extra):
        return {"unit_id": unit_id, "verdict": "REJECTED"} | extra

    good = {"v": 1, "results": [ok(unit_id) for unit_id in ids]}
    assert len(validate_adjudication_batch_response(good, request)) == len(ids)
    topic_id = next(row["unit_id"] for row in rows if row["kind"] == "TOPIC")
    alias_id = next(row["unit_id"] for row in rows if row["kind"] == "ENTITY_ALIAS")
    catalyst_id = next(row["unit_id"] for row in rows if row["kind"] == "CATALYST")
    bad_responses = (
        {"v": 1, "results": [ok(unit_id) for unit_id in ids[:-1]]},  # missing
        {
            "v": 1,
            "results": [ok(unit_id) for unit_id in ids] + [ok(ids[0])],
        },  # duplicate
        {
            "v": 1,
            "results": [ok(unit_id) for unit_id in ids] + [ok("f" * 64)],
        },  # extra id
        {
            "v": 1,
            "results": [ok(unit_id) for unit_id in ids[:-1]]
            + [{"unit_id": ids[-1], "verdict": "MAYBE"}],
        },  # bad verdict
        {
            "v": 1,
            "results": [
                {
                    "unit_id": unit_id,
                    "verdict": "REJECTED",
                    "canonical_key": "ent:alpha",
                }
                if unit_id == alias_id
                else ok(unit_id)
                for unit_id in ids
            ],
        },  # canonical on REJECTED
        {
            "v": 1,
            "results": [
                {
                    "unit_id": unit_id,
                    "verdict": "VERIFIED",
                    "canonical_key": "ent:other",
                }
                if unit_id == topic_id
                else ok(unit_id)
                for unit_id in ids
            ],
        },  # canonical on a TOPIC
        {
            "v": 1,
            "results": [
                {
                    "unit_id": unit_id,
                    "verdict": "VERIFIED",
                    "canonical_key": "NOT_A_TYPE",
                }
                if unit_id == catalyst_id
                else ok(unit_id)
                for unit_id in ids
            ],
        },  # unknown catalyst type
        {
            "v": 1,
            "results": [
                {
                    "unit_id": unit_id,
                    "verdict": "VERIFIED",
                    "canonical_key": "ent:gamma",
                }
                if unit_id == alias_id
                else ok(unit_id)
                for unit_id in ids
            ],
        },  # canonical not observed
        {"v": 2, "results": [ok(unit_id) for unit_id in ids]},
        {"results": "everything fine"},
    )
    for response in bad_responses:
        with pytest.raises(ValueError):
            validate_adjudication_batch_response(response, request)


# --------------------------------------------------------------------- config


def test_default_missing_and_malformed_config_are_off(tmp_path):
    path = tmp_path / "news_taxonomy_adjudication.json"
    assert read_adjudication_config(path).mode == "OFF"  # missing
    path.write_text('{"mode": "OFF"}', encoding="utf-8")
    assert read_adjudication_config(path).mode == "OFF"  # explicit
    for malformed in (
        "{not json",
        '{"mode": "ROUTE"}',  # no route_id
        '{"mode": "ROUTE", "route_id": "A B"}',  # bad slug
        '{"mode": "ROUTE", "route_id": "r1", "batch_max": 13}',
        '{"mode": "ROUTE", "route_id": "r1", "max_starts": 9}',
        '{"mode": "ROUTE", "route_id": "r1", "retry_max": 5}',
        '{"mode": "ROUTE", "route_id": "r1", "extra": true}',
        '{"mode": "ROUTE", "route_id": "r1", "batch_max": true}',
        '{"mode": "AUTO", "route_id": "r1"}',
        '{"mode": "OFF", "route_id": "r1"}',
        '["ROUTE"]',
    ):
        path.write_text(malformed, encoding="utf-8")
        config = read_adjudication_config(path)
        assert config.mode == "OFF", malformed
        assert config.route_id is None
    path.write_text(
        json.dumps(
            {
                "mode": "ROUTE",
                "route_id": "route-alpha",
                "batch_max": 12,
                "max_starts": 2,
                "retry_max": 1,
            }
        ),
        encoding="utf-8",
    )
    config = read_adjudication_config(path)
    assert (config.mode, config.route_id) == ("ROUTE", "route-alpha")
    assert (config.batch_max, config.max_starts, config.retry_max) == (12, 2, 1)
    assert ADJUDICATION_BATCH_DEFAULT == 8
    assert ADJUDICATION_BATCH_HARD_MAX == 12


def test_off_config_starts_zero_and_never_touches_transport(tmp_path):
    store = _store(tmp_path)
    for i in range(12):
        _scoped_catalyst(store, i)
    path = tmp_path / "news_taxonomy_adjudication.json"

    def bomb(_request):
        raise AssertionError("transport must never be called while OFF")

    for content in (None, '{"mode": "OFF"}', "{malformed"):
        if content is not None:
            path.write_text(content, encoding="utf-8")
        config = read_adjudication_config(path)
        report = _run(store, config, bomb)
        assert report["started"] == 0 and report["requested"] == 0
    assert store.list_taxonomy_adjudication_jobs() == ()
    assert len(store.list_taxonomy_pending(limit=99)) == 12


def test_route_without_bound_transport_starts_zero(tmp_path):
    store = _store(tmp_path)
    _scoped_catalyst(store, 1)
    config = _route_config(tmp_path)
    report = _run(store, config, None)
    assert report["transport_bound"] is False
    assert report["started"] == 0
    assert store.list_taxonomy_adjudication_jobs() == ()


# ------------------------------------------------------------- caps/priority


def test_default_caps_one_start_eight_units(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        _scoped_catalyst(store, i, at=NOW + timedelta(seconds=i))
    transport = FakeTransport("REJECTED")
    report = _run(store, _route_config(tmp_path), transport)
    assert len(transport.requests) == 1
    assert len(transport.requests[0]["units"]) == ADJUDICATION_BATCH_DEFAULT
    assert report["requested"] == 8 and report["started"] == 1
    assert report["settled"] == 1 and report["failed"] == 0
    assert report["rejected"] == 8
    assert len(store.list_taxonomy_pending(limit=99)) == 12


def test_priority_scoped_and_ambiguous_before_unscoped_generic(tmp_path):
    store = _store(tmp_path)
    base = NOW - timedelta(days=2)
    for i in range(6):
        _unscoped_topic(store, i, at=base)  # oldest, low value
        _unresolved_metric(store, i, at=base)
    for i in range(5):
        _predicted_effect(store, i, at=NOW - timedelta(hours=2, seconds=-i))
    for i in range(4):
        _scoped_catalyst(store, i, at=NOW - timedelta(hours=1, seconds=-i))
    _collision(store, "겹칭", at=NOW)  # newest
    transport = FakeTransport("DEFERRED")
    _run(store, _route_config(tmp_path), transport)
    (request,) = transport.requests
    kinds = [unit["kind"] for unit in request["units"]]
    # collision/ambiguity + scoped relevance fill the batch before typed
    # unscoped items; unscoped generic topics/metrics never enter at all.
    assert kinds.count("ENTITY_ALIAS") == 1
    assert kinds.count("CATALYST") == 4
    assert kinds.count("EFFECT") == 3
    assert "TOPIC" not in kinds and "METRIC" not in kinds
    # grouped deterministically: ambiguity class first, then scoped class,
    # each internally grouped by kind.
    assert kinds[0] == "ENTITY_ALIAS"
    assert kinds[1:5] == ["CATALYST"] * 4


def test_unscoped_generic_expires_deferred_without_deletion(tmp_path):
    store = _store(tmp_path)
    for i in range(3):
        _unscoped_topic(store, i, at=NOW - timedelta(days=ADJUDICATION_EXPIRY_DAYS + 1))
    _unscoped_topic(store, 90, at=NOW - timedelta(days=1))

    def bomb(_request):
        raise AssertionError("low-value pending must never reach a model")

    report = _run(store, _route_config(tmp_path), bomb)
    assert report["started"] == 0
    assert report["expired_deferred"] == 3
    # History preserved: units stay PENDING_MODEL with an appended
    # decision, never deleted — but they leave the adjudication queue.
    assert len(store.list_taxonomy_pending(limit=99)) == 4
    eligible = store.list_taxonomy_adjudication_eligible(limit=99)
    assert [row["unit_key"] for row in eligible] == ["무관 화제090"]
    expired = store.taxonomy_unit("TOPIC", "무관 화제000")
    assert expired["state"] == "PENDING_MODEL"
    decisions = store.taxonomy_decisions(expired["unit_id"])
    assert decisions[-1]["reason"] == "ADJUDICATION_EXPIRED_LOW_VALUE"
    # A later worker run performs no further expiry work for them.
    rerun = _run(store, _route_config(tmp_path), bomb)
    assert rerun["expired_deferred"] == 0


# ------------------------------------------------- job ledger / idempotency


def test_same_pending_digest_cannot_start_twice(tmp_path):
    store = _store(tmp_path)
    _scoped_catalyst(store, 1)
    rows = store.list_taxonomy_adjudication_eligible(limit=8)
    request = build_adjudication_batch_request(rows)
    job_id = adjudication_job_id(rows)

    def begin(retry_max=0):
        return store.begin_taxonomy_adjudication(
            job_id,
            request_digest=request["request_digest"],
            unit_ids=request["unit_ids"],
            route_id="route-alpha",
            now=NOW,
            retry_max=retry_max,
        )

    assert begin() == "STARTED"
    assert begin() == "IN_FLIGHT"  # never a concurrent second
    # Crash recovery: a stale STARTED job reconciles to explicit UNKNOWN.
    assert (
        store.reconcile_taxonomy_adjudication(
            now=NOW + timedelta(hours=1), stale_after_seconds=900
        )
        == 1
    )
    job = store.taxonomy_adjudication_job(job_id)
    assert job["state"] == "UNKNOWN" and job["outcome"] == "CRASH_UNKNOWN"
    assert begin(retry_max=0) == "EXHAUSTED"  # default retry=0
    assert begin(retry_max=1) == "STARTED"  # explicit RETRYABLE budget
    assert store.taxonomy_adjudication_job(job_id)["attempts"] == 2


def test_settled_replay_performs_no_new_transition_or_call(tmp_path):
    store = _store(tmp_path)
    _scoped_catalyst(store, 1)
    rows = store.list_taxonomy_adjudication_eligible(limit=8)
    request = build_adjudication_batch_request(rows)
    job_id = adjudication_job_id(rows)
    transport = FakeTransport("REJECTED")
    report = _run(store, _route_config(tmp_path), transport)
    assert report["settled"] == 1 and len(transport.requests) == 1
    unit = store.taxonomy_unit("CATALYST", rows[0]["unit_key"])
    assert unit["state"] == "REJECTED_MODEL"
    decisions_before = store.taxonomy_decisions(unit["unit_id"])
    # Replay of the settled batch: refused at begin, no transition applied.
    assert (
        store.begin_taxonomy_adjudication(
            job_id,
            request_digest=request["request_digest"],
            unit_ids=request["unit_ids"],
            route_id="route-alpha",
            now=NOW + timedelta(minutes=5),
        )
        == "REPLAY_SETTLED"
    )
    with pytest.raises(NewsEpisodeStoreError):
        store.apply_taxonomy_adjudication(
            job_id,
            (
                {
                    "unit_id": rows[0]["unit_id"],
                    "verdict": "REJECTED",
                    "reason": "again",
                    "canonical_key": None,
                },
            ),
            response_digest="0" * 64,
            now=NOW + timedelta(minutes=5),
        )
    assert store.taxonomy_decisions(unit["unit_id"]) == decisions_before
    rerun = _run(
        store, _route_config(tmp_path), transport, now=NOW + timedelta(minutes=10)
    )
    assert rerun["started"] == 0 and len(transport.requests) == 1


def test_invalid_response_settles_failed_with_zero_partial_settlement(tmp_path):
    store = _store(tmp_path)
    for i in range(4):
        _scoped_catalyst(store, i)

    def drop_last(_request, response):
        response["results"] = response["results"][:-1]
        return response

    transport = FakeTransport("VERIFIED", rewrite=drop_last)
    report = _run(store, _route_config(tmp_path), transport)
    assert report["failed"] == 1 and report["settled"] == 0
    assert report["verified"] == 0
    (job,) = store.list_taxonomy_adjudication_jobs()
    assert job["state"] == "FAILED" and job["outcome"] == "INVALID_RESPONSE"
    # Whole-response fail-closed: not one unit settled.
    assert len(store.list_taxonomy_pending(limit=99)) == 4
    assert all(
        row["state"] == "PENDING_MODEL"
        for row in store.list_taxonomy_units(kind="CATALYST")
    )


def test_transport_exception_settles_failed_and_units_stay_pending(tmp_path):
    store = _store(tmp_path)
    _scoped_catalyst(store, 1)

    def explode(_request):
        raise RuntimeError("socket burnt")

    report = _run(store, _route_config(tmp_path), explode)
    assert report["started"] == 1 and report["failed"] == 1
    (job,) = store.list_taxonomy_adjudication_jobs()
    assert job["state"] == "FAILED" and job["outcome"] == "TRANSPORT_FAIL"
    assert len(store.list_taxonomy_pending(limit=9)) == 1
    # Default retry=0: the identical batch never restarts by itself.
    rerun = _run(
        store, _route_config(tmp_path), explode, now=NOW + timedelta(minutes=30)
    )
    assert rerun["started"] == 0
    assert rerun["exhausted_deferred"] == 1


# ------------------------------------------------------- verdict application


def test_valid_batch_applies_verdicts_atomically_with_provenance(tmp_path):
    store = _store(tmp_path)
    _collision(store, "겹칭", at=NOW - timedelta(minutes=3))
    _scoped_catalyst(store, 1, at=NOW - timedelta(minutes=2))
    _predicted_effect(store, 1, at=NOW - timedelta(minutes=1))

    def verdicts(request, response):
        by_kind = {unit["unit_id"]: unit["kind"] for unit in request["units"]}
        for result in response["results"]:
            kind = by_kind[result["unit_id"]]
            if kind == "ENTITY_ALIAS":
                result.update(verdict="VERIFIED", canonical_key="ent:alpha")
            elif kind == "CATALYST":
                result.update(verdict="VERIFIED", canonical_key="EARNINGS_RELEASE")
            else:
                result.update(verdict="DEFERRED")
        return response

    transport = FakeTransport("REJECTED", rewrite=verdicts)
    report = _run(store, _route_config(tmp_path), transport, scope=("엔비디아", "제타"))
    assert report["settled"] == 1
    assert (report["verified"], report["rejected"], report["deferred"]) == (2, 0, 1)
    alias = store.taxonomy_unit("ENTITY_ALIAS", "겹칭")
    assert alias["state"] == "VERIFIED_MODEL"
    assert alias["canonical_key"] == "ent:alpha"
    catalyst = store.taxonomy_unit("CATALYST", "ent:nvidia|UNKNOWN|story-c1")
    assert catalyst["state"] == "VERIFIED_MODEL"
    assert catalyst["canonical_key"] == "EARNINGS_RELEASE"
    effect = store.taxonomy_unit("EFFECT", "ent:zeta|PREDICTED|story-e1")
    assert effect["state"] == "PENDING_MODEL"  # deferred, never deleted
    assert (
        store.taxonomy_decisions(effect["unit_id"])[-1]["to_state"] == "PENDING_MODEL"
    )
    # Deferred with unchanged evidence leaves the adjudication queue.
    assert store.list_taxonomy_adjudication_eligible(limit=9) == ()
    (job,) = store.list_taxonomy_adjudication_jobs()
    assert job["state"] == "SETTLED" and job["outcome"] == "OK"
    assert len(job["response_digest"]) == 64


def test_adjudication_keeps_unit_evidence_immutable_verdict_in_decisions(tmp_path):
    """B1: a verdict must never rewrite the unit's original observation
    evidence. ``taxonomy_units.payload_json``/``evidence_digest`` stay the
    immutable original observation pair across VERIFIED (with canonical)
    and REJECTED; only the dedicated ``canonical_key`` column may change;
    verdict evidence and its digest are appended to taxonomy_decisions;
    job identity/dedupe keep keying the original evidence digest."""
    from tokmo.values.news_taxonomy import taxonomy_evidence_digest

    path = tmp_path / "stores" / "news_episodes.sqlite3"
    store = _store(tmp_path)
    _collision(store, "겹칭")  # → VERIFIED + canonical
    _scoped_catalyst(store, 1)  # → REJECTED
    before = {
        row["unit_id"]: dict(row)
        for row in store.list_taxonomy_adjudication_eligible(limit=8)
    }
    job_id = adjudication_job_id(tuple(before.values()))
    for row in before.values():  # sane precondition
        assert (
            taxonomy_evidence_digest(json.loads(row["payload_json"]))
            == row["evidence_digest"]
        )

    def verdicts(request, response):
        by_kind = {unit["unit_id"]: unit["kind"] for unit in request["units"]}
        for result in response["results"]:
            if by_kind[result["unit_id"]] == "ENTITY_ALIAS":
                result.update(verdict="VERIFIED", canonical_key="ent:alpha")
        return response

    report = _run(
        store, _route_config(tmp_path), FakeTransport("REJECTED", rewrite=verdicts)
    )
    assert (report["settled"], report["verified"], report["rejected"]) == (1, 1, 1)

    def check_units(current_store):
        alias = current_store.taxonomy_unit("ENTITY_ALIAS", "겹칭")
        catalyst = current_store.taxonomy_unit(
            "CATALYST", "ent:nvidia|UNKNOWN|story-c1"
        )
        assert alias["state"] == "VERIFIED_MODEL"
        assert alias["canonical_key"] == "ent:alpha"  # dedicated column
        assert catalyst["state"] == "REJECTED_MODEL"
        for unit in (alias, catalyst):
            original = before[unit["unit_id"]]
            # Original observation evidence byte-intact, digest unchanged,
            # and the payload↔digest pair still self-consistent.
            assert unit["payload_json"] == original["payload_json"]
            assert unit["evidence_digest"] == original["evidence_digest"]
            assert (
                taxonomy_evidence_digest(json.loads(unit["payload_json"]))
                == unit["evidence_digest"]
            )
        assert json.loads(alias["payload_json"]) == {
            "by_canonical": {
                "ent:alpha": {"stories": ["s1"]},
                "ent:beta": {"stories": ["s2"]},
            }
        }
        # Verdict evidence + digest live in the appended decision row.
        verdict_evidence = {
            alias["unit_id"]: {
                "verdict": "VERIFIED",
                "reason": "fake-adjudicated",
                "canonical_key": "ent:alpha",
            },
            catalyst["unit_id"]: {"verdict": "REJECTED", "reason": "fake-adjudicated"},
        }
        for unit in (alias, catalyst):
            decision = current_store.taxonomy_decisions(unit["unit_id"])[-1]
            evidence = verdict_evidence[unit["unit_id"]]
            assert decision["from_state"] == "PENDING_MODEL"
            assert decision["to_state"] == unit["state"]
            assert json.loads(decision["evidence_json"]) == evidence
            assert decision["evidence_digest"] == taxonomy_evidence_digest(evidence)
            assert decision["evidence_digest"] != unit["evidence_digest"]
        # Job identity/dedupe still key the original unit evidence digest.
        assert adjudication_job_id((alias, catalyst)) == job_id
        job = current_store.taxonomy_adjudication_job(job_id)
        assert job["state"] == "SETTLED"
        assert len(job["response_digest"]) == 64  # operational ledger only
        return alias, catalyst

    alias, catalyst = check_units(store)
    decisions_before = {
        unit["unit_id"]: store.taxonomy_decisions(unit["unit_id"])
        for unit in (alias, catalyst)
    }
    # Replay is idempotent: no second transition, no evidence mutation.
    assert (
        store.begin_taxonomy_adjudication(
            job_id,
            request_digest="0" * 64,
            unit_ids=tuple(before),
            route_id="route-alpha",
            now=NOW + timedelta(minutes=5),
        )
        == "REPLAY_SETTLED"
    )
    rerun = _run(
        store,
        _route_config(tmp_path),
        FakeTransport("REJECTED", rewrite=verdicts),
        now=NOW + timedelta(minutes=10),
    )
    assert rerun["started"] == 0
    check_units(store)
    for unit in (alias, catalyst):
        assert (
            store.taxonomy_decisions(unit["unit_id"])
            == decisions_before[unit["unit_id"]]
        )
    # Reopen integrity: the migration/verification path on a fresh open
    # sees the same immutable pair and history.
    store.close()
    reopened = NewsEpisodeStore(path)
    check_units(reopened)
    for unit in (alias, catalyst):
        assert (
            reopened.taxonomy_decisions(unit["unit_id"])
            == decisions_before[unit["unit_id"]]
        )
    reopened.close()


def test_next_run_is_bounded_separately_with_no_duplicates(tmp_path):
    store = _store(tmp_path)
    for i in range(20):
        _scoped_catalyst(store, i, at=NOW + timedelta(seconds=i))
    transport = FakeTransport("REJECTED")
    first = _run(store, _route_config(tmp_path), transport)
    second = _run(
        store, _route_config(tmp_path), transport, now=NOW + timedelta(hours=1)
    )
    assert first["started"] == 1 and second["started"] == 1
    batch_one = set(transport.requests[0]["unit_ids"])
    batch_two = set(transport.requests[1]["unit_ids"])
    assert len(batch_one) == 8 and len(batch_two) == 8
    assert batch_one.isdisjoint(batch_two)
    assert len(store.list_taxonomy_pending(limit=99)) == 4


# ---------------------------------------------------- provider pluggability


def test_provider_switch_changes_route_ledger_only(tmp_path):
    from tokmo.records.news_episode_store import _actual_ddl_digest

    store = _store(tmp_path)
    for i in range(16):
        _scoped_catalyst(store, i, at=NOW + timedelta(seconds=i))
    digest_before = _actual_ddl_digest(store.connection)
    transport_a = FakeTransport("REJECTED")
    _run(store, _route_config(tmp_path, "route-alpha"), transport_a)
    transport_b = FakeTransport("REJECTED")
    _run(
        store,
        _route_config(tmp_path, "route-beta"),
        transport_b,
        now=NOW + timedelta(hours=1),
    )
    assert len(transport_a.requests) == 1 and len(transport_b.requests) == 1
    routes = sorted(job["route_id"] for job in store.list_taxonomy_adjudication_jobs())
    assert routes == ["route-alpha", "route-beta"]
    # Domain bytes/schema unchanged: same DDL digest, and no route or
    # transport identifier anywhere in taxonomy unit identity or history.
    assert _actual_ddl_digest(store.connection) == digest_before
    domain_rows = (
        store.connection.execute(
            "SELECT group_concat(unit_id||kind||unit_key||display_text"
            "||payload_json||state||reason) FROM taxonomy_units"
        ).fetchone()[0]
        + store.connection.execute(
            "SELECT coalesce(group_concat(reason||evidence_json),'')"
            " FROM taxonomy_decisions"
        ).fetchone()[0]
    )
    assert "route-alpha" not in domain_rows
    assert "route-beta" not in domain_rows


def test_receipts_live_only_in_operational_job_ledger(tmp_path):
    store = _store(tmp_path)
    _scoped_catalyst(store, 1)
    transport = FakeTransport("REJECTED")
    _run(store, _route_config(tmp_path), transport)
    (job,) = store.list_taxonomy_adjudication_jobs()
    assert job["route_id"] == "route-alpha"
    assert len(job["request_digest"]) == 64
    assert job["response_digest"] == adjudication_response_digest(
        {
            "v": 1,
            "results": [
                {
                    "unit_id": transport.requests[0]["unit_ids"][0],
                    "verdict": "REJECTED",
                    "reason": "fake-adjudicated",
                }
            ],
        }
    )
    assert job["started_at"] and job["settled_at"]


# ------------------------------------------------------------------ CLI seam


def test_cli_is_inert_off_by_default_and_route_unbound(tmp_path, capsys):
    from tokmo.programs.news_taxonomy_adjudication import main

    root = tmp_path / "runtime"
    root.mkdir()
    assert main(["--root", str(root), "--now", NOW.isoformat()]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "OFF" and report["started"] == 0
    (root / "news_taxonomy_adjudication.json").write_text(
        '{"mode": "ROUTE", "route_id": "route-alpha"}', encoding="utf-8"
    )
    assert main(["--root", str(root), "--now", NOW.isoformat()]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["mode"] == "ROUTE"
    assert report["transport_bound"] is False and report["started"] == 0


# --------------------------------------------- production-faithful shadow


def test_shadow_93_pending_one_call_eight_units_replay_clean(tmp_path):
    """Mirror of the observed 24h corpus shape: 93 PENDING_MODEL, 29
    VERIFIED_AUTO, 3 CANDIDATE. One worker run makes exactly one fake
    provider start over at most 8 units; a second run is bounded
    separately and never re-adjudicates a settled unit."""
    store = _store(tmp_path)
    base = NOW - timedelta(hours=20)
    for i in range(3):
        _collision(store, f"겹칭{i}", at=base + timedelta(minutes=i))
    for i in range(5):
        _scoped_catalyst(store, i, at=base + timedelta(minutes=10 + i))
    for i in range(10):
        _predicted_effect(store, i, at=base + timedelta(minutes=20 + i))
    for i in range(60):
        _unscoped_topic(store, i, at=base + timedelta(minutes=40 + i))
    for i in range(15):
        _unresolved_metric(store, i, at=base + timedelta(minutes=110 + i))
    for i in range(29):
        store.record_taxonomy_observation(
            {
                "kind": "TOPIC",
                "unit_key": f"검증 화제{i:02d}",
                "canonical_key": None,
                "proposed_state": "VERIFIED_AUTO",
                "reason": "TOPIC_SCOPED_REPEATED",
                "display_text": f"검증 화제{i:02d}",
                "evidence": {"stories": [f"v{i}"]},
            },
            now=base,
        )
    for i in range(3):
        store.record_taxonomy_observation(
            {
                "kind": "ENTITY_ALIAS",
                "unit_key": f"후보{i}",
                "canonical_key": "ent:alpha",
                "proposed_state": "CANDIDATE",
                "reason": "ALIAS_SINGLE_SIGHTING",
                "display_text": f"후보{i}",
                "evidence": {"stories": [f"c{i}"]},
            },
            now=base,
        )
    assert len(store.list_taxonomy_pending(limit=200)) == 93
    transport = FakeTransport("REJECTED")
    report = _run(store, _route_config(tmp_path), transport)
    assert len(transport.requests) == 1  # ≤1 provider start
    assert len(transport.requests[0]["units"]) <= 8  # ≤8 units
    assert report["started"] == 1 and report["requested"] <= 8
    adjudicated_first = set(transport.requests[0]["unit_ids"])
    verified_or_candidate = {
        row["unit_id"]
        for row in store.list_taxonomy_units()
        if row["state"] in ("VERIFIED_AUTO", "CANDIDATE")
    }
    assert adjudicated_first.isdisjoint(verified_or_candidate)
    # Second run: bounded separately, zero duplicate adjudication.
    second = _run(
        store, _route_config(tmp_path), transport, now=NOW + timedelta(hours=1)
    )
    assert second["started"] <= 1
    assert set(transport.requests[-1]["unit_ids"]).isdisjoint(adjudicated_first)
    for unit_id in adjudicated_first:
        transitions = [
            decision
            for decision in store.taxonomy_decisions(unit_id)
            if decision["to_state"] == "REJECTED_MODEL"
        ]
        assert len(transitions) == 1
