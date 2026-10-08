from dataclasses import replace
from datetime import datetime, timezone
import json
import sqlite3
import pytest

from tokmo.programs.analysis_pipeline import run_analysis_pipeline
import tokmo.programs.analysis_pipeline as pipeline_module
from tokmo.values.portfolio_scope import resolve_account_scope_pattern
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_contracts import (
    AnalysisRunState, ModelCallStatus, ProviderExit,
)
from tokmo.values.ops_kernel import IncidentKeyV1
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.session_gate import Market
from test_candidate_builder import _barrier

NOW = datetime(2026, 7, 30, tzinfo=timezone.utc)


def _ids(body):
    return [row["candidate_id"] for row in json.loads(body)["candidates"]]


def glm_ok(body):
    return json.dumps({
        "schema_version": 1,
        "annotations": [{
            "candidate_id": candidate_id, "relevance": "NONE",
            "materiality": "LOW", "urgency": "NONE",
            "evidence_unknowns": [], "summary": "screened",
        } for candidate_id in _ids(body)],
    }, separators=(",", ":")).encode()


def sol_ok(body):
    payload = json.loads(body)
    return json.dumps({
        "schema_version": 1,
        "analyses": [{
            "candidate_id": row["candidate_id"], "verdict": "ALERT_WORTHY",
            "proposed_action": "ALERT", "rationale": "verified",
            "cited_evidence_ids": [],
            "confidence": "1", "disagreements_with_glm": [],
        } for row in payload["candidates"]],
    }, separators=(",", ":")).encode()


def _sol_with(action, *, evidence_id=None):
    def stub(body):
        payload = json.loads(body)
        return json.dumps({
            "schema_version": 1,
            "analyses": [{
                "candidate_id": row["candidate_id"],
                "verdict": "ALERT_WORTHY",
                "proposed_action": action,
                "rationale": "matrix",
                "cited_evidence_ids": ([evidence_id()] if evidence_id else []),
                "confidence": "1",
                "disagreements_with_glm": [],
            } for row in payload["candidates"]],
        }, separators=(",", ":")).encode()
    return stub


def _only_assignment(monkeypatch, assignment):
    original = pipeline_module.build_candidates
    selected_evidence = {"id": None}

    def selected(*args, **kwargs):
        built = original(*args, **kwargs)
        row = next(item for item in built.candidates
                   if item.assignment.value == assignment)
        selected_evidence["id"] = row.evidence_refs[0].evidence_id
        return type(built)(
            built.material_binding_digest, (row,), built.baseline_advances,
        )

    monkeypatch.setattr(pipeline_module, "build_candidates", selected)
    return lambda: selected_evidence["id"]


def run(tmp_path, *, glm_call=glm_ok, sol_call=sol_ok, invocation="a" * 32):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    barrier = _barrier()
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: invocation,
        glm_call=glm_call, sol_call=sol_call, now=NOW, clock=lambda: NOW,
        resolve_scope=resolve_account_scope_pattern,
    )
    return store, outcome


def test_public_pipeline_seals_calls_injected_stubs_and_terminalizes_all(tmp_path):
    calls = {"glm": 0, "sol": 0}

    def glm(body):
        calls["glm"] += 1
        return glm_ok(body)

    def sol(body):
        calls["sol"] += 1
        return sol_ok(body)

    store, outcome = run(tmp_path, glm_call=glm, sol_call=sol)
    assert outcome.state is AnalysisRunState.COMPLETED
    assert outcome.candidate_count == outcome.disposition_count == 2
    assert calls == {"glm": 1, "sol": 1}
    assert store.read_manifest_summary(10).state.value == "SEALED"
    assert {row.state.value for row in store.read_manifest_candidates(10)} <= {
        "DEFERRED", "DISPOSITIONED", "TERMINAL",
    }
    assert all(row["state"] == "SETTLED" for row in store.read_analysis_batches(10))


