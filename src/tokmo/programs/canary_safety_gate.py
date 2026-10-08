"""Protected-path safety gate for bounded live canaries.

The old fingerprint_directory() hashed the ENTIRE canonical root content.
When the production cadence cron writes to its own stores concurrently,
the full-root hash always changes — producing a false positive
canonical_fingerprint_mutated that masks genuine canary data-path success.

This module replaces that full-root gate with a targeted safety contract:

1. Predeclared protected_paths — the canary must NEVER mutate these.
   Content-hashed (SHA-256) pre/post for exact mutation detection.

2. Predeclared expected_mutable_paths — scheduler-owned stores that
   are expected to change concurrently. Tracked by lightweight metadata
   (size+mtime). Changes here are attributed and tolerated.
   Directory entries (trailing /) mean any new file under that directory
   is also attributed.

3. Full-root diagnostic — a lightweight content-hash of the full root
   is computed for diagnostic purposes (is the overall tree different?),
   but does NOT gate success. Only protected mutations and unattributed
   changes gate.

4. Fail closed on:
   - Any protected path whose SHA-256 content hash changed
   - Any file that changed but is neither protected nor expected-mutable
     (unattributed change)

5. PASS when:
   - All protected paths unchanged
   - All changes are attributed (to expected_mutable or to new files
     under expected_mutable directories)
   - No unattributed changes exist

No raw file content, secrets, or identifiers appear in the result —
only relative paths and hex digest hashes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import hashlib
import os


# ── Predeclared paths for tokmo-v3 canonical root ─────────────────────
# These are the stores the canary must NEVER mutate. The canary is
# quote-only and writes exclusively to its isolated evidence store.

DEFAULT_PROTECTED_PATHS: tuple[str, ...] = (
    "stores/latest_quotes.sqlite3",
    "stores/trade_ledger.sqlite3",
    "stores/order_scan_state.sqlite3",
    "stores/pension_registry.sqlite3",
    "toss/holdings_snapshots.sqlite3",
)

# Scheduler-owned stores expected to change concurrently with canary.
# Directory entries (trailing /) match any new file under that directory.

DEFAULT_EXPECTED_MUTABLE_PATHS: tuple[str, ...] = (
    "ops/",                          # covers ops_kernel, cadence_ticks, etc.
    "ops/ops_kernel.sqlite3",        # explicit for evidence clarity
    "ops/cadence_ticks.sqlite3",     # explicit for evidence clarity
    "news/",
    "opendart-bulk.sqlite3",
    "events.jsonl",
    "decisions.jsonl",
    "krx_decisions.jsonl",
    "krx_facts.json",
    "opendart_decisions.jsonl",
    "opendart_facts.json",
    "facts.json",
    "runtime/",
)


@dataclass(frozen=True)
class SafetyGateResult:
    """Outcome of the protected-path safety gate evaluation."""

    verdict: str  # "pass" or "fail"
    reason: str
    protected_mutations: tuple[str, ...] = ()
    unattributed_changes: tuple[str, ...] = ()
    full_root_pre_hash: str = ""
    full_root_post_hash: str = ""
    full_root_changed: bool = False
    # v3: post-run mtime (ns since epoch) of each mutated protected path.
    # Actor attribution needs a timestamp to compare against durable
    # cadence receipts — the content hash alone names no actor.
    protected_mutation_mtimes: tuple[tuple[str, int], ...] = ()
    # v3 reclosure: same metadata for unattributed changes, so each path's
    # mtime can be individually checked against cadence receipt intervals.
    unattributed_mutation_mtimes: tuple[tuple[str, int], ...] = ()

    def to_dict(self) -> dict:
        return {
            "verdict": self.verdict,
            "reason": self.reason,
            "protected_mutations": list(self.protected_mutations),
            "unattributed_changes": list(self.unattributed_changes),
            "full_root_pre_hash": self.full_root_pre_hash,
            "full_root_post_hash": self.full_root_post_hash,
            "full_root_changed": self.full_root_changed,
            "protected_mutation_mtimes": [
                {"path": rel, "mtime_ns": mtime}
                for rel, mtime in self.protected_mutation_mtimes
            ],
            "unattributed_mutation_mtimes": [
                {"path": rel, "mtime_ns": mtime}
                for rel, mtime in self.unattributed_mutation_mtimes
            ],
        }


@dataclass
class _FileSnapshot:
    """Lightweight metadata snapshot of a single file."""
    rel_path: str
    exists: bool
    size: int
    mtime_ns: int
    content_hash: str  # SHA-256 hex digest, or "" if absent


def _hash_file_content(path: Path) -> str:
    """Compute SHA-256 content hash of a file."""
    hasher = hashlib.sha256()
    hasher.update(path.read_bytes())
    return hasher.hexdigest()


def _snapshot_file(root: Path, rel: str) -> _FileSnapshot:
    """Snapshot a single file by relative path."""
    full = root / rel
    if not full.exists() or not full.is_file():
        return _FileSnapshot(
            rel_path=rel, exists=False, size=0, mtime_ns=0, content_hash="")
    st = full.stat()
    return _FileSnapshot(
        rel_path=rel, exists=True, size=st.st_size,
        mtime_ns=st.st_mtime_ns, content_hash=_hash_file_content(full))


def _is_under_dir(rel: str, dir_prefix: str) -> bool:
    """Check if relative path 'rel' is under directory prefix 'dir_prefix'.

    dir_prefix ends with / (e.g. "ops/").
    """
    if not dir_prefix.endswith("/"):
        return False
    return rel.startswith(dir_prefix)


def _scan_all_files(root: Path) -> list[str]:
    """Return sorted list of all regular file relative paths under root."""
    if not root.exists():
        return []
    result = []
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            result.append(path.relative_to(root).as_posix())
    return result


def _diagnostic_root_hash(root: Path) -> str:
    """Compute a lightweight content hash of the full root for diagnostic.

    This is NOT used for gating — only to record whether the overall tree
    changed. Uses content hash of each file so it's deterministic.
    """
    if not root.exists():
        return hashlib.sha256(b"").hexdigest()
    hasher = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_file() and not path.is_symlink():
            rel = path.relative_to(root).as_posix()
            file_hash = _hash_file_content(path)
            hasher.update(f"{rel}\0{file_hash}\0".encode())
    return hasher.hexdigest()


class CanarySafetyGate:
    """Protected-path safety gate for bounded canaries.

    Usage:
        gate = CanarySafetyGate(
            root=canonical_root,
            protected_paths=DEFAULT_PROTECTED_PATHS,
            expected_mutable_paths=DEFAULT_EXPECTED_MUTABLE_PATHS,
        )
        gate.snapshot_pre()
        # ... run canary (concurrent scheduler may write to its stores) ...
        result = gate.evaluate_post()
        if result.verdict == "fail":
            # STOP: canary mutated protected material or unattributed change
    """

    def __init__(self, root: Path,
                 protected_paths: "tuple[str, ...] | None" = None,
                 expected_mutable_paths: "tuple[str, ...] | None" = None):
        self.root = Path(root)
        self.protected_paths = protected_paths if protected_paths is not None \
            else DEFAULT_PROTECTED_PATHS
        self.expected_mutable_paths = (
            expected_mutable_paths if expected_mutable_paths is not None
            else DEFAULT_EXPECTED_MUTABLE_PATHS)

        self._pre_protected: dict[str, _FileSnapshot] = {}
        self._pre_all: dict[str, _FileSnapshot] = {}
        self._pre_root_hash: str = ""

    def snapshot_pre(self) -> None:
        """Take the pre-run snapshot of protected paths and full root."""
        # Snapshot all files for unattributed-change detection
        all_files = _scan_all_files(self.root)
        self._pre_all = {
            rel: _snapshot_file(self.root, rel) for rel in all_files
        }

        # Snapshot protected paths specifically (content hash)
        self._pre_protected = {}
        for rel in self.protected_paths:
            self._pre_protected[rel] = _snapshot_file(self.root, rel)

        # Diagnostic full root hash
        self._pre_root_hash = _diagnostic_root_hash(self.root)

    def evaluate_post(self) -> SafetyGateResult:
        """Evaluate post-run state against pre-run snapshot."""
        if not self.root.exists():
            # Root doesn't exist post-run — nothing mutated
            return SafetyGateResult(
                verdict="pass",
                reason="root_absent_no_mutation",
                full_root_pre_hash=self._pre_root_hash,
                full_root_post_hash=self._pre_root_hash,
                full_root_changed=False,
            )

        post_root_hash = _diagnostic_root_hash(self.root)
        full_root_changed = (post_root_hash != self._pre_root_hash)

        # 1. Check protected paths for content mutation
        protected_mutations = []
        protected_mtimes = []
        for rel in self.protected_paths:
            pre_snap = self._pre_protected.get(rel)
            post_snap = _snapshot_file(self.root, rel)

            if pre_snap is None:
                # Protected path wasn't in pre snapshot — shouldn't happen
                # unless root didn't exist pre. Check if file exists now.
                if post_snap.exists:
                    protected_mutations.append(rel)
                    protected_mtimes.append((rel, post_snap.mtime_ns))
                continue

            if post_snap.exists != pre_snap.exists:
                # File appeared or disappeared — mutation
                protected_mutations.append(rel)
                protected_mtimes.append((rel, post_snap.mtime_ns))
            elif post_snap.exists and pre_snap.exists:
                if post_snap.content_hash != pre_snap.content_hash:
                    protected_mutations.append(rel)
                    protected_mtimes.append((rel, post_snap.mtime_ns))

        # 2. Check for unattributed changes.
        # Both classes are always computed. The earlier gate returned on the
        # first protected mutation, so an unattributed change hiding behind
        # one stayed invisible — and a protected mutation that durable
        # cadence receipts explain may now be excused, at which point the
        # caller still needs the unattributed list to fail closed on.
        post_all_files = set(_scan_all_files(self.root))
        pre_all_files = set(self._pre_all.keys())

        unattributed = []
        unattributed_mtimes = []
        protected_set = set(self.protected_paths)

        # Check files that existed pre and may have changed
        for rel in sorted(pre_all_files | post_all_files):
            if rel in protected_set:
                # Accounted for by the protected check above; it must never
                # be double-counted as an unattributed change.
                continue
            pre_snap = self._pre_all.get(rel)
            post_snap = _snapshot_file(self.root, rel)

            # Did this file change?
            changed = False
            if pre_snap is None and post_snap.exists:
                # New file
                changed = True
            elif pre_snap is not None and not post_snap.exists:
                # Deleted file
                changed = True
            elif pre_snap is not None and post_snap.exists:
                if pre_snap.size != post_snap.size or \
                        pre_snap.mtime_ns != post_snap.mtime_ns:
                    changed = True

            if not changed:
                continue

            # Is this change attributed?
            if self._is_attributed(rel):
                continue

            unattributed.append(rel)
            unattributed_mtimes.append((rel, post_snap.mtime_ns))

        if protected_mutations or unattributed:
            # Protected mutation is the more severe class, so it names the
            # reason when both are present.
            if protected_mutations:
                reason = f"protected_mutation:{','.join(protected_mutations)}"
            else:
                reason = f"unattributed_change:{','.join(unattributed)}"
            return SafetyGateResult(
                verdict="fail",
                reason=reason,
                protected_mutations=tuple(protected_mutations),
                unattributed_changes=tuple(unattributed),
                full_root_pre_hash=self._pre_root_hash,
                full_root_post_hash=post_root_hash,
                full_root_changed=full_root_changed,
                protected_mutation_mtimes=tuple(protected_mtimes),
                unattributed_mutation_mtimes=tuple(unattributed_mtimes),
            )

        # 3. All clear — protected unchanged, all changes attributed
        return SafetyGateResult(
            verdict="pass",
            reason="protected_unchanged_all_attributed",
            full_root_pre_hash=self._pre_root_hash,
            full_root_post_hash=post_root_hash,
            full_root_changed=full_root_changed,
        )

    def _is_attributed(self, rel: str) -> bool:
        """Check if a changed path is attributed (protected or expected)."""
        # Protected paths are checked separately; if a protected path
        # changed, it would have been caught above. So here we only
        # check expected_mutable.
        for pattern in self.expected_mutable_paths:
            if pattern.endswith("/"):
                if _is_under_dir(rel, pattern):
                    return True
            else:
                if rel == pattern:
                    return True
        return False
