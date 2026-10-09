"""Private, durable NEWS/EVENT source of truth."""

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.values.news_config import (
    META_COOLDOWN_DAYS,
    META_MIN_NEGATIVE,
    META_WINDOW_DAYS,
)
from tokmo.values.news_digest import compose_morning_digest, morning_digest_eligible
from tokmo.values.news_event import (
    EpisodeState,
    Evidence,
    NewsClaimStatusV1,
    NewsEpisode,
    NewsEpisodeTransitionError,
)
from tokmo.values.news_taxonomy import (
    TAXONOMY_KINDS_V1,
    taxonomy_evidence_digest,
    taxonomy_unit_id,
)
from tokmo.values.ops_kernel import ReplayResult


class NewsEpisodeSchemaError(RuntimeError):
    pass


class NewsEpisodeStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class ClassifierOutage:
    outage_id: str
    model_id: str
    failure_class: str
    first_observed_at: datetime


# NEWS G3 canary attempt ledger (approval packet §2.2): one durable,
# non-rearmable row per activation_id. Reservation is an atomic INSERT that
# precedes the Telegram transport start; SENT/FAILED/UNKNOWN all keep the
# cap consumed and there is no delete or reset path.
_CANARY_DDL = "CREATE TABLE canary_attempts(activation_id TEXT PRIMARY KEY CHECK(activation_id<>'' AND length(activation_id)<=64), ops_alert_id TEXT NOT NULL, reserved_at TEXT NOT NULL, outcome TEXT NOT NULL CHECK(outcome IN('RESERVED','SENT','FAILED','UNKNOWN')), settled_at TEXT, CHECK((settled_at IS NULL)=(outcome='RESERVED'))) STRICT"

# NEWS G3 live-lane ledgers (2026-08-21 follow-up direction):
# - classifier_work_queue is the durable bounded asynchronous classifier
#   authority: pulse enqueues one frozen request per classification
#   identity and stays nonblocking; only the separately invoked worker
#   consumes attempts and starts model transport.
# - canary_slot_proofs is the durable predecessor proof for TELEGRAM_LIVE:
#   it can only reference a SENT canary attempt and requires exactly one
#   observed transport start over two post-settle pulse slots. A gate-file
#   edit alone can never fabricate this row.
# - live_delivery_trips is the durable delivery-only OFF ledger: the first
#   trip per activation wins and there is no delete or reset path.
_LIVE_DDL = (
    "CREATE TABLE classifier_work_queue(classification_identity TEXT PRIMARY KEY CHECK(length(classification_identity)=64), purpose TEXT NOT NULL CHECK(purpose IN('DOMAIN_AND_EPISODE_RELATION','EVIDENCE_CLAIM_LABEL','EVENT_CLASS','MATERIAL_CHANGE_KIND')), candidate_id TEXT, episode_id TEXT, provider TEXT NOT NULL CHECK(provider<>''), model_id TEXT NOT NULL CHECK(model_id<>''), model_contract_digest TEXT NOT NULL CHECK(length(model_contract_digest)=64), prompt_policy_digest TEXT NOT NULL CHECK(length(prompt_policy_digest)=64), input_evidence_digest TEXT NOT NULL CHECK(length(input_evidence_digest)=64), output_schema_id TEXT NOT NULL CHECK(output_schema_id='news-classifier-output-v1'), evidence_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('PENDING','LEASED','DONE','EXHAUSTED','STALE')), attempts INTEGER NOT NULL CHECK(attempts>=0), lease_owner TEXT, lease_expires_at TEXT, last_exit TEXT, enqueued_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK((lease_owner IS NULL)=(state<>'LEASED')), CHECK((lease_expires_at IS NULL)=(lease_owner IS NULL)), CHECK(candidate_id IS NOT NULL OR episode_id IS NOT NULL)) STRICT;\n"
    "CREATE TABLE canary_slot_proofs(activation_id TEXT PRIMARY KEY REFERENCES canary_attempts(activation_id), settled_outcome TEXT NOT NULL CHECK(settled_outcome='SENT'), slots_json TEXT NOT NULL, transport_starts INTEGER NOT NULL CHECK(transport_starts=1), recorded_at TEXT NOT NULL) STRICT;\n"
    "CREATE TABLE live_delivery_trips(activation_id TEXT PRIMARY KEY CHECK(activation_id<>'' AND length(activation_id)<=64), reason TEXT NOT NULL CHECK(reason<>''), tripped_at TEXT NOT NULL) STRICT"
)

# NEWS taxonomy registration ledgers (2026-08-22 stages 1–6): one row per
# typed unit (entity alias/topic/metric/catalyst/effect) plus an
# append-only decision log. Decision ids are digests of the transition and
# its evidence, so re-running the same corpus is replay-idempotent. There
# is no delete path — TOMBSTONED is the only deactivation.
_TAXONOMY_DDL = (
    "CREATE TABLE taxonomy_units(unit_id TEXT PRIMARY KEY CHECK(length(unit_id)=64), kind TEXT NOT NULL CHECK(kind IN('ENTITY_ALIAS','TOPIC','METRIC','CATALYST','EFFECT')), unit_key TEXT NOT NULL CHECK(unit_key<>''), canonical_key TEXT, display_text TEXT NOT NULL, payload_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('CANDIDATE','VERIFIED_AUTO','PENDING_MODEL','VERIFIED_MODEL','REJECTED_MODEL','TOMBSTONED')), reason TEXT NOT NULL CHECK(reason<>''), evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(kind,unit_key)) STRICT;\n"
    "CREATE TABLE taxonomy_decisions(decision_id TEXT PRIMARY KEY CHECK(length(decision_id)=64), unit_id TEXT NOT NULL REFERENCES taxonomy_units(unit_id), from_state TEXT, to_state TEXT NOT NULL, reason TEXT NOT NULL CHECK(reason<>''), evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), evidence_json TEXT NOT NULL, decided_at TEXT NOT NULL) STRICT"
)

# NEWS taxonomy stage-7 adjudication ledgers (2026-08-22): the durable
# operational job/call ledger for batched pending adjudication plus the
# per-unit deferral markers. Route/model identifiers, request/response
# digests, timestamps, and terminal outcomes live ONLY here — taxonomy
# unit identity and decision history stay provider-neutral. job_id is the
# digest over the batch's exact (unit_id, evidence_digest) pairs, so the
# same pending evidence can never start twice; UNKNOWN is the explicit
# crash-recovery terminal and retry is an explicit bounded budget.
_ADJUDICATION_DDL = (
    "CREATE TABLE taxonomy_adjudication_jobs(job_id TEXT PRIMARY KEY CHECK(length(job_id)=64), request_digest TEXT NOT NULL CHECK(length(request_digest)=64), unit_ids_json TEXT NOT NULL, route_id TEXT NOT NULL CHECK(route_id<>'' AND length(route_id)<=128 AND route_id NOT GLOB '*[^a-z0-9.-]*'), state TEXT NOT NULL CHECK(state IN('STARTED','SETTLED','FAILED','UNKNOWN')), attempts INTEGER NOT NULL CHECK(attempts>=1), outcome TEXT CHECK(outcome IS NULL OR outcome IN('OK','INVALID_RESPONSE','TRANSPORT_FAIL','STATE_CONFLICT','CRASH_UNKNOWN')), response_digest TEXT CHECK(response_digest IS NULL OR length(response_digest)=64), started_at TEXT NOT NULL, settled_at TEXT, CHECK((settled_at IS NULL)=(state='STARTED')), CHECK((outcome IS NULL)=(state='STARTED')), CHECK(response_digest IS NULL OR outcome='OK'), CHECK(state<>'SETTLED' OR outcome='OK'), CHECK(state<>'UNKNOWN' OR outcome='CRASH_UNKNOWN'), CHECK(state<>'FAILED' OR outcome IN('INVALID_RESPONSE','TRANSPORT_FAIL','STATE_CONFLICT'))) STRICT;\n"
    "CREATE TABLE taxonomy_adjudication_units(unit_id TEXT NOT NULL REFERENCES taxonomy_units(unit_id), evidence_digest TEXT NOT NULL CHECK(length(evidence_digest)=64), job_id TEXT REFERENCES taxonomy_adjudication_jobs(job_id), status TEXT NOT NULL CHECK(status IN('DEFERRED','EXPIRED','EXHAUSTED')), reason TEXT NOT NULL CHECK(reason<>''), marked_at TEXT NOT NULL, PRIMARY KEY(unit_id,evidence_digest)) STRICT"
)

