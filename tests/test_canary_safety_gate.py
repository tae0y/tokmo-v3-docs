"""TDD tests for the canary protected-path safety gate.

The old fingerprint_directory() hashed the entire canonical root content.
When the production cadence cron writes to its own stores concurrently,
the full-root hash changes — a false positive canonical_fingerprint_mutated.

The new safety gate:
- Predeclares protected paths (canary must NEVER mutate these) and
  content-hashes them pre/post.
- Optionally scans full root for diagnostic diff using lightweight metadata
  (size + mtime), avoiding content-hash of huge mutable DBs.
- Tolerates changes on expected_mutable paths (scheduler-owned writes).
- Fails closed on protected path mutation OR unattributed change.
- No change at all => PASS.

Test matrix:
1. No change at all => PASS
2. Expected mutable change + protected unchanged => PASS
3. Protected path mutation => FAIL
4. Unattributed change (not protected, not expected) => FAIL
5. Both expected and protected change => FAIL (protected dominates)
6. Diagnostic full root hash changes but all changes attributed => PASS
"""

from datetime import datetime, timezone
import json
import os
import time

import pytest


# ── Test 1: No change => PASS ──────────────────────────────────────────

def test_no_change_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"quote-data")
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-data")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()
    # No mutation
    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.protected_mutations == ()
    assert result.unattributed_changes == ()


# ── Test 2: Expected mutable change + protected unchanged => PASS ──────

def test_expected_mutable_change_protected_unchanged_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"quote-data")
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-old")
    (root / "ops" / "cadence_ticks.sqlite3").write_bytes(b"ticks-old")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(
            "ops/ops_kernel.sqlite3",
            "ops/cadence_ticks.sqlite3",
        ),
    )
    gate.snapshot_pre()

    # Scheduler writes to its own stores — expected
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-new-data")
    (root / "ops" / "cadence_ticks.sqlite3").write_bytes(b"ticks-new")

    # Ensure mtime changes even on fast filesystems
    os.utime(root / "ops" / "ops_kernel.sqlite3", ns=(time.time_ns(), time.time_ns()))

    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.protected_mutations == ()
    assert result.unattributed_changes == ()
    # Diagnostic: full root changed, but all changes attributed
    assert result.full_root_changed is True


# ── Test 3: Protected path mutation => FAIL ────────────────────────────

def test_protected_mutation_fails(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"original-quotes")
    (root / "stores" / "trade_ledger.sqlite3").write_bytes(b"original-ledger")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=(
            "stores/latest_quotes.sqlite3",
            "stores/trade_ledger.sqlite3",
        ),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()

    # Canary (or something) mutates a protected file
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"tampered")

    result = gate.evaluate_post()

    assert result.verdict == "fail"
    assert "stores/latest_quotes.sqlite3" in result.protected_mutations
    assert "protected_mutation" in result.reason


# ── Test 4: Unattributed change => FAIL ────────────────────────────────

def test_unattributed_change_fails(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"quote-data")
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-old")
    (root / "unknown").mkdir(parents=True)
    (root / "unknown" / "mystery.json").write_bytes(b"{}")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()

    # Something writes to a path that is neither protected nor expected
    (root / "unknown" / "mystery.json").write_bytes(b'{"changed": true}')
    os.utime(root / "unknown" / "mystery.json", ns=(time.time_ns(), time.time_ns()))

    result = gate.evaluate_post()

    assert result.verdict == "fail"
    assert "unknown/mystery.json" in result.unattributed_changes
    assert "unattributed" in result.reason


# ── Test 5: Both expected and protected change => FAIL (protected dom) ─

def test_both_change_protected_dominates(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"original")
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-old")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()

    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"tampered")
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-new")

    result = gate.evaluate_post()

    assert result.verdict == "fail"
    assert "stores/latest_quotes.sqlite3" in result.protected_mutations


# ── Test 6: Full root changed but all changes attributed => PASS ───────

