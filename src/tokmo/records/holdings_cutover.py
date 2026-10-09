"""Closed Toss-holdings cutover marker, route, and family lock.

This runtime module is deliberately read-only with respect to the cutover
marker.  Marker creation and transition belong to the explicit offline tool.
"""

import errno
import fcntl
import json
import os
import stat
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import TypeVar

from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
)
from tokmo.records.legacy_holdings_v1 import validate_legacy_json
from tokmo.records.private_sqlite import prepare_private_path, private_creation

CUTOVER_MARKER_BYTES = b'{"state":"sqlite-v1","schema_version":1}'
FAMILY_LOCK_DEADLINE_S = 1.0
FAMILY_LOCK_POLL_INTERVAL_S = 0.01
_SIDECARS = ("-journal", "-wal", "-shm")
_T = TypeVar("_T")


@dataclass(frozen=True)
class CutoverMarkerV1:
    state: str
    schema_version: int


@dataclass(frozen=True)
class CutoverContext:
    """Explicit knowledge needed to distinguish first use from data loss."""

    first_use_legacy: bool


class HoldingsRoute(Enum):
    LEGACY = "LEGACY"
    SQLITE_V1 = "SQLITE_V1"
    STAGED_OR_CRASHED = "STAGED_OR_CRASHED"
    INVALID = "INVALID"


class FamilyLockTimeout(RuntimeError):
    def __init__(self, *, deadline_s: float) -> None:
        self.reason = "family_locked"
        self.deadline_s = deadline_s
        super().__init__("family_locked")


