"""Private STRICT profile receipt store for the KR/US quant market profiles.

Fresh store (`ops/quant_profile_receipts.sqlite3`) with the same
STRICT/private/flock/`user_version=1` discipline as the frozen V1
`QuantRecurringReceiptStore`; the V1 DB is never opened, written, or
migrated by this module. Result rows carry the bounded canonical normalized
membership material bytes plus their sha256 digest; the digest binding is
validated in application code on every write and read (§9).
"""

import fcntl
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (
    MIN_SQLITE_VERSION,
    acquire_immediate_transaction,
    assert_no_sidecars_after_close,
    assert_private_sidecars_while_open,
    connect_private,
    prepare_private_path,
    private_creation,
    table_is_strict,
)
from tokmo.values.cadence_policy import CadencePolicyV1
from tokmo.values.ops_kernel import iso, parse_iso
from tokmo.values.quant_profiles import (
    QUANT_PROFILE_MATERIAL_MAX_BYTES,
    QUANT_PROFILE_REGISTRY,
    QuantProfileDueIdentityV1,
    decode_membership_material,
    validate_material_binding,
)
from tokmo.values.quant_recurring import QuantTerminalStateV1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quant_profile_due(
 identity_digest TEXT PRIMARY KEY
   CHECK(length(identity_digest)=64 AND NOT identity_digest GLOB '*[^0-9a-f]*'),
 profile_id TEXT NOT NULL
   CHECK(profile_id IN ('quant-us-nyse-nasdaq-v1','quant-kr-toss-membership-v1')),
 policy_version TEXT NOT NULL,
 market TEXT NOT NULL CHECK(market IN ('KR','US')),
 completed_session_date TEXT NOT NULL CHECK(length(completed_session_date)=10),
 universe_policy_version TEXT NOT NULL,
 state TEXT NOT NULL
   CHECK(state IN ('CLAIMED','SUCCEEDED','NO_PACK','FAILED','UNKNOWN')),
 owner_invocation_id TEXT NOT NULL,
 owner_lease_key TEXT NOT NULL,
 claimed_at TEXT NOT NULL,
 finished_at TEXT,
 reason_code TEXT NOT NULL DEFAULT '',
 CHECK((state='CLAIMED') = (finished_at IS NULL))
) STRICT;

CREATE TABLE IF NOT EXISTS quant_profile_result(
 identity_digest TEXT PRIMARY KEY
   REFERENCES quant_profile_due(identity_digest),
 acquisition_id TEXT NOT NULL,
 membership_holdings_digest TEXT NOT NULL
   CHECK(length(membership_holdings_digest)=64),
 holdings_captured_at TEXT NOT NULL,
 membership_starts INTEGER NOT NULL CHECK(membership_starts BETWEEN 0 AND 7),
 candle_page1_starts INTEGER NOT NULL CHECK(candle_page1_starts BETWEEN 0 AND 5),
 candle_page2_starts INTEGER NOT NULL CHECK(candle_page2_starts BETWEEN 0 AND 5),
 cohort_size INTEGER NOT NULL CHECK(cohort_size BETWEEN 0 AND 5),
 admissible_count INTEGER NOT NULL CHECK(admissible_count BETWEEN 0 AND 5),
 provenance_digest TEXT NOT NULL CHECK(length(provenance_digest)=64),
 membership_material BLOB NOT NULL
   CHECK(length(membership_material) <= 262144),
 membership_material_digest TEXT NOT NULL
   CHECK(length(membership_material_digest)=64
     AND NOT membership_material_digest GLOB '*[^0-9a-f]*'),
 pack_digest TEXT NOT NULL DEFAULT '',
 degraded INTEGER NOT NULL CHECK(degraded IN (0,1)),
 limitations TEXT NOT NULL
   CHECK(length(CAST(limitations AS BLOB))<=8192),
 symbols TEXT NOT NULL CHECK(length(CAST(symbols AS BLOB))<=1024),
 CHECK(candle_page1_starts + candle_page2_starts <= 10),
 CHECK(membership_starts <= 3 + cohort_size),
 CHECK((pack_digest='') OR length(pack_digest)=64)
) STRICT;

