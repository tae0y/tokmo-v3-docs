#!/usr/bin/env python3
"""Offline migration executor for the pension registry current read model.

Generation 1 keeps a holdings payload on every revision row, duplicating
holdings history between the registry and the official snapshot store.
Generation 2 keeps the payload of exactly one current revision and reduces
every other row to the canonical empty payload, preserving audit and reminder
rows and their foreign keys.

Order enforced by this tool, refusing at the first step that does not verify:

    require the snapshot store already migrated
        -> inspect registry -> backup (+ manifest, hash-verified)
        -> build sidecar -> independently verify -> atomic install
        -> post-install verification -> (on failure) hash-verified restore

The snapshot store must be migrated FIRST, because after this migration the
registry can no longer install current state except from a verified pension
snapshot, and pension snapshots require the widened snapshot schema.

Any failure BEFORE the install leaves the registry byte-identical; the source
is only ever opened read-only and the sidecar is built beside it.

Usage:
    migrate_pension_registry_current_model.py --root DIR \\
        --backup PATH --manifest PATH --authorization-record PATH [--dry-run]
    migrate_pension_registry_current_model.py --restore --root DIR \\
        --backup PATH --manifest PATH

DO NOT RUN during cadence operation.
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
)

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_RESTORED = 3
EXIT_RESTORE_FAILED = 4

REGISTRY_RELATIVE_PATH = ("stores", "pension_registry.sqlite3")
# The prerequisite check must look at the CANONICAL snapshot database,
# under the Toss leaf, or it would never see the real store.


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest_sha256(manifest: Path) -> str | None:
    for line in Path(manifest).read_text(encoding="utf-8").splitlines():
        if line.startswith("sha256="):
            return line.split("=", 1)[1].strip()
    return None


def _write_backup(source: Path, backup: Path, manifest: Path) -> str:
    from tokmo.records.durability import fsync_directory

    raw = Path(source).read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    backup.parent.mkdir(parents=True, exist_ok=True)
    backup.write_bytes(raw)
    os.chmod(backup, 0o600)
    manifest.write_text(f"source={source}\nsha256={digest}\n", encoding="utf-8")
    os.chmod(manifest, 0o600)
    fsync_directory(backup.parent)
    return digest


# Test-only fault injection. Recognized values abort the tool at a named point
# so the recovery boundary can be exercised in a real subprocess. It can only
# ABORT; it can never skip a safety check or a verification.
FAULT_ENV = "TOKMO_MIGRATION_FAULT"
FAULT_AFTER_REPLACE = "after_replace"
FAULT_AT_FSYNC = "at_fsync"
FAULT_AT_POST_VERIFY = "post_verify"
FAULT_RESTORE_PREFIX = "restore:"  # + the primitive's stage name


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


def _restore(
    source: Path, backup: Path, manifest: Path, *, target_already_replaced: bool = False
) -> int:
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
        target=Path(source),
        backup=Path(backup),
        expected_sha256=expected,
        fault=lambda stage: _fault(f"{FAULT_RESTORE_PREFIX}{stage}"),
    )

    if outcome.ok:
        if not _verify_restored_schema(Path(source)):
            print(
                "ERROR: restored file does not carry the expected schema",
                file=sys.stderr,
            )
            print(
                "UNCERTAIN: bytes match the manifest but the schema check "
                "failed. Backup and manifest are intact.",
                file=sys.stderr,
            )
            return EXIT_RESTORE_FAILED
        print(
            f"Restored from backup; sha256 {expected[:16]}... and schema "
            f"verified. Backup and manifest preserved and re-verified."
        )
        return EXIT_RESTORED

    print(
        f"ERROR: restore failed at stage '{outcome.stage}': {outcome.detail}",
        file=sys.stderr,
    )
    if outcome.replacement_may_have_occurred or target_already_replaced:
        # Either the restore itself may have replaced, or the install already
        # did and the restore did not finish undoing it. Both mean the target
        # is not known to be the original.
        print(
            "UNCERTAIN: the target is not known to hold the original bytes. "
            "The backup and manifest are intact and were not overwritten; "
            "rerun --restore.",
            file=sys.stderr,
        )
    else:
        print(
            "The target was NOT replaced by this restore and the install had "
            "not replaced it either. The backup and manifest are intact and "
            "were not overwritten.",
            file=sys.stderr,
        )
    return EXIT_RESTORE_FAILED


def _verify_restored_schema(source: Path) -> bool:
    """A restored registry must reopen as the original generation."""
    try:
        from tokmo.records.pension_revisions import PensionRegistryStore

        return PensionRegistryStore(source, clock=lambda: None).schema_generation() == 1
    except Exception:  # noqa: BLE001 - any doubt is a failed restore
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Offline pension registry current-read-model migration"
    )
    parser.add_argument(
        "--root",
        type=Path,
        required=True,
        help="root holding stores/; no production default",
    )
    parser.add_argument("--backup", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--authorization-record", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--restore", action="store_true")
    args = parser.parse_args(argv)

    from tokmo.records.holdings_snapshots import holdings_snapshot_store_path

    registry_path = args.root.joinpath(*REGISTRY_RELATIVE_PATH)
    snapshot_path = holdings_snapshot_store_path(args.root)

    if args.restore:
        return _restore(registry_path, args.backup, args.manifest)

    from tokmo.records.pension_registry_migration import (
        RegistryMigrationError,
        build_current_read_model_sidecar,
        inspect_registry,
        verify_current_read_model_sidecar,
    )

    # Order gate: the snapshot store must already admit the pension source.
    print("Checking snapshot store generation ...")
    try:
        from tokmo.records.holdings_snapshots import (
            SCHEMA_GENERATION_TWO,
            HoldingsSnapshotStore,
        )

        connection, snapshot_generation = HoldingsSnapshotStore(
            snapshot_path, clock=lambda: None
        )._open_read_generation()
        connection.close()
    except Exception as error:  # noqa: BLE001 - any doubt refuses
        print(f"ERROR: snapshot store unreadable: {error}", file=sys.stderr)
        return EXIT_REFUSED
    if snapshot_generation != SCHEMA_GENERATION_TWO:
        print(
            "ERROR: migrate the snapshot store first; the registry cannot "
            "install current state without pension snapshots",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    print("  snapshot store already admits the pension source.")

    print(f"Inspecting registry at {registry_path} ...")
    try:
        inventory = inspect_registry(registry_path)
    except (RegistryMigrationError, FileNotFoundError) as error:
        reason = getattr(error, "reason", str(error))
        print(f"ERROR: inspection refused: {reason}", file=sys.stderr)
        return EXIT_REFUSED
    print(f"  sha256:           {inventory.sha256[:16]}...")
    print(f"  row counts:       {inventory.row_counts}")
    print(f"  current revision: {inventory.active_revision_id}")
    print(f"  payload rows:     {inventory.payload_bearing_rows}")

    if args.dry_run:
        print("Dry run complete. Nothing written.")
        return EXIT_OK

    if args.authorization_record is None or not args.authorization_record.exists():
        print(
            "ERROR: --authorization-record naming the source sha256 is required",
            file=sys.stderr,
        )
        return EXIT_REFUSED
    if inventory.sha256 not in args.authorization_record.read_text(encoding="utf-8"):
        print(
            "ERROR: authorization record does not name the source sha256",
            file=sys.stderr,
        )
        return EXIT_REFUSED

    print(f"Backing up to {args.backup} ...")
    digest = _write_backup(registry_path, args.backup, args.manifest)
    if digest != inventory.sha256:
        print("ERROR: backup does not match the inspected registry", file=sys.stderr)
        return EXIT_REFUSED
    print(f"  backup sha256 verified: {digest[:16]}...")

    sidecar = registry_path.with_name(registry_path.name + ".sidecar")
    print("Building sidecar ...")
    try:
        build_current_read_model_sidecar(registry_path, sidecar)
        verify_current_read_model_sidecar(inventory, sidecar)
    except (RegistryMigrationError, ValueError, OSError) as error:
        reason = getattr(error, "reason", str(error))
        print(f"ERROR: sidecar refused: {reason}", file=sys.stderr)
        try:
            sidecar.unlink()
        except OSError:
            pass
        if _sha256(registry_path) != inventory.sha256:
            print(
                "ERROR: registry changed despite a pre-install failure", file=sys.stderr
            )
            return EXIT_REFUSED
        print("Registry is byte-identical; nothing was installed.")
        return EXIT_REFUSED
    print("  sidecar verified independently.")

    # B-03: install, directory fsync and post-install verification live inside
    # ONE recovery boundary that knows whether the replacement may already have
    # happened.
    print("Installing ...")
    replaced = False
    try:
        from tokmo.records.durability import fsync_directory

        os.replace(sidecar, registry_path)
        replaced = True
        _fault(FAULT_AFTER_REPLACE)

        _fault(FAULT_AT_FSYNC)
        fsync_directory(registry_path.parent)

        print("Post-install verification ...")
        _fault(FAULT_AT_POST_VERIFY)
        from tokmo.records.pension_revisions import (
            SCHEMA_GENERATION_TWO as REGISTRY_GENERATION_TWO,
        )
        from tokmo.records.pension_revisions import (
            PensionRegistryStore,
        )

        store = PensionRegistryStore(registry_path, clock=lambda: None)
        if store.schema_generation() != REGISTRY_GENERATION_TWO:
            raise ValueError("installed registry is not the current read model")
        active = store.read_active_revision()
        if inventory.active_revision_id is not None:
            if active is None or active.revision_id != inventory.active_revision_id:
                raise ValueError("current revision did not survive")
            print(f"  current holdings preserved: {active.holding_count}")
        if store.count_payload_bearing_rows() > 1:
            raise ValueError("more than one payload row after migration")
        if store.count_superseded_payload_rows() != 0:
            raise ValueError("historical payload retained after migration")
    except BaseException as error:  # noqa: BLE001 - any doubt triggers restore
        print(f"ERROR: install stage failed: {error}", file=sys.stderr)
        if not replaced:
            try:
                sidecar.unlink()
            except OSError:
                pass
            if _sha256(registry_path) == inventory.sha256:
                print("Replacement did not occur; registry is byte-identical.")
                return EXIT_REFUSED
            print(
                "UNCERTAIN: replacement did not report success but the "
                "registry changed; restoring.",
                file=sys.stderr,
            )
        else:
            print(
                "Replacement occurred; restoring the verified backup ...",
                file=sys.stderr,
            )
        return _restore(
            registry_path, args.backup, args.manifest, target_already_replaced=replaced
        )

    print(
        "Migration complete and verified. The registry is a current read "
        "model; snapshot history is the sole holdings-history payload."
    )
    print(
        f"Recovery, if ever needed: --restore --root {args.root} "
        f"--backup {args.backup} --manifest {args.manifest}"
    )
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
