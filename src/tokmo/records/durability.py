"""Durability helpers shared by tracer record leaves."""

import hashlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path


def fsync_directory(directory: Path) -> None:
    """Persist directory entries (new/replaced file names) to disk."""
    fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


# ---------------------------------------------------------------------------
# Durable restore-from-backup
#
# Writing a temp copy and calling os.replace is NOT durable: the temp file's
# own data must reach disk before the rename, or a crash can leave the target
# name pointing at a file whose contents were never persisted. Fsyncing only
# the parent directory persists the name, not the bytes.
#
# One primitive, shared by every migration tool, so there is no second subtly
# different and unsafe copy of this sequence.
# ---------------------------------------------------------------------------

RESTORE_STAGE_BACKUP_VERIFY = "backup_verify"
RESTORE_STAGE_TEMP_WRITE = "temp_write"
RESTORE_STAGE_FILE_FSYNC = "file_fsync"
RESTORE_STAGE_TEMP_VERIFY = "temp_verify"
RESTORE_STAGE_REPLACE = "replace"
RESTORE_STAGE_DIR_FSYNC = "dir_fsync"
RESTORE_STAGE_POST_VERIFY = "post_verify"
RESTORE_STAGE_BACKUP_PRESERVED = "backup_preserved"
RESTORE_STAGE_COMPLETE = "complete"

# Stages at or after which the target name may already point at the new bytes.
_STAGES_AFTER_REPLACEMENT = frozenset(
    {
        RESTORE_STAGE_DIR_FSYNC,
        RESTORE_STAGE_POST_VERIFY,
        RESTORE_STAGE_BACKUP_PRESERVED,
        RESTORE_STAGE_COMPLETE,
    }
)


@dataclass(frozen=True)
class DurableRestoreOutcomeV1:
    """Where a restore got to, and whether the target may already have moved."""

    ok: bool
    stage: str
    replacement_may_have_occurred: bool
    detail: str = ""


def _noop_fault(stage: str) -> None:
    return None


def durable_restore_from_backup(
    *,
    target: Path,
    backup: Path,
    expected_sha256: str,
    fault: Callable[[str], None] = _noop_fault,
) -> DurableRestoreOutcomeV1:
    """Put *backup* back at *target*, durably, without consuming the backup.

    Sequence, with each stage separately reportable:

      1. verify the backup against ``expected_sha256``
      2. create a private, exclusive, no-follow temp beside the target
      3. write the verified bytes, flush, and **fsync the open descriptor**
      4. re-read the temp and verify its hash and size
      5. ``os.replace`` the temp over the target
      6. fsync the parent directory
      7. reopen the target and verify its hash
      8. verify the backup is still intact

    The backup and its manifest are never moved or overwritten, so a failure at
    any stage leaves the evidence for another attempt. ``fault`` is a test seam
    that may only raise at a named stage; it can never skip a verification.
    """
    target = Path(target)
    backup = Path(backup)
    temporary = target.with_name(target.name + ".restoring")

    def failure(stage: str, detail: str) -> DurableRestoreOutcomeV1:
        try:
            if temporary.exists():
                temporary.unlink()
        except OSError:
            pass
        return DurableRestoreOutcomeV1(
            False, stage, stage in _STAGES_AFTER_REPLACEMENT, detail
        )

    try:
        if not backup.exists():
            return failure(RESTORE_STAGE_BACKUP_VERIFY, "backup_missing")
        payload = backup.read_bytes()
        if hashlib.sha256(payload).hexdigest() != expected_sha256:
            return failure(
                RESTORE_STAGE_BACKUP_VERIFY, "backup_does_not_match_manifest"
            )
    except OSError as error:
        return failure(RESTORE_STAGE_BACKUP_VERIFY, type(error).__name__)

    # 2 + 3: exclusive, no-follow, owner-only temp; bytes fsynced on its own fd.
    try:
        fault(RESTORE_STAGE_TEMP_WRITE)
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
        descriptor = os.open(temporary, flags, 0o600)
        try:
            os.write(descriptor, payload)
            fault(RESTORE_STAGE_FILE_FSYNC)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except BaseException as error:  # noqa: BLE001 - stage must be reported
        stage = (
            RESTORE_STAGE_FILE_FSYNC
            if isinstance(error, _FaultAt) and error.stage == RESTORE_STAGE_FILE_FSYNC
            else RESTORE_STAGE_TEMP_WRITE
        )
        return failure(stage, type(error).__name__)

    try:
        written = temporary.read_bytes()
        if (
            len(written) != len(payload)
            or hashlib.sha256(written).hexdigest() != expected_sha256
        ):
            return failure(RESTORE_STAGE_TEMP_VERIFY, "temp_copy_mismatch")
    except OSError as error:
        return failure(RESTORE_STAGE_TEMP_VERIFY, type(error).__name__)

    try:
        fault(RESTORE_STAGE_REPLACE)
        os.replace(temporary, target)
    except BaseException as error:  # noqa: BLE001
        return failure(RESTORE_STAGE_REPLACE, type(error).__name__)

    # From here the target name may already carry the restored bytes.
    try:
        fault(RESTORE_STAGE_DIR_FSYNC)
        fsync_directory(target.parent)
    except BaseException as error:  # noqa: BLE001
        return DurableRestoreOutcomeV1(
            False, RESTORE_STAGE_DIR_FSYNC, True, type(error).__name__
        )

    try:
        fault(RESTORE_STAGE_POST_VERIFY)
        if hashlib.sha256(target.read_bytes()).hexdigest() != expected_sha256:
            return DurableRestoreOutcomeV1(
                False, RESTORE_STAGE_POST_VERIFY, True, "target_mismatch"
            )
    except BaseException as error:  # noqa: BLE001
        return DurableRestoreOutcomeV1(
            False, RESTORE_STAGE_POST_VERIFY, True, type(error).__name__
        )

    try:
        if (
            not backup.exists()
            or hashlib.sha256(backup.read_bytes()).hexdigest() != expected_sha256
        ):
            return DurableRestoreOutcomeV1(
                False, RESTORE_STAGE_BACKUP_PRESERVED, True, "backup_did_not_survive"
            )
    except OSError as error:
        return DurableRestoreOutcomeV1(
            False, RESTORE_STAGE_BACKUP_PRESERVED, True, type(error).__name__
        )

    return DurableRestoreOutcomeV1(True, RESTORE_STAGE_COMPLETE, True)


class _FaultAt(RuntimeError):
    """Raised by a test fault seam; carries the stage it fired at."""

    def __init__(self, stage: str) -> None:
        self.stage = stage
        super().__init__(stage)
