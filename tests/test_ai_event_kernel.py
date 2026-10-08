import os
import sqlite3
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from tokmo.records import ops_kernel as kernel_module
from tokmo.records.ops_kernel import OpsKernelStore
from tokmo.values.analysis_contracts import (
    AnalysisCoverageV1,
    AnalysisKind,
    AnalysisRequestV1,
    AnalysisResultV1,
    ExpectedAnalysisCoverageV1,
    TriggerKindV1,
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
    OpsKernelConflictError,
    OpsKernelCorruptError,
    OpsKernelMigrationRequiredError,
    ProducerRole,
    ReplayResult,
)

NOW = datetime(2026, 8, 4, 1, 2, 3, tzinfo=timezone.utc)
BINDING = "a" * 64
POLICY = "b" * 64


def make_store(tmp_path):
    return OpsKernelStore(tmp_path / "ops" / "ops.sqlite3")


def candidate(candidate_id="candidate-1", subject_id="subject-1"):
    return CandidateManifestV1(
        candidate_id, subject_id, "QUOTE_STATE", ("evidence-1",),
        CandidateAssignment.FULL_REVIEW, CandidateState.PENDING, NOW,
    )


def request(candidate_id="candidate-1", subject_id="subject-1", *, policy=POLICY):
    request_id = AnalysisRequestV1.compute_request_id(
        analysis_kind=AnalysisKind.EVENT,
        tick_id=42,
        candidate_id=candidate_id,
        material_binding_digest=BINDING,
        admission_policy_digest=policy,
    )
    return AnalysisRequestV1(
        request_id, AnalysisKind.EVENT, TriggerKindV1.DETERMINISTIC_EVENT,
        42, candidate_id, "delta-1", subject_id, BINDING,
        "event-v1", policy, ("event_quote_threshold_move",), NOW,
        (ExpectedAnalysisCoverageV1(
            "KR", "005930", "evidence-1", 41, NOW,
        ),),
    )


def create_requests(store, requests=None, *, inject_crash_at=None):
    requests = (request(),) if requests is None else tuple(requests)
    return store.create_analysis_requests_with_manifest(
        ProducerRole.ANALYSIS,
        tick_id=42,
        started_at=NOW,
        material_ticks={JobKind.HOLDINGS: 40, JobKind.QUOTES: 41, JobKind.TRADES: 39},
        material_binding_digest=BINDING,
        candidates=tuple(candidate(row.candidate_id, row.subject_id) for row in requests),
        requests=requests,
        inject_crash_at=inject_crash_at,
    )


def successful_result(req):
    return AnalysisResultV1(
        AnalysisResultV1.compute_result_id(request_id=req.request_id, decision_id="decision-1"),
        req.request_id, AnalysisKind.EVENT, "SUCCEEDED", "decision-1",
        "c" * 64, NOW,
    )


def coverage(req, symbol="005930"):
    result = successful_result(req)
    return AnalysisCoverageV1(
        AnalysisKind.EVENT, "KR", symbol, req.request_id, result.result_id,
        req.subject_id, 41, NOW, NOW, POLICY, "evidence-1",
    )


def disposition_and_outbox(req):
    disposition = DispositionV1(
        "decision-1", 42, req.candidate_id, req.subject_id,
        DispositionAction.SEND, "material_change", ("evidence-1",),
        {JobKind.QUOTES: 41}, "OK", "OK", "policy-v1", NOW,
    )
    outbox = AlertOutboxV1.for_disposition(
        alert_id="alert-1", decision_id="decision-1", tick_id=42,
        subject_id=req.subject_id, channel=AlertChannel.SHADOW,
        severity="HIGH", body_evidence=("evidence-1",), created_at=NOW,
    )
    return disposition, outbox


