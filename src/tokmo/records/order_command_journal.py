"""Private write-ahead command journal. Stores no provider credentials."""
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from decimal import Decimal
import json
import os
from pathlib import Path
import sqlite3

from tokmo.records.private_sqlite import connect_private, prepare_private_path, private_creation
from tokmo.values.order_commands import OrderCommandResult
from tokmo.values.order_execution import RiskEnvelope, RiskSnapshot, aware


class CommandConflict(ValueError):
    pass


_SCHEMA = """
CREATE TABLE IF NOT EXISTS command_journal (
 request_key TEXT PRIMARY KEY,
 mode TEXT NOT NULL CHECK(mode IN ('paper','live')),
 account_scope TEXT NOT NULL,
 symbol TEXT NOT NULL,
 operation TEXT NOT NULL,
 target_id TEXT,
 client_key TEXT,
 digest TEXT NOT NULL,
 command_json TEXT NOT NULL,
 plan_version TEXT NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('SENDING','ACCEPTED','REJECTED','UNKNOWN','NOT_SENT')),
 code TEXT NOT NULL,
 order_id TEXT,
 lifecycle TEXT NOT NULL CHECK(lifecycle IN ('OPEN','TERMINAL')),
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL,
 UNIQUE(mode, account_scope, client_key)
) STRICT;
CREATE TABLE IF NOT EXISTS command_evidence (
 evidence_id INTEGER PRIMARY KEY,
 request_key TEXT NOT NULL REFERENCES command_journal(request_key),
 observed_at TEXT NOT NULL,
 evidence_json TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS rotation_decisions (
 decision_key TEXT PRIMARY KEY,
 experiment_id TEXT NOT NULL,
 model_id TEXT NOT NULL,
 input_json TEXT NOT NULL,
 output_json TEXT,
 status TEXT NOT NULL,
 created_at TEXT NOT NULL,
 updated_at TEXT NOT NULL
) STRICT;
CREATE TABLE IF NOT EXISTS experiment_books (
 mode TEXT NOT NULL, experiment_id TEXT NOT NULL, config_json TEXT NOT NULL,
 cash TEXT NOT NULL, quantity TEXT NOT NULL, frozen INTEGER NOT NULL,
 PRIMARY KEY(mode,experiment_id)
) STRICT;
CREATE TABLE IF NOT EXISTS experiment_fills (
 mode TEXT NOT NULL, experiment_id TEXT NOT NULL, fill_id TEXT NOT NULL,
 fill_json TEXT NOT NULL, PRIMARY KEY(mode,experiment_id,fill_id),
 FOREIGN KEY(mode,experiment_id) REFERENCES experiment_books(mode,experiment_id)
) STRICT;
CREATE TABLE IF NOT EXISTS protected_fills (
 mode TEXT NOT NULL, experiment_id TEXT NOT NULL, fill_id TEXT NOT NULL,
 fill_json TEXT NOT NULL, recorded_at TEXT NOT NULL,
 PRIMARY KEY(mode,experiment_id,fill_id),
 FOREIGN KEY(mode,experiment_id) REFERENCES experiment_books(mode,experiment_id)
) STRICT;
"""


