#!/usr/bin/env python3
"""Closed Stage3 FRED G-LIVE controller. Live use requires an explicit gate."""

import argparse
import fcntl
import hashlib
import json
import os
import pwd
import shutil
import sqlite3
import stat
import sys
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1] / "src"))

from tokmo.facts.fred_live import (
    FredLiveTransport,
    build_live_opener,
    load_fred_api_key,
)
from tokmo.fred_composition import run_reserved_fred_operation
from tokmo.records.atomic_json import write_atomic_json
from tokmo.records.durability import fsync_directory
from tokmo.records.fred_material_store import (
    ADDITIVE_V4_DDL,
    SCHEMA_FINGERPRINT,
    FredMaterialStore,
    _schema_semantics,
)
from tokmo.records.private_sqlite import connect_private, private_creation
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
    canonical_json,
    g_live_manifest,
    r3_g_live_manifest,
    r4_g_live_manifest,
)

EVIDENCE_CLASSES = (
    "G0_OFFICIAL_RETRIEVAL",
    "RECONSTRUCTED_CONTRACT",
    "G0_OFFICIAL_RETRIEVAL",
    "G0_OFFICIAL_RETRIEVAL",
    "RECONSTRUCTED_CONTRACT",
)


def migrate_r2_schema(root: Path, *, lock_path: Path, migrated_at: datetime) -> dict:
    """Perform the reviewed V3->V4 mutation on an explicitly injected local root."""
    _metadata_preflight(root, lock_path)
    if _r3_campaign_exists(root):
        return {"code": "r2_retired_by_r3"}
    with canonical_lock(lock_path):
        return _migrate_r2_locked(root, migrated_at)


