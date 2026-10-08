"""Offline explicit legacy-holdings migration and cutover orchestration.

This tool is never imported by startup or composition. It operates only on
the explicit source/candidate paths its caller supplies, decodes with the
frozen ``legacy_holdings_v1`` validator, and authorizes imports solely
through ``authorize_legacy_migration``.

``--dry-run`` is the preflight-only mode: it validates the source in place
and creates and mutates nothing. ``--clone`` is the SDS §10.2 full
rehearsal protocol: it first hashes and copies the source into an
owner-only rehearsal source copy beside the explicit candidate (deterministic
pinned filename, private 0600 creation, never overwriting anything), decodes
that copy — not the original — and then writes exactly one separate
candidate SQLite file under private path rules, reopens it read-only, and
proves schema/row/hash/count parity while the source stays byte-and-mtime
invariant. Per SDS §10.5, deleting any private artifact requires a separate
explicit approval, so the tool never removes the rehearsal copy or a partial
candidate, on success or on failure. Explicit cutover APIs build a separate
approved promotion artifact in the canonical parent, promote with
``os.link`` no-replace, retain its staging alias, revalidate under the family
exclusive lock, and atomically commit the exact marker. Rollback and
retirement remain non-mutating simulations in this wave.

Output is closed and redacted: bounded counts, closed status/reason tokens,
and version constants only — never a holdings value, symbol, ID, hash, or
path.
"""

import argparse
import errno
import hashlib
import os
import stat
import sys
from dataclasses import dataclass, field as dataclass_field
from datetime import datetime, timezone
from pathlib import Path

from tokmo.records.durability import fsync_directory
from tokmo.records.holdings_cutover import (
    CUTOVER_MARKER_BYTES,
    with_family_lock,
)
from tokmo.records.private_sqlite import private_creation

from tokmo.records.holdings_persistence import (
    MIGRATION_CRITERIA_VERSION,
    DenialToken,
    authorize_legacy_migration,
)
from tokmo.records.holdings_snapshots import (
    HoldingsSnapshotReadError,
    HoldingsSnapshotStore,
    canonical_snapshot_bytes,
)
from tokmo.records.legacy_holdings_v1 import (
    LEGACY_REFUSAL_REASONS,
    validate_legacy_json,
)
from tokmo.values import CompleteHoldingsSnapshot

SCHEMA_VERSION = 1
_SIDECAR_SUFFIXES = ("-journal", "-wal", "-shm")

TOOL_REFUSAL_REASONS = frozenset(LEGACY_REFUSAL_REASONS | {
    "source_missing",
    "source_unsafe",
    "candidate_exists",
    "candidate_unsafe",
    "rehearsal_copy_exists",
    "copy_failed",
    "import_failed",
    "verify_failed",
    "source_changed",
})

REHEARSAL_SOURCE_SUFFIX = ".rehearsal-source.json"
PROMOTION_PREFIX = ".holdings_snapshots.staging."


def rehearsal_source_path(candidate: Path) -> Path:
    """Pinned deterministic rehearsal-source-copy path (SDS §10.2 step 3).

    Always beside the explicit candidate inside its already-verified private
    0700 parent — never an implicit external or default location — and by
    construction of the non-empty suffix never equal to the candidate itself.
    """
    return candidate.parent / (candidate.name + REHEARSAL_SOURCE_SUFFIX)


@dataclass(frozen=True)
class MigrationToolReport:
    """Bounded closed summary: counts, closed tokens, versions — nothing else."""

    status: str
    reason: str
    captures: int
    accounts: int
    items: int
    schema_version: int
    criteria_version: str


@dataclass(frozen=True)
class CutoverBuildApproval:
    """Explicit build approval bound to one source identity and byte image."""

    source: Path = dataclass_field(repr=False)
    source_digest: str = dataclass_field(repr=False)
    source_mtime_ns: int = dataclass_field(repr=False)
    source_device: int = dataclass_field(repr=False)
    source_inode: int = dataclass_field(repr=False)
    captures: int
    accounts: int
    items: int


@dataclass(frozen=True)
class CutoverToolReport:
    status: str
    reason: str
    captures: int = 0
    accounts: int = 0
    items: int = 0


def _refusal(reason: str) -> MigrationToolReport:
    return MigrationToolReport(
        "refused", reason, 0, 0, 0, SCHEMA_VERSION, MIGRATION_CRITERIA_VERSION)


