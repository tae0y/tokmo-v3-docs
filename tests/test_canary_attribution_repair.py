"""G2.5 v3 post-run attribution RCA repair — TDD tests.

Root cause identified: adjudicate_safety_gate checked unattributed_changes
BEFORE attempting receipt attribution. With 1944 files in the canonical
root not in protected/expected lists, any concurrent file change triggers
an immediate unattributed_change fail without ever reading receipts.

Secondary design issue: receipt reader opened canonical SQLite inside
sandbox. While the read succeeds (journal_mode=delete, mode=ro works),
the architecture is fragile. The repair uses the Hermes runtime manifest
at ~/.hermes/runtime/tokmo-v3/cadence/<tick>/manifest.json — a fixed path
OUTSIDE the canonical root that requires no canonical writes and is
written by the scheduler parent, not the sandboxed process.

Test matrix:
1. Reproduce: unattributed change with OS write-deny + available receipt
   but adjudication returns unattributed (old behavior) — then show repair
2. Manifest-based receipt reader finds fresh COMPLETED receipt
3. Missing manifest => empty receipts => fail closed
4. Stale manifest (outside window) => not fresh => fail closed
5. Malformed manifest => fail closed
6. Nonzero exit code manifest => fail closed
7. Adjudication with OS write-deny + fresh receipt => PASS even with
   unattributed changes (OS proved canary can't write, receipt attributes)
8. Adjudication without OS write-deny + unattributed => still fail
9. Protected mutation with fresh receipt => PASS with attribution
10. Protected mutation without receipt => fail closed
11. No canonical write needed by receipt reader
12. Sandbox read of canonical SQLite works (regression proof)
"""

import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from tokmo.programs.canary_sandbox_attestation import CanonicalWriteDenyAttestation

T0 = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)
CANARY_WINDOW_START = datetime(2026, 8, 10, 23, 2, tzinfo=UTC)
CANARY_WINDOW_END = datetime(2026, 8, 10, 23, 7, tzinfo=UTC)


def _write_manifest(
    cadence_dir: Path,
    tick_id: str,
    *,
    started_at: str,
    finished_at: str,
    status: str = "SUCCEEDED",
    exit_code: int = 0,
    stdout_bytes: int = 2066,
    stderr_bytes: int = 0,
):
    """Write a realistic cadence manifest."""
    tick_dir = cadence_dir / tick_id
    tick_dir.mkdir(parents=True, exist_ok=True)
    manifest = {
        "job_id": f"tokmo-v3-cadence-{tick_id}",
        "schema_version": 2,
        "status": status,
        "child_exit_code": exit_code,
        "child_pid": 88843,
        "child_signal": None,
        "executable": "python",
        "started_at": started_at,
        "finished_at": finished_at,
        "stdout_bytes": stdout_bytes,
        "stderr_bytes": stderr_bytes,
        "timed_out": False,
        "wall_time_ms": 1156,
    }
    (tick_dir / "manifest.json").write_text(json.dumps(manifest))


# ══════════════════════════════════════════════════════════════════════
# 1. Manifest-based receipt reader finds fresh COMPLETED receipt
# ══════════════════════════════════════════════════════════════════════


def test_manifest_reader_finds_fresh_completed_receipt(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260810T2305",
        started_at="2026-08-10T23:05:25.106093+00:00",
        finished_at="2026-08-10T23:05:26.262612+00:00",
    )
    receipts = read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert len(receipts) == 1
    r = receipts[0]
    assert r.status == "COMPLETE"
    assert r.scheduled_for_utc == "20260810T2305"
    # Interval should overlap canary window
    begin, end = r.active_interval(timedelta(seconds=90))
    assert begin <= CANARY_WINDOW_END
    assert end >= CANARY_WINDOW_START


# ══════════════════════════════════════════════════════════════════════
# 2. Missing manifest => empty receipts
# ══════════════════════════════════════════════════════════════════════


