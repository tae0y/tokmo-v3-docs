"""Private immutable FRED material store and closed LIVE campaign ledger."""

import hashlib
import json
import os
import sqlite3
import stat
import urllib.parse
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from tokmo.records.atomic_json import write_atomic_json
from tokmo.records.durability import fsync_directory
from tokmo.records.private_sqlite import (
    acquire_immediate_transaction,
    assert_no_sidecars_after_close,
    assert_private_sidecars_while_open,
    connect_private,
    prepare_private_path,
    private_creation,
)
from tokmo.values.fred_macro import (
    CAMPAIGN_CAP,
    CAMPAIGN_CODE,
    CODE_REVISION,
    MANIFEST_CODE,
    MANIFEST_DIGEST,
    MANIFEST_REVISION,
    PURPOSE,
    PURPOSE_CUMULATIVE_CEILING,
    R2_CAMPAIGN_CODE,
    R2_CODE_REVISION,
    R2_MANIFEST_CODE,
    R2_MANIFEST_DIGEST,
    R2_MANIFEST_REVISION,
    R3_CAMPAIGN_CODE,
    R3_CODE_REVISION,
    R3_MANIFEST_CODE,
    R3_MANIFEST_DIGEST,
    R3_MANIFEST_REVISION,
    R4_CAMPAIGN_CODE,
    R4_CODE_REVISION,
    R4_MANIFEST_CODE,
    R4_MANIFEST_DIGEST,
    R4_MANIFEST_REVISION,
    SERIES_ID,
    SOURCE,
    ExecutionMode,
    FredRequest,
    canonical_json,
    invocation_identity,
)

USER_VERSION = 4

_CAMPAIGN_ALLOWLIST = frozenset({R2_CAMPAIGN_CODE, R3_CAMPAIGN_CODE, R4_CAMPAIGN_CODE})

DDL = """
CREATE TABLE campaign(singleton INTEGER PRIMARY KEY CHECK(singleton=1),source TEXT NOT NULL,purpose TEXT NOT NULL,campaign_code TEXT NOT NULL,code_revision TEXT NOT NULL,manifest_code TEXT NOT NULL,manifest_revision TEXT NOT NULL,manifest_digest TEXT NOT NULL,execution_mode TEXT NOT NULL CHECK(execution_mode='LIVE'),series_id TEXT NOT NULL,campaign_cap INTEGER NOT NULL CHECK(campaign_cap=10),terminal_state TEXT NOT NULL CHECK(terminal_state IN ('ACTIVE','COMPLETE','QUARANTINED')),terminal_code TEXT,CHECK((terminal_state='ACTIVE' AND terminal_code IS NULL) OR (terminal_state!='ACTIVE' AND terminal_code IS NOT NULL))) STRICT;
CREATE TABLE attempts(source TEXT NOT NULL,purpose TEXT NOT NULL,invocation_identity TEXT NOT NULL,ordinal INTEGER NOT NULL,operation TEXT NOT NULL,request_identity TEXT NOT NULL,execution_mode TEXT NOT NULL CHECK(execution_mode IN ('FIXTURE','LIVE')),started_at TEXT NOT NULL,state TEXT NOT NULL CHECK(state IN ('RESERVED','SUCCEEDED','FAILED','UNKNOWN')),campaign_code TEXT NOT NULL,manifest_revision TEXT NOT NULL,manifest_digest TEXT NOT NULL,PRIMARY KEY(source,purpose,invocation_identity),UNIQUE(invocation_identity)) STRICT;
CREATE TABLE materials(material_identity TEXT PRIMARY KEY,source_canonical_bytes BLOB NOT NULL,source_sha256 TEXT NOT NULL,identity_provenance TEXT NOT NULL,execution_mode TEXT NOT NULL CHECK(execution_mode IN ('FIXTURE','LIVE')),available_at TEXT NOT NULL) STRICT;
CREATE TABLE acquisitions(material_identity TEXT NOT NULL REFERENCES materials(material_identity),invocation_identity TEXT NOT NULL,ordinal INTEGER NOT NULL,fetched_at TEXT NOT NULL,available_at TEXT NOT NULL,available_at_evidence TEXT NOT NULL,batch_identity TEXT NOT NULL,source_release_date_evidence TEXT,execution_mode TEXT NOT NULL CHECK(execution_mode IN ('FIXTURE','LIVE')),PRIMARY KEY(material_identity,invocation_identity)) STRICT;
CREATE TABLE batches(batch_identity TEXT PRIMARY KEY,invocation_identity TEXT NOT NULL,ordinal INTEGER NOT NULL,state TEXT NOT NULL CHECK(state='SUCCEEDED'),row_count INTEGER NOT NULL CHECK(row_count>0),provider_count INTEGER,completeness TEXT NOT NULL,completeness_evidence TEXT NOT NULL) STRICT;
CREATE UNIQUE INDEX attempts_live_ordinal ON attempts(source,purpose,execution_mode,ordinal);
CREATE INDEX acquisitions_invocation_batch ON acquisitions(invocation_identity,batch_identity,ordinal);
CREATE INDEX batches_invocation_ordinal ON batches(invocation_identity,ordinal);
CREATE TABLE campaigns (
    campaign_code TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    purpose TEXT NOT NULL,
    code_revision TEXT NOT NULL,
    manifest_code TEXT NOT NULL,
    manifest_revision TEXT NOT NULL,
    manifest_digest TEXT NOT NULL CHECK(length(manifest_digest)=64),
    execution_mode TEXT NOT NULL CHECK(execution_mode='LIVE'),
    series_id TEXT NOT NULL,
    campaign_cap INTEGER NOT NULL CHECK(campaign_cap=10),
    terminal_state TEXT NOT NULL CHECK(terminal_state IN ('ACTIVE','COMPLETE','QUARANTINED')),
    terminal_code TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    CHECK((terminal_state='ACTIVE' AND terminal_code IS NULL) OR
          (terminal_state!='ACTIVE' AND terminal_code IS NOT NULL))
) STRICT;
CREATE TABLE campaign_attempts (
    campaign_code TEXT NOT NULL,
    invocation_identity TEXT NOT NULL UNIQUE,
    manifest_ordinal INTEGER NOT NULL CHECK(manifest_ordinal BETWEEN 1 AND 5),
    PRIMARY KEY (campaign_code, manifest_ordinal),
    FOREIGN KEY (campaign_code) REFERENCES campaigns(campaign_code),
    FOREIGN KEY (invocation_identity) REFERENCES attempts(invocation_identity)
) STRICT;
CREATE INDEX campaign_attempts_invocation_idx ON campaign_attempts(invocation_identity);
PRAGMA user_version=4;
"""
DDL_V3 = DDL.split("CREATE TABLE campaigns", 1)[0] + "PRAGMA user_version=3;\n"
ADDITIVE_V4_DDL = (
    "CREATE TABLE campaigns" + DDL.split("CREATE TABLE campaigns", 1)[1]
).rsplit("PRAGMA user_version=4;", 1)[0]

