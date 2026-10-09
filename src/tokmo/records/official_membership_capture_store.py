"""Canonical private store for material-zero SIC documentation captures."""

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

from tokmo.records.canonical_observation_registry import (
    OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY,
    require_registered_family,
)
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

_DDL = "CREATE TABLE captures(capture_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, status INTEGER NOT NULL CHECK(status=200), body_byte_count INTEGER NOT NULL CHECK(body_byte_count>0), body BLOB NOT NULL, body_sha256 TEXT NOT NULL CHECK(length(body_sha256)=64)) STRICT"


@dataclass(frozen=True, slots=True)
class DocumentationCaptureV1:
    capture_id: str
    captured_at: datetime
    status: int
    body_byte_count: int
    body: bytes
    body_sha256: str


class OfficialMembershipCaptureStore:
    def __init__(
        self,
        path: Path,
        *,
        family_name=OFFICIAL_MEMBERSHIP_DOCUMENTATION_CAPTURE_FAMILY,
    ):
        require_registered_family(family_name, adapter_type=type(self).__name__)
        self.path = Path(path)

    def _open(self, *, readonly=False):
        if (
            sqlite3.sqlite_version_info < MIN_SQLITE_VERSION
            or prepare_private_path(self.path).unsafe
            or (self.path.exists() and os.lstat(self.path).st_nlink != 1)
        ):
            raise ValueError("private_path")
        new = not os.path.lexists(self.path)
        try:
            if new:
                with private_creation():
                    c = connect_private(self.path)
                os.chmod(self.path, 0o600)
                c.execute(_DDL)
                c.execute("PRAGMA user_version=1")
                assert_private_sidecars_while_open(self.path)
                fsync_directory(self.path.parent)
            else:
                c = connect_private(self.path, mode="ro" if readonly else "rwc")
            if (
                c.execute("PRAGMA user_version").fetchone() != (1,)
                or not table_is_strict(c, "captures")
                or c.execute(
                    "SELECT sql FROM sqlite_master WHERE name='captures'"
                ).fetchone()
                != (_DDL,)
            ):
                raise ValueError("schema_mismatch")
            return c
        except BaseException:
            if "c" in locals():
                c.close()
            raise

    def _close(self, c, *, write=False):
        c.close()
        if write:
            fsync_directory(self.path.parent)
        if assert_no_sidecars_after_close(self.path):
            raise ValueError("sqlite_sidecar_present")

    def append_success(
        self,
        *,
        body: bytes,
        captured_at: datetime,
        status: int = 200,
        capture_id: str | None = None,
    ):
        if (
            type(body) is not bytes
            or not body
            or captured_at.tzinfo is None
            or status != 200
        ):
            raise ValueError("capture")
        digest = sha256(body).hexdigest()
        capture_id = capture_id or "sic-" + digest
        desired = (captured_at.isoformat(), status, len(body), body, digest)
        c = self._open()
        try:
            acquire_immediate_transaction(c)
            existing = c.execute(
                "SELECT captured_at,status,body_byte_count,body,body_sha256 FROM captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
            if existing is not None:
                c.execute("ROLLBACK")
                if existing != desired:
                    raise ValueError("capture_conflict")
            else:
                c.execute(
                    "INSERT INTO captures VALUES(?,?,?,?,?,?)", (capture_id, *desired)
                )
                c.execute("COMMIT")
        finally:
            self._close(c, write=True)
        return DocumentationCaptureV1(
            capture_id, captured_at, status, len(body), body, digest
        )

    def read(self, capture_id):
        c = self._open(readonly=True)
        try:
            row = c.execute(
                "SELECT captured_at,status,body_byte_count,body,body_sha256 FROM captures WHERE capture_id=?",
                (capture_id,),
            ).fetchone()
        finally:
            self._close(c)
        if row is None:
            raise ValueError("capture")
        return DocumentationCaptureV1(
            capture_id, datetime.fromisoformat(row[0]), row[1], row[2], row[3], row[4]
        )

    def count(self):
        if not self.path.exists():
            return 0
        c = self._open(readonly=True)
        try:
            return c.execute("SELECT count(*) FROM captures").fetchone()[0]
        finally:
            self._close(c)
