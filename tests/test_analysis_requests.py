from datetime import UTC, datetime, timedelta

import pytest

from tokmo.programs.analysis_pipeline import (
    RequestAnalysisSettlementV1,
    analyze_requests,
)
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_contracts import (
    AnalysisCoverageV1,
    AnalysisKind,
    AnalysisRequestV1,
    AnalysisResultV1,
    EvidenceRefV1,
    ExpectedAnalysisCoverageV1,
    FreshnessState,
    TriggerKindV1,
    TrustClass,
)
from tokmo.values.material_pack import JobKind
from tokmo.values.ops_kernel import (
    AlertChannel,
    AlertOutboxV1,
    CandidateAssignment,
    CandidateManifestV1,
    CandidateState,
    DispositionAction,
    DispositionV1,
    ProducerRole,
)

NOW = datetime(2026, 8, 4, 1, 0, tzinfo=UTC)
BINDING = "a" * 64
POLICY = "b" * 64


def request():
    rid = AnalysisRequestV1.compute_request_id(
        analysis_kind=AnalysisKind.EVENT,
        tick_id=42,
        candidate_id="candidate-1",
        material_binding_digest=BINDING,
        admission_policy_digest=POLICY,
    )
    return AnalysisRequestV1(
        rid,
        AnalysisKind.EVENT,
        TriggerKindV1.DETERMINISTIC_EVENT,
        42,
        "candidate-1",
        "delta-1",
        "subject-1",
        BINDING,
        "event-v1",
        POLICY,
        ("event_quote_threshold_move",),
        NOW,
        (
            ExpectedAnalysisCoverageV1(
                "KR",
                "005930",
                "evidence-1",
                42,
                NOW,
            ),
        ),
    )


def evidence(*, freshness=FreshnessState.FRESH, expires_at=None):
    return EvidenceRefV1(
        "evidence-1",
        __import__(
            "tokmo.values.ops_kernel", fromlist=["MaterialKindV1"]
        ).MaterialKindV1.QUOTE_STATE,
        "fixture",
        "record-1",
        NOW,
        NOW,
        NOW,
        NOW,
        "c" * 64,
        1,
        "fixture-v1",
        TrustClass.OFFICIAL_PROVIDER,
        freshness,
        expires_at or NOW + timedelta(minutes=10),
    )


def setup_store(tmp_path):
    store = OpsKernelStore(tmp_path / "ops.sqlite3")
    req = request()
    candidate = CandidateManifestV1(
        req.candidate_id,
        req.subject_id,
        "QUOTE_STATE",
        ("evidence-1",),
        CandidateAssignment.FULL_REVIEW,
        CandidateState.PENDING,
        NOW,
    )
    store.create_analysis_requests_with_manifest(
        ProducerRole.ANALYSIS,
        tick_id=42,
        started_at=NOW,
        material_ticks={JobKind.QUOTES: 42},
        material_binding_digest=BINDING,
        candidates=(candidate,),
        requests=(req,),
    )
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    return store, req


def settlement(req):
    result = AnalysisResultV1(
        AnalysisResultV1.compute_result_id(
            request_id=req.request_id, decision_id="decision-1"
        ),
        req.request_id,
        AnalysisKind.EVENT,
        "SUCCEEDED",
        "decision-1",
        "d" * 64,
        NOW,
    )
    coverage = AnalysisCoverageV1(
        AnalysisKind.EVENT,
        "KR",
        "005930",
        req.request_id,
        result.result_id,
        req.subject_id,
        42,
        NOW,
        NOW,
        POLICY,
        "evidence-1",
    )
    disposition = DispositionV1(
        "decision-1",
        42,
        req.candidate_id,
        req.subject_id,
        DispositionAction.SEND,
        "material_change",
        ("evidence-1",),
        {JobKind.QUOTES: 42},
        "OK",
        "OK",
        "policy-v1",
        NOW,
    )
    outbox = AlertOutboxV1.for_disposition(
        alert_id="alert-1",
        decision_id="decision-1",
        tick_id=42,
        subject_id=req.subject_id,
        channel=AlertChannel.SHADOW,
        severity="HIGH",
        body_evidence=("evidence-1",),
        created_at=NOW,
    )
    return RequestAnalysisSettlementV1(result, (coverage,), disposition, outbox)


def test_common_analyzer_starts_durable_request_before_fixture_call(tmp_path):
    store, req = setup_store(tmp_path)
    seen = []

    def analyzer(current):
        with store._read() as connection:
            seen.append(
                connection.execute(
                    "SELECT state FROM analysis_requests WHERE request_id=?",
                    (current.request_id,),
                ).fetchone()[0]
            )
        return settlement(current)

    outcome = analyze_requests(
        requests=(req,),
        evidence_by_request={req.request_id: (evidence(),)},
        store=store,
        analyzer=analyzer,
        now=NOW,
    )
    assert seen == ["ANALYZING"]
    assert outcome.succeeded == 1
    assert outcome.failed == 0


def test_started_unknown_restart_reconciles_before_execution_and_calls_analyzer_zero(
    tmp_path,
):
    store, req = setup_store(tmp_path)
    assert (
        store.start_analysis_request(
            ProducerRole.ANALYSIS,
            req.request_id,
            now=NOW,
        ).value
        == "CREATED"
    )
    calls = []
    outcome = analyze_requests(
        requests=(req,),
        evidence_by_request={req.request_id: (evidence(),)},
        store=store,
        analyzer=lambda current: calls.append(current),
        now=NOW,
    )
    assert calls == []
    assert outcome.duplicates == 1
    durable = store.read_analysis_request(req.request_id)
    assert durable["state"] == "UNKNOWN"
    assert durable["result_id"] == AnalysisResultV1.compute_result_id(
        request_id=req.request_id,
        decision_id=None,
    )


def test_terminal_duplicate_calls_analyzer_zero(tmp_path):
    store, req = setup_store(tmp_path)
    calls = []
    analyze_requests(
        requests=(req,),
        evidence_by_request={req.request_id: (evidence(),)},
        store=store,
        analyzer=lambda current: calls.append(current) or settlement(current),
        now=NOW,
    )
    outcome = analyze_requests(
        requests=(req,),
        evidence_by_request={req.request_id: (evidence(),)},
        store=store,
        analyzer=lambda current: calls.append(current) or settlement(current),
        now=NOW,
    )
    assert len(calls) == 1
    assert outcome.duplicates == 1


@pytest.mark.parametrize(
    "ref",
    [
        evidence(freshness=FreshnessState.STALE),
        evidence(expires_at=NOW - timedelta(seconds=1)),
    ],
)
def test_stale_or_expired_required_evidence_calls_analyzer_zero_and_writes_coverage_zero(
    tmp_path, ref
):
    store, req = setup_store(tmp_path)
    calls = []
    outcome = analyze_requests(
        requests=(req,),
        evidence_by_request={req.request_id: (ref,)},
        store=store,
        analyzer=lambda current: calls.append(current),
        now=NOW,
    )
    assert calls == []
    assert outcome.failed == 1
    with store._read() as connection:
        assert (
            connection.execute("SELECT count(*) FROM analysis_coverage").fetchone()[0]
            == 0
        )


def test_health_coverage_kind_has_contract_but_no_producer_or_schedule():
    assert AnalysisKind.HEALTH_COVERAGE.value == "HEALTH_COVERAGE"
