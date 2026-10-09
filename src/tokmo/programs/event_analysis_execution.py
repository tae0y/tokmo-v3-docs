"""Fixture-only EVENT vertical execution through durable request boundaries.

This module contains no provider adapter and loads no secret.  Callers may
construct :class:`AnalysisExecutionV1` only with local fixture callables.  The
production cadence accepts the typed facade but leaves it disabled by default.
"""

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from typing import cast

from tokmo.programs.analysis_pipeline import (
    RequestAnalysisSettlementV1,
    _call,
    _decision,
    _glm_json,
    _sol_json,
    analyze_requests,
)
from tokmo.programs.analysis_redaction import project_for_glm, project_for_sol
from tokmo.programs.event_admission import (
    DEFAULT_EVENT_ADMISSION_POLICY_V1,
    EventAdmissionPolicyV1,
    evaluate_event_admission,
)
from tokmo.programs.material_delta import (
    BaselineAdvanceV1,
    build_material_deltas,
    quote_history_subjects,
)
from tokmo.programs.portfolio_ai_payload import (
    PortfolioCandidateContextV1,
    build_portfolio_event_payloads_v1,
)
from tokmo.programs.portfolio_calculations import (
    compute_portfolio_snapshot,
    project_portfolio_presentation,
)
from tokmo.programs.prompt_builder import (
    GLM_ANALYSIS_V1,
    MODEL_REQUEST_MAX_BYTES,
    SOL_ANALYSIS_V1,
    InstructionSegmentV1,
    PromptV1,
    build_prompt_body,
)
from tokmo.values.analysis_contracts import (
    AnalysisCoverageV1,
    AnalysisKind,
    AnalysisRequestV1,
    AnalysisResultV1,
    CandidateDataSegmentV1,
    CandidateEnvelopeV1,
    ExpectedAnalysisCoverageV1,
    GlmMateriality,
    GlmRelevance,
    MaterialKindV1,
    ModelCallStatus,
    ProviderExit,
    TriggerKindV1,
    candidate_id_for,
    parse_glm_batch,
    parse_sol_batch,
)
from tokmo.values.material_pack import JobKind, canonical_encode
from tokmo.values.ops_kernel import (
    CandidateAssignment,
    CandidateManifestV1,
    CandidateState,
    DeferredTerminalState,
    ProducerRole,
)
from tokmo.values.portfolio_ai_payload import (
    PortfolioPayloadBuildStatus,
    normalize_portfolio_payload_v1,
)
from tokmo.values.portfolio_calculations import ScopeUnresolved
from tokmo.values.portfolio_scope import resolve_account_scope_pattern
from tokmo.values.production_cadence import AnalysisExecutionV1
from tokmo.values.session_gate import Market

_JOB_BY_KIND = {
    MaterialKindV1.HOLDINGS_CHANGE: JobKind.HOLDINGS,
    MaterialKindV1.QUOTE_STATE: JobKind.QUOTES,
    MaterialKindV1.COMPLETED_EXECUTION: JobKind.TRADES,
}


class EventAnalysisExecutionStatusV1(str, Enum):
    COMPLETED = "COMPLETED"
    NO_CANDIDATE = "NO_CANDIDATE"


@dataclass(frozen=True)
class EventAnalysisExecutionOutcomeV1:
    delta_count: int
    admitted_count: int
    succeeded: int
    failed: int
    unknown: int
    duplicates: int
    status: EventAnalysisExecutionStatusV1 = EventAnalysisExecutionStatusV1.COMPLETED
    selected_count: int = 0
    suppressed_count: int = 0
    no_call_settled: int = 0