def _migrate_r2_locked(root: Path, migrated_at: datetime) -> dict:
    _root_metadata_preflight(root)  # re-verify under lock before any backup/write
    database = root / "fred.sqlite3"
    receipt_path = root / "g-live-r2-migration-receipt.json"
    if os.path.lexists(receipt_path):
        receipt = _safe_json(receipt_path)
        readback = readback_r2_postmigration(root)
        backup = root / str(receipt.get("backup_basename", ""))
        if (
            readback["code"] == "readback_ok"
            and _safe_digest(backup) == receipt.get("backup_sha256")
            and _database_digest(database) == receipt.get("post_database_sha256")
            and receipt.get("r1_evidence_sha256") == _r1_evidence_sha256(root)
            and receipt.get("store_evidence_sha256") == _store_evidence_sha256(root)
        ):
            return receipt
        raise ValueError("migration_artifact_conflict")
    r1_evidence_hash = _r1_evidence_sha256(root)
    store_evidence_hash = _store_evidence_sha256(root)
    pre_hash = _database_digest(database)
    backup = root / f"fred.sqlite3.pre-r2.{pre_hash}.bak"
    if os.path.lexists(backup):
        raise ValueError("migration_artifact_conflict")
    source_fd = os.open(database, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        with private_creation():
            backup_fd = os.open(
                backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600
            )
        try:
            while chunk := os.read(source_fd, 65536):
                os.write(backup_fd, chunk)
            os.fsync(backup_fd)
        finally:
            os.close(backup_fd)
    finally:
        os.close(source_fd)
    fsync_directory(root)
    if _database_digest(backup) != pre_hash:
        raise ValueError("backup_hash_mismatch")
    connection = connect_private(database)
    try:
        if connection.execute("PRAGMA user_version").fetchone()[0] != 3:
            raise ValueError("schema_version_mismatch")
        legacy = connection.execute(
            "SELECT * FROM campaign WHERE singleton=1"
        ).fetchone()
        if legacy is None:
            raise ValueError("r1_campaign_missing")
        connection.executescript("BEGIN IMMEDIATE;\n" + ADDITIVE_V4_DDL)
        timestamp = (
            migrated_at.astimezone(UTC)
            .isoformat(timespec="microseconds")
            .replace("+00:00", "Z")
        )
        connection.execute(
            "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                legacy[3],
                legacy[1],
                legacy[2],
                legacy[4],
                legacy[5],
                legacy[6],
                legacy[7],
                legacy[8],
                legacy[9],
                10,
                legacy[11],
                legacy[12],
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            "INSERT INTO campaigns VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                R2_CAMPAIGN_CODE,
                "FRED",
                "CANONICAL_SOURCE_STAGE3",
                R2_CODE_REVISION,
                R2_MANIFEST_CODE,
                R2_MANIFEST_REVISION,
                R2_MANIFEST_DIGEST,
                "LIVE",
                "PAYEMS",
                10,
                "ACTIVE",
                None,
                timestamp,
                timestamp,
            ),
        )
        r1_attempt = connection.execute(
            "SELECT invocation_identity FROM attempts WHERE campaign_code=? ORDER BY ordinal LIMIT 1",
            (legacy[3],),
        ).fetchone()
        if r1_attempt is not None:
            connection.execute(
                "INSERT INTO campaign_attempts VALUES(?,?,1)",
                (legacy[3], r1_attempt[0]),
            )
        connection.execute("PRAGMA user_version=4")
        connection.execute("COMMIT")
    except Exception:
        if connection.in_transaction:
            connection.execute("ROLLBACK")
        raise
    finally:
        connection.close()
    fsync_directory(root)
    post_hash = _database_digest(database)
    if post_hash == pre_hash:
        raise ValueError("migration_no_change")
    FredMaterialStore.inspect_existing(root)
    readback_r2_postmigration(root)
    r1_projection = _r1_projection_digest(root)
    timestamp = (
        migrated_at.astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
    r1_evidence_post = _r1_evidence_sha256(root)
    store_evidence_post = _store_evidence_sha256(root)
    receipt = {
        "schema": "tokmo.fred.g-live-r2-migration-receipt.v1",
        "from_user_version": 3,
        "to_user_version": 4,
        "schema_fingerprint_before": _v3_fingerprint(root),
        "schema_fingerprint_after": SCHEMA_FINGERPRINT,
        "pre_database_sha256": pre_hash,
        "post_database_sha256": post_hash,
        "backup_basename": backup.name,
        "backup_sha256": pre_hash,
        "r1_projection_sha256": r1_projection["digest"],
        "r1_campaign_count": r1_projection["campaign_count"],
        "r1_attempt_count": r1_projection["attempt_count"],
        "r1_evidence_sha256": r1_evidence_hash,
        "store_evidence_sha256": store_evidence_hash,
        "migrated_at": timestamp,
        "aggregate_live_high_water": 1,
    }
    write_atomic_json(receipt_path, canonical_json(receipt), max_bytes=16384)
    evidence = {
        "schema": "tokmo.fred.g-live-r2-evidence.v1",
        "campaign_code": R2_CAMPAIGN_CODE,
        "manifest_code": R2_MANIFEST_CODE,
        "manifest_revision": R2_MANIFEST_REVISION,
        "manifest_digest": R2_MANIFEST_DIGEST,
        "aggregate_live_high_water": 1,
        "aggregate_live_count": 1,
        "r1_evidence_sha256": r1_evidence_post,
        "store_evidence_sha256": store_evidence_post,
        "pre_database_sha256": pre_hash,
        "latest_post_database_sha256": post_hash,
        "backup_sha256": pre_hash,
        "migration_receipt_sha256": _safe_digest(receipt_path),
    }
    write_atomic_json(
        root / "g-live-r2-evidence.json", canonical_json(evidence), max_bytes=16384
    )
    return receipt


def readback_r2(root: Path) -> dict:
    if _r3_campaign_exists(root):
        return {"code": "r2_retired_by_r3"}
    inspection = FredMaterialStore.inspect_existing(root)
    if not inspection.schema_fingerprint_ok:
        raise ValueError("schema_fingerprint_mismatch")
    connection = connect_private(root / "fred.sqlite3", mode="ro")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("integrity_failure")
        campaigns = connection.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        mappings = connection.execute(
            "SELECT count(*) FROM campaign_attempts"
        ).fetchone()[0]
        aggregate = connection.execute(
            "SELECT count(*) FROM attempts WHERE source='FRED' AND purpose='CANONICAL_SOURCE_STAGE3' AND execution_mode='LIVE'"
        ).fetchone()[0]
        r2_attempts = connection.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (R2_CAMPAIGN_CODE,),
        ).fetchone()[0]
        r1_campaign = connection.execute(
            "SELECT source,purpose,campaign_code,code_revision,manifest_code,manifest_revision,manifest_digest,execution_mode,series_id,campaign_cap,terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
        r1_projected = connection.execute(
            "SELECT source,purpose,campaign_code,code_revision,manifest_code,manifest_revision,manifest_digest,execution_mode,series_id,campaign_cap,terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (CAMPAIGN_CODE,),
        ).fetchone()
    finally:
        connection.close()
    if campaigns != 2:
        raise ValueError("readback_campaign_count_mismatch")
    if r1_campaign is None:
        raise ValueError("readback_r1_campaign_missing")
    if r1_projected is None:
        raise ValueError("readback_r1_projection_missing")
    if tuple(r1_campaign) != tuple(r1_projected):
        raise ValueError("readback_r1_projection_mismatch")
    return {
        "code": "readback_ok",
        "fingerprint": inspection.fingerprint,
        "campaigns": campaigns,
        "campaign_attempts": mappings,
        "aggregate_live": aggregate,
        "r2_attempts": r2_attempts,
        "integrity_ok": True,
    }


def readback_r2_postmigration(root: Path) -> dict:
    """Strict postmigration gate: campaigns=2, mappings=1, aggregate=1, r2_attempts=0."""
    result = readback_r2(root)
    if result["campaign_attempts"] != 1:
        raise ValueError("readback_mapping_count_mismatch")
    if result["aggregate_live"] != 1:
        raise ValueError("readback_aggregate_mismatch")
    if result["r2_attempts"] != 0:
        raise ValueError("readback_r2_attempts_mismatch")
    return result


def _r2_paths(root: Path) -> tuple[Path, Path]:
    return root / "g-live-r2-migration-receipt.json", root / "g-live-r2-evidence.json"


def _write_r2_evidence(root: Path, value: dict) -> None:
    path = root / "g-live-r2-evidence.json"
    write_atomic_json(path, canonical_json(value), max_bytes=16384)


def reconcile_r2_high_water(root: Path) -> dict:
    receipt_path, evidence_path = _r2_paths(root)
    receipt, evidence = _safe_json(receipt_path), _safe_json(evidence_path)
    pinned_r1_hash = receipt.get("r1_evidence_sha256")
    actual_r1_hash = _r1_evidence_sha256(root)
    if actual_r1_hash != pinned_r1_hash:
        raise ValueError("r1_evidence_hash_mismatch")
    aggregate = FredMaterialStore(root).aggregate_live_count()
    prior = max(
        receipt.get("aggregate_live_high_water", -1),
        evidence.get("aggregate_live_high_water", -1),
    )
    if aggregate < prior:
        raise ValueError("aggregate_high_water_regression")
    high = max(aggregate, prior)
    current_hash = _database_digest(root / "fred.sqlite3")
    actual_store_hash = _store_evidence_sha256(root)
    if (
        evidence.get("aggregate_live_high_water") != high
        or evidence.get("latest_post_database_sha256") != current_hash
        or evidence.get("r1_evidence_sha256") != actual_r1_hash
        or evidence.get("store_evidence_sha256") != actual_store_hash
    ):
        evidence = dict(
            evidence,
            aggregate_live_high_water=high,
            aggregate_live_count=aggregate,
            latest_post_database_sha256=current_hash,
            r1_evidence_sha256=actual_r1_hash,
            store_evidence_sha256=actual_store_hash,
        )
        _write_r2_evidence(root, evidence)
    return evidence


def _preflight_r2_locked(root: Path, *, credential_path: Path | None = None) -> dict:
    """Noncreating immutable preflight under lock. Does not write evidence.

    Caller must already hold canonical_lock.
    """
    _root_metadata_preflight(root)
    readback = readback_r2(root)
    receipt_path, evidence_path = _r2_paths(root)
    receipt, evidence = _safe_json(receipt_path), _safe_json(evidence_path)
    pinned_r1_hash = receipt.get("r1_evidence_sha256")
    actual_r1_hash = _r1_evidence_sha256(root)
    if actual_r1_hash != pinned_r1_hash:
        raise ValueError("r1_evidence_hash_mismatch")
    backup = root / str(receipt.get("backup_basename", ""))
    current_hash = _database_digest(root / "fred.sqlite3")
    if readback["aggregate_live"] == 1:
        if current_hash != receipt.get("post_database_sha256"):
            raise ValueError("migration_artifact_conflict")
    else:
        if current_hash != evidence.get("latest_post_database_sha256"):
            if aggregate_is_ahead(root, evidence):
                raise ValueError("restart_reconciliation_required")
            raise ValueError("migration_artifact_conflict")
    if _safe_digest(backup) != receipt.get("backup_sha256") or evidence.get(
        "migration_receipt_sha256"
    ) != _safe_digest(receipt_path):
        raise ValueError("migration_artifact_conflict")
    if credential_path is not None:
        _credential_preflight(credential_path)
    return dict(readback, code="preflight_ok")


def preflight_r2(
    root: Path, *, lock_path: Path | None = None, credential_path: Path | None = None
) -> dict:
    """Noncreating immutable preflight. Does not write evidence.

    High-water reconciliation that may write belongs to the authorized
    post-reservation/restart action under lock, not to standalone preflight.
    Acquires canonical_lock when lock_path is supplied.
    """
    _safe_existing(root, directory=True, mode=0o700)
    if _r3_campaign_exists(root):
        return {"code": "r2_retired_by_r3"}
    if lock_path is None:
        return _preflight_r2_locked(root, credential_path=credential_path)
    _metadata_preflight(root, lock_path)
    with canonical_lock(lock_path):
        return _preflight_r2_locked(root, credential_path=credential_path)


def aggregate_is_ahead(root: Path, evidence: dict) -> bool:
    aggregate = FredMaterialStore(root).aggregate_live_count()
    prior = evidence.get("aggregate_live_high_water", -1)
    return aggregate > prior


def rollback_r2_schema(root: Path, *, lock_path: Path) -> dict:
    _metadata_preflight(root, lock_path)
    if _r3_campaign_exists(root):
        return {"code": "r2_retired_by_r3"}
    with canonical_lock(lock_path):
        return _rollback_r2_locked(root)


def _rollback_r2_locked(root: Path) -> dict:
    _root_metadata_preflight(root)  # re-verify under lock before any restore/write
    receipt_path, evidence_path = _r2_paths(root)
    receipt, evidence = _safe_json(receipt_path), _safe_json(evidence_path)
    readback = readback_r2(root)
    if (
        readback["r2_attempts"] != 0
        or readback["aggregate_live"] != 1
        or max(
            receipt.get("aggregate_live_high_water", -1),
            evidence.get("aggregate_live_high_water", -1),
        )
        != 1
    ):
        raise ValueError("rollback_forbidden")
    database = root / "fred.sqlite3"
    backup = root / str(receipt["backup_basename"])
    if (
        _safe_digest(backup) != receipt["backup_sha256"]
        or _database_digest(database) != receipt["post_database_sha256"]
    ):
        raise ValueError("migration_artifact_conflict")
    diagnostic = root / f"fred.sqlite3.failed-r2.{receipt['post_database_sha256']}.bak"
    if os.path.lexists(diagnostic):
        raise ValueError("migration_artifact_conflict")
    shutil.copyfile(database, diagnostic, follow_symlinks=False)
    os.chmod(diagnostic, 0o600)
    temporary = root / ".fred.sqlite3.rollback.tmp"
    if os.path.lexists(temporary):
        raise ValueError("migration_artifact_conflict")
    shutil.copyfile(backup, temporary, follow_symlinks=False)
    os.chmod(temporary, 0o600)
    os.replace(temporary, database)
    fsync_directory(root)
    if _database_digest(database) != receipt["pre_database_sha256"]:
        raise ValueError("rollback_verification_failure")
    evidence = dict(evidence, rollback_state="RESTORED_V3")
    _write_r2_evidence(root, evidence)
    return {"code": "rollback_ok", "from_user_version": 4, "to_user_version": 3}


class CountingOpener:
    def __init__(self, opener):
        self._opener = opener
        self.start_count = 0
        self._attempt_opened = True

    def begin_attempt(self):
        if not self._attempt_opened:
            raise ValueError("multiple_provider_starts")
        self._attempt_opened = False

    def open(self, request, *, timeout):
        if self._attempt_opened:
            raise ValueError("multiple_provider_starts")
        self._attempt_opened = True
        self.start_count += 1
        return self._opener.open(request, timeout=timeout)


def canonical_paths() -> tuple[Path, Path, Path]:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve(strict=True)
    environment = os.environ.get("HOME")
    if environment is not None and Path(environment).resolve(strict=True) != home:
        raise ValueError("home_mismatch")
    namespace = home / ".local" / "share" / "tokmo-v3" / "fred-canonical"
    return (
        namespace / "v1",
        Path(__file__).resolve().parents[1]
        / ".secrets"
        / "hermes-credentials"
        / "fred.env",
        namespace / ".g-live-v1.lock",
    )


def _safe_existing(path: Path, *, directory: bool, mode: int) -> None:
    value = os.lstat(path)
    expected = stat.S_ISDIR if directory else stat.S_ISREG
    if (
        stat.S_ISLNK(value.st_mode)
        or not expected(value.st_mode)
        or stat.S_IMODE(value.st_mode) != mode
        or (not directory and value.st_nlink != 1)
        or value.st_uid != os.getuid()
    ):
        raise ValueError("unsafe_private_path")


def _root_metadata_preflight(root: Path) -> None:
    """Re-verify noncreating metadata and sidecar absence for a root."""
    if os.path.lexists(root):
        _safe_existing(root, directory=True, mode=0o700)
        database = root / "fred.sqlite3"
        if os.path.lexists(database):
            _safe_existing(database, directory=False, mode=0o600)
        for evidence in (root / "store-evidence.json", root / "g-live-evidence.json"):
            if os.path.lexists(evidence):
                _safe_existing(evidence, directory=False, mode=0o600)
        for suffix in ("-journal", "-wal", "-shm"):
            if os.path.lexists(Path(f"{database}{suffix}")):
                raise ValueError("sqlite_sidecar_present")


def _metadata_preflight(root: Path, lock_path: Path) -> None:
    _root_metadata_preflight(root)
    if os.path.lexists(lock_path):
        _safe_existing(lock_path, directory=False, mode=0o600)


def _credential_preflight(credential_path: Path) -> None:
    _safe_existing(credential_path.parent, directory=True, mode=0o700)
    _safe_existing(credential_path, directory=False, mode=0o600)


@contextmanager
def canonical_lock(lock_path: Path):
    parent = lock_path.parent
    if not parent.exists():
        with private_creation():
            parent.mkdir(mode=0o700, parents=True)
        fsync_directory(parent.parent)
    _safe_existing(parent, directory=True, mode=0o700)
    flags = os.O_RDWR | os.O_NOFOLLOW
    if os.path.lexists(lock_path):
        descriptor = os.open(lock_path, flags)
    else:
        with private_creation():
            try:
                descriptor = os.open(lock_path, flags | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                descriptor = os.open(lock_path, flags)
        fsync_directory(parent)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or stat.S_IMODE(opened.st_mode) != 0o600
            or opened.st_nlink != 1
            or opened.st_uid != os.getuid()
        ):
            raise ValueError("unsafe_private_path")
        current = os.lstat(lock_path)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError("lock_not_acquired") from None
        yield
    finally:
        os.close(descriptor)


def _r1_evidence_sha256(root: Path) -> str | None:
    path = root / "g-live-evidence.json"
    if not os.path.lexists(path):
        return None
    return _safe_digest(path)


def _store_evidence_sha256(root: Path) -> str | None:
    path = root / "store-evidence.json"
    if not os.path.lexists(path):
        return None
    return _safe_digest(path)


def _r1_projection_digest(root: Path) -> dict:
    connection = connect_private(root / "fred.sqlite3", mode="ro")
    try:
        campaign = connection.execute(
            "SELECT source,purpose,campaign_code,code_revision,manifest_code,"
            "manifest_revision,manifest_digest,execution_mode,series_id,"
            "campaign_cap,terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
        attempt_count = connection.execute("SELECT count(*) FROM attempts").fetchone()[
            0
        ]
    finally:
        connection.close()
    payload = {"campaign": campaign, "attempt_count": attempt_count}
    return {
        "digest": hashlib.sha256(canonical_json(payload)).hexdigest(),
        "campaign_count": 1,
        "attempt_count": attempt_count,
    }


def _v3_fingerprint(root: Path) -> str:
    """Compute the actual V3 semantic fingerprint from the pre-migration backup."""
    from tokmo.records.fred_material_store import DDL_V3

    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(DDL_V3)
        semantics = _schema_semantics(connection)
    finally:
        connection.close()
    return hashlib.sha256(canonical_json(semantics)).hexdigest()


def _database_digest(path: Path) -> str:
    digest = hashlib.sha256()
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            digest.update(chunk)
    finally:
        os.close(descriptor)
    return digest.hexdigest()


def _safe_digest(path: Path) -> str:
    _safe_existing(path, directory=False, mode=0o600)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        before = os.lstat(path)
        opened = os.fstat(descriptor)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        digest = hashlib.sha256()
        while chunk := os.read(descriptor, 65536):
            digest.update(chunk)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        return digest.hexdigest()
    finally:
        os.close(descriptor)


def _safe_json(path: Path) -> dict:
    _safe_existing(path, directory=False, mode=0o600)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        before = os.lstat(path)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, 65536):
            size += len(chunk)
            if size > 16384:
                raise ValueError("evidence_invalid")
            chunks.append(chunk)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        value = json.loads(b"".join(chunks))
        if type(value) is not dict:
            raise ValueError("evidence_invalid")
        return value
    except json.JSONDecodeError:
        raise ValueError("evidence_invalid") from None
    finally:
        os.close(descriptor)


def _campaign_matches(
    store: FredMaterialStore, campaign_code: str = CAMPAIGN_CODE
) -> bool:
    connection = store._connect()
    try:
        if campaign_code == R2_CAMPAIGN_CODE:
            row = connection.execute(
                "SELECT source,purpose,campaign_code,code_revision,manifest_code,manifest_revision,manifest_digest,execution_mode,series_id,campaign_cap,terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
                (campaign_code,),
            ).fetchone()
            return row == (
                SOURCE,
                PURPOSE,
                R2_CAMPAIGN_CODE,
                R2_CODE_REVISION,
                R2_MANIFEST_CODE,
                R2_MANIFEST_REVISION,
                R2_MANIFEST_DIGEST,
                "LIVE",
                SERIES_ID,
                CAMPAIGN_CAP,
                "ACTIVE",
                None,
            )
        row = connection.execute(
            "SELECT source,purpose,campaign_code,code_revision,manifest_code,manifest_revision,manifest_digest,execution_mode,series_id,campaign_cap FROM campaign WHERE singleton=1"
        ).fetchone()
    finally:
        connection.close()
    return row == (
        "FRED",
        "CANONICAL_SOURCE_STAGE3",
        CAMPAIGN_CODE,
        CODE_REVISION,
        MANIFEST_CODE,
        MANIFEST_REVISION,
        MANIFEST_DIGEST,
        "LIVE",
        "PAYEMS",
        10,
    )


def _evidence(
    store: FredMaterialStore,
    operations: list[dict],
    opener_count: int,
    terminal_state: str,
    terminal_code: str | None,
    *,
    schema_fingerprint_ok: bool | None = None,
    controller_evidence_ok: bool = False,
) -> dict:
    if schema_fingerprint_ok is None:
        schema_fingerprint_ok = FredMaterialStore.inspect_existing(
            store.root
        ).schema_fingerprint_ok
    store_digest = None
    if store.evidence_path.exists():
        store_digest = _safe_digest(store.evidence_path)
    return {
        "schema": "tokmo.fred.g-live-evidence.r1",
        "campaign_code": CAMPAIGN_CODE,
        "code_revision": CODE_REVISION,
        "manifest_code": MANIFEST_CODE,
        "manifest_revision": MANIFEST_REVISION,
        "manifest_digest": MANIFEST_DIGEST,
        "terminal_state": terminal_state,
        "terminal_code": terminal_code,
        "operations": operations,
        "local_opener_start_count": opener_count,
        "counts": store.counts(),
        "integrity": {
            "schema_fingerprint_ok": schema_fingerprint_ok,
            "campaign_match": _campaign_matches(store),
            "store_evidence_ok": store.evidence_path.exists(),
            "controller_evidence_ok": controller_evidence_ok,
        },
        "store_evidence_sha256": store_digest,
        "database_sha256": _database_digest(store.path),
    }


def _write_evidence(store: FredMaterialStore, value: dict) -> None:
    path = store.root / "g-live-evidence.json"
    initial = canonical_json(value)
    write_atomic_json(path, initial, max_bytes=16384)
    _safe_existing(path, directory=False, mode=0o600)
    if _safe_digest(path) != hashlib.sha256(initial).hexdigest():
        raise ValueError("evidence_write_failure")
    verified = dict(value)
    verified["integrity"] = dict(value["integrity"], controller_evidence_ok=True)
    final = canonical_json(verified)
    write_atomic_json(path, final, max_bytes=16384)
    if _safe_digest(path) != hashlib.sha256(final).hexdigest():
        raise ValueError("evidence_write_failure")


def _completed_replay(store: FredMaterialStore) -> bool:
    connection = store._connect()
    try:
        attempts = connection.execute(
            "SELECT ordinal,operation,state,campaign_code,manifest_revision,manifest_digest FROM attempts WHERE execution_mode='LIVE' ORDER BY ordinal"
        ).fetchall()
        expected = [
            (
                index,
                request.operation.value,
                "SUCCEEDED",
                CAMPAIGN_CODE,
                MANIFEST_REVISION,
                MANIFEST_DIGEST,
            )
            for index, request in enumerate(g_live_manifest(release_id=1), 1)
        ]
        if attempts != expected:
            return False
        for ordinal in range(1, 6):
            joined = connection.execute(
                "SELECT count(*),count(DISTINCT q.material_identity),count(DISTINCT b.batch_identity),min(b.row_count),max(b.row_count) FROM attempts a JOIN acquisitions q ON q.invocation_identity=a.invocation_identity JOIN materials m ON m.material_identity=q.material_identity JOIN batches b ON b.invocation_identity=a.invocation_identity AND b.batch_identity=q.batch_identity WHERE a.ordinal=? AND a.state='SUCCEEDED' AND q.ordinal=a.ordinal AND q.execution_mode='LIVE' AND m.execution_mode='LIVE' AND b.ordinal=a.ordinal AND b.state='SUCCEEDED'",
                (ordinal,),
            ).fetchone()
            if (
                joined[0] < 1
                or joined[1] < 1
                or joined[2] != 1
                or joined[3] != joined[0]
                or joined[4] != joined[0]
            ):
                return False
    finally:
        connection.close()
    controller = store.root / "g-live-evidence.json"
    try:
        store_digest = _safe_digest(store.evidence_path)
        _safe_digest(controller)
        payload = _safe_json(controller)
        store_payload = _safe_json(store.evidence_path)
    except (OSError, ValueError):
        return False
    expected_operations = [
        {
            "ordinal": index,
            "name": request.operation.value,
            "evidence_class": EVIDENCE_CLASSES[index - 1],
            "outcome_code": "durable_success",
            "attempt_state": "SUCCEEDED",
        }
        for index, request in enumerate(g_live_manifest(release_id=1), 1)
    ]
    exact_keys = {
        "schema",
        "campaign_code",
        "code_revision",
        "manifest_code",
        "manifest_revision",
        "manifest_digest",
        "terminal_state",
        "terminal_code",
        "operations",
        "local_opener_start_count",
        "counts",
        "integrity",
        "store_evidence_sha256",
        "database_sha256",
    }
    return (
        set(payload) == exact_keys
        and payload.get("schema") == "tokmo.fred.g-live-evidence.r1"
        and payload.get("campaign_code") == CAMPAIGN_CODE
        and payload.get("manifest_revision") == MANIFEST_REVISION
        and payload.get("manifest_digest") == MANIFEST_DIGEST
        and payload.get("terminal_state") == "COMPLETE"
        and payload.get("terminal_code") == "manifest_complete"
        and payload.get("operations") == expected_operations
        and payload.get("local_opener_start_count") == 5
        and payload.get("counts") == store.counts()
        and payload.get("store_evidence_sha256") == store_digest
        and payload.get("database_sha256") == _database_digest(store.path)
        and store_payload.get("schema") == "tokmo.fred.store-evidence.r1"
        and store_payload.get("provenance") == "STORE_VERIFIED"
        and type(store_payload.get("last_batch")) is str
        and type(store_payload.get("material_count")) is int
        and store_payload.get("material_count") > 0
        and payload.get("integrity")
        == {
            "schema_fingerprint_ok": True,
            "campaign_match": True,
            "store_evidence_ok": True,
            "controller_evidence_ok": True,
        }
    )


def run_initial(
    *,
    root: Path,
    credential_path: Path,
    lock_path: Path,
    execution_allowed: bool,
    wall_clock,
    monotonic_clock,
    sleeper,
    api_key_loader=load_fred_api_key,
    opener_builder=build_live_opener,
) -> dict:
    if not execution_allowed:
        return {
            "code": "g_live_forbidden",
            "G_LIVE_ALLOWED": "NO",
            "provider_starts": 0,
            "credential_reads": 0,
        }
    counting = None
    key_reads = 0
    operations = []
    store = None
    schema_ok = False

    def counts() -> tuple[int, int]:
        return (0 if counting is None else counting.start_count, key_reads)

    def result(code: str) -> dict:
        starts, reads = counts()
        return {
            "code": code,
            "G_LIVE_ALLOWED": "NO",
            "provider_starts": starts,
            "credential_reads": reads,
        }

    def terminal(code: str, quarantine_code: str | None = None) -> dict:
        if store is None:
            return result(code)
        if quarantine_code is not None:
            try:
                store.quarantine(quarantine_code)
            except (OSError, ValueError):
                pass
        state = store.campaign_state()
        terminal_state = "QUARANTINED" if state is None else state[0]
        try:
            _write_evidence(
                store,
                _evidence(
                    store,
                    operations,
                    counts()[0],
                    terminal_state,
                    code,
                    schema_fingerprint_ok=schema_ok,
                ),
            )
        except (OSError, ValueError):
            try:
                store.quarantine("evidence_write_failure")
            except (OSError, ValueError):
                pass
            return result("evidence_write_failure")
        return result(code)

    try:
        _metadata_preflight(root, lock_path)
        lock_context = canonical_lock(lock_path)
        with lock_context:
            inspection = FredMaterialStore.inspect_existing(root)
            store = FredMaterialStore(root)
            verified = FredMaterialStore.inspect_existing(root)
            schema_ok = verified.schema_fingerprint_ok
            try:
                store.ensure_live_campaign()
            except ValueError as error:
                if str(error) == "campaign_mismatch":
                    return terminal("campaign_mismatch", "revision_mismatch")
                raise
            campaign_state = store.campaign_state()
            if campaign_state[0] == "QUARANTINED":
                return terminal("campaign_quarantined")
            acquired = wall_clock()
            if (
                type(acquired) is not datetime
                or acquired.tzinfo is None
                or acquired.utcoffset() is None
            ):
                raise ValueError("clock_failure")
            if store.reconcile_unknown(before=acquired):
                return terminal("stale_reservation", "stale_reservation")
            if _completed_replay(store):
                return result("already_complete")
            if campaign_state[0] == "COMPLETE":
                return terminal("replay_invalid", "replay_invalid")
            if store.counts()["attempts"]:
                return terminal("terminal_partial", "terminal_partial")
            _credential_preflight(credential_path)
            counting = CountingOpener(opener_builder())
            transport = FredLiveTransport(opener=counting)
            release_id = 1
            previous_wall = None
            previous_mono = None
            key_reads = 0
            for index in range(1, 6):
                request = g_live_manifest(release_id=release_id)[index - 1]
                now = wall_clock()
                mono = monotonic_clock()
                if (
                    type(now) is not datetime
                    or now.tzinfo is None
                    or now.utcoffset() is None
                    or type(mono) not in (int, float)
                ):
                    raise ValueError("clock_failure")
                if previous_wall is not None:
                    remaining = 1.0 - (now - previous_wall).total_seconds()
                    if remaining > 0:
                        sleeper(remaining)
                        now = wall_clock()
                        mono = monotonic_clock()
                    if (
                        type(now) is not datetime
                        or now.tzinfo is None
                        or now.utcoffset() is None
                        or type(mono) not in (int, float)
                        or (now - previous_wall).total_seconds() < 1.0
                        or mono < previous_mono
                    ):
                        raise ValueError("clock_failure")
                reservation = store.reserve(
                    request, execution_mode=ExecutionMode.LIVE, started_at=now
                )
                if reservation.code != "reserved":
                    raise ValueError(reservation.code)

                def provider():
                    nonlocal key_reads
                    key_reads += 1
                    return api_key_loader(credential_path)

                counting.begin_attempt()
                outcome = run_reserved_fred_operation(
                    root=root,
                    request=request,
                    reservation=reservation,
                    api_key_provider=provider,
                    transport=transport,
                    clock=wall_clock,
                )
                operations.append(
                    {
                        "ordinal": index,
                        "name": request.operation.value,
                        "evidence_class": EVIDENCE_CLASSES[index - 1],
                        "outcome_code": outcome.code,
                        "attempt_state": "SUCCEEDED"
                        if outcome.code
                        in ("durable_success", "durable_success_evidence_failure")
                        else "FAILED",
                    }
                )
                if outcome.code != "durable_success":
                    return terminal(outcome.code, outcome.code)
                if index == 3:
                    release_id = store.read_release_id_for_campaign(
                        mode="CURRENT_CONSTRUCTION",
                        source="FRED",
                        purpose="CANONICAL_SOURCE_STAGE3",
                        campaign_code=CAMPAIGN_CODE,
                        manifest_revision=MANIFEST_REVISION,
                        manifest_digest=MANIFEST_DIGEST,
                        invocation_identity=reservation.invocation_identity,
                        ordinal=3,
                        execution_mode=ExecutionMode.LIVE,
                        current_run_identity=reservation.invocation_identity,
                    )
                previous_wall, previous_mono = now, mono
            connection = store._connect()
            try:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "UPDATE campaign SET terminal_state='COMPLETE',terminal_code='manifest_complete' WHERE singleton=1 AND terminal_state='ACTIVE'"
                )
                connection.execute("COMMIT")
            finally:
                connection.close()
            try:
                _write_evidence(
                    store,
                    _evidence(
                        store,
                        operations,
                        counting.start_count,
                        "COMPLETE",
                        "manifest_complete",
                        schema_fingerprint_ok=schema_ok,
                    ),
                )
            except (OSError, ValueError):
                store.quarantine("durable_success_evidence_failure")
                return result("evidence_write_failure")
            return result("durable_success")
    except (OSError, ValueError) as error:
        code = str(error)
        allowed = {
            "unsafe_private_path",
            "unsafe_path_swap",
            "sqlite_sidecar_present",
            "lock_not_acquired",
            "clock_failure",
            "campaign_mismatch",
            "credential_unsafe",
            "schema_fingerprint_mismatch",
            "integrity_failure",
            "pacing_required",
            "provenance_mismatch",
            "cap_exhausted",
            "release_handoff_invalid",
            "settlement_conflict",
            "multiple_provider_starts",
        }
        allowed.update({"api_key_not_registered", "provider_bad_request_unknown"})
        static_code = code if code in allowed else "operation_failure"
        return terminal(static_code, static_code if store is not None else None)


