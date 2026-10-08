"""Minimal private-SQLite mechanics shared by the holdings adapter."""

import errno
import os
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
import sqlite3
import stat
import threading
import time

from tokmo.records.durability import fsync_directory

MIN_SQLITE_VERSION = (3, 37, 0)
BUSY_TIMEOUT_MS = 1000
_UMASK_LOCK = threading.Lock()


@dataclass(frozen=True)
class PathSafetyResult:
    unsafe: bool
    reason: str = ""


@contextmanager
def private_creation():
    with _UMASK_LOCK:
        prior = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(prior)


def _uid() -> int:
    if not hasattr(os, "getuid"):
        raise OSError("unsafe_owner")
    return os.getuid()


def _check_stat(value: os.stat_result, *, directory: bool,
                expected_mode: int) -> PathSafetyResult:
    if directory and not stat.S_ISDIR(value.st_mode):
        return PathSafetyResult(True, "unsafe_nonregular")
    if not directory and not stat.S_ISREG(value.st_mode):
        return PathSafetyResult(True, "unsafe_nonregular")
    if stat.S_IMODE(value.st_mode) != expected_mode:
        return PathSafetyResult(True, "unsafe_mode")
    if value.st_uid != _uid():
        return PathSafetyResult(True, "unsafe_owner")
    return PathSafetyResult(False)


def prepare_private_path(path: Path) -> PathSafetyResult:
    parent = path.parent
    try:
        parent_stat = os.lstat(parent)
    except FileNotFoundError:
        try:
            with private_creation():
                parent.mkdir(mode=0o700, parents=True)
            parent_stat = os.lstat(parent)
        except OSError:
            return PathSafetyResult(True, "unsafe_nonregular")
    if stat.S_ISLNK(parent_stat.st_mode):
        return PathSafetyResult(True, "unsafe_symlink")
    result = _check_stat(parent_stat, directory=True, expected_mode=0o700)
    if result.unsafe:
        return result
    try:
        value = os.lstat(path)
    except FileNotFoundError:
        return PathSafetyResult(False)
    if stat.S_ISLNK(value.st_mode):
        return PathSafetyResult(True, "unsafe_symlink")
    result = _check_stat(value, directory=False, expected_mode=0o600)
    if result.unsafe:
        return result
    parent_fd = None
    fd = None
    try:
        parent_fd = os.open(parent, os.O_RDONLY | os.O_NOFOLLOW)
        fd = os.open(
            path.name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        opened = os.fstat(fd)
        if (value.st_dev, value.st_ino) != (opened.st_dev, opened.st_ino):
            return PathSafetyResult(True, "unsafe_path_swap")
        return _check_stat(opened, directory=False, expected_mode=0o600)
    except OSError as error:
        if error.errno == errno.ELOOP:
            return PathSafetyResult(True, "unsafe_symlink")
        return PathSafetyResult(True, "unsafe_nonregular")
    finally:
        if fd is not None:
            os.close(fd)
        if parent_fd is not None:
            os.close(parent_fd)


def check_private_path_without_open(path: Path) -> PathSafetyResult:
    """lstat-only recheck of an existing private file and its parent.

    Safe while this process holds SQLite locks on ``path``: closing any other
    descriptor for that file drops every POSIX lock the process holds on it.
    """
    try:
        values = ((os.lstat(path.parent), True, 0o700),
                  (os.lstat(path), False, 0o600))
    except FileNotFoundError:
        return PathSafetyResult(True, "unsafe_nonregular")
    for value, directory, mode in values:
        if stat.S_ISLNK(value.st_mode):
            return PathSafetyResult(True, "unsafe_symlink")
        result = _check_stat(value, directory=directory, expected_mode=mode)
        if result.unsafe:
            return result
    return PathSafetyResult(False)


def path_identity(path: Path) -> tuple[int, int]:
    value = os.lstat(path)
    return value.st_dev, value.st_ino


def connect_private(path: Path, *, mode: str = "rwc") -> sqlite3.Connection:
    try:
        pre = os.lstat(path)
    except FileNotFoundError:
        pre = None
    uri = f"{path.resolve().as_uri()}?mode={mode}"
    connection = sqlite3.connect(
        uri, uri=True, timeout=BUSY_TIMEOUT_MS / 1000, isolation_level=None)
    try:
        post = os.lstat(path)
        if pre is not None and (
            pre.st_ino != post.st_ino or pre.st_dev != post.st_dev
        ):
            raise OSError("unsafe_path_swap")
        connection.execute("PRAGMA busy_timeout=0")
        connection.execute("PRAGMA foreign_keys=ON")
        if mode != "ro":
            journal = connection.execute("PRAGMA journal_mode").fetchone()
            if journal != ("delete",):
                journal = connection.execute(
                    "PRAGMA journal_mode=DELETE").fetchone()
            synchronous = connection.execute(
                "PRAGMA synchronous").fetchone()
            if synchronous != (2,):
                connection.execute("PRAGMA synchronous=FULL")
                synchronous = connection.execute(
                    "PRAGMA synchronous").fetchone()
            if journal != ("delete",) or synchronous != (2,):
                raise OSError("sqlite_pragma_mismatch")
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        return connection
    except BaseException:
        connection.close()
        raise


def sqlite_locked(error: sqlite3.Error) -> bool:
    return getattr(error, "sqlite_errorcode", None) in {
        sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED,
    }


class ImmediateTransactionTimeout(sqlite3.OperationalError):
    sqlite_errorcode = sqlite3.SQLITE_BUSY


def acquire_immediate_transaction(
    connection: sqlite3.Connection, *, timeout_seconds: float = 1.0,
    poll_interval: float = 0.01,
) -> None:
    """Acquire a write transaction against one monotonic operation deadline."""
    deadline = time.monotonic() + timeout_seconds
    connection.execute("PRAGMA busy_timeout=0")
    try:
        while True:
            try:
                connection.execute("BEGIN IMMEDIATE")
            except sqlite3.Error as error:
                if not sqlite_locked(error):
                    raise
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ImmediateTransactionTimeout(
                        "immediate transaction deadline expired") from error
                time.sleep(min(poll_interval, remaining))
                if time.monotonic() >= deadline:
                    raise ImmediateTransactionTimeout(
                        "immediate transaction deadline expired") from error
                continue
            if time.monotonic() > deadline:
                connection.execute("ROLLBACK")
                raise ImmediateTransactionTimeout(
                    "immediate transaction deadline expired")
            return
    finally:
        connection.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")


def table_is_strict(connection: sqlite3.Connection, table: str) -> bool:
    return any(
        row[1] == table and row[5] == 1
        for row in connection.execute("PRAGMA table_list")
    )


def _sidecars(path: Path) -> list[Path]:
    return [
        Path(f"{path}{suffix}") for suffix in ("-journal", "-wal", "-shm")
        if Path(f"{path}{suffix}").exists()
    ]


def assert_private_sidecars_while_open(path: Path) -> list[Path]:
    paths = _sidecars(path)
    for sidecar in paths:
        value = os.lstat(sidecar)
        if (not stat.S_ISREG(value.st_mode)
                or stat.S_IMODE(value.st_mode) != 0o600
                or value.st_uid != _uid()):
            raise OSError("unsafe_sidecar")
    return paths


def assert_no_sidecars_after_close(path: Path) -> list[Path]:
    return _sidecars(path)