@dataclass(frozen=True)
class _DurableFixtureAnalyzerV1:
    store: object
    candidates: dict[str, CandidateEnvelopeV1]
    ordinals: dict[str, int]
    material_ticks: dict[JobKind, int | None]
    glm_call: Callable
    sol_call: Callable
    now: datetime
    payloads: dict[str, object] | None = None

    def _batch(
        self,
        request,
        candidate,
        *,
        provider,
        ordinal,
        projection,
        call,
        instruction,
        parser,
        serializer,
    ):
        invocation_id = request.request_id
        request_digest = hashlib.sha256(canonical_encode(projection)).hexdigest()
        self.store.plan_analysis_batches(
            ProducerRole.ANALYSIS,
            request.tick_id,
            (
                {
                    "ordinal": ordinal,
                    "provider": provider,
                    "candidate_ids": (candidate.candidate_id,),
                    "request_digest": request_digest,
                    "planned_at": self.now,
                },
            ),
            invocation_id=invocation_id,
        )
        row = next(
            item
            for item in self.store.read_analysis_batches(request.tick_id)
            if item["ordinal"] == ordinal
        )
        if row["state"] == "PLANNED":
            self.store.start_analysis_batch(
                ProducerRole.ANALYSIS,
                request.tick_id,
                ordinal,
                invocation_id=invocation_id,
                now=self.now,
            )
            body = build_prompt_body(
                PromptV1(
                    (
                        InstructionSegmentV1(instruction),
                        CandidateDataSegmentV1(candidate.candidate_id, projection),
                    )
                )
            )
            exit_status, raw = _call(call, body)
            parsed = (
                parser(raw)
                if exit_status is ProviderExit.OK and raw is not None
                else None
            )
            if parsed is not None and parsed.exit is ProviderExit.PARSE_FAIL:
                exit_status = ProviderExit.PARSE_FAIL
            self.store.settle_analysis_batch(
                ProducerRole.ANALYSIS,
                request.tick_id,
                ordinal,
                provider,
                exit_status.value,
                serializer(parsed) if exit_status is ProviderExit.OK else None,
                invocation_id=invocation_id,
                now=self.now,
            )
        settled = next(
            item
            for item in self.store.read_analysis_batches(request.tick_id)
            if item["ordinal"] == ordinal
        )
        if settled["state"] != "SETTLED" or settled["exit"] != "OK":
            raise ValueError(f"{provider} fixture analysis failed")
        return settled

    def __call__(self, request):
        candidate = self.candidates[request.request_id]
        base = self.ordinals[request.request_id]
        payload = (
            None if self.payloads is None else self.payloads.get(candidate.candidate_id)
        )
        glm_projection = (
            project_for_glm(candidate)
            if payload is None
            else normalize_portfolio_payload_v1(payload)
        )
        glm_settled = self._batch(
            request,
            candidate,
            provider="GLM",
            ordinal=base,
            projection=glm_projection,
            call=self.glm_call,
            instruction=GLM_ANALYSIS_V1,
            parser=lambda raw: parse_glm_batch(raw, (candidate.candidate_id,)),
            serializer=_glm_json,
        )
        glm = parse_glm_batch(
            canonical_encode(glm_settled["result"]),
            (candidate.candidate_id,),
        )
        annotation = glm.annotations[0]
        if annotation.relevance is GlmRelevance.NONE and annotation.materiality not in {
            GlmMateriality.HIGH,
            GlmMateriality.UNKNOWN,
        }:
            raise ValueError("fixture GLM did not select admitted candidate")

        sol_projection = (
            project_for_sol(candidate)
            if payload is None
            else normalize_portfolio_payload_v1(payload)
        )
        evidence_ids = frozenset(
            ref.evidence_id
            for ref in (
                candidate.evidence_refs if payload is None else payload.evidence_refs
            )
        )
        sol_settled = self._batch(
            request,
            candidate,
            provider="SOL",
            ordinal=base + 1,
            projection=sol_projection,
            call=self.sol_call,
            instruction=SOL_ANALYSIS_V1,
            parser=lambda raw: parse_sol_batch(
                raw,
                (candidate.candidate_id,),
                evidence_ids,
            ),
            serializer=_sol_json,
        )
        sol = parse_sol_batch(
            canonical_encode(sol_settled["result"]),
            (candidate.candidate_id,),
            evidence_ids,
        ).analyses[0]
        disposition, outbox, rejection = _decision(
            candidate,
            ModelCallStatus.OK,
            ModelCallStatus.OK,
            sol,
            self.material_ticks,
            self.now,
        )
        if rejection is not None:
            raise ValueError("fixture disposition rejected")
        artifact_digest = hashlib.sha256(
            canonical_encode(
                {
                    "glm": glm_settled["result"],
                    "sol": sol_settled["result"],
                }
            )
        ).hexdigest()
        result = AnalysisResultV1(
            AnalysisResultV1.compute_result_id(
                request_id=request.request_id,
                decision_id=disposition.decision_id,
            ),
            request.request_id,
            request.analysis_kind,
            "SUCCEEDED",
            disposition.decision_id,
            artifact_digest,
            self.now,
        )
        coverage = tuple(
            AnalysisCoverageV1(
                request.analysis_kind,
                expected.market,
                expected.symbol,
                request.request_id,
                result.result_id,
                request.subject_id,
                expected.material_tick_id,
                expected.material_observed_at,
                result.analyzed_at,
                request.admission_policy_digest,
                expected.evidence_id,
            )
            for expected in request.expected_coverage
        )
        return RequestAnalysisSettlementV1(
            result,
            coverage,
            disposition,
            outbox,
        )