def _completed_replay_r2(store: FredMaterialStore) -> bool:
    if store.campaign_state_for(R2_CAMPAIGN_CODE) != ("COMPLETE", "manifest_complete"):
        return False
    connection = store._connect()
    try:
        rows = connection.execute(
            "SELECT c.manifest_ordinal,a.operation,a.state FROM campaign_attempts c "
            "JOIN attempts a ON a.invocation_identity=c.invocation_identity "
            "WHERE c.campaign_code=? ORDER BY c.manifest_ordinal",
            (R2_CAMPAIGN_CODE,),
        ).fetchall()
    finally:
        connection.close()
    return rows == [
        (index, request.operation.value, "SUCCEEDED")
        for index, request in enumerate(g_live_manifest(release_id=1), 1)
    ]


def execute_r2(
    *,
    root: Path,
    credential_path: Path,
    lock_path: Path,
    execution_allowed: bool,
    wall_clock,
    monotonic_clock,
    sleeper,
    api_key_loader=load_fred_api_key,
    opener_builder=build_live_opener,
) -> dict:
    if not execution_allowed:
        return {
            "code": "g_live_forbidden",
            "provider_starts": 0,
            "credential_reads": 0,
            "LIVE_ALLOWED": "NO",
        }
    if _r3_campaign_exists(root):
        return {
            "code": "r2_retired_by_r3",
            "provider_starts": 0,
            "credential_reads": 0,
            "LIVE_ALLOWED": "NO",
        }
    starts = reads = 0
    counting: CountingOpener | None = None
    operations: list[dict] = []
    try:
        _metadata_preflight(root, lock_path)
        with canonical_lock(lock_path):
            restart_required = False
            try:
                _preflight_r2_locked(root, credential_path=credential_path)
            except ValueError as preflight_error:
                if str(preflight_error) != "restart_reconciliation_required":
                    raise
                restart_required = True
            reconcile_r2_high_water(root)
            store = FredMaterialStore(root)
            if _completed_replay_r2(store):
                return {
                    "code": "already_complete",
                    "provider_starts": 0,
                    "credential_reads": 0,
                    "LIVE_ALLOWED": "NO",
                }
            state = store.campaign_state_for(R2_CAMPAIGN_CODE)
            if state is None or state != ("ACTIVE", None):
                raise ValueError("campaign_mismatch")
            store.assert_mode_allowed(ExecutionMode.LIVE, R2_CAMPAIGN_CODE)
            now = wall_clock()
            if (
                type(now) is not datetime
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                raise ValueError("clock_failure")
            reconciled = store.reconcile_campaign_unknown(R2_CAMPAIGN_CODE, before=now)
            if reconciled:
                reconcile_r2_high_water(root)
                raise ValueError("stale_reservation")
            if restart_required:
                raise ValueError("restart_reconciliation_required")
            mapped = store.campaign_attempt_count(R2_CAMPAIGN_CODE)
            if mapped:
                raise ValueError("terminal_partial")
            counting = CountingOpener(opener_builder())
            transport = FredLiveTransport(opener=counting)
            release_id, previous_wall, previous_mono = 1, None, None
            for index in range(1, 6):
                request = g_live_manifest(release_id=release_id)[index - 1]
                now, mono = wall_clock(), monotonic_clock()
                if (
                    type(now) is not datetime
                    or now.tzinfo is None
                    or now.utcoffset() is None
                    or type(mono) not in (int, float)
                ):
                    raise ValueError("clock_failure")
                if previous_wall is not None:
                    remaining = 1.0 - (now - previous_wall).total_seconds()
                    if remaining > 0:
                        sleeper(remaining)
                        now, mono = wall_clock(), monotonic_clock()
                    if (
                        now - previous_wall
                    ).total_seconds() < 1.0 or mono < previous_mono:
                        raise ValueError("clock_failure")
                reservation = store.reserve_campaign(
                    R2_CAMPAIGN_CODE, index, request, started_at=now
                )
                if reservation.code != "reserved":
                    raise ValueError(reservation.code)
                reconcile_r2_high_water(root)

                def provider():
                    nonlocal reads
                    reads += 1
                    return api_key_loader(credential_path)

                counting.begin_attempt()
                outcome = run_reserved_fred_operation(
                    root=root,
                    request=request,
                    reservation=reservation,
                    api_key_provider=provider,
                    transport=transport,
                    clock=wall_clock,
                )
                starts = counting.start_count
                operations.append(
                    {
                        "manifest_ordinal": index,
                        "store_ordinal": reservation.ordinal,
                        "operation": request.operation.value,
                        "outcome_code": outcome.code,
                    }
                )
                evidence = reconcile_r2_high_water(root)
                _write_r2_evidence(
                    root,
                    dict(
                        evidence,
                        operations=operations,
                        terminal_state="ACTIVE",
                        terminal_code=None,
                    ),
                )
                if outcome.code != "durable_success":
                    store.quarantine_campaign(R2_CAMPAIGN_CODE, outcome.code)
                    evidence = reconcile_r2_high_water(root)
                    _write_r2_evidence(
                        root,
                        dict(
                            evidence,
                            operations=operations,
                            terminal_state="QUARANTINED",
                            terminal_code=outcome.code,
                        ),
                    )
                    return {
                        "code": outcome.code,
                        "provider_starts": starts,
                        "credential_reads": reads,
                        "LIVE_ALLOWED": "NO",
                    }
                if index == 3:
                    release_id = store.read_release_id_for_campaign(
                        mode="CURRENT_CONSTRUCTION",
                        source="FRED",
                        purpose="CANONICAL_SOURCE_STAGE3",
                        campaign_code=R2_CAMPAIGN_CODE,
                        manifest_revision=R2_MANIFEST_REVISION,
                        manifest_digest=R2_MANIFEST_DIGEST,
                        invocation_identity=reservation.invocation_identity,
                        ordinal=3,
                        execution_mode=ExecutionMode.LIVE,
                        current_run_identity=reservation.invocation_identity,
                    )
                previous_wall, previous_mono = now, mono
            store.complete_campaign(R2_CAMPAIGN_CODE)
            evidence = reconcile_r2_high_water(root)
            _write_r2_evidence(
                root,
                dict(
                    evidence,
                    operations=operations,
                    terminal_state="COMPLETE",
                    terminal_code="manifest_complete",
                ),
            )
            return {
                "code": "durable_success",
                "provider_starts": starts,
                "credential_reads": reads,
                "LIVE_ALLOWED": "NO",
            }
    except (OSError, ValueError) as error:
        if counting is not None:
            starts = counting.start_count
        static = _static_error_code(str(error))
        return {
            "code": static,
            "provider_starts": starts,
            "credential_reads": reads,
            "LIVE_ALLOWED": "NO",
        }


_STATIC_ERROR_CODES = frozenset(
    {
        "unsafe_private_path",
        "unsafe_path_swap",
        "sqlite_sidecar_present",
        "lock_not_acquired",
        "campaign_mismatch",
        "credential_unsafe",
        "schema_fingerprint_mismatch",
        "integrity_failure",
        "migration_artifact_conflict",
        "backup_hash_mismatch",
        "migration_no_change",
        "r1_campaign_missing",
        "schema_version_mismatch",
        "rollback_forbidden",
        "rollback_verification_failure",
        "readback_campaign_count_mismatch",
        "readback_mapping_count_mismatch",
        "readback_aggregate_mismatch",
        "readback_r2_attempts_mismatch",
        "readback_r1_campaign_missing",
        "readback_r1_projection_missing",
        "readback_r1_projection_mismatch",
        "aggregate_high_water_regression",
        "restart_reconciliation_required",
        "r1_evidence_hash_mismatch",
        "evidence_invalid",
        "evidence_write_failure",
        "clock_failure",
        "manifest_position_mismatch",
        "fixture_live_campaign_forbidden",
        "unsafe_nonregular",
        "unsafe_mode",
        "unsafe_owner",
        "unsafe_symlink",
        "unsafe_sidecar",
        "sqlite_pragma_mismatch",
        "home_mismatch",
        "live_campaign_required",
        "execution_mode_required",
        "campaign_cap_mismatch",
        "injected_root_required",
        "aware_utc_required",
        "g_live_forbidden",
        "terminal_partial",
        "stale_reservation",
        "credential_failure",
        "transport_failure",
        "response_contract_failure",
        "store_failure",
        "empty_page",
        "settlement_conflict",
        "durable_success_evidence_failure",
        "replay_invalid",
        "cap_exhausted",
        "pacing_required",
        "provenance_mismatch",
        "release_handoff_invalid",
        "multiple_provider_starts",
        "connection_failure",
        "timeout",
        "redirect",
        "auth_rejection",
        "bad_request",
        "http_429",
        "http_5xx",
        "body_too_large",
        "unsupported_content_encoding",
        "protocol_failure",
        "attempt_join_missing",
        "immutable_conflict",
        "invalid_settlement",
        "invalid_live_reservation",
        "operation_failure",
        "api_key_not_registered",
        "provider_bad_request_unknown",
        "already_complete",
        "already_attempted_fixture",
        "r2_retired_by_r3",
        "r3_retired_by_r4",
        "diagnostic_invalid",
        "diagnostic_count_mismatch",
        "diagnostic_schema_mismatch",
        "diagnostic_noncontiguous",
        "diagnostic_unsettled",
        "diagnostic_hash_mismatch",
        "readback_r1_state_mismatch",
        "readback_r2_state_mismatch",
        "readback_r3_missing",
        "readback_r3_state_mismatch",
        "readback_r4_missing",
        "readback_r4_state_mismatch",
        "operational_total_mismatch",
        "rca_invalid",
        "rca_count_mismatch",
        "rca_schema_mismatch",
        "rca_noncontiguous",
        "rca_unsettled",
        "rca_hash_mismatch",
    }
)


def _static_error_code(code: str) -> str:
    return code if code in _STATIC_ERROR_CODES else "operation_failure"


def _r3_campaign_exists(root: Path) -> bool:
    """Check whether the R3 campaign has been created in the DB."""
    database = root / "fred.sqlite3"
    if not database.exists():
        return False
    connection = connect_private(database, mode="ro")
    try:
        row = connection.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?", (R3_CAMPAIGN_CODE,)
        ).fetchone()
        return row is not None and row[0] > 0
    except sqlite3.Error:
        return False
    finally:
        connection.close()


