"""Local-only Slice E G1 analysis orchestration with injected model stubs."""

import fcntl
import hashlib
import os
import uuid
from dataclasses import dataclass, replace
from datetime import timedelta
from pathlib import Path

from tokmo.programs.analysis_redaction import project_for_glm, project_for_sol
from tokmo.programs.candidate_builder import (
    BaselineAdvanceV1,
    build_candidates,
    material_binding_digest,
    quote_history_subjects,
)
from tokmo.programs.prompt_builder import (
    GLM_ANALYSIS_V1,
    SOL_ANALYSIS_V1,
    InstructionSegmentV1,
    PromptV1,
    build_prompt_body,
)
from tokmo.values.analysis_barrier import MarketReadiness
from tokmo.values.analysis_contracts import (
    VALIDATOR_REJECTION_FALLBACK_V1,
    AnalysisResultV1,
    AnalysisRunOutcomeV1,
    AnalysisRunState,
    CandidateDataSegmentV1,
    FreshnessState,
    GlmMateriality,
    GlmRelevance,
    HermesDispositionInputV1,
    MaterialKindV1,
    ModelCallStatus,
    ProviderExit,
    SolProposedAction,
    ValidatorRejection,
    parse_glm_batch,
    parse_sol_batch,
    validate_disposition,
)
from tokmo.values.material_pack import canonical_encode
from tokmo.values.ops_kernel import (
    AlertChannel,
    AlertOutboxV1,
    CandidateAssignment,
    CandidateManifestV1,
    CandidateState,
    DeferredAttemptV1,
    DeferredTerminalState,
    DispositionAction,
    DispositionV1,
    IncidentKeyV1,
    ProducerRole,
)
from tokmo.values.portfolio_scope import resolve_account_scope_pattern
from tokmo.values.session_gate import Market

GLM_MAX_CANDIDATES_PER_CALL = 200
SOL_MAX_CANDIDATES_PER_CALL = 20


@dataclass(frozen=True)
class RequestAnalysisSettlementV1:
    result: AnalysisResultV1
    coverage_rows: tuple
    disposition: object
    outbox_or_none: object | None = None


@dataclass(frozen=True)
class RequestAnalysisOutcomeV1:
    succeeded: int = 0
    failed: int = 0
    unknown: int = 0
    duplicates: int = 0


def analyze_requests(*, requests, evidence_by_request, store, analyzer, now):
    """Consume durable requests through one injected common-analyzer seam.

    Adds no provider retry/state machine. Each request transitions to ANALYZING
    before the injected callable; stale/expired required evidence fails closed;
    terminal replay performs analyzer zero. Callers choose this entrypoint or
    ``run_analysis_pipeline`` for a candidate, never both.
    """
    counts = {"succeeded": 0, "failed": 0, "unknown": 0, "duplicates": 0}
    store.reconcile_analysis_requests(ProducerRole.ANALYSIS, now=now)
    for request in tuple(requests):
        durable = store.read_analysis_request(request.request_id)
        if durable is None or durable["request_digest"] != request.request_digest:
            raise ValueError("analysis request is not durable")
        if durable["state"] in {"SUCCEEDED", "DEFERRED", "FAILED", "UNKNOWN"}:
            counts["duplicates"] += 1
            continue
        started = store.start_analysis_request(
            ProducerRole.ANALYSIS,
            request.request_id,
            now=now,
        )
        if started.value == "DUPLICATE":
            store.reconcile_analysis_requests(ProducerRole.ANALYSIS, now=now)
            counts["unknown"] += 1
            continue
        evidence = tuple(evidence_by_request.get(request.request_id, ()))
        healthy = bool(evidence) and all(
            row.freshness_state is FreshnessState.FRESH and row.expires_at > now
            for row in evidence
        )
        if not healthy:
            result = AnalysisResultV1(
                AnalysisResultV1.compute_result_id(
                    request_id=request.request_id,
                    decision_id=None,
                ),
                request.request_id,
                request.analysis_kind,
                "FAILED",
                None,
                None,
                now,
            )
            store.settle_analysis_result_with_coverage(
                ProducerRole.ANALYSIS,
                request_id=request.request_id,
                result=result,
                coverage_rows=(),
            )
            counts["failed"] += 1
            continue
        try:
            settlement = analyzer(request)
        except Exception:
            # Started-but-not-durably-terminal work is ambiguous by contract.
            result = AnalysisResultV1(
                AnalysisResultV1.compute_result_id(
                    request_id=request.request_id,
                    decision_id=None,
                ),
                request.request_id,
                request.analysis_kind,
                "UNKNOWN",
                None,
                None,
                now,
            )
            key = IncidentKeyV1("analysis-request-unknown", request.request_id)
            outbox = AlertOutboxV1.for_incident(
                alert_id=hashlib.sha256(
                    f"analysis-unknown:{request.request_id}".encode()
                ).hexdigest()[:32],
                key=key,
                tick_id=request.tick_id,
                subject_id=request.subject_id,
                severity="HIGH",
                body_evidence=(),
                created_at=now,
            )
            store.settle_analysis_result_with_coverage(
                ProducerRole.ANALYSIS,
                request_id=request.request_id,
                result=result,
                coverage_rows=(),
                unknown_incident_key=key,
                unknown_outbox_or_none=outbox,
            )
            counts["unknown"] += 1
            continue
        if not isinstance(settlement, RequestAnalysisSettlementV1):
            raise ValueError("invalid analyzer settlement")
        if not all(
            row.freshness_state is FreshnessState.FRESH and row.expires_at > now
            for row in evidence
        ):
            raise ValueError("evidence expired before settlement")
        store.settle_analysis_result_with_coverage(
            ProducerRole.ANALYSIS,
            request_id=request.request_id,
            result=settlement.result,
            coverage_rows=settlement.coverage_rows,
            disposition=settlement.disposition,
            outbox_or_none=settlement.outbox_or_none,
        )
        status_key = settlement.result.status.lower()
        if status_key not in counts:
            raise ValueError("invalid analyzer result status")
        counts[status_key] += 1
    return RequestAnalysisOutcomeV1(**counts)