_DDL = f"""{_CANARY_DDL};
{_LIVE_DDL};
{_TAXONOMY_DDL};
{_ADJUDICATION_DDL};
CREATE TABLE schema_meta(singleton INTEGER PRIMARY KEY CHECK(singleton=1), schema_version INTEGER NOT NULL CHECK(schema_version=1), schema_id TEXT NOT NULL CHECK(schema_id='news-episode-v1'), ddl_sha256 TEXT NOT NULL CHECK(length(ddl_sha256)=64), created_at TEXT NOT NULL) STRICT;
CREATE TABLE discovery_runs(run_id TEXT PRIMARY KEY, window_from TEXT NOT NULL, window_to TEXT NOT NULL, corpus_snapshot_digest TEXT NOT NULL CHECK(length(corpus_snapshot_digest)=64), policy_digest TEXT NOT NULL CHECK(length(policy_digest)=64), scanned_documents INTEGER NOT NULL CHECK(scanned_documents>=0), distinct_stories INTEGER NOT NULL CHECK(distinct_stories>=0), dropped_over_cap INTEGER NOT NULL CHECK(dropped_over_cap>=0), status TEXT NOT NULL CHECK(status IN('OK','STORE_UNAVAILABLE','SCHEMA_MISMATCH','CAP_EXCEEDED','CORRUPT','BUSY_OR_LOCKED')), started_at TEXT NOT NULL, finished_at TEXT NOT NULL) STRICT;
CREATE TABLE discovery_candidates(candidate_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES discovery_runs(run_id), lane TEXT NOT NULL CHECK(lane IN('WHOLE_CORPUS_FREQUENCY','DIRECT_HOLDING','DIRECT_PLAYBOOK','PRE_CATALYST','EARLY_PROPAGATION','PRICE_VOLUME_BACKSTOP','CONCEPT_EMERGENCE','MARKET_STRUCTURE','MACRO_GEOPOLITICAL')), entity_key TEXT, phrase_text TEXT, distinct_story_count INTEGER NOT NULL CHECK(distinct_story_count>=1), distinct_source_count INTEGER NOT NULL CHECK(distinct_source_count>=1), evidence_story_keys_json TEXT NOT NULL, state TEXT NOT NULL CHECK(state IN('SURFACED','ADMITTED_FOR_INVESTIGATION','MERGED_INTO_EPISODE','DROPPED_NOISE','EXPIRED')), episode_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK(entity_key IS NOT NULL OR phrase_text IS NOT NULL)) STRICT;
CREATE TABLE episodes(episode_id TEXT PRIMARY KEY, episode_key TEXT NOT NULL UNIQUE CHECK(length(episode_key)=64), parent_episode_id TEXT REFERENCES episodes(episode_id), cause_key TEXT, title TEXT NOT NULL CHECK(title<>''), domain TEXT NOT NULL CHECK(domain IN('CORPORATE','MACRO_GEOPOLITICAL','MARKET_STRUCTURE','UNCERTAIN_CLAIM')), state TEXT NOT NULL CHECK(state IN('DISCOVERED','INVESTIGATING','EARLY_ALERTED','ACTIVE','ESCALATED','DEESCALATED','CONFLICTED','DISCONFIRMED','RESOLVED','MUTED','CLOSED')), event_class TEXT NOT NULL CHECK(event_class IN('CORE_BREAKING','ORDINARY')), provisional INTEGER NOT NULL CHECK(provisional IN(0,1)), revision INTEGER NOT NULL CHECK(revision>=1), first_seen_at TEXT NOT NULL, last_material_change_kind TEXT, last_material_change_observed_at TEXT, muted_at TEXT, mute_note TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, CHECK((muted_at IS NULL)=(state<>'MUTED')), CHECK(parent_episode_id IS NULL OR parent_episode_id<>episode_id)) STRICT;
CREATE TABLE episode_revisions(episode_id TEXT,revision INTEGER,state TEXT,evidence_json TEXT,observed_at TEXT,PRIMARY KEY(episode_id,revision)) STRICT;
CREATE TABLE episode_entities(episode_id TEXT NOT NULL REFERENCES episodes(episode_id), entity_key TEXT NOT NULL, role TEXT NOT NULL CHECK(role IN('PRIMARY','SECONDARY')), PRIMARY KEY(episode_id,entity_key)) STRICT;
CREATE TABLE episode_evidence(evidence_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), news_document_id TEXT, story_key TEXT NOT NULL, source_kind TEXT NOT NULL CHECK(source_kind IN('LOCAL_NEWS','OFFICIAL_PRIMARY','EXTERNAL_PUBLIC')), claim_status TEXT NOT NULL CHECK(claim_status IN('CONFIRMED_FACT','CLAIM','INTERPRETATION','CAUSAL_HYPOTHESIS','RETRACTED','CORRECTED')), conflict_group TEXT, novel INTEGER NOT NULL CHECK(novel IN(0,1)), published_at TEXT, observed_at TEXT NOT NULL, url_hash TEXT CHECK(url_hash IS NULL OR length(url_hash)=64), title_digest TEXT CHECK(title_digest IS NULL OR length(title_digest)=64), UNIQUE(episode_id,story_key,claim_status)) STRICT;
CREATE TABLE episode_transitions(transition_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), from_state TEXT NOT NULL CHECK(from_state IN('DISCOVERED','INVESTIGATING','EARLY_ALERTED','ACTIVE','ESCALATED','DEESCALATED','CONFLICTED','DISCONFIRMED','RESOLVED','MUTED','CLOSED')), to_state TEXT NOT NULL CHECK(to_state IN('DISCOVERED','INVESTIGATING','EARLY_ALERTED','ACTIVE','ESCALATED','DEESCALATED','CONFLICTED','DISCONFIRMED','RESOLVED','MUTED','CLOSED')), material_change_kind TEXT, material_change_observed_at TEXT, evidence_ids_json TEXT NOT NULL, transitioned_at TEXT NOT NULL) STRICT;
CREATE TABLE scenario_nodes(node_id TEXT PRIMARY KEY, episode_id TEXT NOT NULL REFERENCES episodes(episode_id), parent_node_id TEXT REFERENCES scenario_nodes(node_id), ordinal INTEGER NOT NULL CHECK(ordinal>=0), kind TEXT NOT NULL CHECK(kind IN('SEQUENTIAL_STAGE','PARALLEL_BRANCH','CONDITIONAL_OUTCOME')), title TEXT NOT NULL CHECK(title<>''), observable_conditions_json TEXT NOT NULL, disconfirming_conditions_json TEXT NOT NULL, suggested_response TEXT NOT NULL, material_action_condition INTEGER NOT NULL CHECK(material_action_condition IN(0,1)), state TEXT NOT NULL CHECK(state IN('PENDING','ACTIVE','MET','DISCONFIRMED','EXPIRED')), created_at TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
CREATE TABLE alert_revisions(alert_revision_id TEXT PRIMARY KEY CHECK(length(alert_revision_id)=64), episode_id TEXT NOT NULL REFERENCES episodes(episode_id), revision_no INTEGER NOT NULL CHECK(revision_no>=1), revision_kind TEXT NOT NULL CHECK(revision_kind IN('INITIAL','EARLY_FACTUAL','MATERIAL_UPDATE','CORRECTION','DEESCALATION','ACTION_DRAFT','MORNING_COALESCED')), content_digest TEXT NOT NULL CHECK(length(content_digest)=64), portfolio_context_digest TEXT NOT NULL CHECK(length(portfolio_context_digest)=64), confidence INTEGER NOT NULL CHECK(confidence BETWEEN 0 AND 100), material_change_kind TEXT NOT NULL, material_change_observed_at TEXT NOT NULL, acknowledges_prior_reaction INTEGER NOT NULL CHECK(acknowledges_prior_reaction IN(0,1)), body_text TEXT NOT NULL CHECK(body_text<>''), links_json TEXT NOT NULL, callback_ref TEXT NOT NULL UNIQUE CHECK(length(callback_ref)=32 AND callback_ref NOT GLOB '*[^0-9a-f]*'), delivery_state TEXT NOT NULL CHECK(delivery_state IN('DRAFT','HELD_MORNING','PENDING_OUTBOX','OUTBOXED','DELIVERY_BOUND','SUPPRESSED')), ops_alert_id TEXT UNIQUE, provider_message_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(episode_id,revision_no), CHECK((ops_alert_id IS NULL)=(delivery_state IN('DRAFT','HELD_MORNING','PENDING_OUTBOX','SUPPRESSED'))), CHECK(provider_message_id IS NULL OR delivery_state='DELIVERY_BOUND')) STRICT;
CREATE TABLE alert_reactions(reaction_event_id TEXT PRIMARY KEY, alert_revision_id TEXT NOT NULL REFERENCES alert_revisions(alert_revision_id), episode_id TEXT NOT NULL REFERENCES episodes(episode_id), reaction TEXT NOT NULL CHECK(reaction IN('HELPFUL','NOT_HELPFUL')), provider_update_id INTEGER NOT NULL UNIQUE, provider_message_id TEXT NOT NULL, alert_content_digest TEXT NOT NULL CHECK(length(alert_content_digest)=64), portfolio_context_digest TEXT NOT NULL CHECK(length(portfolio_context_digest)=64), supersedes_event_id TEXT REFERENCES alert_reactions(reaction_event_id), reacted_at TEXT NOT NULL) STRICT;
CREATE TABLE meta_feedback_proposals(proposal_id TEXT PRIMARY KEY, context_key TEXT NOT NULL, evidence_reaction_ids_json TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN('PROPOSED','DISCUSSED','DECLINED','EXPIRED')), cooldown_until TEXT NOT NULL, created_at TEXT NOT NULL) STRICT;
CREATE TABLE morning_hold_queue(episode_id TEXT PRIMARY KEY REFERENCES episodes(episode_id), latest_held_revision_id TEXT NOT NULL REFERENCES alert_revisions(alert_revision_id), release_not_before TEXT NOT NULL, updated_at TEXT NOT NULL) STRICT;
CREATE TABLE classifier_calls(call_id TEXT PRIMARY KEY, classification_identity TEXT NOT NULL CHECK(length(classification_identity)=64), attempt_no INTEGER NOT NULL CHECK(attempt_no>=1), purpose TEXT NOT NULL CHECK(purpose IN('DOMAIN_AND_EPISODE_RELATION','EVIDENCE_CLAIM_LABEL','EVENT_CLASS','MATERIAL_CHANGE_KIND')), candidate_id TEXT, episode_id TEXT REFERENCES episodes(episode_id), provider TEXT NOT NULL CHECK(provider<>'' AND length(provider)<=64 AND provider NOT GLOB '*[^a-z0-9.-]*'), model_id TEXT NOT NULL CHECK(model_id<>'' AND length(model_id)<=128 AND model_id NOT GLOB '*[^a-z0-9.-]*'), model_contract_digest TEXT NOT NULL CHECK(length(model_contract_digest)=64), prompt_policy_digest TEXT NOT NULL CHECK(length(prompt_policy_digest)=64), input_evidence_digest TEXT NOT NULL CHECK(length(input_evidence_digest)=64), output_schema_id TEXT NOT NULL CHECK(output_schema_id='news-classifier-output-v1'), state TEXT NOT NULL CHECK(state IN('PLANNED','STARTED','SETTLED','UNKNOWN','DEFERRED')), exit TEXT CHECK(exit IS NULL OR exit IN('OK','TIMEOUT','CANCELLED','TRUNCATED','OVERSIZE','EMPTY_SUCCESS','PARSE_FAIL','SCHEMA_INVALID','CITATION_UNRESOLVED','RATE_LIMITED','QUOTA_EXCEEDED','MODEL_UNAVAILABLE','MODEL_REJECTED','AUTH_FAIL','TRANSPORT_FAIL')), output_json TEXT, output_digest TEXT CHECK(output_digest IS NULL OR length(output_digest)=64), requested_at TEXT NOT NULL, started_at TEXT, settled_at TEXT, UNIQUE(classification_identity,attempt_no), CHECK(candidate_id IS NOT NULL OR episode_id IS NOT NULL), CHECK((exit IS NULL)=(state IN('PLANNED','STARTED','UNKNOWN'))), CHECK(state<>'DEFERRED' OR exit IN('RATE_LIMITED','QUOTA_EXCEEDED','MODEL_UNAVAILABLE','MODEL_REJECTED')), CHECK(output_json IS NULL OR (state='SETTLED' AND exit='OK')), CHECK(state<>'SETTLED' OR exit<>'OK' OR output_json IS NOT NULL), CHECK(output_json IS NULL OR output_digest IS NOT NULL), CHECK(output_digest IS NULL OR output_json IS NOT NULL)) STRICT;
CREATE TABLE classifier_outages(outage_id TEXT PRIMARY KEY, model_id TEXT NOT NULL CHECK(model_id<>'' AND length(model_id)<=128), failure_class TEXT NOT NULL CHECK(failure_class IN('RATE_LIMITED','QUOTA_EXCEEDED','MODEL_UNAVAILABLE','MODEL_REJECTED','CONFIG_INVALID')), model_contract_digest TEXT CHECK(model_contract_digest IS NULL OR length(model_contract_digest)=64), first_observed_at TEXT NOT NULL, ops_alert_id TEXT, status TEXT NOT NULL CHECK(status IN('OPEN','CLEARED')), cleared_at TEXT, cleared_by_call_id TEXT REFERENCES classifier_calls(call_id), CHECK((cleared_at IS NULL)=(status='OPEN')), CHECK(status<>'CLEARED' OR failure_class='CONFIG_INVALID' OR cleared_by_call_id IS NOT NULL)) STRICT;
CREATE UNIQUE INDEX classifier_outage_open ON classifier_outages(model_id,failure_class) WHERE status='OPEN';
"""


def _actual_ddl_digest(connection):
    rows = connection.execute(
        "SELECT type,name,sql FROM sqlite_master WHERE sql IS NOT NULL ORDER BY type,name"
    ).fetchall()
    return hashlib.sha256(
        b"".join(b"\0".join(field.encode() for field in row) + b"\n" for row in rows)
    ).hexdigest()