def _r4_campaign_exists(root: Path) -> bool:
    """Check whether the R4 campaign has been created in the DB."""
    database = root / "fred.sqlite3"
    if not database.exists():
        return False
    connection = connect_private(database, mode="ro")
    try:
        row = connection.execute(
            "SELECT count(*) FROM campaigns WHERE campaign_code=?", (R4_CAMPAIGN_CODE,)
        ).fetchone()
        return row is not None and row[0] > 0
    except sqlite3.Error:
        return False
    finally:
        connection.close()


R3_DIAGNOSTIC_COUNT = 2
R3_DIAGNOSTIC_SHA256 = (
    "bf8844d1a639461ac8de3c2fe5f11f53ed9c611030d5def0d7f092734f2f6f4e"
)
R3_PREFLIGHT_DB_LIVE_COUNT = 2


def validate_diagnostic_ledger(
    path: Path,
    *,
    expected_count: int = R3_DIAGNOSTIC_COUNT,
    expected_sha256: str = R3_DIAGNOSTIC_SHA256,
) -> dict:
    """Validate the pinned diagnostic artifact before any DB write or credential read.

    Real schema: each probe contains sequence, name, started_at, outcome,
    http_status, category, provider_code.
    """
    _safe_existing(path, directory=False, mode=0o600)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        before = os.lstat(path)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, 65536):
            size += len(chunk)
            if size > 16384:
                raise ValueError("diagnostic_invalid")
            chunks.append(chunk)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        try:
            value = json.loads(b"".join(chunks))
        except json.JSONDecodeError:
            raise ValueError("diagnostic_invalid") from None
    finally:
        os.close(descriptor)
    if type(value) is not dict:
        raise ValueError("diagnostic_invalid")
    if value.get("schema") != "tokmo.fred.bounded-diagnostic.v1":
        raise ValueError("diagnostic_schema_mismatch")
    probes = value.get("probes")
    if type(probes) is not list:
        raise ValueError("diagnostic_invalid")
    if len(probes) != expected_count:
        raise ValueError("diagnostic_count_mismatch")
    allowed_names = frozenset({"minimal_series", "r2_realtime_date"})
    for index, probe in enumerate(probes):
        if type(probe) is not dict:
            raise ValueError("diagnostic_invalid")
        if probe.get("sequence") != index + 1:
            raise ValueError("diagnostic_noncontiguous")
        if probe.get("name") not in allowed_names:
            raise ValueError("diagnostic_invalid")
        if probe.get("outcome") != "settled":
            raise ValueError("diagnostic_unsettled")
        started_at = probe.get("started_at")
        if type(started_at) is not str:
            raise ValueError("diagnostic_invalid")
        try:
            parsed = datetime.fromisoformat(started_at)
        except ValueError:
            raise ValueError("diagnostic_invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("diagnostic_invalid")
        for key in ("http_status", "category", "provider_code"):
            if key not in probe:
                raise ValueError("diagnostic_invalid")
        if type(probe.get("http_status")) is not int:
            raise ValueError("diagnostic_invalid")
        if type(probe.get("category")) is not str:
            raise ValueError("diagnostic_invalid")
        provider_code = probe.get("provider_code")
        if provider_code is not None and type(provider_code) is not int:
            raise ValueError("diagnostic_invalid")
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    if digest != expected_sha256:
        raise ValueError("diagnostic_hash_mismatch")
    return {"count": len(probes), "sha256": digest}


def _r3_evidence(
    store: FredMaterialStore,
    operations: list[dict],
    opener_count: int,
    terminal_state: str,
    terminal_code: str | None,
    *,
    external_diagnostic_count: int,
    diagnostic_sha256: str,
    manifest_identity: dict,
    r1_evidence_sha256: str | None,
    r2_evidence_sha256: str | None,
    migration_receipt_sha256: str | None,
    schema_fingerprint_ok: bool | None = None,
) -> dict:
    if schema_fingerprint_ok is None:
        schema_fingerprint_ok = FredMaterialStore.inspect_existing(
            store.root
        ).schema_fingerprint_ok
    store_digest = None
    if store.evidence_path.exists():
        store_digest = _safe_digest(store.evidence_path)
    aggregate = store.aggregate_live_count()
    return {
        "schema": "tokmo.fred.g-live-r3-evidence.v1",
        "campaign_code": R3_CAMPAIGN_CODE,
        "code_revision": R3_CODE_REVISION,
        "manifest_code": R3_MANIFEST_CODE,
        "manifest_revision": R3_MANIFEST_REVISION,
        "manifest_digest": R3_MANIFEST_DIGEST,
        "terminal_state": terminal_state,
        "terminal_code": terminal_code,
        "operations": operations,
        "local_opener_start_count": opener_count,
        "counts": store.counts(),
        "aggregate_live_count": aggregate,
        "external_diagnostic_count": external_diagnostic_count,
        "operational_total": aggregate + external_diagnostic_count,
        "diagnostic_sha256": diagnostic_sha256,
        "manifest_identity": manifest_identity,
        "integrity": {
            "schema_fingerprint_ok": schema_fingerprint_ok,
            "r1_evidence_sha256": r1_evidence_sha256,
            "r2_evidence_sha256": r2_evidence_sha256,
            "migration_receipt_sha256": migration_receipt_sha256,
            "store_evidence_ok": store.evidence_path.exists(),
        },
        "store_evidence_sha256": store_digest,
        "database_sha256": _database_digest(store.path),
    }


def _write_r3_evidence(store: FredMaterialStore, value: dict) -> None:
    path = store.root / "g-live-r3-evidence.json"
    initial = canonical_json(value)
    write_atomic_json(path, initial, max_bytes=16384)
    _safe_existing(path, directory=False, mode=0o600)


def readback_r3(root: Path) -> dict:
    """R3 readback: exactly three campaigns, R1/R2 terminal states preserved, R3 valid.

    After valid R4 creation (4 campaigns), returns r3_retired_by_r4.
    """
    if _r4_campaign_exists(root):
        return {"code": "r3_retired_by_r4", "provider_starts": 0}
    inspection = FredMaterialStore.inspect_existing(root)
    if not inspection.schema_fingerprint_ok:
        raise ValueError("schema_fingerprint_mismatch")
    connection = connect_private(root / "fred.sqlite3", mode="ro")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("integrity_failure")
        campaigns = connection.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        r1_campaign = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
        r2_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R2_CAMPAIGN_CODE,),
        ).fetchone()
        r3_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R3_CAMPAIGN_CODE,),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT count(*) FROM attempts WHERE source=? AND purpose=? AND execution_mode='LIVE'",
            (SOURCE, PURPOSE),
        ).fetchone()[0]
        r3_attempts = connection.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (R3_CAMPAIGN_CODE,),
        ).fetchone()[0]
    finally:
        connection.close()
    if campaigns != 3:
        raise ValueError("readback_campaign_count_mismatch")
    if r1_campaign is None or r1_campaign != ("QUARANTINED", "bad_request"):
        raise ValueError("readback_r1_state_mismatch")
    if r2_state != ("QUARANTINED", "provider_bad_request_unknown"):
        raise ValueError("readback_r2_state_mismatch")
    if r3_state is None:
        raise ValueError("readback_r3_missing")
    if r3_state[0] not in ("ACTIVE", "QUARANTINED", "COMPLETE"):
        raise ValueError("readback_r3_state_mismatch")
    return {
        "code": "readback_ok",
        "fingerprint": inspection.fingerprint,
        "campaigns": campaigns,
        "aggregate_live": aggregate,
        "r3_attempts": r3_attempts,
        "r1_terminal_state": r1_campaign[0],
        "r1_terminal_code": r1_campaign[1],
        "r2_terminal_state": r2_state[0],
        "r2_terminal_code": r2_state[1],
        "r3_terminal_state": r3_state[0],
        "r3_terminal_code": r3_state[1],
        "integrity_ok": True,
    }