def _chunks(rows, size):
    return tuple(
        tuple(rows[index : index + size]) for index in range(0, len(rows), size)
    )


def _baseline_map(store):
    result = {}
    for kind in MaterialKindV1:
        for row in store.read_subject_baselines(kind.value):
            value = BaselineAdvanceV1(
                kind,
                row["subject_id"],
                row["fields"],
                row["account_scope"],
                row["source_tick_id"],
                row["baseline_hash"],
            )
            result[(kind, row["subject_id"])] = value
    return result


def _call(stub, body):
    response = stub(body)
    if type(response) is bytes:
        return ProviderExit.OK, response
    if (
        type(response) is tuple
        and len(response) == 2
        and isinstance(response[0], (ProviderExit, str))
        and (response[1] is None or type(response[1]) is bytes)
    ):
        return ProviderExit(response[0]), response[1]
    raise ValueError("invalid model stub response")


def _glm_json(batch):
    return {
        "schema_version": 1,
        "annotations": [
            {
                "candidate_id": row.candidate_id,
                "relevance": row.relevance.value,
                "materiality": row.materiality.value,
                "urgency": row.urgency.value,
                "evidence_unknowns": list(row.evidence_unknowns),
                "summary": row.summary,
            }
            for row in batch.annotations
        ],
    }


def _sol_json(batch):
    return {
        "schema_version": 1,
        "analyses": [
            {
                "candidate_id": row.candidate_id,
                "verdict": row.verdict.value,
                "proposed_action": row.proposed_action.value,
                "rationale": row.rationale,
                "cited_evidence_ids": list(row.cited_evidence_ids),
                "confidence": row.confidence,
                "disagreements_with_glm": list(row.disagreements_with_glm),
            }
            for row in batch.analyses
        ],
    }


def _decision(candidate, glm_status, sol_status, sol, material_ticks, now):
    rejection = None
    critical = candidate.assignment is CandidateAssignment.CRITICAL
    if sol_status != ModelCallStatus.OK:
        incident_kind = (
            candidate.fields["incident_kind"]
            if candidate.kind is MaterialKindV1.OPERATIONAL_INCIDENT
            else None
        )
        if critical and incident_kind in {
            "execution-identity-conflict",
            "execution-quarantine",
            "trades-quarantine-observed",
        }:
            action, reason = DispositionAction.ESCALATE, "quarantine_conflict"
        elif critical and incident_kind in {
            "holdings-identity-collision",
            "holdings_material_unavailable",
            "quotes_material_unavailable",
            "trades_material_unavailable",
            "kr_holdings_unavailable",
            "us_holdings_unavailable",
        }:
            action, reason = DispositionAction.ESCALATE, "stop_signal"
        elif critical and glm_status is ModelCallStatus.OK:
            action, reason = DispositionAction.DEFER, "sol_failure_defer"
        elif critical:
            action, reason = DispositionAction.ESCALATE, "judgment_unavailable"
        else:
            action, reason = DispositionAction.DEFER, "sol_failure_defer"
        citations = ()
    else:
        action, reason = {
            SolProposedAction.ALERT: (DispositionAction.SEND, "evidence_verified_send"),
            SolProposedAction.WATCH: (
                DispositionAction.MERGE,
                "same_subject_session_merge",
            ),
            SolProposedAction.IGNORE: (DispositionAction.SKIP, "subject_unchanged"),
        }[sol.proposed_action]
        citations = sol.cited_evidence_ids
    decision_id = hashlib.sha256(
        canonical_encode(
            {
                "v": 1,
                "tick_id": candidate.tick_id,
                "candidate_id": candidate.candidate_id,
                "action": action.value,
            }
        )
    ).hexdigest()[:32]
    disposition = DispositionV1(
        decision_id,
        candidate.tick_id,
        candidate.candidate_id,
        candidate.subject_id,
        action,
        reason,
        tuple(citations),
        material_ticks,
        glm_status.value,
        sol_status.value,
        "analysis-policy-v1",
        now,
    )
    if sol_status is ModelCallStatus.OK:
        validation = validate_disposition(
            HermesDispositionInputV1(
                candidate,
                glm_status,
                sol_status,
                None,
                True,
                tuple(citations),
                {
                    ref.evidence_id: (
                        FreshnessState.FRESH
                        if now <= ref.expires_at
                        else FreshnessState.STALE
                    )
                    for ref in candidate.evidence_refs
                },
                (candidate.market, now.date().isoformat()),
                None,
                False,
            ),
            disposition,
        )
        if isinstance(validation, ValidatorRejection):
            rejection = validation
            action, reason = VALIDATOR_REJECTION_FALLBACK_V1[(validation, critical)]
            disposition = DispositionV1(
                decision_id,
                candidate.tick_id,
                candidate.candidate_id,
                candidate.subject_id,
                action,
                reason,
                (),
                material_ticks,
                glm_status.value,
                sol_status.value,
                "analysis-policy-v1",
                now,
            )
            citations = ()
    outbox = None
    if action in {DispositionAction.SEND, DispositionAction.ESCALATE}:
        outbox = AlertOutboxV1.for_disposition(
            alert_id=hashlib.sha256(("shadow:" + decision_id).encode()).hexdigest()[
                :32
            ],
            decision_id=decision_id,
            tick_id=candidate.tick_id,
            subject_id=candidate.subject_id,
            channel=AlertChannel.SHADOW,
            severity="HIGH" if critical else "MEDIUM",
            body_evidence=tuple(citations),
            created_at=now,
        )
    return disposition, outbox, rejection