_TABLES = (
    "campaign",
    "attempts",
    "materials",
    "acquisitions",
    "batches",
    "campaigns",
    "campaign_attempts",
)
_INDEXES = (
    "attempts_live_ordinal",
    "acquisitions_invocation_batch",
    "batches_invocation_ordinal",
    "campaign_attempts_invocation_idx",
)


@dataclass(frozen=True)
class Reservation:
    code: str
    invocation_identity: str
    ordinal: int | None


@dataclass(frozen=True)
class StoredMaterial:
    material_identity: str
    source_sha256: str
    available_at: datetime


@dataclass(frozen=True)
class StoreInspection:
    code: str
    schema_fingerprint_ok: bool = False
    fingerprint: str | None = None


def _instant(value: datetime) -> str:
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
        or value.utcoffset().total_seconds() != 0
    ):
        raise ValueError("aware_utc_required")
    return value.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _safe_stat(path: Path, *, directory: bool, mode: int) -> os.stat_result:
    value = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(value.st_mode)
        or not expected(value.st_mode)
        or stat.S_IMODE(value.st_mode) != mode
        or (not directory and value.st_nlink != 1)
        or not hasattr(os, "getuid")
        or value.st_uid != os.getuid()
    ):
        raise ValueError("unsafe_private_path")
    return value


def _schema_semantics(connection: sqlite3.Connection) -> dict:
    tables = {}
    for table in _TABLES:
        columns = [
            tuple(row[1:7])
            for row in connection.execute(f"PRAGMA table_xinfo('{table}')")
        ]
        foreign_keys = [
            tuple(row[2:8])
            for row in connection.execute(f"PRAGMA foreign_key_list('{table}')")
        ]
        indexes = []
        for row in connection.execute(f"PRAGMA index_list('{table}')"):
            name, unique, origin, partial = row[1], row[2], row[3], row[4]
            fields = tuple(
                item[2]
                for item in connection.execute(f"PRAGMA index_xinfo('{name}')")
                if item[5]
            )
            indexes.append((name if origin == "c" else origin, unique, partial, fields))
        tables[table] = {
            "columns": columns,
            "foreign_keys": sorted(foreign_keys),
            "indexes": sorted(indexes),
        }
    declared = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' AND type IN ('table','index')"
        )
    }
    contracts = {}
    for name in ("campaigns", "campaign_attempts"):
        row = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (name,)
        ).fetchone()
        contracts[name] = None if row is None else " ".join(row[0].split())
    return {
        "user_version": connection.execute("PRAGMA user_version").fetchone()[0],
        "declared": sorted(declared),
        "tables": tables,
        "sql_contracts": contracts,
    }


def _expected_semantics() -> tuple[dict, str]:
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL)
        value = _schema_semantics(connection)
    finally:
        connection.close()
    return value, hashlib.sha256(canonical_json(value)).hexdigest()


EXPECTED_SCHEMA, SCHEMA_FINGERPRINT = _expected_semantics()


