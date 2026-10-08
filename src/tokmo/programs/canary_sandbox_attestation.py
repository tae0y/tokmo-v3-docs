"""OS-bound proof that the current process cannot write the canonical root.

G2.5 v3 RCA follow-up. The earlier safety gate compared pre/post content
hashes of the canonical root. That cannot distinguish actors: the production
cadence legitimately mutates ``stores/latest_quotes.sqlite3``,
``stores/trade_ledger.sqlite3`` and ``stores/order_scan_state.sqlite3``
while the canary runs, so a hash change proves nothing about the canary.

The v3 primary non-mutation proof is therefore process-level, not
content-level: the canary runs under macOS ``/usr/bin/sandbox-exec`` with

    (version 1)
    (allow default)
    (deny file-write* (subpath "<canonical root>"))

and this module *verifies from inside that process* that the denial is
really in force. An environment variable is NOT proof — any caller can set
one — so nothing here reads the process environment. The proof is a bounded
sequence of real filesystem syscalls whose EPERM can only come from the
kernel's sandbox policy:

1. ``chmod(canonical_root, <its current mode>)`` — idempotent. Under the
   sandbox this is ``file-write-mode`` and fails EPERM. Without the sandbox
   it succeeds and changes nothing (the mode written is the mode read).
2. the same idempotent ``chmod`` against a nested, already-existing
   protected path, proving the deny covers the subpath and not just the
   root directory inode.
3. only once 1 and 2 have both been denied — at which point a write is
   known to be impossible — an ``O_CREAT|O_EXCL`` create of a unique
   probe file directly under the canonical root, which must also fail
   EPERM and must leave no artifact.

Step 3 is the one probe that *would* mutate the canonical root if the
sandbox were absent, so it is never reached in that case: step 1 already
returns ``denied=False`` and the caller refuses before any credential is
assembled. This is the fail-closed ordering required by the v3 contract.

Two non-sandbox sources of EPERM are excluded explicitly, because they
would otherwise forge a passing attestation:

- a canonical root owned by another uid (``chmod`` by a non-owner is EPERM
  regardless of sandbox) — rejected as ``not_owned_by_process_user``;
- a non-macOS platform, where ``sandbox-exec`` does not exist at all.

A read-only filesystem raises EROFS, not EPERM, and is likewise rejected
because only EPERM is accepted as evidence.

No secret, credential, symbol or raw exception text appears in the result —
only fixed probe names and verdict tokens.
"""

from dataclasses import dataclass
from pathlib import Path
import errno
import os
import stat
import sys
import time


# Mechanism token recorded in evidence when the attestation holds.
SANDBOX_MECHANISM = "macos_sandbox_exec_file_write_deny"

# Probe names, in execution order. Fixed tokens — safe for evidence.
PROBE_ROOT_IDEMPOTENT = "chmod_root_same_mode"
PROBE_NESTED_IDEMPOTENT = "chmod_nested_same_mode"
PROBE_CREATE_UNIQUE = "create_unique_file"

_PROBE_PREFIX = ".canary_write_deny_probe_"


@dataclass(frozen=True)
class CanonicalWriteDenyAttestation:
    """Result of the bounded OS write-denial proof.

    ``denied`` is the only field the safety adjudication may trust. It is
    True only when every probe that ran was refused by the kernel with
    EPERM and the create probe left no artifact behind.
    """

    denied: bool
    reason: str
    mechanism: str = ""
    canonical_root: str = ""
    probes: tuple = ()

    def to_dict(self) -> dict:
        return {
            "denied": self.denied,
            "reason": self.reason,
            "mechanism": self.mechanism,
            "canonical_root": self.canonical_root,
            "probes": [{"probe": name, "verdict": verdict}
                       for name, verdict in self.probes],
        }


def _refused(root: Path, reason: str, probes: tuple) -> CanonicalWriteDenyAttestation:
    return CanonicalWriteDenyAttestation(
        denied=False, reason=reason, mechanism="",
        canonical_root=str(root), probes=probes,
    )


def _eperm_or_none(action) -> "int | None":
    """Run ``action``; return the errno it raised, or None if it succeeded."""
    try:
        action()
    except OSError as error:
        return error.errno
    return None


def _nested_probe_target(root: Path,
                         nested_relatives: "tuple[str, ...]") -> "Path | None":
    """First existing nested path from the fixed relative allowlist.

    Falling back to any existing entry directly under the root keeps the
    proof meaningful on a canonical root that does not yet hold the
    predeclared stores.
    """
    for relative in nested_relatives:
        candidate = root / relative
        if candidate.exists():
            return candidate
    for child in sorted(root.iterdir()):
        return child
    return None