def _closed(captures: tuple[CompleteHoldingsSnapshot, ...]) -> MigrationToolReport:
    return MigrationToolReport(
        "closed", "",
        len(captures),
        sum(len(capture.accounts) for capture in captures),
        sum(len(account.holdings)
            for capture in captures for account in capture.accounts),
        SCHEMA_VERSION, MIGRATION_CRITERIA_VERSION)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def approve_cutover_build(source: Path) -> CutoverBuildApproval:
    """Pin the synthetic/offline source that a later exclusive build rechecks."""
    reason, captures, data, mtime_ns = _decode_source(source)
    if reason:
        raise ValueError(reason)
    value = os.lstat(source)
    return CutoverBuildApproval(
        Path(source), hashlib.sha256(data).hexdigest(), mtime_ns,
        value.st_dev, value.st_ino, len(captures),
        sum(len(c.accounts) for c in captures),
        sum(len(a.holdings) for c in captures for a in c.accounts),
    )


def _read_source(source: Path) -> tuple[str, bytes, int]:
    """Read the whole source once behind lstat/open/fstat identity checks.

    Returns ``(refusal_reason, bytes, mtime_ns)``; the reason is empty on
    success. The source is only ever opened read-only with ``O_NOFOLLOW``.
    """
    try:
        pre = os.lstat(source)
    except FileNotFoundError:
        return "source_missing", b"", 0
    except OSError:
        return "source_unsafe", b"", 0
    if (stat.S_ISLNK(pre.st_mode)
            or not stat.S_ISREG(pre.st_mode)
            or pre.st_uid != os.getuid()
            or stat.S_IMODE(pre.st_mode) & 0o022):
        return "source_unsafe", b"", 0
    try:
        fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "source_unsafe", b"", 0
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (pre.st_dev, pre.st_ino)
                or not stat.S_ISREG(opened.st_mode)):
            return "source_unsafe", b"", 0
        with os.fdopen(os.dup(fd), "rb") as handle:
            data = handle.read()
    except OSError:
        return "source_unsafe", b"", 0
    finally:
        os.close(fd)
    return "", data, pre.st_mtime_ns


def _source_unchanged(source: Path, data: bytes, mtime_ns: int) -> bool:
    reason, current, current_mtime = _read_source(source)
    return reason == "" and current == data and current_mtime == mtime_ns


def _decode_source(
    source: Path,
) -> tuple[str, tuple[CompleteHoldingsSnapshot, ...], bytes, int]:
    reason, data, mtime_ns = _read_source(source)
    if reason:
        return reason, (), b"", 0
    result = validate_legacy_json(data)
    if result.status != "VALID":
        return result.reason, (), b"", 0
    return "", result.captures, data, mtime_ns


def _candidate_path_reason(candidate: Path) -> str:
    if os.path.lexists(candidate):
        return "candidate_exists"
    try:
        parent = os.lstat(candidate.parent)
    except OSError:
        return "candidate_unsafe"
    if (stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.getuid()):
        return "candidate_unsafe"
    return ""


def _create_rehearsal_copy(copy_path: Path, data: bytes) -> str:
    """Privately create the rehearsal source copy; never overwrite anything.

    The copy is born 0600 via O_CREAT|O_EXCL|O_NOFOLLOW, written fully, then
    flushed with fsync on both the file and its directory — atomic enough for
    a disposable rehearsal artifact. Any preexisting entry (regular file,
    symlink, or otherwise) refuses; any partial artifact a failure leaves
    behind is preserved (SDS §10.5: deletion needs separate approval).
    """
    if os.path.lexists(copy_path):
        return "rehearsal_copy_exists"
    try:
        fd = os.open(copy_path,
                     os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                     0o600)
    except OSError:
        return "copy_failed"
    try:
        os.fchmod(fd, 0o600)
        view = memoryview(data)
        while view:
            view = view[os.write(fd, view):]
        os.fsync(fd)
    except OSError:
        return "copy_failed"
    finally:
        os.close(fd)
    try:
        fsync_directory(copy_path.parent)
    except OSError:
        return "copy_failed"
    return ""