class FredMaterialStore:
    def __init__(self, root: Path):
        if not isinstance(root, Path):
            raise ValueError("injected_root_required")
        self.root = root
        self.path = root / "fred.sqlite3"
        self.evidence_path = root / "store-evidence.json"
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise ValueError(safety.reason)
        created = not self.path.exists()
        if created:
            with private_creation():
                descriptor = os.open(
                    self.path, os.O_RDWR | os.O_CREAT | os.O_EXCL, 0o600
                )
                os.close(descriptor)
            fsync_directory(root)
        self._initialize(created)

    @classmethod
    def inspect_existing(cls, root: Path) -> StoreInspection:
        if not isinstance(root, Path):
            raise ValueError("injected_root_required")
        if not os.path.lexists(root):
            intended = []
            cursor = root.parent
            while not cursor.exists() and cursor != cursor.parent:
                intended.append(cursor)
                cursor = cursor.parent
            _safe_stat(cursor, directory=True, mode=0o700)
            for descendant in reversed(intended):
                if os.path.lexists(descendant):
                    _safe_stat(descendant, directory=True, mode=0o700)
            return StoreInspection("ABSENT")
        checked = {root: _safe_stat(root, directory=True, mode=0o700)}
        path = root / "fred.sqlite3"
        checked[path] = _safe_stat(path, directory=False, mode=0o600)
        for evidence in (root / "store-evidence.json", root / "g-live-evidence.json"):
            if os.path.lexists(evidence):
                checked[evidence] = _safe_stat(evidence, directory=False, mode=0o600)
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.lexists(Path(f"{path}{suffix}")):
                raise ValueError("sqlite_sidecar_present")
        parent_fd = os.open(root, os.O_RDONLY | os.O_NOFOLLOW)
        descriptor = None
        try:
            before = os.lstat(path)
            descriptor = os.open(
                path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd
            )
            opened = os.fstat(descriptor)
            if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("unsafe_path_swap")
            encoded = urllib.parse.quote(str(path.resolve()), safe="/")
            connection = sqlite3.connect(
                f"file:{encoded}?mode=ro&immutable=1", uri=True
            )
            try:
                if connection.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
                    raise ValueError("integrity_failure")
                semantics = _schema_semantics(connection)
            finally:
                connection.close()
            for checked_path, original in checked.items():
                current = os.lstat(checked_path)
                if (
                    current.st_dev,
                    current.st_ino,
                    current.st_mode,
                    current.st_uid,
                    current.st_nlink,
                ) != (
                    original.st_dev,
                    original.st_ino,
                    original.st_mode,
                    original.st_uid,
                    original.st_nlink,
                ):
                    raise ValueError("unsafe_path_swap")
            for suffix in ("-journal", "-wal", "-shm"):
                if os.path.lexists(Path(f"{path}{suffix}")):
                    raise ValueError("sqlite_sidecar_present")
        finally:
            if descriptor is not None:
                os.close(descriptor)
            os.close(parent_fd)
        if semantics != EXPECTED_SCHEMA:
            raise ValueError("schema_fingerprint_mismatch")
        digest = hashlib.sha256(canonical_json(semantics)).hexdigest()
        return StoreInspection("PRESENT", True, digest)

    def _connect(self):
        safety = prepare_private_path(self.path)
        if safety.unsafe:
            raise ValueError(safety.reason)
        return connect_private(self.path)

    def _initialize(self, created: bool):
        connection = self._connect()
        try:
            if created:
                connection.executescript(DDL)
            elif _schema_semantics(connection) != EXPECTED_SCHEMA:
                raise ValueError("schema_fingerprint_mismatch")
            assert_private_sidecars_while_open(self.path)
        finally:
            connection.close()
        fsync_directory(self.root)
        if assert_no_sidecars_after_close(self.path):
            raise ValueError("sqlite_sidecar_present")
        self.inspect_existing(self.root)

    def _campaign_metadata(self, campaign_code: str) -> tuple:
        """Return the expected 14-tuple for a closed campaign code."""
        instant = "2026-08-12T00:00:00.000000Z"
        if campaign_code == R2_CAMPAIGN_CODE:
            return (
                campaign_code,
                SOURCE,
                PURPOSE,
                R2_CODE_REVISION,
                R2_MANIFEST_CODE,
                R2_MANIFEST_REVISION,
                R2_MANIFEST_DIGEST,
                "LIVE",
                SERIES_ID,
                CAMPAIGN_CAP,
                "ACTIVE",
                None,
                instant,
                instant,
            )
        if campaign_code == R3_CAMPAIGN_CODE:
            return (
                campaign_code,
                SOURCE,
                PURPOSE,
                R3_CODE_REVISION,
                R3_MANIFEST_CODE,
                R3_MANIFEST_REVISION,
                R3_MANIFEST_DIGEST,
                "LIVE",
                SERIES_ID,
                CAMPAIGN_CAP,
                "ACTIVE",
                None,
                instant,
                instant,
            )
        if campaign_code == R4_CAMPAIGN_CODE:
            return (
                campaign_code,
                SOURCE,
                PURPOSE,
                R4_CODE_REVISION,
                R4_MANIFEST_CODE,
                R4_MANIFEST_REVISION,
                R4_MANIFEST_DIGEST,
                "LIVE",
                SERIES_ID,
                CAMPAIGN_CAP,
                "ACTIVE",
                None,
                instant,
                instant,
            )
        raise ValueError("campaign_mismatch")

    def _campaign_manifest_pair(self, campaign_code: str) -> tuple[str, str]:
        """Return (manifest_revision, manifest_digest) for a closed campaign code."""
        if campaign_code == R2_CAMPAIGN_CODE:
            return R2_MANIFEST_REVISION, R2_MANIFEST_DIGEST
        if campaign_code == R3_CAMPAIGN_CODE:
            return R3_MANIFEST_REVISION, R3_MANIFEST_DIGEST
        if campaign_code == R4_CAMPAIGN_CODE:
            return R4_MANIFEST_REVISION, R4_MANIFEST_DIGEST
        raise ValueError("campaign_mismatch")

    def ensure_live_campaign(self, *, campaign_code: str = CAMPAIGN_CODE) -> None:
        if campaign_code in _CAMPAIGN_ALLOWLIST:
            expected = self._campaign_metadata(campaign_code)
            connection = self._connect()
            try:
                acquire_immediate_transaction(connection)
                row = connection.execute(
                    "SELECT * FROM campaigns WHERE campaign_code=?", (campaign_code,)
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        expected,
                    )
                elif row != expected:
                    raise ValueError("campaign_mismatch")
                connection.execute("COMMIT")
                return
            except BaseException:
                if connection.in_transaction:
                    connection.execute("ROLLBACK")
                raise
            finally:
                connection.close()
        if campaign_code != CAMPAIGN_CODE:
            raise ValueError("campaign_mismatch")
        prefix = (
            1,
            SOURCE,
            PURPOSE,
            CAMPAIGN_CODE,
            CODE_REVISION,
            MANIFEST_CODE,
            MANIFEST_REVISION,
            MANIFEST_DIGEST,
            "LIVE",
            SERIES_ID,
            CAMPAIGN_CAP,
        )
        expected = (*prefix, "ACTIVE", None)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            row = connection.execute(
                "SELECT * FROM campaign WHERE singleton=1"
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO campaign VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", expected
                )
            elif row[:11] != prefix:
                connection.execute(
                    "UPDATE campaign SET terminal_state='QUARANTINED',terminal_code='revision_mismatch' WHERE singleton=1 AND terminal_state='ACTIVE'"
                )
                connection.execute("COMMIT")
                raise ValueError("campaign_mismatch")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def campaign_state(self) -> tuple[str, str | None] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT terminal_state,terminal_code FROM campaign WHERE singleton=1"
            ).fetchone()
            return None if row is None else (row[0], row[1])
        finally:
            connection.close()

    def campaign_state_for(self, campaign_code: str) -> tuple[str, str | None] | None:
        if campaign_code not in _CAMPAIGN_ALLOWLIST:
            raise ValueError("campaign_mismatch")
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
                (campaign_code,),
            ).fetchone()
            return None if row is None else (row[0], row[1])
        finally:
            connection.close()

    def campaign_attempt_count(self, campaign_code: str) -> int:
        if campaign_code not in _CAMPAIGN_ALLOWLIST:
            raise ValueError("campaign_mismatch")
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
                (campaign_code,),
            ).fetchone()[0]
        finally:
            connection.close()

    def aggregate_live_count(self) -> int:
        connection = self._connect()
        try:
            return connection.execute(
                "SELECT count(*) FROM attempts WHERE source=? AND purpose=? AND execution_mode='LIVE'",
                (SOURCE, PURPOSE),
            ).fetchone()[0]
        finally:
            connection.close()

    def reconcile_campaign_unknown(
        self, campaign_code: str, before: datetime | None = None
    ) -> int:
        if campaign_code not in _CAMPAIGN_ALLOWLIST:
            raise ValueError("campaign_mismatch")
        cutoff = None if before is None else _instant(before)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            where = "c.campaign_code=? AND a.state='RESERVED'"
            args: tuple[object, ...] = (campaign_code,)
            if cutoff is not None:
                where += " AND a.started_at < ?"
                args += (cutoff,)
            identities = [
                row[0]
                for row in connection.execute(
                    f"SELECT a.invocation_identity FROM attempts a JOIN campaign_attempts c "
                    f"ON c.invocation_identity=a.invocation_identity WHERE {where}",
                    args,
                )
            ]
            for identity in identities:
                connection.execute(
                    "UPDATE attempts SET state='UNKNOWN' WHERE invocation_identity=? AND state='RESERVED'",
                    (identity,),
                )
            if identities:
                connection.execute(
                    "UPDATE campaigns SET terminal_state='QUARANTINED',terminal_code='stale_reservation' WHERE campaign_code=? AND terminal_state='ACTIVE'",
                    (campaign_code,),
                )
            connection.execute("COMMIT")
            return len(identities)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def _campaign(self, connection, campaign_code: str | None = None):
        if campaign_code in _CAMPAIGN_ALLOWLIST:
            return connection.execute(
                "SELECT * FROM campaigns WHERE campaign_code=?", (campaign_code,)
            ).fetchone()
        return connection.execute("SELECT * FROM campaign WHERE singleton=1").fetchone()

    def assert_mode_allowed(
        self, execution_mode: ExecutionMode, campaign_code: str | None = None
    ) -> None:
        if campaign_code in _CAMPAIGN_ALLOWLIST:
            connection = self._connect()
            try:
                campaign = self._campaign(connection, campaign_code)
            finally:
                connection.close()
            if campaign is None or campaign[10] != "ACTIVE":
                raise ValueError("campaign_mismatch")
            if execution_mode is not ExecutionMode.LIVE:
                raise ValueError("fixture_live_campaign_forbidden")
            return
        connection = self._connect()
        try:
            campaign = self._campaign(connection)
        finally:
            connection.close()
        if campaign is not None and execution_mode is not ExecutionMode.LIVE:
            raise ValueError("fixture_live_campaign_forbidden")

    def reconcile_unknown(self, before: datetime | None = None) -> int:
        cutoff = None if before is None else _instant(before)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            where, args = "state='RESERVED'", ()
            if cutoff is not None:
                where += " AND started_at < ?"
                args = (cutoff,)
            count = connection.execute(
                f"SELECT count(*) FROM attempts WHERE {where}", args
            ).fetchone()[0]
            connection.execute(
                f"UPDATE attempts SET state='UNKNOWN' WHERE {where}", args
            )
            if count and self._campaign(connection) is not None:
                connection.execute(
                    "UPDATE campaign SET terminal_state='QUARANTINED',terminal_code='stale_reservation' WHERE singleton=1 AND terminal_state='ACTIVE'"
                )
            connection.execute("COMMIT")
            return count
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reserve(
        self,
        request: FredRequest,
        *,
        execution_mode: ExecutionMode = ExecutionMode.FIXTURE,
        started_at: datetime | None = None,
    ) -> Reservation:
        if not isinstance(execution_mode, ExecutionMode):
            raise ValueError("execution_mode_required")
        start = _instant(started_at)
        self.assert_mode_allowed(execution_mode)
        identity = invocation_identity(request, execution_mode)
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            campaign = self._campaign(connection)
            if execution_mode is ExecutionMode.LIVE:
                if campaign is None:
                    raise ValueError("live_campaign_required")
                if campaign[11] != "ACTIVE" or campaign[1:11] != (
                    SOURCE,
                    PURPOSE,
                    CAMPAIGN_CODE,
                    CODE_REVISION,
                    MANIFEST_CODE,
                    MANIFEST_REVISION,
                    MANIFEST_DIGEST,
                    "LIVE",
                    SERIES_ID,
                    CAMPAIGN_CAP,
                ):
                    raise ValueError("campaign_mismatch")
            prior = connection.execute(
                "SELECT ordinal FROM attempts WHERE invocation_identity=?", (identity,)
            ).fetchone()
            if prior is not None:
                connection.execute("COMMIT")
                return Reservation(
                    "already_attempted_fixture"
                    if execution_mode is ExecutionMode.FIXTURE
                    else "provenance_mismatch",
                    identity,
                    prior[0],
                )
            count = connection.execute(
                "SELECT count(*) FROM attempts WHERE source=? AND purpose=? AND execution_mode=?",
                (SOURCE, PURPOSE, execution_mode.value),
            ).fetchone()[0]
            if count >= CAMPAIGN_CAP:
                connection.execute("COMMIT")
                return Reservation("cap_exhausted", identity, None)
            last = connection.execute(
                "SELECT started_at FROM attempts WHERE source=? AND purpose=? AND execution_mode=? ORDER BY started_at DESC LIMIT 1",
                (SOURCE, PURPOSE, execution_mode.value),
            ).fetchone()
            if last is not None:
                previous = datetime.fromisoformat(last[0].replace("Z", "+00:00"))
                if (started_at - previous).total_seconds() < 1.0:
                    connection.execute("COMMIT")
                    return Reservation("pacing_required", identity, None)
            ordinal = count + 1
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    SOURCE,
                    PURPOSE,
                    identity,
                    ordinal,
                    request.operation.value,
                    request.request_identity,
                    execution_mode.value,
                    start,
                    "RESERVED",
                    CAMPAIGN_CODE
                    if execution_mode is ExecutionMode.LIVE
                    else "FIXTURE",
                    MANIFEST_REVISION,
                    MANIFEST_DIGEST,
                ),
            )
            connection.execute("COMMIT")
            return Reservation("reserved", identity, ordinal)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def reserve_campaign(
        self,
        campaign_code: str,
        manifest_ordinal: int,
        request: FredRequest,
        *,
        started_at: datetime,
        external_diagnostic_count: int = 0,
        external_rca_count: int = 0,
    ) -> Reservation:
        if (
            campaign_code not in _CAMPAIGN_ALLOWLIST
            or type(manifest_ordinal) is not int
        ):
            raise ValueError("campaign_mismatch")
        if type(external_diagnostic_count) is not int or external_diagnostic_count < 0:
            raise ValueError("invalid_external_diagnostic_count")
        if type(external_rca_count) is not int or external_rca_count < 0:
            raise ValueError("invalid_external_rca_count")
        if not 1 <= manifest_ordinal <= 5:
            raise ValueError("manifest_position_mismatch")
        start = _instant(started_at)
        manifest_revision, manifest_digest = self._campaign_manifest_pair(campaign_code)
        identity = hashlib.sha256(
            canonical_json(
                {
                    "campaign_code": campaign_code,
                    "manifest_ordinal": manifest_ordinal,
                    "request_identity": request.request_identity,
                    "manifest_revision": manifest_revision,
                }
            )
        ).hexdigest()
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            campaign = connection.execute(
                "SELECT * FROM campaigns WHERE campaign_code=?", (campaign_code,)
            ).fetchone()
            expected_prefix = self._campaign_metadata(campaign_code)[:12]
            if campaign is None or campaign[:12] != expected_prefix:
                raise ValueError("campaign_mismatch")
            if connection.execute(
                "SELECT count(*) FROM campaigns WHERE campaign_cap != ?",
                (CAMPAIGN_CAP,),
            ).fetchone()[0]:
                raise ValueError("campaign_cap_mismatch")
            aggregate = connection.execute(
                "SELECT count(*) FROM attempts WHERE source=? AND purpose=? AND execution_mode='LIVE'",
                (SOURCE, PURPOSE),
            ).fetchone()[0]
            if (
                aggregate + external_diagnostic_count + external_rca_count
                >= PURPOSE_CUMULATIVE_CEILING
            ):
                connection.execute("COMMIT")
                return Reservation("cap_exhausted", identity, None)
            mapped = connection.execute(
                "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
                (campaign_code,),
            ).fetchone()[0]
            if manifest_ordinal != mapped + 1:
                raise ValueError("manifest_position_mismatch")
            ordinal = connection.execute(
                "SELECT coalesce(max(ordinal),0)+1 FROM attempts WHERE source=? AND purpose=? AND execution_mode='LIVE'",
                (SOURCE, PURPOSE),
            ).fetchone()[0]
            connection.execute(
                "INSERT INTO attempts VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    SOURCE,
                    PURPOSE,
                    identity,
                    ordinal,
                    request.operation.value,
                    request.request_identity,
                    "LIVE",
                    start,
                    "RESERVED",
                    campaign_code,
                    manifest_revision,
                    manifest_digest,
                ),
            )
            connection.execute(
                "INSERT INTO campaign_attempts VALUES(?,?,?)",
                (campaign_code, identity, manifest_ordinal),
            )
            connection.execute("COMMIT")
            return Reservation("reserved", identity, ordinal)
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def validate_live_reservation(
        self, reservation: Reservation, request: FredRequest
    ) -> None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT ordinal,operation,request_identity,execution_mode,state,campaign_code,manifest_revision,manifest_digest FROM attempts WHERE invocation_identity=?",
                (reservation.invocation_identity,),
            ).fetchone()
        finally:
            connection.close()
        valid = {
            (
                reservation.ordinal,
                request.operation.value,
                request.request_identity,
                "LIVE",
                "RESERVED",
                CAMPAIGN_CODE,
                MANIFEST_REVISION,
                MANIFEST_DIGEST,
            ),
            (
                reservation.ordinal,
                request.operation.value,
                request.request_identity,
                "LIVE",
                "RESERVED",
                R2_CAMPAIGN_CODE,
                R2_MANIFEST_REVISION,
                R2_MANIFEST_DIGEST,
            ),
            (
                reservation.ordinal,
                request.operation.value,
                request.request_identity,
                "LIVE",
                "RESERVED",
                R3_CAMPAIGN_CODE,
                R3_MANIFEST_REVISION,
                R3_MANIFEST_DIGEST,
            ),
            (
                reservation.ordinal,
                request.operation.value,
                request.request_identity,
                "LIVE",
                "RESERVED",
                R4_CAMPAIGN_CODE,
                R4_MANIFEST_REVISION,
                R4_MANIFEST_DIGEST,
            ),
        }
        if reservation.code != "reserved" or row not in valid:
            raise ValueError("invalid_live_reservation")

    def settle(self, reservation: Reservation, state: str):
        if reservation.code != "reserved" or state not in (
            "SUCCEEDED",
            "FAILED",
            "UNKNOWN",
        ):
            raise ValueError("invalid_settlement")
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            changed = connection.execute(
                "UPDATE attempts SET state=? WHERE invocation_identity=? AND state='RESERVED'",
                (state, reservation.invocation_identity),
            ).rowcount
            if changed != 1:
                raise ValueError("settlement_conflict")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def quarantine(self, code: str) -> None:
        allowed = {
            "empty_page",
            "credential_failure",
            "operation_failure",
            "durable_success_evidence_failure",
            "evidence_write_failure",
            "stale_reservation",
            "revision_mismatch",
            "settlement_conflict",
            "terminal_partial",
            "clock_failure",
            "release_handoff_invalid",
            "pacing_required",
            "provenance_mismatch",
            "cap_exhausted",
            "multiple_provider_starts",
            "transport_failure",
            "replay_invalid",
            "response_contract_failure",
            "store_failure",
            "timeout",
            "connection_failure",
            "redirect",
            "auth_rejection",
            "bad_request",
            "http_429",
            "http_5xx",
            "body_too_large",
            "unsupported_content_encoding",
            "protocol_failure",
        }
        allowed.update({"api_key_not_registered", "provider_bad_request_unknown"})
        if code not in allowed:
            code = "operation_failure"
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            connection.execute(
                "UPDATE campaign SET terminal_state='QUARANTINED',terminal_code=? WHERE singleton=1 AND terminal_state IN ('ACTIVE','COMPLETE')",
                (code,),
            )
            connection.execute("COMMIT")
        finally:
            connection.close()

    def quarantine_campaign(self, campaign_code: str, code: str) -> None:
        if campaign_code not in _CAMPAIGN_ALLOWLIST:
            raise ValueError("campaign_mismatch")
        allowed = {
            "stale_reservation",
            "terminal_partial",
            "replay_invalid",
            "credential_failure",
            "response_contract_failure",
            "store_failure",
            "timeout",
            "connection_failure",
            "redirect",
            "auth_rejection",
            "bad_request",
            "api_key_not_registered",
            "provider_bad_request_unknown",
            "http_429",
            "http_5xx",
            "body_too_large",
            "unsupported_content_encoding",
            "protocol_failure",
            "operation_failure",
            "evidence_write_failure",
            "durable_success_evidence_failure",
            "clock_failure",
            "transport_failure",
            "empty_page",
        }
        static = code if code in allowed else "operation_failure"
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            changed = connection.execute(
                "UPDATE campaigns SET terminal_state='QUARANTINED',terminal_code=? WHERE campaign_code=? AND terminal_state IN ('ACTIVE','COMPLETE')",
                (static, campaign_code),
            ).rowcount
            if changed == 0:
                row = connection.execute(
                    "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
                    (campaign_code,),
                ).fetchone()
                if row is None or row[0] != "QUARANTINED" or row[1] != static:
                    raise ValueError("campaign_mismatch")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def complete_campaign(self, campaign_code: str) -> None:
        if campaign_code not in _CAMPAIGN_ALLOWLIST:
            raise ValueError("campaign_mismatch")
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            successes = connection.execute(
                "SELECT count(*) FROM campaign_attempts c JOIN attempts a ON a.invocation_identity=c.invocation_identity WHERE c.campaign_code=? AND a.state='SUCCEEDED'",
                (campaign_code,),
            ).fetchone()[0]
            if successes != 5:
                raise ValueError("terminal_partial")
            changed = connection.execute(
                "UPDATE campaigns SET terminal_state='COMPLETE',terminal_code='manifest_complete' WHERE campaign_code=? AND terminal_state='ACTIVE'",
                (campaign_code,),
            ).rowcount
            if changed != 1:
                raise ValueError("campaign_mismatch")
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()

    def counts(self) -> dict:
        connection = self._connect()
        try:
            result = {
                name.lower(): connection.execute(
                    f"SELECT count(*) FROM {name}"
                ).fetchone()[0]
                for name in ("attempts", "materials", "acquisitions", "batches")
            }
            for state in ("RESERVED", "SUCCEEDED", "FAILED", "UNKNOWN"):
                result[state.lower()] = connection.execute(
                    "SELECT count(*) FROM attempts WHERE state=?", (state,)
                ).fetchone()[0]
            return result
        finally:
            connection.close()

    def store_material(
        self,
        source: dict,
        *,
        material_identity: str,
        invocation_identity: str,
        ordinal: int,
        fetched_at: datetime,
        batch_identity: str,
        authoritative_scheduled_release_at: datetime | None = None,
        identity_provenance: str = "PROVIDER",
        source_release_date_evidence: str | None = None,
    ) -> StoredMaterial:
        fetched = _instant(fetched_at)
        scheduled = (
            None
            if authoritative_scheduled_release_at is None
            else _instant(authoritative_scheduled_release_at)
        )
        available = max(fetched, scheduled) if scheduled else fetched
        canonical, digest = (
            canonical_json(source),
            hashlib.sha256(canonical_json(source)).hexdigest(),
        )
        connection = self._connect()
        try:
            acquire_immediate_transaction(connection)
            attempt = connection.execute(
                "SELECT execution_mode FROM attempts WHERE invocation_identity=? AND ordinal=?",
                (invocation_identity, ordinal),
            ).fetchone()
            if attempt is None:
                raise ValueError("attempt_join_missing")
            prior = connection.execute(
                "SELECT source_canonical_bytes,source_sha256,available_at FROM materials WHERE material_identity=?",
                (material_identity,),
            ).fetchone()
            if prior is None:
                connection.execute(
                    "INSERT INTO materials VALUES(?,?,?,?,?,?)",
                    (
                        material_identity,
                        canonical,
                        digest,
                        identity_provenance,
                        attempt[0],
                        available,
                    ),
                )
                material_available = available
            elif prior[:2] != (canonical, digest):
                raise ValueError("immutable_conflict")
            else:
                material_available = prior[2]
            connection.execute(
                "INSERT INTO acquisitions VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    material_identity,
                    invocation_identity,
                    ordinal,
                    fetched,
                    available,
                    "AUTHORITATIVE_SCHEDULE_MAX_FETCH" if scheduled else "FETCHED_AT",
                    batch_identity,
                    source_release_date_evidence,
                    attempt[0],
                ),
            )
            connection.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch_identity,
                    invocation_identity,
                    ordinal,
                    "SUCCEEDED",
                    1,
                    1,
                    "COMPLETE",
                    "MATERIAL",
                ),
            )
            connection.execute("COMMIT")
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        fsync_directory(self.root)
        write_atomic_json(
            self.evidence_path,
            canonical_json(
                {
                    "schema": "tokmo.fred.store-evidence.r1",
                    "last_batch": batch_identity,
                    "last_source_sha256": digest,
                    "provenance": "STORE_VERIFIED",
                }
            ),
            max_bytes=4096,
        )
        return StoredMaterial(
            material_identity,
            digest,
            datetime.fromisoformat(material_available.replace("Z", "+00:00")),
        )

    def store_page(
        self,
        rows: tuple[dict, ...],
        *,
        reservation: Reservation,
        operation: str,
        fetched_at: datetime,
        provider_count: int | None = None,
        completeness: str = "UNKNOWN",
        completeness_evidence: str = "UNSPECIFIED",
    ) -> tuple[str, ...]:
        fetched = _instant(fetched_at)
        if not rows:
            raise ValueError("empty_page")
        connection = self._connect()
        try:
            mode = connection.execute(
                "SELECT execution_mode FROM attempts WHERE invocation_identity=?",
                (reservation.invocation_identity,),
            ).fetchone()
        finally:
            connection.close()
        if mode is None:
            raise ValueError("attempt_join_missing")
        self.assert_mode_allowed(ExecutionMode(mode[0]))
        prepared = []
        for index, row in enumerate(rows):
            canonical = canonical_json({"operation": operation, "row": row})
            digest = hashlib.sha256(canonical).hexdigest()
            fields = {
                key: row.get(key)
                for key in (
                    "series_id",
                    "id",
                    "release_id",
                    "date",
                    "vintage_date",
                    "realtime_start",
                    "realtime_end",
                )
                if key in row
            }
            identity = hashlib.sha256(
                canonical_json(
                    {
                        "operation": operation,
                        "identity": fields,
                        "execution_mode": mode[0],
                    }
                )
            ).hexdigest()
            prepared.append(
                (
                    identity,
                    canonical,
                    digest,
                    index,
                    row.get("date") if operation == "RELEASE_DATES" else None,
                )
            )
        batch = hashlib.sha256(
            canonical_json(
                {
                    "invocation_identity": reservation.invocation_identity,
                    "ordinal": reservation.ordinal,
                    "operation": operation,
                }
            )
        ).hexdigest()
        connection = self._connect()
        committed = False
        try:
            acquire_immediate_transaction(connection)
            for identity, canonical, digest, index, source_date in prepared:
                prior = connection.execute(
                    "SELECT source_canonical_bytes,source_sha256 FROM materials WHERE material_identity=?",
                    (identity,),
                ).fetchone()
                if prior is None:
                    connection.execute(
                        "INSERT INTO materials VALUES(?,?,?,?,?,?)",
                        (identity, canonical, digest, "MIXED", mode[0], fetched),
                    )
                elif prior != (canonical, digest):
                    raise ValueError("immutable_conflict")
                connection.execute(
                    "INSERT INTO acquisitions VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        identity,
                        reservation.invocation_identity,
                        reservation.ordinal,
                        fetched,
                        fetched,
                        "FETCHED_AT",
                        batch,
                        source_date,
                        mode[0],
                    ),
                )
            connection.execute(
                "INSERT INTO batches VALUES(?,?,?,?,?,?,?,?)",
                (
                    batch,
                    reservation.invocation_identity,
                    reservation.ordinal,
                    "SUCCEEDED",
                    len(prepared),
                    provider_count,
                    completeness,
                    completeness_evidence,
                ),
            )
            if (
                connection.execute(
                    "UPDATE attempts SET state='SUCCEEDED' WHERE invocation_identity=? AND state='RESERVED'",
                    (reservation.invocation_identity,),
                ).rowcount
                != 1
            ):
                raise ValueError("settlement_conflict")
            connection.execute("COMMIT")
            committed = True
        except BaseException:
            if connection.in_transaction:
                connection.execute("ROLLBACK")
            raise
        finally:
            connection.close()
        try:
            fsync_directory(self.root)
            write_atomic_json(
                self.evidence_path,
                canonical_json(
                    {
                        "schema": "tokmo.fred.store-evidence.r1",
                        "last_batch": batch,
                        "material_count": len(prepared),
                        "provenance": "STORE_VERIFIED",
                    }
                ),
                max_bytes=4096,
            )
        except Exception as error:
            if committed:
                raise ValueError("durable_success_evidence_failure") from error
            raise
        return tuple(item[2] for item in prepared)

    def read_release_id_for_campaign(
        self,
        *,
        mode: str,
        source: str,
        purpose: str,
        campaign_code: str,
        manifest_revision: str,
        manifest_digest: str,
        invocation_identity: str,
        ordinal: int,
        execution_mode: ExecutionMode,
        current_run_identity: str | None = None,
    ) -> int:
        if (
            mode not in ("RECONSTRUCTION", "CURRENT_CONSTRUCTION")
            or execution_mode is not ExecutionMode.LIVE
        ):
            raise ValueError("release_handoff_invalid")
        if mode == "CURRENT_CONSTRUCTION" and (
            current_run_identity is None or current_run_identity != invocation_identity
        ):
            raise ValueError("release_handoff_invalid")
        connection = self._connect()
        try:
            if campaign_code in _CAMPAIGN_ALLOWLIST:
                rows = connection.execute(
                    """SELECT m.source_canonical_bytes FROM campaign_attempts c JOIN attempts a ON a.invocation_identity=c.invocation_identity JOIN acquisitions q ON q.invocation_identity=a.invocation_identity JOIN batches b ON b.invocation_identity=a.invocation_identity AND b.batch_identity=q.batch_identity JOIN materials m ON m.material_identity=q.material_identity WHERE a.source=? AND a.purpose=? AND c.campaign_code=? AND a.manifest_revision=? AND a.manifest_digest=? AND a.invocation_identity=? AND c.manifest_ordinal=? AND a.operation='SERIES_RELEASE' AND a.state='SUCCEEDED' AND a.execution_mode='LIVE' AND q.execution_mode='LIVE' AND q.ordinal=a.ordinal AND b.ordinal=a.ordinal AND b.state='SUCCEEDED' AND b.row_count=1""",
                    (
                        source,
                        purpose,
                        campaign_code,
                        manifest_revision,
                        manifest_digest,
                        invocation_identity,
                        ordinal,
                    ),
                ).fetchall()
            else:
                rows = connection.execute(
                    """SELECT m.source_canonical_bytes FROM attempts a JOIN acquisitions q ON q.invocation_identity=a.invocation_identity JOIN batches b ON b.invocation_identity=a.invocation_identity AND b.batch_identity=q.batch_identity JOIN materials m ON m.material_identity=q.material_identity WHERE a.source=? AND a.purpose=? AND a.campaign_code=? AND a.manifest_revision=? AND a.manifest_digest=? AND a.invocation_identity=? AND a.ordinal=? AND a.operation='SERIES_RELEASE' AND a.state='SUCCEEDED' AND a.execution_mode='LIVE' AND q.execution_mode='LIVE' AND q.ordinal=a.ordinal AND b.ordinal=a.ordinal AND b.state='SUCCEEDED' AND b.row_count=1""",
                    (
                        source,
                        purpose,
                        campaign_code,
                        manifest_revision,
                        manifest_digest,
                        invocation_identity,
                        ordinal,
                    ),
                ).fetchall()
        finally:
            connection.close()
        if len(rows) != 1:
            raise ValueError("release_handoff_invalid")
        try:
            value = json.loads(rows[0][0])["row"]["id"]
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            raise ValueError("release_handoff_invalid") from None
        if type(value) is not int or value <= 0:
            raise ValueError("release_handoff_invalid")
        return value
