"""G2.5 bounded live canary: config, deps, runner, fingerprint.

A separate canary harness that does NOT weaken pulse_main's structural
refusal for production mode. It runs exactly one market+symbol, quote-only,
for exactly 5 wall-clock 60-second ticks with a cumulative provider-start cap.

Design:
- CanaryConfigV1: explicit (market, symbol) authority. No union/implicit.
  max_ticks is hard-locked at exactly 5. max_provider_starts hard-clamped
  to <= CANARY_MAX_PROVIDER_STARTS_LIMIT (the family cumulative cap).
- CanaryDepsV1: minimal DI seam — clock, token_fetch, quotes_fetch,
  sleep_until. quotes_fetch receives an explicit bearer kwarg (B3).
  Execution/dialogue/AI/Telegram/orders/accounts/holdings structurally absent.
- run_canary_session: 5-tick bounded runner with:
  * True 60s wall-clock pacing via sleep_until DI seam (B2).
  * A fresh token per tick with explicit bearer pass-through (B3).
  * Write-ahead durable provider-start ledger bound to purpose identity (B4).
  * Immediate STOP on provider failure (B5).
  * Canonical fingerprint pre/post compare as actual gate (M1).
- fingerprint_directory: canonical invariant proof.
- CLI: exit 0 only for fresh all_ticks_complete (M2).
- B-1: campaign evidence root is an absolute anchor derived from Path.home()
  + immutable relative constant. It is outside canonical root, not
  caller-selectable, and not controllable via --root/--canary-base flags.
  Campaign ID and market/symbol are module constants, not CLI flags.

No live transport is imported here. All transport is injected.
The live transport composition lives in build_canary_live_deps() in
tools/run_tokmo_pulse_canary.py, which directly reuses the real
TossAuthLiveTransport / TossPricesLiveTransport and query_current_prices —
no build_production_cadence_deps, no claim=None.

Fixture composition (test/dev only) lives in tools/run_tokmo_pulse_canary_fixture.py.
The production src module does not contain fixture-to-TossCurrentPriceFact mapping.
"""

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Callable
import argparse
import hashlib
import json
import os
import sqlite3
import sys
import time

from tokmo.records.pulse_canary_evidence import CanaryEvidenceStore
from tokmo.values.session_gate import Market
from tokmo.programs.canary_safety_gate import (
    CanarySafetyGate,
    DEFAULT_PROTECTED_PATHS,
    DEFAULT_EXPECTED_MUTABLE_PATHS,
)
from tokmo.programs.canary_sandbox_attestation import (
    CanonicalWriteDenyAttestation,
    attest_canonical_write_denied,
)
from tokmo.programs.canary_cadence_attribution import (
    DEFAULT_ATTRIBUTION_GRACE,
    fresh_receipts,
    mtime_attributed,
    read_cadence_receipts,
    read_cadence_receipts_from_manifests,
)


# ── Config ────────────────────────────────────────────────────────────

CANARY_MAX_TICKS = 5
CANARY_MAX_PROVIDER_STARTS_LIMIT = 32
CANARY_MAX_PROVIDER_STARTS_DEFAULT = 32

# M-1: Grace period for past-slot detection — must be < tick interval (60s).
# Any slot whose observed time is past scheduled+5s is MISSED, not replayed.
_PAST_SLOT_GRACE_SECONDS = 5

# ── Campaign family (v4 minimal verification) ─────────────────────────
# The family identity is stable across all versions.  User-approved v4
# reuses the existing runner for one additional 5-tick verification only:
# inherited v1-v3=22 plus local TOKEN5+PRICE_BATCH5=10 => exact 32/32.
CANARY_FAMILY_ID = "g25-quote-kr-005930"
CANARY_FAMILY_CUMULATIVE_CAP = 32

# v4 campaign contract (current).
CANARY_CAMPAIGN_ID = "g25-quote-kr-005930-v4"
CANARY_CAMPAIGN_VERSION = 4
CANARY_CAMPAIGN_MARKET = Market.KR
CANARY_CAMPAIGN_SYMBOL = "005930"

# B-1: Immutable relative path for the campaign evidence root anchor.
# Derived from Path.home() at runtime — never hardcoded as a user home string.
# This path is outside canonical root and not caller-selectable.
_CAMPAIGN_EVIDENCE_REL = Path(".local") / "share" / "tokmo-v3-canary" \
    / "campaigns" / CANARY_CAMPAIGN_ID \
    / CANARY_CAMPAIGN_MARKET.value / CANARY_CAMPAIGN_SYMBOL

# v3: the canonical (production) root the canary must never write. Also
# derived from Path.home() + an immutable relative constant, so the live
# CLI knows the true target of the write-deny attestation independently
# of whatever --canonical-root the caller passes.
_CANONICAL_ROOT_REL = Path(".local") / "share" / "tokmo-v3"


@dataclass(frozen=True)
class CanaryConfigV1:
    """Explicit selector: one market, one symbol. No union, no implicit.

    max_ticks is hard-locked at exactly 5 (M3).
    max_provider_starts is hard-clamped to the family cumulative cap (M3).
    """

    market: Market
    symbol: str
    max_ticks: int = CANARY_MAX_TICKS
    max_provider_starts: int = CANARY_MAX_PROVIDER_STARTS_DEFAULT

    def __post_init__(self):
        if not isinstance(self.market, Market):
            raise ValueError("market must be a Market enum")
        if type(self.symbol) is not str or not self.symbol:
            raise ValueError("symbol must be a non-empty string")
        if type(self.max_ticks) is not int:
            raise ValueError("max_ticks must be int")
        if self.max_ticks != CANARY_MAX_TICKS:
            raise ValueError(
                f"max_ticks must be exactly {CANARY_MAX_TICKS}")
        if type(self.max_provider_starts) is not int:
            raise ValueError("max_provider_starts must be int")
        if self.max_provider_starts <= 0:
            raise ValueError("max_provider_starts must be positive")
        if self.max_provider_starts > CANARY_MAX_PROVIDER_STARTS_LIMIT:
            raise ValueError(
                f"max_provider_starts {self.max_provider_starts} exceeds "
                f"hard limit {CANARY_MAX_PROVIDER_STARTS_LIMIT}")