def test_full_root_diagnostic_changed_but_all_attributed_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")
    (root / "ops").mkdir(parents=True)
    (root / "ops" / "ops_kernel.sqlite3").write_bytes(b"old")
    (root / "ops" / "cadence_ticks.sqlite3").write_bytes(b"old-ticks")
    (root / "news").mkdir(parents=True)
    (root / "news" / "external_news.sqlite3").write_bytes(b"old-news")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(
            "ops/ops_kernel.sqlite3",
            "ops/cadence_ticks.sqlite3",
            "news/external_news.sqlite3",
        ),
    )
    gate.snapshot_pre()

    # Multiple scheduler-owned writes
    for p, data in [
        ("ops/ops_kernel.sqlite3", b"new-kernel"),
        ("ops/cadence_ticks.sqlite3", b"new-ticks"),
        ("news/external_news.sqlite3", b"new-news"),
    ]:
        (root / p).write_bytes(data)
        os.utime(root / p, ns=(time.time_ns(), time.time_ns()))

    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.full_root_changed is True
    assert result.protected_mutations == ()
    assert result.unattributed_changes == ()


# ── Test 7: New file in expected-mutable dir => PASS (dir-level match) ─

def test_new_file_under_expected_mutable_dir_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")
    (root / "ops").mkdir(parents=True)

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/",),  # directory-level
    )
    gate.snapshot_pre()

    # Scheduler creates a new file under ops/
    (root / "ops" / "new_store.sqlite3").write_bytes(b"new")
    os.utime(root / "ops" / "new_store.sqlite3",
             ns=(time.time_ns(), time.time_ns()))

    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.unattributed_changes == ()


# ── Test 8: New unattributed file => FAIL ──────────────────────────────