def test_manifest_reader_missing_returns_empty(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    cadence_dir = tmp_path / "nonexistent"
    receipts = read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert receipts == ()


# ══════════════════════════════════════════════════════════════════════
# 3. Stale manifest (outside window) => not fresh
# ══════════════════════════════════════════════════════════════════════


def test_manifest_reader_stale_excluded(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260810T2100",
        started_at="2026-08-10T21:00:25+00:00",
        finished_at="2026-08-10T21:00:26+00:00",
    )
    receipts = read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert receipts == ()


# ══════════════════════════════════════════════════════════════════════
# 4. Malformed manifest => excluded
# ══════════════════════════════════════════════════════════════════════


def test_manifest_reader_malformed_excluded(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    tick_dir = cadence_dir / "20260810T2305"
    tick_dir.mkdir(parents=True)
    (tick_dir / "manifest.json").write_text("{not json}")
    receipts = read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert receipts == ()


# ══════════════════════════════════════════════════════════════════════
# 5. Nonzero exit code manifest => excluded (failed cadence)
# ══════════════════════════════════════════════════════════════════════


def test_manifest_reader_failed_cadence_excluded(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260810T2305",
        started_at="2026-08-10T23:05:25+00:00",
        finished_at="2026-08-10T23:05:26+00:00",
        status="FAILED",
        exit_code=1,
    )
    receipts = read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert receipts == ()


# ══════════════════════════════════════════════════════════════════════
# 6. Repaired adjudication: OS write-deny + fresh receipt =>
#    PASS even with unattributed changes
# ══════════════════════════════════════════════════════════════════════


def test_adjudication_os_deny_plus_receipt_passes_unattributed(tmp_path):
    """With OS write-deny + fresh cadence receipt, changes whose mtimes
    fall inside the receipt interval are attributed. Each path must
    individually have its mtime covered — no blanket waiver."""
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )
    from tokmo.programs.canary_safety_gate import SafetyGateResult
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260810T2305",
        started_at="2026-08-10T23:05:25+00:00",
        finished_at="2026-08-10T23:05:26+00:00",
    )

    def receipt_reader():
        return read_cadence_receipts_from_manifests(
            cadence_dir,
            window_start=CANARY_WINDOW_START,
            window_end=CANARY_WINDOW_END,
        )

    # Simulate a gate result with both protected mutations AND unattributed
    import hashlib

    inside_ns = int(datetime(2026, 8, 10, 23, 5, 25, tzinfo=UTC).timestamp() * 1e9)
    gate_result = SafetyGateResult(
        verdict="fail",
        reason="both",
        protected_mutations=("stores/latest_quotes.sqlite3",),
        unattributed_changes=("sec-targeted.sqlite3", "shadow/x.json"),
        full_root_pre_hash=hashlib.sha256(b"pre").hexdigest(),
        full_root_post_hash=hashlib.sha256(b"post").hexdigest(),
        full_root_changed=True,
        protected_mutation_mtimes=(("stores/latest_quotes.sqlite3", inside_ns),),
        unattributed_mutation_mtimes=(
            ("sec-targeted.sqlite3", inside_ns),
            ("shadow/x.json", inside_ns),
        ),
    )

    stop_reason, attribution = adjudicate_safety_gate(
        gate_result,
        write_denied=True,
        cadence_receipt_reader=receipt_reader,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    # With OS write-deny + fresh receipt, all changes are explained
    assert stop_reason == ""
    assert len(attribution) > 0


# ══════════════════════════════════════════════════════════════════════
# 7. Without OS write-deny + unattributed => still fail
# ══════════════════════════════════════════════════════════════════════


def test_adjudication_no_os_deny_unattributed_fails(tmp_path):

    from tokmo.programs.canary_safety_gate import SafetyGateResult
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    gate_result = SafetyGateResult(
        verdict="fail",
        reason="unattributed",
        protected_mutations=(),
        unattributed_changes=("sec-targeted.sqlite3",),
        full_root_pre_hash="x",
        full_root_post_hash="y",
        full_root_changed=True,
    )

    stop_reason, attribution = adjudicate_safety_gate(
        gate_result,
        write_denied=False,
        cadence_receipt_reader=lambda: (),
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert stop_reason == "safety_gate_unattributed_change"
    assert attribution == ()


# ══════════════════════════════════════════════════════════════════════
# 8. OS write-deny + no receipt => still fail (can't attribute)
# ══════════════════════════════════════════════════════════════════════


def test_adjudication_os_deny_no_receipt_fails(tmp_path):

    from tokmo.programs.canary_safety_gate import SafetyGateResult
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    gate_result = SafetyGateResult(
        verdict="fail",
        reason="unattributed",
        protected_mutations=(),
        unattributed_changes=("sec-targeted.sqlite3",),
        full_root_pre_hash="x",
        full_root_post_hash="y",
        full_root_changed=True,
    )

    stop_reason, attribution = adjudicate_safety_gate(
        gate_result,
        write_denied=True,
        cadence_receipt_reader=lambda: (),  # no receipts
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert stop_reason == "safety_gate_unattributed_change"
    assert attribution == ()


# ══════════════════════════════════════════════════════════════════════
# 9. Protected mutation with fresh receipt => PASS
# ══════════════════════════════════════════════════════════════════════


def test_protected_mutation_with_fresh_receipt_passes(tmp_path):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )
    from tokmo.programs.canary_safety_gate import SafetyGateResult
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260810T2305",
        started_at="2026-08-10T23:05:25+00:00",
        finished_at="2026-08-10T23:05:26+00:00",
    )

    def receipt_reader():
        return read_cadence_receipts_from_manifests(
            cadence_dir,
            window_start=CANARY_WINDOW_START,
            window_end=CANARY_WINDOW_END,
        )

    mtime_ns = int(datetime(2026, 8, 10, 23, 5, 26, tzinfo=UTC).timestamp() * 1e9)

    gate_result = SafetyGateResult(
        verdict="fail",
        reason="protected",
        protected_mutations=("stores/latest_quotes.sqlite3",),
        unattributed_changes=(),
        full_root_pre_hash="a",
        full_root_post_hash="b",
        full_root_changed=True,
        protected_mutation_mtimes=(("stores/latest_quotes.sqlite3", mtime_ns),),
    )

    stop_reason, attribution = adjudicate_safety_gate(
        gate_result,
        write_denied=True,
        cadence_receipt_reader=receipt_reader,
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert stop_reason == ""
    assert len(attribution) > 0


# ══════════════════════════════════════════════════════════════════════
# 10. Protected mutation without receipt => fail closed
# ══════════════════════════════════════════════════════════════════════


def test_protected_mutation_no_receipt_fails(tmp_path):
    from tokmo.programs.canary_safety_gate import SafetyGateResult
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    mtime_ns = int(datetime(2026, 8, 10, 23, 5, 26, tzinfo=UTC).timestamp() * 1e9)
    gate_result = SafetyGateResult(
        verdict="fail",
        reason="protected",
        protected_mutations=("stores/latest_quotes.sqlite3",),
        unattributed_changes=(),
        full_root_pre_hash="a",
        full_root_post_hash="b",
        full_root_changed=True,
        protected_mutation_mtimes=(("stores/latest_quotes.sqlite3", mtime_ns),),
    )

    stop_reason, attribution = adjudicate_safety_gate(
        gate_result,
        write_denied=True,
        cadence_receipt_reader=lambda: (),
        window_start=CANARY_WINDOW_START,
        window_end=CANARY_WINDOW_END,
    )
    assert "no_fresh_receipt" in stop_reason or "protected" in stop_reason


# ══════════════════════════════════════════════════════════════════════
# 11. Sandbox read of canonical SQLite works (regression proof)
# ══════════════════════════════════════════════════════════════════════


def test_canonical_sqlite_readonly_works_under_sandbox(tmp_path):
    """Prove that mode=ro on a canonical-style SQLite works even when
    the parent directory is read-only. Journal mode=delete doesn't
    need WAL/SHM files."""
    import sqlite3

    db = tmp_path / "canonical" / "ops" / "cadence_ticks.sqlite3"
    db.parent.mkdir(parents=True)
    conn = sqlite3.connect(str(db))
    conn.execute("CREATE TABLE t(x TEXT)")
    conn.execute("INSERT INTO t VALUES ('test')")
    conn.commit()
    conn.close()
    # Make parent read-only
    os.chmod(db.parent, 0o500)
    try:
        conn2 = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1.0)
        row = conn2.execute("SELECT x FROM t").fetchone()
        conn2.close()
        assert row[0] == "test"
    finally:
        os.chmod(db.parent, 0o755)


# ══════════════════════════════════════════════════════════════════════
# 12. Runtime manifest path is fixed from account home
# ══════════════════════════════════════════════════════════════════════


def test_runtime_manifest_path_is_fixed():
    from tokmo.programs.canary_cadence_attribution import (
        RUNTIME_CADENCE_DIR,
    )

    assert RUNTIME_CADENCE_DIR == (
        Path(__file__).resolve().parents[1] / "var" / "runtime" / "cadence"
    )


# ══════════════════════════════════════════════════════════════════════
# 13. Integration: run_canary_session with manifest-based receipts
# ══════════════════════════════════════════════════════════════════════


def test_integration_manifest_receipts_allow_concurrent_writes(tmp_path):
    """Full integration: safety gate detects changes, OS write-deny is true,
    manifest-based receipt reader finds fresh cadence receipt, session PASS."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1,
        CanaryDepsV1,
        run_canary_session,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.session_gate import Market

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"orig")
    (canonical / "ops").mkdir(parents=True)
    (canonical / "sec-targeted.sqlite3").write_bytes(b"sec-old")

    cadence_dir = tmp_path / "runtime" / "tokmo-v3" / "cadence"
    _write_manifest(
        cadence_dir,
        "20260811T0000",
        started_at="2026-08-11T00:00:01+00:00",
        finished_at="2026-08-11T00:00:03+00:00",
    )

    def quotes_fetch(market, symbols, *, bearer=None):
        # Concurrent cadence writes to multiple stores
        # Use controlled mtime inside manifest interval (T0+1s to T0+3s)
        controlled_ns = int((T0.timestamp() + 2) * 1e9)
        (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"changed")
        os.utime(
            canonical / "stores" / "latest_quotes.sqlite3",
            ns=(controlled_ns, controlled_ns),
        )
        (canonical / "sec-targeted.sqlite3").write_bytes(b"sec-new")
        os.utime(canonical / "sec-targeted.sqlite3", ns=(controlled_ns, controlled_ns))
        return PulseQuoteBatchOutcomeV1(
            "success",
            tuple(
                TossCurrentPriceFact(
                    source="toss-current-price-v1",
                    symbol=sym,
                    last_price="226500",
                    currency_raw="KRW",
                    timestamp=T0,
                    fetched_at=T0,
                )
                for sym in symbols
            ),
            (),
        )

    cfg = CanaryConfigV1(
        market=Market.KR,
        symbol="005930",
        max_ticks=5,
        max_provider_starts=22,
    )
    deps = CanaryDepsV1(
        clock=lambda: T0,
        token_fetch=lambda: "token",
        quotes_fetch=quotes_fetch,
        sleep_until=lambda t: None,
    )
    result = run_canary_session(
        root=tmp_path / "evidence",
        config=cfg,
        deps=deps,
        t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=("ops/",),
        inherited_provider_starts=12,
        write_deny_attestation=CanonicalWriteDenyAttestation(
            denied=True, reason="sandbox_exec", mechanism="sandbox-exec"
        ),
        cadence_receipt_reader=(lambda: _read_manifests(cadence_dir)),
    )
    assert result.stop_reason == "all_ticks_complete"
    assert len(result.cadence_attribution) > 0


def _read_manifests(cadence_dir):
    from tokmo.programs.canary_cadence_attribution import (
        read_cadence_receipts_from_manifests,
    )

    # T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
    # Session window is t0 to clock(), both are T0 in the virtual test
    return read_cadence_receipts_from_manifests(
        cadence_dir,
        window_start=T0,
        window_end=T0,
    )
