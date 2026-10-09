"""Hardened, closed-command SQLite store for Slice D operational transitions."""

import fcntl
import hashlib
import json
import os
import re
import sqlite3
import stat
import uuid
from collections import Counter
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, date, datetime, timedelta, timezone
from pathlib import Path
from typing import NamedTuple

from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (
    prepare_private_path,
    private_creation,
    sqlite_locked,
)
from tokmo.values.cadence_policy import (
    CADENCE_CHILD_LIMIT_FIELDS,
    CADENCE_OPERATIONS,
    CALENDAR_ATTEMPT_MAX,
    CALENDAR_RETRY_DELAY_MINUTES,
    GROUP_LIMIT_FIELDS,
    TICK_REQUEST_LIMITS_V1,
    CadencePolicyV1,
    TradesChildLimitsV1,
    policy_digest,
    policy_snapshot,
    validate_cadence_operation,
)
from tokmo.values.material_pack import JobKind, canonical_decode, canonical_encode
from tokmo.values.ops_kernel import (
    _HYPOTHESIS_TERMINAL_STATES,
    _HYPOTHESIS_TRANSITIONS,
    AdmissionDecisionV1,
    AlertChannel,
    AlertOrigin,
    AlertOutboxV1,
    AlertResolution,
    AlertStatus,
    AuditBacklogV1,
    AuditCompletionV1,
    CadenceBindingError,
    CadenceBudgetExceeded,
    CadenceCalendarBackoffError,
    CadenceCalendarClosedError,
    CadenceCalendarExhaustedError,
    CadenceDuplicateError,
    CadencePolicyMismatchError,
    CadenceTransitionError,
    CalendarStartReceiptV1,
    CandidateAssignment,
    CandidateManifestV1,
    CandidateState,
    DeferredAttemptV1,
    DeferredCandidateV1,
    DeferredTerminalState,
    DispositionAction,
    DispositionV1,
    FeedbackEventKind,
    FeedbackInboundV1,
    HypothesisMergeOutcome,
    HypothesisRelationV1,
    HypothesisSightingV1,
    HypothesisState,
    HypothesisV1,
    IncidentKeyV1,
    IncidentObservationV1,
    IncidentState,
    IncidentV1,
    ManifestState,
    ManifestSummaryV1,
    Market,
    MaterialKindV1,
    MergeReviewV1,
    OpsKernelBudgetExceeded,
    OpsKernelConflictError,
    OpsKernelCooldownActive,
    OpsKernelCorruptError,
    OpsKernelLockedError,
    OpsKernelMigrationRequiredError,
    OpsKernelRoleError,
    OpsKernelTransitionError,
    ProducerRole,
    ReplayResult,
    ReservationCountsV1,
    SemanticClassification,
    SemanticDecisionV1,
    TrackingCandidateV1,
    TriggerKind,
    WeeklyReviewPairV1,
    WeeklyReviewRunV1,
    created_on_or_after,
    iso,
    parse_iso,
    require_token,
    validate_failure_reason,
    validate_resolution_reason,
)
from tokmo.values.request_parity import (
    PROVIDER_HOST,
    REQUEST_JOB_SCOPES,
    REQUEST_OPERATION_SPECS,
    REQUEST_PATHS,
    RequestOperation,
    canonical_params_digest,
    validate_canonical_params,
)

SCHEMA_VERSION = 15
DEFERRED_ATTEMPTS_MAX = 8640
BUSY_TIMEOUT_MS = 250


class _ReconciledAnalysisResultV1(NamedTuple):
    result_id: str
    request_id: str
    analysis_kind: str
    status: str
    decision_id: None
    artifact_digest: None
    analyzed_at: datetime


def _ts27(column):
    return (
        f"length({column})=27 AND substr({column},5,1)='-' "
        f"AND substr({column},8,1)='-' AND substr({column},11,1)='T' "
        f"AND substr({column},14,1)=':' AND substr({column},17,1)=':' "
        f"AND substr({column},20,1)='.' AND substr({column},27,1)='Z' "
        f"AND NOT substr({column},1,4) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},6,2) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},9,2) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},12,2) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},15,2) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},18,2) GLOB '*[^0-9]*' "
        f"AND NOT substr({column},21,6) GLOB '*[^0-9]*'"
    )


def _sha256(raw):
    constants = (
        0x428A2F98,
        0x71374491,
        0xB5C0FBCF,
        0xE9B5DBA5,
        0x3956C25B,
        0x59F111F1,
        0x923F82A4,
        0xAB1C5ED5,
        0xD807AA98,
        0x12835B01,
        0x243185BE,
        0x550C7DC3,
        0x72BE5D74,
        0x80DEB1FE,
        0x9BDC06A7,
        0xC19BF174,
        0xE49B69C1,
        0xEFBE4786,
        0x0FC19DC6,
        0x240CA1CC,
        0x2DE92C6F,
        0x4A7484AA,
        0x5CB0A9DC,
        0x76F988DA,
        0x983E5152,
        0xA831C66D,
        0xB00327C8,
        0xBF597FC7,
        0xC6E00BF3,
        0xD5A79147,
        0x06CA6351,
        0x14292967,
        0x27B70A85,
        0x2E1B2138,
        0x4D2C6DFC,
        0x53380D13,
        0x650A7354,
        0x766A0ABB,
        0x81C2C92E,
        0x92722C85,
        0xA2BFE8A1,
        0xA81A664B,
        0xC24B8B70,
        0xC76C51A3,
        0xD192E819,
        0xD6990624,
        0xF40E3585,
        0x106AA070,
        0x19A4C116,
        0x1E376C08,
        0x2748774C,
        0x34B0BCB5,
        0x391C0CB3,
        0x4ED8AA4A,
        0x5B9CCA4F,
        0x682E6FF3,
        0x748F82EE,
        0x78A5636F,
        0x84C87814,
        0x8CC70208,
        0x90BEFFFA,
        0xA4506CEB,
        0xBEF9A3F7,
        0xC67178F2,
    )
    state = [
        0x6A09E667,
        0xBB67AE85,
        0x3C6EF372,
        0xA54FF53A,
        0x510E527F,
        0x9B05688C,
        0x1F83D9AB,
        0x5BE0CD19,
    ]
    bit_length = len(raw) * 8
    padded = raw + b"\x80"
    padded += b"\0" * ((55 - len(raw)) % 64) + bit_length.to_bytes(8, "big")
    rotate = lambda value, count: (
        ((value >> count) | (value << (32 - count))) & 0xFFFFFFFF
    )
    for offset in range(0, len(padded), 64):
        words = [
            int.from_bytes(padded[offset + index : offset + index + 4], "big")
            for index in range(0, 64, 4)
        ]
        for index in range(16, 64):
            s0 = (
                rotate(words[index - 15], 7)
                ^ rotate(words[index - 15], 18)
                ^ (words[index - 15] >> 3)
            )
            s1 = (
                rotate(words[index - 2], 17)
                ^ rotate(words[index - 2], 19)
                ^ (words[index - 2] >> 10)
            )
            words.append((words[index - 16] + s0 + words[index - 7] + s1) & 0xFFFFFFFF)
        a, b, c, d, e, f, g, h = state
        for index in range(64):
            upper = rotate(e, 6) ^ rotate(e, 11) ^ rotate(e, 25)
            choice = (e & f) ^ ((~e) & g)
            first = (h + upper + choice + constants[index] + words[index]) & 0xFFFFFFFF
            lower = rotate(a, 2) ^ rotate(a, 13) ^ rotate(a, 22)
            majority = (a & b) ^ (a & c) ^ (b & c)
            second = (lower + majority) & 0xFFFFFFFF
            h, g, f, e, d, c, b, a = (
                g,
                f,
                e,
                (d + first) & 0xFFFFFFFF,
                c,
                b,
                a,
                (first + second) & 0xFFFFFFFF,
            )
        state = [
            (left + right) & 0xFFFFFFFF
            for left, right in zip(state, (a, b, c, d, e, f, g, h))
        ]
    return "".join(f"{value:08x}" for value in state)


def _checkpoint_binding_digest(
    *,
    schema_version,
    tick_id,
    job_scope,
    ordinal,
    account_scope,
    window_from,
    window_to,
    request_cursor,
    page_digest,
    has_next,
    next_cursor,
):
    """Bind page content and every durable primitive without importing value types."""
    projection = (
        schema_version,
        tick_id,
        job_scope,
        ordinal,
        account_scope,
        window_from,
        window_to,
        request_cursor,
        page_digest,
        has_next,
        next_cursor,
    )
    encoded = bytearray()
    for value in projection:
        if value is None:
            primitive = b"n"
        elif type(value) is bool:
            primitive = b"b1" if value else b"b0"
        elif type(value) is int:
            primitive = b"i" + str(value).encode("ascii")
        elif type(value) is str:
            primitive = b"s" + value.encode("utf-8")
        else:
            raise TypeError("invalid checkpoint binding primitive")
        encoded.extend(len(primitive).to_bytes(8, "big"))
        encoded.extend(primitive)
    return _sha256(bytes(encoded))


_REQUEST_OPERATIONS_SQL = ",".join(repr(value) for value in REQUEST_OPERATION_SPECS)
_REQUEST_PATHS_SQL = ",".join(repr(value) for value in REQUEST_PATHS)
_CADENCE_WIRE_OPERATIONS_SQL = ",".join(repr(value) for value in CADENCE_OPERATIONS)
_CADENCE_WIRE_GROUPS_SQL = ",".join(repr(value) for value in GROUP_LIMIT_FIELDS)
# The deployed v14 DDL already contains MEMBERSHIP_TOSS_STOCKS immediately
# before CONDITIONAL_ORDERS. Derive that exact ordering without editing the
# closed values-level operation registry in this migration slice.
if "'MEMBERSHIP_TOSS_STOCKS'" in _CADENCE_WIRE_OPERATIONS_SQL:
    _V15_CADENCE_WIRE_OPERATIONS_SQL = _CADENCE_WIRE_OPERATIONS_SQL
else:
    _V15_CADENCE_WIRE_OPERATIONS_SQL = _CADENCE_WIRE_OPERATIONS_SQL.replace(
        "'CONDITIONAL_ORDERS'",
        "'MEMBERSHIP_TOSS_STOCKS','CONDITIONAL_ORDERS'",
    )
if _V15_CADENCE_WIRE_OPERATIONS_SQL.count("'MEMBERSHIP_TOSS_STOCKS'") != 1:
    raise RuntimeError("invalid cadence wire membership operation count")
_CORE_SCHEMA_SQL = f"""
CREATE TABLE manifest_runs(
  tick_id INTEGER PRIMARY KEY, started_at TEXT NOT NULL,
  material_ticks_json TEXT NOT NULL,
  material_binding_digest TEXT NOT NULL CHECK(length(material_binding_digest) = 64),
  counts_json TEXT NOT NULL,
  total_count INTEGER NOT NULL, state TEXT NOT NULL, sealed_at TEXT,
  failure_reason TEXT
) STRICT;
CREATE TABLE candidate_manifest(
  tick_id INTEGER NOT NULL, candidate_id TEXT NOT NULL, subject_id TEXT NOT NULL,
  kind TEXT NOT NULL, evidence_refs_json TEXT NOT NULL, assignment TEXT NOT NULL,
  state TEXT NOT NULL, created_at TEXT NOT NULL,
  PRIMARY KEY(tick_id,candidate_id),
  FOREIGN KEY(tick_id) REFERENCES manifest_runs(tick_id)
) STRICT;
CREATE TABLE dispositions(
  decision_id TEXT PRIMARY KEY, tick_id INTEGER NOT NULL, candidate_id TEXT NOT NULL,
  subject_id TEXT NOT NULL, action TEXT NOT NULL, reason_code TEXT NOT NULL,
  cited_evidence_ids_json TEXT NOT NULL, material_ticks_json TEXT NOT NULL,
  glm_status TEXT NOT NULL, sol_status TEXT NOT NULL, policy_version TEXT NOT NULL,
  decided_at TEXT NOT NULL, UNIQUE(tick_id,candidate_id),
  FOREIGN KEY(tick_id,candidate_id) REFERENCES candidate_manifest(tick_id,candidate_id)
) STRICT;
CREATE TABLE alert_outbox(
  alert_id TEXT PRIMARY KEY, origin TEXT NOT NULL, decision_id TEXT,
  incident_kind TEXT, incident_subject TEXT, requeue_of TEXT, tick_id INTEGER NOT NULL,
  subject_id TEXT NOT NULL, dedupe_key TEXT NOT NULL, channel TEXT NOT NULL,
  severity TEXT NOT NULL, body_evidence_json TEXT NOT NULL, created_at TEXT NOT NULL,
  attempted_at TEXT, delivered_at TEXT, status TEXT, provider_message_id TEXT,
  resolution TEXT NOT NULL, resolved_at TEXT, resolution_reason TEXT,
  FOREIGN KEY(decision_id) REFERENCES dispositions(decision_id),
  FOREIGN KEY(incident_kind,incident_subject) REFERENCES incidents(kind,subject),
  FOREIGN KEY(requeue_of) REFERENCES alert_outbox(alert_id)
) STRICT;
CREATE UNIQUE INDEX alert_outbox_decision_open ON alert_outbox(decision_id)
 WHERE origin='DISPOSITION' AND resolution='UNRESOLVED';
CREATE UNIQUE INDEX alert_outbox_dedupe_open ON alert_outbox(dedupe_key)
 WHERE channel='TELEGRAM' AND resolution='UNRESOLVED'
 AND status IN ('PENDING','ATTEMPTING','UNKNOWN');
CREATE TABLE incidents(
  kind TEXT NOT NULL, subject TEXT NOT NULL, first_seen_at TEXT NOT NULL,
  last_seen_at TEXT NOT NULL, count INTEGER NOT NULL, state TEXT NOT NULL,
  impact_ref TEXT NOT NULL, last_notified_at TEXT, PRIMARY KEY(kind,subject)
) STRICT;
CREATE TABLE deferred_candidates(
  subject_id TEXT PRIMARY KEY, market TEXT NOT NULL, evidence_refs_json TEXT NOT NULL,
  material_ticks_json TEXT NOT NULL, first_seen_tick INTEGER NOT NULL,
  last_attempt_tick INTEGER NOT NULL, next_review_at TEXT NOT NULL,
  attempts INTEGER NOT NULL, terminal_state TEXT, updated_at TEXT NOT NULL,
  CHECK(length(subject_id) BETWEEN 1 AND 128
    AND NOT substr(subject_id,1,1) GLOB '[^A-Za-z0-9]'
    AND NOT subject_id GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(market IN ('KR','US')),
  CHECK(first_seen_tick BETWEEN 0 AND 2147483647),
  CHECK(last_attempt_tick BETWEEN 0 AND 2147483647),
  CHECK(last_attempt_tick >= first_seen_tick),
  CHECK(attempts BETWEEN 1 AND 8640),
  CHECK(terminal_state IS NULL OR terminal_state IN
    ('DISPOSITIONED','TERMINAL','AUDITED','ESCALATED')),
  CHECK(length(CAST(evidence_refs_json AS BLOB)) BETWEEN 2 AND 8192
    AND substr(evidence_refs_json,1,1)='['
    AND substr(evidence_refs_json,length(evidence_refs_json),1)=']'
    AND NOT evidence_refs_json GLOB '* *'
    AND NOT evidence_refs_json GLOB ('*'||char(9)||'*')
    AND NOT evidence_refs_json GLOB ('*'||char(10)||'*')
    AND NOT evidence_refs_json GLOB ('*'||char(13)||'*')),
  CHECK(length(CAST(material_ticks_json AS BLOB)) BETWEEN 2 AND 256
    AND substr(material_ticks_json,1,1)='{{'
    AND substr(material_ticks_json,length(material_ticks_json),1)='}}'
    AND NOT material_ticks_json GLOB '* *'
    AND NOT material_ticks_json GLOB ('*'||char(9)||'*')
    AND NOT material_ticks_json GLOB ('*'||char(10)||'*')
    AND NOT material_ticks_json GLOB ('*'||char(13)||'*')),
  CHECK({_ts27("next_review_at")}),
  CHECK({_ts27("updated_at")})
) STRICT;
CREATE TABLE deferred_attempts(
  subject_id TEXT NOT NULL, market TEXT NOT NULL, attempt_tick INTEGER NOT NULL,
  candidate_tick INTEGER NOT NULL, candidate_id TEXT NOT NULL,
  evidence_refs_json TEXT NOT NULL, material_ticks_json TEXT NOT NULL,
  next_review_at TEXT NOT NULL, reason_code TEXT NOT NULL,
  assignment TEXT NOT NULL, attempted_at TEXT NOT NULL,
  PRIMARY KEY(subject_id,attempt_tick),
  FOREIGN KEY(candidate_tick,candidate_id)
    REFERENCES candidate_manifest(tick_id,candidate_id),
  CHECK(length(subject_id) BETWEEN 1 AND 128
    AND NOT substr(subject_id,1,1) GLOB '[^A-Za-z0-9]'
    AND NOT subject_id GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(market IN ('KR','US')),
  CHECK(length(candidate_id) BETWEEN 1 AND 128
    AND NOT substr(candidate_id,1,1) GLOB '[^A-Za-z0-9]'
    AND NOT candidate_id GLOB '*[^A-Za-z0-9_.:-]*'),
  CHECK(attempt_tick BETWEEN 0 AND 2147483647),
  CHECK(candidate_tick BETWEEN 0 AND 2147483647),
  CHECK(candidate_tick <= attempt_tick),
  CHECK(length(CAST(evidence_refs_json AS BLOB)) BETWEEN 2 AND 8192
    AND substr(evidence_refs_json,1,1)='['
    AND substr(evidence_refs_json,length(evidence_refs_json),1)=']'
    AND NOT evidence_refs_json GLOB '* *'
    AND NOT evidence_refs_json GLOB ('*'||char(9)||'*')
    AND NOT evidence_refs_json GLOB ('*'||char(10)||'*')
    AND NOT evidence_refs_json GLOB ('*'||char(13)||'*')),
  CHECK(length(CAST(material_ticks_json AS BLOB)) BETWEEN 2 AND 256
    AND substr(material_ticks_json,1,1)='{{'
    AND substr(material_ticks_json,length(material_ticks_json),1)='}}'
    AND NOT material_ticks_json GLOB '* *'
    AND NOT material_ticks_json GLOB ('*'||char(9)||'*')
    AND NOT material_ticks_json GLOB ('*'||char(10)||'*')
    AND NOT material_ticks_json GLOB ('*'||char(13)||'*')),
  CHECK({_ts27("next_review_at")}),
  CHECK({_ts27("attempted_at")}),
  CHECK(length(reason_code) BETWEEN 7 AND 64
    AND NOT reason_code GLOB '*[^a-z_]*' AND reason_code GLOB '*_defer'),
  CHECK(assignment IN
    ('CRITICAL','FULL_REVIEW','SAMPLED_AUDIT','EXPLICITLY_DEFERRED','TERMINAL'))
) STRICT;
CREATE INDEX deferred_attempts_candidate
  ON deferred_attempts(candidate_tick,candidate_id);
CREATE TABLE sample_rotation(
  kind TEXT PRIMARY KEY, cursor_subject_id TEXT, updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE audit_coverage(
  regular_session_date TEXT NOT NULL, kind TEXT NOT NULL,
  eligible_count INTEGER NOT NULL, audited_count INTEGER NOT NULL,
  debt_count INTEGER NOT NULL, oldest_wait_regular_sessions INTEGER NOT NULL,
  capacity_incident_opened INTEGER NOT NULL,
  PRIMARY KEY(regular_session_date,kind)
) STRICT;
CREATE TABLE audit_backlog(
  kind TEXT NOT NULL, subject_id TEXT NOT NULL, first_eligible_session TEXT NOT NULL,
  last_considered_session TEXT NOT NULL, state TEXT NOT NULL,
  PRIMARY KEY(kind,subject_id)
) STRICT;
CREATE TABLE request_day_counters(
  utc_day TEXT PRIMARY KEY CHECK(length(utc_day)=10),
  -- Monotonic UTC-day telemetry only (G-10): no daily aggregate admission cap.
  provider_started_count INTEGER NOT NULL CHECK(provider_started_count >= 0),
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
  CHECK({_ts27("created_at")}), CHECK({_ts27("updated_at")})
) STRICT;
CREATE TABLE runtime_request_reservations(
  runtime_day TEXT NOT NULL, source TEXT NOT NULL, method TEXT NOT NULL,
  host TEXT NOT NULL, normalized_path_template TEXT NOT NULL,
  started_count INTEGER NOT NULL,
  PRIMARY KEY(runtime_day,source,method,host,normalized_path_template)
) STRICT;
CREATE TABLE development_request_reservations(
  stable_phase_id TEXT NOT NULL, source TEXT NOT NULL, method TEXT NOT NULL,
  host TEXT NOT NULL, normalized_path_template TEXT NOT NULL,
  started_count INTEGER NOT NULL,
  PRIMARY KEY(stable_phase_id,source,method,host,normalized_path_template)
) STRICT;
CREATE TABLE endpoint_cooldowns(
  source TEXT NOT NULL, method TEXT NOT NULL, host TEXT NOT NULL,
  normalized_path_template TEXT NOT NULL, next_eligible_at TEXT NOT NULL,
  reason TEXT NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(source,method,host,normalized_path_template)
) STRICT;
CREATE TABLE feedback_inbound(
  feedback_id TEXT PRIMARY KEY, provider TEXT NOT NULL, provider_update_id TEXT NOT NULL,
  provider_message_ref TEXT NOT NULL, sender_ref TEXT NOT NULL, thread_ref TEXT,
  received_at TEXT NOT NULL, raw_text TEXT NOT NULL, context_ref TEXT,
  capture_source TEXT NOT NULL, raw_hash TEXT NOT NULL, event_kind TEXT NOT NULL,
  UNIQUE(provider,provider_update_id,event_kind)
) STRICT;
CREATE TABLE analysis_batches(
  tick_id INTEGER NOT NULL, ordinal INTEGER NOT NULL CHECK(ordinal >= 0),
  provider TEXT NOT NULL CHECK(provider IN ('GLM','SOL')),
  candidate_ids_json TEXT NOT NULL,
  candidate_count INTEGER NOT NULL CHECK(candidate_count >= 1),
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64),
  state TEXT NOT NULL CHECK(state IN ('PLANNED','STARTED','SETTLED','UNKNOWN')),
  exit TEXT CHECK(exit IS NULL OR exit IN
    ('OK','TIMEOUT','CANCELLED','TRUNCATED','OVERSIZE','EMPTY_SUCCESS',
     'PARSE_FAIL','RATE_LIMITED','AUTH_FAIL','TRANSPORT_FAIL')),
  result_json TEXT, result_digest TEXT
    CHECK(result_digest IS NULL OR length(result_digest)=64),
  planned_at TEXT NOT NULL, started_at TEXT, settled_at TEXT,
  planned_invocation_id TEXT NOT NULL, started_invocation_id TEXT,
  PRIMARY KEY(tick_id,ordinal),
  FOREIGN KEY(tick_id) REFERENCES manifest_runs(tick_id),
  CHECK(length(planned_invocation_id)=32
    AND NOT planned_invocation_id GLOB '*[^0-9a-f]*'),
  CHECK(started_invocation_id IS NULL OR
    (length(started_invocation_id)=32
     AND NOT started_invocation_id GLOB '*[^0-9a-f]*')),
  CHECK((started_invocation_id IS NULL)=(state='PLANNED')),
  CHECK((started_at IS NULL)=(state='PLANNED')),
  CHECK((settled_at IS NULL)=(state IN ('PLANNED','STARTED'))),
  CHECK((exit IS NULL)=(state<>'SETTLED')),
  CHECK(result_json IS NULL OR (state='SETTLED' AND exit='OK')),
  CHECK(state<>'SETTLED' OR exit<>'OK' OR result_json IS NOT NULL),
  CHECK(result_json IS NULL OR result_digest IS NOT NULL),
  CHECK(result_digest IS NULL OR result_json IS NOT NULL),
  CHECK((provider='GLM' AND candidate_count<=200)
     OR (provider='SOL' AND candidate_count<=20))
) STRICT;
CREATE INDEX analysis_batches_open ON analysis_batches(state)
  WHERE state IN ('PLANNED','STARTED');
CREATE INDEX analysis_batches_started_run ON analysis_batches(started_invocation_id)
  WHERE state='STARTED';
CREATE TABLE subject_baselines(
  kind TEXT NOT NULL CHECK(kind IN
    ('HOLDINGS_CHANGE','QUOTE_STATE','COMPLETED_EXECUTION','OPERATIONAL_INCIDENT')),
  subject_id TEXT NOT NULL,
  baseline_hash TEXT NOT NULL CHECK(length(baseline_hash)=64),
  baseline_fields_json TEXT NOT NULL, account_scope TEXT,
  source_tick_id INTEGER NOT NULL, updated_at TEXT NOT NULL,
  PRIMARY KEY(kind,subject_id),
  CHECK((account_scope IS NULL)=(kind<>'HOLDINGS_CHANGE')),
  CHECK(account_scope IS NULL OR
    (length(account_scope)=34
     AND substr(account_scope,1,10)='toss-acct-'
     AND NOT substr(account_scope,11) GLOB '*[^0-9a-f]*'))
) STRICT;
CREATE INDEX subject_baselines_scope ON subject_baselines(kind,account_scope);
CREATE TABLE quote_state_history(
  subject_id TEXT NOT NULL,
  tick_id INTEGER NOT NULL CHECK(tick_id BETWEEN 0 AND 2147483647),
  observed_at TEXT NOT NULL,
  last_price_text TEXT NOT NULL,
  PRIMARY KEY(subject_id, tick_id)
) STRICT;
CREATE INDEX quote_state_history_subject ON quote_state_history(subject_id, tick_id DESC);
CREATE TABLE planned_requests(
  tick_id INTEGER NOT NULL CHECK(tick_id BETWEEN 0 AND 2147483647),
  job_scope TEXT NOT NULL CHECK(job_scope IN
    ('CALENDAR','HOLDINGS','QUOTES','TRADES','DAILY_CANDLES')),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 11),
  operation TEXT NOT NULL CHECK(operation IN ({_REQUEST_OPERATIONS_SQL})),
  params_digest TEXT NOT NULL CHECK(length(params_digest)=64
    AND NOT params_digest GLOB '*[^0-9a-f]*'),
  market TEXT CHECK(market IS NULL OR market IN ('KR','US')),
  local_trade_date TEXT CHECK(local_trade_date IS NULL OR
    (length(local_trade_date)=10 AND local_trade_date GLOB
     '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]')),
  method TEXT NOT NULL CHECK(method IN ('GET','POST')),
  host TEXT NOT NULL CHECK(host={PROVIDER_HOST!r}),
  normalized_path_template TEXT NOT NULL CHECK(normalized_path_template IN
    ({_REQUEST_PATHS_SQL})),
  state TEXT NOT NULL CHECK(state IN
    ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING',
     'SETTLED','UNKNOWN','CANCELLED_UNSTARTED')),
  account_ordinal INTEGER CHECK(account_ordinal IS NULL OR account_ordinal BETWEEN 1 AND 5),
  planned_at TEXT NOT NULL, armed_at TEXT, started_at TEXT,
  checkpointed_at TEXT, applying_at TEXT, settled_at TEXT,
  planned_invocation_id TEXT NOT NULL CHECK(length(planned_invocation_id)=32
    AND NOT planned_invocation_id GLOB '*[^0-9a-f]*'),
  started_invocation_id TEXT CHECK(started_invocation_id IS NULL OR
    (length(started_invocation_id)=32
     AND NOT started_invocation_id GLOB '*[^0-9a-f]*')),
  exit TEXT CHECK(exit IS NULL OR exit IN
    ('OK','EMPTY_OK','PROVIDER_ERROR','PARSE_FAIL','RATE_LIMITED','AUTH_FAIL',
     'REDIRECT','TRANSPORT_FAIL','CEILING_DENIED','EARLY_STOP',
     'UNKNOWN_OUTCOME','STORE_APPLY_FAILED')),
  checkpoint_digest TEXT CHECK(checkpoint_digest IS NULL OR
    (length(checkpoint_digest)=64 AND NOT checkpoint_digest GLOB '*[^0-9a-f]*')),
  PRIMARY KEY(tick_id,job_scope,ordinal),
  CHECK((operation='TOKEN')=(method='POST')),
  CHECK((operation='TOKEN')=(normalized_path_template={REQUEST_OPERATION_SPECS["TOKEN"][1]!r})),
  CHECK((operation='ACCOUNT_HOLDINGS')=(account_ordinal IS NOT NULL)),
  CHECK((operation IN ('CALENDAR_DAY_KR','CALENDAR_DAY_US'))=
        (market IS NOT NULL AND local_trade_date IS NOT NULL)),
  CHECK(operation IN ('CALENDAR_DAY_KR','CALENDAR_DAY_US')
        OR (market IS NULL AND local_trade_date IS NULL)),
  CHECK(operation<>'CALENDAR_DAY_KR' OR market='KR'),
  CHECK(operation<>'CALENDAR_DAY_US' OR market='US'),
  CHECK((started_at IS NULL)=(state IN ('PLANNED','ARMED','CANCELLED_UNSTARTED'))),
  CHECK((started_invocation_id IS NULL)=(state IN ('PLANNED','ARMED','CANCELLED_UNSTARTED'))),
  CHECK(checkpoint_digest IS NULL OR operation='ORDERS_PAGE'),
  CHECK(operation<>'ORDERS_PAGE' OR
    ((checkpoint_digest IS NOT NULL)=(state IN ('CHECKPOINTED','APPLYING')
      OR (state='SETTLED' AND exit IN ('OK','EMPTY_OK','STORE_APPLY_FAILED'))))),
  CHECK((settled_at IS NULL)=(state IN
    ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING'))),
  CHECK((exit IS NULL)=(state IN
    ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING'))),
  CHECK((state='UNKNOWN')=(exit='UNKNOWN_OUTCOME')),
  CHECK({_ts27("planned_at")}),
  CHECK(armed_at IS NULL OR {_ts27("armed_at")}),
  CHECK(started_at IS NULL OR {_ts27("started_at")}),
  CHECK(checkpointed_at IS NULL OR {_ts27("checkpointed_at")}),
  CHECK(applying_at IS NULL OR {_ts27("applying_at")}),
  CHECK(settled_at IS NULL OR {_ts27("settled_at")})
) STRICT;
CREATE INDEX planned_requests_open ON planned_requests(state)
 WHERE state IN ('PLANNED','ARMED','STARTED','CHECKPOINTED','APPLYING');
CREATE TABLE planned_request_pages(
  tick_id INTEGER NOT NULL,
  job_scope TEXT NOT NULL CHECK(job_scope='TRADES'),
  ordinal INTEGER NOT NULL CHECK(ordinal BETWEEN 0 AND 9),
  schema_version INTEGER NOT NULL CHECK(schema_version=1),
  account_scope TEXT NOT NULL CHECK(length(account_scope)=34
    AND substr(account_scope,1,10)='toss-acct-'
    AND NOT substr(account_scope,11) GLOB '*[^0-9a-f]*'),
  window_from TEXT NOT NULL CHECK(length(window_from)=10),
  window_to TEXT NOT NULL CHECK(length(window_to)=10),
  request_cursor TEXT,
  orders_json TEXT NOT NULL CHECK(length(CAST(orders_json AS BLOB)) BETWEEN 2 AND 2097152),
  has_next INTEGER NOT NULL CHECK(has_next IN (0,1)),
  next_cursor TEXT,
  page_digest TEXT NOT NULL CHECK(length(page_digest)=64
    AND NOT page_digest GLOB '*[^0-9a-f]*'),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  PRIMARY KEY(tick_id,job_scope,ordinal),
  FOREIGN KEY(tick_id,job_scope,ordinal)
    REFERENCES planned_requests(tick_id,job_scope,ordinal),
  CHECK((has_next=0 AND next_cursor IS NULL)
     OR (has_next=1 AND next_cursor IS NOT NULL AND length(next_cursor) BETWEEN 1 AND 4096)),
  CHECK(next_cursor IS NULL OR request_cursor IS NULL OR next_cursor<>request_cursor)
) STRICT;
CREATE TABLE calendar_request_counters(
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  local_trade_date TEXT NOT NULL CHECK(length(local_trade_date)=10 AND
    local_trade_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  successful_count INTEGER NOT NULL CHECK(successful_count=1),
  first_tick_id INTEGER NOT NULL CHECK(first_tick_id BETWEEN 0 AND 2147483647),
  updated_at TEXT NOT NULL CHECK({_ts27("updated_at")}),
  PRIMARY KEY(market,local_trade_date)
) STRICT;
"""
# Cadence admission authority (SDS §4/§7.3): these tables live in the SAME
# SQLite database and transactional authority as production planned requests.
# There is no separate cadence admission store or ledger.
_CADENCE_SCHEMA_SQL = f"""
CREATE TABLE cadence_ticks(
  policy_version TEXT NOT NULL CHECK(length(policy_version) BETWEEN 1 AND 64),
  scheduled_for_utc TEXT NOT NULL CHECK({_ts27("scheduled_for_utc")}),
  policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64
    AND NOT policy_digest GLOB '*[^0-9a-f]*'),
  snapshot_json TEXT NOT NULL
    CHECK(length(CAST(snapshot_json AS BLOB)) BETWEEN 2 AND 65536),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  PRIMARY KEY(policy_version,scheduled_for_utc)
) STRICT;
CREATE TABLE cadence_wire_starts(
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  operation_group TEXT NOT NULL CHECK(operation_group IN
    ({_CADENCE_WIRE_GROUPS_SQL})),
  operation TEXT NOT NULL CHECK(operation IN ({_V15_CADENCE_WIRE_OPERATIONS_SQL})),
  native_identity TEXT NOT NULL CHECK(length(native_identity) BETWEEN 1 AND 128),
  state TEXT NOT NULL CHECK(state IN
    ('PLANNED','STARTED','SUCCEEDED','FAILED','UNKNOWN')),
  planned_at TEXT NOT NULL CHECK({_ts27("planned_at")}),
  started_at TEXT CHECK(started_at IS NULL OR {_ts27("started_at")}),
  settled_at TEXT CHECK(settled_at IS NULL OR {_ts27("settled_at")}),
  PRIMARY KEY(policy_version,scheduled_for_utc,native_identity),
  FOREIGN KEY(policy_version,scheduled_for_utc)
    REFERENCES cadence_ticks(policy_version,scheduled_for_utc),
  CHECK((started_at IS NULL)=(state='PLANNED')),
  CHECK((settled_at IS NULL)=(state IN ('PLANNED','STARTED')))
) STRICT;
CREATE INDEX cadence_wire_starts_open ON cadence_wire_starts(state)
 WHERE state IN ('PLANNED','STARTED');
CREATE TABLE cadence_calendar_slots(
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  market_local_date TEXT NOT NULL CHECK(length(market_local_date)=10 AND
    market_local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  eligible_slot_utc TEXT NOT NULL CHECK({_ts27("eligible_slot_utc")}),
  state TEXT NOT NULL CHECK(state IN ('PLANNED','STARTED')),
  attempt_ordinal INTEGER CHECK(attempt_ordinal IS NULL
    OR attempt_ordinal BETWEEN 0 AND 5),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  PRIMARY KEY(market,market_local_date,eligible_slot_utc),
  CHECK((attempt_ordinal IS NULL)=(state='PLANNED'))
) STRICT;
CREATE TABLE cadence_calendar_attempts(
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  market_local_date TEXT NOT NULL CHECK(length(market_local_date)=10 AND
    market_local_date GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]'),
  attempt_ordinal INTEGER NOT NULL CHECK(attempt_ordinal BETWEEN 0 AND 5),
  state TEXT NOT NULL CHECK(state IN ('STARTED','FAILED','UNKNOWN','SUCCEEDED')),
  eligible_slot_utc TEXT NOT NULL CHECK({_ts27("eligible_slot_utc")}),
  policy_version TEXT NOT NULL,
  scheduled_for_utc TEXT NOT NULL,
  started_at TEXT NOT NULL CHECK({_ts27("started_at")}),
  settled_at TEXT CHECK(settled_at IS NULL OR {_ts27("settled_at")}),
  next_eligible_at TEXT CHECK(next_eligible_at IS NULL OR {_ts27("next_eligible_at")}),
  -- One-time receipt authority (B1): the provider claim and the cache-commit
  -- consumption are durable DB facts, never in-memory object identity.
  provider_claimed_at TEXT CHECK(provider_claimed_at IS NULL
    OR {_ts27("provider_claimed_at")}),
  cache_committed_at TEXT CHECK(cache_committed_at IS NULL
    OR {_ts27("cache_committed_at")}),
  PRIMARY KEY(market,market_local_date,attempt_ordinal),
  FOREIGN KEY(policy_version,scheduled_for_utc)
    REFERENCES cadence_ticks(policy_version,scheduled_for_utc),
  CHECK((settled_at IS NULL)=(state='STARTED')),
  CHECK(next_eligible_at IS NULL OR state IN ('FAILED','UNKNOWN')),
  CHECK(cache_committed_at IS NULL OR provider_claimed_at IS NOT NULL)
) STRICT;
CREATE INDEX cadence_calendar_attempts_open ON cadence_calendar_attempts(state)
 WHERE state='STARTED';
"""
# v7 analysis tables (unchanged content through v10).
_V7_ANALYSIS_SCHEMA_SQL = f"""
CREATE TABLE analysis_requests(
  request_id TEXT PRIMARY KEY,
  tick_id INTEGER NOT NULL,
  candidate_id TEXT NOT NULL,
  analysis_kind TEXT NOT NULL CHECK(analysis_kind IN ('EVENT','HEALTH_COVERAGE')),
  trigger_kind TEXT NOT NULL CHECK(trigger_kind='DETERMINISTIC_EVENT'),
  delta_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  material_binding_digest TEXT NOT NULL CHECK(length(material_binding_digest)=64
    AND NOT material_binding_digest GLOB '*[^0-9a-f]*'),
  admission_policy_version TEXT NOT NULL,
  admission_policy_digest TEXT NOT NULL CHECK(length(admission_policy_digest)=64
    AND NOT admission_policy_digest GLOB '*[^0-9a-f]*'),
  admission_reason_codes_json TEXT NOT NULL,
  expected_coverage_json TEXT NOT NULL,
  request_digest TEXT NOT NULL CHECK(length(request_digest)=64
    AND NOT request_digest GLOB '*[^0-9a-f]*'),
  state TEXT NOT NULL CHECK(state IN
    ('PENDING','ANALYZING','SUCCEEDED','DEFERRED','FAILED','UNKNOWN')),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  settled_at TEXT CHECK(settled_at IS NULL OR {_ts27("settled_at")}),
  result_id TEXT,
  CHECK((settled_at IS NULL)=(state='PENDING')),
  CHECK((result_id IS NULL)=(state NOT IN ('SUCCEEDED','DEFERRED','FAILED','UNKNOWN'))),
  FOREIGN KEY(tick_id,candidate_id) REFERENCES candidate_manifest(tick_id,candidate_id),
  UNIQUE(tick_id,candidate_id)
) STRICT;
CREATE TABLE analysis_coverage(
  analysis_kind TEXT NOT NULL CHECK(analysis_kind IN ('EVENT','HEALTH_COVERAGE')),
  market TEXT NOT NULL CHECK(market IN ('KR','US')),
  symbol TEXT NOT NULL CHECK(length(symbol) BETWEEN 1 AND 32),
  request_id TEXT NOT NULL,
  result_id TEXT NOT NULL,
  subject_id TEXT NOT NULL,
  evidence_id TEXT NOT NULL,
  material_tick_id INTEGER NOT NULL CHECK(material_tick_id BETWEEN 0 AND 2147483647),
  material_observed_at TEXT NOT NULL CHECK({_ts27("material_observed_at")}),
  analyzed_at TEXT NOT NULL CHECK({_ts27("analyzed_at")}),
  policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64
    AND NOT policy_digest GLOB '*[^0-9a-f]*'),
  PRIMARY KEY(analysis_kind,symbol,request_id),
  FOREIGN KEY(request_id) REFERENCES analysis_requests(request_id)
) STRICT;
CREATE INDEX analysis_coverage_latest
  ON analysis_coverage(analysis_kind,market,symbol,analyzed_at DESC);
"""

