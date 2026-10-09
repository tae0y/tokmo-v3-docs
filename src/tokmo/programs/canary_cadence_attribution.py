"""Attribute concurrent canonical-root changes to the production cadence.

G2.5 v3. Under the sandbox the canary provably cannot write the canonical
root (see ``canary_sandbox_attestation``), yet the protected stores still
change while it runs, because the production cadence cron writes them. A
content hash cannot name the actor, so this module supplies the missing
half of the argument: durable, timestamped receipts written by the cadence
itself.

Two receipt sources:

1. ``<canonical root>/ops/cadence_ticks.sqlite3`` — SQLite ledger, one row
   per scheduler slot with ``started_at`` and ``finished_at``.  Opened
   read-only.  This source works under the sandbox (journal_mode=delete)
   but depends on a canonical-root file.

2. ``<project>/var/runtime/cadence/<tick>/manifest.json`` — Hermes
   runtime manifest, written by the scheduler parent process *outside*
   the canonical root.  This is the preferred source because it requires
   no canonical-root access and is therefore safe under any sandbox
   profile that denies canonical writes.

A change to a protected path is attributed to the cadence only when

- the OS write-deny attestation holds (the canary could not be the writer),
- at least one receipt is *fresh*, i.e. its activity interval overlaps the
  canary session window, and
- the mutated path's post-run mtime falls inside such a receipt's interval.

Receipts that predate the session window are stale and attribute nothing;
a missing or unreadable ledger attributes nothing. Both cases fail closed
at the call site.
"""

import json
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.values.ops_kernel import parse_iso

# Relative location of the cadence receipt ledger inside the canonical root.
CADENCE_RECEIPT_RELATIVE = "ops/cadence_ticks.sqlite3"

# Fixed Hermes runtime cadence directory (outside canonical root).
# This path is derived from Path.home() and is NOT caller-selectable.
RUNTIME_CADENCE_DIR = (
    Path(__file__).resolve().parents[3] / "var" / "runtime" / "cadence"
)  # relocated from ~/.hermes/runtime 2026-09-24

# Tolerance applied to a receipt's activity interval when matching an
# observed mtime. Filesystem mtimes are recorded by the writing process
# slightly after its own receipt row, and the terminal receipt is written
# after the last store flush.
DEFAULT_ATTRIBUTION_GRACE = timedelta(seconds=90)


@dataclass(frozen=True)
class CadenceReceiptWindow:
    """One cadence slot's activity interval, from its durable receipt."""

    policy_version: str
    scheduled_for_utc: str
    status: str
    started_at: datetime
    finished_at: "datetime | None" = None

    def active_interval(self, grace: timedelta) -> "tuple[datetime, datetime]":
        """Interval during which this cadence run could have written.

        A receipt still in STARTED has no ``finished_at``; its interval is
        left open by ``grace`` rather than treated as instantaneous.
        """
        end = self.finished_at if self.finished_at is not None else self.started_at
        return (self.started_at - grace, end + grace)

    def to_dict(self) -> dict:
        return {
            "policy_version": self.policy_version,
            "scheduled_for_utc": self.scheduled_for_utc,
            "status": self.status,
            "started_at": self.started_at.isoformat(),
            "finished_at": (
                None if self.finished_at is None else self.finished_at.isoformat()
            ),
        }


def read_cadence_receipts(canonical_root: Path) -> "tuple[CadenceReceiptWindow, ...]":
    """Read every cadence receipt from the canonical ledger, read-only.

    Returns an empty tuple when the ledger is absent or unreadable, so a
    missing ledger attributes nothing and the caller fails closed.
    """
    path = Path(canonical_root) / CADENCE_RECEIPT_RELATIVE
    if not path.exists():
        return ()
    try:
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=1.0)
    except sqlite3.Error:
        return ()
    try:
        rows = connection.execute(
            "SELECT policy_version, scheduled_for_utc, status, "
            "started_at, finished_at FROM cadence_tick_receipts"
        ).fetchall()
    except sqlite3.Error:
        return ()
    finally:
        connection.close()

    receipts = []
    for policy_version, scheduled, status, started, finished in rows:
        try:
            started_at = parse_iso(started)
            finished_at = parse_iso(finished)
        except ValueError:
            continue
        if started_at is None:
            continue
        receipts.append(
            CadenceReceiptWindow(
                policy_version=str(policy_version),
                scheduled_for_utc=str(scheduled),
                status=str(status),
                started_at=started_at,
                finished_at=finished_at,
            )
        )
    return tuple(receipts)