def test_parser_failure_is_durable_and_never_reaches_network(tmp_path):
    store, outcome = run(
        tmp_path, glm_call=lambda _body: b"{",
        sol_call=lambda _body: (ProviderExit.TIMEOUT, None),
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    batches = store.read_analysis_batches(10)
    assert batches[0]["exit"] == "PARSE_FAIL"
    assert all(row.state.value != "PENDING" for row in store.read_manifest_candidates(10))


def test_same_tick_replay_uses_durable_batches_without_stub_recall(tmp_path):
    store, first = run(tmp_path)
    assert first.state is AnalysisRunState.COMPLETED

    def forbidden(_body):
        raise AssertionError("settled batch must not be called again")

    barrier = _barrier()
    second = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=forbidden, sol_call=forbidden, now=NOW, clock=lambda: NOW,
    )
    assert second.state is AnalysisRunState.COMPLETED


def test_same_tick_manifest_preflight_precedes_baseline_reads(tmp_path):
    store, first = run(tmp_path)
    assert first.state is AnalysisRunState.COMPLETED
    original = store.read_subject_baselines

    def forbidden(*args, **kwargs):
        raise AssertionError("same-tick manifest replay must not read baselines")

    store.read_subject_baselines = forbidden
    barrier = _barrier()
    second = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        sol_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        now=NOW, clock=lambda: NOW,
    )
    store.read_subject_baselines = original
    assert second.state is AnalysisRunState.COMPLETED


def test_started_crash_reopen_terminalizes_without_stub_recall(tmp_path):
    calls = {"glm": 0}

    def crash(_body):
        calls["glm"] += 1
        raise RuntimeError("process died")

    with pytest.raises(RuntimeError, match="process died"):
        run(tmp_path, glm_call=crash)
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    barrier = _barrier()
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        sol_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    assert calls == {"glm": 1}
    assert all(row.state.value != "PENDING" for row in store.read_manifest_candidates(10))


def test_stale_started_batch_emits_one_stable_unknown_incident_on_reopen(
    tmp_path, monkeypatch,
):
    _only_assignment(monkeypatch, "CRITICAL")

    def crash(_body):
        raise RuntimeError("process died")

    with pytest.raises(RuntimeError, match="process died"):
        run(tmp_path, glm_call=crash)

    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    barrier = _barrier(tick=11)
    reopened = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert reopened.state is AnalysisRunState.COMPLETED
    unknown_batch, = store.read_analysis_batches(10)
    assert (unknown_batch["provider"], unknown_batch["state"]) == (
        "GLM", "UNKNOWN",
    )
    candidates = store.read_manifest_candidates(10)
    dispositions = store.read_dispositions_for_tick(10)
    assert len(candidates) == len(dispositions) == 1
    assert all(row.state.value != "PENDING" for row in candidates)
    assert {(row.glm_status, row.sol_status) for row in dispositions} == {
        ("OUTCOME_UNKNOWN", "OUTCOME_UNKNOWN"),
    }

    key = IncidentKeyV1("model-call-unknown", "glm-5.2")
    incident = store.read_incident(key)
    assert incident.impact_ref == "10:0"
    assert incident.count == 1
    with sqlite3.connect(tmp_path / "ops" / "kernel.sqlite3") as connection:
        rows = connection.execute(
            "SELECT channel,subject_id FROM alert_outbox "
            "WHERE incident_kind=? AND incident_subject=?",
            (key.kind, key.subject),
        ).fetchall()
    assert rows == [("SHADOW", "glm-5.2")]
    assert all(
        private not in f"{key.subject}:{incident.impact_ref}"
        for private in ("token", "secret", "key", "http", "/", "\\")
    )

    replayed = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "c" * 32,
        glm_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        sol_call=lambda _body: (_ for _ in ()).throw(AssertionError("no recall")),
        now=NOW, clock=lambda: NOW,
    )
    assert replayed.state is AnalysisRunState.COMPLETED
    assert store.read_incident(key).count == 1
    with sqlite3.connect(tmp_path / "ops" / "kernel.sqlite3") as connection:
        assert connection.execute(
            "SELECT count(*) FROM alert_outbox "
            "WHERE incident_kind=? AND incident_subject=?",
            (key.kind, key.subject),
        ).fetchone()[0] == 1