def preflight_r3(
    root: Path,
    *,
    lock_path: Path | None = None,
    diagnostic_path: Path | None = None,
    credential_path: Path | None = None,
) -> dict:
    """Noncreating immutable R3 preflight. Validates diagnostic ledger under lock."""
    _safe_existing(root, directory=True, mode=0o700)
    if lock_path is None:
        return _preflight_r3_locked(
            root, diagnostic_path=diagnostic_path, credential_path=credential_path
        )
    _metadata_preflight(root, lock_path)
    with canonical_lock(lock_path):
        return _preflight_r3_locked(
            root, diagnostic_path=diagnostic_path, credential_path=credential_path
        )


def _preflight_r3_locked(
    root: Path,
    *,
    diagnostic_path: Path | None = None,
    credential_path: Path | None = None,
) -> dict:
    _root_metadata_preflight(root)
    # Diagnostic ledger is always required for R3 preflight
    if diagnostic_path is None:
        raise ValueError("diagnostic_invalid")
    diagnostic = validate_diagnostic_ledger(diagnostic_path)
    # Validate R1/R2 evidence and receipt hashes
    r1_hash = _r1_evidence_sha256(root)
    r2_ev_hash = _r2_evidence_sha256(root)
    receipt_path = root / "g-live-r2-migration-receipt.json"
    receipt_hash = _safe_digest(receipt_path) if receipt_path.exists() else None
    if credential_path is not None:
        _credential_preflight(credential_path)
    r2_readback = readback_r2(root)
    if r2_readback.get("code") == "r2_retired_by_r3":
        readback = readback_r3(root)
        if readback.get("code") == "r3_retired_by_r4":
            return readback
    else:
        readback = r2_readback
    terminal_connection = connect_private(root / "fred.sqlite3", mode="ro")
    try:
        r1_terminal = terminal_connection.execute(
            "SELECT terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
        r2_terminal = terminal_connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R2_CAMPAIGN_CODE,),
        ).fetchone()
    finally:
        terminal_connection.close()
    if r1_terminal != ("QUARANTINED", "bad_request"):
        raise ValueError("readback_r1_state_mismatch")
    if r2_terminal != ("QUARANTINED", "provider_bad_request_unknown"):
        raise ValueError("readback_r2_state_mismatch")
    aggregate = readback.get("aggregate_live")
    if aggregate != R3_PREFLIGHT_DB_LIVE_COUNT:
        raise ValueError("readback_aggregate_mismatch")
    operational_total = aggregate + diagnostic["count"]
    if operational_total != R3_PREFLIGHT_DB_LIVE_COUNT + R3_DIAGNOSTIC_COUNT:
        raise ValueError("operational_total_mismatch")
    result = dict(
        readback,
        code="preflight_ok",
        diagnostic_count=diagnostic["count"],
        diagnostic_sha256=diagnostic["sha256"],
        operational_total=operational_total,
        r1_evidence_sha256=r1_hash,
        r2_evidence_sha256=r2_ev_hash,
        migration_receipt_sha256=receipt_hash,
    )
    return result


def _r2_evidence_sha256(root: Path) -> str | None:
    path = root / "g-live-r2-evidence.json"
    if not os.path.lexists(path):
        return None
    return _safe_digest(path)


def _completed_replay_r3(store: FredMaterialStore) -> bool:
    if store.campaign_state_for(R3_CAMPAIGN_CODE) != ("COMPLETE", "manifest_complete"):
        return False
    connection = store._connect()
    try:
        rows = connection.execute(
            "SELECT c.manifest_ordinal,a.operation,a.state FROM campaign_attempts c "
            "JOIN attempts a ON a.invocation_identity=c.invocation_identity "
            "WHERE c.campaign_code=? ORDER BY c.manifest_ordinal",
            (R3_CAMPAIGN_CODE,),
        ).fetchall()
    finally:
        connection.close()
    return rows == [
        (index, request.operation.value, "SUCCEEDED")
        for index, request in enumerate(r3_g_live_manifest(release_id=1), 1)
    ]