@dataclass(frozen=True)
class _DispatchAnalyzerV1:
    selected: _DurableFixtureAnalyzerV1
    selected_request_ids: frozenset[str]
    now: datetime

    def __call__(self, request):
        if request.request_id in self.selected_request_ids:
            return self.selected(request)
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
            self.now,
        )
        return RequestAnalysisSettlementV1(result, (), None, None)


@dataclass(frozen=True)
class FixtureEventAnalysisExecutionV1(AnalysisExecutionV1):
    """Typed, fixture-only EVENT facade; production construction is disabled."""

    glm_fixture_call: Callable
    sol_fixture_call: Callable
    policy: EventAdmissionPolicyV1 = DEFAULT_EVENT_ADMISSION_POLICY_V1
    resolve_scope: Callable | None = None
    fx_evidence: object | None = None

    def __post_init__(self):
        if not callable(self.glm_fixture_call) or not callable(self.sol_fixture_call):
            raise ValueError("fixture model callables required")

    def _call_ai_safe(self, *, tick, barrier, store, now):
        store.reconcile_analysis_requests(ProducerRole.ANALYSIS, now=now)
        due_rows = tuple(
            row
            for market in sorted(barrier.proceed_markets, key=lambda item: item.value)
            for row in store.read_due_deferred(
                market,
                getattr(tick, "start_utc", now),
            )
        )
        due_by_market_subject = {(row.market, row.subject_id): row for row in due_rows}
        baselines = {}
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
                baselines[(kind, row["subject_id"])] = value
        built = build_material_deltas(
            barrier,
            baselines=baselines,
            history=store.read_quote_state_history(quote_history_subjects(barrier)),
            now=now,
            incident_rows=store.read_incident_rows(),
            resolve_scope=(self.resolve_scope or resolve_account_scope_pattern),
        )
        decisions = evaluate_event_admission(self.policy, built.deltas, now=now)
        by_delta = {row.delta_id: row for row in built.deltas}
        admitted = tuple(
            (by_delta[row.delta_id], row)
            for row in decisions
            if row.decision == "ADMIT"
        )
        context_rows = []
        metadata = []
        for delta, decision in admitted:
            assignment = (
                CandidateAssignment.CRITICAL
                if delta.material_kind
                in {
                    MaterialKindV1.COMPLETED_EXECUTION,
                    MaterialKindV1.OPERATIONAL_INCIDENT,
                }
                else CandidateAssignment.FULL_REVIEW
            )
            candidate_id = candidate_id_for(
                delta.tick_id, delta.material_kind, delta.subject_id
            )
            scope_binding = None
            if delta.material_kind is MaterialKindV1.HOLDINGS_CHANGE:
                prior = baselines.get((delta.material_kind, delta.subject_id))
                if prior is not None and prior.account_scope is not None:
                    resolved = self.resolve_scope(prior.account_scope)
                    if resolved is not ScopeUnresolved.UNRESOLVED:
                        from tokmo.values.portfolio_ai_payload import (
                            CandidateScopeBindingV1,
                        )

                        scope_binding = CandidateScopeBindingV1(
                            delta.delta_id, resolved
                        )
            context_rows.append(
                PortfolioCandidateContextV1(
                    candidate_id,
                    delta,
                    decision,
                    assignment,
                    scope_binding,
                )
            )
            metadata.append((candidate_id, delta, decision, assignment))
        snapshot = compute_portfolio_snapshot(
            barrier,
            resolve_scope=self.resolve_scope,
            fx_evidence=self.fx_evidence,
        )
        presentation = (
            project_portfolio_presentation(snapshot.body)
            if snapshot.body is not None
            else None
        )
        remaining_contexts = tuple(context_rows)
        oversize_ids = set()
        while True:
            payload_build = build_portfolio_event_payloads_v1(
                snapshot,
                presentation,
                remaining_contexts,
                built.material_binding_digest,
                barrier.mask_digest,
            )
            if payload_build.status in {
                PortfolioPayloadBuildStatus.PRIVATE_SCOPE_UNRESOLVED,
                PortfolioPayloadBuildStatus.SNAPSHOT_INCOMPLETE,
                PortfolioPayloadBuildStatus.CONTRACT_INVALID,
            }:
                return EventAnalysisExecutionOutcomeV1(
                    len(built.deltas),
                    0,
                    0,
                    0,
                    0,
                    0,
                    EventAnalysisExecutionStatusV1.NO_CANDIDATE,
                    0,
                    0,
                    0,
                )
            selected_payloads = {
                row.candidate_id: row for row in payload_build.selected
            }
            newly_oversize = set()
            for candidate_id, payload in selected_payloads.items():
                projection = cast(
                    Mapping[str, object], normalize_portfolio_payload_v1(payload)
                )
                for instruction in (GLM_ANALYSIS_V1, SOL_ANALYSIS_V1):
                    exact_body = build_prompt_body(
                        PromptV1(
                            (
                                InstructionSegmentV1(instruction),
                                CandidateDataSegmentV1(candidate_id, projection),
                            )
                        )
                    )
                    if len(exact_body) > MODEL_REQUEST_MAX_BYTES:
                        newly_oversize.add(candidate_id)
                        break
            if not newly_oversize:
                break
            oversize_ids.update(newly_oversize)
            remaining_contexts = tuple(
                row
                for row in remaining_contexts
                if row.candidate_id not in oversize_ids
            )
            if not remaining_contexts:
                selected_payloads = {}
                break
        material_ticks = {
            job: (
                barrier.materials[job].pack_tick_id
                if job in barrier.materials
                else None
            )
            for job in JobKind
        }
        requests = []
        candidates = []
        envelopes = {}
        subject_by_candidate = {}
        for candidate_id, delta, decision, assignment in metadata:
            envelope = CandidateEnvelopeV1(
                candidate_id,
                delta.subject_id,
                delta.material_kind,
                delta.market,
                delta.tick_id,
                assignment,
                delta.evidence_refs,
                delta.current_fields,
                delta.created_at,
            )
            expected = ()
            if delta.market in {"KR", "US"} and delta.symbol is not None:
                job = _JOB_BY_KIND[delta.material_kind]
                material = barrier.materials[job]
                evidence = delta.evidence_refs[0]
                expected = (
                    ExpectedAnalysisCoverageV1(
                        delta.market,
                        delta.symbol,
                        evidence.evidence_id,
                        material.pack_tick_id,
                        evidence.observed_at,
                    ),
                )
            request_id = AnalysisRequestV1.compute_request_id(
                analysis_kind=AnalysisKind.EVENT,
                tick_id=delta.tick_id,
                candidate_id=candidate_id,
                material_binding_digest=built.material_binding_digest,
                admission_policy_digest=self.policy.policy_digest,
            )
            request = AnalysisRequestV1(
                request_id,
                AnalysisKind.EVENT,
                TriggerKindV1.DETERMINISTIC_EVENT,
                delta.tick_id,
                candidate_id,
                delta.delta_id,
                delta.subject_id,
                built.material_binding_digest,
                self.policy.policy_version,
                self.policy.policy_digest,
                decision.admission_reason_codes,
                now,
                expected,
            )
            requests.append(request)
            candidates.append(
                CandidateManifestV1(
                    candidate_id,
                    delta.subject_id,
                    delta.material_kind.value,
                    tuple(ref.evidence_id for ref in delta.evidence_refs),
                    assignment,
                    CandidateState.PENDING,
                    now,
                )
            )
            envelopes[request_id] = envelope
            subject_by_candidate[candidate_id] = (
                (Market(delta.market), delta.subject_id)
                if delta.market in {"KR", "US"}
                else None
            )
        store.create_analysis_requests_with_manifest(
            ProducerRole.ANALYSIS,
            tick_id=barrier.tick_id,
            started_at=now,
            material_ticks=material_ticks,
            material_binding_digest=built.material_binding_digest,
            candidates=tuple(candidates),
            requests=tuple(requests),
            baseline_advances=built.baseline_advances,
            history_advances=built.history_advances,
        )
        summary = store.read_manifest_summary(barrier.tick_id)
        if summary.state.value == "OPEN":
            store.seal_manifest(ProducerRole.ANALYSIS, barrier.tick_id, now)
        selected_request_ids = frozenset(
            request.request_id
            for request in requests
            if request.candidate_id in selected_payloads
        )
        ordinals = {
            request.request_id: index * 2 for index, request in enumerate(requests)
        }
        selected_analyzer = _DurableFixtureAnalyzerV1(
            store,
            envelopes,
            ordinals,
            material_ticks,
            self.glm_fixture_call,
            self.sol_fixture_call,
            now,
            selected_payloads,
        )
        analyzer = _DispatchAnalyzerV1(selected_analyzer, selected_request_ids, now)
        evidence = {
            request.request_id: envelopes[request.request_id].evidence_refs
            for request in requests
        }
        outcome = analyze_requests(
            requests=tuple(requests),
            evidence_by_request=evidence,
            store=store,
            analyzer=analyzer,
            now=now,
        )
        suppressed_ids = {
            request.candidate_id
            for request in requests
            if request.request_id not in selected_request_ids
        }
        for candidate_id in sorted(suppressed_ids):
            market_subject = subject_by_candidate[candidate_id]
            if market_subject is not None and market_subject in due_by_market_subject:
                store.terminalize_deferred(
                    ProducerRole.ANALYSIS,
                    market_subject[1],
                    DeferredTerminalState.TERMINAL,
                    now,
                )
        selected_count = len(selected_request_ids)
        suppressed_count = len(requests) - selected_count
        status = (
            EventAnalysisExecutionStatusV1.COMPLETED
            if selected_count
            else EventAnalysisExecutionStatusV1.NO_CANDIDATE
        )
        return EventAnalysisExecutionOutcomeV1(
            len(built.deltas),
            len(requests),
            outcome.succeeded,
            outcome.failed,
            outcome.unknown,
            outcome.duplicates,
            status,
            selected_count,
            suppressed_count,
            suppressed_count,
        )

    def __call__(self, *, tick, barrier, store, now):
        if self.resolve_scope is not None:
            return self._call_ai_safe(tick=tick, barrier=barrier, store=store, now=now)
        baselines = {}
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
                baselines[(kind, row["subject_id"])] = value
        built = build_material_deltas(
            barrier,
            baselines=baselines,
            history=store.read_quote_state_history(quote_history_subjects(barrier)),
            now=now,
            incident_rows=store.read_incident_rows(),
            resolve_scope=resolve_account_scope_pattern,
        )
        decisions = evaluate_event_admission(
            self.policy,
            built.deltas,
            now=now,
        )
        by_delta = {row.delta_id: row for row in built.deltas}
        admitted = tuple(
            (by_delta[row.delta_id], row)
            for row in decisions
            if row.decision == "ADMIT"
        )
        material_ticks = {
            job: (
                barrier.materials[job].pack_tick_id
                if job in barrier.materials
                else None
            )
            for job in JobKind
        }
        requests = []
        candidates = []
        envelopes = {}
        for delta, decision in admitted:
            assignment = (
                CandidateAssignment.CRITICAL
                if delta.material_kind
                in {
                    MaterialKindV1.COMPLETED_EXECUTION,
                    MaterialKindV1.OPERATIONAL_INCIDENT,
                }
                else CandidateAssignment.FULL_REVIEW
            )
            candidate_id = candidate_id_for(
                delta.tick_id,
                delta.material_kind,
                delta.subject_id,
            )
            envelope = CandidateEnvelopeV1(
                candidate_id,
                delta.subject_id,
                delta.material_kind,
                delta.market,
                delta.tick_id,
                assignment,
                delta.evidence_refs,
                delta.current_fields,
                delta.created_at,
            )
            expected = ()
            if delta.market in {"KR", "US"} and delta.symbol is not None:
                job = _JOB_BY_KIND[delta.material_kind]
                material = barrier.materials[job]
                evidence = delta.evidence_refs[0]
                expected = (
                    ExpectedAnalysisCoverageV1(
                        delta.market,
                        delta.symbol,
                        evidence.evidence_id,
                        material.pack_tick_id,
                        evidence.observed_at,
                    ),
                )
            request_id = AnalysisRequestV1.compute_request_id(
                analysis_kind=AnalysisKind.EVENT,
                tick_id=delta.tick_id,
                candidate_id=candidate_id,
                material_binding_digest=built.material_binding_digest,
                admission_policy_digest=self.policy.policy_digest,
            )
            reason_codes = tuple(
                result.reason_code.value
                for result in decision.matched_results
                if result.matched
            )
            request = AnalysisRequestV1(
                request_id,
                AnalysisKind.EVENT,
                TriggerKindV1.DETERMINISTIC_EVENT,
                delta.tick_id,
                candidate_id,
                delta.delta_id,
                delta.subject_id,
                built.material_binding_digest,
                self.policy.policy_version,
                self.policy.policy_digest,
                reason_codes,
                now,
                expected,
            )
            requests.append(request)
            candidates.append(
                CandidateManifestV1(
                    candidate_id,
                    delta.subject_id,
                    delta.material_kind.value,
                    tuple(ref.evidence_id for ref in delta.evidence_refs),
                    assignment,
                    CandidateState.PENDING,
                    now,
                )
            )
            envelopes[request_id] = envelope

        store.create_analysis_requests_with_manifest(
            ProducerRole.ANALYSIS,
            tick_id=barrier.tick_id,
            started_at=now,
            material_ticks=material_ticks,
            material_binding_digest=built.material_binding_digest,
            candidates=tuple(candidates),
            requests=tuple(requests),
            baseline_advances=built.baseline_advances,
            history_advances=built.history_advances,
        )
        summary = store.read_manifest_summary(barrier.tick_id)
        if summary.state.value == "OPEN":
            store.seal_manifest(ProducerRole.ANALYSIS, barrier.tick_id, now)
        ordinals = {
            request.request_id: index * 2 for index, request in enumerate(requests)
        }
        analyzer = _DurableFixtureAnalyzerV1(
            store,
            envelopes,
            ordinals,
            material_ticks,
            self.glm_fixture_call,
            self.sol_fixture_call,
            now,
        )
        evidence = {
            request.request_id: envelopes[request.request_id].evidence_refs
            for request in requests
        }
        outcome = analyze_requests(
            requests=tuple(requests),
            evidence_by_request=evidence,
            store=store,
            analyzer=analyzer,
            now=now,
        )
        return EventAnalysisExecutionOutcomeV1(
            len(built.deltas),
            len(requests),
            outcome.succeeded,
            outcome.failed,
            outcome.unknown,
            outcome.duplicates,
        )