def _probe_id() -> str:
    """Unique, non-secret probe suffix for the create probe filename."""
    return f"{os.getpid()}_{time.monotonic_ns()}"


def attest_canonical_write_denied(
    canonical_root: Path, *,
    nested_relatives: "tuple[str, ...]" = (),
    probe_id: "str | None" = None,
) -> CanonicalWriteDenyAttestation:
    """Prove, from inside this process, that writes to ``canonical_root``
    are denied by the OS.

    Returns an attestation with ``denied=True`` only when the kernel
    refused every probe with EPERM. Every other outcome — including any
    probe unexpectedly succeeding — returns ``denied=False`` with a reason
    token, so callers fail closed.

    The function never mutates the canonical root: probes 1 and 2 write
    back exactly the mode they read, and probe 3 (the only creating probe)
    runs only after 1 and 2 have proven that creating is impossible.
    """
    root = Path(canonical_root)
    probes: tuple = ()

    if sys.platform != "darwin":
        return _refused(root, "not_macos", probes)

    if not root.exists() or not root.is_dir():
        return _refused(root, "canonical_root_absent", probes)

    root_stat = os.stat(str(root))
    if root_stat.st_uid != os.geteuid():
        # A non-owner chmod is EPERM with or without a sandbox, so EPERM
        # here would prove nothing.
        return _refused(root, "canonical_root_not_owned_by_process_user", probes)

    # ── Probe 1: idempotent chmod of the root itself ──────────────────
    root_mode = stat.S_IMODE(root_stat.st_mode)
    code = _eperm_or_none(lambda: os.chmod(str(root), root_mode))
    if code is None:
        probes += ((PROBE_ROOT_IDEMPOTENT, "allowed"),)
        return _refused(root, "root_write_allowed", probes)
    if code != errno.EPERM:
        probes += ((PROBE_ROOT_IDEMPOTENT, f"errno_{code}"),)
        return _refused(root, "root_probe_unexpected_errno", probes)
    probes += ((PROBE_ROOT_IDEMPOTENT, "eperm"),)

    # ── Probe 2: idempotent chmod of a nested existing path ───────────
    nested = _nested_probe_target(root, nested_relatives)
    if nested is None:
        return _refused(root, "no_nested_probe_target", probes)
    nested_stat = os.stat(str(nested))
    if nested_stat.st_uid != os.geteuid():
        return _refused(root, "nested_probe_not_owned_by_process_user", probes)
    nested_mode = stat.S_IMODE(nested_stat.st_mode)
    code = _eperm_or_none(lambda: os.chmod(str(nested), nested_mode))
    if code is None:
        probes += ((PROBE_NESTED_IDEMPOTENT, "allowed"),)
        return _refused(root, "nested_write_allowed", probes)
    if code != errno.EPERM:
        probes += ((PROBE_NESTED_IDEMPOTENT, f"errno_{code}"),)
        return _refused(root, "nested_probe_unexpected_errno", probes)
    probes += ((PROBE_NESTED_IDEMPOTENT, "eperm"),)

    # ── Probe 3: create a unique file (only now that writes are known
    #    impossible, so this can never mutate the canonical root) ──────
    target = root / f"{_PROBE_PREFIX}{probe_id or _probe_id()}"

    def _create():
        handle = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(handle)

    code = _eperm_or_none(_create)
    if code is None:
        probes += ((PROBE_CREATE_UNIQUE, "created"),)
        # Contradicts probes 1 and 2. Remove the artifact and fail closed.
        try:
            os.unlink(str(target))
        except OSError:
            pass
        return _refused(root, "create_probe_succeeded", probes)
    if code != errno.EPERM:
        probes += ((PROBE_CREATE_UNIQUE, f"errno_{code}"),)
        return _refused(root, "create_probe_unexpected_errno", probes)
    if target.exists():
        probes += ((PROBE_CREATE_UNIQUE, "artifact_present"),)
        return _refused(root, "create_probe_left_artifact", probes)
    probes += ((PROBE_CREATE_UNIQUE, "eperm_no_artifact"),)

    return CanonicalWriteDenyAttestation(
        denied=True,
        reason="all_write_probes_denied_by_os",
        mechanism=SANDBOX_MECHANISM,
        canonical_root=str(root),
        probes=probes,
    )
