import ast
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import inspect
import re
import subprocess
import sqlite3
import sys
import textwrap
from types import SimpleNamespace
import types
from dataclasses import fields, replace
from datetime import datetime, timedelta, timezone

import pytest

from tokmo.records.ops_kernel import (
    OpsKernelStore, ROLE_METHODS, SCHEMA_VERSION,
)
from tokmo.values.analysis_contracts import AuditCompletionV1, MaterialKindV1
from tokmo.values.material_pack import JobKind
from tokmo.values.session_gate import Market
from tokmo.values.ops_kernel import (
    AlertChannel,
    AlertOutboxV1,
    AlertResolution,
    AlertStatus,
    CandidateAssignment,
    CandidateManifestV1,
    CandidateState,
    DeferredTerminalState,
    DeferredAttemptV1,
    DispositionAction,
    DispositionV1,
    FeedbackEventKind,
    FeedbackInboundV1,
    IncidentKeyV1,
    ManifestState,
    OpsKernelBudgetExceeded,
    OpsKernelCorruptError,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
    OpsKernelLockedError,
    OpsKernelRoleError,
    OpsKernelTransitionError,
    ProducerRole,
    ReplayResult,
    RequestEndpointV1,
)


NOW = datetime(2026, 7, 30, 1, 0, tzinfo=timezone.utc)
DIGEST = "a" * 64


def candidate(
    candidate_id="c1", *, state=CandidateState.PENDING,
    assignment=CandidateAssignment.FULL_REVIEW,
    kind="POSITION", subject_id="subject-1", evidence_refs=("e1",),
):
    return CandidateManifestV1(
        candidate_id, subject_id, kind, evidence_refs,
        assignment, state, NOW,
    )


def disposition(
    action=DispositionAction.SEND, *, decision_id="d1", candidate_id="c1"
):
    return DispositionV1(
        decision_id, 42, candidate_id, "subject-1", action, "material_change",
        ("e1",), {JobKind.HOLDINGS: 40, JobKind.QUOTES: 41, JobKind.TRADES: 39},
        "OK", "OK", "policy-v1", NOW,
    )


def outbox(*, channel=AlertChannel.SHADOW, alert_id="a1", decision_id="d1"):
    return AlertOutboxV1.for_disposition(
        alert_id=alert_id, decision_id=decision_id, tick_id=42,
        subject_id="subject-1", channel=channel, severity="HIGH",
        body_evidence=("e1",), created_at=NOW,
    )

def deferred_attempt(
    *, subject_id="deferred-subject", market=Market.KR,
    first_seen_tick=42, attempt_tick=42,
    candidate_tick=42, candidate_id="c1", evidence_refs=("e2",),
    material_ticks=None, next_review_at=NOW, reason_code="next_session_defer",
    assignment=CandidateAssignment.EXPLICITLY_DEFERRED, attempted_at=NOW,
):
    return DeferredAttemptV1(
        subject_id, market, first_seen_tick, attempt_tick, candidate_tick, candidate_id,
        evidence_refs,
        material_ticks if material_ticks is not None else {JobKind.TRADES: 37},
        next_review_at, reason_code, assignment, attempted_at,
    )


def make_store(tmp_path, **kwargs):
    return OpsKernelStore(
        tmp_path / "ops" / "ops_kernel.sqlite3",
        stable_phase_ids=frozenset({"slice-d-fixture-v1"}), **kwargs,
    )


def create_sealed(store):
    assert store.create_manifest(
        ProducerRole.ANALYSIS, 42, NOW,
        {JobKind.HOLDINGS: 40, JobKind.QUOTES: 41, JobKind.TRADES: 39},
        DIGEST,
        (candidate(),),
    ) is ReplayResult.CREATED
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)


def batch_row(ordinal=0, provider="GLM", candidate_ids=("c1",), digest=DIGEST):
    return {
        "ordinal": ordinal,
        "provider": provider,
        "candidate_ids": candidate_ids,
        "request_digest": digest,
        "planned_at": NOW,
    }


def incident_outbox(key, alert_id="incident-a1"):
    return AlertOutboxV1.for_incident(
        alert_id=alert_id, key=key, tick_id=42, subject_id=key.subject,
        severity="HIGH", body_evidence=("e1",), created_at=NOW,
    )


DEFERRED_CANDIDATE_COLUMNS = (
    "subject_id", "market", "evidence_refs_json", "material_ticks_json", "first_seen_tick",
    "last_attempt_tick", "next_review_at", "attempts", "terminal_state", "updated_at",
)
DEFERRED_CANDIDATE_ROW = (
    "matrix-subject", "KR", '["e2"]', '{"trades":37}', 42, 42,
    "2026-07-30T01:00:00.000000Z", 1, None,
    "2026-07-30T01:00:00.000000Z",
)
DEFERRED_ATTEMPT_COLUMNS = (
    "subject_id", "market", "attempt_tick", "candidate_tick", "candidate_id",
    "evidence_refs_json", "material_ticks_json", "next_review_at", "reason_code",
    "assignment", "attempted_at",
)
DEFERRED_ATTEMPT_ROW = (
    "matrix-subject", "KR", 42, 42, "c1", '["e2"]', '{"trades":37}',
    "2026-07-30T01:00:00.000000Z", "next_session_defer",
    "EXPLICITLY_DEFERRED", "2026-07-30T01:00:00.000000Z",
)


def _raw_insert(connection, table, columns, row):
    placeholders = ",".join("?" for _ in row)
    connection.execute(
        f"INSERT INTO {table}({','.join(columns)}) VALUES({placeholders})", row
    )


def _assert_raw_insert_and_update_rejected(
    connection, table, columns, valid_row, column, invalid_value,
):
    bad = list(valid_row)
    bad[columns.index(column)] = invalid_value
    connection.execute("SAVEPOINT adversarial_insert")
    with pytest.raises(sqlite3.IntegrityError):
        _raw_insert(connection, table, columns, tuple(bad))
    connection.execute("ROLLBACK TO adversarial_insert")
    connection.execute("RELEASE adversarial_insert")
    assert connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0

    _raw_insert(connection, table, columns, valid_row)
    connection.execute("SAVEPOINT adversarial_update")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE subject_id=?",
            (invalid_value, valid_row[0]),
        )
    connection.execute("ROLLBACK TO adversarial_update")
    connection.execute("RELEASE adversarial_update")
    assert connection.execute(
        f"SELECT {column} FROM {table} WHERE subject_id=?", (valid_row[0],)
    ).fetchone()[0] == valid_row[columns.index(column)]
    connection.execute(f"DELETE FROM {table}")


def test_exact_strict_wal_full_schema_and_private_permissions(tmp_path):
    store = make_store(tmp_path)
    path = store.path
    assert (os.stat(path.parent).st_mode & 0o777) == 0o700
    assert (os.stat(path).st_mode & 0o777) == 0o600
    connection = sqlite3.connect(path)
    try:
        assert connection.execute("PRAGMA user_version").fetchone() == (
            SCHEMA_VERSION,
        )
        assert connection.execute("PRAGMA journal_mode").fetchone() == ("wal",)
        assert connection.execute("PRAGMA synchronous").fetchone() == (2,)
        tables = {
            row[1] for row in connection.execute("PRAGMA table_list")
            if row[1] not in {"sqlite_schema", "sqlite_temp_schema"}
        }
        assert tables == {
            "manifest_runs", "candidate_manifest", "dispositions", "alert_outbox",
            "incidents", "deferred_candidates", "sample_rotation", "audit_coverage",
            "audit_backlog", "request_day_counters", "runtime_request_reservations",
            "development_request_reservations", "endpoint_cooldowns",
            "feedback_inbound", "analysis_batches", "subject_baselines",
            "quote_state_history", "deferred_attempts", "planned_requests",
            "calendar_request_counters", "planned_request_pages",
            "cadence_ticks", "cadence_wire_starts",
            "cadence_calendar_slots", "cadence_calendar_attempts",
            "analysis_requests", "analysis_coverage",
            "hypotheses", "hypothesis_sightings", "hypothesis_index",
            "hypothesis_lifecycle", "shadow_recall_records",
            "candidate_failures",
            "hypothesis_relations", "merge_review_queue", "weekly_review_runs",
            "weekly_review_pairs", "hypothesis_admission_decisions",
        }
        assert all(
            row[5] == 1 for row in connection.execute("PRAGMA table_list")
            if row[1] in tables
        )
        indexes = {
                row[1] for row in connection.execute(
                    "SELECT type,name FROM sqlite_schema "
                    "WHERE type='index' AND sql IS NOT NULL"
                )
        }
        assert indexes == {
            "alert_outbox_decision_open", "alert_outbox_dedupe_open",
            "deferred_attempts_candidate", "analysis_batches_open",
            "analysis_batches_started_run", "subject_baselines_scope",
            "quote_state_history_subject", "planned_requests_open",
            "cadence_wire_starts_open", "cadence_calendar_attempts_open",
            "analysis_coverage_latest",
            "hypotheses_open", "hypothesis_sightings_hypothesis",
            "hypothesis_index_hypothesis", "hypothesis_lifecycle_hypothesis",
            "candidate_failures_source",
            "hypothesis_relations_a", "hypothesis_relations_b",
            "merge_review_queue_open", "merge_review_queue_a",
            "weekly_review_pairs_week",
            "hypothesis_admission_decisions_hypothesis",
        }
        assert len(connection.execute(
            "PRAGMA table_info(manifest_runs)"
        ).fetchall()) == 9
        assert len(connection.execute(
            "PRAGMA table_info(deferred_candidates)"
        ).fetchall()) == 10
        assert len(connection.execute(
            "PRAGMA table_info(deferred_attempts)"
        ).fetchall()) == 11
    finally:
        connection.close()
    store.close()
    for suffix in ("-wal", "-shm"):
        sidecar = path.with_name(path.name + suffix)
        if sidecar.exists():
            assert (os.stat(sidecar).st_mode & 0o777) == 0o600


def test_manifest_atomic_replay_seal_fail_and_crash(tmp_path):
    store = make_store(tmp_path)
    args = (
        ProducerRole.ANALYSIS, 42, NOW,
        {JobKind.HOLDINGS: 40, JobKind.QUOTES: 41}, DIGEST, (candidate(),),
    )
    assert store.create_manifest(*args) is ReplayResult.CREATED
    assert store.create_manifest(*args) is ReplayResult.DUPLICATE
    with pytest.raises(OpsKernelConflictError):
        store.create_manifest(
            ProducerRole.ANALYSIS, 42, NOW,
            {JobKind.HOLDINGS: 40}, DIGEST, (candidate("different"),),
        )
    summary = store.read_manifest_summary(42)
    assert summary.state is ManifestState.OPEN
    assert summary.total_count == 1
    assert summary.counts == {"POSITION": 1}
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    assert store.read_manifest_summary(42).state is ManifestState.SEALED
    with pytest.raises(OpsKernelTransitionError):
        store.fail_manifest(ProducerRole.ANALYSIS, 42, "provider_failure")

    assert store.create_manifest(
        ProducerRole.ANALYSIS, 43, NOW, {}, DIGEST, (),
    ) is ReplayResult.CREATED
    store.fail_manifest(ProducerRole.ANALYSIS, 43, "provider_failure")
    assert store.read_manifest_summary(43).state is ManifestState.FAILED
    with pytest.raises(OpsKernelTransitionError):
        store.create_manifest(ProducerRole.ANALYSIS, 43, NOW, {}, DIGEST, ())

    with pytest.raises(RuntimeError, match="injected"):
        store.create_manifest(
            ProducerRole.ANALYSIS, 44, NOW, {}, DIGEST, (candidate(),),
            inject_crash_at="after_run",
        )
    assert store.read_manifest_summary(44) is None


