"""Durable trade-event dialogue state (Plan §20/§22).

Pulse-only writer. Three concerns in one private 0600 store: the trade-event
state machine (DETECTED→…→ANSWERED/SUPPRESSED/EXPIRED), the outbox with a
UNIQUE dedupe key and no-auto-resend UNKNOWN semantics, and the inbound
reply ledger whose offset cursor advances atomically with the reply insert
so a crash never double-consumes or skips a Telegram update. Message text
stored here is already masked (no scope, native ref, token, or path).
"""

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import sqlite3
import stat

from tokmo.values.ops_kernel import iso, parse_iso
from tokmo.values.trade_event import (
    PROMPT_KINDS,
    TradeEventState,
    TradeEventTimesV1,
    TradeEventTransitionError,
    validate_transition,
)

OUTBOX_STATUS_TOKENS = frozenset({"PENDING", "SENT", "UNKNOWN", "FAILED"})

_SCHEMA = """
CREATE TABLE IF NOT EXISTS trade_events(
  trade_event_id TEXT PRIMARY KEY CHECK(length(trade_event_id) = 64),
  source TEXT NOT NULL,
  account_scope TEXT NOT NULL,
  source_record_key TEXT NOT NULL,
  state TEXT NOT NULL CHECK(state IN
    ('DETECTED','EVALUATED','ENQUEUED','SENT','SUPPRESSED','ANSWERED',
     'EXPIRED')),
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  quantity TEXT NOT NULL,
  unit_price TEXT NOT NULL,
  amount TEXT NOT NULL,
  currency TEXT NOT NULL,
  classified_market TEXT NOT NULL CHECK(classified_market IN ('KR','US')),
  filled_at TEXT NOT NULL,
  provider_observed_at TEXT,
  discovered_at TEXT NOT NULL,
  stored_at TEXT NOT NULL,
  notified_at TEXT,
  late_arrival INTEGER NOT NULL CHECK(late_arrival IN (0,1)),
  evaluation_policy_version TEXT,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(source, account_scope, source_record_key)
) STRICT;
CREATE TABLE IF NOT EXISTS dialogue_outbox(
  dedupe_key TEXT PRIMARY KEY CHECK(length(dedupe_key) = 64),
  trade_event_id TEXT NOT NULL,
  prompt_kind TEXT NOT NULL,
  prompt_version TEXT NOT NULL,
  status TEXT NOT NULL CHECK(status IN ('PENDING','SENT','UNKNOWN','FAILED')),
  message_text TEXT NOT NULL,
  telegram_message_id INTEGER,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS inbound_replies(
  provider_update_id INTEGER PRIMARY KEY,
  message_id INTEGER NOT NULL,
  reply_to_message_id INTEGER,
  raw_text BLOB NOT NULL,
  received_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS inbound_cursor(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  next_offset INTEGER NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS reason_appends(
  append_id TEXT PRIMARY KEY CHECK(length(append_id) = 64),
  trade_event_id TEXT NOT NULL,
  inbound_message_id INTEGER NOT NULL,
  appended_at TEXT NOT NULL,
  UNIQUE(trade_event_id, inbound_message_id)
) STRICT;
CREATE TABLE IF NOT EXISTS pending_appends(
  trade_event_id TEXT PRIMARY KEY,
  provider_update_id INTEGER NOT NULL,
  inbound_message_id INTEGER NOT NULL,
  preview_dedupe_key TEXT NOT NULL,
  created_at TEXT NOT NULL,
  payload BLOB,
  save_intent INTEGER,
  attempts INTEGER
) STRICT;
CREATE TABLE IF NOT EXISTS question_recommendations(
  trade_event_id TEXT PRIMARY KEY CHECK(length(trade_event_id) = 64),
  recommendation BLOB NOT NULL,
  created_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS conditional_alerts(
  conditional_alert_id TEXT PRIMARY KEY
    CHECK(length(conditional_alert_id) = 64),
  conditional_order_id TEXT NOT NULL,
  leg_index INTEGER NOT NULL CHECK(leg_index IN (0,1)),
  triggered_order_id TEXT,
  account_scope TEXT NOT NULL,
  symbol TEXT NOT NULL,
  market_class TEXT NOT NULL CHECK(market_class IN ('KR','US')),
  order_type TEXT NOT NULL,
  quantity_ordered TEXT NOT NULL,
  prev_status TEXT NOT NULL,
  new_status TEXT NOT NULL,
  detected_at TEXT NOT NULL,
  migrated_event_id TEXT,
  UNIQUE(conditional_order_id, leg_index, new_status)
) STRICT;
CREATE TABLE IF NOT EXISTS conditional_scan_state(
  id INTEGER PRIMARY KEY CHECK(id = 1),
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
) STRICT;
"""