def _object_without_duplicates(pairs: list[tuple[str, object]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("cutover_incomplete")
        result[key] = value
    return result


def parse_marker_bytes(raw: bytes) -> CutoverMarkerV1:
    """Parse only the CLOSED exact two-key marker schema."""
    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=_object_without_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        raise ValueError("cutover_incomplete") from None
    if (
        type(value) is not dict
        or set(value) != {"state", "schema_version"}
        or value["state"] != "sqlite-v1"
        or type(value["schema_version"]) is not int
        or value["schema_version"] != 1
    ):
        raise ValueError("cutover_incomplete")
    return CutoverMarkerV1("sqlite-v1", 1)


def _private_regular(path: Path) -> bool:
    result = prepare_private_path(path)
    return not result.unsafe and os.path.lexists(path)


def _read_private(path: Path) -> bytes:
    pre = os.lstat(path)
    if (
        stat.S_ISLNK(pre.st_mode)
        or not stat.S_ISREG(pre.st_mode)
        or stat.S_IMODE(pre.st_mode) != 0o600
        or pre.st_uid != os.getuid()
    ):
        raise OSError("cutover_incomplete")
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        opened = os.fstat(fd)
        if (pre.st_dev, pre.st_ino) != (
            opened.st_dev,
            opened.st_ino,
        ) or not stat.S_ISREG(opened.st_mode):
            raise OSError("cutover_incomplete")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                return b"".join(chunks)
            chunks.append(chunk)
    finally:
        os.close(fd)


def _marker_state(path: Path) -> str:
    if not os.path.lexists(path):
        return "absent"
    try:
        parse_marker_bytes(_read_private(path))
    except (OSError, ValueError):
        return "invalid"
    return "valid"


def _sqlite_state(path: Path) -> str:
    if any(os.path.lexists(f"{path}{suffix}") for suffix in _SIDECARS):
        return "sidecars"
    if not os.path.lexists(path):
        return "absent"
    if not _private_regular(path):
        return "invalid"
    try:
        HoldingsSnapshotStore(
            path,
            clock=lambda: datetime(1970, 1, 1, tzinfo=UTC),
        ).list_capture_headers(limit=1)
    except (HoldingsSnapshotReadError, OSError, ValueError):
        return "invalid"
    return "valid"


def _legacy_state(path: Path, *, first_use: bool) -> str:
    if not os.path.lexists(path):
        return "safe_absent" if first_use else "unsafe"
    if not _private_regular(path):
        return "unsafe"
    try:
        raw = _read_private(path)
    except OSError:
        return "unsafe"
    if not raw or validate_legacy_json(raw).status != "VALID":
        return "unsafe"
    return "safe_valid"


def resolve_route(
    *,
    marker_path: Path,
    sqlite_path: Path,
    json_path: Path,
    context: CutoverContext,
) -> HoldingsRoute:
    """Resolve the exhaustive SDS §9.3 matrix without creating anything."""
    marker = _marker_state(marker_path)
    sqlite = _sqlite_state(sqlite_path)
    if marker == "invalid":
        return HoldingsRoute.INVALID
    if marker == "valid":
        return HoldingsRoute.SQLITE_V1 if sqlite == "valid" else HoldingsRoute.INVALID
    if sqlite == "sidecars":
        return HoldingsRoute.INVALID
    if sqlite != "absent":
        return HoldingsRoute.STAGED_OR_CRASHED
    legacy = _legacy_state(json_path, first_use=context.first_use_legacy)
    return (
        HoldingsRoute.LEGACY
        if legacy in {"safe_absent", "safe_valid"}
        else HoldingsRoute.INVALID
    )


def _unlock_close(fd: int) -> None:
    try:
        fcntl.flock(fd, fcntl.LOCK_UN | fcntl.LOCK_NB)
    finally:
        os.close(fd)


def acquire_family_lock(
    lock_path: Path,
    *,
    exclusive: bool,
    deadline_s: float = FAMILY_LOCK_DEADLINE_S,
) -> int:
    """Acquire owner-only flock using LOCK_NB under one monotonic deadline."""
    deadline = time.monotonic() + deadline_s
    operation = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise FamilyLockTimeout(deadline_s=deadline_s)
        fd = None
        try:
            parent = os.lstat(lock_path.parent)
            if (
                stat.S_ISLNK(parent.st_mode)
                or not stat.S_ISDIR(parent.st_mode)
                or stat.S_IMODE(parent.st_mode) != 0o700
                or parent.st_uid != os.getuid()
            ):
                raise OSError(errno.EPERM, "family_locked")
            with private_creation():
                fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            if time.monotonic() >= deadline:
                os.close(fd)
                raise FamilyLockTimeout(deadline_s=deadline_s)
            opened = os.fstat(fd)
            if (
                not stat.S_ISREG(opened.st_mode)
                or stat.S_IMODE(opened.st_mode) != 0o600
                or opened.st_uid != os.getuid()
            ):
                os.close(fd)
                fd = None
                raise FamilyLockTimeout(deadline_s=deadline_s)
            if time.monotonic() >= deadline:
                os.close(fd)
                raise FamilyLockTimeout(deadline_s=deadline_s)
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            if time.monotonic() >= deadline:
                _unlock_close(fd)
                raise FamilyLockTimeout(deadline_s=deadline_s)
            return fd
        except FamilyLockTimeout:
            raise
        except (BlockingIOError, OSError) as error:
            if fd is not None:
                os.close(fd)
            if isinstance(error, OSError) and error.errno not in {
                errno.EAGAIN,
                errno.EACCES,
                errno.EINTR,
                errno.EWOULDBLOCK,
            }:
                # Unsafe/unopenable lock paths are deliberately reported with
                # the same bounded closed reason as contention.
                pass
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise FamilyLockTimeout(deadline_s=deadline_s) from None
            time.sleep(min(FAMILY_LOCK_POLL_INTERVAL_S, remaining))
            if time.monotonic() >= deadline:
                raise FamilyLockTimeout(deadline_s=deadline_s) from None


def with_family_lock(
    lock_path: Path,
    *,
    exclusive: bool,
    fn: Callable[[], _T],
    deadline_s: float = FAMILY_LOCK_DEADLINE_S,
) -> _T:
    fd = acquire_family_lock(lock_path, exclusive=exclusive, deadline_s=deadline_s)
    try:
        return fn()
    finally:
        _unlock_close(fd)