# ── Deps ──────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanaryDepsV1:
    """Minimal DI seam: clock, token_fetch, quotes_fetch, sleep_until.

    quotes_fetch signature: (market, symbols, *, bearer) — the tick's own
    bearer is passed explicitly to every quote request (B3). No hidden
    token call inside quotes_fetch.

    sleep_until signature: (target_utc: datetime) -> None — the runner
    uses this to wait until the scheduled tick time (B2). Tests inject
    a virtual clock.

    Forbidden seams (structurally absent):
    orders_page_fetch, order_detail_fetch, telegram_sender, ai_evaluator,
    dialogue_store, updates_provider, holdings_reader, accounts_fetch,
    ledger_store, scan_state_store, native_refs_reader.
    """

    clock: Callable
    token_fetch: Callable
    quotes_fetch: Callable
    sleep_until: "Callable[[datetime], None] | None" = None


# ── Result ────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class CanarySessionResult:
    session_id: str
    completed_ticks: int
    total_provider_starts: int
    stop_reason: str
    ticks: tuple = ()
    is_fresh: bool = True
    inherited_provider_starts: int = 0
    local_provider_starts: int = 0
    # v3: OS-level proof that this process could not write the canonical
    # root. True only when a real EPERM probe sequence succeeded — never
    # from an environment variable or any other caller-settable signal.
    canary_canonical_write_denied_by_os: bool = False
    write_deny_mechanism: str = ""
    write_deny_reason: str = ""
    # Cadence receipts that attributed protected-path changes, if any.
    cadence_attribution: tuple = ()


# ── Fingerprint ───────────────────────────────────────────────────────

def fingerprint_directory(root: Path) -> str:
    """Compute a deterministic SHA-256 fingerprint of a directory tree.

    Includes relative path, size, and content hash of each regular file.
    Used to prove the canary does not mutate the canonical root.
    """
    root = Path(root)
    hasher = hashlib.sha256()
    if not root.exists():
        return hasher.hexdigest()
    for path in sorted(root.rglob("*")):
        if path.is_file():
            rel = path.relative_to(root).as_posix()
            stat = path.stat()
            file_hash = hashlib.sha256(path.read_bytes()).hexdigest()
            hasher.update(f"{rel}\\0{stat.st_size}\\0{file_hash}\\0".encode())
    return hasher.hexdigest()


# ── Root validation (M5) ─────────────────────────────────────────────

def _validate_evidence_root(root: Path) -> Path:
    """Ensure the evidence root is a real private directory.

    M5: no symlink, 0700 mode, disjoint from canonical root.
    """
    root = Path(root)
    # Resolve for comparison but don't follow symlinks for the path itself
    real = root.resolve()
    if os.path.islink(str(root)):
        raise ValueError("evidence_root_must_not_be_symlink")
    if not real.exists():
        real.mkdir(mode=0o700, parents=True, exist_ok=True)
    st = os.lstat(str(real))
    if not st.st_mode & 0o170000 == 0o040000:
        raise ValueError("evidence_root_must_be_directory")
    # Enforce 0700
    current_mode = st.st_mode & 0o777
    if current_mode != 0o700:
        os.chmod(str(real), 0o700)
    return real


def _validate_root_disjoint(evidence_root: Path,
                            canonical_root: "Path | None") -> None:
    """M5: evidence root and canonical root must not contain each other."""
    if canonical_root is None:
        return
    ev = evidence_root.resolve()
    ca = Path(canonical_root).resolve()
    if ev == ca:
        raise ValueError("evidence_root_equals_canonical_root")
    # Check containment in both directions
    if ev in ca.parents or ca in ev.parents:
        raise ValueError("evidence_root_overlaps_canonical_root")
    if os.path.islink(str(canonical_root)):
        raise ValueError("canonical_root_must_not_be_symlink")


# ── Campaign evidence root anchor (B-1) ───────────────────────────────

def campaign_evidence_anchor_root() -> Path:
    """B-1: Derive the absolute, deterministic campaign evidence root.

    The path is: Path.home() / .local/share/tokmo-v3-canary/campaigns/
                  <campaign_id>/<market>/<symbol>

    This is an immutable anchor derived from Path.home() + a constant
    relative path. It is:
    - Outside canonical root (always under ~/.local/share/tokmo-v3-canary/)
    - Not caller-selectable (no CLI flag can change it)
    - Not a hardcoded user home string (uses Path.home())
    - Deterministic for this approved campaign contract

    The purpose identity and ledger path use the same immutable campaign
    contract. No prefix/root/campaign flag can create a new ledger.
    """
    return Path.home() / _CAMPAIGN_EVIDENCE_REL


def canonical_root_anchor() -> Path:
    """v3 M-6: the one canonical root the live CLI will attest against.

    Derived from Path.home() + an immutable relative constant, exactly
    like the evidence anchor. --canonical-root must equal this; a
    caller-chosen path could be a decoy the process happens to be unable
    to write, which would forge a passing write-deny attestation.
    """
    return Path.home() / _CANONICAL_ROOT_REL


# ── Purpose identity (B4/B-1) ────────────────────────────────────────

def compute_purpose_identity(market: Market, symbol: str) -> str:
    """Exact purpose identity bound to approved campaign contract.

    The purpose ledger is cumulative across sessions/restarts and
    cannot be re-armed by a new session, root, or t0 (B4/B-1).

    Identity = campaign_id + campaign_version + market + symbol.
    market/symbol are the only variable components but in live mode they
    are enforced to be exactly CANARY_CAMPAIGN_MARKET/CANARY_CAMPAIGN_SYMBOL.
    """
    return f"canary|{CANARY_CAMPAIGN_ID}|v{CANARY_CAMPAIGN_VERSION}|{market.value}|{symbol}"


def compute_campaign_session_id(market: Market, symbol: str) -> str:
    """B-1: Campaign-based session_id — not root/t0-based.

    Same campaign => same session => cap persists across t0/root changes.
    """
    identity = f"{CANARY_CAMPAIGN_ID}|v{CANARY_CAMPAIGN_VERSION}|{market.value}|{symbol}"
    session_hash = hashlib.sha256(identity.encode()).hexdigest()[:16]
    return f"canary-{CANARY_CAMPAIGN_ID}-v{CANARY_CAMPAIGN_VERSION}-{session_hash}"


# ── Campaign family and inherited accounting (v2) ─────────────────────

@dataclass(frozen=True)
class PriorCampaignSpec:
    """Fixed allowlist entry for a prior campaign ledger to read
    inherited provider-start usage from. The allowlist is immutable —
    callers cannot add entries. This prevents resetting same-purpose
    usage by switching roots or versions."""
    campaign_id: str
    version: int