def _reopen_rehearsal_copy(copy_path: Path, data: bytes,
                           digest: str) -> tuple[str, bytes]:
    """Reopen the copy read-only with nofollow and prove exact parity.

    Verifies regular/mode/uid/size on both lstat and the opened descriptor,
    then exact bytes and SHA-256 against the original source read.
    """
    try:
        pre = os.lstat(copy_path)
    except OSError:
        return "copy_failed", b""
    if (stat.S_ISLNK(pre.st_mode)
            or not stat.S_ISREG(pre.st_mode)
            or stat.S_IMODE(pre.st_mode) != 0o600
            or pre.st_uid != os.getuid()
            or pre.st_size != len(data)):
        return "copy_failed", b""
    try:
        fd = os.open(copy_path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return "copy_failed", b""
    try:
        opened = os.fstat(fd)
        if ((opened.st_dev, opened.st_ino) != (pre.st_dev, pre.st_ino)
                or not stat.S_ISREG(opened.st_mode)):
            return "copy_failed", b""
        with os.fdopen(os.dup(fd), "rb") as handle:
            copy_bytes = handle.read()
    except OSError:
        return "copy_failed", b""
    finally:
        os.close(fd)
    if (copy_bytes != data
            or hashlib.sha256(copy_bytes).hexdigest() != digest):
        return "copy_failed", b""
    return "", copy_bytes


def _verify_candidate(
    candidate: Path, captures: tuple[CompleteHoldingsSnapshot, ...],
) -> str:
    """Read-only reopen: exact modes, zero sidecars, and full row/hash parity."""
    try:
        value = os.lstat(candidate)
        parent = os.lstat(candidate.parent)
    except OSError:
        return "verify_failed"
    if (not stat.S_ISREG(value.st_mode)
            or stat.S_IMODE(value.st_mode) != 0o600
            or value.st_uid != os.getuid()
            or stat.S_IMODE(parent.st_mode) != 0o700):
        return "verify_failed"
    if any(os.path.lexists(f"{candidate}{suffix}")
           for suffix in _SIDECAR_SUFFIXES):
        return "verify_failed"
    store = HoldingsSnapshotStore(candidate, clock=_utc_now)
    try:
        headers = store.list_capture_headers()
        if len(headers) != len(captures):
            return "verify_failed"
        for capture in captures:
            read_back = store.read_snapshot(capture.snapshot_id)
            if (canonical_snapshot_bytes(read_back)
                    != canonical_snapshot_bytes(capture)
                    or read_back.content_hash != capture.content_hash):
                return "verify_failed"
    except (HoldingsSnapshotReadError, ValueError, OSError):
        return "verify_failed"
    return ""


def run_dry_run(source: Path) -> MigrationToolReport:
    """Preflight-only mode: validate the whole source in place and report
    counts; create and mutate nothing (the SDS §10.2 full rehearsal is
    ``run_clone``)."""
    reason, captures, data, mtime_ns = _decode_source(source)
    if reason:
        return _refusal(reason)
    if not _source_unchanged(source, data, mtime_ns):
        return _refusal("source_changed")
    return _closed(captures)


def run_clone(source: Path, candidate: Path) -> MigrationToolReport:
    """SDS §10.2 full rehearsal: copy the source, then build and verify one
    fresh candidate clone from that copy.

    The source is read once behind lstat/open/fstat checks, hashed, and
    privately copied to the pinned rehearsal path beside the explicit
    candidate; the decoder and the imports consume the verified copy, never
    the original. Writes only the copy and the candidate (neither may
    preexist) inside an owner-only 0700 parent; the source is never mutated,
    renamed, or linked, and no artifact is ever deleted — success or failure
    — because SDS §10.5 reserves deletion for a separate explicit approval.
    """
    reason, data, mtime_ns = _read_source(source)
    if reason:
        return _refusal(reason)
    source_digest = hashlib.sha256(data).hexdigest()
    reason = _candidate_path_reason(candidate)
    if reason:
        return _refusal(reason)
    copy_path = rehearsal_source_path(candidate)
    reason = _create_rehearsal_copy(copy_path, data)
    if reason:
        return _refusal(reason)
    reason, copy_bytes = _reopen_rehearsal_copy(
        copy_path, data, source_digest)
    if reason:
        return _refusal(reason)
    if not _source_unchanged(source, data, mtime_ns):
        return _refusal("source_changed")
    result = validate_legacy_json(copy_bytes)
    if result.status != "VALID":
        return _refusal(result.reason)
    captures = result.captures

    store = HoldingsSnapshotStore(candidate, clock=_utc_now)
    for capture in captures:
        permit = authorize_legacy_migration(
            snapshot_id=capture.snapshot_id,
            canonical_bytes=canonical_snapshot_bytes(capture),
            clock=_utc_now,
        )
        if isinstance(permit, DenialToken):
            return _refusal("import_failed")
        receipt = store.import_legacy(capture, permit)
        if receipt.status != "SAVED":
            return _refusal("import_failed")

    reason = _verify_candidate(candidate, captures)
    if reason:
        return _refusal(reason)
    reason, _ = _reopen_rehearsal_copy(copy_path, data, source_digest)
    if reason:
        return _refusal(reason)
    if not _source_unchanged(source, data, mtime_ns):
        return _refusal("source_changed")
    return _closed(captures)


def _approval_source(
    approval: CutoverBuildApproval,
) -> tuple[str, tuple[CompleteHoldingsSnapshot, ...]]:
    reason, captures, data, mtime_ns = _decode_source(approval.source)
    if reason:
        return "source_changed", ()
    try:
        value = os.lstat(approval.source)
    except OSError:
        return "source_changed", ()
    counts = (
        len(captures),
        sum(len(c.accounts) for c in captures),
        sum(len(a.holdings) for c in captures for a in c.accounts),
    )
    if (
        hashlib.sha256(data).hexdigest() != approval.source_digest
        or mtime_ns != approval.source_mtime_ns
        or (value.st_dev, value.st_ino)
        != (approval.source_device, approval.source_inode)
        or counts != (approval.captures, approval.accounts, approval.items)
    ):
        return "source_changed", ()
    return "", captures


def _new_staging(
    canonical: Path,
) -> tuple[str, Path | None, int | None, int | None]:
    try:
        parent = os.lstat(canonical.parent)
    except OSError:
        return "candidate_unsafe", None, None, None
    if (not stat.S_ISDIR(parent.st_mode)
            or stat.S_IMODE(parent.st_mode) != 0o700
            or parent.st_uid != os.getuid()):
        return "candidate_unsafe", None, None, None
    try:
        parent_fd = os.open(canonical.parent, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return "candidate_unsafe", None, None, None
    try:
        for _attempt in range(8):
            name = PROMOTION_PREFIX + os.urandom(16).hex() + ".sqlite3"
            try:
                fd = os.open(
                    name,
                    os.O_RDWR | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
            except OSError:
                return "candidate_unsafe", None, None, None
            try:
                os.fchmod(fd, 0o600)
                value = os.fstat(fd)
                if (not stat.S_ISREG(value.st_mode)
                        or stat.S_IMODE(value.st_mode) != 0o600
                        or value.st_uid != os.getuid()):
                    return "candidate_unsafe", None, None, None
            finally:
                os.close(fd)
            return "", canonical.parent / name, value.st_dev, value.st_ino
    finally:
        os.close(parent_fd)
    return "candidate_unsafe", None, None, None


def _staging_identity(path: Path, device: int, inode: int) -> str:
    try:
        value = os.lstat(path)
    except OSError:
        return "unsafe_path_swap"
    if (stat.S_ISLNK(value.st_mode)
            or not stat.S_ISREG(value.st_mode)
            or (value.st_dev, value.st_ino) != (device, inode)):
        return "unsafe_path_swap"
    return ""


def _build_promotion(
    staging: Path, captures: tuple[CompleteHoldingsSnapshot, ...],
    device: int, inode: int,
) -> str:
    if _staging_identity(staging, device, inode):
        return "unsafe_path_swap"
    store = HoldingsSnapshotStore(
        staging, clock=_utc_now, initialize_precreated=True)
    for capture in captures:
        permit = authorize_legacy_migration(
            snapshot_id=capture.snapshot_id,
            canonical_bytes=canonical_snapshot_bytes(capture),
            clock=_utc_now,
        )
        if isinstance(permit, DenialToken):
            return "import_failed"
        if store.import_legacy(capture, permit).status != "SAVED":
            return "import_failed"
        if _staging_identity(staging, device, inode):
            return "unsafe_path_swap"
    reason = _verify_candidate(staging, captures)
    return reason or _staging_identity(staging, device, inode)


def _commit_marker(marker: Path) -> str:
    """Offline-tool-owned atomic marker writer; callers hold exclusive lock."""
    temp = marker.parent / (
        ".holdings_cutover_v1." + os.urandom(16).hex() + ".tmp")
    replaced = False
    try:
        with private_creation():
            fd = os.open(
                temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, CUTOVER_MARKER_BYTES)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temp, marker)
        replaced = True
        fsync_directory(marker.parent)
    except OSError:
        return ("marker_durability_uncertain" if replaced
                else "marker_commit_failed")
    return ""


def execute_cutover(
    *, approval: CutoverBuildApproval, canonical: Path, marker: Path,
    lock_path: Path, stop_after: str | None = None,
) -> CutoverToolReport:
    """Build, no-replace promote, and commit the exact marker under one lock.

    ``stop_after`` is a synthetic crash-window test seam.  It never removes
    the source, staging alias, canonical, or any other private artifact.
    """
    if stop_after not in {None, "build", "promotion"}:
        raise ValueError("cutover_context_invalid")

    def operation() -> CutoverToolReport:
        if os.path.lexists(canonical):
            return CutoverToolReport("refused", "canonical_exists")
        if os.path.lexists(marker):
            return CutoverToolReport("refused", "marker_exists")
        reason, captures = _approval_source(approval)
        if reason:
            return CutoverToolReport("refused", reason)
        reason, staging, device, inode = _new_staging(canonical)
        if reason or staging is None or device is None or inode is None:
            return CutoverToolReport("refused", reason)
        reason = _build_promotion(staging, captures, device, inode)
        if reason:
            return CutoverToolReport("refused", reason)
        if stop_after == "build":
            return CutoverToolReport("interrupted", "before_promotion")
        try:
            if _staging_identity(staging, device, inode):
                return CutoverToolReport("refused", "unsafe_path_swap")
            fd = os.open(staging, os.O_RDONLY | os.O_NOFOLLOW)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            os.link(staging, canonical)
            if (_staging_identity(staging, device, inode)
                    or _staging_identity(canonical, device, inode)):
                return CutoverToolReport("refused", "cutover_incomplete")
            fsync_directory(canonical.parent)
        except FileExistsError:
            return CutoverToolReport("refused", "canonical_exists")
        except OSError as error:
            reason = (
                "cross_device" if error.errno == errno.EXDEV
                else "promotion_failed")
            return CutoverToolReport("refused", reason)
        if stop_after == "promotion":
            return CutoverToolReport("interrupted", "before_marker")
        # Source, schema, exact captures/counts/hashes are all independently
        # revalidated while the exclusive lock is still held.
        reason, current = _approval_source(approval)
        if reason or _verify_candidate(canonical, current):
            return CutoverToolReport("refused", reason or "verify_failed")
        reason = _commit_marker(marker)
        if reason:
            status = ("recovery_required"
                      if reason == "marker_durability_uncertain"
                      else "refused")
            return CutoverToolReport(status, reason)
        return CutoverToolReport(
            "closed", "", approval.captures, approval.accounts, approval.items)

    try:
        return with_family_lock(
            lock_path, exclusive=True, fn=operation)
    except OSError:
        return CutoverToolReport("refused", "cutover_incomplete")
    except Exception as error:
        if getattr(error, "reason", "") == "family_locked":
            return CutoverToolReport("refused", "cutover_incomplete")
        raise


def simulate_whole_file_quarantine(
    *, source: Path, canonical: Path, marker: Path,
) -> CutoverToolReport:
    """Non-mutating plan only; retirement needs a later explicit approval."""
    del source, canonical, marker
    return CutoverToolReport("simulation", "separate_approval_required")


def simulate_rollback_before_promotion(
    *, source: Path, staging: Path,
) -> CutoverToolReport:
    """Describe the pre-promotion stop without deleting its retained artifact."""
    del source, staging
    return CutoverToolReport("simulation", "artifact_retained")


def simulate_rollback_after_promotion_before_marker(
    *, source: Path, staging: Path, canonical: Path, marker: Path,
) -> CutoverToolReport:
    """Fail-closed simulation; reversal/quarantine requires new authority."""
    del source, staging, canonical, marker
    return CutoverToolReport("simulation", "separate_reversal_required")


def format_report(report: MigrationToolReport) -> str:
    return (
        f"status={report.status}\n"
        f"reason={report.reason}\n"
        f"captures={report.captures}\n"
        f"accounts={report.accounts}\n"
        f"items={report.items}\n"
        f"schema_version={report.schema_version}\n"
        f"criteria_version={report.criteria_version}\n"
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="migrate_holdings_factstore",
        description="Offline legacy holdings migration: dry-run/clone only.",
    )
    parser.add_argument("--source", required=True, type=Path,
                        help="explicit legacy source JSON path")
    parser.add_argument("--candidate", required=True, type=Path,
                        help="explicit candidate SQLite path inside a "
                             "private 0700 parent")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true",
                      help="preflight only: validate the source in place "
                           "and report counts; creates and mutates nothing")
    mode.add_argument("--clone", action="store_true",
                      help="SDS full rehearsal protocol: privately copy the "
                           "source beside the candidate, decode the copy, "
                           "build and verify the candidate; artifacts are "
                           "never deleted (deletion needs separate approval)")
    args = parser.parse_args(argv)

    if args.dry_run:
        report = run_dry_run(args.source)
    else:
        report = run_clone(args.source, args.candidate)
    sys.stdout.write(format_report(report))
    return 0 if report.status == "closed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