def test_disposition_outbox_atomic_shadow_and_transitions(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    with pytest.raises(RuntimeError, match="injected"):
        store.record_disposition_with_outbox(
            ProducerRole.ANALYSIS, disposition(), outbox(),
            activation_enabled=False, inject_crash_at="after_disposition",
        )
    assert store.read_disposition("d1") is None
    assert store.read_outbox("a1") is None
    with pytest.raises(RuntimeError, match="injected"):
        store.record_disposition_with_outbox(
            ProducerRole.ANALYSIS, disposition(), outbox(),
            inject_crash_at="after_outbox",
        )
    assert store.read_disposition("d1") is None
    assert store.read_outbox("a1") is None

    assert store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(), outbox(), activation_enabled=False,
    ) is ReplayResult.CREATED
    row = store.read_outbox("a1")
    assert row.channel is AlertChannel.SHADOW
    assert row.status is None
    assert row.attempted_at is row.delivered_at is row.provider_message_id is None
    assert store.claim_pending_telegram(ProducerRole.HERMES, NOW) is None
    assert store.read_manifest_candidates(42)[0].state is CandidateState.DISPOSITIONED

    with pytest.raises(OpsKernelTransitionError):
        store.mark_sent(ProducerRole.HERMES, "a1", NOW, "provider-ref")


def test_telegram_unknown_requeue_and_failed_acknowledgement(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(),
        outbox(channel=AlertChannel.TELEGRAM), activation_enabled=True,
    )
    claimed = store.claim_pending_telegram(ProducerRole.HERMES, NOW)
    assert claimed.status is AlertStatus.ATTEMPTING
    assert store.promote_stale_attempting(
        ProducerRole.HERMES, NOW + timedelta(seconds=299)
    ) == 0
    assert store.promote_stale_attempting(
        ProducerRole.HERMES, NOW + timedelta(seconds=300)
    ) == 1
    assert store.claim_pending_telegram(ProducerRole.HERMES, NOW) is None
    with pytest.raises(RuntimeError):
        store.requeue_unknown(
            ProducerRole.HERMES, "a1", "a2", NOW + timedelta(seconds=301),
            inject_crash_at="after_resolve",
        )
    assert store.read_outbox("a1").resolution is AlertResolution.UNRESOLVED
    assert store.read_outbox("a2") is None
    store.requeue_unknown(
        ProducerRole.HERMES, "a1", "a2", NOW + timedelta(seconds=301)
    )
    assert store.read_outbox("a1").status is AlertStatus.UNKNOWN
    assert store.read_outbox("a1").resolution is AlertResolution.REQUEUED
    assert store.read_outbox("a2").status is AlertStatus.PENDING

    claimed = store.claim_pending_telegram(ProducerRole.HERMES, NOW)
    store.mark_failed(ProducerRole.HERMES, claimed.alert_id, NOW, "provider_failed")
    store.acknowledge_failed(ProducerRole.HERMES, claimed.alert_id, NOW, "operator_checked")
    failed = store.read_outbox(claimed.alert_id)
    assert failed.status is AlertStatus.FAILED
    assert failed.resolution is AlertResolution.FAILED_ACKNOWLEDGED


def test_incident_deferred_audit_rotation_and_pack_ticks(tmp_path):
    store = make_store(tmp_path)
    key = IncidentKeyV1("market_unclassified", "scope-token")
    assert store.read_incident(key) is None
    first = store.observe_incident(ProducerRole.COLLECTOR, key, "impact-a", NOW)
    assert first.notification_required and first.incident.count == 1
    repeated = store.observe_incident(ProducerRole.COLLECTOR, key, "impact-a", NOW)
    assert not repeated.notification_required and repeated.incident.count == 2
    changed = store.observe_incident(ProducerRole.COLLECTOR, key, "impact-b", NOW)
    assert changed.notification_required
    assert store.resolve_incident(ProducerRole.HERMES, key, NOW).notification_required
    assert store.observe_incident(
        ProducerRole.COLLECTOR, key, "impact-b", NOW
    ).notification_required
    incident_alert = AlertOutboxV1.for_incident(
        alert_id="ia1", key=key, tick_id=42, subject_id="masked",
        severity="HIGH", body_evidence=("incident",), created_at=NOW,
    )
    store.record_incident_outbox(ProducerRole.HERMES, incident_alert)
    assert store.read_outbox("ia1").decision_id is None

    create_sealed(store)
    deferred = deferred_attempt()
    assert store.upsert_deferred(ProducerRole.ANALYSIS, deferred) is ReplayResult.CREATED
    assert store.read_due_deferred(Market.KR, NOW)[0].attempts == 1
    assert store.upsert_deferred(
        ProducerRole.ANALYSIS, deferred
    ) is ReplayResult.DUPLICATE
    assert store.read_due_deferred(Market.KR, NOW)[0].attempts == 1
    store.upsert_deferred(
        ProducerRole.ANALYSIS,
        replace(
            deferred, attempt_tick=43, candidate_tick=42,
            evidence_refs=("e3",), attempted_at=NOW + timedelta(minutes=5),
        ),
    )
    assert store.read_due_deferred(Market.KR, NOW)[0].attempts == 2
    store.upsert_audit_backlog(
        ProducerRole.ANALYSIS, "LOW", "deferred-subject",
        "2026-07-30", "2026-07-30", "PENDING_AUDIT",
    )
    store.update_sample_rotation(ProducerRole.ANALYSIS, "LOW", "deferred-subject", NOW)
    with pytest.raises(OpsKernelRoleError):
        store.write_audit_coverage(
            ProducerRole.ANALYSIS, "2026-07-30", "LOW", 3, 1, 2, 4, False,
        )
    assert store.read_audit_backlog("LOW")[0].subject_id == "deferred-subject"
    assert store.referenced_pack_ticks()[JobKind.TRADES] == frozenset({37, 39})
    store.terminalize_deferred(
        ProducerRole.ANALYSIS, "deferred-subject", DeferredTerminalState.TERMINAL, NOW
    )
    with pytest.raises(OpsKernelTransitionError):
        store.upsert_deferred(ProducerRole.ANALYSIS, deferred)


@pytest.mark.parametrize("raised", [KeyboardInterrupt(), SystemExit(23)])
@pytest.mark.parametrize("wrapper_name", ["_transaction", "_read"])
def test_wrappers_cleanup_and_propagate_base_exceptions_unchanged(
    tmp_path, monkeypatch, raised, wrapper_name,
):
    store = make_store(tmp_path)

    class InjectedConnection:
        def __init__(self):
            self.in_transaction = False
            self.statements = []
            self.closed = False

        def execute(self, statement):
            self.statements.append(statement)
            if statement == "BEGIN IMMEDIATE":
                self.in_transaction = True
            elif statement == "ROLLBACK":
                self.in_transaction = False

        def close(self):
            self.closed = True

    connection = InjectedConnection()
    monkeypatch.setattr(store, "_connect", lambda: connection)
    with pytest.raises(type(raised)) as caught:
        with getattr(store, wrapper_name)():
            raise raised
    assert caught.value is raised
    assert connection.closed
    if wrapper_name == "_transaction":
        assert "ROLLBACK" in connection.statements


@pytest.mark.parametrize("wrapper_name", ["_transaction", "_read"])
def test_wrappers_map_ordinary_exceptions_after_cleanup(
    tmp_path, monkeypatch, wrapper_name,
):
    store = make_store(tmp_path)

    class InjectedConnection:
        in_transaction = False

        def __init__(self):
            self.statements = []
            self.closed = False

        def execute(self, statement):
            self.statements.append(statement)
            if statement == "BEGIN IMMEDIATE":
                self.in_transaction = True
            elif statement == "ROLLBACK":
                self.in_transaction = False

        def close(self):
            self.closed = True

    connection = InjectedConnection()
    monkeypatch.setattr(store, "_connect", lambda: connection)
    with pytest.raises(OpsKernelCorruptError):
        with getattr(store, wrapper_name)():
            raise ValueError("ordinary")
    assert connection.closed
    if wrapper_name == "_transaction":
        assert "ROLLBACK" in connection.statements


def test_partial_unique_dedupe_violation_is_conflict_and_fully_rolls_back(tmp_path):
    store = make_store(tmp_path)
    assert store.create_manifest(
        ProducerRole.ANALYSIS, 42, NOW,
        {JobKind.HOLDINGS: 40, JobKind.QUOTES: 41, JobKind.TRADES: 39},
        DIGEST,
        (candidate("c1"), candidate("c2")),
    ) is ReplayResult.CREATED
    store.seal_manifest(ProducerRole.ANALYSIS, 42, NOW)
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS,
        disposition(decision_id="d1", candidate_id="c1"),
        outbox(
            channel=AlertChannel.TELEGRAM, alert_id="a1", decision_id="d1"
        ),
        activation_enabled=True,
    )

    with pytest.raises(OpsKernelConflictError):
        store.record_disposition_with_outbox(
            ProducerRole.ANALYSIS,
            disposition(decision_id="d2", candidate_id="c2"),
            outbox(
                channel=AlertChannel.TELEGRAM, alert_id="a2", decision_id="d2"
            ),
            activation_enabled=True,
        )

    assert store.read_disposition("d2") is None
    assert store.read_outbox("a2") is None
    states = {
        row.candidate_id: row.state for row in store.read_manifest_candidates(42)
    }
    assert states == {
        "c1": CandidateState.DISPOSITIONED,
        "c2": CandidateState.PENDING,
    }


def test_runtime_and_development_reservation_cooldown_and_endpoint_validation(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1("toss", "get", "API.TOSS.IM", "/api//v1/orders/{id}")
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, NOW)
    counts = store.read_reservation_counts(None, endpoint, NOW)
    assert counts.runtime == 1 and counts.development is None
    store.reserve_request(
        ProducerRole.ANALYSIS, "slice-d-fixture-v1", endpoint, 1, NOW,
    )
    with pytest.raises(OpsKernelBudgetExceeded):
        store.reserve_request(
            ProducerRole.ANALYSIS, "slice-d-fixture-v1", endpoint, 1, NOW,
        )
    assert store.read_reservation_counts(
        "slice-d-fixture-v1", endpoint, NOW
    ).runtime == 2
    store.record_cooldown(
        ProducerRole.COLLECTOR, endpoint, NOW + timedelta(minutes=5),
        "rate_limited", NOW,
    )
    store.record_cooldown(
        ProducerRole.COLLECTOR, endpoint, NOW + timedelta(minutes=1),
        "rate_limited", NOW,
    )
    with pytest.raises(OpsKernelCooldownActive):
        store.reserve_request(
            ProducerRole.COLLECTOR, None, endpoint, None,
            NOW + timedelta(minutes=4),
        )
    for bad in (
        ("toss", "GET", "user@example.com", "/x"),
        ("toss", "GET", "example.com", "/x?q=1"),
        ("toss", "GET", "example.com", "/../x"),
        ("toss", "GET", "example.com", "relative"),
    ):
        with pytest.raises(ValueError):
            RequestEndpointV1(*bad)