# Immutable allowlist of prior campaign ledgers in the same family.
# v4 reads durable provider_start_intent rows from each to compute
# inherited usage. No caller-selectable additions.
_PRIOR_CAMPAIGN_SPECS: tuple[PriorCampaignSpec, ...] = (
    PriorCampaignSpec("g25-quote-kr-005930-v1", 1),
    PriorCampaignSpec("g25-quote-kr-005930-v2", 2),
    PriorCampaignSpec("g25-quote-kr-005930-v3", 3),
)


def compute_family_identity(market: Market, symbol: str) -> str:
    """Family identity stable across campaign versions.

    This groups all versions (v1, v2, ...) of the same campaign into
    one cumulative accounting family. The identity excludes version
    so it never changes across versions.
    """
    return f"canary-family|{CANARY_FAMILY_ID}|{market.value}|{symbol}"


def _prior_ledger_path(spec: PriorCampaignSpec, market: Market,
                       symbol: str) -> Path:
    """Derive the evidence DB path for a prior campaign ledger."""
    return (Path.home() / ".local" / "share" / "tokmo-v3-canary"
            / "campaigns" / spec.campaign_id
            / market.value / symbol / "ops" / "canary_evidence.sqlite3")


def read_inherited_provider_starts(market: Market, symbol: str) -> int:
    """Read cumulative provider-start usage from all prior campaign
    ledgers in the family allowlist.

    Returns the sum of provider_start_intent rows across all prior
    campaigns. If no prior DBs exist, returns 0.

    This is read-only — it never opens the DBs for write. The prior
    ledgers are immutable evidence.
    """
    total = 0
    for spec in _PRIOR_CAMPAIGN_SPECS:
        db_path = _prior_ledger_path(spec, market, symbol)
        if not db_path.exists():
            continue
        try:
            conn = sqlite3.connect(
                f"file:{db_path}?mode=ro", uri=True, timeout=1.0)
            try:
                row = conn.execute(
                    "SELECT COUNT(*) FROM provider_start_intent"
                ).fetchone()
                total += int(row[0])
            finally:
                conn.close()
        except sqlite3.Error:
            continue
    return total


# ── Safety adjudication (v3) ──────────────────────────────────────────

def adjudicate_safety_gate(gate_result, *, write_denied: bool,
                           cadence_receipt_reader: "Callable | None",
                           window_start: datetime,
                           window_end: datetime,
                           ) -> "tuple[str, tuple]":
    """Decide whether a failing safety gate is a real canary violation.

    Returns ``(stop_reason, cadence_attribution)``. An empty stop_reason
    means the changes are explained and the session may still pass.

    Contract (v3 reclosure):

    - Every changed path — protected or unattributed — must individually
      have its post-run mtime covered by a fresh successful cadence
      receipt's activity interval. A blanket fresh-receipt waiver for
      unattributed changes is NOT sufficient; each path needs its own
      mtime attribution.

    - Protected-path mutations are excused only when the OS proved this
      process could not write the canonical root AND each mutated path's
      mtime falls inside a fresh receipt interval.

    - Unattributed changes are excused only under the same two conditions:
      OS write-deny AND each path's mtime inside a fresh receipt interval.
      Without OS proof, unattributed changes always fail closed.

    - Missing, stale, or non-covering receipts fail closed for every path
      they don't cover.

    The OS proof alone is deliberately not sufficient: it establishes only
    that the canary is not the writer, not that the cadence is. The receipt
    supplies the positive attribution, and the mtime bound proves the
    receipt's activity interval explains that specific file's change.
    """
    if gate_result.verdict != "fail":
        return "", ()

    # No OS write-deny: standard fail-closed behavior
    if not write_denied:
        if gate_result.unattributed_changes:
            return "safety_gate_unattributed_change", ()
        if not gate_result.protected_mutations:
            return "", ()
        return "safety_gate_protected_mutation", ()

    # OS proved this process can't write canonical root.
    # Each changed path still needs individual mtime attribution.
    receipts = (cadence_receipt_reader()
                if cadence_receipt_reader is not None else ())
    covering = fresh_receipts(
        receipts, window_start=window_start, window_end=window_end,
        grace=DEFAULT_ATTRIBUTION_GRACE)
    if not covering:
        if gate_result.protected_mutations:
            return "safety_gate_protected_mutation_no_fresh_receipt", ()
        return "safety_gate_unattributed_change", ()

    # Check EACH protected mutation mtime against receipt intervals
    if gate_result.protected_mutation_mtimes:
        unexplained = [
            rel for rel, mtime_ns in gate_result.protected_mutation_mtimes
            if not mtime_attributed(
                covering,
                datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc),
                grace=DEFAULT_ATTRIBUTION_GRACE)
        ]
        if unexplained:
            return "safety_gate_protected_mutation_outside_cadence_window", ()

    # Check EACH unattributed mtime against receipt intervals.
    # If mtimes are missing (older gate), fail closed — can't attribute
    # without a timestamp.
    if gate_result.unattributed_changes:
        unattributed_mtimes = getattr(
            gate_result, "unattributed_mutation_mtimes", ())
        if not unattributed_mtimes:
            return "safety_gate_unattributed_change", ()
        unexplained = [
            rel for rel, mtime_ns in unattributed_mtimes
            if not mtime_attributed(
                covering,
                datetime.fromtimestamp(mtime_ns / 1_000_000_000, tz=timezone.utc),
                grace=DEFAULT_ATTRIBUTION_GRACE)
        ]
        if unexplained:
            return "safety_gate_unattributed_change_outside_cadence_window", ()

    return "", tuple(receipt.to_dict() for receipt in covering)


# ── Runner ────────────────────────────────────────────────────────────

