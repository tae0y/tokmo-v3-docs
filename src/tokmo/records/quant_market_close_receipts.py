"""Private schema-2 receipts for the KR/US market-close product.

This is schema 2 of the existing Quant receipt authority.  Its
constructor can rehearse the exact legacy shape, preserving a byte backup and
all legacy identities/results; production activation remains a separate gate.
"""

import json
import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tokmo.values.quant_recurring import QuantDueIdentityV1

_TERMINAL = frozenset({"SUCCEEDED", "NO_PACK", "FAILED", "UNKNOWN"})
_PENDING = frozenset({"PENDING_LEASE", "PENDING_CANDLES"})
_CREATE_DUE = """CREATE TABLE quant_market_close_due(
 identity_digest TEXT PRIMARY KEY CHECK(length(identity_digest)=64),
 identity_schema TEXT NOT NULL CHECK(identity_schema IN ('market-close-schema-2','legacy-v1')),
 market TEXT NOT NULL CHECK(market IN ('KR','US','US_NYSE_NASDAQ')),
 completed_session_date TEXT NOT NULL,
 universe_revision TEXT NOT NULL,
 quant_policy_version TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN
  ('PENDING_LEASE','PENDING_CANDLES','RUNNING','SUCCEEDED','NO_PACK','FAILED','UNKNOWN')),
 invocation_id TEXT NOT NULL DEFAULT '',
 result_json TEXT NOT NULL DEFAULT '',
 outbox_id TEXT NOT NULL DEFAULT '',
 outbox_state TEXT NOT NULL DEFAULT 'NONE'
  CHECK(outbox_state IN ('NONE','PENDING_OUTBOX','OUTBOX_BOUND'))
) STRICT"""


@dataclass(frozen=True, slots=True)
class QuantMarketCloseReceiptSchema2:
    identity_digest: str
    identity_schema: str
    market: str
    completed_session_date: str
    universe_revision: str
    quant_policy_version: str
    state: str
    invocation_id: str
    result_json: str
    outbox_id: str
    outbox_state: str


class QuantMarketCloseReceiptStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        self.migration_report = (0, 0)
        self._changes = 0
        if self.path.exists():
            with sqlite3.connect(self.path) as connection:
                version = connection.execute("PRAGMA user_version").fetchone()[0]
            if version == 1:
                self._migrate_v1()
            elif version != 2:
                raise ValueError("schema_mismatch")
        else:
            with self._connect() as connection:
                connection.execute(_CREATE_DUE)
                connection.execute("PRAGMA user_version=2")
        os.chmod(self.path, 0o600)
        self._validate()

    def _connect(self):
        connection = sqlite3.connect(self.path)
        connection.row_factory = sqlite3.Row
        return connection

    def _validate(self) -> None:
        with self._connect() as connection:
            if connection.execute("PRAGMA user_version").fetchone()[0] != 2:
                raise ValueError("schema_mismatch")
            row = connection.execute(
                "SELECT strict FROM pragma_table_list WHERE name='quant_market_close_due'"
            ).fetchone()
            if row is None or row[0] != 1:
                raise ValueError("schema_mismatch")

    def _migrate_v1(self) -> None:
        backup = self.path.with_suffix(self.path.suffix + ".v1.bak")
        if backup.exists():
            raise ValueError("migration_backup_exists")
        shutil.copy2(self.path, backup)
        os.chmod(backup, 0o600)
        with self._connect() as connection:
            required = {
                "identity_digest",
                "program_id",
                "policy_version",
                "market",
                "completed_session_date",
                "universe_policy_version",
                "state",
            }
            columns = {
                row[1]
                for row in connection.execute("PRAGMA table_info(quant_recurring_due)")
            }
            if not required.issubset(columns):
                raise ValueError("legacy_schema_mismatch")
            due_rows = connection.execute(
                "SELECT * FROM quant_recurring_due"
            ).fetchall()
            result_columns = {
                row[1]
                for row in connection.execute(
                    "PRAGMA table_info(quant_recurring_result)"
                )
            }
            result_rows = connection.execute(
                "SELECT * FROM quant_recurring_result"
            ).fetchall()
            connection.execute("BEGIN")
            connection.execute("DROP TABLE quant_recurring_result")
            connection.execute("DROP TABLE quant_recurring_due")
            self._migration_checkpoint("after_drop")
            connection.execute(_CREATE_DUE)
            connection.execute("PRAGMA user_version=2")
            results = {row["identity_digest"]: row for row in result_rows}
            for row in due_rows:
                state = "RUNNING" if row["state"] == "CLAIMED" else row["state"]
                result = results.get(row["identity_digest"])
                result_json = (
                    ""
                    if result is None
                    else json.dumps(
                        {column: result[column] for column in sorted(result_columns)},
                        sort_keys=True,
                        separators=(",", ":"),
                        ensure_ascii=True,
                    )
                )
                connection.execute(
                    "INSERT INTO quant_market_close_due VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        row["identity_digest"],
                        "legacy-v1",
                        row["market"],
                        row["completed_session_date"],
                        row["universe_policy_version"],
                        row["policy_version"],
                        state,
                        row["owner_invocation_id"],
                        result_json,
                        "",
                        "NONE",
                    ),
                )
            connection.commit()
        self.migration_report = (len(due_rows), len(result_rows))

    def _migration_checkpoint(self, point: str) -> None:
        """Test-only crash seam inside the single migration transaction."""
        del point

    @staticmethod
    def _values(identity: QuantDueIdentityV1):
        if not isinstance(identity, QuantDueIdentityV1):
            raise ValueError("identity")
        return (
            identity.identity_digest,
            "market-close-schema-2",
            identity.market.value,
            identity.completed_session_date.isoformat(),
            identity.universe_revision,
            identity.quant_policy_version,
        )

    def total_changes(self) -> int:
        return self._changes

    def ensure_pending(self, identity, *, state: str) -> str:
        if state not in _PENDING:
            raise ValueError("pending_state")
        values = self._values(identity)
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT state FROM quant_market_close_due WHERE identity_digest=?",
                (values[0],),
            ).fetchone()
            if existing is None:
                legacy_market = "US_NYSE_NASDAQ" if values[2] == "US" else values[2]
                existing = connection.execute(
                    "SELECT state FROM quant_market_close_due WHERE identity_schema='legacy-v1' "
                    "AND market=? AND completed_session_date=? AND universe_revision=? "
                    "AND quant_policy_version=? AND state IN ('SUCCEEDED','NO_PACK','FAILED','UNKNOWN')",
                    (legacy_market, values[3], values[4], values[5]),
                ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO quant_market_close_due"
                    "(identity_digest,identity_schema,market,completed_session_date,"
                    "universe_revision,quant_policy_version,state) VALUES(?,?,?,?,?,?,?)",
                    (*values, state),
                )
                connection.commit()
                self._changes += 1
                return state
            return existing[0]

    def set_pending(self, identity, *, state: str) -> None:
        if state not in _PENDING:
            raise ValueError("pending_state")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE quant_market_close_due SET state=? WHERE identity_digest=? "
                "AND state IN ('PENDING_LEASE','PENDING_CANDLES')",
                (state, identity.identity_digest),
            ).rowcount
            connection.commit()
        self._changes += changed

    def claim(self, identity, *, invocation_id: str) -> bool:
        values = self._values(identity)
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE quant_market_close_due SET state='RUNNING',invocation_id=? "
                "WHERE identity_digest=? AND state IN ('PENDING_LEASE','PENDING_CANDLES')",
                (invocation_id, values[0]),
            ).rowcount
            connection.commit()
        self._changes += changed
        return changed == 1

    def finish(
        self, identity, *, state: str, result_json: str, outbox_id: str = ""
    ) -> None:
        if state not in _TERMINAL:
            raise ValueError("terminal_state")
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE quant_market_close_due SET state=?,result_json=?,outbox_id=?,"
                "outbox_state=? WHERE identity_digest=? AND state='RUNNING'",
                (
                    state,
                    result_json,
                    outbox_id,
                    "PENDING_OUTBOX" if outbox_id else "NONE",
                    identity.identity_digest,
                ),
            ).rowcount
            if changed != 1:
                raise ValueError("terminal")
            connection.commit()
            self._changes += 1

    def bind_outbox(self, identity, outbox_id: str) -> bool:
        with self._connect() as connection:
            changed = connection.execute(
                "UPDATE quant_market_close_due SET outbox_state='OUTBOX_BOUND' "
                "WHERE identity_digest=? AND outbox_id=? AND state IN "
                "('SUCCEEDED','NO_PACK') AND outbox_state='PENDING_OUTBOX'",
                (identity.identity_digest, outbox_id),
            ).rowcount
            connection.commit()
        self._changes += changed
        return changed == 1

    def read(self, identity) -> QuantMarketCloseReceiptSchema2 | None:
        return self.read_digest(identity.identity_digest)

    def read_digest(self, digest: str) -> QuantMarketCloseReceiptSchema2 | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM quant_market_close_due WHERE identity_digest=?",
                (digest,),
            ).fetchone()
        return None if row is None else QuantMarketCloseReceiptSchema2(*tuple(row))
