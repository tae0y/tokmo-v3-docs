"""Bounded atomic replacement for mutable private JSON files."""

import os
import stat
from pathlib import Path

from tokmo.records.durability import fsync_directory


def _require_regular(path: Path) -> None:
    mode = os.lstat(path).st_mode
    if not stat.S_ISREG(mode):
        raise ValueError("path is not a regular file")


def write_atomic_json(path: Path, payload: bytes, *, max_bytes: int) -> None:
    if type(payload) is not bytes or len(payload) > max_bytes:
        raise ValueError("payload exceeds max_bytes")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    temp = path.with_name(f".{path.name}.{os.urandom(16).hex()}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp, flags, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb", closefd=False) as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(fd)
        os.replace(temp, path)
        os.chmod(path, 0o600)
        fsync_directory(path.parent)
    finally:
        os.close(fd)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def read_atomic_json(path: Path, *, max_bytes: int) -> bytes:
    _require_regular(path)
    if os.lstat(path).st_size > max_bytes:
        raise ValueError("file exceeds max_bytes")
    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        data = os.read(fd, max_bytes + 1)
    finally:
        os.close(fd)
    if len(data) > max_bytes:
        raise ValueError("file exceeds max_bytes")
    return data