def _provider_incident(store, *, provider, status, tick_id, now):
    kind = (
        "model-auth-unavailable"
        if status is ModelCallStatus.AUTH_FAIL
        else "model-rate-limited"
        if status is ModelCallStatus.RATE_LIMITED
        else None
    )
    if kind is not None:
        key = IncidentKeyV1(kind, provider)
        prior = store.read_incident(key)
        required = (
            prior is None
            or prior.state.value == "RESOLVED"
            or (
                prior.impact_ref
                != (
                    f"model-auth:{provider}"
                    if kind == "model-auth-unavailable"
                    else str(tick_id)
                )
            )
        )
        outbox = None
        if required:
            sequence = 1 if prior is None else prior.count + 1
            outbox = AlertOutboxV1.for_incident(
                alert_id=hashlib.sha256(
                    f"incident:{kind}:{provider}:{sequence}".encode()
                ).hexdigest()[:32],
                key=key,
                tick_id=tick_id,
                subject_id=provider,
                severity="HIGH",
                body_evidence=(),
                created_at=now,
            )
        store.observe_incident_with_shadow_outbox(
            ProducerRole.ANALYSIS,
            key,
            f"model-auth:{provider}"
            if kind == "model-auth-unavailable"
            else str(tick_id),
            outbox,
            now,
        )
    elif status is ModelCallStatus.OK:
        for kind_name in ("model-auth-unavailable", "model-rate-limited"):
            key = IncidentKeyV1(kind_name, provider)
            prior = store.read_incident(key)
            if prior is not None and prior.state.value == "OPEN":
                outbox = AlertOutboxV1.for_incident(
                    alert_id=hashlib.sha256(
                        f"resolve:{kind_name}:{provider}:{prior.count}".encode()
                    ).hexdigest()[:32],
                    key=key,
                    tick_id=tick_id,
                    subject_id=provider,
                    severity="HIGH",
                    body_evidence=(),
                    created_at=now,
                )
                store.resolve_incident_with_shadow_outbox(
                    ProducerRole.ANALYSIS,
                    key,
                    outbox,
                    now,
                )


def _pipeline_incident(
    store, *, kind, subject, impact_ref, tick_id, now, resolve=False
):
    key = IncidentKeyV1(kind, subject)
    prior = store.read_incident(key)
    if resolve:
        if prior is None or prior.state.value != "OPEN":
            return
        prefix = "resolve"
    else:
        required = (
            prior is None
            or prior.state.value == "RESOLVED"
            or prior.impact_ref != impact_ref
        )
        prefix = "incident"
        if not required:
            store.observe_incident_with_shadow_outbox(
                ProducerRole.ANALYSIS,
                key,
                impact_ref,
                None,
                now,
            )
            return
    sequence = 1 if prior is None else prior.count + 1
    outbox = AlertOutboxV1.for_incident(
        alert_id=hashlib.sha256(
            f"{prefix}:{kind}:{subject}:{sequence}".encode()
        ).hexdigest()[:32],
        key=key,
        tick_id=tick_id,
        subject_id=subject,
        severity="HIGH",
        body_evidence=(),
        created_at=now,
    )
    method = (
        store.resolve_incident_with_shadow_outbox
        if resolve
        else store.observe_incident_with_shadow_outbox
    )
    if resolve:
        method(ProducerRole.ANALYSIS, key, outbox, now)
    else:
        method(ProducerRole.ANALYSIS, key, impact_ref, outbox, now)