# v10 YouTube long-term hypothesis tracking tables (2026-08-16 wiring).
# hypothesis_lifecycle is the append-only M3 evidence: every accepted
# transition keeps its reason_token durably.
_HYPOTHESIS_SCHEMA_BLOCK = f"""
CREATE TABLE hypotheses(
  hypothesis_id TEXT PRIMARY KEY
    CHECK(hypothesis_id GLOB 'hypothesis:[0-9a-z]*'
      AND length(hypothesis_id)=35
      AND NOT substr(hypothesis_id,12) GLOB '*[^0-9a-z]*'),
  identity_digest TEXT NOT NULL UNIQUE
    CHECK(length(identity_digest)=64 AND NOT identity_digest GLOB '*[^0-9a-f]*'),
  state TEXT NOT NULL CHECK(state IN
    ('CANDIDATE','WATCHING','RESOLVED','INVALIDATED','EXPIRED','ARCHIVED')),
  statement TEXT NOT NULL CHECK(length(statement) BETWEEN 1 AND 300),
  trigger_kind TEXT NOT NULL CHECK(trigger_kind IN
    ('THRESHOLD_PERSIST','EVENT_OCCUR','CALENDAR_CHECKPOINT')),
  trigger_expression TEXT NOT NULL CHECK(length(trigger_expression)
    BETWEEN 1 AND 200),
  supporting_evidence_json TEXT NOT NULL
    CHECK(length(CAST(supporting_evidence_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(supporting_evidence_json,1,1)='['),
  counter_evidence_json TEXT NOT NULL
    CHECK(length(CAST(counter_evidence_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(counter_evidence_json,1,1)='['),
  invalidation_conditions_json TEXT NOT NULL
    CHECK(length(CAST(invalidation_conditions_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(invalidation_conditions_json,1,1)='['),
  next_check_at TEXT CHECK(next_check_at IS NULL OR {_ts27("next_check_at")}),
  portfolio_touch TEXT NOT NULL CHECK(portfolio_touch IN
    ('PERSONAL','PENSION','PORTFOLIO_ALL','NONE')),
  origin TEXT NOT NULL CHECK(origin='youtube:12man'),
  provenance_first TEXT NOT NULL,
  superseded_by TEXT CHECK(superseded_by IS NULL OR
    (superseded_by GLOB 'hypothesis:*' AND superseded_by<>hypothesis_id)),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  updated_at TEXT NOT NULL CHECK({_ts27("updated_at")}),
  CHECK(updated_at>=created_at)
) STRICT;
CREATE INDEX hypotheses_open ON hypotheses(state)
  WHERE state IN ('CANDIDATE','WATCHING');
CREATE TABLE hypothesis_sightings(
  sighting_id TEXT PRIMARY KEY
    CHECK(length(sighting_id)=32 AND NOT sighting_id GLOB '*[^0-9a-f]*'),
  hypothesis_id TEXT NOT NULL,
  source_kind TEXT NOT NULL CHECK(source_kind IN ('REPORT','TRANSCRIPT','MATERIAL')),
  source_ref TEXT NOT NULL,
  revision TEXT NOT NULL,
  payload_digest TEXT NOT NULL CHECK(length(payload_digest)=64
    AND NOT payload_digest GLOB '*[^0-9a-f]*'),
  evidence_delta_json TEXT NOT NULL
    CHECK(length(CAST(evidence_delta_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(evidence_delta_json,1,1)='['),
  observed_at TEXT NOT NULL CHECK({_ts27("observed_at")}),
  UNIQUE(hypothesis_id,source_ref,revision,payload_digest),
  FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE INDEX hypothesis_sightings_hypothesis ON hypothesis_sightings(hypothesis_id);
CREATE TABLE hypothesis_index(
  kind TEXT NOT NULL CHECK(kind IN ('METRIC','ENTITY','EFFECT','SOURCE_EVENT')),
  key_value TEXT NOT NULL CHECK(length(key_value) BETWEEN 3 AND 128),
  hypothesis_id TEXT NOT NULL,
  PRIMARY KEY(kind,key_value,hypothesis_id),
  FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE INDEX hypothesis_index_hypothesis ON hypothesis_index(hypothesis_id);
CREATE TABLE hypothesis_lifecycle(
  lifecycle_id TEXT PRIMARY KEY
    CHECK(length(lifecycle_id)=32 AND NOT lifecycle_id GLOB '*[^0-9a-f]*'),
  hypothesis_id TEXT NOT NULL,
  from_state TEXT NOT NULL CHECK(from_state IN
    ('CANDIDATE','WATCHING','RESOLVED','INVALIDATED','EXPIRED','ARCHIVED')),
  to_state TEXT NOT NULL CHECK(to_state IN
    ('CANDIDATE','WATCHING','RESOLVED','INVALIDATED','EXPIRED','ARCHIVED')),
  reason_token TEXT NOT NULL,
  transitioned_at TEXT NOT NULL CHECK({_ts27("transitioned_at")}),
  FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
  UNIQUE(hypothesis_id,from_state,to_state,reason_token)
) STRICT;
CREATE INDEX hypothesis_lifecycle_hypothesis
  ON hypothesis_lifecycle(hypothesis_id);
CREATE TABLE shadow_recall_records(
  shadow_id TEXT PRIMARY KEY
    CHECK(length(shadow_id)=32 AND NOT shadow_id GLOB '*[^0-9a-f]*'),
  hypothesis_id TEXT NOT NULL,
  material_delta_ids_json TEXT NOT NULL
    CHECK(length(CAST(material_delta_ids_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(material_delta_ids_json,1,1)='['),
  matched_keys_json TEXT NOT NULL
    CHECK(length(CAST(matched_keys_json AS BLOB)) BETWEEN 2 AND 4096
    AND substr(matched_keys_json,1,1)='['),
  match_kind TEXT NOT NULL CHECK(match_kind IN ('EXACT','PARTIAL')),
  would_have_sent INTEGER NOT NULL CHECK(would_have_sent=0),
  payload_json TEXT NOT NULL CHECK(length(CAST(payload_json AS BLOB))
    BETWEEN 2 AND 8192),
  recorded_at TEXT NOT NULL CHECK({_ts27("recorded_at")}),
  UNIQUE(hypothesis_id,material_delta_ids_json,matched_keys_json),
  FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE TABLE candidate_failures(
  failure_id TEXT PRIMARY KEY
    CHECK(length(failure_id)=32 AND NOT failure_id GLOB '*[^0-9a-f]*'),
  source_ref TEXT NOT NULL,
  revision TEXT NOT NULL,
  error_kind TEXT NOT NULL CHECK(error_kind IN
    ('missing_fields','bad_field','unarticulated_statement',
     'bad_statement','bad_trigger','bad_keys','duplicate_field',
     'unterminated_block')),
  block_digest TEXT NOT NULL CHECK(length(block_digest)=64
    AND NOT block_digest GLOB '*[^0-9a-f]*'),
  detail_token TEXT,
  recorded_at TEXT NOT NULL CHECK({_ts27("recorded_at")}),
  UNIQUE(source_ref,revision,block_digest,error_kind)
) STRICT;
CREATE INDEX candidate_failures_source ON candidate_failures(source_ref);
"""

# v11 semantic dedupe + weekly review tables (2026-08-16 wiring).
# One relation table + one review/run family (SDS "prefer one table over
# duplicated state"). hypothesis_admission_decisions keeps the per-admission
# audit trail: decision, reason code, compared conflict fields.
_SEMANTIC_SCHEMA_BLOCK = f"""
CREATE TABLE hypothesis_relations(
  relation_id TEXT PRIMARY KEY
    CHECK(length(relation_id)=32 AND NOT relation_id GLOB '*[^0-9a-f]*'),
  hypothesis_a TEXT NOT NULL,
  hypothesis_b TEXT NOT NULL CHECK(hypothesis_b>hypothesis_a),
  relation_kind TEXT NOT NULL CHECK(relation_kind IN
    ('RELATED_BUT_DISTINCT')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  conflicts_json TEXT NOT NULL
    CHECK(length(CAST(conflicts_json AS BLOB)) BETWEEN 2 AND 512),
  overlap REAL NOT NULL CHECK(overlap BETWEEN 0 AND 1),
  origin TEXT NOT NULL CHECK(origin IN ('ADMISSION','WEEKLY_REVIEW')),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  UNIQUE(hypothesis_a,hypothesis_b,relation_kind),
  FOREIGN KEY(hypothesis_a) REFERENCES hypotheses(hypothesis_id),
  FOREIGN KEY(hypothesis_b) REFERENCES hypotheses(hypothesis_id),
  CHECK(hypothesis_a<>hypothesis_b)
) STRICT;
CREATE INDEX hypothesis_relations_a ON hypothesis_relations(hypothesis_a);
CREATE INDEX hypothesis_relations_b ON hypothesis_relations(hypothesis_b);
CREATE TABLE merge_review_queue(
  review_id TEXT PRIMARY KEY
    CHECK(length(review_id)=32 AND NOT review_id GLOB '*[^0-9a-f]*'),
  hypothesis_a TEXT NOT NULL,
  hypothesis_b TEXT NOT NULL CHECK(hypothesis_b>hypothesis_a),
  status TEXT NOT NULL CHECK(status IN ('OPEN','APPROVED','REJECTED','SUPERSEDED')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  origin TEXT NOT NULL CHECK(origin IN ('ADMISSION','WEEKLY_REVIEW')),
  created_at TEXT NOT NULL CHECK({_ts27("created_at")}),
  resolved_at TEXT CHECK(resolved_at IS NULL OR {_ts27("resolved_at")}),
  UNIQUE(hypothesis_a,hypothesis_b,status,origin,created_at),
  FOREIGN KEY(hypothesis_a) REFERENCES hypotheses(hypothesis_id),
  FOREIGN KEY(hypothesis_b) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE INDEX merge_review_queue_open ON merge_review_queue(status)
  WHERE status='OPEN';
CREATE INDEX merge_review_queue_a ON merge_review_queue(hypothesis_a);
CREATE TABLE weekly_review_runs(
  week_identity TEXT PRIMARY KEY
    CHECK(length(week_identity)=8 AND week_identity GLOB '[0-9][0-9][0-9][0-9]-W[0-9][0-9]'),
  status TEXT NOT NULL CHECK(status IN ('RESERVED','COMPLETE')),
  open_count INTEGER NOT NULL CHECK(open_count>=0),
  pairs_scanned INTEGER NOT NULL CHECK(pairs_scanned>=0),
  new_related_edges INTEGER NOT NULL CHECK(new_related_edges>=0),
  new_merge_reviews INTEGER NOT NULL CHECK(new_merge_reviews>=0),
  same_hypothesis_findings INTEGER NOT NULL
    CHECK(same_hypothesis_findings>=0),
  reserved_at TEXT NOT NULL CHECK({_ts27("reserved_at")}),
  completed_at TEXT CHECK(completed_at IS NULL OR {_ts27("completed_at")})
) STRICT;
CREATE TABLE weekly_review_pairs(
  week_identity TEXT NOT NULL
    CHECK(length(week_identity)=8 AND week_identity GLOB '[0-9][0-9][0-9][0-9]-W[0-9][0-9]'),
  hypothesis_a TEXT NOT NULL,
  hypothesis_b TEXT NOT NULL CHECK(hypothesis_b>hypothesis_a),
  classification TEXT NOT NULL CHECK(classification IN
    ('EXACT_DUPLICATE','SAME_HYPOTHESIS_NEW_SIGHTING','RELATED_BUT_DISTINCT',
     'MERGE_REVIEW','NO_RELATION')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  adjudicated_at TEXT NOT NULL CHECK({_ts27("adjudicated_at")}),
  PRIMARY KEY(week_identity,hypothesis_a,hypothesis_b),
  FOREIGN KEY(week_identity) REFERENCES weekly_review_runs(week_identity),
  FOREIGN KEY(hypothesis_a) REFERENCES hypotheses(hypothesis_id),
  FOREIGN KEY(hypothesis_b) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE INDEX weekly_review_pairs_week
  ON weekly_review_pairs(week_identity);
CREATE TABLE hypothesis_admission_decisions(
  decision_id TEXT PRIMARY KEY
    CHECK(length(decision_id)=32 AND NOT decision_id GLOB '*[^0-9a-f]*'),
  candidate_digest TEXT NOT NULL CHECK(length(candidate_digest)=64
    AND NOT candidate_digest GLOB '*[^0-9a-f]*'),
  hypothesis_id TEXT,
  matched_hypothesis_id TEXT,
  classification TEXT NOT NULL CHECK(classification IN
    ('EXACT_DUPLICATE','SAME_HYPOTHESIS_NEW_SIGHTING','RELATED_BUT_DISTINCT',
     'MERGE_REVIEW','NO_RELATION')),
  reason_code TEXT NOT NULL CHECK(length(reason_code) BETWEEN 1 AND 64),
  conflicts_json TEXT NOT NULL
    CHECK(length(CAST(conflicts_json AS BLOB)) BETWEEN 2 AND 512),
  decided_at TEXT NOT NULL CHECK({_ts27("decided_at")}),
  FOREIGN KEY(hypothesis_id) REFERENCES hypotheses(hypothesis_id),
  FOREIGN KEY(matched_hypothesis_id) REFERENCES hypotheses(hypothesis_id)
) STRICT;
CREATE INDEX hypothesis_admission_decisions_hypothesis
  ON hypothesis_admission_decisions(hypothesis_id);
"""


SCHEMA_SQL = (
    _CORE_SCHEMA_SQL
    + _CADENCE_SCHEMA_SQL
    + _V7_ANALYSIS_SCHEMA_SQL
    + _HYPOTHESIS_SCHEMA_BLOCK
    + _SEMANTIC_SCHEMA_BLOCK
    + f"PRAGMA user_version={SCHEMA_VERSION};\n"
)
TABLES = frozenset(
    {
        "manifest_runs",
        "candidate_manifest",
        "dispositions",
        "alert_outbox",
        "incidents",
        "deferred_candidates",
        "sample_rotation",
        "audit_coverage",
        "audit_backlog",
        "request_day_counters",
        "runtime_request_reservations",
        "development_request_reservations",
        "endpoint_cooldowns",
        "feedback_inbound",
        "analysis_batches",
        "subject_baselines",
        "quote_state_history",
        "deferred_attempts",
        "planned_requests",
        "calendar_request_counters",
        "planned_request_pages",
        "cadence_ticks",
        "cadence_wire_starts",
        "cadence_calendar_slots",
        "cadence_calendar_attempts",
        "analysis_requests",
        "analysis_coverage",
        "hypotheses",
        "hypothesis_sightings",
        "hypothesis_index",
        "hypothesis_lifecycle",
        "shadow_recall_records",
        "candidate_failures",
        "hypothesis_relations",
        "merge_review_queue",
        "weekly_review_runs",
        "weekly_review_pairs",
        "hypothesis_admission_decisions",
    }
)
INDEXES = frozenset(
    {
        "alert_outbox_decision_open",
        "alert_outbox_dedupe_open",
        "deferred_attempts_candidate",
        "analysis_batches_open",
        "analysis_batches_started_run",
        "subject_baselines_scope",
        "quote_state_history_subject",
        "planned_requests_open",
        "cadence_wire_starts_open",
        "cadence_calendar_attempts_open",
        "analysis_coverage_latest",
        "hypotheses_open",
        "hypothesis_sightings_hypothesis",
        "hypothesis_index_hypothesis",
        "hypothesis_lifecycle_hypothesis",
        "candidate_failures_source",
        "hypothesis_relations_a",
        "hypothesis_relations_b",
        "merge_review_queue_open",
        "merge_review_queue_a",
        "weekly_review_pairs_week",
        "hypothesis_admission_decisions_hypothesis",
    }
)

# Committed baseline schema (v4): identical core tables except the day-counter
# CHECK still embedded the removed 3,744 aggregate cap, and no cadence tables.
# A valid v4 database is refused by ordinary construction and requires an explicit reviewed offline migration.
_LEGACY_SCHEMA_VERSION_V4 = 4
_LEGACY_SCHEMA_SQL_V4 = (
    _CORE_SCHEMA_SQL.replace(
        "  -- Monotonic UTC-day telemetry only (G-10): no daily aggregate admission cap.\n"
        "  provider_started_count INTEGER NOT NULL CHECK(provider_started_count >= 0),",
        "  provider_started_count INTEGER NOT NULL "
        "CHECK(provider_started_count BETWEEN 0 AND 3744),",
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V4};\n"
)
_CADENCE_TABLES = frozenset(
    {
        "cadence_ticks",
        "cadence_wire_starts",
        "cadence_calendar_slots",
        "cadence_calendar_attempts",
    }
)
# Tables/indexes added by schema v10 (YouTube hypothesis tracking); excluded
# from every earlier legacy table set computed by difference below.
_V10_TABLES = frozenset(
    {
        "hypotheses",
        "hypothesis_sightings",
        "hypothesis_index",
        "hypothesis_lifecycle",
        "shadow_recall_records",
        "candidate_failures",
    }
)
_V10_INDEXES = frozenset(
    {
        "hypotheses_open",
        "hypothesis_sightings_hypothesis",
        "hypothesis_index_hypothesis",
        "hypothesis_lifecycle_hypothesis",
        "candidate_failures_source",
    }
)
# Tables/indexes added by schema v11 (semantic dedupe + weekly review);
# excluded from every earlier legacy table set computed by difference below.
_V11_TABLES = frozenset(
    {
        "hypothesis_relations",
        "merge_review_queue",
        "weekly_review_runs",
        "weekly_review_pairs",
        "hypothesis_admission_decisions",
    }
)
_V11_INDEXES = frozenset(
    {
        "hypothesis_relations_a",
        "hypothesis_relations_b",
        "merge_review_queue_open",
        "merge_review_queue_a",
        "weekly_review_pairs_week",
        "hypothesis_admission_decisions_hypothesis",
    }
)
_LEGACY_TABLES_V4 = (
    TABLES
    - _CADENCE_TABLES
    - {
        "analysis_requests",
        "analysis_coverage",
    }
    - _V10_TABLES
    - _V11_TABLES
)
_LEGACY_INDEXES_V4 = (
    INDEXES
    - {
        "cadence_wire_starts_open",
        "cadence_calendar_attempts_open",
        "analysis_coverage_latest",
    }
    - _V10_INDEXES
    - _V11_INDEXES
)
# Deterministic copy order: exactly the v4 creation order.
_LEGACY_TABLE_ORDER_V4 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V4)
)

# Committed schema v6: identical to v7 minus the analysis_requests and
# analysis_coverage tables and the analysis_coverage_latest index. A valid v6
# database is refused by ordinary construction and requires reviewed migration.
_LEGACY_SCHEMA_VERSION_V6 = 6
_LEGACY_SCHEMA_SQL_V6 = (
    _CORE_SCHEMA_SQL
    + _CADENCE_SCHEMA_SQL
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V6};\n"
)
_V7_TABLES = frozenset({"analysis_requests", "analysis_coverage"})
_V7_INDEXES = frozenset({"analysis_coverage_latest"})
_LEGACY_TABLES_V6 = TABLES - _V7_TABLES - _V10_TABLES - _V11_TABLES
_LEGACY_INDEXES_V6 = INDEXES - _V7_INDEXES - _V10_INDEXES - _V11_INDEXES
_LEGACY_TABLE_ORDER_V6 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V6)
)

# The pre-v10 (canonical) schema SQL: SCHEMA_SQL minus the hypothesis block.
# Canonical v7/v8 legacy schemas derive from this base so they stay exactly
# the committed Market/Quant schemas without any v10 hypothesis tables.
_PRE_V10_SCHEMA_SQL = _CORE_SCHEMA_SQL + _CADENCE_SCHEMA_SQL + _V7_ANALYSIS_SCHEMA_SQL

# Exact committed v8 schema: the Toss request vocabulary still (incorrectly)
# governed cadence-wire rows and MEMBERSHIP was absent from the group CHECK.
_LEGACY_SCHEMA_VERSION_V8 = 8
_LEGACY_SCHEMA_SQL_V8 = (
    _PRE_V10_SCHEMA_SQL.replace(
        f"({_CADENCE_WIRE_GROUPS_SQL})",
        "('TOKEN','CALENDAR','HOLDINGS','QUOTES','TRADES','DAILY_CANDLES')",
    ).replace(
        f"operation IN ({_V15_CADENCE_WIRE_OPERATIONS_SQL})",
        f"operation IN ({_REQUEST_OPERATIONS_SQL})",
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V8};\n"
)
_LEGACY_TABLES_V8 = set(re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V8))
_LEGACY_INDEXES_V8 = frozenset(
    {
        "alert_outbox_decision_open",
        "alert_outbox_dedupe_open",
        "deferred_attempts_candidate",
        "analysis_batches_open",
        "analysis_batches_started_run",
        "subject_baselines_scope",
        "quote_state_history_subject",
        "planned_requests_open",
        "cadence_wire_starts_open",
        "cadence_calendar_attempts_open",
        "analysis_coverage_latest",
    }
)
_LEGACY_TABLE_ORDER_V8 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V8)
)

# Exact committed v7 schema: v8 with the pre-daily-candle CHECKs.
_LEGACY_SCHEMA_VERSION_V7 = 7
_LEGACY_SCHEMA_SQL_V7 = (
    _LEGACY_SCHEMA_SQL_V8.replace(
        "('TOKEN','CALENDAR','HOLDINGS','QUOTES','TRADES','DAILY_CANDLES')",
        "('TOKEN','CALENDAR','HOLDINGS','QUOTES','TRADES')",
    )
    .replace(
        ",'STOCK_CANDLES_DAILY'",
        "",
    )
    .replace(
        ",'DAILY_CANDLES'",
        "",
    )
    .replace(
        f",{REQUEST_OPERATION_SPECS['STOCK_CANDLES_DAILY'][1]!r}",
        "",
    )
    .replace(
        "PRAGMA user_version=8",
        "PRAGMA user_version=7",
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V7};\n"
)
_LEGACY_TABLES_V7 = set(re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V7))
_LEGACY_INDEXES_V7 = frozenset(
    {
        "alert_outbox_decision_open",
        "alert_outbox_dedupe_open",
        "deferred_attempts_candidate",
        "analysis_batches_open",
        "analysis_batches_started_run",
        "subject_baselines_scope",
        "quote_state_history_subject",
        "planned_requests_open",
        "cadence_wire_starts_open",
        "cadence_calendar_attempts_open",
        "analysis_coverage_latest",
    }
)
_LEGACY_TABLE_ORDER_V7 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V7)
)

# Every committed schema from v9 through v12 predates the CONDITIONAL_ORDERS
# / CONDITIONAL_ORDER_HISTORY registration (conditional-order SDS v0.4
# §4.4), so their generated cadence-wire CHECK lists are the
# pre-conditional bytes. v13 (current) carries the widened lists.
_PRE_CONDITIONAL_WIRE_OPERATIONS_SQL = ",".join(
    repr(value) for value in CADENCE_OPERATIONS if value != "CONDITIONAL_ORDERS"
)
_PRE_CONDITIONAL_WIRE_GROUPS_SQL = ",".join(
    repr(value) for value in GROUP_LIMIT_FIELDS if value != "CONDITIONAL_ORDER_HISTORY"
)
_PRE_MEMBERSHIP_TOSS_STOCKS_WIRE_OPERATIONS_SQL = ",".join(
    repr(value) for value in CADENCE_OPERATIONS if value != "MEMBERSHIP_TOSS_STOCKS"
)


def _pre_conditional_wire_checks(sql: str) -> str:
    return sql.replace(
        f"operation IN ({_V15_CADENCE_WIRE_OPERATIONS_SQL})",
        f"operation IN ({_PRE_CONDITIONAL_WIRE_OPERATIONS_SQL})",
    ).replace(
        f"({_CADENCE_WIRE_GROUPS_SQL})",
        f"({_PRE_CONDITIONAL_WIRE_GROUPS_SQL})",
    )


# Exact committed v9 schema (canonical news-revision-model): v10 minus the
# hypothesis-tracking tables/indexes. A valid v9 database is refused by
# ordinary construction and requires reviewed migration.
_LEGACY_SCHEMA_VERSION_V9 = 9
_LEGACY_SCHEMA_SQL_V9 = (
    _pre_conditional_wire_checks(
        _CORE_SCHEMA_SQL + _CADENCE_SCHEMA_SQL + _V7_ANALYSIS_SCHEMA_SQL
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V9};\n"
)
_LEGACY_TABLES_V9 = TABLES - _V10_TABLES - _V11_TABLES
_LEGACY_INDEXES_V9 = INDEXES - _V10_INDEXES - _V11_INDEXES
_LEGACY_TABLE_ORDER_V9 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V9)
)

# Exact committed v11 schema: v12 with the merge-review uniqueness still on
# (pair, status, origin) only — which forced requeue/resolution events for
# the same pair to overwrite history. v12 widens that key with the queueing
# instant so resolution history is append-only.
_LEGACY_SCHEMA_VERSION_V11 = 11
_LEGACY_SCHEMA_SQL_V11 = (
    _pre_conditional_wire_checks(
        _CORE_SCHEMA_SQL
        + _CADENCE_SCHEMA_SQL
        + _V7_ANALYSIS_SCHEMA_SQL
        + _HYPOTHESIS_SCHEMA_BLOCK
        + _SEMANTIC_SCHEMA_BLOCK
    ).replace(
        "UNIQUE(hypothesis_a,hypothesis_b,status,origin,created_at),",
        "UNIQUE(hypothesis_a,hypothesis_b,status,origin),",
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V11};\n"
)
_LEGACY_TABLES_V11 = TABLES
_LEGACY_INDEXES_V11 = INDEXES
_LEGACY_TABLE_ORDER_V11 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V11)
)

# Exact committed v12 schema: identical tables/indexes to v13 — only the
# two generated cadence-wire CHECK lists lack the conditional entries. A
# valid v12 database is refused by ordinary construction and requires an explicit reviewed offline migration.
_LEGACY_SCHEMA_VERSION_V12 = 12
_LEGACY_SCHEMA_SQL_V12 = (
    _pre_conditional_wire_checks(
        _CORE_SCHEMA_SQL
        + _CADENCE_SCHEMA_SQL
        + _V7_ANALYSIS_SCHEMA_SQL
        + _HYPOTHESIS_SCHEMA_BLOCK
        + _SEMANTIC_SCHEMA_BLOCK
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V12};\n"
)
_LEGACY_TABLES_V12 = TABLES
_LEGACY_INDEXES_V12 = INDEXES
_LEGACY_TABLE_ORDER_V12 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V12)
)

# Exact committed v13 schema: all current objects and closed vocabulary except
# MEMBERSHIP_TOSS_STOCKS. The Quant candidate advances user_version rather than
# silently changing this CHECK under the same version.
_LEGACY_SCHEMA_VERSION_V13 = 13
_LEGACY_SCHEMA_SQL_V13 = (
    _CORE_SCHEMA_SQL
    + _CADENCE_SCHEMA_SQL
    + _V7_ANALYSIS_SCHEMA_SQL
    + _HYPOTHESIS_SCHEMA_BLOCK
    + _SEMANTIC_SCHEMA_BLOCK
).replace(
    f"operation IN ({_V15_CADENCE_WIRE_OPERATIONS_SQL})",
    f"operation IN ({_PRE_MEMBERSHIP_TOSS_STOCKS_WIRE_OPERATIONS_SQL})",
) + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V13};\n"
_LEGACY_TABLES_V13 = TABLES
_LEGACY_INDEXES_V13 = INDEXES
_LEGACY_TABLE_ORDER_V13 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V13)
)

# Exact deployed production-v14 schema, read back on 2026-08-30: the same
# object set and CHECK vocabulary as v15, including MEMBERSHIP_TOSS_STOCKS,
# published under user_version=14. This exact deployed drift variant is the
# immutable legacy contract and raises the typed migration-required error.
# The inferred narrowed-v14 source shape is not deployed and fails closed.
_LEGACY_SCHEMA_VERSION_V14 = 14
_LEGACY_SCHEMA_SQL_V14 = (
    _CORE_SCHEMA_SQL
    + _CADENCE_SCHEMA_SQL
    + _V7_ANALYSIS_SCHEMA_SQL
    + _HYPOTHESIS_SCHEMA_BLOCK
    + _SEMANTIC_SCHEMA_BLOCK
) + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V14};\n"
_LEGACY_TABLES_V14 = TABLES
_LEGACY_INDEXES_V14 = INDEXES
_LEGACY_TABLE_ORDER_V14 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V14)
)

# Exact committed v10 schema (canonical YouTube hypothesis tracking): v11
# minus the semantic-dedupe/weekly-review tables and indexes. A valid v10
# database is refused by ordinary construction and requires an explicit reviewed offline migration.
_LEGACY_SCHEMA_VERSION_V10 = 10
_LEGACY_SCHEMA_SQL_V10 = (
    _pre_conditional_wire_checks(
        _CORE_SCHEMA_SQL
        + _CADENCE_SCHEMA_SQL
        + _V7_ANALYSIS_SCHEMA_SQL
        + _HYPOTHESIS_SCHEMA_BLOCK
    )
    + f"PRAGMA user_version={_LEGACY_SCHEMA_VERSION_V10};\n"
)
_LEGACY_TABLES_V10 = TABLES - _V11_TABLES
_LEGACY_INDEXES_V10 = INDEXES - _V11_INDEXES
_LEGACY_TABLE_ORDER_V10 = tuple(
    re.findall(r"CREATE TABLE (\w+)\(", _LEGACY_SCHEMA_SQL_V10)
)

