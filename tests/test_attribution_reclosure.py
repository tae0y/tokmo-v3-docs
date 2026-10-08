"""Targeted reclosure: unattributed-change mtime bounds.

The v3 attribution repair let ALL canonical changes pass when write_denied
+ any fresh receipt existed, only checking protected mtimes. Unattributed
changes were blanket-waived without checking if their mtimes fell inside
the cadence receipt interval. This violates the approved contract.

Repair: each changed path (protected AND unattributed) must individually
have its mtime covered by a fresh successful cadence receipt interval.

Test matrix:
1. Unattributed change inside cadence window + OS proof + receipt => PASS
2. Unattributed change outside cadence window + OS proof + receipt => FAIL
3. Mixed inside/outside unattributed => FAIL (outside one fails all)
4. Protected inside + unattributed outside => FAIL
5. Protected inside + unattributed inside => PASS
6. No receipt at all => FAIL
7. No OS proof + unattributed => FAIL
8. No changes => PASS
"""

from datetime import datetime, timezone, timedelta
from pathlib import Path
import hashlib
import os
import time

import pytest

T0 = datetime(2026, 8, 11, 0, 0, tzinfo=timezone.utc)
WINDOW_START = T0
WINDOW_END = T0


def _receipt(start_offset_s: int, end_offset_s: int):
    """Build a CadenceReceiptWindow at T0+offset."""
    from tokmo.programs.canary_cadence_attribution import CadenceReceiptWindow
    return CadenceReceiptWindow(
        policy_version="test",
        scheduled_for_utc="20260811T0000",
        status="COMPLETE",
        started_at=T0 + timedelta(seconds=start_offset_s),
        finished_at=T0 + timedelta(seconds=end_offset_s),
    )


def _gate_result(*, protected=(), protected_mtimes=(),
                 unattributed=(), unattributed_mtimes=()):
    from tokmo.programs.canary_safety_gate import SafetyGateResult
    return SafetyGateResult(
        verdict="fail",
        reason="test",
        protected_mutations=tuple(protected),
        unattributed_changes=tuple(unattributed),
        full_root_pre_hash="a",
        full_root_post_hash="b",
        full_root_changed=True,
        protected_mutation_mtimes=tuple(protected_mtimes),
        unattributed_mutation_mtimes=tuple(unattributed_mtimes),
    )


def _ns(offset_s: float) -> int:
    """Convert T0+offset_seconds to nanoseconds."""
    return int((T0.timestamp() + offset_s) * 1_000_000_000)


# ══════════════════════════════════════════════════════════════════════
# 1. Unattributed inside window + OS proof + receipt => PASS
# ══════════════════════════════════════════════════════════════════════

