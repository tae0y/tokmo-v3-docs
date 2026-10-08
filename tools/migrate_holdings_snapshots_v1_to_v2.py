#!/usr/bin/env python3
"""Offline migration executor for the official holdings snapshot store.

Generation 1 admits only Toss captures; generation 2 additionally admits the
pension source. This tool performs the whole sequence, refusing at the first
step that does not verify:

    inspect -> backup (+ manifest, hash-verified) -> build sidecar
            -> independently verify sidecar -> atomic install
            -> post-install verification -> (on failure) hash-verified restore

Any failure BEFORE the install leaves the source file byte-identical: the
sidecar is built beside it and the source is only ever opened read-only. A
failure AFTER the install is recovered by ``--restore``, which puts the
verified backup back and re-verifies it.

Usage:
    migrate_holdings_snapshots_v1_to_v2.py --source PATH --backup PATH \\
        --manifest PATH --authorization-record PATH [--dry-run]
    migrate_holdings_snapshots_v1_to_v2.py --restore --source PATH \\
        --backup PATH --manifest PATH

DO NOT RUN during cadence operation. DO NOT RUN against production without an
authorization record naming the exact source SHA-256.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sys
from pathlib import Path

from tokmo.records.durability import (
    _FaultAt,
    durable_restore_from_backup,
    fsync_directory,
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_RESTORED = 3
EXIT_RESTORE_FAILED = 4


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest_sha256(manifest: Path) -> str | None:
    for line in Path(manifest).read_text(encoding="utf-8").splitlines():
        if line.startswith("sha256="):
            return line.split("=", 1)[1].strip()
    return None


# Test-only fault injection. Recognized values abort the tool at a named point
# so the recovery boundary can be exercised in a real subprocess. It can only
# ABORT; it can never skip a safety check or a verification.
FAULT_ENV = "TOKMO_MIGRATION_FAULT"
FAULT_AFTER_REPLACE = "after_replace"
FAULT_AT_FSYNC = "at_fsync"
FAULT_AT_POST_VERIFY = "post_verify"
FAULT_RESTORE_PREFIX = "restore:"   # + the primitive's stage name


def _fault(point: str) -> None:
    """Abort at *point* if it is named in the fault list.

    The value is a comma-separated list so an install failure and a restore
    failure can be injected in the same run, which is the only way to reach the
    "restore itself failed" outcome. It can only ABORT; it can never skip a
    safety check or a verification.
    """
    selected = os.environ.get(FAULT_ENV, "")
    if point in {item.strip() for item in selected.split(",") if item.strip()}:
        raise _FaultAt(point)


def _restore(source: Path, backup: Path, manifest: Path, *,
             target_already_replaced: bool = False) -> int:
    """Durably put the verified backup back, never consuming it.

    Delegates to the one shared primitive so there is no second, subtly
    different copy of the write/fsync/replace sequence. The temp copy's own
    bytes are fsynced on its open descriptor before the rename; the parent
    directory is fsynced after; the target is reopened and hashed; and the
    backup is re-verified before success is reported.
    """
    expected = _manifest_sha256(manifest)
    if expected is None:
        print("ERROR: manifest carries no sha256", file=sys.stderr)
        return EXIT_RESTORE_FAILED

    # The restore has its own stage vocabulary; namespace it so an install
    # fault and a restore fault of the same name cannot collide.
    outcome = durable_restore_from_backup(
        target=Path(source), backup=Path(backup),
        expected_sha256=expected,
        fault=lambda stage: _fault(f"{FAULT_RESTORE_PREFIX}{stage}"))

    if outcome.ok:
        if not _verify_restored_schema(Path(source)):
            print("ERROR: restored file does not carry the expected schema",
                  file=sys.stderr)
            print("UNCERTAIN: bytes match the manifest but the schema check "
                  "failed. Backup and manifest are intact.", file=sys.stderr)
            return EXIT_RESTORE_FAILED
        print(f"Restored from backup; sha256 {expected[:16]}... and schema "
              f"verified. Backup and manifest preserved and re-verified.")
        return EXIT_RESTORED

    print(f"ERROR: restore failed at stage '{outcome.stage}': {outcome.detail}",
          file=sys.stderr)
    if outcome.replacement_may_have_occurred or target_already_replaced:
        # Either the restore itself may have replaced, or the install already
        # did and the restore did not finish undoing it. Both mean the target
        # is not known to be the original.
        print("UNCERTAIN: the target is not known to hold the original bytes. "
              "The backup and manifest are intact and were not overwritten; "
              "rerun --restore.", file=sys.stderr)
    else:
        print("The target was NOT replaced by this restore and the install had "
              "not replaced it either. The backup and manifest are intact and "
              "were not overwritten.", file=sys.stderr)
    return EXIT_RESTORE_FAILED


def _verify_restored_schema(source: Path) -> bool:
    """A restored snapshot store must reopen as the original generation."""
    try:
        from tokmo.records.holdings_snapshots import HoldingsSnapshotStore
        connection, generation = HoldingsSnapshotStore(
            source, clock=lambda: None)._open_read_generation()
        connection.close()
        return generation == 1
    except Exception:  # noqa: BLE001 - any doubt is a failed restore
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline holdings snapshot store migration executor")
    parser.add_argument("--source", type=Path, required=True,
                        help="the live holdings snapshot SQLite file")
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--authorization-record", type=Path, default=None,
                        help="file naming the exact source SHA-256")
    parser.add_argument("--dry-run", action="store_true",
                        help="inspect only; write nothing")
    parser.add_argument("--restore", action="store_true",
                        help="put the verified backup back and re-verify")
    args = parser.parse_args(argv)

    from tokmo.records.holdings_snapshot_migration import (
        SidecarBuildError,
        backup_v1,
        build_generation_r2_sidecar,
        inspect_v1,
        verify_generation_r2_sidecar,
    )

    if args.restore:
        return _restore(args.source, args.backup, args.manifest)

    print(f"Inspecting source at {args.source} ...")
    try:
        inventory = inspect_v1(args.source)
    except (SidecarBuildError, FileNotFoundError, ValueError) as error:
        reason = getattr(error, "reason", str(error))
        print(f"ERROR: inspection refused: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"  sha256:     {inventory.sha256[:16]}...")
    print(f"  row counts: {inventory.row_counts}")

    if args.dry_run:
        print("Dry run complete. Nothing written.")
        return EXIT_OK

    if args.authorization_record is None:
        print("ERROR: --authorization-record is required for a real migration",
              file=sys.stderr)
        return EXIT_REFUSED
    if not args.authorization_record.exists():
        print("ERROR: authorization record not found", file=sys.stderr)
        return EXIT_REFUSED
    if inventory.sha256 not in args.authorization_record.read_text(
            encoding="utf-8"):
        print("ERROR: authorization record does not name the source sha256",
              file=sys.stderr)
        return EXIT_REFUSED

    print(f"Backing up to {args.backup} ...")
    receipt = backup_v1(args.source, args.backup, args.manifest)
    if receipt.sha256 != inventory.sha256 or _sha256(args.backup) != inventory.sha256:
        print("ERROR: backup does not match the inspected source",
              file=sys.stderr)
        return EXIT_REFUSED
    print(f"  backup sha256 verified: {receipt.sha256[:16]}...")

    sidecar = args.source.with_name(args.source.name + ".sidecar")
    print("Building sidecar ...")
    try:
        build_generation_r2_sidecar(args.source, sidecar)
        verify_generation_r2_sidecar(inventory, sidecar)
    except (SidecarBuildError, ValueError, OSError) as error:
        reason = getattr(error, "reason", str(error))
        print(f"ERROR: sidecar refused: {reason}", file=sys.stderr)
        try:
            sidecar.unlink()
        except OSError:
            pass
        if _sha256(args.source) != inventory.sha256:
            print("ERROR: source changed despite a pre-install failure",
                  file=sys.stderr)
            return EXIT_REFUSED
        print("Source is byte-identical; nothing was installed.")
        return EXIT_REFUSED
    print("  sidecar verified independently.")

    # B-02: install, directory fsync and post-install verification live inside
    # ONE recovery boundary that knows whether the replacement may already have
    # happened. Anything failing after os.replace restores from the verified
    # backup; anything failing before it leaves the source untouched.
    print("Installing ...")
    replaced = False
    try:
        os.replace(sidecar, args.source)
        replaced = True
        _fault(FAULT_AFTER_REPLACE)

        _fault(FAULT_AT_FSYNC)
        fsync_directory(args.source.parent)

        print("Post-install verification ...")
        _fault(FAULT_AT_POST_VERIFY)
        from tokmo.records.holdings_snapshots import (
            SCHEMA_GENERATION_TWO,
            HoldingsSnapshotStore,
        )
        store = HoldingsSnapshotStore(args.source, clock=lambda: None)
        connection, generation = store._open_read_generation()
        try:
            counts = {
                table: connection.execute(
                    f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                for table in ("holdings_snapshots",
                              "holdings_snapshot_accounts",
                              "holdings_snapshot_items")
            }
        finally:
            connection.close()
        if generation != SCHEMA_GENERATION_TWO:
            raise ValueError("installed store is not generation 2")
        for table, count in counts.items():
            if count != inventory.row_counts[table]:
                raise ValueError(f"row count changed for {table}")
    except BaseException as error:  # noqa: BLE001 - any doubt triggers restore
        print(f"ERROR: install stage failed: {error}", file=sys.stderr)
        if not replaced:
            try:
                sidecar.unlink()
            except OSError:
                pass
            if _sha256(args.source) == inventory.sha256:
                print("Replacement did not occur; source is byte-identical.")
                return EXIT_REFUSED
            print("UNCERTAIN: replacement did not report success but the "
                  "source changed; restoring.", file=sys.stderr)
        else:
            print("Replacement occurred; restoring the verified backup ...",
                  file=sys.stderr)
        return _restore(args.source, args.backup, args.manifest,
                        target_already_replaced=replaced)

    print("Migration complete and verified. Generation 2 is installed.")
    print(f"Recovery, if ever needed: --restore --source {args.source} "
          f"--backup {args.backup} --manifest {args.manifest}")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