def test_new_unattributed_file_fails(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()

    # A new file appears in an unexpected location
    (root / "suspicious.json").write_bytes(b"who-am-i")
    os.utime(root / "suspicious.json", ns=(time.time_ns(), time.time_ns()))

    result = gate.evaluate_post()

    assert result.verdict == "fail"
    assert "suspicious.json" in result.unattributed_changes


# ── Test 9: Empty root => PASS ─────────────────────────────────────────

def test_empty_root_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    root.mkdir()

    gate = CanarySafetyGate(
        root=root,
        protected_paths=(),
        expected_mutable_paths=(),
    )
    gate.snapshot_pre()
    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.full_root_changed is False


# ── Test 10: Nonexistent root => PASS (nothing to protect) ─────────────

def test_nonexistent_root_passes(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "does-not-exist"

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
    )
    gate.snapshot_pre()
    result = gate.evaluate_post()

    assert result.verdict == "pass"
    assert result.protected_mutations == ()


# ── Test 11: serialize/deserialize result (for evidence storage) ───────

def test_result_to_dict_roundtrip(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    gate.snapshot_pre()
    result = gate.evaluate_post()

    d = result.to_dict()
    assert isinstance(d, dict)
    assert d["verdict"] == "pass"
    assert "protected_mutations" in d
    assert "unattributed_changes" in d
    assert "full_root_changed" in d
    # JSON serializable
    json.dumps(d)


# ── Test 12: Default protected paths for tokmo-v3 canonical root ───────

def test_default_protected_paths_are_predeclared():
    """The safety gate must ship with predeclared protected paths
    for the tokmo-v3 canonical root."""
    from tokmo.programs.canary_safety_gate import DEFAULT_PROTECTED_PATHS

    assert isinstance(DEFAULT_PROTECTED_PATHS, tuple)
    assert len(DEFAULT_PROTECTED_PATHS) > 0
    # Must include key stores the canary must never mutate
    paths_joined = " ".join(DEFAULT_PROTECTED_PATHS)
    assert "latest_quotes" in paths_joined
    assert "trade_ledger" in paths_joined
    assert "holdings_snapshots" in paths_joined


def test_default_expected_mutable_paths_are_predeclared():
    from tokmo.programs.canary_safety_gate import DEFAULT_EXPECTED_MUTABLE_PATHS

    assert isinstance(DEFAULT_EXPECTED_MUTABLE_PATHS, tuple)
    assert len(DEFAULT_EXPECTED_MUTABLE_PATHS) > 0
    paths_joined = " ".join(DEFAULT_EXPECTED_MUTABLE_PATHS)
    assert "ops_kernel" in paths_joined
    assert "cadence_ticks" in paths_joined


# ── Test 13: No secrets/raw identifiers in evidence ────────────────────

def test_no_secrets_in_result(tmp_path):
    from tokmo.programs.canary_safety_gate import CanarySafetyGate

    root = tmp_path / "canonical"
    (root / "stores").mkdir(parents=True)
    (root / "stores" / "latest_quotes.sqlite3").write_bytes(
        b"account_seq=12345 token=abcdef")

    gate = CanarySafetyGate(
        root=root,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
    )
    gate.snapshot_pre()
    result = gate.evaluate_post()
    d = result.to_dict()

    # The result must not contain raw file content, only paths and hashes
    serialized = json.dumps(d)
    assert "12345" not in serialized
    assert "abcdef" not in serialized
    assert "account_seq" not in serialized


# ── Test 14: Integration — run_canary_session with safety gate ─────────

def test_run_canary_session_with_safety_gate_passes(tmp_path):
    """Integration: run_canary_session accepts safety gate params and
    returns all_ticks_complete when safety gate passes."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.session_gate import Market

    T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")
    (canonical / "ops").mkdir(parents=True)
    (canonical / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel")

    def quotes_fetch(market, symbols, *, bearer=None):
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path / "evidence", config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/ops_kernel.sqlite3",),
    )
    assert result.stop_reason == "all_ticks_complete"


def test_run_canary_session_safety_gate_protected_mutation_fails(tmp_path):
    """Integration: if a protected path mutates during canary, the safety
    gate fails and stop_reason reflects it."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.session_gate import Market

    T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")

    mutation_done = {"done": False}

    def quotes_fetch(market, symbols, *, bearer=None):
        if not mutation_done["done"]:
            (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(
                b"tampered")
            mutation_done["done"] = True
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="50000", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path / "evidence", config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
    )
    assert result.stop_reason == "safety_gate_protected_mutation"


def test_run_canary_session_safety_gate_concurrent_cadence_passes(tmp_path):
    """Integration: concurrent cadence-owned writes to expected_mutable
    paths do NOT cause failure — the original G2.5 false positive scenario."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.session_gate import Market

    T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"protected")
    (canonical / "ops").mkdir(parents=True)
    (canonical / "ops" / "ops_kernel.sqlite3").write_bytes(b"kernel-old")
    (canonical / "ops" / "cadence_ticks.sqlite3").write_bytes(b"ticks-old")

    call_count = {"n": 0}

    def quotes_fetch(market, symbols, *, bearer=None):
        call_count["n"] += 1
        # Simulate concurrent cadence writing to its own stores
        (canonical / "ops" / "ops_kernel.sqlite3").write_bytes(
            b"kernel-updated-%d" % call_count["n"])
        (canonical / "ops" / "cadence_ticks.sqlite3").write_bytes(
            b"ticks-updated-%d" % call_count["n"])
        os.utime(canonical / "ops" / "ops_kernel.sqlite3",
                 ns=(time.time_ns(), time.time_ns()))
        os.utime(canonical / "ops" / "cadence_ticks.sqlite3",
                 ns=(time.time_ns(), time.time_ns()))
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="toss-current-price-v1", symbol=sym,
                last_price="231500", currency_raw="KRW",
                timestamp=T0, fetched_at=T0,
            ) for sym in symbols
        ), ())

    deps = CanaryDepsV1(
        clock=lambda: T0, token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    cfg = CanaryConfigV1(
        market=Market.KR, symbol="005930",
        max_ticks=5, max_provider_starts=10,
    )
    result = run_canary_session(
        root=tmp_path / "evidence", config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(
            "ops/ops_kernel.sqlite3",
            "ops/cadence_ticks.sqlite3",
        ),
    )
    assert result.stop_reason == "all_ticks_complete"