def test_unattributed_inside_window_passes_with_os_proof():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)  # cadence active T0 to T0+5s
    gate = _gate_result(
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(2)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == ""
    assert len(attr) > 0


# ══════════════════════════════════════════════════════════════════════
# 2. Unattributed outside window + OS proof + receipt => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_unattributed_outside_window_fails_even_with_os_proof():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)  # cadence active T0 to T0+5s
    gate = _gate_result(
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(600)),),  # 10 min later
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == "safety_gate_unattributed_change_outside_cadence_window"
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 3. Mixed inside/outside unattributed => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_mixed_inside_outside_unattributed_fails():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)
    gate = _gate_result(
        unattributed=("inside.sqlite3", "outside.sqlite3"),
        unattributed_mtimes=(
            ("inside.sqlite3", _ns(2)),
            ("outside.sqlite3", _ns(600)),
        ),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == "safety_gate_unattributed_change_outside_cadence_window"
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 4. Protected inside + unattributed outside => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_protected_inside_unattributed_outside_fails():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)
    gate = _gate_result(
        protected=("stores/latest_quotes.sqlite3",),
        protected_mtimes=(("stores/latest_quotes.sqlite3", _ns(2)),),
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(600)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == "safety_gate_unattributed_change_outside_cadence_window"
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 5. Protected inside + unattributed inside => PASS
# ══════════════════════════════════════════════════════════════════════

def test_protected_inside_unattributed_inside_passes():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)
    gate = _gate_result(
        protected=("stores/latest_quotes.sqlite3",),
        protected_mtimes=(("stores/latest_quotes.sqlite3", _ns(2)),),
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(3)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == ""
    assert len(attr) > 0


# ══════════════════════════════════════════════════════════════════════
# 6. No receipt at all => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_no_receipt_fails():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    gate = _gate_result(
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(2)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: (),
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert "safety_gate" in stop
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 7. No OS proof + unattributed => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_no_os_proof_unattributed_fails():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)
    gate = _gate_result(
        unattributed=("sec-targeted.sqlite3",),
        unattributed_mtimes=(("sec-targeted.sqlite3", _ns(2)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=False,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == "safety_gate_unattributed_change"
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 8. No changes => PASS
# ══════════════════════════════════════════════════════════════════════

def test_no_changes_passes():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate
    from tokmo.programs.canary_safety_gate import SafetyGateResult

    gate = SafetyGateResult(
        verdict="pass", reason="clean",
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: (),
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == ""
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 9. SafetyGateResult has unattributed_mutation_mtimes field
# ══════════════════════════════════════════════════════════════════════

def test_safety_gate_result_has_unattributed_mtimes():
    from tokmo.programs.canary_safety_gate import SafetyGateResult

    result = SafetyGateResult(
        verdict="fail", reason="test",
        unattributed_changes=("x.sqlite3",),
        unattributed_mutation_mtimes=(("x.sqlite3", 12345),),
    )
    assert result.unattributed_mutation_mtimes == (("x.sqlite3", 12345),)
    d = result.to_dict()
    assert "unattributed_mutation_mtimes" in d


# ══════════════════════════════════════════════════════════════════════
# 10. Protected outside window + OS proof + receipt => FAIL
# ══════════════════════════════════════════════════════════════════════

def test_protected_outside_window_fails():
    from tokmo.programs.pulse_canary import adjudicate_safety_gate

    covering = (_receipt(0, 5),)
    gate = _gate_result(
        protected=("stores/latest_quotes.sqlite3",),
        protected_mtimes=(("stores/latest_quotes.sqlite3", _ns(600)),),
    )
    stop, attr = adjudicate_safety_gate(
        gate, write_denied=True,
        cadence_receipt_reader=lambda: covering,
        window_start=WINDOW_START, window_end=WINDOW_END,
    )
    assert stop == "safety_gate_protected_mutation_outside_cadence_window"
    assert attr == ()


# ══════════════════════════════════════════════════════════════════════
# 11. Integration: run_canary_session enforces mtime bounds
# ══════════════════════════════════════════════════════════════════════

def test_integration_unattributed_outside_fails_in_session(tmp_path):
    """Full integration: unattributed change with mtime outside cadence
    interval fails even with OS write-deny attestation."""
    from tokmo.programs.pulse_canary import (
        CanaryConfigV1, CanaryDepsV1, run_canary_session,
    )
    from tokmo.programs.pulse_quotes import PulseQuoteBatchOutcomeV1
    from tokmo.values.market_observations import TossCurrentPriceFact
    from tokmo.values.session_gate import Market
    from tokmo.programs.canary_sandbox_attestation import CanonicalWriteDenyAttestation
    from tokmo.programs.canary_cadence_attribution import CadenceReceiptWindow

    canonical = tmp_path / "canonical"
    (canonical / "stores").mkdir(parents=True)
    (canonical / "stores" / "latest_quotes.sqlite3").write_bytes(b"prot")
    (canonical / "stranger.json").write_bytes(b"old")

    covering = (CadenceReceiptWindow(
        policy_version="test", scheduled_for_utc="20260811T0000",
        status="COMPLETE",
        started_at=T0, finished_at=T0 + timedelta(seconds=5),
    ),)

    def quotes_fetch(market, symbols, *, bearer=None):
        # Change stranger.json at T0+600s (10 min outside cadence interval)
        outside_ns = int((T0.timestamp() + 600) * 1e9)
        (canonical / "stranger.json").write_bytes(b"new")
        os.utime(canonical / "stranger.json", ns=(outside_ns, outside_ns))
        return PulseQuoteBatchOutcomeV1("success", tuple(
            TossCurrentPriceFact(
                source="t", symbol=s, last_price="1",
                currency_raw="KRW", timestamp=T0, fetched_at=T0,
            ) for s in symbols), ())

    cfg = CanaryConfigV1(market=Market.KR, symbol="005930",
                          max_ticks=5, max_provider_starts=22)
    deps = CanaryDepsV1(clock=lambda: T0, token_fetch=lambda: "t",
                         quotes_fetch=quotes_fetch, sleep_until=lambda t: None)
    attestation = CanonicalWriteDenyAttestation(
        denied=True, reason="test", mechanism="test")
    result = run_canary_session(
        root=tmp_path / "ev", config=cfg, deps=deps, t0=T0,
        canonical_root=canonical,
        protected_paths=("stores/latest_quotes.sqlite3",),
        expected_mutable_paths=(),
        inherited_provider_starts=12,
        write_deny_attestation=attestation,
        cadence_receipt_reader=lambda: covering,
    )
    assert result.stop_reason == "safety_gate_unattributed_change_outside_cadence_window"