def run_canary_session(*, root: Path, config: CanaryConfigV1,
                       deps: CanaryDepsV1,
                       t0: datetime,
                       session_id: "str | None" = None,
                       canonical_root: "Path | None" = None,
                       protected_paths: "tuple[str, ...] | None" = None,
                       expected_mutable_paths: "tuple[str, ...] | None" = None,
                       inherited_provider_starts: int = 0,
                       write_deny_attestation:
                           "CanonicalWriteDenyAttestation | None" = None,
                       cadence_receipt_reader: "Callable | None" = None,
                       ) -> CanarySessionResult:
    """Run a bounded 5-tick canary session.

    - First tick is t0; subsequent ticks at t0+60, t0+120, ...
    - True 60s wall-clock pacing via deps.sleep_until (B2).
    - One TOKEN start per tick, minted just before its PRICE_BATCH (B3).
    - Bearer passed explicitly to quotes_fetch (B3).
    - Write-ahead durable provider-start ledger (B4).
    - Immediate STOP on provider failure (B5).
    - Canonical fingerprint pre/post compare as actual gate (M1).
    - Completed ticks are durable and never re-executed on restart.
    - Past slots (observed > scheduled + grace) are MISSED, not replayed (B2).
    - M-1: missed_count is derived from durable evidence ticks across all
      prior invocations, not an in-memory counter.

    Safety gate (post-G2.5 RCA repair):
    - If protected_paths is provided, the new CanarySafetyGate is used
      instead of the legacy full-root fingerprint_directory(). This
      tolerates concurrent scheduler-owned writes to expected_mutable_paths
      while failing closed on protected path mutation or unattributed change.
    - If protected_paths is None and canonical_root is provided, the
      legacy full-root fingerprint_directory() is used (backward-compat).

    v3 safety adjudication (write_deny_attestation / cadence_receipt_reader):
    - write_deny_attestation carries the OS-bound proof produced by
      canary_sandbox_attestation.attest_canonical_write_denied(). Only
      attestation.denied is trusted; None or denied=False means the v2
      fail-closed behaviour applies unchanged.
    - cadence_receipt_reader is a zero-argument callable returning durable
      CadenceReceiptWindow rows. Protected-path changes are excused only
      when the OS proof holds AND a fresh receipt's activity interval
      contains the mutated path's post-run mtime. Missing, stale or
      non-covering receipts fail closed.
    - Unattributed changes are excused only when OS write-deny holds AND
      each path's mtime falls inside a fresh receipt interval. Without
      OS proof or without per-path mtime coverage, they fail closed.
    """
    evidence_root = _validate_evidence_root(root)
    _validate_root_disjoint(evidence_root, canonical_root)

    if (not isinstance(t0, datetime) or t0.tzinfo is None
            or t0.utcoffset() != timedelta(0)):
        raise ValueError("t0 must be aware UTC")
    if t0.microsecond != 0:
        raise ValueError("t0 must be a whole second")
    if t0.second % 60 != 0:
        raise ValueError("t0 must be aligned to a 60-second grid")

    if session_id is None:
        # B-1: session_id is campaign-based (not root/t0-based).
        session_id = compute_campaign_session_id(config.market, config.symbol)

    evidence = CanaryEvidenceStore(
        evidence_root / "ops" / "canary_evidence.sqlite3",
        clock=deps.clock,
    )

    # Inherited provider starts from prior campaign family members (v2).
    # This is passed in by the CLI (read from prior campaign ledgers) or
    # by tests directly. The family cumulative cap check uses this.
    inherited = inherited_provider_starts

    # v3: only a real OS-bound attestation counts. A missing attestation is
    # indistinguishable from an unsandboxed run, so it is treated as "the
    # canary could have written" and the v2 fail-closed gate applies.
    write_denied = (write_deny_attestation is not None
                    and write_deny_attestation.denied is True)
    write_deny_mechanism = (write_deny_attestation.mechanism
                            if write_denied else "")
    write_deny_reason = (write_deny_attestation.reason
                         if write_deny_attestation is not None else
                         "no_attestation_supplied")

    # Purpose identity for the write-ahead provider-start ledger (B4/B-1)
    purpose = compute_purpose_identity(config.market, config.symbol)

    # Compute canonical fingerprint (pre) if canonical root provided.
    # If protected_paths is provided, use the new CanarySafetyGate which
    # tolerates concurrent scheduler-owned writes while failing closed on
    # protected path mutation or unattributed change.
    # Otherwise, use the legacy full-root fingerprint_directory (back-compat).
    fingerprint_pre = None
    safety_gate = None
    if canonical_root is not None and protected_paths is not None:
        safety_gate = CanarySafetyGate(
            root=canonical_root,
            protected_paths=protected_paths,
            expected_mutable_paths=expected_mutable_paths,
        )
        safety_gate.snapshot_pre()
        # Store the diagnostic pre hash as the fingerprint for evidence record
        fingerprint_pre = safety_gate._pre_root_hash
    elif canonical_root is not None:
        fingerprint_pre = fingerprint_directory(canonical_root)

    # Check for existing session progress
    existing = evidence.read_session(session_id)
    if existing is not None:
        completed = evidence.completed_tick_indices(session_id)
        cumulative = evidence.cumulative_provider_starts_by_purpose(purpose)

        # M3: verify stored config matches current config
        if (existing["max_ticks"] != config.max_ticks
                or existing["max_provider_starts"] != config.max_provider_starts):
            evidence.finish_session(
                session_id, completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                stop_reason="config_mismatch_refusal",
            )
            ticks = evidence.read_ticks(session_id)
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="config_mismatch_refusal",
                ticks=ticks,
                is_fresh=False,
            )
    else:
        completed = ()
        cumulative = 0
        evidence.start_session(
            session_id=session_id,
            market=config.market.value, symbol=config.symbol,
            max_ticks=config.max_ticks,
            max_provider_starts=config.max_provider_starts,
            t0_utc=t0, canonical_fingerprint_pre=fingerprint_pre,
        )

    # Check if already complete
    if len(completed) >= config.max_ticks:
        # M1: fingerprint/safety-gate compare on completion rerun
        fingerprint_post = None
        fingerprint_ok = True
        if safety_gate is not None:
            gate_result = safety_gate.evaluate_post()
            fingerprint_post = gate_result.full_root_post_hash
            stop_reason, _attribution = adjudicate_safety_gate(
                gate_result, write_denied=write_denied,
                cadence_receipt_reader=cadence_receipt_reader,
                window_start=t0, window_end=deps.clock(),
            )
            if stop_reason:
                fingerprint_ok = False
                evidence.finish_session(
                    session_id, completed_ticks=len(completed),
                    total_provider_starts=inherited + cumulative,
                    stop_reason=stop_reason,
                    canonical_fingerprint_post=fingerprint_post,
                )
                ticks = evidence.read_ticks(session_id)
                return CanarySessionResult(
                    session_id=session_id,
                    completed_ticks=len(completed),
                    total_provider_starts=inherited + cumulative,
                    inherited_provider_starts=inherited,
                    local_provider_starts=cumulative,
                    canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                    stop_reason=stop_reason,
                    ticks=ticks,
                    is_fresh=False,
                )
        elif canonical_root is not None:
            fingerprint_post = fingerprint_directory(canonical_root)
            stored_pre = existing.get("canonical_fingerprint_pre") if existing else fingerprint_pre
            if stored_pre is not None and fingerprint_post != stored_pre:
                fingerprint_ok = False
                evidence.finish_session(
                    session_id, completed_ticks=len(completed),
                    total_provider_starts=inherited + cumulative,
                    stop_reason="canonical_fingerprint_mutated",
                    canonical_fingerprint_post=fingerprint_post,
                )
                ticks = evidence.read_ticks(session_id)
                return CanarySessionResult(
                    session_id=session_id,
                    completed_ticks=len(completed),
                    total_provider_starts=inherited + cumulative,
                    inherited_provider_starts=inherited,
                    local_provider_starts=cumulative,
                    canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                    stop_reason="canonical_fingerprint_mutated",
                    ticks=ticks,
                    is_fresh=False,
                )
        evidence.finish_session(
            session_id, completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            stop_reason="already_complete",
            canonical_fingerprint_post=fingerprint_post,
        )
        ticks = evidence.read_ticks(session_id)
        return CanarySessionResult(
            session_id=session_id,
            completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            inherited_provider_starts=inherited,
            local_provider_starts=cumulative,
            canary_canonical_write_denied_by_os=write_denied,
            write_deny_mechanism=write_deny_mechanism,
            write_deny_reason=write_deny_reason,
            stop_reason="already_complete",
            ticks=ticks,
            is_fresh=False,
        )

    # M-1: Derive missed_count from durable evidence ticks across ALL
    # prior invocations for this session. This ensures that even after
    # a crash boundary, durable MISSED ticks are counted and the final
    # outcome is never all_ticks_complete if any MISSED exists.
    prior_ticks = evidence.read_ticks(session_id)
    durable_missed_count = sum(
        1 for t in prior_ticks if t.get("outcome") == "missed_past_slot")

    # Check cap before starting (B4: purpose ledger is authoritative)
    if inherited + cumulative >= config.max_provider_starts:
        evidence.finish_session(
            session_id, completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            stop_reason="provider_start_cap_exceeded",
        )
        ticks = evidence.read_ticks(session_id)
        return CanarySessionResult(
            session_id=session_id,
            completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            inherited_provider_starts=inherited,
            local_provider_starts=cumulative,
            canary_canonical_write_denied_by_os=write_denied,
            write_deny_mechanism=write_deny_mechanism,
            write_deny_reason=write_deny_reason,
            stop_reason="provider_start_cap_exceeded",
            ticks=ticks,
        )

    # v3 pre-session feasibility check: token-per-tick needs max_ticks * 2
    # starts (TOKEN + PRICE_BATCH per tick). If inherited + needed > cap,
    # the full session cannot complete — stop before any outbound call.
    v3_needed_starts = config.max_ticks * 2
    if inherited + v3_needed_starts > config.max_provider_starts:
        evidence.finish_session(
            session_id, completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            stop_reason="provider_start_cap_exceeded",
        )
        ticks = evidence.read_ticks(session_id)
        return CanarySessionResult(
            session_id=session_id,
            completed_ticks=len(completed),
            total_provider_starts=inherited + cumulative,
            inherited_provider_starts=inherited,
            local_provider_starts=cumulative,
            canary_canonical_write_denied_by_os=write_denied,
            write_deny_mechanism=write_deny_mechanism,
            write_deny_reason=write_deny_reason,
            stop_reason="provider_start_cap_exceeded",
            ticks=ticks,
        )

    # v3: Token fetched per-tick (not amortized). This avoids concurrent
    # cadence token rotation invalidating an amortized bearer.
    # Each tick: TOKEN immediately before PRICE_BATCH.

    tick_results = []
    sleep_fn = deps.sleep_until if deps.sleep_until is not None else _real_sleep_until
    # M-1: local_missed tracks missed slots in THIS invocation only.
    # durable_missed_count already includes prior-invocation MISSED.
    local_missed = 0

    for tick_index in range(config.max_ticks):
        # Skip already-completed ticks (durable progress)
        if tick_index in completed:
            continue

        scheduled = t0 + timedelta(seconds=60 * tick_index)
        observed = deps.clock()

        # B2: past-slot detection — if observed is far past scheduled,
        # the slot is MISSED, not replayed
        lag = (observed - scheduled).total_seconds()
        if lag > _PAST_SLOT_GRACE_SECONDS:
            # Record as MISSED and continue to next future slot
            evidence.record_tick(
                session_id=session_id, tick_index=tick_index,
                scheduled_utc=scheduled, observed_utc=observed,
                market=config.market.value, symbol=config.symbol,
                price=None, currency=None, source=None,
                quote_timestamp=None, quote_fetched_at=None,
                token_started=0, price_batch_started=0,
                provider_start_count=0,
                cumulative_provider_starts=cumulative,
                duration_ms=0, lag_seconds=max(0.0, lag),
                outcome="missed_past_slot", reason="past_slot_not_replayed",
            )
            tick_results.append({
                "tick_index": tick_index,
                "outcome": "missed_past_slot",
                "reason": "past_slot_not_replayed",
            })
            local_missed += 1
            continue

        # B2: true wall-clock pacing — wait until scheduled time
        if observed < scheduled:
            sleep_fn(scheduled)
            observed = deps.clock()

        start_time = time.monotonic()

        # v3: Token per tick — fetch fresh token immediately before PRICE_BATCH
        token_started = 0
        price_batch_started = 0

        if inherited + cumulative + 1 > config.max_provider_starts:
            evidence.finish_session(
                session_id, completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                stop_reason="provider_start_cap_exceeded",
            )
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="provider_start_cap_exceeded",
                ticks=tuple(tick_results),
            )
        # B4: write-ahead intent before TOKEN call
        intent_id = evidence.record_provider_start_intent(
            purpose_identity=purpose, operation="TOKEN",
            session_id=session_id, tick_index=tick_index,
        )
        cumulative = evidence.cumulative_provider_starts_by_purpose(purpose)
        try:
            token_value = deps.token_fetch()
            evidence.settle_provider_start_intent(
                intent_id, outcome="success")
        except Exception:
            evidence.settle_provider_start_intent(
                intent_id, outcome="token_failed")
            # B5: provider failure => immediate STOP
            evidence.record_tick(
                session_id=session_id, tick_index=tick_index,
                scheduled_utc=scheduled, observed_utc=observed,
                market=config.market.value, symbol=config.symbol,
                price=None, currency=None, source=None,
                quote_timestamp=None, quote_fetched_at=None,
                token_started=1, price_batch_started=0,
                provider_start_count=1,
                cumulative_provider_starts=cumulative,
                duration_ms=int((time.monotonic() - start_time) * 1000),
                lag_seconds=max(0.0, lag),
                outcome="failed", reason="token_fetch_exception",
            )
            evidence.finish_session(
                session_id, completed_ticks=tick_index,
                total_provider_starts=inherited + cumulative,
                stop_reason="provider_failed",
            )
            ticks = evidence.read_ticks(session_id)
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=tick_index,
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="provider_failed",
                ticks=ticks,
            )
        token_started = 1

        # PRICE_BATCH
        if inherited + cumulative + 1 > config.max_provider_starts:
            evidence.finish_session(
                session_id, completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                stop_reason="provider_start_cap_exceeded",
            )
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=len(completed),
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="provider_start_cap_exceeded",
                ticks=tuple(tick_results),
            )

        # B4: write-ahead intent before PRICE_BATCH
        intent_id = evidence.record_provider_start_intent(
            purpose_identity=purpose, operation="PRICE_BATCH",
            session_id=session_id, tick_index=tick_index,
        )
        cumulative = evidence.cumulative_provider_starts_by_purpose(purpose)
        price_batch_started = 1

        provider_start_count = token_started + price_batch_started

        # v3: token_value is fresh per-tick
        bearer = token_value
        try:
            outcome = deps.quotes_fetch(config.market, (config.symbol,),
                                        bearer=bearer)
        except Exception:
            outcome = None

        duration_ms = int((time.monotonic() - start_time) * 1000)

        # Extract sanitized quote data
        price = None
        currency = None
        source = None
        quote_timestamp = None
        quote_fetched_at = None
        outcome_str = "error"
        reason = "fetch_exception"

        if outcome is not None:
            code = getattr(outcome, "code", None)
            facts = getattr(outcome, "facts", ())
            if code == "success" and facts:
                fact = facts[0]
                price = str(getattr(fact, "last_price", None) or "")
                currency = getattr(fact, "currency_raw", None)
                source = getattr(fact, "source", None)
                quote_timestamp = getattr(fact, "timestamp", None)
                quote_fetched_at = getattr(fact, "fetched_at", None)
                outcome_str = "success"
                reason = None
                evidence.settle_provider_start_intent(
                    intent_id, outcome="success")
            elif code:
                outcome_str = "failed"
                reason = f"provider_{code}"
                evidence.settle_provider_start_intent(
                    intent_id, outcome=f"failed_{code}")
            else:
                outcome_str = "failed"
                reason = "unknown_outcome"
                evidence.settle_provider_start_intent(
                    intent_id, outcome="failed_unknown")
        else:
            evidence.settle_provider_start_intent(
                intent_id, outcome="fetch_exception")

        evidence.record_tick(
            session_id=session_id, tick_index=tick_index,
            scheduled_utc=scheduled, observed_utc=observed,
            market=config.market.value, symbol=config.symbol,
            price=price, currency=currency, source=source,
            quote_timestamp=quote_timestamp,
            quote_fetched_at=quote_fetched_at,
            token_started=token_started,
            price_batch_started=price_batch_started,
            provider_start_count=provider_start_count,
            cumulative_provider_starts=cumulative,
            duration_ms=duration_ms,
            lag_seconds=max(0.0, lag),
            outcome=outcome_str, reason=reason,
        )
        tick_results.append({
            "tick_index": tick_index,
            "outcome": outcome_str,
            "reason": reason,
        })

        # B5: provider failure => immediate STOP, no next tick
        if outcome_str != "success":
            evidence.finish_session(
                session_id, completed_ticks=tick_index + 1,
                total_provider_starts=inherited + cumulative,
                stop_reason="provider_failed",
            )
            ticks = evidence.read_ticks(session_id)
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=tick_index + 1,
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="provider_failed",
                ticks=ticks,
            )

        # Check cap after tick
        if inherited + cumulative >= config.max_provider_starts and \
                tick_index + 1 < config.max_ticks:
            evidence.finish_session(
                session_id, completed_ticks=tick_index + 1,
                total_provider_starts=inherited + cumulative,
                stop_reason="provider_start_cap_exceeded",
            )
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=tick_index + 1,
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="provider_start_cap_exceeded",
                ticks=tuple(tick_results),
            )

    # All ticks completed — safety gate adjudication.
    #
    # v3 rule. The gate reports two change classes:
    #
    # - unattributed changes always fail closed. No attestation and no
    #   receipt excuses a file nobody declared.
    # - protected-path mutations are excused only when BOTH the OS proved
    #   this process could not write the canonical root AND a fresh,
    #   timestamped cadence receipt covers the mutation's own mtime. The
    #   hash says a byte changed; it never says who changed it, so the
    #   actor claim rests on the OS proof plus the cadence's own receipt.
    #
    # Missing, stale, or non-covering receipts therefore fail closed even
    # under a valid attestation.
    fingerprint_post = None
    cadence_attribution: tuple = ()
    if safety_gate is not None:
        gate_result = safety_gate.evaluate_post()
        fingerprint_post = gate_result.full_root_post_hash
        stop_reason, cadence_attribution = adjudicate_safety_gate(
            gate_result, write_denied=write_denied,
            cadence_receipt_reader=cadence_receipt_reader,
            window_start=t0, window_end=deps.clock(),
        )
        if stop_reason:
            evidence.finish_session(
                session_id, completed_ticks=config.max_ticks,
                total_provider_starts=inherited + cumulative,
                stop_reason=stop_reason,
                canonical_fingerprint_post=fingerprint_post,
            )
            ticks = evidence.read_ticks(session_id)
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=config.max_ticks,
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason=stop_reason,
                ticks=ticks,
            )
    elif canonical_root is not None:
        fingerprint_post = fingerprint_directory(canonical_root)
        # Compare against stored pre from session
        session_data = evidence.read_session(session_id)
        stored_pre = (session_data.get("canonical_fingerprint_pre")
                      if session_data else fingerprint_pre)
        if stored_pre is not None and fingerprint_post != stored_pre:
            evidence.finish_session(
                session_id, completed_ticks=config.max_ticks,
                total_provider_starts=inherited + cumulative,
                stop_reason="canonical_fingerprint_mutated",
                canonical_fingerprint_post=fingerprint_post,
            )
            ticks = evidence.read_ticks(session_id)
            return CanarySessionResult(
                session_id=session_id,
                completed_ticks=config.max_ticks,
                total_provider_starts=inherited + cumulative,
                inherited_provider_starts=inherited,
                local_provider_starts=cumulative,
                canary_canonical_write_denied_by_os=write_denied,
                write_deny_mechanism=write_deny_mechanism,
                write_deny_reason=write_deny_reason,
                stop_reason="canonical_fingerprint_mutated",
                ticks=ticks,
            )

    # M-1: if any past slots were missed (durable + local), the campaign
    # FAILS (nonzero). Do NOT report all_ticks_complete when slots were
    # missed — even if later future ticks succeed after a crash boundary.
    total_missed = durable_missed_count + local_missed
    if total_missed > 0:
        evidence.finish_session(
            session_id, completed_ticks=config.max_ticks - total_missed,
            total_provider_starts=inherited + cumulative,
            stop_reason="missed_past_slots",
            canonical_fingerprint_post=fingerprint_post,
        )
        ticks = evidence.read_ticks(session_id)
        return CanarySessionResult(
            session_id=session_id,
            completed_ticks=config.max_ticks - total_missed,
            total_provider_starts=inherited + cumulative,
            inherited_provider_starts=inherited,
            local_provider_starts=cumulative,
            canary_canonical_write_denied_by_os=write_denied,
            write_deny_mechanism=write_deny_mechanism,
            write_deny_reason=write_deny_reason,
            stop_reason="missed_past_slots",
            ticks=ticks,
        )

    evidence.finish_session(
        session_id, completed_ticks=config.max_ticks,
        total_provider_starts=inherited + cumulative,
        stop_reason="all_ticks_complete",
        canonical_fingerprint_post=fingerprint_post,
    )
    ticks = evidence.read_ticks(session_id)
    return CanarySessionResult(
        session_id=session_id,
        completed_ticks=config.max_ticks,
        total_provider_starts=inherited + cumulative,
        inherited_provider_starts=inherited,
        local_provider_starts=cumulative,
        canary_canonical_write_denied_by_os=write_denied,
        write_deny_mechanism=write_deny_mechanism,
        write_deny_reason=write_deny_reason,
        cadence_attribution=cadence_attribution,
        stop_reason="all_ticks_complete",
        ticks=ticks,
    )