def execute_r3(
    *,
    root: Path,
    credential_path: Path,
    lock_path: Path,
    diagnostic_path: Path,
    execution_allowed: bool,
    wall_clock,
    monotonic_clock,
    sleeper,
    api_key_loader=load_fred_api_key,
    opener_builder=build_live_opener,
) -> dict:
    if not execution_allowed:
        return {
            "code": "g_live_forbidden",
            "provider_starts": 0,
            "credential_reads": 0,
            "LIVE_ALLOWED": "NO",
        }
    if _r4_campaign_exists(root):
        return {
            "code": "r3_retired_by_r4",
            "provider_starts": 0,
            "credential_reads": 0,
            "LIVE_ALLOWED": "NO",
        }
    starts = reads = 0
    counting: CountingOpener | None = None
    operations: list[dict] = []
    store: FredMaterialStore | None = None
    try:
        _metadata_preflight(root, lock_path)
        with canonical_lock(lock_path):
            # Validate diagnostic ledger under lock before anything
            diagnostic = validate_diagnostic_ledger(diagnostic_path)
            external_diagnostic_count = diagnostic["count"]
            diagnostic_sha256 = diagnostic["sha256"]
            store = FredMaterialStore(root)
            if _completed_replay_r3(store):
                return {
                    "code": "already_complete",
                    "provider_starts": 0,
                    "credential_reads": 0,
                    "LIVE_ALLOWED": "NO",
                }
            r3_state = store.campaign_state_for(R3_CAMPAIGN_CODE)
            if r3_state is not None and r3_state[0] == "QUARANTINED":
                raise ValueError("campaign_mismatch")
            if r3_state is not None and r3_state[0] == "COMPLETE":
                raise ValueError("replay_invalid")
            # Create R3 campaign under lock
            store.ensure_live_campaign(campaign_code=R3_CAMPAIGN_CODE)
            store.assert_mode_allowed(ExecutionMode.LIVE, R3_CAMPAIGN_CODE)
            now = wall_clock()
            if (
                type(now) is not datetime
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                raise ValueError("clock_failure")
            reconciled = store.reconcile_campaign_unknown(R3_CAMPAIGN_CODE, before=now)
            if reconciled:
                raise ValueError("stale_reservation")
            mapped = store.campaign_attempt_count(R3_CAMPAIGN_CODE)
            if mapped:
                raise ValueError("terminal_partial")
            counting = CountingOpener(opener_builder())
            transport = FredLiveTransport(opener=counting)
            release_id, previous_wall, previous_mono = 1, None, None
            for index in range(1, 6):
                request = r3_g_live_manifest(release_id=release_id)[index - 1]
                now, mono = wall_clock(), monotonic_clock()
                if (
                    type(now) is not datetime
                    or now.tzinfo is None
                    or now.utcoffset() is None
                    or type(mono) not in (int, float)
                ):
                    raise ValueError("clock_failure")
                if previous_wall is not None:
                    remaining = 1.0 - (now - previous_wall).total_seconds()
                    if remaining > 0:
                        sleeper(remaining)
                        now, mono = wall_clock(), monotonic_clock()
                    if (
                        now - previous_wall
                    ).total_seconds() < 1.0 or mono < previous_mono:
                        raise ValueError("clock_failure")
                reservation = store.reserve_campaign(
                    R3_CAMPAIGN_CODE,
                    index,
                    request,
                    started_at=now,
                    external_diagnostic_count=external_diagnostic_count,
                )
                if reservation.code != "reserved":
                    raise ValueError(reservation.code)

                def provider():
                    nonlocal reads
                    reads += 1
                    return api_key_loader(credential_path)

                counting.begin_attempt()
                outcome = run_reserved_fred_operation(
                    root=root,
                    request=request,
                    reservation=reservation,
                    api_key_provider=provider,
                    transport=transport,
                    clock=wall_clock,
                )
                starts = counting.start_count
                operations.append(
                    {
                        "manifest_ordinal": index,
                        "store_ordinal": reservation.ordinal,
                        "operation": request.operation.value,
                        "outcome_code": outcome.code,
                    }
                )
                if outcome.code != "durable_success":
                    store.quarantine_campaign(R3_CAMPAIGN_CODE, outcome.code)
                    _write_r3_evidence(
                        store,
                        _r3_evidence(
                            store,
                            operations,
                            starts,
                            "QUARANTINED",
                            outcome.code,
                            external_diagnostic_count=external_diagnostic_count,
                            diagnostic_sha256=diagnostic_sha256,
                            manifest_identity={
                                "campaign_code": R3_CAMPAIGN_CODE,
                                "manifest_code": R3_MANIFEST_CODE,
                            },
                            r1_evidence_sha256=_r1_evidence_sha256(root),
                            r2_evidence_sha256=_r2_evidence_sha256(root),
                            migration_receipt_sha256=_safe_digest(
                                root / "g-live-r2-migration-receipt.json"
                            )
                            if (root / "g-live-r2-migration-receipt.json").exists()
                            else None,
                        ),
                    )
                    return {
                        "code": outcome.code,
                        "provider_starts": starts,
                        "credential_reads": reads,
                        "LIVE_ALLOWED": "NO",
                    }
                if index == 3:
                    release_id = store.read_release_id_for_campaign(
                        mode="CURRENT_CONSTRUCTION",
                        source=SOURCE,
                        purpose=PURPOSE,
                        campaign_code=R3_CAMPAIGN_CODE,
                        manifest_revision=R3_MANIFEST_REVISION,
                        manifest_digest=R3_MANIFEST_DIGEST,
                        invocation_identity=reservation.invocation_identity,
                        ordinal=3,
                        execution_mode=ExecutionMode.LIVE,
                        current_run_identity=reservation.invocation_identity,
                    )
                previous_wall, previous_mono = now, mono
            store.complete_campaign(R3_CAMPAIGN_CODE)
            _write_r3_evidence(
                store,
                _r3_evidence(
                    store,
                    operations,
                    starts,
                    "COMPLETE",
                    "manifest_complete",
                    external_diagnostic_count=external_diagnostic_count,
                    diagnostic_sha256=diagnostic_sha256,
                    manifest_identity={
                        "campaign_code": R3_CAMPAIGN_CODE,
                        "manifest_code": R3_MANIFEST_CODE,
                    },
                    r1_evidence_sha256=_r1_evidence_sha256(root),
                    r2_evidence_sha256=_r2_evidence_sha256(root),
                    migration_receipt_sha256=_safe_digest(
                        root / "g-live-r2-migration-receipt.json"
                    )
                    if (root / "g-live-r2-migration-receipt.json").exists()
                    else None,
                ),
            )
            return {
                "code": "durable_success",
                "provider_starts": starts,
                "credential_reads": reads,
                "LIVE_ALLOWED": "NO",
            }
    except (OSError, ValueError, TypeError) as error:
        if counting is not None:
            starts = counting.start_count
        if isinstance(error, TypeError):
            static = "operation_failure"
        else:
            static = _static_error_code(str(error))
        return {
            "code": static,
            "provider_starts": starts,
            "credential_reads": reads,
            "LIVE_ALLOWED": "NO",
        }


# ---------------------------------------------------------------------------
# R4 RCA artifact validation (M1 repair)
# ---------------------------------------------------------------------------

R4_RCA_COUNT = 2
R4_RCA_SHA256 = "e24ea053975068779cdd69f3183f25a3808afc58e6e9c3501535a4a587ce3b23"

# SDS §3.3 per-position row contract. The rows are non-homogeneous: only row 1
# carries provider_message, and neither row carries provider_code.
_R4_RCA_ROW_KEYS = frozenset(
    {"sequence", "name", "started_at", "outcome", "http_status", "category"}
)
_R4_RCA_ROW_SPEC = (
    ("observations_output4_minimal", 400, "provider_error_json", True),
    ("observations_output4_broad_realtime", 200, "success", False),
)


def validate_rca_ledger(
    path: Path,
    *,
    expected_count: int = R4_RCA_COUNT,
    expected_sha256: str = R4_RCA_SHA256,
) -> dict:
    """Validate the pinned R4 RCA artifact before any DB write or credential read.

    Schema: tokmo.fred.r4-rca.v1 with top-level keys
    {schema, prior_operational_starts, ceiling, probes}.
    Row schema is non-homogeneous: row 1 has provider_message, row 2 omits it.
    Neither row carries provider_code. Each row's name/http_status/category and
    closed key set are pinned per position (SDS §3.3), so wrong row, status,
    category, or order reject on the contract alone rather than only via the
    whole-file hash. provider_message is checked for presence and type only; its
    value is never read into evidence, output, or exception text (SDS §3.4).
    """
    _safe_existing(path, directory=False, mode=0o600)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        opened = os.fstat(descriptor)
        before = os.lstat(path)
        if (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        chunks = []
        size = 0
        while chunk := os.read(descriptor, 65536):
            size += len(chunk)
            if size > 16384:
                raise ValueError("rca_invalid")
            chunks.append(chunk)
        after = os.lstat(path)
        if (after.st_dev, after.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("unsafe_path_swap")
        try:
            value = json.loads(b"".join(chunks))
        except json.JSONDecodeError:
            raise ValueError("rca_invalid") from None
    finally:
        os.close(descriptor)
    if type(value) is not dict:
        raise ValueError("rca_invalid")
    # Top-level closed key set
    expected_top = {"schema", "prior_operational_starts", "ceiling", "probes"}
    if set(value.keys()) != expected_top:
        raise ValueError("rca_invalid")
    if value.get("schema") != "tokmo.fred.r4-rca.v1":
        raise ValueError("rca_schema_mismatch")
    # Type-check ceiling and prior_operational_starts (value not used in cap)
    if type(value.get("ceiling")) is not int:
        raise ValueError("rca_invalid")
    if type(value.get("prior_operational_starts")) is not int:
        raise ValueError("rca_invalid")
    probes = value.get("probes")
    if type(probes) is not list:
        raise ValueError("rca_invalid")
    if len(probes) != expected_count:
        raise ValueError("rca_count_mismatch")
    for index, probe in enumerate(probes):
        if type(probe) is not dict:
            raise ValueError("rca_invalid")
        if index >= len(_R4_RCA_ROW_SPEC):
            raise ValueError("rca_count_mismatch")
        name, http_status, category, has_message = _R4_RCA_ROW_SPEC[index]
        if probe.get("sequence") != index + 1:
            raise ValueError("rca_noncontiguous")
        if probe.get("outcome") != "settled":
            raise ValueError("rca_unsettled")
        # Closed per-position key set: enforces the row-1-only provider_message
        # rule and the absence of provider_code on every row.
        expected_keys = _R4_RCA_ROW_KEYS | (
            {"provider_message"} if has_message else set()
        )
        if set(probe.keys()) != expected_keys:
            raise ValueError("rca_invalid")
        if probe.get("name") != name:
            raise ValueError("rca_invalid")
        started_at = probe.get("started_at")
        if type(started_at) is not str:
            raise ValueError("rca_invalid")
        try:
            parsed = datetime.fromisoformat(started_at)
        except ValueError:
            raise ValueError("rca_invalid") from None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            raise ValueError("rca_invalid")
        if (
            type(probe.get("http_status")) is not int
            or probe.get("http_status") != http_status
        ):
            raise ValueError("rca_invalid")
        if probe.get("category") != category:
            raise ValueError("rca_invalid")
        # Presence and type only; the message value is never read or propagated.
        if has_message and type(probe.get("provider_message")) is not str:
            raise ValueError("rca_invalid")
    digest = hashlib.sha256(b"".join(chunks)).hexdigest()
    if digest != expected_sha256:
        raise ValueError("rca_hash_mismatch")
    return {"count": len(probes), "sha256": digest}


# ---------------------------------------------------------------------------
# R4 evidence
# ---------------------------------------------------------------------------


def _r4_evidence(
    store: FredMaterialStore,
    operations: list[dict],
    opener_count: int,
    terminal_state: str,
    terminal_code: str | None,
    *,
    external_diagnostic_count: int,
    external_rca_count: int,
    diagnostic_sha256: str,
    rca_sha256: str,
    manifest_identity: dict,
    r1_evidence_sha256: str | None,
    r2_evidence_sha256: str | None,
    r3_evidence_sha256: str | None,
    migration_receipt_sha256: str | None,
    rca_sha256_value: str | None = None,
) -> dict:
    schema_fingerprint_ok = FredMaterialStore.inspect_existing(
        store.root
    ).schema_fingerprint_ok
    store_digest = None
    if store.evidence_path.exists():
        store_digest = _safe_digest(store.evidence_path)
    aggregate = store.aggregate_live_count()
    return {
        "schema": "tokmo.fred.g-live-r4-evidence.v1",
        "campaign_code": R4_CAMPAIGN_CODE,
        "code_revision": R4_CODE_REVISION,
        "manifest_code": R4_MANIFEST_CODE,
        "manifest_revision": R4_MANIFEST_REVISION,
        "manifest_digest": R4_MANIFEST_DIGEST,
        "terminal_state": terminal_state,
        "terminal_code": terminal_code,
        "operations": operations,
        "local_opener_start_count": opener_count,
        "counts": store.counts(),
        "aggregate_live_count": aggregate,
        "external_diagnostic_count": external_diagnostic_count,
        "external_rca_count": external_rca_count,
        "operational_total": aggregate + external_diagnostic_count + external_rca_count,
        "diagnostic_sha256": diagnostic_sha256,
        "rca_sha256": rca_sha256_value or rca_sha256,
        "manifest_identity": manifest_identity,
        "integrity": {
            "schema_fingerprint_ok": schema_fingerprint_ok,
            "r1_evidence_sha256": r1_evidence_sha256,
            "r2_evidence_sha256": r2_evidence_sha256,
            "r3_evidence_sha256": r3_evidence_sha256,
            "migration_receipt_sha256": migration_receipt_sha256,
            "store_evidence_ok": store.evidence_path.exists(),
        },
        "store_evidence_sha256": store_digest,
        "database_sha256": _database_digest(store.path),
    }


def _write_r4_evidence(store: FredMaterialStore, value: dict) -> None:
    path = store.root / "g-live-r4-evidence.json"
    write_atomic_json(path, canonical_json(value), max_bytes=16384)
    _safe_existing(path, directory=False, mode=0o600)


def _r3_evidence_sha256(root: Path) -> str | None:
    path = root / "g-live-r3-evidence.json"
    if not os.path.lexists(path):
        return None
    return _safe_digest(path)


# ---------------------------------------------------------------------------
# R4 readback, preflight
# ---------------------------------------------------------------------------


def readback_r4(root: Path) -> dict:
    """R4 readback: exactly four campaigns, R1/R2/R3 terminal states preserved."""
    inspection = FredMaterialStore.inspect_existing(root)
    if not inspection.schema_fingerprint_ok:
        raise ValueError("schema_fingerprint_mismatch")
    connection = connect_private(root / "fred.sqlite3", mode="ro")
    try:
        integrity = connection.execute("PRAGMA integrity_check").fetchall()
        if integrity != [("ok",)]:
            raise ValueError("integrity_failure")
        campaigns = connection.execute("SELECT count(*) FROM campaigns").fetchone()[0]
        r1_campaign = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaign WHERE singleton=1"
        ).fetchone()
        r2_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R2_CAMPAIGN_CODE,),
        ).fetchone()
        r3_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R3_CAMPAIGN_CODE,),
        ).fetchone()
        r4_state = connection.execute(
            "SELECT terminal_state,terminal_code FROM campaigns WHERE campaign_code=?",
            (R4_CAMPAIGN_CODE,),
        ).fetchone()
        aggregate = connection.execute(
            "SELECT count(*) FROM attempts WHERE source=? AND purpose=? AND execution_mode='LIVE'",
            (SOURCE, PURPOSE),
        ).fetchone()[0]
        r4_attempts = connection.execute(
            "SELECT count(*) FROM campaign_attempts WHERE campaign_code=?",
            (R4_CAMPAIGN_CODE,),
        ).fetchone()[0]
    finally:
        connection.close()
    if campaigns != 4:
        raise ValueError("readback_campaign_count_mismatch")
    if r1_campaign is None or r1_campaign != ("QUARANTINED", "bad_request"):
        raise ValueError("readback_r1_state_mismatch")
    if r2_state != ("QUARANTINED", "provider_bad_request_unknown"):
        raise ValueError("readback_r2_state_mismatch")
    if r3_state is None:
        raise ValueError("readback_r3_missing")
    if r3_state[0] not in ("ACTIVE", "QUARANTINED", "COMPLETE"):
        raise ValueError("readback_r3_state_mismatch")
    if r4_state is None:
        raise ValueError("readback_r4_missing")
    if r4_state[0] not in ("ACTIVE", "QUARANTINED", "COMPLETE"):
        raise ValueError("readback_r4_state_mismatch")
    return {
        "code": "readback_ok",
        "fingerprint": inspection.fingerprint,
        "campaigns": campaigns,
        "aggregate_live": aggregate,
        "r4_attempts": r4_attempts,
        "r1_terminal_state": r1_campaign[0],
        "r1_terminal_code": r1_campaign[1],
        "r2_terminal_state": r2_state[0],
        "r2_terminal_code": r2_state[1],
        "r3_terminal_state": r3_state[0],
        "r3_terminal_code": r3_state[1],
        "r4_terminal_state": r4_state[0],
        "r4_terminal_code": r4_state[1],
        "integrity_ok": True,
    }