_ALLOWED = {
    EpisodeState.DISCOVERED: {EpisodeState.INVESTIGATING},
    EpisodeState.INVESTIGATING: {
        EpisodeState.EARLY_ALERTED,
        EpisodeState.ACTIVE,
        EpisodeState.DISCONFIRMED,
        EpisodeState.CLOSED,
    },
    EpisodeState.EARLY_ALERTED: {
        EpisodeState.ACTIVE,
        EpisodeState.CONFLICTED,
        EpisodeState.DISCONFIRMED,
    },
    EpisodeState.ACTIVE: {
        EpisodeState.ESCALATED,
        EpisodeState.DEESCALATED,
        EpisodeState.CONFLICTED,
        EpisodeState.DISCONFIRMED,
        EpisodeState.RESOLVED,
        EpisodeState.MUTED,
    },
    EpisodeState.ESCALATED: {
        EpisodeState.DEESCALATED,
        EpisodeState.CONFLICTED,
        EpisodeState.RESOLVED,
        EpisodeState.MUTED,
    },
    EpisodeState.DEESCALATED: {
        EpisodeState.ACTIVE,
        EpisodeState.ESCALATED,
        EpisodeState.RESOLVED,
        EpisodeState.CLOSED,
        EpisodeState.MUTED,
    },
    EpisodeState.CONFLICTED: {
        EpisodeState.ACTIVE,
        EpisodeState.DISCONFIRMED,
        EpisodeState.RESOLVED,
        EpisodeState.MUTED,
    },
    EpisodeState.DISCONFIRMED: {EpisodeState.CLOSED},
    EpisodeState.RESOLVED: {EpisodeState.CLOSED},
    EpisodeState.MUTED: set(),
    EpisodeState.CLOSED: set(),
}


class NewsEpisodeStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        existed = self.path.exists()
        old = os.umask(0o077)
        try:
            self.connection = sqlite3.connect(self.path, timeout=0.25)
        except sqlite3.Error as exc:
            raise NewsEpisodeStoreError("store unavailable") from exc
        finally:
            os.umask(old)
        try:
            os.chmod(self.path, 0o600)
            self.connection.execute("PRAGMA foreign_keys=ON")
            self.connection.execute("PRAGMA journal_mode=WAL")
            self.connection.execute("PRAGMA busy_timeout=250")
            if not existed:
                self.connection.executescript(_DDL)
                digest = _actual_ddl_digest(self.connection)
                self.connection.execute(
                    "INSERT INTO schema_meta VALUES(1,1,'news-episode-v1',?,?)",
                    (digest, "1970-01-01T00:00:00+00:00"),
                )
                self.connection.commit()
        except OSError as exc:
            self.connection.close()
            raise NewsEpisodeStoreError("store initialization failed") from exc
        except sqlite3.DatabaseError as exc:
            self.connection.close()
            message = str(exc).casefold()
            error = (
                NewsEpisodeStoreError
                if any(token in message for token in ("locked", "busy"))
                else NewsEpisodeSchemaError
            )
            raise error("store initialization failed") from exc
        try:
            meta = self.connection.execute(
                "SELECT schema_version,schema_id,ddl_sha256 FROM schema_meta WHERE singleton=1"
            ).fetchone()
        except sqlite3.Error as exc:
            self.connection.close()
            raise NewsEpisodeSchemaError("invalid schema") from exc
        if (
            meta is None
            or meta[:2] != (1, "news-episode-v1")
            or meta[2] != _actual_ddl_digest(self.connection)
        ):
            self.connection.close()
            raise NewsEpisodeSchemaError("ddl digest mismatch")
        try:
            self._ensure_canary_ledger()
            self._ensure_live_ledgers()
            self._ensure_taxonomy_ledgers()
            self._ensure_adjudication_ledgers()
        except sqlite3.Error as exc:
            self.connection.close()
            raise NewsEpisodeStoreError("canary ledger migration failed") from exc

    def _ensure_canary_ledger(self):
        """Additive, guarded upgrade for pre-G3 stores: create the canary
        ledger and re-seal the stored DDL digest in one transaction. Runs
        only after the digest check accepted the existing shape, so a
        tampered store still fails closed before any DDL executes."""
        if self.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='canary_attempts'"
        ).fetchone():
            return
        with self.connection:
            self.connection.execute(_CANARY_DDL)
            self.connection.execute(
                "UPDATE schema_meta SET ddl_sha256=? WHERE singleton=1",
                (_actual_ddl_digest(self.connection),),
            )

    def _ensure_live_ledgers(self):
        """Additive, guarded upgrade for pre-live stores: create the async
        work queue and live ledgers and re-seal the stored DDL digest in one
        transaction, mirroring the canary-ledger upgrade."""
        missing = [
            statement
            for statement in _LIVE_DDL.split(";\n")
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (statement.split("(", 1)[0].rsplit(" ", 1)[-1],),
            ).fetchone()
            is None
        ]
        if not missing:
            return
        with self.connection:
            for statement in missing:
                self.connection.execute(statement)
            self.connection.execute(
                "UPDATE schema_meta SET ddl_sha256=? WHERE singleton=1",
                (_actual_ddl_digest(self.connection),),
            )

    def _ensure_taxonomy_ledgers(self):
        """Additive, guarded upgrade for pre-taxonomy stores: create the
        registration ledgers and re-seal the stored DDL digest in one
        transaction, mirroring the canary/live-ledger upgrades. Rollback
        safe: pre-taxonomy code verifies stored-vs-actual digests and
        never reads these tables."""
        missing = [
            statement
            for statement in _TAXONOMY_DDL.split(";\n")
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (statement.split("(", 1)[0].rsplit(" ", 1)[-1],),
            ).fetchone()
            is None
        ]
        if not missing:
            return
        with self.connection:
            for statement in missing:
                self.connection.execute(statement)
            self.connection.execute(
                "UPDATE schema_meta SET ddl_sha256=? WHERE singleton=1",
                (_actual_ddl_digest(self.connection),),
            )

    def _ensure_adjudication_ledgers(self):
        """Additive, guarded upgrade for pre-stage-7 stores: create the
        adjudication job/marker ledgers and re-seal the stored DDL digest
        in one transaction, mirroring the taxonomy-ledger upgrade."""
        missing = [
            statement
            for statement in _ADJUDICATION_DDL.split(";\n")
            if self.connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                (statement.split("(", 1)[0].rsplit(" ", 1)[-1],),
            ).fetchone()
            is None
        ]
        if not missing:
            return
        with self.connection:
            for statement in missing:
                self.connection.execute(statement)
            self.connection.execute(
                "UPDATE schema_meta SET ddl_sha256=? WHERE singleton=1",
                (_actual_ddl_digest(self.connection),),
            )

    # -------------------------------------------- taxonomy registration

    _TAXONOMY_COLUMNS = (
        "unit_id",
        "kind",
        "unit_key",
        "canonical_key",
        "display_text",
        "payload_json",
        "state",
        "reason",
        "evidence_digest",
        "created_at",
        "updated_at",
    )
    # Automatic evidence may only ever move a unit up this ladder; the
    # model-adjudicated and tombstoned states are terminal for automation.
    _TAXONOMY_AUTO_RANK = {"CANDIDATE": 0, "PENDING_MODEL": 1, "VERIFIED_AUTO": 2}

    def _record_taxonomy_decision(
        self,
        unit_id,
        from_state,
        to_state,
        reason,
        evidence_digest,
        evidence_json,
        *,
        now,
    ):
        decision_id = hashlib.sha256(
            f"{unit_id}\0{from_state}\0{to_state}\0{reason}\0{evidence_digest}".encode()
        ).hexdigest()
        self.connection.execute(
            "INSERT OR IGNORE INTO taxonomy_decisions VALUES(?,?,?,?,?,?,?,?)",
            (
                decision_id,
                unit_id,
                from_state,
                to_state,
                reason,
                evidence_digest,
                evidence_json,
                now.isoformat(),
            ),
        )

    def record_taxonomy_observation(self, observation, *, now):
        """Idempotently apply one deterministic admission proposal.

        Collision policy (never last-write-wins): an ENTITY_ALIAS whose
        proposed canonical differs from the stored one drops to
        PENDING_MODEL/ALIAS_COLLISION with the original canonical kept,
        and a collided alias can only leave PENDING_MODEL through the
        explicit stage-7 resolution seam.

        Invariant: the unit's ``payload_json``/``evidence_digest`` pair is
        always one matched observation — every path that moves the digest
        writes the exactly corresponding payload in the same UPDATE, so
        stage-7 requests (built from payload) and job identity (keyed by
        digest) can never diverge. Prior observation evidence is preserved
        in the append-only taxonomy_decisions log, never on the unit."""
        kind = observation["kind"]
        if kind not in TAXONOMY_KINDS_V1:
            raise NewsEpisodeStoreError("invalid taxonomy kind")
        proposed = observation["proposed_state"]
        if proposed not in self._TAXONOMY_AUTO_RANK:
            raise NewsEpisodeStoreError("invalid automatic taxonomy state")
        unit_key = observation["unit_key"]
        unit_id = taxonomy_unit_id(kind, unit_key)
        evidence = dict(observation.get("evidence") or {})
        digest = taxonomy_evidence_digest(evidence)
        evidence_json = json.dumps(
            evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        )
        reason = observation["reason"]
        canonical = observation.get("canonical_key")
        with self.connection:
            row = self.connection.execute(
                "SELECT state,reason,canonical_key FROM taxonomy_units WHERE unit_id=?",
                (unit_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    f"INSERT INTO taxonomy_units"
                    f"({','.join(self._TAXONOMY_COLUMNS)})"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        unit_id,
                        kind,
                        unit_key,
                        canonical,
                        observation.get("display_text") or unit_key,
                        evidence_json,
                        proposed,
                        reason,
                        digest,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._record_taxonomy_decision(
                    unit_id, None, proposed, reason, digest, evidence_json, now=now
                )
                return "CREATED"
            state, stored_reason, stored_canonical = row
            if state == "TOMBSTONED":
                return "TOMBSTONED"
            if state in ("VERIFIED_MODEL", "REJECTED_MODEL"):
                return "ADJUDICATED"
            collided = (
                kind == "ENTITY_ALIAS"
                and canonical is not None
                and stored_canonical is not None
                and canonical != stored_canonical
            )
            sticky = state == "PENDING_MODEL" and stored_reason == "ALIAS_COLLISION"
            if (
                collided
                or (sticky and proposed != "PENDING_MODEL")
                or (sticky and reason == "ALIAS_COLLISION")
            ):
                if not sticky:
                    self.connection.execute(
                        "UPDATE taxonomy_units SET state='PENDING_MODEL',"
                        " reason='ALIAS_COLLISION',payload_json=?,"
                        " evidence_digest=?,updated_at=? WHERE unit_id=?",
                        (evidence_json, digest, now.isoformat(), unit_id),
                    )
                self._record_taxonomy_decision(
                    unit_id,
                    state,
                    "PENDING_MODEL",
                    "ALIAS_COLLISION",
                    digest,
                    evidence_json,
                    now=now,
                )
                return "COLLISION_PENDING"
            if self._TAXONOMY_AUTO_RANK[proposed] <= self._TAXONOMY_AUTO_RANK[state]:
                return (
                    "DUPLICATE"
                    if (proposed, reason) == (state, stored_reason)
                    else "KEPT"
                )
            self.connection.execute(
                "UPDATE taxonomy_units SET canonical_key=COALESCE(?,"
                "canonical_key),state=?,reason=?,payload_json=?,"
                "evidence_digest=?,updated_at=? WHERE unit_id=?",
                (
                    canonical,
                    proposed,
                    reason,
                    evidence_json,
                    digest,
                    now.isoformat(),
                    unit_id,
                ),
            )
            self._record_taxonomy_decision(
                unit_id, state, proposed, reason, digest, evidence_json, now=now
            )
            return "UPGRADED"

    def taxonomy_unit(self, kind, unit_key):
        row = self.connection.execute(
            f"SELECT {','.join(self._TAXONOMY_COLUMNS)} FROM taxonomy_units"
            " WHERE unit_id=?",
            (taxonomy_unit_id(kind, unit_key),),
        ).fetchone()
        return None if row is None else dict(zip(self._TAXONOMY_COLUMNS, row))

    def list_taxonomy_units(self, *, kind=None, state=None):
        clauses, values = [], []
        if kind is not None:
            clauses.append("kind=?")
            values.append(kind)
        if state is not None:
            clauses.append("state=?")
            values.append(state)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        rows = self.connection.execute(
            f"SELECT {','.join(self._TAXONOMY_COLUMNS)} FROM taxonomy_units"
            f"{where} ORDER BY kind,unit_key",
            values,
        ).fetchall()
        return tuple(dict(zip(self._TAXONOMY_COLUMNS, row)) for row in rows)

    def list_taxonomy_pending(self, *, limit):
        rows = self.connection.execute(
            f"SELECT {','.join(self._TAXONOMY_COLUMNS)} FROM taxonomy_units"
            " WHERE state='PENDING_MODEL' ORDER BY created_at,unit_id"
            " LIMIT ?",
            (int(limit),),
        ).fetchall()
        return tuple(dict(zip(self._TAXONOMY_COLUMNS, row)) for row in rows)

    def verified_entity_aliases(self):
        aliases = {}
        for canonical, alias in self.connection.execute(
            "SELECT canonical_key,unit_key FROM taxonomy_units"
            " WHERE kind='ENTITY_ALIAS' AND state='VERIFIED_AUTO'"
            " AND canonical_key IS NOT NULL"
            " ORDER BY canonical_key,unit_key"
        ):
            aliases[canonical] = aliases.get(canonical, ()) + (alias,)
        return aliases

    def taxonomy_decisions(self, unit_id):
        keys = (
            "decision_id",
            "unit_id",
            "from_state",
            "to_state",
            "reason",
            "evidence_digest",
            "evidence_json",
            "decided_at",
        )
        rows = self.connection.execute(
            f"SELECT {','.join(keys)} FROM taxonomy_decisions"
            " WHERE unit_id=? ORDER BY rowid",
            (unit_id,),
        ).fetchall()
        return tuple(dict(zip(keys, row)) for row in rows)

    def _resolve_taxonomy_pending_locked(
        self, unit_id, verdict, *, reason, now, canonical_key=None
    ):
        """PENDING_MODEL → VERIFIED_MODEL/REJECTED_MODEL inside the
        caller's open transaction.

        The unit's ``payload_json``/``evidence_digest`` pair is the latest
        deterministic observation's matched evidence and is immutable
        under adjudication: a verdict never rewrites it (only the
        dedicated ``canonical_key`` column may change), so job
        identity/dedupe keep keying the digest of exactly the payload that
        was sent. The verdict evidence and its digest are appended to
        taxonomy_decisions."""
        target = {"VERIFIED": "VERIFIED_MODEL", "REJECTED": "REJECTED_MODEL"}.get(
            verdict
        )
        if target is None:
            raise NewsEpisodeStoreError("invalid adjudication verdict")
        evidence = {"verdict": verdict, "reason": reason}
        if canonical_key is not None:
            evidence["canonical_key"] = canonical_key
        digest = taxonomy_evidence_digest(evidence)
        changed = self.connection.execute(
            "UPDATE taxonomy_units SET state=?,reason=?,"
            "canonical_key=COALESCE(?,canonical_key),"
            "updated_at=? WHERE unit_id=? AND state='PENDING_MODEL'",
            (target, reason, canonical_key, now.isoformat(), unit_id),
        ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("invalid pending resolution")
        self._record_taxonomy_decision(
            unit_id,
            "PENDING_MODEL",
            target,
            reason,
            digest,
            json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            now=now,
        )

    def resolve_taxonomy_pending(
        self, unit_id, verdict, *, reason, now, canonical_key=None
    ):
        """Stage-7 seam: only an explicit adjudication verdict moves a unit
        out of PENDING_MODEL; there is no automatic fallback."""
        with self.connection:
            self._resolve_taxonomy_pending_locked(
                unit_id, verdict, reason=reason, now=now, canonical_key=canonical_key
            )

    def tombstone_taxonomy_unit(self, unit_id, note, *, now):
        evidence = {"note": note}
        digest = taxonomy_evidence_digest(evidence)
        with self.connection:
            row = self.connection.execute(
                "SELECT state FROM taxonomy_units WHERE unit_id=?", (unit_id,)
            ).fetchone()
            if row is None:
                raise NewsEpisodeStoreError("taxonomy unit absent")
            self.connection.execute(
                "UPDATE taxonomy_units SET state='TOMBSTONED',reason=?,"
                "updated_at=? WHERE unit_id=?",
                (
                    f"TOMBSTONED:{note}"[:120] if note else "TOMBSTONED",
                    now.isoformat(),
                    unit_id,
                ),
            )
            self._record_taxonomy_decision(
                unit_id,
                row[0],
                "TOMBSTONED",
                note or "TOMBSTONED",
                digest,
                json.dumps(
                    evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
                ),
                now=now,
            )

    # ------------------------------------ stage 7: adjudication job ledger

    _ADJUDICATION_JOB_COLUMNS = (
        "job_id",
        "request_digest",
        "unit_ids_json",
        "route_id",
        "state",
        "attempts",
        "outcome",
        "response_digest",
        "started_at",
        "settled_at",
    )

    def list_taxonomy_adjudication_eligible(self, *, limit):
        """Pending units still owed a batch slot: PENDING_MODEL rows with
        no durable deferral/expiry marker for their CURRENT evidence
        digest. New evidence (a digest change) automatically re-qualifies
        a previously deferred unit."""
        rows = self.connection.execute(
            f"SELECT {','.join('u.' + column for column in self._TAXONOMY_COLUMNS)}"
            " FROM taxonomy_units u WHERE u.state='PENDING_MODEL'"
            " AND NOT EXISTS(SELECT 1 FROM taxonomy_adjudication_units m"
            " WHERE m.unit_id=u.unit_id"
            " AND m.evidence_digest=u.evidence_digest)"
            " ORDER BY u.created_at,u.unit_id LIMIT ?",
            (int(limit),),
        ).fetchall()
        return tuple(dict(zip(self._TAXONOMY_COLUMNS, row)) for row in rows)

    def begin_taxonomy_adjudication(
        self, job_id, *, request_digest, unit_ids, route_id, now, retry_max=0
    ):
        """Atomically reserve one provider start for one pending batch.

        The job_id (digest over the batch's exact unit/evidence pairs) is
        the primary key, so the same pending evidence can never start
        twice: an in-flight duplicate reports IN_FLIGHT, a settled batch
        reports REPLAY_SETTLED (no new call, no new transition), and a
        FAILED/UNKNOWN batch restarts only inside the explicit bounded
        retry budget (default 0) — otherwise EXHAUSTED."""
        unit_ids_json = json.dumps(sorted(unit_ids), separators=(",", ":"))
        with self.connection:
            row = self.connection.execute(
                "SELECT state,attempts FROM taxonomy_adjudication_jobs WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None:
                self.connection.execute(
                    "INSERT INTO taxonomy_adjudication_jobs"
                    f"({','.join(self._ADJUDICATION_JOB_COLUMNS)})"
                    " VALUES(?,?,?,?,'STARTED',1,NULL,NULL,?,NULL)",
                    (job_id, request_digest, unit_ids_json, route_id, now.isoformat()),
                )
                return "STARTED"
            state, attempts = row
            if state == "STARTED":
                return "IN_FLIGHT"
            if state == "SETTLED":
                return "REPLAY_SETTLED"
            if attempts > int(retry_max):
                return "EXHAUSTED"
            self.connection.execute(
                "UPDATE taxonomy_adjudication_jobs SET state='STARTED',"
                "attempts=attempts+1,outcome=NULL,response_digest=NULL,"
                "route_id=?,started_at=?,settled_at=NULL WHERE job_id=?",
                (route_id, now.isoformat(), job_id),
            )
            return "STARTED"

    def settle_taxonomy_adjudication_failure(self, job_id, outcome, *, now):
        """STARTED → FAILED with a typed terminal outcome; units untouched
        (fail closed — they stay PENDING_MODEL)."""
        if outcome not in ("INVALID_RESPONSE", "TRANSPORT_FAIL", "STATE_CONFLICT"):
            raise NewsEpisodeStoreError("invalid adjudication failure")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE taxonomy_adjudication_jobs SET state='FAILED',"
                "outcome=?,settled_at=? WHERE job_id=? AND state='STARTED'",
                (outcome, now.isoformat(), job_id),
            ).rowcount
            if changed != 1:
                raise NewsEpisodeStoreError("adjudication job is not in flight")

    def reconcile_taxonomy_adjudication(self, *, now, stale_after_seconds=900):
        """Crash recovery: STARTED jobs older than the stale window become
        explicit UNKNOWN terminals (attempt stays consumed; a restart
        requires the explicit retry budget)."""
        cutoff = (now - timedelta(seconds=stale_after_seconds)).isoformat()
        with self.connection:
            return self.connection.execute(
                "UPDATE taxonomy_adjudication_jobs SET state='UNKNOWN',"
                "outcome='CRASH_UNKNOWN',settled_at=? WHERE state='STARTED'"
                " AND started_at<=?",
                (now.isoformat(), cutoff),
            ).rowcount

    def _mark_taxonomy_deferral_locked(
        self, unit_id, status, reason, *, now, job_id=None
    ):
        row = self.connection.execute(
            "SELECT state,evidence_digest FROM taxonomy_units WHERE unit_id=?",
            (unit_id,),
        ).fetchone()
        if row is None or row[0] != "PENDING_MODEL":
            raise NewsEpisodeStoreError("unit is not pending")
        evidence = {"status": status, "reason": reason}
        digest = taxonomy_evidence_digest(evidence)
        self.connection.execute(
            "INSERT OR IGNORE INTO taxonomy_adjudication_units VALUES(?,?,?,?,?,?)",
            (unit_id, row[1], job_id, status, reason, now.isoformat()),
        )
        self._record_taxonomy_decision(
            unit_id,
            "PENDING_MODEL",
            "PENDING_MODEL",
            reason,
            digest,
            json.dumps(
                evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=False
            ),
            now=now,
        )

    def mark_taxonomy_adjudication_deferral(
        self, unit_id, status, reason, *, now, job_id=None
    ):
        """Durable DEFERRED/EXPIRED/EXHAUSTED marker for the unit's current
        evidence: the unit stays PENDING_MODEL with its history appended
        (never deleted), but leaves the adjudication queue until its
        evidence changes."""
        with self.connection:
            self._mark_taxonomy_deferral_locked(
                unit_id, status, reason, now=now, job_id=job_id
            )

    def apply_taxonomy_adjudication(self, job_id, results, *, response_digest, now):
        """Settle one STARTED job with its validated per-unit verdicts in
        a single transaction: VERIFIED/REJECTED resolve the unit, DEFERRED
        marks it for its current evidence, and the job becomes SETTLED —
        all or nothing, so a replayed settled response can never apply a
        second transition."""
        with self.connection:
            row = self.connection.execute(
                "SELECT state,unit_ids_json FROM taxonomy_adjudication_jobs"
                " WHERE job_id=?",
                (job_id,),
            ).fetchone()
            if row is None or row[0] != "STARTED":
                raise NewsEpisodeStoreError("adjudication job is not in flight")
            expected = set(json.loads(row[1]))
            seen = [result["unit_id"] for result in results]
            if len(seen) != len(set(seen)) or set(seen) != expected:
                raise NewsEpisodeStoreError("adjudication results do not match the job")
            for result in results:
                verdict = result["verdict"]
                reason = str(result.get("reason") or "adjudicated")
                if verdict == "DEFERRED":
                    self._mark_taxonomy_deferral_locked(
                        result["unit_id"], "DEFERRED", reason, now=now, job_id=job_id
                    )
                else:
                    self._resolve_taxonomy_pending_locked(
                        result["unit_id"],
                        verdict,
                        reason=reason,
                        now=now,
                        canonical_key=result.get("canonical_key"),
                    )
            self.connection.execute(
                "UPDATE taxonomy_adjudication_jobs SET state='SETTLED',"
                "outcome='OK',response_digest=?,settled_at=?"
                " WHERE job_id=?",
                (response_digest, now.isoformat(), job_id),
            )

    def taxonomy_adjudication_job(self, job_id):
        row = self.connection.execute(
            f"SELECT {','.join(self._ADJUDICATION_JOB_COLUMNS)}"
            " FROM taxonomy_adjudication_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        return None if row is None else dict(zip(self._ADJUDICATION_JOB_COLUMNS, row))

    def list_taxonomy_adjudication_jobs(self):
        rows = self.connection.execute(
            f"SELECT {','.join(self._ADJUDICATION_JOB_COLUMNS)}"
            " FROM taxonomy_adjudication_jobs ORDER BY started_at,job_id"
        ).fetchall()
        return tuple(dict(zip(self._ADJUDICATION_JOB_COLUMNS, row)) for row in rows)

    # ---------------------------------------- async classifier work queue

    _WORK_COLUMNS = (
        "classification_identity",
        "purpose",
        "candidate_id",
        "episode_id",
        "provider",
        "model_id",
        "model_contract_digest",
        "prompt_policy_digest",
        "input_evidence_digest",
        "output_schema_id",
        "evidence_json",
        "state",
        "attempts",
        "lease_owner",
        "lease_expires_at",
        "last_exit",
        "enqueued_at",
        "updated_at",
    )

    def enqueue_classifier_work(self, item, *, now):
        """Idempotently freeze one classifier request per identity.

        Returns the queue state after the ensure: a fresh insert reports
        "PENDING"; an existing row reports its current state unchanged."""
        request_columns = self._WORK_COLUMNS[:11]
        with self.connection:
            self.connection.execute(
                f"INSERT OR IGNORE INTO classifier_work_queue"
                f"({','.join(self._WORK_COLUMNS)}) VALUES("
                f"{','.join('?' for _ in request_columns)},"
                "'PENDING',0,NULL,NULL,NULL,?,?)",
                tuple(item.get(column) for column in request_columns)
                + (now.isoformat(), now.isoformat()),
            )
        return self.classifier_work(item["classification_identity"])["state"]

    def classifier_work(self, identity):
        row = self.connection.execute(
            f"SELECT {','.join(self._WORK_COLUMNS)} FROM"
            " classifier_work_queue WHERE classification_identity=?",
            (identity,),
        ).fetchone()
        return None if row is None else dict(zip(self._WORK_COLUMNS, row))

    def lease_classifier_work(self, owner, *, now, lease_seconds, limit, attempt_max):
        """Atomically claim up to ``limit`` runnable items for one owner.

        Expired leases first fall back to PENDING (crashed worker) or
        EXHAUSTED once the attempt cap is spent; a live lease is never
        stolen. Every successful claim consumes one attempt up front."""
        expiry = (now + timedelta(seconds=lease_seconds)).isoformat()
        with self.connection:
            self.connection.execute(
                "UPDATE classifier_work_queue SET"
                " state=CASE WHEN attempts>=:cap THEN 'EXHAUSTED'"
                " ELSE 'PENDING' END,"
                " lease_owner=NULL,lease_expires_at=NULL,updated_at=:now"
                " WHERE state='LEASED' AND lease_expires_at<=:now",
                {"cap": int(attempt_max), "now": now.isoformat()},
            )
            self.connection.execute(
                "UPDATE classifier_work_queue SET state='EXHAUSTED',"
                " updated_at=:now WHERE state='PENDING' AND attempts>=:cap",
                {"cap": int(attempt_max), "now": now.isoformat()},
            )
            identities = [
                row[0]
                for row in self.connection.execute(
                    "SELECT classification_identity FROM classifier_work_queue"
                    " WHERE state='PENDING' AND attempts<:cap"
                    " ORDER BY enqueued_at,classification_identity LIMIT :limit",
                    {"cap": int(attempt_max), "limit": int(limit)},
                )
            ]
            for identity in identities:
                self.connection.execute(
                    "UPDATE classifier_work_queue SET state='LEASED',"
                    " attempts=attempts+1,lease_owner=?,lease_expires_at=?,"
                    " updated_at=? WHERE classification_identity=?",
                    (owner, expiry, now.isoformat(), identity),
                )
        return tuple(self.classifier_work(identity) for identity in identities)

    def settle_classifier_work(
        self, identity, state, *, now, last_exit=None, refund_attempt=False
    ):
        if state not in ("PENDING", "DONE", "EXHAUSTED", "STALE"):
            raise NewsEpisodeStoreError("invalid work settle state")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE classifier_work_queue SET state=?,"
                " attempts=MAX(0,attempts-?),lease_owner=NULL,"
                " lease_expires_at=NULL,last_exit=?,updated_at=?"
                " WHERE classification_identity=? AND state='LEASED'",
                (
                    state,
                    1 if refund_attempt else 0,
                    last_exit,
                    now.isoformat(),
                    identity,
                ),
            ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("invalid work settle transition")

    def classifier_work_counts(self):
        return dict(
            self.connection.execute(
                "SELECT state,count(*) FROM classifier_work_queue"
                " GROUP BY state ORDER BY state"
            )
        )

    # ------------------------------------------------- live-lane ledgers

    def record_canary_slot_proof(self, activation_id, slots, transport_starts, *, now):
        """Durably prove the canary's duplicate-observation rollback read.

        Only a SENT canary attempt can be proven, exactly one transport
        start is acceptable, and at least two observed post-settle pulse
        slots are required. Identical replays are DUPLICATE; divergent
        evidence fails closed with zero writes."""
        slots = tuple(str(slot) for slot in slots)
        if len(slots) < 2:
            raise NewsEpisodeStoreError("slot proof requires two pulse slots")
        attempt = self.canary_attempt(activation_id)
        if attempt is None or attempt["outcome"] != "SENT":
            raise NewsEpisodeStoreError("slot proof requires a SENT canary")
        slots_json = json.dumps(list(slots), separators=(",", ":"))
        with self.connection:
            prior = self.connection.execute(
                "SELECT slots_json,transport_starts FROM canary_slot_proofs"
                " WHERE activation_id=?",
                (activation_id,),
            ).fetchone()
            if prior is not None:
                if prior != (slots_json, transport_starts):
                    raise NewsEpisodeStoreError("slot proof conflict")
                return ReplayResult.DUPLICATE
            self.connection.execute(
                "INSERT INTO canary_slot_proofs VALUES(?,?,?,?,?)",
                (activation_id, "SENT", slots_json, transport_starts, now.isoformat()),
            )
        return ReplayResult.CREATED

    def canary_slot_proof(self, activation_id):
        row = self.connection.execute(
            "SELECT activation_id,settled_outcome,slots_json,"
            "transport_starts,recorded_at FROM canary_slot_proofs"
            " WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "activation_id",
                        "settled_outcome",
                        "slots_json",
                        "transport_starts",
                        "recorded_at",
                    ),
                    row,
                )
            )
        )

    def record_live_trip(self, activation_id, reason, *, now):
        """First durable delivery-only trip per activation wins; no reset."""
        with self.connection:
            created = self.connection.execute(
                "INSERT OR IGNORE INTO live_delivery_trips VALUES(?,?,?)",
                (activation_id, reason, now.isoformat()),
            ).rowcount
        return created == 1

    def live_trip(self, activation_id):
        row = self.connection.execute(
            "SELECT activation_id,reason,tripped_at FROM live_delivery_trips"
            " WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(zip(("activation_id", "reason", "tripped_at"), row))
        )

    def canary_attempt(self, activation_id):
        row = self.connection.execute(
            "SELECT activation_id,ops_alert_id,reserved_at,outcome,settled_at FROM canary_attempts WHERE activation_id=?",
            (activation_id,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "activation_id",
                        "ops_alert_id",
                        "reserved_at",
                        "outcome",
                        "settled_at",
                    ),
                    row,
                )
            )
        )

    def reserve_canary_attempt(self, activation_id, ops_alert_id, *, now):
        """Atomically consume the one-attempt cap; False means already consumed."""
        try:
            with self.connection:
                self.connection.execute(
                    "INSERT INTO canary_attempts VALUES(?,?,?,'RESERVED',NULL)",
                    (activation_id, ops_alert_id, now.isoformat()),
                )
        except sqlite3.IntegrityError:
            return False
        return True

    def settle_canary_attempt(self, activation_id, outcome, *, now):
        if outcome not in ("SENT", "FAILED", "UNKNOWN"):
            raise NewsEpisodeStoreError("invalid canary outcome")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE canary_attempts SET outcome=?,settled_at=? WHERE activation_id=? AND outcome='RESERVED'",
                (outcome, now.isoformat(), activation_id),
            ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("invalid canary settle transition")

    def close(self):
        self.connection.close()

    def upsert(self, episode_key, evidence, now, *, entities=()):
        title = episode_key
        episode_key = (
            episode_key
            if len(episode_key) == 64
            and all(c in "0123456789abcdef" for c in episode_key)
            else hashlib.sha256(episode_key.encode()).hexdigest()
        )
        row = self.connection.execute(
            "SELECT episode_id,state,revision FROM episodes WHERE episode_key=?",
            (episode_key,),
        ).fetchone()
        episode_id = "nep-" + hashlib.sha256(episode_key.encode()).hexdigest()[:24]
        prior = () if row is None else self.revisions(episode_id)[-1].evidence
        combined = prior + tuple(
            item
            for item in evidence
            if item.evidence_id not in {e.evidence_id for e in prior}
        )
        statuses = {NewsClaimStatusV1(e.claim_status) for e in combined}
        groups = {}
        for item in combined:
            if item.conflict_group:
                groups.setdefault(item.conflict_group, set()).add(item.claim_status)
        conflict = any(len(values) > 1 for values in groups.values())
        state = (
            EpisodeState.DISCONFIRMED
            if NewsClaimStatusV1.RETRACTED in statuses
            else (EpisodeState.CONFLICTED if conflict else EpisodeState.INVESTIGATING)
        )
        revision = 1 if row is None else row[2] + 1
        encoded = json.dumps(
            [[e.evidence_id, e.claim_status, e.conflict_group] for e in combined],
            separators=(",", ":"),
        )
        with self.connection:
            if row is None:
                self.connection.execute(
                    "INSERT INTO episodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        episode_id,
                        episode_key,
                        None,
                        None,
                        title,
                        "UNCERTAIN_CLAIM",
                        state.value,
                        "ORDINARY",
                        0,
                        revision,
                        now.isoformat(),
                        None,
                        None,
                        None,
                        None,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
            else:
                self.connection.execute(
                    "UPDATE episodes SET state=?,revision=?,updated_at=? WHERE episode_id=?",
                    (state.value, revision, now.isoformat(), episode_id),
                )
            self.connection.execute(
                "INSERT INTO episode_revisions VALUES(?,?,?,?,?)",
                (episode_id, revision, state.value, encoded, now.isoformat()),
            )
            for ordinal, entity in enumerate(entities):
                self.connection.execute(
                    "INSERT OR IGNORE INTO episode_entities VALUES(?,?,?)",
                    (episode_id, entity, "PRIMARY" if ordinal == 0 else "SECONDARY"),
                )
            for item in evidence:
                self.connection.execute(
                    "INSERT OR IGNORE INTO episode_evidence VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        item.evidence_id,
                        episode_id,
                        None,
                        item.evidence_id,
                        "LOCAL_NEWS",
                        NewsClaimStatusV1(item.claim_status).value,
                        item.conflict_group,
                        1,
                        None,
                        now.isoformat(),
                        None,
                        None,
                    ),
                )
        return NewsEpisode(episode_id, episode_key, state, revision, combined, now)

    def upsert_with_cause(self, episode_key, evidence, now, *, cause_key, entities=()):
        prior = self.connection.execute(
            "SELECT episode_key FROM episodes WHERE cause_key=? AND state NOT IN('CLOSED','RESOLVED','DISCONFIRMED') ORDER BY first_seen_at LIMIT 1",
            (cause_key,),
        ).fetchone()
        if prior is not None:
            return self.upsert(prior[0], evidence, now, entities=entities)
        episode = self.upsert(episode_key, evidence, now, entities=entities)
        with self.connection:
            self.connection.execute(
                "UPDATE episodes SET cause_key=? WHERE episode_id=?",
                (cause_key, episode.episode_id),
            )
        return episode

    def record_discovery_run(self, run, candidates, *, now):
        with self.connection:
            self.connection.execute(
                "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)", run
            )
            for candidate in candidates:
                self.connection.execute(
                    "INSERT INTO discovery_candidates VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    candidate,
                )
        return ReplayResult.CREATED

    def discovery_run_exists_for_window(self, window_to):
        return (
            self.connection.execute(
                "SELECT 1 FROM discovery_runs WHERE window_to=? LIMIT 1", (window_to,)
            ).fetchone()
            is not None
        )

    def record_discovery_failure(self, *, window_from, window_to, status, now):
        run_id = (
            "ndr-"
            + hashlib.sha256(
                f"{'0' * 64}\0{window_to.isoformat()}\0{status}".encode()
            ).hexdigest()[:24]
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO discovery_runs VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    run_id,
                    window_from.isoformat(),
                    window_to.isoformat(),
                    "0" * 64,
                    "0" * 64,
                    0,
                    0,
                    0,
                    status,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
        return run_id

    # Candidate lifecycle (SDS §6.6): claim-eligible == NOT expiry-eligible on
    # the same tick ``now``; the two predicates are disjoint by construction.
    _CANDIDATE_COLUMNS = (
        "candidate_id",
        "run_id",
        "lane",
        "entity_key",
        "phrase_text",
        "distinct_story_count",
        "distinct_source_count",
        "evidence_story_keys_json",
        "state",
        "episode_id",
        "created_at",
        "updated_at",
    )
    _CLAIM_ELIGIBLE_SQL = (
        "(d.episode_id IS NOT NULL OR d.created_at > :ttl_deadline"
        " OR EXISTS(SELECT 1 FROM classifier_calls c WHERE c.candidate_id=d.candidate_id"
        " AND c.state='SETTLED' AND c.exit='OK'))"
    )

    def list_open_candidates(
        self, *, now, ttl_seconds, lease_seconds, backoff_cap_seconds, limit_per_lane
    ):
        parameters = {
            "now": now.isoformat(),
            "ttl_deadline": (now - timedelta(seconds=ttl_seconds)).isoformat(),
            "lease": float(lease_seconds),
            "cap": float(backoff_cap_seconds),
            "limit": int(limit_per_lane),
        }
        columns = ",".join("d." + name for name in self._CANDIDATE_COLUMNS)
        recovery = self.connection.execute(
            f"SELECT {columns} FROM discovery_candidates d"
            f" WHERE d.state='ADMITTED_FOR_INVESTIGATION' AND {self._CLAIM_ELIGIBLE_SQL}"
            " AND (julianday(:now) - julianday(d.updated_at)) * 86400.0 >="
            " MIN(:cap, MAX(:lease, (julianday(d.updated_at) - julianday(d.created_at)) * 86400.0))"
            " ORDER BY d.updated_at ASC, d.candidate_id ASC LIMIT :limit",
            parameters,
        ).fetchall()
        fresh = self.connection.execute(
            f"SELECT {columns} FROM discovery_candidates d"
            f" WHERE d.state='SURFACED' AND {self._CLAIM_ELIGIBLE_SQL}"
            " ORDER BY d.updated_at ASC, d.candidate_id ASC LIMIT :limit",
            parameters,
        ).fetchall()
        as_row = lambda values: dict(zip(self._CANDIDATE_COLUMNS, values))
        return tuple(as_row(row) for row in recovery), tuple(
            as_row(row) for row in fresh
        )

    def claim_candidate(self, candidate_id, *, expected_updated_at, now):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE discovery_candidates SET state='ADMITTED_FOR_INVESTIGATION',updated_at=:now"
                " WHERE candidate_id=:cid AND updated_at=:expected"
                " AND (state='SURFACED' OR (state='ADMITTED_FOR_INVESTIGATION' AND :now > updated_at))",
                {
                    "cid": candidate_id,
                    "expected": expected_updated_at,
                    "now": now.isoformat(),
                },
            ).rowcount
        return changed == 1

    def release_candidate(self, candidate_id, *, now):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE discovery_candidates SET state='SURFACED',updated_at=?"
                " WHERE candidate_id=? AND state='ADMITTED_FOR_INVESTIGATION'",
                (now.isoformat(), candidate_id),
            ).rowcount
        return changed == 1

    def attach_candidate_episode(self, candidate_id, episode_id, *, now):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE discovery_candidates SET episode_id=?,updated_at=?"
                " WHERE candidate_id=? AND state='ADMITTED_FOR_INVESTIGATION' AND episode_id IS NULL",
                (episode_id, now.isoformat(), candidate_id),
            ).rowcount
        return changed == 1

    def finalize_candidate(self, candidate_id, state, *, now):
        if state not in ("MERGED_INTO_EPISODE", "DROPPED_NOISE", "EXPIRED"):
            raise NewsEpisodeStoreError("invalid terminal candidate state")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE discovery_candidates SET state=?,updated_at=?"
                " WHERE candidate_id=? AND state IN('SURFACED','ADMITTED_FOR_INVESTIGATION')",
                (state, now.isoformat(), candidate_id),
            ).rowcount
        return changed == 1

    def expire_stale_candidates(self, *, now, ttl_seconds, limit):
        with self.connection:
            return self.connection.execute(
                "UPDATE discovery_candidates SET state='EXPIRED', updated_at=:now"
                " WHERE candidate_id IN ("
                "   SELECT d.candidate_id FROM discovery_candidates d"
                "    WHERE d.state IN ('SURFACED','ADMITTED_FOR_INVESTIGATION')"
                "      AND d.created_at <= :ttl_deadline"
                "      AND d.episode_id IS NULL"
                "      AND NOT EXISTS (SELECT 1 FROM classifier_calls c"
                "                       WHERE c.candidate_id = d.candidate_id"
                "                         AND c.state = 'SETTLED' AND c.exit = 'OK')"
                "    ORDER BY d.created_at LIMIT :limit)",
                {
                    "now": now.isoformat(),
                    "limit": int(limit),
                    "ttl_deadline": (now - timedelta(seconds=ttl_seconds)).isoformat(),
                },
            ).rowcount

    def count_candidate_classifier_calls(self, candidate_id):
        return self.connection.execute(
            "SELECT count(*) FROM classifier_calls WHERE candidate_id=?",
            (candidate_id,),
        ).fetchone()[0]

    def candidate_checkpoint(self, candidate_id):
        row = self.connection.execute(
            "SELECT d.episode_id,"
            " (SELECT c.call_id FROM classifier_calls c WHERE c.candidate_id=d.candidate_id"
            "   AND c.state='SETTLED' AND c.exit='OK' ORDER BY c.attempt_no LIMIT 1)"
            " FROM discovery_candidates d WHERE d.candidate_id=?",
            (candidate_id,),
        ).fetchone()
        return (None, None) if row is None else (row[0], row[1])

    def revisions(self, episode_id):
        rows = self.connection.execute(
            "SELECT e.episode_key,r.state,r.revision,r.evidence_json,r.observed_at FROM episode_revisions r JOIN episodes e USING(episode_id) WHERE r.episode_id=? ORDER BY r.revision",
            (episode_id,),
        ).fetchall()
        return tuple(
            NewsEpisode(
                episode_id,
                k,
                EpisodeState(s),
                n,
                tuple(Evidence(*e) for e in json.loads(raw)),
                datetime.fromisoformat(at),
            )
            for k, s, n, raw, at in rows
        )

    def transition(self, episode_id, expected, target, now):
        row = self.connection.execute(
            "SELECT state FROM episodes WHERE episode_id=?", (episode_id,)
        ).fetchone()
        if (
            row is None
            or row[0] != expected.value
            or target not in _ALLOWED.get(expected, set())
        ):
            raise NewsEpisodeTransitionError("invalid episode transition")
        tid = (
            "ntr-"
            + hashlib.sha256(
                f"{episode_id}\0{expected.value}\0{target.value}\0{now.isoformat()}".encode()
            ).hexdigest()[:24]
        )
        with self.connection:
            self.connection.execute(
                "UPDATE episodes SET state=?,updated_at=? WHERE episode_id=?",
                (target.value, now.isoformat(), episode_id),
            )
            self.connection.execute(
                "INSERT INTO episode_transitions VALUES(?,?,?,?,?,?,?,?)",
                (
                    tid,
                    episode_id,
                    expected.value,
                    target.value,
                    None,
                    None,
                    "[]",
                    now.isoformat(),
                ),
            )

    def replace_scenario_nodes(self, episode_id, nodes, *, now):
        """Persist any useful scenario shape; deliberately no node-count invariant."""
        with self.connection:
            for ordinal, node in enumerate(nodes):
                self.connection.execute(
                    "INSERT OR REPLACE INTO scenario_nodes VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        node.node_id,
                        episode_id,
                        node.parent_id,
                        ordinal,
                        "PARALLEL_BRANCH",
                        node.node_id,
                        "[]",
                        "[]",
                        "",
                        int(node.material_action_condition),
                        node.state,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )

    def scenario_nodes(self, episode_id):
        return tuple(
            self.connection.execute(
                "SELECT node_id,parent_node_id,ordinal,kind,title,material_action_condition,state FROM scenario_nodes WHERE episode_id=? ORDER BY ordinal,node_id",
                (episode_id,),
            )
        )

    def mute_episode(self, episode_id, note, *, now):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE episodes SET state='MUTED',muted_at=?,mute_note=?,updated_at=? WHERE episode_id=? AND state<>'MUTED'",
                (now.isoformat(), note, now.isoformat(), episode_id),
            ).rowcount
        if not changed:
            raise NewsEpisodeTransitionError("episode absent or already muted")

    def episode_state(self, episode_id):
        row = self.connection.execute(
            "SELECT state FROM episodes WHERE episode_id=?", (episode_id,)
        ).fetchone()
        return None if row is None else row[0]

    def set_episode_event_class(self, episode_id, event_class, *, now):
        """Persist the deterministically judged class on the CURRENT episode
        (SDS 2026-09-01 W6/§E.2): UPDATE only — no DDL, no retroactive
        backfill path; callers apply it solely to the episode of the
        revision being admitted."""
        if event_class not in ("CORE_BREAKING", "ORDINARY"):
            raise NewsEpisodeStoreError("invalid event class")
        with self.connection:
            changed = self.connection.execute(
                "UPDATE episodes SET event_class=?,updated_at=? WHERE episode_id=?",
                (event_class, now.isoformat(), episode_id),
            ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("episode absent for event class")

    def mark_early_alerted(self, episode_id, *, now):
        with self.connection:
            self.connection.execute(
                "UPDATE episodes SET state='EARLY_ALERTED',event_class='CORE_BREAKING',provisional=1,updated_at=? WHERE episode_id=?",
                (now.isoformat(), episode_id),
            )

    def mark_disconfirmed(self, episode_id, *, now):
        with self.connection:
            self.connection.execute(
                "UPDATE episodes SET state='DISCONFIRMED',provisional=0,updated_at=? WHERE episode_id=?",
                (now.isoformat(), episode_id),
            )

    def next_alert_revision_no(self, episode_id):
        return self.connection.execute(
            "SELECT COALESCE(max(revision_no),0)+1 FROM alert_revisions WHERE episode_id=?",
            (episode_id,),
        ).fetchone()[0]

    def admit_alert_revision(self, revision, *, now):
        body = revision["body_text"]
        content_digest = hashlib.sha256(body.encode()).hexdigest()
        revision_id = hashlib.sha256(
            json.dumps(
                {
                    "episode_id": revision["episode_id"],
                    "revision_no": revision["revision_no"],
                    "content_digest": content_digest,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        values = (
            revision_id,
            revision["episode_id"],
            revision["revision_no"],
            revision["revision_kind"],
            content_digest,
            revision["portfolio_context_digest"],
            revision["confidence"],
            revision["material_change_kind"],
            revision["material_change_observed_at"].isoformat(),
            int(revision.get("acknowledges_prior_reaction", False)),
            body,
            json.dumps(revision["links"], separators=(",", ":")),
            revision_id[:32],
            "PENDING_OUTBOX",
            None,
            None,
            now.isoformat(),
            now.isoformat(),
        )
        with self.connection:
            prior = self.connection.execute(
                "SELECT * FROM alert_revisions WHERE alert_revision_id=?",
                (revision_id,),
            ).fetchone()
            if prior is None:
                self.connection.execute(
                    "INSERT INTO alert_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    values,
                )
        return revision_id

    def list_pending_outbox_revisions(self):
        rows = self.connection.execute("""SELECT r.alert_revision_id,r.episode_id,r.revision_no,r.body_text,
          r.callback_ref,r.created_at,e.event_class,r.material_change_observed_at
          FROM alert_revisions r JOIN episodes e USING(episode_id)
          WHERE r.delivery_state='PENDING_OUTBOX' ORDER BY r.created_at,r.alert_revision_id""").fetchall()
        keys = (
            "alert_revision_id",
            "episode_id",
            "revision_no",
            "body_text",
            "callback_ref",
            "created_at",
            "event_class",
            "material_change_observed_at",
        )
        return tuple(dict(zip(keys, row)) for row in rows)

    def mark_revision_outboxed(self, alert_revision_id, ops_alert_id, *, now):
        with self.connection:
            row = self.connection.execute(
                "SELECT delivery_state,ops_alert_id FROM alert_revisions WHERE alert_revision_id=?",
                (alert_revision_id,),
            ).fetchone()
            if row == ("OUTBOXED", ops_alert_id):
                return ReplayResult.DUPLICATE
            changed = self.connection.execute(
                "UPDATE alert_revisions SET delivery_state='OUTBOXED',ops_alert_id=?,updated_at=? WHERE alert_revision_id=? AND delivery_state='PENDING_OUTBOX' AND ops_alert_id IS NULL",
                (ops_alert_id, now.isoformat(), alert_revision_id),
            ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("invalid revision outbox transition")
        return ReplayResult.CREATED

    def bind_delivery(self, alert_revision_id, provider_message_id, *, now):
        with self.connection:
            changed = self.connection.execute(
                "UPDATE alert_revisions SET delivery_state='DELIVERY_BOUND',provider_message_id=?,updated_at=? WHERE alert_revision_id=? AND delivery_state='OUTBOXED'",
                (str(provider_message_id), now.isoformat(), alert_revision_id),
            ).rowcount
        if changed != 1:
            raise NewsEpisodeStoreError("invalid delivery binding")

    def bind_delivery_by_ops_alert(self, ops_alert_id, provider_message_id, *, now):
        row = self.connection.execute(
            "SELECT alert_revision_id FROM alert_revisions WHERE ops_alert_id=?",
            (ops_alert_id,),
        ).fetchone()
        if row is None:
            raise NewsEpisodeStoreError("ops alert revision absent")
        return self.bind_delivery(row[0], provider_message_id, now=now)

    def hold_morning_revision(
        self, episode_id, revision_no, body, *, now, release_not_before
    ):
        """Idempotent replay on the (episode_id, revision_no) axis (SDS §6.7):
        identical replays are DUPLICATE, released holds are never re-held, and
        a content mismatch fails closed with zero writes."""
        content = hashlib.sha256(body.encode()).hexdigest()
        revision_id = hashlib.sha256(
            f"{episode_id}\0{revision_no}\0{content}".encode()
        ).hexdigest()
        callback = revision_id[:32]
        with self.connection:
            prior = self.connection.execute(
                "SELECT alert_revision_id,content_digest,delivery_state FROM alert_revisions"
                " WHERE episode_id=? AND revision_no=?",
                (episode_id, revision_no),
            ).fetchone()
            if prior is not None:
                if prior[1] != content:
                    raise NewsEpisodeStoreError("morning hold content conflict")
                if prior[2] != "HELD_MORNING":
                    return prior[0], ReplayResult.DUPLICATE
                self.connection.execute(
                    "INSERT INTO morning_hold_queue VALUES(?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET latest_held_revision_id=excluded.latest_held_revision_id,release_not_before=excluded.release_not_before,updated_at=excluded.updated_at",
                    (
                        episode_id,
                        prior[0],
                        release_not_before.astimezone(UTC).isoformat(),
                        now.isoformat(),
                    ),
                )
                return prior[0], ReplayResult.DUPLICATE
            self.connection.execute(
                "INSERT INTO alert_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    revision_id,
                    episode_id,
                    revision_no,
                    "MATERIAL_UPDATE",
                    content,
                    "0" * 64,
                    50,
                    "SCALE_CHANGE",
                    now.isoformat(),
                    0,
                    body,
                    "[]",
                    callback,
                    "HELD_MORNING",
                    None,
                    None,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self.connection.execute(
                "INSERT INTO morning_hold_queue VALUES(?,?,?,?) ON CONFLICT(episode_id) DO UPDATE SET latest_held_revision_id=excluded.latest_held_revision_id,release_not_before=excluded.release_not_before,updated_at=excluded.updated_at",
                (
                    episode_id,
                    revision_id,
                    release_not_before.astimezone(UTC).isoformat(),
                    now.isoformat(),
                ),
            )
        return revision_id, ReplayResult.CREATED

    @staticmethod
    def _held_observed_utc(raw):
        """Aware UTC parse of a held revision's material observed time; any
        degenerate value fails closed to the pre-cutoff (SHADOW) side."""
        try:
            observed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
        except (TypeError, ValueError):
            return None
        if observed.tzinfo is None or observed.utcoffset() is None:
            return None
        return observed.astimezone(UTC)

    def _release_digest_group(self, group, *, now_kst):
        """One release cycle, one group, at most ONE released revision.

        A singleton keeps its held bytes untouched; multiple episodes fold
        into a single bounded combined digest attached to the most recent
        episode, with member provenance in ``links_json``. Overflow beyond
        the digest bounds is never re-eligible (B1 2026-08-22): the contract
        is one morning digest, not one per tick, so overflow holds are
        dequeued and SUPPRESSED with the selected members — held bytes stay
        durable in ``alert_revisions`` and the omission is auditable via
        ``links_json`` entries marked ``DIGEST_OVERFLOW``. Everything
        commits in one transaction so a retry after the queue rows are gone
        finds nothing to release (no duplicate combined digest)."""
        body, used = compose_morning_digest([member["body"] for member in group])
        members = group[:used]
        overflow = group[used:]
        primary = members[0]
        episode_id = primary["episode_id"]
        revision_no = (
            self.connection.execute(
                "SELECT max(revision_no) FROM alert_revisions WHERE episode_id=?",
                (episode_id,),
            ).fetchone()[0]
            + 1
        )
        content = hashlib.sha256(body.encode()).hexdigest()
        rid = hashlib.sha256(
            f"{episode_id}\0{revision_no}\0{content}".encode()
        ).hexdigest()
        # Coalesce to the latest material state (G3 §2.3): the released
        # revision inherits the members' held material observed times (the
        # newest of them), so a pre-cutoff overnight backlog can never
        # re-date itself into Telegram eligibility at release time.
        observed = members[0]["observed_raw"]  # newest: input is observed-DESC
        links = json.dumps(
            [
                {
                    "episode_id": member["episode_id"],
                    "revision_no": member["revision_no"],
                    "alert_revision_id": member["latest_id"],
                }
                for member in members
            ]
            + [
                {
                    "episode_id": member["episode_id"],
                    "revision_no": member["revision_no"],
                    "alert_revision_id": member["latest_id"],
                    "omitted": "DIGEST_OVERFLOW",
                }
                for member in overflow
            ],
            separators=(",", ":"),
        )
        with self.connection:
            for member in group:
                self.connection.execute(
                    "UPDATE alert_revisions SET delivery_state='SUPPRESSED',updated_at=? WHERE episode_id=? AND delivery_state='HELD_MORNING'",
                    (now_kst.isoformat(), member["episode_id"]),
                )
                self.connection.execute(
                    "DELETE FROM morning_hold_queue WHERE episode_id=?",
                    (member["episode_id"],),
                )
            self.connection.execute(
                "INSERT INTO alert_revisions VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    episode_id,
                    revision_no,
                    "MORNING_COALESCED",
                    content,
                    "0" * 64,
                    50,
                    "SCALE_CHANGE",
                    observed,
                    0,
                    body,
                    links,
                    rid[:32],
                    "PENDING_OUTBOX",
                    None,
                    None,
                    now_kst.isoformat(),
                    now_kst.isoformat(),
                ),
            )
        return {
            "alert_revision_id": rid,
            "episode_id": episode_id,
            "revision_kind": "MORNING_COALESCED",
            "episode_ids": tuple(member["episode_id"] for member in members),
            "omitted_episode_ids": tuple(member["episode_id"] for member in overflow),
        }

    def release_morning_holds(self, *, now_kst, telegram_cutoff=None):
        now_utc = now_kst.astimezone(UTC).isoformat()
        rows = self.connection.execute(
            "SELECT q.episode_id,q.latest_held_revision_id,r.revision_no,r.body_text,r.material_change_observed_at FROM morning_hold_queue q JOIN alert_revisions r ON r.alert_revision_id=q.latest_held_revision_id WHERE q.release_not_before<=? ORDER BY r.material_change_observed_at DESC,q.episode_id",
            (now_utc,),
        ).fetchall()
        eligible = []
        for episode_id, latest_id, revision_no, body, observed_raw in rows:
            # Morning-alert repair (2026-08-22): a digest item must carry a
            # meaningful event statement from stored evidence. Generic
            # counts-only/verdict boilerplate and internal JSON fail closed
            # here — held history is SUPPRESSED, never deleted — and released
            # bodies carry no integration prefix.
            if not morning_digest_eligible(body):
                with self.connection:
                    self.connection.execute(
                        "UPDATE alert_revisions SET delivery_state='SUPPRESSED',updated_at=? WHERE episode_id=? AND delivery_state='HELD_MORNING'",
                        (now_kst.isoformat(), episode_id),
                    )
                    self.connection.execute(
                        "DELETE FROM morning_hold_queue WHERE episode_id=?",
                        (episode_id,),
                    )
                continue
            eligible.append(
                {
                    "episode_id": episode_id,
                    "latest_id": latest_id,
                    "revision_no": revision_no,
                    "body": body,
                    "observed_raw": observed_raw,
                    "observed": self._held_observed_utc(observed_raw),
                }
            )
        if not eligible:
            return ()
        # M4: at most ONE Telegram-visible digest per release cycle. With a
        # delivery cutoff, pre-cutoff backlog coalesces separately so its
        # combined digest keeps a pre-cutoff observed time (stays SHADOW,
        # §2.3) — the post-cutoff group is the single Telegram-visible one.
        if telegram_cutoff is None:
            groups = [eligible]
        else:
            fresh = [
                member
                for member in eligible
                if member["observed"] is not None
                and member["observed"] >= telegram_cutoff
            ]
            backlog = [member for member in eligible if member not in fresh]
            groups = [group for group in (fresh, backlog) if group]
        return tuple(
            self._release_digest_group(group, now_kst=now_kst) for group in groups
        )

    def revision_by_callback(self, callback_ref):
        row = self.connection.execute(
            "SELECT alert_revision_id,episode_id,content_digest,portfolio_context_digest,provider_message_id FROM alert_revisions WHERE callback_ref=?",
            (callback_ref,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(
                zip(
                    (
                        "alert_revision_id",
                        "episode_id",
                        "content_digest",
                        "portfolio_context_digest",
                        "provider_message_id",
                    ),
                    row,
                )
            )
        )

    def append_reaction(self, event, *, now):
        prior = self.connection.execute(
            "SELECT reaction_event_id FROM alert_reactions WHERE provider_update_id=?",
            (event["provider_update_id"],),
        ).fetchone()
        if prior:
            return ReplayResult.DUPLICATE
        latest = self.connection.execute(
            "SELECT reaction_event_id FROM alert_reactions WHERE alert_revision_id=? ORDER BY reacted_at DESC,reaction_event_id DESC LIMIT 1",
            (event["alert_revision_id"],),
        ).fetchone()
        rid = (
            "nre-"
            + hashlib.sha256(
                f"{event['provider_update_id']}\0{event['alert_revision_id']}".encode()
            ).hexdigest()[:24]
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO alert_reactions VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    rid,
                    event["alert_revision_id"],
                    event["episode_id"],
                    event["reaction"],
                    event["provider_update_id"],
                    event["provider_message_id"],
                    event["content_digest"],
                    event["portfolio_context_digest"],
                    None if latest is None else latest[0],
                    now.isoformat(),
                ),
            )
        return ReplayResult.CREATED

    def propose_meta_feedback(self, context_key, reaction_ids, *, now):
        if len(reaction_ids) < META_MIN_NEGATIVE:
            return "INSUFFICIENT"
        placeholders = ",".join("?" for _ in reaction_ids)
        cutoff = (now - timedelta(days=META_WINDOW_DAYS)).isoformat()
        eligible = self.connection.execute(
            f"SELECT count(*) FROM alert_reactions WHERE reaction_event_id IN ({placeholders}) AND reaction='NOT_HELPFUL' AND reacted_at>=?",
            (*reaction_ids, cutoff),
        ).fetchone()[0]
        if eligible < META_MIN_NEGATIVE:
            return "INSUFFICIENT"
        row = self.connection.execute(
            "SELECT cooldown_until FROM meta_feedback_proposals WHERE context_key=? ORDER BY created_at DESC LIMIT 1",
            (context_key,),
        ).fetchone()
        if row and datetime.fromisoformat(row[0]) > now:
            return "COOLDOWN"
        pid = (
            "nmp-"
            + hashlib.sha256(f"{context_key}\0{now.isoformat()}".encode()).hexdigest()[
                :24
            ]
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO meta_feedback_proposals VALUES(?,?,?,?,?,?)",
                (
                    pid,
                    context_key,
                    json.dumps(reaction_ids, separators=(",", ":")),
                    "PROPOSED",
                    (now + timedelta(days=META_COOLDOWN_DAYS)).isoformat(),
                    now.isoformat(),
                ),
            )
        return "CREATED"

    def plan_classifier_call(self, call, *, now):
        prior = self.connection.execute(
            "SELECT * FROM classifier_calls WHERE call_id=?", (call["call_id"],)
        ).fetchone()
        if prior:
            return ReplayResult.DUPLICATE
        cols = (
            "call_id",
            "classification_identity",
            "attempt_no",
            "purpose",
            "candidate_id",
            "episode_id",
            "provider",
            "model_id",
            "model_contract_digest",
            "prompt_policy_digest",
            "input_evidence_digest",
            "output_schema_id",
        )
        with self.connection:
            self.connection.execute(
                f"INSERT INTO classifier_calls({','.join(cols)},state,requested_at) VALUES({','.join('?' for _ in cols)},'PLANNED',?)",
                tuple(call.get(c) for c in cols) + (now.isoformat(),),
            )
        return ReplayResult.CREATED

    def start_classifier_call(self, call_id, *, now):
        with self.connection:
            self.connection.execute(
                "UPDATE classifier_calls SET state='STARTED',started_at=? WHERE call_id=? AND state='PLANNED'",
                (now.isoformat(), call_id),
            )

    def settle_classifier_call(self, call_id, exit, output_json, *, now):
        deferred = {
            "RATE_LIMITED",
            "QUOTA_EXCEEDED",
            "MODEL_UNAVAILABLE",
            "MODEL_REJECTED",
        }
        state = "DEFERRED" if exit in deferred else "SETTLED"
        digest = (
            hashlib.sha256(output_json.encode()).hexdigest() if output_json else None
        )
        # Returns the applied rowcount: 0 means the STARTED row was lost to
        # a crash reconcile and nothing durable was written (lost race).
        with self.connection:
            return self.connection.execute(
                "UPDATE classifier_calls SET state=?,exit=?,output_json=?,output_digest=?,settled_at=? WHERE call_id=? AND state='STARTED'",
                (
                    state,
                    exit,
                    output_json if exit == "OK" else None,
                    digest if exit == "OK" else None,
                    now.isoformat(),
                    call_id,
                ),
            ).rowcount

    def reconcile_classifier_calls(self, *, now):
        # A STARTED row whose identity holds an unexpired async worker lease
        # is a live model call, not a crash: only lease-less or expired rows
        # reconcile to UNKNOWN.
        with self.connection:
            return self.connection.execute(
                "UPDATE classifier_calls SET state='UNKNOWN' WHERE state='STARTED' AND classification_identity NOT IN (SELECT classification_identity FROM classifier_work_queue WHERE state='LEASED' AND lease_expires_at>?)",
                (now.isoformat(),),
            ).rowcount

    def find_settled_classification(self, identity):
        row = self.connection.execute(
            "SELECT call_id,provider,model_id,output_json FROM classifier_calls WHERE classification_identity=? AND state='SETTLED' AND exit='OK' ORDER BY attempt_no LIMIT 1",
            (identity,),
        ).fetchone()
        return (
            None
            if row is None
            else dict(zip(("call_id", "provider", "model_id", "output_json"), row))
        )

    def next_classifier_attempt(self, identity):
        return self.connection.execute(
            "SELECT COALESCE(max(attempt_no),0)+1 FROM classifier_calls WHERE classification_identity=?",
            (identity,),
        ).fetchone()[0]

    def classifier_calls_started_on(self, day):
        return self.connection.execute(
            "SELECT count(*) FROM classifier_calls WHERE requested_at>=? AND requested_at<?",
            (day.isoformat(), (day + timedelta(days=1)).isoformat()),
        ).fetchone()[0]

    def record_outage(self, model_id, failure_class, now, model_contract_digest=None):
        row = self.connection.execute(
            "SELECT outage_id,first_observed_at FROM classifier_outages WHERE model_id=? AND failure_class=? AND status='OPEN'",
            (model_id, failure_class),
        ).fetchone()
        if row:
            return ClassifierOutage(
                row[0], model_id, failure_class, datetime.fromisoformat(row[1])
            )
        outage_id = (
            "nout-"
            + hashlib.sha256(
                f"{model_id}\0{failure_class}\0{now.isoformat()}".encode()
            ).hexdigest()[:24]
        )
        with self.connection:
            self.connection.execute(
                "INSERT INTO classifier_outages(outage_id,model_id,failure_class,model_contract_digest,first_observed_at,status) VALUES(?,?,?,?,?,'OPEN')",
                (
                    outage_id,
                    model_id,
                    failure_class,
                    model_contract_digest,
                    now.isoformat(),
                ),
            )
        return ClassifierOutage(outage_id, model_id, failure_class, now)

    open_classifier_outage = record_outage

    def open_outages(self):
        return tuple(
            ClassifierOutage(r[0], r[1], r[2], datetime.fromisoformat(r[3]))
            for r in self.connection.execute(
                "SELECT outage_id,model_id,failure_class,first_observed_at FROM classifier_outages WHERE status='OPEN' ORDER BY outage_id"
            )
        )

    def clear_open_outages(self, cleared_by_call_id, *, now):
        if (
            self.connection.execute(
                "SELECT 1 FROM classifier_calls WHERE call_id=? AND state='SETTLED' AND exit='OK'",
                (cleared_by_call_id,),
            ).fetchone()
            is None
        ):
            raise NewsEpisodeStoreError("clearing classifier call is not SETTLED-OK")
        with self.connection:
            self.connection.execute(
                "UPDATE classifier_outages SET status='CLEARED',cleared_at=?,cleared_by_call_id=? WHERE status='OPEN' AND failure_class<>'CONFIG_INVALID'",
                (now.isoformat(), cleared_by_call_id),
            )

    def clear_config_invalid_outages(self, *, now):
        with self.connection:
            return self.connection.execute(
                "UPDATE classifier_outages SET status='CLEARED',cleared_at=? WHERE status='OPEN' AND failure_class='CONFIG_INVALID'",
                (now.isoformat(),),
            ).rowcount

    def list_unalerted_open_outages(self):
        return tuple(
            self.connection.execute(
                "SELECT outage_id,model_id,failure_class,first_observed_at FROM classifier_outages WHERE status='OPEN' AND ops_alert_id IS NULL"
            )
        )

    def bind_outage_alert(self, outage_id, ops_alert_id, *, now):
        with self.connection:
            self.connection.execute(
                "UPDATE classifier_outages SET ops_alert_id=? WHERE outage_id=? AND ops_alert_id IS NULL",
                (ops_alert_id, outage_id),
            )