def _real_sleep_until(target_utc: datetime) -> None:
    """Real wall-clock sleep until the target UTC datetime (B2)."""
    now = datetime.fromtimestamp(time.time(), tz=timezone.utc)
    delta = (target_utc - now).total_seconds()
    if delta > 0:
        time.sleep(delta)


# ── CLI (live-only) ───────────────────────────────────────────────────
#
# B1: The live transport composition (build_canary_live_deps) lives OUTSIDE
# the architecture import seal — in tools/run_tokmo_pulse_canary.py — because
# it imports tokmo.facts.toss_auth_live / toss_prices_live and
# tokmo.runtime.config, which the seal restricts to composition.py and
# opendart_bulk_composition.py. The CLI accepts a pre-built CanaryDepsV1 via
# the `live_deps` parameter so the tool can inject the real production
# transport without the sealed module importing live adapters.
#
# B-1: The live CLI does NOT accept --root, --canary-base, or --campaign-id.
# The campaign evidence root is derived from Path.home() + an immutable
# relative constant (campaign_evidence_anchor_root). The market/symbol
# must be exactly CANARY_CAMPAIGN_MARKET/CANARY_CAMPAIGN_SYMBOL.
# Fixture mode is handled by tools/run_tokmo_pulse_canary_fixture.py.