def test_feedback_append_only_dedupe_conflict_and_private_read(tmp_path):
    store = make_store(tmp_path)
    create = FeedbackInboundV1.build(
        "f1", "telegram", "u1", "m1", "sender", None, NOW,
        "raw private text", None, "webhook", FeedbackEventKind.CREATE,
    )
    assert store.append_feedback(ProducerRole.FEEDBACK, create) is ReplayResult.CREATED
    assert store.append_feedback(ProducerRole.FEEDBACK, create) is ReplayResult.DUPLICATE
    with pytest.raises(OpsKernelConflictError):
        store.append_feedback(
            ProducerRole.FEEDBACK,
            FeedbackInboundV1.build(
                "f2", "telegram", "u1", "m1", "sender", None, NOW,
                "different", None, "webhook", FeedbackEventKind.CREATE,
            ),
        )
    edit = FeedbackInboundV1.build(
        "f3", "telegram", "u2", "m1", "sender", None, NOW,
        "edited", None, "webhook", FeedbackEventKind.EDIT,
    )
    store.append_feedback(ProducerRole.FEEDBACK, edit)
    assert store.read_feedback_private("f1").raw_text == "raw private text"


def test_role_matrix_rejects_cross_surface(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1("toss", "GET", "example.com", "/v1/x")
    with pytest.raises(OpsKernelRoleError):
        store.reserve_request(ProducerRole.FEEDBACK, None, endpoint, None, NOW)


def test_path_hardening_schema_tamper_and_lock_are_typed(tmp_path):
    target = tmp_path / "target"
    target.write_bytes(b"x")
    symlink = tmp_path / "unsafe.sqlite3"
    symlink.symlink_to(target)
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(symlink)

    fifo = tmp_path / "fifo.sqlite3"
    os.mkfifo(fifo, 0o600)
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(fifo)

    store = make_store(tmp_path / "hardlink")
    link = tmp_path / "hardlink-copy.sqlite3"
    os.link(store.path, link)
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(store.path)

    store = make_store(tmp_path / "schema")
    connection = sqlite3.connect(store.path)
    connection.execute("DROP INDEX alert_outbox_decision_open")
    connection.commit()
    connection.close()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(store.path)

    store = make_store(tmp_path / "locked")
    blocker = sqlite3.connect(store.path, isolation_level=None)
    blocker.execute("BEGIN IMMEDIATE")
    endpoint = RequestEndpointV1("toss", "GET", "example.com", "/v1/x")
    try:
        with pytest.raises(OpsKernelLockedError):
            store.reserve_request(ProducerRole.COLLECTOR, None, endpoint, None, NOW)
    finally:
        blocker.execute("ROLLBACK")
        blocker.close()
    assert store.read_reservation_counts(None, endpoint, NOW).runtime == 0


@pytest.mark.parametrize("tamper_sql", [
    # m7: the cadence tables inherit the kernel's exact reference-schema
    # validation — dropped tables/indexes, weakened CHECKs, and altered
    # columns all fail closed, never table-name-only validation.
    "DROP INDEX cadence_wire_starts_open",
    "DROP TABLE cadence_calendar_slots",
    "ALTER TABLE cadence_ticks ADD COLUMN leaked TEXT",
    "ALTER TABLE cadence_wire_starts RENAME TO cadence_wire_starts_old",
])
def test_cadence_schema_tamper_fails_closed(tmp_path, tamper_sql):
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript(tamper_sql)
    connection.close()
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(store.path)


def test_cadence_weakened_check_ddl_fails_closed(tmp_path):
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.executescript(
        "DROP TABLE cadence_calendar_slots;"
        "CREATE TABLE cadence_calendar_slots("
        "market TEXT NOT NULL, market_local_date TEXT NOT NULL,"
        "eligible_slot_utc TEXT NOT NULL, state TEXT NOT NULL,"
        "attempt_ordinal INTEGER, created_at TEXT NOT NULL,"
        "PRIMARY KEY(market,market_local_date,eligible_slot_utc)) STRICT;"
    )
    connection.close()
    # Same table name and column names, but the closed-state/ordinal CHECKs
    # are gone: the exact-DDL comparison still refuses to open.
    with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
        OpsKernelStore(store.path)


@pytest.mark.parametrize("extra_sql", [
    "CREATE VIEW leaked_feedback AS SELECT raw_text FROM feedback_inbound",
    "CREATE TRIGGER mutate_feedback AFTER INSERT ON feedback_inbound BEGIN "
    "UPDATE feedback_inbound SET raw_text='x' WHERE feedback_id=NEW.feedback_id; END",
])
def test_exact_schema_rejects_every_extra_object(tmp_path, extra_sql):
    store = make_store(tmp_path)
    connection = sqlite3.connect(store.path)
    connection.execute(extra_sql)
    connection.commit()
    connection.close()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(store.path)


def test_preexisting_empty_or_truncated_database_is_corrupt(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    path.parent.mkdir(mode=0o700)
    for raw in (b"", b"not-a-sqlite-database"):
        path.write_bytes(raw)
        path.chmod(0o600)
        with pytest.raises(OpsKernelCorruptError):
            OpsKernelStore(path)
        assert path.read_bytes() == raw


def test_two_process_fresh_bootstrap_is_atomic_and_leak_free(tmp_path):
    path = tmp_path / "ops" / "ops_kernel.sqlite3"
    code = (
        "from pathlib import Path\n"
        "from tokmo.records.ops_kernel import OpsKernelStore\n"
        f"OpsKernelStore(Path({str(path)!r}))\n"
    )
    processes = [
        subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, text=True,
                         env={**os.environ, "PYTHONPATH": "src"})
        for _ in range(2)
    ]
    results = [process.communicate(timeout=15) + (process.returncode,) for process in processes]
    assert all(code == 0 for _, _, code in results), results
    OpsKernelStore(path)
    assert not list(path.parent.glob(".*.bootstrap-*"))


def test_fixed_width_timestamp_stale_boundary_and_failure_reason(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(),
        outbox(channel=AlertChannel.TELEGRAM), activation_enabled=True,
    )
    claimed = store.claim_pending_telegram(
        ProducerRole.HERMES, NOW + timedelta(microseconds=1)
    )
    assert store.promote_stale_attempting(
        ProducerRole.HERMES, NOW + timedelta(seconds=300)
    ) == 0
    assert store.promote_stale_attempting(
        ProducerRole.HERMES, NOW + timedelta(seconds=300, microseconds=1)
    ) == 1
    assert len(claimed.attempted_at.isoformat()) > 20


def test_disposition_replay_includes_outbox_bytes_and_constraint_is_conflict(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    original = outbox()
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(), original
    )
    drift = AlertOutboxV1.for_disposition(
        alert_id="a-drift", decision_id="d1", tick_id=42,
        subject_id="subject-1", channel=AlertChannel.SHADOW, severity="HIGH",
        body_evidence=("e1",), created_at=NOW,
    )
    with pytest.raises(OpsKernelConflictError):
        store.record_disposition_with_outbox(
            ProducerRole.ANALYSIS, disposition(), drift
        )
    assert store.read_outbox("a-drift") is None


def test_candidate_initial_state_and_closed_private_reasons(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelTransitionError):
        store.create_manifest(
            ProducerRole.ANALYSIS, 1, NOW, {}, DIGEST,
            (candidate(state=CandidateState.TERMINAL),)
        )
    with pytest.raises(ValueError):
        store.terminalize_deferred(
            ProducerRole.ANALYSIS, "missing", "raw private reason", NOW
        )


def test_full_role_method_matrix_is_closed(tmp_path):
    store = make_store(tmp_path)
    all_methods = set().union(*ROLE_METHODS.values())
    for role in ProducerRole:
        for method in all_methods:
            if method in ROLE_METHODS[role]:
                store._role(role, method)
            else:
                with pytest.raises(OpsKernelRoleError):
                    store._role(role, method)


def test_sent_failed_and_feedback_linkage_contracts(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(),
        outbox(channel=AlertChannel.TELEGRAM), activation_enabled=True,
    )
    store.claim_pending_telegram(ProducerRole.HERMES, NOW)
    store.mark_failed(ProducerRole.HERMES, "a1", NOW, "provider_failed")
    assert store.read_outbox("a1").resolution_reason == "provider_failed"
    with pytest.raises(OpsKernelTransitionError):
        store.mark_sent(ProducerRole.HERMES, "a1", NOW, "provider-ref")
    with pytest.raises(ValueError):
        store.acknowledge_failed(
            ProducerRole.HERMES, "a1", NOW, "private arbitrary explanation"
        )
    orphan = FeedbackInboundV1.build(
        "delete-1", "telegram", "delete-update", "missing-message", "sender",
        None, NOW, "", None, "webhook", FeedbackEventKind.DELETE,
    )
    with pytest.raises(OpsKernelTransitionError):
        store.append_feedback(ProducerRole.FEEDBACK, orphan)


def test_telegram_activation_sent_and_linked_delete(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    with pytest.raises(OpsKernelTransitionError):
        store.record_disposition_with_outbox(
            ProducerRole.ANALYSIS, disposition(),
            outbox(channel=AlertChannel.TELEGRAM), activation_enabled=False,
        )
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(),
        outbox(channel=AlertChannel.TELEGRAM), activation_enabled=True,
    )
    store.claim_pending_telegram(ProducerRole.HERMES, NOW)
    store.mark_sent(ProducerRole.HERMES, "a1", NOW, "provider-ref")
    sent = store.read_outbox("a1")
    assert sent.status is AlertStatus.SENT
    assert sent.resolution is AlertResolution.CONFIRMED_SENT

    created = FeedbackInboundV1.build(
        "create-1", "telegram", "create-update", "message-1", "sender",
        None, NOW, "private", None, "webhook", FeedbackEventKind.CREATE,
    )
    deleted = FeedbackInboundV1.build(
        "delete-1", "telegram", "delete-update", "message-1", "sender",
        None, NOW, "", None, "webhook", FeedbackEventKind.DELETE,
    )
    store.append_feedback(ProducerRole.FEEDBACK, created)
    store.append_feedback(ProducerRole.FEEDBACK, deleted)
    assert store.read_feedback_private("create-1").raw_text == "private"
    assert store.read_feedback_private("delete-1").event_kind is FeedbackEventKind.DELETE


def test_operation_scoped_connections_do_not_grow_fds(tmp_path):
    fd_dir = "/dev/fd"
    if not os.path.isdir(fd_dir):
        pytest.skip("fd inventory unavailable")
    store = make_store(tmp_path)
    before = len(os.listdir(fd_dir))
    for _ in range(50):
        assert store.read_manifest_summary(999) is None
    after = len(os.listdir(fd_dir))
    assert after <= before + 1


def test_reservation_crash_rolls_back_both_and_committed_start_is_not_refunded(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1("toss", "GET", "example.com", "/v1/x")
    with pytest.raises(RuntimeError, match="injected"):
        store.reserve_request(
            ProducerRole.ANALYSIS, "slice-d-fixture-v1", endpoint, 3, NOW,
            inject_crash_at="after_runtime",
        )
    counts = store.read_reservation_counts("slice-d-fixture-v1", endpoint, NOW)
    assert counts == type(counts)(0, 0)
    store.reserve_request(
        ProducerRole.ANALYSIS, "slice-d-fixture-v1", endpoint, 3, NOW,
    )
    reopened = make_store(tmp_path)
    assert reopened.read_reservation_counts(
        "slice-d-fixture-v1", endpoint, NOW
    ) == type(counts)(1, 1)


def test_d_mod_1_day_counter_is_monotonic_telemetry_and_endpoint_audit_aggregate(tmp_path):
    store = make_store(tmp_path)
    endpoint_a = RequestEndpointV1("toss", "GET", "example.com", "/v1/a")
    endpoint_b = RequestEndpointV1("toss", "HEAD", "example.com", "/v1/b")
    assert tuple(inspect.signature(store.reserve_request).parameters) == (
        "role", "stable_phase_id", "endpoint", "development_limit", "now",
        "inject_crash_at",
    )
    assert tuple(inspect.signature(store.read_reservation_counts).parameters) == (
        "stable_phase_id", "endpoint", "now",
    )
    local_next_day = datetime(2026, 7, 31, 8, 30, tzinfo=timezone(timedelta(hours=9)))
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint_a, None, local_next_day)
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint_b, None, local_next_day)
    assert store.read_reservation_counts(None, endpoint_a, local_next_day).runtime == 2
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT utc_day,provider_started_count FROM request_day_counters"
        ).fetchall() == [("2026-07-30", 2)]
        assert connection.execute(
            "SELECT normalized_path_template,started_count "
            "FROM runtime_request_reservations ORDER BY normalized_path_template"
        ).fetchall() == [("/v1/a", 1), ("/v1/b", 1)]
        connection.execute(
            "UPDATE request_day_counters SET provider_started_count=3743 "
            "WHERE utc_day='2026-07-30'"
        )
        connection.commit()
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint_a, None, local_next_day)
    assert store.read_reservation_counts(None, endpoint_b, local_next_day).runtime == 3744
    # G-10: crossing the historical 3,744 total is admitted; the UTC-day
    # counter is monotonic telemetry, not an admission ceiling.
    store.reserve_request(ProducerRole.COLLECTOR, None, endpoint_b, None, local_next_day)
    assert store.read_reservation_counts(None, endpoint_b, local_next_day).runtime == 3745
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT started_count FROM runtime_request_reservations "
            "WHERE normalized_path_template='/v1/b'"
        ).fetchone() == (2,)