class OrderCommandJournal:
    def __init__(self, path):
        self.path = Path(path)
        # Refuse symlink ancestors as well as symlink files.
        if any(p.is_symlink() for p in (self.path, *self.path.parents)):
            raise ValueError("unsafe_journal_path")
        if prepare_private_path(self.path).unsafe:
            raise ValueError("unsafe_journal_path")
        with private_creation():
            if not self.path.exists():
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                os.close(fd)
        with self._db() as db:
            db.executescript(_SCHEMA)

    @contextmanager
    def _db(self):
        if prepare_private_path(self.path).unsafe:
            raise ValueError("unsafe_journal_path")
        db = connect_private(self.path, mode="rw")
        db.row_factory = sqlite3.Row
        try:
            yield db
        finally:
            db.close()

    def get(self, request_key):
        with self._db() as db:
            row = db.execute("SELECT * FROM command_journal WHERE request_key=?", (request_key,)).fetchone()
            return None if row is None else dict(row)

    def count(self):
        with self._db() as db:
            return db.execute("SELECT count(*) FROM command_journal").fetchone()[0]

    def open_commands(self, *, mode, account_scope, symbol):
        """Every unfinished row for one instrument, oldest first; read-only."""
        with self._db() as db:
            rows = db.execute("SELECT * FROM command_journal WHERE mode=? AND account_scope=? AND symbol=? AND lifecycle='OPEN' ORDER BY created_at",
                              (mode, account_scope, symbol)).fetchall()
            return [dict(row) for row in rows]

    def experiment_lot_cost(self, experiment_id, *, mode):
        """Per-share fill price of the lot currently held (before costs); None when flat or unknown.

        Fills are replayed in observation order; whenever the position returns
        to zero the lot is closed and the next buy starts a new basis.
        """
        with self._db() as db:
            rows = db.execute("SELECT fill_json FROM experiment_fills WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchall()
        fills = sorted((json.loads(r[0]) for r in rows), key=lambda f: f["observed_at"])
        position, amount = Decimal(0), Decimal(0)
        for fill in fills:
            quantity = Decimal(fill["quantity"])
            if fill["side"] == "BUY":
                position += quantity
                amount += Decimal(fill["amount"])
            elif quantity >= position:
                position, amount = Decimal(0), Decimal(0)
            else:
                # A partial sell leaves the remaining shares at the same average basis.
                amount = amount * (position - quantity) / position
                position -= quantity
        return None if position <= 0 else amount / position

    def experiment_fill(self, experiment_id, *, mode, fill_id):
        with self._db() as db:
            row = db.execute("SELECT fill_json FROM experiment_fills WHERE mode=? AND experiment_id=? AND fill_id=?",
                             (mode, experiment_id, fill_id)).fetchone()
            return None if row is None else json.loads(row[0])

    def experiment_book(self, experiment_id, *, mode):
        with self._db() as db:
            book = db.execute("SELECT cash, quantity, frozen FROM experiment_books WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchone()
            if book is None:
                return None
            return {"cash": Decimal(book["cash"]), "quantity": Decimal(book["quantity"]), "frozen": bool(book["frozen"])}

    def known_order_ids(self, *, mode, account_scope, symbol):
        """Broker order ids this runner has ever sent for one instrument.

        A fill whose id is absent here was placed outside the automation.
        """
        with self._db() as db:
            rows = db.execute("SELECT DISTINCT order_id FROM command_journal WHERE mode=? AND account_scope=? AND symbol=? AND order_id IS NOT NULL",
                              (mode, account_scope, symbol)).fetchall()
            return {row[0] for row in rows}

    def experiment_watermark(self, experiment_id, *, mode, account_scope, symbol):
        """When this experiment first touched the instrument; None before it ever did.

        Account history older than this belongs to the owner alone and is never
        attributed to the experiment's protected rail.
        """
        with self._db() as db:
            fill = db.execute("SELECT min(json_extract(fill_json,'$.observed_at')) FROM experiment_fills WHERE mode=? AND experiment_id=?",
                              (mode, experiment_id)).fetchone()[0]
            command = db.execute("SELECT min(created_at) FROM command_journal WHERE mode=? AND account_scope=? AND symbol=?",
                                 (mode, account_scope, symbol)).fetchone()[0]
        stamps = [s for s in (fill, command) if s]
        return min(stamps) if stamps else None

    def protected_fill(self, experiment_id, *, mode, fill_id):
        with self._db() as db:
            row = db.execute("SELECT fill_json FROM protected_fills WHERE mode=? AND experiment_id=? AND fill_id=?",
                             (mode, experiment_id, fill_id)).fetchone()
            return None if row is None else json.loads(row[0])

    def record_protected_fill(self, experiment_id, *, mode, fill_id, fill, recorded_at):
        """Remember one fill as belonging to the protected position, exactly once.

        This books no experiment cash: the protected lot is the owner's, not the
        experiment's. The row exists so the same broker fill is never counted
        twice against ``protected_quantity``.
        """
        if not fill_id or not aware(recorded_at):
            raise ValueError("protected_fill_identity")
        payload = json.dumps(fill, sort_keys=True, default=str)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT fill_json FROM protected_fills WHERE mode=? AND experiment_id=? AND fill_id=?",
                                 (mode, experiment_id, fill_id)).fetchone()
                if row is not None:
                    if row[0] != payload:
                        raise CommandConflict("protected_fill_payload_changed")
                    db.commit()
                    return "DUPLICATE"
                db.execute("INSERT INTO protected_fills VALUES (?,?,?,?,?)",
                           (mode, experiment_id, fill_id, payload, recorded_at.isoformat()))
                db.commit()
                return "APPLIED"
            except BaseException:
                db.rollback()
                raise

    def release_protected_fill(self, experiment_id, *, mode, fill_id):
        """Forget an attribution so the owner can claim the fill for the experiment.

        Returns the released payload, or None when nothing was attributed. The
        caller moves the rail back; this only drops the claim that held it.
        """
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT fill_json FROM protected_fills WHERE mode=? AND experiment_id=? AND fill_id=?",
                                 (mode, experiment_id, fill_id)).fetchone()
                if row is None:
                    db.commit()
                    return None
                db.execute("DELETE FROM protected_fills WHERE mode=? AND experiment_id=? AND fill_id=?",
                           (mode, experiment_id, fill_id))
                db.commit()
                return json.loads(row[0])
            except BaseException:
                db.rollback()
                raise

    def reconfigure_protected_quantity(self, experiment_id, *, mode, envelope):
        """Move only the protected-quantity rail; every other envelope field must match.

        Widening the rail is how an owner's own trade in the protected lot is
        absorbed without reopening the whole configuration for review.
        """
        if type(envelope) is not RiskEnvelope:
            raise ValueError("experiment_identity")
        config = json.dumps(asdict(envelope), sort_keys=True, default=str)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT config_json FROM experiment_books WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchone()
                if row is None:
                    raise CommandConflict("experiment_book_missing")
                current, revised = json.loads(row[0]), json.loads(config)
                if {k: v for k, v in current.items() if k != "protected_quantity"} \
                        != {k: v for k, v in revised.items() if k != "protected_quantity"}:
                    raise CommandConflict("experiment_reconfiguration_requires_review")
                db.execute("UPDATE experiment_books SET config_json=? WHERE mode=? AND experiment_id=?",
                           (config, mode, experiment_id))
                db.commit()
                return current["protected_quantity"], revised["protected_quantity"]
            except BaseException:
                db.rollback()
                raise

    def owned_target(self, *, mode, account_scope, symbol, order_id):
        with self._db() as db:
            row = db.execute("SELECT * FROM command_journal WHERE mode=? AND account_scope=? AND symbol=? AND order_id=? AND status='ACCEPTED' AND lifecycle='OPEN' ORDER BY created_at DESC LIMIT 1",
                             (mode, account_scope, symbol, order_id)).fetchone()
            superseding = db.execute("SELECT 1 FROM command_journal WHERE mode=? AND account_scope=? AND target_id=? AND status IN ('SENDING','UNKNOWN','ACCEPTED') LIMIT 1",
                                     (mode, account_scope, order_id)).fetchone()
            return None if row is None or superseding is not None else dict(row)

    def initialize_experiment(self, experiment_id, *, mode, envelope):
        if mode not in {"paper", "live"} or type(envelope) is not RiskEnvelope or not experiment_id:
            raise ValueError("experiment_identity")
        numbers = (envelope.initial_budget, envelope.max_quantity, envelope.protected_quantity)
        if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in numbers) \
                or envelope.initial_budget <= 0 or envelope.max_quantity <= 0 \
                or envelope.max_quantity != envelope.max_quantity.to_integral_value():
            raise ValueError("experiment_numbers")
        if not aware(envelope.valid_until) or not envelope.account_scope or not envelope.symbol \
                or envelope.market not in {"KR", "US"} or not envelope.plan_version:
            raise ValueError("experiment_configuration")
        config = json.dumps(asdict(envelope), sort_keys=True, default=str)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT config_json FROM experiment_books WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchone()
                if row is not None and row[0] != config:
                    raise CommandConflict("experiment_reconfiguration_requires_review")
                db.execute("INSERT OR IGNORE INTO experiment_books VALUES (?,?,?,?,?,0)",
                           (mode, experiment_id, config, str(envelope.initial_budget), "0"))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def apply_experiment_fill(self, experiment_id, *, mode, fill_id, symbol, side,
                              quantity, amount, costs, observed_at):
        """Apply a confirmed fill's native-currency cash flow once, never a quote.

        Overfills/overspends are recorded faithfully and freeze the book rather
        than silently discarded. Caller supplies unique broker execution IDs,
        not repeated cumulative order snapshots.
        """
        if side not in {"BUY", "SELL"} or not fill_id or not isinstance(observed_at, datetime) or observed_at.utcoffset() is None:
            raise ValueError("fill_identity")
        if any(not isinstance(v, Decimal) or not v.is_finite() or v < 0 for v in (quantity, amount, costs)) or quantity == 0 or amount == 0:
            raise ValueError("fill_numbers")
        document = json.dumps({"symbol": symbol, "side": side, "quantity": str(quantity),
                               "amount": str(amount), "costs": str(costs), "observed_at": observed_at.isoformat()}, sort_keys=True)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                book = db.execute("SELECT * FROM experiment_books WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchone()
                if book is None:
                    raise ValueError("experiment_missing")
                config = json.loads(book["config_json"])
                if config["symbol"] != symbol:
                    raise ValueError("fill_symbol")
                prior = db.execute("SELECT fill_json FROM experiment_fills WHERE mode=? AND experiment_id=? AND fill_id=?", (mode, experiment_id, fill_id)).fetchone()
                if prior is not None:
                    if prior[0] != document:
                        raise CommandConflict("fill_payload_changed")
                    db.commit()
                    return "DUPLICATE"
                cash = Decimal(book["cash"]) + (-amount if side == "BUY" else amount) - costs
                position = Decimal(book["quantity"]) + (quantity if side == "BUY" else -quantity)
                frozen = bool(book["frozen"]) or cash < 0 or position < 0 or position > Decimal(config["max_quantity"])
                db.execute("INSERT INTO experiment_fills VALUES (?,?,?,?)", (mode, experiment_id, fill_id, document))
                db.execute("UPDATE experiment_books SET cash=?,quantity=?,frozen=? WHERE mode=? AND experiment_id=?",
                           (str(cash), str(position), int(frozen), mode, experiment_id))
                db.commit()
                return "FROZEN" if frozen else "APPLIED"
            except BaseException:
                db.rollback()
                raise

    def experiment_snapshot(self, experiment_id, *, mode, total_quantity, pending_buy,
                            pending_sell, fee_buffer, observed_at, unresolved=False):
        with self._db() as db:
            book = db.execute("SELECT * FROM experiment_books WHERE mode=? AND experiment_id=?", (mode, experiment_id)).fetchone()
            if book is None:
                raise ValueError("experiment_missing")
            config = json.loads(book["config_json"])
            return RiskSnapshot(observed_at, total_quantity, Decimal(book["quantity"]), pending_buy, pending_sell,
                                Decimal(book["cash"]), fee_buffer, unresolved or bool(book["frozen"]),
                                config["account_scope"], config["symbol"], config["market"], Decimal(config["initial_budget"]))

    def begin_decision(self, *, decision_key, experiment_id, model_id, input_json, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM rotation_decisions WHERE decision_key=?", (decision_key,)).fetchone()
                if row is not None:
                    if (row["experiment_id"], row["model_id"], row["input_json"]) != (experiment_id, model_id, input_json):
                        raise CommandConflict("decision_key_reused")
                    db.commit()
                    return False
                db.execute("INSERT INTO rotation_decisions VALUES (?,?,?,?,NULL,'ANALYZING',?,?)",
                           (decision_key, experiment_id, model_id, input_json, now, now))
                db.commit()
                return True
            except BaseException:
                db.rollback()
                raise

    def finish_decision(self, decision_key, *, status, output_json, now):
        with self._db() as db:
            db.execute("UPDATE rotation_decisions SET status=?,output_json=?,updated_at=? WHERE decision_key=? AND status='ANALYZING'",
                       (status, output_json, now, decision_key))

    def decision(self, decision_key):
        with self._db() as db:
            row = db.execute("SELECT * FROM rotation_decisions WHERE decision_key=?", (decision_key,)).fetchone()
            return None if row is None else dict(row)

    def claim(self, *, request_key, mode, account_scope, symbol, operation, target_id,
              client_key, digest, command_json, plan_version, now, snapshot_at=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM command_journal WHERE request_key=?", (request_key,)).fetchone()
                if row is not None:
                    if (row["digest"], row["mode"], row["account_scope"]) != (digest, mode, account_scope):
                        raise CommandConflict("request_key_reused")
                    db.commit()
                    return "EXISTING", dict(row)
                rows = db.execute("SELECT * FROM command_journal WHERE mode=? AND account_scope=? AND symbol=? AND lifecycle='OPEN'",
                                  (mode, account_scope, symbol)).fetchall()
                if snapshot_at is not None:
                    evidence = db.execute("SELECT observed_at FROM command_evidence e JOIN command_journal j ON e.request_key=j.request_key WHERE j.mode=? AND j.account_scope=? AND j.symbol=?",
                                          (mode, account_scope, symbol)).fetchall()
                    if any(datetime.fromisoformat(r[0]) >= datetime.fromisoformat(snapshot_at) for r in evidence):
                        db.commit()
                        return "STALE", None
                creating = operation in {"CREATE", "CONDITIONAL_CREATE"}
                if not creating:
                    owner = next((r for r in rows if r["order_id"] == target_id and r["status"] == "ACCEPTED"), None)
                    superseding = db.execute("SELECT 1 FROM command_journal WHERE mode=? AND account_scope=? AND target_id=? AND status IN ('SENDING','UNKNOWN','ACCEPTED') LIMIT 1", (mode, account_scope, target_id)).fetchone()
                    if owner is None or superseding is not None or owner["operation"].startswith("CONDITIONAL") != operation.startswith("CONDITIONAL"):
                        db.commit()
                        return "TARGET", None
                if any(r["status"] in {"SENDING", "UNKNOWN"} for r in rows) or (
                        creating and any(r["status"] == "ACCEPTED" for r in rows)):
                    db.commit()
                    return "BLOCKED", None
                db.execute("INSERT INTO command_journal VALUES (?,?,?,?,?,?,?,?,?,?,'SENDING','in_flight',NULL,'OPEN',?,?)",
                           (request_key, mode, account_scope, symbol, operation, target_id, client_key,
                            digest, command_json, plan_version, now, now))
                db.commit()
                return "CLAIMED", None
            except sqlite3.IntegrityError as error:
                db.rollback()
                raise CommandConflict("client_key_reused") from error
            except BaseException:
                db.rollback()
                raise

    def settle(self, request_key, result, *, now):
        if type(result) is not OrderCommandResult:
            raise ValueError("result_type")
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                cursor = db.execute("UPDATE command_journal SET status=?,code=?,order_id=?,lifecycle=?,updated_at=? WHERE request_key=? AND status='SENDING'",
                                    (result.status, result.code, result.order_id,
                                     "TERMINAL" if result.status in {"REJECTED", "NOT_SENT"} else "OPEN", now, request_key))
                if cursor.rowcount != 1:
                    raise CommandConflict("settlement_state")
                db.execute("INSERT INTO command_evidence(request_key,observed_at,evidence_json) VALUES (?,?,?)",
                           (request_key, now, json.dumps({"status": result.status, "code": result.code,
                                                        "order_id": result.order_id, "previous_order_id": result.previous_order_id,
                                                        "request_id": result.request_id}, sort_keys=True)))
                db.commit()
            except BaseException:
                db.rollback()
                raise

    def reconcile(self, request_key, *, account_scope, order_id, evidence, now):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            try:
                row = db.execute("SELECT * FROM command_journal WHERE request_key=?", (request_key,)).fetchone()
                if row is None or row["account_scope"] != account_scope or row["order_id"] != order_id:
                    raise ValueError("reconciliation_identity")
                if row["status"] != "ACCEPTED":
                    raise ValueError("reconciliation_state")
                db.execute("INSERT INTO command_evidence(request_key,observed_at,evidence_json) VALUES (?,?,?)",
                           (request_key, now, json.dumps(evidence, sort_keys=True)))
                db.execute("UPDATE command_journal SET lifecycle='TERMINAL',updated_at=? WHERE request_key=?", (now, request_key))
                db.commit()
            except BaseException:
                db.rollback()
                raise