ROLE_METHODS = {
    ProducerRole.COLLECTOR: frozenset(
        {
            "reserve_request",
            "record_cooldown",
            "observe_incident",
            "resolve_incident",
            "plan_requests",
            "arm_account_rows",
            "start_request",
            "settle_request",
            "settle_unstarted_account_rows",
            "checkpoint_orders_page",
            "begin_page_apply",
            "reconcile_requests",
            "prune_request_parity",
            "pin_cadence_tick",
            "plan_cadence_requests",
            "start_cadence_request",
            "settle_cadence_request",
            "reconcile_cadence",
            "plan_calendar_slot",
            "start_calendar_attempt",
            "settle_calendar_attempt",
            "record_pension_reminder_outbox",
        }
    ),
    ProducerRole.ANALYSIS: frozenset(
        {
            "create_manifest",
            "seal_manifest",
            "fail_manifest",
            "create_manifest_with_baselines",
            "plan_analysis_batches",
            "start_analysis_batch",
            "settle_analysis_batch",
            "reconcile_analysis_batches",
            "observe_incident_with_shadow_outbox",
            "resolve_incident_with_shadow_outbox",
            "write_audit_survey",
            "complete_audit",
            "record_disposition_with_outbox",
            "upsert_deferred",
            "terminalize_deferred",
            "upsert_audit_backlog",
            "update_sample_rotation",
            "observe_incident",
            "resolve_incident",
            "reserve_request",
            "record_cooldown",
            "create_analysis_requests_with_manifest",
            "start_analysis_request",
            "settle_analysis_result_with_coverage",
            "reconcile_analysis_requests",
            "read_latest_analysis_coverage",
            "upsert_hypothesis",
            "record_sighting",
            "merge_hypotheses",
            "transition_hypothesis",
            "record_shadow_recall",
            "record_candidate_failure",
            "record_hypothesis_relation",
            "queue_merge_review",
            "record_admission_decision",
            "read_admission_decisions",
            "read_hypothesis_relations",
            "read_merge_reviews",
            "resolve_merge_review",
            "reserve_weekly_review",
            "complete_weekly_review",
            "record_weekly_pair_adjudication",
            "read_weekly_review_summary",
        }
    ),
    ProducerRole.HERMES: frozenset(
        {
            "claim_pending_telegram",
            "mark_sent",
            "mark_failed",
            "mark_unknown",
            "acknowledge_failed",
            "promote_stale_attempting",
            "resolve_unknown",
            "requeue_unknown",
            "resolve_incident",
            "record_incident_outbox",
            "record_news_alert_outbox",
            "record_news_ops_failure_outbox",
            "record_quant_market_close_outbox",
        }
    ),
    ProducerRole.FEEDBACK: frozenset({"append_feedback"}),
}


def _json(value) -> str:
    return canonical_encode(value).decode()


def _decode(value: str):
    decoded = canonical_decode(value.encode(), max_bytes=16 * 1024 * 1024)
    if canonical_encode(decoded).decode() != value:
        raise OpsKernelCorruptError("noncanonical kernel value")
    return decoded


def _field(row, name):
    return row[name] if isinstance(row, Mapping) else row.__getattribute__(name)


def _ticks_json(ticks: Mapping[JobKind, int | None]) -> str:
    return _json({job.value: tick for job, tick in ticks.items()})


def _ticks(value: str) -> dict[JobKind, int | None]:
    return {JobKind(key): tick for key, tick in _decode(value).items()}


class OpsKernelMigrationReceiptV1(NamedTuple):
    """Bounded migration evidence; contains no paths or private values."""

    status: str
    from_version: int
    to_version: int
    table_count: int
    row_count: int
    row_fingerprint: str
    writers_paused_attested: bool
    pre_main_sha256: str
    pre_wal_sha256: str | None
    snapshot_sha256: str
    installed_main_sha256: str