def read_cadence_receipts_from_manifests(
    cadence_dir: "Path | None" = None,
    *,
    window_start: "datetime | None" = None,
    window_end: "datetime | None" = None,
    grace: timedelta = DEFAULT_ATTRIBUTION_GRACE,
) -> "tuple[CadenceReceiptWindow, ...]":
    """Read cadence receipts from Hermes runtime manifests.

    Each subdirectory of ``cadence_dir`` named like ``20260810T2305``
    contains a ``manifest.json`` written by the scheduler parent. This
    path is OUTSIDE the canonical root and requires no canonical writes.

    Only SUCCEEDED manifests with zero stderr/exit_code are accepted.
    Stale manifests (outside the window) are excluded.

    When cadence_dir is None, uses the fixed RUNTIME_CADENCE_DIR.
    When window_start/window_end are None, no freshness filter is applied.
    """
    if cadence_dir is None:
        cadence_dir = RUNTIME_CADENCE_DIR
    cadence_dir = Path(cadence_dir)
    if not cadence_dir.exists():
        return ()

    receipts = []
    for tick_dir in sorted(cadence_dir.iterdir()):
        if not tick_dir.is_dir():
            continue
        manifest_path = tick_dir / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text())
        except (json.JSONDecodeError, OSError):
            continue

        # Only accept successful cadence runs
        status_raw = manifest.get("status", "")
        exit_code = manifest.get("child_exit_code", -1)
        if exit_code != 0:
            continue
        # Map manifest status to receipt status
        status = "COMPLETE" if status_raw == "SUCCEEDED" else status_raw

        try:
            started_at = datetime.fromisoformat(manifest["started_at"])
            finished_at = datetime.fromisoformat(manifest["finished_at"])
        except (KeyError, ValueError, TypeError):
            continue

        if started_at.tzinfo is None:
            started_at = started_at.replace(tzinfo=UTC)
        if finished_at is not None and finished_at.tzinfo is None:
            finished_at = finished_at.replace(tzinfo=UTC)

        receipt = CadenceReceiptWindow(
            policy_version="runtime-manifest",
            scheduled_for_utc=tick_dir.name,
            status=status,
            started_at=started_at,
            finished_at=finished_at,
        )

        # Apply freshness filter if window provided
        if window_start is not None and window_end is not None:
            begin, end = receipt.active_interval(grace)
            if end < window_start or begin > window_end:
                continue

        receipts.append(receipt)

    return tuple(receipts)


def fresh_receipts(
    receipts: "tuple[CadenceReceiptWindow, ...]",
    *,
    window_start: datetime,
    window_end: datetime,
    grace: timedelta = DEFAULT_ATTRIBUTION_GRACE,
) -> "tuple[CadenceReceiptWindow, ...]":
    """Receipts whose activity interval overlaps the canary session window.

    Anything entirely before ``window_start`` is stale: the cadence run it
    records finished before the canary began and cannot explain a change
    observed during the session.
    """
    selected = []
    for receipt in receipts:
        begin, end = receipt.active_interval(grace)
        if end < window_start or begin > window_end:
            continue
        selected.append(receipt)
    return tuple(selected)


def mtime_attributed(
    receipts: "tuple[CadenceReceiptWindow, ...]",
    mtime: datetime,
    *,
    grace: timedelta = DEFAULT_ATTRIBUTION_GRACE,
) -> bool:
    """True when ``mtime`` falls inside some receipt's activity interval."""
    for receipt in receipts:
        begin, end = receipt.active_interval(grace)
        if begin <= mtime <= end:
            return True
    return False