def preflight_r4(
    root: Path,
    *,
    lock_path: Path | None = None,
    diagnostic_path: Path | None = None,
    rca_path: Path | None = None,
    credential_path: Path | None = None,
) -> dict:
    """Noncreating immutable R4 preflight. Validates diagnostic and RCA ledgers."""
    _safe_existing(root, directory=True, mode=0o700)
    if lock_path is None:
        return _preflight_r4_locked(
            root,
            diagnostic_path=diagnostic_path,
            rca_path=rca_path,
            credential_path=credential_path,
        )
    _metadata_preflight(root, lock_path)
    with canonical_lock(lock_path):
        return _preflight_r4_locked(
            root,
            diagnostic_path=diagnostic_path,
            rca_path=rca_path,
            credential_path=credential_path,
        )


def _preflight_r4_locked(
    root: Path,
    *,
    diagnostic_path: Path | None = None,
    rca_path: Path | None = None,
    credential_path: Path | None = None,
) -> dict:
    _root_metadata_preflight(root)
    if diagnostic_path is None:
        raise ValueError("diagnostic_invalid")
    if rca_path is None:
        raise ValueError("rca_invalid")
    diagnostic = validate_diagnostic_ledger(diagnostic_path)
    rca = validate_rca_ledger(rca_path)
    r1_hash = _r1_evidence_sha256(root)
    r2_ev_hash = _r2_evidence_sha256(root)
    r3_ev_hash = _r3_evidence_sha256(root)
    receipt_path = root / "g-live-r2-migration-receipt.json"
    receipt_hash = _safe_digest(receipt_path) if receipt_path.exists() else None
    if credential_path is not None:
        _credential_preflight(credential_path)
    readback = readback_r3(root)
    if readback.get("code") == "r3_retired_by_r4":
        readback = readback_r4(root)
    aggregate = readback.get("aggregate_live")
    operational_total = aggregate + diagnostic["count"] + rca["count"]
    result = dict(
        readback,
        code="preflight_ok",
        diagnostic_count=diagnostic["count"],
        diagnostic_sha256=diagnostic["sha256"],
        rca_count=rca["count"],
        rca_sha256=rca["sha256"],
        operational_total=operational_total,
        purpose_cumulative_ceiling=PURPOSE_CUMULATIVE_CEILING,
        r1_evidence_sha256=r1_hash,
        r2_evidence_sha256=r2_ev_hash,
        r3_evidence_sha256=r3_ev_hash,
        migration_receipt_sha256=receipt_hash,
    )
    return result


# ---------------------------------------------------------------------------
# R4 execute
# ---------------------------------------------------------------------------


def _completed_replay_r4(store: FredMaterialStore) -> bool:
    if store.campaign_state_for(R4_CAMPAIGN_CODE) != ("COMPLETE", "manifest_complete"):
        return False
    connection = store._connect()
    try:
        rows = connection.execute(
            "SELECT c.manifest_ordinal,a.operation,a.state FROM campaign_attempts c "
            "JOIN attempts a ON a.invocation_identity=c.invocation_identity "
            "WHERE c.campaign_code=? ORDER BY c.manifest_ordinal",
            (R4_CAMPAIGN_CODE,),
        ).fetchall()
    finally:
        connection.close()
    return rows == [
        (index, request.operation.value, "SUCCEEDED")
        for index, request in enumerate(r4_g_live_manifest(release_id=1), 1)
    ]