def test_d_mod_1_cooldown_precedes_every_count_mutation(tmp_path):
    store = make_store(tmp_path)
    endpoint = RequestEndpointV1("toss", "GET", "example.com", "/v1/x")
    store.record_cooldown(
        ProducerRole.COLLECTOR, endpoint, NOW + timedelta(minutes=5),
        "rate_limited", NOW,
    )
    with pytest.raises(OpsKernelCooldownActive):
        store.reserve_request(
            ProducerRole.ANALYSIS, "slice-d-fixture-v1", endpoint, 3, NOW,
        )
    assert store.read_reservation_counts(
        "slice-d-fixture-v1", endpoint, NOW
    ) == type(store.read_reservation_counts(None, endpoint, NOW))(0, 0)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("SELECT count(*) FROM request_day_counters").fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM runtime_request_reservations"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT count(*) FROM development_request_reservations"
        ).fetchone() == (0,)


def test_referenced_ticks_manifest_outbox_and_terminal_release(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    assert store.referenced_pack_ticks() == {
        JobKind.HOLDINGS: frozenset({40}),
        JobKind.QUOTES: frozenset({41}),
        JobKind.TRADES: frozenset({39}),
    }
    store.record_disposition_with_outbox(
        ProducerRole.ANALYSIS, disposition(),
        outbox(channel=AlertChannel.TELEGRAM), activation_enabled=True,
    )
    assert store.referenced_pack_ticks()[JobKind.QUOTES] == frozenset({41})
    claimed = store.claim_pending_telegram(ProducerRole.HERMES, NOW)
    store.mark_failed(ProducerRole.HERMES, claimed.alert_id, NOW, "provider_failed")
    store.acknowledge_failed(ProducerRole.HERMES, claimed.alert_id, NOW, "checked")
    assert store.referenced_pack_ticks() == {
        JobKind.HOLDINGS: frozenset(),
        JobKind.QUOTES: frozenset(),
        JobKind.TRADES: frozenset(),
    }
    with pytest.raises(OpsKernelRoleError):
        store.append_feedback(
            ProducerRole.ANALYSIS,
            FeedbackInboundV1.build(
                "f1", "telegram", "u1", "m1", "sender", None, NOW,
                "raw", None, "webhook", FeedbackEventKind.CREATE,
            ),
        )


def test_d_mod_e1_digest_replay_conflict_empty_and_validation(tmp_path):
    store = make_store(tmp_path)
    assert store.create_manifest(
        ProducerRole.ANALYSIS, 1, NOW, {}, DIGEST, ()
    ) is ReplayResult.CREATED
    assert store.create_manifest(
        ProducerRole.ANALYSIS, 1, NOW, {}, DIGEST, ()
    ) is ReplayResult.DUPLICATE
    with pytest.raises(OpsKernelConflictError):
        store.create_manifest(ProducerRole.ANALYSIS, 1, NOW, {}, "b" * 64, ())
    with pytest.raises(ValueError):
        store.create_manifest(ProducerRole.ANALYSIS, 2, NOW, {}, "short", ())
    assert store.read_manifest_summary(1).material_binding_digest == DIGEST


def test_d_mod_e3_exact_replay_drift_skipped_regression_and_terminal(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    first = deferred_attempt()
    assert store.upsert_deferred(ProducerRole.ANALYSIS, first) is ReplayResult.CREATED
    before = store.read_due_deferred(Market.KR, NOW)[0]
    assert before.updated_at == first.attempted_at
    assert store.upsert_deferred(
        ProducerRole.ANALYSIS,
        replace(first, attempted_at=NOW + timedelta(seconds=1)),
    ) is ReplayResult.DUPLICATE
    assert store.read_due_deferred(Market.KR, NOW)[0] == before
    for field_name, value in (
        ("evidence_refs", ("other",)),
        ("material_ticks", {JobKind.TRADES: 38}),
        ("next_review_at", NOW + timedelta(days=1)),
        ("reason_code", "carryover_defer"),
        ("assignment", CandidateAssignment.CRITICAL),
        ("candidate_tick", 41),
        ("candidate_id", "other"),
    ):
        with pytest.raises(OpsKernelConflictError):
            store.upsert_deferred(
                ProducerRole.ANALYSIS, replace(first, **{field_name: value})
            )
    skipped = replace(
        first, attempt_tick=49, evidence_refs=("e49",),
        attempted_at=NOW + timedelta(minutes=35),
    )
    assert store.upsert_deferred(
        ProducerRole.ANALYSIS, skipped
    ) is ReplayResult.CREATED
    with pytest.raises(OpsKernelConflictError):
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            replace(first, attempt_tick=48, attempted_at=NOW + timedelta(minutes=30)),
        )
    store.terminalize_deferred(
        ProducerRole.ANALYSIS, first.subject_id,
        DeferredTerminalState.TERMINAL, NOW + timedelta(hours=1),
    )
    history = store.read_deferred_attempts(first.subject_id)
    with pytest.raises(OpsKernelTransitionError):
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            replace(skipped, attempt_tick=50, attempted_at=NOW + timedelta(hours=2)),
        )
    assert store.read_deferred_attempts(first.subject_id) == history


def test_d_mod_e3_first_shape_and_torn_history_fail_typed(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    with pytest.raises(OpsKernelTransitionError):
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            replace(deferred_attempt(), first_seen_tick=41),
        )
    first = deferred_attempt()
    store.upsert_deferred(ProducerRole.ANALYSIS, first)
    connection = sqlite3.connect(store.path)
    connection.execute(
        "DELETE FROM deferred_attempts WHERE subject_id=? AND attempt_tick=?",
        (first.subject_id, first.attempt_tick),
    )
    connection.commit()
    connection.close()
    with pytest.raises(OpsKernelCorruptError):
        store.upsert_deferred(ProducerRole.ANALYSIS, first)


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        ("subject_id", "bad subject"), ("subject_id", "-leading"),
        ("subject_id", ""), ("subject_id", "a" * 129),
        ("first_seen_tick", -1), ("first_seen_tick", 2147483648),
        ("last_attempt_tick", -1), ("last_attempt_tick", 2147483648),
        ("last_attempt_tick", 41),
        ("attempts", 0), ("attempts", -1), ("attempts", 8641),
        ("terminal_state", "BOGUS"),
        ("evidence_refs_json", "{}"), ("evidence_refs_json", '["a b"]'),
        ("evidence_refs_json", '["a\tb"]'), ("evidence_refs_json", '["a\nb"]'),
        ("evidence_refs_json", '["a\rb"]'),
        ("evidence_refs_json", "[" + "a" * 8191 + "]"),
        ("material_ticks_json", "[]"), ("material_ticks_json", '{"a": 1}'),
        ("material_ticks_json", '{"a\tb":1}'),
        ("material_ticks_json", '{"a\nb":1}'),
        ("material_ticks_json", '{"a\rb":1}'),
        ("material_ticks_json", "{" + "a" * 255 + "}"),
        *[
            (column, value)
            for column in ("next_review_at", "updated_at")
            for value in (
                "2026-07-30T01:00:00Z",
                "2026-07-30T01:00:00.000000+00:00",
                "2026-07-30t01:00:00.000000Z",
                "2026-07-30T01:00:00.00000Z",
                "2026-07-30T01:00:00.0000000Z",
            )
        ],
    ],
)
def test_d_mod_e3_11a_candidates_raw_sql_insert_update_matrix(
    tmp_path, column, invalid_value,
):
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        _assert_raw_insert_and_update_rejected(
            connection, "deferred_candidates", DEFERRED_CANDIDATE_COLUMNS,
            DEFERRED_CANDIDATE_ROW, column, invalid_value,
        )