def test_auth_fail_incident_is_provider_stable_deduped_and_resolved(tmp_path):
    store, first = run(
        tmp_path, glm_call=lambda _body: (ProviderExit.AUTH_FAIL, None),
    )
    assert first.state is AnalysisRunState.COMPLETED
    key = IncidentKeyV1("model-auth-unavailable", "glm-5.2")
    incident = store.read_incident(key)
    assert incident.impact_ref == "model-auth:glm-5.2"
    assert incident.count == 1

    barrier = _barrier(tick=11)
    second = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=lambda _body: (ProviderExit.AUTH_FAIL, None),
        sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert second.state is AnalysisRunState.COMPLETED
    assert store.read_incident(key).count == 2

    barrier = _barrier(tick=12)
    third = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "c" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert third.state is AnalysisRunState.COMPLETED
    assert store.read_incident(key).state.value == "RESOLVED"


def test_closed_market_backlog_waits_then_runs_once_via_public_entrypoint(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    calls = {"glm": 0, "sol": 0, "due": 0}
    original_due = store.read_due_deferred

    def counted_due(*args):
        calls["due"] += 1
        return original_due(*args)

    store.read_due_deferred = counted_due

    def glm(body):
        calls["glm"] += 1
        return glm_ok(body)

    def sol(body):
        calls["sol"] += 1
        return sol_ok(body)

    for tick_id in range(10, 70):
        source = _barrier(tick=tick_id)
        closed = replace(
            source,
            market_readiness={
                Market.KR: MarketReadiness.SKIP_CLOSED,
                Market.US: MarketReadiness.SKIP_CLOSED,
            },
            proceed_markets=frozenset(),
        )
        outcome = run_analysis_pipeline(
            tick=closed, barrier=closed, store=store,
            run_lock_path=tmp_path / "run" / "analysis.lock",
            invocation_id_factory=lambda tick_id=tick_id: f"{tick_id:032x}",
            glm_call=glm, sol_call=sol, now=NOW, clock=lambda: NOW,
        )
        assert outcome.state is AnalysisRunState.COMPLETED

    history = store.read_deferred_attempts(
        next(
            row.subject_id for row in store.read_manifest_candidates(10)
            if row.assignment.value == "EXPLICITLY_DEFERRED"
        )
    )
    assert len(history) == 1
    assert history[0].market is Market.KR
    assert calls == {"glm": 0, "sol": 0, "due": 0}

    ready = _barrier(tick=70)
    run_analysis_pipeline(
        tick=ready, barrier=ready, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "f" * 32,
        glm_call=glm, sol_call=sol, now=NOW, clock=lambda: NOW,
    )
    assert calls["glm"] == calls["sol"] == 1
    attempts = store.read_deferred_attempts(history[0].subject_id)
    assert len(attempts) == 2
    assert attempts[1].reason_code == "next_session_defer"
    assert attempts[1].attempt_tick == 70
    assert store.read_due_deferred(Market.KR, NOW) == ()

    next_tick = _barrier(tick=71)
    run_analysis_pipeline(
        tick=next_tick, barrier=next_tick, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "e" * 32,
        glm_call=glm, sol_call=sol, now=NOW, clock=lambda: NOW,
    )
    assert len(store.read_deferred_attempts(history[0].subject_id)) == 2


def test_closed_market_crash_reopen_has_zero_model_calls_and_no_duplicate_attempt(
    tmp_path,
):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    source = _barrier(tick=10)
    closed = replace(
        source,
        market_readiness={
            Market.KR: MarketReadiness.SKIP_CLOSED,
            Market.US: MarketReadiness.SKIP_CLOSED,
        },
        proceed_markets=frozenset(),
    )
    original = store.create_manifest_with_baselines

    def crash_after_commit(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("closed commit crash")

    store.create_manifest_with_baselines = crash_after_commit
    calls = {"glm": 0, "sol": 0}

    def glm(_body):
        calls["glm"] += 1
        raise AssertionError("closed market must not call GLM")

    def sol(_body):
        calls["sol"] += 1
        raise AssertionError("closed market must not call SOL")

    with pytest.raises(RuntimeError, match="closed commit crash"):
        run_analysis_pipeline(
            tick=closed, barrier=closed, store=store,
            run_lock_path=tmp_path / "run" / "analysis.lock",
            invocation_id_factory=lambda: "a" * 32,
            glm_call=glm, sol_call=sol, now=NOW, clock=lambda: NOW,
        )

    reopened = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    outcome = run_analysis_pipeline(
        tick=closed, barrier=closed, store=reopened,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=glm, sol_call=sol, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    subjects = {
        row.subject_id for row in reopened.read_manifest_candidates(10)
        if row.assignment.value == "EXPLICITLY_DEFERRED"
    }
    assert subjects
    assert all(len(reopened.read_deferred_attempts(subject)) == 1 for subject in subjects)
    assert calls == {"glm": 0, "sol": 0}


def test_glm_three_tick_failure_streak_and_same_tick_both_failure_incidents(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    for tick_id in (10, 11, 12):
        barrier = _barrier(tick=tick_id)
        outcome = run_analysis_pipeline(
            tick=barrier, barrier=barrier, store=store,
            run_lock_path=tmp_path / "run" / "analysis.lock",
            invocation_id_factory=lambda tick_id=tick_id: f"{tick_id:032x}",
            glm_call=lambda _body: (ProviderExit.TIMEOUT, None),
            sol_call=lambda _body: (ProviderExit.TIMEOUT, None),
            now=NOW, clock=lambda: NOW,
        )
        assert outcome.state is AnalysisRunState.COMPLETED

    assert store.read_incident(
        IncidentKeyV1("glm-failure-streak", "glm-5.2")
    ) is not None
    assert store.read_incident(
        IncidentKeyV1("model-failure-both", "2026-07-30")
    ) is not None


def test_public_pipeline_executes_p10_audit_survey(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    calls = []
    original = store.write_audit_survey

    def observed(*args, **kwargs):
        calls.append((args, kwargs))
        return original(*args, **kwargs)

    store.write_audit_survey = observed
    barrier = _barrier()
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "a" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    assert len(calls) == 1
    coverage = store.read_audit_coverage("2026-07-30")
    assert {row["kind"] for row in coverage} == {
        "HOLDINGS_CHANGE", "QUOTE_STATE", "COMPLETED_EXECUTION",
        "OPERATIONAL_INCIDENT",
    }


def test_public_pipeline_p10_completes_sampled_audit(tmp_path):
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    first = _barrier(tick=10)
    run_analysis_pipeline(
        tick=first, barrier=first, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "a" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    pending = [
        row for kind in ("HOLDINGS_CHANGE", "QUOTE_STATE",
                         "COMPLETED_EXECUTION", "OPERATIONAL_INCIDENT")
        for row in store.read_audit_backlog(kind)
        if row.state == "PENDING_AUDIT"
    ]
    assert pending

    second = _barrier(tick=11)
    outcome = run_analysis_pipeline(
        tick=second, barrier=second, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    assert any(
        row.state == "AUDITED"
        for kind in ("HOLDINGS_CHANGE", "QUOTE_STATE",
                     "COMPLETED_EXECUTION", "OPERATIONAL_INCIDENT")
        for row in store.read_audit_backlog(kind)
    )
    assert sum(
        row["audited_count"]
        for row in store.read_audit_coverage("2026-07-30")
    ) == 1


def test_rate_limited_provider_stops_additional_batches_same_tick(
    tmp_path, monkeypatch,
):
    original_build = pipeline_module.build_candidates

    def many_candidates(*args, **kwargs):
        built = original_build(*args, **kwargs)
        seed = built.candidates[-1]
        rows = tuple(
            replace(
                seed, candidate_id=f"{index:032x}",
                subject_id=f"bulk-{index:04d}",
            )
            for index in range(201)
        )
        return type(built)(
            built.material_binding_digest, rows, built.baseline_advances,
        )

    monkeypatch.setattr(pipeline_module, "build_candidates", many_candidates)
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    calls = {"glm": 0}

    def limited(_body):
        calls["glm"] += 1
        return ProviderExit.RATE_LIMITED, None

    barrier = _barrier()
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "a" * 32,
        glm_call=limited, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    assert calls == {"glm": 1}
    assert len([
        row for row in store.read_analysis_batches(10)
        if row["provider"] == "GLM"
    ]) == 1


@pytest.mark.parametrize("exit_value", tuple(ProviderExit))
def test_te7_closed_glm_status_matrix_via_public_entrypoint(
    tmp_path, monkeypatch, exit_value,
):
    """§9.1/9.2: every durable GLM exit is consumed through the public run."""
    _only_assignment(monkeypatch, "FULL_REVIEW")
    store, outcome = run(
        tmp_path,
        glm_call=(
            glm_ok if exit_value is ProviderExit.OK
            else lambda _body: (exit_value, None)
        ),
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    batch = store.read_analysis_batches(10)[0]
    expected = ModelCallStatus(exit_value.value)
    assert batch["exit"] == expected.value
    disposition = store.read_dispositions_for_tick(10)
    assert len(disposition) == 1
    row = disposition[0]
    if expected is ModelCallStatus.OK:
        assert (row.action.value, row.reason_code) == ("SKIP", "glm_screened_none")
    else:
        assert (row.action.value, row.reason_code) == ("DEFER", "glm_failure_defer")
        assert len(store.read_deferred_attempts(row.subject_id)) == 1
    incident_kind = {
        ModelCallStatus.RATE_LIMITED: "model-rate-limited",
        ModelCallStatus.AUTH_FAIL: "model-auth-unavailable",
    }.get(expected)
    if incident_kind:
        assert store.read_incident(
            IncidentKeyV1(incident_kind, "glm-5.2")
        ) is not None


@pytest.mark.parametrize("exit_value", tuple(ProviderExit))
def test_te7_closed_sol_status_matrix_via_public_entrypoint(
    tmp_path, monkeypatch, exit_value,
):
    """§9.1/9.2: every durable SOL exit has one fail-up disposition."""
    _only_assignment(monkeypatch, "CRITICAL")
    store, outcome = run(
        tmp_path,
        sol_call=(
            _sol_with("ALERT") if exit_value is ProviderExit.OK
            else lambda _body: (exit_value, None)
        ),
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    batches = store.read_analysis_batches(10)
    sol_batch = next(row for row in batches if row["provider"] == "SOL")
    expected = ModelCallStatus(exit_value.value)
    assert sol_batch["exit"] == expected.value
    disposition = store.read_dispositions_for_tick(10)
    assert len(disposition) == 1
    row = disposition[0]
    if expected is ModelCallStatus.OK:
        assert (row.action.value, row.reason_code) == (
            "ESCALATE", "evidence_unverifiable",
        )
    else:
        assert (row.action.value, row.reason_code) == (
            "DEFER", "sol_failure_defer",
        )
    incident_kind = {
        ModelCallStatus.RATE_LIMITED: "model-rate-limited",
        ModelCallStatus.AUTH_FAIL: "model-auth-unavailable",
    }.get(expected)
    if incident_kind:
        assert store.read_incident(
            IncidentKeyV1(incident_kind, "gpt-5.6-sol")
        ) is not None


@pytest.mark.parametrize(
    ("assignment", "action", "cite", "late", "expected_reason",
     "expected_action", "backlog", "incident"),
    (
        ("CRITICAL", "IGNORE", False, False, "judgment_unavailable",
         "ESCALATE", False, True),
        ("CRITICAL", "ALERT", False, False, "evidence_unverifiable",
         "ESCALATE", False, False),
        ("FULL_REVIEW", "ALERT", False, False, "evidence_stale_defer",
         "DEFER", True, False),
        ("CRITICAL", "ALERT", True, True, "evidence_unverifiable",
         "ESCALATE", False, False),
        ("FULL_REVIEW", "ALERT", True, True, "evidence_stale_defer",
         "DEFER", True, False),
        ("FULL_REVIEW", "WATCH", False, False, "proposal_invalid_defer",
         "DEFER", True, False),
    ),
)
def test_te8b_exact_six_reachable_validator_rejection_combinations(
    tmp_path, monkeypatch, assignment, action, cite, late, expected_reason,
    expected_action, backlog, incident,
):
    evidence_id = _only_assignment(monkeypatch, assignment)
    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    barrier = _barrier()
    moment = datetime(2026, 7, 30, 1, tzinfo=timezone.utc) if late else NOW
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "a" * 32,
        glm_call=(
            glm_ok if assignment == "CRITICAL"
            else lambda body: json.dumps({
                "schema_version": 1,
                "annotations": [{
                    "candidate_id": candidate_id, "relevance": "PORTFOLIO_DIRECT",
                    "materiality": "HIGH", "urgency": "NOW",
                    "evidence_unknowns": [], "summary": "review",
                } for candidate_id in _ids(body)],
            }, separators=(",", ":")).encode()
        ),
        sol_call=_sol_with(action, evidence_id=evidence_id if cite else None),
        now=moment, clock=lambda: moment,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    rows = store.read_dispositions_for_tick(10)
    assert len(rows) == 1
    row = rows[0]
    assert (row.action.value, row.reason_code) == (
        expected_action, expected_reason,
    )
    assert bool(store.read_deferred_attempts(row.subject_id)) is backlog
    assert (store.read_incident(IncidentKeyV1(
        "validator-proposal-rejected", "2026-07-30",
    )) is not None) is incident
    assert (row.action.value in {"SEND", "ESCALATE"}) is (
        expected_action == "ESCALATE"
    )


def test_te8b_critical_merge_prior_mismatch_is_absorbed_by_critical_rule(
    tmp_path, monkeypatch,
):
    _only_assignment(monkeypatch, "CRITICAL")
    store, outcome = run(tmp_path, sol_call=_sol_with("WATCH"))
    assert outcome.state is AnalysisRunState.COMPLETED
    row, = store.read_dispositions_for_tick(10)
    assert (row.action.value, row.reason_code) == (
        "ESCALATE", "judgment_unavailable",
    )
    assert store.read_deferred_attempts(row.subject_id) == ()


# --- numeric feature wave (plan §4.3 M-1, §7.4, §11 T-N15/T-N18b) ---


def test_history_advances_survive_both_reconstructions_to_the_kernel(tmp_path):
    from tokmo.programs.candidate_builder import quote_history_subjects
    from test_candidate_builder import QUOTE_SUBJECT

    store, first = run(tmp_path)
    assert first.state is AnalysisRunState.COMPLETED
    subjects = quote_history_subjects(_barrier())
    assert subjects == (QUOTE_SUBJECT,)
    rows = store.read_quote_state_history(subjects)[QUOTE_SUBJECT]
    assert [(row["tick_id"], row["last_price"]) for row in rows] == [(10, "11")]

    barrier = _barrier(tick=11, price="12")
    second = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "b" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert second.state is AnalysisRunState.COMPLETED
    rows = store.read_quote_state_history(subjects)[QUOTE_SUBJECT]
    assert [(row["tick_id"], row["last_price"]) for row in rows] == [
        (11, "12"), (10, "11"),
    ]


def test_sampled_audit_promotion_cap_is_five_per_tick_across_kinds(
    tmp_path, monkeypatch,
):
    from dataclasses import replace as dataclass_replace

    from tokmo.values.ops_kernel import ProducerRole

    store = OpsKernelStore(tmp_path / "ops" / "kernel.sqlite3")
    for index in range(7):
        store.upsert_audit_backlog(
            ProducerRole.ANALYSIS, "QUOTE_STATE", f"bulk-{index:04d}",
            "2026-07-29", "2026-07-29", "PENDING_AUDIT",
        )
    original = pipeline_module.build_candidates

    def bulk_full_review(*args, **kwargs):
        built = original(*args, **kwargs)
        seed = next(
            item for item in built.candidates
            if item.assignment.value == "FULL_REVIEW"
        )
        rows = tuple(
            dataclass_replace(
                seed, candidate_id=f"{index:016x}",
                subject_id=f"bulk-{index:04d}",
            )
            for index in range(7)
        )
        return type(built)(
            built.material_binding_digest, rows, built.baseline_advances,
            built.history_advances,
        )

    monkeypatch.setattr(pipeline_module, "build_candidates", bulk_full_review)
    barrier = _barrier()
    outcome = run_analysis_pipeline(
        tick=barrier, barrier=barrier, store=store,
        run_lock_path=tmp_path / "run" / "analysis.lock",
        invocation_id_factory=lambda: "a" * 32,
        glm_call=glm_ok, sol_call=sol_ok, now=NOW, clock=lambda: NOW,
    )
    assert outcome.state is AnalysisRunState.COMPLETED
    assignments = [
        row.assignment.value for row in store.read_manifest_candidates(10)
    ]
    assert assignments.count("SAMPLED_AUDIT") == 5
    assert assignments.count("FULL_REVIEW") == 2
