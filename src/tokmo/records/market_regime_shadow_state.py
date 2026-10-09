"""Private identity-scoped state for the market-regime shadow lane."""

import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tokmo.values.ops_kernel import iso

_SCHEMA = """
CREATE TABLE IF NOT EXISTS market_regime_shadow_claim(
 identity_digest TEXT PRIMARY KEY, program_id TEXT NOT NULL, spec_digest TEXT NOT NULL,
 policy_version TEXT NOT NULL, market TEXT NOT NULL CHECK(market IN ('KR','US')),
 symbol TEXT NOT NULL, completed_session_date TEXT NOT NULL,
 adjustment_mode TEXT NOT NULL, request_identity TEXT NOT NULL,
 state TEXT NOT NULL CHECK(state IN ('CLAIMED','SUCCEEDED','FAILED')),
 owner_invocation_id TEXT NOT NULL, owner_started_at TEXT NOT NULL,
 owner_lease_key TEXT NOT NULL, claimed_at TEXT NOT NULL, finished_at TEXT,
 failure_code TEXT
) STRICT;
CREATE TABLE IF NOT EXISTS market_regime_shadow_result(
 identity_digest TEXT PRIMARY KEY REFERENCES market_regime_shadow_claim(identity_digest),
 input_digest TEXT NOT NULL, pack_digest TEXT NOT NULL, fetched_at TEXT NOT NULL,
 evaluated_at TEXT NOT NULL, completed_session_date TEXT NOT NULL,
 normalized_bar_count INTEGER NOT NULL CHECK(normalized_bar_count BETWEEN 1 AND 200),
 result_payload TEXT NOT NULL CHECK(length(CAST(result_payload AS BLOB))<=65536)
) STRICT;
PRAGMA user_version=1;
"""


@dataclass(frozen=True)
class ShadowClaimV1:
    state: str
    identity_digest: str
    reused_input_digest: str | None = None


class IdentityLeaseV1:
    """Process-held proof that no live owner holds this identity."""

    def __init__(self, descriptor: int, key: str):
        self._descriptor = descriptor
        self.key = key

    def close(self):
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


class MarketRegimeShadowStateStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(self.path.parent, 0o700)
        connection = sqlite3.connect(self.path)
        try:
            connection.executescript(_SCHEMA)
        finally:
            connection.close()
        os.chmod(self.path, 0o600)

    def acquire_identity_lease(self, identity_digest) -> IdentityLeaseV1 | None:
        if (
            type(identity_digest) is not str
            or len(identity_digest) != 64
            or any(char not in "0123456789abcdef" for char in identity_digest)
        ):
            raise ValueError("invalid identity digest")
        lease_dir = self.path.with_name(self.path.name + ".leases")
        lease_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lease_dir, 0o700)
        lease_path = lease_dir / f"{identity_digest}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        descriptor = os.open(lease_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return IdentityLeaseV1(descriptor, f"shadow:{identity_digest}")

    def probe(self, identity_digest) -> ShadowClaimV1 | None:
        """Read committed terminal/live-owner state without creating a row."""
        connection = sqlite3.connect(self.path)
        try:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT c.state,r.input_digest FROM market_regime_shadow_claim c "
                "LEFT JOIN market_regime_shadow_result r USING(identity_digest) "
                "WHERE c.identity_digest=?",
                (identity_digest,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        if row["state"] == "SUCCEEDED":
            return ShadowClaimV1("reused", identity_digest, row["input_digest"])
        if row["state"] == "FAILED":
            return ShadowClaimV1("failed", identity_digest)
        # A non-blocking lock probe distinguishes a live foreign owner from a
        # released lease. It creates no claim and retains no lease.
        lease = self.acquire_identity_lease(identity_digest)
        if lease is None:
            return ShadowClaimV1("in_progress", identity_digest)
        lease.close()
        return None

    def claim(
        self,
        *,
        identity_digest,
        program_id,
        spec_digest,
        policy_version,
        market,
        symbol,
        completed_session_date,
        adjustment_mode,
        request_identity,
        owner_invocation_id,
        owner_started_at,
        owner_lease_key,
        identity_lease,
    ) -> ShadowClaimV1:
        if (
            not isinstance(identity_lease, IdentityLeaseV1)
            or identity_lease.key != owner_lease_key
            or identity_lease._descriptor is None
        ):
            raise ValueError("live identity lease required")
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT c.state,r.input_digest FROM market_regime_shadow_claim c "
                "LEFT JOIN market_regime_shadow_result r USING(identity_digest) "
                "WHERE c.identity_digest=?",
                (identity_digest,),
            ).fetchone()
            if row is not None and row["state"] == "CLAIMED":
                owner = connection.execute(
                    "SELECT owner_invocation_id FROM market_regime_shadow_claim "
                    "WHERE identity_digest=?",
                    (identity_digest,),
                ).fetchone()[0]
                if owner != owner_invocation_id:
                    connection.execute(
                        "UPDATE market_regime_shadow_claim SET state='FAILED',"
                        "failure_code='RECOVERY_REQUIRED',finished_at=? "
                        "WHERE identity_digest=? AND state='CLAIMED'",
                        (iso(owner_started_at), identity_digest),
                    )
                    connection.execute("COMMIT")
                    return ShadowClaimV1("recovery_required", identity_digest)
            if row is not None:
                connection.execute("COMMIT")
                return ShadowClaimV1(
                    "reused"
                    if row["state"] == "SUCCEEDED"
                    else "failed"
                    if row["state"] == "FAILED"
                    else "in_progress",
                    identity_digest,
                    row["input_digest"],
                )
            connection.execute(
                "INSERT INTO market_regime_shadow_claim VALUES(?,?,?,?,?,?,?,?,?,"
                "'CLAIMED',?,?,?,?,NULL,NULL)",
                (
                    identity_digest,
                    program_id,
                    spec_digest,
                    policy_version,
                    market.value,
                    symbol,
                    completed_session_date.isoformat(),
                    adjustment_mode.value,
                    request_identity,
                    owner_invocation_id,
                    iso(owner_started_at),
                    owner_lease_key,
                    iso(owner_started_at),
                ),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()
        return ShadowClaimV1("owner", identity_digest)

    def succeed(
        self,
        identity_digest,
        *,
        input_digest,
        pack_digest,
        fetched_at,
        evaluated_at,
        completed_session_date,
        normalized_bar_count,
        result_payload,
    ):
        encoded = json.dumps(result_payload, sort_keys=True, separators=(",", ":"))
        if len(encoded.encode()) > 65536:
            raise ValueError("result payload cap")
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO market_regime_shadow_result VALUES(?,?,?,?,?,?,?,?)",
                (
                    identity_digest,
                    input_digest,
                    pack_digest,
                    iso(fetched_at),
                    iso(evaluated_at),
                    completed_session_date.isoformat(),
                    normalized_bar_count,
                    encoded,
                ),
            )
            changed = connection.execute(
                "UPDATE market_regime_shadow_claim SET state='SUCCEEDED',finished_at=? "
                "WHERE identity_digest=? AND state='CLAIMED'",
                (iso(evaluated_at), identity_digest),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                raise ValueError("claim not owned")
            connection.execute("COMMIT")
        finally:
            connection.close()

    def fail(self, identity_digest, *, failure_code, finished_at):
        connection = sqlite3.connect(self.path, isolation_level=None)
        try:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE market_regime_shadow_claim SET state='FAILED',failure_code=?,finished_at=? "
                "WHERE identity_digest=? AND state='CLAIMED'",
                (failure_code, iso(finished_at), identity_digest),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                raise ValueError("claim not owned")
            connection.execute("COMMIT")
        except Exception:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