def test_schema_v9_exact_tables_indexes_foreign_keys_and_strict(tmp_path):
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            kernel_module.SCHEMA_VERSION,
        )
        tables = {row[1]: row[5] for row in connection.execute("PRAGMA table_list")}
        assert tables["analysis_requests"] == 1
        assert tables["analysis_coverage"] == 1
        assert connection.execute(
            "SELECT name FROM sqlite_schema WHERE type='index' AND name='analysis_coverage_latest'"
        ).fetchone() == ("analysis_coverage_latest",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        request_fks = connection.execute("PRAGMA foreign_key_list(analysis_requests)").fetchall()
        coverage_fks = connection.execute("PRAGMA foreign_key_list(analysis_coverage)").fetchall()
        assert {row[2] for row in request_fks} == {"candidate_manifest"}
        assert {row[2] for row in coverage_fks} == {"analysis_requests"}


def test_schema_v9_durably_binds_expected_coverage_and_evidence_provenance(tmp_path):
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        request_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(analysis_requests)"
            )
        }
        coverage_columns = {
            row[1] for row in connection.execute(
                "PRAGMA table_info(analysis_coverage)"
            )
        }
    assert "expected_coverage_json" in request_columns
    assert "evidence_id" in coverage_columns


def test_v6_constructor_requires_explicit_migration_and_preserves_every_existing_row(tmp_path):
    path = tmp_path / "ops" / "ops.sqlite3"
    path.parent.mkdir(mode=0o700)
    connection = sqlite3.connect(path)
    connection.executescript(kernel_module._LEGACY_SCHEMA_SQL_V6)
    connection.execute(
        "INSERT INTO manifest_runs VALUES(42,?,?,?,?,?,'SEALED',?,NULL)",
        (
            "2026-08-04T01:02:03.000000Z", "{}", BINDING, "{}", 0,
            "2026-08-04T01:02:03.000000Z",
        ),
    )
    connection.commit()
    before_counts = {
        table: connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
        for table in kernel_module._LEGACY_TABLE_ORDER_V6
    }
    connection.close()
    os.chmod(path, 0o600)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns

    with pytest.raises(OpsKernelMigrationRequiredError, match="migration required"):
        OpsKernelStore(path)
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    with sqlite3.connect(path) as preserved:
        assert preserved.execute("PRAGMA user_version").fetchone() == (6,)
        after_counts = {
            table: preserved.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in kernel_module._LEGACY_TABLE_ORDER_V6
        }
        assert after_counts == before_counts
        assert preserved.execute("PRAGMA foreign_key_check").fetchall() == []
        assert preserved.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_v6_constructor_never_enters_removed_copy_migration_hook(tmp_path, monkeypatch):
    path = tmp_path / "ops" / "ops.sqlite3"
    path.parent.mkdir(mode=0o700)
    with sqlite3.connect(path) as connection:
        connection.executescript(kernel_module._LEGACY_SCHEMA_SQL_V6)
        connection.execute(
            "INSERT INTO manifest_runs VALUES(42,?,?,?,?,?,'SEALED',?,NULL)",
            (
                "2026-08-04T01:02:03.000000Z", "{}", BINDING, "{}", 0,
                "2026-08-04T01:02:03.000000Z",
            ),
        )
    os.chmod(path, 0o600)
    before_bytes = path.read_bytes()
    before_mtime = path.stat().st_mtime_ns
    called = False

    def drift(_store, connection, legacy_version):
        nonlocal called
        called = True

    monkeypatch.setattr(
        OpsKernelStore, "_migration_copy_validation_hook", drift, raising=False,
    )
    with pytest.raises(OpsKernelMigrationRequiredError, match="migration required"):
        OpsKernelStore(path)
    assert called is False
    assert path.read_bytes() == before_bytes
    assert path.stat().st_mtime_ns == before_mtime
    with sqlite3.connect(path) as original:
        assert original.execute("PRAGMA user_version").fetchone() == (6,)
        assert original.execute(
            "SELECT state FROM manifest_runs WHERE tick_id=42"
        ).fetchone() == ("SEALED",)