@pytest.mark.parametrize(
    ("column", "invalid_value"),
    [
        *[
            (column, value)
            for column in ("subject_id", "candidate_id")
            for value in ("bad token", "-leading", "", "a" * 129)
        ],
        ("attempt_tick", -1), ("attempt_tick", 2147483648),
        ("candidate_tick", -1), ("candidate_tick", 2147483648),
        ("candidate_tick", 43),
        ("evidence_refs_json", "{}"), ("evidence_refs_json", '["a b"]'),
        ("evidence_refs_json", '["a\tb"]'), ("evidence_refs_json", '["a\nb"]'),
        ("evidence_refs_json", '["a\rb"]'),
        ("evidence_refs_json", "[" + "a" * 8191 + "]"),
        ("material_ticks_json", "[]"), ("material_ticks_json", '{"a": 1}'),
        ("material_ticks_json", '{"a\tb":1}'),
        ("material_ticks_json", '{"a\nb":1}'),
        ("material_ticks_json", '{"a\rb":1}'),
        ("material_ticks_json", "{" + "a" * 255 + "}"),
        *[
            (column, value)
            for column in ("next_review_at", "attempted_at")
            for value in (
                "2026-07-30T01:00:00Z",
                "2026-07-30T01:00:00.000000+00:00",
                "2026-07-30t01:00:00.000000Z",
                "2026-07-30T01:00:00.00000Z",
                "2026-07-30T01:00:00.0000000Z",
            )
        ],
        ("reason_code", "UPPER_defer"), ("reason_code", "notdefer"),
        ("reason_code", "defer"), ("reason_code", "a" * 59 + "_defer"),
        ("assignment", "UNKNOWN"), ("assignment", "critical"),
        ("candidate_id", "missing"),
    ],
)
def test_d_mod_e3_11b_attempts_raw_sql_insert_update_matrix(
    tmp_path, column, invalid_value,
):
    store = make_store(tmp_path)
    create_sealed(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _assert_raw_insert_and_update_rejected(
            connection, "deferred_attempts", DEFERRED_ATTEMPT_COLUMNS,
            DEFERRED_ATTEMPT_ROW, column, invalid_value,
        )


@pytest.mark.parametrize(
    ("table", "columns", "valid_row", "column", "boundary_value", "byte_length"),
    [
        (
            "deferred_candidates", DEFERRED_CANDIDATE_COLUMNS,
            DEFERRED_CANDIDATE_ROW, "evidence_refs_json",
            "[" + "a" * 8190 + "]", 8192,
        ),
        (
            "deferred_candidates", DEFERRED_CANDIDATE_COLUMNS,
            DEFERRED_CANDIDATE_ROW, "material_ticks_json",
            "{" + "a" * 254 + "}", 256,
        ),
        (
            "deferred_attempts", DEFERRED_ATTEMPT_COLUMNS,
            DEFERRED_ATTEMPT_ROW, "evidence_refs_json",
            "[" + "a" * 8190 + "]", 8192,
        ),
        (
            "deferred_attempts", DEFERRED_ATTEMPT_COLUMNS,
            DEFERRED_ATTEMPT_ROW, "material_ticks_json",
            "{" + "a" * 254 + "}", 256,
        ),
    ],
)
def test_d_mod_e3_11a_11b_sql_byte_boundaries_are_inclusive(
    tmp_path, table, columns, valid_row, column, boundary_value, byte_length,
):
    store = make_store(tmp_path)
    create_sealed(store)
    boundary_row = list(valid_row)
    boundary_row[columns.index(column)] = boundary_value
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _raw_insert(connection, table, columns, tuple(boundary_row))
        assert connection.execute(
            f"SELECT length(CAST({column} AS BLOB)) FROM {table}"
        ).fetchone() == (byte_length,)
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE subject_id=?",
            (valid_row[columns.index(column)], valid_row[0]),
        )
        connection.execute(
            f"UPDATE {table} SET {column}=? WHERE subject_id=?",
            (boundary_value, valid_row[0]),
        )
        assert connection.execute(
            f"SELECT length(CAST({column} AS BLOB)) FROM {table}"
        ).fetchone() == (byte_length,)


@pytest.mark.parametrize(
    ("table", "column", "invalid_value"),
    [
        ("deferred_candidates", "material_ticks_json",
         '{"quotes":41,"holdings":40}'),
        ("deferred_attempts", "material_ticks_json",
         '{"quotes":41,"holdings":40}'),
        *[
            (table, column, "2026-13-45T99:99:99.123456Z")
            for table, columns in (
                ("deferred_candidates", ("next_review_at", "updated_at")),
                ("deferred_attempts", ("next_review_at", "attempted_at")),
            )
            for column in columns
        ],
        ("deferred_attempts", "reason_code", "weird_defer"),
    ],
)
def test_d_mod_e3_11c_sql_accepts_python_boundary_rejects(
    tmp_path, table, column, invalid_value,
):
    store = make_store(tmp_path)
    create_sealed(store)
    candidate_row = list(DEFERRED_CANDIDATE_ROW)
    attempt_row = list(DEFERRED_ATTEMPT_ROW)
    target_columns = (
        DEFERRED_CANDIDATE_COLUMNS
        if table == "deferred_candidates" else DEFERRED_ATTEMPT_COLUMNS
    )
    target_row = candidate_row if table == "deferred_candidates" else attempt_row
    target_row[target_columns.index(column)] = invalid_value
    with sqlite3.connect(store.path) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        _raw_insert(
            connection, "deferred_candidates", DEFERRED_CANDIDATE_COLUMNS,
            tuple(candidate_row),
        )
        _raw_insert(
            connection, "deferred_attempts", DEFERRED_ATTEMPT_COLUMNS,
            tuple(attempt_row),
        )
    with pytest.raises(OpsKernelCorruptError):
        if table == "deferred_candidates":
            store.read_due_deferred(
                Market.KR, datetime.max.replace(tzinfo=timezone.utc)
            )
        else:
            store.read_deferred_attempts(DEFERRED_CANDIDATE_ROW[0])


def test_d_mod_e3_13f_timestamp_projection_and_audit_reconstruction(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    first = deferred_attempt()
    second = replace(
        first, attempt_tick=43, evidence_refs=("e3",),
        reason_code="carryover_defer", assignment=CandidateAssignment.CRITICAL,
        attempted_at=NOW + timedelta(minutes=5),
    )
    store.upsert_deferred(ProducerRole.ANALYSIS, first)
    store.upsert_deferred(ProducerRole.ANALYSIS, second)
    history = store.read_deferred_attempts(first.subject_id)
    assert history == (first, second)
    current = store.read_due_deferred(Market.KR, NOW)[0]
    assert current.attempts == len(history) == 2
    assert current.updated_at == max(row.attempted_at for row in history)
    terminal_at = NOW + timedelta(hours=1)
    store.terminalize_deferred(
        ProducerRole.ANALYSIS, first.subject_id,
        DeferredTerminalState.TERMINAL, terminal_at,
    )
    with store._read() as connection:
        updated_at = connection.execute(
            "SELECT updated_at FROM deferred_candidates WHERE subject_id=?",
            (first.subject_id,),
        ).fetchone()[0]
    assert updated_at == terminal_at.isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )
    assert store.read_deferred_attempts(first.subject_id) == history
    assert max(row.attempted_at for row in history) == second.attempted_at
    assert terminal_at != max(row.attempted_at for row in history)


def test_d_mod_e3_14_terminal_insert_defense_is_repository_owned(tmp_path):
    assert {field.name for field in fields(DeferredAttemptV1)} == {
        "subject_id", "market", "first_seen_tick", "attempt_tick", "candidate_tick",
        "candidate_id", "evidence_refs", "material_ticks", "next_review_at",
        "reason_code", "assignment", "attempted_at",
    }
    source = inspect.getsource(OpsKernelStore._upsert_deferred)
    tree = ast.parse(textwrap.dedent(source))
    insert_sql = [
        call.args[0].value
        for call in ast.walk(tree)
        if isinstance(call, ast.Call)
        and isinstance(call.func, ast.Attribute)
        and call.func.attr == "execute"
        and call.args
        and isinstance(call.args[0], ast.Constant)
        and isinstance(call.args[0].value, str)
        and "INSERT INTO deferred_candidates" in call.args[0].value
    ]
    assert len(insert_sql) == 1
    match = re.fullmatch(
        r"INSERT INTO deferred_candidates\(([^)]+)\) VALUES\(([^)]+)\)",
        insert_sql[0],
    )
    assert match is not None
    columns = tuple(part.strip() for part in match.group(1).split(","))
    values = tuple(part.strip() for part in match.group(2).split(","))
    assert len(columns) == len(values)
    assert values[columns.index("terminal_state")] == "NULL"
    assert values[columns.index("terminal_state")] != "?"


def test_e_market_is_closed_durable_and_due_read_is_market_scoped(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    kr = deferred_attempt(subject_id="kr-subject", market=Market.KR)
    us = deferred_attempt(subject_id="us-subject", market=Market.US)
    store.upsert_deferred(ProducerRole.ANALYSIS, kr)
    store.upsert_deferred(ProducerRole.ANALYSIS, us)

    assert store.read_due_deferred(Market.KR, NOW) == (
        replace(store.read_due_deferred(Market.KR, NOW)[0], market=Market.KR),
    )
    assert store.read_due_deferred(Market.US, NOW)[0].market is Market.US
    with pytest.raises(OpsKernelConflictError):
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            replace(kr, market=Market.US, attempt_tick=43, candidate_tick=42),
        )
    assert store.read_deferred_attempts("kr-subject") == (kr,)

    with pytest.raises(ValueError):
        replace(kr, market="KR")
    with sqlite3.connect(store.path) as connection:
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE deferred_candidates SET market='JP' WHERE subject_id='kr-subject'"
            )
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE deferred_attempts SET market='JP' WHERE subject_id='kr-subject'"
            )
    terminal_writers = {
        name for methods in ROLE_METHODS.values() for name in methods
        if "terminal_state" in inspect.signature(getattr(OpsKernelStore, name)).parameters
    }
    assert terminal_writers == {"terminalize_deferred"}
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "INSERT INTO deferred_candidates VALUES"
            "(?,?,?,?,?,?,?,?,?,?)",
            ("raw-terminal", "KR", "[]", "{}", 1, 1,
             "2026-07-30T01:00:00.000000Z", 1, "TERMINAL",
             "2026-07-30T01:00:00.000000Z"),
        )
        assert connection.execute(
            "SELECT terminal_state FROM deferred_candidates"
            " WHERE subject_id='raw-terminal'"
        ).fetchone() == ("TERMINAL",)


def test_d_mod_e3_15_retention_fk_pack_release_and_no_prune_api(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.upsert_deferred(ProducerRole.ANALYSIS, deferred_attempt())
    assert store.referenced_pack_ticks()[JobKind.TRADES] == frozenset({37, 39})
    store.terminalize_deferred(
        ProducerRole.ANALYSIS, "deferred-subject",
        DeferredTerminalState.TERMINAL, NOW + timedelta(hours=1),
    )
    assert 37 not in store.referenced_pack_ticks()[JobKind.TRADES]
    connection = sqlite3.connect(store.path)
    connection.execute("PRAGMA foreign_keys=ON")
    with pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "DELETE FROM candidate_manifest WHERE tick_id=42 AND candidate_id='c1'"
        )
    connection.close()
    assert {
        name for name, value in inspect.getmembers(OpsKernelStore, inspect.isfunction)
        if any(token in name for token in ("prune", "delete", "truncate", "archive"))
    } == {"prune_request_parity"}


