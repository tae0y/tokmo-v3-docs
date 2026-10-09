"""Isolated evidence store for the G2.5 bounded live canary.

Physically separate from every canonical store (LatestQuoteStore,
TradeLedger, OrderScanState, CalendarLookahead, etc.) so the canary
can never mutate canonical material. Only sanitized per-tick receipts
are persisted: tick_index, scheduled/observed UTC, market, symbol,
price, currency, source, quote timestamps, provider-start flags,
duration_ms, lag_seconds, outcome, reason, cumulative_provider_starts.

A write-ahead provider_start_intent table (B4) durably records every
outbound provider call *before* it is made, so a crash between intent
and settle still consumes the start. The purpose ledger is bound to the
exact (purpose_identity) key — base+market+symbol+approved campaign —
and is cumulative across sessions/restarts. It cannot be re-armed by
a new session or root.

Forbidden in evidence: raw token, account scope/seq, native ref,
request URL, file path, response body, secret, exception text.
"""

import os
import sqlite3
import stat
from datetime import datetime
from pathlib import Path

from tokmo.values.ops_kernel import iso, parse_iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS canary_evidence(
  tick_index INTEGER NOT NULL CHECK(tick_index >= 0),
  scheduled_utc TEXT NOT NULL,
  observed_utc TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  price TEXT,
  currency TEXT,
  source TEXT,
  quote_timestamp TEXT,
  quote_fetched_at TEXT,
  token_started INTEGER NOT NULL DEFAULT 0,
  price_batch_started INTEGER NOT NULL DEFAULT 0,
  provider_start_count INTEGER NOT NULL DEFAULT 0,
  cumulative_provider_starts INTEGER NOT NULL DEFAULT 0,
  duration_ms INTEGER NOT NULL,
  lag_seconds REAL NOT NULL,
  outcome TEXT NOT NULL,
  reason TEXT,
  session_id TEXT NOT NULL,
  PRIMARY KEY(tick_index, session_id)
) STRICT;

CREATE TABLE IF NOT EXISTS canary_session(
  session_id TEXT NOT NULL,
  market TEXT NOT NULL,
  symbol TEXT NOT NULL,
  max_ticks INTEGER NOT NULL,
  max_provider_starts INTEGER NOT NULL,
  t0_utc TEXT NOT NULL,
  completed_ticks INTEGER NOT NULL DEFAULT 0,
  total_provider_starts INTEGER NOT NULL DEFAULT 0,
  stop_reason TEXT,
  canonical_fingerprint_pre TEXT,
  canonical_fingerprint_post TEXT,
  created_at TEXT NOT NULL,
  finished_at TEXT,
  PRIMARY KEY(session_id)
) STRICT;

CREATE TABLE IF NOT EXISTS provider_start_intent(
  intent_id INTEGER PRIMARY KEY AUTOINCREMENT,
  purpose_identity TEXT NOT NULL,
  operation TEXT NOT NULL CHECK(operation IN ('TOKEN','PRICE_BATCH')),
  intended_at TEXT NOT NULL,
  settled INTEGER NOT NULL DEFAULT 0,
  session_id TEXT NOT NULL,
  tick_index INTEGER NOT NULL,
  outcome TEXT
) STRICT;

CREATE INDEX IF NOT EXISTS idx_intent_purpose
  ON provider_start_intent(purpose_identity);