def _record_failure_aggregates(store, *, tick_id, now):
    ticks = sorted(
        {
            row["tick_id"]
            for candidate_tick in range(max(0, tick_id - 2), tick_id + 1)
            for row in store.read_analysis_batches(candidate_tick)
            if row["provider"] == "GLM"
        }
    )
    glm_failed_three = len(ticks) == 3 and ticks == list(
        range(tick_id - 2, tick_id + 1)
    )
    if glm_failed_three:
        glm_failed_three = all(
            (
                rows := [
                    row
                    for row in store.read_analysis_batches(candidate_tick)
                    if row["provider"] == "GLM"
                ]
            )
            and all(
                row["state"] in {"SETTLED", "UNKNOWN"} and row["exit"] != "OK"
                for row in rows
            )
            for candidate_tick in ticks
        )
    _pipeline_incident(
        store,
        kind="glm-failure-streak",
        subject="glm-5.2",
        impact_ref="3",
        tick_id=tick_id,
        now=now,
        resolve=not glm_failed_three
        and any(
            row["provider"] == "GLM"
            and row["state"] == "SETTLED"
            and row["exit"] == "OK"
            for row in store.read_analysis_batches(tick_id)
        ),
    ) if glm_failed_three or store.read_incident(
        IncidentKeyV1("glm-failure-streak", "glm-5.2")
    ) is not None else None

    rows = store.read_analysis_batches(tick_id)
    glm_failed = any(
        row["provider"] == "GLM" and row["exit"] not in {None, "OK"} for row in rows
    )
    sol_failed = any(
        row["provider"] == "SOL" and row["exit"] not in {None, "OK"} for row in rows
    )
    subject = now.date().isoformat()
    if glm_failed and sol_failed:
        _pipeline_incident(
            store,
            kind="model-failure-both",
            subject=subject,
            impact_ref=str(tick_id),
            tick_id=tick_id,
            now=now,
        )
    elif any(row["provider"] == "GLM" and row["exit"] == "OK" for row in rows) and any(
        row["provider"] == "SOL" and row["exit"] == "OK" for row in rows
    ):
        _pipeline_incident(
            store,
            kind="model-failure-both",
            subject=subject,
            impact_ref=str(tick_id),
            tick_id=tick_id,
            now=now,
            resolve=True,
        )


def _complete_p10_audit(store, *, tick_id, session_date, now):
    kinds = tuple(kind.value for kind in MaterialKindV1)
    backlogs = {kind: store.read_audit_backlog(kind) for kind in kinds}
    store.write_audit_survey(
        ProducerRole.ANALYSIS,
        session_date,
        tuple(
            {
                "kind": kind,
                "eligible_count": sum(
                    row.state == "PENDING_AUDIT" for row in backlogs[kind]
                ),
                "debt_count": sum(
                    row.state == "PENDING_AUDIT" for row in backlogs[kind]
                ),
                "oldest_wait_regular_sessions": 0,
                "capacity_incident_opened": False,
            }
            for kind in kinds
        ),
        now,
    )
    batches = store.read_analysis_batches(tick_id)
    for candidate in store.read_manifest_candidates(tick_id):
        if candidate.assignment is not CandidateAssignment.SAMPLED_AUDIT:
            continue
        batch = next(
            (
                row
                for row in batches
                if row["provider"] == "SOL"
                and row["state"] == "SETTLED"
                and row["exit"] == "OK"
                and candidate.candidate_id in row["candidate_ids"]
            ),
            None,
        )
        if batch is not None:
            store.complete_audit(
                ProducerRole.ANALYSIS,
                tick_id=tick_id,
                candidate_id=candidate.candidate_id,
                batch_ordinal=batch["ordinal"],
                kind=candidate.kind,
                subject_id=candidate.subject_id,
                session_date=session_date,
                considered_session=session_date,
                now=now,
            )


def _manifest_failure(
    store,
    row,
    *,
    tick_id,
    attempt_tick,
    material_ticks,
    glm_status,
    sol_status,
    now,
    carryover=False,
    market=None,
):
    critical = row.assignment is CandidateAssignment.CRITICAL
    if critical:
        action, reason = DispositionAction.ESCALATE, "judgment_unavailable"
    else:
        action = DispositionAction.DEFER
        reason = "carryover_defer" if carryover else "glm_failure_defer"
    decision_id = hashlib.sha256(
        canonical_encode(
            {
                "v": 1,
                "tick_id": tick_id,
                "candidate_id": row.candidate_id,
                "action": action.value,
                "reason": reason,
            }
        )
    ).hexdigest()[:32]
    disposition = DispositionV1(
        decision_id,
        tick_id,
        row.candidate_id,
        row.subject_id,
        action,
        reason,
        (),
        material_ticks,
        glm_status.value,
        sol_status.value,
        "analysis-policy-v1",
        now,
    )
    outbox = None
    if action is DispositionAction.ESCALATE:
        outbox = AlertOutboxV1.for_disposition(
            alert_id=hashlib.sha256(("shadow:" + decision_id).encode()).hexdigest()[
                :32
            ],
            decision_id=decision_id,
            tick_id=tick_id,
            subject_id=row.subject_id,
            channel=AlertChannel.SHADOW,
            severity="HIGH",
            body_evidence=(),
            created_at=now,
        )
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS,
        disposition,
        outbox,
    )
    if action is DispositionAction.DEFER:
        history = store.read_deferred_attempts(row.subject_id)
        deferred_market = history[0].market if history else market
        if deferred_market is None:
            raise ValueError("deferred market unavailable")
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            DeferredAttemptV1(
                row.subject_id,
                deferred_market,
                attempt_tick,
                attempt_tick,
                tick_id,
                row.candidate_id,
                tuple(row.evidence_refs),
                material_ticks,
                now + timedelta(seconds=300),
                reason,
                row.assignment,
                now,
            ),
        )