def test_event_request_id_is_domain_separated_and_never_candidate_id():
    req = request()
    assert req.request_id != req.candidate_id
    assert len(req.request_id) == 32
    assert req.request_id == AnalysisRequestV1.compute_request_id(
        analysis_kind=AnalysisKind.EVENT, tick_id=42,
        candidate_id=req.candidate_id, material_binding_digest=BINDING,
        admission_policy_digest=POLICY,
    )
    changed = AnalysisRequestV1.compute_request_id(
        analysis_kind=AnalysisKind.EVENT, tick_id=42,
        candidate_id=req.candidate_id, material_binding_digest=BINDING,
        admission_policy_digest="d" * 64,
    )
    assert changed != req.request_id


def test_request_manifest_and_request_rows_are_one_atomic_transaction(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(RuntimeError, match="injected"):
        create_requests(store, inject_crash_at="after_requests")
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM manifest_runs").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM candidate_manifest").fetchone() == (0,)
        assert connection.execute("SELECT count(*) FROM analysis_requests").fetchone() == (0,)


def test_request_exact_replay_duplicate_and_payload_or_policy_drift_conflict(tmp_path):
    store = make_store(tmp_path)
    assert create_requests(store) is ReplayResult.CREATED
    assert create_requests(store) is ReplayResult.DUPLICATE
    drift = request(policy="d" * 64)
    # Same tick/candidate is authoritative even if the policy-derived request id changes.
    with pytest.raises(OpsKernelConflictError):
        create_requests(store, (drift,))


def test_start_reconcile_unknown_updates_request_and_candidate_atomically(tmp_path):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    assert store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW) is ReplayResult.CREATED
    reconciled = store.reconcile_analysis_requests(ProducerRole.ANALYSIS, now=NOW)
    expected_result_id = AnalysisResultV1.compute_result_id(
        request_id=req.request_id, decision_id=None,
    )
    assert reconciled == ({
        "request_id": req.request_id,
        "result_id": expected_result_id,
        "replay": ReplayResult.CREATED,
    },)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state FROM analysis_requests WHERE request_id=?", (req.request_id,)
        ).fetchone() == ("UNKNOWN",)
        assert connection.execute(
            "SELECT state FROM candidate_manifest WHERE tick_id=42 AND candidate_id=?",
            (req.candidate_id,),
        ).fetchone() == ("TERMINAL",)
        assert connection.execute("SELECT count(*) FROM analysis_coverage").fetchone() == (0,)
        assert connection.execute(
            "SELECT kind,subject,impact_ref,state FROM incidents"
        ).fetchone() == (
            "analysis-request-unknown", req.request_id, req.request_id, "OPEN",
        )
        assert connection.execute(
            "SELECT origin,channel,incident_kind,incident_subject FROM alert_outbox"
        ).fetchone() == (
            "INCIDENT", "SHADOW", "analysis-request-unknown", req.request_id,
        )


def test_success_coverage_and_candidate_request_settle_in_one_transaction(tmp_path):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW)
    result = successful_result(req)
    row = coverage(req)
    disposition, outbox = disposition_and_outbox(req)
    with pytest.raises(RuntimeError, match="injected"):
        store.settle_analysis_result_with_coverage(
            ProducerRole.ANALYSIS, request_id=req.request_id, result=result,
            coverage_rows=(row,), disposition=disposition,
            outbox_or_none=outbox, inject_crash_at="after_coverage",
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT state,result_id FROM analysis_requests WHERE request_id=?", (req.request_id,)
        ).fetchone() == ("ANALYZING", None)
        assert connection.execute("SELECT count(*) FROM analysis_coverage").fetchone() == (0,)
        assert connection.execute(
            "SELECT state FROM candidate_manifest WHERE tick_id=42 AND candidate_id=?",
            (req.candidate_id,),
        ).fetchone() == ("PENDING",)
    assert store.settle_analysis_result_with_coverage(
        ProducerRole.ANALYSIS, request_id=req.request_id, result=result,
        coverage_rows=(row,), disposition=disposition, outbox_or_none=outbox,
    ) is ReplayResult.CREATED
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM analysis_coverage").fetchone() == (1,)
        assert connection.execute(
            "SELECT state,result_id FROM analysis_requests WHERE request_id=?", (req.request_id,)
        ).fetchone() == ("SUCCEEDED", result.result_id)
        assert connection.execute(
            "SELECT state FROM candidate_manifest WHERE tick_id=42 AND candidate_id=?",
            (req.candidate_id,),
        ).fetchone() == ("DISPOSITIONED",)