"""


class CanaryEvidenceStoreError(ValueError):
    pass


def _iso_utc(dt: datetime) -> str:
    if not isinstance(dt, datetime) or dt.tzinfo is None or dt.utcoffset() is None:
        raise CanaryEvidenceStoreError("datetime must be aware")
    return iso(dt)


class CanaryEvidenceStore:
    """Durable isolated evidence store for the G2.5 canary."""

    def __init__(self, path: Path, *, clock):
        self.path = Path(path)
        self._clock = clock

    def _connect(self) -> sqlite3.Connection:
        # Use lstat-safe mkdir: resolve and refuse symlink parents.
        parent = self.path.parent
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self._verify_private_dir(parent)
        conn = sqlite3.connect(self.path, timeout=1.0)
        try:
            if self.path.exists():
                st = os.lstat(self.path)
                if not stat.S_ISREG(st.st_mode) or st.st_nlink != 1:
                    raise CanaryEvidenceStoreError("unsafe evidence store")
                os.chmod(self.path, 0o600)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=1000")
            conn.execute("PRAGMA journal_mode=DELETE")
            conn.execute("PRAGMA synchronous=FULL")
            conn.executescript(_SCHEMA)
            return conn
        except BaseException:
            conn.close()
            raise

    @staticmethod
    def _verify_private_dir(path: Path) -> None:
        """Verify a directory is a real private directory (no symlink, 0700,
        regular+nlink1 checks)."""
        real = Path(os.path.realpath(path))
        st = os.lstat(path)
        if not stat.S_ISDIR(st.st_mode):
            raise CanaryEvidenceStoreError("root must be a directory")
        if os.path.islink(path):
            raise CanaryEvidenceStoreError("root must not be a symlink")
        # Verify mode is 0700 (owner only)
        current_mode = stat.S_IMODE(os.stat(path).st_mode)
        if current_mode != 0o700:
            os.chmod(path, 0o700)

    # ── Session ──────────────────────────────────────────────────

    def start_session(
        self,
        *,
        session_id: str,
        market: str,
        symbol: str,
        max_ticks: int,
        max_provider_starts: int,
        t0_utc: datetime,
        canonical_fingerprint_pre: "str | None" = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO canary_session("
                "session_id, market, symbol, max_ticks, "
                "max_provider_starts, t0_utc, completed_ticks, "
                "total_provider_starts, stop_reason, "
                "canonical_fingerprint_pre, canonical_fingerprint_post, "
                "created_at, finished_at) "
                "VALUES(?,?,?,?,?,?,0,0,NULL,?,NULL,?,NULL)",
                (
                    session_id,
                    market,
                    symbol,
                    max_ticks,
                    max_provider_starts,
                    _iso_utc(t0_utc),
                    canonical_fingerprint_pre,
                    _iso_utc(self._clock()),
                ),
            )

    def read_session(self, session_id: str) -> "dict | None":
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM canary_session WHERE session_id=?",
                (session_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def read_latest_session(self) -> "dict | None":
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM canary_session ORDER BY created_at DESC LIMIT 1",
            ).fetchone()
        return None if row is None else dict(row)

    def finish_session(
        self,
        session_id: str,
        *,
        completed_ticks: int,
        total_provider_starts: int,
        stop_reason: str,
        canonical_fingerprint_post: "str | None" = None,
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE canary_session SET completed_ticks=?, "
                "total_provider_starts=?, stop_reason=?, "
                "canonical_fingerprint_post=?, finished_at=? "
                "WHERE session_id=?",
                (
                    completed_ticks,
                    total_provider_starts,
                    stop_reason,
                    canonical_fingerprint_post,
                    _iso_utc(self._clock()),
                    session_id,
                ),
            )

    # ── Ticks ────────────────────────────────────────────────────

    def has_tick(self, session_id: str, tick_index: int) -> bool:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT 1 FROM canary_evidence WHERE session_id=? AND tick_index=?",
                (session_id, tick_index),
            ).fetchone()
        return row is not None

    def completed_tick_indices(self, session_id: str) -> tuple:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT tick_index FROM canary_evidence "
                "WHERE session_id=? ORDER BY tick_index",
                (session_id,),
            ).fetchall()
        return tuple(row["tick_index"] for row in rows)

    def record_tick(
        self,
        *,
        session_id: str,
        tick_index: int,
        scheduled_utc: datetime,
        observed_utc: datetime,
        market: str,
        symbol: str,
        price: "str | None",
        currency: "str | None",
        source: "str | None",
        quote_timestamp: "datetime | None",
        quote_fetched_at: "datetime | None",
        token_started: int,
        price_batch_started: int,
        provider_start_count: int,
        cumulative_provider_starts: int,
        duration_ms: int,
        lag_seconds: float,
        outcome: str,
        reason: "str | None",
    ) -> None:
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO canary_evidence("
                "tick_index, scheduled_utc, observed_utc, market, symbol, "
                "price, currency, source, quote_timestamp, "
                "quote_fetched_at, token_started, price_batch_started, "
                "provider_start_count, cumulative_provider_starts, "
                "duration_ms, lag_seconds, outcome, reason, session_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tick_index,
                    _iso_utc(scheduled_utc),
                    _iso_utc(observed_utc),
                    market,
                    symbol,
                    price,
                    currency,
                    source,
                    _iso_utc(quote_timestamp) if quote_timestamp else None,
                    _iso_utc(quote_fetched_at) if quote_fetched_at else None,
                    token_started,
                    price_batch_started,
                    provider_start_count,
                    cumulative_provider_starts,
                    duration_ms,
                    lag_seconds,
                    outcome,
                    reason,
                    session_id,
                ),
            )

    def read_ticks(self, session_id: "str | None" = None) -> tuple:
        with self._connect() as conn:
            if session_id is not None:
                rows = conn.execute(
                    "SELECT * FROM canary_evidence WHERE session_id=? "
                    "ORDER BY tick_index",
                    (session_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM canary_evidence ORDER BY tick_index",
                ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            # Parse datetime fields back to datetime objects
            for key in (
                "scheduled_utc",
                "observed_utc",
                "quote_timestamp",
                "quote_fetched_at",
            ):
                if d.get(key) is not None:
                    d[key] = parse_iso(d[key])
            result.append(d)
        return tuple(result)

    # ── Provider-start intent ledger (B4) ────────────────────────

    def record_provider_start_intent(
        self, *, purpose_identity: str, operation: str, session_id: str, tick_index: int
    ) -> int:
        """Write-ahead durable intent before any outbound provider call.

        Returns the intent_id. The purpose ledger is cumulative: it counts
        every intent for this purpose_identity, settled or not.
        """
        with self._connect() as conn:
            cur = conn.execute(
                "INSERT INTO provider_start_intent("
                "purpose_identity, operation, intended_at, settled, "
                "session_id, tick_index, outcome) "
                "VALUES(?,?,?,0,?,?,NULL)",
                (
                    purpose_identity,
                    operation,
                    _iso_utc(self._clock()),
                    session_id,
                    tick_index,
                ),
            )
            return cur.lastrowid

    def settle_provider_start_intent(self, intent_id: int, *, outcome: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE provider_start_intent SET settled=1, outcome=? "
                "WHERE intent_id=?",
                (outcome, intent_id),
            )

    def cumulative_provider_starts_by_purpose(self, purpose_identity: str) -> int:
        """Count ALL intents for this purpose, settled or not.

        This is the authoritative cumulative count (B4): a crash between
        intent and settle still consumes the start because the intent row
        was already durably committed.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COUNT(*) FROM provider_start_intent WHERE purpose_identity=?",
                (purpose_identity,),
            ).fetchone()
        return int(row[0])

    def cumulative_provider_starts(self, session_id: str) -> int:
        """Legacy: sum of provider_start_count from evidence rows.

        Kept for backward-compat but the purpose ledger count is
        authoritative for cap enforcement.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT COALESCE(SUM(provider_start_count), 0) "
                "FROM canary_evidence WHERE session_id=?",
                (session_id,),
            ).fetchone()
        return int(row[0])