PRAGMA user_version=1;
"""

_TERMINAL_STATES = frozenset(
    {
        QuantTerminalStateV1.SUCCEEDED,
        QuantTerminalStateV1.NO_PACK,
        QuantTerminalStateV1.FAILED,
        QuantTerminalStateV1.UNKNOWN,
    }
)


@dataclass(frozen=True, slots=True)
class QuantProfileDueV1:
    identity_digest: str
    profile_id: str
    policy_version: str
    market: str
    completed_session_date: str
    universe_policy_version: str
    state: QuantTerminalStateV1
    owner_invocation_id: str
    owner_lease_key: str
    claimed_at: datetime
    finished_at: datetime | None
    reason_code: str


@dataclass(frozen=True, slots=True)
class QuantProfileResultV1:
    identity_digest: str
    acquisition_id: str
    membership_holdings_digest: str
    holdings_captured_at: datetime
    membership_starts: int
    candle_page1_starts: int
    candle_page2_starts: int
    cohort_size: int
    admissible_count: int
    provenance_digest: str
    membership_material: bytes
    membership_material_digest: str
    pack_digest: str
    degraded: bool
    limitations: tuple[str, ...]
    symbols: tuple[str, ...]


class QuantProfileIdentityLeaseV1:
    def __init__(self, descriptor: int, key: str):
        self._descriptor = descriptor
        self.key = key

    def close(self) -> None:
        if self._descriptor is not None:
            fcntl.flock(self._descriptor, fcntl.LOCK_UN)
            os.close(self._descriptor)
            self._descriptor = None

    def __enter__(self):
        return self

    def __exit__(self, *unused):
        self.close()


def _valid_digest(value: str) -> bool:
    return (
        type(value) is str
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _validated_material(result: QuantProfileResultV1) -> dict:
    material = result.membership_material
    if (
        type(material) is not bytes
        or not material
        or len(material) > QUANT_PROFILE_MATERIAL_MAX_BYTES
    ):
        raise ValueError("membership_material")
    if sha256(material).hexdigest() != result.membership_material_digest:
        raise ValueError("membership_material_digest")
    return decode_membership_material(material)


def _due_binding_view(row) -> SimpleNamespace:
    return SimpleNamespace(
        identity_digest=row["identity_digest"],
        profile_id=row["profile_id"],
        policy_version=row["policy_version"],
        completed_session_date=row["completed_session_date"],
        universe_policy_version=row["universe_policy_version"],
    )


def _schema_objects(connection: sqlite3.Connection) -> tuple[tuple[str, str, str], ...]:
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT type,name,sql FROM sqlite_schema "
            "WHERE name NOT LIKE 'sqlite_%' ORDER BY type,name"
        )
    )


class QuantProfileReceiptStore:
    def __init__(self, path: Path):
        self.path = Path(path)
        connection, created = self._open(create=True)
        try:
            if created:
                connection.executescript(_SCHEMA)
                os.chmod(self.path, 0o600)
                assert_private_sidecars_while_open(self.path)
            self._validate(connection)
        finally:
            self._close(connection, write=created)

    def _open(self, *, create: bool = False, readonly: bool = False):
        if sqlite3.sqlite_version_info < MIN_SQLITE_VERSION:
            raise ValueError("schema_mismatch")
        safety = prepare_private_path(self.path)
        if safety.unsafe or (self.path.exists() and os.lstat(self.path).st_nlink != 1):
            raise ValueError("private_path")
        created = not os.path.lexists(self.path)
        if created and not create:
            raise ValueError("receipt_store_absent")
        if created:
            with private_creation():
                connection = connect_private(self.path)
        else:
            connection = connect_private(self.path, mode="ro" if readonly else "rwc")
        connection.row_factory = sqlite3.Row
        return connection, created

    def _close(self, connection: sqlite3.Connection, *, write: bool = False) -> None:
        connection.close()
        if write:
            fsync_directory(self.path.parent)
        if assert_no_sidecars_after_close(self.path):
            raise ValueError("sqlite_sidecar_present")

    def _validate(self, connection: sqlite3.Connection) -> None:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 1:
            raise ValueError("schema_mismatch")
        if not all(
            table_is_strict(connection, table)
            for table in ("quant_profile_due", "quant_profile_result")
        ):
            raise ValueError("schema_mismatch")
        reference = sqlite3.connect(":memory:")
        try:
            reference.executescript(_SCHEMA)
            if _schema_objects(connection) != _schema_objects(reference):
                raise ValueError("schema_mismatch")
        finally:
            reference.close()

    def acquire_identity_lease(
        self,
        identity_digest: str,
    ) -> QuantProfileIdentityLeaseV1 | None:
        if not _valid_digest(identity_digest):
            raise ValueError("identity_digest")
        lease_directory = self.path.with_name(self.path.name + ".leases")
        with private_creation():
            lease_directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(lease_directory, 0o700)
        lease_path = lease_directory / f"{identity_digest}.lock"
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        with private_creation():
            descriptor = os.open(lease_path, flags, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(descriptor)
            return None
        return QuantProfileIdentityLeaseV1(
            descriptor, f"quant-profile:{identity_digest}"
        )

    def claim(
        self,
        identity: QuantProfileDueIdentityV1,
        *,
        owner_invocation_id: str,
        owner_lease_key: str,
        claimed_at: datetime,
        identity_lease: QuantProfileIdentityLeaseV1,
    ) -> QuantProfileDueV1:
        if not isinstance(identity, QuantProfileDueIdentityV1):
            raise ValueError("identity")
        if (
            not isinstance(identity_lease, QuantProfileIdentityLeaseV1)
            or identity_lease._descriptor is None
            or identity_lease.key != owner_lease_key
        ):
            raise ValueError("live identity lease required")
        connection, _ = self._open()
        try:
            self._validate(connection)
            acquire_immediate_transaction(connection)
            row = connection.execute(
                "SELECT * FROM quant_profile_due WHERE identity_digest=?",
                (identity.identity_digest,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO quant_profile_due VALUES(?,?,?,?,?,?,'CLAIMED',"
                    "?,?,?,NULL,'')",
                    (
                        identity.identity_digest,
                        identity.profile_id,
                        identity.policy_version,
                        identity.market.value,
                        identity.completed_session_date.isoformat(),
                        identity.universe_policy_version,
                        owner_invocation_id,
                        owner_lease_key,
                        iso(claimed_at),
                    ),
                )
                connection.execute("COMMIT")
            else:
                expected = (
                    identity.profile_id,
                    identity.policy_version,
                    identity.market.value,
                    identity.completed_session_date.isoformat(),
                    identity.universe_policy_version,
                )
                actual = tuple(
                    row[key]
                    for key in (
                        "profile_id",
                        "policy_version",
                        "market",
                        "completed_session_date",
                        "universe_policy_version",
                    )
                )
                if actual != expected:
                    connection.execute("ROLLBACK")
                    raise ValueError("identity_conflict")
                connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._close(connection, write=True)
        result = self.read_due(identity.identity_digest)
        if result is None:
            raise ValueError("claim_missing")
        return result

    def finish(
        self,
        identity_digest: str,
        *,
        state: QuantTerminalStateV1,
        reason_code: str,
        finished_at: datetime,
        result: QuantProfileResultV1 | None = None,
    ) -> None:
        if state not in _TERMINAL_STATES:
            raise ValueError("terminal_state")
        if state in {QuantTerminalStateV1.SUCCEEDED, QuantTerminalStateV1.NO_PACK}:
            if not isinstance(result, QuantProfileResultV1):
                raise ValueError("result")
        elif result is not None:
            raise ValueError("result")
        if result is not None:
            if result.identity_digest != identity_digest:
                raise ValueError("result_identity")
            _validated_material(result)
        connection, _ = self._open()
        try:
            self._validate(connection)
            acquire_immediate_transaction(connection)
            if result is not None:
                due_row = connection.execute(
                    "SELECT identity_digest,profile_id,policy_version,"
                    "completed_session_date,universe_policy_version "
                    "FROM quant_profile_due WHERE identity_digest=?",
                    (identity_digest,),
                ).fetchone()
                profile = (
                    None
                    if due_row is None
                    else (QUANT_PROFILE_REGISTRY.get(due_row["profile_id"]))
                )
                if profile is None:
                    connection.execute("ROLLBACK")
                    raise ValueError("terminal")
                if (
                    result.cohort_size > profile.cohort_max
                    or result.admissible_count > result.cohort_size
                ):
                    connection.execute("ROLLBACK")
                    raise ValueError("cohort_exceeds_profile_maximum")
                # B2: the material's typed projection must bind exactly to
                # this due row before the result row may exist.
                try:
                    validate_material_binding(
                        due=_due_binding_view(due_row),
                        result=result,
                    )
                except ValueError:
                    connection.execute("ROLLBACK")
                    raise
                connection.execute(
                    "INSERT INTO quant_profile_result "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        result.identity_digest,
                        result.acquisition_id,
                        result.membership_holdings_digest,
                        iso(result.holdings_captured_at),
                        result.membership_starts,
                        result.candle_page1_starts,
                        result.candle_page2_starts,
                        result.cohort_size,
                        result.admissible_count,
                        result.provenance_digest,
                        result.membership_material,
                        result.membership_material_digest,
                        result.pack_digest,
                        int(result.degraded),
                        json.dumps(result.limitations, separators=(",", ":")),
                        json.dumps(result.symbols, separators=(",", ":")),
                    ),
                )
            changed = connection.execute(
                "UPDATE quant_profile_due SET state=?,finished_at=?,reason_code=? "
                "WHERE identity_digest=? AND state='CLAIMED'",
                (state.value, iso(finished_at), reason_code, identity_digest),
            ).rowcount
            if changed != 1:
                connection.execute("ROLLBACK")
                raise ValueError("terminal")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._close(connection, write=True)

    def reconcile_stale(self, *, now: datetime, policy: CadencePolicyV1) -> int:
        if not isinstance(policy, CadencePolicyV1):
            raise ValueError("policy")
        cutoff = now - timedelta(
            seconds=policy.child_timeout_seconds + policy.termination_margin_seconds
        )
        connection, _ = self._open()
        try:
            self._validate(connection)
            acquire_immediate_transaction(connection)
            changed = connection.execute(
                "UPDATE quant_profile_due SET state='UNKNOWN',finished_at=?,"
                "reason_code='stale_claim' WHERE state='CLAIMED' AND claimed_at<?",
                (iso(now), iso(cutoff)),
            ).rowcount
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            self._close(connection, write=True)
        return changed

    def read_due(self, identity_digest: str) -> QuantProfileDueV1 | None:
        connection, _ = self._open(readonly=True)
        try:
            self._validate(connection)
            row = connection.execute(
                "SELECT * FROM quant_profile_due WHERE identity_digest=?",
                (identity_digest,),
            ).fetchone()
        finally:
            self._close(connection)
        if row is None:
            return None
        return QuantProfileDueV1(
            row["identity_digest"],
            row["profile_id"],
            row["policy_version"],
            row["market"],
            row["completed_session_date"],
            row["universe_policy_version"],
            QuantTerminalStateV1(row["state"]),
            row["owner_invocation_id"],
            row["owner_lease_key"],
            parse_iso(row["claimed_at"]),
            parse_iso(row["finished_at"]),
            row["reason_code"],
        )

    def read_result(self, identity_digest: str) -> QuantProfileResultV1 | None:
        connection, _ = self._open(readonly=True)
        try:
            self._validate(connection)
            row = connection.execute(
                "SELECT * FROM quant_profile_result WHERE identity_digest=?",
                (identity_digest,),
            ).fetchone()
            due_row = connection.execute(
                "SELECT identity_digest,profile_id,policy_version,"
                "completed_session_date,universe_policy_version "
                "FROM quant_profile_due WHERE identity_digest=?",
                (identity_digest,),
            ).fetchone()
        finally:
            self._close(connection)
        if row is None:
            return None
        if due_row is None:
            raise ValueError("result_without_due")
        result = QuantProfileResultV1(
            row["identity_digest"],
            row["acquisition_id"],
            row["membership_holdings_digest"],
            parse_iso(row["holdings_captured_at"]),
            row["membership_starts"],
            row["candle_page1_starts"],
            row["candle_page2_starts"],
            row["cohort_size"],
            row["admissible_count"],
            row["provenance_digest"],
            row["membership_material"],
            row["membership_material_digest"],
            row["pack_digest"],
            bool(row["degraded"]),
            tuple(json.loads(row["limitations"])),
            tuple(json.loads(row["symbols"])),
        )
        _validated_material(result)
        # B2: re-bind the typed projection on every read so a semantically
        # tampered persisted row (even with a consistent hash) refuses.
        validate_material_binding(
            due=_due_binding_view(due_row),
            result=result,
        )
        return result