def test_non_success_forbids_coverage_and_success_symbol_requires_coverage(tmp_path):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW)
    failed = AnalysisResultV1(
        AnalysisResultV1.compute_result_id(request_id=req.request_id, decision_id=None),
        req.request_id, AnalysisKind.EVENT, "FAILED", None, None, NOW,
    )
    with pytest.raises(ValueError):
        store.settle_analysis_result_with_coverage(
            ProducerRole.ANALYSIS, request_id=req.request_id, result=failed,
            coverage_rows=(coverage(req),),
        )


@pytest.mark.parametrize(("field", "value"), (
    ("subject_id", "wrong-subject"),
    ("policy_digest", "d" * 64),
    ("market", "US"),
    ("symbol", "000660"),
    ("material_tick_id", 40),
    ("material_observed_at", NOW.replace(second=4)),
    ("analyzed_at", NOW.replace(second=4)),
    ("evidence_id", "wrong-evidence"),
))
def test_success_coverage_rejects_every_request_or_provenance_drift(
    tmp_path, field, value,
):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW)
    result = successful_result(req)
    row = replace(coverage(req), **{field: value})
    disposition, outbox = disposition_and_outbox(req)
    with pytest.raises(OpsKernelConflictError):
        store.settle_analysis_result_with_coverage(
            ProducerRole.ANALYSIS, request_id=req.request_id, result=result,
            coverage_rows=(row,), disposition=disposition,
            outbox_or_none=outbox,
        )
    assert store.read_analysis_request(req.request_id)["state"] == "ANALYZING"
    assert store.read_latest_analysis_coverage(
        AnalysisKind.EVENT, "KR", ("005930", "000660"),
    ) == ()


@pytest.mark.parametrize("mode", ("missing", "extra"))
def test_success_coverage_requires_exact_expected_symbol_set(tmp_path, mode):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW)
    rows = () if mode == "missing" else (
        coverage(req), replace(coverage(req), market="US", symbol="AAPL"),
    )
    disposition, outbox = disposition_and_outbox(req)
    with pytest.raises(OpsKernelConflictError, match="exactly match"):
        store.settle_analysis_result_with_coverage(
            ProducerRole.ANALYSIS, request_id=req.request_id,
            result=successful_result(req), coverage_rows=rows,
            disposition=disposition, outbox_or_none=outbox,
        )


def test_latest_coverage_query_is_bounded_market_symbol_and_kind_scoped(tmp_path):
    store = make_store(tmp_path)
    req = request()
    create_requests(store)
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.start_analysis_request(ProducerRole.ANALYSIS, req.request_id, now=NOW)
    disposition, outbox = disposition_and_outbox(req)
    store.settle_analysis_result_with_coverage(
        ProducerRole.ANALYSIS, request_id=req.request_id,
        result=successful_result(req), coverage_rows=(coverage(req),),
        disposition=disposition, outbox_or_none=outbox,
    )
    rows = store.read_latest_analysis_coverage(
        AnalysisKind.EVENT, "KR", ("005930", "000660"),
    )
    assert len(rows) == 1
    assert rows[0]["symbol"] == "005930"
    assert store.read_latest_analysis_coverage(
        AnalysisKind.EVENT, "US", ("005930",),
    ) == ()
