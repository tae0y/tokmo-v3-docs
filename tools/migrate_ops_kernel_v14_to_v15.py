"""Explicit offline Ops Kernel v14 -> v15 migration CLI (Plan/SDS 2026-08-30).

Never invoked by ordinary construction: imports of this module have no side
effects. The operator must attest that all Ops Kernel writers are paused
(--attest-writers-paused). Emits a bounded JSON result on stdout; any refusal
or fault reports status REFUSED/ERROR with a machine-readable reason and a
non-zero exit code. The receipt is written only on success, 0600, never
overwriting an existing target — and a pre-existing receipt target is
detected BEFORE the migration runs so the source stays at exact v14.
"""

import argparse
import json
import os
import stat
import sys
import uuid
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

from tokmo.records.ops_kernel import (
    OpsKernelMigrationReceiptV1,
    OpsKernelStore,
)
from tokmo.values.ops_kernel import OpsKernelError


def _emit(payload, stream=sys.stdout):
    json.dump(payload, stream, separators=(",", ":"), sort_keys=True)
    stream.write("\n")
    stream.flush()


def _receipt_to_json(receipt: OpsKernelMigrationReceiptV1) -> dict:
    return {
        "status": receipt.status,
        "from_version": receipt.from_version,
        "to_version": receipt.to_version,
        "table_count": receipt.table_count,
        "row_count": receipt.row_count,
        "row_fingerprint": receipt.row_fingerprint,
        "writers_paused_attested": receipt.writers_paused_attested,
        "pre_main_sha256": receipt.pre_main_sha256,
        "pre_wal_sha256": receipt.pre_wal_sha256,
        "snapshot_sha256": receipt.snapshot_sha256,
        "installed_main_sha256": receipt.installed_main_sha256,
    }


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Explicit offline Ops Kernel v14 -> v15 migration.",
    )
    parser.add_argument("--database", required=True)
    parser.add_argument("--snapshot", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--attest-writers-paused", action="store_true")
    arguments = parser.parse_args(argv)
    database = Path(arguments.database)
    snapshot = Path(arguments.snapshot)
    receipt_target = Path(arguments.receipt)
    for option, value in (
        ("--database", database),
        ("--snapshot", snapshot),
        ("--receipt", receipt_target),
    ):
        if not value.is_absolute():
            _emit({"status": "REFUSED", "reason": f"{option} must be absolute"})
            return 1
    for option, value in (
        ("--database", database.parent),
        ("--snapshot", snapshot.parent),
        ("--receipt", receipt_target.parent),
    ):
        try:
            info = os.lstat(value)
        except OSError:
            _emit({"status": "REFUSED", "reason": f"{option} parent directory missing"})
            return 1
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != os.getuid()
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            _emit(
                {
                    "status": "REFUSED",
                    "reason": f"{option} parent directory not private",
                }
            )
            return 1
    # B1: refuse a pre-existing receipt target BEFORE migrating so the
    # source database is never left v15 with the requested receipt absent.
    if receipt_target.exists() or receipt_target.is_symlink():
        _emit({"status": "REFUSED", "reason": "receipt target exists"})
        return 1
    try:
        receipt = OpsKernelStore.migrate_v14_to_v15(
            database,
            snapshot_path=snapshot,
            writers_paused=arguments.attest_writers_paused,
        )
    except OpsKernelError as error:
        _emit({"status": "REFUSED", "reason": str(error)})
        return 1
    except Exception as error:  # pragma: no cover - defensive surface
        _emit({"status": "ERROR", "reason": type(error).__name__})
        return 2
    payload = _receipt_to_json(receipt)
    receipt_temporary = receipt_target.with_name(
        f".{receipt_target.name}.tmp-{os.getpid()}-{uuid.uuid4().hex}"
    )
    try:
        descriptor = os.open(
            receipt_temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | (os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0),
            0o600,
        )
        with os.fdopen(descriptor, "w") as writer:
            json.dump(payload, writer, separators=(",", ":"), sort_keys=True)
            writer.write("\n")
            writer.flush()
            os.fsync(writer.fileno())
        # Hard-link publication is atomic and refuses rather than overwrites
        # a target created by another process after the preflight check.
        os.link(receipt_temporary, receipt_target)
        receipt_temporary.unlink()
        parent_descriptor = os.open(receipt_target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
    except OSError as error:
        # Remove only our private temporary. Never delete a raced target.
        receipt_temporary.unlink(missing_ok=True)
        _emit(
            {
                "status": "ERROR",
                "reason": f"receipt persistence failed: {type(error).__name__}",
            }
        )
        return 2
    _emit({"status": "MIGRATED", "receipt": payload})
    return 0


if __name__ == "__main__":
    sys.exit(main())