def _settle_unknown_candidates(
    store,
    batches,
    *,
    current_tick,
    now,
    markets_by_candidate=None,
):
    markets_by_candidate = markets_by_candidate or {}
    by_tick = {}
    for batch in batches:
        provider = {
            "GLM": "glm-5.2",
            "SOL": "gpt-5.6-sol",
        }[batch["provider"]]
        _pipeline_incident(
            store,
            kind="model-call-unknown",
            subject=provider,
            impact_ref=f"{batch['tick_id']}:{batch['ordinal']}",
            tick_id=batch["tick_id"],
            now=now,
        )
        by_tick.setdefault(batch["tick_id"], set()).update(batch["candidate_ids"])
    settled = 0
    for batch_tick, candidate_ids in sorted(by_tick.items()):
        summary = store.read_manifest_summary(batch_tick)
        if summary is None:
            continue
        if summary.state.value == "OPEN":
            store.seal_manifest(ProducerRole.ANALYSIS, batch_tick, now)
        for row in store.read_manifest_candidates(batch_tick):
            if (
                row.candidate_id in candidate_ids
                and row.state is CandidateState.PENDING
            ):
                _manifest_failure(
                    store,
                    row,
                    tick_id=batch_tick,
                    attempt_tick=current_tick,
                    material_ticks=summary.material_ticks,
                    glm_status=ModelCallStatus.OUTCOME_UNKNOWN,
                    sol_status=ModelCallStatus.OUTCOME_UNKNOWN,
                    now=now,
                    market=markets_by_candidate.get(row.candidate_id),
                )
                settled += 1
    return settled


def _settle_carryover(store, *, current_tick, now):
    settled = 0
    for raw in store.read_carryover_candidates(current_tick):
        summary = store.read_manifest_summary(raw["tick_id"])
        if raw["manifest_state"] == "OPEN":
            store.seal_manifest(ProducerRole.ANALYSIS, raw["tick_id"], now)
        row = next(
            item
            for item in store.read_manifest_candidates(raw["tick_id"])
            if item.candidate_id == raw["candidate_id"]
        )
        if row.state is not CandidateState.PENDING:
            continue
        _manifest_failure(
            store,
            row,
            tick_id=raw["tick_id"],
            attempt_tick=current_tick,
            material_ticks=summary.material_ticks,
            glm_status=ModelCallStatus.OUTCOME_UNKNOWN,
            sol_status=ModelCallStatus.OUTCOME_UNKNOWN,
            now=now,
            carryover=True,
        )
        settled += 1
    return settled


def _reconstruct_deferred(store, *, tick_id, review_base, now):
    manifests = {
        row.candidate_id: row for row in store.read_manifest_candidates(tick_id)
    }
    for disposition in store.read_dispositions_for_tick(tick_id):
        if disposition.action is not DispositionAction.DEFER:
            continue
        row = manifests[disposition.candidate_id]
        history = store.read_deferred_attempts(row.subject_id)
        first_seen = history[0].first_seen_tick if history else tick_id
        if not history:
            continue
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            DeferredAttemptV1(
                row.subject_id,
                history[0].market,
                first_seen,
                tick_id,
                tick_id,
                row.candidate_id,
                tuple(row.evidence_refs),
                disposition.material_ticks,
                review_base + timedelta(seconds=300),
                disposition.reason_code,
                row.assignment,
                now,
            ),
        )