@pytest.mark.parametrize("crash_point", ["after_current", "after_history"])
def test_d_mod_e3_12_current_history_crash_is_atomic(tmp_path, crash_point):
    store = make_store(tmp_path)
    create_sealed(store)
    with pytest.raises(RuntimeError, match="injected"):
        store.upsert_deferred(
            ProducerRole.ANALYSIS, deferred_attempt(),
            inject_crash_at=crash_point,
        )
    assert store.read_due_deferred(Market.KR, NOW) == ()
    assert store.read_deferred_attempts("deferred-subject") == ()


def test_d_mod_e3_12b_same_attempt_two_connection_writers_are_atomic(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    stores = (OpsKernelStore(store.path), OpsKernelStore(store.path))
    barrier = __import__("threading").Barrier(2)

    def write(candidate_store):
        barrier.wait(timeout=2)
        try:
            return candidate_store.upsert_deferred(
                ProducerRole.ANALYSIS, deferred_attempt()
            )
        except OpsKernelLockedError:
            return "LOCKED"

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(write, candidate_store) for candidate_store in stores]
        outcomes = [future.result(timeout=5) for future in futures]
    assert outcomes.count(ReplayResult.CREATED) == 1
    assert outcomes.count(ReplayResult.DUPLICATE) + outcomes.count("LOCKED") == 1
    current = store.read_due_deferred(Market.KR, NOW)
    history = store.read_deferred_attempts("deferred-subject")
    assert len(current) == 1
    assert current[0].attempts == 1
    assert history == (deferred_attempt(),)


