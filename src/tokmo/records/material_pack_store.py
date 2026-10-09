"""Write-once MaterialPack publication and bounded retention."""

import hashlib
import os
import re
import stat
from datetime import datetime
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.ops_paths import confine_to_parent
from tokmo.values.material_pack import (
    PACK_MAX_BYTES,
    JobKind,
    MaterialPackV1,
    canonical_decode,
    canonical_pack_file_bytes,
)

_PACK_NAME = re.compile(r"^([0-9]+)\.json$")
_TEMP_NAME = re.compile(r"^\..+\.tmp$")


class PackHashMismatchError(ValueError):
    pass


class PackCorruptError(ValueError):
    pass


class PackIdentityConflictError(ValueError):
    pass


def _regular(path: Path) -> bool:
    return stat.S_ISREG(os.lstat(path).st_mode)


def _parse_aware(value):
    if type(value) is not str:
        raise ValueError("datetime must be a string")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("datetime must be aware")
    return parsed


def _pack_from_raw(raw: bytes) -> MaterialPackV1:
    value = canonical_decode(raw, max_bytes=PACK_MAX_BYTES)
    keys = {
        "schema_version",
        "job",
        "tick_id",
        "market_partition",
        "payload",
        "captured_at",
        "source_timestamps",
        "parser_version",
        "content_hash",
    }
    if (
        type(value) is not dict
        or not keys <= set(value)
        or not set(value) <= keys | {"mask_digest"}
    ):
        raise ValueError("material pack exact keys required")
    if type(value["market_partition"]) is not list:
        raise ValueError("market_partition must be a list")
    pack = MaterialPackV1(
        mask_digest=value.get("mask_digest"),
        schema_version=value["schema_version"],
        job=JobKind(value["job"]),
        tick_id=value["tick_id"],
        market_partition=tuple(value["market_partition"]),
        payload=value["payload"],
        captured_at=_parse_aware(value["captured_at"]),
        source_timestamps=value["source_timestamps"],
        parser_version=value["parser_version"],
        content_hash=value["content_hash"],
    )
    if canonical_pack_file_bytes(pack) != raw:
        raise ValueError("non-canonical material pack")
    return pack


class MaterialPackWriter:
    def __init__(self, root: Path, *, clock):
        self.root = Path(root)
        self.clock = clock

    def _directory(self, job: JobKind) -> Path:
        return self.root / "packs" / job.value

    def write(self, pack: MaterialPackV1) -> Path:
        directory = self._directory(pack.job)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(directory, 0o700)
        self._cleanup_stale_temps(directory)
        target = directory / f"{pack.tick_id}.json"
        data = canonical_pack_file_bytes(pack)
        if target.exists() or target.is_symlink():
            return self._existing(target, data)
        temp = directory / f".{target.name}.{os.urandom(16).hex()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        fd = os.open(temp, flags, 0o600)
        try:
            os.fchmod(fd, 0o600)
            with os.fdopen(fd, "wb", closefd=False) as handle:
                handle.write(data)
                handle.flush()
                os.fsync(fd)
            try:
                os.link(temp, target, follow_symlinks=False)
            except FileExistsError:
                return self._existing(target, data)
            os.chmod(target, 0o600)
            temp.unlink()
            fsync_directory(directory)
            return target
        finally:
            os.close(fd)
            try:
                temp.unlink()
            except FileNotFoundError:
                pass

    def _existing(self, target: Path, data: bytes) -> Path:
        if not _regular(target):
            raise PackCorruptError("existing pack is not regular")
        existing = target.read_bytes()
        if (
            hashlib.sha256(existing).digest() == hashlib.sha256(data).digest()
            and existing == data
        ):
            return target
        raise PackIdentityConflictError("tick_id already has different final bytes")

    def read_and_verify(self, path: Path, expected_sha256: str) -> MaterialPackV1:
        try:
            if not _regular(path):
                raise PackCorruptError("pack is not regular")
            confined = confine_to_parent(path, self.root / "packs")
            if not _regular(confined):
                raise PackCorruptError("pack is not regular")
            raw = confined.read_bytes()
            if hashlib.sha256(raw).hexdigest() != expected_sha256:
                raise PackHashMismatchError("pack file hash mismatch")
            pack = _pack_from_raw(raw)
            expected_path = (
                self._directory(pack.job) / f"{pack.tick_id}.json"
            ).resolve()
            if confined != expected_path:
                raise PackCorruptError("pack path does not match typed identity")
            return pack
        except PackHashMismatchError:
            raise
        except (OSError, TypeError, ValueError) as error:
            if isinstance(error, PackCorruptError):
                raise
            raise PackCorruptError("corrupt material pack") from error

    def list_packs(self, job: JobKind):
        directory = self._directory(job)
        if not directory.exists():
            return []
        result = []
        for path in directory.iterdir():
            match = _PACK_NAME.fullmatch(path.name)
            if not match:
                continue
            confine_to_parent(path, directory)
            if not _regular(path):
                raise PackCorruptError("pack entry is not regular")
            raw = path.read_bytes()
            result.append((int(match.group(1)), path, hashlib.sha256(raw).hexdigest()))
        return sorted(result, key=lambda item: item[0])

    def reap_orphans(
        self, job: JobKind, keep_tick_ids: frozenset[int], external_refs: frozenset[int]
    ) -> int:
        keep = keep_tick_ids | external_refs
        removed = 0
        for tick_id, path, _digest in self.list_packs(job):
            if tick_id not in keep:
                path.unlink()
                removed += 1
        if removed:
            fsync_directory(self._directory(job))
        return removed

    def _cleanup_stale_temps(self, directory: Path) -> None:
        cutoff = self.clock().timestamp() - 3600
        for path in directory.iterdir():
            if not _TEMP_NAME.fullmatch(path.name):
                continue
            confine_to_parent(path, directory)
            if not _regular(path):
                raise PackCorruptError("temp entry is not regular")
            if os.lstat(path).st_mtime <= cutoff:
                path.unlink()