def run_canary_cli(argv: "list[str] | None" = None, *,
                   live_deps_factory: "Callable | None" = None,
                   evidence_root_override: "Path | None" = None,
                   canonical_root_anchor_override: "Path | None" = None,
                   ) -> int:
    """CLI entrypoint for the bounded live canary (live-only).

    M2: exit 0 only for fresh all_ticks_complete.
    M3: hard-lock max_ticks==5, max_provider_starts==family cumulative cap.
    M-6: --canonical-root must equal the derived canonical anchor.
    M-7: OS write-denial is proven before any credential is assembled.
    M4: exact commit/hash + clean tree guard before live call.
    M5: evidence/canonical root disjoint check.
    B-1: campaign evidence root derived from Path.home() + immutable constant.
         --root, --canary-base, --campaign-id are NOT accepted in live mode.
         market/symbol must be exactly KR/005930 for this campaign.

    live_deps_factory: callable(*, project_root, clock) returning CanaryDepsV1.
                       This is how the tool (outside the architecture seal)
                       injects the real Toss production transport.

    evidence_root_override: test-only injection of a custom evidence root
                           (NOT a CLI flag). Used by tests and the fixture
                           tool to inject temp-dir roots. When None, the
                           anchored absolute root is used.

    canonical_root_anchor_override: test-only injection of the canonical
                           root the write-deny attestation is checked
                           against (NOT a CLI flag). When None, the
                           derived canonical_root_anchor() is used.
    """
    parser = argparse.ArgumentParser(
        prog="tokmo-pulse-canary", add_help=True,
    )
    parser.add_argument("--mode", default="bounded-live-canary")
    parser.add_argument("--market", default=None)
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--canonical-root", default=None)
    parser.add_argument("--project-root", default=None)
    parser.add_argument("--max-ticks", type=int, default=CANARY_MAX_TICKS)
    parser.add_argument("--max-provider-starts", type=int,
                        default=CANARY_MAX_PROVIDER_STARTS_DEFAULT)
    parser.add_argument("--base-commit", default=None)
    parser.add_argument("--t0", default=None,
                        help="t0 UTC ISO timestamp (default: now)")
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 1

    def refuse(reason: str) -> int:
        print(f"tokmo-pulse-canary refusal={reason}", file=sys.stderr)
        return 1

    if args.mode != "bounded-live-canary":
        return refuse("unsupported_mode")

    if not args.market or not args.symbol:
        return refuse("market_symbol_required")

    try:
        market = Market(args.market)
    except ValueError:
        return refuse("invalid_market")

    # B-1: market and symbol must be exactly the approved campaign values.
    # Different values fail before credential/provider assembly.
    if market != CANARY_CAMPAIGN_MARKET:
        return refuse(
            f"market_must_be_{CANARY_CAMPAIGN_MARKET.value}_for_this_campaign")
    if args.symbol != CANARY_CAMPAIGN_SYMBOL:
        return refuse(
            f"symbol_must_be_{CANARY_CAMPAIGN_SYMBOL}_for_this_campaign")

    if not args.project_root:
        return refuse("project_root_required")

    # M3: hard-lock max_ticks==5 and max_provider_starts==32 (v4 family cap)
    if args.max_ticks != CANARY_MAX_TICKS:
        return refuse(f"max_ticks_must_be_{CANARY_MAX_TICKS}")
    if args.max_provider_starts != CANARY_FAMILY_CUMULATIVE_CAP:
        return refuse(
            f"max_provider_starts_must_be_exactly_"
            f"{CANARY_FAMILY_CUMULATIVE_CAP}_for_v4_campaign")
    if args.max_provider_starts <= 0:
        return refuse("max_provider_starts_must_be_positive")

    try:
        config = CanaryConfigV1(
            market=market, symbol=args.symbol,
            max_ticks=args.max_ticks,
            max_provider_starts=args.max_provider_starts,
        )
    except ValueError as error:
        return refuse(f"invalid_config:{error}".replace(" ", "_"))

    # Determine t0
    if args.t0:
        try:
            t0 = datetime.fromisoformat(
                args.t0.replace("Z", "+00:00"))
        except ValueError:
            return refuse("invalid_t0")
    else:
        epoch = time.time()
        t0_epoch = int(epoch) - (int(epoch) % 60) + 60
        t0 = datetime.fromtimestamp(t0_epoch, tz=timezone.utc)

    # Validate t0 alignment
    if t0.microsecond != 0 or t0.second % 60 != 0:
        return refuse("t0_must_be_whole_second_aligned_to_60s_grid")

    # B-1: Derive evidence root from the absolute anchor.
    # In live mode, the root is always campaign_evidence_anchor_root().
    # Tests may inject evidence_root_override (NOT a CLI flag).
    canonical_root = Path(args.canonical_root) if args.canonical_root else None
    if evidence_root_override is not None:
        evidence_root = Path(evidence_root_override)
    else:
        evidence_root = campaign_evidence_anchor_root()

    # M5: validate evidence root and disjoint check
    try:
        ev_validated = _validate_evidence_root(evidence_root)
        _validate_root_disjoint(ev_validated, canonical_root)
    except ValueError as e:
        return refuse(f"root_validation:{e}".replace(" ", "_"))

    # M-2: live mode requires --canonical-root before provider start.
    if not args.canonical_root:
        return refuse("canonical_root_required_for_live_mode")

    # M4: exact commit/hash + clean tree guard before live call
    if not args.base_commit:
        return refuse("base_commit_required_for_live_mode")

    project_root = Path(args.project_root)

    # Verify HEAD matches base-commit
    import subprocess
    try:
        head_result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, cwd=str(project_root),
            timeout=5,
        )
        if head_result.returncode != 0:
            return refuse("git_not_available")
        actual_head = head_result.stdout.strip()
        # Allow both short and full hash forms
        if (actual_head != args.base_commit
                and not actual_head.startswith(args.base_commit)):
            return refuse(
                f"base_commit_mismatch:expected={args.base_commit}"
                f":actual={actual_head}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return refuse("git_revision_check_failed")

    # m-3: robust porcelain XY code parsing.
    # git status --porcelain outputs "XY path" where X=index status,
    # Y=worktree status. Any non-space in X or Y means dirty.
    try:
        status_result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, cwd=str(project_root),
            timeout=5,
        )
        if status_result.returncode != 0:
            return refuse("git_status_check_failed")
        for line in status_result.stdout.splitlines():
            if not line:
                continue
            xy = line[:2]
            # Any non-space character in XY means the tree is dirty
            if xy.strip():
                return refuse(f"dirty_tracked_tree:{line.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return refuse("git_status_check_failed")

    # v3 M-6: the canonical root is not caller-selectable in live mode.
    # Attesting write-denial against a path the caller chose would prove
    # nothing about the real canonical root — a decoy path the process
    # cannot write would forge a passing attestation. The expected anchor
    # is derived here; --canonical-root must match it exactly.
    expected_canonical = (Path(canonical_root_anchor_override)
                          if canonical_root_anchor_override is not None
                          else canonical_root_anchor())
    if canonical_root.resolve() != Path(expected_canonical).resolve():
        return refuse("canonical_root_must_be_the_derived_anchor")

    # v3 M-7: prove OS write-denial BEFORE credential/provider assembly.
    # This is a bounded EPERM probe sequence against the real canonical
    # root, not an environment variable — no caller can assert it. The
    # first probe is idempotent, so an unsandboxed run refuses here
    # without ever having written to the canonical root.
    attestation = attest_canonical_write_denied(
        canonical_root, nested_relatives=DEFAULT_PROTECTED_PATHS)
    if not attestation.denied:
        return refuse(
            f"sandbox_write_deny_not_proven:{attestation.reason}")

    # B1: live deps must be injected by the tool (outside the seal)
    if live_deps_factory is None:
        return refuse("live_deps_factory_required")

    def _utc_now():
        return datetime.fromtimestamp(time.time(), tz=timezone.utc)

    deps = live_deps_factory(
        project_root=project_root,
        clock=_utc_now,
    )

    clock_fn = _utc_now

    # v3: compute inherited provider starts from prior campaign family
    inherited = read_inherited_provider_starts(market, args.symbol)

    result = run_canary_session(
        root=evidence_root, config=config, deps=deps, t0=t0,
        canonical_root=canonical_root,
        protected_paths=DEFAULT_PROTECTED_PATHS,
        expected_mutable_paths=DEFAULT_EXPECTED_MUTABLE_PATHS,
        inherited_provider_starts=inherited,
        write_deny_attestation=attestation,
        cadence_receipt_reader=lambda: read_cadence_receipts_from_manifests() + read_cadence_receipts(canonical_root),
    )

    # Output sanitized summary
    summary = {
        "session_id": result.session_id,
        "completed_ticks": result.completed_ticks,
        "total_provider_starts": result.total_provider_starts,
        "inherited_provider_starts": result.inherited_provider_starts,
        "local_provider_starts": result.local_provider_starts,
        "family_cumulative_cap": CANARY_FAMILY_CUMULATIVE_CAP,
        "canary_canonical_write_denied_by_os": result.canary_canonical_write_denied_by_os,
        "write_deny_mechanism": result.write_deny_mechanism,
        "write_deny_probes": [
            {"probe": name, "verdict": verdict}
            for name, verdict in attestation.probes
        ],
        "cadence_attribution": list(result.cadence_attribution),
        "stop_reason": result.stop_reason,
        "market": config.market.value,
        "symbol": config.symbol,
        "max_ticks": config.max_ticks,
        "max_provider_starts": config.max_provider_starts,
        "campaign_id": CANARY_CAMPAIGN_ID,
        "campaign_version": CANARY_CAMPAIGN_VERSION,
        "family_id": CANARY_FAMILY_ID,
    }
    print(json.dumps(summary, sort_keys=True))

    # M2: exit 0 only for fresh all_ticks_complete
    if result.stop_reason == "all_ticks_complete" and result.is_fresh:
        return 0
    return 1