def test_d_mod_e3_12c_reverse_tick_two_connections_keep_high_tick(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.upsert_deferred(ProducerRole.ANALYSIS, deferred_attempt())
    high = replace(
        deferred_attempt(), attempt_tick=44, evidence_refs=("high",),
        attempted_at=NOW + timedelta(minutes=2),
    )
    low = replace(
        deferred_attempt(), attempt_tick=43, evidence_refs=("low",),
        attempted_at=NOW + timedelta(minutes=1),
    )
    high_connection = store._connect()
    high_connection.execute("BEGIN IMMEDIATE")
    assert store._upsert_deferred(high_connection, high) is ReplayResult.CREATED
    barrier = __import__("threading").Barrier(2)

    def write_low_after_high_is_pending():
        low_store = OpsKernelStore(store.path)
        connection = low_store._connect()
        try:
            barrier.wait(timeout=2)
            connection.execute("BEGIN IMMEDIATE")
            low_store._upsert_deferred(connection, low)
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(write_low_after_high_is_pending)
        barrier.wait(timeout=2)
        high_connection.commit()
        high_connection.close()
        with pytest.raises(OpsKernelConflictError):
            future.result(timeout=5)
    current = store.read_due_deferred(Market.KR, NOW + timedelta(minutes=3))[0]
    history = store.read_deferred_attempts("deferred-subject")
    assert current.last_attempt_tick == 44
    assert current.attempts == 2
    assert tuple(row.attempt_tick for row in history) == (42, 44)
    assert all(row.attempt_tick != 43 for row in history)


def test_d_mod_e3_12d_reopen_roundtrip_keeps_current_and_history_consistent(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    first = deferred_attempt()
    second = replace(
        first, attempt_tick=49, evidence_refs=("e49",),
        reason_code="carryover_defer", assignment=CandidateAssignment.CRITICAL,
        attempted_at=NOW + timedelta(minutes=7),
    )
    store.upsert_deferred(ProducerRole.ANALYSIS, first)
    store.upsert_deferred(ProducerRole.ANALYSIS, second)
    expected_current = store.read_due_deferred(
        Market.KR, NOW + timedelta(minutes=8)
    )
    expected_history = store.read_deferred_attempts(first.subject_id)
    store.close()

    reopened = OpsKernelStore(store.path)
    assert reopened.read_due_deferred(
        Market.KR, NOW + timedelta(minutes=8)
    ) == expected_current
    assert reopened.read_deferred_attempts(first.subject_id) == expected_history
    assert expected_current[0].attempts == len(expected_history) == 2
    assert expected_current[0].last_attempt_tick == expected_history[-1].attempt_tick


def test_d_mod_e3_budget_overflow_is_typed_and_unchanged(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    store.upsert_deferred(ProducerRole.ANALYSIS, deferred_attempt())
    connection = sqlite3.connect(store.path)
    first = connection.execute(
        "SELECT * FROM deferred_attempts"
    ).fetchone()
    connection.executemany(
        "INSERT INTO deferred_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (
            ("deferred-subject", first[1], tick, first[3], first[4], first[5],
             first[6], first[7], first[8], first[9], first[10])
            for tick in range(43, 42 + 8640)
        ),
    )
    connection.execute(
        "UPDATE deferred_candidates SET attempts=8640,last_attempt_tick=8681"
        " WHERE subject_id='deferred-subject'"
    )
    connection.commit()
    before = connection.execute(
        "SELECT * FROM deferred_candidates"
    ).fetchone()
    count = connection.execute(
        "SELECT count(*) FROM deferred_attempts"
    ).fetchone()[0]
    connection.close()
    with pytest.raises(OpsKernelBudgetExceeded):
        store.upsert_deferred(
            ProducerRole.ANALYSIS,
            replace(
                deferred_attempt(), attempt_tick=8682,
                attempted_at=NOW + timedelta(days=30),
            ),
        )
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT * FROM deferred_candidates"
        ).fetchone() == before
        assert connection.execute(
            "SELECT count(*) FROM deferred_attempts"
        ).fetchone()[0] == count


def test_schema_v1_and_unknown_open_fail_closed_without_mutation(tmp_path):
    for version in (1, 0, 3):
        path = tmp_path / str(version) / "ops.sqlite3"
        path.parent.mkdir(mode=0o700)
        connection = sqlite3.connect(path)
        connection.execute("CREATE TABLE legacy(value TEXT) STRICT")
        connection.execute(f"PRAGMA user_version={version}")
        connection.commit()
        connection.close()
        os.chmod(path, 0o600)
        before = path.read_bytes()
        before_stat = path.stat()
        with pytest.raises(OpsKernelCorruptError, match="schema mismatch"):
            OpsKernelStore(path)
        after_stat = path.stat()
        assert path.read_bytes() == before
        assert after_stat.st_size == before_stat.st_size
        assert after_stat.st_mtime_ns == before_stat.st_mtime_ns
        assert not path.with_name(path.name + "-wal").exists()
        assert not path.with_name(path.name + "-shm").exists()


def test_e_kernel_manifest_baseline_deferred_atomic_replay_and_reads(tmp_path):
    store = make_store(tmp_path)
    baseline = SimpleNamespace(
        kind=SimpleNamespace(value="HOLDINGS_CHANGE"), subject_id="subject-1",
        baseline_hash=DIGEST, fields={"present": True},
        account_scope="toss-acct-" + "a" * 24, source_tick_id=42,
    )
    with pytest.raises(RuntimeError, match="injected"):
        store.create_manifest_with_baselines(
            ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
            (candidate(),), (baseline,), (deferred_attempt(),),
            inject_crash_at="after_deferred",
        )
    with sqlite3.connect(store.path) as connection:
        assert all(connection.execute(f"SELECT count(*) FROM {table}").fetchone()[0] == 0
                   for table in ("manifest_runs", "candidate_manifest",
                                 "subject_baselines", "deferred_candidates",
                                 "deferred_attempts"))
    assert store.create_manifest_with_baselines(
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (candidate(),), (baseline,), (deferred_attempt(),),
    ) is ReplayResult.CREATED
    assert store.create_manifest_with_baselines(
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (candidate(),), (baseline,), (deferred_attempt(),),
    ) is ReplayResult.DUPLICATE
    assert store.read_subject_baselines("HOLDINGS_CHANGE")[0]["subject_id"] == "subject-1"
    assert store.read_carryover_candidates(43)[0]["candidate_id"] == "c1"


def test_e_kernel_batch_state_replay_reconcile_and_limits(tmp_path):
    store = make_store(tmp_path)
    create_sealed(store)
    invocation_a = "a" * 32
    invocation_b = "b" * 32
    assert store.plan_analysis_batches(
        ProducerRole.ANALYSIS, 42, (batch_row(),), invocation_id=invocation_a
    ) is ReplayResult.CREATED
    assert store.plan_analysis_batches(
        ProducerRole.ANALYSIS, 42, (batch_row(),), invocation_id=invocation_b
    ) is ReplayResult.DUPLICATE
    ticket = store.start_analysis_batch(
        ProducerRole.ANALYSIS, 42, 0, invocation_id=invocation_a, now=NOW
    )
    assert ticket["state"] == "STARTED"
    before = store.read_analysis_batches(42)
    with pytest.raises(OpsKernelTransitionError):
        store.settle_analysis_batch(
            ProducerRole.ANALYSIS, 42, 0, "SOL", "OK", {"analyses": []},
            invocation_id=invocation_a, now=NOW,
        )
    assert store.read_analysis_batches(42) == before
    unknown = store.reconcile_analysis_batches(
        ProducerRole.ANALYSIS, invocation_id=invocation_b, now=NOW
    )
    assert unknown[0]["state"] == "UNKNOWN"
    with pytest.raises(OpsKernelTransitionError):
        store.start_analysis_batch(
            ProducerRole.ANALYSIS, 42, 0, invocation_id=invocation_b, now=NOW
        )
    too_many = tuple(batch_row(index, "GLM", ("c1",)) for index in range(4))
    with pytest.raises(OpsKernelBudgetExceeded):
        store.plan_analysis_batches(
            ProducerRole.ANALYSIS, 42, too_many, invocation_id=invocation_b
        )


def test_e_kernel_incident_shadow_atomic_role_and_replay(tmp_path):
    store = make_store(tmp_path)
    key = IncidentKeyV1("model-call-unknown", "glm-5.2")
    row = incident_outbox(key)
    with pytest.raises(RuntimeError, match="injected"):
        store.observe_incident_with_shadow_outbox(
            ProducerRole.ANALYSIS, key, "42:0", row, NOW,
            inject_crash_at="after_incident",
        )
    assert store.read_incident(key) is None
    observation, replay = store.observe_incident_with_shadow_outbox(
        ProducerRole.ANALYSIS, key, "42:0", row, NOW
    )
    assert observation.notification_required is True and replay is ReplayResult.CREATED
    observation, replay = store.observe_incident_with_shadow_outbox(
        ProducerRole.ANALYSIS, key, "42:0", None, NOW
    )
    assert observation.notification_required is False and replay is None
    with pytest.raises(OpsKernelRoleError):
        store.resolve_incident_with_shadow_outbox(
            ProducerRole.HERMES, key, None, NOW
        )
    with pytest.raises(OpsKernelTransitionError):
        store.resolve_incident_with_shadow_outbox(
            ProducerRole.ANALYSIS, key, None, NOW
        )
    resolved, replay = store.resolve_incident_with_shadow_outbox(
        ProducerRole.ANALYSIS, key, incident_outbox(key, "incident-a2"), NOW
    )
    assert resolved.notification_required is True and replay is ReplayResult.CREATED
    assert store.read_incident_rows("RESOLVED")[0].key == key


def test_e_kernel_audit_survey_rotation_coverage_and_role_surface(tmp_path):
    store = make_store(tmp_path)
    kinds = (
        "HOLDINGS_CHANGE", "QUOTE_STATE", "COMPLETED_EXECUTION",
        "OPERATIONAL_INCIDENT",
    )
    rows = tuple({
        "kind": kind, "eligible_count": 2, "debt_count": 1,
        "oldest_wait_regular_sessions": 1, "capacity_incident_opened": False,
    } for kind in kinds)
    store.write_audit_survey(ProducerRole.ANALYSIS, "2026-07-30", rows, NOW)
    coverage = store.read_audit_coverage("2026-07-30")
    assert len(coverage) == 4 and all(row["audited_count"] == 0 for row in coverage)
    store.update_sample_rotation(ProducerRole.ANALYSIS, kinds[0], "subject-1", NOW)
    assert store.read_sample_rotation(kinds[0])["cursor_subject_id"] == "subject-1"
    with pytest.raises(ValueError):
        store.write_audit_survey(ProducerRole.ANALYSIS, "2026-07-30", rows[:3], NOW)


def test_e_kernel_role_surface_exactly_includes_complete_audit():
    expected = {
        "create_manifest_with_baselines", "plan_analysis_batches",
        "start_analysis_batch", "settle_analysis_batch",
        "reconcile_analysis_batches", "observe_incident_with_shadow_outbox",
        "resolve_incident_with_shadow_outbox", "write_audit_survey",
        "complete_audit",
    }
    assert expected <= ROLE_METHODS[ProducerRole.ANALYSIS]
    assert all("write_audit_coverage" not in methods for methods in ROLE_METHODS.values())


def test_e_kernel_complete_audit_public_contract_is_exercised(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(OpsKernelTransitionError):
        store.complete_audit(
            ProducerRole.ANALYSIS, tick_id=42, candidate_id="missing",
            batch_ordinal=0, kind="HOLDINGS_CHANGE", subject_id="subject-1",
            session_date="2026-07-30", considered_session="2026-07-30",
            now=NOW,
        )


AUDIT_KINDS = tuple(kind.value for kind in MaterialKindV1)


def prepare_audit(
    store, *, tick_id=42, candidate_id="audit-c1",
    kind=MaterialKindV1.HOLDINGS_CHANGE, subject_id="audit-subject",
    session_date="2026-07-30", citations=("audit-e1",),
):
    sampled = candidate(
        candidate_id, assignment=CandidateAssignment.SAMPLED_AUDIT,
        kind=kind.value, subject_id=subject_id, evidence_refs=("audit-e1",),
    )
    assert store.create_manifest(
        ProducerRole.ANALYSIS, tick_id, NOW,
        {JobKind.HOLDINGS: 40, JobKind.QUOTES: 41, JobKind.TRADES: 39},
        DIGEST, (sampled,),
    ) is ReplayResult.CREATED
    store.seal_manifest(ProducerRole.ANALYSIS, tick_id, NOW)
    assert store.plan_analysis_batches(
        ProducerRole.ANALYSIS, tick_id,
        (batch_row(0, "SOL", (candidate_id,)),), invocation_id="a" * 32,
    ) is ReplayResult.CREATED
    store.start_analysis_batch(
        ProducerRole.ANALYSIS, tick_id, 0, invocation_id="a" * 32, now=NOW,
    )
    store.settle_analysis_batch(
        ProducerRole.ANALYSIS, tick_id, 0, "SOL", "OK",
        {"analyses": [{
            "candidate_id": candidate_id,
            "cited_evidence_ids": list(citations),
        }]},
        invocation_id="a" * 32, now=NOW,
    )
    store.upsert_audit_backlog(
        ProducerRole.ANALYSIS, kind.value, subject_id,
        session_date, session_date, "PENDING_AUDIT",
    )
    store.write_audit_survey(
        ProducerRole.ANALYSIS, session_date,
        tuple({
            "kind": item, "eligible_count": 1, "debt_count": 1,
            "oldest_wait_regular_sessions": 1,
            "capacity_incident_opened": False,
        } for item in AUDIT_KINDS),
        NOW,
    )


def audit_call(store, **overrides):
    values = {
        "tick_id": 42, "candidate_id": "audit-c1", "batch_ordinal": 0,
        "kind": "HOLDINGS_CHANGE", "subject_id": "audit-subject",
        "session_date": "2026-07-30",
        "considered_session": "2026-07-30", "now": NOW,
    }
    values.update(overrides)
    return store.complete_audit(ProducerRole.ANALYSIS, **values)


def audit_rows(store):
    return (
        store.read_audit_backlog("HOLDINGS_CHANGE"),
        store.read_audit_coverage("2026-07-30"),
    )


def test_e_kernel_complete_audit_success_receipt_exact_values_and_privacy(tmp_path):
    store = make_store(tmp_path)
    prepare_audit(store)
    before = store.read_audit_coverage("2026-07-30")
    receipt = audit_call(store)
    assert receipt == AuditCompletionV1(
        MaterialKindV1.HOLDINGS_CHANGE, "audit-subject", "2026-07-30", 1,
    )
    assert tuple(field.name for field in fields(receipt)) == (
        "kind", "subject_id", "session_date", "audited_count",
    )
    assert store.read_audit_backlog("HOLDINGS_CHANGE")[0].state == "AUDITED"
    after = store.read_audit_coverage("2026-07-30")
    changed = next(row for row in after if row["kind"] == "HOLDINGS_CHANGE")
    old = next(row for row in before if row["kind"] == "HOLDINGS_CHANGE")
    assert changed["audited_count"] == old["audited_count"] + 1
    assert {key: changed[key] for key in changed if key != "audited_count"} == {
        key: old[key] for key in old if key != "audited_count"
    }
    assert not {
        "tick_id", "candidate_id", "batch_ordinal", "cited_evidence_ids",
        "account_scope", "result_json",
    } & {field.name for field in fields(receipt)}


@pytest.mark.parametrize(
    ("mutate", "overrides"),
    [
        ("candidate_missing", {}),
        ("assignment", {}),
        ("candidate_identity", {"subject_id": "wrong-subject"}),
        ("batch_missing", {"batch_ordinal": 9}),
        ("batch_provider", {}),
        ("batch_state", {}),
        ("batch_exit", {}),
        ("batch_coverage", {}),
        ("result_missing", {}),
        ("result_duplicate", {}),
        ("citation", {}),
        ("backlog_missing", {}),
        ("backlog_state", {}),
        ("coverage_missing", {"session_date": "2026-07-31"}),
    ],
)
def test_e_kernel_complete_audit_all_conditions_rollback(
    tmp_path, mutate, overrides,
):
    store = make_store(tmp_path)
    prepare_audit(store)
    with sqlite3.connect(store.path) as connection:
        if mutate == "candidate_missing":
            connection.execute("PRAGMA foreign_keys=OFF")
            connection.execute("DELETE FROM candidate_manifest")
        elif mutate == "assignment":
            connection.execute(
                "UPDATE candidate_manifest SET assignment='FULL_REVIEW'"
            )
        elif mutate == "batch_provider":
            connection.execute("UPDATE analysis_batches SET provider='GLM'")
        elif mutate == "batch_state":
            connection.execute(
                "UPDATE analysis_batches SET state='UNKNOWN',exit=NULL,"
                "result_json=NULL,result_digest=NULL"
            )
        elif mutate == "batch_exit":
            connection.execute(
                "UPDATE analysis_batches SET exit='TIMEOUT',"
                "result_json=NULL,result_digest=NULL"
            )
        elif mutate == "batch_coverage":
            connection.execute(
                "UPDATE analysis_batches SET candidate_ids_json='[\"other\"]'"
            )
        elif mutate == "result_missing":
            connection.execute(
                "UPDATE analysis_batches SET result_json='{\"analyses\":[]}'"
            )
        elif mutate == "result_duplicate":
            connection.execute(
                "UPDATE analysis_batches SET result_json="
                "'{\"analyses\":[{\"candidate_id\":\"audit-c1\","
                "\"cited_evidence_ids\":[]},{\"candidate_id\":\"audit-c1\","
                "\"cited_evidence_ids\":[]}]}'"
            )
        elif mutate == "citation":
            connection.execute(
                "UPDATE analysis_batches SET result_json="
                "'{\"analyses\":[{\"candidate_id\":\"audit-c1\","
                "\"cited_evidence_ids\":[\"private-evidence\"]}]}'"
            )
        elif mutate == "backlog_missing":
            connection.execute("DELETE FROM audit_backlog")
        elif mutate == "backlog_state":
            connection.execute("UPDATE audit_backlog SET state='AUDITED'")
        connection.commit()
    before = audit_rows(store)
    with pytest.raises(OpsKernelTransitionError):
        audit_call(store, **overrides)
    assert audit_rows(store) == before


def test_e_kernel_complete_audit_global_cap_and_same_tx_post_increment(tmp_path):
    store = make_store(tmp_path)
    prepare_audit(store)
    with sqlite3.connect(store.path) as connection:
        connection.execute(
            "UPDATE audit_coverage SET audited_count=0 "
            "WHERE regular_session_date='2026-07-30'"
        )
        connection.execute(
            "UPDATE audit_coverage SET audited_count=2 "
            "WHERE regular_session_date='2026-07-30' AND kind='HOLDINGS_CHANGE'"
        )
        connection.commit()
    receipt = audit_call(store)
    assert receipt.audited_count == 3

    capped = make_store(tmp_path / "capped")
    prepare_audit(capped)
    with sqlite3.connect(capped.path) as connection:
        connection.execute(
            "UPDATE audit_coverage SET audited_count=2 "
            "WHERE kind='HOLDINGS_CHANGE'"
        )
        connection.execute(
            "UPDATE audit_coverage SET audited_count=1 "
            "WHERE kind<>'HOLDINGS_CHANGE'"
        )
        connection.commit()
    before = audit_rows(capped)
    with pytest.raises(OpsKernelBudgetExceeded):
        audit_call(capped)
    assert audit_rows(capped) == before


# --- numeric feature wave: quote_state_history ring (plan §4, §8) ---


def history_advance(
    subject_id="quote-subject", tick_id=42, observed_at=NOW, last_price="11",
):
    return SimpleNamespace(
        subject_id=subject_id, tick_id=tick_id,
        observed_at=observed_at, last_price=last_price,
    )


def test_quote_state_history_roundtrip_and_pk_conflict(tmp_path):
    store = make_store(tmp_path)
    assert store.create_manifest_with_baselines(
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (), (), (), history_advances=(history_advance(),),
    ) is ReplayResult.CREATED
    rows = store.read_quote_state_history(("quote-subject",))["quote-subject"]
    assert [
        (row["subject_id"], row["tick_id"], row["observed_at"], row["last_price"])
        for row in rows
    ] == [("quote-subject", 42, NOW, "11")]
    before = rows
    with pytest.raises(OpsKernelConflictError):
        store.create_manifest_with_baselines(
            ProducerRole.ANALYSIS, 43, NOW, {JobKind.TRADES: 39}, DIGEST,
            (), (), (),
            history_advances=(history_advance(tick_id=42, last_price="12"),),
        )
    assert store.read_quote_state_history(("quote-subject",))["quote-subject"] == before
    assert store.read_manifest_summary(43) is None


def test_quote_state_history_trims_to_latest_seven_rows(tmp_path):
    store = make_store(tmp_path)
    for tick in range(1, 9):
        assert store.create_manifest_with_baselines(
            ProducerRole.ANALYSIS, tick, NOW, {JobKind.TRADES: 0}, DIGEST,
            (), (), (),
            history_advances=(
                history_advance(tick_id=tick, last_price=str(tick)),
            ),
        ) is ReplayResult.CREATED
        with sqlite3.connect(store.path) as connection:
            stored = connection.execute(
                "SELECT count(*) FROM quote_state_history"
            ).fetchone()[0]
        assert stored == min(tick, 7)
    rows = store.read_quote_state_history(("quote-subject",))["quote-subject"]
    assert [row["tick_id"] for row in rows] == [8, 7, 6, 5, 4, 3, 2]
    assert [row["last_price"] for row in rows] == ["8", "7", "6", "5", "4", "3", "2"]


def test_read_quote_state_history_is_bulk_scoped_and_empty_for_missing(tmp_path):
    store = make_store(tmp_path)
    assert store.create_manifest_with_baselines(
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (), (), (),
        history_advances=(
            history_advance(subject_id="subject-a", last_price="1"),
            history_advance(subject_id="subject-b", last_price="2"),
        ),
    ) is ReplayResult.CREATED
    result = store.read_quote_state_history(("subject-a", "missing-subject"))
    assert set(result) == {"subject-a", "missing-subject"}
    assert result["missing-subject"] == ()
    assert [row["last_price"] for row in result["subject-a"]] == ["1"]
    assert "subject-b" not in result
    assert dict(store.read_quote_state_history(())) == {}


def test_history_commit_is_atomic_with_manifest_and_baselines(tmp_path):
    store = make_store(tmp_path)
    baseline = SimpleNamespace(
        kind=SimpleNamespace(value="QUOTE_STATE"), subject_id="quote-subject",
        baseline_hash=DIGEST, fields={"market": "KR"},
        account_scope=None, source_tick_id=42,
    )
    for crash_at in ("after_baselines", "after_history"):
        with pytest.raises(RuntimeError, match="injected"):
            store.create_manifest_with_baselines(
                ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
                (candidate(),), (baseline,), (deferred_attempt(),),
                history_advances=(history_advance(),),
                inject_crash_at=crash_at,
            )
        with sqlite3.connect(store.path) as connection:
            assert all(
                connection.execute(
                    f"SELECT count(*) FROM {table}"
                ).fetchone()[0] == 0
                for table in (
                    "manifest_runs", "candidate_manifest", "subject_baselines",
                    "quote_state_history", "deferred_candidates",
                    "deferred_attempts",
                )
            )


def test_same_tick_replay_does_not_reinsert_history_and_conflicts_on_drift(
    tmp_path,
):
    store = make_store(tmp_path)
    args = (
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (candidate(),), (), (),
    )
    assert store.create_manifest_with_baselines(
        *args, history_advances=(history_advance(),),
    ) is ReplayResult.CREATED
    assert store.create_manifest_with_baselines(
        *args, history_advances=(history_advance(),),
    ) is ReplayResult.DUPLICATE
    rows = store.read_quote_state_history(("quote-subject",))["quote-subject"]
    assert len(rows) == 1
    with pytest.raises(OpsKernelConflictError):
        store.create_manifest_with_baselines(
            *args, history_advances=(history_advance(last_price="99"),),
        )
    with pytest.raises(OpsKernelConflictError):
        store.create_manifest_with_baselines(
            *args,
            history_advances=(
                history_advance(),
                history_advance(subject_id="never-written"),
            ),
        )


def test_history_advances_is_keyword_only_and_deferred_stays_positional(
    tmp_path,
):
    parameters = list(inspect.signature(
        OpsKernelStore.create_manifest_with_baselines
    ).parameters.values())
    names = [parameter.name for parameter in parameters]
    assert names[:9] == [
        "self", "role", "tick_id", "started_at", "material_ticks",
        "material_binding_digest", "candidates", "baseline_advances",
        "deferred_writes",
    ]
    by_name = {parameter.name: parameter for parameter in parameters}
    assert by_name["history_advances"].kind is inspect.Parameter.KEYWORD_ONLY
    assert by_name["history_advances"].default == ()
    assert by_name["deferred_writes"].kind is inspect.Parameter.POSITIONAL_OR_KEYWORD
    assert by_name["deferred_writes"].default is inspect.Parameter.empty
    store = make_store(tmp_path)
    assert store.create_manifest_with_baselines(
        ProducerRole.ANALYSIS, 42, NOW, {JobKind.TRADES: 39}, DIGEST,
        (candidate(),), (), (deferred_attempt(),),
    ) is ReplayResult.CREATED
    assert len(store.read_deferred_attempts("deferred-subject")) == 1
    with sqlite3.connect(store.path) as connection:
        assert connection.execute(
            "SELECT count(*) FROM quote_state_history"
        ).fetchone()[0] == 0


def test_schema_v6_fresh_bootstrap_creates_history_table(tmp_path):
    from tokmo.records.ops_kernel import SCHEMA_VERSION

    assert SCHEMA_VERSION == 15
    store = make_store(tmp_path)
    with sqlite3.connect(store.path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (15,)
        assert [
            row[1] for row in connection.execute(
                "PRAGMA table_info(quote_state_history)"
            )
        ] == ["subject_id", "tick_id", "observed_at", "last_price_text"]
        assert connection.execute(
            "SELECT strict FROM pragma_table_list "
            "WHERE name='quote_state_history'"
        ).fetchone() == (1,)


def test_pension_reminder_kernel_role_contract_is_exact_and_typed():
    parameters = inspect.signature(
        OpsKernelStore.record_pension_reminder_outbox
    ).parameters
    assert tuple(parameters) == ("self", "role", "outbox", "activation_enabled")
    assert parameters["activation_enabled"].default is False
    assert "record_pension_reminder_outbox" in ROLE_METHODS[ProducerRole.COLLECTOR]
    assert "record_pension_reminder_outbox" not in ROLE_METHODS[ProducerRole.HERMES]


_CANONICAL_V7_COMMIT = "627593dd9780c03437250785679717e62a505a2e"
_CANONICAL_V7_SCHEMA_SHA256 = (
    "6e80c63c2c6e588c71a013e01e7124db0b557de08a6a5f402b1e5fe1599704d0"
)


def _canonical_v7_schema_sql():
    """Extract reviewed pre-v8 SQL bytes without consulting the v8 constant."""
    root = "projects/tokmo-v3/src/tokmo/"
    source_path = root + "records/ops_kernel.py"
    request_path = root + "values/request_parity.py"
    request_source = subprocess.run(
        ["git", "show", f"{_CANONICAL_V7_COMMIT}:{request_path}"],
        check=True, text=True, capture_output=True,
    ).stdout
    source = subprocess.run(
        ["git", "show", f"{_CANONICAL_V7_COMMIT}:{source_path}"],
        check=True, text=True, capture_output=True,
    ).stdout
    historical_request = types.ModuleType("tokmo.values.request_parity")
    historical_request.__file__ = request_path
    historical_request.__package__ = "tokmo.values"
    current_request = sys.modules.get(historical_request.__name__)
    exec(compile(request_source, request_path, "exec"), historical_request.__dict__)
    historical = types.ModuleType("_canonical_v7_ops_kernel")
    historical.__file__ = source_path
    historical.__package__ = "tokmo.records"
    try:
        sys.modules[historical_request.__name__] = historical_request
        exec(compile(source, source_path, "exec"), historical.__dict__)
    finally:
        if current_request is None:
            del sys.modules[historical_request.__name__]
        else:
            sys.modules[historical_request.__name__] = current_request
    schema = historical.SCHEMA_SQL
    assert historical.SCHEMA_VERSION == 7
    assert hashlib.sha256(schema.encode()).hexdigest() == _CANONICAL_V7_SCHEMA_SHA256
    assert "DAILY_CANDLES" not in schema
    assert "/api/v1/candles" not in schema
    return schema


def test_v7_legacy_reads_preserve_cadence_tick_readback_until_migration(tmp_path):
    from tokmo.records.ops_kernel import OpsKernelStore, SCHEMA_VERSION
    from tokmo.values.ops_kernel import OpsKernelMigrationRequiredError

    path = tmp_path / "legacy-v7.sqlite3"
    stamp = "2026-08-10T00:00:00.000000Z"
    snapshot = '{"marker":"preserve-me"}'
    with sqlite3.connect(path) as connection:
        connection.executescript(_canonical_v7_schema_sql())
        connection.executemany(
            "INSERT INTO cadence_ticks VALUES(?,?,?,?,?)",
            ((f"legacy-policy-{index}", stamp, f"{index:064x}", snapshot, stamp)
             for index in range(1652)),
        )
    os.chmod(path, 0o600)
    with pytest.raises(OpsKernelMigrationRequiredError):
        OpsKernelStore(path)
    # Historical v7 row compatibility stays provable via the test-only
    # legacy builder; ordinary construction never migrates (Plan/SDS
    # 2026-08-30 §4).
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute(
            "SELECT count(*) FROM cadence_ticks"
        ).fetchone() == (1652,)


def test_v8_daily_scope_check_and_malformed_v7_rollback(tmp_path):
    from tokmo.records.ops_kernel import OpsKernelStore
    from tokmo.values.ops_kernel import OpsKernelCorruptError

    fresh = OpsKernelStore(tmp_path / "fresh.sqlite3")
    with sqlite3.connect(fresh.path) as connection:
        sql = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='planned_requests'"
        ).fetchone()[0]
    assert "'DAILY_CANDLES'" in sql

    path = tmp_path / "malformed-v7.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.executescript(_canonical_v7_schema_sql())
        connection.execute("DROP INDEX cadence_wire_starts_open")
    os.chmod(path, 0o600)
    before = path.read_bytes()
    with pytest.raises(OpsKernelCorruptError):
        OpsKernelStore(path)
    assert path.read_bytes() == before
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone() == (7,)
        assert connection.execute(
            "SELECT count(*) FROM sqlite_schema WHERE name='cadence_wire_starts_open'"
        ).fetchone() == (0,)