def _file_sha256_for_receipt(path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as reader:
        while True:
            chunk = reader.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


class OpsKernelStore:
    def __init__(
        self,
        path: Path | None = None,
        *,
        stable_phase_ids: frozenset[str] = frozenset(),
    ):
        self.path = (
            (Path.home() / ".local" / "share" / "tokmo-v3").resolve()
            / "ops"
            / "ops_kernel.sqlite3"
            if path is None
            else Path(path)
        )
        self._stable_phase_ids = stable_phase_ids
        try:
            with private_creation():
                self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as error:
            raise OpsKernelCorruptError("unsafe private store") from error
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise OpsKernelCorruptError("unsafe private store")
        if self.path.exists() and os.lstat(self.path).st_nlink != 1:
            raise OpsKernelCorruptError("unsafe private store")
        connection = None
        try:
            if not self.path.exists():
                self._bootstrap()
            else:
                descriptor = os.open(self.path, os.O_RDONLY | os.O_NOFOLLOW)
                try:
                    header = os.read(descriptor, 100)
                finally:
                    os.close(descriptor)
                if len(header) != 100 or not header.startswith(b"SQLite format 3\0"):
                    raise OpsKernelCorruptError("operations kernel unavailable")
                readonly = sqlite3.connect(
                    f"{self.path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                )
                readonly.row_factory = sqlite3.Row
                try:
                    version = readonly.execute("PRAGMA user_version").fetchone()[0]
                    if version == _LEGACY_SCHEMA_VERSION_V4:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V4,
                            version=_LEGACY_SCHEMA_VERSION_V4,
                            tables=_LEGACY_TABLES_V4,
                            indexes=_LEGACY_INDEXES_V4,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V6:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V6,
                            version=_LEGACY_SCHEMA_VERSION_V6,
                            tables=_LEGACY_TABLES_V6,
                            indexes=_LEGACY_INDEXES_V6,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V7:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V7,
                            version=_LEGACY_SCHEMA_VERSION_V7,
                            tables=_LEGACY_TABLES_V7,
                            indexes=_LEGACY_INDEXES_V7,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V8:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V8,
                            version=_LEGACY_SCHEMA_VERSION_V8,
                            tables=_LEGACY_TABLES_V8,
                            indexes=_LEGACY_INDEXES_V8,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V9:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V9,
                            version=_LEGACY_SCHEMA_VERSION_V9,
                            tables=_LEGACY_TABLES_V9,
                            indexes=_LEGACY_INDEXES_V9,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V10:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V10,
                            version=_LEGACY_SCHEMA_VERSION_V10,
                            tables=_LEGACY_TABLES_V10,
                            indexes=_LEGACY_INDEXES_V10,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V11:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V11,
                            version=_LEGACY_SCHEMA_VERSION_V11,
                            tables=_LEGACY_TABLES_V11,
                            indexes=_LEGACY_INDEXES_V11,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V12:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V12,
                            version=_LEGACY_SCHEMA_VERSION_V12,
                            tables=_LEGACY_TABLES_V12,
                            indexes=_LEGACY_INDEXES_V12,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V13:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V13,
                            version=_LEGACY_SCHEMA_VERSION_V13,
                            tables=_LEGACY_TABLES_V13,
                            indexes=_LEGACY_INDEXES_V13,
                        )
                    elif version == _LEGACY_SCHEMA_VERSION_V14:
                        self._validate_schema(
                            readonly,
                            reference_sql=_LEGACY_SCHEMA_SQL_V14,
                            version=_LEGACY_SCHEMA_VERSION_V14,
                            tables=_LEGACY_TABLES_V14,
                            indexes=_LEGACY_INDEXES_V14,
                        )
                    else:
                        self._validate_schema(readonly)
                finally:
                    readonly.close()
                # Plan/SDS 2026-08-30 §4: ordinary constructors never
                # migrate. Every exact supported legacy schema raises the
                # typed OpsKernelMigrationRequiredError without mutation;
                # only the explicit offline v14 -> v15 command migrates.
                if version != SCHEMA_VERSION:
                    raise OpsKernelMigrationRequiredError("migration required")
            connection = self._connect()
            self._validate_schema(connection)
            self._secure_files()
        except OpsKernelCorruptError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise self._map_error(error) from error
        finally:
            if connection is not None:
                connection.close()

    def _bootstrap(self) -> None:
        temporary = self.path.with_name(
            f".{self.path.name}.bootstrap-{os.getpid()}-{uuid.uuid4().hex}"
        )
        connection = None
        published = False
        try:
            with private_creation():
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            os.close(descriptor)
            connection = self._connect_path(temporary, create=True)
            connection.executescript(SCHEMA_SQL)
            self._validate_schema(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            connection.close()
            connection = None
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                os.link(temporary, self.path, follow_symlinks=False)
                published = True
                fsync_directory(self.path.parent)
            except FileExistsError:
                pass
        finally:
            if connection is not None:
                connection.close()
            for candidate in (
                temporary,
                temporary.with_name(temporary.name + "-wal"),
                temporary.with_name(temporary.name + "-shm"),
            ):
                try:
                    candidate.unlink()
                except FileNotFoundError:
                    pass
            if published:
                fsync_directory(self.path.parent)

    @staticmethod
    def _migration_copy_validation_hook(connection, legacy_version):
        """No-op seam used only to inject pre-validation copy faults in tests."""

    @staticmethod
    def _copy_table_fingerprint(connection, schema, table):
        quoted_table = '"' + table.replace('"', '""') + '"'
        columns = list(
            connection.execute(f"PRAGMA {schema}.table_info({quoted_table})")
        )
        if not columns:
            raise OpsKernelCorruptError("copy verification failed")
        names = [row[1] for row in columns]
        primary = [row[1] for row in sorted(columns, key=lambda row: row[5]) if row[5]]
        ordering = primary or names
        select_columns = ",".join('"' + name.replace('"', '""') + '"' for name in names)
        order_columns = ",".join(
            '"' + name.replace('"', '""') + '"' for name in ordering
        )
        encoded_rows = bytearray()
        count = 0
        for row in connection.execute(
            f"SELECT {select_columns} FROM {schema}.{quoted_table} "
            f"ORDER BY {order_columns}"
        ):
            encoded = []
            for value in row:
                if isinstance(value, bytes):
                    encoded.append({"type": "bytes", "value": value.hex()})
                elif isinstance(value, float):
                    # REAL columns (e.g. hypothesis_relations.overlap) reach
                    # this fingerprint: canonical_encode rejects float by
                    # design, and equality is all the fingerprint needs, so
                    # encode the exact bits losslessly via float.hex().
                    encoded.append({"type": "float", "value": value.hex()})
                else:
                    encoded.append(
                        {
                            "type": type(value).__name__,
                            "value": value,
                        }
                    )
            encoded_rows.extend(canonical_encode(encoded))
            encoded_rows.extend(b"\n")
            count += 1
        return count, _sha256(bytes(encoded_rows))

    def _verify_migration_copy(self, connection, tables, legacy_version):
        self._migration_copy_validation_hook(connection, legacy_version)
        if connection.execute("PRAGMA main.quick_check").fetchone()[0] != "ok":
            raise OpsKernelCorruptError("copy verification failed")
        if connection.execute("PRAGMA legacy.quick_check").fetchone()[0] != "ok":
            raise OpsKernelCorruptError("copy verification failed")
        for table in tables:
            if self._copy_table_fingerprint(
                connection, "main", table
            ) != self._copy_table_fingerprint(
                connection,
                "legacy",
                table,
            ):
                raise OpsKernelCorruptError("copy verification failed")

    @staticmethod
    def migrate_v14_to_v15(path, *, snapshot_path, writers_paused: bool):
        """Explicit, locked, snapshot-backed offline v14 -> v15 migration.

        Plan/SDS 2026-08-30 §4/§5: never called by ordinary construction.
        Under one exclusive ``.migrate.lock`` the exact deployed-v14 source
        is verified, byte-snapshotted for rollback, copied into a fresh
        candidate v15 database, fingerprint-verified table by table,
        fsynced, and atomically replaced into place; the installed result is
        read back and validated before success is reported. Any fault before
        the atomic replace leaves the complete original v14 file (and its
        WAL) untouched; a fault after the replace reports an uncertain
        outcome and retains the snapshot for verified rollback.
        """
        path = Path(path)
        snapshot_path = Path(snapshot_path)
        if not writers_paused:
            raise OpsKernelCorruptError("writers-paused attestation required")
        # --- safety pre-checks (zero writes before the lock) -------------
        if not path.is_absolute() or not snapshot_path.is_absolute():
            raise OpsKernelCorruptError("absolute paths required")
        if not path.exists() or path.is_symlink() or path.is_dir():
            raise OpsKernelCorruptError("unsafe private store")
        value = os.lstat(path)
        if (
            not stat.S_ISREG(value.st_mode)
            or value.st_nlink != 1
            or stat.S_IMODE(value.st_mode) != 0o600
        ):
            raise OpsKernelCorruptError("unsafe private store")
        # M3: migration is a root-privileged operation; accept the exact
        # current owner only, refusing e.g. root-owned artifacts.
        if value.st_uid != os.getuid():
            raise OpsKernelCorruptError("unsafe private owner")
        if not snapshot_path.parent.is_dir():
            raise OpsKernelCorruptError("unsafe private store")
        # M2: a rollback journal violates the WAL-mode writer contract and
        # is an immediate pre-lock refusal (never auto-checkpointed).
        if path.with_name(path.name + "-journal").exists():
            raise OpsKernelCorruptError("journal sidecar present")
        if snapshot_path.exists() or snapshot_path.is_symlink():
            # An already-v15 replay must report its specific reason even when
            # the operator reused the previous snapshot path (Matrix 12).
            probe = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
            )
            try:
                if probe.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION:
                    raise OpsKernelCorruptError("already-v15-no-receipt")
            except sqlite3.Error as error:
                raise OpsKernelCorruptError("operations kernel unavailable") from error
            finally:
                probe.close()
            raise OpsKernelCorruptError("snapshot target exists")
        receipt_target = snapshot_path.with_name(
            snapshot_path.name + ".receipt.json",
        )
        if receipt_target.exists():
            raise OpsKernelCorruptError("receipt target exists")

        store = OpsKernelStore.__new__(OpsKernelStore)
        store.path = path
        store._stable_phase_ids = frozenset()
        lock_path = path.with_name(path.name + ".migrate.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        temporary = path.with_name(
            f".{path.name}.migrate-v14-{os.getpid()}-{uuid.uuid4().hex}"
        )
        connection = None
        replaced = False
        lock_descriptor = None
        installed_main_sha256 = None
        try:
            with private_creation():
                lock_descriptor = os.open(lock_path, flags, 0o600)
            try:
                fcntl.flock(lock_descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise OpsKernelCorruptError("operations kernel busy") from None
            locked_value = os.lstat(path)
            if (
                (locked_value.st_dev, locked_value.st_ino)
                != (value.st_dev, value.st_ino)
                or not stat.S_ISREG(locked_value.st_mode)
                or locked_value.st_nlink != 1
                or stat.S_IMODE(locked_value.st_mode) != 0o600
                or locked_value.st_uid != os.getuid()
            ):
                raise OpsKernelCorruptError("source identity changed under lock")
            # --- verify the exact deployed-v14 source (read-only) --------
            recheck = sqlite3.connect(
                f"{path.resolve().as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
            )
            recheck.row_factory = sqlite3.Row
            try:
                version = recheck.execute("PRAGMA user_version").fetchone()[0]
                if version == SCHEMA_VERSION:
                    raise OpsKernelCorruptError("already-v15-no-receipt")
                if version != _LEGACY_SCHEMA_VERSION_V14:
                    raise OpsKernelCorruptError("schema mismatch")
                store._validate_schema(
                    recheck,
                    reference_sql=_LEGACY_SCHEMA_SQL_V14,
                    version=_LEGACY_SCHEMA_VERSION_V14,
                    tables=_LEGACY_TABLES_V14,
                    indexes=_LEGACY_INDEXES_V14,
                )
            finally:
                recheck.close()
            # M1: fingerprint the exact pre-migration source bytes before
            # any snapshot or candidate work changes durable state.
            pre_main_sha256 = _file_sha256_for_receipt(path)
            pre_wal_path = path.with_name(path.name + "-wal")
            pre_wal_sha256 = (
                _file_sha256_for_receipt(pre_wal_path)
                if pre_wal_path.exists()
                else None
            )
            # --- exact DB/main/WAL/SHM snapshot for rollback -------------
            # M2: build the snapshot with the SQLite backup API so the
            # retained rollback artifact is one consistent, checkpointed
            # image that carries WAL-committed rows (byte-copying a live
            # WAL-mode pair yields torn snapshots).
            snapshot_descriptor = os.open(
                snapshot_path,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
            )
            os.close(snapshot_descriptor)
            source_connection = None
            snapshot_connection = None
            try:
                source_connection = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                )
                try:
                    source_connection.execute("PRAGMA busy_timeout=0")
                    snapshot_connection = sqlite3.connect(
                        snapshot_path,
                        isolation_level=None,
                    )
                    source_connection.backup(snapshot_connection)
                finally:
                    source_connection.close()
                try:
                    if (
                        snapshot_connection.execute(
                            "PRAGMA wal_checkpoint(TRUNCATE)"
                        ).fetchone()
                        is None
                    ):  # pragma: no cover - defensive
                        raise OpsKernelCorruptError("snapshot checkpoint failed")
                    snapshot_connection.execute("PRAGMA journal_mode=DELETE")
                    snapshot_connection.close()
                except BaseException:
                    snapshot_connection.close()
                    raise
                snapshot_descriptor = os.open(
                    snapshot_path,
                    os.O_RDONLY | os.O_NOFOLLOW,
                )
                try:
                    os.fsync(snapshot_descriptor)
                finally:
                    os.close(snapshot_descriptor)
            except BaseException:
                if snapshot_connection is not None:
                    snapshot_connection.close()
                snapshot_path.unlink(missing_ok=True)
                raise
            for suffix in ("-wal", "-shm", "-journal"):
                snapshot_path.with_name(snapshot_path.name + suffix).unlink(
                    missing_ok=True,
                )
            fsync_directory(snapshot_path.parent)
            snapshot_sha256 = _file_sha256_for_receipt(snapshot_path)
            snapshot_probe = sqlite3.connect(
                f"{snapshot_path.resolve().as_uri()}?mode=ro",
                uri=True,
                isolation_level=None,
            )
            snapshot_probe.row_factory = sqlite3.Row
            try:
                if (
                    snapshot_probe.execute("PRAGMA integrity_check").fetchone()[0]
                    != "ok"
                ):
                    raise OpsKernelCorruptError("snapshot integrity check failed")
                if (
                    snapshot_probe.execute("PRAGMA foreign_key_check").fetchone()
                    is not None
                ):
                    raise OpsKernelCorruptError("snapshot foreign key check failed")
                store._validate_schema(
                    snapshot_probe,
                    reference_sql=_LEGACY_SCHEMA_SQL_V14,
                    version=_LEGACY_SCHEMA_VERSION_V14,
                    tables=_LEGACY_TABLES_V14,
                    indexes=_LEGACY_INDEXES_V14,
                )
            finally:
                snapshot_probe.close()
            # --- candidate v15 copy --------------------------------------
            with private_creation():
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                )
            os.close(descriptor)
            connection = store._connect_path(temporary, create=True)
            connection.executescript(SCHEMA_SQL)
            connection.execute("PRAGMA foreign_keys=OFF")
            # M7: attach the verified snapshot read-only so a candidate-side
            # fault can never write through the rollback artifact.
            connection.execute(
                "ATTACH DATABASE ? AS legacy",
                (f"file:{snapshot_path.resolve()}?mode=ro",),
            )
            connection.execute("BEGIN IMMEDIATE")
            for table in _LEGACY_TABLE_ORDER_V14:
                connection.execute(
                    f"INSERT INTO main.{table} SELECT * FROM legacy.{table}"
                )
            connection.execute("COMMIT")
            store._verify_migration_copy(
                connection,
                _LEGACY_TABLE_ORDER_V14,
                _LEGACY_SCHEMA_VERSION_V14,
            )
            connection.execute("DETACH DATABASE legacy")
            connection.execute("PRAGMA foreign_keys=ON")
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise OpsKernelCorruptError("operations kernel unavailable")
            store._validate_schema(connection)
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            # M1: total rows and a content fingerprint of the installed copy.
            row_count = 0
            fingerprint = hashlib.sha256()
            for table in sorted(TABLES):
                count = connection.execute(
                    f'SELECT count(*) FROM "{table}"'
                ).fetchone()[0]
                row_count += count
                fingerprint.update(table.encode())
                fingerprint.update(f":{count}:".encode())
            connection.close()
            connection = None
            # m4: verify the candidate's hash and size before replace.
            candidate_size = os.lstat(temporary).st_size
            candidate_sha256 = _file_sha256_for_receipt(temporary)
            if candidate_size <= 0:
                raise OpsKernelCorruptError("candidate verification failed")
            descriptor = os.open(temporary, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            os.replace(temporary, path)
            replaced = True
            try:
                # Every operation after replace is an uncertain-outcome region.
                for suffix in ("-wal", "-shm"):
                    temporary.with_name(temporary.name + suffix).unlink(
                        missing_ok=True,
                    )
                for suffix in ("-wal", "-shm"):
                    path.with_name(path.name + suffix).unlink(missing_ok=True)
                fsync_directory(path.parent)
                installed_main_sha256 = _file_sha256_for_receipt(path)
                if installed_main_sha256 != candidate_sha256:
                    raise OpsKernelCorruptError("installed hash mismatch")
                readback = sqlite3.connect(
                    f"{path.resolve().as_uri()}?mode=ro",
                    uri=True,
                    isolation_level=None,
                )
                readback.row_factory = sqlite3.Row
                try:
                    if readback.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                        raise OpsKernelCorruptError("installed integrity check failed")
                    if (
                        readback.execute("PRAGMA foreign_key_check").fetchone()
                        is not None
                    ):
                        raise OpsKernelCorruptError(
                            "installed foreign key check failed"
                        )
                    store._validate_schema(readback)
                    # M7: re-verify the full row fingerprints installed-side.
                    for table in _LEGACY_TABLE_ORDER_V14:
                        installed = store._copy_table_fingerprint(
                            readback,
                            "main",
                            table,
                        )
                        snapshot_probe = sqlite3.connect(
                            f"{snapshot_path.resolve().as_uri()}?mode=ro",
                            uri=True,
                            isolation_level=None,
                        )
                        try:
                            source_fp = store._copy_table_fingerprint(
                                snapshot_probe,
                                "main",
                                table,
                            )
                        finally:
                            snapshot_probe.close()
                        if installed != source_fp:
                            raise OpsKernelCorruptError("copy verification failed")
                finally:
                    readback.close()
            except (OpsKernelCorruptError, sqlite3.Error, OSError, ValueError):
                # The v15 replacement already landed on disk; outcome is
                # uncertain, not failed — keep the snapshot for rollback.
                raise OpsKernelCorruptError(
                    "migration outcome uncertain; snapshot retained for rollback"
                ) from None
        except OpsKernelCorruptError:
            raise
        except (sqlite3.Error, OSError, ValueError) as error:
            raise store._map_error(error) from error
        finally:
            if connection is not None:
                connection.close()
            if not replaced:
                for candidate in (
                    temporary,
                    temporary.with_name(temporary.name + "-wal"),
                    temporary.with_name(temporary.name + "-shm"),
                ):
                    candidate.unlink(missing_ok=True)
                for suffix in ("-wal", "-shm"):
                    sidecar = snapshot_path.with_name(snapshot_path.name + suffix)
                    sidecar.unlink(missing_ok=True)
            # m1: the lock is always opened before any in-try failure, so a
            # descriptor is guaranteed here; do not mask unlock failures.
            if lock_descriptor is not None:
                fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                os.close(lock_descriptor)
        receipt = OpsKernelMigrationReceiptV1(
            status="MIGRATED",
            from_version=_LEGACY_SCHEMA_VERSION_V14,
            to_version=SCHEMA_VERSION,
            table_count=len(TABLES),
            row_count=row_count,
            row_fingerprint=fingerprint.hexdigest(),
            writers_paused_attested=True,
            pre_main_sha256=pre_main_sha256,
            pre_wal_sha256=pre_wal_sha256,
            snapshot_sha256=snapshot_sha256,
            installed_main_sha256=installed_main_sha256,
        )
        return receipt

    def close(self) -> None:
        """Connections are operation-scoped; retained for explicit lifecycle symmetry."""

    def _connect(self) -> sqlite3.Connection:
        return self._connect_path(self.path, create=False)

    def _connect_path(self, path: Path, *, create: bool) -> sqlite3.Connection:
        with private_creation():
            uri = f"{path.resolve().as_uri()}?mode={'rwc' if create else 'rw'}"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=BUSY_TIMEOUT_MS / 1000,
                isolation_level=None,
            )
            try:
                os.chmod(path, 0o600)
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA busy_timeout=250")
                connection.execute("PRAGMA foreign_keys=ON")
                connection.execute("PRAGMA trusted_schema=OFF")
                if (
                    connection.execute("PRAGMA journal_mode=WAL").fetchone()[0].lower()
                    != "wal"
                ):
                    raise OpsKernelCorruptError("journal mode mismatch")
                connection.execute("PRAGMA synchronous=FULL")
                if connection.execute("PRAGMA synchronous").fetchone()[0] != 2:
                    raise OpsKernelCorruptError("synchronous mismatch")
                self._secure_path_files(path)
                return connection
            except BaseException:
                connection.close()
                raise

    def _secure_files(self):
        self._secure_path_files(self.path)

    @staticmethod
    def _secure_path_files(path):
        if path.exists():
            value = os.lstat(path)
            if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                raise OpsKernelCorruptError("unsafe private store")
            os.chmod(path, 0o600)
        for suffix in ("-wal", "-shm"):
            sidecar = path.with_name(path.name + suffix)
            if sidecar.exists():
                value = os.lstat(sidecar)
                if not stat.S_ISREG(value.st_mode):
                    raise OpsKernelCorruptError("unsafe sidecar")
                os.chmod(sidecar, 0o600)

    def _validate_schema(
        self, connection, *, reference_sql=None, version=None, tables=None, indexes=None
    ):
        reference_sql = SCHEMA_SQL if reference_sql is None else reference_sql
        version = SCHEMA_VERSION if version is None else version
        tables = TABLES if tables is None else tables
        indexes = INDEXES if indexes is None else indexes
        if connection.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise OpsKernelCorruptError("integrity check failed")
        if connection.execute("PRAGMA user_version").fetchone()[0] != version:
            raise OpsKernelCorruptError("schema mismatch")
        rows = list(connection.execute("PRAGMA table_list"))
        actual = {
            row["name"]
            for row in rows
            if row["schema"] == "main"
            and row["type"] == "table"
            and not row["name"].startswith("sqlite_")
        }
        if actual != tables or any(
            row["strict"] != 1 for row in rows if row["name"] in tables
        ):
            raise OpsKernelCorruptError("schema mismatch")
        actual_indexes = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
            )
        }
        if actual_indexes != indexes:
            raise OpsKernelCorruptError("schema mismatch")
        reference = sqlite3.connect(":memory:")
        reference.row_factory = sqlite3.Row
        try:
            reference.executescript(reference_sql)
            for table in tables:
                actual_info = [
                    tuple(row)
                    for row in connection.execute(f"PRAGMA table_info({table})")
                ]
                expected_info = [
                    tuple(row)
                    for row in reference.execute(f"PRAGMA table_info({table})")
                ]
                if actual_info != expected_info:
                    raise OpsKernelCorruptError("schema mismatch")
                actual_fk = list(
                    connection.execute(f"PRAGMA foreign_key_list({table})")
                )
                wanted_fk = list(reference.execute(f"PRAGMA foreign_key_list({table})"))
                if [tuple(row) for row in actual_fk] != [
                    tuple(row) for row in wanted_fk
                ]:
                    raise OpsKernelCorruptError("schema mismatch")
            actual_index_sql = {
                row["name"]: " ".join(row["sql"].split())
                for row in connection.execute(
                    "SELECT name,sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
                )
            }
            expected_index_sql = {
                row["name"]: " ".join(row["sql"].split())
                for row in reference.execute(
                    "SELECT name,sql FROM sqlite_schema WHERE type='index' AND sql IS NOT NULL"
                )
            }
            if actual_index_sql != expected_index_sql:
                raise OpsKernelCorruptError("schema mismatch")

            def objects(database):
                return {
                    (
                        row["type"],
                        row["name"],
                        row["tbl_name"],
                        None if row["sql"] is None else " ".join(row["sql"].split()),
                    )
                    for row in database.execute(
                        "SELECT type,name,tbl_name,sql FROM sqlite_schema"
                        " WHERE name NOT LIKE 'sqlite_stat%'"
                    )
                }

            if objects(connection) != objects(reference):
                raise OpsKernelCorruptError("schema mismatch")
        finally:
            reference.close()

    @staticmethod
    def _map_error(error):
        if isinstance(
            error,
            (
                OpsKernelCorruptError,
                OpsKernelConflictError,
                OpsKernelTransitionError,
                OpsKernelRoleError,
                OpsKernelBudgetExceeded,
                OpsKernelCooldownActive,
            ),
        ):
            return error
        if isinstance(error, sqlite3.Error) and sqlite_locked(error):
            return OpsKernelLockedError("operations kernel busy")
        if isinstance(error, sqlite3.IntegrityError):
            return OpsKernelConflictError("durable identity conflict")
        return OpsKernelCorruptError("operations kernel unavailable")

    def _role(self, role, method):
        if not isinstance(role, ProducerRole) or method not in ROLE_METHODS.get(
            role, ()
        ):
            raise OpsKernelRoleError("producer role denied")

    @contextmanager
    def _migration_shared_lock(self):
        """Shared side of the migration coordination lock.

        Every normal transaction/read holds this from before the database
        file is opened until after the connection closes, and the explicit
        v14->v15 migration holds the exclusive side across its whole
        snapshot/copy/validate/fsync/replace sequence. So a migration never
        snapshots mid-transaction, an open writer's commit can never land on
        a replaced old inode, and new work opens only the current path after
        any replace.
        """
        lock_path = self.path.with_name(self.path.name + ".migrate.lock")
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with private_creation():
            descriptor = os.open(lock_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_SH)
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)

    @contextmanager
    def _transaction(self):
        with self._migration_shared_lock():
            connection = None
            try:
                connection = self._connect()
                connection.execute("BEGIN IMMEDIATE")
                yield connection
                connection.execute("COMMIT")
            except BaseException as error:
                if connection is not None and connection.in_transaction:
                    connection.execute("ROLLBACK")
                if not isinstance(error, Exception):
                    raise
                if isinstance(error, RuntimeError) and str(error) == "injected":
                    raise
                mapped = self._map_error(error)
                if mapped is error:
                    raise
                raise mapped from error
            finally:
                if connection is not None:
                    connection.close()

    @contextmanager
    def _read(self):
        with self._migration_shared_lock():
            connection = None
            try:
                connection = self._connect()
                yield connection
            except BaseException as error:
                if not isinstance(error, Exception):
                    raise
                mapped = self._map_error(error)
                if mapped is error:
                    raise
                raise mapped from error
            finally:
                if connection is not None:
                    connection.close()

    def create_manifest(
        self,
        role,
        tick_id,
        started_at,
        material_ticks,
        material_binding_digest,
        candidates,
        *,
        inject_crash_at=None,
    ):
        self._role(role, "create_manifest")
        candidates = tuple(candidates)
        if type(tick_id) is not int or tick_id < 0:
            raise ValueError("invalid tick")
        if (
            type(material_binding_digest) is not str
            or len(material_binding_digest) != 64
        ):
            raise ValueError("invalid material binding digest")
        if any(row.state is not CandidateState.PENDING for row in candidates):
            raise OpsKernelTransitionError("candidate initial state must be pending")
        counts = dict(sorted(Counter(row.kind for row in candidates).items()))
        immutable = (
            iso(started_at),
            _ticks_json(material_ticks),
            material_binding_digest,
            _json(counts),
            len(candidates),
            tuple(
                (
                    row.candidate_id,
                    row.subject_id,
                    row.kind,
                    _json(row.evidence_refs),
                    row.assignment.value,
                    row.state.value,
                    iso(row.created_at),
                )
                for row in candidates
            ),
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM manifest_runs WHERE tick_id=?", (tick_id,)
            ).fetchone()
            if existing:
                if existing["state"] != ManifestState.OPEN.value:
                    raise OpsKernelTransitionError("manifest is terminal")
                rows = connection.execute(
                    "SELECT * FROM candidate_manifest WHERE tick_id=? ORDER BY candidate_id",
                    (tick_id,),
                ).fetchall()
                stored = (
                    existing["started_at"],
                    existing["material_ticks_json"],
                    existing["material_binding_digest"],
                    existing["counts_json"],
                    existing["total_count"],
                    tuple(
                        (
                            row["candidate_id"],
                            row["subject_id"],
                            row["kind"],
                            row["evidence_refs_json"],
                            row["assignment"],
                            row["state"],
                            row["created_at"],
                        )
                        for row in rows
                    ),
                )
                requested = immutable[:-1] + (tuple(sorted(immutable[-1])),)
                if stored != requested:
                    raise OpsKernelConflictError("manifest identity conflict")
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO manifest_runs VALUES(?,?,?,?,?,?,'OPEN',NULL,NULL)",
                (tick_id, *immutable[:5]),
            )
            if inject_crash_at == "after_run":
                raise RuntimeError("injected")
            connection.executemany(
                "INSERT INTO candidate_manifest VALUES(?,?,?,?,?,?,?,?)",
                ((tick_id, *row) for row in immutable[-1]),
            )
            return ReplayResult.CREATED

    def create_manifest_with_baselines(
        self,
        role,
        tick_id,
        started_at,
        material_ticks,
        material_binding_digest,
        candidates,
        baseline_advances,
        deferred_writes,
        *,
        history_advances=(),
        analysis_requests=(),
        inject_crash_at=None,
    ):
        self._role(role, "create_manifest_with_baselines")
        candidates = tuple(candidates)
        baselines = tuple(baseline_advances)
        deferred = tuple(deferred_writes)
        history = tuple(history_advances)
        requests = tuple(analysis_requests)
        if type(tick_id) is not int or tick_id < 0:
            raise ValueError("invalid tick")
        if (
            type(material_binding_digest) is not str
            or len(material_binding_digest) != 64
        ):
            raise ValueError("invalid material binding digest")
        if any(row.state is not CandidateState.PENDING for row in candidates):
            raise OpsKernelTransitionError("candidate initial state must be pending")
        counts = dict(sorted(Counter(row.kind for row in candidates).items()))
        manifest_values = (
            iso(started_at),
            _ticks_json(material_ticks),
            material_binding_digest,
            _json(counts),
            len(candidates),
        )
        candidate_values = tuple(
            sorted(
                (
                    row.candidate_id,
                    row.subject_id,
                    row.kind,
                    _json(row.evidence_refs),
                    row.assignment.value,
                    row.state.value,
                    iso(row.created_at),
                )
                for row in candidates
            )
        )
        baseline_values = tuple(
            sorted(
                (
                    _field(row, "kind").value,
                    _field(row, "subject_id"),
                    _field(row, "baseline_hash"),
                    _json(_field(row, "fields")),
                    _field(row, "account_scope"),
                    _field(row, "source_tick_id"),
                    iso(started_at),
                )
                for row in baselines
            )
        )
        history_values = tuple(
            sorted(
                (
                    _field(row, "subject_id"),
                    _field(row, "tick_id"),
                    iso(_field(row, "observed_at")),
                    _field(row, "last_price"),
                )
                for row in history
            )
        )
        request_values = tuple(
            sorted(
                (
                    row.request_id,
                    row.tick_id,
                    row.candidate_id,
                    row.analysis_kind.value,
                    row.trigger_kind.value,
                    row.delta_id,
                    row.subject_id,
                    row.material_binding_digest,
                    row.admission_policy_version,
                    row.admission_policy_digest,
                    _json(list(row.admission_reason_codes)),
                    _json([item.canonical_payload for item in row.expected_coverage]),
                    row.request_digest,
                    "PENDING",
                    iso(row.requested_at),
                    None,
                    None,
                )
                for row in requests
            )
        )
        if any(value[1] != tick_id for value in request_values):
            raise ValueError("request tick mismatch")
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM manifest_runs WHERE tick_id=?", (tick_id,)
            ).fetchone()
            if existing is not None:
                if existing["state"] != "OPEN":
                    raise OpsKernelTransitionError("manifest is terminal")
                stored_candidates = tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT candidate_id,subject_id,kind,evidence_refs_json,assignment,"
                        "state,created_at FROM candidate_manifest WHERE tick_id=? "
                        "ORDER BY candidate_id",
                        (tick_id,),
                    )
                )
                stored_candidates = tuple(
                    (*row[:5], "PENDING", row[6])
                    if row[4] == CandidateAssignment.EXPLICITLY_DEFERRED.value
                    and row[5] == CandidateState.DEFERRED.value
                    else row
                    for row in stored_candidates
                )
                stored_baselines = []
                for value in baseline_values:
                    row = connection.execute(
                        "SELECT * FROM subject_baselines WHERE kind=? AND subject_id=?",
                        value[:2],
                    ).fetchone()
                    if row is None:
                        raise OpsKernelConflictError("baseline replay conflict")
                    stored_baselines.append(tuple(row))
                if (
                    tuple(existing)[1:6] != manifest_values
                    or stored_candidates != candidate_values
                    or tuple(stored_baselines) != baseline_values
                ):
                    raise OpsKernelConflictError("manifest identity conflict")
                for value in history_values:
                    row = connection.execute(
                        "SELECT subject_id,tick_id,observed_at,last_price_text "
                        "FROM quote_state_history WHERE subject_id=? AND tick_id=?",
                        value[:2],
                    ).fetchone()
                    if row is None or tuple(row) != value:
                        raise OpsKernelConflictError("history replay conflict")
                for attempt in deferred:
                    result = self._upsert_deferred(connection, attempt)
                    if result is not ReplayResult.DUPLICATE:
                        raise OpsKernelConflictError("deferred replay conflict")
                stored_requests = tuple(
                    tuple(row)
                    for row in connection.execute(
                        "SELECT * FROM analysis_requests WHERE tick_id=? ORDER BY request_id",
                        (tick_id,),
                    )
                )
                if stored_requests != request_values:
                    raise OpsKernelConflictError("request replay conflict")
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO manifest_runs VALUES(?,?,?,?,?,?,'OPEN',NULL,NULL)",
                (tick_id, *manifest_values),
            )
            connection.executemany(
                "INSERT INTO candidate_manifest VALUES(?,?,?,?,?,?,?,?)",
                ((tick_id, *row) for row in candidate_values),
            )
            connection.executemany(
                "INSERT INTO analysis_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                request_values,
            )
            if inject_crash_at == "after_requests":
                raise RuntimeError("injected")
            if inject_crash_at == "after_manifest":
                raise RuntimeError("injected")
            for value in baseline_values:
                prior = connection.execute(
                    "SELECT source_tick_id FROM subject_baselines WHERE kind=? AND subject_id=?",
                    value[:2],
                ).fetchone()
                if prior is not None and value[5] < prior[0]:
                    raise OpsKernelConflictError("baseline tick regression")
                connection.execute(
                    "INSERT INTO subject_baselines VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(kind,subject_id) DO UPDATE SET "
                    "baseline_hash=excluded.baseline_hash,"
                    "baseline_fields_json=excluded.baseline_fields_json,"
                    "account_scope=excluded.account_scope,"
                    "source_tick_id=excluded.source_tick_id,updated_at=excluded.updated_at",
                    value,
                )
            if inject_crash_at == "after_baselines":
                raise RuntimeError("injected")
            for value in history_values:
                connection.execute(
                    "INSERT INTO quote_state_history VALUES(?,?,?,?)",
                    value,
                )
                connection.execute(
                    "DELETE FROM quote_state_history WHERE subject_id=? "
                    "AND tick_id NOT IN (SELECT tick_id FROM quote_state_history "
                    "WHERE subject_id=? ORDER BY tick_id DESC LIMIT 7)",
                    (value[0], value[0]),
                )
            if inject_crash_at == "after_history":
                raise RuntimeError("injected")
            for attempt in deferred:
                self._upsert_deferred(connection, attempt)
                if attempt.assignment is CandidateAssignment.EXPLICITLY_DEFERRED:
                    connection.execute(
                        "UPDATE candidate_manifest SET state='DEFERRED' "
                        "WHERE tick_id=? AND candidate_id=? AND state='PENDING' "
                        "AND assignment='EXPLICITLY_DEFERRED'",
                        (attempt.candidate_tick, attempt.candidate_id),
                    )
            if inject_crash_at == "after_deferred":
                raise RuntimeError("injected")
            return ReplayResult.CREATED

    def read_quote_state_history(self, subject_ids):
        """subject_id -> tick_id 내림차순 최대 7행. 없는 subject는 빈 튜플."""
        subject_ids = tuple(subject_ids)
        result = {subject_id: () for subject_id in subject_ids}
        if not subject_ids:
            return result
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM quote_state_history WHERE subject_id IN ("
                + ",".join("?" for _ in subject_ids)
                + ") ORDER BY subject_id, tick_id DESC",
                subject_ids,
            ).fetchall()
        for row in rows:
            result[row["subject_id"]] += (
                {
                    "subject_id": row["subject_id"],
                    "tick_id": row["tick_id"],
                    "observed_at": parse_iso(row["observed_at"]),
                    "last_price": row["last_price_text"],
                },
            )
        return result

    def read_subject_baselines(self, kind, *, subject_ids=None, account_scopes=None):
        if account_scopes is not None and kind != "HOLDINGS_CHANGE":
            raise ValueError("account scopes require holdings")
        clauses = ["kind=?"]
        values = [kind]
        if subject_ids is not None:
            subject_ids = tuple(subject_ids)
            if not subject_ids:
                return ()
            clauses.append("subject_id IN (" + ",".join("?" for _ in subject_ids) + ")")
            values.extend(subject_ids)
        if account_scopes is not None:
            account_scopes = tuple(account_scopes)
            if not account_scopes:
                return ()
            clauses.append(
                "account_scope IN (" + ",".join("?" for _ in account_scopes) + ")"
            )
            values.extend(account_scopes)
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM subject_baselines WHERE "
                + " AND ".join(clauses)
                + " ORDER BY subject_id",
                values,
            ).fetchall()
        return tuple(
            {
                "kind": row["kind"],
                "subject_id": row["subject_id"],
                "baseline_hash": row["baseline_hash"],
                "fields": _decode(row["baseline_fields_json"]),
                "account_scope": row["account_scope"],
                "source_tick_id": row["source_tick_id"],
                "updated_at": parse_iso(row["updated_at"]),
            }
            for row in rows
        )

    def read_carryover_candidates(self, before_tick_id):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT c.*,m.state AS manifest_state FROM candidate_manifest c "
                "JOIN manifest_runs m ON m.tick_id=c.tick_id "
                "WHERE c.tick_id<? AND m.state IN ('OPEN','SEALED') "
                "AND c.state='PENDING' ORDER BY c.tick_id,c.candidate_id",
                (before_tick_id,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    @staticmethod
    def _validate_invocation(invocation_id):
        if (
            type(invocation_id) is not str
            or len(invocation_id) != 32
            or any(char not in "0123456789abcdef" for char in invocation_id)
        ):
            raise ValueError("invalid invocation id")

    def plan_requests(self, role, tick_id, job_scope, rows, *, invocation_id, now):
        """LEGACY-ONLY G2 acceptance API: plans historical tick-scoped rows.

        Sealed for bounded G2 acceptance evidence. The production cadence
        surface uses ``plan_cadence_requests``/``start_cadence_request`` with
        the mandatory versioned tick binding instead; architecture tests
        forbid new cadence transports from reaching this method.
        """
        self._role(role, "plan_requests")
        self._validate_invocation(invocation_id)
        planned_at = iso(now)
        rows = tuple(rows)
        if not rows:
            raise OpsKernelConflictError("empty request plan")

        requested = []
        canonical_by_identity = {}
        try:
            for row in rows:
                ordinal = _field(row, "ordinal")
                operation = _field(row, "operation")
                if isinstance(operation, RequestOperation):
                    operation = operation.value
                params_digest = _field(row, "params_digest")
                method = _field(row, "method")
                host = _field(row, "host")
                path = _field(row, "normalized_path_template")
                account_ordinal = _field(row, "account_ordinal")
                market = _field(row, "market")
                local_trade_date = _field(row, "local_trade_date")
                canonical_params = validate_canonical_params(
                    operation, _field(row, "canonical_params")
                )
                spec = (
                    REQUEST_OPERATION_SPECS.get(operation)
                    if type(operation) is str
                    else None
                )
                valid_digest = (
                    type(params_digest) is str
                    and len(params_digest) == 64
                    and all(char in "0123456789abcdef" for char in params_digest)
                )
                if (
                    type(tick_id) is not int
                    or not 0 <= tick_id <= 2147483647
                    or job_scope not in REQUEST_JOB_SCOPES
                    or type(ordinal) is not int
                    or not 0 <= ordinal <= 11
                    or spec is None
                    or (method, path) != spec[:2]
                    or host != PROVIDER_HOST
                    or not valid_digest
                    or ((account_ordinal is not None) != spec[2])
                    or (
                        account_ordinal is not None
                        and (
                            type(account_ordinal) is not int
                            or not 1 <= account_ordinal <= 5
                        )
                    )
                    or canonical_params_digest(operation, canonical_params)
                    != params_digest
                    or (
                        operation == "ACCOUNT_HOLDINGS"
                        and canonical_params["account_ordinal"] != account_ordinal
                    )
                    or (
                        operation == "ACCOUNT_HOLDINGS"
                        and (
                            job_scope != "HOLDINGS"
                            or ordinal != (account_ordinal or 0) + 1
                        )
                    )
                    or (operation != "ACCOUNT_HOLDINGS" and account_ordinal is not None)
                    or (operation == "ORDERS_PAGE" and job_scope != "TRADES")
                    or (
                        job_scope == "TRADES"
                        and operation == "ORDERS_PAGE"
                        and not 0 <= ordinal <= 9
                    )
                    or (
                        job_scope == "TRADES"
                        and operation == "ORDER_DETAIL"
                        and not 10 <= ordinal <= 11
                    )
                    or (
                        operation.startswith("CALENDAR_DAY_")
                        and (
                            market != canonical_params["market"]
                            or local_trade_date != canonical_params["local_trade_date"]
                        )
                    )
                    or (
                        not operation.startswith("CALENDAR_DAY_")
                        and (market is not None or local_trade_date is not None)
                    )
                ):
                    raise OpsKernelConflictError("invalid request plan")
                requested.append(
                    (
                        tick_id,
                        job_scope,
                        ordinal,
                        operation,
                        params_digest,
                        market,
                        local_trade_date,
                        method,
                        host,
                        path,
                        "PLANNED",
                        account_ordinal,
                        planned_at,
                        None,
                        None,
                        None,
                        None,
                        None,
                        invocation_id,
                        None,
                        None,
                        None,
                    )
                )
                canonical_by_identity[(tick_id, job_scope, ordinal)] = canonical_params
        except (KeyError, AttributeError) as error:
            raise OpsKernelConflictError("missing field in request plan") from error
        except (TypeError, ValueError) as error:
            raise OpsKernelConflictError("invalid request plan") from error

        identities = [values[:3] for values in requested]
        if len(set(identities)) != len(identities):
            raise OpsKernelConflictError("duplicate request identity in batch")
        requested = tuple(requested)
        with self._transaction() as connection:
            for values in requested:
                if job_scope != "TRADES" or values[3] != "ORDERS_PAGE":
                    continue
                params = canonical_by_identity[values[:3]]
                prior = connection.execute(
                    "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope=? AND ordinal=?",
                    values[:3],
                ).fetchone()
                requested_identity = values[:5] + values[7:10]
                if (
                    prior is not None
                    and tuple(
                        prior[name]
                        for name in (
                            "tick_id",
                            "job_scope",
                            "ordinal",
                            "operation",
                            "params_digest",
                            "method",
                            "host",
                            "normalized_path_template",
                        )
                    )
                    == requested_identity
                ):
                    # An exact eight-tuple replay is its own row, never a new page admission.
                    continue
                pages = connection.execute(
                    "SELECT p.*,r.state,r.exit,r.checkpoint_digest FROM planned_request_pages p "
                    "JOIN planned_requests r USING(tick_id,job_scope,ordinal) "
                    "WHERE p.tick_id=? AND p.job_scope='TRADES' AND p.account_scope=? "
                    "ORDER BY p.ordinal",
                    (tick_id, params["account_scope"]),
                ).fetchall()
                for page in pages:
                    binding = _checkpoint_binding_digest(
                        schema_version=page["schema_version"],
                        tick_id=page["tick_id"],
                        job_scope=page["job_scope"],
                        ordinal=page["ordinal"],
                        account_scope=page["account_scope"],
                        window_from=page["window_from"],
                        window_to=page["window_to"],
                        request_cursor=page["request_cursor"],
                        page_digest=page["page_digest"],
                        has_next=bool(page["has_next"]),
                        next_cursor=page["next_cursor"],
                    )
                    if (
                        _sha256(page["orders_json"].encode()) != page["page_digest"]
                        or binding != page["checkpoint_digest"]
                    ):
                        raise OpsKernelCorruptError("orders page checkpoint corrupt")
                if params["request_cursor"] is None:
                    if (
                        pages
                        or connection.execute(
                            "SELECT count(*) FROM planned_requests WHERE tick_id=? "
                            "AND job_scope='TRADES' AND operation='ORDERS_PAGE' "
                            "AND params_digest=? AND ordinal<>?",
                            (tick_id, values[4], values[2]),
                        ).fetchone()[0]
                    ):
                        raise OpsKernelConflictError("duplicate orders first page")
                    continue
                if len(pages) >= 2:
                    raise OpsKernelBudgetExceeded("orders page ceiling exceeded")
                parents = [
                    page
                    for page in pages
                    if (
                        page["window_from"] == params["window_from"]
                        and page["window_to"] == params["window_to"]
                        and page["next_cursor"] == params["request_cursor"]
                    )
                ]
                if len(parents) != 1:
                    raise OpsKernelConflictError("orders child parent binding conflict")
                parent = parents[0]
                if parent["has_next"] != 1 or not (
                    parent["state"] == "CHECKPOINTED"
                    or (
                        parent["state"] == "SETTLED"
                        and parent["exit"] in ("OK", "EMPTY_OK")
                    )
                ):
                    raise OpsKernelConflictError("orders child parent not eligible")
                if connection.execute(
                    "SELECT count(*) FROM planned_requests WHERE tick_id=? "
                    "AND job_scope='TRADES' AND operation='ORDERS_PAGE' "
                    "AND params_digest=? AND ordinal<>?",
                    (tick_id, values[4], values[2]),
                ).fetchone()[0]:
                    raise OpsKernelConflictError("orders child cardinality conflict")
            outcome = ReplayResult.DUPLICATE
            for values in requested:
                prior = connection.execute(
                    "SELECT * FROM planned_requests "
                    "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                    values[:3],
                ).fetchone()
                if prior is not None:
                    # Only the immutable eight-field plan is replay identity.
                    stored_identity = tuple(
                        prior[name]
                        for name in (
                            "tick_id",
                            "job_scope",
                            "ordinal",
                            "operation",
                            "params_digest",
                            "method",
                            "host",
                            "normalized_path_template",
                        )
                    )
                    requested_identity = values[:5] + values[7:10]
                    if stored_identity != requested_identity:
                        raise OpsKernelConflictError(
                            "planned request identity conflict"
                        )
                    if (
                        prior["account_ordinal"],
                        prior["market"],
                        prior["local_trade_date"],
                    ) != (
                        values[11],
                        values[5],
                        values[6],
                    ):
                        raise OpsKernelCorruptError(
                            "planned request raw projection corrupt"
                        )
                    continue
                connection.execute(
                    "INSERT INTO planned_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                outcome = ReplayResult.CREATED
            return outcome

    @staticmethod
    def _planned_request(row):
        value = dict(row)
        if value["job_scope"] not in REQUEST_JOB_SCOPES:
            raise OpsKernelCorruptError("planned request raw projection corrupt")
        account = value["operation"] == "ACCOUNT_HOLDINGS"
        if account and (
            value["job_scope"] != "HOLDINGS"
            or type(value["account_ordinal"]) is not int
            or value["ordinal"] != value["account_ordinal"] + 1
        ):
            raise OpsKernelCorruptError("planned request raw projection corrupt")
        calendar = value["operation"] in ("CALENDAR_DAY_KR", "CALENDAR_DAY_US")
        expected_market = value["operation"].rsplit("_", 1)[1] if calendar else None
        if (
            value["market"] is not None or value["local_trade_date"] is not None
        ) != calendar or (calendar and value["market"] != expected_market):
            raise OpsKernelCorruptError("planned request raw projection corrupt")
        if calendar:
            try:
                if (
                    date.fromisoformat(value["local_trade_date"]).isoformat()
                    != value["local_trade_date"]
                ):
                    raise ValueError
            except (TypeError, ValueError):
                raise OpsKernelCorruptError(
                    "planned request raw projection corrupt"
                ) from None
        for name in (
            "planned_at",
            "armed_at",
            "started_at",
            "checkpointed_at",
            "applying_at",
            "settled_at",
        ):
            value[name] = parse_iso(value[name])
        return value

    def read_planned_requests(self, tick_id, job_scope=None):
        with self._read() as connection:
            if job_scope is None:
                rows = connection.execute(
                    "SELECT * FROM planned_requests WHERE tick_id=? "
                    "ORDER BY job_scope,ordinal",
                    (tick_id,),
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope=? "
                    "ORDER BY ordinal",
                    (tick_id, job_scope),
                ).fetchall()
        return tuple(self._planned_request(row) for row in rows)

    @staticmethod
    def _account_rows(connection, tick_id, invocation_id=None):
        rows = connection.execute(
            "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='HOLDINGS' "
            "AND operation='ACCOUNT_HOLDINGS' ORDER BY account_ordinal",
            (tick_id,),
        ).fetchall()
        account_ordinals = tuple(row["account_ordinal"] for row in rows)
        if (
            len(rows) > 5
            or any(
                type(value) is not int or not 1 <= value <= 5
                for value in account_ordinals
            )
            or len(set(account_ordinals)) != len(account_ordinals)
            or any(row["ordinal"] != row["account_ordinal"] + 1 for row in rows)
        ):
            raise OpsKernelCorruptError("account request shape corrupt")
        if len(rows) != 5:
            raise OpsKernelTransitionError("account rows unavailable")
        if invocation_id is not None and any(
            row["planned_invocation_id"] != invocation_id for row in rows
        ):
            raise OpsKernelTransitionError("account request invocation mismatch")
        return rows

    def arm_account_rows(
        self,
        role,
        tick_id,
        account_count,
        *,
        invocation_id,
        now,
    ) -> tuple[dict, ...]:
        """LEGACY-ONLY G2 acceptance API: arms historical account rows."""
        self._role(role, "arm_account_rows")
        self._validate_invocation(invocation_id)
        if type(tick_id) is not int or not 0 <= tick_id <= 2147483647:
            raise OpsKernelConflictError("invalid tick id")
        if type(account_count) is not int or not 1 <= account_count <= 5:
            raise OpsKernelConflictError("invalid account count")
        timestamp = iso(now)
        with self._transaction() as connection:
            rows = self._account_rows(connection, tick_id, invocation_id)
            if any(row["state"] != "PLANNED" for row in rows):
                raise OpsKernelTransitionError("account rows not armable")
            connection.execute(
                "UPDATE planned_requests SET state='ARMED',armed_at=? WHERE tick_id=? "
                "AND job_scope='HOLDINGS' AND operation='ACCOUNT_HOLDINGS' "
                "AND account_ordinal<=?",
                (timestamp, tick_id, account_count),
            )
            connection.execute(
                "UPDATE planned_requests SET state='CANCELLED_UNSTARTED',exit='EARLY_STOP',"
                "settled_at=? WHERE tick_id=? AND job_scope='HOLDINGS' "
                "AND operation='ACCOUNT_HOLDINGS' AND account_ordinal>?",
                (timestamp, tick_id, account_count),
            )
            updated = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='HOLDINGS' "
                "AND operation='ACCOUNT_HOLDINGS' ORDER BY account_ordinal",
                (tick_id,),
            ).fetchall()
            return tuple(self._planned_request(row) for row in updated)

    def settle_unstarted_account_rows(
        self,
        role,
        tick_id,
        *,
        invocation_id,
        now,
    ) -> tuple[dict, ...]:
        self._role(role, "settle_unstarted_account_rows")
        self._validate_invocation(invocation_id)
        if type(tick_id) is not int or not 0 <= tick_id <= 2147483647:
            raise OpsKernelConflictError("invalid tick id")
        timestamp = iso(now)
        with self._transaction() as connection:
            rows = self._account_rows(connection, tick_id)
            open_ordinals = tuple(
                row["account_ordinal"]
                for row in rows
                if row["state"] in ("PLANNED", "ARMED")
            )
            if not open_ordinals:
                return ()
            connection.execute(
                "UPDATE planned_requests SET state='CANCELLED_UNSTARTED',exit='EARLY_STOP',"
                "settled_at=? WHERE tick_id=? AND job_scope='HOLDINGS' "
                "AND operation='ACCOUNT_HOLDINGS' AND state IN ('PLANNED','ARMED')",
                (timestamp, tick_id),
            )
            updated = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='HOLDINGS' "
                "AND operation='ACCOUNT_HOLDINGS' AND account_ordinal IN ("
                + ",".join("?" for _ in open_ordinals)
                + ") ORDER BY account_ordinal",
                (tick_id, *open_ordinals),
            ).fetchall()
            return tuple(self._planned_request(row) for row in updated)

    def start_request(self, role, tick_id, job_scope, ordinal, *, invocation_id, now):
        """LEGACY-ONLY G2 acceptance API: starts a historical planned row.

        Sealed for bounded G2 acceptance evidence only. It carries no
        versioned cadence tick binding or pinned-policy admission, so no new
        cadence transport closure may authorize wire I/O through it; the
        production cadence surface requires an OpsKernel STARTED receipt from
        ``start_cadence_request``/``start_calendar_attempt``.
        """
        self._role(role, "start_request")
        self._validate_invocation(invocation_id)
        timestamp = iso(now)
        runtime_day = now.astimezone(UTC).date().isoformat()
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planned_requests "
                "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (tick_id, job_scope, ordinal),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("planned request unavailable")
            spec = REQUEST_OPERATION_SPECS.get(row["operation"])
            account = row["operation"] == "ACCOUNT_HOLDINGS"
            if account:
                self._account_rows(connection, tick_id)
            if (
                row["state"] != ("ARMED" if account else "PLANNED")
                or spec is None
                or (row["method"], row["normalized_path_template"]) != spec[:2]
                or row["host"] != PROVIDER_HOST
            ):
                raise OpsKernelTransitionError("planned request not startable")

            endpoint = (
                "toss",
                row["method"],
                row["host"],
                row["normalized_path_template"],
            )
            if row["method"] == "GET":
                cooldown = connection.execute(
                    "SELECT next_eligible_at FROM endpoint_cooldowns "
                    "WHERE source=? AND method=? AND host=? "
                    "AND normalized_path_template=?",
                    endpoint,
                ).fetchone()
                if cooldown and parse_iso(cooldown[0]) > now:
                    raise OpsKernelCooldownActive("endpoint cooldown active")

            calendar = row["operation"] in ("CALENDAR_DAY_KR", "CALENDAR_DAY_US")
            if calendar:
                tick_used = connection.execute(
                    "SELECT count(*) FROM planned_requests WHERE tick_id=? AND "
                    "operation IN ('CALENDAR_DAY_KR','CALENDAR_DAY_US') "
                    "AND started_at IS NOT NULL",
                    (tick_id,),
                ).fetchone()[0]
                if tick_used >= TICK_REQUEST_LIMITS_V1.calendar_starts:
                    raise OpsKernelBudgetExceeded("calendar request budget exceeded")
            else:
                connection.execute(
                    "INSERT INTO request_day_counters VALUES(?,1,?,?) "
                    "ON CONFLICT(utc_day) DO UPDATE SET "
                    "provider_started_count=provider_started_count+1,updated_at=excluded.updated_at",
                    (runtime_day, timestamp, timestamp),
                )
            if row["method"] == "GET":
                connection.execute(
                    "INSERT INTO runtime_request_reservations VALUES(?,?,?,?,?,1) "
                    "ON CONFLICT(runtime_day,source,method,host,normalized_path_template) "
                    "DO UPDATE SET started_count=started_count+1",
                    (runtime_day, *endpoint),
                )
            connection.execute(
                "UPDATE planned_requests SET state='STARTED',started_at=?,"
                "started_invocation_id=? WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (timestamp, invocation_id, tick_id, job_scope, ordinal),
            )
            started = connection.execute(
                "SELECT * FROM planned_requests "
                "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (tick_id, job_scope, ordinal),
            ).fetchone()
            return self._planned_request(started)

    def checkpoint_orders_page(
        self,
        role,
        tick_id,
        ordinal,
        *,
        account_scope,
        window_from,
        window_to,
        request_cursor,
        orders_json,
        has_next,
        next_cursor,
        page_digest,
        invocation_id,
        now,
    ) -> dict:
        self._role(role, "checkpoint_orders_page")
        self._validate_invocation(invocation_id)
        timestamp = iso(now)
        try:
            decoded = json.loads(orders_json)
            canonical = json.dumps(
                decoded,
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            )
        except (TypeError, ValueError, json.JSONDecodeError) as error:
            raise OpsKernelConflictError("invalid orders page checkpoint") from error
        valid_digest = (
            type(page_digest) is str
            and len(page_digest) == 64
            and all(character in "0123456789abcdef" for character in page_digest)
        )
        valid_cursor_shape = type(has_next) is bool and (
            (not has_next and next_cursor is None)
            or (
                has_next
                and type(next_cursor) is str
                and 1 <= len(next_cursor) <= 4096
                and next_cursor != request_cursor
            )
        )
        params = {
            "account_scope": account_scope,
            "window_from": window_from,
            "window_to": window_to,
            "request_cursor": request_cursor,
        }
        try:
            params = validate_canonical_params("ORDERS_PAGE", params)
        except (TypeError, ValueError) as error:
            raise OpsKernelConflictError("invalid orders page checkpoint") from error
        if (
            type(tick_id) is not int
            or not 0 <= tick_id <= 2147483647
            or type(ordinal) is not int
            or not 0 <= ordinal <= 9
            or type(orders_json) is not str
            or not isinstance(decoded, list)
            or canonical != orders_json
            or not 2 <= len(orders_json.encode()) <= 2097152
            or not valid_digest
            or _sha256(orders_json.encode()) != page_digest
            or not valid_cursor_shape
        ):
            raise OpsKernelConflictError("invalid orders page checkpoint")
        requested_page = (
            tick_id,
            "TRADES",
            ordinal,
            1,
            account_scope,
            window_from,
            window_to,
            request_cursor,
            orders_json,
            int(has_next),
            next_cursor,
            page_digest,
        )
        checkpoint_digest = _checkpoint_binding_digest(
            schema_version=1,
            tick_id=tick_id,
            job_scope="TRADES",
            ordinal=ordinal,
            account_scope=account_scope,
            window_from=window_from,
            window_to=window_to,
            request_cursor=request_cursor,
            page_digest=page_digest,
            has_next=has_next,
            next_cursor=next_cursor,
        )
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='TRADES' "
                "AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            if row is None or row["operation"] != "ORDERS_PAGE":
                raise OpsKernelTransitionError("orders page request unavailable")
            if row["started_invocation_id"] != invocation_id:
                raise OpsKernelTransitionError(
                    "orders page request invocation mismatch"
                )
            page = connection.execute(
                "SELECT * FROM planned_request_pages WHERE tick_id=? AND job_scope='TRADES' "
                "AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            if row["state"] == "CHECKPOINTED":
                if page is None or tuple(page)[:12] != requested_page:
                    raise OpsKernelConflictError("orders page checkpoint conflict")
                if row["checkpoint_digest"] != checkpoint_digest:
                    raise OpsKernelConflictError("orders page checkpoint conflict")
                return self._planned_request(row)
            if (
                row["state"] != "STARTED"
                or row["started_invocation_id"] != invocation_id
            ):
                raise OpsKernelTransitionError("orders page request cannot checkpoint")
            if page is not None:
                raise OpsKernelCorruptError("orders page checkpoint torn")
            if canonical_params_digest("ORDERS_PAGE", params) != row["params_digest"]:
                raise OpsKernelConflictError("orders page plan binding conflict")
            connection.execute(
                "INSERT INTO planned_request_pages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (*requested_page, timestamp),
            )
            changed = connection.execute(
                "UPDATE planned_requests SET state='CHECKPOINTED',checkpointed_at=?,"
                "checkpoint_digest=? WHERE tick_id=? AND job_scope='TRADES' AND ordinal=? "
                "AND state='STARTED' AND started_invocation_id=?",
                (timestamp, checkpoint_digest, tick_id, ordinal, invocation_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("orders page request cannot checkpoint")
            fresh = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='TRADES' "
                "AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            return self._planned_request(fresh)

    def begin_page_apply(
        self,
        role,
        tick_id,
        ordinal,
        *,
        invocation_id,
        now,
    ) -> dict:
        self._role(role, "begin_page_apply")
        self._validate_invocation(invocation_id)
        timestamp = iso(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='TRADES' "
                "AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            if row is None or row["operation"] != "ORDERS_PAGE":
                raise OpsKernelTransitionError("orders page request unavailable")
            if row["state"] not in ("CHECKPOINTED", "APPLYING"):
                raise OpsKernelTransitionError("orders page request cannot apply")
            page = connection.execute(
                "SELECT * FROM planned_request_pages WHERE tick_id=? AND job_scope='TRADES' "
                "AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            if page is None:
                raise OpsKernelCorruptError("orders page checkpoint missing")
            params = {
                "account_scope": page["account_scope"],
                "window_from": page["window_from"],
                "window_to": page["window_to"],
                "request_cursor": page["request_cursor"],
            }
            try:
                canonical_params = validate_canonical_params("ORDERS_PAGE", params)
            except (TypeError, ValueError) as error:
                raise OpsKernelCorruptError("orders page checkpoint corrupt") from error
            payload = page["orders_json"]
            checkpoint_digest = _checkpoint_binding_digest(
                schema_version=page["schema_version"],
                tick_id=page["tick_id"],
                job_scope=page["job_scope"],
                ordinal=page["ordinal"],
                account_scope=page["account_scope"],
                window_from=page["window_from"],
                window_to=page["window_to"],
                request_cursor=page["request_cursor"],
                page_digest=page["page_digest"],
                has_next=bool(page["has_next"]),
                next_cursor=page["next_cursor"],
            )
            valid_cursor_shape = page["has_next"] in (0, 1) and (
                (page["has_next"] == 0 and page["next_cursor"] is None)
                or (
                    page["has_next"] == 1
                    and type(page["next_cursor"]) is str
                    and 1 <= len(page["next_cursor"]) <= 4096
                    and page["next_cursor"] != page["request_cursor"]
                )
            )
            if (
                page["schema_version"] != 1
                or type(payload) is not str
                or _sha256(payload.encode()) != page["page_digest"]
                or row["checkpoint_digest"] != checkpoint_digest
                or canonical_params_digest("ORDERS_PAGE", canonical_params)
                != row["params_digest"]
                or not valid_cursor_shape
            ):
                raise OpsKernelCorruptError("orders page checkpoint corrupt")
            try:
                decoded = json.loads(payload)
                canonical = json.dumps(
                    decoded, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                )
            except (TypeError, ValueError, json.JSONDecodeError) as error:
                raise OpsKernelCorruptError("orders page checkpoint corrupt") from error
            if not isinstance(decoded, list) or canonical != payload:
                raise OpsKernelCorruptError("orders page checkpoint corrupt")
            if row["state"] == "CHECKPOINTED":
                changed = connection.execute(
                    "UPDATE planned_requests SET state='APPLYING',applying_at=? "
                    "WHERE tick_id=? AND job_scope='TRADES' AND ordinal=? "
                    "AND state='CHECKPOINTED'",
                    (timestamp, tick_id, ordinal),
                ).rowcount
                if changed != 1:
                    raise OpsKernelTransitionError("orders page request cannot apply")
                row = connection.execute(
                    "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope='TRADES' "
                    "AND ordinal=?",
                    (tick_id, ordinal),
                ).fetchone()
            result = self._planned_request(row)
            result["orders_page"] = {
                "schema_version": page["schema_version"],
                "tick_id": page["tick_id"],
                "request_ordinal": page["ordinal"],
                "account_scope": page["account_scope"],
                "window_from": page["window_from"],
                "window_to": page["window_to"],
                "request_cursor": page["request_cursor"],
                "orders_json": payload,
                "has_next": bool(page["has_next"]),
                "next_cursor": page["next_cursor"],
                "page_digest": page["page_digest"],
            }
            return result

    def reconcile_requests(self, role, *, invocation_id, now) -> tuple[dict, ...]:
        self._role(role, "reconcile_requests")
        self._validate_invocation(invocation_id)
        timestamp = iso(now)
        with self._transaction() as connection:
            keys = tuple(
                connection.execute(
                    "SELECT tick_id,job_scope,ordinal FROM planned_requests "
                    "WHERE state='STARTED' AND started_invocation_id<>? "
                    "ORDER BY tick_id,job_scope,ordinal",
                    (invocation_id,),
                ).fetchall()
            )
            connection.execute(
                "UPDATE planned_requests SET state='UNKNOWN',exit='UNKNOWN_OUTCOME',settled_at=? "
                "WHERE state='STARTED' AND started_invocation_id<>?",
                (timestamp, invocation_id),
            )
            return tuple(
                self._planned_request(
                    connection.execute(
                        "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope=? AND ordinal=?",
                        tuple(key),
                    ).fetchone()
                )
                for key in keys
            )

    def prune_request_parity(self, role, *, now) -> dict[str, int]:
        """Delete only unpinned terminal request evidence from completed UTC days."""
        self._role(role, "prune_request_parity")
        iso(now)
        current_day = now.astimezone(UTC).date()
        day_start = (
            int(
                datetime.combine(
                    current_day,
                    datetime.min.time(),
                    tzinfo=UTC,
                ).timestamp()
            )
            // 300
        )
        with self._transaction() as connection:
            if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise OpsKernelCorruptError("planned request foreign key corrupt")
            torn = connection.execute(
                "SELECT r.tick_id,r.ordinal FROM planned_requests r "
                "LEFT JOIN planned_request_pages p USING(tick_id,job_scope,ordinal) "
                "WHERE r.operation='ORDERS_PAGE' "
                "AND r.state IN ('CHECKPOINTED','APPLYING') AND p.tick_id IS NULL LIMIT 1"
            ).fetchone()
            if torn is not None:
                raise OpsKernelCorruptError("orders page checkpoint missing")

            eligible = tuple(
                connection.execute(
                    "SELECT tick_id,job_scope,ordinal FROM planned_requests "
                    "WHERE tick_id<? AND ("
                    "state IN ('UNKNOWN','CANCELLED_UNSTARTED') OR "
                    "(state='SETTLED' AND exit<>'STORE_APPLY_FAILED')) "
                    "ORDER BY tick_id,job_scope,ordinal",
                    (day_start,),
                ).fetchall()
            )
            page_count = 0
            for key in eligible:
                page_count += connection.execute(
                    "DELETE FROM planned_request_pages "
                    "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                    tuple(key),
                ).rowcount
            parent_count = 0
            for key in eligible:
                parent_count += connection.execute(
                    "DELETE FROM planned_requests "
                    "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                    tuple(key),
                ).rowcount
            return {
                "planned_request_pages": page_count,
                "planned_requests": parent_count,
            }

    def settle_request(
        self,
        role,
        tick_id,
        job_scope,
        ordinal,
        exit,
        *,
        invocation_id,
        now,
    ) -> dict | ReplayResult:
        """LEGACY-ONLY G2 acceptance API: settles a historical started row."""
        self._role(role, "settle_request")
        self._validate_invocation(invocation_id)
        exits = {
            "OK",
            "EMPTY_OK",
            "PROVIDER_ERROR",
            "PARSE_FAIL",
            "RATE_LIMITED",
            "AUTH_FAIL",
            "REDIRECT",
            "TRANSPORT_FAIL",
            "CEILING_DENIED",
            "UNKNOWN_OUTCOME",
            "STORE_APPLY_FAILED",
        }
        if exit not in exits:
            raise OpsKernelConflictError("invalid request settlement")
        timestamp = iso(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (tick_id, job_scope, ordinal),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("planned request unavailable")
            calendar = row["operation"] in ("CALENDAR_DAY_KR", "CALENDAR_DAY_US")
            if calendar:
                self._planned_request(row)
            terminal_state = "UNKNOWN" if exit == "UNKNOWN_OUTCOME" else "SETTLED"
            if row["state"] == terminal_state:
                if row["exit"] != exit:
                    raise OpsKernelConflictError("conflicting terminal settlement")
                if (
                    calendar
                    and exit in ("OK", "EMPTY_OK")
                    and connection.execute(
                        "SELECT 1 FROM calendar_request_counters WHERE market=? AND local_trade_date=?",
                        (row["market"], row["local_trade_date"]),
                    ).fetchone()
                    is None
                ):
                    raise OpsKernelCorruptError("calendar settlement counter missing")
                return ReplayResult.DUPLICATE
            applying = row["state"] == "APPLYING"
            if applying:
                if (
                    row["operation"] != "ORDERS_PAGE"
                    or job_scope != "TRADES"
                    or exit not in ("OK", "EMPTY_OK", "STORE_APPLY_FAILED")
                ):
                    raise OpsKernelTransitionError("planned request cannot settle")
            elif (
                row["state"] != "STARTED"
                or row["started_invocation_id"] != invocation_id
                or exit == "STORE_APPLY_FAILED"
            ):
                raise OpsKernelTransitionError("planned request cannot settle")
            if calendar and exit in ("OK", "EMPTY_OK"):
                connection.execute(
                    "INSERT INTO calendar_request_counters VALUES(?,?,1,?,?) "
                    "ON CONFLICT(market,local_trade_date) DO NOTHING",
                    (row["market"], row["local_trade_date"], tick_id, timestamp),
                )
            connection.execute(
                "UPDATE planned_requests SET state=?,exit=?,settled_at=? "
                "WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (terminal_state, exit, timestamp, tick_id, job_scope, ordinal),
            )
            settled = connection.execute(
                "SELECT * FROM planned_requests WHERE tick_id=? AND job_scope=? AND ordinal=?",
                (tick_id, job_scope, ordinal),
            ).fetchone()
            return self._planned_request(settled)

    def plan_analysis_batches(self, role, tick_id, rows, *, invocation_id):
        self._role(role, "plan_analysis_batches")
        self._validate_invocation(invocation_id)
        rows = tuple(rows)
        requested = []
        for row in rows:
            provider = _field(row, "provider")
            candidate_ids = tuple(_field(row, "candidate_ids"))
            requested.append(
                (
                    tick_id,
                    _field(row, "ordinal"),
                    provider,
                    _json(candidate_ids),
                    len(candidate_ids),
                    _field(row, "request_digest"),
                    "PLANNED",
                    None,
                    None,
                    None,
                    iso(_field(row, "planned_at")),
                    None,
                    None,
                    invocation_id,
                    None,
                )
            )
        if Counter(row[2] for row in requested)["GLM"] > 3:
            raise OpsKernelBudgetExceeded("GLM batch cap")
        if Counter(row[2] for row in requested)["SOL"] > 8:
            raise OpsKernelBudgetExceeded("SOL batch cap")
        with self._transaction() as connection:
            existing_counts = dict(
                connection.execute(
                    "SELECT provider,count(*) FROM analysis_batches WHERE tick_id=? "
                    "GROUP BY provider",
                    (tick_id,),
                ).fetchall()
            )
            new_by_provider = Counter()
            outcome = ReplayResult.DUPLICATE
            for values in requested:
                old = connection.execute(
                    "SELECT * FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                    values[:2],
                ).fetchone()
                if old is not None:
                    stored = tuple(old)
                    # Replay identity is the immutable plan, never lifecycle,
                    # caller-clock, result, or invocation ownership fields.
                    comparable_stored = stored[:6]
                    comparable_requested = values[:6]
                    if comparable_stored != comparable_requested:
                        raise OpsKernelConflictError("analysis batch identity conflict")
                    continue
                new_by_provider[values[2]] += 1
                outcome = ReplayResult.CREATED
            if existing_counts.get("GLM", 0) + new_by_provider["GLM"] > 3:
                raise OpsKernelBudgetExceeded("GLM batch cap")
            if existing_counts.get("SOL", 0) + new_by_provider["SOL"] > 8:
                raise OpsKernelBudgetExceeded("SOL batch cap")
            for values in requested:
                if (
                    connection.execute(
                        "SELECT 1 FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                        values[:2],
                    ).fetchone()
                    is None
                ):
                    connection.execute(
                        "INSERT INTO analysis_batches VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        values,
                    )
            return outcome

    @staticmethod
    def _analysis_batch(row):
        return {
            "tick_id": row["tick_id"],
            "ordinal": row["ordinal"],
            "provider": row["provider"],
            "candidate_ids": tuple(_decode(row["candidate_ids_json"])),
            "candidate_count": row["candidate_count"],
            "request_digest": row["request_digest"],
            "state": row["state"],
            "exit": row["exit"],
            "result": None
            if row["result_json"] is None
            else _decode(row["result_json"]),
            "result_digest": row["result_digest"],
            "planned_at": parse_iso(row["planned_at"]),
            "started_at": parse_iso(row["started_at"]),
            "settled_at": parse_iso(row["settled_at"]),
            "planned_invocation_id": row["planned_invocation_id"],
            "started_invocation_id": row["started_invocation_id"],
        }

    def read_analysis_batches(self, tick_id):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM analysis_batches WHERE tick_id=? ORDER BY ordinal",
                (tick_id,),
            ).fetchall()
        return tuple(self._analysis_batch(row) for row in rows)

    def start_analysis_batch(self, role, tick_id, ordinal, *, invocation_id, now):
        self._role(role, "start_analysis_batch")
        self._validate_invocation(invocation_id)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE analysis_batches SET state='STARTED',started_at=?,"
                "started_invocation_id=? WHERE tick_id=? AND ordinal=? AND state='PLANNED'",
                (iso(now), invocation_id, tick_id, ordinal),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("analysis batch cannot start")
            row = connection.execute(
                "SELECT * FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            return self._analysis_batch(row)

    def settle_analysis_batch(
        self,
        role,
        tick_id,
        ordinal,
        provider,
        exit,
        result_json,
        *,
        invocation_id,
        now,
    ):
        self._role(role, "settle_analysis_batch")
        self._validate_invocation(invocation_id)
        exits = {
            "OK",
            "TIMEOUT",
            "CANCELLED",
            "TRUNCATED",
            "OVERSIZE",
            "EMPTY_SUCCESS",
            "PARSE_FAIL",
            "RATE_LIMITED",
            "AUTH_FAIL",
            "TRANSPORT_FAIL",
        }
        if exit not in exits or ((exit == "OK") != (result_json is not None)):
            raise ValueError("invalid provider settlement")
        encoded = None if result_json is None else _json(result_json)
        digest = None if encoded is None else _sha256(encoded.encode())
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            if (
                row is None
                or row["provider"] != provider
                or row["state"] != "STARTED"
                or row["started_invocation_id"] != invocation_id
            ):
                raise OpsKernelTransitionError("analysis batch cannot settle")
            connection.execute(
                "UPDATE analysis_batches SET state='SETTLED',exit=?,result_json=?,"
                "result_digest=?,settled_at=? WHERE tick_id=? AND ordinal=?",
                (exit, encoded, digest, iso(now), tick_id, ordinal),
            )
            fresh = connection.execute(
                "SELECT * FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                (tick_id, ordinal),
            ).fetchone()
            return self._analysis_batch(fresh)

    def reconcile_analysis_batches(self, role, *, invocation_id, now):
        self._role(role, "reconcile_analysis_batches")
        self._validate_invocation(invocation_id)
        with self._transaction() as connection:
            rows = connection.execute(
                "SELECT tick_id,ordinal FROM analysis_batches WHERE state='STARTED' "
                "AND started_invocation_id<>? ORDER BY tick_id,ordinal",
                (invocation_id,),
            ).fetchall()
            connection.execute(
                "UPDATE analysis_batches SET state='UNKNOWN',settled_at=? "
                "WHERE state='STARTED' AND started_invocation_id<>?",
                (iso(now), invocation_id),
            )
            return tuple(
                self._analysis_batch(
                    connection.execute(
                        "SELECT * FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                        tuple(key),
                    ).fetchone()
                )
                for key in rows
            )

    def seal_manifest(self, role, tick_id, sealed_at):
        self._role(role, "seal_manifest")
        with self._transaction() as connection:
            run = connection.execute(
                "SELECT * FROM manifest_runs WHERE tick_id=?", (tick_id,)
            ).fetchone()
            if not run or run["state"] != "OPEN":
                raise OpsKernelTransitionError("manifest cannot seal")
            counts = dict(
                connection.execute(
                    "SELECT kind,count(*) FROM candidate_manifest WHERE tick_id=? GROUP BY kind",
                    (tick_id,),
                ).fetchall()
            )
            total = sum(counts.values())
            if (
                total != run["total_count"]
                or _json(dict(sorted(counts.items()))) != run["counts_json"]
            ):
                raise OpsKernelCorruptError("manifest cardinality mismatch")
            connection.execute(
                "UPDATE manifest_runs SET state='SEALED',sealed_at=? WHERE tick_id=?",
                (iso(sealed_at), tick_id),
            )

    def fail_manifest(self, role, tick_id, failure_reason):
        self._role(role, "fail_manifest")
        validate_failure_reason(failure_reason)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE manifest_runs SET state='FAILED',failure_reason=? "
                "WHERE tick_id=? AND state='OPEN'",
                (failure_reason, tick_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("manifest cannot fail")

    def read_manifest_summary(self, tick_id):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM manifest_runs WHERE tick_id=?", (tick_id,)
            ).fetchone()
        if row is None:
            return None
        return ManifestSummaryV1(
            row["tick_id"],
            parse_iso(row["started_at"]),
            _ticks(row["material_ticks_json"]),
            row["material_binding_digest"],
            _decode(row["counts_json"]),
            row["total_count"],
            ManifestState(row["state"]),
            parse_iso(row["sealed_at"]),
            row["failure_reason"],
        )

    def read_manifest_candidates(self, tick_id):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM candidate_manifest WHERE tick_id=? ORDER BY candidate_id",
                (tick_id,),
            ).fetchall()
        return tuple(
            CandidateManifestV1(
                row["candidate_id"],
                row["subject_id"],
                row["kind"],
                tuple(_decode(row["evidence_refs_json"])),
                CandidateAssignment(row["assignment"]),
                CandidateState(row["state"]),
                parse_iso(row["created_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _outbox_values(row):
        return (
            row.alert_id,
            row.origin.value,
            row.decision_id,
            row.incident_kind,
            row.incident_subject,
            row.requeue_of,
            row.tick_id,
            row.subject_id,
            row.dedupe_key,
            row.channel.value,
            row.severity,
            _json(row.body_evidence),
            iso(row.created_at),
            iso(row.attempted_at),
            iso(row.delivered_at),
            None if row.status is None else row.status.value,
            row.provider_message_id,
            row.resolution.value,
            iso(row.resolved_at),
            row.resolution_reason,
        )

    @staticmethod
    def _insert_outbox(connection, row):
        connection.execute(
            "INSERT INTO alert_outbox VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            OpsKernelStore._outbox_values(row),
        )

    def record_disposition_with_outbox(
        self,
        role,
        disposition,
        outbox_or_none,
        *,
        activation_enabled=False,
        inject_crash_at=None,
    ):
        self._role(role, "record_disposition_with_outbox")
        needs = disposition.action in {
            DispositionAction.SEND,
            DispositionAction.ESCALATE,
        }
        if needs != (outbox_or_none is not None):
            raise OpsKernelTransitionError("outbox/action invariant")
        if outbox_or_none is not None:
            if (
                outbox_or_none.origin is not AlertOrigin.DISPOSITION
                or outbox_or_none.decision_id != disposition.decision_id
                or outbox_or_none.tick_id != disposition.tick_id
                or outbox_or_none.subject_id != disposition.subject_id
                or outbox_or_none.requeue_of is not None
                or outbox_or_none.resolution is not AlertResolution.UNRESOLVED
                or outbox_or_none.resolved_at is not None
                or outbox_or_none.resolution_reason is not None
            ):
                raise OpsKernelTransitionError("outbox decision mismatch")
            if outbox_or_none.channel is AlertChannel.TELEGRAM and (
                outbox_or_none.status is not AlertStatus.PENDING
                or outbox_or_none.attempted_at is not None
                or outbox_or_none.delivered_at is not None
                or outbox_or_none.provider_message_id is not None
            ):
                raise OpsKernelTransitionError("telegram initial state invariant")
            if (
                outbox_or_none.channel is AlertChannel.TELEGRAM
                and not activation_enabled
            ):
                raise OpsKernelTransitionError("telegram activation disabled")
        values = (
            disposition.decision_id,
            disposition.tick_id,
            disposition.candidate_id,
            disposition.subject_id,
            disposition.action.value,
            disposition.reason_code,
            _json(disposition.cited_evidence_ids),
            _ticks_json(disposition.material_ticks),
            disposition.glm_status,
            disposition.sol_status,
            disposition.policy_version,
            iso(disposition.decided_at),
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM dispositions WHERE tick_id=? AND candidate_id=?",
                (disposition.tick_id, disposition.candidate_id),
            ).fetchone()
            if existing:
                stored = tuple(existing)
                if stored != values:
                    raise OpsKernelConflictError("disposition identity conflict")
                stored_outbox = connection.execute(
                    "SELECT * FROM alert_outbox WHERE decision_id=? ORDER BY alert_id",
                    (disposition.decision_id,),
                ).fetchall()
                requested_outbox = (
                    ()
                    if outbox_or_none is None
                    else (self._outbox_values(outbox_or_none),)
                )
                if tuple(tuple(row) for row in stored_outbox) != requested_outbox:
                    raise OpsKernelConflictError("outbox replay conflict")
                return ReplayResult.DUPLICATE
            run = connection.execute(
                "SELECT state FROM manifest_runs WHERE tick_id=?",
                (disposition.tick_id,),
            ).fetchone()
            candidate = connection.execute(
                "SELECT subject_id,state FROM candidate_manifest WHERE tick_id=? AND candidate_id=?",
                (disposition.tick_id, disposition.candidate_id),
            ).fetchone()
            if (
                not run
                or run[0] != "SEALED"
                or not candidate
                or candidate[0] != disposition.subject_id
            ):
                raise OpsKernelTransitionError("candidate not consumable")
            if candidate["state"] != CandidateState.PENDING.value:
                raise OpsKernelTransitionError("candidate not pending")
            connection.execute(
                "INSERT INTO dispositions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
            if inject_crash_at == "after_disposition":
                raise RuntimeError("injected")
            if outbox_or_none is not None:
                self._insert_outbox(connection, outbox_or_none)
            if inject_crash_at == "after_outbox":
                raise RuntimeError("injected")
            state = (
                "DEFERRED"
                if disposition.action is DispositionAction.DEFER
                else "TERMINAL"
                if disposition.action
                in {DispositionAction.SKIP, DispositionAction.MERGE}
                else "DISPOSITIONED"
            )
            connection.execute(
                "UPDATE candidate_manifest SET state=? WHERE tick_id=? AND candidate_id=?",
                (state, disposition.tick_id, disposition.candidate_id),
            )
            return ReplayResult.CREATED

    def read_disposition(self, decision_id):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM dispositions WHERE decision_id=?", (decision_id,)
            ).fetchone()
        if not row:
            return None
        return DispositionV1(
            row["decision_id"],
            row["tick_id"],
            row["candidate_id"],
            row["subject_id"],
            DispositionAction(row["action"]),
            row["reason_code"],
            tuple(_decode(row["cited_evidence_ids_json"])),
            _ticks(row["material_ticks_json"]),
            row["glm_status"],
            row["sol_status"],
            row["policy_version"],
            parse_iso(row["decided_at"]),
        )

    def read_dispositions_for_tick(self, tick_id):
        if type(tick_id) is not int or tick_id < 0:
            raise ValueError("invalid tick")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM dispositions WHERE tick_id=? ORDER BY candidate_id",
                (tick_id,),
            ).fetchall()
        return tuple(
            DispositionV1(
                row["decision_id"],
                row["tick_id"],
                row["candidate_id"],
                row["subject_id"],
                DispositionAction(row["action"]),
                row["reason_code"],
                tuple(_decode(row["cited_evidence_ids_json"])),
                _ticks(row["material_ticks_json"]),
                row["glm_status"],
                row["sol_status"],
                row["policy_version"],
                parse_iso(row["decided_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _outbox(row):
        if row is None:
            return None
        return AlertOutboxV1(
            row["alert_id"],
            AlertOrigin(row["origin"]),
            row["decision_id"],
            row["incident_kind"],
            row["incident_subject"],
            row["requeue_of"],
            row["tick_id"],
            row["subject_id"],
            row["dedupe_key"],
            AlertChannel(row["channel"]),
            row["severity"],
            tuple(_decode(row["body_evidence_json"])),
            parse_iso(row["created_at"]),
            parse_iso(row["attempted_at"]),
            parse_iso(row["delivered_at"]),
            None if row["status"] is None else AlertStatus(row["status"]),
            row["provider_message_id"],
            AlertResolution(row["resolution"]),
            parse_iso(row["resolved_at"]),
            row["resolution_reason"],
        )

    def read_outbox(self, alert_id):
        with self._read() as connection:
            return self._outbox(
                connection.execute(
                    "SELECT * FROM alert_outbox WHERE alert_id=?", (alert_id,)
                ).fetchone()
            )

    def create_analysis_requests_with_manifest(
        self,
        role,
        *,
        tick_id,
        started_at,
        material_ticks,
        material_binding_digest,
        candidates,
        requests,
        baseline_advances=(),
        deferred_writes=(),
        history_advances=(),
        inject_crash_at=None,
    ):
        """Atomic manifest + analysis request creation (§7 Atomic APIs).

        One transaction for the candidate manifest, admitted analysis
        requests, baseline/history advances, and initial deferred rows.
        Empty admitted set is a valid sealed/terminal tick outcome with
        analyzer calls 0.
        """
        self._role(role, "create_analysis_requests_with_manifest")
        return self.create_manifest_with_baselines(
            role,
            tick_id,
            started_at,
            material_ticks,
            material_binding_digest,
            candidates,
            baseline_advances,
            deferred_writes,
            history_advances=history_advances,
            analysis_requests=tuple(requests),
            inject_crash_at=inject_crash_at,
        )

    def start_analysis_request(self, role, request_id, *, now):
        """Transition PENDING -> ANALYZING before any analyzer call (§7)."""
        self._role(role, "start_analysis_request")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT state FROM analysis_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("request not found")
            if row["state"] == "ANALYZING":
                return ReplayResult.DUPLICATE
            if row["state"] != "PENDING":
                raise OpsKernelTransitionError("request not pending")
            connection.execute(
                "UPDATE analysis_requests SET state='ANALYZING',settled_at=? "
                "WHERE request_id=?",
                (iso(now), request_id),
            )
            return ReplayResult.CREATED

    def read_analysis_request(self, request_id):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM analysis_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
        return None if row is None else dict(row)

    def settle_analysis_result_with_coverage(
        self,
        role,
        *,
        request_id,
        result,
        coverage_rows=(),
        disposition=None,
        outbox_or_none=None,
        unknown_incident_key=None,
        unknown_outbox_or_none=None,
        inject_crash_at=None,
    ):
        """Atomic result/disposition/coverage settlement (§7).

        One transaction: matching candidate terminal state, agreeing
        request terminal state, and zero-or-more coverage rows. Injected
        crash at each named point rolls back all.
        """
        self._role(role, "settle_analysis_result_with_coverage")
        coverage_rows = tuple(coverage_rows)
        if result.request_id != request_id:
            raise ValueError("result request mismatch")
        if (result.status == "SUCCEEDED") != (disposition is not None):
            raise ValueError("successful result requires disposition")
        if result.status != "SUCCEEDED" and coverage_rows:
            raise ValueError("non-success result forbids coverage")
        if (result.status == "UNKNOWN") != (unknown_incident_key is not None):
            raise ValueError("UNKNOWN result requires incident")
        if result.status != "UNKNOWN" and unknown_outbox_or_none is not None:
            raise ValueError("non-UNKNOWN result forbids incident outbox")
        if any(
            row.request_id != request_id
            or row.result_id != result.result_id
            or row.analysis_kind != result.analysis_kind
            for row in coverage_rows
        ):
            raise ValueError("coverage result mismatch")
        with self._transaction() as connection:
            req_row = connection.execute(
                "SELECT * FROM analysis_requests WHERE request_id=?",
                (request_id,),
            ).fetchone()
            if req_row is None:
                raise OpsKernelTransitionError("request not found")
            if req_row["state"] not in ("ANALYZING",):
                if req_row["state"] == result.status:
                    return ReplayResult.DUPLICATE
                raise OpsKernelTransitionError("request not in analyzing state")
            candidate = connection.execute(
                "SELECT state,evidence_refs_json,subject_id FROM candidate_manifest "
                "WHERE tick_id=? AND candidate_id=?",
                (req_row["tick_id"], req_row["candidate_id"]),
            ).fetchone()
            if candidate is None:
                raise OpsKernelTransitionError("candidate not found")
            if candidate["state"] != "PENDING":
                raise OpsKernelTransitionError("candidate not pending")
            result_kind = (
                result.analysis_kind.value
                if hasattr(result.analysis_kind, "value")
                else result.analysis_kind
            )
            if result_kind != req_row["analysis_kind"]:
                raise OpsKernelTransitionError("result analysis kind mismatch")
            expected = tuple(_decode(req_row["expected_coverage_json"]))
            expected_by_key = {(row["market"], row["symbol"]): row for row in expected}
            actual_by_key = {(row.market, row.symbol): row for row in coverage_rows}
            if len(actual_by_key) != len(coverage_rows):
                raise OpsKernelConflictError("duplicate coverage symbol")
            if result.status == "SUCCEEDED" and set(actual_by_key) != set(
                expected_by_key
            ):
                raise OpsKernelConflictError(
                    "coverage must exactly match expected symbols"
                )
            candidate_evidence = set(_decode(candidate["evidence_refs_json"]))
            for key, cov in actual_by_key.items():
                contract = expected_by_key[key]
                if (
                    cov.subject_id != req_row["subject_id"]
                    or candidate["subject_id"] != req_row["subject_id"]
                    or cov.policy_digest != req_row["admission_policy_digest"]
                    or cov.evidence_id != contract["evidence_id"]
                    or cov.evidence_id not in candidate_evidence
                    or cov.material_tick_id != contract["material_tick_id"]
                    or iso(cov.material_observed_at) != contract["material_observed_at"]
                    or cov.analyzed_at != result.analyzed_at
                ):
                    raise OpsKernelConflictError("coverage provenance mismatch")
            if disposition is not None:
                if (
                    disposition.decision_id != result.decision_id
                    or disposition.tick_id != req_row["tick_id"]
                    or disposition.candidate_id != req_row["candidate_id"]
                    or disposition.subject_id != req_row["subject_id"]
                ):
                    raise OpsKernelTransitionError("result disposition mismatch")
                needs = disposition.action in {
                    DispositionAction.SEND,
                    DispositionAction.ESCALATE,
                }
                if needs != (outbox_or_none is not None):
                    raise OpsKernelTransitionError("outbox/action invariant")
                values = (
                    disposition.decision_id,
                    disposition.tick_id,
                    disposition.candidate_id,
                    disposition.subject_id,
                    disposition.action.value,
                    disposition.reason_code,
                    _json(disposition.cited_evidence_ids),
                    _ticks_json(disposition.material_ticks),
                    disposition.glm_status,
                    disposition.sol_status,
                    disposition.policy_version,
                    iso(disposition.decided_at),
                )
                connection.execute(
                    "INSERT INTO dispositions VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
                if outbox_or_none is not None:
                    if (
                        outbox_or_none.channel is not AlertChannel.SHADOW
                        or outbox_or_none.decision_id != disposition.decision_id
                    ):
                        raise OpsKernelTransitionError("shadow outbox invariant")
                    self._insert_outbox(connection, outbox_or_none)
                if inject_crash_at == "after_disposition":
                    raise RuntimeError("injected")
            if unknown_incident_key is not None:
                expected_key = IncidentKeyV1(
                    "analysis-request-unknown",
                    request_id,
                )
                if unknown_incident_key != expected_key:
                    raise OpsKernelTransitionError("UNKNOWN incident mismatch")
                if unknown_outbox_or_none is None:
                    raise OpsKernelTransitionError("UNKNOWN shadow outbox required")
                self._validate_shadow_incident_outbox(
                    unknown_outbox_or_none,
                    unknown_incident_key,
                )
                connection.execute(
                    "INSERT INTO incidents VALUES(?,?,?,?,1,'OPEN',?,?)",
                    (
                        unknown_incident_key.kind,
                        unknown_incident_key.subject,
                        iso(result.analyzed_at),
                        iso(result.analyzed_at),
                        request_id,
                        iso(result.analyzed_at),
                    ),
                )
                self._insert_outbox(connection, unknown_outbox_or_none)
                if inject_crash_at == "after_incident":
                    raise RuntimeError("injected")
            connection.execute(
                "UPDATE analysis_requests SET state=?,result_id=? WHERE request_id=?",
                (result.status, result.result_id, request_id),
            )
            if inject_crash_at == "after_request":
                raise RuntimeError("injected")
            new_state = (
                "DEFERRED"
                if result.status == "DEFERRED"
                else "TERMINAL"
                if result.status in ("FAILED", "UNKNOWN")
                else "TERMINAL"
                if disposition is not None
                and disposition.action
                in {
                    DispositionAction.SKIP,
                    DispositionAction.MERGE,
                }
                else "DISPOSITIONED"
            )
            connection.execute(
                "UPDATE candidate_manifest SET state=? "
                "WHERE tick_id=? AND candidate_id=?",
                (new_state, req_row["tick_id"], req_row["candidate_id"]),
            )
            if inject_crash_at == "after_candidate":
                raise RuntimeError("injected")
            if result.status == "SUCCEEDED" and coverage_rows:
                for cov in coverage_rows:
                    connection.execute(
                        "INSERT INTO analysis_coverage VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                        (
                            cov.analysis_kind.value,
                            cov.market,
                            cov.symbol,
                            cov.request_id,
                            cov.result_id,
                            cov.subject_id,
                            cov.evidence_id,
                            cov.material_tick_id,
                            iso(cov.material_observed_at),
                            iso(cov.analyzed_at),
                            cov.policy_digest,
                        ),
                    )
            if inject_crash_at == "after_coverage":
                raise RuntimeError("injected")
            return ReplayResult.CREATED

    def reconcile_analysis_requests(self, role, *, now):
        """Settle every orphan ANALYZING request through the authoritative path."""
        self._role(role, "reconcile_analysis_requests")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT request_id,tick_id,candidate_id,analysis_kind,subject_id "
                "FROM analysis_requests WHERE state='ANALYZING' ORDER BY request_id"
            ).fetchall()
        reconciled = []
        for row in rows:
            payload = {
                "v": 1,
                "domain": "tokmo-v3:analysis-result:v1",
                "request_id": row["request_id"],
                "decision_id": None,
            }
            result = _ReconciledAnalysisResultV1(
                _sha256(canonical_encode(payload))[:32],
                row["request_id"],
                row["analysis_kind"],
                "UNKNOWN",
                None,
                None,
                now,
            )
            key = IncidentKeyV1("analysis-request-unknown", row["request_id"])
            outbox = AlertOutboxV1.for_incident(
                alert_id=_sha256(f"analysis-unknown:{row['request_id']}".encode())[:32],
                key=key,
                tick_id=row["tick_id"],
                subject_id=row["subject_id"],
                severity="HIGH",
                body_evidence=(),
                created_at=now,
            )
            replay = self.settle_analysis_result_with_coverage(
                role,
                request_id=row["request_id"],
                result=result,
                coverage_rows=(),
                unknown_incident_key=key,
                unknown_outbox_or_none=outbox,
            )
            reconciled.append(
                {
                    "request_id": row["request_id"],
                    "result_id": result.result_id,
                    "replay": replay,
                }
            )
        return tuple(reconciled)

    def read_latest_analysis_coverage(self, analysis_kind, market, symbols):
        """Closed bounded latest-coverage query (§7).

        For future AI-HC-001 producer; no health policy or clock logic.
        """
        if not symbols:
            return ()
        placeholders = ",".join("?" for _ in symbols)
        kind_value = (
            analysis_kind.value if hasattr(analysis_kind, "value") else analysis_kind
        )
        with self._read() as connection:
            rows = connection.execute(
                f"SELECT * FROM analysis_coverage "
                f"WHERE analysis_kind=? AND market=? "
                f"AND symbol IN ({placeholders}) "
                f"ORDER BY analyzed_at DESC",
                (kind_value, market, *symbols),
            ).fetchall()
        seen = set()
        result = []
        for row in rows:
            key = (row["analysis_kind"], row["market"], row["symbol"])
            if key in seen:
                continue
            seen.add(key)
            result.append(dict(row))
        return tuple(result)

    def claim_pending_telegram(
        self, role, now, *, origins=None, created_not_before=None
    ):
        self._role(role, "claim_pending_telegram")
        with self._transaction() as connection:
            origin_values = (
                None
                if origins is None
                else tuple(
                    origin.value if isinstance(origin, AlertOrigin) else origin
                    for origin in origins
                )
            )
            origin_clause = ""
            parameters = []
            if origin_values is not None:
                if not origin_values:
                    return None
                origin_clause = (
                    "AND origin IN (" + ",".join("?" for _ in origin_values) + ") "
                )
                parameters.extend(origin_values)
            base_query = (
                "SELECT * FROM alert_outbox WHERE channel='TELEGRAM' "
                "AND status='PENDING' AND resolution='UNRESOLVED' "
                + origin_clause
                + "ORDER BY CASE WHEN origin='NEWS_EVENT' AND severity='NEWS_IMMEDIATE' THEN 0 ELSE 1 END,created_at,alert_id"
            )

            def eligible(candidate):
                if created_not_before is not None and not created_on_or_after(
                    candidate["created_at"], created_not_before
                ):
                    return False
                digest = (
                    re.fullmatch(r"NEWS_DIGEST_(\d{1,2})", candidate["severity"])
                    if candidate["origin"] == "NEWS_EVENT"
                    else None
                )
                if digest:
                    local_hour = now.astimezone(timezone(timedelta(hours=9))).hour
                    return int(digest[1]) <= local_hour < 22
                return True

            # Deferred digests stay PENDING; they never block a later urgent/ordinary row.
            row = next(
                (
                    candidate
                    for candidate in connection.execute(base_query, parameters)
                    if eligible(candidate)
                ),
                None,
            )
            if row is None:
                return None
            connection.execute(
                "UPDATE alert_outbox SET status='ATTEMPTING',attempted_at=? WHERE alert_id=?",
                (iso(now), row["alert_id"]),
            )
            row = connection.execute(
                "SELECT * FROM alert_outbox WHERE alert_id=?", (row["alert_id"],)
            ).fetchone()
            return self._outbox(row)

    def _status_transition(
        self,
        role,
        method,
        alert_id,
        expected,
        target,
        now,
        provider_message_id=None,
        reason=None,
    ):
        self._role(role, method)
        with self._transaction() as connection:
            updates = "status=?"
            values = [target]
            if target == "SENT":
                updates += ",delivered_at=?,provider_message_id=?,resolution='CONFIRMED_SENT',resolved_at=?"
                values += [iso(now), provider_message_id, iso(now)]
            elif target == "FAILED":
                updates += ",resolution_reason=?"
                values.append(reason)
            values += [alert_id, expected]
            changed = connection.execute(
                f"UPDATE alert_outbox SET {updates} WHERE alert_id=? AND status=? "
                "AND channel='TELEGRAM' AND resolution='UNRESOLVED'",
                values,
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("invalid outbox transition")

    def mark_sent(self, role, alert_id, delivered_at, provider_message_id):
        if not provider_message_id:
            raise ValueError("provider reference required")
        self._status_transition(
            role,
            "mark_sent",
            alert_id,
            "ATTEMPTING",
            "SENT",
            delivered_at,
            provider_message_id,
        )

    def mark_failed(self, role, alert_id, failed_at, reason):
        validate_resolution_reason(reason)
        self._status_transition(
            role,
            "mark_failed",
            alert_id,
            "ATTEMPTING",
            "FAILED",
            failed_at,
            reason=reason,
        )

    def mark_unknown(self, role, alert_id, settled_at, reason):
        """M7: Set an ATTEMPTING alert to UNKNOWN (transport succeeded, settle failed).

        Unlike mark_failed, this does NOT mark the alert as failed, so it
        will not be retried by bounded requeue logic. The alert_outbox
        table has no settled_at column; we store the timestamp in
        attempted_at (already set at claim time) and keep resolution as
        UNRESOLVED so dedupe still holds.
        """
        validate_resolution_reason(reason)
        self._role(role, "mark_unknown")
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE alert_outbox SET status='UNKNOWN',resolution_reason=? "
                "WHERE alert_id=? AND status='ATTEMPTING' "
                "AND channel='TELEGRAM' AND resolution='UNRESOLVED'",
                (reason, alert_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("invalid unknown transition")

    def acknowledge_failed(self, role, alert_id, now, reason):
        self._role(role, "acknowledge_failed")
        validate_resolution_reason(reason)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE alert_outbox SET resolution='FAILED_ACKNOWLEDGED',resolved_at=?,"
                "resolution_reason=? WHERE alert_id=? AND status='FAILED' "
                "AND resolution='UNRESOLVED'",
                (iso(now), reason, alert_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("failed alert cannot acknowledge")

    def promote_stale_attempting(self, role, now):
        self._role(role, "promote_stale_attempting")
        cutoff = now - timedelta(seconds=300)
        with self._transaction() as connection:
            return connection.execute(
                "UPDATE alert_outbox SET status='UNKNOWN' WHERE channel='TELEGRAM' "
                "AND status='ATTEMPTING' AND resolution='UNRESOLVED' AND attempted_at<=?",
                (iso(cutoff),),
            ).rowcount

    def resolve_unknown(self, role, alert_id, now, reason):
        self._role(role, "resolve_unknown")
        validate_resolution_reason(reason)
        with self._transaction() as connection:
            changed = connection.execute(
                "UPDATE alert_outbox SET resolution='CONFIRMED_SENT',resolved_at=?,"
                "resolution_reason=? WHERE alert_id=? AND status='UNKNOWN' "
                "AND resolution='UNRESOLVED'",
                (iso(now), reason, alert_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("unknown alert cannot resolve")

    def requeue_unknown(
        self, role, alert_id, new_alert_id, now, *, inject_crash_at=None
    ):
        self._role(role, "requeue_unknown")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM alert_outbox WHERE alert_id=? AND status='UNKNOWN' "
                "AND resolution='UNRESOLVED'",
                (alert_id,),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("unknown alert cannot requeue")
            connection.execute(
                "UPDATE alert_outbox SET resolution='REQUEUED',resolved_at=?,"
                "resolution_reason='operator_requeue' WHERE alert_id=?",
                (iso(now), alert_id),
            )
            if inject_crash_at == "after_resolve":
                raise RuntimeError("injected")
            new = AlertOutboxV1(
                new_alert_id,
                AlertOrigin(row["origin"]),
                row["decision_id"],
                row["incident_kind"],
                row["incident_subject"],
                alert_id,
                row["tick_id"],
                row["subject_id"],
                row["dedupe_key"],
                AlertChannel.TELEGRAM,
                row["severity"],
                tuple(_decode(row["body_evidence_json"])),
                now,
                status=AlertStatus.PENDING,
            )
            self._insert_outbox(connection, new)

    def observe_incident(self, role, key, impact_ref, now):
        self._role(role, "observe_incident")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            required = (
                row is None
                or row["impact_ref"] != impact_ref
                or row["state"] == "RESOLVED"
            )
            if row is None:
                connection.execute(
                    "INSERT INTO incidents VALUES(?,?,?,?,1,'OPEN',?,NULL)",
                    (key.kind, key.subject, iso(now), iso(now), impact_ref),
                )
            else:
                connection.execute(
                    "UPDATE incidents SET last_seen_at=?,count=count+1,state='OPEN',impact_ref=? "
                    "WHERE kind=? AND subject=?",
                    (iso(now), impact_ref, key.kind, key.subject),
                )
            fresh = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            return IncidentObservationV1(self._incident(fresh), required)

    def resolve_incident(self, role, key, now):
        self._role(role, "resolve_incident")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("incident absent")
            required = row["state"] == "OPEN"
            if required:
                connection.execute(
                    "UPDATE incidents SET state='RESOLVED',last_seen_at=? "
                    "WHERE kind=? AND subject=?",
                    (iso(now), key.kind, key.subject),
                )
            fresh = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            return IncidentObservationV1(self._incident(fresh), required)

    @staticmethod
    def _validate_shadow_incident_outbox(outbox, key):
        if (
            outbox.origin is not AlertOrigin.INCIDENT
            or outbox.channel is not AlertChannel.SHADOW
            or outbox.incident_kind != key.kind
            or outbox.incident_subject != key.subject
            or outbox.decision_id is not None
            or outbox.requeue_of is not None
            or outbox.resolution is not AlertResolution.UNRESOLVED
            or outbox.resolved_at is not None
            or outbox.resolution_reason is not None
        ):
            raise OpsKernelTransitionError("incident alert invariant")

    def observe_incident_with_shadow_outbox(
        self,
        role,
        key,
        impact_ref,
        outbox_or_none,
        now,
        *,
        inject_crash_at=None,
    ):
        self._role(role, "observe_incident_with_shadow_outbox")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            required = (
                row is None
                or row["impact_ref"] != impact_ref
                or row["state"] == "RESOLVED"
            )
            if required and outbox_or_none is None:
                raise OpsKernelTransitionError("incident notification required")
            if outbox_or_none is not None:
                self._validate_shadow_incident_outbox(outbox_or_none, key)
            if row is None:
                connection.execute(
                    "INSERT INTO incidents VALUES(?,?,?,?,1,'OPEN',?,NULL)",
                    (key.kind, key.subject, iso(now), iso(now), impact_ref),
                )
            else:
                connection.execute(
                    "UPDATE incidents SET last_seen_at=?,count=count+1,state='OPEN',"
                    "impact_ref=? WHERE kind=? AND subject=?",
                    (iso(now), impact_ref, key.kind, key.subject),
                )
            if inject_crash_at == "after_incident":
                raise RuntimeError("injected")
            replay = None
            if required:
                prior = connection.execute(
                    "SELECT * FROM alert_outbox WHERE alert_id=?",
                    (outbox_or_none.alert_id,),
                ).fetchone()
                if prior is None:
                    self._insert_outbox(connection, outbox_or_none)
                    replay = ReplayResult.CREATED
                elif tuple(prior) == self._outbox_values(outbox_or_none):
                    replay = ReplayResult.DUPLICATE
                else:
                    raise OpsKernelConflictError("incident outbox identity conflict")
                connection.execute(
                    "UPDATE incidents SET last_notified_at=? WHERE kind=? AND subject=?",
                    (iso(outbox_or_none.created_at), key.kind, key.subject),
                )
            fresh = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            return IncidentObservationV1(self._incident(fresh), required), replay

    def resolve_incident_with_shadow_outbox(
        self,
        role,
        key,
        outbox_or_none,
        now,
        *,
        inject_crash_at=None,
    ):
        self._role(role, "resolve_incident_with_shadow_outbox")
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("incident absent")
            required = row["state"] == "OPEN"
            if required and outbox_or_none is None:
                raise OpsKernelTransitionError("incident notification required")
            if outbox_or_none is not None:
                self._validate_shadow_incident_outbox(outbox_or_none, key)
            if required:
                connection.execute(
                    "UPDATE incidents SET state='RESOLVED',last_seen_at=? "
                    "WHERE kind=? AND subject=?",
                    (iso(now), key.kind, key.subject),
                )
            if inject_crash_at == "after_incident":
                raise RuntimeError("injected")
            replay = None
            if required:
                prior = connection.execute(
                    "SELECT * FROM alert_outbox WHERE alert_id=?",
                    (outbox_or_none.alert_id,),
                ).fetchone()
                if prior is None:
                    self._insert_outbox(connection, outbox_or_none)
                    replay = ReplayResult.CREATED
                elif tuple(prior) == self._outbox_values(outbox_or_none):
                    replay = ReplayResult.DUPLICATE
                else:
                    raise OpsKernelConflictError("incident outbox identity conflict")
                connection.execute(
                    "UPDATE incidents SET last_notified_at=? WHERE kind=? AND subject=?",
                    (iso(outbox_or_none.created_at), key.kind, key.subject),
                )
            fresh = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
            return IncidentObservationV1(self._incident(fresh), required), replay

    @staticmethod
    def _incident(row):
        return IncidentV1(
            IncidentKeyV1(row["kind"], row["subject"]),
            parse_iso(row["first_seen_at"]),
            parse_iso(row["last_seen_at"]),
            row["count"],
            IncidentState(row["state"]),
            row["impact_ref"],
            parse_iso(row["last_notified_at"]),
        )

    def read_incident(self, key):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM incidents WHERE kind=? AND subject=?",
                (key.kind, key.subject),
            ).fetchone()
        return None if row is None else self._incident(row)

    def read_incident_rows(self, state=None):
        if state is not None and state not in {"OPEN", "RESOLVED"}:
            raise ValueError("invalid incident state")
        with self._read() as connection:
            if state is None:
                rows = connection.execute(
                    "SELECT * FROM incidents ORDER BY kind,subject"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM incidents WHERE state=? ORDER BY kind,subject",
                    (state,),
                ).fetchall()
        return tuple(self._incident(row) for row in rows)

    def record_incident_outbox(self, role, outbox):
        self._role(role, "record_incident_outbox")
        if (
            outbox.origin is not AlertOrigin.INCIDENT
            or outbox.channel is not AlertChannel.SHADOW
            or outbox.decision_id is not None
            or outbox.requeue_of is not None
            or outbox.resolution is not AlertResolution.UNRESOLVED
            or outbox.resolved_at is not None
            or outbox.resolution_reason is not None
        ):
            raise OpsKernelTransitionError("incident alert invariant")
        with self._transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM incidents WHERE kind=? AND subject=?",
                (outbox.incident_kind, outbox.incident_subject),
            ).fetchone()
            if not exists:
                raise OpsKernelTransitionError("incident absent")
            prior = connection.execute(
                "SELECT * FROM alert_outbox WHERE alert_id=?", (outbox.alert_id,)
            ).fetchone()
            if prior is not None:
                if tuple(prior) != self._outbox_values(outbox):
                    raise OpsKernelConflictError("incident outbox identity conflict")
                return ReplayResult.DUPLICATE
            self._insert_outbox(connection, outbox)
            connection.execute(
                "UPDATE incidents SET last_notified_at=? WHERE kind=? AND subject=?",
                (iso(outbox.created_at), outbox.incident_kind, outbox.incident_subject),
            )
            return ReplayResult.CREATED

    def record_pension_reminder_outbox(
        self,
        role,
        outbox,
        *,
        activation_enabled=False,
    ):
        """N7: Record a pension-reminder alert_outbox row with TELEGRAM channel.

        Unlike record_incident_outbox (SHADOW-only), this method admits
        AlertChannel.TELEGRAM with status=PENDING, gated by activation_enabled.
        """
        self._role(role, "record_pension_reminder_outbox")
        if (
            outbox.origin is not AlertOrigin.INCIDENT
            or outbox.channel is not AlertChannel.TELEGRAM
            or outbox.status is not AlertStatus.PENDING
            or outbox.decision_id is not None
            or outbox.requeue_of is not None
            or outbox.resolution is not AlertResolution.UNRESOLVED
            or outbox.resolved_at is not None
            or outbox.resolution_reason is not None
        ):
            raise OpsKernelTransitionError("pension reminder invariant")
        if not activation_enabled:
            raise OpsKernelTransitionError("telegram activation disabled")
        with self._transaction() as connection:
            exists = connection.execute(
                "SELECT 1 FROM incidents WHERE kind=? AND subject=?",
                (outbox.incident_kind, outbox.incident_subject),
            ).fetchone()
            if not exists:
                raise OpsKernelTransitionError("incident absent")
            prior = connection.execute(
                "SELECT * FROM alert_outbox WHERE alert_id=?", (outbox.alert_id,)
            ).fetchone()
            if prior is not None:
                if tuple(prior) != self._outbox_values(outbox):
                    raise OpsKernelConflictError("pension reminder identity conflict")
                return ReplayResult.DUPLICATE
            self._insert_outbox(connection, outbox)
            connection.execute(
                "UPDATE incidents SET last_notified_at=? WHERE kind=? AND subject=?",
                (iso(outbox.created_at), outbox.incident_kind, outbox.incident_subject),
            )
            return ReplayResult.CREATED

    def _record_news_outbox(self, role, outbox, origin, method, *, activation_enabled):
        self._role(role, method)
        if (
            outbox.origin is not origin
            or outbox.requeue_of is not None
            or outbox.resolution is not AlertResolution.UNRESOLVED
            or outbox.resolved_at is not None
            or outbox.resolution_reason is not None
        ):
            raise OpsKernelTransitionError("news outbox invariant")
        if outbox.channel is AlertChannel.TELEGRAM:
            if outbox.status is not AlertStatus.PENDING or not activation_enabled:
                raise OpsKernelTransitionError("telegram activation disabled")
        elif outbox.channel is not AlertChannel.SHADOW or outbox.status is not None:
            raise OpsKernelTransitionError("news shadow invariant")
        with self._transaction() as connection:
            prior = connection.execute(
                "SELECT * FROM alert_outbox WHERE alert_id=?", (outbox.alert_id,)
            ).fetchone()
            if prior is not None:
                if tuple(prior) != self._outbox_values(outbox):
                    raise OpsKernelConflictError("news outbox identity conflict")
                return ReplayResult.DUPLICATE
            self._insert_outbox(connection, outbox)
            return ReplayResult.CREATED

    def record_news_alert_outbox(self, role, outbox, *, activation_enabled=False):
        return self._record_news_outbox(
            role,
            outbox,
            AlertOrigin.NEWS_EVENT,
            "record_news_alert_outbox",
            activation_enabled=activation_enabled,
        )

    def record_news_ops_failure_outbox(self, role, outbox, *, activation_enabled=False):
        return self._record_news_outbox(
            role,
            outbox,
            AlertOrigin.NEWS_OPS,
            "record_news_ops_failure_outbox",
            activation_enabled=activation_enabled,
        )

    def record_quant_market_close_outbox(self, role, outbox):
        return self._record_news_outbox(
            role,
            outbox,
            AlertOrigin.QUANT_MARKET_CLOSE,
            "record_quant_market_close_outbox",
            activation_enabled=False,
        )

    def upsert_deferred(self, role, attempt, *, inject_crash_at=None):
        self._role(role, "upsert_deferred")
        if not isinstance(attempt, DeferredAttemptV1):
            raise ValueError("invalid deferred attempt")
        with self._transaction() as connection:
            return self._upsert_deferred(
                connection, attempt, inject_crash_at=inject_crash_at
            )

    def _upsert_deferred(self, connection, attempt, *, inject_crash_at=None):
        evidence_json = _json(attempt.evidence_refs)
        ticks_json = _ticks_json(attempt.material_ticks)
        current_projection = (
            attempt.market.value,
            evidence_json,
            ticks_json,
            attempt.next_review_at,
            attempt.reason_code,
            attempt.assignment.value,
            attempt.candidate_tick,
            attempt.candidate_id,
        )
        old = connection.execute(
            "SELECT * FROM deferred_candidates WHERE subject_id=?",
            (attempt.subject_id,),
        ).fetchone()
        if old is not None:
            self._validate_deferred_state(connection, old)
        history = connection.execute(
            "SELECT * FROM deferred_attempts WHERE subject_id=? AND attempt_tick=?",
            (attempt.subject_id, attempt.attempt_tick),
        ).fetchone()
        history_projection = (
            None
            if history is None
            else (
                history["market"],
                history["evidence_refs_json"],
                history["material_ticks_json"],
                parse_iso(history["next_review_at"]),
                history["reason_code"],
                history["assignment"],
                history["candidate_tick"],
                history["candidate_id"],
            )
        )
        if old is None:
            if attempt.attempt_tick != attempt.first_seen_tick:
                raise OpsKernelTransitionError("deferred first attempt shape")
            if history is not None:
                raise OpsKernelCorruptError("deferred history torn")
            connection.execute(
                "INSERT INTO deferred_candidates"
                "(subject_id,market,evidence_refs_json,material_ticks_json,first_seen_tick,"
                "last_attempt_tick,next_review_at,attempts,terminal_state,updated_at)"
                " VALUES(?,?,?,?,?,?,?,1,NULL,?)",
                (
                    attempt.subject_id,
                    attempt.market.value,
                    evidence_json,
                    ticks_json,
                    attempt.first_seen_tick,
                    attempt.attempt_tick,
                    iso(attempt.next_review_at),
                    iso(attempt.attempted_at),
                ),
            )
            if inject_crash_at == "after_current":
                raise RuntimeError("injected")
            self._insert_deferred_attempt(
                connection, attempt, evidence_json, ticks_json
            )
            if inject_crash_at == "after_history":
                raise RuntimeError("injected")
            return ReplayResult.CREATED
        if old["terminal_state"] is not None:
            raise OpsKernelTransitionError("deferred candidate terminal")
        if old["first_seen_tick"] != attempt.first_seen_tick:
            raise OpsKernelConflictError("deferred identity conflict")
        if old["market"] != attempt.market.value:
            raise OpsKernelConflictError("deferred market conflict")
        if attempt.attempt_tick < old["last_attempt_tick"]:
            raise OpsKernelConflictError("deferred attempt regression")
        if attempt.attempt_tick == old["last_attempt_tick"]:
            stored_current = (
                old["market"],
                old["evidence_refs_json"],
                old["material_ticks_json"],
                parse_iso(old["next_review_at"]),
                history["reason_code"] if history else None,
                history["assignment"] if history else None,
                history["candidate_tick"] if history else None,
                history["candidate_id"] if history else None,
            )
            if history is None:
                raise OpsKernelCorruptError("deferred history torn")
            if (
                stored_current != current_projection
                or history_projection != current_projection
            ):
                raise OpsKernelConflictError("deferred attempt identity conflict")
            return ReplayResult.DUPLICATE
        if old["attempts"] >= DEFERRED_ATTEMPTS_MAX:
            raise OpsKernelBudgetExceeded("deferred attempts exhausted")
        if history is not None:
            raise OpsKernelCorruptError("deferred history torn")
        connection.execute(
            "UPDATE deferred_candidates SET evidence_refs_json=?,"
            "material_ticks_json=?,last_attempt_tick=?,next_review_at=?,"
            "attempts=attempts+1,updated_at=? WHERE subject_id=?",
            (
                evidence_json,
                ticks_json,
                attempt.attempt_tick,
                iso(attempt.next_review_at),
                iso(attempt.attempted_at),
                attempt.subject_id,
            ),
        )
        if inject_crash_at == "after_current":
            raise RuntimeError("injected")
        self._insert_deferred_attempt(connection, attempt, evidence_json, ticks_json)
        if inject_crash_at == "after_history":
            raise RuntimeError("injected")
        return ReplayResult.CREATED

    @staticmethod
    def _validate_deferred_state(connection, current):
        rows = connection.execute(
            "SELECT * FROM deferred_attempts WHERE subject_id=? ORDER BY attempt_tick",
            (current["subject_id"],),
        ).fetchall()
        if (
            len(rows) != current["attempts"]
            or not rows
            or rows[0]["attempt_tick"] != current["first_seen_tick"]
            or rows[-1]["attempt_tick"] != current["last_attempt_tick"]
        ):
            raise OpsKernelCorruptError("deferred history torn")
        latest = rows[-1]
        try:
            evidence = _json(tuple(_decode(latest["evidence_refs_json"])))
            ticks = _ticks_json(_ticks(latest["material_ticks_json"]))
            next_review = iso(parse_iso(latest["next_review_at"]))
            parse_iso(latest["attempted_at"])
        except (ValueError, TypeError) as error:
            raise OpsKernelCorruptError("deferred history invalid") from error
        if (
            current["market"],
            current["evidence_refs_json"],
            current["material_ticks_json"],
            current["next_review_at"],
        ) != (latest["market"], evidence, ticks, next_review):
            raise OpsKernelCorruptError("deferred projection drift")
        if (
            current["terminal_state"] is None
            and current["updated_at"] != latest["attempted_at"]
        ):
            raise OpsKernelCorruptError("deferred timestamp drift")

    @staticmethod
    def _insert_deferred_attempt(connection, attempt, evidence_json, ticks_json):
        connection.execute(
            "INSERT INTO deferred_attempts VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                attempt.subject_id,
                attempt.market.value,
                attempt.attempt_tick,
                attempt.candidate_tick,
                attempt.candidate_id,
                evidence_json,
                ticks_json,
                iso(attempt.next_review_at),
                attempt.reason_code,
                attempt.assignment.value,
                iso(attempt.attempted_at),
            ),
        )

    def terminalize_deferred(self, role, subject_id, terminal_state, now):
        self._role(role, "terminalize_deferred")
        if not isinstance(terminal_state, DeferredTerminalState):
            raise ValueError("invalid deferred terminal state")
        with self._transaction() as connection:
            current = connection.execute(
                "SELECT * FROM deferred_candidates WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if current is None:
                raise OpsKernelTransitionError("deferred cannot terminalize")
            self._validate_deferred_state(connection, current)
            changed = connection.execute(
                "UPDATE deferred_candidates SET terminal_state=?,updated_at=? "
                "WHERE subject_id=? AND terminal_state IS NULL",
                (terminal_state.value, iso(now), subject_id),
            ).rowcount
            if changed != 1:
                raise OpsKernelTransitionError("deferred cannot terminalize")

    @staticmethod
    def _deferred(row):
        return DeferredCandidateV1(
            row["subject_id"],
            Market(row["market"]),
            tuple(_decode(row["evidence_refs_json"])),
            _ticks(row["material_ticks_json"]),
            row["first_seen_tick"],
            row["last_attempt_tick"],
            parse_iso(row["next_review_at"]),
            row["attempts"],
            row["terminal_state"],
            parse_iso(row["updated_at"]),
        )

    def read_due_deferred(self, market, now):
        if not isinstance(market, Market):
            raise ValueError("invalid market")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM deferred_candidates WHERE terminal_state IS NULL "
                "AND market=? AND next_review_at<=? ORDER BY next_review_at,subject_id",
                (market.value, iso(now)),
            ).fetchall()
            for row in rows:
                self._validate_deferred_state(connection, row)
            return tuple(self._deferred(row) for row in rows)

    def read_deferred_attempts(self, subject_id):
        require_token(subject_id, "subject_id")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM deferred_attempts WHERE subject_id=? "
                "ORDER BY attempt_tick LIMIT ?",
                (subject_id, DEFERRED_ATTEMPTS_MAX + 1),
            ).fetchall()
            if len(rows) > DEFERRED_ATTEMPTS_MAX:
                raise OpsKernelCorruptError("deferred history overflow")
            current = connection.execute(
                "SELECT first_seen_tick FROM deferred_candidates WHERE subject_id=?",
                (subject_id,),
            ).fetchone()
            if rows and current is None:
                raise OpsKernelCorruptError("deferred history torn")
            result = tuple(
                DeferredAttemptV1(
                    row["subject_id"],
                    Market(row["market"]),
                    current["first_seen_tick"],
                    row["attempt_tick"],
                    row["candidate_tick"],
                    row["candidate_id"],
                    tuple(_decode(row["evidence_refs_json"])),
                    _ticks(row["material_ticks_json"]),
                    parse_iso(row["next_review_at"]),
                    row["reason_code"],
                    CandidateAssignment(row["assignment"]),
                    parse_iso(row["attempted_at"]),
                )
                for row in rows
            )
            if result and result[0].attempt_tick != current["first_seen_tick"]:
                raise OpsKernelCorruptError("deferred history torn")
            return result

    def upsert_audit_backlog(
        self, role, kind, subject_id, first_session, considered_session, state
    ):
        self._role(role, "upsert_audit_backlog")
        if state not in {"PENDING_AUDIT", "AUDITED", "ESCALATED"}:
            raise ValueError("invalid audit state")
        with self._transaction() as connection:
            old = connection.execute(
                "SELECT first_eligible_session FROM audit_backlog WHERE kind=? AND subject_id=?",
                (kind, subject_id),
            ).fetchone()
            if old and old[0] != first_session:
                raise OpsKernelConflictError("audit identity conflict")
            connection.execute(
                "INSERT INTO audit_backlog VALUES(?,?,?,?,?) "
                "ON CONFLICT(kind,subject_id) DO UPDATE SET "
                "last_considered_session=excluded.last_considered_session,state=excluded.state",
                (kind, subject_id, first_session, considered_session, state),
            )

    def read_audit_backlog(self, kind):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_backlog WHERE kind=? ORDER BY first_eligible_session,subject_id",
                (kind,),
            ).fetchall()
        return tuple(AuditBacklogV1(*tuple(row)) for row in rows)

    def update_sample_rotation(self, role, kind, cursor_subject_id, now):
        self._role(role, "update_sample_rotation")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO sample_rotation VALUES(?,?,?) ON CONFLICT(kind) DO UPDATE SET "
                "cursor_subject_id=excluded.cursor_subject_id,updated_at=excluded.updated_at",
                (kind, cursor_subject_id, iso(now)),
            )

    def read_sample_rotation(self, kind):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM sample_rotation WHERE kind=?", (kind,)
            ).fetchone()
        if row is None:
            return None
        return {
            "kind": row["kind"],
            "cursor_subject_id": row["cursor_subject_id"],
            "updated_at": parse_iso(row["updated_at"]),
        }

    def write_audit_survey(self, role, session_date, rows, now):
        self._role(role, "write_audit_survey")
        rows = tuple(rows)
        kinds = {
            "HOLDINGS_CHANGE",
            "QUOTE_STATE",
            "COMPLETED_EXECUTION",
            "OPERATIONAL_INCIDENT",
        }
        if {_field(row, "kind") for row in rows} != kinds or len(rows) != 4:
            raise ValueError("audit survey requires four kinds")
        values = []
        for row in rows:
            counts = (
                _field(row, "eligible_count"),
                _field(row, "debt_count"),
                _field(row, "oldest_wait_regular_sessions"),
            )
            if any(type(value) is not int or value < 0 for value in counts):
                raise ValueError("invalid audit survey")
            opened = _field(row, "capacity_incident_opened")
            if type(opened) is not bool:
                raise ValueError("invalid audit survey")
            values.append(
                (
                    session_date,
                    _field(row, "kind"),
                    counts[0],
                    0,
                    counts[1],
                    counts[2],
                    int(opened),
                )
            )
        with self._transaction() as connection:
            for value in values:
                connection.execute(
                    "INSERT INTO audit_coverage VALUES(?,?,?,?,?,?,?) "
                    "ON CONFLICT(regular_session_date,kind) DO UPDATE SET "
                    "eligible_count=excluded.eligible_count,"
                    "debt_count=excluded.debt_count,"
                    "oldest_wait_regular_sessions=excluded.oldest_wait_regular_sessions,"
                    "capacity_incident_opened=excluded.capacity_incident_opened",
                    value,
                )

    def read_audit_coverage(self, session_date):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_coverage WHERE regular_session_date=? "
                "ORDER BY kind",
                (session_date,),
            ).fetchall()
        return tuple(dict(row) for row in rows)

    def complete_audit(
        self,
        role,
        *,
        tick_id,
        candidate_id,
        batch_ordinal,
        kind,
        subject_id,
        session_date,
        considered_session,
        now,
    ):
        self._role(role, "complete_audit")
        require_token(candidate_id, "candidate_id")
        require_token(subject_id, "subject_id")
        material_kind = MaterialKindV1(kind)
        # Validate the public receipt fields before opening the write transaction.
        AuditCompletionV1(material_kind, subject_id, session_date, 1)
        if type(considered_session) is not str:
            raise ValueError("invalid considered session")
        try:
            if (
                datetime.fromisoformat(considered_session).date().isoformat()
                != considered_session
            ):
                raise ValueError("invalid considered session")
        except ValueError as error:
            raise ValueError("invalid considered session") from error
        if type(tick_id) is not int or tick_id < 0:
            raise ValueError("invalid tick")
        if type(batch_ordinal) is not int or batch_ordinal < 0:
            raise ValueError("invalid batch ordinal")
        _ = iso(now)
        with self._transaction() as connection:
            candidate_row = connection.execute(
                "SELECT subject_id,kind,evidence_refs_json,assignment "
                "FROM candidate_manifest WHERE tick_id=? AND candidate_id=?",
                (tick_id, candidate_id),
            ).fetchone()
            if (
                candidate_row is None
                or candidate_row["assignment"] != "SAMPLED_AUDIT"
                or candidate_row["subject_id"] != subject_id
                or candidate_row["kind"] != material_kind.value
            ):
                raise OpsKernelTransitionError("audit candidate unavailable")

            batch = connection.execute(
                "SELECT provider,state,exit,candidate_ids_json,result_json "
                "FROM analysis_batches WHERE tick_id=? AND ordinal=?",
                (tick_id, batch_ordinal),
            ).fetchone()
            if (
                batch is None
                or batch["provider"] != "SOL"
                or batch["state"] != "SETTLED"
                or batch["exit"] != "OK"
            ):
                raise OpsKernelTransitionError("audit batch unavailable")
            try:
                batch_candidate_ids = _decode(batch["candidate_ids_json"])
                result = _decode(batch["result_json"])
                analyses = result["analyses"]
                matching = [
                    row
                    for row in analyses
                    if isinstance(row, Mapping)
                    and row.get("candidate_id") == candidate_id
                ]
            except (AttributeError, KeyError, TypeError, ValueError):
                raise OpsKernelTransitionError("audit result invalid") from None
            if candidate_id not in batch_candidate_ids or len(matching) != 1:
                raise OpsKernelTransitionError("audit result coverage invalid")
            citations = matching[0].get("cited_evidence_ids")
            try:
                evidence_rows = _decode(candidate_row["evidence_refs_json"])
                evidence_ids = {
                    row["evidence_id"] if isinstance(row, Mapping) else row
                    for row in evidence_rows
                }
            except (KeyError, TypeError, ValueError):
                raise OpsKernelTransitionError("audit evidence invalid") from None
            if (
                type(citations) is not list
                or len(citations) != len(set(citations))
                or not set(citations) <= evidence_ids
            ):
                raise OpsKernelTransitionError("audit citations invalid")

            backlog = connection.execute(
                "SELECT state FROM audit_backlog WHERE kind=? AND subject_id=?",
                (material_kind.value, subject_id),
            ).fetchone()
            if backlog is None or backlog["state"] != "PENDING_AUDIT":
                raise OpsKernelTransitionError("audit backlog unavailable")
            coverage = connection.execute(
                "SELECT audited_count FROM audit_coverage "
                "WHERE regular_session_date=? AND kind=?",
                (session_date, material_kind.value),
            ).fetchone()
            if coverage is None:
                raise OpsKernelTransitionError("audit coverage unavailable")
            total = connection.execute(
                "SELECT COALESCE(SUM(audited_count),0) FROM audit_coverage "
                "WHERE regular_session_date=?",
                (session_date,),
            ).fetchone()[0]
            if total + 1 > 5:
                raise OpsKernelBudgetExceeded("daily audit sample cap")

            connection.execute(
                "UPDATE audit_backlog SET state='AUDITED',last_considered_session=? "
                "WHERE kind=? AND subject_id=?",
                (considered_session, material_kind.value, subject_id),
            )
            connection.execute(
                "UPDATE audit_coverage SET audited_count=audited_count+1 "
                "WHERE regular_session_date=? AND kind=?",
                (session_date, material_kind.value),
            )
            stored_count = connection.execute(
                "SELECT audited_count FROM audit_coverage "
                "WHERE regular_session_date=? AND kind=?",
                (session_date, material_kind.value),
            ).fetchone()[0]
            return AuditCompletionV1(
                material_kind,
                subject_id,
                session_date,
                stored_count,
            )

    def write_audit_coverage(
        self,
        role,
        session_date,
        kind,
        eligible,
        audited,
        debt,
        oldest_wait,
        capacity_incident_opened,
    ):
        self._role(role, "write_audit_coverage")
        values = (eligible, audited, debt, oldest_wait)
        if any(type(value) is not int or value < 0 for value in values):
            raise ValueError("invalid audit counts")
        with self._transaction() as connection:
            connection.execute(
                "INSERT INTO audit_coverage VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(regular_session_date,kind) DO UPDATE SET "
                "eligible_count=excluded.eligible_count,audited_count=excluded.audited_count,"
                "debt_count=excluded.debt_count,"
                "oldest_wait_regular_sessions=excluded.oldest_wait_regular_sessions,"
                "capacity_incident_opened=excluded.capacity_incident_opened",
                (session_date, kind, *values, int(capacity_incident_opened)),
            )

    @staticmethod
    def _endpoint_values(endpoint):
        return (
            endpoint.source,
            endpoint.method,
            endpoint.host,
            endpoint.normalized_path_template,
        )

    def reserve_request(
        self,
        role,
        stable_phase_id,
        endpoint,
        development_limit,
        now,
        *,
        inject_crash_at=None,
    ):
        """LEGACY-ONLY G2 acceptance API: endpoint reservation telemetry.

        Sealed for bounded acceptance evidence; never an admission authority
        for the production cadence surface.
        """
        self._role(role, "reserve_request")
        if (stable_phase_id is None) != (development_limit is None):
            raise ValueError("development identity and limit must be paired")
        if (
            stable_phase_id is not None
            and stable_phase_id not in self._stable_phase_ids
        ):
            raise ValueError("unknown stable phase")
        if stable_phase_id is not None and (
            type(development_limit) is not int or development_limit < 1
        ):
            raise ValueError("invalid development limit")
        runtime_day = now.astimezone(UTC).date().isoformat()
        ep = self._endpoint_values(endpoint)
        with self._transaction() as connection:
            cooldown = connection.execute(
                "SELECT next_eligible_at FROM endpoint_cooldowns "
                "WHERE source=? AND method=? AND host=? AND normalized_path_template=?",
                ep,
            ).fetchone()
            if cooldown and parse_iso(cooldown[0]) > now:
                raise OpsKernelCooldownActive("endpoint cooldown active")
            if stable_phase_id is not None:
                development = connection.execute(
                    "SELECT started_count FROM development_request_reservations "
                    "WHERE stable_phase_id=? AND source=? AND method=? AND host=? "
                    "AND normalized_path_template=?",
                    (stable_phase_id, *ep),
                ).fetchone()
                if (development[0] if development else 0) >= development_limit:
                    raise OpsKernelBudgetExceeded("development request budget exceeded")
            timestamp = iso(now)
            connection.execute(
                "INSERT INTO request_day_counters VALUES(?,1,?,?) "
                "ON CONFLICT(utc_day) DO UPDATE SET "
                "provider_started_count=provider_started_count+1,"
                "updated_at=excluded.updated_at",
                (runtime_day, timestamp, timestamp),
            )
            connection.execute(
                "INSERT INTO runtime_request_reservations VALUES(?,?,?,?,?,1) "
                "ON CONFLICT(runtime_day,source,method,host,normalized_path_template) "
                "DO UPDATE SET started_count=started_count+1",
                (runtime_day, *ep),
            )
            if inject_crash_at == "after_runtime":
                raise RuntimeError("injected")
            if stable_phase_id is not None:
                connection.execute(
                    "INSERT INTO development_request_reservations VALUES(?,?,?,?,?,1) "
                    "ON CONFLICT(stable_phase_id,source,method,host,normalized_path_template) "
                    "DO UPDATE SET started_count=started_count+1",
                    (stable_phase_id, *ep),
                )

    def record_cooldown(self, role, endpoint, next_eligible_at, reason, now):
        self._role(role, "record_cooldown")
        ep = self._endpoint_values(endpoint)
        with self._transaction() as connection:
            old = connection.execute(
                "SELECT next_eligible_at FROM endpoint_cooldowns WHERE "
                "source=? AND method=? AND host=? AND normalized_path_template=?",
                ep,
            ).fetchone()
            maximum = (
                max(parse_iso(old[0]), next_eligible_at) if old else next_eligible_at
            )
            connection.execute(
                "INSERT INTO endpoint_cooldowns VALUES(?,?,?,?,?,?,?) "
                "ON CONFLICT(source,method,host,normalized_path_template) DO UPDATE SET "
                "next_eligible_at=excluded.next_eligible_at,reason=excluded.reason,"
                "updated_at=excluded.updated_at",
                (*ep, iso(maximum), reason, iso(now)),
            )

    def read_reservation_counts(self, stable_phase_id, endpoint, now):
        runtime_day = now.astimezone(UTC).date().isoformat()
        ep = self._endpoint_values(endpoint)
        with self._read() as connection:
            runtime = connection.execute(
                "SELECT provider_started_count FROM request_day_counters WHERE utc_day=?",
                (runtime_day,),
            ).fetchone()
            development = None
            if stable_phase_id is not None:
                row = connection.execute(
                    "SELECT started_count FROM development_request_reservations WHERE "
                    "stable_phase_id=? AND source=? AND method=? AND host=? "
                    "AND normalized_path_template=?",
                    (stable_phase_id, *ep),
                ).fetchone()
                development = row[0] if row else 0
        return ReservationCountsV1(runtime[0] if runtime else 0, development)

    def append_feedback(self, role, row):
        self._role(role, "append_feedback")
        values = (
            row.feedback_id,
            row.provider,
            row.provider_update_id,
            row.provider_message_ref,
            row.sender_ref,
            row.thread_ref,
            iso(row.received_at),
            row.raw_text,
            row.context_ref,
            row.capture_source,
            row.raw_hash,
            row.event_kind.value,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM feedback_inbound WHERE provider=? "
                "AND provider_update_id=? AND event_kind=?",
                (row.provider, row.provider_update_id, row.event_kind.value),
            ).fetchone()
            if existing:
                if tuple(existing) != values:
                    raise OpsKernelConflictError("feedback identity conflict")
                return ReplayResult.DUPLICATE
            if connection.execute(
                "SELECT 1 FROM feedback_inbound WHERE feedback_id=?", (row.feedback_id,)
            ).fetchone():
                raise OpsKernelConflictError("feedback identity conflict")
            if row.event_kind in {FeedbackEventKind.EDIT, FeedbackEventKind.DELETE}:
                linked = connection.execute(
                    "SELECT 1 FROM feedback_inbound WHERE provider=? "
                    "AND provider_message_ref=? AND event_kind='CREATE'",
                    (row.provider, row.provider_message_ref),
                ).fetchone()
                if linked is None:
                    raise OpsKernelTransitionError("feedback edit linkage absent")
            connection.execute(
                "INSERT INTO feedback_inbound VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", values
            )
            return ReplayResult.CREATED

    @staticmethod
    def _feedback(row):
        return FeedbackInboundV1(
            row["feedback_id"],
            row["provider"],
            row["provider_update_id"],
            row["provider_message_ref"],
            row["sender_ref"],
            row["thread_ref"],
            parse_iso(row["received_at"]),
            row["raw_text"],
            row["context_ref"],
            row["capture_source"],
            row["raw_hash"],
            FeedbackEventKind(row["event_kind"]),
        )

    def read_feedback_private(self, feedback_id):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM feedback_inbound WHERE feedback_id=?", (feedback_id,)
            ).fetchone()
        return None if row is None else self._feedback(row)

    # ------------------------------------------------------------------
    # Cadence admission authority (SDS §7.3): versioned tick pinning and
    # the durable PLANNED -> STARTED -> SUCCEEDED/FAILED/UNKNOWN wire-start
    # state machine, in the same SQLite transactional authority as every
    # other kernel mutation. One BEGIN IMMEDIATE transaction performs
    # planned-row/native-identity validation -> pinned-policy match ->
    # total/group/child STARTED counts -> ceiling comparisons -> STARTED
    # transition. Any failure mutates nothing and wire I/O stays zero.
    # ------------------------------------------------------------------

    _CADENCE_SETTLE_OUTCOMES = ("SUCCEEDED", "FAILED", "UNKNOWN")

    @staticmethod
    def _cadence_slot(policy, scheduled_for_utc, child_limits):
        if not isinstance(policy, CadencePolicyV1):
            raise CadenceBindingError(
                "versioned tick binding requires a cadence policy"
            )
        if not isinstance(child_limits, TradesChildLimitsV1):
            raise CadenceBindingError("invalid trades child limits")
        if (
            not isinstance(scheduled_for_utc, datetime)
            or scheduled_for_utc.tzinfo is None
            or scheduled_for_utc.utcoffset() is None
        ):
            raise CadenceBindingError("scheduled_for_utc must be an aware datetime")
        if scheduled_for_utc.microsecond != 0 or (
            int(scheduled_for_utc.timestamp()) % policy.cadence_seconds != 0
        ):
            raise CadenceBindingError("scheduled slot off the policy interval grid")
        try:
            policy_snapshot(policy, child_limits)
        except ValueError as error:
            raise CadenceBindingError("infeasible policy snapshot") from error
        return iso(scheduled_for_utc)

    @staticmethod
    def _cadence_operation(operation, native_identity):
        try:
            return validate_cadence_operation(operation, native_identity)
        except ValueError as error:
            raise CadenceBindingError(str(error)) from error

    def _pin_or_match_cadence(self, connection, policy, slot_iso, child_limits, now):
        digest = policy_digest(policy, child_limits)
        snapshot_json = canonical_encode(policy_snapshot(policy, child_limits)).decode()
        row = connection.execute(
            "SELECT policy_digest,snapshot_json FROM cadence_ticks "
            "WHERE policy_version=? AND scheduled_for_utc=?",
            (policy.policy_version, slot_iso),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO cadence_ticks VALUES(?,?,?,?,?)",
                (policy.policy_version, slot_iso, digest, snapshot_json, iso(now)),
            )
            return canonical_decode(snapshot_json.encode(), max_bytes=64 * 1024)
        if row["policy_digest"] != digest or row["snapshot_json"] != snapshot_json:
            raise CadencePolicyMismatchError(
                "policy does not match pinned tick snapshot"
            )
        return canonical_decode(row["snapshot_json"].encode(), max_bytes=64 * 1024)

    @staticmethod
    def _cadence_wire_row(row):
        return {
            "policy_version": row["policy_version"],
            "scheduled_for_utc": parse_iso(row["scheduled_for_utc"]),
            "operation_group": row["operation_group"],
            "operation": row["operation"],
            "native_identity": row["native_identity"],
            "state": row["state"],
            "planned_at": parse_iso(row["planned_at"]),
            "started_at": parse_iso(row["started_at"]),
            "settled_at": parse_iso(row["settled_at"]),
        }

    def pin_cadence_tick(
        self, role, policy, *, scheduled_for_utc, now, child_limits=None
    ) -> None:
        """Pin (or verify) a tick's policy snapshot without admitting any call."""
        self._role(role, "pin_cadence_tick")
        if child_limits is None:
            child_limits = TradesChildLimitsV1()
        slot_iso = self._cadence_slot(policy, scheduled_for_utc, child_limits)
        iso(now)
        with self._transaction() as connection:
            self._pin_or_match_cadence(connection, policy, slot_iso, child_limits, now)

    def plan_cadence_requests(
        self, role, policy, *, scheduled_for_utc, requests, now, child_limits=None
    ) -> ReplayResult:
        """Persist durable PLANNED wire rows for a closed set of operations."""
        self._role(role, "plan_cadence_requests")
        if child_limits is None:
            child_limits = TradesChildLimitsV1()
        slot_iso = self._cadence_slot(policy, scheduled_for_utc, child_limits)
        planned_at = iso(now)
        validated = []
        for request in tuple(requests):
            operation, native_identity = request
            group = self._cadence_operation(operation, native_identity)
            validated.append((group, operation, native_identity))
        identities = [row[2] for row in validated]
        if len(set(identities)) != len(identities):
            raise CadenceBindingError("duplicate native identity in plan")
        with self._transaction() as connection:
            self._pin_or_match_cadence(connection, policy, slot_iso, child_limits, now)
            outcome = ReplayResult.DUPLICATE
            for group, operation, native_identity in validated:
                prior = connection.execute(
                    "SELECT operation_group,operation FROM cadence_wire_starts "
                    "WHERE policy_version=? AND scheduled_for_utc=? AND native_identity=?",
                    (policy.policy_version, slot_iso, native_identity),
                ).fetchone()
                if prior is not None:
                    if (prior["operation_group"], prior["operation"]) != (
                        group,
                        operation,
                    ):
                        raise CadenceDuplicateError(
                            "conflicting planned cadence identity"
                        )
                    continue
                connection.execute(
                    "INSERT INTO cadence_wire_starts VALUES(?,?,?,?,?,'PLANNED',?,NULL,NULL)",
                    (
                        policy.policy_version,
                        slot_iso,
                        group,
                        operation,
                        native_identity,
                        planned_at,
                    ),
                )
                outcome = ReplayResult.CREATED
            return outcome

    def _admit_cadence_start(
        self,
        connection,
        policy,
        slot_iso,
        group,
        operation,
        native_identity,
        child_limits,
        now,
    ):
        """Shared STARTED transition; caller already opened BEGIN IMMEDIATE.

        Requires the exact durable PLANNED row (or, for calendar attempts, a
        row inserted by the same transaction) and enforces total/group/child
        ceilings with values bound from the pinned snapshot; no limit literal
        is copied into this module.
        """
        pinned = self._pin_or_match_cadence(
            connection,
            policy,
            slot_iso,
            child_limits,
            now,
        )
        row = connection.execute(
            "SELECT * FROM cadence_wire_starts WHERE policy_version=? "
            "AND scheduled_for_utc=? AND native_identity=?",
            (policy.policy_version, slot_iso, native_identity),
        ).fetchone()
        if row is None:
            raise CadenceTransitionError("cadence request not planned")
        if (row["operation_group"], row["operation"]) != (group, operation):
            raise CadenceTransitionError("planned cadence identity conflict")
        if row["state"] != "PLANNED":
            raise CadenceDuplicateError("duplicate wire start for this identity")
        limits = pinned["request_limits"]
        child_field = CADENCE_CHILD_LIMIT_FIELDS.get(operation)
        child_limit = (
            None if child_field is None else pinned["trades_child_limits"][child_field]
        )
        total_ok, group_ok, child_ok = connection.execute(
            "SELECT"
            " (SELECT COUNT(*) FROM cadence_wire_starts"
            "  WHERE policy_version=:v AND scheduled_for_utc=:s"
            "  AND state<>'PLANNED') < :total_limit,"
            " (SELECT COUNT(*) FROM cadence_wire_starts"
            "  WHERE policy_version=:v AND scheduled_for_utc=:s"
            "  AND operation_group=:g AND state<>'PLANNED') < :group_limit,"
            " CASE WHEN :child_limit IS NULL THEN 1 ELSE"
            "  (SELECT COUNT(*) FROM cadence_wire_starts"
            "   WHERE policy_version=:v AND scheduled_for_utc=:s"
            "   AND operation=:o AND state<>'PLANNED') < :child_limit END",
            {
                "v": policy.policy_version,
                "s": slot_iso,
                "g": group,
                "o": operation,
                "total_limit": limits["total_wire_starts"],
                # The pinned snapshot format is frozen for digest stability;
                # a group absent from it (CONDITIONAL_ORDER_HISTORY) has a
                # structural zero budget in the 300s lane.
                "group_limit": limits.get(GROUP_LIMIT_FIELDS[group], 0),
                "child_limit": child_limit,
            },
        ).fetchone()
        if not total_ok:
            raise CadenceBudgetExceeded("total wire-start ceiling reached")
        if not group_ok:
            raise CadenceBudgetExceeded("operation group ceiling reached")
        if not child_ok:
            raise CadenceBudgetExceeded("trades child ceiling reached")
        connection.execute(
            "UPDATE cadence_wire_starts SET state='STARTED',started_at=? "
            "WHERE policy_version=? AND scheduled_for_utc=? AND native_identity=?",
            (iso(now), policy.policy_version, slot_iso, native_identity),
        )
        fresh = connection.execute(
            "SELECT * FROM cadence_wire_starts WHERE policy_version=? "
            "AND scheduled_for_utc=? AND native_identity=?",
            (policy.policy_version, slot_iso, native_identity),
        ).fetchone()
        return self._cadence_wire_row(fresh)

    def start_cadence_request(
        self,
        role,
        policy,
        *,
        scheduled_for_utc,
        operation,
        native_identity,
        now,
        child_limits=None,
    ) -> dict:
        self._role(role, "start_cadence_request")
        if child_limits is None:
            child_limits = TradesChildLimitsV1()
        slot_iso = self._cadence_slot(policy, scheduled_for_utc, child_limits)
        group = self._cadence_operation(operation, native_identity)
        iso(now)
        with self._transaction() as connection:
            return self._admit_cadence_start(
                connection,
                policy,
                slot_iso,
                group,
                operation,
                native_identity,
                child_limits,
                now,
            )

    def settle_cadence_request(
        self, role, *, policy_version, scheduled_for_utc, native_identity, outcome, now
    ):
        self._role(role, "settle_cadence_request")
        if outcome not in self._CADENCE_SETTLE_OUTCOMES:
            raise CadenceTransitionError("invalid cadence settlement outcome")
        if (
            not isinstance(scheduled_for_utc, datetime)
            or scheduled_for_utc.tzinfo is None
            or scheduled_for_utc.utcoffset() is None
        ):
            raise CadenceBindingError("scheduled_for_utc must be an aware datetime")
        slot_iso = iso(scheduled_for_utc)
        settled_at = iso(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM cadence_wire_starts WHERE policy_version=? "
                "AND scheduled_for_utc=? AND native_identity=?",
                (policy_version, slot_iso, native_identity),
            ).fetchone()
            if row is None:
                raise CadenceTransitionError("cadence request unavailable")
            if row["state"] != "STARTED":
                if row["state"] == outcome:
                    return ReplayResult.DUPLICATE
                raise CadenceTransitionError("conflicting cadence settlement")
            connection.execute(
                "UPDATE cadence_wire_starts SET state=?,settled_at=? "
                "WHERE policy_version=? AND scheduled_for_utc=? AND native_identity=?",
                (outcome, settled_at, policy_version, slot_iso, native_identity),
            )
            fresh = connection.execute(
                "SELECT * FROM cadence_wire_starts WHERE policy_version=? "
                "AND scheduled_for_utc=? AND native_identity=?",
                (policy_version, slot_iso, native_identity),
            ).fetchone()
            return self._cadence_wire_row(fresh)

    def reconcile_cadence(self, role, *, now) -> dict:
        """Settle every stale STARTED cadence row to UNKNOWN before new starts.

        Runs at startup of a new invocation: any row still STARTED belongs to
        a dead process, keeps its consumed identity/ordinal, and is never
        restarted.
        """
        self._role(role, "reconcile_cadence")
        settled_at = iso(now)
        with self._transaction() as connection:
            wire_keys = tuple(
                connection.execute(
                    "SELECT policy_version,scheduled_for_utc,native_identity "
                    "FROM cadence_wire_starts WHERE state='STARTED' "
                    "ORDER BY policy_version,scheduled_for_utc,native_identity"
                ).fetchall()
            )
            connection.execute(
                "UPDATE cadence_wire_starts SET state='UNKNOWN',settled_at=? "
                "WHERE state='STARTED'",
                (settled_at,),
            )
            wire_rows = tuple(
                self._cadence_wire_row(
                    connection.execute(
                        "SELECT * FROM cadence_wire_starts WHERE policy_version=? "
                        "AND scheduled_for_utc=? AND native_identity=?",
                        tuple(key),
                    ).fetchone()
                )
                for key in wire_keys
            )
            calendar_rows = self._reconcile_calendar_attempts(connection, now)
            return {"wire_starts": wire_rows, "calendar_attempts": calendar_rows}

    def read_cadence_tick_report(
        self, policy_version, scheduled_for_utc
    ) -> dict | None:
        if (
            not isinstance(scheduled_for_utc, datetime)
            or scheduled_for_utc.tzinfo is None
            or scheduled_for_utc.utcoffset() is None
        ):
            raise CadenceBindingError("scheduled_for_utc must be an aware datetime")
        slot_iso = iso(scheduled_for_utc)
        with self._read() as connection:
            tick = connection.execute(
                "SELECT * FROM cadence_ticks WHERE policy_version=? AND scheduled_for_utc=?",
                (policy_version, slot_iso),
            ).fetchone()
            if tick is None:
                return None
            groups = dict.fromkeys(GROUP_LIMIT_FIELDS, 0)
            groups.update(
                connection.execute(
                    "SELECT operation_group,COUNT(*) FROM cadence_wire_starts "
                    "WHERE policy_version=? AND scheduled_for_utc=? AND state<>'PLANNED' "
                    "GROUP BY operation_group",
                    (policy_version, slot_iso),
                ).fetchall()
            )
            operations = dict(
                connection.execute(
                    "SELECT operation,COUNT(*) FROM cadence_wire_starts "
                    "WHERE policy_version=? AND scheduled_for_utc=? AND state<>'PLANNED' "
                    "GROUP BY operation",
                    (policy_version, slot_iso),
                ).fetchall()
            )
            states = dict(
                connection.execute(
                    "SELECT state,COUNT(*) FROM cadence_wire_starts "
                    "WHERE policy_version=? AND scheduled_for_utc=? "
                    "GROUP BY state",
                    (policy_version, slot_iso),
                ).fetchall()
            )
        return {
            "policy_version": policy_version,
            "scheduled_for_utc": parse_iso(slot_iso),
            "policy_digest": tick["policy_digest"],
            "snapshot": canonical_decode(
                tick["snapshot_json"].encode(),
                max_bytes=64 * 1024,
            ),
            "total_started": sum(groups.values()),
            "group_started": groups,
            "operation_started": operations,
            "state_counts": states,
        }

    # ------------------------------------------------------------------
    # Calendar retry ledger (SDS §4): planned slot identity
    # (market, market_local_date, eligible_slot_utc); wire identity
    # (market, market_local_date, ordinal 0..5) allocated only at STARTED.
    # The retry identity is native identity only — every start still passes
    # the same versioned tick binding and total/group admission above.
    # ------------------------------------------------------------------

    @staticmethod
    def _calendar_identity(market, market_local_date):
        if not isinstance(market, Market):
            raise CadenceBindingError("unknown market")
        if not isinstance(market_local_date, date) or isinstance(
            market_local_date, datetime
        ):
            raise CadenceBindingError("market local date must be a date")
        return market.value, market_local_date.isoformat()

    @staticmethod
    def _aware_slot(value, name):
        if (
            not isinstance(value, datetime)
            or value.tzinfo is None
            or value.utcoffset() is None
        ):
            raise CadenceBindingError(f"{name} must be an aware datetime")

    def plan_calendar_slot(
        self, role, market, market_local_date, eligible_slot_utc, *, now
    ) -> ReplayResult:
        self._role(role, "plan_calendar_slot")
        market_value, local_date = self._calendar_identity(market, market_local_date)
        self._aware_slot(eligible_slot_utc, "eligible_slot_utc")
        slot_iso = iso(eligible_slot_utc)
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT 1 FROM cadence_calendar_slots WHERE market=? "
                "AND market_local_date=? AND eligible_slot_utc=?",
                (market_value, local_date, slot_iso),
            ).fetchone()
            if existing:
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO cadence_calendar_slots VALUES(?,?,?,'PLANNED',NULL,?)",
                (market_value, local_date, slot_iso, iso(now)),
            )
            return ReplayResult.CREATED

    def start_calendar_attempt(
        self,
        role,
        policy,
        *,
        scheduled_for_utc,
        market,
        market_local_date,
        eligible_slot_utc,
        now,
        child_limits=None,
    ) -> CalendarStartReceiptV1:
        self._role(role, "start_calendar_attempt")
        if child_limits is None:
            child_limits = TradesChildLimitsV1()
        market_value, local_date = self._calendar_identity(market, market_local_date)
        tick_slot_iso = self._cadence_slot(policy, scheduled_for_utc, child_limits)
        self._aware_slot(eligible_slot_utc, "eligible_slot_utc")
        eligible_iso = iso(eligible_slot_utc)
        operation = f"CALENDAR_DAY_{market_value}"
        with self._transaction() as connection:
            self._pin_or_match_cadence(
                connection,
                policy,
                tick_slot_iso,
                child_limits,
                now,
            )
            slot = connection.execute(
                "SELECT * FROM cadence_calendar_slots WHERE market=? "
                "AND market_local_date=? AND eligible_slot_utc=?",
                (market_value, local_date, eligible_iso),
            ).fetchone()
            if slot is None:
                raise CadenceTransitionError("calendar slot not planned")
            if slot["state"] != "PLANNED":
                raise CadenceDuplicateError("calendar slot already started")
            if connection.execute(
                "SELECT 1 FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=? AND state='SUCCEEDED'",
                (market_value, local_date),
            ).fetchone():
                raise CadenceCalendarClosedError("calendar identity already succeeded")
            if connection.execute(
                "SELECT 1 FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=? AND state='STARTED'",
                (market_value, local_date),
            ).fetchone():
                raise CadenceTransitionError("previous calendar attempt unreconciled")
            consumed = connection.execute(
                "SELECT COUNT(*) FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=?",
                (market_value, local_date),
            ).fetchone()[0]
            if consumed >= CALENDAR_ATTEMPT_MAX:
                raise CadenceCalendarExhaustedError(
                    "six calendar attempts consumed for this market-local date"
                )
            last = connection.execute(
                "SELECT next_eligible_at FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=? ORDER BY attempt_ordinal DESC LIMIT 1",
                (market_value, local_date),
            ).fetchone()
            if last is not None and last[0] is not None and parse_iso(last[0]) > now:
                raise CadenceCalendarBackoffError("calendar retry backoff active")
            ordinal = consumed
            native_identity = f"calendar:{market_value}:{local_date}:{ordinal}"
            self._cadence_operation(operation, native_identity)
            connection.execute(
                "INSERT INTO cadence_calendar_attempts "
                "VALUES(?,?,?,'STARTED',?,?,?,?,NULL,NULL,NULL,NULL)",
                (
                    market_value,
                    local_date,
                    ordinal,
                    eligible_iso,
                    policy.policy_version,
                    tick_slot_iso,
                    iso(now),
                ),
            )
            connection.execute(
                "UPDATE cadence_calendar_slots SET state='STARTED',attempt_ordinal=? "
                "WHERE market=? AND market_local_date=? AND eligible_slot_utc=?",
                (ordinal, market_value, local_date, eligible_iso),
            )
            # The ordinal exists only now, so the wire row is planned and
            # started inside this same admission transaction; the durable
            # planned row for calendar is the retry slot above (SDS §4).
            connection.execute(
                "INSERT INTO cadence_wire_starts VALUES(?,?,?,?,?,'PLANNED',?,NULL,NULL)",
                (
                    policy.policy_version,
                    tick_slot_iso,
                    "CALENDAR",
                    operation,
                    native_identity,
                    iso(now),
                ),
            )
            self._admit_cadence_start(
                connection,
                policy,
                tick_slot_iso,
                "CALENDAR",
                operation,
                native_identity,
                child_limits,
                now,
            )
            return CalendarStartReceiptV1(
                policy.policy_version,
                parse_iso(tick_slot_iso),
                market,
                market_local_date,
                ordinal,
                native_identity,
                "STARTED",
            )

    def settle_calendar_attempt(
        self, role, market, market_local_date, attempt_ordinal, outcome, *, now
    ):
        self._role(role, "settle_calendar_attempt")
        if outcome not in self._CADENCE_SETTLE_OUTCOMES:
            raise CadenceTransitionError("invalid calendar attempt outcome")
        market_value, local_date = self._calendar_identity(market, market_local_date)
        if (
            type(attempt_ordinal) is not int
            or not 0 <= attempt_ordinal < CALENDAR_ATTEMPT_MAX
        ):
            raise CadenceTransitionError("invalid calendar attempt ordinal")
        settled_at = iso(now)
        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=? AND attempt_ordinal=?",
                (market_value, local_date, attempt_ordinal),
            ).fetchone()
            if row is None:
                raise CadenceTransitionError("calendar attempt unavailable")
            if row["state"] != "STARTED":
                if row["state"] == outcome:
                    return ReplayResult.DUPLICATE
                raise CadenceTransitionError("conflicting calendar attempt settlement")
            next_eligible = None
            if outcome in ("FAILED", "UNKNOWN") and attempt_ordinal < len(
                CALENDAR_RETRY_DELAY_MINUTES
            ):
                next_eligible = iso(
                    now
                    + timedelta(minutes=CALENDAR_RETRY_DELAY_MINUTES[attempt_ordinal])
                )
            connection.execute(
                "UPDATE cadence_calendar_attempts SET state=?,settled_at=?,"
                "next_eligible_at=? WHERE market=? AND market_local_date=? "
                "AND attempt_ordinal=?",
                (
                    outcome,
                    settled_at,
                    next_eligible,
                    market_value,
                    local_date,
                    attempt_ordinal,
                ),
            )
            connection.execute(
                "UPDATE cadence_wire_starts SET state=?,settled_at=? "
                "WHERE policy_version=? AND scheduled_for_utc=? "
                "AND native_identity=? AND state='STARTED'",
                (
                    outcome,
                    settled_at,
                    row["policy_version"],
                    row["scheduled_for_utc"],
                    f"calendar:{market_value}:{local_date}:{attempt_ordinal}",
                ),
            )
            return {
                "market": market,
                "market_local_date": market_local_date,
                "attempt_ordinal": attempt_ordinal,
                "state": outcome,
                "settled_at": parse_iso(settled_at),
                "next_eligible_at": parse_iso(next_eligible),
            }

    def _calendar_receipt_rows(self, connection, receipt):
        """Exact durable rows behind a receipt; every field must match and
        both the attempt and its wire start must currently be STARTED.

        The typed dataclass is never authority on its own (B1): a receipt is
        genuine only while these rows exist in this database.
        """
        if type(receipt) is not CalendarStartReceiptV1:
            raise CadenceBindingError("calendar receipt must be kernel-issued")
        market_value, local_date = self._calendar_identity(
            receipt.market,
            receipt.market_local_date,
        )
        slot_iso = iso(receipt.scheduled_for_utc)
        attempt = connection.execute(
            "SELECT * FROM cadence_calendar_attempts WHERE market=? "
            "AND market_local_date=? AND attempt_ordinal=?",
            (market_value, local_date, receipt.attempt_ordinal),
        ).fetchone()
        if attempt is None:
            raise CadenceTransitionError("calendar receipt has no attempt row")
        if (
            attempt["policy_version"] != receipt.policy_version
            or attempt["scheduled_for_utc"] != slot_iso
        ):
            raise CadenceTransitionError("calendar receipt does not match its rows")
        if attempt["state"] != "STARTED":
            raise CadenceTransitionError("calendar receipt attempt is terminal")
        wire = connection.execute(
            "SELECT state FROM cadence_wire_starts WHERE policy_version=? "
            "AND scheduled_for_utc=? AND native_identity=?",
            (receipt.policy_version, slot_iso, receipt.native_identity),
        ).fetchone()
        if wire is None or wire["state"] != "STARTED":
            raise CadenceTransitionError("calendar receipt has no started wire row")
        return attempt, market_value, local_date

    def claim_calendar_provider(self, receipt, *, now) -> None:
        """Atomic one-time provider claim for one STARTED calendar receipt.

        Exactly one provider call per kernel-issued receipt (B1): the claim
        validates every receipt field against the current planned/STARTED
        attempt and wire rows in this same database/transaction and marks the
        claim durably. A forged, missing, mismatched, terminal, or already
        claimed receipt fails, so the caller performs provider wire zero.
        """
        claimed_at = iso(now)
        with self._transaction() as connection:
            attempt, market_value, local_date = self._calendar_receipt_rows(
                connection,
                receipt,
            )
            if attempt["provider_claimed_at"] is not None:
                raise CadenceDuplicateError("calendar provider claim already consumed")
            connection.execute(
                "UPDATE cadence_calendar_attempts SET provider_claimed_at=? "
                "WHERE market=? AND market_local_date=? AND attempt_ordinal=?",
                (claimed_at, market_value, local_date, receipt.attempt_ordinal),
            )

    def verify_calendar_history_receipt(self, receipt, *, now) -> None:
        """Read-only provenance check for a backfilled history append (M2).

        The receipt must correspond to the exact currently STARTED attempt
        and wire rows in this database with a consumed provider claim — the
        same rows that admitted the calendar wire whose official fact the
        history store is about to persist. A forged, mismatched, terminal,
        or unclaimed receipt fails, so the history append writes zero.
        """
        iso(now)
        with self._read() as connection:
            attempt, _market_value, _local_date = self._calendar_receipt_rows(
                connection,
                receipt,
            )
            if attempt["provider_claimed_at"] is None:
                raise CadenceTransitionError(
                    "history append requires a claimed calendar receipt"
                )

    def read_settled_calendar_provenance(
        self, market, market_local_date
    ) -> dict | None:
        """Read-only durable provenance of a SUCCEEDED calendar identity (M2).

        Returns the identity of the one SUCCEEDED attempt for
        ``(market, market_local_date)`` whose provider claim was consumed —
        the admission behind the current cache entry — or ``None`` when no
        such official wire exists (the caller must then never forge a
        history row for this date).
        """
        market_value, local_date = self._calendar_identity(market, market_local_date)
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM cadence_calendar_attempts WHERE market=? "
                "AND market_local_date=? AND state='SUCCEEDED'",
                (market_value, local_date),
            ).fetchone()
        if row is None or row["provider_claimed_at"] is None:
            return None
        return {
            "market": market,
            "market_local_date": market_local_date,
            "attempt_ordinal": row["attempt_ordinal"],
            "native_identity": (
                f"calendar:{market_value}:{local_date}:{row['attempt_ordinal']}"
            ),
            "policy_version": row["policy_version"],
            "scheduled_for_utc": parse_iso(row["scheduled_for_utc"]),
        }

    def consume_calendar_cache_commit(self, receipt, *, now) -> None:
        """Atomic one-time cache-commit consumption for a claimed receipt.

        The calendar cache validates against this before replacing a market
        entry (B1): it requires the same exact STARTED rows, a consumed
        provider claim, and no prior cache commit. Exactly one cache update
        per receipt; reuse and forgery mutate cache zero.
        """
        committed_at = iso(now)
        with self._transaction() as connection:
            attempt, market_value, local_date = self._calendar_receipt_rows(
                connection,
                receipt,
            )
            if attempt["provider_claimed_at"] is None:
                raise CadenceTransitionError(
                    "calendar cache commit requires a claimed receipt"
                )
            if attempt["cache_committed_at"] is not None:
                raise CadenceDuplicateError("calendar cache commit already consumed")
            connection.execute(
                "UPDATE cadence_calendar_attempts SET cache_committed_at=? "
                "WHERE market=? AND market_local_date=? AND attempt_ordinal=?",
                (committed_at, market_value, local_date, receipt.attempt_ordinal),
            )

    @staticmethod
    def _reconcile_calendar_attempts(connection, now):
        """Settle stale STARTED calendar attempts to UNKNOWN at the SAME
        ordinal; normal backoff then permits ordinal N+1, never a restart."""
        settled_at = iso(now)
        keys = tuple(
            connection.execute(
                "SELECT market,market_local_date,attempt_ordinal "
                "FROM cadence_calendar_attempts WHERE state='STARTED' "
                "ORDER BY market,market_local_date,attempt_ordinal"
            ).fetchall()
        )
        rows = []
        for key in keys:
            market_value, local_date, ordinal = tuple(key)
            next_eligible = None
            if ordinal < len(CALENDAR_RETRY_DELAY_MINUTES):
                next_eligible = iso(
                    now + timedelta(minutes=CALENDAR_RETRY_DELAY_MINUTES[ordinal])
                )
            connection.execute(
                "UPDATE cadence_calendar_attempts SET state='UNKNOWN',settled_at=?,"
                "next_eligible_at=? WHERE market=? AND market_local_date=? "
                "AND attempt_ordinal=?",
                (settled_at, next_eligible, market_value, local_date, ordinal),
            )
            rows.append(
                {
                    "market": Market(market_value),
                    "market_local_date": date.fromisoformat(local_date),
                    "attempt_ordinal": ordinal,
                    "state": "UNKNOWN",
                    "settled_at": parse_iso(settled_at),
                    "next_eligible_at": parse_iso(next_eligible),
                }
            )
        return tuple(rows)

    def referenced_pack_ticks(self):
        result = {job: set() for job in JobKind}
        with self._read() as connection:
            rows = connection.execute(
                "SELECT DISTINCT r.material_ticks_json FROM manifest_runs r "
                "JOIN candidate_manifest c ON c.tick_id=r.tick_id "
                "WHERE r.state IN ('OPEN','SEALED') AND c.state IN ('PENDING','DEFERRED') "
                "UNION SELECT DISTINCT d.material_ticks_json FROM dispositions d "
                "JOIN alert_outbox a ON a.decision_id=d.decision_id "
                "WHERE a.channel='TELEGRAM' AND a.resolution='UNRESOLVED' "
                "UNION SELECT DISTINCT material_ticks_json FROM deferred_candidates "
                "WHERE terminal_state IS NULL"
            ).fetchall()
        for row in rows:
            for job, tick in _ticks(row[0]).items():
                if tick is not None:
                    result[job].add(tick)
        return {job: frozenset(ticks) for job, ticks in result.items()}

    # --- YouTube long-term hypothesis tracking (schema v10) ------------------

    def _hypothesis_row(self, row):
        return HypothesisV1(
            hypothesis_id=row["hypothesis_id"],
            state=HypothesisState(row["state"]),
            statement=row["statement"],
            trigger_kind=TriggerKind(row["trigger_kind"]),
            trigger_expression=row["trigger_expression"],
            supporting_evidence=tuple(_decode(row["supporting_evidence_json"])),
            counter_evidence=tuple(_decode(row["counter_evidence_json"])),
            invalidation_conditions=tuple(_decode(row["invalidation_conditions_json"])),
            next_check_at=parse_iso(row["next_check_at"]),
            portfolio_touch=row["portfolio_touch"],
            origin=row["origin"],
            provenance_first=row["provenance_first"],
            superseded_by=row["superseded_by"],
            created_at=parse_iso(row["created_at"]),
            updated_at=parse_iso(row["updated_at"]),
        )

    def upsert_hypothesis(self, role, candidate, *, now):
        """Admit or revise one tracking candidate idempotently.

        M1: a historical replay (sighting already recorded) returns DUPLICATE
        BEFORE any terminal/supersession guard fires — re-observing an
        already-recorded report is always a no-op. New sightings on open
        hypotheses append revisions; new sightings on terminal/superseded
        hypotheses are transition errors (they carry genuinely new evidence
        the caller must route through an explicit lifecycle decision).
        """
        self._role(role, "upsert_hypothesis")
        if not isinstance(candidate, TrackingCandidateV1):
            raise ValueError("invalid tracking candidate")
        _aware_now = iso(now)
        identity = candidate.identity_digest
        hypothesis_id = f"hypothesis:{identity[:24]}"
        sighting = HypothesisSightingV1(
            hypothesis_id=hypothesis_id,
            source_kind="REPORT",
            source_ref=candidate.source_ref,
            revision=candidate.revision,
            payload_digest=candidate.payload_digest,
            evidence_delta=(),
            observed_at=candidate.observed_at,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM hypotheses WHERE identity_digest=?",
                (identity,),
            ).fetchone()
            seen = connection.execute(
                "SELECT sighting_id FROM hypothesis_sightings WHERE sighting_id=?",
                (sighting.sighting_id,),
            ).fetchone()
            if existing is None:
                if seen is not None:
                    raise OpsKernelCorruptError("hypothesis history torn")
                connection.execute(
                    "INSERT INTO hypotheses(hypothesis_id,identity_digest,state,"
                    "statement,trigger_kind,trigger_expression,"
                    "supporting_evidence_json,counter_evidence_json,"
                    "invalidation_conditions_json,next_check_at,portfolio_touch,"
                    "origin,provenance_first,superseded_by,created_at,updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,NULL,?,?)",
                    (
                        hypothesis_id,
                        identity,
                        HypothesisState.CANDIDATE.value,
                        candidate.statement,
                        candidate.trigger_kind.value,
                        candidate.trigger_expression.strip(),
                        _json(candidate.supporting_evidence),
                        _json(candidate.counter_evidence),
                        _json(candidate.invalidation_conditions),
                        iso(candidate.next_check_at),
                        candidate.portfolio_touch,
                        "youtube:12man",
                        candidate.source_ref,
                        _aware_now,
                        _aware_now,
                    ),
                )
                for key in candidate.index_keys:
                    connection.execute(
                        "INSERT INTO hypothesis_index(kind,key_value,hypothesis_id)"
                        " VALUES(?,?,?)",
                        (key.kind.value, key.value, hypothesis_id),
                    )
                connection.execute(
                    "INSERT INTO hypothesis_sightings VALUES(?,?,?,?,?,?,?,?)",
                    (
                        sighting.sighting_id,
                        hypothesis_id,
                        "REPORT",
                        sighting.source_ref,
                        sighting.revision,
                        sighting.payload_digest,
                        _json(candidate.supporting_evidence),
                        iso(sighting.observed_at),
                    ),
                )
                return ReplayResult.CREATED, hypothesis_id
            # M1: identical historical sighting first -> DUPLICATE regardless
            # of current lifecycle state (terminal, superseded, anything).
            if seen is not None:
                return ReplayResult.DUPLICATE, hypothesis_id
            if existing["superseded_by"] is not None:
                raise OpsKernelConflictError("hypothesis superseded")
            if existing["state"] in {
                item.value for item in _HYPOTHESIS_TERMINAL_STATES
            }:
                raise OpsKernelTransitionError("hypothesis terminal")
            # M4: evidence delta = new supporting tokens not in the current
            # set; the prior evidence remains durable in earlier sightings.
            prior = set(_decode(existing["supporting_evidence_json"]))
            evidence_delta = tuple(
                token for token in candidate.supporting_evidence if token not in prior
            )
            connection.execute(
                "UPDATE hypotheses SET statement=?,trigger_expression=?,"
                "supporting_evidence_json=?,counter_evidence_json=?,"
                "invalidation_conditions_json=?,next_check_at=?,"
                "portfolio_touch=?,updated_at=? WHERE hypothesis_id=?",
                (
                    candidate.statement,
                    candidate.trigger_expression.strip(),
                    _json(candidate.supporting_evidence),
                    _json(candidate.counter_evidence),
                    _json(candidate.invalidation_conditions),
                    iso(candidate.next_check_at),
                    candidate.portfolio_touch,
                    _aware_now,
                    hypothesis_id,
                ),
            )
            for key in candidate.index_keys:
                connection.execute(
                    "INSERT OR IGNORE INTO hypothesis_index"
                    "(kind,key_value,hypothesis_id) VALUES(?,?,?)",
                    (key.kind.value, key.value, hypothesis_id),
                )
            connection.execute(
                "INSERT INTO hypothesis_sightings VALUES(?,?,?,?,?,?,?,?)",
                (
                    sighting.sighting_id,
                    hypothesis_id,
                    "REPORT",
                    sighting.source_ref,
                    sighting.revision,
                    sighting.payload_digest,
                    _json(evidence_delta),
                    iso(sighting.observed_at),
                ),
            )
            return ReplayResult.CREATED, hypothesis_id

    def record_sighting(self, role, sighting, *, now=None):
        self._role(role, "record_sighting")
        if not isinstance(sighting, HypothesisSightingV1):
            raise ValueError("invalid sighting")
        with self._transaction() as connection:
            parent = connection.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE hypothesis_id=?",
                (sighting.hypothesis_id,),
            ).fetchone()
            if parent is None:
                raise OpsKernelTransitionError("unknown hypothesis")
            if (
                connection.execute(
                    "SELECT sighting_id FROM hypothesis_sightings WHERE sighting_id=?",
                    (sighting.sighting_id,),
                ).fetchone()
                is not None
            ):
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO hypothesis_sightings VALUES(?,?,?,?,?,?,?,?)",
                (
                    sighting.sighting_id,
                    sighting.hypothesis_id,
                    sighting.source_kind,
                    sighting.source_ref,
                    sighting.revision,
                    sighting.payload_digest,
                    _json(sighting.evidence_delta),
                    iso(sighting.observed_at),
                ),
            )
            return ReplayResult.CREATED

    def merge_hypotheses(self, role, kept_id, merged_id, *, now):
        """Archive ``merged_id`` as superseded by ``kept_id`` (B2).

        - merged's unique index keys transfer to (or are added for) kept, so
          future reverse-index recall wakes the kept hypothesis;
        - terminal merged states CONFLICT: terminal provenance is never
          overwritten;
        - kept must be a live (non-terminal) hypothesis; kept cannot itself
          be superseded; self-merge and A<->B cycles CONFLICT;
        - an identical replay of an already-applied merge returns DUPLICATE.
        """
        self._role(role, "merge_hypotheses")
        with self._transaction() as connection:
            kept = connection.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (kept_id,),
            ).fetchone()
            merged = connection.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (merged_id,),
            ).fetchone()
            if kept is None or merged is None:
                raise OpsKernelTransitionError("unknown hypothesis")
            if merged_id == kept_id:
                return HypothesisMergeOutcome.CONFLICT
            # identical replay of an already-applied merge
            if merged["superseded_by"] == kept_id:
                return HypothesisMergeOutcome.DUPLICATE
            # merged already superseded by someone else / any prior provenance
            if merged["superseded_by"] is not None:
                return HypothesisMergeOutcome.CONFLICT
            # terminal merged states never overwrite terminal provenance
            if merged["state"] in {item.value for item in _HYPOTHESIS_TERMINAL_STATES}:
                return HypothesisMergeOutcome.CONFLICT
            # kept must be live: terminal or already-superseded kept conflicts
            if kept["state"] in {item.value for item in _HYPOTHESIS_TERMINAL_STATES}:
                return HypothesisMergeOutcome.CONFLICT
            if kept["superseded_by"] is not None:
                return HypothesisMergeOutcome.CONFLICT
            # A<->B cycle: kept is superseded (directly) by merged
            if kept["superseded_by"] == merged_id:
                return HypothesisMergeOutcome.CONFLICT
            changed = connection.execute(
                "UPDATE hypotheses SET state=?,superseded_by=?,updated_at=? "
                "WHERE hypothesis_id=? AND superseded_by IS NULL",
                (HypothesisState.ARCHIVED.value, kept_id, iso(now), merged_id),
            ).rowcount
            if changed != 1:
                return HypothesisMergeOutcome.CONFLICT
            # B2: MOVE every merged index key to kept (add for kept, remove
            # from merged) so future reverse-index recall wakes kept exactly.
            connection.execute(
                "INSERT OR IGNORE INTO hypothesis_index"
                "(kind,key_value,hypothesis_id) "
                "SELECT kind,key_value,? FROM hypothesis_index "
                "WHERE hypothesis_id=?",
                (kept_id, merged_id),
            )
            connection.execute(
                "DELETE FROM hypothesis_index WHERE hypothesis_id=?",
                (merged_id,),
            )
            # v11: an approved manual merge supersedes any OPEN review that
            # involves the merged hypothesis — the queue entry is resolved
            # with durable provenance, never silently lost. Reviews between
            # unrelated pairs stay untouched. Fable 2nd repair M-3: only
            # rows still awaiting a decision (resolved_at IS NULL) are
            # superseded — an already SUPERSEDED/REJECTED/APPROVED-closed
            # OPEN row is history, and re-superseding it would fabricate a
            # spurious event.
            superseded_rows = list(
                connection.execute(
                    "SELECT * FROM merge_review_queue WHERE status='OPEN' "
                    "AND resolved_at IS NULL "
                    "AND (hypothesis_a=? OR hypothesis_b=?)",
                    (merged_id, merged_id),
                ).fetchall()
            )
            for row in superseded_rows:
                resolved = MergeReviewV1(
                    hypothesis_a=row["hypothesis_a"],
                    hypothesis_b=row["hypothesis_b"],
                    status="SUPERSEDED",
                    reason_code=row["reason_code"],
                    origin=row["origin"],
                    created_at=parse_iso(row["created_at"]),
                    resolved_at=now,
                )
                # append-only resolution event; the OPEN row is closed in
                # place, never deleted (Fable minor 2).
                connection.execute(
                    "INSERT INTO merge_review_queue VALUES(?,?,?,?,?,?,?,?)",
                    (
                        resolved.review_id,
                        resolved.hypothesis_a,
                        resolved.hypothesis_b,
                        "SUPERSEDED",
                        resolved.reason_code,
                        resolved.origin,
                        iso(resolved.created_at),
                        iso(now),
                    ),
                )
                connection.execute(
                    "UPDATE merge_review_queue SET resolved_at=? WHERE review_id=?",
                    (iso(now), row["review_id"]),
                )
            return HypothesisMergeOutcome.MERGED

    # --- semantic dedupe + weekly review (schema v11) -----------------------

    def _require_hypotheses(self, connection, *hypothesis_ids):
        for hypothesis_id in hypothesis_ids:
            row = connection.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("unknown hypothesis")

    def record_hypothesis_relation(
        self, role, left_id, right_id, decision, *, now, origin="ADMISSION"
    ):
        """Append one RELATED_BUT_DISTINCT edge (idempotent by pair+kind)."""
        self._role(role, "record_hypothesis_relation")
        if not isinstance(decision, SemanticDecisionV1):
            raise ValueError("invalid semantic decision")
        if decision.classification is not (SemanticClassification.RELATED_BUT_DISTINCT):
            raise ValueError("relation requires distinct classification")
        first, second = sorted((left_id, right_id))
        relation = HypothesisRelationV1(
            hypothesis_a=first,
            hypothesis_b=second,
            relation_kind="RELATED_BUT_DISTINCT",
            reason_code=decision.reason_code,
            conflicts=decision.conflicts,
            overlap=float(decision.overlap),
            origin=origin,
            created_at=now,
        )
        with self._transaction() as connection:
            self._require_hypotheses(connection, first, second)
            if (
                connection.execute(
                    "SELECT relation_id FROM hypothesis_relations WHERE relation_id=?",
                    (relation.relation_id,),
                ).fetchone()
                is not None
            ):
                return "duplicate"
            connection.execute(
                "INSERT INTO hypothesis_relations VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    relation.relation_id,
                    first,
                    second,
                    relation.relation_kind,
                    relation.reason_code,
                    _json(list(relation.conflicts)),
                    relation.overlap,
                    relation.origin,
                    iso(relation.created_at),
                ),
            )
            return "created"

    def read_hypothesis_relations(self, *, hypothesis_id=None):
        with self._read() as connection:
            if hypothesis_id is None:
                rows = connection.execute(
                    "SELECT * FROM hypothesis_relations ORDER BY created_at,relation_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM hypothesis_relations "
                    "WHERE hypothesis_a=? OR hypothesis_b=? "
                    "ORDER BY created_at,relation_id",
                    (hypothesis_id, hypothesis_id),
                ).fetchall()
            return tuple(
                HypothesisRelationV1(
                    hypothesis_a=row["hypothesis_a"],
                    hypothesis_b=row["hypothesis_b"],
                    relation_kind=row["relation_kind"],
                    reason_code=row["reason_code"],
                    conflicts=tuple(_decode(row["conflicts_json"])),
                    overlap=row["overlap"],
                    origin=row["origin"],
                    created_at=parse_iso(row["created_at"]),
                    relation_id=row["relation_id"],
                )
                for row in rows
            )

    def queue_merge_review(
        self, role, left_id, right_id, decision, *, now, origin="ADMISSION"
    ):
        """Queue one OPEN MERGE_REVIEW item (idempotent per pair+origin)."""
        self._role(role, "queue_merge_review")
        if not isinstance(decision, SemanticDecisionV1):
            raise ValueError("invalid semantic decision")
        if decision.classification is not SemanticClassification.MERGE_REVIEW:
            raise ValueError("merge review requires merge review classification")
        first, second = sorted((left_id, right_id))
        review = MergeReviewV1(
            hypothesis_a=first,
            hypothesis_b=second,
            status="OPEN",
            reason_code=decision.reason_code,
            origin=origin,
            created_at=now,
        )
        with self._transaction() as connection:
            self._require_hypotheses(connection, first, second)
            # Fable 2nd repair M-2: an unresolved OPEN for this canonical
            # pair+origin means the pair is already queued and awaiting a
            # decision — a resume at a different instant must return
            # duplicate instead of inserting a second unresolved OPEN with
            # a different event id. After a resolution the row is closed,
            # so a later requeue is a genuinely new append-only event.
            if (
                connection.execute(
                    "SELECT review_id FROM merge_review_queue WHERE "
                    "hypothesis_a=? AND hypothesis_b=? AND origin=? AND "
                    "status='OPEN' AND resolved_at IS NULL",
                    (first, second, origin),
                ).fetchone()
                is not None
            ):
                return "duplicate"
            if (
                connection.execute(
                    "SELECT review_id FROM merge_review_queue WHERE review_id=?",
                    (review.review_id,),
                ).fetchone()
                is not None
            ):
                return "duplicate"
            connection.execute(
                "INSERT INTO merge_review_queue VALUES(?,?,?,?,?,?,?,NULL)",
                (
                    review.review_id,
                    first,
                    second,
                    review.status,
                    review.reason_code,
                    review.origin,
                    iso(review.created_at),
                ),
            )
            return "created"

    def resolve_merge_review(
        self, role, left_id, right_id, status, *, now, origin=None
    ):
        """Manual resolution of an OPEN review (APPROVED/REJECTED/SUPERSEDED)."""
        self._role(role, "resolve_merge_review")
        if status not in {"APPROVED", "REJECTED", "SUPERSEDED"}:
            raise ValueError("invalid review status")
        first, second = sorted((left_id, right_id))
        with self._transaction() as connection:
            if origin is None:
                row = connection.execute(
                    "SELECT * FROM merge_review_queue WHERE hypothesis_a=? "
                    "AND hypothesis_b=? AND status='OPEN' AND resolved_at "
                    "IS NULL ORDER BY created_at LIMIT 1",
                    (first, second),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT * FROM merge_review_queue WHERE hypothesis_a=? "
                    "AND hypothesis_b=? AND status='OPEN' AND origin=? "
                    "AND resolved_at IS NULL ORDER BY created_at LIMIT 1",
                    (first, second, origin),
                ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("no open review")
            resolved = MergeReviewV1(
                hypothesis_a=first,
                hypothesis_b=second,
                status=status,
                reason_code=row["reason_code"],
                origin=row["origin"],
                created_at=parse_iso(row["created_at"]),
                resolved_at=now,
            )
            # append-only: the resolution is a NEW event row; the OPEN row
            # it resolves is closed in place (resolved_at set), never
            # deleted, so the full queue->resolution history is durable.
            connection.execute(
                "INSERT INTO merge_review_queue VALUES(?,?,?,?,?,?,?,?)",
                (
                    resolved.review_id,
                    first,
                    second,
                    status,
                    resolved.reason_code,
                    resolved.origin,
                    iso(resolved.created_at),
                    iso(now),
                ),
            )
            connection.execute(
                "UPDATE merge_review_queue SET resolved_at=? WHERE review_id=?",
                (iso(now), row["review_id"]),
            )
            return "resolved"

    def read_merge_review_history(self, left_id, right_id):
        """Append-only resolution-event history for one pair, oldest first.

        The queue's OPEN row is the pending marker (closed in place); every
        resolution decision is its own durable event row, so the same pair
        resolved repeatedly keeps its whole history.
        """
        first, second = sorted((left_id, right_id))
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM merge_review_queue WHERE hypothesis_a=? "
                "AND hypothesis_b=? AND status<>'OPEN' "
                "ORDER BY created_at,review_id",
                (first, second),
            ).fetchall()
            return tuple(
                MergeReviewV1(
                    hypothesis_a=row["hypothesis_a"],
                    hypothesis_b=row["hypothesis_b"],
                    status=row["status"],
                    reason_code=row["reason_code"],
                    origin=row["origin"],
                    created_at=parse_iso(row["created_at"]),
                    resolved_at=parse_iso(row["resolved_at"]),
                    review_id=row["review_id"],
                )
                for row in rows
            )

    def read_merge_reviews(self, *, status=None, hypothesis_id=None):
        """Actionable queue view: OPEN events still awaiting a decision.

        Resolution history (every APPROVED/REJECTED/SUPERSEDED event) lives
        in the append-only rows; use ``read_merge_review_history`` for it.
        """
        with self._read() as connection:
            clauses, params = [], []
            if status is None:
                clauses.append("status='OPEN'")
                clauses.append("resolved_at IS NULL")
            else:
                clauses.append("status=?")
                params.append(status)
                if status == "OPEN":
                    # minor 2: the explicit OPEN filter behaves exactly like
                    # the default actionable view — OPEN rows already carried
                    # into a resolution event are history, not queue work.
                    clauses.append("resolved_at IS NULL")
            if hypothesis_id is not None:
                clauses.append("(hypothesis_a=? OR hypothesis_b=?)")
                params.extend([hypothesis_id, hypothesis_id])
            where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
            rows = connection.execute(
                f"SELECT * FROM merge_review_queue{where} "
                "ORDER BY created_at,review_id",
                params,
            ).fetchall()
            return tuple(
                MergeReviewV1(
                    hypothesis_a=row["hypothesis_a"],
                    hypothesis_b=row["hypothesis_b"],
                    status=row["status"],
                    reason_code=row["reason_code"],
                    origin=row["origin"],
                    created_at=parse_iso(row["created_at"]),
                    resolved_at=parse_iso(row["resolved_at"]),
                    review_id=row["review_id"],
                )
                for row in rows
            )

    def record_admission_decision(self, role, decision):
        """Idempotent audit write of one semantic pre-admission comparison."""
        self._role(role, "record_admission_decision")
        if not isinstance(decision, AdmissionDecisionV1):
            raise ValueError("invalid admission decision")
        with self._transaction() as connection:
            if (
                connection.execute(
                    "SELECT decision_id FROM hypothesis_admission_decisions "
                    "WHERE decision_id=?",
                    (decision.decision_id,),
                ).fetchone()
                is not None
            ):
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO hypothesis_admission_decisions VALUES(?,?,?,?,?,?,?,?)",
                (
                    decision.decision_id,
                    decision.candidate_digest,
                    decision.hypothesis_id,
                    decision.matched_hypothesis_id,
                    decision.classification,
                    decision.reason_code,
                    _json(list(decision.conflicts)),
                    iso(decision.decided_at),
                ),
            )
            return ReplayResult.CREATED

    def read_admission_decisions(self, *, hypothesis_id=None):
        with self._read() as connection:
            if hypothesis_id is None:
                rows = connection.execute(
                    "SELECT * FROM hypothesis_admission_decisions "
                    "ORDER BY decided_at,decision_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM hypothesis_admission_decisions "
                    "WHERE hypothesis_id=? OR matched_hypothesis_id=? "
                    "ORDER BY decided_at,decision_id",
                    (hypothesis_id, hypothesis_id),
                ).fetchall()
            return tuple(
                AdmissionDecisionV1(
                    candidate_digest=row["candidate_digest"],
                    hypothesis_id=row["hypothesis_id"],
                    matched_hypothesis_id=row["matched_hypothesis_id"],
                    classification=row["classification"],
                    reason_code=row["reason_code"],
                    conflicts=tuple(_decode(row["conflicts_json"])),
                    decided_at=parse_iso(row["decided_at"]),
                    decision_id=row["decision_id"],
                )
                for row in rows
            )

    def reserve_weekly_review(self, role, week, *, now):
        """Atomically reserve this KST ISO week or replay as DUPLICATE."""
        self._role(role, "reserve_weekly_review")
        identity = week.identity if hasattr(week, "identity") else week
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM weekly_review_runs WHERE week_identity=?",
                (identity,),
            ).fetchone()
            if existing is not None:
                if existing["status"] == "COMPLETE":
                    return {"status": "DUPLICATE", "resumable": False}
                return {"status": "RESUME", "resumable": True}
            connection.execute(
                "INSERT INTO weekly_review_runs "
                "(week_identity,status,open_count,pairs_scanned,"
                "new_related_edges,new_merge_reviews,same_hypothesis_findings,"
                "reserved_at,completed_at) VALUES(?, 'RESERVED',0,0,0,0,0,?,NULL)",
                (identity, iso(now)),
            )
            return {"status": "CREATED", "resumable": False}

    def record_weekly_pair_adjudication(
        self, role, week_identity, pair, decision, *, now
    ):
        """Durable same-week pair result; replay returns duplicate."""
        self._role(role, "record_weekly_pair_adjudication")
        if not isinstance(decision, SemanticDecisionV1):
            raise ValueError("invalid semantic decision")
        first, second = sorted(pair)
        row = WeeklyReviewPairV1(
            week_identity=week_identity,
            hypothesis_a=first,
            hypothesis_b=second,
            classification=decision.classification.value,
            reason_code=decision.reason_code,
            adjudicated_at=now,
        )
        with self._transaction() as connection:
            if (
                connection.execute(
                    "SELECT 1 FROM weekly_review_pairs WHERE week_identity=? "
                    "AND hypothesis_a=? AND hypothesis_b=?",
                    (row.week_identity, row.hypothesis_a, row.hypothesis_b),
                ).fetchone()
                is not None
            ):
                return "duplicate"
            connection.execute(
                "INSERT INTO weekly_review_pairs VALUES(?,?,?,?,?,?)",
                (
                    row.week_identity,
                    row.hypothesis_a,
                    row.hypothesis_b,
                    row.classification,
                    row.reason_code,
                    iso(row.adjudicated_at),
                ),
            )
            return "created"

    def complete_weekly_review(
        self,
        role,
        week,
        *,
        now,
        open_count=0,
        pairs_scanned=0,
        new_related_edges=0,
        new_merge_reviews=0,
        same_hypothesis_findings=0,
    ):
        """Mark the reserved week COMPLETE with its durable summary."""
        self._role(role, "complete_weekly_review")
        identity = week.identity if hasattr(week, "identity") else week
        run = WeeklyReviewRunV1(
            week_identity=identity,
            status="COMPLETE",
            open_count=open_count,
            pairs_scanned=pairs_scanned,
            new_related_edges=new_related_edges,
            new_merge_reviews=new_merge_reviews,
            same_hypothesis_findings=same_hypothesis_findings,
            reserved_at=now,
            completed_at=now,
        )
        with self._transaction() as connection:
            existing = connection.execute(
                "SELECT * FROM weekly_review_runs WHERE week_identity=?",
                (identity,),
            ).fetchone()
            if existing is None:
                raise OpsKernelTransitionError("week not reserved")
            if existing["status"] == "COMPLETE":
                return ReplayResult.DUPLICATE
            connection.execute(
                "UPDATE weekly_review_runs SET status='COMPLETE',"
                "open_count=?,pairs_scanned=?,new_related_edges=?,"
                "new_merge_reviews=?,same_hypothesis_findings=?,completed_at=? "
                "WHERE week_identity=? AND status='RESERVED'",
                (
                    open_count,
                    pairs_scanned,
                    new_related_edges,
                    new_merge_reviews,
                    same_hypothesis_findings,
                    iso(now),
                    identity,
                ),
            )
            return ReplayResult.CREATED

    def read_weekly_review_summary(self, week_identity):
        with self._read() as connection:
            run = connection.execute(
                "SELECT * FROM weekly_review_runs WHERE week_identity=?",
                (week_identity,),
            ).fetchone()
            pair_rows = connection.execute(
                "SELECT * FROM weekly_review_pairs WHERE week_identity=? "
                "ORDER BY hypothesis_a,hypothesis_b",
                (week_identity,),
            ).fetchall()
            # Fable 2nd repair minor 3: the week's hypothesis ids are the
            # FROZEN set recorded by this week's durable pair rows — never
            # a live query over current open hypotheses, which would leak
            # later admissions into an already-completed week's summary.
            # A degenerate week (fewer than two open hypotheses) has no
            # pair rows and therefore an empty frozen set; open_count on
            # the receipt preserves its cardinality.
            hypothesis_ids = tuple(
                dict.fromkeys(
                    identifier
                    for row in pair_rows
                    for identifier in (row["hypothesis_a"], row["hypothesis_b"])
                )
            )
            return {
                "run": WeeklyReviewRunV1(
                    week_identity=run["week_identity"],
                    status=run["status"],
                    open_count=run["open_count"],
                    pairs_scanned=run["pairs_scanned"],
                    new_related_edges=run["new_related_edges"],
                    new_merge_reviews=run["new_merge_reviews"],
                    same_hypothesis_findings=run["same_hypothesis_findings"],
                    reserved_at=parse_iso(run["reserved_at"]),
                    completed_at=parse_iso(run["completed_at"]),
                )
                if run is not None
                else None,
                "pairs": tuple(
                    WeeklyReviewPairV1(
                        week_identity=row["week_identity"],
                        hypothesis_a=row["hypothesis_a"],
                        hypothesis_b=row["hypothesis_b"],
                        classification=row["classification"],
                        reason_code=row["reason_code"],
                        adjudicated_at=parse_iso(row["adjudicated_at"]),
                    )
                    for row in pair_rows
                ),
                "hypothesis_ids": hypothesis_ids,
            }

    def transition_hypothesis(
        self, role, hypothesis_id, new_state, *, now, reason_token="operator_transition"
    ):
        """Apply one lifecycle transition with durable reason evidence (M3)."""
        self._role(role, "transition_hypothesis")
        if not isinstance(new_state, HypothesisState):
            raise ValueError("invalid hypothesis state")
        require_token(reason_token, "reason_token")
        from tokmo.values.ops_kernel import HypothesisLifecycleV1

        with self._transaction() as connection:
            current = connection.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if current is None:
                raise OpsKernelTransitionError("unknown hypothesis")
            state = HypothesisState(current["state"])
            if new_state is state:
                raise OpsKernelTransitionError("hypothesis state unchanged")
            allowed = _HYPOTHESIS_TRANSITIONS.get(state, frozenset())
            if new_state not in allowed:
                raise OpsKernelTransitionError("hypothesis transition denied")
            if current["superseded_by"] is not None and (
                new_state is not HypothesisState.ARCHIVED
            ):
                raise OpsKernelTransitionError("hypothesis superseded")
            changed = connection.execute(
                "UPDATE hypotheses SET state=?,updated_at=? WHERE hypothesis_id=? "
                "AND state=?",
                (new_state.value, iso(now), hypothesis_id, state.value),
            ).rowcount
            if changed != 1:
                raise OpsKernelConflictError("hypothesis state conflict")
            lifecycle = HypothesisLifecycleV1(
                hypothesis_id=hypothesis_id,
                from_state=state,
                to_state=new_state,
                reason_token=reason_token,
                transitioned_at=now,
            )
            connection.execute(
                "INSERT OR IGNORE INTO hypothesis_lifecycle"
                "(lifecycle_id,hypothesis_id,from_state,to_state,reason_token,"
                "transitioned_at) VALUES(?,?,?,?,?,?)",
                (
                    lifecycle.lifecycle_id,
                    hypothesis_id,
                    state.value,
                    new_state.value,
                    reason_token,
                    iso(now),
                ),
            )
            return ReplayResult.CREATED

    def read_hypothesis(self, hypothesis_id):
        with self._read() as connection:
            row = connection.execute(
                "SELECT * FROM hypotheses WHERE hypothesis_id=?",
                (hypothesis_id,),
            ).fetchone()
            if row is None:
                raise OpsKernelTransitionError("unknown hypothesis")
            return self._hypothesis_row(row)

    def read_open_hypotheses(self):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM hypotheses WHERE state IN ('CANDIDATE','WATCHING') "
                "ORDER BY created_at,hypothesis_id"
            ).fetchall()
            return tuple(self._hypothesis_row(row) for row in rows)

    def read_hypotheses_by_index(self, kind, value):
        from tokmo.values.ops_kernel import IndexKeyKind

        if not isinstance(kind, IndexKeyKind):
            raise ValueError("invalid index kind")
        with self._read() as connection:
            rows = connection.execute(
                "SELECT hypothesis_id FROM hypothesis_index "
                "WHERE kind=? AND key_value=? ORDER BY hypothesis_id",
                (kind.value, value),
            ).fetchall()
            return tuple(row["hypothesis_id"] for row in rows)

    def read_hypothesis_index(self, hypothesis_id):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT kind,key_value FROM hypothesis_index "
                "WHERE hypothesis_id=? ORDER BY kind,key_value",
                (hypothesis_id,),
            ).fetchall()
            from tokmo.values.ops_kernel import HypothesisIndexKeyV1, IndexKeyKind

            return tuple(
                HypothesisIndexKeyV1(IndexKeyKind(row["kind"]), row["key_value"])
                for row in rows
            )

    def read_hypothesis_sightings(self, hypothesis_id):
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM hypothesis_sightings WHERE hypothesis_id=? "
                "ORDER BY observed_at,sighting_id",
                (hypothesis_id,),
            ).fetchall()
            return tuple(
                HypothesisSightingV1(
                    hypothesis_id=row["hypothesis_id"],
                    source_kind=row["source_kind"],
                    source_ref=row["source_ref"],
                    revision=row["revision"],
                    payload_digest=row["payload_digest"],
                    evidence_delta=tuple(_decode(row["evidence_delta_json"])),
                    observed_at=parse_iso(row["observed_at"]),
                    sighting_id=row["sighting_id"],
                )
                for row in rows
            )

    def read_hypothesis_lifecycle(self, hypothesis_id):
        """Append-only transition history with durable reason tokens (M3)."""
        from tokmo.values.ops_kernel import (
            HypothesisLifecycleV1,
            HypothesisState,
        )

        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM hypothesis_lifecycle WHERE hypothesis_id=? "
                "ORDER BY transitioned_at,lifecycle_id",
                (hypothesis_id,),
            ).fetchall()
            return tuple(
                HypothesisLifecycleV1(
                    hypothesis_id=row["hypothesis_id"],
                    from_state=HypothesisState(row["from_state"]),
                    to_state=HypothesisState(row["to_state"]),
                    reason_token=row["reason_token"],
                    transitioned_at=parse_iso(row["transitioned_at"]),
                    lifecycle_id=row["lifecycle_id"],
                )
                for row in rows
            )

    def record_candidate_failure(self, role, failure, *, now=None):
        """Durably quarantine one malformed candidate block (M2)."""
        self._role(role, "record_candidate_failure")
        from tokmo.values.ops_kernel import CandidateFailureV1

        if not isinstance(failure, CandidateFailureV1):
            raise ValueError("invalid candidate failure")
        with self._transaction() as connection:
            if (
                connection.execute(
                    "SELECT failure_id FROM candidate_failures WHERE failure_id=?",
                    (failure.failure_id,),
                ).fetchone()
                is not None
            ):
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO candidate_failures VALUES(?,?,?,?,?,?,?)",
                (
                    failure.failure_id,
                    failure.source_ref,
                    failure.revision,
                    failure.error_kind,
                    failure.block_digest,
                    failure.detail_token,
                    iso(failure.recorded_at),
                ),
            )
            return ReplayResult.CREATED

    def read_candidate_failures(self, *, source_ref=None):
        from tokmo.values.ops_kernel import CandidateFailureV1

        with self._read() as connection:
            if source_ref is None:
                rows = connection.execute(
                    "SELECT * FROM candidate_failures ORDER BY recorded_at,failure_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM candidate_failures WHERE source_ref=? "
                    "ORDER BY recorded_at,failure_id",
                    (source_ref,),
                ).fetchall()
            return tuple(
                CandidateFailureV1(
                    source_ref=row["source_ref"],
                    revision=row["revision"],
                    error_kind=row["error_kind"],
                    block_digest=row["block_digest"],
                    recorded_at=parse_iso(row["recorded_at"]),
                    detail_token=row["detail_token"],
                    failure_id=row["failure_id"],
                )
                for row in rows
            )

    def record_shadow_recall(self, role, record):
        """Idempotent would-have-sent shadow write; delivery is absent."""
        self._role(role, "record_shadow_recall")
        from tokmo.values.ops_kernel import ShadowRecallRecordV1

        if not isinstance(record, ShadowRecallRecordV1):
            raise ValueError("invalid shadow record")
        with self._transaction() as connection:
            parent = connection.execute(
                "SELECT hypothesis_id FROM hypotheses WHERE hypothesis_id=?",
                (record.hypothesis_id,),
            ).fetchone()
            if parent is None:
                raise OpsKernelTransitionError("unknown hypothesis")
            if (
                connection.execute(
                    "SELECT shadow_id FROM shadow_recall_records WHERE shadow_id=?",
                    (record.shadow_id,),
                ).fetchone()
                is not None
            ):
                return ReplayResult.DUPLICATE
            connection.execute(
                "INSERT INTO shadow_recall_records VALUES(?,?,?,?,?,?,?,?)",
                (
                    record.shadow_id,
                    record.hypothesis_id,
                    _json(record.material_delta_ids),
                    _json(
                        [f"{key.kind.value}:{key.value}" for key in record.matched_keys]
                    ),
                    record.match_kind,
                    0,
                    record.payload_json,
                    iso(record.recorded_at),
                ),
            )
            return ReplayResult.CREATED

    def read_shadow_recall(self, hypothesis_id=None):
        with self._read() as connection:
            if hypothesis_id is None:
                rows = connection.execute(
                    "SELECT * FROM shadow_recall_records ORDER BY recorded_at,shadow_id"
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM shadow_recall_records WHERE hypothesis_id=? "
                    "ORDER BY recorded_at,shadow_id",
                    (hypothesis_id,),
                ).fetchall()
            from tokmo.values.ops_kernel import (
                HypothesisIndexKeyV1,
                IndexKeyKind,
                ShadowRecallRecordV1,
            )

            result = []
            for row in rows:
                # Tokens are stored as "<KIND>:<key.value>" (write/read
                # symmetric); source-event key values keep their inner colon.
                matched = []
                for token in _decode(row["matched_keys_json"]):
                    for kind in IndexKeyKind:
                        prefix = kind.value + ":"
                        if token.startswith(prefix):
                            matched.append(
                                HypothesisIndexKeyV1(
                                    kind,
                                    token[len(prefix) :],
                                )
                            )
                            break
                result.append(
                    ShadowRecallRecordV1(
                        hypothesis_id=row["hypothesis_id"],
                        material_delta_ids=tuple(
                            _decode(row["material_delta_ids_json"])
                        ),
                        matched_keys=tuple(matched),
                        match_kind=row["match_kind"],
                        would_have_sent=False,
                        payload_json=row["payload_json"],
                        recorded_at=parse_iso(row["recorded_at"]),
                        shadow_id=row["shadow_id"],
                    )
                )
            return tuple(result)