def execute_r4(
    *,
    root: Path,
    credential_path: Path,
    lock_path: Path,
    diagnostic_path: Path,
    rca_path: Path,
    execution_allowed: bool,
    wall_clock,
    monotonic_clock,
    sleeper,
    api_key_loader=load_fred_api_key,
    opener_builder=build_live_opener,
) -> dict:
    if not execution_allowed:
        return {
            "code": "g_live_forbidden",
            "provider_starts": 0,
            "credential_reads": 0,
            "LIVE_ALLOWED": "NO",
        }
    starts = reads = 0
    counting: CountingOpener | None = None
    operations: list[dict] = []
    store: FredMaterialStore | None = None
    try:
        _metadata_preflight(root, lock_path)
        with canonical_lock(lock_path):
            diagnostic = validate_diagnostic_ledger(diagnostic_path)
            external_diagnostic_count = diagnostic["count"]
            diagnostic_sha256 = diagnostic["sha256"]
            rca = validate_rca_ledger(rca_path)
            external_rca_count = rca["count"]
            rca_sha256 = rca["sha256"]
            store = FredMaterialStore(root)
            if _completed_replay_r4(store):
                return {
                    "code": "already_complete",
                    "provider_starts": 0,
                    "credential_reads": 0,
                    "LIVE_ALLOWED": "NO",
                }
            r4_state = store.campaign_state_for(R4_CAMPAIGN_CODE)
            if r4_state is not None and r4_state[0] == "QUARANTINED":
                raise ValueError("campaign_mismatch")
            if r4_state is not None and r4_state[0] == "COMPLETE":
                raise ValueError("replay_invalid")
            store.ensure_live_campaign(campaign_code=R4_CAMPAIGN_CODE)
            store.assert_mode_allowed(ExecutionMode.LIVE, R4_CAMPAIGN_CODE)
            now = wall_clock()
            if (
                type(now) is not datetime
                or now.tzinfo is None
                or now.utcoffset() is None
            ):
                raise ValueError("clock_failure")
            reconciled = store.reconcile_campaign_unknown(R4_CAMPAIGN_CODE, before=now)
            if reconciled:
                raise ValueError("stale_reservation")
            mapped = store.campaign_attempt_count(R4_CAMPAIGN_CODE)
            if mapped:
                raise ValueError("terminal_partial")
            counting = CountingOpener(opener_builder())
            transport = FredLiveTransport(opener=counting)
            release_id, previous_wall, previous_mono = 1, None, None
            for index in range(1, 6):
                request = r4_g_live_manifest(release_id=release_id)[index - 1]
                now, mono = wall_clock(), monotonic_clock()
                if (
                    type(now) is not datetime
                    or now.tzinfo is None
                    or now.utcoffset() is None
                    or type(mono) not in (int, float)
                ):
                    raise ValueError("clock_failure")
                if previous_wall is not None:
                    remaining = 1.0 - (now - previous_wall).total_seconds()
                    if remaining > 0:
                        sleeper(remaining)
                        now, mono = wall_clock(), monotonic_clock()
                    if (
                        now - previous_wall
                    ).total_seconds() < 1.0 or mono < previous_mono:
                        raise ValueError("clock_failure")
                reservation = store.reserve_campaign(
                    R4_CAMPAIGN_CODE,
                    index,
                    request,
                    started_at=now,
                    external_diagnostic_count=external_diagnostic_count,
                    external_rca_count=external_rca_count,
                )
                if reservation.code != "reserved":
                    raise ValueError(reservation.code)

                def provider():
                    nonlocal reads
                    reads += 1
                    return api_key_loader(credential_path)

                counting.begin_attempt()
                outcome = run_reserved_fred_operation(
                    root=root,
                    request=request,
                    reservation=reservation,
                    api_key_provider=provider,
                    transport=transport,
                    clock=wall_clock,
                )
                starts = counting.start_count
                operations.append(
                    {
                        "manifest_ordinal": index,
                        "store_ordinal": reservation.ordinal,
                        "operation": request.operation.value,
                        "outcome_code": outcome.code,
                    }
                )
                if outcome.code != "durable_success":
                    store.quarantine_campaign(R4_CAMPAIGN_CODE, outcome.code)
                    _write_r4_evidence(
                        store,
                        _r4_evidence(
                            store,
                            operations,
                            starts,
                            "QUARANTINED",
                            outcome.code,
                            external_diagnostic_count=external_diagnostic_count,
                            external_rca_count=external_rca_count,
                            diagnostic_sha256=diagnostic_sha256,
                            rca_sha256=rca_sha256,
                            manifest_identity={
                                "campaign_code": R4_CAMPAIGN_CODE,
                                "manifest_code": R4_MANIFEST_CODE,
                            },
                            r1_evidence_sha256=_r1_evidence_sha256(root),
                            r2_evidence_sha256=_r2_evidence_sha256(root),
                            r3_evidence_sha256=_r3_evidence_sha256(root),
                            migration_receipt_sha256=_safe_digest(
                                root / "g-live-r2-migration-receipt.json"
                            )
                            if (root / "g-live-r2-migration-receipt.json").exists()
                            else None,
                            rca_sha256_value=rca_sha256,
                        ),
                    )
                    return {
                        "code": outcome.code,
                        "provider_starts": starts,
                        "credential_reads": reads,
                        "LIVE_ALLOWED": "NO",
                    }
                if index == 3:
                    release_id = store.read_release_id_for_campaign(
                        mode="CURRENT_CONSTRUCTION",
                        source=SOURCE,
                        purpose=PURPOSE,
                        campaign_code=R4_CAMPAIGN_CODE,
                        manifest_revision=R4_MANIFEST_REVISION,
                        manifest_digest=R4_MANIFEST_DIGEST,
                        invocation_identity=reservation.invocation_identity,
                        ordinal=3,
                        execution_mode=ExecutionMode.LIVE,
                        current_run_identity=reservation.invocation_identity,
                    )
                previous_wall, previous_mono = now, mono
            store.complete_campaign(R4_CAMPAIGN_CODE)
            _write_r4_evidence(
                store,
                _r4_evidence(
                    store,
                    operations,
                    starts,
                    "COMPLETE",
                    "manifest_complete",
                    external_diagnostic_count=external_diagnostic_count,
                    external_rca_count=external_rca_count,
                    diagnostic_sha256=diagnostic_sha256,
                    rca_sha256=rca_sha256,
                    manifest_identity={
                        "campaign_code": R4_CAMPAIGN_CODE,
                        "manifest_code": R4_MANIFEST_CODE,
                    },
                    r1_evidence_sha256=_r1_evidence_sha256(root),
                    r2_evidence_sha256=_r2_evidence_sha256(root),
                    r3_evidence_sha256=_r3_evidence_sha256(root),
                    migration_receipt_sha256=_safe_digest(
                        root / "g-live-r2-migration-receipt.json"
                    )
                    if (root / "g-live-r2-migration-receipt.json").exists()
                    else None,
                    rca_sha256_value=rca_sha256,
                ),
            )
            return {
                "code": "durable_success",
                "provider_starts": starts,
                "credential_reads": reads,
                "LIVE_ALLOWED": "NO",
            }
    except (OSError, ValueError, TypeError) as error:
        if counting is not None:
            starts = counting.start_count
        if isinstance(error, TypeError):
            static = "operation_failure"
        else:
            static = _static_error_code(str(error))
        return {
            "code": static,
            "provider_starts": starts,
            "credential_reads": reads,
            "LIVE_ALLOWED": "NO",
        }


def _cli_wrap(action: str, fn) -> dict:
    try:
        return fn()
    except (OSError, ValueError, TypeError) as error:
        if isinstance(error, TypeError):
            return {"code": "operation_failure"}
        return {"code": _static_error_code(str(error))}


def run_cli(argv, **dependencies) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "action",
        choices=(
            "initial",
            "preflight-r2",
            "migrate-r2-schema",
            "readback-r2",
            "rollback-r2-schema",
            "execute-r2",
            "preflight-r3",
            "readback-r3",
            "execute-r3",
            "preflight-r4",
            "readback-r4",
            "execute-r4",
        ),
    )
    parser.add_argument(
        "--g-live-r2-execute",
        action="store_true",
        help="explicit closed authorization flag for execute-r2 "
        "(retired after R3; use execute-r3 instead)",
    )
    parser.add_argument(
        "--g-live-r3-execute",
        action="store_true",
        help="explicit closed authorization flag for execute-r3 "
        "(retired after R4; use execute-r4 instead)",
    )
    parser.add_argument(
        "--g-live-r4-execute",
        action="store_true",
        help="explicit closed authorization flag for execute-r4",
    )
    parser.add_argument(
        "--diagnostic-path",
        default=None,
        help="path to pinned diagnostic ledger for R3/R4 operations",
    )
    parser.add_argument(
        "--rca-path",
        default=None,
        help="path to pinned R4 RCA ledger for R4 operations",
    )
    args = parser.parse_args(argv)
    canonical_root, canonical_credential, canonical_lock_path = canonical_paths()
    root = dependencies.pop("root", canonical_root)
    credential = dependencies.pop("credential_path", canonical_credential)
    lock_path = dependencies.pop("lock_path", canonical_lock_path)
    diagnostic_path = dependencies.pop(
        "diagnostic_path",
        Path(args.diagnostic_path)
        if args.diagnostic_path
        else root / "g-live-diagnostic-probes.json",
    )
    rca_path = dependencies.pop(
        "rca_path",
        Path(args.rca_path) if args.rca_path else root / "g-live-r4-rca-probes.json",
    )
    if args.action == "initial":
        result = _cli_wrap(
            "initial",
            lambda: run_initial(
                root=root,
                credential_path=credential,
                lock_path=lock_path,
                execution_allowed=dependencies.pop("execution_allowed", True),
                wall_clock=dependencies.pop("wall_clock", lambda: datetime.now(UTC)),
                monotonic_clock=dependencies.pop("monotonic_clock", time.monotonic),
                sleeper=dependencies.pop("sleeper", time.sleep),
                **dependencies,
            ),
        )
    elif args.action == "preflight-r2":
        result = _cli_wrap(
            "preflight-r2",
            lambda: preflight_r2(root, lock_path=lock_path, credential_path=credential),
        )
    elif args.action == "migrate-r2-schema":
        result = _cli_wrap(
            "migrate-r2-schema",
            lambda: migrate_r2_schema(
                root,
                lock_path=lock_path,
                migrated_at=dependencies.pop("migrated_at", datetime.now(UTC)),
            ),
        )
    elif args.action == "readback-r2":
        result = _cli_wrap("readback-r2", lambda: readback_r2(root))
    elif args.action == "rollback-r2-schema":
        result = _cli_wrap(
            "rollback-r2-schema", lambda: rollback_r2_schema(root, lock_path=lock_path)
        )
    elif args.action == "preflight-r3":
        result = _cli_wrap(
            "preflight-r3",
            lambda: preflight_r3(
                root,
                lock_path=lock_path,
                diagnostic_path=diagnostic_path,
                credential_path=credential,
            ),
        )
    elif args.action == "readback-r3":
        result = _cli_wrap("readback-r3", lambda: readback_r3(root))
    elif args.action == "execute-r3":
        execute_flag = args.g_live_r3_execute or dependencies.pop(
            "execution_allowed", False
        )
        result = _cli_wrap(
            "execute-r3",
            lambda: execute_r3(
                root=root,
                credential_path=credential,
                lock_path=lock_path,
                diagnostic_path=diagnostic_path,
                execution_allowed=execute_flag,
                wall_clock=dependencies.pop("wall_clock", lambda: datetime.now(UTC)),
                monotonic_clock=dependencies.pop("monotonic_clock", time.monotonic),
                sleeper=dependencies.pop("sleeper", time.sleep),
                **dependencies,
            ),
        )
    elif args.action == "preflight-r4":
        result = _cli_wrap(
            "preflight-r4",
            lambda: preflight_r4(
                root,
                lock_path=lock_path,
                diagnostic_path=diagnostic_path,
                rca_path=rca_path,
                credential_path=credential,
            ),
        )
    elif args.action == "readback-r4":
        result = _cli_wrap("readback-r4", lambda: readback_r4(root))
    elif args.action == "execute-r4":
        execute_flag = args.g_live_r4_execute or dependencies.pop(
            "execution_allowed", False
        )
        result = _cli_wrap(
            "execute-r4",
            lambda: execute_r4(
                root=root,
                credential_path=credential,
                lock_path=lock_path,
                diagnostic_path=diagnostic_path,
                rca_path=rca_path,
                execution_allowed=execute_flag,
                wall_clock=dependencies.pop("wall_clock", lambda: datetime.now(UTC)),
                monotonic_clock=dependencies.pop("monotonic_clock", time.monotonic),
                sleeper=dependencies.pop("sleeper", time.sleep),
                **dependencies,
            ),
        )
    else:
        execute_flag = args.g_live_r2_execute or dependencies.pop(
            "execution_allowed", False
        )
        result = _cli_wrap(
            "execute-r2",
            lambda: execute_r2(
                root=root,
                credential_path=credential,
                lock_path=lock_path,
                execution_allowed=execute_flag,
                wall_clock=dependencies.pop("wall_clock", lambda: datetime.now(UTC)),
                monotonic_clock=dependencies.pop("monotonic_clock", time.monotonic),
                sleeper=dependencies.pop("sleeper", time.sleep),
                **dependencies,
            ),
        )
    print(json.dumps(result, sort_keys=True))
    return (
        0
        if result.get("code")
        in (
            "durable_success",
            "already_complete",
            "preflight_ok",
            "readback_ok",
            "rollback_ok",
            "r2_retired_by_r3",
            "r3_retired_by_r4",
        )
        or args.action == "migrate-r2-schema"
        else 1
    )


def main() -> int:
    return run_cli(sys.argv[1:])


if __name__ == "__main__":
    raise SystemExit(main())