def run_analysis_pipeline(
    *,
    tick,
    barrier,
    store,
    run_lock_path: Path,
    invocation_id_factory=lambda: uuid.uuid4().hex,
    glm_call,
    sol_call,
    now,
    clock,
    numeric_evidence_sink=None,
    resolve_scope=resolve_account_scope_pattern,
):
    tick_id = barrier.tick_id
    if getattr(tick, "tick_id", tick_id) != tick_id:
        raise ValueError("tick and barrier mismatch")
    lock_path = Path(run_lock_path)
    try:
        lock_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        descriptor = os.open(
            lock_path,
            os.O_WRONLY | os.O_CREAT | os.O_NOFOLLOW,
            0o600,
        )
    except OSError:
        return AnalysisRunOutcomeV1(
            AnalysisRunState.LOCK_UNAVAILABLE,
            None,
            tick_id,
            0,
            0,
        )
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return AnalysisRunOutcomeV1(
                AnalysisRunState.LOCK_CONTENDED,
                None,
                tick_id,
                0,
                0,
            )
        invocation_id = invocation_id_factory()
        if (
            type(invocation_id) is not str
            or len(invocation_id) != 32
            or any(char not in "0123456789abcdef" for char in invocation_id)
        ):
            raise ValueError("invalid invocation id")
        reconciled = store.reconcile_analysis_batches(
            ProducerRole.ANALYSIS,
            invocation_id=invocation_id,
            now=clock(),
        )
        past_unknown = tuple(row for row in reconciled if row["tick_id"] < tick_id)
        current_unknown = tuple(row for row in reconciled if row["tick_id"] == tick_id)
        preflight_envelopes = build_candidates(
            barrier,
            baselines={},
            now=now,
            resolve_scope=resolve_scope,
        ).candidates
        preflight_markets = {
            row.candidate_id: Market(row.market)
            for row in preflight_envelopes
            if row.market in {"KR", "US"}
        }
        _settle_unknown_candidates(
            store,
            past_unknown,
            current_tick=tick_id,
            now=clock(),
        )
        _settle_carryover(store, current_tick=tick_id, now=clock())

        # P3 is deliberately before every baseline/candidate read.
        summary = store.read_manifest_summary(tick_id)
        binding = material_binding_digest(barrier)
        if summary is not None and summary.material_binding_digest != binding:
            return AnalysisRunOutcomeV1(
                AnalysisRunState.DRIFT_CONFLICT,
                invocation_id,
                tick_id,
                summary.total_count,
                0,
            )
        if summary is not None and summary.state.value == "FAILED":
            return AnalysisRunOutcomeV1(
                AnalysisRunState.TERMINAL_MANIFEST,
                invocation_id,
                tick_id,
                summary.total_count,
                0,
            )
        if current_unknown:
            _settle_unknown_candidates(
                store,
                current_unknown,
                current_tick=tick_id,
                now=clock(),
                markets_by_candidate=preflight_markets,
            )
        if summary is not None:
            _reconstruct_deferred(
                store,
                tick_id=tick_id,
                review_base=getattr(tick, "start_utc", now),
                now=clock(),
            )
            manifest_rows = store.read_manifest_candidates(tick_id)
            if all(row.state is not CandidateState.PENDING for row in manifest_rows):
                return AnalysisRunOutcomeV1(
                    AnalysisRunState.COMPLETED,
                    invocation_id,
                    tick_id,
                    summary.total_count,
                    0,
                )

        built = build_candidates(
            barrier,
            baselines=_baseline_map(store),
            history=store.read_quote_state_history(quote_history_subjects(barrier)),
            now=now,
            incident_rows=store.read_incident_rows(),
            resolve_scope=resolve_scope,
        )
        if numeric_evidence_sink is not None:
            # The explicit per-tick numeric evidence (§10.9): tallied by the
            # gate itself over every observed quote subject, before eligibility
            # filtering — never reconstructed from a model projection body.
            numeric_evidence_sink(
                {
                    "tick_id": tick_id,
                    "numeric_state_counts": dict(built.numeric_state_counts),
                    "trigger_counts": dict(built.trigger_fire_counts),
                }
            )
        pending_audits = {
            (kind.value, row.subject_id)
            for kind in MaterialKindV1
            for row in store.read_audit_backlog(kind.value)
            if row.state == "PENDING_AUDIT"
        }
        sampled = 0
        promoted = []
        for row in built.candidates:
            if (
                sampled < 5
                and row.assignment is CandidateAssignment.FULL_REVIEW
                and (row.kind.value, row.subject_id) in pending_audits
            ):
                row = replace(row, assignment=CandidateAssignment.SAMPLED_AUDIT)
                sampled += 1
            promoted.append(row)
        built = type(built)(
            built.material_binding_digest,
            tuple(promoted),
            built.baseline_advances,
            built.history_advances,
        )
        due_subjects = {
            row.subject_id
            for market in Market
            if (
                barrier.market_readiness[market] is MarketReadiness.READY
                and market in barrier.proceed_markets
            )
            for row in store.read_due_deferred(market, getattr(tick, "start_utc", now))
        }
        eligible_candidates = []
        for row in built.candidates:
            history = store.read_deferred_attempts(row.subject_id)
            if row.assignment is CandidateAssignment.EXPLICITLY_DEFERRED:
                if not history:
                    eligible_candidates.append(row)
            elif not history or row.subject_id in due_subjects:
                eligible_candidates.append(row)
        built = type(built)(
            built.material_binding_digest,
            tuple(eligible_candidates),
            built.baseline_advances,
            built.history_advances,
        )
        envelopes = {row.candidate_id: row for row in built.candidates}
        if summary is None:
            manifest = tuple(
                CandidateManifestV1(
                    row.candidate_id,
                    row.subject_id,
                    row.kind.value,
                    tuple(evidence.evidence_id for evidence in row.evidence_refs),
                    row.assignment,
                    CandidateState.PENDING,
                    row.created_at,
                )
                for row in built.candidates
            )
            initial_deferred = tuple(
                DeferredAttemptV1(
                    row.subject_id,
                    Market(row.market),
                    tick_id,
                    tick_id,
                    tick_id,
                    row.candidate_id,
                    tuple(evidence.evidence_id for evidence in row.evidence_refs),
                    barrier.material_ticks,
                    getattr(tick, "start_utc", now),
                    "market_closed_defer",
                    row.assignment,
                    clock(),
                )
                for row in built.candidates
                if row.assignment is CandidateAssignment.EXPLICITLY_DEFERRED
            ) + tuple(
                DeferredAttemptV1(
                    row.subject_id,
                    Market(row.market),
                    store.read_deferred_attempts(row.subject_id)[0].first_seen_tick,
                    tick_id,
                    tick_id,
                    row.candidate_id,
                    tuple(evidence.evidence_id for evidence in row.evidence_refs),
                    barrier.material_ticks,
                    getattr(tick, "start_utc", now) + timedelta(seconds=300),
                    "next_session_defer",
                    row.assignment,
                    clock(),
                )
                for row in built.candidates
                if (
                    row.assignment is not CandidateAssignment.EXPLICITLY_DEFERRED
                    and row.subject_id in due_subjects
                    and store.read_deferred_attempts(row.subject_id)
                )
            )
            store.create_manifest_with_baselines(
                ProducerRole.ANALYSIS,
                tick_id,
                now,
                barrier.material_ticks,
                built.material_binding_digest,
                manifest,
                built.baseline_advances,
                initial_deferred,
                history_advances=built.history_advances,
            )
            summary = store.read_manifest_summary(tick_id)
        if summary.state.value == "FAILED":
            return AnalysisRunOutcomeV1(
                AnalysisRunState.TERMINAL_MANIFEST,
                invocation_id,
                tick_id,
                summary.total_count,
                0,
            )
        if summary.state.value == "OPEN":
            store.seal_manifest(ProducerRole.ANALYSIS, tick_id, clock())

        candidates = tuple(
            envelopes[row.candidate_id]
            for row in store.read_manifest_candidates(tick_id)
            if row.state is CandidateState.PENDING
        )
        glm_annotations = {}
        glm_status = {}
        glm_batches = _chunks(candidates, GLM_MAX_CANDIDATES_PER_CALL)
        existing_batch_rows = store.read_analysis_batches(tick_id)
        stop_glm = False
        for ordinal, group in enumerate(glm_batches):
            if stop_glm:
                break
            batch_rows = store.read_analysis_batches(tick_id)
            existing = next(
                (row for row in batch_rows if row["ordinal"] == ordinal),
                None,
            )
            if existing is None:
                store.plan_analysis_batches(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    (
                        {
                            "ordinal": ordinal,
                            "provider": "GLM",
                            "candidate_ids": tuple(item.candidate_id for item in group),
                            "request_digest": hashlib.sha256(
                                canonical_encode(
                                    [project_for_glm(item) for item in group]
                                )
                            ).hexdigest(),
                            "planned_at": now,
                        },
                    ),
                    invocation_id=invocation_id,
                )
                existing = next(
                    row
                    for row in store.read_analysis_batches(tick_id)
                    if row["ordinal"] == ordinal
                )
            if existing["state"] == "PLANNED":
                store.start_analysis_batch(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    ordinal,
                    invocation_id=invocation_id,
                    now=clock(),
                )
                body = build_prompt_body(
                    PromptV1(
                        (
                            InstructionSegmentV1(GLM_ANALYSIS_V1),
                            *(
                                CandidateDataSegmentV1(
                                    item.candidate_id, project_for_glm(item)
                                )
                                for item in group
                            ),
                        )
                    )
                )
                exit, raw = _call(glm_call, body)
                parsed = (
                    parse_glm_batch(raw, tuple(item.candidate_id for item in group))
                    if exit is ProviderExit.OK and raw is not None
                    else None
                )
                if parsed is not None and parsed.exit is ProviderExit.PARSE_FAIL:
                    exit = ProviderExit.PARSE_FAIL
                store.settle_analysis_batch(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    ordinal,
                    "GLM",
                    exit.value,
                    _glm_json(parsed) if exit is ProviderExit.OK else None,
                    invocation_id=invocation_id,
                    now=clock(),
                )
            settled = store.read_analysis_batches(tick_id)[ordinal]
            status = ModelCallStatus(settled["exit"])
            _provider_incident(
                store,
                provider="glm-5.2",
                status=status,
                tick_id=tick_id,
                now=clock(),
            )
            for item in group:
                glm_status[item.candidate_id] = status
            if status is ModelCallStatus.OK:
                parsed = parse_glm_batch(
                    canonical_encode(settled["result"]),
                    tuple(item.candidate_id for item in group),
                )
                glm_annotations.update(
                    {row.candidate_id: row for row in parsed.annotations}
                )
            if status in {ModelCallStatus.RATE_LIMITED, ModelCallStatus.AUTH_FAIL}:
                stop_glm = True

        sol_targets = tuple(
            item
            for item in candidates
            if (
                item.assignment
                in {CandidateAssignment.CRITICAL, CandidateAssignment.SAMPLED_AUDIT}
                or (
                    glm_status.get(item.candidate_id) is ModelCallStatus.OK
                    and (
                        glm_annotations[item.candidate_id].relevance
                        is not GlmRelevance.NONE
                        or glm_annotations[item.candidate_id].materiality
                        in {GlmMateriality.HIGH, GlmMateriality.UNKNOWN}
                    )
                )
            )
        )
        sol_results = {}
        sol_status = {}
        sol_batches = _chunks(sol_targets, SOL_MAX_CANDIDATES_PER_CALL)
        base_ordinal = len(glm_batches)
        sol_rows = tuple(
            {
                "ordinal": base_ordinal + offset,
                "provider": "SOL",
                "candidate_ids": tuple(item.candidate_id for item in group),
                "request_digest": hashlib.sha256(
                    canonical_encode([project_for_sol(item) for item in group])
                ).hexdigest(),
                "planned_at": now,
            }
            for offset, group in enumerate(sol_batches)
        )
        stop_sol = False
        for offset, group in enumerate(sol_batches):
            if stop_sol:
                break
            ordinal = base_ordinal + offset
            existing = next(
                (
                    row
                    for row in store.read_analysis_batches(tick_id)
                    if row["ordinal"] == ordinal
                ),
                None,
            )
            if existing is None:
                store.plan_analysis_batches(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    (sol_rows[offset],),
                    invocation_id=invocation_id,
                )
                existing = next(
                    row
                    for row in store.read_analysis_batches(tick_id)
                    if row["ordinal"] == ordinal
                )
            if existing["state"] == "PLANNED":
                store.start_analysis_batch(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    ordinal,
                    invocation_id=invocation_id,
                    now=clock(),
                )
                body = build_prompt_body(
                    PromptV1(
                        (
                            InstructionSegmentV1(SOL_ANALYSIS_V1),
                            *(
                                CandidateDataSegmentV1(
                                    item.candidate_id, project_for_sol(item)
                                )
                                for item in group
                            ),
                        )
                    )
                )
                exit, raw = _call(sol_call, body)
                evidence = frozenset(
                    ref.evidence_id for item in group for ref in item.evidence_refs
                )
                parsed = (
                    parse_sol_batch(
                        raw,
                        tuple(item.candidate_id for item in group),
                        evidence,
                    )
                    if exit is ProviderExit.OK and raw is not None
                    else None
                )
                if parsed is not None and parsed.exit is ProviderExit.PARSE_FAIL:
                    exit = ProviderExit.PARSE_FAIL
                store.settle_analysis_batch(
                    ProducerRole.ANALYSIS,
                    tick_id,
                    ordinal,
                    "SOL",
                    exit.value,
                    _sol_json(parsed) if exit is ProviderExit.OK else None,
                    invocation_id=invocation_id,
                    now=clock(),
                )
            settled = store.read_analysis_batches(tick_id)[ordinal]
            status = ModelCallStatus(settled["exit"])
            _provider_incident(
                store,
                provider="gpt-5.6-sol",
                status=status,
                tick_id=tick_id,
                now=clock(),
            )
            for item in group:
                sol_status[item.candidate_id] = status
            if status is ModelCallStatus.OK:
                parsed = parse_sol_batch(
                    canonical_encode(settled["result"]),
                    tuple(item.candidate_id for item in group),
                    frozenset(
                        ref.evidence_id for item in group for ref in item.evidence_refs
                    ),
                )
                sol_results.update({row.candidate_id: row for row in parsed.analyses})
            if status in {ModelCallStatus.RATE_LIMITED, ModelCallStatus.AUTH_FAIL}:
                stop_sol = True

        dispositions = 0
        for candidate in candidates:
            if candidate.candidate_id not in sol_status:
                if glm_status.get(candidate.candidate_id) is ModelCallStatus.OK:
                    action, reason = DispositionAction.SKIP, "glm_screened_none"
                else:
                    action, reason = DispositionAction.DEFER, "glm_failure_defer"
                decision_id = hashlib.sha256(
                    f"{tick_id}:{candidate.candidate_id}:{reason}".encode()
                ).hexdigest()[:32]
                disposition = DispositionV1(
                    decision_id,
                    tick_id,
                    candidate.candidate_id,
                    candidate.subject_id,
                    action,
                    reason,
                    (),
                    barrier.material_ticks,
                    glm_status.get(
                        candidate.candidate_id, ModelCallStatus.OUTCOME_UNKNOWN
                    ).value,
                    ModelCallStatus.OUTCOME_UNKNOWN.value,
                    "analysis-policy-v1",
                    clock(),
                )
                outbox = None
            else:
                disposition, outbox, rejection = _decision(
                    candidate,
                    glm_status.get(
                        candidate.candidate_id, ModelCallStatus.OUTCOME_UNKNOWN
                    ),
                    sol_status[candidate.candidate_id],
                    sol_results.get(candidate.candidate_id),
                    barrier.material_ticks,
                    clock(),
                )
                if rejection is ValidatorRejection.CRITICAL_SKIP_OR_MERGE:
                    _pipeline_incident(
                        store,
                        kind="validator-proposal-rejected",
                        subject=getattr(tick, "start_utc", now).date().isoformat(),
                        impact_ref=f"{tick_id}:{rejection.value}",
                        tick_id=tick_id,
                        now=clock(),
                    )
            store.record_disposition_with_outbox(
                ProducerRole.ANALYSIS,
                disposition,
                outbox,
            )
            if (
                disposition.action is DispositionAction.SKIP
                and disposition.reason_code == "glm_screened_none"
            ):
                session_date = getattr(tick, "start_utc", now).date().isoformat()
                existing_audit = next(
                    (
                        row
                        for row in store.read_audit_backlog(candidate.kind.value)
                        if row.subject_id == candidate.subject_id
                    ),
                    None,
                )
                if existing_audit is None:
                    store.upsert_audit_backlog(
                        ProducerRole.ANALYSIS,
                        candidate.kind.value,
                        candidate.subject_id,
                        session_date,
                        session_date,
                        "PENDING_AUDIT",
                    )
            if disposition.action is DispositionAction.DEFER and candidate.market in {
                "KR",
                "US",
            }:
                history = store.read_deferred_attempts(candidate.subject_id)
                first_seen = history[0].first_seen_tick if history else tick_id
                store.upsert_deferred(
                    ProducerRole.ANALYSIS,
                    DeferredAttemptV1(
                        candidate.subject_id,
                        Market(candidate.market),
                        first_seen,
                        tick_id,
                        tick_id,
                        candidate.candidate_id,
                        tuple(ref.evidence_id for ref in candidate.evidence_refs),
                        barrier.material_ticks,
                        getattr(tick, "start_utc", now) + timedelta(seconds=300),
                        disposition.reason_code,
                        candidate.assignment,
                        clock(),
                    ),
                )
            elif store.read_deferred_attempts(candidate.subject_id):
                store.terminalize_deferred(
                    ProducerRole.ANALYSIS,
                    candidate.subject_id,
                    DeferredTerminalState.DISPOSITIONED,
                    clock(),
                )
            dispositions += 1
        _record_failure_aggregates(store, tick_id=tick_id, now=clock())
        _complete_p10_audit(
            store,
            tick_id=tick_id,
            session_date=getattr(tick, "start_utc", now).date().isoformat(),
            now=clock(),
        )
        return AnalysisRunOutcomeV1(
            AnalysisRunState.COMPLETED,
            invocation_id,
            tick_id,
            len(candidates),
            dispositions,
        )
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)