class DialogueStoreError(ValueError):
    pass


class ExecutionDialogueStore:
    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                value = os.lstat(self.path)
                if not stat.S_ISREG(value.st_mode) or value.st_nlink != 1:
                    raise DialogueStoreError("unsafe dialogue store")
                os.chmod(self.path, 0o600)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA busy_timeout=1000")
            connection.execute("PRAGMA journal_mode=DELETE")
            connection.execute("PRAGMA synchronous=FULL")
            connection.executescript(_SCHEMA)
            # Stores created before the button UI lack the pending payload
            # column, and stores created before the receipt UI lack the
            # save-intent/attempt columns; STRICT tables accept a nullable
            # ADD COLUMN in place. A pre-existing row keeps NULL, which the
            # readers treat as "legacy preview handshake, zero attempts" —
            # so an old in-flight bubble is never auto-settled by the sweep.
            columns = {row[1] for row in connection.execute(
                "PRAGMA table_info(pending_appends)")}
            for name, declaration in (("payload", "BLOB"),
                                      ("save_intent", "INTEGER"),
                                      ("attempts", "INTEGER")):
                if name not in columns:
                    connection.execute(
                        f"ALTER TABLE pending_appends ADD COLUMN {name} "
                        f"{declaration}")
            return connection
        except sqlite3.DatabaseError:
            connection.close()
            raise DialogueStoreError("corrupt dialogue store") from None
        except BaseException:
            connection.close()
            raise

    # --- trade events ----------------------------------------------------

    def register_event(self, event_id: str, *, source: str,
                       account_scope: str, source_record_key: str,
                       symbol: str, side: str, quantity: str,
                       unit_price: str, amount: str, currency: str,
                       classified_market: str,
                       times: TradeEventTimesV1) -> str:
        """Insert DETECTED once; the ledger-identical replay is a duplicate."""
        if type(event_id) is not str or len(event_id) != 64:
            raise DialogueStoreError("invalid trade event id")
        for value in (source, account_scope, source_record_key, symbol, side,
                      quantity, unit_price, amount, currency,
                      classified_market):
            if type(value) is not str or not value:
                raise DialogueStoreError("invalid event identity")
        if not isinstance(times, TradeEventTimesV1):
            raise DialogueStoreError("invalid event times")
        now = iso(self._clock())
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT trade_event_id FROM trade_events WHERE "
                "trade_event_id=? OR (source=? AND account_scope=? AND "
                "source_record_key=?)",
                (event_id, source, account_scope, source_record_key),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO trade_events VALUES(?,?,?,?, 'DETECTED', "
                "?,?,?,?,?,?,?, ?,?,?,?,?,?,NULL,?,?)",
                (event_id, source, account_scope, source_record_key,
                 symbol, side, quantity, unit_price, amount, currency,
                 classified_market,
                 iso(times.filled_at),
                 None if times.provider_observed_at is None
                 else iso(times.provider_observed_at),
                 iso(times.discovered_at), iso(times.stored_at),
                 None if times.notified_at is None else iso(times.notified_at),
                 int(times.late_arrival), now, now),
            )
            return "created"

    def transition_event(self, event_id: str, target: TradeEventState, *,
                         evaluation_policy_version: "str | None" = None,
                         notified_at: "datetime | None" = None) -> None:
        if not isinstance(target, TradeEventState):
            raise DialogueStoreError("invalid target state")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM trade_events WHERE trade_event_id=?",
                (event_id,),
            ).fetchone()
            if row is None:
                raise DialogueStoreError("unknown trade event")
            validate_transition(TradeEventState(row["state"]), target)
            connection.execute(
                "UPDATE trade_events SET state=?, "
                "evaluation_policy_version=COALESCE(?, evaluation_policy_version), "
                "notified_at=COALESCE(?, notified_at), updated_at=? "
                "WHERE trade_event_id=?",
                (target.value, evaluation_policy_version,
                 None if notified_at is None else iso(notified_at),
                 iso(self._clock()), event_id),
            )

    def read_event(self, event_id: str) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM trade_events WHERE trade_event_id=?",
                (event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "trade_event_id": row["trade_event_id"],
            "source": row["source"],
            "account_scope": row["account_scope"],
            "source_record_key": row["source_record_key"],
            "symbol": row["symbol"],
            "side": row["side"],
            "quantity": row["quantity"],
            "unit_price": row["unit_price"],
            "amount": row["amount"],
            "currency": row["currency"],
            "classified_market": row["classified_market"],
            "state": TradeEventState(row["state"]),
            "filled_at": parse_iso(row["filled_at"]),
            "provider_observed_at": (
                None if row["provider_observed_at"] is None
                else parse_iso(row["provider_observed_at"])),
            "discovered_at": parse_iso(row["discovered_at"]),
            "stored_at": parse_iso(row["stored_at"]),
            "notified_at": (None if row["notified_at"] is None
                            else parse_iso(row["notified_at"])),
            "late_arrival": bool(row["late_arrival"]),
            "evaluation_policy_version": row["evaluation_policy_version"],
        }

    def list_events(self) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT trade_event_id FROM trade_events ORDER BY created_at,"
                "trade_event_id",
            ).fetchall()
        return tuple(row["trade_event_id"] for row in rows)

    def find_event_by_source_record_key(
            self, source_record_key: str) -> "str | None":
        """Reverse-arrival pre-check (conditional SDS §3.1(d)): the migrated
        regular event already exists for this provider order id."""
        with self._connect() as connection:
            row = connection.execute(
                "SELECT trade_event_id FROM trade_events "
                "WHERE source_record_key=?",
                (source_record_key,),
            ).fetchone()
        return None if row is None else row["trade_event_id"]

    # --- outbox ----------------------------------------------------------

    def enqueue_outbox(self, dedupe_key: str, *, trade_event_id: str,
                       prompt_kind: str, prompt_version: str,
                       message_text: str) -> str:
        if type(dedupe_key) is not str or len(dedupe_key) != 64:
            raise DialogueStoreError("invalid dedupe key")
        if prompt_kind not in PROMPT_KINDS:
            raise DialogueStoreError("unknown prompt kind")
        if type(message_text) is not str or not message_text:
            raise DialogueStoreError("invalid message text")
        now = iso(self._clock())
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT dedupe_key FROM dialogue_outbox WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO dialogue_outbox VALUES(?,?,?,?, 'PENDING', "
                "?,NULL,?,?)",
                (dedupe_key, trade_event_id, prompt_kind, prompt_version,
                 message_text, now, now),
            )
            return "created"

    def mark_outbox(self, dedupe_key: str, status: str, *,
                    telegram_message_id: "int | None" = None) -> None:
        """PENDING→SENT/UNKNOWN/FAILED only. UNKNOWN never auto-resends."""
        if status not in OUTBOX_STATUS_TOKENS - {"PENDING"}:
            raise DialogueStoreError("invalid outbox status")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT status FROM dialogue_outbox WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
            if row is None:
                raise DialogueStoreError("unknown outbox row")
            if row["status"] != "PENDING":
                raise TradeEventTransitionError(
                    "outbox rows settle exactly once (no auto-resend)")
            connection.execute(
                "UPDATE dialogue_outbox SET status=?, telegram_message_id=?, "
                "updated_at=? WHERE dedupe_key=?",
                (status, telegram_message_id, iso(self._clock()), dedupe_key),
            )

    def read_outbox(self, dedupe_key: str) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM dialogue_outbox WHERE dedupe_key=?",
                (dedupe_key,),
            ).fetchone()
        if row is None:
            return None
        return {
            "dedupe_key": row["dedupe_key"],
            "trade_event_id": row["trade_event_id"],
            "prompt_kind": row["prompt_kind"],
            "prompt_version": row["prompt_version"],
            "status": row["status"],
            "message_text": row["message_text"],
            "telegram_message_id": row["telegram_message_id"],
        }

    def list_outbox(self, *, status: "str | None" = None,
                    send_eligible_after=None) -> tuple[dict, ...]:
        query = "SELECT dedupe_key FROM dialogue_outbox"
        params = []
        if status is not None:
            query += " WHERE status=?"
            params.append(status)
        if send_eligible_after is not None:
            if (not isinstance(send_eligible_after, datetime)
                    or send_eligible_after.tzinfo is None
                    or send_eligible_after.utcoffset() is None):
                return ()
            cutoff = send_eligible_after.astimezone(timezone.utc)
            query += (" AND " if params else " WHERE ") + "created_at >= ?"
            params.append(iso(cutoff))
        query += " ORDER BY created_at, dedupe_key"
        with self._connect() as connection:
            rows = connection.execute(query, tuple(params)).fetchall()
        return tuple(self.read_outbox(row["dedupe_key"]) for row in rows)

    def find_outbox_by_telegram_message(self, telegram_message_id: int) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT dedupe_key FROM dialogue_outbox WHERE "
                "telegram_message_id=?",
                (telegram_message_id,),
            ).fetchone()
        return None if row is None else self.read_outbox(row["dedupe_key"])

    # --- inbound ---------------------------------------------------------

    def record_inbound(self, *, provider_update_id: int, message_id: int,
                       reply_to_message_id: "int | None", raw_text: bytes,
                       next_offset: int) -> str:
        """Reply insert and offset advance in one transaction (§22.3)."""
        for value in (provider_update_id, message_id, next_offset):
            if type(value) is not int:
                raise DialogueStoreError("invalid inbound identity")
        if reply_to_message_id is not None and type(reply_to_message_id) is not int:
            raise DialogueStoreError("invalid reply binding")
        if type(raw_text) is not bytes:
            raise DialogueStoreError("inbound raw text must be bytes")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                existing = connection.execute(
                    "SELECT provider_update_id FROM inbound_replies WHERE "
                    "provider_update_id=?",
                    (provider_update_id,),
                ).fetchone()
                outcome = "duplicate"
                if existing is None:
                    connection.execute(
                        "INSERT INTO inbound_replies VALUES(?,?,?,?,?)",
                        (provider_update_id, message_id, reply_to_message_id,
                         raw_text, iso(self._clock())),
                    )
                    outcome = "created"
                connection.execute(
                    "INSERT INTO inbound_cursor(id, next_offset) VALUES(1, ?) "
                    "ON CONFLICT(id) DO UPDATE SET next_offset="
                    "MAX(next_offset, excluded.next_offset)",
                    (next_offset,),
                )
                connection.execute("COMMIT")
                return outcome
            except BaseException:
                connection.execute("ROLLBACK")
                raise

    def read_inbound_cursor(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT next_offset FROM inbound_cursor WHERE id=1",
            ).fetchone()
        return 0 if row is None else row["next_offset"]

    def advance_inbound_cursor(self, next_offset: int) -> None:
        """Advance past a rejected provider update without storing its body."""
        if type(next_offset) is not int or next_offset < 0:
            raise DialogueStoreError("invalid inbound offset")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO inbound_cursor(id, next_offset) VALUES(1, ?) "
                "ON CONFLICT(id) DO UPDATE SET next_offset="
                "MAX(next_offset, excluded.next_offset)",
                (next_offset,),
            )

    def read_inbound(self, provider_update_id: int) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM inbound_replies WHERE provider_update_id=?",
                (provider_update_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "provider_update_id": row["provider_update_id"],
            "message_id": row["message_id"],
            "reply_to_message_id": row["reply_to_message_id"],
            "raw_text": row["raw_text"],
        }

    def list_news_inbound(self):
        with self._connect() as connection:
            rows=connection.execute("SELECT provider_update_id,message_id,raw_text FROM inbound_replies WHERE CAST(raw_text AS TEXT) LIKE 'n1:%' ORDER BY provider_update_id").fetchall()
        return tuple({"update_id":row["provider_update_id"],"message_id":row["message_id"],
            "data":bytes(row["raw_text"]).decode("utf-8"),"callback":True} for row in rows)

    # --- pending append (preview handshake §22.2) ------------------------

    def create_pending_append(self, *, trade_event_id: str,
                              provider_update_id: int,
                              inbound_message_id: int,
                              preview_dedupe_key: str,
                              payload: "bytes | None" = None,
                              save_intent: bool = False) -> None:
        """Durable record of bytes the user has committed to (or is about to).

        ``save_intent`` marks the 2026-08-21 contract: the user has already
        submitted these bytes and the only thing left is the append, so the
        bounded sweep may retry them. It stays false for the legacy preview
        handshake, whose rows must wait for an explicit 확인.
        """
        if payload is not None and type(payload) is not bytes:
            raise DialogueStoreError("pending payload must be bytes or None")
        if type(save_intent) is not bool:
            raise DialogueStoreError("save intent must be a bool")
        with self._connect() as connection:
            connection.execute(
                "INSERT OR REPLACE INTO pending_appends "
                "VALUES(?,?,?,?,?,?,?,0)",
                (trade_event_id, provider_update_id, inbound_message_id,
                 preview_dedupe_key, iso(self._clock()), payload,
                 int(save_intent)),
            )

    @staticmethod
    def _pending_row(row) -> dict:
        return {
            "trade_event_id": row["trade_event_id"],
            "provider_update_id": row["provider_update_id"],
            "inbound_message_id": row["inbound_message_id"],
            "preview_dedupe_key": row["preview_dedupe_key"],
            "payload": row["payload"],
            "save_intent": bool(row["save_intent"]),
            "attempts": row["attempts"] or 0,
        }

    def read_pending_append(self, trade_event_id: str) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM pending_appends WHERE trade_event_id=?",
                (trade_event_id,),
            ).fetchone()
        return None if row is None else self._pending_row(row)

    def list_pending_appends(self) -> tuple[dict, ...]:
        """Every unsettled row, oldest first (the bounded sweep's input)."""
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM pending_appends "
                "ORDER BY created_at, trade_event_id",
            ).fetchall()
        return tuple(self._pending_row(row) for row in rows)

    def bump_pending_append_attempt(self, trade_event_id: str) -> int:
        """Count one settled-with-failure attempt; returns the new count."""
        with self._connect() as connection:
            connection.execute(
                "UPDATE pending_appends SET attempts=COALESCE(attempts,0)+1 "
                "WHERE trade_event_id=?",
                (trade_event_id,),
            )
            row = connection.execute(
                "SELECT attempts FROM pending_appends WHERE trade_event_id=?",
                (trade_event_id,),
            ).fetchone()
        return 0 if row is None else (row["attempts"] or 0)

    def find_pending_append_by_preview(
            self, preview_dedupe_key: str) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT trade_event_id FROM pending_appends WHERE "
                "preview_dedupe_key=?",
                (preview_dedupe_key,),
            ).fetchone()
        return None if row is None else self.read_pending_append(
            row["trade_event_id"])

    def delete_pending_append(self, trade_event_id: str) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM pending_appends WHERE trade_event_id=?",
                (trade_event_id,),
            )

    # --- conditional alerts (conditional SDS v0.4 §4.1/§4.5) --------------

    def record_conditional_alert(self, conditional_alert_id: str, *,
                                 conditional_order_id: str, leg_index: int,
                                 triggered_order_id: "str | None",
                                 account_scope: str, symbol: str,
                                 market_class: str, order_type: str,
                                 quantity_ordered: str, prev_status: str,
                                 new_status: str) -> str:
        """One row per leg trigger; the restart replay is a no-op duplicate."""
        if type(conditional_alert_id) is not str \
                or len(conditional_alert_id) != 64:
            raise DialogueStoreError("invalid conditional alert id")
        if type(leg_index) is not int or leg_index not in (0, 1):
            raise DialogueStoreError("invalid conditional leg index")
        if triggered_order_id is not None \
                and (type(triggered_order_id) is not str
                     or not triggered_order_id):
            raise DialogueStoreError("invalid triggered order id")
        for value in (conditional_order_id, account_scope, symbol,
                      market_class, order_type, quantity_ordered,
                      prev_status, new_status):
            if type(value) is not str or not value:
                raise DialogueStoreError("invalid conditional alert identity")
        if market_class not in ("KR", "US"):
            raise DialogueStoreError("invalid conditional market class")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT conditional_alert_id FROM conditional_alerts WHERE "
                "conditional_alert_id=? OR (conditional_order_id=? AND "
                "leg_index=? AND new_status=?)",
                (conditional_alert_id, conditional_order_id, leg_index,
                 new_status),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO conditional_alerts VALUES(?,?,?,?,?,?,?,?,?,?,"
                "?,?,NULL)",
                (conditional_alert_id, conditional_order_id, leg_index,
                 triggered_order_id, account_scope, symbol, market_class,
                 order_type, quantity_ordered, prev_status, new_status,
                 iso(self._clock())),
            )
            return "created"

    @staticmethod
    def _alert_row(row) -> dict:
        return {
            "conditional_alert_id": row["conditional_alert_id"],
            "conditional_order_id": row["conditional_order_id"],
            "leg_index": row["leg_index"],
            "triggered_order_id": row["triggered_order_id"],
            "account_scope": row["account_scope"],
            "symbol": row["symbol"],
            "market_class": row["market_class"],
            "order_type": row["order_type"],
            "quantity_ordered": row["quantity_ordered"],
            "prev_status": row["prev_status"],
            "new_status": row["new_status"],
            "detected_at": parse_iso(row["detected_at"]),
            "migrated_event_id": row["migrated_event_id"],
        }

    def read_conditional_alert(
            self, conditional_alert_id: str) -> "dict | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conditional_alerts WHERE "
                "conditional_alert_id=?",
                (conditional_alert_id,),
            ).fetchone()
        return None if row is None else self._alert_row(row)

    def list_conditional_alerts(self) -> tuple[dict, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conditional_alerts "
                "ORDER BY detected_at, conditional_alert_id",
            ).fetchall()
        return tuple(self._alert_row(row) for row in rows)

    def find_alert_by_triggered_order(
            self, triggered_order_id: str) -> "dict | None":
        """Merge primary key (§4.3(1)); a NULL id never matches anything."""
        if type(triggered_order_id) is not str or not triggered_order_id:
            return None
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM conditional_alerts WHERE "
                "migrated_event_id IS NULL AND triggered_order_id=? "
                "ORDER BY detected_at, conditional_alert_id",
                (triggered_order_id,),
            ).fetchone()
        return None if row is None else self._alert_row(row)

    def find_unmigrated_alerts_in_window(
            self, *, account_scope: str, symbol: str, center: datetime,
            window_hours: int = 25) -> tuple[dict, ...]:
        """Merge fallback key (§4.3(2)): scope + symbol + ±window candidates."""
        if (not isinstance(center, datetime) or center.tzinfo is None
                or center.utcoffset() is None):
            raise DialogueStoreError("fallback window center must be aware")
        low = iso((center - timedelta(hours=window_hours))
                  .astimezone(timezone.utc))
        high = iso((center + timedelta(hours=window_hours))
                   .astimezone(timezone.utc))
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM conditional_alerts WHERE "
                "migrated_event_id IS NULL AND account_scope=? AND symbol=? "
                "AND detected_at BETWEEN ? AND ? "
                "ORDER BY detected_at, conditional_alert_id",
                (account_scope, symbol, low, high),
            ).fetchall()
        return tuple(self._alert_row(row) for row in rows)

    def backfill_migrated_event(self, conditional_alert_id: str,
                                trade_event_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT migrated_event_id FROM conditional_alerts WHERE "
                "conditional_alert_id=?",
                (conditional_alert_id,),
            ).fetchone()
            if row is None:
                raise DialogueStoreError("unknown conditional alert")
            if row["migrated_event_id"] is not None:
                raise TradeEventTransitionError(
                    "conditional alerts merge exactly once")
            connection.execute(
                "UPDATE conditional_alerts SET migrated_event_id=? "
                "WHERE conditional_alert_id=?",
                (trade_event_id, conditional_alert_id),
            )
            # Preserve any user-entered conditional-fill dialogue. Once the
            # regular order becomes canonical, the same preview bubble can
            # settle against the real event instead of the synthetic alert id.
            connection.execute(
                "UPDATE pending_appends SET trade_event_id=? "
                "WHERE trade_event_id=?",
                (trade_event_id, conditional_alert_id),
            )
            connection.execute(
                "UPDATE dialogue_outbox SET trade_event_id=?, updated_at=? "
                "WHERE trade_event_id=?",
                (trade_event_id, iso(self._clock()), conditional_alert_id),
            )
            # Carry a precomputed conditional recommendation across the same
            # identity migration. A regular-event recommendation already
            # present remains first-write-wins.
            connection.execute(
                "INSERT OR IGNORE INTO question_recommendations "
                "(trade_event_id, recommendation, created_at) "
                "SELECT ?, recommendation, created_at FROM "
                "question_recommendations WHERE trade_event_id=?",
                (trade_event_id, conditional_alert_id),
            )
            connection.execute(
                "DELETE FROM question_recommendations WHERE trade_event_id=?",
                (conditional_alert_id,),
            )

    def read_conditional_scan_state(self) -> dict:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state_json FROM conditional_scan_state WHERE id=1",
            ).fetchone()
        if row is None:
            return {"observations": {}, "cursors": {}}
        state = json.loads(row["state_json"])
        state.setdefault("observations", {})
        state.setdefault("cursors", {})
        return state

    def write_conditional_scan_state(self, state: dict) -> None:
        if type(state) is not dict:
            raise DialogueStoreError("invalid conditional scan state")
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO conditional_scan_state(id, state_json, "
                "updated_at) VALUES(1, ?, ?) ON CONFLICT(id) DO UPDATE SET "
                "state_json=excluded.state_json, "
                "updated_at=excluded.updated_at",
                (json.dumps(state, sort_keys=True), iso(self._clock())),
            )

    # --- question recommendations (button UI 예 adoption source) ----------

    def record_question_recommendation(self, trade_event_id: str,
                                       recommendation: bytes) -> str:
        """First write wins; the identical tick replay is a duplicate."""
        if type(trade_event_id) is not str or len(trade_event_id) != 64:
            raise DialogueStoreError("invalid trade event id")
        if type(recommendation) is not bytes or not recommendation:
            raise DialogueStoreError("recommendation must be non-empty bytes")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT trade_event_id FROM question_recommendations "
                "WHERE trade_event_id=?",
                (trade_event_id,),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO question_recommendations VALUES(?,?,?)",
                (trade_event_id, recommendation, iso(self._clock())),
            )
            return "created"

    def read_question_recommendation(
            self, trade_event_id: str) -> "bytes | None":
        with self._connect() as connection:
            row = connection.execute(
                "SELECT recommendation FROM question_recommendations "
                "WHERE trade_event_id=?",
                (trade_event_id,),
            ).fetchone()
        return None if row is None else row["recommendation"]

    # --- append gate -----------------------------------------------------

    def record_reason_append(self, append_id: str, *, trade_event_id: str,
                             inbound_message_id: int) -> str:
        if type(append_id) is not str or len(append_id) != 64:
            raise DialogueStoreError("invalid append id")
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT append_id FROM reason_appends WHERE append_id=? OR "
                "(trade_event_id=? AND inbound_message_id=?)",
                (append_id, trade_event_id, inbound_message_id),
            ).fetchone()
            if existing is not None:
                return "duplicate"
            connection.execute(
                "INSERT INTO reason_appends VALUES(?,?,?,?)",
                (append_id, trade_event_id, inbound_message_id,
                 iso(self._clock())),
            )
            return "created"

    def read_latest_reason_append(self,
                                  trade_event_id: str) -> "dict | None":
        """The most recent settled append for one event.

        A later submission for the same event is a correction of this one:
        the renderer links to it instead of rewriting the earlier entry. The
        rowid tiebreak keeps the order stable when two appends share the same
        recorded instant.
        """
        with self._connect() as connection:
            row = connection.execute(
                "SELECT append_id, trade_event_id, inbound_message_id, "
                "appended_at FROM reason_appends WHERE trade_event_id=? "
                "ORDER BY appended_at DESC, rowid DESC LIMIT 1",
                (trade_event_id,),
            ).fetchone()
        if row is None:
            return None
        return {
            "append_id": row["append_id"],
            "trade_event_id": row["trade_event_id"],
            "inbound_message_id": row["inbound_message_id"],
            "appended_at": parse_iso(row["appended_at"]),
        }
